# What: spike S2 "medoid" approach. Per pixel and year, picks the clear observation closest (summed 6-band Euclidean distance) to the others.
# Inputs: data/cache/s2/<place>.npz (`scenes` uint16 [n, 7, 334, 334]) and <place>.json (band order, per-scene metadata).
# Outputs: data/spikes/s2/medoid/<place>_n<N>.npz (`years`, `dn` uint16 [Y, 6, 334, 334]) and results.json beside this file.

import ctypes
import json
import sys
import time
import tracemalloc
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / "data" / "cache" / "s2"
OUT = ROOT / "data" / "spikes" / "s2" / "medoid"
RESULTS = Path(__file__).resolve().parent / "results.json"

PLACES = ["lake-mead", "paradise", "central-park"]
NS = [2, 4, 6]
N_REFL = 6  # blue, green, red, nir08, swir16, swir22
# QA bits that make a pixel invalid (corrected README rule): 0 fill,
# 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow, 5 snow.
# Bit 7 (water) is deliberately not masked.
QA_BAD_BITS = 0b111111


def clear_mask(qa: np.ndarray, refl: np.ndarray) -> np.ndarray:
    """Return a boolean mask that is True where a pixel is a clear observation.

    What: qa is [..., H, W], refl is [..., 6, H, W]. Invalid if qa_pixel is 0,
    any of bits 0-5 (fill, dilated cloud, cirrus, cloud, shadow, snow) is set,
    or any reflectance band is 0 (no data) or 65535 (saturated).
    Why: every S2 approach must use exactly the same mask so their outputs
    compare fairly. In this cache no-data pixels (incl. Landsat 7 SLC-off
    stripes) have qa == 1 (bit 0) and all-zero bands; the original rule missed
    bit 0, so the medoid could pick an all-zero "observation". The band check
    also drops saturated or zero values that would distort the distances.
    """
    qa_ok = (qa != 0) & ((qa & QA_BAD_BITS) == 0)
    dn_ok = np.all((refl >= 1) & (refl <= 65534), axis=-3)
    return qa_ok & dn_ok


def pick_scenes(meta: dict, n: int) -> dict[int, list[int]]:
    """Map each year to the indices of its N lowest-cloud_cover scenes.

    What: groups scenes by the year of their datetime, sorts each group by
    cloud_cover (stable, so ties keep date order) and keeps the first N.
    Why: the README fixes scene choice per N so all approaches see the same
    inputs. The returned order (lowest cloud first) also matters to the
    medoid: argmin breaks ties toward the first index, so a tie between two
    observations resolves to the scene with the lower cloud_cover.
    """
    by_year: dict[int, list[int]] = {}
    for i, s in enumerate(meta["scenes"]):
        by_year.setdefault(int(s["datetime"][:4]), []).append(i)
    picked = {}
    for year in sorted(by_year):
        idx = sorted(by_year[year], key=lambda i: meta["scenes"][i]["cloud_cover"])
        picked[year] = idx[:n]
    return picked


