from dataclasses import dataclass, field

import pytest

from sqlalchemy import select

from app.data.models import (
    InstructionBoard,
    InstructionBoardEvent,
    PipelineRun,
    RunVariant,
    StageTask,
    VariantAsset,
)
from app.data.session import SessionLocal
from app.schemas.boards import BoardScene
from app.services.board_tool_pass import run_board_tool_pass


def create_run(client, code: str, mode: str = "copy_image_only") -> str:
    response = client.post("/runs", json={
        "workspace_name": "pass-shop", "project_name": "pass-project",
        "product_name": code, "product_code": code, "industry_code": "apparel",
        "campaign_name": "pass-campaign", "creative_preset": "meta_square_5s",
        "pipeline_mode": mode,
    })
    assert response.status_code == 200
    return response.json()["id"]


def seed_assets(run_id: str, uris: list[str]) -> list[str]:
    with SessionLocal() as db:
        variant = RunVariant(run_id=run_id, variant_id="v1")
        db.add(variant)
        db.flush()
        ids = []
        for index, uri in enumerate(uris):
            row = VariantAsset(
                run_id=run_id, run_variant_id=variant.id, stage_name="copy_image_generation",
                asset_type="image", uri=uri, idempotency_key=f"pass-asset-{index}",
            )
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
        return ids


