# What: spike S2 "median" approach - per-pixel, per-band median of clear Landsat observations per year.
# Inputs: data/cache/s2/<place>.npz (scenes uint16 [n,7,334,334]) + <place>.json (band order, scene metadata).
# Outputs: data/spikes/s2/median/<place>_n<N>.npz (years, dn uint16 [Y,6,334,334]) + results.json here.
from __future__ import annotations

import json
import sys
import time
import tracemalloc
import warnings
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
CACHE = REPO / "data" / "cache" / "s2"
OUT = REPO / "data" / "spikes" / "s2" / "median"
RESULTS = Path(__file__).resolve().parent / "results.json"

PLACES = ["lake-mead", "paradise", "central-park"]
NS = [2, 4, 6]
REFLECTANCE_BANDS = ["blue", "green", "red", "nir08", "swir16", "swir22"]

# QA_PIXEL bits that make a pixel invalid (README rule): 1 dilated cloud, 2 cirrus,
# 3 cloud, 4 cloud shadow, 5 snow. Bit 7 (water) is deliberately NOT included.
QA_BAD_BITS = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5)


def clear_mask(qa: np.ndarray) -> np.ndarray:
    """Return a boolean array that is True where a pixel is a clear observation.

    Why: every approach must use the same cloud rule so results compare fairly.
    A pixel is invalid when QA is 0 (fill, which also covers Landsat 7 SLC-off
    stripes) or any of the cloud/shadow/snow bits is set.
    """
    return (qa != 0) & ((qa & QA_BAD_BITS) == 0)


def select_scenes(meta: dict, n: int) -> dict[int, list[int]]:
    """Map each year to the indices of its N lowest-cloud_cover scenes.

    Why: the README fixes the scene choice per year (N lowest cloud_cover) so
    the three approaches see identical input. Ties are broken by date order
    (stable sort) so the choice is deterministic. Indices are returned in date
    order, which does not matter for a median but keeps things readable.
    """
    by_year: dict[int, list[int]] = {}
    for i, s in enumerate(meta["scenes"]):
        by_year.setdefault(int(s["datetime"][:4]), []).append(i)
    chosen: dict[int, list[int]] = {}
    for year, idx in sorted(by_year.items()):
        ranked = sorted(idx, key=lambda i: float(meta["scenes"][i]["cloud_cover"]))
        chosen[year] = sorted(ranked[:n])
    return chosen


def reference_median_year(stack: np.ndarray, band_idx: list[int], qa_idx: int) -> np.ndarray:
    """Slow, obviously-correct median: float32 with NaN for invalid, np.nanmedian.

    Why: kept only as the oracle for self_test. It is the straightforward
    NaN-aware median, but np.nanmedian on a short axis (k <= 6 scenes) uses a
    masked-array fallback that took 12-19 s per place, over the 10 s budget,
    so the production path is median_year below, checked to match this exactly.
    """
    valid = clear_mask(stack[:, qa_idx])
    refl = stack[:, band_idx].astype(np.float32)
    refl[~valid[:, None, :, :] | (refl == 0)] = np.nan
    with warnings.catch_warnings():
        # All-NaN pixels raise "All-NaN slice encountered"; they are expected.
        warnings.simplefilter("ignore", category=RuntimeWarning)
        med = np.nanmedian(refl, axis=0)
    out = np.zeros(med.shape, dtype=np.uint16)
    ok = ~np.isnan(med)
    out[ok] = np.rint(med[ok]).astype(np.uint16)
    return out


