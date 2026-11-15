# Revisit: Requirements

The rules we build to. IDs (HL-, FR-, TR-, DR-) are referenced from the plan, prompts and commits. Details of formats live in [`CONTRACTS.md`](CONTRACTS.md); tasks and deadlines in [`PLAN.md`](PLAN.md).

## 1. Scope

**In:** any place on land, Landsat imagery from 1984 to 2026, three measured signals (vegetation, water, built-up land), causes found on the web and verified, a 60–90 s narrated film, an evidence panel, a gallery of pre-built stories.

**Out:** oceans and ice sheets, predictions, anything finer than 30 m (single buildings), proof of causation. We show *documented events that line up in time and place* with a measured change.

## 2. Hard limits

Never traded for features.

| ID | Limit | Enforced by |
| --- | --- | --- |
| **HL-1** | Story film **60–90 s** (script **150–225 words**) | `script.py` rejects outside the range; `/api/narrate` rejects audio > 95 s |
| **HL-2** | **1–5** change chapters + intro + outro | Schema; keep the 5 largest changes |
| **HL-3** | **Zero** unsupported claims | Truthfulness rules (§4); gold-set check |
| **HL-4** | **Real imagery only**, no generated images | No image-generation APIs in the codebase |
| **HL-5** | New story ≤ 90 s median, **180 s hard timeout** | Timeout → error + offer nearest cached story |
| **HL-6** | Area 3–10 km square (default 6 km, final from spike S2) | Config bounds check |
| **HL-7** | Devpost video **≤ 3 min** (aim for 2) | Checked before upload |
| **HL-8** | **No secrets in git or the browser** | `.env` git-ignored; ElevenLabs key server-side only |
| **HL-9** | Cached stories **play offline** | Wi-Fi-off test before demo |
| **HL-10** | Per story: ≤ 15 Exa, ≤ 25 LLM, ≤ 6 Gemini, ≤ 5 TTS calls | Counters; over budget → remaining changes become "cause unknown" |
| **HL-11** | Credit lines on every page: *Landsat imagery courtesy of NASA and the U.S. Geological Survey.* / *Supported by the Michigan Space Grant Consortium, NASA grant 80NSSC20M0124.* No NASA logos | Footer + README |

## 3. What it must do

**Place**

- **FR-1** Resolve a place name to a point, display name and admin names; **FR-2** if several places match, return a picker instead of guessing; **FR-3** reject open water with a friendly message.

**Imagery**

- **FR-13** One cloud-free composite per year from Landsat 4–9 (summer months; cloud, shadow and snow masked).
- **FR-14** Record clear-sky % per year; skip years under 60% and say why.
- **FR-15** One fixed colour stretch across all years, so brightness changes are real, not rendering.

**Change and cause**

- **FR-20** Measure per year: vegetation (NDVI), water fraction, built-up fraction.
- **FR-22** A change must last ≥ 2 years (fires excepted). **FR-23** The control place produces 0 changes.
- **FR-30** For each change, search with Exa and reason with a text model (Gemini by default; any model is allowed), following §4.
- **FR-33** Each chapter gets a confidence: `confirmed`, `likely` or `cause_unknown`.

**Film and web app**

