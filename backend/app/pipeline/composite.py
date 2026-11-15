# Builds one clean median image per summer from that summer's Landsat scenes, with the S2b fixes.
# Inputs: {year: (scenes uint16 [n, 7, H, W] in types.BANDS order, [SceneMeta] same order)}.
# Outputs: {year: YearComposite}: dn uint16 [6, H, W] (0 = hole), clear_pct, scenes used, notes.
#
# How it works, for one summer:
#   1. Merge same-day scenes: two neighbouring rows of one satellite pass count as one day.
#   2. Mask: remove pixels with no data, cloud, cirrus, cloud shadow or snow (QA bits).
#   3. Guard bright surfaces (S2b fix): a bright, steady surface that QA calls "cloud" in nearly
#      every scene of this summer AND a neighbouring summer (e.g. a new concrete bridge deck)
#      keeps its pixels. That is why composite_series looks at every summer first.
#   4. Choose scenes: rank by scene-wide cloud cover and take the best few. Hazy days (S2b fix,
#      dense vegetation looks too bright) only fill pixels that no clearer chosen day sees.
#   5. Drop outliers (S2b fix): per pixel, drop a day whose blue is far above that pixel's usual
#      value. That is a white cloud QA missed; a roof that is bright every day is never dropped.
#   6. Top up if holes: if too many pixels have no usable day, add more scenes (up to max_n).
#   7. Median: per pixel and band, the median of the usable days. Holes stay 0: nothing is
#      ever borrowed from another summer.
#   8. Stats and notes: clear_pct, the scenes that supplied pixels, and plain-words notes.
#
# Decisions: docs/decisions/ADR-002-composite.md (median, full mask, 4 → 6 top-up, no borrowing
# between years) and spikes/s2b-mask/ (the three fixes; evidence in results*.json).
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from app.pipeline.types import (
    BANDS, BLUE, GREEN, NIR, QA, RED, SR_OFFSET, SR_SCALE, SceneMeta, YearComposite,
    scene_rank_score,
)

# --- QA_PIXEL bits (Landsat Collection 2) ---

# Bit 0: the pixel has no data at all.
FILL_BIT = 1 << 0
# Bits 1–5: dilated cloud edge, cirrus, cloud, cloud shadow, snow (ADR-002 masks all five).
DILATED_BIT, CIRRUS_BIT, CLOUD_BIT, SHADOW_BIT, SNOW_BIT = (1 << b for b in range(1, 6))
FLAG_BITS = DILATED_BIT | CIRRUS_BIT | CLOUD_BIT | SHADOW_BIT | SNOW_BIT
# Bit 7 (water) is deliberately NOT masked: water is a real surface, not a cloud.
WATER_BIT = 1 << 7
# 65535 means "no value": it sorts last in the median, and as a reflectance it is invalid.
NODATA = 65535


def _dn(refl_delta: float) -> int:
    """A reflectance difference as a raw DN difference (the offset cancels).
    The pixel work stays in integers for speed; only thresholds are written in reflectance."""
    return int(round(refl_delta / SR_SCALE))


def _dn_abs(refl: float) -> int:
    """An absolute reflectance as a raw DN (the inverse of the DR-1 scaling)."""
    return int(round((refl - SR_OFFSET) / SR_SCALE))


# --- S2b thresholds (tuned on Delray, Willow Run and Central Park; see spikes/s2b-mask/) ---

