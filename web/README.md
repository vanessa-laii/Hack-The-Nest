# Revisit (web)

Next.js App Router UI for Landsat change films. Vanessa owns this folder.

## Phase 0 — mock backend + web

See [docs/PHASE0.md](./docs/PHASE0.md) for the full runbook.

```powershell
# Terminal 1 — mock API (sample-lake-6km)
cd backend
$env:EM_MOCK=1; .venv\Scripts\python -m uvicorn app.main:app --port 8000

# Terminal 2 — web
cd web
npm run dev
```

Open http://localhost:3000/story/sample-lake-6km

`NEXT_PUBLIC_API_BASE` defaults to `http://localhost:8000` (see `.env.local.example`).

## Dev only

```bash
npm run dev
```
