"""Local FFmpeg adapter. Render jobs pin a document and immutable source references."""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import time
from fractions import Fraction

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.data.models import Artifact, VideoRender
from app.schemas.video import VideoDocument
from app.services.video_editing import VideoConflict, VideoError, binary, get_project, root, validate_sources

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crispy-video")


def job_payload(job: VideoRender) -> dict:
    return {"id": job.id, "project_id": job.project_id, "revision": job.revision,
            "status": job.status, "progress": job.progress, "error": job.error,
            "uri": job.uri, "artifact_id": job.artifact_id,
            "created_at": job.created_at.isoformat()}


def read_job(db: Session, project_id: str, job_id: str) -> VideoRender:
    job = db.get(VideoRender, job_id)
    if not job or job.project_id != project_id:
        raise VideoError("Render job not found in this project")
    return job


def request_render(db: Session, project_id: str, revision: int, request_id: str, *, launch=True) -> VideoRender:
    project = get_project(db, project_id)
    previous = db.scalar(select(VideoRender).where(VideoRender.project_id == project_id,
                                                  VideoRender.request_id == request_id))
    if previous:
        if previous.revision != revision:
            raise VideoConflict("Render request ID was used for another revision")
        return previous
    if revision != project.revision:
        raise VideoConflict("Project changed. Save and render the latest revision.")
    document = VideoDocument.model_validate(project.document)
    validate_sources(document, project.sources)
    if not document.duration_us:
        raise VideoError("Add a clip before exporting")
    binary("ffmpeg")
    binary("ffprobe")
    job = VideoRender(project_id=project.id, revision=revision, request_id=request_id,
                      document=project.document, sources=project.sources)
    db.add(job)
    db.commit()
    if launch:
        executor.submit(render_job, db.get_bind(), job.id)
    return job


def cancel_render(db: Session, project_id: str, job_id: str) -> VideoRender:
    job = read_job(db, project_id, job_id)
    db.execute(update(VideoRender).where(VideoRender.id == job.id,
        VideoRender.status.in_(["queued", "running"])).values(status="cancelled"))
    db.commit()
    db.refresh(job)
    return job


