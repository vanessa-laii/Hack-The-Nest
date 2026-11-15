# Cause agent (S4 winner, ADR-004): search, extract exact quotes, verify them in code, compose.
# Inputs: story folder, the story's place dict, its changes[]; Trace + Budget (HL-10); emit.
# Outputs: find_causes() → {sources, chapters, claim_confidence, checks, usage, warnings}
#
# How it works:
#   1. Search: each change gets 2 cause-neutral Exa searches from a fixed template (the same one
#      spikes/s4-causes/record.py recorded with, so recorded queries are free cache hits).
#   2. Trim each page to the windows around the place's names: a quote that doesn't name the
#      place can never pass TR-3, so the rest of the page is only cost. Pages with no name drop.
#   3. Extract: one model call per batch of trimmed pages asks for exact quotes only, no claims.
#      The pages are wrapped as untrusted text (TR-9).
#   4. Verify in code: each quote must be on its page word for word (TR-2), name the place
#      (TR-3) and fit the change's years ±1 (TR-4). Failures are dropped; repeats are merged.
#   5. Compose: one call per change sees ONLY the verified quotes and writes 0-3 claims, each
#      citing quote ids. "Cause unknown" is an allowed answer (TR-8).
#   6. Check claims in code: cited ids exist, every number and quantity word is in the claim's
#      own quotes, every name is in the quotes or the place data (TR-6), and the claim is about
#      the ground, not about the imagery or the web page.
#   7. Checker (TR-5): a separate call per change asks, claim by claim, whether its own quotes
#      state every part of it, for this place and these years. Any "no" drops the claim; a
#      failed checker call drops all of that change's claims.
#   8. One rewrite round: claims that failed step 6 or 7 go back to the model once, with the
#      exact problems, and the rewrites go through steps 6 and 7 again. Skipped when the
#      budget is low. Whatever still fails is dropped.
#   9. Number sources, excerpts and claims across the story. Each claim's confidence comes from
#      verify.confidence() over ITS OWN sources (TR-7); the chapter takes its best claim's.
#   A used-up budget, the Exa spend cap or any failure leaves that change cause_unknown, with
#   a warning; it never fails the build or the other changes.
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app import llm
from app.agent import search as exa_search
from app.agent import verify
from app.agent.trace import Budget, BudgetExceeded, Trace

# --- Constants ---

NUM_RESULTS = 8             # Exa results per search (the recording used 8)
WINDOW_CHARS = 700          # text kept on each side of a place-name mention
MAX_PAGE_CHARS = 5000       # trimmed text per page, at most
MAX_BATCH_CHARS = 30_000    # trimmed page text per call; long prompts made the model
                            # return no quotes at all (Palm Jumeirah, 45k chars)
MAX_EXCERPT_CHARS = 600     # story schema limit
MAX_CLAIM_CHARS = 240       # story schema limit
MAX_CLAIMS = 3
RETRY_SLACK = 3             # calls kept back for llm.py's one retry on an invalid reply
CALLS_PER_CHANGE = 2        # compose + checker, on top of the extraction batches
REWRITE_CALLS = 2           # one rewrite call + one checker call on the rewrites

# Search wording: what the change looks like, never why (no cause words in the query).
# Neutral about lake vs coast: "water area shrank" fits Lake Mead and Palm Jumeirah alike.
SEARCH_KIND_WORDS = {
    "water_loss": "water area shrank",
    "water_gain": "water area grew",
    "vegetation_loss": "vegetation loss",
    "vegetation_gain": "land turned greener",
    "urban_growth": "land cleared or built on",
    "burn": "burned area",
    "other": "land change",
}

# What the metric measures, for the second, question-style query. Exa searches by meaning,
# so a natural question finds news and reports better than a keyword list.
FEATURE_WORDS = {
    "water_frac": "water and shoreline",
    "ndvi_mean": "land and vegetation",
    "built_frac": "land and buildings",
}

# What the satellite shows, in words, for the prompts. Urban growth is often bare cleared
# ground (ADR-003).
PROMPT_KIND_WORDS = {
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

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "reason": {"type": "string"},
                    "supported": {"type": "string", "enum": ["yes", "no"]},
                },
                "required": ["claim", "reason", "supported"],
            },
        },
    },
    "required": ["verdicts"],
}
CHECK_ID_PATTERN = re.compile(r"\bc(\d+)\b", re.IGNORECASE)   # "c1", "C1" and "claim c1" are c1

