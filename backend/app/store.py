# Disk is the source of truth (PLAN §Architecture): reads/writes stories under DATA_DIR/stories/.
# Inputs: story ids; story dicts to save (validated against the contract first).
# Outputs: story dicts, gallery items, files story.json + index.json.
#
# Why disk rather than a database: cached stories must play with Wi-Fi off at
# the demo (HL-9). MongoDB only mirrors the gallery list and is optional.
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from app.config import settings
from app.contract_check import assert_valid_story

# The id format from CONTRACTS §2. Also a security check: an id becomes a
# folder name, so anything with '/', '..' or '\' must never get through.
ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")


def stories_dir() -> Path:
    return settings.data_dir / "stories"


def slugify(text: str) -> str:
    """'São Paulo, Brazil' -> 'sao-paulo-brazil'.

    What: strips accents to ASCII, lowercases, and collapses everything else
    into single hyphens.
    Why: story ids appear in URLs and folder names, and the contract only
    allows [a-z0-9-].
    """
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def story_id(place_slug: str, country_code: str, aoi_km: float) -> str:
    """Builds <place-slug>-<country>-<aoi>km, e.g. 'lake-mead-us-6km', at most 64 chars.

    Why this shape: the same place at the same area size always gets the same
    id, so it doubles as the cache key ("already built? serve it").
    Why trim the slug rather than the tail: the country and km suffix are what
    keep ids unique, so a long place name loses characters first.
    """
    tail = f"-{country_code.lower()}-{int(round(aoi_km))}km"
    return slugify(place_slug)[: 64 - len(tail)].rstrip("-") + tail


def story_path(sid: str) -> Path | None:
    """Path to a story's story.json, or None if the id is malformed.
    Why None and not an exception: callers treat a bad id the same as an
    unknown one, a plain 404, without revealing anything about the filesystem."""
    if not ID_RE.match(sid):
        return None
    return stories_dir() / sid / "story.json"


def load_story(sid: str) -> dict | None:
    """Reads a saved story, or returns None if there isn't one."""
    p = story_path(sid)
    if p is None or not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_story(story: dict) -> Path:
    """Validates a story, then writes it to disk.

    What: checks the contract first (CLAUDE.md #4), writes to a .tmp file, then
    renames it over story.json.
    Why the tmp + rename: the rename is atomic, so the web app (or a crash
    mid-write) never sees a half-written story.json, only the old file or the
    complete new one.
    """
    assert_valid_story(story)
    p = story_path(story["id"])
    if p is None:
        raise ValueError(f"bad story id {story['id']!r}")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(story, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return p


def gallery() -> list[dict]:
    """Returns the gallery list from stories/index.json, or [] if there isn't
    one yet. Why a separate index: the gallery page shouldn't open every
    story.json just to draw thumbnails."""
    idx = stories_dir() / "index.json"
    if not idx.exists():
        return []
    return json.loads(idx.read_text(encoding="utf-8")).get("stories", [])
