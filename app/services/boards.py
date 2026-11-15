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
from sqlalchemy import delete, select
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
# Stages that may annotate the canvas after they finish. The last three read the
# board through the tool session (their main generation prompts do not consume
# the operator digest yet), so they are intentionally outside the scope above.
BOARD_TOOL_STAGES = BOARD_STAGE_SCOPE_STAGES | {
    "video_generation",
    "visual_quality_assessment",
    "evaluation_selection",
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
    """The board agents read: the most recently saved one.

    A blank tab has revision 0 and is deliberately skipped, so opening a new
    tab never changes what a stage agent sees until the operator saves it.
    """
    return db.scalar(
        select(InstructionBoard)
        .where(
            InstructionBoard.run_id == run_id,
            InstructionBoard.revision > 0,
        )
        .order_by(InstructionBoard.updated_at.desc(), InstructionBoard.created_at.desc())
        .limit(1)
    )


def get_board(db: Session, run_id: str, board_id: str) -> InstructionBoard | None:
    board = db.get(InstructionBoard, board_id)
    if board is None or board.run_id != run_id:
        return None
    return board


def run_boards(db: Session, run_id: str) -> list[InstructionBoard]:
    """Every board tab for a run, oldest first so tabs keep a stable order."""
    return list(
        db.scalars(
            select(InstructionBoard)
            .where(InstructionBoard.run_id == run_id)
            .order_by(InstructionBoard.created_at.asc(), InstructionBoard.id.asc())
        ).all()
    )


def _media_uris(db: Session, run_id: str) -> set[str]:
    summary = run_media_summaries(db, [run_id], limit=None).get(run_id, {})
    return {str(item["uri"]) for item in summary.get("media_previews", []) if item.get("uri")}


def _inside_run_assets_dir(run_id: str, uri: str) -> bool:
    requested = Path(uri).expanduser()
    if not requested.is_absolute():
        requested = (Path.cwd() / requested).resolve()
    # Generated media lives under ``assets/<run>/``; composed board sheets live
    # under ``assets/boards/<run>/``. Both are run-owned and safe to pin.
    for base in (
        get_settings().assets_dir / run_id,
        get_settings().assets_dir / "boards" / run_id,
    ):
        try:
            requested.relative_to(base.resolve())
        except ValueError:
            continue
        return True
    return False


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


def _write_snapshot(run_id: str, board_id: str, revision: int, value: str) -> str:
    image = _decode_snapshot(value).convert("RGB")
    target_dir = get_settings().assets_dir / "boards" / run_id / board_id
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
    board_id: str | None = None,
    title: str = "",
    actor: str = "human",
    expected_revision: int | None = None,
    snapshot: str = "",
    op: str = "",
    event: dict | None = None,
) -> InstructionBoard:
    """Store a new revision of a run's board.

    ``board_id`` targets one tab; without it the most recently saved board is
    used, which keeps the single-board API working. ``expected_revision`` is
    the revision the caller edited. A missing value means "create only"; a
    stale value is a conflict. That keeps concurrent human and agent writes
    from silently overwriting each other.
    """
    validate_scene_assets(db, run, scene)

    if board_id is not None:
        existing = get_board(db, run.id, board_id)
        if existing is None:
            raise BoardError("board tab not found")
    else:
        existing = latest_board(db, run.id)
    # A tab row can exist before its first save (revision 0), so "created"
    # means this save lays down revision 1 whether or not the row is new.
    created = existing is None or existing.revision == 0

    if created:
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

    document = scene.model_dump(by_alias=True, mode="json")
    event_payload = event or _change_summary(existing.document if existing else None, scene)

    if existing is None:
        board = InstructionBoard(
            workspace_id=run.workspace_id,
            run_id=run.id,
            stage_scope=None,
            title=title,
            revision=0,
            document={},
            snapshot_uri="",
        )
        db.add(board)
    else:
        board = existing

    board.title = title or board.title
    board.revision = revision
    board.document = document
    db.flush()

    # Snapshots live under the board id so two tabs at the same revision do
    # not overwrite each other's screenshots. A save without a snapshot (an
    # agent tool write, or a restore) keeps the last rendered one rather than
    # erasing the board's only preview.
    if snapshot:
        board.snapshot_uri = _write_snapshot(run.id, board.id, revision, snapshot)
    elif existing is None:
        board.snapshot_uri = ""
    db.add(board)

    _record_event(
        db,
        board.id,
        revision,
        actor,
        op or ("create" if created else "save"),
        event_payload,
    )

    db.add(
        InstructionBoardRevision(
            board_id=board.id,
            revision=revision,
            document=document,
            snapshot_uri=board.snapshot_uri,
        )
    )
    db.flush()
    return board


