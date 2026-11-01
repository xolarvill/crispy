from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.contracts import ConversionForecast, FeedbackRow, ScoreCard, StageName

PipelineMode = Literal[
    "copy_image_only",
    "dtc_site_image",
    "video_only",
    "full_multimodal",
    "marketplace_main_image",
    "tiktok_shop_video",
]
ApprovalMode = Literal["manual", "semi_auto", "full_auto"]


class RunCreateRequest(BaseModel):
    workspace_name: str
    project_name: str
    product_name: str
    product_code: str
    industry_code: str
    campaign_name: str
    channel: str = "meta"
    objective: str = "conversions"
    market: str = "US"
    locale: str = "en-US"
    creative_preset: str
    creative_specs: dict = Field(default_factory=dict)
    variant_count: int = 8
    context: dict = Field(default_factory=dict)
    model_provider: str | None = "openai"
    model_name: str | None = "gpt-4.1"
    pipeline_mode: PipelineMode = "full_multimodal"
    approval_mode: ApprovalMode = "manual"
    enable_research: bool = False
    manual_research_brief: str = ""
    memory_selection: dict = Field(default_factory=dict)
    business_context: dict = Field(default_factory=dict)
    category_tags: list[str] = Field(default_factory=list)


class StageTaskView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    stage_name: str
    status: str
    attempt: int
    review_notes: str | None = None
    output_payload: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)
    summary: str = ""
    raw_ref: str = ""
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class AgentTraceEventView(BaseModel):
    id: str
    run_id: str
    stage_task_id: str | None = None
    stage_name: str
    agent_name: str
    event_type: str
    visibility: str = "user"
    message: str = ""
    provider_name: str | None = None
    model_name: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime


class RunStatusExplanation(BaseModel):
    tone: Literal["info", "review", "danger", "success"] = "info"
    headline: str
    detail: str = ""
    primary_action: str = ""
    next_actions: list[str] = Field(default_factory=list)


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    current_stage: str | None
    workspace_id: str
    project_id: str
    product_id: str
    product_code: str
    industry_code: str
    campaign_id: str
    market: str
    locale: str
    model_provider: str | None
    model_name: str | None
    creative_preset: str
    creative_specs: dict = Field(default_factory=dict)
    pipeline_mode: PipelineMode = "full_multimodal"
    approval_mode: str = "manual"
    enable_research: bool = False
    manual_research_brief: str = ""
    memory_selection: dict = Field(default_factory=dict)
    business_context: dict = Field(default_factory=dict)
    category_tags: list[str] = Field(default_factory=list)
    budget_used: float
    variant_count: int
    created_at: datetime
    updated_at: datetime
    stage_tasks: list[StageTaskView] = Field(default_factory=list)
    trace_events: list[AgentTraceEventView] = Field(default_factory=list)
    variant_summary: dict = Field(default_factory=dict)
    status_explanation: RunStatusExplanation
    latest_scorecard: ScoreCard | None = None
    latest_forecast: ConversionForecast | None = None
    research_context: dict = Field(default_factory=dict)


class RunMediaPreview(BaseModel):
    uri: str
    kind: Literal["image", "video"]
    source: Literal["generated", "input"]


class RunMediaResponse(BaseModel):
    run_id: str
    media_count: int = 0
    media_previews: list[RunMediaPreview] = Field(default_factory=list)


class RunSummary(BaseModel):
    id: str
    status: str
    current_stage: str | None
    pipeline_mode: PipelineMode = "full_multimodal"
    project_id: str
    product_code: str = ""
    industry_code: str = ""
    updated_at: datetime
    media_count: int = 0
    media_previews: list[RunMediaPreview] = Field(default_factory=list)


class ReviewActionRequest(BaseModel):
    notes: str = ""


class FeedbackImportResponse(BaseModel):
    import_id: str
    rows: int
    snapshots_created: int
    memory_entry_id: str | None = None


class LeaderboardItem(BaseModel):
    creative_key: str
    weighted_score: float
    ctr: float
    cpc: float
    cpa: float
    roas: float
    recommendation: str


class LeaderboardResponse(BaseModel):
    project_id: str
    ranking: list[LeaderboardItem]


