# Spike S3, approach "segments": finds lasting changes by fitting straight segments to each
# Inputs: a story folder (imagery.json metrics.series + frames, composites.npz).
# Outputs: detect(story_dir) → 0–5 changes[] items of contracts/story.schema.json.
#
# How it works:
#   1. Per metric series, estimate the year-to-year noise and remove one-year spikes.
#   2. Fit straight segments (LandTrendr-like): best split for 1..6 segments, chosen by BIC.
#   3. Walk the fitted line: neighbouring rises (or falls) join into one movement; a movement
#      bigger than max(physical floor, 4 × noise) that lasts ≥ 2 years (FR-22) is a candidate.
#   4. Find where it happened: per-pixel index change between the before and after windows.
#      The largest connected patch must cover ≥ 1% of the area (real change comes in
#      blocks; noise is scattered) → lon/lat bbox and plain words.
#   5. Name it (kind) with the cross-metric rules: shrinking lake, burn scar, desert suburb.
#   6. Flag sensor switches (DR-6), keep the 5 largest, number them.
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "backend"))
from app.pipeline.metrics import indices  # noqa: E402  (per-pixel NDVI / MNDWI / NDBI)

METRICS = ["ndvi_mean", "water_frac", "built_frac"]

# --- Thresholds (each one explained; none is tuned to a single place) ---
# Smallest change worth a chapter, in the metric's own units, before noise scaling.
# 0.03 NDVI is about the difference between a dry and a wet summer for the same vegetation;
# 0.02 of the area is ~0.7 km² of a 6 km square: a reservoir arm, a new neighbourhood.
PHYSICAL_FLOOR = {"ndvi_mean": 0.03, "water_frac": 0.02, "built_frac": 0.02}
NOISE_K = 4.0          # a change must be ≥ 4 × the series' own year-to-year noise
SPIKE_K = 3.0          # a one-year jump ≥ 3 × noise that comes back is a spike (cloud/haze)
SPIKE_AGREE = 0.5      # ... if the two neighbours agree within half the spike's size
MAX_SEGMENTS = 6       # LandTrendr's usual maximum
MIN_SEGMENT_YEARS = 3  # a segment needs ≥ 3 years to have a measurable slope
PERSIST_MIN = 2        # FR-22: a change must last ≥ 2 years after it happens
WINDOW = 3             # years averaged on each side for the per-pixel before/after maps
MAX_CHANGES = 5        # HL-2

# Per-pixel class rules, the same definitions as metrics.py (schema 1.1/1.2).
WATER_MNDWI_MIN = 0.0
BUILT_NDBI_MIN, BUILT_NDVI_MAX = 0.0, 0.2
PIXEL_NDVI_CHANGE = 0.10   # a pixel's greenness moved by ≥ 0.1: visible in the film
PIXEL_CLASS_CHANGE = 0.5   # a pixel's class share moved by ≥ half (e.g. water 3/3 → 1/3)
MIN_PATCH_SHARE = 0.01     # the largest connected patch must cover ≥ 1% of the area
# (~0.36 km² of a 6 km square). Real land change comes in blocks (a shoreline, a burn, a
# subdivision); haze, sensor and season effects flip scattered single pixels instead.

# Cross-metric rules.
DESERT_BUILT_MIN = 0.5     # over half the area "built" before = bare soil read as built
BURN_DNBR_MIN = 0.27       # USGS dNBR "moderate severity" lower bound: a real burn scar
BURN_MAX_YEARS = 2         # a burn happens within one or two summers (sudden)
OVERLAP_MIN = 0.5          # ≥ half of the new-built pixels must be the lake bed / scar

SR_SCALE, SR_OFFSET = 0.0000275, -0.2
NIR, SWIR2 = 3, 5


# --- Step 1: noise and spikes ---

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


# --- Step 2: straight-segment fit ---

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
        sxx = np.cumsum(xs * xs)
        sxy = np.cumsum(xs * ys)
        syy = np.cumsum(ys * ys)
        var_x = sxx - sx * sx / count
        cov = sxy - sx * sy / count
        var_y = syy - sy * sy / count
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
    x = years.astype(float)
    cost = line_costs(x, values)
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


def fitted_line(years: np.ndarray, values: np.ndarray, first: int, last: int) -> np.ndarray:
    """Least-squares line through one segment, evaluated at its own years."""
    x = years[first:last + 1].astype(float)
    y = values[first:last + 1]
    slope, intercept = np.polyfit(x, y, 1)
    return slope * x + intercept


