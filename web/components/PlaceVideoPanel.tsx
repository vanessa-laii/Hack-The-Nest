/**
 * Selected-place glass card — only mounts when a globe marker is chosen.
 * Inputs: selected place + close handler.
 * Outputs: quiet overlay with place name, placeholder film, Play CTA.
 */

"use client";

import Link from "next/link";
import { useEffect, useRef } from "react";
import { splitPlaceName, type PlaceLocation } from "@/lib/places";

type PlaceVideoPanelProps = {
  place: PlaceLocation;
  onClose: () => void;
};

function PlaceTitle({ displayName }: { displayName: string }) {
  const { primary, secondary } = splitPlaceName(displayName);

  return (
    <span className="block min-w-0">
      <span className="font-ui text-[1.45rem] font-semibold leading-[1.15] tracking-tight text-[#f5f5f7] sm:text-[1.6rem]">
        {primary}
      </span>
      {secondary ? (
        <span className="mt-1.5 block font-label text-[10px] uppercase tracking-[0.18em] text-[#6e6e73]">
          {secondary}
        </span>
      ) : null}
    </span>
  );
}

export function PlaceVideoPanel({ place, onClose }: PlaceVideoPanelProps) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    }

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [place.id, onClose]);

  return (
    <aside
      role="dialog"
      aria-modal="false"
      aria-label={`${place.displayName} film`}
      className="pointer-events-auto w-full"
    >
      <div className="rounded-3xl border border-white/10 bg-[#1a1a1d]/75 px-5 py-5 shadow-[0_24px_80px_rgba(18,18,20,0.45)] backdrop-blur-2xl supports-[backdrop-filter]:bg-[#1a1a1d]/65">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="font-label text-[10px] uppercase tracking-[0.28em] text-[#2997ff]/90">
              {place.stories.some((s) => s.id === "sample-lake-6km")
                ? "Mock sample · ready"
                : "Sample place"}
            </p>
            <h2 className="mt-2.5">
              <PlaceTitle displayName={place.displayName} />
            </h2>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className="shrink-0 rounded-full px-2.5 py-1 font-label text-[10px] uppercase tracking-[0.18em] text-[#6e6e73] transition hover:bg-white/5 hover:text-[#f5f5f7] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff]"
            aria-label="Close place card"
          >
            Close
          </button>
        </div>

        <p className="mt-3 font-label text-[10px] uppercase tracking-[0.2em] text-[#6e6e73]">
          {place.theme}
          <span className="mx-2 text-white/20" aria-hidden>
            ·
          </span>
          {place.stories.some((s) => s.id === "sample-lake-6km")
            ? "Backend mock"
            : "Placeholder"}
        </p>

        <div className="mt-5 flex flex-col gap-4">
          {place.stories.map((story) => (
            <article
              key={story.id}
              className="border-t border-white/[0.08] pt-4"
            >
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h3 className="font-ui text-lg font-semibold leading-snug tracking-tight text-[#f5f5f7]">
                  {story.title}
                </h3>
                <span className="font-label text-[10px] tracking-[0.12em] text-[#6e6e73]">
                  {story.firstYear}–{story.lastYear}
                </span>
              </div>
              <p className="mt-2.5 text-[13px] leading-relaxed text-[#a1a1a6]">
                {story.blurb}
              </p>
              <div className="mt-5 flex flex-wrap items-center gap-3">
                <Link
                  href={`/story/${story.id}`}
                  className="inline-flex min-h-10 items-center justify-center rounded-full bg-[#2997ff] px-5 text-sm font-medium text-white transition hover:bg-[#54aeff] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff]"
                >
                  Play film
                </Link>
                <span className="font-label text-[9px] uppercase tracking-[0.18em] text-[#6e6e73]">
                  {story.id === "sample-lake-6km"
                    ? "Open mock story"
                    : "Stub · sample only"}
                </span>
              </div>
            </article>
          ))}
        </div>
      </div>
    </aside>
  );
}