def create_board(
    db: Session,
    run: PipelineRun,
    *,
    source: InstructionBoard | None = None,
    title: str = "",
    actor: str = "human",
) -> InstructionBoard:
    """Open a new tab: a copy of ``source`` when given, otherwise a blank board.

    A copy starts at revision 1 because it is already a saved document; a blank
    tab stays at revision 0 until its first save, so agents keep reading the
    previous board in the meantime.
    """
    document: dict = {}
    revision = 0
    fallback_title = ""
    if source is not None:
        scene = BoardScene.model_validate(source.document or {})
        document = scene.model_dump(by_alias=True, mode="json")
        revision = 1
        if source.title:
            fallback_title = source.title
    board = InstructionBoard(
        workspace_id=run.workspace_id,
        run_id=run.id,
        stage_scope=source.stage_scope if source else None,
        title=title or fallback_title,
        revision=revision,
        document=document,
        snapshot_uri="",
    )
    db.add(board)
    db.flush()

    if source is not None:
        db.add(
            InstructionBoardRevision(
                board_id=board.id,
                revision=1,
                document=document,
                snapshot_uri="",
            )
        )
        _record_event(
            db,
            board.id,
            1,
            actor,
            "duplicate",
            {
                "source_board_id": source.id,
                "source_revision": source.revision,
                **_change_summary(None, scene),
            },
        )
    else:
        _record_event(db, board.id, 0, actor, "new_tab", {"title": board.title})
    db.flush()
    return board


def rename_board(db: Session, board: InstructionBoard, title: str) -> InstructionBoard:
    board.title = title
    db.add(board)
    db.flush()
    return board


def delete_board(db: Session, board: InstructionBoard) -> None:
    """Remove a tab with its revisions and activity log."""
    db.execute(
        delete(InstructionBoardRevision).where(InstructionBoardRevision.board_id == board.id)
    )
    db.execute(
        delete(InstructionBoardEvent).where(InstructionBoardEvent.board_id == board.id)
    )
    db.delete(board)
    db.flush()


def _node_label(item) -> str:
    if isinstance(item, BoardAssetNode):
        return circled(item.mark)
    return f"{item.type}#{item.id[:6]}"


