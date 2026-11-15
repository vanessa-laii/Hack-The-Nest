# Spike S3 (ruptures): finds lasting changes in a place's yearly metrics (change-point detection).
# Inputs: a story folder with imagery.json (metrics.series, frames, place.bbox) and composites.npz.
# Outputs: detect() → 0–5 story.json changes[] items, largest first (HL-2).
#
# How it works:
#   1. Measure each series' own year-to-year noise (a robust MAD of first differences).
#   2. Run PELT (ruptures, mean-shift "l2" cost) on each of ndvi_mean, water_frac, built_frac,
#      with a BIC-style penalty scaled by that noise, so it transfers to unknown places.
#   3. Turn breakpoints into shifts (mean after − mean before); merge back-to-back shifts in the
#      same direction into one gradual change (a ramp is cut into several small steps).
#   4. Keep a shift only if it is large in absolute terms (a physical minimum per metric) and
#      large against the series' noise, and lasts ≥ 2 years (FR-22; burns may be sudden).
#   5. Find where it happened: per-pixel before/after difference, thresholded, largest connected
#      area → lon/lat bbox and plain words. No coherent area (drift, haze) → the change is dropped.
#   6. Name the kind (burn, shrinking-lake rule, burnt land is not new buildings), flag sensor
#      switches (DR-6) and return the 5 largest.
from __future__ import annotations

import json
import sys
import warnings
from collections import deque
from pathlib import Path

import numpy as np
import ruptures as rpt

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "backend"))
from app.pipeline.metrics import indices  # noqa: E402  (needs backend/ on the path)
from app.pipeline.types import NIR, SR_OFFSET, SR_SCALE, SWIR2, YearComposite  # noqa: E402

METRICS = ["ndvi_mean", "water_frac", "built_frac"]

# --- Penalty ---
# PELT adds a breakpoint when it lowers the squared error by more than the penalty. For a mean
# shift in Gaussian noise of spread sigma, BIC gives pen ≈ 2 · sigma² · ln(n). Scaling by each
# series' own sigma makes the same rule work for a noisy city and a calm lake.
PENALTY_BIC_FACTOR = 2.0
MIN_SEGMENT_YEARS = 3       # a level needs 3 years: 2 could be one hazy summer and its neighbour
MAX_RAMP_PLATEAU = 5        # steps merge into one gradual change across a pause of ≤ 5 years
# Noise floor for sigma: metrics are rounded to 4 decimals and a few series are almost flat
# (water_frac on dry land). Without a floor, a 0.001 wobble would look "huge" against noise 0.
SIGMA_FLOOR = {"ndvi_mean": 0.005, "water_frac": 0.002, "built_frac": 0.003}

# --- Thresholds that a shift must pass ---
# Physical minimum jump, per metric:
#   ndvi_mean 0.05: about the gap between a healthy and a stressed summer canopy; smaller shifts
#     are within the sun-angle / phenology spread between composites.
#   water_frac 0.02 and built_frac 0.03: 2–3% of the area (≈ 0.7–1.1 km² of a 6 km square),
#     big enough to see in the film. built gets more room: its NDBI test is the noisiest.
MIN_JUMP = {"ndvi_mean": 0.05, "water_frac": 0.02, "built_frac": 0.03}
# The shift must also stand out from the year-to-year noise: 3 sigma means a single unlucky
# year can't fake it (the control has 43 years, so 2 sigma would fire by chance).
MIN_JUMP_SIGMAS = 3.0
MIN_PERSISTENCE = 2         # FR-22: holds ≥ 2 years after it happens (burns exempt)

# --- Kinds ---
# Burn: NDVI drops by ≥ 0.1, mostly within one year, and the burned area shows a dNBR ≥ 0.27
# (USGS "moderate-low severity" class). Clear-cuts can pass too; a gradual decline can't.
BURN_MIN_DROP = 0.10
BURN_SUDDEN_SHARE = 0.7     # ≥ 70% of the drop happens between two consecutive years
BURN_MIN_DNBR = 0.27
# Shrinking-lake rule: if most newly "built" pixels were water before, it is exposed shoreline
# or reclaimed land, not buildings.
WAS_WATER_SHARE = 0.5
# Burnt ground reads as "built" (low NIR, high SWIR). A built rise in the same years as a burn,
# whose new "built" pixels mostly lie inside the burned area, is the fire's scar, not buildings.
IN_BURN_SHARE = 0.5

