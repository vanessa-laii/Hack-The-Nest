# Exa web search for the cause agent, with a disk cache and a hard project-wide spending cap.
# Inputs: a query and optional year range; Trace + Budget; EXA_API_KEY; EXA_CACHED_ONLY.
# Outputs: search() → results {url, title, published_date, domain, text}; cache + ledger files.
#
# How it works:
#   1. The request (query with spaces tidied, years, result count, text cap) is hashed; the hash
#      names its cache file DATA_DIR/cache/exa/<sha256>.json.
#   2. A cached request is answered from disk: no budget, no money, traced as a cache hit.
#   3. In cached-only mode (EXA_CACHED_ONLY=1 or cached_only=True) a miss raises ExaNotCached,
#      so spikes and tests can never spend money by accident.
#   4. Before a live request: the story budget (HL-10) and the project ledger are checked. If the
#      ledger total plus this request would pass EXA_SPEND_CAP_USD, ExaSpendCapReached is raised
#      and nothing is sent. Otherwise the request is written to the ledger first, then sent.
#      The ledger's read-check-write holds a lock file (O_EXCL), so two processes (two
#      servers, a spike and a server) can never both pass the cap check.
#   5. exa-py is synchronous, so it runs in a worker thread (asyncio.to_thread).
#   6. Results are tidied (domain, ISO date, text capped at 20k chars), cached and traced.
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import app.net  # noqa: F401  (switches TLS to the OS certificate store, ADR-001)
from app.agent.trace import Budget, Trace
from app.config import settings

TEXT_MAX_CHARS = 20_000
EXA_TIMEOUT_S = 30

# Exa's changelog says $0.007 per search with contents. We count $0.01 per live request to
# leave room for pricing changes and extra results, so the ledger over-counts, never under.
EXA_COST_PER_REQUEST_USD = 0.01

# Owner's rule: total Exa spend for the whole project, demo included, never passes $20.
# We stop at $15: the $5 gap covers estimate error plus anything spent before this ledger.
EXA_SPEND_CAP_USD = 15.00

LEDGER_NAME = "ledger.json"

# One lock for the ledger file: two searches in parallel must not both pass the cap check.
# The thread lock covers this process; the lock file next to the ledger covers other ones.
_ledger_lock = threading.Lock()
LOCK_NAME = "ledger.json.lock"
LOCK_TIMEOUT_S = 10.0   # give up waiting and send nothing (fail closed)
LOCK_STALE_S = 30.0     # a lock file this old was left by a crashed process; the work under
                        # the lock takes milliseconds, so a live holder is never this slow
LOCK_POLL_S = 0.02


class ExaNotCached(RuntimeError):
    """Cached-only mode and this request is not in the cache, so nothing was sent."""


class ExaSpendCapReached(RuntimeError):
    """The project-wide Exa spending cap would be passed, so nothing was sent."""


class ExaSearchError(RuntimeError):
    """The live Exa request failed. The message never contains the key."""


class LedgerBusy(ExaSearchError):
    """The ledger lock could not be taken in time, so nothing was sent."""


# --- Step 1: cache key and files ---


def cache_dir() -> Path:
    """DATA_DIR/cache/exa, created on first use."""
    folder = settings.data_dir / "cache" / "exa"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def normalised_request(
    query: str, start_year: int | None, end_year: int | None, num_results: int,
) -> dict:
    """The request in a fixed shape, so the same search always gets the same cache key.
    Only runs of spaces are tidied; case is kept because Exa may treat it differently."""
    return {
        "query": " ".join(query.split()),
        "start_year": start_year,
        "end_year": end_year,
        "num_results": num_results,
        "text_max_chars": TEXT_MAX_CHARS,
    }


