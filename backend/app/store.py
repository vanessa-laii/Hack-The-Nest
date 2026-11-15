# Disk is the source of truth (PLAN §Architecture): reads/writes stories under DATA_DIR/stories/.
# Inputs: story ids; story dicts to save (validated against the contract first).
# Outputs: story dicts, gallery items, files story.json + index.json.
#
# How it works:
#   1. Story ids are built from place, country and area size, and double as cache keys.
#   2. An id is checked against ID_RE before it becomes a folder name.
#   3. save_story validates the story, then writes it atomically (tmp file + rename).
#   4. load_story and gallery read the files back.
#
# Disk, not a database, so cached stories play with Wi-Fi off at the demo (HL-9).
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from app.config import settings
from app.contract_check import assert_valid_story

# The id format from CONTRACTS §2. Also a security check: an id becomes a folder
# name, so '/', '..' or '\' must never get through.
ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")


def stories_dir() -> Path:
    """DATA_DIR/stories, the root for every saved story (CONTRACTS §6).
    A function so tests can point DATA_DIR at a temp folder."""
    return settings.data_dir / "stories"


def slugify(text: str) -> str:
    """'São Paulo, Brazil' -> 'sao-paulo-brazil'. Ids appear in URLs and folder
    names, and the contract allows only [a-z0-9-]."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower())
    return hyphenated.strip("-")


def story_id(place_slug: str, country_code: str, aoi_km: float) -> str:
    """Builds '<place>-<country>-<aoi>km' (e.g. 'lake-mead-us-6km'), at most 64 chars.
    The exact size is kept ('6p5km') so 5.5 and 6.5 km never share a cache entry.
    Raises ValueError if the place name has no usable characters (e.g. '!!!')."""
    slug = slugify(place_slug)
    if not slug:
        raise ValueError(f"place name {place_slug!r} gives an empty id slug")

    country = slugify(country_code) or "xx"
    size = f"{aoi_km:g}".replace(".", "p")  # '.' isn't allowed in ids
    tail = f"-{country}-{size}km"

    # Trim the place name, not the tail: the tail is what keeps ids unique.
    trimmed_slug = slug[: 64 - len(tail)].rstrip("-")
    return trimmed_slug + tail


def story_path(sid: str) -> Path | None:
    """Path to a story's story.json, or None if the id is malformed.
    Callers treat a bad id like an unknown one: a plain 404 that reveals nothing."""
    if not ID_RE.match(sid):
        return None
    return stories_dir() / sid / "story.json"


def load_story(sid: str) -> dict | None:
    """Reads a saved story, or returns None if there isn't one.
    No validation here: every story was validated when it was saved."""
    path = story_path(sid)
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_story(story: dict) -> Path:
    """Validates a story (CLAUDE.md #4), writes it to a .tmp file, then renames it.
    The rename is atomic, so nobody ever sees a half-written story.json."""
    assert_valid_story(story)
    path = story_path(story["id"])
    if path is None:
        raise ValueError(f"bad story id {story['id']!r}")
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(".json.tmp")
    text = json.dumps(story, indent=2, ensure_ascii=False)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def gallery() -> list[dict]:
    """The gallery list from stories/index.json, or [] if there isn't one yet.
    A separate index so the gallery doesn't open every story.json."""
    index_path = stories_dir() / "index.json"
    if not index_path.exists():
        return []
    index = json.loads(index_path.read_text(encoding="utf-8"))
    return index.get("stories", [])
