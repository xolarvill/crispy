from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path

from sqlalchemy import desc, or_, select, update
from sqlalchemy.orm import Session

from app.agents.persona_contracts import build_compiled_persona
from app.agents.registry import STAGE_CONTRACT_VERSION, stage_agent, stage_collaborators
from app.agents.runtime import AgentsRuntime
from app.data.models import (
    AgentTraceEvent,
    Artifact,
    Campaign,
    GmMemory,
    PipelineRun,
    Product,
    Project,
    RunStatus,
    RunVariant,
    ScoreCard as ScoreCardModel,
    StageTask,
    TaskFailureCategory,
    TaskStatus,
    VariantAsset,
    VariantLifecycleStatus,
    VariantReview,
    VariantReviewAction,
    VariantScore,
    Workspace,
)
from app.orchestrator.state_machine import next_stage, should_auto_approve, stage_plan_for
from app.orchestrator.stage_contracts import get_stage_contract
from app.schemas.api import RunCreateRequest
from app.schemas.contracts import (
    VariantSet,
    VideoSegmentPlan,
    VideoScriptPack,
)
from app.services.agent_api_configs import (
    has_resolved_image_config,
    resolve_agent_config,
    resolve_agent_runtime,
    with_fallback_image_config,
)
from app.services.creative_specs import (
    DTC_SITE_IMAGE_PRESET,
    TIKTOK_SHOP_VIDEO_DEFAULT_STYLE,
    TIKTOK_SHOP_VIDEO_PRESET,
    get_dtc_site_review_hints,
    resolve_creative_specs,
)
from app.services.creative_attribution import canonical_creative_key
from app.services.execution_memory import (
    append_execution_memory_payload,
    build_variant_execution_summary,
    resolve_execution_memory,
    write_regeneration_memory,
    write_stage_approval_memory,
    write_stage_completion_memory,
    write_stage_rejection_memory,
    write_variant_review_memory,
)
from app.services.marketplace_qa import MARKETPLACE_REVIEW_TAGS, is_marketplace_main_image
from app.services.memory_selection import normalize_memory_selection
from app.services.personas import get_persona
from app.services.gm_evolution import compile_run_outcome_reflection
from app.services.stage_inputs import (
    analytics_insights as _analytics_insights,
    build_task_input as _build_task_input,
    recent_gm_lessons as _recent_gm_lessons,
    stage_output_optional as _stage_output_optional,
)
from app.services.stage_execution import (
    RegenerationExecutionContext,
    StageExecutionContext,
    execute_regeneration_stage,
    execute_runtime_stage,
)
from app.services.video_frames import extract_last_video_frame, stitch_video_files
from app.services.visual_qa import inspect_visual_asset


runtime = AgentsRuntime()

logger = logging.getLogger(__name__)

RETRYABLE_FAILURES = {
    TaskFailureCategory.PROVIDER_ERROR.value,
    TaskFailureCategory.SCHEMA_ERROR.value,
    TaskFailureCategory.TIMEOUT.value,
}

REGENERATABLE_STAGES = {
    "copy_image_generation",
    "video_scripting",
    "storyboard_image_generation",
    "video_generation",
}
STAGE_RETRY_DELAYS_SECONDS = (180.0, 300.0, 480.0)


def _get_stage_output(db: Session, run_id: str, stage_name: str) -> dict | None:
    """Read the output_payload of the most recent completed task for a stage."""
    task = (
        db.query(StageTask)
        .filter_by(run_id=run_id, stage_name=stage_name, failure_category=None)
        .order_by(StageTask.completed_at.desc())
        .first()
    )
    return task.output_payload if task else None


def utcnow() -> datetime:
    return datetime.now(UTC)


def _retry_delay(attempt: int) -> float:
    return STAGE_RETRY_DELAYS_SECONDS[min(max(attempt, 1), len(STAGE_RETRY_DELAYS_SECONDS)) - 1]


def select_next_queued_task(db: Session) -> StageTask | None:
    """Atomically claim the next queued task with priority ordering.

    Priority: lower int = higher priority (0=human-rejected, 1=regen, 2=normal).
    Within same priority, oldest tasks first (FIFO).
    Uses a guarded UPDATE to prevent double-claiming across concurrent workers.
    """
    now = utcnow()
    candidate = db.scalar(
        select(StageTask)
        .where(
            StageTask.status == TaskStatus.QUEUED.value,
            or_(StageTask.retry_at.is_(None), StageTask.retry_at <= now),
        )
        .order_by(StageTask.priority.asc(), StageTask.created_at.asc())
        .limit(1)
    )
    if not candidate:
        return None
    result = db.execute(
        update(StageTask)
        .where(StageTask.id == candidate.id, StageTask.status == TaskStatus.QUEUED.value)
        .values(status=TaskStatus.RUNNING.value, started_at=now)
    )
    if result.rowcount == 0:
        return None
    db.refresh(candidate)
    return candidate


def _truncate_text(value: str, limit: int = 800) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def _payload_shape(payload: dict | None) -> dict:
    payload = payload or {}
    shape: dict = {}
    for key, value in payload.items():
        if isinstance(value, list):
            shape[key] = {"type": "list", "count": len(value)}
        elif isinstance(value, dict):
            shape[key] = {"type": "dict", "keys": list(value.keys())[:12]}
        else:
            shape[key] = _truncate_text(str(value), 160)
    return shape


def add_agent_trace_event(
    db: Session,
    *,
    run_id: str,
    stage_task_id: str | None,
    stage_name: str,
    agent_name: str,
    event_type: str,
    message: str,
    visibility: str = "user",
    provider_name: str | None = None,
    model_name: str | None = None,
    payload: dict | None = None,
) -> AgentTraceEvent:
    event = AgentTraceEvent(
        run_id=run_id,
        stage_task_id=stage_task_id,
        stage_name=stage_name,
        agent_name=agent_name,
        event_type=event_type,
        visibility=visibility,
        message=message,
        provider_name=provider_name,
        model_name=model_name,
        payload=payload or {},
    )
    db.add(event)
    db.flush()
    return event


def run_trace_events(db: Session, run_id: str, *, limit: int = 200, visibility: str | None = None) -> list[AgentTraceEvent]:
    stmt = select(AgentTraceEvent).where(AgentTraceEvent.run_id == run_id)
    if visibility:
        stmt = stmt.where(AgentTraceEvent.visibility == visibility)
    rows = list(db.scalars(stmt.order_by(desc(AgentTraceEvent.created_at)).limit(limit)).all())
    return list(reversed(rows))


def _get_or_create_workspace(db: Session, name: str) -> Workspace:
    workspace = db.scalar(select(Workspace).where(Workspace.name == name))
    if workspace:
        return workspace
    workspace = Workspace(name=name)
    db.add(workspace)
    db.flush()
    return workspace


def _get_or_create_project(db: Session, workspace_id: str, name: str, market: str, locale: str) -> Project:
    project = db.scalar(select(Project).where(Project.workspace_id == workspace_id, Project.name == name))
    if project:
        project.market = market
        project.locale = locale
        return project
    project = Project(workspace_id=workspace_id, name=name, market=market, locale=locale)
    db.add(project)
    db.flush()
    return project


def _get_or_create_product(db: Session, project_id: str, name: str, product_code: str) -> Product:
    normalized_code = product_code.strip()
    if not normalized_code:
        raise ValueError("product_code is required")
    product = db.scalar(select(Product).where(Product.product_code == normalized_code))
    if product:
        if product.name != name:
            raise ValueError(f"product_code conflict: {normalized_code} is already bound to product_name={product.name}")
        if product.project_id != project_id:
            raise ValueError(f"product_code conflict: {normalized_code} belongs to another project")
        return product
    name_conflict = db.scalar(select(Product).where(Product.project_id == project_id, Product.name == name))
    if name_conflict:
        raise ValueError(
            f"product name conflict: {name} is already bound to product_code={name_conflict.product_code}"
        )
    product = Product(project_id=project_id, name=name, product_code=normalized_code)
    db.add(product)
    db.flush()
    return product


def _get_or_create_campaign(
    db: Session,
    project_id: str,
    product_id: str,
    name: str,
    channel: str,
    objective: str,
) -> Campaign:
    campaign = db.scalar(select(Campaign).where(Campaign.project_id == project_id, Campaign.name == name))
    if campaign:
        campaign.product_id = product_id
        campaign.channel = channel
        campaign.objective = objective
        return campaign
    campaign = Campaign(
        project_id=project_id,
        product_id=product_id,
        name=name,
        channel=channel,
        objective=objective,
    )
    db.add(campaign)
    db.flush()
    return campaign


def _model_snapshot_for_run(db: Session, *, run_provider: str | None, run_model: str | None) -> dict:
    fallback_provider = run_provider or "openai"
    fallback_model = run_model or "gpt-4.1"
    agent_names = ("planning_agent", "copy_image_agent", "video_generation_agent", "evaluation_agent")
    snapshot = {}
    default_cfg = resolve_agent_config(
        db,
        agent_name="default",
        run_provider=fallback_provider,
        run_model=fallback_model,
    )
    snapshot["default_text"] = {
        "provider_name": default_cfg.get("provider_name"),
        "model_name": default_cfg.get("model_name"),
        "api_base_url": default_cfg.get("api_base_url"),
        "api_key_env": default_cfg.get("api_key_env"),
    }
    for agent_name in agent_names:
        cfg = resolve_agent_config(
            db,
            agent_name=agent_name,
            run_provider=fallback_provider,
            run_model=fallback_model,
        )
        snapshot[agent_name] = {
            "provider_name": cfg.get("provider_name"),
            "model_name": cfg.get("model_name"),
            "api_base_url": cfg.get("api_base_url"),
            "api_key_env": cfg.get("api_key_env"),
        }
    return snapshot


def create_run(db: Session, payload: RunCreateRequest) -> PipelineRun:
    stage_plan = stage_plan_for(payload.pipeline_mode)
    workspace = _get_or_create_workspace(db, payload.workspace_name)
    project = _get_or_create_project(db, workspace.id, payload.project_name, payload.market, payload.locale)
    product = _get_or_create_product(db, project.id, payload.product_name, payload.product_code)
    campaign = _get_or_create_campaign(
        db=db,
        project_id=project.id,
        product_id=product.id,
        name=payload.campaign_name,
        channel=payload.channel,
        objective=payload.objective,
    )
    creative_preset = payload.creative_preset
    if payload.pipeline_mode == "dtc_site_image":
        creative_preset = DTC_SITE_IMAGE_PRESET
    creative_specs = resolve_creative_specs(creative_preset, payload.creative_specs, pipeline_mode=payload.pipeline_mode)
    if payload.pipeline_mode == "dtc_site_image":
        defaults = resolve_creative_specs(DTC_SITE_IMAGE_PRESET)
        defaults.update(creative_specs)
        defaults["asset_goal"] = "dtc_site_image"
        defaults.setdefault("platform_targets", ["shopify"])
        creative_specs = defaults
    if payload.pipeline_mode == "marketplace_main_image" and not is_marketplace_main_image(creative_specs):
        creative_preset = "marketplace_main_image_pack"
        defaults = resolve_creative_specs(creative_preset)
        defaults.update(creative_specs)
        defaults["asset_goal"] = "marketplace_main_image"
        defaults.setdefault("platform_targets", [])
        defaults.setdefault("export_size_px", 2000)
        defaults.setdefault("background_policy", "pure_white")
        creative_specs = defaults
    if payload.pipeline_mode == "tiktok_shop_video":
        creative_preset = TIKTOK_SHOP_VIDEO_PRESET
        defaults = resolve_creative_specs(creative_preset)
        defaults.update(creative_specs)
        defaults["platform"] = "tiktok"
        defaults["creative_goal"] = "shop_conversion_video"
        defaults.setdefault("tiktok_video_style", TIKTOK_SHOP_VIDEO_DEFAULT_STYLE)
        defaults.setdefault("platform_targets", ["tiktok", "tiktok_shop"])
        creative_specs = defaults
    enable_research = False if payload.pipeline_mode == "tiktok_shop_video" else payload.enable_research
    model_snapshot = _model_snapshot_for_run(
        db,
        run_provider=payload.model_provider,
        run_model=payload.model_name,
    )
    context_json = payload.context or {}
    context_json.setdefault("business_context", payload.business_context or {})
    context_json.setdefault("category_tags", payload.category_tags or [])
    context_json["memory_selection"] = normalize_memory_selection(payload.memory_selection)
    context_json["creative_specs"] = creative_specs
    context_json["model_snapshot"] = model_snapshot
    run = PipelineRun(
        workspace_id=workspace.id,
        project_id=project.id,
        product_id=product.id,
        campaign_id=campaign.id,
        status=RunStatus.RUNNING.value,
        current_stage=stage_plan[0],
        market=payload.market,
        locale=payload.locale,
        pipeline_mode=payload.pipeline_mode,
        approval_mode=payload.approval_mode or "manual",
        product_code=product.product_code,
        industry_code=payload.industry_code,
        creative_preset=creative_preset,
        creative_specs=creative_specs,
        model_provider=payload.model_provider or "openai",
        model_name=payload.model_name or "gpt-4.1",
        variant_count=payload.variant_count,
        enable_research=enable_research,
        manual_research_brief=payload.manual_research_brief,
        business_context=payload.business_context or {},
        category_tags=payload.category_tags or [],
        context_json=context_json,
    )
    db.add(run)
    db.flush()

    for idx, stage_name in enumerate(stage_plan):
        task_status = TaskStatus.QUEUED.value if idx == 0 else TaskStatus.DRAFT.value
        db.add(
            StageTask(
                run_id=run.id,
                stage_name=stage_name,
                status=task_status,
                priority=2,
                input_payload={},
            )
        )
    db.flush()
    return run


def get_run(db: Session, run_id: str) -> PipelineRun:
    run = db.get(PipelineRun, run_id)
    if not run:
        raise ValueError(f"run not found: {run_id}")
    return run


def get_stage_task(db: Session, run_id: str, stage_name: str) -> StageTask:
    task = db.scalar(select(StageTask).where(StageTask.run_id == run_id, StageTask.stage_name == stage_name))
    if not task:
        raise ValueError(f"stage task not found: {run_id}/{stage_name}")
    return task


def get_run_variant(db: Session, run_id: str, variant_id: str) -> RunVariant:
    row = db.scalar(select(RunVariant).where(RunVariant.run_id == run_id, RunVariant.variant_id == variant_id))
    if not row:
        raise ValueError(f"run variant not found: {run_id}/{variant_id}")
    return row


def latest_scorecard(db: Session, run_id: str) -> ScoreCardModel | None:
    return db.scalar(select(ScoreCardModel).where(ScoreCardModel.run_id == run_id).order_by(desc(ScoreCardModel.created_at)))


def _requeue_next_stage(db: Session, run_id: str, stage_name: str | None) -> None:
    if not stage_name:
        return
    next_task = get_stage_task(db, run_id, stage_name)
    if next_task.status in {TaskStatus.DRAFT.value, TaskStatus.REJECTED.value, TaskStatus.FAILED.value}:
        next_task.status = TaskStatus.QUEUED.value
        next_task.priority = 2
        next_task.retry_at = None


