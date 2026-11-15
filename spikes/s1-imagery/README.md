# Spike S1: Imagery access

**Question:** What is the fastest reliable way to load 40 years of Landsat Collection 2 Level-2 surface reflectance (all bands + QA cloud mask) for one small area from Microsoft Planetary Computer?

**Decide by:** Sat 6:00 PM. Decision note: `docs/decisions/ADR-001-imagery-access.md`.

## Approaches

| Folder | Loader |
| --- | --- |
| `odc-stac/` | `pystac-client` + `planetary-computer` signing + `odc.stac.load` |
| `stackstac/` | `pystac-client` + `planetary-computer` signing + `stackstac.stack` |
| `pc-data-api/` | Planetary Computer data API (`https://planetarycomputer.microsoft.com/api/data/v1/...`, TiTiler item/mosaic endpoints), no local COG reads |

## Common test

- **Place:** Lake Mead, Nevada. Centre lat 36.25, lon -114.39. AOI 6 km square (bbox ≈ [-114.4235, 36.223, -114.3565, 36.277]).
- **Collection:** `landsat-c2-l2` on Planetary Computer (Landsat 4, 5, 7, 8, 9).
- **Dates:** 1984-01-01 to 2026-12-31, **summer months June–September** only.
- **Scene filter:** `eo:cloud_cover < 50` at search time.
- **Bands needed:** blue, green, red, nir08, swir16, swir22, qa_pixel. Note Landsat 4/5/7 vs 8/9 use the same common names on PC (`blue`, `green`, `red`, `nir08`, `swir16`, `swir22`, `qa_pixel`).
- **Resolution:** 30 m, in a UTM CRS (EPSG:32611 for Lake Mead).
- **Output:** one array per scene (or per year) for the AOI, loaded into memory as numpy. Then compute a trivial per-year median of red to prove the data is real.

## Pass bar

- 40 years (1984–2026) for one place loaded in **≤ 60 s** wall clock (cold, no local cache), all bands + cloud mask available.
- Works on Windows with Python 3.12.

## What each approach writes: `results.json`

```json
{
  "approach": "odc-stac",
  "passed": true,
  "wall_time_s": 41.2,
  "search_time_s": 3.1,
  "load_time_s": 38.1,
  "n_items": 412,
  "n_years_with_data": 41,
  "years_missing": [2012],
  "bands_loaded": ["blue","green","red","nir08","swir16","swir22","qa_pixel"],
  "array_shape": [412, 7, 200, 200],
  "peak_memory_mb": 900,
  "scaling_applied": "reflectance = DN * 0.0000275 - 0.2 (DR-1)",
  "install_notes": "pip install ... worked on py3.12 win",
  "problems": ["..."],
  "notes": "..."
}
```

Plus `run.py` (the reproducible script) and `requirements.txt`.

## Rules for spike agents

- Work **only** inside your own approach folder. Make a venv there (`py -3.12 -m venv .venv`), it is git-ignored.
- No API keys are needed (Planetary Computer signing is anonymous). Do not read `.env`.
- Don't write large files outside your folder; don't save GeoTIFFs into git (`*.tif` is ignored anyway).
- Don't commit.
