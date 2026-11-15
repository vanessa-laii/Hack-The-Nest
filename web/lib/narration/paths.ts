/**
 * Resolve DATA_DIR and narration paths for a story.
 * Inputs: env DATA_DIR (relative to web/ or absolute).
 * Outputs: absolute paths under stories/<id>/ and narration/.
 */

import fs from "node:fs";
import path from "node:path";

export function getDataDir(): string {
  const raw = process.env.DATA_DIR?.trim() || "../data";
  if (path.isAbsolute(raw)) return raw;
  return path.resolve(/*turbopackIgnore: true*/ process.cwd(), raw);
}

export function storyDir(storyId: string): string {
  return path.join(getDataDir(), "stories", storyId);
}

export function storyJsonPath(storyId: string): string {
  return path.join(storyDir(storyId), "story.json");
}

export function narrationDir(storyId: string): string {
  return path.join(storyDir(storyId), "narration");
}

export function narrationJsonPath(storyId: string): string {
  return path.join(narrationDir(storyId), "narration.json");
}

export function segmentMp3Path(storyId: string, segmentId: string): string {
  return path.join(narrationDir(storyId), `${segmentId}.mp3`);
}

/** Root-relative URL served by backend /files (or local Next fallback). */
export function segmentAudioUrl(storyId: string, segmentId: string): string {
  return `/files/stories/${storyId}/narration/${segmentId}.mp3`;
}

export function ensureNarrationDir(storyId: string): void {
  fs.mkdirSync(narrationDir(storyId), { recursive: true });
}

export function removeNarrationArtifacts(storyId: string): void {
  const dir = narrationDir(storyId);
  if (!fs.existsSync(dir)) return;
  for (const name of fs.readdirSync(dir)) {
    fs.unlinkSync(path.join(dir, name));
  }
}
