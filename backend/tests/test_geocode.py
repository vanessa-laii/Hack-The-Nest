# Offline tests for app.pipeline.geocode (FR-1, FR-2, FR-3): no network, Nominatim is faked.
# Inputs: recorded-style Nominatim JSON served by httpx.MockTransport; a temp cache dir.
# Outputs: pytest pass/fail.
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest

# Make `app` importable when pytest is run from the repo root (no conftest/pyproject here).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import net  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.pipeline import geocode  # noqa: E402


def _res(name, lat, lon, imp, address, typ="city", cat="place", bbox=None, addresstype=None):
    """One Nominatim jsonv2 search/reverse result in the shape the live API returns."""
    lat, lon = float(lat), float(lon)
    return {
        "place_id": hash((name, lat, lon)) & 0xFFFFFF, "lat": str(lat), "lon": str(lon),
        "category": cat, "type": typ, "addresstype": addresstype or typ, "importance": imp,
        "name": name, "display_name": f"{name}, long, chain, of, areas",
        "address": address,
        "boundingbox": [str(x) for x in (bbox or (lat - 0.05, lat + 0.05, lon - 0.05, lon + 0.05))],
    }


US_NV = {"county": "Clark County", "state": "Nevada",
         "country": "United States", "country_code": "us"}
US_CA = {"town": "Paradise", "county": "Butte County", "state": "California",
         "country": "United States", "country_code": "us"}
FR = {"city": "Paris", "state": "Ile-de-France", "country": "France", "country_code": "fr"}
US_TX = {"city": "Paris", "county": "Lamar County", "state": "Texas",
         "country": "United States", "country_code": "us"}

LAKE_MEAD = [
    # The lake polygon, the recreation area around it (centroid far away but its
    # bbox contains the lake) and a nearby marina: one place, not ambiguous.
    _res("Lake Mead", 36.13, -114.44, 0.55, US_NV, typ="reservoir", cat="natural",
         bbox=(35.9, 36.5, -114.9, -113.9)),
    _res("Lake Mead National Recreation Area", 35.6, -114.5, 0.50, US_NV, typ="protected_area",
         cat="boundary", bbox=(35.0, 36.6, -115.0, -113.8)),
    _res("Lake Mead Marina", 36.03, -114.77, 0.2, US_NV, typ="marina", cat="leisure"),
]
PARIS = [
    _res("Paris", 48.8566, 2.3522, 0.88, FR),
    _res("Paris", 33.6609, -95.5555, 0.57, US_TX),
    _res("Paris", 48.85, 2.35, 0.80, FR, typ="administrative", cat="boundary"),  # same city again
    _res("Paris", 36.30, -88.32, 0.30, {"city": "Paris", "state": "Tennessee",
                                        "country": "United States", "country_code": "us"}),
]