class PersonaView(BaseModel):
    agent_name: str
    display_name: str | None = None
    stage: str | None = None
    role: str | None = None
    content: str
    version: int
    source_path: str


class PersonaPatchRequest(BaseModel):
    content: str
    changed_by: str = "dashboard"


class PipelineStageResult(BaseModel):
    stage: StageName
    payload: dict


class FeedbackImportRequest(BaseModel):
    workspace_name: str
    project_name: str
    rows: list[FeedbackRow]
    file_name: str = "manual_import.csv"


class VariantReviewRequest(BaseModel):
    action: Literal[
        "approve_variant",
        "reject_variant",
        "shortlist_variant",
        "set_winner",
        "request_regeneration",
    ]
    comment: str = ""
    tags: list[str] = Field(default_factory=list)


class VariantSelectRequest(BaseModel):
    shortlist: bool = False
    winner: bool = False
    comment: str = ""


class VariantRegenerateRequest(BaseModel):
    reason: str
    target_stage: str | None = None


class PersonaMeta(BaseModel):
    agent_name: str
    display_name: str
    stage: str
    role: str
    order: int
    source_path: str


class AgentApiConfigView(BaseModel):
    agent_name: str
    provider_name: str
    model_name: str
    api_base_url: str | None = None
    api_key_env: str | None = None
    api_key_available: bool = False
    image_provider_name: str | None = None
    image_model_name: str | None = None
    image_api_base_url: str | None = None
    image_api_key_env: str | None = None
    image_api_key_available: bool = False
    video_provider_name: str | None = None
    video_model_name: str | None = None
    video_api_base_url: str | None = None
    video_api_key_env: str | None = None
    video_api_key_available: bool = False
    tavily_api_key_env: str | None = None
    tavily_api_key_available: bool = False
    firecrawl_api_key_env: str | None = None
    firecrawl_api_key_available: bool = False
    thinking_mode: Literal["auto", "enabled", "disabled"] = "auto"
    thinking_budget_tokens: int | None = None
    max_output_tokens: int | None = None
    request_timeout_seconds: int | None = None
    thinking_applied: bool = False
    streaming_enabled: bool = False
    extra: dict = Field(default_factory=dict)
    is_default: bool = False
    updated_at: datetime


class AgentApiConfigPatchRequest(BaseModel):
    provider_name: str | None = None
    model_name: str | None = None
    api_base_url: str | None = None
    api_key_env: str | None = None
    image_provider_name: str | None = None
    image_model_name: str | None = None
    image_api_base_url: str | None = None
    image_api_key_env: str | None = None
    video_provider_name: str | None = None
    video_model_name: str | None = None
    video_api_base_url: str | None = None
    video_api_key_env: str | None = None
    thinking_mode: Literal["auto", "enabled", "disabled"] | None = None
    thinking_budget_tokens: int | None = None
    max_output_tokens: int | None = None
    request_timeout_seconds: int | None = None
    streaming_enabled: bool | None = None
    extra: dict | None = None


class IntegrationConfigView(BaseModel):
    id: str
    platform: str
    config_key: str
    label: str
    env_var: str
    is_required: bool = True
    is_set: bool = False
    updated_at: str | None = None


class IntegrationConfigPatchRequest(BaseModel):
    env_var: str | None = None


class DeliverablesResponse(BaseModel):
    run_id: str
    winner_variant_id: str | None = None
    deliverables: dict = Field(default_factory=dict)
    score: dict = Field(default_factory=dict)


class VariantAssetView(BaseModel):
    id: str
    stage_name: str
    asset_type: str
    uri: str | None = None
    provider_name: str | None = None
    model_name: str | None = None
    prompt_summary: str | None = None
    failure_category: str | None = None
    error_message: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime


class VariantReviewView(BaseModel):
    id: str
    action: str
    comment: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)
    created_at: datetime


class VariantScoreView(BaseModel):
    id: str
    stage_name: str
    score_type: str
    total_score: float | None = None
    compliance_level: str | None = None
    recommended_action: str | None = None
    sub_scores: dict = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    forecast: dict = Field(default_factory=dict)
    payload: dict = Field(default_factory=dict)
    created_at: datetime