def median_year(stack: np.ndarray, band_idx: list[int], qa_idx: int) -> np.ndarray:
    """Composite one year's scenes [k,7,H,W] into a uint16 [6,H,W] median image.

    Why median: robust to the odd residual cloud edge or haze the QA mask
    misses, as long as most observations are clean. Invalid pixels (QA rule,
    plus reflectance DN 0 = fill) are ignored per pixel, so a pixel clear in 1
    of 3 scenes takes that one value; pixels never clear are written as 0.

    Why this implementation: it stays in uint16 (half the memory of float32,
    and a faster sort). Invalid values are set to 65535 so they sort last. The
    valid count c comes from the mask, NOT from the sentinel, because 65535 is
    also a real (saturated, QA-clear) DN in the data; since sentinels equal the
    maximum value, the first c sorted entries are still exactly the c valid
    values. Median = mean of sorted slots (c-1)//2 and c//2, rounded half to
    even to match np.rint in the reference. Result equals reference_median_year.
    """
    k = stack.shape[0]
    valid = clear_mask(stack[:, qa_idx])[:, None]  # [k,1,H,W]
    refl = stack[:, band_idx]  # fancy index -> fresh copy we can modify
    invalid = ~valid | (refl == 0)  # [k,6,H,W]
    refl[invalid] = 65535
    refl.sort(axis=0)
    c = k - np.count_nonzero(invalid, axis=0)  # [6,H,W] valid count
    lo = np.maximum((c - 1) // 2, 0)[None]
    hi = np.minimum(c // 2, k - 1)[None]
    total = np.take_along_axis(refl, lo, 0)[0].astype(np.uint32)
    total += np.take_along_axis(refl, hi, 0)[0]
    half = total >> 1
    half += (total & 1) & (half & 1)  # .5 cases: round half to even
    half[c == 0] = 0  # no clear observation -> agreed nodata code
    return half.astype(np.uint16)


def composite(scenes: np.ndarray, meta: dict, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Build yearly median composites from the cached scenes.

    Why: this is the shared spike interface (README) that evaluate.py and the
    later backend promotion rely on. It processes one year at a time so peak
    memory is bounded by N scenes in uint16 (one year at a time), not the whole archive.
    Returns years int [Y] (every year with >=1 scene) and dn uint16 [Y,6,H,W].
    """
    bands = meta["bands"]
    band_idx = [bands.index(b) for b in REFLECTANCE_BANDS]
    qa_idx = bands.index("qa_pixel")
    chosen = select_scenes(meta, n)
    years = np.array(sorted(chosen), dtype=np.int32)
    h, w = scenes.shape[-2:]
    dn = np.zeros((len(years), len(band_idx), h, w), dtype=np.uint16)
    for yi, year in enumerate(years):
        dn[yi] = median_year(scenes[chosen[int(year)]], band_idx, qa_idx)
    return years, dn


def self_test() -> None:
    """Check on synthetic data that clouded and fill values are ignored.

    Why: a wrong mask would silently let clouds into the median. Three scenes,
    one pixel clouded (bright 9000, cloud bit set) in scene 0: its median must
    be the median of the two clear scenes (120,130 -> 125), not 130. Also checks
    a pixel with no clear observation comes out as 0 and N-selection by cloud.
    """
    bands = REFLECTANCE_BANDS + ["qa_pixel"]
    meta = {
        "bands": bands,
        "scenes": [
            {"datetime": "2000-06-01T00:00:00Z", "cloud_cover": 5.0},
            {"datetime": "2000-07-01T00:00:00Z", "cloud_cover": 1.0},
            {"datetime": "2000-08-01T00:00:00Z", "cloud_cover": 2.0},
            {"datetime": "2001-07-01T00:00:00Z", "cloud_cover": 50.0},
        ],
    }
    s = np.zeros((4, 7, 2, 2), dtype=np.uint16)
    s[:, :6] = np.array([100, 120, 130, 140], dtype=np.uint16)[:, None, None, None]
    s[:, 6] = 1 << 6  # clear bit only -> valid
    s[0, :6, 0, 0] = 9000
    s[0, 6, 0, 0] = 1 << 3  # cloud
    s[:3, 6, 1, 1] = 1 << 4  # shadow in every 2000 scene
    years, dn = composite(s, meta, n=3)
    assert years.tolist() == [2000, 2001], years
    assert dn.shape == (2, 6, 2, 2) and dn.dtype == np.uint16
    assert (dn[0, :, 0, 0] == 125).all(), dn[0, :, 0, 0]  # median(120,130)
    assert (dn[0, :, 0, 1] == 120).all(), dn[0, :, 0, 1]  # median(100,120,130)
    assert (dn[0, :, 1, 1] == 0).all(), dn[0, :, 1, 1]  # never clear
    assert (dn[1] == 140).all()
    # N=2 must drop the cloudiest 2000 scene (index 0, cc=5) -> median(120,130).
    _, dn2 = composite(s, meta, n=2)
    assert (dn2[0, :, 0, 1] == 125).all(), dn2[0, :, 0, 1]
    # Water bit 7 must not be masked.
    s[:, 6] = (1 << 6) | (1 << 7)
    _, dn3 = composite(s, meta, n=3)
    assert (dn3[0, :, 1, 1] == 120).all()
    # median_year must match the np.nanmedian oracle exactly on random stacks
    # with masked pixels, DN 0 fill, and saturated 65535 values that are clear.
    rng = np.random.default_rng(0)
    for k in (1, 2, 3, 4, 5, 6):
        x = rng.integers(1, 20000, size=(k, 7, 40, 40)).astype(np.uint16)
        x[:, 6] = np.where(rng.random((k, 40, 40)) < 0.4, 1 << 3, 1 << 6)
        x[:, :6][rng.random((k, 6, 40, 40)) < 0.05] = 0
        x[:, :6][rng.random((k, 6, 40, 40)) < 0.05] = 65535
        bi = list(range(6))
        assert np.array_equal(median_year(x, bi, 6), reference_median_year(x, bi, 6)), k
    print("self-test OK")


def main() -> None:
    """Run all 3 places x N=2,4,6, write the 9 npz files and results.json.

    Why: gives evaluate.py identical-format outputs and records the runtime
    (composite step only, which is what the 10 s/place budget covers; load
    time is listed separately) and peak traced memory per run.
    """
    self_test()
    OUT.mkdir(parents=True, exist_ok=True)
    runs: dict[str, dict] = {}
    for place in PLACES:
        meta = json.loads((CACHE / f"{place}.json").read_text())
        t0 = time.perf_counter()
        scenes = np.load(CACHE / f"{place}.npz")["scenes"]
        load_s = time.perf_counter() - t0
        runs[place] = {"n_scenes": int(scenes.shape[0]), "load_s": round(load_s, 2)}
        for n in NS:
            tracemalloc.start()
            t0 = time.perf_counter()
            years, dn = composite(scenes, meta, n)
            secs = time.perf_counter() - t0
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            np.savez(OUT / f"{place}_n{n}.npz", years=years, dn=dn)
            empty = float((dn == 0).all(axis=1).mean())
            runs[place][f"n{n}"] = {
                "runtime_s": round(secs, 2),
                "peak_mem_mb_composite": round(peak / 2**20, 1),
                "years": int(len(years)),
                "nodata_pixel_frac": round(empty, 4),
            }
            print(place, n, runs[place][f"n{n}"], flush=True)
    results = {
        "approach": "median",
        "runs": runs,
        "input_array_mb": {p: round(runs[p]["n_scenes"] * 7 * 334 * 334 * 2 / 2**20, 1) for p in PLACES},
        "notes": [
            "runtime_s covers composite() only (input already in RAM); load_s is the npz read.",
            "peak_mem_mb_composite is tracemalloc peak during composite(), excluding the input array (see input_array_mb).",
            "median_year sorts in uint16 with a 65535 sentinel and counts valid obs from the mask (65535 is also a real saturated DN in the data); self_test checks it equals an np.nanmedian float32 oracle exactly.",
            "Even clear counts average the two middle values, so the composite pixel can be a value no real date had; bands may also come from different dates.",
            "Reflectance DN 0 is treated as fill in addition to the QA rule; valid medians are clipped to >= 1 to keep 0 = nodata.",
            "nodata_pixel_frac = fraction of pixels with no clear observation, averaged over years.",
        ],
        "problems": [
            "First version used np.nanmedian: 12-19 s per place (masked-array fallback for short axes), over the 10 s budget. A float32 sort-based median got ~8-11 s; the uint16 sort version is used instead.",
        ],
    }
    RESULTS.write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    if "--test" in sys.argv:
        self_test()
    else:
        main()
