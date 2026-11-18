"""Local video editing API; all mutations use the shared editing service."""
from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import Field
from sqlalchemy.orm import Session

from app.data.session import get_db
from app.schemas.video import Strict, VideoOperation, VideoWrite, VideoDocument
from app.services import video_editing as service
from app.services.run_media import run_media_summaries
from app.services import video_rendering as rendering
from app.services.video_review import ReviewPlatform, review_render
from app.services.video_tools import call_tool
from app.data.models import VideoRender
from sqlalchemy import select
from fastapi.responses import FileResponse

router = APIRouter()


class CreateProject(Strict):
    title: str = Field(default="Video edit", min_length=1, max_length=160)
    request_id: str = Field(min_length=1, max_length=128)


class RevisionRequest(Strict):
    expected_revision: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=128)


class ImportSource(RevisionRequest):
    uri: str = Field(min_length=1, max_length=4096)


class Operations(RevisionRequest):
    operations: list[VideoOperation] = Field(min_length=1, max_length=120)
    summary: str = Field(default="Edit timeline", max_length=300)


class Restore(RevisionRequest):
    revision: int = Field(ge=0)


class RenderReview(Strict):
    platform: ReviewPlatform | None = None


def run_action(action):
    try:
        return action()
    except service.VideoConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (service.VideoError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/video-editing/capabilities")
def capabilities():
    return service.capabilities()


@router.get("/runs/{run_id}/video-projects")
def projects(run_id: str, db: Session = Depends(get_db)):
    return service.list_projects(db, run_id)


@router.post("/runs/{run_id}/video-projects")
def create(run_id: str, body: CreateProject, db: Session = Depends(get_db)):
    return run_action(lambda: service.payload(service.create_project(db, run_id, body.title, body.request_id)))


@router.get("/video-projects/{project_id}")
def read(project_id: str, db: Session = Depends(get_db)):
    return run_action(lambda: service.payload(service.get_project(db, project_id)))


@router.put("/video-projects/{project_id}")
def save(project_id: str, body: VideoWrite, db: Session = Depends(get_db)):
    return run_action(lambda: service.payload(service.save_project(db, project_id, body.document,
        body.expected_revision, body.request_id, "human", body.summary)))


@router.post("/video-projects/{project_id}/operations")
def operations(project_id: str, body: Operations, db: Session = Depends(get_db)):
    def action():
        return service.payload(service.edit_operations(db, project_id, body.operations, body.expected_revision,
                                                       body.request_id, "human", body.summary))
    return run_action(action)


@router.get("/video-projects/{project_id}/history")
def history(project_id: str, db: Session = Depends(get_db)):
    return run_action(lambda: service.history(db, project_id))


@router.post("/video-projects/{project_id}/restore")
def restore(project_id: str, body: Restore, db: Session = Depends(get_db)):
    return run_action(lambda: service.payload(service.restore(db, project_id, body.revision,
        body.expected_revision, body.request_id, "human")))


@router.get("/video-projects/{project_id}/run-assets")
def run_assets(project_id: str, db: Session = Depends(get_db)):
    def action():
        project = service.get_project(db, project_id)
        return run_media_summaries(db, [project.run_id], limit=None).get(project.run_id, {}).get("media_previews", [])
    return run_action(action)


@router.post("/video-projects/{project_id}/sources/from-run")
def import_run_source(project_id: str, body: ImportSource, db: Session = Depends(get_db)):
    def action():
        project = service.get_project(db, project_id)
        path = service.resolve_run_source(db, project, body.uri)
        updated, source_id = service.import_source_with_id(db, project_id, path, path.name,
            body.expected_revision, body.request_id, "human")
        return {**service.payload(updated), "imported_source_id": source_id}
    return run_action(action)


@router.post("/video-projects/{project_id}/sources")
async def upload(project_id: str, file: UploadFile = File(...), expected_revision: int = Form(...),
                 request_id: str = Form(...), db: Session = Depends(get_db)):
    run_action(lambda: service.get_project(db, project_id))
    if len(request_id) > 128 or not request_id or expected_revision < 0:
        raise HTTPException(422, "Invalid revision or request ID")
    name = Path(file.filename or "media").name
    target = service.root() / (uuid.uuid4().hex + Path(name).suffix.lower())
    try:
        size = 0
        with target.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 500_000_000:
                    raise HTTPException(413, "Media must be at most 500 MB")
                stream.write(chunk)
        return run_action(lambda: service.payload(service.import_source(db, project_id, target, name,
            expected_revision, request_id, "human")))
    finally:
        target.unlink(missing_ok=True)
        await file.close()


@router.post("/video-projects/{project_id}/renders")
def render(project_id: str, body: RevisionRequest, db: Session = Depends(get_db)):
    return run_action(lambda: rendering.job_payload(rendering.request_render(db, project_id,
        body.expected_revision, body.request_id)))


@router.get("/video-projects/{project_id}/renders")
def renders(project_id: str, db: Session = Depends(get_db)):
    run_action(lambda: service.get_project(db, project_id))
    return [rendering.job_payload(j) for j in db.scalars(select(VideoRender)
            .where(VideoRender.project_id == project_id).order_by(VideoRender.created_at.desc()).limit(20))]


@router.get("/video-projects/{project_id}/renders/{job_id}")
def job(project_id: str, job_id: str, db: Session = Depends(get_db)):
    return run_action(lambda: rendering.job_payload(rendering.read_job(db, project_id, job_id)))


@router.post("/video-projects/{project_id}/renders/{job_id}/cancel")
def cancel(project_id: str, job_id: str, db: Session = Depends(get_db)):
    return run_action(lambda: rendering.job_payload(rendering.cancel_render(db, project_id, job_id)))


@router.post("/video-projects/{project_id}/renders/{job_id}/review")
def review(project_id: str, job_id: str, body: RenderReview, db: Session = Depends(get_db)):
    return run_action(lambda: review_render(db, project_id, job_id, body.platform))


@router.get("/video-projects/{project_id}/sources/{source_id}/frame")
def frame(project_id: str, source_id: str, time_us: int = 0, db: Session = Depends(get_db)):
    path = run_action(lambda: rendering.source_frame(db, project_id, source_id, time_us))
    return FileResponse(path)


class ToolCall(Strict):
    name: str
    arguments: dict


@router.post("/video-editing/tools")
def tool(body: ToolCall, db: Session = Depends(get_db)):
    return run_action(lambda: call_tool(db, body.name, body.arguments, "agent:http"))
