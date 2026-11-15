# S4 approach "extract-verify": exact quotes first, checked in code, then claims from those only.
# Inputs: a PlaceInput (base story + recorded pages per change), a Trace and a Budget (HL-10).
# Outputs: {"sources": [...], "chapters": {change_id: {"confidence", "claims"}}} (story schema).
#
# How it works:
#   1. Trim each page to the windows around the place's names: a quote that doesn't name the
#      place can never pass TR-3, so the rest of the page is only cost. Pages with no name drop.
#   2. Extract: one model call per batch of trimmed pages asks for exact quotes only, no claims.
#      The pages are wrapped as untrusted text (TR-9).
#   3. Verify in code: each quote must be on its page word for word (TR-2), name the place
#      (TR-3) and fit the change's years ±1 (TR-4). Failures are dropped; repeats are merged.
#   4. Compose: one call per change sees ONLY the verified quotes and writes 0-3 claims, each
#      citing quote ids. "Cause unknown" is an allowed answer (TR-8).
#   5. Check claims in code: cited ids exist, numbers and names come from the quotes or the
#      story's data (TR-6), length fits. Anything else is dropped.
#   6. Number sources, excerpts and claims across the story; confidence comes from
#      verify.confidence() over each chapter's sources (TR-7), never from the model.
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from app import llm
from app.agent import verify
from app.agent.trace import BudgetExceeded

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

WINDOW_CHARS = 700          # text kept on each side of a place-name mention
MAX_PAGE_CHARS = 5000       # trimmed text per page, at most
MAX_BATCH_CHARS = 30_000    # trimmed page text per call; long prompts made the model
                            # return no quotes at all (Palm Jumeirah, 45k chars)
MAX_EXCERPT_CHARS = 600     # story schema limit
MAX_CLAIM_CHARS = 240       # story schema limit
MAX_CLAIMS = 3
RATE_LIMIT_WAIT_S = 45      # pause after the provider says "too many requests" (429)
MIN_GAP_S = 5.0             # spacing between our own calls, to stay under the rate limit
RETRY_SLACK = 3             # calls kept back for llm.py's one retry on an invalid reply

# What the satellite shows, in words. Urban growth is often bare cleared ground (ADR-003).
KIND_WORDS = {
    "water_loss": "the water area shrank",
    "water_gain": "the water area grew",
    "vegetation_loss": "vegetation was lost",
    "vegetation_gain": "the land turned greener",
    "urban_growth": "land was cleared or built over (this may be bare ground, not buildings)",
    "burn": "green cover was lost suddenly, as after a fire",
    "other": "the ground changed: built-up or bare ground disappeared without turning green",
}

LOCAL_RULE = (
    "This change is a small patch of about {area} hectares, not the whole area. Only write a "
    "claim if a quote names a specific site, building, plant, airport, track, road or project "
    "at this place and says what was done to it in these years (built, cleared, demolished, "
    "planted, graded). Never use a quote about the whole town, region, economy or a general trend."
)
PLACE_RULE = (
    "This change covers a large part of the area, so quotes about the place as a whole are fine."
)

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"page": {"type": "string"}, "text": {"type": "string"}},
                "required": ["page", "text"],
            },
        },
    },
    "required": ["quotes"],
}

COMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "quote_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "quote_ids"],
            },
        },
    },
    "required": ["claims"],
}

# Capitalised words that start sentences or are plain English, not names (TR-6 name check).
COMMON_CAPITALS = {"The", "A", "An", "In", "On", "At", "By", "From", "During", "After",
                   "Before", "Between", "Its", "It", "This", "These", "Records", "Work",
                   "Construction", "Crews", "Officials", "Plans", "Part", "Parts"}

NUMBER_PATTERN = re.compile(r"\d[\d,.]*")
CAPITAL_WORD_PATTERN = re.compile(r"\b[A-Z][A-Za-z'\-]*")


_last_call_at = 0.0


# --- Model calls ---


