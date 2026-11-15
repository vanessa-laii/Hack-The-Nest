# Spike S3b, approach "combined": place-wide straight segments plus strict local built-up patches.
# Inputs: a story folder (imagery.json metrics.series + frames + place, composites.npz).
# Outputs: detect(story_dir) → 0–5 changes[] items of contracts/story.schema.json.
#
# How it works:
#   1. Load the yearly series and per-pixel stacks (land NDVI, water, built-up, burn ratio).
#   2. Place-wide layer (from segments/): fit straight segments to each series; a big, lasting
#      rise or fall becomes a candidate, placed on the map by its largest changed patch.
#   3. Name place-wide candidates: burn test (dNBR), shrinking-lake rule, desert rule; a
#      built-up fall is re-told as greening or dropped, never told as demolition. Changes at a
#      sensor switch need 2 later switch-free summers (DR-6). Re-told numbers pass the size test.
#   4. Local layer (from window-means/): per pixel, find connected patches ≥ 4 ha whose
#      built-up class fully flipped between 3 summers before and 3 after, and held (FR-22).
#   5. Local patches are measured inside their own region ("in this area"), checked against
#      sensor switches (DR-6), re-read through water and burns, and merged when close.
#   6. Every change's kind must agree with its metric and sign; anything else is dropped.
#   7. Rank: place-wide first, then local by area × |magnitude|; keep 5 (HL-2), number them.
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "backend"))
from app.pipeline.metrics import indices  # noqa: E402  (per-pixel NDVI / MNDWI / NDBI)
from app.pipeline.types import NIR, SR_OFFSET, SR_SCALE, SWIR2, YearComposite  # noqa: E402

METRICS = ["ndvi_mean", "water_frac", "built_frac"]
LAYER_OF = {"ndvi_mean": "ndvi", "water_frac": "water", "built_frac": "built"}

# --- Thresholds: place-wide layer (the segments approach, unchanged) ---
# Smallest change worth a chapter, before noise scaling. 0.03 NDVI ≈ a dry vs a wet summer for
# the same plants; 0.02 of the area ≈ 0.7 km² of a 6 km square (a reservoir arm, a suburb).
PHYSICAL_FLOOR = {"ndvi_mean": 0.03, "water_frac": 0.02, "built_frac": 0.02}
NOISE_K = 4.0          # a change must be ≥ 4 × the series' own year-to-year noise
SPIKE_K = 3.0          # a one-year jump ≥ 3 × noise that comes straight back is a spike
SPIKE_AGREE = 0.5      # ... if the two neighbours agree within half the spike's size
MAX_SEGMENTS = 6       # LandTrendr's usual maximum
MIN_SEGMENT_YEARS = 3  # a segment needs ≥ 3 years to have a measurable slope
PERSIST_MIN = 2        # FR-22: a change must last ≥ 2 years after it happens
WINDOW = 3             # summers averaged on each side of a change, both layers
PIXEL_NDVI_CHANGE = 0.10   # a pixel's greenness moved by ≥ 0.1: visible in the film
PIXEL_CLASS_CHANGE = 0.5   # a pixel's water/built share moved by ≥ half the summers
MIN_PATCH_SHARE = 0.01     # a place-wide change needs one connected patch ≥ 1% of the area

# --- Thresholds: local layer ---
# A local change is one connected patch (8-connected, so a diagonal runway stays in one piece)
# of ≥ 4 ha ≈ 44 pixels of 30 m: a big-box store with its car park, a bridge plaza. Central
# Park's largest fully flipped patch at any split is ≈ 2 ha, so ordinary city churn and
# sensor noise stay under it with a 2× margin.
LOCAL_MIN_PATCH_HA = 4.0
LOCAL_MIN_CLEAR = 2        # each 3-summer window needs ≥ 2 clear summers for a pixel to count
# "Fully changed": built-up in none of the clear summers before and in all of them after (or
# the reverse). Partial flips (2 of 3) let the control reach 10 ha patches in window-means.
LOCAL_GROUP_SPLITS = 2     # the same pixels flipping 1–2 splits apart are one event
LOCAL_GROUP_OVERLAP = 0.1  # ... if they share ≥ 10% of the smaller patch
# Merge same-kind local changes ≤ 500 m apart whose start years are ≤ 2 apart: a cleared
# neighbourhood and the plaza built on it next door are one story, not five tiny ones.
MERGE_GAP_M = 500.0
MERGE_YEARS = 2
# A built-up fall (local or place-wide, non-arid) whose pixels turned green (≥ 0.1 NDVI, the
# same "visible" step as the pixel test) is told as what is visible, greening, not as built-up
# falling: the built-up test can't tell a torn-down roof from bare soil that grew over.
LOCAL_GREENING_MIN = 0.10

# --- Cross-layer rules ---
BURN_DNBR_MIN = 0.27       # USGS dNBR "moderate severity" lower bound: a real burn scar
BURN_MAX_YEARS = 2         # a burn happens within one or two summers (sudden)
WATER_SHARE = 0.5          # ≥ half of a patch was / became water → it's a water story
DESERT_NDVI_MAX = 0.2      # median yearly land NDVI of the record below this = arid place
DESERT_GREENING_MIN = 0.05  # pixels leaving "built" gained ≥ 0.05 NDVI: greener, not cleared
OVERLAP_MIN = 0.1          # two changes are "the same place" if they share ≥ 10% of the smaller
SWITCH_NEAR = 1            # DR-6: within 1 year of a sensor switch
MAX_CHANGES = 5            # HL-2

# Which way each kind moves its metric (same table as the scorer: the narration must agree).
EXPECTED_SIGN = {
    ("water_loss", "water_frac"): -1, ("water_gain", "water_frac"): +1,
    ("vegetation_loss", "ndvi_mean"): -1, ("vegetation_gain", "ndvi_mean"): +1,
    ("burn", "ndvi_mean"): -1, ("urban_growth", "built_frac"): +1,
    ("other", "built_frac"): -1,       # "other" is only ever used here for demolition
}