def _font(size: int, text: str = ""):
    import platform
    windows_fonts = (["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/msyh.ttc"] if text.isascii()
                     else ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf"])
    choices = (windows_fonts if platform.system() == "Windows"
               else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"])
    for path in choices:
        if Path(path).is_file():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default(size=size)


def _text_image(clip, document: VideoDocument, directory: Path) -> Path:
    width, height = max(2, round(clip.width * document.width)), max(2, round(clip.height * document.height))
    image = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(image)
    font = _font(clip.font_size, clip.text)
    # Wrap at the same explicit text-box width used by the editor.
    lines, line = [], ""
    for char in clip.text:
        if char == "\n":
            lines.append(line)
            line = ""
        elif line and draw.textlength(line + char, font=font) > width:
            lines.append(line)
            line = char
        else:
            line += char
    lines.append(line)
    draw.multiline_text((0, 0), "\n".join(lines), font=font, fill=clip.color, spacing=round(clip.font_size * .2))
    filename = hashlib.sha256(clip.id.encode()).hexdigest()[:24]
    output = directory / f"{filename}.png"
    image.save(output)
    return output


def build_command(document: VideoDocument, sources: list[dict], directory: Path, output: Path) -> list[str]:
    duration = document.duration_us / 1e6
    fps = f"{document.fps_num}/{document.fps_den}"
    command = [binary("ffmpeg"), "-hide_banner", "-y", "-filter_complex_threads", "1", "-f", "lavfi", "-i",
               f"color=c={document.background}:s={document.width}x{document.height}:r={fps}:d={duration:.6f}"]
    known = {s["id"]: s for s in sources}
    filters, audio = [], []
    last, index = "0:v", 1
    # Tracks are bottom-to-top. A clip's identity is never used as filter syntax.
    for track in document.tracks:
        for clip in sorted(track.clips, key=lambda c: c.start_us):
            start, length = clip.start_us / 1e6, clip.duration_us / 1e6
            source = known.get(clip.source_id)
            is_image = clip.kind in {"image", "text"}
            path = _text_image(clip, document, directory) if clip.kind == "text" else Path(source["uri"])
            if is_image:
                command += ["-loop", "1", "-framerate", fps, "-t", f"{length:.6f}", "-i", str(path)]
            else:
                command += ["-ss", f"{clip.source_in_us / 1e6:.6f}", "-t", f"{length:.6f}", "-i", str(path)]
            if clip.kind != "audio":
                w, h = max(2, round(clip.width * document.width)), max(2, round(clip.height * document.height))
                scaling = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}" if clip.fit == "cover"
                           else f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black@0")
                filters.append(f"[{index}:v]format=rgba,{scaling},setsar=1,colorchannelmixer=aa={clip.opacity},"
                               f"setpts=PTS-STARTPTS+{start:.6f}/TB[v{index}]")
                filters.append(f"[{last}][v{index}]overlay=x={round(clip.x * document.width)}:y={round(clip.y * document.height)}:"
                               f"eof_action=pass:repeatlast=0:enable='gte(t,{start:.6f})*lt(t,{start+length:.6f})'[o{index}]")
                last = f"o{index}"
            if source and source["has_audio"] and clip.kind in {"video", "audio"} and clip.volume:
                filters.append(f"[{index}:a]asetpts=PTS-STARTPTS,volume={clip.volume},"
                               f"adelay={round(start*1000)}:all=1[a{index}]")
                audio.append(f"[a{index}]")
            index += 1
    filters.append(f"[{last}]format=yuv420p[outv]")
    if audio:
        filters.append("".join(audio) + f"amix=inputs={len(audio)}:normalize=0,apad,atrim=duration={duration:.6f}[outa]")
    script = directory / "filters.txt"
    script.write_text(";\n".join(filters), encoding="utf-8")
    command += ["-/filter_complex", str(script), "-map", "[outv]"]
    command += ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"] if audio else ["-an"]
    command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", fps,
                "-t", f"{duration:.6f}", "-movflags", "+faststart", "-progress", str(directory / "progress.txt"), str(output)]
    return command


def _render_progress(report: str, duration_us: int) -> float | None:
    for line in reversed(report.splitlines(keepends=True)):
        if not line.startswith("out_time_us=") or not line.endswith("\n"):
            continue
        try:
            elapsed_us = int(line.split("=", 1)[1])
        except ValueError:
            continue
        return max(0, min(.95, elapsed_us / max(1, duration_us)))
    return None