# Confidence levels from weakest to strongest (TR-7), to pick a chapter's best claim.
CONFIDENCE_ORDER = ["cause_unknown", "likely", "confirmed"]

# Capitalised words that start sentences or are plain English, not names (TR-6 name check).
COMMON_CAPITALS = {"The", "A", "An", "In", "On", "At", "By", "From", "During", "After",
                   "Before", "Between", "Its", "It", "This", "These", "Records", "Work",
                   "Construction", "Crews", "Officials", "Plans", "Part", "Parts"}
CAPITAL_WORD_PATTERN = re.compile(r"\b[A-Z][A-Za-z'\-]*")

# Numbers written as words, for the number rule (ADR-004). "thirty-three" is read as 33, so it
# matches a quote that says "33", and the other way round.
UNIT_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
TENS_WORDS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
# "hundreds of homes" is a number too: it must not turn into "thousands" in a claim.
SCALE_WORDS = {
    "hundred": 100, "hundreds": 100, "thousand": 1000, "thousands": 1000,
    "million": 1_000_000, "millions": 1_000_000, "billion": 1_000_000_000,
    "billions": 1_000_000_000, "dozen": 12, "dozens": 12,
}
# Ordinals claim an order ("the first family", "the second-largest"), so they count as well.
ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
# Quantity and superlative words state a fact just like a number does: "the lake fell by half",
# "a record low", "its lowest level". Each must be in the claim's own quotes (ADR-004). Word
# forms that say the same thing share one fact ("twice" and "doubled"). "third" is already an
# ordinal above, and "all-time" is read as the two tokens "all" "time" (see numbers_in).
QUANTITY_WORDS = {
    "half": "half", "halved": "half", "halve": "half",
    "twice": "double", "double": "double", "doubled": "double",
    "triple": "triple", "tripled": "triple", "thrice": "triple",
    "quarter": "quarter", "quarters": "quarter",
    "percent": "percent", "%": "percent", "percentage": "percent",
    "record": "record", "lowest": "lowest", "highest": "highest",
}
# Digits ("1,200", "3.5", "1st"), one lower-case word, or one punctuation mark (it ends a run
# of number words: "two; hundreds" is 2 and 100). Hyphens are skipped, so "thirty-three" is 33.
NUMBER_TOKEN_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?(?:st|nd|rd|th)?|[a-z]+|[^\sa-z\d\-]")

# Meta claims (ADR-004): a claim that only talks about the pictures or the web page is true
# but tells nothing about what happened on the ground ("Images of Lake Mead were acquired on
# July 21, 2014"). The film already shows the imagery, so any claim with these words is dropped.
# Deliberately left out:
#   - bare "acquired": buying land is a real event ("the city acquired the site in 1995");
#     only "acquired on" (a capture date) is listed, and image claims carry an image word too.
#   - "site": "the site was cleared" is a ground event.
#   - "report"/"study" with "a": "a 2019 report found …" still states an event.
#   - bare "Sentinel": it is also a newspaper and a place name ("Sentinel Dome"); only the
#     satellites' numbered names (Sentinel-1, Sentinel-2) count. "MODIS" only in capitals.
#   - "mapped"/"mapping": surveying land can be part of a real project. "map" itself is
#     listed, at the cost of figures of speech ("wiped off the map").
#   - "Landsat" has no sense other than the satellite programme, so it stays as is. Names
#     that only contain it are not matched: "Landsatz" (German for clause) passes.
META_PATTERNS = [
    re.compile(r"\bimage(s|ry|d)?\b", re.IGNORECASE),
    re.compile(r"\bimager\b", re.IGNORECASE),             # "Operational Land Imager"
    re.compile(r"\bphoto(s|graph|graphs|graphed)?\b", re.IGNORECASE),
    re.compile(r"\bpictured\b", re.IGNORECASE),
    re.compile(r"\bsatellites?\b", re.IGNORECASE),
    re.compile(r"\blandsat\b", re.IGNORECASE),
    re.compile(r"\bastronauts?\b", re.IGNORECASE),
    re.compile(r"\bpixels?\b", re.IGNORECASE),
    re.compile(r"\b(true|false)[- ]colou?r\b", re.IGNORECASE),
    re.compile(r"\baerial view\b", re.IGNORECASE),
    re.compile(r"\bscreenshots?\b", re.IGNORECASE),
    re.compile(r"\bacquired on\b", re.IGNORECASE),
    re.compile(r"\bmaps?\b", re.IGNORECASE),
    re.compile(r"\b(thematic )?mapper\b", re.IGNORECASE),   # "Thematic Mapper" (Landsat 5)
    re.compile(r"\bpictures?\b", re.IGNORECASE),
    re.compile(r"\bcameras?\b", re.IGNORECASE),
    re.compile(r"\bMODIS\b"),                              # capitals only: "Modis" is a name
    re.compile(r"\bSentinel[- ]?\d"),                      # Sentinel-2, not "Sentinel Dome"
    re.compile(r"\b(this|the) (article|web ?page|page|post|blog)\b", re.IGNORECASE),
    re.compile(r"\bthis (report|story|website|video)\b", re.IGNORECASE),
]

