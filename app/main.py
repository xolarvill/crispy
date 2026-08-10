from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.agents.personas import ensure_default_personas
from app.api.routes import router
from app.core.config import get_settings
from app.data.models import Workspace
from app.data.session import SessionLocal, init_db
from app.orchestrator.research_worker import research_worker
from app.orchestrator.worker import worker

logger = logging.getLogger(__name__)


async def _auto_sync_loop() -> None:
    """Check all workspaces every 60s and trigger auto-sync for configured platforms."""
    while True:
        await asyncio.sleep(60)
        db = SessionLocal()
        try:
            workspaces = db.scalars(select(Workspace)).all()
            now = datetime.now(UTC)
            for ws in workspaces:
                if ws.shopify_auto_sync_minutes > 0:
                    last = ws.shopify_last_sync_at
                    if last is None or (now - last).total_seconds() >= ws.shopify_auto_sync_minutes * 60:
                        try:
                            from app.integrations.sync_service import sync_shopify

                            project = ws.projects[0] if ws.projects else None
                            if project:
                                await sync_shopify(db, workspace_name=ws.name, project_name=project.name)
                                ws.shopify_last_sync_at = now
                                db.commit()
                                logger.info("Auto-sync shopify for %s OK", ws.name)
                        except Exception as exc:
                            logger.warning("Auto-sync shopify for %s failed: %s", ws.name, exc)
                if ws.meta_auto_sync_minutes > 0:
                    last = ws.meta_last_sync_at
                    if last is None or (now - last).total_seconds() >= ws.meta_auto_sync_minutes * 60:
                        try:
                            from app.integrations.sync_service import sync_meta

                            project = ws.projects[0] if ws.projects else None
                            if project:
                                await sync_meta(db, workspace_name=ws.name, project_name=project.name)
                                ws.meta_last_sync_at = now
                                db.commit()
                                logger.info("Auto-sync meta for %s OK", ws.name)
                        except Exception as exc:
                            logger.warning("Auto-sync meta for %s failed: %s", ws.name, exc)
        except Exception as exc:
            logger.exception("Auto-sync loop error: %s", exc)
        finally:
            db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    init_db()
    ensure_default_personas()
    from app.data.session import backup_database
    backup_path = backup_database()
    if backup_path:
        print(f"[crispy] Database backed up to {backup_path}")
    if settings.enable_worker:
        await worker.start()
        await research_worker.start()
    auto_sync_task = asyncio.create_task(_auto_sync_loop())
    try:
        yield
    finally:
        auto_sync_task.cancel()
        if settings.enable_worker:
            await research_worker.stop()
            await worker.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(router)
    return app


app = create_app()
