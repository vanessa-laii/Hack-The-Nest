# S4 approach "single-pass": one model call per change asks for claims with exact quotes.
# Inputs: a PlaceInput (story + recorded pages per change), a Trace and a Budget (HL-10).
# Outputs: {"sources": [...], "chapters": {change_id: {"confidence", "claims"}}} (story schema).
#
# How it works:
#   1. Each page is trimmed to the passages near the place's names, the change's years and
#      words about the change kind, so a change's pages fit easily in one prompt.
#   2. Each page is wrapped as untrusted web text (TR-9) under a header with its URL and date.
#   3. One complete_json call per change, one change at a time, asks for 0-3 claims, each
#      with exact quotes and URLs. A used-up budget or failed call leaves that change unknown.
#   4. In code, a quote is kept only if it is word for word on that URL's recorded page (TR-2)
#      and is about the place or the years. A claim is kept only if one kept quote both names
#      the place and fits the years ±1 (TR-3/4), and its numbers all appear in its quotes (TR-6).
#   5. Kept quotes become sources/excerpts with ids src-NN / ex-NN-MM, claims get cl-NN, and
#      confidence comes from verify.confidence() over the chapter's sources (TR-7), never the model.
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "backend"))

from app import llm  # noqa: E402
from app.agent import verify  # noqa: E402
from app.agent.trace import BudgetExceeded  # noqa: E402

PROMPT_NAME = "find_causes"
PROMPTS_DIR = HERE / "prompts"

PAGE_CHARS = 2000          # trimmed text kept per page
LEAD_CHARS = 600           # shown when nothing in a page matches, so the model still sees its gist
CHUNK_CHARS = 700          # long paragraphs are cut into sentence groups about this size
MAX_CLAIMS = 3
MAX_QUOTES = 3
EXCERPT_MAX_CHARS = 600    # story.schema.json excerpt maxLength
CLAIM_MAX_CHARS = 240      # story.schema.json claim maxLength
GAP = "\n[...]\n"
CALL_GAP_S = 4             # pause between calls: bursts hit the provider's rate limit

# Words that hint a passage is about this kind of change. Used only to pick passages to show.
KIND_WORDS = {
    "water_loss": ["drought", "water level", "low", "shrink", "decline", "elevation", "drop"],
    "water_gain": ["rise", "rose", "flood", "water level", "elevation", "fill", "high"],
    "vegetation_loss": ["clear", "drought", "fire", "logging", "removed", "dead"],
    "vegetation_gain": ["park", "golf", "landscap", "irrigat", "planted", "trees", "green",
                        "development", "community", "homes", "housing"],
    "burn": ["fire", "burn", "blaze", "acres"],
    "urban_growth": ["construct", "built", "build", "develop", "clear", "demoli", "bridge",
                     "plant", "site", "acres"],
    "other": ["demoli", "torn down", "razed", "removed", "plant", "site", "cleanup"],
}

# The model is told to answer in this shape; llm.py checks it with jsonschema.
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "quotes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                            "required": ["url", "quote"],
                        },
                    },
                },
                "required": ["text", "quotes"],
            },
        },
    },
    "required": ["claims"],
}

NUMBER_PATTERN = re.compile(r"\d[\d,.]*\d|\d")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


# --- Step 1: trimming pages ---


def split_passages(text: str) -> list[str]:
    """Paragraphs of the page; long ones are cut into groups of whole sentences.
    Passages are exact slices of the page, so quotes copied from them stay word for word."""
    passages = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= CHUNK_CHARS:
            passages.append(paragraph)
            continue
        group = ""
        for sentence in SENTENCE_END.split(paragraph):
            if group and len(group) + len(sentence) > CHUNK_CHARS:
                passages.append(group)
                group = ""
            group = f"{group} {sentence}".strip()
        if group:
            passages.append(group)
    return passages


def passage_score(passage: str, names: list[str], change: dict) -> int:
    """How useful a passage looks: names the place, mentions the years, or the change kind."""
    if len(passage.split()) < verify.MIN_EXCERPT_WORDS:
        return 0  # too short to quote (dates, captions, headings)
    score = 0
    if verify.names_place(passage, names):
        score += 3
    years = verify.stated_year_spans(passage)
    for first, last in years:
        if verify.overlaps(first, last, change["start_year"], change["end_year"]):
            score += 2
            break
    lowered = passage.lower()
    for word in KIND_WORDS.get(change["kind"], []):
        if word in lowered:
            score += 1
            break
    return score