async def call_model(prompt_name: str, variables: dict, schema: dict, untrusted: set[str],
                     *, trace, budget) -> dict:
    """llm.complete_json, spaced out, with one longer wait and retry after a rate limit.
    llm.py's own backoff (seconds) is too short when several jobs share one quota."""
    global _last_call_at
    for attempt in range(2):
        wait = MIN_GAP_S - (time.monotonic() - _last_call_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_at = time.monotonic()
        try:
            return await llm.complete_json(prompt_name, variables, schema, trace=trace,
                                           budget=budget, untrusted=untrusted)
        except llm.LLMError as error:
            if "429" not in str(error) or attempt == 1:
                raise
            await asyncio.sleep(RATE_LIMIT_WAIT_S)
    raise llm.LLMError(f"{prompt_name}: rate limited")


# --- Step 1: trimming pages ---


def mention_starts(text: str, place_names: list[str]) -> list[int]:
    """Character offsets where one of the place names appears as whole words."""
    starts = []
    for name in place_names:
        if len(name.strip()) < 3:
            continue
        for match in verify.name_pattern(name).finditer(text):
            starts.append(match.start())
    return sorted(starts)


def merged_windows(starts: list[int], text_length: int) -> list[tuple[int, int]]:
    """Windows of WINDOW_CHARS around each mention, overlapping ones merged."""
    windows: list[tuple[int, int]] = []
    for start in starts:
        low = max(0, start - WINDOW_CHARS)
        high = min(text_length, start + WINDOW_CHARS)
        if windows and low <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], high))
        else:
            windows.append((low, high))
    return windows


def mentions_change_years(window: str, start_year: int, end_year: int) -> bool:
    """True if the window states a year inside the change's years ±1."""
    for first, last in verify.stated_year_spans(window):
        if verify.overlaps(first, last, start_year, end_year):
            return True
    return False


def trim_page(text: str, place_names: list[str], start_year: int, end_year: int) -> str:
    """The page cut down to the windows around place names, windows that state the change's
    years first, at most MAX_PAGE_CHARS. Empty when the page never names the place."""
    page = verify.normalise(text)
    windows = merged_windows(mention_starts(page, place_names), len(page))
    with_years = []
    without_years = []
    for low, high in windows:
        window = page[low:high]
        if mentions_change_years(window, start_year, end_year):
            with_years.append((low, window))
        else:
            without_years.append((low, window))

    kept = []
    used_chars = 0
    for low, window in with_years + without_years:
        if used_chars + len(window) > MAX_PAGE_CHARS:
            continue
        kept.append((low, window))
        used_chars += len(window)
    kept.sort()  # back into page order, so the model reads the page as written
    pieces = []
    for _, window in kept:
        pieces.append(window)
    return "\n[...]\n".join(pieces)


def trimmed_pages(results: list[dict], place_names: list[str], change: dict) -> list[dict]:
    """Pages that name the place, each with an id P1, P2... and its trimmed text."""
    pages = []
    for result in results:
        trimmed = trim_page(result.get("text") or "", place_names,
                            change["start_year"], change["end_year"])
        if not trimmed:
            continue
        page_id = f"P{len(pages) + 1}"
        pages.append({"page_id": page_id, "result": result, "trimmed": trimmed})
    return pages


def batches_of(pages: list[dict]) -> list[list[dict]]:
    """Groups pages so each extraction call carries at most MAX_BATCH_CHARS of page text."""
    batches: list[list[dict]] = []
    batch_chars = 0
    for page in pages:
        size = len(page["trimmed"])
        if not batches or batch_chars + size > MAX_BATCH_CHARS:
            batches.append([])
            batch_chars = 0
        batches[-1].append(page)
        batch_chars += size
    return batches


# --- Step 2: extracting quotes ---


def change_words(change: dict) -> str:
    """What the satellite shows for this change, and where."""
    words = KIND_WORDS.get(change["kind"], "the land changed")
    region = (change.get("region") or {}).get("description") or ""
    return f"{words}, {region}".strip(", ")


