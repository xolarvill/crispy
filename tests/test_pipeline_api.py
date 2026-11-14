from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.data.models import VariantAsset
from app.data.session import SessionLocal
from app.providers.llm import GeneratedImage, GeneratedVideo, ImageGenResult, VideoGenResult
from app.orchestrator.state_machine import STAGE_ORDER, stage_plan_for
from app.services.runs import RETRYABLE_FAILURES, _build_task_input, _classify_failure, _generated_asset_failure, _qa_repair_context, _retry_delay, _submit_next_video_segment, execute_next_queued_stage


def _run_worker_once() -> None:
    with SessionLocal() as db:
        execute_next_queued_stage(db)
        db.commit()


def _patch_valid_generated_images(monkeypatch):
    def fake_materialize_generated_image(_selected):
        from PIL import Image

        image = Image.new("RGB", (200, 200))
        for x in range(200):
            for y in range(200):
                image.putpixel((x, y), ((x * 3) % 255, (y * 5) % 255, ((x + y) * 2) % 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), "b64_json"

    monkeypatch.setattr("app.services.runs.runtime._materialize_generated_image", fake_materialize_generated_image)


def _valid_png_b64() -> str:
    from PIL import Image

    image = Image.new("RGB", (200, 200))
    for x in range(200):
        for y in range(200):
            image.putpixel((x, y), ((x * 3) % 255, (y * 5) % 255, ((x + y) * 2) % 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_retryable_stage_failure_waits_in_queue(client, monkeypatch):
    def fail_intake(*_args, **_kwargs):
        raise TimeoutError("provider read timeout")

    monkeypatch.setattr("app.services.runs.runtime.run_intake", fail_intake)

    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-retry",
            "project_name": "p-retry",
            "product_name": "retry harness",
            "product_code": "RETRY-001",
            "industry_code": "pet_care",
            "campaign_name": "retry campaign",
            "channel": "meta",
            "creative_preset": "meta_square_5s",
            "business_context": {"target_audience": "dog owners"},
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["id"]

    _run_worker_once()

    run = client.get(f"/runs/{run_id}").json()
    task = next(item for item in run["stage_tasks"] if item["stage_name"] == "intake")
    assert run["status"] == "running"
    assert task["status"] == "queued"
    assert task["metadata_json"]["waiting_state"]["retry_delay_seconds"] == 180.0
    assert task["metadata_json"]["waiting_state"]["next_attempt"] == 2
    with SessionLocal() as db:
        from app.data.models import StageTask

        row = db.query(StageTask).filter_by(run_id=run_id, stage_name="intake").one()
        assert row.failure_category == "timeout"
        assert row.retry_at is not None
        assert row.max_retries == 4
    assert _retry_delay(1) == 180.0
    assert _retry_delay(2) == 300.0
    assert _retry_delay(3) == 480.0
    assert _retry_delay(99) == 480.0


def test_malformed_model_json_is_retryable_schema_error():
    try:
        json.loads('{"variants": [')
    except json.JSONDecodeError as exc:
        assert _classify_failure(exc) == "schema_error"

    assert _classify_failure(ValueError("Evaluation response missing total_score for V1")) == "schema_error"
    assert "schema_error" in RETRYABLE_FAILURES


def test_qa_repair_context_maps_visual_qa_flags_to_actions():
    class EmptyRows:
        def all(self):
            return []

    class FakeDb:
        def scalars(self, *_args, **_kwargs):
            return EmptyRows()

    variant = SimpleNamespace(
        id="variant-row",
        metadata_json={
            "visual_quality": {
                "blocking_issues": ["visual_qa_product_truth_color_mismatch"],
                "asset_reports": [
                    {
                        "flags": ["visual_qa_placeholder"],
                        "image_asset_contract": {"flags": ["product_fill_low"]},
                    }
                ],
            }
        },
    )

    repair = _qa_repair_context(
        db=FakeDb(),
        variant=variant,
        reason="try again",
        target_stage="copy_image_generation",
    )

    assert repair["actions"] == [
        "Produce a real usable visual asset; do not return a placeholder, blank image, or error artifact.",
        "Restore the required product colors from the product truth contract.",
        "Make the product larger and clearly inspectable in frame.",
    ]
    assert "Regeneration repair instructions" in repair["prompt"]


@pytest.mark.parametrize("repair_note", ["", "Move the handle clear of the working edge."])
def test_qa_repair_context_maps_visual_proof_failure_to_action(repair_note):
    class EmptyRows:
        def all(self):
            return []

    class FakeDb:
        def scalars(self, *_args, **_kwargs):
            return EmptyRows()

    variant = SimpleNamespace(
        id="variant-row",
        metadata_json={
            "visual_quality": {
                "visual_proof_qa": {"status": "fail", "repair_instruction": repair_note},
                "blocking_issues": [
                    "visual_qa_visual_proof_failed",
                    "visual_proof_failed:image communicates active pulling instead of controlled anti-pull redirection",
                ],
                "asset_reports": [],
            }
        },
    )

    repair = _qa_repair_context(
        db=FakeDb(),
        variant=variant,
        reason="fix visual concept",
        target_stage="copy_image_generation",
    )

    assert any("Regenerate with the visual proof mechanism clearly shown" in action for action in repair["actions"])
    if repair_note:
        assert repair["actions"][0] == repair_note
        assert repair_note in repair["prompt"]
    assert any("Avoid semantic failure" in action for action in repair["actions"])
    assert "visual_qa_visual_proof_failed" in repair["evidence_flags"]


def test_channel_is_included_in_agent_task_input(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "channel_w",
            "project_name": "channel_p",
            "product_name": "pet wipes",
            "product_code": "CH-001",
            "industry_code": "pet_care",
            "campaign_name": "tiktok-launch",
            "channel": "tiktok",
            "creative_preset": "meta_square_5s",
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["id"]

    with SessionLocal() as db:
        from app.data.models import PipelineRun, StageTask

        run = db.get(PipelineRun, run_id)
        task = db.query(StageTask).filter_by(run_id=run_id, stage_name="planning").one()
        assert _build_task_input(db, run, task)["channel"] == "tiktok"


def test_run_status_explanation_guides_operator_next_steps(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "status_w",
            "project_name": "status_p",
            "product_name": "pet wipes",
            "product_code": "STATUS-001",
            "industry_code": "pet_care",
            "campaign_name": "status-campaign",
            "creative_preset": "meta_square_5s",
            "pipeline_mode": "copy_image_only",
        },
    )
    assert create_resp.status_code == 200
    created = create_resp.json()
    run_id = created["id"]

    assert created["status_explanation"]["tone"] == "info"
    assert created["status_explanation"]["headline"] == "Queued for intake"
    assert created["status_explanation"]["primary_action"] == "Wait for worker"

    _run_worker_once()
    waiting = client.get(f"/runs/{run_id}").json()
    assert waiting["status"] == "waiting_review"
    assert waiting["status_explanation"]["tone"] == "review"
    assert waiting["status_explanation"]["headline"] == "Intake is waiting for review"
    assert waiting["status_explanation"]["primary_action"] == "Review and approve"
    assert "Approve to continue" in waiting["status_explanation"]["next_actions"]

    with SessionLocal() as db:
        from app.data.models import PipelineRun, RunStatus, StageTask, TaskStatus

        run = db.get(PipelineRun, run_id)
        task = db.query(StageTask).filter_by(run_id=run_id, stage_name="intake").one()
        run.status = RunStatus.FAILED.value
        task.status = TaskStatus.FAILED.value
        task.error_message = "provider timeout"
        db.commit()

    failed = client.get(f"/runs/{run_id}").json()
    assert failed["status_explanation"]["tone"] == "danger"
    assert failed["status_explanation"]["headline"] == "Intake failed"
    assert failed["status_explanation"]["detail"] == "provider timeout"
    assert failed["status_explanation"]["primary_action"] == "Reject to retry"


def test_pipeline_run_can_progress_with_human_gates(client, monkeypatch):
    _patch_valid_generated_images(monkeypatch)
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w1",
            "project_name": "p1",
            "product_name": "pet wipes",
            "product_code": "PW-001",
            "industry_code": "pet_care",
            "campaign_name": "meta-us-1",
            "creative_preset": "meta_square_5s",
            "context": {"positioning": "premium convenience"},
            "business_context": {
                "target_audience": "busy pet owners",
                "key_value_props": ["save time", "reduce odor"],
                "primary_cta": "Shop Now",
                "campaign_objective": "conversions",
            },
            "category_tags": ["pet_care"],
        },
    )
    assert create_resp.status_code == 200
    run = create_resp.json()
    run_id = run["id"]
    assert run["model_provider"] == "openai"
    assert run["model_name"] == "gpt-4.1"
    assert run["pipeline_mode"] == "full_multimodal"
    assert run["enable_research"] is False
    assert run["variant_summary"]["total"] == 0

    for stage in STAGE_ORDER:
        _run_worker_once()
        run = client.get(f"/runs/{run_id}").json()
        assert run["current_stage"] == stage
        assert run["status"] == "waiting_review"
        current_task = [t for t in run["stage_tasks"] if t["stage_name"] == stage][0]
        assert current_task["metadata_json"]["stage_contract_version"] == "commercial-pilot-v2"
        assert current_task["metadata_json"]["stage_contract"]["stage_name"] == stage
        assert current_task["metadata_json"]["stage_contract"]["runtime_handler"].startswith("run_")
        assert current_task["metadata_json"]["stage_contract"]["produces"]
        assert current_task["metadata_json"]["persona_snapshots"]
        stage_events = [event for event in run["trace_events"] if event["stage_name"] == stage]
        assert any(event["event_type"] == "started" for event in stage_events)
        started_event = next(event for event in stage_events if event["event_type"] == "started")
        assert started_event["payload"]["stage_contract"]["stage_name"] == stage
        assert any(event["event_type"] == "input_summary" for event in stage_events)
        assert any(event["event_type"] == "handoff" for event in stage_events)
        assert any(event["event_type"] == "completed" for event in stage_events)
        if stage in {"copy_image_generation", "storyboard_image_generation", "video_generation"}:
            provider_events = [event for event in stage_events if event["event_type"] == "provider_selection"]
            assert provider_events
            assert all(event["payload"]["decision_type"] == "generation_provider_selection" for event in provider_events)
            assert all(event["provider_name"] == event["payload"]["selected_provider_name"] for event in provider_events)

        if stage == "divergence":
            assert run["variant_summary"]["total"] == run["variant_count"]
            rej = client.post(f"/runs/{run_id}/reject", json={"notes": "need stronger variant split"})
            assert rej.status_code == 200
            _run_worker_once()
            run = client.get(f"/runs/{run_id}").json()
            divergence_task = [t for t in run["stage_tasks"] if t["stage_name"] == "divergence"][0]
            assert divergence_task["attempt"] >= 2
            assert run["status"] == "waiting_review"
            assert any(event["event_type"] == "human_rejected" for event in run["trace_events"])

        if stage != STAGE_ORDER[-1]:
            adv = client.post(f"/runs/{run_id}/advance", json={"notes": "approved"})
            assert adv.status_code == 200

    run = client.get(f"/runs/{run_id}").json()
    assert run["latest_scorecard"] is not None
    assert run["latest_forecast"] is not None

    done = client.post(f"/runs/{run_id}/advance", json={"notes": "final approve"})
    assert done.status_code == 200
    run = done.json()
    assert run["status"] == "completed"
    assert run["current_stage"] is None
    assert any(event["event_type"] == "human_approved" for event in run["trace_events"])
    events_resp = client.get(f"/runs/{run_id}/events", params={"once": True})
    assert events_resp.status_code == 200
    assert "event: agent_trace" in events_resp.text
    assert "data:" in events_resp.text


def test_stage_rerun_requeues_target_and_resets_downstream(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "rerun_w",
            "project_name": "rerun_p",
            "product_name": "pet wipes",
            "product_code": "RERUN-001",
            "industry_code": "pet_care",
            "campaign_name": "rerun-campaign",
            "channel": "meta",
            "creative_preset": "meta_square_5s",
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["id"]

    for stage in ["intake", "planning", "divergence"]:
        _run_worker_once()
        if stage != "divergence":
            ok = client.post(f"/runs/{run_id}/advance", json={"notes": f"approve {stage}"})
            assert ok.status_code == 200

    rerun = client.post(f"/runs/{run_id}/stages/planning/rerun", json={"notes": "quality below bar"})
    assert rerun.status_code == 200
    payload = rerun.json()
    assert payload["status"] == "running"
    assert payload["current_stage"] == "planning"
    tasks = {task["stage_name"]: task for task in payload["stage_tasks"]}
    assert tasks["planning"]["status"] == "queued"
    assert tasks["planning"]["output_payload"] == {}
    assert tasks["planning"]["metadata_json"]["stage_rerun_requested"] is True
    assert tasks["divergence"]["status"] == "draft"
    assert tasks["divergence"]["output_payload"] == {}
    assert any(event["event_type"] == "stage_rerun_requested" for event in payload["trace_events"])

    _run_worker_once()
    rerun_after_worker = client.get(f"/runs/{run_id}").json()
    planning = next(task for task in rerun_after_worker["stage_tasks"] if task["stage_name"] == "planning")
    assert planning["status"] == "waiting_review"
    assert planning["attempt"] >= 2


def test_run_deliverables_and_variants_endpoints(client, monkeypatch):
    _patch_valid_generated_images(monkeypatch)
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w2",
            "project_name": "p2",
            "product_name": "pet brush",
            "product_code": "PB-001",
            "industry_code": "pet_care",
            "campaign_name": "meta-us-2",
            "creative_preset": "meta_square_5s",
            "business_context": {"target_audience": "cat owners", "primary_cta": "Shop Now", "campaign_objective": "conversions"},
        },
    )
    run_id = create_resp.json()["id"]
    for stage in STAGE_ORDER:
        _run_worker_once()
        if stage != STAGE_ORDER[-1]:
            client.post(f"/runs/{run_id}/advance", json={"notes": "ok"})

    variants = client.get(f"/runs/{run_id}/variants")
    assert variants.status_code == 200
    variants_payload = variants.json()
    assert len(variants_payload["variants"]) > 0
    assert len(variants_payload["ranked"]) > 0
    assert len(variants_payload["items"]) == 8
    assert variants_payload["summary"]["winner_count"] == 1
    assert variants_payload["summary"]["filtered_count"] == 8
    assert variants_payload["summary"]["quality_flag_counts"]["winner"] == 1
    assert variants_payload["items"][0]["assets"]
    assert variants_payload["items"][0]["scores"]
    assert variants_payload["items"][0]["quality_summary"]["required_asset_types"]
    image_asset = next(asset for item in variants_payload["items"] for asset in item["assets"] if asset["asset_type"] == "image")
    assert "visual_qa" in image_asset["payload"]
    assert image_asset["payload"]["visual_qa"]["status"] in {"pass", "warn", "fail"}

    winner_variants = client.get(f"/runs/{run_id}/variants", params={"quality": "winner"})
    assert winner_variants.status_code == 200
    winner_payload = winner_variants.json()
    assert winner_payload["summary"]["filtered_count"] == 1
    assert len(winner_payload["items"]) == 1
    assert winner_payload["items"][0]["is_winner"] is True

    high_score_variants = client.get(f"/runs/{run_id}/variants", params={"min_score": 1})
    assert high_score_variants.status_code == 200
    assert high_score_variants.json()["items"]

    deliverables = client.get(f"/runs/{run_id}/deliverables")
    assert deliverables.status_code == 200
    deliverables_payload = deliverables.json()
    assert deliverables_payload["winner_variant_id"] is not None
    assert "copy_variant" in deliverables_payload["deliverables"]
    run_payload = client.get(f"/runs/{run_id}").json()
    planning_task = next(task for task in run_payload["stage_tasks"] if task["stage_name"] == "planning")
    divergence_task = next(task for task in run_payload["stage_tasks"] if task["stage_name"] == "divergence")
    visual_qa_task = next(task for task in run_payload["stage_tasks"] if task["stage_name"] == "visual_quality_assessment")
    evaluation_task = next(task for task in run_payload["stage_tasks"] if task["stage_name"] == "evaluation_selection")
    assert planning_task["output_payload"]["strategy_handoff"]["handoff_standard"] == "commercial-pilot-v2"
    assert divergence_task["output_payload"]["experiment_matrix"]
    assert visual_qa_task["metadata_json"]["agent_name"] == "visual_qa_agent"
    assert visual_qa_task["metadata_json"]["resolved_api"]["provider_name"] == "deepseek"
    assert visual_qa_task["output_payload"]["variant_summaries"]
    assert visual_qa_task["output_payload"]["model_media_inputs"]["image_count"] > 0
    assert any(score["score_type"] == "visual_quality" for score in variants_payload["items"][0]["scores"])
    ranked_first = evaluation_task["output_payload"]["evaluation_result"]["ranked_variants"][0]
    assert "visual_qa" in ranked_first["sub_scores"]
    assert "compliance_block" in ranked_first


