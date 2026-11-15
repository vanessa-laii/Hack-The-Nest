# Runs a full story build: imagery → changes → story assembly → save → gallery index.
# Inputs: a place query or an already-resolved Place, optional aoi_km, a `sink` for events,
#         and an optional deadline (time.monotonic()) after which nothing is saved.
# Outputs: the saved story dict; story.json and an updated stories/index.json under DATA_DIR.
#
# How it works:
#   1. run_imagery makes the frames, metrics and imagery.json, emitting its own events.
#   2. detect_changes finds 0–5 lasting changes; one `change` event is sent per change.
#      No changes at all raises NoLastingChanges: a story needs 1–5 change chapters (HL-2).
#   3. assemble_story writes the chapters and the 150–225 word script (stage "script").
#   4. In one worker thread: check the deadline, then store.save_story validates and saves
#      story.json and the gallery list (stories/index.json) gets this story at the top.
#      Both are written or neither is (stage "save").
#   5. A `done` event carries the story's URL.
#
# The caller (jobs.py) adds the 180 s timeout (HL-5) and turns exceptions into error events.
# A timeout stops this coroutine but not a worker thread it is waiting on, which is why the
# save thread checks the deadline itself before writing anything.
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from app import store
from app.pipeline import changes as change_detection
from app.pipeline.run import EventLog, Emit, _ignore_event, story_dir, run_imagery, write_json
from app.pipeline.story import assemble_story, headline
from app.pipeline.types import Place

# Share of the progress bar reached when each stage finishes; run.py stops at 0.64.
PROGRESS = {"changes": 0.75, "script": 0.88, "save": 0.97}


# Builds save from worker threads, so two of them could read-modify-write index.json at once.
_index_lock = threading.Lock()


class NoLastingChanges(Exception):
    """The place has no lasting change to tell, so there is no film to make (HL-2)."""


class DeadlinePassed(TimeoutError):
    """The build's time limit passed before saving, so nothing was saved (HL-5)."""


# --- Continuing the event log ---

class SeqTracker:
    """Wraps the caller's sink and remembers the last event that passed through it.
    run_imagery keeps its EventLog to itself (and run.py must not change), so build_story
    starts a new EventLog for the same story and sets its seq to one past the last one seen.
    Event numbers then go up by exactly 1 across both halves (CONTRACTS §3)."""

    def __init__(self, sink: Emit | None):
        self.sink = sink or _ignore_event
        self.last_seq = -1

    async def __call__(self, event: dict) -> None:
        """Notes the event's seq, then passes the event on to the caller's sink."""
        self.last_seq = event["seq"]
        await self.sink(event)

    def continue_log(self, story_id: str) -> EventLog:
        """A new EventLog for the rest of the build that picks up where run_imagery stopped."""
        log = EventLog(story_id, self)
        log.seq = self.last_seq + 1
        return log


SAVE_MARGIN_S = 5.0   # stop saving this long before the HL-5 deadline (see save_and_index)


# --- Gallery index ---

def gallery_item(story: dict, folder: Path) -> dict:
    """One GalleryItem (CONTRACTS §1) for a saved story.
    Its headline is the largest change's chapter (changes[] is largest-first)."""
    largest_id = story["changes"][0]["id"]
    largest_chapter = None
    for chapter in story["chapters"]:
        if chapter["change_id"] == largest_id:
            largest_chapter = chapter
            break
    has_narration = (folder / "narration" / "narration.json").exists()
    return {
        "id": story["id"],
        "display_name": story["place"]["display_name"],
        "first_year": story["coverage"]["first_year"],
        "last_year": story["coverage"]["last_year"],
        "thumb_url": story["frames"][-1]["thumb_url"],
        "headline": largest_chapter["headline"],
        "chapter_count": len(story["chapters"]),
        "has_narration": has_narration,
    }


