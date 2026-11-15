# Spike S2 shared input: downloads Landsat C2 L2 scene crops once per test place via the PC data API (ADR-001).
# Inputs: PLACES below; network (Planetary Computer STAC + data API). Run with any venv that has httpx, numpy, truststore.
# Outputs: data/cache/s2/<place>.npz (scenes uint16 [n,7,H,W] + per-scene metadata) and <place>.json (metadata only).
#
# Why a shared fetch step: the three compositing approaches must be compared on
# *identical* input, and running three downloaders at once skewed S1's timings.
# Download once at the largest area (10 km); the approaches crop the centre for 3 km and 6 km.
from __future__ import annotations

import asyncio
import io
import json
import math
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import truststore

# Norton Antivirus on the dev laptop re-signs TLS certificates. Using the Windows
# trust store keeps certificate checks on while still connecting (see ADR-001).
truststore.inject_into_ssl()

import httpx  # noqa: E402  (must come after truststore injection)
import numpy as np  # noqa: E402

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
DATA = "https://planetarycomputer.microsoft.com/api/data/v1"
BANDS = ["blue", "green", "red", "nir08", "swir16", "swir22", "qa_pixel"]
SIZE = 334            # 334 px × 30 m ≈ 10 km, the largest area S2 tests (HL-6 upper bound)
MAX_PER_YEAR = 6      # more than S1's 4, so S2 can test whether 2/4/6 scenes per year matter
MAX_CLOUD = 70        # looser than S1's 50: compositing methods should get to show they can use partly cloudy scenes
CONCURRENCY = 24
YEARS = range(1984, 2027)
OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "cache" / "s2"

# Test places from eval/gold_places.yaml. UTM zone = floor((lon + 180) / 6) + 1, northern hemisphere.
PLACES = {
    "lake-mead": {"lat": 36.25, "lon": -114.39, "zone": 11},
    "paradise": {"lat": 39.7596, "lon": -121.6219, "zone": 10},
    "central-park": {"lat": 40.7812, "lon": -73.9665, "zone": 18},  # CONTROL: should show no change
}


def utm_forward(lat: float, lon: float, zone: int) -> tuple[float, float]:
    """WGS84 lat/lon → UTM easting/northing (northern hemisphere), Krüger series.
    Why hand-rolled: avoids a pyproj dependency for one conversion. Accurate to well
    under a metre, which is plenty to snap a 30 m pixel grid. (Copied from S1.)"""
    a, f = 6378137.0, 1 / 298.257223563
    k0, e2 = 0.9996, f * (2 - f)
    n = f / (2 - f)
    A = a / (1 + n) * (1 + n**2 / 4 + n**4 / 64)
    al = [n / 2 - 2 * n**2 / 3 + 5 * n**3 / 16, 13 * n**2 / 48 - 3 * n**3 / 5, 61 * n**3 / 240]
    lon0 = math.radians(-183 + 6 * zone)
    phi, lam = math.radians(lat), math.radians(lon) - lon0
    e = math.sqrt(e2)
    t = math.sinh(math.atanh(math.sin(phi)) - e * math.atanh(e * math.sin(phi)))
    xi = math.atan2(t, math.cos(lam))
    eta = math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
    x = eta + sum(al[j] * math.cos(2 * (j + 1) * xi) * math.sinh(2 * (j + 1) * eta) for j in range(3))
    y = xi + sum(al[j] * math.sin(2 * (j + 1) * xi) * math.cosh(2 * (j + 1) * eta) for j in range(3))
    return 500000 + k0 * A * x, k0 * A * y


def snapped_utm_bbox(lat: float, lon: float, zone: int) -> tuple[int, int, int, int]:
    """Square AOI of SIZE pixels centred on the point, with edges on the Landsat 30 m grid.
    Why snap: Landsat C2 pixel edges sit at coordinate % 30 == 15 (checked in S1).
    Matching that grid means scenes in the native zone are cropped, not resampled."""
    e, n = utm_forward(lat, lon, zone)
    minx = math.floor((e - SIZE * 15 - 15) / 30) * 30 + 15
    miny = math.floor((n - SIZE * 15 - 15) / 30) * 30 + 15
    return minx, miny, minx + SIZE * 30, miny + SIZE * 30


async def search_year(client: httpx.AsyncClient, bbox_ll: list[float], year: int) -> list[dict]:
    """STAC search for one summer (June–September), following paging links.
    Why per year: one query across all years took ~25 s in S1; 43 parallel
    per-year queries take ~3–5 s. Only the fields we need are requested."""
    body = {"collections": ["landsat-c2-l2"], "bbox": bbox_ll, "limit": 250,
            "datetime": f"{year}-06-01T00:00:00Z/{year}-09-30T23:59:59Z",
            "query": {"eo:cloud_cover": {"lt": MAX_CLOUD}},
            "fields": {"include": ["id", "properties.datetime", "properties.platform",
                                   "properties.eo:cloud_cover", "properties.proj:code", "properties.proj:epsg"],
                       "exclude": ["assets", "links"]}}
    out, url = [], f"{STAC}/search"
    while url:
        for attempt in range(5):
            r = await client.post(url, json=body)
            if r.status_code == 200:
                break
            await asyncio.sleep(0.5 * 2**attempt)  # back off on 429/5xx
        r.raise_for_status()
        d = r.json()
        out += d["features"]
        nxt = [link for link in d.get("links", []) if link.get("rel") == "next"]
        url, body = (nxt[0]["href"], nxt[0].get("body", body)) if nxt else (None, None)
    return out


