# Agent Operating Rules

- When uncertain, explain the assumption first. Do not hide confusion; state the tradeoff honestly.
- For large projects or heavy tasks, split work into small tasks, sort by necessity, build the necessary layers first, and commit after each completed layer.
- Use Conventional Commits. Match the language used by prior commits; if there is no precedent, use English.
- When communicating with Figma, use `figwright` to share and discuss design drafts.

# Crispy — Technical Reference

## Architecture Snapshot

```yaml
project: crispy
language: Python 3.11+
runtime: uv
api: FastAPI + Uvicorn
orm: SQLAlchemy (SQLite local, PostgreSQL-compatible schema)
schema: Pydantic (strict contracts between agents)
ui: Server-side rendered dashboard pages
db: SQLite (local) with JSONB-compatible columns
```

## Pipeline Modes & Stage Plans

### Pipeline Modes

```yaml
copy_image_only:
  - intake → planning → divergence → copy_image_generation
  - visual_quality_assessment → evaluation_selection

video_only:
  - intake → planning → divergence → video_scripting
  - storyboard_image_generation → video_generation
  - visual_quality_assessment → evaluation_selection

full_multimodal:
  - intake → planning → divergence
  - copy_image_generation
  - video_scripting → storyboard_image_generation → video_generation
  - visual_quality_assessment → evaluation_selection

marketplace_main_image:
  - Same as copy_image_only + marketplace QA checks

tiktok_shop_video:
  - Same as video_only + TikTok-specific creative specs
```

### Stage → Agent Mapping

```yaml
intake: gm_orchestrator
planning: planning_agent
divergence: variant_strategy_agent
copy_image_generation: copy_image_agent
video_scripting: video_script_agent
storyboard_image_generation: storyboard_agent
video_generation: video_generation_agent
visual_quality_assessment: visual_qa_agent
evaluation_selection: evaluation_agent
```

Source of truth: `app/agents/registry.py`

### Approval Modes

| Mode | Behavior |
|---|---|
| `manual` | Every stage pauses for human review |
| `semi_auto` | Strategy stages auto-advance; creative/evaluation stages hold |
| `full_auto` | All stages auto-advance; visual_qa failures trigger up to 2 regeneration cycles |

Controlled via `StageTask.status` lifecycle: `draft → queued → running → waiting_review → approved | rejected | failed`

## Agent Personas

Persona files are structured markdown loaded at runtime:

```
personas/
├── gm/gm_orchestrator.md       # Intake processing
├── stages/01_product_research_agent.md
├── stages/02_planning_agent.md  # Strategy drafting
├── stages/03_variant_strategy_agent.md
├── stages/04_copy_image_agent.md
├── stages/05_video_script_agent.md
├── stages/06_storyboard_agent.md
├── stages/07_video_generation_agent.md
├── stages/08_visual_qa_agent.md # Visual quality assessment
├── stages/08_evaluation_agent.md
├── stages/09_compliance_agent.md# Compliance checks
└── stages/shop_analyst.md       # Shop analysis
```

Personas are versioned (`persona_version` table) and editable via the dashboard.

## Data Models

```
workspace → project → product → campaign → pipeline_run
                                         → stage_task
                                         → run_variant → variant_asset
                                                       → variant_review
                                                       → variant_score
                                         → artifact
                                         → scorecard
                                         → agent_trace_event

workspace → feedback_import → gm_memory
                            → gm_instruction_version

workspace → integration_sync
          → content_schedule

global: agent_api_config, integration_config, creative_preset, run_template, persona_version
```

### Key Models

| Model | Purpose |
|---|---|
| `PipelineRun` | One generation run bound to product + campaign |
| `StageTask` | Individual stage execution with retry, priority, failure tracking |
| `RunVariant` | One creative variant (angle + hook + message) |
| `VariantAsset` | Generated asset (copy, image, storyboard_frame, video, video_script) |
| `VariantScore` | Evaluation/compliance/visual_quality scores per variant |
| `AgentTraceEvent` | Per-stage execution log for debugging |
| `GmMemory` | Cross-run strategy memory (product/industry/shop scopes) |
| `ResearchTask` | Durable Research Intelligence queue item for store, competitor, industry, audience, and compliance tasks |
| `ContentSchedule` | Publishing schedule entries, synced to Notion |

## API Reference

### Run Management
```
GET    /runs                          # List runs
POST   /runs                          # Create run (JSON)
POST   /runs/rich                     # Create run (multipart: files + JSON)
GET    /runs/{id}                     # Run detail
POST   /runs/{id}/advance             # Approve current stage → advance
POST   /runs/{id}/reject              # Reject current stage → requeue
GET    /runs/{id}/deliverables        # Winner variant assets
GET    /runs/{id}/variants            # All variants with scores, assets, reviews
GET    /runs/{id}/events              # Agent trace events
POST   /runs/preflight                # Pre-flight capability check
```

