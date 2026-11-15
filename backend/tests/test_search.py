# Tests for app/agent/search.py with exa-py mocked: cache, cached-only mode, budget, spend cap.
# Inputs: a temporary DATA_DIR and a fake call_exa.
# Outputs: pytest pass/fail. No network and no money.
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import search as search_module
from app.agent.search import (
    EXA_COST_PER_REQUEST_USD,
    EXA_SPEND_CAP_USD,
    ExaNotCached,
    ExaSpendCapReached,
    remaining_usd,
    spent_usd,
)
from app.agent.trace import Budget, BudgetExceeded, Trace
from app.config import settings


def raw_result(n):
    """A fake exa-py Result."""
    return SimpleNamespace(
        url=f"https://www.example{n}.com/page",
        title=f"Title {n}",
        published_date="2019-05-01T00:00:00.000Z",
        text="x" * 30_000,
    )


@pytest.fixture
def fake_exa(tmp_path, monkeypatch):
    """Points DATA_DIR at a temp folder and replaces the live Exa call with a counter."""
    monkeypatch.setattr(settings, "data_dir_raw", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "exa_cached_only", False)
    monkeypatch.delenv("EXA_CACHED_ONLY", raising=False)
    calls = []

    def fake_call(request):
        calls.append(request)
        return [raw_result(1), raw_result(2)], 0.007

    monkeypatch.setattr(search_module, "call_exa", fake_call)
    return calls, Trace(tmp_path / "story")


def do_search(query, trace, budget=None, **kwargs):
    """Runs search() synchronously."""
    budget = budget or Budget()
    return asyncio.run(search_module.search(query, trace=trace, budget=budget, **kwargs))


def test_live_search_tidies_and_caches(fake_exa):
    calls, trace = fake_exa
    results = do_search("Lake Mead drought", trace, start_year=1999, end_year=2011)
    assert len(results) == 2
    first = results[0]
    assert first["domain"] == "example1.com"
    assert first["published_date"] == "2019-05-01"
    assert len(first["text"]) == 20_000
    assert calls[0]["start_year"] == 1999
    assert search_module.year_bounds(1999, 2011) == {
        "start_published_date": "1999-01-01", "end_published_date": "2011-12-31",
    }
    assert trace.read()[0]["cache_hit"] is False


def test_cache_hit_is_free(fake_exa):
    calls, trace = fake_exa
    do_search("Lake Mead drought", trace)
    budget = Budget()
    spent_before = spent_usd()
    results = do_search("  Lake  Mead   drought ", trace, budget)  # same after tidying spaces
    assert len(results) == 2
    assert len(calls) == 1
    assert budget.usage()["exa"] == 0
    assert spent_usd() == spent_before
    assert trace.read()[-1]["cache_hit"] is True


def test_cached_only_raises_on_miss(fake_exa, monkeypatch):
    calls, trace = fake_exa
    with pytest.raises(ExaNotCached):
        do_search("never asked", trace, cached_only=True)
    monkeypatch.setenv("EXA_CACHED_ONLY", "1")
    with pytest.raises(ExaNotCached):
        do_search("never asked", trace)
    assert calls == []
    assert spent_usd() == 0


def test_cached_only_still_answers_hits(fake_exa, monkeypatch):
    calls, trace = fake_exa
    do_search("Paradise Camp Fire", trace)
    monkeypatch.setenv("EXA_CACHED_ONLY", "1")
    assert len(do_search("Paradise Camp Fire", trace)) == 2
    assert len(calls) == 1


def test_story_budget_stops_live_calls(fake_exa):
    calls, trace = fake_exa
    budget = Budget({"exa": 1, "llm": 1, "vision": 1})
    do_search("one", trace, budget)
    with pytest.raises(BudgetExceeded):
        do_search("two", trace, budget)
    assert len(calls) == 1


def test_ledger_records_each_live_request(fake_exa):
    calls, trace = fake_exa
    do_search("one", trace)
    do_search("two", trace)
    ledger = search_module.read_ledger()
    assert len(ledger["entries"]) == 2
    entry = ledger["entries"][0]
    assert entry["cost_usd_est"] == EXA_COST_PER_REQUEST_USD
    assert entry["cost_usd_reported"] == 0.007
    assert len(entry["query_hash"]) == 64
    assert spent_usd() == pytest.approx(0.02)


def test_cap_blocks_the_request_that_would_cross_it(fake_exa):
    calls, trace = fake_exa
    # Fill the ledger to just under the cap: one more request would pass it.
    almost_full = EXA_SPEND_CAP_USD - EXA_COST_PER_REQUEST_USD / 2
    entry = {"at": "2026-11-15T00:00:00+00:00", "query_hash": "old", "cost_usd_est": almost_full}
    search_module.write_json_atomic(search_module.ledger_path(), {"entries": [entry]})
    budget = Budget()
    with pytest.raises(ExaSpendCapReached):
        do_search("one too many", trace, budget)
    assert calls == []
    assert budget.usage()["exa"] == 0  # refused before the story budget was touched
    assert len(search_module.read_ledger()["entries"]) == 1


