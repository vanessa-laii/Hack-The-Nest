# Turns what the user typed (or picked) into one Place via Nominatim (FR-1, FR-2, FR-3).
# Inputs: query text, or lat/lon/display_name from the picker; settings (user agent, data_dir).
# Outputs: Place, or ApiError PLACE_NOT_FOUND / PLACE_AMBIGUOUS / NO_LAND_DATA / UPSTREAM_ERROR.
#
# How it works:
#   1. If the user picked a point from the list, only reverse-geocode it to fill `admin`.
#   2. Otherwise search Nominatim (disk cache first, at most 1 request/s, one retry).
#   3. Parse the results; drop ones with no country; mark seas.
#   4. Sort by importance. A sea on top means "no land" (NO_LAND_DATA).
#   5. Apply the ambiguity rule: one clear winner → return it; several real rivals →
#      PLACE_AMBIGUOUS with up to 5 candidates for the picker.
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from pathlib import Path

import httpx

from app import net
from app.config import settings
from app.errors import ApiError
from app.pipeline.types import Place

log = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org"

# Nominatim usage policy: absolute maximum of 1 request per second.
MIN_INTERVAL_S = 1.0
TIMEOUT_S = 15.0

# --- Ambiguity rule (FR-2) -------------------------------------------------
# Another result is a "rival" of the top result only if both hold:
#   1. Its importance is at least RIVAL_IMPORTANCE_RATIO × the top importance.
#      Importance means roughly "how well known". Paris, Texas rivals Paris, France;
#      small hamlets called "Central Park" do not rival the Manhattan park.
#   2. It is a different place: more than RIVAL_MIN_KM away and not inside the top
#      result's bounding box (or the other way round). Nominatim often returns the
#      same lake as polygon, park and dam; they all give the same film, so no question.
# Exception for obscure names: if the top importance is below OBSCURE_IMPORTANCE
# (no Wikipedia signal) and more than half the results are the same place as the
# top, take the top. Obscure features all share one flat importance, so rule 1
# can't separate them; e.g. "Willow Run, Michigan" gives 4 hits at Ypsilanti and
# 2 unrelated creeks, and the majority cluster is the real answer.
# Exception for tiny settlements: when the top is a town or city, a hamlet (or a census
# area, a farm, a lone house) is never its rival. "Paradise, California" gives the Camp
# Fire town and a Mono County hamlet of a few dozen people with similar importance.
# We compare kinds, not place_rank gaps: Paris, Texas (rank 16) is 4 ranks below
# Paris, France (rank 12) yet is a real town people mean, so it must stay a rival.
# At least one rival → PLACE_AMBIGUOUS with up to MAX_CANDIDATES candidates.
RIVAL_IMPORTANCE_RATIO = 0.5
RIVAL_MIN_KM = 25.0
OBSCURE_IMPORTANCE = 0.2
MAX_CANDIDATES = 5
TOWN_OR_BIGGER = {"city", "town", "municipality"}
MINOR_SETTLEMENTS = {"hamlet", "isolated_dwelling", "farm", "locality", "census"}

# Nominatim feature types that mean "open sea", not a lake or reservoir (FR-3).
# Bays and straits are left out on purpose: they usually have land in a 6 km box.
SEA_TYPES = {"sea", "ocean"}

# Countries where "Name, State" reads naturally (Ypsilanti, Michigan). Elsewhere
# we use "Name, Country" (Paris, France): regions like Île-de-France are less known.
STATE_STYLE_COUNTRIES = {"us", "ca", "au"}

EARTH_RADIUS_KM = 6371.0

NOT_FOUND_MESSAGE = "We couldn't find that place. Try adding the state or country."
NO_LAND_MESSAGE = "Revisit works on land. Try a nearby town."

# Module-level rate-limit state, shared by every resolve() in the process.
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None
_last_request = 0.0


# --- Fetching (cache, rate limit, retry) ------------------------------------

def _cache_dir() -> Path:
    """Where raw Nominatim responses are cached.
    Raw JSON (not Places), so a later fix to parsing also applies to cached queries."""
    return settings.data_dir / "cache" / "geocode"


def _normalise_query(q: str) -> str:
    """Lower-case and collapse spaces so 'Paris ' and 'paris' share a cache entry."""
    return " ".join(q.split()).lower()


def _get_lock() -> asyncio.Lock:
    """The rate-limit lock for the running event loop.
    Made per loop because a lock is tied to one loop, and tests run several."""
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


async def _throttled_get(client: httpx.AsyncClient, path: str, params: dict) -> httpx.Response:
    """One GET, at least MIN_INTERVAL_S after the previous one in this process.
    The lock is held for the whole request, so two users at once can't break 1 req/s."""
    global _last_request
    async with _get_lock():
        wait = _last_request + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request = time.monotonic()
        try:
            return await client.get(f"{NOMINATIM}{path}", params=params)
        finally:
            _last_request = time.monotonic()


def _cache_path(cache_key: dict) -> Path:
    """The cache file for one request, named by a hash of its key."""
    key_text = json.dumps(cache_key, sort_keys=True)
    digest = hashlib.sha256(key_text.encode()).hexdigest()[:32]
    return _cache_dir() / f"{digest}.json"