def scope_words(change: dict) -> str:
    """The size rule for this change: small local patch or most of the area."""
    if change.get("scope") == "local":
        area = round(change.get("area_ha") or 0)
        return LOCAL_RULE.format(area=area)
    return PLACE_RULE


def pages_text(batch: list[dict]) -> str:
    """The batch's trimmed pages as one block, each page labelled by id, title and date."""
    blocks = []
    for page in batch:
        result = page["result"]
        header = (f"=== {page['page_id']} | {result.get('title') or ''} | "
                  f"{result.get('domain') or ''} | {result.get('published_date') or ''} ===")
        blocks.append(header + "\n" + page["trimmed"])
    return "\n\n".join(blocks)


async def extract_quotes(place: dict, place_names: list[str], change: dict,
                         batch: list[dict], *, trace, budget) -> list[dict]:
    """One model call: exact quotes from this batch of pages. Returns {page, text} items."""
    variables = {
        "place_name": place["display_name"],
        "place_names": ", ".join(place_names),
        "start_year": change["start_year"],
        "end_year": change["end_year"],
        "change_words": change_words(change),
        "scope_words": scope_words(change),
        "pages": pages_text(batch),
    }
    reply = await call_model("extract_quotes", variables, EXTRACT_SCHEMA, {"pages"},
                             trace=trace, budget=budget)
    return reply["quotes"]


# --- Step 3: verifying quotes in code ---


def quote_problem(text: str, result: dict, place_names: list[str], change: dict) -> str | None:
    """TR-2/3/4 on one quote: on the page word for word, names the place, fits the years.
    Returns None when it passes, else a short reason for the trace."""
    if len(text) > MAX_EXCERPT_CHARS:
        return "too long"
    if not verify.excerpt_in_page(text, result.get("text") or ""):
        return "not on page"
    if not verify.names_place(text, place_names):
        return "no place name"
    fits = verify.in_years(text, result.get("published_date"),
                           change["start_year"], change["end_year"])
    if not fits:
        return "other years"
    return None


SENTENCE_END = re.compile(r"[.!?][\"')\]]?\s")
SENTENCE_CLOSERS = ".!?\"')]"
MAX_NEIGHBOUR_CHARS = 300   # a longer "sentence" is usually page clutter (dates, menus)


def ends_sentence(text: str) -> bool:
    """True if the text ends like a sentence (full stop, ! or ?, maybe a closing quote)."""
    return bool(text) and text[-1] in SENTENCE_CLOSERS


def neighbour_sentences(text: str, page: str) -> tuple[str, str]:
    """(sentence before, sentence after) the quote in the normalised page, or "" for none.
    Only whole sentences: a quote that starts or ends mid-sentence gets no neighbour there."""
    start = page.find(text)
    if start < 0:
        return "", ""
    before = page[:start].rstrip()
    previous = ""
    if ends_sentence(before):
        boundaries = list(SENTENCE_END.finditer(before))
        previous_start = boundaries[-1].end() if boundaries else 0
        previous = before[previous_start:]
    after = page[start + len(text):].lstrip() + " "
    following = ""
    next_end = SENTENCE_END.search(after)
    if ends_sentence(text) and next_end:
        following = after[:next_end.end()].strip()
    return previous, following


def clean_neighbour(sentence: str) -> bool:
    """True for a short, plain sentence: no page headings (#) and not overly long."""
    return bool(sentence) and len(sentence) <= MAX_NEIGHBOUR_CHARS and "#" not in sentence


def widen_to_name(text: str, result: dict, place_names: list[str]) -> str | None:
    """The quote plus the sentence before or after it, when that neighbour names the place.
    Still a word-for-word piece of the page, so TR-2/3 are checked on the wider text as is."""
    page = verify.normalise(result.get("text") or "")
    previous, following = neighbour_sentences(text, page)
    if not clean_neighbour(previous):
        previous = ""
    if not clean_neighbour(following):
        following = ""
    candidates = []
    if previous:
        candidates.append(previous + " " + text)
    if following:
        candidates.append(text + " " + following)
    if previous and following:
        candidates.append(previous + " " + text + " " + following)
    for candidate in candidates:
        fits = len(candidate) <= MAX_EXCERPT_CHARS
        if fits and verify.names_place(candidate, place_names):
            return candidate
    return None


