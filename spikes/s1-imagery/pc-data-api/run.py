# Spike S1 / pc-data-api: load 40 years of Landsat C2 L2 for a 6 km AOI via the PC data API (server-side TiTiler crop).
# Inputs: none (Lake Mead AOI, landsat-c2-l2, Jun-Sep 1984-2026, cloud<50 hard-coded per spikes/s1-imagery/README.md).
# Outputs: results.json in this folder (timings, shapes, per-year median red reflectance, fidelity checks).
import asyncio
import io
import json
import math
import os
import random
import sys
import time
import tracemalloc
from collections import defaultdict
from pathlib import Path

import truststore

truststore.inject_into_ssl()  # Windows: certifi bundle failed TLS verify here; use OS trust store

import httpx
import numpy as np

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
DATA = "https://planetarycomputer.microsoft.com/api/data/v1"
BBOX = [-114.4235, 36.223, -114.3565, 36.277]
LAT, LON = 36.25, -114.39
BANDS = ["blue", "green", "red", "nir08", "swir16", "swir22", "qa_pixel"]
SIZE = 200  # 200 px * 30 m = 6 km
CONCURRENCY = int(sys.argv[1]) if len(sys.argv) > 1 else 24
# Output file can be overridden so capped re-runs don't overwrite the original results.json.
OUT = Path(__file__).parent / os.environ.get("RESULTS_FILE", "results.json")


def utm_forward(lat, lon, zone):
    """WGS84 -> UTM (northern hemisphere), Krueger series; sub-mm accurate, enough to snap a grid."""
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


def snapped_utm_bbox():
    # Landsat C2 UTM grid: pixel edges at coordinate % 30 == 15 (checked against proj:transform).
    e, n = utm_forward(LAT, LON, 11)
    minx = math.floor((e - SIZE * 15 - 15) / 30) * 30 + 15
    miny = math.floor((n - SIZE * 15 - 15) / 30) * 30 + 15
    return minx, miny, minx + SIZE * 30, miny + SIZE * 30


class _It:  # minimal item record (id, datetime, properties) from raw STAC JSON
    def __init__(self, f):
        import datetime as dt
        self.id = f["id"]
        self.properties = f["properties"]
        self.datetime = dt.datetime.fromisoformat(f["properties"]["datetime"].replace("Z", "+00:00"))


async def _search_year(client, year):
    body = {"collections": ["landsat-c2-l2"], "bbox": BBOX, "limit": 250,
            "datetime": f"{year}-06-01T00:00:00Z/{year}-09-30T23:59:59Z",
            "query": {"eo:cloud_cover": {"lt": 50}},
            "fields": {"include": ["id", "properties.datetime", "properties.platform", "properties.proj:code",
                                   "properties.proj:epsg", "properties.eo:cloud_cover"], "exclude": ["assets", "links"]}}
    out = []
    url = f"{STAC}/search"
    while url:
        for attempt in range(5):
            r = await client.post(url, json=body)
            if r.status_code == 200:
                break
            await asyncio.sleep(0.5 * 2**attempt)
        r.raise_for_status()
        d = r.json()
        out += d["features"]
        nxt = [l for l in d.get("links", []) if l.get("rel") == "next"]
        url, body = (nxt[0]["href"], nxt[0].get("body", body)) if nxt else (None, None)
    return out


async def _search_all():
    async with httpx.AsyncClient(timeout=60) as client:
        res = await asyncio.gather(*(_search_year(client, y) for y in range(1984, 2027)))
    return [_It(f) for feats in res for f in feats]


def search():
    # Per-year Jun-Sep searches in parallel via raw STAC API POST (pystac-client paging over all
    # 43 years took ~23 s in run 1). Assets are not needed: the data API resolves them server-side.
    items = asyncio.run(_search_all())
    items = [it for it in items if it.datetime.month in (6, 7, 8, 9)]
    # Re-run (orchestrator): optionally keep only the N clearest scenes per summer.
    # Why: all 546 scenes = ~3,800 remote reads, which is latency-bound and misses the
    # 60 s bar. A yearly composite only needs a few clear scenes (spike S2 decides how many).
    n = int(os.environ.get("MAX_PER_YEAR", "0"))
    if n > 0:
        by_year = defaultdict(list)
        for it in items:
            by_year[it.datetime.year].append(it)
        items = [it for yr in sorted(by_year)
                 for it in sorted(by_year[yr], key=lambda i: i.properties.get("eo:cloud_cover", 100))[:n]]
    return items


async def fetch(client, sem, item_id, ubox, stats):
    minx, miny, maxx, maxy = ubox
    url = f"{DATA}/item/bbox/{minx},{miny},{maxx},{maxy}/{SIZE}x{SIZE}.npy"
    params = [("collection", "landsat-c2-l2"), ("item", item_id), ("coord_crs", "epsg:32611"),
              ("dst_crs", "epsg:32611"), ("resampling", "nearest"), ("reproject", "nearest")]
    params += [("assets", b) for b in BANDS]
    async with sem:
        for attempt in range(6):
            try:
                r = await client.get(url, params=params)
            except httpx.HTTPError as ex:
                stats["errors"].append(f"{item_id}: {type(ex).__name__}")
                await asyncio.sleep(1 + attempt)
                continue
            if r.status_code == 200:
                arr = np.load(io.BytesIO(r.content), allow_pickle=False)
                stats["bytes"] += len(r.content)
                return item_id, arr
            if r.status_code in (429, 500, 502, 503, 504):
                stats["retries"][r.status_code] += 1
                ra = r.headers.get("retry-after")
                await asyncio.sleep(float(ra) if ra and ra.isdigit() else (2**attempt) * 0.5 + random.random())
                continue
            stats["errors"].append(f"{item_id}: HTTP {r.status_code} {r.text[:120]}")
            return item_id, None
    stats["errors"].append(f"{item_id}: gave up after retries")
    return item_id, None


