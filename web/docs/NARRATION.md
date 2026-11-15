# Revisit: Narration Design (S5)

Design only — how `story.json` becomes cached, word-timed film audio.  
Owner: Vanessa (`web/`). Do not edit `contracts/`. No API keys in code.

**Status:** implemented — `web/app/api/narrate/route.ts`, `web/lib/narration/*`, mock under `data/stories/mock-narrate-demo/`.  
**Smoke test:** `npm run narrate:mock` (with `next dev`). FilmPlayer / captions wiring still later.

---

## 0. Confirmed ownership & flow

| Who | Writes | Reads |
| --- | --- | --- |
| Backend (Harman) | `DATA_DIR/stories/<id>/story.json`, frames, thumbs, masks, `index.json` | — |
| Web (Vanessa) | **Only** `DATA_DIR/stories/<id>/narration/` (`narration.json` + `seg-XX.mp3`) | story via `GET {API}/api/stories/{id}`; audio served by backend `GET /files/...` |

```text
Client (FilmPlayer / story page)
  │  POST /api/narrate  { "story_id": "..." }     ← Next.js, server-only
  ▼
app/api/narrate/route.ts
  │  1. If DATA_DIR/stories/<id>/narration/narration.json exists → return it
  │  2. Else GET NEXT_PUBLIC_API_BASE/api/stories/{id} → story.json
  │  3. For each script.segments[] → ElevenLabs with-timestamps (1 call / segment)
  │  4. Char alignment → words[{text,start_s,end_s}] (relative to segment start)
  │  5. Write seg-XX.mp3 + narration.json
  │  6. If total_duration_s > 95 → reject (HL-1); do not leave a bad cache hit
  ▼
Player plays segments in order; captions from words; frames year_from→year_to
synced evenly across each segment's audio duration (CONTRACTS §2 playback rule).
```

**Env (server-only, from `web/.env.local.example`):**

- `ELEVENLABS_API_KEY` — never `NEXT_PUBLIC_`
- `ELEVENLABS_VOICE_ID` — one fixed voice (FR-45)
- `ELEVENLABS_MODEL_ID` — see §2
- `DATA_DIR` — shared with backend (default `../data` from `web/`)
- `NEXT_PUBLIC_API_BASE` — prefix for `/api` and `/files` only

---

## 1. Exact ElevenLabs endpoint(s)

### Primary (use this)

**POST** `https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps`

Docs: [Create speech with timing](https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps)

| | |
| --- | --- |
| Auth | Header `xi-api-key: <ELEVENLABS_API_KEY>` |
| Content-Type | `application/json` |
| Query (optional) | `output_format=mp3_44100_128` (default; matches `.mp3` on disk) |

**Request body (minimal + recommended):**

```json
{
  "text": "<script.segments[i].text>",
  "model_id": "<ELEVENLABS_MODEL_ID>",
  "voice_settings": {
    "stability": 0.75,
    "similarity_boost": 0.7,
    "style": 0.0,
    "use_speaker_boost": true,
    "speed": 1.0
  },
  "previous_text": "<optional: prior segment text for continuity>",
  "next_text": "<optional: following segment text>",
  "apply_text_normalization": "auto"
}
```

**200 response shape:**

```json
{
  "audio_base64": "<mp3 bytes as base64>",
  "alignment": {
    "characters": ["H", "e", "l", "l", "o", " ", "…"],
    "character_start_times_seconds": [0.0, 0.05, …],
    "character_end_times_seconds": [0.05, 0.10, …]
  },
  "normalized_alignment": { "characters": [], "character_start_times_seconds": [], "character_end_times_seconds": [] }
}
```

- Decode `audio_base64` → write `seg-XX.mp3`.
- Build captions from **`alignment`** (maps to the **original** script text the UI shows), not `normalized_alignment` (spoken expansion of numbers/abbreviations). Our scripts already spell years in words (“nineteen eighty-four”), so both usually align; still prefer `alignment` so caption `text` matches `story.script.segments[].text`.

### Do not use for v1

| Endpoint | Why not |
| --- | --- |
| `/v1/text-to-speech/{voice_id}` | Audio only — no timestamps |
| `/v1/text-to-speech/{voice_id}/stream/with-timestamps` | Streaming JSON chunks; harder to cache atomically; no benefit for offline gallery |
| WebSocket / Agents | Overkill; secrets risk if miswired |

Optional later: `GET /v1/voices` once locally to pick `ELEVENLABS_VOICE_ID` — never hardcode keys; voice id in env is fine.

---

## 2. Recommended model_id & voice settings (FR-45)

