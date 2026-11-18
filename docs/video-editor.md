# Local video editing

Open a live task's video preview and choose **Edit video**. Existing edits for that run reopen automatically, and **New edit** creates another independent project. Demo-mode media does not write projects.

On an instruction board, select a pinned video and choose **Edit video on board**. This opens the composition inside the board workspace and reopens the project attached to that pin. Use the **Project** picker to continue an existing edit for the run, including one started from Assets. Under **From this board**, choose pinned images or text to add them above the video on separate tracks. These layers start at the beginning of the composition and span its current duration. Select a timeline layer and drag its outline in the preview to position it, or set position, size and timing in the inspector. After export, choose **Add to board**, return to the board and save it to keep the new output in the layout.

## Editing

- Import video, image or audio files, or select media already registered to the task. Local files are copied into a content-addressed source library. Originals are not overwritten.
- Click a source to add it. Video and images append to the picture track; audio appends to an audio track. Add text creates an overlay track.
- Drag clips or their edges, split at the playhead, or trim either side of the selected clip to the playhead. The inspector supports timeline start and duration, source in and out points, frame fit, position, size, opacity and volume. Timing fields respect adjacent clips and the source duration.
- **Move earlier / Move later** swaps adjacent clips while retaining their durations. **Delete and close gap on track** moves later clips on that track left by the deleted clip's duration. **Track forward / Track backward** changes stacking for the entire track.
- Choose output dimensions, frame rate and background in Composition. Save explicitly. Undo/redo works within the editing session; **History** restores a saved revision as a new revision.
- Clean projects refresh when an agent saves. If local edits are unsaved, remote updates show a conflict notice and leave the draft intact. **Load latest** discards the unsaved local draft; it does not merge conflicting edits.
- **Export MP4** saves the current draft and starts a background render of that revision. Closing the editor does not cancel a server-side render; reopening resumes its status. Completed output is a new task asset, available to the existing gallery and board tools.

The browser preview uses native media elements driven by Twick's timeline clock. Twick provides timeline manipulation, selection and local undo history; Crispy owns the edit document, persistence, source references, public tools and FFmpeg render adapter. Twick's optional motion-graphics player is not the preview authority. Upstream UI styles are scoped to the editor and optional remote font imports are removed at build time. Timeline analytics are disabled.

## Requirements and limits

- Use the repository's normal Python environment and frontend installation. Twick packages are pinned to `0.15.31` in the npm lockfile. `npm run dev` / `npm run build` generate the scoped CSS automatically. No separate OpenMotion runtime is required.
- Install FFmpeg and FFprobe on PATH. The render adapter requires the `-/filter_complex` file option; local validation used FFmpeg **9.0.2**. Set `CRISPY_FFMPEG_PATH` and `CRISPY_FFPROBE_PATH` to explicit binaries when needed.
- Media: MP4, MOV, M4V, WebM, PNG, JPEG, WebP and common audio formats. Maximum 500 MB per file; video/audio must have a finite duration of at most one hour. Remote URLs are not downloaded by the editor: import local media first.
- Timeline: up to 16 tracks and 120 clips, no overlap within a track; place overlays on separate tracks. Maximum one-hour timeline, even output dimensions up to 4096 px, 1–120 fps including rational frame rates. These are contract limits, not claims of tested long-form/4K performance.
- Basic text uses system fonts. Exact typography can vary with installed fonts. Complex motion, transitions, effects, arbitrary HTML, speed changes, professional NLE project interchange and PR/DaVinci/AE adapters are outside this release.
- Rendering uses one local worker per application/MCP process. Jobs are persisted and claimed atomically. A restarted application resumes queued jobs and marks jobs from a dead owner as interrupted. Failed/cancelled jobs can be retried with a new request ID.
- Source files under `assets/video/<project>/sources` are immutable inputs. Do not modify them manually; export verifies hashes. Keep enough space in the configured asset directory for source copies and renders.

## MCP: videowright

