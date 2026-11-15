/**
 * Client bridge: load narration (cache-first) then mount FilmPlayer.
 * Inputs: story + optional playback callbacks (year/segment/play/ended).
 * Outputs: preparing / error / FilmPlayer. Never rewrites story text.
 */

"use client";

import { useEffect, useState } from "react";
import { FilmPlayer } from "@/components/FilmPlayer";
import { isNarrationTooLong } from "@/lib/playback";
import type { Narration } from "@/lib/types/narration";
import type { Story } from "@/lib/types/story";

type StoryFilmProps = {
  story: Story;
  /** Forwarded to FilmPlayer so MetricChart can track the playing year. */
  onYearChange?: (year: number) => void;
  onSegmentChange?: (segmentId: string, index: number) => void;
  onPlayStateChange?: (playing: boolean) => void;
  onEnded?: () => void;
};

type NarrateState =
  | { status: "loading" }
  | { status: "ready"; narration: Narration }
  | { status: "error"; message: string };

function narrateErrorMessage(data: unknown): string {
  const err =
    data && typeof data === "object" && "error" in data
      ? (data as { error?: { code?: string; message?: string } }).error
      : undefined;
  if (
    err?.code === "NARRATION_TOO_LONG" ||
    (typeof err?.message === "string" && /too long|95|120/i.test(err.message))
  ) {
    return "This narration ran long — try again, or ask for a shorter script.";
  }
  return err?.message || "Could not prepare narration. Check the narrate route and try again.";
}

export function StoryFilm({
  story,
  onYearChange,
  onSegmentChange,
  onPlayStateChange,
  onEnded,
}: StoryFilmProps) {
  const [state, setState] = useState<NarrateState>({ status: "loading" });
  const [retryToken, setRetryToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });

    (async () => {
      try {
        const res = await fetch("/api/narrate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ story_id: story.id }),
        });
        const data = await res.json();
        if (cancelled) return;

        if (!res.ok) {
          setState({ status: "error", message: narrateErrorMessage(data) });
          return;
        }

        const narration = data as Narration;
        if (isNarrationTooLong(narration.total_duration_s)) {
          setState({
            status: "error",
            message: "This narration ran long — try another story.",
          });
          return;
        }

        setState({ status: "ready", narration });
      } catch {
        if (!cancelled) {
          setState({
            status: "error",
            message: "Could not reach the narrate route.",
          });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [story.id, retryToken]);

  if (state.status === "loading") {
    return (
      <div
        className="rounded-md border border-white/10 bg-[#14181E] px-5 py-12 text-center"
        role="status"
        aria-live="polite"
      >
        <p className="font-ui text-base text-[#F4F1EC]">Preparing narration…</p>
        <p className="em-chrome-label mt-2 text-[#7A766E]">
          Cache-first · POST /api/narrate
        </p>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div
        className="rounded-md border border-[#C4A574]/35 bg-[#C4A574]/10 px-5 py-6"
        role="alert"
      >
        <p className="font-ui text-base font-semibold text-[#F4F1EC]">
          Narration unavailable
        </p>
        <p className="mt-2 text-sm text-[#A8A49C]">{state.message}</p>
        <button
          type="button"
          className="mt-4 inline-flex min-h-11 items-center rounded-full border border-white/15 px-5 text-sm text-[#F4F1EC] transition hover:border-[#5BA8A0]/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
          onClick={() => setRetryToken((n) => n + 1)}
        >
          Try again
        </button>
      </div>
    );
  }

  return (
    <FilmPlayer
      key={`${story.id}-${state.narration.created_at}`}
      story={story}
      narration={state.narration}
      autoPlay
      captionsDefaultOn
      onYearChange={onYearChange}
      onSegmentChange={onSegmentChange}
      onPlayStateChange={onPlayStateChange}
      onEnded={onEnded}
    />
  );
}
