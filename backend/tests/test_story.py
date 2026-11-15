# Tests for story.assemble_story: contract-valid output, 150–225 words, no invented numbers,
# sourced causes said in their own words, segments covering every kept year once, and no
# sentence that says a cause is missing (TR-8).
# Inputs: a synthetic imagery.json written to a temp folder, synthetic changes and causes.
# Outputs: pytest results.
import copy
import json
import random
import re

from app.contract_check import story_errors
from app.pipeline.story import (SENSOR_NOTE, assemble_story, claim_sentence, intro_headline,
                                what_changed)

FIRST_YEAR, LAST_YEAR = 1990, 2020


def _imagery(story_id: str, display_name: str = "Testville, Nowhere") -> dict:
    """A small fake imagery.json: one frame and metrics row per summer, 1990–2020."""
    frames = []
    series = []
    for year in range(FIRST_YEAR, LAST_YEAR + 1):
        frames.append({
            "year": year,
            "image_url": f"/files/stories/{story_id}/frames/{year}.jpg",
            "thumb_url": f"/files/stories/{story_id}/thumbs/{year}.jpg",
            "sensors": ["Landsat 5"] if year < 2013 else ["Landsat 8"],
            "scene_ids": [f"SCENE_{year}"],
            "date_from": f"{year}-06-10",
            "date_to": f"{year}-09-01",
            "clear_pct": 97.0,
        })
        water = 0.40 if year < 2005 else 0.18
        series.append({"year": year, "ndvi_mean": 0.31, "water_frac": water,
                       "built_frac": 0.12})
    return {
        "place": {"query": "Testville", "display_name": display_name, "lat": 40.0,
                  "lon": -100.0, "bbox": [-100.04, 39.97, -99.96, 40.03], "aoi_km": 6.0,
                  "admin": {"city": None, "county": None, "state": None,
                            "country": "United States", "country_code": "us"}},
        "coverage": {"first_year": FIRST_YEAR, "last_year": LAST_YEAR,
                     "season_months": [6, 7, 8, 9],
                     "years_skipped": [{"year": 1989, "reason": "only 41% of the area had a "
                                                               "clear view"}]},
        "frames": frames,
        "metrics": {"series": series},
    }


def _change(number: int, kind: str, metric: str, scope: str, start: int, end: int,
            magnitude: float, area_ha: float, where: str, flagged: bool = False) -> dict:
    """One schema-1.3 changes[] item."""
    return {
        "id": f"chg-{number:02d}", "kind": kind, "metric": metric,
        "start_year": start, "end_year": end, "magnitude": magnitude, "scope": scope,
        "area_ha": area_ha, "persistence_years": LAST_YEAR - end + 1,
        "region": {"bbox": [-100.02, 39.98, -99.99, 40.01], "description": where},
        "before_year": start, "after_year": end, "mask_url": None,
        "sensor_switch_flag": flagged,
    }


FIVE_CHANGES = [
    _change(1, "water_loss", "water_frac", "place", 2001, 2008, -0.215, 770.4,
            "in the east part of the area"),
    _change(2, "urban_growth", "built_frac", "local", 2012, 2013, 1.0, 6.3,
            "a 6 ha area in the south part, where land was cleared or built over", True),
    _change(3, "burn", "ndvi_mean", "place", 2015, 2016, -0.198, 1210.0,
            "across most of the area"),
    _change(4, "vegetation_gain", "ndvi_mean", "local", 1995, 1999, 0.168, 9.2,
            "a 9 ha area near the centre"),
    _change(5, "other", "built_frac", "local", 2017, 2018, -1.0, 4.5,
            "a 5 ha area in the northwest part", True),
]


def _write_imagery(tmp_path, story_id: str = "testville-us-6km",
                   display_name: str = "Testville, Nowhere"):
    """Writes the fake imagery.json and returns (story folder, imagery dict)."""
    folder = tmp_path / story_id
    folder.mkdir()
    imagery = _imagery(story_id, display_name)
    (folder / "imagery.json").write_text(json.dumps(imagery), encoding="utf-8")
    return folder, imagery


def _used_claims(story: dict) -> list[dict]:
    """The claims the script actually says: those listed in some segment's claim_ids."""
    spoken = set()
    for segment in story["script"]["segments"]:
        spoken.update(segment["claim_ids"])
    used = []
    for chapter in story["chapters"]:
        for claim in chapter["claims"]:
            if claim["id"] in spoken:
                used.append(claim)
    return used


