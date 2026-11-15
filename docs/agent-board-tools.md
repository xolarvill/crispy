# Agent canvas tools

Stage agents can read and write the instruction board through a small, typed
tool surface. A pass mutates an in-memory copy of the board's `BoardScene` and
lands as **one** revertable revision, so an agent's work can be reviewed and
undone as a unit.

Implemented surface: `app/services/board_tools.py` (`TOOL_SPECS`,
`BoardToolSession`). Stage prompt and round loop: `app/services/board_tool_pass.py`.
External harness route: `POST /runs/{run_id}/board-docs/{board_id}/tools`.

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
uv run python -m pytest tests/test_board_tools.py tests/test_board_tool_protocol.py tests/test_board_tool_pass.py tests/test_instruction_boards.py -q
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
  reparent, data edges, `bind_run`/`validate_graph`/`run_stage`, and the
  standalone MCP `boardwright` server. The HTTP route above is the seam those
  clients will call.
