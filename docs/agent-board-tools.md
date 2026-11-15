# Agent canvas tools

Stage agents can read and write the instruction board through a small, typed
tool surface. A pass mutates an in-memory copy of the board's `BoardScene` and
lands as **one** revertable revision, so an agent's work can be reviewed and
undone as a unit.

Implemented surface: `app/services/board_tools.py` (`TOOL_SPECS`,
`BoardToolSession`). Stage prompt and round loop: `app/services/board_tool_pass.py`.
External surfaces: `POST /runs/{run_id}/board-docs/{board_id}/tools` and the
stdio MCP server `app/mcp/boardwright.py`.

## Who gets the tools

`BOARD_TOOL_STAGES` (`app/services/boards.py`) covers intake-adjacent and
creative stages:

```
planning, copy_image_generation, video_scripting,
storyboard_image_generation, video_generation,
visual_quality_assessment, evaluation_selection
```

`compliance` and `product_research` are intentionally excluded. Disable the
pass per agent config with `extra.board_tools = "off"` on the `generation_agent`
(API-key config page / `PATCH /agent-configs/{agent}`).

A stage pass runs after the stage output is persisted and before the task moves
to `waiting_review`:

- at most **3 LLM rounds**; one protocol repair round if the model emits invalid JSON
- at most **24 writes**, **6 pinned assets**, **3 composes**, **9 items per compose**
- non-fatal: a provider or parse failure writes `board_tools_failed` to the trace
  and the stage still completes
- annotation-only stages (planning, visual QA, evaluation) skip the pass when the
  run has no saved board

## Calling model

Providers do not expose native tool calling in this runtime, so the pass uses a
JSON protocol in the assistant text:

```json
{
  "summary": "one line for the operator",
  "tool_calls": [
    {"name": "place_assets", "args": {"items": [{"uri": "assets/generated/hero.png", "caption": "v2"}], "link_from": "1"}}
  ]
}
```

Each round receives the tool catalog, the board digest (human instructions),
annotations, the previous results and any images returned by
`get_screenshot` / `compose`. Tool failures are returned to the model as
`{"ok": false, "error": "..."}` and do not abort the pass.

## Read tools

| tool | args | returns |
| --- | --- | --- |
| `get_board` | `include?: summary \| annotations \| full` | revision, title, goal, nodes, links, digest |
| `get_annotations` | — | marks, regions (asset mark, box, note, intent), notes, questions, links |
| `search_nodes` | `query`, `types?` | matching notes, regions, captions, frames with ids |
| `list_assets` | `kind?`, `source?`, `pinned?` | run media (VariantAsset + Artifact) with ids, stages, URIs and pin state |
| `get_screenshot` | `items?`, `layout?`, `columns?`, `labels?`, `title?` | board snapshot, or a composed sheet of the given nodes; attached to the next round, not pinned |
| `get_run_status` | — | stage status, retries, variant count, winner |
| `batch` | `calls: [{name, args}]` | sequential results for up to 24 calls |

## Write tools

All writes are stamped `created_by = "agent:<agent_name>"` and can only modify
nodes the same actor created in this pass. Human nodes are read-only.

| tool | args | effect |
| --- | --- | --- |
| `create_note` | `text`, `intent?`, `near?`, `x?`, `y?` | note node; `intent=question` is highlighted in the UI |
| `create_region` | `asset` (id or mark), `box: [x,y,w,h]` (0–1), `note`, `intent?` | region parented to the asset |
| `create_edge` | `from`, `to`, `label?`, `kind?` (annotation only) | annotation arrow |
| `place_assets` | `items: [{asset_id? \| uri, caption?, role?}]`, `layout`, `columns?`, `link_from?` | pins run media in a row/grid/stack and links output → source |
| `create_asset_node` | `asset_id? \| uri?`, `x?`, `y?`, `role?`, `caption?`, `link_from?` | one precise pin |
| `set_node_props` | `node_id`, `caption?`, `role?` | agent nodes only |
| `move_nodes` | `node_ids`, `dx?`/`dy?` or `x?`/`y?` | agent nodes only |
| `delete_nodes` | `node_ids` | agent nodes only; assets with operator regions are protected |
| `set_goal` | `text` | only fills an empty goal; never replaces human direction |
| `create_board` | `title?`, `source_board_id?` | new tab; must be the first write in a pass |
| `compose` | `items`, `layout`, `columns?`, `labels?`, `title?`, `pin?`, `caption?`, `link_from?` | labelled PIL contact sheet under `assets/boards/<run>/<board>/compose/`, optionally pinned |
| `batch` | `calls` | sequential execution, still one revision |