def clearest_per_year(features: list[dict]) -> list[dict]:
    """Keeps the MAX_PER_YEAR lowest-cloud scenes in each year, in date order.
    Why: S1 showed loading every scene is too slow. A yearly composite only
    needs a handful of clear looks; S2 decides the final number."""
    by_year: dict[int, list[dict]] = defaultdict(list)
    for f in features:
        by_year[int(f["properties"]["datetime"][:4])].append(f)
    keep = []
    for yr in sorted(by_year):
        keep += sorted(by_year[yr], key=lambda f: f["properties"].get("eo:cloud_cover", 100))[:MAX_PER_YEAR]
    return sorted(keep, key=lambda f: f["properties"]["datetime"])


async def fetch_scene(client, sem, item_id: str, ubox, zone: int, errors: list[str]):
    """Fetches one scene's 7 bands, cropped to the AOI, as a numpy array [7, H, W] uint16.
    Why nearest resampling: keeps the raw values (DR-1 scaling happens later in our
    code). Scenes from a neighbouring UTM zone do get reprojected (ADR-001 caveat)."""
    minx, miny, maxx, maxy = ubox
    url = f"{DATA}/item/bbox/{minx},{miny},{maxx},{maxy}/{SIZE}x{SIZE}.npy"
    epsg = f"epsg:{32600 + zone}"
    params = [("collection", "landsat-c2-l2"), ("item", item_id), ("coord_crs", epsg),
              ("dst_crs", epsg), ("resampling", "nearest"), ("reproject", "nearest")]
    params += [("assets", b) for b in BANDS]
    async with sem:
        for attempt in range(6):
            try:
                r = await client.get(url, params=params)
            except httpx.HTTPError as ex:
                await asyncio.sleep(1 + attempt)
                last = type(ex).__name__
                continue
            if r.status_code == 200:
                # The data API appends its own mask band after the requested assets,
                # so 7 assets come back as 8 bands. Keep only the 7 we asked for, in
                # BANDS order. (The current cache was written before this fix and
                # holds 8 bands; the approaches index bands 0–6, so it's unaffected.)
                return np.load(io.BytesIO(r.content), allow_pickle=False)[: len(BANDS)]
            if r.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep((2**attempt) * 0.5 + random.random())
                last = f"HTTP {r.status_code}"
                continue
            errors.append(f"{item_id}: HTTP {r.status_code}")
            return None
    errors.append(f"{item_id}: gave up ({last})")
    return None


async def fetch_place(name: str, p: dict) -> None:
    """Searches, downloads and saves one place's scenes. Scenes that fail to
    download are dropped and listed in the metadata, never silently filled."""
    t0 = time.perf_counter()
    d = 0.06  # ~6.5 km half-width in degrees: a search box safely covering the 10 km AOI
    bbox_ll = [p["lon"] - d, p["lat"] - d, p["lon"] + d, p["lat"] + d]
    ubox = snapped_utm_bbox(p["lat"], p["lon"], p["zone"])
    async with httpx.AsyncClient(timeout=60) as client:
        feats = [f for lst in await asyncio.gather(*(search_year(client, bbox_ll, y) for y in YEARS)) for f in lst]
    feats = clearest_per_year(feats)
    errors: list[str] = []
    sem = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(timeout=90, limits=limits) as client:
        arrays = await asyncio.gather(*(fetch_scene(client, sem, f["id"], ubox, p["zone"], errors) for f in feats))
    kept = [(f, a) for f, a in zip(feats, arrays) if a is not None]
    scenes = np.stack([a for _, a in kept]).astype(np.uint16)  # [n, 7, H, W]
    meta = {
        "place": name, **p, "utm_bbox": ubox, "epsg": 32600 + p["zone"], "size_px": SIZE, "pixel_m": 30,
        "bands": BANDS, "max_per_year": MAX_PER_YEAR, "max_cloud": MAX_CLOUD,
        "scenes": [{"id": f["id"], "datetime": f["properties"]["datetime"],
                    "platform": f["properties"].get("platform"),
                    "cloud_cover": f["properties"].get("eo:cloud_cover"),
                    "native_epsg": f["properties"].get("proj:epsg") or f["properties"].get("proj:code")}
                   for f, _ in kept],
        "download_errors": errors, "fetch_seconds": round(time.perf_counter() - t0, 1),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT_DIR / f"{name}.npz", scenes=scenes)
    (OUT_DIR / f"{name}.json").write_text(json.dumps(meta, indent=2))
    years = sorted({s["datetime"][:4] for s in meta["scenes"]})
    print(f"{name}: {len(kept)} scenes, {len(years)} years, {len(errors)} errors, {meta['fetch_seconds']} s")


def main() -> None:
    """Fetches places one after another. Why not in parallel: the polite request
    rate stays the same, and a failure in one place doesn't hide the others' output."""
    names = sys.argv[1:] or list(PLACES)
    for name in names:
        asyncio.run(fetch_place(name, PLACES[name]))


if __name__ == "__main__":
    main()
