# CLI: runs the imagery half of the pipeline for one place and prints build events as they happen.
# Inputs: place query (argv), optional --aoi-km; settings from the repo-root .env (EM_MOCK ignored).
# Outputs: DATA_DIR/stories/<id>/{frames,thumbs,imagery.json}; events on stdout; exit 1 on failure.
#
# How it works:
#   1. Read the place query, optional --aoi-km, and optional fixed --lat/--lon centre.
#   2. Run the imagery pipeline, printing and schema-checking each event as it arrives.
#   3. Print a summary: story id, years kept, years skipped, timings.
#
# Lets us build and inspect real frames before the HTTP flow exists, and pre-build gallery places.
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.contract_check import event_errors  # noqa: E402
from app.pipeline.run import run_imagery  # noqa: E402


async def _print_event(ev: dict) -> None:
    """Prints one event and flags it if it breaks event.schema.json.
    The web app's progress page parses exactly these, so a bad one should fail loudly here."""
    errors = event_errors(ev)
    flag = f"   !! INVALID: {errors}" if errors else ""
    seq = f"{ev['seq']:>3}"
    kind = f"{ev['type']:8}"
    stage = f"{ev.get('stage', ''):9}"
    print(f"[{seq}] {kind} {stage} {ev['message']}{flag}", flush=True)


async def main() -> int:
    """Builds imagery for one place from the command line and prints a summary."""
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--aoi-km", type=float, default=None)
    # A fixed centre skips the name search (as if picked from the place list), e.g. for
    # gold places whose centre comes from eval/gold_places.yaml. --name labels the story.
    parser.add_argument("--lat", type=float, default=None)
    parser.add_argument("--lon", type=float, default=None)
    parser.add_argument("--name", default=None, help="display name when --lat/--lon are given")
    args = parser.parse_args()
    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon must be given together")

    result = await run_imagery(args.query, lat=args.lat, lon=args.lon,
                               display_name=args.name or (args.query if args.lat is not None else None),
                               aoi_km=args.aoi_km, sink=_print_event)
    summary = {
        "story_id": result.story_id,
        "kept_years": len(result.kept),
        "skipped": result.sections["coverage"]["years_skipped"],
        "timings": result.timings,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
