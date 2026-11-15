import base64
import io
from pathlib import Path

from PIL import Image

from app.core.config import get_settings
from app.data.models import (
    InstructionBoard,
    InstructionBoardEvent,
    PipelineRun,
    RunVariant,
    VariantAsset,
)
from app.data.session import SessionLocal
from app.schemas.boards import BoardScene
from app.services import board_tools as tools_module
from app.services.board_tools import BoardToolSession, MAX_COMPOSES_PER_PASS, MAX_OPS_PER_PASS
from app.services.boards import BoardConflict


def create_run(client, code: str) -> str:
    response = client.post("/runs", json={
        "workspace_name": "tool-shop", "project_name": "tool-project",
        "product_name": code, "product_code": code, "industry_code": "apparel",
        "campaign_name": "tool-campaign", "creative_preset": "meta_square_5s",
        "pipeline_mode": "copy_image_only",
    })
    assert response.status_code == 200
    return response.json()["id"]


def seed_media(run_id: str, uris: list[str], *, asset_type: str = "image") -> list[str]:
    with SessionLocal() as db:
        variant = RunVariant(run_id=run_id, variant_id="v1")
        db.add(variant)
        db.flush()
        ids = []
        for index, uri in enumerate(uris):
            row = VariantAsset(
                run_id=run_id, run_variant_id=variant.id, stage_name="copy_image_generation",
                asset_type=asset_type, uri=uri, idempotency_key=f"tool-asset-{index}",
            )
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
        return ids


def write_png(run_id: str, name: str, colour=(190, 120, 90)) -> str:
    target = get_settings().assets_dir / run_id / name
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (160, 120), colour).save(target, "PNG")
    return str(target)


