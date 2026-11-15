# Runs the imagery half of a story build: place → scenes → yearly composites → frames, metrics.
# Inputs: a place query (or a picked lat/lon/display_name), aoi_km, and a `sink` for build events.
# Outputs: frames/, thumbs/ and imagery.json under DATA_DIR/stories/<id>/, plus an ImageryResult.
#
# How it works:
#   1. Find the place (geocode) and build the square area around it (AOI).
#   2. Search Landsat for up to 6 clear summer scenes per year, 1984–2026.
#   3. Download those scenes and slice them back into one stack per year.
#   4. Clean each year into one composite, then skip years under 60% clear (FR-14).
#   5. Render a frame and thumbnail per kept year, and announce each one as an event.
#   6. Measure vegetation, water and built-up land for each year.
#   7. Save the story sections (place, coverage, frames, metrics) to imagery.json.
#
# Why "imagery half": story.json also needs changes (spike S3) and the agent's script (S4).
# Until those exist, this saves its sections in the contract's shapes, ready to merge.
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from app import store
from app.config import settings
from app.pipeline import composite, geocode, imagery, metrics, render
from app.pipeline.aoi import make_aoi
from app.pipeline.types import Aoi, Place, SceneMeta, YearComposite

MIN_CLEAR_PCT = 60       # FR-14: years with less clear area are skipped, with a reason
MIN_LAND_PCT = 1         # schema 1.2: greenness is measured on land, so a year needs some
SEASON_MONTHS = (6, 9)   # June–September: northern green season, fewest clouds and snow
FIRST_YEAR, LAST_YEAR = 1984, 2026

# Share of the progress bar reached when each stage finishes. Download and compositing
# take most of the time, so they get most of the bar.
PROGRESS = {"geocode": 0.03, "scenes": 0.10, "composite": 0.60, "metrics": 0.64}
COMPOSITE_START = PROGRESS["scenes"] + 0.01

Emit = Callable[[dict], Awaitable[None]]


# --- Build events ---

class EventLog:
    """Turns (type, message, extras) into numbered build events and passes them to a sink.
    seq must go up by exactly 1 per event so a browser can resume (CONTRACTS §3)."""

    def __init__(self, story_id: str, sink: Emit):
        self.story_id = story_id
        self.sink = sink
        self.seq = 0

    async def __call__(self, type_: str, message: str, *, stage: str | None = None,
                       status: str | None = None, progress: float | None = None,
                       **extra: Any) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        event: dict[str, Any] = {
            "type": type_,
            "story_id": self.story_id,
            "seq": self.seq,
            "ts": now,
            "message": message[:120],  # schema: maxLength 120
        }
        if stage:
            event["stage"] = stage
        if status:
            event["status"] = status
        if progress is not None:
            clamped = min(max(progress, 0.0), 1.0)
            event["progress"] = round(clamped, 3)
        event.update(extra)
        self.seq += 1
        await self.sink(event)


async def _ignore_event(_event: dict) -> None:
    """Sink used when the caller doesn't want events."""
    return None


@dataclass
class ImageryResult:
    """What the imagery half hands to change detection and the cause agent."""
    story_id: str
    place: Place
    aoi: Aoi
    kept: dict[int, YearComposite]   # only years that passed FR-14, ascending
    sections: dict                   # place / coverage / frames / metrics, in story.json shapes
    timings: dict[str, float] = field(default_factory=dict)


# --- Small helpers ---

def story_dir(story_id: str) -> Path:
    """Folder for one story's files: DATA_DIR/stories/<id> (CONTRACTS §6)."""
    return settings.data_dir / "stories" / story_id


def file_url(path: Path) -> str:
    """Turns a file path under DATA_DIR into its /files/... URL (CONTRACTS §1).
    The web app only ever sees URLs, never disk paths."""
    relative = path.resolve().relative_to(settings.data_dir)
    return "/files/" + relative.as_posix()


def seconds_since(start: float) -> float:
    """Seconds since a perf_counter() reading, rounded for the timings record."""
    return round(time.perf_counter() - start, 2)


