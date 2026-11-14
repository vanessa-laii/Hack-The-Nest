/**
 * Place search control — sits beside the full-bleed globe.
 * Inputs: optional onSubmit callback.
 * Outputs: minimal Apple-like search field.
 */

"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

type SearchBoxProps = {
  onSubmitQuery?: (query: string) => void;
  className?: string;
};

export function SearchBox({ onSubmitQuery, className }: SearchBoxProps) {
  const [query, setQuery] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    };
  }, []);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) return;
    if (onSubmitQuery) {
      onSubmitQuery(trimmed);
      return;
    }
    setNotice("Live build coming soon — tap a glowing place for now.");
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), 4200);
  }

  return (
    <div className={className}>
      <form
        onSubmit={handleSubmit}
        className="flex w-full items-stretch gap-2"
        role="search"
      >
        <label htmlFor="place-search" className="sr-only">
          Search for a place
        </label>
        <input
          id="place-search"
          type="search"
          name="query"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Lake Mead, Nevada"
          autoComplete="off"
          className="min-h-11 flex-1 rounded-full border border-white/15 bg-black/45 px-5 text-sm text-[#f5f5f7] placeholder:text-[#6e6e73] outline-none backdrop-blur-md transition focus:border-[#2997ff]/70 focus:ring-1 focus:ring-[#2997ff]/40"
        />
        <button
          type="submit"
          className="min-h-11 shrink-0 rounded-full bg-[#2997ff] px-5 text-sm font-medium text-white transition hover:bg-[#54aeff] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff]"
        >
          Go
        </button>
      </form>
      {notice ? (
        <p
          role="status"
          className="mt-2 text-xs text-[#a1a1a6]"
          aria-live="polite"
        >
          {notice}
        </p>
      ) : null}
    </div>
  );
}
