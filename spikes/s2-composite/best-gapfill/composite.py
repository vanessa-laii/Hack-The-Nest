# Spike S2 approach "best-gapfill": one composite per year = the clearest scene, holes filled from the next-clearest.
# Inputs: data/cache/s2/<place>.npz (scenes uint16 [n,7,334,334]) + <place>.json (band order, per-scene metadata).
# Outputs: data/spikes/s2/best-gapfill/<place>_n<N>.npz (years, dn uint16 [Y,6,334,334]) and results.json here.
#
# Why this approach: every output pixel is one real observation (all 6 bands from one date),
# and most pixels come from a single scene, so a year looks like one coherent image rather
# than a statistical blend. The risk it is tested for: seams where a fill scene from a
# different date sits next to the base scene, and clouds the QA mask misses.
from __future__ import annotations

import ctypes
import json
import sys
import time
import tracemalloc
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / "data" / "cache" / "s2"
OUT_DIR = ROOT / "data" / "spikes" / "s2" / "best-gapfill"
RESULTS = Path(__file__).resolve().parent / "results.json"
PLACES = ["lake-mead", "paradise", "central-park"]
NS = [2, 4, 6]
QA_BAND = 6
# README rule: bits 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow, 5 snow. Bit 7 (water) stays valid.
QA_BAD_BITS = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5)
# Bit 0 is the C2 QA_PIXEL "fill" flag: nodata pixels have qa == 1 and all reflectance bands 0.
# The README rule only treats qa == 0 as fill, so it misses these. FIX_VARIANT also masks bit 0.
QA_FILL_BIT = 1 << 0
FIX_SUBDIR = "fill-bit0"
# Filled in after inspecting the run (see results.json); kept here so a rerun reproduces them.
NOTES: list[str] = [
    "Ranking uses in-area QA clear fraction; ties keep cloud_cover order. All 6 bands of a pixel are copied from one scene under one mask.",
    "Runtime well under the 10 s bar: 0.4-1.7 s per place for any N (composite only, input in memory); loading the compressed cache npz (2-5 s) costs more than compositing.",
    "Memory: input array ~435 MB per place (258 scenes x 7 bands); compositing adds ~70 MB. Process peak ~575 MB. In the backend only ~N x 43 scenes would be held.",
    "README mask: one scene supplies almost every year at lake-mead and paradise (1.0-1.14 scenes/year), but 13-37 % of pixels end up 0 because fill pixels (qa == 1) are ranked as clear and pasted in; see problems.",
    "fill_bit0 variant (data/spikes/s2/best-gapfill/fill-bit0/): unfilled pixels drop to 0.3-0.6 % at N=4 and ~0-0.2 % at N=6; 1.2-1.4 scenes/year at lake-mead and paradise, ~3-3.7 at central-park.",
    "central-park uses ~3 scenes per year even when the base scene is >97 % clear: the extra scenes fill scattered small masked patches (dilated cloud, shadow), so speckled seams from other dates are possible; evaluate.py should look at this.",
    "Per-year scene ids, in-area clear fraction and fraction supplied are in scene_usage.<variant>.<place>.<nN>.per_year.",
]
PROBLEMS: list[str] = [
    "README mask rule is wrong for this cache: C2 QA_PIXEL marks fill with bit 0 (qa == 1, all reflectance bands 0), not qa == 0. Measured share of qa == 1 pixels: lake-mead 19.5 %, paradise 37.0 %, central-park 11.5 % (qa == 0 is only 0-2.3 %). Under the README rule these count as clear, so best-gapfill ranks edge-of-swath scenes as 100 % clear and writes 0s instead of filling them. Recommend adding bit 0 to the shared mask; median would otherwise average in 0 DNs.",
    "After masking bit 0, no remaining clear pixel has any reflectance band equal to 0, so the corrected mask is sufficient.",
    "Main output files follow the README rule exactly so evaluate.py compares like with like; the fill-bit0 variant is written to a subfolder and is not one of the 9 required files.",
]


