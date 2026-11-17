import json
from pathlib import Path
import shutil
import subprocess

import pytest
from app.core.config import get_settings
from app.data.session import SessionLocal
from app.data.models import Artifact, VideoRender
from app.schemas.video import VideoDocument
from app.services.video_editing import import_source, save_project
from app.services.video_rendering import request_render, render_job, cancel_render
from app.services.video_tools import call_tool
from test_video_editing import make_project


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Requires local FFmpeg and FFprobe")
def test_real_export_pins_sources_and_registers_result(client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "assets_dir", tmp_path / "assets")
    p = make_project(client)
    source = tmp_path / "input.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-y", str(source)], check=True)
    with SessionLocal() as db:
        project = import_source(db, p["id"], source, "Footage", 0, "import", "human")
        doc = VideoDocument.model_validate({"width": 320, "height": 180, "fps_num": 30000, "fps_den": 1001,
            "tracks": [{"id": "video", "clips": [{"id": "left", "kind": "video", "source_id": project.sources[0]["id"],
            "source_in_us": 1_000_000, "start_us": 0, "duration_us": 1_000_000},
            {"id": "right", "kind": "video", "source_id": project.sources[0]["id"],
            "source_in_us": 2_000_000, "start_us": 1_000_000, "duration_us": 1_000_000}]},
            {"id": "text", "clips": [{"id": "title", "kind": "text", "duration_us": 2_000_000,
                "text": "Crispy test", "x": .1, "y": .7, "width": .8, "height": .25, "font_size": 20}]}]})
        project = save_project(db, project.id, doc, project.revision, "edit", "human")
        job = request_render(db, project.id, project.revision, "export", launch=False)
        assert request_render(db, project.id, project.revision, "export", launch=False).id == job.id
        # Editing the project after queueing must not alter the snapshot.
        save_project(db, project.id, VideoDocument(), project.revision, "later", "agent:test")
        render_job(db.get_bind(), job.id)
        db.expire_all()
        job = db.get(VideoRender, job.id)
        assert job.status == "completed", job.error
        artifact = db.get(Artifact, job.artifact_id)
        assert artifact.run_id == p["run_id"] and artifact.payload["revision"] == job.revision
        result = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", job.uri]))
        video = next(s for s in result["streams"] if s["codec_type"] == "video")
        assert video["r_frame_rate"] == "30000/1001"
        assert any(s["codec_type"] == "audio" for s in result["streams"])
        assert abs(float(result["format"]["duration"]) - 2) < .15
        source.unlink()
        assert Path(job.uri).is_file()
        # Existing media endpoint already provides byte-range playback.
        response = client.get("/media", params={"path": job.uri}, headers={"Range": "bytes=0-99"})
        assert response.status_code == 206 and len(response.content) == 100


def test_tool_operations_retry_and_cancellation(client):
    p = make_project(client)
    args = {"project_id": p["id"], "expected_revision": 0, "request_id": "tools-edit",
        "operations": [{"op": "add_track", "track_id": "title"}, {"op": "add_clip", "track_id": "title",
        "values": {"id": "caption", "kind": "text", "text": "Agent", "duration_us": 1_000_000}}]}
    with SessionLocal() as db:
        edited = call_tool(db, "edit_video_project", args, "agent:test")
        assert call_tool(db, "edit_video_project", args, "agent:test")["revision"] == edited["revision"]
        job = request_render(db, p["id"], edited["revision"], "queued", launch=False)
        cancelled = cancel_render(db, p["id"], job.id)
        assert cancelled.status == "cancelled"
        render_job(db.get_bind(), job.id)
        db.refresh(job)
        assert not job.uri and job.status == "cancelled"
    from app.mcp.videowright import definitions
    specs = definitions()
    assert len(specs) == 11
    assert next(s for s in specs if s.name == "edit_video_project").annotations.readOnlyHint is False