def test_cap_allows_the_request_that_lands_exactly_on_it(fake_exa):
    calls, trace = fake_exa
    exactly = EXA_SPEND_CAP_USD - EXA_COST_PER_REQUEST_USD
    entry = {"at": "2026-11-15T00:00:00+00:00", "query_hash": "old", "cost_usd_est": exactly}
    search_module.write_json_atomic(search_module.ledger_path(), {"entries": [entry]})
    do_search("last one", trace)
    assert len(calls) == 1
    assert remaining_usd() == pytest.approx(0.0)
    with pytest.raises(ExaSpendCapReached):
        do_search("over", trace)


def test_cap_still_allows_cached_hits(fake_exa):
    calls, trace = fake_exa
    do_search("cached", trace)
    full = {"at": "x", "query_hash": "old", "cost_usd_est": EXA_SPEND_CAP_USD}
    ledger = search_module.read_ledger()
    ledger["entries"].append(full)
    search_module.write_json_atomic(search_module.ledger_path(), ledger)
    assert len(do_search("cached", trace)) == 2


def test_ledger_survives_a_reload(fake_exa):
    calls, trace = fake_exa
    do_search("one", trace)
    # A fresh Python process (like a server restart) must read the same total from disk.
    backend_dir = Path(__file__).resolve().parents[1]
    code = "from app.agent.search import spent_usd; print(spent_usd())"
    env = {**os.environ, "DATA_DIR": str(settings.data_dir)}
    finished = subprocess.run(
        [sys.executable, "-c", code], cwd=backend_dir, env=env,
        capture_output=True, text=True, check=True,
    )
    assert float(finished.stdout.strip()) == pytest.approx(EXA_COST_PER_REQUEST_USD)


# --- Cross-process ledger lock ---

RESERVE_LOOP = (
    "import sys\n"
    "from app.agent.search import reserve_spend\n"
    "for i in range(int(sys.argv[1])):\n"
    "    reserve_spend(f'{sys.argv[2]}-{i}', cost=0.0001)\n"
)


def test_two_processes_never_lose_a_ledger_entry(fake_exa):
    """Two processes reserving at once: every entry lands, none overwritten (file lock).
    Nothing is sent to Exa: reserve_spend only writes the ledger."""
    backend_dir = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATA_DIR": str(settings.data_dir)}
    per_process = 25
    workers = []
    for name in ("a", "b"):
        workers.append(subprocess.Popen(
            [sys.executable, "-c", RESERVE_LOOP, str(per_process), name],
            cwd=backend_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ))
    for worker in workers:
        _, errors = worker.communicate(timeout=60)
        assert worker.returncode == 0, errors.decode("utf-8", "replace")
    hashes = [entry["query_hash"] for entry in search_module.read_ledger()["entries"]]
    assert len(hashes) == 2 * per_process
    assert len(set(hashes)) == 2 * per_process
    assert not search_module.lock_path().exists()


def test_threads_never_lose_a_ledger_entry(fake_exa):
    """Threads in one process share the thread lock and the lock file."""
    import threading

    def reserve_many(name):
        for i in range(20):
            search_module.reserve_spend(f"{name}-{i}", cost=0.0001)

    threads = [threading.Thread(target=reserve_many, args=(f"t{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(search_module.read_ledger()["entries"]) == 80


def test_stale_lock_from_a_crashed_process_is_cleared(fake_exa):
    """A lock file older than LOCK_STALE_S is removed, so a crash can't block spending."""
    lock = search_module.lock_path()
    lock.write_text("12345", encoding="ascii")
    old = lock.stat().st_mtime - search_module.LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    search_module.reserve_spend("after-crash")
    assert len(search_module.read_ledger()["entries"]) == 1
    assert not lock.exists()


def test_held_lock_times_out_and_sends_nothing(fake_exa, monkeypatch):
    """A fresh lock held by someone else: give up after the timeout, never write unlocked."""
    calls, trace = fake_exa
    monkeypatch.setattr(search_module, "LOCK_TIMEOUT_S", 0.2)
    search_module.lock_path().write_text("12345", encoding="ascii")
    with pytest.raises(search_module.LedgerBusy):
        search_module.reserve_spend("blocked")
    with pytest.raises(search_module.ExaSearchError):   # LedgerBusy is an ExaSearchError
        do_search("blocked", trace)
    assert calls == []
    search_module.lock_path().unlink()
    assert search_module.read_ledger()["entries"] == []


def test_higher_reported_cost_counts(fake_exa, monkeypatch):
    calls, trace = fake_exa
    monkeypatch.setattr(search_module, "call_exa", lambda request: ([raw_result(1)], 0.05))
    do_search("expensive", trace)
    assert spent_usd() == pytest.approx(0.05)


def test_failed_live_request_still_counts_and_hides_key(fake_exa, monkeypatch):
    calls, trace = fake_exa
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "exa_api_key", SecretStr("exa-secret-123"))

    def broken(request):
        raise RuntimeError("401 for key exa-secret-123")

    monkeypatch.setattr(search_module, "call_exa", broken)
    with pytest.raises(search_module.ExaSearchError) as caught:
        do_search("fails", trace)
    assert "exa-secret-123" not in str(caught.value)
    assert "exa-secret-123" not in trace.path.read_text(encoding="utf-8")
    assert spent_usd() == pytest.approx(EXA_COST_PER_REQUEST_USD)
