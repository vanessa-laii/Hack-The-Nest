# Tests for app/agent/causes.py: quotes verified in code, the claim rules, budgets, confidence, ids.
# Inputs: fake Exa pages and a fake text model (monkeypatched); no network, no Exa spend.
# Outputs: pytest results.
#
# How it works:
#   1. search.search is replaced by a fake that returns hand-written pages per query, so no test
#      can reach Exa. llm.complete_json is replaced by a fake that returns scripted quotes
#      (extract_quotes), claims (compose_claims), checker verdicts (check_claims) and rewrites
#      (rewrite_claims), and spends the budget like the real one.
#   2. Each test scripts what the "model" says and checks what survives the code checks.
import asyncio
import re

import pytest

from app import llm
from app.agent import causes
from app.agent import search as exa_search
from app.agent.trace import Budget, BudgetExceeded, Trace
from app.contract_check import event_errors

PLACE = {
    "query": "Lake Mead, Nevada",
    "display_name": "Lake Mead, Nevada",
    "lat": 36.2, "lon": -114.4, "bbox": [-114.45, 36.18, -114.37, 36.24], "aoi_km": 6.0,
    "admin": {"city": None, "county": "Clark County", "state": "Nevada",
              "country": "United States", "country_code": "us"},
}


def _change(number: int, start: int, end: int, kind: str = "water_loss") -> dict:
    """One schema-1.3 change covering most of the area."""
    return {
        "id": f"chg-{number:02d}", "kind": kind, "metric": "water_frac",
        "start_year": start, "end_year": end, "magnitude": -0.2, "scope": "place",
        "area_ha": 800.0, "persistence_years": 5,
        "region": {"bbox": [-114.45, 36.18, -114.37, 36.24],
                   "description": "in the east part of the area"},
        "before_year": start, "after_year": end, "mask_url": None,
        "sensor_switch_flag": False,
    }


DROUGHT = ("Since drought took hold on the Colorado in 1999, the surface of Lake Mead has "
           "plunged almost 130 feet by 2010.")
RAMPS = "In 2009 thirty-three boat ramps at Lake Mead were closed as the water fell."
IMAGES = "Images of Lake Mead were acquired on July 21, 2009, by the Landsat 5 satellite."
FLOOD = "Heavy snow in 1983 filled Lake Mead to its highest level ever recorded in 1983."


def _page(url: str, domain: str, text: str, published: str = "2010-08-01") -> dict:
    """One search result in search.py's shape."""
    return {"url": url, "title": "Lake Mead story", "published_date": published,
            "domain": domain, "text": "Menu. Home. " + text + " Share this page."}


GOV_PAGE = _page("https://usbr.gov/lake-mead", "usbr.gov", DROUGHT + " " + RAMPS)
NEWS_PAGE = _page("https://news-one.com/mead", "news-one.com", DROUGHT + " " + IMAGES)
OTHER_NEWS = _page("https://paper-two.com/mead", "paper-two.com", DROUGHT)
FLOOD_PAGE = _page("https://paper-two.com/flood", "paper-two.com", FLOOD, "1984-02-01")


class FakeModel:
    """Stands in for llm.complete_json. `quotes`, `claims` and `rewrites` map change start year
    to what the model returns for that change. The checker says "yes" to every claim except
    those whose text is in `rejected` (text -> reason). `failing` maps a prompt name to an
    exception it raises. Each call spends the budget exactly like the real one."""

    def __init__(self, quotes: dict, claims: dict, rewrites: dict | None = None,
                 rejected: dict | None = None, failing: dict | None = None):
        self.quotes = quotes
        self.claims = claims
        self.rewrites = rewrites or {}
        self.rejected = rejected or {}
        self.failing = failing or {}
        self.calls = []

    def verdicts(self, claims_text: str) -> list[dict]:
        """A verdict per "cN: text" line of the checker's claims block."""
        verdicts = []
        for line in claims_text.splitlines():
            match = re.match(r"c(\d+): (.*)", line)
            if match is None:
                continue
            text = match.group(2)
            if text in self.rejected:
                verdicts.append({"claim": f"c{match.group(1)}", "reason": self.rejected[text],
                                 "supported": "no"})
            else:
                verdicts.append({"claim": f"c{match.group(1)}", "reason": "All stated.",
                                 "supported": "yes"})
        return verdicts

    async def __call__(self, prompt_name, variables, schema, *, trace, budget,
                       temperature=0, untrusted=()):
        budget.spend("llm")
        self.calls.append((prompt_name, variables))
        if prompt_name in self.failing:
            raise self.failing[prompt_name]
        year = variables["start_year"]
        if prompt_name == "extract_quotes":
            return {"quotes": self.quotes.get(year, [])}
        if prompt_name == "check_claims":
            return {"verdicts": self.verdicts(variables["claims"])}
        if prompt_name == "rewrite_claims":
            return {"claims": self.rewrites.get(year, [])}
        return {"claims": self.claims.get(year, [])}

    def prompts(self) -> list[str]:
        """The prompt names called, in order."""
        names = []
        for name, _ in self.calls:
            names.append(name)
        return names


