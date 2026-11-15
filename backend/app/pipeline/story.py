# Assembles a full story.json from imagery.json, the detected changes and their causes.
# Inputs: a story folder holding imagery.json, changes[] from changes.detect_changes, and
#         optionally find_causes' result (sources, plus claims and confidence per change).
# Outputs: a story dict (chapters, a 150–225 word template script, sources, credits); unsaved.
#
# How it works:
#   1. Read imagery.json: place, coverage, frames and metrics are copied as they are.
#   2. Make the chapters: an intro, one chapter per change (in time order), an outro. A change
#      with verified claims takes them and their confidence; the rest are "cause_unknown".
#   3. Plan the segments. The film plays every kept year once, in order, with no gaps: the
#      words in a segment set how slowly the film moves through its years (CONTRACTS §2).
#      Changes with verified causes ("featured"; all changes when none has one) get their own
#      segment over their own years. The years between them go to the opening, the closing
#      or a short connector, which describe any other change there by what changed only.
#   4. Write candidate sentences from fixed templates. The only numbers, years and names
#      filled in come from imagery.json, the changes or the claims (TR-6). A claim is said in
#      its own words after a lead-in set by its confidence ("Records show: …"). The narration
#      never says that a cause is missing (TR-8): the evidence panel shows that plainly.
#   5. Keep every required sentence, then add claims (first, second, third), then the
#      featured changes' measured details, while the script stays within 225 words. Fillers
#      (start/end states, then how the pictures were made, placed in the main sourced
#      segment) only top it up to 150. Under 150 words at the end is an error (HL-1).
#   6. Add sources, credits (HL-11), generation info and the ids, and return the dict.
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1.3"
MIN_WORDS, MAX_WORDS = 150, 225   # HL-1: a 60–90 s film
WORDS_PER_SECOND = 2.5            # calm narration pace; 150–225 words → 60–90 s

# HL-11, word for word.
CREDITS = [
    "Landsat imagery courtesy of NASA and the U.S. Geological Survey.",
    "Supported by the Michigan Space Grant Consortium, NASA grant 80NSSC20M0124.",
]

SOURCED = ("confirmed", "likely")   # the confidences that come with claims (TR-7)
MAX_CLAIMS_SPOKEN = 3               # per segment: every claim a chapter can have
# How a claim is introduced, by ITS OWN confidence (TR-7): (first, second, third claim).
# The lead-in ends in a colon and the claim follows word for word, capitals included, so a
# name at its start ("Water Street", "Construction Partners Inc.") is never changed.
CLAIM_LEADS = {
    "confirmed": ("Records show:", "Records also show:", "Records also show:"),
    "likely": ("One report says:", "One report also says:", "One report also says:"),
}
SENTENCE_ENDS = ".!?\"')"
# DR-6: a measurement caveat, not a missing cause. Short: five flagged changes must fit.
SENSOR_NOTE = "Part of it may come from switching Landsat satellites."

# Ranks of optional sentences: lower ranks are added first while the script has room.
# The featured changes' details come before the opening/closing padding, so the narration
# (and so the film's time) goes to the changes with verified causes.
RANK_BEFORE_AFTER = 10   # "In 1999, water covered …; by 2010, …"
RANK_HECTARES = 11       # "In all, about 650 hectares changed."
RANK_HELD = 12           # "The change held for six summers in the record."
RANK_COMPARE = 13        # "Compare the 1999 and 2010 frames to see it."
# The rest are fillers: added only while the script is under 150 words, so the film moves
# quickly through the years without a verified cause.
RANK_STATE = 14          # how the place looked at the start and the end
RANK_COUNT = 15          # "Over these years, the pictures show four lasting changes."
RANK_NEUTRAL = 20        # how the pictures were made; 20, 21, … in this order

# Shares close to a familiar fraction are said as that fraction ("about a fifth").
FRACTION_WORDS = {10: "a tenth", 20: "a fifth", 25: "a quarter", 33: "a third", 50: "half",
                  67: "two thirds", 75: "three quarters"}
SMALL_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
                      7: "seven", 8: "eight", 9: "nine", 10: "ten"}
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]
COMPASS_WORDS = {"northeast", "northwest", "southeast", "southwest",
                 "north", "south", "east", "west"}

# Short chapter titles by kind. "urban_growth" is often bare cleared land, so it never
# claims buildings (ADR-003 Limits). None of these names a cause.
HEADLINE_MAX = 60   # schema: chapter headline maxLength

