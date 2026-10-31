# Crispy console — version 3

Production workspace for finding runs, inspecting stages and product references, creating work, and managing shop credentials. Keep the user's approved minimalist resource-console structure, restore forest green and add restrained Aceternity interaction polish.

## Structure

- Global header: 62px white surface, green Lucide Layers mark, local workspace context and explicit demo/API mode.
- Navigation: 214px white sidebar with task/material resources, operational links and settings. Narrow-screen navigation is hidden until opened.
- Task index: full content width, state filters, search/mode toolbar and a single bordered table. No auto-selected task panel.
- Task page: addressable `#/tasks/:id`, breadcrumb return, compact resource identity, view buttons, factual metadata and a review workspace. Product references use contain sizing and link to the full image.
- Creation: addressable `#/new`, three steps for product/project, creative settings and confirmation. File selection, preset choices and preflight feedback belong in the page flow.
- Shop settings: `#/shops`, independent shop/account editors, and `#/shops/shared` for global mappings. No agent-management screen; future Magpie integration is outside this layer.

## Visual system

White surfaces on `#f8f9fb`, foreground `#22252a`, primary `#175c46`, primary hover `#104b38`, selected surface `#eaf3ee`, resource links `#1b60b9`, borders `#dfe2e6`. System UI typography, 22px compact page labels, 12–13px operational text and 11px metadata. Controls use 4px radii and fine borders. Green identifies primary actions and current navigation; statuses use text plus dots, never color alone. No decorative hero metrics or persistent panel shadows.

## Interaction and content rules

- No H1/H2 elements, including titles. Page-title paragraphs receive focus after route changes.
- Remove slogans and redundant explanatory prose. Keep essential labels, state, validation and source disclosure.
- Lucide icons for familiar actions, accessible names and tooltips; brief text remains on important submissions.
- shadcn-style source-owned controls, semantic tokens, explicit input labels, visible focus and reduced-motion support.
- New resources use page navigation rather than drawers. Native back/reload must work.
- Polling preserves the selected detail view. Closed mobile navigation is not keyboard reachable.
- Demo data is explicitly labelled and never substitutes for failed live requests. Future stage state must be truthful.
- Preflight errors stop submission. Warnings require explicit continuation. Reference-file limits match backend intake limits.
- Aceternity's free Glowing Effect provides a thin green pointer edge on the shop identity and material tiles. Motion shared-layout underlines identify selected resource views. Material tiles lift only 3px. No full-screen shader, continuous ambient animation or light effect behind credential inputs.
- Reduced-motion preferences are observed initially and when changed at runtime. Hover tracking runs only over an eligible mouse target; superseded animations are stopped.
- Credentials are environment-variable references only. No secret-value fields or browser storage. Local readiness must never imply successful platform authentication.
- Preserve drafts after failed saves, serialize pending mutations across settings routes, and ignore late navigation effects after leaving a form. Archive is reversible through the archived filter and restore action.

Live full-output review still opens the existing dashboard. No approval is offered in the new live preview without complete review content. Generated-variant comparison, advanced creation controls, configuration and deployment integration remain migration work.

Current Impeccable review, verification evidence and provenance are recorded in `docs/design/review-v3.md`. Prior layout decisions remain in `docs/design/surface-v2.md`, superseded by the green palette and motion rules here. Aceternity Glowing Effect source was installed from its public registry; Motion is installed. Fluid Functionalism remains a design reference. No paid component source was used.
