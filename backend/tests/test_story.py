# Tests for story.assemble_story: contract-valid output, 150–225 words, and no invented numbers.
# Inputs: a synthetic imagery.json written to a temp folder, plus synthetic changes.
# Outputs: pytest results.
import copy
import json
import random
import re

from app.contract_check import story_errors
from app.pipeline.story import SENSOR_NOTE, UNKNOWN_CAUSE, assemble_story, intro_headline

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


def _allowed_numbers(imagery: dict, changes: list[dict]) -> set[str]:
    """Every number the script may say (TR-6), worked out here from the inputs on their own:
    years, rounded hectares and shares, counts of summers, the area size, satellite numbers."""
    allowed = set()
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


def _check(story: dict, imagery: dict, changes: list[dict]) -> str:
    """The shared assertions: valid contract, HL-1 length, only input numbers (TR-6)."""
    assert story_errors(story) == []
    text = _script_text(story)
    words = len(text.split())
    assert 150 <= words <= 225
    assert story["script"]["word_count"] == words
    allowed = _allowed_numbers(imagery, changes)
    invented = [n for n in _numbers_in(text) if n not in allowed]
    assert invented == [], f"numbers not in the inputs: {invented}"
    return text


def test_one_place_and_one_local_change(tmp_path):
    """A valid story whose change chapters say the cause is unknown (TR-8)."""
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
    assert text.count("We haven't found what caused this change.") == 2
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
    every truth sentence (cause unknown, sensor note) is still said for every change."""
    assert len(LONG_NAME) == 99
    folder, imagery = _write_imagery(tmp_path, display_name=LONG_NAME)
    changes = _flag_all(FIVE_CHANGES)
    story = assemble_story(folder, changes)
    text = _check(story, imagery, changes)
    assert text.count(UNKNOWN_CAUSE) == 5
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
        assert text.count(UNKNOWN_CAUSE) == len(changes)
        n_flagged = sum(1 for change in changes if change["sensor_switch_flag"])
        assert text.count(SENSOR_NOTE) == n_flagged


def test_intro_headline_never_loses_the_year():
    """Long names are shortened, never the year; the result fits the schema's 60 chars."""
    assert intro_headline("Lake Mead, Nevada", 1984) == "Lake Mead, Nevada in 1984"
    for name in (LONG_NAME, "x" * 99, "Llanfairpwllgwyngyll " * 5):
        title = intro_headline(name, 1984)
        assert title.endswith(" in 1984")
        assert len(title) <= 60


def test_too_long_required_text_switches_to_short_wording(tmp_path):
    """A 200-character name (the most POST accepts) pushes the required text over 225 words.
    Required sentences switch to their short wording (here the opening); no truth sentence
    is dropped and the script is still valid."""
    name = LONG_NAME + ", " + LONG_NAME
    assert len(name) == 200
    folder, imagery = _write_imagery(tmp_path, display_name=name)
    changes = _flag_all(FIVE_CHANGES)
    story = assemble_story(folder, changes)
    text = _check(story, imagery, changes)
    assert f"seen by Landsat from {FIRST_YEAR} to {LAST_YEAR}." in text   # short opening
    assert text.count(UNKNOWN_CAUSE) == 5
    assert text.count(SENSOR_NOTE) == 5
