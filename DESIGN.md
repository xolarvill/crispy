# Crispy console — version 2

Production workspace for finding runs, inspecting stages and product references, and creating work. The current visual direction follows the user's request for a Cloudflare-like minimalist console. The previous green split-pane workspace and slide-in creation drawer have been replaced.

## Structure

- Global header: 62px white surface, orange Lucide Layers mark, local workspace context and explicit demo/API mode.
- Navigation: 214px white sidebar with task/material resources, operational links and settings. Narrow-screen navigation is hidden until opened.
- Task index: full content width, state filters, search/mode toolbar and a single bordered table. No auto-selected task panel.
- Task page: addressable `#/tasks/:id`, breadcrumb return, compact resource identity, view buttons, factual metadata and a review workspace. Product references use contain sizing and link to the full image.
- Creation: addressable `#/new`, three steps for product/project, creative settings and confirmation. File selection, preset choices and preflight feedback belong in the page flow.

## Visual system

White surfaces on `#f8f9fb`, foreground `#22252a`, primary `#c94b07`, resource links `#1b60b9`, borders `#dfe2e6`. System UI typography, 22px compact page labels, 12–13px operational text and 11px metadata. Controls use 4px radii and fine borders. Orange identifies primary actions and current navigation; statuses use text plus dots, never color alone. Layout has no decorative gradients, hero metrics or persistent panel shadows.

## Interaction and content rules

- No H1/H2 elements, including titles. Page-title paragraphs receive focus after route changes.
- Remove slogans and redundant explanatory prose. Keep essential labels, state, validation and source disclosure.
- Lucide icons for familiar actions, accessible names and tooltips; brief text remains on important submissions.
- shadcn-style source-owned controls, semantic tokens, explicit input labels, visible focus and reduced-motion support.
- New resources use page navigation rather than drawers. Native back/reload must work.
- Polling preserves the selected detail view. Closed mobile navigation is not keyboard reachable.
- Demo data is explicitly labelled and never substitutes for failed live requests. Future stage state must be truthful.
- Preflight errors stop submission. Warnings require explicit continuation. Reference-file limits match backend intake limits.

Live full-output review still opens the existing dashboard. No approval is offered in the new live preview without complete review content. Generated-variant comparison, advanced creation controls, configuration and deployment integration remain migration work.

Impeccable review, verification evidence, reference-photo provenance and concept-generation prompt are recorded in `docs/design/review-v2.md`. The current design contract is `docs/design/surface-v2.md`. Fluid Functionalism and Aceternity packages are not installed; the new direction follows this user's explicitly requested console style.