def _store_run_visual_identity(db: Session, run: PipelineRun, task: StageTask) -> None:
    if task.stage_name != "intake" or not is_marketplace_main_image(run.creative_specs):
        return
    visual_identity = (task.output_payload or {}).get("visual_identity")
    if not isinstance(visual_identity, dict):
        return
    product = db.get(Product, run.product_id)
    if not product:
        return
    product.metadata_json = {
        **(product.metadata_json or {}),
        "latest_visual_identity": visual_identity,
        "latest_visual_identity_run_id": run.id,
        "latest_visual_identity_updated_at": utcnow().isoformat(),
    }


def approve_stage(db: Session, run_id: str, notes: str = "") -> PipelineRun:
    run = get_run(db, run_id)
    if not run.current_stage:
        return run
    task = get_stage_task(db, run_id, run.current_stage)
    if task.status != TaskStatus.WAITING_REVIEW.value:
        raise ValueError(f"stage {run.current_stage} is not waiting for review")
    task.status = TaskStatus.APPROVED.value
    task.approved_at = utcnow()
    task.review_notes = notes
    task.failure_category = None
    _store_run_visual_identity(db, run, task)
    add_agent_trace_event(
        db,
        run_id=run.id,
        stage_task_id=task.id,
        stage_name=task.stage_name,
        agent_name=(task.metadata_json or {}).get("agent_name") or "human_reviewer",
        event_type="human_approved",
        message=f"Human approved stage {run.current_stage}.",
        payload={"notes": notes},
    )
    write_stage_approval_memory(db, run=run, task=task, notes=notes)

    nxt = next_stage(run.current_stage, run.pipeline_mode)
    if nxt is None:
        run.current_stage = None
        run.status = RunStatus.COMPLETED.value
    else:
        _requeue_next_stage(db, run.id, nxt)
        run.current_stage = nxt
        run.status = RunStatus.RUNNING.value
    run.updated_at = utcnow()
    db.flush()
    return run


def auto_approve_stage(db: Session, run_id: str, approved_stage: str) -> PipelineRun:
    """Programmatic auto-approval — same logic as approve_stage but with auto_approved trace."""
    run = get_run(db, run_id)
    if not run.current_stage or run.current_stage != approved_stage:
        return run
    task = get_stage_task(db, run_id, approved_stage)
    if task.status != TaskStatus.WAITING_REVIEW.value:
        return run
    task.status = TaskStatus.APPROVED.value
    task.approved_at = utcnow()
    task.review_notes = f"auto_approved ({run.approval_mode})"
    task.failure_category = None
    _store_run_visual_identity(db, run, task)
    add_agent_trace_event(
        db,
        run_id=run.id,
        stage_task_id=task.id,
        stage_name=task.stage_name,
        agent_name="system_auto_approve",
        event_type="auto_approved",
        message=f"Auto-approved stage {approved_stage} (mode={run.approval_mode}).",
        payload={"approval_mode": run.approval_mode},
    )

    nxt = next_stage(run.current_stage, run.pipeline_mode)
    if nxt is None:
        run.current_stage = None
        run.status = RunStatus.COMPLETED.value
    else:
        _requeue_next_stage(db, run.id, nxt)
        run.current_stage = nxt
        run.status = RunStatus.RUNNING.value
    run.updated_at = utcnow()
    db.flush()
    return run


def reject_stage(db: Session, run_id: str, notes: str = "") -> PipelineRun:
    run = get_run(db, run_id)
    if not run.current_stage:
        raise ValueError("run already completed")
    task = get_stage_task(db, run_id, run.current_stage)
    if task.status not in {TaskStatus.WAITING_REVIEW.value, TaskStatus.FAILED.value}:
        raise ValueError(f"stage {run.current_stage} is not rejectable")
    task.status = TaskStatus.QUEUED.value
    task.priority = 0
    task.retry_at = None
    task.rejected_at = utcnow()
    task.review_notes = notes
    task.failure_category = TaskFailureCategory.HUMAN_REJECT.value
    task.metadata_json = {**(task.metadata_json or {}), "human_feedback": notes}
    add_agent_trace_event(
        db,
        run_id=run.id,
        stage_task_id=task.id,
        stage_name=task.stage_name,
        agent_name=(task.metadata_json or {}).get("agent_name") or "human_reviewer",
        event_type="human_rejected",
        message=f"Human rejected stage {run.current_stage}; it was queued for rerun.",
        payload={"notes": notes},
    )
    write_stage_rejection_memory(db, run=run, task=task, notes=notes)
    run.status = RunStatus.RUNNING.value
    run.updated_at = utcnow()
    db.flush()
    return run


def rerun_stage(db: Session, run_id: str, stage_name: str, notes: str = "") -> PipelineRun:
    run = get_run(db, run_id)
    stage_plan = stage_plan_for(run.pipeline_mode)
    if stage_name not in stage_plan:
        raise ValueError(f"stage {stage_name} is not in run pipeline")

    start_index = stage_plan.index(stage_name)
    target = get_stage_task(db, run_id, stage_name)
    for downstream_stage in stage_plan[start_index + 1:]:
        task = get_stage_task(db, run_id, downstream_stage)
        task.status = TaskStatus.DRAFT.value
        task.output_payload = {}
        task.review_notes = None
        task.error_message = None
        task.failure_category = None
        task.model_used = None
        task.retry_at = None
        task.started_at = None
        task.completed_at = None
        task.approved_at = None
        task.rejected_at = None
        task.metadata_json = {**(task.metadata_json or {}), "superseded_by_stage_rerun": stage_name}

    target.status = TaskStatus.QUEUED.value
    target.priority = 0
    target.retry_at = None
    target.output_payload = {}
    target.review_notes = notes
    target.error_message = None
    target.failure_category = TaskFailureCategory.HUMAN_REJECT.value if notes else None
    target.started_at = None
    target.completed_at = None
    target.approved_at = None
    target.rejected_at = utcnow()
    target.input_payload = _build_task_input(db, run, target)
    target.metadata_json = {
        **(target.metadata_json or {}),
        "stage_rerun_requested": True,
        "stage_rerun_notes": notes,
    }
    add_agent_trace_event(
        db,
        run_id=run.id,
        stage_task_id=target.id,
        stage_name=target.stage_name,
        agent_name=(target.metadata_json or {}).get("agent_name") or "operator",
        event_type="stage_rerun_requested",
        message=f"Stage {stage_name} queued for rerun.",
        payload={"notes": notes, "downstream_reset": stage_plan[start_index + 1:]},
    )
    run.current_stage = stage_name
    run.status = RunStatus.RUNNING.value
    run.updated_at = utcnow()
    db.flush()
    return run


def _sync_refreshed_video_generation_state(
    db: Session,
    run: PipelineRun,
    refreshed_payloads_by_variant: dict[str, dict],
) -> None:
    if not refreshed_payloads_by_variant:
        return
    try:
        video_task = get_stage_task(db, run.id, "video_generation")
    except ValueError:
        return

    current_payload = dict(video_task.output_payload or {})
    existing_videos = [
        dict(item)
        for item in (current_payload.get("videos") or [])
        if isinstance(item, dict)
    ]
    merged_videos: list[dict] = []
    seen_variant_ids: set[str] = set()
    for item in existing_videos:
        variant_id = str(item.get("variant_id") or "")
        if variant_id and variant_id in refreshed_payloads_by_variant:
            merged_videos.append(dict(refreshed_payloads_by_variant[variant_id]))
            seen_variant_ids.add(variant_id)
        else:
            merged_videos.append(item)
    for variant_id, payload in refreshed_payloads_by_variant.items():
        if variant_id not in seen_variant_ids:
            merged_videos.append(dict(payload))
    video_task.output_payload = {**current_payload, "videos": merged_videos}

    for stage_name in ("visual_quality_assessment", "evaluation_selection"):
        try:
            task = get_stage_task(db, run.id, stage_name)
        except ValueError:
            continue
        if task.status in {
            TaskStatus.DRAFT.value,
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value,
            TaskStatus.WAITING_REVIEW.value,
            TaskStatus.REJECTED.value,
            TaskStatus.FAILED.value,
        }:
            task.input_payload = _build_task_input(db, run, task)


def _resume_full_auto_visual_qa_after_refresh(db: Session, run: PipelineRun) -> None:
    if run.approval_mode != "full_auto":
        return
    try:
        visual_task = get_stage_task(db, run.id, "visual_quality_assessment")
        video_task = get_stage_task(db, run.id, "video_generation")
    except ValueError:
        return
    if run.current_stage != "visual_quality_assessment":
        return
    if visual_task.status != TaskStatus.WAITING_REVIEW.value:
        return
    summaries = (visual_task.output_payload or {}).get("variant_summaries") or []
    has_pending_review = any(
        isinstance(summary, dict)
        and (
            summary.get("recommended_action") == "wait_for_asset"
            or str(summary.get("qa_status") or "").lower() == "pending"
        )
        for summary in summaries
    )
    if not has_pending_review:
        return
    videos = (video_task.output_payload or {}).get("videos") or []
    if any(
        isinstance(video, dict)
        and str(video.get("generation_status") or "").lower() in {"submitted", "queued", "pending", "processing", "running"}
        for video in videos
    ):
        return
    visual_task.status = TaskStatus.QUEUED.value
    visual_task.priority = 1
    visual_task.retry_at = None
    visual_task.input_payload = _build_task_input(db, run, visual_task)
    visual_task.metadata_json = {
        **(visual_task.metadata_json or {}),
        "full_auto_visual_qa_pending_assets": False,
        "full_auto_visual_qa_resumed_after_refresh": True,
    }
    run.status = RunStatus.RUNNING.value
    run.current_stage = "visual_quality_assessment"
    run.updated_at = utcnow()


def _gm_memory_trace_payload(gm_lessons: list[dict], research_context: dict | None = None) -> dict:
    references = []
    for item in gm_lessons[:5]:
        content = item.get("content") or {}
        references.append({
            "memory_id": item.get("id"),
            "memory_scope": item.get("memory_scope"),
            "source_type": item.get("source_type"),
            "memory_type": item.get("memory_type"),
            "summary": content.get("summary") or content.get("source") or "",
            "research_status": content.get("research_status"),
            "evidence_count": len(content.get("evidence") or []),
            "expires_at": content.get("expires_at"),
            "reason": "matched product/shop/industry planning context",
        })
    return {
        "memory_count": len(gm_lessons),
        "references": references,
        "research_context": research_context or {},
        "excluded_research": (research_context or {}).get("excluded", [])[:10],
        "influence": "included in planning gm_lessons prompt context",
    }


def _single_variant_set(db: Session, run_id: str, variant_id: str) -> VariantSet:
    variants = VariantSet.model_validate(_stage_output_optional(db, run_id, "divergence"))
    selected = [item for item in variants.variants if item.variant_id == variant_id]
    if not selected:
        raise ValueError(f"variant {variant_id} not found in divergence output")
    return VariantSet(variants=selected)


def _single_script_pack(db: Session, run_id: str, variant_id: str) -> VideoScriptPack:
    scripts = VideoScriptPack.model_validate(_stage_output_optional(db, run_id, "video_scripting"))
    selected = [item for item in scripts.scripts if item.variant_id == variant_id]
    if not selected:
        raise ValueError(f"video script for {variant_id} is required before regeneration")
    return VideoScriptPack(scripts=selected)


def _latest_video_payload_for_variant(db: Session, run_id: str, variant_id: str) -> dict | None:
    assets = db.scalars(
        select(VariantAsset).where(
            VariantAsset.run_id == run_id,
            VariantAsset.stage_name == "video_generation",
            VariantAsset.asset_type == "video",
        )
    ).all()
    for asset in sorted(assets, key=lambda item: item.created_at, reverse=True):
        payload = asset.payload or {}
        if payload.get("variant_id") == variant_id:
            return dict(payload)
    return None


def _merge_stage_payload(stage_name: str, existing: dict, new_payload: dict) -> dict:
    existing = dict(existing or {})
    if stage_name == "copy_image_generation":
        def merge_by_variant(old_items: list[dict], new_items: list[dict]) -> list[dict]:
            merged = {str(item.get("variant_id") or ""): item for item in old_items if isinstance(item, dict)}
            for item in new_items:
                if isinstance(item, dict):
                    merged[str(item.get("variant_id") or "")] = item
            return [item for key, item in merged.items() if key]

        return {
            **existing,
            "copy_variants": merge_by_variant(existing.get("copy_variants") or [], new_payload.get("copy_variants") or []),
            "image_assets": merge_by_variant(existing.get("image_assets") or [], new_payload.get("image_assets") or []),
        }
    if stage_name == "video_scripting":
        return {**existing, "scripts": [*(existing.get("scripts") or []), *(new_payload.get("scripts") or [])]}
    if stage_name == "storyboard_image_generation":
        return {**existing, "frames": [*(existing.get("frames") or []), *(new_payload.get("frames") or [])]}
    if stage_name == "video_generation":
        return {**existing, "videos": [*(existing.get("videos") or []), *(new_payload.get("videos") or [])]}
    return {**existing, **new_payload}


def _persona_snapshots(db: Session, agent_names: list[str]) -> dict:
    snapshots: dict = {}
    for agent_name in agent_names:
        content, version, source_path = get_persona(db, agent_name)
        snapshots[agent_name] = {
            "version": version,
            "source_path": source_path,
            "content": content,
        }
    return snapshots


def _classify_failure(exc: Exception) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return TaskFailureCategory.SCHEMA_ERROR.value
    message = str(exc).lower()
    if "timeout" in message:
        return TaskFailureCategory.TIMEOUT.value
    if "compliance" in message and "block" in message:
        return TaskFailureCategory.COMPLIANCE_BLOCK.value
    if (
        "validation" in message
        or "pydantic" in message
        or "schema" in message
        or "json" in message
        or "expecting" in message
        or "missing total_score" in message
        or "missing sub-scores" in message
        or "evaluation response" in message
    ):
        return TaskFailureCategory.SCHEMA_ERROR.value
    if "provider" in message or "request failed" in message or "endpoint" in message or "api" in message:
        return TaskFailureCategory.PROVIDER_ERROR.value
    return TaskFailureCategory.UNKNOWN.value


def _variant_idempotency_key(stage_name: str, asset_type: str, variant_id: str, discriminator: str = "") -> str:
    base = f"{stage_name}:{asset_type}:{variant_id}"
    if discriminator:
        return f"{base}:{discriminator}"
    return base


def _uri_has_payload(uri: str | None, min_bytes: int = 1024) -> bool:
    if not uri:
        return False
    path = Path(uri)
    return path.exists() and path.is_file() and path.stat().st_size > min_bytes