def test_run_deliverables_excludes_failed_winner_media(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-deliverables-filter",
            "project_name": "p-deliverables-filter",
            "product_name": "pet brush",
            "product_code": "PB-FILTER",
            "industry_code": "pet_care",
            "campaign_name": "meta-us-filter",
            "creative_preset": "meta_square_5s",
        },
    )
    run_id = create_resp.json()["id"]
    ok_image = Path(f"assets/{run_id}/V1_ok.png")
    ok_image.parent.mkdir(parents=True, exist_ok=True)
    ok_image.write_bytes(b"x" * 2048)

    with SessionLocal() as db:
        from app.data.models import RunVariant

        variant = RunVariant(run_id=run_id, variant_id="V1", angle="A", hook="H", message="M", is_winner=True)
        db.add(variant)
        db.flush()
        db.add_all(
            [
                VariantAsset(
                    run_variant_id=variant.id,
                    run_id=run_id,
                    stage_name="copy_image_generation",
                    asset_type="image",
                    uri=f"assets/{run_id}/V1_generation_error.txt",
                    payload={"variant_id": "V1", "uri": f"assets/{run_id}/V1_generation_error.txt", "source": "generation_error"},
                    idempotency_key="failed-winner-image",
                ),
                VariantAsset(
                    run_variant_id=variant.id,
                    run_id=run_id,
                    stage_name="copy_image_generation",
                    asset_type="image",
                    uri=f"assets/{run_id}/V1_ok.png",
                    payload={"variant_id": "V1", "uri": f"assets/{run_id}/V1_ok.png", "source": "generated"},
                    idempotency_key="valid-winner-image",
                ),
            ]
        )
        db.commit()

    payload = client.get(f"/runs/{run_id}/deliverables").json()["deliverables"]
    assert [item["uri"] for item in payload["image_assets"]] == [f"assets/{run_id}/V1_ok.png"]
    assert _generated_asset_failure({"source": "generation_error"}, "assets/x_generation_error.txt")[0] == "provider_error"


def test_variant_review_endpoints_update_variant_library(client, monkeypatch):
    _patch_valid_generated_images(monkeypatch)

    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-variant-review",
            "project_name": "p-variant-review",
            "product_name": "pet brush",
            "product_code": "VR-001",
            "industry_code": "pet_care",
            "campaign_name": "meta-us-review",
            "creative_preset": "meta_square_5s",
        },
    )
    run_id = create_resp.json()["id"]
    for stage in STAGE_ORDER:
        _run_worker_once()
        if stage != STAGE_ORDER[-1]:
            client.post(f"/runs/{run_id}/advance", json={"notes": "ok"})

    variants_payload = client.get(f"/runs/{run_id}/variants").json()
    target = variants_payload["items"][1]["variant_id"]
    before_target = next(item for item in variants_payload["items"] if item["variant_id"] == target)
    before_asset_count = len(before_target["assets"])

    shortlist = client.post(
        f"/runs/{run_id}/variants/{target}/select",
        json={"shortlist": True, "comment": "keep for review"},
    )
    assert shortlist.status_code == 200
    assert shortlist.json()["shortlisted"] is True

    regen = client.post(
        f"/runs/{run_id}/variants/{target}/regenerate",
        json={"reason": "need a different hook", "target_stage": "copy_image_generation"},
    )
    assert regen.status_code == 200
    assert regen.json()["regenerate_requested"] is False
    assert regen.json()["review_status"] == "regenerated"
    assert len(regen.json()["assets"]) > before_asset_count
    assert regen.json()["metadata_json"]["latest_regeneration"]["target_stage"] == "copy_image_generation"
    run_after_regen = client.get(f"/runs/{run_id}").json()
    assert any(event["event_type"] == "regeneration_started" for event in run_after_regen["trace_events"])
    assert any(event["event_type"] == "regeneration_completed" for event in run_after_regen["trace_events"])

    winner = client.post(
        f"/runs/{run_id}/variants/{target}/select",
        json={"winner": True, "comment": "manual winner"},
    )
    assert winner.status_code == 200
    assert winner.json()["is_winner"] is True

    review = client.post(
        f"/runs/{run_id}/variants/{target}/review",
        json={"action": "approve_variant", "comment": "approved by operator"},
    )
    assert review.status_code == 200
    assert review.json()["review_status"] == "approved"


