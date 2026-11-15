# Finds and downloads Landsat C2 L2 crops of an AOI from Planetary Computer (FR-13, DR-1, ADR-001).
# Inputs: types.Aoi, a year range, a months window; network (PC STAC + data API) or the disk cache.
# Outputs: search_scenes → {year: [SceneMeta]}; load_scenes → uint16 [n, 7, H, W], BANDS order.
#
# How it works:
#   1. Search each summer (one STAC query per year) in parallel; searches are cached on disk.
#   2. Keep the clearest `per_year` scenes of each year (lowest cloud cover).
#   3. Download each scene's crop from the data API, about 24 at a time (cached on disk).
#   4. Keep only the 7 bands we asked for: the API adds its own mask band.
#   5. Retry busy or failing requests with backoff; a scene that still fails becomes
#      all zeros (no data) and a failed year is left out. Nothing here raises for network errors.
from __future__ import annotations

import asyncio
import calendar
import io
import json
import logging
import os
import random
import uuid
from pathlib import Path

import httpx
import numpy as np

from app.config import settings
from app.net import http_client
from app.pipeline.types import BANDS, PLATFORM_TO_SENSOR, Aoi, SceneMeta, scene_rank_score

log = logging.getLogger(__name__)

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
DATA_URL = "https://planetarycomputer.microsoft.com/api/data/v1"
COLLECTION = "landsat-c2-l2"

# Landsat 4/5/7/8/9 only. Landsat 7 stays in on purpose: 2012 has no other
# sensor (L5 ended in 2011, L8 started in 2013), so dropping it leaves a gap.
ALLOWED_PLATFORMS = set(PLATFORM_TO_SENSOR)

CONCURRENCY = 24         # parallel data API requests; S1 measured this as fast and error-free
MAX_TRIES = 6            # attempts per request before giving up
BACKOFF_BASE_S = 0.5     # first retry waits ~0.5 s, doubling each time (tests set this to 0)
BACKOFF_CAP_S = 20.0     # never wait longer than this, even if Retry-After says so
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_STAC_PAGES = 10      # safety cap on paging (see _fetch_all_pages)
STAC_PAGE_LIMIT = 250    # one page almost always covers a summer; paging handles the rest

# Only the STAC fields SceneMeta needs. Full Landsat items carry ~20 asset
# blocks each, so trimming them makes the 43 searches much smaller and faster.
STAC_FIELDS = {
    "include": ["id", "properties.datetime", "properties.platform", "properties.eo:cloud_cover",
                "properties.proj:epsg", "properties.proj:code"],
    "exclude": ["assets", "links", "geometry", "bbox"],
}


# --- Shared HTTP retry and cache writes ---------------------------------------

def _retry_delay(attempt: int, resp: httpx.Response | None) -> float:
    """Seconds to wait before the next try: the server's Retry-After if numeric, else
    exponential backoff plus random jitter, so 24 parallel retries don't all hit at once."""
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), BACKOFF_CAP_S)
            except ValueError:
                pass  # HTTP-date form: rare, fall back to our own backoff
    backoff = BACKOFF_BASE_S * (2 ** attempt)
    jitter = random.random() * BACKOFF_BASE_S
    return min(backoff + jitter, BACKOFF_CAP_S)


async def _request(client: httpx.AsyncClient, method: str, url: str, what: str,
                   **kw) -> httpx.Response | None:
    """Sends one request, retrying 429/5xx and network errors; returns the 200 response or None.
    Other statuses (e.g. 404) fail at once. None instead of raising lets callers decide."""
    last = ""
    for attempt in range(MAX_TRIES):
        resp = None
        try:
            resp = await client.request(method, url, **kw)
        except httpx.HTTPError as ex:  # timeouts, resets, DNS hiccups on venue Wi-Fi
            last = type(ex).__name__
        else:
            if resp.status_code == 200:
                return resp
            last = f"HTTP {resp.status_code}"
            if resp.status_code not in RETRY_STATUS:
                log.warning("imagery: %s failed permanently (%s)", what, last)
                return None
        if attempt < MAX_TRIES - 1:
            await asyncio.sleep(_retry_delay(attempt, resp))
    log.warning("imagery: %s gave up after %d tries (%s)", what, MAX_TRIES, last)
    return None