def request_key(request: dict) -> str:
    """sha256 of the normalised request; names its cache file."""
    canonical = json.dumps(request, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_json_atomic(path: Path, data: object) -> None:
    """Writes to a temporary file then renames it, so a crash never leaves half a file."""
    temporary = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def read_cache(key: str) -> list[dict] | None:
    """The cached results for this key, or None when the request was never made."""
    path = cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["results"]


def write_cache(key: str, request: dict, results: list[dict]) -> None:
    """Stores the request and its tidied results under the request's key."""
    record = {
        "request": request,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results": results,
    }
    write_json_atomic(cache_dir() / f"{key}.json", record)


# --- Step 4: spending ledger ---


def ledger_path() -> Path:
    """DATA_DIR/cache/exa/ledger.json: one entry per live Exa request ever sent."""
    return cache_dir() / LEDGER_NAME


def lock_path() -> Path:
    """DATA_DIR/cache/exa/ledger.json.lock: exists only while someone holds the ledger."""
    return cache_dir() / LOCK_NAME


def remove_if_stale(path: Path) -> None:
    """Deletes a lock file left behind by a process that died while holding it."""
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return
    if age > LOCK_STALE_S:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def take_file_lock(path: Path) -> None:
    """Creates the lock file with O_EXCL, which only one process can win, waiting up to
    LOCK_TIMEOUT_S. Raises LedgerBusy instead of touching the ledger unlocked."""
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            remove_if_stale(path)
            if time.monotonic() > deadline:
                raise LedgerBusy("the Exa ledger is locked by another process") from None
            time.sleep(LOCK_POLL_S)
            continue
        os.write(handle, str(os.getpid()).encode("ascii"))
        os.close(handle)
        return


@contextmanager
def ledger_locked():
    """Holds the ledger for this thread and process: the thread lock, then the lock file."""
    with _ledger_lock:
        path = lock_path()
        take_file_lock(path)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)


def read_ledger() -> dict:
    """The ledger contents; an empty ledger when the file does not exist yet."""
    path = ledger_path()
    if not path.exists():
        return {"entries": []}
    return json.loads(path.read_text(encoding="utf-8"))


def entry_cost(entry: dict) -> float:
    """What one request counts as: our estimate, or Exa's reported cost if that is higher."""
    estimate = entry.get("cost_usd_est", EXA_COST_PER_REQUEST_USD)
    reported = entry.get("cost_usd_reported") or 0.0
    return max(estimate, reported)


def ledger_total(ledger: dict) -> float:
    """Sum of every recorded request's cost."""
    total = 0.0
    for entry in ledger["entries"]:
        total += entry_cost(entry)
    return total


def spent_usd() -> float:
    """Estimated USD spent on live Exa requests so far, across the whole project."""
    with ledger_locked():
        return round(ledger_total(read_ledger()), 4)


def remaining_usd() -> float:
    """USD left before EXA_SPEND_CAP_USD."""
    return round(EXA_SPEND_CAP_USD - spent_usd(), 4)


def would_pass_cap(total: float, cost: float) -> bool:
    """True if spending `cost` on top of `total` goes over the cap (tiny float slack)."""
    return total + cost > EXA_SPEND_CAP_USD + 1e-9


def check_cap(cost: float = EXA_COST_PER_REQUEST_USD) -> None:
    """Raises ExaSpendCapReached if one more request of `cost` would pass the cap."""
    total = spent_usd()
    if would_pass_cap(total, cost):
        raise ExaSpendCapReached(
            f"Exa spend cap ${EXA_SPEND_CAP_USD:.2f} reached (spent ~${total:.2f})"
        )


def reserve_spend(key: str, cost: float = EXA_COST_PER_REQUEST_USD) -> int:
    """Checks the cap and records the request in the ledger BEFORE it is sent, under the lock.
    Recording first means a crash mid-request still counts it. Returns the entry's index."""
    with ledger_locked():
        ledger = read_ledger()
        total = ledger_total(ledger)
        if would_pass_cap(total, cost):
            raise ExaSpendCapReached(
                f"Exa spend cap ${EXA_SPEND_CAP_USD:.2f} reached (spent ~${total:.2f})"
            )
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query_hash": key,
            "cost_usd_est": cost,
        }
        ledger["entries"].append(entry)
        write_json_atomic(ledger_path(), ledger)
        return len(ledger["entries"]) - 1


def note_reported_cost(index: int, reported: float | None) -> None:
    """Adds Exa's own reported cost to a ledger entry, so the higher figure counts."""
    if reported is None:
        return
    with ledger_locked():
        ledger = read_ledger()
        ledger["entries"][index]["cost_usd_reported"] = round(float(reported), 5)
        write_json_atomic(ledger_path(), ledger)


# --- Step 5: live request ---


def year_bounds(start_year: int | None, end_year: int | None) -> dict:
    """Exa's published-date filters for a year range (whole years)."""
    bounds = {}
    if start_year is not None:
        bounds["start_published_date"] = f"{start_year}-01-01"
    if end_year is not None:
        bounds["end_published_date"] = f"{end_year}-12-31"
    return bounds