class RunVariantView(BaseModel):
    id: str
    run_id: str
    variant_id: str
    angle: str = ""
    hook: str = ""
    message: str = ""
    status: str
    current_score: float | None = None
    is_winner: bool = False
    shortlisted: bool = False
    review_status: str | None = None
    regenerate_requested: bool = False
    metadata_json: dict = Field(default_factory=dict)
    strategy_brief: dict = Field(default_factory=dict)
    execution_summary: dict = Field(default_factory=dict)
    quality_summary: dict = Field(default_factory=dict)
    assets: list[VariantAssetView] = Field(default_factory=list)
    scores: list[VariantScoreView] = Field(default_factory=list)
    reviews: list[VariantReviewView] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ExecutionMemoryLedgerResponse(BaseModel):
    run_ledger: dict = Field(default_factory=dict)
    stage_handoffs: list[dict] = Field(default_factory=list)
    variant_ledgers: list[dict] = Field(default_factory=list)
    recent_reviews: list[dict] = Field(default_factory=list)
    active_regeneration_goals: list[dict] = Field(default_factory=list)


class VariantsResponse(BaseModel):
    run_id: str
    variants: list[dict] = Field(default_factory=list)
    ranked: list[dict] = Field(default_factory=list)
    items: list[RunVariantView] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


class PipelineModeView(BaseModel):
    mode: PipelineMode
    display_name: str
    stages: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    agent_count: int = 0


class DataSourceInfo(BaseModel):
    id: str
    name: str
    path: str
    url: str
    is_active: bool = False


class DataSourceListResponse(BaseModel):
    active_url: str
    items: list[DataSourceInfo] = Field(default_factory=list)


class DataSourceSelectRequest(BaseModel):
    url: str


class BulkIdsRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


class ArtifactListItem(BaseModel):
    artifact_id: str
    run_id: str
    artifact_type: str
    stage_name: str
    pipeline_mode: PipelineMode | str
    product_code: str = ""
    uri: str
    preview_text: str = ""
    score: float | None = None
    created_at: datetime


class ArtifactListResponse(BaseModel):
    page: int
    page_size: int
    total: int
    items: list[ArtifactListItem] = Field(default_factory=list)


class AssetProductItem(BaseModel):
    product_id: str
    product_code: str
    name: str
    workspace_name: str
    project_name: str
    thumbnail_uri: str | None = None
    run_count: int = 0
    asset_count: int = 0
    memory_count: int = 0
    latest_run_at: datetime | None = None
    created_at: datetime


class AssetProductListResponse(BaseModel):
    page: int
    page_size: int
    total: int
    items: list[AssetProductItem] = Field(default_factory=list)


class GmMemoryItem(BaseModel):
    id: str
    project_id: str
    run_id: str | None = None
    memory_scope: str
    product_code: str | None = None
    industry_code: str | None = None
    source_type: str
    memory_type: str
    status: str = "active"
    pinned: bool = False
    score_hint: float | None = None
    content: dict = Field(default_factory=dict)
    created_at: datetime


class GmMemoryUpdateRequest(BaseModel):
    status: Literal["active", "archived", "superseded"] | None = None
    pinned: bool | None = None
    superseded_by_id: str | None = None


class GmMemoryReviewActionRequest(BaseModel):
    action: Literal["approve", "pin", "unpin", "reject", "resolve_conflicts"]
    notes: str | None = None
    changed_by: str = "operator"


class GmMemoryCompactRequest(BaseModel):
    project_id: str
    memory_scope: Literal["shop", "product", "industry"]
    product_code: str | None = None
    industry_code: str | None = None
    shop_id: str | None = None
    limit: int = Field(default=20, ge=1, le=200)


class GmReflectionItem(BaseModel):
    id: str
    project_id: str
    run_id: str | None = None
    feedback_import_id: str | None = None
    reflection_type: str
    target_scope: str
    shop_id: str | None = None
    product_code: str | None = None
    industry_code: str | None = None
    pipeline_mode: str | None = None
    confidence_score: float | None = None
    evidence_count: int = 0
    summary: str = ""
    payload: dict = Field(default_factory=dict)
    created_at: datetime