def _install(monkeypatch, pages_by_year: dict, model: FakeModel) -> list:
    """Swaps in the fake search and model. Returns the list of searched queries."""
    searched = []

    async def fake_search(query, *, start_year=None, end_year=None, num_results=8, trace,
                          budget, cached_only=False, purpose=""):
        searched.append(query)
        return pages_by_year.get(start_year + 1, [])  # query years are widened by one

    monkeypatch.setattr(exa_search, "search", fake_search)
    monkeypatch.setattr(llm, "complete_json", model)
    return searched


def _run(tmp_path, changes, budget=None, emit=None) -> dict:
    """find_causes on PLACE with a fresh trace and (by default) the real HL-10 budget."""
    return asyncio.run(causes.find_causes(
        tmp_path, PLACE, changes, trace=Trace(tmp_path), budget=budget or Budget(), emit=emit,
    ))


def _excerpt_texts(answer: dict) -> list[str]:
    """Every excerpt text in the answer's sources."""
    texts = []
    for source in answer["sources"]:
        for excerpt in source["excerpts"]:
            texts.append(excerpt["text"])
    return texts


def _claim_texts(answer: dict, change_id: str) -> list[str]:
    """The claim texts kept for one change."""
    texts = []
    for claim in answer["chapters"][change_id]["claims"]:
        texts.append(claim["text"])
    return texts


# --- Query template ---


def test_queries_match_the_recorded_template():
    """The exact strings spikes/s4-causes/record.py recorded, so they are cache hits."""
    planned = causes.plan_queries(PLACE, _change(1, 1999, 2010))
    assert [query["query"] for query in planned] == [
        "Lake Mead, Nevada water area shrank 1999-2010",
        "What happened to the water and shoreline in Lake Mead, Nevada between 1999 and 2010?",
    ]
    assert planned[0]["start_year"] == 1998
    assert planned[0]["end_year"] == 2011


# --- TR-2: quotes are checked word for word ---


def test_fabricated_quote_is_dropped(monkeypatch, tmp_path):
    """A quote the model made up never reaches the compose step or the story."""
    made_up = "Lake Mead fell 500 feet in 2005 because of a secret canal."
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": made_up}, {"page": "P1", "text": DROUGHT}]},
        claims={2000: [{"text": "The surface of Lake Mead plunged almost 130 feet by 2010.",
                        "quote_ids": ["q1"]}]},
    )
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])

    compose_inputs = [variables for name, variables in model.calls if name == "compose_claims"]
    assert made_up not in compose_inputs[0]["quotes"]
    assert made_up not in _excerpt_texts(answer)
    assert _claim_texts(answer, "chg-01") == [
        "The surface of Lake Mead plunged almost 130 feet by 2010."]


def test_only_fabricated_quotes_leave_cause_unknown_without_compose(monkeypatch, tmp_path):
    """No verified quote means no compose call and a cause_unknown chapter (TR-8)."""
    model = FakeModel(quotes={2000: [{"page": "P1", "text": "Lake Mead was drained in 2004."}]},
                      claims={})
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert answer["chapters"]["chg-01"] == {"confidence": "cause_unknown", "claims": []}
    assert answer["sources"] == []
    assert [name for name, _ in model.calls] == ["extract_quotes"]


# --- ADR-004 rule 1: numbers come from the claim's own quotes ---