| Setting | Recommendation | Why |
| --- | --- | --- |
| `model_id` | **`eleven_multilingual_v2`** | Default on the with-timestamps endpoint; ElevenLabs documents it as the **most stable** model for long-form / consistent delivery. Good fit for 60–90 s documentary. Put this in `ELEVENLABS_MODEL_ID`. |
| Alternatives | `eleven_v3` / newer `eleven_v4` | More expressive; try only if multilingual_v2 sounds dull — re-cache all stories after a model change. |
| Avoid for film | Flash / Turbo models | Optimized for low latency agents, not calm documentary VO. |

**Voice (FR-45 — one fixed voice for the product):**

1. In ElevenLabs UI or `GET /v1/voices`, pick **one** calm, mid-pace documentary voice (examples people use for docs: “PapaLemus”-style IDs, or a premade narrative voice you like).
2. Store only the id in `ELEVENLABS_VOICE_ID`. Never rotate per story.
3. Freeze voice + model before pre-building the 6–8 gallery stories so offline demo is consistent.

**`voice_settings` for documentary steadiness:**

| Field | Value | Notes |
| --- | --- | --- |
| `stability` | **0.70–0.80** | Higher = less emotional swing; good for narration |
| `similarity_boost` | **0.65–0.75** | Stay close to the chosen voice without amplifying clone noise |
| `style` | **0** | Style exaggeration adds instability; leave off |
| `speed` | **0.95–1.0** | Slightly slow helps hit 60–90 s from 150–225 words; tune once against sample |
| `use_speaker_boost` | `true` | Clarity |

Estimate: ~150–180 wpm → 164-word sample ≈ 55–65 s spoken; sample `est_duration_s: 66` is in band. If TTS runs hot and exceeds 95 s, lower `speed` slightly **or** ask Harman to shorten scripts (backend HL-1 word gate) — do not reword claims in the web app.

---

## 3. Character → word alignment (algorithm sketch)

Input: ElevenLabs `alignment` parallel arrays.  
Output: `words: { text, start_s, end_s }[]` with times **relative to segment start** (already true for a single-segment call).

```text
function charactersToWords(alignment):
  chars  = alignment.characters
  starts = alignment.character_start_times_seconds
  ends   = alignment.character_end_times_seconds
  words  = []
  buf = ""
  wStart = null

  for i in 0 .. chars.length-1:
    ch = chars[i]
    if ch is whitespace (or newline):
      if buf non-empty:
        words.push({ text: buf, start_s: wStart, end_s: ends[i-1] })
        buf = ""; wStart = null
      continue
    if wStart is null: wStart = starts[i]
    buf += ch

  if buf non-empty:
    words.push({ text: buf, start_s: wStart, end_s: ends[last] })

  return words
```

**Details to implement carefully:**

1. **Whitespace-only splits** — keep punctuation attached to the word (`shoreline,` / `lake.`) so caption highlighting matches visible tokens; strip trailing punctuation only if Captions UI needs bare words later.
2. **Empty / missing alignment** — treat as `UPSTREAM_ERROR`; do not invent timings by evenly dividing duration.
3. **`duration_s`** — prefer `max(ends)` from alignment; fallback: decode MP3 duration if alignment empty (shouldn’t happen on success).
4. **Sanity** — every word: `0 ≤ start_s ≤ end_s ≤ duration_s + ε` (ε ≈ 0.05 for float noise).
5. **Do not re-tokenize** the story text for display; captions highlight the spoken words, film still shows exact `story.json` claims elsewhere (TR / “never change what the story says”).

Pseudo TypeScript (for a future `web/lib/narration/align.ts` — not written yet):

```ts
type CharAlignment = {
  characters: string[];
  character_start_times_seconds: number[];
  character_end_times_seconds: number[];
};

type WordTiming = { text: string; start_s: number; end_s: number };

function charactersToWords(a: CharAlignment): WordTiming[] {
  // as above
}
```

---

## 4. Error handling

Map failures to the shared error envelope (`CONTRACTS` §5). Narrate-specific codes can ride in `message` until we agree new codes with Harman.

| Condition | HTTP | `error.code` | Behavior |
| --- | --- | --- | --- |
| Missing / invalid `story_id` | 400 | `INVALID_REQUEST` | Friendly: “Something went wrong with that request.” |
| Story not found | 404 | (pass through / `PLACE_NOT_FOUND` style) | Offer gallery |
| Story still building | 425 | `NOT_READY` | Keep listening to SSE; don’t call TTS |
| `narration.json` cache hit | 200 | — | **No ElevenLabs call** |
| Segment count **> 5** | 422 or 502 | see §5 / HL-10 | Refuse or coalesce — decide before Phase 2 ship (open Q) |
| ElevenLabs timeout / 5xx / network | 502 | `UPSTREAM_ERROR` | Retry once with backoff; then fail soft. **Do not** write partial `narration.json` |
| ElevenLabs 401/403 | 502 | `UPSTREAM_ERROR` | Log server-side only (never leak key). Message: voice service unavailable |
| `total_duration_s > 95` | 422 | e.g. `NARRATION_TOO_LONG` (propose) | Delete any temp mp3s written this attempt; **do not** cache. UI: “This narration ran long — try another story.” |
| `total_duration_s < 55` (schema min) | 422 | validate fail | Unlikely with 150+ words; log + don’t cache |
| Disk write failure | 500 | `INTERNAL` | No cache file |
| Missing env (`ELEVENLABS_*`, `DATA_DIR`) | 500 | `INTERNAL` | Fail at route start |

