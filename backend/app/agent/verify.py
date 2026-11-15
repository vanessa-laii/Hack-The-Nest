# Deterministic truth checks for the cause agent (REQUIREMENTS §4). No model calls here.
# Inputs: an excerpt the model picked, the fetched page text and date, the place's names, a domain.
# Outputs: yes/no checks (TR-2, TR-3, TR-4), a source's authority and a claim's confidence (TR-7).
#
# How it works:
#   1. normalise() evens out only what web text extraction scrambles: whitespace, curly
#      quotes, dash types and soft hyphens. Letters, digits and case are never touched.
#   2. excerpt_in_page(): the normalised excerpt must appear word for word in the normalised
#      page (TR-2). Too-short excerpts are refused, since "the lake" is in every page.
#   3. names_place(): the excerpt must contain one of the place's own names (place, city,
#      county, named feature) as a whole word (TR-3). place_names_for() builds that list and
#      leaves out the state and country, which are never enough on their own.
#   4. in_years(): a year stated in the excerpt must be within the change's years ±1. With no
#      stated year, the page's publication date must be (TR-4).
#   5. authority() sorts a domain into "authoritative" (government, science, major newspaper)
#      or "other"; confidence() applies the TR-7 counting rules.
from __future__ import annotations

import re

MIN_EXCERPT_WORDS = 5  # shorter excerpts match almost any page, so they prove nothing
YEAR_SLACK = 1  # TR-4: ±1 year

# --- Step 1: normalising text ---

# Curly and low quotes become straight ones. Pages and model replies mix the two freely,
# and which glyph was used never changes what the sentence says.
QUOTE_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
}

# Hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar, minus sign all
# become "-". Typesetting picks among them; "1999–2010" and "1999-2010" say the same thing.
DASH_MAP = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
}

# The soft hyphen (U+00AD) is an invisible "may break here" mark inside a word. It is not
# part of the word, so removing it restores the word exactly as written.
SOFT_HYPHEN = "­"

# Any run of whitespace (spaces, tabs, new lines, non-breaking spaces) becomes one space.
# Line breaks and spacing come from page layout and text extraction, not from the author.
WHITESPACE_RUN = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Evens out whitespace, quotes, dashes and soft hyphens; keeps everything else as is.
    A hard hyphen at a line break ("de-\\nstroyed") stays a hyphen: we cannot tell it from a
    real one ("well-\\nknown"), so an excerpt that silently joins it fails, on purpose."""
    text = text.replace(SOFT_HYPHEN, "")
    for fancy, plain in QUOTE_MAP.items():
        text = text.replace(fancy, plain)
    for fancy, plain in DASH_MAP.items():
        text = text.replace(fancy, plain)
    text = WHITESPACE_RUN.sub(" ", text)
    return text.strip()


# --- Step 2: TR-2, the excerpt is really on the page ---


def excerpt_in_page(excerpt: str, page_text: str) -> bool:
    """True if the excerpt appears word for word in the page (TR-2). Case-sensitive."""
    wanted = normalise(excerpt or "")
    if len(wanted.split(" ")) < MIN_EXCERPT_WORDS:
        return False
    page = normalise(page_text or "")
    return wanted in page


# --- Step 3: TR-3, the excerpt names the place ---


def name_pattern(name: str) -> re.Pattern:
    """Matches a name as whole words, so "Mead" doesn't match "Meadows"."""
    words = normalise(name).split(" ")
    escaped = []
    for word in words:
        escaped.append(re.escape(word))
    return re.compile(r"(?<!\w)" + r"\s+".join(escaped) + r"(?!\w)")


def names_place(excerpt: str, place_names: list[str]) -> bool:
    """True if the excerpt contains one of the place's names as whole words (TR-3).
    Case-sensitive: proper names are capitalised, and "paradise" the word is not the town.
    Pass names from place_names_for(), which leaves out the state and country."""
    text = normalise(excerpt or "")
    for name in place_names:
        if len(name.strip()) < 3:
            continue  # two-letter names match too much by accident
        if name_pattern(name).search(text):
            return True
    return False


def add_name(names: list[str], name: str | None, too_broad: set[str]) -> None:
    """Adds a cleaned name unless it is empty, already there, or only a state/country."""
    if not name:
        return
    cleaned = normalise(name)
    if not cleaned or cleaned.lower() in too_broad:
        return
    if cleaned not in names:
        names.append(cleaned)


def place_names_for(place: dict, features: list[str] | None = None) -> list[str]:
    """Names that count for TR-3: the place itself, its city and county, and named features
    inside the area. The state and country are dropped: "Nevada" alone isn't the lake."""
    admin = place.get("admin") or {}
    too_broad = set()
    for broad in (admin.get("state"), admin.get("country")):
        if broad:
            too_broad.add(normalise(broad).lower())

    names: list[str] = []
    for source in (place.get("display_name"), place.get("query")):
        for part in (source or "").split(","):
            add_name(names, part, too_broad)
    add_name(names, admin.get("city"), too_broad)
    add_name(names, admin.get("county"), too_broad)
    for feature in features or []:
        add_name(names, feature, too_broad)
    return names


# --- Step 4: TR-4, the excerpt is about the change's years ---

