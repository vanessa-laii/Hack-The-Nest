# Validates story.json / build events against contracts/*.schema.json plus the CONTRACTS §2 invariants.
# Inputs: a parsed story dict (or event dict).
# Outputs: list of human-readable error strings (empty = valid); `assert_valid_story` raises on any error.
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from app.config import CONTRACTS_DIR


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    """Loads contracts/<name>.schema.json and builds a validator for it, once.

    Why the cache: every story save and every event goes through here, and
    re-parsing the schema each time would waste work.
    Why FormatChecker: without it, jsonschema skips "format" (date, date-time,
    uri) entirely, so a bad date like "2014-13-40" would get through.
    check_schema also catches a broken schema file before it can approve bad data.
    """
    schema = json.loads((CONTRACTS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _schema_errors(name: str, obj: Any) -> list[str]:
    """Collects every schema error, not just the first, each with a JSON path.
    Why: one validation run should show all the problems at once."""
    errs = sorted(_validator(name).iter_errors(obj), key=lambda e: list(e.absolute_path))
    return [f"schema: /{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errs]


def story_errors(story: dict) -> list[str]:
    """Lists every problem with a story: the schema first, then the invariants.

    Why invariants as well as the schema: JSON Schema can't express rules that
    cross fields (an excerpt id must exist elsewhere in the file, the word count
    must match the text). CONTRACTS §2 promises the web app these hold, and
    several of them carry the truthfulness rules (TR-1/TR-2) and hard limits (HL-1/HL-2).
    """
    errors = _schema_errors("story", story)
    if errors:
        # The checks below index into fields freely. On a malformed story they
        # would raise KeyErrors that hide the real schema message, so stop here.
        return errors

    # Frames and metrics must cover the same years in ascending order.
    # Why: the player lines up the film, the chart and the timeline by year.
    # A mismatch shows a frame whose numbers belong to a different year.
    f_years = [f["year"] for f in story["frames"]]
    m_years = [s["year"] for s in story["metrics"]["series"]]
    if f_years != sorted(f_years) or len(set(f_years)) != len(f_years):
        errors.append("frames years must be strictly ascending")
    if f_years != m_years:
        errors.append(f"frames years {f_years} != metrics years {m_years}")

    # Every excerpt must be verified. The schema already requires
    # verified == true; this repeats the check in code because TR-2 is the
    # product's core promise and must not depend on the schema alone.
    excerpt_ids = {ex["id"] for src in story["sources"] for ex in src["excerpts"]}
    unverified = [ex["id"] for src in story["sources"] for ex in src["excerpts"] if not ex.get("verified")]
    if unverified:
        errors.append(f"unverified excerpts present (TR-1/TR-2): {unverified}")

    # Chapter rules:
    #  - change_id is set exactly when kind == "change", and points to a real change.
    #  - cause_unknown <=> no claims. Why: TR-8. A chapter with no evidence must
    #    say "unknown", and one marked unknown must not sneak in claims.
    #  - every claim's excerpt_ids exist. Why: TR-1. A claim pointing to a missing
    #    excerpt is a claim without evidence, and the "one click to the excerpt"
    #    link (FR-54) would be dead.
    claim_ids: set[str] = set()
    chapter_ids = {c["id"] for c in story["chapters"]}
    change_ids = {c["id"] for c in story["changes"]}
    for ch in story["chapters"]:
        is_change = ch["kind"] == "change"
        if is_change != (ch.get("change_id") is not None):
            errors.append(f"{ch['id']}: change_id must be set exactly when kind == 'change'")
        if is_change and ch.get("change_id") not in change_ids:
            errors.append(f"{ch['id']}: change_id {ch.get('change_id')} not in changes")
        if is_change and (ch.get("confidence") == "cause_unknown") != (len(ch["claims"]) == 0):
            errors.append(f"{ch['id']}: confidence cause_unknown <=> no claims")
        for cl in ch["claims"]:
            claim_ids.add(cl["id"])
            missing = [e for e in cl["excerpt_ids"] if e not in excerpt_ids]
            if missing:
                errors.append(f"{cl['id']}: excerpt_ids not found in sources: {missing}")

    # HL-2: 1–5 change chapters, plus the intro and outro.
    n_change_chapters = sum(1 for c in story["chapters"] if c["kind"] == "change")
    if not 1 <= n_change_chapters <= 5:
        errors.append(f"{n_change_chapters} change chapters; must be 1-5 (HL-2)")

    # Script rules. Each segment must point to a real chapter and real claims
    # (narration can't cite anything that isn't in the evidence panel). The
    # stored word_count must equal the real count and fall within 150–225.
    # Why recount: HL-1 (a 60–90 s film) is enforced from this number. A stale
    # or made-up word_count would let a too-long script through.
    words = 0
    for seg in story["script"]["segments"]:
        words += len(seg["text"].split())
        if seg["chapter_id"] not in chapter_ids:
            errors.append(f"{seg['id']}: chapter_id {seg['chapter_id']} not in chapters")
        missing = [c for c in seg["claim_ids"] if c not in claim_ids]
        if missing:
            errors.append(f"{seg['id']}: claim_ids not found in chapters: {missing}")
    if words != story["script"]["word_count"]:
        errors.append(f"script.word_count {story['script']['word_count']} != real count {words}")
    if not 150 <= words <= 225:
        errors.append(f"script has {words} words; must be 150-225 (HL-1)")
    return errors


def event_errors(event: dict) -> list[str]:
    """Checks one build event against event.schema.json. Why: the web app's live
    progress page parses these directly, so a malformed event breaks it."""
    return _schema_errors("event", event)


class ContractError(ValueError):
    """Raised when a story breaks the contract. A separate type so callers can
    tell 'our output is invalid' apart from ordinary bugs."""


def assert_valid_story(story: dict) -> None:
    """Raises ContractError listing every problem. Called before every save
    (CLAUDE.md #4), so an invalid story never reaches disk or the player."""
    errors = story_errors(story)
    if errors:
        raise ContractError("story.json invalid:\n  " + "\n  ".join(errors))
