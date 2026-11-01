# Operations pages — first migration layer

New React routes: `#/research`, `#/calendar`, `#/data`. Sidebar links stay in the application. No global header or H1/H2 was added.

Implemented: research task listing by shop, active memory listing across shops, search and record details; schedule date/shop query and record detail; project/shop summary metrics. Demo mode is explicit. Live errors remain errors, with retry. Components are lazy loaded.

Remaining migration work: research creation/execution and readiness, memory governance/review actions and evidence-specific presentation; schedule create/edit/delete/publish, calendar grid and media selection; data imports, integrations sync, trends and creative/product analytics. Persona/API configuration stays deferred for Magpie. Existing product-assets backend parity also needs its own audit.

Validation: production TypeScript/Vite build passed. Browser plugin unavailable; Playwright Edge checked desktop 1500 and mobile 390 on all three routes, memory navigation, API contract fixtures, error rendering and overflow. No page exceptions. Localhost sockets were blocked by the environment, so browser requests served production assets through interception. This is not real-backend integration verification. Screenshots alongside this file capture this pass.

## Research navigation split

Product research (`#/product-research`), competitor research (`#/competitor-research`), and memory (`#/memory`) are independent pages and sidebar entries. The old `#/research` route resolves to product research for compatibility. Competitor research shows only `competitive_landscape`; product research shows the remaining existing research types with their original labels. The backend currently supplies shop/industry/audience research, not a separate SKU-specific product-research workflow. Memory is queried independently across shops. No tabs remain.

Verified production build and desktop/mobile browser route/type-isolation/detail checks with intercepted production assets. Real backend integration remains unverified in this socket-restricted environment.