HEADLINES = {
    "water_loss": "Water gives way to land",
    "water_gain": "Land goes under water",
    "vegetation_loss": "Green cover thins",
    "vegetation_gain": "The land turns greener",
    "burn": "Green cover lost suddenly",
    "urban_growth": "Land cleared or built over",
    "other": "The ground changes",
}


@dataclass
class Sentence:
    """One candidate sentence. Required ones always stay; optional ones are added by rank
    (lower first) while the script has room."""
    text: str
    required: bool = False
    rank: int = 0
    short: str | None = None   # a shorter wording of a required sentence, same facts
    claim_id: str | None = None   # set on a sentence that says one of the chapter's claims
    claim_order: int = 0          # 1 = the chapter's first claim, 2 = its second, …
    filler: bool = False          # opening/closing extra: only added while under 150 words


@dataclass
class Segment:
    """One planned script segment: its chapter, the kept years the film plays during it
    (as positions in the list of kept years) and its candidate sentences."""
    chapter_id: str
    first: int   # position of year_from in the kept years
    last: int    # position of year_to
    sentences: list[Sentence] = field(default_factory=list)


# --- Plain-word helpers ---

def word_count(text: str) -> int:
    """Words as contract_check counts them: whitespace-separated pieces."""
    return len(text.split())


def number_words(n: int) -> str:
    """'three' for small counts, digits otherwise. Counts read better as words."""
    return SMALL_NUMBER_WORDS.get(n, str(n))


def share_words(fraction: float) -> str:
    """0.215 → 'about 22 percent', 0.198 → 'about a fifth'. Always 'about': these are
    satellite estimates, not surveys."""
    pct = round(abs(fraction) * 100)
    if pct < 1:
        return "a very small part"
    for target, words in FRACTION_WORDS.items():
        if abs(pct - target) <= 1:
            return f"about {words}"
    return f"about {pct} percent"


def hectare_words(area_ha: float) -> str:
    """6.3 → 'about 6 hectares'; big areas round to tens so the number isn't falsely exact."""
    hectares = round(area_ha)
    if hectares >= 100:
        hectares = int(round(area_ha, -1))
    hectares = max(hectares, 1)
    unit = "hectare" if hectares == 1 else "hectares"
    return f"about {hectares} {unit}"


def compass_part(description: str) -> str:
    """Pulls the compass part ('southeast', 'most', 'centre') out of a region description,
    so the script quotes a direction, not the detector's whole phrase."""
    text = description.lower()
    if "across most" in text or "spread across" in text:
        return "most"
    if "centre" in text or "center" in text:
        return "centre"
    for word in re.findall(r"[a-z]+", text):
        if word in COMPASS_WORDS:
            return word
    return ""


def where_words(change: dict) -> str:
    """', mostly in the east' for a place-wide change, ' in the south' for a patch,
    or '' when it spans the place or the direction is unknown."""
    part = compass_part(change["region"]["description"])
    if part == "most" or not part:
        return ""
    if part == "centre":
        place = "the centre"
    else:
        place = f"the {part}"
    if change["scope"] == "place":
        return f", mostly in {place}"
    return f" in {place}"


def headline(change: dict) -> str:
    """'Land cleared or built over in the south' (≤ 60 chars, schema). Plain, no cause."""
    title = HEADLINES.get(change["kind"], HEADLINES["other"])
    part = compass_part(change["region"]["description"])
    if part == "most":
        return f"{title} across the area"
    if part == "centre":
        return f"{title} near the centre"
    if part:
        return f"{title} in the {part}"
    return title


def metrics_for(imagery: dict, year: int) -> dict | None:
    """The metrics.series row for one year, or None if that year isn't kept."""
    for row in imagery["metrics"]["series"]:
        if row["year"] == year:
            return row
    return None


# --- Change sentences ---

def place_body(change: dict) -> str:
    """What happened across the whole place, read on the whole-area metric (scope 'place')."""
    share = share_words(change["magnitude"])
    rising = change["magnitude"] > 0
    metric = change["metric"]
    if metric == "water_frac":
        if rising:
            return f"{share} of the area turned from land to water"
        return f"{share} of the area turned from water to land"
    if metric == "built_frac":
        if rising:
            return f"{share} of the area was cleared or built over"
        return f"{share} of the area stopped looking like bare or built-up ground"
    # ndvi_mean is not a share of the area, so it is told as a direction only.
    if change["kind"] == "burn":
        return "the land across the area lost its green cover suddenly"
    if rising:
        return "the land across the area turned greener on average"
    return "the land across the area lost green cover on average"


