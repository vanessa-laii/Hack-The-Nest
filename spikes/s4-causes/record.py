# Spike S4 recorder: plans (and, only with --live, runs) the Exa searches for the gold places.
# Inputs: spikes/s3-changes/places.json; data/stories/<id>/ (story.json, or imagery.json + npz).
# Outputs: a printed plan with cost and ledger status; with --live, cached results under
#          DATA_DIR/cache/exa/ and a trace per place in spikes/s4-causes/record_trace/.
#
# How it works:
#   1. For each gold place with a built story folder, take its changes: from story.json if it
#      exists, else by running app.pipeline.changes.detect_changes() on the folder.
#   2. Each change gets 2 queries from fixed, cause-neutral templates (app.agent.causes.
#      plan_queries), limited to the change's years widened by one year on each side.
#   3. Requests already in the cache are marked free; the rest are the planned live spend.
#   4. --dry-run (the default) prints the plan, the Exa ledger total, the planned spend and what
#      would remain under the cap. Nothing is called.
#   5. --live runs the uncached requests through app.agent.search, which enforces the HL-10
#      story budget and the project spend cap. It refuses to start if the plan would cross it.
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
REPO_ROOT = SPIKE_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.agent import search as exa_search  # noqa: E402
# The query template lives in the cause agent, so a real build asks exactly what was recorded.
from app.agent.causes import NUM_RESULTS, plan_queries  # noqa: E402,F401 (inputs.py uses both)
from app.agent.trace import Budget, BudgetExceeded, Trace  # noqa: E402
from app.config import settings  # noqa: E402

PLACES_FILE = REPO_ROOT / "spikes" / "s3-changes" / "places.json"
TRACE_DIR = SPIKE_DIR / "record_trace"
EXA_LIST_PRICE_USD = 0.007  # Exa's published price per search with contents


# --- Steps 1-2: changes per place and their queries ---


def load_story_changes(story_dir: Path) -> tuple[dict, list[dict]] | None:
    """(place, changes) from story.json, or from imagery.json + detect_changes(), or None."""
    story_file = story_dir / "story.json"
    if story_file.exists():
        story = json.loads(story_file.read_text(encoding="utf-8"))
        return story["place"], story["changes"]
    imagery_file = story_dir / "imagery.json"
    if not imagery_file.exists():
        return None
    from app.pipeline.changes import detect_changes  # heavy import, only when needed

    imagery = json.loads(imagery_file.read_text(encoding="utf-8"))
    return imagery["place"], detect_changes(story_dir)


def build_plan() -> list[dict]:
    """Every gold place with its planned queries (skipped places are listed with a reason)."""
    places = json.loads(PLACES_FILE.read_text(encoding="utf-8"))
    plan = []
    for gold in places:
        story_dir = settings.data_dir / "stories" / gold["story_id"]
        loaded = load_story_changes(story_dir)
        if loaded is None:
            plan.append({"story_id": gold["story_id"], "skipped": "no built story folder"})
            continue
        place, changes = loaded
        queries = []
        for change in changes:
            queries.extend(plan_queries(place, change))
        plan.append({"story_id": gold["story_id"], "queries": queries})
    return plan


# --- Step 3: what is already cached ---


def mark_cached(plan: list[dict]) -> None:
    """Marks each query as cached (free) or live, and drops exact repeats."""
    seen = set()
    for entry in plan:
        kept = []
        for query in entry.get("queries", []):
            request = exa_search.normalised_request(
                query["query"], query["start_year"], query["end_year"], NUM_RESULTS,
            )
            key = exa_search.request_key(request)
            if key in seen:
                continue
            seen.add(key)
            query["cached"] = exa_search.read_cache(key) is not None
            kept.append(query)
        if "queries" in entry:
            entry["queries"] = kept


# --- Step 4: print ---


def live_count(plan: list[dict]) -> int:
    """How many planned requests are not cached yet."""
    count = 0
    for entry in plan:
        for query in entry.get("queries", []):
            if not query["cached"]:
                count += 1
    return count


def print_plan(plan: list[dict]) -> float:
    """Prints every query and the money summary; returns the planned ledger spend."""
    total = 0
    for entry in plan:
        if "skipped" in entry:
            print(f"\n{entry['story_id']}: skipped ({entry['skipped']})")
            continue
        print(f"\n{entry['story_id']}: {len(entry['queries'])} queries")
        for query in entry["queries"]:
            status = "cached" if query["cached"] else "live"
            years = f"{query['start_year']}-{query['end_year']}"
            print(f"  [{status:6}] {query['change_id']} {years}  {query['query']}")
            total += 1
    live = live_count(plan)
    planned_spend = live * exa_search.EXA_COST_PER_REQUEST_USD
    spent = exa_search.spent_usd()
    print(f"\nQueries: {total} total, {live} not cached (live).")
    list_price_cost = live * EXA_LIST_PRICE_USD
    print(f"Cost at Exa list price (${EXA_LIST_PRICE_USD}/search): ${list_price_cost:.3f}")
    print(f"Ledger so far: ${spent:.2f} of the ${exa_search.EXA_SPEND_CAP_USD:.2f} cap")
    print(f"Planned spend (ledger rate ${exa_search.EXA_COST_PER_REQUEST_USD}): "
          f"${planned_spend:.2f}")
    print(f"Remaining after this plan: "
          f"${exa_search.EXA_SPEND_CAP_USD - spent - planned_spend:.2f}")
    return planned_spend


# --- Step 5: live run ---


async def run_live(plan: list[dict]) -> None:
    """Runs every uncached query through search(); stops a place when its budget is used."""
    for entry in plan:
        if "skipped" in entry:
            continue
        trace = Trace(TRACE_DIR / entry["story_id"])
        budget = Budget()  # HL-10 per story, the same limit a real build has
        for query in entry["queries"]:
            if query["cached"]:
                continue
            try:
                results = await exa_search.search(
                    query["query"], start_year=query["start_year"], end_year=query["end_year"],
                    num_results=NUM_RESULTS, trace=trace, budget=budget,
                    purpose=f"s4 record {query['change_id']}",
                )
            except BudgetExceeded:
                print(f"  {entry['story_id']}: story budget used up, rest skipped")
                break
            print(f"  {len(results)} results  {query['query']}")


def main() -> int:
    """Prints the plan; with --live, also records it (if it fits under the cap)."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the plan only (default)")
    mode.add_argument("--live", action="store_true", help="call Exa for uncached queries")
    args = parser.parse_args()

    plan = build_plan()
    mark_cached(plan)
    planned_spend = print_plan(plan)
    if not args.live:
        print("\nDry run: nothing was called. Use --live to record.")
        return 0

    if os.environ.get("EXA_CACHED_ONLY", "").strip() == "1" or settings.exa_cached_only:
        print("\nEXA_CACHED_ONLY is set; refusing --live.")
        return 1
    if exa_search.spent_usd() + planned_spend > exa_search.EXA_SPEND_CAP_USD:
        print("\nThis plan would cross the Exa spend cap; refusing --live.")
        return 1
    asyncio.run(run_live(plan))
    print(f"\nLedger now: ${exa_search.spent_usd():.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