def verified_quotes(raw_quotes: list[dict], pages: list[dict], place_names: list[str],
                    change: dict, rejected: list[str]) -> list[dict]:
    """The quotes that pass every check, de-duplicated, each as {url, text, result}.
    The reason for each rejected quote is added to `rejected`."""
    pages_by_id = {}
    for page in pages:
        pages_by_id[page["page_id"]] = page
    kept = []
    seen = set()
    for quote in raw_quotes:
        page = pages_by_id.get(quote["page"].strip())
        text = verify.normalise(quote["text"])
        if page is None or not text:
            rejected.append("unknown page")
            continue
        result = page["result"]
        problem = quote_problem(text, result, place_names, change)
        if problem == "no place name":
            widened = widen_to_name(text, result, place_names)
            if widened and quote_problem(widened, result, place_names, change) is None:
                text = widened
                problem = None
        if problem is not None:
            rejected.append(f"{problem}: {text[:60]}")
            continue
        key = (result["url"], text)
        if key in seen:
            continue
        seen.add(key)
        kept.append({"url": result["url"], "text": text, "result": result})
    return drop_contained(kept)


def drop_contained(quotes: list[dict]) -> list[dict]:
    """Drops a quote whose text sits inside a longer quote from the same page."""
    kept = []
    for quote in quotes:
        inside_other = False
        for other in quotes:
            same_page = other["url"] == quote["url"]
            longer = len(other["text"]) > len(quote["text"])
            if same_page and longer and quote["text"] in other["text"]:
                inside_other = True
        if not inside_other:
            kept.append(quote)
    return kept


# --- Step 4: composing claims ---


def quotes_text(quotes: list[dict]) -> str:
    """The verified quotes as a numbered list with their source and date."""
    lines = []
    for number, quote in enumerate(quotes, start=1):
        result = quote["result"]
        lines.append(f"q{number} ({result.get('domain')}, {result.get('published_date')}): "
                     f"\"{quote['text']}\"")
    return "\n".join(lines)


async def compose_claims(place: dict, change: dict, quotes: list[dict],
                         *, trace, budget) -> list[dict]:
    """One model call: 0-3 claims from the verified quotes. Returns {text, quote_ids} items."""
    variables = {
        "place_name": place["display_name"],
        "start_year": change["start_year"],
        "end_year": change["end_year"],
        "change_words": change_words(change),
        "scope_rule": scope_words(change),
        "quotes": quotes_text(quotes),
    }
    reply = await call_model("compose_claims", variables, COMPOSE_SCHEMA, {"quotes"},
                             trace=trace, budget=budget)
    return reply["claims"][:MAX_CLAIMS]


# --- Step 5: checking claims in code ---


def story_numbers(change: dict) -> set[str]:
    """Numbers the claim may take from the story's own data (TR-6): years and area."""
    allowed = {str(change["start_year"]), str(change["end_year"])}
    area = change.get("area_ha")
    if area:
        allowed.add(str(round(area)))
    return allowed


def numbers_supported(claim: str, evidence: str, change: dict) -> bool:
    """Every number in the claim appears in its quotes or in the story's data (TR-6)."""
    allowed = story_numbers(change)
    for number in NUMBER_PATTERN.findall(claim):
        number = number.rstrip(".,")
        alone = r"(?<![\d.,])" + re.escape(number) + r"(?![\d])"  # "4" must not match "2014"
        if number in allowed or re.search(alone, evidence):
            continue
        return False
    return True


