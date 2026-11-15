from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.agents.runtime import AgentsRuntime
from app.data.models import Artifact, Campaign, PipelineRun, StageTask
from app.orchestrator.stage_contracts import all_stage_contracts, get_stage_contract
from app.schemas.contracts import (
    CopyImageBundle,
    PlanningBrief,
    ProductIntake,
    VariantSet,
    VideoBundle,
    VideoScriptPack,
)
from app.services.agent_api_configs import (
    has_resolved_image_config,
    resolve_agent_config,
    resolve_agent_runtime,
    with_fallback_image_config,
)
from app.services.reference_library import build_reference_bundle

TraceEmitter = Callable[..., None]
VariantLibrarySync = Callable[[Session, PipelineRun, StageTask, dict], None]
StageOutputReader = Callable[[Session, str, str], dict | None]
MemoryTracePayload = Callable[[list[dict], dict | None], dict]
SingleVariantSet = Callable[[Session, str, str], VariantSet]
SingleScriptPack = Callable[[Session, str, str], VideoScriptPack]
LatestVideoPayload = Callable[[Session, str, str], dict | None]


@dataclass(frozen=True, slots=True)
class StageExecutionContext:
    db: Session
    run: PipelineRun
    task: StageTask
    runtime: AgentsRuntime
    resolved: dict
    runtime_config: dict
    provider_name: str
    model_name: str
    lead_agent: str
    emit_trace: TraceEmitter
    variant_library_sync: VariantLibrarySync
    get_stage_output: StageOutputReader
    gm_memory_trace_payload: MemoryTracePayload
    utcnow: Callable[[], Any]


@dataclass(frozen=True, slots=True)
class RegenerationExecutionContext:
    db: Session
    run: PipelineRun
    task: StageTask
    runtime: AgentsRuntime
    runtime_config: dict
    provider_name: str
    model_name: str
    variant_id: str
    get_single_variant_set: SingleVariantSet
    get_single_script_pack: SingleScriptPack
    get_stage_output: StageOutputReader
    get_latest_video_payload: LatestVideoPayload


def execute_runtime_stage(context: StageExecutionContext) -> Any:
    contract = get_stage_contract(context.task.stage_name)
    try:
        handler = RUNTIME_HANDLER_DISPATCH[contract.runtime_handler]
    except KeyError as exc:
        raise ValueError(f"unknown runtime handler for stage {context.task.stage_name}: {contract.runtime_handler}") from exc
    return handler(context)


def runtime_stage_names() -> set[str]:
    return {
        contract.stage_name
        for contract in all_stage_contracts()
        if contract.runtime_handler in RUNTIME_HANDLER_DISPATCH
    }


def runtime_handler_names() -> set[str]:
    return set(RUNTIME_HANDLER_DISPATCH)


def execute_regeneration_stage(context: RegenerationExecutionContext) -> Any:
    contract = get_stage_contract(context.task.stage_name)
    try:
        handler = REGENERATION_HANDLER_DISPATCH[contract.runtime_handler]
    except KeyError as exc:
        raise ValueError(f"stage {context.task.stage_name} does not support variant regeneration") from exc
    return handler(context)


def regeneratable_stage_names() -> set[str]:
    return {
        contract.stage_name
        for contract in all_stage_contracts()
        if contract.runtime_handler in REGENERATION_HANDLER_DISPATCH
    }


def regeneration_handler_names() -> set[str]:
    return set(REGENERATION_HANDLER_DISPATCH)


def _payload_operator_instructions(payload: dict) -> str:
    board = payload.get("instruction_board") or {}
    return str(board.get("digest") or "")


def _operator_instructions(context: StageExecutionContext) -> str:
    """Digest of the run's instruction board, plus a trace when one is applied."""
    board = context.task.input_payload.get("instruction_board") or {}
    digest = _payload_operator_instructions(context.task.input_payload)
    if not digest:
        return ""
    context.emit_trace(
        "instruction_board_applied",
        f"Stage {context.task.stage_name} applied instruction board r{board.get('revision')}.",
        payload={
            "revision": board.get("revision"),
            "assets": len(board.get("assets") or []),
            "regions": len(board.get("regions") or []),
            "links": len(board.get("links") or []),
            "snapshot_uri": board.get("snapshot_uri") or "",
        },
    )
    return digest


