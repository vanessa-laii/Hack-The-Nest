# Tests for run.split_kept: which summers are kept, and that skip reasons are truthful.
# Inputs: small fake YearComposite and SceneMeta objects (no network, no images).
# Outputs: pytest results.
import numpy as np

from app.pipeline.run import ARCHIVE_DOWN, split_kept
from app.pipeline.types import SceneMeta, YearComposite


def _scene(scene_id: str, year: int) -> SceneMeta:
    return SceneMeta(id=scene_id, datetime=f"{year}-07-01T00:00:00Z", year=year,
                     platform="landsat-5", cloud_cover=5.0, native_epsg=32611)


def _year(year: int, clear_pct: float) -> YearComposite:
    return YearComposite(year=year, dn=np.zeros((6, 2, 2), np.uint16), clear_pct=clear_pct,
                         scenes=[])


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