Emit = Callable[..., Awaitable[None]]


# --- Step 1: search ---


def short_name(place: dict) -> str:
    """'Lake Mead, Nevada' stays; 'Palm Jumeirah, United Arab Emirates' -> 'Palm Jumeirah, Dubai'.
    Search engines match a place + city better than a place + long country name."""
    name = place["display_name"]
    admin = place.get("admin", {})
    if admin.get("country_code") == "us":
        return name
    first = name.split(",")[0].strip()
    local = admin.get("city") or admin.get("state")
    return f"{first}, {local}" if local and local != first else first


def query_years(change: dict) -> tuple[int, int]:
    """The change's years widened by one on each side, never past this year."""
    first = change["start_year"] - 1
    last = min(change["end_year"] + 1, date.today().year)
    return first, last


def plan_queries(place: dict, change: dict) -> list[dict]:
    """Two fixed, cause-neutral queries for one change: {change_id, query, start_year,
    end_year}. spikes/s4-causes/record.py uses this too, so the cache keys match."""
    name = short_name(place)
    kind_words = SEARCH_KIND_WORDS.get(change["kind"], "land change")
    feature_words = FEATURE_WORDS.get(change["metric"], "land")
    first, last = query_years(change)
    start, end = change["start_year"], change["end_year"]
    texts = [
        f"{name} {kind_words} {start}-{end}",
        f"What happened to the {feature_words} in {name} between {start} and {end}?",
    ]
    planned = []
    for text in texts:
        planned.append({"change_id": change["id"], "query": text, "start_year": first,
                        "end_year": last})
    return planned


async def search_change(place: dict, change: dict, *, trace: Trace,
                        budget: Budget) -> list[dict]:
    """The pages found by the change's 2 searches, de-duplicated by URL. Cached requests are
    free; a live one goes through search.py's HL-10 budget and project spend cap."""
    seen_urls = set()
    pages = []
    for query in plan_queries(place, change):
        found = await exa_search.search(
            query["query"], start_year=query["start_year"], end_year=query["end_year"],
            num_results=NUM_RESULTS, trace=trace, budget=budget,
            purpose=f"cause search {change['id']}",
        )
        for result in found:
            if result["url"] not in seen_urls:
                seen_urls.add(result["url"])
                pages.append(result)
    return pages


# --- Step 2: trimming pages ---


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


# --- Step 3: extracting quotes ---


def change_words(change: dict) -> str:
    """What the satellite shows for this change, and where."""
    words = PROMPT_KIND_WORDS.get(change["kind"], "the land changed")
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
                         batch: list[dict], *, trace: Trace, budget: Budget) -> list[dict]:
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
    reply = await llm.complete_json("extract_quotes", variables, EXTRACT_SCHEMA, trace=trace,
                                    budget=budget, untrusted={"pages"})
    return reply["quotes"]


# --- Step 4: verifying quotes in code ---


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


# --- Step 5: composing claims ---


def quotes_text(quotes: list[dict]) -> str:
    """The verified quotes as a numbered list with their source and date."""
    lines = []
    for number, quote in enumerate(quotes, start=1):
        result = quote["result"]
        lines.append(f"q{number} ({result.get('domain')}, {result.get('published_date')}): "
                     f"\"{quote['text']}\"")
    return "\n".join(lines)