# --- Step 3: segments → movements ---

def movements(years: np.ndarray, values: np.ndarray, segs: list[tuple[int, int]],
              sigma: float) -> list[dict]:
    """Rises and falls of the fitted line: each segment's slope and each jump between segments.
    Pieces smaller than the noise are ignored; same-direction pieces in a row are joined."""
    fit = np.empty_like(values)
    for first, last in segs:
        fit[first:last + 1] = fitted_line(years, values, first, last)
    pieces = []
    for idx, (first, last) in enumerate(segs):
        if idx > 0:
            prev_last = segs[idx - 1][1]
            pieces.append((prev_last, first, fit[first] - fit[prev_last]))   # jump
        pieces.append((first, last, fit[last] - fit[first]))                 # slope
    joined = []
    for begin, end, size in pieces:
        if abs(size) < sigma:
            continue
        same_way = joined and np.sign(joined[-1]["size"]) == np.sign(size)
        if same_way:
            joined[-1]["end"] = end
            joined[-1]["size"] += size
        else:
            joined.append({"begin": begin, "end": end, "size": size})
    for move in joined:
        move["fit"] = fit
    return joined


def tighten(move: dict, fit: np.ndarray) -> tuple[int, int]:
    """Trims flat ends off a movement so the years cover where the line actually moved
    (≥ 90% of the movement happens between the returned indices)."""
    begin, end = move["begin"], move["end"]
    total = fit[end] - fit[begin]
    while begin < end and abs(fit[end] - fit[begin + 1]) >= 0.9 * abs(total):
        begin += 1
        total_left = fit[end] - fit[begin]
        if abs(total_left) < 0.9 * abs(total):
            break
    while end > begin and abs(fit[end - 1] - fit[begin]) >= 0.9 * abs(total):
        end -= 1
    return begin, end


def persistence(values: np.ndarray, begin: int, end: int) -> int:
    """Years from the end of the change (inclusive) that stay past the halfway level.
    FR-22: a change that bounces straight back is not a lasting change."""
    before = values[begin]
    after = values[end]
    halfway = (before + after) / 2
    direction = np.sign(after - before)
    years = 0
    for value in values[end:]:
        if np.sign(value - halfway) != direction:
            break
        years += 1
    return years


def series_candidates(series: list[dict]) -> list[dict]:
    """All lasting movements of all three metrics, before naming them."""
    years = np.array([row["year"] for row in series])
    found = []
    for metric in METRICS:
        raw = np.array([row[metric] for row in series], dtype=float)
        sigma = noise_sigma(raw)
        clean = despike(raw, sigma)
        segs = fit_segments(years, clean)
        threshold = max(PHYSICAL_FLOOR[metric], NOISE_K * sigma)
        for move in movements(years, clean, segs, sigma):
            if abs(move["size"]) < threshold:
                continue
            begin, end = tighten(move, move["fit"])
            magnitude = raw[end] - raw[begin]       # after minus before, as on the chart
            if np.sign(magnitude) != np.sign(move["size"]) or abs(magnitude) < threshold:
                continue
            found.append({
                "metric": metric, "begin": begin, "end": end,
                "start_year": int(years[begin]), "end_year": int(years[end]),
                "magnitude": float(magnitude), "fitted_size": float(move["size"]),
                "persistence_years": persistence(clean, begin, end),
                "noise": sigma, "threshold": threshold,
            })
    return found


# --- Step 4: where it happened ---

