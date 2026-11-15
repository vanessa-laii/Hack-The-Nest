# Spike S2b input: downloads the two Detroit-area failure places (Willow Run, Delray) at 6 km via the PC data API.
# Inputs: PLACES below; network. Reuses spikes/s2-composite/fetch.py (imported, not edited). Run with the S1 pc-data-api venv.
# Outputs: data/cache/s2b/<place>.npz (scenes uint16 [n,7,200,200]) and <place>.json (scene metadata), same format as S2.
#
# Why reuse S2's fetch: the "before" composites must use exactly the S2 download
# rules (up to 6 clearest scenes per summer, cloud < 70) so before/after is fair.
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "spikes" / "s2-composite"))

import fetch  # noqa: E402  (S2's fetch module; importing it also injects truststore)

# Override the S2 module's settings instead of copying its code: 200 px × 30 m = 6 km,
# the ADR-002 default area, written to a separate cache so S2's data is untouched.
fetch.SIZE = 200
fetch.OUT_DIR = REPO / "data" / "cache" / "s2b"

# Willow Run: the plant/airport point from the gold set (failure #3, unflagged cloud in 2020).
# Delray: SW Detroit; its 6 km box also contains the Gordie Howe bridge at
# (42.289, -83.112), so it covers failure #1 (bridge deck hole) and #2 (2021 haze).
PLACES = {
    "willow-run": {"lat": 42.2424, "lon": -83.5552, "zone": 17},
    "delray": {"lat": 42.30, "lon": -83.10, "zone": 17},
}


def main() -> None:
    """Fetch each requested place in turn (default: both)."""
    for name in sys.argv[1:] or list(PLACES):
        asyncio.run(fetch.fetch_place(name, PLACES[name]))


if __name__ == "__main__":
    main()