def story_names(place: dict) -> str:
    """The place's own names from the story (display name, city, county, state, country)."""
    admin = place.get("admin") or {}
    parts = [place.get("display_name") or ""]
    for field in ("city", "county", "state", "country"):
        parts.append(admin.get(field) or "")
    return " ".join(parts)


def names_supported(claim: str, evidence: str, place: dict) -> bool:
    """Every capitalised word in the claim appears in its quotes or the story's place data
    (TR-6). Compared ignoring case, so a sentence-initial "Crews" matches "crews"."""
    known = (evidence + " " + story_names(place)).lower()
    for word in CAPITAL_WORD_PATTERN.findall(claim):
        if word in COMMON_CAPITALS:
            continue
        if not re.search(r"(?<!\w)" + re.escape(word.lower()) + r"(?!\w)", known):
            return False
    return True


def checked_claims(raw_claims: list[dict], quotes: list[dict], place: dict,
                   change: dict, dropped: list[str]) -> list[dict]:
    """Claims that cite existing quotes and add no outside numbers or names.
    Returns {text, quotes} items, where quotes are the verified quote dicts it rests on.
    The reason for each dropped claim is added to `dropped`."""
    kept = []
    for claim in raw_claims:
        text = verify.normalise(claim["text"])
        if not text or len(text) > MAX_CLAIM_CHARS:
            dropped.append(f"empty or too long: {text[:80]}")
            continue
        cited = []
        for quote_id in claim["quote_ids"]:
            match = re.fullmatch(r"q(\d+)", quote_id.strip())
            if match is None:
                continue
            index = int(match.group(1)) - 1
            if 0 <= index < len(quotes) and quotes[index] not in cited:
                cited.append(quotes[index])
        if not cited:
            dropped.append(f"no valid quote ids: {text[:80]}")
            continue  # TR-1: no evidence, no claim
        evidence = " ".join(quote["text"] for quote in cited)
        if not numbers_supported(text, evidence, change):
            dropped.append(f"number not in quotes: {text[:80]}")
            continue
        if not names_supported(text, evidence, place):
            dropped.append(f"name not in quotes: {text[:80]}")
            continue
        kept.append({"text": text, "quotes": cited})
    return kept


# --- One change, end to end ---


def batches_per_change(budget, change_count: int) -> int:
    """How many extraction calls each change may make, so that every change keeps one
    compose call and a little slack for retries within the HL-10 budget."""
    spare = budget.remaining("llm") - change_count - RETRY_SLACK
    return max(1, spare // max(1, change_count))


async def explain_change(place: dict, place_names: list[str], change: dict,
                         results: list[dict],
                         max_batches: int, *, trace, budget) -> list[dict]:
    """Extract, verify, compose and check for one change. Returns checked claims."""
    pages = trimmed_pages(results, place_names, change)
    if not pages:
        return []
    raw_quotes = []
    for batch in batches_of(pages)[:max_batches]:
        raw_quotes += await extract_quotes(place, place_names, change, batch,
                                           trace=trace, budget=budget)
    rejected: list[str] = []
    quotes = verified_quotes(raw_quotes, pages, place_names, change, rejected)
    trace.log("check", f"verify quotes {change['id']}",
              {"raw_quotes": len(raw_quotes)}, {"verified": len(quotes), "rejected": rejected})
    if not quotes:
        return []  # TR-8: nothing verified, so the chapter stays cause unknown
    raw_claims = await compose_claims(place, change, quotes, trace=trace, budget=budget)
    dropped: list[str] = []
    claims = checked_claims(raw_claims, quotes, place, change, dropped)
    trace.log("check", f"check claims {change['id']}",
              {"raw_claims": len(raw_claims)}, {"kept": len(claims), "dropped": dropped})
    return claims


async def safe_explain_change(*args, trace, budget) -> list[dict]:
    """explain_change, but a used-up budget or failed model call leaves the change unknown."""
    try:
        return await explain_change(*args, trace=trace, budget=budget)
    except (BudgetExceeded, llm.LLMError) as error:
        trace.log("check", "change left cause_unknown", outputs={"why": str(error)[:200]})
        return []


# --- Step 6: ids, sources and confidence ---


def published_day(result: dict) -> str | None:
    """The page's date as YYYY-MM-DD, or None (the schema wants a plain date)."""
    value = result.get("published_date") or ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value[:10]):
        return value[:10]
    return None


