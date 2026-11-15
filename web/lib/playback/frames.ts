/**
 * Frame-year selection for a segment at local time t.
 * Inputs: playback segment, available frame years, local seconds.
 * Outputs: year to show (nearest kept frame); after_year hold at change end.
 */

import type { Story } from "@/lib/types/story";
import type { Narration } from "@/lib/types/narration";
import type { PlaybackSegment } from "./types";

/** Last fraction of a change-chapter segment held on after_year. */
export const AFTER_YEAR_HOLD_RATIO = 0.12;

export function nearestFrameYear(
  availableYears: number[],
  target: number,
): number {
  if (availableYears.length === 0) return Math.round(target);
  let best = availableYears[0];
  let bestDist = Math.abs(best - target);
  for (let i = 1; i < availableYears.length; i++) {
    const y = availableYears[i];
    const d = Math.abs(y - target);
    if (d < bestDist) {
      best = y;
      bestDist = d;
    }
  }
  return best;
}

/**
 * Years from story.frames that fall inside [yearFrom, yearTo], inclusive.
 * Falls back to nearest frames outside the range if none sit inside.
 */
export function yearsInRange(
  availableYears: number[],
  yearFrom: number,
  yearTo: number,
): number[] {
  const lo = Math.min(yearFrom, yearTo);
  const hi = Math.max(yearFrom, yearTo);
  if (availableYears.length === 0) return [];

  const inside = availableYears.filter((y) => y >= lo && y <= hi);
  if (inside.length > 0) return inside;

  const start = nearestFrameYear(availableYears, lo);
  const end = nearestFrameYear(availableYears, hi);
  if (start === end) return [start];
  return availableYears.filter(
    (y) => y >= Math.min(start, end) && y <= Math.max(start, end),
  );
}

/**
 * Pick the frame year for segment + local t.
 * Spreads years in [year_from, year_to] evenly across duration; for change
 * chapters, holds after_year for the final AFTER_YEAR_HOLD_RATIO of the audio.
 */
export function pickFrameYear(
  segment: PlaybackSegment,
  localT: number,
  availableYears: number[],
  holdRatio: number = AFTER_YEAR_HOLD_RATIO,
): number {
  const duration = Math.max(segment.duration_s, 1e-6);
  const t = Math.max(0, Math.min(localT, duration));
  const progress = t / duration;

  const hold =
    segment.after_year != null && holdRatio > 0
      ? Math.min(Math.max(holdRatio, 0), 0.45)
      : 0;

  if (hold > 0 && segment.after_year != null && progress >= 1 - hold) {
    return nearestFrameYear(availableYears, segment.after_year);
  }

  const animProgress =
    hold > 0 ? Math.min(1, progress / (1 - hold)) : progress;

  const span = yearsInRange(
    availableYears,
    segment.year_from,
    segment.year_to,
  );
  if (span.length === 0) {
    return availableYears.length > 0
      ? nearestFrameYear(availableYears, segment.year_from)
      : Math.round(segment.year_from);
  }
  if (span.length === 1) return span[0];

  const idx = Math.round(animProgress * (span.length - 1));
  return span[Math.max(0, Math.min(idx, span.length - 1))];
}

/** Sorted unique years from story.frames. */
export function frameYearsFromStory(story: Story): number[] {
  return [...new Set(story.frames.map((f) => f.year))].sort((a, b) => a - b);
}

/**
 * Merge story.script.segments with narration durations + change after_year.
 */
export function buildPlaybackSegments(
  story: Story,
  narration: Narration,
): PlaybackSegment[] {
  const chapterById = new Map(story.chapters.map((c) => [c.id, c]));
  const changeById = new Map(story.changes.map((c) => [c.id, c]));
  const narrById = new Map(
    narration.segments.map((s) => [s.segment_id, s]),
  );

  return story.script.segments.map((seg) => {
    const narr = narrById.get(seg.id);
    const chapter = chapterById.get(seg.chapter_id);
    let afterYear: number | null = null;
    if (chapter?.kind === "change" && chapter.change_id) {
      const change = changeById.get(chapter.change_id);
      if (change?.after_year != null) afterYear = change.after_year;
    }
    return {
      segment_id: seg.id,
      chapter_id: seg.chapter_id,
      year_from: seg.year_from,
      year_to: seg.year_to,
      duration_s: narr?.duration_s ?? 0,
      after_year: afterYear,
    };
  });
}

/** Prev/next discrete year among available frames (keyboard ←/→). */
export function stepYear(
  availableYears: number[],
  currentYear: number,
  direction: -1 | 1,
): number {
  if (availableYears.length === 0) return currentYear;
  const sorted = [...availableYears].sort((a, b) => a - b);
  const idx = sorted.findIndex((y) => y === currentYear);
  if (idx === -1) {
    const nearest = nearestFrameYear(sorted, currentYear);
    const nIdx = sorted.indexOf(nearest);
    const next = nIdx + direction;
    return sorted[Math.max(0, Math.min(next, sorted.length - 1))];
  }
  const next = idx + direction;
  return sorted[Math.max(0, Math.min(next, sorted.length - 1))];
}

/**
 * Global time of the first moment a given year is shown under current segments.
 * Used when ←/→ seeks the scrubber to that year.
 */
export function globalTimeForYear(
  segments: PlaybackSegment[],
  availableYears: number[],
  year: number,
  samplesPerSegment = 48,
): number {
  if (segments.length === 0) return 0;
  let bestT = 0;
  let bestDist = Infinity;
  let acc = 0;
  for (const seg of segments) {
    const steps = Math.max(2, samplesPerSegment);
    for (let i = 0; i <= steps; i++) {
      const localT = (i / steps) * seg.duration_s;
      const y = pickFrameYear(seg, localT, availableYears);
      const dist = Math.abs(y - year);
      const globalT = acc + localT;
      if (dist < bestDist || (dist === bestDist && globalT < bestT)) {
        bestDist = dist;
        bestT = globalT;
      }
    }
    acc += seg.duration_s;
  }
  return bestT;
}
