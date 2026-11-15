"""Typed canvas tools for stage agents.

The instruction board is server-authoritative: every tool call mutates an
in-memory copy of the scene and the whole pass lands as **one** revision when
``BoardToolSession.commit()`` runs. That gives the operator one revertable
step per stage run and keeps a failed model round from leaving half-written
nodes behind.

Rules enforced here (not just in prompts):

* agents may only touch nodes they authored; human nodes are read-only,
* node ids are deterministic per ``(stage_task_id, sequence)`` so a retry
  after a revision conflict replays onto the new scene without duplicates,
* data links and stage execution are unavailable — annotation edges only,
* one pass is bounded: 24 writes, 6 pins, 3 composes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models import (
    Artifact,
    InstructionBoard,
    PipelineRun,
    RunVariant,
    StageTask,
    VariantAsset,
)
from app.schemas.boards import (
    MAX_ITEMS,
    MAX_LINKS,
    MAX_NOTE_CHARS,
    MAX_REGION_NOTE_CHARS,
    BoardAssetNode,
    BoardFrameNode,
    BoardIntent,
    BoardLink,
    BoardNoteNode,
    BoardRegionNode,
    BoardRole,
    BoardScene,
    BoardSketchNode,
    BoardStageNode,
    BoardTextNode,
)
from app.services.board_compose import ComposeError, ComposeItem, compose_sheet, image_size
from app.services.boards import (
    BoardConflict,
    BoardError,
    _actor_kind,
    _compile_digest,
    _inside_run_assets_dir,
    _media_uris,
    _record_event,
    create_board,
    get_board,
    latest_board,
    save_board,
)

logger = logging.getLogger(__name__)

MAX_OPS_PER_PASS = 24
MAX_PINS_PER_PASS = 6
MAX_COMPOSES_PER_PASS = 3
MAX_PLACED_PER_CALL = 12
MAX_COMPOSE_ITEMS = 9
MAX_BATCH_CALLS = 24


class BoardToolError(ValueError):
    """A tool call the model should see corrected, not a server failure."""


# ---------------------------------------------------------------------------
# Argument models. The JSON schema of each model is shown to the model, so the
# field descriptions are part of the prompt.
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class GetBoardArgs(_Args):
    include: Literal["summary", "annotations", "full"] = Field(
        default="summary",
        description="summary: node list + digest; annotations: only notes/regions/links; full: every field.",
    )


class GetAnnotationsArgs(_Args):
    pass


class SearchNodesArgs(_Args):
    query: str = Field(min_length=1, max_length=200, description="Case-insensitive text to find.")
    types: list[Literal["asset", "region", "note", "text", "frame", "stage"]] = Field(
        default_factory=list, description="Restrict the search to these node types."
    )


class ListAssetsArgs(_Args):
    kind: Literal["", "image", "video"] = Field(default="", description="Filter by media kind.")
    source: Literal["", "generated", "input"] = Field(
        default="", description="generated: produced by a stage; input: operator upload."
    )
    pinned: bool | None = Field(
        default=None, description="true = already on the board; false = not pinned yet."
    )


class GetScreenshotArgs(_Args):
    items: list[str] = Field(
        default_factory=list,
        max_length=MAX_COMPOSE_ITEMS,
        description="Node ids, asset marks (e.g. '3'), or run media URIs to compose. Empty = saved board snapshot.",
    )
    layout: Literal["row", "grid", "stack"] = "row"
    columns: int | None = Field(default=None, ge=1, le=MAX_COMPOSE_ITEMS)
    labels: bool = True
    title: str = Field(default="", max_length=120)


class GetRunStatusArgs(_Args):
    pass


class CreateNoteArgs(_Args):
    text: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)
    intent: BoardIntent = Field(
        default="", description="direction | explanation | question | finding | change | defect | keep | focus"
    )
    near: str | None = Field(default=None, description="Node id or asset mark to place the note beside.")
    x: float | None = None
    y: float | None = None


class CreateRegionArgs(_Args):
    asset: str = Field(description="Asset node id or its numbered mark, e.g. '2'.")
    box: list[float] = Field(
        min_length=4, max_length=4,
        description="[x, y, w, h] in the asset's own 0–1 space; x+w and y+h must stay ≤ 1.",
    )
    note: str = Field(min_length=1, max_length=MAX_REGION_NOTE_CHARS)
    intent: BoardIntent = Field(default="change")


class CreateEdgeArgs(_Args):
    from_: str = Field(alias="from", description="Source node id or asset mark.")
    to: str = Field(description="Target node id or asset mark.")
    label: str = Field(default="", max_length=300)
    kind: Literal["annotation"] = "annotation"


class PlaceAssetItem(_Args):
    asset_id: str | None = Field(default=None, description="VariantAsset / Artifact id from list_assets.")
    uri: str | None = Field(default=None, description="Run media URI instead of asset_id.")
    caption: str = Field(default="", max_length=300)
    role: BoardRole = "output"
    label: str = Field(default="", max_length=300, description="Edge label when link_from is set.")
    width: float | None = Field(default=None, ge=160, le=640)


class PlaceAssetsArgs(_Args):
    items: list[PlaceAssetItem] = Field(min_length=1, max_length=MAX_PLACED_PER_CALL)
    layout: Literal["row", "grid", "stack"] = "row"
    columns: int | None = Field(default=None, ge=1, le=MAX_PLACED_PER_CALL)
    link_from: str | None = Field(
        default=None, description="Node id or mark to link each pinned asset from (source → output)."
    )
    x: float | None = None
    y: float | None = None


class CreateAssetArgs(_Args):
    asset_id: str | None = None
    uri: str | None = None
    x: float | None = None
    y: float | None = None
    role: BoardRole = "output"
    caption: str = Field(default="", max_length=300)
    link_from: str | None = None


class SetNodePropsArgs(_Args):
    node_id: str
    caption: str | None = Field(default=None, max_length=300)
    role: BoardRole | None = None


class MoveNodesArgs(_Args):
    node_ids: list[str] = Field(min_length=1, max_length=12)
    dx: float | None = None
    dy: float | None = None
    x: float | None = None
    y: float | None = None

    @model_validator(mode="after")
    def _needs_a_target(self) -> "MoveNodesArgs":
        if self.x is None and self.y is None and self.dx is None and self.dy is None:
            raise ValueError("pass x/y or dx/dy")
        return self


class DeleteNodesArgs(_Args):
    node_ids: list[str] = Field(min_length=1, max_length=12)


class SetGoalArgs(_Args):
    text: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)


class CreateBoardArgs(_Args):
    title: str = Field(default="", max_length=160)
    source_board_id: str | None = Field(
        default=None, description="Duplicate this run's tab; omit for a blank tab."
    )


class ComposeArgs(_Args):
    items: list[str] = Field(
        min_length=1, max_length=MAX_COMPOSE_ITEMS,
        description="Node ids, asset marks, or run media URIs, in reading order.",
    )
    layout: Literal["row", "grid", "stack"] = "row"
    columns: int | None = Field(default=None, ge=1, le=MAX_COMPOSE_ITEMS)
    labels: bool = True
    title: str = Field(default="", max_length=120)
    pin: bool = Field(default=True, description="Pin the sheet onto the board as an output asset.")
    caption: str = Field(default="", max_length=300)
    link_from: str | None = Field(default=None, description="Link the pinned sheet from this node.")

    @model_validator(mode="after")
    def _pin_needs_something(self) -> "ComposeArgs":
        return self


class BatchCall(_Args):
    name: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)


class BatchArgs(_Args):
    calls: list[BatchCall] = Field(min_length=1, max_length=MAX_BATCH_CALLS)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    args: type[_Args]
    write: bool
    description: str


TOOL_SPECS: dict[str, ToolSpec] = {
    # Read ------------------------------------------------------------------
    "get_board": ToolSpec(
        "get_board", GetBoardArgs, False,
        "Read the current board: goal, numbered assets, notes, regions, relations and the "
        "compiled digest the stage already received.",
    ),
    "get_annotations": ToolSpec(
        "get_annotations", GetAnnotationsArgs, False,
        "Read only the operator and agent annotations: regions with their asset mark, notes, "
        "questions and annotation links.",
    ),
    "search_nodes": ToolSpec(
        "search_nodes", SearchNodesArgs, False,
        "Search note text, region notes, captions and frame titles. Returns node ids you can act on.",
    ),
    "list_assets": ToolSpec(
        "list_assets", ListAssetsArgs, False,
        "List this run's generated and input media with asset ids, kinds, stages and whether they are pinned.",
    ),
    "get_screenshot": ToolSpec(
        "get_screenshot", GetScreenshotArgs, False,
        "Get an image of the board: the saved snapshot, or a composed sheet of the given nodes. "
        "The image is attached to your next round; it is not pinned.",
    ),
    "get_run_status": ToolSpec(
        "get_run_status", GetRunStatusArgs, False,
        "Read the run's stage status, retries, variant count and winner.",
    ),
    # Write -----------------------------------------------------------------
    "create_note": ToolSpec(
        "create_note", CreateNoteArgs, True,
        "Write a note on the canvas. Use intent=explanation for reasoning, finding for a QA result, "
        "question when the board is ambiguous, direction only when proposing a change.",
    ),
    "create_region": ToolSpec(
        "create_region", CreateRegionArgs, True,
        "Box an area on a pinned asset and explain it. Coordinates are 0–1 of that asset.",
    ),
    "create_edge": ToolSpec(
        "create_edge", CreateEdgeArgs, True,
        "Draw an annotation arrow between two nodes (e.g. output → its source reference).",
    ),
    "place_assets": ToolSpec(
        "place_assets", PlaceAssetsArgs, True,
        "Pin run assets on the canvas in a row/grid/stack and optionally link them from a source node. "
        "This is the main 'show your work' tool.",
    ),
    "create_asset_node": ToolSpec(
        "create_asset_node", CreateAssetArgs, True,
        "Pin one run asset at a precise position.",
    ),
    "set_node_props": ToolSpec(
        "set_node_props", SetNodePropsArgs, True,
        "Change the caption or role of an asset node you created.",
    ),
    "move_nodes": ToolSpec(
        "move_nodes", MoveNodesArgs, True,
        "Move nodes you created, by a delta or to an absolute position.",
    ),
    "delete_nodes": ToolSpec(
        "delete_nodes", DeleteNodesArgs, True,
        "Delete nodes you created earlier in this session. Human nodes and annotated assets are protected.",
    ),
    "set_goal": ToolSpec(
        "set_goal", SetGoalArgs, True,
        "Set the board goal only when the operator has not written one. Never replaces human direction.",
    ),
    "create_board": ToolSpec(
        "create_board", CreateBoardArgs, True,
        "Open a new tab (blank or a duplicate of this run's board). Must be the first write in a pass.",
    ),
    "compose": ToolSpec(
        "compose", ComposeArgs, True,
        "Compose candidates into one labelled comparison sheet, save it as a run image, optionally pin it "
        "and return the image for your next round. Use for A/B comparisons and winner explanations.",
    ),
    "batch": ToolSpec(
        "batch", BatchArgs, False,
        "Run up to 24 tool calls in order. Each result is reported separately; the pass still saves once.",
    ),
}


# ---------------------------------------------------------------------------
# Result + session
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    ok: bool
    data: dict = field(default_factory=dict)
    error: str = ""
    images: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        if not self.ok:
            return {"ok": False, "error": self.error}
        return {"ok": True, **self.data}


@dataclass(frozen=True)
class ToolOp:
    name: str
    args: dict


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:4]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "args"
        parts.append(f"{location}: {error.get('msg', 'invalid value')}")
    return "invalid arguments — " + "; ".join(parts)


def _event_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= 300 else f"{value[:297]}…"
    if isinstance(value, list):
        return [_event_value(item, depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key): _event_value(item, depth + 1) for key, item in list(value.items())[:20]}
    return value


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class BoardToolSession:
    """One pass of canvas tool calls over a run board.

    ``stage_task_id`` seeds deterministic node ids. ``actor`` is the agent name
    (``planning_agent``) and is stored as ``agent:<actor>`` on every write.
    """

    def __init__(
        self,
        db: Session,
        run: PipelineRun,
        *,
        board_id: str | None = None,
        actor: str,
        stage_task_id: str = "",
        board_title: str = "",
    ) -> None:
        self.db = db
        self.run = run
        actor = (actor or "agent").strip() or "agent"
        self.actor = actor if actor.startswith("agent:") else f"agent:{actor}"
        self.stage_task_id = stage_task_id or "manual"
        self._token = hashlib.sha1(
            f"{self.stage_task_id}|{self.actor}".encode("utf-8")
        ).hexdigest()[:6]
        self._allowed_uris: set[str] | None = None

        row: InstructionBoard | None = None
        if board_id:
            row = get_board(db, run.id, board_id)
            if row is None:
                raise BoardToolError("board tab not found")
        else:
            row = latest_board(db, run.id)

        self.board_row = row
        self.board_id = row.id if row else None
        self.title = (row.title if row and row.title else "") or board_title or f"{run.product_code} board"
        base = self._scene_from(row) if row else BoardScene()
        self._base = base.model_copy(deep=True)
        self.scene = base.model_copy(deep=True)
        self.expected_revision = row.revision if row else None
        self.ops: list[ToolOp] = []
        self._seq = 0
        self._pins = 0
        self._composes = 0
        self._shots = 0
        self._scene_dirty = False
        self._tab_created = False

    # -- lifecycle ----------------------------------------------------------

    @staticmethod
    def _scene_from(row: InstructionBoard | None) -> BoardScene:
        if row is None or not row.document:
            return BoardScene()
        try:
            return BoardScene.model_validate(row.document)
        except ValueError as exc:
            raise BoardToolError("this board's document is invalid; ask the operator to reopen it") from exc

    @property
    def revision(self) -> int:
        return int(self.board_row.revision) if self.board_row else 0

    @property
    def pending_writes(self) -> int:
        return len(self.ops)

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._token}_{self._seq:02d}"

    def _restore_counters(self, counters: tuple[int, int, int, int]) -> None:
        self._seq, self._pins, self._composes, self._shots = counters

    def _reset_replay_state(self, scene: BoardScene) -> None:
        self._base = scene.model_copy(deep=True)
        self.scene = scene.model_copy(deep=True)
        self.ops = []
        self._seq = 0
        self._pins = 0
        self._composes = 0
        self._shots = 0
        self._scene_dirty = False

    def _reload_and_replay(self) -> None:
        row = (
            get_board(self.db, self.run.id, self.board_id)
            if self.board_id
            else latest_board(self.db, self.run.id)
        )
        if row is None:
            raise BoardToolError("the board tab disappeared before the write could be saved")
        scene = self._scene_from(row)
        self.board_row = row
        self.board_id = row.id
        self.expected_revision = row.revision
        replay = [op for op in self.ops if op.name != "create_board"]
        self._reset_replay_state(scene)
        for op in replay:
            result = self.execute(op.name, dict(op.args))
            if not result.ok:
                raise BoardToolError(
                    f"could not re-apply {op.name} after a concurrent edit: {result.error}"
                )

    def commit(self, *, summary: str = "") -> InstructionBoard | None:
        """Save every write in this pass as one revision.

        Retries a stale-revision conflict by re-reading the board and replaying
        the recorded ops. An agent pass must never call ``db.commit()`` itself;
        the stage worker owns the transaction.
        """
        if not self._scene_dirty:
            return self.board_row
        attempts = 0
        while True:
            attempts += 1
            try:
                board = save_board(
                    self.db,
                    self.run,
                    self.scene.model_copy(deep=True),
                    board_id=self.board_id,
                    title=self.title,
                    actor=self.actor,
                    expected_revision=self.expected_revision,
                    snapshot="",
                    op="agent_edit",
                    event={
                        "summary": (summary or "").strip()[:500],
                        "stage_task_id": self.stage_task_id,
                        "tools": [op.name for op in self.ops],
                    },
                )
            except BoardConflict:
                if attempts >= 3:
                    raise
                self._reload_and_replay()
                continue
            self.board_row = board
            self.board_id = board.id
            self.expected_revision = board.revision
            self._base = self.scene.model_copy(deep=True)
            for op in self.ops:
                if op.name == "create_board":
                    continue
                _record_event(
                    self.db,
                    board.id,
                    board.revision,
                    self.actor,
                    f"tool:{op.name}",
                    {"args": _event_value(op.args), "stage_task_id": self.stage_task_id},
                )
            self.db.flush()
            self._scene_dirty = False
            return board

    # -- dispatch -----------------------------------------------------------

    def execute(self, name: str, args: dict | None = None) -> ToolResult:
        spec = TOOL_SPECS.get((name or "").strip())
        if spec is None:
            return ToolResult(ok=False, error=f"unknown tool '{name}'")
        try:
            parsed = spec.args.model_validate(args or {})
        except ValidationError as exc:
            return ToolResult(ok=False, error=_format_validation_error(exc))

        if spec.write and len(self.ops) >= MAX_OPS_PER_PASS:
            return ToolResult(
                ok=False,
                error=f"this pass already used its {MAX_OPS_PER_PASS} writes; summarise what you have",
            )

        before = self.scene.model_copy(deep=True) if spec.write else None
        counters = (self._seq, self._pins, self._composes, self._shots)
        try:
            result = getattr(self, f"_tool_{name}")(parsed)
        except BoardToolError as exc:
            if before is not None:
                self.scene = before
                self._restore_counters(counters)
            return ToolResult(ok=False, error=str(exc))
        except (ComposeError, BoardError) as exc:
            if before is not None:
                self.scene = before
                self._restore_counters(counters)
            return ToolResult(ok=False, error=str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("board tool %s failed", name)
            if before is not None:
                self.scene = before
                self._restore_counters(counters)
            return ToolResult(ok=False, error=f"{name} failed: {exc}")

        if spec.write:
            self.ops.append(ToolOp(name, parsed.model_dump(by_alias=True, mode="json")))
            if name != "create_board":
                self._scene_dirty = True
        return result

    # -- shared helpers -----------------------------------------------------

    def _nodes(self):
        return self.scene.items

    def _assets(self) -> list[BoardAssetNode]:
        return [item for item in self.scene.items if isinstance(item, BoardAssetNode)]

    def _find_node(self, ref: str | int):
        text = str(ref).strip()
        for item in self.scene.items:
            if item.id == text:
                return item
            if isinstance(item, BoardLink):
                continue
        if text.isdigit():
            mark = int(text)
            for item in self._assets():
                if item.mark == mark:
                    return item
        raise BoardToolError(f"no node or asset mark '{ref}' on this board")

    def _find_asset(self, ref: str | int) -> BoardAssetNode:
        node = self._find_node(ref)
        if not isinstance(node, BoardAssetNode):
            raise BoardToolError(f"'{ref}' is a {node.type}, not an image asset")
        return node

    def _find_pinned(self, asset_id: str, uri: str) -> BoardAssetNode | None:
        for item in self._assets():
            if item.assetId == asset_id or (uri and item.uri == uri):
                return item
        return None

    def _allowed_media(self) -> set[str]:
        if self._allowed_uris is None:
            self._allowed_uris = _media_uris(self.db, self.run.id)
        return self._allowed_uris

    def _check_uri(self, uri: str) -> None:
        value = (uri or "").strip()
        if not value:
            raise BoardToolError("empty media uri")
        if value in self._allowed_media() or _inside_run_assets_dir(self.run.id, value):
            return
        raise BoardToolError(f"'{value}' is not media from this run")

    def _content_bounds(self) -> tuple[float, float, float, float] | None:
        boxes = [
            (item.x, item.y, item.x + item.w, item.y + item.h)
            for item in self.scene.items
            if isinstance(item, (BoardAssetNode, BoardNoteNode, BoardFrameNode, BoardStageNode))
        ]
        if not boxes:
            return None
        return (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )

    def _stage_label(self) -> str:
        rows = self.db.scalars(
            select(StageTask.stage_name).where(
                StageTask.run_id == self.run.id, StageTask.status == "running"
            )
        ).all()
        return rows[0] if rows else "agent"

    def _require_items(self, count: int = 1) -> None:
        if len(self.scene.items) + count > MAX_ITEMS:
            raise BoardToolError(f"the board is full ({MAX_ITEMS} nodes); ask the operator to clear space")

    def _require_links(self, count: int = 1) -> None:
        if len(self.scene.links) + count > MAX_LINKS:
            raise BoardToolError(f"the board is full ({MAX_LINKS} links)")

    def _next_mark(self) -> int:
        marks = {item.mark for item in self._assets()}
        for mark in range(1, 100):
            if mark not in marks:
                return mark
        raise BoardToolError("all 99 numbered asset slots are used")

    def _require_pin_budget(self, count: int) -> None:
        if self._pins + count > MAX_PINS_PER_PASS:
            raise BoardToolError(
                f"this pass may pin {MAX_PINS_PER_PASS} assets; {MAX_PINS_PER_PASS - self._pins} left"
            )

    def _add_link(self, from_id: str, to_id: str, *, label: str = "") -> BoardLink | None:
        for link in self.scene.links:
            if link.from_ == from_id and link.to == to_id and link.label == label:
                return None
        self._require_links()
        link = BoardLink(
            id=self._next_id("e"),
            kind="annotation",
            **{"from": from_id},
            to=to_id,
            label=label,
            created_by=self.actor,
        )
        self.scene.links.append(link)
        return link

    def _node_brief(self, item, include: str = "summary") -> dict:
        data: dict = {
            "id": item.id,
            "type": item.type,
            "created_by": item.created_by,
        }
        if isinstance(item, BoardAssetNode):
            data.update(
                mark=item.mark,
                asset_id=item.assetId,
                role=item.role,
                caption=item.caption,
                box=[round(item.x, 1), round(item.y, 1), round(item.w, 1), round(item.h, 1)],
            )
            if include == "full":
                data["uri"] = item.uri
        elif isinstance(item, BoardRegionNode):
            mark = next(
                (asset.mark for asset in self._assets() if asset.id == item.assetId), None
            )
            data.update(
                asset_id=item.assetId,
                asset_mark=mark,
                box=[round(item.x, 3), round(item.y, 3), round(item.w, 3), round(item.h, 3)],
                note=item.note,
                intent=item.intent,
            )
        elif isinstance(item, (BoardNoteNode, BoardTextNode)):
            data.update(text=item.text, intent=getattr(item, "intent", ""), box=[item.x, item.y])
        elif isinstance(item, BoardFrameNode):
            data.update(title=item.title, box=[item.x, item.y, item.w, item.h])
        elif isinstance(item, BoardStageNode):
            data.update(stage=item.stage, params=item.params, box=[item.x, item.y])
        elif isinstance(item, BoardSketchNode):
            data.update(points=len(item.points), box=[item.x, item.y])
        return data

    def _link_brief(self, link: BoardLink) -> dict:
        return {
            "id": link.id,
            "from": link.from_,
            "to": link.to,
            "kind": link.kind,
            "label": link.label,
            "created_by": link.created_by,
        }

    def _resolve_media_ref(self, ref: str) -> str:
        """Node id, mark, or run URI → URI."""
        text = (ref or "").strip()
        if not text:
            raise BoardToolError("empty item reference")
        for item in self.scene.items:
            if item.id == text:
                if isinstance(item, BoardAssetNode):
                    return item.uri
                raise BoardToolError(f"'{ref}' is a {item.type}, not an image asset")
        if text.isdigit():
            asset = self._find_asset(int(text))
            return asset.uri
        row = self.db.get(VariantAsset, text)
        if row is not None and row.run_id == self.run.id and row.uri:
            return row.uri
        artifact = self.db.get(Artifact, text)
        if artifact is not None and artifact.run_id == self.run.id and artifact.uri:
            return artifact.uri
        self._check_uri(text)
        return text

    def _resolve_asset(self, item: PlaceAssetItem) -> dict:
        if item.asset_id:
            node = next((node for node in self.scene.items if node.id == item.asset_id), None)
            if isinstance(node, BoardAssetNode):
                return {
                    "asset_id": node.assetId,
                    "uri": node.uri,
                    "kind": "video" if node.uri.lower().endswith((".mp4", ".mov", ".m4v", ".webm")) else "image",
                    "stage": "",
                }
            row = self.db.get(VariantAsset, item.asset_id)
            if row is not None and row.run_id == self.run.id and row.uri:
                return {
                    "asset_id": row.id,
                    "uri": row.uri,
                    "kind": "video" if row.asset_type == "video" else "image",
                    "stage": row.stage_name,
                }
            artifact = self.db.get(Artifact, item.asset_id)
            if artifact is not None and artifact.run_id == self.run.id and artifact.uri:
                return {
                    "asset_id": artifact.id,
                    "uri": artifact.uri,
                    "kind": "video"
                    if artifact.artifact_type == "generated_video"
                    or artifact.uri.lower().endswith((".mp4", ".mov", ".m4v", ".webm"))
                    else "image",
                    "stage": artifact.stage_name,
                }
            raise BoardToolError(
                f"'{item.asset_id}' is not a run asset; call list_assets for valid ids"
            )
        if item.uri:
            self._check_uri(item.uri)
            uri = item.uri.strip()
            return {
                "asset_id": Path(uri).stem[:64] or Path(uri).name[:64],
                "uri": uri,
                "kind": "video" if uri.lower().endswith((".mp4", ".mov", ".m4v", ".webm")) else "image",
                "stage": "",
            }
        raise BoardToolError("each item needs asset_id or uri")

    def _display_size(self, uri: str, kind: str, width: float | None) -> tuple[float, float]:
        target_w = _clamp(float(width or 320), 160, 640)
        dims = image_size(uri, self.run.id) if kind == "image" else None
        if dims:
            ratio = dims[1] / dims[0] if dims[0] else 0.75
            return round(target_w, 1), round(_clamp(target_w * ratio, 90, 640), 1)
        if kind == "video":
            return round(target_w, 1), round(target_w * 9 / 16, 1)
        return round(target_w, 1), round(target_w * 0.75, 1)

    def _place_uri(
        self,
        *,
        asset_id: str,
        uri: str,
        kind: str,
        role: str,
        caption: str,
        width: float | None,
        x: float,
        y: float,
    ) -> BoardAssetNode:
        self._require_items()
        node_w, node_h = self._display_size(uri, kind, width)
        node = BoardAssetNode(
            id=self._next_id("a"),
            type="asset",
            mark=self._next_mark(),
            assetId=asset_id[:64],
            uri=uri,
            role=role,
            caption=caption[:300],
            x=round(x, 1),
            y=round(y, 1),
            w=node_w,
            h=node_h,
            created_by=self.actor,
        )
        self.scene.items.append(node)
        return node

    # ===================================================================
    # Read tools
    # ===================================================================

    def _tool_get_board(self, args: GetBoardArgs) -> ToolResult:
        include = args.include
        nodes = []
        for item in self.scene.items:
            if include == "annotations" and not isinstance(
                item, (BoardRegionNode, BoardNoteNode, BoardTextNode)
            ):
                continue
            nodes.append(self._node_brief(item, include))
        links = [self._link_brief(link) for link in self.scene.links]
        digest = _compile_digest(self.scene, self.revision)
        return ToolResult(
            ok=True,
            data={
                "board_id": self.board_id,
                "title": self.title,
                "revision": self.revision,
                "goal": self.scene.goal,
                "goal_created_by": self.scene.goal_created_by,
                "nodes": nodes,
                "links": links,
                "digest": digest,
                "pending_writes": self.pending_writes,
            },
        )

    def _tool_get_annotations(self, _args: GetAnnotationsArgs) -> ToolResult:
        regions, notes, questions = [], [], []
        marks = {item.id: item.mark for item in self._assets()}
        for item in self.scene.items:
            if isinstance(item, BoardRegionNode):
                regions.append(
                    {
                        "node_id": item.id,
                        "asset_id": item.assetId,
                        "asset_mark": marks.get(item.assetId),
                        "box": [round(item.x, 3), round(item.y, 3), round(item.w, 3), round(item.h, 3)],
                        "note": item.note,
                        "intent": item.intent,
                        "created_by": item.created_by,
                    }
                )
                continue
            if not isinstance(item, (BoardNoteNode, BoardTextNode)):
                continue
            entry = {
                "node_id": item.id,
                "type": item.type,
                "text": item.text,
                "intent": getattr(item, "intent", ""),
                "created_by": item.created_by,
            }
            (questions if entry["intent"] == "question" else notes).append(entry)
        return ToolResult(
            ok=True,
            data={
                "goal": self.scene.goal,
                "regions": regions,
                "notes": notes,
                "questions": questions,
                "links": [
                    self._link_brief(link)
                    for link in self.scene.links
                    if not link.kind == "data"
                ],
            },
        )

    def _tool_search_nodes(self, args: SearchNodesArgs) -> ToolResult:
        needle = args.query.strip().lower()
        wanted = set(args.types)
        matches = []
        for item in self.scene.items:
            if wanted and item.type not in wanted:
                continue
            text = ""
            if isinstance(item, BoardAssetNode):
                text = " ".join(part for part in (item.assetId, item.caption, item.role) if part)
            elif isinstance(item, BoardRegionNode):
                text = item.note
            elif isinstance(item, (BoardNoteNode, BoardTextNode)):
                text = item.text
            elif isinstance(item, BoardFrameNode):
                text = item.title
            elif isinstance(item, BoardStageNode):
                text = item.stage
            if needle not in text.lower():
                continue
            brief = self._node_brief(item, "summary")
            brief["text"] = text[:400]
            matches.append(brief)
        return ToolResult(ok=True, data={"query": args.query, "matches": matches[:40], "count": len(matches)})

    def _tool_list_assets(self, args: ListAssetsArgs) -> ToolResult:
        pinned_uris = {item.uri for item in self._assets()}
        rows: list[dict] = []
        generated = self.db.execute(
            select(
                VariantAsset.id,
                VariantAsset.uri,
                VariantAsset.asset_type,
                VariantAsset.stage_name,
                VariantAsset.prompt_summary,
                RunVariant.variant_id,
            )
            .join(RunVariant, RunVariant.id == VariantAsset.run_variant_id)
            .where(
                VariantAsset.run_id == self.run.id,
                VariantAsset.uri.is_not(None),
                VariantAsset.failure_category.is_(None),
            )
            .order_by(VariantAsset.created_at.asc())
        ).mappings()
        for row in generated:
            uri = str(row["uri"] or "").strip()
            if not uri or "_generation_error." in uri:
                continue
            kind = "video" if row["asset_type"] == "video" else "image"
            rows.append(
                {
                    "asset_id": row["id"],
                    "uri": uri,
                    "kind": kind,
                    "source": "generated",
                    "stage": row["stage_name"],
                    "variant_id": row["variant_id"],
                    "caption": (row["prompt_summary"] or "")[:160],
                    "pinned": uri in pinned_uris,
                }
            )
        inputs = self.db.execute(
            select(Artifact.id, Artifact.uri, Artifact.artifact_type, Artifact.stage_name)
            .where(Artifact.run_id == self.run.id)
            .order_by(Artifact.created_at.asc())
        ).mappings()
        for row in inputs:
            uri = str(row["uri"] or "").strip()
            if not uri or uri.startswith("data:"):
                continue
            artifact_type = row["artifact_type"]
            if artifact_type not in ("input_file", "generated_image", "generated_video", "storyboard_frame"):
                continue
            if artifact_type == "input_file" and not uri.lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".mp4", ".mov", ".m4v", ".webm")
            ):
                continue
            kind = (
                "video"
                if artifact_type == "generated_video"
                or uri.lower().endswith((".mp4", ".mov", ".m4v", ".webm"))
                else "image"
            )
            rows.append(
                {
                    "asset_id": row["id"],
                    "uri": uri,
                    "kind": kind,
                    "source": "input" if artifact_type == "input_file" else "generated",
                    "stage": row["stage_name"],
                    "variant_id": "",
                    "caption": "",
                    "pinned": uri in pinned_uris,
                }
            )

        if args.kind:
            rows = [row for row in rows if row["kind"] == args.kind]
        if args.source:
            rows = [row for row in rows if row["source"] == args.source]
        if args.pinned is not None:
            rows = [row for row in rows if row["pinned"] is args.pinned]
        return ToolResult(ok=True, data={"assets": rows[:80], "count": len(rows)})

    def _tool_get_screenshot(self, args: GetScreenshotArgs) -> ToolResult:
        if args.items:
            return self._compose(
                args.items,
                layout=args.layout,
                columns=args.columns,
                labels=args.labels,
                title=args.title,
                pin=False,
                caption="",
                link_from=None,
                stem_prefix="shot",
            )
        snapshot = self.board_row.snapshot_uri if self.board_row else ""
        dims = image_size(snapshot, self.run.id) if snapshot else None
        if snapshot and dims:
            return ToolResult(
                ok=True,
                data={
                    "uri": snapshot,
                    "width": dims[0],
                    "height": dims[1],
                    "source": "snapshot",
                    "stale": self._scene_dirty,
                    "note": "Compose specific nodes for a closer look; the snapshot is the last human save.",
                },
                images=[snapshot],
            )
        assets = [item for item in self._assets() if image_size(item.uri, self.run.id)]
        if not assets:
            raise BoardToolError(
                "no rendered board image yet — pin or compose images first, then call get_screenshot"
            )
        refs = [item.id for item in assets[:MAX_COMPOSE_ITEMS]]
        result = self._compose(
            refs,
            layout="grid",
            columns=None,
            labels=True,
            title=args.title or "Board images",
            pin=False,
            caption="",
            link_from=None,
            stem_prefix="shot",
        )
        if result.ok:
            result.data["source"] = "composed"
        return result

    def _tool_get_run_status(self, _args: GetRunStatusArgs) -> ToolResult:
        tasks = self.db.execute(
            select(
                StageTask.stage_name,
                StageTask.status,
                StageTask.attempt,
                StageTask.error_message,
            )
            .where(StageTask.run_id == self.run.id)
            .order_by(StageTask.created_at.asc())
        ).mappings()
        variants = self.db.execute(
            select(RunVariant.variant_id, RunVariant.current_score, RunVariant.is_winner)
            .where(RunVariant.run_id == self.run.id)
        ).mappings()
        ranked = sorted(
            (row for row in variants if row["current_score"] is not None),
            key=lambda row: row["current_score"],
            reverse=True,
        )
        return ToolResult(
            ok=True,
            data={
                "run": {
                    "id": self.run.id,
                    "status": self.run.status,
                    "stage": self.run.current_stage,
                    "pipeline_mode": self.run.pipeline_mode,
                    "approval_mode": self.run.approval_mode,
                    "variant_count": self.run.variant_count,
                },
                "stages": [
                    {
                        "stage": row["stage_name"],
                        "status": row["status"],
                        "attempt": row["attempt"],
                        "error": (row["error_message"] or "")[:200],
                    }
                    for row in tasks
                ],
                "variants": {
                    "count": len(list(variants)),
                    "winner": next((row["variant_id"] for row in variants if row["is_winner"]), None),
                    "top": [
                        {"variant_id": row["variant_id"], "score": row["current_score"]}
                        for row in ranked[:3]
                    ],
                },
                "board": {"revision": self.revision, "title": self.title},
            },
        )

    # ===================================================================
    # Write tools
    # ===================================================================

    def _tool_create_note(self, args: CreateNoteArgs) -> ToolResult:
        self._require_items()
        note_text = args.text.strip()
        if not note_text:
            raise BoardToolError("note text is empty")
        if args.x is not None or args.y is not None:
            x = float(args.x or 0)
            y = float(args.y or 0)
        elif args.near:
            anchor = self._find_node(args.near)
            x = anchor.x + (getattr(anchor, "w", 240) or 240) + 24
            y = anchor.y
        else:
            bounds = self._content_bounds()
            if bounds:
                x, y = bounds[0], bounds[3] + 32
            else:
                x, y = 40, 40
        height = _clamp(60 + 22 * math.ceil(len(note_text) / 38), 120, 480)
        node = BoardNoteNode(
            id=self._next_id("n"),
            type="note",
            text=note_text,
            intent=args.intent,
            x=round(x, 1),
            y=round(y, 1),
            w=280,
            h=height,
            created_by=self.actor,
        )
        self.scene.items.append(node)
        return ToolResult(
            ok=True,
            data={"node_id": node.id, "type": "note", "intent": node.intent, "box": [node.x, node.y, node.w, node.h]},
        )

    def _tool_create_region(self, args: CreateRegionArgs) -> ToolResult:
        asset = self._find_asset(args.asset)
        x, y, w, h = (round(float(value), 3) for value in args.box)
        if w <= 0 or h <= 0:
            raise BoardToolError("region width and height must be positive")
        x = _clamp(x, 0, 0.999)
        y = _clamp(y, 0, 0.999)
        w = _clamp(w, 0.001, 1 - x)
        h = _clamp(h, 0.001, 1 - y)
        node = BoardRegionNode(
            id=self._next_id("r"),
            type="region",
            assetId=asset.id,
            x=x,
            y=y,
            w=w,
            h=h,
            note=args.note.strip(),
            intent=args.intent,
            created_by=self.actor,
        )
        self.scene.items.append(node)
        return ToolResult(
            ok=True,
            data={
                "node_id": node.id,
                "asset_mark": asset.mark,
                "box": [node.x, node.y, node.w, node.h],
                "intent": node.intent,
            },
        )

    def _tool_create_edge(self, args: CreateEdgeArgs) -> ToolResult:
        source = self._find_node(args.from_)
        target = self._find_node(args.to)
        if source.id == target.id:
            raise BoardToolError("an edge needs two different nodes")
        link = self._add_link(source.id, target.id, label=args.label.strip())
        if link is None:
            raise BoardToolError("that relation already exists")
        return ToolResult(ok=True, data={"link_id": link.id, "from": link.from_, "to": link.to})

    def _tool_place_assets(self, args: PlaceAssetsArgs) -> ToolResult:
        resolved = [self._resolve_asset(item) for item in args.items]
        fresh: list[tuple[dict, PlaceAssetItem]] = []
        already: list[BoardAssetNode] = []
        for asset, item in zip(resolved, args.items):
            existing = self._find_pinned(asset["asset_id"], asset["uri"])
            if existing is not None:
                already.append(existing)
            else:
                fresh.append((asset, item))
        self._require_pin_budget(len(fresh))

        source_node = self._find_node(args.link_from) if args.link_from else None
        bounds = self._content_bounds()
        if args.x is not None or args.y is not None:
            origin_x = args.x if args.x is not None else (bounds[0] if bounds else 40)
            origin_y = args.y if args.y is not None else (bounds[1] if bounds else 40)
        elif source_node is not None:
            origin_x = source_node.x + (getattr(source_node, "w", 320) or 320) + 96
            origin_y = source_node.y
        elif bounds:
            origin_x, origin_y = bounds[2] + 96, bounds[1]
        else:
            origin_x, origin_y = 40, 40

        count = len(fresh)
        if args.columns:
            columns = min(args.columns, count)
        elif args.layout == "row":
            columns = count
        elif args.layout == "stack":
            columns = 1
        else:
            columns = max(1, math.ceil(math.sqrt(count)))

        placed: list[BoardAssetNode] = []
        x, y = float(origin_x), float(origin_y)
        row_height = 0.0
        for index, (asset, item) in enumerate(fresh):
            node_w, node_h = self._display_size(asset["uri"], asset["kind"], item.width)
            if index and index % columns == 0:
                x = float(origin_x)
                y += row_height + 32
                row_height = 0.0
            node = self._place_uri(
                asset_id=asset["asset_id"],
                uri=asset["uri"],
                kind=asset["kind"],
                role=item.role,
                caption=item.caption,
                width=item.width,
                x=x,
                y=y,
            )
            self._pins += 1
            placed.append(node)
            row_height = max(row_height, node_h)
            x += node_w + 32

        links: list[dict] = []
        if source_node is not None:
            for node, (_, item) in zip(placed, fresh):
                link = self._add_link(source_node.id, node.id, label=item.label.strip())
                if link is not None:
                    links.append({"link_id": link.id, "from": link.from_, "to": link.to})

        return ToolResult(
            ok=True,
            data={
                "nodes": [
                    {
                        "node_id": node.id,
                        "mark": node.mark,
                        "asset_id": node.assetId,
                        "uri": node.uri,
                        "box": [node.x, node.y, node.w, node.h],
                    }
                    for node in placed
                ],
                "already_pinned": [
                    {"node_id": node.id, "mark": node.mark, "asset_id": node.assetId}
                    for node in already
                ],
                "links": links,
                "pins_left": MAX_PINS_PER_PASS - self._pins,
            },
        )

    def _tool_create_asset_node(self, args: CreateAssetArgs) -> ToolResult:
        item = PlaceAssetItem(
            asset_id=args.asset_id,
            uri=args.uri,
            caption=args.caption,
            role=args.role,
        )
        asset = self._resolve_asset(item)
        existing = self._find_pinned(asset["asset_id"], asset["uri"])
        if existing is not None:
            return ToolResult(
                ok=True,
                data={"node_id": existing.id, "mark": existing.mark, "already_pinned": True},
            )
        self._require_pin_budget(1)
        x, y = args.x, args.y
        if x is None or y is None:
            bounds = self._content_bounds()
            base_x = bounds[2] + 96 if bounds else 40
            base_y = bounds[1] if bounds else 40
            x = base_x if x is None else x
            y = base_y if y is None else y
        node = self._place_uri(
            asset_id=asset["asset_id"],
            uri=asset["uri"],
            kind=asset["kind"],
            role=args.role,
            caption=args.caption,
            width=None,
            x=float(x),
            y=float(y),
        )
        self._pins += 1
        link = None
        if args.link_from:
            source = self._find_node(args.link_from)
            link = self._add_link(source.id, node.id)
        return ToolResult(
            ok=True,
            data={
                "node_id": node.id,
                "mark": node.mark,
                "asset_id": node.assetId,
                "uri": node.uri,
                "box": [node.x, node.y, node.w, node.h],
                "link_id": link.id if link else None,
            },
        )

    def _agent_node(self, node_id: str):
        node = self._find_node(node_id)
        if _actor_kind(node.created_by) != "agent":
            raise BoardToolError(f"'{node_id}' belongs to the operator and is read-only")
        return node

    def _tool_set_node_props(self, args: SetNodePropsArgs) -> ToolResult:
        node = self._agent_node(args.node_id)
        if not isinstance(node, BoardAssetNode):
            raise BoardToolError("only asset nodes have a caption and role")
        if args.caption is not None:
            node.caption = args.caption.strip()[:300]
        if args.role is not None:
            node.role = args.role
        return ToolResult(
            ok=True,
            data={"node_id": node.id, "caption": node.caption, "role": node.role},
        )

    def _tool_move_nodes(self, args: MoveNodesArgs) -> ToolResult:
        moved: list[dict] = []
        for node_id in args.node_ids:
            node = self._agent_node(node_id)
            if isinstance(node, BoardRegionNode):
                raise BoardToolError("regions live in the asset's own space; delete and recreate instead")
            if args.x is not None:
                node.x = args.x
            elif args.dx is not None:
                node.x += args.dx
            if args.y is not None:
                node.y = args.y
            elif args.dy is not None:
                node.y += args.dy
            moved.append({"node_id": node.id, "x": node.x, "y": node.y})
        return ToolResult(ok=True, data={"moved": moved})

    def _tool_delete_nodes(self, args: DeleteNodesArgs) -> ToolResult:
        doomed = [self._agent_node(node_id) for node_id in args.node_ids]
        doomed_ids = {node.id for node in doomed}
        for link in self.scene.links:
            if link.from_ in doomed_ids or link.to in doomed_ids:
                if _actor_kind(link.created_by) != "agent":
                    raise BoardToolError("an operator relation uses this node; it cannot be deleted")
        for item in self.scene.items:
            if isinstance(item, BoardRegionNode) and item.assetId in doomed_ids:
                if _actor_kind(item.created_by) != "agent":
                    raise BoardToolError("an operator marking uses this asset; it cannot be deleted")
        self.scene.items = [item for item in self.scene.items if item.id not in doomed_ids]
        self.scene.links = [
            link for link in self.scene.links if link.from_ not in doomed_ids and link.to not in doomed_ids
        ]
        return ToolResult(ok=True, data={"deleted": sorted(doomed_ids)})

    def _tool_set_goal(self, args: SetGoalArgs) -> ToolResult:
        if self.scene.goal and _actor_kind(self.scene.goal_created_by) == "human":
            raise BoardToolError("the operator already wrote the goal; do not replace it")
        self.scene.goal = args.text.strip()
        self.scene.goal_created_by = self.actor
        return ToolResult(ok=True, data={"goal": self.scene.goal})

    def _tool_create_board(self, args: CreateBoardArgs) -> ToolResult:
        if self.ops:
            raise BoardToolError("create the tab before any other write in a pass")
        source = None
        if args.source_board_id:
            source = get_board(self.db, self.run.id, args.source_board_id)
            if source is None:
                raise BoardToolError("source board tab not found")
        title = args.title.strip() or f"{self.run.product_code} {self._stage_label()}"
        board = create_board(self.db, self.run, source=source, title=title, actor=self.actor)
        self.board_row = board
        self.board_id = board.id
        self.title = board.title or title
        self.expected_revision = board.revision
        scene = self._scene_from(board)
        self._base = scene.model_copy(deep=True)
        self.scene = scene.model_copy(deep=True)
        self._tab_created = True
        return ToolResult(
            ok=True,
            data={"board_id": board.id, "title": board.title, "revision": board.revision},
        )

    def _tool_compose(self, args: ComposeArgs) -> ToolResult:
        return self._compose(
            args.items,
            layout=args.layout,
            columns=args.columns,
            labels=args.labels,
            title=args.title,
            pin=args.pin,
            caption=args.caption,
            link_from=args.link_from,
            stem_prefix="sheet",
        )

    def _compose(
        self,
        refs: list[str],
        *,
        layout: str,
        columns: int | None,
        labels: bool,
        title: str,
        pin: bool,
        caption: str,
        link_from: str | None,
        stem_prefix: str,
    ) -> ToolResult:
        if self._composes >= MAX_COMPOSES_PER_PASS:
            raise BoardToolError(
                f"this pass may compose {MAX_COMPOSES_PER_PASS} images; summarise with what you have"
            )
        items: list[ComposeItem] = []
        for ref in refs:
            uri = self._resolve_media_ref(ref)
            label = ""
            text = str(ref).strip()
            node = next((item for item in self.scene.items if item.id == text), None)
            if isinstance(node, BoardAssetNode):
                label = f"{node.mark}. {node.caption}" if node.caption else f"{node.mark}"
            elif text.isdigit():
                asset = self._find_asset(int(text))
                label = f"{asset.mark}. {asset.caption}" if asset.caption else f"{asset.mark}"
            else:
                label = Path(uri).stem.replace("_", " ")[:40]
            items.append(ComposeItem(uri=uri, label=label))

        if pin:
            self._require_pin_budget(1)
            self._require_items()
        self._composes += 1
        self._shots += 1
        board_key = self.board_id or "pending"
        result = compose_sheet(
            items,
            run_id=self.run.id,
            board_id=board_key,
            stem=f"{stem_prefix}_{self._token}_{self._shots:02d}",
            layout=layout,
            columns=columns,
            labels=labels,
            title=title,
        )
        data: dict = {
            "uri": result.path,
            "width": result.width,
            "height": result.height,
            "count": result.count,
        }
        if pin:
            # Place the sheet under the images it compares, never on top of
            # them; compute the bounds before the sheet joins the scene.
            bounds = self._content_bounds()
            node = self._place_uri(
                asset_id=f"compose_{self._token}_{self._shots:02d}",
                uri=result.path,
                kind="image",
                role="output",
                caption=caption or title or "Comparison sheet",
                width=360,
                x=bounds[0] if bounds else 40,
                y=(bounds[3] + 48) if bounds else 40,
            )
            self._pins += 1
            data.update({"node_id": node.id, "mark": node.mark})
            if link_from:
                source = self._find_node(link_from)
                link = self._add_link(source.id, node.id, label=caption or title)
                if link is not None:
                    data["link_id"] = link.id
        return ToolResult(ok=True, data=data, images=[result.path])

    def _tool_batch(self, args: BatchArgs) -> ToolResult:
        results: list[dict] = []
        for call in args.calls:
            if call.name == "batch":
                results.append({"tool": "batch", "ok": False, "error": "batch cannot nest"})
                continue
            result = self.execute(call.name, call.args)
            results.append({"tool": call.name, "result": result.as_dict()})
        return ToolResult(ok=True, data={"results": results})


def tool_catalog() -> list[dict]:
    """Compact catalog for prompts and the HTTP surface."""
    catalog = []
    for spec in TOOL_SPECS.values():
        catalog.append(
            {
                "name": spec.name,
                "write": spec.write,
                "description": spec.description,
                "args": spec.args.model_json_schema(),
            }
        )
    return catalog


def catalog_json() -> str:
    return json.dumps(tool_catalog(), ensure_ascii=False, separators=(",", ":"))