### Variant Actions
```
POST   /runs/{run_id}/variants/{variant_id}/review          # Human review action
POST   /runs/{run_id}/variants/{variant_id}/regenerate       # Request regeneration
POST   /runs/{run_id}/assets/refresh                         # Poll async video tasks
```

### Configuration
```
GET    /agent-configs                 # List all agent API configs
PATCH  /agent-configs/{agent}         # Update agent config
GET    /agent-configs/env-vars        # List CRISPY_API_KEY_* env var names
GET    /integration-configs           # List integration credentials
PATCH  /integration-configs/{id}      # Update integration credential
```

### Feedback & Memory
```
POST   /feedback/import               # Import CSV feedback rows
GET    /projects/{id}/leaderboard     # Creative performance ranking
GET    /gm-memory                     # List GM memory entries
POST   /gm-memory/{id}/review         # Approve/reject/resolve conflicts for research_intelligence memory
```

### Research Intelligence
```
GET    /research-context                    # Included/excluded research context for planning and dashboards
GET    /shop-analysis/preflight             # LLM/Tavily/Firecrawl readiness for Research Intelligence
POST   /shop-analysis/run                   # Create and optionally execute a research task
GET    /shop-analysis/history               # Latest research memory by shop/store
GET    /shop-analysis/tasks                 # Research task history
GET    /shop-analysis/tasks/{id}            # Single research task status
POST   /shop-analysis/tasks/{id}/execute    # Execute/retry a queued or failed research task
GET    /shop-analysis/queue/status          # Research worker queue status
POST   /shop-analysis/refresh-due           # Queue expired/refresh-soon research memory for refresh
```

Research focus values:
`full_intelligence`, `store_context`, `competitive_landscape`, `industry_baseline`, `audience_pain_points`, `compliance_scan`.

### Database Backup
```
POST   /backup                        # Create timestamped backup
GET    /backups                       # List available backups
POST   /backup/restore                # Restore from a backup
```

### UI Metadata
```
GET    /pipeline-modes                # Available pipeline modes
GET    /creative-presets              # Saved creative presets
GET    /run-templates                 # Saved run templates
GET    /personas                      # Agent persona metadata
GET    /personas/{agent}              # Persona content
PATCH  /personas/{agent}              # Update persona
GET    /shops                         # List shop workspaces
GET    /shops/{name}/categories       # Product categories for a shop
```

## Model Routing

### Configuration Priority
1. Per-agent config (`/agent-configs/{agent}`)
2. Default config (`agent_name=default`)
3. Run payload legacy fields (deprecated)

### Generation Agent Split Config
The `generation_agent` supports three independent model configurations:

```yaml
text:  top-level provider_name/model_name/api_base_url/api_key_env
image: extra.image_config (provider_name/model_name/api_base_url/api_key_env)
video: extra.video_config (provider_name/model_name/api_base_url/api_key_env)
```

### API Key Security
Only environment variable names are stored in the database (`api_key_env` column). The actual key values are read from `os.environ` at runtime. All keys use the `CRISPY_API_KEY_*` prefix convention and are auto-discovered by the configs page.

## Memory System (GM)

### Architecture

```
Feedback Import ──→ GmMemory (product scope)
                 ──→ GmMemory (industry scope)
                 ──→ GmMemory (shop scope)

Shopify Sync ──→ GmMemory (product_intelligence)
              ──→ GmMemory (store_intelligence)

Meta Sync ──→ GmMemory (store_intelligence)

Research Intelligence ──→ GmMemory (research_intelligence)
                      ├─ shop_profile
                      ├─ competitor_analysis
                      ├─ industry_baseline
                      ├─ audience_pain_points
                      └─ compliance_scan
```

### Write Path
- `POST /feedback/import` → writes product + industry GmMemory entries
- Shopify/Meta auto-sync → writes product/store intelligence
- Operator variant review (marketplace QA tags) → writes visual_quality memory
- Research Intelligence → writes shop/industry-scoped `research_intelligence` memory with evidence, quality, conflicts, freshness, and review state
- `GmInstructionVersion` incremented on each feedback import

### Read Path
- `planning` stage: `_recent_gm_lessons()` queries recent memories by `product_code` + `industry_code`
- Shop-scoped research memory is included when `content.shop_id == PipelineRun.workspace_id`
- Memory is gated by `memory_is_strategy_safe()`; weak evidence, expired research, unresolved conflicts, low confidence, or archived status are excluded unless pinned/reviewed
- `build_research_context()` summarizes included/excluded research for Create Run, Run Detail, Agent Trace, GM Review, Data Dashboard, and planning prompts
- Merged with analytics insights (sales velocity, creative fatigue, creative comparison)
- Injected into planning agent prompt as `gm_lessons` plus `research_context`