YEAR_PATTERN = re.compile(r"(?<!\d)(1[89]\d\d|20\d\d)(s?)(?!\d)")


def stated_year_spans(excerpt: str) -> list[tuple[int, int]]:
    """Years written in the excerpt as (first, last): "2002" → (2002, 2002),
    "1990s" → (1990, 1999)."""
    spans = []
    for match in YEAR_PATTERN.finditer(excerpt or ""):
        year = int(match.group(1))
        is_decade = match.group(2) == "s" and year % 10 == 0
        if is_decade:
            spans.append((year, year + 9))
        else:
            spans.append((year, year))
    return spans


def overlaps(first: int, last: int, start_year: int, end_year: int) -> bool:
    """True if [first, last] touches the change's years widened by ±1."""
    return first <= end_year + YEAR_SLACK and last >= start_year - YEAR_SLACK


def in_years(
    excerpt: str, published_date: str | None, start_year: int, end_year: int,
) -> bool:
    """True if the excerpt fits the change's years ±1 (TR-4). A year stated in the excerpt
    decides; only an excerpt with no year falls back to the page's publication date."""
    spans = stated_year_spans(excerpt)
    if spans:
        for first, last in spans:
            if overlaps(first, last, start_year, end_year):
                return True
        return False  # it names other years only, so it is about another time
    if not published_date or len(published_date) < 4 or not published_date[:4].isdigit():
        return False
    published_year = int(published_date[:4])
    return overlaps(published_year, published_year, start_year, end_year)


# --- Step 5: TR-7, authority and confidence ---

# Government: whole domain endings, and labels that mark a government site anywhere
# (gov.uk, nsw.gov.au, dubai.gov.ae, ca.gov).
GOV_SUFFIXES = (".gov", ".mil", ".gc.ca", ".canada.ca", ".europa.eu", ".int")
GOV_LABELS = {"gov", "gouv", "govt", "gob", "mil"}

# Science: universities (.edu, ac.uk, edu.au) plus journals and science bodies.
SCIENCE_LABELS = {"edu", "ac"}
SCIENCE_DOMAINS = (
    "nature.com", "science.org", "sciencedirect.com", "springer.com", "wiley.com",
    "pnas.org", "plos.org", "agu.org", "copernicus.org", "tandfonline.com",
    "ipcc.ch", "unep.org", "esa.int", "wmo.int",
)

# Major newspapers and wire services: a short, explicit list.
NEWSPAPER_DOMAINS = (
    "nytimes.com", "washingtonpost.com", "wsj.com", "latimes.com", "usatoday.com",
    "theguardian.com", "bbc.com", "bbc.co.uk", "reuters.com", "apnews.com", "npr.org",
    "bloomberg.com", "ft.com", "economist.com", "sfchronicle.com", "chicagotribune.com",
    "bostonglobe.com",
)


def clean_domain(domain: str) -> str:
    """Lower case, no port, no leading 'www.'."""
    host = (domain or "").strip().lower().split(":")[0].rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def on_list(host: str, domains: tuple[str, ...]) -> bool:
    """True if host is one of the domains or a subdomain of one."""
    for listed in domains:
        if host == listed or host.endswith("." + listed):
            return True
    return False


def is_government(host: str) -> bool:
    """Government and military sites."""
    if host.endswith(GOV_SUFFIXES):
        return True
    labels = host.split(".")[:-1]  # the last label is the country/TLD itself
    for label in labels:
        if label in GOV_LABELS:
            return True
    return False


def is_science(host: str) -> bool:
    """Universities, journals and science bodies."""
    if host.endswith(".edu"):
        return True
    labels = host.split(".")
    # "ac.uk" / "edu.au": the science label must sit just before a two-letter country code.
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SCIENCE_LABELS:
        return True
    return on_list(host, SCIENCE_DOMAINS)


def authority(domain: str) -> str:
    """'authoritative' for government, science or a major newspaper (TR-7), else 'other'.
    Values match story.schema.json sources[].authority."""
    host = clean_domain(domain)
    if not host:
        return "other"
    if is_government(host) or is_science(host) or on_list(host, NEWSPAPER_DOMAINS):
        return "authoritative"
    return "other"


def site_of(domain: str) -> str:
    """The registrable site, so news.bbc.co.uk and bbc.co.uk count as one source:
    the last two labels, or three for forms like co.uk / gov.au."""
    labels = clean_domain(domain).split(".")
    second_level = {"co", "com", "gov", "ac", "org", "net", "edu"}
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in second_level:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def confidence(sources_for_claim: list[dict]) -> str:
    """TR-7 by rule: 'confirmed' = 2+ independent sites or 1 authoritative source;
    'likely' = 1 other source; no sources = 'cause_unknown'. Authority is recomputed from
    each source's domain, never taken from the model."""
    sites = set()
    for source in sources_for_claim:
        domain = source.get("domain") or ""
        if authority(domain) == "authoritative":
            return "confirmed"
        if domain:
            sites.add(site_of(domain))
    if len(sites) >= 2:
        return "confirmed"
    if len(sites) == 1:
        return "likely"
    return "cause_unknown"


def lower_one_step(level: str) -> str:
    """Drops confidence one step when the image check disagrees (TR-7)."""
    if level == "confirmed":
        return "likely"
    return "cause_unknown"