class GmPolicyItem(BaseModel):
    id: str
    project_id: str
    version: int
    status: str
    target_scope: str
    shop_id: str | None = None
    product_code: str | None = None
    industry_code: str | None = None
    pipeline_mode: str | None = None
    confidence_score: float | None = None
    evidence_count: int = 0
    replay_status: str = "needs_review"
    replay_score: float | None = None
    replay_summary: str | None = None
    replay_details: dict = Field(default_factory=dict)
    source_reflection_ids: list[str] = Field(default_factory=list)
    content: dict = Field(default_factory=dict)
    notes: str | None = None
    created_at: datetime
    activated_at: datetime | None = None
    last_evaluated_at: datetime | None = None


class GmPolicyPromoteRequest(BaseModel):
    changed_by: str = Field(default="dashboard")
    notes: str | None = None


class RunPreflightRequest(BaseModel):
    pipeline_mode: PipelineMode = "full_multimodal"
    has_image_inputs: bool = False
    has_video_inputs: bool = False
    creative_specs: dict = Field(default_factory=dict)


class CapabilityCheckItem(BaseModel):
    key: str
    severity: Literal["ok", "warn", "error"]
    message: str
    stage_name: str | None = None
    agent_name: str | None = None


class CapabilitySpecItem(BaseModel):
    key: str
    capability: str
    stage_name: str
    agent_name: str
    provider_name: str | None = None
    model_name: str | None = None
    api_base_url: str | None = None
    api_key_env: str | None = None
    api_key_available: bool = False
    supported: bool | None = None
    supports: dict[str, bool | None] = Field(default_factory=dict)
    setup_hint: str = ""


class RunPreflightResponse(BaseModel):
    ok: bool = True
    severity: Literal["ok", "warn", "error"] = "ok"
    summary: str = ""
    checks: list[CapabilityCheckItem] = Field(default_factory=list)
    capabilities: list[CapabilitySpecItem] = Field(default_factory=list)


class QueueStatusResponse(BaseModel):
    total_queued: int
    queued_by_stage: dict[str, int] = Field(default_factory=dict)
    status_counts: dict[str, int] = Field(default_factory=dict)
    currently_running: int


class QueueRunningTask(BaseModel):
    task_id: str
    run_id: str
    stage_name: str
    attempt: int
    started_at: str
    duration_seconds: float


class QueueHealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    concurrency: int
    active_workers: int
    total_completed: int
    total_failed: int
    video_poller_last_run: str | None = None
    video_poller_ok: bool = True


class CreativePresetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    workspace_name: str = Field(default="workspace_demo")
    image_size: str | None = None
    video_size: str | None = None
    resolution: str | None = None
    video_duration_seconds: int | None = None
    storyboard_candidate_count: int | None = None
    tiktok_video_style: str | None = None
    site_surface: str | None = None
    platform_targets: dict = Field(default_factory=dict)


class CreativePresetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    image_size: str | None = None
    video_size: str | None = None
    resolution: str | None = None
    video_duration_seconds: int | None = None
    storyboard_candidate_count: int | None = None
    tiktok_video_style: str | None = None
    site_surface: str | None = None
    platform_targets: dict | None = None


class CreativePresetView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_name: str
    name: str
    image_size: str | None = None
    video_size: str | None = None
    resolution: str | None = None
    video_duration_seconds: int | None = None
    storyboard_candidate_count: int = 1
    tiktok_video_style: str | None = None
    site_surface: str | None = None
    platform_targets: dict
    created_at: datetime
    updated_at: datetime


class CreativePresetListResponse(BaseModel):
    system: list[dict]  # system presets as plain dicts
    user: list[CreativePresetView]


class RunTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    workspace_name: str = Field(default="workspace_demo")
    config_json: dict = Field(default_factory=dict)
    is_shared: bool = False


class RunTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    config_json: dict | None = None
    is_shared: bool | None = None


class RunTemplateView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_name: str
    name: str
    config_json: dict
    is_shared: bool
    created_at: datetime
    updated_at: datetime