def trim_page(text: str, names: list[str], change: dict) -> str:
    """The best-scoring passages of a page, in page order, up to PAGE_CHARS."""
    passages = split_passages(text or "")
    ranked = []
    for index, passage in enumerate(passages):
        score = passage_score(passage, names, change)
        if score > 0:
            ranked.append((score, index))
    ranked.sort(key=lambda item: (-item[0], item[1]))

    chosen = []
    used = 0
    for score, index in ranked:
        size = len(passages[index])
        if used + size > PAGE_CHARS:
            continue
        chosen.append(index)
        used += size
    if not chosen:
        return (text or "")[:LEAD_CHARS]
    chosen.sort()
    return GAP.join(passages[index] for index in chosen)


# --- Step 2 and 3: the prompt and the call ---


def page_block(number: int, page: dict, names: list[str], change: dict) -> str:
    """One page for the prompt: a header line we write, then the page wrapped as untrusted."""
    header = f"PAGE {number}: url: {page['url']} | published: {page.get('published_date')}"
    body = f"Title: {page.get('title') or ''}\n\n{trim_page(page.get('text'), names, change)}"
    return f"{header}\n{llm.wrap_untrusted(body, f'page {number}')}"


def scope_note(change: dict) -> str:
    """Tells the model how big the change is, so a small patch does not get a regional story."""
    if change.get("scope") == "local":
        return (f"LOCAL. Only a small patch of about {change.get('area_ha')} hectares changed. "
                "Only claim a cause if a quote describes work on a specific named site at this "
                "place in these years (a plant, a construction project, a park) that matches "
                "this kind of change. Something spread across a whole neighbourhood, town or "
                "region (population growth, a program of scattered demolitions, a drought) is "
                "NOT a cause for a small patch.")
    return (f"PLACE-WIDE. About {change.get('area_ha')} hectares changed, across a large part "
            "of the area.")


def change_summary(place: dict, change: dict) -> dict:
    """The facts about the change that the model needs, from the story's own data."""
    region = change.get("region") or {}
    return {
        "place": place.get("display_name"),
        "kind": change["kind"],
        "years": f"{change['start_year']}-{change['end_year']}",
        "scope": change.get("scope"),
        "area_ha": change.get("area_ha"),
        "where": region.get("description"),
    }


async def ask_model(place: dict, change: dict, pages: list[dict], names: list[str],
                    trace, budget) -> dict:
    """One complete_json call for one change. Pages are wrapped one by one (TR-9) here, so
    they are not passed as `untrusted` again (that would break the inner delimiters)."""
    blocks = []
    for number, page in enumerate(pages, start=1):
        blocks.append(page_block(number, page, names, change))
    variables = {
        "change": change_summary(place, change),
        "place_names": ", ".join(names),
        "example_name": names[0],
        "scope_note": scope_note(change),
        "year_from": str(change["start_year"] - verify.YEAR_SLACK),
        "year_to": str(change["end_year"] + verify.YEAR_SLACK),
        "pages": "\n\n".join(blocks),
    }
    return await llm.complete_json(PROMPT_NAME, variables, REPLY_SCHEMA,
                                   trace=trace, budget=budget)


async def safe_ask(place, change, pages, names, trace, budget) -> dict:
    """ask_model, but a used-up budget or failed call means 'no claims' (TR-8), not a crash."""
    if not pages:
        return {"claims": []}
    try:
        return await ask_model(place, change, pages, names, trace, budget)
    except (BudgetExceeded, llm.LLMError):
        return {"claims": []}


# --- Step 4: checking the reply in code ---


def numbers_in(text: str) -> set[str]:
    """Numbers written in a text, without thousands commas ("1,200" -> "1200")."""
    found = set()
    for match in NUMBER_PATTERN.finditer(text):
        found.add(match.group(0).replace(",", "").rstrip("."))
    return found


def numbers_supported(claim_text: str, quotes: list[str]) -> bool:
    """TR-6: every number in the claim (years included) appears in its own quotes.
    The change's years are not allowed here: a claim must not date an event its quotes don't."""
    allowed = set()
    for quote in quotes:
        allowed |= numbers_in(quote)
    return numbers_in(claim_text) <= allowed


def checked_quotes(claim: dict, pages_by_url: dict, names: list[str], change: dict) -> list:
    """The claim's quotes that are word for word on their page (TR-2) and are about the place
    or the years. Returns (page, text, fits_place_and_years) for each kept quote."""
    kept = []
    for item in claim.get("quotes", [])[:MAX_QUOTES]:
        page = pages_by_url.get(item.get("url", "").strip())
        text = (item.get("quote") or "").strip()
        if page is None or not text or len(text) > EXCERPT_MAX_CHARS:
            continue
        if not verify.excerpt_in_page(text, page.get("text") or ""):
            continue
        about_place = verify.names_place(text, names)
        in_time = verify.in_years(text, page.get("published_date"),
                                  change["start_year"], change["end_year"])
        if not (about_place or in_time):
            continue  # neither this place nor this time: no evidence for this change
        kept.append((page, text, about_place and in_time))
    return kept


