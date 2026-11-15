# Mock mode (EM_MOCK=1): serves the contract samples and generates labelled placeholder images.
# Inputs: contracts/samples/story.sample.json + events.sample.jsonl; settings.data_dir.
# Outputs: sample story dict, an async replay of sample events, PNG/JPG placeholders under DATA_DIR/samples/.
#
# Why mock mode exists: Vanessa builds the whole web app (search, live progress,
# player) before the real pipeline works (PLAN Phase 0/1). Mock mode follows the
# same contract as the real backend, so switching over is just EM_MOCK=0.
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from typing import AsyncIterator

from PIL import Image, ImageDraw

from app.config import CONTRACTS_DIR, settings

SAMPLES = CONTRACTS_DIR / "samples"
# Gap between replayed events. ~1 s per CONTRACTS §3, slow enough to watch the
# progress UI update, fast enough that a full replay takes about 20 s.
EVENT_DELAY_S = 1.0


def sample_story() -> dict:
    """Reads the contract sample from disk on every call. Why not cache it:
    while the contract is being agreed, edits to the sample should show up
    without restarting the server. It's a tiny file."""
    return json.loads((SAMPLES / "story.sample.json").read_text(encoding="utf-8"))


def sample_events() -> list[dict]:
    """Parses events.sample.jsonl (one JSON event per line), skipping blank lines.
    Why read the contract file directly: the mock stream is then exactly the sample
    Vanessa's types were generated from, with no second copy to drift."""
    lines = (SAMPLES / "events.sample.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def gallery_item(story: dict) -> dict:
    """Builds a GalleryItem (CONTRACTS §1) from a full story.

    What: the latest frame's thumbnail, the first change chapter's headline,
    and the number of change chapters.
    Why derive it: the gallery card then always matches the story it opens, with
    no separate list to keep in sync. has_narration checks for the file the web
    app writes, because narration belongs to Vanessa's side (CONTRACTS §6).
    """
    change_ch = [c for c in story["chapters"] if c["kind"] == "change"]
    last = story["frames"][-1]
    return {
        "id": story["id"],
        "display_name": story["place"]["display_name"],
        "first_year": story["coverage"]["first_year"],
        "last_year": story["coverage"]["last_year"],
        "thumb_url": last["thumb_url"],
        "headline": change_ch[0]["headline"] if change_ch else story["chapters"][0]["headline"],
        "chapter_count": len(change_ch),
        "has_narration": (settings.data_dir / "stories" / story["id"] / "narration" / "narration.json").exists(),
    }


async def replay_events(after_seq: int = -1) -> AsyncIterator[dict]:
    """Yields the sample build events one by one, about 1 s apart.

    What: skips events with seq <= after_seq and stamps each one with the
    current time.
    Why: after_seq comes from the SSE Last-Event-ID header, so a browser that
    reconnects mid-build resumes where it left off rather than replaying from
    the start (CONTRACTS §1). Fresh timestamps make the live feed look real.
    """
    events = sample_events()
    remaining = [ev for ev in events if ev["seq"] > after_seq]
    if not remaining:
        # Resuming at or past the end: send the final done/error event again.
        # Why: EventSource reconnects automatically whenever a stream closes. An
        # empty stream would make a client that missed `done` reconnect forever
        # without ever learning the build finished.
        remaining = events[-1:]
    for ev in remaining:
        await asyncio.sleep(EVENT_DELAY_S)
        yield {**ev, "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


# ---------- Placeholder images ----------
# Mock mode only. The sample story references /files/samples/frames/<year>.jpg,
# so the player needs real image files at those paths. They are drawn shapes,
# stamped "NOT REAL" so nobody mistakes them for imagery. The real pipeline only
# ever shows Landsat pixels (HL-4).

def _placeholder(year: int, water_frac: float, size: int) -> Image.Image:
    """Draws a tan square with a blue 'lake' whose area tracks that year's
    water_frac. Why: the film then visibly shrinks and regrows like the sample
    data says, so the crossfade and chart sync can be checked by eye."""
    img = Image.new("RGB", (size, size), (196, 170, 128))  # desert tan
    d = ImageDraw.Draw(img)
    r = math.sqrt(max(water_frac, 0.01) / math.pi) * size  # circle area ∝ water fraction
    c = size / 2
    d.ellipse([c - r, c - r, c + r, c + r], fill=(38, 84, 140))
    # Black banner with the label, scaled to the image size so it reads on 256 px thumbs too.
    s = max(size // 32, 1)
    d.rectangle([0, 0, size, s * 5], fill=(0, 0, 0))
    d.text((s, s), f"SAMPLE {year} - NOT REAL", fill=(255, 255, 0), font_size=s * 2.5)
    return img


def ensure_placeholders() -> None:
    """Writes any missing placeholder frames (1024 px), thumbs (256 px) and
    change masks under DATA_DIR/samples/. Runs at startup in mock mode.

    Why generate instead of committing them: data/ is git-ignored (no binaries
    in git, CLAUDE.md), and generating means the images always match the
    current sample's years. Existing files are kept, so restarts are instant.
    """
    story = sample_story()
    root = settings.data_dir / "samples"
    for sub in ("frames", "thumbs", "masks"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    water = {s["year"]: s["water_frac"] for s in story["metrics"]["series"]}
    for f in story["frames"]:
        y = f["year"]
        frame, thumb = root / "frames" / f"{y}.jpg", root / "thumbs" / f"{y}.jpg"
        if not frame.exists():
            _placeholder(y, water[y], 1024).save(frame, quality=85)
        if not thumb.exists():
            _placeholder(y, water[y], 256).save(thumb, quality=85)
    # A change mask is a greyscale PNG (white = changed pixels) for the evidence
    # panel overlay. Here it's a ring, standing in for a shrinking shoreline.
    for ch in story["changes"]:
        if ch.get("mask_url"):
            mask = root / "masks" / f"{ch['id']}.png"
            if not mask.exists():
                m = Image.new("L", (1024, 1024), 0)
                ImageDraw.Draw(m).ellipse([200, 200, 824, 824], outline=255, width=60)
                m.save(mask)
