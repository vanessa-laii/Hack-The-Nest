/**
 * Main story film stage: CSS crossfade + narration audio clock (FR-52, S6).
 * Inputs: story + narration; optional callbacks for year/segment/play.
 * Outputs: playable film with captions, scrubber, year label, keyboard.
 */

"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import clsx from "clsx";
import { Captions } from "@/components/Captions";
import { fileUrl } from "@/lib/api";
import {
  buildPlaybackSegments,
  frameYearsFromStory,
  fractionToGlobal,
  globalTimeForYear,
  globalToLocal,
  isNarrationTooLong,
  localToGlobal,
  pickFrameYear,
  stepYear,
  totalDuration,
} from "@/lib/playback";
import type { Narration } from "@/lib/types/narration";
import type { Story } from "@/lib/types/story";

/** Stable props API — FE polish can restyle; keep these contracts. */
export type FilmPlayerProps = {
  story: Story;
  narration: Narration;
  /** Attempt autoplay on mount; fall back to prominent Play. Default true. */
  autoPlay?: boolean;
  /** Captions visible by default (FR-45). Default true. */
  captionsDefaultOn?: boolean;
  className?: string;
  onYearChange?: (year: number) => void;
  onSegmentChange?: (segmentId: string, index: number) => void;
  onPlayStateChange?: (playing: boolean) => void;
  onEnded?: () => void;
};

function formatClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${r.toString().padStart(2, "0")}`;
}

/** Same-origin /files for narration (Next DATA_DIR route). */
function audioSrc(audioUrl: string): string {
  if (!audioUrl) return "";
  if (audioUrl.startsWith("http://") || audioUrl.startsWith("https://")) {
    return audioUrl;
  }
  return audioUrl.startsWith("/") ? audioUrl : `/${audioUrl}`;
}

export function FilmPlayer({
  story,
  narration,
  autoPlay = true,
  captionsDefaultOn = true,
  className,
  onYearChange,
  onSegmentChange,
  onPlayStateChange,
  onEnded,
}: FilmPlayerProps) {
  const tooLong = isNarrationTooLong(narration.total_duration_s);

  const segments = useMemo(
    () => buildPlaybackSegments(story, narration),
    [story, narration],
  );
  const availableYears = useMemo(() => frameYearsFromStory(story), [story]);
  const frameByYear = useMemo(() => {
    const map = new Map<number, (typeof story.frames)[number]>();
    for (const f of story.frames) map.set(f.year, f);
    return map;
  }, [story.frames]);
  const narrSegById = useMemo(
    () => new Map(narration.segments.map((s) => [s.segment_id, s])),
    [narration.segments],
  );

  const totalS = useMemo(() => totalDuration(segments), [segments]);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const prefetchRef = useRef<HTMLAudioElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const playingRef = useRef(false);
  const segmentIndexRef = useRef(0);
  const globalTRef = useRef(0);
  const yearRef = useRef(availableYears[0] ?? story.coverage.first_year);

  const [segmentIndex, setSegmentIndex] = useState(0);
  const [localT, setLocalT] = useState(0);
  const [globalT, setGlobalT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [needsGesture, setNeedsGesture] = useState(false);
  const [captionsOn, setCaptionsOn] = useState(captionsDefaultOn);
  const [ended, setEnded] = useState(false);
  const [displayYear, setDisplayYear] = useState(() => {
    const first = segments[0];
    if (!first) return story.coverage.first_year;
    return pickFrameYear(first, 0, availableYears);
  });
  const [fadeYears, setFadeYears] = useState<number[]>(() => {
    const first = segments[0];
    const y = first
      ? pickFrameYear(first, 0, availableYears)
      : story.coverage.first_year;
    return [y];
  });

  yearRef.current = displayYear;

  const activeSeg = segments[segmentIndex];
  const activeNarr = activeSeg
    ? narrSegById.get(activeSeg.segment_id)
    : undefined;
  const fallbackText =
    story.script.segments.find((s) => s.id === activeSeg?.segment_id)?.text ??
    "";

  const stopRaf = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  }, []);

  const setPlayState = useCallback(
    (next: boolean) => {
      playingRef.current = next;
      setPlaying(next);
      onPlayStateChange?.(next);
    },
    [onPlayStateChange],
  );

  const syncFromClock = useCallback(
    (nextGlobal: number, nextSegIndex: number, nextLocal: number) => {
      globalTRef.current = nextGlobal;
      segmentIndexRef.current = nextSegIndex;
      setGlobalT(nextGlobal);
      setLocalT(nextLocal);
      setSegmentIndex((prev) => {
        if (prev !== nextSegIndex) {
          const seg = segments[nextSegIndex];
          if (seg) onSegmentChange?.(seg.segment_id, nextSegIndex);
        }
        return nextSegIndex;
      });
      const seg = segments[nextSegIndex];
      if (!seg) return;
      const year = pickFrameYear(seg, nextLocal, availableYears);
      if (year !== yearRef.current) {
        yearRef.current = year;
        setFadeYears((stack) =>
          [...stack.filter((y) => y !== year), year].slice(-2),
        );
        setDisplayYear(year);
        onYearChange?.(year);
      }
    },
    [segments, availableYears, onSegmentChange, onYearChange],
  );

  const prefetchNext = useCallback(
    (fromIndex: number) => {
      // Resolve by playback segment_id (story.script order), not narration array index.
      const nextPlay = segments[fromIndex + 1];
      const next = nextPlay
        ? narrSegById.get(nextPlay.segment_id)
        : undefined;
      if (!next) return;
      const url = audioSrc(next.audio_url);
      if (!prefetchRef.current) {
        prefetchRef.current = new Audio();
        prefetchRef.current.preload = "auto";
      }
      try {
        const absolute = new URL(url, window.location.href).href;
        if (prefetchRef.current.src !== absolute) {
          prefetchRef.current.src = url;
          prefetchRef.current.load();
        }
      } catch {
        prefetchRef.current.src = url;
        prefetchRef.current.load();
      }
    },
    [segments, narrSegById],
  );

  const loadSegmentAudio = useCallback(
    async (index: number, seekLocal = 0, shouldPlay = false) => {
      const audio = audioRef.current;
      const playSeg = segments[index];
      const narrSeg = playSeg
        ? narrSegById.get(playSeg.segment_id)
        : undefined;
      if (!audio || !narrSeg) return false;
      const url = audioSrc(narrSeg.audio_url);
      try {
        const absolute = new URL(url, window.location.href).href;
        if (audio.src !== absolute) {
          audio.src = url;
          audio.load();
        }
      } catch {
        audio.src = url;
        audio.load();
      }

      const applySeek = () => {
        try {
          audio.currentTime = Math.min(
            Math.max(0, seekLocal),
            Math.max(0, narrSeg.duration_s - 0.05),
          );
        } catch {
          /* ignore seek before metadata */
        }
      };

      if (audio.readyState >= 1) applySeek();
      else {
        await new Promise<void>((resolve) => {
          const onMeta = () => {
            audio.removeEventListener("loadedmetadata", onMeta);
            applySeek();
            resolve();
          };
          audio.addEventListener("loadedmetadata", onMeta);
          // Safety if metadata already fired
          if (audio.readyState >= 1) {
            audio.removeEventListener("loadedmetadata", onMeta);
            applySeek();
            resolve();
          }
        });
      }

      prefetchNext(index);

      if (shouldPlay) {
        try {
          await audio.play();
          setNeedsGesture(false);
          setPlayState(true);
          return true;
        } catch {
          setNeedsGesture(true);
          setPlayState(false);
          return false;
        }
      }
      return true;
    },
    [segments, narrSegById, prefetchNext, setPlayState],
  );

  const tick = useCallback(() => {
    const audio = audioRef.current;
    if (!audio || !playingRef.current) return;
    const idx = segmentIndexRef.current;
    const seg = segments[idx];
    if (!seg) return;
    const local = audio.currentTime;
    const g = localToGlobal(segments, idx, local);
    syncFromClock(g, idx, local);
    rafRef.current = requestAnimationFrame(tick);
  }, [segments, syncFromClock]);

  const startRaf = useCallback(() => {
    stopRaf();
    rafRef.current = requestAnimationFrame(tick);
  }, [stopRaf, tick]);

  const pause = useCallback(() => {
    audioRef.current?.pause();
    stopRaf();
    setPlayState(false);
  }, [setPlayState, stopRaf]);

  const playFromGlobal = useCallback(
    async (g: number) => {
      if (tooLong || segments.length === 0) return;
      const pos = globalToLocal(segments, g);
      setEnded(false);
      syncFromClock(pos.globalT, pos.segmentIndex, pos.localT);
      const ok = await loadSegmentAudio(pos.segmentIndex, pos.localT, true);
      if (ok) startRaf();
    },
    [tooLong, segments, syncFromClock, loadSegmentAudio, startRaf],
  );

  const togglePlay = useCallback(async () => {
    if (tooLong) return;
    if (playingRef.current) {
      pause();
      return;
    }
    if (ended || globalTRef.current >= totalS - 0.05) {
      await playFromGlobal(0);
      return;
    }
    await playFromGlobal(globalTRef.current);
  }, [tooLong, pause, ended, totalS, playFromGlobal]);

  const seekGlobal = useCallback(
    async (g: number, resume: boolean) => {
      if (tooLong || segments.length === 0) return;
      const wasPlaying = playingRef.current || resume;
      pause();
      const pos = globalToLocal(segments, Math.max(0, Math.min(g, totalS)));
      setEnded(pos.ended);
      syncFromClock(pos.globalT, pos.segmentIndex, pos.localT);
      await loadSegmentAudio(pos.segmentIndex, pos.localT, false);
      if (wasPlaying && !pos.ended) {
        await playFromGlobal(pos.globalT);
      }
    },
    [
      tooLong,
      segments,
      totalS,
      pause,
      syncFromClock,
      loadSegmentAudio,
      playFromGlobal,
    ],
  );

  const seekYear = useCallback(
    (dir: -1 | 1) => {
      const nextYear = stepYear(availableYears, yearRef.current, dir);
      const g = globalTimeForYear(segments, availableYears, nextYear);
      void seekGlobal(g, playingRef.current);
    },
    [availableYears, segments, seekGlobal],
  );

  const onAudioEnded = useCallback(async () => {
    const idx = segmentIndexRef.current;
    if (idx >= segments.length - 1) {
      stopRaf();
      setPlayState(false);
      setEnded(true);
      const last = segments[segments.length - 1];
      if (last) {
        syncFromClock(totalS, segments.length - 1, last.duration_s);
      }
      onEnded?.();
      return;
    }
    const next = idx + 1;
    const g = localToGlobal(segments, next, 0);
    syncFromClock(g, next, 0);
    const ok = await loadSegmentAudio(next, 0, true);
    if (ok) startRaf();
  }, [
    segments,
    stopRaf,
    setPlayState,
    totalS,
    syncFromClock,
    onEnded,
    loadSegmentAudio,
    startRaf,
  ]);

  // Initial load + autoplay
  useEffect(() => {
    if (tooLong || segments.length === 0) return;
    let cancelled = false;
    (async () => {
      syncFromClock(0, 0, 0);
      await loadSegmentAudio(0, 0, false);
      if (cancelled) return;
      if (!autoPlay) {
        setNeedsGesture(true);
        return;
      }
      const ok = await loadSegmentAudio(0, 0, true);
      if (cancelled) return;
      if (ok) startRaf();
      else setNeedsGesture(true);
    })();
    return () => {
      cancelled = true;
      stopRaf();
      audioRef.current?.pause();
    };
    // Remount via key={story.id} from parent when story changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [story.id, narration.created_at]);

  // Keyboard: Space play/pause, ←/→ year
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      if (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        target?.isContentEditable
      ) {
        return;
      }
      // Space on a focused <button> already activates it — avoid double toggle.
      if (e.code === "Space" || e.key === " ") {
        if (tag === "BUTTON" || target?.getAttribute("role") === "button") {
          return;
        }
        e.preventDefault();
        void togglePlay();
        return;
      }
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        seekYear(-1);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        seekYear(1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [togglePlay, seekYear]);

  if (tooLong) {
    return (
      <div
        className={clsx(
          "rounded-md border border-[#C4A574]/40 bg-[#C4A574]/10 px-5 py-6",
          className,
        )}
        role="alert"
      >
        <p className="font-ui text-lg font-semibold text-[#F4F1EC]">
          Narration too long to play
        </p>
        <p className="mt-2 text-sm text-[#A8A49C]">
          This film&apos;s audio is {narration.total_duration_s.toFixed(1)}s
          (limit 120s). Try another story from the gallery.
        </p>
      </div>
    );
  }

  if (!story.frames.length) {
    return (
      <p className="rounded-md border border-white/10 bg-[#14181E] px-5 py-8 text-center text-sm text-[#A8A49C]">
        No frames in this story yet.
      </p>
    );
  }

  const progress = totalS > 0 ? Math.min(1, globalT / totalS) : 0;

  return (
    <div
      className={clsx(
        /* Signature: thin film-rebate double edge around the player mount */
        "relative w-full overflow-hidden rounded-md border border-white/18 bg-[#14181E] shadow-[inset_0_0_0_1px_rgba(244,241,236,0.06)] outline outline-1 outline-offset-[-5px] outline-white/10",
        className,
      )}
      role="region"
      aria-label="Film player"
      data-film-player
      data-story-id={story.id}
      style={{ "--em-crossfade": "480ms" } as CSSProperties}
    >
      {/* eslint-disable-next-line jsx-a11y/media-has-caption -- Captions renders word timings */}
      <audio
        ref={audioRef}
        preload="auto"
        onEnded={() => {
          void onAudioEnded();
        }}
        className="hidden"
      />

      <div className="relative aspect-square w-full sm:aspect-[4/3] lg:aspect-[16/10]">
        {/* Crossfade stack (S6 — CSS opacity, not MP4) */}
        {fadeYears.map((year) => {
          const frame = frameByYear.get(year);
          if (!frame) return null;
          const isTop = year === displayYear;
          return (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={year}
              src={fileUrl(frame.image_url)}
              alt={isTop ? `Landsat ${year}` : ""}
              aria-hidden={!isTop}
              className={clsx(
                "pointer-events-none absolute inset-0 h-full w-full object-cover transition-opacity ease-out",
                "duration-[var(--em-crossfade)]",
                isTop ? "opacity-100" : "opacity-0",
              )}
            />
          );
        })}

        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 bottom-0 h-2/5 bg-gradient-to-t from-[#0A0E13]/92 via-[#0A0E13]/4 to-transparent"
        />

        <p
          className="pointer-events-none absolute left-5 top-5 z-[2] font-display text-5xl font-medium tracking-tight text-[#F4F1EC] drop-shadow-[0_2px_12px_rgba(0,0,0,0.65)] sm:left-6 sm:top-6 sm:text-6xl lg:text-7xl"
          aria-live="polite"
        >
          {displayYear}
        </p>

        {/* Click frame to play/pause (sibling overlays stopPropagation) */}
        <button
          type="button"
          aria-label={playing ? "Pause film" : "Play film"}
          onClick={() => {
            void togglePlay();
          }}
          className="absolute inset-0 z-[1] cursor-pointer border-0 bg-transparent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-4px] focus-visible:outline-[#5BA8A0]"
        />

        <button
          type="button"
          aria-pressed={captionsOn}
          onClick={(e) => {
            e.stopPropagation();
            setCaptionsOn((v) => !v);
          }}
          className="absolute right-4 top-4 z-10 rounded-full border border-white/15 bg-[#0A0E13]/90 px-3 py-1.5 text-[12px] text-[#F4F1EC] transition hover:border-white/25 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0] sm:right-5 sm:top-5"
        >
          Captions {captionsOn ? "on" : "off"}
        </button>

        {/* Film-style subtitles: bottom-centered overlay on the frame */}
        <div
          className="pointer-events-none absolute inset-x-0 bottom-0 z-10 flex justify-center px-4 pb-4 pt-16 sm:px-8 sm:pb-5"
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => e.stopPropagation()}
        >
          <Captions
            words={activeNarr?.words ?? []}
            localT={localT}
            visible={captionsOn}
            fallbackText={fallbackText}
            className="pointer-events-auto w-full max-w-3xl"
          />
        </div>

        {needsGesture && !playing ? (
          <div
            className="absolute inset-0 z-20 flex items-center justify-center bg-black/55"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                void playFromGlobal(globalTRef.current);
              }}
              className="inline-flex min-h-14 min-w-[10rem] items-center justify-center rounded-full bg-[#5BA8A0] px-8 text-base font-semibold text-[#0A0E13] transition hover:bg-[#6FB8B0] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
            >
              Play film
            </button>
          </div>
        ) : null}
      </div>

      <div
        className="flex flex-col gap-3 border-t border-white/[0.06] bg-[#0A0E13] px-4 py-3 sm:px-5"
        onClick={(e) => e.stopPropagation()}
      >
        <label className="sr-only" htmlFor="film-scrub">
          Scrub timeline
        </label>
        <input
          id="film-scrub"
          type="range"
          min={0}
          max={1}
          step={0.001}
          value={progress}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => {
            e.stopPropagation();
            void seekGlobal(
              fractionToGlobal(segments, Number(e.target.value)),
              false,
            );
          }}
          className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-white/15 accent-[#5BA8A0] [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-[#5BA8A0]"
        />

        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              void togglePlay();
            }}
            className="inline-flex min-h-10 min-w-[4.5rem] items-center justify-center rounded-full bg-[#5BA8A0] px-4 text-sm font-medium text-[#0A0E13] transition hover:bg-[#6FB8B0] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
            aria-label={playing ? "Pause" : "Play"}
          >
            {playing ? "Pause" : "Play"}
          </button>

          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              seekYear(-1);
            }}
            className="inline-flex min-h-10 items-center rounded-full px-3 text-sm text-[#A8A49C] transition hover:text-[#F4F1EC] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
            aria-label="Previous year"
          >
            Previous year
          </button>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              seekYear(1);
            }}
            className="inline-flex min-h-10 items-center rounded-full px-3 text-sm text-[#A8A49C] transition hover:text-[#F4F1EC] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
            aria-label="Next year"
          >
            Next year
          </button>

          <span className="ml-auto font-label text-[12px] tabular-nums text-[#7A766E]">
            {formatClock(globalT)} / {formatClock(totalS)}
          </span>
        </div>
      </div>
    </div>
  );
}