# --- Region ---
WINDOW_YEARS = 3            # years averaged on each side, per pixel, so one bad summer can't decide
# A pixel changed when its index moved this far between the windows. NDVI 0.15 is ~2.5× the
# per-pixel year-to-year flicker on the control (≈ 0.06, ADR-002); for water and built, the
# pixel's class (water / built) flipped in most of the window years.
PIXEL_NDVI_DELTA = 0.15
PIXEL_CLASS_DELTA = 0.5
# A real land change is one place, not salt-and-pepper. The largest connected changed area must be
# ≥ 0.5% of the square (≈ 0.18 km², ~200 Landsat pixels at 6 km), or the change is dropped.
MIN_REGION_SHARE = 0.005
# ... and that area must hold ≥ 20% of the whole change (sum of per-pixel change inside it ÷ sum
# over the square). A sensor or haze shift moves every pixel a little, so its largest area holds
# almost none of it (control: 2%); a fire, a new island or a shrinking shore holds 28–90%.
MIN_EXPLAINED_SHARE = 0.2
MAX_CHANGES = 5             # HL-2


# --- Step 1–2: noise and breakpoints ---

def noise_sigma(values: np.ndarray, metric: str) -> float:
    """Robust year-to-year noise: MAD of first differences, scaled to a Gaussian sigma.
    Differences remove slow trends; MAD ignores the few real jumps."""
    diffs = np.diff(values)
    mad = np.median(np.abs(diffs - np.median(diffs)))
    sigma = 1.4826 * mad / np.sqrt(2.0)   # diff of two noisy years has √2 × the spread
    return max(float(sigma), SIGMA_FLOOR[metric])


def breakpoints(values: np.ndarray, sigma: float) -> list[int]:
    """Indices where a new level starts (PELT, l2 cost), without the final end index."""
    penalty = PENALTY_BIC_FACTOR * sigma ** 2 * np.log(len(values))
    algo = rpt.Pelt(model="l2", min_size=MIN_SEGMENT_YEARS, jump=1).fit(values.reshape(-1, 1))
    ends = algo.predict(pen=penalty)
    return ends[:-1]


def segments(n: int, breaks: list[int]) -> list[tuple[int, int]]:
    """[(start, stop)) index ranges between breakpoints."""
    edges = [0] + list(breaks) + [n]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


# --- Step 3: shifts ---

def shifts(values: np.ndarray, segs: list[tuple[int, int]]) -> list[dict]:
    """One shift per run of same-direction level changes. A ramp cut into several steps by PELT
    becomes one gradual change, from the last year of the first level to the first of the last."""
    means = [float(values[a:b].mean()) for a, b in segs]
    out = []
    i = 0
    while i < len(segs) - 1:
        direction = np.sign(means[i + 1] - means[i])
        j = i + 1
        while j < len(segs) - 1:
            same_way = np.sign(means[j + 1] - means[j]) == direction
            pause = segs[j][1] - segs[j][0]
            if not same_way or pause > MAX_RAMP_PLATEAU:
                break
            j += 1
        out.append({"before_seg": segs[i], "after_seg": segs[j],
                    "magnitude": means[j] - means[i]})
        i = j
    return out


def persistence(values: np.ndarray, end_idx: int, before: float, after: float) -> int:
    """Years from the change's end on, in a row, that stay nearer the new level than the old."""
    years = 0
    for value in values[end_idx:]:
        if abs(value - after) <= abs(value - before):
            years += 1
        else:
            break
    return years


def largest_one_year_step(values: np.ndarray, start_idx: int, end_idx: int) -> float:
    """The biggest single-year move inside [start, end], signed like the overall change."""
    steps = np.diff(values[start_idx:end_idx + 1])
    return float(steps.min()) if len(steps) else 0.0


# --- Step 5: region ---

