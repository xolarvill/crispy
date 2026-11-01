# Task list media previews — 2026-10-31

Added a media column to the task list. Desktop rows show up to three actual images/videos and an overflow count; mobile rows show a single cover and total count. Empty media displays an inert placeholder. Clicking a tile opens a Radix focus-managed gallery with full-image fitting, native video controls, previous/next selection, keyboard arrows, Escape/close and focus restoration. Full gallery metadata is requested only after opening, with retry on failure. Broken media has a visible fallback. No H1/H2 tags were added.

Real data support is additive: `RunSummary` now includes `media_count` and `media_previews`; `/runs/{id}/media` returns full media metadata. The batched SQL projection unions generated variant media and original/legacy artifacts, deduplicates by URI/type, excludes failed or queued-without-URI assets and auxiliary input frames, prioritizes generated assets and returns at most three list previews per run. One list query plus one metadata query handles all returned runs. No schema migration, provider call or media-file transformation is involved. Files are served through the existing guarded `/media` route; HTTP(S) media can load directly. Video thumbnail metadata loads only when the tile approaches the viewport; no video autoplay is enabled.

Demo rows use their existing repository reference photos, clearly labelled as references in the preview; no generated variants or video results are fabricated.

Verification:

- Production TypeScript/Vite build passes.
- **6 focused tests pass** in `test_run_media.py` and `test_rich_run.py`. Coverage includes deduplication, actual counts, first-three summaries, generated/source distinction, legacy URI support, failed/empty media, run isolation, constant list query count and missing-run 404.
- Browser verification passes: demo and controlled live thumbnails, complete gallery loading only after click, overflow count, retry, native video metadata/control rendering using a tiny local test clip, broken-image fallback, keyboard switching, Escape focus restoration, no H1/H2 and desktop/mobile rendering. At 390px neither the page nor the task table requires horizontal scrolling.
- Visual inspection found and fixed an initial long-image crop by giving the gallery a definite bounded height; full media now fits without clipping. Mobile thumbnail and close controls use 44px-wide targets. Tooltips appear above the gallery overlay.

Broader regression runs encountered two pre-existing failures:

- `tests/test_pipeline_api.py::test_pipeline_mode_copy_image_only`: stage-task order differs from the expected pipeline order before media listing is called.
- `tests/test_dashboard_assets.py::test_dashboard_create_run_has_accordion_sections`: expected legacy HTML input markup differs from current template markup.

Both failures were reproduced using the committed pre-change router loaded in an isolated test harness, with production files untouched. They are not reported as passing and were not changed in this focused feature. Initial expanded runs reached 11 and 16 passing tests respectively before stopping at those failures. No production database, credentials or provider assets were altered by QA.

Artifacts: `task-thumbnails.png`, `task-media-preview.png`, `mobile-task-thumbnails.png`, `mobile-task-media-preview.png` are browser screenshots. Product photos remain existing repository assets. Temporary test clip and QA harnesses are removed; the preview remains at port 5174 for user review. No new service or dependency was installed.
