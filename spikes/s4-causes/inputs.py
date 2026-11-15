# Spike S4 shared inputs: per gold place, its base story and the recorded Exa results per change.
# Inputs: spikes/s3-changes/places.json, data/stories/<id>/, the Exa disk cache (cached only).
# Outputs: load_place(story_id) -> PlaceInput; gold_places() -> the list of places with changes.
#
# How it works:
#   1. Re-plan the same queries record.py recorded (same template, so the same cache keys).
#   2. Fetch each query from the cache only: cached_only=True raises rather than spending money.
#   3. Group the results by change id, de-duplicated by URL.
#   4. The base story is story.json if built, else assemble_story() from the detected changes.
#
# Why shared: every approach must see exactly the same pages, so differences in results come
# from the method, not from what it happened to search for.
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(HERE))

from app.agent import search as exa_search  # noqa: E402
from app.agent.trace import Budget, Trace  # noqa: E402
from app.config import settings  # noqa: E402
from app.pipeline.changes import detect_changes  # noqa: E402
from app.pipeline.story import assemble_story  # noqa: E402
import record  # noqa: E402  (same query template as the recording)


@dataclass
class PlaceInput:
    """Everything an approach gets for one place."""
    story_id: str
    story: dict                                   # schema-valid base story, all cause_unknown
    results_by_change: dict[str, list[dict]] = field(default_factory=dict)


def base_story(story_dir: Path) -> dict | None:
    """story.json if it exists; else assembled from imagery.json + detected changes."""
    story_file = story_dir / "story.json"
    if story_file.exists():
        return json.loads(story_file.read_text(encoding="utf-8"))
    changes = detect_changes(story_dir)
    if not changes:
        return None
    return assemble_story(story_dir, changes)


async def recorded_results(story: dict) -> dict[str, list[dict]]:
    """The recorded Exa results for each change, fetched from cache only (never spends)."""
    by_change: dict[str, list[dict]] = {}
    trace = Trace(HERE / "input_trace" / story["id"])
    budget = Budget({"exa": 10_000, "llm": 0, "vision": 0})  # cache hits don't count anyway
    for change in story["changes"]:
        seen_urls = set()
        results = []
        for query in record.plan_queries(story["place"], change):
            found = await exa_search.search(
                query["query"], start_year=query["start_year"], end_year=query["end_year"],
                num_results=record.NUM_RESULTS, trace=trace, budget=budget, cached_only=True,
                purpose=f"s4 input {change['id']}")
            for result in found:
                if result["url"] not in seen_urls:
                    seen_urls.add(result["url"])
                    results.append(result)
        by_change[change["id"]] = results
    return by_change


async def load_place(story_id: str) -> PlaceInput | None:
    """The base story and recorded results for one place, or None if it has no changes."""
    story = base_story(settings.data_dir / "stories" / story_id)
    if story is None:
        return None
    return PlaceInput(story_id, story, await recorded_results(story))


def gold_places() -> list[dict]:
    """The S3 gold places list (story ids and expected changes with cause_keywords)."""
    return json.loads((REPO / "spikes" / "s3-changes" / "places.json").read_text(encoding="utf-8"))
