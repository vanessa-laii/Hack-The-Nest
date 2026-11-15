# Spike S2 scorer: rates every approach's yearly composites with the same code, at 3/6/10 km crops.
# Inputs: data/spikes/s2/<approach>/<place>_n<N>.npz (years, dn) + data/cache/s2/<place>.json; approaches/places/N below.
# Outputs: spikes/s2-composite/scores.json, a printed summary table, contact sheets in data/spikes/s2/sheets/*.jpg.
#
# Why one scorer: if each approach scored itself, differences in metric code
# would look like differences in quality. Fixed rules keep the comparison fair.
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[2]
OUT_ROOT = REPO / "data" / "spikes" / "s2"
# best-gapfill's main outputs use the first (wrong) mask rule, which pasted fill
# pixels in as 0. Its fill-bit0 variant uses the corrected rule, so that variant is
# the fair one to score.
APPROACHES = ["median", "best-gapfill/fill-bit0", "medoid"]
PLACES = ["lake-mead", "paradise", "central-park"]
CONTROL = "central-park"
NS = [2, 4, 6]
FULL = 334
CROPS = {3: 100, 6: 200, 10: 334}  # km → pixels at 30 m (centre crop of the 10 km download)
B, G, R, NIR = 0, 1, 2, 3          # band indices in the 6-band composite (blue, green, red, nir08, swir16, swir22)

# Thresholds. All are heuristics; the contact sheets are the visual check on them.
CLOUD_REFL = 0.25        # a pixel bright in blue, green AND red at once looks like cloud (or snow)
CLOUD_YEAR_FRAC = 0.01   # a year counts as "cloudy" if > 1% of its clear pixels look like cloud
MIN_CLEAR_PCT = 60       # FR-14: years under 60% clear are skipped, not shown


def reflectance(dn: np.ndarray) -> np.ndarray:
    """Raw Landsat C2 L2 values → surface reflectance (DR-1), NaN where there's no data (dn == 0).
    Why NaN: so every later mean or median ignores no-data pixels instead of counting them as dark."""
    r = dn.astype(np.float32) * 0.0000275 - 0.2
    r[dn == 0] = np.nan
    return r


def crop(a: np.ndarray, px: int) -> np.ndarray:
    """Centre crop of the last two axes. Why centre: every area size is centred on the
    same point, which is how the real pipeline will build them."""
    o = (FULL - px) // 2
    return a[..., o:o + px, o:o + px]


def year_stats(refl: np.ndarray) -> dict:
    """Per-year clear %, cloud-like fraction and mean NDVI for one cropped composite [Y, 6, H, W].
    Why these three: clear % feeds FR-14 skipping; the cloud fraction is the
    'no visible clouds' bar; mean NDVI is the series the control must keep flat."""
    valid = np.all(np.isfinite(refl), axis=1)  # [Y, H, W]: all 6 bands present
    clear_pct = valid.mean(axis=(1, 2)) * 100
    bright = (refl[:, B] > CLOUD_REFL) & (refl[:, G] > CLOUD_REFL) & (refl[:, R] > CLOUD_REFL)
    with np.errstate(invalid="ignore", divide="ignore"):
        cloud_frac = np.where(valid.sum((1, 2)) > 0, (bright & valid).sum((1, 2)) / valid.sum((1, 2)), np.nan)
        ndvi = (refl[:, NIR] - refl[:, R]) / (refl[:, NIR] + refl[:, R])
    ndvi_mean = np.array([np.nanmean(n[v]) if v.any() else np.nan for n, v in zip(ndvi, valid)])
    return {"clear_pct": clear_pct, "cloud_frac": cloud_frac, "ndvi_mean": ndvi_mean, "ndvi": ndvi, "valid": valid}


def flicker(st: dict, kept: np.ndarray) -> dict:
    """How much an unchanged place jumps between consecutive kept years.
    - series: median |Δ mean NDVI| between consecutive years (whole-area brightness jumps)
    - pixel: median over year pairs of the mean per-pixel |Δ NDVI| (patchy noise, e.g. leftover cloud)
    Why both: a method can keep the area mean steady while individual pixels jump."""
    idx = np.flatnonzero(kept)
    if len(idx) < 2:
        return {"series": None, "pixel": None}
    s = np.abs(np.diff(st["ndvi_mean"][idx]))
    px = []
    for a, b in zip(idx[:-1], idx[1:]):
        both = st["valid"][a] & st["valid"][b]
        if both.any():
            px.append(float(np.nanmean(np.abs(st["ndvi"][b][both] - st["ndvi"][a][both]))))
    return {"series": round(float(np.nanmedian(s)), 4), "pixel": round(float(np.median(px)), 4) if px else None}


