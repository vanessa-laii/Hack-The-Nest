# Spike S3: Change detection

**Question:** Given a place's yearly measurements and composites, which method best finds the real, lasting changes (and nothing at an unchanged place)? Decision note: `docs/decisions/ADR-003-change-detection.md`.

## Input (already built, don't rebuild)

`spikes/s3-changes/places.json` lists each gold place: its story folder, plus the expected changes from `eval/gold_places.yaml`. For each place, in `data/stories/<id>/`:

- `imagery.json`:
  - `metrics.series`: `[{year, ndvi_mean, water_frac, built_frac}]`, kept years only, ascending. `ndvi_mean` is land-only (schema 1.2); `built_frac` excludes water (1.1).
  - `frames[]`: `{year, sensors, scene_ids, clear_pct}` for each year.
  - `place.bbox`: [W, S, E, N].
- `composites.npz`: `years` int16 `[Y]` and `dn` uint16 `[Y, 6, H, W]` (blue, green, red, nir08, swir16, swir22; 0 = hole). Reflectance = DN × 0.0000275 − 0.2. Use `backend/app/pipeline/metrics.indices()` for per-pixel NDVI / MNDWI / NDBI.

## What every approach must provide

`spikes/s3-changes/<approach>/detect.py` with:

```python
def detect(story_dir: Path) -> list[dict]
```

It returns **0–5 changes** (HL-2: keep the 5 largest), each a valid `changes[]` item of `contracts/story.schema.json`:

- `id` `chg-01`…, `kind`, `metric`, `start_year`, `end_year`
- `magnitude`: after minus before, in the metric's own units
- `persistence_years`
- `region {bbox [W,S,E,N] in lon/lat, description}`: the description in plain words, e.g. "northeast quarter", "along the west shore"
- `before_year`, `after_year`: years that exist in the frames
- `mask_url: null`
- `sensor_switch_flag`

## Rules (from REQUIREMENTS)

- **FR-22:** a change must last ≥ 2 years after it happens. Fires (`burn`) are the exception: they can be sudden. Report `persistence_years`.
- **FR-23:** the control place (Central Park) must produce **0** changes. Also run on every other place, and keep false positives low.
- **DR-6:** set `sensor_switch_flag` when the change's start or end year is within 1 year of a sensor switch in `frames[].sensors` (e.g. Landsat 5 → 8 around 2012–2013, or Landsat 7-only years).
- **Shrinking-lake rule:** when built-up land rises in the same place and years that water falls, it is newly exposed shoreline. Report it as `water_loss`, never `urban_growth`.
- Kinds: `vegetation_loss`, `vegetation_gain`, `water_loss`, `water_gain`, `urban_growth`, `burn`, `other`. Demolition (built-up falls) is `other` on `built_frac`.
- **Region:** where the change happened, from per-pixel index differences between the before and after windows. The bbox must lie inside `place.bbox`.
- Only numbers from our own data: no web, no LLM.

## Approaches (one folder each)

| Folder | Method |
| --- | --- |
| `window-means/` | Before/after window means: slide a split year; compare the mean of the k years before with the k years after (k ≈ 3); keep splits whose jump is large relative to the year-to-year noise |
| `ruptures/` | Change-point detection with the `ruptures` library (PELT or binary segmentation) on each metric series, with a penalty tuned so the control gives 0 |
| `segments/` | Straight-segment fitting (LandTrendr-like): fit piecewise-linear segments to each series, then turn large-slope or step segments into changes |

## Scoring: one shared scorer

`python spikes/s3-changes/score.py <approach>` runs `detect()` on every place and reports:

- schema validity
- **recall**: an expected change counts as found if a detected change has a matching kind/metric and its years overlap the expected years ±2
- **false positives** per place
- **control changes**
- the shrinking-lake rule (Lake Mead must have no `urban_growth`)
- runtime

**Pass bar:** recall ≥ 80%, 0 changes on the control, all schema-valid, < 2 s per place.

## Rules for spike agents

- Work only in your own `spikes/s3-changes/<approach>/` folder. Python 3.12: use `backend/.venv` (numpy and jsonschema are already there). If you need a new package (e.g. `ruptures`), make your own `.venv` in your folder.
- No network calls (except pip), no `.env`, no commits.
- Follow `backend/STYLE.md`: 3-line header, a "How it works" list, short what/why comments.
- **Don't overfit to the scorer.** Thresholds must be physically sensible and explained in comments. The winner will run on places nobody has checked.
