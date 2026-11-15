# Spike S3, approach "window-means": finds lasting changes by comparing k-year means before/after.
# Inputs: a story folder data/stories/<id>/ with imagery.json and composites.npz.
# Outputs: detect(story_dir) → 0–5 changes[] items of contracts/story.schema.json.
#
# How it works:
#   1. Read the yearly series (ndvi_mean, water_frac, built_frac) and the per-pixel composites.
#   2. For each series, slide a split year and compare the mean of the k years before with the
#      k years after. A split counts when the jump beats both the series' own year-to-year
#      noise (robust MAD of first differences) and a physical floor for that metric.
#   3. Merge neighbouring splits into one change, trim the window smear, measure start/end years.
#   4. Keep it only if it lasts (FR-22) and shows up in the pixels (a real place on the map).
#   4b. Run the same window test per pixel on built-up: one connected patch ≥ 4 ha that fully
#      flipped is a change too (a factory torn down or a bridge plaza is invisible area-wide).
#   5. Name the kind; apply the burn test (dNBR), the shrinking-lake rule and the desert rule.
#   6. Find where: per-pixel before/after difference, largest connected patch → lon/lat bbox
#      and a plain-words description. Flag sensor switches (DR-6). Keep the 5 largest.
from __future__ import annotations

import json
import sys
import warnings
from collections import deque
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "backend"))

from app.pipeline.metrics import indices  # noqa: E402
from app.pipeline.types import NIR, SR_OFFSET, SR_SCALE, SWIR2, YearComposite  # noqa: E402

# --- Thresholds (each one is a physical or statistical choice, not a fit to the gold set) ---

WINDOW_YEARS = 3        # k: 3 summers per side average out one odd summer but still date a change

# A split counts only if the jump is this many standard errors of the window-mean difference.
# Noise per year = 1.4826 × MAD(first differences) / √2 (robust: one odd summer can't inflate it);
# the difference of two k-year means then has standard error noise × √(2/k).
NOISE_Z = 4.0

# Physical floors on the area-wide jump, so a very quiet series can't report a trivial change.
MIN_JUMP = {
    "ndvi_mean": 0.05,   # ≈ a tenth of a 6 km area going from full green (0.6) to bare (0.1)
    "water_frac": 0.02,  # 2% of the area ≈ 0.7 km² at 6 km, a lake shore visibly moving
    "built_frac": 0.03,  # 3% of the area ≈ 1 km², a new neighbourhood or a large site cleared
}

# Per-pixel tests for "this pixel changed between the before and after windows".
PIXEL_NDVI_JUMP = 0.15   # ≈ 4× the noise of a 3-summer NDVI mean (single-summer noise ~0.06)
PIXEL_CLASS_JUMP = 0.5   # water / built: the pixel's class flipped in at least half the summers
MIN_REGION_SHARE = 0.005  # 0.5% of the area (≈ 0.18 km² at 6 km): smaller is speckle, not change

# Local layer: a single connected patch whose built-up class flipped counts as a change on its
# own when it covers ≥ 4 ha (≈ 44 pixels of 30 m; a big-box store with its car park, a
# bridge plaza). That is about twice the largest patch the control (Central Park) shows at
# any split (23 pixels ≈ 2 ha), so ordinary city churn stays below it.
LOCAL_MIN_PATCH_HA = 4.0
# ... where "flipped" means a full flip: built-up in none of the 3 summers before and in all 3
# after (or the reverse). Stricter than the 0.5 used to draw a region, because here a patch
# alone has to prove the change. (Allowing 2-of-3 let the control reach 111-pixel patches.)
LOCAL_CLASS_JUMP = 0.99

PERSIST_MIN_YEARS = 2   # FR-22: the new level must hold ≥ 2 years after the change

# Burn: dNBR = NBR before − NBR after over the changed pixels (USGS: > 0.27 is moderate severity).
BURN_DNBR_MIN = 0.27
BURN_MAX_YEARS = 2       # fires are sudden: the drop happens within two summers