def test_pipeline_mode_copy_image_only(client, monkeypatch):
    _patch_valid_generated_images(monkeypatch)

    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w3",
            "project_name": "p3",
            "product_name": "dog leash",
            "product_code": "DL-001",
            "industry_code": "pet_accessories",
            "campaign_name": "meta-copy-image-1",
            "pipeline_mode": "copy_image_only",
            "creative_preset": "meta_square_5s",
            "business_context": {"target_audience": "dog owners", "key_value_props": ["anti-pull"], "primary_cta": "Shop Now"},
        },
    )
    assert create_resp.status_code == 200
    run = create_resp.json()
    run_id = run["id"]
    plan = stage_plan_for("copy_image_only")
    assert run["pipeline_mode"] == "copy_image_only"
    assert [task["stage_name"] for task in run["stage_tasks"]] == plan

    for stage in plan:
        _run_worker_once()
        current = client.get(f"/runs/{run_id}").json()
        assert current["current_stage"] == stage
        assert current["status"] == "waiting_review"
        if stage != plan[-1]:
            client.post(f"/runs/{run_id}/advance", json={"notes": "ok"})

    final_review = client.post(f"/runs/{run_id}/advance", json={"notes": "final"})
    assert final_review.status_code == 200
    done = final_review.json()
    assert done["status"] == "completed"
    assert done["current_stage"] is None

    deliverables = client.get(f"/runs/{run_id}/deliverables")
    assert deliverables.status_code == 200
    payload = deliverables.json()["deliverables"]
    assert payload["copy_variant"] is not None
    assert payload["video_asset"] is None
    image_uri = payload["image_assets"][0]["uri"]
    image_path = Path(image_uri)
    assert image_path.exists()
    assert image_path.stat().st_size > 0


def test_pipeline_modes_endpoint(client):
    resp = client.get("/pipeline-modes")
    assert resp.status_code == 200
    modes = {item["mode"]: item for item in resp.json()}
    assert "copy_image_only" in modes
    assert "dtc_site_image" in modes
    assert "video_only" in modes
    assert "full_multimodal" in modes
    assert "marketplace_main_image" in modes
    assert "tiktok_shop_video" in modes
    assert modes["copy_image_only"]["agent_count"] >= 1
    assert modes["dtc_site_image"]["display_name"] == "DTC Site Image"
    assert modes["dtc_site_image"]["stages"] == modes["copy_image_only"]["stages"]
    assert modes["marketplace_main_image"]["display_name"] == "Studio Main Image"
    assert modes["marketplace_main_image"]["stages"] == modes["copy_image_only"]["stages"]
    assert modes["tiktok_shop_video"]["display_name"] == "TikTok Shop Video"
    assert modes["tiktok_shop_video"]["stages"] == stage_plan_for("video_only")


def test_create_run_requires_product_and_industry_and_preset(client):
    resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-required",
            "project_name": "p-required",
            "product_name": "missing fields",
            "campaign_name": "meta-required",
        },
    )
    assert resp.status_code == 422


def test_product_code_conflict_returns_409(client):
    first = client.post(
        "/runs",
        json={
            "workspace_name": "w-code",
            "project_name": "p-code",
            "product_name": "dog leash",
            "product_code": "DL-CODE-001",
            "industry_code": "pet_accessories",
            "campaign_name": "meta-code-1",
            "creative_preset": "meta_square_5s",
        },
    )
    assert first.status_code == 200

    second = client.post(
        "/runs",
        json={
            "workspace_name": "w-code",
            "project_name": "p-code",
            "product_name": "cat leash different name",
            "product_code": "DL-CODE-001",
            "industry_code": "pet_accessories",
            "campaign_name": "meta-code-2",
            "creative_preset": "meta_square_5s",
        },
    )
    assert second.status_code == 409
    assert "product_code conflict" in second.text


def test_product_name_conflict_returns_409(client):
    first = client.post(
        "/runs",
        json={
            "workspace_name": "w-name",
            "project_name": "p-name",
            "product_name": "dog leash",
            "product_code": "DL-NAME-001",
            "industry_code": "pet_accessories",
            "campaign_name": "meta-name-1",
            "creative_preset": "meta_square_5s",
        },
    )
    assert first.status_code == 200

    second = client.post(
        "/runs",
        json={
            "workspace_name": "w-name",
            "project_name": "p-name",
            "product_name": "dog leash",
            "product_code": "DL-NAME-002",
            "industry_code": "pet_accessories",
            "campaign_name": "meta-name-2",
            "creative_preset": "meta_square_5s",
        },
    )
    assert second.status_code == 409
    assert "product name conflict" in second.text
    assert "DL-NAME-001" in second.text


def test_custom_copy_image_only_does_not_require_video_specs(client):
    resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-custom-image",
            "project_name": "p-custom-image",
            "product_name": "dog leash",
            "product_code": "DL-CUSTOM-IMAGE-001",
            "industry_code": "pet_accessories",
            "campaign_name": "meta-custom-image-1",
            "creative_preset": "custom",
            "creative_specs": {"image_size": "1:1", "resolution": "720p"},
            "pipeline_mode": "copy_image_only",
        },
    )
    assert resp.status_code == 200
    creative_specs = resp.json()["creative_specs"]
    assert creative_specs["image_size"] == "1:1"
    assert creative_specs["resolution"] == "720p"
    assert creative_specs["video_size"] == "9:16"
    assert creative_specs["video_duration_seconds"] == 5


def test_creative_preset_is_materialized_into_run(client, monkeypatch):
    _patch_valid_generated_images(monkeypatch)
    resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-preset",
            "project_name": "p-preset",
            "product_name": "dog leash",
            "product_code": "DL-PRESET-001",
            "industry_code": "pet_accessories",
            "campaign_name": "meta-preset-1",
            "creative_preset": "meta_vertical_5s",
            "pipeline_mode": "copy_image_only",
        },
    )
    assert resp.status_code == 200
    run = resp.json()
    assert run["creative_preset"] == "meta_vertical_5s"
    assert run["creative_specs"]["image_size"] == "9:16"
    assert run["creative_specs"]["video_duration_seconds"] == 5

    for stage in stage_plan_for("copy_image_only"):
        _run_worker_once()
        current = client.get(f"/runs/{run['id']}").json()
        if stage == "copy_image_generation":
            copy_task = [t for t in current["stage_tasks"] if t["stage_name"] == "copy_image_generation"][0]
            assets = copy_task["output_payload"]["image_assets"]
            assert len(assets) > 0
            assert assets[0]["aspect_ratio"] == "9:16"
            break
        client.post(f"/runs/{run['id']}/advance", json={"notes": "ok"})


def test_pipeline_mode_tiktok_shop_video_materializes_defaults(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-tiktok",
            "project_name": "p-tiktok",
            "product_name": "pet grooming glove",
            "product_code": "TT-001",
            "industry_code": "pet_care",
            "campaign_name": "tiktok-shop-video",
            "pipeline_mode": "tiktok_shop_video",
            "creative_preset": "custom",
            "creative_specs": {
                "image_size": "9:16",
                "video_size": "9:16",
                "resolution": "720p",
                "video_duration_seconds": 12,
                "tiktok_video_style": "direct_response_ad",
            },
            "enable_research": True,
        },
    )
    assert create_resp.status_code == 200
    run = create_resp.json()
    assert run["pipeline_mode"] == "tiktok_shop_video"
    assert run["creative_preset"] == "tiktok_shop_conversion_12s"
    assert run["creative_specs"]["platform"] == "tiktok"
    assert run["creative_specs"]["creative_goal"] == "shop_conversion_video"
    assert run["creative_specs"]["tiktok_video_style"] == "direct_response_ad"
    assert run["creative_specs"]["platform_targets"] == ["tiktok", "tiktok_shop"]
    assert run["enable_research"] is False
    assert [task["stage_name"] for task in run["stage_tasks"]] == stage_plan_for("tiktok_shop_video")


def test_pipeline_mode_dtc_site_image_materializes_defaults(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-dtc",
            "project_name": "p-dtc",
            "product_name": "pet carrier",
            "product_code": "DTC-001",
            "industry_code": "pet_accessories",
            "campaign_name": "dtc-site-image",
            "pipeline_mode": "dtc_site_image",
            "creative_preset": "custom",
            "creative_specs": {
                "image_size": "4:5",
                "resolution": "1600px",
            },
            "enable_research": True,
        },
    )
    assert create_resp.status_code == 200
    run = create_resp.json()
    assert run["pipeline_mode"] == "dtc_site_image"
    assert run["creative_preset"] == "dtc_site_image_pack"
    assert run["creative_specs"]["asset_goal"] == "dtc_site_image"
    assert run["creative_specs"]["image_size"] == "4:5"
    assert run["creative_specs"]["resolution"] == "1600px"
    assert run["creative_specs"]["site_surface"] == "pdp_primary"
    assert run["creative_specs"]["platform_targets"] == ["shopify"]
    assert run["enable_research"] is True
    assert [task["stage_name"] for task in run["stage_tasks"]] == stage_plan_for("dtc_site_image")


def test_pipeline_mode_dtc_site_image_accepts_site_surface_override(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-dtc-hero",
            "project_name": "p-dtc-hero",
            "product_name": "pet carrier",
            "product_code": "DTC-002",
            "industry_code": "pet_accessories",
            "campaign_name": "dtc-site-hero",
            "pipeline_mode": "dtc_site_image",
            "creative_preset": "custom",
            "creative_specs": {
                "image_size": "4:5",
                "resolution": "1600px",
                "site_surface": "homepage_hero",
            },
        },
    )
    assert create_resp.status_code == 200
    run = create_resp.json()
    assert run["creative_specs"]["site_surface"] == "homepage_hero"


def test_dtc_site_surface_changes_planning_and_image_prompt(client, monkeypatch):
    _patch_valid_generated_images(monkeypatch)
    def create_run_for_surface(surface: str, code: str) -> dict:
        resp = client.post(
            "/runs",
            json={
                "workspace_name": f"w-dtc-surface-{surface}",
                "project_name": f"p-dtc-surface-{surface}",
                "product_name": f"pet carrier {surface}",
                "product_code": code,
                "industry_code": "pet_accessories",
                "campaign_name": f"dtc-{surface}",
                "pipeline_mode": "dtc_site_image",
                "creative_preset": "custom",
                "variant_count": 1,
                "creative_specs": {
                    "image_size": "4:5",
                    "resolution": "1600px",
                    "site_surface": surface,
                },
                "business_context": {
                    "target_audience": "urban dog owners",
                    "key_value_props": ["airline-friendly form", "comfortable ventilation"],
                    "primary_cta": "Shop Now",
                },
            },
        )
        assert resp.status_code == 200
        run_id = resp.json()["id"]
        for stage in ["intake", "planning", "divergence", "copy_image_generation"]:
            _run_worker_once()
            run_payload = client.get(f"/runs/{run_id}").json()
            if stage != "copy_image_generation":
                approve = client.post(f"/runs/{run_id}/advance", json={"notes": "ok"})
                assert approve.status_code == 200
        return client.get(f"/runs/{run_id}").json()

    hero_run = create_run_for_surface("homepage_hero", "DTC-SURFACE-HERO")
    pdp_run = create_run_for_surface("pdp_primary", "DTC-SURFACE-PDP")

    hero_planning = next(task for task in hero_run["stage_tasks"] if task["stage_name"] == "planning")["output_payload"]
    pdp_planning = next(task for task in pdp_run["stage_tasks"] if task["stage_name"] == "planning")["output_payload"]
    hero_image = next(task for task in hero_run["stage_tasks"] if task["stage_name"] == "copy_image_generation")["output_payload"]["image_assets"][0]
    pdp_image = next(task for task in pdp_run["stage_tasks"] if task["stage_name"] == "copy_image_generation")["output_payload"]["image_assets"][0]

    assert hero_planning["surface_strategy"]["site_surface"] == "homepage_hero"
    assert hero_planning["surface_strategy"]["composition_focus"] == "brand_story_scene"
    assert "homepage hero" in hero_image["prompt"].lower()
    assert "headline-safe negative space" in hero_image["prompt"].lower()
    assert "social media ad image" not in hero_image["prompt"].lower()

    assert pdp_planning["surface_strategy"]["site_surface"] == "pdp_primary"
    assert pdp_planning["surface_strategy"]["composition_focus"] == "product_dominant_detail"
    assert "pdp primary" in pdp_image["prompt"].lower()
    assert "product should dominate the frame" in pdp_image["prompt"].lower()
    assert "social media ad image" not in pdp_image["prompt"].lower()


