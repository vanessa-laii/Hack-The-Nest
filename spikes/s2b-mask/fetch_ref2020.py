# Spike S2b input: fetches the exact 4 Willow Run 2020 scenes used by the gold-set reference sheet (failure #3).
# Inputs: the scene ids below (from the reference sheet's willow-run-plant.json); network. Run with the S1 pc-data-api venv.
# Outputs: data/cache/s2b/willow-run-2020ref.npz (scenes uint16 [4,7,200,200]) + .json (ids, dates), same grid as willow-run.
#
# Why: our 6-clearest download for Willow Run picked Landsat 7 scenes in 2020, so the
# unflagged white blobs seen in the reference composite (built from two Landsat 8 scenes
# we didn't download, 08-01 T2 and 09-18) didn't reproduce. These are those exact scenes.
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_s2b  # noqa: E402  (sets fetch.SIZE = 200 and the s2b cache dir)

F = fetch_s2b.fetch
IDS = ["LC08_L2SP_020031_20200614_02_T1", "LC08_L2SP_020031_20200630_02_T1",
       "LC08_L2SP_020031_20200801_02_T2", "LC08_L2SP_020031_20200918_02_T1"]


async def main() -> None:
    """Download the 4 scenes on the willow-run grid and save them with minimal metadata."""
    p = fetch_s2b.PLACES["willow-run"]
    ubox = F.snapped_utm_bbox(p["lat"], p["lon"], p["zone"])
    errors: list[str] = []
    sem = asyncio.Semaphore(4)
    async with F.httpx.AsyncClient(timeout=90) as client:
        arrs = await asyncio.gather(*(F.fetch_scene(client, sem, i, ubox, p["zone"], errors) for i in IDS))
    if errors:
        raise SystemExit(errors)
    F.OUT_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(F.OUT_DIR / "willow-run-2020ref.npz", scenes=np.stack(arrs).astype(np.uint16))
    meta = {"utm_bbox": ubox, "scenes": [{"id": i, "datetime": f"{i[17:21]}-{i[21:23]}-{i[23:25]}T16:00:00Z",
                                          "platform": "landsat-8", "cloud_cover": 0.0} for i in IDS]}
    (F.OUT_DIR / "willow-run-2020ref.json").write_text(json.dumps(meta, indent=2))
    print("saved", len(arrs), "scenes")


if __name__ == "__main__":
    asyncio.run(main())
