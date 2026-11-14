# Earth Memory: Contracts

The contracts are the only things `backend/` and `web/` share. If it isn't here or in `contracts/`, neither side may depend on it.

**Rules**

1. `contracts/*.schema.json` is the source of truth; this page explains it.
2. **Changing a contract:** message the other person, agree, bump `schema_version` (minor for additions, major for breaking), commit to `contracts/`, both regenerate types. AI tools must never change `contracts/` on their own.
3. Both sides validate against the schema: the backend before saving every story (`scripts/validate_story.py`), the web app in development (`lib/validate.ts`, dev only).
4. Everything in `contracts/samples/` is **made-up data** for building and testing. It's flagged with `"is_sample": true`, and sample text is marked `[SAMPLE]`.

---

## 1. HTTP API (backend, `http://localhost:8000`)

All JSON. CORS allows only `WEB_ORIGIN`. Errors use one envelope (§5).

| Method | Path | Request | Success | Errors |
| --- | --- | --- | --- | --- |
| GET | `/api/health` | — | `200 {"ok": true, "mock": false, "version": "1.0"}` | — |
| POST | `/api/stories` | `{"query": "Ypsilanti, Michigan"}` or, after the picker, `{"query": "...", "lat": 42.24, "lon": -83.61, "display_name": "Ypsilanti, Michigan"}`. Optional `"aoi_km"` (3–10) | **Cached:** `200 {"id", "cached": true, "story_url"}`. **New:** `202 {"id", "cached": false, "events_url", "story_url"}` | `400 INVALID_REQUEST`, `404 PLACE_NOT_FOUND`, `409 PLACE_AMBIGUOUS` (with `candidates`), `422 NO_LAND_DATA` |
| GET | `/api/stories/{id}/events` | Header `Last-Event-ID` to resume | `text/event-stream` of [BuildEvent](#3-build-events-sse) | `404` if unknown id |
| GET | `/api/stories/{id}` | — | `200` [`story.json`](#2-storyjson) | `404`; `425 NOT_READY` while building |
| GET | `/api/stories` | — | `200 {"stories": [GalleryItem]}` newest first | — |
| GET | `/files/...` | — | Static files from `DATA_DIR` (frames, thumbs, masks, audio) | `404` |

**GalleryItem**

```json
{
  "id": "lake-mead-nv-us-6km",
  "display_name": "Lake Mead, Nevada",
  "first_year": 1984,
  "last_year": 2026,
  "thumb_url": "/files/stories/lake-mead-nv-us-6km/thumbs/2026.jpg",
  "headline": "The shoreline retreats",
  "chapter_count": 4,
  "has_narration": true
}
```

**Web app (Next.js) route, server-side only**

| Method | Path | Request | Success | Notes |
| --- | --- | --- | --- | --- |
| POST | `/api/narrate` | `{"story_id": "..."}` | `200` [`narration.json`](#4-narrationjson) | Reads the story from the backend, calls ElevenLabs once per segment, writes audio + `narration.json` into `DATA_DIR/stories/<id>/narration/`. If `narration.json` exists, returns it without calling ElevenLabs. Rejects totals > 95 s (HL-1). |

## 2. `story.json`

Schema: [`contracts/story.schema.json`](../contracts/story.schema.json). Sample: [`contracts/samples/story.sample.json`](../contracts/samples/story.sample.json).

| Field | What it holds | Who reads it |
| --- | --- | --- |
| `schema_version`, `id`, `status`, `created_at`, `is_sample` | Identity. `status: "partial"` stories are playable but show a "partial" note | Gallery, player |
| `place` | Query, display name, point, `bbox` [W, S, E, N], `aoi_km`, admin names | Player header, agent |
| `coverage` | First/last year, season months, skipped years with reasons | Player "About this data", chart gaps |
| `frames[]` | One per kept year: image + thumb URLs, sensors, scene ids, date range, clear % | Film, evidence panel |
| `metrics.series[]` | Per year: `ndvi_mean`, `water_frac`, `built_frac` | Chart, script numbers |
| `changes[]` | Up to 5: kind, metric, years, magnitude, region bbox + description, before/after years, mask, sensor-switch flag | Chapters, evidence panel, zoom target |
| `sources[]` | Only sources with ≥ 1 **verified** excerpt; authority class | Evidence panel |
| `chapters[]` | Intro, 1–5 change chapters, outro. Claims point to excerpt ids. Confidence + vision check | Timeline markers, evidence panel |
| `script` | 150–225 words in segments; each segment has `chapter_id`, `year_from`, `year_to`, `claim_ids` | Narration, film sync |
| `validation` | Optional comparison against a published record | "How we checked" note |
| `credits` | Required credit lines (HL-11) | Footer |
| `generation` | Duration, API usage counts, warnings | Debug, judges |

**ID formats**: story `^[a-z0-9-]{1,64}$` built as `<place-slug>-<country>-<aoi>km`; change `chg-01`; source `src-01`; excerpt `ex-<source>-<n>` (e.g. `ex-01-02`); claim `cl-01`; chapter `ch-01`; segment `seg-01`. IDs are stable within a story.

**Invariants the backend guarantees** (validated before saving):

- `frames` and `metrics.series` cover the same years, ascending.
- Every `claims[].excerpt_ids` entry exists in `sources[].excerpts`.
- Every `script.segments[].claim_ids` entry exists in `chapters[].claims`.
- `chapters[].change_id` is set exactly when `kind == "change"`.
- `confidence == "cause_unknown"` ⇔ that chapter has no claims.
- `script.word_count` is 150–225 and equals the real word count of all segment texts.

**Playback rule (web)**: play segments in order. During a segment, the film animates frames from `year_from` to `year_to`, spread evenly across the segment's audio duration, pausing on `after_year` at the end of a change chapter. Captions highlight words from `narration.json`.

## 3. Build events (SSE)

Schema: [`contracts/event.schema.json`](../contracts/event.schema.json). Sample stream: [`contracts/samples/events.sample.jsonl`](../contracts/samples/events.sample.jsonl).

Wire format:

```text
id: 12
event: frame
data: {"type":"frame","story_id":"...","seq":12,"ts":"...","stage":"composite","message":"Built 2008","progress":0.42,"frame":{"year":2008,"thumb_url":"/files/...","clear_pct":92}}

```

| `type` | When | Extra fields |
| --- | --- | --- |
| `progress` | Stage started / running / done / warning | `stage`, `status`, `progress` |
| `frame` | A yearly frame is ready | `frame {year, thumb_url, clear_pct}` |
| `change` | A change was detected | `change {change_id, kind, start_year, end_year, region_description}` |
| `done` | Story saved | `story_url` |
| `error` | Fatal failure | `error {code, message, partial_story_url}` |

Stages in order: `geocode → scenes → composite → metrics → changes → research → verify → script → save`.

The backend also sends an SSE comment (`: ping`) every 10 s to keep the connection open. The stream closes after `done` or `error`.

**Mock mode**: with `EM_MOCK=1`, the backend replays `events.sample.jsonl` with ~1 s between events for any POST, and serves `story.sample.json` plus generated placeholder images at `/files/samples/...`. The web app is built entirely against mock mode until Phase 1 is done.

## 4. `narration.json`

Schema: [`contracts/narration.schema.json`](../contracts/narration.schema.json). Written by the web app's `/api/narrate`.

```json
{
  "schema_version": "1.0",
  "story_id": "lake-mead-nv-us-6km",
  "voice_id": "<from ELEVENLABS_VOICE_ID>",
  "model_id": "<from ELEVENLABS_MODEL_ID>",
  "created_at": "2026-11-15T01:10:00Z",
  "total_duration_s": 71.4,
  "segments": [
    {
      "segment_id": "seg-01",
      "audio_url": "/files/stories/lake-mead-nv-us-6km/narration/seg-01.mp3",
      "duration_s": 17.2,
      "words": [ { "text": "In", "start_s": 0.0, "end_s": 0.12 } ]
    }
  ]
}
```

Word timings are built from ElevenLabs' character-level alignment (text-to-speech "with timestamps"), grouped into words, relative to the segment start.

## 5. Errors

Every non-2xx response:

```json
{ "error": { "code": "PLACE_AMBIGUOUS", "message": "Several places match \"Paris\".", "candidates": [
  { "display_name": "Paris, France", "lat": 48.8566, "lon": 2.3522, "kind": "city" },
  { "display_name": "Paris, Texas, United States", "lat": 33.6609, "lon": -95.5555, "kind": "city" }
] } }
```

| Code | HTTP | Meaning | Web shows |
| --- | --- | --- | --- |
| `INVALID_REQUEST` | 400 | Bad body | "Something went wrong with that request." |
| `PLACE_NOT_FOUND` | 404 | Geocoder found nothing | "We couldn't find that place. Try adding the state or country." |
| `PLACE_AMBIGUOUS` | 409 | Several matches | Picker with `candidates` (max 5) |
| `NO_LAND_DATA` | 422 | Open water / no Landsat land coverage | "Earth Memory works on land. Try a nearby town." |
| `NOT_READY` | 425 | Story still building | Keep listening to events |
| `TIMEOUT` | event | Over 180 s | Offer `partial_story_url` if set, else the nearest cached story |
| `UPSTREAM_ERROR` | 502 / event | A provider failed after retry | Same as timeout |
| `INTERNAL` | 500 / event | Bug | Same as timeout |

## 6. Files on disk

`DATA_DIR` (default `./data` at the repo root, git-ignored) is shared by both apps on the demo laptop. The backend serves it at `/files/`.

```text
data/
├── stories/
│   ├── index.json                    # gallery list (backend writes)
│   └── <story-id>/
│       ├── story.json                # backend writes, schema-validated
│       ├── frames/<year>.jpg         # 1024 x 1024
│       ├── thumbs/<year>.jpg         # 256 x 256
│       ├── masks/<change-id>.png     # optional
│       ├── trace.jsonl               # every LLM/search call (TR-10)
│       └── narration/                # web writes ONLY here
│           ├── narration.json
│           └── seg-01.mp3 ...
├── samples/                          # mock mode placeholders (backend generates)
└── cache/                            # geocode + STAC search caches (backend)
```

Ownership: the backend never writes inside `narration/`; the web app never writes anywhere else.

## 7. Environment variables

Listed in `.env.example`. Each app reads only its own.

| Variable | Used by | Example / default | Notes |
| --- | --- | --- | --- |
| `DATA_DIR` | both | `../data` | Same folder for both on the laptop |
| `EM_MOCK` | backend | `0` | `1` = mock mode |
| `AOI_KM` | backend | `6` | 3–10, final default from spike S2 |
| `PIPELINE_TIMEOUT_S` | backend | `180` | HL-5 |
| `WEB_ORIGIN` | backend | `http://localhost:3000` | CORS |
| `NOMINATIM_USER_AGENT` | backend | `earth-memory-hackthenest/1.0 (contact email)` | Required by Nominatim policy |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | backend | — | Model pinned after spike S4 |
| `EXA_API_KEY` | backend | — | |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | backend | — | Vision check |
| `MONGODB_URI` | backend | empty = disabled | Optional mirror |
| `ELEVENLABS_API_KEY` | web (server only) | — | Never `NEXT_PUBLIC_` |
| `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL_ID` | web (server only) | — | One fixed voice (FR-45) |
| `NEXT_PUBLIC_API_BASE` | web | `http://localhost:8000` | Prefix for `/api` and `/files` URLs |

## 8. Generating types

- **Web:** `npx json-schema-to-typescript contracts/story.schema.json -o web/lib/types/story.ts` (same for `narration` and `event`). Add as `npm run types`.
- **Backend:** `datamodel-codegen --input contracts/story.schema.json --input-file-type jsonschema --output backend/app/contract_models.py --output-model-type pydantic_v2.BaseModel`, or hand-write Pydantic models and test them against the sample with `jsonschema`.
- Never edit generated files by hand; regenerate.
