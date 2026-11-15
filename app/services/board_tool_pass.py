"""Post-stage canvas pass: let a stage agent read and annotate the board.

The pass runs after the stage output is final and before the task waits for
review. It is deliberately **non-fatal**: provider errors, bad JSON and board
write failures are traced and skipped, never raised into ``execute_stage_task``.
All canvas writes stay in memory until one ``BoardToolSession.commit()``, so a
failed pass leaves the board untouched (except compose files on disk).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.agents.board_tool_protocol import ToolProtocolError, ToolTurn, parse_tool_turn
from app.agents.board_tools_prompt import render_board_tool_prompt
from app.services.board_tools import MAX_OPS_PER_PASS, BoardToolSession
from app.services.boards import BOARD_TOOL_STAGES, _compile_digest

logger = logging.getLogger(__name__)

MAX_ROUNDS = 3
MAX_IMAGES_PER_ROUND = 2
MAX_QUESTIONS = 5
ANNOTATION_ONLY_STAGES = {
    "planning",
    "video_scripting",
    "visual_quality_assessment",
    "evaluation_selection",
}


@dataclass
class BoardPassResult:
    ran: bool = False
    revision: int | None = None
    summary: str = ""
    ops: int = 0
    questions: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    estimated_cost: float = 0.0


def board_tools_enabled(stage_name: str, runtime_config: dict | None) -> bool:
    if stage_name not in BOARD_TOOL_STAGES:
        return False
    extra = (runtime_config or {}).get("extra") or {}
    value = str(extra.get("board_tools", "on")).strip().lower()
    return value not in {"off", "false", "0", "no", "disabled", "none"}


def _compact(value, depth: int = 0, limit: int = 6):
    if depth > 3:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= 240 else value[:237] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        items = [_compact(item, depth + 1, limit) for item in value[:limit]]
        if len(value) > limit:
            items.append("…")
        return items
    if isinstance(value, dict):
        return {
            str(key): _compact(item, depth + 1, limit)
            for key, item in list(value.items())[:16]
        }
    return str(value)[:200]


def _stage_summary(stage_name: str, payload: dict) -> str:
    try:
        text = json.dumps(_compact(payload), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(payload)[:1500]
    if len(text) > 3000:
        text = text[:2980] + "…"
    return f"stage={stage_name}\n{text}"


def _result_entry(name: str, result, images: list[str]) -> dict:
    entry: dict = {"tool": name, "ok": result.ok}
    if result.ok:
        entry["result"] = _compact(result.data, limit=10)
    else:
        entry["error"] = result.error[:300]
    if images:
        entry["images"] = len(images)
    text = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    if len(text) > 1400:
        entry["result"] = f"{text[:1100]}…"
    return entry


def _questions(session: BoardToolSession) -> list[str]:
    from app.schemas.boards import BoardNoteNode

    found: list[str] = []
    for item in session.scene.items:
        if not isinstance(item, BoardNoteNode):
            continue
        if item.created_by != session.actor or item.intent != "question":
            continue
        found.append(item.text.strip()[:300])
    return found[:MAX_QUESTIONS]


def _image_input(runtime, uri: str) -> str | None:
    if not uri:
        return None
    if uri.startswith(("data:", "http://", "https://")):
        return uri
    return runtime._local_image_to_review_data_url(uri, max_side=1024)


def run_board_tool_pass(
    context,
    *,
    runtime_config: dict | None,
    provider_name: str,
    model_name: str,
    stage_output: dict | None,
) -> BoardPassResult:
    """Run up to three canvas rounds for the stage that just finished."""
    task = context.task
    stage_name = task.stage_name
    result = BoardPassResult()
    if not board_tools_enabled(stage_name, runtime_config):
        return result
    if stage_name in ANNOTATION_ONLY_STAGES:
        # Nothing on the canvas to annotate; opening one from an annotation-only
        # stage would surprise the operator.
        from app.services.boards import latest_board

        if latest_board(context.db, context.run.id) is None:
            return result

    try:
        session = BoardToolSession(
            context.db,
            context.run,
            actor=context.lead_agent,
            stage_task_id=task.id,
            board_title=f"{context.run.product_code} board",
        )
    except Exception as exc:  # invalid document, missing tab, …
        context.emit_trace(
            "board_tools_failed",
            f"{context.lead_agent} could not open the instruction board.",
            payload={"error": str(exc)[:300], "stage": stage_name},
        )
        return result

    pass_config = {**dict(runtime_config or {}), "streaming_enabled": False}
    transcript: list[dict] = []
    images: list[str] = []
    result_images: list[str] = []
    starting_revision = session.revision
    result.ran = True
    repaired = False

    try:
        for round_index in range(1, MAX_ROUNDS + 1):
            annotations_result = session.execute("get_annotations", {})
            prompt = render_board_tool_prompt(
                agent_name=context.lead_agent,
                stage_name=stage_name,
                board_digest=_compile_digest(session.scene, session.revision),
                revision=session.revision,
                annotations=annotations_result.data if annotations_result.ok else {},
                stage_summary=_stage_summary(stage_name, stage_output or {}),
                round_index=round_index,
                max_rounds=MAX_ROUNDS,
                transcript=transcript,
            )
            try:
                text, _model_used, cost = context.runtime._chat_complete(
                    provider_name,
                    model_name,
                    prompt,
                    pass_config,
                    image_urls=images or None,
                )
            except Exception as exc:
                result.errors.append({"error": f"provider: {exc}"[:300]})
                context.emit_trace(
                    "board_tools_failed",
                    f"{context.lead_agent} could not reach the model for board tools.",
                    payload={"error": str(exc)[:300], "stage": stage_name, "round": round_index},
                )
                break
            result.estimated_cost += float(cost or 0.0)

            try:
                turn: ToolTurn = parse_tool_turn(text)
            except ToolProtocolError as exc:
                result.errors.append({"error": f"protocol: {exc}"[:300]})
                transcript.append(
                    {
                        "round": round_index,
                        "protocol_error": str(exc)[:300],
                        "response": (text or "").strip()[:600],
                    }
                )
                if repaired:
                    break
                repaired = True
                continue

            result.summary = turn.summary
            context.emit_trace(
                "board_tool_round",
                f"{context.lead_agent} board round {round_index}: {len(turn.tool_calls)} call(s).",
                payload={
                    "round": round_index,
                    "calls": [call.name for call in turn.tool_calls],
                    "summary": turn.summary,
                    "pending_writes": session.pending_writes,
                },
            )
            if not turn.tool_calls:
                break

            round_results: list[dict] = []
            next_images: list[str] = []
            for call in turn.tool_calls:
                if session.pending_writes >= MAX_OPS_PER_PASS:
                    round_results.append(
                        {"tool": call.name, "ok": False, "error": "write limit reached"}
                    )
                    continue
                tool_result = session.execute(call.name, call.args)
                raw_images = tool_result.images[:MAX_IMAGES_PER_ROUND]
                converted = [
                    image
                    for image in (
                        _image_input(context.runtime, uri)
                        for uri in raw_images
                    )
                    if image
                ]
                round_results.append(_result_entry(call.name, tool_result, converted))
                if not tool_result.ok:
                    result.errors.append({"tool": call.name, "error": tool_result.error[:300]})
                if raw_images:
                    result_images.extend(raw_images)
                context.emit_trace(
                    "board_tool_call",
                    f"{context.lead_agent} called {call.name}.",
                    payload={
                        "tool": call.name,
                        "ok": tool_result.ok,
                        "error": tool_result.error[:300] if not tool_result.ok else "",
                        "args": _compact(call.args, limit=8),
                        "images": converted[:MAX_IMAGES_PER_ROUND],
                        "round": round_index,
                    },
                )
                next_images.extend(converted)
            transcript.append(
                {"round": round_index, "summary": turn.summary, "results": round_results}
            )
            images = next_images[:MAX_IMAGES_PER_ROUND]
            if len(result.errors) > 12:
                break

        if not session.pending_writes:
            return result

        board = session.commit(summary=result.summary or "agent canvas pass")
        if board is not None and board.revision > starting_revision:
            result.revision = board.revision
        result.ops = len(session.ops)
        result.questions = _questions(session)
        result.images = result_images[:6]
        task.metadata_json = {
            **(task.metadata_json or {}),
            "board_tool_revision": result.revision,
            "board_questions": result.questions,
        }
        for question in result.questions:
            context.emit_trace(
                "board_question",
                f"{context.lead_agent} asked the operator: {question}",
                payload={
                    "question": question,
                    "revision": result.revision,
                    "stage": stage_name,
                },
            )
        context.emit_trace(
            "board_tools_completed",
            f"{context.lead_agent} annotated the board at r{result.revision}.",
            payload={
                "revision": result.revision,
                "ops": result.ops,
                "questions": result.questions,
                "tools": [op.name for op in session.ops],
                "errors": result.errors[:5],
            },
        )
    except Exception as exc:
        logger.exception("board tool pass failed for %s", stage_name)
        context.emit_trace(
            "board_tools_failed",
            f"{context.lead_agent} could not annotate the board.",
            payload={"error": str(exc)[:300], "stage": stage_name},
        )
        result.errors.append({"error": str(exc)[:300]})
    return result