def load_cube(story_dir: Path) -> tuple[np.ndarray, np.ndarray]:
    """Years [Y] and raw composites [Y, 6, H, W] (0 = hole)."""
    with np.load(story_dir / "composites.npz") as npz:
        return npz["years"].astype(int), npz["dn"]


def pixel_layer(dn: np.ndarray, metric: str) -> np.ndarray:
    """The per-pixel version of a metric for one year: NDVI, or a 0/1 water or built class."""
    ix = indices(YearComposite(year=0, dn=dn, clear_pct=100.0, scenes=[]))
    if metric == "ndvi_mean":
        layer = ix["ndvi"].copy()
        layer[ix["mndwi"] > 0] = np.nan          # land only, like ndvi_mean
        return layer
    clear = np.isfinite(ix["ndvi"]) & np.isfinite(ix["mndwi"]) & np.isfinite(ix["ndbi"])
    if metric == "water_frac":
        cls = ix["mndwi"] > 0
    else:  # same definition as metrics.py built_frac
        cls = (ix["ndbi"] > 0) & (ix["ndvi"] < 0.2) & (ix["mndwi"] <= 0)
    layer = cls.astype(np.float32)
    layer[~clear] = np.nan
    return layer


def window_mean(cube: np.ndarray, cube_years: np.ndarray, years: list[int], fn) -> np.ndarray:
    """Per-pixel mean of fn(dn) over the given years, ignoring holes."""
    stack = []
    for year in years:
        idx = int(np.nonzero(cube_years == year)[0][0])
        stack.append(fn(cube[idx]))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # a pixel that is a hole every year
        return np.nanmean(np.stack(stack), axis=0)


def largest_component(mask: np.ndarray) -> np.ndarray:
    """The largest 8-connected True area of a mask (plain BFS, numpy only)."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best: list[tuple[int, int]] = []
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        seen[y0, x0] = True
        queue = deque([(y0, x0)])
        area = []
        while queue:
            y, x = queue.popleft()
            area.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    inside = 0 <= ny < h and 0 <= nx < w
                    if inside and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        queue.append((ny, nx))
        if len(area) > len(best):
            best = area
    out = np.zeros_like(mask, dtype=bool)
    if best:
        rows, cols = zip(*best)
        out[list(rows), list(cols)] = True
    return out


def describe(component: np.ndarray) -> str:
    """Plain words for where an area sits in the square and how big it is."""
    h, w = component.shape
    rows, cols = np.nonzero(component)
    cy = rows.mean() / h
    cx = cols.mean() / w
    ns = "north" if cy < 1 / 3 else ("south" if cy > 2 / 3 else "")
    ew = "west" if cx < 1 / 3 else ("east" if cx > 2 / 3 else "")
    where = f"the {ns}{ew} side" if (ns or ew) else "the centre"
    spread = (rows.max() - rows.min() + 1) * (cols.max() - cols.min() + 1) / component.size
    if spread > 0.5:                    # its outline spans over half the square
        where = "across most of the area"
    share = 100.0 * component.sum() / component.size
    if share < 1:
        return f"{where} (under 1% of the area)"
    return f"{where} (about {share:.0f}% of the area)"


def to_bbox(component: np.ndarray, bbox: list[float]) -> list[float]:
    """Pixel extent → [W, S, E, N]; row 0 is north. Linear in lon/lat, clamped to the place."""
    west, south, east, north = bbox
    h, w = component.shape
    rows, cols = np.nonzero(component)
    lon_w = west + (east - west) * cols.min() / w
    lon_e = west + (east - west) * (cols.max() + 1) / w
    lat_n = north - (north - south) * rows.min() / h
    lat_s = north - (north - south) * (rows.max() + 1) / h
    box = [max(lon_w, west), max(lat_s, south), min(lon_e, east), min(lat_n, north)]
    return [round(float(v), 5) for v in box]


def pixel_delta(cube, cube_years, cand: dict) -> np.ndarray:
    """Per-pixel move between the before and after windows, positive in the change's direction."""
    metric = cand["metric"]
    fn = lambda dn: pixel_layer(dn, metric)  # noqa: E731
    before = window_mean(cube, cube_years, cand["before_years"], fn)
    after = window_mean(cube, cube_years, cand["after_years"], fn)
    return np.nan_to_num((after - before) * np.sign(cand["magnitude"]))  # hole → no change