def test_claim_with_a_number_not_in_its_quotes_is_dropped(monkeypatch, tmp_path):
    """150 feet, and a year the quote doesn't state, are dropped; the true claim is kept."""
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": DROUGHT}]},
        claims={2000: [
            {"text": "The surface of Lake Mead plunged 150 feet.", "quote_ids": ["q1"]},
            {"text": "The surface of Lake Mead began to fall in 2001.", "quote_ids": ["q1"]},
            {"text": "Since 1999 drought, Lake Mead plunged almost 130 feet.",
             "quote_ids": ["q1"]},
        ]},
    )
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert _claim_texts(answer, "chg-01") == [
        "Since 1999 drought, Lake Mead plunged almost 130 feet."]


def test_change_years_are_not_a_free_pass_for_numbers():
    """The story's own years don't count: a claim must not date what its quotes don't."""
    assert not causes.numbers_supported("Lake Mead fell in 2000.", [DROUGHT])
    assert causes.numbers_supported("Lake Mead fell after 1999.", [DROUGHT])


def test_written_numbers_count_both_ways():
    """'thirty-three' and '33' are the same number; 'thirty-four' is not."""
    assert causes.numbers_supported("33 boat ramps were closed in 2009.", [RAMPS])
    assert causes.numbers_supported("Thirty-three boat ramps closed in 2009.", [RAMPS])
    assert not causes.numbers_supported("Thirty-four boat ramps closed in 2009.", [RAMPS])
    assert causes.numbers_supported("It covers 1.5 million acres.", ["about 1,500,000 acres"])
    assert not causes.numbers_supported("It covers 1.5 billion acres.", ["1.5 million acres"])
    assert not causes.numbers_supported("Thousands of homes burned.", ["hundreds of homes"])
    assert not causes.numbers_supported("The first ramp closed.", ["A ramp closed."])


def test_number_parser_reads_common_forms():
    """Digits with commas, decimals, ranges, decades, ordinals and compound words."""
    found = causes.numbers_in("1,200 acres; 3.50 m; 1999–2010; the 1990s; 2nd; "
                              "two hundred and fifty; one and two")
    assert {"1200", "3.5", "1999", "2010", "1990", "ordinal 2", "250", "1", "2"} <= found
    assert "3" not in found  # "one and two" is two numbers, not 3


# --- ADR-004 rule 2: claims about the imagery or the page are dropped ---