def local_body(change: dict) -> str:
    """What happened in one patch, told in hectares, never as a share of the place (ADR-003)."""
    size = hectare_words(change["area_ha"])
    rising = change["magnitude"] > 0
    kind = change["kind"]
    if kind == "water_loss":
        return f"{size} that were water became dry land"
    if kind == "water_gain":
        return f"{size} of land went under water"
    if kind == "burn":
        return f"{size} lost their green cover suddenly"
    if kind == "vegetation_loss":
        return f"{size} lost much of their green cover"
    if kind == "vegetation_gain":
        return f"{size} turned greener"
    if kind == "urban_growth" or (change["metric"] == "built_frac" and rising):
        return f"{size} were cleared or built over"
    return f"{size} that looked like bare or built-up ground changed to something else"


def what_changed(change: dict) -> Sentence:
    """'Between 1999 and 2010, about a fifth of the area turned from water to land, mostly
    in the east.' Required; the short wording drops where it happened."""
    start, end = change["start_year"], change["end_year"]
    if change["scope"] == "local":
        body = local_body(change)
    else:
        body = place_body(change)
    full = f"Between {start} and {end}, {body}{where_words(change)}."
    short = f"Between {start} and {end}, {body}."
    return Sentence(full, required=True, short=short)


def claim_sentence(lead: str, text: str) -> str:
    """'Records show: <the claim, verbatim>.' Only a full stop is added, and only when the
    claim doesn't already end like a sentence. The claim's own text is never altered."""
    claim = text.strip()
    if not claim.endswith(tuple(SENTENCE_ENDS)):
        claim += "."
    return f"{lead} {claim}"


def claim_sentences(cause: dict) -> list[Sentence]:
    """'Records show: …' / 'One report says: …' for each claim, in order, each by its own
    confidence. Not required: fitted before any padding (add_claims)."""
    levels = cause.get("claim_confidence") or {}
    sentences = []
    for order, claim in enumerate(cause["claims"][:MAX_CLAIMS_SPOKEN], start=1):
        level = levels.get(claim["id"], cause["confidence"])
        if level not in SOURCED:
            level = cause["confidence"]
        text = claim_sentence(CLAIM_LEADS[level][order - 1], claim["text"])
        sentences.append(Sentence(text, claim_id=claim["id"], claim_order=order))
    return sentences


def before_after_sentence(imagery: dict, change: dict) -> str | None:
    """'In 1999, water covered about 66 percent of the view; by 2010, about 45 percent.'
    Measured on the whole view, so only for place-wide changes (ADR-003), and only when the
    two shares read differently."""
    if change["scope"] != "place":
        return None
    if change["metric"] == "water_frac":
        metric = "water_frac"
    elif change["metric"] == "built_frac" or change["kind"] in ("burn", "urban_growth"):
        metric = "built_frac"
    else:
        return None
    before = metrics_for(imagery, change["before_year"])
    after = metrics_for(imagery, change["after_year"])
    if before is None or after is None:
        return None
    if max(before[metric], after[metric]) < 0.05:
        return None   # too little of it in view to be worth saying
    share_before = share_words(before[metric])
    share_after = share_words(after[metric])
    if share_before == share_after:
        return None
    year_before, year_after = change["before_year"], change["after_year"]
    if metric == "water_frac":
        return (f"In {year_before}, water covered {share_before} of the view; "
                f"by {year_after}, {share_after}.")
    return (f"In {year_before}, {share_before} of the view read as bare or built-up ground; "
            f"by {year_after}, {share_after}.")


