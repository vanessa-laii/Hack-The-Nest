# CLI: validates story.json files against contracts/story.schema.json and the CONTRACTS §2 invariants.
# Inputs: one or more paths to story.json (or *.jsonl event streams); default = the contract sample.
# Outputs: "OK"/errors per file on stdout; exit code 1 if any file is invalid.
#
# Why a CLI as well as the in-code check: CLAUDE.md says to run this after any
# pipeline change, and it's how cached stories get checked before the demo.
from __future__ import annotations

import json
import sys
from pathlib import Path

# Lets `python backend/scripts/validate_story.py` import `app.*` from any
# working directory, without installing the backend as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import CONTRACTS_DIR  # noqa: E402
from app.contract_check import event_errors, story_errors  # noqa: E402


def main(argv: list[str]) -> int:
    """Checks each file and prints every error, not only the first.

    A .jsonl file is treated as a build-event stream and checked line by line
    against event.schema.json. Anything else is treated as a story.json.
    Returns a non-zero exit code if any file failed, so scripts and CI can stop.
    """
    paths = [Path(a) for a in argv] or [CONTRACTS_DIR / "samples" / "story.sample.json"]
    bad = 0
    for p in paths:
        if p.suffix == ".jsonl":
            errs = []
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip():
                    errs += [f"line {i}: {e}" for e in event_errors(json.loads(line))]
        else:
            errs = story_errors(json.loads(p.read_text(encoding="utf-8")))
        if errs:
            bad += 1
            print(f"INVALID {p}")
            for e in errs:
                print(f"  - {e}")
        else:
            print(f"OK {p}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
