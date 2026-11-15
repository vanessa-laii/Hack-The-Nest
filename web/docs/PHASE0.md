# Phase 0 — run mock backend + web together

Proof that the web app loads `contracts/samples/story.sample.json` via the backend (`EM_MOCK=1`).

## Backend (port 8000)

```powershell
cd backend
# once: py -3.12 -m venv .venv ; .venv\Scripts\python -m pip install -r requirements.txt
$env:EM_MOCK=1; .venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Check: `curl http://localhost:8000/api/health` → `"mock": true`  
Story: `curl http://localhost:8000/api/stories/sample-lake-6km`

## Web (port 3000)

```powershell
cd web
# .env.local should have NEXT_PUBLIC_API_BASE=http://localhost:8000
npm run dev
```

Open:

- http://localhost:3000 — globe; pick **Sample Lake** → Play film
- http://localhost:3000/story/sample-lake-6km — story proof UI (frames, chapters, script)
- Search “anything” → `createStory` → `/build/sample-lake-6km` (SSE replay) → story

Frame URLs look like `http://localhost:8000/files/samples/thumbs/1984.jpg`.
