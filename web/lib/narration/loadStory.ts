/**
 * Load story.json for narration: prefer DATA_DIR, else backend GET.
 * Inputs: story_id.
 * Outputs: story with script.segments (never rewrites text).
 */

import fs from "node:fs/promises";

import { storyJsonPath } from "./paths";

export type ScriptSegment = {
  id: string;
  chapter_id: string;
  year_from: number;
  year_to: number;
  claim_ids: string[];
  text: string;
};

export type StoryForNarration = {
  id: string;
  script: {
    segments: ScriptSegment[];
  };
};

export class StoryLoadError extends Error {
  status: number;
  code: string;

  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = "StoryLoadError";
    this.status = status;
    this.code = code;
  }
}

function isStoryForNarration(value: unknown): value is StoryForNarration {
  if (!value || typeof value !== "object") return false;
  const s = value as StoryForNarration;
  if (typeof s.id !== "string") return false;
  if (!s.script || !Array.isArray(s.script.segments) || s.script.segments.length === 0) {
    return false;
  }
  return s.script.segments.every(
    (seg) => seg && typeof seg.id === "string" && typeof seg.text === "string" && seg.text.length > 0,
  );
}

async function readLocalStory(storyId: string): Promise<StoryForNarration | null> {
  const localPath = storyJsonPath(storyId);
  try {
    const raw = await fs.readFile(localPath, "utf8");
    const parsed: unknown = JSON.parse(raw);
    if (!isStoryForNarration(parsed)) {
      throw new StoryLoadError("Local story.json missing script.segments", 500, "INTERNAL");
    }
    if (parsed.id !== storyId) {
      throw new StoryLoadError(`story.json id mismatch: expected ${storyId}`, 500, "INTERNAL");
    }
    return parsed;
  } catch (err) {
    if (err instanceof StoryLoadError) throw err;
    if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return null;
    throw err;
  }
}

async function fetchBackendStory(storyId: string): Promise<StoryForNarration> {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") || "http://localhost:8000";
  let res: Response;
  try {
    res = await fetch(`${apiBase}/api/stories/${encodeURIComponent(storyId)}`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
  } catch {
    throw new StoryLoadError("Could not reach story API", 502, "UPSTREAM_ERROR");
  }

  if (res.status === 404) {
    throw new StoryLoadError("Story not found", 404, "NOT_FOUND");
  }
  if (res.status === 425) {
    throw new StoryLoadError("Story not ready", 425, "NOT_READY");
  }
  if (!res.ok) {
    throw new StoryLoadError(`Backend story fetch failed (${res.status})`, 502, "UPSTREAM_ERROR");
  }

  const parsed: unknown = await res.json();
  if (!isStoryForNarration(parsed)) {
    throw new StoryLoadError("Backend story missing script.segments", 500, "INTERNAL");
  }
  return parsed;
}

export async function loadStoryForNarration(storyId: string): Promise<StoryForNarration> {
  const local = await readLocalStory(storyId);
  if (local) return local;
  return fetchBackendStory(storyId);
}
