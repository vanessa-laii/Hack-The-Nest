# Assembles a full story.json from the imagery half (imagery.json) and the detected changes.
# Inputs: a story folder holding imagery.json, the changes[] list from changes.detect_changes.
# Outputs: a story dict (chapters, a 150–225 word template script, credits); not yet saved.
#
# How it works:
#   1. Read imagery.json: place, coverage, frames and metrics are copied as they are.
#   2. Make the chapters: an intro, one chapter per change (in time order), an outro.
#   3. Write candidate sentences for each chapter from fixed templates. The only numbers,
#      years and names filled in come from imagery.json or the changes (TR-6).
#   4. Every change chapter says plainly that its cause hasn't been found (TR-8).
#   5. Keep every required sentence, then add optional ones in rank order while the script
#      stays within 225 words. If the required ones alone are too long (five changes, a long
#      name), some swap to a shorter wording; no truth sentence is ever dropped. Under 150
#      words at the end is an error (HL-1).
#   6. Add credits (HL-11), generation info and the ids, and return the dict.
#
# Until the cause agent (S4) exists, every change is "cause_unknown" with no claims or sources.
from __future__ import annotations

import json
import re
from dataclasses import dataclass
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

UNKNOWN_CAUSE = "We haven't found what caused this change."   # TR-8, said plainly
# DR-6. Short on purpose: five flagged changes must still fit in 225 words.
SENSOR_NOTE = "Part of it may come from switching Landsat satellites."

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


def change_sentences(change: dict, rank_base: int) -> list[Sentence]:
    """Candidate sentences for one change chapter. The years, size and cause-unknown line
    are required; the rest is added if the script has room."""
    start, end = change["start_year"], change["end_year"]
    if change["scope"] == "local":
        body = local_body(change)
    else:
        body = place_body(change)
    full = f"Between {start} and {end}, {body}{where_words(change)}."
    short = f"Between {start} and {end}, {body}."   # without where it happened
    sentences = [Sentence(full, required=True, short=short)]
    if change["scope"] == "place" and change["metric"] == "ndvi_mean":
        size = hectare_words(change["area_ha"])
        sentences.append(Sentence(f"In all, {size} changed.", rank=rank_base))
    if change["sensor_switch_flag"]:
        sentences.append(Sentence(SENSOR_NOTE, required=True))
    compare = (f"Compare the {change['before_year']} and {change['after_year']} "
               f"frames to see it.")
    sentences.append(Sentence(compare, rank=rank_base + 20))
    summers = change["persistence_years"]
    if summers >= 2:
        held = f"The change held for {number_words(summers)} summers in the record."
        sentences.append(Sentence(held, rank=rank_base + 10))
    sentences.append(Sentence(UNKNOWN_CAUSE, required=True))
    return sentences


# --- Intro and outro sentences ---

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


def intro_sentences(imagery: dict, n_changes: int) -> list[Sentence]:
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
        sentences.append(Sentence(start_state, rank=1))
    aoi_km = f"{imagery['place']['aoi_km']:g}"
    sentences.append(Sentence(f"Each frame shows the same square of ground, {aoi_km} "
                              f"kilometres on a side.", rank=30))
    noun = "change" if n_changes == 1 else "changes"
    sentences.append(Sentence(f"Over these years, the pictures show {number_words(n_changes)} "
                              f"lasting {noun}.", rank=2))
    return sentences


def outro_sentences(imagery: dict) -> list[Sentence]:
    """How the place looks now, then neutral facts about the data used as padding."""
    coverage = imagery["coverage"]
    last = coverage["last_year"]
    sentences = []
    end_state = state_sentence(imagery, last, "By")
    if end_state:
        sentences.append(Sentence(end_state, rank=3))

    n_summers = len(imagery["frames"])
    uses = f"This film uses {n_summers} summers of Landsat imagery."
    sentences.append(Sentence(uses, rank=20))
    months = coverage["season_months"]
    sentences.append(Sentence(
        f"Each frame combines the clearest days of one summer, between "
        f"{MONTH_NAMES[months[0] - 1]} and {MONTH_NAMES[months[-1] - 1]}.", rank=21))
    sentences.append(Sentence("Clouds and their shadows are removed, and gaps are left empty "
                              "instead of being filled from other years.", rank=22))
    sentences.append(Sentence("Every year is shown with the same colour settings, "
                              "close to what the eye would see.", rank=23))
    sentences.append(Sentence(f"The pictures come from {join_names(sensors_used(imagery))}.",
                              rank=24))
    n_skipped = len(coverage["years_skipped"])
    if n_skipped:
        noun = "summer was" if n_skipped == 1 else "summers were"
        # Reasons differ (clouds, archive outages), so the script points to them, not one cause.
        skipped = (f"{number_words(n_skipped).capitalize()} {noun} left out, "
                   f"with the reasons listed in the data notes.")
        sentences.append(Sentence(skipped, rank=25))
    sentences.append(Sentence("Every frame is a real satellite picture; nothing is drawn in.",
                              rank=26))
    built_now = built_sentence(imagery, last)
    if built_now:
        sentences.append(Sentence(built_now, rank=31))
    sentences.append(Sentence("Where the record is silent about a cause, this film says so "
                              "instead of guessing.", rank=32))
    name = imagery["place"]["display_name"]
    first = coverage["first_year"]
    sentences.append(Sentence(f"That is how {name} has changed since {first}.", required=True,
                              short=f"That is how this place has changed since {first}."))
    return sentences