def _generated_asset_failure(payload: dict, uri: str | None) -> tuple[str | None, str | None]:
    error = payload.get("error")
    if error:
        return TaskFailureCategory.PROVIDER_ERROR.value, str(error)
    status = str(payload.get("generation_status") or "").lower()
    if status in {"failed", "cancelled", "canceled"}:
        return TaskFailureCategory.PROVIDER_ERROR.value, f"provider task status={status}"
    if status in {"submitted", "queued", "pending", "processing", "running"}:
        return None, None
    source = str(payload.get("source") or "").lower()
    if source == "generation_error":
        return TaskFailureCategory.PROVIDER_ERROR.value, "provider returned generation error artifact"
    if source == "placeholder":
        return TaskFailureCategory.PROVIDER_ERROR.value, "provider returned placeholder media"
    if source in {"external_task_pending", "segmented_pending"}:
        return None, None
    if uri and not _uri_has_payload(uri):
        return TaskFailureCategory.PROVIDER_ERROR.value, "generated media file is empty or placeholder-sized"
    return None, None


def _copy_image_failure_lines(image_assets: list[dict]) -> list[str]:
    lines: list[str] = []
    for item in image_assets:
        if not isinstance(item, dict):
            continue
        failure_category, error_message = _generated_asset_failure(item, item.get("uri"))
        if failure_category:
            variant_id = item.get("variant_id") or "variant"
            lines.append(f"{variant_id}: {error_message or failure_category}")
    return lines


def _copy_image_asset_rank(payload: dict) -> int:
    if _generated_asset_failure(payload, payload.get("uri"))[0]:
        return 0
    if _pending_copy_image_asset(payload):
        return 1
    return 2


def _settle_copy_image_stage_status(
    db: Session,
    run: PipelineRun,
    task: StageTask,
    image_assets: list[dict],
    *,
    emit_trace: bool = True,
) -> None:
    pending = [item for item in image_assets if isinstance(item, dict) and _pending_copy_image_asset(item)]
    failures = _copy_image_failure_lines(image_assets)
    if pending:
        if task.status == TaskStatus.FAILED.value:
            task.status = TaskStatus.WAITING_REVIEW.value
            task.failure_category = None
            task.error_message = None
            run.status = RunStatus.WAITING_REVIEW.value
            run.current_stage = task.stage_name
            run.updated_at = utcnow()
        return
    if failures:
        error_message = "Copy/image generation failed assets: " + "; ".join(failures)
        changed = task.status != TaskStatus.FAILED.value or task.error_message != error_message
        task.status = TaskStatus.FAILED.value
        task.failure_category = TaskFailureCategory.PROVIDER_ERROR.value
        task.error_message = error_message
        run.status = RunStatus.FAILED.value
        run.current_stage = task.stage_name
        run.updated_at = utcnow()
        if changed and emit_trace:
            add_agent_trace_event(
                db,
                run_id=run.id,
                stage_task_id=task.id,
                stage_name=task.stage_name,
                agent_name=(task.metadata_json or {}).get("agent_name") or "copy_image_agent",
                event_type="failed",
                message=task.error_message,
                payload={"failure_category": task.failure_category, "asset_failures": failures},
            )
    elif task.status == TaskStatus.FAILED.value:
        task.status = TaskStatus.WAITING_REVIEW.value
        task.failure_category = None
        task.error_message = None
        run.status = RunStatus.WAITING_REVIEW.value
        run.current_stage = task.stage_name
        run.updated_at = utcnow()


def _refresh_completed_image_qa(
    payload: dict,
    *,
    asset_type: str,
    uri: str | None,
    expected_ratio: str | None = None,
) -> None:
    if not _uri_has_payload(uri):
        return
    payload["generation_status"] = "completed"
    if payload.get("source") == "external_task_pending":
        payload["source"] = "local_file"
    qa = payload.get("visual_qa") if isinstance(payload.get("visual_qa"), dict) else {}
    flags = [str(flag) for flag in qa.get("flags") or []]
    if qa and "visual_qa_asset_processing" not in flags:
        return
    payload["visual_qa"] = inspect_visual_asset(
        asset_type=asset_type,
        uri=uri,
        payload=payload,
        expected_ratio=expected_ratio,
    )
    if asset_type in {"image", "storyboard_frame"}:
        runtime._attach_image_asset_contract(payload)