def test_dtc_site_surface_adds_surface_specific_review_hints(client, monkeypatch):
    _patch_valid_generated_images(monkeypatch)
    def create_run_to_visual_qa(surface: str, code: str) -> tuple[dict, dict]:
        resp = client.post(
            "/runs",
            json={
                "workspace_name": f"w-dtc-review-{surface}",
                "project_name": f"p-dtc-review-{surface}",
                "product_name": f"pet carrier review {surface}",
                "product_code": code,
                "industry_code": "pet_accessories",
                "campaign_name": f"dtc-review-{surface}",
                "pipeline_mode": "dtc_site_image",
                "creative_preset": "custom",
                "variant_count": 1,
                "creative_specs": {
                    "image_size": "4:5",
                    "resolution": "1600px",
                    "site_surface": surface,
                },
            },
        )
        assert resp.status_code == 200
        run_id = resp.json()["id"]
        for stage in ["intake", "planning", "divergence", "copy_image_generation", "visual_quality_assessment"]:
            _run_worker_once()
            if stage != "visual_quality_assessment":
                approve = client.post(f"/runs/{run_id}/advance", json={"notes": "ok"})
                assert approve.status_code == 200
        run_payload = client.get(f"/runs/{run_id}").json()
        variants_payload = client.get(f"/runs/{run_id}/variants").json()
        return run_payload, variants_payload

    hero_run, hero_variants = create_run_to_visual_qa("homepage_hero", "DTC-REVIEW-HERO")
    pdp_run, pdp_variants = create_run_to_visual_qa("pdp_primary", "DTC-REVIEW-PDP")

    hero_summary = next(task for task in hero_run["stage_tasks"] if task["stage_name"] == "visual_quality_assessment")["output_payload"]["variant_summaries"][0]
    pdp_summary = next(task for task in pdp_run["stage_tasks"] if task["stage_name"] == "visual_quality_assessment")["output_payload"]["variant_summaries"][0]
    hero_quality = hero_variants["items"][0]["quality_summary"]
    pdp_quality = pdp_variants["items"][0]["quality_summary"]

    assert any("headline-safe space" in hint.lower() for hint in hero_summary["review_hints"])
    assert any("brand atmosphere" in hint.lower() for hint in hero_summary["review_hints"])
    assert any("headline-safe space" in hint.lower() for hint in hero_quality["review_hints"])

    assert any("product occupies enough of the frame" in hint.lower() for hint in pdp_summary["review_hints"])
    assert any("material or structure details" in hint.lower() for hint in pdp_summary["review_hints"])
    assert any("product occupies enough of the frame" in hint.lower() for hint in pdp_quality["review_hints"])


def test_dtc_site_image_rejects_unknown_site_surface(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-dtc-invalid-surface",
            "project_name": "p-dtc-invalid-surface",
            "product_name": "pet carrier invalid surface",
            "product_code": "DTC-INVALID-SURFACE",
            "industry_code": "pet_accessories",
            "campaign_name": "dtc-invalid-surface",
            "pipeline_mode": "dtc_site_image",
            "creative_preset": "custom",
            "creative_specs": {
                "image_size": "4:5",
                "resolution": "1600px",
                "site_surface": "story_card",
            },
        },
    )
    assert create_resp.status_code == 400
    assert "creative_specs.site_surface" in create_resp.text


