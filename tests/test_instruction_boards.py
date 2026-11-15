import base64
import io
from pathlib import Path

from PIL import Image
from sqlalchemy import select

from app.agents.runtime import AgentsRuntime
from app.data.models import AgentTraceEvent, PipelineRun, RunVariant, VariantAsset
from app.data.session import SessionLocal
from app.services.boards import board_context_for_run
from app.services.runs import execute_next_queued_stage


def run_worker_once() -> None:
    with SessionLocal() as db:
        execute_next_queued_stage(db)
        db.commit()


def png(colour=(198, 122, 84)) -> str:
    image = Image.new("RGB", (64, 64), colour)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def create_run(client, workspace: str, code: str) -> str:
    response = client.post("/runs", json={
        "workspace_name": workspace, "project_name": "studio-project",
        "product_name": code, "product_code": code, "industry_code": "apparel",
        "campaign_name": "studio-campaign", "creative_preset": "meta_square_5s",
        "pipeline_mode": "copy_image_only",
    })
    assert response.status_code == 200
    return response.json()["id"]


def seed_assets(run_id: str, uris: list[str]) -> None:
    with SessionLocal() as db:
        variant = RunVariant(run_id=run_id, variant_id="v1")
        db.add(variant)
        db.flush()
        for index, uri in enumerate(uris):
            db.add(VariantAsset(
                run_id=run_id, run_variant_id=variant.id, stage_name="copy_image_generation",
                asset_type="image", uri=uri, idempotency_key=f"asset-{index}", prompt_summary=uri,
            ))
        db.commit()


