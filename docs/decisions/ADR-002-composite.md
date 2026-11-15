# ADR-002: Yearly composites and area size (spike S2)

**Status:** accepted · Sat 2026-11-14 · Owner: Harman

## Question

Which compositing method gives one clean image per summer, with no visible clouds and no flicker on an unchanged place? How many scenes per year does it need? What is the default area size? Pass bar: ≥ 90% of years clean, the control doesn't flicker, and compositing takes ≤ 10 s per place.

## Method

- **Input:** each place was downloaded once (`spikes/s2-composite/fetch.py`, the PC data API per ADR-001), up to 6 of the clearest scenes per summer, at 10 km.
- **Places:** Lake Mead, Paradise, and Central Park (the control). Each had about 255 scenes, covering all 43 years.
- **Approaches:** the three agents built composites offline from that same input, with N = 2, 4 and 6 scenes per year.
- **Scoring:** one shared scorer (`evaluate.py`) at 3, 6 and 10 km centre crops measured:
  - clean years: a year fails if more than 1% of its pixels are bright in blue, green and red at once
  - flicker on the control: year-to-year change in NDVI, as the area mean and per pixel
  - clear %
- **Visual check:** contact sheets with one fixed colour stretch (FR-15).

## Results

**Lake Mead and Paradise:** 100% clean years for every method, N and size.

**Central Park (control):**

| Method | N=4 flicker, 6 km (mean / per pixel) | Clean years at 10 km | Composite time / place |
| --- | --- | --- | --- |
| **Median** | **0.015 / 0.060** | 88–93% | 4–7 s |
| Medoid | 0.017 / 0.070 | 86–91% | 1.2–2.2 s |
| Best scene + gap fill | 0.032 / 0.075 | 79–84% | ~1 s |

- **Best-gapfill flickers most.** Each year is mostly one date, so the light, haze and water colour of that one day show through.
- **Median vs medoid: a close call.**
  - Median has less per-pixel noise. Part of that lead comes from how a median works: with an even number of clear values it averages the middle two.
  - Medoid has slightly less whole-area flicker at 10 km (0.0108 vs 0.0119). It takes every band from one real date, and it's about 3× faster.
  - We pick median because pixel noise is what a viewer sees in the film, and per-band medians are the common, easy-to-explain choice. Medoid is a drop-in alternative if the compositing time ever matters.
- **The cloud check barely separates the methods.** At 10 km, Central Park's "cloudy" years are the same Landsat 8/9 years for all three methods, which points to bright rooftops rather than leftover clouds. The decision therefore rests on flicker plus the contact sheets.
- **Clean years at 10 km:** the lower figures there come from bright Manhattan rooftops and from haze in 1986 and 1992, not from visible clouds. 3 km and 6 km were 100% clean for median.
- **Holes:**
  - Central Park keeps pixels with no clear observation at all. At 6 km with N=4: 1986 is 5.4% holes, 2021 5.3%, 2013 0.9%. That's Landsat 7 stripes plus clouds in every scene.
  - With N=6 the holes shrink to 1986 4.1% and 2021 1.0%.

## Decision

1. **Median composite**, per pixel and per band, computed with the uint16 sort-based median (`np.nanmedian` was 3× too slow).
2. **Cloud mask:**
   - `qa_pixel` is 0, or has any of bits 0 (fill), 1, 2, 3, 4, 5 set → invalid. Water (bit 7) is kept.
   - A reflectance value of 0 or 65535 → invalid.
   - The first spike README left out bit 0. That's corrected now, and medoid was re-run with the fix.
3. **Scenes per year:** start with the **4 clearest**. If more than 0.5% of the year's area is still a hole, **top up to 6**. This costs extra downloads only in the years that need them.
4. **Default area: 6 km** (HL-6 allows 3–10). 3 km leaves too little context around the place. 10 km downloads 2.8× the data, and its cloud check trips on bright cities.
5. **Remaining holes are never filled from other years.** That would put another year's pixels on this year's date (TR rules: no invented evidence). Holes render as neutral grey, the frame records `clear_pct` (DR-2), and years under 60% clear are skipped with a reason (FR-14).

## Fixes required when promoting the spike code (from QA)

- The spike's median code leaves out bit 0 and doesn't mask 65535.
  - Fill is still handled, but only because fill pixels here also have 0 in every band.
  - About 1,400 saturated 65535 values reached Central Park's output. That changed no score, but it's wrong.
  - The promoted code must use the full rule from point 2 and invalidate the **whole pixel**, not just one band.
- The data API returns an extra mask band after the 7 requested ones. The loader must select bands by name or position, never assume the band count.
- Story ids keep a non-integer area size as `6p5km`. That isn't in the CONTRACTS §2 id format yet, so tell Vanessa before any non-integer size ships.

## Known masking failures (fixed in S2b, results below)

These come from the gold-set review of Detroit-area places. They are failures of the QA mask itself, so every compositing method is affected and the choice of median stands. They must be fixed, and tested on these places, when the pipeline is built (call it **S2b**).

| # | Where | What happens | Why it matters |
| --- | --- | --- | --- |
| 1 | Gordie Howe bridge (42.289, −83.112), 2024–25 | The new bright concrete deck is flagged as cloud in every scene, so it becomes a hole | The **largest real change gets erased**: a truthfulness and science risk |
| 2 | Delray / SW Detroit, 2021 | Summer haze isn't in the QA bits, so the frame is washed out | Brightness change that isn't real |
| 3 | Willow Run (42.2424, −83.5552), 2020 | White cloud blobs aren't flagged | Fake "change"; flicker |

