# Offline tests for app.pipeline.imagery: STAC paging and ranking, scene loading, retries, caching.
# Inputs: fake HTTP responses via httpx.MockTransport (no network); a pytest tmp_path as DATA_DIR.
# Outputs: pass/fail. Run from backend/: python -m pytest tests/test_imagery.py
from __future__ import annotations

import asyncio
import io
import json

import httpx
import numpy as np
import pytest

from app.config import settings
from app.pipeline import imagery
from app.pipeline.aoi import make_aoi

AOI = make_aoi(36.25, -114.39, 3)   # 3 km keeps the fake arrays small (100 × 100)
SIZE = AOI.size_px


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Points DATA_DIR at a temp folder and removes backoff sleeps.
    Why: tests must never read or write the real data/ cache, and retry tests
    shouldn't wait seconds for exponential backoff."""
    monkeypatch.setattr(settings, "data_dir_raw", str(tmp_path))
    monkeypatch.setattr(imagery, "BACKOFF_BASE_S", 0.0)


def use_transport(monkeypatch, handler):
    """Makes imagery's http_client() return a client backed by `handler`, and
    returns the list of requests it saw. Why: proves exactly which calls happen."""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(imagery, "http_client",
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    return seen


def npy_bytes(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, arr)
    return buf.getvalue()


def feature(i: str, year: int, cc: float, platform: str = "landsat-5") -> dict:
    return {"id": i, "properties": {"datetime": f"{year}-07-01T18:00:00Z", "platform": platform,
                                    "eo:cloud_cover": cc, "proj:code": "EPSG:32611"}}


def scene(i: str = "LT05_TEST", year: int = 1990):
    return imagery._to_meta(feature(i, year, 5.0))


# ---------------------------------------------------------------- load_scenes

def test_extra_mask_band_is_dropped(monkeypatch):
    # 8 bands back (7 requested + the API's mask); band k is filled with value k+1.
    eight = np.stack([np.full((SIZE, SIZE), k + 1, np.uint16) for k in range(8)])
    seen = use_transport(monkeypatch, lambda r: httpx.Response(200, content=npy_bytes(eight)))
    out = asyncio.run(imagery.load_scenes(AOI, [scene()]))
    assert out.shape == (1, 7, SIZE, SIZE) and out.dtype == np.uint16
    assert [int(out[0, k, 0, 0]) for k in range(7)] == [1, 2, 3, 4, 5, 6, 7]
    # The request asks for the 7 BANDS assets, nearest resampling, in the AOI's UTM CRS.
    q = seen[0].url.params
    assert q.get_list("assets") == imagery.BANDS
    assert q["dst_crs"] == q["coord_crs"] == f"epsg:{AOI.epsg}"
    assert q["resampling"] == q["reproject"] == "nearest"


def test_retry_on_429_then_success(monkeypatch):
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, content=npy_bytes(np.ones((8, SIZE, SIZE), np.uint16)))

    use_transport(monkeypatch, handler)
    out = asyncio.run(imagery.load_scenes(AOI, [scene()]))
    assert calls["n"] == 2
    assert out.min() == 1


def test_permanent_failure_gives_zeros_and_no_cache(monkeypatch, caplog):
    seen = use_transport(monkeypatch, lambda r: httpx.Response(503))
    out = asyncio.run(imagery.load_scenes(AOI, [scene()]))
    assert len(seen) == imagery.MAX_TRIES
    assert out.shape == (1, 7, SIZE, SIZE) and not out.any()
    assert "gave up" in caplog.text
    assert not list(imagery._scene_cache_dir(AOI).glob("*.npy"))  # failures are not cached


def test_timeouts_are_retried_then_zeros(monkeypatch):
    def handler(r):
        raise httpx.ReadTimeout("slow", request=r)

    seen = use_transport(monkeypatch, handler)
    out = asyncio.run(imagery.load_scenes(AOI, [scene()]))
    assert len(seen) == imagery.MAX_TRIES and not out.any()


def test_cache_hit_makes_no_request(monkeypatch):
    arr = np.full((8, SIZE, SIZE), 7, np.uint16)
    seen = use_transport(monkeypatch, lambda r: httpx.Response(200, content=npy_bytes(arr)))
    s = scene()
    first = asyncio.run(imagery.load_scenes(AOI, [s]))
    assert len(seen) == 1
    assert (imagery._scene_cache_dir(AOI) / f"{s.id}.npy").exists()
    second = asyncio.run(imagery.load_scenes(AOI, [s]))
    assert len(seen) == 1                      # no second request
    assert np.array_equal(first, second)


