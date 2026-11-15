# Spike S2b harness: compares the plain ADR-002 median ("before") with backend composite_series ("after").
# Inputs: data/cache/s2b/{willow-run,delray}.npz/.json (6 km) and data/cache/s2/{central-park,lake-mead,paradise} (centre 200 px).
# Outputs: spikes/s2b-mask/results.json, printed numbers, contact sheets in data/spikes/s2b/sheets/ (+ copies here).
#
# Why a separate harness: the backend module must stay free of evaluation code, and the
# "before" must be scored with exactly the same metric code as the "after".
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "spikes" / "s2-composite"))

from app.pipeline import composite as C  # noqa: E402
from app.pipeline.types import SceneMeta  # noqa: E402
import evaluate as EV  # noqa: E402  (S2's shared scorer: year_stats, flicker, CLOUD_REFL)

SHEETS = REPO / "data" / "spikes" / "s2b" / "sheets"
PX = 200  # 6 km at 30 m
# Gordie Howe bridge deck inside the Delray 6 km box (row/col window around the span,
# found from the per-scene QA maps: the deck is the diagonal line the QA flags as cloud).
BRIDGE = (slice(120, 170), slice(80, 135))


def load(place: str) -> tuple[dict[int, tuple[np.ndarray, list[SceneMeta]]], int]:
    """Load a cached place as composite_series input. S2 caches are 10 km with an extra API band:
    keep bands 0–6 and crop the centre 200 px (6 km), as the real pipeline would download."""
    s2b = (REPO / "data" / "cache" / "s2b" / f"{place}.json").exists()
    root = REPO / "data" / "cache" / ("s2b" if s2b else "s2")
    meta = json.loads((root / f"{place}.json").read_text())
    scenes = np.load(root / f"{place}.npz")["scenes"][:, :7]
    if scenes.shape[-1] != PX:
        o = (scenes.shape[-1] - PX) // 2
        scenes = scenes[..., o:o + PX, o:o + PX]
    metas = [SceneMeta(s["id"], s["datetime"], int(s["datetime"][:4]), s["platform"], float(s["cloud_cover"]),
                       None) for s in meta["scenes"]]
    by_year: dict[int, list[int]] = {}
    for i, m in enumerate(metas):
        by_year.setdefault(m.year, []).append(i)
    return {y: (np.ascontiguousarray(scenes[idx]), [metas[i] for i in idx]) for y, idx in by_year.items()}, len(metas)


def before_series(by_year, target_n=4, max_n=6, hole_topup_pct=0.5) -> dict[int, np.ndarray]:
    """Plain ADR-002 composite: N lowest scene-wide cloud_cover, full QA mask (bits 0–5, DN 0/65535,
    whole pixel), median; if holes > 0.5% use up to 6 scenes. No S2b fixes."""
    out = {}
    for y, (scenes, metas) in by_year.items():
        order = sorted(range(len(metas)), key=lambda i: metas[i].cloud_cover)
        for n in range(target_n, max_n + 1):
            idx = sorted(order[:n])
            st = scenes[idx]
            valid = ~C._fill_mask(st) & ~C._flag_mask(st)
            dn, c = C._masked_median(st[:, :6], valid)
            if 100 * (c[0] == 0).mean() <= hole_topup_pct or n >= len(order):
                break
        out[y] = dn
    return out


def refl(dn: np.ndarray) -> np.ndarray:
    """DN → reflectance with NaN on holes (same rule as evaluate.reflectance)."""
    r = dn.astype(np.float32) * 0.0000275 - 0.2
    r[np.broadcast_to((dn == 0).any(0, keepdims=True), dn.shape)] = np.nan
    return r


def cloud_like_pct(dn: np.ndarray) -> float:
    """% of clear pixels bright in blue, green and red at once (> 0.25: evaluate.CLOUD_REFL)."""
    r = refl(dn)
    ok = np.isfinite(r).all(0)
    bright = (r[0] > EV.CLOUD_REFL) & (r[1] > EV.CLOUD_REFL) & (r[2] > EV.CLOUD_REFL) & ok
    return round(100 * bright.sum() / max(ok.sum(), 1), 3)


def veg_mask(series: dict[int, np.ndarray]) -> np.ndarray:
    """Pixels that are dense vegetation (NDVI > 0.5) in the all-years median of a series.
    Why one fixed mask: the haze score must compare the same pixels across years and
    between before/after, or it would measure which pixels were picked, not haze."""
    with np.errstate(invalid="ignore", divide="ignore"):
        st = np.stack([refl(series[y]) for y in sorted(series)])
        ndvi = (st[:, 3] - st[:, 2]) / (st[:, 3] + st[:, 2])
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return np.nanmedian(ndvi, 0) > 0.5


