"""Run the pinned ffmpeg-skill tools against an existing Crispy export."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from sqlalchemy.orm import Session

from app.services.video_editing import VideoError, binary, root
from app.services.video_rendering import read_job

ReviewPlatform = Literal["youtube", "shorts", "tiktok", "reels"]
SCRIPTS = Path(__file__).resolve().parents[2] / "third_party" / "media" / "ffmpeg-skill" / "scripts"

# The pinned tools normally discover binaries on PATH. Reuse the binaries
# selected by Crispy, including explicit paths and the imageio-ffmpeg fallback.
_LAUNCH = """\
import runpy
import sys
from pathlib import Path

script, ffmpeg, ffprobe, *args = sys.argv[1:]
sys.path.insert(0, str(Path(script).parent))
import _common
from _common import runner

binaries = {"ffmpeg": ffmpeg, "ffprobe": ffprobe}
original = runner.ffmpeg_exe
def locate(name):
    return (binaries[name], "crispy") if name in binaries else original(name)
runner.ffmpeg_exe = _common.ffmpeg_exe = locate
sys.argv = [script, *args]
runpy.run_path(script, run_name="__main__")
"""


def _run_tool(tool: Literal["check", "look"], binaries: tuple[str, str], *args: str) -> dict:
    script = SCRIPTS / f"{tool}.py"
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", _LAUNCH, str(script), *binaries,
             *args, "--json", "--timeout", "40"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=50,
        )
        report = json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"ffmpeg-skill {tool} timed out") from exc
    except (OSError, ValueError) as exc:
        raise VideoError(f"ffmpeg-skill {tool} could not produce a report") from exc
    if not isinstance(report, dict):
        raise VideoError(f"ffmpeg-skill {tool} returned an invalid report")
    if result.returncode:
        error = report.get("error", {})
        # A delivery conformance failure still has diagnostic rows to display.
        if tool != "check" or error.get("kind") != "verification" or not report.get("checks"):
            raise VideoError(error.get("message") or f"ffmpeg-skill {tool} failed")
    return report


def review_render(db: Session, project_id: str, job_id: str, platform: ReviewPlatform | None) -> dict:
    job = read_job(db, project_id, job_id)
    if job.status != "completed" or not job.uri:
        raise VideoError("Only a completed export can be reviewed")

    revision = job.revision
    path = Path(job.uri).resolve()
    asset_root = root().resolve()
    expected_path = asset_root / project_id / "renders" / job_id / "video.mp4"
    if path != expected_path or not path.is_file():
        raise VideoError("The exported video is missing from the local render directory")

    binaries = binary("ffmpeg"), binary("ffprobe")
    stat = path.stat()
    identity = f"{stat.st_size}:{stat.st_mtime_ns}"
    options = ("--platform", platform) if platform else ()
    report = _run_tool("check", binaries, str(path), "--expect-identity", identity, *options)
    sheet = path.with_name("review.png")
    try:
        with TemporaryDirectory(prefix="qa-", dir=path.parent) as work:
            image = Path(work) / "sheet.png"
            _run_tool("look", binaries, str(path), "--tiles", "3x2", "--width", "960", "--output", str(image))
            current = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                raise VideoError("The exported file changed during review")
            image.replace(sheet)
    except OSError as exc:
        raise VideoError("Could not store the export contact sheet") from exc
    return {
        "revision": revision,
        "platform": platform,
        "checks": report["checks"],
        "notes": report.get("notes", []),
        "sheet_uri": str(sheet),
    }
