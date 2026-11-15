/**
 * Frame-year selection for a segment at local time t.
 * Inputs: playback segment, available frame years, local seconds.
 * Outputs: year to show (nearest kept frame); after_year hold mid-segment when needed.
 */

import type { Story } from "@/lib/types/story";
import type { Narration } from "@/lib/types/narration";
import type { PlaybackSegment } from "./types";

/** Fraction of segment audio spent held on a change’s after_year. */
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
 * Where along [year_from, year_to] the change’s after_year sits (0…1).
 * Clamped so a hold always lands on the segment’s year path.
 */
function afterYearPauseAt(
  yearFrom: number,
  yearTo: number,
  afterYear: number,
): number {
  const span = yearTo - yearFrom;
  if (span === 0) return 1;
  return Math.min(1, Math.max(0, (afterYear - yearFrom) / span));
}

/** Map film progress → year-span progress, inserting a hold at pauseAt. */
function animProgressWithHold(
  progress: number,
  hold: number,
  pauseAt: number,
): number {
  if (hold <= 0) return progress;

  const preEnd = pauseAt * (1 - hold);
  const holdEnd = preEnd + hold;

  if (progress < preEnd) {
    if (preEnd <= 0) return pauseAt;
    return (progress / preEnd) * pauseAt;
  }
  if (progress < holdEnd) return pauseAt;

  const post = 1 - holdEnd;
  if (post <= 0 || pauseAt >= 1) return 1;
  return pauseAt + ((progress - holdEnd) / post) * (1 - pauseAt);
}

/**
 * Pick the frame year for segment + local t.
 * Spreads years in [year_from, year_to] across duration. For change chapters,
 * holds on after_year for AFTER_YEAR_HOLD_RATIO when the year path reaches it
 * (then continues through any quiet years up to year_to).
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

  const hold =
    segment.after_year != null && holdRatio > 0
      ? Math.min(Math.max(holdRatio, 0), 0.45)
      : 0;

  const pauseAt =
    hold > 0 && segment.after_year != null
      ? afterYearPauseAt(
          segment.year_from,
          segment.year_to,
          segment.after_year,
        )
      : 1;

  if (hold > 0 && segment.after_year != null) {
    const preEnd = pauseAt * (1 - hold);
    const holdEnd = preEnd + hold;
    if (progress >= preEnd && progress < holdEnd) {
      return nearestFrameYear(availableYears, segment.after_year);
    }
  }

  const animProgress = animProgressWithHold(progress, hold, pauseAt);
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
 * Global time when a given year is shown. With fromGlobalT + direction, picks
 * the next/previous occurrence so ←/→ cannot jump the wrong way across
 * overlapping segment year ranges.
 */
export function globalTimeForYear(
  segments: PlaybackSegment[],
  availableYears: number[],
  year: number,
  fromGlobalT = 0,
  direction: -1 | 1 = 1,
  samplesPerSegment = 48,
): number {
  if (segments.length === 0) return 0;

  const hits: number[] = [];
  let fallbackT = 0;
  let fallbackDist = Infinity;
  let acc = 0;

  for (const seg of segments) {
    const steps = Math.max(2, samplesPerSegment);
    for (let i = 0; i <= steps; i++) {
      const localT = (i / steps) * seg.duration_s;
      const y = pickFrameYear(seg, localT, availableYears);
      const globalT = acc + localT;
      const dist = Math.abs(y - year);
      if (y === year) hits.push(globalT);
      if (
        dist < fallbackDist ||
        (dist === fallbackDist && globalT < fallbackT)
      ) {
        fallbackDist = dist;
        fallbackT = globalT;
      }
    }
    acc += seg.duration_s;
  }

  const eps = 1e-3;
  if (hits.length === 0) return fallbackT;

  if (direction === 1) {
    const after = hits.find((t) => t > fromGlobalT + eps);
    return after ?? hits[hits.length - 1]!;
  }

  let before: number | undefined;
  for (const t of hits) {
    if (t < fromGlobalT - eps) before = t;
    else break;
  }
  return before ?? hits[0]!;
}
