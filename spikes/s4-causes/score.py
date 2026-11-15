# Spike S4 scorer: runs one approach's explain() on every gold place and checks every claim in code.
# Inputs: approach folder name (argv); recorded Exa results (cache only); GEMINI via app.llm.
# Outputs: a table on stdout, <approach>/score.json, and <approach>/review.md (claims + quotes).
#
# How it works:
#   1. For each gold place with changes, load the base story and recorded pages (inputs.py).
#   2. Run explain() with a fresh per-story Budget (HL-10) and a Trace.
#   3. Merge its sources and claims into the story and validate it against the contract.
#   4. Re-check every claim in code, independently of the approach (REQUIREMENTS §4):
#        TR-2  every excerpt appears word for word on the page it cites (from the recording),
#        TR-3/4 at least one of its excerpts names the place and fits the years ±1,
#        TR-7  each claim's confidence is the rule applied to ITS OWN sources (when the
#              approach reports claim_confidence), and the chapter's equals its best claim's.
#              Pooling sources across claims fails this check.
#      Any failure counts as an unsupported claim; the pass bar is ZERO.
#   5. Cause recall: an expected gold cause is found if the matching change's chapter has a
#      claim whose text or excerpts contain one of the gold cause_keywords.
#   6. Write review.md so a person can read each claim next to its quotes (TR-5 by eye),
#      plus what the TR-5 checker dropped and which claims a rewrite rescued (if reported).
#
# One scorer for all approaches, so results differ only because the methods differ.
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

os.environ["EXA_CACHED_ONLY"] = "1"   # belt and braces: the spike can never spend Exa money

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import inputs  # noqa: E402  (sets up sys.path for app.*)

from app.agent import verify  # noqa: E402
from app.agent.trace import Budget, Trace  # noqa: E402
from app.contract_check import story_errors  # noqa: E402

sys.path.insert(0, str(inputs.REPO / "spikes" / "s3-changes"))
from score import matches as change_matches  # noqa: E402  (S3's kind/years matching)

CAUSE_RECALL_BAR = 0.6


