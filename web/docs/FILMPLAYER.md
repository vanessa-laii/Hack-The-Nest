# FilmPlayer — clock model & how to test

Owner: Vanessa (`web/`). Implements CONTRACTS §2 playback + FR-45 / FR-52 / S6.

## Clock model

```text
narration.segments[].duration_s  →  film-global timeline
                                        │
globalT  ──►  globalToLocal()  ──►  { segmentIndex, localT }
                                        │
                   ┌────────────────────┴────────────────────┐
                   ▼                                         ▼
            HTMLAudioElement.currentTime              pickFrameYear()
            (source of truth while playing)           year_from→year_to
                   │                                  (+ after_year hold)
                   ▼                                         ▼
            Captions(words, localT)                   CSS crossfade stack
```

| Concept | Rule |
| --- | --- |
| Segment order | Play `narration.segments` in order; hand off on `ended` |
| Frame years | During a segment, keep frames with `year ∈ [year_from, year_to]`, spread evenly across `duration_s` |
| `after_year` hold | Change chapters: ~12% hold when the year path hits the change’s `after_year` (`AFTER_YEAR_HOLD_RATIO`); segment may continue past that through quiet years to `year_to` |
| Captions | Word timings are **segment-relative**; highlight from `narration.json` |
| Audio URLs | Same-origin `/files/stories/<id>/narration/seg-XX.mp3` (Next `app/files` → `DATA_DIR`) |
| Frames | `fileUrl(image_url)` → backend `/files/...` (mock samples under `data/samples/`) |
| HL-1 | Do not play if `total_duration_s > 95` |
| Prefetch | Next segment’s mp3 is loaded into a hidden `Audio` while the current plays |

Pure helpers live in `web/lib/playback/` (no React). `FilmPlayer` owns the audio element + rAF sync. `StoryFilm` calls `POST /api/narrate` (cache-first) then mounts the player.

### Stable `FilmPlayer` props

```ts
story: Story
narration: Narration
autoPlay?: boolean            // default true; gesture fallback if blocked
captionsDefaultOn?: boolean   // default true
onYearChange?(year)
onSegmentChange?(segmentId, index)
onPlayStateChange?(playing)
onEnded?()
```

Keyboard: **Space** play/pause · **← / →** previous / next discrete frame year.

## How to test

1. Backend: from `backend/`, prefer `EM_MOCK=0` + uvicorn on `:8000` (live gallery). Ensure a story exists (e.g. build Lake Mead so `lake-mead-nevada-us-6km` is in `GET /api/stories`).
2. Web: from `web/`, `npm run types` then `npm run dev` on `:3000`.
3. Open the globe, pick a **Film ready** place, or go to `/story/<story_id>` (e.g. `lake-mead-nevada-us-6km`).
4. Expect: “Preparing narration…” then film with audio, captions ON, big year label, scrubber.
5. If autoplay is blocked, tap **Play film**.
6. Confirm Space / ← / →; year should track segments (intro 1984→…, change holds `after_year`).
7. `npm run build` must pass.

Cached narration (after first generate):

`data/stories/<story_id>/narration/narration.json` + `seg-01.mp3` …

Re-generate: `POST /api/narrate` with `{ "story_id": "<story_id>" }` (needs `ELEVENLABS_*` only on cache miss).

For contract/sample wiring only, `EM_MOCK=1` still serves `contracts/samples/story.sample.json` — not a product deep link.

## Gaps (ok for this pass)

- MetricChart + EvidencePanel — shipped on `/story/[id]` below the film (`StoryBelowFold` forwards `onYearChange` into MetricChart). Timeline chapter markers: cut by agreement.
- BeforeAfter slider — not wired (evidence shows side-by-side before/after thumbs).
- Visual polish of chrome may still iterate; keep the props API above.