Node ids are deterministic per `(stage_task_id, actor, sequence)`
(`n_<sha1[:6]>_01`), so a retry after a revision conflict replays onto the new
scene without duplicate nodes.

## Images

`get_screenshot` and `compose` return local run paths (for example
`assets/boards/<run>/<board>/compose/sheet_<token>_01.png`). The stage pass
attaches them to the next model round; the trace UI renders the first one as a
thumbnail through `/api/media?path=...`.

Compose only accepts this run's media (VariantAsset/Artifact ids, pinned node
ids or marks, run-owned files, data URIs). Remote `http(s)` sources are fetched
with a size cap.

## HTTP harness surface

```http
POST /runs/{run_id}/board-docs/{board_id}/tools
Content-Type: application/json

{
  "name": "create_note",
  "args": {"text": "Logo 贴边有裁切风险", "intent": "finding", "near": "1"},
  "actor": "visual_qa_agent",
  "stage_task_id": "manual-check",
  "summary": "QA finding"
}
```

Response:

```json
{
  "ok": true,
  "result": {"ok": true, "node_id": "n_ab12cd_01", "...": "..."},
  "images": [],
  "revision": 7,
  "board": {"id": "...", "revision": 7, "scene": {"...": "..."}}
}
```

Invalid arguments and unknown tools return `422`; a stale `expected_revision`
during the save returns `409`. The endpoint commits one revision and records
`tool:<name>` plus, when the scene changed, `agent_edit` events.

## MCP server (`boardwright`)

`app/mcp/boardwright.py` is a stdio MCP server for external agents (Claude
Desktop, coding agents, the MCP Inspector). It exposes all 18 `TOOL_SPECS`
entries as MCP tools. Names, descriptions and JSON input schemas are generated
from the same pydantic argument models the HTTP route uses, so limits such as
`MAX_NOTE_CHARS`, `MAX_COMPOSE_ITEMS` and `MAX_BATCH_CALLS` stay in sync.

The server runs **in-process**: every MCP call opens a `SessionLocal` session
and runs a `BoardToolSession`; a write lands as exactly one board revision,
while reads do not open one. It needs direct access to the same database and
run assets as the Crispy app (SQLite handles the concurrent access; the app and
the server must share the `.db` file). The alternative — calling `POST .../tools`
— would be stateless and could run remotely, but would require the API server
plus credentials and could not return images as native MCP `ImageContent`.

Safety is unchanged: `actor` is required at startup, every write is stamped
`agent:<actor>`, and the service only lets agents modify agent-owned nodes.
Human notes, regions and assets stay read-only. Each MCP call gets an internal
provenance nonce, so deterministic node ids never collide across calls.
`compose`/`get_screenshot` results are attached as `ImageContent` when the
generated file is readable; `structuredContent` carries the same payload as the
JSON text.

### Run it

```bash
uv run python -m app.mcp.boardwright \
  --run-id "$RUN_ID" \
  --actor external_agent \
  --database-url "sqlite:///./crispy.db" \
  --board-id "$BOARD_ID"
```

`--board-id` is optional: without it the server starts on the run's latest
saved board. `create_board` moves the server onto the new tab for subsequent
calls. Use `--root` if relative asset paths (compose sheets, screenshots)
should resolve against a directory other than the server's working directory.