def _run_intake(context: StageExecutionContext) -> Any:
    return context.runtime.run_intake(
        context.run.id,
        context.task.input_payload,
        provider=context.provider_name,
        model=context.model_name,
        runtime_config=context.runtime_config,
    )


def _run_planning(context: StageExecutionContext) -> Any:
    intake = ProductIntake.model_validate(context.task.input_payload["intake"])
    gm_lessons = context.task.input_payload.get("gm_lessons", [])
    context.emit_trace(
        "gm_memory_applied",
        f"Planning applied {len(gm_lessons)} GM memory entries.",
        payload=context.gm_memory_trace_payload(
            gm_lessons,
            context.task.input_payload.get("research_context") or {},
        ),
    )
    return context.runtime.run_planning(
        context.run.id,
        intake,
        gm_lessons=gm_lessons,
        research_context=context.task.input_payload.get("research_context") or {},
        gm_policy=context.task.input_payload.get("gm_policy", {}),
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        enable_research=bool(context.task.input_payload.get("enable_research")),
        provider=context.provider_name,
        model=context.model_name,
        runtime_config=context.runtime_config,
        operator_instructions=_operator_instructions(context),
    )


def _run_divergence(context: StageExecutionContext) -> Any:
    planning = PlanningBrief.model_validate(context.task.input_payload["planning"])
    return context.runtime.run_divergence(
        context.run.id,
        planning,
        variant_count=context.run.variant_count,
        gm_policy=context.task.input_payload.get("gm_policy", {}),
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        provider=context.provider_name,
        model=context.model_name,
        runtime_config=context.runtime_config,
    )


def _run_copy_image_generation(context: StageExecutionContext) -> Any:
    variants = VariantSet.model_validate(context.task.input_payload["variants"])
    intake_payload = context.task.input_payload.get("intake") or {}
    intake = ProductIntake.model_validate(intake_payload) if intake_payload else None
    reference_bundle = _reference_bundle(context)
    return context.runtime.run_copy_image_generation(
        context.run.id,
        variants,
        intake=intake,
        business_context=context.task.input_payload.get("business_context", {}),
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        market=context.run.market,
        locale=context.run.locale,
        provider=context.provider_name,
        model=context.model_name,
        runtime_config=context.runtime_config,
        historical_references=reference_bundle["images"],
        operator_instructions=_operator_instructions(context),
    )


def _run_video_scripting(context: StageExecutionContext) -> Any:
    variants = VariantSet.model_validate(context.task.input_payload["variants"])
    intake_payload = context.task.input_payload.get("intake") or {}
    intake = ProductIntake.model_validate(intake_payload) if intake_payload else None
    reference_bundle = _reference_bundle(context)
    return context.runtime.run_video_scripting(
        context.run.id,
        variants,
        intake=intake,
        business_context=context.task.input_payload.get("business_context", {}),
        provider=context.provider_name,
        model=context.model_name,
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        pipeline_mode=context.run.pipeline_mode,
        runtime_config=context.runtime_config,
        reference_bundle=reference_bundle,
        planning=context.task.input_payload.get("planning"),
        operator_instructions=_operator_instructions(context),
    )


def _run_storyboard_image_generation(context: StageExecutionContext) -> Any:
    scripts = VideoScriptPack.model_validate(context.task.input_payload["video_scripts"])
    runtime_config = context.runtime_config
    resolved = context.resolved
    if not has_resolved_image_config(resolved):
        image_resolved = resolve_agent_config(
            context.db,
            agent_name="copy_image_agent",
            run_provider=context.run.model_provider,
            run_model=context.run.model_name,
        )
        resolved = with_fallback_image_config(
            resolved,
            image_resolved,
            source="copy_image_agent",
        )
        context.task.metadata_json = {
            **(context.task.metadata_json or {}),
            "storyboard_image_config_source": "copy_image_agent",
            "resolved_api": resolved,
        }
    storyboard_runtime = resolve_agent_runtime(resolved)
    storyboard_image_runtime = dict(storyboard_runtime.get("image") or {})
    storyboard_image_runtime["extra"] = {
        **(storyboard_image_runtime.get("extra") or {}),
        "submit_only": True,
    }
    runtime_config = {
        **runtime_config,
        "image": storyboard_image_runtime,
    }
    reference_bundle = _reference_bundle(context)
    return context.runtime.run_storyboard_image_generation(
        context.run.id,
        scripts,
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        provider=context.provider_name,
        model=context.model_name,
        runtime_config=runtime_config,
        historical_references=reference_bundle["frames"] or reference_bundle["images"],
        intake=ProductIntake.model_validate(context.task.input_payload["intake"]) if context.task.input_payload.get("intake") else None,
        planning=context.task.input_payload.get("planning"),
        operator_instructions=_operator_instructions(context),
    )


