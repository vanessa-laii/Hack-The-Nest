# In-memory registry of story builds: one background job per story id (CONTRACTS §3, HL-5).
# Inputs: a resolved Place, the story id and aoi_km from POST /api/stories.
# Outputs: Job objects holding every build event, which any number of SSE subscribers follow.
# Assumes ONE uvicorn worker: the job registry and the index lock live in this process.
#
# How it works:
#   1. start() returns the job for an id if it is still busy (or found no change in the last
#      hour); otherwise it starts a new one as an asyncio task.
#   2. At most MAX_CONCURRENT_BUILDS builds run at once; a queued job says it is waiting.
#   3. The task runs build.build_story with the place already resolved, under the 180 s limit,
#      which starts counting when the build does.
#   4. Every event the build sends lands in job.events (in seq order) and wakes the subscribers.
#   5. A failure, timeout or server shutdown becomes one final `error` event with a generic
#      message and the next seq.
#   6. follow(after_seq) replays stored events after a Last-Event-ID, then waits for new ones.
#   7. Finished jobs are kept (the last 50) so a late or reconnecting browser can still replay.
#
# Worker threads: a timeout stops the build's coroutine, but a thread it was waiting on keeps
# running. TrackedExecutor notes which job started each thread, so a timed-out job stays
# "finishing" (and is reused, not rebuilt) until its last thread has returned.
#
# Memory, not disk: a job only matters while the server that runs it is up.
from __future__ import annotations

import asyncio
import importlib
import logging
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from app.config import settings
from app.errors import ApiError
from app.pipeline.types import Place

log = logging.getLogger(__name__)

KEEP_FINISHED = 50  # finished jobs kept for late subscribers
NO_CHANGES_KEEP_S = 3600  # a place with no lasting change isn't rebuilt for an hour
# Real builds running at once. Each one downloads ~250 scenes and keeps CPU threads busy;
# more in parallel would slow every build towards the 180 s limit (HL-5). Others queue.
MAX_CONCURRENT_BUILDS = 2
WAITING_MESSAGE = "Waiting for another story to finish building"

# User-facing error messages. Generic on purpose: exception text can hold paths or URLs.
MESSAGES = {
    "TIMEOUT": "This place took too long to build. Try again, or pick a story from the gallery.",
    "UPSTREAM_ERROR": "A data provider didn't respond. Try again in a minute.",
    "INTERNAL": "Something went wrong building this story.",
    "NO_LASTING_CHANGES": "We didn't find a lasting change here we can show yet.",
}
# ApiError codes from the build that the event contract can carry as they are.
PASS_THROUGH_CODES = {"PLACE_NOT_FOUND", "NO_LAND_DATA"}


def _now() -> str:
    """The current UTC time in the events' ts format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_module():
    """Imports app.pipeline.build when a job starts, not when the app starts.
    A broken build module then fails one job, not mock mode or the whole server."""
    return importlib.import_module("app.pipeline.build")


# --- Worker threads per job ---

# The running job's set of live worker threads. Each job's task sets its own (contextvars
# are per task), and asyncio.to_thread submits from inside that task, so the pool can see it.
_job_threads: ContextVar[set[Future] | None] = ContextVar("job_threads", default=None)


class TrackedExecutor(ThreadPoolExecutor):
    """The event loop's default thread pool, which also adds each thread to the set of the
    job that started it, until the thread returns."""

    def submit(self, fn, /, *args, **kwargs) -> Future:
        """Starts the thread as usual, then records it with the current job, if any."""
        future = super().submit(fn, *args, **kwargs)
        threads = _job_threads.get()
        if threads is not None:
            threads.add(future)
            future.add_done_callback(threads.discard)
        return future


def track_threads() -> None:
    """Makes the running loop's default thread pool a TrackedExecutor. Called at startup."""
    asyncio.get_running_loop().set_default_executor(TrackedExecutor())


# --- One job ---