def write_json(path: Path, obj: dict) -> None:
    """Writes JSON to a temp file, then renames it, so readers never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# --- Steps 1–3: place, scenes, downloads ---

async def find_place(query: str, lat: float | None, lon: float | None,
                     display_name: str | None, aoi_km: float,
                     sink: Emit | None) -> tuple[Place, EventLog]:
    """Geocodes the query (FR-1/2/3) and starts the event log.
    Events can only start here, because the story id is built from the place."""
    place = await geocode.resolve(query, lat=lat, lon=lon, display_name=display_name)
    story_id = store.story_id(place.display_name, place.admin["country_code"], aoi_km)
    emit = EventLog(story_id, sink or _ignore_event)
    await emit("progress", f"Found {place.display_name}", stage="geocode", status="done",
               progress=PROGRESS["geocode"])
    return place, emit


async def find_scenes(aoi: Aoi, years: range, emit: EventLog,
                      failed_years: set[int]) -> dict[int, list[SceneMeta]]:
    """Searches Landsat year by year for the clearest summer scenes (ADR-001).
    Years whose search failed are added to `failed_years`, so they get an honest reason."""
    scenes_by_year = await imagery.search_scenes(aoi, years=years, months=SEASON_MONTHS,
                                                 failed_years=failed_years)
    if not scenes_by_year:
        raise RuntimeError("no Landsat scenes found for this place")  # caller: NO_LAND_DATA

    n_scenes = 0
    for scenes in scenes_by_year.values():
        n_scenes += len(scenes)
    first, last = min(scenes_by_year), max(scenes_by_year)
    await emit("progress", f"Found {n_scenes} Landsat images from {first} to {last}",
               stage="scenes", status="done", progress=PROGRESS["scenes"])
    return scenes_by_year


async def download_scenes(aoi: Aoi, scenes_by_year: dict[int, list[SceneMeta]],
                          failed_ids: set[str]) -> np.ndarray:
    """Downloads every scene in one call (cached per scene), ordered by year.
    Ids of scenes that failed to download are added to `failed_ids`."""
    all_scenes = []
    for year in sorted(scenes_by_year):
        all_scenes.extend(scenes_by_year[year])
    return await imagery.load_scenes(aoi, all_scenes, failed_ids=failed_ids)


def split_by_year(pixels: np.ndarray, scenes_by_year: dict[int, list[SceneMeta]]
                  ) -> dict[int, tuple[np.ndarray, list[SceneMeta]]]:
    """Slices the downloaded array back into one (pixels, scenes) stack per year.
    Uses the same year order as download_scenes."""
    by_year = {}
    start = 0
    for year in sorted(scenes_by_year):
        scenes = scenes_by_year[year]
        end = start + len(scenes)
        by_year[year] = (pixels[start:end], scenes)
        start = end
    return by_year


# --- Step 4: which years to keep ---

ARCHIVE_DOWN = "the image archive didn't respond for this summer"


def _low_coverage_reason(clear_pct: float, n_failed: int, n_scenes: int) -> str:
    """Why a summer had too little usable area, without blaming clouds for a download failure.
    "Clear view" covers clouds, Landsat 7 stripe gaps and image edges alike."""
    reason = f"only {clear_pct:.0f}% of the area had a clear view"
    if n_failed:
        reason += f" ({n_failed} of {n_scenes} images failed to download)"
    return reason


def split_kept(series: dict[int, YearComposite], searched_years: range,
               scenes_by_year: dict[int, list[SceneMeta]], failed_years: set[int],
               failed_ids: set[str]) -> tuple[dict[int, YearComposite], list[dict]]:
    """Applies FR-14. Returns (kept years, skipped [{year, reason}]).
    Reasons must be true (truthfulness rules): an archive or download failure is never
    reported as "no images" or as clouds. The viewer sees them in coverage.years_skipped."""
    kept, skipped = {}, []
    for year in searched_years:
        scenes = scenes_by_year.get(year, [])
        n_failed = sum(1 for s in scenes if s.id in failed_ids)

        if year in failed_years or (scenes and n_failed == len(scenes)):
            reason = ARCHIVE_DOWN  # search failed, or every download failed
        elif not scenes:
            reason = "no Landsat images of this place that summer"
        elif series[year].clear_pct < MIN_CLEAR_PCT:
            reason = _low_coverage_reason(series[year].clear_pct, n_failed, len(scenes))
        elif metrics.land_pct(series[year]) < MIN_LAND_PCT:
            reason = "almost all water in view, so the land's greenness can't be measured"
        else:
            kept[year] = series[year]
            continue
        skipped.append({"year": year, "reason": reason})
    return kept, skipped


# --- Step 5: announce frames ---

async def announce_frames(emit: EventLog, kept: dict[int, YearComposite],
                          paths: dict[int, tuple[Path, Path]]) -> None:
    """Sends one 'frame' event per kept year, so the live feed shows thumbnails as they arrive."""
    span = PROGRESS["composite"] - PROGRESS["scenes"] - 0.01
    years = sorted(kept)
    for i, year in enumerate(years):
        progress = COMPOSITE_START + span * (i + 1) / len(kept)
        frame = {
            "year": year,
            "thumb_url": file_url(paths[year][1]),
            "clear_pct": round(kept[year].clear_pct, 1),
        }
        await emit("frame", f"Built {year}", stage="composite", progress=progress, frame=frame)


async def announce_skipped(emit: EventLog, skipped: list[dict],
                           scenes_by_year: dict[int, list[SceneMeta]]) -> None:
    """Warns about skipped years that had images. Years with no images at all are routine."""
    for skip in skipped:
        if skip["year"] in scenes_by_year:
            await emit("progress", f"Skipped {skip['year']}: {skip['reason']}",
                       stage="composite", status="warning", progress=PROGRESS["composite"])


# --- Step 7: story sections ---

def place_section(place: Place, aoi: Aoi) -> dict:
    """story.json `place`: the geocoder result plus the AOI's lon/lat box."""
    return {
        "query": place.query,
        "display_name": place.display_name,
        "lat": place.lat,
        "lon": place.lon,
        "bbox": list(aoi.bbox_ll),
        "aoi_km": aoi.aoi_km,
        "admin": place.admin,
    }