def update_index(story: dict, folder: Path) -> None:
    """Puts this story first in stories/index.json, replacing any older entry with its id.
    Newest first is the gallery's order. The lock stops two builds losing each other's entry;
    write_json renames a temp file, so readers never see half a file."""
    index_path = store.stories_dir() / "index.json"
    newest = gallery_item(story, folder)
    with _index_lock:
        try:
            existing = store.gallery()
        except (OSError, ValueError):
            # An unreadable index must not block every future save; start it afresh.
            existing = []
        older = []
        for item in existing:
            if item["id"] != story["id"]:
                older.append(item)
        write_json(index_path, {"stories": [newest] + older})


def save_and_index(story: dict, folder: Path, deadline: float | None = None) -> None:
    """Saves story.json and adds it to the gallery index, both or neither. Runs in a thread.
    Checks the deadline first: after a timeout this thread may still be running, and a story
    saved then would appear after the browser was told it failed (HL-5)."""
    # Stop SAVE_MARGIN_S early: validating and writing take a moment, and the save must never
    # land after the TIMEOUT event has gone out (QA).
    if deadline is not None and time.monotonic() > deadline - SAVE_MARGIN_S:
        raise DeadlinePassed(f"deadline passed before saving {story['id']}")
    path = store.save_story(story)
    try:
        update_index(story, folder)
    except Exception:
        path.unlink(missing_ok=True)  # no story.json without its gallery entry
        raise


# --- The full build ---

async def announce_changes(emit: EventLog, found: list[dict]) -> None:
    """One `change` event per detected change, for the live feed."""
    for change in found:
        message = f"Change {change['start_year']}–{change['end_year']}: {headline(change)}"
        await emit("change", message, stage="changes", progress=PROGRESS["changes"], change={
            "change_id": change["id"],
            "kind": change["kind"],
            "start_year": change["start_year"],
            "end_year": change["end_year"],
            "region_description": change["region"]["description"],
        })


async def build_story(query: str, *, lat: float | None = None, lon: float | None = None,
                      display_name: str | None = None, aoi_km: float | None = None,
                      sink: Emit | None = None, place: Place | None = None,
                      deadline: float | None = None) -> dict:
    """Builds, validates and saves one story, emitting build events as it goes.
    `place` skips the geocoder; `deadline` (time.monotonic()) stops a late save.
    Returns the saved story dict. Raises NoLastingChanges if nothing lasting changed."""
    start = time.perf_counter()
    tracker = SeqTracker(sink)

    # 1. Imagery half
    imagery = await run_imagery(query, lat=lat, lon=lon, display_name=display_name,
                                aoi_km=aoi_km, sink=tracker, place=place)
    emit = tracker.continue_log(imagery.story_id)
    folder = story_dir(imagery.story_id)

    # 2. Changes
    await emit("progress", "Looking for lasting changes", stage="changes", status="started",
               progress=PROGRESS["changes"] - 0.05)
    found = await asyncio.to_thread(change_detection.detect_changes, folder)
    if not found:
        raise NoLastingChanges(f"no lasting changes found for {imagery.story_id}")
    await announce_changes(emit, found)

    # 3. Script
    generation = {
        "duration_s": round(time.perf_counter() - start, 2),
        "usage": {"exa_calls": 0, "llm_calls": 0, "vision_calls": 0},
        "warnings": [],
    }
    story = await asyncio.to_thread(assemble_story, folder, found, generation=generation)
    words = story["script"]["word_count"]
    await emit("progress", f"Wrote a {words}-word script", stage="script", status="done",
               progress=PROGRESS["script"])

    # 4. Save (validates against the contract first), together with the gallery entry
    await asyncio.to_thread(save_and_index, story, folder, deadline)
    await emit("progress", "Saved the story", stage="save", status="done",
               progress=PROGRESS["save"])

    # 5. Done
    await emit("done", f"{story['place']['display_name']} is ready", progress=1.0,
               story_url=f"/api/stories/{story['id']}")
    return story
