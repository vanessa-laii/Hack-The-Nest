# Validates story.json / build events against contracts/*.schema.json plus CONTRACTS §2 rules.
# Inputs: a parsed story dict (or event dict).
# Outputs: list of readable error strings (empty = valid); `assert_valid_story` raises on any error.
#
# How it works:
#   1. Load each schema file once and build a validator for it.
#   2. Check the story against the schema; stop there if it fails.
#   3. Check the rules a schema can't express: years line up, excerpts are verified,
#      chapters and claims point to real things, chapter count, script word count.
#   4. Return every problem found, or raise ContractError before a save.
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from app.config import CONTRACTS_DIR


# --- Schema checks ---

@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    """Loads contracts/<name>.schema.json once and builds a validator for it.
    FormatChecker makes "format" (dates, uris) count, so "2014-13-40" is rejected."""
    schema_path = CONTRACTS_DIR / f"{name}.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)  # a broken schema must not approve bad data
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _schema_errors(name: str, obj: Any) -> list[str]:
    """Collects every schema error (not just the first), each with its JSON path."""
    found = _validator(name).iter_errors(obj)
    found = sorted(found, key=lambda e: list(e.absolute_path))
    messages = []
    for error in found:
        path = "/".join(map(str, error.absolute_path))
        messages.append(f"schema: /{path}: {error.message}")
    return messages


# --- Story rules (CONTRACTS §2) ---

def _year_errors(story: dict) -> list[str]:
    """Frames and metrics must cover the same years, strictly ascending.
    The player lines up film, chart and timeline by year."""
    errors = []
    frame_years = [frame["year"] for frame in story["frames"]]
    metric_years = [point["year"] for point in story["metrics"]["series"]]
    is_sorted = frame_years == sorted(frame_years)
    has_duplicates = len(set(frame_years)) != len(frame_years)
    if not is_sorted or has_duplicates:
        errors.append("frames years must be strictly ascending")
    if frame_years != metric_years:
        errors.append(f"frames years {frame_years} != metrics years {metric_years}")
    return errors


def _all_excerpts(story: dict) -> list[dict]:
    """Every excerpt from every source, in order."""
    excerpts = []
    for source in story["sources"]:
        excerpts.extend(source["excerpts"])
    return excerpts


def _chapter_errors(chapter: dict, change_ids: set[str], excerpt_ids: set[str]) -> list[str]:
    """Checks one chapter's change link, confidence and claims.
    No evidence must mean cause_unknown (TR-8); every claim needs a real excerpt (TR-1)."""
    errors = []
    cid = chapter["id"]
    is_change = chapter["kind"] == "change"
    change_id = chapter.get("change_id")

    if is_change != (change_id is not None):
        errors.append(f"{cid}: change_id must be set exactly when kind == 'change'")
    if is_change and change_id not in change_ids:
        errors.append(f"{cid}: change_id {change_id} not in changes")

    if is_change:
        is_unknown = chapter.get("confidence") == "cause_unknown"
        has_no_claims = len(chapter["claims"]) == 0
        if is_unknown != has_no_claims:
            errors.append(f"{cid}: confidence cause_unknown <=> no claims")

    # Causes belong in change chapters, where confidence is set (TR-7).
    if not is_change and chapter["claims"]:
        errors.append(f"{cid}: {chapter['kind']} chapter must have no claims")

    for claim in chapter["claims"]:
        missing = [e for e in claim["excerpt_ids"] if e not in excerpt_ids]
        if missing:
            errors.append(f"{claim['id']}: excerpt_ids not found in sources: {missing}")
    return errors


def _script_errors(story: dict, chapter_ids: set[str], claim_ids: set[str]) -> list[str]:
    """Segments must point to real chapters and claims; the word count is recounted.
    HL-1 (a 60–90 s film) is enforced from this number, so a stale one must not pass."""
    errors = []
    words = 0
    for segment in story["script"]["segments"]:
        words += len(segment["text"].split())
        if segment["chapter_id"] not in chapter_ids:
            errors.append(
                f"{segment['id']}: chapter_id {segment['chapter_id']} not in chapters"
            )
        missing = [c for c in segment["claim_ids"] if c not in claim_ids]
        if missing:
            errors.append(f"{segment['id']}: claim_ids not found in chapters: {missing}")

    stored_count = story["script"]["word_count"]
    if words != stored_count:
        errors.append(f"script.word_count {stored_count} != real count {words}")
    if not 150 <= words <= 225:
        errors.append(f"script has {words} words; must be 150-225 (HL-1)")
    return errors


def story_errors(story: dict) -> list[str]:
    """Lists every problem with a story: schema first, then the cross-field rules.
    Those rules carry the truthfulness promises (TR-1/TR-2) and limits (HL-1/HL-2)."""
    errors = _schema_errors("story", story)
    if errors:
        # The checks below assume a well-formed story; on a broken one they would
        # raise KeyErrors that hide the real schema message.
        return errors

    errors.extend(_year_errors(story))

    # The schema already requires verified == true. TR-2 is the core promise,
    # so it is checked again in code.
    excerpts = _all_excerpts(story)
    excerpt_ids = {excerpt["id"] for excerpt in excerpts}
    unverified = [excerpt["id"] for excerpt in excerpts if not excerpt.get("verified")]
    if unverified:
        errors.append(f"unverified excerpts present (TR-1/TR-2): {unverified}")

    chapters = story["chapters"]
    chapter_ids = {chapter["id"] for chapter in chapters}
    change_ids = {change["id"] for change in story["changes"]}
    claim_ids: set[str] = set()
    for chapter in chapters:
        errors.extend(_chapter_errors(chapter, change_ids, excerpt_ids))
        for claim in chapter["claims"]:
            claim_ids.add(claim["id"])

    # HL-2: 1–5 change chapters, plus the intro and outro.
    n_change_chapters = sum(1 for chapter in chapters if chapter["kind"] == "change")
    if not 1 <= n_change_chapters <= 5:
        errors.append(f"{n_change_chapters} change chapters; must be 1-5 (HL-2)")

    errors.extend(_script_errors(story, chapter_ids, claim_ids))
    return errors


def event_errors(event: dict) -> list[str]:
    """Checks one build event against event.schema.json.
    The web app's progress page parses these directly, so a bad one breaks it."""
    return _schema_errors("event", event)


class ContractError(ValueError):
    """Raised when a story breaks the contract.
    A separate type so callers can tell 'our output is invalid' from ordinary bugs."""


def assert_valid_story(story: dict) -> None:
    """Raises ContractError listing every problem. Called before every save
    (CLAUDE.md #4), so an invalid story never reaches disk or the player."""
    errors = story_errors(story)
    if errors:
        raise ContractError("story.json invalid:\n  " + "\n  ".join(errors))