def _atomic_write(path: Path, data: bytes) -> None:
    """Writes to a temp file next to `path`, then renames it into place.
    A crash mid-write must never leave a half-written file for an offline rebuild (HL-9)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)  # atomic on the same volume, Windows included
    finally:
        tmp.unlink(missing_ok=True)


# --- STAC search ------------------------------------------------------------

def _stac_cache_path(bbox_ll: tuple, year: int, months: tuple[int, int], max_cloud: float) -> Path:
    """Cache file for one (bbox, year, months, max_cloud) search.
    per_year isn't in the key: the file holds every match and the cut happens after reading."""
    w, s, e, n = bbox_ll
    name = f"{w}_{s}_{e}_{n}_{year}_m{months[0]}-{months[1]}_c{max_cloud}.json"
    return settings.data_dir / "cache" / "stac" / name


def _native_epsg(props: dict) -> int | None:
    """The scene's stored CRS as an int. Newer items use 'proj:code' ('EPSG:32611'),
    older ones 'proj:epsg' (32611)."""
    value = props.get("proj:epsg") or props.get("proj:code")
    if value is None:
        return None
    try:
        return int(str(value).split(":")[-1])
    except ValueError:
        return None


def _to_meta(feature: dict) -> SceneMeta | None:
    """STAC feature → SceneMeta, or None if not Landsat 4–9 or missing its date.
    Missing cloud cover counts as 100 % so it sorts last."""
    props = feature.get("properties", {})
    platform = props.get("platform")
    dt = props.get("datetime")
    if platform not in ALLOWED_PLATFORMS or not dt:
        return None
    cloud = props.get("eo:cloud_cover")
    cloud_cover = float(cloud) if cloud is not None else 100.0
    return SceneMeta(id=feature["id"], datetime=dt, year=int(dt[:4]), platform=platform,
                     cloud_cover=cloud_cover, native_epsg=_native_epsg(props))


def _read_stac_cache(cache: Path) -> list[SceneMeta] | None:
    """Scenes from a cached search, or None if there is no usable cache file."""
    if not cache.exists():
        return None
    try:
        records = json.loads(cache.read_text(encoding="utf-8"))
        return [SceneMeta(**record) for record in records]
    except (ValueError, TypeError):
        log.warning("imagery: unreadable STAC cache %s, searching again", cache.name)
        return None


def _search_body(aoi: Aoi, year: int, months: tuple[int, int], max_cloud: float) -> dict:
    """The STAC search request for one year's months window."""
    first_month, last_month = months
    last_day = calendar.monthrange(year, last_month)[1]
    start = f"{year}-{first_month:02d}-01T00:00:00Z"
    end = f"{year}-{last_month:02d}-{last_day:02d}T23:59:59Z"
    return {
        "collections": [COLLECTION],
        "bbox": list(aoi.bbox_ll),
        "limit": STAC_PAGE_LIMIT,
        "datetime": f"{start}/{end}",
        "query": {"eo:cloud_cover": {"lt": max_cloud}},
        "fields": STAC_FIELDS,
    }


def _next_link(page: dict) -> dict | None:
    """The page's rel=next link, or None on the last page."""
    for link in page.get("links", []):
        if link.get("rel") == "next":
            return link
    return None


def _next_body(link: dict, body: dict | None) -> dict | None:
    """Request body for a POST next link. PC puts a page token in the link's 'body';
    'merge' means that body only overrides keys of the original request."""
    link_body = link.get("body") or {}
    if link.get("merge") and body:
        return {**body, **link_body}
    return link_body or body


async def _fetch_all_pages(client: httpx.AsyncClient, body: dict | None,
                           year: int) -> list[dict] | None:
    """Every feature of a search, following rel=next links (POST with a body, or GET).
    None if any page fails, so a partial year is never cached or mistaken for complete."""
    url: str | None = f"{STAC_URL}/search"
    method = "POST"
    features: list[dict] = []
    pages = 0
    while url:
        # A summer has a few dozen scenes, so 1 page is normal. A server that keeps
        # sending `next` links would loop forever; give up and treat the year as failed.
        pages += 1
        if pages > MAX_STAC_PAGES:
            log.warning("imagery: STAC search %s exceeded %d pages; giving up",
                        year, MAX_STAC_PAGES)
            return None
        kw = {"json": body} if method == "POST" else {}
        resp = await _request(client, method, url, f"STAC search {year}", **kw)
        if resp is None:
            return None
        page = resp.json()
        features += page.get("features", [])

        link = _next_link(page)
        if link is None:
            url = None
        else:
            url = link["href"]
            method = link.get("method", "GET").upper()
            if method == "POST":
                body = _next_body(link, body)
    return features