class ProductConfigHint(BaseModel):
    product_code: str
    pipeline_mode: str | None = None
    approval_mode: str | None = None
    creative_preset: str | None = None
    creative_specs: dict | None = None
    channel: str | None = None
    objective: str | None = None
    last_run_at: datetime | None = None


class ShopAnalysisRequest(BaseModel):
    shop_id: str | None = None
    store_url: str = Field(..., min_length=1, description="Store URL to research")
    description: str = Field(default="", description="Operator-provided store description")
    industry_code: str = Field(default="general", description="Industry code for GmMemory association")
    workspace_name: str = Field(default="workspace_demo")
    project_name: str = Field(default="")
    refresh_reason: str | None = None
    research_focus: Literal[
        "full_intelligence",
        "store_context",
        "competitive_landscape",
        "industry_baseline",
        "audience_pain_points",
        "compliance_scan",
    ] = "full_intelligence"
    execution_mode: Literal["sync", "queued"] = "sync"


class ShopAnalysisResult(BaseModel):
    source_type: str  # "shop_profile" or "competitor_analysis"
    content: dict     # structured profile or markdown report
    summary: str      # one-line summary for display
    research_status: str = "unknown"
    evidence_count: int = 0
    evidence_quality: dict = Field(default_factory=dict)
    conflict_count: int = 0
    research_focus: str = "full_intelligence"


class ResearchTaskItem(BaseModel):
    id: str
    project_id: str
    shop_id: str | None = None
    shop_name: str | None = None
    store_url: str
    industry_code: str = "general"
    task_type: str = "full_intelligence"
    status: str = "queued"
    priority: int = 2
    source: str = "manual"
    refresh_reason: str | None = None
    memory_ids: list[str] = Field(default_factory=list)
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ShopAnalysisResponse(BaseModel):
    id: str
    shop_id: str | None = None
    shop_name: str | None = None
    store_url: str
    industry_code: str
    profile: ShopAnalysisResult | None = None
    competitor_analysis: ShopAnalysisResult | None = None
    extended_results: list[ShopAnalysisResult] = Field(default_factory=list)
    status: str  # "queued", "running", "completed", "failed"
    research_focus: str = "full_intelligence"
    task: ResearchTaskItem | None = None
    tool_status: dict = Field(default_factory=dict)
    error_message: str | None = None
    created_at: datetime


class ShopAnalysisPreflightResponse(BaseModel):
    ok: bool
    severity: Literal["ok", "warn", "error"]
    checks: list[dict] = Field(default_factory=list)


class ShopAnalysisListItem(BaseModel):
    id: str
    store_url: str
    industry_code: str
    status: str
    source_type: str
    memory_type: str = ""
    research_focus: str = "full_intelligence"
    research_status: str = "unknown"
    evidence_count: int = 0
    evidence_quality: dict = Field(default_factory=dict)
    conflict_count: int = 0
    expires_at: str | None = None
    refresh_state: str = "unknown"
    latest_task: ResearchTaskItem | None = None
    summary: str
    created_at: datetime


class ShopAnalysisHistoryResponse(BaseModel):
    items: list[ShopAnalysisListItem]


class ResearchTaskHistoryResponse(BaseModel):
    items: list[ResearchTaskItem]


class ShopItem(BaseModel):
    id: str | None = None
    name: str
    industry_code: str = "general"
    store_url: str | None = None
    description: str | None = None
    category_count: int = 0
    product_count: int = 0
    run_count: int = 0
    analysis_count: int = 0
    research_ready_count: int = 0
    research_blocked_count: int = 0
    memory_count: int = 0
    memory_safe_count: int = 0
    memory_review_count: int = 0
    memory_conflict_count: int = 0
    archived_at: datetime | None = None
    last_analyzed_at: datetime | None = None
    site_count: int = 0
    channel_count: int = 0


class ShopPatchRequest(BaseModel):
    name: str | None = None
    industry_code: str | None = None
    store_url: str | None = None
    description: str | None = None
    archived: bool | None = None


class ShopListResponse(BaseModel):
    shops: list[ShopItem]


