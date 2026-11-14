# ADR-001: Imagery access (spike S1)

**Status:** accepted · Sat 2026-11-14, 17:50 · Owner: Harman

## Question

What is the fastest reliable way to load 40 years of Landsat C2 L2 (7 bands including `qa_pixel`) for one 6 km area from Planetary Computer? Pass bar: ≤ 60 s cold. Test place: Lake Mead; details in `spikes/s1-imagery/README.md`.

## What we measured

**Round 1: all 546 summer scenes, three agents running at the same time.** All three failed:

| Approach | Wall time | Notes |
| --- | --- | --- |
| PC data API | 38–89 s (4 runs) | Native uint16, a single-pixel check matched the source exactly |
| stackstac | 69–111 s (5 runs) | Needs `fill_value=np.uint16(0)` with numpy 2 |
| odc-stac | 803 s (1 run) | About 3,800 remote reads; limited by the number of requests, not CPU |

Search took 23–27 s as one query across all years and 2–5 s as one parallel query per year.

**Round 2: the 4 clearest scenes per summer (170 scenes), loaders run one after another:**

| Approach | Runs (s) | Years present |
| --- | --- | --- |
| **PC data API** | 15.5 · 20.7 · 13.9 | 43 / 43 |
| stackstac | 26.3 · 29.4 · 32.0 | 43 / 43 |

odc-stac was not re-run: it was the slowest by an order of magnitude and works the same way as stackstac.

## Decision

1. **Use the Planetary Computer data API** (`/api/data/v1/item/bbox/...npy`, all 7 bands per request, nearest resampling, EPSG of the area's UTM zone). It is the fastest, and it needs one HTTP request per scene rather than ~7 file reads. That makes it more robust on venue Wi-Fi. It also needs no GDAL or rasterio for loading.
2. **Search STAC with one parallel query per year**, June–September, `eo:cloud_cover < 50`.
3. **Keep the N clearest scenes per year, N = 4 for now.** Spike S2 sets the final N, since it decides how the yearly image is built.
4. **stackstac is the fallback.** Its script stays in `spikes/s1-imagery/stackstac/`. If the data API is down or returns 429s on the day, we switch to it, which costs about 10 s more per story.

## Consequences and risks

- **TLS on this laptop.** Norton Antivirus intercepts HTTPS. The backend must call `truststore.inject_into_ssl()`, and GDAL (the fallback only) needs `GDAL_CURL_CA_BUNDLE` pointing at an export of the Windows root certificates. Certificate checks stay on.
- **Reprojection.** Some Lake Mead scenes are stored in UTM zone 12 (EPSG:32612). Both loaders resample them with nearest-neighbour into zone 11, so for those scenes the pixels are not on their original grid. That's fine at 30 m, but the README's limits section should say so (DR-8).
- **Server dependence.** No 429s or errors in 7 runs, but rate limits aren't documented. The backend needs retries with backoff and a disk cache of downloaded arrays in `data/cache/`. Cached stories (HL-9) don't depend on the API at all.
- **Scaling.** The returned arrays are raw values. The DR-1 scaling (`DN × 0.0000275 − 0.2`) is applied in our own code.
- Run-to-run variation was about 1.5× under load. The 180 s timeout (HL-5) leaves plenty of room.
