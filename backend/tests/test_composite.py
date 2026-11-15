# Unit tests for app.pipeline.composite: ADR-002 mask, median and top-up, plus the S2b fixes.
# Inputs: none (small synthetic scenes are built here; no network, no cache).
# Outputs: pytest results (run: backend/.venv/Scripts/python -m pytest backend/tests/...).
#
# How it works:
#   1. scene() builds one uniform 8×8 scene of dense vegetation; tests then paint clouds,
#      haze or a bright "deck" onto it.
#   2. series() runs composite_series on those scenes, turning any numpy warning into an error.
#   3. Each group of tests covers one part: mask, basics, guard, outlier rejection, haze.
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline import composite as C  # noqa: E402
from app.pipeline.types import SR_OFFSET, SR_SCALE, SceneMeta  # noqa: E402

CLEAR = 1 << 6          # QA "clear" bit only → valid
CLOUD = 1 << 3          # QA "cloud" bit
DILATED = 1 << 1        # QA "dilated cloud" bit (the ring around a cloud)
H = W = 8


def dn(refl: float) -> int:
    """Reflectance → raw DN (inverse of DR-1)."""
    return int(round((refl - SR_OFFSET) / SR_SCALE))


def meta(year: int, i: int, platform: str = "landsat-8", cc: float = 1.0,
         day: int | None = None) -> SceneMeta:
    """A SceneMeta with a unique date per i, unless `day` forces a shared date."""
    if day is None:
        day = i + 1
    return SceneMeta(f"S{year}_{i}", f"{year}-07-{day:02d}T16:00:00Z", year, platform, cc, None)


def scene(blue=0.04, green=0.06, red=0.04, nir=0.35, sw1=0.15, sw2=0.08, qa=CLEAR) -> np.ndarray:
    """One uniform 7-band scene [7, H, W] (dense vegetation by default: NDVI ≈ 0.8)."""
    s = np.zeros((7, H, W), np.uint16)
    for band, value in enumerate([blue, green, red, nir, sw1, sw2]):
        s[band] = dn(value)
    s[6] = qa
    return s


def series(scenes_by_year: dict[int, list[np.ndarray]], **kw) -> dict:
    """Run composite_series on per-year scene lists; any numpy warning fails the test."""
    by_year = {}
    for year, scenes in scenes_by_year.items():
        metas = [meta(year, i) for i in range(len(scenes))]
        by_year[year] = (np.stack(scenes), metas)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no silent numpy warnings in the pipeline
        return C.composite_series(by_year, **kw)


# --- Mask ---

@pytest.mark.parametrize("bad", ["qa0", "fill", 1, 2, 3, 4, 5, "dn0", "dn65535"])
def test_full_mask_invalidates_whole_pixel(bad):
    """Each ADR-002 rule must knock out the pixel in ALL bands (not just one band)."""
    s = np.stack([scene(), scene()])
    if bad == "qa0":
        s[0, 6, 0, 0] = 0
    elif bad == "fill":
        s[0, 6, 0, 0] = 1
    elif bad == "dn0":
        s[0, 3, 0, 0] = 0
    elif bad == "dn65535":
        s[0, 3, 0, 0] = 65535
    else:
        s[0, 6, 0, 0] = 1 << bad
    invalid = C._fill_mask(s) | C._flag_mask(s)
    assert invalid[0, 0, 0] and not invalid[0, 0, 1] and not invalid[1, 0, 0]


def test_water_bit_is_kept():
    """Water (QA bit 7) is a real surface, so it is never masked."""
    s = np.stack([scene(qa=CLEAR | (1 << 7))])
    assert not (C._fill_mask(s) | C._flag_mask(s)).any()


def test_masked_median_matches_nanmedian_oracle():
    """The uint16 sort median must equal np.nanmedian (rounded half to even) for k = 1..6."""
    rng = np.random.default_rng(0)
    for k in range(1, 7):
        x = rng.integers(1, 30000, size=(k, 6, 20, 20)).astype(np.uint16)
        valid = rng.random((k, 20, 20)) > 0.4
        med, count = C._masked_median(x, valid)
        as_float = x.astype(np.float64)
        as_float[~np.broadcast_to(valid[:, None], as_float.shape)] = np.nan
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels
            ref = np.nanmedian(as_float, axis=0)
        expect = np.where(np.isnan(ref), 0, np.rint(np.nan_to_num(ref))).astype(np.uint16)
        assert np.array_equal(med, expect), k
        assert np.array_equal(count[0], valid.sum(0))


