/**
 * Shared playback types for FilmPlayer sync helpers.
 * Inputs: story script/chapters + narration segment durations.
 * Outputs: clock position and frame-year selection inputs.
 */

export type PlaybackSegment = {
  segment_id: string;
  chapter_id: string;
  year_from: number;
  year_to: number;
  duration_s: number;
  /** Change chapters: hold when the year path reaches this (CONTRACTS §2). */
  after_year: number | null;
};

export type ClockPosition = {
  segmentIndex: number;
  localT: number;
  globalT: number;
  /** True when globalT is at or past total duration. */
  ended: boolean;
};
