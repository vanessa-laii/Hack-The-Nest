# S4 baseline: claims nothing, so every change stays cause_unknown (checks the scorer's plumbing).
# Inputs: a PlaceInput (unused).
# Outputs: an empty answer: no sources, no claims.


async def explain(place, *, trace, budget) -> dict:
    """Returns no claims. TR-8: 'cause unknown' is always a truthful answer."""
    return {"sources": [], "chapters": {}}