# Bright-surface guard: the Gordie Howe bridge deck was flagged as cloud in every scene.
# 75% of scenes: a surface is flagged nearly every time; a real cloud only now and then.
GUARD_FLAG_FRAC = 0.75
# 2 scenes: one scene alone can't show that something is "always there".
GUARD_MIN_OBS = 2
# 0.08 blue: concrete and roofs are ~0.10–0.20; vegetation and water (~0.02–0.06) never qualify.
GUARD_BRIGHT = 0.08
# ±0.05 blue: a deck looks the same every day (spread 0.01–0.06); real cloud varies 0.1–0.25.
GUARD_STABLE_TOL = 0.05
# 75%: three in four of the summer's days must sit within that ±0.05 band to count as steady.
GUARD_STABLE_FRAC = 0.75
# 2 years: the confirming summer may be up to 2 years away, which bridges the 2012 data gap.
GUARD_NEIGHBOUR_GAP = 2
# 0.10 blue: both summers' typical brightness must agree: the same surface, not two clouds.
GUARD_NEIGHBOUR_TOL = 0.10
# 50%: edge pixels need a flag only half the time; the false cloud's "shadow" moves with the sun.
GUARD_RING_FLAG_FRAC = 0.5
# 6 px: QA's ~3 px cloud ring plus the shadow beside it (4 px left 3.5% holes in 2024, 6 px 1.8%).
GUARD_RING_PX = 6
# 0.2 blue: edge pixels must be darker than any cloud top (~0.3–0.9) on a typical day.
GUARD_RING_MAX_BLUE = 0.2

# Outlier rejection: unflagged white cloud at Willow Run 2020.
# +0.08 blue: a missed cloud is far brighter than the same ground on other days; roofs stay within.
OUTLIER_BLUE = 0.08

# Haze: the washed-out Delray 2021 frame.
# NDVI > 0.5 = dense vegetation, a dark target whose extra brightness reveals haze (not water).
HAZE_DDV_NDVI = 0.5
# 2% of the area: with less dense vegetation than that (deserts), a haze score would be noise.
HAZE_MIN_DARK_FRAC = 0.02
# +0.02 blue+green above the clearest day = hazy (Delray 2021-07-19: +0.022; 10–14% elsewhere).
HAZE_EXCESS = 0.02


@dataclass
class _Pass:
    """One satellite pass over the area: same-day scenes from neighbouring rows merged into one."""
    data: np.ndarray          # uint16 [7, H, W]
    members: list[SceneMeta]  # the original scenes merged into this pass
    src: np.ndarray           # int8 [H, W]: which member each pixel came from


@dataclass
class _GuardStats:
    """One summer's evidence for the bright-surface guard."""
    core: np.ndarray       # bool [H, W]: flagged cloud nearly always, bright and steady
    ring: np.ndarray       # bool [H, W]: flagged often, not cloud-bright (possible surface edge)
    blue_med: np.ndarray   # uint16 [H, W]: median blue over the summer's days with data


# --- Shared helpers ---