def _upsert_run_variant(
    db: Session,
    *,
    run_id: str,
    variant_id: str,
    angle: str,
    hook: str,
    message: str,
    rationale: str = "",
    status: str | None = None,
) -> RunVariant:
    row = db.scalar(select(RunVariant).where(RunVariant.run_id == run_id, RunVariant.variant_id == variant_id))
    now = utcnow()
    if not row:
        row = RunVariant(
            run_id=run_id,
            variant_id=variant_id,
            angle=angle,
            hook=hook,
            message=message,
            status=status or VariantLifecycleStatus.DRAFT.value,
            metadata_json={"strategy_brief": {"angle": angle, "hook": hook, "message": message, "rationale": rationale}},
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
        return row
    row.angle = angle
    row.hook = hook
    row.message = message
    row.updated_at = now
    row.metadata_json = {
        **(row.metadata_json or {}),
        "strategy_brief": {"angle": angle, "hook": hook, "message": message, "rationale": rationale},
    }
    if status:
        row.status = status
    return row


def _upsert_variant_asset(
    db: Session,
    *,
    variant: RunVariant,
    run_id: str,
    stage_name: str,
    asset_type: str,
    uri: str | None,
    provider_name: str | None,
    model_name: str | None,
    prompt_summary: str | None,
    payload: dict,
    idempotency_key: str,
    failure_category: str | None = None,
    error_message: str | None = None,
) -> VariantAsset:
    payload = dict(payload or {})
    creative_key = canonical_creative_key(run_id, variant.variant_id, asset_type)
    payload["creative_key"] = creative_key
    payload["canonical_creative_key"] = creative_key
    if asset_type in {"image", "storyboard_frame", "video"}:
        expected_ratio = None
        if asset_type == "video":
            expected_ratio = payload.get("output_ratio") or payload.get("aspect_ratio")
        else:
            expected_ratio = payload.get("aspect_ratio")
        payload["visual_qa"] = inspect_visual_asset(
            asset_type=asset_type,
            uri=uri,
            payload=payload,
            expected_ratio=expected_ratio,
        )
        if asset_type in {"image", "storyboard_frame"}:
            runtime._attach_image_asset_contract(payload)
    row = db.scalar(
        select(VariantAsset).where(
            VariantAsset.run_variant_id == variant.id,
            VariantAsset.idempotency_key == idempotency_key,
        )
    )
    if not row:
        row = VariantAsset(
            run_variant_id=variant.id,
            run_id=run_id,
            stage_name=stage_name,
            asset_type=asset_type,
            uri=uri,
            provider_name=provider_name,
            model_name=model_name,
            prompt_summary=prompt_summary,
            failure_category=failure_category,
            error_message=error_message,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        db.add(row)
        db.flush()
        return row
    row.uri = uri
    row.provider_name = provider_name
    row.model_name = model_name
    row.prompt_summary = prompt_summary
    row.failure_category = failure_category
    row.error_message = error_message
    row.payload = payload
    return row


def _upsert_variant_score(
    db: Session,
    *,
    variant: RunVariant,
    run_id: str,
    stage_name: str,
    score_type: str,
    total_score: float | None,
    compliance_level: str | None,
    recommended_action: str | None,
    sub_scores: dict,
    reasons: list[str],
    forecast: dict,
    payload: dict,
) -> VariantScore:
    row = db.scalar(
        select(VariantScore).where(
            VariantScore.run_variant_id == variant.id,
            VariantScore.score_type == score_type,
        )
    )
    if not row:
        row = VariantScore(
            run_variant_id=variant.id,
            run_id=run_id,
            stage_name=stage_name,
            score_type=score_type,
            total_score=total_score,
            compliance_level=compliance_level,
            recommended_action=recommended_action,
            sub_scores=sub_scores,
            reasons=reasons,
            forecast=forecast,
            payload=payload,
        )
        db.add(row)
        db.flush()
        return row
    row.stage_name = stage_name
    row.total_score = total_score
    row.compliance_level = compliance_level
    row.recommended_action = recommended_action
    row.sub_scores = sub_scores
    row.reasons = reasons
    row.forecast = forecast
    row.payload = payload
    return row


def _scoped_idempotency_key(
    stage_name: str,
    asset_type: str,
    variant_id: str,
    discriminator: str = "",
    scope: str | None = None,
) -> str:
    scoped_discriminator = ":".join(part for part in [scope, discriminator] if part)
    return _variant_idempotency_key(stage_name, asset_type, variant_id, scoped_discriminator)


def _variant_library_sync(
    db: Session,
    run: PipelineRun,
    task: StageTask,
    output_payload: dict,
    *,
    idempotency_scope: str | None = None,
) -> None:
    stage_name = task.stage_name
    if stage_name == "divergence":
        for item in output_payload.get("variants", []):
            _upsert_run_variant(
                db,
                run_id=run.id,
                variant_id=item.get("variant_id", ""),
                angle=item.get("angle", ""),
                hook=item.get("hook", ""),
                message=item.get("message", ""),
                rationale=item.get("rationale", ""),
                status=VariantLifecycleStatus.DRAFT.value,
            )
        return

    if stage_name == "copy_image_generation":
        copy_variants = {item.get("variant_id"): item for item in output_payload.get("copy_variants", [])}
        image_assets = [item for item in output_payload.get("image_assets", []) if item.get("variant_id")]
        for variant_id, copy_payload in copy_variants.items():
            variant = get_run_variant(db, run.id, variant_id)
            _upsert_variant_asset(
                db,
                variant=variant,
                run_id=run.id,
                stage_name=stage_name,
                asset_type="copy",
                uri=None,
                provider_name=task.metadata_json.get("resolved_api", {}).get("provider_name"),
                model_name=task.metadata_json.get("resolved_api", {}).get("model_name"),
                prompt_summary=(copy_payload.get("headline") or copy_payload.get("primary_text") or "")[:240],
                payload=copy_payload,
                idempotency_key=_scoped_idempotency_key(stage_name, "copy", variant_id, scope=idempotency_scope),
            )
            variant.status = VariantLifecycleStatus.GENERATED.value
        for image_payload in image_assets:
            variant = get_run_variant(db, run.id, image_payload["variant_id"])
            failure_category, error_message = _generated_asset_failure(image_payload, image_payload.get("uri"))
            _upsert_variant_asset(
                db,
                variant=variant,
                run_id=run.id,
                stage_name=stage_name,
                asset_type="image",
                uri=image_payload.get("uri"),
                provider_name=task.metadata_json.get("resolved_api", {}).get("image_provider_name"),
                model_name=task.metadata_json.get("resolved_api", {}).get("image_model_name"),
                prompt_summary=(image_payload.get("prompt") or "")[:240],
                payload=image_payload,
                idempotency_key=_scoped_idempotency_key(stage_name, "image", image_payload["variant_id"], scope=idempotency_scope),
                failure_category=failure_category,
                error_message=error_message,
            )
        return

    if stage_name == "video_scripting":
        for script_payload in output_payload.get("scripts", []):
            variant = get_run_variant(db, run.id, script_payload.get("variant_id", ""))
            _upsert_variant_asset(
                db,
                variant=variant,
                run_id=run.id,
                stage_name=stage_name,
                asset_type="video_script",
                uri=None,
                provider_name=task.metadata_json.get("resolved_api", {}).get("provider_name"),
                model_name=task.metadata_json.get("resolved_api", {}).get("model_name"),
                prompt_summary=(script_payload.get("hook") or "")[:240],
                payload=script_payload,
                idempotency_key=_scoped_idempotency_key(stage_name, "video_script", script_payload.get("variant_id", ""), scope=idempotency_scope),
            )
            variant.status = VariantLifecycleStatus.GENERATED.value
        return

    if stage_name == "storyboard_image_generation":
        for frame_payload in output_payload.get("frames", []):
            variant = get_run_variant(db, run.id, frame_payload.get("variant_id", ""))
            failure_category = TaskFailureCategory.PROVIDER_ERROR.value if frame_payload.get("error") else None
            _upsert_variant_asset(
                db,
                variant=variant,
                run_id=run.id,
                stage_name=stage_name,
                asset_type="storyboard_frame",
                uri=frame_payload.get("image_uri"),
                provider_name=frame_payload.get("image_provider") or task.metadata_json.get("resolved_api", {}).get("provider_name"),
                model_name=frame_payload.get("image_model") or task.metadata_json.get("resolved_api", {}).get("model_name"),
                prompt_summary=(frame_payload.get("prompt") or "")[:240],
                payload=frame_payload,
                failure_category=failure_category,
                error_message=frame_payload.get("error"),
                idempotency_key=_scoped_idempotency_key(
                    stage_name,
                    "storyboard_frame",
                    frame_payload.get("variant_id", ""),
                    frame_payload.get("frame_id", ""),
                    idempotency_scope,
                ),
            )
        return

    if stage_name == "video_generation":
        for video_payload in output_payload.get("videos", []):
            variant = get_run_variant(db, run.id, video_payload.get("variant_id", ""))
            failure_category, error_message = _generated_asset_failure(video_payload, video_payload.get("video_uri"))
            _upsert_variant_asset(
                db,
                variant=variant,
                run_id=run.id,
                stage_name=stage_name,
                asset_type="video",
                uri=video_payload.get("video_uri"),
                provider_name=task.metadata_json.get("resolved_api", {}).get("video_provider_name"),
                model_name=task.metadata_json.get("resolved_api", {}).get("video_model_name"),
                prompt_summary=f"video asset for {video_payload.get('variant_id', '')}",
                payload=video_payload,
                idempotency_key=_scoped_idempotency_key(stage_name, "video", video_payload.get("variant_id", ""), scope=idempotency_scope),
                failure_category=failure_category,
                error_message=error_message,
            )
            variant.status = VariantLifecycleStatus.GENERATED.value
        return

    if stage_name == "visual_quality_assessment":
        reports = {
            item.get("variant_id"): item
            for item in output_payload.get("reports", [])
            if isinstance(item, dict) and item.get("variant_id")
        }
        for summary in output_payload.get("variant_summaries", []):
            variant = get_run_variant(db, run.id, summary.get("variant_id", ""))
            report = reports.get(summary.get("variant_id")) or {}
            _upsert_variant_score(
                db,
                variant=variant,
                run_id=run.id,
                stage_name=stage_name,
                score_type="visual_quality",
                total_score=summary.get("visual_score"),
                compliance_level=summary.get("qa_status"),
                recommended_action=summary.get("recommended_action"),
                sub_scores={
                    "visual_score": summary.get("visual_score"),
                    "blocking_issue_count": summary.get("blocking_issue_count", 0),
                },
                reasons=summary.get("issues") or report.get("blocking_issues") or [],
                forecast={},
                payload=report or summary,
            )
            variant.metadata_json = {
                **(variant.metadata_json or {}),
                "visual_quality": report or summary,
            }
            if summary.get("recommended_action") == "request_regeneration":
                variant.regenerate_requested = True
                variant.status = VariantLifecycleStatus.NEEDS_REGENERATION.value
            if summary.get("recommended_action") in {"manual_review", "wait_for_asset", "request_regeneration"}:
                variant.review_status = summary.get("recommended_action")
            if summary.get("export_ready"):
                variant.shortlisted = True
                variant.review_status = "export_ready"
                if variant.status in {VariantLifecycleStatus.DRAFT.value, VariantLifecycleStatus.GENERATED.value}:
                    variant.status = VariantLifecycleStatus.SHORTLISTED.value
        return

    if stage_name == "evaluation_selection":
        evaluation = (output_payload.get("evaluation_result") or {})
        forecast = evaluation.get("forecast") or {}
        ranked = evaluation.get("ranked_variants") or []
        winner = ((output_payload.get("selected_deliverables") or {}).get("winner_variant_id")) or None
        manual_winner = db.scalar(
            select(RunVariant).where(
                RunVariant.run_id == run.id,
                RunVariant.is_winner.is_(True),
                RunVariant.review_status == "winner",
            )
        )
        if manual_winner:
            winner = manual_winner.variant_id
        for ranked_payload in ranked:
            variant = get_run_variant(db, run.id, ranked_payload.get("variant_id", ""))
            _upsert_variant_score(
                db,
                variant=variant,
                run_id=run.id,
                stage_name=stage_name,
                score_type="evaluation",
                total_score=ranked_payload.get("total_score"),
                compliance_level=ranked_payload.get("compliance_level"),
                recommended_action=ranked_payload.get("recommended_action"),
                sub_scores=ranked_payload.get("sub_scores") or {},
                reasons=ranked_payload.get("reasons") or [],
                forecast=forecast,
                payload=ranked_payload,
            )
            _upsert_variant_score(
                db,
                variant=variant,
                run_id=run.id,
                stage_name=stage_name,
                score_type="compliance",
                total_score=None,
                compliance_level=ranked_payload.get("compliance_level"),
                recommended_action=ranked_payload.get("recommended_action"),
                sub_scores={},
                reasons=ranked_payload.get("compliance_reasons") or [],
                forecast={},
                payload={
                    "block": ranked_payload.get("compliance_block") or {},
                    "risks": ranked_payload.get("compliance_risks") or [],
                    "reasons": ranked_payload.get("compliance_reasons") or [],
                },
            )
            variant.current_score = ranked_payload.get("total_score")
            variant.metadata_json = {
                **(variant.metadata_json or {}),
                "evaluation": ranked_payload,
            }
            variant.is_winner = variant.variant_id == winner
            variant.shortlisted = variant.variant_id in {item.get("variant_id") for item in evaluation.get("top_k", [])}
            variant.review_status = ranked_payload.get("recommended_action")
            variant.regenerate_requested = ranked_payload.get("recommended_action") == "iterate_new_variants"
            variant.status = (
                VariantLifecycleStatus.WINNER.value
                if variant.is_winner
                else VariantLifecycleStatus.SHORTLISTED.value
                if variant.shortlisted
                else variant.status
            )
        return


def execute_next_queued_stage(db: Session) -> StageTask | None:
    """Backward-compatible wrapper: claim and execute one queued task.

    For concurrent worker use, prefer calling select_next_queued_task() +
    execute_stage_task() separately so the worker controls session lifecycle.
    """
    task = select_next_queued_task(db)
    if not task:
        return None
    run = get_run(db, task.run_id)
    task.input_payload = _build_task_input(db, run, task)
    task.attempt = (task.attempt or 0) + 1
    run.status = RunStatus.RUNNING.value
    run.current_stage = task.stage_name
    run.updated_at = utcnow()
    db.flush()
    execute_stage_task(db, task, run)
    return task


def execute_stage_task(db: Session, task: StageTask, run: PipelineRun) -> None:
    """Execute a single stage task that has already been claimed (status=RUNNING).

    All outcomes are persisted to db — this function should not raise.
    On success: sets task.status=WAITING_REVIEW, advances run state.
    On retryable failure: sets task.status=QUEUED with retry_at backoff.
    On permanent failure: sets task.status=FAILED, run.status=FAILED.
    """
    try:
        stage_contract = get_stage_contract(task.stage_name)
        lead_agent = stage_agent(task.stage_name)
        collaborators = list(stage_collaborators(task.stage_name))
        persona_snapshots = _persona_snapshots(db, [lead_agent, *collaborators])
        compiled_persona = build_compiled_persona(
            persona_snapshots=persona_snapshots,
            lead_agent=lead_agent,
            collaborators=collaborators,
        )
        resolved = resolve_agent_config(
            db,
            agent_name=lead_agent,
            run_provider=run.model_provider,
            run_model=run.model_name,
        )
        runtime_config = resolve_agent_runtime(resolved)
        provider_name = resolved["provider_name"]
        model_name = resolved["model_name"]
        task.metadata_json = {
            **(task.metadata_json or {}),
            "agent_name": lead_agent,
            "collaborators": collaborators,
            "stage_contract_version": STAGE_CONTRACT_VERSION,
            "stage_contract": stage_contract.as_dict(),
            "resolved_api": resolved,
            "persona_snapshots": persona_snapshots,
            "compiled_persona": compiled_persona,
        }
        task.metadata_json.pop("waiting_state", None)
        add_agent_trace_event(
            db,
            run_id=run.id,
            stage_task_id=task.id,
            stage_name=task.stage_name,
            agent_name=lead_agent,
            event_type="started",
            message=f"{lead_agent} started {task.stage_name}.",
            provider_name=provider_name,
            model_name=model_name,
            payload={
                "attempt": task.attempt,
                "collaborators": collaborators,
                "stage_contract_version": STAGE_CONTRACT_VERSION,
                "stage_contract": stage_contract.as_dict(),
            },
        )
        add_agent_trace_event(
            db,
            run_id=run.id,
            stage_task_id=task.id,
            stage_name=task.stage_name,
            agent_name=lead_agent,
            event_type="input_summary",
            message=f"{lead_agent} received stage inputs.",
            provider_name=provider_name,
            model_name=model_name,
            payload={"input_shape": _payload_shape(task.input_payload)},
        )

        def trace_model_event(event_type: str, message: str, payload: dict | None = None) -> None:
            payload = payload or {}
            add_agent_trace_event(
                db,
                run_id=run.id,
                stage_task_id=task.id,
                stage_name=task.stage_name,
                agent_name=lead_agent,
                event_type=event_type,
                message=_truncate_text(message, 500),
                provider_name=payload.get("selected_provider_name") or provider_name,
                model_name=payload.get("selected_model_name") or model_name,
                payload=payload,
            )
            db.flush()

        runtime_config = {
            **runtime_config,
            "trace_callback": trace_model_event,
            "compiled_persona": compiled_persona,
        }

        output = execute_runtime_stage(
            StageExecutionContext(
                db=db,
                run=run,
                task=task,
                runtime=runtime,
                resolved=resolved,
                runtime_config=runtime_config,
                provider_name=provider_name,
                model_name=model_name,
                lead_agent=lead_agent,
                emit_trace=trace_model_event,
                variant_library_sync=_variant_library_sync,
                get_stage_output=_get_stage_output,
                gm_memory_trace_payload=_gm_memory_trace_payload,
                utcnow=utcnow,
            )
        )

        task.output_payload = output.payload
        task.model_used = output.model_used
        task.completed_at = utcnow()
        task.failure_category = None
        task.error_message = None
        task.retry_at = None
        run.budget_used = float(run.budget_used or 0.0) + output.estimated_cost
        run.updated_at = utcnow()

        for artifact in output.artifacts:
            if task.stage_name == "video_generation" and artifact["type"] == "generated_video":
                continue
            db.add(
                Artifact(
                    run_id=run.id,
                    stage_name=task.stage_name,
                    artifact_type=artifact["type"],
                    uri=artifact["uri"],
                    payload=artifact["payload"],
                )
            )

        _variant_library_sync(db, run, task, output.payload)
        write_stage_completion_memory(db, run=run, task=task)
        add_agent_trace_event(
            db,
            run_id=run.id,
            stage_task_id=task.id,
            stage_name=task.stage_name,
            agent_name=lead_agent,
            event_type="handoff",
            message=f"{lead_agent} produced a handoff payload for downstream review.",
            provider_name=provider_name,
            model_name=model_name,
            payload={"output_shape": _payload_shape(output.payload), "artifact_count": len(output.artifacts)},
        )

        if task.stage_name == "evaluation_selection" and output.scorecard and output.forecast:
            scorecard = output.scorecard
            db.add(
                ScoreCardModel(
                    run_id=run.id,
                    stage_task_id=task.id,
                    total_score=scorecard.total_score,
                    sub_scores=scorecard.sub_scores.model_dump(),
                    risk_labels=scorecard.risk_labels,
                    explanation=scorecard.explanation,
                    compliance_level=scorecard.compliance_level.value,
                    ai_artifact_score=scorecard.ai_artifact_score,
                    forecast=output.forecast.model_dump(),
                )
            )
            ranked = ((output.payload or {}).get("evaluation_result") or {}).get("ranked_variants") or []
            winner = ((output.payload or {}).get("selected_deliverables") or {}).get("winner_variant_id")
            add_agent_trace_event(
                db,
                run_id=run.id,
                stage_task_id=task.id,
                stage_name=task.stage_name,
                agent_name=lead_agent,
                event_type="decision",
                message=f"{lead_agent} ranked variants and recommended winner {winner or '-'}.",
                provider_name=provider_name,
                model_name=model_name,
                payload={
                    "winner_variant_id": winner,
                    "ranked_count": len(ranked),
                    "top_variants": ranked[:3],
                    "score": output.scorecard.total_score,
                    "recommended_action": output.forecast.recommended_action,
                },
            )
            compile_run_outcome_reflection(db, run.id)

        task.status = TaskStatus.WAITING_REVIEW.value
        run.status = RunStatus.WAITING_REVIEW.value
        if task.stage_name == "copy_image_generation":
            _settle_copy_image_stage_status(
                db,
                run,
                task,
                [item for item in (task.output_payload or {}).get("image_assets", []) or [] if isinstance(item, dict)],
                emit_trace=False,
            )
        add_agent_trace_event(
            db,
            run_id=run.id,
            stage_task_id=task.id,
            stage_name=task.stage_name,
            agent_name=lead_agent,
            event_type="completed" if task.status == TaskStatus.WAITING_REVIEW.value else "failed",
            message=(
                f"{lead_agent} completed {task.stage_name}; waiting for human review."
                if task.status == TaskStatus.WAITING_REVIEW.value
                else (task.error_message or f"{lead_agent} failed {task.stage_name}.")
            ),
            provider_name=provider_name,
            model_name=model_name,
            payload={"model_used": output.model_used, "estimated_cost": output.estimated_cost},
        )
        db.flush()
    except Exception as exc:  # pragma: no cover
        from app.core.config import get_settings

        category = _classify_failure(exc)
        task.error_message = str(exc)
        task.failure_category = category
        provider_errors = getattr(exc, "errors", None)
        if provider_errors:
            task.metadata_json = {**(task.metadata_json or {}), "provider_errors": provider_errors}
        task.completed_at = utcnow()

        settings = get_settings()
        is_retryable = category in RETRYABLE_FAILURES
        max_attempts = max(int(task.max_retries or 0), settings.max_stage_retries)
        under_max = (task.attempt or 0) < max_attempts

        if is_retryable and under_max:
            delay = _retry_delay(task.attempt or 1)
            task.status = TaskStatus.QUEUED.value
            task.retry_at = utcnow() + timedelta(seconds=delay)
            task.priority = max(0, (task.priority or 2) - 1)
            task.max_retries = max_attempts
            task.metadata_json = {
                **(task.metadata_json or {}),
                "waiting_state": {
                    "status": "waiting_retry",
                    "retry_at": task.retry_at.isoformat(),
                    "retry_delay_seconds": delay,
                    "next_attempt": (task.attempt or 0) + 1,
                    "max_attempts": max_attempts,
                    "failure_category": category,
                },
            }
            logger.warning(
                "Task %s (%s) attempt %s failed (%s); retry scheduled in %ss",
                task.id, task.stage_name, task.attempt, category, delay,
            )
            add_agent_trace_event(
                db,
                run_id=run.id,
                stage_task_id=task.id,
                stage_name=task.stage_name,
                agent_name=(task.metadata_json or {}).get("agent_name") or stage_agent(task.stage_name),
                event_type="retry_scheduled",
                message=f"{task.stage_name} attempt {task.attempt} failed ({category}); retry in {delay:.0f}s.",
                provider_name=((task.metadata_json or {}).get("resolved_api") or {}).get("provider_name"),
                model_name=((task.metadata_json or {}).get("resolved_api") or {}).get("model_name"),
                payload={
                    "failure_category": category,
                    "retry_at": task.retry_at.isoformat(),
                    "retry_delay_seconds": delay,
                    "attempt": task.attempt,
                },
            )
            run.status = RunStatus.RUNNING.value
        else:
            task.status = TaskStatus.FAILED.value
            run.status = RunStatus.FAILED.value
            add_agent_trace_event(
                db,
                run_id=run.id,
                stage_task_id=task.id,
                stage_name=task.stage_name,
                agent_name=(task.metadata_json or {}).get("agent_name") or stage_agent(task.stage_name),
                event_type="failed",
                message=f"{task.stage_name} failed: {_truncate_text(str(exc), 240)}",
                provider_name=((task.metadata_json or {}).get("resolved_api") or {}).get("provider_name"),
                model_name=((task.metadata_json or {}).get("resolved_api") or {}).get("model_name"),
                payload={"failure_category": category, "provider_errors": provider_errors or []},
            )

        run.updated_at = utcnow()
        db.flush()


def _variant_summary(db: Session, run_id: str) -> dict:
    rows = db.scalars(select(RunVariant).where(RunVariant.run_id == run_id)).all()
    counter = Counter(row.status for row in rows)
    return {
        "total": len(rows),
        "winner_count": sum(1 for row in rows if row.is_winner),
        "shortlisted_count": sum(1 for row in rows if row.shortlisted),
        "regeneration_requested_count": sum(1 for row in rows if row.regenerate_requested),
        "status_counts": dict(counter),
    }


def _asset_payload_status(asset: VariantAsset) -> str:
    payload = asset.payload or {}
    if asset.failure_category:
        return "failed"
    status = str(payload.get("generation_status") or payload.get("status") or "").lower()
    if status in {"submitted", "queued", "pending", "processing", "running"}:
        return "processing"
    if status in {"completed", "succeeded", "success", "ready"}:
        return "completed"
    if asset.uri:
        return "completed"
    return "missing"


def _uri_file_issue(uri: str | None, *, minimum_bytes: int = 1) -> str | None:
    if not uri:
        return "missing_uri"
    if uri.startswith(("http://", "https://")):
        return None
    path = Path(uri)
    if not path.exists():
        return "missing_file"
    if path.is_file() and path.stat().st_size < minimum_bytes:
        return "empty_file"
    return None


def _required_asset_types_for_run(run: PipelineRun | None) -> set[str]:
    if not run:
        return set()
    mode = run.pipeline_mode
    if mode == "copy_image_only":
        return {"copy", "image"}
    if mode == "video_only":
        return {"video_script", "storyboard_frame", "video"}
    return {"copy", "image", "video_script", "storyboard_frame", "video"}


def _latest_score(scores: list[VariantScore], score_type: str) -> VariantScore | None:
    rows = [score for score in scores if score.score_type == score_type]
    return rows[-1] if rows else None


def _variant_quality_summary(
    *,
    run: PipelineRun | None,
    row: RunVariant,
    assets: list[VariantAsset],
    reviews: list[VariantReview],
    scores: list[VariantScore],
) -> dict:
    review_hints = get_dtc_site_review_hints(run.creative_specs if run else None)
    asset_counts = Counter(asset.asset_type for asset in assets)
    asset_status_counts = Counter(_asset_payload_status(asset) for asset in assets)
    required_asset_types = _required_asset_types_for_run(run)
    missing_required_assets = sorted(asset_type for asset_type in required_asset_types if not asset_counts.get(asset_type))
    media_issues: list[dict] = []
    visual_qa_flags: list[str] = []
    visual_qa_scores: list[float] = []
    frame_review_flags: list[str] = []
    reference_source_count = 0
    for asset in assets:
        if asset.asset_type == "image":
            issue = _uri_file_issue(asset.uri)
        elif asset.asset_type == "video":
            issue = _uri_file_issue(asset.uri, minimum_bytes=1024) if _asset_payload_status(asset) == "completed" else None
        elif asset.asset_type == "storyboard_frame":
            issue = _uri_file_issue(asset.uri)
        else:
            issue = None
        if issue:
            media_issues.append({"asset_id": asset.id, "asset_type": asset.asset_type, "issue": issue, "uri": asset.uri})
        visual_qa = (asset.payload or {}).get("visual_qa") or {}
        try:
            reference_source_count = max(
                reference_source_count,
                int((asset.payload or {}).get("reference_source_count") or 0),
            )
        except (TypeError, ValueError):
            pass
        for flag in visual_qa.get("flags") or []:
            visual_qa_flags.append(str(flag))
            if "frame" in str(flag):
                frame_review_flags.append(str(flag))
        if isinstance(visual_qa.get("score"), (int, float)):
            visual_qa_scores.append(float(visual_qa["score"]))
        if visual_qa.get("status") == "fail":
            media_issues.append({"asset_id": asset.id, "asset_type": asset.asset_type, "issue": "visual_qa_failed", "uri": asset.uri})
        marketplace_qa = (asset.payload or {}).get("marketplace_qa") or {}
        for flag in marketplace_qa.get("flags") or []:
            visual_qa_flags.append(str(flag))
        if isinstance(marketplace_qa.get("score"), (int, float)):
            visual_qa_scores.append(float(marketplace_qa["score"]))
        if marketplace_qa.get("status") == "fail":
            media_issues.append({"asset_id": asset.id, "asset_type": asset.asset_type, "issue": "marketplace_qa_failed", "uri": asset.uri})

    evaluation = _latest_score(scores, "evaluation")
    compliance = _latest_score(scores, "compliance")
    visual_quality = _latest_score(scores, "visual_quality")
    if visual_quality:
        payload = visual_quality.payload or {}
        for asset_report in payload.get("asset_reports") or []:
            if not isinstance(asset_report, dict):
                continue
            for flag in asset_report.get("flags") or []:
                if "frame" in str(flag):
                    frame_review_flags.append(str(flag))
    compliance_level = (compliance.compliance_level if compliance else None) or (evaluation.compliance_level if evaluation else None)
    score = row.current_score if row.current_score is not None else (evaluation.total_score if evaluation else None)
    operator_tags = sorted({tag for review in reviews for tag in (review.tags or [])})
    quality_flags: list[str] = []
    if row.is_winner:
        quality_flags.append("winner")
    if row.review_status == "export_ready":
        quality_flags.append("export_ready")
    if row.shortlisted:
        quality_flags.append("shortlisted")
    if row.regenerate_requested or row.status == VariantLifecycleStatus.NEEDS_REGENERATION.value:
        quality_flags.append("needs_regeneration")
    if row.status == VariantLifecycleStatus.REJECTED.value or row.review_status == "rejected":
        quality_flags.append("rejected")
    if missing_required_assets:
        quality_flags.append("missing_assets")
    if media_issues:
        quality_flags.append("media_issue")
    if visual_quality and visual_quality.recommended_action in {"manual_review", "wait_for_asset"}:
        quality_flags.append("visual_qa_attention")
    if visual_quality and visual_quality.recommended_action == "request_regeneration":
        quality_flags.append("visual_qa_failed")
    if visual_quality and visual_quality.compliance_level in {"fail", "pending", "warn"}:
        quality_flags.append(f"visual_qa_{visual_quality.compliance_level}")
    if visual_qa_flags:
        quality_flags.extend(visual_qa_flags)
    if any(flag in visual_qa_flags for flag in {"visual_qa_needs_frame_review", "visual_qa_remote_unchecked"}):
        quality_flags.append("visual_qa_attention")
    if any(flag in visual_qa_flags for flag in {"visual_qa_placeholder", "visual_qa_empty_video", "visual_qa_decode_error"}):
        quality_flags.append("visual_qa_failed")
    if any(str(flag).startswith("marketplace_") or flag == "product_fill_low" for flag in visual_qa_flags):
        quality_flags.append("marketplace_attention")
    if any(flag in visual_qa_flags for flag in {"marketplace_placeholder", "marketplace_background_not_white", "marketplace_missing_reference", "marketplace_resolution_low"}):
        quality_flags.append("marketplace_failed")
    if asset_status_counts.get("processing"):
        quality_flags.append("processing_assets")
    if asset_status_counts.get("failed"):
        quality_flags.append("failed_assets")
    if compliance_level and str(compliance_level).lower() not in {"pass", "passed", "ok", "low", "none", "safe"}:
        quality_flags.append("compliance_attention")
    if score is not None and score < 70:
        quality_flags.append("low_score")
    if any("error" in tag or "issue" in tag or "fail" in tag for tag in operator_tags):
        quality_flags.append("operator_quality_issue")
    if row.review_status in {None, "", "promote_winner", "shortlist", "needs_review"} and not row.is_winner:
        quality_flags.append("pending_review")
    if not any(flag in quality_flags for flag in ["missing_assets", "media_issue", "processing_assets", "failed_assets", "compliance_attention", "needs_regeneration", "rejected"]):
        quality_flags.append("ready_to_review")

    if "failed_assets" in quality_flags or "media_issue" in quality_flags or "operator_quality_issue" in quality_flags or "visual_qa_failed" in quality_flags or "marketplace_failed" in quality_flags:
        quality_status = "asset_issue"
    elif "export_ready" in quality_flags:
        quality_status = "export_ready"
    elif "processing_assets" in quality_flags:
        quality_status = "processing"
    elif "needs_regeneration" in quality_flags:
        quality_status = "needs_regeneration"
    elif "compliance_attention" in quality_flags:
        quality_status = "compliance_attention"
    elif "missing_assets" in quality_flags:
        quality_status = "incomplete"
    elif row.is_winner:
        quality_status = "winner"
    elif row.shortlisted:
        quality_status = "shortlisted"
    else:
        quality_status = "ready"

    return {
        "quality_status": quality_status,
        "quality_flags": sorted(set(quality_flags)),
        "review_hints": review_hints,
        "asset_counts": dict(asset_counts),
        "asset_status_counts": dict(asset_status_counts),
        "required_asset_types": sorted(required_asset_types),
        "missing_required_assets": missing_required_assets,
        "media_issues": media_issues,
        "visual_qa_flags": sorted(set(visual_qa_flags)),
        "frame_review_flags": sorted(set(frame_review_flags)),
        "reference_source_count": reference_source_count,
        "visual_qa_min_score": min(visual_qa_scores) if visual_qa_scores else None,
        "visual_quality_score": visual_quality.total_score if visual_quality else None,
        "visual_quality_status": visual_quality.compliance_level if visual_quality else None,
        "visual_quality_recommended_action": visual_quality.recommended_action if visual_quality else None,
        "score": score,
        "compliance_level": compliance_level,
        "review_status": row.review_status,
        "operator_tags": operator_tags,
    }


def _variant_matches_filters(
    item: dict,
    *,
    status: str | None = None,
    review_status: str | None = None,
    quality: str | None = None,
    asset_type: str | None = None,
    generation_status: str | None = None,
    compliance: str | None = None,
    min_score: float | None = None,
    q: str | None = None,
) -> bool:
    quality_summary = item.get("quality_summary") or {}
    if status and item.get("status") != status:
        return False
    if review_status and item.get("review_status") != review_status:
        return False
    if quality and quality not in set(quality_summary.get("quality_flags") or []) | {quality_summary.get("quality_status")}:
        return False
    if asset_type and not (quality_summary.get("asset_counts") or {}).get(asset_type):
        return False
    if generation_status and not (quality_summary.get("asset_status_counts") or {}).get(generation_status):
        return False
    if compliance and str(quality_summary.get("compliance_level") or "").lower() != compliance.lower():
        return False
    if min_score is not None:
        score = quality_summary.get("score")
        if score is None or float(score) < min_score:
            return False
    if q:
        haystack = " ".join(
            str(value or "")
            for value in [
                item.get("variant_id"),
                item.get("angle"),
                item.get("hook"),
                item.get("message"),
                (item.get("strategy_brief") or {}).get("rationale"),
            ]
        ).lower()
        if q.lower() not in haystack:
            return False
    return True


def _serialize_run_variant(db: Session, row: RunVariant, run: PipelineRun | None = None) -> dict:
    assets = db.scalars(select(VariantAsset).where(VariantAsset.run_variant_id == row.id).order_by(VariantAsset.created_at.asc())).all()
    reviews = db.scalars(select(VariantReview).where(VariantReview.run_variant_id == row.id).order_by(VariantReview.created_at.asc())).all()
    scores = db.scalars(select(VariantScore).where(VariantScore.run_variant_id == row.id).order_by(VariantScore.created_at.asc())).all()
    quality_summary = _variant_quality_summary(run=run, row=row, assets=assets, reviews=reviews, scores=scores)
    return {
        "id": row.id,
        "run_id": row.run_id,
        "variant_id": row.variant_id,
        "angle": row.angle,
        "hook": row.hook,
        "message": row.message,
        "status": row.status,
        "current_score": row.current_score,
        "is_winner": row.is_winner,
        "shortlisted": row.shortlisted,
        "review_status": row.review_status,
        "regenerate_requested": row.regenerate_requested,
        "metadata_json": row.metadata_json or {},
        "strategy_brief": (row.metadata_json or {}).get("strategy_brief", {}),
        "execution_summary": build_variant_execution_summary(db, row.run_id, row.variant_id),
        "quality_summary": quality_summary,
        "assets": [
            {
                "id": asset.id,
                "stage_name": asset.stage_name,
                "asset_type": asset.asset_type,
                "uri": asset.uri,
                "provider_name": asset.provider_name,
                "model_name": asset.model_name,
                "prompt_summary": asset.prompt_summary,
                "failure_category": asset.failure_category,
                "error_message": asset.error_message,
                "payload": asset.payload or {},
                "created_at": asset.created_at,
            }
            for asset in assets
        ],
        "scores": [
            {
                "id": score.id,
                "stage_name": score.stage_name,
                "score_type": score.score_type,
                "total_score": score.total_score,
                "compliance_level": score.compliance_level,
                "recommended_action": score.recommended_action,
                "sub_scores": score.sub_scores or {},
                "reasons": score.reasons or [],
                "forecast": score.forecast or {},
                "payload": score.payload or {},
                "created_at": score.created_at,
            }
            for score in scores
        ],
        "reviews": [
            {
                "id": review.id,
                "action": review.action,
                "comment": review.comment,
                "tags": review.tags or [],
                "metadata_json": review.metadata_json or {},
                "created_at": review.created_at,
            }
            for review in reviews
        ],
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def run_deliverables(db: Session, run_id: str) -> dict:
    winner = db.scalar(
        select(RunVariant)
        .where(RunVariant.run_id == run_id, RunVariant.is_winner.is_(True))
        .order_by(desc(RunVariant.updated_at))
        .limit(1)
    )
    if not winner:
        eval_task = get_stage_task(db, run_id, "evaluation_selection")
        payload = eval_task.output_payload or {}
        return payload.get("selected_deliverables", {})
    assets = db.scalars(select(VariantAsset).where(VariantAsset.run_variant_id == winner.id).order_by(VariantAsset.created_at.asc())).all()
    scores = db.scalars(select(VariantScore).where(VariantScore.run_variant_id == winner.id)).all()
    copy_asset = next((item for item in assets if item.asset_type == "copy"), None)
    usable_assets = [item for item in assets if not (item.failure_category or _generated_asset_failure(item.payload or {}, item.uri)[0])]
    image_assets = [item.payload for item in usable_assets if item.asset_type == "image"]
    video_asset = next((item.payload for item in usable_assets if item.asset_type == "video"), None)
    evaluation = next((item for item in scores if item.score_type == "evaluation"), None)
    return {
        "winner_variant_id": winner.variant_id,
        "copy_variant": (copy_asset.payload if copy_asset else None),
        "image_assets": image_assets,
        "video_asset": video_asset,
        "reasoning": evaluation.reasons if evaluation else [],
    }


def run_variants(
    db: Session,
    run_id: str,
    *,
    status: str | None = None,
    review_status: str | None = None,
    quality: str | None = None,
    asset_type: str | None = None,
    generation_status: str | None = None,
    compliance: str | None = None,
    min_score: float | None = None,
    q: str | None = None,
) -> dict:
    run = get_run(db, run_id)
    rows = db.scalars(select(RunVariant).where(RunVariant.run_id == run_id).order_by(RunVariant.variant_id.asc())).all()
    evaluation = get_stage_task(db, run_id, "evaluation_selection")
    ranked = ((evaluation.output_payload or {}).get("evaluation_result", {}) or {}).get("ranked_variants", [])
    items = [_serialize_run_variant(db, row, run) for row in rows]
    filtered_items = [
        item
        for item in items
        if _variant_matches_filters(
            item,
            status=status,
            review_status=review_status,
            quality=quality,
            asset_type=asset_type,
            generation_status=generation_status,
            compliance=compliance,
            min_score=min_score,
            q=q,
        )
    ]
    quality_counter = Counter((item.get("quality_summary") or {}).get("quality_status", "unknown") for item in items)
    flag_counter: Counter[str] = Counter()
    for item in items:
        flag_counter.update((item.get("quality_summary") or {}).get("quality_flags") or [])
    summary = {
        **_variant_summary(db, run_id),
        "filtered_count": len(filtered_items),
        "quality_status_counts": dict(quality_counter),
        "quality_flag_counts": dict(flag_counter),
    }
    return {
        "variants": [
            {
                "variant_id": row.variant_id,
                "angle": row.angle,
                "hook": row.hook,
                "message": row.message,
                "status": row.status,
            }
            for row in rows
        ],
        "ranked": ranked,
        "items": filtered_items,
        "summary": summary,
    }


def _pending_status(value: object) -> bool:
    return str(value or "").lower() in {"", "submitted", "queued", "pending", "processing", "running"}


def _external_task_elapsed_seconds(submitted_at: object, now: datetime) -> float | None:
    if not submitted_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(submitted_at))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return round(max(0.0, (now - parsed).total_seconds()), 3)


def _mark_external_task_polled(payload: dict, *, asset_type: str, status: str | None) -> dict:
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    state = dict(payload.get("external_task_state") or {})
    task_id = payload.get("external_task_id") or state.get("task_id")
    if not task_id:
        return state
    state.setdefault("asset_type", asset_type)
    state["task_id"] = task_id
    state["status"] = status or payload.get("generation_status") or state.get("status") or "pending"
    state.setdefault("submitted_at", now_iso)
    state["last_seen_at"] = now_iso
    state["last_polled_at"] = now_iso
    state["last_event"] = "polled"
    state["poll_count"] = int(state.get("poll_count") or 0) + 1
    elapsed = _external_task_elapsed_seconds(state.get("submitted_at"), now)
    if elapsed is not None:
        state["elapsed_seconds"] = elapsed
        payload["external_task_elapsed_seconds"] = elapsed
    payload["external_task_state"] = state
    payload["last_polled_at"] = now_iso
    payload["provider_status"] = state["status"]
    return state


def _trace_external_task_poll(
    db: Session,
    *,
    run_id: str,
    stage_name: str,
    agent_name: str,
    provider_name: str | None,
    model_name: str | None,
    payload: dict,
) -> None:
    state = payload.get("external_task_state") if isinstance(payload.get("external_task_state"), dict) else {}
    task_id = payload.get("external_task_id") or state.get("task_id")
    if not task_id:
        return
    stage_task = db.scalar(select(StageTask).where(StageTask.run_id == run_id, StageTask.stage_name == stage_name))
    add_agent_trace_event(
        db,
        run_id=run_id,
        stage_task_id=stage_task.id if stage_task else None,
        stage_name=stage_name,
        agent_name=agent_name,
        event_type="external_task_polled",
        message=f"External {state.get('asset_type') or 'media'} task {task_id} status: {state.get('status') or payload.get('generation_status')}.",
        provider_name=provider_name,
        model_name=model_name,
        payload={
            "external_task_id": task_id,
            "generation_status": payload.get("generation_status"),
            "provider_status": payload.get("provider_status"),
            "external_task_state": state,
        },
    )


def _pending_storyboard_frame(frame: dict) -> bool:
    for item in [frame, *((frame.get("candidate_frames") or []) if isinstance(frame.get("candidate_frames"), list) else [])]:
        if isinstance(item, dict) and item.get("external_task_id") and _pending_status(item.get("generation_status")):
            return True
    return False


def _pending_copy_image_asset(asset: dict) -> bool:
    return bool(asset.get("external_task_id") and _pending_status(asset.get("generation_status")))


def _sync_storyboard_frame_payload(frame: dict, payload: dict) -> None:
    frame_updates = dict(payload)
    frame_updates.pop("candidate_frames", None)
    frame.update(frame_updates)
    candidates = frame.get("candidate_frames")
    if not isinstance(candidates, list):
        return
    candidate_updates = dict(frame_updates)
    payload_task_id = payload.get("external_task_id")
    payload_candidate_index = payload.get("candidate_index")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        same_task = payload_task_id and candidate.get("external_task_id") == payload_task_id
        same_candidate = (
            payload_candidate_index is not None
            and candidate.get("candidate_index") == payload_candidate_index
        )
        if same_task or same_candidate:
            candidate.update(candidate_updates)


def _segment_prompt(base_prompt: str, segment: dict) -> str:
    return (
        f"{base_prompt}\n\nSegment {segment.get('segment_id')}: {segment.get('motion_prompt')}. "
        f"First frame: {segment.get('first_frame_prompt')}. Last frame target: {segment.get('last_frame_prompt')}. "
        f"Continuity constraints: {segment.get('continuity_constraints') or []}. "
        f"Segment contract: {segment.get('segment_contract') or {}}. "
        f"Transition to next: {segment.get('transition_to_next')}."
    )


def _submit_next_video_segment(
    *,
    run: PipelineRun,
    payload: dict,
    provider_name: str,
    model_name: str,
    runtime_config: dict,
    video_runtime: dict,
) -> tuple[dict | None, float]:
    queued = payload.get("segment_queue") or []
    segments = payload.get("segments") or []
    next_index = len(segments)
    if next_index >= len(queued):
        return None, 0.0
    previous = segments[-1] if segments else {}
    bridge_frame_uri = previous.get("last_frame_url") or previous.get("last_frame_uri")
    segment = queued[next_index]
    generation_spec = {
        **(payload.get("generation_spec") or {}),
        "duration": int(segment.get("duration_seconds") or 8),
        "return_last_frame": True,
    }
    base_image_refs = list((payload.get("generation_spec") or {}).get("image_urls") or [])
    try:
        max_reference_images = int(generation_spec.get("max_reference_images") or 9)
    except (TypeError, ValueError):
        max_reference_images = 9
    generation_spec.pop("image_urls", None)
    generation_spec.pop("image_with_roles", None)
    reference_payload, reference_mode, reference_manifest = runtime._segment_image_reference_payload(
        base_image_refs,
        str(bridge_frame_uri) if bridge_frame_uri else None,
        max_reference_images=max_reference_images,
        allow_data_urls=not runtime._video_runtime_requires_hosted_references(
            fallback_provider=provider_name,
            fallback_model=model_name,
            runtime_config={"video": video_runtime},
        ),
    )
    generation_spec.update(reference_payload)
    reference_instruction = ""
    if reference_mode == "first_frame":
        reference_instruction = " Reference usage: first_frame is the previous segment tail; maintain the same model and product identity."
    elif reference_mode == "tail_with_anchors":
        reference_instruction = " Reference usage: image 1 is the previous segment tail frame; remaining images are product/model anchors."
    video_prompt = _segment_prompt(str(payload.get("segment_prompt_base") or ""), segment) + reference_instruction
    video_prompt += runtime._human_integrity_instruction(video_prompt)
    segment_payload, cost, _ = runtime._generate_video_clip_payload(
        run_id=run.id,
        variant_id=str(payload.get("variant_id") or "variant"),
        video_prompt=video_prompt,
        video_size=str(payload.get("video_size") or generation_spec.get("size") or "9:16"),
        resolution=str(payload.get("resolution") or generation_spec.get("resolution") or "720p"),
        duration_seconds=int(segment.get("duration_seconds") or generation_spec.get("duration") or 8),
        generation_spec=generation_spec,
        provider=provider_name,
        model=model_name,
        runtime_config={**runtime_config, "video": video_runtime},
        video_filename=f"{segment.get('segment_id')}{payload.get('asset_suffix') or ''}.mp4",
        force_regenerate=False,
    )
    segment_payload["segment_id"] = segment.get("segment_id")
    segment_payload["segment_index"] = next_index
    segment_payload["transition_to_next"] = segment.get("transition_to_next")
    segment_payload["segment_contract"] = segment.get("segment_contract") or {}
    segment_payload["reference_mode"] = reference_mode
    segment_payload["reference_image_count"] = len(generation_spec.get("image_urls") or []) + len(generation_spec.get("image_with_roles") or [])
    segment_payload["reference_manifest"] = reference_manifest
    segment_payload["hosted_reference_ready"] = all(
        bool(item.get("provider_usable")) for item in reference_manifest if isinstance(item, dict) and item.get("used")
    )
    return segment_payload, cost


def _refresh_segmented_video_payload(
    *,
    db: Session,
    run: PipelineRun,
    payload: dict,
    provider,
    provider_name: str,
    model_name: str,
    runtime_config: dict,
    video_runtime: dict,
) -> tuple[dict, int, int]:
    refreshed = 0
    completed = 0
    segments = [dict(item) for item in payload.get("segments") or []]
    for segment in segments:
        task_id = segment.get("external_task_id")
        if not task_id or not _pending_status(segment.get("generation_status")):
            continue
        result = provider.poll_video_task(
            task_id=task_id,
            model=model_name,
            api_base_url=video_runtime.get("api_base_url") or runtime_config.get("api_base_url"),
            api_key=video_runtime.get("api_key") or runtime_config.get("api_key"),
            extra=video_runtime.get("extra") or runtime_config.get("extra"),
        )
        selected = result.videos[0] if result.videos else None
        refreshed += 1
        segment["generation_status"] = result.status or (selected.status if selected else None) or segment.get("generation_status")
        segment["raw_response"] = result.raw_response or (selected.raw_response if selected else {}) or segment.get("raw_response") or {}
        _mark_external_task_polled(segment, asset_type="video_segment", status=segment.get("generation_status"))
        _trace_external_task_poll(
            db=db,
            run_id=run.id,
            stage_name="video_generation",
            agent_name="video_generation_agent",
            provider_name=provider_name,
            model_name=model_name,
            payload=segment,
        )
        if selected and selected.url:
            segment["result_url"] = selected.url
        last_frame_url = runtime._last_frame_url_from_raw(getattr(selected, "raw_response", None), result.raw_response)
        if last_frame_url:
            segment["last_frame_url"] = last_frame_url
        if selected and (selected.url or selected.b64_data):
            video_bytes, source = runtime._materialize_generated_video(selected)
            if video_bytes:
                filename = Path(segment.get("video_uri") or f"{segment.get('segment_id')}.mp4").name
                uri = runtime.media.write_binary_artifact(run.id, filename, video_bytes)
                segment["video_uri"] = uri
                segment["source"] = source
                segment["generation_status"] = "completed"
                segment["last_frame_uri"] = extract_last_video_frame(
                    video_path=Path(uri),
                    output_path=runtime.media.settings.assets_dir / run.id / f"{segment.get('segment_id')}_last_frame.png",
                )
                runtime._attach_segment_frame_qa(run_id=run.id, segment_payload=segment)
                completed += 1
        break

    payload["segments"] = segments
    for segment in segments:
        runtime._attach_segment_frame_qa(run_id=run.id, segment_payload=segment)
    has_blocking_segment_qa = any((segment.get("segment_frame_qa") or {}).get("blocking") for segment in segments)
    if completed and not has_blocking_segment_qa:
        next_segment, _ = _submit_next_video_segment(
            run=run,
            payload=payload,
            provider_name=provider_name,
            model_name=model_name,
            runtime_config=runtime_config,
            video_runtime=video_runtime,
        )
        if next_segment:
            segments.append(next_segment)
            payload["segments"] = segments
            if next_segment.get("error"):
                payload["source"] = next_segment.get("source") or "placeholder"
                payload["generation_status"] = "failed"
                payload["error"] = next_segment.get("error")
                return payload, refreshed, completed

    queued = payload.get("segment_queue") or []
    complete_paths = [
        Path(str(segment.get("video_uri")))
        for segment in sorted(segments, key=lambda item: int(item.get("segment_index") or 0))
        if str(segment.get("generation_status") or "").lower() in {"completed", "succeeded", "success", "ready"}
        and runtime._artifact_has_payload(segment.get("video_uri"))
    ]
    stitch_segments = []
    for idx, segment in enumerate(queued):
        if not isinstance(segment, dict):
            continue
        stitch_segments.append(
            VideoSegmentPlan.model_validate(
                {
                    "segment_id": segment.get("segment_id") or f"{payload.get('variant_id') or 'variant'}_S{idx + 1}",
                    "variant_id": segment.get("variant_id") or payload.get("variant_id") or "variant",
                    "duration_seconds": segment.get("duration_seconds") or 8,
                    **segment,
                }
            )
        )
    stitch_preflight = runtime._stitch_preflight(segments=stitch_segments, segment_payloads=segments) if stitch_segments else {}
    if queued and len(complete_paths) == len(queued) and stitch_preflight.get("status") == "pass":
        stitched_uri = stitch_video_files(
            video_paths=complete_paths,
            output_path=runtime.media.settings.assets_dir / run.id / f"{payload.get('variant_id')}_stitched{payload.get('asset_suffix') or ''}.mp4",
        )
        if stitched_uri:
            payload["video_uri"] = stitched_uri
            payload["source"] = "stitched_segments"
            payload["generation_status"] = "completed"
            payload = runtime._attach_generated_video_frames(run_id=run.id, video_payload=payload)
            payload["visual_qa"] = runtime._local_media_qa(asset_type="video", uri=stitched_uri, payload=payload)
    else:
        payload["source"] = "segmented_pending"
        payload["generation_status"] = "pending"
        if segments:
            payload["video_uri"] = segments[-1].get("video_uri") or payload.get("video_uri")
    payload["stitch_preflight"] = stitch_preflight
    payload["segment_ledger"] = runtime._segment_ledger(
        variant_id=str(payload.get("variant_id") or "variant"),
        segment_queue=queued,
        segment_payloads=segments,
        stitch_preflight=stitch_preflight,
    )
    return payload, refreshed, completed


def refresh_video_task_assets(db: Session, run_id: str) -> dict:
    run = get_run(db, run_id)
    config = resolve_agent_config(
        db,
        agent_name="video_generation_agent",
        run_provider=run.model_provider,
        run_model=run.model_name,
    )
    runtime_config = resolve_agent_runtime(config)
    video_runtime = runtime_config.get("video") or {}
    provider_name = video_runtime.get("provider_name") or config.get("video_provider_name") or config.get("provider_name")
    model_name = video_runtime.get("model_name") or config.get("video_model_name") or config.get("model_name")
    provider = runtime.providers.get(provider_name)
    refreshed = 0
    completed = 0
    assets = db.scalars(
        select(VariantAsset).where(
            VariantAsset.run_id == run_id,
            VariantAsset.asset_type == "video",
        )
    ).all()
    refreshed_payloads_by_variant: dict[str, dict] = {}
    for asset in assets:
        payload = dict(asset.payload or {})
        if payload.get("segment_queue"):
            payload, segment_refreshed, segment_completed = _refresh_segmented_video_payload(
                db=db,
                run=run,
                payload=payload,
                provider=provider,
                provider_name=provider_name,
                model_name=model_name,
                runtime_config=runtime_config,
                video_runtime=video_runtime,
            )
            refreshed += segment_refreshed
            completed += segment_completed
            failure_category, error_message = _generated_asset_failure(payload, payload.get("video_uri"))
            asset.uri = payload.get("video_uri") or asset.uri
            asset.payload = payload
            asset.failure_category = failure_category
            asset.error_message = error_message
            variant_id = str(payload.get("variant_id") or asset.variant.variant_id)
            refreshed_payloads_by_variant[variant_id] = dict(payload)
            continue
        task_id = payload.get("external_task_id")
        status = str(payload.get("generation_status") or "").lower()
        if not task_id or status in {"completed", "succeeded", "success"}:
            continue
        result = provider.poll_video_task(
            task_id=task_id,
            model=model_name,
            api_base_url=video_runtime.get("api_base_url") or runtime_config.get("api_base_url"),
            api_key=video_runtime.get("api_key") or runtime_config.get("api_key"),
            extra=video_runtime.get("extra") or runtime_config.get("extra"),
        )
        selected = result.videos[0] if result.videos else None
        refreshed += 1
        payload["generation_status"] = result.status or (selected.status if selected else None) or payload.get("generation_status")
        payload["raw_response"] = result.raw_response or (selected.raw_response if selected else {}) or payload.get("raw_response") or {}
        _mark_external_task_polled(payload, asset_type="video", status=payload.get("generation_status"))
        _trace_external_task_poll(
            db=db,
            run_id=run_id,
            stage_name="video_generation",
            agent_name="video_generation_agent",
            provider_name=provider_name,
            model_name=model_name,
            payload=payload,
        )
        if selected and selected.url:
            payload["result_url"] = selected.url
        if selected and (selected.url or selected.b64_data):
            video_bytes, source = runtime._materialize_generated_video(selected)
            if video_bytes:
                filename = f"{payload.get('variant_id') or asset.variant.variant_id}_sample.mp4"
                uri = runtime.media.write_binary_artifact(run_id, filename, video_bytes)
                payload["video_uri"] = uri
                payload["source"] = source
                payload["generation_status"] = "completed"
                payload = runtime._attach_generated_video_frames(run_id=run_id, video_payload=payload)
                payload["visual_qa"] = runtime._local_media_qa(
                    asset_type="video",
                    uri=payload.get("video_uri"),
                    payload=payload,
                )
                asset.uri = uri
                completed += 1
        failure_category, error_message = _generated_asset_failure(payload, payload.get("video_uri"))
        asset.payload = payload
        asset.failure_category = failure_category
        asset.error_message = error_message
        variant_id = str(payload.get("variant_id") or asset.variant.variant_id)
        refreshed_payloads_by_variant[variant_id] = dict(payload)
    if refreshed_payloads_by_variant:
        _sync_refreshed_video_generation_state(db, run, refreshed_payloads_by_variant)
        _resume_full_auto_visual_qa_after_refresh(db, run)
    db.flush()
    return {"refreshed": refreshed, "completed": completed, "summary": _variant_summary(db, run_id)}


def refresh_copy_image_task_assets(db: Session, run_id: str) -> dict:
    run = get_run(db, run_id)
    config = resolve_agent_config(
        db,
        agent_name="copy_image_agent",
        run_provider=run.model_provider,
        run_model=run.model_name,
    )
    runtime_config = resolve_agent_runtime(config)
    image_runtime = runtime_config.get("image") or {}
    provider_name = image_runtime.get("provider_name") or config.get("image_provider_name") or config.get("provider_name")
    model_name = image_runtime.get("model_name") or config.get("image_model_name") or config.get("model_name")
    provider = runtime.providers.get(provider_name)
    refreshed = 0
    completed = 0
    task = db.scalar(
        select(StageTask).where(
            StageTask.run_id == run_id,
            StageTask.stage_name == "copy_image_generation",
        )
    )
    image_assets_by_variant: dict[str, dict] = {}
    if task and task.output_payload:
        for image_asset in task.output_payload.get("image_assets", []) or []:
            if isinstance(image_asset, dict):
                image_assets_by_variant[str(image_asset.get("variant_id") or "")] = deepcopy(image_asset)

    assets = db.scalars(
        select(VariantAsset).where(
            VariantAsset.run_id == run_id,
            VariantAsset.asset_type == "image",
        ).order_by(VariantAsset.created_at.asc())
    ).all()
    for asset in assets:
        payload = dict(asset.payload or {})
        task_id = payload.get("external_task_id")
        status = str(payload.get("generation_status") or "").lower()
        if not task_id:
            continue
        if status in {"completed", "succeeded", "success"} and _uri_has_payload(asset.uri or payload.get("uri")):
            _refresh_completed_image_qa(
                payload,
                asset_type="image",
                uri=asset.uri or payload.get("uri"),
                expected_ratio=payload.get("aspect_ratio"),
            )
            completed += 1
        elif _uri_has_payload(asset.uri or payload.get("uri")) and not payload.get("error"):
            _refresh_completed_image_qa(
                payload,
                asset_type="image",
                uri=asset.uri or payload.get("uri"),
                expected_ratio=payload.get("aspect_ratio"),
            )
            completed += 1
        else:
            result = provider.poll_image_task(
                task_id=task_id,
                model=model_name,
                api_base_url=image_runtime.get("api_base_url") or runtime_config.get("api_base_url"),
                api_key=image_runtime.get("api_key") or runtime_config.get("api_key"),
                extra=image_runtime.get("extra") or runtime_config.get("extra"),
            )
            selected = result.images[0] if result.images else None
            refreshed += 1
            payload["generation_status"] = result.status or (selected.status if selected else None) or payload.get("generation_status")
            payload["raw_response"] = result.raw_response or (selected.raw_response if selected else {}) or payload.get("raw_response") or {}
            _mark_external_task_polled(payload, asset_type="image", status=payload.get("generation_status"))
            _trace_external_task_poll(
                db=db,
                run_id=run_id,
                stage_name="copy_image_generation",
                agent_name="copy_image_agent",
                provider_name=provider_name,
                model_name=model_name,
                payload=payload,
            )
            if selected and (selected.url or selected.b64_json):
                image_bytes, source = runtime._materialize_generated_image(selected)
                if image_bytes:
                    filename = Path(asset.uri or payload.get("uri") or f"{payload.get('variant_id') or asset.id}.png").name
                    uri = runtime.media.write_binary_artifact(run_id, filename, image_bytes)
                    payload["uri"] = uri
                    payload["source"] = source
                    payload["generation_status"] = "completed"
                    _refresh_completed_image_qa(
                        payload,
                        asset_type="image",
                        uri=uri,
                        expected_ratio=payload.get("aspect_ratio"),
                    )
                    asset.uri = uri
                    completed += 1
        failure_category, error_message = _generated_asset_failure(payload, payload.get("uri"))
        asset.payload = payload
        asset.failure_category = failure_category
        asset.error_message = error_message
        variant_id = str(payload.get("variant_id") or "")
        if variant_id in image_assets_by_variant:
            current = image_assets_by_variant[variant_id]
            if _copy_image_asset_rank(payload) >= _copy_image_asset_rank(current):
                image_assets_by_variant[variant_id].update(payload)
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.run_id == run_id,
                Artifact.artifact_type == "generated_image",
                Artifact.uri == (asset.uri or payload.get("uri")),
            )
        )
        if artifact:
            artifact.payload = payload
            artifact.uri = payload.get("uri") or artifact.uri
    if task and task.output_payload:
        image_assets = list(image_assets_by_variant.values())
        task.output_payload = {**task.output_payload, "image_assets": image_assets}
        pending = [item for item in image_assets if isinstance(item, dict) and _pending_copy_image_asset(item)]
        copy_auto = should_auto_approve(run.approval_mode, task.stage_name) or (
            run.approval_mode == "semi_auto" and is_marketplace_main_image(run.creative_specs)
        )
        _settle_copy_image_stage_status(db, run, task, image_assets)
        failed = _copy_image_failure_lines(image_assets)
        if copy_auto and task.status == TaskStatus.WAITING_REVIEW.value and not pending and not failed:
            auto_approve_stage(db, run.id, task.stage_name)
    db.flush()
    return {"refreshed": refreshed, "completed": completed, "summary": _variant_summary(db, run_id)}