The stdio adapter shares the same domain services as the UI and HTTP API. It does not require the API process to be running. The UI and MCP must use the same database and asset directory. No client configuration is installed automatically.

Example local-client configuration (replace paths for your machine):

```json
{
  "mcpServers": {
    "crispy-video": {
      "command": "uv",
      "args": ["--directory", "D:/CODING/crispy", "run", "python", "-m", "app.mcp.videowright", "--actor", "codex"],
      "env": {
        "CRISPY_DATABASE_URL": "sqlite:///D:/CODING/crispy/crispy.db",
        "CRISPY_ASSETS_DIR": "D:/CODING/crispy/assets"
      }
    }
  }
}
```

| Tool | Purpose |
| --- | --- |
| `list_video_projects` | List a run's video projects |
| `create_video_project` | Create an idempotent empty project in an existing run |
| `get_video_project` | Read the current revision, source inventory and timeline |
| `import_video_source` | Pin a local file; does not place it on the timeline |
| `edit_video_project` | Apply a batch of typed timeline operations |
| `get_video_history` | Read saved revisions and attribution |
| `restore_video_revision` | Restore a revision as new history |
| `get_video_frame` | Inspect a source at a timestamp, returning MCP image content |
| `render_video_project` | Queue a fixed-revision MP4 export |
| `get_video_render` | Poll status, progress and output |
| `cancel_video_render` | Cancel a queued/running export |

Read the latest project before editing. Times are **integer microseconds**, coordinates/sizes are fractions of output dimensions, and tracks are stored bottom-to-top. A typed operation batch is one atomic revision. Use stable request IDs for retries, and a new request ID for an intentional new operation. On a revision conflict, reread and preserve the user's edits before issuing another request.

Example split:

```json
{
  "project_id": "<project-id>",
  "expected_revision": 4,
  "request_id": "split-opening-1",
  "summary": "Split the opening at two seconds",
  "operations": [{"op": "split_clip", "clip_id": "<clip-id>", "at_us": 2000000, "new_id": "opening-right"}]
}
```

Operations: `add_track`, `remove_track`, `add_clip`, `update_clip`, `remove_clip`, `split_clip`, `settings`. `add_clip.values` follows `VideoClip` in `app/schemas/video.py`. Source IDs come from the pinned source inventory; file paths do not belong in clips. Export jobs pin document and source references even if later edits continue.

HTTP clients can use `POST /video-editing/tools` with `{name, arguments}` to call the same tool catalog. Direct UI routes include `/runs/{run_id}/video-projects`, `/video-projects/{id}`, `/sources`, `/sources/from-run`, `/operations`, `/history`, `/restore`, `/renders` and `/sources/{source_id}/frame`. Existing `/media?path=...` serves playback with HTTP Range support.

## Validation

```bash
uv run pytest tests/test_video_editing.py tests/test_video_rendering.py tests/test_instruction_boards.py tests/test_board_tools.py -q
cd frontend
npm run test:video
npm run build
```

Real-browser validation covered gallery entry, playback, split, text edits, undo/redo, image insertion, duration/reordering, persistence, clean remote sync, unsaved-draft conflict handling and local MP4 export back into run assets. The stdio MCP was separately checked for discovery, project reads, agent edits and image responses. Targeted tests include real audio/video rendering at rational frame rates, immutable snapshots and byte-range playback. This does not establish full backend regression coverage, professional-editor parity, or long-video/4K performance.

## Ownership and attribution

Authority: `app/schemas/video.py`, `app/services/video_editing.py`. Execution adapter: `app/services/video_rendering.py`. Shared agent contract: `app/services/video_tools.py`; stdio transport: `app/mcp/videowright.py`. Frontend mapping: `frontend/src/video-data.ts`.

[Twick](https://github.com/ncounterspecialist/twick) supplies the timeline SDK; its package licenses remain applicable. [OpenMotion](https://github.com/nexu-io/openmotion) was evaluated as an architectural reference; no OpenMotion application, model gateway or project authority is integrated. [Design scope](design/video-media-editing.md).
