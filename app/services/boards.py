"""Instruction board service.

One document, three consumers:

* the UI writes it through the API,
* the in-process stage runtime reads a compiled bundle,
* future board tools (``boardwright``) will read and write the same document.

Everything that decides what an agent sees lives here so those three paths
cannot drift apart.
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data.models import (
    InstructionBoard,
    InstructionBoardEvent,
    InstructionBoardRevision,
    PipelineRun,
)
from app.schemas.boards import (
    BoardAssetNode,
    BoardNoteNode,
    BoardRegionNode,
    BoardScene,
    BoardStageNode,
    BoardTextNode,
)
from app.services.run_media import run_media_summaries

logger = logging.getLogger(__name__)

MAX_SNAPSHOT_EDGE = 4096
MAX_DIGEST_CHARS = 2000
BOARD_STAGE_SCOPE_STAGES = {
    "planning",
    "copy_image_generation",
    "video_scripting",
    "storyboard_image_generation",
}

_HEADERS = ("data:image/png;base64", "data:image/jpeg;base64")


class BoardError(ValueError):
    """Validation or state error the API should map to a 4xx response."""


class BoardConflict(BoardError):
    """The caller's expected revision is stale."""


def circled(mark: int) -> str:
    if 1 <= mark <= 20:
        return chr(0x2460 + mark - 1)
    return f"[{mark}]"


def latest_board(db: Session, run_id: str) -> InstructionBoard | None:
    return db.scalar(
        select(InstructionBoard)
        .where(InstructionBoard.run_id == run_id)
        .order_by(InstructionBoard.updated_at.desc(), InstructionBoard.created_at.desc())
        .limit(1)
    )


def _media_uris(db: Session, run_id: str) -> set[str]:
    summary = run_media_summaries(db, [run_id], limit=None).get(run_id, {})
    return {str(item["uri"]) for item in summary.get("media_previews", []) if item.get("uri")}


def _inside_run_assets_dir(run_id: str, uri: str) -> bool:
    requested = Path(uri).expanduser()
    if not requested.is_absolute():
        requested = (Path.cwd() / requested).resolve()
    run_dir = (get_settings().assets_dir / run_id).resolve()
    try:
        requested.relative_to(run_dir)
    except ValueError:
        return False
    return True


def validate_scene_assets(db: Session, run: PipelineRun, scene: BoardScene) -> None:
    allowed = _media_uris(db, run.id)
    for item in scene.items:
        if not isinstance(item, BoardAssetNode):
            continue
        if item.uri in allowed or _inside_run_assets_dir(run.id, item.uri):
            continue
        raise BoardError(f"asset {item.assetId} is not part of this run")


def _decode_snapshot(value: str) -> Image.Image:
    try:
        header, data = value.split(",", 1)
        if header not in _HEADERS:
            raise ValueError("unsupported snapshot encoding")
        raw = base64.b64decode(data, validate=True)
        image = Image.open(io.BytesIO(raw))
        image.load()
        if image.format not in ("PNG", "JPEG") or max(image.size) > MAX_SNAPSHOT_EDGE:
            raise ValueError("unsupported snapshot image")
        return image
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        raise BoardError("invalid instruction board snapshot") from exc


def _write_snapshot(run_id: str, revision: int, value: str) -> str:
    image = _decode_snapshot(value).convert("RGB")
    target_dir = get_settings().assets_dir / "boards" / run_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"r{revision}.png"
    image.save(target, "PNG", optimize=True)
    return str(target)


def _record_event(db: Session, board_id: str, revision: int, actor: str, op: str, payload: dict) -> None:
    db.add(
        InstructionBoardEvent(
            board_id=board_id,
            revision=revision,
            actor=actor or "human",
            op=op,
            payload=payload,
        )
    )


def _actor_kind(actor: str) -> str:
    return "human" if (actor or "").lower() in {"human", "you", "operator"} else "agent"


def _change_summary(previous: dict | None, scene: BoardScene) -> dict:
    """What a save changed, in counts the activity log can show in one line."""
    summary: dict = {"items": len(scene.items), "links": len(scene.links)}
    if not previous:
        return summary
    try:
        before = BoardScene.model_validate(previous)
    except ValueError:
        return summary
    added = len({item.id for item in scene.items} - {item.id for item in before.items})
    added += len({link.id for link in scene.links} - {link.id for link in before.links})
    removed = len({item.id for item in before.items} - {item.id for item in scene.items})
    removed += len({link.id for link in before.links} - {link.id for link in scene.links})
    before_items = {item.id: item for item in before.items}
    before_links = {link.id: link for link in before.links}
    changed = sum(
        1
        for item in scene.items
        if (old := before_items.get(item.id)) is not None and old != item
    )
    changed += sum(
        1
        for link in scene.links
        if (old := before_links.get(link.id)) is not None and old != link
    )
    summary.update({"added": added, "removed": removed, "changed": changed})
    return summary