def save_board(client, run_id: str, *, scene: dict | None = None) -> None:
    payload = scene or {
        "version": 1,
        "goal": "保留鞋面，只换背景",
        "items": [
            {
                "type": "asset", "id": "a1", "mark": 1, "assetId": "va_1",
                "uri": "artifacts/hero.png", "role": "source", "caption": "实拍",
                "x": 0, "y": 0, "w": 320, "h": 420,
            },
        ],
        "links": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    response = client.put(f"/runs/{run_id}/board", json={
        "scene": payload, "title": "pass board", "actor": "human",
        "expected_revision": None, "snapshot": "",
    })
    assert response.status_code == 200, response.text


def task_for(db, run_id: str, stage: str) -> StageTask:
    return db.scalar(
        select(StageTask).where(StageTask.run_id == run_id, StageTask.stage_name == stage)
    )


class FakeRuntime:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.image_urls: list[list[str] | None] = []

    def _chat_complete(self, provider, model, prompt, runtime_config, *, image_urls=None, video_urls=None):
        self.prompts.append(prompt)
        self.image_urls.append(list(image_urls or []))
        if not self.responses:
            return '{"summary": "done", "tool_calls": []}', model, 0.0
        return self.responses.pop(0), model, 0.01

    def _local_image_to_review_data_url(self, path: str, *, max_side: int = 1024) -> str:
        return "data:image/jpeg;base64,AAAA"


@dataclass
class FakeContext:
    db: object
    run: PipelineRun
    task: StageTask
    runtime: FakeRuntime
    lead_agent: str
    traces: list = field(default_factory=list)

    def emit_trace(self, event_type, message, payload=None):
        self.traces.append({"event_type": event_type, "message": message, "payload": payload or {}})


def context_for(db, run_id: str, stage: str, runtime: FakeRuntime, agent: str = "planning_agent") -> FakeContext:
    run = db.get(PipelineRun, run_id)
    task = task_for(db, run_id, stage)
    assert task is not None
    return FakeContext(db=db, run=run, task=task, runtime=runtime, lead_agent=agent)


def test_pass_writes_notes_and_commits_one_revision(client):
    run_id = create_run(client, "PASS-1")
    seed_assets(run_id, ["artifacts/hero.png"])
    save_board(client, run_id)

    runtime = FakeRuntime([
        '{"summary": "direction placed", "tool_calls": ['
        '{"name": "create_note", "args": {"text": "背景换成暖色", "intent": "direction"}}]}',
        '{"summary": "done", "tool_calls": []}',
    ])
    with SessionLocal() as db:
        context = context_for(db, run_id, "planning", runtime)
        result = run_board_tool_pass(
            context,
            runtime_config={"extra": {"board_tools": "on"}},
            provider_name="stub",
            model_name="stub-model",
            stage_output={"summary": "planned"},
        )
        assert result.ran
        assert result.revision == 2
        assert result.ops == 1
        assert result.estimated_cost == pytest.approx(0.02)
        assert len(runtime.prompts) == 2
        assert "BOARD TOOLS" in runtime.prompts[0]
        assert "CURRENT BOARD r1" in runtime.prompts[0]
        assert any(trace["event_type"] == "board_tools_completed" for trace in context.traces)
        assert context.task.metadata_json["board_tool_revision"] == 2
        db.commit()
        board = db.scalar(select(InstructionBoard).where(InstructionBoard.run_id == run_id))
        scene = BoardScene.model_validate(board.document)
        assert any(item.created_by == "agent:planning_agent" for item in scene.items)
        events = db.query(InstructionBoardEvent).filter_by(board_id=board.id).all()
        assert "agent_edit" in {event.op for event in events}
        assert "tool:create_note" in {event.op for event in events}


def test_questions_are_saved_and_traced(client):
    run_id = create_run(client, "PASS-2")
    seed_assets(run_id, ["artifacts/hero.png"])
    save_board(client, run_id)

    runtime = FakeRuntime([
        '{"summary": "one question", "tool_calls": ['
        '{"name": "create_note", "args": {"text": "背景要暖到什么程度？", "intent": "question"}}]}',
        '{"summary": "done", "tool_calls": []}',
    ])
    with SessionLocal() as db:
        context = context_for(db, run_id, "planning", runtime)
        result = run_board_tool_pass(
            context,
            runtime_config={},
            provider_name="stub",
            model_name="stub-model",
            stage_output={},
        )
        assert result.questions == ["背景要暖到什么程度？"]
        assert context.task.metadata_json["board_questions"] == result.questions
        questions = [trace for trace in context.traces if trace["event_type"] == "board_question"]
        assert len(questions) == 1


def test_protocol_error_gets_one_repair_round(client):
    run_id = create_run(client, "PASS-3")
    seed_assets(run_id, ["artifacts/hero.png"])
    save_board(client, run_id)

    runtime = FakeRuntime([
        "I will annotate the board now.",
        '{"summary": "fixed", "tool_calls": ['
        '{"name": "create_note", "args": {"text": "finding", "intent": "finding"}}]}',
        '{"summary": "done", "tool_calls": []}',
    ])
    with SessionLocal() as db:
        context = context_for(db, run_id, "planning", runtime)
        result = run_board_tool_pass(
            context,
            runtime_config={},
            provider_name="stub",
            model_name="stub-model",
            stage_output={},
        )
        assert result.revision == 2
        assert len(runtime.prompts) == 3
        assert any(error.get("error", "").startswith("protocol:") for error in result.errors)
        assert "protocol_error" in runtime.prompts[1]


def test_provider_failure_never_raises_and_keeps_the_board(client):
    run_id = create_run(client, "PASS-4")
    seed_assets(run_id, ["artifacts/hero.png"])
    save_board(client, run_id)

    class BrokenRuntime(FakeRuntime):
        def _chat_complete(self, *args, **kwargs):
            raise RuntimeError("provider down")

    with SessionLocal() as db:
        context = context_for(db, run_id, "planning", BrokenRuntime([]))
        result = run_board_tool_pass(
            context,
            runtime_config={},
            provider_name="stub",
            model_name="stub-model",
            stage_output={},
        )
        assert result.ran
        assert result.revision is None
        assert any("provider" in error.get("error", "") for error in result.errors)
        assert any(trace["event_type"] == "board_tools_failed" for trace in context.traces)
        board = db.scalar(select(InstructionBoard).where(InstructionBoard.run_id == run_id))
        assert board.revision == 1


def test_disabled_pass_makes_no_model_calls(client):
    run_id = create_run(client, "PASS-5")
    seed_assets(run_id, ["artifacts/hero.png"])
    save_board(client, run_id)

    runtime = FakeRuntime([])
    with SessionLocal() as db:
        context = context_for(db, run_id, "planning", runtime)
        result = run_board_tool_pass(
            context,
            runtime_config={"extra": {"board_tools": "off"}},
            provider_name="stub",
            model_name="stub-model",
            stage_output={},
        )
    assert not result.ran
    assert runtime.prompts == []


def test_annotation_only_stage_skips_when_no_board_exists(client):
    run_id = create_run(client, "PASS-6")
    seed_assets(run_id, ["artifacts/hero.png"])

    runtime = FakeRuntime([])
    with SessionLocal() as db:
        context = context_for(db, run_id, "planning", runtime)
        result = run_board_tool_pass(
            context,
            runtime_config={},
            provider_name="stub",
            model_name="stub-model",
            stage_output={},
        )
    assert not result.ran
    assert runtime.prompts == []


def test_asset_stage_creates_a_board_and_pins_outputs(client):
    run_id = create_run(client, "PASS-7")
    asset_ids = seed_assets(run_id, ["artifacts/hero.png", "artifacts/out.png"])

    runtime = FakeRuntime([
        '{"summary": "output pinned", "tool_calls": ['
        '{"name": "place_assets", "args": {"items": [{"asset_id": "' + asset_ids[1] + '", '
        '"caption": "生成图", "role": "output"}]}}]}',
        '{"summary": "done", "tool_calls": []}',
    ])
    with SessionLocal() as db:
        context = context_for(db, run_id, "copy_image_generation", runtime, agent="copy_image_agent")
        result = run_board_tool_pass(
            context,
            runtime_config={},
            provider_name="stub",
            model_name="stub-model",
            stage_output={"image_assets": [{"uri": "artifacts/out.png"}]},
        )
        db.commit()
        assert result.revision == 1
        board = db.scalar(select(InstructionBoard).where(InstructionBoard.run_id == run_id))
        assert board is not None
        scene = BoardScene.model_validate(board.document)
        assert any(
            item.type == "asset" and item.created_by == "agent:copy_image_agent" for item in scene.items
        )


def test_compose_images_are_attached_to_the_next_round(client):
    from pathlib import Path

    from PIL import Image

    from app.core.config import get_settings

    run_id = create_run(client, "PASS-8")
    target = get_settings().assets_dir / run_id / "a.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (120, 90), (120, 60, 30)).save(target, "PNG")
    seed_assets(run_id, ["artifacts/hero.png", str(target)])
    save_board(client, run_id)

    uri = str(target).replace("\\", "/")
    runtime = FakeRuntime([
        '{"summary": "sheet", "tool_calls": [{"name": "compose", "args": {'
        '"items": ["' + uri + '"], "pin": false, "title": "one"}}]}',
        '{"summary": "done", "tool_calls": []}',
    ])
    with SessionLocal() as db:
        context = context_for(db, run_id, "planning", runtime)
        result = run_board_tool_pass(
            context,
            runtime_config={},
            provider_name="stub",
            model_name="stub-model",
            stage_output={},
        )
        assert result.revision is None  # compose without pin is a read-like write
        assert len(runtime.image_urls) == 2
        assert runtime.image_urls[1] == ["data:image/jpeg;base64,AAAA"]
        assert Path(result.images[0]).exists()


def run_worker_once() -> None:
    from app.services.runs import execute_next_queued_stage

    with SessionLocal() as db:
        execute_next_queued_stage(db)
        db.commit()


def _planning_run(client, code: str) -> str:
    run_id = create_run(client, code)
    seed_assets(run_id, ["artifacts/hero.png"])
    save_board(client, run_id)
    run_worker_once()  # intake
    assert client.post(f"/runs/{run_id}/advance", json={"notes": "approved"}).status_code == 200
    return run_id


def test_planning_stage_annotates_the_board_end_to_end(client, monkeypatch):
    from app.agents.runtime import AgentsRuntime
    from app.data.models import AgentTraceEvent

    seen = {"board_prompt": False}

    def stub_chat(self, provider, model, prompt, runtime_config, **kwargs):
        if "BOARD TOOLS" in prompt:
            if seen["board_prompt"]:
                return '{"summary": "done", "tool_calls": []}', model, 0.0
            seen["board_prompt"] = True
            return (
                '{"summary": "direction", "tool_calls": [{"name": "create_note", '
                '"args": {"text": "暖色背景，保留鞋面", "intent": "direction"}}]}',
                model,
                0.0,
            )
        return "stub summary", model, 0.0

    monkeypatch.setattr(AgentsRuntime, "_chat_complete", stub_chat)
    run_id = _planning_run(client, "PASS-9")
    run_worker_once()  # planning + board pass

    with SessionLocal() as db:
        task = task_for(db, run_id, "planning")
        assert task.status == "waiting_review"
        assert task.metadata_json["board_tool_revision"] >= 2
        board = db.scalar(select(InstructionBoard).where(InstructionBoard.run_id == run_id))
        scene = BoardScene.model_validate(board.document)
        notes = [
            item for item in scene.items
            if getattr(item, "text", "") == "暖色背景，保留鞋面"
        ]
        assert notes and notes[0].created_by == "agent:planning_agent"
        completed = db.scalar(
            select(AgentTraceEvent).where(
                AgentTraceEvent.run_id == run_id,
                AgentTraceEvent.event_type == "board_tools_completed",
            )
        )
        assert completed is not None
        assert completed.stage_name == "planning"


def test_board_pass_failure_does_not_fail_the_stage(client, monkeypatch):
    from app.agents.runtime import AgentsRuntime
    from app.data.models import AgentTraceEvent

    def stub_chat(self, provider, model, prompt, runtime_config, **kwargs):
        if "BOARD TOOLS" in prompt:
            raise RuntimeError("board model down")
        return "stub summary", model, 0.0

    monkeypatch.setattr(AgentsRuntime, "_chat_complete", stub_chat)
    run_id = _planning_run(client, "PASS-10")
    run_worker_once()  # planning + failing pass

    with SessionLocal() as db:
        task = task_for(db, run_id, "planning")
        assert task.status == "waiting_review"
        board = db.scalar(select(InstructionBoard).where(InstructionBoard.run_id == run_id))
        assert board.revision == 1
        failed = db.scalar(
            select(AgentTraceEvent).where(
                AgentTraceEvent.run_id == run_id,
                AgentTraceEvent.event_type == "board_tools_failed",
            )
        )
        assert failed is not None
