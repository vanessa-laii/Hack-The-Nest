# Backend code style: written to be read

Harman and Vanessa must be able to explain any part of this code (REQUIREMENTS §6). Write for a reader who knows Python but not this project.

## File layout

1. **Header (3 lines):** what the file does, its inputs, its outputs (CLAUDE.md).
2. **"How it works" block:** right under the header, 3–8 short numbered steps in plain words, describing the flow through the file. Someone reading only this block should know what the file does.
3. Constants, then helpers, then the main public function(s), in the order the steps use them. Use `# --- Step name ---` dividers in long files.

## Comments

- Every function gets a docstring of **1–3 short lines**: what it does, then why, if the why isn't obvious. Plain sentences. No `What:` / `Why:` labels and no essays.
- Comment *why* inside a function only where a reader would ask "why is it done this way?". Don't narrate obvious code.
- Use everyday words first and jargon second: "clouds are removed (QA mask)", not "QA-bitmask invalidation".
- Requirement IDs (HL-5, TR-2…) only where the rule is the reason. One per comment is plenty.

## Code

- Lines ≤ 100 characters.
- One idea per line. Split long expressions and nested comprehensions into named intermediate variables (`clear_pixels = …`, `cloud_free = …`).
- Prefer a plain `for` loop over a comprehension with conditions and tuple unpacking.
- Name things after what they mean (`scenes_by_year`, `hole_pct`), not after their type (`d`, `arr2`). Single letters only for tiny maths (`x`, `y`, loop `i`).
- Keep functions short. If you need a comment to separate the parts of a function, those parts are probably separate functions.
- No cleverness for its own sake: no walrus, no `getattr` tricks, no chained ternaries.

## Example

```python
# Hard to read
kept = [(f, a) for f, a in zip(feats, arrays) if a is not None]

# Easier
kept = []
for scene, pixels in zip(scenes, downloads):
    if pixels is not None:  # a failed download comes back as None
        kept.append((scene, pixels))
```
