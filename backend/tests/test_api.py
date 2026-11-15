# Tests for the HTTP API in real mode (jobs + SSE) and mock mode (CONTRACTS §1, §3, §5; HL-5).
# Inputs: a fake geocoder and a fake build module that emits a few events and saves a sample story.
# Outputs: pytest results. No network, no imagery.
#
# How it works:
#   1. The real_api fixture turns mock mode off, points DATA_DIR at a temp folder and swaps in
#      the fakes. TestClient is used as a context manager so background jobs share one loop.
#   2. The fake build waits on a threading gate, so a test can look at a job mid-build.
#   3. SSE responses are split into events and checked against the event schema.
#   4. Job-level tests (build slots, shutdown) drive jobs.start in their own event loop.
#   5. build.save_and_index and update_index are tested directly for the save rules (HL-5).
import asyncio
import copy
import json
import sys
import threading
import time
import types

import pytest
from fastapi.testclient import TestClient

from app import jobs, main, mock, store
from app.config import settings
from app.contract_check import event_errors
from app.errors import ApiError
from app.pipeline import build as real_build
from app.pipeline import geocode
from app.pipeline.types import Place

PLACE = Place(query="Testville", display_name="Testville, Nevada", lat=36.1, lon=-114.7,
              admin={"city": None, "county": None, "state": "Nevada",
                     "country": "United States", "country_code": "us"})
CANDIDATES = [
    {"display_name": "Paris, France", "lat": 48.8566, "lon": 2.3522, "kind": "city"},
    {"display_name": "Paris, Texas, United States", "lat": 33.66, "lon": -95.56, "kind": "city"},
]


def _story_id() -> str:
    """The id main.py computes for PLACE at the default area size."""
    return store.story_id(PLACE.display_name, "us", settings.aoi_km)


async def fake_resolve(query, lat=None, lon=None, display_name=None):
    """Stands in for geocode.resolve: 'Paris' is ambiguous, anything else is Testville."""
    if query == "Paris":
        raise ApiError("PLACE_AMBIGUOUS", 'Several places match "Paris".', candidates=CANDIDATES)
    return PLACE


def _sample_story(sid: str) -> dict:
    """A copy of the valid sample story under another id."""
    story = copy.deepcopy(mock.sample_story())
    story["id"] = sid
    return story


class FakeBuild:
    """A stand-in for app.pipeline.build: two progress events, a wait, then save and done.
    mode 'gate' waits for the test to open the gate; 'hang' sleeps past any timeout;
    'sleep' waits sleep_s; 'slow_save' saves with the real save_and_index in a slow thread."""

    def __init__(self):
        self.gate = threading.Event()
        self.mode = "gate"
        self.sleep_s = 0.0
        self.calls = 0
        self.places = []
        self.module = types.ModuleType("app.pipeline.build")
        self.module.NoLastingChanges = type("NoLastingChanges", (Exception,), {})
        self.module.build_story = self.build_story

    async def build_story(self, query, *, aoi_km=None, sink=None, place=None, deadline=None):
        """Emits seq 0 and 1, waits, saves a tiny valid story, then emits done."""
        self.calls += 1
        self.places.append(place)
        sid = store.story_id(place.display_name, "us", aoi_km)
        await sink(_event(sid, 0, "progress", stage="geocode", status="done"))
        await sink(_event(sid, 1, "progress", stage="scenes", status="done"))
        story = _sample_story(sid)
        if self.mode == "hang":
            await asyncio.sleep(30)
        elif self.mode == "sleep":
            await asyncio.sleep(self.sleep_s)
        elif self.mode == "slow_save":
            await asyncio.to_thread(self.slow_save, story, deadline)
        else:
            await asyncio.to_thread(self.gate.wait, 5)

        store.save_story(story)
        await sink(_event(sid, 2, "done", story_url=f"/api/stories/{sid}"))
        return story

    def slow_save(self, story: dict, deadline: float) -> None:
        """A save thread still busy when the timeout fires, as a slow disk would be."""
        time.sleep(self.sleep_s)
        folder = store.stories_dir() / story["id"]
        real_build.save_and_index(story, folder, deadline)


def _event(sid: str, seq: int, type_: str, **extra) -> dict:
    """One build event in the contract's shape."""
    return {"type": type_, "story_id": sid, "seq": seq, "ts": "2026-11-15T00:00:00Z",
            "message": f"step {seq}", **extra}


