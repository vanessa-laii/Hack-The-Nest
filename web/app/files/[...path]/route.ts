/**
 * Local fallback for GET /files/... when the FastAPI backend is down.
 * Inputs: path under DATA_DIR (narration mp3 / json for smoke tests).
 * Outputs: raw file bytes. Prefer backend /files in production demos.
 */

import fs from "node:fs/promises";
import path from "node:path";
import { NextResponse } from "next/server";

import { getDataDir } from "@/lib/narration/paths";

const CONTENT_TYPES: Record<string, string> = {
  ".mp3": "audio/mpeg",
  ".json": "application/json",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
};

function safeJoinData(parts: string[]): string | null {
  if (parts.some((p) => p === ".." || p.includes("\0"))) return null;
  const root = path.resolve(getDataDir());
  const full = path.resolve(root, ...parts);
  if (!full.startsWith(root + path.sep) && full !== root) return null;
  return full;
}

export async function GET(
  _request: Request,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path: parts } = await context.params;
  if (!parts?.length) {
    return NextResponse.json(
      { error: { code: "INVALID_REQUEST", message: "Missing file path." } },
      { status: 400 },
    );
  }

  const full = safeJoinData(parts);
  if (!full) {
    return NextResponse.json(
      { error: { code: "INVALID_REQUEST", message: "Invalid file path." } },
      { status: 400 },
    );
  }

  try {
    const data = await fs.readFile(/*turbopackIgnore: true*/ full);
    const ext = path.extname(full).toLowerCase();
    const contentType = CONTENT_TYPES[ext] || "application/octet-stream";
    // Narration mp3s are rewritten in place under the same URL; long browser
    // cache made captions (fresh JSON) drift from audio after regenerate.
    const isNarrationAudio =
      ext === ".mp3" && parts.some((p) => p === "narration");
    return new NextResponse(data, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Cache-Control": isNarrationAudio
          ? "private, no-store"
          : "public, max-age=3600",
      },
    });
  } catch (err) {
    if ((err as NodeJS.ErrnoException)?.code === "ENOENT") {
      return NextResponse.json(
        { error: { code: "NOT_FOUND", message: "File not found." } },
        { status: 404 },
      );
    }
    return NextResponse.json(
      { error: { code: "INTERNAL", message: "Could not read file." } },
      { status: 500 },
    );
  }
}