def _allowed_numbers(imagery: dict, changes: list[dict], story: dict) -> set[str]:
    """Every number the script may say (TR-6), worked out here from the inputs on their own:
    years, rounded hectares and shares, counts of summers, the area size, satellite numbers,
    and any number written in a claim the script uses."""
    allowed = set()
    for claim in _used_claims(story):
        allowed.update(_numbers_in(claim["text"]))
    for frame in imagery["frames"]:
        allowed.add(str(frame["year"]))
        for sensor in frame["sensors"]:
            allowed.add(sensor.split()[-1])
    for row in imagery["metrics"]["series"]:
        allowed.add(str(round(row["water_frac"] * 100)))
        allowed.add(str(round(row["built_frac"] * 100)))
    allowed.add(str(len(imagery["frames"])))
    allowed.add(f"{imagery['place']['aoi_km']:g}")
    for change in changes:
        for key in ("start_year", "end_year", "before_year", "after_year",
                    "persistence_years"):
            allowed.add(str(change[key]))
        allowed.add(str(round(change["area_ha"])))
        allowed.add(str(int(round(change["area_ha"], -1))))
        allowed.add(str(round(abs(change["magnitude"]) * 100)))
    return allowed


def _numbers_in(text: str) -> list[str]:
    """Every run of digits in the script."""
    return re.findall(r"\d+(?:\.\d+)?", text)


def _script_text(story: dict) -> str:
    return " ".join(segment["text"] for segment in story["script"]["segments"])


# Ways of saying a cause is missing. The narration never says any of them (TR-8): the
# timeline and evidence panel show "cause unknown" instead.
ABSENCE_PHRASES = ["haven't found", "have not found", "not found", "unknown", "no record",
                   "no source", "unclear", "is silent", "couldn't find", "could not find",
                   "don't know", "do not know", "isn't known", "not known"]


def _check_no_absence(text: str) -> None:
    """The script never says that a cause is missing."""
    lowered = text.lower()
    for phrase in ABSENCE_PHRASES:
        assert phrase not in lowered, f"absence line in the script: {phrase!r}"


def _check_causes(story: dict) -> None:
    """Each segment says only its own chapter's claims, in order and verbatim; an unsourced
    chapter has no claims; headlines never quote a claim."""
    chapters = {chapter["id"]: chapter for chapter in story["chapters"]}
    for segment in story["script"]["segments"]:
        chapter = chapters[segment["chapter_id"]]
        own_ids = [claim["id"] for claim in chapter["claims"]]
        assert set(segment["claim_ids"]) <= set(own_ids)
        assert len(segment["claim_ids"]) <= 3
        assert segment["claim_ids"] == own_ids[:len(segment["claim_ids"])]   # in order
        texts = {claim["id"]: claim["text"] for claim in chapter["claims"]}
        for claim_id in segment["claim_ids"]:
            assert texts[claim_id] in segment["text"]   # said verbatim, capitals included
    for chapter in story["chapters"]:
        if chapter["confidence"] == "cause_unknown":
            assert chapter["claims"] == []
        for claim in chapter["claims"]:
            assert claim["text"] not in chapter["headline"]


def _check_years(story: dict) -> None:
    """Segments cover every kept year exactly once, in order, from the first to the last:
    each starts on the kept year right after the previous one ends, so the film never skips
    or repeats a year (CONTRACTS §2 playback rule)."""
    years = [frame["year"] for frame in story["frames"]]
    segments = story["script"]["segments"]
    assert segments[0]["year_from"] == years[0] == story["coverage"]["first_year"]
    assert segments[-1]["year_to"] == years[-1] == story["coverage"]["last_year"]
    position = 0
    for segment in segments:
        assert segment["year_from"] == years[position]
        assert segment["year_from"] <= segment["year_to"]
        assert segment["year_to"] in years
        position = years.index(segment["year_to"]) + 1
    assert position == len(years)
    chapters = story["chapters"]
    assert chapters[0]["start_year"] == segments[0]["year_from"]
    assert chapters[0]["end_year"] == segments[0]["year_to"]
    assert chapters[-1]["start_year"] == segments[-1]["year_from"]
    assert chapters[-1]["end_year"] == segments[-1]["year_to"]
    for chapter in chapters:
        assert chapter["start_year"] <= chapter["end_year"]


def _segment_kinds(story: dict) -> tuple[list[int], list[int]]:
    """Word counts of (sourced change segments, connector segments). A connector is a
    segment of an unsourced change chapter; the opening and closing are neither."""
    chapters = {chapter["id"]: chapter for chapter in story["chapters"]}
    sourced, connectors = [], []
    for segment in story["script"]["segments"]:
        chapter = chapters[segment["chapter_id"]]
        words = len(segment["text"].split())
        if chapter["kind"] != "change":
            continue
        if chapter["claims"]:
            sourced.append(words)
        else:
            connectors.append(words)
    return sourced, connectors