def _parse_sse(text: str) -> list[dict]:
    """Splits an SSE body into event dicts, skipping ': ping' comments."""
    events = []
    for block in text.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def _stream(client: TestClient, sid: str, last_event_id: str | None = None) -> list[dict]:
    """Reads one SSE stream to its end (done or error) and checks every event's shape."""
    headers = {}
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    response = client.get(f"/api/stories/{sid}/events", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    for event in events:
        assert event_errors(event) == []
    return events


@pytest.fixture
def fake_build(monkeypatch, tmp_path):
    """Real mode with a temp DATA_DIR, the fake geocoder and the fake build module."""
    fake = FakeBuild()
    monkeypatch.setattr(settings, "em_mock", False)
    monkeypatch.setattr(settings, "data_dir_raw", str(tmp_path))
    monkeypatch.setattr(geocode, "resolve", fake_resolve)
    monkeypatch.setitem(sys.modules, "app.pipeline.build", fake.module)
    jobs.clear()
    yield fake
    fake.gate.set()  # never leave a fake build waiting
    jobs.clear()


@pytest.fixture
def real_api(fake_build):
    """The fake build behind a TestClient (its lifespan installs the thread tracking)."""
    with TestClient(main.app) as client:
        yield client, fake_build
        fake_build.gate.set()


# --- Real mode ---

def test_post_streams_to_done_then_story_is_served(real_api):
    """202 → events to done (seq 0, 1, 2) → GET story 200."""
    client, fake = real_api
    response = client.post("/api/stories", json={"query": "Testville"})
    assert response.status_code == 202
    body = response.json()
    sid = _story_id()
    assert body == {"id": sid, "cached": False, "events_url": f"/api/stories/{sid}/events",
                    "story_url": f"/api/stories/{sid}"}

    fake.gate.set()
    events = _stream(client, sid)
    assert [e["seq"] for e in events] == [0, 1, 2]
    assert events[-1]["type"] == "done"

    story = client.get(f"/api/stories/{sid}")
    assert story.status_code == 200
    assert story.json()["id"] == sid


def test_second_post_while_running_reuses_job_and_story_is_not_ready(real_api):
    """Same id while building: same job, one build call, and GET story says 425 NOT_READY."""
    client, fake = real_api
    first = client.post("/api/stories", json={"query": "Testville"})
    job = jobs.get(first.json()["id"])
    second = client.post("/api/stories", json={"query": "Testville"})

    assert second.status_code == 202
    assert jobs.get(second.json()["id"]) is job
    not_ready = client.get(f"/api/stories/{first.json()['id']}")
    assert not_ready.status_code == 425
    assert not_ready.json()["error"]["code"] == "NOT_READY"

    fake.gate.set()
    _stream(client, first.json()["id"])
    assert fake.calls == 1


def test_subscriber_follows_live_events(real_api):
    """A stream opened mid-build gets the stored events, then the live ones up to done."""
    client, fake = real_api
    sid = client.post("/api/stories", json={"query": "Testville"}).json()["id"]
    threading.Timer(0.3, fake.gate.set).start()  # the build finishes while we listen
    events = _stream(client, sid)
    assert [e["type"] for e in events] == ["progress", "progress", "done"]


def test_last_event_id_replays_only_later_events(real_api):
    """Resuming after seq 0 sends 1 and 2; resuming after the end re-sends the final event."""
    client, fake = real_api
    sid = client.post("/api/stories", json={"query": "Testville"}).json()["id"]
    fake.gate.set()
    _stream(client, sid)

    assert [e["seq"] for e in _stream(client, sid, last_event_id="0")] == [1, 2]
    assert [e["seq"] for e in _stream(client, sid, last_event_id="2")] == [2]


def test_cached_story_returns_200(real_api):
    """A story already on disk is returned straight away, without starting a build."""
    client, fake = real_api
    story = copy.deepcopy(mock.sample_story())
    story["id"] = _story_id()
    store.save_story(story)

    response = client.post("/api/stories", json={"query": "Testville"})
    assert response.status_code == 200
    assert response.json() == {"id": story["id"], "cached": True,
                               "story_url": f"/api/stories/{story['id']}"}
    assert jobs.get(story["id"]) is None


def test_ambiguous_place_is_409_with_candidates(real_api):
    """Geocoder errors pass through as HTTP errors in the envelope (FR-2)."""
    client, _ = real_api
    response = client.post("/api/stories", json={"query": "Paris"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PLACE_AMBIGUOUS"
    assert error["candidates"] == CANDIDATES


def test_timeout_sends_error_event_with_next_seq(real_api, monkeypatch):
    """Over the time limit (HL-5): one error event TIMEOUT, seq right after the last one."""
    client, fake = real_api
    fake.mode = "hang"
    monkeypatch.setattr(settings, "pipeline_timeout_s", 0.3)
    sid = client.post("/api/stories", json={"query": "Testville"}).json()["id"]

    events = _stream(client, sid)
    last = events[-1]
    assert [e["seq"] for e in events] == [0, 1, 2]
    assert last["type"] == "error"
    assert last["error"]["code"] == "TIMEOUT"
    assert last["error"]["partial_story_url"] is None
    assert client.get(f"/api/stories/{sid}").status_code == 404  # finished, nothing saved


def test_no_lasting_changes_is_internal_with_plain_message(real_api):
    """NoLastingChanges from the build becomes INTERNAL with the agreed message."""
    client, fake = real_api

    async def no_changes(query, *, sink=None, **kwargs):
        raise fake.module.NoLastingChanges("internal detail /secret/path")

    fake.module.build_story = no_changes
    sid = client.post("/api/stories", json={"query": "Testville"}).json()["id"]
    events = _stream(client, sid)
    assert len(events) == 1
    assert events[0]["seq"] == 0
    assert events[0]["error"]["code"] == "INTERNAL"
    assert events[0]["error"]["message"] == "We didn't find a lasting change here we can show yet."


def test_no_change_place_is_not_rebuilt_within_the_hour(real_api, monkeypatch):
    """A second POST for a no-change place gets 202 and the old job's error, no new build.
    After NO_CHANGES_KEEP_S it really retries."""
    client, fake = real_api
    calls = []

    async def no_changes(query, *, sink=None, **kwargs):
        calls.append(query)
        raise fake.module.NoLastingChanges("nothing")

    fake.module.build_story = no_changes
    sid = client.post("/api/stories", json={"query": "Testville"}).json()["id"]
    first_job = jobs.get(sid)
    _stream(client, sid)

    again = client.post("/api/stories", json={"query": "Testville"})
    assert again.status_code == 202
    assert again.json()["events_url"] == f"/api/stories/{sid}/events"
    assert jobs.get(sid) is first_job
    replay = _stream(client, sid)
    assert replay[-1]["error"]["message"] == jobs.MESSAGES["NO_LASTING_CHANGES"]
    assert len(calls) == 1

    monkeypatch.setattr(jobs, "NO_CHANGES_KEEP_S", 0)
    client.post("/api/stories", json={"query": "Testville"})
    _stream(client, sid)
    assert len(calls) == 2


def test_build_gets_the_resolved_place(real_api):
    """The job passes the Place main.py resolved, so the build doesn't geocode again (4a)."""
    client, fake = real_api
    sid = client.post("/api/stories", json={"query": "Testville"}).json()["id"]
    fake.gate.set()
    _stream(client, sid)
    assert fake.places == [PLACE]


def test_slow_save_after_timeout_never_appears(real_api, monkeypatch):
    """A save thread still running when HL-5's timeout fires finds the deadline passed and
    writes nothing. Until that thread returns the job is 'finishing' and a new POST reuses it."""
    client, fake = real_api
    fake.mode = "slow_save"
    fake.sleep_s = 0.8
    monkeypatch.setattr(settings, "pipeline_timeout_s", 0.3)
    sid = client.post("/api/stories", json={"query": "Testville"}).json()["id"]
    job = jobs.get(sid)

    events = _stream(client, sid)
    assert events[-1]["error"]["code"] == "TIMEOUT"
    assert job.finishing
    assert client.post("/api/stories", json={"query": "Testville"}).status_code == 202
    assert jobs.get(sid) is job
    assert fake.calls == 1

    for _ in range(50):  # the thread returns about 0.5 s after the timeout
        if not job.finishing:
            break
        time.sleep(0.05)
    assert not job.finishing
    assert store.load_story(sid) is None
    assert store.gallery() == []


def test_unknown_story_events_and_story_are_404(real_api):
    """No job and no file: both routes answer 404 NOT_FOUND."""
    client, _ = real_api
    assert client.get("/api/stories/nowhere-us-6km/events").status_code == 404
    assert client.get("/api/stories/nowhere-us-6km").status_code == 404


# --- Job slots and shutdown (no HTTP) ---

def _place(name: str) -> Place:
    """A Testville-like place with another name, so it gets its own story id."""
    return Place(query=name, display_name=name, lat=PLACE.lat, lon=PLACE.lon,
                 admin=PLACE.admin)


def _start(name: str) -> jobs.Job:
    """Starts a job for a named place at 6 km."""
    return jobs.start(store.story_id(name, "us", 6.0), _place(name), 6.0)


def test_third_build_waits_for_a_slot(fake_build):
    """With MAX_CONCURRENT_BUILDS = 2, a third job emits one 'waiting' event, starts when a
    slot frees up, and its seq still goes up by exactly 1."""
    assert jobs.MAX_CONCURRENT_BUILDS == 2

    async def scenario():
        running = [_start("Alpha"), _start("Beta")]
        queued = _start("Gamma")
        await asyncio.sleep(0.2)
        waiting_events = list(queued.events)
        calls_while_full = fake_build.calls
        fake_build.gate.set()
        await asyncio.gather(*(job.task for job in running + [queued]))
        return waiting_events, calls_while_full, queued

    waiting_events, calls_while_full, queued = asyncio.run(scenario())
    assert calls_while_full == 2
    assert len(waiting_events) == 1
    waiting = waiting_events[0]
    assert waiting["message"] == jobs.WAITING_MESSAGE
    assert (waiting["stage"], waiting["status"]) == ("geocode", "running")
    assert [e["seq"] for e in queued.events] == [0, 1, 2, 3]
    assert queued.events[-1]["type"] == "done"
    for event in queued.events:
        assert event_errors(event) == []


def test_time_limit_counts_from_build_start_not_queue(fake_build, monkeypatch):
    """A job queued longer than the HL-5 limit still gets its full time once it starts."""
    monkeypatch.setattr(jobs, "MAX_CONCURRENT_BUILDS", 1)
    monkeypatch.setattr(settings, "pipeline_timeout_s", 0.6)
    fake_build.mode = "sleep"
    fake_build.sleep_s = 0.4

    async def scenario():
        first, second = _start("Alpha"), _start("Beta")
        await asyncio.gather(first.task, second.task)
        return first, second

    first, second = asyncio.run(scenario())  # second: 0.4 s queued + 0.4 s build > 0.6 s
    assert first.events[-1]["type"] == "done"
    assert second.events[-1]["type"] == "done"


def test_cancelled_job_ends_with_internal_error(fake_build):
    """Server shutdown cancels the task: the job still ends with a final INTERNAL error event,
    so a browser stops reconnecting."""
    fake_build.mode = "hang"

    async def scenario():
        job = _start("Alpha")
        await asyncio.sleep(0.1)
        job.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job.task
        return job

    job = asyncio.run(scenario())
    last = job.events[-1]
    assert job.finished
    assert last["type"] == "error"
    assert last["error"]["code"] == "INTERNAL"
    assert last["error"]["message"] == jobs.MESSAGES["INTERNAL"]
    assert [e["seq"] for e in job.events] == [0, 1, 2]
    assert event_errors(last) == []


# --- Saving and the gallery index (build.py) ---

def test_save_after_deadline_writes_nothing(monkeypatch, tmp_path):
    """Past the deadline neither story.json nor the index entry is written (HL-5)."""
    monkeypatch.setattr(settings, "data_dir_raw", str(tmp_path))
    story = _sample_story("late-us-6km")
    folder = store.stories_dir() / story["id"]
    with pytest.raises(real_build.DeadlinePassed):
        real_build.save_and_index(story, folder, deadline=time.monotonic() - 1)
    assert store.load_story(story["id"]) is None
    assert store.gallery() == []

    real_build.save_and_index(story, folder, deadline=time.monotonic() + 60)
    assert store.load_story(story["id"]) is not None
    assert [item["id"] for item in store.gallery()] == [story["id"]]


def test_failed_index_update_removes_the_saved_story(monkeypatch, tmp_path):
    """story.json and its gallery entry succeed or fail together."""
    monkeypatch.setattr(settings, "data_dir_raw", str(tmp_path))
    story = _sample_story("broken-us-6km")

    def broken_index(*args):
        raise OSError("disk full")

    monkeypatch.setattr(real_build, "update_index", broken_index)
    with pytest.raises(OSError):
        real_build.save_and_index(story, store.stories_dir() / story["id"])
    assert store.load_story(story["id"]) is None


def test_concurrent_index_updates_keep_every_entry(monkeypatch, tmp_path):
    """Builds finishing at the same moment in different threads don't lose each other's
    gallery entry: the read-modify-write of index.json holds a lock."""
    monkeypatch.setattr(settings, "data_dir_raw", str(tmp_path))
    stories = [_sample_story(f"place-{i}-us-6km") for i in range(12)]
    barrier = threading.Barrier(len(stories))

    def add(story):
        barrier.wait()  # all threads start the update together
        real_build.update_index(story, store.stories_dir() / story["id"])

    threads = [threading.Thread(target=add, args=(story,)) for story in stories]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ids = {item["id"] for item in store.gallery()}
    assert ids == {story["id"] for story in stories}


# --- Mock mode ---

def test_mock_mode_unchanged(monkeypatch, tmp_path):
    """EM_MOCK=1: health says mock, POST is 202 for the sample, the sample story and its
    event replay are served."""
    monkeypatch.setattr(settings, "em_mock", True)
    monkeypatch.setattr(settings, "data_dir_raw", str(tmp_path))
    monkeypatch.setattr(mock, "EVENT_DELAY_S", 0)
    sample_id = mock.sample_story()["id"]

    with TestClient(main.app) as client:
        assert client.get("/api/health").json()["mock"] is True

        response = client.post("/api/stories", json={"query": "anything"})
        assert response.status_code == 202
        assert response.json()["id"] == sample_id
        assert response.json()["cached"] is False

        assert client.get(f"/api/stories/{sample_id}").json()["id"] == sample_id
        events = _stream(client, sample_id)
        assert events[-1]["type"] == "done"
