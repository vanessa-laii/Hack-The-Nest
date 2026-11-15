# CLI: zips the built stories' player files so the demo laptop can play them offline (HL-9).
# Inputs: data/stories/ (story.json, frames, thumbs, masks, index.json).
# Outputs: data/earth-memory-stories.zip, ready to unzip into the other machine's data/stories/.
#
# How it works:
#   1. Take every story folder that has a story.json (finished stories only).
#   2. Add only what the web player reads: story.json, frames/, thumbs/, masks/, narration/.
#   3. Add the gallery index, filtered to the stories in the zip.
#
# Why only player files: composites.npz (~15 MB each), imagery.json and trace.jsonl are
# backend working files. The player never reads them, and they would triple the zip.
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

PLAYER_FOLDERS = ("frames", "thumbs", "masks", "narration")


def main() -> int:
    stories_dir = settings.data_dir / "stories"
    out = settings.data_dir / "earth-memory-stories.zip"
    finished = sorted(d for d in stories_dir.iterdir() if (d / "story.json").exists())
    ids = {d.name for d in finished}

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for story_dir in finished:
            archive.write(story_dir / "story.json", f"{story_dir.name}/story.json")
            for folder in PLAYER_FOLDERS:
                for path in sorted((story_dir / folder).glob("*")) if (story_dir / folder).exists() else []:
                    archive.write(path, f"{story_dir.name}/{folder}/{path.name}")
        index_path = stories_dir / "index.json"
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["stories"] = [s for s in index.get("stories", []) if s["id"] in ids]
            archive.writestr("index.json", json.dumps(index, indent=2))

    size_mb = out.stat().st_size / 1_000_000
    print(f"{out}  ({size_mb:.1f} MB, {len(finished)} stories)")
    for story_dir in finished:
        print(f"  {story_dir.name}")
    print("On the demo laptop: unzip into <repo>/data/stories/ (keep the folder structure).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
