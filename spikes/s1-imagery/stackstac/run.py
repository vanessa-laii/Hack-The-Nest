# Spike S1 (stackstac): load Landsat C2 L2 for Lake Mead 1984-2026 summers via stackstac and time it.
# Inputs: none (fixed AOI); optional CLI args: --threads N --chunk N --search single|per-year --out FILE.
# Outputs: results.json (per README shape) in this folder; prints timing summary.
import argparse, json, sys, time, threading
import truststore
truststore.inject_into_ssl()  # Windows: use OS cert store (certifi bundle failed: local AV TLS interception)
import ssl
from pathlib import Path

import numpy as np
import psutil
import dask
import planetary_computer
import pystac_client
import stackstac

BBOX = [-114.4235, 36.223, -114.3565, 36.277]
BANDS = ["blue", "green", "red", "nir08", "swir16", "swir22", "qa_pixel"]
HERE = Path(__file__).parent


def os_ca_bundle() -> str:
    """Export Windows ROOT certs trusted for server auth to a PEM so GDAL/libcurl trusts the same roots as the OS."""
    pems = [ssl.DER_cert_to_PEM_cert(c) for c, enc, trust in ssl.enum_certificates("ROOT")
            if enc == "x509_asn" and (trust is True or "1.3.6.1.5.5.7.3.1" in trust)]
    p = (HERE / ".venv" / "os_ca_bundle.pem").resolve()
    p.write_text("".join(pems))
    return str(p)

ap = argparse.ArgumentParser()
ap.add_argument("--threads", type=int, default=64)  # best run
ap.add_argument("--chunk", type=int, default=1024)
ap.add_argument("--out", default="results.json")
ap.add_argument("--search", choices=["single", "per-year"], default="per-year")
ap.add_argument("--search-threads", type=int, default=16)
ap.add_argument("--gdal-tune", action="store_true", help="HTTP/2 multiplex + bigger header read at open")
args = ap.parse_args()

proc = psutil.Process()
peak = [proc.memory_info().rss]
stop = threading.Event()
def watch():
    while not stop.is_set():
        peak[0] = max(peak[0], proc.memory_info().rss)
        time.sleep(0.1)
threading.Thread(target=watch, daemon=True).start()

problems = []
t0 = time.perf_counter()
cat = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)
from concurrent.futures import ThreadPoolExecutor

def summer(y):
    return list(cat.search(
        collections=["landsat-c2-l2"],
        bbox=BBOX,
        datetime=f"{y}-06-01/{y}-09-30",
        query={"eo:cloud_cover": {"lt": 50}},
        limit=100,
    ).items())

if args.search == "single":
    all_items = list(cat.search(collections=["landsat-c2-l2"], bbox=BBOX,
                                datetime="1984-01-01/2026-12-31",
                                query={"eo:cloud_cover": {"lt": 50}}, limit=500).items())
else:  # one Jun-Sep search per year, run in parallel
    with ThreadPoolExecutor(args.search_threads) as ex:
        all_items = [it for lst in ex.map(summer, range(1984, 2027)) for it in lst]
items = [it for it in all_items if 6 <= it.datetime.month <= 9]
# Re-run (orchestrator): optionally keep only the N clearest scenes per summer.
# Why: loading all 546 scenes is bound by the number of remote reads and misses the
# 60 s bar. A yearly composite only needs a few clear scenes (spike S2 decides how many).
import os
from collections import defaultdict
_n = int(os.environ.get("MAX_PER_YEAR", "0"))
if _n > 0:
    _by_year = defaultdict(list)
    for it in items:
        _by_year[it.datetime.year].append(it)
    items = [it for yr in sorted(_by_year)
             for it in sorted(_by_year[yr], key=lambda i: i.properties.get("eo:cloud_cover", 100))[:_n]]
t_search = time.perf_counter() - t0

# drop items missing any band asset
good = [it for it in items if all(b in it.assets for b in BANDS)]
if len(good) != len(items):
    problems.append(f"{len(items)-len(good)} items missing one of the band assets, dropped")
items = good

t1 = time.perf_counter()
stack = stackstac.stack(
    items,
    assets=BANDS,
    epsg=32611,
    resolution=30,
    bounds_latlon=BBOX,
    dtype="uint16",
    fill_value=np.uint16(0),  # plain int 0 rejected by stackstac 0.5.1 under numpy 2
    rescale=False,
    chunksize=args.chunk,
    gdal_env=stackstac.DEFAULT_GDAL_ENV.updated(always=dict(
        GDAL_CURL_CA_BUNDLE=os_ca_bundle(),
        **(dict(GDAL_HTTP_MULTIPLEX="YES", GDAL_HTTP_VERSION="2", GDAL_INGESTED_BYTES_AT_OPEN="32768",
                GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES") if args.gdal_tune else {}),
    )),
)
with dask.config.set(scheduler="threads", num_workers=args.threads):
    arr = stack.compute()
t_load = time.perf_counter() - t1
wall = time.perf_counter() - t0
stop.set()

data = arr.values  # (time, band, y, x) uint16
times = arr["time"].values.astype("datetime64[Y]").astype(int) + 1970
red = data[:, BANDS.index("red")].astype("float32")
valid = red > 0
refl = np.where(valid, red * 0.0000275 - 0.2, np.nan)
per_year = {}
for y in sorted(set(times.tolist())):
    sel = refl[times == y]
    v = sel[np.isfinite(sel)]
    if v.size:
        per_year[int(y)] = round(float(np.median(v)), 4)
years_with = sorted(per_year)
missing = [y for y in range(1984, 2027) if y not in per_year]
nonzero_frac = float(valid.mean())
platforms = sorted({it.properties.get("platform", "?") for it in items})

res = {
    "approach": "stackstac",
    "passed": bool(wall <= 60 and len(items) > 0),
    "wall_time_s": round(wall, 1),
    "search_time_s": round(t_search, 1),
    "load_time_s": round(t_load, 1),
    "n_items": len(items),
    "n_years_with_data": len(years_with),
    "years_missing": missing,
    "bands_loaded": BANDS,
    "array_shape": list(data.shape),
    "peak_memory_mb": round(peak[0] / 2**20),
    "scaling_applied": "reflectance = DN * 0.0000275 - 0.2 (DR-1)",
    "install_notes": "py -3.12 -m venv .venv; pip install pystac-client planetary-computer stackstac numpy dask rasterio psutil: all wheels, worked on py3.12 Windows 11",
    "problems": problems,
    "notes": f"search={args.search}, gdal_tune={args.gdal_tune}, load threads={args.threads}, chunksize={args.chunk}; nonzero red pixel fraction={nonzero_frac:.3f}; platforms={platforms}; STAC search returned {len(all_items)} items, {len(items)} Jun-Sep",
    "median_red_reflectance_by_year": per_year,
}
(HERE / args.out).write_text(json.dumps(res, indent=2))
print(json.dumps({k: v for k, v in res.items() if k != "median_red_reflectance_by_year"}, indent=2))
print(per_year)
