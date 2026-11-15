# FastAPI app implementing the HTTP API in docs/CONTRACTS.md §1 (mock mode + cached stories for now).
# Inputs: HTTP requests from web/ (CORS limited to WEB_ORIGIN); settings; stories on disk.
# Outputs: JSON responses, SSE build-event streams, static files from DATA_DIR at /files/.
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
# Gap after which an SSE comment is sent if no event has gone out. Why: proxies
# and browsers drop idle connections, and a real build can be quiet for a while
# during research (CONTRACTS §3).
PING_EVERY_S = 10


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup work: make sure DATA_DIR exists and, in mock mode, that the
    placeholder images exist. Why at startup: the first page load would
    otherwise get 404s for frames."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if settings.em_mock:
        mock.ensure_placeholders()
    yield


app = FastAPI(title="Earth Memory backend", version=VERSION, lifespan=lifespan)

# CORS: only the web app's origin may call us from a browser (CONTRACTS §1).
# Last-Event-ID is allowed so a browser can resume an SSE stream.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Last-Event-ID"],
)
errors.install(app)

# /files/ serves frames, thumbs, masks and narration audio straight from DATA_DIR.
# The folder is created here, not just in lifespan, because StaticFiles checks
# that the directory exists when it's mounted, which happens at import time.
settings.data_dir.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=settings.data_dir), name="files")


class StoryRequest(BaseModel):
    """Body of POST /api/stories. Either a bare query, or a query plus the
    lat/lon/display_name the user picked after a PLACE_AMBIGUOUS response (FR-2).
    The bounds reject garbage early. aoi_km 3–10 is HL-6."""

    query: str = Field(min_length=1, max_length=200)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    display_name: str | None = Field(None, max_length=200)
    aoi_km: float | None = Field(None, ge=3, le=10)

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, v: str) -> str:
        """Strips whitespace and rejects a blank query. Why: '   ' passes
        min_length but would send an empty search to the geocoder."""
        v = v.strip()
        if not v:
            raise ValueError("query is blank")
        return v

    @model_validator(mode="after")
    def _picked_place_complete(self) -> "StoryRequest":
        """lat, lon and display_name come together or not at all. Why: they are the
        user's pick from the PLACE_AMBIGUOUS picker (FR-2), and half a pick (lat
        without lon) can't be geocoded or labelled."""
        given = [self.lat is not None, self.lon is not None, self.display_name is not None]
        if any(given) and not all(given):
            raise ValueError("lat, lon and display_name must be sent together")
        return self


@app.get("/api/health")
async def health():
    """Liveness check. Also reports mock mode, so the web app can show a
    'sample data' badge and nobody demos mock data by mistake."""
    return {"ok": True, "mock": settings.em_mock, "version": VERSION}


@app.post("/api/stories")
async def create_story(req: StoryRequest):
    """Starts building a story, or points to a cached one.

    Mock mode: any query gets 202 and the sample story's id, so the web app can
    exercise the full new-story flow (POST → events → story).
    Real mode: the pipeline isn't built yet (Phase 1). Until then this returns
    the standard error so the web app's fallback ("pick from the gallery") is
    what users see.
    """
    if settings.em_mock:
        sid = mock.sample_story()["id"]
        return JSONResponse(
            {"id": sid, "cached": False, "events_url": f"/api/stories/{sid}/events", "story_url": f"/api/stories/{sid}"},
            status_code=202,
        )
    raise ApiError("INTERNAL", "Live story generation isn't available yet. Pick a story from the gallery.")


@app.get("/api/stories")
async def list_stories():
    """Gallery list, newest first (CONTRACTS §1). Mock mode returns just the
    sample. Real mode reads stories/index.json."""
    if settings.em_mock:
        return {"stories": [mock.gallery_item(mock.sample_story())]}
    return {"stories": store.gallery()}


@app.get("/api/stories/{sid}")
async def get_story(sid: str):
    """Returns a full story.json. Real stories on disk are served in mock mode
    too, so pre-built stories can be tested alongside the sample."""
    if settings.em_mock and sid == mock.sample_story()["id"]:
        return mock.sample_story()
    story = store.load_story(sid)
    if story is None:
        raise ApiError("NOT_FOUND", f"No story with id {sid!r}.")
    return story


def _sse(ev: dict) -> str:
    """Formats one event in SSE wire format (CONTRACTS §3): `id` = seq so the
    browser sends it back as Last-Event-ID on reconnect, `event` = type so the
    client can listen per type, and compact JSON on a single `data:` line."""
    return f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev, separators=(',', ':'))}\n\n"


async def _with_pings(source: AsyncIterator[dict]) -> AsyncIterator[str]:
    """Turns an event iterator into SSE text, with keep-alive pings.

    What: waits for the next event for up to 10 s. If none arrives, sends an
    SSE comment (': ping') and keeps waiting. The stream ends after a `done` or
    `error` event (CONTRACTS §3).
    Why the separate task with asyncio.wait rather than wait_for: wait_for
    cancels the pending __anext__ on timeout, which would kill the underlying
    generator. asyncio.wait just stops waiting and leaves the task running.
    The finally block cancels it if the client disconnects.
    """
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
        # Client gone or stream finished: stop the in-flight read, then close the
        # source generator so its own cleanup runs. Why aclose: the real pipeline's
        # event source will hold resources (queues, subscriptions) that must be
        # released, not left for garbage collection.
        pending.cancel()
        aclose = getattr(it, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except RuntimeError:
                pass  # generator still running: the cancelled __anext__ owns it, and cancellation cleans it up


@app.get("/api/stories/{sid}/events")
async def story_events(sid: str, last_event_id: str | None = Header(None)):
    """SSE stream of build events for one story.

    Mock mode replays the sample stream. Last-Event-ID (sent automatically by
    the browser's EventSource on reconnect) resumes after that seq. A
    non-numeric header falls back to the beginning rather than failing.
    The real pipeline will plug its own event source in here in Phase 1.
    """
    if not (settings.em_mock and sid == mock.sample_story()["id"]):
        raise ApiError("NOT_FOUND", f"No build in progress for {sid!r}.")
    try:
        after = int(last_event_id) if last_event_id is not None else -1
    except ValueError:
        after = -1
    return StreamingResponse(
        _with_pings(mock.replay_events(after)),
        media_type="text/event-stream",
        # no-cache + X-Accel-Buffering: stop caches and proxies from holding
        # events back, which would make the progress page look frozen.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
