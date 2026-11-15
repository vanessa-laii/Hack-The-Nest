# Spike S2: Clean yearly images + area size

**Question:** Which compositing method gives one clean image per summer (no visible clouds, no flicker on an unchanged place), how many scenes per year does it need, and what default area size should we use?

Decision note: `docs/decisions/ADR-002-composite.md`.

## Shared input (already downloaded, don't re-download)

`data/cache/s2/<place>.npz` → `scenes`: uint16 `[n, 7, 334, 334]`. Raw Landsat C2 L2 values, 0 = nodata, 30 m, about 10 km square, UTM.
`data/cache/s2/<place>.json` → band order, plus `scenes[i]`: `id`, `datetime`, `platform`, `cloud_cover`. Scenes are in date order, with up to 6 of the clearest per summer (cloud < 70%).

Places: `lake-mead` (water loss), `paradise` (2018 Camp Fire burn), `central-park` (**control: must not flicker**).

Band order: `blue, green, red, nir08, swir16, swir22, qa_pixel`.

### Rules every approach must follow (so results compare fairly)

- **Cloud mask from `qa_pixel`:** a pixel is invalid if `qa_pixel` is 0, or any of these bits is set: **0 fill**, 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow, 5 snow. Bit 7 (water) is *not* masked.
  - *Corrected during the spike.* The first version left out bit 0. In this data, no-data pixels have `qa_pixel == 1` and reflectance values of 0, which the first version counted as clear.
- Landsat 7 after May 2003 has SLC-off stripes. Those pixels carry the fill bit, so the rule above masks them.
- Also treat a reflectance value of 0 (no data) or 65535 (saturated) as invalid. Saturated values turn up in pixels the QA mask counts as clear.
- **Scene choice for N:** for each year, take the N lowest `cloud_cover` scenes from the cache. Run **N = 2, 4 and 6**.
- Composite the 6 reflectance bands (blue … swir22). Keep them as **uint16 raw values** (0 = no clear observation). Scaling to reflectance is linear, so medians and medoids are the same either way.

## Approaches (one folder each)

| Folder | Method |
| --- | --- |
| `median/` | Per-pixel, per-band median of the clear observations |
| `best-gapfill/` | Per year: the clearest scene by the *in-area* clear-pixel fraction, with masked pixels filled from the next-clearest scene, and so on |
| `medoid/` | Per-pixel medoid: the clear observation with the smallest summed distance to the other clear observations, over all 6 bands, so all bands come from one real date |

## What each approach writes

1. **`data/spikes/s2/<approach>/<place>_n<N>.npz`** (git-ignored), with:
   - `years`: int `[Y]`, every year 1984–2026 that has at least one scene
   - `dn`: uint16 `[Y, 6, 334, 334]` composite, 0 = no clear observation
2. **`spikes/s2-composite/<approach>/composite.py`:** a function `composite(scenes, meta, n) -> (years, dn)` plus a `__main__` that writes the 9 files above (3 places × 3 values of N).
3. **`spikes/s2-composite/<approach>/results.json`:** runtime in seconds per place and N, peak memory, and notes and problems.

Scoring is **not** done by the approaches. `spikes/s2-composite/evaluate.py` scores all of them with the same code: residual clouds, control flicker, clear %, and the 3/6/10 km crops. It also renders contact sheets with one fixed colour stretch (FR-15).

## Pass bar

- No visible clouds in ≥ 90% of years.
- The control place doesn't flicker.
- Composite step ≤ 10 s per place: it runs inside the 180 s story budget, after about 15–20 s of download.

## Rules for spike agents

- Work only in your own `spikes/s2-composite/<approach>/` folder, plus your own `data/spikes/s2/<approach>/` output folder.
- Python 3.12 venv in your folder (`.venv`, git-ignored). numpy only, plus scipy if you really need it.
- No network calls. No `.env`. No commits. Comment every function with **what it does and why**, and start each module with the 3-line header.
