from __future__ import annotations

import uuid
from datetime import UTC, datetime, date
from enum import StrEnum

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.data.base import Base, json_type


def utcnow() -> datetime:
    return datetime.now(UTC)


class StageName(StrEnum):
    INTAKE = "intake"
    PLANNING = "planning"
    DIVERGENCE = "divergence"
    COPY_IMAGE_GENERATION = "copy_image_generation"
    VIDEO_SCRIPTING = "video_scripting"
    STORYBOARD_IMAGE_GENERATION = "storyboard_image_generation"
    VIDEO_GENERATION = "video_generation"
    VISUAL_QUALITY_ASSESSMENT = "visual_quality_assessment"
    EVALUATION_SELECTION = "evaluation_selection"


class RunStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class ApprovalMode(StrEnum):
    MANUAL = "manual"
    SEMI_AUTO = "semi_auto"
    FULL_AUTO = "full_auto"


class TaskStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class TaskFailureCategory(StrEnum):
    PROVIDER_ERROR = "provider_error"
    SCHEMA_ERROR = "schema_error"
    COMPLIANCE_BLOCK = "compliance_block"
    TIMEOUT = "timeout"
    HUMAN_REJECT = "human_reject"
    UNKNOWN = "unknown"


class VariantLifecycleStatus(StrEnum):
    DRAFT = "draft"
    GENERATED = "generated"
    SHORTLISTED = "shortlisted"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REGENERATION = "needs_regeneration"
    WINNER = "winner"
    FAILED = "failed"


class VariantReviewAction(StrEnum):
    APPROVE = "approve_variant"
    REJECT = "reject_variant"
    SHORTLIST = "shortlist_variant"
    SET_WINNER = "set_winner"
    REQUEST_REGENERATION = "request_regeneration"


class ExecutionMemoryScope(StrEnum):
    RUN = "run"
    VARIANT = "variant"
    STAGE_HANDOFF = "stage_handoff"
    REVIEW = "review"
    REGENERATION = "regeneration"


class ExecutionMemoryStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


class ExecutionMemorySource(StrEnum):
    SYSTEM_STAGE = "system_stage"
    HUMAN_REVIEW = "human_review"
    VISUAL_QA = "visual_qa"
    EVALUATION = "evaluation"
    REGENERATION = "regeneration"