def test_output_keeps_input_order(monkeypatch):
    # Each scene id maps to a distinct fill value; the stack must follow the input list.
    vals = {"A": 10, "B": 20, "C": 30}

    def handler(r):
        v = vals[r.url.params["item"]]
        return httpx.Response(200, content=npy_bytes(np.full((8, SIZE, SIZE), v, np.uint16)))

    use_transport(monkeypatch, handler)
    out = asyncio.run(imagery.load_scenes(AOI, [scene("C"), scene("A"), scene("B")]))
    assert [int(out[i, 0, 0, 0]) for i in range(3)] == [30, 10, 20]


# ---------------------------------------------------------------- search_scenes

def test_paging_is_followed(monkeypatch):
    """Page 1 links to page 2 with a POST token body; both pages' scenes come back."""
    def handler(r):
        body = json.loads(r.content)
        if body.get("token") == "p2":
            return httpx.Response(200, json={"features": [feature("p2a", 2000, 3)], "links": []})
        return httpx.Response(200, json={
            "features": [feature("p1a", 2000, 9)],
            "links": [{"rel": "next", "href": f"{imagery.STAC_URL}/search", "method": "POST",
                       "body": {"token": "p2"}, "merge": True}]})

    seen = use_transport(monkeypatch, handler)
    out = asyncio.run(imagery.search_scenes(AOI, years=range(2000, 2001)))
    assert [s.id for s in out[2000]] == ["p2a", "p1a"]
    assert len(seen) == 2
    # merge=True keeps the original query (bbox, datetime window) on page 2.
    p2 = json.loads(seen[1].content)
    assert p2["bbox"] == list(AOI.bbox_ll) and p2["datetime"].startswith("2000-06-01")


def test_per_year_sort_limit_and_platform_filter(monkeypatch):
    def handler(r):
        year = int(json.loads(r.content)["datetime"][:4])
        if year == 2012:
            # Landsat 7 must be kept (only sensor in 2012); a non-4–9 platform is dropped.
            feats = [feature("L7", 2012, 40, "landsat-7"), feature("MSS", 2012, 1, "landsat-3")]
        elif year == 2013:
            feats = []                       # an empty year is left out
        else:
            feats = [feature(f"s{cc}", year, cc) for cc in (50, 10, 30, 0, 20, 60, 40, 5)]
        return httpx.Response(200, json={"features": feats, "links": []})

    use_transport(monkeypatch, handler)
    out = asyncio.run(imagery.search_scenes(AOI, years=range(2011, 2014), per_year=3))
    assert sorted(out) == [2011, 2012]
    assert [s.id for s in out[2011]] == ["s0", "s5", "s10"]
    assert [s.id for s in out[2012]] == ["L7"] and out[2012][0].sensor == "Landsat 7"
    assert out[2011][0].native_epsg == 32611


def test_stac_cache_makes_search_offline(monkeypatch):
    seen = use_transport(monkeypatch, lambda r: httpx.Response(
        200, json={"features": [feature("x", 1999, 12)], "links": []}))
    first = asyncio.run(imagery.search_scenes(AOI, years=range(1999, 2000)))
    assert len(seen) == 1
    second = asyncio.run(imagery.search_scenes(AOI, years=range(1999, 2000)))
    assert len(seen) == 1 and first == second


def test_failed_search_year_is_omitted_not_cached(monkeypatch):
    seen = use_transport(monkeypatch, lambda r: httpx.Response(500))
    out = asyncio.run(imagery.search_scenes(AOI, years=range(1999, 2000)))
    assert out == {} and len(seen) == imagery.MAX_TRIES
    assert not list((settings.data_dir / "cache" / "stac").glob("*.json"))


def test_ranking_prefers_tier1_and_stripe_free_scenes():
    """Tier 2 and striped Landsat 7 scenes sort after clean ones, but are still used if needed."""
    from app.pipeline.imagery import _clearest
    from app.pipeline.types import SceneMeta

    def scene(scene_id, platform, cloud):
        return SceneMeta(id=scene_id, datetime="2023-07-01T00:00:00Z", year=2023,
                         platform=platform, cloud_cover=cloud, native_epsg=32617)

    striped = scene("LE07_x_02_T1", "landsat-7", 1.0)     # clearest, but stripe gaps
    tier2 = scene("LC08_y_02_T2", "landsat-8", 2.0)       # poor quality tier
    good = scene("LC09_z_02_T1", "landsat-9", 20.0)       # cloudier, but clean
    ranked = _clearest([striped, tier2, good], 2023, per_year=3)
    assert [s.id for s in ranked] == ["LC09_z_02_T1", "LE07_x_02_T1", "LC08_y_02_T2"]