SENSOR_FAMILY = {"Landsat 4": "TM", "Landsat 5": "TM", "Landsat 7": "ETM+",
                 "Landsat 8": "OLI", "Landsat 9": "OLI"}


# --- Step 1: inputs ---

def load_pixels(story_dir: Path, years: list[int]) -> dict[str, np.ndarray]:
    """Per-pixel stacks [Y, H, W] for the kept years: land NDVI, water (0/1), built (0/1), NBR.
    NaN marks holes. The built-up and water tests are the ones metrics.py uses."""
    npz = np.load(story_dir / "composites.npz")
    npz_years = [int(y) for y in npz["years"]]
    dn_all = npz["dn"]
    stacks = {"ndvi": [], "water": [], "built": [], "nbr": []}
    for year in years:
        dn = dn_all[npz_years.index(year)]
        ix = indices(YearComposite(year=year, dn=dn, clear_pct=100.0, scenes=[]))
        clear = np.isfinite(ix["ndvi"]) & np.isfinite(ix["mndwi"]) & np.isfinite(ix["ndbi"])
        water = ix["mndwi"] > 0
        built = (ix["ndbi"] > 0) & (ix["ndvi"] < 0.2) & ~water
        stacks["ndvi"].append(np.where(clear & ~water, ix["ndvi"], np.nan))
        stacks["water"].append(np.where(clear, water, np.nan))
        stacks["built"].append(np.where(clear, built, np.nan))
        stacks["nbr"].append(burn_ratio(dn))
    return {name: np.stack(layers).astype(np.float32) for name, layers in stacks.items()}


def burn_ratio(dn: np.ndarray) -> np.ndarray:
    """Normalised burn ratio (NIR − SWIR2) / (NIR + SWIR2), NaN on holes; burnt ground drops it."""
    nir = dn[NIR].astype(np.float32) * SR_SCALE + SR_OFFSET
    swir2 = dn[SWIR2].astype(np.float32) * SR_SCALE + SR_OFFSET
    total = nir + swir2
    out = np.full(nir.shape, np.nan, dtype=np.float32)
    np.divide(nir - swir2, total, out=out, where=total != 0)
    out[(dn == 0).any(axis=0)] = np.nan
    return out