def _check_sourced_outweigh_connectors(story: dict) -> None:
    """When a story has sourced changes, each of their segments has more words than any
    connector, so the film moves slowly there and quickly through the rest."""
    sourced, connectors = _segment_kinds(story)
    if sourced and connectors:
        assert min(sourced) > max(connectors), (sourced, connectors)


def _described_flagged(story: dict, changes: list[dict]) -> int:
    """How many sensor-flagged changes the script describes. Each one described carries the
    sensor note (DR-6); a change it doesn't describe needs none."""
    text = _script_text(story)
    count = 0
    for change in changes:
        sentence = what_changed(change)
        described = sentence.text in text or sentence.short in text
        if described and change["sensor_switch_flag"]:
            count += 1
    return count


def _check(story: dict, imagery: dict, changes: list[dict]) -> str:
    """The shared assertions: valid contract, HL-1 length, only input numbers (TR-6)."""
    assert story_errors(story) == []
    text = _script_text(story)
    words = len(text.split())
    assert 150 <= words <= 225
    assert story["script"]["word_count"] == words
    allowed = _allowed_numbers(imagery, changes, story)
    invented = [n for n in _numbers_in(text) if n not in allowed]
    assert invented == [], f"numbers not in the inputs: {invented}"
    _check_causes(story)
    _check_no_absence(text)
    _check_years(story)
    return text


def test_one_place_and_one_local_change(tmp_path):
    """No sourced change: a plain measured film. Both chapters stay cause_unknown in
    story.json, and the narration describes them without any absence line (TR-8)."""
    folder, imagery = _write_imagery(tmp_path)
    changes = FIVE_CHANGES[:2]
    story = assemble_story(folder, changes)
    text = _check(story, imagery, changes)

    assert story["id"] == "testville-us-6km"
    assert story["schema_version"] == "1.3"
    kinds = [chapter["kind"] for chapter in story["chapters"]]
    assert kinds == ["intro", "change", "change", "outro"]
    for chapter in story["chapters"][1:-1]:
        assert chapter["confidence"] == "cause_unknown"
        assert chapter["claims"] == []
    assert "about 6 hectares" in text          # local change told in hectares
    assert "about 22 percent of the area" in text   # place change on the whole-area metric
    assert "building" not in text.lower()      # urban_growth never claims buildings


def test_single_change_is_padded_to_150_words(tmp_path):
    """One short change still makes a 150+ word script, using neutral data sentences."""
    folder, imagery = _write_imagery(tmp_path)
    changes = [FIVE_CHANGES[1]]
    story = assemble_story(folder, changes)
    _check(story, imagery, changes)


def test_five_changes_still_fit_225_words(tmp_path):
    """The longest case (HL-2's maximum, two flagged) stays within HL-1."""
    folder, imagery = _write_imagery(tmp_path)
    story = assemble_story(folder, FIVE_CHANGES)
    _check(story, imagery, FIVE_CHANGES)
    assert len(story["chapters"]) == 7
    years = [chapter["start_year"] for chapter in story["chapters"][1:-1]]
    assert years == sorted(years)   # change chapters play in time order


def test_chapter_years_stay_inside_coverage(tmp_path):
    """A change dated at the very first summer gives an intro of just that summer."""
    folder, imagery = _write_imagery(tmp_path)
    early = _change(1, "water_gain", "water_frac", "place", FIRST_YEAR, 1993, 0.08, 300.0,
                    "in the west part of the area")
    story = assemble_story(folder, [early])
    _check(story, imagery, [early])
    for chapter in story["chapters"]:
        assert FIRST_YEAR <= chapter["start_year"] <= chapter["end_year"] <= LAST_YEAR


# A 99-character place name, about as long as a real geocoder display name gets.
LONG_NAME = ("Lake Havasu City Municipal Airport Industrial Park Northeast, Mohave County, "
             "Arizona, United States")


def _flag_all(changes: list[dict]) -> list[dict]:
    """Copies of the changes, every one flagged as lining up with a satellite switch."""
    flagged = copy.deepcopy(changes)
    for change in flagged:
        change["sensor_switch_flag"] = True
    return flagged


def test_sensor_note_is_short():
    """Five flagged changes must fit in 225 words, so the DR-6 note stays short."""
    assert len(SENSOR_NOTE.split()) <= 9


