# Data types shared by the pipeline stages (geocode → imagery → composite → render/metrics → run).
# Inputs: none (definitions only).
# Outputs: Place, Aoi, SceneMeta, YearComposite dataclasses and the band-order constants.
#
# How it works:
#   1. Band constants fix the order of the 7 bands in every scene array.
#   2. Place and Aoi describe where: the resolved place and the square around it.
#   3. SceneMeta describes one Landsat scene; YearComposite is one cleaned summer image.
#
# One shared module, so stages written in parallel can't drift apart.
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Band order of every scene array [n, 7, H, W] that imagery.load_scenes returns.
# Common names, not band numbers: Landsat 4–7 and 8/9 number bands differently.
BANDS = ["blue", "green", "red", "nir08", "swir16", "swir22", "qa_pixel"]
REFL_BANDS = BANDS[:6]          # the 6 reflectance bands kept in a composite
QA = BANDS.index("qa_pixel")    # = 6
BLUE, GREEN, RED, NIR, SWIR1, SWIR2 = range(6)

# DR-1: Landsat Collection 2 Level-2 surface reflectance scaling.
SR_SCALE, SR_OFFSET = 0.0000275, -0.2

# STAC platform name → the sensor names allowed in story.schema.json frames[].sensors.
PLATFORM_TO_SENSOR = {
    "landsat-4": "Landsat 4", "landsat-5": "Landsat 5", "landsat-7": "Landsat 7",
    "landsat-8": "Landsat 8", "landsat-9": "Landsat 9",
}


@dataclass(frozen=True)
class Place:
    """A resolved place: story.json `place` without bbox/aoi_km (those come from Aoi).
    `admin` has city, county, state, country, country_code (2 lower-case letters)."""
    query: str
    display_name: str
    lat: float
    lon: float
    admin: dict
    kind: str = "place"  # Nominatim type, e.g. 'city', 'reservoir'; shown in the picker


@dataclass(frozen=True)
class Aoi:
    """The square analysis area, snapped to the Landsat 30 m grid in the local UTM zone.
    The data API crops in UTM metres (utm_bbox, epsg); STAC and story.json use bbox_ll."""
    lat: float
    lon: float
    aoi_km: float
    epsg: int
    utm_bbox: tuple[int, int, int, int]  # minx, miny, maxx, maxy in metres
    size_px: int
    bbox_ll: tuple[float, float, float, float]


@dataclass(frozen=True)
class SceneMeta:
    """One Landsat scene from a STAC search (no pixels)."""
    id: str
    datetime: str          # ISO 8601, UTC
    year: int
    platform: str          # STAC value, e.g. 'landsat-5'
    cloud_cover: float     # scene-wide eo:cloud_cover in %, used only for the first ranking
    native_epsg: int | None

    @property
    def sensor(self) -> str:
        return PLATFORM_TO_SENSOR.get(self.platform, self.platform)

    @property
    def date(self) -> str:
        return self.datetime[:10]


@dataclass
class YearComposite:
    """One clean image for one summer, plus where its pixels came from (frames[], DR-2).
    A 0 in dn is a hole (no clear view); holes are never filled from other years (ADR-002)."""
    year: int
    dn: np.ndarray            # uint16 [6, H, W], raw values in REFL_BANDS order, 0 = hole
    clear_pct: float          # % of the area with a real value (frames[].clear_pct, FR-14)
    scenes: list[SceneMeta]   # only the scenes that contributed pixels
    notes: list[str] = field(default_factory=list)  # what the cleaning did, in plain words

    @property
    def sensors(self) -> list[str]:
        return sorted({s.sensor for s in self.scenes})

    @property
    def scene_ids(self) -> list[str]:
        return [s.id for s in self.scenes]

    @property
    def date_from(self) -> str:
        return min(s.date for s in self.scenes)

    @property
    def date_to(self) -> str:
        return max(s.date for s in self.scenes)


# --- Scene ranking, shared by imagery (what to download) and composite (what to use) ---
# Penalties are in "cloud cover percentage points", so a worse kind of scene is still
# used when nothing better exists that summer (e.g. 2012 has only Landsat 7).
TIER2_PENALTY = 50        # Tier 2 = poorer geolocation/calibration; S2b found an all-white T2 scene
SLC_OFF_PENALTY = 25      # Landsat 7 after its scan-line failure: ~22% of each scene is stripe gaps
SLC_OFF_SINCE = "2003-05-31"


def scene_rank_score(scene: SceneMeta, penalise_stripes: bool = True) -> float:
    """Lower is better: cloud cover plus penalties for Tier 2 (and, optionally, striped Landsat 7).
    imagery.py penalises stripes when choosing downloads, so a summer never gets only striped
    scenes (Willow Run 2023). composite.py doesn't: once downloaded, the median fills stripes,
    and avoiding them pulled in September scenes: Central Park flicker 0.013 → 0.019 (ADR-002)."""
    score = scene.cloud_cover
    if scene.id.endswith("_T2"):
        score += TIER2_PENALTY
    if penalise_stripes and scene.platform == "landsat-7" and scene.date >= SLC_OFF_SINCE:
        score += SLC_OFF_PENALTY
    return score
