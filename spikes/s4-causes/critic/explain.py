# S4 "critic" approach: extract quotes → verify in code → compose claims → separate checker (TR-5).
# Inputs: a PlaceInput (base story + recorded pages per change), a Trace and a Budget (HL-10).
# Outputs: {"sources": [...], "chapters": {change_id: {"confidence", "claims"}}}; a run log.
#
# How it works:
#   1. Trim each page to the passages that name the place, so one extract call per change
#      can see all of its pages (HL-10: ≤ 25 calls per story).
#   2. Extract: one model call per change copies candidate quotes from the wrapped pages (TR-9).
#   3. Verify in code: a quote survives only if it is word for word on its page, names the
#      place and fits the change's years ±1 (TR-2, TR-3, TR-4). Failures are dropped.
#   4. Compose: one call writes 0–3 claims from the verified quotes only, each citing quote ids.
#      Code then rejects claims with numbers or names that are not in their quotes (TR-6).
#   5. Critic: a separate call checks every claim of the change against its quotes; any "no"
#      drops the claim (TR-5).
#   6. Assemble sources (src-NN, ex-NN-NN), claims (cl-NN) and confidence by rule (TR-7).
#      A change without claims stays cause_unknown (TR-8). Each step is logged to runs/.
from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app import llm
from app.agent import verify
from app.agent.trace import BudgetExceeded

HERE = Path(__file__).resolve().parent
PROMPTS_DIR = HERE / "prompts"
RUNS_DIR = HERE / "runs"

CALLS_PER_CHANGE = 3          # extract + compose + critic
MAX_PAGE_CHARS = 3000         # trimmed text per page sent to the extractor
MAX_CHUNK_CHARS = 700         # long lines are cut into sentence groups of about this size
MAX_EXCERPT_CHARS = 600       # story.schema.json excerpt maxLength
MAX_CLAIM_CHARS = 240         # story.schema.json claim maxLength
MAX_CLAIMS_PER_CHANGE = 3
RATE_LIMIT_WAIT_S = 20       # pause before the one extra try after a 429 (rate limit)

# What the images show, in plain words. Says what changed, never why.
CHANGE_WORDS = {
    "water_loss": "water area shrank (water gave way to land)",
    "water_gain": "water area grew (land went under water)",
    "vegetation_loss": "green cover was lost",
    "vegetation_gain": "the land turned greener",
    "urban_growth": "land was cleared or built over (bare ground and buildings look alike)",
    "burn": "a large area burned",
    "other": "ground that read as built-up or bare became something else",
}

# Rule for local patches (README): no regional story for a few-hectare patch.
LOCAL_RULE = ("This is a local patch of about {area} hectares, not the whole place. Only a "
              "passage about a specific site, project or event at this place, big enough to "
              "change about {area} hectares, can explain it. A regional trend cannot, and "
              "neither can a few garden plots or a single building.")
PLACE_RULE = "This change covers the place as a whole, so a passage about the place fits."

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
CLAIM_ID = re.compile(r"[cC]\d+")
CAPITALISED_WORD = re.compile(r"\b[A-Z][A-Za-z'’\-]+")


# --- Prompts and model calls ---


@contextmanager
def spike_prompts():
    """Points llm.py's prompt folder at this approach's prompts/ for the duration of a run.
    complete_json() loads prompts by name only, and these prompts belong to the spike."""
    original = llm.PROMPTS_DIR
    llm.PROMPTS_DIR = PROMPTS_DIR
    try:
        yield
    finally:
        llm.PROMPTS_DIR = original


async def ask(prompt_name: str, variables: dict, schema: dict, calls_after: int,
              trace, budget) -> dict:
    """complete_json(), with one more try after a pause if the provider said "slow down" (429).
    The extra try happens only if the budget still covers it plus the calls after it (HL-10)."""
    try:
        return await llm.complete_json(prompt_name, variables, schema,
                                       trace=trace, budget=budget)
    except llm.LLMError as error:
        is_rate_limit = "429" in str(error)
        affordable = budget.remaining("llm") >= llm.NETWORK_ATTEMPTS + calls_after
        if not (is_rate_limit and affordable):
            raise
    await asyncio.sleep(RATE_LIMIT_WAIT_S)
    return await llm.complete_json(prompt_name, variables, schema, trace=trace, budget=budget)


