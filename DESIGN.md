# Crispy console — version 4

Production workspace for finding runs, inspecting stages and product references, creating work, and managing shop credentials. Keep the approved page-based workflows and forest green. The user's supplied TaskPilot screenshot now pins the material treatment: warm gray canvas, white application shell, pale panel frames, fine warm borders, soft contact shadows and dark green navigation selection.

## Structure

- Desktop shell: centered rounded white application on a warm gray canvas, 32px outer margin (responsive), 24px outer radius. Header occupies the content column; sidebar carries the brand/workspace tile.
- Header: 78px white surface with compact page context and explicit demo/API mode. Mobile uses a 64px sticky header.
- Navigation: 238px pale sidebar (narrower on tablets), dark green selected item, soft 10px navigation corners. Narrow-screen navigation is hidden until opened and scrolls in short landscape viewports.
- Task index: full content width, state filters, search/mode toolbar and a single bordered table. No auto-selected task panel.
- Task media: a compact preview strip between task identity and status, up to three media tiles on desktop and one cover plus count on mobile. Clicking opens a focus-managed image/video gallery; generated media is prioritized and input references are labelled separately. Empty or broken media has an explicit fallback.
- Task page: addressable `#/tasks/:id`, breadcrumb return, compact resource identity, view buttons, factual metadata and a review workspace. Product references use contain sizing and link to the full image.
- Creation: addressable `#/new`, three steps for product/project, creative settings and confirmation. File selection, preset choices and preflight feedback belong in the page flow.
- Shop settings: `#/shops`, independent shop/account editors, and `#/shops/shared` for global mappings. No agent-management screen; future Magpie integration is outside this layer.

## Visual system

White surfaces on warm `#f0eeeb`, pale panel `#faf9f7`, foreground `#302e32`, muted text `#706e72`, borders `#e8e5e1`, primary `#175c46` and navigation selection `#253d33`. System UI typography, 24px compact page labels, 12–13px operational text and 11px metadata. Controls use 8–9px radii; panels 13–15px, inset tables 10–11px. Fine borders and small offset contact shadows follow the reference's layered material. Desktop outer shell has a broad low-opacity offset shadow. No copied metric cards, decorative widgets or reference-specific product claims.

## Interaction and content rules

- No H1/H2 elements, including titles. Page-title paragraphs receive focus after route changes.
- Remove slogans and redundant explanatory prose. Keep essential labels, state, validation and source disclosure.
- Lucide icons for familiar actions, accessible names and tooltips; brief text remains on important submissions.
- shadcn-style source-owned controls, semantic tokens, explicit input labels, visible focus and reduced-motion support.
- New resources use page navigation rather than drawers. Native back/reload must work.
- Polling preserves the selected detail view. Closed mobile navigation is not keyboard reachable.
- Demo data is explicitly labelled and never substitutes for failed live requests. Future stage state must be truthful.
- Preflight errors stop submission. Warnings require explicit continuation. Reference-file limits match backend intake limits.
- Aceternity's free Glowing Effect provides a subdued green pointer edge on the shop identity and material tiles. Motion shared-layout selection now uses a raised white segment on a pale track, matching the reference material. Material tiles lift only 3px. No full-screen shader, continuous ambient animation or light effect behind credential inputs.
- Reduced-motion preferences are observed initially and when changed at runtime. Hover tracking runs only over an eligible mouse target; superseded animations are stopped.
- Credentials are environment-variable references only. No secret-value fields or browser storage. Local readiness must never imply successful platform authentication.
- Preserve drafts after failed saves, serialize pending mutations across settings routes, and ignore late navigation effects after leaving a form. Archive is reversible through the archived filter and restore action.

Live full-output review still opens the existing dashboard. No approval is offered in the new live preview without complete review content. Generated-variant comparison, advanced creation controls, configuration and deployment integration remain migration work.

Current reference-driven review and verification are recorded in `docs/design/review-v4.md`. The material layer is centralized in `frontend/src/surface.css`; `styles.css` retains the functional layout/state foundations. Prior workflow and credential verification remains in `review-v2.md` and `review-v3.md`. Aceternity Glowing Effect source was installed from its public registry; Motion is installed. No paid component source was used.