def test_runs_preflight_reports_video_generation_incompatibility(client):
    patch_resp = client.patch(
        "/agent-configs/video_generation_agent",
        json={
            "video_provider_name": "deepseek",
            "video_model_name": "deepseek-v3.2",
            "video_api_base_url": "https://api.deepseek.com/v1/chat/completions",
        },
    )
    assert patch_resp.status_code == 200

    resp = client.post(
        "/runs/preflight",
        json={
            "pipeline_mode": "video_only",
            "has_image_inputs": False,
            "has_video_inputs": False,
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is False
    assert payload["severity"] == "error"
    keys = [row["key"] for row in payload["checks"]]
    assert "video_generation.video_generation" in keys


def test_tiktok_shop_preflight_rejects_invalid_style(client):
    resp = client.post(
        "/runs/preflight",
        json={
            "pipeline_mode": "tiktok_shop_video",
            "has_image_inputs": True,
            "has_video_inputs": False,
            "creative_specs": {
                "video_size": "9:16",
                "video_duration_seconds": 12,
                "tiktok_video_style": "viral_dance",
            },
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is False
    assert payload["severity"] == "error"
    assert "tiktok_shop_video.style" in [row["key"] for row in payload["checks"]]


def test_tiktok_shop_video_scripting_outputs_tiktok_payload(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-tiktok-script",
            "project_name": "p-tiktok-script",
            "product_name": "travel toiletry bag",
            "product_code": "TT-SCRIPT-001",
            "industry_code": "travel",
            "campaign_name": "tiktok-script",
            "pipeline_mode": "tiktok_shop_video",
            "creative_preset": "tiktok_shop_conversion_12s",
            "creative_specs": {"tiktok_video_style": "ugc_demo"},
            "business_context": {
                "target_audience": "frequent travelers",
                "key_value_props": ["keeps bottles upright", "clear compartments"],
                "primary_cta": "Shop on TikTok",
            },
            "manual_research_brief": "Show a creator packing for a weekend trip.",
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["id"]

    for stage in ["intake", "planning", "divergence", "video_scripting"]:
        _run_worker_once()
        run = client.get(f"/runs/{run_id}").json()
        if stage != "video_scripting":
            client.post(f"/runs/{run_id}/advance", json={"notes": "ok"})

    script_task = next(task for task in run["stage_tasks"] if task["stage_name"] == "video_scripting")
    first_script = script_task["output_payload"]["scripts"][0]
    assert first_script["tiktok"]["style"] == "ugc_demo"
    assert first_script["tiktok"]["opening_hook"]
    assert first_script["tiktok"]["on_screen_text"]
    assert first_script["tiktok"]["voiceover_lines"]
    assert first_script["tiktok"]["shot_timing"][0]["intent"] == "thumb_stop"
    assert first_script["tiktok"]["cta"] == "Shop on TikTok"


def test_tiktok_shop_evaluation_includes_tiktok_scores(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-tiktok-eval",
            "project_name": "p-tiktok-eval",
            "product_name": "desk cable clips",
            "product_code": "TT-EVAL-001",
            "industry_code": "office",
            "campaign_name": "tiktok-eval",
            "pipeline_mode": "tiktok_shop_video",
            "creative_preset": "tiktok_shop_conversion_12s",
            "creative_specs": {"tiktok_video_style": "direct_response_ad"},
            "business_context": {
                "target_audience": "home office workers",
                "key_value_props": ["clean desk setup"],
                "primary_cta": "Shop Now",
            },
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["id"]
    for stage in stage_plan_for("tiktok_shop_video"):
        _run_worker_once()
        run = client.get(f"/runs/{run_id}").json()
        if stage != "evaluation_selection":
            client.post(f"/runs/{run_id}/advance", json={"notes": "ok"})

    evaluation_task = next(task for task in run["stage_tasks"] if task["stage_name"] == "evaluation_selection")
    ranked = evaluation_task["output_payload"]["evaluation_result"]["ranked_variants"][0]
    for key in [
        "thumb_stop_power",
        "product_clarity",
        "purchase_intent",
        "native_tiktok_feel",
        "watch_through_potential",
        "claim_safety",
        "generation_feasibility",
    ]:
        assert key in ranked["sub_scores"]


def test_runs_preflight_reports_intake_video_understanding_incompatibility(client):
    patch_resp = client.patch(
        "/agent-configs/gm_orchestrator",
        json={
            "provider_name": "deepseek",
            "model_name": "deepseek-v3.2",
            "api_base_url": "https://api.deepseek.com/v1",
        },
    )
    assert patch_resp.status_code == 200

    resp = client.post(
        "/runs/preflight",
        json={
            "pipeline_mode": "copy_image_only",
            "has_image_inputs": False,
            "has_video_inputs": True,
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is False
    assert payload["severity"] == "error"
    keys = [row["key"] for row in payload["checks"]]
    assert "intake.video_understanding" in keys


def test_assets_refresh_recovers_external_video_task(client, monkeypatch):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-refresh",
            "project_name": "p-refresh",
            "product_name": "dog leash",
            "product_code": "DL-REFRESH-001",
            "industry_code": "pet_accessories",
            "campaign_name": "meta-refresh",
            "pipeline_mode": "video_only",
            "creative_preset": "meta_vertical_5s",
        },
    )
    run_id = create_resp.json()["id"]
    for stage in ["intake", "planning", "divergence"]:
        _run_worker_once()
        client.post(f"/runs/{run_id}/advance", json={"notes": "ok"})

    with SessionLocal() as db:
        variant = db.query(VariantAsset).filter(VariantAsset.run_id == run_id).first()
        assert variant is None
        from app.data.models import RunVariant

        run_variant = db.query(RunVariant).filter(RunVariant.run_id == run_id, RunVariant.variant_id == "V1").one()
        db.add(
            VariantAsset(
                run_variant_id=run_variant.id,
                run_id=run_id,
                stage_name="video_generation",
                asset_type="video",
                uri=f"assets/{run_id}/V1_sample.mp4",
                provider_name="stub",
                model_name="stub-video",
                prompt_summary="video asset for V1",
                idempotency_key="test-refresh-video-v1",
                payload={
                    "variant_id": "V1",
                    "video_uri": f"assets/{run_id}/V1_sample.mp4",
                    "external_task_id": "task_refresh_1",
                    "generation_status": "processing",
                    "source": "external_task_pending",
                },
            )
        )
        db.commit()

    class FakeProvider:
        def poll_video_task(self, **kwargs):
            return VideoGenResult(
                model_used="stub-video",
                task_id=kwargs["task_id"],
                status="completed",
                videos=[
                    GeneratedVideo(
                        url="https://example.com/video.mp4",
                        task_id=kwargs["task_id"],
                        status="completed",
                    )
                ],
            )

    monkeypatch.setattr("app.services.runs.runtime.providers.get", lambda _name: FakeProvider())
    monkeypatch.setattr("app.services.runs.runtime._materialize_generated_video", lambda _selected: (b"fake-video-bytes", "url"))

    resp = client.post(f"/runs/{run_id}/assets/refresh")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["refreshed"] == 1
    assert payload["completed"] == 1

    variants = client.get(f"/runs/{run_id}/variants").json()
    v1 = next(item for item in variants["items"] if item["variant_id"] == "V1")
    video = next(asset for asset in v1["assets"] if asset["asset_type"] == "video")
    assert video["payload"]["generation_status"] == "completed"
    assert video["payload"]["external_task_state"]["poll_count"] == 1
    assert video["payload"]["provider_status"] == "completed"
    assert video["payload"]["last_polled_at"]

    with SessionLocal() as db:
        from app.data.models import AgentTraceEvent

        assert db.query(AgentTraceEvent).filter_by(run_id=run_id, event_type="external_task_polled").count() == 1


def test_assets_refresh_recovers_pending_copy_image_task(client, monkeypatch):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-copy-image-refresh",
            "project_name": "p-copy-image-refresh",
            "product_name": "paper cup",
            "product_code": "COPY-IMAGE-REFRESH-001",
            "industry_code": "packaging",
            "campaign_name": "copy-image-refresh",
            "pipeline_mode": "copy_image_only",
            "approval_mode": "full_auto",
            "creative_preset": "meta_square_5s",
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["id"]
    image_uri = f"assets/{run_id}/copy_image_1.png"
    Path(image_uri).parent.mkdir(parents=True, exist_ok=True)
    Path(image_uri).write_bytes(b"")

    with SessionLocal() as db:
        from app.data.models import PipelineRun, RunStatus, RunVariant, StageTask, TaskStatus

        run = db.get(PipelineRun, run_id)
        run.current_stage = "copy_image_generation"
        run.status = RunStatus.WAITING_REVIEW.value
        run_variant = RunVariant(run_id=run_id, variant_id="V1", angle="Cup", hook="Hook", message="Message")
        db.add(run_variant)
        db.flush()
        copy_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="copy_image_generation").one()
        copy_task.status = TaskStatus.WAITING_REVIEW.value
        copy_task.output_payload = {
            "copy_variants": [{"variant_id": "V1", "headline": "Paper cup"}],
            "image_assets": [
                {
                    "variant_id": "V1",
                    "uri": image_uri,
                    "aspect_ratio": "1:1",
                    "prompt": "paper cup",
                    "source": "external_task_pending",
                    "external_task_id": "copy-image-task-1",
                    "generation_status": "submitted",
                }
            ],
        }
        db.add(
            VariantAsset(
                run_variant_id=run_variant.id,
                run_id=run_id,
                stage_name="copy_image_generation",
                asset_type="image",
                uri=image_uri,
                provider_name="stub",
                model_name="stub-image",
                prompt_summary="paper cup",
                idempotency_key="test-refresh-copy-image-v1",
                payload={
                    "variant_id": "V1",
                    "uri": image_uri,
                    "aspect_ratio": "1:1",
                    "prompt": "paper cup",
                    "source": "external_task_pending",
                    "external_task_id": "copy-image-task-1",
                    "generation_status": "submitted",
                },
            )
        )
        db.commit()

    class FakeProvider:
        def poll_image_task(self, **kwargs):
            return ImageGenResult(
                model_used=kwargs["model"],
                images=[GeneratedImage(b64_json=_valid_png_b64(), status="completed")],
                task_id=kwargs["task_id"],
                status="completed",
                raw_response={"status": "completed"},
            )

    monkeypatch.setattr("app.services.runs.runtime.providers.get", lambda _name: FakeProvider())

    resp = client.post(f"/runs/{run_id}/assets/refresh")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["copy_images"]["completed"] == 1

    with SessionLocal() as db:
        from app.data.models import AgentTraceEvent, PipelineRun, StageTask, TaskStatus

        copy_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="copy_image_generation").one()
        visual_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="visual_quality_assessment").one()
        image_asset = db.query(VariantAsset).filter_by(run_id=run_id, asset_type="image").one()
        image_payload = copy_task.output_payload["image_assets"][0]
        assert copy_task.status == TaskStatus.APPROVED.value
        assert visual_task.status == TaskStatus.QUEUED.value
        assert db.get(PipelineRun, run_id).current_stage == "visual_quality_assessment"
        assert image_payload["generation_status"] == "completed"
        assert image_payload["source"] == "b64_json"
        assert image_payload["external_task_state"]["poll_count"] == 1
        assert image_payload["provider_status"] == "completed"
        assert image_payload["last_polled_at"]
        assert db.query(AgentTraceEvent).filter_by(run_id=run_id, event_type="external_task_polled").count() == 1
        assert image_asset.failure_category is None
        assert Path(image_asset.uri).stat().st_size > 1024


def test_assets_refresh_fails_copy_image_stage_when_assets_failed(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-copy-image-refresh-failed",
            "project_name": "p-copy-image-refresh-failed",
            "product_name": "paper cup",
            "product_code": "COPY-IMAGE-REFRESH-FAILED-001",
            "industry_code": "packaging",
            "campaign_name": "copy-image-refresh-failed",
            "pipeline_mode": "copy_image_only",
            "approval_mode": "manual",
            "creative_preset": "meta_square_5s",
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["id"]

    with SessionLocal() as db:
        from app.data.models import PipelineRun, RunStatus, RunVariant, StageTask, TaskStatus

        run = db.get(PipelineRun, run_id)
        run.current_stage = "copy_image_generation"
        run.status = RunStatus.WAITING_REVIEW.value
        variants = [
            RunVariant(run_id=run_id, variant_id="V1", angle="Cup", hook="Hook", message="Message"),
            RunVariant(run_id=run_id, variant_id="V2", angle="Cup", hook="Hook", message="Message"),
        ]
        db.add_all(variants)
        db.flush()
        image_payloads = [
            {
                "variant_id": "V1",
                "uri": f"assets/{run_id}/V1_generation_error.txt",
                "source": "generation_error",
                "error": "request failed for all endpoint candidates",
            },
            {
                "variant_id": "V2",
                "uri": f"assets/{run_id}/copy_image_2.png",
                "source": "placeholder",
                "generation_status": "completed",
            },
        ]
        copy_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="copy_image_generation").one()
        copy_task.status = TaskStatus.WAITING_REVIEW.value
        copy_task.output_payload = {"copy_variants": [], "image_assets": image_payloads}
        for variant, payload in zip(variants, image_payloads, strict=True):
            db.add(
                VariantAsset(
                    run_variant_id=variant.id,
                    run_id=run_id,
                    stage_name="copy_image_generation",
                    asset_type="image",
                    uri=payload["uri"],
                    provider_name="stub",
                    model_name="stub-image",
                    prompt_summary="paper cup",
                    idempotency_key=f"failed-copy-image-{variant.variant_id}",
                    payload=payload,
                )
            )
        db.commit()

    resp = client.post(f"/runs/{run_id}/assets/refresh")
    assert resp.status_code == 200

    with SessionLocal() as db:
        from app.data.models import PipelineRun, StageTask, TaskStatus

        run = db.get(PipelineRun, run_id)
        copy_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="copy_image_generation").one()
        assert run.status == "failed"
        assert copy_task.status == TaskStatus.FAILED.value
        assert "V1: request failed for all endpoint candidates" in copy_task.error_message
        assert "V2: provider returned placeholder media" in copy_task.error_message


def test_assets_refresh_keeps_latest_retried_copy_image(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-copy-image-refresh-latest",
            "project_name": "p-copy-image-refresh-latest",
            "product_name": "paper cup",
            "product_code": "COPY-IMAGE-REFRESH-LATEST-001",
            "industry_code": "packaging",
            "campaign_name": "copy-image-refresh-latest",
            "pipeline_mode": "copy_image_only",
            "approval_mode": "manual",
            "creative_preset": "meta_square_5s",
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["id"]
    old_uri = f"assets/{run_id}/old.png"
    new_uri = f"assets/{run_id}/new.png"
    failed_uri = f"assets/{run_id}/failed.txt"
    Path(old_uri).parent.mkdir(parents=True, exist_ok=True)
    Path(old_uri).write_bytes(base64.b64decode(_valid_png_b64()))
    Path(new_uri).write_bytes(base64.b64decode(_valid_png_b64()))
    Path(failed_uri).write_text("provider returned placeholder media")

    with SessionLocal() as db:
        from app.data.models import PipelineRun, RunStatus, RunVariant, StageTask, TaskStatus

        run = db.get(PipelineRun, run_id)
        run.current_stage = "copy_image_generation"
        run.status = RunStatus.WAITING_REVIEW.value
        variant = RunVariant(run_id=run_id, variant_id="V1", angle="Cup", hook="Hook", message="Message")
        db.add(variant)
        db.flush()
        copy_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="copy_image_generation").one()
        copy_task.status = TaskStatus.WAITING_REVIEW.value
        copy_task.output_payload = {
            "copy_variants": [],
            "image_assets": [
                {
                    "variant_id": "V1",
                    "uri": old_uri,
                    "source": "url",
                    "external_task_id": "old-task",
                    "generation_status": "completed",
                }
            ],
        }
        rows = [
            (old_uri, "old-task", "old", "url", None),
            (new_uri, "new-task", "new", "url", None),
            (failed_uri, "failed-task", "failed", "placeholder", "provider returned placeholder media"),
        ]
        for uri, task_id, key, source, error in rows:
            db.add(
                VariantAsset(
                    run_variant_id=variant.id,
                    run_id=run_id,
                    stage_name="copy_image_generation",
                    asset_type="image",
                    uri=uri,
                    provider_name="stub",
                    model_name="stub-image",
                    prompt_summary="paper cup",
                    idempotency_key=f"copy-image-latest-{key}",
                    payload={
                        "variant_id": "V1",
                        "uri": uri,
                        "source": source,
                        "external_task_id": task_id,
                        "generation_status": "completed",
                        "error": error,
                    },
                )
            )
            db.flush()
        db.commit()

    resp = client.post(f"/runs/{run_id}/assets/refresh")
    assert resp.status_code == 200

    with SessionLocal() as db:
        from app.data.models import StageTask

        copy_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="copy_image_generation").one()
        assert copy_task.output_payload["image_assets"][0]["uri"] == new_uri
        assert copy_task.output_payload["image_assets"][0]["external_task_id"] == "new-task"


def test_copy_image_merge_replaces_regenerated_variant_payload():
    from app.services.runs import _merge_stage_payload

    merged = _merge_stage_payload(
        "copy_image_generation",
        {
            "copy_variants": [{"variant_id": "V1", "headline": "old"}, {"variant_id": "V2", "headline": "keep"}],
            "image_assets": [{"variant_id": "V1", "uri": "old.png"}, {"variant_id": "V2", "uri": "keep.png"}],
        },
        {
            "copy_variants": [{"variant_id": "V1", "headline": "new"}],
            "image_assets": [{"variant_id": "V1", "uri": "new.png"}],
        },
    )

    assert merged["copy_variants"] == [{"variant_id": "V1", "headline": "new"}, {"variant_id": "V2", "headline": "keep"}]
    assert merged["image_assets"] == [{"variant_id": "V1", "uri": "new.png"}, {"variant_id": "V2", "uri": "keep.png"}]


def test_assets_refresh_rechecks_completed_copy_image_qa(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-copy-image-refresh-qa",
            "project_name": "p-copy-image-refresh-qa",
            "product_name": "paper cup",
            "product_code": "COPY-IMAGE-REFRESH-QA-001",
            "industry_code": "packaging",
            "campaign_name": "copy-image-refresh-qa",
            "pipeline_mode": "copy_image_only",
            "approval_mode": "full_auto",
            "creative_preset": "meta_square_5s",
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["id"]
    image_uri = f"assets/{run_id}/copy_image_1.png"
    Path(image_uri).parent.mkdir(parents=True, exist_ok=True)
    Path(image_uri).write_bytes(base64.b64decode(_valid_png_b64()))

    stale_payload = {
        "variant_id": "V1",
        "uri": image_uri,
        "aspect_ratio": "1:1",
        "prompt": "paper cup",
        "source": "external_task_pending",
        "external_task_id": "copy-image-task-1",
        "generation_status": "submitted",
        "visual_qa": {"status": "warn", "flags": ["visual_qa_asset_processing"]},
        "image_asset_contract": {"status": "warn", "blocking": False, "flags": ["visual_qa_asset_processing"]},
    }
    with SessionLocal() as db:
        from app.data.models import PipelineRun, RunStatus, RunVariant, StageTask, TaskStatus

        run = db.get(PipelineRun, run_id)
        run.current_stage = "copy_image_generation"
        run.status = RunStatus.WAITING_REVIEW.value
        run_variant = RunVariant(run_id=run_id, variant_id="V1", angle="Cup", hook="Hook", message="Message")
        db.add(run_variant)
        db.flush()
        copy_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="copy_image_generation").one()
        copy_task.status = TaskStatus.WAITING_REVIEW.value
        copy_task.output_payload = {
            "copy_variants": [{"variant_id": "V1", "headline": "Paper cup"}],
            "image_assets": [dict(stale_payload)],
        }
        db.add(
            VariantAsset(
                run_variant_id=run_variant.id,
                run_id=run_id,
                stage_name="copy_image_generation",
                asset_type="image",
                uri=image_uri,
                provider_name="stub",
                model_name="stub-image",
                prompt_summary="paper cup",
                idempotency_key="test-refresh-copy-image-qa-v1",
                payload=dict(stale_payload),
            )
        )
        db.commit()

    resp = client.post(f"/runs/{run_id}/assets/refresh")
    assert resp.status_code == 200

    with SessionLocal() as db:
        copy_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="copy_image_generation").one()
        image_asset = db.query(VariantAsset).filter_by(run_id=run_id, asset_type="image").one()
        image_payload = copy_task.output_payload["image_assets"][0]
        assert image_payload["generation_status"] == "completed"
        assert image_payload["source"] == "local_file"
        assert "visual_qa_asset_processing" not in image_payload["visual_qa"]["flags"]
        assert image_payload["image_asset_contract"]["blocking"] is False
        assert image_asset.payload["visual_qa"]["status"] == image_payload["visual_qa"]["status"]


