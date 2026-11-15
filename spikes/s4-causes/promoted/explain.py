# S4 check that the promoted agent scores like the spike: explain() calls app.agent.causes.
# Inputs: a PlaceInput (base story + recorded pages per change), a Trace and a Budget (HL-10).
# Outputs: {"sources": [...], "chapters": {change_id: {"confidence", "claims"}}} (story schema).
#
# How it works:
#   1. find_causes runs its own searches. The query template is the one record.py recorded
#      with, and score.py sets EXA_CACHED_ONLY=1, so every search is a cache hit: no spend.
#   2. The PlaceInput's pages are therefore not passed in; they are the same recorded pages.
from __future__ import annotations

from app.agent.causes import find_causes
from app.config import settings


async def explain(place_input, *, trace, budget) -> dict:
    """The promoted cause agent on one gold place."""
    story = place_input.story
    story_dir = settings.data_dir / "stories" / place_input.story_id
    return await find_causes(story_dir, story["place"], story["changes"],
                             trace=trace, budget=budget)
