# Revisit backend

FastAPI app implementing `docs/CONTRACTS.md`. Python **3.12** (geo wheels aren't reliable on 3.13+ yet).

## Run (mock mode: serves the contract sample story and replays sample build events)

```bash
cd backend
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # macOS/Linux: .venv/bin/python
EM_MOCK=1 .venv/Scripts/python -m uvicorn app.main:app --port 8000
```

PowerShell: `$env:EM_MOCK=1; .venv\Scripts\python -m uvicorn app.main:app --port 8000`. Or set `EM_MOCK=1` in the repo-root `.env`.

In mock mode:

- `POST /api/stories` (any query) → `202`, id `sample-lake-6km`.
- `GET /api/stories/sample-lake-6km/events` replays `contracts/samples/events.sample.jsonl` about 1 s apart and supports `Last-Event-ID`.
- `GET /api/stories/sample-lake-6km` returns `contracts/samples/story.sample.json`.
- `GET /api/stories` returns a one-item gallery.
- `/files/samples/...` serves generated placeholder frames, labelled "SAMPLE … NOT REAL".

## Validate a story

```bash
.venv/Scripts/python scripts/validate_story.py path/to/story.json     # no args = the contract sample
```

Checks the JSON schema plus the CONTRACTS §2 invariants (years aligned, ids resolve, cause_unknown ⇔ no claims, 150–225 words, 1–5 change chapters).
