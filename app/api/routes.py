from __future__ import annotations

import io
import json
import uuid
import mimetypes
import asyncio
import os
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from sqlalchemy import String, cast, desc, func, or_, select
from sqlalchemy.orm import Session

from app.agents.registry import stage_agent
from app.core.config import get_settings
from app.data.models import Artifact, Campaign, ContentSchedule, GmMemory, GmPolicyVersion, GmReflection, IntegrationSync, PerformanceSnapshot, PipelineRun, Product, Project, RunVariant, ScoreCard as ScoreCardModel, ShopChannelAccount, ShopSite, StageTask, VariantAsset, Workspace
from app.data.session import (
    SessionLocal,
    get_active_database_url,
    get_db,
    list_local_sqlite_database_urls,
    switch_database_url,
)
from app.orchestrator.state_machine import PIPELINE_STAGE_PLANS, PipelineMode
from app.orchestrator.worker import worker
from app.templates import templates
from app.schemas.api import (
    AgentApiConfigPatchRequest,
    AgentApiConfigView,
    AssetProductItem,
    AssetProductListResponse,
    AgentTraceEventView,
    ArtifactListItem,
    ArtifactListResponse,
    BulkIdsRequest,
    DataSourceInfo,
    DataSourceListResponse,
    DataSourceSelectRequest,
    DeliverablesResponse,
    ExecutionMemoryLedgerResponse,
    FeedbackImportRequest,
    FeedbackImportResponse,
    GmMemoryCompactRequest,
    GmMemoryItem,
    GmMemoryUpdateRequest,
    GmPolicyItem,
    GmPolicyPromoteRequest,
    GmReflectionItem,
    IntegrationConfigPatchRequest,
    IntegrationConfigView,
    LeaderboardItem,
    LeaderboardResponse,
    PersonaMeta,
    PersonaPatchRequest,
    PersonaView,
    PipelineModeView,
    QueueHealthResponse,
    QueueRunningTask,
    QueueStatusResponse,
    ReviewActionRequest,
    RunCreateRequest,
    RunPreflightRequest,
    RunPreflightResponse,
    RunVariantView,
    RunSummary,
    RunMediaResponse,
    RunView,
    StageTaskView,
    RunStatusExplanation,
    VariantRegenerateRequest,
    VariantReviewRequest,
    VariantSelectRequest,
    VariantsResponse,
    CreativePresetCreate,
    CreativePresetListResponse,
    CreativePresetUpdate,
    CreativePresetView,
    ProductConfigHint,
    RunTemplateCreate,
    RunTemplateUpdate,
    RunTemplateView,
    GmMemoryReviewActionRequest,
    ShopAnalysisRequest,
    ShopAnalysisResponse,
    ShopAnalysisPreflightResponse,
    ShopAnalysisListItem,
    ShopAnalysisHistoryResponse,
    ResearchTaskHistoryResponse,
    ResearchTaskItem,
    ShopItem,
    ShopChannelAccountItem,
    ShopChannelAccountListResponse,
    ShopChannelAccountPatchRequest,
    ShopListResponse,
    ShopPatchRequest,
    ShopSiteItem,
    ShopSiteListResponse,
    ShopSitePatchRequest,
    CategoryItem,
    CategoryListResponse,
    ContentScheduleCreateRequest,
    ContentSchedulePublishRequest,
    ContentScheduleUpdateRequest,
    ContentScheduleView,
    ContentScheduleListResponse,
    NotionConnectionTestResponse,
    VariantScheduleCandidate,
)
from app.schemas.contracts import ComplianceLevel, ConversionForecast, ScoreCard
from app.services.agent_api_configs import (
    API_KEY_ENV_PREFIX,
    api_key_available,
    list_agent_configs,
    list_api_key_env_names,
    upsert_agent_config,
)
from app.services.feedback import import_feedback_rows, project_leaderboard
from app.services.execution_memory import build_run_execution_ledger
from app.services.gm_evolution import (
    compile_feedback_import_reflections,
    compile_operator_review_reflection,
    evaluate_gm_policy,
    promote_gm_policy,
)
from app.services.gm_review import build_gm_review_summary, render_gm_review_markdown
from app.services.gm_memory import compact_gm_memory
from app.services.intake_assets import process_uploaded_payloads
from app.services.marketplace_qa import is_marketplace_main_image
from app.services.personas import get_persona, list_persona_catalog, persona_info, update_persona
from app.services.creative_specs import (
    create_creative_preset,
    extract_site_surface,
    delete_creative_preset,
    extract_storyboard_candidate_count,
    extract_tiktok_video_style,
    get_creative_preset,
    list_system_presets,
    list_user_presets,
    update_creative_preset,
    with_preset_metadata,
)
from app.services.capability_preflight import preflight_run_capabilities
from app.services.templates import (
    create_run_template,
    delete_run_template,
    get_run_template,
    list_run_templates,
    update_run_template,
)
from app.services.runs import (
    approve_stage,
    create_run,
    get_last_product_config,
    get_run,
    latest_scorecard,
    reject_stage,
    regenerate_variant_assets,
    rerun_stage,
    refresh_async_assets,
    refresh_video_task_assets,
    retry_copy_image_asset,
    review_variant,
    run_deliverables,
    run_trace_events,
    run_variants,
)
from app.services.shop_analysis import (
    list_shop_analyses,
    save_competitor_analysis,
    save_shop_profile,
)


router = APIRouter()
DEFAULT_GENERATED_ARTIFACT_TYPES = {
    "copy_image_bundle",
    "generated_image",
    "video_script_pack",
    "storyboard_pack",
    "storyboard_frame",
    "generated_video",
    "video_bundle",
    "visual_quality_report",
    "evaluation_selection",
}


def _serialize_gm_policy(row: GmPolicyVersion) -> GmPolicyItem:
    return GmPolicyItem(
        id=row.id,
        project_id=row.project_id,
        version=row.version,
        status=row.status,
        target_scope=row.target_scope,
        shop_id=row.shop_id,
        product_code=row.product_code,
        industry_code=row.industry_code,
        pipeline_mode=row.pipeline_mode,
        confidence_score=row.confidence_score,
        evidence_count=row.evidence_count,
        replay_status=row.replay_status,
        replay_score=row.replay_score,
        replay_summary=row.replay_summary,
        replay_details=row.replay_details or {},
        source_reflection_ids=row.source_reflection_ids or [],
        content=row.content or {},
        notes=row.notes,
        created_at=row.created_at,
        activated_at=row.activated_at,
        last_evaluated_at=row.last_evaluated_at,
    )


def _load_json_list(raw: str | None, field_name: str) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid json for {field_name}") from exc
    if not isinstance(parsed, list):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a JSON list")
    return parsed


def _load_json_dict(raw: str | None, field_name: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid json for {field_name}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a JSON object")
    return parsed


def _infer_media_kind_from_reference(value: object) -> str | None:
    if isinstance(value, str):
        ref = value.strip()
    elif isinstance(value, dict):
        for key in ("url", "image_url", "video_url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                ref = candidate.strip()
                break
        else:
            return None
    else:
        return None
    if not ref:
        return None
    lowered = ref.lower()
    if lowered.startswith("data:image/"):
        return "image"
    if lowered.startswith("data:video/"):
        return "video"
    parsed = urlsplit(ref)
    guess_target = parsed.path or ref
    mime, _ = mimetypes.guess_type(guess_target)
    if (mime or "").startswith("image/"):
        return "image"
    if (mime or "").startswith("video/"):
        return "video"
    return None


def _preflight_media_flags_from_urls(url_references: list[object]) -> tuple[bool, bool]:
    has_image = False
    has_video = False
    for ref in url_references:
        kind = _infer_media_kind_from_reference(ref)
        if kind == "image":
            has_image = True
        elif kind == "video":
            has_video = True
    return has_image, has_video


def _preflight_media_flags_for_payload(payload: RunCreateRequest) -> tuple[bool, bool]:
    creative_specs = payload.creative_specs or {}
    context = payload.context or {}
    candidates: list[object] = [*(context.get("url_references") or [])]
    for key in ("image_urls", "reference_image_urls", "video_image_urls", "video_urls"):
        candidates.extend(creative_specs.get(key) or [])
    return _preflight_media_flags_from_urls(candidates)


def _enforce_run_creation_preflight(
    db: Session,
    *,
    payload: RunCreateRequest,
    has_image_inputs: bool,
    has_video_inputs: bool,
) -> dict:
    preflight_result = preflight_run_capabilities(
        db,
        pipeline_mode=payload.pipeline_mode,
        has_image_inputs=has_image_inputs,
        has_video_inputs=has_video_inputs,
        creative_specs=payload.creative_specs,
    )
    if preflight_result.get("severity") == "error":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "preflight_failed",
                "message": "Run creation blocked by preflight checks.",
                "preflight": preflight_result,
            },
        )
    return preflight_result


def _stage_task_summary(task: StageTask) -> str:
    payload = task.output_payload or {}
    if task.error_message:
        return f"error: {task.error_message[:160]}"
    if not payload:
        return "No output yet."
    if task.stage_name == "intake":
        return (
            f"Intake ready: sku={len(payload.get('sku_summary', []))}, "
            f"images={len(payload.get('image_references', []))}, videos={len(payload.get('video_references', []))}"
        )
    if task.stage_name == "planning":
        return (
            f"Planning brief: angles={len(payload.get('strategic_angles', []))}, "
            f"constraints={len(payload.get('constraints', []))}"
        )
    if task.stage_name == "divergence":
        return f"Variants generated: {len(payload.get('variants', []))}"
    if task.stage_name == "copy_image_generation":
        return (
            f"Copy/Image generated: copy_variants={len(payload.get('copy_variants', []))}, "
            f"image_assets={len(payload.get('image_assets', []))}"
        )
    if task.stage_name == "video_scripting":
        return f"Video scripts generated: {len(payload.get('scripts', []))}"
    if task.stage_name == "storyboard_image_generation":
        return f"Storyboard frames generated: {len(payload.get('frames', []))}"
    if task.stage_name == "video_generation":
        return f"Video assets generated: {len(payload.get('videos', []))}"
    if task.stage_name == "evaluation_selection":
        selected = payload.get("selected_deliverables", {}) or {}
        winner = selected.get("winner_variant_id") or "N/A"
        ranked = ((payload.get("evaluation_result", {}) or {}).get("ranked_variants", [])) or []
        return f"Evaluation complete: winner={winner}, ranked={len(ranked)}"
    keys = list(payload.keys())
    return f"Output keys: {', '.join(keys[:6])}"


def _resolve_media_path(path: str) -> Path:
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        requested = (Path.cwd() / requested).resolve()
    settings_path = get_settings().assets_dir.resolve()
    try:
        requested.relative_to(settings_path)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="path outside allowed assets directory") from exc
    if not requested.exists() or not requested.is_file():
        raise HTTPException(status_code=404, detail="asset file not found")
    return requested


def _serialize_data_source(database_url: str, active_url: str) -> DataSourceInfo:
    path = database_url.removeprefix("sqlite:///")
    resolved = str(Path(path).expanduser().resolve()) if database_url.startswith("sqlite:///") else path
    return DataSourceInfo(
        id=database_url,
        name=Path(resolved).name if resolved else database_url,
        path=resolved,
        url=database_url,
        is_active=database_url == active_url,
    )