async def compose_claims(place: dict, change: dict, quotes: list[dict],
                         *, trace: Trace, budget: Budget) -> list[dict]:
    """One model call: 0-3 claims from the verified quotes. Returns {text, quote_ids} items."""
    variables = {
        "place_name": place["display_name"],
        "start_year": change["start_year"],
        "end_year": change["end_year"],
        "change_words": change_words(change),
        "scope_rule": scope_words(change),
        "quotes": quotes_text(quotes),
    }
    reply = await llm.complete_json("compose_claims", variables, COMPOSE_SCHEMA, trace=trace,
                                    budget=budget, untrusted={"quotes"})
    return reply["claims"][:MAX_CLAIMS]


# --- Step 6: checking claims in code ---


def digit_value(token: str) -> str:
    """'1,200' -> '1200', '3.50' -> '3.5', '2018' -> '2018': one spelling per value."""
    plain = token.replace(",", "")
    if "." in plain:
        plain = plain.rstrip("0").rstrip(".")
    return plain


def number_word_value(words: list[str]) -> int:
    """The value of a run of number words: ['thirty', 'three'] -> 33,
    ['two', 'hundred', 'fifty'] -> 250, ['hundreds'] -> 100."""
    total = 0
    current = 0
    for word in words:
        if word in UNIT_WORDS:
            current += UNIT_WORDS[word]
        elif word in TENS_WORDS:
            current += TENS_WORDS[word]
        elif word in ("hundred", "hundreds"):
            current = max(current, 1) * 100
        else:  # thousand, million, billion, dozen
            total += max(current, 1) * SCALE_WORDS[word]
            current = 0
    return total + current


def is_number_word(word: str) -> bool:
    """True for a cardinal number word (units, tens, hundred, thousand, ...)."""
    return word in UNIT_WORDS or word in TENS_WORDS or word in SCALE_WORDS


def numbers_in(text: str) -> set[str]:
    """Every number a text states, each in one spelling: digits and words alike ("33" and
    "thirty-three" both give "33"), "1.5 million" as "1500000", ordinals as "ordinal 1",
    quantity words as "word half", "word record", "word all-time"."""
    tokens = NUMBER_TOKEN_PATTERN.findall(text.lower())
    found = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        two_words = " ".join(tokens[index:index + 2])
        if two_words in ("all time", "per cent"):   # "all-time", "per cent"
            found.add("word " + ("all-time" if two_words == "all time" else "percent"))
            index += 2
            continue
        if token in QUANTITY_WORDS:
            found.add(f"word {QUANTITY_WORDS[token]}")
            index += 1
            continue
        if token in ORDINAL_WORDS:
            found.add(f"ordinal {ORDINAL_WORDS[token]}")
            index += 1
            continue
        ordinal = re.fullmatch(r"(\d+)(st|nd|rd|th)", token)
        if ordinal:
            found.add(f"ordinal {int(ordinal.group(1))}")
            index += 1
            continue
        if token[0].isdigit():
            value = digit_value(token.rstrip(","))
            # "1.5 million": the scale word belongs to the digits
            if index + 1 < len(tokens) and tokens[index + 1] in SCALE_WORDS:
                scaled = float(value) * SCALE_WORDS[tokens[index + 1]]
                value = digit_value(f"{scaled:.6f}")
                index += 1
            found.add(value)
            index += 1
            continue
        if is_number_word(token):
            run, index = number_word_run(tokens, index)
            found.add(str(number_word_value(run)))
            continue
        index += 1
    return found


def number_word_run(tokens: list[str], index: int) -> tuple[list[str], int]:
    """The number words starting at tokens[index], and the index just after them.
    "and" counts only after hundred/thousand/... ("two hundred and fifty"), so "one and two"
    stays two numbers."""
    run = []
    while index < len(tokens):
        word = tokens[index]
        if is_number_word(word):
            run.append(word)
            index += 1
            continue
        after_scale = bool(run) and run[-1] in SCALE_WORDS
        next_is_number = index + 1 < len(tokens) and is_number_word(tokens[index + 1])
        if word == "and" and after_scale and next_is_number:
            index += 1
            continue
        break
    return run, index