def base_scene(**overrides) -> dict:
    payload = {
        "version": 1,
        "goal": "保留鞋面细节，只换背景",
        "items": [
            {
                "type": "asset", "id": "a1", "mark": 1, "assetId": "va_1",
                "uri": "artifacts/hero.png", "role": "source", "caption": "实拍主图",
                "x": 0, "y": 0, "w": 320, "h": 420,
            },
            {
                "type": "region", "id": "r1", "assetId": "a1",
                "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.2, "note": "鞋带换成黑色",
            },
            {
                "type": "note", "id": "n1", "text": "主标题放左上留白",
                "x": 100, "y": 500, "w": 240, "h": 120,
            },
        ],
        "links": [
            {"id": "e1", "kind": "annotation", "from": "a1", "to": "n1", "label": "说明"},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    payload.update(overrides)
    return payload


def save_human(client, run_id: str, *, scene: dict | None = None, snapshot: str = ""):
    response = client.put(f"/runs/{run_id}/board", json={
        "scene": scene or base_scene(),
        "title": "融合主图",
        "actor": "human",
        "expected_revision": None,
        "snapshot": snapshot,
    })
    assert response.status_code == 200, response.text
    return response.json()


def session_for(run_id: str, **kwargs) -> tuple[SessionLocal, BoardToolSession]:
    db = SessionLocal()
    run = db.get(PipelineRun, run_id)
    session = BoardToolSession(db, run, **kwargs)
    return db, session


def test_create_note_tags_provenance_and_commits_once(client):
    run_id = create_run(client, "TOOL-1")
    seed_media(run_id, ["artifacts/hero.png"])
    save_human(client, run_id)

    db, session = session_for(run_id, actor="planning_agent", stage_task_id="task-1")
    result = session.execute("create_note", {
        "text": "背景换成暖色棚拍", "intent": "direction", "near": "1",
    })
    assert result.ok, result.error
    node_id = result.data["node_id"]
    assert node_id.startswith("n_") and node_id.endswith("_01")

    second = session.execute("create_note", {"text": "为什么不用冷光？", "intent": "question"})
    assert second.ok
    assert second.data["node_id"].endswith("_02")

    board = session.commit(summary="directions placed")
    assert board is not None
    assert board.revision == 2
    board_id = board.id
    db.commit()
    db.close()

    with SessionLocal() as check:
        saved = check.get(InstructionBoard, board_id)
        scene = BoardScene.model_validate(saved.document)
        agent_notes = [item for item in scene.items if item.created_by == "agent:planning_agent"]
        assert len(agent_notes) == 2
        assert agent_notes[0].intent == "direction"
        assert agent_notes[1].intent == "question"
        events = check.query(InstructionBoardEvent).filter_by(board_id=board_id).all()
        ops = [event.op for event in events]
        assert "agent_edit" in ops
        assert ops.count("tool:create_note") == 2
        edit = next(event for event in events if event.op == "agent_edit")
        assert edit.actor == "agent:planning_agent"
        assert edit.payload["summary"] == "directions placed"


def test_agent_cannot_touch_human_nodes(client):
    run_id = create_run(client, "TOOL-2")
    seed_media(run_id, ["artifacts/hero.png"])
    save_human(client, run_id)

    db, session = session_for(run_id, actor="visual_qa_agent", stage_task_id="task-2")
    for name, args in [
        ("set_node_props", {"node_id": "a1", "caption": "mine now"}),
        ("move_nodes", {"node_ids": ["n1"], "dx": 10}),
        ("delete_nodes", {"node_ids": ["r1"]}),
    ]:
        result = session.execute(name, args)
        assert not result.ok, name
        assert "operator" in result.error or "read-only" in result.error

    region = session.execute("create_region", {
        "asset": "1", "box": [0.5, 0.5, 0.2, 0.2], "note": "logo 太贴边", "intent": "defect",
    })
    assert region.ok, region.error
    assert region.data["asset_mark"] == 1
    db.close()


def test_region_is_clamped_and_requires_an_asset(client):
    run_id = create_run(client, "TOOL-3")
    seed_media(run_id, ["artifacts/hero.png"])
    save_human(client, run_id)

    db, session = session_for(run_id, actor="visual_qa_agent", stage_task_id="task-3")
    clamped = session.execute("create_region", {
        "asset": "1", "box": [0.9, 0.9, 0.5, 0.5], "note": "边缘裁切",
    })
    assert clamped.ok, clamped.error
    box = clamped.data["box"]
    assert box[0] + box[2] <= 1.000001
    assert box[1] + box[3] <= 1.000001

    assert not session.execute("create_region", {
        "asset": "n1", "box": [0, 0, 0.2, 0.2], "note": "x",
    }).ok
    assert not session.execute("create_region", {
        "asset": "9", "box": [0, 0, 0.2, 0.2], "note": "x",
    }).ok
    db.close()


def test_place_assets_pins_dedupes_and_links(client):
    run_id = create_run(client, "TOOL-4")
    asset_ids = seed_media(run_id, ["artifacts/hero.png", "artifacts/out-1.png"])
    save_human(client, run_id)

    db, session = session_for(run_id, actor="copy_image_agent", stage_task_id="task-4")
    first = session.execute("place_assets", {
        "items": [{"asset_id": asset_ids[1], "caption": "生成 A", "role": "output"}],
        "link_from": "1",
        "layout": "row",
    })
    assert first.ok, first.error
    node = first.data["nodes"][0]
    assert node["mark"] == 2
    assert first.data["links"][0]["from"] == "a1"

    again = session.execute("place_assets", {
        "items": [{"asset_id": asset_ids[1], "caption": "生成 A"}],
    })
    assert again.ok
    assert again.data["nodes"] == []
    assert again.data["already_pinned"][0]["node_id"] == node["node_id"]

    board = session.commit(summary="output placed")
    board_id = board.id
    db.commit()
    db.close()

    with SessionLocal() as check:
        scene = BoardScene.model_validate(check.get(InstructionBoard, board_id).document)
        pinned = [item for item in scene.items if getattr(item, "assetId", None) == asset_ids[1]]
        assert len(pinned) == 1
        assert pinned[0].created_by == "agent:copy_image_agent"
        link = next(link for link in scene.links if link.to == pinned[0].id)
        assert link.created_by == "agent:copy_image_agent"


def test_pin_and_op_budgets(client):
    run_id = create_run(client, "TOOL-5")
    asset_ids = seed_media(run_id, ["artifacts/hero.png", *(f"artifacts/out-{index}.png" for index in range(8))])
    save_human(client, run_id)

    db, session = session_for(run_id, actor="copy_image_agent", stage_task_id="task-5")
    placed = session.execute("place_assets", {
        "items": [{"asset_id": asset_id} for asset_id in asset_ids[1:7]],
        "layout": "grid",
    })
    assert placed.ok, placed.error
    assert len(placed.data["nodes"]) == 6
    assert placed.data["pins_left"] == 0

    rejected = session.execute("place_assets", {"items": [{"asset_id": asset_ids[7]}]})
    assert not rejected.ok
    assert "pin" in rejected.error

    for index in range(MAX_OPS_PER_PASS - session.pending_writes):
        assert session.execute("create_note", {"text": f"note {index}"}).ok
    over = session.execute("create_note", {"text": "one too many"})
    assert not over.ok
    assert "writes" in over.error
    db.close()


def test_compose_writes_a_sheet_and_pins_it(client):
    run_id = create_run(client, "TOOL-6")
    first = write_png(run_id, "one.png", (200, 40, 40))
    second = write_png(run_id, "two.png", (40, 60, 200))
    asset_ids = seed_media(run_id, ["artifacts/hero.png", first, second])
    save_human(client, run_id)

    db, session = session_for(run_id, actor="evaluation_agent", stage_task_id="task-6")
    result = session.execute("compose", {
        "items": [asset_ids[1], asset_ids[2]],
        "title": "A/B 对比",
        "pin": True,
        "caption": "候选对比",
    })
    assert result.ok, result.error
    path = Path(result.data["uri"])
    assert path.is_file()
    assert result.images == [result.data["uri"]]
    with Image.open(path) as sheet:
        assert sheet.width > sheet.height  # a 2-up row
    assert result.data["node_id"]

    board = session.commit(summary="comparison sheet")
    board_id = board.id
    db.commit()
    db.close()
    with SessionLocal() as check:
        scene = BoardScene.model_validate(check.get(InstructionBoard, board_id).document)
        sheet_node = next(item for item in scene.items if getattr(item, "assetId", "").startswith("compose_"))
        assert sheet_node.created_by == "agent:evaluation_agent"
        assert Path(sheet_node.uri).is_file()


def test_compose_budget_and_screenshot_fallback(client):
    run_id = create_run(client, "TOOL-7")
    uri = write_png(run_id, "only.png", (20, 140, 90))
    seed_media(run_id, ["artifacts/hero.png", uri])
    scene = base_scene()
    scene["items"][0]["uri"] = uri
    save_human(client, run_id, scene=scene)  # human save without a snapshot

    db, session = session_for(run_id, actor="evaluation_agent", stage_task_id="task-7")
    shot = session.execute("get_screenshot", {})
    assert shot.ok, shot.error
    assert shot.data["source"] == "composed"
    assert Path(shot.data["uri"]).is_file()

    for index in range(MAX_COMPOSES_PER_PASS - 1):
        assert session.execute("compose", {
            "items": ["1"], "pin": False, "title": f"sheet {index}",
        }).ok
    over = session.execute("compose", {"items": ["1"], "pin": False})
    assert not over.ok
    assert "compose" in over.error
    db.close()


def test_conflict_retry_replays_onto_the_new_scene(client, monkeypatch):
    run_id = create_run(client, "TOOL-8")
    seed_media(run_id, ["artifacts/hero.png"])
    save_human(client, run_id)

    db, session = session_for(run_id, actor="planning_agent", stage_task_id="task-8")
    assert session.execute("create_note", {"text": "agent note", "intent": "finding"}).ok

    # A human saves a new revision while the agent pass is still open. The run
    # session sees it because both writes share this test's transaction.
    from app.services.boards import save_board

    human_scene = BoardScene.model_validate(base_scene())
    human_scene.items.append(
        tools_module.BoardNoteNode(
            id="human-2", type="note", text="human note", x=10, y=10, w=200, h=100,
        )
    )
    save_board(db, db.get(PipelineRun, run_id), human_scene, actor="human", expected_revision=1)

    real = tools_module.save_board
    calls = {"count": 0}

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise BoardConflict("stale revision")
        return real(*args, **kwargs)

    monkeypatch.setattr(tools_module, "save_board", flaky)
    board = session.commit(summary="retried")
    assert calls["count"] == 2
    assert board.revision == 3
    board_id = board.id
    db.commit()
    db.close()

    with SessionLocal() as check:
        scene = BoardScene.model_validate(check.get(InstructionBoard, board_id).document)
        texts = {getattr(item, "text", "") for item in scene.items}
        assert {"human note", "agent note"} <= texts
        agent_ids = [item.id for item in scene.items if item.created_by == "agent:planning_agent"]
        assert len(agent_ids) == len(set(agent_ids)) == 1


def test_agent_commit_keeps_the_last_human_snapshot(client):
    run_id = create_run(client, "TOOL-9")
    seed_media(run_id, ["artifacts/hero.png"])
    image = Image.new("RGB", (32, 32), (30, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    snapshot = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    saved = save_human(client, run_id, snapshot=snapshot)
    assert Path(saved["snapshot_uri"]).exists()

    db, session = session_for(run_id, actor="planning_agent", stage_task_id="task-9")
    assert session.execute("create_note", {"text": "keep the frame"}).ok
    board = session.commit(summary="note")
    revision = board.revision
    snapshot_uri = board.snapshot_uri
    db.commit()
    db.close()
    assert snapshot_uri == saved["snapshot_uri"]
    assert revision == 2


def test_digest_keeps_agent_content_out_of_operator_instructions(client):
    run_id = create_run(client, "TOOL-10")
    asset_ids = seed_media(run_id, ["artifacts/hero.png", "artifacts/out.png"])
    save_human(client, run_id)

    db, session = session_for(run_id, actor="planning_agent", stage_task_id="task-10")
    session.execute("create_note", {"text": "我自己写的猜测", "intent": "explanation"})
    session.execute("place_assets", {"items": [{"asset_id": asset_ids[1]}]})
    session.commit(summary="agent pass")
    db.commit()
    db.close()

    with SessionLocal() as check:
        from app.services.boards import board_context_for_run

        context = board_context_for_run(check, check.get(PipelineRun, run_id))
    assert "我自己写的猜测" not in context["digest"]
    assert "(agent output)" in context["digest"]
    assert context["agent_annotations"]
    assert any(item["text"] == "我自己写的猜测" for item in context["agent_annotations"])


def test_batch_and_create_board_as_first_write(client):
    run_id = create_run(client, "TOOL-11")
    seed_media(run_id, ["artifacts/hero.png"])
    save_human(client, run_id)

    db, session = session_for(run_id, actor="planning_agent", stage_task_id="task-11")
    batch = session.execute("batch", {"calls": [
        {"name": "create_note", "args": {"text": "one"}},
        {"name": "create_note", "args": {"text": "two", "intent": "question"}},
    ]})
    assert batch.ok
    assert len(batch.data["results"]) == 2
    assert all(row["result"]["ok"] for row in batch.data["results"])

    late = session.execute("create_board", {"title": "late tab"})
    assert not late.ok

    db2, fresh = session_for(run_id, actor="planning_agent", stage_task_id="task-11b")
    created = fresh.execute("create_board", {"title": "方案 B"})
    assert created.ok, created.error
    tab_id = created.data["board_id"]
    assert fresh.execute("create_note", {"text": "tab note"}).ok
    board = fresh.commit(summary="new tab")
    assert board.id == tab_id
    db2.commit()
    db2.close()
    db.close()


def test_read_tools_report_run_state_and_assets(client):
    run_id = create_run(client, "TOOL-12")
    asset_ids = seed_media(run_id, ["artifacts/hero.png"])
    save_human(client, run_id)

    db, session = session_for(run_id, actor="visual_qa_agent", stage_task_id="task-12")
    board = session.execute("get_board", {"include": "full"})
    assert board.ok
    assert board.data["revision"] == 1
    assert any(node["type"] == "asset" for node in board.data["nodes"])
    assert "鞋带换成黑色" in board.data["digest"]

    annotations = session.execute("get_annotations", {})
    assert annotations.ok
    assert annotations.data["regions"][0]["asset_mark"] == 1

    search = session.execute("search_nodes", {"query": "标题"})
    assert search.ok
    assert search.data["count"] == 1

    assets = session.execute("list_assets", {"kind": "image"})
    assert assets.ok
    assert assets.data["assets"][0]["asset_id"] == asset_ids[0]
    assert assets.data["assets"][0]["pinned"] is True

    status = session.execute("get_run_status", {})
    assert status.ok
    assert status.data["run"]["id"] == run_id
    assert isinstance(status.data["stages"], list)
    db.close()


def test_invalid_tool_calls_return_errors_not_exceptions(client):
    run_id = create_run(client, "TOOL-13")
    seed_media(run_id, ["artifacts/hero.png"])
    db, session = session_for(run_id, actor="planning_agent", stage_task_id="task-13")
    assert not session.execute("nope", {}).ok
    assert not session.execute("create_note", {}).ok
    assert not session.execute("create_note", {"text": "x", "intent": "bogus"}).ok
    assert not session.execute("place_assets", {"items": [{"uri": "C:/secrets.png"}]}).ok
    db.close()