### Research Quality & Governance
- Evidence is normalized with source URL, query, evidence category, fetched timestamp, quality score, and quality tier.
- `audience_pain_points` uses review/community evidence categories.
- `compliance_scan` uses policy/regulatory evidence categories.
- Expired or soon-to-expire memory can be refreshed through `/shop-analysis/refresh-due`.
- Conflicted research is held out of planning until resolved in GM Review or explicitly pinned.

### Attribution Logic
- **Shopify → Product**: Order line items matched to `Product.product_code` via variant SKU
- **Meta → Product**: Ad insights matched through Campaign → Product chain
- **Shopify → Store**: Aggregated revenue/quantity across all matched products
- **Meta → Store**: Aggregated spend/revenue/impressions across all ad insights
- **Industry**: Set from `Workspace.industry_code` or `PipelineRun.industry_code`

## Capability Preflight

Prevents runtime failures by checking model availability before run creation.

```
POST /runs/preflight
{
  "pipeline_mode": "video_only",
  "has_image_inputs": false,
  "has_video_inputs": true
}

Response:
{
  "ok": true,
  "severity": "ok | warn | error",
  "checks": [
    {
      "key": "video_generation.video_generation",
      "severity": "error",
      "message": "Video generation not configured for generation_agent",
      "stage_name": "video_generation",
      "agent_name": "generation_agent"
    }
  ]
}
```

Dashboard blocks run creation on `error` severity; warns on `warn`.

## Multimodal Input Processing

### Intake Stage
- Accepts: `.csv`, `.xlsx` (SKU data), `.png`, `.jpg`, `.jpeg`, `.webp` (images), `.mp4`, `.mov`, `.m4v` (videos)
- Limits: max 10 files, 50MB per file, 200MB total
- Video understanding with automatic fallback to image-only on failure
- Output: `ProductIntake.asset_media_summary` — shared multimodal context consumed by downstream stages

### Downstream Consumption
- `copy_image_generation`: Uses `asset_media_summary` for copy and image prompts
- `video_scripting`: Injects `asset_media_summary` for product-accurate hooks and scripts

## Data Source Switching

The dashboard supports switching between SQLite database files at runtime via the Shop selector. Any `.db` file in the project root (excluding `.git`, `.venv`, `node_modules`) is auto-discovered.

Implementation: `switch_database_url()` in `app/data/session.py` — rebuilds engine and session factory, applies runtime migrations.

## Worker & Concurrency

The `PipelineWorker` runs background task processing:
- Configurable concurrency (`worker_concurrency`, default 1)
- Atomic task claiming via guarded UPDATE
- Priority queue (0=human-rejected, 1=regen, 2=normal)
- Retry with exponential backoff for provider errors and timeouts
- Video poller loop for async video generation tasks
- Orphaned task recovery on startup

The `ResearchWorker` runs alongside the pipeline worker when `CRISPY_ENABLE_WORKER=true`:
- Claims queued `ResearchTask` rows
- Executes store/competitor/industry/audience/compliance research through `execute_research_task()`
- Recovers orphaned running research tasks on startup
- Exposes queue visibility through `/shop-analysis/queue/status`

## Environment Variables

All configuration uses the `CRISPY_API_KEY_*` prefix:

```bash
# LLM API keys
CRISPY_API_KEY_OPENAI
CRISPY_API_KEY_DEEPSEEK
CRISPY_API_KEY_KIMI

# Notion integration
CRISPY_API_KEY_NOTION              # Internal integration token
CRISPY_API_KEY_NOTION_DATABASE     # Database 32-char ID

# Shopify (reserved)
CRISPY_API_KEY_SHOPIFY

# Meta Ads (reserved)
CRISPY_API_KEY_META

# Research Intelligence search tools
CRISPY_API_KEY_TAVILY
CRISPY_API_KEY_FIRECRAWL

# Database
CRISPY_DATABASE_URL=sqlite:///./crispy.db
CRISPY_ENABLE_WORKER=true
```

## Development

### Running Tests
```bash
uv run pytest tests/ -x -q
```

### Adding a New Pipeline Stage
1. Add stage name to `StageName` enum in `app/data/models.py`
2. Add stage to pipeline plans in `app/orchestrator/state_machine.py`
3. Add stage → agent mapping in `app/agents/registry.py`
4. Add execution branch in `execute_stage_task()` in `app/services/runs.py`
5. Add input assembly in `_build_task_input()`
6. Add agent runtime method in `app/agents/runtime.py`
7. Create persona file at `personas/stages/`

### Adding a New Integration Provider
1. Implement provider adapter in `app/integrations/`
2. Register in `app/integrations/__init__.py`
3. Add integration config entries for credential env vars
4. Add sync logic in `app/integrations/sync_service.py`