def render_job(engine, job_id: str):
    process = None
    with Session(engine) as db:
        claim = db.execute(update(VideoRender).where(VideoRender.id == job_id, VideoRender.status == "queued")
                           .values(status="running", worker_pid=os.getpid()))
        db.commit()
        if not claim.rowcount:
            return
        job = db.get(VideoRender, job_id)
        directory = root() / job.project_id / "renders" / job.id
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / "video.mp4"
        try:
            document = VideoDocument.model_validate(job.document)
            for source in job.sources:
                with Path(source["uri"]).open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != source["sha256"]:
                        raise VideoError("An imported source was modified on disk; reimport it before exporting")
            command = build_command(document, job.sources, directory, output)
            with (directory / "render.log").open("wb") as log:
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log)
                started = time.monotonic()
                while process.poll() is None:
                    time.sleep(.25)
                    db.expire_all()
                    if db.get(VideoRender, job_id).status == "cancelled":
                        process.terminate()
                        process.wait(timeout=10)
                        output.unlink(missing_ok=True)
                        return
                    if time.monotonic() - started > max(120, document.duration_us / 1e6 * 20):
                        raise VideoError("Local render exceeded its time limit")
                    progress_file = directory / "progress.txt"
                    if progress_file.exists():
                        progress = _render_progress(progress_file.read_text(errors="ignore"), document.duration_us)
                        if progress is not None:
                            db.execute(update(VideoRender).where(VideoRender.id == job_id).values(progress=progress))
                            db.commit()
            if process.returncode:
                raise VideoError((directory / "render.log").read_text(errors="replace")[-2000:])
            info = subprocess.run([binary("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
                                  capture_output=True, timeout=30, check=True)
            metadata = json.loads(info.stdout)
            video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
            if (video["width"], video["height"]) != (document.width, document.height):
                raise VideoError("Rendered dimensions do not match the project")
            actual_fps = float(Fraction(video["avg_frame_rate"]))
            if abs(actual_fps - document.fps_num / document.fps_den) > .001:
                raise VideoError("Rendered frame rate does not match the project")
            source_map = {s["id"]: s for s in job.sources}
            needs_audio = any(c.volume > 0 and source_map.get(c.source_id, {}).get("has_audio")
                              for t in document.tracks for c in t.clips if c.kind in {"video", "audio"})
            if needs_audio and not any(s["codec_type"] == "audio" for s in metadata["streams"]):
                raise VideoError("Rendered audio track is missing")
            if abs(float(metadata["format"]["duration"]) - document.duration_us / 1e6) > .15:
                raise VideoError("Rendered duration does not match the project")
            subprocess.run([binary("ffmpeg"), "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"],
                           capture_output=True, timeout=max(60, document.duration_us / 1e6 * 2), check=True)
            db.expire_all()
            job = db.get(VideoRender, job_id)
            if job.status == "cancelled":
                output.unlink(missing_ok=True)
                return
            project = get_project(db, job.project_id)
            artifact = Artifact(run_id=project.run_id, stage_name="video_editing", artifact_type="generated_video", uri=str(output),
                payload={"video_project_id": project.id, "revision": job.revision, "render_id": job.id,
                         "source_ids": [s["id"] for s in job.sources], "technical_check": "passed"})
            db.add(artifact)
            db.flush()
            # Final status transition is guarded: a simultaneous cancellation wins.
            result = db.execute(update(VideoRender).where(VideoRender.id == job.id, VideoRender.status == "running")
                .values(status="completed", progress=1, uri=str(output), artifact_id=artifact.id))
            if result.rowcount:
                db.commit()
            else:
                db.rollback()
                output.unlink(missing_ok=True)
        except Exception as exc:
            if process and process.poll() is None:
                process.kill()
                process.wait()
            db.rollback()
            db.execute(update(VideoRender).where(VideoRender.id == job_id, VideoRender.status == "running")
                       .values(status="failed", error=str(exc)[-3000:]))
            db.commit()


def recover_jobs(db: Session):
    for job in db.scalars(select(VideoRender).where(VideoRender.status.in_(["queued", "running"]))):
        if job.status == "queued":
            executor.submit(render_job, db.get_bind(), job.id)
        else:
            try:
                if not job.worker_pid:
                    raise ProcessLookupError()
                os.kill(job.worker_pid, 0)
            except (ProcessLookupError, OSError):
                job.status = "failed"
                job.error = "Render interrupted by application shutdown. Retry export."
    db.commit()


def source_frame(db: Session, project_id: str, source_id: str, time_us: int) -> Path:
    project = get_project(db, project_id)
    source = next((s for s in project.sources if s["id"] == source_id), None)
    if not source or source["kind"] == "audio":
        raise VideoError("Visual source not found")
    if source["kind"] == "image":
        return Path(source["uri"])
    if time_us < 0 or time_us >= source["duration_us"]:
        raise VideoError("Frame time is outside this source")
    target = root() / project_id / "frames" / f"{source_id}-{time_us}.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        subprocess.run([binary("ffmpeg"), "-v", "error", "-ss", f"{time_us/1e6:.6f}", "-i", source["uri"],
                        "-frames:v", "1", "-vf", "scale=960:-2", "-y", str(target)], capture_output=True, timeout=30, check=True)
    return target
