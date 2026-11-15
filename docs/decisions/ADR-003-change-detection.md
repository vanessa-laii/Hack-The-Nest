# ADR-003: Change detection (spike S3)

**Status:** accepted · Sat 2026-11-14 · Owner: Harman

## Question

Given a place's yearly measurements and composites (ADR-002), which method finds the real, lasting changes, and nothing at an unchanged place? Pass bar:
- recall ≥ 80% of the gold changes
- 0 changes at the control (Central Park)
- schema-valid output
- under 2 s per place

## Method

- **Places and data:** 8 gold places built with the real pipeline, at 6 km, 1984–2026.
- **Approaches:** three in parallel, plus a combined one (S3b), each in `spikes/s3-changes/`.
- **Scoring:** one shared scorer (`score.py`).
- **Tightening:** after the first round the scorer got stricter:
  - one-to-one matching, so a spray of small detections can't count for several expected changes
  - counts of **contradictions**, where a change's kind disagrees with its number's direction (e.g. "urban growth" while built-up fell)
  - counts of **tiny** changes (< 0.5 points)
  
  Each detected change becomes a story chapter, so either flaw would put a false sentence in the narration.

## Results (strict scorer, original gold set of 9)

| Method | Recall | Control | Contradictions | Tiny | Extra changes |
| --- | --- | --- | --- | --- | --- |
| Before/after window means (+ per-pixel layer) | 8/9 | 0 | 7 | 12 | 12 |
| Segment fitting (LandTrendr-like) | 4/9 | 0 | 1 | 0 | 4 |
| `ruptures` change points | 3/9 | 0 | 0 | 0 | 4 |
| **Combined (S3b)** | 6/9 → **7/7** after the gold review | **0** | **0** | **0** | 10, all checked in the frames |

**What we learned:**
- **Area averages hide small changes.** The Willow Run demolition, the test track and the Gordie Howe bridge each cover about 1% of a 6 km square, below the year-to-year noise of the averages. Only a per-pixel layer sees them.
- **Every method hits the same three limits** (below), so they are limits of the data, not of a method.

## Decision

**Combined detector** (`spikes/s3-changes/combined/detect.py`), promoted next into `backend/app/pipeline/changes.py`.

1. **Place-wide layer:** piecewise-linear segments on `ndvi_mean`, `water_frac` and `built_frac`.
   - Despike one-summer outliers first.
   - Keep a change if it is ≥ max(physical floor, 4 × the series' own noise).
2. **Local-patch layer:**
   - A patch must be ≥ 4 ha, connected, and *fully* flipped: none of the 3 summers before, all 3 after.
   - The old state must have held for 6 summers.
   - The control's largest such patch is ~2 ha. Every size from 2.5 to 6 ha keeps the control at 0.
3. **Rules:**
   - **Lasting change:** ≥ 2 summers (FR-22). Burns are excepted, but need dNBR ≥ 0.27.
   - **Sensor switch:** a change within 1 year of a sensor switch needs ≥ 2 later switch-free summers, and keeps `sensor_switch_flag` (DR-6).
   - **Shrinking lake:** exposed shore is water loss, never urban growth.
   - **No contradictions:** a change's kind always matches its number's direction.
   - **Merging:** nearby patches within 500 m and 2 years count as one change.
   - **Limit:** ≤ 5 changes, place-wide first (HL-2).
4. **Local magnitudes are measured inside the change's region**, not over the whole square. A patch that went fully built reads `+1.0` on `built_frac`. The narration (S4) must say "in this area", not "of the place".

## Gold set review (Harman approved, 2026-11-14)

Three gold expectations conflicted with our own rules or the data, so `eval/gold_places.yaml` was updated, with the reason recorded on each entry:

- **Lake Mead 2023 water gain: removed.** It only partly undid the one-summer 2022 low and was gone by 2026. Not a lasting change under FR-22.
- **Summerlin: `urban_growth` → `vegetation_gain` on `ndvi_mean`.** Our built-up measure counts bare desert as built, so building the suburb makes `built_frac` fall. What the data honestly shows is desert turning green: NDVI 0.10 → 0.21, 1991–2019, matching the frames.
- **Willow Run runway removal: removed.** The runways are 1–2 Landsat pixels wide, below what a reliable patch can resolve.

## Limits (say these in the README and the "About this data" panel)

- At 30 m, a change narrower than about 60 m or smaller than about 4 ha isn't reported.
- In deserts, new suburbs show as greening, not as built-up growth. A better built-up index is future work.
- Short reversals (one or two summers) are deliberately not reported.
- **"Urban growth" patches are often cleared land, not finished buildings** (Delray 2015–16, Willow Run 2001–02). Bare ground passes the built-up test. The region text says "cleared or built over", and the narration (S4) must not claim buildings.
- **Falls in built-up.** Bare fields and quarries score as built, so a fall usually means regrowth. Place-wide falls are re-read as greening, or dropped, and are never told as demolition. A *local* patch that fully flipped from built to not-built and did *not* turn green stays `other` (e.g. Willow Run 2013–14, −1.0 over 6 ha). That is the demolition case, and S4 still needs a source before narrating it as one.
- **Dates can be a summer early.** Willow Run's plant demolition shows in the 2014 composite, while the records say September 2014 – January 2015. Composites are summer snapshots, so S4 relies on TR-4's ±1 year.
- The 4 ha minimum patch size sits just below where gold patches start to drop out (5 ha). The control stays at 0 for anything from 2.5 to 6 ha. It's a judgement call on a small gold set and should be re-checked on new places.
- The sensor-switch rule (≥ 2 later switch-free summers) now applies to place-wide and local changes alike.
- **Contract 1.3 (agreed with Vanessa):** every change carries `scope` (`place` | `local`) and `area_ha`. `magnitude` is over the whole area for `place` and inside the region for `local`. Local changes are shown as "≈ N ha", not on the whole-area chart. `area_ha` counts every changed pixel, for either scope. For a burn it is the area that lost noticeable greenness, not a burn-severity map. The narration may quote `area_ha` ("about 6 hectares"), never a local magnitude as a share of the place.
- The "extra" changes are real, visible ones the gold set didn't list: Lake Mead's 1980s–90s level swings, Palm Jumeirah landscaping, cleared lots in Delray. Several will become "cause unknown" chapters, which is the honest outcome (TR-8).
