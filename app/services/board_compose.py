"""Compose board images into a single labelled sheet.

Agent tools need to *show* a comparison, not just describe one. A composed
sheet is a plain PNG under ``assets/boards/<run>/<board>/compose`` so it can be
pinned to the board as an ordinary asset and attached to the next model round
as an image.

Only run-owned media is read: a URI must already be a run asset (the tool
session checks that before calling in). Remote run assets are downloaded with
a tight timeout and a byte cap; data URIs are decoded in memory.
"""
from __future__ import annotations

import base64
import io
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.core.config import get_settings

logger = logging.getLogger(__name__)

PADDING = 24
GAP = 20
LABEL_HEIGHT = 34
TITLE_HEIGHT = 46
MAX_CELL_W = 420
MAX_CELL_H = 420
MAX_ITEMS = 9
MAX_PIXELS = 36_000_000
MAX_REMOTE_BYTES = 20 * 1024 * 1024
REMOTE_TIMEOUT = 10.0
BACKGROUND = (246, 245, 242)
INK = (38, 38, 36)
MUTED = (110, 108, 102)


class ComposeError(ValueError):
    """The sheet cannot be built from the given items."""


@dataclass(frozen=True)
class ComposeItem:
    uri: str
    label: str = ""


@dataclass(frozen=True)
class ComposeResult:
    path: str
    width: int
    height: int
    count: int


def compose_dir(run_id: str, board_id: str) -> Path:
    return get_settings().assets_dir / "boards" / run_id / board_id / "compose"


def _local_candidates(uri: str, run_id: str) -> list[Path]:
    raw = uri.strip()
    if raw.startswith("file://"):
        raw = raw[len("file://") :]
    path = Path(raw)
    if path.is_absolute():
        return [path]
    assets = get_settings().assets_dir
    return [
        Path.cwd() / path,
        assets / run_id / path,
        assets / "boards" / run_id / path,
        assets / path,
    ]


def _image_from_bytes(raw: bytes) -> Image.Image:
    if not raw:
        raise ComposeError("empty image data")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ComposeError("item is not a readable image") from exc
    return image


def load_image(uri: str, run_id: str) -> Image.Image:
    """Open a run image from disk, a data URI, or (as a last resort) the web."""
    raw_uri = (uri or "").strip()
    if not raw_uri:
        raise ComposeError("empty image uri")
    if raw_uri.startswith("data:"):
        try:
            header, data = raw_uri.split(",", 1)
            if ";base64" not in header:
                raise ComposeError("only base64 data URIs are supported")
            return _image_from_bytes(base64.b64decode(data, validate=False))
        except ValueError as exc:
            raise ComposeError("invalid image data URI") from exc
    if raw_uri.startswith(("http://", "https://")):
        try:
            with httpx.Client(timeout=REMOTE_TIMEOUT, follow_redirects=True) as client:
                with client.stream("GET", raw_uri) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_REMOTE_BYTES:
                            raise ComposeError("remote image is too large to compose")
                        chunks.append(chunk)
            return _image_from_bytes(b"".join(chunks))
        except httpx.HTTPError as exc:
            raise ComposeError("could not fetch the remote image") from exc
    if raw_uri.startswith(("asset://",)):
        raise ComposeError("hosted asset URIs cannot be composed; pin them instead")

    for candidate in _local_candidates(raw_uri, run_id):
        if candidate.is_file():
            return _image_from_bytes(candidate.read_bytes())
    raise ComposeError(f"image file is missing for {Path(raw_uri).name}")


_FONT_CANDIDATES = (
    "msyh.ttc",  # Microsoft YaHei (Windows)
    "PingFang.ttc",  # macOS
    "NotoSansCJK-Regular.ttc",
    "NotoSansCJKsc-Regular.otf",
    "DejaVuSans.ttf",
)


def _font(size: int) -> ImageFont.ImageFont:
    """Prefer a CJK-capable font; compose labels often contain product copy."""
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        box = draw.textbbox((0, 0), text, font=font)
    except (OSError, ValueError):
        return len(text) * 8
    return int(box[2] - box[0])


def _trim(text: str, draw: ImageDraw.ImageDraw, font, width: int) -> str:
    value = (text or "").strip().replace("\n", " ")
    if not value:
        return ""
    if _text_width(draw, value, font) <= width:
        return value
    suffix = "…"
    while value and _text_width(draw, value + suffix, font) > width:
        value = value[:-1]
    return (value.rstrip() + suffix) if value else ""