def refresh_storyboard_image_task_assets(db: Session, run_id: str) -> dict:
    run = get_run(db, run_id)
    config = resolve_agent_config(
        db,
        agent_name="storyboard_agent",
        run_provider=run.model_provider,
        run_model=run.model_name,
    )
    if not has_resolved_image_config(config):
        fallback = resolve_agent_config(
            db,
            agent_name="copy_image_agent",
            run_provider=run.model_provider,
            run_model=run.model_name,
        )
        config = with_fallback_image_config(config, fallback, source="copy_image_agent")
    runtime_config = resolve_agent_runtime(config)
    image_runtime = runtime_config.get("image") or {}
    provider_name = image_runtime.get("provider_name") or config.get("image_provider_name") or config.get("provider_name")
    model_name = image_runtime.get("model_name") or config.get("image_model_name") or config.get("model_name")
    provider = runtime.providers.get(provider_name)
    refreshed = 0
    completed = 0
    task = db.scalar(
        select(StageTask).where(
            StageTask.run_id == run_id,
            StageTask.stage_name == "storyboard_image_generation",
        )
    )
    frames_by_key: dict[tuple[str, str], dict] = {}
    if task and task.output_payload:
        for frame in task.output_payload.get("frames", []) or []:
            if isinstance(frame, dict):
                frames_by_key[(str(frame.get("variant_id") or ""), str(frame.get("frame_id") or ""))] = deepcopy(frame)

    assets = db.scalars(
        select(VariantAsset).where(
            VariantAsset.run_id == run_id,
            VariantAsset.asset_type == "storyboard_frame",
        )
    ).all()
    for asset in assets:
        payload = dict(asset.payload or {})
        task_id = payload.get("external_task_id")
        status = str(payload.get("generation_status") or "").lower()
        if not task_id:
            continue
        if status in {"completed", "succeeded", "success"}:
            _refresh_completed_image_qa(
                payload,
                asset_type="storyboard_frame",
                uri=asset.uri or payload.get("image_uri"),
                expected_ratio=payload.get("aspect_ratio"),
            )
        elif payload.get("image_uri") and _uri_has_payload(payload.get("image_uri")) and not payload.get("error"):
            _refresh_completed_image_qa(
                payload,
                asset_type="storyboard_frame",
                uri=asset.uri or payload.get("image_uri"),
                expected_ratio=payload.get("aspect_ratio"),
            )
            completed += 1
        else:
            result = provider.poll_image_task(
                task_id=task_id,
                model=model_name,
                api_base_url=image_runtime.get("api_base_url") or runtime_config.get("api_base_url"),
                api_key=image_runtime.get("api_key") or runtime_config.get("api_key"),
                extra=image_runtime.get("extra") or runtime_config.get("extra"),
            )
            selected = result.images[0] if result.images else None
            refreshed += 1
            payload["generation_status"] = result.status or (selected.status if selected else None) or payload.get("generation_status")
            payload["raw_response"] = result.raw_response or (selected.raw_response if selected else {}) or payload.get("raw_response") or {}
            _mark_external_task_polled(payload, asset_type="storyboard_frame", status=payload.get("generation_status"))
            _trace_external_task_poll(
                db=db,
                run_id=run_id,
                stage_name="storyboard_image_generation",
                agent_name="storyboard_agent",
                provider_name=provider_name,
                model_name=model_name,
                payload=payload,
            )
            if selected and (selected.url or selected.b64_json):
                image_bytes, source = runtime._materialize_generated_image(selected)
                if image_bytes:
                    filename = Path(asset.uri or payload.get("image_uri") or f"{payload.get('frame_id') or asset.id}.png").name
                    uri = runtime.media.write_binary_artifact(run_id, filename, image_bytes)
                    payload["image_uri"] = uri
                    payload["source"] = source
                    payload["generation_status"] = "completed"
                    _refresh_completed_image_qa(
                        payload,
                        asset_type="storyboard_frame",
                        uri=uri,
                    )
                    asset.uri = uri
                    completed += 1
        failure_category, error_message = _generated_asset_failure(payload, payload.get("image_uri"))
        asset.payload = payload
        asset.failure_category = failure_category
        asset.error_message = error_message
        key = (str(payload.get("variant_id") or ""), str(payload.get("frame_id") or ""))
        if key in frames_by_key:
            _sync_storyboard_frame_payload(frames_by_key[key], payload)
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.run_id == run_id,
                Artifact.artifact_type == "storyboard_frame",
                Artifact.uri == (asset.uri or payload.get("image_uri")),
            )
        )
        if artifact:
            artifact.payload = payload
            artifact.uri = payload.get("image_uri") or artifact.uri
    if task and task.output_payload:
        task.output_payload = {**task.output_payload, "frames": list(frames_by_key.values())}
        frames = task.output_payload.get("frames") or []
        pending = [
            frame for frame in frames
            if isinstance(frame, dict)
            and _pending_storyboard_frame(frame)
        ]
        failed = [frame for frame in frames if isinstance(frame, dict) and frame.get("error")]
        if run.approval_mode == "full_auto" and task.status == TaskStatus.WAITING_REVIEW.value and not pending and not failed:
            auto_approve_stage(db, run.id, task.stage_name)
    db.flush()
    return {"refreshed": refreshed, "completed": completed, "summary": _variant_summary(db, run_id)}