def test_assets_refresh_recovers_materialized_storyboard_candidate(client, monkeypatch):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-storyboard-refresh",
            "project_name": "p-storyboard-refresh",
            "product_name": "robe dress",
            "product_code": "ROBE-STORYBOARD-REFRESH-001",
            "industry_code": "fashion",
            "campaign_name": "storyboard-refresh",
            "pipeline_mode": "video_only",
            "approval_mode": "full_auto",
            "creative_preset": "meta_vertical_5s",
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["id"]

    with SessionLocal() as db:
        from app.data.models import PipelineRun, RunStatus, RunVariant, StageTask, TaskStatus

        run = db.get(PipelineRun, run_id)
        run.current_stage = "storyboard_image_generation"
        run.status = RunStatus.RUNNING.value
        run_variant = RunVariant(
            run_id=run_id,
            variant_id="V1",
            angle="Robe dress",
            hook="Open with an emotional real-model scene.",
            message="Show the robe dress across real settings.",
        )
        db.add(run_variant)
        db.flush()
        storyboard_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="storyboard_image_generation").one()
        storyboard_task.status = TaskStatus.WAITING_REVIEW.value
        storyboard_task.output_payload = {
            "frames": [
                {
                    "variant_id": "V1",
                    "frame_id": "V1_F1",
                    "external_task_id": "image-task-1",
                    "generation_status": "submitted",
                    "candidate_frames": [
                        {
                            "variant_id": "V1",
                            "frame_id": "V1_F1",
                            "candidate_index": 0,
                            "external_task_id": "image-task-1",
                            "generation_status": "submitted",
                        }
                    ],
                }
            ]
        }
        db.add(
            VariantAsset(
                run_variant_id=run_variant.id,
                run_id=run_id,
                stage_name="storyboard_image_generation",
                asset_type="storyboard_frame",
                uri=f"assets/{run_id}/V1_F1.png",
                provider_name="stub",
                model_name="stub-image",
                prompt_summary="storyboard frame for V1",
                idempotency_key="test-refresh-storyboard-v1-f1",
                payload={
                    "variant_id": "V1",
                    "frame_id": "V1_F1",
                    "candidate_index": 0,
                    "image_uri": f"assets/{run_id}/V1_F1.png",
                    "external_task_id": "image-task-1",
                    "generation_status": "submitted",
                    "source": "url",
                    "candidate_frames": [
                        {
                            "variant_id": "V1",
                            "frame_id": "V1_F1",
                            "candidate_index": 0,
                            "external_task_id": "image-task-1",
                            "generation_status": "submitted",
                        }
                    ],
                },
            )
        )
        db.commit()

    class FakeProvider:
        def poll_image_task(self, **kwargs):
            raise AssertionError("materialized storyboard assets should not be polled again")

    monkeypatch.setattr("app.services.runs.runtime.providers.get", lambda _name: FakeProvider())
    monkeypatch.setattr("app.services.runs._uri_has_payload", lambda *_args, **_kwargs: True)

    resp = client.post(f"/runs/{run_id}/assets/refresh")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["images"]["completed"] == 1

    with SessionLocal() as db:
        from app.data.models import PipelineRun, StageTask, TaskStatus

        storyboard_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="storyboard_image_generation").one()
        video_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="video_generation").one()
        frame = storyboard_task.output_payload["frames"][0]
        assert storyboard_task.status == TaskStatus.APPROVED.value
        assert video_task.status == TaskStatus.QUEUED.value
        assert db.get(PipelineRun, run_id).current_stage == "video_generation"
        assert frame["generation_status"] == "completed"
        assert frame["candidate_frames"][0]["generation_status"] == "completed"


def test_assets_refresh_does_not_complete_empty_storyboard_placeholder(client, monkeypatch):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-storyboard-empty",
            "project_name": "p-storyboard-empty",
            "product_name": "white dress",
            "product_code": "WHITE-STORYBOARD-EMPTY-001",
            "industry_code": "fashion",
            "campaign_name": "storyboard-empty",
            "pipeline_mode": "video_only",
            "approval_mode": "full_auto",
            "creative_preset": "meta_vertical_5s",
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["id"]
    empty_uri = f"assets/{run_id}/V1_F1.png"
    empty_path = Path(empty_uri)
    empty_path.parent.mkdir(parents=True, exist_ok=True)
    empty_path.write_bytes(b"")

    with SessionLocal() as db:
        from app.data.models import PipelineRun, RunStatus, RunVariant, StageTask, TaskStatus

        run = db.get(PipelineRun, run_id)
        run.current_stage = "storyboard_image_generation"
        run.status = RunStatus.RUNNING.value
        run_variant = RunVariant(run_id=run_id, variant_id="V1", angle="White dress", hook="Hook", message="Message")
        db.add(run_variant)
        db.flush()
        storyboard_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="storyboard_image_generation").one()
        storyboard_task.status = TaskStatus.WAITING_REVIEW.value
        storyboard_task.output_payload = {
            "frames": [
                {
                    "variant_id": "V1",
                    "frame_id": "V1_F1",
                    "external_task_id": "image-task-empty",
                    "generation_status": "submitted",
                    "image_uri": empty_uri,
                    "candidate_frames": [
                        {
                            "variant_id": "V1",
                            "frame_id": "V1_F1",
                            "candidate_index": 0,
                            "external_task_id": "image-task-empty",
                            "generation_status": "submitted",
                            "image_uri": empty_uri,
                        }
                    ],
                }
            ]
        }
        db.add(
            VariantAsset(
                run_variant_id=run_variant.id,
                run_id=run_id,
                stage_name="storyboard_image_generation",
                asset_type="storyboard_frame",
                uri=empty_uri,
                provider_name="stub",
                model_name="stub-image",
                prompt_summary="empty placeholder storyboard frame",
                idempotency_key="test-empty-storyboard-v1-f1",
                payload={
                    "variant_id": "V1",
                    "frame_id": "V1_F1",
                    "candidate_index": 0,
                    "image_uri": empty_uri,
                    "external_task_id": "image-task-empty",
                    "generation_status": "submitted",
                    "source": "external_task_pending",
                },
            )
        )
        db.commit()

    class FakeProvider:
        def poll_image_task(self, **kwargs):
            return type("ImageResult", (), {"images": [], "status": "processing", "raw_response": {}})()

    monkeypatch.setattr("app.services.runs.runtime.providers.get", lambda _name: FakeProvider())

    resp = client.post(f"/runs/{run_id}/assets/refresh")
    assert resp.status_code == 200
    assert resp.json()["images"]["completed"] == 0

    with SessionLocal() as db:
        from app.data.models import StageTask, TaskStatus

        storyboard_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="storyboard_image_generation").one()
        frame = storyboard_task.output_payload["frames"][0]
        assert storyboard_task.status == TaskStatus.WAITING_REVIEW.value
        assert frame["generation_status"] == "processing"