def change_variables(place: dict, chapter: dict, change: dict) -> dict:
    """The change description shared by all three prompts."""
    if change.get("scope") == "local":
        scope_words = f"a local patch of about {change.get('area_ha')} hectares"
        scope_rule = LOCAL_RULE.replace("{area}", str(change.get("area_ha")))
    else:
        scope_words = "across the place as a whole"
        scope_rule = PLACE_RULE
    region = (change.get("region") or {}).get("description") or ""
    if region:
        scope_words += f" ({region})"
    return {
        "place_name": place["display_name"],
        "start_year": str(chapter["start_year"]),
        "end_year": str(chapter["end_year"]),
        "start_year_minus_one": str(chapter["start_year"] - verify.YEAR_SLACK),
        "end_year_plus_one": str(chapter["end_year"] + verify.YEAR_SLACK),
        "change_words": CHANGE_WORDS.get(change["kind"], CHANGE_WORDS["other"]),
        "scope_words": scope_words,
        "scope_rule": scope_rule,
    }


# --- Step 1: trim pages ---


def page_chunks(text: str) -> list[str]:
    """Splits page text into lines, and long lines into groups of whole sentences.
    Each chunk is an exact slice of the page, so a quote copied from it is on the page."""
    chunks = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) <= MAX_CHUNK_CHARS:
            chunks.append(line)
            continue
        group = ""
        for sentence in SENTENCE_END.split(line):
            if group and len(group) + len(sentence) + 1 > MAX_CHUNK_CHARS:
                chunks.append(group)
                group = ""
            group = f"{group} {sentence}".strip()
        if group:
            chunks.append(group)
    return chunks


def trimmed_page(text: str, names: list[str], start_year: int, end_year: int) -> str:
    """Only the chunks that name the place, those that also fit the years first.
    A quote must name the place (TR-3), so other chunks could never become evidence."""
    naming = []
    for position, chunk in enumerate(page_chunks(text)):
        if not verify.names_place(chunk, names):
            continue
        fits_years = verify.in_years(chunk, None, start_year, end_year)
        naming.append((0 if fits_years else 1, position, chunk))
    naming.sort()
    kept = []
    used = 0
    for _, position, chunk in naming:
        if used + len(chunk) > MAX_PAGE_CHARS:
            continue
        kept.append((position, chunk))
        used += len(chunk)
    kept.sort()  # back to page order so the passages read naturally
    return "\n".join(chunk for _, chunk in kept)


def pages_block(pages: list[dict]) -> str:
    """All trimmed pages, each wrapped separately as untrusted web text (TR-9)."""
    blocks = []
    for page in pages:
        body = (f"Title: {page['title']}\nSite: {page['domain']}\n"
                f"Published: {page['published_date']}\n\n{page['trimmed']}")
        blocks.append(f"### Page {page['label']}\n" + llm.wrap_untrusted(body, page["label"]))
    return "\n\n".join(blocks)


# --- Step 2 and 3: extract quotes, then verify them in code ---

EXTRACT_SCHEMA = {
    "type": "object",
    "required": ["quotes"],
    "properties": {"quotes": {"type": "array", "items": {
        "type": "object", "required": ["page", "text"],
        "properties": {"page": {"type": "string"}, "text": {"type": "string"}},
    }}},
}


def quote_problem(text: str, page: dict, names: list[str], chapter: dict) -> str | None:
    """Why a quote fails the code checks (TR-2/3/4, length), or None if it passes."""
    if len(text) > MAX_EXCERPT_CHARS:
        return "longer than 600 characters"
    if not verify.excerpt_in_page(text, page["text"]):
        return "not word for word on the page (TR-2)"
    if not verify.names_place(text, names):
        return "does not name the place (TR-3)"
    if not verify.in_years(text, page["published_date"], chapter["start_year"],
                           chapter["end_year"]):
        return "outside the change's years ±1 (TR-4)"
    return None