class Workspace(Base):
    __tablename__ = "workspace"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    config: Mapped[dict] = mapped_column(json_type(), default=dict)
    industry_code: Mapped[str] = mapped_column(String(128), default="general")
    store_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shopify_auto_sync_minutes: Mapped[int] = mapped_column(Integer, default=0)
    meta_auto_sync_minutes: Mapped[int] = mapped_column(Integer, default=0)
    shopify_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    projects: Mapped[list["Project"]] = relationship(back_populates="workspace")
    sites: Mapped[list["ShopSite"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")
    channel_accounts: Mapped[list["ShopChannelAccount"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")


class ShopSite(Base):
    __tablename__ = "shop_site"
    __table_args__ = (UniqueConstraint("workspace_id", "url", name="uq_shop_site_workspace_url"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(128), default="")
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    site_type: Mapped[str] = mapped_column(String(32), default="storefront")
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    workspace: Mapped[Workspace] = relationship(back_populates="sites")


class ShopChannelAccount(Base):
    __tablename__ = "shop_channel_account"
    __table_args__ = (
        UniqueConstraint("workspace_id", "platform", "account_key", name="uq_shop_channel_workspace_platform_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    account_key: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(128), default="")
    account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    credential_env_vars: Mapped[dict] = mapped_column(json_type(), default=dict)
    sync_settings: Mapped[dict] = mapped_column(json_type(), default=dict)
    attribution_rules: Mapped[dict] = mapped_column(json_type(), default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    workspace: Mapped[Workspace] = relationship(back_populates="channel_accounts")


class Project(Base):
    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_project_workspace_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    market: Mapped[str] = mapped_column(String(16), default="US")
    locale: Mapped[str] = mapped_column(String(16), default="en-US")
    metric_weights: Mapped[dict] = mapped_column(
        json_type(),
        default=lambda: {"ctr": 0.35, "cpc": 0.15, "cpa": 0.30, "roas": 0.20},
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    workspace: Mapped[Workspace] = relationship(back_populates="projects")
    products: Mapped[list["Product"]] = relationship(back_populates="project")
    campaigns: Mapped[list["Campaign"]] = relationship(back_populates="project")
    runs: Mapped[list["PipelineRun"]] = relationship(back_populates="project")


class Product(Base):
    __tablename__ = "product"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_product_project_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    product_code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped[Project] = relationship(back_populates="products")
    campaigns: Mapped[list["Campaign"]] = relationship(back_populates="product")
    runs: Mapped[list["PipelineRun"]] = relationship(back_populates="product")


class Campaign(Base):
    __tablename__ = "campaign"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_campaign_project_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("product.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), default="meta")
    objective: Mapped[str] = mapped_column(String(64), default="conversions")
    platform_campaign_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    platform_ad_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped[Project] = relationship(back_populates="campaigns")
    product: Mapped[Product | None] = relationship(back_populates="campaigns")
    runs: Mapped[list["PipelineRun"]] = relationship(back_populates="campaign")


class PipelineRun(Base):
    __tablename__ = "pipeline_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), nullable=False)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaign.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.DRAFT.value)
    current_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    market: Mapped[str] = mapped_column(String(16), default="US")
    locale: Mapped[str] = mapped_column(String(16), default="en-US")
    pipeline_mode: Mapped[str] = mapped_column(String(32), default="full_multimodal")
    approval_mode: Mapped[str] = mapped_column(String(16), default=ApprovalMode.MANUAL.value)
    product_code: Mapped[str] = mapped_column(String(128), default="")
    industry_code: Mapped[str] = mapped_column(String(128), default="general")
    creative_preset: Mapped[str] = mapped_column(String(64), default="meta_square_5s")
    creative_specs: Mapped[dict] = mapped_column(json_type(), default=dict)
    model_provider: Mapped[str] = mapped_column(String(64), default="openai")
    model_name: Mapped[str] = mapped_column(String(128), default="gpt-4.1")
    budget_used: Mapped[float] = mapped_column(Float, default=0.0)
    variant_count: Mapped[int] = mapped_column(Integer, default=8)
    enable_research: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_research_brief: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_context: Mapped[dict] = mapped_column(json_type(), default=dict)
    category_tags: Mapped[list[str]] = mapped_column(json_type(), default=list)
    context_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="runs")
    product: Mapped[Product] = relationship(back_populates="runs")
    campaign: Mapped[Campaign] = relationship(back_populates="runs")
    stage_tasks: Mapped[list["StageTask"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    scorecards: Mapped[list["ScoreCard"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    variants: Mapped[list["RunVariant"]] = relationship(cascade="all, delete-orphan")
    trace_events: Mapped[list["AgentTraceEvent"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class StageTask(Base):
    __tablename__ = "stage_task"
    __table_args__ = (UniqueConstraint("run_id", "stage_name", name="uq_stage_task_run_stage"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    stage_name: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.DRAFT.value)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=2)
    max_retries: Mapped[int] = mapped_column(Integer, default=4)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    output_payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[PipelineRun] = relationship(back_populates="stage_tasks")
    scorecards: Mapped[list["ScoreCard"]] = relationship(back_populates="stage_task")
    trace_events: Mapped[list["AgentTraceEvent"]] = relationship(back_populates="stage_task")


class ExecutionMemoryEntry(Base):
    __tablename__ = "execution_memory_entry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    stage_task_id: Mapped[str | None] = mapped_column(ForeignKey("stage_task.id"), nullable=True)
    run_variant_id: Mapped[str | None] = mapped_column(ForeignKey("run_variant.id"), nullable=True)
    stage_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    memory_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=ExecutionMemoryStatus.ACTIVE.value)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentTraceEvent(Base):
    __tablename__ = "agent_trace_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    stage_task_id: Mapped[str | None] = mapped_column(ForeignKey("stage_task.id"), nullable=True)
    stage_name: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), default="user")
    message: Mapped[str] = mapped_column(Text, default="")
    provider_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[PipelineRun] = relationship(back_populates="trace_events")
    stage_task: Mapped[StageTask | None] = relationship(back_populates="trace_events")


class BrandCreative(Base):
    __tablename__ = "brand_creative"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("brand_creative.id"))
    name: Mapped[str] = mapped_column(String(160))
    document: Mapped[dict] = mapped_column(json_type())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunVariant(Base):
    __tablename__ = "run_variant"
    __table_args__ = (UniqueConstraint("run_id", "variant_id", name="uq_run_variant_run_variant"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    variant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    angle: Mapped[str] = mapped_column(Text, default="")
    hook: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default=VariantLifecycleStatus.DRAFT.value)
    current_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False)
    shortlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    review_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    regenerate_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    assets: Mapped[list["VariantAsset"]] = relationship(back_populates="variant", cascade="all, delete-orphan")
    reviews: Mapped[list["VariantReview"]] = relationship(back_populates="variant", cascade="all, delete-orphan")
    scores: Mapped[list["VariantScore"]] = relationship(back_populates="variant", cascade="all, delete-orphan")


class VariantAsset(Base):
    __tablename__ = "variant_asset"
    __table_args__ = (UniqueConstraint("run_variant_id", "idempotency_key", name="uq_variant_asset_dedupe"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_variant_id: Mapped[str] = mapped_column(ForeignKey("run_variant.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    stage_name: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    variant: Mapped["RunVariant"] = relationship(back_populates="assets")


class VariantReview(Base):
    __tablename__ = "variant_review"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_variant_id: Mapped[str] = mapped_column(ForeignKey("run_variant.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(json_type(), default=list)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    variant: Mapped["RunVariant"] = relationship(back_populates="reviews")


class VariantScore(Base):
    __tablename__ = "variant_score"
    __table_args__ = (UniqueConstraint("run_variant_id", "score_type", name="uq_variant_score_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_variant_id: Mapped[str] = mapped_column(ForeignKey("run_variant.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    stage_name: Mapped[str] = mapped_column(String(32), nullable=False)
    score_type: Mapped[str] = mapped_column(String(32), nullable=False, default="evaluation")
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    compliance_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    recommended_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sub_scores: Mapped[dict] = mapped_column(json_type(), default=dict)
    reasons: Mapped[list[str]] = mapped_column(json_type(), default=list)
    forecast: Mapped[dict] = mapped_column(json_type(), default=dict)
    payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    variant: Mapped["RunVariant"] = relationship(back_populates="scores")


class Artifact(Base):
    __tablename__ = "artifact"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    stage_name: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    uri: Mapped[str] = mapped_column(String(512), nullable=False)
    payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[PipelineRun] = relationship(back_populates="artifacts")


class ScoreCard(Base):
    __tablename__ = "scorecard"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    stage_task_id: Mapped[str] = mapped_column(ForeignKey("stage_task.id"), nullable=False)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    sub_scores: Mapped[dict] = mapped_column(json_type(), default=dict)
    risk_labels: Mapped[list[str]] = mapped_column(json_type(), default=list)
    explanation: Mapped[dict] = mapped_column(json_type(), default=dict)
    compliance_level: Mapped[str] = mapped_column(String(16), default="low")
    ai_artifact_score: Mapped[float] = mapped_column(Float, default=0.0)
    forecast: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[PipelineRun] = relationship(back_populates="scorecards")
    stage_task: Mapped[StageTask] = relationship(back_populates="scorecards")


class FeedbackImport(Base):
    __tablename__ = "feedback_import"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    file_name: Mapped[str] = mapped_column(String(256), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_rows: Mapped[list[dict]] = mapped_column(json_type(), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshot"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaign.id"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_run.id"), nullable=True)
    creative_key: Mapped[str] = mapped_column(String(128), nullable=False)
    metrics: Mapped[dict] = mapped_column(json_type(), default=dict)
    weighted_score: Mapped[float] = mapped_column(Float, default=0.0)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PersonaVersion(Base):
    __tablename__ = "persona_version"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[str] = mapped_column(String(64), default="dashboard")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchTask(Base):
    __tablename__ = "research_task"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    shop_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    shop_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    store_url: Mapped[str] = mapped_column(String(512), nullable=False)
    industry_code: Mapped[str] = mapped_column(String(128), default="general")
    task_type: Mapped[str] = mapped_column(String(64), default="full_intelligence")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=2)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    requested_by: Mapped[str] = mapped_column(String(64), default="operator")
    refresh_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    memory_ids: Mapped[list[str]] = mapped_column(json_type(), default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GmMemory(Base):
    __tablename__ = "gm_memory"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_run.id"), nullable=True)
    memory_scope: Mapped[str] = mapped_column(String(32), default="industry")
    product_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_type: Mapped[str] = mapped_column(String(64), default="feedback_import")
    score_hint: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_type: Mapped[str] = mapped_column(String(32), default="strategy")
    status: Mapped[str] = mapped_column(String(16), default="active")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    content: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GmInstructionVersion(Base):
    __tablename__ = "gm_instruction_version"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    source_feedback_import_id: Mapped[str | None] = mapped_column(ForeignKey("feedback_import.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(json_type(), default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GmReflection(Base):
    __tablename__ = "gm_reflection"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_run.id"), nullable=True)
    feedback_import_id: Mapped[str | None] = mapped_column(ForeignKey("feedback_import.id"), nullable=True)
    reflection_type: Mapped[str] = mapped_column(String(32), default="run_outcome")
    target_scope: Mapped[str] = mapped_column(String(32), default="product")
    shop_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pipeline_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GmPolicyVersion(Base):
    __tablename__ = "gm_policy_version"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="candidate")
    target_scope: Mapped[str] = mapped_column(String(32), default="product")
    shop_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pipeline_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    replay_status: Mapped[str] = mapped_column(String(16), default="needs_review")
    replay_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    replay_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    replay_details: Mapped[dict] = mapped_column(json_type(), default=dict)
    source_reflection_ids: Mapped[list[str]] = mapped_column(json_type(), default=list)
    content: Mapped[dict] = mapped_column(json_type(), default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GmPolicyPromotion(Base):
    __tablename__ = "gm_policy_promotion"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    gm_policy_version_id: Mapped[str] = mapped_column(ForeignKey("gm_policy_version.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(16), default="promote")
    changed_by: Mapped[str] = mapped_column(String(64), default="system")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentApiConfig(Base):
    __tablename__ = "agent_api_config"
    __table_args__ = (UniqueConstraint("agent_name", name="uq_agent_api_config_agent_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(64), default="kimi")
    model_name: Mapped[str] = mapped_column(String(128), default="kimi-default-text")
    api_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    api_key_env: Mapped[str | None] = mapped_column(String(128), nullable=True)
    thinking_mode: Mapped[str] = mapped_column(String(16), default="auto")
    thinking_budget_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    streaming_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    extra: Mapped[dict] = mapped_column(json_type(), default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IntegrationSync(Base):
    __tablename__ = "integration_sync"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    channel_account_id: Mapped[str | None] = mapped_column(ForeignKey("shop_channel_account.id"), nullable=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    sync_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running")
    items_synced: Mapped[int] = mapped_column(Integer, default=0)
    error_log: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IntegrationConfig(Base):
    __tablename__ = "integration_config"
    __table_args__ = (UniqueConstraint("platform", "config_key", name="uq_integration_config_platform_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspace.id"), nullable=True)
    config_key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    env_var: Mapped[str] = mapped_column(String(128), nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CreativePreset(Base):
    __tablename__ = "creative_preset"
    __table_args__ = (UniqueConstraint("workspace_name", "name", name="uq_preset_workspace_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    image_size: Mapped[str | None] = mapped_column(String(16), nullable=True)
    video_size: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    video_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    platform_targets: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RunTemplate(Base):
    __tablename__ = "run_template"
    __table_args__ = (UniqueConstraint("workspace_name", "name", name="uq_template_workspace_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    config_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ContentSchedule(Base):
    __tablename__ = "content_schedule"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    variant_id: Mapped[str | None] = mapped_column(ForeignKey("run_variant.id"), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaign.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), default="meta")
    scheduled_date: Mapped[date] = mapped_column(Date, nullable=False)
    scheduled_time: Mapped[str | None] = mapped_column(String(8), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="draft")
    channel_account_id: Mapped[str | None] = mapped_column(ForeignKey("shop_channel_account.id"), nullable=True)
    publish_payload: Mapped[dict] = mapped_column(json_type(), default=dict)
    platform_post_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    platform_post_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    publish_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notion_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