def featured_sentences(imagery: dict, change: dict, cause: dict | None) -> list[Sentence]:
    """A featured change's own segment: what changed, its size, the sensor note, all its
    claims, then measured details (before and after, how long it held, which frames)."""
    sentences = [what_changed(change)]
    if change["scope"] == "place":
        size = hectare_words(change["area_ha"])
        sentences.append(Sentence(f"In all, {size} changed.", rank=RANK_HECTARES))
    if change["sensor_switch_flag"]:
        sentences.append(Sentence(SENSOR_NOTE, required=True))
    if cause is not None:
        sentences.extend(claim_sentences(cause))
    before_after = before_after_sentence(imagery, change)
    if before_after:
        sentences.append(Sentence(before_after, rank=RANK_BEFORE_AFTER))
    summers = change["persistence_years"]
    if summers >= 2:
        held = f"The change held for {number_words(summers)} summers in the record."
        sentences.append(Sentence(held, rank=RANK_HELD))
    compare = (f"Compare the {change['before_year']} and {change['after_year']} "
               f"frames to see it.")
    sentences.append(Sentence(compare, rank=RANK_COMPARE))
    return sentences


def mention_sentences(change: dict) -> list[Sentence]:
    """A change in the years between featured ones, told briefly by what changed: never by a
    cause and never by what is missing (TR-8). The sensor note stays: it is a measurement."""
    sentences = [what_changed(change)]
    if change["sensor_switch_flag"]:
        sentences.append(Sentence(SENSOR_NOTE, required=True))
    return sentences


# --- Opening and closing sentences ---

def state_sentence(imagery: dict, year: int, lead: str) -> str | None:
    """'In 1984, water covered about 76 percent of the view.' Only when water matters here."""
    row = metrics_for(imagery, year)
    if row is None or row["water_frac"] < 0.05:
        return None
    return f"{lead} {year}, water covered {share_words(row['water_frac'])} of the view."


def built_sentence(imagery: dict, year: int) -> str | None:
    """'In 2026, about 15 percent of the view read as bare or built-up ground.'
    Bare soil passes the same test, so it is never called "buildings" (ADR-003)."""
    row = metrics_for(imagery, year)
    if row is None or row["built_frac"] < 0.05:
        return None
    share = share_words(row["built_frac"])
    return f"In {year}, {share} of the view read as bare or built-up ground."


def sensors_used(imagery: dict) -> list[str]:
    """Landsat satellite names that contributed to any frame, in number order."""
    names = set()
    for frame in imagery["frames"]:
        names.update(frame["sensors"])
    return sorted(names)


def join_names(names: list[str]) -> str:
    """['A', 'B', 'C'] → 'A, B and C'."""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def opening_sentences(imagery: dict, n_changes: int) -> list[Sentence]:
    """Where we are, what the film is, and how the place looked at the start."""
    name = imagery["place"]["display_name"]
    coverage = imagery["coverage"]
    first, last = coverage["first_year"], coverage["last_year"]
    opening = Sentence(f"This is {name}, seen from space by Landsat satellites, "
                       f"one summer at a time from {first} to {last}.", required=True,
                       short=f"This is {name}, seen by Landsat from {first} to {last}.")
    sentences = [opening]
    start_state = state_sentence(imagery, first, "In")
    if start_state:
        sentences.append(Sentence(start_state, rank=RANK_STATE, filler=True))
    noun = "change" if n_changes == 1 else "changes"
    sentences.append(Sentence(f"Over these years, the pictures show {number_words(n_changes)} "
                              f"lasting {noun}.", rank=RANK_COUNT, filler=True))
    return sentences


def neutral_sentences(imagery: dict) -> list[Sentence]:
    """Facts about how the pictures were made: padding that names no cause and no absence."""
    coverage = imagery["coverage"]
    months = coverage["season_months"]
    aoi_km = f"{imagery['place']['aoi_km']:g}"
    texts = [
        f"This film uses {len(imagery['frames'])} summers of Landsat imagery.",
        f"Each frame shows the same square of ground, {aoi_km} kilometres on a side.",
        f"Each frame combines the clearest days of one summer, between "
        f"{MONTH_NAMES[months[0] - 1]} and {MONTH_NAMES[months[-1] - 1]}.",
        "Clouds and their shadows are removed, and gaps are left empty instead of being "
        "filled from other years.",
        "Every year is shown with the same colour settings, close to what the eye would see.",
        f"The pictures come from {join_names(sensors_used(imagery))}.",
        "Water, green plants and bare ground reflect sunlight differently, which is how "
        "they are told apart.",
        "The changes were measured by comparing the same ground across the years.",
    ]
    n_skipped = len(coverage["years_skipped"])
    if n_skipped:
        noun = "summer was" if n_skipped == 1 else "summers were"
        # Reasons differ (clouds, archive outages), so the script points to them, not one cause.
        texts.append(f"{number_words(n_skipped).capitalize()} {noun} left out, "
                     f"with the reasons listed in the data notes.")
    texts.append("Every frame is a real satellite picture; nothing is drawn in.")
    built_now = built_sentence(imagery, coverage["last_year"])
    if built_now:
        texts.append(built_now)
    sentences = []
    for i, text in enumerate(texts):
        sentences.append(Sentence(text, rank=RANK_NEUTRAL + i, filler=True))
    return sentences