def _read_cache(path: Path):
    """The cached JSON, or None if missing. A corrupt cache file counts as a miss."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, data) -> None:
    """Saves JSON via a temp file, so a crash never leaves a half-written entry."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("geocode cache write failed: %s", e)


async def _get_with_retry(path: str, params: dict):
    """Calls Nominatim with one retry on network errors, timeouts, 429 and 5xx.
    Returns the parsed JSON; after the retry, raises UPSTREAM_ERROR (CONTRACTS §5)."""
    headers = {"User-Agent": settings.nominatim_user_agent}
    last_err = ""
    async with net.http_client(timeout=TIMEOUT_S, max_connections=1, headers=headers) as client:
        for _attempt in range(2):
            try:
                r = await _throttled_get(client, path, params)
                if r.status_code == 429 or r.status_code >= 500:
                    last_err = f"HTTP {r.status_code}"
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as e:
                # TransportError covers timeouts and connection failures;
                # ValueError is a body that isn't JSON.
                last_err = type(e).__name__
    log.warning("nominatim %s failed after retry: %s", path, last_err)
    raise ApiError("UPSTREAM_ERROR",
                   "The place lookup service is not responding. Try again shortly.")


async def _fetch(path: str, params: dict, cache_key: dict):
    """Cached, rate-limited, retried Nominatim GET returning the parsed JSON.
    A repeated query never hits Nominatim, and a single blip doesn't fail a story."""
    path_on_disk = _cache_path(cache_key)
    cached = _read_cache(path_on_disk)
    if cached is not None:
        return cached
    data = await _get_with_retry(path, params)
    _write_cache(path_on_disk, data)
    return data


# --- Reading results ----------------------------------------------------------

def _admin(address: dict) -> dict:
    """Nominatim `address` → story.json place.admin.
    country_code may be None; callers treat that as 'not found' or 'open water'."""
    city = (address.get("city") or address.get("town") or address.get("village")
            or address.get("hamlet") or address.get("municipality"))
    state = address.get("state") or address.get("province") or address.get("region")
    cc = (address.get("country_code") or "").lower()
    country_code = cc if len(cc) == 2 and cc.isalpha() else None
    return {
        "city": city,
        "county": address.get("county"),
        "state": state,
        "country": address.get("country"),
        "country_code": country_code,
    }


def _short_name(res: dict, admin: dict) -> str:
    """Short human name: 'Ypsilanti, Michigan', 'Paris, France'.
    Nominatim's display_name is too long: it lists every enclosing area."""
    first_part = (res.get("display_name") or "").split(",")[0].strip()
    name = res.get("name") or admin["city"] or first_part
    country = admin["country"]
    state = admin["state"]
    if admin["country_code"] in STATE_STYLE_COUNTRIES and state and state != name:
        return f"{name}, {state}"
    if country and country != name:
        return f"{name}, {country}"
    return name or "Unknown place"


def _kind(res: dict) -> str:
    """The feature type shown to users, e.g. 'city' or 'reservoir'."""
    return res.get("addresstype") or res.get("type") or "place"


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lon) points (haversine)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    lat_part = math.sin((lat2 - lat1) / 2) ** 2
    lon_part = math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    h = lat_part + lon_part
    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(h))


def _in_bbox(pt: tuple[float, float], res: dict) -> bool:
    """True if pt (lat, lon) lies inside res's Nominatim boundingbox [S, N, W, E]."""
    try:
        south, north, west, east = map(float, res["boundingbox"])
    except (KeyError, ValueError, TypeError):
        return False
    lat, lon = pt
    return south <= lat <= north and west <= lon <= east


def _same_place(a: dict, b: dict) -> bool:
    """Whether two results are the same place for a 3–10 km film (rule 2 above)."""
    point_a = (a["_lat"], a["_lon"])
    point_b = (b["_lat"], b["_lon"])
    if _km(point_a, point_b) <= RIVAL_MIN_KM:
        return True
    return _in_bbox(point_a, b) or _in_bbox(point_b, a)


def _is_sea(res: dict) -> bool:
    """A sea or ocean feature (FR-3). Lakes and reservoirs have other types, so they pass."""
    return res.get("type") in SEA_TYPES or res.get("addresstype") in SEA_TYPES


def _to_place(query: str, res: dict, display_name: str | None = None) -> Place:
    """Builds the Place from a parsed result (with _admin already attached)."""
    admin = res["_admin"]
    return Place(
        query=query,
        display_name=display_name or _short_name(res, admin),
        lat=res["_lat"],
        lon=res["_lon"],
        admin=admin,
        kind=_kind(res),
    )


# --- Picker path (reverse geocode) --------------------------------------------