def verified_quotes(raw_quotes: list[dict], pages_by_label: dict, names: list[str],
                    chapter: dict, log: dict) -> list[dict]:
    """Keeps the quotes that pass every code check, de-duplicated, ids q01, q02..."""
    kept = []
    seen = set()
    for raw in raw_quotes:
        text = (raw.get("text") or "").strip()
        page = pages_by_label.get((raw.get("page") or "").strip())
        problem = "unknown page label" if page is None else \
            quote_problem(text, page, names, chapter)
        if problem is None and (page["url"], verify.normalise(text)) in seen:
            problem = "duplicate"
        if problem:
            log["dropped_quotes"].append({"text": text[:200], "why": problem})
            continue
        seen.add((page["url"], verify.normalise(text)))
        quote_id = f"q{len(kept) + 1:02d}"
        kept.append({"id": quote_id, "text": text, "page": page})
    return kept


# --- Step 4: compose claims, then check numbers and names in code ---

COMPOSE_SCHEMA = {
    "type": "object",
    "required": ["claims"],
    "properties": {"claims": {"type": "array", "items": {
        "type": "object", "required": ["text", "quote_ids"],
        "properties": {"text": {"type": "string"},
                       "quote_ids": {"type": "array", "items": {"type": "string"}}},
    }}},
}


def quotes_block(quotes: list[dict]) -> str:
    """Verified quotes with their ids and source, wrapped as untrusted web text (TR-9)."""
    lines = []
    for quote in quotes:
        page = quote["page"]
        lines.append(f"[{quote['id']}] ({page['domain']}, {page['published_date']}) "
                     f"{quote['text']}")
    return llm.wrap_untrusted("\n".join(lines), "verified passages")


def claim_problem(text: str, cited: list[dict], names: list[str]) -> str | None:
    """Why a composed claim fails the code checks (length, TR-6), or None if it passes.
    Every number, and every capitalised word after the first, must be in its quotes. Not even
    the change's own years are allowed: a year the quotes don't state is an added date (TR-5)."""
    if not text or len(text) > MAX_CLAIM_CHARS:
        return "empty or longer than 240 characters"
    if not cited:
        return "cites no verified quote"
    quote_text = verify.normalise(" ".join(quote["text"] for quote in cited))
    quote_numbers = set()
    for number in NUMBER.findall(quote_text):
        quote_numbers.add(number.replace(",", "").rstrip("."))
    for number in NUMBER.findall(text):
        plain = number.replace(",", "").rstrip(".")
        if plain not in quote_numbers:
            return f"number {number} is not in its quotes (TR-6)"
    known_names = quote_text + " " + " ".join(names)
    words = CAPITALISED_WORD.findall(verify.normalise(text))
    for word in words[1:]:
        word = re.sub(r"['’]s$", "", word)  # "Detroit's" is still the name "Detroit"
        whole_word = re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)")
        if not whole_word.search(known_names):
            return f"name {word!r} is not in its quotes (TR-6)"
    return None


def checked_claims(raw_claims: list[dict], quotes: list[dict], names: list[str],
                   log: dict) -> list[dict]:
    """Claims that cite real quote ids and pass claim_problem(); ids c1, c2..."""
    quotes_by_id = {quote["id"]: quote for quote in quotes}
    kept = []
    for raw in raw_claims[:MAX_CLAIMS_PER_CHANGE]:
        text = (raw.get("text") or "").strip()
        cited = []
        for quote_id in raw.get("quote_ids") or []:
            if quote_id in quotes_by_id and quotes_by_id[quote_id] not in cited:
                cited.append(quotes_by_id[quote_id])
        problem = claim_problem(text, cited, names)
        if problem:
            log["code_dropped_claims"].append({"text": text, "why": problem})
            continue
        kept.append({"id": f"c{len(kept) + 1}", "text": text, "quotes": cited})
    return kept


# --- Step 5: the critic, a separate checker call (TR-5) ---

