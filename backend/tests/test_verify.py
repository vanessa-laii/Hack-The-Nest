# Tests for app/agent/verify.py: the deterministic truth checks (TR-2, TR-3, TR-4, TR-7).
# Inputs: hand-written excerpts and page texts, including tricky typography.
# Outputs: pytest pass/fail.
from app.agent.verify import (
    authority,
    confidence,
    excerpt_in_page,
    in_years,
    lower_one_step,
    names_place,
    normalise,
    place_names_for,
)

LAKE_MEAD_PLACE = {
    "query": "Lake Mead, Nevada",
    "display_name": "Lake Mead, Nevada",
    "admin": {
        "city": None,
        "county": "Clark County",
        "state": "Nevada",
        "country": "United States",
    },
}

PAGE = (
    "By 2010 the water level at Lake Mead had dropped\nmore than 100 feet, "
    "the Bureau of Reclamation said. “It’s the lowest since 1956,” "
    "an official noted — a record low for the 1999–2010 drought.\n"
    "The Camp Fire de-\nstroyed most of Paradise in Novem­ber 2018."
)


# --- TR-2: excerpt_in_page ---


def test_exact_excerpt_found():
    assert excerpt_in_page("the water level at Lake Mead had dropped", PAGE)


def test_line_break_inside_excerpt_is_whitespace():
    assert excerpt_in_page("Lake Mead had dropped more than 100 feet", PAGE)


def test_straight_quotes_match_curly_quotes():
    assert excerpt_in_page("\"It's the lowest since 1956,\" an official noted", PAGE)


def test_hyphen_matches_dash_types():
    assert excerpt_in_page("an official noted - a record low for the 1999-2010 drought", PAGE)


def test_soft_hyphen_is_ignored():
    assert excerpt_in_page("most of Paradise in November 2018", PAGE)


def test_hard_hyphen_at_line_break_is_kept():
    # The page breaks "destroyed" with a real hyphen; we can't tell it from "well-known".
    assert excerpt_in_page("The Camp Fire de- stroyed most of Paradise", PAGE)
    assert not excerpt_in_page("The Camp Fire destroyed most of Paradise", PAGE)


def test_case_matters():
    assert not excerpt_in_page("the water level at lake mead had dropped", PAGE)


def test_changed_word_fails():
    assert not excerpt_in_page("the water level at Lake Mead had risen", PAGE)


def test_short_or_empty_excerpt_fails():
    assert not excerpt_in_page("Lake Mead", PAGE)
    assert not excerpt_in_page("", PAGE)
    assert not excerpt_in_page("   ", PAGE)


def test_normalise_keeps_letters_and_case():
    assert normalise("  A “B”\t– c ") == 'A "B" - c'


# --- TR-3: names_place ---


def test_place_names_leave_out_state_and_country():
    names = place_names_for(LAKE_MEAD_PLACE)
    assert "Lake Mead" in names
    assert "Clark County" in names
    assert "Nevada" not in names
    assert "United States" not in names


def test_state_alone_fails():
    names = place_names_for(LAKE_MEAD_PLACE)
    assert not names_place("Nevada faces its worst drought in a century.", names)


def test_county_passes_for_lake_mead():
    names = place_names_for(LAKE_MEAD_PLACE)
    assert names_place("Water restrictions began across Clark County in 2003.", names)


def test_place_name_passes():
    names = place_names_for(LAKE_MEAD_PLACE)
    assert names_place("Lake Mead fell to a record low.", names)


def test_named_feature_passes():
    names = place_names_for(LAKE_MEAD_PLACE, features=["Hoover Dam"])
    assert names_place("Hoover Dam's turbines lost capacity.", names)


def test_name_must_be_whole_words():
    assert not names_place("Meadows near Lake Meadville flooded.", ["Lake Mead"])


def test_common_word_is_not_the_town():
    assert not names_place("It was a paradise for birds.", ["Paradise"])
    assert names_place("Paradise lost 18,000 buildings.", ["Paradise"])


# --- TR-4: in_years ---


def test_stated_year_inside_range():
    assert in_years("In 2005 the lake fell.", None, 2000, 2010)


def test_stated_year_one_off_passes():
    assert in_years("In 2011 the lake fell.", None, 2000, 2010)
    assert in_years("In 1999 the lake fell.", None, 2000, 2010)


def test_stated_year_two_off_fails():
    assert not in_years("In 2012 the lake fell.", None, 2000, 2010)
    # A stated year decides, even when the publication date would fit.
    assert not in_years("In 2012 the lake fell.", "2005-06-01", 2000, 2010)


def test_publication_date_used_without_stated_year():
    assert in_years("The lake fell sharply.", "2011-03-04", 2000, 2010)
    assert not in_years("The lake fell sharply.", "2013-03-04", 2000, 2010)
    assert not in_years("The lake fell sharply.", None, 2000, 2010)


def test_decade_counts():
    assert in_years("Building boomed in the 1990s.", None, 2000, 2004)
    assert not in_years("Building boomed in the 1970s.", None, 2000, 2004)


def test_numbers_that_are_not_years():
    # "18,000" and "100" must not be read as years.
    assert in_years("Paradise lost 18,000 buildings.", "2018-11-20", 2018, 2018)


# --- TR-7: authority and confidence ---


def test_authority_classes():
    assert authority("www.usbr.gov") == "authoritative"
    assert authority("earthobservatory.nasa.gov") == "authoritative"
    assert authority("www.gov.uk") == "authoritative"
    assert authority("dubai.gov.ae") == "authoritative"
    assert authority("nrcan.gc.ca") == "authoritative"
    assert authority("umich.edu") == "authoritative"
    assert authority("ox.ac.uk") == "authoritative"
    assert authority("www.nature.com") == "authoritative"
    assert authority("www.nytimes.com") == "authoritative"
    assert authority("apnews.com") == "authoritative"
    assert authority("someblog.wordpress.com") == "other"
    assert authority("governing.com") == "other"
    assert authority("nytimes.com.evil.example") == "other"
    assert authority("") == "other"


def test_confidence_rules():
    assert confidence([]) == "cause_unknown"
    assert confidence([{"domain": "localnews.com"}]) == "likely"
    assert confidence([{"domain": "usgs.gov"}]) == "confirmed"
    two_sites = [{"domain": "localnews.com"}, {"domain": "otherpaper.com"}]
    assert confidence(two_sites) == "confirmed"


def test_same_site_twice_is_one_source():
    same_site = [{"domain": "localnews.com"}, {"domain": "www.localnews.com"}]
    assert confidence(same_site) == "likely"
    subdomains = [{"domain": "a.blog.co.uk"}, {"domain": "b.blog.co.uk"}]
    assert confidence(subdomains) == "likely"


def test_model_authority_label_is_ignored():
    claimed = [{"domain": "someblog.net", "authority": "authoritative"}]
    assert confidence(claimed) == "likely"


def test_lower_one_step():
    assert lower_one_step("confirmed") == "likely"
    assert lower_one_step("likely") == "cause_unknown"
    assert lower_one_step("cause_unknown") == "cause_unknown"
