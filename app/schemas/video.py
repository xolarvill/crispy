"""Editor-independent timeline contract. All time values are integer microseconds."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TIME = 3_600_000_000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class VideoClip(Strict):
    id: str = Field(min_length=1, max_length=64)
    kind: Literal["video", "image", "audio", "text"]
    source_id: str | None = Field(default=None, max_length=64)
    start_us: int = Field(default=0, ge=0, le=MAX_TIME)
    duration_us: int = Field(gt=0, le=MAX_TIME)
    source_in_us: int = Field(default=0, ge=0, le=MAX_TIME)
    text: str = Field(default="", max_length=2000)
    x: float = Field(default=0, ge=0, le=1)
    y: float = Field(default=0, ge=0, le=1)
    width: float = Field(default=1, gt=0, le=1)
    height: float = Field(default=1, gt=0, le=1)
    fit: Literal["contain", "cover"] = "contain"
    volume: float = Field(default=1, ge=0, le=2)
    opacity: float = Field(default=1, ge=0, le=1)
    font_size: int = Field(default=36, ge=8, le=300)
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def valid_clip(self):
        if self.start_us + self.duration_us > MAX_TIME:
            raise ValueError("Timeline exceeds one hour")
        if self.kind == "text" and (not self.text.strip() or self.source_id):
            raise ValueError("Text clips require text and no source")
        if self.kind != "text" and not self.source_id:
            raise ValueError("Media clips require a source")
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("Clip lies outside the output frame")
        return self


class VideoTrack(Strict):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="Track", max_length=100)
    clips: list[VideoClip] = Field(default_factory=list, max_length=120)


class VideoDocument(Strict):
    version: Literal[1] = 1
    width: int = Field(default=1280, ge=64, le=4096)
    height: int = Field(default=720, ge=64, le=4096)
    fps_num: int = Field(default=30, ge=1, le=120000)
    fps_den: int = Field(default=1, ge=1, le=1001)
    background: str = Field(default="#111111", pattern=r"^#[0-9a-fA-F]{6}$")
    tracks: list[VideoTrack] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def valid_document(self):
        if self.width % 2 or self.height % 2:
            raise ValueError("Output dimensions must be even")
        if not 1 <= self.fps_num / self.fps_den <= 120:
            raise ValueError("Frame rate must be between 1 and 120 fps")
        tracks = [t.id for t in self.tracks]
        clips = [c.id for t in self.tracks for c in t.clips]
        if len(tracks) != len(set(tracks)) or len(clips) != len(set(clips)):
            raise ValueError("Track and clip IDs must be unique")
        if len(clips) > 120:
            raise ValueError("At most 120 clips per project")
        for track in self.tracks:
            ordered = sorted(track.clips, key=lambda c: c.start_us)
            if any(a.start_us + a.duration_us > b.start_us for a, b in zip(ordered, ordered[1:])):
                raise ValueError("Clips on the same track must not overlap; use another track")
        return self

    @property
    def duration_us(self):
        return max((c.start_us + c.duration_us for t in self.tracks for c in t.clips), default=0)


class VideoOperation(Strict):
    op: Literal["add_track", "remove_track", "add_clip", "update_clip", "remove_clip", "split_clip", "settings"]
    track_id: str | None = None
    clip_id: str | None = None
    at_us: int | None = Field(default=None, ge=0, le=MAX_TIME)
    new_id: str | None = Field(default=None, max_length=64)
    values: dict = Field(default_factory=dict)


class VideoWrite(Strict):
    expected_revision: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=128)
    document: VideoDocument
    summary: str = Field(default="Edit timeline", max_length=300)