def refresh_async_assets(db: Session, run_id: str) -> dict:
    copy_images = refresh_copy_image_task_assets(db, run_id)
    images = refresh_storyboard_image_task_assets(db, run_id)
    videos = refresh_video_task_assets(db, run_id)
    return {
        "refreshed": copy_images["refreshed"] + images["refreshed"] + videos["refreshed"],
        "completed": copy_images["completed"] + images["completed"] + videos["completed"],
        "copy_images": copy_images,
        "images": images,
        "videos": videos,
        "summary": videos.get("summary") or images.get("summary") or copy_images.get("summary"),
    }


def _default_regeneration_stage(run: PipelineRun) -> str:
    plan = stage_plan_for(run.pipeline_mode)
    for stage_name in ["video_generation", "copy_image_generation", "storyboard_image_generation", "video_scripting"]:
        if stage_name in plan:
            return stage_name
    raise ValueError(f"pipeline mode {run.pipeline_mode} has no regeneratable stage")


def _qa_repair_context(
    *,
    db: Session,
    variant: RunVariant,
    reason: str,
    target_stage: str,
) -> dict:
    visual_quality = (variant.metadata_json or {}).get("visual_quality") or {}
    flags: list[str] = []
    flags.extend(str(item) for item in (visual_quality.get("blocking_issues") or []) if str(item).strip())
    for report in visual_quality.get("asset_reports") or []:
        if not isinstance(report, dict):
            continue
        flags.extend(str(item) for item in (report.get("flags") or []) if str(item).strip())
        contract = report.get("image_asset_contract") or {}
        if isinstance(contract, dict):
            flags.extend(str(item) for item in (contract.get("flags") or []) if str(item).strip())
        preflight = report.get("stitch_preflight") or {}
        if isinstance(preflight, dict):
            flags.extend(str(item) for item in (preflight.get("flags") or []) if str(item).strip())
    assets = db.scalars(
        select(VariantAsset).where(VariantAsset.run_variant_id == variant.id).order_by(VariantAsset.created_at.desc())
    ).all()
    for asset in assets[:8]:
        payload = asset.payload or {}
        for key in ("visual_qa", "image_asset_contract", "marketplace_qa"):
            row = payload.get(key) or {}
            if isinstance(row, dict):
                flags.extend(str(item) for item in (row.get("flags") or []) if str(item).strip())
        ledger = payload.get("segment_ledger") or {}
        if isinstance(ledger, dict):
            blocked_id = ledger.get("first_blocked_segment_id")
            if blocked_id:
                flags.append(f"segment_blocked:{blocked_id}")
    flag_set = list(dict.fromkeys(flags))
    rules = {
        "media_gate_generation_error": "Retry image generation; the provider failed before returning usable media.",
        "media_gate_placeholder": "Produce a real usable visual asset; do not return a placeholder, blank image, or error artifact.",
        "media_gate_decode_error": "Return a valid decodable media file.",
        "media_gate_empty_file": "Return a non-empty generated asset.",
        "media_gate_missing_file": "Regenerate the missing asset file.",
        "media_gate_missing_uri": "Return a concrete asset URI.",
        "media_gate_product_truth_color_mismatch": "Restore the required product colors from the product truth contract.",
        "media_gate_product_truth_structure_review": "Preserve the submitted product structure and visible parts.",
        "media_gate_low_information": "Increase visible product detail and visual information.",
        "visual_qa_placeholder": "Produce a real usable visual asset; do not return a placeholder, blank image, or error artifact.",
        "visual_qa_decode_error": "Return a valid decodable media file.",
        "visual_qa_empty_file": "Return a non-empty generated asset.",
        "visual_qa_missing_file": "Regenerate the missing asset file.",
        "visual_qa_missing_uri": "Return a concrete asset URI.",
        "visual_qa_product_truth_color_mismatch": "Restore the required product colors from the product truth contract.",
        "visual_qa_product_truth_structure_review": "Preserve the submitted product structure and visible parts.",
        "visual_qa_low_information": "Increase visible product detail and visual information.",
        "visual_qa_unusable_frame_sequence": "Generate readable first, middle, and final video frames.",
        "visual_qa_needs_frame_review": "Make the sampled video frames clear enough for review.",
        "visual_qa_visual_proof_failed": "Regenerate with the visual proof mechanism clearly shown and avoid the listed semantic failure condition.",
        "stitch_preflight_failed": "Repair the failed segment continuity before stitching.",
        "visual_qa_human_anatomy_review": "Keep any human pose physically plausible.",
        "marketplace_background_not_white": "Use a clean pure white marketplace background.",
        "marketplace_placeholder": "Produce a real marketplace product image, not a placeholder.",
        "marketplace_missing_reference": "Use the submitted source product reference.",
        "marketplace_resolution_low": "Meet the requested marketplace export resolution.",
        "product_fill_low": "Make the product larger and clearly inspectable in frame.",
    }
    actions = [text for key, text in rules.items() if key in flag_set]
    review = visual_quality.get("visual_proof_qa") or {}
    if target_stage == "copy_image_generation" and isinstance(review, dict) and review.get("status") == "fail":
        repair = review.get("repair_instruction")
        if isinstance(repair, str) and repair.strip():
            actions.insert(0, repair.strip()[:320])
    for flag in flag_set:
        if flag.startswith("segment_blocked:"):
            actions.append(f"Resume from {flag.split(':', 1)[1]} and keep previous completed segments unchanged.")
        if flag.startswith("visual_proof_failed:"):
            actions.append(f"Avoid semantic failure: {flag.split(':', 1)[1]}.")
    if not actions and reason.strip():
        actions.append(f"Address operator request: {reason.strip()[:160]}.")
    actions = list(dict.fromkeys(actions))[:4]
    prompt = (
        "Regeneration repair instructions: keep the current variant angle and strategy; repair only the failed asset details. "
        + " ".join(f"{idx + 1}. {action}" for idx, action in enumerate(actions))
    )
    return {
        "target_stage": target_stage,
        "prompt": prompt,
        "actions": actions,
        "evidence_flags": flag_set[:12],
    }


