# Spike S1 / odc-stac: time loading 1984-2026 summer Landsat C2 L2 for Lake Mead from Planetary Computer.
# Inputs: CLI flags --workers N --chunk N (dask threads, spatial chunk px); no API keys, anonymous PC signing.
# Outputs: results.json in this folder (timings, item counts, per-year median red reflectance), no rasters on disk.
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import dask
import numpy as np
import planetary_computer
import psutil
import pystac_client
from odc.stac import configure_rio, load

HERE = Path(__file__).parent
BBOX = [-114.4235, 36.223, -114.3565, 36.277]
BANDS = ["blue", "green", "red", "nir08", "swir16", "swir22", "qa_pixel"]
YEARS = list(range(1984, 2027))


def use_windows_trust_store():
    """This machine's Norton AV re-signs TLS; certifi lacks its root. Use the OS store for
    Python (truststore) and export it as a PEM bundle for GDAL/libcurl (COG reads)."""
    try:
        import ssl
        import truststore
        truststore.inject_into_ssl()
        if sys.platform == "win32":
            pem = HERE / ".venv" / "win_roots.pem"
            certs = [ssl.DER_cert_to_PEM_cert(c) for store in ("ROOT", "CA")
                     for c, enc, _ in ssl.enum_certificates(store) if enc == "x509_asn"]
            pem.write_text("".join(certs))
            os.environ["CURL_CA_BUNDLE"] = str(pem)
            os.environ["GDAL_CURL_CA_BUNDLE"] = str(pem)
        return True
    except ImportError:
        return False


def peak_mem_sampler(stop, out):
    proc = psutil.Process(os.getpid())
    while not stop.is_set():
        out[0] = max(out[0], proc.memory_info().rss)
        time.sleep(0.1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--chunk", type=int, default=2048)
    ap.add_argument("--search", choices=["cql2", "parallel"], default="cql2")
    ap.add_argument("--out",default=str(HERE / "results.json"))
    args = ap.parse_args()

    problems = []
    use_windows_trust_store()
    peak = [0]
    stop = threading.Event()
    threading.Thread(target=peak_mem_sampler, args=(stop, peak), daemon=True).start()

    t0 = time.perf_counter()
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    # One CQL2 query: OR of per-year June-Sept windows, cloud cover < 50.
    windows = [
        {"op": "t_intersects", "args": [{"property": "datetime"},
                                          {"interval": [f"{y}-06-01T00:00:00Z", f"{y}-09-30T23:59:59Z"]}]}
        for y in YEARS
    ]
    flt = {"op": "and", "args": [
        {"op": "<", "args": [{"property": "eo:cloud_cover"}, 50]},
        {"op": "or", "args": windows},
    ]}
    search = catalog.search(
        collections=["landsat-c2-l2"], bbox=BBOX,
        datetime="1984-01-01/2026-12-31", filter=flt, filter_lang="cql2-json",
        limit=1000,
    )
    if args.search == "cql2":
        items = list(search.items())
    else:
        # 43 per-year summer searches in parallel threads.
        from concurrent.futures import ThreadPoolExecutor

        def one(y):
            return list(catalog.search(
                collections=["landsat-c2-l2"], bbox=BBOX,
                datetime=f"{y}-06-01/{y}-09-30", query={"eo:cloud_cover": {"lt": 50}},
                limit=200).items())
        with ThreadPoolExecutor(16) as ex:
            items = [i for lst in ex.map(one, YEARS) for i in lst]
    # Safety: keep June-Sept only (in case server-side filter ignored).
    items = [i for i in items if 6 <= i.datetime.month <= 9]
    t_search = time.perf_counter() - t0
    print(f"search: {len(items)} items in {t_search:.1f}s", file=sys.stderr, flush=True)

    configure_rio(cloud_defaults=True)
    t1 = time.perf_counter()
    ds = load(
        items, bands=BANDS, bbox=BBOX, crs="EPSG:32611", resolution=30,
        chunks={"x": args.chunk, "y": args.chunk}, groupby="solar_day",
        fail_on_error=False,
    )
    with dask.config.set(scheduler="threads", num_workers=args.workers):
        ds = ds.compute()
    t_load = time.perf_counter() - t1

    # Prove data is real: per-year median red reflectance over valid (non-zero) pixels.
    red = ds["red"].values
    times = ds["time"].values.astype("datetime64[Y]").astype(int) + 1970
    per_year = {}
    for y in YEARS:
        sel = red[times == y]
        valid = sel[sel != 0]
        if valid.size:
            per_year[str(y)] = round(float(np.median(valid * 0.0000275 - 0.2)), 4)
    wall = time.perf_counter() - t0
    stop.set()

    years_missing = [y for y in YEARS if str(y) not in per_year]
    arr_shape = [int(ds.sizes["time"]), len(BANDS), int(ds.sizes["y"]), int(ds.sizes["x"])]
    platforms = sorted({i.properties.get("platform", "?") for i in items})
    res = {
        "approach": "odc-stac",
        "passed": wall <= 60 and set(BANDS) <= set(ds.data_vars),
        "wall_time_s": round(wall, 1),
        "search_time_s": round(t_search, 1),
        "load_time_s": round(t_load, 1),
        "n_items": len(items),
        "n_years_with_data": len(per_year),
        "years_missing": years_missing,
        "bands_loaded": [b for b in BANDS if b in ds.data_vars],
        "array_shape": arr_shape,
        "peak_memory_mb": round(peak[0] / 1e6),
        "scaling_applied": "reflectance = DN * 0.0000275 - 0.2 (DR-1)",
        "install_notes": "",
        "problems": problems,
        "notes": "",
        "run_config": {"dask_workers": args.workers, "chunk_px": args.chunk,
                       "groupby": "solar_day", "search_mode": args.search, "n_time_slices": arr_shape[0],
                       "platforms": platforms, "python": sys.version.split()[0]},
        "median_red_reflectance_by_year": per_year,
    }
    print(json.dumps(res, indent=2))
    Path(args.out).write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
