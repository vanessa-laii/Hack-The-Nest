# Tests for changes.detect_changes on small made-up places (no network, no real imagery).
# Inputs: synthetic imagery.json + composites.npz written into tmp_path.
# Outputs: pytest results.
import json

import numpy as np
import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from app.config import CONTRACTS_DIR, settings
from app.pipeline import metrics
from app.pipeline.changes import EXPECTED_SIGN, detect_changes
from app.pipeline.types import SR_OFFSET, SR_SCALE, YearComposite

SIZE = 80              # pixels on a side
AOI_KM = 2.4           # 80 × 30 m: one pixel = 0.09 ha, like real Landsat
YEARS = list(range(1990, 2006))
FLIP_YEAR = 1998


def _dn(reflectance: list[float]) -> np.ndarray:
    """6 raw band values [6, 1, 1] for the given surface reflectances (DR-1, inverted)."""
    values = [(r - SR_OFFSET) / SR_SCALE for r in reflectance]
    return np.array(values, dtype=np.float64).round().astype(np.uint16)[:, None, None]


# blue, green, red, nir, swir1, swir2
GREEN_LAND = _dn([0.04, 0.08, 0.05, 0.35, 0.20, 0.10])   # NDVI 0.75
BUILT = _dn([0.12, 0.14, 0.16, 0.20, 0.28, 0.24])        # NDBI > 0, NDVI 0.11, MNDWI < 0
WATER = _dn([0.05, 0.06, 0.04, 0.02, 0.01, 0.01])        # MNDWI > 0


def _paint(classes: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """A composite [6, H, W] from a class map (0 green land, 1 built, 2 water), with small
    noise so the series aren't perfectly flat."""
    dn = np.where(classes == 0, GREEN_LAND, np.where(classes == 1, BUILT, WATER))
    noise = rng.integers(-60, 61, size=dn.shape)
    return (dn.astype(np.int32) + noise).astype(np.uint16)


def _write_story(story_dir, class_maps: dict[int, np.ndarray]) -> None:
    """Writes imagery.json and composites.npz, in run.py's formats, for the class maps."""
    rng = np.random.default_rng(7)
    series = {}
    for year, classes in class_maps.items():
        series[year] = YearComposite(year=year, dn=_paint(classes, rng), clear_pct=100.0,
                                     scenes=[])
    story_dir.mkdir(parents=True)
    years = sorted(series)
    dn = np.stack([series[year].dn for year in years])
    np.savez_compressed(story_dir / "composites.npz", years=np.array(years, np.int16), dn=dn)
    frames = []
    for year in years:
        frames.append({"year": year, "sensors": ["Landsat 5"]})
    imagery = {
        "place": {"aoi_km": AOI_KM, "bbox": [-83.6, 42.2, -83.57, 42.22]},
        "frames": frames,
        "metrics": {"series": metrics.compute_metrics(series)},
    }
    (story_dir / "imagery.json").write_text(json.dumps(imagery), encoding="utf-8")


def _maps(before: np.ndarray, after: np.ndarray) -> dict[int, np.ndarray]:
    """The `before` class map up to FLIP_YEAR, the `after` one from then on."""
    maps = {}
    for year in YEARS:
        maps[year] = before if year < FLIP_YEAR else after
    return maps


def _mixed_land() -> np.ndarray:
    """Green land with a built-up strip and a pond, so every metric has something in it."""
    classes = np.zeros((SIZE, SIZE), dtype=np.int8)
    classes[:, :12] = 1
    classes[60:75, 60:75] = 2
    return classes


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """DATA_DIR points at tmp_path, so mask URLs are /files/stories/<id>/masks/..."""
    monkeypatch.setattr(settings, "data_dir_raw", str(tmp_path))
    return tmp_path


def _validator() -> Draft202012Validator:
    """A validator for the story schema's changes[] list, sharing its $defs."""
    schema = json.loads((CONTRACTS_DIR / "story.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator({**schema["properties"]["changes"], "$defs": schema["$defs"]})


def _check_honest(changes: list[dict]) -> None:
    """Every change is schema-valid and its kind agrees with its number's direction."""
    assert list(_validator().iter_errors(changes)) == []
    for change in changes:
        sign = EXPECTED_SIGN[(change["kind"], change["metric"])]
        assert change["magnitude"] * sign > 0, change


def test_stable_place_has_no_changes(data_dir):
    land = _mixed_land()
    story = data_dir / "stories" / "stable"
    _write_story(story, _maps(land, land))
    assert detect_changes(story) == []
    assert not (story / "masks").exists()


def test_lake_shrinking_is_place_wide_water_loss(data_dir):
    """Half the area is a lake that shrinks to a quarter: one place-wide water loss."""
    full = np.zeros((SIZE, SIZE), dtype=np.int8)
    full[:, SIZE // 2:] = 2
    shrunk = np.zeros((SIZE, SIZE), dtype=np.int8)
    shrunk[:, 3 * SIZE // 4:] = 2
    story = data_dir / "stories" / "lake"
    _write_story(story, _maps(full, shrunk))
    changes = detect_changes(story)

    _check_honest(changes)
    water = [c for c in changes if c["kind"] == "water_loss"]
    assert len(water) == 1
    assert water[0]["scope"] == "place"
    assert water[0]["metric"] == "water_frac"
    assert water[0]["magnitude"] < 0
    # Exposed lake bed is never told as urban growth (shrinking-lake rule).
    assert all(c["kind"] != "urban_growth" for c in changes)


def test_six_hectare_patch_is_a_local_change_with_mask(data_dir):
    """A 6 × 11 pixel field (≈ 5.9 ha) that becomes built-up: a local urban_growth."""
    before = _mixed_land()
    after = before.copy()
    after[20:26, 30:41] = 1
    story = data_dir / "stories" / "patch"
    _write_story(story, _maps(before, after))
    changes = detect_changes(story)

    _check_honest(changes)
    assert len(changes) == 1
    change = changes[0]
    assert change["kind"] == "urban_growth"
    assert change["scope"] == "local"
    assert change["area_ha"] == pytest.approx(6.0, abs=0.2)
    assert change["magnitude"] == pytest.approx(1.0, abs=0.01)   # measured inside the patch
    assert change["start_year"] == FLIP_YEAR - 1
    assert change["end_year"] == FLIP_YEAR
    assert "cleared or built over" in change["region"]["description"]
    assert change["mask_url"] == "/files/stories/patch/masks/chg-01.png"

    mask = Image.open(story / "masks" / "chg-01.png")
    assert mask.mode == "L"
    assert mask.size == (1024, 1024)
    pixels = np.array(mask)
    rows, cols = np.nonzero(pixels == 255)
    scale = 1024 / SIZE      # nearest upscale: each Landsat pixel is a 12.8 px square
    assert rows.min() == int(np.ceil(20 * scale)) and cols.min() == int(np.ceil(30 * scale))
    assert set(np.unique(pixels)) == {0, 255}
    assert not list(story.glob("masks/*.tmp"))


def test_no_change_contradicts_its_kind(data_dir):
    """A built-up block that turns green is re-told as greening, never as built-up growth or
    as a 'demolition' with the wrong sign; every change agrees with its kind."""
    before = _mixed_land()
    before[40:50, 20:40] = 1
    after = _mixed_land()
    story = data_dir / "stories" / "regrowth"
    _write_story(story, _maps(before, after))
    changes = detect_changes(story)

    _check_honest(changes)
    assert changes, "the regrowth should be found"
    for change in changes:
        assert change["kind"] not in {"urban_growth", "other"}
