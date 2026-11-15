# Spike S2b check for failure #3: Willow Run 2020 composites from the exact 4 scenes of the reference sheet.
# Inputs: data/cache/s2b/willow-run-2020ref.npz/.json (fetch_ref2020.py) + willow-run cache (neighbouring years for the guard).
# Outputs: printed cloud-like %, spikes/s2b-mask/willow-run-2020_compare.jpg (rows: masks/methods), entries in results_willow2020.json.
#
# Why separate: the 6-clearest download picked other 2020 scenes, so the failure only shows
# with the reference set. Compares: bits 1–4 mask (what the reference sheet appears to have
# used), the full ADR-002 mask (bits 0–5), and composite_series.
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import run_s2b as H  # noqa: E402  (also puts backend/ on sys.path)
from app.pipeline import composite as C  # noqa: E402
from app.pipeline.types import SceneMeta  # noqa: E402


def main() -> None:
    root = REPO / "data" / "cache" / "s2b"
    ref = np.load(root / "willow-run-2020ref.npz")["scenes"][:, :7]
    meta = json.loads((root / "willow-run-2020ref.json").read_text())["scenes"]
    metas = [SceneMeta(m["id"], m["datetime"], 2020, m["platform"], m["cloud_cover"], None) for m in meta]
    fill = C._fill_mask(ref)
    # (1) the reference sheet's apparent rule: cloud/shadow bits 1–4 only (snow bit 5 NOT masked).
    v14 = ~fill & ((ref[:, 6] & 0b11110) == 0)
    m14, _ = C._masked_median(ref[:, :6], v14)
    # (2) plain ADR-002 median with the full mask (bits 0–5, DN 0/65535).
    vfull = ~fill & ~C._flag_mask(ref)
    mfull, _ = C._masked_median(ref[:, :6], vfull)
    # (3) composite_series with the real neighbouring years so the guard behaves as in production.
    by_year, _ = H.load("willow-run")
    by_year[2020] = (ref, metas)
    after = C.composite_series(by_year)[2020]
    # (4) outlier rejection's own effect: bits 1–4 mask + rejection (shows fix B in isolation).
    m14r, _ = C._masked_median(ref[:, :6], C._reject_outliers(ref[:, 0], v14))
    rows = {"bits1-4 (reference)": m14, "bits1-4 + outlier rejection": m14r,
            "full ADR-002 mask": mfull, "composite_series": after.dn}
    res = {k: {"cloud_like_pct": H.cloud_like_pct(v), "holes_pct": H.hole_pct(v)} for k, v in rows.items()}
    res["composite_series_notes"] = after.notes
    res["scene_0801_qa_values"] = [int(x) for x in np.unique(ref[2, 6])]
    for k, v in res.items():
        print(k, v)
    (HERE / "results_willow2020.json").write_text(json.dumps(res, indent=1))
    tile = 300
    im = Image.new("RGB", (tile * len(rows), tile + 20), "black")
    d = ImageDraw.Draw(im)
    for i, (k, v) in enumerate(rows.items()):
        r = np.nan_to_num(H.refl(v)[[2, 1, 0]], nan=0.0)
        rgb = (np.clip(r / 0.3, 0, 1) ** (1 / 1.6) * 255).astype(np.uint8).transpose(1, 2, 0)
        im.paste(Image.fromarray(rgb).resize((tile, tile), Image.NEAREST), (i * tile, 20))
        d.text((i * tile + 4, 4), f"2020 {k}", fill="yellow")
    im.save(HERE / "willow-run-2020_compare.jpg", quality=90)


if __name__ == "__main__":
    main()
