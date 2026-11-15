/**
 * Sentence-synced film captions (FR-45). Captions ON by default.
 * Inputs: narration words + local audio time (segment-relative).
 * Outputs: one full sentence at a time over the frame (no word karaoke).
 */

"use client";

import { useMemo } from "react";
import clsx from "clsx";

export type CaptionWord = {
  text: string;
  start_s: number;
  end_s: number;
};

export type CaptionSentence = {
  text: string;
  start_s: number;
  end_s: number;
};

export type CaptionsProps = {
  words: CaptionWord[];
  /** Seconds relative to the active segment audio start. */
  localT: number;
  /** When false, the region stays in the layout but is visually hidden. */
  visible?: boolean;
  className?: string;
  /** Optional plain script line when words are empty. */
  fallbackText?: string;
};

/** True if this word token ends a sentence (. ? ! … plus optional closing quotes). */
const SENTENCE_END_RE = /[.?!…]["’”')\]]*$/u;

/**
 * Group word timings into sentences.
 * Split after a word whose text ends with `.` `?` `!` or `…` (optional quotes).
 * Sentence start_s = first word start; end_s = last word end. Text joined with spaces.
 */
export function wordsToSentences(words: CaptionWord[]): CaptionSentence[] {
  if (words.length === 0) return [];

  const sentences: CaptionSentence[] = [];
  let buf: CaptionWord[] = [];

  const flush = () => {
    if (buf.length === 0) return;
    const text = buf.map((w) => w.text).join(" ");
    sentences.push({
      text,
      start_s: buf[0].start_s,
      end_s: buf[buf.length - 1].end_s,
    });
    buf = [];
  };

  for (const w of words) {
    buf.push(w);
    if (SENTENCE_END_RE.test(w.text)) flush();
  }
  flush(); // trailing words without terminal punctuation

  return sentences;
}

/**
 * Active sentence: localT in [start_s, end_s).
 * In gaps after a sentence ends, keep showing the last sentence that has started
 * until the next one's start_s (avoids blank flashes between sentences).
 */
export function activeSentenceIndex(
  sentences: CaptionSentence[],
  localT: number,
): number {
  if (sentences.length === 0) return -1;
  const t = Math.max(0, localT);

  for (let i = 0; i < sentences.length; i++) {
    const s = sentences[i];
    if (t >= s.start_s && t < s.end_s) return i;
  }

  // Between / after: last sentence that has begun
  let last = -1;
  for (let i = 0; i < sentences.length; i++) {
    if (t >= sentences[i].start_s) last = i;
    else break;
  }
  return last;
}

export function Captions({
  words,
  localT,
  visible = true,
  className,
  fallbackText,
}: CaptionsProps) {
  const sentences = useMemo(() => wordsToSentences(words), [words]);
  const active = activeSentenceIndex(sentences, localT);
  const line =
    active >= 0
      ? sentences[active].text
      : words.length === 0
        ? (fallbackText ?? "")
        : "";

  if (!visible) {
    return <div className={clsx("min-h-[2.75rem]", className)} aria-hidden />;
  }

  if (!line) {
    return <div className={clsx("min-h-[2.75rem]", className)} aria-hidden />;
  }

  return (
    <div
      className={clsx(
        "rounded-md bg-[#0A0E13]/92 px-4 py-2.5 text-center shadow-[0_8px_28px_rgba(0,0,0,0.45)] ring-1 ring-white/10 sm:px-5 sm:py-3",
        className,
      )}
      aria-live="polite"
      aria-atomic="true"
    >
      <p className="font-ui text-[14px] leading-snug tracking-tight text-[#F4F1EC]/95 sm:text-[15px]">
        {line}
      </p>
    </div>
  );
}