def clear_mask(qa: np.ndarray, mask_fill_bit: bool = False) -> np.ndarray:
    """Returns True where a pixel is a clear observation.

    Why: all three S2 approaches must mask identically so the evaluation compares
    compositing methods, not masking choices, so the default is exactly the README rule
    (qa == 0 is fill; bits 1-5 are cloud, cirrus, shadow, snow). The spike found that
    in this cache nodata pixels carry qa == 1 (bit 0, the C2 fill flag), not 0, so the
    README rule counts them as clear. mask_fill_bit=True also rejects bit 0; it is
    used only for the separate fill-bit0 variant so the main outputs stay comparable.
    """
    bad = QA_BAD_BITS | (QA_FILL_BIT if mask_fill_bit else 0)
    return (qa != 0) & ((qa & bad) == 0)


def pick_scenes(meta: dict, n: int) -> dict[int, list[int]]:
    """Groups scene indices by year and keeps the n lowest scene-wide cloud_cover in each.

    Why: the README fixes scene choice for N so every approach sees the same inputs.
    Ties on cloud_cover are broken by date order (the cache order) so the choice is
    deterministic across runs and approaches.
    """
    by_year: dict[int, list[int]] = defaultdict(list)
    for i, s in enumerate(meta["scenes"]):
        by_year[int(s["datetime"][:4])].append(i)
    return {y: sorted(idx, key=lambda i: (meta["scenes"][i]["cloud_cover"], i))[:n]
            for y, idx in sorted(by_year.items())}


def gapfill_year(stack: np.ndarray, ids: list[str], mask_fill_bit: bool = False) -> tuple[np.ndarray, list[dict]]:
    """Builds one year's composite from that year's chosen scenes, best-first gap filling.

    stack: uint16 [k, 7, H, W] for the k chosen scenes; ids: their scene ids.
    Returns dn uint16 [6, H, W] and a per-scene usage list (rank order).

    How: rank scenes by their clear fraction *inside the area* (from the QA mask, not
    the scene-wide eo:cloud_cover, which describes the whole 185 km scene and can be
    badly wrong for a 10 km crop, e.g. a partial-coverage edge scene reporting 0 %).
    Take the clearest scene, then for every pixel still unfilled copy all 6 bands from
    the next-clearest scene that is clear there. Copying the whole band vector under one
    mask keeps every pixel a single real observation, so band ratios (NDVI, NDWI) stay
    physically consistent. Pixels clear in no scene stay 0, which downstream reads as
    "no clear observation".
    """
    k, _, h, w = stack.shape
    clear = clear_mask(stack[:, QA_BAND], mask_fill_bit)        # [k, H, W]
    frac = clear.reshape(k, -1).mean(axis=1)
    # Stable sort on -frac: equal fractions keep cloud_cover order from pick_scenes.
    order = np.argsort(-frac, kind="stable")

    dn = np.zeros((6, h, w), dtype=np.uint16)
    filled = np.zeros((h, w), dtype=bool)
    usage = []
    for r in order:
        take = clear[r] & ~filled                               # clear here and not yet filled
        n_take = int(take.sum())
        if n_take:
            dn[:, take] = stack[r, :6][:, take]                 # all 6 bands from this one scene
            filled |= take
        usage.append({"id": ids[r], "clear_frac": round(float(frac[r]), 4),
                      "supplied_frac": round(n_take / (h * w), 4)})
        if filled.all():
            # Every pixel has a value; lower-ranked scenes would contribute nothing.
            # They are still listed (supplied 0) so the record shows what was available.
            for r2 in order[len(usage):]:
                usage.append({"id": ids[r2], "clear_frac": round(float(frac[r2]), 4),
                              "supplied_frac": 0.0})
            break
    return dn, usage


