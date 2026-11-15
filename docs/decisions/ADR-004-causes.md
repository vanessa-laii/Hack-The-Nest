# ADR-004: Finding causes without hallucination (spike S4)

**Status:** accepted · Sun 2026-11-15 · Owner: Harman

## Question

Which method finds documented causes for detected changes with **zero unsupported claims** (REQUIREMENTS §4), finds ≥ 60% of the expected causes, and is cheapest?

## Method

- **Search results:** 34 Exa searches were recorded once for the gold changes (2 cause-neutral queries per change), costing $0.34 of the hard $15 project cap (see `app/agent/search.py`'s ledger). Every approach read only these 272 saved pages, with `EXA_CACHED_ONLY=1`.
- **Scoring:** one scorer (`spikes/s4-causes/score.py`) re-checks every claim in code, whatever the approach claims about itself:
  - TR-2: each quote appears word for word on the cited page
  - TR-3/4: the place is named and the years fit ±1
  - TR-7: the confidence follows the rule
  - the contract
- **Recall:** expected causes are found through the gold `cause_keywords`.
- **By eye:** each approach's claims were read next to their quotes (TR-5), because code can't judge that.
- **Gemini rate limits:** the first round ran all three approaches at once, and up to 96% of their requests hit 429 rate limits. `llm.py` now paces requests (2 in flight, 0.5 s apart), retries 429s with a longer allowance, and doesn't count them against the story's HL-10 budget. The scores below are the re-run, one approach at a time.

## Results

| Approach | Unsupported (code) | Overreach (by eye) | Causes found | Gemini calls / 6 places |
| --- | --- | --- | --- | --- |
| Single pass + quote check | 0 | 1 clear, 1 borderline | 5/7 | **20** |
| **Extract → verify → compose** | **0** | **0** | 5/7 | 41 |
| + critic (spike) | 0 | 0 | 4/7 | 49 |
| **Promoted + TR-5 checker + one rewrite** | **0** | 0 (1 dropped by checker) | 5/7 | 53 |

- **Single pass overreach:** "dropped to a **record low** due to arid winters". The quote gives a 51-foot drop, not a record. It also joined a fire's start date with a different place.
- **Extract-verify:**
  - it keeps tense ("**planned** to demolish 33 homes")
  - it found Summerlin ("the first family moved to Summerlin in 1991") and Willow Run's plant demolition
  - its weakness was true but empty "meta" claims ("images were acquired on …")
- **Critic:** it dropped some good claims along with nothing bad. It cost more and found less.

## Decision

**Extract → verify → compose**, promoted to `backend/app/agent/causes.py`:

1. **Extract:** the model returns exact quotes only, from pages wrapped as untrusted (TR-9), batched and trimmed around the place name and years.
2. **Verify, in code:**
   - word for word (TR-2)
   - the place is named (TR-3)
   - the years fit ±1 (TR-4)
   - de-duplicated
3. **Compose:** one call per change with only the verified quotes. It returns 0–3 claims, each citing its quote ids.
4. **Code re-check, with two rules added from this spike:**
   - **every number in a claim must appear in its own quotes**, and so must quantity and superlative words (half, twice/doubled, tripled, quarter, third, percent, record, lowest, highest, all-time). This stops figures being borrowed from other sentences or stretched ("a 51-foot drop" into "a record low").
   - **drop claims that only describe the imagery or the source** ("images were acquired", "this article", maps, pictures, cameras, MODIS, Sentinel-1/2, Thematic Mapper), not an event on the ground.
5. **Checker (TR-5):** a separate call per change with claims. It sees each claim with only its own quotes (wrapped as untrusted) and answers, per claim, yes/no with a reason: is every part (cause, number, name, date, tense) stated by those quotes, and is it about this place and these years? Any "no", or a missing verdict, drops the claim. If the checker call fails, that change's claims are dropped (`cause_unknown`), never kept unchecked. Tuned against the critic spike's false alarms: it judges only support and place/years, not relevance, and accepts faithful paraphrase.
6. **One rewrite round per change:** claims that failed step 4 or 5 go back to the model once, with the exact problems ("number not in its quotes", the checker's reason). The rewrites pass step 4 and the checker again; whatever still fails is dropped. Skipped when the HL-10 budget can't spare the 2 calls; a failed rewrite or re-check drops only the rewrites.
7. **Confidence by rule, per claim** (TR-7, `verify.confidence` over each claim's own sources). The chapter takes its best claim's level, and the script introduces each claim by its own level ("Records show: …" / "One report says: …", the claim verbatim). Pooling sources across claims is not allowed: two single-source claims stay `likely`.
8. **Fallback:** a change with no surviving claim stays `cause_unknown` (TR-8). That's a correct answer. Any error in one change, expected or not, leaves only that change unknown.

**Cost:** about 9 Gemini calls per story: extraction, compose and one checker call per change, plus a rewrite and re-check only when something failed (the 6 gold places took 53 calls, Willow Run 16 with 5 changes), within HL-10's 25. Live Exa use is unchanged: ≤ 2 searches per change, so ≤ 10 per story, within HL-10's 15, about $0.07–0.10 per new story.

**Re-score with the checker:** 0 unsupported, 5/7 found, 53 calls; the checker dropped 1 claim and the rewrite rescued none.

## Limits

- The checker is a model too: it can wrongly pass a subtle overreach, or drop a true claim for a wrong reason. The code rules (TR-2/3/4/6) still hold whatever it says, and the evidence panel shows every quote next to its claim.
- Recall depends on what two cause-neutral searches return and on extraction, which varies run to run. Small local changes often stay `cause_unknown`, which is honest.
- Gemini's per-minute quota is shared. Several simultaneous builds will be slower, but not less truthful.
