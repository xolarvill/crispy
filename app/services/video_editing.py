"""Shared timeline authority for UI, HTTP, in-process agents and MCP."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data.models import Artifact, PipelineRun, VariantAsset, VideoProject, VideoRevision
from app.schemas.video import VideoDocument, VideoOperation


class VideoError(ValueError):
    pass


class VideoConflict(VideoError):
    pass


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def root() -> Path:
    result = get_settings().assets_dir.resolve() / "video"
    result.mkdir(parents=True, exist_ok=True)
    return result


def binary(name: str) -> str:
    import os
    configured = os.environ.get(f"CRISPY_{name.upper()}_PATH")
    result = configured or shutil.which(name)
    if not result and name == "ffmpeg":
        import imageio_ffmpeg
        result = imageio_ffmpeg.get_ffmpeg_exe()
    if not result:
        raise VideoError(f"{name} is required for local video editing; install it or set CRISPY_{name.upper()}_PATH")
    return result


def capabilities() -> dict:
    result = {}
    for name in ("ffmpeg", "ffprobe"):
        try:
            result[name] = bool(binary(name))
        except VideoError:
            result[name] = False
    return {"ready": all(result.values()), **result}


def probe(path: Path) -> dict:
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return {"kind": "image", "width": image.width, "height": image.height,
                    "duration_us": 0, "has_audio": False}
    try:
        result = subprocess.run([binary("ffprobe"), "-v", "error", "-show_streams", "-show_format",
                                 "-of", "json", str(path)], capture_output=True, timeout=30, check=True)
        data = json.loads(result.stdout)
        video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
        audio = any(s["codec_type"] == "audio" for s in data["streams"])
        duration = float(data.get("format", {}).get("duration") or (video or {}).get("duration") or 0)
        if duration <= 0 or duration > 3600 or not (video or audio):
            raise VideoError("Media must have a finite duration of at most one hour")
        return {"kind": "video" if video else "audio", "width": (video or {}).get("width", 0),
                "height": (video or {}).get("height", 0), "duration_us": round(duration * 1_000_000),
                "has_audio": audio}
    except (subprocess.SubprocessError, KeyError, ValueError) as exc:
        raise VideoError("Could not read this media file") from exc


def get_project(db: Session, project_id: str) -> VideoProject:
    project = db.get(VideoProject, project_id)
    if not project:
        raise VideoError("Video project not found")
    return project


def payload(project: VideoProject) -> dict:
    return {"id": project.id, "workspace_id": project.workspace_id, "run_id": project.run_id,
            "title": project.title, "revision": project.revision, "document": project.document,
            "sources": project.sources, "updated_at": project.updated_at.isoformat() if project.updated_at else None}


def list_projects(db: Session, run_id: str) -> list[dict]:
    return [{k: v for k, v in payload(p).items() if k not in {"document", "sources"}}
            for p in db.scalars(select(VideoProject).where(VideoProject.run_id == run_id)
                                .order_by(VideoProject.updated_at.desc()))]


def create_project(db: Session, run_id: str, title: str, request_id: str) -> VideoProject:
    run = db.get(PipelineRun, run_id)
    if not run:
        raise VideoError("Run not found")
    project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"crispy-video:{run_id}:{request_id}"))
    signature = fingerprint({"run_id": run_id, "title": title})
    old = db.get(VideoProject, project_id)
    if old:
        if old.creation_hash != signature:
            raise VideoConflict("Request ID was already used with different creation inputs")
        return old
    document = VideoDocument().model_dump()
    project = VideoProject(id=project_id, workspace_id=run.workspace_id, run_id=run.id,
                           title=title, creation_hash=signature, document=document, sources=[], revision=0)
    db.add(project)
    db.add(VideoRevision(project_id=project_id, revision=0, document=document,
                         actor="system", request_id="initial", request_hash=signature, summary="Created project"))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(VideoProject, project_id)
        if existing is None:
            raise
        if existing.creation_hash != signature:
            raise VideoConflict("Request ID was already used with different creation inputs")
        return existing
    return project


def validate_sources(document: VideoDocument, sources: list[dict]) -> None:
    available = {s["id"]: s for s in sources}
    for track in document.tracks:
        for clip in track.clips:
            if clip.kind == "text":
                continue
            source = available.get(clip.source_id)
            if not source or source["kind"] != clip.kind:
                raise VideoError(f"Clip {clip.id} refers to an unknown or incompatible source")
            if clip.kind != "image" and clip.source_in_us + clip.duration_us > source["duration_us"] + 1000:
                raise VideoError(f"Clip {clip.id} extends beyond its source duration")


def save_project(db: Session, project_id: str, document: VideoDocument, expected_revision: int,
                 request_id: str, actor: str, summary: str = "Edit timeline", *, sources=None,
                 request_payload: dict | None = None) -> VideoProject:
    project = get_project(db, project_id)
    signature = fingerprint(request_payload if request_payload is not None else {
        "document": document.model_dump(), "sources": sources, "summary": summary,
        "expected_revision": expected_revision, "actor": actor})
    previous = db.scalar(select(VideoRevision).where(VideoRevision.project_id == project_id,
                                                     VideoRevision.request_id == request_id))
    if previous:
        if previous.request_hash != signature:
            raise VideoConflict("Request ID was already used with different edits")
        return project
    validate_sources(document, project.sources if sources is None else sources)
    values = {"document": document.model_dump(), "revision": expected_revision + 1}
    if sources is not None:
        values["sources"] = sources
    changed = db.execute(update(VideoProject).where(VideoProject.id == project_id,
                         VideoProject.revision == expected_revision).values(**values),
                         execution_options={"synchronize_session": False})
    if not changed.rowcount:
        db.rollback()
        previous = db.scalar(select(VideoRevision).where(VideoRevision.project_id == project_id,
                                                         VideoRevision.request_id == request_id))
        if previous and previous.request_hash == signature:
            return get_project(db, project_id)
        raise VideoConflict("Project changed. Reload the latest revision before applying edits.")
    db.add(VideoRevision(project_id=project_id, revision=expected_revision + 1,
                         document=document.model_dump(), request_id=request_id, request_hash=signature,
                         actor=actor, summary=summary))
    db.commit()
    db.refresh(project)
    return project


def resolve_run_source(db: Session, project: VideoProject, uri: str) -> Path:
    allowed = set(db.scalars(select(Artifact.uri).where(Artifact.run_id == project.run_id)))
    allowed.update(db.scalars(select(VariantAsset.uri).where(VariantAsset.run_id == project.run_id)))
    if uri not in allowed:
        raise VideoError("Source is not registered to this run; upload a local file instead")
    path = Path(uri).resolve()
    if not path.is_relative_to(get_settings().assets_dir.resolve()) or not path.is_file():
        raise VideoError("Source must be a local file inside the configured assets directory")
    return path


def import_source_with_id(db: Session, project_id: str, path: Path, name: str, expected_revision: int,
                          request_id: str, actor: str) -> tuple[VideoProject, str]:
    project = get_project(db, project_id)
    if path.stat().st_size > 500_000_000:
        raise VideoError("Media must be at most 500 MB")
    if path.suffix.lower() not in {".mp4", ".mov", ".m4v", ".webm", ".png", ".jpg", ".jpeg", ".webp",
                                  ".wav", ".mp3", ".ogg", ".m4a", ".aac", ".flac"}:
        raise VideoError("Unsupported media type")
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    source_id = "s-" + sha[:24]
    command = {"op": "import", "sha256": sha, "name": name, "expected_revision": expected_revision, "actor": actor}
    previous = db.scalar(select(VideoRevision).where(VideoRevision.project_id == project_id,
                                                     VideoRevision.request_id == request_id))
    if previous:
        if previous.request_hash != fingerprint(command):
            raise VideoConflict("Request ID was already used for a different import")
        return project, source_id
    target = root() / project_id / "sources" / (sha + path.suffix.lower())
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        shutil.copyfile(path, temporary)
        with temporary.open("rb") as stream:
            copied_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        if copied_sha != sha:
            temporary.unlink(missing_ok=True)
            raise VideoError("Source changed while importing; retry with a stable file")
        temporary.replace(target)
    metadata = probe(target)
    sources = copy.deepcopy(project.sources)
    source = {"id": source_id, "name": name[:160], "uri": str(target), "sha256": sha, **metadata}
    if not any(s["id"] == source_id for s in sources):
        sources.append(source)
    result = save_project(db, project_id, VideoDocument.model_validate(project.document), expected_revision,
                          request_id, actor, f"Import {name[:120]}", sources=sources, request_payload=command)
    return result, source_id


def import_source(db: Session, project_id: str, path: Path, name: str, expected_revision: int,
                  request_id: str, actor: str) -> VideoProject:
    project, _ = import_source_with_id(db, project_id, path, name, expected_revision, request_id, actor)
    return project


def apply_operations(document: VideoDocument, operations: list[VideoOperation]) -> VideoDocument:
    data = document.model_dump()
    for operation in operations:
        tracks = data["tracks"]
        track = next((t for t in tracks if t["id"] == operation.track_id), None)
        located = [(t, c) for t in tracks for c in t["clips"] if c["id"] == operation.clip_id]
        owner, clip = located[0] if located else (None, None)
        if operation.op == "settings":
            if set(operation.values) - {"width", "height", "fps_num", "fps_den", "background"}:
                raise VideoError("Unsupported composition setting")
            data.update(operation.values)
        elif operation.op == "add_track":
            tracks.append({"id": operation.track_id or str(uuid.uuid4()), "name": operation.values.get("name", "Track"), "clips": []})
        elif operation.op == "remove_track" and track:
            tracks.remove(track)
        elif operation.op == "add_clip" and track:
            track["clips"].append(operation.values)
        elif operation.op == "update_clip" and clip:
            if "id" in operation.values:
                raise VideoError("Clip identity cannot be changed")
            if operation.track_id and operation.track_id != owner["id"]:
                if not track:
                    raise VideoError("Target track not found")
                owner["clips"].remove(clip)
                track["clips"].append(clip)
            clip.update(operation.values)
        elif operation.op == "remove_clip" and clip:
            owner["clips"].remove(clip)
        elif operation.op == "split_clip" and clip:
            at = operation.at_us
            if at is None or not clip["start_us"] < at < clip["start_us"] + clip["duration_us"]:
                raise VideoError("Split point must lie inside the clip")
            if not operation.new_id:
                raise VideoError("split_clip requires a stable new_id")
            right = copy.deepcopy(clip)
            elapsed = at - clip["start_us"]
            right.update(id=operation.new_id, start_us=at, duration_us=clip["duration_us"] - elapsed)
            if clip["kind"] in {"video", "audio"}:
                right["source_in_us"] += elapsed
            clip["duration_us"] = elapsed
            owner["clips"].append(right)
        else:
            raise VideoError(f"Unknown track or clip for {operation.op}")
    return VideoDocument.model_validate(data)


def history(db: Session, project_id: str) -> list[dict]:
    get_project(db, project_id)
    return [{"revision": r.revision, "actor": r.actor, "summary": r.summary,
             "created_at": r.created_at.isoformat()} for r in db.scalars(select(VideoRevision)
            .where(VideoRevision.project_id == project_id).order_by(VideoRevision.revision.desc()).limit(100))]


def restore(db: Session, project_id: str, revision: int, expected_revision: int, request_id: str, actor: str):
    old = db.scalar(select(VideoRevision).where(VideoRevision.project_id == project_id,
                                               VideoRevision.revision == revision))
    if not old:
        raise VideoError("Revision not found")
    return save_project(db, project_id, VideoDocument.model_validate(old.document), expected_revision,
                        request_id, actor, f"Restore revision {revision}")


def edit_operations(db: Session, project_id: str, operations: list[VideoOperation], expected_revision: int,
                    request_id: str, actor: str, summary: str):
    project = get_project(db, project_id)
    command = {"operations": [op.model_dump() for op in operations], "expected_revision": expected_revision,
               "actor": actor, "summary": summary}
    previous = db.scalar(select(VideoRevision).where(VideoRevision.project_id == project_id,
                                                     VideoRevision.request_id == request_id))
    if previous:
        if previous.request_hash != fingerprint(command):
            raise VideoConflict("Request ID was already used with different operations")
        return project
    document = apply_operations(VideoDocument.model_validate(project.document), operations)
    return save_project(db, project_id, document, expected_revision, request_id, actor, summary,
                        request_payload=command)