# --- Composite basics ---

def test_cloud_ignored_and_clear_pct():
    """A flagged bright cloud in one scene doesn't reach the median; all pixels clear → 100%."""
    sc = [scene(), scene(), scene()]
    sc[0][:6, :2, :2] = dn(0.6)
    sc[0][6, :2, :2] = CLOUD
    out = series({2000: sc})[2000]
    assert out.dn.shape == (6, H, W) and out.dn.dtype == np.uint16
    assert (out.dn[0] == dn(0.04)).all()
    assert out.clear_pct == 100.0


def test_holes_never_filled_from_other_years():
    """A pixel cloudy in every scene of one year stays 0 even though other years see it."""
    sc = [scene(), scene()]
    for s in sc:
        s[:6, 3, 3] = dn(0.5)
        s[6, 3, 3] = CLOUD  # flagged in every 2001 scene, but NOT in 2000/2002 → a real cloud
    out = series({2000: [scene(), scene()], 2001: sc, 2002: [scene(), scene()]})
    assert (out[2001].dn[:, 3, 3] == 0).all()
    assert out[2001].clear_pct == pytest.approx(100 * (1 - 1 / (H * W)), abs=0.01)
    assert any("never filled from other years" in n for n in out[2001].notes)


def test_topup_only_when_holes_exceed_threshold():
    """target_n=2: holes in the 2 best scenes trigger adding more scenes up to max_n."""
    sc = [scene() for _ in range(4)]
    for s in sc[:2]:
        s[6, :2, :] = CLOUD  # top 2 rows cloudy in the first two scenes (25% holes)
    for i, s in enumerate(sc):
        s[:6] += i  # tiny differences so we can see which scenes are used
    by_year = {2000: (np.stack(sc), [meta(2000, i, cc=float(i)) for i in range(4)])}
    out = C.composite_series(by_year, target_n=2, max_n=3)[2000]
    assert out.clear_pct == 100.0
    assert [s.id for s in out.scenes] == ["S2000_0", "S2000_1", "S2000_2"]
    assert any("Topped up with 1" in n for n in out.notes)
    out2 = C.composite_series(by_year, target_n=2, max_n=6, hole_topup_pct=30)[2000]
    assert out2.clear_pct == 75.0 and len(out2.scenes) == 2


def test_only_contributing_scenes_listed_and_extra_band_dropped():
    """A scene with no clear pixel isn't listed; an 8th API band is ignored."""
    sc = [scene(), scene(), scene(qa=CLOUD)]
    sc[2][:6] = dn(0.7)
    extra_band = np.full((3, 1, H, W), 255, np.uint16)
    arr = np.concatenate([np.stack(sc), extra_band], axis=1)  # [3, 8, H, W]
    out = C.composite_series({2000: (arr, [meta(2000, i) for i in range(3)])})[2000]
    assert [s.id for s in out.scenes] == ["S2000_0", "S2000_1"]


def test_same_day_scenes_merge_into_one_vote():
    """Two rows of the same pass count once: median(hazy, clear, clear) stays clear."""
    hazy = scene(blue=0.10, green=0.12)
    sc = [hazy, hazy.copy(), scene(blue=0.04), scene(blue=0.05)]
    metas = [meta(2000, 0, day=5), meta(2000, 1, day=5),
             meta(2000, 2, day=9), meta(2000, 3, day=20)]
    metas[1] = SceneMeta("S2000_1b", metas[0].datetime, 2000, "landsat-8", 1.0, None)
    out = C.composite_series({2000: (np.stack(sc), metas)}, target_n=4)[2000]
    assert any("Merged 1 same-day" in n for n in out.notes)
    assert (out.dn[0] <= dn(0.05)).all()  # not pulled toward 0.10 by a double vote


# --- S2b fix: bright-surface guard ---

def _deck_year(flag_deck: bool, n: int = 4, deck_blue: float = 0.15,
               jitter: float = 0.0) -> list[np.ndarray]:
    """n scenes of vegetation with a bright 'deck' at column 4, optionally flagged as cloud.
    `jitter` makes every second scene's deck brighter by that much (an unsteady 'surface')."""
    out = []
    for i in range(n):
        s = scene()
        blue = deck_blue
        if jitter and i % 2 == 1:
            blue += jitter
        s[0, :, 4] = dn(blue)
        s[1, :, 4] = dn(blue + 0.02)
        s[2, :, 4] = dn(blue + 0.03)
        if flag_deck:
            s[6, :, 4] = CLOUD
            s[6, :, 3] = DILATED  # QA dilation ring on both sides
            s[6, :, 5] = DILATED
        out.append(s)
    return out