def test_five_flagged_changes_with_a_long_name_fit(tmp_path):
    """Worst case: five changes, all sensor-flagged, a 99-character name. Still valid, and
    every sensor note is still said for every change."""
    assert len(LONG_NAME) == 99
    folder, imagery = _write_imagery(tmp_path, display_name=LONG_NAME)
    changes = _flag_all(FIVE_CHANGES)
    story = assemble_story(folder, changes)
    text = _check(story, imagery, changes)
    assert text.count(SENSOR_NOTE) == 5
    assert story["chapters"][0]["headline"].endswith(f" in {FIRST_YEAR}")


KINDS = [
    ("water_loss", "water_frac", -1), ("water_gain", "water_frac", 1),
    ("vegetation_loss", "ndvi_mean", -1), ("vegetation_gain", "ndvi_mean", 1),
    ("burn", "ndvi_mean", -1), ("urban_growth", "built_frac", 1), ("other", "built_frac", -1),
]
WHERE = ["in the northeast part of the area", "across most of the area",
         "a 5 ha area near the centre", "somewhere", "a 12 ha area in the southwest part"]


def _random_change(rng: random.Random, number: int) -> dict:
    """One synthetic change with random kind, scope, years, size, flag and persistence."""
    kind, metric, sign = rng.choice(KINDS)
    start = rng.randint(FIRST_YEAR, LAST_YEAR - 1)
    end = rng.randint(start + 1, LAST_YEAR)
    scope = rng.choice(["place", "local"])
    if scope == "local" and metric == "built_frac":
        magnitude = float(sign)
    else:
        magnitude = sign * rng.uniform(0.02, 0.9)
    change = _change(number, kind, metric, scope, start, end, magnitude,
                     rng.uniform(0.5, 3000.0), rng.choice(WHERE), rng.random() < 0.6)
    change["persistence_years"] = rng.randint(1, LAST_YEAR - end + 1)
    return change


def test_random_change_sets_always_make_a_valid_script(tmp_path):
    """Fuzz: 200 seeded random sets of 1-5 changes, random flags and persistence, some with
    the long name. Every one is valid, 150-225 words, with only input numbers (TR-6)."""
    rng = random.Random(2026)
    for i in range(200):
        name = LONG_NAME if i % 3 == 0 else "Testville, Nowhere"
        folder, imagery = _write_imagery(tmp_path, story_id=f"fuzz-{i}-us-6km",
                                         display_name=name)
        changes = []
        for number in range(1, rng.randint(1, 5) + 1):
            changes.append(_random_change(rng, number))
        story = assemble_story(folder, changes)
        text = _check(story, imagery, changes)
        assert text.count(SENSOR_NOTE) == _described_flagged(story, changes)


def test_intro_headline_never_loses_the_year():
    """Long names are shortened, never the year; the result fits the schema's 60 chars."""
    assert intro_headline("Lake Mead, Nevada", 1984) == "Lake Mead, Nevada in 1984"
    for name in (LONG_NAME, "x" * 99, "Llanfairpwllgwyngyll " * 5):
        title = intro_headline(name, 1984)
        assert title.endswith(" in 1984")
        assert len(title) <= 60


def test_too_long_required_text_switches_to_short_wording(tmp_path):
    """A 200-character name (the most POST accepts), five flagged changes and a long first
    claim on each leave no room. Required sentences switch to their short wording (here the
    opening) to make room for a claim; no sensor note is dropped and the script is valid."""
    name = LONG_NAME + ", " + LONG_NAME
    assert len(name) == 200
    folder, imagery = _write_imagery(tmp_path, display_name=name)
    changes = _flag_all(FIVE_CHANGES)
    spec = {}
    for change in changes:
        spec[change["id"]] = ("confirmed", [LONG_CLAIM])
    story = assemble_story(folder, changes, causes=_causes(spec))
    text = _check(story, imagery, changes)
    assert f"seen by Landsat from {FIRST_YEAR} to {LAST_YEAR}." in text   # short opening
    assert text.count(SENSOR_NOTE) == 5


# --- Sourced causes ---

# Real claims from the S4 spike (spikes/s4-causes/extract-verify/review.md).
DROUGHT_CLAIM = ("Since drought took hold on the Colorado and its tributaries in 1999, "
                 "the surface of Lake Mead has plunged almost 130 feet.")
LOW_LEVEL_CLAIM = "In August 2010, Lake Mead reached its lowest level since 1956."
SUMMERLIN_CLAIM = "The first family moved to Summerlin in 1991."
# Close to the schema's 240-character limit for a claim.
LONG_CLAIM = ("Since drought took hold on the Colorado and its tributaries in 1999, the "
              "surface of Lake Mead has plunged almost 130 feet and caused fits for the "
              "National Park Service and its marina operators who must extend roads to "
              "reach the shoreline.")
