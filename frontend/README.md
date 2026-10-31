# Crispy frontend preview

```powershell
cd D:\CODING\crispy\frontend
npm install
npm run dev -- --port 5174
```

Open http://127.0.0.1:5174. The default is explicitly labelled demo mode. Choose 本地 API to connect through Vite's `/api` proxy to FastAPI at http://127.0.0.1:8000.

`npm run build` checks strict TypeScript and produces `dist/`. This preview runs independently of the existing dashboard; deployment integration is a later migration layer.

Version 2 replaces the former split-pane/drawer layout with a full-width task index, addressable task pages (`#/tasks/:id`) and three-step creation (`#/new`). Implemented: status/mode filters, search, task detail, stage status, full product reference, product material grid, input file selection, preset selection, preflight warnings, responsive navigation and labelled Lucide actions/tooltips. The local button component follows shadcn's source-owned CVA pattern. Registry aliases and theme tokens are configured.

Real mode supports listing/detail, preset loading, preflight and multipart creation via `/runs/rich`. Warnings require explicit continuation. Complete stage-output review and approval use the existing dashboard; the new preview only performs approval/rejection in demo mode. Advanced memory controls and configuration screens remain in the old dashboard. Live database/provider integration has not been tested; browser API checks use controlled responses. No generation runs were submitted. Demo data lives in memory and resets on reload.

Image references are copies of existing repository assets in `other/`; demo product labels and execution states are illustrative. The three demo reference thumbnails are not generated variants.

Version 3 restores green, adds the free Aceternity Glowing Effect plus restrained Motion transitions, and implements `#/shops`. Shop settings support create/edit, per-shop account bindings, enable/disable, archive/restore and global environment mappings. Supported account platforms: Shopify, Meta, TikTok and Notion. Only `CRISPY_API_KEY_*` variable names are saved; secret values remain in the backend process environment. Readiness is local configuration completeness, not remote authentication. Credential editing stays on independent pages. Agent management is deferred to the future Magpie plugin phase.

Shop writes use the existing FastAPI routes; there are no backend schema or secret-storage changes. Live browser checks use controlled API responses; real platform credentials are never modified by QA. Existing shop/configuration Python tests pass (28 tests).