def numbers_supported(claim: str, quotes: list[str]) -> bool:
    """ADR-004 number rule (TR-6): every number in the claim, years included, appears in at
    least one of its own quotes. The change's own years are NOT allowed as a free pass: a
    claim must not date an event its quotes don't date."""
    allowed: set[str] = set()
    for quote in quotes:
        allowed |= numbers_in(quote)
    return numbers_in(claim) <= allowed


def is_meta_claim(claim: str) -> bool:
    """True when the claim talks about the imagery or the web page (META_PATTERNS)
    instead of something that happened on the ground."""
    for pattern in META_PATTERNS:
        if pattern.search(claim):
            return True
    return False


def story_names(place: dict) -> str:
    """The place's own names from the story (display name, city, county, state, country)."""
    admin = place.get("admin") or {}
    parts = [place.get("display_name") or ""]
    for field in ("city", "county", "state", "country"):
        parts.append(admin.get(field) or "")
    return " ".join(parts)


def names_supported(claim: str, evidence: str, place: dict) -> bool:
    """Every capitalised word in the claim appears in its quotes or the story's place data
    (TR-6). Compared ignoring case, so a sentence-initial "Crews" matches "crews".
    A possessive is checked without its "'s": "Mead's" needs "Mead" in the quotes."""
    known = (evidence + " " + story_names(place)).lower()
    for word in CAPITAL_WORD_PATTERN.findall(claim):
        if word.endswith("'s"):
            word = word[:-2]
        if word in COMMON_CAPITALS:
            continue
        if not re.search(r"(?<!\w)" + re.escape(word.lower()) + r"(?!\w)", known):
            return False
    return True


def cited_quotes(quote_ids: list[str], quotes: list[dict]) -> list[dict]:
    """The verified quotes a claim cites by id (q1, q2...). Unknown ids are skipped."""
    cited = []
    for quote_id in quote_ids:
        match = re.fullmatch(r"q(\d+)", quote_id.strip())
        if match is None:
            continue
        index = int(match.group(1)) - 1
        if 0 <= index < len(quotes) and quotes[index] not in cited:
            cited.append(quotes[index])
    return cited


def claim_problem(text: str, cited: list[dict], place: dict) -> str | None:
    """Why a claim must be dropped, or None when it passes every code check."""
    if not text or len(text) > MAX_CLAIM_CHARS:
        return "empty or too long"
    if not cited:
        return "no valid quote ids"  # TR-1: no evidence, no claim
    quote_texts = []
    for quote in cited:
        quote_texts.append(quote["text"])
    if not numbers_supported(text, quote_texts):
        return "number not in its quotes"
    if not names_supported(text, " ".join(quote_texts), place):
        return "name not in its quotes"
    if is_meta_claim(text):
        return "about the imagery or the page, not the ground"
    return None


def checked_claims(raw_claims: list[dict], quotes: list[dict], place: dict,
                   dropped: list[str], failed: list[dict] | None = None) -> list[dict]:
    """Claims that pass claim_problem(). Returns {text, quotes} items, where quotes are the
    verified quote dicts the claim rests on. Each drop's reason goes into `dropped`, and the
    claim itself with its problem into `failed` (for the rewrite round)."""
    kept = []
    for claim in raw_claims:
        text = verify.normalise(claim["text"])
        cited = cited_quotes(claim["quote_ids"], quotes)
        problem = claim_problem(text, cited, place)
        if problem is not None:
            dropped.append(f"{problem}: {text[:80]}")
            if failed is not None:
                failed.append({"text": text, "problem": problem})
            continue
        kept.append({"text": text, "quotes": cited})
    return kept


# --- Step 7: the checker, a separate call (TR-5) ---


def claims_for_check(claims: list[dict]) -> str:
    """Each claim with an id (c1, c2...) and ONLY the quotes it cites, with their dates."""
    blocks = []
    for number, claim in enumerate(claims, start=1):
        lines = [f"c{number}: {claim['text']}", "Its quotes:"]
        for quote in claim["quotes"]:
            result = quote["result"]
            lines.append(f"- ({result.get('domain')}, published {result.get('published_date')}) "
                         f"\"{quote['text']}\"")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def verdicts_by_number(verdicts: list[dict]) -> dict[int, dict]:
    """The checker's verdicts keyed by claim number; "c1", "C1" and "claim c1" all mean 1."""
    found = {}
    for verdict in verdicts:
        match = CHECK_ID_PATTERN.search(verdict["claim"])
        if not match:
            continue
        number = int(match.group(1))
        # TR-5: any "no" drops the claim, so a "no" is never overwritten by a later "yes".
        earlier = found.get(number)
        if earlier is not None and earlier["supported"] != "yes":
            continue
        found[number] = verdict
    return found


