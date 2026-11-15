/**
 * Pure global-time ↔ segment+local-t mapping for film sync.
 * Inputs: segment durations + a global or local clock value.
 * Outputs: ClockPosition / global seconds (no DOM).
 */

import type { ClockPosition, PlaybackSegment } from "./types";

/** HL-1 hard reject for playback (matches /api/narrate). */
export const MAX_NARRATION_S = 95;

export function segmentOffsets(segments: PlaybackSegment[]): number[] {
  const offsets: number[] = [];
  let acc = 0;
  for (const seg of segments) {
    offsets.push(acc);
    acc += Math.max(0, seg.duration_s);
  }
  return offsets;
}

export function totalDuration(segments: PlaybackSegment[]): number {
  return segments.reduce((sum, s) => sum + Math.max(0, s.duration_s), 0);
}

export function isNarrationTooLong(totalDurationS: number): boolean {
  return totalDurationS > MAX_NARRATION_S;
}

/**
 * Map film-global seconds → active segment index + local seconds.
 * Clamps past the end to the last segment at its duration.
 */
export function globalToLocal(
  segments: PlaybackSegment[],
  globalT: number,
): ClockPosition {
  if (segments.length === 0) {
    return { segmentIndex: 0, localT: 0, globalT: 0, ended: true };
  }

  const offsets = segmentOffsets(segments);
  const total = totalDuration(segments);
  const t = Number.isFinite(globalT) ? Math.max(0, globalT) : 0;

  if (t >= total) {
    const last = segments.length - 1;
    return {
      segmentIndex: last,
      localT: segments[last].duration_s,
      globalT: total,
      ended: true,
    };
  }

  for (let i = 0; i < segments.length; i++) {
    const start = offsets[i];
    const end = start + segments[i].duration_s;
    if (t < end || i === segments.length - 1) {
      return {
        segmentIndex: i,
        localT: Math.min(Math.max(0, t - start), segments[i].duration_s),
        globalT: t,
        ended: false,
      };
    }
  }

  const last = segments.length - 1;
  return {
    segmentIndex: last,
    localT: segments[last].duration_s,
    globalT: total,
    ended: true,
  };
}

export function localToGlobal(
  segments: PlaybackSegment[],
  segmentIndex: number,
  localT: number,
): number {
  if (segments.length === 0) return 0;
  const i = Math.max(0, Math.min(segmentIndex, segments.length - 1));
  const offsets = segmentOffsets(segments);
  const dur = segments[i].duration_s;
  const clamped = Math.max(0, Math.min(localT, dur));
  return offsets[i] + clamped;
}

/** Seek helpers for scrubbing by fraction of total film. */
export function fractionToGlobal(
  segments: PlaybackSegment[],
  fraction: number,
): number {
  const total = totalDuration(segments);
  const f = Math.max(0, Math.min(1, fraction));
  return f * total;
}
