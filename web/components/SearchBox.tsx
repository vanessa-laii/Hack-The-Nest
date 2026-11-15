/**
 * Place search — createStory then navigate to /build/[id].
 * Inputs: optional onSubmit override; otherwise hits mock/real POST /api/stories.
 * Outputs: search field + friendly error status.
 */

"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  createStory,
  friendlyErrorMessage,
  isApiError,
  type ApiErrorCode,
} from "@/lib/api";

type SearchBoxProps = {
  onSubmitQuery?: (query: string) => void;
  className?: string;
};

export function SearchBox({ onSubmitQuery, className }: SearchBoxProps) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    };
  }, []);

  function flash(msg: string, ms = 5000) {
    setNotice(msg);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), ms);
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = query.trim();
    if (!trimmed || busy) return;

    if (onSubmitQuery) {
      onSubmitQuery(trimmed);
      return;
    }

    setBusy(true);
    setNotice(null);
    try {
      const result = await createStory({ query: trimmed });
      if (isApiError(result)) {
        const code = result.error.code as ApiErrorCode;
        if (code === "PLACE_AMBIGUOUS" && result.error.candidates?.length) {
          flash(
            `${friendlyErrorMessage(code)} Try: ${result.error.candidates
              .slice(0, 3)
              .map((c) => c.display_name)
              .join(", ")}`,
          );
        } else {
          // Prefer backend message (e.g. EM_MOCK=0: live generation not available yet).
          flash(result.error.message || friendlyErrorMessage(code));
        }
        return;
      }

      // Mock returns sample-lake-6km; real pipeline goes to build + SSE.
      if (result.cached) {
        router.push(`/story/${result.id}`);
      } else {
        const eventsQ = result.events_url
          ? `?events=${encodeURIComponent(result.events_url)}`
          : "";
        router.push(`/build/${result.id}${eventsQ}`);
      }
    } catch {
      flash(
        "Couldn’t reach the backend. Run it with EM_MOCK=1 on port 8000, or open Sample Lake on the globe.",
      );
    } finally {
      setBusy(false);
    }
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
          disabled={busy}
          className="min-h-11 flex-1 rounded-full border border-white/15 bg-black/45 px-5 text-sm text-[#f5f5f7] placeholder:text-[#6e6e73] outline-none backdrop-blur-md transition focus:border-[#2997ff]/70 focus:ring-1 focus:ring-[#2997ff]/40 disabled:opacity-60"
        />
        <button
          type="submit"
          disabled={busy}
          className="min-h-11 shrink-0 rounded-full bg-[#2997ff] px-5 text-sm font-medium text-white transition hover:bg-[#54aeff] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff] disabled:cursor-wait disabled:opacity-60"
        >
          {busy ? "…" : "Go"}
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