def closing_sentences(imagery: dict) -> list[Sentence]:
    """How the place looks now, then the closing line. Padding is added by the caller."""
    coverage = imagery["coverage"]
    sentences = []
    end_state = state_sentence(imagery, coverage["last_year"], "By")
    if end_state:
        sentences.append(Sentence(end_state, rank=RANK_STATE, filler=True))
    return sentences


def closing_line(imagery: dict) -> Sentence:
    """'That is how Lake Mead, Nevada has changed since 1984.' Always the last sentence."""
    name = imagery["place"]["display_name"]
    first = imagery["coverage"]["first_year"]
    return Sentence(f"That is how {name} has changed since {first}.", required=True,
                    short=f"That is how this place has changed since {first}.")


# --- Planning the segments over the years ---

def kept_years(imagery: dict) -> list[int]:
    """The years the film plays, in order: one per frame."""
    return [frame["year"] for frame in imagery["frames"]]


def position_from(years: list[int], year: int) -> int:
    """Position of the first kept year at or after `year` (the last one if none is)."""
    for i, kept in enumerate(years):
        if kept >= year:
            return i
    return len(years) - 1


def position_to(years: list[int], year: int) -> int:
    """Position of the last kept year at or before `year` (the first one if none is)."""
    found = 0
    for i, kept in enumerate(years):
        if kept <= year:
            found = i
    return found


def featured_spans(featured: list[dict], years: list[int]) -> list[list[int]]:
    """[first, last] positions in the kept years for each featured change, in time order,
    so that no two overlap and the first and last kept years stay free for the opening and
    closing. Where two changes overlap, the later one starts on its own first year."""
    spans = []
    for change in featured:
        first = max(position_from(years, change["start_year"]), 1)
        if spans:
            first = max(first, spans[-1][0] + 1)
            spans[-1][1] = min(spans[-1][1], first - 1)
        last = max(position_to(years, change["end_year"]), first)
        spans.append([first, last])
    # Walk back from the end so every span keeps at least one year and the closing keeps
    # the last one. Callers pass at most len(years) - 2 changes, so this always fits.
    limit = len(years) - 2
    for span in reversed(spans):
        span[1] = min(span[1], limit)
        span[0] = min(span[0], span[1])
        limit = span[0] - 1
    return spans


def gap_mentions(others: list[dict], years: list[int], first: int, last: int) -> list[dict]:
    """The non-featured changes that start inside the kept years first..last (positions)."""
    found = []
    for change in others:
        if first <= position_from(years, change["start_year"]) <= last:
            found.append(change)
    return found


def padding_host(segments: list[Segment], sourced_chapters: set[str]) -> Segment:
    """Where the neutral padding goes: the opening. "How the pictures were made" belongs
    before the story starts, not inside a sourced change, where it would interrupt the
    verified causes the owner wants the narration to dwell on."""
    return segments[0]


def add_padding(segment: Segment, padding: list[Sentence]) -> None:
    """Puts the neutral padding before the segment's closing details ("The change held …",
    "Compare the … frames …"), so a sourced segment still ends on its own change."""
    position = len(segment.sentences)
    for i, sentence in enumerate(segment.sentences):
        if sentence.rank in (RANK_HELD, RANK_COMPARE) and not sentence.required:
            position = i
            break
    segment.sentences[position:position] = padding


