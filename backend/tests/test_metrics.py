# Tests for pipeline/metrics.py: index maths, hole handling, schema definitions and ranges (FR-20).
# Inputs: synthetic uint16 composites built in-test.
# Outputs: pass/fail only.
#
# How it works:
#   1. Build synthetic composites from known spectra (vegetation, water, built-up).
#   2. Check the index formulas, hole handling, divide-by-zero and clipping.
#   3. Check the yearly metrics follow the schema definitions and validate against it.
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

# Make `app` importable when pytest is run from the repo root (no conftest needed).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline import metrics  # noqa: E402
from app.pipeline.types import (  # noqa: E402
    BLUE, GREEN, NIR, RED, SWIR1, SWIR2, SceneMeta, YearComposite,
)


def dn_for(refl: float) -> int:
    """Inverse of DR-1 scaling: the DN that gives this reflectance."""
    return int(round((refl + 0.2) / 0.0000275))


def make_yc(year: int, dn: np.ndarray) -> YearComposite:
    """Wrap a synthetic DN array in a YearComposite with one dummy scene."""
    scene = SceneMeta(f"S{year}", f"{year}-07-01T18:00:00Z", year, "landsat-5", 5.0, 32611)
    return YearComposite(year=year, dn=dn, clear_pct=100.0, scenes=[scene])


def pixel_dn(blue=0.05, green=0.05, red=0.05, nir=0.05, swir1=0.05, swir2=0.05,
             h=4, w=4) -> np.ndarray:
    """A composite filled with one spectrum given in reflectance."""
    dn = np.zeros((6, h, w), dtype=np.uint16)
    spectrum = {BLUE: blue, GREEN: green, RED: red, NIR: nir, SWIR1: swir1, SWIR2: swir2}
    for band, refl in spectrum.items():
        dn[band] = dn_for(refl)
    return dn


VEG = dict(green=0.08, red=0.04, nir=0.40, swir1=0.20)    # NDVI .82, MNDWI -.43, NDBI -.33
WATER = dict(green=0.06, red=0.04, nir=0.02, swir1=0.01)  # MNDWI .71, NDBI -.33
BUILT = dict(green=0.12, red=0.14, nir=0.18, swir1=0.25)  # NDVI .125, NDBI .16, MNDWI -.35


def test_index_maths():
    """Indices match the formulas on reflectance (not DN), as float32 [H, W]."""
    ix = metrics.indices(make_yc(2000, pixel_dn(**VEG)))
    green, red, nir, swir1 = VEG["green"], VEG["red"], VEG["nir"], VEG["swir1"]
    expected_ndvi = (nir - red) / (nir + red)
    expected_mndwi = (green - swir1) / (green + swir1)
    expected_ndbi = (swir1 - nir) / (swir1 + nir)
    # DN rounding gives ~3e-5 reflectance error, so compare to 1e-3.
    assert ix["ndvi"][0, 0] == pytest.approx(expected_ndvi, abs=1e-3)
    assert ix["mndwi"][0, 0] == pytest.approx(expected_mndwi, abs=1e-3)
    assert ix["ndbi"][0, 0] == pytest.approx(expected_ndbi, abs=1e-3)
    for arr in ix.values():
        assert arr.dtype == np.float32
        assert arr.shape == (4, 4)


def test_holes_are_nan():
    """A 0 in any band -> NaN in all three indices at that pixel only."""
    dn = pixel_dn(**VEG)
    dn[:, 0, 0] = 0
    dn[SWIR2, 1, 1] = 0   # a band no index uses still marks the pixel as a hole
    ix = metrics.indices(make_yc(2000, dn))
    for a in ix.values():
        assert math.isnan(a[0, 0]) and math.isnan(a[1, 1])
        assert np.isfinite(a[2, 2])


def test_divide_by_zero():
    """Zero denominator -> NaN, with no numpy warning and no inf.
    Tested on the helper because no uint16 DN maps to exactly 0.0 reflectance in float32."""
    a = np.array([0.1, 0.0, 0.3], dtype=np.float32)
    b = np.array([-0.1, 0.0, 0.1], dtype=np.float32)
    with np.errstate(all="raise"):
        out = metrics._normalised_difference(a, b)
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert out[2] == pytest.approx(0.5)


def test_indices_clipped_to_range():
    """Slightly negative reflectance can push ratios past +-1; output stays in [-1, 1]."""
    dn = pixel_dn(red=-0.01, nir=0.02)  # NDVI = 0.03 / 0.01 = 3 before clipping
    ix = metrics.indices(make_yc(2000, dn))
    assert ix["ndvi"][0, 0] == 1.0


def test_compute_metrics_definitions():
    """Columns: 2 veg, 1 water, 1 built, 1 holes. Holes must be left out of every denominator."""
    dn = np.zeros((6, 4, 5), dtype=np.uint16)
    for col, spec in enumerate((VEG, VEG, WATER, BUILT)):
        column = pixel_dn(**spec, h=4, w=1)
        dn[:, :, col] = column[:, :, 0]
    # column 4 stays 0 = holes
    yc = make_yc(2005, dn)
    [m] = metrics.compute_metrics({2005: yc})
    assert m["year"] == 2005
    assert m["water_frac"] == 0.25
    assert m["built_frac"] == 0.25
    assert m["ndvi_mean"] == round(float(np.nanmean(metrics.indices(yc)["ndvi"])), 4)


def test_ascending_rounded_and_json_safe():
    """Years ascending, values rounded to 4 dp, inside schema ranges, valid strict JSON."""
    series = {y: make_yc(y, pixel_dn(**VEG)) for y in (2010, 1984, 1999)}
    out = metrics.compute_metrics(series)
    assert [r["year"] for r in out] == [1984, 1999, 2010]
    for r in out:
        assert set(r) == {"year", "ndvi_mean", "water_frac", "built_frac"}
        assert -1 <= r["ndvi_mean"] <= 1
        assert 0 <= r["water_frac"] <= 1
        assert 0 <= r["built_frac"] <= 1
        assert r["ndvi_mean"] == round(r["ndvi_mean"], 4)
    json.dumps(out, allow_nan=False)


def test_all_holes_raises():
    """A year with no clear pixels raises ValueError instead of emitting NaN."""
    with pytest.raises(ValueError, match="no clear pixels"):
        metrics.compute_metrics({1990: make_yc(1990, np.zeros((6, 3, 3), dtype=np.uint16))})


def test_schema_validates():
    """Output validates against the metrics part of contracts/story.schema.json."""
    jsonschema = pytest.importorskip("jsonschema")
    path = Path(__file__).resolve().parents[2] / "contracts" / "story.schema.json"
    schema = json.loads(path.read_text("utf-8"))
    sub = dict(schema["properties"]["metrics"])
    sub["$defs"] = schema.get("$defs", {})
    series = {year: make_yc(year, pixel_dn(**BUILT)) for year in (1990, 2020)}
    out = metrics.compute_metrics(series)
    jsonschema.validate({"series": out}, sub)