# Shrinking-lake rule: a built-up rise whose new "built" pixels were mostly water before.
SHORE_WATER_SHARE = 0.5
# Desert rule: below this mean NDVI the land is bare/sparse, and the built-up test cannot tell
# bare soil from roofs, so "built-up falls while greenness rises" means desert became suburb.
DESERT_NDVI_MAX = 0.2
# ... and "greenness rises" = the pixels that stopped looking built gained ≥ 0.05 NDVI on average
# (lawns, trees and parks between houses; a third of the per-pixel NDVI test).
DESERT_GREENING_MIN = 0.05

MAX_CHANGES = 5          # HL-2

# Landsat sensor families: a change next to a switch between them may be an artefact (DR-6).
SENSOR_FAMILY = {"Landsat 4": "TM", "Landsat 5": "TM", "Landsat 7": "ETM+",
                 "Landsat 8": "OLI", "Landsat 9": "OLI"}

METRICS = ["ndvi_mean", "water_frac", "built_frac"]


# --- Step 1: inputs ---

def _load(story_dir: Path) -> tuple[dict, np.ndarray, dict[str, np.ndarray]]:
    """imagery.json, the kept years, and per-pixel stacks [Y, H, W] for the kept years:
    land NDVI, water (0/1), built (0/1), NBR. NaN marks holes."""
    imagery = json.loads((story_dir / "imagery.json").read_text(encoding="utf-8"))
    kept_years = [row["year"] for row in imagery["metrics"]["series"]]
    npz = np.load(story_dir / "composites.npz")
    npz_years = [int(y) for y in npz["years"]]
    dn_all = npz["dn"]

    stacks = {"ndvi": [], "water": [], "built": [], "nbr": []}
    for year in kept_years:
        dn = dn_all[npz_years.index(year)]
        ix = indices(YearComposite(year=year, dn=dn, clear_pct=100.0, scenes=[]))
        clear = np.isfinite(ix["ndvi"]) & np.isfinite(ix["mndwi"]) & np.isfinite(ix["ndbi"])
        water = ix["mndwi"] > 0
        built = (ix["ndbi"] > 0) & (ix["ndvi"] < 0.2) & ~water   # same test as metrics.py
        stacks["ndvi"].append(np.where(clear & ~water, ix["ndvi"], np.nan))
        stacks["water"].append(np.where(clear, water, np.nan))
        stacks["built"].append(np.where(clear, built, np.nan))
        stacks["nbr"].append(_nbr(dn))
    arrays = {name: np.stack(layers).astype(np.float32) for name, layers in stacks.items()}
    return imagery, np.array(kept_years), arrays


def _nbr(dn: np.ndarray) -> np.ndarray:
    """Normalised burn ratio (NIR − SWIR2) / (NIR + SWIR2), NaN on holes; burnt ground drops it."""
    nir = dn[NIR].astype(np.float32) * SR_SCALE + SR_OFFSET
    swir2 = dn[SWIR2].astype(np.float32) * SR_SCALE + SR_OFFSET
    total = nir + swir2
    out = np.full(nir.shape, np.nan, dtype=np.float32)
    np.divide(nir - swir2, total, out=out, where=total != 0)
    out[(dn == 0).any(axis=0)] = np.nan
    return out


# --- Step 2: split scores ---

def _noise(values: np.ndarray) -> float:
    """Typical one-summer wobble of a series: robust MAD of first differences, as a std dev.
    The median of the differences is removed, so a steady trend doesn't count as noise."""
    diffs = np.diff(values)
    mad = np.median(np.abs(diffs - np.median(diffs)))
    return float(1.4826 * mad / np.sqrt(2.0))


def _split_jumps(values: np.ndarray, k: int) -> dict[int, float]:
    """{split index i: mean(values[i:i+k]) − mean(values[i-k:i])} for every full window."""
    jumps = {}
    for i in range(k, len(values) - k + 1):
        jumps[i] = float(values[i:i + k].mean() - values[i - k:i].mean())
    return jumps