class Job:
    """One story build: its events so far, whether it is still running, and its task.
    The Condition wakes every waiting subscriber when an event arrives or the job ends."""

    def __init__(self, story_id: str):
        self.story_id = story_id
        self.events: list[dict] = []
        self.finished = False
        self.task: asyncio.Task | None = None
        self.threads: set[Future] = set()       # this build's worker threads still running
        self.no_changes_at: float | None = None  # time.monotonic() of a NoLastingChanges end
        self._changed = asyncio.Condition()

    @property
    def running(self) -> bool:
        """True until the build has sent its last event."""
        return not self.finished

    @property
    def finishing(self) -> bool:
        """True after a timeout while one of the build's threads is still running."""
        return self.finished and bool(self.threads)

    @property
    def busy(self) -> bool:
        """True while a new build for this id must not start: it would write the same files."""
        return self.running or self.finishing

    def found_no_changes_recently(self) -> bool:
        """True if this build ended with no lasting change less than an hour ago."""
        if self.no_changes_at is None:
            return False
        return time.monotonic() - self.no_changes_at < NO_CHANGES_KEEP_S

    def ended(self) -> bool:
        """True once a final done or error event is stored."""
        return bool(self.events) and self.events[-1]["type"] in ("done", "error")

    def next_seq(self) -> int:
        """The seq for an event we add ourselves, so seq keeps going up (CONTRACTS §3)."""
        if not self.events:
            return 0
        return self.events[-1]["seq"] + 1

    async def add_event(self, event: dict) -> None:
        """Stores one event and wakes the subscribers."""
        async with self._changed:
            self.events.append(event)
            self._changed.notify_all()

    async def add_build_event(self, event: dict) -> None:
        """The build's sink. The build numbers its events from 0, but a "waiting" event may
        already be stored, so seq is renumbered to keep going up by 1 (CONTRACTS §3)."""
        await self.add_event({**event, "seq": self.next_seq()})

    async def finish(self) -> None:
        """Marks the job over and wakes subscribers so their streams can end."""
        async with self._changed:
            self.finished = True
            self._changed.notify_all()

    def _start_index(self, after_seq: int) -> int:
        """Index of the first stored event with seq > after_seq."""
        for index, event in enumerate(self.events):
            if event["seq"] > after_seq:
                return index
        return len(self.events)

    async def follow(self, after_seq: int = -1) -> AsyncIterator[dict]:
        """Yields events with seq > after_seq: stored ones first, then live ones.
        Ends when the job is over and everything has been sent."""
        async with self._changed:
            index = self._start_index(after_seq)
            caught_up = index >= len(self.events)
            if self.finished and caught_up and self.events:
                # Resumed after the last event: re-send the final done/error, like mock
                # mode, so EventSource learns the build ended instead of reconnecting forever.
                index = len(self.events) - 1

        while True:
            async with self._changed:
                await self._changed.wait_for(lambda: index < len(self.events) or self.finished)
                new_events = self.events[index:]
                over = self.finished
            for event in new_events:
                yield event
            index += len(new_events)
            if over and not new_events:
                return


# --- Running a build ---

def _error_code(exc: BaseException, build) -> str:
    """Maps a build failure to an event error code (CONTRACTS §5)."""
    if isinstance(exc, build.NoLastingChanges):
        return "NO_LASTING_CHANGES"  # sent as INTERNAL with its own message
    if isinstance(exc, ApiError):
        if exc.code in PASS_THROUGH_CODES:
            return exc.code
        return "UPSTREAM_ERROR"  # geocoder or another provider failed after retries
    if isinstance(exc, httpx.HTTPError):
        return "UPSTREAM_ERROR"
    return "INTERNAL"


def _error_message(code: str, exc: BaseException) -> str:
    """The user-facing message. A pass-through ApiError's message is already written
    for users (errors.py); everything else gets a fixed generic one."""
    if code in PASS_THROUGH_CODES and isinstance(exc, ApiError):
        return exc.message
    return MESSAGES[code]


async def _emit_error(job: Job, code: str, message: str) -> None:
    """Adds the final `error` event with the next seq. No partial story yet."""
    wire_code = "INTERNAL" if code == "NO_LASTING_CHANGES" else code
    event = {
        "type": "error",
        "story_id": job.story_id,
        "seq": job.next_seq(),
        "ts": _now(),
        "message": message[:120],  # schema: maxLength 120
        "error": {"code": wire_code, "message": message, "partial_story_url": None},
    }
    await job.add_event(event)


async def _emit_done_if_missing(job: Job) -> None:
    """Adds a `done` event if the build returned without sending one, so streams end."""
    if job.events and job.events[-1]["type"] == "done":
        return
    event = {
        "type": "done",
        "story_id": job.story_id,
        "seq": job.next_seq(),
        "ts": _now(),
        "message": "Your story is ready",
        "progress": 1,
        "story_url": f"/api/stories/{job.story_id}",
    }
    await job.add_event(event)