def contact_sheet(years: np.ndarray, dn: np.ndarray, path: Path, title: str) -> None:
    """Grid of every year's true-colour image with ONE fixed stretch for all years (FR-15).
    Why fixed: a per-year stretch would hide or invent brightness changes. Clouds,
    stripes and flicker should show up exactly as they are."""
    refl = reflectance(dn[:, [R, G, B]])
    rgb = np.clip(np.nan_to_num(refl, nan=0.0) / 0.3, 0, 1) ** (1 / 1.6)  # 0–0.3 reflectance, gentle gamma
    tile, cols = 120, 9
    rows = int(np.ceil(len(years) / cols))
    sheet = Image.new("RGB", (cols * tile, rows * (tile + 16) + 24), "black")
    d = ImageDraw.Draw(sheet)
    d.text((4, 4), title, fill="yellow")
    for i, y in enumerate(years):
        im = Image.fromarray((rgb[i].transpose(1, 2, 0) * 255).astype(np.uint8)).resize((tile, tile))
        x0, y0 = (i % cols) * tile, 24 + (i // cols) * (tile + 16)
        sheet.paste(im, (x0, y0 + 16))
        d.text((x0 + 2, y0 + 2), str(int(y)), fill="white")
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, quality=85)


def score_one(approach: str, place: str, n: int) -> dict | None:
    """Scores one approach × place × N at all three area sizes. Returns None if that file wasn't produced."""
    f = OUT_ROOT / approach / f"{place}_n{n}.npz"
    if not f.exists():
        return None
    z = np.load(f)
    years, dn = z["years"], z["dn"]
    out = {}
    for km, px in CROPS.items():
        st = year_stats(reflectance(crop(dn, px)))
        kept = st["clear_pct"] >= MIN_CLEAR_PCT
        cloudy = kept & (st["cloud_frac"] > CLOUD_YEAR_FRAC)
        out[f"{km}km"] = {
            "years_total": int(len(years)),
            "years_kept": int(kept.sum()),
            "years_skipped_lt60": [int(y) for y in years[~kept]],
            "clean_year_pct": round(100 * (1 - cloudy.sum() / max(kept.sum(), 1)), 1),
            "cloudy_years": [int(y) for y in years[cloudy]],
            "median_clear_pct": round(float(np.median(st["clear_pct"])), 1),
            "flicker": flicker(st, kept) if place == CONTROL else None,
        }
    if n == 4:  # one sheet per approach and place is enough to eyeball
        contact_sheet(years, dn, OUT_ROOT / "sheets" / f"{approach}_{place}.jpg", f"{approach} {place} N=4 10km")
    return out


def main() -> None:
    """Scores everything that exists, writes scores.json, and prints a compact table.
    Approaches can be passed as arguments to score only some of them."""
    approaches = sys.argv[1:] or APPROACHES
    scores: dict = {}
    for a in approaches:
        for p in PLACES:
            for n in NS:
                s = score_one(a, p, n)
                if s is not None:
                    scores.setdefault(a, {}).setdefault(p, {})[f"n{n}"] = s
    (Path(__file__).parent / "scores.json").write_text(json.dumps(scores, indent=2))
    print(f"{'approach':13} {'place':13} N  km  kept/total clean%  med-clear  flicker(series,pixel)")
    for a, ps in scores.items():
        for p, ns in ps.items():
            for n, kms in ns.items():
                for km, s in kms.items():
                    fl = s["flicker"] or {}
                    print(f"{a:13} {p:13} {n[1:]:2} {km[:-2]:>3} {s['years_kept']:>4}/{s['years_total']:<5} "
                          f"{s['clean_year_pct']:>6} {s['median_clear_pct']:>9}  {fl.get('series','')} {fl.get('pixel','')}")


if __name__ == "__main__":
    main()