async def _reverse(query: str, lat: float, lon: float, display_name: str | None) -> Place:
    """Picker path: the user already chose a point, so only fill `admin` and check for sea.
    Nominatim answers {"error": ...} for open ocean, which has no address at all."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ApiError("INVALID_REQUEST", "Something went wrong with that request.")
    params = {
        "lat": f"{lat:.5f}",
        "lon": f"{lon:.5f}",
        "format": "jsonv2",
        "addressdetails": 1,
        "accept-language": "en",
    }
    data = await _fetch("/reverse", params, {"ep": "reverse", **params})

    no_land = ApiError("NO_LAND_DATA", NO_LAND_MESSAGE)
    if not isinstance(data, dict) or "error" in data or _is_sea(data):
        raise no_land
    admin = _admin(data.get("address") or {})
    if not admin["country_code"] or not admin["country"]:
        raise no_land  # outside every country = international waters

    # Keep the user's exact point, not Nominatim's.
    res = {**data, "_lat": lat, "_lon": lon, "_admin": admin}
    return _to_place(query, res, display_name)


# --- Search path ------------------------------------------------------------

def _parse_results(data) -> list[dict]:
    """Adds _lat, _lon, _imp, _admin (and _sea) to each result.
    Drops broken results and land results without a country (the schema needs one)."""
    if not isinstance(data, list):
        return []
    results = []
    for raw in data:
        try:
            parsed = {
                **raw,
                "_lat": float(raw["lat"]),
                "_lon": float(raw["lon"]),
                "_imp": float(raw.get("importance") or 0.0),
                "_admin": _admin(raw.get("address") or {}),
            }
        except (KeyError, ValueError, TypeError):
            continue
        has_country = parsed["_admin"]["country_code"] and parsed["_admin"]["country"]
        if _is_sea(parsed):
            parsed["_sea"] = True
        elif not has_country:
            continue
        results.append(parsed)
    return results


def _sort_key(res: dict) -> tuple:
    """Most important first; near-ties go to the more significant feature (lower place_rank).
    Live "Paris" returns the city and a district boundary at the same 0.897; we want the city."""
    importance = round(res["_imp"], 2)
    place_rank = res.get("place_rank") or 99
    return (-importance, place_rank)


def _majority_is_top(top: dict, land: list[dict]) -> bool:
    """Obscure-name exception: more than half the land results are the same place as top."""
    same_count = 0
    for res in land:
        if _same_place(res, top):
            same_count += 1
    return same_count * 2 > len(land)


def _is_minor_settlement(res: dict, top: dict) -> bool:
    """A hamlet-sized place next to a top town or city (tiny-settlement exception above)."""
    top_is_town = _kind(top) in TOWN_OR_BIGGER
    return top_is_town and _kind(res) in MINOR_SETTLEMENTS


def _pick_rivals(top: dict, land: list[dict]) -> list[dict]:
    """The top result plus every rival, each a different place from those already picked."""
    floor = top["_imp"] * RIVAL_IMPORTANCE_RATIO
    picks = [top]
    for res in land[1:]:
        if res["_imp"] < floor:
            continue
        if _is_minor_settlement(res, top):
            continue
        already_picked = any(_same_place(res, p) for p in picks)
        if not already_picked:
            picks.append(res)
    return picks


def _candidates(picks: list[dict]) -> list[dict]:
    """Picker options for PLACE_AMBIGUOUS, at most MAX_CANDIDATES.
    Repeated names ("Springfield, Illinois" city and township) get the county added."""
    cands = []
    for p in picks[:MAX_CANDIDATES]:
        cands.append({
            "display_name": _short_name(p, p["_admin"]),
            "lat": round(p["_lat"], 5),
            "lon": round(p["_lon"], 5),
            "kind": _kind(p),
        })

    names = [c["display_name"] for c in cands]
    for cand, pick in zip(cands, picks):
        county = pick["_admin"]["county"]
        if names.count(cand["display_name"]) > 1 and county:
            head, _, tail = cand["display_name"].partition(", ")
            if tail:
                cand["display_name"] = f"{head}, {county}, {tail}"
            else:
                cand["display_name"] = f"{head}, {county}"
    return cands


async def resolve(query: str, lat: float | None = None, lon: float | None = None,
                  display_name: str | None = None) -> Place:
    """Resolve a typed query (or a picked point) to one Place. See README.md geocode.py."""
    query = " ".join((query or "").split())
    if lat is not None and lon is not None:
        return await _reverse(query, float(lat), float(lon), display_name)
    if not query:
        raise ApiError("PLACE_NOT_FOUND", NOT_FOUND_MESSAGE)

    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 10,
        "accept-language": "en",
    }
    cache_key = {"ep": "search", **params, "q": _normalise_query(query)}
    data = await _fetch("/search", params, cache_key)

    results = _parse_results(data)
    if not results:
        raise ApiError("PLACE_NOT_FOUND", NOT_FOUND_MESSAGE)
    results.sort(key=_sort_key)
    top = results[0]
    if top.get("_sea"):
        raise ApiError("NO_LAND_DATA", NO_LAND_MESSAGE)
    land = [r for r in results if not r.get("_sea")]

    if top["_imp"] < OBSCURE_IMPORTANCE and _majority_is_top(top, land):
        return _to_place(query, top)

    picks = _pick_rivals(top, land)
    if len(picks) == 1:
        return _to_place(query, top)

    cands = _candidates(picks)
    raise ApiError("PLACE_AMBIGUOUS", f'Several places match "{query}".', candidates=cands)