def test_assets_refresh_advances_segmented_video_queue(client, monkeypatch):
    def fake_sample_video_frames(*, video_path, output_dir, prefix, count=3):
        from PIL import Image

        output_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for idx in range(count):
            path = output_dir / f"{prefix}_frame_{idx + 1:03d}.png"
            image = Image.new("RGB", (200, 200))
            for x in range(200):
                for y in range(200):
                    image.putpixel(xy=(x, y), value=((x * 3 + idx * 20) % 255, (y * 5) % 255, ((x + y) * 2) % 255))
            image.save(path, format="PNG")
            paths.append(str(path))
        return paths

    monkeypatch.setattr("app.agents.runtime.sample_video_frames", fake_sample_video_frames)

    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-segment-refresh",
            "project_name": "p-segment-refresh",
            "product_name": "robe dress",
            "product_code": "ROBE-SEGMENT-REFRESH-001",
            "industry_code": "fashion",
            "campaign_name": "segment-refresh",
            "pipeline_mode": "video_only",
            "creative_preset": "meta_vertical_5s",
        },
    )
    run_id = create_resp.json()["id"]
    for _stage in ["intake", "planning", "divergence"]:
        _run_worker_once()
        client.post(f"/runs/{run_id}/advance", json={"notes": "ok"})

    with SessionLocal() as db:
        from app.data.models import RunVariant

        run_variant = db.query(RunVariant).filter(RunVariant.run_id == run_id, RunVariant.variant_id == "V1").one()
        db.add(
            VariantAsset(
                run_variant_id=run_variant.id,
                run_id=run_id,
                stage_name="video_generation",
                asset_type="video",
                uri=f"assets/{run_id}/V1_S1.mp4",
                provider_name="stub",
                model_name="stub-video",
                prompt_summary="segmented video asset for V1",
                idempotency_key="test-refresh-segmented-video-v1",
                payload={
                    "variant_id": "V1",
                    "video_uri": f"assets/{run_id}/V1_S1.mp4",
                    "source": "segmented_pending",
                    "generation_status": "pending",
                    "segment_prompt_base": "base prompt",
                    "video_size": "9:16",
                    "resolution": "720p",
                    "generation_spec": {"size": "9:16", "resolution": "720p"},
                    "segment_queue": [
                        {"segment_id": "V1_S1", "duration_seconds": 8, "motion_prompt": "first", "transition_to_next": "match_cut", "segment_contract": {"must_preserve": ["blue harness"]}},
                        {"segment_id": "V1_S2", "duration_seconds": 8, "motion_prompt": "second", "transition_to_next": "none", "segment_contract": {"must_preserve": ["blue harness"]}},
                    ],
                    "segments": [
                        {
                            "variant_id": "V1",
                            "segment_id": "V1_S1",
                            "segment_index": 0,
                            "video_uri": f"assets/{run_id}/V1_S1.mp4",
                            "external_task_id": "segment_task_1",
                            "generation_status": "processing",
                            "source": "external_task_pending",
                        }
                    ],
                },
            )
        )
        db.commit()

    class FakeProvider:
        def poll_video_task(self, **kwargs):
            return VideoGenResult(
                model_used="stub-video",
                task_id=kwargs["task_id"],
                status="completed",
                videos=[
                    GeneratedVideo(
                        url="https://example.com/segment1.mp4",
                        task_id=kwargs["task_id"],
                        status="completed",
                        raw_response={"last_frame_url": "https://example.com/segment1-last.png"},
                    )
                ],
            )

    def fake_next_segment(**kwargs):
        assert kwargs["generation_spec"]["return_last_frame"] is True
        assert kwargs["generation_spec"]["image_with_roles"] == [
            {"url": "https://example.com/segment1-last.png", "role": "first_frame"}
        ]
        assert not kwargs["generation_spec"].get("image_urls")
        return (
            {
                "variant_id": kwargs["variant_id"],
                "video_uri": f"assets/{run_id}/V1_S2.mp4",
                "external_task_id": "segment_task_2",
                "generation_status": "processing",
                "source": "external_task_pending",
            },
            0.0,
            "stub-video",
        )

    monkeypatch.setattr("app.services.runs.runtime.providers.get", lambda _name: FakeProvider())
    monkeypatch.setattr("app.services.runs.runtime._materialize_generated_video", lambda _selected: (b"x" * 2048, "url"))
    monkeypatch.setattr("app.services.runs.extract_last_video_frame", lambda **_kwargs: f"assets/{run_id}/V1_S1_last_frame.png")
    monkeypatch.setattr("app.services.runs.runtime._generate_video_clip_payload", fake_next_segment)

    resp = client.post(f"/runs/{run_id}/assets/refresh")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["videos"]["refreshed"] == 1
    assert payload["videos"]["completed"] == 1
    with SessionLocal() as db:
        asset = db.query(VariantAsset).filter(VariantAsset.run_id == run_id, VariantAsset.asset_type == "video").one()
        segments = asset.payload["segments"]
        assert segments[0]["generation_status"] == "completed"
        assert segments[0]["last_frame_uri"].endswith("V1_S1_last_frame.png")
        assert segments[0]["segment_frame_qa"]["status"] == "pass"
        assert segments[1]["segment_id"] == "V1_S2"
        assert segments[1]["external_task_id"] == "segment_task_2"
        assert segments[1]["reference_mode"] == "first_frame"
        assert segments[1]["reference_image_count"] == 1
        assert segments[1]["reference_manifest"][0]["source"] == "tail_frame"
        assert segments[1]["reference_manifest"][0]["provider_usable"] is True
        assert segments[1]["hosted_reference_ready"] is True
        assert asset.payload["segment_ledger"]["status"] == "pending"
        assert asset.payload["segment_ledger"]["segments"][0]["tail_frame"]
        assert asset.payload["segment_ledger"]["segments"][1]["contract"]["must_preserve"] == ["blue harness"]


def test_submit_next_video_segment_uses_local_tail_frame_and_human_constraints(monkeypatch, tmp_path):
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
    tail = tmp_path / "tail.png"
    tail.write_bytes(png)
    anchor_data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    def fake_generate(**kwargs):
        spec = kwargs["generation_spec"]
        assert len(spec["image_urls"]) == 1
        assert spec["image_urls"][0].startswith("data:image/png;base64,")
        assert "image_with_roles" not in spec
        assert "previous segment tail frame" in kwargs["video_prompt"]
        assert "Human anatomy constraint" in kwargs["video_prompt"]
        return (
            {
                "variant_id": kwargs["variant_id"],
                "video_uri": "assets/run/V1_S2.mp4",
                "external_task_id": "segment_task_2",
                "generation_status": "processing",
            },
            0.0,
            "stub-video",
        )

    monkeypatch.setattr("app.services.runs.runtime._generate_video_clip_payload", fake_generate)

    next_segment, _ = _submit_next_video_segment(
        run=SimpleNamespace(id="run"),
        payload={
            "variant_id": "V1",
            "segment_prompt_base": "Base prompt with a model wearing the dress.",
            "video_size": "9:16",
            "resolution": "720p",
            "generation_spec": {
                "size": "9:16",
                "resolution": "720p",
                "max_reference_images": 1,
                "image_urls": [anchor_data_url],
            },
            "segment_queue": [
                {"segment_id": "V1_S1", "duration_seconds": 8, "motion_prompt": "first"},
                {"segment_id": "V1_S2", "duration_seconds": 8, "motion_prompt": "same model walks slowly"},
            ],
            "segments": [{"segment_id": "V1_S1", "last_frame_uri": str(tail), "generation_status": "completed"}],
        },
        provider_name="stub",
        model_name="stub-video",
        runtime_config={},
        video_runtime={},
    )

    assert next_segment["segment_id"] == "V1_S2"
    assert next_segment["reference_mode"] == "tail_with_anchors"
    assert next_segment["reference_image_count"] == 1
    assert next_segment["reference_manifest"][0]["source"] == "tail_frame"
    assert next_segment["reference_manifest"][0]["used"] is True
    assert next_segment["reference_manifest"][0]["transport"] == "data_url"
    assert next_segment["hosted_reference_ready"] is True


def test_assets_refresh_updates_stage_output_and_downstream_inputs(client, monkeypatch):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-refresh-stage",
            "project_name": "p-refresh-stage",
            "product_name": "travel organizer",
            "product_code": "REFRESH-STAGE-001",
            "industry_code": "travel",
            "campaign_name": "video-refresh-stage",
            "pipeline_mode": "video_only",
            "creative_preset": "meta_vertical_5s",
        },
    )
    run_id = create_resp.json()["id"]
    for stage in ["intake", "planning", "divergence"]:
        _run_worker_once()
        client.post(f"/runs/{run_id}/advance", json={"notes": "ok"})

    with SessionLocal() as db:
        from app.data.models import PipelineRun, RunVariant, StageTask

        run = db.get(PipelineRun, run_id)
        run_variant = db.query(RunVariant).filter(RunVariant.run_id == run_id, RunVariant.variant_id == "V1").one()
        video_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="video_generation").one()
        visual_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="visual_quality_assessment").one()
        stale_payload = {
            "variant_id": "V1",
            "video_uri": f"assets/{run_id}/V1_sample.mp4",
            "external_task_id": "task_refresh_stage_1",
            "generation_status": "processing",
            "source": "external_task_pending",
        }
        video_task.output_payload = {"videos": [dict(stale_payload)]}
        db.add(
            VariantAsset(
                run_variant_id=run_variant.id,
                run_id=run_id,
                stage_name="video_generation",
                asset_type="video",
                uri=stale_payload["video_uri"],
                provider_name="stub",
                model_name="stub-video",
                prompt_summary="video asset for V1",
                idempotency_key="test-refresh-stage-output-v1",
                payload=dict(stale_payload),
            )
        )
        visual_task.input_payload = _build_task_input(db, run, visual_task)
        db.commit()

    class FakeProvider:
        def poll_video_task(self, **kwargs):
            return VideoGenResult(
                model_used="stub-video",
                task_id=kwargs["task_id"],
                status="completed",
                videos=[
                    GeneratedVideo(
                        url="https://example.com/video.mp4",
                        task_id=kwargs["task_id"],
                        status="completed",
                    )
                ],
            )

    monkeypatch.setattr("app.services.runs.runtime.providers.get", lambda _name: FakeProvider())
    monkeypatch.setattr("app.services.runs.runtime._materialize_generated_video", lambda _selected: (b"fake-video-bytes", "url"))
    monkeypatch.setattr(
        "app.services.runs.runtime._sample_generated_video_frames",
        lambda **kwargs: (
            [f"assets/{run_id}/V1_generated_video_frame_1.png", f"assets/{run_id}/V1_generated_video_frame_2.png", f"assets/{run_id}/V1_generated_video_frame_3.png"],
            [
                {"frame_id": "f1", "variant_id": "V1", "uri": f"assets/{run_id}/V1_generated_video_frame_1.png", "frame_index": 1},
                {"frame_id": "f2", "variant_id": "V1", "uri": f"assets/{run_id}/V1_generated_video_frame_2.png", "frame_index": 2},
                {"frame_id": "f3", "variant_id": "V1", "uri": f"assets/{run_id}/V1_generated_video_frame_3.png", "frame_index": 3},
            ],
        ),
    )

    resp = client.post(f"/runs/{run_id}/assets/refresh")
    assert resp.status_code == 200

    with SessionLocal() as db:
        from app.data.models import PipelineRun, StageTask

        run = db.get(PipelineRun, run_id)
        video_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="video_generation").one()
        refreshed_video = video_task.output_payload["videos"][0]
        assert refreshed_video["generation_status"] == "completed"
        assert refreshed_video["video_uri"].endswith("V1_sample.mp4")
        assert refreshed_video["frame_uris"] == [
            f"assets/{run_id}/V1_generated_video_frame_1.png",
            f"assets/{run_id}/V1_generated_video_frame_2.png",
            f"assets/{run_id}/V1_generated_video_frame_3.png",
        ]

        visual_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="visual_quality_assessment").one()
        rebuilt_input = _build_task_input(db, run, visual_task)
        rebuilt_video = rebuilt_input["videos"]["videos"][0]
        assert rebuilt_video["generation_status"] == "completed"
        assert rebuilt_video["video_uri"] == refreshed_video["video_uri"]
        assert rebuilt_video["frame_uris"] == refreshed_video["frame_uris"]