def plan_segments(imagery: dict, ordered: list[dict], chapters: list[dict],
                  sourced: dict) -> list[Segment]:
    """Splits the kept years into contiguous segments, with no gaps or overlaps: the opening,
    one per featured change, short connectors where another change sits between two
    featured ones, and the closing. Featured = the changes with verified causes, or every
    change when none has one."""
    years = kept_years(imagery)
    chapter_by_change = {}
    for chapter in chapters:
        if chapter["change_id"] is not None:
            chapter_by_change[chapter["change_id"]] = chapter["id"]

    featured = [change for change in ordered if change["id"] in sourced]
    if not featured:
        featured = list(ordered)   # nothing sourced: a plain measured film of every change
    featured = featured[:max(len(years) - 2, 0)]
    others = [change for change in ordered if change not in featured]
    spans = featured_spans(featured, years)

    opening_end = spans[0][0] - 1 if spans else max(len(years) - 2, 0)
    opening = Segment(chapters[0]["id"], 0, opening_end,
                      opening_sentences(imagery, len(ordered)))
    for change in gap_mentions(others, years, 0, opening_end):
        opening.sentences.extend(mention_sentences(change))
    segments = [opening]

    for i, change in enumerate(featured):
        first, last = spans[i]
        cause = sourced.get(change["id"])
        segments.append(Segment(chapter_by_change[change["id"]], first, last,
                                featured_sentences(imagery, change, cause)))
        if i + 1 == len(featured):
            break
        gap_first, gap_last = last + 1, spans[i + 1][0] - 1
        if gap_first > gap_last:
            continue
        between = gap_mentions(others, years, gap_first, gap_last)
        if not between:
            segments[-1].last = gap_last   # quiet years: this change's segment plays them
            continue
        connector = Segment(chapter_by_change[between[0]["id"]], gap_first, gap_last)
        for other in between:
            connector.sentences.extend(mention_sentences(other))
        segments.append(connector)

    closing_first = segments[-1].last + 1
    closing = Segment(chapters[-1]["id"], closing_first, len(years) - 1)
    for change in gap_mentions(others, years, closing_first, len(years) - 1):
        closing.sentences.extend(mention_sentences(change))
    closing.sentences.extend(closing_sentences(imagery))
    segments.append(closing)

    sourced_chapters = set()
    for change in featured:
        if change["id"] in sourced:
            sourced_chapters.add(chapter_by_change[change["id"]])
    add_padding(padding_host(segments, sourced_chapters), neutral_sentences(imagery))
    closing.sentences.append(closing_line(imagery))
    return segments


# --- Fitting the script to 150–225 words ---

def required_words(groups: list[list[Sentence]]) -> int:
    """Words in the required sentences alone."""
    total = 0
    for group in groups:
        for sentence in group:
            if sentence.required:
                total += word_count(sentence.text)
    return total


def can_shorten(sentence: Sentence) -> bool:
    """A required sentence that still has a shorter wording to swap to."""
    return sentence.required and bool(sentence.short) and sentence.text != sentence.short


def shortening_room(groups: list[list[Sentence]]) -> int:
    """Words that swapping every remaining required sentence to its short wording saves."""
    room = 0
    for group in groups:
        for sentence in group:
            if can_shorten(sentence):
                room += word_count(sentence.text) - word_count(sentence.short)
    return room


def shorten_until(groups: list[list[Sentence]], limit: int) -> None:
    """Swaps required sentences to their short wording, in script order, until the required
    text is at most `limit` words or nothing is left to shorten."""
    total = required_words(groups)
    for group in groups:
        for sentence in group:
            if total <= limit:
                return
            if can_shorten(sentence):
                total -= word_count(sentence.text) - word_count(sentence.short)
                sentence.text = sentence.short


def shorten_required(groups: list[list[Sentence]]) -> None:
    """Makes the required text alone fit in 225 words. The sensor note has no short form
    and is never dropped. Raises ValueError if even the short forms are too long (HL-1)."""
    shorten_until(groups, MAX_WORDS)
    total = required_words(groups)
    if total > MAX_WORDS:
        raise ValueError(f"required script text is {total} words; max is {MAX_WORDS} (HL-1)")


def make_room(groups: list[list[Sentence]], needed: int) -> int:
    """Shortens required sentences to free `needed` words, only if that is enough.
    Returns the words freed (0 when it wouldn't be enough, so nothing is shortened)."""
    if shortening_room(groups) < needed:
        return 0
    before = required_words(groups)
    shorten_until(groups, before - needed)
    return before - required_words(groups)


def claim_said(group: list[Sentence], chosen_ids: set[int], order: int) -> bool:
    """True if this segment's claim number `order` made it into the script."""
    for sentence in group:
        if sentence.claim_order == order and id(sentence) in chosen_ids:
            return True
    return False


