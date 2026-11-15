from __future__ import annotations

import json
import logging

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.data.models import (
    Campaign,
    GmMemory,
    PerformanceSnapshot,
    PipelineRun,
    Product,
    StageName,
    StageTask,
    TaskFailureCategory,
)
from app.orchestrator.stage_contracts import get_stage_contract
from app.services.boards import BOARD_STAGE_SCOPE_STAGES, board_context_for_run
from app.services.creative_specs import get_social_review_contract
from app.services.execution_memory import append_execution_memory_payload, resolve_execution_memory
from app.services.gm_evolution import resolve_active_gm_policy
from app.services.gm_memory import memory_dirty_reasons, memory_is_strategy_safe
from app.services.memory_selection import normalize_memory_selection
from app.services.shop_analysis import RESEARCH_SOURCE_TYPES

logger = logging.getLogger(__name__)


STAGE_OUTPUT_INPUTS: dict[str, dict[str, str]] = {
    StageName.PLANNING.value: {"intake": StageName.INTAKE.value},
    StageName.DIVERGENCE.value: {"planning": StageName.PLANNING.value},
    StageName.COPY_IMAGE_GENERATION.value: {
        "variants": StageName.DIVERGENCE.value,
        "intake": StageName.INTAKE.value,
    },
    StageName.VIDEO_SCRIPTING.value: {
        "variants": StageName.DIVERGENCE.value,
        "intake": StageName.INTAKE.value,
        "planning": StageName.PLANNING.value,
    },
    StageName.STORYBOARD_IMAGE_GENERATION.value: {
        "video_scripts": StageName.VIDEO_SCRIPTING.value,
        "intake": StageName.INTAKE.value,
        "planning": StageName.PLANNING.value,
    },
    StageName.VIDEO_GENERATION.value: {
        "video_scripts": StageName.VIDEO_SCRIPTING.value,
        "storyboards": StageName.STORYBOARD_IMAGE_GENERATION.value,
    },
    StageName.VISUAL_QUALITY_ASSESSMENT.value: {
        "variants": StageName.DIVERGENCE.value,
        "intake": StageName.INTAKE.value,
        "copy_images": StageName.COPY_IMAGE_GENERATION.value,
        "video_scripts": StageName.VIDEO_SCRIPTING.value,
        "storyboards": StageName.STORYBOARD_IMAGE_GENERATION.value,
        "videos": StageName.VIDEO_GENERATION.value,
    },
    StageName.EVALUATION_SELECTION.value: {
        "variants": StageName.DIVERGENCE.value,
        "copy_images": StageName.COPY_IMAGE_GENERATION.value,
        "video_scripts": StageName.VIDEO_SCRIPTING.value,
        "videos": StageName.VIDEO_GENERATION.value,
        "visual_quality": StageName.VISUAL_QUALITY_ASSESSMENT.value,
    },
}


def stage_input_keys_for_contract(stage_name: str) -> set[str]:
    contract = get_stage_contract(stage_name)
    return set(_base_input_keys()) | set(STAGE_OUTPUT_INPUTS.get(stage_name, {})) | set(contract.optional_inputs)


def stage_output_optional(db: Session, run_id: str, stage_name: str) -> dict:
    task = db.scalar(select(StageTask).where(StageTask.run_id == run_id, StageTask.stage_name == stage_name))
    if not task:
        return {}
    return task.output_payload or {}