def regenerate_variant_assets(
    db: Session,
    *,
    run_id: str,
    variant_id: str,
    reason: str,
    target_stage: str | None = None,
    write_memory: bool = True,
) -> RunVariant:
    run = get_run(db, run_id)
    variant = get_run_variant(db, run_id, variant_id)
    variant = review_variant(
        db,
        run_id=run_id,
        variant_id=variant_id,
        action=VariantReviewAction.REQUEST_REGENERATION.value,
        comment=reason,
        metadata={"target_stage": target_stage},
        write_memory=write_memory,
    )
    stage_name = target_stage or _default_regeneration_stage(run)
    if stage_name not in REGENERATABLE_STAGES:
        raise ValueError(f"stage {stage_name} does not support variant regeneration")
    if stage_name not in stage_plan_for(run.pipeline_mode):
        raise ValueError(f"stage {stage_name} is not part of pipeline mode {run.pipeline_mode}")
    task = get_stage_task(db, run_id, stage_name)
    lead_agent = stage_agent(stage_name)
    collaborators = list(stage_collaborators(stage_name))
    resolved = resolve_agent_config(
        db,
        agent_name=lead_agent,
        run_provider=run.model_provider,
        run_model=run.model_name,
    )
    if stage_name == "storyboard_image_generation" and not has_resolved_image_config(resolved):
        image_resolved = resolve_agent_config(
            db,
            agent_name="copy_image_agent",
            run_provider=run.model_provider,
            run_model=run.model_name,
        )
        resolved = with_fallback_image_config(
            resolved,
            image_resolved,
            source="copy_image_agent",
        )
    runtime_config = resolve_agent_runtime(resolved)
    provider_name = resolved["provider_name"]
    model_name = resolved["model_name"]
    scope = f"regen-{utcnow().strftime('%Y%m%d%H%M%S%f')}"
    asset_suffix = f"_{scope}"
    qa_repair = _qa_repair_context(db=db, variant=variant, reason=reason, target_stage=stage_name)
    if write_memory:
        write_regeneration_memory(
            db,
            run_id=run.id,
            variant=variant,
            stage_name=stage_name,
            reason=reason,
            scope=scope,
            status="requested",
        )
    runtime_config = {
        **runtime_config,
        "force_regenerate": True,
        "asset_name_suffix": asset_suffix,
        "regeneration_reason": reason,
        "regeneration_variant_id": variant_id,
        "qa_repair": qa_repair,
        "qa_repair_prompt": qa_repair["prompt"],
    }
    task.input_payload = _build_task_input(db, run, task)
    task.input_payload = append_execution_memory_payload(
        task.input_payload,
        bucket="variant",
        memory=resolve_execution_memory(db, run_id=run.id, stage_name=stage_name, variant_id=variant_id),
    )
    persona_snapshots = _persona_snapshots(db, [lead_agent, *collaborators])
    compiled_persona = build_compiled_persona(
        persona_snapshots=persona_snapshots,
        lead_agent=lead_agent,
        collaborators=collaborators,
    )
    task.metadata_json = {
        **(task.metadata_json or {}),
        "agent_name": lead_agent,
        "collaborators": collaborators,
        "stage_contract_version": STAGE_CONTRACT_VERSION,
        "resolved_api": resolved,
        "persona_snapshots": persona_snapshots,
        "compiled_persona": compiled_persona,
        "regeneration_requests": [
            *((task.metadata_json or {}).get("regeneration_requests") or []),
            {
                "variant_id": variant_id,
                "target_stage": stage_name,
                "reason": reason,
                "scope": scope,
                "qa_repair_actions": qa_repair["actions"],
                "created_at": utcnow().isoformat(),
            },
        ],
    }
    runtime_config = {**runtime_config, "compiled_persona": compiled_persona}
    add_agent_trace_event(
        db,
        run_id=run.id,
        stage_task_id=task.id,
        stage_name=stage_name,
        agent_name=lead_agent,
        event_type="regeneration_started",
        message=f"{lead_agent} started regeneration for variant {variant_id}.",
        provider_name=provider_name,
        model_name=model_name,
        payload={
            "variant_id": variant_id,
            "target_stage": stage_name,
            "reason": reason,
            "scope": scope,
            "qa_repair_actions": qa_repair["actions"],
            "qa_repair_evidence_flags": qa_repair["evidence_flags"],
        },
    )

    def trace_regeneration_model_event(event_type: str, message: str, payload: dict | None = None) -> None:
        payload = payload or {}
        add_agent_trace_event(
            db,
            run_id=run.id,
            stage_task_id=task.id,
            stage_name=stage_name,
            agent_name=lead_agent,
            event_type=event_type,
            message=_truncate_text(message, 500),
            provider_name=payload.get("selected_provider_name") or provider_name,
            model_name=payload.get("selected_model_name") or model_name,
            payload=payload,
        )
        db.flush()

    runtime_config = {**runtime_config, "trace_callback": trace_regeneration_model_event}

    output = execute_regeneration_stage(
        RegenerationExecutionContext(
            db=db,
            run=run,
            task=task,
            runtime=runtime,
            runtime_config=runtime_config,
            provider_name=provider_name,
            model_name=model_name,
            variant_id=variant_id,
            get_single_variant_set=_single_variant_set,
            get_single_script_pack=_single_script_pack,
            get_stage_output=_get_stage_output,
            get_latest_video_payload=_latest_video_payload_for_variant,
        )
    )

    task.output_payload = _merge_stage_payload(stage_name, task.output_payload or {}, output.payload)
    task.model_used = output.model_used
    task.failure_category = None
    task.error_message = None
    for artifact in output.artifacts:
        db.add(
            Artifact(
                run_id=run.id,
                stage_name=stage_name,
                artifact_type=artifact["type"],
                uri=artifact["uri"],
                payload={
                    **(artifact["payload"] or {}),
                    "regeneration_scope": scope,
                    "regeneration_reason": reason,
                    "qa_repair": qa_repair,
                },
            )
        )
    _variant_library_sync(db, run, task, output.payload, idempotency_scope=scope)
    add_agent_trace_event(
        db,
        run_id=run.id,
        stage_task_id=task.id,
        stage_name=stage_name,
        agent_name=lead_agent,
        event_type="regeneration_completed",
        message=f"{lead_agent} completed regeneration for variant {variant_id}.",
        provider_name=provider_name,
        model_name=model_name,
        payload={
            "variant_id": variant_id,
            "target_stage": stage_name,
            "scope": scope,
            "output_shape": _payload_shape(output.payload),
            "artifact_count": len(output.artifacts),
        },
    )
    variant.regenerate_requested = False
    variant.review_status = "regenerated"
    variant.status = VariantLifecycleStatus.GENERATED.value
    variant.metadata_json = {
        **(variant.metadata_json or {}),
        "latest_regeneration": {
            "target_stage": stage_name,
            "reason": reason,
            "scope": scope,
            "qa_repair_actions": qa_repair["actions"],
            "completed_at": utcnow().isoformat(),
        },
    }
    if write_memory:
        write_regeneration_memory(
            db,
            run_id=run.id,
            variant=variant,
            stage_name=stage_name,
            reason=reason,
            scope=scope,
            status="completed",
        )
    run.budget_used = float(run.budget_used or 0.0) + output.estimated_cost
    run.updated_at = utcnow()
    db.flush()
    return variant