def test_meta_image_acquired_claim_is_dropped(monkeypatch, tmp_path):
    """A true claim about when pictures were taken says nothing about the ground."""
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": IMAGES}, {"page": "P1", "text": DROUGHT}]},
        claims={2000: [
            {"text": "Images of Lake Mead were acquired on July 21, 2009, by Landsat 5.",
             "quote_ids": ["q1"]},
            {"text": "The surface of Lake Mead plunged almost 130 feet by 2010.",
             "quote_ids": ["q2"]},
        ]},
    )
    _install(monkeypatch, {2000: [NEWS_PAGE]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert _claim_texts(answer, "chg-01") == [
        "The surface of Lake Mead plunged almost 130 feet by 2010."]


def test_meta_word_list():
    """Image and page words are meta; buying land and a construction site are not."""
    assert causes.is_meta_claim("The top image, acquired in October 2002, shows the island.")
    assert causes.is_meta_claim("This article describes the drought at Lake Mead.")
    assert causes.is_meta_claim("Satellite photos show the shoreline in 2010.")
    assert not causes.is_meta_claim("The city acquired the site in 1995 and cleared it.")
    assert not causes.is_meta_claim("Crews cleared the construction site in 2016.")


# --- Budgets and failures leave cause_unknown, never crash ---


def test_budget_exceeded_leaves_cause_unknown(monkeypatch, tmp_path):
    """With no LLM calls left, every change stays cause_unknown with a warning."""
    model = FakeModel(quotes={2000: [{"page": "P1", "text": DROUGHT}]}, claims={})
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    budget = Budget({"exa": 15, "llm": 0, "vision": 6})
    answer = _run(tmp_path, [_change(1, 2000, 2010)], budget=budget)
    assert answer["chapters"]["chg-01"] == {"confidence": "cause_unknown", "claims": []}
    assert answer["sources"] == []
    assert len(answer["warnings"]) == 1
    assert "HL-10" in answer["warnings"][0]


@pytest.mark.parametrize("error", [
    BudgetExceeded("exa budget used up"),
    exa_search.ExaSpendCapReached("cap"),
    exa_search.ExaNotCached("not cached"),
    llm.LLMError("model failed"),
])
def test_search_or_model_failure_leaves_cause_unknown(monkeypatch, tmp_path, error):
    """The failing change stays unknown with a warning; the other change still gets causes."""
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": DROUGHT}]},
        claims={2000: [{"text": "Lake Mead plunged almost 130 feet by 2010.",
                        "quote_ids": ["q1"]}]},
    )
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    real_search = exa_search.search

    async def failing_for_1983(query, **kwargs):
        if kwargs["start_year"] == 1982:
            raise error
        return await real_search(query, **kwargs)

    monkeypatch.setattr(exa_search, "search", failing_for_1983)
    answer = _run(tmp_path, [_change(1, 2000, 2010), _change(2, 1983, 1990, "water_gain")])
    assert answer["chapters"]["chg-02"] == {"confidence": "cause_unknown", "claims": []}
    assert answer["chapters"]["chg-01"]["confidence"] == "confirmed"
    assert len(answer["warnings"]) == 1
    assert answer["warnings"][0].startswith("chg-02:")


# --- TR-7: confidence by rule ---


@pytest.mark.parametrize("pages, expected", [
    ([GOV_PAGE], "confirmed"),                    # one authoritative (.gov) source
    ([NEWS_PAGE], "likely"),                      # one ordinary site
    ([NEWS_PAGE, OTHER_NEWS], "confirmed"),       # two independent sites
])
def test_confidence_follows_the_rule(monkeypatch, tmp_path, pages, expected):
    """Confidence comes from verify.confidence over the cited sources, never the model."""
    quotes = []
    quote_ids = []
    for number in range(1, len(pages) + 1):
        quotes.append({"page": f"P{number}", "text": DROUGHT})
        quote_ids.append(f"q{number}")
    model = FakeModel(
        quotes={2000: quotes},
        claims={2000: [{"text": "Lake Mead plunged almost 130 feet by 2010.",
                        "quote_ids": quote_ids}]},
    )
    _install(monkeypatch, {2000: pages}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert answer["chapters"]["chg-01"]["confidence"] == expected
    for source in answer["sources"]:
        assert source["authority"] in ("authoritative", "other")


# --- Ids, usage and events ---


def test_ids_are_unique_across_the_story(monkeypatch, tmp_path):
    """cl-, src- and ex- ids never repeat, and claims are numbered in story (year) order."""
    model = FakeModel(
        quotes={
            2000: [{"page": "P1", "text": DROUGHT}, {"page": "P1", "text": RAMPS}],
            1983: [{"page": "P1", "text": FLOOD}],
        },
        claims={
            2000: [
                {"text": "Lake Mead plunged almost 130 feet by 2010.", "quote_ids": ["q1"]},
                {"text": "In 2009, 33 boat ramps at Lake Mead were closed.",
                 "quote_ids": ["q2"]},
            ],
            1983: [{"text": "Heavy snow in 1983 filled Lake Mead to its highest level.",
                    "quote_ids": ["q1"]}],
        },
    )
    _install(monkeypatch, {2000: [GOV_PAGE], 1983: [FLOOD_PAGE]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010), _change(2, 1983, 1990, "water_gain")])

    assert [c["id"] for c in answer["chapters"]["chg-02"]["claims"]] == ["cl-01"]
    assert [c["id"] for c in answer["chapters"]["chg-01"]["claims"]] == ["cl-02", "cl-03"]
    source_ids = [source["id"] for source in answer["sources"]]
    excerpt_ids = []
    for source in answer["sources"]:
        for excerpt in source["excerpts"]:
            excerpt_ids.append(excerpt["id"])
            assert excerpt["verified"] is True
    assert len(source_ids) == len(set(source_ids)) == 2
    assert len(excerpt_ids) == len(set(excerpt_ids)) == 3
    assert answer["usage"] == {"exa_calls": 0, "llm_calls": 6}   # extract, compose, check x2
    assert answer["warnings"] == []


def test_events_fit_the_event_contract(monkeypatch, tmp_path):
    """research and verify progress events validate against event.schema.json."""
    model = FakeModel(quotes={2000: [{"page": "P1", "text": DROUGHT}]}, claims={})
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    events = []

    async def emit(type_, message, *, stage, status, progress=None):
        events.append({"type": type_, "story_id": "lake-mead-nevada-us-6km",
                       "seq": len(events), "ts": "2026-11-15T00:00:00Z", "message": message,
                       "stage": stage, "status": status})

    _run(tmp_path, [_change(1, 2000, 2010)], emit=emit)
    assert {event["stage"] for event in events} == {"research", "verify"}
    for event in events:
        assert event_errors(event) == []


def test_page_text_is_sent_as_untrusted(monkeypatch, tmp_path):
    """TR-9: the real complete_json is asked to wrap the page text."""
    seen = {}

    async def spy(prompt_name, variables, schema, *, trace, budget, temperature=0,
                  untrusted=()):
        seen[prompt_name] = set(untrusted)
        return {"quotes": []}

    _install(monkeypatch, {2000: [GOV_PAGE]}, FakeModel({}, {}))
    monkeypatch.setattr(llm, "complete_json", spy)
    _run(tmp_path, [_change(1, 2000, 2010)])
    assert seen["extract_quotes"] == {"pages"}


def test_prompts_exist_in_the_prompts_folder():
    """llm.load_prompt reads app/agent/prompts/, so the promoted prompts must live there."""
    assert "{pages}" in llm.load_prompt("extract_quotes")
    assert "{quotes}" in llm.load_prompt("compose_claims")
    assert "{claims}" in llm.load_prompt("check_claims")
    assert "{problems}" in llm.load_prompt("rewrite_claims")


def test_compose_and_rewrite_ask_for_what_changed_the_land_not_background():
    """A claim must be a cause or event that changed the land or water, not a background
    fact about the place (e.g. its population). This is a prompt rule, not a code check."""
    for name in ("compose_claims", "rewrite_claims"):
        prompt = llm.load_prompt(name)
        assert "what happened to the land or water" in prompt
        assert "Background facts about the place" in prompt
        assert "leave them out" in prompt


# --- TR-5: the separate checker call ---

KEPT = "The surface of Lake Mead plunged almost 130 feet by 2010."
STRETCHED = "Drought caused Lake Mead to plunge almost 130 feet by 2010."


def test_checker_runs_once_per_change_and_drops_a_no(monkeypatch, tmp_path):
    """One check_claims call batches the change's claims; a "no" drops that claim only, and
    its reason is kept for the trace and the scorer."""
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": DROUGHT}]},
        claims={2000: [{"text": KEPT, "quote_ids": ["q1"]},
                       {"text": STRETCHED, "quote_ids": ["q1"]}]},
        rejected={STRETCHED: "The quote does not say drought caused the fall."},
    )
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert _claim_texts(answer, "chg-01") == [KEPT]
    check_inputs = [variables for name, variables in model.calls if name == "check_claims"]
    assert KEPT in check_inputs[0]["claims"] and STRETCHED in check_inputs[0]["claims"]
    dropped = answer["checks"]["chg-01"]["checker_dropped"]
    assert dropped == [{"text": STRETCHED,
                        "reason": "The quote does not say drought caused the fall."}]


