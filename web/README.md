# Revisit (web)

Next.js App Router UI for Landsat change films. Vanessa owns this folder.

## Phase 1 — live build API + gallery seeds

See [docs/PHASE0.md](./docs/PHASE0.md) for the runbook (updated for schema 1.3 + live builds).

```powershell
# Terminal 1 — backend (prefer live builds)
cd backend
$env:EM_MOCK=0; .venv\Scripts\python -m uvicorn app.main:app --port 8000

# Terminal 2 — web
cd web
npm run types   # after contract bumps
npm run dev
```

Open http://localhost:3000 — globe shows real seed places (Lake Mead, Paradise, Willow Run, …). Films open once `GET /api/stories` lists their ids (e.g. `lake-mead-nevada-us-6km`).

`NEXT_PUBLIC_API_BASE` defaults to `http://localhost:8000` (see `.env.local.example`).

`EM_MOCK=1` still serves `contracts/samples/story.sample.json` for backend wiring checks; the product UI no longer deep-links to that sample.

## Dev only

```bash
npm run dev
```
