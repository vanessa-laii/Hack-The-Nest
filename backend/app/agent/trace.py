# Per-story call log (TR-10) and call budget (HL-10) for every model and search call.
# Inputs: a story folder; one record per call (kind, purpose, short summaries, timing, cost).
# Outputs: lines appended to <story_dir>/trace.jsonl; Budget raises BudgetExceeded when full.
#
# How it works:
#   1. Trace.log() turns one call into a JSON line: what kind of call, why, short summaries
#      of what went in and came out, how long it took, cache hit, estimated cost.
#   2. Summaries are cut to a few hundred characters, and keys that look secret are dropped,
#      so the log can never hold an API key or a header (CLAUDE.md #3).
#   3. Budget.spend(kind) is called BEFORE each call. If the call would go over the HL-10
#      limit it raises BudgetExceeded and the call is never made.
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

# HL-10: per story, at most this many calls of each kind.
BUDGET_LIMITS = {"exa": 15, "llm": 25, "vision": 6}

SUMMARY_MAX_CHARS = 300

# Any summary key containing one of these words is dropped before writing.
SECRET_WORDS = ("key", "token", "secret", "authorization", "header", "password", "cookie")


class BudgetExceeded(RuntimeError):
    """Raised before a call that would go over the story's HL-10 budget.
    The agent catches it and marks the remaining changes 'cause unknown'."""


# --- Helpers ---


def looks_secret(name: str) -> bool:
    """True if a summary key name suggests it holds a credential."""
    lowered = name.lower()
    for word in SECRET_WORDS:
        if word in lowered:
            return True
    return False


def shorten(value: object) -> object:
    """Cuts long strings so a trace line stays small and readable."""
    if isinstance(value, str) and len(value) > SUMMARY_MAX_CHARS:
        return value[:SUMMARY_MAX_CHARS] + "…"
    return value


def clean_summary(summary: dict | None) -> dict:
    """Copies a summary without secret-looking keys and with long text shortened.
    One level of nesting is cleaned too (e.g. a list of result titles)."""
    cleaned = {}
    for name, value in (summary or {}).items():
        if looks_secret(name):
            continue
        if isinstance(value, dict):
            value = clean_summary(value)
        elif isinstance(value, list):
            value = [shorten(item) for item in value[:20]]
        cleaned[name] = shorten(value)
    return cleaned


# --- Trace ---


class Trace:
    """Appends one JSON line per model or search call to <story_dir>/trace.jsonl (TR-10)."""

    def __init__(self, story_dir: Path | str):
        self.path = Path(story_dir) / "trace.jsonl"
        self._lock = threading.Lock()

    def log(
        self,
        kind: str,
        purpose: str,
        inputs: dict | None = None,
        outputs: dict | None = None,
        duration_s: float = 0.0,
        cache_hit: bool = False,
        cost_usd: float = 0.0,
        error: str | None = None,
    ) -> dict:
        """Writes one call record and returns it. `kind` is exa, llm or vision."""
        record = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "purpose": purpose,
            "inputs": clean_summary(inputs),
            "outputs": clean_summary(outputs),
            "duration_s": round(duration_s, 3),
            "cache_hit": cache_hit,
            "cost_usd_est": round(cost_usd, 5),
            "error": shorten(error),
        }
        line = json.dumps(record, ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record

    def read(self) -> list[dict]:
        """All records written so far (for tests and the generation summary)."""
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records


# --- Budget ---


class Budget:
    """Counts calls per story and refuses the one that would pass the HL-10 limit."""

    def __init__(self, limits: dict[str, int] | None = None):
        self.limits = dict(limits or BUDGET_LIMITS)
        self.used = {kind: 0 for kind in self.limits}
        self._lock = threading.Lock()

    def spend(self, kind: str) -> None:
        """Counts one call of `kind`, or raises BudgetExceeded without counting it."""
        if kind not in self.limits:
            raise ValueError(f"unknown budget kind: {kind}")
        with self._lock:
            if self.used[kind] + 1 > self.limits[kind]:
                limit = self.limits[kind]
                raise BudgetExceeded(f"{kind} budget used up ({limit} calls per story, HL-10)")
            self.used[kind] += 1

    def refund(self, kind: str) -> None:
        """Gives back one call of `kind`: for a request the provider refused before doing any
        work (a 429 rate limit). Rate limits come from the shared quota, not from this story,
        so they must not use up the story's HL-10 calls and turn real causes into unknowns."""
        with self._lock:
            self.used[kind] = max(self.used[kind] - 1, 0)

    def remaining(self, kind: str) -> int:
        """Calls of `kind` still allowed for this story."""
        return self.limits[kind] - self.used[kind]

    def usage(self) -> dict[str, int]:
        """Calls made so far per kind, for story.generation."""
        return dict(self.used)