# --- Fitting the script to 150–225 words ---

def required_words(groups: list[list[Sentence]]) -> int:
    """Words in the required sentences alone."""
    total = 0
    for group in groups:
        for sentence in group:
            if sentence.required:
                total += word_count(sentence.text)
    return total


def shorten_required(groups: list[list[Sentence]]) -> None:
    """Swaps required sentences to their short wording, in script order, until the required
    text fits in 225 words. Truth sentences (cause unknown, sensor note) have no short form
    and are never dropped. Raises ValueError if even the short forms are too long (HL-1)."""
    total = required_words(groups)
    for group in groups:
        for sentence in group:
            if total <= MAX_WORDS:
                return
            if sentence.required and sentence.short:
                total -= word_count(sentence.text) - word_count(sentence.short)
                sentence.text = sentence.short
    if total > MAX_WORDS:
        raise ValueError(f"required script text is {total} words; max is {MAX_WORDS} (HL-1)")


def choose_sentences(groups: list[list[Sentence]]) -> list[list[Sentence]]:
    """Keeps every required sentence (shortened if needed), then adds optional ones by rank
    while the total stays ≤ 225 words. Sentence order inside each chapter is kept."""
    shorten_required(groups)
    chosen_ids = set()
    total = required_words(groups)
    for group in groups:
        for sentence in group:
            if sentence.required:
                chosen_ids.add(id(sentence))

    optional = []
    for group in groups:
        for sentence in group:
            if not sentence.required:
                optional.append(sentence)
    optional.sort(key=lambda s: s.rank)  # stable, so equal ranks keep chapter order
    for sentence in optional:
        words = word_count(sentence.text)
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


def make_chapters(imagery: dict, ordered: list[dict]) -> list[dict]:
    """Intro (first years before the first change), one chapter per change, outro (latest
    years). All without claims: no cause has been checked yet (TR-8)."""
    coverage = imagery["coverage"]
    first_start = min(change["start_year"] for change in ordered)
    last_end = max(change["end_year"] for change in ordered)

    intro_end = clamp_year(first_start - 1, coverage)
    chapters = [{
        "id": "ch-01", "kind": "intro", "change_id": None,
        "start_year": coverage["first_year"], "end_year": intro_end,
        "headline": intro_headline(imagery["place"]["display_name"], coverage["first_year"]),
        "claims": [], "confidence": None, "vision_check": blank_vision_check(),
    }]
    for change in ordered:
        start = clamp_year(change["start_year"], coverage)
        end = clamp_year(change["end_year"], coverage)
        chapters.append({
            "id": f"ch-{len(chapters) + 1:02d}", "kind": "change", "change_id": change["id"],
            "start_year": start, "end_year": max(start, end),
            "headline": headline(change),
            "claims": [], "confidence": "cause_unknown", "vision_check": blank_vision_check(),
        })
    outro_start = clamp_year(last_end, coverage)
    chapters.append({
        "id": f"ch-{len(chapters) + 1:02d}", "kind": "outro", "change_id": None,
        "start_year": outro_start, "end_year": coverage["last_year"],
        "headline": "Where it stands today",
        "claims": [], "confidence": None, "vision_check": blank_vision_check(),
    })
    return chapters


def make_script(imagery: dict, ordered: list[dict], chapters: list[dict]) -> dict:
    """One segment per chapter, fitted to 150–225 words, with an exact word count."""
    groups = [intro_sentences(imagery, len(ordered))]
    for i, change in enumerate(ordered):
        groups.append(change_sentences(change, rank_base=4 + i))
    groups.append(outro_sentences(imagery))
    chosen = choose_sentences(groups)

    segments = []
    total = 0
    for chapter, sentences in zip(chapters, chosen):
        text = " ".join(sentence.text for sentence in sentences)
        total += word_count(text)
        segments.append({
            "id": f"seg-{len(segments) + 1:02d}",
            "chapter_id": chapter["id"],
            "text": text,
            "year_from": chapter["start_year"],
            "year_to": chapter["end_year"],
            "claim_ids": [],
        })
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


def assemble_story(story_dir: Path, changes: list[dict], *, status: str = "complete",
                   generation: dict | None = None) -> dict:
    """Builds the full story.json dict from imagery.json plus 1–5 changes.
    The caller validates and saves it (store.save_story)."""
    if not 1 <= len(changes) <= 5:
        raise ValueError(f"{len(changes)} changes; a story needs 1-5 (HL-2)")
    imagery = json.loads((story_dir / "imagery.json").read_text(encoding="utf-8"))

    # Chapters play in time order; changes[] keeps the detector's largest-first order.
    ordered = sorted(changes, key=lambda change: (change["start_year"], change["id"]))
    chapters = make_chapters(imagery, ordered)
    script = make_script(imagery, ordered, chapters)

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
        "sources": [],
        "chapters": chapters,
        "script": script,
        "validation": None,
        "credits": list(CREDITS),
        "generation": generation or default_generation(),
    }