async def check_claims(place: dict, change: dict, claims: list[dict], checks: dict,
                       *, trace: Trace, budget: Budget) -> list[dict]:
    """One checker call for all of a change's claims (TR-5). Keeps a claim only on an
    explicit "yes"; a "no" or a missing verdict drops it, with its reason in checks.
    LLMError and BudgetExceeded are raised: the caller then drops the claims."""
    variables = {
        "place_name": place["display_name"],
        "start_year": change["start_year"],
        "end_year": change["end_year"],
        "year_low": change["start_year"] - 1,
        "year_high": change["end_year"] + 1,
        "claims": claims_for_check(claims),
    }
    reply = await llm.complete_json("check_claims", variables, CHECK_SCHEMA, trace=trace,
                                    budget=budget, untrusted={"claims"})
    verdicts = verdicts_by_number(reply["verdicts"])
    kept = []
    for number, claim in enumerate(claims, start=1):
        verdict = verdicts.get(number)
        if verdict is not None and verdict["supported"] == "yes":
            kept.append(claim)
            continue
        reason = verdict["reason"] if verdict is not None else "the checker gave no verdict"
        checks["checker_dropped"].append({"text": claim["text"], "reason": reason[:300]})
    return kept


# --- Step 8: one rewrite round ---


def problems_text(problems: list[dict]) -> str:
    """The failed claims and why each failed, as a numbered list for the rewrite prompt."""
    lines = []
    for number, problem in enumerate(problems, start=1):
        lines.append(f"{number}. Claim: \"{problem['text']}\"\n   Problem: {problem['problem']}")
    return "\n".join(lines)


async def rewrite_claims(place: dict, change: dict, quotes: list[dict], problems: list[dict],
                         room: int, *, trace: Trace, budget: Budget) -> list[dict]:
    """One model call: the failed claims rewritten from the same verified quotes, given the
    exact problems. Returns {text, quote_ids} items, at most `room`."""
    variables = {
        "place_name": place["display_name"],
        "start_year": change["start_year"],
        "end_year": change["end_year"],
        "change_words": change_words(change),
        "scope_rule": scope_words(change),
        "quotes": quotes_text(quotes),
        "problems": problems_text(problems),
    }
    reply = await llm.complete_json("rewrite_claims", variables, COMPOSE_SCHEMA, trace=trace,
                                    budget=budget, untrusted={"quotes", "problems"})
    return reply["claims"][:room]


def checker_problems(checks: dict) -> list[dict]:
    """The checker's drops in the rewrite prompt's shape."""
    problems = []
    for dropped in checks["checker_dropped"]:
        reason = dropped["reason"]
        problems.append({"text": dropped["text"],
                         "problem": f"the checker said it is not supported: {reason}"})
    return problems


def can_rewrite(budget: Budget, reserve: int) -> bool:
    """True if a rewrite round (rewrite + checker) still leaves `reserve` calls for the
    changes after this one and the retry slack (HL-10)."""
    return budget.remaining("llm") - REWRITE_CALLS >= reserve + RETRY_SLACK


async def rewrite_round(place: dict, change: dict, quotes: list[dict], kept: list[dict],
                        problems: list[dict], checks: dict,
                        *, trace: Trace, budget: Budget) -> list[dict]:
    """Rewrites the failed claims once, then runs every code check and the checker on the
    rewrites. Returns the rewrites that pass. If either call fails, the rewrites are dropped
    (never kept unchecked); the claims that already passed are kept."""
    room = MAX_CLAIMS - len(kept)
    try:
        raw = await rewrite_claims(place, change, quotes, problems, room,
                                   trace=trace, budget=budget)
        dropped: list[str] = []
        rewrites = checked_claims(raw, quotes, place, dropped)
        checks["code_dropped"] += dropped
        already = {claim["text"] for claim in kept}
        fresh = []
        for claim in rewrites:
            if claim["text"] not in already:
                fresh.append(claim)
        if not fresh:
            return []
        rescued = await check_claims(place, change, fresh, checks, trace=trace, budget=budget)
    except (BudgetExceeded, llm.LLMError) as error:
        trace.log("check", f"rewrite round {change['id']} failed; rewrites dropped",
                  error=llm.describe_error(error))
        return []
    for claim in rescued:
        checks["rescued"].append(claim["text"])
    return rescued


