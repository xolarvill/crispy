from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.core.config import get_settings
from app.data.models import (
    RunStatus,
    StageTask,
    TaskStatus,
    VariantAsset,
    utcnow as model_utcnow,
)
from app.data.session import SessionLocal
from app.orchestrator.state_machine import should_auto_approve
from app.services.marketplace_qa import is_marketplace_main_image
from app.services.runs import (
    _build_task_input,
    add_agent_trace_event,
    auto_approve_stage,
    execute_stage_task,
    get_run,
    refresh_async_assets,
    regenerate_variant_assets,
    select_next_queued_task,
)

logger = logging.getLogger(__name__)


@dataclass
class RunningTaskInfo:
    task_id: str
    run_id: str
    stage_name: str
    attempt: int
    started_at: datetime


class PipelineWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._concurrency = self.settings.worker_concurrency
        self._poll_interval = max(0.2, self.settings.polling_interval_seconds)
        self._video_interval = max(5.0, self.settings.video_polling_interval_seconds)

        self._stop_event = asyncio.Event()
        self._worker_tasks: list[asyncio.Task] = []
        self._video_poller_task: asyncio.Task | None = None

        # In-memory state for queue visibility
        self._started_at: datetime | None = None
        self._running_tasks: dict[str, RunningTaskInfo] = {}
        self._total_completed: int = 0
        self._total_failed: int = 0
        self._video_poller_last_run: datetime | None = None
        self._video_poller_success: bool = True

    # ── Public API ──────────────────────────────────────────

    async def start(self) -> None:
        if self._worker_tasks:
            return
        self._stop_event.clear()
        self._started_at = datetime.now(UTC)

        self._recover_orphaned_tasks()

        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(), name=f"crispy-worker-{i}")
            for i in range(self._concurrency)
        ]
        self._video_poller_task = asyncio.create_task(
            self._video_poller_loop(), name="crispy-video-poller"
        )
        logger.info(
            "PipelineWorker started | concurrency=%d | poll=%.1fs | video_poll=%.1fs",
            self._concurrency,
            self._poll_interval,
            self._video_interval,
        )

    async def stop(self) -> None:
        if not self._worker_tasks:
            return
        self._stop_event.set()
        tasks: list[asyncio.Task] = [*self._worker_tasks]
        if self._video_poller_task:
            tasks.append(self._video_poller_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        self._worker_tasks.clear()
        self._video_poller_task = None
        self._started_at = None
        logger.info("PipelineWorker stopped")

    def get_queue_status(self) -> dict[str, Any]:
        db = SessionLocal()
        try:
            queued = (
                db.scalar(
                    select(func.count()).select_from(StageTask).where(
                        StageTask.status == TaskStatus.QUEUED.value
                    )
                )
                or 0
            )

            rows = (
                db.execute(
                    select(StageTask.stage_name, func.count())
                    .where(StageTask.status == TaskStatus.QUEUED.value)
                    .group_by(StageTask.stage_name)
                )
                .all()
            )
            by_stage = {row[0]: row[1] for row in rows}

            status_rows = (
                db.execute(
                    select(StageTask.status, func.count()).group_by(StageTask.status)
                )
                .all()
            )
            by_status = {row[0]: row[1] for row in status_rows}

            return {
                "total_queued": queued,
                "queued_by_stage": by_stage,
                "status_counts": by_status,
                "currently_running": len(self._running_tasks),
            }
        finally:
            db.close()

    def get_running_tasks(self) -> list[dict]:
        now = datetime.now(UTC)
        return [
            {
                "task_id": info.task_id,
                "run_id": info.run_id,
                "stage_name": info.stage_name,
                "attempt": info.attempt,
                "started_at": info.started_at.isoformat(),
                "duration_seconds": round(
                    (now - info.started_at).total_seconds(), 2
                ),
            }
            for info in self._running_tasks.values()
        ]

    def get_health(self) -> dict:
        uptime = (
            (datetime.now(UTC) - self._started_at).total_seconds()
            if self._started_at
            else 0.0
        )
        return {
            "status": "running" if self._worker_tasks else "stopped",
            "uptime_seconds": round(uptime, 2),
            "concurrency": self._concurrency,
            "active_workers": len(self._running_tasks),
            "total_completed": self._total_completed,
            "total_failed": self._total_failed,
            "video_poller_last_run": (
                self._video_poller_last_run.isoformat()
                if self._video_poller_last_run
                else None
            ),
            "video_poller_ok": self._video_poller_success,
        }

    # ── Internal loops ──────────────────────────────────────

    async def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                ran = await asyncio.to_thread(self._execute_one_task)
                if not ran:
                    await self._sleep_interruptible(self._poll_interval)
            except Exception:
                logger.exception("Worker loop error")
                await self._sleep_interruptible(self._poll_interval)

    async def _video_poller_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._sleep_interruptible(self._video_interval)
            if self._stop_event.is_set():
                break
            try:
                await asyncio.to_thread(self._poll_all_video_assets)
                self._video_poller_success = True
            except Exception:
                logger.exception("Video poller error")
                self._video_poller_success = False
            finally:
                self._video_poller_last_run = datetime.now(UTC)

    async def _sleep_interruptible(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ── Synchronous helpers (run in thread pool) ────────────

    def _execute_one_task(self) -> bool:
        db = SessionLocal()
        try:
            task = select_next_queued_task(db)
            if not task:
                db.rollback()
                return False

            run = get_run(db, task.run_id)
            task.input_payload = _build_task_input(db, run, task)
            task.attempt = (task.attempt or 0) + 1
            run.status = RunStatus.RUNNING.value
            run.current_stage = task.stage_name
            run.updated_at = model_utcnow()
            db.flush()

            info = RunningTaskInfo(
                task_id=task.id,
                run_id=run.id,
                stage_name=task.stage_name,
                attempt=task.attempt,
                started_at=task.started_at or model_utcnow(),
            )
            self._running_tasks[task.id] = info

            try:
                execute_stage_task(db, task, run)

                if task.status == TaskStatus.WAITING_REVIEW.value:
                    self._total_completed += 1
                    # Auto-approval check
                    if should_auto_approve(run.approval_mode, task.stage_name) or self._should_marketplace_auto_approve(run, task):
                        auto_advance = True
                        if task.stage_name == "storyboard_image_generation" and self._has_pending_storyboard_assets(task):
                            auto_advance = False
                        if task.stage_name == "copy_image_generation" and self._has_pending_copy_image_assets(task):
                            auto_advance = False
                        if task.stage_name == "visual_quality_assessment" and run.approval_mode == "full_auto":
                            auto_advance = self._full_auto_visual_qa_regen(db, run, task)
                        # A board question is meant for a human; auto-approval
                        # would move the run past it before anyone saw it.
                        questions = (task.metadata_json or {}).get("board_questions") or []
                        if questions and run.approval_mode != "full_auto":
                            auto_advance = False
                            add_agent_trace_event(
                                db,
                                run_id=run.id,
                                stage_task_id=task.id,
                                stage_name=task.stage_name,
                                agent_name=(task.metadata_json or {}).get("agent_name") or "",
                                event_type="board_questions_pending",
                                message=(
                                    f"{task.stage_name} left {len(questions)} question(s) on the board; "
                                    "auto-approval is paused."
                                ),
                                payload={"questions": questions[:5]},
                            )
                        if auto_advance:
                            auto_approve_stage(db, run.id, task.stage_name)
                            logger.info(
                                "Auto-approved %s for run %s (mode=%s)",
                                task.stage_name, run.id, run.approval_mode,
                            )
                elif task.status == TaskStatus.FAILED.value:
                    self._total_failed += 1
                db.commit()
                return True
            except Exception:
                db.rollback()
                raise
            finally:
                self._running_tasks.pop(task.id, None)
        except Exception:
            db.rollback()
            logger.exception("_execute_one_task failed")
            return False
        finally:
            db.close()

    def _should_marketplace_auto_approve(self, run, task: StageTask) -> bool:
        if run.approval_mode != "semi_auto" or not is_marketplace_main_image(run.creative_specs):
            return False
        if task.stage_name == "copy_image_generation":
            return True
        if task.stage_name == "visual_quality_assessment":
            summaries = (task.output_payload or {}).get("variant_summaries") or []
            return bool(summaries) and all(
                isinstance(item, dict) and item.get("export_ready") is True
                for item in summaries
            )
        return False

    def _has_pending_storyboard_assets(self, task: StageTask) -> bool:
        frames = (task.output_payload or {}).get("frames") or []
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            for item in [frame, *((frame.get("candidate_frames") or []) if isinstance(frame.get("candidate_frames"), list) else [])]:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("generation_status") or "").lower()
                if item.get("external_task_id") and status in {"", "submitted", "queued", "pending", "processing", "running"}:
                    return True
        return False

    def _has_pending_copy_image_assets(self, task: StageTask) -> bool:
        for item in (task.output_payload or {}).get("image_assets", []) or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("generation_status") or "").lower()
            if item.get("external_task_id") and status in {"", "submitted", "queued", "pending", "processing", "running"}:
                return True
        return False

    def _poll_all_video_assets(self) -> None:
        db = SessionLocal()
        try:
            assets = db.scalars(
                select(VariantAsset).where(VariantAsset.asset_type.in_(["video", "storyboard_frame", "image"]))
            ).all()

            pending_run_ids: set[str] = set()
            for asset in assets:
                payload = asset.payload or {}
                status = str(payload.get("generation_status", "")).lower()
                segment_pending = any(
                    isinstance(segment, dict)
                    and segment.get("external_task_id")
                    and str(segment.get("generation_status") or "").lower() in {"", "submitted", "queued", "pending", "processing", "running"}
                    for segment in (payload.get("segments") or [])
                )
                if segment_pending or (
                    payload.get("external_task_id") and status in {"", "submitted", "queued", "pending", "processing", "running"}
                ):
                    pending_run_ids.add(asset.run_id)

            for run_id in pending_run_ids:
                try:
                    refresh_async_assets(db, run_id)
                except Exception:
                    logger.exception("Video poll failed for run %s", run_id)

            db.commit()
        finally:
            db.close()

    def _full_auto_visual_qa_regen(self, db, run, task) -> bool:
        """Full-auto regeneration for visual_qa. Returns True if safe to auto-approve."""
        output_payload = task.output_payload or {}
        summaries = output_payload.get("variant_summaries") or []
        pending_variants = [
            s for s in summaries
            if isinstance(s, dict) and (
                s.get("recommended_action") == "wait_for_asset" or str(s.get("qa_status") or "").lower() == "pending"
            )
        ]
        if pending_variants:
            metadata = dict(task.metadata_json or {})
            task.metadata_json = {
                **metadata,
                "full_auto_visual_qa_pending_assets": True,
            }
            logger.info(
                "Full-auto visual_qa: pending assets still exist for run %s; holding for refresh",
                run.id,
            )
            return False
        review_blockers = [
            s for s in summaries
            if isinstance(s, dict)
            and "visual_qa_model_review_unavailable" in {str(issue) for issue in (s.get("issues") or [])}
        ]
        if review_blockers:
            metadata = dict(task.metadata_json or {})
            task.metadata_json = {
                **metadata,
                "full_auto_visual_qa_model_review_unavailable": True,
            }
            logger.info(
                "Full-auto visual_qa: model review unavailable for run %s; holding for manual review",
                run.id,
            )
            return False
        regen_variants = [
            s for s in summaries
            if isinstance(s, dict) and s.get("recommended_action") == "request_regeneration"
        ]
        if not regen_variants:
            return True

        metadata = dict(task.metadata_json or {})
        regen_cycles = int(metadata.get("full_auto_visual_qa_regen_cycles") or 0)

        if regen_cycles >= 2:
            logger.warning(
                "Full-auto visual_qa regen limit reached for run %s; advancing anyway",
                run.id,
            )
            task.metadata_json = {**metadata, "full_auto_visual_qa_regen_limit_reached": True}
            return True

        regen_cycles += 1
        task.metadata_json = {
            **metadata,
            "full_auto_visual_qa_regen_cycles": regen_cycles,
            "full_auto_visual_qa_regen_limit_reached": False,
        }
        regenerated = 0
        for summary in regen_variants[:3]:
            variant_id = summary.get("variant_id")
            if not variant_id:
                continue
            try:
                regenerate_variant_assets(
                    db,
                    run_id=run.id,
                    variant_id=str(variant_id),
                    reason=f"full_auto_visual_qa_cycle_{regen_cycles}",
                )
                regenerated += 1
            except Exception:
                logger.exception(
                    "Full-auto regen failed for variant %s in run %s",
                    variant_id, run.id,
                )

        if regenerated > 0:
            task.status = TaskStatus.QUEUED.value
            task.retry_at = None
            task.priority = 1
            logger.info(
                "Full-auto visual_qa: regenerated %d variants for run %s; re-queued visual_qa (cycle %d)",
                regenerated, run.id, regen_cycles,
            )
            return False

        return True

    def _recover_orphaned_tasks(self) -> None:
        """Reset any RUNNING tasks that were abandoned by a previous crash."""
        db = SessionLocal()
        try:
            orphaned = (
                db.scalars(
                    select(StageTask).where(
                        StageTask.status == TaskStatus.RUNNING.value
                    )
                )
                .all()
            )
            for task in orphaned:
                task.status = TaskStatus.QUEUED.value
                task.retry_at = None
                logger.warning(
                    "Recovered orphaned task %s (%s) — reset to QUEUED",
                    task.id,
                    task.stage_name,
                )
            if orphaned:
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to recover orphaned tasks")
        finally:
            db.close()


worker = PipelineWorker()