def window_mean(stack: np.ndarray, first: int, last: int) -> np.ndarray:
    """Per-pixel mean over rows first..last-1, NaN where all of them are holes."""
    first = max(first, 0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # all-hole pixels → NaN, on purpose
        return np.nanmean(stack[first:last], axis=0)


def region_series(stack: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """The mean of a layer over the mask's pixels, per year (holes skipped, gaps filled)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        values = np.nanmean(stack[:, mask], axis=1)
        fill = float(np.nanmean(values))
    return np.nan_to_num(values, nan=fill)


# --- Step 2: place-wide layer (straight segments) ---

def noise_sigma(values: np.ndarray) -> float:
    """Year-to-year noise from the median absolute second difference.
    Second differences ignore straight trends, so a real slow change doesn't count as noise."""
    second = values[2:] - 2 * values[1:-1] + values[:-2]
    mad = np.median(np.abs(second - np.median(second)))
    return float(1.4826 * mad / np.sqrt(6.0))   # var(second diff) = 6 σ² for white noise


def despike(values: np.ndarray, sigma: float) -> np.ndarray:
    """Replaces one-year spikes with the mean of their neighbours.
    A spike jumps away and comes straight back: clouds or haze, not change."""
    clean = values.copy()
    for i in range(1, len(values) - 1):
        neighbours_mean = (clean[i - 1] + values[i + 1]) / 2
        jump = values[i] - neighbours_mean
        neighbours_gap = abs(clean[i - 1] - values[i + 1])
        if abs(jump) >= SPIKE_K * sigma and neighbours_gap < SPIKE_AGREE * abs(jump):
            clean[i] = neighbours_mean
    return clean


def line_costs(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """cost[i, j] = squared error of the best straight line through points i..j (inclusive).
    Built from running sums, so every pair costs O(1)."""
    n = len(x)
    cost = np.full((n, n), np.inf)
    for i in range(n):
        xs = x[i:]
        ys = y[i:]
        count = np.arange(1, len(xs) + 1, dtype=float)
        sx = np.cumsum(xs)
        sy = np.cumsum(ys)
        var_x = np.cumsum(xs * xs) - sx * sx / count
        cov = np.cumsum(xs * ys) - sx * sy / count
        var_y = np.cumsum(ys * ys) - sy * sy / count
        with np.errstate(divide="ignore", invalid="ignore"):
            sse = var_y - np.where(var_x > 0, cov * cov / var_x, 0.0)
        cost[i, i:] = np.maximum(sse, 0.0)
    return cost


def best_split(cost: np.ndarray, segments: int) -> tuple[float, list[int]]:
    """Exact best split of the series into `segments` lines (dynamic programming).
    Returns the total squared error and the index where each segment starts."""
    n = cost.shape[0]
    m = MIN_SEGMENT_YEARS
    total = np.full((segments + 1, n + 1), np.inf)
    start = np.zeros((segments + 1, n + 1), dtype=int)
    total[0, 0] = 0.0
    for k in range(1, segments + 1):
        for end in range(k * m, n + 1):          # segment k covers points begin..end-1
            begins = np.arange((k - 1) * m, end - m + 1)
            options = total[k - 1, begins] + cost[begins, end - 1]
            best = int(np.argmin(options))
            total[k, end] = options[best]
            start[k, end] = begins[best]
    starts = []
    end = n
    for k in range(segments, 0, -1):
        begin = int(start[k, end])
        starts.append(begin)
        end = begin
    return float(total[segments, n]), sorted(starts)


def fit_segments(years: np.ndarray, values: np.ndarray) -> list[tuple[int, int]]:
    """Picks the number of straight segments by BIC and returns them as (first, last) indices.
    BIC charges 3 numbers per extra segment (start, slope, level), so noise doesn't buy one."""
    n = len(values)
    cost = line_costs(years.astype(float), values)
    best_bic = np.inf
    best_starts = [0]
    max_k = min(MAX_SEGMENTS, n // MIN_SEGMENT_YEARS)
    for k in range(1, max_k + 1):
        sse, starts = best_split(cost, k)
        params = 3 * k - 1
        bic = n * np.log(max(sse, 1e-12) / n) + params * np.log(n)
        if bic < best_bic:
            best_bic = bic
            best_starts = starts
    bounds = best_starts + [n]
    return [(bounds[i], bounds[i + 1] - 1) for i in range(len(best_starts))]


def fitted_values(years: np.ndarray, values: np.ndarray,
                  segs: list[tuple[int, int]]) -> np.ndarray:
    """The fitted line: a least-squares line through each segment, at its own years."""
    fit = np.empty_like(values)
    for first, last in segs:
        x = years[first:last + 1].astype(float)
        slope, intercept = np.polyfit(x, values[first:last + 1], 1)
        fit[first:last + 1] = slope * x + intercept
    return fit


def movements(fit: np.ndarray, segs: list[tuple[int, int]], sigma: float) -> list[dict]:
    """Rises and falls of the fitted line: each segment's slope and each jump between segments.
    Pieces smaller than the noise are ignored; same-direction pieces in a row are joined."""
    pieces = []
    for idx, (first, last) in enumerate(segs):
        if idx > 0:
            prev_last = segs[idx - 1][1]
            pieces.append((prev_last, first, fit[first] - fit[prev_last]))   # jump
        pieces.append((first, last, fit[last] - fit[first]))                 # slope
    joined: list[dict] = []
    for begin, end, size in pieces:
        if abs(size) < sigma:
            continue
        if joined and np.sign(joined[-1]["size"]) == np.sign(size):
            joined[-1]["end"] = end
            joined[-1]["size"] += size
        else:
            joined.append({"begin": begin, "end": end, "size": size})
    return joined


def tighten(move: dict, fit: np.ndarray) -> tuple[int, int]:
    """Trims flat ends off a movement so its years cover where the line actually moved
    (≥ 90% of the movement happens between the returned indices)."""
    begin, end = move["begin"], move["end"]
    total = fit[end] - fit[begin]
    while begin < end and abs(fit[end] - fit[begin + 1]) >= 0.9 * abs(total):
        begin += 1
    while end > begin and abs(fit[end - 1] - fit[begin]) >= 0.9 * abs(total):
        end -= 1
    return begin, end


def persistence(values: np.ndarray, end: int, before: float, after: float) -> int:
    """Summers from the end of the change on that stay nearer the new level than the old.
    One odd summer is forgiven; two in a row end the count (FR-22)."""
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


def clean_series(series: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Each place-wide series with its one-year spikes removed."""
    cleaned = {}
    for metric in METRICS:
        cleaned[metric] = despike(series[metric], noise_sigma(series[metric]))
    return cleaned


def big_enough(metric: str, magnitude: float, values: np.ndarray) -> bool:
    """The size test every change passes: ≥ the metric's physical floor and ≥ NOISE_K × the
    noise of the series it is measured on. Re-told changes must pass it for their new metric."""
    threshold = max(PHYSICAL_FLOOR[metric], NOISE_K * noise_sigma(values))
    return abs(magnitude) >= threshold


def set_levels(change: dict, clean: np.ndarray) -> None:
    """Stores the series a place-wide change is told on and its old and new levels, so the
    sensor-switch check (DR-6) can test later summers the same way as for local changes."""
    change["values"] = clean
    change["before_level"] = float(clean[change["start"]])
    change["after_level"] = float(clean[change["end"]])
    change["persistence_years"] = persistence(clean, change["end"], change["before_level"],
                                              change["after_level"])


def series_candidates(years: np.ndarray, series: dict[str, np.ndarray],
                      cleaned: dict[str, np.ndarray]) -> list[dict]:
    """All lasting movements of all three place-wide series, before naming them."""
    found = []
    for metric in METRICS:
        raw = series[metric]
        sigma = noise_sigma(raw)
        clean = cleaned[metric]
        segs = fit_segments(years, clean)
        fit = fitted_values(years, clean, segs)
        threshold = max(PHYSICAL_FLOOR[metric], NOISE_K * sigma)
        for move in movements(fit, segs, sigma):
            if abs(move["size"]) < threshold:
                continue
            begin, end = tighten(move, fit)
            magnitude = float(raw[end] - raw[begin])     # after minus before, as on the chart
            if np.sign(magnitude) != np.sign(move["size"]) or abs(magnitude) < threshold:
                continue
            found.append({
                "layer": "place", "metric": metric, "start": begin, "end": end,
                "magnitude": magnitude,
                "rank": abs(move["size"]) / threshold,   # size in noise units
                "persistence_years": persistence(clean, end, clean[begin], clean[end]),
            })
    return found


def window_rows(change: dict) -> tuple[int, int, int, int]:
    """Rows of up to WINDOW summers ending at the start, and starting at the end."""
    before = (change["start"] - WINDOW + 1, change["start"] + 1)
    after = (change["end"], change["end"] + WINDOW)
    return before[0], before[1], after[0], after[1]


def pixel_diff(pixels: dict, layer: str, change: dict) -> np.ndarray:
    """Per-pixel after-window mean minus before-window mean of one layer (NaN on holes)."""
    b0, b1, a0, a1 = window_rows(change)
    stack = pixels[layer]
    return window_mean(stack, a0, a1) - window_mean(stack, b0, b1)


def place_mask(pixels: dict, change: dict) -> np.ndarray:
    """Pixels that moved the same way as the place-wide series, by a visible amount."""
    layer = LAYER_OF[change["metric"]]
    diff = np.nan_to_num(pixel_diff(pixels, layer, change), nan=0.0)
    limit = PIXEL_NDVI_CHANGE if layer == "ndvi" else PIXEL_CLASS_CHANGE
    return diff * np.sign(change["magnitude"]) >= limit


# --- Connected patches (numpy only) ---

def label_patches(mask: np.ndarray) -> np.ndarray:
    """8-connected labels: each pixel takes the smallest label among its neighbours until
    nothing changes. A label is a pixel index, so a pixel can also jump to its label's label,
    which makes long winding patches converge in a few dozen rounds. Background = -1."""
    height, width = mask.shape
    big = height * width
    labels = np.where(mask, np.arange(big).reshape(height, width), big)
    padded = np.full((height + 2, width + 2), big)
    while True:
        padded[1:-1, 1:-1] = labels
        spread = labels.copy()
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                shifted = padded[1 + dr:1 + dr + height, 1 + dc:1 + dc + width]
                spread = np.minimum(spread, shifted)
        spread = np.where(mask, spread, big)
        lookup = np.append(spread.ravel(), big)
        spread = lookup[spread]                 # pointer jump: my label's own label
        if np.array_equal(spread, labels):
            break
        labels = spread
    return np.where(mask, labels, -1)


def patches_at_least(mask: np.ndarray, min_px: int) -> list[np.ndarray]:
    """Every connected patch of the mask with ≥ min_px pixels, as boolean masks."""
    if mask.sum() < min_px:
        return []
    labels = label_patches(mask)
    ids, counts = np.unique(labels[mask], return_counts=True)
    patches = []
    for label, count in zip(ids, counts):
        if count >= min_px:
            patches.append(labels == label)
    return patches


def largest_patch(mask: np.ndarray) -> np.ndarray:
    """The largest connected patch of the mask (empty mask if there is none)."""
    if not mask.any():
        return mask
    labels = label_patches(mask)
    ids, counts = np.unique(labels[mask], return_counts=True)
    return labels == ids[np.argmax(counts)]


# --- Step 3: naming place-wide changes ---

def base_kind(metric: str, magnitude: float) -> str:
    """The kind a change gets from its metric and direction alone (built-up falling: other)."""
    rising = magnitude > 0
    if metric == "ndvi_mean":
        return "vegetation_gain" if rising else "vegetation_loss"
    if metric == "water_frac":
        return "water_gain" if rising else "water_loss"
    return "urban_growth" if rising else "other"


def is_burn(change: dict, pixels: dict, years: np.ndarray) -> bool:
    """A sudden greenness loss whose changed pixels lost NBR like burnt ground (dNBR ≥ 0.27).
    Measured on the first summer after the drop began: burn scars fade as plants regrow."""
    sudden = years[change["end"]] - years[change["start"]] <= BURN_MAX_YEARS
    if change["kind"] != "vegetation_loss" or not sudden or not change["mask"].any():
        return False
    b0, b1, _, _ = window_rows(change)
    before = window_mean(pixels["nbr"], b0, b1)
    after = pixels["nbr"][change["start"] + 1]
    dnbr = (before - after)[change["mask"]]
    dnbr = dnbr[np.isfinite(dnbr)]
    return len(dnbr) > 0 and float(np.median(dnbr)) >= BURN_DNBR_MIN


def years_touch(a: dict, b: dict, years: np.ndarray, slack: int = 1) -> bool:
    """Do two changes share years (± slack)?"""
    return (years[a["start"]] <= years[b["end"]] + slack
            and years[b["start"]] <= years[a["end"]] + slack)


def same_place(a: dict, b: dict) -> bool:
    """Do two changes share at least OVERLAP_MIN of the smaller one's pixels?"""
    shared = np.count_nonzero(a["mask"] & b["mask"])
    smaller = min(int(a["mask"].sum()), int(b["mask"].sum()))
    return smaller > 0 and shared >= OVERLAP_MIN * smaller


def was_water_share(pixels: dict, change: dict) -> float:
    """Share of the change's pixels that were water in at least one summer before it.
    Any earlier summer: a lake shrinking for years leaves bed that was dry by the before-window."""
    before_end = window_rows(change)[1]
    if before_end <= 0:
        return 0.0
    ever_water = np.nan_to_num(pixels["water"][:before_end], nan=0.0).max(axis=0) >= 1.0
    count = int(change["mask"].sum())
    return float(np.count_nonzero(ever_water & change["mask"])) / max(count, 1)


def is_arid(series: dict[str, np.ndarray]) -> bool:
    """Is the place desert: median yearly land NDVI over the whole record under 0.2?
    The whole record, because irrigated suburbs can lift a desert place above 0.2 later on,
    and bare soil still reads as built-up there."""
    return float(np.median(series["ndvi_mean"])) < DESERT_NDVI_MAX


def greening(pixels: dict, change: dict) -> float:
    """Mean NDVI rise over the change's pixels between the windows."""
    rise = pixel_diff(pixels, "ndvi", change)[change["mask"]]
    rise = rise[np.isfinite(rise)]
    return float(rise.mean()) if len(rise) else 0.0


def name_place_changes(cands: list[dict], pixels: dict, years: np.ndarray,
                       series: dict[str, np.ndarray]) -> list[dict]:
    """Applies the kind rules to place-wide candidates. A built-up change that is really another
    event seen through the built-up index is dropped or re-told, never kept as is."""
    for cand in cands:
        cand["kind"] = base_kind(cand["metric"], cand["magnitude"])
        cand["mask"] = place_mask(pixels, cand)
        if is_burn(cand, pixels, years):
            cand["kind"] = "burn"
    kept = []
    for cand in cands:
        if cand["metric"] != "built_frac":
            kept.append(cand)
            continue
        others = [c for c in cands if c is not cand and years_touch(c, cand, years)]
        if cand["magnitude"] > 0 and built_rise_kept(cand, others, pixels, series):
            kept.append(cand)
        elif cand["magnitude"] < 0 and built_fall_kept(cand, others, pixels, series):
            kept.append(cand)
    return [c for c in kept if not c.get("drop")]


def retell_place(cand: dict, kind: str, metric: str, series: dict[str, np.ndarray]) -> bool:
    """Re-tells a place-wide change as another kind on another metric, measured on that series
    over the same years. False (drop it) unless the new number has the kind's sign and passes
    the size test for its new metric: a re-told change must be as real as a fresh one."""
    values = series[metric]
    magnitude = float(values[cand["end"]] - values[cand["start"]])
    if magnitude * EXPECTED_SIGN[(kind, metric)] <= 0:
        return False
    if not big_enough(metric, magnitude, values):
        return False
    cand["kind"] = kind
    cand["metric"] = metric
    cand["magnitude"] = magnitude
    return True


def built_rise_kept(cand: dict, others: list[dict], pixels: dict,
                    series: dict[str, np.ndarray]) -> bool:
    """Shrinking-lake rule: new "built-up" on old lake bed is exposed shore, told as water_loss
    (or not at all if a water_loss already tells it). Burnt ground also reads as built-up."""
    if was_water_share(pixels, cand) >= WATER_SHARE:
        for other in others:
            if other["kind"] == "water_loss":
                return False
        # Say nothing rather than guess if the water share didn't clearly fall.
        return retell_place(cand, "water_loss", "water_frac", series)
    for other in others:
        if other["kind"] == "burn" and same_place(cand, other):
            return False
    return True


def built_fall_kept(cand: dict, others: list[dict], pixels: dict,
                    series: dict[str, np.ndarray]) -> bool:
    """A place-wide built-up fall is never kept as 'other' (the film would tell it as
    demolition): bare soil, summer fields and quarries pass the built-up test too. Arid places
    use the desert rule; elsewhere it is re-read as greening, like a local fall, or dropped."""
    if is_arid(series):
        desert_rule(cand, others, pixels, series)
        return False
    if greening(pixels, cand) < LOCAL_GREENING_MIN:
        return False
    for other in others:
        if other["kind"] == "vegetation_gain" and same_place(other, cand):
            return False             # an overlapping greening change already tells it
    return retell_place(cand, "vegetation_gain", "ndvi_mean", series)


def desert_rule(cand: dict, others: list[dict], pixels: dict,
                series: dict[str, np.ndarray]) -> None:
    """Desert rule. Bare desert soil passes the built-up test, so in arid land a built-up fall
    is not evidence of demolition (new roofs, lawns and roads all score lower than sand). If the
    same pixels greened, an overlapping greening change tells it, worded as desert greening."""
    if greening(pixels, cand) < DESERT_GREENING_MIN:
        return
    greenings = []
    for other in others:
        if other["kind"] == "vegetation_gain" and same_place(other, cand):
            greenings.append(other)
    if not greenings:
        return
    # Several greening pieces of one long rise are the same event: tell it once.
    greenings.sort(key=lambda c: (c.get("desert", False), c["rank"]), reverse=True)
    if not greenings[0].get("desert"):
        retime_desert_greening(greenings[0], cand, series)
    for duplicate in greenings[1:]:
        duplicate["drop"] = True


def retime_desert_greening(greening_change: dict, built_fall: dict,
                           series: dict[str, np.ndarray]) -> None:
    """Dates and places the greening by the (clearer) built-up fall, which is the same event.
    The number stays a greenness change, so the chart and the script agree. If the greenness
    change over the built-up fall's years fails the size test, the greening keeps its own."""
    magnitude = float(series["ndvi_mean"][built_fall["end"]]
                      - series["ndvi_mean"][built_fall["start"]])
    greening_change["desert"] = True
    if magnitude <= 0 or not big_enough("ndvi_mean", magnitude, series["ndvi_mean"]):
        return
    greening_change["start"] = built_fall["start"]
    greening_change["end"] = built_fall["end"]
    greening_change["magnitude"] = magnitude
    greening_change["mask"] = built_fall["mask"]


# --- Step 4: local layer (fully flipped built-up patches) ---

def pixel_size_ha(place: dict, shape: tuple[int, int]) -> float:
    """Area of one pixel in hectares (the composite is a square of aoi_km on a side)."""
    side_m = place["aoi_km"] * 1000.0
    return (side_m / shape[1]) * (side_m / shape[0]) / 10_000.0


def full_flips(built: np.ndarray, split: int) -> tuple[np.ndarray, np.ndarray]:
    """Pixels whose built-up class fully flipped at this split: never built in the clear
    summers of the WINDOW before and always built in those after (rise), or the reverse."""
    before = built[split - WINDOW:split]
    after = built[split:split + WINDOW]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        before_max = np.nanmax(before, axis=0)
        before_min = np.nanmin(before, axis=0)
        after_max = np.nanmax(after, axis=0)
        after_min = np.nanmin(after, axis=0)
    enough = ((np.isfinite(before).sum(axis=0) >= LOCAL_MIN_CLEAR)
              & (np.isfinite(after).sum(axis=0) >= LOCAL_MIN_CLEAR))
    rise = enough & (before_max == 0) & (after_min == 1)
    fall = enough & (before_min == 1) & (after_max == 0)
    return rise, fall


def flipped_patches(built: np.ndarray, min_px: int) -> list[dict]:
    """Every (split, direction, patch) with a fully flipped connected patch of ≥ min_px."""
    found = []
    for split in range(WINDOW, built.shape[0] - WINDOW + 1):
        rise, fall = full_flips(built, split)
        for sign, mask in ((1.0, rise), (-1.0, fall)):
            for patch in patches_at_least(mask, min_px):
                found.append({"split": split, "sign": sign, "mask": patch})
    return found


def group_events(found: list[dict]) -> list[dict]:
    """The same pixels flipping at neighbouring splits are one event: masks are joined and
    the event runs from its first to its last split."""
    events: list[dict] = []
    for item in sorted(found, key=lambda f: f["split"]):
        home = None
        for event in events:
            near = item["split"] - event["last_split"] <= LOCAL_GROUP_SPLITS
            if near and item["sign"] == event["sign"] and same_place(item, event):
                home = event
                break
        if home is None:
            events.append({"sign": item["sign"], "first_split": item["split"],
                           "last_split": item["split"], "mask": item["mask"].copy()})
        else:
            home["last_split"] = item["split"]
            home["mask"] |= item["mask"]
    return events


def measure_local(event: dict, pixels: dict) -> dict:
    """A local change measured inside its own region: the built-up share of the patch's pixels,
    after-window minus before-window. So the magnitude means "in this area", not the 6 km
    square (a 20 ha plant torn down is −1.0 here, but only −0.006 of the whole square)."""
    values = region_series(pixels["built"], event["mask"])
    change = {
        "layer": "local", "metric": "built_frac", "mask": event["mask"],
        "start": event["first_split"] - 1,      # last summer at the old level
        "end": event["last_split"],             # first summer at the new level
        "values": values,
    }
    b0, b1, a0, a1 = window_rows(change)
    before = float(values[max(b0, 0):b1].mean())
    after = float(values[a0:a1].mean())
    change["before_level"] = before
    change["after_level"] = after
    change["magnitude"] = after - before
    change["persistence_years"] = persistence(values, change["end"], before, after)
    return change


def old_level_held(change: dict) -> bool:
    """False when the summers before the before-window already sat at the new level: then the
    'before' was a passing excursion (a burn scar, an odd summer), not an old state that ended.
    Also False without a full WINDOW of earlier summers: one patch is weak evidence on its own,
    so its old state must be seen for 2 × WINDOW summers (an odd first summer can't make it)."""
    b0 = window_rows(change)[0]
    if b0 < WINDOW:
        return False
    earlier = change["values"][b0 - WINDOW:b0]
    midpoint = change["before_level"] + change["magnitude"] / 2.0
    if change["magnitude"] > 0:
        return float(earlier.mean()) < midpoint
    return float(earlier.mean()) > midpoint


def local_candidates(pixels: dict, place: dict) -> list[dict]:
    """Local built-up changes that fully flipped, held for ≥ 2 summers and weren't excursions."""
    built = pixels["built"]
    min_px = int(np.ceil(LOCAL_MIN_PATCH_HA / pixel_size_ha(place, built.shape[1:])))
    found = []
    for event in group_events(flipped_patches(built, min_px)):
        change = measure_local(event, pixels)
        if change["persistence_years"] < PERSIST_MIN:
            continue                # FR-22
        if not old_level_held(change):
            continue
        change["kind"] = base_kind("built_frac", change["magnitude"])
        found.append(change)
    return found


# --- Step 5: local changes against sensors, water, burns and each other ---

def sensor_family(sensors: list[str]) -> str:
    """The main instrument of a year: TM if any Landsat 4/5, else OLI if any 8/9, else ETM+."""
    families = {SENSOR_FAMILY.get(s, s) for s in sensors}
    if "TM" in families:
        return "TM"
    if "OLI" in families:
        return "OLI"
    return "ETM+"


def switch_years(frames: list[dict]) -> list[int]:
    """Years whose main instrument differs from the year before (DR-6)."""
    switches = []
    previous = None
    for frame in sorted(frames, key=lambda f: f["year"]):
        family = sensor_family(frame["sensors"])
        if previous is not None and family != previous:
            switches.append(frame["year"])
        previous = family
    return switches


def near_switch(year: int, switches: list[int]) -> bool:
    """Is the year within SWITCH_NEAR (1) year of a sensor switch (DR-6)?"""
    for switch in switches:
        if abs(year - switch) <= SWITCH_NEAR:
            return True
    return False


def supported_after_switch(change: dict, years: np.ndarray, switches: list[int]) -> bool:
    """A change (either layer) dated at a sensor switch (±1 year) must also show in ≥ 2 later
    summers clear of every switch; otherwise the new sensor's calibration could be the change."""
    start_year = int(years[change["start"]])
    end_year = int(years[change["end"]])
    if not (near_switch(start_year, switches) or near_switch(end_year, switches)):
        return True
    midpoint = (change["before_level"] + change["after_level"]) / 2.0
    rising = change["after_level"] > change["before_level"]
    clean_summers = 0
    for row in range(change["end"], len(years)):
        if near_switch(int(years[row]), switches):
            continue
        value = change["values"][row]
        on_new_side = value > midpoint if rising else value < midpoint
        if not on_new_side:
            break
        clean_summers += 1
    return clean_summers >= PERSIST_MIN


def reread_local(change: dict, place_changes: list[dict], pixels: dict, years: np.ndarray,
                 series: dict[str, np.ndarray]) -> bool:
    """Re-reads a local built-up change through water, burns and the desert rule, like the
    place-wide one. Returns False to drop it; may re-tell it as a water change in its region."""
    if change["magnitude"] > 0:
        if was_water_share(pixels, change) >= WATER_SHARE:
            return as_local_water(change, place_changes, pixels, "water_loss")
        for other in place_changes:
            if other["kind"] == "burn" and same_place(change, other):
                return False         # burnt ground reads as built-up
        return True
    became_water = region_after_level(pixels["water"], change) >= WATER_SHARE
    if became_water:
        return as_local_water(change, place_changes, pixels, "water_gain")
    if is_arid(series):
        return False                 # desert rule: a built-up fall in arid land isn't demolition
    if greening(pixels, change) >= LOCAL_GREENING_MIN:
        return as_local_greening(change, pixels)
    return True


def as_local_greening(change: dict, pixels: dict) -> bool:
    """Re-tells a local built-up fall whose region turned green as vegetation_gain, measured on
    the region's land NDVI (after-window minus before-window). Dropped unless that rise passes
    the size test for NDVI (floor and noise of the region's own series)."""
    change["kind"] = "vegetation_gain"
    change["metric"] = "ndvi_mean"
    remeasure(change, pixels)
    if change["magnitude"] <= 0:
        return False
    return big_enough("ndvi_mean", change["magnitude"], change["values"])


def region_after_level(stack: np.ndarray, change: dict) -> float:
    """Mean of a layer over the change's region in the after-window."""
    _, _, a0, a1 = window_rows(change)
    values = region_series(stack, change["mask"])
    return float(values[a0:a1].mean())


def as_local_water(change: dict, place_changes: list[dict], pixels: dict, kind: str) -> bool:
    """Re-tells a local built-up change as a water change in the same region, measured on the
    region's water share. Dropped if a place-wide change of that kind already tells it, or if
    the water share didn't move that way by enough to pass the size test for water."""
    for other in place_changes:
        if other["kind"] == kind:
            return False
    change["kind"] = kind
    change["metric"] = "water_frac"
    remeasure(change, pixels)
    if change["magnitude"] * EXPECTED_SIGN[(kind, "water_frac")] <= 0:
        return False
    return big_enough("water_frac", change["magnitude"], change["values"])


def told_by_place(change: dict, place_changes: list[dict], years: np.ndarray) -> bool:
    """Is this local change already told by a place-wide change of the same kind, years
    and place? Then it isn't told twice."""
    for other in place_changes:
        same_kind = other["kind"] == change["kind"] and other["metric"] == change["metric"]
        if same_kind and years_touch(other, change, years) and same_place(other, change):
            return True
    return False


def box_gap(a: np.ndarray, b: np.ndarray) -> int:
    """Pixel gap between the bounding boxes of two masks (0 if they touch or overlap)."""
    rows_a, cols_a = np.nonzero(a)
    rows_b, cols_b = np.nonzero(b)
    row_gap = max(rows_b.min() - rows_a.max(), rows_a.min() - rows_b.max(), 0)
    col_gap = max(cols_b.min() - cols_a.max(), cols_a.min() - cols_b.max(), 0)
    return int(max(row_gap, col_gap))


def merge_local(changes: list[dict], pixels: dict, years: np.ndarray,
                pixel_m: float) -> list[dict]:
    """Joins same-kind local changes ≤ 500 m apart with start years ≤ 2 apart into one change,
    re-measured over the joined region, so the film doesn't get five tiny ones."""
    gap_px = MERGE_GAP_M / pixel_m
    merged: list[dict] = []
    for change in sorted(changes, key=lambda c: c["start"]):
        home = None
        for kept in merged:
            same_kind = kept["kind"] == change["kind"] and kept["metric"] == change["metric"]
            close_years = abs(int(years[kept["start"]]) - int(years[change["start"]]))
            if same_kind and close_years <= MERGE_YEARS:
                if box_gap(kept["mask"], change["mask"]) <= gap_px:
                    home = kept
                    break
        if home is None:
            merged.append(change)
            continue
        home["mask"] = home["mask"] | change["mask"]
        home["start"] = min(home["start"], change["start"])
        home["end"] = max(home["end"], change["end"])
        remeasure(home, pixels)
    return merged


def remeasure(change: dict, pixels: dict) -> None:
    """Recomputes a local change's in-region values, magnitude and persistence."""
    layer = LAYER_OF[change["metric"]]
    values = region_series(pixels[layer], change["mask"])
    b0, b1, a0, a1 = window_rows(change)
    before = float(values[max(b0, 0):b1].mean())
    after = float(values[a0:a1].mean())
    change["values"] = values
    change["before_level"] = before
    change["after_level"] = after
    change["magnitude"] = after - before
    change["persistence_years"] = persistence(values, change["end"], before, after)


# --- Step 6: honesty check and output ---

def agrees(change: dict) -> bool:
    """The kind, the metric and the sign of the magnitude must tell the same story.
    Anything else would put a contradiction into the narration, so it is dropped."""
    sign = EXPECTED_SIGN.get((change["kind"], change["metric"]))
    return sign is not None and change["magnitude"] * sign > 0


def lasting(change: dict) -> bool:
    """FR-22: ≥ 2 years after it happens. A burn may be sudden and recent (dNBR checked it)."""
    if change["kind"] == "burn":
        return change["persistence_years"] >= 1
    return change["persistence_years"] >= PERSIST_MIN


def compass_part(patch: np.ndarray) -> str:
    """Where the patch sits: 'north', 'southeast', …, 'most' (it spans the area) or '' (centre)."""
    height, width = patch.shape
    rows, cols = np.nonzero(patch)
    span_h = (rows.max() - rows.min() + 1) / height
    span_w = (cols.max() - cols.min() + 1) / width
    if span_h > 0.66 and span_w > 0.66:
        return "most"
    north_south = ["north", "", "south"][min(int(3 * rows.mean() / height), 2)]
    west_east = ["west", "", "east"][min(int(3 * cols.mean() / width), 2)]
    return north_south + west_east


def where_words(part: str, hectares: int | None) -> str:
    """'in the east part of the area', or for a local patch 'a 6 ha area in the east part'."""
    if hectares is None:
        if part == "most":
            return "across most of the area"
        if part:
            return f"in the {part} part of the area"
        return "around the centre of the area"
    if part == "most":
        return f"a {hectares} ha area spread across the place"
    if part:
        return f"a {hectares} ha area in the {part} part"
    return f"a {hectares} ha area near the centre"


def describe(patch: np.ndarray, change: dict, pixel_ha: float) -> str:
    """Plain words for where the change is (a compass part, plus the size of a local patch),
    and what the land did when the kind alone would mislead."""
    hectares = None
    if change["layer"] == "local":
        hectares = int(round(change["mask"].sum() * pixel_ha))
    where = where_words(compass_part(patch), hectares)
    if change.get("desert"):
        return f"{where}, where bare dry ground turned greener"
    if change["kind"] == "urban_growth":
        # The built-up test also passes bare soil: many patches are cleared land, not buildings.
        return f"{where}, where land was cleared or built over"
    return where


def region_bbox(patch: np.ndarray, place_bbox: list[float]) -> list[float]:
    """Pixel bounds of the patch → [W, S, E, N] inside the place bbox (row 0 = north).
    Linear in lon/lat; the area is a 6 km UTM square, so the skew is a fraction of a pixel."""
    west, south, east, north = place_bbox
    height, width = patch.shape
    rows, cols = np.nonzero(patch)
    lon_step = (east - west) / width
    lat_step = (north - south) / height
    box = [
        west + cols.min() * lon_step,
        north - (rows.max() + 1) * lat_step,
        west + (cols.max() + 1) * lon_step,
        north - rows.min() * lat_step,
    ]
    return [round(float(v), 5) for v in box]


def to_change(change: dict, number: int, years: np.ndarray, imagery: dict,
              switches: list[int], pixel_ha: float) -> dict:
    """One schema changes[] item."""
    start_year = int(years[change["start"]])
    end_year = int(years[change["end"]])
    patch = largest_patch(change["mask"]) if change["layer"] == "place" else change["mask"]
    flagged = near_switch(start_year, switches) or near_switch(end_year, switches)
    return {
        "id": f"chg-{number:02d}",
        "kind": change["kind"],
        "metric": change["metric"],
        "start_year": start_year,
        "end_year": end_year,
        "magnitude": round(float(change["magnitude"]), 4),
        # Schema 1.3: says how to read magnitude (whole area vs inside the region), and gives
        # the narration a real size to quote ("a 6-hectare area").
        "scope": "place" if change["layer"] == "place" else "local",
        # All changed pixels, not just the largest patch: the narration may quote this number,
        # so it must be the true total (QA: Lake Mead said 365 ha of ~770 ha).
        "area_ha": round(float(np.count_nonzero(change["mask"])) * pixel_ha, 1),
        "persistence_years": max(int(change["persistence_years"]), 1),
        "region": {
            "bbox": region_bbox(patch, imagery["place"]["bbox"]),
            "description": describe(patch, change, pixel_ha),
        },
        "before_year": start_year,
        "after_year": end_year,
        "mask_url": None,
        "sensor_switch_flag": flagged,
    }


def place_layer(years: np.ndarray, series: dict, pixels: dict,
                switches: list[int]) -> list[dict]:
    """Place-wide changes that are named, lasting, not just a sensor switch (DR-6) and visible
    as one big enough patch."""
    cleaned = clean_series(series)
    named = name_place_changes(series_candidates(years, series, cleaned), pixels, years, series)
    kept = []
    for change in named:
        set_levels(change, cleaned[change["metric"]])   # re-told changes: on their new series
        if not supported_after_switch(change, years, switches):
            continue
        patch = largest_patch(change["mask"])
        if lasting(change) and patch.mean() >= MIN_PATCH_SHARE:
            kept.append(change)
    kept.sort(key=lambda c: c["rank"], reverse=True)
    return kept


def local_layer(years: np.ndarray, series: dict, pixels: dict, imagery: dict,
                place_changes: list[dict], switches: list[int]) -> list[dict]:
    """Local changes that survive the sensor, water, burn and desert re-reads, merged."""
    kept = []
    for change in local_candidates(pixels, imagery["place"]):
        if not supported_after_switch(change, years, switches):
            continue                 # DR-6: dated at a sensor switch and nothing after it
        if not reread_local(change, place_changes, pixels, years, series):
            continue
        if told_by_place(change, place_changes, years):
            continue
        kept.append(change)
    pixel_m = np.sqrt(pixel_size_ha(imagery["place"], pixels["built"].shape[1:]) * 10_000.0)
    merged = merge_local(kept, pixels, years, pixel_m)
    merged.sort(key=lambda c: c["mask"].sum() * abs(c["magnitude"]), reverse=True)
    return merged


def detect(story_dir: Path) -> list[dict]:
    """Finds the place's lasting changes: place-wide first, then local (at most 5, HL-2)."""
    story_dir = Path(story_dir)
    imagery = json.loads((story_dir / "imagery.json").read_text(encoding="utf-8"))
    rows = imagery["metrics"]["series"]
    if len(rows) < 2 * WINDOW + 1:
        return []                    # too few summers to compare two windows
    years = np.array([row["year"] for row in rows])
    series = {}
    for metric in METRICS:
        series[metric] = np.array([row[metric] for row in rows], dtype=float)
    pixels = load_pixels(story_dir, [int(y) for y in years])
    switches = switch_years(imagery["frames"])

    place_changes = place_layer(years, series, pixels, switches)
    local_changes = local_layer(years, series, pixels, imagery, place_changes, switches)
    ordered = []
    for change in place_changes + local_changes:
        if agrees(change) and lasting(change):
            ordered.append(change)
    pixel_ha = pixel_size_ha(imagery["place"], pixels["built"].shape[1:])
    out = []
    for number, change in enumerate(ordered[:MAX_CHANGES], start=1):
        out.append(to_change(change, number, years, imagery, switches, pixel_ha))
    return out