| CLI flag | env var | required | meaning |
| --- | --- | --- | --- |
| `--run-id` | `BOARDWRIGHT_RUN_ID` | yes | Pipeline run to bind the server to |
| `--actor` | `BOARDWRIGHT_ACTOR` | yes | Provenance name (`agent:<actor>` on writes) |
| `--board-id` | `BOARDWRIGHT_BOARD_ID` | no | Fixed tab; default is the latest saved board |
| `--database-url` | `BOARDWRIGHT_DATABASE_URL` | no | Defaults to the app's `CRISPY_DATABASE_URL` |
| `--stage-task-id` | `BOARDWRIGHT_STAGE_TASK_ID` | no | Prefix for deterministic ids; default `manual` |
| `--root` | `BOARDWRIGHT_ROOT` | no | Base for relative asset paths; default cwd |

The server validates the run and board at startup and exits with a message on
stderr (stdout is reserved for MCP) when either is missing.

### Connect a client

Claude Desktop (`claude_desktop_config.json`). Use absolute paths; `--directory`
makes `uv` resolve the Crispy project and its environment:

```json
{
  "mcpServers": {
    "boardwright": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/absolute/path/to/crispy",
        "python", "-m", "app.mcp.boardwright",
        "--run-id", "<run-id>",
        "--actor", "external_agent",
        "--board-id", "<board-id>"
      ],
      "env": {
        "CRISPY_DATABASE_URL": "sqlite:////absolute/path/to/crispy/crispy.db"
      }
    }
  }
}
```

MCP Inspector:

```bash
npx @modelcontextprotocol/inspector \
  uv run python -m app.mcp.boardwright \
  --run-id "$RUN_ID" --actor external_agent --board-id "$BOARD_ID"
```

`mcp dev` / `mcp install` are not used: they only accept FastMCP instances,
while `boardwright` uses the low-level MCP server so the pydantic schemas pass
through verbatim. Add the JSON block above manually (or point any stdio
inspector at the `uv run python -m …` command).

Client tests: `uv run pytest tests/test_boardwright_mcp.py -q` spawns the real
server over stdio, lists the tools, runs `get_board`, `create_note` and
`compose`, and checks the committed revision and `created_by`.

## Board events and review

- Per-tool event: `tool:<name>`, payload `{args, stage_task_id, images}`
- One `agent_edit` revision event per pass with `{summary, stage_task_id, tools}`
- Questions raised by an agent are stored on the stage task as
  `metadata_json["board_questions"]` and pause auto-advance (except
  `full_auto`), so the operator sees them before approving
- The UI polls the open board every 5 seconds: a clean board is replaced in
  place ("Agent updated this board · rN"); unsaved human work is never
  replaced — the operator gets a conflict notice and can save or discard first

## Checks

```bash
uv run python -m pytest tests/test_board_tools.py tests/test_board_tool_protocol.py tests/test_board_tool_pass.py tests/test_instruction_boards.py tests/test_boardwright_mcp.py -q
cd frontend && npx tsc -b
```

## Current limitations

- The stage pass reuses the stage's DB session instead of opening its own. It
  must see `VariantAsset` rows the current stage has not committed yet; the
  worker runs one stage at a time and the local database is SQLite, so the
  same-session trade-off is currently safe. A separate session would break
  "pin what this stage just produced".
- `visual_quality_assessment`, `evaluation_selection` and `video_generation`
  receive the tool pass, but their main stage prompts still do not consume the
  operator digest (`BOARD_STAGE_SCOPE_STAGES` is unchanged). They can mark and
  ask on the canvas; their generation prompt will not yet read new human
  instructions.
- Not implemented: `get_selection`, `get_viewport`, locking, resize/group/
  reparent, data edges, and `bind_run`/`validate_graph`/`run_stage`.