def _artifact_preview(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("headline", "primary_text", "description", "summary", "reasoning"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:180]
        if isinstance(value, list) and value:
            head = value[0]
            if isinstance(head, str) and head.strip():
                return head.strip()[:180]
    if "copy_variants" in payload and isinstance(payload["copy_variants"], list) and payload["copy_variants"]:
        item = payload["copy_variants"][0]
        if isinstance(item, dict):
            text = item.get("headline") or item.get("primary_text") or ""
            return str(text)[:180]
    return ""


def _parse_date_start(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def _parse_date_end(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw) + timedelta(days=1)


def _serialize_run(db: Session, run: PipelineRun) -> RunView:
    tasks = db.scalars(select(StageTask).where(StageTask.run_id == run.id).order_by(StageTask.created_at.asc())).all()
    task_views = [
        StageTaskView(
            id=task.id,
            stage_name=task.stage_name,
            status=task.status,
            attempt=task.attempt,
            review_notes=task.review_notes,
            output_payload=task.output_payload or {},
            metadata_json=task.metadata_json or {},
            summary=_stage_task_summary(task),
            raw_ref=task.id,
            error_message=task.error_message,
            started_at=task.started_at,
            completed_at=task.completed_at,
        )
        for task in tasks
    ]

    scorecard_model = latest_scorecard(db, run.id)
    scorecard = None
    forecast = None
    if scorecard_model:
        scorecard = ScoreCard(
            sub_scores=scorecard_model.sub_scores,
            total_score=scorecard_model.total_score,
            risk_labels=scorecard_model.risk_labels,
            explanation=scorecard_model.explanation,
            compliance_level=ComplianceLevel(scorecard_model.compliance_level),
            ai_artifact_score=scorecard_model.ai_artifact_score,
        )
        forecast = ConversionForecast.model_validate(scorecard_model.forecast)
    trace_views = [
        AgentTraceEventView(
            id=event.id,
            run_id=event.run_id,
            stage_task_id=event.stage_task_id,
            stage_name=event.stage_name,
            agent_name=event.agent_name,
            event_type=event.event_type,
            visibility=event.visibility,
            message=event.message,
            provider_name=event.provider_name,
            model_name=event.model_name,
            payload=event.payload or {},
            created_at=event.created_at,
        )
        for event in run_trace_events(db, run.id, limit=200)
    ]

    from app.services.research_context import build_research_context

    research_context = build_research_context(
        db,
        project_id=run.project_id,
        shop_id=run.workspace_id,
        industry_code=run.industry_code,
        memory_selection=(run.context_json or {}).get("memory_selection"),
    )

    return RunView(
        id=run.id,
        status=run.status,
        current_stage=run.current_stage,
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        product_id=run.product_id,
        product_code=run.product_code,
        industry_code=run.industry_code,
        campaign_id=run.campaign_id,
        market=run.market,
        locale=run.locale,
        model_provider=run.model_provider,
        model_name=run.model_name,
        creative_preset=run.creative_preset,
        creative_specs=run.creative_specs or {},
        pipeline_mode=run.pipeline_mode,
        approval_mode=run.approval_mode or "manual",
        enable_research=run.enable_research,
        manual_research_brief=run.manual_research_brief or "",
        memory_selection=(run.context_json or {}).get("memory_selection") or {},
        business_context=run.business_context or {},
        category_tags=run.category_tags or [],
        budget_used=run.budget_used,
        variant_count=run.variant_count,
        created_at=run.created_at,
        updated_at=run.updated_at,
        stage_tasks=task_views,
        trace_events=trace_views,
        variant_summary=run_variants(db, run.id).get("summary", {}),
        status_explanation=_run_status_explanation(run, tasks),
        latest_scorecard=scorecard,
        latest_forecast=forecast,
        research_context=research_context,
    )


def _stage_label(stage_name: str | None) -> str:
    labels = {
        "intake": "Intake",
        "planning": "Planning",
        "divergence": "Variant strategy",
        "copy_image_generation": "Copy and image generation",
        "video_scripting": "Video scripting",
        "storyboard_image_generation": "Storyboard generation",
        "video_generation": "Video generation",
        "visual_quality_assessment": "Visual QA",
        "evaluation_selection": "Evaluation",
    }
    return labels.get(stage_name or "", (stage_name or "Run").replace("_", " ").title())


def _run_status_explanation(run: PipelineRun, tasks: list[StageTask]) -> RunStatusExplanation:
    current_task = next((task for task in tasks if task.stage_name == run.current_stage), None)
    failed_task = next((task for task in tasks if task.status == "failed"), None)
    task = current_task or failed_task
    stage_label = _stage_label(task.stage_name if task else run.current_stage)

    if run.status == "completed":
        return RunStatusExplanation(
            tone="success",
            headline="Run completed",
            detail="Winner assets and scorecard are ready for review or scheduling.",
            primary_action="Review deliverables",
            next_actions=["Inspect winner assets", "Schedule approved creative", "Import performance feedback later"],
        )

    if run.status == "failed" or (task and task.status == "failed"):
        detail = (task.error_message if task else None) or "The current stage failed before producing a reviewable output."
        return RunStatusExplanation(
            tone="danger",
            headline=f"{stage_label} failed",
            detail=detail,
            primary_action="Reject to retry",
            next_actions=["Read the stage error", "Fix provider/config/input issues", "Reject the stage to requeue it"],
        )

    if task and task.status == "waiting_review":
        return RunStatusExplanation(
            tone="review",
            headline=f"{stage_label} is waiting for review",
            detail="The agent produced a stage handoff and needs an operator decision before the run can continue.",
            primary_action="Review and approve",
            next_actions=["Inspect the stage output", "Approve to continue", "Reject with notes if the output needs regeneration"],
        )

    if task and task.status == "running":
        return RunStatusExplanation(
            tone="info",
            headline=f"{stage_label} is running",
            detail="The worker has claimed this stage. New trace events will appear as the agent progresses.",
            primary_action="Wait for completion",
            next_actions=["Watch Agent Trace", "Refresh async assets if media generation is pending"],
        )

    if task and task.status == "queued":
        return RunStatusExplanation(
            tone="info",
            headline=f"Queued for {stage_label.lower()}",
            detail="This stage is ready for the background worker. It will start when a worker slot is available.",
            primary_action="Wait for worker",
            next_actions=["Check queue health", "Confirm worker is enabled if it stays queued"],
        )

    return RunStatusExplanation(
        tone="info",
        headline="Run is preparing the next stage",
        detail="The pipeline state is being updated. Refresh if this state does not change shortly.",
        primary_action="Refresh run",
        next_actions=["Refresh run detail", "Check Stage Timeline for the last completed stage"],
    )


def _serialize_trace_event(event) -> dict:
    return {
        "id": event.id,
        "run_id": event.run_id,
        "stage_task_id": event.stage_task_id,
        "stage_name": event.stage_name,
        "agent_name": event.agent_name,
        "event_type": event.event_type,
        "visibility": event.visibility,
        "message": event.message,
        "provider_name": event.provider_name,
        "model_name": event.model_name,
        "payload": event.payload or {},
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _sse_event(event_name: str, payload: dict, event_id: str | None = None) -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_name}")
    lines.append(f"data: {json.dumps(payload, default=str)}")
    return "\n".join(lines) + "\n\n"


def _pipeline_mode_views() -> list[PipelineModeView]:
    labels = {
        PipelineMode.COPY_IMAGE_ONLY.value: "Copy + Image",
        PipelineMode.DTC_SITE_IMAGE.value: "DTC Site Image",
        PipelineMode.VIDEO_ONLY.value: "Copy + Video",
        PipelineMode.FULL_MULTIMODAL.value: "Full Multimodal",
        PipelineMode.MARKETPLACE_MAIN_IMAGE.value: "Studio Main Image",
        PipelineMode.TIKTOK_SHOP_VIDEO.value: "TikTok Shop Video",
    }
    views: list[PipelineModeView] = []
    for mode, stages in PIPELINE_STAGE_PLANS.items():
        ordered_agents: list[str] = []
        for stage in stages:
            agent = stage_agent(stage)
            if agent not in ordered_agents:
                ordered_agents.append(agent)
        views.append(
            PipelineModeView(
                mode=mode,
                display_name=labels.get(mode, mode),
                stages=stages,
                agents=ordered_agents,
                agent_count=len(ordered_agents),
            )
        )
    return views


def _serialize_agent_config(row) -> AgentApiConfigView:
    image_cfg = ((row.extra or {}).get("image_config") or {}) if isinstance(row.extra, dict) else {}
    image_key_env = image_cfg.get("api_key_env")
    video_cfg = ((row.extra or {}).get("video_config") or {}) if isinstance(row.extra, dict) else {}
    video_key_env = video_cfg.get("api_key_env")
    # Search tool configs for shop_analyst
    extra_dict = row.extra if isinstance(row.extra, dict) else {}
    tavily_cfg = extra_dict.get("tavily_config") or {}
    firecrawl_cfg = extra_dict.get("firecrawl_config") or {}
    tavily_key_env = tavily_cfg.get("api_key_env")
    firecrawl_key_env = firecrawl_cfg.get("api_key_env")
    thinking_mode = row.thinking_mode or "auto"
    thinking_applied = bool(row.provider_name == "kimi" and row.model_name.startswith("kimi-k") and thinking_mode != "disabled")
    return AgentApiConfigView(
        agent_name=row.agent_name,
        provider_name=row.provider_name,
        model_name=row.model_name,
        api_base_url=row.api_base_url,
        api_key_env=row.api_key_env,
        api_key_available=api_key_available(row.api_key_env),
        image_provider_name=image_cfg.get("provider_name"),
        image_model_name=image_cfg.get("model_name"),
        image_api_base_url=image_cfg.get("api_base_url"),
        image_api_key_env=image_key_env,
        image_api_key_available=api_key_available(image_key_env),
        video_provider_name=video_cfg.get("provider_name"),
        video_model_name=video_cfg.get("model_name"),
        video_api_base_url=video_cfg.get("api_base_url"),
        video_api_key_env=video_key_env,
        video_api_key_available=api_key_available(video_key_env),
        tavily_api_key_env=tavily_key_env,
        tavily_api_key_available=api_key_available(tavily_key_env),
        firecrawl_api_key_env=firecrawl_key_env,
        firecrawl_api_key_available=api_key_available(firecrawl_key_env),
        thinking_mode=row.thinking_mode or "auto",
        thinking_budget_tokens=row.thinking_budget_tokens,
        max_output_tokens=row.max_output_tokens,
        request_timeout_seconds=row.request_timeout_seconds,
        streaming_enabled=bool(row.streaming_enabled),
        thinking_applied=thinking_applied,
        extra=row.extra or {},
        is_default=row.agent_name == "default",
        updated_at=row.updated_at,
    )


# ── Queue monitoring ──────────────────────────────────────


@router.get("/queue/status", response_model=QueueStatusResponse)
def get_queue_status() -> QueueStatusResponse:
    return QueueStatusResponse(**worker.get_queue_status())


@router.get("/queue/running", response_model=list[QueueRunningTask])
def get_queue_running() -> list[QueueRunningTask]:
    return [QueueRunningTask(**item) for item in worker.get_running_tasks()]


@router.get("/queue/health", response_model=QueueHealthResponse)
def get_queue_health() -> QueueHealthResponse:
    return QueueHealthResponse(**worker.get_health())


def _dashboard_shared_js() -> str:
    """Shared JavaScript for dashboard pages: run list, run detail, polling, data sources."""
    return """
        <script>
          let currentRunId = null;
          let pipelineModes = [];
          let dataSources = [];
          let dataSourceSelectInFlight = false;
          let variantBoardFilters = { quality: "", assetType: "", reviewStatus: "", minScore: "", q: "" };
          let runEventSource = null;
          let currentTraceEvents = [];
          let runDetailTimer = null;
          let runDetailLastUpdated = null;
          let retryProgressTimer = null;
          let retryProgressState = null;
          const retryingImageVariants = new Set();
          let runListInterval = null;
          let runNotificationState = null;
          const runNotificationTimers = new Map();
          const runsPanelCollapsedStorageKey = "dashboard_runs_panel_collapsed";
          const variantBoardCollapsedStorageKey = "variant_board_collapsed";
          let runsPanelCollapsed = false;
          let runsCompactRailPositionFrame = null;
          let variantBoardCollapsed = false;
          let expandedVariantId = null;

          function esc(v){ return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");}
          function fmtTime(iso){
            if (!iso) return "-";
            const d = new Date(iso);
            if (isNaN(d.getTime())) return iso;
            const MM = String(d.getMonth()+1).padStart(2,'0');
            const DD = String(d.getDate()).padStart(2,'0');
            const hh = String(d.getHours()).padStart(2,'0');
            const mm = String(d.getMinutes()).padStart(2,'0');
            return MM + "-" + DD + " " + hh + ":" + mm;
          }
          function toList(raw){ return String(raw || "").split(",").map(s => s.trim()).filter(Boolean); }
          function parseJsonObject(raw){
            if (!raw || !raw.trim()) return {};
            try { return JSON.parse(raw); } catch (_e) { throw new Error("Advanced Business Context JSON is invalid."); }
          }
          function mediaUrl(path){ return `/media?path=${encodeURIComponent(path || "")}`; }
          function mediaViewUrl(path){
            const returnTo = currentRunId ? `/dashboard#run=${encodeURIComponent(currentRunId)}` : "/dashboard";
            return `/media/view?path=${encodeURIComponent(path || "")}&return_to=${encodeURIComponent(returnTo)}`;
          }
          function loadRunsPanelCollapsedState() {
            try {
              runsPanelCollapsed = localStorage.getItem(runsPanelCollapsedStorageKey) === "true";
            } catch (_err) {
              runsPanelCollapsed = false;
            }
          }
          function persistRunsPanelCollapsedState() {
            try {
              localStorage.setItem(runsPanelCollapsedStorageKey, runsPanelCollapsed ? "true" : "false");
            } catch (_err) {}
          }
          function canCollapseRunsPanel() {
            return window.matchMedia("(min-width: 1101px)").matches;
          }
          function updateRunsCompactRailPosition() {
            const compactRail = document.getElementById("runs-panel-compact-rail");
            const globalRail = document.querySelector(".global-rail");
            const detailPanel = document.getElementById("run-detail-panel");
            if (!compactRail || !globalRail || !detailPanel || !canCollapseRunsPanel()) {
              if (compactRail) compactRail.style.removeProperty("--runs-panel-compact-left");
              return;
            }
            const railRect = globalRail.getBoundingClientRect();
            const detailRect = detailPanel.getBoundingClientRect();
            const compactWidth = compactRail.getBoundingClientRect().width || 58;
            const gap = detailRect.left - railRect.right;
            if (!Number.isFinite(gap) || gap <= compactWidth) {
              compactRail.style.removeProperty("--runs-panel-compact-left");
              return;
            }
            const sideGap = (gap - compactWidth) / 2;
            const left = railRect.right + sideGap;
            compactRail.style.setProperty("--runs-panel-compact-left", `${Math.round(left)}px`);
          }
          function queueRunsCompactRailPosition() {
            if (runsCompactRailPositionFrame) cancelAnimationFrame(runsCompactRailPositionFrame);
            runsCompactRailPositionFrame = requestAnimationFrame(() => {
              runsCompactRailPositionFrame = requestAnimationFrame(() => {
                runsCompactRailPositionFrame = null;
                updateRunsCompactRailPosition();
              });
            });
          }
          function syncRunsPanelState() {
            const dock = document.getElementById("runs-dock");
            const toggle = document.getElementById("runs-panel-toggle");
            const icon = document.getElementById("runs-panel-toggle-icon");
            const compactRail = document.getElementById("runs-panel-compact-rail");
            const shell = document.querySelector(".dashboard-shell");
            if (!dock) return;
            const canCollapse = canCollapseRunsPanel() && !!currentRunId;
            const collapsed = canCollapse && runsPanelCollapsed;
            if (!currentRunId && runsPanelCollapsed) {
              runsPanelCollapsed = false;
              persistRunsPanelCollapsedState();
            }
            dock.classList.toggle("is-collapsed", collapsed);
            if (toggle) {
              toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
              toggle.title = !currentRunId
                ? "Select a run before collapsing the runs list"
                : collapsed
                  ? "Expand runs list"
                  : "Collapse runs list";
              toggle.disabled = !currentRunId;
            }
            if (icon) icon.textContent = collapsed ? "→" : "←";
            if (compactRail) compactRail.setAttribute("aria-hidden", collapsed ? "false" : "true");
            if (shell) shell.classList.toggle("is-hero-hidden", collapsed);
            queueRunsCompactRailPosition();
          }
          function toggleRunsPanel(forceState) {
            if (!canCollapseRunsPanel()) {
              runsPanelCollapsed = false;
              syncRunsPanelState();
              return;
            }
            if (!currentRunId) {
              runsPanelCollapsed = false;
              syncRunsPanelState();
              return;
            }
            runsPanelCollapsed = typeof forceState === "boolean" ? forceState : !runsPanelCollapsed;
            persistRunsPanelCollapsedState();
            syncRunsPanelState();
          }
          function autoCollapseRunsPanel() {
            if (!canCollapseRunsPanel() || runsPanelCollapsed) return;
            runsPanelCollapsed = true;
            persistRunsPanelCollapsedState();
            syncRunsPanelState();
          }
          function loadVariantBoardCollapsedState() {
            try {
              variantBoardCollapsed = localStorage.getItem(variantBoardCollapsedStorageKey) === "true";
            } catch (_err) {
              variantBoardCollapsed = false;
            }
          }
          function persistVariantBoardCollapsedState() {
            try {
              localStorage.setItem(variantBoardCollapsedStorageKey, variantBoardCollapsed ? "true" : "false");
            } catch (_err) {}
          }

          async function api(path, options = {}) {
            const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
            if (!res.ok) { throw new Error(await res.text()); }
            return res.headers.get("content-type")?.includes("application/json") ? res.json() : res.text();
          }

          function retryWaitingTask(run) {
            const tasks = run?.stage_tasks || [];
            return tasks.find((task) => {
              const waiting = task.metadata_json?.waiting_state || {};
              return task.status === "queued" && waiting.status === "waiting_retry" && waiting.retry_at;
            }) || null;
          }

          function formatRetryRemaining(ms) {
            const total = Math.max(0, Math.ceil(ms / 1000));
            const minutes = String(Math.floor(total / 60)).padStart(2, "0");
            const seconds = String(total % 60).padStart(2, "0");
            return `${minutes}:${seconds}`;
          }

          function renderRetryProgress() {
            const box = document.getElementById("fab-retry-progress");
            if (!box || !retryProgressState) return;
            const fill = document.getElementById("fab-retry-fill");
            const label = document.getElementById("fab-retry-label");
            const time = document.getElementById("fab-retry-time");
            const remainingMs = retryProgressState.retryAt - Date.now();
            const totalMs = Math.max(1000, retryProgressState.delaySeconds * 1000);
            const pct = Math.max(0, Math.min(100, ((totalMs - Math.max(0, remainingMs)) / totalMs) * 100));
            box.classList.add("visible");
            if (fill) fill.style.width = `${pct}%`;
            if (label) label.textContent = `${retryProgressState.stageName} retry ${retryProgressState.nextAttempt}/${retryProgressState.maxAttempts}`;
            if (time) time.textContent = formatRetryRemaining(remainingMs);
            if (remainingMs <= 0) {
              if (time) time.textContent = "queued";
              if (fill) fill.style.width = "100%";
            }
          }

          function setRetryProgressFromRun(run) {
            const task = retryWaitingTask(run);
            const box = document.getElementById("fab-retry-progress");
            if (!task) {
              retryProgressState = null;
              if (retryProgressTimer) {
                clearInterval(retryProgressTimer);
                retryProgressTimer = null;
              }
              if (box) box.classList.remove("visible");
              return;
            }
            const waiting = task.metadata_json.waiting_state || {};
            retryProgressState = {
              stageName: task.stage_name || "stage",
              retryAt: new Date(waiting.retry_at).getTime(),
              delaySeconds: Number(waiting.retry_delay_seconds || 1),
              nextAttempt: waiting.next_attempt || Number(task.attempt || 0) + 1,
              maxAttempts: waiting.max_attempts || task.max_retries || 4,
            };
            if (!Number.isFinite(retryProgressState.retryAt)) {
              retryProgressState = null;
              if (box) box.classList.remove("visible");
              return;
            }
            renderRetryProgress();
            if (!retryProgressTimer) retryProgressTimer = setInterval(renderRetryProgress, 1000);
          }

          async function loadDataSources() {
            const data = await api("/dashboard/data-sources");
            dataSources = data.items || [];
            const sel = document.getElementById("data-source-select");
            sel.innerHTML = "";
            dataSources.forEach((item) => {
              const opt = document.createElement("option");
              opt.value = item.url;
              opt.textContent = item.name;
              if (item.is_active) opt.selected = true;
              sel.appendChild(opt);
            });
            const active = dataSources.find((x) => x.is_active);
            document.getElementById("data-source-path").textContent = active ? active.path : data.active_url;
          }

          async function switchDataSource() {
            if (dataSourceSelectInFlight) return;
            const url = document.getElementById("data-source-select").value;
            if (!url) return;
            dataSourceSelectInFlight = true;
            try {
              await api("/dashboard/data-sources/select", { method: "POST", body: JSON.stringify({ url }) });
              await loadDataSources();
              await refreshRuns();
              if (currentRunId) {
                try { await selectRun(currentRunId); } catch (_err) {
                  currentRunId = null;
                  const detail = document.getElementById("run-detail");
                  detail.classList.add("run-detail-empty");
                  detail.classList.remove("run-detail-content");
                  detail.innerHTML = "Select a run.";
                  syncRunsPanelState();
                }
              }
            } finally {
              dataSourceSelectInFlight = false;
            }
          }

          function statusPillClass(status) {
            const s = String(status || "").toLowerCase();
            if (s === "running") return "status-pill running";
            if (s === "waiting_review") return "status-pill waiting_review";
            if (s === "completed") return "status-pill completed";
            if (s === "failed") return "status-pill failed";
            if (s === "rejected") return "status-pill rejected";
            return "status-pill draft";
          }
          function statusLabel(status) {
            const s = String(status || "").toLowerCase();
            if (s === "waiting_review") return "REVIEW";
            return String(status || "draft").toUpperCase();
          }
          function modeLabel(mode) {
            const m = String(mode || "");
            if (m === "full_multimodal") return "Full";
            if (m === "video_only") return "Copy + Video";
            if (m === "copy_image_only") return "Image";
            if (m === "dtc_site_image") return "DTC Site Image";
            if (m === "marketplace_main_image") return "Studio Main Image";
            return m;
          }

          function runNotificationKey(run) {
            return [run.status || "", run.current_stage || "", run.updated_at || ""].join("|");
          }

          function payloadHasProcessingAssets(payload) {
            const rows = [
              ...(payload?.image_assets || []),
              ...(payload?.frames || []),
              ...(payload?.videos || []),
            ];
            return rows.some((item) => {
              const status = String(item?.generation_status || item?.status || "").toLowerCase();
              return (item?.external_task_id && ["", "submitted", "queued", "pending", "processing", "running"].includes(status))
                || item?.source === "external_task_pending"
                || item?.source === "segmented_pending";
            });
          }

          function reviewNotificationReady(run) {
            const task = currentReviewTask(run);
            return !!task && !payloadHasProcessingAssets(task.output_payload || {});
          }

          function dismissRunNotification(runId) {
            const toast = document.querySelector(`.run-notification[data-run-id="${CSS.escape(runId)}"]`);
            if (toast) toast.remove();
            const timer = runNotificationTimers.get(runId);
            if (timer) clearTimeout(timer);
            runNotificationTimers.delete(runId);
          }

          async function openRunNotification(runId) {
            dismissRunNotification(runId);
            await selectRun(runId);
            const panel = document.getElementById("run-detail-panel");
            if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
          }

          function showRunNotification(run, kind) {
            const stack = document.getElementById("run-notification-stack");
            if (!stack) return;
            dismissRunNotification(run.id);
            const toast = document.createElement("div");
            toast.className = `run-notification ${kind}`;
            toast.dataset.runId = run.id;
            const title = kind === "completed" ? "Run completed" : "Review needed";
            const stage = run.current_stage || "done";
            toast.innerHTML = `
              <div onclick="openRunNotification('${run.id}')">
                <div class="run-notification-title">${title}</div>
                <div class="run-notification-meta">${esc(run.id.slice(0, 8))} - ${esc(stage)} - ${esc(modeLabel(run.pipeline_mode))}</div>
              </div>
              <button class="run-notification-close" onclick="event.stopPropagation(); dismissRunNotification('${run.id}')" title="Dismiss">&times;</button>
            `;
            stack.prepend(toast);
            runNotificationTimers.set(run.id, setTimeout(() => dismissRunNotification(run.id), 12000));
          }

          async function notifyRunTransitions(rows) {
            if (!runNotificationState) {
              runNotificationState = new Map(rows.map((run) => [run.id, runNotificationKey(run)]));
              return;
            }
            const next = new Map(runNotificationState || []);
            for (const run of rows) {
              const current = runNotificationKey(run);
              const prior = runNotificationState?.get(run.id);
              if (prior === current) continue;
              if (run.status === "waiting_review") {
                const fullRun = await api(`/runs/${run.id}`).catch(() => null);
                if (!fullRun || !reviewNotificationReady(fullRun)) continue;
                showRunNotification(run, "review");
              } else if (run.status === "completed") {
                showRunNotification(run, "completed");
              }
              next.set(run.id, current);
            }
            runNotificationState = next;
          }

          async function refreshRuns() {
            const rows = await api("/runs");
            await notifyRunTransitions(rows);
            const body = document.getElementById("runs-body");
            body.innerHTML = "";
            if (!rows.length) {
              const tr = document.createElement("tr");
              tr.innerHTML = `<td colspan="4" class="muted">No runs available in current data source.</td>`;
              body.appendChild(tr);
              return;
            }
            rows.forEach((r) => {
              const tr = document.createElement("tr");
              if (r.id === currentRunId) tr.classList.add("selected");
              tr.innerHTML = `<td><a href="#" onclick="selectRun('${r.id}');return false;">${r.id.slice(0,8)}</a></td><td><span class="${statusPillClass(r.status)}">${statusLabel(r.status)}</span></td><td>${modeLabel(r.pipeline_mode)}</td><td>${fmtTime(r.updated_at)}</td>`;
              body.appendChild(tr);
            });
          }

          function renderDeliverables(deliverables) {
            const winner = deliverables?.winner_variant_id || "-";
            const copy = deliverables?.deliverables?.copy_variant || null;
            const images = (deliverables?.deliverables?.image_assets || []).filter((asset) => !failedMediaAsset(asset));
            const rawVideo = deliverables?.deliverables?.video_asset || null;
            const video = failedMediaAsset(rawVideo) ? null : rawVideo;
            const image = images.length ? images[0] : null;
            const scoreAction = deliverables?.score?.winner?.recommended_action || deliverables?.score?.forecast?.recommended_action || deliverables?.score?.recommended_action || "-";
            return `
              <h3>Deliverables Overview</h3>
              <div class="muted">winner: ${esc(winner)} | recommendation: ${esc(scoreAction)}</div>
              <div class="deliverables">
                <article class="deliverable-card">
                  <div class="stage-title">Copy</div>
                  ${copy ? `
                    <div><b>${esc(copy.headline || "-")}</b></div>
                    <div>${esc(copy.primary_text || "-")}</div>
                    <div class="muted">CTA: ${esc(copy.call_to_action || "-")}</div>
                  ` : '<div class="muted">No copy winner yet.</div>'}
                </article>
                <article class="deliverable-card">
                  <div class="stage-title">Image</div>
                  ${image ? `
                    <a href="${mediaViewUrl(image.uri)}" target="_blank">
                      <img class="media-preview image" src="${mediaUrl(image.uri)}" alt="generated image" />
                    </a>
                    <div class="muted">${esc(image.aspect_ratio || "1:1")} | ${esc(image.uri)}</div>
                  ` : '<div class="muted">No image winner yet.</div>'}
                </article>
                <article class="deliverable-card">
                  <div class="stage-title">Video</div>
                  ${video ? `
                    <a href="${mediaViewUrl(video.video_uri)}" target="_blank" class="muted">Open video</a>
                    <video controls playsinline class="media-preview video" src="${mediaUrl(video.video_uri)}"></video>
                    <div class="muted">${esc(video.video_uri)}</div>
                  ` : '<div class="muted">No video winner yet.</div>'}
                </article>
              </div>
            `;
          }

          function failedMediaAsset(asset) {
            return asset?.failure_category || asset?.error || asset?.source === "generation_error" || String(asset?.uri || "").includes("_generation_error.");
          }

          function latestScore(item, scoreType){
            const rows = (item?.scores || []).filter((row) => row.score_type === scoreType);
            return rows.length ? rows[rows.length - 1] : null;
          }

          function assetsByType(item, type){
            return (item?.assets || []).filter((asset) => asset.asset_type === type);
          }

          function assetGenerationState(asset){
            const payload = asset?.payload || {};
            const status = String(payload.generation_status || "").toLowerCase();
            if (asset?.failure_category || payload.error || payload.source === "generation_error") return "failed";
            if (["submitted", "queued", "pending", "processing", "running"].includes(status) || payload.source === "external_task_pending") return "processing";
            if (status === "completed" || ["url", "b64_json"].includes(payload.source)) return "completed";
            return "unknown";
          }

          function preferredImageAsset(images){
            const rows = [...(images || [])].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
            return rows.find((asset) => assetGenerationState(asset) === "processing")
              || rows.find((asset) => assetGenerationState(asset) === "completed")
              || rows[0]
              || null;
          }

          function qualitySummary(item){
            return item?.quality_summary || {};
          }

          function qualityFlags(item){
            return qualitySummary(item).quality_flags || [];
          }

          function qualityChipClass(flag){
            if (["ready_to_review", "winner", "shortlisted"].includes(flag)) return "good";
            if (["processing_assets", "missing_assets", "compliance_attention", "low_score", "pending_review", "visual_qa_attention", "visual_qa_needs_frame_review", "visual_qa_remote_unchecked", "visual_qa_aspect_mismatch", "visual_qa_low_information", "visual_qa_video_header_unverified", "media_gate_asset_processing", "media_gate_aspect_mismatch", "media_gate_low_information"].includes(flag)) return "warn";
            if (["failed_assets", "media_issue", "operator_quality_issue", "needs_regeneration", "rejected", "visual_qa_failed", "visual_qa_placeholder", "visual_qa_empty_video", "visual_qa_decode_error", "visual_qa_empty_file", "visual_qa_missing_file", "media_gate_generation_error", "media_gate_placeholder", "media_gate_empty_video", "media_gate_decode_error", "media_gate_empty_file", "media_gate_missing_file", "media_gate_missing_uri"].includes(flag)) return "bad";
            return "";
          }
          function qualityChipLabel(flag){
            const titleize = (value) => String(value || "-")
              .replaceAll("_", " ")
              .split(" ")
              .filter(Boolean)
              .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
              .join(" ");
            const labels = {
              ready_to_review: "Ready",
              winner: "Winner",
              shortlisted: "Picked",
              processing_assets: "Processing",
              missing_assets: "Missing assets",
              compliance_attention: "Needs compliance check",
              low_score: "Low score",
              pending_review: "Pending review",
              visual_qa_attention: "Needs visual QA",
              visual_qa_needs_frame_review: "Needs frame review",
              visual_qa_remote_unchecked: "Remote QA unchecked",
              visual_qa_aspect_mismatch: "Aspect mismatch",
              visual_qa_low_information: "Low detail",
              visual_qa_video_header_unverified: "Video header unchecked",
              media_gate_asset_processing: "Media processing",
              media_gate_aspect_mismatch: "Aspect mismatch",
              media_gate_low_information: "Low detail",
              failed_assets: "Asset failed",
              media_issue: "Media issue",
              operator_quality_issue: "Marked issue",
              needs_regeneration: "Needs redo",
              rejected: "Rejected",
              visual_qa_failed: "Visual QA failed",
              visual_qa_placeholder: "Placeholder asset",
              visual_qa_empty_video: "Empty video",
              visual_qa_decode_error: "Decode error",
              visual_qa_empty_file: "Empty file",
              visual_qa_missing_file: "Missing file",
              media_gate_generation_error: "Generation failed",
              media_gate_placeholder: "Placeholder asset",
              media_gate_empty_video: "Empty video",
              media_gate_decode_error: "Decode error",
              media_gate_empty_file: "Empty file",
              media_gate_missing_file: "Missing file",
              media_gate_missing_uri: "Missing file link",
            };
            return labels[flag] || titleize(flag);
          }
          function qualitySurfaceBadges(summary){
            const badges = [];
            const frameFlags = summary?.frame_review_flags || [];
            const refCount = Number(summary?.reference_source_count || 0);
            if (frameFlags.length) badges.push('<span class="quality-chip warn">Frame review</span>');
            if (refCount > 0) badges.push(`<span class="quality-chip good">Ref-backed${refCount > 1 ? ` ${esc(refCount)}` : ""}</span>`);
            return badges.join("");
          }

          function variantMatchesOperationalFilters(item){
            const quality = variantBoardFilters.quality;
            const assetType = variantBoardFilters.assetType;
            const reviewStatus = variantBoardFilters.reviewStatus;
            const minScoreRaw = variantBoardFilters.minScore;
            const q = String(variantBoardFilters.q || "").trim().toLowerCase();
            const summary = qualitySummary(item);
            const flags = new Set(summary.quality_flags || []);
            if (quality && summary.quality_status !== quality && !flags.has(quality)) return false;
            if (assetType && !(summary.asset_counts || {})[assetType]) return false;
            if (reviewStatus && (item.review_status || "") !== reviewStatus) return false;
            if (minScoreRaw) {
              const minScore = Number(minScoreRaw);
              const score = Number(summary.score ?? item.current_score);
              if (!Number.isFinite(minScore) || !Number.isFinite(score) || score < minScore) return false;
            }
            if (q) {
              const haystack = [item.variant_id, item.angle, item.hook, item.message, item.strategy_brief?.rationale].join(" ").toLowerCase();
              if (!haystack.includes(q)) return false;
            }
            return true;
          }

          async function updateVariantFilter(field, value){
            variantBoardFilters = { ...variantBoardFilters, [field]: value };
            if (currentRunId) await selectRun(currentRunId);
          }

          function resetVariantFilters(){
            variantBoardFilters = { quality: "", assetType: "", reviewStatus: "", minScore: "", q: "" };
            if (currentRunId) selectRun(currentRunId);
          }
          function variantBoardToggleLabel() {
            return variantBoardCollapsed ? "Expand Variant Board" : "Collapse Variant Board";
          }
          function toggleVariantBoard(){
            variantBoardCollapsed = !variantBoardCollapsed;
            persistVariantBoardCollapsedState();
            const body = document.getElementById("variant-board-body");
            if (body) body.classList.toggle("is-collapsed", variantBoardCollapsed);
            const btn = document.getElementById("variant-board-toggle");
            if (btn) btn.textContent = variantBoardToggleLabel();
          }

          async function variantAction(runId, variantId, endpoint, body){
            await api(endpoint, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body)
            });
            await selectRun(runId);
          }

          async function refreshAsyncAssets(runId){
            await api(`/runs/${runId}/assets/refresh`, { method: "POST" });
            await selectRun(runId);
          }

          async function requestVariantRegeneration(runId, variantId){
            const targetStage = window.prompt("Target stage: copy_image_generation, video_scripting, storyboard_image_generation, or video_generation. Leave blank for default.", "");
            if (targetStage === null) return;
            const reason = window.prompt("Regeneration reason", "dashboard regeneration request");
            if (reason === null) return;
            await variantAction(runId, variantId, `/runs/${runId}/variants/${variantId}/regenerate`, {
              reason: reason || "dashboard regeneration request",
              target_stage: targetStage.trim() || null
            });
          }

          function setImageRetryLoading(runId, variantId, loading){
            const key = `${runId}:${variantId}`;
            document.querySelectorAll("[data-image-retry-run][data-image-retry-variant]").forEach((btn) => {
              if (`${btn.dataset.imageRetryRun}:${btn.dataset.imageRetryVariant}` !== key) return;
              btn.disabled = loading;
              btn.classList.toggle("is-loading", loading);
              btn.innerHTML = loading ? '<span class="retry-spinner" aria-hidden="true"></span><span>Waiting</span>' : "Retry";
              btn.title = loading ? "Retrying image generation" : "Retry image generation";
            });
          }

          async function waitForRetriedImage(runId, variantId){
            for (let i = 0; i < 18; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, i < 2 ? 3000 : 10000));
              await api(`/runs/${runId}/assets/refresh`, { method: "POST" });
              const variants = await api(`/runs/${runId}/variants`).catch(() => null);
              const item = (variants?.items || []).find((row) => row.variant_id === variantId);
              const image = preferredImageAsset(assetsByType(item, "image"));
              const state = assetGenerationState(image);
              if (state === "completed" || state === "failed") return { state, image };
            }
            return { state: "processing", image: null };
          }

          async function retryVariantImage(event, runId, variantId){
            event?.stopPropagation();
            if (!window.confirm(`Retry image generation for ${variantId}?`)) return;
            const key = `${runId}:${variantId}`;
            retryingImageVariants.add(key);
            setImageRetryLoading(runId, variantId, true);
            try {
              await api(`/runs/${runId}/variants/${variantId}/assets/image/retry`, {
                method: "POST",
                body: JSON.stringify({ reason: "retry image asset from dashboard" })
              });
              await selectRun(runId);
              const result = await waitForRetriedImage(runId, variantId);
              await selectRun(runId);
              if (result.state === "completed") alert(`Image retry completed for ${variantId}.`);
              else if (result.state === "failed") alert(`Image retry failed for ${variantId}. Check failure reasons.`);
              else alert(`Image retry is still processing for ${variantId}. Use Refresh Assets to check again.`);
            } catch (err) {
              alert(err?.message || "Image retry failed.");
            } finally {
              retryingImageVariants.delete(key);
              setImageRetryLoading(runId, variantId, false);
            }
          }

          async function scheduleVariantQuick(variantId, runId, hook){
            const wsSelect = document.getElementById('workspace_name');
            const wsName = wsSelect?.value || 'workspace_demo';
            const today = new Date().toISOString().slice(0,10);
            const title = (hook || 'Variant ' + variantId.slice(0,8)).slice(0,256);
            // Show inline form instead of auto-creating
            const dateStr = window.prompt('Schedule date (YYYY-MM-DD):', today);
            if (!dateStr) return;
            const channel = window.prompt('Channel (meta / tiktok / youtube / google / amazon):', 'meta');
            if (!channel) return;
            try {
              const shopsResp = await fetch('/shops?limit=50');
              const shopsData = await shopsResp.json();
              const shops = shopsData.shops || shopsData.items || [];
              const shop = shops.find(function(s){ return s.name === wsName; }) || shops[0] || {};
              const wsId = shop.id || wsName;
              const projResp = await fetch('/projects?workspace_name=' + encodeURIComponent(wsName));
              const projData = await projResp.json();
              const projId = (projData && projData[0]) ? projData[0].id : (wsName + '_project');
              const r = await fetch('/content-schedules', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({
                  workspace_id: wsId, project_id: projId, variant_id: variantId,
                  title: title, channel: channel, scheduled_date: dateStr
                })
              });
              if (!r.ok) { const err = await r.json(); throw new Error(err.detail || 'Failed'); }
              const created = await r.json();
              const notionMsg = created.notion_sync_error ? ' (Notion sync: ' + created.notion_sync_error + ')' : '';
              alert('Scheduled for ' + dateStr + ' on ' + channel + '.' + notionMsg + ' Open Content Calendar to view.');
              // Refresh to show schedule indicator
              if (window.__lastRunId) selectRun(window.__lastRunId);
            } catch(e){
              alert('Schedule failed: ' + e.message);
            }
          }

          function scoreColorClass(score) {
            if (score == null) return "";
            if (score >= 80) return "high";
            if (score >= 60) return "mid";
            return "low";
          }

          function renderScoreBreakdown(item) {
            const evaluation = latestScore(item, "evaluation");
            const subs = evaluation?.sub_scores || {};
            const bars = [
              { key: "hook_strength", label: "Hook" },
              { key: "clarity", label: "Clarity" },
              { key: "generation_fit", label: "Gen Fit" },
              { key: "visual_qa", label: "Vis QA" },
              { key: "compliance", label: "Compliance" },
              { key: "ai_naturalness", label: "AI Natural" },
            ];
            return bars.map((b) => {
              const val = subs[b.key] != null ? Number(subs[b.key]) : 0;
              return `
                <div class="score-item">
                  <span>${b.label}</span>
                  <div class="bar"><div class="bar-fill" style="width:${Math.min(100, val)}%"></div></div>
                  <span style="min-width:36px;text-align:right;font-weight:700;">${Math.round(val)}</span>
                </div>
              `;
            }).join("");
          }

          function toggleVariantDetail(runId, variantId) {
            const panel = document.getElementById("variant-detail-panel");
            const cards = document.querySelectorAll(".variant-score-card");
            if (expandedVariantId === variantId) {
              expandedVariantId = null;
              if (panel) {
                panel.classList.remove("open");
                panel.classList.add("is-closing");
                const closeMs = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--modal-close-dur")) || 150;
                setTimeout(() => panel.classList.remove("is-closing"), closeMs);
              }
              cards.forEach((c) => c.classList.remove("selected"));
              return;
            }
            expandedVariantId = variantId;
            cards.forEach((c) => {
              c.classList.toggle("selected", c.dataset.variantId === variantId);
            });
            if (panel) {
              panel.classList.remove("is-closing");
              panel.innerHTML = renderVariantDetail(runId, variantId);
              panel.classList.add("open");
              panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
            }
          }

          function renderVariantDetail(runId, variantId) {
            const allItems = window.__lastVariants?.items || [];
            const item = allItems.find((v) => v.variant_id === variantId);
            if (!item) return '<div class="muted">Variant not found.</div>';
            const copy = assetsByType(item, "copy")[0]?.payload || null;
            const images = assetsByType(item, "image");
            const image = preferredImageAsset(images);
            const script = assetsByType(item, "video_script")[0]?.payload || null;
            const videoAsset = assetsByType(item, "video")[0] || null;
            const video = videoAsset?.payload || null;
            const evaluation = latestScore(item, "evaluation");
            const score = evaluation?.total_score;
            const qSummary = qualitySummary(item);
            const flags = qualityFlags(item);
            const qualityChips = flags.map((flag) => `<span class="quality-chip ${qualityChipClass(flag)}">${esc(qualityChipLabel(flag))}</span>`).join("");
            const surfaceBadges = qualitySurfaceBadges(qSummary);
            return `
              <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                <div>
                  <b style="font-size:18px;">${esc(item.variant_id)}</b>
                  <span class="muted"> · ${esc(item.angle || "-")}</span>
                  ${item.is_winner ? '<span class="quality-chip good">Winner</span>' : ''}
                  ${item.shortlisted ? '<span class="quality-chip good">Picked</span>' : ''}
                </div>
                <button onclick="(()=>{const p=document.getElementById('variant-detail-panel');expandedVariantId=null;p.classList.remove('open');p.classList.add('is-closing');const m=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--modal-close-dur'))||150;setTimeout(()=>p.classList.remove('is-closing'),m);document.querySelectorAll('.variant-score-card').forEach(c=>c.classList.remove('selected'));})()" style="font-size:12px;">Close</button>
              </div>
              <div class="variant-detail-grid" style="margin-top:14px;">
                <div>
                  ${image && assetGenerationState(image) === "completed" ? `<a href="${mediaViewUrl(image.uri)}" target="_blank"><img class="detail-image" src="${mediaUrl(image.uri)}" alt="variant image" /></a>` : `<div class="muted">${assetGenerationState(image) === "processing" ? "Image processing." : assetGenerationState(image) === "failed" ? "Image failed." : "No image asset."}</div>`}
                  ${video?.video_uri ? `
                    <div style="margin-top:10px;">
                      <video controls playsinline class="media-preview video" src="${mediaUrl(video.video_uri)}" style="max-height:400px;"></video>
                    </div>
                  ` : ''}
                </div>
                <div>
                  <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:10px;">
                    <span class="score-number ${scoreColorClass(score)}" style="font-size:42px;">${score != null ? Math.round(score) : "-"}</span>
                    <span class="muted">/ 100</span>
                    <span class="quality-chip ${qualityChipClass(qSummary.quality_status)}">${esc(qualityChipLabel(qSummary.quality_status || "-"))}</span>
                  </div>
                  <div class="variant-score-breakdown">${renderScoreBreakdown(item)}</div>
                  <div class="quality-row" style="margin-top:10px;">${surfaceBadges}${qualityChips}</div>
                </div>
              </div>
              <div style="margin-top:14px;">
                <div><b>Hook</b>: ${esc(item.hook || "-")}</div>
                <div style="margin-top:6px;"><b>Message</b>: ${esc(item.message || "-")}</div>
                <div style="margin-top:6px;"><b>Headline</b>: ${esc(copy?.headline || "-")}</div>
                <div class="muted">${esc(copy?.primary_text || "-")}</div>
                ${script ? `<div style="margin-top:8px;"><b>Script</b>: ${esc(script.hook || "-")}<div class="muted">${esc(script.script || "-")}</div></div>` : ''}
                <div class="muted" style="margin-top:6px;">CTA: ${esc(copy?.call_to_action || "-")} | action: ${esc(evaluation?.recommended_action || "-")}</div>
              </div>
              ${(qSummary.review_hints || []).length ? `
                <div style="margin-top:12px;">
                  <div><b>Review Hints</b></div>
                  <div class="muted" style="display:flex;flex-direction:column;gap:4px;margin-top:6px;">
                    ${(qSummary.review_hints || []).map((hint) => `<div>- ${esc(hint)}</div>`).join("")}
                  </div>
                </div>
              ` : ''}
              <div class="variant-detail-actions">
                <button onclick="variantAction('${runId}', '${variantId}', '/runs/${runId}/variants/${variantId}/review', {action:'approve_variant', comment:'approved from dashboard'})">Approve</button>
                <button onclick="variantAction('${runId}', '${variantId}', '/runs/${runId}/variants/${variantId}/review', {action:'reject_variant', comment:'rejected from dashboard'})">Reject</button>
                <button onclick="variantAction('${runId}', '${variantId}', '/runs/${runId}/variants/${variantId}/select', {shortlist:true, comment:'shortlisted from dashboard'})">Pick</button>
                <button class="primary" onclick="variantAction('${runId}', '${variantId}', '/runs/${runId}/variants/${variantId}/select', {winner:true, comment:'winner chosen from dashboard'})">Set Winner</button>
                <button onclick="requestVariantRegeneration('${runId}', '${variantId}')">Regenerate</button>
                <button onclick="scheduleVariantQuick('${variantId}','${runId}','${(item.hook||'').replace(/'/g,\"\\\\'\")}')" style="background:var(--soft);border-color:var(--accent);color:var(--accent);">+ Calendar</button>
              </div>
            `;
          }

          async function loadScheduleIndicators(){
            const cards = document.querySelectorAll('.variant-score-card');
            if (!cards.length) return;
            const variantIds = [];
            cards.forEach(function(c){ variantIds.push(c.getAttribute('data-variant-id')); });
            const wsSelect = document.getElementById('workspace_name');
            const wsName = wsSelect?.value || 'workspace_demo';
            try {
              const shopsResp = await fetch('/shops?limit=50');
              const shopsData = await shopsResp.json();
              const shops = shopsData.shops || shopsData.items || [];
              const shop = shops.find(function(s){ return s.name === wsName; }) || shops[0] || {};
              const wsId = shop.id || wsName;
              const r = await fetch('/content-schedules?workspace_id=' + encodeURIComponent(wsId) + '&start_date=2020-01-01&end_date=2099-12-31');
              const data = await r.json();
              const scheduled = new Set();
              (data.items || []).forEach(function(s){
                if (s.variant_id) scheduled.add(s.variant_id);
              });
              cards.forEach(function(c){
                const vid = c.getAttribute('data-variant-id');
                if (scheduled.has(vid)){
                  const s = (data.items || []).find(function(x){ return x.variant_id === vid; });
                  const label = s ? (s.scheduled_date + (s.channel ? ' ' + s.channel : '')) : '';
                  let badge = c.querySelector('.schedule-badge');
                  if (!badge){
                    badge = document.createElement('span');
                    badge.className = 'schedule-badge';
                    badge.title = label;
                    badge.textContent = 'Scheduled: ' + label;
                    badge.style.cssText = 'display:inline-block;font-size:10px;color:var(--accent);margin-top:3px;';
                    c.appendChild(badge);
                  }
                }
              });
            } catch(e){ /* silently skip */ }
          }

          function renderVariantBoard(runId, variants){
            window.__lastVariants = variants;
            const allItems = variants?.items || [];
            const items = allItems.filter(variantMatchesOperationalFilters);
            const summary = variants?.summary || {};
            const qualityCounts = summary.quality_flag_counts || {};
            const header = `<div class="muted">showing: ${esc(items.length)} / ${esc(summary.total || allItems.length || 0)} | shortlisted: ${esc(summary.shortlisted_count || 0)} | winners: ${esc(summary.winner_count || 0)} | regen requests: ${esc(summary.regeneration_requested_count || 0)} | asset issues: ${esc((qualityCounts.failed_assets || 0) + (qualityCounts.media_issue || 0))} | processing: ${esc(qualityCounts.processing_assets || 0)}</div>`;
            const filters = `
              <div class="variant-filter-bar">
                <div>
                  <label>Quality</label>
                  <select onchange="updateVariantFilter('quality', this.value)">
                    <option value="" ${variantBoardFilters.quality === "" ? "selected" : ""}>All</option>
                    <option value="ready_to_review" ${variantBoardFilters.quality === "ready_to_review" ? "selected" : ""}>Ready</option>
                    <option value="winner" ${variantBoardFilters.quality === "winner" ? "selected" : ""}>Winner</option>
                    <option value="shortlisted" ${variantBoardFilters.quality === "shortlisted" ? "selected" : ""}>Shortlisted</option>
                    <option value="pending_review" ${variantBoardFilters.quality === "pending_review" ? "selected" : ""}>Pending</option>
                    <option value="processing_assets" ${variantBoardFilters.quality === "processing_assets" ? "selected" : ""}>Processing</option>
                    <option value="failed_assets" ${variantBoardFilters.quality === "failed_assets" ? "selected" : ""}>Failed</option>
                    <option value="visual_qa_attention" ${variantBoardFilters.quality === "visual_qa_attention" ? "selected" : ""}>QA Attn</option>
                    <option value="visual_qa_failed" ${variantBoardFilters.quality === "visual_qa_failed" ? "selected" : ""}>QA Fail</option>
                    <option value="needs_regeneration" ${variantBoardFilters.quality === "needs_regeneration" ? "selected" : ""}>Regen</option>
                    <option value="rejected" ${variantBoardFilters.quality === "rejected" ? "selected" : ""}>Rejected</option>
                  </select>
                </div>
                <div>
                  <label>Asset</label>
                  <select onchange="updateVariantFilter('assetType', this.value)">
                    <option value="" ${variantBoardFilters.assetType === "" ? "selected" : ""}>Any</option>
                    <option value="copy" ${variantBoardFilters.assetType === "copy" ? "selected" : ""}>Copy</option>
                    <option value="image" ${variantBoardFilters.assetType === "image" ? "selected" : ""}>Image</option>
                    <option value="video_script" ${variantBoardFilters.assetType === "video_script" ? "selected" : ""}>Script</option>
                    <option value="video" ${variantBoardFilters.assetType === "video" ? "selected" : ""}>Video</option>
                  </select>
                </div>
                <div>
                  <label>Review</label>
                  <select onchange="updateVariantFilter('reviewStatus', this.value)">
                    <option value="" ${variantBoardFilters.reviewStatus === "" ? "selected" : ""}>Any</option>
                    <option value="approved" ${variantBoardFilters.reviewStatus === "approved" ? "selected" : ""}>Approved</option>
                    <option value="shortlisted" ${variantBoardFilters.reviewStatus === "shortlisted" ? "selected" : ""}>Shortlisted</option>
                    <option value="winner" ${variantBoardFilters.reviewStatus === "winner" ? "selected" : ""}>Winner</option>
                    <option value="rejected" ${variantBoardFilters.reviewStatus === "rejected" ? "selected" : ""}>Rejected</option>
                  </select>
                </div>
                <div>
                  <label>Min Score</label>
                  <input type="number" min="0" max="100" value="${esc(variantBoardFilters.minScore)}" onchange="updateVariantFilter('minScore', this.value)" placeholder="e.g. 80" style="width:72px;" />
                </div>
                <div>
                  <label>Search</label>
                  <div style="display:flex;gap:6px;align-items:center;">
                    <input value="${esc(variantBoardFilters.q)}" onchange="updateVariantFilter('q', this.value)" placeholder="angle, hook" style="width:96px;" />
                    <button onclick="resetVariantFilters()" style="font-size:11px;padding:5px 8px;white-space:nowrap;" title="Clear all filters">&#8634; Reset</button>
                  </div>
                </div>
              </div>
            `;
            if (!allItems.length) {
              return `
                <div class="variant-board-header">
                  <h3>Variant Board</h3>
                  <button id="variant-board-toggle" class="variant-toggle-btn" onclick="toggleVariantBoard()">${variantBoardToggleLabel()}</button>
                </div>
                <div id="variant-board-body" class="variant-board-body ${variantBoardCollapsed ? "is-collapsed" : ""}">
                  ${header}
                  <div class="run-detail-empty">No variants materialized yet.</div>
                </div>
              `;
            }
            if (!items.length) {
              return `
                <div class="variant-board-header">
                  <h3>Variant Board</h3>
                  <button id="variant-board-toggle" class="variant-toggle-btn" onclick="toggleVariantBoard()">${variantBoardToggleLabel()}</button>
                </div>
                <div id="variant-board-body" class="variant-board-body ${variantBoardCollapsed ? "is-collapsed" : ""}">
                  ${header}
                  ${filters}
                  <div class="run-detail-empty">No variants match the active filters.</div>
                </div>
              `;
            }
            const scoreCards = items.map((item, idx) => {
              const images = assetsByType(item, "image");
              const image = preferredImageAsset(images);
              const imageState = assetGenerationState(image);
              const evaluation = latestScore(item, "evaluation");
              const score = evaluation?.total_score;
              const qSummary = qualitySummary(item);
              const surfaceBadges = qualitySurfaceBadges(qSummary);
              const execSummary = item.execution_summary || {};
              const lastDecision = execSummary.last_decision?.summary || "-";
              const blocker = execSummary.active_blockers?.[0]?.summary || "-";
              const regenGoal = execSummary.active_regen_goal?.summary || "-";
              const imageRetrying = retryingImageVariants.has(`${runId}:${item.variant_id}`);
              const retryButton = `
                <button class="image-retry-btn ${imageRetrying ? "is-loading" : ""}"
                  data-image-retry-run="${esc(runId)}"
                  data-image-retry-variant="${esc(item.variant_id)}"
                  title="${imageRetrying ? "Retrying image generation" : "Retry image generation"}"
                  ${imageRetrying ? "disabled" : ""}
                  onclick="retryVariantImage(event, '${runId}', '${item.variant_id}')">
                  ${imageRetrying ? '<span class="retry-spinner" aria-hidden="true"></span>' : "Retry"}
                </button>
              `;
              return `
                <article class="variant-score-card" data-variant-id="${esc(item.variant_id)}" onclick="toggleVariantDetail('${runId}', '${item.variant_id}')">
                  <div class="rank-badge">${idx + 1}</div>
                  <div class="stage-title">${esc(item.variant_id)}</div>
                  <div class="muted" style="font-size:11px;">${esc(item.angle || "-")}</div>
                  <div class="score-number ${scoreColorClass(score)}">${score != null ? Math.round(score) : "-"}</div>
                  <div class="thumb-wrap">
                    ${image && imageState === "completed" ? `<img class="thumb" src="${mediaUrl(image.uri)}" alt="variant thumbnail" />` : `<div class="thumb muted" style="display:flex;align-items:center;justify-content:center;font-size:11px;">${imageState === "processing" ? "Image processing" : imageState === "failed" ? "Image failed" : "No image"}</div>`}
                    ${retryButton}
                  </div>
                  <div class="quality-row" style="justify-content:center;">
                    ${item.is_winner ? '<span class="quality-chip good">Winner</span>' : ''}
                    ${item.shortlisted && !item.is_winner ? '<span class="quality-chip good">Picked</span>' : ''}
                    ${surfaceBadges}
                    <span class="quality-chip ${qualityChipClass(qSummary.quality_status)}">${esc(qualityChipLabel(qSummary.quality_status || "-"))}</span>
                  </div>
                  <div class="muted" style="font-size:11px;margin-top:6px;">Decision: ${esc(lastDecision)}</div>
                  <div class="muted" style="font-size:11px;">Blocker: ${esc(blocker)}</div>
                  <div class="muted" style="font-size:11px;">Regen Goal: ${esc(regenGoal)}</div>
                  <div class="quick-actions">
                    <button class="primary" onclick="event.stopPropagation();variantAction('${runId}', '${item.variant_id}', '/runs/${runId}/variants/${item.variant_id}/select', {winner:true, comment:'winner chosen from dashboard'})">Set Winner</button>
                    <button onclick="event.stopPropagation();variantAction('${runId}', '${item.variant_id}', '/runs/${runId}/variants/${item.variant_id}/select', {shortlist:true, comment:'shortlisted from dashboard'})">Pick</button>
                  </div>
                </article>
              `;
            }).join("");
            const detailPanel = expandedVariantId
              ? `<div id="variant-detail-panel" class="variant-detail-panel open">${renderVariantDetail(runId, expandedVariantId)}</div>`
              : `<div id="variant-detail-panel" class="variant-detail-panel"></div>`;
            return `
              <div class="variant-board-header">
                <h3>Variant Board</h3>
                <button id="variant-board-toggle" class="variant-toggle-btn" onclick="toggleVariantBoard()">${variantBoardToggleLabel()}</button>
              </div>
              <div id="variant-board-body" class="variant-board-body ${variantBoardCollapsed ? "is-collapsed" : ""}">
                ${header}
                ${filters}
                <div class="variant-scoreboard">${scoreCards}</div>
                <div id="variant-schedule-indicators" style="display:none;"></div>
                ${detailPanel}
              </div>
            `;
          }

          function renderTimeline(run) {
            const stages = [...(run.stage_tasks || [])].sort((a, b) => {
              const ta = Date.parse(a.started_at || a.updated_at || a.created_at || "") || 0;
              const tb = Date.parse(b.started_at || b.updated_at || b.created_at || "") || 0;
              if (ta !== tb) return tb - ta;
              return String(b.id || "").localeCompare(String(a.id || ""));
            });
            if (!stages.length) return '<div class="run-detail-empty">No stage logs yet.</div>';
            return `
              <div id="timeline-board" class="agent-trace">
                ${stages.map((task, index) => {
                  const agent = task.metadata_json?.agent_name || "-";
                  const isAutoApproved = task.status === "approved" && String(task.review_notes || "").includes("auto_approved");
                  const statusLabel = isAutoApproved ? "AUTO-APPROVED" : esc(task.status);
                  return `
                <article class="trace-event t-resize">
                  <div class="trace-head">
                    <div class="trace-head-main">
                      <span class="trace-index">${stages.length - index}</span>
                      <span class="pill">${esc(task.stage_name)}</span>
                    </div>
                    <div class="muted">${esc(task.started_at || "-")}</div>
                  </div>
                  <div style="display:flex;gap:4px;flex-wrap:wrap;margin:4px 0;">
                    <span class="pill">${statusLabel}</span>
                    <span class="pill">attempt ${esc(task.attempt)}</span>
                    <span class="pill">${esc(agent)}</span>
                    ${isAutoApproved ? '<span class="quality-chip good">auto</span>' : ""}
                  </div>
                  <div class="trace-message">${esc(task.summary || "No summary")}</div>
                  <div class="muted" style="margin-top:4px;">review: ${isAutoApproved ? '<span class="quality-chip good">AUTO-APPROVED</span>' : esc(task.review_notes || "-")}</div>
                  <details class="trace-payload">
                    <summary>Stage payload</summary>
                    <pre>${esc(JSON.stringify(task.output_payload || {}, null, 2))}</pre>
                  </details>
                </article>
                  `;
                }).join("")}
              </div>
            `;
          }

          function renderAgentTrace(run) {
            const events = [...(run.trace_events || currentTraceEvents || [])].sort((a, b) => {
              const ta = Date.parse(a.created_at || "") || 0;
              const tb = Date.parse(b.created_at || "") || 0;
              if (ta !== tb) return tb - ta;
              return String(b.id || "").localeCompare(String(a.id || ""));
            });
            if (!events.length) return '<div class="run-detail-empty">No agent trace events yet.</div>';
            return `
              <div id="agent-trace-board" class="agent-trace">
                ${events.map((event, index) => `
                  <article class="trace-event t-resize">
                    <div class="trace-head">
                      <div class="trace-head-main">
                        <span class="trace-index">${events.length - index}</span>
                        <span class="pill">${esc(event.stage_name)}</span>
                        <span class="pill">${esc(event.agent_name)}</span>
                        <span class="pill">${esc(event.event_type)}</span>
                      </div>
                      <div class="muted">${esc(event.created_at)}</div>
                    </div>
                    <div class="trace-message">${esc(event.message || "-")}</div>
                    <div class="muted" style="margin-top:4px;">provider/model: ${esc(event.provider_name || "-")} / ${esc(event.model_name || "-")}</div>
                    <details class="trace-payload">
                      <summary>Trace payload</summary>
                      <pre>${esc(JSON.stringify(event.payload || {}, null, 2))}</pre>
                    </details>
                  </article>
                `).join("")}
              </div>
            `;
          }
          function executionSummaries(rows, emptyText){
            if (!rows || !rows.length) return `<div class="muted">${esc(emptyText)}</div>`;
            return rows.slice(0, 4).map((item) => `<div class="pill">${esc(item.summary || item.memory_key || "-")}</div>`).join("");
          }
          function renderExecutionMemory(executionMemory){
            const data = executionMemory || {};
            const runLedger = data.run_ledger || {};
            return `
              <details class="card execution-memory-card" style="margin:12px 0;">
                <summary>
                  <span>
                    <b>Execution Memory</b>
                    <span class="muted">Locked facts, active constraints, regen goals, and recent decisions.</span>
                  </span>
                </summary>
                <div class="panel-grid-3" style="margin-top:10px;">
                  <section>
                    <div class="muted" style="margin-bottom:6px;">Locked Facts</div>
                    <div class="pill-row">${executionSummaries(runLedger.locked_facts, "No locked facts yet.")}</div>
                  </section>
                  <section>
                    <div class="muted" style="margin-bottom:6px;">Active Constraints</div>
                    <div class="pill-row">${executionSummaries(runLedger.active_constraints, "No active constraints yet.")}</div>
                  </section>
                  <section>
                    <div class="muted" style="margin-bottom:6px;">Open Regen Goals</div>
                    <div class="pill-row">${executionSummaries(data.active_regeneration_goals, "No open regen goals.")}</div>
                  </section>
                </div>
                <div style="margin-top:10px;">
                  <div class="muted" style="margin-bottom:6px;">Recent Human Decisions</div>
                  <div class="pill-row">${executionSummaries(data.recent_reviews, "No human review memory yet.")}</div>
                </div>
              </details>
            `;
          }
          function renderScorecard(scorecard){
            if (!scorecard) return `<span class="muted">No score yet.</span>`;
            const subScores = scorecard.sub_scores || {};
            const subScoreRows = Object.entries(subScores).map(([key, value]) => `
              <div class="scorecard-item">
                <span>${esc(key.replaceAll("_", " "))}</span>
                <span class="scorecard-item-value">${esc(value)}</span>
              </div>
            `).join("");
            const riskRows = [
              ["Compliance", scorecard.compliance_level || "-"],
              ["AI artifact score", scorecard.ai_artifact_score ?? "-"],
              ...((scorecard.risk_labels || []).map((label, index) => [`Risk ${index + 1}`, label]))
            ].map(([label, value]) => `
              <div class="scorecard-item">
                <span>${esc(label)}</span>
                <span class="scorecard-item-value">${esc(value)}</span>
              </div>
            `).join("");
            return `
              <div class="scorecard-summary">
                <div class="scorecard-hero">
                  <div>
                    <div class="scorecard-kicker">Latest Scorecard</div>
                    <div class="scorecard-total-line">
                      <span class="scorecard-total-caption">Total Score</span>
                      <span class="scorecard-total-value">${esc(scorecard.total_score ?? "-")}</span>
                    </div>
                  </div>
                  <div class="scorecard-top-metrics">
                    <span class="scorecard-badge">Compliance <b>${esc(scorecard.compliance_level || "-")}</b></span>
                    <span class="scorecard-badge">AI Artifact <b>${esc(scorecard.ai_artifact_score ?? "-")}</b></span>
                  </div>
                </div>
                <div class="scorecard-grid">
                  <section class="scorecard-panel">
                    <h4 class="scorecard-panel-title">Sub Scores</h4>
                    ${subScoreRows
                      ? `<div class="scorecard-list">${subScoreRows}</div>`
                      : `<div class="muted">No sub scores.</div>`}
                  </section>
                  <section class="scorecard-panel">
                    <h4 class="scorecard-panel-title">Risk & Compliance</h4>
                    <div class="scorecard-list">${riskRows}</div>
                  </section>
                </div>
                <div class="scorecard-note">${esc(scorecard.explanation?.selection || scorecard.explanation || "No explanation.")}</div>
              </div>
            `;
          }
          function renderScorecardStatusSummary(scorecard){
            if (!scorecard) return "";
            const topSubScores = Object.entries(scorecard.sub_scores || {}).slice(0, 4).map(([key, value]) => `
              <div class="scorecard-item">
                <span>${esc(key.replaceAll("_", " "))}</span>
                <span class="scorecard-item-value">${esc(value)}</span>
              </div>
            `).join("");
            const riskRows = [
              ["Compliance", scorecard.compliance_level || "-"],
              ["AI artifact score", scorecard.ai_artifact_score ?? "-"],
              ...((scorecard.risk_labels || []).slice(0, 2).map((label, index) => [`Risk ${index + 1}`, label]))
            ].map(([label, value]) => `
              <div class="scorecard-item">
                <span>${esc(label)}</span>
                <span class="scorecard-item-value">${esc(value)}</span>
              </div>
            `).join("");
            return `
              <div class="status-scorecard">
                <div class="status-scorecard-head">
                  <div>
                    <div class="status-explainer-kicker">Outcome Scorecard</div>
                    <div class="status-scorecard-total">
                      <span>Total score</span>
                      <strong>${esc(scorecard.total_score ?? "-")}</strong>
                    </div>
                  </div>
                  <div class="status-scorecard-badges">
                    <span class="scorecard-badge">Compliance <b>${esc(scorecard.compliance_level || "-")}</b></span>
                    <span class="scorecard-badge">AI Artifact <b>${esc(scorecard.ai_artifact_score ?? "-")}</b></span>
                  </div>
                </div>
                <div class="status-scorecard-grid">
                  <section class="status-scorecard-panel">
                    <h4>Top Sub Scores</h4>
                    <div class="scorecard-list">${topSubScores || '<div class="muted">No sub scores.</div>'}</div>
                  </section>
                  <section class="status-scorecard-panel">
                    <h4>Risk & Compliance</h4>
                    <div class="scorecard-list">${riskRows}</div>
                  </section>
                </div>
                <div class="status-scorecard-note">${esc(scorecard.explanation?.selection || scorecard.explanation || "No explanation.")}</div>
              </div>
            `;
          }
          function firstTextValue(obj, keys){
            if (!obj || typeof obj !== "object") return "";
            for (const key of keys) {
              const value = obj[key];
              if (typeof value === "string" && value.trim()) return value.trim();
              if (Array.isArray(value) && value.length) return value.map((item) => typeof item === "string" ? item : JSON.stringify(item)).slice(0, 3).join("; ");
            }
            return "";
          }
          function summarizeList(label, rows, keys){
            if (!Array.isArray(rows) || !rows.length) return "";
            const sample = rows.slice(0, 3).map((item) => {
              if (typeof item === "string") return item;
              return firstTextValue(item, keys) || JSON.stringify(item).slice(0, 120);
            }).filter(Boolean).join(" | ");
            return `${label}: ${rows.length}${sample ? ` - ${sample}` : ""}`;
          }
          function summarizeVariants(rows){
            if (!Array.isArray(rows) || !rows.length) return "";
            return {
              title: `Variants: ${rows.length}`,
              rows: rows.slice(0, 6).map((item) => {
                if (typeof item === "string") return { title: item, detail: "" };
                const variantId = item.variant_id || item.id || "variant";
                return {
                  title: `${variantId}: ${item.angle || item.hook || "Variant"}`,
                  detail: [item.hook, item.message].filter(Boolean).join(" - "),
                };
              }),
            };
          }
          function compactText(value, maxLength = 220){
            const text = String(value || "").replace(/\\s+/g, " ").trim();
            return text.length > maxLength ? `${text.slice(0, maxLength - 1)}...` : text;
          }
          function intakeReviewRows(payload){
            const skuRows = Array.isArray(payload.sku_summary) ? payload.sku_summary : [];
            const images = Array.isArray(payload.image_references) ? payload.image_references : [];
            const videos = Array.isArray(payload.video_references) ? payload.video_references : [];
            const identity = payload.visual_identity || {};
            const truth = payload.product_truth_contract || {};
            const valueProps = Array.isArray(payload.business_context?.key_value_props) ? payload.business_context.key_value_props : [];
            const rows = [
              {
                title: `Product: ${payload.product_name || "unknown product"}`,
                detail: [
                  payload.market ? `Market: ${payload.market}` : "",
                  payload.locale ? `Locale: ${payload.locale}` : "",
                  valueProps.length ? `Value props: ${valueProps.slice(0, 3).join(", ")}` : "",
                ].filter(Boolean).join(" | "),
              },
              {
                title: `Inputs: ${skuRows.length} SKU rows, ${images.length} images, ${videos.length} videos`,
                detail: [
                  summarizeList("SKU sample", skuRows, ["sku", "name", "title", "product_name"]),
                  summarizeList("Image sample", images, ["filename", "uri", "url"]),
                  summarizeList("Video sample", videos, ["filename", "uri", "url"]),
                ].filter(Boolean).join(" | "),
              },
            ];
            const preserve = truth.must_preserve || identity.must_preserve_details || [];
            if (preserve.length) rows.push({ title: "Must preserve", detail: preserve.slice(0, 5).join(" | ") });
            if (identity.primary_colors?.length) rows.push({ title: "Visual identity", detail: `Colors: ${identity.primary_colors.slice(0, 5).join(", ")}` });
            if (payload.asset_media_summary) rows.push({ title: "Media read", detail: compactText(payload.asset_media_summary) });
            if (payload.llm_summary) rows.push({ title: "Normalized brief", detail: compactText(payload.llm_summary) });
            if (payload.manual_research_brief) rows.push({ title: "Research notes", detail: compactText(payload.manual_research_brief) });
            return { title: "Intake summary", rows };
          }
          function copyImageReviewRows(payload){
            const copies = Array.isArray(payload.copy_variants) ? payload.copy_variants : [];
            const images = Array.isArray(payload.image_assets) ? payload.image_assets : [];
            const ids = [...new Set([...copies, ...images].map((item) => item?.variant_id || item?.id).filter(Boolean))];
            return {
              title: `Copy/Image variants: ${ids.length || Math.max(copies.length, images.length)}`,
              rows: (ids.length ? ids : copies.map((_, index) => `V${index + 1}`)).map((variantId) => {
                const copy = copies.find((item) => (item.variant_id || item.id) === variantId) || {};
                const image = images.find((item) => (item.variant_id || item.id) === variantId) || {};
                const imageStatus = image.error ? "image failed" : (image.generation_status || image.source || (image.uri ? "image ready" : "no image"));
                return {
                  title: `${variantId}: ${copy.headline || copy.hook || image.prompt || "Copy/Image output"}`,
                  detail: [copy.primary_text, imageStatus].filter(Boolean).join(" - "),
                  imageUri: image.uri && !image.error ? image.uri : "",
                  actionVariantId: variantId,
                };
              }),
            };
          }
          function videoReviewRows(payload){
            const videos = Array.isArray(payload.videos) ? payload.videos : [];
            return {
              title: `Video variants: ${videos.length}`,
              rows: videos.map((video, index) => {
                const variantId = video.variant_id || video.id || `V${index + 1}`;
                const status = video.error ? "video failed" : (video.generation_status || video.source || (video.video_uri ? "video ready" : "no video"));
                return {
                  title: `${variantId}: ${status}`,
                  detail: [video.duration_seconds ? `${video.duration_seconds}s` : "", video.error || video.video_uri].filter(Boolean).join(" - "),
                  videoUri: video.video_uri && !video.error ? video.video_uri : "",
                  actionVariantId: variantId,
                };
              }),
            };
          }
          function visualQualityReviewRows(payload){
            const summaries = Array.isArray(payload.variant_summaries) ? payload.variant_summaries : [];
            const reports = Object.fromEntries((Array.isArray(payload.reports) ? payload.reports : [])
              .filter((report) => report && report.variant_id)
              .map((report) => [report.variant_id, report]));
            const media = payload.model_media_inputs || {};
            const titleParts = [
              `Variants reviewed: ${summaries.length}`,
              payload.model_review_status ? `model review: ${payload.model_review_status}` : "",
              (media.image_count || media.video_count) ? `media inputs: ${media.image_count || 0} image, ${media.video_count || 0} video` : "",
            ].filter(Boolean);
            return {
              title: titleParts.join(" | ") || "Visual QA",
              rows: summaries.map((summary, index) => {
                const variantId = summary.variant_id || `V${index + 1}`;
                const report = reports[variantId] || {};
                const proof = summary.visual_proof_qa || report.visual_proof_qa || {};
                const issues = summary.issues || summary.blocking_issues || report.blocking_issues || [];
                const detail = [
                  summary.recommended_action ? `Action: ${summary.recommended_action}` : "",
                  report.asset_reports ? `Assets checked: ${report.asset_reports.length}` : "",
                  proof.evidence ? `Evidence: ${proof.evidence}` : "",
                  issues.length ? `Issues: ${issues.slice(0, 3).join(", ")}` : "",
                ].filter(Boolean).join(" | ");
                return {
                  title: `${variantId}: ${summary.qa_status || report.qa_status || "unknown"}${summary.visual_score != null ? ` (${summary.visual_score})` : ""}`,
                  detail,
                  actionVariantId: variantId,
                };
              }),
            };
          }
          function focusVariantFromChecklist(variantId){
            variantBoardCollapsed = false;
            persistVariantBoardCollapsedState();
            const body = document.getElementById("variant-board-body");
            if (body) body.classList.remove("is-collapsed");
            const btn = document.getElementById("variant-board-toggle");
            if (btn) btn.textContent = variantBoardToggleLabel();
            const card = document.querySelector(`.variant-score-card[data-variant-id="${CSS.escape(variantId)}"]`);
            if (card) {
              card.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
              if (window.__lastRunId) toggleVariantDetail(window.__lastRunId, variantId);
            }
          }
          function renderChecklistItem(item){
            if (typeof item === "string") return `<li>${esc(item)}</li>`;
            if (item && Array.isArray(item.rows)) {
              return `<li><div class="review-checklist-group-title">${esc(item.title || "Items")}</div><ul class="review-checklist-sublist">${item.rows.map((row) => {
                if (typeof row === "string") return `<li>${esc(row)}</li>`;
                return `<li class="${row.imageUri || row.videoUri ? "has-review-thumb" : ""}">
                  ${row.imageUri ? `<img class="review-checklist-thumb" src="${mediaUrl(row.imageUri)}" alt="${esc(row.title || "variant thumbnail")}" />` : ""}
                  ${row.videoUri ? `<video class="review-checklist-thumb review-checklist-video" src="${mediaUrl(row.videoUri)}" controls muted preload="metadata"></video>` : ""}
                  <div class="review-checklist-row-main">
                    <b>${esc(row.title || "-")}</b>
                    ${row.detail ? `<div>${esc(row.detail)}</div>` : ""}
                    ${row.actionVariantId ? `<button type="button" class="review-jump-btn" onclick="focusVariantFromChecklist('${esc(row.actionVariantId)}')">Open in board</button>` : ""}
                  </div>
                </li>`;
              }).join("")}</ul></li>`;
            }
            return "";
          }
          function currentReviewTask(run){
            const tasks = run.stage_tasks || [];
            return tasks.find((task) => task.stage_name === run.current_stage && task.status === "waiting_review")
              || tasks.find((task) => task.status === "waiting_review")
              || null;
          }
          function reviewChecklistItems(task){
            const payload = task.output_payload || {};
            const stage = task.stage_name;
            const items = [];
            if (task.summary) items.push(task.summary);
            if (stage === "intake") {
              items.push(intakeReviewRows(payload));
            } else if (stage === "planning") {
              items.push(summarizeList("Strategic angles", payload.strategic_angles, ["angle", "name", "summary", "hook"]));
              items.push(summarizeList("Constraints", payload.constraints, ["rule", "constraint", "summary"]));
            } else if (stage === "divergence") {
              items.push(summarizeVariants(payload.variants));
            } else if (stage === "copy_image_generation") {
              items.push(copyImageReviewRows(payload));
            } else if (stage === "video_scripting") {
              items.push(summarizeList("Video scripts", payload.scripts, ["variant_id", "hook", "opening_hook", "script"]));
            } else if (stage === "storyboard_image_generation") {
              items.push(summarizeList("Storyboard frames", payload.frames, ["frame_id", "variant_id", "prompt", "image_uri"]));
            } else if (stage === "video_generation") {
              items.push(videoReviewRows(payload));
            } else if (stage === "visual_quality_assessment") {
              if (payload.model_summary) items.push(`Model summary: ${payload.model_summary}`);
              items.push(visualQualityReviewRows(payload));
            } else if (stage === "evaluation_selection") {
              const selected = payload.selected_deliverables || {};
              if (selected.winner_variant_id) items.push(`Winner candidate: ${selected.winner_variant_id}`);
              items.push(summarizeList("Ranked variants", payload.evaluation_result?.ranked_variants, ["variant_id", "rationale", "reason"]));
            }
            return items.filter((item) => item && (typeof item !== "string" || item.trim())).slice(0, 6);
          }
          function renderReviewChecklist(run){
            const task = currentReviewTask(run);
            if (!task) return "";
            const items = reviewChecklistItems(task);
            const list = items.length
              ? items.map(renderChecklistItem).join("")
              : "<li>Review the stage output before approving or rejecting.</li>";
            return `
              <section class="status-explainer review" style="margin-top:12px;">
                <div class="status-explainer-kicker">Review checklist</div>
                <h3>${esc(task.stage_name)} output</h3>
                <ol>${list}</ol>
              </section>
            `;
          }
          function statusExplanationClass(tone){
            if (tone === "danger") return "status-explainer danger";
            if (tone === "review") return "status-explainer review";
            if (tone === "success") return "status-explainer success";
            return "status-explainer info";
          }
          function failureReasonLabel(flag){
            const labels = {
              media_gate_generation_error: "Image generation provider failed before producing media.",
              media_gate_decode_error: "Copy/Image local media gate could not decode the generated media.",
              media_gate_placeholder: "Copy/Image local media gate found a placeholder or unusable asset.",
              media_gate_product_truth_color_mismatch: "Copy/Image local media gate did not find required product colors.",
              media_gate_product_truth_structure_review: "Copy/Image local media gate needs product-structure review.",
              media_gate_low_information: "Copy/Image local media gate found too little visual information.",
              media_gate_aspect_mismatch: "Copy/Image local media gate found an aspect-ratio mismatch.",
              media_gate_missing_file: "Copy/Image local media gate could not find the generated asset file.",
              media_gate_empty_file: "Copy/Image local media gate found an empty generated asset file.",
              media_gate_missing_uri: "Copy/Image local media gate found a missing generated asset URI.",
              visual_qa_decode_error: "Generated media could not be decoded.",
              visual_qa_placeholder: "Provider returned a placeholder or unusable asset.",
              visual_qa_product_truth_color_mismatch: "Generated asset does not preserve required product colors.",
              visual_qa_product_truth_structure_review: "Generated asset needs product-structure review.",
              visual_qa_low_information: "Generated asset has too little visual information.",
              visual_qa_aspect_mismatch: "Generated asset aspect ratio does not match the request.",
              visual_qa_missing_file: "Generated asset file is missing.",
              visual_qa_empty_file: "Generated asset file is empty.",
              visual_qa_missing_uri: "Generated asset URI is missing.",
              visual_qa_visual_proof_failed: "Visual proof did not clearly demonstrate the intended claim.",
            };
            return labels[flag] || flag.replaceAll("_", " ");
          }
          function extractFailureFlags(detail){
            return [...new Set(String(detail || "").match(/(?:media_gate|visual_qa|marketplace)_[a-z0-9_]+/g) || [])];
          }
          function renderFailureReasons(info){
            if (info.tone !== "danger") return "";
            const flags = extractFailureFlags(info.detail);
            if (!flags.length) return "";
            return `
              <div class="failure-reasons">
                <div class="review-checklist-group-title">Failure reasons</div>
                <ul class="review-checklist-sublist">
                  ${flags.map((flag) => `<li><b>${esc(flag)}</b><div>${esc(failureReasonLabel(flag))}</div></li>`).join("")}
                </ul>
              </div>
            `;
          }
          function currentStageTask(run){
            const tasks = run.stage_tasks || [];
            return tasks.find((task) => task.stage_name === run.current_stage)
              || tasks.find((task) => task.status === "running" || task.status === "waiting_review" || task.status === "failed")
              || null;
          }
          function providerSummary(run){
            const resolved = currentStageTask(run)?.metadata_json?.resolved_api || {};
            const rows = [];
            if (resolved.provider_name || resolved.model_name) rows.push(`text: ${resolved.provider_name || "-"} / ${resolved.model_name || "-"}`);
            if (resolved.image_provider_name || resolved.image_model_name) rows.push(`image: ${resolved.image_provider_name || "-"} / ${resolved.image_model_name || "-"}`);
            if (resolved.video_provider_name || resolved.video_model_name) rows.push(`video: ${resolved.video_provider_name || "-"} / ${resolved.video_model_name || "-"}`);
            return rows.length ? rows.join(" | ") : `run fallback: ${run.model_provider || "-"} / ${run.model_name || "-"}`;
          }
          function renderStatusExplanation(run){
            const info = run.status_explanation || {};
            const actions = (info.next_actions || []).map((item) => `<li>${esc(item)}</li>`).join("");
            const scorecardSummary = run.status === "completed" ? renderScorecardStatusSummary(run.latest_scorecard) : "";
            return `
              <section class="${statusExplanationClass(info.tone)}">
                <div class="status-explainer-main">
                  <div>
                    <div class="status-explainer-kicker">Current state</div>
                    <h3>${esc(info.headline || statusLabel(run.status))}</h3>
                    <p>${esc(info.detail || "No additional status detail is available yet.")}</p>
                  </div>
                  <div class="status-explainer-action">${esc(info.primary_action || "Review run")}</div>
                </div>
                ${renderFailureReasons(info)}
                ${scorecardSummary}
                ${actions ? `<ol>${actions}</ol>` : ""}
              </section>
            `;
          }
          function isNearTraceLeftEdge(container){
            if (!container) return true;
            return container.scrollLeft <= 48;
          }
          function scrollTraceToLeft(behavior = "smooth"){
            const container = document.getElementById("agent-trace-board");
            if (!container) return;
            container.scrollTo({ left: 0, behavior });
          }
          function bindTracePayloadToggles(){
            ["agent-trace-board", "timeline-board"].forEach((id) => {
              const container = document.getElementById(id);
              if (!container) return;
              container.querySelectorAll(".trace-payload").forEach((details) => {
                if (details.dataset.bound === "1") return;
                details.dataset.bound = "1";
                details.addEventListener("toggle", () => {
                  const card = details.closest(".trace-event");
                  if (!card) return;
                  card.classList.toggle("trace-event-expanded", details.open);
                if (details.open) {
                  requestAnimationFrame(() => {
                    card.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
                  });
                }
              });
            });
            });
          }

          function connectRunEvents(runId){
            if (runEventSource) {
              runEventSource.close();
              runEventSource = null;
            }
            if (!window.EventSource) return;
            runEventSource = new EventSource(`/runs/${runId}/events`);
            runEventSource.addEventListener("agent_trace", (event) => {
              try {
                const item = JSON.parse(event.data);
                if (currentRunId !== runId) return;
                if (!currentTraceEvents.some((existing) => existing.id === item.id)) {
                  const containerBefore = document.getElementById("agent-trace-board");
                  const shouldStickToLeft = isNearTraceLeftEdge(containerBefore);
                  currentTraceEvents.unshift(item);
                  const container = document.getElementById("agent-trace-container");
                  if (container) {
                    container.innerHTML = renderAgentTrace({ trace_events: currentTraceEvents });
                    bindTracePayloadToggles();
                    if (shouldStickToLeft) {
                      requestAnimationFrame(() => scrollTraceToLeft("smooth"));
                    }
                  }
                }
              } catch (_err) {}
            });
          }

          function startRunDetailPolling(runId) {
            stopRunDetailPolling();
            runDetailTimer = setInterval(async () => {
              if (!document.hidden) {
                try {
                  const run = await api(`/runs/${runId}`);
                  const newUpdated = run.updated_at;
                  if (runDetailLastUpdated === newUpdated) return;
                  runDetailLastUpdated = newUpdated;
                  await silentRefreshRunDetail(run);
                } catch (_err) {}
              }
            }, 3000);
          }

          function stopRunDetailPolling() {
            if (runDetailTimer) {
              clearInterval(runDetailTimer);
              runDetailTimer = null;
            }
            runDetailLastUpdated = null;
          }
          function setRunDetailHtml(html) {
            const detail = document.getElementById("run-detail");
            detail.classList.remove("run-detail-empty");
            detail.classList.add("run-detail-content");
            detail.innerHTML = html;
          }

          async function silentRefreshRunDetail(run) {
            const wasCollapsed = variantBoardCollapsed;
            const wasExpandedVariantId = expandedVariantId;
            const traceBoard = document.getElementById("agent-trace-board");
            const traceScrollLeft = traceBoard ? traceBoard.scrollLeft : 0;

            const [deliverables, variants, executionMemory] = await Promise.all([
              api(`/runs/${run.id}/deliverables`).catch(() => ({ run_id: run.id, deliverables: {}, score: {} })),
              api(`/runs/${run.id}/variants`).catch(() => ({ run_id: run.id, items: [], summary: {}, variants: [], ranked: [] })),
              api(`/runs/${run.id}/execution-memory`).catch(() => ({ run_ledger: {}, stage_handoffs: [], variant_ledgers: [], recent_reviews: [], active_regeneration_goals: [] }))
            ]);

            // Merge: keep SSE-streamed events not yet on server, add server events on top
            const serverEventIds = new Set((run.trace_events || []).map(e => e.id));
            const sseOnly = currentTraceEvents.filter(e => !serverEventIds.has(e.id));
            currentTraceEvents = [...sseOnly, ...(run.trace_events || [])];
            run.trace_events = currentTraceEvents;

            expandedVariantId = wasExpandedVariantId;
            setRunDetailHtml(renderRunDetail(run, deliverables, variants, executionMemory));
            setRetryProgressFromRun(run);

            if (wasCollapsed) {
              const body = document.getElementById("variant-board-body");
              if (body) body.classList.add("is-collapsed");
              const btn = document.getElementById("variant-board-toggle");
              if (btn) btn.textContent = variantBoardToggleLabel();
            }
            if (wasExpandedVariantId) {
              const panel = document.getElementById("variant-detail-panel");
              if (panel && !panel.classList.contains("open")) {
                panel.innerHTML = renderVariantDetail(run.id, wasExpandedVariantId);
                panel.classList.add("open");
              }
            }
            const newTraceBoard = document.getElementById("agent-trace-board");
            if (newTraceBoard && traceScrollLeft > 0) {
              newTraceBoard.scrollLeft = traceScrollLeft;
            }
            bindTracePayloadToggles();
          }

          function renderRunDetail(run, deliverables, variants, executionMemory){
            const score = renderScorecard(run.latest_scorecard);
            const scoreSection = run.status === "completed" && run.latest_scorecard
              ? ""
              : `<h3 style="margin-top:14px;">Latest Scorecard</h3>${score}`;
            return `
              <div class="run-meta-compact">
                <div><b>Run:</b> ${esc(run.id)}</div>
                <div><span class="${statusPillClass(run.status)}">${statusLabel(run.status)}</span> <span class="pill">stage: ${esc(run.current_stage || "-")}</span><span class="pill">mode: ${esc(run.pipeline_mode)}</span><span class="pill">approval: ${esc(run.approval_mode || "manual")}</span></div>
                <div class="muted">provider/model: ${esc(providerSummary(run))} | budget: ${esc(run.budget_used)}</div>
                <div class="muted">product_code: ${esc(run.product_code)} | industry_code: ${esc(run.industry_code)} | creative_preset: ${esc(run.creative_preset)}</div>
                <div style="margin-top:8px;"><button onclick="refreshAsyncAssets('${run.id}')">Refresh async assets</button></div>
              </div>
              ${renderStatusExplanation(run)}
              ${renderResearchContext(run.research_context || {})}
              ${renderReviewChecklist(run)}
              ${renderExecutionMemory(executionMemory)}
              ${renderDeliverables(deliverables)}
              ${renderVariantBoard(run.id, variants)}
              <h3 style="margin-top:8px;">Agent Trace</h3>
              <div id="agent-trace-container">${renderAgentTrace(run)}</div>
              <h3 style="margin-top:14px;">Stage Timeline</h3>
              ${renderTimeline(run)}
              ${scoreSection}
            `;
          }

          function renderResearchContext(ctx){
            const summary = ctx.summary || {};
            const included = ctx.included || [];
            const excluded = ctx.excluded || [];
            const sourceCounts = Object.entries(summary.source_counts || {}).map(([k,v]) => `${esc(k)}=${esc(v)}`).join(" · ") || "none";
            const excludedReasons = (ctx.planning_guidance?.excluded_reasons || []).join(", ") || "none";
            return `<section class="run-panel" style="margin-top:10px;">
              <h3 style="margin:0 0 6px;">Research Context</h3>
              <div class="muted">Included=${esc(summary.included_count || 0)} · Excluded=${esc(summary.excluded_count || 0)} · Evidence=${esc(summary.evidence_count || 0)} · Avg quality=${esc(summary.average_quality || 0)}</div>
              <div class="muted">Types: ${sourceCounts}</div>
              <div class="muted">Excluded reasons: ${esc(excludedReasons)}</div>
              ${included.length ? `<div class="muted">Used: ${included.slice(0,3).map(item => esc(item.source_type + ": " + (item.summary || "").slice(0,80))).join("<br>")}</div>` : ""}
              ${excluded.length ? `<div class="muted">Held for review: ${excluded.slice(0,3).map(item => esc(item.source_type + " (" + (item.dirty_reasons || []).join(", ") + ")")).join("<br>")}</div>` : ""}
            </section>`;
          }

          async function selectRun(runId){
            currentRunId = runId;
            expandedVariantId = null;
            document.getElementById('fab-advance').classList.add('visible');
            document.getElementById('fab-reject').classList.add('visible');
            const [run, deliverables, variants, executionMemory] = await Promise.all([
              api(`/runs/${runId}`),
              api(`/runs/${runId}/deliverables`).catch(() => ({ run_id: runId, deliverables: {}, score: {} })),
              api(`/runs/${runId}/variants`).catch(() => ({ run_id: runId, items: [], summary: {}, variants: [], ranked: [] })),
              api(`/runs/${runId}/execution-memory`).catch(() => ({ run_ledger: {}, stage_handoffs: [], variant_ledgers: [], recent_reviews: [], active_regeneration_goals: [] }))
            ]);
            currentTraceEvents = run.trace_events || [];
            runDetailLastUpdated = run.updated_at;
            setRunDetailHtml(renderRunDetail(run, deliverables, variants, executionMemory));
            setRetryProgressFromRun(run);
            bindTracePayloadToggles();
            requestAnimationFrame(() => scrollTraceToLeft("auto"));
            connectRunEvents(runId);
            startRunDetailPolling(runId);
            setTimeout(loadScheduleIndicators, 300);
            autoCollapseRunsPanel();
          }

          async function loadPipelineModes(){
            pipelineModes = await api("/pipeline-modes");
            const sel = document.getElementById("pipeline_mode");
            sel.innerHTML = "";
            pipelineModes.forEach((m) => {
              const opt = document.createElement("option");
              opt.value = m.mode;
              opt.textContent = `${m.display_name}`;
              if (m.mode === "copy_image_only") opt.selected = true;
              sel.appendChild(opt);
            });
            if (typeof refreshPipelineFields === "function") refreshPipelineFields();
            else refreshModeHint();
          }

          function refreshModeHint(){
            const mode = document.getElementById("pipeline_mode").value;
            const chosen = pipelineModes.find((m) => m.mode === mode);
            if (!chosen) return;
            const text = `Stages: ${chosen.stages.join(" -> ")} | Active agents (${chosen.agent_count}): ${chosen.agents.join(", ")}`;
            document.getElementById("mode-summary").textContent = text;
          }

          function refreshResearchHint(){
            const mode = document.getElementById("research_mode").value;
            const hint = document.getElementById("research-hint");
            if (mode === "autonomous_web") {
              hint.textContent = "Autonomous web research will run in planning and may be slower due to online fetches.";
            } else {
              hint.textContent = "Planning will rely on your manual notes and uploaded assets only (recommended for fast debugging).";
            }
            refreshResearchContextCard();
          }

          async function refreshResearchContextCard(){
            const card = document.getElementById("research-context-card");
            if (!card) return;
            const workspace = document.getElementById("workspace_name")?.value || "workspace_demo";
            const project = document.getElementById("project_name")?.value || "shop_analysis";
            try {
              const ctx = await api(`/research-context?workspace_name=${encodeURIComponent(workspace)}&project_name=${encodeURIComponent(project)}`);
              const summary = ctx.summary || {};
              const sourceCounts = Object.entries(summary.source_counts || {}).map(([k,v]) => `${esc(k)}=${esc(v)}`).join(" · ") || "none";
              const excludedReasons = (ctx.planning_guidance?.excluded_reasons || []).join(", ") || "none";
              card.innerHTML = `<b>Research context</b>: ${summary.ready_for_planning ? "ready" : "not ready"} · included=${esc(summary.included_count || 0)} · excluded=${esc(summary.excluded_count || 0)} · avg quality=${esc(summary.average_quality || 0)}<br><span class="muted">types: ${sourceCounts} · excluded reasons: ${esc(excludedReasons)}</span>`;
            } catch (err) {
              card.textContent = "Research context unavailable for this workspace/project.";
            }
          }

          function detectInputKinds(files){
            let hasImageInputs = false;
            let hasVideoInputs = false;
            for (let i = 0; i < files.length; i++) {
              const name = (files[i].name || "").toLowerCase();
              if (name.endsWith(".png") || name.endsWith(".jpg") || name.endsWith(".jpeg") || name.endsWith(".webp")) hasImageInputs = true;
              if (name.endsWith(".mp4") || name.endsWith(".mov") || name.endsWith(".m4v")) hasVideoInputs = true;
            }
            return { hasImageInputs, hasVideoInputs };
          }

          function preflightDetail(preflight){
            const rows = (preflight.checks || []).filter((row) => row.severity !== "ok");
            if (!rows.length) return preflight.summary || "No compatibility risk detected.";
            return rows.map((row) => {
              const scope = [row.stage_name, row.agent_name].filter(Boolean).join(" / ");
              return `[${row.severity.toUpperCase()}] ${scope ? scope + ": " : ""}${row.message}`;
            }).join("\\n");
          }

          async function advanceRun(){
            if(!currentRunId) return;
            await api(`/runs/${currentRunId}/advance`, { method:"POST", body: JSON.stringify({notes:"approved"})});
            await selectRun(currentRunId);
            await refreshRuns();
          }

          async function rejectRun(){
            if(!currentRunId) return;
            await api(`/runs/${currentRunId}/reject`, { method:"POST", body: JSON.stringify({notes:"rejected"})});
            await selectRun(currentRunId);
            await refreshRuns();
          }

          function startRunListPolling() {
            if (runListInterval) return;
            runListInterval = setInterval(async () => {
              if (!document.hidden) await refreshRuns();
            }, 5000);
            updateRefreshIndicator(true);
          }

          function stopRunListPolling() {
            if (runListInterval) {
              clearInterval(runListInterval);
              runListInterval = null;
            }
            updateRefreshIndicator(false);
          }

          function updateRefreshIndicator(active) {
            const indicator = document.getElementById("runs-refresh-indicator");
            const compactDot = document.getElementById("runs-compact-live-dot");
            if (!indicator) return;
            if (active) {
              indicator.classList.add("active");
              indicator.title = "Auto-refreshing every 5s";
              if (compactDot) compactDot.classList.add("active");
            } else {
              indicator.classList.remove("active");
              indicator.title = "Auto-refresh paused (tab hidden)";
              if (compactDot) compactDot.classList.remove("active");
            }
          }

          document.addEventListener("visibilitychange", () => {
            if (document.hidden) {
              updateRefreshIndicator(false);
            } else {
              updateRefreshIndicator(true);
              refreshRuns();
              if (currentRunId) {
                stopRunDetailPolling();
                startRunDetailPolling(currentRunId);
              }
            }
          });

          window.addEventListener("resize", () => {
            if (!canCollapseRunsPanel() && runsPanelCollapsed) {
              runsPanelCollapsed = false;
              persistRunsPanelCollapsedState();
            }
            syncRunsPanelState();
          });

          refreshResearchHint();
          loadRunsPanelCollapsedState();
          loadVariantBoardCollapsedState();
          loadPipelineModes();
          syncRunsPanelState();
          loadDataSources().then(async () => {
            await refreshRuns();
            const hash = window.location.hash || "";
            if (hash.startsWith("#run=")) {
              const runId = hash.replace("#run=", "");
              if (runId) {
                try { await selectRun(runId); } catch (_err) {}
              }
            }
          });
          startRunListPolling();

          async function backupDatabase() {
            const btn = event.target;
            const orig = btn.textContent;
            btn.textContent = "Backing up...";
            btn.disabled = true;
            try {
              const res = await fetch("/backup", { method: "POST" });
              if (!res.ok) throw new Error(await res.text());
              const data = await res.json();
              alert("Backed up to ~/.crispy/backups/" + data.backups[0].name + "\\n" + data.backups.length + " total backups.");
            } catch (err) {
              alert("Backup failed: " + err.message);
            } finally {
              btn.textContent = orig;
              btn.disabled = false;
            }
          }

          async function showRestoreDialog() {
            try {
              const res = await fetch("/backups");
              if (!res.ok) throw new Error(await res.text());
              const backups = await res.json();
              if (backups.length === 0) { alert("No backups found."); return; }
              const list = backups.map((b, i) =>
                (i + 1) + ". " + b.name + " (" + b.size_kb + " KB)"
              ).join("\\n");
              const choice = prompt(
                "Available backups in ~/.crispy/backups/:\\n\\n" + list +
                "\\n\\nType the number to restore, or cancel:"
              );
              if (!choice) return;
              const idx = parseInt(choice) - 1;
              if (idx < 0 || idx >= backups.length) { alert("Invalid choice."); return; }
              const selected = backups[idx];
              if (!confirm("Restore from " + selected.name + "?\\n\\nCurrent database will be backed up first, then overwritten. This cannot be undone.")) return;
              const restoreRes = await fetch("/backup/restore", {
                method: "POST",
                body: JSON.stringify({ name: selected.name }),
                headers: { "Content-Type": "application/json" }
              });
              if (!restoreRes.ok) throw new Error(await restoreRes.text());
              alert("Restored from " + selected.name + ".\\n\\nPage will reload.");
              location.reload();
            } catch (err) {
              alert("Restore failed: " + err.message);
            }
          }
        </script>
    """
@router.get("/dashboard/research", response_class=HTMLResponse)
def dashboard_research(request: Request) -> str:
    return templates.TemplateResponse(request=request, name="shop_analysis.html")


@router.get("/dashboard/shop-analysis", response_class=HTMLResponse)
def dashboard_shop_analysis(request: Request) -> str:
    return templates.TemplateResponse(request=request, name="shop_analysis.html")


def _dashboard_html(request: Request) -> str:
    """Render the dashboard page using Jinja2 templates."""
    from app.dashboard.create_run import CREATE_RUN_HTML, CREATE_RUN_JS
    return templates.TemplateResponse(request, "dashboard.html", {
        "create_run_html": CREATE_RUN_HTML,
        "shared_js": CREATE_RUN_JS + _dashboard_shared_js(),
    })



@router.get("/", response_class=HTMLResponse)
def dashboard_root(request: Request) -> str:
    return _dashboard_html(request)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> str:
    return _dashboard_html(request)


@router.get("/dashboard/shops", response_class=HTMLResponse)
def dashboard_shops_page(request: Request) -> str:
    return templates.TemplateResponse(request, "shops.html")


@router.get("/dashboard/assets", response_class=HTMLResponse)
def dashboard_assets_page(request: Request) -> str:
    return templates.TemplateResponse(request, "assets.html")


@router.get("/dashboard/data", response_class=HTMLResponse)
def dashboard_data_page(request: Request) -> str:
    return templates.TemplateResponse(request, "data_dashboard.html")


@router.get("/dashboard/gm-review", response_class=HTMLResponse)
def dashboard_gm_review_page(request: Request) -> str:
    return templates.TemplateResponse(request, "gm_review.html")


@router.get("/dashboard/personas", response_class=HTMLResponse)
def dashboard_personas_page(request: Request) -> str:
    return templates.TemplateResponse(request, "personas.html")


@router.get("/dashboard/data-sources", response_model=DataSourceListResponse)
def dashboard_data_sources() -> DataSourceListResponse:
    active_url = get_active_database_url()
    urls = list_local_sqlite_database_urls()
    return DataSourceListResponse(
        active_url=active_url,
        items=[_serialize_data_source(item, active_url=active_url) for item in urls],
    )


@router.post("/dashboard/data-sources/select", response_model=DataSourceListResponse)
def dashboard_select_data_source(payload: DataSourceSelectRequest) -> DataSourceListResponse:
    try:
        active_url = switch_database_url(payload.url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"failed to switch data source: {exc}") from exc
    urls = list_local_sqlite_database_urls()
    return DataSourceListResponse(
        active_url=active_url,
        items=[_serialize_data_source(item, active_url=active_url) for item in urls],
    )


@router.post("/backup")
def create_backup() -> dict:
    from app.data.session import backup_database

    path = backup_database()
    if path is None:
        raise HTTPException(status_code=400, detail="database is not a local SQLite file or does not exist")
    backups = sorted(
        [{"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1), "mtime": p.stat().st_mtime} for p in path.parent.glob("*.db")],
        key=lambda item: item["mtime"], reverse=True,
    )
    return {"backup_path": str(path), "backups": backups}


@router.get("/backups")
def list_backups() -> list[dict]:
    from app.data.session import BACKUP_DIR

    if not BACKUP_DIR.exists():
        return []
    files = sorted(BACKUP_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1), "mtime": f.stat().st_mtime}
        for f in files
    ]


@router.post("/backup/restore")
def restore_backup(payload: dict) -> dict:
    from app.data.session import BACKUP_DIR, _active_database_url, _sqlite_url_to_path, backup_database, switch_database_url

    backup_name = (payload or {}).get("name", "")
    if not backup_name or "/" in backup_name:
        raise HTTPException(status_code=400, detail="invalid backup name")
    restore_path = BACKUP_DIR / backup_name
    if not restore_path.exists():
        raise HTTPException(status_code=404, detail=f"backup not found: {backup_name}")

    current_path = _sqlite_url_to_path(_active_database_url)
    if not current_path:
        raise HTTPException(status_code=400, detail="current database is not a local file")

    # Safety: backup current DB before restoring
    backup_database()

    import shutil
    shutil.copy2(restore_path, current_path)
    switch_database_url(_active_database_url)

    return {"restored_from": backup_name, "current_db": str(current_path)}


@router.get("/media")
def media_file(path: str = Query(..., min_length=1)) -> FileResponse:
    requested = _resolve_media_path(path)
    media_type = "application/octet-stream"
    guessed, _ = mimetypes.guess_type(str(requested))
    if guessed:
        media_type = guessed
    return FileResponse(path=str(requested), media_type=media_type)


@router.get("/media/view", response_class=HTMLResponse)
def media_view(request: Request, path: str = Query(..., min_length=1), return_to: str = Query("/dashboard")) -> str:
    requested = _resolve_media_path(path)
    media_type = mimetypes.guess_type(str(requested))[0] or "application/octet-stream"
    media_src = f"/media?path={quote(str(requested), safe='')}"
    title = requested.name
    safe_return_to = return_to if return_to.startswith("/") and not return_to.startswith("//") else "/dashboard"
    if media_type.startswith("image/"):
        body = f'<img class="viewer-media image" src="{media_src}" alt="{title}" />'
    elif media_type.startswith("video/"):
        body = f'<video class="viewer-media video" src="{media_src}" controls playsinline autoplay muted></video>'
    else:
        body = f'<a href="{media_src}">Download {title}</a>'
    return templates.TemplateResponse(request, "media_view.html", {
        "title": title,
        "body": body,
        "media_src": media_src,
        "return_to": safe_return_to,
    })


@router.get("/dashboard/calendar", response_class=HTMLResponse)
def dashboard_calendar_page(request: Request) -> str:
    return templates.TemplateResponse(request, "calendar.html")


@router.get("/dashboard/agent-apis", response_class=HTMLResponse)
def dashboard_agent_apis(request: Request, db: Session = Depends(get_db)) -> str:
    from app.services.agent_api_configs import list_integration_configs

    personas = [PersonaMeta(**row).model_dump(mode="json") for row in list_persona_catalog()]
    configs = [_serialize_agent_config(row).model_dump(mode="json") for row in list_agent_configs(db)]
    int_configs = list_integration_configs(db)
    db.commit()
    return templates.TemplateResponse(request, "agent_apis.html", {
        "personas_json": json.dumps(personas, ensure_ascii=False).replace("</", "<\\/"),
        "configs_json": json.dumps(configs, ensure_ascii=False).replace("</", "<\\/"),
        "env_vars_json": json.dumps(list_api_key_env_names(), ensure_ascii=False).replace("</", "<\\/"),
        "integration_configs_json": json.dumps(int_configs, ensure_ascii=False).replace("</", "<\\/"),
    })


@router.get("/pipeline-modes", response_model=list[PipelineModeView])
def list_pipeline_modes() -> list[PipelineModeView]:
    return _pipeline_mode_views()


# ── Shops & Categories ────────────────────────────────────────────

CHANNEL_ACCOUNT_REQUIREMENTS = {
    "shopify": ["store_domain", "access_token"],
    "meta": ["access_token", "ad_account_id"],
    "tiktok": ["access_token", "advertiser_id"],
    "notion": ["api_key", "database_id"],
}


def _account_fallback_credential(row: ShopChannelAccount, key: str) -> str:
    if key in {"ad_account_id", "advertiser_id"}:
        return row.account_id or row.account_key or ""
    if key == "store_domain":
        return row.account_id or row.account_url or row.account_key or ""
    if key == "database_id":
        return row.account_id or ""
    return ""


def _channel_missing_credentials(row: ShopChannelAccount) -> list[str]:
    required = CHANNEL_ACCOUNT_REQUIREMENTS.get(row.platform, [])
    env_vars = row.credential_env_vars if isinstance(row.credential_env_vars, dict) else {}
    missing = []
    for key in required:
        env_name = str(env_vars.get(key) or "").strip()
        has_env_value = bool(env_name and os.getenv(env_name))
        has_account_value = bool(_account_fallback_credential(row, key))
        if not has_env_value and not has_account_value:
            missing.append(key)
    return missing

def _serialize_shop(db: Session, workspace) -> dict:
    from app.data.models import GmMemory, PipelineRun, Product, Project, ShopChannelAccount, ShopSite
    from app.services.gm_memory import memory_dirty_reasons
    from app.services.shop_analysis import RESEARCH_SOURCE_TYPES

    project_ids = db.scalars(
        select(Project.id).where(Project.workspace_id == workspace.id)
    ).all()
    category_count = db.scalar(
        select(func.count(Project.id)).where(Project.workspace_id == workspace.id)
    ) or 0
    product_count = 0
    if project_ids:
        product_count = db.scalar(
            select(func.count(Product.id)).where(Product.project_id.in_(project_ids))
        ) or 0
    run_count = db.scalar(
        select(func.count(PipelineRun.id)).where(PipelineRun.workspace_id == workspace.id)
    ) or 0
    site_count = db.scalar(
        select(func.count(ShopSite.id)).where(ShopSite.workspace_id == workspace.id)
    ) or 0
    channel_count = db.scalar(
        select(func.count(ShopChannelAccount.id)).where(
            ShopChannelAccount.workspace_id == workspace.id,
            ShopChannelAccount.status != "archived",
        )
    ) or 0
    memory_rows = []
    if project_ids:
        raw_memory_rows = db.scalars(
            select(GmMemory).where(
                GmMemory.project_id.in_(project_ids),
                GmMemory.memory_scope == "shop",
                GmMemory.status == "active",
            )
        ).all()
        memory_rows = [
            row for row in raw_memory_rows
            if (row.content or {}).get("shop_id") in {None, "", workspace.id}
        ]
    analysis_rows = [
        row for row in memory_rows
        if row.memory_type == "research_intelligence" and row.source_type in RESEARCH_SOURCE_TYPES
    ]
    memory_dirty = [(row, memory_dirty_reasons(row)) for row in memory_rows]
    research_dirty = [(row, dirty) for row, dirty in memory_dirty if row in analysis_rows]
    memory_conflict_count = sum(1 for row, dirty in memory_dirty if "unresolved_conflicts" in dirty)
    return ShopItem(
        id=workspace.id,
        name=workspace.name,
        industry_code=workspace.industry_code or "general",
        store_url=workspace.store_url,
        description=workspace.description,
        category_count=category_count,
        product_count=product_count,
        run_count=run_count,
        analysis_count=len(analysis_rows),
        research_ready_count=sum(1 for row, dirty in research_dirty if not dirty),
        research_blocked_count=sum(1 for row, dirty in research_dirty if dirty),
        memory_count=len(memory_rows),
        memory_safe_count=sum(1 for row, dirty in memory_dirty if not dirty),
        memory_review_count=sum(1 for row, dirty in memory_dirty if dirty),
        memory_conflict_count=memory_conflict_count,
        archived_at=workspace.archived_at,
        last_analyzed_at=workspace.last_analyzed_at,
        site_count=site_count,
        channel_count=channel_count,
    ).model_dump()


def _get_shop_by_id_or_name(db: Session, shop_ref: str):
    from app.data.models import Workspace

    return db.scalar(
        select(Workspace).where(or_(Workspace.id == shop_ref, Workspace.name == shop_ref))
    )


def _serialize_shop_site(row: ShopSite) -> dict:
    return ShopSiteItem(
        id=row.id,
        workspace_id=row.workspace_id,
        label=row.label or "",
        url=row.url,
        site_type=row.site_type or "storefront",
        platform=row.platform,
        locale=row.locale,
        currency=row.currency,
        is_primary=bool(row.is_primary),
        metadata_json=row.metadata_json or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    ).model_dump()


def _serialize_channel_account(row: ShopChannelAccount) -> dict:
    missing = _channel_missing_credentials(row)
    return ShopChannelAccountItem(
        id=row.id,
        workspace_id=row.workspace_id,
        platform=row.platform,
        account_key=row.account_key,
        label=row.label or "",
        account_id=row.account_id,
        account_url=row.account_url,
        credential_env_vars=row.credential_env_vars or {},
        sync_settings=row.sync_settings or {},
        attribution_rules=row.attribution_rules or {},
        status=row.status or "active",
        is_primary=bool(row.is_primary),
        ready=(row.status != "archived" and not missing),
        missing_credentials=missing,
        last_verified_at=row.last_verified_at,
        last_sync_at=row.last_sync_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    ).model_dump()


def _normalize_short(value: str | None, default: str = "") -> str:
    text = (value or "").strip()
    return text or default


def _normalize_optional(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _validate_credential_env_vars(payload: dict | None) -> dict:
    result = dict(payload or {})
    for key, value in result.items():
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.startswith("CRISPY_API_KEY_"):
            raise HTTPException(status_code=400, detail=f"credential env var for {key} must start with CRISPY_API_KEY_")
    return result


def _ensure_single_primary_site(db: Session, workspace_id: str, primary_id: str) -> None:
    rows = db.scalars(select(ShopSite).where(ShopSite.workspace_id == workspace_id, ShopSite.id != primary_id)).all()
    for row in rows:
        row.is_primary = False


def _ensure_single_primary_channel(db: Session, workspace_id: str, platform: str, primary_id: str) -> None:
    rows = db.scalars(
        select(ShopChannelAccount).where(
            ShopChannelAccount.workspace_id == workspace_id,
            ShopChannelAccount.platform == platform,
            ShopChannelAccount.id != primary_id,
        )
    ).all()
    for row in rows:
        row.is_primary = False


def _upsert_primary_site_from_store_url(db: Session, workspace: Workspace, store_url: str | None) -> None:
    url = _normalize_optional(store_url)
    if not url:
        return
    existing = db.scalar(
        select(ShopSite).where(ShopSite.workspace_id == workspace.id, ShopSite.url == url)
    )
    if not existing:
        existing = ShopSite(
            workspace_id=workspace.id,
            label="Primary Storefront",
            url=url,
            site_type="storefront",
            is_primary=True,
        )
        db.add(existing)
        db.flush()
    existing.is_primary = True
    _ensure_single_primary_site(db, workspace.id, existing.id)


@router.get("/shops", response_model=ShopListResponse)
def list_shops(
    include_archived: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    from app.data.models import Workspace

    rows = db.scalars(select(Workspace).order_by(Workspace.name)).all()
    if not rows:
        ws = Workspace(name="workspace_demo", industry_code="general")
        db.add(ws)
        db.commit()
        db.refresh(ws)
        rows = [ws]
    if not include_archived:
        rows = [row for row in rows if not row.archived_at]
    return {
        "shops": [_serialize_shop(db, row) for row in rows]
    }


@router.get("/shops/{shop_name}/categories", response_model=CategoryListResponse)
def list_shop_categories(shop_name: str, db: Session = Depends(get_db)) -> dict:
    from app.data.models import Project

    workspace = _get_shop_by_id_or_name(db, shop_name)
    if not workspace:
        return {"categories": []}
    rows = db.scalars(
        select(Project.name).where(Project.workspace_id == workspace.id).order_by(Project.name)
    ).all()
    return {"categories": [CategoryItem(name=r).model_dump() for r in rows]}


@router.post("/shops", response_model=ShopItem, status_code=201)
def create_shop(payload: ShopItem, db: Session = Depends(get_db)) -> dict:
    from app.data.models import Workspace
    existing = db.scalar(select(Workspace).where(Workspace.name == payload.name))
    if existing:
        raise HTTPException(status_code=409, detail=f"shop already exists: {payload.name}")
    ws = Workspace(
        name=payload.name,
        industry_code=payload.industry_code or "general",
        store_url=payload.store_url,
        description=payload.description,
    )
    db.add(ws)
    db.flush()
    _upsert_primary_site_from_store_url(db, ws, ws.store_url)
    db.commit()
    db.refresh(ws)
    return _serialize_shop(db, ws)


@router.patch("/shops/{shop_id}", response_model=ShopItem)
def update_shop(shop_id: str, payload: ShopPatchRequest, db: Session = Depends(get_db)) -> dict:
    from app.data.models import Workspace

    ws = db.get(Workspace, shop_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
    if payload.name is not None:
        new_name = payload.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="shop name cannot be empty")
        if new_name != ws.name:
            conflict = db.scalar(select(Workspace).where(Workspace.name == new_name))
            if conflict:
                raise HTTPException(status_code=409, detail=f"shop already exists: {new_name}")
            ws.name = new_name
    if payload.industry_code is not None:
        ws.industry_code = payload.industry_code.strip() or "general"
    if payload.store_url is not None:
        ws.store_url = payload.store_url.strip() or None
        _upsert_primary_site_from_store_url(db, ws, ws.store_url)
    if payload.description is not None:
        ws.description = payload.description.strip() or None
    if payload.archived is True and not ws.archived_at:
        ws.archived_at = datetime.now(UTC)
    if payload.archived is False:
        ws.archived_at = None
    db.commit()
    db.refresh(ws)
    return _serialize_shop(db, ws)


@router.get("/shops/{shop_id}/sites", response_model=ShopSiteListResponse)
def list_shop_sites(shop_id: str, db: Session = Depends(get_db)) -> dict:
    ws = _get_shop_by_id_or_name(db, shop_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
    rows = db.scalars(
        select(ShopSite).where(ShopSite.workspace_id == ws.id).order_by(ShopSite.is_primary.desc(), ShopSite.created_at)
    ).all()
    return {"sites": [_serialize_shop_site(row) for row in rows]}


@router.post("/shops/{shop_id}/sites", response_model=ShopSiteItem, status_code=201)
def create_shop_site(shop_id: str, payload: ShopSiteItem, db: Session = Depends(get_db)) -> dict:
    ws = _get_shop_by_id_or_name(db, shop_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
    url = payload.url.strip()
    existing = db.scalar(select(ShopSite).where(ShopSite.workspace_id == ws.id, ShopSite.url == url))
    if existing:
        raise HTTPException(status_code=409, detail=f"site already exists for shop: {url}")
    row = ShopSite(
        workspace_id=ws.id,
        label=_normalize_short(payload.label, "Storefront"),
        url=url,
        site_type=_normalize_short(payload.site_type, "storefront"),
        platform=_normalize_optional(payload.platform),
        locale=_normalize_optional(payload.locale),
        currency=_normalize_optional(payload.currency),
        is_primary=bool(payload.is_primary),
        metadata_json=payload.metadata_json or {},
    )
    db.add(row)
    db.flush()
    if row.is_primary:
        _ensure_single_primary_site(db, ws.id, row.id)
        ws.store_url = row.url
    db.commit()
    db.refresh(row)
    return _serialize_shop_site(row)


@router.patch("/shops/{shop_id}/sites/{site_id}", response_model=ShopSiteItem)
def update_shop_site(shop_id: str, site_id: str, payload: ShopSitePatchRequest, db: Session = Depends(get_db)) -> dict:
    ws = _get_shop_by_id_or_name(db, shop_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
    row = db.scalar(select(ShopSite).where(ShopSite.id == site_id, ShopSite.workspace_id == ws.id))
    if not row:
        raise HTTPException(status_code=404, detail=f"site not found: {site_id}")
    if payload.url is not None:
        new_url = payload.url.strip()
        conflict = db.scalar(select(ShopSite).where(ShopSite.workspace_id == ws.id, ShopSite.url == new_url, ShopSite.id != row.id))
        if conflict:
            raise HTTPException(status_code=409, detail=f"site already exists for shop: {new_url}")
        row.url = new_url
    if payload.label is not None:
        row.label = _normalize_short(payload.label, "Storefront")
    if payload.site_type is not None:
        row.site_type = _normalize_short(payload.site_type, "storefront")
    if payload.platform is not None:
        row.platform = _normalize_optional(payload.platform)
    if payload.locale is not None:
        row.locale = _normalize_optional(payload.locale)
    if payload.currency is not None:
        row.currency = _normalize_optional(payload.currency)
    if payload.metadata_json is not None:
        row.metadata_json = payload.metadata_json
    if payload.is_primary is not None:
        row.is_primary = bool(payload.is_primary)
    if row.is_primary:
        _ensure_single_primary_site(db, ws.id, row.id)
        ws.store_url = row.url
    db.commit()
    db.refresh(row)
    return _serialize_shop_site(row)


@router.delete("/shops/{shop_id}/sites/{site_id}", status_code=204)
def delete_shop_site(shop_id: str, site_id: str, db: Session = Depends(get_db)) -> None:
    ws = _get_shop_by_id_or_name(db, shop_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
    row = db.scalar(select(ShopSite).where(ShopSite.id == site_id, ShopSite.workspace_id == ws.id))
    if not row:
        raise HTTPException(status_code=404, detail=f"site not found: {site_id}")
    was_primary = bool(row.is_primary)
    db.delete(row)
    db.flush()
    if was_primary:
        replacement = db.scalar(select(ShopSite).where(ShopSite.workspace_id == ws.id).order_by(ShopSite.created_at).limit(1))
        if replacement:
            replacement.is_primary = True
            ws.store_url = replacement.url
        else:
            ws.store_url = None
    db.commit()
    return None


@router.get("/shops/{shop_id}/channel-accounts", response_model=ShopChannelAccountListResponse)
def list_shop_channel_accounts(shop_id: str, include_archived: bool = Query(False), db: Session = Depends(get_db)) -> dict:
    ws = _get_shop_by_id_or_name(db, shop_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
    stmt = select(ShopChannelAccount).where(ShopChannelAccount.workspace_id == ws.id)
    if not include_archived:
        stmt = stmt.where(ShopChannelAccount.status != "archived")
    rows = db.scalars(stmt.order_by(ShopChannelAccount.platform, ShopChannelAccount.is_primary.desc(), ShopChannelAccount.label)).all()
    return {"accounts": [_serialize_channel_account(row) for row in rows]}


@router.post("/shops/{shop_id}/channel-accounts", response_model=ShopChannelAccountItem, status_code=201)
def create_shop_channel_account(shop_id: str, payload: ShopChannelAccountItem, db: Session = Depends(get_db)) -> dict:
    ws = _get_shop_by_id_or_name(db, shop_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
    platform = payload.platform.lower().strip()
    account_key = payload.account_key.strip()
    existing = db.scalar(
        select(ShopChannelAccount).where(
            ShopChannelAccount.workspace_id == ws.id,
            ShopChannelAccount.platform == platform,
            ShopChannelAccount.account_key == account_key,
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"channel account already exists: {platform}/{account_key}")
    row = ShopChannelAccount(
        workspace_id=ws.id,
        platform=platform,
        account_key=account_key,
        label=_normalize_short(payload.label, account_key),
        account_id=_normalize_optional(payload.account_id),
        account_url=_normalize_optional(payload.account_url),
        credential_env_vars=_validate_credential_env_vars(payload.credential_env_vars),
        sync_settings=payload.sync_settings or {},
        attribution_rules=payload.attribution_rules or {},
        status=payload.status or "active",
        is_primary=bool(payload.is_primary),
    )
    db.add(row)
    db.flush()
    if row.is_primary:
        _ensure_single_primary_channel(db, ws.id, row.platform, row.id)
    db.commit()
    db.refresh(row)
    return _serialize_channel_account(row)


@router.patch("/shops/{shop_id}/channel-accounts/{account_id}", response_model=ShopChannelAccountItem)
def update_shop_channel_account(shop_id: str, account_id: str, payload: ShopChannelAccountPatchRequest, db: Session = Depends(get_db)) -> dict:
    ws = _get_shop_by_id_or_name(db, shop_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
    row = db.scalar(select(ShopChannelAccount).where(ShopChannelAccount.id == account_id, ShopChannelAccount.workspace_id == ws.id))
    if not row:
        raise HTTPException(status_code=404, detail=f"channel account not found: {account_id}")
    new_platform = payload.platform.lower().strip() if payload.platform is not None else row.platform
    new_key = payload.account_key.strip() if payload.account_key is not None else row.account_key
    if new_platform != row.platform or new_key != row.account_key:
        conflict = db.scalar(
            select(ShopChannelAccount).where(
                ShopChannelAccount.workspace_id == ws.id,
                ShopChannelAccount.platform == new_platform,
                ShopChannelAccount.account_key == new_key,
                ShopChannelAccount.id != row.id,
            )
        )
        if conflict:
            raise HTTPException(status_code=409, detail=f"channel account already exists: {new_platform}/{new_key}")
        row.platform = new_platform
        row.account_key = new_key
    if payload.label is not None:
        row.label = _normalize_short(payload.label, row.account_key)
    if payload.account_id is not None:
        row.account_id = _normalize_optional(payload.account_id)
    if payload.account_url is not None:
        row.account_url = _normalize_optional(payload.account_url)
    if payload.credential_env_vars is not None:
        row.credential_env_vars = _validate_credential_env_vars(payload.credential_env_vars)
    if payload.sync_settings is not None:
        row.sync_settings = payload.sync_settings
    if payload.attribution_rules is not None:
        row.attribution_rules = payload.attribution_rules
    if payload.status is not None:
        row.status = payload.status
    if payload.is_primary is not None:
        row.is_primary = bool(payload.is_primary)
    if row.is_primary:
        _ensure_single_primary_channel(db, ws.id, row.platform, row.id)
    db.commit()
    db.refresh(row)
    return _serialize_channel_account(row)


@router.delete("/shops/{shop_id}/channel-accounts/{account_id}", status_code=204)
def archive_shop_channel_account(shop_id: str, account_id: str, db: Session = Depends(get_db)) -> None:
    ws = _get_shop_by_id_or_name(db, shop_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
    row = db.scalar(select(ShopChannelAccount).where(ShopChannelAccount.id == account_id, ShopChannelAccount.workspace_id == ws.id))
    if not row:
        raise HTTPException(status_code=404, detail=f"channel account not found: {account_id}")
    row.status = "archived"
    row.is_primary = False
    db.commit()
    return None


@router.put("/shops/{shop_name}", response_model=ShopItem)
def rename_shop(shop_name: str, payload: ShopItem, db: Session = Depends(get_db)) -> dict:
    from app.data.models import Workspace
    ws = _get_shop_by_id_or_name(db, shop_name)
    if not ws:
        raise HTTPException(status_code=404, detail=f"shop not found: {shop_name}")
    if payload.name and payload.name != shop_name:
        conflict = db.scalar(select(Workspace).where(Workspace.name == payload.name))
        if conflict:
            raise HTTPException(status_code=409, detail=f"shop already exists: {payload.name}")
        ws.name = payload.name
    if payload.industry_code:
        ws.industry_code = payload.industry_code
    if payload.store_url is not None:
        ws.store_url = payload.store_url.strip() or None
        _upsert_primary_site_from_store_url(db, ws, ws.store_url)
    if payload.description is not None:
        ws.description = payload.description.strip() or None
    db.commit()
    db.refresh(ws)
    return _serialize_shop(db, ws)


@router.delete("/shops/{shop_name}", status_code=204)
def delete_shop(shop_name: str, db: Session = Depends(get_db)):
    from app.data.models import PipelineRun, Workspace

    ws = _get_shop_by_id_or_name(db, shop_name)
    if not ws:
        raise HTTPException(status_code=404, detail=f"shop not found: {shop_name}")
    run_count = db.scalar(
        select(func.count(PipelineRun.id)).where(PipelineRun.workspace_id == ws.id)
    )
    if run_count and run_count > 0:
        raise HTTPException(status_code=409, detail=f"Shop has {run_count} runs, cannot delete")
    # Also block if shop is the only/default one
    total = db.scalar(select(func.count(Workspace.id)))
    if total and total <= 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last shop")
    if run_count and run_count > 0:
        ws.archived_at = datetime.now(UTC)
        db.commit()
        return None
    db.delete(ws)
    db.commit()


# ── Shop Analysis ─────────────────────────────────────────────────

def _shop_analysis_tool_status(config: dict) -> dict:
    from app.services.shop_analysis import shop_analysis_tool_status

    return shop_analysis_tool_status(config)


@router.get("/shop-analysis/preflight", response_model=ShopAnalysisPreflightResponse)
def shop_analysis_preflight(db: Session = Depends(get_db)) -> dict:
    from app.services.agent_api_configs import resolve_agent_config

    config = resolve_agent_config(db, agent_name="shop_analyst", run_provider="", run_model="")
    tool_status = _shop_analysis_tool_status(config)
    checks = [
        {"key": "shop_analyst.llm", "severity": "ok" if tool_status["llm"]["available"] else "error", **tool_status["llm"]},
        {"key": "shop_analyst.tavily", "severity": "ok" if tool_status["tavily"]["available"] else "warn", **tool_status["tavily"]},
        {"key": "shop_analyst.firecrawl", "severity": "ok" if tool_status["firecrawl"]["available"] else "warn", **tool_status["firecrawl"]},
    ]
    has_llm = tool_status["llm"]["available"]
    has_external_research = tool_status["tavily"]["available"] or tool_status["firecrawl"]["available"]
    severity = "ok" if has_llm and has_external_research else "warn" if has_llm else "error"
    return {"ok": has_llm, "severity": severity, "checks": checks}


@router.get("/research-context")
def research_context(
    workspace_name: str = Query(default="workspace_demo"),
    project_name: str = Query(default="shop_analysis"),
    shop_id: str | None = Query(default=None),
    industry_code: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    from app.services.research_context import build_research_context
    from app.services.shop_analysis import _get_or_create_workspace_project

    if shop_id:
        shop = _get_shop_by_id_or_name(db, shop_id)
        if not shop:
            raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
        _, project = _get_or_create_workspace_project(db, shop.name, project_name or "shop_analysis")
        return build_research_context(
            db,
            project_id=project.id,
            shop_id=shop.id,
            industry_code=industry_code or shop.industry_code,
        )
    workspace, project = _get_or_create_workspace_project(db, workspace_name, project_name)
    return build_research_context(
        db,
        project_id=project.id,
        shop_id=workspace.id,
        industry_code=industry_code or workspace.industry_code,
    )


@router.get("/run-memory-candidates")
def run_memory_candidates(
    workspace_name: str = Query(default=""),
    project_name: str | None = Query(default=None),
    product_code: str | None = Query(default=None),
    industry_code: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    from app.services.memory_selection import build_run_memory_candidates

    return build_run_memory_candidates(
        db,
        workspace_name=workspace_name,
        project_name=project_name,
        product_code=product_code,
        industry_code=industry_code,
    )


@router.post("/shop-analysis/run", response_model=ShopAnalysisResponse)
def run_shop_analysis(
    payload: ShopAnalysisRequest,
    db: Session = Depends(get_db),
) -> dict:
    from app.services.agent_api_configs import resolve_agent_config
    from app.services.shop_analysis import (
        _get_or_create_workspace_project,
        create_research_task,
        execute_research_task,
        research_task_to_dict,
        shop_analysis_tool_status,
    )
    from app.data.models import Workspace

    shop = db.get(Workspace, payload.shop_id) if payload.shop_id else None
    if payload.shop_id and not shop:
        raise HTTPException(status_code=404, detail=f"shop not found: {payload.shop_id}")
    if shop:
        workspace, project = _get_or_create_workspace_project(
            db, shop.name, payload.project_name if payload.project_name else "shop_analysis"
        )
        if payload.industry_code:
            workspace.industry_code = payload.industry_code
        if payload.store_url:
            workspace.store_url = payload.store_url
        if payload.description:
            workspace.description = payload.description
    else:
        workspace, project = _get_or_create_workspace_project(
            db, payload.workspace_name, payload.project_name
        )
        shop = workspace
    config = resolve_agent_config(db, agent_name="shop_analyst", run_provider="", run_model="")
    tool_status = shop_analysis_tool_status(config)
    task = create_research_task(
        db,
        project_id=project.id,
        shop_id=shop.id if shop else None,
        shop_name=shop.name if shop else None,
        store_url=payload.store_url,
        industry_code=payload.industry_code,
        task_type=payload.research_focus,
        source="manual",
        refresh_reason=payload.refresh_reason,
        payload=payload.model_dump(mode="json"),
    )
    db.commit()

    if payload.execution_mode == "sync":
        return execute_research_task(db, task.id)

    return ShopAnalysisResponse(
        id=task.id,
        shop_id=shop.id if shop else None,
        shop_name=shop.name if shop else None,
        store_url=payload.store_url,
        industry_code=payload.industry_code,
        profile=None,
        competitor_analysis=None,
        extended_results=[],
        status="queued",
        research_focus=payload.research_focus,
        task=research_task_to_dict(task),
        tool_status=tool_status,
        error_message=None,
        created_at=datetime.now(UTC),
    ).model_dump(mode="json")


@router.get("/shop-analysis/history", response_model=ShopAnalysisHistoryResponse)
def shop_analysis_history(
    shop_id: str | None = Query(default=None),
    workspace_name: str = Query(default="workspace_demo"),
    project_name: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    from app.services.shop_analysis import _get_or_create_workspace_project
    if shop_id:
        shop = _get_shop_by_id_or_name(db, shop_id)
        if not shop:
            raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
        _, project = _get_or_create_workspace_project(db, shop.name, "shop_analysis")
        items = list_shop_analyses(db, project.id, limit=limit, shop_id=shop.id)
    else:
        _, project = _get_or_create_workspace_project(db, workspace_name, project_name)
        items = list_shop_analyses(db, project.id, limit=limit)
    return {"items": items}


@router.get("/shop-analysis/tasks", response_model=ResearchTaskHistoryResponse)
def shop_analysis_tasks(
    shop_id: str | None = Query(default=None),
    workspace_name: str = Query(default="workspace_demo"),
    project_name: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    from app.services.shop_analysis import _get_or_create_workspace_project, list_research_tasks

    if shop_id:
        shop = _get_shop_by_id_or_name(db, shop_id)
        if not shop:
            raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
        _, project = _get_or_create_workspace_project(db, shop.name, "shop_analysis")
        items = list_research_tasks(db, project_id=project.id, shop_id=shop.id, limit=limit)
    else:
        _, project = _get_or_create_workspace_project(db, workspace_name, project_name)
        items = list_research_tasks(db, project_id=project.id, limit=limit)
    return {"items": items}


@router.get("/shop-analysis/queue/status")
def shop_analysis_queue_status() -> dict:
    from app.orchestrator.research_worker import research_worker

    return research_worker.get_status()


@router.post("/shop-analysis/refresh-due")
def queue_due_shop_analysis_refreshes(
    shop_id: str | None = Query(default=None),
    workspace_name: str = Query(default="workspace_demo"),
    project_name: str = Query(default="shop_analysis"),
    include_refresh_soon: bool = Query(default=True),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    from app.services.shop_analysis import _get_or_create_workspace_project, enqueue_due_research_refreshes

    if shop_id:
        shop = _get_shop_by_id_or_name(db, shop_id)
        if not shop:
            raise HTTPException(status_code=404, detail=f"shop not found: {shop_id}")
        _, project = _get_or_create_workspace_project(db, shop.name, "shop_analysis")
        result = enqueue_due_research_refreshes(
            db,
            project_id=project.id,
            shop_id=shop.id,
            include_refresh_soon=include_refresh_soon,
            limit=limit,
        )
    else:
        _, project = _get_or_create_workspace_project(db, workspace_name, project_name)
        result = enqueue_due_research_refreshes(
            db,
            project_id=project.id,
            include_refresh_soon=include_refresh_soon,
            limit=limit,
        )
    db.commit()
    return result


@router.get("/shop-analysis/tasks/{task_id}", response_model=ResearchTaskItem)
def get_shop_analysis_task(
    task_id: str,
    db: Session = Depends(get_db),
) -> dict:
    from app.services.shop_analysis import get_research_task, research_task_to_dict

    task = get_research_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"research task not found: {task_id}")
    return research_task_to_dict(task)


@router.post("/shop-analysis/tasks/{task_id}/execute", response_model=ShopAnalysisResponse)
def execute_shop_analysis_task(
    task_id: str,
    db: Session = Depends(get_db),
) -> dict:
    from app.services.shop_analysis import execute_research_task, get_research_task

    task = get_research_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"research task not found: {task_id}")
    if task.status not in {"queued", "failed", "running", "completed"}:
        raise HTTPException(status_code=409, detail=f"research task cannot be executed from status={task.status}")
    try:
        return execute_research_task(db, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ── Creative Preset CRUD ──────────────────────────────────────────

@router.get("/creative-presets", response_model=CreativePresetListResponse)
def list_presets(
    workspace_name: str = Query(default="workspace_demo"),
    db: Session = Depends(get_db),
) -> dict:
    system = []
    for key, spec in list_system_presets().items():
        system.append({"key": key, "storyboard_candidate_count": int(spec.get("storyboard_candidate_count") or 1), **spec})
    user = list_user_presets(db, workspace_name)
    return {"system": system, "user": [_creative_preset_view(p) for p in user]}


def _creative_preset_view(preset) -> CreativePresetView:
    return CreativePresetView(
        id=preset.id,
        workspace_name=preset.workspace_name,
        name=preset.name,
        image_size=preset.image_size,
        video_size=preset.video_size,
        resolution=preset.resolution,
        video_duration_seconds=preset.video_duration_seconds,
        storyboard_candidate_count=extract_storyboard_candidate_count(preset.platform_targets),
        tiktok_video_style=extract_tiktok_video_style(preset.platform_targets),
        site_surface=extract_site_surface(preset.platform_targets),
        platform_targets=preset.platform_targets or {},
        created_at=preset.created_at,
        updated_at=preset.updated_at,
    )


@router.post("/creative-presets", response_model=CreativePresetView, status_code=201)
def create_preset(payload: CreativePresetCreate, db: Session = Depends(get_db)) -> CreativePresetView:
    try:
        preset = create_creative_preset(
            db,
            workspace_name=payload.workspace_name,
            name=payload.name,
            image_size=payload.image_size,
            video_size=payload.video_size,
            resolution=payload.resolution,
            video_duration_seconds=payload.video_duration_seconds,
            platform_targets=with_preset_metadata(
                payload.platform_targets,
                storyboard_candidate_count=payload.storyboard_candidate_count,
                tiktok_video_style=payload.tiktok_video_style,
                site_surface=payload.site_surface,
            ),
        )
        db.commit()
        db.refresh(preset)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _creative_preset_view(preset)


@router.put("/creative-presets/{preset_id}", response_model=CreativePresetView)
def update_preset(preset_id: str, payload: CreativePresetUpdate, db: Session = Depends(get_db)) -> CreativePresetView:
    try:
        existing = get_creative_preset(db, preset_id)
        preset = update_creative_preset(
            db,
            preset_id,
            name=payload.name,
            image_size=payload.image_size,
            video_size=payload.video_size,
            resolution=payload.resolution,
            video_duration_seconds=payload.video_duration_seconds,
            platform_targets=with_preset_metadata(
                payload.platform_targets if payload.platform_targets is not None else (existing.platform_targets or {}),
                storyboard_candidate_count=payload.storyboard_candidate_count if payload.storyboard_candidate_count is not None else extract_storyboard_candidate_count(existing.platform_targets),
                tiktok_video_style=payload.tiktok_video_style if payload.tiktok_video_style is not None else extract_tiktok_video_style(existing.platform_targets),
                site_surface=payload.site_surface if payload.site_surface is not None else extract_site_surface(existing.platform_targets),
            ),
        )
        db.commit()
        db.refresh(preset)
    except ValueError as exc:
        db.rollback()
        status = 409 if "already exists" in str(exc) else 404
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _creative_preset_view(preset)


@router.delete("/creative-presets/{preset_id}", status_code=204)
def delete_preset(preset_id: str, db: Session = Depends(get_db)):
    try:
        delete_creative_preset(db, preset_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ── Run Template CRUD ────────────────────────────────────────────

@router.get("/run-templates", response_model=list[RunTemplateView])
def list_templates(
    workspace_name: str = Query(default="workspace_demo"),
    db: Session = Depends(get_db),
) -> list[RunTemplateView]:
    templates = list_run_templates(db, workspace_name)
    return [RunTemplateView.model_validate(t) for t in templates]


@router.post("/run-templates", response_model=RunTemplateView, status_code=201)
def create_template(payload: RunTemplateCreate, db: Session = Depends(get_db)) -> RunTemplateView:
    try:
        template = create_run_template(
            db,
            workspace_name=payload.workspace_name,
            name=payload.name,
            config_json=payload.config_json,
            is_shared=payload.is_shared,
        )
        db.commit()
        db.refresh(template)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunTemplateView.model_validate(template)


@router.put("/run-templates/{template_id}", response_model=RunTemplateView)
def update_template(template_id: str, payload: RunTemplateUpdate, db: Session = Depends(get_db)) -> RunTemplateView:
    try:
        template = update_run_template(
            db,
            template_id,
            name=payload.name,
            config_json=payload.config_json,
            is_shared=payload.is_shared,
        )
        db.commit()
        db.refresh(template)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RunTemplateView.model_validate(template)


@router.delete("/run-templates/{template_id}", status_code=204)
def delete_template(template_id: str, db: Session = Depends(get_db)):
    try:
        delete_run_template(db, template_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ── Product Config Hint ──────────────────────────────────────────

@router.get("/product-config-hint", response_model=ProductConfigHint | None)
def product_config_hint(
    product_code: str = Query(...),
    db: Session = Depends(get_db),
) -> dict | None:
    return get_last_product_config(db, product_code)


@router.post("/runs/preflight", response_model=RunPreflightResponse)
def preflight_pipeline_run(payload: RunPreflightRequest, db: Session = Depends(get_db)) -> RunPreflightResponse:
    result = preflight_run_capabilities(
        db,
        pipeline_mode=payload.pipeline_mode,
        has_image_inputs=payload.has_image_inputs,
        has_video_inputs=payload.has_video_inputs,
        creative_specs=payload.creative_specs,
    )
    return RunPreflightResponse(**result)


@router.get("/runs", response_model=list[RunSummary])
def list_runs(db: Session = Depends(get_db)) -> list[RunSummary]:
    from app.services.run_media import run_media_summaries

    runs = db.scalars(select(PipelineRun).order_by(desc(PipelineRun.created_at)).limit(50)).all()
    media = run_media_summaries(db, [run.id for run in runs])
    return [
        RunSummary(
            id=run.id,
            status=run.status,
            current_stage=run.current_stage,
            pipeline_mode=run.pipeline_mode,
            project_id=run.project_id,
            product_code=run.product_code or "",
            industry_code=run.industry_code or "",
            updated_at=run.updated_at,
            **media.get(run.id, {}),
        )
        for run in runs
    ]


@router.get("/runs/{run_id}/media", response_model=RunMediaResponse)
def run_media_gallery(run_id: str, db: Session = Depends(get_db)) -> RunMediaResponse:
    from app.services.run_media import run_media_summaries

    if db.get(PipelineRun, run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return RunMediaResponse(run_id=run_id, **run_media_summaries(db, [run_id], limit=None).get(run_id, {}))


def _gm_memory_item(row: GmMemory) -> GmMemoryItem:
    return GmMemoryItem(
        id=row.id,
        project_id=row.project_id,
        run_id=row.run_id,
        memory_scope=row.memory_scope,
        product_code=row.product_code,
        industry_code=row.industry_code,
        source_type=row.source_type,
        memory_type=row.memory_type,
        status=row.status,
        pinned=bool(row.pinned),
        score_hint=row.score_hint,
        content=row.content or {},
        created_at=row.created_at,
    )


@router.get("/gm-memory", response_model=list[GmMemoryItem])
def list_gm_memory(
    project_id: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    product_code: str | None = Query(default=None),
    industry_code: str | None = Query(default=None),
    source_type: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
    status: str | None = Query(default="active"),
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[GmMemoryItem]:
    query = select(GmMemory).order_by(desc(GmMemory.pinned), desc(GmMemory.created_at))
    if project_id:
        query = query.where(GmMemory.project_id == project_id)
    if scope:
        query = query.where(GmMemory.memory_scope == scope)
    if product_code:
        query = query.where(GmMemory.product_code == product_code)
    if industry_code:
        query = query.where(GmMemory.industry_code == industry_code)
    if source_type:
        query = query.where(GmMemory.source_type == source_type)
    if memory_type:
        query = query.where(GmMemory.memory_type == memory_type)
    if status:
        query = query.where(GmMemory.status == status)
    rows = db.scalars(query.limit(limit)).all()
    return [_gm_memory_item(row) for row in rows]


@router.post("/gm-memory/compact", response_model=GmMemoryItem)
def compact_gm_memory_endpoint(
    payload: GmMemoryCompactRequest,
    db: Session = Depends(get_db),
) -> GmMemoryItem:
    row = compact_gm_memory(
        db,
        project_id=payload.project_id,
        memory_scope=payload.memory_scope,
        product_code=payload.product_code,
        industry_code=payload.industry_code,
        shop_id=payload.shop_id,
        limit=payload.limit,
    )
    if not row:
        raise HTTPException(status_code=404, detail="No active raw GM memory to compact")
    db.commit()
    db.refresh(row)
    return _gm_memory_item(row)


@router.patch("/gm-memory/{memory_id}", response_model=GmMemoryItem)
def update_gm_memory(
    memory_id: str,
    payload: GmMemoryUpdateRequest,
    db: Session = Depends(get_db),
) -> GmMemoryItem:
    row = db.get(GmMemory, memory_id)
    if not row:
        raise HTTPException(status_code=404, detail="GM memory not found")
    if payload.status is not None:
        row.status = payload.status
    if payload.pinned is not None:
        row.pinned = payload.pinned
    if payload.superseded_by_id:
        content = dict(row.content or {})
        content["superseded_by_id"] = payload.superseded_by_id
        row.content = content
        row.status = "superseded"
    db.add(row)
    db.commit()
    db.refresh(row)
    return _gm_memory_item(row)


@router.post("/gm-memory/{memory_id}/review", response_model=GmMemoryItem)
def review_research_memory(
    memory_id: str,
    payload: GmMemoryReviewActionRequest,
    db: Session = Depends(get_db),
) -> GmMemoryItem:
    row = db.get(GmMemory, memory_id)
    if not row:
        raise HTTPException(status_code=404, detail="GM memory not found")
    if row.memory_type != "research_intelligence":
        raise HTTPException(status_code=400, detail="Review actions are only supported for research_intelligence memory")

    content = dict(row.content or {})
    review_log = list(content.get("review_log") or [])
    review_event = {
        "action": payload.action,
        "changed_by": payload.changed_by,
        "notes": payload.notes or "",
        "reviewed_at": datetime.now(UTC).isoformat(),
    }
    review_log.append(review_event)

    if payload.action == "approve":
        row.status = "active"
        row.pinned = True
        content["review_status"] = "approved"
    elif payload.action == "pin":
        row.pinned = True
        content["review_status"] = content.get("review_status") or "pinned"
    elif payload.action == "unpin":
        row.pinned = False
        content["review_status"] = content.get("review_status") or "unreviewed"
    elif payload.action == "reject":
        row.status = "archived"
        row.pinned = False
        content["review_status"] = "rejected"
    elif payload.action == "resolve_conflicts":
        resolved = []
        for conflict in content.get("conflicts") or []:
            item = dict(conflict or {})
            item["status"] = "resolved"
            item["resolved_by"] = payload.changed_by
            item["resolved_at"] = review_event["reviewed_at"]
            if payload.notes:
                item["resolution_notes"] = payload.notes
            resolved.append(item)
        content["conflicts"] = resolved
        content["conflict_count"] = 0
        content["review_status"] = "conflicts_resolved"
    else:
        raise HTTPException(status_code=400, detail="Unsupported review action")

    content["review_log"] = review_log
    content["reviewed_at"] = review_event["reviewed_at"]
    content["reviewed_by"] = payload.changed_by
    if payload.notes:
        content["review_notes"] = payload.notes
    row.content = content
    db.add(row)
    db.commit()
    db.refresh(row)
    return _gm_memory_item(row)


@router.get("/gm-reflections", response_model=list[GmReflectionItem])
def list_gm_reflections(
    scope: str | None = Query(default=None),
    reflection_type: str | None = Query(default=None),
    product_code: str | None = Query(default=None),
    industry_code: str | None = Query(default=None),
    pipeline_mode: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[GmReflectionItem]:
    query = select(GmReflection).order_by(desc(GmReflection.created_at))
    if scope:
        query = query.where(GmReflection.target_scope == scope)
    if reflection_type:
        query = query.where(GmReflection.reflection_type == reflection_type)
    if product_code:
        query = query.where(GmReflection.product_code == product_code)
    if industry_code:
        query = query.where(GmReflection.industry_code == industry_code)
    if pipeline_mode:
        query = query.where(GmReflection.pipeline_mode == pipeline_mode)
    rows = db.scalars(query.limit(limit)).all()
    return [
        GmReflectionItem(
            id=row.id,
            project_id=row.project_id,
            run_id=row.run_id,
            feedback_import_id=row.feedback_import_id,
            reflection_type=row.reflection_type,
            target_scope=row.target_scope,
            shop_id=row.shop_id,
            product_code=row.product_code,
            industry_code=row.industry_code,
            pipeline_mode=row.pipeline_mode,
            confidence_score=row.confidence_score,
            evidence_count=row.evidence_count,
            summary=row.summary,
            payload=row.payload or {},
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/gm-policies", response_model=list[GmPolicyItem])
def list_gm_policies(
    status: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    product_code: str | None = Query(default=None),
    industry_code: str | None = Query(default=None),
    pipeline_mode: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[GmPolicyItem]:
    query = select(GmPolicyVersion).order_by(desc(GmPolicyVersion.created_at))
    if status:
        query = query.where(GmPolicyVersion.status == status)
    if scope:
        query = query.where(GmPolicyVersion.target_scope == scope)
    if product_code:
        query = query.where(GmPolicyVersion.product_code == product_code)
    if industry_code:
        query = query.where(GmPolicyVersion.industry_code == industry_code)
    if pipeline_mode:
        query = query.where(GmPolicyVersion.pipeline_mode == pipeline_mode)
    rows = db.scalars(query.limit(limit)).all()
    return [_serialize_gm_policy(row) for row in rows]


@router.post("/gm-policies/{policy_id}/evaluate", response_model=GmPolicyItem)
def post_gm_policy_evaluate(
    policy_id: str,
    db: Session = Depends(get_db),
) -> GmPolicyItem:
    try:
        row = evaluate_gm_policy(db, policy_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_gm_policy(row)


@router.post("/gm-policies/{policy_id}/promote", response_model=GmPolicyItem)
def post_gm_policy_promote(
    policy_id: str,
    payload: GmPolicyPromoteRequest,
    db: Session = Depends(get_db),
) -> GmPolicyItem:
    try:
        row = promote_gm_policy(db, policy_id=policy_id, changed_by=payload.changed_by, notes=payload.notes)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409 if "replay gate" in str(exc) else 404, detail=str(exc)) from exc
    return _serialize_gm_policy(row)


@router.post("/runs", response_model=RunView)
def create_pipeline_run(payload: RunCreateRequest, db: Session = Depends(get_db)) -> RunView:
    has_image_inputs, has_video_inputs = _preflight_media_flags_for_payload(payload)
    _enforce_run_creation_preflight(
        db,
        payload=payload,
        has_image_inputs=has_image_inputs,
        has_video_inputs=has_video_inputs,
    )
    try:
        run = create_run(db, payload)
        db.commit()
        db.refresh(run)
    except ValueError as exc:
        db.rollback()
        detail = str(exc)
        status_code = 409 if "conflict" in detail else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return _serialize_run(db, run)


@router.post("/runs/rich")
async def create_pipeline_run_rich(
    workspace_name: str = Form(...),
    project_name: str = Form(...),
    product_name: str = Form(...),
    product_code: str = Form(...),
    industry_code: str = Form(...),
    campaign_name: str = Form(...),
    channel: str = Form("meta"),
    objective: str = Form("conversions"),
    market: str = Form("US"),
    locale: str = Form("en-US"),
    variant_count: int = Form(8),
    creative_preset: str = Form(...),
    creative_specs: str = Form("{}"),
    model_provider: str | None = Form(None),
    model_name: str | None = Form(None),
    pipeline_mode: str = Form(PipelineMode.FULL_MULTIMODAL.value),
    approval_mode: str = Form("manual"),
    enable_research: bool = Form(False),
    manual_research_brief: str = Form(""),
    memory_selection: str = Form("{}"),
    business_context: str = Form("{}"),
    category_tags: str = Form("[]"),
    url_references: str = Form("[]"),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    if pipeline_mode not in PIPELINE_STAGE_PLANS:
        raise HTTPException(status_code=400, detail=f"unsupported pipeline_mode: {pipeline_mode}")
    creative_specs_payload = _load_json_dict(creative_specs, "creative_specs")
    payload = RunCreateRequest(
        workspace_name=workspace_name,
        project_name=project_name,
        product_name=product_name,
        product_code=product_code,
        industry_code=industry_code,
        campaign_name=campaign_name,
        channel=channel,
        objective=objective,
        market=market,
        locale=locale,
        creative_preset=creative_preset,
        creative_specs=creative_specs_payload,
        variant_count=variant_count,
        model_provider=model_provider,
        model_name=model_name,
        pipeline_mode=pipeline_mode,
        approval_mode=approval_mode,
        enable_research=enable_research,
        manual_research_brief=manual_research_brief,
        memory_selection=_load_json_dict(memory_selection, "memory_selection"),
        business_context=_load_json_dict(business_context, "business_context"),
        category_tags=_load_json_list(category_tags, "category_tags"),
        context={"url_references": _load_json_list(url_references, "url_references")},
    )
    # -- inline preflight --
    has_image = any(
        (f.content_type or "").startswith("image/") for f in files
    )
    has_video = any(
        (f.content_type or "").startswith("video/") for f in files
    )
    preflight_result = _enforce_run_creation_preflight(
        db,
        payload=payload,
        has_image_inputs=has_image,
        has_video_inputs=has_video,
    )
    # -- end inline preflight --

    try:
        run = create_run(db, payload)
    except ValueError as exc:
        db.rollback()
        detail = str(exc)
        status_code = 409 if "conflict" in detail else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    uploaded_payloads = []
    for file in files:
        content = await file.read()
        uploaded_payloads.append({"filename": file.filename or "upload.bin", "content_type": file.content_type, "content": content})
    try:
        assets_summary, artifacts = process_uploaded_payloads(run.id, uploaded_payloads)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if is_marketplace_main_image(run.creative_specs) and not (
        assets_summary.get("sample_images") or assets_summary.get("sample_videos")
    ):
        db.rollback()
        raise HTTPException(status_code=400, detail="marketplace_main_image requires at least one uploaded product image or video")

    run.context_json = {
        **(run.context_json or {}),
        "input_assets": assets_summary,
        "url_references": payload.context.get("url_references", []),
    }
    for artifact in artifacts:
        db.add(
            Artifact(
                run_id=run.id,
                stage_name="intake",
                artifact_type=artifact["type"],
                uri=artifact["uri"],
                payload=artifact["payload"],
            )
        )
    db.commit()
    db.refresh(run)
    result = _serialize_run(db, run).model_dump()
    result["_preflight"] = preflight_result
    return result


@router.get("/runs/{run_id}", response_model=RunView)
def get_pipeline_run(run_id: str, db: Session = Depends(get_db)) -> RunView:
    try:
        run = get_run(db, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_run(db, run)


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    since_id: str | None = Query(default=None),
    once: bool = Query(default=False),
    poll_seconds: float = Query(default=1.0, ge=0.2, le=10.0),
) -> StreamingResponse:
    async def event_generator():
        seen: set[str] = set()
        if since_id:
            seen.add(since_id)
        with SessionLocal() as db:
            try:
                get_run(db, run_id)
            except ValueError:
                yield _sse_event("error", {"detail": f"run not found: {run_id}"})
                return
        while True:
            if await request.is_disconnected():
                break
            emitted = False
            with SessionLocal() as db:
                for event in run_trace_events(db, run_id, limit=200):
                    if event.id in seen:
                        continue
                    seen.add(event.id)
                    emitted = True
                    yield _sse_event("agent_trace", _serialize_trace_event(event), event.id)
            if once:
                break
            if not emitted:
                yield _sse_event("heartbeat", {"run_id": run_id})
            await asyncio.sleep(poll_seconds)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/deliverables", response_model=DeliverablesResponse)
def get_run_deliverables(run_id: str, db: Session = Depends(get_db)) -> DeliverablesResponse:
    try:
        run = get_run(db, run_id)
        deliverables = run_deliverables(db, run_id)
        evaluation = get_stage_payload(db, run_id, "evaluation_selection")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    score = (evaluation.get("evaluation_result", {}) if evaluation else {}) or {}
    return DeliverablesResponse(
        run_id=run.id,
        winner_variant_id=deliverables.get("winner_variant_id"),
        deliverables=deliverables,
        score=score,
    )


@router.get("/runs/{run_id}/deliverables.zip")
def get_run_deliverables_zip(run_id: str, db: Session = Depends(get_db)) -> StreamingResponse:
    try:
        run = get_run(db, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    assets = db.scalars(
        select(VariantAsset).where(VariantAsset.run_id == run_id, VariantAsset.asset_type == "image")
    ).all()
    intake_payload = get_stage_payload(db, run_id, "intake") if run.stage_tasks else {}
    exported: list[dict] = []
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for asset in assets:
            payload = asset.payload or {}
            marketplace_qa = payload.get("marketplace_qa") or {}
            readiness = payload.get("platform_readiness") or marketplace_qa.get("platform_readiness") or {}
            variant = db.get(RunVariant, asset.run_variant_id)
            approved = bool(
                variant
                and (
                    variant.is_winner
                    or variant.review_status in {"approved", "winner", "export_ready"}
                    or variant.status in {"approved", "winner"}
                )
            )
            export_ready = bool(payload.get("export_ready") or marketplace_qa.get("export_ready"))
            if not (export_ready or approved):
                continue
            if marketplace_qa.get("status") == "fail" and not approved:
                continue
            try:
                media_path = _resolve_media_path(asset.uri or "")
            except HTTPException:
                continue
            image_role = str(payload.get("image_role") or "main_image").replace("/", "_")
            variant_id = str(payload.get("variant_id") or (variant.variant_id if variant else asset.id)).replace("/", "_")
            suffix = media_path.suffix or ".png"
            export_name = f"{run.product_code}_{variant_id}_{image_role}{suffix}"
            zf.write(media_path, export_name)
            exported.append(
                {
                    "file": export_name,
                    "variant_id": variant_id,
                    "image_role": image_role,
                    "uri": asset.uri,
                    "platform_readiness": readiness,
                    "marketplace_qa": marketplace_qa,
                    "approved": approved,
                    "export_ready": export_ready,
                }
            )
        qa_report = {
            "run_id": run.id,
            "product_code": run.product_code,
            "industry_code": run.industry_code,
            "creative_preset": run.creative_preset,
            "creative_specs": run.creative_specs or {},
            "visual_identity": intake_payload.get("visual_identity") if isinstance(intake_payload, dict) else {},
            "exported_count": len(exported),
            "images": exported,
        }
        zf.writestr("qa_report.json", json.dumps(qa_report, ensure_ascii=False, indent=2))
    archive.seek(0)
    filename = f"{run.product_code or run.id}_deliverables.zip"
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/runs/{run_id}/variants", response_model=VariantsResponse)
def get_run_variants(
    run_id: str,
    status: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    quality: str | None = Query(default=None),
    asset_type: str | None = Query(default=None),
    generation_status: str | None = Query(default=None),
    compliance: str | None = Query(default=None),
    min_score: float | None = Query(default=None),
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> VariantsResponse:
    try:
        get_run(db, run_id)
        data = run_variants(
            db,
            run_id,
            status=status,
            review_status=review_status,
            quality=quality,
            asset_type=asset_type,
            generation_status=generation_status,
            compliance=compliance,
            min_score=min_score,
            q=q,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return VariantsResponse(
        run_id=run_id,
        variants=data.get("variants", []),
        ranked=data.get("ranked", []),
        items=[RunVariantView(**item) for item in data.get("items", [])],
        summary=data.get("summary", {}),
    )


@router.get("/runs/{run_id}/execution-memory", response_model=ExecutionMemoryLedgerResponse)
def get_run_execution_memory(run_id: str, db: Session = Depends(get_db)) -> ExecutionMemoryLedgerResponse:
    try:
        get_run(db, run_id)
        data = build_run_execution_ledger(db, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ExecutionMemoryLedgerResponse(**data)


@router.post("/runs/{run_id}/videos/refresh")
def post_run_video_refresh(run_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        result = refresh_video_task_assets(db, run_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result


@router.post("/runs/{run_id}/assets/refresh")
def post_run_assets_refresh(run_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        result = refresh_async_assets(db, run_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result


@router.post("/runs/{run_id}/variants/{variant_id}/review", response_model=RunVariantView)
def post_variant_review(
    run_id: str,
    variant_id: str,
    payload: VariantReviewRequest,
    db: Session = Depends(get_db),
) -> RunVariantView:
    try:
        variant = review_variant(
            db,
            run_id=run_id,
            variant_id=variant_id,
            action=payload.action,
            comment=payload.comment,
            tags=payload.tags,
        )
        compile_operator_review_reflection(
            db,
            run_id=run_id,
            variant_id=variant_id,
            action=payload.action,
            tags=payload.tags,
            comment=payload.comment,
        )
        db.commit()
        data = next(item for item in run_variants(db, run_id)["items"] if item["variant_id"] == variant.variant_id)
    except (ValueError, StopIteration) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunVariantView(**data)


@router.post("/runs/{run_id}/variants/{variant_id}/select", response_model=RunVariantView)
def post_variant_select(
    run_id: str,
    variant_id: str,
    payload: VariantSelectRequest,
    db: Session = Depends(get_db),
) -> RunVariantView:
    try:
        if payload.winner:
            review_variant(
                db,
                run_id=run_id,
                variant_id=variant_id,
                action="set_winner",
                comment=payload.comment,
            )
            compile_operator_review_reflection(
                db,
                run_id=run_id,
                variant_id=variant_id,
                action="set_winner",
                tags=[],
                comment=payload.comment,
            )
        elif payload.shortlist:
            review_variant(
                db,
                run_id=run_id,
                variant_id=variant_id,
                action="shortlist_variant",
                comment=payload.comment,
            )
            compile_operator_review_reflection(
                db,
                run_id=run_id,
                variant_id=variant_id,
                action="shortlist_variant",
                tags=[],
                comment=payload.comment,
            )
        else:
            raise ValueError("select requires shortlist or winner")
        db.commit()
        data = next(item for item in run_variants(db, run_id)["items"] if item["variant_id"] == variant_id)
    except (ValueError, StopIteration) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunVariantView(**data)


@router.post("/runs/{run_id}/variants/{variant_id}/regenerate", response_model=RunVariantView)
def post_variant_regenerate(
    run_id: str,
    variant_id: str,
    payload: VariantRegenerateRequest,
    db: Session = Depends(get_db),
) -> RunVariantView:
    try:
        variant = regenerate_variant_assets(
            db,
            run_id=run_id,
            variant_id=variant_id,
            reason=payload.reason,
            target_stage=payload.target_stage,
        )
        db.commit()
        data = next(item for item in run_variants(db, run_id)["items"] if item["variant_id"] == variant_id)
    except (ValueError, StopIteration) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunVariantView(**data)


@router.post("/runs/{run_id}/variants/{variant_id}/assets/image/retry", response_model=RunVariantView)
def post_variant_image_retry(
    run_id: str,
    variant_id: str,
    payload: VariantRegenerateRequest,
    db: Session = Depends(get_db),
) -> RunVariantView:
    try:
        variant = retry_copy_image_asset(
            db,
            run_id=run_id,
            variant_id=variant_id,
            reason=payload.reason or "retry image asset",
        )
        db.commit()
        data = next(item for item in run_variants(db, run_id)["items"] if item["variant_id"] == variant.variant_id)
    except (ValueError, RuntimeError, StopIteration) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunVariantView(**data)


@router.get("/artifacts", response_model=ArtifactListResponse)
def list_artifacts(
    q: str | None = Query(default=None),
    artifact_types: str | None = Query(default=None),
    pipeline_mode: str | None = Query(default=None),
    product_code: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ArtifactListResponse:
    types = [item.strip() for item in (artifact_types or "").split(",") if item.strip()]
    if not types:
        types = sorted(DEFAULT_GENERATED_ARTIFACT_TYPES)
    if sort_by not in {"created_at", "score"}:
        raise HTTPException(status_code=400, detail="sort_by must be created_at or score")
    if sort_order not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="sort_order must be asc or desc")
    try:
        start_dt = _parse_date_start(date_from)
        end_dt = _parse_date_end(date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid date: {exc}") from exc

    score_expr = (
        select(func.max(ScoreCardModel.total_score))
        .where(ScoreCardModel.run_id == Artifact.run_id)
        .scalar_subquery()
    )
    query = (
        select(
            Artifact.id,
            Artifact.run_id,
            Artifact.artifact_type,
            Artifact.stage_name,
            Artifact.uri,
            Artifact.payload,
            Artifact.created_at,
            PipelineRun.pipeline_mode,
            PipelineRun.product_code,
            score_expr.label("score"),
        )
        .join(PipelineRun, PipelineRun.id == Artifact.run_id)
        .where(Artifact.artifact_type.in_(types))
    )

    if q:
        pattern = f"%{q.lower()}%"
        query = query.where(
            or_(
                func.lower(Artifact.run_id).like(pattern),
                func.lower(Artifact.uri).like(pattern),
                func.lower(cast(Artifact.payload, String)).like(pattern),
            )
        )
    if pipeline_mode:
        query = query.where(PipelineRun.pipeline_mode == pipeline_mode)
    if product_code:
        query = query.where(PipelineRun.product_code == product_code)
    if start_dt:
        query = query.where(Artifact.created_at >= start_dt)
    if end_dt:
        query = query.where(Artifact.created_at < end_dt)

    count_stmt = select(func.count()).select_from(query.subquery())
    total = int(db.scalar(count_stmt) or 0)

    if sort_by == "score":
        if sort_order == "asc":
            query = query.order_by(func.coalesce(score_expr, 101.0).asc(), desc(Artifact.created_at))
        else:
            query = query.order_by(desc(func.coalesce(score_expr, -1.0)), desc(Artifact.created_at))
    elif sort_order == "asc":
        query = query.order_by(Artifact.created_at.asc())
    else:
        query = query.order_by(desc(Artifact.created_at))

    rows = db.execute(query.offset((page - 1) * page_size).limit(page_size)).all()
    items = [
        ArtifactListItem(
            artifact_id=row.id,
            run_id=row.run_id,
            artifact_type=row.artifact_type,
            stage_name=row.stage_name,
            pipeline_mode=row.pipeline_mode,
            product_code=row.product_code or "",
            uri=row.uri,
            preview_text=_artifact_preview(row.payload or {}),
            score=float(row.score) if row.score is not None else None,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return ArtifactListResponse(page=page, page_size=page_size, total=total, items=items)


@router.post("/artifacts/images.zip")
def download_artifact_images_zip(payload: BulkIdsRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    ids = [item for item in payload.ids if item]
    if not ids:
        raise HTTPException(status_code=400, detail="no artifact ids selected")
    rows = db.scalars(
        select(Artifact)
        .where(Artifact.id.in_(ids))
        .order_by(desc(Artifact.created_at))
    ).all()
    archive = io.BytesIO()
    exported: list[dict] = []
    skipped: list[dict] = []
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            media_type = mimetypes.guess_type(row.uri)[0] or ""
            if "image" not in row.artifact_type and not media_type.startswith("image/"):
                skipped.append({"artifact_id": row.id, "uri": row.uri, "reason": "not_image"})
                continue
            try:
                media_path = _resolve_media_path(row.uri)
            except HTTPException:
                skipped.append({"artifact_id": row.id, "uri": row.uri, "reason": "missing_file"})
                continue
            suffix = media_path.suffix or ".png"
            export_name = f"{row.artifact_type}_{row.id}{suffix}".replace("/", "_")
            zf.write(media_path, export_name)
            exported.append({"artifact_id": row.id, "file": export_name, "uri": row.uri})
        zf.writestr(
            "manifest.json",
            json.dumps(
                {"selected_count": len(ids), "exported_count": len(exported), "skipped_count": len(skipped), "exported": exported, "skipped": skipped},
                ensure_ascii=False,
                indent=2,
            ),
        )
    archive.seek(0)
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="selected_asset_images.zip"'},
    )


@router.post("/artifacts/bulk-delete")
def bulk_delete_artifacts(payload: BulkIdsRequest, db: Session = Depends(get_db)) -> dict:
    ids = [item for item in payload.ids if item]
    if not ids:
        raise HTTPException(status_code=400, detail="no artifact ids selected")
    rows = db.scalars(select(Artifact).where(Artifact.id.in_(ids))).all()
    deleted_ids = [row.id for row in rows]
    for row in rows:
        db.delete(row)
    db.commit()
    return {"requested": len(ids), "deleted": len(deleted_ids), "deleted_ids": deleted_ids, "skipped": len(ids) - len(deleted_ids)}


@router.get("/asset-products", response_model=AssetProductListResponse)
def list_asset_products(
    q: str | None = Query(default=None),
    workspace_name: str | None = Query(default=None),
    project_name: str | None = Query(default=None),
    product_code: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
    db: Session = Depends(get_db),
) -> AssetProductListResponse:
    query = (
        select(Product, Project.name.label("project_name"), Workspace.name.label("workspace_name"))
        .join(Project, Project.id == Product.project_id)
        .join(Workspace, Workspace.id == Project.workspace_id)
    )
    if q:
        pattern = f"%{q.lower()}%"
        query = query.where(
            or_(
                func.lower(Product.name).like(pattern),
                func.lower(Product.product_code).like(pattern),
                func.lower(Project.name).like(pattern),
                func.lower(Workspace.name).like(pattern),
            )
        )
    if workspace_name:
        query = query.where(Workspace.name == workspace_name)
    if project_name:
        query = query.where(Project.name == project_name)
    if product_code:
        query = query.where(Product.product_code == product_code)

    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = db.execute(
        query.order_by(Workspace.name, Project.name, Product.product_code)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items: list[AssetProductItem] = []
    for product, project_label, workspace_label in rows:
        run_count = db.scalar(
            select(func.count(PipelineRun.id)).where(PipelineRun.product_code == product.product_code)
        ) or 0
        asset_count = db.scalar(
            select(func.count(Artifact.id))
            .join(PipelineRun, PipelineRun.id == Artifact.run_id)
            .where(
                PipelineRun.product_code == product.product_code,
                Artifact.artifact_type.in_(DEFAULT_GENERATED_ARTIFACT_TYPES),
            )
        ) or 0
        memory_count = db.scalar(
            select(func.count(GmMemory.id)).where(
                GmMemory.memory_scope == "product",
                GmMemory.product_code == product.product_code,
                GmMemory.status == "active",
            )
        ) or 0
        latest_run_at = db.scalar(
            select(func.max(PipelineRun.created_at)).where(PipelineRun.product_code == product.product_code)
        )
        thumbnail_uri = db.scalar(
            select(Artifact.uri)
            .join(PipelineRun, PipelineRun.id == Artifact.run_id)
            .where(
                PipelineRun.product_code == product.product_code,
                Artifact.artifact_type.in_(["generated_image", "copy_image_bundle", "storyboard_frame"]),
                Artifact.uri.isnot(None),
            )
            .order_by(desc(Artifact.created_at))
            .limit(1)
        )
        items.append(
            AssetProductItem(
                product_id=product.id,
                product_code=product.product_code,
                name=product.name,
                workspace_name=workspace_label,
                project_name=project_label,
                thumbnail_uri=thumbnail_uri,
                run_count=run_count,
                asset_count=asset_count,
                memory_count=memory_count,
                latest_run_at=latest_run_at,
                created_at=product.created_at,
            )
        )
    return AssetProductListResponse(page=page, page_size=page_size, total=total, items=items)


@router.post("/asset-products/bulk-delete")
def bulk_delete_asset_products(payload: BulkIdsRequest, db: Session = Depends(get_db)) -> dict:
    ids = [item for item in payload.ids if item]
    if not ids:
        raise HTTPException(status_code=400, detail="no product ids selected")
    rows = db.scalars(select(Product).where(Product.id.in_(ids))).all()
    deleted_ids: list[str] = []
    skipped: list[dict] = []
    for product in rows:
        run_count = db.scalar(select(func.count(PipelineRun.id)).where(PipelineRun.product_id == product.id)) or 0
        campaign_count = db.scalar(select(func.count(Campaign.id)).where(Campaign.product_id == product.id)) or 0
        if run_count or campaign_count:
            skipped.append({
                "product_id": product.id,
                "product_code": product.product_code,
                "reason": "has_history",
                "run_count": run_count,
                "campaign_count": campaign_count,
            })
            continue
        deleted_ids.append(product.id)
        db.delete(product)
    db.commit()
    return {"requested": len(ids), "deleted": len(deleted_ids), "deleted_ids": deleted_ids, "skipped": skipped}


def get_stage_payload(db: Session, run_id: str, stage_name: str) -> dict:
    task = db.scalar(select(StageTask).where(StageTask.run_id == run_id, StageTask.stage_name == stage_name))
    if not task:
        raise ValueError(f"stage task not found: {run_id}/{stage_name}")
    return task.output_payload or {}


@router.post("/runs/{run_id}/advance", response_model=RunView)
def advance_pipeline_run(run_id: str, payload: ReviewActionRequest, db: Session = Depends(get_db)) -> RunView:
    try:
        run = approve_stage(db, run_id, notes=payload.notes)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize_run(db, run)


@router.post("/runs/{run_id}/reject", response_model=RunView)
def reject_pipeline_run(run_id: str, payload: ReviewActionRequest, db: Session = Depends(get_db)) -> RunView:
    try:
        run = reject_stage(db, run_id, notes=payload.notes)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize_run(db, run)


@router.post("/runs/{run_id}/stages/{stage_name}/rerun", response_model=RunView)
def rerun_pipeline_stage(run_id: str, stage_name: str, payload: ReviewActionRequest, db: Session = Depends(get_db)) -> RunView:
    try:
        run = rerun_stage(db, run_id, stage_name, notes=payload.notes)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize_run(db, run)


@router.post("/feedback/import", response_model=FeedbackImportResponse)
def import_feedback(payload: FeedbackImportRequest, db: Session = Depends(get_db)) -> FeedbackImportResponse:
    import_record, snapshots, memory = import_feedback_rows(
        db=db,
        workspace_name=payload.workspace_name,
        project_name=payload.project_name,
        rows=payload.rows,
        file_name=payload.file_name,
    )
    compile_feedback_import_reflections(db, import_record=import_record, rows=payload.rows)
    db.commit()
    return FeedbackImportResponse(
        import_id=import_record.id,
        rows=import_record.row_count,
        snapshots_created=snapshots,
        memory_entry_id=memory.id if memory else None,
    )


@router.get("/projects/{project_id}/leaderboard", response_model=LeaderboardResponse)
def leaderboard(project_id: str, db: Session = Depends(get_db)) -> LeaderboardResponse:
    ranking = [LeaderboardItem(**item) for item in project_leaderboard(db, project_id)]
    return LeaderboardResponse(project_id=project_id, ranking=ranking)


# ── Integration Sync Endpoints ──────────────────────────────────────────────

from pydantic import BaseModel as _PydanticBaseModel


class IntegrationSyncResult(_PydanticBaseModel):
    platform: str
    sync_type: str
    status: str
    items_synced: int
    memory_entries_created: int
    channel_account_id: str | None = None
    error: str | None = None


class SyncStatusItem(_PydanticBaseModel):
    id: str
    channel_account_id: str | None = None
    platform: str
    sync_type: str
    status: str
    items_synced: int
    error_log: dict | None = None
    created_at: str


class SyncStatusResponse(_PydanticBaseModel):
    items: list[SyncStatusItem]


@router.post("/integrations/{platform}/sync", response_model=IntegrationSyncResult)
async def trigger_integration_sync(
    platform: str,
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    sync_type: str = Query("all"),
    channel_account_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> IntegrationSyncResult:
    from app.integrations.sync_service import supported_integration_platforms, sync_integration

    platform = platform.lower().strip()
    if platform not in supported_integration_platforms():
        supported = ", ".join(supported_integration_platforms())
        raise HTTPException(status_code=400, detail=f"Platform must be one of: {supported}")

    result = await sync_integration(
        platform,
        db,
        workspace_name=workspace_name,
        project_name=project_name,
        sync_type=sync_type,
        channel_account_id=channel_account_id,
    )
    db.commit()
    return IntegrationSyncResult(**result.model_dump())


@router.get("/integrations/sync-status", response_model=SyncStatusResponse)
def get_sync_status(
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    platform: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> SyncStatusResponse:
    from app.data.models import Workspace, Project

    workspace = db.scalar(select(Workspace).where(Workspace.name == workspace_name))
    if not workspace:
        return SyncStatusResponse(items=[])
    project = db.scalar(
        select(Project).where(Project.workspace_id == workspace.id, Project.name == project_name)
    )
    if not project:
        return SyncStatusResponse(items=[])

    conditions = [IntegrationSync.project_id == project.id]
    if platform:
        conditions.append(IntegrationSync.platform == platform)
    rows = db.scalars(
        select(IntegrationSync)
        .where(*conditions)
        .order_by(desc(IntegrationSync.created_at))
        .limit(limit)
    ).all()
    return SyncStatusResponse(items=[
        SyncStatusItem(
            id=row.id,
            channel_account_id=row.channel_account_id,
            platform=row.platform,
            sync_type=row.sync_type,
            status=row.status,
            items_synced=row.items_synced,
            error_log=row.error_log,
            created_at=row.created_at.isoformat() if row.created_at else "",
        )
        for row in rows
    ])


@router.get("/integrations/health")
def get_integration_health(
    workspace_name: str | None = Query(None),
    project_name: str | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    from app.services.integration_health import integration_health

    result = integration_health(db, workspace_name=workspace_name, project_name=project_name)
    db.commit()
    return result


@router.get("/integrations/shopify/products")
def list_shopify_products(
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    db: Session = Depends(get_db),
) -> list[dict]:
    from app.data.models import Product as ProductModel, Project, Workspace

    workspace = db.scalar(select(Workspace).where(Workspace.name == workspace_name))
    if not workspace:
        return []
    project = db.scalar(
        select(Project).where(Project.workspace_id == workspace.id, Project.name == project_name)
    )
    if not project:
        return []
    products = db.scalars(
        select(ProductModel).where(ProductModel.project_id == project.id)
    ).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "product_code": p.product_code,
            "shopify_product_id": (p.metadata_json or {}).get("shopify_product_id"),
            "shopify_handle": (p.metadata_json or {}).get("shopify_handle"),
            "shopify_vendor": (p.metadata_json or {}).get("shopify_vendor"),
            "shopify_product_type": (p.metadata_json or {}).get("shopify_product_type"),
        }
        for p in products
        if (p.metadata_json or {}).get("shopify_product_id")
    ]


@router.get("/integrations/meta/campaigns")
def list_meta_campaigns(
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    db: Session = Depends(get_db),
) -> list[dict]:
    from app.data.models import Campaign as CampaignModel, Project, Workspace

    workspace = db.scalar(select(Workspace).where(Workspace.name == workspace_name))
    if not workspace:
        return []
    project = db.scalar(
        select(Project).where(Project.workspace_id == workspace.id, Project.name == project_name)
    )
    if not project:
        return []
    campaigns = db.scalars(
        select(CampaignModel).where(
            CampaignModel.project_id == project.id,
            CampaignModel.platform_campaign_id.isnot(None),
        )
    ).all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "channel": c.channel,
            "objective": c.objective,
            "platform_campaign_id": c.platform_campaign_id,
            "platform_ad_account_id": c.platform_ad_account_id,
        }
        for c in campaigns
    ]


# ── Persona Endpoints ──────────────────────────────────────────────────────

@router.get("/personas", response_model=list[PersonaMeta])
def list_agent_personas() -> list[PersonaMeta]:
    return [PersonaMeta(**row) for row in list_persona_catalog()]


@router.get("/personas/{agent_name}", response_model=PersonaView)
def read_agent_persona(agent_name: str, db: Session = Depends(get_db)) -> PersonaView:
    try:
        content, version, source_path = get_persona(db, agent_name)
    except (FileNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    info = persona_info(agent_name)
    return PersonaView(
        agent_name=agent_name,
        display_name=info["display_name"],
        stage=info["stage"],
        role=info["role"],
        content=content,
        version=version,
        source_path=source_path,
    )


@router.patch("/personas/{agent_name}", response_model=PersonaView)
def patch_agent_persona(agent_name: str, payload: PersonaPatchRequest, db: Session = Depends(get_db)) -> PersonaView:
    try:
        content, version, source_path = update_persona(db, agent_name, payload.content, payload.changed_by)
        info = persona_info(agent_name)
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return PersonaView(
        agent_name=agent_name,
        display_name=info["display_name"],
        stage=info["stage"],
        role=info["role"],
        content=content,
        version=version,
        source_path=source_path,
    )


@router.get("/agent-configs", response_model=list[AgentApiConfigView])
def get_agent_configs(db: Session = Depends(get_db)) -> list[AgentApiConfigView]:
    rows = list_agent_configs(db)
    db.commit()
    return [_serialize_agent_config(row) for row in rows]


@router.get("/agent-configs/env-vars", response_model=list[str])
def get_agent_config_env_vars() -> list[str]:
    return list_api_key_env_names()


@router.patch("/agent-configs/{agent_name}", response_model=AgentApiConfigView)
def patch_agent_config(agent_name: str, payload: AgentApiConfigPatchRequest, db: Session = Depends(get_db)) -> AgentApiConfigView:
    try:
        row = upsert_agent_config(
            db,
            agent_name=agent_name,
            provider_name=payload.provider_name,
            model_name=payload.model_name,
            api_base_url=payload.api_base_url,
            api_key_env=payload.api_key_env,
            image_provider_name=payload.image_provider_name,
            image_model_name=payload.image_model_name,
            image_api_base_url=payload.image_api_base_url,
            image_api_key_env=payload.image_api_key_env,
            video_provider_name=payload.video_provider_name,
            video_model_name=payload.video_model_name,
            video_api_base_url=payload.video_api_base_url,
            video_api_key_env=payload.video_api_key_env,
            thinking_mode=payload.thinking_mode,
            thinking_budget_tokens=payload.thinking_budget_tokens,
            max_output_tokens=payload.max_output_tokens,
            request_timeout_seconds=payload.request_timeout_seconds,
            streaming_enabled=payload.streaming_enabled,
            extra=payload.extra,
            update_fields=set(payload.model_fields_set),
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _serialize_agent_config(row)


# ── Content Calendar ────────────────────────────────────────────────────────


@router.get("/content-schedules", response_model=ContentScheduleListResponse)
def list_content_schedules(
    workspace_id: str = Query(...),
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
) -> ContentScheduleListResponse:
    from datetime import date as date_type

    rows = db.scalars(
        select(ContentSchedule)
        .where(
            ContentSchedule.workspace_id == workspace_id,
            ContentSchedule.scheduled_date >= date_type.fromisoformat(start_date),
            ContentSchedule.scheduled_date <= date_type.fromisoformat(end_date),
        )
        .order_by(ContentSchedule.scheduled_date.asc(), ContentSchedule.scheduled_time.asc())
    ).all()
    return ContentScheduleListResponse(items=[_serialize_schedule(r) for r in rows])


@router.post("/content-schedules", response_model=ContentScheduleView)
async def create_content_schedule(
    payload: ContentScheduleCreateRequest,
    db: Session = Depends(get_db),
) -> ContentScheduleView:
    from datetime import date as date_type

    from app.integrations.calendar_service import schedule_variant

    scheduled_date = date_type.fromisoformat(payload.scheduled_date)
    variant_url = ""
    if payload.variant_id:
        variant = db.get(RunVariant, payload.variant_id)
        if variant:
            variant_url = f"/dashboard?run={variant.run_id}&variant={variant.id}"

    schedule = await schedule_variant(
        db,
        workspace_id=payload.workspace_id,
        project_id=payload.project_id,
        variant_id=payload.variant_id,
        campaign_id=payload.campaign_id,
        channel_account_id=payload.channel_account_id,
        title=payload.title,
        channel=payload.channel,
        scheduled_date=scheduled_date,
        scheduled_time=payload.scheduled_time,
        publish_payload=payload.publish_payload,
        notes=payload.notes,
        variant_url=variant_url,
    )
    db.commit()
    return _serialize_schedule(schedule)


@router.put("/content-schedules/{schedule_id}", response_model=ContentScheduleView)
async def update_content_schedule(
    schedule_id: str,
    payload: ContentScheduleUpdateRequest,
    db: Session = Depends(get_db),
) -> ContentScheduleView:
    from datetime import date as date_type

    from app.integrations.calendar_service import push_to_notion

    schedule = db.get(ContentSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    if payload.title is not None:
        schedule.title = payload.title
    if payload.channel is not None:
        schedule.channel = payload.channel
    if payload.channel_account_id is not None:
        schedule.channel_account_id = payload.channel_account_id or None
    if payload.scheduled_date is not None:
        schedule.scheduled_date = date_type.fromisoformat(payload.scheduled_date)
    if payload.scheduled_time is not None:
        schedule.scheduled_time = payload.scheduled_time or None
    if payload.state is not None:
        schedule.state = payload.state
    if payload.publish_payload is not None:
        schedule.publish_payload = payload.publish_payload
    if payload.notes is not None:
        schedule.notes = payload.notes or None
    if payload.variant_id is not None:
        schedule.variant_id = payload.variant_id or None
    if payload.campaign_id is not None:
        schedule.campaign_id = payload.campaign_id or None

    db.flush()

    variant_url = ""
    if schedule.variant_id:
        variant = db.get(RunVariant, schedule.variant_id)
        if variant:
            variant_url = f"/dashboard?run={variant.run_id}&variant={variant.id}"

    notion_id, notion_error = await push_to_notion(db, schedule, variant_url)
    if notion_id:
        schedule.notion_page_id = notion_id
        schedule.notion_sync_error = None
    elif notion_error:
        schedule.notion_sync_error = notion_error
    db.flush()

    db.commit()
    return _serialize_schedule(schedule)


@router.post("/content-schedules/{schedule_id}/publish", response_model=ContentScheduleView)
async def publish_content_schedule(
    schedule_id: str,
    payload: ContentSchedulePublishRequest | None = None,
    db: Session = Depends(get_db),
) -> ContentScheduleView:
    from app.integrations.social_publishing import publish_schedule

    schedule = db.get(ContentSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if schedule.state == "published" and schedule.platform_post_id:
        return _serialize_schedule(schedule)
    request = payload or ContentSchedulePublishRequest()
    try:
        await publish_schedule(
            db,
            schedule,
            channel_account_id=request.channel_account_id,
            publish_payload=request.publish_payload,
        )
    except Exception as exc:
        schedule.state = "failed"
        schedule.publish_error = str(exc)
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(schedule)
    return _serialize_schedule(schedule)


@router.delete("/content-schedules/{schedule_id}")
async def delete_content_schedule(
    schedule_id: str,
    db: Session = Depends(get_db),
) -> dict:
    from app.integrations.calendar_service import delete_from_notion

    schedule = db.get(ContentSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    if schedule.notion_page_id:
        await delete_from_notion(db, schedule.notion_page_id)

    db.delete(schedule)
    db.commit()
    return {"ok": True}


@router.get("/content-schedules/notion-status", response_model=NotionConnectionTestResponse)
async def check_notion_connection(db: Session = Depends(get_db)) -> NotionConnectionTestResponse:
    from app.integrations.calendar_service import test_notion_connection

    result = await test_notion_connection(db)
    return NotionConnectionTestResponse(**result)


@router.get("/variants/ready-to-schedule", response_model=list[VariantScheduleCandidate])
def list_variants_ready_to_schedule(
    workspace_id: str = Query(...),
    project_id: str = Query(...),
    db: Session = Depends(get_db),
) -> list[VariantScheduleCandidate]:
    rows = db.scalars(
        select(RunVariant)
        .join(PipelineRun, RunVariant.run_id == PipelineRun.id)
        .where(
            PipelineRun.workspace_id == workspace_id,
            PipelineRun.project_id == project_id,
            RunVariant.status.in_(["approved", "winner"]),
        )
        .order_by(RunVariant.updated_at.desc())
        .limit(50)
    ).all()

    candidates: list[VariantScheduleCandidate] = []
    seen = set()
    for v in rows:
        if v.id in seen:
            continue
        seen.add(v.id)
        run = db.get(PipelineRun, v.run_id)
        campaign_name = ""
        channel = "meta"
        if run:
            campaign = db.get(Campaign, run.campaign_id) if run.campaign_id else None
            if campaign:
                campaign_name = campaign.name
                channel = campaign.channel
        candidates.append(VariantScheduleCandidate(
            variant_id=v.id,
            run_id=v.run_id,
            hook=v.hook or "",
            message=v.message or "",
            status=v.status,
            is_winner=v.is_winner,
            product_code=run.product_code if run else "",
            campaign_name=campaign_name,
            channel=channel,
        ))
    return candidates


def _serialize_schedule(s: ContentSchedule) -> ContentScheduleView:
    return ContentScheduleView(
        id=s.id,
        workspace_id=s.workspace_id,
        project_id=s.project_id,
        variant_id=s.variant_id,
        campaign_id=s.campaign_id,
        channel_account_id=s.channel_account_id,
        title=s.title,
        channel=s.channel,
        scheduled_date=s.scheduled_date.isoformat(),
        scheduled_time=s.scheduled_time,
        state=s.state,
        publish_payload=s.publish_payload or {},
        platform_post_id=s.platform_post_id,
        platform_post_url=s.platform_post_url,
        publish_error=s.publish_error,
        published_at=s.published_at,
        notion_page_id=s.notion_page_id,
        notion_sync_error=s.notion_sync_error,
        notes=s.notes,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


@router.get("/integration-configs", response_model=list[IntegrationConfigView])
def get_integration_configs(db: Session = Depends(get_db)) -> list[IntegrationConfigView]:
    from app.services.agent_api_configs import list_integration_configs

    rows = list_integration_configs(db)
    db.commit()
    return [IntegrationConfigView(**row) for row in rows]


CHANNEL_CREDENTIAL_CONFIG_KEYS = {
    "shopify": {"store_domain": "store_domain", "access_token": "access_token"},
    "meta": {"access_token": "access_token", "ad_account_id": "ad_account_id"},
    "tiktok": {"access_token": "access_token", "advertiser_id": "advertiser_id"},
    "notion": {"api_key": "api_key", "database_id": "database_id"},
}


@router.get("/integration-configs/usage")
def get_integration_config_usage(db: Session = Depends(get_db)) -> dict:
    accounts = db.scalars(
        select(ShopChannelAccount).where(ShopChannelAccount.status != "archived")
    ).all()
    usage: dict[tuple[str, str], dict] = {}
    for account in accounts:
        key_map = CHANNEL_CREDENTIAL_CONFIG_KEYS.get(account.platform, {})
        credential_envs = account.credential_env_vars if isinstance(account.credential_env_vars, dict) else {}
        for account_key, config_key in key_map.items():
            if not credential_envs.get(account_key):
                continue
            usage_key = (account.platform, config_key)
            item = usage.setdefault(
                usage_key,
                {
                    "platform": account.platform,
                    "config_key": config_key,
                    "account_count": 0,
                    "shop_ids": set(),
                    "examples": [],
                },
            )
            item["account_count"] += 1
            item["shop_ids"].add(account.workspace_id)
            if len(item["examples"]) < 3:
                item["examples"].append(
                    {
                        "shop_id": account.workspace_id,
                        "account_id": account.id,
                        "label": account.label or account.account_key,
                        "account_key": account.account_key,
                    }
                )
    items = []
    for item in usage.values():
        items.append(
            {
                "platform": item["platform"],
                "config_key": item["config_key"],
                "account_count": item["account_count"],
                "shop_count": len(item["shop_ids"]),
                "examples": item["examples"],
            }
        )
    return {"items": sorted(items, key=lambda row: (row["platform"], row["config_key"]))}


@router.patch("/integration-configs/{platform}/{config_key}", response_model=IntegrationConfigView)
def patch_integration_config(
    platform: str,
    config_key: str,
    payload: IntegrationConfigPatchRequest,
    db: Session = Depends(get_db),
) -> IntegrationConfigView:
    from app.services.agent_api_configs import upsert_integration_config

    env_var = (payload.env_var or "").strip()
    row = upsert_integration_config(db, platform=platform, config_key=config_key, env_var=env_var)
    db.commit()
    return IntegrationConfigView(**row)


@router.get("/projects")
def list_projects(
    workspace_name: str = Query(...),
    db: Session = Depends(get_db),
) -> list[dict]:
    workspace = db.scalar(select(Workspace).where(Workspace.name == workspace_name))
    if not workspace:
        return []
    rows = db.scalars(
        select(Project).where(Project.workspace_id == workspace.id).order_by(Project.name)
    ).all()
    return [{"id": r.id, "name": r.name, "metric_weights": r.metric_weights} for r in rows]


@router.get("/gm-review/summary")
def gm_review_summary(
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    days: int = Query(default=7, ge=1, le=365),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    pipeline_mode: str | None = Query(default=None),
    product_code: str | None = Query(default=None),
    include_narrative: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return build_gm_review_summary(
            db,
            workspace_name=workspace_name,
            project_name=project_name,
            days=days,
            date_from=date_from,
            date_to=date_to,
            pipeline_mode=pipeline_mode,
            product_code=product_code,
            include_narrative=include_narrative,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/gm-review/report.md")
def gm_review_markdown_report(
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    days: int = Query(default=7, ge=1, le=365),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    pipeline_mode: str | None = Query(default=None),
    product_code: str | None = Query(default=None),
    include_narrative: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        summary = build_gm_review_summary(
            db,
            workspace_name=workspace_name,
            project_name=project_name,
            days=days,
            date_from=date_from,
            date_to=date_to,
            pipeline_mode=pipeline_mode,
            product_code=product_code,
            include_narrative=include_narrative,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    markdown = render_gm_review_markdown(summary)
    filename = f"gm-review-{workspace_name}-{project_name}.md".replace("/", "-")
    return StreamingResponse(
        io.BytesIO(markdown.encode("utf-8")),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Data Dashboard Endpoints ────────────────────────────────────────────────


@router.get("/data-dashboard/summary")
def data_dashboard_summary(
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    workspace = db.scalar(select(Workspace).where(Workspace.name == workspace_name))
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    project = db.scalar(
        select(Project).where(Project.workspace_id == workspace.id, Project.name == project_name)
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    store_memories = db.scalars(
        select(GmMemory)
        .where(
            GmMemory.project_id == project.id,
            GmMemory.memory_scope == "shop",
            GmMemory.source_type.in_(["shopify_sync", "meta_sync", "tiktok_sync", "offline_csv_import"]),
        )
        .order_by(desc(GmMemory.created_at))
        .limit(10)
    ).all()

    latest_shopify = next((m for m in store_memories if m.source_type == "shopify_sync"), None)
    latest_offline = next((m for m in store_memories if m.source_type == "offline_csv_import"), None)
    latest_store = latest_shopify or latest_offline
    latest_meta = next((m for m in store_memories if m.source_type == "meta_sync"), None)
    latest_tiktok = next((m for m in store_memories if m.source_type == "tiktok_sync"), None)

    product_count = db.scalar(
        select(func.count()).select_from(Product).where(Product.project_id == project.id)
    )

    recent_snapshots = db.scalars(
        select(PerformanceSnapshot)
        .where(PerformanceSnapshot.project_id == project.id)
        .order_by(desc(PerformanceSnapshot.created_at))
        .limit(100)
    ).all()

    total_spend = sum(float((s.metrics or {}).get("spend", 0)) for s in recent_snapshots)
    total_revenue = sum(float((s.metrics or {}).get("revenue", 0)) for s in recent_snapshots)
    total_impressions = sum(int((s.metrics or {}).get("impressions", 0)) for s in recent_snapshots)
    total_clicks = sum(int((s.metrics or {}).get("clicks", 0)) for s in recent_snapshots)

    from app.services.research_context import build_research_context
    from app.services.integration_health import credential_ready_map

    credentials = credential_ready_map(db)

    return {
        "workspace_name": workspace_name,
        "project_name": project_name,
        "credentials": credentials,
        "product_count": product_count,
        "shopify_revenue": (latest_store.content or {}).get("total_revenue", 0) if latest_store else 0,
        "shopify_quantity": (latest_store.content or {}).get("total_quantity", 0) if latest_store else 0,
        "store_data_source": latest_store.source_type if latest_store else None,
        "meta_spend": round(total_spend, 2),
        "meta_revenue": round(total_revenue, 2),
        "tiktok_spend": (latest_tiktok.content or {}).get("total_spend", 0) if latest_tiktok else 0,
        "tiktok_revenue": (latest_tiktok.content or {}).get("total_revenue", 0) if latest_tiktok else 0,
        "overall_roas": round(total_revenue / total_spend, 4) if total_spend > 0 else 0,
        "overall_ctr": round(total_clicks / total_impressions * 100, 4) if total_impressions > 0 else 0,
        "total_impressions": total_impressions,
        "total_clicks": total_clicks,
        "auto_sync": {
            "shopify_interval_minutes": workspace.shopify_auto_sync_minutes,
            "meta_interval_minutes": workspace.meta_auto_sync_minutes,
            "shopify_last_sync_at": workspace.shopify_last_sync_at.isoformat() if workspace.shopify_last_sync_at else None,
            "meta_last_sync_at": workspace.meta_last_sync_at.isoformat() if workspace.meta_last_sync_at else None,
        },
        "research_context": build_research_context(
            db,
            project_id=project.id,
            shop_id=workspace.id,
            industry_code=workspace.industry_code,
        ),
        "daily_trend": _daily_revenue_trend(recent_snapshots),
    }


@router.post("/data-dashboard/offline-csv-import/{platform}")
async def data_dashboard_offline_csv_import(
    platform: str,
    workspace_name: str = Form(...),
    project_name: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    from app.services.offline_store_import import import_offline_store_csv

    filename = file.filename or f"{platform}_fallback.csv"
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are supported for offline CSV fallback import")
    content = await file.read()
    try:
        result = import_offline_store_csv(
            db,
            workspace_name=workspace_name,
            project_name=project_name,
            platform=platform,
            file_name=filename,
            content=content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/data-dashboard/offline-csv-imports")
def data_dashboard_offline_csv_imports(
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    from app.services.offline_store_import import list_offline_csv_batches

    return {
        "items": list_offline_csv_batches(
            db,
            workspace_name=workspace_name,
            project_name=project_name,
        )
    }


@router.delete("/data-dashboard/offline-csv-imports/{batch_id}")
def data_dashboard_delete_offline_csv_import(
    batch_id: str,
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    from app.services.offline_store_import import delete_offline_csv_batch

    result = delete_offline_csv_batch(
        db,
        workspace_name=workspace_name,
        project_name=project_name,
        batch_id=batch_id,
    )
    db.commit()
    return result


@router.get("/data-dashboard/product-analytics")
def data_dashboard_product_analytics(
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    product_code: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    from app.analytics import ProductAnalyzer, AdAnalyzer
    from app.data.models import Project, Workspace

    workspace = db.scalar(select(Workspace).where(Workspace.name == workspace_name))
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    project = db.scalar(
        select(Project).where(Project.workspace_id == workspace.id, Project.name == project_name)
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    pa = ProductAnalyzer(db, project.id)
    aa = AdAnalyzer(db, project.id)

    velocity = pa.analyze_product_sales_velocity(product_code)
    contribution = pa.analyze_product_contribution([product_code])

    snapshots = db.scalars(
        select(PerformanceSnapshot)
        .where(PerformanceSnapshot.project_id == project.id)
        .order_by(desc(PerformanceSnapshot.created_at))
        .limit(50)
    ).all()
    creative_keys = list({s.creative_key for s in snapshots if s.creative_key})
    fatigue_results = []
    for ck in creative_keys[:5]:
        f = aa.analyze_creative_fatigue(ck)
        if not f.insufficient_data:
            fatigue_results.append(f.model_dump())

    return {
        "product_code": product_code,
        "sales_velocity": velocity.model_dump(),
        "contribution": contribution.model_dump(),
        "creative_fatigue": fatigue_results,
    }


@router.get("/data-dashboard/creative-decisions")
def data_dashboard_creative_decisions(
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    product_code: str | None = Query(None),
    window_days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> dict:
    from app.analytics.creative_decisions import CreativeDecisionAnalyzer

    workspace = db.scalar(select(Workspace).where(Workspace.name == workspace_name))
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    project = db.scalar(
        select(Project).where(Project.workspace_id == workspace.id, Project.name == project_name)
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return CreativeDecisionAnalyzer(db, project.id).decision_report(
        product_code=product_code,
        window_days=window_days,
    )


@router.post("/data-dashboard/creative-decisions/refresh")
def data_dashboard_refresh_creative_decisions(
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    product_code: str | None = Query(None),
    window_days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> dict:
    from app.analytics.creative_decisions import refresh_creative_decision_memory

    workspace = db.scalar(select(Workspace).where(Workspace.name == workspace_name))
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    project = db.scalar(
        select(Project).where(Project.workspace_id == workspace.id, Project.name == project_name)
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    report, memories = refresh_creative_decision_memory(
        db,
        project_id=project.id,
        product_code=product_code,
        window_days=window_days,
    )
    db.commit()
    return {
        "memories_created": len(memories),
        "memory_ids": [memory.id for memory in memories],
        "report": report,
    }


@router.get("/data-dashboard/store-analytics")
def data_dashboard_store_analytics(
    workspace_name: str = Query(...),
    project_name: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    from app.data.models import Product as ProductModel, Project, Workspace

    workspace = db.scalar(select(Workspace).where(Workspace.name == workspace_name))
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    project = db.scalar(
        select(Project).where(Project.workspace_id == workspace.id, Project.name == project_name)
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    product_memories = db.scalars(
        select(GmMemory)
        .where(
            GmMemory.project_id == project.id,
            GmMemory.memory_scope == "product",
            GmMemory.source_type.in_(["shopify_sync", "feedback_import", "offline_csv_import"]),
        )
        .order_by(desc(GmMemory.created_at))
        .limit(200)
    ).all()

    by_product: dict[str, dict] = {}
    for mem in product_memories:
        code = mem.product_code or "unknown"
        if code not in by_product:
            by_product[code] = {"revenue": 0, "quantity": 0, "score_hint": 0, "memory_count": 0}
        c = mem.content or {}
        by_product[code]["revenue"] = max(by_product[code]["revenue"], float(c.get("total_revenue", 0)))
        by_product[code]["quantity"] = max(by_product[code]["quantity"], int(c.get("total_quantity", 0)))
        by_product[code]["score_hint"] = max(by_product[code]["score_hint"], float(mem.score_hint or 0))
        by_product[code]["memory_count"] += 1

    products = sorted(by_product.items(), key=lambda kv: kv[1]["revenue"], reverse=True)
    total_rev = sum(p[1]["revenue"] for p in products)

    # Query thumbnails for all product codes in one batch
    from app.data.models import PipelineRun as PR, VariantAsset as VA

    product_codes = [code for code, _ in products[:30]]
    thumbnails: dict[str, list[str]] = {code: [] for code in product_codes}
    if product_codes:
        # Get one representative run per product, then get its image assets
        for code in product_codes:
            if len(thumbnails[code]) >= 4:
                continue
            asset_rows = db.scalars(
                select(VA.uri)
                .join(PR, VA.run_id == PR.id)
                .where(
                    PR.product_code == code,
                    VA.asset_type.in_(["generated_image", "image"]),
                    VA.uri.isnot(None),
                )
                .order_by(desc(VA.created_at))
                .limit(4)
            ).all()
            thumbnails[code] = [uri for uri in asset_rows if uri]

    return {
        "workspace_name": workspace_name,
        "project_name": project_name,
        "product_count": len(products),
        "total_revenue": round(total_rev, 2),
        "products": [
            {
                "product_code": code,
                "revenue": round(data["revenue"], 2),
                "quantity": data["quantity"],
                "revenue_share_pct": round(data["revenue"] / total_rev * 100, 1) if total_rev > 0 else 0,
                "score_hint": round(data["score_hint"], 2),
                "memory_count": data["memory_count"],
                "thumbnails": thumbnails.get(code, [])[:4],
            }
            for code, data in products
        ],
    }


@router.get("/data-dashboard/auto-sync-config")
def get_auto_sync_config(
    workspace_name: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    workspace = db.scalar(select(Workspace).where(Workspace.name == workspace_name))
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {
        "workspace_name": workspace_name,
        "shopify_auto_sync_minutes": workspace.shopify_auto_sync_minutes,
        "meta_auto_sync_minutes": workspace.meta_auto_sync_minutes,
        "shopify_last_sync_at": workspace.shopify_last_sync_at.isoformat() if workspace.shopify_last_sync_at else None,
        "meta_last_sync_at": workspace.meta_last_sync_at.isoformat() if workspace.meta_last_sync_at else None,
    }


class AutoSyncPatchRequest(_PydanticBaseModel):
    shopify_auto_sync_minutes: int | None = None
    meta_auto_sync_minutes: int | None = None


@router.patch("/data-dashboard/auto-sync-config")
def patch_auto_sync_config(
    workspace_name: str = Query(...),
    payload: AutoSyncPatchRequest = None,
    db: Session = Depends(get_db),
) -> dict:
    workspace = db.scalar(select(Workspace).where(Workspace.name == workspace_name))
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if payload is None:
        payload = AutoSyncPatchRequest()
    if payload.shopify_auto_sync_minutes is not None:
        workspace.shopify_auto_sync_minutes = max(0, payload.shopify_auto_sync_minutes)
    if payload.meta_auto_sync_minutes is not None:
        workspace.meta_auto_sync_minutes = max(0, payload.meta_auto_sync_minutes)
    db.commit()
    return {
        "workspace_name": workspace_name,
        "shopify_auto_sync_minutes": workspace.shopify_auto_sync_minutes,
        "meta_auto_sync_minutes": workspace.meta_auto_sync_minutes,
        "shopify_last_sync_at": workspace.shopify_last_sync_at.isoformat() if workspace.shopify_last_sync_at else None,
        "meta_last_sync_at": workspace.meta_last_sync_at.isoformat() if workspace.meta_last_sync_at else None,
    }


def _daily_revenue_trend(snapshots: list) -> list[dict]:
    from collections import defaultdict

    daily: dict[str, float] = defaultdict(float)
    for s in snapshots:
        day = str(s.period_start or s.created_at.date())
        daily[day] += float((s.metrics or {}).get("revenue", 0))
    sorted_days = sorted(daily.keys())[-14:]
    return [{"date": d, "revenue": round(daily[d], 2)} for d in sorted_days]