def add_claims(groups: list[list[Sentence]], chosen_ids: set[int], total: int,
               order: int) -> int:
    """Adds each segment's claim number `order` while the script stays within 225 words;
    returns the new total. A claim after the first is said only if the one before it was
    (never a lone "also"), and only a first claim may shorten required sentences."""
    for group in groups:
        for sentence in group:
            if sentence.claim_order != order:
                continue
            if order > 1 and not claim_said(group, chosen_ids, order - 1):
                continue
            words = word_count(sentence.text)
            overflow = total + words - MAX_WORDS
            if overflow > 0 and order == 1:
                total -= make_room(groups, overflow)
            if total + words <= MAX_WORDS:
                chosen_ids.add(id(sentence))
                total += words
            # Otherwise even the shortest wordings leave no room. The claim stays in the
            # chapter, shown with its quotes, but isn't spoken: trimming its words would
            # change what the source says (TR-5), and going over 225 words breaks HL-1.
    return total


def choose_sentences(groups: list[list[Sentence]]) -> list[list[Sentence]]:
    """Keeps every required sentence (shortened if needed), then adds claims in order, then
    optional sentences by rank, while the total stays ≤ 225 words. Fillers are added only
    while under 150 words. Sentence order inside each segment is kept."""
    shorten_required(groups)
    chosen_ids = set()
    total = required_words(groups)
    for group in groups:
        for sentence in group:
            if sentence.required:
                chosen_ids.add(id(sentence))

    for order in range(1, MAX_CLAIMS_SPOKEN + 1):
        total = add_claims(groups, chosen_ids, total, order)

    optional = []
    for group in groups:
        for sentence in group:
            if not sentence.required and not sentence.claim_order:
                optional.append(sentence)
    optional.sort(key=lambda s: s.rank)  # stable, so equal ranks keep script order
    for sentence in optional:
        words = word_count(sentence.text)
        if sentence.filler and total >= MIN_WORDS:
            continue   # fillers only reach 150 words, so the film doesn't drag
        if total + words <= MAX_WORDS:
            chosen_ids.add(id(sentence))
            total += words

    if total < MIN_WORDS:
        raise ValueError(f"script is only {total} words; min is {MIN_WORDS} (HL-1)")
    kept = []
    for group in groups:
        kept.append([s for s in group if id(s) in chosen_ids])
    return kept


# --- Chapters ---

def blank_vision_check() -> dict:
    """The image check (S4) hasn't run yet."""
    return {"verdict": "not_run", "note": None}


def intro_headline(name: str, year: int) -> str:
    """'Lake Mead, Nevada in 1984', always ending in the year and ≤ 60 chars (schema).
    A long name loses its region part first ('Lake Mead'), then words from the end."""
    suffix = f" in {year}"
    room = HEADLINE_MAX - len(suffix)
    if len(name) > room:
        name = name.split(",")[0].strip()
    if len(name) > room:
        name = name[:room + 1].rsplit(" ", 1)[0]   # cut at a word boundary
        name = name[:room].rstrip(" ,")              # one very long word: hard cut
    return name + suffix


def clamp_year(year: int, coverage: dict) -> int:
    """Keeps a chapter year inside the film's coverage."""
    return min(max(year, coverage["first_year"]), coverage["last_year"])


def sourced_causes(causes: dict | None) -> dict:
    """change_id → {"confidence", "claims", "claim_confidence"} for the changes with verified
    claims only. Anything else is cause_unknown, so 'cause_unknown ⇔ no claims' holds (TR-8).
    claim_confidence ({claim_id: level}) only picks lead-ins; it never goes into story.json."""
    sourced = {}
    if not causes:
        return sourced
    levels = causes.get("claim_confidence") or {}
    for change_id, cause in causes.get("chapters", {}).items():
        claims = cause.get("claims") or []
        if claims and cause.get("confidence") in SOURCED:
            claim_levels = {}
            for claim in claims:
                if claim["id"] in levels:
                    claim_levels[claim["id"]] = levels[claim["id"]]
            sourced[change_id] = {"confidence": cause["confidence"], "claims": claims,
                                  "claim_confidence": claim_levels}
    return sourced


