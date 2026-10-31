# Crispy frontend preview

```powershell
cd D:\CODING\crispy\frontend
npm install
npm run dev -- --port 5174
```

Open http://127.0.0.1:5174. The default is explicitly labelled demo mode. Choose 真实接口 to connect through Vite's `/api` proxy to FastAPI at http://127.0.0.1:8000.

`npm run build` checks strict TypeScript and produces `dist/`. This preview runs independently of the existing dashboard; deployment integration is a later migration layer.

Implemented: task list, status/mode filters, search, task detail, stage status, product reference preview, product material grid, creation drawer, stage approval/rejection, responsive navigation, labelled Lucide icon actions and Radix focus-managed dialogs/tooltips. The local button component follows shadcn's source-owned CVA pattern. Registry aliases and theme tokens are configured.

Real mode supports listing/detail, preflight and basic JSON task creation, and stage actions. Full generated-asset review, uploads, memory controls, preset selection and configuration screens remain in the existing dashboard. Research, scheduling, data and settings link there explicitly. Live API integration has not been tested against the user's database or generation providers; browser API checks use controlled responses. No generation runs were submitted.

Image references are copies of existing repository assets in `other/`; demo product labels and execution states are illustrative. The three demo reference thumbnails are not generated variants.