def locate(cube, cube_years, cand: dict) -> np.ndarray | None:
    """The largest connected changed area, or None if the change has no coherent place."""
    delta = pixel_delta(cube, cube_years, cand)
    limit = PIXEL_NDVI_DELTA if cand["metric"] == "ndvi_mean" else PIXEL_CLASS_DELTA
    component = largest_component(delta >= limit)
    if component.sum() < MIN_REGION_SHARE * component.size:
        return None
    total = float(delta.sum())
    explained = float(delta[component].sum()) / total if total > 0 else 0.0
    if explained < MIN_EXPLAINED_SHARE:
        return None                       # spread thinly everywhere: drift, not a place
    return component


# --- Step 6: kinds and flags ---

def nbr(dn: np.ndarray) -> np.ndarray:
    """Normalised burn ratio (NIR − SWIR2) / (NIR + SWIR2), NaN on holes."""
    refl = dn.astype(np.float32) * np.float32(SR_SCALE) + np.float32(SR_OFFSET)
    num = refl[NIR] - refl[SWIR2]
    den = refl[NIR] + refl[SWIR2]
    out = np.full(num.shape, np.nan, dtype=np.float32)
    np.divide(num, den, out=out, where=den != 0)
    out[(dn == 0).any(axis=0)] = np.nan
    return out


def generation(sensors: list[str]) -> str:
    """Which instrument family a year's composite mostly comes from."""
    if "Landsat 8" in sensors or "Landsat 9" in sensors:
        return "OLI"
    if "Landsat 4" in sensors or "Landsat 5" in sensors:
        return "TM"
    return "ETM+"   # Landsat 7 only, e.g. 2012 (striped)


def switch_years(frames: list[dict]) -> list[int]:
    """Years whose instrument family differs from the year before (DR-6)."""
    out = []
    ordered = sorted(frames, key=lambda f: f["year"])
    for prev, cur in zip(ordered, ordered[1:]):
        if generation(prev["sensors"]) != generation(cur["sensors"]):
            out.append(cur["year"])
    return out


def years_overlap(a: dict, b: dict) -> bool:
    return a["start_year"] <= b["end_year"] and b["start_year"] <= a["end_year"]


def share_of(component: np.ndarray, layer: np.ndarray, test) -> float:
    """Share of a component's pixels whose before-layer passes test."""
    values = layer[component]
    values = values[np.isfinite(values)]
    return float(np.mean(test(values))) if len(values) else 0.0


# --- Main ---

def candidates(series: list[dict], metric: str) -> list[dict]:
    """Breakpoints → shifts that pass the size, noise and persistence tests (no pixels yet)."""
    years = [row["year"] for row in series]
    values = np.array([row[metric] for row in series], dtype=float)
    sigma = noise_sigma(values, metric)
    segs = segments(len(values), breakpoints(values, sigma))
    out = []
    for shift in shifts(values, segs):
        magnitude = shift["magnitude"]
        big_enough = abs(magnitude) >= MIN_JUMP[metric]
        above_noise = abs(magnitude) >= MIN_JUMP_SIGMAS * sigma
        if not (big_enough and above_noise):
            continue
        b0, b1 = shift["before_seg"]
        a0, a1 = shift["after_seg"]
        start_idx = b1 - 1                     # last year of the old level
        end_idx = a0                           # first year of the new level
        before_mean = float(values[b0:b1].mean())
        after_mean = float(values[a0:a1].mean())
        step = largest_one_year_step(values * np.sign(-magnitude), start_idx, end_idx)
        sudden = -step >= BURN_SUDDEN_SHARE * abs(magnitude)
        out.append({
            "metric": metric, "magnitude": magnitude, "sigma": sigma,
            "start_year": years[start_idx], "end_year": years[end_idx],
            "persistence_years": persistence(values, end_idx, before_mean, after_mean),
            "before_years": years[max(b0, b1 - WINDOW_YEARS):b1],
            "after_years": years[a0:min(a1, a0 + WINDOW_YEARS)],
            "sudden": bool(sudden),
        })
    return out