CRITIC_SCHEMA = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object", "required": ["claim", "reason", "supported"],
        "properties": {"claim": {"type": "string"}, "reason": {"type": "string"},
                       "supported": {"enum": ["yes", "no"]}},
    }}},
}


def claims_block(claims: list[dict]) -> str:
    """Each claim with only the quotes it cites; the quotes are wrapped as untrusted."""
    parts = []
    for claim in claims:
        quote_lines = []
        for quote in claim["quotes"]:
            page = quote["page"]
            quote_lines.append(f"- ({page['domain']}, {page['published_date']}) {quote['text']}")
        wrapped = llm.wrap_untrusted("\n".join(quote_lines), f"passages for {claim['id']}")
        parts.append(f"### Claim {claim['id']}: {claim['text']}\nCited passages:\n{wrapped}")
    return "\n\n".join(parts)


def critic_approved(claims: list[dict], verdicts: list[dict], log: dict) -> list[dict]:
    """Keeps a claim only with an explicit "yes"; a "no" or a missing verdict drops it."""
    verdict_by_claim = {}
    for verdict in verdicts:
        found_id = CLAIM_ID.search(verdict["claim"])  # "c1", "Claim c1" and "C1" all mean c1
        if found_id:
            verdict_by_claim[found_id.group(0).lower()] = verdict
    approved = []
    for claim in claims:
        verdict = verdict_by_claim.get(claim["id"])
        entry = {"text": claim["text"], "quotes": [q["text"] for q in claim["quotes"]],
                 "verdict": verdict["supported"] if verdict else "missing",
                 "reason": verdict["reason"] if verdict else "no verdict returned"}
        if verdict and verdict["supported"] == "yes":
            log["critic_kept"].append(entry)
            approved.append(claim)
        else:
            log["critic_dropped"].append(entry)
    return approved


# --- One change: steps 2 to 5 ---


async def explain_change(place: dict, chapter: dict, change: dict, results: list[dict],
                         names: list[str], trace, budget, log: dict) -> list[dict]:
    """The approved claims for one change (each with its quotes), or [] for cause unknown."""
    variables = change_variables(place, chapter, change)
    pages = []
    for result in results:
        trimmed = trimmed_page(result.get("text") or "", names, chapter["start_year"],
                               chapter["end_year"])
        if trimmed:
            pages.append({**result, "trimmed": trimmed, "label": f"P{len(pages) + 1:02d}"})
    log["pages_sent"] = len(pages)
    if not pages:
        return []

    extracted = await ask(
        "critic_extract", {**variables, "place_names": ", ".join(names),
                           "pages": pages_block(pages)},
        EXTRACT_SCHEMA, 2, trace, budget)
    pages_by_label = {page["label"]: page for page in pages}
    quotes = verified_quotes(extracted["quotes"], pages_by_label, names, chapter, log)
    log["verified_quotes"] = [quote["text"] for quote in quotes]
    if not quotes:
        return []

    composed = await ask(
        "critic_compose", {**variables, "quotes": quotes_block(quotes)},
        COMPOSE_SCHEMA, 1, trace, budget)
    claims = checked_claims(composed["claims"], quotes, names, log)
    if not claims:
        return []

    checked = await ask(
        "critic_check", {**variables, "claims": claims_block(claims)},
        CRITIC_SCHEMA, 0, trace, budget)
    return critic_approved(claims, checked["verdicts"], log)


async def safe_explain_change(*args, log: dict) -> list[dict]:
    """explain_change(), but a used-up budget or a failed model call means cause unknown."""
    budget = args[-1]
    if budget.remaining("llm") < CALLS_PER_CHANGE:  # can't finish all three steps (HL-10)
        log["error"] = "skipped: budget left is less than one change needs"
        return []
    try:
        return await explain_change(*args, log=log)
    except (BudgetExceeded, llm.LLMError) as error:
        log["error"] = f"{type(error).__name__}: {error}"
        return []


# --- Step 6: assemble sources, claims and confidence ---


