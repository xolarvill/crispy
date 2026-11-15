from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data.models import InstructionBoardRevision, PipelineRun
from app.data.session import get_db
from app.schemas.boards import BoardCompileRequest, BoardSaveRequest
from app.services.boards import (
    BoardConflict,
    BoardError,
    board_payload,
    compile_scene,
    latest_board,
    save_board,
    validate_scene_assets,
)

router = APIRouter()


def _run_or_404(db: Session, run_id: str) -> PipelineRun:
    run = db.get(PipelineRun, run_id)
    if not run:
        raise HTTPException(404, "找不到此任务")
    return run


@router.get("/runs/{run_id}/board")
def read_board(run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    board = latest_board(db, run.id)
    if not board:
        raise HTTPException(404, "此任务还没有指令白板")
    return board_payload(board)


@router.get("/runs/{run_id}/boards")
def list_board_revisions(run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    board = latest_board(db, run.id)
    if not board:
        return []
    rows = db.scalars(
        select(InstructionBoardRevision)
        .where(InstructionBoardRevision.board_id == board.id)
        .order_by(InstructionBoardRevision.revision.desc())
        .limit(50)
    ).all()
    return [
        {
            "revision": row.revision,
            "snapshot_uri": row.snapshot_uri,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@router.get("/runs/{run_id}/boards/{revision}")
def read_board_revision(run_id: str, revision: int, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    board = latest_board(db, run.id)
    if not board:
        raise HTTPException(404, "此任务还没有指令白板")
    row = db.scalar(
        select(InstructionBoardRevision).where(
            InstructionBoardRevision.board_id == board.id,
            InstructionBoardRevision.revision == revision,
        )
    )
    if not row:
        raise HTTPException(404, "找不到此白板版本")
    return {
        "revision": row.revision,
        "snapshot_uri": row.snapshot_uri,
        "scene": row.document,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.put("/runs/{run_id}/board")
def write_board(run_id: str, payload: BoardSaveRequest, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    try:
        board = save_board(
            db,
            run,
            payload.scene,
            title=payload.title,
            actor=payload.actor,
            expected_revision=payload.expected_revision,
            snapshot=payload.snapshot,
        )
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
