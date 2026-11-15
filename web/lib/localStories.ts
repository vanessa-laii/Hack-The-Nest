/**
 * Read cached stories from DATA_DIR (play-only / Vercel — no FastAPI needed).
 * Inputs: story id or gallery index on disk. Outputs: Story / GalleryItem[].
 */

import fs from "node:fs/promises";
import path from "node:path";

import type { GalleryItem } from "./api";
import { getDataDir, storyJsonPath } from "./narration/paths";
import type { Story } from "./types/story";

function galleryIndexPath(): string {
  return path.join(getDataDir(), "stories", "index.json");
}

export async function readGalleryFromDisk(): Promise<GalleryItem[]> {
  try {
    const raw = await fs.readFile(
      /*turbopackIgnore: true*/ galleryIndexPath(),
      "utf8",
    );
    const data = JSON.parse(raw) as { stories?: GalleryItem[] };
    return data.stories?.length ? data.stories : [];
  } catch {
    return [];
  }
}

export async function readStoryFromDisk(id: string): Promise<Story | null> {
  try {
    const raw = await fs.readFile(
      /*turbopackIgnore: true*/ storyJsonPath(id),
      "utf8",
    );
    const story = JSON.parse(raw) as Story;
    if (!story || story.id !== id) return null;
    return story;
  } catch {
    return null;
  }
}

export async function storyExistsOnDisk(id: string): Promise<boolean> {
  try {
    await fs.access(/*turbopackIgnore: true*/ storyJsonPath(id));
    return true;
  } catch {
    return false;
  }
}
