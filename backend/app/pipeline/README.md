# Pipeline module contracts

The stages are built in parallel, so each one codes against **these signatures only**. Shared types live in `types.py` (`Place`, `Aoi`, `SceneMeta`, `YearComposite`, band constants). Outbound HTTP must use `app.net.http_client()`, which carries the Norton/truststore TLS fix. Decisions: `docs/decisions/ADR-001` (imagery access) and `ADR-002` (composites, including the S2b masking fixes).

```text
run.py      place query ──► geocode.resolve ──► aoi.make_aoi ──► imagery.search_scenes ──► imagery.load_scenes
            ──► composite.composite_series ──► render.render_frames + metrics.compute_metrics ──► (S3 changes, S4 agent) ──► story.json
```

## geocode.py (FR-1, FR-2, FR-3)

```python
async def resolve(query: str, lat: float | None = None, lon: float | None = None,
                  display_name: str | None = None) -> Place
```

- Uses Nominatim with `settings.nominatim_user_agent`, at most 1 request/s, and a disk cache in `DATA_DIR/cache/geocode/`.
- If lat, lon and display_name are given (the user picked from the list), skip the search and only reverse-geocode to fill `admin`.
- Raises `app.errors.ApiError`:
  - `PLACE_NOT_FOUND` when nothing matches.
  - `PLACE_AMBIGUOUS` with `candidates` (≤ 5 of `{display_name, lat, lon, kind}`) when several results differ meaningfully.
  - `NO_LAND_DATA` when the point is open water (ocean or sea).

## imagery.py (FR-13, DR-1, ADR-001)

```python
async def search_scenes(aoi: Aoi, years: range = range(1984, 2027), months: tuple[int, int] = (6, 9),
                        max_cloud: float = 70, per_year: int = 6,
                        failed_years: set[int] | None = None) -> dict[int, list[SceneMeta]]
async def load_scenes(aoi: Aoi, scenes: list[SceneMeta]) -> np.ndarray  # uint16 [n, 7, H, W], BANDS order
```

- `search_scenes`: one parallel STAC query per year, Landsat C2 L2, returning up to `per_year` scenes per year sorted by `cloud_cover` ascending. Years with no scenes are left out.
- `load_scenes`: the PC data API `item/bbox/{utm_bbox}/{size}x{size}.npy`, all 7 assets, nearest resampling, in `aoi.epsg`.
  - Keep **only the 7 requested bands**: the API appends its own mask band.
  - Retries with backoff on 429/5xx, about 24 requests at a time.
  - **Disk cache** per (scene id, aoi), in `DATA_DIR/cache/scenes/`.
  - A scene that fails after retries comes back as all zeros (no data) and is logged; it never raises.

## composite.py (FR-13, FR-14, ADR-002 + S2b)

```python
def composite_series(by_year: dict[int, tuple[np.ndarray, list[SceneMeta]]], target_n: int = 4,
                     max_n: int = 6, hole_topup_pct: float = 0.5) -> dict[int, YearComposite]
```

- Input per year: all downloaded scenes (up to 6) with their metadata, in the same order.
- **Mask:** a pixel is invalid if qa == 0, any of bits 0–5 is set, or any reflectance band is 0 or 65535. Invalidate the whole pixel, not one band.
- **S2b fixes**, all in ADR-002 "Known masking failures":
  - Rank scenes by in-area clear fraction plus a haze score.
  - Bright-surface guard, which needs the neighbouring years: that's why the input is the whole series.
  - Per-pixel temporal outlier rejection.
- **Median** of the chosen `target_n` scenes. If holes are > `hole_topup_pct` % of the area, add scenes up to `max_n`.
- Holes stay 0, **never filled from other years**.
- `clear_pct`, `scenes` (only those that contributed pixels) and `notes` are filled in.

## render.py (FR-15, DR-2)

```python
def render_frames(series: dict[int, YearComposite], out_dir: Path) -> dict[int, tuple[Path, Path]]  # year → (frame, thumb)
```

- True-colour JPEGs, `frames/<year>.jpg` at 1024² and `thumbs/<year>.jpg` at 256².
- **One fixed stretch for every year and every place.** Constants are in the module and documented: no per-image auto-contrast.
- Holes render as neutral grey.

## metrics.py (FR-20, story.schema metrics)

```python
def indices(yc: YearComposite) -> dict[str, np.ndarray]   # 'ndvi', 'mndwi', 'ndbi' float32 [H, W], NaN on holes
def compute_metrics(series: dict[int, YearComposite]) -> list[dict]  # [{year, ndvi_mean, water_frac, built_frac}], ascending
```

- Definitions come from `story.schema.json`:
  - `ndvi_mean` = mean NDVI over clear pixels.
  - `water_frac` = share of clear pixels with MNDWI > 0.
  - `built_frac` = share of clear pixels with NDBI > 0, NDVI < 0.2 and MNDWI ≤ 0 (schema 1.1: water excluded).
- Reflectance = DN × 0.0000275 − 0.2 (DR-1).
- `indices` is also what S3's change detection reads.

## run.py (orchestration, HL-5, CONTRACTS §3)

Owned by Harman/Claude. It calls the stages in order, emits build events, applies FR-14 (skip years under 60% clear, with a reason), enforces the 180 s timeout, and writes outputs under `DATA_DIR/stories/<id>/`.