async def _search_year(client: httpx.AsyncClient, aoi: Aoi, year: int, months: tuple[int, int],
                       max_cloud: float) -> list[SceneMeta] | None:
    """All allowed scenes for one year's months window, from cache or STAC; None on failure.
    One query per year: 43 parallel queries take 2–5 s, one big query 23–27 s (ADR-001)."""
    cache = _stac_cache_path(aoi.bbox_ll, year, months, max_cloud)
    cached = _read_stac_cache(cache)
    if cached is not None:
        return cached

    body = _search_body(aoi, year, months, max_cloud)
    features = await _fetch_all_pages(client, body, year)
    if features is None:
        return None

    scenes = []
    for feature in features:
        meta = _to_meta(feature)
        if meta is not None:
            scenes.append(meta)
    # Cache every match (not just the per_year cut) so stories rebuild offline (HL-9)
    # and a different per_year needs no new search.
    records = [s.__dict__ for s in scenes]
    _atomic_write(cache, json.dumps(records, indent=1).encode("utf-8"))
    return scenes


def _clearest(scenes: list[SceneMeta], year: int, per_year: int) -> list[SceneMeta]:
    """Up to per_year scenes from `year`, best rank score first (ties by date)."""
    # A scene from another year can't come from a same-year window; this keeps the dict honest.
    same_year = [s for s in scenes if s.year == year]
    ranked = sorted(same_year, key=lambda s: (scene_rank_score(s), s.datetime))
    return ranked[:per_year]


async def search_scenes(aoi: Aoi, years: range = range(1984, 2027),
                        months: tuple[int, int] = (6, 9),
                        max_cloud: float = 70, per_year: int = 6,
                        failed_years: set[int] | None = None) -> dict[int, list[SceneMeta]]:
    """The clearest Landsat 4/5/7/8/9 scenes per year over the AOI, one parallel query per year.
    Empty or failed years are left out (never raises). Pass a set as `failed_years` to learn
    which years failed, so they aren't reported as "no images" (that would be untrue).
    composite.py ranks the downloaded scenes with the same scene_rank_score."""
    async with http_client(timeout=60, max_connections=CONCURRENCY) as client:
        searches = [_search_year(client, aoi, y, months, max_cloud) for y in years]
        results = await asyncio.gather(*searches)

    scenes_by_year: dict[int, list[SceneMeta]] = {}
    for year, scenes in zip(years, results):
        if scenes is None and failed_years is not None:
            failed_years.add(year)  # the search itself failed, not "no scenes"
        if not scenes:
            continue
        ranked = _clearest(scenes, year, per_year)
        if ranked:
            scenes_by_year[year] = ranked
    return scenes_by_year


# --- Data API load ------------------------------------------------------------

def _scene_cache_dir(aoi: Aoi) -> Path:
    """Folder for one AOI's scene crops, keyed by CRS, origin and size,
    because the same scene cropped to another area or grid is a different array."""
    minx, miny = aoi.utm_bbox[0], aoi.utm_bbox[1]
    return settings.data_dir / "cache" / "scenes" / f"{aoi.epsg}_{minx}_{miny}_{aoi.size_px}"


def _read_cached(path: Path, shape: tuple[int, int, int]) -> np.ndarray | None:
    """A cached crop if it exists and has the expected shape, else None.
    An old or foreign file (e.g. the 8-band spike format) is downloaded again."""
    if not path.exists():
        return None
    try:
        arr = np.load(path, allow_pickle=False)
    except (OSError, ValueError, EOFError):  # EOFError: an empty or truncated file
        return None
    if arr.shape != shape:
        return None
    return arr.astype(np.uint16, copy=False)