def call_exa(request: dict) -> tuple[list, float | None]:
    """Runs one synchronous Exa search with page text. Returns (raw results, reported cost)."""
    from exa_py import Exa  # imported here so tests and cached runs never need the client

    api_key = settings.exa_api_key.get_secret_value() if settings.exa_api_key else None
    if not api_key:
        raise ExaSearchError("EXA_API_KEY is not set")
    client = Exa(api_key=api_key, timeout=EXA_TIMEOUT_S)
    response = client.search(
        request["query"],
        num_results=request["num_results"],
        contents={"text": {"max_characters": request["text_max_chars"]}},
        **year_bounds(request["start_year"], request["end_year"]),
    )
    reported = None
    cost = getattr(response, "cost_dollars", None)
    if cost is not None:
        reported = getattr(cost, "total", None)
    return list(response.results), reported


def redact(text: str) -> str:
    """Removes the Exa key from an error message, in case a library ever echoes it."""
    if settings.exa_api_key and settings.exa_api_key.get_secret_value():
        text = text.replace(settings.exa_api_key.get_secret_value(), "[redacted]")
    return text[:300]


# --- Step 6: tidy results ---


def domain_of(url: str) -> str:
    """The host name without 'www.', lower case."""
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def iso_date(value: object) -> str | None:
    """'2019-05-01T00:00:00.000Z' → '2019-05-01'; anything unreadable → None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def tidy_result(raw: object) -> dict:
    """One Exa result in our shape, text capped at TEXT_MAX_CHARS."""
    url = getattr(raw, "url", "") or ""
    text = getattr(raw, "text", None) or ""
    return {
        "url": url,
        "title": getattr(raw, "title", None) or "",
        "published_date": iso_date(getattr(raw, "published_date", None)),
        "domain": domain_of(url),
        "text": text[:TEXT_MAX_CHARS],
    }


# --- Public ---


def cached_only_mode(cached_only: bool) -> bool:
    """Cached-only if the caller asks or the setting/env var says so. The env var is read at
    call time too, so a spike that sets it after import is still protected."""
    from_env = os.environ.get("EXA_CACHED_ONLY", "").strip() == "1"
    return cached_only or settings.exa_cached_only or from_env


def trace_inputs(request: dict, key: str) -> dict:
    """The request summary written to trace.jsonl."""
    return {
        "query": request["query"],
        "start_year": request["start_year"],
        "end_year": request["end_year"],
        "num_results": request["num_results"],
        "cache_key": key[:16],
    }


def trace_outputs(results: list[dict]) -> dict:
    """The result summary written to trace.jsonl."""
    domains = []
    for result in results:
        domains.append(result["domain"])
    return {"count": len(results), "domains": domains}


async def search(
    query: str,
    *,
    start_year: int | None = None,
    end_year: int | None = None,
    num_results: int = 8,
    trace: Trace,
    budget: Budget,
    cached_only: bool = False,
    purpose: str = "cause search",
) -> list[dict]:
    """Searches the web with Exa (cache first). Each result has url, title, published_date,
    domain and text. Raises ExaNotCached, ExaSpendCapReached or BudgetExceeded instead of
    sending a request it must not send."""
    request = normalised_request(query, start_year, end_year, num_results)
    key = request_key(request)
    inputs = trace_inputs(request, key)

    cached = read_cache(key)
    if cached is not None:
        trace.log("exa", purpose, inputs, trace_outputs(cached), cache_hit=True)
        return cached
    if cached_only_mode(cached_only):
        raise ExaNotCached(f"not in the Exa cache (cached-only mode): {request['query']!r}")

    check_cap()  # read-only check first, so a full cap doesn't use story budget
    budget.spend("exa")  # HL-10, raises BudgetExceeded before anything is sent
    index = reserve_spend(key)  # re-checks the cap under the lock and records the request

    started = time.monotonic()
    try:
        raw_results, reported = await asyncio.to_thread(call_exa, request)
    except Exception as exc:
        elapsed = time.monotonic() - started
        message = f"{type(exc).__name__}: {redact(str(exc))}"
        trace.log("exa", purpose, inputs, duration_s=elapsed,
                  cost_usd=EXA_COST_PER_REQUEST_USD, error=message)
        raise ExaSearchError(message) from None
    elapsed = time.monotonic() - started
    note_reported_cost(index, reported)

    results = []
    for raw in raw_results:
        results.append(tidy_result(raw))
    write_cache(key, request, results)
    cost = max(EXA_COST_PER_REQUEST_USD, reported or 0.0)
    trace.log("exa", purpose, inputs, trace_outputs(results), elapsed, cost_usd=cost)
    return results