def test_assets_refresh_requeues_full_auto_visual_qa_after_materialization(client, monkeypatch):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "w-refresh-resume",
            "project_name": "p-refresh-resume",
            "product_name": "travel organizer",
            "product_code": "REFRESH-RESUME-001",
            "industry_code": "travel",
            "campaign_name": "video-refresh-resume",
            "pipeline_mode": "video_only",
            "approval_mode": "full_auto",
            "creative_preset": "meta_vertical_5s",
        },
    )
    run_id = create_resp.json()["id"]
    for stage in ["intake", "planning", "divergence"]:
        _run_worker_once()
        client.post(f"/runs/{run_id}/advance", json={"notes": "ok"})

    with SessionLocal() as db:
        from app.data.models import PipelineRun, RunStatus, RunVariant, StageTask, TaskStatus

        run = db.get(PipelineRun, run_id)
        run.status = RunStatus.WAITING_REVIEW.value
        run.current_stage = "visual_quality_assessment"
        run_variant = db.query(RunVariant).filter(RunVariant.run_id == run_id, RunVariant.variant_id == "V1").one()
        video_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="video_generation").one()
        visual_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="visual_quality_assessment").one()
        stale_payload = {
            "variant_id": "V1",
            "video_uri": f"assets/{run_id}/V1_sample.mp4",
            "external_task_id": "task_refresh_resume_1",
            "generation_status": "processing",
            "source": "external_task_pending",
        }
        video_task.output_payload = {"videos": [dict(stale_payload)]}
        db.add(
            VariantAsset(
                run_variant_id=run_variant.id,
                run_id=run_id,
                stage_name="video_generation",
                asset_type="video",
                uri=stale_payload["video_uri"],
                provider_name="stub",
                model_name="stub-video",
                prompt_summary="video asset for V1",
                idempotency_key="test-refresh-resume-v1",
                payload=dict(stale_payload),
            )
        )
        visual_task.status = TaskStatus.WAITING_REVIEW.value
        visual_task.output_payload = {
            "variant_summaries": [
                {"variant_id": "V1", "recommended_action": "wait_for_asset", "qa_status": "pending"}
            ]
        }
        visual_task.input_payload = _build_task_input(db, run, visual_task)
        db.commit()

    class FakeProvider:
        def poll_video_task(self, **kwargs):
            return VideoGenResult(
                model_used="stub-video",
                task_id=kwargs["task_id"],
                status="completed",
                videos=[
                    GeneratedVideo(
                        url="https://example.com/video.mp4",
                        task_id=kwargs["task_id"],
                        status="completed",
                    )
                ],
            )

    monkeypatch.setattr("app.services.runs.runtime.providers.get", lambda _name: FakeProvider())
    monkeypatch.setattr("app.services.runs.runtime._materialize_generated_video", lambda _selected: (b"fake-video-bytes", "url"))
    monkeypatch.setattr(
        "app.services.runs.runtime._sample_generated_video_frames",
        lambda **kwargs: (
            [f"assets/{run_id}/V1_generated_video_frame_1.png"],
            [{"frame_id": "f1", "variant_id": "V1", "uri": f"assets/{run_id}/V1_generated_video_frame_1.png", "frame_index": 1}],
        ),
    )

    resp = client.post(f"/runs/{run_id}/assets/refresh")
    assert resp.status_code == 200

    with SessionLocal() as db:
        from app.data.models import PipelineRun, StageTask, TaskStatus

        run = db.get(PipelineRun, run_id)
        visual_task = db.query(StageTask).filter_by(run_id=run_id, stage_name="visual_quality_assessment").one()
        assert run.status == "running"
        assert run.current_stage == "visual_quality_assessment"
        assert visual_task.status == TaskStatus.QUEUED.value
        assert visual_task.input_payload["videos"]["videos"][0]["generation_status"] == "completed"
        assert visual_task.input_payload["videos"]["videos"][0]["frame_uris"] == [
            f"assets/{run_id}/V1_generated_video_frame_1.png"
        ]


def test_variant_quality_summary_exposes_frame_review_flags(client):
    create_resp = client.post(
        "/runs",
        json={
            "workspace_name": "frame-review-w",
            "project_name": "frame-review-p",
            "product_name": "travel organizer",
            "product_code": "FRAME-001",
            "industry_code": "travel",
            "campaign_name": "frame-review-campaign",
            "pipeline_mode": "video_only",
            "creative_preset": "meta_vertical_5s",
        },
    )
    run_id = create_resp.json()["id"]

    with SessionLocal() as db:
        from app.data.models import RunVariant, VariantScore

        run_variant = RunVariant(
            run_id=run_id,
            variant_id="V1",
            angle="product-first opening",
            hook="Show the organizer before the mess",
            message="Lead with a clear first frame and keep continuity stable.",
        )
        db.add(run_variant)
        db.flush()
        db.add(
            VariantAsset(
                run_variant_id=run_variant.id,
                run_id=run_id,
                stage_name="copy_image_generation",
                asset_type="image",
                uri=f"assets/{run_id}/V1_reference.png",
                provider_name="stub",
                model_name="stub-image",
                prompt_summary="reference-backed image asset for V1",
                idempotency_key="frame-review-v1-image",
                payload={
                    "variant_id": "V1",
                    "image_uri": f"assets/{run_id}/V1_reference.png",
                    "generation_status": "completed",
                    "reference_source_count": 2,
                    "visual_qa": {
                        "status": "pass",
                        "score": 91,
                        "flags": [],
                        "checks": [],
                    },
                },
            )
        )
        db.add(
            VariantAsset(
                run_variant_id=run_variant.id,
                run_id=run_id,
                stage_name="video_generation",
                asset_type="video",
                uri=f"assets/{run_id}/V1_sample.mp4",
                provider_name="stub",
                model_name="stub-video",
                prompt_summary="video asset for V1",
                idempotency_key="frame-review-v1-video",
                payload={
                    "variant_id": "V1",
                    "video_uri": f"assets/{run_id}/V1_sample.mp4",
                    "generation_status": "completed",
                    "visual_qa": {
                        "status": "warn",
                        "score": 84,
                        "flags": [
                            "visual_qa_needs_frame_review",
                            "visual_qa_first_frame_clarity_check",
                        ],
                        "checks": [
                            {
                                "key": "frame_sequence",
                                "status": "manual_review",
                                "message": "Review the opening frame for hook clarity.",
                            }
                        ],
                    },
                },
            )
        )
        db.add(
            VariantScore(
                run_variant_id=run_variant.id,
                run_id=run_id,
                stage_name="visual_quality_assessment",
                score_type="visual_quality",
                total_score=84,
                compliance_level="warn",
                recommended_action="manual_review",
                sub_scores={"visual_score": 84, "blocking_issue_count": 0},
                reasons=["visual_qa_needs_frame_review"],
                forecast={},
                payload={
                    "variant_id": "V1",
                    "asset_reports": [
                        {
                            "asset_type": "video",
                            "flags": [
                                "visual_qa_needs_frame_review",
                                "visual_qa_first_frame_clarity_check",
                            ],
                        }
                    ],
                },
            )
        )
        db.commit()

    variants = client.get(f"/runs/{run_id}/variants")
    assert variants.status_code == 200
    payload = variants.json()
    item = next(entry for entry in payload["items"] if entry["variant_id"] == "V1")
    assert item["quality_summary"]["frame_review_flags"] == [
        "visual_qa_first_frame_clarity_check",
        "visual_qa_needs_frame_review",
    ]
    assert item["quality_summary"]["reference_source_count"] == 2


def test_dashboard_variant_board_exposes_compact_social_quality_badges(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.text
    assert "Frame review" in html
    assert "Ref-backed" in html


def test_plain_runs_blocks_preflight_errors_before_creating_run(client):
    patch_resp = client.patch(
        "/agent-configs/video_generation_agent",
        json={
            "video_provider_name": "deepseek",
            "video_model_name": "deepseek-v3.2",
            "video_api_base_url": "https://api.deepseek.com/v1/chat/completions",
        },
    )
    assert patch_resp.status_code == 200

    resp = client.post(
        "/runs",
        json={
            "workspace_name": "plain-preflight-ws",
            "project_name": "plain-preflight-project",
            "product_name": "plain-preflight-product",
            "product_code": "PLAIN-PF-001",
            "industry_code": "pet",
            "campaign_name": "plain-preflight-campaign",
            "creative_preset": "meta_square_5s",
            "pipeline_mode": "video_only",
        },
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "preflight_failed"
    assert detail["preflight"]["severity"] == "error"
    assert any(
        row["key"] == "video_generation.video_generation"
        for row in detail["preflight"]["checks"]
    )

    runs = client.get("/runs").json()
    assert all(run["product_code"] != "PLAIN-PF-001" for run in runs)


def test_plain_runs_accept_remote_reference_media_from_creative_specs(client):
    resp = client.post(
        "/runs",
        json={
            "workspace_name": "plain-remote-media-ws",
            "project_name": "plain-remote-media-project",
            "product_name": "plain-remote-media-product",
            "product_code": "PLAIN-REMOTE-001",
            "industry_code": "pet",
            "campaign_name": "plain-remote-media-campaign",
            "creative_preset": "marketplace_main_image_pack",
            "pipeline_mode": "marketplace_main_image",
            "creative_specs": {
                "image_urls": [
                    "https://cdn.example.com/reference/front-shot.png?width=1600&v=2"
                ],
                "video_urls": [
                    "https://cdn.example.com/reference/demo.mp4?download=1"
                ],
            },
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"]
    assert payload["product_code"] == "PLAIN-REMOTE-001"
