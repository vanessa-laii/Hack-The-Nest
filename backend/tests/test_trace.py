# Tests for app/agent/trace.py: the trace.jsonl log (TR-10) and per-story budgets (HL-10).
# Inputs: a temporary story folder.
# Outputs: pytest pass/fail.
import pytest

from app.agent.trace import BUDGET_LIMITS, Budget, BudgetExceeded, Trace


def test_trace_appends_one_line_per_call(tmp_path):
    trace = Trace(tmp_path)
    trace.log("exa", "cause search", {"query": "Lake Mead drought"}, {"count": 3}, 1.234)
    trace.log("llm", "pick_excerpts", {"prompt_chars": 900}, cache_hit=False, cost_usd=0.0001)
    records = trace.read()
    assert len(records) == 2
    assert records[0]["kind"] == "exa"
    assert records[0]["duration_s"] == 1.234
    assert records[1]["purpose"] == "pick_excerpts"
    assert (tmp_path / "trace.jsonl").read_text(encoding="utf-8").count("\n") == 2


def test_trace_drops_secret_looking_fields(tmp_path):
    trace = Trace(tmp_path)
    inputs = {"api_key": "sk-secret", "headers": {"Authorization": "x"}, "query": "q"}
    trace.log("llm", "test", inputs)
    text = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert "sk-secret" not in text
    assert "Authorization" not in text
    assert '"query": "q"' in text


def test_trace_shortens_long_text(tmp_path):
    trace = Trace(tmp_path)
    record = trace.log("llm", "test", outputs={"reply": "x" * 5000})
    assert len(record["outputs"]["reply"]) < 400


def test_budget_limits_match_hl10():
    assert BUDGET_LIMITS == {"exa": 15, "llm": 25, "vision": 6}


def test_budget_raises_before_going_over():
    budget = Budget({"exa": 2, "llm": 1, "vision": 1})
    budget.spend("exa")
    budget.spend("exa")
    with pytest.raises(BudgetExceeded):
        budget.spend("exa")
    assert budget.usage()["exa"] == 2  # the refused call was not counted
    assert budget.remaining("llm") == 1


def test_budget_unknown_kind():
    with pytest.raises(ValueError):
        Budget().spend("tts")