CLAIM_POOL = [DROUGHT_CLAIM, LOW_LEVEL_CLAIM, SUMMERLIN_CLAIM, LONG_CLAIM,
              "Construction on the first phase of the project began in May 2017.",
              "Palm Jumeirah is an artificial island created through land reclamation.",
              "The city planned to demolish 33 homes in the Delray community."]


def _causes(spec: dict) -> dict:
    """A find_causes-shaped result. spec: change_id → (confidence, [claim texts]).
    Each claim gets its own source with one verified excerpt (the claim text itself)."""
    sources = []
    chapters = {}
    n = 0
    for change_id, (confidence, texts) in spec.items():
        claims = []
        for text in texts:
            n += 1
            excerpt_id = f"ex-{n:02d}-01"
            sources.append({
                "id": f"src-{n:02d}", "url": f"https://example.org/story-{n}",
                "title": f"Report {n}", "publisher": None, "domain": "example.org",
                "published_date": "2010-10-19", "authority": "authoritative",
                "retrieved_at": "2026-11-15T00:00:00Z",
                "excerpts": [{"id": excerpt_id, "text": text, "verified": True}],
            })
            claims.append({"id": f"cl-{n:02d}", "text": text, "excerpt_ids": [excerpt_id]})
        chapters[change_id] = {"confidence": confidence, "claims": claims}
    return {"sources": sources, "chapters": chapters, "usage": {"exa_calls": 0, "llm_calls": 0}}


def _segment_for(story: dict, change_id: str) -> dict:
    """The script segment of the chapter for one change."""
    for chapter in story["chapters"]:
        if chapter["change_id"] == change_id:
            for segment in story["script"]["segments"]:
                if segment["chapter_id"] == chapter["id"]:
                    return segment
    raise AssertionError(f"no segment for {change_id}")


def test_confirmed_and_likely_claims_get_their_lead_in(tmp_path):
    """A confirmed chapter says 'Records show: …', a likely one 'One report says: …',
    in the claim's own words, and the segment lists exactly those claim ids."""
    folder, imagery = _write_imagery(tmp_path)
    changes = FIVE_CHANGES[:2]
    causes = _causes({"chg-01": ("confirmed", [DROUGHT_CLAIM, LOW_LEVEL_CLAIM]),
                      "chg-02": ("likely", [SUMMERLIN_CLAIM])})
    story = assemble_story(folder, changes, causes=causes)
    text = _check(story, imagery, changes)

    first = _segment_for(story, "chg-01")
    assert ("Records show: Since drought took hold on the Colorado and its tributaries "
            "in 1999, the surface of Lake Mead has plunged almost 130 feet.") in first["text"]
    assert ("Records also show: In August 2010, Lake Mead reached its lowest level "
            "since 1956.") in first["text"]
    assert first["claim_ids"] == ["cl-01", "cl-02"]
    second = _segment_for(story, "chg-02")
    assert "One report says: The first family moved to Summerlin in 1991." in second["text"]
    assert second["claim_ids"] == ["cl-03"]
    assert story["sources"] == causes["sources"]
    confidences = [chapter["confidence"] for chapter in story["chapters"][1:-1]]
    assert sorted(confidences) == ["confirmed", "likely"]
    for chapter in story["chapters"]:   # headlines stay neutral
        assert "drought" not in chapter["headline"].lower()
        assert "Summerlin" not in chapter["headline"]


def test_claim_sentence_never_changes_the_claim():
    """The claim follows the lead-in word for word, capitals included; only a missing full
    stop is added."""
    assert (claim_sentence("Records show:", "Water Street was rebuilt in 2012.")
            == "Records show: Water Street was rebuilt in 2012.")
    assert (claim_sentence("One report says:", "In August 2010, Lake Mead fell")
            == "One report says: In August 2010, Lake Mead fell.")
    assert claim_sentence("Records show:", "Summerlin grew?") == "Records show: Summerlin grew?"