**Planned fixes, to be checked on Willow Run and Delray with contact sheets plus a test that each one is solved:**
- **Persistent-bright guard, for #1.** Clouds come and go; concrete stays. If a pixel is flagged cloud in at least 75% of that year's scenes *and* is also bright in the next year's composite, ignore the cloud bits for that pixel and take the median of all its non-fill observations. Never let a persistent surface become a hole.
- **Per-pixel temporal outlier rejection, for #3.** Within each year, drop an observation whose blue reflectance is far above that pixel's median for the year (for example, more than 0.08 higher), before taking the final median. One-off clouds get removed; surfaces that stay bright don't.
- **Pick scenes by haze inside the area, for #2.** Rank scenes by the share of clear pixels *inside the area* plus a haze score (mean blue reflectance over clear dark pixels such as water and vegetation), not by the scene-wide `eo:cloud_cover`. Where the data has an aerosol band (Landsat 8/9 `qa_aerosol`), drop high-aerosol pixels.

**Landsat 7 must stay in.** Without it, 2012 has no data: Landsat 5 ended in 2011 and Landsat 8 starts in 2013. Its stripe gaps become holes. If a year still falls under 60% clear, FR-14 skips it and says why.

## Consequences

- The real pipeline, per place: about 170–200 scene downloads (15–25 s), then about 5 s of compositing. That fits the 90 s median story budget (HL-5).
- Metrics (FR-20) are computed on the median composite, ignoring hole pixels.
- Later: show hole pixels in the evidence panel, so "grey" is explained, not mysterious.

## S2b results (2026-11-14)

All three failures are fixed in `backend/app/pipeline/composite.py`, checked on real imagery with before/after contact sheets in `spikes/s2b-mask/`. Compositing takes 1.4–2.0 s per place.

| Failure | Before → after | How |
| --- | --- | --- |
| A. Gordie Howe bridge deck erased | Holes in the deck window: 2024 12.7% → 1.5%, 2025 11.8% → 0.2%, 2026 10.4% → 0.6%. The deck is visible | **Persistent-bright guard.** A pixel is treated as a surface, not a cloud, if it is cloud-flagged in ≥ 75% of a summer's scenes, steadily bright (median blue ≥ 0.08, within ±0.05 in ≥ 75% of scenes) *and* confirmed by a summer ≤ 2 years away. A pixel flagged in one summer only stays masked |
| B. Willow Run 2020 white blobs | Cloud-like pixels 5.7% → 0.48% | The real cause was an all-white **Tier 2** scene that QA labels *snow*. The full mask (bit 5) plus **outlier rejection** (+0.08 blue above the pixel's median for that summer) removes it |
| C. Delray 2021 haze | Haze index 0.078 → 0.060 (neighbouring years 0.065 / 0.057) | **Hazy scenes demoted.** A scene ≥ 0.02 hazier than normal only fills pixels no clearer scene covers. Same-day scenes from neighbouring paths count as one look |

**Flicker didn't get worse; it improved slightly.** 6 km, N = 4, series / pixel flicker, before → after:

| Place | Before | After |
| --- | --- | --- |
| Central Park | 0.0147 / 0.0601 | 0.0130 / 0.0579 |
| Lake Mead | 0.0178 / 0.0397 | 0.0167 / 0.0398 |
| Paradise | 0.0194 / 0.0371 | 0.0141 / 0.0317 |

Lake Mead and Paradise stay 100% clean.

**Changes from the plan:**
- Ranking scenes by clear pixels inside the area plus haze made Central Park flicker *more* (0.018–0.020). Scene ranking therefore stays on cloud cover, and haze is handled by demoting hazy scenes as described above.
- **Scene ranking after QA.** Tier 2 scenes (+50 "cloud points") rank after clean ones both when downloading (`imagery.py`) and when compositing (`composite.py`), via the shared `types.scene_rank_score`. QA found Lake Mead 1993 composited from a Tier 2 scene.
- **Striped Landsat 7 penalty (+25) applies to downloads only.** That stops a summer from getting only striped scenes, the motivation from Willow Run 2023 (9.31% → 9.13% holes in the spike, not yet re-measured with this ranking). Inside the composite, measured on the S2 cache:

  | Composite ranking | Central Park flicker (series / pixel) |
  | --- | --- |
  | cloud cover only | 0.0130 / 0.0572 |
  | + Tier 2 penalty (chosen) | 0.0130 / 0.0575 |
  | + Tier 2 + stripe penalty | 0.0194 / 0.0589 |

  These numbers use the same method as the S2b "after" table above (spike scorer, S2 cache, centre 200 px, recomputed by QA). The stripe penalty pulled in September scenes, where autumn fading looks like flicker, and the median already fills the stripes. Trade-off: the stripe penalty would *help* Lake Mead slightly (series 0.0167 → 0.0154) and leaves Paradise unchanged. The control place not flickering is the pass bar, so Central Park decides.

**Still open:**
- Delray 2021 cloud-like pixels went 0% → 0.17% with the fixes (small; noted for honesty).
- Edge-ring pixels next to a confirmed bright surface ignore all QA flags, including shadow. A recurring shadow within 6 px of a deck could pass (QA: speculative, limited to those pixels).
- Single-year holes over dark water (Delray 1985/87/97/2017). The guard only covers bright surfaces. These stay grey and are reported in `clear_pct`.
- The Landsat 8/9 `qa_aerosol` band would detect haze directly. We don't download it.
