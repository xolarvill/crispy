from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data.models import PipelineRun
from app.data.session import get_db
from app.schemas.boards import (
    BoardCompileRequest,
    BoardDocCreateRequest,
    BoardDocRenameRequest,
    BoardSaveRequest,
    BoardToolRequest,
)
from app.services.board_tools import BoardToolError, BoardToolSession
from app.services.boards import (
    BoardConflict,
    BoardError,
    board_event_payload,
    board_payload,
    board_revision,
    board_revisions,
    board_tab_payload,
    compile_scene,
    create_board,
    delete_board,
    get_board,
    latest_board,
    recent_board_events,
    rename_board,
    revision_payload,
    run_boards,
    save_board,
    validate_scene_assets,
)

router = APIRouter()


def _run_or_404(db: Session, run_id: str) -> PipelineRun:
    run = db.get(PipelineRun, run_id)
    if not run:
        raise HTTPException(404, "找不到此任务")
    return run


def _board_or_404(db: Session, run_id: str, board_id: str):
    board = get_board(db, run_id, board_id)
    if not board:
        raise HTTPException(404, "找不到此白板")
    return board


def _commit_save(db: Session, run: PipelineRun, scene, **kwargs):
    try:
        board = save_board(db, run, scene, **kwargs)
        db.commit()
    except BoardConflict as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except IntegrityError as exc:
        # Two writers raced past the revision check; the unique constraint on
        # (board_id, revision) rejects the loser, which is still a conflict.
        db.rollback()
        raise HTTPException(409, "另一处正在保存白板，请刷新后重试") from exc
    except BoardError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    db.refresh(board)
    return board