def kind_for(cand: dict) -> str:
    """The plain kind from the metric and direction (refined later by pixel rules)."""
    up = cand["magnitude"] > 0
    if cand["metric"] == "ndvi_mean":
        return "vegetation_gain" if up else "vegetation_loss"
    if cand["metric"] == "water_frac":
        return "water_gain" if up else "water_loss"
    return "urban_growth" if up else "other"   # built falling = demolition → other


def detect(story_dir: Path) -> list[dict]:
    story_dir = Path(story_dir)
    meta = json.loads((story_dir / "imagery.json").read_text(encoding="utf-8"))
    series = meta["metrics"]["series"]
    bbox = meta["place"]["bbox"]
    switches = switch_years(meta["frames"])
    cube_years, cube = load_cube(story_dir)

    found = []
    for metric in METRICS:
        for cand in candidates(series, metric):
            cand["kind"] = kind_for(cand)
            component = locate(cube, cube_years, cand)
            if component is None:
                continue
            cand["component"] = component
            found.append(cand)

    found = apply_kind_rules(found, cube, cube_years)
    kept = []
    for cand in found:
        lasting = cand["persistence_years"] >= MIN_PERSISTENCE
        if lasting or cand["kind"] == "burn":   # FR-22, burns may be sudden
            kept.append(cand)

    # Largest first, by size against the metric's own minimum, so metrics compare fairly.
    kept.sort(key=lambda c: abs(c["magnitude"]) / MIN_JUMP[c["metric"]], reverse=True)
    out = []
    for n, cand in enumerate(kept[:MAX_CHANGES], start=1):
        near_switch = any(abs(cand["start_year"] - s) <= 1 or abs(cand["end_year"] - s) <= 1
                          for s in switches)
        out.append({
            "id": f"chg-{n:02d}",
            "kind": cand["kind"],
            "metric": cand["metric"],
            "start_year": int(cand["start_year"]),
            "end_year": int(cand["end_year"]),
            "magnitude": round(float(cand["magnitude"]), 4),
            "persistence_years": max(1, int(cand["persistence_years"])),
            "region": {"bbox": to_bbox(cand["component"], bbox),
                       "description": describe(cand["component"])},
            "before_year": int(cand["start_year"]),
            "after_year": int(cand["end_year"]),
            "mask_url": None,
            "sensor_switch_flag": bool(near_switch),
        })
    return out


def apply_kind_rules(found: list[dict], cube, cube_years) -> list[dict]:
    """Burn test (dNBR), shrinking-lake rule and burn-scar rule, using the before-window pixels."""
    for cand in found:
        if cand["kind"] == "vegetation_loss" and cand["sudden"] \
                and -cand["magnitude"] >= BURN_MIN_DROP:
            before = window_mean(cube, cube_years, cand["before_years"], nbr)
            after = window_mean(cube, cube_years, cand["after_years"], nbr)
            dnbr = np.nanmedian((before - after)[cand["component"]])
            if dnbr >= BURN_MIN_DNBR:
                cand["kind"] = "burn"

    burns = [c for c in found if c["kind"] == "burn"]
    kept = []
    for cand in found:
        if cand["kind"] != "urban_growth":
            kept.append(cand)
            continue
        water_before = window_mean(cube, cube_years, cand["before_years"],
                                   lambda dn: pixel_layer(dn, "water_frac"))
        was_water = share_of(cand["component"], water_before, lambda v: v >= 0.5)
        if was_water >= WAS_WATER_SHARE:
            # Shrinking-lake rule: new land where water was. Kept as water_loss only if no
            # water_frac change already tells the story for these years.
            told = any(c["kind"] == "water_loss" and years_overlap(c, cand) for c in found)
            if not told:
                cand["kind"] = "water_loss"
                kept.append(cand)
            continue
        scar = False
        for burn in burns:
            inside = float(np.mean(burn["component"][cand["component"]]))
            if years_overlap(burn, cand) and inside >= IN_BURN_SHARE:
                scar = True
        if scar:
            continue                           # the burn scar, already told as the burn
        kept.append(cand)
    return kept
