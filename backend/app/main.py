# FastAPI app implementing the HTTP API in docs/CONTRACTS.md §1 (mock mode + cached stories for now).
# Inputs: HTTP requests from web/ (CORS limited to WEB_ORIGIN); settings; stories on disk.
# Outputs: JSON responses, SSE build-event streams, static files from DATA_DIR at /files/.
#
# How it works:
#   1. Create the app: CORS for the web app, the error envelope, /files/ from DATA_DIR.
#   2. POST /api/stories starts a build (mock mode: points at the sample story).
#   3. GET /api/stories and /api/stories/{id} serve the gallery and saved stories.
#   4. GET /api/stories/{id}/events streams build events as SSE, with keep-alive pings
#      and resume from Last-Event-ID.
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from app import errors, mock, store
from app.config import settings
from app.errors import ApiError

VERSION = "1.0"
# Send an SSE ping after this many quiet seconds. Proxies and browsers drop idle
# connections, and a real build can be quiet during research (CONTRACTS §3).
PING_EVERY_S = 10


# --- App setup ---

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup: make sure DATA_DIR exists and, in mock mode, the placeholder images,
    so the first page load doesn't get 404s for frames."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if settings.em_mock:
        mock.ensure_placeholders()
    yield


app = FastAPI(title="Earth Memory backend", version=VERSION, lifespan=lifespan)

# Only the web app's origin may call us from a browser (CONTRACTS §1).
# Last-Event-ID is allowed so a browser can resume an SSE stream.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Last-Event-ID"],
)
errors.install(app)

# /files/ serves frames, thumbs, masks and narration audio from DATA_DIR.
# The folder is created here too because StaticFiles checks it exists on import.
settings.data_dir.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=settings.data_dir), name="files")


# --- Story routes ---

class StoryRequest(BaseModel):
    """Body of POST /api/stories: a query, optionally with the lat/lon/display_name
    picked after a PLACE_AMBIGUOUS response (FR-2). aoi_km 3–10 is HL-6."""

    query: str = Field(min_length=1, max_length=200)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    display_name: str | None = Field(None, max_length=200)
    aoi_km: float | None = Field(None, ge=3, le=10)

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, v: str) -> str:
        """Strips whitespace and rejects a blank query.
        '   ' passes min_length but would send an empty search to the geocoder."""
        v = v.strip()
        if not v:
            raise ValueError("query is blank")
        return v

    @model_validator(mode="after")
    def _picked_place_complete(self) -> "StoryRequest":
        """lat, lon and display_name come together or not at all.
        Half a pick (lat without lon) can't be geocoded or labelled."""
        given = [self.lat is not None, self.lon is not None, self.display_name is not None]
        if any(given) and not all(given):
            raise ValueError("lat, lon and display_name must be sent together")
        return self


@app.get("/api/health")
async def health():
    """Liveness check. Reports mock mode so the web app can show a 'sample data'
    badge and nobody demos mock data by mistake."""
    return {"ok": True, "mock": settings.em_mock, "version": VERSION}


@app.post("/api/stories")
async def create_story(req: StoryRequest):
    """Starts building a story. Mock mode: 202 with the sample story's id.
    Real mode: the pipeline isn't wired in yet, so return the standard error and
    the web app falls back to the gallery."""
    if settings.em_mock:
        sid = mock.sample_story()["id"]
        body = {
            "id": sid,
            "cached": False,
            "events_url": f"/api/stories/{sid}/events",
            "story_url": f"/api/stories/{sid}",
        }
        return JSONResponse(body, status_code=202)
    raise ApiError(
        "INTERNAL",
        "Live story generation isn't available yet. Pick a story from the gallery.",
    )


@app.get("/api/stories")
async def list_stories():
    """Gallery list, newest first (CONTRACTS §1). Mock mode returns just the sample;
    real mode reads stories/index.json."""
    if settings.em_mock:
        return {"stories": [mock.gallery_item(mock.sample_story())]}
    return {"stories": store.gallery()}


@app.get("/api/stories/{sid}")
async def get_story(sid: str):
    """Returns a full story.json. Saved stories are served in mock mode too,
    so pre-built stories can be tested alongside the sample."""
    if settings.em_mock and sid == mock.sample_story()["id"]:
        return mock.sample_story()
    story = store.load_story(sid)
    if story is None:
        raise ApiError("NOT_FOUND", f"No story with id {sid!r}.")
    return story


# --- Build events (SSE) ---

def _sse(ev: dict) -> str:
    """Formats one event as SSE (CONTRACTS §3). id = seq, so the browser sends it
    back as Last-Event-ID; event = type; compact JSON on one data: line."""
    data = json.dumps(ev, separators=(",", ":"))
    return f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {data}\n\n"


async def _close_source(it: AsyncIterator[dict]) -> None:
    """Closes the event source so its own cleanup runs (queues, subscriptions)."""
    aclose = getattr(it, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except RuntimeError:
        # Still running: the cancelled __anext__ owns it, and cancellation cleans it up.
        pass


async def _with_pings(source: AsyncIterator[dict]) -> AsyncIterator[str]:
    """Turns events into SSE text, sending ': ping' after 10 quiet seconds.
    Ends after a done or error event. Uses asyncio.wait, not wait_for, because
    wait_for would cancel the pending read and kill the generator on timeout."""
    it = source.__aiter__()
    pending = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=PING_EVERY_S)
            if not done:
                yield ": ping\n\n"
                continue
            try:
                ev = pending.result()
            except StopAsyncIteration:
                return
            yield _sse(ev)
            if ev["type"] in ("done", "error"):
                return
            pending = asyncio.ensure_future(it.__anext__())
    finally:
        # Client gone or stream finished: stop the in-flight read, then close the source.
        pending.cancel()
        await _close_source(it)


def _resume_after(last_event_id: str | None) -> int:
    """The seq to resume after; -1 (from the start) if the header is missing or not a number."""
    if last_event_id is None:
        return -1
    try:
        return int(last_event_id)
    except ValueError:
        return -1


@app.get("/api/stories/{sid}/events")
async def story_events(sid: str, last_event_id: str | None = Header(None)):
    """SSE stream of build events for one story. Mock mode replays the sample stream,
    resuming after Last-Event-ID (sent by EventSource on reconnect)."""
    if not (settings.em_mock and sid == mock.sample_story()["id"]):
        raise ApiError("NOT_FOUND", f"No build in progress for {sid!r}.")
    after = _resume_after(last_event_id)
    return StreamingResponse(
        _with_pings(mock.replay_events(after)),
        media_type="text/event-stream",
        # Stop caches and proxies holding events back (the progress page would freeze).
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