**Atomic cache rule:** write `seg-XX.mp3` files first (or to `*.tmp` then rename), write `narration.json` **last**. Cache hit = presence of valid `narration.json` only. On reject (>95 s) or mid-flight error, remove incomplete files for that story’s narration dir (or leave orphans without `narration.json` so next request retries).

**Timeouts:** set fetch timeout ~30–45 s **per** TTS call (4 segments ≈ ≤3 min worst case). Player should show a “Preparing narration…” state; never blank screen.

**HL-10 (≤ 5 TTS calls):** count each successful ElevenLabs with-timestamps request toward the story. Cache hits cost **0**. Sample story has **4** segments → fits. See §5 for >5 segments.

---

## 5. Spike S5 recommendation: per-segment vs one-shot

| Approach | Pros | Cons |
| --- | --- | --- |
| **A. One TTS call per `script.segments[]`** (default) | Matches CONTRACTS §1; word times naturally segment-relative; player can swap audio per chapter; failed segment retryable; `previous_text` / `next_text` stitch prosody | N API calls; if chapters grow to 7, exceeds HL-10’s 5 |
| **B. One-shot full script + alignment** | 1 TTS call (HL-10 safe); continuous prosody | Must split audio + shift word times by cumulative offsets; harder retry; chapter boundaries less clean; one failure regenerates everything |

**Recommendation: ship Approach A (per-segment)** unless a side-by-side listen on the sample shows A is clearly choppier at boundaries.

Mitigations for A:

- Pass `previous_text` / `next_text` (segment neighbors) for continuity — free quality win, still one call per segment.
- Keep segment count ≤ 5 for demo stories (intro + ≤3 changes + outro is the sweet spot).
- If a future story has 6–7 segments: either (1) one-shot fallback for that story only, or (2) ask Harman to merge intro/outro into adjacent change segments in `script.py`. **Do not silently drop chapters.**

**S5 pass bar:** captions stay within ~100–150 ms of audio on sample; total duration in 60–90 s (reject >95); voice sounds continuous enough across seg boundaries with `previous_text`/`next_text`.

Optional tiny spike (only if needed before implementing the route): `web/spikes/s5-listen/` — generate A vs B once with a personal key, listen, write `results.json`. Not required to start the route if we trust the contract default.

---

## 6. Story → files on disk

Product films use real catalog ids (e.g. `lake-mead-nevada-us-6km`). For narration pipeline checks against the contract sample, input may still be [`contracts/samples/story.sample.json`](../../contracts/samples/story.sample.json) under `EM_MOCK=1` — that path is **not** linked from the product UI.

After a successful `POST /api/narrate` with `{ "story_id": "<story_id>" }`:

```text
DATA_DIR/
└── stories/
    └── <story_id>/
        └── narration/                    ← web writes ONLY here
            ├── narration.json
            ├── seg-01.mp3
            ├── seg-02.mp3
            └── …
```

**Example `narration.json` (illustrative timings):**

```json
{
  "schema_version": "1.0",
  "story_id": "lake-mead-nevada-us-6km",
  "voice_id": "<from ELEVENLABS_VOICE_ID>",
  "model_id": "eleven_multilingual_v2",
  "created_at": "2026-11-15T02:00:00Z",
  "total_duration_s": 68.2,
  "segments": [
    {
      "segment_id": "seg-01",
      "audio_url": "/files/stories/lake-mead-nevada-us-6km/narration/seg-01.mp3",
      "duration_s": 18.4,
      "words": [
        { "text": "This", "start_s": 0.05, "end_s": 0.22 },
        { "text": "is", "start_s": 0.23, "end_s": 0.35 }
      ]
    }
  ]
}
```

Player loads audio as `NEXT_PUBLIC_API_BASE + audio_url` (backend static `/files`).  
Gallery `has_narration` is already computed by backend as “does `narration/narration.json` exist?” — no contract change needed.

**Phase 1:** prefer live stories (`EM_MOCK=0`). Narration always lands under `DATA_DIR/stories/<story_id>/narration/` so offline HL-9 works the same for every id.

---

## 7. Minimal TypeScript interface (matches `narration.schema.json`)

Generated file already exists: `web/lib/types/narration.ts` (`npm run types`). Prefer that in app code. Hand-maintained minimal form for design clarity:

