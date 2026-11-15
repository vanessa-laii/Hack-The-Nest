# Computes per-pixel spectral indices and per-year summary metrics from composites (FR-20).
# Inputs: YearComposite (dn uint16 [6, H, W], 0 = hole), or {year: YearComposite}.
# Outputs: indices() → {'ndvi','mndwi','ndbi'} float32 [H, W], NaN on holes;
#          compute_metrics() → story metrics.series.
#
# How it works:
#   1. Turn raw values into reflectance (DR-1).
#   2. Compute three indices per pixel: vegetation (NDVI), water (MNDWI), built-up (NDBI).
#   3. Mark holes (no clear view) as NaN so they never count.
#   4. Per year, over clear pixels only: mean NDVI, share of water, share of built-up land.
#   5. Clamp and round each value, one row per year, oldest first.
from __future__ import annotations

import numpy as np

from app.pipeline.types import GREEN, NIR, RED, SR_OFFSET, SR_SCALE, SWIR1, YearComposite

# Thresholds come from story.schema.json metrics.series. They are the published
# definitions: never tune them to make a chart look right.
WATER_MNDWI_MIN = 0.0   # water_frac: share of clear pixels with MNDWI > 0
BUILT_NDBI_MIN = 0.0    # built_frac: NDBI > 0 ...
BUILT_NDVI_MAX = 0.2    # ... and NDVI < 0.2 ...
BUILT_MNDWI_MAX = 0.0   # ... and MNDWI <= 0 (schema 1.1).
# Schema 1.1 added the MNDWI test to leave out water: murky or algae-tinted water passed
# the first two, and Lake Mead read 84–86% "built". Agreed with Vanessa, 2026-11-14.
DECIMALS = 4


def _normalised_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(a − b) / (a + b), with NaN where a + b is 0.
    NaN, not 0, because a fake 0 would bias means and thresholds."""
    num = a - b
    den = a + b
    out = np.full(a.shape, np.nan, dtype=np.float32)
    np.divide(num, den, out=out, where=den != 0)
    # Reflectance can be slightly negative, which can push the ratio past ±1; clip it back.
    return np.clip(out, -1.0, 1.0, out=out)


def indices(yc: YearComposite) -> dict[str, np.ndarray]:
    """NDVI, MNDWI and NDBI for one composite, float32 [H, W], NaN on holes.
    Computed on reflectance, not raw values: the −0.2 offset changes the ratios.
    Also read by S3's change detection."""
    dn = yc.dn
    refl = dn.astype(np.float32) * np.float32(SR_SCALE) + np.float32(SR_OFFSET)
    red = refl[RED]
    green = refl[GREEN]
    nir = refl[NIR]
    swir1 = refl[SWIR1]

    out = {
        "ndvi": _normalised_difference(nir, red),       # (NIR − R) / (NIR + R)
        "mndwi": _normalised_difference(green, swir1),  # (G − SWIR1) / (G + SWIR1)
        "ndbi": _normalised_difference(swir1, nir),     # (SWIR1 − NIR) / (SWIR1 + NIR)
    }
    # A 0 in any band is a hole (ADR-002): never report a value there.
    hole = (dn == 0).any(axis=0)
    for arr in out.values():
        arr[hole] = np.nan
    return out


def _clamp(value: float, low: float, high: float) -> float:
    """Keeps a value inside the schema's allowed range."""
    return min(max(value, low), high)


def _year_metrics(yc: YearComposite) -> dict:
    """The three schema metrics for one year, over clear pixels only.
    A pixel is clear when all three indices are finite, so all metrics share one denominator.
    Raises ValueError if nothing is clear, because NaN is not valid JSON."""
    ix = indices(yc)
    ndvi = ix["ndvi"]
    mndwi = ix["mndwi"]
    ndbi = ix["ndbi"]
    clear = np.isfinite(ndvi) & np.isfinite(mndwi) & np.isfinite(ndbi)
    n = int(clear.sum())
    if n == 0:
        raise ValueError(f"year {yc.year}: no clear pixels, cannot compute metrics "
                         "(years under 60% clear should be skipped before this, FR-14)")

    ndvi_clear = ndvi[clear]
    mndwi_clear = mndwi[clear]
    ndbi_clear = ndbi[clear]

    # float64 sum so a 300k-pixel mean doesn't lose precision.
    ndvi_mean = float(np.mean(ndvi_clear, dtype=np.float64))
    water_frac = float(np.count_nonzero(mndwi_clear > WATER_MNDWI_MIN)) / n

    looks_built = ndbi_clear > BUILT_NDBI_MIN
    not_green = ndvi_clear < BUILT_NDVI_MAX
    not_water = mndwi_clear <= BUILT_MNDWI_MAX
    built = looks_built & not_green & not_water
    built_frac = float(np.count_nonzero(built)) / n

    # Clamp to the schema ranges, then round (keeps story.json small and stable).
    return {
        "year": int(yc.year),
        "ndvi_mean": round(_clamp(ndvi_mean, -1.0, 1.0), DECIMALS),
        "water_frac": round(_clamp(water_frac, 0.0, 1.0), DECIMALS),
        "built_frac": round(_clamp(built_frac, 0.0, 1.0), DECIMALS),
    }


def compute_metrics(series: dict[int, YearComposite]) -> list[dict]:
    """[{year, ndvi_mean, water_frac, built_frac}] for every year, ascending (metrics.series)."""
    rows = []
    for year in sorted(series):
        rows.append(_year_metrics(series[year]))
    return rows