def composite(scenes: np.ndarray, meta: dict, n: int, log: list | None = None,
              mask_fill_bit: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Spike interface: returns (years int [Y], dn uint16 [Y, 6, H, W]) for one place and N.

    Why the optional log: the task asks which scenes fed each year and how much each
    supplied; passing a list collects that without changing the shared signature.
    mask_fill_bit switches to the corrected mask (see clear_mask); default is README.
    Only years with at least one scene are output (README), in ascending order.
    """
    picks = pick_scenes(meta, n)
    years = np.array(sorted(picks), dtype=np.int32)
    _, _, h, w = scenes.shape
    dn = np.zeros((len(years), 6, h, w), dtype=np.uint16)
    for yi, y in enumerate(years):
        idx = picks[int(y)]
        # Fancy indexing copies only this year's k scenes, keeping memory small.
        dn[yi], usage = gapfill_year(scenes[idx], [meta["scenes"][i]["id"] for i in idx], mask_fill_bit)
        if log is not None:
            log.append({"year": int(y), "scenes": usage,
                        "unfilled_frac": round(float((dn[yi, 0] == 0).mean()), 4)})
    return years, dn


def peak_working_set_mb() -> float | None:
    """Returns the process's peak working set in MB on Windows, else None.

    Why: tracemalloc only sees Python/numpy allocations; the OS peak also includes
    the npz decompression buffers, which is what would matter inside the backend.
    """
    if sys.platform != "win32":
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
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        return None
    return round(pmc.PeakWorkingSetSize / 2**20, 1)


def self_test() -> None:
    """Synthetic check run before the real data: a clouded patch must be filled from scene 2.

    Why: proves the three properties the method depends on, on data whose answer is known:
    (1) ranking uses in-area QA, not cloud_cover (scene A claims 90 % cloud but is the
    clearer one here); (2) the patch is filled from the other scene with all 6 bands from
    that scene; (3) pixels clear nowhere stay 0.
    """
    h = w = 10
    a = np.zeros((7, h, w), np.uint16); b = np.zeros((7, h, w), np.uint16)
    a[:6] = np.arange(1, 7, dtype=np.uint16)[:, None, None] * 100      # A bands = 100..600
    b[:6] = np.arange(1, 7, dtype=np.uint16)[:, None, None] * 1000     # B bands = 1000..6000
    a[QA_BAND] = 1 << 6; b[QA_BAND] = 1 << 6                            # bit 6 = clear
    a[QA_BAND, 2:5, 2:5] = (1 << 3) | (1 << 1)                          # cloud patch in A (9 px)
    b[QA_BAND, :, 5:] = 1 << 3                                          # B clouded on its right half
    a[QA_BAND, 0, 0] = 1 << 4; b[QA_BAND, 0, 0] = 0                     # (0,0) bad in both
    a[QA_BAND, 1, 1] = (1 << 7) | (1 << 6)                              # water bit must stay valid
    meta = {"scenes": [{"id": "A", "datetime": "2000-07-01", "cloud_cover": 90.0},
                       {"id": "B", "datetime": "2000-08-01", "cloud_cover": 5.0}]}
    log: list = []
    years, dn = composite(np.stack([a, b]), meta, 2, log)
    assert years.tolist() == [2000] and dn.shape == (1, 6, h, w)
    d = dn[0]
    assert log[0]["scenes"][0]["id"] == "A", "ranking must use in-area QA, not cloud_cover"
    assert (d[:, 2:5, 2:5] == b[:6, 2:5, 2:5]).all(), "patch must be filled from B, all bands"
    assert (d[:, 1, 1] == a[:6, 1, 1]).all(), "water pixel must come from A"
    assert (d[:, 0, 0] == 0).all(), "pixel clear nowhere must stay 0"
    rest = np.ones((h, w), bool); rest[2:5, 2:5] = False; rest[0, 0] = False
    assert (d[:, rest] == a[:6, rest]).all(), "everything else must come from A"
    assert log[0]["scenes"][1]["supplied_frac"] == 0.09
    # Patch overlapping B's cloud: A cloud at (2..4, 5..7) region tested via a second case.
    a2 = a.copy(); a2[QA_BAND, 7, 7] = 1 << 3                           # A cloud where B is also cloudy
    _, dn2 = composite(np.stack([a2, b]), meta, 2)
    assert (dn2[0, :, 7, 7] == 0).all(), "cloud in both scenes must stay 0"
    # Fill-bit case: A has qa == 1 (C2 nodata, bands 0) on row 9. README mask keeps it
    # (so 0s are pasted in); the fixed mask must fill that row from B where B is clear.
    a3 = a.copy(); a3[:6, 9, :] = 0; a3[QA_BAND, 9, :] = 1
    _, dn3 = composite(np.stack([a3, b]), meta, 2)
    assert (dn3[0, :, 9, :] == 0).all(), "README rule treats qa == 1 as clear (documents the gap)"
    _, dn4 = composite(np.stack([a3, b]), meta, 2, mask_fill_bit=True)
    assert (dn4[0, :, 9, :5] == b[:6, 9, :5]).all() and (dn4[0, :, 9, 5:] == 0).all()
    print("self-test passed")


def summarise(log: list) -> dict:
    """Condenses one run's per-year scene log into the numbers worth reading.

    Why: per_year is long (43 years x up to 6 scenes); the summary shows at a glance
    how often one scene covers the whole area and how much stays unfilled.
    """
    base = [y["scenes"][0]["supplied_frac"] for y in log]
    n_used = [sum(s["supplied_frac"] > 0 for s in y["scenes"]) for y in log]
    return {
        "years": len(log),
        "mean_base_scene_frac": round(float(np.mean(base)), 3),
        "min_base_scene_frac": round(float(np.min(base)), 3),
        "mean_scenes_used_per_year": round(float(np.mean(n_used)), 2),
        "years_single_scene": int(sum(u == 1 for u in n_used)),
        "mean_unfilled_frac": round(float(np.mean([y["unfilled_frac"] for y in log])), 4),
        "years_unfilled_gt_5pct": [y["year"] for y in log if y["unfilled_frac"] > 0.05],
        "per_year": log,
    }


def main() -> None:
    """Runs the self-test, then writes the 9 composites (README mask), the 9 fill-bit0
    variants, and results.json.

    Why load once per place: the npz is compressed and decompression costs more than
    compositing; the backend will receive arrays in memory, so only the composite step
    is timed against the 10 s pass bar, and load time is reported separately.
    Why two variants: the README mask lets C2 fill pixels (qa == 1) through; the main
    files keep the shared rule for a fair comparison, the variant shows the fix's effect.
    """
    self_test()
    variants = [("readme_mask", False, OUT_DIR), ("fill_bit0_mask", True, OUT_DIR / FIX_SUBDIR)]
    for _, _, d in variants:
        d.mkdir(parents=True, exist_ok=True)
    tracemalloc.start()
    runtime: dict = {v: {} for v, _, _ in variants}
    usage: dict = {v: {} for v, _, _ in variants}
    load_s: dict = {}
    input_mb: dict = {}
    tm_extra = 0
    for place in PLACES:
        meta = json.loads((CACHE / f"{place}.json").read_text())
        t0 = time.perf_counter()
        scenes = np.load(CACHE / f"{place}.npz")["scenes"]
        load_s[place] = round(time.perf_counter() - t0, 2)
        input_mb[place] = round(scenes.nbytes / 2**20, 1)
        assert meta["bands"][QA_BAND] == "qa_pixel"
        for v, fix, out in variants:
            runtime[v][place] = {}
            usage[v][place] = {}
            for n in NS:
                before = tracemalloc.get_traced_memory()[0]
                tracemalloc.reset_peak()
                log: list = []
                t0 = time.perf_counter()
                years, dn = composite(scenes, meta, n, log, mask_fill_bit=fix)
                runtime[v][place][f"n{n}"] = round(time.perf_counter() - t0, 3)
                # Extra memory the composite step needs on top of the already-loaded input.
                tm_extra = max(tm_extra, tracemalloc.get_traced_memory()[1] - before)
                np.savez_compressed(out / f"{place}_n{n}.npz", years=years, dn=dn)
                usage[v][place][f"n{n}"] = summarise(log)
                del dn
                print(f"{v} {place} n={n}: {len(years)} years, {runtime[v][place][f'n{n}']} s")
        del scenes

    def brief(v):
        return {pl: {n: {k: s[k] for k in ("mean_scenes_used_per_year", "years_single_scene",
                                           "mean_unfilled_frac")} for n, s in d.items()}
                for pl, d in usage[v].items()}

    results = {
        "approach": "best-gapfill",
        "runtime_s": runtime["readme_mask"],
        "runtime_s_fill_bit0_variant": runtime["fill_bit0_mask"],
        "runtime_note": "composite step only, input already in memory; npz load time listed separately",
        "load_s": load_s,
        "peak_memory_mb": {"input_array": input_mb,
                           "composite_step_extra_tracemalloc": round(tm_extra / 2**20, 1),
                           "process_peak_working_set": peak_working_set_mb()},
        "outputs": {"main (README mask, for evaluate.py)": str(OUT_DIR.relative_to(ROOT)),
                    "fill_bit0 variant": str((OUT_DIR / FIX_SUBDIR).relative_to(ROOT))},
        "usage_brief": {v: brief(v) for v, _, _ in variants},
        "notes": NOTES,
        "problems": PROBLEMS,
        "scene_usage": usage,
    }
    RESULTS.write_text(json.dumps(results, indent=2))
    print(json.dumps({k: v for k, v in results.items() if k != "scene_usage"}, indent=2))


if __name__ == "__main__":
    main()
