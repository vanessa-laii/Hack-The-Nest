# Phase 0/1 — run backend + web together

Proof that the web app talks to the backend gallery + story APIs. Prefer **live builds** (`EM_MOCK=0`). Mock mode remains for contract/sample checks only.

**Story schema:** `1.3` (regenerate with `npm run types`). Includes `changes[].scope` (`place` | `local`) + `changes[].area_ha`, plus earlier metric updates:
- **1.1** `built_frac` excludes water (MNDWI ≤ 0)
- **1.2** `ndvi_mean` averaged over land only (MNDWI ≤ 0)

## Backend (port 8000)

```powershell
cd backend
# once: py -3.12 -m venv .venv ; .venv\Scripts\python -m pip install -r requirements.txt
$env:EM_MOCK=0; .venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Check: `curl http://localhost:8000/api/health`  
Gallery: `curl http://localhost:8000/api/stories`  
Story (once built): `curl http://localhost:8000/api/stories/lake-mead-nevada-us-6km`

**Mock fallback:** `$env:EM_MOCK=1` still serves `contracts/samples/story.sample.json` for backend tests. The web globe seeds real catalog ids and does not promote the sample as the demo path.

## Web (port 3000)

```powershell
cd web
# .env.local should have NEXT_PUBLIC_API_BASE=http://localhost:8000
npm run types   # after pulling contract bumps
npm run dev
```

Open:

- http://localhost:3000 — globe; seed places (Lake Mead, Paradise, Summerlin, Palm Jumeirah, Central Park, Ypsilanti, Willow Run, Delray)
- Play film when a place is in the gallery; otherwise the card says the film isn’t ready yet
- Search a place → `createStory` → `/build/<story-id>` (SSE) → `/story/<story-id>`

Frame URLs look like `http://localhost:8000/files/stories/<id>/frames/...` (or sample paths under mock).

## SSE note

`ProgressFeed` **closes** the `EventSource` on `done` or `error` (and on unmount) so the browser does not keep reconnecting.
