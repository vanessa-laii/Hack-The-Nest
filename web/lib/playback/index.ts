/**
 * Film playback sync helpers (pure, no React).
 * Inputs: story + narration clocks / years.
 * Outputs: re-exported clock + frame helpers for FilmPlayer.
 */

export {
  AFTER_YEAR_HOLD_RATIO,
  buildPlaybackSegments,
  frameYearsFromStory,
  globalTimeForYear,
  nearestFrameYear,
  pickFrameYear,
  stepYear,
  yearsInRange,
} from "./frames";

export {
  fractionToGlobal,
  globalToLocal,
  isNarrationTooLong,
  localToGlobal,
  MAX_NARRATION_S,
  segmentOffsets,
  totalDuration,
} from "./clock";

export type { ClockPosition, PlaybackSegment } from "./types";
