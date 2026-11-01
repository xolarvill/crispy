# Raised detail media and same-page popup — 2026-10-31

Moved current stage, creative mode, product code and update time into the desktop task-title row. Removed their old separate block below the view selector, bringing media and execution panels higher. Responsive layouts wrap this metadata beneath the title without page overflow.

Replaced the external reference-image anchor with a task-media panel using the existing `/runs/{id}/media` API. It shows a main cover, count, previous/next controls, expand action and selectable filmstrip. Image/video preview actions open the current page's Radix popup. The popup's original-media external link is removed. Popup and detail selection synchronize; closing returns focus and retains the last media. Loaded detail metadata is reused. Long filmstrips reveal the selected item without scrolling the page.

Interactions include thumbnail selection, previous/next, keyboard arrows, image swipe, native video controls and Escape/close. Video gestures remain native; multitouch/cancel clears image-swipe tracking. Failed detail media has visible feedback. Reference/generated labels remain accurate; no H1/H2 elements were added.

The third demo task now represents a labelled apparel reference collection with three existing repository photos. This demonstrates multi-media interaction without claiming generated outputs for one product.

Verification:

- Strict TypeScript and Vite production build pass.
- Desktop metadata shares the title row; the cover begins 186px below the title at 1600px viewport, with no standalone metadata block in between.
- Clicking detail media keeps the URL and browser-tab count unchanged. Popup opens the selected item, supports keyboard/thumbnail selection, restores focus, and synchronizes selection back to detail.
- Three-item demo and controlled twelve-item live response work. Previous/next wraps correctly; long strips reveal the active item. Opening the popup does not re-request already-loaded metadata.
- Mobile 390px has no page overflow. Image swipe changes media and synchronizes selection. Native video metadata/controls were verified with a tiny temporary local clip; no provider or production data was used.
- Browser checks completed without page exceptions. No backend code changed, so previously documented backend baseline failures were not rerun.

Artifacts: `detail-media-raised.png`, `detail-media-multiple.png`, `mobile-detail-media.png`, `mobile-detail-popup.png`. These are browser captures using existing reference photos. Final desktop and mobile popup captures were visually inspected. Temporary browser script and clip are removed; the existing port-5174 preview stays available. Work remains on `codex/frontend-console`.
