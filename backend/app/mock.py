# Mock mode (EM_MOCK=1): serves the contract samples and generates labelled placeholder images.
# Inputs: contracts/samples/story.sample.json + events.sample.jsonl; settings.data_dir.
# Outputs: sample story dict, async replay of sample events, placeholder images in DATA_DIR/samples/
#
# How it works:
#   1. Read the sample story and sample events straight from contracts/samples/.
#   2. Build the gallery card for a story from the story itself.
#   3. Replay the sample events about 1 s apart, resuming after a given seq.
#   4. At startup, draw any missing placeholder frames, thumbs and masks.
#
# Mock mode lets the web app be built before the real pipeline works (PLAN Phase 0/1).
# It follows the same contract, so switching over is just EM_MOCK=0.
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from PIL import Image, ImageDraw

from app.config import CONTRACTS_DIR, settings

SAMPLES = CONTRACTS_DIR / "samples"
# ~1 s per CONTRACTS §3: slow enough to watch the progress UI, and a full replay
# takes about 20 s.
EVENT_DELAY_S = 1.0


# --- Sample data ---

def sample_story() -> dict:
    """Reads the contract sample from disk on every call (it's tiny).
    Not cached, so edits to the sample show up without a server restart."""
    path = SAMPLES / "story.sample.json"
    return json.loads(path.read_text(encoding="utf-8"))


def sample_events() -> list[dict]:
    """Parses events.sample.jsonl (one JSON event per line), skipping blank lines.
    Reading the contract file means there is no second copy to drift."""
    path = SAMPLES / "events.sample.jsonl"
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _has_narration(story_id: str) -> bool:
    """True if the web app has written narration for this story (its side, CONTRACTS §6)."""
    narration = settings.data_dir / "stories" / story_id / "narration" / "narration.json"
    return narration.exists()


def gallery_item(story: dict) -> dict:
    """Builds a GalleryItem (CONTRACTS §1) from a full story: latest thumbnail, first
    change headline, change count. Derived, so the card always matches its story."""
    change_chapters = [c for c in story["chapters"] if c["kind"] == "change"]
    if change_chapters:
        headline = change_chapters[0]["headline"]
    else:
        headline = story["chapters"][0]["headline"]
    last_frame = story["frames"][-1]
    return {
        "id": story["id"],
        "display_name": story["place"]["display_name"],
        "first_year": story["coverage"]["first_year"],
        "last_year": story["coverage"]["last_year"],
        "thumb_url": last_frame["thumb_url"],
        "headline": headline,
        "chapter_count": len(change_chapters),
        "has_narration": _has_narration(story["id"]),
    }


async def replay_events(after_seq: int = -1) -> AsyncIterator[dict]:
    """Yields sample events with seq > after_seq, about 1 s apart, stamped with now.
    after_seq comes from Last-Event-ID, so a reconnecting browser resumes mid-build."""
    events = sample_events()
    remaining = [ev for ev in events if ev["seq"] > after_seq]
    if not remaining:
        # At or past the end: re-send the final done/error event. An empty stream
        # would make EventSource reconnect forever without learning the build ended.
        remaining = events[-1:]
    for ev in remaining:
        await asyncio.sleep(EVENT_DELAY_S)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        yield {**ev, "ts": now}


# --- Placeholder images ---
# Mock mode only. The sample story points at /files/samples/frames/<year>.jpg, so
# those files must exist. They are drawn shapes stamped "NOT REAL"; the real
# pipeline only ever shows Landsat pixels (HL-4).

def _placeholder(year: int, water_frac: float, size: int) -> Image.Image:
    """Draws a tan square with a blue 'lake' sized by that year's water_frac,
    so the film visibly shrinks and regrows like the sample data."""
    img = Image.new("RGB", (size, size), (196, 170, 128))  # desert tan
    draw = ImageDraw.Draw(img)

    # Circle area is proportional to the water fraction.
    radius = math.sqrt(max(water_frac, 0.01) / math.pi) * size
    centre = size / 2
    lake_box = [centre - radius, centre - radius, centre + radius, centre + radius]
    draw.ellipse(lake_box, fill=(38, 84, 140))

    # Black label banner, scaled so it is readable on 256 px thumbs too.
    unit = max(size // 32, 1)
    draw.rectangle([0, 0, size, unit * 5], fill=(0, 0, 0))
    label = f"SAMPLE {year} - NOT REAL"
    draw.text((unit, unit), label, fill=(255, 255, 0), font_size=unit * 2.5)
    return img


def _write_ring_mask(path: Path) -> None:
    """A greyscale change mask (white = changed): a ring standing in for a shoreline."""
    mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(mask).ellipse([200, 200, 824, 824], outline=255, width=60)
    mask.save(path)


def ensure_placeholders() -> None:
    """Writes any missing frames (1024 px), thumbs (256 px) and masks in DATA_DIR/samples/.
    Generated, not committed: data/ is git-ignored and the years always match the sample.
    Existing files are kept, so restarts are instant."""
    story = sample_story()
    root = settings.data_dir / "samples"
    for sub in ("frames", "thumbs", "masks"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    water_by_year = {}
    for point in story["metrics"]["series"]:
        water_by_year[point["year"]] = point["water_frac"]

    for frame_info in story["frames"]:
        year = frame_info["year"]
        frame_path = root / "frames" / f"{year}.jpg"
        thumb_path = root / "thumbs" / f"{year}.jpg"
        if not frame_path.exists():
            _placeholder(year, water_by_year[year], 1024).save(frame_path, quality=85)
        if not thumb_path.exists():
            _placeholder(year, water_by_year[year], 256).save(thumb_path, quality=85)

    for change in story["changes"]:
        if not change.get("mask_url"):
            continue
        mask_path = root / "masks" / f"{change['id']}.png"
        if not mask_path.exists():
            _write_ring_mask(mask_path)