async def load_all(items, ubox, stats):
    sem = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(timeout=60, limits=limits, http2=False) as client:
        return await asyncio.gather(*(fetch(client, sem, it.id, ubox, stats) for it in items))


async def point_check(item_id, lon, lat):
    # Native pixel value at a point (no resampling) to compare against the crop.
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{DATA}/item/point/{lon},{lat}",
                             params=[("collection", "landsat-c2-l2"), ("item", item_id), ("assets", "red")])
        return r.status_code, r.json() if r.status_code == 200 else r.text[:200]


def utm_to_ll_approx(e, n):
    # inverse by Newton on utm_forward (good enough for a pixel-center check)
    lat, lon = LAT, LON
    for _ in range(20):
        x, y = utm_forward(lat, lon, 11)
        lat += (n - y) / 111000.0
        lon += (e - x) / (111000.0 * math.cos(math.radians(lat)))
    return lat, lon


def main():
    tracemalloc.start()
    t0 = time.perf_counter()
    items = search()
    t1 = time.perf_counter()
    ubox = snapped_utm_bbox()
    stats = {"bytes": 0, "retries": defaultdict(int), "errors": []}
    results = asyncio.run(load_all(items, ubox, stats))
    t2 = time.perf_counter()
    _, peak = tracemalloc.get_traced_memory()

    meta = {it.id: it for it in items}
    good = [(iid, a) for iid, a in results if a is not None]
    first = good[0][1] if good else None
    raw_shape = list(first.shape) if first is not None else None
    raw_dtype = str(first.dtype) if first is not None else None

    # TiTiler .npy = data bands (+ trailing mask band when return_mask=True)
    stack = np.stack([a[: len(BANDS)] for _, a in good]) if good else np.zeros((0,))
    by_year = defaultdict(list)
    for (iid, a) in good:
        red = a[BANDS.index("red")].astype("float64")
        qa = a[BANDS.index("qa_pixel")].astype("uint16")
        valid = (red > 0) & ((qa & 0b11010) == 0)  # drop fill + dilated cloud/cloud/shadow (bits 1,3,4)
        if valid.sum() > 0:
            by_year[meta[iid].datetime.year].append(red[valid] * 0.0000275 - 0.2)
    per_year = {y: round(float(np.median(np.concatenate(v))), 4) for y, v in sorted(by_year.items())}
    years_missing = [y for y in range(1984, 2027) if y not in per_year]

    # Fidelity checks
    fidelity = {}
    if good:
        iid, a = good[len(good) // 2]
        red = a[BANDS.index("red")]
        fidelity["dtype"] = raw_dtype
        fidelity["red_dn_min_max"] = [int(red[red > 0].min()), int(red.max())] if (red > 0).any() else None
        fidelity["red_is_integer_dn"] = bool(np.all(np.equal(np.mod(red, 1), 0)))
        # pixel (row 50, col 120) center in UTM -> lon/lat -> /point
        r_, c_ = 50, 120
        e = ubox[0] + (c_ + 0.5) * 30
        n = ubox[3] - (r_ + 0.5) * 30
        lat, lon = utm_to_ll_approx(e, n)
        code, pt = asyncio.run(point_check(iid, lon, lat))
        fidelity["point_check"] = {"item": iid, "crop_value": int(red[r_, c_]), "point_api": pt, "http": code}
        fidelity["item_epsgs"] = sorted({str(m.properties.get("proj:code") or m.properties.get("proj:epsg")) for m in items})

    wall = t2 - t0
    passed = bool(wall <= 60 and good and len(good) == len(items) and len(years_missing) <= 3)
    platforms = sorted({m.properties.get("platform") for m in items})
    res = {
        "approach": "pc-data-api",
        "passed": passed,
        "wall_time_s": round(wall, 1),
        "search_time_s": round(t1 - t0, 1),
        "load_time_s": round(t2 - t1, 1),
        "n_items": len(items),
        "n_years_with_data": len(per_year),
        "years_missing": years_missing,
        "bands_loaded": BANDS,
        "array_shape": list(stack.shape),
        "peak_memory_mb": round(peak / 1e6, 1),
        "scaling_applied": "reflectance = DN * 0.0000275 - 0.2 (DR-1), applied client-side; server returned raw DN (unscale=false)",
        "install_notes": "pip install httpx numpy truststore (pystac-client used only in run 1) worked on py3.12 win; no GDAL/rasterio needed (.npy output)",
        "problems": [],
        "notes": "",
        "per_year_median_red": per_year,
        "fidelity": fidelity,
        "n_loaded": len(good),
        "concurrency": CONCURRENCY,
        "bytes_downloaded_mb": round(stats["bytes"] / 1e6, 1),
        "retries_by_status": dict(stats["retries"]),
        "errors": stats["errors"][:20],
        "n_errors": len(stats["errors"]),
        "platforms": platforms,
        "utm_bbox_epsg32611": list(ubox),
        "raw_npy_shape_per_item": raw_shape,
    }
    OUT.write_text(json.dumps(res, indent=2))
    print(json.dumps({k: v for k, v in res.items() if k != "per_year_median_red"}, indent=2))
    print(per_year)


if __name__ == "__main__":
    main()