def build_task_input(db: Session, run: PipelineRun, task: StageTask) -> dict:
    contract = get_stage_contract(task.stage_name)
    product = db.get(Product, run.product_id)
    campaign = db.get(Campaign, run.campaign_id)
    gm_policy = resolve_active_gm_policy(db, run, stage_name=task.stage_name)
    payload = {
        "run_id": run.id,
        "product_name": product.name if product else "unknown_product",
        "channel": campaign.channel if campaign else "",
        "context": run.context_json or {},
        "market": run.market,
        "locale": run.locale,
        "product_code": run.product_code,
        "industry_code": run.industry_code,
        "pipeline_mode": run.pipeline_mode,
        "creative_preset": run.creative_preset,
        "creative_specs": run.creative_specs or {},
        "social_review_contract": get_social_review_contract(
            campaign.channel if campaign else "",
            run.pipeline_mode,
            run.creative_specs or {},
        ),
        "variant_count": run.variant_count,
        "enable_research": run.enable_research,
        "manual_research_brief": run.manual_research_brief or "",
        "business_context": run.business_context or {},
        "category_tags": run.category_tags or [],
        "gm_policy": gm_policy,
    }
    for input_name, source_stage in STAGE_OUTPUT_INPUTS.get(task.stage_name, {}).items():
        payload[input_name] = stage_output_optional(db, run.id, source_stage)
    if task.stage_name in BOARD_STAGE_SCOPE_STAGES:
        board_context = board_context_for_run(db, run)
        if board_context:
            # Pinned into input_payload when the task is claimed: editing the
            # board later must not silently change a stage that already ran.
            payload["instruction_board"] = board_context
    if task.stage_name == StageName.PLANNING.value:
        from app.services.research_context import build_research_context

        payload["gm_lessons"] = recent_gm_lessons(db, run) + analytics_insights(db, run)
        payload["research_context"] = build_research_context(
            db,
            project_id=run.project_id,
            shop_id=run.workspace_id,
            industry_code=run.industry_code,
            memory_selection=(run.context_json or {}).get("memory_selection"),
        )
    _ensure_contract_inputs_present(contract.stage_name, contract.required_inputs, payload)
    if task.rejected_at or task.failure_category == TaskFailureCategory.HUMAN_REJECT.value:
        payload = append_execution_memory_payload(
            payload,
            bucket="run",
            memory=resolve_execution_memory(db, run_id=run.id, stage_name=task.stage_name),
        )
    return payload


def recent_gm_lessons(db: Session, run: PipelineRun, limit: int = 5) -> list[dict]:
    memory_selection = normalize_memory_selection((run.context_json or {}).get("memory_selection"))
    if memory_selection["mode"] == "none":
        return []

    product_rows = db.scalars(
        select(GmMemory)
        .where(
            GmMemory.project_id == run.project_id,
            GmMemory.memory_scope == "product",
            GmMemory.product_code == run.product_code,
            GmMemory.status == "active",
        )
        .order_by(desc(GmMemory.score_hint), desc(GmMemory.created_at))
        .limit(20)
    ).all()
    industry_rows = db.scalars(
        select(GmMemory)
        .where(
            GmMemory.project_id == run.project_id,
            GmMemory.memory_scope == "industry",
            GmMemory.industry_code == run.industry_code,
            GmMemory.status == "active",
        )
        .order_by(desc(GmMemory.score_hint), desc(GmMemory.created_at))
        .limit(20)
    ).all()
    shop_candidates = db.scalars(
        select(GmMemory)
        .where(
            GmMemory.memory_scope == "shop",
            GmMemory.source_type.in_([*RESEARCH_SOURCE_TYPES, "shopify_sync", "meta_sync"]),
            GmMemory.status == "active",
        )
        .order_by(desc(GmMemory.score_hint), desc(GmMemory.created_at))
        .limit(50)
    ).all()
    shop_rows = [
        row for row in shop_candidates
        if (row.content or {}).get("shop_id") == run.workspace_id
    ][:5]
    product_rows = sorted(product_rows, key=_gm_memory_priority)[:10]
    industry_rows = sorted(industry_rows, key=_gm_memory_priority)[:10]
    shop_rows = sorted(shop_rows, key=_gm_memory_priority)[:5]
    safe_candidates = {
        row.id: row
        for row in [*product_rows, *shop_rows, *industry_rows]
        if memory_is_strategy_safe(row)
    }

    if memory_selection["mode"] == "manual":
        selected_rows = [
            safe_candidates[memory_id]
            for memory_id in memory_selection["include_ids"]
            if memory_id in safe_candidates
        ]
    else:
        exclude_ids = set(memory_selection["exclude_ids"])
        default_rows = [
            row
            for row in [*product_rows[:3], *shop_rows[:3], *industry_rows[:2]]
            if row.id in safe_candidates and row.id not in exclude_ids
        ]
        manual_additions = [
            safe_candidates[memory_id]
            for memory_id in memory_selection["include_ids"]
            if memory_id in safe_candidates and memory_id not in exclude_ids
        ]
        selected_rows = [*manual_additions, *default_rows]

    merged: list[dict] = []
    seen_fingerprints: set[str] = set()
    seen_ids: set[str] = set()
    for row in selected_rows:
        if row.id in seen_ids:
            continue
        seen_ids.add(row.id)
        payload = _gm_memory_payload(row)
        fingerprint = f"{payload['memory_scope']}|{payload['product_code']}|{payload['industry_code']}|{json.dumps(payload['content'], sort_keys=True)}"
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        merged.append(payload)
        if len(merged) >= limit:
            break
    return merged