def test_checker_sees_each_claim_with_only_its_own_quotes_as_untrusted(monkeypatch, tmp_path):
    """TR-9: the claims block is wrapped; each claim lists only the quotes it cites."""
    seen = {}
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": DROUGHT}, {"page": "P1", "text": RAMPS}]},
        claims={2000: [{"text": KEPT, "quote_ids": ["q1"]}]},
    )

    async def spy(prompt_name, variables, schema, *, trace, budget, temperature=0,
                  untrusted=()):
        seen[prompt_name] = (set(untrusted), variables)
        return await model(prompt_name, variables, schema, trace=trace, budget=budget)

    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    monkeypatch.setattr(llm, "complete_json", spy)
    _run(tmp_path, [_change(1, 2000, 2010)])
    untrusted, variables = seen["check_claims"]
    assert untrusted == {"claims"}
    assert DROUGHT in variables["claims"]
    assert RAMPS not in variables["claims"]
    assert (variables["year_low"], variables["year_high"]) == (1999, 2011)


def test_checker_missing_verdict_drops_the_claim(monkeypatch, tmp_path):
    """Only an explicit "yes" keeps a claim."""
    model = FakeModel(quotes={2000: [{"page": "P1", "text": DROUGHT}]},
                      claims={2000: [{"text": KEPT, "quote_ids": ["q1"]}]})

    async def no_verdicts(prompt_name, variables, schema, *, trace, budget, temperature=0,
                          untrusted=()):
        if prompt_name == "check_claims":
            budget.spend("llm")
            return {"verdicts": []}
        return await model(prompt_name, variables, schema, trace=trace, budget=budget)

    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    monkeypatch.setattr(llm, "complete_json", no_verdicts)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert answer["chapters"]["chg-01"] == {"confidence": "cause_unknown", "claims": []}


