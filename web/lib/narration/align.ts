/**
 * Convert ElevenLabs character-level alignment into word timings.
 * Inputs: parallel characters / start / end arrays from with-timestamps.
 * Outputs: words[{ text, start_s, end_s }] relative to segment audio start.
 */

export type CharAlignment = {
  characters: string[];
  character_start_times_seconds: number[];
  character_end_times_seconds: number[];
};

export type WordTiming = {
  text: string;
  start_s: number;
  end_s: number;
};

export function charactersToWords(alignment: CharAlignment): WordTiming[] {
  const { characters, character_start_times_seconds: starts, character_end_times_seconds: ends } =
    alignment;

  if (
    characters.length === 0 ||
    characters.length !== starts.length ||
    characters.length !== ends.length
  ) {
    throw new Error("Invalid alignment: empty or mismatched parallel arrays");
  }

  const words: WordTiming[] = [];
  let buf = "";
  let wStart: number | null = null;

  for (let i = 0; i < characters.length; i++) {
    const ch = characters[i];
    if (/\s/.test(ch)) {
      if (buf.length > 0 && wStart !== null) {
        words.push({ text: buf, start_s: wStart, end_s: ends[i - 1]! });
        buf = "";
        wStart = null;
      }
      continue;
    }
    if (wStart === null) wStart = starts[i]!;
    buf += ch;
  }

  if (buf.length > 0 && wStart !== null) {
    words.push({
      text: buf,
      start_s: wStart,
      end_s: ends[characters.length - 1]!,
    });
  }

  return words;
}

/** Segment duration from the last character end time (seconds). */
export function durationFromAlignment(alignment: CharAlignment): number {
  const ends = alignment.character_end_times_seconds;
  if (!ends.length) return 0;
  return ends[ends.length - 1]!;
}