```ts
/** contracts/narration.schema.json — schema_version 1.0 */
export interface NarrationWord {
  text: string;
  start_s: number; // ≥ 0, relative to segment audio start
  end_s: number;   // ≥ start_s
}

export interface NarrationSegment {
  segment_id: string; // /^seg-[0-9]{2}$/
  audio_url: string;  // /^\/files\//
  duration_s: number;
  words: NarrationWord[];
}

export interface Narration {
  schema_version: "1.0";
  story_id: string;
  voice_id: string;
  model_id: string;
  created_at: string; // ISO-8601
  total_duration_s: number; // schema: 55..95; HL-1 reject if > 95
  segments: NarrationSegment[]; // min 3; same order/ids as story.script.segments
}
```

**Invariants the route must enforce before write:**

1. `segments.length === story.script.segments.length`
2. `segments[i].segment_id === story.script.segments[i].id`
3. `total_duration_s === sum(duration_s)` (within float epsilon)
4. `55 ≤ total_duration_s ≤ 95` (reject > 95 hard)
5. Every `audio_url` is `/files/stories/<story_id>/narration/seg-XX.mp3`

---

## 8. Integration checklist (real Gemini / OpenAI stories)

Same schema — narration must not break when mock → real:

- [ ] Load story **only** via `GET /api/stories/{id}` (or read `DATA_DIR/.../story.json` as fallback); never parse a different shape.
- [ ] Use `script.segments[].id` / `.text` / `.year_from` / `.year_to` only — do not depend on sample-only fields or `[SAMPLE]` markers.
- [ ] Segment count can be **3–7** (intro + 1–5 changes + outro). Cache path stays `stories/<id>/narration/`.
- [ ] `claim_ids` / evidence are **player** concerns; narrate route does not rewrite text (TR-6 is backend).
- [ ] `status: "partial"` stories may still narrate if `script.segments` is present and valid; show partial banner in UI, don’t invent missing segments.
- [ ] After voice/model env change, **delete** old `narration/` dirs (or bump a cache key) so stale audio isn’t served.
- [ ] Offline demo (HL-9): pre-call `/api/narrate` for each gallery id so mp3 + json exist under `DATA_DIR` before Wi-Fi-off.
- [ ] Prefix all `/files/...` with `NEXT_PUBLIC_API_BASE` in the client; narration `audio_url` stays root-relative as in schema.
- [ ] Never put `ELEVENLABS_API_KEY` in client bundles; only `app/api/narrate/route.ts` (and server libs it imports).
- [ ] Regenerate types with `npm run types` if contracts change — never hand-edit `lib/types/narration.ts`.
- [ ] Film sync (later): for each playing segment, map `t ∈ [0, duration_s]` → frame year by lerping `year_from`→`year_to`; on change chapters, pause on `after_year` at segment end (CONTRACTS §2).

---

## 9. Open questions for Vanessa

1. **Voice pick:** Which exact `ELEVENLABS_VOICE_ID` do we freeze for the demo? (Listen to 2–3 candidates on sample seg-02 before locking.)
2. **HL-10 vs 6–7 segments:** If a real story has intro + 5 changes + outro, do we (a) one-shot fallback, (b) ask Harman to cap/merge script segments ≤ 5, or (c) treat HL-10 as backend-agent-only and allow up to 7 web TTS calls for gallery builds?
3. **New error code:** Agree `NARRATION_TOO_LONG` (422) with Harman, or reuse `INVALID_REQUEST` / `UPSTREAM_ERROR`?
4. **Speed knob:** Fix `voice_settings.speed` at `1.0` or tune to `0.95` after measuring sample `total_duration_s`?
5. **Story load path:** Backend GET only, or also read `DATA_DIR/stories/<id>/story.json` when the API is down (stronger offline)?
6. **Caption punctuation:** Keep commas/periods on word `text`, or strip for cleaner karaoke UI?
7. **S5 listen test:** Do a 20-minute A/B spike under `web/spikes/`, or implement per-segment directly and only revisit if boundaries sound bad?
8. **Concurrent narrate:** If two clients hit `/api/narrate` for the same id, file-lock or “in progress” guard — needed for live venue, or only pre-cache?

---

## 10. Implementation order (when ready — not this doc)

1. `web/lib/narration/align.ts` — `charactersToWords` + duration helpers (unit-testable, no key).
2. `web/lib/narration/elevenlabs.ts` — server-only fetch wrapper (key from env).
3. `web/app/api/narrate/route.ts` — cache check → story fetch → loop segments → write → HL-1 gate.
4. Wire story page to call narrate once, then play; Captions consume `words`.
5. Pre-narrate gallery stories before offline check.

**Out of scope for this design:** FilmPlayer crossfade (S6), EvidencePanel, rewording any script text.