def load_explain(approach: str):
    """Imports <approach>/explain.py and returns its async explain(place, *, trace, budget)."""
    path = HERE / approach / "explain.py"
    spec = importlib.util.spec_from_file_location(f"s4_{approach.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module.explain


def merge(story: dict, answer: dict) -> dict:
    """The story with the approach's sources and per-change claims/confidence filled in."""
    merged = json.loads(json.dumps(story))
    merged["sources"] = answer.get("sources", [])
    by_change = answer.get("chapters", {})
    for chapter in merged["chapters"]:
        if chapter["kind"] == "change" and chapter["change_id"] in by_change:
            filled = by_change[chapter["change_id"]]
            chapter["claims"] = filled.get("claims", [])
            chapter["confidence"] = filled.get("confidence", "cause_unknown")
    # The base story's script may cite claims from an earlier build; the script isn't scored
    # here, so links to claims the merge replaced are removed rather than reported.
    claim_ids = set()
    for chapter in merged["chapters"]:
        for claim in chapter["claims"]:
            claim_ids.add(claim["id"])
    for segment in merged["script"]["segments"]:
        kept = []
        for claim_id in segment["claim_ids"]:
            if claim_id in claim_ids:
                kept.append(claim_id)
        segment["claim_ids"] = kept
    return merged


def page_texts(place: inputs.PlaceInput) -> dict[str, str]:
    """URL -> recorded page text, across all changes of the place."""
    texts = {}
    for results in place.results_by_change.values():
        for result in results:
            texts[result["url"]] = result.get("text") or ""
    return texts


LEVELS = ["cause_unknown", "likely", "confirmed"]   # TR-7, weakest first


def claim_level_problems(claim: dict, own_sources: dict,
                         claim_confidence: dict | None) -> tuple[list[str], str]:
    """(problems, expected level) for one claim, from its OWN sources only (TR-7)."""
    expected = verify.confidence(list(own_sources.values()))
    problems = []
    if claim_confidence is not None:
        reported = claim_confidence.get(claim["id"])
        if reported != expected:
            problems.append(f"{claim['id']}: claim confidence {reported} but its own sources "
                            f"give {expected} (TR-7)")
    return problems, expected


def check_claims(story: dict, place: inputs.PlaceInput,
                 claim_confidence: dict | None = None) -> tuple[list[str], int]:
    """Independent truth checks. Returns (problems, number of claims checked).
    claim_confidence is the approach's {claim_id: level}, when it reports one."""
    problems = []
    texts = page_texts(place)
    names = verify.place_names_for(story["place"])
    excerpt_source = {}
    for source in story["sources"]:
        for excerpt in source["excerpts"]:
            excerpt_source[excerpt["id"]] = (excerpt, source)

    checked = 0
    for chapter in story["chapters"]:
        if chapter["kind"] != "change":
            continue
        best = "cause_unknown"
        for claim in chapter["claims"]:
            checked += 1
            fits_place_and_years = False
            own_sources = {}
            for excerpt_id in claim["excerpt_ids"]:
                if excerpt_id not in excerpt_source:
                    problems.append(f"{claim['id']}: unknown excerpt {excerpt_id}")
                    continue
                excerpt, source = excerpt_source[excerpt_id]
                page = texts.get(source["url"])
                if page is None:
                    problems.append(f"{claim['id']}: {source['url']} is not a recorded page")
                    continue
                if not verify.excerpt_in_page(excerpt["text"], page):
                    problems.append(f"{claim['id']}: excerpt {excerpt_id} not word for word "
                                    "(TR-2)")
                    continue
                own_sources[source["id"]] = source
                if (verify.names_place(excerpt["text"], names)
                        and verify.in_years(excerpt["text"], source.get("published_date"),
                                            chapter["start_year"], chapter["end_year"])):
                    fits_place_and_years = True
            if not fits_place_and_years:
                problems.append(f"{claim['id']}: no excerpt both names the place and fits "
                                f"{chapter['start_year']}-{chapter['end_year']} ±1 (TR-3/4)")
            level_problems, expected = claim_level_problems(claim, own_sources,
                                                            claim_confidence)
            problems += level_problems
            if LEVELS.index(expected) > LEVELS.index(best):
                best = expected
        if chapter["confidence"] != best:
            problems.append(f"{chapter['id']}: confidence {chapter['confidence']} but its best "
                            f"claim's own sources give {best} (TR-7)")
    return problems, checked


def causes_found(story: dict, gold: dict) -> tuple[int, int, list[str]]:
    """(found, expected, notes) for gold changes that list cause_keywords."""
    expected_list = gold["expected_changes"] if isinstance(gold["expected_changes"], list) else []
    found = 0
    notes = []
    chapters = {c["change_id"]: c for c in story["chapters"] if c["kind"] == "change"}
    excerpts = {e["id"]: e["text"] for s in story["sources"] for e in s["excerpts"]}
    for expected in expected_list:
        keywords = [k.lower() for k in expected.get("cause_keywords", [])]
        hit = False
        for change in story["changes"]:
            if not change_matches(expected, change):
                continue
            chapter = chapters.get(change["id"])
            for claim in (chapter or {}).get("claims", []):
                cited = " ".join(excerpts.get(i, "") for i in claim["excerpt_ids"])
                text = claim["text"] + " " + cited
                if any(k in text.lower() for k in keywords):
                    hit = True
        found += hit
        notes.append(f"{'FOUND ' if hit else 'MISSED'} {expected['kind']} "
                     f"{expected['approx_years']} {keywords}")
    return found, len(expected_list), notes


def review_lines(story: dict) -> list[str]:
    """Markdown: each claim with its quotes and links, for a person to judge TR-5."""
    excerpt_source = {e["id"]: (e, s) for s in story["sources"] for e in s["excerpts"]}
    lines = [f"## {story['place']['display_name']}"]
    for chapter in story["chapters"]:
        if chapter["kind"] != "change":
            continue
        lines.append(f"\n**{chapter['id']} {chapter['start_year']}-{chapter['end_year']}: "
                     f"{chapter['headline']}**, confidence `{chapter['confidence']}`")
        for claim in chapter["claims"]:
            lines.append(f"- CLAIM: {claim['text']}")
            for excerpt_id in claim["excerpt_ids"]:
                if excerpt_id in excerpt_source:
                    excerpt, source = excerpt_source[excerpt_id]
                    link = f"[{source['domain']}]({source['url']})"
                    lines.append(f"  - > {excerpt['text']}  \n    {link}"
                                 f" {source.get('published_date')}")
    return lines


def checks_lines(checks: dict) -> list[str]:
    """Markdown: what the TR-5 checker dropped and which claims a rewrite rescued."""
    lines = []
    for change_id, change_checks in checks.items():
        for dropped in change_checks.get("checker_dropped", []):
            lines.append(f"- CHECKER DROPPED ({change_id}): {dropped['text']}  \n"
                         f"  reason: {dropped['reason']}")
        for text in change_checks.get("rescued", []):
            lines.append(f"- RESCUED BY REWRITE ({change_id}): {text}")
    if lines:
        return ["\n**Checks**"] + lines
    return []


def check_counts(checks: dict) -> tuple[int, int]:
    """(claims the checker dropped, claims a rewrite rescued) over all changes."""
    dropped = 0
    rescued = 0
    for change_checks in checks.values():
        dropped += len(change_checks.get("checker_dropped", []))
        rescued += len(change_checks.get("rescued", []))
    return dropped, rescued


async def main() -> int:
    approach = sys.argv[1]
    explain = load_explain(approach)
    out_dir = HERE / approach
    results, review = {}, ["# S4 review: every claim with its quotes\n"]
    total = {"claims": 0, "unsupported": 0, "found": 0, "expected": 0, "llm_calls": 0,
             "schema_errors": 0, "seconds": 0.0, "checker_dropped": 0, "rescued": 0}

    for gold in inputs.gold_places():
        place = await inputs.load_place(gold["story_id"])
        if place is None:
            continue
        budget = Budget()   # real HL-10 limits per story
        trace = Trace(out_dir / "trace" / gold["story_id"])
        start = time.perf_counter()
        try:
            answer = await explain(place, trace=trace, budget=budget)
            crashed = None
        except Exception as error:  # a crash scores as all cause_unknown, and is reported
            answer, crashed = {}, f"{type(error).__name__}: {error}"
        seconds = time.perf_counter() - start

        story = merge(place.story, answer)
        schema = story_errors(story)
        problems, checked = check_claims(story, place, answer.get("claim_confidence"))
        checks = answer.get("checks", {})
        checker_dropped, rescued = check_counts(checks)
        found, expected, notes = causes_found(story, gold)
        llm_calls = budget.used.get("llm", 0)
        results[gold["key"]] = {"claims": checked, "unsupported": problems, "schema": schema,
                                "found": found, "expected": expected, "notes": notes,
                                "llm_calls": llm_calls, "seconds": round(seconds, 1),
                                "crashed": crashed, "checker_dropped": checker_dropped,
                                "rescued": rescued, "checks": checks,
                                "confidence": [c["confidence"] for c in story["chapters"]
                                               if c["kind"] == "change"]}
        total["claims"] += checked
        total["unsupported"] += len(problems)
        total["found"] += found
        total["expected"] += expected
        total["llm_calls"] += llm_calls
        total["schema_errors"] += len(schema)
        total["seconds"] += seconds
        total["checker_dropped"] += checker_dropped
        total["rescued"] += rescued
        review += review_lines(story) + checks_lines(checks) + [""]

    total["seconds"] = round(total["seconds"], 1)
    total["cause_recall"] = None
    if total["expected"]:
        total["cause_recall"] = round(total["found"] / total["expected"], 3)
    total["passes"] = (total["unsupported"] == 0 and total["schema_errors"] == 0
                       and (total["cause_recall"] or 0) >= CAUSE_RECALL_BAR)
    scores = json.dumps({"summary": total, "places": results}, indent=2, ensure_ascii=False)
    (out_dir / "score.json").write_text(scores, encoding="utf-8")
    (out_dir / "review.md").write_text("\n".join(review), encoding="utf-8")
    for key, r in results.items():
        print(f"{key:14} claims {r['claims']:2}  causes {r['found']}/{r['expected']}  "
              f"llm {r['llm_calls']:2}  {r['seconds']:5.1f}s  conf {r['confidence']}  "
              f"checker-dropped {r['checker_dropped']} rescued {r['rescued']}"
              f"{'  CRASHED ' + r['crashed'] if r['crashed'] else ''}")
        for line in r["unsupported"][:5] + r["schema"][:3]:
            print(f"    !! {line}")
    print(json.dumps(total, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