def _passing_splits(values: np.ndarray, metric: str, k: int) -> dict[int, float]:
    """Splits whose jump is both statistically large (NOISE_Z) and physically large (MIN_JUMP)."""
    standard_error = _noise(values) * np.sqrt(2.0 / k)
    floor = max(NOISE_Z * standard_error, MIN_JUMP[metric])
    passing = {}
    for i, jump in _split_jumps(values, k).items():
        if abs(jump) >= floor:
            passing[i] = jump
    return passing


# --- Step 3: merge splits into changes ---

def _runs(passing: dict[int, float]) -> list[list[int]]:
    """Groups passing splits into runs of neighbours with the same sign.
    A one-split gap is bridged: one noisy summer shouldn't cut a long trend in two."""
    runs: list[list[int]] = []
    for i in sorted(passing):
        sign = np.sign(passing[i])
        if runs:
            last = runs[-1][-1]
            same_sign = np.sign(passing[last]) == sign
            if same_sign and i - last <= 2:
                runs[-1].append(i)
                continue
        runs.append([i])
    return runs


def _trim(run: list[int], passing: dict[int, float], k: int) -> tuple[int, int]:
    """First and last split of the change itself. A k-year window smears a step over k−1 splits
    on each side, so trim those, but never past the strongest split."""
    peak = max(run, key=lambda i: abs(passing[i]))
    first = min(run[0] + k - 1, peak)
    last = max(run[-1] - (k - 1), peak)
    return first, last


def _persistence(values: np.ndarray, end: int, before: float, after: float) -> int:
    """Summers from the end of the change on whose value stays nearer the new level than the old.
    One odd summer is forgiven; two in a row end the count."""
    midpoint = (before + after) / 2.0
    rising = after > before
    count = 0
    misses = 0
    for value in values[end:]:
        on_new_side = value > midpoint if rising else value < midpoint
        if on_new_side:
            count += 1
            misses = 0
        else:
            misses += 1
            if misses == 2:
                break
    return count


def _series_changes(years: np.ndarray, values: np.ndarray, metric: str) -> list[dict]:
    """Candidate changes on one series: years, before/after means, magnitude, persistence."""
    k = WINDOW_YEARS
    passing = _passing_splits(values, metric, k)
    changes = []
    for run in _runs(passing):
        first, last = _trim(run, passing, k)
        change = _make_change(values, metric, first, last, k)
        if _old_level_held(values, change, k):
            changes.append(change)
    return _merge_steps(values, changes, k)


def _make_change(values: np.ndarray, metric: str, first: int, last: int, k: int) -> dict:
    """One change from its first to its last split: windows, magnitude and persistence."""
    before_idx = (first - k, first)        # the k summers before the change
    after_idx = (last, last + k)           # the k summers after it
    before_mean = float(values[before_idx[0]:before_idx[1]].mean())
    after_mean = float(values[after_idx[0]:after_idx[1]].mean())
    return {
        "metric": metric,
        "first_split": first,
        "start": first - 1,                # last summer at the old level
        "end": last,                       # first summer at the new level
        "before_idx": before_idx,
        "after_idx": after_idx,
        "magnitude": after_mean - before_mean,
        "persistence_years": _persistence(values, last, before_mean, after_mean),
    }


def _old_level_held(values: np.ndarray, change: dict, k: int) -> bool:
    """False when the summers before the before-window already sat at the new level: then the
    'before' was a passing excursion (a couple of odd summers), not an old state that ended."""
    b0, b1 = change["before_idx"]
    earlier = values[max(b0 - k, 0):b0]
    if len(earlier) == 0:
        return True                         # the series starts here: nothing to contradict it
    before_mean = float(values[b0:b1].mean())
    midpoint = before_mean + change["magnitude"] / 2.0
    earlier_mean = float(earlier.mean())
    if change["magnitude"] > 0:
        return earlier_mean < midpoint
    return earlier_mean > midpoint