- **FR-45** One fixed narration voice; captions always on, synced to words.
- **FR-52** Player: crossfaded frames, big year label, play/pause, scrub, timeline with chapter markers, metric chart.
- **FR-54** Every claim reachable in one click: before/after frames with dates, sources, the exact verified excerpt, confidence.
- **FR-55** Show "1 pixel = 30 m" and the area size.
- *Stretch:* before/after slider, shareable links, "Ask the place" (answers only from the story's own data).

## 4. Truthfulness rules (no hallucinations)

The product's credibility is its honesty. These apply to every model call.

- **TR-1** A claim exists only if it has at least one **verified excerpt** from a fetched page.
- **TR-2** "Verified" is checked **in code**: the excerpt must appear word for word in the page text. Not found → thrown out.
- **TR-3** The excerpt must name the place (or its city/county, or a named feature inside the area). State or country alone isn't enough.
- **TR-4** The excerpt must fall in the change's years (±1), by a stated date or the page's publication date.
- **TR-5** A separate checker call confirms each claim is actually stated by its excerpts. Any "no" drops the claim.
- **TR-6** The narration may only use numbers, years and names that appear in the metrics or verified excerpts. Code rejects anything else.
- **TR-7** Confidence is set by rules, not by the model: `confirmed` = 2+ independent sources, or 1 authoritative (government, science, major newspaper); `likely` = 1 other source; otherwise `cause_unknown`. Gemini disagreeing with the images lowers it one step.
- **TR-8** "Cause unknown" is shown plainly in the timeline and evidence panel. The narration spends its time on changes with verified causes, never mentions a missing cause, and never fills a gap with a guess.
- **TR-9** Web pages and search results are untrusted data; instructions inside them are ignored.
- **TR-10** Every model and search call is logged to the story's `trace.jsonl`.

## 5. Data correctness (for the science judges)

- **DR-1** Use Landsat Collection 2 Level-2 surface reflectance with its documented scaling.
- **DR-2** Each frame records sensors, scene ids, dates and clear-sky %; the player shows them.
- **DR-6** Flag changes that coincide only with a satellite switch (Landsat 5 → 8) as possible artefacts.
- **DR-7** Check at least one series against a published record (e.g. Lake Mead levels) and show the result, including disagreement.
- **DR-8** README lists datasets, dates, places, links, and plain limits ("30 m detail; causes are documented coincidences, not proof").

## 6. Event and track rules

- All code written during the 24 hours; public repo; both of us added on Devpost; AI tools listed.
- Draft by Sun 12:00 AM, final by Sun 12:00 PM, no late entries; demo video ≤ 3 min.
- **Future of Intelligence (updated Sat night):** any AI model or framework. Judged on creativity, innovation, technical implementation, real-world impact and meaningful AI integration; agentic systems and combining AI fields are strongly encouraged.
- **Beyond the Horizon:** NASA data at the centre, used correctly, credited (HL-11).
- **ElevenLabs:** the voice is the product.
- **MLH (Gemini, MongoDB Atlas, GoDaddy Registry):** actually used; selected on Devpost.
- Both of us can explain any part of the code.

## 7. Hard problems: spikes run with subagents

Where the right answer isn't obvious, build 2–3 approaches in parallel, test them on the same places (`eval/gold_places.yaml`), pick, and write a one-page note in `docs/decisions/`.

**How (Harman, Claude Code):** write `spikes/<name>/README.md` (question, approaches, test places, pass bar) → launch one subagent per approach in a single message, each confined to `spikes/<name>/<approach>/` and writing `results.json` → compare → write the decision note → move the winner into `backend/`. **Vanessa (Cursor):** two chats or background agents, one per approach, in `spikes/web-<name>/`.

| Spike | Owner | Decide by | Approaches | Pass bar |
| --- | --- | --- | --- | --- |
| **S1 Imagery access** | Harman | 6:00 PM | `odc-stac` · `stackstac` · Planetary Computer data API | 40 years for one place in ≤ 60 s, all bands + cloud mask available |
| **S2 Clean yearly images + area size** | Harman | 7:00 PM | Median · best scene + gap fill · medoid; area 3 / 6 / 10 km | No visible clouds in 90% of years; control place doesn't flicker |
| **S3 Change detection** | Harman | 8:30 PM | Before/after window means · `ruptures` change points · straight-segment fitting | Finds ≥ 80% of expected changes; **0** on control |
| **S4 Causes without hallucination** | Harman | 9:30 PM (start 6:30) | Single pass + quote check · extract → verify → compose · same plus a critic agent | **0** unsupported claims; ≥ 60% of expected causes found; cheapest that passes |
| **S5 Narration sync** | Vanessa | 10:00 PM | One TTS call per segment · one call + character alignment | Default to per segment unless the other sounds clearly better |
| **S6 Film animation** | Vanessa | 6:30 PM | CSS image crossfade · canvas · pre-rendered MP4 | Smooth 60 fps on the demo laptop |

## 8. Demo checklist (Sun 10:45 AM)

- [ ] 6–8 cached stories play offline, narrated, captioned, each 60–90 s
- [ ] Every claim opens its excerpt; at least one story shows a "cause unknown" chapter
- [ ] Live build works on venue Wi-Fi, or we know the fallback line by heart
- [ ] Credit lines visible; no keys in git history
- [ ] README runs from a fresh clone; Devpost complete with video ≤ 3 min
- [ ] Each of us can explain the other's half in 60 seconds
