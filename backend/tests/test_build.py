# Tests for build.build_story's cause step: find_causes is called, its result reaches the story.
# Inputs: fakes for run_imagery, detect_changes, find_causes and assemble_story; a temp DATA_DIR.
# Outputs: pytest results. No network, no Exa, no model calls.
#
# How it works:
#   1. The imagery half, change detection and story assembly are faked; the sample story is
#      what gets "assembled", so the real save (with contract validation) still runs.
#   2. find_causes is faked to spend some budget and emit research/verify events, so the test
#      can check usage, warnings, events and the HL-5 skip without any outside call.
import asyncio
import copy
import time
from types import SimpleNamespace

from app import mock, store
from app.config import settings
from app.contract_check import event_errors
from app.pipeline import build

STORY_ID = "sample-lake-6km"


class Fakes:
    """Records what build_story passed to find_causes and assemble_story."""

    def __init__(self):
        self.causes_calls = []
        self.assembled_with = None
        self.causes_error = None

    async def run_imagery(self, query, *, lat=None, lon=None, display_name=None, aoi_km=None,
                          sink=None, place=None):
        sample = mock.sample_story()
        return SimpleNamespace(story_id=STORY_ID, sections={"place": sample["place"]})

    def detect_changes(self, folder):
        return copy.deepcopy(mock.sample_story()["changes"])

    async def find_causes(self, story_dir, place, changes, *, trace, budget, emit=None):
        self.causes_calls.append({"place": place, "changes": changes})
        if self.causes_error is not None:
            raise self.causes_error
        budget.spend("exa")
        budget.spend("llm")
        budget.spend("llm")
        await emit("progress", "Change 1999–2010: searching for documented causes",
                   stage="research", status="running")
        await emit("progress", "Change 1999–2010: 2 of 3 quotes matched their pages",
                   stage="verify", status="running")
        return {"sources": [], "chapters": {}, "usage": {"exa_calls": 1, "llm_calls": 2},
                "warnings": ["chg-02: cause left unknown because the web search failed"]}

    def assemble_story(self, folder, changes, *, causes=None, status="complete",
                       generation=None):
        self.assembled_with = {"causes": causes, "generation": generation}
        story = copy.deepcopy(mock.sample_story())
        story["generation"] = generation
        return story


def _build(monkeypatch, tmp_path, fakes: Fakes, deadline=None) -> tuple[dict, list[dict]]:
    """Runs build_story with the fakes; returns (saved story, events)."""
    monkeypatch.setattr(settings, "data_dir_raw", str(tmp_path))
    monkeypatch.setattr(build, "run_imagery", fakes.run_imagery)
    monkeypatch.setattr(build.change_detection, "detect_changes", fakes.detect_changes)
    monkeypatch.setattr(build, "find_causes", fakes.find_causes)
    monkeypatch.setattr(build, "assemble_story", fakes.assemble_story)
    events = []

    async def sink(event):
        events.append(event)

    story = asyncio.run(build.build_story("Sample Lake", sink=sink, deadline=deadline))
    return story, events


def test_causes_reach_the_story_with_usage_and_warnings(monkeypatch, tmp_path):
    """find_causes gets the imagery place and the changes; its result goes to assemble_story,
    and the budget's counts and the agent's warnings fill story.generation."""
    fakes = Fakes()
    story, events = _build(monkeypatch, tmp_path, fakes)

    assert len(fakes.causes_calls) == 1
    assert fakes.causes_calls[0]["place"]["display_name"] == story["place"]["display_name"]
    assert fakes.assembled_with["causes"]["warnings"] == [
        "chg-02: cause left unknown because the web search failed"]
    generation = story["generation"]
    assert generation["usage"] == {"exa_calls": 1, "llm_calls": 2, "vision_calls": 0}
    assert generation["warnings"] == [
        "chg-02: cause left unknown because the web search failed"]
    assert store.load_story(STORY_ID) is not None

    stages = [event.get("stage") for event in events]
    assert "research" in stages and "verify" in stages
    assert stages.index("research") < stages.index("script")
    assert [event["seq"] for event in events] == list(range(len(events)))
    for event in events:
        assert event_errors(event) == []
        assert len(event["message"]) <= 120


def test_too_little_time_skips_research(monkeypatch, tmp_path):
    """HL-5: near the deadline the cause search is skipped and the story is still saved."""
    fakes = Fakes()
    deadline = time.monotonic() + build.AFTER_RESEARCH_S + build.RESEARCH_MIN_S - 5
    story, events = _build(monkeypatch, tmp_path, fakes, deadline=deadline)

    assert fakes.causes_calls == []
    assert fakes.assembled_with["causes"] is None
    assert story["generation"]["usage"] == {"exa_calls": 0, "llm_calls": 0, "vision_calls": 0}
    assert any("HL-5" in warning for warning in story["generation"]["warnings"])
    warning_events = [event for event in events if event.get("status") == "warning"]
    assert warning_events and warning_events[0]["stage"] == "research"
    for event in events:
        assert event_errors(event) == []


def test_unexpected_agent_error_never_fails_the_build(monkeypatch, tmp_path):
    """A bug in the agent leaves every change cause_unknown, with a warning, and still saves."""
    fakes = Fakes()
    fakes.causes_error = KeyError("boom")
    story, events = _build(monkeypatch, tmp_path, fakes)

    assert fakes.assembled_with["causes"] is None
    assert story["generation"]["warnings"] == ["Cause search failed; causes left unknown"]
    assert events[-1]["type"] == "done"


def test_agent_error_in_the_trace_is_scrubbed(monkeypatch, tmp_path):
    """The broad except writes llm.describe_error(error) to the trace, so a key in an
    exception message never reaches trace.jsonl (CLAUDE.md #3)."""
    fakes = Fakes()
    fakes.causes_error = RuntimeError("bad request with key sk-abcdefghijklmnop1234")
    story, _ = _build(monkeypatch, tmp_path, fakes)
    trace_text = ""
    for path in tmp_path.rglob("trace.jsonl"):
        trace_text += path.read_text(encoding="utf-8")
    assert "cause search failed" in trace_text
    assert "sk-abcdefghijklmnop1234" not in trace_text
    assert "RuntimeError" in trace_text