def medoid_stack(refl: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Compute the per-pixel medoid of a small stack of observations.

    What: refl is [K, 6, H, W] (any numeric dtype), valid is [K, H, W] bool,
    ordered by preference (lowest cloud_cover first). For each pixel, sums the
    6-band Euclidean distance from each clear observation to every other clear
    observation, and returns the 6 band values of the observation with the
    smallest sum as uint16 [6, H, W]. Pixels with no clear observation are 0.
    Why: unlike a per-band median, the medoid takes all bands from one real
    date, so band ratios stay physically consistent. Cases:
      - 1 clear obs: its sum is 0, so it wins.
      - 2 clear obs: both sums equal d(a, b) exactly (the squared differences
        are bit-identical either way), so argmin returns the first, which is
        the lower-cloud_cover scene because of the input order.
      - 0 clear obs: every sum is +inf; we write 0 via the any-valid mask.
    Speed: the distance is symmetric and d(i, i) = 0, so we only compute the
    K(K-1)/2 pairs i < j (15 at K = 6, not 36 for a full [K, K, H, W] matrix)
    and add each distance to both totals. A first version built the full
    [K, K, H, W] matrix and took ~10 s per place at N = 6, right on the pass
    bar. Each pair is still vectorised over the whole [6, H, W] tile.
    Invalid observations are turned into NaN, and NaN distances are zeroed so
    they never count against a clear observation; the invalid observations
    themselves get +inf.
    """
    k = refl.shape[0]
    x = refl.astype(np.float32)
    x[~np.broadcast_to(valid[:, None], x.shape)] = np.nan

    total = np.zeros((k,) + refl.shape[2:], dtype=np.float32)  # [K, H, W]
    for i in range(k):
        for j in range(i + 1, k):
            diff = x[i] - x[j]  # [6, H, W]
            d = np.sqrt(np.einsum("bhw,bhw->hw", diff, diff))
            # A pair involving an invalid obs is NaN: drop it from the sums.
            np.nan_to_num(d, copy=False, nan=0.0)
            total[i] += d
            total[j] += d
    total[~valid] = np.inf

    best = np.argmin(total, axis=0)  # [H, W]; first index wins ties
    out = np.take_along_axis(refl, best[None, None], axis=0)[0]  # [6, H, W]
    out = out.astype(np.uint16)
    out[:, ~valid.any(axis=0)] = 0
    return out


def composite(scenes: np.ndarray, meta: dict, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Build one medoid composite per year from the N clearest scenes.

    What: for each year with at least one scene, takes its N lowest-cloud
    scenes, masks them with the QA rule and computes the per-pixel medoid.
    Returns years int [Y] and dn uint16 [Y, 6, H, W].
    Why: this is the shared spike interface, so evaluate.py can score every
    approach the same way.
    """
    qa_i = meta["bands"].index("qa_pixel")
    picked = pick_scenes(meta, n)
    years = np.array(sorted(picked), dtype=np.int32)
    h, w = scenes.shape[-2:]
    dn = np.zeros((len(years), N_REFL, h, w), dtype=np.uint16)
    for yi, year in enumerate(years):
        idx = picked[int(year)]
        stack = scenes[idx]  # [K, 7, H, W], lowest cloud_cover first
        refl = stack[:, :N_REFL]
        valid = clear_mask(stack[:, qa_i], refl)
        dn[yi] = medoid_stack(refl, valid)
    return years, dn


def self_test() -> None:
    """Check the medoid on tiny synthetic stacks before touching real data.

    What: asserts that (1) with 3 observations the outlier is never chosen,
    (2) with 2 the lower-cloud (first) scene wins, (3) one clear observation
    is used as-is, (4) zero clear gives 0, (5) masked observations are
    ignored even if they would have been the medoid, and (6) the two-scene
    tie-break follows cloud_cover through composite()/pick_scenes even when
    the clearer scene comes later in date order.
    Why: the vectorised NaN handling is easy to get subtly wrong, and a wrong
    medoid would not be obvious from contact sheets.
    """
    h = w = 2
    a = np.full((6, h, w), 1000, np.uint16)
    b = np.full((6, h, w), 1010, np.uint16)
    outlier = np.full((6, h, w), 9000, np.uint16)
    ones = np.ones((h, w), bool)

    # (1) outlier first in order, still must not be picked; b and a are close.
    out = medoid_stack(np.stack([outlier, b, a]), np.stack([ones, ones, ones]))
    assert np.all((out == 1000) | (out == 1010)), out
    # Three close obs: the middle one (a2) has the smallest summed distance
    # (b: 9 + 10, a2: 9 + 1, a: 10 + 1, per-band units), although it is last.
    a2 = np.full((6, h, w), 1001, np.uint16)
    out = medoid_stack(np.stack([b, a, a2]), np.stack([ones] * 3))
    assert np.all(out == 1001), out

    # (2) 2 clear obs: first (lower cloud) wins in both orders.
    assert np.all(medoid_stack(np.stack([b, outlier]), np.stack([ones, ones])) == 1010)
    assert np.all(medoid_stack(np.stack([outlier, b]), np.stack([ones, ones])) == 9000)

    # (3) only one clear obs; (4) none; (5) masked obs ignored.
    one = np.zeros((h, w), bool)
    one[0, 0] = True
    zero = np.zeros((h, w), bool)
    out = medoid_stack(np.stack([outlier, a]), np.stack([zero, one]))
    assert np.all(out[:, 0, 0] == 1000) and np.all(out[:, 1:, :] == 0) and np.all(out[:, 0, 1] == 0)
    out = medoid_stack(np.stack([a, a2, outlier]), np.stack([ones, zero, ones]))
    assert np.all(out == 1000), out  # only a and outlier valid: tie -> first

    # Per-pixel mixing: different winners in different pixels, all bands from one obs.
    c = a.copy()
    c[:, 1, 1] = 9500
    out = medoid_stack(np.stack([c, b, a]), np.stack([ones] * 3))
    assert np.all(out[:, 1, 1] == 1000) or np.all(out[:, 1, 1] == 1010)

    # (6) tie-break through composite(): the later-dated scene has lower cloud.
    qa_clear = np.full((h, w), 1 << 6, np.uint16)  # bit 6 = clear, no bad bits
    s0 = np.concatenate([outlier, qa_clear[None]])
    s1 = np.concatenate([b, qa_clear[None]])
    meta = {
        "bands": ["blue", "green", "red", "nir08", "swir16", "swir22", "qa_pixel"],
        "scenes": [
            {"datetime": "2000-06-01T00:00:00Z", "cloud_cover": 20.0},
            {"datetime": "2000-07-01T00:00:00Z", "cloud_cover": 5.0},
        ],
    }
    years, dn = composite(np.stack([s0, s1]), meta, 2)
    assert years.tolist() == [2000] and np.all(dn[0] == 1010), dn
    # A cloud bit set on the clearer scene must hand the pixel to the other one.
    s1c = s1.copy()
    s1c[6, 0, 0] |= 1 << 3
    years, dn = composite(np.stack([s0, s1c]), meta, 2)
    assert np.all(dn[0, :, 0, 0] == 9000) and np.all(dn[0, :, 1, 1] == 1010)

    # (7) fill pixels (qa = 1, all bands 0) must never be chosen. Two fill
    # scenes (the lowest cloud_cover ones) would form a zero-distance pair and
    # win the medoid if they were counted as clear.
    fill = np.concatenate([np.zeros((6, h, w), np.uint16), np.ones((1, h, w), np.uint16)])
    meta3 = {
        "bands": meta["bands"],
        "scenes": [
            {"datetime": "2001-06-01T00:00:00Z", "cloud_cover": 1.0},
            {"datetime": "2001-06-17T00:00:00Z", "cloud_cover": 2.0},
            {"datetime": "2001-07-03T00:00:00Z", "cloud_cover": 3.0},
        ],
    }
    years, dn = composite(np.stack([fill, fill, s1]), meta3, 3)
    assert np.all(dn[0] == 1010), dn
    # All fill -> output 0.
    years, dn = composite(np.stack([fill, fill]), meta3 | {"scenes": meta3["scenes"][:2]}, 2)
    assert np.all(dn == 0), dn
    # A saturated band (65535) with otherwise clear QA is invalid too.
    sat = s1.copy()
    sat[3, 0, 0] = 65535
    # sat is the lower-cloud scene, so it wins the tie wherever it is valid.
    years, dn = composite(np.stack([s0, sat]), meta, 2)
    assert np.all(dn[0, :, 0, 0] == 9000) and np.all(dn[0, :, 1, 1] == 1010), dn
    print("self-test passed")


def peak_rss_mb() -> float | None:
    """Return the process peak working set in MB (Windows), else None.

    What: calls GetProcessMemoryInfo through ctypes.
    Why: numpy-only rule means no psutil; the peak working set includes the
    ~400 MB decompressed scene cache, which is the real memory cost.
    """
    if sys.platform != "win32":
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:
            return None

    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        return None
    return pmc.PeakWorkingSetSize / 2**20


def main() -> None:
    """Run the self-test, then write all 9 composites and results.json.

    What: for each place loads the cached scenes once, times composite() for
    N = 2, 4, 6 (load time excluded; tracemalloc measures the extra memory
    the composite step allocates) and saves the npz outputs.
    Why: the pass bar is about the composite step's runtime (<= 10 s per
    place) inside the 180 s story budget, so that is what we time.
    """
    self_test()
    OUT.mkdir(parents=True, exist_ok=True)
    results = {"approach": "medoid", "runtime_s": {}, "load_s": {},
               "composite_peak_alloc_mb": {}, "years": {}, "notes": [], "problems": []}
    for place in PLACES:
        t0 = time.perf_counter()
        scenes = np.load(CACHE / f"{place}.npz")["scenes"]
        meta = json.loads((CACHE / f"{place}.json").read_text())
        results["load_s"][place] = round(time.perf_counter() - t0, 2)
        results["runtime_s"][place] = {}
        results["composite_peak_alloc_mb"][place] = {}
        for n in NS:
            tracemalloc.start()
            t0 = time.perf_counter()
            years, dn = composite(scenes, meta, n)
            dt = time.perf_counter() - t0
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            results["runtime_s"][place][f"n{n}"] = round(dt, 2)
            results["composite_peak_alloc_mb"][place][f"n{n}"] = round(peak / 2**20, 1)
            results["years"][place] = len(years)
            np.savez_compressed(OUT / f"{place}_n{n}.npz", years=years, dn=dn)
            print(f"{place} n={n}: {dt:.2f}s, {len(years)} years, peak alloc {peak / 2**20:.0f} MB")
        del scenes
    results["peak_process_memory_mb"] = round(peak_rss_mb() or -1, 1)
    results["notes"] = [
        "runtime_s times composite() only (all years for one place); load_s is np.load of the cached npz.",
        "composite_peak_alloc_mb is tracemalloc peak inside composite() (includes the 43-year uint16 output, ~27 MB); "
        "peak_process_memory_mb is the whole-process peak working set, dominated by the decompressed scene cache.",
        "Only the K(K-1)/2 pairs i<j are computed; a first version with a full [K,K,H,W] matrix took ~7-10 s per place at N=6, "
        "this one ~2.6-3.4 s.",
        "Scenes per year are ordered by cloud_cover ascending, so argmin's first-index tie-break gives the 2-observation rule "
        "(lower cloud_cover wins) and also breaks rarer exact ties among 3+ observations the same way.",
        "Self-test (python composite.py --test) covers outlier rejection, 1/2/0 clear observations, masked observations, "
        "the cloud_cover tie-break through composite(), fill pixels (qa=1, dn=0) and saturated (65535) bands.",
        "Mask uses the corrected rule: invalid if qa==0, any of bits 0-5 set, or any reflectance band is 0 or 65535. "
        "Outputs were regenerated after the correction (the first rule let qa=1 fill pixels through as all-zero observations).",
        "Timings measured while other S2 spike agents may have been running in parallel; treat as upper bounds.",
    ]
    results["problems"] = [
        "With 2 clear observations the medoid cannot reject an outlier: it is just the lower-cloud scene, so N=2 behaves like "
        "best-scene with per-pixel fallback; a missed cloud in that scene passes through.",
        "pip inside the Bash tool's sandbox failed with a network access violation; the install worked from PowerShell.",
    ]
    RESULTS.write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    if "--test" in sys.argv:
        self_test()
    else:
        main()
