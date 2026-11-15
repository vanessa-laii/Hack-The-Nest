# Spike S2b proof for failure #1: zoomed before/after crops of the Gordie Howe bridge window, 2022–2026.
# Inputs: data/cache/s2b/delray.* via run_s2b.load; backend composite_series.
# Outputs: spikes/s2b-mask/delray_bridge_zoom.jpg (top row before, bottom row after; holes in magenta).
#
# Why magenta for holes here: on the river, black holes and dark water look alike in a small crop.
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_s2b as H  # noqa: E402
from app.pipeline import composite as C  # noqa: E402

YEARS = [2022, 2023, 2024, 2025, 2026]


def tile(dn: np.ndarray) -> Image.Image:
    """Fixed-stretch RGB of the bridge window, holes magenta, upscaled 5x."""
    win = dn[:, H.BRIDGE[0], H.BRIDGE[1]]
    r = np.nan_to_num(H.refl(win)[[2, 1, 0]], nan=0.0)
    rgb = (np.clip(r / 0.3, 0, 1) ** (1 / 1.6) * 255).astype(np.uint8).transpose(1, 2, 0)
    rgb[(win == 0).all(0)] = [255, 0, 255]
    return Image.fromarray(rgb).resize((rgb.shape[1] * 5, rgb.shape[0] * 5), Image.NEAREST)


def main() -> None:
    by_year, _ = H.load("delray")
    before = H.before_series(by_year)
    after = C.composite_series(by_year)
    tiles = [[tile(before[y]) for y in YEARS], [tile(after[y].dn) for y in YEARS]]
    tw, th = tiles[0][0].size
    im = Image.new("RGB", (tw * len(YEARS), 2 * (th + 18)), "black")
    d = ImageDraw.Draw(im)
    for row, label in enumerate(["before", "after"]):
        for i, y in enumerate(YEARS):
            im.paste(tiles[row][i], (i * tw, row * (th + 18) + 18))
            d.text((i * tw + 4, row * (th + 18) + 3), f"{y} {label}: holes {H.hole_pct((before if row == 0 else {k: v.dn for k, v in after.items()})[y], H.BRIDGE)}%", fill="yellow")
    im.save(HERE / "delray_bridge_zoom.jpg", quality=92)


if __name__ == "__main__":
    main()
