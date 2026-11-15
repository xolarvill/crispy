"""End-to-end checks for the stdio ``boardwright`` MCP server.

These tests spawn the real server and talk to it through the MCP client, so
they cover the transport, the generated tool schemas and the in-process
``BoardToolSession`` commit path together.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from app.core.config import get_settings
from app.data.models import (
    InstructionBoard,
    InstructionBoardEvent,
    PipelineRun,
    RunVariant,
    VariantAsset,
)
from app.data.session import SessionLocal, get_active_database_url
from app.mcp.boardwright import parse_args
from app.schemas.boards import BoardScene
from app.services.board_tools import TOOL_SPECS

ROOT = Path(__file__).resolve().parents[1]
ACTOR = "external_agent"


def create_run(client, code: str) -> str:
    response = client.post("/runs", json={
        "workspace_name": "mcp-shop", "project_name": "mcp-project",
        "product_name": code, "product_code": code, "industry_code": "apparel",
        "campaign_name": "mcp-campaign", "creative_preset": "meta_square_5s",
        "pipeline_mode": "copy_image_only",
    })
    assert response.status_code == 200, response.text
    return response.json()["id"]


def save_human_board(client, run_id: str) -> dict:
    response = client.put(f"/runs/{run_id}/board", json={
        "scene": {
            "version": 1,
            "goal": "MCP board",
            "items": [],
            "links": [],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        },
        "title": "MCP board",
        "actor": "human",
        "expected_revision": None,
        "snapshot": "",
    })
    assert response.status_code == 200, response.text
    return response.json()


def seed_images(run_id: str) -> list[str]:
    assets_dir = get_settings().assets_dir / run_id
    assets_dir.mkdir(parents=True, exist_ok=True)
    uris: list[str] = []
    for index, colour in enumerate(((200, 40, 40), (40, 60, 200))):
        target = assets_dir / f"mcp-{index}.png"
        Image.new("RGB", (160, 120), colour).save(target, "PNG")
        uris.append(str(target))
    with SessionLocal() as db:
        variant = RunVariant(run_id=run_id, variant_id="v1")
        db.add(variant)
        db.flush()
        ids: list[str] = []
        for index, uri in enumerate(uris):
            row = VariantAsset(
                run_id=run_id,
                run_variant_id=variant.id,
                stage_name="copy_image_generation",
                asset_type="image",
                uri=uri,
                idempotency_key=f"mcp-asset-{index}",
            )
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
        return ids


def server_params(run_id: str, *, board_id: str | None = None) -> StdioServerParameters:
    args = [
        "-m", "app.mcp.boardwright",
        "--run-id", run_id,
        "--actor", ACTOR,
        "--database-url", get_active_database_url(),
        "--root", str(ROOT),
    ]
    if board_id:
        args.extend(["--board-id", board_id])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["CRISPY_ENABLE_WORKER"] = "false"
    return StdioServerParameters(
        command=sys.executable,
        args=args,
        env=env,
        cwd=str(ROOT),
    )


def payload(call_result) -> dict:
    for block in call_result.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise AssertionError("tool result had no text content")


async def _list_tools(params: StdioServerParameters):
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.list_tools()


async def _canvas_round_trip(params: StdioServerParameters, asset_ids: list[str]):
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return {
                "tools": await session.list_tools(),
                "board": payload(await session.call_tool("get_board", {"include": "full"})),
                "note": await session.call_tool("create_note", {
                    "text": "Keep the product hero centred",
                    "intent": "direction",
                }),
                "compose": await session.call_tool("compose", {
                    "items": asset_ids,
                    "layout": "row",
                    "title": "Candidate A/B",
                    "pin": True,
                    "caption": "MCP comparison",
                }),
            }


def test_cli_requires_run_id_and_actor(monkeypatch):
    monkeypatch.delenv("BOARDWRIGHT_RUN_ID", raising=False)
    monkeypatch.delenv("BOARDWRIGHT_ACTOR", raising=False)

    with pytest.raises(SystemExit):
        parse_args(["--actor", ACTOR])
    with pytest.raises(SystemExit):
        parse_args(["--run-id", "run-1"])
    with pytest.raises(SystemExit):
        parse_args(["--run-id", "run-1", "--actor", "   "])

    config = parse_args(["--run-id", "run-1", "--actor", ACTOR])
    assert config.run_id == "run-1"
    assert config.actor == ACTOR
    assert config.board_id is None


def test_mcp_lists_all_canvas_tools(client):
    run_id = create_run(client, "MCP-1")

    result = asyncio.run(_list_tools(server_params(run_id)))

    names = [tool.name for tool in result.tools]
    assert names == list(TOOL_SPECS)
    assert len(names) == 18

    by_name = {tool.name: tool for tool in result.tools}
    for spec in TOOL_SPECS.values():
        assert by_name[spec.name].description == spec.description
        assert by_name[spec.name].inputSchema["type"] == "object"

    note = by_name["create_note"]
    assert note.inputSchema["properties"]["text"]["maxLength"] > 0
    assert note.inputSchema["required"] == ["text"]
    assert note.inputSchema["additionalProperties"] is False
    assert note.annotations.readOnlyHint is False

    edge = by_name["create_edge"]
    assert "from" in edge.inputSchema["properties"]
    assert edge.inputSchema["required"] == ["from", "to"]

    assert by_name["get_board"].annotations.readOnlyHint is True
    # batch is read-like in the service but can contain writes.
    assert by_name["batch"].annotations.readOnlyHint is False


def test_mcp_create_note_and_compose_commit_revisions(client):
    run_id = create_run(client, "MCP-2")
    asset_ids = seed_images(run_id)
    board = save_human_board(client, run_id)
    board_id = board["id"]
    assert board["revision"] == 1

    outcome = asyncio.run(_canvas_round_trip(server_params(run_id, board_id=board_id), asset_ids))

    listed = {tool.name for tool in outcome["tools"].tools}
    assert listed == set(TOOL_SPECS)

    board_read = outcome["board"]
    assert board_read["ok"] is True
    assert board_read["revision"] == 1
    assert board_read["board_id"] == board_id
    assert board_read["result"]["ok"] is True

    note_call = outcome["note"]
    assert not note_call.isError
    note = payload(note_call)
    assert note["ok"] is True
    assert note["revision"] == 2
    assert note["result"]["node_id"]
    assert note["actor"] == f"agent:{ACTOR}"

    compose_call = outcome["compose"]
    assert not compose_call.isError
    compose = payload(compose_call)
    assert compose["ok"] is True
    assert compose["revision"] == 3
    assert compose["result"]["node_id"]
    assert compose["images"]
    assert any(getattr(block, "type", None) == "image" for block in compose_call.content)

    with SessionLocal() as db:
        saved = db.get(InstructionBoard, board_id)
        assert saved is not None
        assert saved.revision == 3
        scene = BoardScene.model_validate(saved.document)
        notes = [item for item in scene.items if item.type == "note"]
        assert len(notes) == 1
        assert notes[0].text == "Keep the product hero centred"
        assert notes[0].created_by == f"agent:{ACTOR}"
        sheet = next(
            item for item in scene.items if getattr(item, "assetId", "").startswith("compose_")
        )
        assert sheet.created_by == f"agent:{ACTOR}"

        events = db.query(InstructionBoardEvent).filter_by(board_id=board_id).all()
        assert any(event.op == "tool:create_note" and event.actor == f"agent:{ACTOR}" for event in events)
        assert any(event.op == "agent_edit" for event in events)
        run = db.get(PipelineRun, run_id)
        assert run is not None