# --- One change, end to end ---


def new_checks() -> dict:
    """What happened to one change's claims, for the trace and the S4 scorer."""
    return {"code_dropped": [], "checker_dropped": [], "rescued": []}


def batches_per_change(budget: Budget, change_count: int) -> int:
    """How many extraction calls each change may make, so that every change keeps its
    compose and checker calls and a little slack for retries within the HL-10 budget."""
    spare = budget.remaining("llm") - CALLS_PER_CHANGE * change_count - RETRY_SLACK
    return max(1, spare // max(1, change_count))


async def change_quotes(place: dict, place_names: list[str], change: dict,
                        results: list[dict], max_batches: int, emit: Emit,
                        *, trace: Trace, budget: Budget) -> list[dict]:
    """Steps 2-4 for one change: trim, extract and verify. Returns the verified quotes."""
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
    await emit("progress", f"{years_label(change)}: {len(quotes)} of {len(raw_quotes)} "
               "quotes matched their pages word for word", stage="verify", status="running")
    return quotes


async def explain_change(place: dict, place_names: list[str], change: dict,
                         results: list[dict], max_batches: int, emit: Emit, checks: dict,
                         reserve: int, *, trace: Trace, budget: Budget) -> list[dict]:
    """Extract, verify, compose, code-check, checker, and one rewrite round for one change.
    Returns the claims that passed everything. `reserve` is the calls later changes need."""
    quotes = await change_quotes(place, place_names, change, results, max_batches, emit,
                                 trace=trace, budget=budget)
    if not quotes:
        return []  # TR-8: nothing verified, so the chapter stays cause unknown
    raw_claims = await compose_claims(place, change, quotes, trace=trace, budget=budget)
    failed: list[dict] = []
    claims = checked_claims(raw_claims, quotes, place, checks["code_dropped"], failed)
    if claims:
        # A failed checker call raises here, and research_change leaves the change unknown:
        # unchecked claims are never kept (TR-5).
        claims = await check_claims(place, change, claims, checks, trace=trace, budget=budget)
    problems = failed + checker_problems(checks)
    if problems and len(claims) < MAX_CLAIMS and can_rewrite(budget, reserve):
        claims += await rewrite_round(place, change, quotes, claims, problems, checks,
                                      trace=trace, budget=budget)
    trace.log("check", f"check claims {change['id']}", {"raw_claims": len(raw_claims)},
              {"kept": len(claims), "code_dropped": checks["code_dropped"],
               "checker_dropped": len(checks["checker_dropped"]),
               "rescued": len(checks["rescued"])})
    return claims


def years_label(change: dict) -> str:
    """'Change 1999–2010', for event messages."""
    return f"Change {change['start_year']}–{change['end_year']}"


def failure_words(error: Exception) -> str:
    """A plain, key-free reason for a warning. Details stay in the trace."""
    if isinstance(error, BudgetExceeded):
        return f"the story's call budget was used up (HL-10): {error}"
    if isinstance(error, exa_search.ExaSpendCapReached):
        return "the project's web search spending cap was reached"
    if isinstance(error, exa_search.ExaNotCached):
        return "web search is in cached-only mode and this search was not cached"
    if isinstance(error, exa_search.ExaSearchError):
        return "the web search failed"
    if isinstance(error, llm.LLMError):
        return "the text model call failed"
    return "of an unexpected error"


async def research_change(place: dict, place_names: list[str], change: dict,
                          max_batches: int, emit: Emit, warnings: list[str], checks: dict,
                          reserve: int, *, trace: Trace, budget: Budget) -> list[dict]:
    """Search, then explain one change. Any failure, expected or not (a bad model reply, a
    bug), leaves only THIS change cause_unknown (TR-8) with a warning, never the others."""
    try:
        await emit("progress", f"{years_label(change)}: searching for documented causes",
                   stage="research", status="running")
        results = await search_change(place, change, trace=trace, budget=budget)
        return await explain_change(place, place_names, change, results, max_batches, emit,
                                    checks, reserve, trace=trace, budget=budget)
    except Exception as error:  # CancelledError is not an Exception, so timeouts still work
        reason = failure_words(error)
        warnings.append(f"{change['id']}: cause left unknown because {reason}")
        trace.log("check", f"change {change['id']} left cause_unknown",
                  error=llm.describe_error(error))
        return []


# --- Step 9: ids, sources and confidence ---


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


def stronger(level: str, other: str) -> str:
    """The stronger of two TR-7 levels (confirmed > likely > cause_unknown)."""
    if CONFIDENCE_ORDER.index(other) > CONFIDENCE_ORDER.index(level):
        return other
    return level


def chapter_answer(claims: list[dict], book: SourceBook, next_claim: int,
                   claim_confidence: dict[str, str]) -> tuple[dict, int]:
    """The chapter's claims with story ids, and the rule-based confidence (TR-7). Each claim's
    level comes from its OWN sources and goes into `claim_confidence`; the chapter's level is
    its best claim's. Pooling sources across claims would let two weak claims confirm."""
    story_claims = []
    level = "cause_unknown"
    for claim in claims:
        excerpt_ids = []
        claim_sources = {}
        for quote in claim["quotes"]:
            excerpt_id, source = book.excerpt_id(quote)
            excerpt_ids.append(excerpt_id)
            claim_sources[source["id"]] = source
        claim_level = verify.confidence(list(claim_sources.values()))
        if claim_level == "cause_unknown":
            # No source we can rate (e.g. no domain): the rule gives no confidence, so the
            # claim must not borrow "Records show:" from a stronger sibling (QA).
            continue
        claim_id = f"cl-{next_claim:02d}"
        claim_confidence[claim_id] = claim_level
        level = stronger(level, claim_level)
        story_claims.append({"id": claim_id, "text": claim["text"], "excerpt_ids": excerpt_ids})
        next_claim += 1
    return {"confidence": level, "claims": story_claims}, next_claim


# --- Public ---


async def _no_events(*_args: Any, **_kwargs: Any) -> None:
    """Used when the caller passes no emit."""
    return None


async def find_causes(story_dir: Path, place: dict, changes: list[dict], *,
                      trace: Trace, budget: Budget, emit: Emit | None = None) -> dict:
    """Finds documented, code-verified causes for each change of one place (ADR-004).
    `emit` is called like run.py's EventLog: emit("progress", message, stage=..., status=...).
    Returns {"sources", "chapters", "claim_confidence", "checks", "usage", "warnings"}.
    claim_confidence ({claim_id: level}) and checks ({change_id: what was dropped or rescued})
    are private to the backend and the scorer: they never go into story.json."""
    emit = emit or _no_events
    place_names = verify.place_names_for(place)
    warnings: list[str] = []
    max_batches = batches_per_change(budget, len(changes))

    # Largest change first (changes[] is largest-first), so if a budget runs out it is the
    # smaller changes that stay unknown. One change at a time: parallel bursts hit the
    # provider's rate limit.
    claims_by_change = {}
    checks = {}
    for position, change in enumerate(changes):
        later_changes = len(changes) - position - 1
        reserve = later_changes * (max_batches + CALLS_PER_CHANGE)
        checks[change["id"]] = new_checks()
        claims_by_change[change["id"]] = await research_change(
            place, place_names, change, max_batches, emit, warnings, checks[change["id"]],
            reserve, trace=trace, budget=budget,
        )

    # Ids are numbered in story order (by start year), so cl-01 is the first one told.
    in_story_order = sorted(changes, key=lambda change: (change["start_year"], change["id"]))
    book = SourceBook()
    chapters = {}
    claim_confidence: dict[str, str] = {}
    next_claim = 1
    for change in in_story_order:
        claims = claims_by_change[change["id"]]
        chapters[change["id"]], next_claim = chapter_answer(claims, book, next_claim,
                                                            claim_confidence)

    usage = {"exa_calls": budget.used.get("exa", 0), "llm_calls": budget.used.get("llm", 0)}
    return {"sources": book.sources, "chapters": chapters,
            "claim_confidence": claim_confidence, "checks": checks, "usage": usage,
            "warnings": warnings}