class StoryEvidence:
    """Hands out src-NN / ex-NN-NN / cl-NN ids, re-using a source or excerpt seen before."""

    def __init__(self):
        self.sources: list[dict] = []
        self.source_by_url: dict[str, dict] = {}
        self.claim_count = 0
        self.retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def source_for(self, page: dict) -> dict:
        """The story source for a page, created on first use."""
        if page["url"] not in self.source_by_url:
            source = {
                "id": f"src-{len(self.sources) + 1:02d}",
                "url": page["url"],
                "title": page.get("title") or page["domain"],
                "domain": page["domain"],
                "published_date": page.get("published_date"),
                "authority": verify.authority(page["domain"]),
                "retrieved_at": self.retrieved_at,
                "excerpts": [],
            }
            self.sources.append(source)
            self.source_by_url[page["url"]] = source
        return self.source_by_url[page["url"]]

    def excerpt_id_for(self, quote: dict) -> tuple[str, dict]:
        """(excerpt id, source) for a verified quote, re-using an identical excerpt."""
        source = self.source_for(quote["page"])
        for excerpt in source["excerpts"]:
            if excerpt["text"] == quote["text"]:
                return excerpt["id"], source
        excerpt_id = f"ex-{source['id'][4:]}-{len(source['excerpts']) + 1:02d}"
        source["excerpts"].append({"id": excerpt_id, "text": quote["text"], "verified": True})
        return excerpt_id, source

    def chapter_for(self, claims: list[dict]) -> dict:
        """Story claims and the TR-7 confidence over the sources they use."""
        story_claims = []
        used_sources = {}
        for claim in claims:
            excerpt_ids = []
            for quote in claim["quotes"]:
                excerpt_id, source = self.excerpt_id_for(quote)
                excerpt_ids.append(excerpt_id)
                used_sources[source["id"]] = source
            self.claim_count += 1
            story_claims.append({"id": f"cl-{self.claim_count:02d}", "text": claim["text"],
                                 "excerpt_ids": excerpt_ids})
        level = verify.confidence(list(used_sources.values())) if story_claims \
            else "cause_unknown"
        return {"confidence": level, "claims": story_claims}


def final_code_check(claims: list[dict], names: list[str], chapter: dict) -> list[dict]:
    """Re-runs the quote checks just before output: nothing unverified can slip through."""
    kept = []
    for claim in claims:
        good_quotes = []
        for quote in claim["quotes"]:
            if quote_problem(quote["text"], quote["page"], names, chapter) is None:
                good_quotes.append(quote)
        if good_quotes and len(good_quotes) == len(claim["quotes"]):
            kept.append(claim)
    return kept


# --- Public ---


async def explain(place_input, *, trace, budget) -> dict:
    """Finds documented, checked causes for each change of one place (see the header)."""
    story = place_input.story
    place = story["place"]
    names = verify.place_names_for(place)
    changes = {change["id"]: change for change in story["changes"]}
    chapters = [c for c in story["chapters"] if c["kind"] == "change"]

    # One change at a time: parallel calls hit the provider's rate limit, and every
    # retried request counts against the story's HL-10 budget.
    logs = {}
    answers = []
    with spike_prompts():
        for chapter in chapters:
            change_id = chapter["change_id"]
            logs[change_id] = {"dropped_quotes": [], "code_dropped_claims": [],
                               "critic_kept": [], "critic_dropped": []}
            results = place_input.results_by_change.get(change_id, [])
            claims = await safe_explain_change(place, chapter, changes[change_id], results,
                                               names, trace, budget, log=logs[change_id])
            answers.append(claims)

    evidence = StoryEvidence()
    by_change = {}
    for chapter, claims in zip(chapters, answers):
        claims = final_code_check(claims, names, chapter)
        by_change[chapter["change_id"]] = evidence.chapter_for(claims)

    RUNS_DIR.mkdir(exist_ok=True)
    run_log = {"llm_calls": budget.used.get("llm", 0), "changes": logs}
    (RUNS_DIR / f"{story['id']}.json").write_text(
        json.dumps(run_log, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"sources": evidence.sources, "chapters": by_change}