def test_capitalised_claim_starts_are_said_verbatim(tmp_path):
    """Names at the start of a claim ("Water Street", "Construction Partners Inc.") and plain
    words alike keep their capitals in the script."""
    folder, imagery = _write_imagery(tmp_path)
    changes = FIVE_CHANGES[:2]
    street = "Water Street was rebuilt after the flood in 2012."
    company = "Construction Partners Inc. cleared 40 acres for the plant in 2016."
    causes = _causes({"chg-01": ("confirmed", [street, company]),
                      "chg-02": ("likely", ["Drought took hold in 1999."])})
    story = assemble_story(folder, changes, causes=causes)
    _check(story, imagery, changes)
    first = _segment_for(story, "chg-01")["text"]
    assert f"Records show: {street}" in first
    assert f"Records also show: {company}" in first
    assert "water Street" not in first and "construction Partners" not in first
    assert "One report says: Drought took hold in 1999." in _segment_for(story, "chg-02")["text"]


def test_lead_in_follows_each_claims_own_confidence(tmp_path):
    """A chapter with a confirmed and a likely claim introduces each by its own level."""
    folder, imagery = _write_imagery(tmp_path)
    changes = [FIVE_CHANGES[0]]
    causes = _causes({"chg-01": ("confirmed", [LOW_LEVEL_CLAIM, SUMMERLIN_CLAIM])})
    causes["claim_confidence"] = {"cl-01": "likely", "cl-02": "confirmed"}
    story = assemble_story(folder, changes, causes=causes)
    _check(story, imagery, changes)
    text = _segment_for(story, "chg-01")["text"]
    assert f"One report says: {LOW_LEVEL_CLAIM}" in text
    assert f"Records also show: {SUMMERLIN_CLAIM}" in text
    assert "claim_confidence" not in json.dumps(story)   # private: never in story.json


def test_a_long_claim_never_breaks_225_words(tmp_path):
    """Three near-maximum claims on one change: the script stays within HL-1, the first
    claim is said in full, and only claims that fit are said."""
    assert len(LONG_CLAIM) <= 240
    folder, imagery = _write_imagery(tmp_path)
    changes = [FIVE_CHANGES[0]]
    causes = _causes({"chg-01": ("confirmed", [LONG_CLAIM, LONG_CLAIM, LONG_CLAIM])})
    story = assemble_story(folder, changes, causes=causes)
    text = _check(story, imagery, changes)
    assert f"Records show: {LONG_CLAIM}" in text
    assert len(story["chapters"][1]["claims"]) == 3   # all kept in the chapter
    assert 1 <= len(_segment_for(story, "chg-01")["claim_ids"]) <= 3


def test_five_sourced_changes_with_long_claims_stay_valid(tmp_path):
    """Worst case: five flagged changes, a 99-character name, two long claims each. Still
    valid; every sensor note is said; claims that don't fit stay in their chapter unsaid."""
    folder, imagery = _write_imagery(tmp_path, display_name=LONG_NAME)
    changes = _flag_all(FIVE_CHANGES)
    spec = {}
    for i, change in enumerate(changes):
        confidence = "confirmed" if i % 2 == 0 else "likely"
        spec[change["id"]] = (confidence, [LONG_CLAIM, LONG_CLAIM])
    story = assemble_story(folder, changes, causes=_causes(spec))
    text = _check(story, imagery, changes)
    assert text.count(SENSOR_NOTE) == 5
    spoken = sum(len(segment["claim_ids"]) for segment in story["script"]["segments"])
    assert spoken >= 1
    for chapter in story["chapters"][1:-1]:
        assert len(chapter["claims"]) == 2
        assert chapter["confidence"] in ("confirmed", "likely")


def test_unsourced_chapters_stay_cause_unknown_in_the_story(tmp_path):
    """Only the sourced change gets claims. A change missing from causes, one with no
    claims, and one with claims but no usable confidence all stay cause_unknown in
    story.json (TR-8: shown in the evidence panel), and the narration never says so."""
    folder, imagery = _write_imagery(tmp_path)
    changes = FIVE_CHANGES[:4]
    causes = _causes({"chg-01": ("likely", [SUMMERLIN_CLAIM]),
                      "chg-02": ("cause_unknown", []),
                      "chg-03": ("cause_unknown", [LOW_LEVEL_CLAIM])})
    story = assemble_story(folder, changes, causes=causes)
    text = _check(story, imagery, changes)
    for chapter in story["chapters"][1:-1]:
        if chapter["change_id"] == "chg-01":
            assert chapter["confidence"] == "likely"
        else:
            assert chapter["confidence"] == "cause_unknown"
            assert chapter["claims"] == []
    assert "Lake Mead" not in text   # a claim with no usable confidence is never said