def haze_score(dn: np.ndarray, veg: np.ndarray) -> float:
    """Median of (blue + green) / 2 reflectance over the fixed dense-vegetation mask: ~0.04 when
    clear, higher when a hazy day leaks into the median (the same measure composite.py ranks by)."""
    r = refl(dn)
    v = (r[0] + r[1]) / 2
    sel = veg & np.isfinite(v)
    return round(float(np.median(v[sel])), 4) if sel.sum() > 100 else float("nan")


def hole_pct(dn: np.ndarray, win=(slice(None), slice(None))) -> float:
    """% of pixels (optionally within a window) with no value."""
    return round(100 * float((dn[:, win[0], win[1]] == 0).all(0).mean()), 2)


def sheet(series: dict[int, np.ndarray], path: Path, title: str) -> None:
    """Contact sheet with ONE fixed stretch (reflectance 0–0.3, gamma 1/1.6), holes black."""
    years = sorted(series)
    tile, cols = 160, 9
    rows = int(np.ceil(len(years) / cols))
    im = Image.new("RGB", (cols * tile, rows * (tile + 16) + 24), "black")
    d = ImageDraw.Draw(im)
    d.text((4, 4), title, fill="yellow")
    for k, y in enumerate(years):
        r = np.nan_to_num(refl(series[y])[[2, 1, 0]], nan=0.0)
        rgb = (np.clip(r / 0.3, 0, 1) ** (1 / 1.6) * 255).astype(np.uint8).transpose(1, 2, 0)
        x0, y0 = (k % cols) * tile, 24 + (k // cols) * (tile + 16)
        im.paste(Image.fromarray(rgb).resize((tile, tile), Image.NEAREST), (x0, y0 + 16))
        d.text((x0 + 2, y0 + 2), str(y), fill="white")
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, quality=90)
    shutil.copy(path, HERE / path.name)


def flicker(series: dict[int, np.ndarray]) -> dict:
    """evaluate.py flicker (series, pixel) over the years kept by FR-14 (≥ 60% clear)."""
    years = sorted(series)
    st = EV.year_stats(np.stack([refl(series[y]) for y in years]))
    kept = st["clear_pct"] >= EV.MIN_CLEAR_PCT
    cloudy = kept & (st["cloud_frac"] > EV.CLOUD_YEAR_FRAC)
    return {**EV.flicker(st, kept), "years_kept": int(kept.sum()),
            "clean_year_pct": round(100 * (1 - cloudy.sum() / max(kept.sum(), 1)), 1),
            "cloudy_years": [int(y) for y in np.array(years)[cloudy]]}


def main() -> None:
    results: dict = {"thresholds": {k: getattr(C, k) for k in dir(C) if k.startswith(("GUARD_", "OUTLIER_", "HAZE_"))}}
    for place in ["delray", "willow-run", "central-park", "lake-mead", "paradise"]:
        by_year, n = load(place)
        before = before_series(by_year)
        t0 = time.perf_counter()
        after_yc = C.composite_series(by_year)
        secs = time.perf_counter() - t0
        after = {y: yc.dn for y, yc in after_yc.items()}
        veg = veg_mask(after)
        r = {"veg_mask_pct": round(100 * float(veg.mean()), 1), "scenes": n, "years": len(by_year), "composite_s": round(secs, 2),
             "flicker_before": flicker(before), "flicker_after": flicker(after), "per_year": {}}
        for y in sorted(by_year):
            r["per_year"][y] = {
                "holes_before": hole_pct(before[y]), "holes_after": hole_pct(after[y]),
                "cloud_like_before": cloud_like_pct(before[y]), "cloud_like_after": cloud_like_pct(after[y]),
                "haze_before": haze_score(before[y], veg), "haze_after": haze_score(after[y], veg),
                "notes": after_yc[y].notes,
            }
            if place == "delray":
                r["per_year"][y]["bridge_holes_before"] = hole_pct(before[y], BRIDGE)
                r["per_year"][y]["bridge_holes_after"] = hole_pct(after[y], BRIDGE)
        results[place] = r
        if place in ("delray", "willow-run"):
            sheet(before, SHEETS / f"{place}_before.jpg", f"{place} BEFORE: ADR-002 median, full mask, 6 km")
            sheet(after, SHEETS / f"{place}_after.jpg", f"{place} AFTER: composite_series (S2b), 6 km")
        print(f"{place}: {n} scenes, composite {secs:.2f} s, flicker before {r['flicker_before']} "
              f"after {r['flicker_after']}", flush=True)
    (HERE / "results.json").write_text(json.dumps(results, indent=1, default=str))


if __name__ == "__main__":
    main()
