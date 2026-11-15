# CLI: pre-builds the gallery stories one by one, with an Exa cost check before anything is spent.
# Inputs: places below (or --only <key>), --live to allow paid Exa searches; settings from the .env.
# Outputs: data/stories/<id>/ for each place, the gallery index, and a summary on stdout.
#
# How it works:
#   1. For each place, work out its changes from the saved imagery (no network) and plan the
#      same Exa queries the cause agent will make.
#   2. Count which of those are already in the Exa cache (free) and which would be paid.
#   3. Print the plan: paid searches, cost at the ledger rate, and the ledger after it.
#   4. Only with --live: build each story (geocode, imagery, changes, causes, save). Without
#      --live the build runs with EXA_CACHED_ONLY=1, so it can never spend money.
#
# Why: the $20 Exa limit is for the whole project (memory: exa-budget), so every batch must
# show its cost before it runs. Builds go one at a time to stay under Gemini's rate limit.
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# (key, query, fixed centre or None). Fixed centres come from eval/gold_places.yaml.
PLACES = [
    ("lake-mead", "Lake Mead, Nevada", None),
    ("paradise", "Paradise, California", None),
    ("palm-jumeirah", "Palm Jumeirah, Dubai", None),
    ("summerlin", "Summerlin, Nevada", None),
    ("willow-run", "Willow Run, Michigan", (42.2424, -83.5552)),
    ("delray", "Delray, Detroit, Michigan", (42.289, -83.112)),
    ("ypsilanti", "Ypsilanti, Michigan", None),
]


def plan_place(key: str, query: str, centre) -> dict:
    """Paid vs cached Exa searches for one place, from its saved imagery (no network).
    A place never built before can't be planned offline; it's reported as 'unknown'."""
    from app.agent import causes, search
    from app.config import settings
    from app.pipeline.changes import detect_changes

    story_dirs = sorted((settings.data_dir / "stories").glob(f"{key}*"))
    built = [d for d in story_dirs if (d / "imagery.json").exists()]
    if not built:
        return {"key": key, "paid": None, "cached": None, "note": "no saved imagery yet"}
    story_dir = built[0]
    place = json.loads((story_dir / "imagery.json").read_text(encoding="utf-8"))["place"]
    paid = cached = 0
    for change in detect_changes(story_dir):
        for planned in causes.plan_queries(place, change):
            request = search.normalised_request(planned["query"], planned["start_year"],
                                                planned["end_year"], causes.NUM_RESULTS)
            if search.read_cache(search.request_key(request)) is None:
                paid += 1
            else:
                cached += 1
    return {"key": key, "paid": paid, "cached": cached, "note": story_dir.name}


async def build_one(query: str, centre) -> str:
    """Builds and saves one story; returns a one-line result."""
    from app.pipeline.build import NoLastingChanges, build_story

    start = time.perf_counter()
    try:
        if centre:
            story = await build_story(query, lat=centre[0], lon=centre[1], display_name=query)
        else:
            story = await build_story(query)
    except NoLastingChanges:
        return f"{query}: no lasting change, no story"
    sourced = sum(1 for c in story["chapters"] if c["kind"] == "change" and c["claims"])
    changes = sum(1 for c in story["chapters"] if c["kind"] == "change")
    usage = story["generation"]["usage"]
    seconds = time.perf_counter() - start
    return (f"{story['id']}: {sourced}/{changes} changes sourced, {usage['exa_calls']} paid Exa, "
            f"{usage['llm_calls']} LLM, {story['script']['word_count']} words, {seconds:.0f}s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="allow paid Exa searches")
    parser.add_argument("--only", help="build just this place key")
    args = parser.parse_args()
    if not args.live:
        os.environ["EXA_CACHED_ONLY"] = "1"   # read at call time: a dry build can't spend

    from app.agent import search

    places = [p for p in PLACES if not args.only or p[0] == args.only]
    plans = [plan_place(*p) for p in places]
    paid = sum(p["paid"] or 0 for p in plans)
    for p in plans:
        detail = "?" if p["paid"] is None else f"{p['paid']} paid, {p['cached']} cached"
        print(f"  {p['key']:14} {detail:24} {p['note']}")
    cost = paid * search.EXA_COST_PER_REQUEST_USD
    print(f"Paid searches: {paid} (~${cost:.2f} at the ledger rate). "
          f"Ledger: ${search.spent_usd():.2f} spent, ${search.remaining_usd():.2f} left of the cap.")
    if not args.live and paid:
        print("Dry mode: changes needing paid searches will stay cause_unknown. Use --live to pay.")

    for key, query, centre in places:
        line = asyncio.run(build_one(query, centre))   # one event loop per build (llm.py is per-loop)
        print(f"  built {line}")
    print(f"Ledger now: ${search.spent_usd():.2f} spent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
