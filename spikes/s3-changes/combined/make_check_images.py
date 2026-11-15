# Spike S3b helper: draws before/after thumbnails of every detected change, for a visual check.
# Inputs: place keys (argv; default: the four the owner asked for), places.json, data/stories/.
# Outputs: data/spikes/s3-check/<place>.jpg (git-ignored), one row per detected change.
#
# How it works:
#   1. Run the combined detect() on the place.
#   2. Per change: the whole before and after frames with the region box, then a close-up of
#      the region (padded to at least 1.2 km) in both years, and a caption line.
#   3. Stack the rows into one JPEG per place.
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
from detect import detect  # noqa: E402

OUT_DIR = REPO / "data" / "spikes" / "s3-check"
DEFAULT_PLACES = ["willow-run", "delray", "summerlin", "palm-jumeirah"]
TILE = 320              # each of the 4 thumbnails per row is TILE × TILE pixels
CAPTION = 22            # caption strip height
MIN_ZOOM_SHARE = 0.2    # a close-up shows at least 20% of the 6 km side (1.2 km)
BOX = (255, 40, 40)


def to_pixels(bbox: list[float], place_bbox: list[float], size: int) -> list[float]:
    """Lon/lat [W, S, E, N] → frame pixels [left, top, right, bottom] (row 0 = north)."""
    west, south, east, north = place_bbox
    left = (bbox[0] - west) / (east - west) * size
    right = (bbox[2] - west) / (east - west) * size
    top = (north - bbox[3]) / (north - south) * size
    bottom = (north - bbox[1]) / (north - south) * size
    return [left, top, right, bottom]


def zoom_window(box: list[float], size: int) -> tuple[int, int, int, int]:
    """A square around the box, padded, at least MIN_ZOOM_SHARE of the frame, inside it."""
    side = max(box[2] - box[0], box[3] - box[1]) * 1.6
    side = min(max(side, MIN_ZOOM_SHARE * size), size)
    centre_x = (box[0] + box[2]) / 2
    centre_y = (box[1] + box[3]) / 2
    left = min(max(centre_x - side / 2, 0), size - side)
    top = min(max(centre_y - side / 2, 0), size - side)
    return int(left), int(top), int(left + side), int(top + side)


def tile(frame: Image.Image, box: list[float], window: tuple[int, int, int, int],
         label: str) -> Image.Image:
    """One thumbnail: the window of the frame, scaled to TILE, with the box and a year label."""
    scale = TILE / (window[2] - window[0])
    image = frame.crop(window).resize((TILE, TILE))
    draw = ImageDraw.Draw(image)
    shifted = [(box[0] - window[0]) * scale, (box[1] - window[1]) * scale,
               (box[2] - window[0]) * scale, (box[3] - window[1]) * scale]
    draw.rectangle(shifted, outline=BOX, width=2)
    draw.rectangle([0, 0, 60, 16], fill=(0, 0, 0))
    draw.text((4, 2), label, fill=(255, 255, 0))
    return image


def change_row(story: Path, change: dict, place_bbox: list[float]) -> Image.Image:
    """Whole before, whole after, close-up before, close-up after, and a caption."""
    before = Image.open(story / "frames" / f"{change['before_year']}.jpg").convert("RGB")
    after = Image.open(story / "frames" / f"{change['after_year']}.jpg").convert("RGB")
    size = before.width
    box = to_pixels(change["region"]["bbox"], place_bbox, size)
    whole = (0, 0, size, size)
    close = zoom_window(box, size)
    row = Image.new("RGB", (4 * TILE, TILE + CAPTION), (20, 20, 20))
    row.paste(tile(before, box, whole, str(change["before_year"])), (0, CAPTION))
    row.paste(tile(after, box, whole, str(change["after_year"])), (TILE, CAPTION))
    row.paste(tile(before, box, close, str(change["before_year"])), (2 * TILE, CAPTION))
    row.paste(tile(after, box, close, str(change["after_year"])), (3 * TILE, CAPTION))
    sensor = " [sensor switch]" if change["sensor_switch_flag"] else ""
    caption = (f"{change['id']} {change['kind']} / {change['metric']} "
               f"{change['start_year']}-{change['end_year']} {change['magnitude']:+.3f}"
               f"{sensor} - {change['region']['description']}")
    ImageDraw.Draw(row).text((6, 5), caption, fill=(255, 255, 255))
    return row


def main() -> int:
    keys = sys.argv[1:] or DEFAULT_PLACES
    places = json.loads((HERE.parent / "places.json").read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for place in places:
        if place["key"] not in keys:
            continue
        story = REPO / "data" / "stories" / place["story_id"]
        imagery = json.loads((story / "imagery.json").read_text(encoding="utf-8"))
        changes = detect(story)
        rows = []
        for change in changes:
            rows.append(change_row(story, change, imagery["place"]["bbox"]))
        if not rows:
            print(f"{place['key']}: no changes, no image")
            continue
        sheet = Image.new("RGB", (4 * TILE, len(rows) * (TILE + CAPTION)))
        for i, row in enumerate(rows):
            sheet.paste(row, (0, i * (TILE + CAPTION)))
        out = OUT_DIR / f"{place['key']}.jpg"
        sheet.save(out, quality=88)
        print(f"{place['key']}: {len(rows)} changes -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
