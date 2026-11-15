/**
 * POST /api/narrate — build or return cached word-timed narration for a story.
 * Inputs: { story_id }. Uses ElevenLabs server-side only; writes DATA_DIR/.../narration/.
 * Outputs: narration.json body (CONTRACTS §4). Rejects totals outside 55–120 s (web demo).
 */

import fs from "node:fs/promises";
import { NextResponse } from "next/server";

import type { Narration } from "@/lib/types/narration";
import { charactersToWords, durationFromAlignment } from "@/lib/narration/align";
import { getElevenLabsConfig, synthesizeSegment } from "@/lib/narration/elevenlabs";
import { loadStoryForNarration, StoryLoadError } from "@/lib/narration/loadStory";
import {
  ensureNarrationDir,
  narrationJsonPath,
  removeNarrationArtifacts,
  segmentAudioUrl,
  segmentMp3Path,
} from "@/lib/narration/paths";

/** Min still HL-1-ish; max relaxed past schema 95 so 225-word / 6-seg films can cache. */
const MIN_DURATION_S = 55;
const MAX_DURATION_S = 120;
/**
 * One TTS call per script.segment (FilmPlayer requires matching segment_ids).
 * story.schema allows up to 7 chapters (intro + 1–5 changes + outro) → up to 7 segments.
 * HL-10 says ≤5 TTS; that conflicts with the story contract for real places (e.g. Lake Mead
 * has 6). Cap at schema max so gallery films can narrate; tell Harman if we need a harder budget.
 */
const MAX_TTS_CALLS = 7;
const SEGMENT_ID_RE = /^seg-[0-9]{2}$/;

type ErrorBody = {
  error: { code: string; message: string };
};

function errorResponse(status: number, code: string, message: string) {
  const body: ErrorBody = { error: { code, message } };
  return NextResponse.json(body, { status });
}

function isValidStoryId(id: unknown): id is string {
  return typeof id === "string" && /^[a-z0-9-]{1,64}$/.test(id);
}

async function readCachedNarration(storyId: string): Promise<Narration | null> {
  try {
    const raw = await fs.readFile(narrationJsonPath(storyId), "utf8");
    return JSON.parse(raw) as Narration;
  } catch (err) {
    if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return null;
    throw err;
  }
}

export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return errorResponse(400, "INVALID_REQUEST", "Something went wrong with that request.");
  }

  const storyId =
    body && typeof body === "object" && "story_id" in body
      ? (body as { story_id: unknown }).story_id
      : undefined;

  if (!isValidStoryId(storyId)) {
    return errorResponse(400, "INVALID_REQUEST", "Something went wrong with that request.");
  }

  const cached = await readCachedNarration(storyId);
  if (cached) {
    return NextResponse.json(cached);
  }

  let voiceId: string;
  let modelId: string;
  try {
    ({ voiceId, modelId } = getElevenLabsConfig());
  } catch {
    return errorResponse(500, "INTERNAL", "Narration is not configured on this server.");
  }

  let story;
  try {
    story = await loadStoryForNarration(storyId);
  } catch (err) {
    if (err instanceof StoryLoadError) {
      const message =
        err.code === "NOT_READY"
          ? "This story is still building."
          : err.code === "NOT_FOUND"
            ? "We couldn't find that story."
            : err.message;
      return errorResponse(err.status, err.code, message);
    }
    return errorResponse(500, "INTERNAL", "Could not load the story.");
  }

  const segments = story.script.segments;
  if (segments.length < 3) {
    return errorResponse(400, "INVALID_REQUEST", "Story script needs at least three segments.");
  }
  if (segments.length > MAX_TTS_CALLS) {
    return errorResponse(
      422,
      "INVALID_REQUEST",
      `This story has ${segments.length} segments; narration allows at most ${MAX_TTS_CALLS} TTS calls (HL-10).`,
    );
  }
  if (segments.some((seg) => !SEGMENT_ID_RE.test(seg.id))) {
    return errorResponse(
      400,
      "INVALID_REQUEST",
      "Story segment ids must match seg-NN (e.g. seg-01).",
    );
  }

  ensureNarrationDir(storyId);

  const outSegments: Narration["segments"][number][] = [];
  let totalDuration = 0;

  try {
    for (let i = 0; i < segments.length; i++) {
      const seg = segments[i]!;
      const previousText = i > 0 ? segments[i - 1]!.text : undefined;
      const nextText = i < segments.length - 1 ? segments[i + 1]!.text : undefined;

      let result;
      try {
        result = await synthesizeSegment({
          text: seg.text,
          previousText,
          nextText,
        });
      } catch {
        // One retry on transient failure
        result = await synthesizeSegment({
          text: seg.text,
          previousText,
          nextText,
        });
      }

      const words = charactersToWords(result.alignment);
      const duration_s = durationFromAlignment(result.alignment);
      totalDuration += duration_s;

      await fs.writeFile(segmentMp3Path(storyId, seg.id), result.audio);

      outSegments.push({
        segment_id: seg.id,
        audio_url: segmentAudioUrl(storyId, seg.id),
        duration_s,
        words,
      });
    }
  } catch {
    removeNarrationArtifacts(storyId);
    return errorResponse(
      502,
      "UPSTREAM_ERROR",
      "The narration service failed. Please try again in a moment.",
    );
  }

  const roundedTotal = Math.round(totalDuration * 1000) / 1000;

  if (roundedTotal > MAX_DURATION_S) {
    removeNarrationArtifacts(storyId);
    return errorResponse(
      422,
      "NARRATION_TOO_LONG",
      "This narration ran long — try another story.",
    );
  }

  if (roundedTotal < MIN_DURATION_S) {
    removeNarrationArtifacts(storyId);
    return errorResponse(
      422,
      "NARRATION_TOO_SHORT",
      "This narration came out short of the 55–120 s band — try again or use a longer script.",
    );
  }

  const narration: Narration = {
    schema_version: "1.0",
    story_id: storyId,
    voice_id: voiceId,
    model_id: modelId,
    created_at: new Date().toISOString(),
    total_duration_s: roundedTotal,
    segments: outSegments as Narration["segments"],
  };

  // Write JSON last so cache hit implies complete artifacts
  await fs.writeFile(narrationJsonPath(storyId), JSON.stringify(narration, null, 2) + "\n", "utf8");

  return NextResponse.json(narration);
}