def _run_video_generation(context: StageExecutionContext) -> Any:
    scripts = VideoScriptPack.model_validate(context.task.input_payload["video_scripts"])

    def persist_video_asset(video_payload: dict) -> None:
        current_payload = context.task.output_payload or {"videos": []}
        current_videos = [
            item for item in current_payload.get("videos", []) if item.get("variant_id") != video_payload.get("variant_id")
        ]
        current_videos.append(video_payload)
        context.task.output_payload = {"videos": current_videos}
        context.db.add(
            Artifact(
                run_id=context.run.id,
                stage_name=context.task.stage_name,
                artifact_type="generated_video",
                uri=video_payload.get("video_uri"),
                payload=video_payload,
            )
        )
        context.variant_library_sync(context.db, context.run, context.task, {"videos": [video_payload]})
        context.emit_trace(
            "artifact_created",
            f"Video asset submitted for variant {video_payload.get('variant_id')}.",
            payload={
                "variant_id": video_payload.get("variant_id"),
                "asset_type": "video",
                "uri": video_payload.get("video_uri"),
                "external_task_id": video_payload.get("external_task_id"),
                "generation_status": video_payload.get("generation_status"),
            },
        )
        context.run.updated_at = context.utcnow()
        context.db.commit()

    storyboard_output = context.get_stage_output(context.db, context.run.id, "storyboard_image_generation")
    storyboard_frames = (storyboard_output or {}).get("frames", [])
    variant_ids = {script.variant_id for script in scripts.scripts}
    variant_frames = [frame for frame in storyboard_frames if frame.get("variant_id") in variant_ids]
    return context.runtime.run_video_generation(
        context.run.id,
        scripts,
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        provider=context.provider_name,
        model=context.model_name,
        runtime_config=context.runtime_config,
        on_video_asset=persist_video_asset,
        storyboard_frames=variant_frames,
    )


def _run_visual_quality_assessment(context: StageExecutionContext) -> Any:
    variants = VariantSet.model_validate(context.task.input_payload["variants"])
    return context.runtime.run_visual_quality_assessment(
        context.run.id,
        variants,
        copy_images=context.task.input_payload.get("copy_images", {}),
        video_scripts=context.task.input_payload.get("video_scripts", {}),
        storyboards=context.task.input_payload.get("storyboards", {}),
        videos=context.task.input_payload.get("videos", {}),
        intake=context.task.input_payload.get("intake", {}),
        business_context=context.task.input_payload.get("business_context", {}),
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        social_review_contract=context.task.input_payload.get("social_review_contract", {}),
        gm_policy=context.task.input_payload.get("gm_policy", {}),
        provider=context.provider_name,
        model=context.model_name,
        runtime_config=context.runtime_config,
    )


def _run_evaluation_selection(context: StageExecutionContext) -> Any:
    variants = VariantSet.model_validate(context.task.input_payload["variants"])
    copy_bundle = CopyImageBundle.model_validate(context.task.input_payload.get("copy_images", {}))
    script_pack = VideoScriptPack.model_validate(context.task.input_payload.get("video_scripts", {}))
    video_bundle = VideoBundle.model_validate(context.task.input_payload.get("videos", {}))
    return context.runtime.run_evaluation_selection(
        context.run.id,
        variants,
        copy_bundle,
        script_pack,
        video_bundle,
        context.task.input_payload.get("visual_quality", {}),
        provider=context.provider_name,
        model=context.model_name,
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        pipeline_mode=context.run.pipeline_mode,
        gm_policy=context.task.input_payload.get("gm_policy", {}),
        runtime_config=context.runtime_config,
    )