class FakeNominatim:
    """A MockTransport handler that records requests and answers from a dict by path."""

    def __init__(self, responses: dict, fail_times: int = 0):
        self.responses, self.fail_times = responses, fail_times
        self.calls: list[tuple[float, httpx.Request]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((time.monotonic(), request))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise httpx.ConnectTimeout("simulated timeout", request=request)
        return httpx.Response(200, json=self.responses[request.url.path])


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Patches http_client to use the fake, points the cache at a temp dir and
    shortens the rate limit so tests are fast. Returns a factory for the fake."""
    monkeypatch.setattr(geocode, "_cache_dir", lambda: tmp_path / "cache" / "geocode")
    monkeypatch.setattr(geocode, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(geocode, "_last_request", 0.0)

    def install(responses, fail_times=0):
        handler = FakeNominatim(responses, fail_times)
        real = net.http_client

        def client(**kw):
            c = real(**kw)  # keeps headers/timeout so we can assert on them
            c._transport = httpx.MockTransport(handler)
            return c

        monkeypatch.setattr(net, "http_client", client)
        return handler

    return install


def run(coro):
    return asyncio.run(coro)


def test_single_result_lake_mead(fake):
    h = fake({"/search": LAKE_MEAD})
    p = run(geocode.resolve("Lake Mead, Nevada"))
    assert p.display_name == "Lake Mead, Nevada"
    assert (p.lat, p.lon) == (36.13, -114.44)
    assert p.admin["country_code"] == "us" and p.admin["state"] == "Nevada"
    assert p.kind == "reservoir"
    req = h.calls[0][1]
    assert req.headers["User-Agent"] == geocode.settings.nominatim_user_agent
    assert req.url.params["q"] == "Lake Mead, Nevada"


def test_minor_namesakes_do_not_make_ambiguous(fake):
    # Paradise, California: the town plus a far-away, much less important hamlet.
    fake({"/search": [
        _res("Paradise", 39.7596, -121.6219, 0.52, US_CA, typ="town"),
        _res("Paradise", 37.0, -120.0, 0.12, {**US_CA, "county": "Madera County"}, typ="hamlet"),
    ]})
    p = run(geocode.resolve("Paradise, California"))
    assert p.display_name == "Paradise, California"
    assert p.admin["city"] == "Paradise" and p.admin["county"] == "Butte County"


def _ranked(res, place_rank):
    """A result with Nominatim's place_rank set (larger = smaller feature)."""
    res["place_rank"] = place_rank
    return res


def test_hamlet_is_not_a_rival_of_a_town(fake):
    # Recorded shape of live "Paradise, California" (2026-11-14): the Camp Fire town,
    # plus a Mono County hamlet and its census area at similar importance.
    mono = {**US_CA, "county": "Mono County"}
    fake({"/search": [
        _ranked(_res("Paradise", 39.76, -121.622, 0.474, US_CA, typ="administrative",
                     cat="boundary", addresstype="town"), 16),
        _ranked(_res("Paradise", 37.48, -118.606, 0.367, mono, typ="census",
                     cat="boundary"), 25),
        _ranked(_res("Paradise", 37.48, -118.606, 0.367, mono, typ="hamlet"), 20),
    ]})
    p = run(geocode.resolve("Paradise, California"))
    assert p.kind == "town" and p.admin["county"] == "Butte County"


def test_city_and_town_paris_still_ambiguous(fake):
    # Live ranks: Paris, France is rank 12, Paris, Texas rank 16. A town stays a rival.
    fake({"/search": [
        _ranked(_res("Paris", 48.8535, 2.3484, 0.897, FR, typ="administrative",
                     cat="boundary", addresstype="city"), 12),
        _ranked(_res("Paris", 33.6609, -95.5555, 0.530, US_TX, typ="administrative",
                     cat="boundary", addresstype="town"), 16),
    ]})
    with pytest.raises(ApiError) as e:
        run(geocode.resolve("Paris"))
    assert e.value.code == "PLACE_AMBIGUOUS"
    assert [c["display_name"] for c in e.value.candidates] == ["Paris, France", "Paris, Texas"]


def test_two_far_apart_towns_still_ambiguous(fake):
    oregon = {"town": "Paradise", "state": "Oregon", "country": "United States",
              "country_code": "us"}
    fake({"/search": [
        _ranked(_res("Paradise", 39.76, -121.622, 0.47, US_CA, typ="town"), 16),
        _ranked(_res("Paradise", 44.0, -122.0, 0.40, oregon, typ="town"), 16),
    ]})
    with pytest.raises(ApiError) as e:
        run(geocode.resolve("Paradise"))
    assert e.value.code == "PLACE_AMBIGUOUS" and len(e.value.candidates) == 2


def test_ambiguous_paris(fake):
    fake({"/search": PARIS})
    with pytest.raises(ApiError) as e:
        run(geocode.resolve("Paris"))
    err = e.value
    assert err.code == "PLACE_AMBIGUOUS"
    names = [c["display_name"] for c in err.candidates]
    # France first (most important), Texas second; the duplicate boundary of Paris
    # France is merged; Tennessee is below half the top importance so left out.
    assert names == ["Paris, France", "Paris, Texas"]
    assert set(err.candidates[0]) == {"display_name", "lat", "lon", "kind"}
    assert len(err.candidates) <= 5


def MI(county):  # noqa: N802 (reads like the other address constants)
    """A Michigan address in the given county."""
    return {"county": county, "state": "Michigan", "country": "United States", "country_code": "us"}


# Recorded from live Nominatim on 2026-11-14 (trimmed): six obscure hits with flat importance.
def _creek(lat, lon, county):
    """One obscure 'Willow Run' stream result."""
    return _res("Willow Run", lat, lon, 0.107, MI(county), typ="stream", cat="waterway")


WILLOW_RUN = [
    _creek(42.2287043, -83.5442090, "Washtenaw County"),
    _creek(42.2233380, -83.5408486, "Washtenaw County"),
    _creek(41.9431542, -83.4675566, "Monroe County"),
    _creek(42.2158929, -83.5238497, "Washtenaw County"),
    _creek(44.4515451, -84.8935702, "Missaukee County"),
    _res("Willow Run", 42.2576481, -83.5744874, 0.08, MI("Washtenaw County"), typ="neighbourhood"),
]


def test_obscure_name_majority_cluster_resolves(fake):
    fake({"/search": WILLOW_RUN})
    p = run(geocode.resolve("Willow Run, Michigan"))
    assert p.display_name == "Willow Run, Michigan"
    assert p.admin["county"] == "Washtenaw County"


def test_obscure_name_without_majority_is_ambiguous(fake):
    # Three equally obscure creeks in three counties: no signal, so ask.
    fake({"/search": [WILLOW_RUN[0], WILLOW_RUN[2], WILLOW_RUN[4]]})
    with pytest.raises(ApiError) as e:
        run(geocode.resolve("Willow Run, Michigan"))
    assert e.value.code == "PLACE_AMBIGUOUS"
    # Same short name → county added so the picker options differ.
    assert [c["display_name"] for c in e.value.candidates] == [
        "Willow Run, Washtenaw County, Michigan", "Willow Run, Monroe County, Michigan",
        "Willow Run, Missaukee County, Michigan"]


def test_importance_tie_prefers_more_significant_feature(fake):
    # Live "Paris": an arrondissement-level 'suburb' and the 'city' share importance 0.897.
    sub = _res("Paris", 48.8588897, 2.3200410, 0.897, FR, typ="administrative", cat="boundary",
               addresstype="suburb")
    sub["place_rank"] = 15
    city = _res("Paris", 48.8534951, 2.3483915, 0.897, FR, typ="administrative", cat="boundary",
                addresstype="city")
    city["place_rank"] = 12
    fake({"/search": [sub, city]})
    p = run(geocode.resolve("Paris, France"))
    assert p.kind == "city" and p.display_name == "Paris, France"


def test_ambiguous_caps_at_five(fake):
    springs = [_res("Springfield", 30 + i, -90 - i, 0.6 - i * 0.01,
                    {"city": "Springfield", "state": f"State{i}", "country": "United States",
                     "country_code": "us"}) for i in range(8)]
    fake({"/search": springs})
    with pytest.raises(ApiError) as e:
        run(geocode.resolve("Springfield"))
    assert e.value.code == "PLACE_AMBIGUOUS" and len(e.value.candidates) == 5


def test_not_found(fake):
    fake({"/search": []})
    with pytest.raises(ApiError) as e:
        run(geocode.resolve("Qwxzzy Nowhere"))
    assert e.value.code == "PLACE_NOT_FOUND"


def test_result_without_country_code_is_not_found(fake):
    fake({"/search": [_res("Somewhere", 1, 1, 0.5, {"state": "X"})]})
    with pytest.raises(ApiError) as e:
        run(geocode.resolve("Somewhere"))
    assert e.value.code == "PLACE_NOT_FOUND"


def test_ocean_search(fake):
    fake({"/search": [_res("Atlantic Ocean", 0, -30, 0.8, {}, typ="ocean")]})
    with pytest.raises(ApiError) as e:
        run(geocode.resolve("Atlantic Ocean"))
    assert e.value.code == "NO_LAND_DATA"


def test_ocean_picked_point(fake):
    # Nominatim's real answer for open ocean.
    fake({"/reverse": {"error": "Unable to geocode"}})
    with pytest.raises(ApiError) as e:
        run(geocode.resolve("mid atlantic", lat=30, lon=-40, display_name="Somewhere at sea"))
    assert e.value.code == "NO_LAND_DATA"


def test_named_sea_picked_point(fake):
    italy = {"country": "Italy", "country_code": "it"}
    fake({"/reverse": _res("Mediterranean Sea", 35, 18, 0.7, italy, typ="sea", cat="natural")})
    with pytest.raises(ApiError) as e:
        run(geocode.resolve("x", lat=35, lon=18, display_name="Med"))
    assert e.value.code == "NO_LAND_DATA"


def test_picked_place_reverse_only(fake):
    h = fake({"/reverse": _res("Paris", 33.66, -95.55, 0.5, US_TX)})
    p = run(geocode.resolve("Paris", lat=33.6609, lon=-95.5555, display_name="Paris, Texas"))
    assert p.display_name == "Paris, Texas"          # the user's choice is kept
    assert (p.lat, p.lon) == (33.6609, -95.5555)     # the user's exact point is kept
    assert p.admin == {"city": "Paris", "county": "Lamar County", "state": "Texas",
                       "country": "United States", "country_code": "us"}
    assert [c[1].url.path for c in h.calls] == ["/reverse"]  # no search


def test_cache_hit_makes_no_request(fake):
    h = fake({"/search": LAKE_MEAD})
    a = run(geocode.resolve("Lake Mead, Nevada"))
    b = run(geocode.resolve("  lake mead,   NEVADA "))  # same normalised query
    assert len(h.calls) == 1
    assert (a.lat, a.lon, a.display_name) == (b.lat, b.lon, b.display_name)


def test_retry_then_success(fake):
    h = fake({"/search": LAKE_MEAD}, fail_times=1)
    assert run(geocode.resolve("Lake Mead, Nevada")).display_name == "Lake Mead, Nevada"
    assert len(h.calls) == 2


def test_upstream_error_after_one_retry(fake):
    h = fake({"/search": LAKE_MEAD}, fail_times=5)
    with pytest.raises(ApiError) as e:
        run(geocode.resolve("Lake Mead, Nevada"))
    assert e.value.code == "UPSTREAM_ERROR" and len(h.calls) == 2


def test_rate_limit_spaces_requests(fake, monkeypatch):
    monkeypatch.setattr(geocode, "MIN_INTERVAL_S", 0.3)
    h = fake({"/search": LAKE_MEAD})

    async def three_at_once():
        # Different queries so the cache doesn't absorb them; launched concurrently.
        await asyncio.gather(*(geocode.resolve(f"Lake Mead {i}") for i in range(3)))

    run(three_at_once())
    times = [t for t, _ in h.calls]
    assert len(times) == 3
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= 0.29 for g in gaps), gaps