@pytest.mark.parametrize("error", [llm.LLMError("checker failed"),
                                   BudgetExceeded("llm budget used up")])
def test_failed_checker_call_drops_the_changes_claims(monkeypatch, tmp_path, error):
    """Unchecked claims are never kept: the change goes cause_unknown with a warning."""
    model = FakeModel(quotes={2000: [{"page": "P1", "text": DROUGHT}]},
                      claims={2000: [{"text": KEPT, "quote_ids": ["q1"]}]},
                      failing={"check_claims": error})
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert answer["chapters"]["chg-01"] == {"confidence": "cause_unknown", "claims": []}
    assert answer["sources"] == []
    assert len(answer["warnings"]) == 1
    assert answer["warnings"][0].startswith("chg-01: cause left unknown")


# --- One rewrite round ---


def _rewrite_inputs(model: FakeModel) -> list[dict]:
    """The variables of every rewrite_claims call."""
    found = []
    for name, variables in model.calls:
        if name == "rewrite_claims":
            found.append(variables)
    return found


def test_code_failure_is_rewritten_once_and_rechecked(monkeypatch, tmp_path):
    """A claim with a number not in its quotes goes back once with the exact problem; the
    fixed rewrite passes the code checks and the checker and is kept (rescued)."""
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": DROUGHT}]},
        claims={2000: [{"text": "Lake Mead plunged 150 feet by 2010.", "quote_ids": ["q1"]}]},
        rewrites={2000: [{"text": KEPT, "quote_ids": ["q1"]}]},
    )
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert _claim_texts(answer, "chg-01") == [KEPT]
    assert answer["checks"]["chg-01"]["rescued"] == [KEPT]
    assert "number not in its quotes" in _rewrite_inputs(model)[0]["problems"]
    assert model.prompts() == ["extract_quotes", "compose_claims", "rewrite_claims",
                               "check_claims"]


def test_checker_no_is_rewritten_with_its_reason(monkeypatch, tmp_path):
    """The checker's reason goes to the rewrite; the rewrite is checked again."""
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": DROUGHT}]},
        claims={2000: [{"text": STRETCHED, "quote_ids": ["q1"]}]},
        rewrites={2000: [{"text": KEPT, "quote_ids": ["q1"]}]},
        rejected={STRETCHED: "No quote says drought caused it."},
    )
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert _claim_texts(answer, "chg-01") == [KEPT]
    assert "No quote says drought caused it." in _rewrite_inputs(model)[0]["problems"]
    assert model.prompts().count("check_claims") == 2


def test_a_rewrite_that_still_fails_is_dropped_with_no_second_round(monkeypatch, tmp_path):
    """At most one rewrite round per change: a rewrite the checker rejects again is dropped."""
    again = STRETCHED + " Again."
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": DROUGHT}]},
        claims={2000: [{"text": STRETCHED, "quote_ids": ["q1"]}]},
        rewrites={2000: [{"text": again, "quote_ids": ["q1"]}]},
        rejected={STRETCHED: "Cause not stated.", again: "Still not stated."},
    )
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert answer["chapters"]["chg-01"]["claims"] == []
    assert model.prompts().count("rewrite_claims") == 1
    assert answer["checks"]["chg-01"]["rescued"] == []