class SourceBook:
    """Hands out src-NN / ex-NN-MM ids and keeps one source per URL across the story."""

    def __init__(self):
        self.sources: list[dict] = []
        self.by_url: dict[str, dict] = {}
        self.retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def source_for(self, result: dict) -> dict:
        """The source item for a page, created the first time it is used."""
        url = result["url"]
        if url not in self.by_url:
            source = {
                "id": f"src-{len(self.sources) + 1:02d}",
                "url": url,
                "title": result.get("title") or "",
                "publisher": None,
                "domain": result.get("domain") or "",
                "published_date": published_day(result),
                "authority": verify.authority(result.get("domain") or ""),
                "retrieved_at": self.retrieved_at,
                "excerpts": [],
            }
            self.sources.append(source)
            self.by_url[url] = source
        return self.by_url[url]

    def excerpt_id(self, quote: dict) -> tuple[str, dict]:
        """(excerpt id, source) for a verified quote; the same text reuses the same id."""
        source = self.source_for(quote["result"])
        for excerpt in source["excerpts"]:
            if excerpt["text"] == quote["text"]:
                return excerpt["id"], source
        number = len(source["excerpts"]) + 1
        excerpt_id = f"ex-{source['id'][4:]}-{number:02d}"
        source["excerpts"].append({"id": excerpt_id, "text": quote["text"], "verified": True})
        return excerpt_id, source


def chapter_answer(claims: list[dict], book: SourceBook, next_claim: int) -> tuple[dict, int]:
    """The chapter's claims with story ids, and its rule-based confidence (TR-7)."""
    story_claims = []
    chapter_sources = {}
    for claim in claims:
        excerpt_ids = []
        for quote in claim["quotes"]:
            excerpt_id, source = book.excerpt_id(quote)
            excerpt_ids.append(excerpt_id)
            chapter_sources[source["id"]] = source
        story_claims.append({"id": f"cl-{next_claim:02d}", "text": claim["text"],
                             "excerpt_ids": excerpt_ids})
        next_claim += 1
    if story_claims:
        level = verify.confidence(list(chapter_sources.values()))
    else:
        level = "cause_unknown"
    return {"confidence": level, "claims": story_claims}, next_claim


# --- Public ---


async def explain(place_input, *, trace, budget) -> dict:
    """Finds documented, code-verified causes for each change of one place."""
    story = place_input.story
    place = story["place"]
    place_names = verify.place_names_for(place)
    changes_by_id = {}
    for change in story["changes"]:
        changes_by_id[change["id"]] = change
    ordered_ids = []
    for chapter in story["chapters"]:
        if chapter["kind"] == "change":
            ordered_ids.append(chapter["change_id"])

    saved_prompts_dir = llm.PROMPTS_DIR
    llm.PROMPTS_DIR = PROMPTS_DIR  # this spike's prompts live in its own folder
    try:
        max_batches = batches_per_change(budget, len(ordered_ids))
        claims_per_change = []
        # One change at a time: parallel bursts hit the provider's rate limit, and each
        # 429 retry still spends HL-10 budget.
        for change_id in ordered_ids:
            results = place_input.results_by_change.get(change_id, [])
            claims = await safe_explain_change(place, place_names, changes_by_id[change_id],
                                               results, max_batches, trace=trace, budget=budget)
            claims_per_change.append(claims)
    finally:
        llm.PROMPTS_DIR = saved_prompts_dir

    book = SourceBook()
    chapters = {}
    next_claim = 1
    for change_id, claims in zip(ordered_ids, claims_per_change):
        chapters[change_id], next_claim = chapter_answer(claims, book, next_claim)
    return {"sources": book.sources, "chapters": chapters}