def test_guard_keeps_persistent_bright_surface():
    """Deck flagged as cloud in every scene of 2024 and 2025 → kept, not a hole (Gordie Howe)."""
    out = series({2023: _deck_year(False), 2024: _deck_year(True), 2025: _deck_year(True)})
    for y in (2024, 2025):
        assert out[y].clear_pct == 100.0, y
        assert (out[y].dn[0, :, 4] == dn(0.15)).all()
        assert any("Bright-surface guard" in n for n in out[y].notes)


def test_guard_needs_a_neighbouring_year():
    """Flagged bright pixel in only one summer stays masked (could be a stationary cloud)."""
    out = series({2023: _deck_year(False), 2024: _deck_year(True), 2025: _deck_year(False)})
    assert (out[2024].dn[:, :, 4] == 0).all()


def test_guard_rejects_unstable_flagged_pixels():
    """Flagged in both summers but brightness jumps around (±0.15): that's cloud, not a surface."""
    out = series({2024: _deck_year(True, jitter=0.3), 2025: _deck_year(True, jitter=0.3)})
    assert (out[2024].dn[:, :, 4] == 0).all()


def test_guard_ignores_dark_flagged_pixels():
    """A persistent flag on a dark pixel (blue 0.04) isn't a bright surface; leave it masked."""
    dark_deck = {2024: _deck_year(True, deck_blue=0.04), 2025: _deck_year(True, deck_blue=0.04)}
    out = series(dark_deck)
    assert (out[2024].dn[:, :, 4] == 0).all()


# --- S2b fix: outlier rejection ---

def test_unflagged_cloud_rejected():
    """Unflagged white cloud in 2 of 4 scenes: a plain median would average cloud and ground."""
    sc = [scene(), scene(), scene(), scene()]
    for s in sc[:2]:
        s[:3, :4, :4] = dn(0.45)  # bright in blue/green/red, QA says clear
    out = series({2020: sc})[2020]
    assert (out.dn[0, :4, :4] == dn(0.04)).all()
    assert any("Outlier rejection" in n for n in out.notes)


def test_persistent_bright_roof_not_rejected():
    """A roof that is bright in every scene is its own median, so nothing is dropped."""
    sc = [scene(blue=0.30, green=0.30, red=0.30) for _ in range(4)]
    out = series({2020: sc})[2020]
    assert (out.dn[0] == dn(0.30)).all()
    assert not any("Outlier rejection" in n for n in out.notes)


# --- S2b fix: haze-aware scene choice ---

def test_hazy_scene_left_out_when_clear_ones_suffice():
    """5 clear-ish passes + 1 hazy (lowest scene-wide cloud_cover!) → the hazy one isn't used."""
    sc = [scene(blue=0.10, green=0.13)]
    metas = [meta(2021, 0, cc=0.0)]
    for i in range(5):
        sc.append(scene(blue=0.04 + 0.001 * i))
        metas.append(meta(2021, i + 1, cc=1.0 + i))
    out = C.composite_series({2021: (np.stack(sc), metas)})[2021]
    assert "S2021_0" not in [s.id for s in out.scenes]
    assert out.dn[0].max() <= dn(0.045)
    assert any("hazy" in n for n in out.notes)


def test_hazy_scene_only_fills_where_nothing_else_is_clear():
    """With too few clear passes, the hazy one may only fill pixels the others can't see."""
    hazy = scene(blue=0.10, green=0.13)
    a, b = scene(), scene()
    a[6, :2] = CLOUD  # rows 0–1 cloudy in both clear passes
    b[6, :2] = CLOUD
    metas = [meta(2021, 0, cc=0.0), meta(2021, 1, cc=1.0), meta(2021, 2, cc=2.0)]
    out = C.composite_series({2021: (np.stack([hazy, a, b]), metas)})[2021]
    assert (out.dn[0, :2] == dn(0.10)).all()   # filled from the hazy pass (real data, same summer)
    assert (out.dn[0, 2:] == dn(0.04)).all()   # untouched by haze elsewhere
    assert out.clear_pct == 100.0