async def _emit_waiting(job: Job) -> None:
    """Tells the live page this build is queued behind others, so it isn't silent."""
    event = {
        "type": "progress",
        "story_id": job.story_id,
        "seq": job.next_seq(),
        "ts": _now(),
        "message": WAITING_MESSAGE,
        "stage": "geocode",
        "status": "running",
        "progress": 0,
    }
    await job.add_event(event)


_slots: asyncio.Semaphore | None = None


def _build_slots() -> asyncio.Semaphore:
    """The semaphore that caps real builds at MAX_CONCURRENT_BUILDS. Made on first use, so
    it belongs to the server's event loop, not to whichever loop imported this module."""
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(MAX_CONCURRENT_BUILDS)
    return _slots


async def _build_with_limits(job: Job, build, place: Place, aoi_km: float) -> None:
    """Waits for a build slot, then runs the build under the HL-5 time limit.
    Failures become error events; a NoLastingChanges end is remembered for start()."""
    slots = _build_slots()
    if slots.locked():
        await _emit_waiting(job)
    async with slots:
        # HL-5's 180 s counts from here, not from the POST: time spent queued is other
        # builds' work, and counting it would time out a queued build before it even ran.
        deadline = time.monotonic() + settings.pipeline_timeout_s
        try:
            async with asyncio.timeout(settings.pipeline_timeout_s):
                await build.build_story(
                    place.query,
                    aoi_km=aoi_km,
                    sink=job.add_build_event,
                    place=place,         # no second geocode: the id must match the POST's
                    deadline=deadline,   # the save thread checks this itself
                )
            await _emit_done_if_missing(job)
        except TimeoutError:
            log.warning("story %s timed out after %s s", job.story_id,
                        settings.pipeline_timeout_s)
            await _emit_error(job, "TIMEOUT", MESSAGES["TIMEOUT"])
        except Exception as exc:
            code = _error_code(exc, build)
            if code == "INTERNAL":
                log.exception("story %s failed", job.story_id)
            else:
                log.warning("story %s failed: %s (%s)", job.story_id, code, type(exc).__name__)
            if code == "NO_LASTING_CHANGES":
                job.no_changes_at = time.monotonic()
            await _emit_error(job, code, _error_message(code, exc))


async def _run(job: Job, place: Place, aoi_km: float) -> None:
    """Runs one build and makes sure it always ends with a done or error event.
    Real exceptions go to the server log only, never to the browser."""
    _job_threads.set(job.threads)  # this task's worker threads are recorded on the job
    try:
        build = _build_module()
        await _build_with_limits(job, build, place, aoi_km)
    except asyncio.CancelledError:
        # Server shutdown: a final error, so browsers stop reconnecting to a dead build.
        if not job.ended():
            await _emit_error(job, "INTERNAL", MESSAGES["INTERNAL"])
        raise
    except Exception:
        # Only reached if the build module can't be imported; build errors are handled above.
        log.exception("could not run the build module for %s", job.story_id)
        if not job.ended():
            await _emit_error(job, "INTERNAL", MESSAGES["INTERNAL"])
    finally:
        await job.finish()


# --- The registry ---

_jobs: OrderedDict[str, Job] = OrderedDict()


def get(story_id: str) -> Job | None:
    """The job for a story id (running or recently finished), or None."""
    return _jobs.get(story_id)


def is_running(story_id: str) -> bool:
    """True while a build for this id is in progress (GET story answers 425)."""
    job = _jobs.get(story_id)
    return job is not None and job.running


def _forget_old_finished() -> None:
    """Drops the oldest finished jobs beyond KEEP_FINISHED. Busy jobs are never dropped."""
    finished_ids = []
    for story_id, job in _jobs.items():
        if not job.busy:
            finished_ids.append(story_id)
    extra = len(finished_ids) - KEEP_FINISHED
    for story_id in finished_ids[:max(extra, 0)]:
        del _jobs[story_id]


def start(story_id: str, place: Place, aoi_km: float) -> Job:
    """Returns the busy job for this id, or starts a new build in the background.
    A failed job is replaced, so a retry really retries; but a timed-out job whose thread is
    still finishing, or one that found no lasting change in the last hour, is reused."""
    existing = _jobs.get(story_id)
    if existing is not None:
        if existing.busy or existing.found_no_changes_recently():
            return existing

    job = Job(story_id)
    _jobs.pop(story_id, None)
    _jobs[story_id] = job  # newest last, so the oldest are dropped first
    job.task = asyncio.create_task(_run(job, place, aoi_km))
    _forget_old_finished()
    return job


def clear() -> None:
    """Forgets every job and the build slots. For tests, which each run their own loop."""
    global _slots
    _jobs.clear()
    _slots = None
