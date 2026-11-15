/**
 * Phase 0 narrate / play-audio stub for the story proof page.
 * Inputs: story id. Calls POST /api/narrate (Next route) when available.
 * Outputs: status + optional segment audio links; not a FilmPlayer.
 */

"use client";

import { useState } from "react";
import type { Narration } from "@/lib/types/narration";

type NarrateStubProps = {
  storyId: string;
};

export function NarrateStub({ storyId }: NarrateStubProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [narration, setNarration] = useState<Narration | null>(null);

  async function handleNarrate() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/narrate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ story_id: storyId }),
      });
      const data = await res.json();
      if (!res.ok) {
        const msg =
          data?.error?.message ??
          "Narration failed. Check ELEVENLABS_API_KEY or try again.";
        setError(msg);
        return;
      }
      setNarration(data as Narration);
    } catch {
      setError("Could not reach the narrate route.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={handleNarrate}
          disabled={busy}
          className="inline-flex min-h-11 items-center justify-center rounded-full bg-[#5BA8A0] px-5 text-sm font-medium text-[#0A0E13] transition hover:bg-[#6FB8B0] disabled:cursor-wait disabled:opacity-60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
        >
          {busy
            ? "Narrating…"
            : narration
              ? "Refresh narration"
              : "Narrate / play audio"}
        </button>
        <span className="em-chrome-label text-[#7A766E]">
          Stub · POST /api/narrate
        </span>
      </div>

      {error ? (
        <p role="alert" className="mt-3 text-sm text-[#C4A574]">
          {error}
        </p>
      ) : null}

      {narration ? (
        <div className="mt-4 space-y-3">
          <p className="text-sm text-[#A8A49C]">
            Cached narration · {narration.total_duration_s.toFixed(1)} s ·{" "}
            {narration.segments.length} segments
          </p>
          <ul className="space-y-2">
            {narration.segments.map((seg) => (
              <li key={seg.segment_id} className="flex flex-col gap-1">
                <span className="em-chrome-label text-[#7A766E]">
                  {seg.segment_id}
                </span>
                {/* eslint-disable-next-line jsx-a11y/media-has-caption -- Phase 0 stub; captions land with FilmPlayer */}
                {/* Same-origin /files via Next route (DATA_DIR); not backend frames */}
                <audio
                  controls
                  preload="none"
                  src={seg.audio_url}
                  className="w-full max-w-md"
                >
                  Your browser does not support audio.
                </audio>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