@router.get("/runs/{run_id}/board")
def read_board(run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    board = latest_board(db, run.id)
    if not board:
        raise HTTPException(404, "此任务还没有指令白板")
    return board_payload(board)


@router.get("/runs/{run_id}/board/events")
def read_board_events(
    run_id: str,
    limit: int = Query(40, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Newest-first trace of who read, saved or edited this board."""
    run = _run_or_404(db, run_id)
    board = latest_board(db, run.id)
    if not board:
        return []
    return [board_event_payload(row) for row in recent_board_events(db, board.id, limit=limit)]


@router.get("/runs/{run_id}/boards")
def list_board_revisions(run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    board = latest_board(db, run.id)
    if not board:
        return []
    return [
        {
            "revision": row.revision,
            "snapshot_uri": row.snapshot_uri,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in board_revisions(db, board.id)
    ]


@router.get("/runs/{run_id}/boards/{revision}")
def read_board_revision(run_id: str, revision: int, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    board = latest_board(db, run.id)
    if not board:
        raise HTTPException(404, "此任务还没有指令白板")
    row = board_revision(db, board.id, revision)
    if not row:
        raise HTTPException(404, "找不到此白板版本")
    return revision_payload(row)


@router.put("/runs/{run_id}/board")
def write_board(run_id: str, payload: BoardSaveRequest, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    board = _commit_save(
        db,
        run,
        payload.scene,
        title=payload.title,
        actor=payload.actor,
        expected_revision=payload.expected_revision,
        snapshot=payload.snapshot,
    )
    return board_payload(board)


@router.post("/runs/{run_id}/board/compile")
def compile_board_preview(run_id: str, payload: BoardCompileRequest, db: Session = Depends(get_db)):
    """Compile without saving, so the UI can show what the agent will read."""
    run = _run_or_404(db, run_id)
    try:
        validate_scene_assets(db, run, payload.scene)
    except BoardError as exc:
        raise HTTPException(422, str(exc)) from exc
    board = latest_board(db, run.id)
    revision = (board.revision + 1) if board else 1
    return compile_scene(payload.scene, revision)


# --- Board tabs ------------------------------------------------------------
# Several boards can live on one run. The tab strip reads and writes through
# these routes; the single-board routes above keep working for old clients.


@router.get("/runs/{run_id}/board-docs")
def list_board_docs(run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    current = latest_board(db, run.id)
    return [board_tab_payload(board, current) for board in run_boards(db, run.id)]


@router.post("/runs/{run_id}/board-docs")
def create_board_doc(
    run_id: str,
    payload: BoardDocCreateRequest,
    db: Session = Depends(get_db),
):
    run = _run_or_404(db, run_id)
    source = None
    if payload.source_board_id:
        source = _board_or_404(db, run.id, payload.source_board_id)
    board = create_board(
        db,
        run,
        source=source,
        title=payload.title,
        actor=payload.actor,
    )
    db.commit()
    db.refresh(board)
    return board_payload(board)


@router.get("/runs/{run_id}/board-docs/{board_id}")
def read_board_doc(run_id: str, board_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    return board_payload(_board_or_404(db, run.id, board_id))


@router.put("/runs/{run_id}/board-docs/{board_id}")
def write_board_doc(
    run_id: str,
    board_id: str,
    payload: BoardSaveRequest,
    db: Session = Depends(get_db),
):
    run = _run_or_404(db, run_id)
    _board_or_404(db, run.id, board_id)
    board = _commit_save(
        db,
        run,
        payload.scene,
        board_id=board_id,
        title=payload.title,
        actor=payload.actor,
        expected_revision=payload.expected_revision,
        snapshot=payload.snapshot,
    )
    return board_payload(board)


@router.patch("/runs/{run_id}/board-docs/{board_id}")
def update_board_doc(
    run_id: str,
    board_id: str,
    payload: BoardDocRenameRequest,
    db: Session = Depends(get_db),
):
    run = _run_or_404(db, run_id)
    board = _board_or_404(db, run.id, board_id)
    board = rename_board(db, board, payload.title)
    db.commit()
    db.refresh(board)
    return board_payload(board)


@router.delete("/runs/{run_id}/board-docs/{board_id}")
def remove_board_doc(run_id: str, board_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    board = _board_or_404(db, run.id, board_id)
    delete_board(db, board)
    db.commit()
    return {"ok": True}


@router.post("/runs/{run_id}/board-docs/{board_id}/tools")
def run_board_doc_tool(
    run_id: str,
    board_id: str,
    payload: BoardToolRequest,
    db: Session = Depends(get_db),
):
    """Run one typed canvas tool for an external harness.

    The stage pipeline runs the same service in-process; this route gives
    scripts and future MCP clients the identical, validated surface.
    """
    run = _run_or_404(db, run_id)
    _board_or_404(db, run.id, board_id)
    try:
        session = BoardToolSession(
            db,
            run,
            board_id=board_id,
            actor=payload.actor,
            stage_task_id=payload.stage_task_id,
        )
        result = session.execute(payload.name, payload.args)
        if not result.ok:
            db.rollback()
            raise HTTPException(422, result.error)
        board = session.commit(summary=payload.summary)
        db.commit()
    except BoardConflict as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "另一处正在保存白板，请刷新后重试") from exc
    except (BoardToolError, BoardError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    if board is not None:
        db.refresh(board)
    return {
        "ok": True,
        "result": result.as_dict(),
        "images": list(result.images),
        "revision": board.revision if board is not None else 0,
        "board": board_payload(board) if board is not None else None,
    }


@router.get("/runs/{run_id}/board-docs/{board_id}/events")
def read_board_doc_events(
    run_id: str,
    board_id: str,
    limit: int = Query(40, ge=1, le=200),
    db: Session = Depends(get_db),
):
    run = _run_or_404(db, run_id)
    board = _board_or_404(db, run.id, board_id)
    return [board_event_payload(row) for row in recent_board_events(db, board.id, limit=limit)]


@router.get("/runs/{run_id}/board-docs/{board_id}/revisions")
def list_board_doc_revisions(
    run_id: str,
    board_id: str,
    db: Session = Depends(get_db),
):
    run = _run_or_404(db, run_id)
    _board_or_404(db, run.id, board_id)
    return [
        {
            "revision": row.revision,
            "snapshot_uri": row.snapshot_uri,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in board_revisions(db, board_id)
    ]


@router.get("/runs/{run_id}/board-docs/{board_id}/revisions/{revision}")
def read_board_doc_revision(
    run_id: str,
    board_id: str,
    revision: int,
    db: Session = Depends(get_db),
):
    run = _run_or_404(db, run_id)
    _board_or_404(db, run.id, board_id)
    row = board_revision(db, board_id, revision)
    if not row:
        raise HTTPException(404, "找不到此白板版本")
    return revision_payload(row)