def _masked_median(vals: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-pixel median over axis 0 of the valid values (uint16; 0 where none), plus the count.
    Fast because it stays uint16 and sorts once: invalid values become 65535 and sort last.
    An even count averages the middle two, rounding half to even like np.rint."""
    n_obs = vals.shape[0]
    if valid.ndim < vals.ndim:
        valid = valid[:, None]  # one mask per pixel applies to every band
    valid = np.broadcast_to(valid, vals.shape)
    ordered = np.where(valid, vals, np.uint16(NODATA))
    ordered.sort(axis=0)
    count = np.count_nonzero(valid, axis=0)
    lower_middle = np.maximum((count - 1) // 2, 0)[None]
    upper_middle = np.minimum(count // 2, n_obs - 1)[None]
    total = np.take_along_axis(ordered, lower_middle, 0)[0].astype(np.uint32)
    total += np.take_along_axis(ordered, upper_middle, 0)[0]
    median = total >> 1
    exactly_half_way = total & 1
    median += exactly_half_way & (median & 1)  # x.5 rounds to the even neighbour
    median[count == 0] = 0
    return median.astype(np.uint16), count


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Grow a True/False mask by `radius` pixels in a square (shifted ORs: rows, then columns).
    Hand-rolled so we don't add scipy for one small operation."""
    if radius <= 0 or not mask.any():
        return mask.copy()
    grown = mask.copy()
    for d in range(1, radius + 1):
        grown[d:] |= mask[:-d]
        grown[:-d] |= mask[d:]
    grown_rows = grown.copy()
    for d in range(1, radius + 1):
        grown[:, d:] |= grown_rows[:, :-d]
        grown[:, :-d] |= grown_rows[:, d:]
    return grown


# --- Step 1: merge same-day scenes ---

def _merge_passes(scenes: np.ndarray, metas: list[SceneMeta]) -> list[_Pass]:
    """Merge scenes taken on the same day by the same satellite into one pass, in time order.
    Otherwise that one day (perhaps a hazy one) would get a double vote in the median.
    Per pixel, take the first member that is clear, else the first with data, else fill."""
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, scene_meta in enumerate(metas):
        groups[(scene_meta.platform, scene_meta.date)].append(i)
    passes = []
    for members in sorted(groups.values(), key=lambda g: metas[g[0]].datetime):
        stack = scenes[members]
        member_metas = [metas[i] for i in members]
        if len(members) == 1:
            from_first = np.zeros(stack.shape[-2:], np.int8)
            passes.append(_Pass(stack[0], member_metas, from_first))
            continue
        has_data = ~_fill_mask(stack)
        is_clear = has_data & ~_flag_mask(stack)
        score = has_data.astype(np.int8) + is_clear     # 2 clear, 1 flagged, 0 no data
        best_member = np.argmax(score, axis=0).astype(np.int8)  # argmax picks the first best
        pick = best_member[None, None].astype(np.intp)
        merged = np.take_along_axis(stack, pick, 0)[0]
        passes.append(_Pass(merged, member_metas, best_member))
    return passes


# --- Step 2: mask ---

def _fill_mask(stack: np.ndarray) -> np.ndarray:
    """True where a pixel has no usable data, [k, H, W]; always masks the whole pixel (ADR-002).
    Kept apart from cloud flags: missing data can't be rescued, a wrong cloud flag can."""
    qa = stack[:, QA]
    reflectance = stack[:, :QA]
    qa_missing = qa == 0
    fill_bit_set = (qa & FILL_BIT) != 0
    any_band_zero = (reflectance == 0).any(1)
    any_band_saturated = (reflectance == NODATA).any(1)
    return qa_missing | fill_bit_set | any_band_zero | any_band_saturated


def _flag_mask(stack: np.ndarray) -> np.ndarray:
    """True where QA flags dilated cloud, cirrus, cloud, shadow or snow (bits 1–5), [k, H, W]."""
    return (stack[:, QA] & FLAG_BITS) != 0


# --- Step 3: guard bright surfaces ---

def _guard_stats(stack: np.ndarray) -> _GuardStats:
    """Find pixels that, this summer, look like a real surface mislabelled as cloud.
    A cloud drifts and changes brightness between days; a concrete deck gives about the
    same blue every time (measured IQR 0.01–0.06 on Delray roofs, 0.1–0.25 for real cloud)."""
    has_data = ~_fill_mask(stack)
    flagged = _flag_mask(stack)
    n_days = has_data.sum(0)
    blue = stack[:, BLUE]
    blue_med, _ = _masked_median(blue, has_data)

    distance_from_median = np.abs(blue.astype(np.int32) - blue_med.astype(np.int32))
    near_median = has_data & (distance_from_median <= _dn(GUARD_STABLE_TOL))
    steady = near_median.sum(0) >= GUARD_STABLE_FRAC * n_days
    enough_days = n_days >= GUARD_MIN_OBS
    cloud_flagged = ((stack[:, QA] & CLOUD_BIT) != 0) & has_data
    cloud_nearly_always = cloud_flagged.sum(0) >= GUARD_FLAG_FRAC * n_days
    bright = blue_med >= _dn_abs(GUARD_BRIGHT)
    core = enough_days & steady & cloud_nearly_always & bright

    # Edge pixels (part deck, part water) mix two surfaces, so their brightness shifts with
    # sub-pixel alignment and fails the steadiness test. They only need to be flagged often and
    # not cloud-bright; any one-off real cloud there is removed later by outlier rejection.
    flagged_often = (flagged & has_data).sum(0) >= GUARD_RING_FLAG_FRAC * n_days
    not_cloud_bright = blue_med < _dn_abs(GUARD_RING_MAX_BLUE)
    ring = enough_days & flagged_often & not_cloud_bright
    return _GuardStats(core, ring, blue_med)


def _neighbour_years(year: int, years: list[int]) -> list[int]:
    """The closest earlier and closest later summer within GUARD_NEIGHBOUR_GAP years."""
    before = [y for y in years if year - GUARD_NEIGHBOUR_GAP <= y < year]
    after = [y for y in years if year < y <= year + GUARD_NEIGHBOUR_GAP]
    neighbours = []
    if before:
        neighbours.append(max(before))
    if after:
        neighbours.append(min(after))
    return neighbours


def _confirm_guard(year: int, stats: dict[int, _GuardStats]) -> np.ndarray:
    """Pixels whose cloud flags are ignored this summer: a core that a neighbouring summer
    confirms (same pixel, similar brightness), plus that core's often-flagged edge ring.
    One summer alone can't tell a surface from a cloud that happened to sit there every time."""
    this_year = stats[year]
    if not this_year.core.any():
        return np.zeros_like(this_year.core)
    confirmed = np.zeros_like(this_year.core)
    for other_year in _neighbour_years(year, list(stats)):
        other = stats[other_year]
        blue_gap = np.abs(this_year.blue_med.astype(np.int32) - other.blue_med.astype(np.int32))
        same_brightness = blue_gap <= _dn(GUARD_NEIGHBOUR_TOL)
        confirmed |= this_year.core & other.core & same_brightness
    if not confirmed.any():
        return confirmed
    near_confirmed = _dilate(confirmed, GUARD_RING_PX)
    return confirmed | (this_year.ring & near_confirmed)


def _apply_guard(clear: np.ndarray, guard: np.ndarray, fill: np.ndarray) -> np.ndarray:
    """Count guarded pixels as clear on every day they have data, whatever QA says."""
    guarded_with_data = guard[None] & ~fill
    return clear | guarded_with_data


# --- Step 4: choose scenes ---

def _rank_passes(passes: list[_Pass], clear: np.ndarray) -> list[int]:
    """Pass indices, best first: cloud cover with Tier 2 scenes pushed back (scene_rank_score),
    then most clear area. Passes with no clear pixel are left out."""
    clear_frac = clear.mean(axis=(1, 2))
    # Ranking by in-area clearness and haze first was tried in spikes/s2b-mask/ and rejected:
    # it let in odd days (e.g. Central Park 1993-08-22) and raised year-to-year NDVI flicker
    # from 0.015 to 0.018–0.020. Haze is handled by fallback-only use instead.
    def rank_key(i: int) -> tuple:
        # Tier 2 is pushed back; stripes are not (see scene_rank_score for the measured reason).
        best_member = min(scene_rank_score(m, penalise_stripes=False) for m in passes[i].members)
        return (best_member, -clear_frac[i])

    ranked = sorted(range(len(passes)), key=rank_key)
    return [i for i in ranked if clear_frac[i] > 0]


def _dense_vegetation(data: np.ndarray, clear: np.ndarray) -> np.ndarray:
    """True where the summer-median image is dense vegetation (NDVI above HAZE_DDV_NDVI).
    The NDVI test also excludes water, whose colour changes with sediment and glint."""
    red_med, count = _masked_median(data[:, RED], clear)
    nir_med, _ = _masked_median(data[:, NIR], clear)
    red = red_med.astype(np.float32) * SR_SCALE + SR_OFFSET
    nir = nir_med.astype(np.float32) * SR_SCALE + SR_OFFSET
    with np.errstate(invalid="ignore", divide="ignore"):
        ndvi = (nir - red) / (nir + red)
    return (count > 0) & (ndvi > HAZE_DDV_NDVI)


def _haze_scores(data: np.ndarray, clear: np.ndarray) -> np.ndarray:
    """Per pass: how much brighter dense vegetation is in blue+green than its own summer median
    (reflectance, median over pixels). NaN when a pass sees too little vegetation.
    Blue AND green: Delray's hazy 2021-07-19 rose +0.015 in blue but +0.036 in green."""
    n_passes = data.shape[0]
    height, width = data.shape[-2:]
    min_pixels = HAZE_MIN_DARK_FRAC * height * width
    scores = np.full(n_passes, np.nan)
    vegetation = _dense_vegetation(data, clear)
    if vegetation.sum() < min_pixels:
        return scores  # e.g. a desert: haze never decides there
    # Compare each pixel with its OWN summer median: an area mean would depend on which
    # pixels a pass happens to see (clouds over the park would make a pass look clearer).
    brightening = np.zeros((n_passes, height, width), np.float32)
    for band in (BLUE, GREEN):
        band_med, _ = _masked_median(data[:, band], clear)
        excess_dn = data[:, band].astype(np.int32) - band_med.astype(np.int32)
        brightening += excess_dn.astype(np.float32) * (SR_SCALE / 2)  # mean of the two bands
    for i in range(n_passes):
        seen_vegetation = clear[i] & vegetation
        if seen_vegetation.sum() >= min_pixels:
            scores[i] = float(np.median(brightening[i][seen_vegetation]))
    return scores


def _haze_excess(data: np.ndarray, clear: np.ndarray) -> np.ndarray:
    """Per pass: haze score minus the clearest pass's score (0 where unknown)."""
    scores = _haze_scores(data, clear)
    known = np.isfinite(scores)
    clearest = np.nanmin(scores) if known.any() else np.nan
    return np.where(known, scores - clearest, 0.0)


def _hazy_only_as_fallback(clear_chosen: np.ndarray, hazy_chosen: np.ndarray) -> np.ndarray:
    """Hazy passes keep only pixels that no clear chosen pass sees.
    A hazy day in the median would veil every pixel it touches."""
    usable = clear_chosen.copy()
    if hazy_chosen.any() and not hazy_chosen.all():
        seen_by_clear_pass = usable[~hazy_chosen].any(0)
        usable[hazy_chosen] &= ~seen_by_clear_pass[None]
    return usable


# --- Step 5: drop outliers ---

def _reject_outliers(blue: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Drop days whose blue is more than OUTLIER_BLUE above that pixel's median; returns new mask.
    Only values above the median go, so a pixel keeps its median days and never becomes a hole.
    With 2 days, a cloud > 2 × 0.08 brighter than the ground is still caught."""
    blue_med, count = _masked_median(blue, valid)
    limit = blue_med.astype(np.int32) + _dn(OUTLIER_BLUE)
    too_bright = valid & (blue.astype(np.int32) > limit) & (count >= 2)
    return valid & ~too_bright


def _usable_days(chosen: list[int], clear: np.ndarray, hazy: np.ndarray,
                 blue: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For the chosen passes: the usable-day mask before and after outlier rejection."""
    before_outliers = _hazy_only_as_fallback(clear[chosen], hazy[chosen])
    after_outliers = _reject_outliers(blue[chosen], before_outliers)
    return before_outliers, after_outliers


# --- Step 6: top up if holes ---

def _top_up(order: list[int], target_n: int, max_n: int, hole_topup_pct: float,
            clear: np.ndarray, hazy: np.ndarray, blue: np.ndarray):
    """Take the best target_n passes; while holes exceed hole_topup_pct %, add the next-best
    pass that can fill a hole, up to max_n (ADR-002). Returns the chosen passes, usable-day
    masks before and after outlier rejection, the holes, and the hole % before topping up."""
    chosen = order[:target_n]
    before_outliers, usable = _usable_days(chosen, clear, hazy, blue)
    holes = ~usable.any(0)
    holes_before_pct = 100 * holes.mean()
    for candidate in order[target_n:]:
        if 100 * holes.mean() <= hole_topup_pct or len(chosen) >= max_n:
            break
        if not (clear[candidate] & holes).any():
            continue  # can't fill any hole; adding it would only reshuffle the median
        chosen = chosen + [candidate]
        before_outliers, usable = _usable_days(chosen, clear, hazy, blue)
        holes = ~usable.any(0)
    return chosen, before_outliers, usable, holes, holes_before_pct


# --- Step 8: stats and notes ---

def _guard_note(guard: np.ndarray, flagged: np.ndarray, fill: np.ndarray) -> str:
    """Note for the bright-surface guard: its area and the share of pixels it gave days back to."""
    rescued = guard[None] & flagged & ~fill
    return (f"Bright-surface guard: {100 * guard.mean():.2f}% of the area (a bright surface "
            f"flagged as cloud in most scenes of this and a neighbouring summer with steady "
            f"brightness, e.g. new concrete, plus its flagged edge) had its cloud flags ignored; "
            f"{100 * rescued.any(0).mean():.2f}% of pixels gained observations.")


def _topup_note(n_added: int, holes_before_pct: float, holes: np.ndarray) -> str:
    """Note for the hole top-up."""
    return (f"Topped up with {n_added} more scene(s) to fill holes: {holes_before_pct:.1f}% → "
            f"{100 * holes.mean():.1f}% of the area.")


def _outlier_note(dropped: np.ndarray) -> str:
    """Note for outlier rejection: how many days were dropped, over how much of the area."""
    return (f"Outlier rejection: dropped {int(dropped.sum())} cloud-bright observation(s) the QA "
            f"mask missed (blue > {OUTLIER_BLUE} above the pixel's summer median), changing "
            f"{100 * dropped.any(0).mean():.2f}% of pixels.")


def _haze_note(hazy_positions: list[int], chosen: list[int], passes: list[_Pass],
               excess: np.ndarray, usable: np.ndarray) -> str:
    """Note for hazy passes that were chosen: their dates, how hazy, and the area they filled."""
    filled_pct = 100 * usable[hazy_positions].any(0).mean()
    labels = []
    for j in hazy_positions:
        i = chosen[j]
        labels.append(f"{passes[i].members[0].date} (+{excess[i]:.3f})")
    return (f"Haze: {len(hazy_positions)} hazy scene(s) set aside, used only where no clearer "
            f"scene had a clear pixel ({filled_pct:.1f}% of the area); {', '.join(labels)} = how "
            f"much brighter dense vegetation was in blue/green than on the clearest day.")


def _holes_note(holes: np.ndarray) -> str:
    """Note for pixels left empty."""
    return (f"Holes: {100 * holes.mean():.1f}% of the area has no clear observation and is left "
            f"empty (never filled from other years).")


def _contributing_scenes(chosen: list[int], passes: list[_Pass],
                         usable: np.ndarray) -> list[SceneMeta]:
    """The original scenes that supplied at least one pixel to the median, in time order."""
    scenes: list[SceneMeta] = []
    for j, i in enumerate(chosen):
        if not usable[j].any():
            continue
        members_used = np.unique(passes[i].src[usable[j]])
        for member in members_used:
            scenes.append(passes[i].members[member])
    scenes.sort(key=lambda s: s.datetime)
    return scenes


def _empty_year(year: int, shape: tuple[int, int], notes: list[str]) -> YearComposite:
    """A summer with nothing usable: all holes."""
    height, width = shape
    return YearComposite(year, np.zeros((6, height, width), np.uint16), 0.0, [], notes)


# --- One summer, steps 2–8 ---

def _composite_year(year: int, passes: list[_Pass], guard: np.ndarray, target_n: int,
                    max_n: int, hole_topup_pct: float) -> YearComposite:
    """Composite one summer from its merged passes, following steps 2–8 of "How it works"."""
    if not passes:
        return _empty_year(year, guard.shape, ["No scenes this summer."])
    data = np.stack([p.data for p in passes])  # [k, 7, H, W]
    notes: list[str] = []

    fill = _fill_mask(data)                                      # 2. mask
    flagged = _flag_mask(data)
    clear = ~fill & ~flagged
    if guard.any():                                              # 3. guard bright surfaces
        clear = _apply_guard(clear, guard, fill)
        notes.append(_guard_note(guard, flagged, fill))
    n_merged = sum(len(p.members) - 1 for p in passes)
    if n_merged:
        notes.append(f"Merged {n_merged} same-day scene(s) from neighbouring rows, "
                     f"so one day counts once.")

    order = _rank_passes(passes, clear)                          # 4. choose scenes
    if not order:
        return _empty_year(year, guard.shape, notes + ["No clear pixel in any scene this summer."])
    excess = _haze_excess(data, clear)
    hazy = excess > HAZE_EXCESS

    blue = data[:, BLUE]                                         # 5–6. outliers and top-up
    chosen, before_outliers, usable, holes, holes_before_pct = _top_up(
        order, target_n, max_n, hole_topup_pct, clear, hazy, blue)

    dn, count = _masked_median(data[chosen, :QA], usable)        # 7. median

    if len(chosen) > target_n:                                   # 8. stats and notes
        notes.append(_topup_note(len(chosen) - target_n, holes_before_pct, holes))
    dropped = before_outliers & ~usable
    if dropped.any():
        notes.append(_outlier_note(dropped))
    hazy_positions = [j for j, i in enumerate(chosen) if hazy[i]]
    if hazy_positions:
        notes.append(_haze_note(hazy_positions, chosen, passes, excess, usable))
    if holes.any():
        notes.append(_holes_note(holes))
    scenes = _contributing_scenes(chosen, passes, usable)
    clear_pct = round(100 * float((count[0] > 0).mean()), 2)
    return YearComposite(year, dn, clear_pct, scenes, notes)


# --- Public entry point ---

def composite_series(by_year: dict[int, tuple[np.ndarray, list[SceneMeta]]], target_n: int = 4,
                     max_n: int = 6, hole_topup_pct: float = 0.5) -> dict[int, YearComposite]:
    """Composite every summer of a place (README contract).
    The whole series at once, because the bright-surface guard needs the neighbouring summers
    before it can trust that a "cloud" is really a surface."""
    if target_n < 1 or max_n < target_n:
        raise ValueError("need 1 ≤ target_n ≤ max_n")

    passes_by_year: dict[int, list[_Pass]] = {}
    guard_stats_by_year: dict[int, _GuardStats] = {}
    shape = None
    for year, (scenes, metas) in sorted(by_year.items()):
        if len(scenes) != len(metas):
            raise ValueError(f"{year}: {len(scenes)} scene arrays but "
                             f"{len(metas)} metadata entries")
        if len(scenes) == 0:
            passes_by_year[year] = []
            continue
        if scenes.shape[1] < len(BANDS):
            raise ValueError(f"{year}: expected {len(BANDS)} bands in BANDS order, "
                             f"got {scenes.shape[1]}")
        scenes = np.ascontiguousarray(scenes[:, :len(BANDS)], dtype=np.uint16)  # drop extra bands
        shape = scenes.shape[-2:]
        passes = _merge_passes(scenes, metas)                    # 1. merge same-day scenes
        passes_by_year[year] = passes
        guard_stats_by_year[year] = _guard_stats(np.stack([p.data for p in passes]))
    if shape is None:
        return {}

    results = {}
    for year, passes in passes_by_year.items():
        if year in guard_stats_by_year:
            guard = _confirm_guard(year, guard_stats_by_year)
        else:
            guard = np.zeros(shape, bool)
        results[year] = _composite_year(year, passes, guard, target_n, max_n, hole_topup_pct)
    return results
