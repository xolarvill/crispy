"""Strict contracts for the instruction board document.

The board is the shared surface between the operator and the agents. These
models are the wire and storage contract: the UI writes them, the compiler
reads them, and future board tools operate on them. Keep them explicit —
adding a node type should be a deliberate contract change, not a free-form
``dict``.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


BoardRole = Literal["source", "reference", "style", "output"]
BoardNodeType = Literal["frame", "asset", "region", "note", "text", "sketch", "stage"]
BoardLinkKind = Literal["annotation", "data"]
BoardDataType = Literal["image", "text", "video"]
# Agent-authored notes and regions say why they exist; the UI styles them by it.
BoardIntent = Literal[
    "",
    "direction",
    "explanation",
    "question",
    "finding",
    "change",
    "defect",
    "keep",
    "focus",
]

MAX_ITEMS = 120
MAX_LINKS = 60
MAX_NOTE_CHARS = 2000
MAX_REGION_NOTE_CHARS = 500
MAX_SKETCH_POINTS = 2000


class BoardViewport(BaseModel):
    x: float = 0.0
    y: float = 0.0
    zoom: float = Field(default=1.0, gt=0.01, le=8)


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    parent: str | None = Field(default=None, max_length=64)
    x: float = 0
    y: float = 0
    # "human" for operator work, "agent:<agent_name>" for canvas-tool writes.
    created_by: str = Field(default="human", max_length=128)


class BoardFrameNode(_Node):
    type: Literal["frame"]
    title: str = Field(default="", max_length=160)
    w: float = Field(default=640, gt=0)
    h: float = Field(default=480, gt=0)


class BoardAssetNode(_Node):
    """A pinned run asset. ``mark`` is the numbered badge the compiler refers to."""

    type: Literal["asset"]
    mark: int = Field(ge=1, le=99)
    assetId: str = Field(min_length=1, max_length=64)
    uri: str = Field(min_length=1, max_length=1024)
    role: BoardRole = "source"
    caption: str = Field(default="", max_length=300)
    w: float = Field(gt=0)
    h: float = Field(gt=0)


class BoardRegionNode(_Node):
    """A box in the asset's own 0–1 coordinate space plus one change note."""

    type: Literal["region"]
    assetId: str = Field(min_length=1, max_length=64)
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)
    note: str = Field(min_length=1, max_length=MAX_REGION_NOTE_CHARS)
    intent: BoardIntent = Field(default="")

    @model_validator(mode="after")
    def _inside_asset(self) -> "BoardRegionNode":
        if self.x + self.w > 1.000001 or self.y + self.h > 1.000001:
            raise ValueError("region exceeds the asset bounds")
        return self


class BoardNoteNode(_Node):
    type: Literal["note"]
    text: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)
    w: float = Field(default=240, gt=0)
    h: float = Field(default=120, gt=0)
    intent: BoardIntent = Field(default="")


class BoardTextNode(_Node):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)
    fontSize: float = Field(default=18, gt=0, le=512)


class BoardSketchNode(_Node):
    type: Literal["sketch"]
    points: list[list[float]] = Field(min_length=2, max_length=MAX_SKETCH_POINTS)
    color: str = Field(default="#e5533d", max_length=32)
    width: float = Field(default=2, gt=0, le=64)

    @model_validator(mode="after")
    def _points_are_pairs(self) -> "BoardSketchNode":
        if any(len(point) != 2 for point in self.points):
            raise ValueError("each sketch point must be [x, y]")
        return self


class BoardStageNode(_Node):
    """An executable stage node. P0 only records the binding, it does not run it."""

    type: Literal["stage"]
    stage: str = Field(min_length=1, max_length=64)
    w: float = Field(default=220, gt=0)
    h: float = Field(default=120, gt=0)
    params: dict = Field(default_factory=dict)


BoardNode = Annotated[
    Union[
        BoardFrameNode,
        BoardAssetNode,
        BoardRegionNode,
        BoardNoteNode,
        BoardTextNode,
        BoardSketchNode,
        BoardStageNode,
    ],
    Field(discriminator="type"),
]


class BoardLink(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(min_length=1, max_length=64)
    kind: BoardLinkKind = "annotation"
    from_: str = Field(alias="from", min_length=1, max_length=64)
    to: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=300)
    port: str = Field(default="", max_length=64)
    dataType: BoardDataType | None = None
    created_by: str = Field(default="human", max_length=128)

    @model_validator(mode="after")
    def _data_edges_are_wired(self) -> "BoardLink":
        if self.kind == "data" and not self.port:
            raise ValueError("data links need a port")
        if self.kind == "annotation" and (self.port or self.dataType):
            raise ValueError("annotation links must not declare a port or dataType")
        return self


class BoardScene(BaseModel):
    """Versioned document stored on ``InstructionBoard.document``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    goal: str = Field(default="", max_length=MAX_NOTE_CHARS)
    # The goal is operator direction; agents may only fill an empty goal.
    goal_created_by: str = Field(default="human", max_length=128)
    items: list[BoardNode] = Field(default_factory=list, max_length=MAX_ITEMS)
    links: list[BoardLink] = Field(default_factory=list, max_length=MAX_LINKS)
    viewport: BoardViewport = Field(default_factory=BoardViewport)

    @model_validator(mode="after")
    def _references_are_sound(self) -> "BoardScene":
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate board node id")
        marks = [item.mark for item in self.items if isinstance(item, BoardAssetNode)]
        if len(marks) != len(set(marks)):
            raise ValueError("duplicate asset mark")
        asset_ids = {item.id for item in self.items if isinstance(item, BoardAssetNode)}
        frame_ids = {item.id for item in self.items if isinstance(item, BoardFrameNode)}
        for item in self.items:
            if item.parent is not None:
                if item.parent == item.id:
                    raise ValueError(f"node {item.id} cannot parent itself")
                if item.parent not in frame_ids:
                    raise ValueError(f"node {item.id} points at a missing frame")
            if isinstance(item, BoardRegionNode) and item.assetId not in asset_ids:
                raise ValueError(f"region {item.id} points at a missing asset")
        link_ids = [link.id for link in self.links]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("duplicate board link id")
        known = set(ids)
        for link in self.links:
            if link.from_ not in known or link.to not in known:
                raise ValueError(f"link {link.id} points at a missing node")
        return self


class BoardSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene: BoardScene
    title: str = Field(default="", max_length=160)
    actor: str = Field(default="human", max_length=128)
    expected_revision: int | None = Field(default=None, ge=0)
    snapshot: str = Field(default="", max_length=3_000_000)


class BoardCompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene: BoardScene


class BoardDocCreateRequest(BaseModel):
    """Open a tab: copy ``source_board_id`` or start blank."""

    model_config = ConfigDict(extra="forbid")

    source_board_id: str | None = Field(default=None, max_length=36)
    title: str = Field(default="", max_length=160)
    actor: str = Field(default="human", max_length=128)


class BoardDocRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=160)