def checked_claim(claim: dict, pages_by_url: dict, names: list[str], change: dict):
    """(claim text, kept quotes) if the claim passes every code check, else None."""
    text = " ".join((claim.get("text") or "").split())
    if not text or len(text) > CLAIM_MAX_CHARS:
        return None
    quotes = checked_quotes(claim, pages_by_url, names, change)
    anchored = False
    for _page, _quote, fits in quotes:
        if fits:
            anchored = True
    if not anchored:
        return None  # TR-3/4: no quote both names the place and fits the years
    quote_texts = [quote for _page, quote, _fits in quotes]
    if not numbers_supported(text, quote_texts):
        return None
    return text, quotes


# --- Step 5: ids, sources and confidence ---


class Evidence:
    """Collects sources and excerpts across the whole story, handing out unique ids."""

    def __init__(self):
        self.sources = []
        self.by_url = {}
        self.claim_count = 0
        self.retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def source_for(self, page: dict) -> dict:
        """The story source for a page, created on first use as src-NN."""
        url = page["url"]
        if url not in self.by_url:
            published = page.get("published_date")
            source = {
                "id": f"src-{len(self.sources) + 1:02d}",
                "url": url,
                "title": page.get("title") or page.get("domain") or url,
                "domain": page.get("domain") or "",
                "published_date": published[:10] if published else None,
                "authority": verify.authority(page.get("domain") or ""),
                "retrieved_at": self.retrieved_at,
                "excerpts": [],
            }
            self.sources.append(source)
            self.by_url[url] = source
        return self.by_url[url]

    def excerpt_id(self, page: dict, text: str) -> tuple[str, dict]:
        """The id of this excerpt (ex-NN-MM), reusing it if the same text was quoted before."""
        source = self.source_for(page)
        for excerpt in source["excerpts"]:
            if excerpt["text"] == text:
                return excerpt["id"], source
        number = source["id"].split("-")[1]
        excerpt_id = f"ex-{number}-{len(source['excerpts']) + 1:02d}"
        source["excerpts"].append({"id": excerpt_id, "text": text, "verified": True})
        return excerpt_id, source

    def next_claim_id(self) -> str:
        """The next claim id, cl-01, cl-02 …, unique across the story."""
        self.claim_count += 1
        return f"cl-{self.claim_count:02d}"


def build_chapter(reply: dict, change: dict, pages: list[dict], names: list[str],
                  evidence: Evidence) -> dict:
    """Checks every claim of one reply and turns the survivors into schema claims."""
    pages_by_url = {}
    for page in pages:
        pages_by_url[page["url"]] = page
    claims = []
    chapter_sources = {}
    for claim in reply.get("claims", [])[:MAX_CLAIMS]:
        passed = checked_claim(claim, pages_by_url, names, change)
        if passed is None:
            continue
        text, quotes = passed
        excerpt_ids = []
        for page, quote, _fits in quotes:
            excerpt_id, source = evidence.excerpt_id(page, quote)
            if excerpt_id not in excerpt_ids:
                excerpt_ids.append(excerpt_id)
            chapter_sources[source["id"]] = source
        claims.append({"id": evidence.next_claim_id(), "text": text, "excerpt_ids": excerpt_ids})
    level = verify.confidence(list(chapter_sources.values())) if claims else "cause_unknown"
    return {"confidence": level, "claims": claims}


# --- Public ---


async def explain(place, *, trace, budget) -> dict:
    """Sources and per-change claims for one place. Unsupported changes stay cause_unknown."""
    llm.PROMPTS_DIR = PROMPTS_DIR  # this spike's prompts live in its own folder
    story = place.story
    names = verify.place_names_for(story["place"])
    changes = story["changes"]

    evidence = Evidence()
    chapters = {}
    # One change at a time: parallel calls hit the provider's rate limit, and every
    # rate-limited retry still costs a call from the story's budget.
    for number, change in enumerate(changes):
        if number > 0:
            await asyncio.sleep(CALL_GAP_S)
        pages = place.results_by_change.get(change["id"], [])
        reply = await safe_ask(story["place"], change, pages, names, trace, budget)
        chapters[change["id"]] = build_chapter(reply, change, pages, names, evidence)
    return {"sources": evidence.sources, "chapters": chapters}