def test_rewrite_is_skipped_when_the_budget_is_low(monkeypatch, tmp_path):
    """With no room for a rewrite and its check (HL-10), failures are simply dropped."""
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": DROUGHT}]},
        claims={2000: [{"text": STRETCHED, "quote_ids": ["q1"]}]},
        rewrites={2000: [{"text": KEPT, "quote_ids": ["q1"]}]},
        rejected={STRETCHED: "Cause not stated."},
    )
    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    budget = Budget({"exa": 15, "llm": 6, "vision": 6})
    answer = _run(tmp_path, [_change(1, 2000, 2010)], budget=budget)
    assert answer["chapters"]["chg-01"]["claims"] == []
    assert "rewrite_claims" not in model.prompts()


def test_failed_rewrite_check_keeps_only_already_checked_claims(monkeypatch, tmp_path):
    """If the rewrite's checker call fails, the rewrites go (never kept unchecked) and the
    claims that already passed the checker stay."""
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": DROUGHT}, {"page": "P1", "text": RAMPS}]},
        claims={2000: [{"text": KEPT, "quote_ids": ["q1"]},
                       {"text": "Lake Mead fell 150 feet.", "quote_ids": ["q1"]}]},
        rewrites={2000: [{"text": "In 2009, 33 boat ramps at Lake Mead were closed.",
                          "quote_ids": ["q2"]}]},
    )
    calls = {"check": 0}

    async def second_check_fails(prompt_name, variables, schema, *, trace, budget,
                                 temperature=0, untrusted=()):
        if prompt_name == "check_claims":
            calls["check"] += 1
            if calls["check"] == 2:
                budget.spend("llm")
                raise llm.LLMError("checker failed")
        return await model(prompt_name, variables, schema, trace=trace, budget=budget)

    _install(monkeypatch, {2000: [GOV_PAGE]}, model)
    monkeypatch.setattr(llm, "complete_json", second_check_fails)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert calls["check"] == 2
    assert _claim_texts(answer, "chg-01") == [KEPT]
    assert answer["warnings"] == []


# --- Failures stay inside their own change ---


def test_unexpected_error_in_one_change_leaves_only_that_change_unknown(monkeypatch,
                                                                        tmp_path):
    """A non-API error (e.g. IndexError on an empty choices list) is caught per change."""
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": DROUGHT}], 1983: [{"page": "P1", "text": FLOOD}]},
        claims={2000: [{"text": KEPT, "quote_ids": ["q1"]}]},
    )

    async def broken_for_1983(prompt_name, variables, schema, *, trace, budget,
                              temperature=0, untrusted=()):
        if variables["start_year"] == 1983:
            raise IndexError("list index out of range")
        return await model(prompt_name, variables, schema, trace=trace, budget=budget)

    _install(monkeypatch, {2000: [GOV_PAGE], 1983: [FLOOD_PAGE]}, model)
    monkeypatch.setattr(llm, "complete_json", broken_for_1983)
    answer = _run(tmp_path, [_change(1, 2000, 2010), _change(2, 1983, 1990, "water_gain")])
    assert _claim_texts(answer, "chg-01") == [KEPT]
    assert answer["chapters"]["chg-02"] == {"confidence": "cause_unknown", "claims": []}
    assert answer["warnings"] == ["chg-02: cause left unknown because of an unexpected error"]


# --- TR-7: confidence per claim ---


