# Spike S3 scorer: runs one approach's detect() on every gold place and scores it the same way.
# Inputs: approach folder name (argv); spikes/s3-changes/places.json; data/stories/<id>/.
# Outputs: a table on stdout and spikes/s3-changes/<approach>/score.json.
#
# How it works:
#   1. Load the approach's detect() and the gold places with their expected changes.
#   2. Run detect() per place; validate each change against the story schema's changes[] item.
#   3. Match detections to expected changes (same kind/metric, years overlapping within ±2).
#   4. Report recall, false positives, control changes, the shrinking-lake rule and runtime,
#      plus two honesty checks: kind/direction contradictions and tiny changes.
#   Matching is one-to-one, so one detection can't count for several expected changes.
#
# One scorer for all approaches, so differences in results come from the methods, not the scoring.
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
YEAR_TOLERANCE = 2      # an expected "2018" burn matches a detected 2017–2020 change
CONTROL = "central-park"


def load_detect(approach: str):
    """Imports <approach>/detect.py and returns its detect function."""
    path = HERE / approach / "detect.py"
    spec = importlib.util.spec_from_file_location(f"s3_{approach.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module.detect


def change_validator() -> Draft202012Validator:
    """A validator for the story schema's changes[] list, sharing the schema's $defs."""
    schema = json.loads((REPO / "contracts" / "story.schema.json").read_text(encoding="utf-8"))
    changes_schema = {**schema["properties"]["changes"], "$defs": schema["$defs"]}
    return Draft202012Validator(changes_schema)


def matches(expected: dict, found: dict) -> bool:
    """Same kind (and metric, if the gold entry names one), with overlapping years ±2."""
    if expected["kind"] != found["kind"]:
        return False
    if "metric" in expected and expected["metric"] != found["metric"]:
        return False
    lo, hi = expected["approx_years"]
    return found["start_year"] <= hi + YEAR_TOLERANCE and found["end_year"] >= lo - YEAR_TOLERANCE


# Which way each kind must move its metric. A "water_loss" whose water rose, or an
# "urban_growth" whose built-up fell, would put a contradiction into the narration.
EXPECTED_SIGN = {
    ("water_loss", "water_frac"): -1, ("water_gain", "water_frac"): +1,
    ("vegetation_loss", "ndvi_mean"): -1, ("vegetation_gain", "ndvi_mean"): +1,
    ("burn", "ndvi_mean"): -1, ("urban_growth", "built_frac"): +1,
}
TINY = 0.005   # under 0.5 percentage points of the area (or 0.005 NDVI): hard to show or narrate


def describe(f: dict) -> str:
    """One line per change for the report."""
    sensor = " [sensor]" if f["sensor_switch_flag"] else ""
    return (f"{f['kind']}/{f['metric']} {f['start_year']}-{f['end_year']} "
            f"({f['magnitude']:+.3f}){sensor}")


def contradicts(f: dict) -> bool:
    """True if the change's kind says one direction and its magnitude says the other,
    or the kind is measured on a metric that can't show it (e.g. water_loss on built_frac)."""
    if f["kind"] == "other":
        return False
    sign = EXPECTED_SIGN.get((f["kind"], f["metric"]))
    if sign is None:
        return True
    return f["magnitude"] * sign <= 0


def is_tiny(f: dict) -> bool:
    return abs(f["magnitude"]) < TINY


def match_one_to_one(expected: list[dict], found: list[dict]) -> tuple[list[bool], list[dict]]:
    """Pairs each expected change with at most one detection and vice versa, largest
    detections first. Stops a spray of small detections from matching everything."""
    hit = [False] * len(expected)
    used: list[dict] = []
    for f in sorted(found, key=lambda c: -abs(c["magnitude"])):
        for i, e in enumerate(expected):
            if not hit[i] and matches(e, f):
                hit[i] = True
                used.append(f)
                break
    return hit, used


def score_place(place: dict, detect, validator) -> dict:
    """Runs detect() on one place and compares it with that place's expected changes."""
    story = REPO / "data" / "stories" / place["story_id"]
    start = time.perf_counter()
    found = detect(story)
    seconds = time.perf_counter() - start

    errors = [e.message for e in validator.iter_errors(found)]
    expected = place["expected_changes"] if isinstance(place["expected_changes"], list) else []
    hit, used = match_one_to_one(expected, found)
    return {
        "found": [describe(f) for f in found],
        "expected": len(expected),
        "matched": sum(hit),
        "missed": [e for e, h in zip(expected, hit) if not h],
        "false_positives": len(found) - len(used),
        "contradictions": [describe(f) for f in found if contradicts(f)],
        "tiny": [describe(f) for f in found if is_tiny(f)],
        "schema_errors": errors,
        "seconds": round(seconds, 3),
        "scored": isinstance(place["expected_changes"], list),
    }


def main() -> int:
    approach = sys.argv[1]
    detect = load_detect(approach)
    validator = change_validator()
    places = json.loads((HERE / "places.json").read_text(encoding="utf-8"))

    results = {}
    for place in places:
        results[place["key"]] = score_place(place, detect, validator)

    scored = [r for r in results.values() if r["scored"]]
    expected = sum(r["expected"] for r in scored)
    matched = sum(r["matched"] for r in scored)
    lake = results.get("lake-mead", {}).get("found", [])
    summary = {
        "recall": round(matched / expected, 3) if expected else None,
        "matched": matched,
        "expected": expected,
        "control_changes": len(results.get(CONTROL, {}).get("found", [])),
        "false_positives": sum(r["false_positives"] for r in scored),
        "schema_errors": sum(len(r["schema_errors"]) for r in results.values()),
        "lake_mead_urban_growth": sum(1 for f in lake if f.startswith("urban_growth")),
        "contradictions": sum(len(r["contradictions"]) for r in results.values()),
        "tiny": sum(len(r["tiny"]) for r in results.values()),
        "max_seconds": max(r["seconds"] for r in results.values()),
    }
    (HERE / approach / "score.json").write_text(
        json.dumps({"summary": summary, "places": results}, indent=2), encoding="utf-8")

    for key, r in results.items():
        tag = "" if r["scored"] else " (not scored)"
        print(f"{key:14} matched {r['matched']}/{r['expected']}  FP {r['false_positives']}{tag}")
        for line in r["found"]:
            print(f"    found  {line}")
        for miss in r["missed"]:
            print(f"    MISSED {miss['kind']} {miss['approx_years']}")
        for line in r["contradictions"]:
            print(f"    CONTRADICTS {line}")
        for err in r["schema_errors"][:3]:
            print(f"    SCHEMA {err}")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