def test_tr6_allows_numbers_only_from_spoken_claims(tmp_path):
    """The extended TR-6 check: numbers from a spoken claim are allowed, numbers from a
    claim that isn't said are not, and the script holds no other numbers."""
    folder, imagery = _write_imagery(tmp_path)
    changes = [FIVE_CHANGES[0]]
    causes = _causes({"chg-01": ("confirmed", [DROUGHT_CLAIM, LOW_LEVEL_CLAIM])})
    story = assemble_story(folder, changes, causes=causes)
    text = _check(story, imagery, changes)
    allowed = _allowed_numbers(imagery, changes, story)
    assert {"130", "1956"} <= allowed and "130" in text and "1956" in text
    no_claims = assemble_story(folder, changes)
    assert "130" not in _allowed_numbers(imagery, changes, no_claims)

    # A third claim behind two long ones, with every chapter full of long claims: no room
    # is left for it, so it stays in its chapter unsaid and its number is not allowed.
    crowded = _flag_all(FIVE_CHANGES)
    unsaid = "The lake fell 51 feet in 2014."
    spec = {"chg-01": ("confirmed", [LONG_CLAIM, LONG_CLAIM, unsaid])}
    for change in crowded[1:]:
        spec[change["id"]] = ("likely", [LONG_CLAIM, LONG_CLAIM])
    story = assemble_story(folder, crowded, causes=_causes(spec))
    text = _check(story, imagery, crowded)
    assert "51" not in _allowed_numbers(imagery, crowded, story) and "51" not in text


def _random_causes(rng: random.Random, changes: list[dict]) -> dict:
    """Each change: no entry, cause_unknown, or 1-3 random claims at a random confidence."""
    spec = {}
    for change in changes:
        roll = rng.random()
        if roll < 0.25:
            continue
        if roll < 0.4:
            spec[change["id"]] = ("cause_unknown", [])
            continue
        texts = []
        for _ in range(rng.randint(1, 3)):
            texts.append(rng.choice(CLAIM_POOL))
        spec[change["id"]] = (rng.choice(["confirmed", "likely"]), texts)
    return _causes(spec)


def test_random_sourced_and_unsourced_mixes_make_a_valid_script(tmp_path):
    """Fuzz: 200 seeded random change sets with a random mix of sourced and unsourced
    chapters and claim lengths. Every one is valid, 150-225 words, covers every year once,
    TR-6 holds, no absence line is said (TR-8), and sourced segments outweigh connectors."""
    rng = random.Random(1984)
    for i in range(200):
        name = LONG_NAME if i % 3 == 0 else "Testville, Nowhere"
        folder, imagery = _write_imagery(tmp_path, story_id=f"mix-{i}-us-6km",
                                         display_name=name)
        changes = []
        for number in range(1, rng.randint(1, 5) + 1):
            changes.append(_random_change(rng, number))
        causes = _random_causes(rng, changes)
        story = assemble_story(folder, changes, causes=causes)
        text = _check(story, imagery, changes)
        _check_sourced_outweigh_connectors(story)
        assert text.count(SENSOR_NOTE) == _described_flagged(story, changes)


# --- The script plan: time goes to the sourced changes ---

def test_all_three_claims_of_a_chapter_are_said(tmp_path):
    """A sourced chapter with three short claims says all three, in order, with lead-ins."""
    folder, imagery = _write_imagery(tmp_path)
    changes = [FIVE_CHANGES[0]]
    claims = [DROUGHT_CLAIM, LOW_LEVEL_CLAIM, SUMMERLIN_CLAIM]
    story = assemble_story(folder, changes, causes=_causes({"chg-01": ("confirmed", claims)}))
    _check(story, imagery, changes)
    segment = _segment_for(story, "chg-01")
    assert segment["claim_ids"] == ["cl-01", "cl-02", "cl-03"]
    assert f"Records also show: {SUMMERLIN_CLAIM}" in segment["text"]


def test_sourced_segment_plays_its_own_years_and_gets_the_bulk(tmp_path):
    """A Paradise-like story: one long unsourced greening, one short sourced burn. The burn's
    segment spans the burn's years and gets far more words per year than the opening and
    closing (so the film slows there); the greening is described briefly in the opening."""
    folder, imagery = _write_imagery(tmp_path)
    greening = _change(1, "vegetation_gain", "ndvi_mean", "place", 1991, 2004, 0.1, 620.0,
                       "in the northwest part of the area")
    burn = _change(2, "burn", "ndvi_mean", "place", 2015, 2017, -0.3, 3100.0,
                   "across most of the area")
    fire = ("The Camp Fire ignited on the morning of November 8, 2018, and spread quickly "
            "into the town.")
    causes = _causes({"chg-02": ("confirmed", [fire])})
    story = assemble_story(folder, [greening, burn], causes=causes)
    _check(story, imagery, [greening, burn])
    burn_segment = _segment_for(story, "chg-02")
    assert (burn_segment["year_from"], burn_segment["year_to"]) == (2015, 2017)
    segments = story["script"]["segments"]
    assert len(segments) == 3   # opening, the burn, closing: the greening has no segment
    assert "turned greener" in segments[0]["text"]
    # What the viewer feels is pace: words per year on screen. The film must crawl through
    # the explained burn and move quickly through the unexplained years around it.
    def words_per_year(segment):
        years = segment["year_to"] - segment["year_from"] + 1
        return len(segment["text"].split()) / years
    assert words_per_year(burn_segment) > 2 * words_per_year(segments[0])
    assert words_per_year(burn_segment) > 2 * words_per_year(segments[-1])


