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
        return service.payload(service.import_source(db, project_id, path, path.name,
            body.expected_revision, body.request_id, "human"))
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
