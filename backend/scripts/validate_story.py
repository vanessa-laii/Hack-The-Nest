# CLI: validates story.json files against contracts/story.schema.json and the CONTRACTS §2 rules.
# Inputs: one or more paths to story.json (or *.jsonl event streams); default = the contract sample.
# Outputs: "OK"/errors per file on stdout; exit code 1 if any file is invalid.
#
# How it works:
#   1. Take the paths from the command line, or use the contract sample.
#   2. Check a .jsonl file line by line as build events; anything else as a story.
#   3. Print OK or every error per file; exit 1 if any file failed.
#
# Run it after any pipeline change (CLAUDE.md) and on cached stories before the demo.
from __future__ import annotations

import json
import sys
from pathlib import Path

# Lets this script import `app.*` from any working directory without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import CONTRACTS_DIR  # noqa: E402
from app.contract_check import event_errors, story_errors  # noqa: E402


def _event_stream_errors(path: Path) -> list[str]:
    """Checks each non-blank line of a .jsonl file as one build event."""
    errors = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        for error in event_errors(json.loads(line)):
            errors.append(f"line {line_no}: {error}")
    return errors


def main(argv: list[str]) -> int:
    """Checks each file and prints every error, not only the first.
    Returns non-zero if any file failed, so scripts and CI can stop."""
    paths = [Path(arg) for arg in argv]
    if not paths:
        paths = [CONTRACTS_DIR / "samples" / "story.sample.json"]

    bad = 0
    for path in paths:
        if path.suffix == ".jsonl":
            errors = _event_stream_errors(path)
        else:
            errors = story_errors(json.loads(path.read_text(encoding="utf-8")))

        if errors:
            bad += 1
            print(f"INVALID {path}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK {path}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
