"""boardwright — a stdio MCP server for Crispy instruction boards.

The server exposes every typed canvas tool in ``app.services.board_tools``
(``TOOL_SPECS``) as one MCP tool. Names, argument schemas and descriptions are
generated from the same specs the stage agents and the HTTP harness use, so a
new tool or a changed limit cannot drift between surfaces.

Execution model: **in-process**, one ``BoardToolSession`` per MCP call. The
server imports ``SessionLocal`` directly, executes the tool, and lands a write
as exactly one board revision before returning; reads do not open a revision.
``actor`` is required at startup and every write is stamped ``agent:<actor>``;
the session still enforces that agents may only modify agent-owned nodes, so a
human operator's notes stay read-only.

Why in-process instead of ``POST /runs/{run_id}/board-docs/{board_id}/tools``:

* no API server has to be running, and no bearer token or URL plumbing is
  needed in a desktop MCP config;
* the response can carry real MCP ``ImageContent`` for compose/screenshot
  results, which the HTTP route can only expose as local paths;
* errors keep the same typed messages the service produces.

The trade-off is that the MCP process needs direct access to the same database
(and run assets) as the Crispy app. SQLite handles this across processes, but
the operator's app and the MCP server must point at the same ``.db`` file and
must not run on different hosts. If you need a remote or multi-user gateway,
run it against the HTTP route instead; the tool schemas are identical.

Board selection: set ``--board-id`` (or ``BOARDWRIGHT_BOARD_ID``) for a fixed
tab, or omit it to start on the run's latest saved board. ``create_board`` moves
the server onto the new tab for the rest of the session. There is deliberately
no hardcoded run id: ``--run-id`` / ``BOARDWRIGHT_RUN_ID`` is required.

Run it with::

    uv run python -m app.mcp.boardwright \
        --run-id <run-id> --actor external_agent

See ``docs/agent-board-tools.md`` for Claude Desktop and Inspector configs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import mimetypes
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    # Running this file as a script (some inspectors do) puts app/mcp rather
    # than the repository root on sys.path.
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.types import (  # noqa: E402
    CallToolResult,
    ImageContent,
    TextContent,
    Tool,
    ToolAnnotations,
)
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.data.models import PipelineRun  # noqa: E402
from app.data.session import (  # noqa: E402
    SessionLocal,
    get_active_database_url,
    switch_database_url,
)
from app.services.board_tools import (  # noqa: E402
    TOOL_SPECS,
    BoardToolError,
    BoardToolSession,
)
from app.services.boards import BoardConflict, BoardError, get_board  # noqa: E402

logger = logging.getLogger("boardwright")

SERVER_NAME = "boardwright"
SERVER_VERSION = "0.1.0"
SERVER_INSTRUCTIONS = (
    "Typed read/write tools for one Crispy instruction board. Every write is "
    "attributed to agent:<actor>, lands as one revertable board revision, and "
    "may only modify agent-owned nodes. Read the board before writing; human "
    "nodes are read-only."
)

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}


@dataclass(frozen=True)
class BoardwrightConfig:
    """Everything the server needs, resolved from CLI args or env vars."""

    run_id: str
    actor: str
    board_id: str | None = None
    database_url: str | None = None
    stage_task_id: str = "manual"
    root: Path = Path(".")

    @property
    def stage_prefix(self) -> str:
        value = (self.stage_task_id or "manual").strip() or "manual"
        return value[:24]


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def parse_args(argv: list[str] | None = None) -> BoardwrightConfig:
    parser = argparse.ArgumentParser(
        prog="python -m app.mcp.boardwright",
        description="Expose Crispy instruction-board canvas tools over MCP stdio.",
    )
    parser.add_argument(
        "--run-id",
        default=_env("BOARDWRIGHT_RUN_ID"),
        help="Pipeline run the server is bound to (or BOARDWRIGHT_RUN_ID).",
    )
    parser.add_argument(
        "--actor",
        default=_env("BOARDWRIGHT_ACTOR"),
        help="Agent name stamped on every write (or BOARDWRIGHT_ACTOR). Required.",
    )
    parser.add_argument(
        "--board-id",
        default=_env("BOARDWRIGHT_BOARD_ID"),
        help="Fixed board tab (or BOARDWRIGHT_BOARD_ID). Default: latest saved board.",
    )
    parser.add_argument(
        "--database-url",
        default=_env("BOARDWRIGHT_DATABASE_URL"),
        help=(
            "Database to read/write (or BOARDWRIGHT_DATABASE_URL). "
            "Default: the app's CRISPY_DATABASE_URL."
        ),
    )
    parser.add_argument(
        "--stage-task-id",
        default=_env("BOARDWRIGHT_STAGE_TASK_ID") or "manual",
        help="Provenance prefix for deterministic node ids (or BOARDWRIGHT_STAGE_TASK_ID).",
    )
    parser.add_argument(
        "--root",
        default=_env("BOARDWRIGHT_ROOT") or str(Path.cwd()),
        help="Directory that relative asset paths resolve against (or BOARDWRIGHT_ROOT).",
    )
    args = parser.parse_args(argv)

    run_id = (args.run_id or "").strip()
    actor = (args.actor or "").strip()
    if not run_id:
        parser.error("--run-id is required (or set BOARDWRIGHT_RUN_ID)")
    if not actor:
        parser.error("--actor is required (or set BOARDWRIGHT_ACTOR) so writes have provenance")
    if len(actor) > 128:
        parser.error("--actor must be at most 128 characters")

    return BoardwrightConfig(
        run_id=run_id,
        actor=actor,
        board_id=(args.board_id or "").strip() or None,
        database_url=(args.database_url or "").strip() or None,
        stage_task_id=args.stage_task_id.strip() or "manual",
        root=Path(args.root).expanduser().resolve(),
    )


def prepare(config: BoardwrightConfig) -> None:
    """Apply the configured database and fail fast on a bad run/board id."""
    if config.database_url:
        switch_database_url(config.database_url)
    with SessionLocal() as db:
        run = db.get(PipelineRun, config.run_id)
        if run is None:
            raise SystemExit(
                f"boardwright: run '{config.run_id}' not found in "
                f"{get_active_database_url()}"
            )
        if config.board_id and get_board(db, run.id, config.board_id) is None:
            raise SystemExit(
                f"boardwright: board '{config.board_id}' does not belong to run "
                f"'{config.run_id}' in {get_active_database_url()}"
            )


def tool_definitions() -> list[Tool]:
    """MCP tool descriptors generated from the service specs."""
    tools: list[Tool] = []
    for spec in TOOL_SPECS.values():
        # ``batch`` is marked read-like in the service (it does not spend the
        # per-call write budget itself) but it can execute write tools, so it
        # must not be advertised to clients as read-only.
        read_only = not spec.write and spec.name != "batch"
        tools.append(
            Tool(
                name=spec.name,
                title=spec.name.replace("_", " ").title(),
                description=spec.description,
                inputSchema=spec.args.model_json_schema(),
                annotations=ToolAnnotations(readOnlyHint=read_only),
            )
        )
    return tools


def _error_result(message: str) -> CallToolResult:
    payload = {"ok": False, "error": message}
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, default=str),
            )
        ],
        structuredContent=payload,
        isError=True,
    )


def _resolve_image(raw: str, root: Path) -> Path | None:
    value = (raw or "").strip()
    if not value:
        return None
    candidate = Path(value).expanduser()
    paths = (
        [candidate]
        if candidate.is_absolute()
        else [root / candidate, Path.cwd() / candidate]
    )
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.suffix.lower() in _IMAGE_SUFFIXES:
            return resolved
    return None


def _image_contents(paths: list[str], root: Path) -> list[ImageContent]:
    """Attach compose/screenshot files as real MCP images when they exist."""
    contents: list[ImageContent] = []
    seen: set[Path] = set()
    for raw in paths:
        if len(contents) >= MAX_IMAGES:
            break
        path = _resolve_image(raw, root)
        if path is None or path in seen:
            continue
        seen.add(path)
        try:
            if path.stat().st_size > MAX_IMAGE_BYTES:
                logger.warning("skipping oversized board image %s", path)
                continue
            data = path.read_bytes()
        except OSError as exc:
            logger.warning("could not read board image %s: %s", path, exc)
            continue
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        contents.append(
            ImageContent(
                type="image",
                data=base64.b64encode(data).decode("ascii"),
                mimeType=mime,
            )
        )
    return contents


def call_tool(
    config: BoardwrightConfig,
    state: dict[str, Any],
    name: str,
    arguments: dict[str, Any],
) -> CallToolResult:
    """Run one canvas tool in its own session; a write commits one revision."""
    if name not in TOOL_SPECS:
        return _error_result(f"unknown tool '{name}'")
    # A fresh nonce per call keeps deterministic node ids unique across calls;
    # replay within this call still uses the same id, as the service expects.
    stage_task_id = f"{config.stage_prefix}-{uuid.uuid4().hex[:8]}"

    with SessionLocal() as db:
        run = db.get(PipelineRun, config.run_id)
        if run is None:
            return _error_result(f"run '{config.run_id}' no longer exists")
        try:
            session = BoardToolSession(
                db,
                run,
                board_id=state.get("board_id"),
                actor=config.actor,
                stage_task_id=stage_task_id,
            )
            result = session.execute(name, arguments or {})
            if not result.ok:
                db.rollback()
                return _error_result(result.error)
            board = session.commit(summary=f"boardwright {name}")
            db.commit()
        except BoardConflict as exc:
            db.rollback()
            return _error_result(f"board revision conflict: {exc}")
        except IntegrityError:
            db.rollback()
            return _error_result("another writer saved this board at the same time; retry")
        except (BoardToolError, BoardError) as exc:
            db.rollback()
            return _error_result(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            db.rollback()
            logger.exception("boardwright tool %s failed", name)
            return _error_result(f"{name} failed: {exc}")

        created_tab = name == "create_board"
        if name == "batch":
            # create_board is legal inside batch only as its first call.
            created_tab = any(
                entry.get("tool") == "create_board"
                and isinstance(entry.get("result"), dict)
                and entry["result"].get("ok")
                for entry in result.data.get("results", [])
            )
        if created_tab and session.board_id:
            # A new tab starts at revision 0 and is skipped by latest_board();
            # pin the server to it explicitly for the rest of the session.
            state["board_id"] = session.board_id

        board_id = board.id if board is not None else session.board_id
        revision = int(board.revision) if board is not None else 0
        title = (board.title if board is not None else session.title) or ""
        payload = {
            "ok": True,
            "result": result.as_dict(),
            "images": list(result.images),
            "revision": revision,
            "board_id": board_id,
            "board_title": title,
            "actor": session.actor,
        }
        contents: list[Any] = [
            TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, default=str),
            )
        ]
        contents.extend(_image_contents(list(result.images), config.root))
        return CallToolResult(content=contents, structuredContent=payload)


def build_server(config: BoardwrightConfig) -> Server:
    server: Server = Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
    )
    state: dict[str, Any] = {"board_id": config.board_id}

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return tool_definitions()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        return call_tool(config, state, name, arguments)

    return server


async def serve(config: BoardwrightConfig) -> None:
    server = build_server(config)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("BOARDWRIGHT_LOG_LEVEL", "WARNING").upper(),
        stream=sys.stderr,
    )
    config = parse_args(argv)
    prepare(config)
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