def scene(**overrides) -> dict:
    payload = {
        "version": 1,
        "goal": "把 A 的鞋面融合到 B 的场景里，保留 A 的细节",
        "items": [
            {
                "type": "asset", "id": "a1", "mark": 1, "assetId": "va_1",
                "uri": "artifacts/hero.png", "role": "source", "caption": "实拍主图",
                "x": 0, "y": 0, "w": 320, "h": 420,
            },
            {
                "type": "asset", "id": "a2", "mark": 2, "assetId": "va_2",
                "uri": "artifacts/case.png", "role": "reference", "caption": "竞品场景",
                "x": 400, "y": 0, "w": 320, "h": 420,
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
            {"id": "e1", "kind": "annotation", "from": "a1", "to": "a2", "label": "保留模特，只取背景"},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    payload.update(overrides)
    return payload


def save(client, run_id: str, **overrides):
    body = {
        "scene": scene(),
        "title": "融合主图",
        "actor": "human",
        "expected_revision": None,
        "snapshot": "",
    }
    body.update(overrides)
    return client.put(f"/runs/{run_id}/board", json=body)


def test_save_compile_and_read_board(client):
    run_id = create_run(client, "studio-shop", "BOARD-1")
    seed_assets(run_id, ["artifacts/hero.png", "artifacts/case.png"])

    response = save(client, run_id, snapshot=png())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 1
    assert body["scene"]["links"][0]["from"] == "a1"
    assert Path(body["snapshot_uri"]).exists()

    compiled = body["compiled"]
    assert "①" in compiled["digest"] and "②" in compiled["digest"]
    assert "-[annotation]->" in compiled["digest"]
    assert "鞋带换成黑色" in compiled["digest"]
    assert "主标题放左上留白" in compiled["digest"]
    assert compiled["assets"][0]["mark"] == 1
    assert compiled["regions"][0]["asset_mark"] == 1

    stored = client.get(f"/runs/{run_id}/board")
    assert stored.status_code == 200
    assert stored.json()["revision"] == 1
    assert stored.json()["title"] == "融合主图"

    history = client.get(f"/runs/{run_id}/boards")
    assert history.status_code == 200
    assert history.json()[0]["revision"] == 1

    revision = client.get(f"/runs/{run_id}/boards/1")
    assert revision.status_code == 200
    assert revision.json()["scene"]["goal"].startswith("把 A 的鞋面")

    with SessionLocal() as db:
        run = db.get(PipelineRun, run_id)
        context = board_context_for_run(db, run)
    assert context is not None
    assert context["revision"] == 1


def test_revision_guard_and_compile_preview(client):
    run_id = create_run(client, "studio-shop", "BOARD-2")
    seed_assets(run_id, ["artifacts/hero.png", "artifacts/case.png"])
    assert save(client, run_id).status_code == 200

    assert save(client, run_id).status_code == 409
    assert save(client, run_id, expected_revision=0).status_code == 409

    updated = save(client, run_id, expected_revision=1)
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2

    preview = client.post(f"/runs/{run_id}/board/compile", json={"scene": scene(goal="新的目标")})
    assert preview.status_code == 200
    assert preview.json()["revision"] == 3
    assert "新的目标" in preview.json()["digest"]


def test_board_rejects_invalid_scenes_and_assets(client):
    run_id = create_run(client, "studio-shop", "BOARD-3")
    seed_assets(run_id, ["artifacts/hero.png"])

    missing_asset = scene()
    missing_asset["items"][0]["uri"] = "artifacts/not-in-run.png"
    assert save(client, run_id, scene=missing_asset).status_code == 422

    out_of_bounds = scene()
    out_of_bounds["items"][2].update({"x": 0.8, "w": 0.4})
    assert save(client, run_id, scene=out_of_bounds).status_code == 422

    duplicate_marks = scene()
    duplicate_marks["items"][1]["mark"] = 1
    assert save(client, run_id, scene=duplicate_marks).status_code == 422

    missing_region_asset = scene()
    missing_region_asset["items"][2]["assetId"] = "missing"
    assert save(client, run_id, scene=missing_region_asset).status_code == 422

    missing_link_target = scene()
    missing_link_target["links"][0]["to"] = "missing"
    assert save(client, run_id, scene=missing_link_target).status_code == 422

    missing_parent = scene()
    missing_parent["items"][0]["parent"] = "missing-frame"
    assert save(client, run_id, scene=missing_parent).status_code == 422

    data_link_without_port = scene()
    data_link_without_port["links"][0].update({"kind": "data", "from": "a1", "to": "a2"})
    assert save(client, run_id, scene=data_link_without_port).status_code == 422

    assert save(client, run_id, snapshot="data:image/png;base64,bm90").status_code == 422


def test_board_reaches_the_planning_prompt(client, monkeypatch):
    captured: list[str] = []

    def capture_chat(self, provider, model, prompt, runtime_config, **kwargs):
        captured.append(prompt)
        return "stub summary", model, 0.0

    monkeypatch.setattr(AgentsRuntime, "_chat_complete", capture_chat)

    run_id = create_run(client, "studio-shop", "BOARD-6")
    scene_without_assets = {
        "version": 1,
        "goal": "只改标题位置",
        "items": [
            {
                "type": "note", "id": "n1", "text": "主标题放左上留白",
                "x": 0, "y": 0, "w": 240, "h": 120,
            },
        ],
        "links": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    assert save(client, run_id, scene=scene_without_assets).status_code == 200

    run_worker_once()  # intake
    assert client.post(f"/runs/{run_id}/advance", json={"notes": "approved"}).status_code == 200
    run_worker_once()  # planning

    planning_prompt = next(
        prompt for prompt in captured if "OPERATOR INSTRUCTION BOARD" in prompt
    )
    assert "OPERATOR INSTRUCTION BOARD" in planning_prompt
    assert "只改标题位置" in planning_prompt
    assert "主标题放左上留白" in planning_prompt

    run = client.get(f"/runs/{run_id}").json()
    assert any(task["stage_name"] == "planning" for task in run["stage_tasks"])

    with SessionLocal() as db:
        trace = db.scalar(
            select(AgentTraceEvent).where(
                AgentTraceEvent.run_id == run_id,
                AgentTraceEvent.event_type == "instruction_board_applied",
            )
        )
    assert trace is not None


def test_board_events_log_saves_and_agent_reads(client, monkeypatch):
    def capture_chat(self, provider, model, prompt, runtime_config, **kwargs):
        return "stub summary", model, 0.0

    monkeypatch.setattr(AgentsRuntime, "_chat_complete", capture_chat)

    run_id = create_run(client, "studio-shop", "BOARD-7")
    seed_assets(run_id, ["artifacts/hero.png", "artifacts/case.png"])
    assert save(client, run_id).status_code == 200

    changed = scene()
    changed["items"][3]["text"] = "主标题改到右上留白"
    assert save(client, run_id, scene=changed, expected_revision=1).status_code == 200

    events = client.get(f"/runs/{run_id}/board/events").json()
    assert events[0]["op"] == "save"
    assert events[0]["actor"] == "human"
    assert events[0]["actor_kind"] == "human"
    assert events[0]["revision"] == 2
    assert events[0]["payload"]["changed"] == 1
    assert events[1]["op"] == "create"

    run_worker_once()  # intake
    assert client.post(f"/runs/{run_id}/advance", json={"notes": "approved"}).status_code == 200
    run_worker_once()  # planning

    events = client.get(f"/runs/{run_id}/board/events").json()
    read = next(event for event in events if event["op"] == "read")
    assert read["actor"] == "planning_agent"
    assert read["actor_kind"] == "agent"
    assert read["revision"] == 2
    assert read["payload"]["assets"] == 2
    assert read["payload"]["regions"] == 1

    assert client.get("/runs/missing/board/events").status_code == 404


def test_board_tabs_duplicate_and_versions(client):
    run_id = create_run(client, "studio-shop", "BOARD-8")
    seed_assets(run_id, ["artifacts/hero.png", "artifacts/case.png"])
    first = save(client, run_id, snapshot=png()).json()
    first_id = first["id"]

    tabs = client.get(f"/runs/{run_id}/board-docs").json()
    assert len(tabs) == 1
    assert tabs[0]["id"] == first_id
    assert tabs[0]["is_current"] is True

    duplicate = client.post(
        f"/runs/{run_id}/board-docs",
        json={"source_board_id": first_id, "title": "融合主图 副本"},
    )
    assert duplicate.status_code == 200, duplicate.text
    copy = duplicate.json()
    assert copy["id"] != first_id
    assert copy["revision"] == 1
    assert copy["title"] == "融合主图 副本"
    assert copy["scene"] == first["scene"]

    tabs = client.get(f"/runs/{run_id}/board-docs").json()
    assert [tab["id"] for tab in tabs] == [first_id, copy["id"]]
    assert tabs[0]["is_current"] is False
    assert tabs[1]["is_current"] is True

    changed = scene(goal="保留 A 的鞋面，B 的背景换成暖色")
    saved = client.put(
        f"/runs/{run_id}/board-docs/{copy['id']}",
        json={
            "scene": changed,
            "title": "融合主图 副本",
            "actor": "human",
            "expected_revision": 1,
            "snapshot": png((12, 88, 120)),
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 2
    assert Path(saved.json()["snapshot_uri"]).exists()
    assert saved.json()["snapshot_uri"] != first["snapshot_uri"]

    revisions = client.get(f"/runs/{run_id}/board-docs/{copy['id']}/revisions").json()
    assert [row["revision"] for row in revisions] == [2, 1]
    revision_one = client.get(
        f"/runs/{run_id}/board-docs/{copy['id']}/revisions/1"
    ).json()
    assert revision_one["scene"]["goal"] == first["scene"]["goal"]

    renamed = client.patch(
        f"/runs/{run_id}/board-docs/{copy['id']}", json={"title": "方案 B"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "方案 B"

    blank = client.post(f"/runs/{run_id}/board-docs", json={})
    assert blank.status_code == 200
    assert blank.json()["revision"] == 0
    # A blank tab is not saved, so agents keep reading the previous board.
    assert client.get(f"/runs/{run_id}/board").json()["id"] == copy["id"]
    tabs = client.get(f"/runs/{run_id}/board-docs").json()
    assert [tab["is_current"] for tab in tabs] == [False, True, False]

    started = client.put(
        f"/runs/{run_id}/board-docs/{blank.json()['id']}",
        json={
            "scene": changed,
            "title": "空白开始",
            "actor": "human",
            "expected_revision": 0,
            "snapshot": "",
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["revision"] == 1
    assert client.get(f"/runs/{run_id}/board").json()["id"] == blank.json()["id"]

    stale = client.put(
        f"/runs/{run_id}/board-docs/{first_id}",
        json={
            "scene": changed,
            "title": "过期",
            "actor": "human",
            "expected_revision": 0,
            "snapshot": "",
        },
    )
    assert stale.status_code == 409

    assert client.delete(f"/runs/{run_id}/board-docs/{first_id}").status_code == 200
    assert client.get(f"/runs/{run_id}/board-docs/{first_id}").status_code == 404
    remaining = client.get(f"/runs/{run_id}/board-docs").json()
    assert [tab["id"] for tab in remaining] == [copy["id"], blank.json()["id"]]


def test_board_doc_rejects_foreign_run(client):
    run_id = create_run(client, "studio-shop", "BOARD-9")
    other_run = create_run(client, "studio-shop", "BOARD-10")
    seed_assets(run_id, ["artifacts/hero.png", "artifacts/case.png"])
    board_id = save(client, run_id).json()["id"]

    assert client.get(f"/runs/{other_run}/board-docs/{board_id}").status_code == 404
    assert client.delete(f"/runs/{other_run}/board-docs/{board_id}").status_code == 404
    assert client.post(
        f"/runs/{other_run}/board-docs", json={"source_board_id": board_id}
    ).status_code == 404


def test_board_scopes_to_run_and_workspace(client):
    run_id = create_run(client, "studio-shop", "BOARD-4")
    other_run = create_run(client, "other-shop", "BOARD-5")
    seed_assets(run_id, ["artifacts/hero.png", "artifacts/case.png"])
    seed_assets(other_run, ["artifacts/hero.png", "artifacts/case.png"])
    assert save(client, run_id).status_code == 200

    assert client.get(f"/runs/{other_run}/board").status_code == 404
    assert client.get("/runs/missing/board").status_code == 404
    assert save(client, other_run, scene=scene()).status_code == 200


def test_board_tool_endpoint_writes_one_revision(client):
    run_id = create_run(client, "studio-shop", "BOARD-TOOLS")
    seed_assets(run_id, ["artifacts/hero.png", "artifacts/case.png"])
    board_id = save(client, run_id, snapshot=png()).json()["id"]

    response = client.post(
        f"/runs/{run_id}/board-docs/{board_id}/tools",
        json={
            "name": "create_note",
            "args": {"text": "背景偏冷，建议换暖色", "intent": "finding"},
            "actor": "visual_qa_agent",
            "stage_task_id": "task-board-tools",
            "summary": "qa note",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["revision"] == 2
    note = body["board"]["scene"]["items"][-1]
    assert note["type"] == "note"
    assert note["intent"] == "finding"
    assert note["created_by"] == "agent:visual_qa_agent"

    events = client.get(f"/runs/{run_id}/board-docs/{board_id}/events").json()
    assert any(event["op"] == "tool:create_note" for event in events)
    assert any(event["op"] == "agent_edit" for event in events)

    rejected = client.post(
        f"/runs/{run_id}/board-docs/{board_id}/tools",
        json={
            "name": "create_region",
            "args": {"asset": "99", "box": [0.1, 0.1, 0.2, 0.2], "note": "找不到素材"},
        },
    )
    assert rejected.status_code == 422
    unknown = client.post(
        f"/runs/{run_id}/board-docs/{board_id}/tools",
        json={"name": "run_stage", "args": {}},
    )
    assert unknown.status_code == 422
    assert client.get(f"/runs/{run_id}/board-docs/{board_id}").json()["revision"] == 2
