# Renders each year's composite as a true-colour JPEG frame and thumbnail, one fixed stretch.
# Inputs: {year: YearComposite} (dn uint16 [6, H, W], 0 = hole) and an output directory.
# Outputs: out_dir/frames/<year>.jpg (1024²), out_dir/thumbs/<year>.jpg (256²); {year: paths}.
#
# How it works:
#   1. Take the red, green and blue bands and turn raw values into reflectance (DR-1).
#   2. Map reflectance 0–0.30 to 0–255 with a fixed gentle curve, the same for every image.
#   3. Paint holes (no clear view that year) flat grey.
#   4. Scale up to the 1024² frame with crisp blocks, down to the 256² thumbnail by averaging.
#   5. Save both as JPEGs without readers ever seeing half a file.
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from PIL import Image

from app.pipeline.types import BLUE, GREEN, RED, SR_OFFSET, SR_SCALE, YearComposite

# --- The one fixed stretch (FR-15) ---
# Every frame of every place uses these numbers, and none depend on the image, so a frame
# that looks brighter really was brighter on the ground.
# 0–0.30 reflectance covers nearly all land: forest, cities, soil, desert rock. Only
# clouds, snow and salt flats clip to white.
# Gamma 1/1.6 lifts dark forest and water without washing out bright desert.
# Tuned once by eye on Lake Mead, Paradise and Central Park.
STRETCH_MIN = 0.0    # reflectance mapped to black
STRETCH_MAX = 0.30   # reflectance mapped to white
GAMMA = 1 / 1.6      # exponent applied after the linear 0–1 stretch

# Holes are flat mid grey: it reads as "no data", not as land, and is never a colour
# borrowed from another year (holes are never filled).
HOLE_RGB = (128, 128, 128)

FRAME_PX = 1024      # story.schema frames[].image_url: 1024 x 1024
THUMB_PX = 256       # story.schema frames[].thumb_url: 256 x 256
JPEG_QUALITY = 88

# Upscaling uses NEAREST: each 30 m pixel becomes a crisp block (FR-55), and no smoothing
# invents detail between real samples (HL-4). Downscaling uses BOX, which only averages
# real pixels.
UPSCALE_FILTER = Image.Resampling.NEAREST
DOWNSCALE_FILTER = Image.Resampling.BOX


def stretch_params() -> tuple[float, float, float]:
    """Returns the fixed (min, max, gamma) stretch.
    Tests call it to prove the stretch is a constant, not computed from the image."""
    return STRETCH_MIN, STRETCH_MAX, GAMMA


def to_rgb(dn: np.ndarray) -> np.ndarray:
    """Turns a composite's raw values [6, H, W] into an 8-bit true-colour image [H, W, 3].
    The same mapping for every input is what makes year-to-year brightness real (FR-15)."""
    rgb_dn = dn[[RED, GREEN, BLUE]].astype(np.float32)   # [3, H, W]
    refl = rgb_dn * SR_SCALE + SR_OFFSET                  # DR-1 scaling
    stretched = (refl - STRETCH_MIN) / (STRETCH_MAX - STRETCH_MIN)
    scaled = np.clip(stretched, 0.0, 1.0)
    curved = np.power(scaled, GAMMA)
    out = np.round(curved * 255.0).astype(np.uint8)       # [3, H, W]
    out = np.moveaxis(out, 0, -1).copy()                  # [H, W, 3]

    # A hole is 0 in any band. composite.py clears whole pixels, but checking all six is safe.
    hole = (dn == 0).any(axis=0)
    out[hole] = HOLE_RGB
    return out


def _save_jpeg_atomic(img: Image.Image, path: Path) -> None:
    """Writes the JPEG to a temp file, then renames it over `path`.
    The web app may read frames during a build and must never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    img.save(tmp, format="JPEG", quality=JPEG_QUALITY)
    os.replace(tmp, path)  # atomic on the same filesystem, also on Windows


def render_frames(series: dict[int, YearComposite],
                  out_dir: Path) -> dict[int, tuple[Path, Path]]:
    """Renders every year to frames/<year>.jpg (1024²) and thumbs/<year>.jpg (256²).
    Returns {year: (frame_path, thumb_path)} in ascending year order."""
    out_dir = Path(out_dir)
    result: dict[int, tuple[Path, Path]] = {}
    for year in sorted(series):
        img = Image.fromarray(to_rgb(series[year].dn))  # uint8 [H, W, 3] → RGB
        frame = img.resize((FRAME_PX, FRAME_PX), UPSCALE_FILTER)

        # Thumbnail from the native image, not the upscaled frame, so it averages real pixels.
        # NEAREST only if the area is smaller than the thumbnail.
        if min(img.size) >= THUMB_PX:
            thumb_filter = DOWNSCALE_FILTER
        else:
            thumb_filter = UPSCALE_FILTER
        thumb = img.resize((THUMB_PX, THUMB_PX), thumb_filter)

        frame_path = out_dir / "frames" / f"{year}.jpg"
        thumb_path = out_dir / "thumbs" / f"{year}.jpg"
        _save_jpeg_atomic(frame, frame_path)
        _save_jpeg_atomic(thumb, thumb_path)
        result[year] = (frame_path, thumb_path)
    return result