def test_a_connector_covers_an_unsourced_change_between_sourced_ones(tmp_path):
    """Sourced, unsourced, sourced: the middle change gets a short connector segment on its
    own chapter, made of what changed only, with fewer words than either sourced segment."""
    folder, imagery = _write_imagery(tmp_path)
    changes = [
        _change(1, "water_loss", "water_frac", "place", 1993, 1996, -0.1, 300.0,
                "in the east part of the area"),
        _change(2, "water_gain", "water_frac", "place", 2000, 2003, 0.08, 250.0,
                "in the east part of the area"),
        _change(3, "water_loss", "water_frac", "place", 2006, 2010, -0.2, 700.0,
                "in the east part of the area"),
    ]
    causes = _causes({"chg-01": ("likely", [SUMMERLIN_CLAIM]),
                      "chg-03": ("confirmed", [LOW_LEVEL_CLAIM])})
    story = assemble_story(folder, changes, causes=causes)
    _check(story, imagery, changes)
    connector = _segment_for(story, "chg-02")
    assert connector["claim_ids"] == []
    assert connector["text"].startswith("Between 2000 and 2003,")
    _check_sourced_outweigh_connectors(story)
    sourced, connectors = _segment_kinds(story)
    assert len(sourced) == 2 and len(connectors) == 1


def test_overlapping_sourced_changes_never_overlap_in_the_film(tmp_path):
    """Two sourced changes whose years overlap still get disjoint, contiguous segments."""
    folder, imagery = _write_imagery(tmp_path)
    changes = [
        _change(1, "water_loss", "water_frac", "place", 1995, 2010, -0.2, 700.0, "east"),
        _change(2, "urban_growth", "built_frac", "local", 1995, 2000, 1.0, 6.0, "south"),
        _change(3, "burn", "ndvi_mean", "place", 2019, 2020, -0.3, 900.0, "west"),
    ]
    causes = _causes({"chg-01": ("confirmed", [LOW_LEVEL_CLAIM]),
                      "chg-02": ("likely", [SUMMERLIN_CLAIM]),
                      "chg-03": ("likely", [DROUGHT_CLAIM])})
    story = assemble_story(folder, changes, causes=causes)
    _check(story, imagery, changes)   # _check_years: contiguous, no overlaps


def test_no_sourced_change_is_a_plain_measured_film(tmp_path):
    """No verified cause anywhere: every change gets its own measured segment, the script is
    valid and 150-225 words, and nothing says a cause is missing."""
    folder, imagery = _write_imagery(tmp_path)
    changes = FIVE_CHANGES[:3]
    causes = _causes({"chg-01": ("cause_unknown", [])})
    story = assemble_story(folder, changes, causes=causes)
    text = _check(story, imagery, changes)
    for change in changes:
        segment = _segment_for(story, change["id"])
        assert segment["text"].startswith(f"Between {change['start_year']} and ")
    for chapter in story["chapters"][1:-1]:
        assert chapter["confidence"] == "cause_unknown"
    assert "haven't found" not in text


def test_no_output_ever_says_haven_t_found(tmp_path):
    """Fuzz over sourced/unsourced mixes, flags and long names: "haven't found" (or any
    other absence line) is never in the script."""
    rng = random.Random(77)
    for i in range(150):
        name = LONG_NAME if i % 4 == 0 else "Testville, Nowhere"
        folder, imagery = _write_imagery(tmp_path, story_id=f"absent-{i}-us-6km",
                                         display_name=name)
        changes = []
        for number in range(1, rng.randint(1, 5) + 1):
            changes.append(_random_change(rng, number))
        causes = _random_causes(rng, changes) if i % 2 else None
        story = assemble_story(folder, changes, causes=causes)
        text = _check(story, imagery, changes)
        assert "haven't found" not in text.lower()