def _gm_memory_payload(row: GmMemory) -> dict:
    return {
        "id": row.id,
        "memory_scope": row.memory_scope,
        "product_code": row.product_code,
        "industry_code": row.industry_code,
        "source_type": row.source_type,
        "memory_type": row.memory_type,
        "status": row.status,
        "pinned": bool(row.pinned),
        "dirty_reasons": memory_dirty_reasons(row),
        "score_hint": row.score_hint,
        "content": row.content or {},
    }


def analytics_insights(db: Session, run: PipelineRun) -> list[dict]:
    from app.analytics import AdAnalyzer, CreativeDecisionAnalyzer, ProductAnalyzer

    insights: list[dict] = []
    if run.product_code:
        try:
            pa = ProductAnalyzer(db, run.project_id)
            vel = pa.analyze_product_sales_velocity(run.product_code)
            if not vel.insufficient_data:
                insights.append({
                    "memory_scope": "analytics",
                    "source_type": "product_sales_velocity",
                    "product_code": run.product_code,
                    "content": vel.model_dump(),
                })
            contrib = pa.analyze_product_contribution([run.product_code])
            if not contrib.insufficient_data:
                insights.append({
                    "memory_scope": "analytics",
                    "source_type": "product_contribution",
                    "product_code": run.product_code,
                    "content": contrib.model_dump(),
                })
        except Exception as exc:
            logger.debug("product analytics insight skipped: %s", exc)

    try:
        aa = AdAnalyzer(db, run.project_id)
        snapshots = db.scalars(
            select(PerformanceSnapshot)
            .where(PerformanceSnapshot.project_id == run.project_id)
            .order_by(desc(PerformanceSnapshot.created_at))
            .limit(50)
        ).all()
        creative_keys = list({s.creative_key for s in snapshots if s.creative_key})
        for ck in creative_keys[:3]:
            fatigue = aa.analyze_creative_fatigue(ck)
            if not fatigue.insufficient_data:
                insights.append({
                    "memory_scope": "analytics",
                    "source_type": "creative_fatigue",
                    "creative_key": ck,
                    "product_code": run.product_code,
                    "content": fatigue.model_dump(),
                })
        if len(creative_keys) >= 2:
            comp = aa.compare_creatives(creative_keys[:5])
            if not comp.insufficient_data:
                insights.append({
                    "memory_scope": "analytics",
                    "source_type": "creative_compare",
                    "product_code": run.product_code,
                    "content": comp.model_dump(),
                })
    except Exception as exc:
        logger.debug("ad analytics insight skipped: %s", exc)

    try:
        creative_decisions = CreativeDecisionAnalyzer(db, run.project_id).decision_report(
            product_code=run.product_code or None,
            window_days=30,
        )
        decision_content = {
            "baseline": creative_decisions.get("baseline") or {},
            "promote": creative_decisions.get("promote", [])[:3],
            "retire": creative_decisions.get("retire", [])[:3],
            "needs_test": creative_decisions.get("needs_test", [])[:3],
            "next_generation": creative_decisions.get("next_generation") or {},
            "attribution_summary": creative_decisions.get("attribution_summary") or {},
            "unmatched_count": len(creative_decisions.get("unmatched", [])),
            "summary": "Creative decision attribution suggests which ideas to promote, retire, or test further.",
        }
        if decision_content["promote"] or decision_content["retire"] or decision_content["needs_test"]:
            insights.append({
                "memory_scope": "analytics",
                "source_type": "creative_decision_attribution",
                "product_code": run.product_code,
                "content": decision_content,
            })
    except Exception as exc:
        logger.debug("creative decision insight skipped: %s", exc)

    return insights


def _base_input_keys() -> tuple[str, ...]:
    return (
        "run_id",
        "product_name",
        "channel",
        "context",
        "market",
        "locale",
        "product_code",
        "industry_code",
        "pipeline_mode",
        "creative_preset",
        "creative_specs",
        "social_review_contract",
        "variant_count",
        "enable_research",
        "manual_research_brief",
        "business_context",
        "category_tags",
        "gm_policy",
    )


def _ensure_contract_inputs_present(stage_name: str, required_inputs: tuple[str, ...], payload: dict) -> None:
    missing = [input_name for input_name in required_inputs if input_name not in payload]
    if missing:
        raise ValueError(f"stage input contract mismatch for {stage_name}: missing {', '.join(missing)}")


def _gm_memory_priority(row: GmMemory) -> tuple[int, int, float, float]:
    return (
        0 if row.memory_type == "summary" else 1,
        -int(bool(row.pinned)),
        -(row.score_hint or 0),
        -(row.created_at.timestamp() if row.created_at else 0),
    )