def record_board_read(
    db: Session,
    run: PipelineRun,
    *,
    actor: str,
    revision: int,
    payload: dict,
) -> None:
    """Log that an agent consumed this board on a stage run."""
    board = latest_board(db, run.id)
    if board is None:
        return
    _record_event(db, board.id, int(revision or board.revision), actor, "read", payload)
    db.flush()


def recent_board_events(db: Session, board_id: str, *, limit: int = 40) -> list[InstructionBoardEvent]:
    return list(
        db.scalars(
            select(InstructionBoardEvent)
            .where(InstructionBoardEvent.board_id == board_id)
            .order_by(InstructionBoardEvent.created_at.desc())
            .limit(limit)
        ).all()
    )


def board_event_payload(row: InstructionBoardEvent) -> dict:
    return {
        "id": row.id,
        "revision": row.revision,
        "actor": row.actor,
        "actor_kind": _actor_kind(row.actor),
        "op": row.op,
        "payload": row.payload or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def save_board(
    db: Session,
    run: PipelineRun,
    scene: BoardScene,
    *,
    title: str = "",
    actor: str = "human",
    expected_revision: int | None = None,
    snapshot: str = "",
    op: str = "",
    event: dict | None = None,
) -> InstructionBoard:
    """Store a new revision of the run's board.

    ``expected_revision`` is the revision the caller edited. A missing value
    means "create only"; a stale value is a conflict. That keeps concurrent
    human and agent writes from silently overwriting each other.
    """
    validate_scene_assets(db, run, scene)
    existing = latest_board(db, run.id)

    if existing is None:
        if expected_revision not in (None, 0):
            raise BoardConflict("board does not exist yet")
        revision = 1
    else:
        if expected_revision is None:
            raise BoardConflict("expected_revision is required for an existing board")
        if expected_revision != existing.revision:
            raise BoardConflict(
                f"board moved from revision {expected_revision} to {existing.revision}"
            )
        revision = existing.revision + 1

    snapshot_uri = _write_snapshot(run.id, revision, snapshot) if snapshot else ""
    document = scene.model_dump(by_alias=True, mode="json")
    event_payload = event or _change_summary(existing.document if existing else None, scene)

    if existing is None:
        board = InstructionBoard(
            workspace_id=run.workspace_id,
            run_id=run.id,
            stage_scope=None,
            title=title,
            revision=revision,
            document=document,
            snapshot_uri=snapshot_uri,
        )
        db.add(board)
        db.flush()
    else:
        board = existing
        board.title = title or board.title
        board.revision = revision
        board.document = document
        board.snapshot_uri = snapshot_uri
        db.add(board)

    _record_event(
        db,
        board.id,
        revision,
        actor,
        op or ("create" if existing is None else "save"),
        event_payload,
    )

    db.add(
        InstructionBoardRevision(
            board_id=board.id,
            revision=revision,
            document=document,
            snapshot_uri=snapshot_uri,
        )
    )
    db.flush()
    return board


def _node_label(item) -> str:
    if isinstance(item, BoardAssetNode):
        return circled(item.mark)
    return f"{item.type}#{item.id[:6]}"


def _compile_digest(scene: BoardScene, revision: int) -> str:
    assets = [item for item in scene.items if isinstance(item, BoardAssetNode)]
    regions = [item for item in scene.items if isinstance(item, BoardRegionNode)]
    stage_nodes = [item for item in scene.items if isinstance(item, BoardStageNode)]
    free = [item for item in scene.items if isinstance(item, (BoardNoteNode, BoardTextNode))]

    head = [f"[INSTRUCTION BOARD r{revision}]"]
    goal_line = f"Goal: {scene.goal}" if scene.goal else ""

    asset_lines = ["Assets:"] if assets else []
    for item in sorted(assets, key=lambda node: node.mark):
        caption = f", {item.caption}" if item.caption else ""
        asset_lines.append(f"  {circled(item.mark)} {item.assetId} — {item.role}{caption}")

    link_lines = ["Relations:"] if scene.links else []
    by_id = {item.id: item for item in scene.items}
    for link in scene.links:
        source = by_id[link.from_]
        target = by_id[link.to]
        label = f" : {link.label}" if link.label else ""
        link_lines.append(
            f"  {_node_label(source)} -[{link.kind}]-> {_node_label(target)}{label}"
        )

    region_lines = ["Regions:"] if regions else []
    marks = {item.id: item.mark for item in assets}
    for region in regions:
        mark = circled(marks.get(region.assetId, 0))
        region_lines.append(
            f"  {mark} box({region.x:.2f},{region.y:.2f},{region.w:.2f},{region.h:.2f}) — {region.note}"
        )

    stage_lines = ["Stages:"] if stage_nodes else []
    for item in stage_nodes:
        params = f" params={item.params}" if item.params else ""
        stage_lines.append(f"  {item.stage}{params}")

    free_lines = ["Free notes:"] if free else []
    free_lines.extend(f"  - {item.text}" for item in free)

    blocks = [line for line in [*head, goal_line, *asset_lines, *link_lines, *region_lines, *stage_lines] if line]
    digest = "\n".join(blocks)
    if free_lines:
        digest = f"{digest}\n" + "\n".join(free_lines)

    # Free notes are the least load-bearing section; trim them first, then the
    # goal, and only then hard-truncate. Regions and relations always survive.
    while len(digest) > MAX_DIGEST_CHARS and len(free_lines) > 1:
        free_lines.pop()
        if len(free_lines) == 1 and free_lines[0] == "Free notes:":
            free_lines.pop()
        digest = "\n".join([*blocks, *free_lines])
    if len(digest) > MAX_DIGEST_CHARS and goal_line:
        blocks = [block for block in blocks if block != goal_line]
        digest = "\n".join(blocks + free_lines)
    if len(digest) > MAX_DIGEST_CHARS:
        digest = digest[: MAX_DIGEST_CHARS - 12].rstrip() + "\n…(truncated)"
    return digest


def compile_scene(scene: BoardScene, revision: int, snapshot_uri: str = "") -> dict:
    """Turn the document into the bundle the stage runtime consumes."""
    assets = []
    regions = []
    links = []
    marks: dict[str, int] = {
        item.id: item.mark for item in scene.items if isinstance(item, BoardAssetNode)
    }
    for item in scene.items:
        if isinstance(item, BoardAssetNode):
            assets.append(
                {
                    "node_id": item.id,
                    "mark": item.mark,
                    "asset_id": item.assetId,
                    "uri": item.uri,
                    "role": item.role,
                    "caption": item.caption,
                    "box": [item.x, item.y, item.w, item.h],
                }
            )
        elif isinstance(item, BoardRegionNode):
            regions.append(
                {
                    "node_id": item.id,
                    "asset_id": item.assetId,
                    "asset_mark": marks.get(item.assetId),
                    "box": [item.x, item.y, item.w, item.h],
                    "note": item.note,
                }
            )
    for link in scene.links:
        links.append(
            {
                "id": link.id,
                "kind": link.kind,
                "from": link.from_,
                "to": link.to,
                "label": link.label,
                "port": link.port,
                "data_type": link.dataType,
            }
        )
    stage_nodes = [
        {"node_id": item.id, "stage": item.stage, "params": item.params}
        for item in scene.items
        if isinstance(item, BoardStageNode)
    ]
    return {
        "revision": revision,
        "digest": _compile_digest(scene, revision),
        "assets": assets,
        "regions": regions,
        "links": links,
        "stage_nodes": stage_nodes,
        "snapshot_uri": snapshot_uri,
    }


def board_context_for_run(db: Session, run: PipelineRun) -> dict | None:
    """Compiled board for a run, or ``None`` when the run has no usable board."""
    board = latest_board(db, run.id)
    if board is None:
        return None
    try:
        scene = BoardScene.model_validate(board.document or {})
    except ValueError:
        logger.warning("instruction board %s has an invalid document", board.id, exc_info=True)
        return None
    return compile_scene(scene, board.revision, board.snapshot_uri)


def board_payload(board: InstructionBoard) -> dict:
    scene = BoardScene.model_validate(board.document or {})
    return {
        "id": board.id,
        "run_id": board.run_id,
        "workspace_id": board.workspace_id,
        "title": board.title,
        "revision": board.revision,
        "stage_scope": board.stage_scope,
        "snapshot_uri": board.snapshot_uri,
        "scene": scene.model_dump(by_alias=True, mode="json"),
        "compiled": compile_scene(scene, board.revision, board.snapshot_uri),
        "created_at": board.created_at.isoformat() if board.created_at else None,
        "updated_at": board.updated_at.isoformat() if board.updated_at else None,
    }