def test_each_claim_gets_confidence_from_its_own_sources(monkeypatch, tmp_path):
    """Two claims, each from one ordinary site: each is "likely" and so is the chapter, even
    though pooling their two sites would have said "confirmed"."""
    second = "Since drought took hold in 1999, the surface of Lake Mead has plunged."
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": DROUGHT}, {"page": "P2", "text": DROUGHT}]},
        claims={2000: [{"text": KEPT, "quote_ids": ["q1"]},
                       {"text": second, "quote_ids": ["q2"]}]},
    )
    _install(monkeypatch, {2000: [NEWS_PAGE, OTHER_NEWS]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert answer["chapters"]["chg-01"]["confidence"] == "likely"
    assert answer["claim_confidence"] == {"cl-01": "likely", "cl-02": "likely"}


def test_chapter_confidence_is_its_best_claims(monkeypatch, tmp_path):
    """One claim from a .gov page (confirmed) and one from a news site (likely)."""
    model = FakeModel(
        quotes={2000: [{"page": "P1", "text": RAMPS}, {"page": "P2", "text": DROUGHT}]},
        claims={2000: [{"text": "In 2009, 33 boat ramps at Lake Mead were closed.",
                        "quote_ids": ["q1"]},
                       {"text": KEPT, "quote_ids": ["q2"]}]},
    )
    _install(monkeypatch, {2000: [GOV_PAGE, NEWS_PAGE]}, model)
    answer = _run(tmp_path, [_change(1, 2000, 2010)])
    assert answer["chapters"]["chg-01"]["confidence"] == "confirmed"
    assert answer["claim_confidence"] == {"cl-01": "confirmed", "cl-02": "likely"}
    for claim in answer["chapters"]["chg-01"]["claims"]:
        assert set(claim) == {"id", "text", "excerpt_ids"}   # nothing private in the claim


# --- Number rule: quantity and superlative words ---


@pytest.mark.parametrize("claim, quote", [
    ("Lake Mead fell by half.", "Lake Mead fell sharply."),
    ("The lake shrank to half its size.", "The lake shrank."),
    ("The town's population doubled.", "The town's population grew."),
    ("Water use tripled.", "Water use grew."),
    ("A quarter of the homes burned.", "Many homes burned."),
    ("A third of the homes burned.", "Many homes burned."),
    ("The lake fell 40 percent.", "The lake fell 40 feet."),
    ("The lake hit a record low.", "The lake hit a low."),
    ("It reached its lowest level since 1956.", "It reached a low level in 1956."),
    ("It reached its highest level ever.", "It reached a high level."),
    ("It hit an all-time low.", "It hit a low."),
])
def test_quantity_words_must_be_in_the_quotes(claim, quote):
    """Quantity and superlative words are facts too: missing from the quote, they drop."""
    assert not causes.numbers_supported(claim, [quote])


@pytest.mark.parametrize("claim, quote", [
    ("Lake Mead fell by half.", "Lake Mead lost half its water."),
    ("The town's population doubled.", "The town's population grew twice as large."),
    ("The lake fell 40 percent.", "The lake fell 40%."),
    ("The lake fell 40 per cent.", "The lake fell 40 percent."),
    ("The lake hit a record low.", "The lake hit a record-low level."),
    ("It reached its lowest level since 1956.", "It is the lowest level since 1956."),
    ("It hit an all-time low.", "It fell to an all time low."),
])
def test_quantity_words_in_the_quotes_pass(claim, quote):
    """The same fact in another word form ("twice" / "doubled", "%" / "percent") passes."""
    assert causes.numbers_supported(claim, [quote])


# --- Meta words ---


@pytest.mark.parametrize("claim", [
    "A map of Lake Mead shows the shoreline in 2010.",
    "Maps of the town were redrawn in 2019.",
    "The Thematic Mapper recorded Lake Mead in 1990.",
    "The picture shows Lake Mead in 2010.",
    "Pictures of Lake Mead were released in 2010.",
    "A camera on the station captured Lake Mead.",
    "MODIS data show the burn scar near Paradise.",
    "Sentinel-2 recorded the island in 2019.",
    "Sentinel 1 radar saw flooding at the site.",
])
def test_new_meta_words_are_dropped(claim):
    """Maps, pictures, cameras and other sensors are about the imagery, not the ground."""
    assert causes.is_meta_claim(claim)


@pytest.mark.parametrize("claim", [
    "The trail to Sentinel Dome was rebuilt in 2012.",
    "The Orlando Sentinel reported 30 homes were demolished.",
    "Modis Construction cleared the site in 2016.",
    "The city mapped out a new suburb and built 300 homes.",
    "Crews cleared 40 acres for the Landsatz Road extension.",
])
def test_words_that_only_look_like_meta_words_are_kept(claim):
    """Sentinel as a name, Modis as a company, "mapped out", and a name containing Landsat."""
    assert not causes.is_meta_claim(claim)


def test_a_no_verdict_is_never_overwritten_by_a_later_yes():
    """TR-5: any "no" drops the claim, even if the checker repeats the claim with a "yes"."""
    from app.agent.causes import verdicts_by_number
    found = verdicts_by_number([
        {"claim": "c1", "supported": "no", "reason": "date not stated"},
        {"claim": "c1", "supported": "yes", "reason": "fine"},
        {"claim": "c2", "supported": "yes", "reason": "fine"},
    ])
    assert found[1]["supported"] == "no"
    assert found[2]["supported"] == "yes"
