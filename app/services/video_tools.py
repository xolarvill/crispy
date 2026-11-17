"""One typed tool catalog shared by HTTP harnesses and the video MCP adapter."""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from pydantic import Field
from sqlalchemy.orm import Session
from app.schemas.video import Strict, VideoOperation
from app.services import video_editing as editing, video_rendering as rendering


class ProjectArgs(Strict):
    project_id: str = Field(min_length=1, max_length=64)


class RunArgs(Strict):
    run_id: str = Field(min_length=1, max_length=64)


class CreateArgs(RunArgs):
    title: str = Field(min_length=1, max_length=160)
    request_id: str = Field(min_length=1, max_length=128)


class WriteArgs(ProjectArgs):
    expected_revision: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=128)


class EditArgs(WriteArgs):
    operations: list[VideoOperation] = Field(min_length=1, max_length=120)
    summary: str = Field(default="Agent edit", max_length=300)


class ImportArgs(WriteArgs):
    source_path: str = Field(min_length=1, max_length=4096)


class RestoreArgs(WriteArgs):
    revision: int = Field(ge=0)


class JobArgs(ProjectArgs):
    job_id: str = Field(min_length=1, max_length=64)


class FrameArgs(ProjectArgs):
    source_id: str = Field(min_length=1, max_length=64)
    time_us: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class ToolSpec:
    args: type[Strict]
    description: str
    write: bool
    action: Callable


def _import(db, args, actor):
    path = Path(args.source_path)
    if not path.is_absolute() or not path.is_file():
        raise editing.VideoError("source_path must be an existing absolute local media path")
    return editing.payload(editing.import_source(db, args.project_id, path, path.name,
        args.expected_revision, args.request_id, actor))


TOOLS = {
    "list_video_projects": ToolSpec(RunArgs, "List Crispy video edits belonging to a run.", False,
        lambda db, a, actor: {"projects": editing.list_projects(db, a.run_id)}),
    "create_video_project": ToolSpec(CreateArgs, "Create an idempotent video editing project in an existing run. Start with an empty timeline.", True,
        lambda db, a, actor: editing.payload(editing.create_project(db, a.run_id, a.title, a.request_id))),
    "get_video_project": ToolSpec(ProjectArgs, "Read revision, immutable sources and neutral timeline. All times are integer microseconds. Tracks are bottom-to-top; clips within a track cannot overlap.", False,
        lambda db, a, actor: editing.payload(editing.get_project(db, a.project_id))),
    "import_video_source": ToolSpec(ImportArgs, "Import a local image/video/audio file as an immutable source. Does not place a clip. Reread the resulting revision before editing.", True, _import),
    "edit_video_project": ToolSpec(EditArgs, "Atomic operations: add_track/remove_track (track_id), add_clip (track_id, values as VideoClip), update_clip (clip_id, values), remove_clip, split_clip (clip_id, at_us, stable new_id), settings (values). Clip values: id, kind, source_id, start_us, duration_us, source_in_us; optional text, x/y/width/height normalized 0..1, fit contain/cover, volume, font_size, color. Read current revision first and preserve human edits on conflicts.", True,
        lambda db, a, actor: editing.payload(editing.edit_operations(db, a.project_id, a.operations,
            a.expected_revision, a.request_id, actor, a.summary))),
    "get_video_history": ToolSpec(ProjectArgs, "Read revisions and human/agent attribution.", False,
        lambda db, a, actor: {"history": editing.history(db, a.project_id)}),
    "restore_video_revision": ToolSpec(RestoreArgs, "Restore old content as a new revision, retaining later history.", True,
        lambda db, a, actor: editing.payload(editing.restore(db, a.project_id, a.revision,
            a.expected_revision, a.request_id, actor))),
    "get_video_frame": ToolSpec(FrameArgs, "Return a source image at a microsecond timestamp. For a clip use source_in_us plus the offset within the clip.", False,
        lambda db, a, actor: {"image_path": str(rendering.source_frame(db, a.project_id, a.source_id, a.time_us))}),
    "render_video_project": ToolSpec(WriteArgs, "Queue a local MP4 pinned to this revision. Poll get_video_render. Completed output becomes a new run asset.", True,
        lambda db, a, actor: rendering.job_payload(rendering.request_render(db, a.project_id, a.expected_revision, a.request_id))),
    "get_video_render": ToolSpec(JobArgs, "Read render progress, failure or final output URI.", False,
        lambda db, a, actor: rendering.job_payload(rendering.read_job(db, a.project_id, a.job_id))),
    "cancel_video_render": ToolSpec(JobArgs, "Cancel a queued/running export; completed outputs remain intact.", True,
        lambda db, a, actor: rendering.job_payload(rendering.cancel_render(db, a.project_id, a.job_id))),
}


def call_tool(db: Session, name: str, arguments: dict, actor: str) -> dict:
    spec = TOOLS.get(name)
    if not spec:
        raise editing.VideoError(f"Unknown video tool: {name}")
    return spec.action(db, spec.args.model_validate(arguments), actor)
