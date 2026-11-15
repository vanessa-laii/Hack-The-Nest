# Tests for run.split_kept: which summers are kept, and that skip reasons are truthful.
# Inputs: small fake YearComposite and SceneMeta objects (no network, no images).
# Outputs: pytest results.
import numpy as np

from app.pipeline import metrics
from app.pipeline.run import ARCHIVE_DOWN, MIN_LAND_PCT, split_kept
from app.pipeline.types import SceneMeta, YearComposite


def _scene(scene_id: str, year: int) -> SceneMeta:
    """A minimal fake Landsat 5 scene for one summer."""
    return SceneMeta(id=scene_id, datetime=f"{year}-07-01T00:00:00Z", year=year,
                     platform="landsat-5", cloud_cover=5.0, native_epsg=32611)


# One land pixel's 6 bands (vegetation-like), so a kept year has land to measure (schema 1.2).
LAND_DN = np.array([8727, 10182, 9091, 20000, 14545, 10909], np.uint16)[:, None, None]
# One water pixel's 6 bands (MNDWI > 0), so it counts as no land.
WATER_DN = np.array([9091, 9455, 8727, 7636, 7454, 7382], np.uint16)[:, None, None]


def _year(year: int, clear_pct: float) -> YearComposite:
    """A fake summer image of plain land with the given clear %."""
    return YearComposite(year=year, dn=LAND_DN * np.ones((1, 2, 2), np.uint16),
                         clear_pct=clear_pct, scenes=[])


def test_skip_reasons_never_blame_clouds_for_failures():
    """Each way a summer can go missing gets its own, true reason."""
    scenes = {
        2000: [_scene("a", 2000)],                      # fine
        2001: [_scene("b", 2001), _scene("c", 2001)],   # both downloads failed
        2002: [_scene("d", 2002), _scene("e", 2002)],   # one failed, and too little clear area
        2003: [_scene("f", 2003)],                      # just cloudy
    }
    series = {2000: _year(2000, 95), 2001: _year(2001, 0), 2002: _year(2002, 40),
              2003: _year(2003, 30)}
    kept, skipped = split_kept(series, range(2000, 2006), scenes,
                               failed_years={2004}, failed_ids={"b", "c", "d"})
    reasons = {s["year"]: s["reason"] for s in skipped}

    assert list(kept) == [2000]
    assert reasons[2001] == ARCHIVE_DOWN                       # not "0% clear"
    assert "1 of 2 images failed to download" in reasons[2002]
    assert reasons[2003] == "only 30% of the area had a clear view"
    assert reasons[2004] == ARCHIVE_DOWN                       # search failed: not "no images"
    assert reasons[2005] == "no Landsat images of this place that summer"


def test_almost_all_water_year_is_skipped_with_reason():
    """A clear year with no land can't have a land greenness (schema 1.2), so it's skipped."""
    lake = YearComposite(year=2010, dn=WATER_DN * np.ones((1, 4, 4), np.uint16), clear_pct=100.0,
                         scenes=[])
    kept, skipped = split_kept({2010: lake}, range(2010, 2011), {2010: [_scene("w", 2010)]},
                               failed_years=set(), failed_ids=set())
    assert kept == {}
    assert "almost all water" in skipped[0]["reason"]


def test_cloudy_water_year_gets_the_clear_view_reason():
    """A year that is both too cloudy and all water is skipped for the clear view, checked first."""
    lake = YearComposite(year=2010, dn=WATER_DN * np.ones((1, 4, 4), np.uint16), clear_pct=40.0,
                         scenes=[])
    kept, skipped = split_kept({2010: lake}, range(2010, 2011), {2010: [_scene("w", 2010)]},
                               failed_years=set(), failed_ids=set())
    assert kept == {}
    assert skipped[0]["reason"] == "only 40% of the area had a clear view"


def test_year_at_the_land_threshold_is_kept():
    """1 land pixel in 100 (exactly MIN_LAND_PCT) and 1 in 81 (just above) are both enough."""
    for side in (10, 9):
        dn = WATER_DN * np.ones((1, side, side), np.uint16)
        dn[:, 0, 0] = LAND_DN[:, 0, 0]
        year = YearComposite(year=2010, dn=dn, clear_pct=100.0, scenes=[])
        assert metrics.land_pct(year) >= MIN_LAND_PCT
        kept, skipped = split_kept({2010: year}, range(2010, 2011), {2010: [_scene("l", 2010)]},
                                   failed_years=set(), failed_ids=set())
        assert list(kept) == [2010], skipped