def _merge_steps(values: np.ndarray, changes: list[dict], k: int) -> list[dict]:
    """Joins same-direction steps up to 2k summers apart into one change, when the level never
    went back in between: a reservoir falling for 15 years is one story, not four."""
    merged: list[dict] = []
    for change in changes:
        if merged:
            previous = merged[-1]
            same_sign = np.sign(previous["magnitude"]) == np.sign(change["magnitude"])
            close = change["start"] - previous["end"] <= 2 * k
            gap_summers = change["start"] - previous["end"] + 1
            held = previous["persistence_years"] >= gap_summers   # no reversal in between
            if same_sign and close and held:
                merged[-1] = _make_change(values, change["metric"], previous["first_split"],
                                          change["end"], k)
                continue
        merged.append(change)
    return merged


# --- Step 6 helpers: where ---

def _window_diff(stack: np.ndarray, change: dict) -> np.ndarray:
    """Per-pixel mean over the after-window minus mean over the before-window (NaN on holes)."""
    b0, b1 = change["before_idx"]
    a0, a1 = change["after_idx"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-hole pixels give NaN, on purpose
        return np.nanmean(stack[a0:a1], axis=0) - np.nanmean(stack[b0:b1], axis=0)


def _pixel_change_mask(change: dict, arrays: dict[str, np.ndarray]) -> np.ndarray:
    """Pixels that moved in the same direction as the area-wide change, between the windows."""
    layer = {"ndvi_mean": "ndvi", "water_frac": "water", "built_frac": "built"}[change["metric"]]
    diff = _window_diff(arrays[layer], change)
    jump = PIXEL_NDVI_JUMP if layer == "ndvi" else PIXEL_CLASS_JUMP
    signed = diff * np.sign(change["magnitude"])
    return np.nan_to_num(signed, nan=0.0) >= jump


def _largest_patch(mask: np.ndarray) -> np.ndarray:
    """The largest 8-connected patch of True pixels (plain breadth-first search, no scipy)."""
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best: list[tuple[int, int]] = []
    rows, cols = np.nonzero(mask)
    for start in zip(rows.tolist(), cols.tolist()):
        if seen[start]:
            continue
        seen[start] = True
        patch = [start]
        queue = deque([start])
        while queue:
            r, c = queue.popleft()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nr, nc = r + dr, c + dc
                    inside = 0 <= nr < height and 0 <= nc < width
                    if inside and mask[nr, nc] and not seen[nr, nc]:
                        seen[nr, nc] = True
                        patch.append((nr, nc))
                        queue.append((nr, nc))
        if len(patch) > len(best):
            best = patch
    out = np.zeros_like(mask, dtype=bool)
    for r, c in best:
        out[r, c] = True
    return out


def _describe(patch: np.ndarray) -> str:
    """Plain words for where a patch is: 'across most of the area', 'northeast part', …"""
    height, width = patch.shape
    rows, cols = np.nonzero(patch)
    span_h = (rows.max() - rows.min() + 1) / height
    span_w = (cols.max() - cols.min() + 1) / width
    share = patch.sum() / patch.size
    if span_h > 0.66 and span_w > 0.66:
        return "across most of the area"
    north_south = ["north", "", "south"][min(int(3 * rows.mean() / height), 2)]
    west_east = ["west", "", "east"][min(int(3 * cols.mean() / width), 2)]
    where = north_south + west_east or "centre"
    size = "a small patch" if share < 0.02 else "an area"
    if where == "centre":
        return f"{size} in the centre"
    return f"{size} in the {where}"


def _region(patch: np.ndarray, bbox: list[float]) -> dict:
    """Pixel bounds of the patch → a lon/lat bbox inside place.bbox (row 0 = north).
    Linear in lon/lat; the area is a 6 km UTM square, so the skew is a fraction of a pixel."""
    west, south, east, north = bbox
    height, width = patch.shape
    rows, cols = np.nonzero(patch)
    lon_w = west + (cols.min() / width) * (east - west)
    lon_e = west + ((cols.max() + 1) / width) * (east - west)
    lat_n = north - (rows.min() / height) * (north - south)
    lat_s = north - ((rows.max() + 1) / height) * (north - south)
    bbox_ll = [round(float(v), 5) for v in (lon_w, lat_s, lon_e, lat_n)]
    return {"bbox": bbox_ll,
            "description": _describe(patch)}


# --- Step 5 helpers: kinds ---

def _base_kind(metric: str, magnitude: float) -> str:
    """The plain kind for a change on one metric (demolition, built-up falling, is 'other')."""
    rising = magnitude > 0
    if metric == "ndvi_mean":
        return "vegetation_gain" if rising else "vegetation_loss"
    if metric == "water_frac":
        return "water_gain" if rising else "water_loss"
    return "urban_growth" if rising else "other"


def _is_burn(change: dict, arrays: dict[str, np.ndarray], years: np.ndarray) -> bool:
    """A sudden greenness loss whose changed pixels lost NBR like burnt ground (dNBR ≥ 0.27)."""
    sudden = years[change["end"]] - years[change["start"]] <= BURN_MAX_YEARS
    if change["metric"] != "ndvi_mean" or change["magnitude"] >= 0 or not sudden:
        return False
    nbr = arrays["nbr"]
    b0, b1 = change["before_idx"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        before = np.nanmean(nbr[b0:b1], axis=0)
        after = nbr[change["end"]]      # the first summer after: burn scars fade with regrowth
        dnbr = before - after
        return bool(np.nanmean(dnbr[change["mask"]]) >= BURN_DNBR_MIN)


def _overlaps(a: dict, b: dict, years: np.ndarray) -> bool:
    """Do two changes share years (±1) and at least a tenth of the smaller one's pixels?"""
    years_touch = (years[a["start"]] <= years[b["end"]] + 1
                   and years[b["start"]] <= years[a["end"]] + 1)
    shared = np.count_nonzero(a["mask"] & b["mask"])
    smaller = min(a["mask"].sum(), b["mask"].sum())
    return years_touch and shared >= 0.1 * smaller


def _was_water_share(change: dict, arrays: dict[str, np.ndarray]) -> float:
    """Share of a change's pixels that were water in at least one summer before the change.
    Any earlier summer, because a lake shrinking for years leaves bed that was dry by the time
    of the before-window but was lake earlier (Lake Mead 2021: 42% vs 71%)."""
    b1 = change["before_idx"][1]
    water_before = np.nan_to_num(arrays["water"][:b1], nan=0.0).max(axis=0) >= 1.0
    pixels = change["mask"]
    return float(np.count_nonzero(water_before & pixels)) / max(int(pixels.sum()), 1)


def _assign_kinds(changes: list[dict], arrays: dict, years: np.ndarray,
                  series: dict[str, np.ndarray]) -> list[dict]:
    """Names each change, then applies the rules that re-read one metric through another."""
    for change in changes:
        change["kind"] = _base_kind(change["metric"], change["magnitude"])
        if _is_burn(change, arrays, years):
            change["kind"] = "burn"

    kept = []
    for change in changes:
        if change["metric"] != "built_frac":
            kept.append(change)
            continue
        others = [c for c in changes if c is not change]
        if change["magnitude"] > 0:
            # Shrinking lake: new "built" pixels that were water are exposed shore (README rule).
            if _was_water_share(change, arrays) >= SHORE_WATER_SHARE:
                if _any_overlapping(others, "water_loss", change, years):
                    continue        # already told as the water loss
                change["kind"] = "water_loss"
            # Burnt ground passes the built-up test; it's the fire, not new buildings.
            if _any_overlapping(others, "burn", change, years):
                continue
        elif _is_desert_suburb(change, arrays, series):
            change["kind"] = "urban_growth"
            for other in others:
                if other["kind"] == "vegetation_gain" and _overlaps(other, change, years):
                    other["drop"] = True    # the same event: told once, as urban growth
        kept.append(change)
    return [c for c in kept if not c.get("drop")]


def _any_overlapping(others: list[dict], kind: str, change: dict, years: np.ndarray) -> bool:
    """Is there a change of this kind in the same place and years?"""
    for other in others:
        if other["kind"] == kind and _overlaps(other, change, years):
            return True
    return False


def _is_desert_suburb(change: dict, arrays: dict[str, np.ndarray],
                      series: dict[str, np.ndarray]) -> bool:
    """Desert rule: bare soil passes the built-up test, so built-up falling while the same
    pixels green up, in arid land, is irrigated suburbs replacing desert."""
    b0, b1 = change["before_idx"]
    arid = float(series["ndvi_mean"][b0:b1].mean()) < DESERT_NDVI_MAX
    ndvi_rise = _window_diff(arrays["ndvi"], change)[change["mask"]]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        greening = float(np.nanmean(ndvi_rise))
    return arid and greening >= DESERT_GREENING_MIN


# --- Step 4b: local patches (a factory or a bridge is too small for the area-wide series) ---

def _patch_splits(built: np.ndarray, k: int, min_px: int) -> list[dict]:
    """Every (split, direction) where one connected patch of ≥ min_px pixels flipped its built-up
    class between the k summers before and the k summers after."""
    found = []
    for i in range(k, built.shape[0] - k + 1):
        diff = _window_diff(built, {"before_idx": (i - k, i), "after_idx": (i, i + k)})
        for sign in (1.0, -1.0):
            mask = np.nan_to_num(diff * sign, nan=0.0) >= LOCAL_CLASS_JUMP
            if mask.sum() < min_px:
                continue
            patch = _largest_patch(mask)
            if patch.sum() >= min_px:
                found.append({"split": i, "sign": sign, "patch": patch})
    return found


def _group_patch_splits(found: list[dict]) -> list[dict]:
    """Neighbouring splits (≤ 2 apart, same direction, overlapping patches) are one event;
    each event keeps its split with the biggest patch."""
    events: list[dict] = []
    for item in found:
        joined = False
        for event in events:
            near = item["split"] - event["last_split"] <= 2 and item["sign"] == event["sign"]
            shared = np.count_nonzero(item["patch"] & event["best"]["patch"])
            smaller = min(item["patch"].sum(), event["best"]["patch"].sum())
            if near and shared >= 0.1 * smaller:
                event["last_split"] = item["split"]
                if item["patch"].sum() > event["best"]["patch"].sum():
                    event["best"] = item
                joined = True
                break
        if not joined:
            events.append({"sign": item["sign"], "last_split": item["split"], "best": item})
    return [event["best"] for event in events]


def _local_built_changes(arrays: dict[str, np.ndarray], aoi_km: float) -> list[dict]:
    """Built-up changes found patch by patch, with the same window test, persistence and
    old-level check as the series, run on the patch's own mean. Magnitude is in built_frac
    units for the whole area (patch change × patch share), so it stays comparable."""
    built = arrays["built"]
    height, width = built.shape[1:]
    pixel_ha = (aoi_km * 1000.0 / width) * (aoi_km * 1000.0 / height) / 10_000.0
    min_px = int(np.ceil(LOCAL_MIN_PATCH_HA / pixel_ha))
    k = WINDOW_YEARS

    changes = []
    for best in _group_patch_splits(_patch_splits(built, k, min_px)):
        patch = best["patch"]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            patch_series = np.nanmean(built[:, patch], axis=1)
        patch_series = np.nan_to_num(patch_series, nan=float(np.nanmean(patch_series)))
        change = _make_change(patch_series, "built_frac", best["split"], best["split"], k)
        if not _old_level_held(patch_series, change, k):
            continue
        change["magnitude"] = change["magnitude"] * patch.sum() / patch.size
        change["mask"] = patch
        changes.append(change)
    return _join_local(changes)


def _join_local(changes: list[dict]) -> list[dict]:
    """Same-direction patch changes in touching years become one change (a bridge deck and its
    plaza finished a summer apart are one project). Magnitudes add; masks are joined."""
    changes = sorted(changes, key=lambda c: c["start"])
    joined: list[dict] = []
    for change in changes:
        if joined:
            previous = joined[-1]
            same_way = np.sign(previous["magnitude"]) == np.sign(change["magnitude"])
            touching = change["start"] <= previous["end"] + 1
            if same_way and touching:
                previous["end"] = max(previous["end"], change["end"])
                previous["after_idx"] = change["after_idx"]
                previous["magnitude"] += change["magnitude"]
                previous["mask"] = previous["mask"] | change["mask"]
                previous["persistence_years"] = min(previous["persistence_years"],
                                                    change["persistence_years"])
                continue
        joined.append(change)
    return joined


# --- Step 6: sensors ---

def _sensor_switch_years(imagery: dict) -> list[int]:
    """Years where the main sensor family changes from the year before (TM → ETM+ → OLI).
    Landsat 7 counts as the main sensor only when it is alone (striped gap-filler otherwise)."""
    switches = []
    previous = None
    for frame in sorted(imagery["frames"], key=lambda f: f["year"]):
        families = {SENSOR_FAMILY.get(s, s) for s in frame["sensors"]}
        if "TM" in families:
            main = "TM"
        elif "OLI" in families:
            main = "OLI"
        else:
            main = "ETM+"
        if previous is not None and main != previous:
            switches.append(frame["year"])
        previous = main
    return switches


def _near_switch(start_year: int, end_year: int, switches: list[int]) -> bool:
    """DR-6: the change's start or end year is within 1 year of a sensor switch."""
    for switch in switches:
        if abs(start_year - switch) <= 1 or abs(end_year - switch) <= 1:
            return True
    return False


# --- Main ---

def detect(story_dir: Path) -> list[dict]:
    """The place's lasting changes, largest first, as story.json changes[] items (0–5)."""
    story_dir = Path(story_dir)
    imagery, years, arrays = _load(story_dir)
    rows = imagery["metrics"]["series"]
    series = {m: np.array([row[m] for row in rows], dtype=float) for m in METRICS}
    if len(years) < 2 * WINDOW_YEARS:
        return []           # too few summers to compare two windows

    candidates = []
    for metric in METRICS:
        for change in _series_changes(years, series[metric], metric):
            fire_like = change["metric"] == "ndvi_mean" and change["magnitude"] < 0
            if change["persistence_years"] < PERSIST_MIN_YEARS and not fire_like:
                continue    # FR-22 (a fire may be brief: the burn test below decides)
            mask = _pixel_change_mask(change, arrays)
            if mask.mean() < MIN_REGION_SHARE:
                continue    # an area-wide shift no pixel shows: light, haze or calibration
            change["mask"] = mask
            candidates.append(change)

    for local in _local_built_changes(arrays, imagery["place"]["aoi_km"]):
        if local["persistence_years"] < PERSIST_MIN_YEARS:
            continue        # FR-22
        same_story = False
        for change in candidates:
            same_metric = change["metric"] == "built_frac"
            same_way = np.sign(change["magnitude"]) == np.sign(local["magnitude"])
            if same_metric and same_way and _overlaps(change, local, years):
                same_story = True   # the area-wide series already tells this one
        if not same_story:
            candidates.append(local)

    changes = _assign_kinds(candidates, arrays, years, series)
    # FR-22 for non-fires that only got past the filter above as possible fires.
    lasting = []
    for change in changes:
        if change["kind"] == "burn" or change["persistence_years"] >= PERSIST_MIN_YEARS:
            lasting.append(change)

    lasting.sort(key=lambda c: int(c["mask"].sum()), reverse=True)  # largest area first
    switches = _sensor_switch_years(imagery)
    out = []
    for number, change in enumerate(lasting[:MAX_CHANGES], start=1):
        start_year = int(years[change["start"]])
        end_year = int(years[change["end"]])
        patch = _largest_patch(change["mask"])
        out.append({
            "id": f"chg-{number:02d}",
            "kind": change["kind"],
            "metric": change["metric"],
            "start_year": start_year,
            "end_year": end_year,
            "magnitude": round(change["magnitude"], 4),
            "persistence_years": max(int(change["persistence_years"]), 1),
            "region": _region(patch, imagery["place"]["bbox"]),
            "before_year": start_year,
            "after_year": end_year,
            "mask_url": None,
            "sensor_switch_flag": _near_switch(start_year, end_year, switches),
        })
    return out