def retry_copy_image_asset(
    db: Session,
    *,
    run_id: str,
    variant_id: str,
    reason: str,
) -> RunVariant:
    variant = regenerate_variant_assets(
        db,
        run_id=run_id,
        variant_id=variant_id,
        reason=reason,
        target_stage="copy_image_generation",
        write_memory=False,
    )
    run = get_run(db, run_id)
    task = get_stage_task(db, run_id, "copy_image_generation")
    _settle_copy_image_stage_status(
        db,
        run,
        task,
        [item for item in (task.output_payload or {}).get("image_assets", []) or [] if isinstance(item, dict)],
    )
    return variant


def review_variant(
    db: Session,
    *,
    run_id: str,
    variant_id: str,
    action: str,
    comment: str = "",
    tags: list[str] | None = None,
    metadata: dict | None = None,
    write_memory: bool = True,
) -> RunVariant:
    variant = get_run_variant(db, run_id, variant_id)
    run = get_run(db, run_id)
    tags = tags or []
    metadata = metadata or {}
    if action == VariantReviewAction.APPROVE.value:
        variant.status = VariantLifecycleStatus.WINNER.value if variant.is_winner else VariantLifecycleStatus.APPROVED.value
        variant.review_status = "approved"
        variant.regenerate_requested = False
    elif action == VariantReviewAction.REJECT.value:
        variant.status = VariantLifecycleStatus.REJECTED.value
        variant.review_status = "rejected"
    elif action == VariantReviewAction.SHORTLIST.value:
        variant.status = VariantLifecycleStatus.SHORTLISTED.value
        variant.shortlisted = True
        variant.review_status = "shortlisted"
    elif action == VariantReviewAction.SET_WINNER.value:
        other_rows = db.scalars(select(RunVariant).where(RunVariant.run_id == run_id)).all()
        for row in other_rows:
            if row.id != variant.id and row.is_winner:
                row.is_winner = False
                if row.status == VariantLifecycleStatus.WINNER.value:
                    row.status = VariantLifecycleStatus.SHORTLISTED.value if row.shortlisted else VariantLifecycleStatus.GENERATED.value
        variant.is_winner = True
        variant.shortlisted = True
        variant.status = VariantLifecycleStatus.WINNER.value
        variant.review_status = "winner"
    elif action == VariantReviewAction.REQUEST_REGENERATION.value:
        variant.status = VariantLifecycleStatus.NEEDS_REGENERATION.value
        variant.regenerate_requested = True
        variant.review_status = "request_regeneration"
    else:
        raise ValueError(f"unsupported variant action: {action}")
    variant.updated_at = utcnow()
    db.add(
        VariantReview(
            run_variant_id=variant.id,
            run_id=run_id,
            action=action,
            comment=comment,
            tags=tags,
            metadata_json=metadata,
        )
    )
    marketplace_tags = sorted(set(tags).intersection(MARKETPLACE_REVIEW_TAGS))
    if marketplace_tags and is_marketplace_main_image(run.creative_specs):
        db.add(
            GmMemory(
                project_id=run.project_id,
                run_id=run.id,
                memory_scope="product",
                product_code=run.product_code,
                industry_code=run.industry_code,
                source_type="operator_visual_review",
                score_hint=1.0 if action in {VariantReviewAction.APPROVE.value, VariantReviewAction.SET_WINNER.value} else -1.0,
                memory_type="visual_quality",
                content={
                    "source": "operator_review",
                    "asset_goal": "marketplace_main_image",
                    "variant_id": variant_id,
                    "action": action,
                    "tags": marketplace_tags,
                    "comment": comment,
                    "summary": "Use product-level marketplace image QA tags to avoid repeated visual defects.",
                },
            )
        )
    add_agent_trace_event(
        db,
        run_id=run_id,
        stage_task_id=None,
        stage_name=metadata.get("target_stage") or "variant_review",
        agent_name="human_reviewer",
        event_type=action,
        message=f"Human review action {action} applied to variant {variant_id}.",
        payload={"variant_id": variant_id, "comment": comment, "tags": tags, "metadata": metadata},
    )
    if write_memory:
        write_variant_review_memory(
            db,
            run_id=run_id,
            variant=variant,
            action=action,
            comment=comment,
            tags=tags,
            metadata=metadata,
        )
    db.flush()
    return variant


def get_last_product_config(db: Session, product_code: str) -> dict | None:
    """Return the creative config from the most recent run for a given product_code."""
    last_run = db.scalar(
        select(PipelineRun)
        .where(PipelineRun.product_code == product_code)
        .order_by(desc(PipelineRun.created_at))
    )
    if not last_run:
        return None
    return {
        "product_code": product_code,
        "pipeline_mode": last_run.pipeline_mode,
        "approval_mode": last_run.approval_mode,
        "creative_preset": last_run.creative_preset,
        "creative_specs": last_run.creative_specs,
        "channel": last_run.campaign.channel if last_run.campaign else "meta",
        "objective": last_run.campaign.objective if last_run.campaign else "conversions",
        "last_run_at": last_run.created_at,
    }