class ShopSiteItem(BaseModel):
    id: str | None = None
    workspace_id: str | None = None
    label: str = ""
    url: str = Field(..., min_length=1, max_length=512)
    site_type: str = "storefront"
    platform: str | None = None
    locale: str | None = None
    currency: str | None = None
    is_primary: bool = False
    metadata_json: dict = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ShopSitePatchRequest(BaseModel):
    label: str | None = None
    url: str | None = Field(default=None, min_length=1, max_length=512)
    site_type: str | None = None
    platform: str | None = None
    locale: str | None = None
    currency: str | None = None
    is_primary: bool | None = None
    metadata_json: dict | None = None


class ShopSiteListResponse(BaseModel):
    sites: list[ShopSiteItem]


class ShopChannelAccountItem(BaseModel):
    id: str | None = None
    workspace_id: str | None = None
    platform: str = Field(..., min_length=1, max_length=32)
    account_key: str = Field(..., min_length=1, max_length=128)
    label: str = ""
    account_id: str | None = None
    account_url: str | None = None
    credential_env_vars: dict = Field(default_factory=dict)
    sync_settings: dict = Field(default_factory=dict)
    attribution_rules: dict = Field(default_factory=dict)
    status: str = "active"
    is_primary: bool = False
    ready: bool = False
    missing_credentials: list[str] = Field(default_factory=list)
    last_verified_at: datetime | None = None
    last_sync_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ShopChannelAccountPatchRequest(BaseModel):
    platform: str | None = Field(default=None, min_length=1, max_length=32)
    account_key: str | None = Field(default=None, min_length=1, max_length=128)
    label: str | None = None
    account_id: str | None = None
    account_url: str | None = None
    credential_env_vars: dict | None = None
    sync_settings: dict | None = None
    attribution_rules: dict | None = None
    status: Literal["active", "warning", "disabled", "archived"] | None = None
    is_primary: bool | None = None


class ShopChannelAccountListResponse(BaseModel):
    accounts: list[ShopChannelAccountItem]


class CategoryItem(BaseModel):
    name: str


class CategoryListResponse(BaseModel):
    categories: list[CategoryItem]


# ── Content Calendar ────────────────────────────────────────────────────────


class ContentScheduleCreateRequest(BaseModel):
    workspace_id: str
    project_id: str
    variant_id: str | None = None
    campaign_id: str | None = None
    channel_account_id: str | None = None
    title: str = Field(..., min_length=1, max_length=256)
    channel: str = "meta"
    scheduled_date: str  # "YYYY-MM-DD"
    scheduled_time: str | None = None  # "HH:MM"
    publish_payload: dict = Field(default_factory=dict)
    notes: str | None = None


class ContentScheduleUpdateRequest(BaseModel):
    title: str | None = None
    channel: str | None = None
    channel_account_id: str | None = None
    scheduled_date: str | None = None
    scheduled_time: str | None = None
    state: str | None = None
    publish_payload: dict | None = None
    notes: str | None = None
    variant_id: str | None = None
    campaign_id: str | None = None


class ContentSchedulePublishRequest(BaseModel):
    channel_account_id: str | None = None
    publish_payload: dict | None = None


class ContentScheduleView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    project_id: str
    variant_id: str | None = None
    campaign_id: str | None = None
    channel_account_id: str | None = None
    title: str
    channel: str
    scheduled_date: str
    scheduled_time: str | None = None
    state: str
    publish_payload: dict = Field(default_factory=dict)
    platform_post_id: str | None = None
    platform_post_url: str | None = None
    publish_error: str | None = None
    published_at: datetime | None = None
    notion_page_id: str | None = None
    notion_sync_error: str | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class ContentScheduleListResponse(BaseModel):
    items: list[ContentScheduleView] = Field(default_factory=list)


class NotionConnectionTestResponse(BaseModel):
    ok: bool
    error: str | None = None


class VariantScheduleCandidate(BaseModel):
    variant_id: str
    run_id: str
    hook: str = ""
    message: str = ""
    status: str
    is_winner: bool = False
    product_code: str = ""
    campaign_name: str = ""
    channel: str = "meta"