def _regenerate_copy_image_generation(context: RegenerationExecutionContext) -> Any:
    intake_payload = context.task.input_payload.get("intake") or {}
    intake = ProductIntake.model_validate(intake_payload) if intake_payload else None
    reference_bundle = _reference_bundle(context)
    return context.runtime.run_copy_image_generation(
        context.run.id,
        context.get_single_variant_set(context.db, context.run.id, context.variant_id),
        intake=intake,
        business_context=context.task.input_payload.get("business_context", {}),
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        market=context.run.market,
        locale=context.run.locale,
        provider=context.provider_name,
        model=context.model_name,
        runtime_config=context.runtime_config,
        historical_references=reference_bundle["images"],
        operator_instructions=_payload_operator_instructions(context.task.input_payload),
    )


def _regenerate_video_scripting(context: RegenerationExecutionContext) -> Any:
    intake_payload = context.task.input_payload.get("intake") or {}
    intake = ProductIntake.model_validate(intake_payload) if intake_payload else None
    reference_bundle = _reference_bundle(context)
    return context.runtime.run_video_scripting(
        context.run.id,
        context.get_single_variant_set(context.db, context.run.id, context.variant_id),
        intake=intake,
        business_context=context.task.input_payload.get("business_context", {}),
        provider=context.provider_name,
        model=context.model_name,
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        pipeline_mode=context.run.pipeline_mode,
        runtime_config=context.runtime_config,
        reference_bundle=reference_bundle,
        planning=context.task.input_payload.get("planning"),
        operator_instructions=_payload_operator_instructions(context.task.input_payload),
    )


def _regenerate_storyboard_image_generation(context: RegenerationExecutionContext) -> Any:
    reference_bundle = _reference_bundle(context)
    return context.runtime.run_storyboard_image_generation(
        context.run.id,
        context.get_single_script_pack(context.db, context.run.id, context.variant_id),
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        provider=context.provider_name,
        model=context.model_name,
        runtime_config=context.runtime_config,
        historical_references=reference_bundle["frames"] or reference_bundle["images"],
        intake=ProductIntake.model_validate(context.task.input_payload["intake"]) if context.task.input_payload.get("intake") else None,
        planning=context.task.input_payload.get("planning"),
        operator_instructions=_payload_operator_instructions(context.task.input_payload),
    )


def _regenerate_video_generation(context: RegenerationExecutionContext) -> Any:
    storyboard_output = context.get_stage_output(context.db, context.run.id, "storyboard_image_generation")
    storyboard_frames = (storyboard_output or {}).get("frames", [])
    variant_frames = [frame for frame in storyboard_frames if frame.get("variant_id") == context.variant_id]
    resume_payload = context.get_latest_video_payload(context.db, context.run.id, context.variant_id)
    runtime_config = (
        {**context.runtime_config, "resume_video_payload": resume_payload}
        if resume_payload
        else context.runtime_config
    )
    return context.runtime.run_video_generation(
        context.run.id,
        context.get_single_script_pack(context.db, context.run.id, context.variant_id),
        creative_specs=context.task.input_payload.get("creative_specs", {}),
        provider=context.provider_name,
        model=context.model_name,
        runtime_config=runtime_config,
        storyboard_frames=variant_frames,
    )


def _reference_bundle(context: Any) -> dict:
    campaign = context.db.get(Campaign, context.run.campaign_id)
    return build_reference_bundle(
        context.db,
        product_code=context.run.product_code,
        channel=campaign.channel if campaign else "",
        limit_images=2,
        limit_frames=2,
    )


RUNTIME_HANDLER_DISPATCH: dict[str, Callable[[StageExecutionContext], Any]] = {
    "run_intake": _run_intake,
    "run_planning": _run_planning,
    "run_divergence": _run_divergence,
    "run_copy_image_generation": _run_copy_image_generation,
    "run_video_scripting": _run_video_scripting,
    "run_storyboard_image_generation": _run_storyboard_image_generation,
    "run_video_generation": _run_video_generation,
    "run_visual_quality_assessment": _run_visual_quality_assessment,
    "run_evaluation_selection": _run_evaluation_selection,
}


REGENERATION_HANDLER_DISPATCH: dict[str, Callable[[RegenerationExecutionContext], Any]] = {
    "run_copy_image_generation": _regenerate_copy_image_generation,
    "run_video_scripting": _regenerate_video_scripting,
    "run_storyboard_image_generation": _regenerate_storyboard_image_generation,
    "run_video_generation": _regenerate_video_generation,
}