def make_chapters(imagery: dict, ordered: list[dict], sourced: dict) -> list[dict]:
    """Intro, one chapter per change, outro. A change chapter takes its claims and
    confidence, or is cause_unknown, which the timeline and evidence panel show (TR-8).
    The intro and outro years are set later, to match their segments."""
    coverage = imagery["coverage"]
    chapters = [{
        "id": "ch-01", "kind": "intro", "change_id": None,
        "start_year": coverage["first_year"], "end_year": coverage["first_year"],
        "headline": intro_headline(imagery["place"]["display_name"], coverage["first_year"]),
        "claims": [], "confidence": None, "vision_check": blank_vision_check(),
    }]
    for change in ordered:
        start = clamp_year(change["start_year"], coverage)
        end = clamp_year(change["end_year"], coverage)
        cause = sourced.get(change["id"])
        claims = []
        confidence = "cause_unknown"
        if cause is not None:
            claims = [dict(claim) for claim in cause["claims"]]
            confidence = cause["confidence"]
        chapters.append({
            "id": f"ch-{len(chapters) + 1:02d}", "kind": "change", "change_id": change["id"],
            "start_year": start, "end_year": max(start, end),
            "headline": headline(change),   # neutral: a cause never goes in a headline
            "claims": claims, "confidence": confidence, "vision_check": blank_vision_check(),
        })
    chapters.append({
        "id": f"ch-{len(chapters) + 1:02d}", "kind": "outro", "change_id": None,
        "start_year": coverage["last_year"], "end_year": coverage["last_year"],
        "headline": "Where it stands today",
        "claims": [], "confidence": None, "vision_check": blank_vision_check(),
    })
    return chapters


def make_script(imagery: dict, ordered: list[dict], chapters: list[dict],
                sourced: dict) -> dict:
    """Plans the segments over the kept years, fits them to 150–225 words, and writes them
    out with an exact word count. Each segment lists exactly the claims it says. The intro
    and outro chapters take the years of the opening and closing segments."""
    years = kept_years(imagery)
    plan = plan_segments(imagery, ordered, chapters, sourced)
    chosen = choose_sentences([segment.sentences for segment in plan])

    segments = []
    total = 0
    for planned, sentences in zip(plan, chosen):
        text = " ".join(sentence.text for sentence in sentences)
        total += word_count(text)
        claim_ids = []
        for sentence in sentences:
            if sentence.claim_id is not None:
                claim_ids.append(sentence.claim_id)
        segments.append({
            "id": f"seg-{len(segments) + 1:02d}",
            "chapter_id": planned.chapter_id,
            "text": text,
            "year_from": years[planned.first],
            "year_to": years[planned.last],
            "claim_ids": claim_ids,
        })
    chapters[0]["end_year"] = segments[0]["year_to"]
    chapters[-1]["start_year"] = segments[-1]["year_from"]
    return {
        "word_count": total,
        "est_duration_s": round(total / WORDS_PER_SECOND, 1),
        "segments": segments,
    }


# --- Public ---

def default_generation() -> dict:
    """Generation info when the caller doesn't pass any: no paid API calls were made here."""
    return {"duration_s": 0.0, "usage": {"exa_calls": 0, "llm_calls": 0, "vision_calls": 0},
            "warnings": []}


def assemble_story(story_dir: Path, changes: list[dict], *, causes: dict | None = None,
                   status: str = "complete", generation: dict | None = None) -> dict:
    """Builds the full story.json dict from imagery.json plus 1–5 changes and, optionally,
    find_causes' result. The caller validates and saves it (store.save_story)."""
    if not 1 <= len(changes) <= 5:
        raise ValueError(f"{len(changes)} changes; a story needs 1-5 (HL-2)")
    imagery = json.loads((story_dir / "imagery.json").read_text(encoding="utf-8"))

    # Chapters play in time order; changes[] keeps the detector's largest-first order.
    ordered = sorted(changes, key=lambda change: (change["start_year"], change["id"]))
    sourced = sourced_causes(causes)
    chapters = make_chapters(imagery, ordered, sourced)
    script = make_script(imagery, ordered, chapters, sourced)
    sources = []
    if causes:
        sources = list(causes.get("sources", []))

    return {
        "schema_version": SCHEMA_VERSION,
        "id": story_dir.name,
        "is_sample": False,
        "status": status,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "place": imagery["place"],
        "coverage": imagery["coverage"],
        "frames": imagery["frames"],
        "metrics": imagery["metrics"],
        "changes": changes,
        "sources": sources,
        "chapters": chapters,
        "script": script,
        "validation": None,
        "credits": list(CREDITS),
        "generation": generation or default_generation(),
    }