def _normalise(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        backdrop = Image.new("RGB", rgba.size, (255, 255, 255))
        backdrop.paste(rgba, mask=rgba.getchannel("A"))
        return backdrop
    return image.convert("RGB")


def _columns(count: int, layout: str, columns: int | None) -> int:
    if layout == "stack":
        return 1
    if layout == "row":
        return count
    if columns and columns > 0:
        return min(columns, count)
    return max(1, math.ceil(math.sqrt(count)))


def compose_sheet(
    items: list[ComposeItem],
    *,
    run_id: str,
    board_id: str,
    stem: str,
    layout: str = "row",
    columns: int | None = None,
    labels: bool = True,
    title: str = "",
) -> ComposeResult:
    """Lay items out in a labelled sheet and return the saved PNG path."""
    if not items:
        raise ComposeError("compose needs at least one image")
    if len(items) > MAX_ITEMS:
        raise ComposeError(f"compose accepts at most {MAX_ITEMS} images")
    layout = layout if layout in {"row", "grid", "stack"} else "row"

    loaded: list[tuple[Image.Image, str]] = []
    for item in items:
        image = _normalise(load_image(item.uri, run_id))
        image.thumbnail((MAX_CELL_W, MAX_CELL_H), Image.LANCZOS)
        loaded.append((image, item.label.strip()))

    cols = _columns(len(loaded), layout, columns)
    rows: list[list[tuple[Image.Image, str]]] = [
        loaded[index : index + cols] for index in range(0, len(loaded), cols)
    ]

    measuring = ImageDraw.Draw(Image.new("RGB", (8, 8), BACKGROUND))
    label_font = _font(17)
    title_font = _font(26)
    label_h = LABEL_HEIGHT if labels and any(label for _, label in loaded) else 0

    row_widths = [sum(img.width for img, _ in row) + GAP * (len(row) - 1) for row in rows]
    row_heights = [max(img.height for img, _ in row) for row in rows]
    title_text = title.strip().replace("\n", " ")[:120]
    content_w = max(row_widths)
    if title_text:
        content_w = max(content_w, min(1600, _text_width(measuring, title_text, title_font)))

    width = content_w + PADDING * 2
    height = (
        PADDING * 2
        + (TITLE_HEIGHT if title_text else 0)
        + sum(row_h + label_h for row_h in row_heights)
        + GAP * (len(rows) - 1)
    )

    # A 9-up grid of large images can exceed provider image limits; scale the
    # whole sheet down rather than refusing the comparison.
    if width * height > MAX_PIXELS:
        scale = math.sqrt(MAX_PIXELS / (width * height))
        width = max(1, int(width * scale))
        height = max(1, int(height * scale))
        for index, row in enumerate(rows):
            rows[index] = [
                (img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale)))), label)
                for img, label in row
            ]
        row_widths = [sum(img.width for img, _ in row) + GAP * (len(row) - 1) for row in rows]
        row_heights = [max(img.height for img, _ in row) for row in rows]

    canvas = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)

    y = PADDING
    if title_text:
        draw.text((PADDING, y + 6), _trim(title_text, draw, title_font, width - PADDING * 2), font=title_font, fill=INK)
        y += TITLE_HEIGHT

    for row_index, row in enumerate(rows):
        row_w = row_widths[row_index]
        x = (width - row_w) // 2
        for image, label in row:
            canvas.paste(image, (x, y))
            if label_h and label:
                text = _trim(label, draw, label_font, image.width)
                text_w = _text_width(draw, text, label_font)
                draw.text(
                    (x + max(0, (image.width - text_w) // 2), y + image.height + 8),
                    text,
                    font=label_font,
                    fill=MUTED,
                )
            x += image.width + GAP
        y += row_heights[row_index] + label_h + (GAP if row_index < len(rows) - 1 else 0)

    target = compose_dir(run_id, board_id) / f"{stem}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "PNG", optimize=True)
    logger.info("composed %s board images into %s", len(loaded), target)
    return ComposeResult(path=str(target), width=width, height=height, count=len(loaded))


def local_image_path(uri: str, run_id: str) -> Path | None:
    """The on-disk file for a URI without any network or decode work."""
    raw_uri = (uri or "").strip()
    if not raw_uri or raw_uri.startswith(("data:", "http://", "https://", "asset://")):
        return None
    for candidate in _local_candidates(raw_uri, run_id):
        if candidate.is_file():
            return candidate
    return None


def image_size(path: str, run_id: str) -> tuple[int, int] | None:
    """Best-effort pixel size for pinning; ``None`` when unreadable locally."""
    candidate = local_image_path(path, run_id)
    if candidate is None:
        return None
    try:
        with Image.open(candidate) as image:
            return image.width, image.height
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