def _crop_request(aoi: Aoi, scene: SceneMeta) -> tuple[str, list[tuple[str, str]]]:
    """URL and query params for one scene's crop from the data API.
    Nearest resampling keeps the raw values; DR-1 scaling happens later."""
    size = aoi.size_px
    minx, miny, maxx, maxy = aoi.utm_bbox
    url = f"{DATA_URL}/item/bbox/{minx},{miny},{maxx},{maxy}/{size}x{size}.npy"
    crs = f"epsg:{aoi.epsg}"
    params = [
        ("collection", COLLECTION),
        ("item", scene.id),
        ("coord_crs", crs),
        ("dst_crs", crs),
        ("resampling", "nearest"),
        ("reproject", "nearest"),
    ]
    for band in BANDS:
        params.append(("assets", band))
    return url, params


def _parse_crop(content: bytes, size: int) -> np.ndarray:
    """The downloaded .npy as uint16 [7, size, size]; raises ValueError if it's malformed.
    The API adds its own mask band after the 7 we asked for, so we keep only the first 7."""
    arr = np.load(io.BytesIO(content), allow_pickle=False)
    band_count = len(BANDS)
    if arr.ndim != 3 or arr.shape[0] < band_count or arr.shape[1:] != (size, size):
        raise ValueError(f"unexpected shape {arr.shape}")
    requested_bands = np.ascontiguousarray(arr[:band_count])
    return requested_bands.astype(np.uint16, copy=False)


def _save_crop(path: Path, arr: np.ndarray) -> None:
    """Caches one crop as .npy (atomic write)."""
    buf = io.BytesIO()
    np.save(buf, arr, allow_pickle=False)
    _atomic_write(path, buf.getvalue())


async def _load_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, aoi: Aoi, scene: SceneMeta,
                    cache_dir: Path) -> tuple[np.ndarray, bool]:
    """One scene's 7 bands cropped to the AOI, uint16 [7, size, size], plus whether it worked.
    On failure returns zeros (no data to composite.py's mask) and False, so one bad scene
    costs a little coverage instead of the whole story, and run.py can say why."""
    size = aoi.size_px
    shape = (len(BANDS), size, size)
    path = cache_dir / f"{scene.id}.npy"
    # Disk reads run in a thread so ~250 cached files don't block the event loop (HL-5).
    cached = await asyncio.to_thread(_read_cached, path, shape)
    if cached is not None:
        return cached, True

    url, params = _crop_request(aoi, scene)
    async with sem:  # caps requests in flight; httpx limits alone would queue but not bound memory
        resp = await _request(client, "GET", url, f"scene {scene.id}", params=params)
    if resp is None:
        return np.zeros(shape, np.uint16), False
    try:
        arr = _parse_crop(resp.content, size)
    except (ValueError, EOFError, OSError) as ex:  # EOFError: an empty 200 body
        log.warning("imagery: scene %s returned a bad array (%s); using no data", scene.id, ex)
        return np.zeros(shape, np.uint16), False

    _save_crop(path, arr)
    return arr, True


async def load_scenes(aoi: Aoi, scenes: list[SceneMeta],
                      failed_ids: set[str] | None = None) -> np.ndarray:
    """All scenes' crops as uint16 [n, 7, size, size], in input order and BANDS order.
    About 24 downloads at a time; the disk cache lets stories rebuild offline (HL-9).
    Never raises for network errors: a failed scene is all zeros, logged, and its id is
    added to `failed_ids` if given, so a skipped year can be blamed on the right cause."""
    size = aoi.size_px
    if not scenes:
        return np.zeros((0, len(BANDS), size, size), np.uint16)
    cache_dir = _scene_cache_dir(aoi)
    sem = asyncio.Semaphore(CONCURRENCY)
    async with http_client(timeout=90, max_connections=CONCURRENCY) as client:
        downloads = [_load_one(client, sem, aoi, s, cache_dir) for s in scenes]
        results = await asyncio.gather(*downloads)

    arrays = []
    for scene, (pixels, ok) in zip(scenes, results):
        arrays.append(pixels)
        if not ok and failed_ids is not None:
            failed_ids.add(scene.id)
    # Stacking ~140 MB takes about a second, so it runs off the event loop too.
    stacked = await asyncio.to_thread(np.stack, arrays)
    return stacked.astype(np.uint16, copy=False)