def coverage_section(kept: dict[int, YearComposite], skipped: list[dict]) -> dict:
    """story.json `coverage`: which years are in the film and why the others are not."""
    first_month, last_month = SEASON_MONTHS
    return {
        "first_year": min(kept),
        "last_year": max(kept),
        "season_months": list(range(first_month, last_month + 1)),
        "years_skipped": skipped,
    }


def frame_entry(year: int, yc: YearComposite, frame_path: Path, thumb_path: Path) -> dict:
    """One story.json `frames[]` item: image URLs plus where the pixels came from (DR-2)."""
    return {
        "year": year,
        "image_url": file_url(frame_path),
        "thumb_url": file_url(thumb_path),
        "sensors": yc.sensors,
        "scene_ids": yc.scene_ids,
        "date_from": yc.date_from,
        "date_to": yc.date_to,
        "clear_pct": round(yc.clear_pct, 1),
    }


def build_sections(place: Place, aoi: Aoi, kept: dict[int, YearComposite], skipped: list[dict],
                   paths: dict[int, tuple[Path, Path]], series_rows: list[dict]) -> dict:
    """Assembles the story.json sections this half produces, plus the cleaning notes."""
    frames = []
    notes = {}
    for year in sorted(kept):
        frame_path, thumb_path = paths[year]
        frames.append(frame_entry(year, kept[year], frame_path, thumb_path))
        notes[str(year)] = kept[year].notes
    return {
        "place": place_section(place, aoi),
        "coverage": coverage_section(kept, skipped),
        "frames": frames,
        "metrics": {"series": series_rows},
        # Not a story.json section: what the cleaning did each year, for debugging.
        "composite_notes": notes,
    }


# --- The pipeline ---

async def run_imagery(query: str, *, lat: float | None = None, lon: float | None = None,
                      display_name: str | None = None, aoi_km: float | None = None,
                      sink: Emit | None = None) -> ImageryResult:
    """Runs geocode → scenes → composite → render + metrics, emitting build events as it goes.
    Heavy numpy work runs in threads so the event stream stays live. The caller adds the
    HL-5 timeout."""
    start = time.perf_counter()
    timings: dict[str, float] = {}
    aoi_km = aoi_km or settings.aoi_km

    # 1. Place and area
    place, emit = await find_place(query, lat, lon, display_name, aoi_km, sink)
    timings["geocode_s"] = seconds_since(start)
    aoi = make_aoi(place.lat, place.lon, aoi_km)
    years = range(FIRST_YEAR, LAST_YEAR + 1)

    # 2. Scene search
    step = time.perf_counter()
    failed_years: set[int] = set()
    scenes_by_year = await find_scenes(aoi, years, emit, failed_years)
    timings["search_s"] = seconds_since(step)

    # 3. Download
    await emit("progress", "Downloading images and removing clouds, one summer at a time",
               stage="composite", status="started", progress=COMPOSITE_START)
    step = time.perf_counter()
    failed_ids: set[str] = set()
    pixels = await download_scenes(aoi, scenes_by_year, failed_ids)
    timings["download_s"] = seconds_since(step)
    pixels_by_year = split_by_year(pixels, scenes_by_year)

    # 4. Composite and keep the clear years
    step = time.perf_counter()
    series = await asyncio.to_thread(composite.composite_series, pixels_by_year)
    timings["composite_s"] = seconds_since(step)
    kept, skipped = split_kept(series, years, scenes_by_year, failed_years, failed_ids)
    if len(kept) < 2:  # schema: frames minItems 2. A film needs a before and an after.
        raise RuntimeError(f"only {len(kept)} usable summers for this place")

    # 5. Render and announce
    out = story_dir(emit.story_id)
    step = time.perf_counter()
    paths = await asyncio.to_thread(render.render_frames, kept, out)
    timings["render_s"] = seconds_since(step)
    await announce_frames(emit, kept, paths)
    await announce_skipped(emit, skipped, scenes_by_year)

    # 6. Metrics (FR-20)
    series_rows = await asyncio.to_thread(metrics.compute_metrics, kept)
    await emit("progress", "Measured vegetation, water and built-up land", stage="metrics",
               status="done", progress=PROGRESS["metrics"])

    # 7. Save
    sections = build_sections(place, aoi, kept, skipped, paths, series_rows)
    timings["total_s"] = seconds_since(start)
    write_json(out / "imagery.json", {**sections, "timings": timings})
    return ImageryResult(story_id=emit.story_id, place=place, aoi=aoi, kept=kept,
                         sections=sections, timings=timings)