def load_pixels(story_dir: Path) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Per-year index stacks [Y, H, W] (NaN on holes), plus NBR for the burn check."""
    data = np.load(story_dir / "composites.npz")
    years = data["years"].astype(int)
    dn = data["dn"]
    stacks = {"ndvi": [], "mndwi": [], "ndbi": [], "nbr": []}
    for i in range(len(years)):
        holder = _Composite(dn[i])
        ix = indices(holder)
        for name in ("ndvi", "mndwi", "ndbi"):
            stacks[name].append(ix[name])
        stacks["nbr"].append(_nbr(dn[i]))
    return years, {name: np.stack(arrs) for name, arrs in stacks.items()}


class _Composite:
    """Just enough of a YearComposite for metrics.indices()."""
    def __init__(self, dn: np.ndarray):
        self.dn = dn


def _nbr(dn: np.ndarray) -> np.ndarray:
    """Normalised burn ratio (NIR − SWIR2) / (NIR + SWIR2): drops sharply on burned ground."""
    nir = dn[NIR].astype(np.float32) * SR_SCALE + SR_OFFSET
    swir2 = dn[SWIR2].astype(np.float32) * SR_SCALE + SR_OFFSET
    total = nir + swir2
    out = np.full(nir.shape, np.nan, dtype=np.float32)
    np.divide(nir - swir2, total, out=out, where=total != 0)
    out[(dn == 0).any(axis=0)] = np.nan
    return out


def class_share(pixels: dict, metric: str, rows: np.ndarray) -> np.ndarray:
    """Per pixel, the share of the chosen years in which it was water / built-up,
    or its mean NDVI for ndvi_mean."""
    ndvi = pixels["ndvi"][rows]
    mndwi = pixels["mndwi"][rows]
    if metric == "ndvi_mean":
        land_ndvi = np.where(mndwi <= 0, ndvi, np.nan)   # land only, like ndvi_mean (1.2)
        with np.errstate(all="ignore"):
            return np.nanmean(land_ndvi, axis=0)
    if metric == "water_frac":
        is_class = mndwi > WATER_MNDWI_MIN
    else:
        ndbi = pixels["ndbi"][rows]
        is_class = (ndbi > BUILT_NDBI_MIN) & (ndvi < BUILT_NDVI_MAX) & (mndwi <= 0)
    seen = np.isfinite(ndvi) & np.isfinite(mndwi)
    counts = np.maximum(seen.sum(axis=0), 1)
    return (is_class & seen).sum(axis=0) / counts


def window_rows(years: np.ndarray, start_year: int, end_year: int) -> tuple:
    """Row indices of up to WINDOW years ending at start_year, and starting at end_year."""
    before = np.where((years <= start_year) & (years > start_year - WINDOW))[0]
    after = np.where((years >= end_year) & (years < end_year + WINDOW))[0]
    return before, after


def changed_pixels(pixels: dict, years: np.ndarray, cand: dict) -> np.ndarray:
    """Pixels that moved the same way as the series, by a visible amount."""
    before, after = window_rows(years, cand["start_year"], cand["end_year"])
    diff = class_share(pixels, cand["metric"], after) - class_share(pixels, cand["metric"], before)
    diff = np.nan_to_num(diff, nan=0.0)
    limit = PIXEL_NDVI_CHANGE if cand["metric"] == "ndvi_mean" else PIXEL_CLASS_CHANGE
    return diff * np.sign(cand["magnitude"]) >= limit


def largest_patch(mask: np.ndarray) -> np.ndarray:
    """The largest 4-connected group of True pixels (numpy only: each pixel takes the smallest
    label among its neighbours until nothing changes; a label is a pixel index, so a pixel
    can also jump to its label's label, which makes long winding patches converge fast)."""
    height, width = mask.shape
    big = height * width          # sentinel for background, one past the last pixel index
    labels = np.where(mask, np.arange(height * width).reshape(height, width), big)
    while True:
        spread = labels.copy()
        spread[1:, :] = np.minimum(spread[1:, :], labels[:-1, :])
        spread[:-1, :] = np.minimum(spread[:-1, :], labels[1:, :])
        spread[:, 1:] = np.minimum(spread[:, 1:], labels[:, :-1])
        spread[:, :-1] = np.minimum(spread[:, :-1], labels[:, 1:])
        spread = np.where(mask, spread, big)
        lookup = np.append(spread.ravel(), big)
        spread = lookup[spread]                 # pointer jump: my label's own label
        if np.array_equal(spread, labels):
            break
        labels = spread
    ids, counts = np.unique(labels[mask], return_counts=True)
    if len(ids) == 0:
        return mask
    return labels == ids[np.argmax(counts)]


def describe(patch: np.ndarray) -> str:
    """Plain words for where a patch is: a compass part of the area, or 'most of the area'."""
    height, width = patch.shape
    share = patch.mean()
    rows, cols = np.nonzero(patch)
    row = rows.mean() / height
    col = cols.mean() / width
    north_south = "north" if row < 1 / 3 else ("south" if row > 2 / 3 else "")
    west_east = "west" if col < 1 / 3 else ("east" if col > 2 / 3 else "")
    part = (north_south + west_east) or "centre"
    if share >= 0.4:
        return "across most of the area"
    if part == "centre":
        return "around the centre of the area"
    return f"in the {part} part of the area"


def patch_bbox(patch: np.ndarray, place_bbox: list[float]) -> list[float]:
    """Pixel bounds of the patch → [W, S, E, N] inside the place bbox (row 0 = north)."""
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
    return [round(v, 5) for v in box]


# --- Step 5: naming (kinds and cross-metric rules) ---

def overlap_share(mask: np.ndarray, other: np.ndarray) -> float:
    """Share of mask's pixels that are also in other."""
    count = mask.sum()
    return float((mask & other).sum() / count) if count else 0.0


def burn_severity(pixels: dict, years: np.ndarray, cand: dict, mask: np.ndarray) -> float:
    """Median dNBR (before − after) over the changed pixels; ≥ 0.27 looks like a burn scar."""
    before, after = window_rows(years, cand["start_year"], cand["end_year"])
    nbr_before = np.nanmean(pixels["nbr"][before], axis=0)
    nbr_after = np.nanmean(pixels["nbr"][after], axis=0)
    dnbr = (nbr_before - nbr_after)[mask]
    dnbr = dnbr[np.isfinite(dnbr)]
    return float(np.median(dnbr)) if len(dnbr) else 0.0


def base_kind(cand: dict) -> str:
    """The kind a change gets from its metric and direction alone."""
    rising = cand["magnitude"] > 0
    if cand["metric"] == "ndvi_mean":
        return "vegetation_gain" if rising else "vegetation_loss"
    if cand["metric"] == "water_frac":
        return "water_gain" if rising else "water_loss"
    return "urban_growth" if rising else "other"   # built-up falling = demolition → other


def years_overlap(a: dict, b: dict) -> bool:
    return a["start_year"] <= b["end_year"] and b["start_year"] <= a["end_year"]


def name_changes(cands: list[dict], pixels: dict, years: np.ndarray,
                 series: list[dict]) -> list[dict]:
    """Applies the kind rules. Returns the kept changes; companions of another change
    (the same event seen in a second metric) are dropped, not double-counted."""
    for cand in cands:
        cand["kind"] = base_kind(cand)
        cand["mask"] = changed_pixels(pixels, years, cand)
        sudden = cand["end_year"] - cand["start_year"] <= BURN_MAX_YEARS
        if cand["kind"] == "vegetation_loss" and sudden and cand["mask"].sum() > 0:
            if burn_severity(pixels, years, cand, cand["mask"]) >= BURN_DNBR_MIN:
                cand["kind"] = "burn"

    kept = []
    for cand in cands:
        if cand["metric"] != "built_frac":
            kept.append(cand)
            continue
        others = [c for c in cands if c is not cand and years_overlap(c, cand)]
        if _is_companion(cand, others, pixels, years, series):
            continue
        kept.append(cand)
    return kept


def _is_companion(cand: dict, others: list[dict], pixels: dict, years: np.ndarray,
                  series: list[dict]) -> bool:
    """True when a built-up change is really another event seen through the built-up index:
    shoreline exposed by a falling lake, a burn scar, or desert turned into suburbs."""
    if cand["magnitude"] > 0:
        before, _ = window_rows(years, cand["start_year"], cand["end_year"])
        was_water = class_share(pixels, "water_frac", before) >= 0.5
        if overlap_share(cand["mask"], was_water) >= OVERLAP_MIN:
            # Shrinking-lake rule: new "built-up" on the old lake bed is exposed shoreline.
            return _lake_already_reported(cand, others, series)
        for other in others:
            is_burn = other["kind"] == "burn"
            if is_burn and overlap_share(cand["mask"], other["mask"]) >= OVERLAP_MIN:
                return True   # burned ground reads as built-up (bright SWIR, low NDVI)
        return False
    # Built-up falling in a place that was mostly "built" before: bare desert soil passes
    # the built-up test, and irrigated suburbs (lawns, trees) do not. With greening at the
    # same time and place, this is development, not demolition.
    start_built = series[cand["begin"]]["built_frac"]
    if start_built < DESERT_BUILT_MIN:
        return False
    for other in others:
        greening = other["kind"] == "vegetation_gain"
        if greening and overlap_share(other["mask"], cand["mask"]) >= OVERLAP_MIN:
            _desert_development(other, cand, series)
            return True
    return False


def _lake_already_reported(cand: dict, others: list[dict], series: list[dict]) -> bool:
    """Shrinking-lake rule. True (drop the built-up change) if a water_loss already covers it;
    otherwise it becomes that water_loss, measured on water_frac over the same years."""
    for other in others:
        if other["kind"] == "water_loss":
            return True
    water_change = series[cand["end"]]["water_frac"] - series[cand["begin"]]["water_frac"]
    if water_change >= 0:
        return True   # the water share didn't fall: say nothing rather than guess
    cand["kind"] = "water_loss"
    cand["metric"] = "water_frac"
    cand["magnitude"] = float(water_change)
    return False


def _desert_development(greening: dict, built_fall: dict, series: list[dict]) -> None:
    """Turns the greening into urban_growth, timed and placed by the (clearer) built-up fall.
    The number stays a greenness change, so the chart and the script agree."""
    begin, end = built_fall["begin"], built_fall["end"]
    greening["kind"] = "urban_growth"
    greening["begin"], greening["end"] = begin, end
    greening["start_year"] = built_fall["start_year"]
    greening["end_year"] = built_fall["end_year"]
    greening["magnitude"] = float(series[end]["ndvi_mean"] - series[begin]["ndvi_mean"])
    greening["persistence_years"] = built_fall["persistence_years"]
    greening["fitted_size"] = built_fall["fitted_size"]
    greening["threshold"] = built_fall["threshold"]
    greening["mask"] = built_fall["mask"]


# --- Step 6: sensors and output ---

def sensor_family(sensors: list[str]) -> str:
    """The main instrument of a year: OLI (Landsat 8/9) > TM (4/5) > ETM+ (7)."""
    if any(s in ("Landsat 8", "Landsat 9") for s in sensors):
        return "OLI"
    if any(s in ("Landsat 4", "Landsat 5") for s in sensors):
        return "TM"
    return "ETM+"


def switch_years(frames: list[dict]) -> set[int]:
    """Years whose main instrument differs from the year before (DR-6), plus Landsat 7-only
    years, whose striped, differently calibrated images often look like change."""
    out = set()
    previous = None
    for frame in sorted(frames, key=lambda f: f["year"]):
        family = sensor_family(frame["sensors"])
        if previous is not None and family != previous:
            out.add(frame["year"])
        if family == "ETM+":
            out.add(frame["year"])
        previous = family
    return out


def near_switch(cand: dict, switches: set[int]) -> bool:
    for year in (cand["start_year"], cand["end_year"]):
        if any(abs(year - s) <= 1 for s in switches):
            return True
    return False


def to_change(cand: dict, number: int, place_bbox: list[float], switches: set[int]) -> dict:
    """One schema changes[] item."""
    patch = cand["patch"]
    bbox = patch_bbox(patch, place_bbox)
    description = describe(patch)
    return {
        "id": f"chg-{number:02d}",
        "kind": cand["kind"],
        "metric": cand["metric"],
        "start_year": cand["start_year"],
        "end_year": cand["end_year"],
        "magnitude": round(cand["magnitude"], 4),
        "persistence_years": max(int(cand["persistence_years"]), 1),
        "region": {"bbox": bbox, "description": description},
        "before_year": cand["start_year"],
        "after_year": cand["end_year"],
        "mask_url": None,
        "sensor_switch_flag": near_switch(cand, switches),
    }


def lasting(cand: dict) -> bool:
    """FR-22: ≥ 2 years after it happens. A burn is the exception: it may be sudden and the
    newest summer may be all we have of it (the dNBR check has already ruled out haze)."""
    if cand["kind"] == "burn":
        return cand["persistence_years"] >= 1
    return cand["persistence_years"] >= PERSIST_MIN


def detect(story_dir: Path) -> list[dict]:
    """Finds the place's lasting changes, largest first (at most 5, HL-2)."""
    story_dir = Path(story_dir)
    imagery = json.loads((story_dir / "imagery.json").read_text(encoding="utf-8"))
    series = imagery["metrics"]["series"]
    cands = series_candidates(series)
    if not cands:
        return []
    years, pixels = load_pixels(story_dir)
    named = name_changes(cands, pixels, years, series)
    kept = []
    for cand in named:
        cand["patch"] = largest_patch(cand["mask"])
        if lasting(cand) and cand["patch"].mean() >= MIN_PATCH_SHARE:
            kept.append(cand)
    # Largest first, measured in noise units so NDVI and area shares compare fairly.
    kept.sort(key=lambda c: abs(c["fitted_size"]) / max(c["threshold"], 1e-9), reverse=True)
    kept = kept[:MAX_CHANGES]
    switches = switch_years(imagery["frames"])
    place_bbox = imagery["place"]["bbox"]
    return [to_change(c, i + 1, place_bbox, switches) for i, c in enumerate(kept)]
