# Tests for pipeline/render.py: fixed stretch (FR-15), hole colour, output sizes and paths (DR-2).
# Inputs: synthetic uint16 composites built in-test; writes into pytest's tmp_path.
# Outputs: pass/fail only.
#
# How it works:
#   1. Build flat synthetic composites with a known reflectance (helpers below).
#   2. Check the stretch is fixed, its values, band order and hole colour.
#   3. Render to a temp folder and check file sizes and paths.
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Make `app` importable when pytest is run from the repo root (no conftest needed).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline import render  # noqa: E402
from app.pipeline.types import BLUE, GREEN, RED, SceneMeta, YearComposite  # noqa: E402


def dn_for(refl: float) -> int:
    """Inverse of DR-1 scaling: the DN that gives this reflectance."""
    return int(round((refl + 0.2) / 0.0000275))


def make_yc(year: int, dn: np.ndarray) -> YearComposite:
    """Wrap a synthetic DN array in a YearComposite with one dummy scene."""
    scene = SceneMeta(f"S{year}", f"{year}-07-01T18:00:00Z", year, "landsat-8", 5.0, 32611)
    return YearComposite(year=year, dn=dn, clear_pct=100.0, scenes=[scene])


def uniform(refl: float, h: int = 50, w: int = 50) -> np.ndarray:
    """A composite where all 6 bands have the same reflectance everywhere."""
    return np.full((6, h, w), dn_for(refl), dtype=np.uint16)


def test_stretch_constants_fixed():
    """The stretch is a module constant: identical on every call and independent of input."""
    assert render.stretch_params() == render.stretch_params() == (0.0, 0.30, 1 / 1.6)
    # Rendering a dark and a bright image must not change the constants.
    render.to_rgb(uniform(0.01))
    render.to_rgb(uniform(0.5))
    assert render.stretch_params() == (render.STRETCH_MIN, render.STRETCH_MAX, render.GAMMA)


def test_no_adaptive_stretch():
    """A pixel's colour depends only on its own value, not on the rest of the image.
    An auto-levels stretch would change the 0.1 pixel when a bright pixel is added."""
    a = uniform(0.10)
    b = a.copy()
    b[:, 0, 0] = dn_for(0.29)  # add one bright pixel
    assert np.array_equal(render.to_rgb(a)[10, 10], render.to_rgb(b)[10, 10])
    # Same input in separate calls (separate "years") gives identical output.
    assert np.array_equal(render.to_rgb(a), render.to_rgb(a.copy()))


def test_stretch_values():
    """Endpoints and gamma: 0 -> black, >= 0.3 -> white, 0.15 -> 255 * 0.5**(1/1.6)."""
    assert (render.to_rgb(uniform(0.0)) == 0).all()
    assert (render.to_rgb(uniform(0.30)) == 255).all()
    assert (render.to_rgb(uniform(0.6)) == 255).all()       # clipped, not rescaled
    mid = render.to_rgb(uniform(0.15))[0, 0, 0]
    assert abs(int(mid) - round(255 * 0.5 ** (1 / 1.6))) <= 1


def test_band_order_is_true_colour():
    """Red band drives the R channel, green G, blue B."""
    for band, expected in ((RED, (255, 0, 0)), (GREEN, (0, 255, 0)), (BLUE, (0, 0, 255))):
        dn = uniform(0.0)
        dn[band] = dn_for(0.3)
        assert tuple(render.to_rgb(dn)[0, 0]) == expected


def test_holes_are_neutral_grey():
    """DN 0 in any band -> the documented hole grey; other pixels unaffected."""
    dn = uniform(0.05)       # not 0.1: a flat 0.1 surface happens to render as 128 grey too
    dn[:, 5, 5] = 0          # full hole
    dn[3, 7, 7] = 0          # a non-RGB band is 0 -> still a hole
    rgb = render.to_rgb(dn)
    assert tuple(rgb[5, 5]) == render.HOLE_RGB
    assert tuple(rgb[7, 7]) == render.HOLE_RGB
    assert tuple(rgb[0, 0]) != render.HOLE_RGB


def test_render_frames_sizes_and_paths(tmp_path):
    """Two years -> frames 1024² and thumbs 256² at the documented paths, no temp files left."""
    series = {
        2001: make_yc(2001, uniform(0.05, 200, 200)),
        1990: make_yc(1990, uniform(0.2, 200, 200)),
    }
    out = render.render_frames(series, tmp_path)
    assert list(out) == [1990, 2001]
    for year, (frame, thumb) in out.items():
        assert frame == tmp_path / "frames" / f"{year}.jpg"
        assert thumb == tmp_path / "thumbs" / f"{year}.jpg"
        with Image.open(frame) as im:
            assert im.size == (1024, 1024)
            assert im.format == "JPEG"
            assert im.mode == "RGB"
        with Image.open(thumb) as im:
            assert im.size == (256, 256)
            assert im.format == "JPEG"
    assert not list(tmp_path.rglob("*.tmp"))


def test_small_aoi_thumb(tmp_path):
    """An AOI smaller than 256 px still gives a 256² thumbnail."""
    series = {2000: make_yc(2000, uniform(0.1, 100, 100))}
    out = render.render_frames(series, tmp_path)
    with Image.open(out[2000][1]) as im:
        assert im.size == (256, 256)
