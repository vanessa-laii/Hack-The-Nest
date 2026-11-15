# Phase 0 — run mock backend + web together

Proof that the web app loads `contracts/samples/story.sample.json` via the backend (`EM_MOCK=1`).

**Story schema:** `1.3` (regenerate with `npm run types`). Includes `changes[].scope` (`place` | `local`) + `changes[].area_ha`, plus earlier metric updates:
- **1.1** `built_frac` excludes water (MNDWI ≤ 0)
- **1.2** `ndvi_mean` averaged over land only (MNDWI ≤ 0)

## Backend (port 8000)

```powershell
cd backend
# once: py -3.12 -m venv .venv ; .venv\Scripts\python -m pip install -r requirements.txt
$env:EM_MOCK=1; .venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Check: `curl http://localhost:8000/api/health` → `"mock": true`  
Story: `curl http://localhost:8000/api/stories/sample-lake-6km`

**Important:** keep `EM_MOCK=1` for demos. With `EM_MOCK=0`, `POST /api/stories` returns **500** (“Live story generation isn't available yet”) until Harman’s live wiring lands.

## Web (port 3000)

```powershell
cd web
# .env.local should have NEXT_PUBLIC_API_BASE=http://localhost:8000
npm run types   # after pulling contract bumps
npm run dev
```

Open:

- http://localhost:3000 — globe; pick **Sample Lake** → Play film
- http://localhost:3000/story/sample-lake-6km — FilmPlayer + frames
- Search “anything” → `createStory` → `/build/sample-lake-6km` (SSE replay) → story

Frame URLs look like `http://localhost:8000/files/samples/thumbs/1984.jpg`.

## SSE note

`ProgressFeed` **closes** the `EventSource` on `done` or `error` (and on unmount) so the browser does not keep reconnecting.