def _compile_digest(scene: BoardScene, revision: int) -> str:
    assets = [item for item in scene.items if isinstance(item, BoardAssetNode)]
    regions = [item for item in scene.items if isinstance(item, BoardRegionNode)]
    stage_nodes = [item for item in scene.items if isinstance(item, BoardStageNode)]
    free = [item for item in scene.items if isinstance(item, (BoardNoteNode, BoardTextNode))]

    # The digest is the operator's instruction. Agent-authored notes, regions
    # and links are findings, not direction — re-reading them as instructions
    # would let one stage quietly steer the next. They stay available through
    # the board tools and the compiled ``agent_annotations`` instead.
    human_regions = [item for item in regions if _actor_kind(item.created_by) == "human"]
    human_free = [item for item in free if _actor_kind(item.created_by) == "human"]
    human_links = [link for link in scene.links if _actor_kind(link.created_by) == "human"]
    goal_is_human = _actor_kind(scene.goal_created_by) == "human"

    head = [f"[INSTRUCTION BOARD r{revision}]"]
    goal_line = f"Goal: {scene.goal}" if scene.goal and goal_is_human else ""

    asset_lines = ["Assets:"] if assets else []
    for item in sorted(assets, key=lambda node: node.mark):
        caption = f", {item.caption}" if item.caption else ""
        agent_tag = " (agent output)" if _actor_kind(item.created_by) == "agent" else ""
        asset_lines.append(
            f"  {circled(item.mark)} {item.assetId} — {item.role}{caption}{agent_tag}"
        )

    link_lines = ["Relations:"] if human_links else []
    by_id = {item.id: item for item in scene.items}
    for link in human_links:
        source = by_id[link.from_]
        target = by_id[link.to]
        label = f" : {link.label}" if link.label else ""
        link_lines.append(
            f"  {_node_label(source)} -[{link.kind}]-> {_node_label(target)}{label}"
        )

    region_lines = ["Regions:"] if human_regions else []
    marks = {item.id: item.mark for item in assets}
    for region in human_regions:
        mark = circled(marks.get(region.assetId, 0))
        region_lines.append(
            f"  {mark} box({region.x:.2f},{region.y:.2f},{region.w:.2f},{region.h:.2f}) — {region.note}"
        )

    stage_lines = ["Stages:"] if stage_nodes else []
    for item in stage_nodes:
        params = f" params={item.params}" if item.params else ""
        stage_lines.append(f"  {item.stage}{params}")

    free_lines = ["Free notes:"] if human_free else []
    free_lines.extend(f"  - {item.text}" for item in human_free)

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
                    "created_by": item.created_by,
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
                    "intent": item.intent,
                    "created_by": item.created_by,
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
                "created_by": link.created_by,
            }
        )
    stage_nodes = [
        {"node_id": item.id, "stage": item.stage, "params": item.params}
        for item in scene.items
        if isinstance(item, BoardStageNode)
    ]
    agent_annotations: list[dict] = []
    for item in scene.items:
        if _actor_kind(item.created_by) == "human":
            continue
        if isinstance(item, BoardRegionNode):
            agent_annotations.append(
                {
                    "node_id": item.id,
                    "type": "region",
                    "asset_id": item.assetId,
                    "asset_mark": marks.get(item.assetId),
                    "box": [item.x, item.y, item.w, item.h],
                    "note": item.note,
                    "intent": item.intent,
                    "created_by": item.created_by,
                }
            )
        elif isinstance(item, (BoardNoteNode, BoardTextNode)):
            agent_annotations.append(
                {
                    "node_id": item.id,
                    "type": item.type,
                    "text": item.text,
                    "intent": getattr(item, "intent", ""),
                    "created_by": item.created_by,
                }
            )
    for link in scene.links:
        if _actor_kind(link.created_by) == "agent":
            agent_annotations.append(
                {
                    "node_id": link.id,
                    "type": "link",
                    "from": link.from_,
                    "to": link.to,
                    "label": link.label,
                    "kind": link.kind,
                    "created_by": link.created_by,
                }
            )
    return {
        "revision": revision,
        "digest": _compile_digest(scene, revision),
        "assets": assets,
        "regions": regions,
        "links": links,
        "stage_nodes": stage_nodes,
        "agent_annotations": agent_annotations,
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


def board_tab_payload(board: InstructionBoard, current: InstructionBoard | None) -> dict:
    """Tab-strip metadata; ``is_current`` marks the board agents will read."""
    return {
        "id": board.id,
        "run_id": board.run_id,
        "title": board.title,
        "revision": board.revision,
        "stage_scope": board.stage_scope,
        "is_current": bool(current and current.id == board.id),
        "created_at": board.created_at.isoformat() if board.created_at else None,
        "updated_at": board.updated_at.isoformat() if board.updated_at else None,
    }


def board_revisions(db: Session, board_id: str, *, limit: int = 50) -> list[InstructionBoardRevision]:
    return list(
        db.scalars(
            select(InstructionBoardRevision)
            .where(InstructionBoardRevision.board_id == board_id)
            .order_by(InstructionBoardRevision.revision.desc())
            .limit(limit)
        ).all()
    )


def board_revision(db: Session, board_id: str, revision: int) -> InstructionBoardRevision | None:
    return db.scalar(
        select(InstructionBoardRevision).where(
            InstructionBoardRevision.board_id == board_id,
            InstructionBoardRevision.revision == revision,
        )
    )


def revision_payload(row: InstructionBoardRevision) -> dict:
    return {
        "revision": row.revision,
        "snapshot_uri": row.snapshot_uri,
        "scene": row.document,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
