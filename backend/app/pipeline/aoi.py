# Builds the square analysis area (AOI) around a point, snapped to the Landsat 30 m UTM grid.
# Inputs: lat, lon, aoi_km (3–10, HL-6).
# Outputs: types.Aoi (UTM bbox + EPSG for the data API, lon/lat bbox for STAC and story.json).
#
# How it works:
#   1. Pick the UTM zone for the point and its EPSG code.
#   2. Convert the point's lat/lon to UTM metres.
#   3. Build a square of aoi_km around it, snapped to the Landsat pixel grid.
#   4. Build a slightly larger lon/lat box around it for the STAC search and story.json.
from __future__ import annotations

import math

from app.pipeline.types import Aoi

PIXEL_M = 30


def utm_zone(lat: float, lon: float) -> int:
    """Standard 6° UTM zone. The Norway/Svalbard exceptions are ignored: they only matter
    at high latitudes (out of scope), and a neighbouring zone is still valid."""
    return int((lon + 180) // 6) % 60 + 1


def utm_forward(lat: float, lon: float, zone: int) -> tuple[float, float]:
    """WGS84 lat/lon → UTM easting/northing in metres (sub-millimetre accuracy).
    Hand-written to avoid a pyproj dependency. The south adds the usual 10,000,000 m."""
    # Standard Krüger series for the transverse Mercator projection (unchanged maths).
    a, f = 6378137.0, 1 / 298.257223563
    k0, e2 = 0.9996, f * (2 - f)
    n = f / (2 - f)
    A = a / (1 + n) * (1 + n**2 / 4 + n**4 / 64)
    al = [n / 2 - 2 * n**2 / 3 + 5 * n**3 / 16, 13 * n**2 / 48 - 3 * n**3 / 5, 61 * n**3 / 240]
    lon0 = math.radians(-183 + 6 * zone)
    phi, lam = math.radians(lat), math.radians(lon) - lon0
    e = math.sqrt(e2)
    t = math.sinh(math.atanh(math.sin(phi)) - e * math.atanh(e * math.sin(phi)))
    xi = math.atan2(t, math.cos(lam))
    eta = math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
    x = eta + sum(al[j] * math.cos(2 * (j + 1) * xi) * math.sinh(2 * (j + 1) * eta)
                  for j in range(3))
    y = xi + sum(al[j] * math.sin(2 * (j + 1) * xi) * math.cosh(2 * (j + 1) * eta)
                 for j in range(3))
    northing = k0 * A * y
    return 500000 + k0 * A * x, northing + (10_000_000 if lat < 0 else 0)


def make_aoi(lat: float, lon: float, aoi_km: float) -> Aoi:
    """A square of round(aoi_km*1000/30) pixels centred on the point.
    Its edges sit on the Landsat grid (coordinate % 30 == 15, spike S1), so scenes in
    the native UTM zone are cropped, never resampled."""
    if not 3 <= aoi_km <= 10:
        raise ValueError("aoi_km must be 3–10 (HL-6)")
    zone = utm_zone(lat, lon)
    epsg = (32600 if lat >= 0 else 32700) + zone
    size = int(round(aoi_km * 1000 / PIXEL_M))
    side_m = size * PIXEL_M
    half = side_m / 2

    # Snap the lower-left corner down to the nearest pixel edge.
    easting, northing = utm_forward(lat, lon, zone)
    minx = math.floor((easting - half - 15) / PIXEL_M) * PIXEL_M + 15
    miny = math.floor((northing - half - 15) / PIXEL_M) * PIXEL_M + 15
    utm_bbox = (minx, miny, minx + side_m, miny + side_m)

    # Approximate lon/lat box: metres → degrees at this latitude, plus a 10% margin.
    # Away from the zone's centre line the UTM square is rotated a little against
    # lon/lat (up to ~2°), so its corners poke out; 10% covers that with room to spare.
    margin = 1.10
    dlat = half / 111_320 * margin
    metres_per_lon_degree = 111_320 * max(math.cos(math.radians(lat)), 0.01)
    dlon = half / metres_per_lon_degree * margin
    west, east = round(lon - dlon, 5), round(lon + dlon, 5)
    south, north = round(lat - dlat, 5), round(lat + dlat, 5)
    bbox_ll = (west, south, east, north)

    return Aoi(lat=lat, lon=lon, aoi_km=aoi_km, epsg=epsg, utm_bbox=utm_bbox,
               size_px=size, bbox_ll=bbox_ll)
