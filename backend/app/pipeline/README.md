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
  - `ndvi_mean` = mean NDVI over clear land pixels (MNDWI ≤ 0; schema 1.2: water excluded).
  - `water_frac` = share of clear pixels with MNDWI > 0.
  - `built_frac` = share of clear pixels with NDBI > 0, NDVI < 0.2 and MNDWI ≤ 0 (schema 1.1: water excluded).
- Reflectance = DN × 0.0000275 − 0.2 (DR-1).
- `indices` is also what S3's change detection reads.

## run.py (orchestration, HL-5, CONTRACTS §3)

Owned by Harman/Claude. It calls the stages in order, emits build events, applies FR-14 (skip years under 60% clear, with a reason), enforces the 180 s timeout, and writes outputs under `DATA_DIR/stories/<id>/`.

## changes.py (S3 winner, ADR-003; FR-22, FR-23, DR-6, HL-2)

```python
def detect_changes(story_dir: Path) -> list[dict]   # 0–5 schema-1.3 changes[] items
```

- Reads `story_dir/imagery.json` and `story_dir/composites.npz`, both written by `run_imagery`.
- It is the promoted `spikes/s3-changes/combined/detect.py`: same rules, same thresholds.
- Writes `story_dir/masks/<chg-id>.png`: 1024 × 1024 greyscale, white = changed pixels, nearest upscale. Sets `mask_url` to its `/files/...` URL.
- Returns the list largest-first, ids `chg-01…`, each item validating against the story schema.

## story.py (assembly; TR-6, TR-8, HL-1, HL-11)

```python
def assemble_story(story_dir: Path, changes: list[dict], *, status: str = "complete",
                   generation: dict | None = None) -> dict
```

- Builds a full `story.json` from `imagery.json` (place, coverage, frames, metrics) plus `changes`.
- Until the cause agent (S4) exists, every change chapter has `confidence: "cause_unknown"`, no claims, and `sources: []`. The intro and outro have no claims either.
- **Script** of 150–225 words, from a fixed template. Every number, year and name comes from `imagery.json` (metrics, `place.display_name`) or from `changes` (years, `area_ha`, kind), per TR-6.
  - Each change segment says plainly that the cause hasn't been found (TR-8).
  - A `local` change is told in hectares ("about 6 hectares"), never as a share of the place.
- Returns the dict. The caller validates and saves it (`store.save_story`).

## build.py (full build; CONTRACTS §3 event stages)

```python
async def build_story(query: str, *, lat=None, lon=None, display_name=None, aoi_km=None,
                      sink=None, place: Place | None = None,
                      deadline: float | None = None) -> dict   # the saved story
```

- Steps: `run_imagery` → `detect_changes` (emits one `change` event per change, stage `changes`) → `assemble_story` (stage `script`) → `save_and_index` (stage `save`) → `done` event with `story_url`.
- `place`: an already-resolved Place (jobs.py passes the one main.py resolved). `run_imagery` then skips the geocoder, so the story id can't differ from the one the API returned. The CLI keeps using `query` / `lat` / `lon` / `display_name`.
- `deadline` (a `time.monotonic()` value): `save_and_index` checks it in its thread before writing and raises `DeadlinePassed` (a `TimeoutError`) instead of saving. A timeout cancels the coroutine but not the thread, so this check is what keeps a late story off disk.
- `save_and_index` writes `story.json` (validated) and the gallery entry in one thread call: if the index update fails, the new `story.json` is removed again.
- `update_index` holds a module-level `threading.Lock` around its read-modify-write of `stories/index.json`.
- Uses the same `EventLog` as run.py, so `seq` keeps counting.
- CPU work runs in threads.
- **No changes at all:** the schema needs 1–5 change chapters, so this raises `NoLastingChanges` (in build.py) for the caller to report.

## jobs.py + main.py (HTTP, CONTRACTS §1 and §3; HL-5)

- `POST /api/stories` with `EM_MOCK=0`:
  1. `geocode.resolve()` first, so its PLACE_NOT_FOUND / PLACE_AMBIGUOUS / NO_LAND_DATA come back as HTTP errors.
  2. Compute the story id. If `story.json` exists, return `200 cached`.
  3. Otherwise start one background job per id and return `202`. An existing job is reused while it is running, while it is **finishing** (timed out, but one of its worker threads hasn't returned yet), or for `NO_CHANGES_KEEP_S` (1 hour) after it ended with no lasting change; its `events_url` then replays that job's events, ending in its error.
- **At most `MAX_CONCURRENT_BUILDS` = 2 real builds at once** (an `asyncio.Semaphore`): each downloads ~250 scenes and keeps CPU threads busy. A queued job first emits one `progress` event (stage `geocode`, status `running`, "Waiting for another story to finish building"); the build's own events are renumbered after it so `seq` still goes up by 1.
- A job keeps every event in memory. `GET .../events` replays from `Last-Event-ID`, then streams live, with pings every 10 s.
- `GET /api/stories/{id}` returns `425 NOT_READY` while that id's job is running.
- **180 s timeout (HL-5)**, counted from when the build gets its slot (queue time is other builds' work) → `error` event with code `TIMEOUT`. Other failures → `UPSTREAM_ERROR` or `INTERNAL`, with a generic message (no paths or keys). A cancelled job (server shutdown) still ends with an `INTERNAL` error event, so browsers stop reconnecting.
- Worker threads: main.py's startup makes the loop's default thread pool a `jobs.TrackedExecutor`, which records each `asyncio.to_thread` call on the job that made it. That is how a timed-out job knows its threads are still running.
- `GET /api/stories` reads `stories/index.json`, newest first.
- Mock mode keeps working exactly as now.
