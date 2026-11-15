# Runs a full story build: imagery → changes → causes → script → save → gallery index.
# Inputs: a place query or an already-resolved Place, optional aoi_km, a `sink` for events,
#         and an optional deadline (time.monotonic()) after which nothing is saved.
# Outputs: the saved story dict; story.json and an updated stories/index.json under DATA_DIR.
#
# How it works:
#   1. run_imagery makes the frames, metrics and imagery.json, emitting its own events.
#   2. detect_changes finds 0–5 lasting changes; one `change` event is sent per change.
#      No changes at all raises NoLastingChanges: a story needs 1–5 change chapters (HL-2).
#   3. find_causes searches for documented causes and keeps only code-verified claims
#      (stages "research" and "verify", ADR-004). If too little time is left (HL-5), or the
#      agent fails, every change stays cause_unknown and a warning is recorded.
#   4. assemble_story writes the chapters and the 150–225 word script (stage "script").
#   5. In one worker thread: check the deadline, then store.save_story validates and saves
#      story.json and the gallery list (stories/index.json) gets this story at the top.
#      Both are written or neither is (stage "save").
#   6. A `done` event carries the story's URL.
#
# The caller (jobs.py) adds the 180 s timeout (HL-5) and turns exceptions into error events.
# A timeout stops this coroutine but not a worker thread it is waiting on, which is why the
# save thread checks the deadline itself before writing anything.
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from app import llm, store
from app.agent.causes import find_causes
from app.agent.trace import Budget, Trace
from app.pipeline import changes as change_detection
from app.pipeline.run import EventLog, Emit, _ignore_event, story_dir, run_imagery, write_json
from app.pipeline.story import assemble_story, headline
from app.pipeline.types import Place

# Share of the progress bar reached when each stage finishes; run.py stops at 0.64.
PROGRESS = {"changes": 0.75, "research": 0.80, "verify": 0.84, "script": 0.88, "save": 0.97}

# HL-5: the cause search needs roughly this long (about 7 model calls, paced). With less time
# left before the deadline it is skipped, and every change stays cause_unknown.
RESEARCH_MIN_S = 40.0
# Time kept back after the cause search for the script, the save and the done event.
AFTER_RESEARCH_S = 12.0


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


# --- Causes (S4 agent) ---

def research_time_s(deadline: float | None) -> float | None:
    """Seconds the cause search may take before the deadline, or None for no limit."""
    if deadline is None:
        return None
    return deadline - time.monotonic() - AFTER_RESEARCH_S


def stage_emitter(emit: EventLog):
    """An emit for find_causes that adds the progress value for its stage."""
    async def emit_with_progress(type_: str, message: str, *, stage: str, **extra) -> None:
        await emit(type_, message, stage=stage, progress=PROGRESS.get(stage), **extra)
    return emit_with_progress


async def research_causes(folder: Path, place: dict, found: list[dict], emit: EventLog,
                          deadline: float | None) -> tuple[dict | None, dict, list[str]]:
    """Runs the cause agent within the time left. Returns (causes or None, usage, warnings).
    None means every change stays cause_unknown (TR-8); the build carries on either way."""
    trace = Trace(folder)
    budget = Budget()  # HL-10 per story
    warnings: list[str] = []
    time_left = research_time_s(deadline)
    if time_left is not None and time_left < RESEARCH_MIN_S:
        warnings.append("Cause search skipped: too little time left before the time limit "
                        "(HL-5)")
        await emit("progress", "Not enough time left to search for causes", stage="research",
                   status="warning", progress=PROGRESS["research"])
        return None, usage_counts(budget), warnings

    await emit("progress", "Searching for documented causes", stage="research",
               status="started", progress=PROGRESS["research"] - 0.03)
    search = find_causes(folder, place, found, trace=trace, budget=budget,
                         emit=stage_emitter(emit))
    try:
        causes = await asyncio.wait_for(search, timeout=time_left)
    except asyncio.TimeoutError:
        warnings.append("Cause search stopped at the time limit (HL-5); causes left unknown")
        await emit("progress", "Cause search ran out of time", stage="research",
                   status="warning", progress=PROGRESS["verify"])
        return None, usage_counts(budget), warnings
    except Exception as error:  # an unexpected agent bug must not cost the user the film
        trace.log("check", "cause search failed", error=llm.describe_error(error))
        warnings.append("Cause search failed; causes left unknown")
        await emit("progress", "Cause search failed; causes left unknown", stage="research",
                   status="warning", progress=PROGRESS["verify"])
        return None, usage_counts(budget), warnings

    warnings.extend(causes.get("warnings", []))
    sourced = 0
    for chapter in causes["chapters"].values():
        if chapter["claims"]:
            sourced += 1
    await emit("progress", f"Found documented causes for {sourced} of {len(found)} changes",
               stage="verify", status="done", progress=PROGRESS["verify"])
    return causes, usage_counts(budget), warnings


def usage_counts(budget: Budget) -> dict:
    """story.generation.usage from the story's budget (HL-10). No image check yet."""
    return {"exa_calls": budget.used.get("exa", 0), "llm_calls": budget.used.get("llm", 0),
            "vision_calls": 0}


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

    # 3. Causes
    place_section = imagery.sections["place"]
    causes, usage, warnings = await research_causes(folder, place_section, found, emit,
                                                    deadline)

    # 4. Script
    generation = {
        "duration_s": round(time.perf_counter() - start, 2),
        "usage": usage,
        "warnings": warnings,
    }
    story = await asyncio.to_thread(assemble_story, folder, found, causes=causes,
                                    generation=generation)
    words = story["script"]["word_count"]
    await emit("progress", f"Wrote a {words}-word script", stage="script", status="done",
               progress=PROGRESS["script"])

    # 5. Save (validates against the contract first), together with the gallery entry
    await asyncio.to_thread(save_and_index, story, folder, deadline)
    await emit("progress", "Saved the story", stage="save", status="done",
               progress=PROGRESS["save"])

    # 6. Done
    await emit("done", f"{story['place']['display_name']} is ready", progress=1.0,
               story_url=f"/api/stories/{story['id']}")
    return story
