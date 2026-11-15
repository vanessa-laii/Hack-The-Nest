/**
 * Selected-place instrument panel — mounts when a globe marker is chosen.
 * Inputs: selected place + close handler.
 * Outputs: opaque overlay with place name, overview thumb, Play CTA when ready.
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
      <span className="font-ui text-[1.45rem] font-semibold leading-[1.15] tracking-tight text-[#F4F1EC] sm:text-[1.6rem]">
        {primary}
      </span>
      {secondary ? (
        <span className="mt-1.5 block text-[13px] text-[#7A766E]">
          {secondary}
        </span>
      ) : null}
    </span>
  );
}

function PlaceOverview({
  placeName,
  thumbUrl,
}: {
  placeName: string;
  thumbUrl?: string;
}) {
  if (thumbUrl) {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- remote /files thumbs from API_BASE
      <img
        src={thumbUrl}
        alt={`${placeName} overview`}
        className="mt-4 h-36 w-full rounded-md border border-white/10 object-cover"
      />
    );
  }

  return (
    <div
      className="mt-4 h-36 w-full rounded-md border border-white/10 bg-white/[0.04]"
      role="img"
      aria-label={`${placeName} overview unavailable`}
    />
  );
}

export function PlaceVideoPanel({ place, onClose }: PlaceVideoPanelProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const overviewThumb =
    place.stories.find((s) => s.ready && s.thumbUrl)?.thumbUrl ??
    place.stories.find((s) => s.thumbUrl)?.thumbUrl;

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
      <div className="rounded-xl border border-white/10 bg-[#14181E] px-5 py-5 shadow-[0_24px_80px_rgba(10,14,19,0.55)]">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h2>
              <PlaceTitle displayName={place.displayName} />
            </h2>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className="em-chrome-label shrink-0 rounded-full px-2.5 py-1 transition hover:bg-white/5 hover:text-[#F4F1EC] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
            aria-label="Close place card"
          >
            Close
          </button>
        </div>

        <PlaceOverview
          placeName={place.displayName}
          thumbUrl={overviewThumb}
        />

        <div className="mt-5 flex flex-col gap-4">
          {place.stories.map((story) => (
            <article
              key={story.id}
              className="border-t border-white/[0.08] pt-4"
            >
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h3 className="font-ui text-lg font-semibold leading-snug tracking-tight text-[#F4F1EC]">
                  {story.title}
                </h3>
                <span className="font-label text-[12px] tabular-nums text-[#7A766E]">
                  {story.firstYear}–{story.lastYear}
                </span>
              </div>
              <p className="mt-2.5 text-[13px] leading-relaxed text-[#A8A49C]">
                {story.blurb}
              </p>
              <div className="mt-5 flex flex-wrap items-center gap-3">
                {story.ready ? (
                  <Link
                    href={`/story/${story.id}`}
                    className="inline-flex min-h-10 items-center justify-center rounded-full bg-[#5BA8A0] px-5 text-sm font-medium text-[#0A0E13] transition hover:bg-[#6FB8B0] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
                  >
                    Play film
                  </Link>
                ) : (
                  <>
                    <span className="inline-flex min-h-10 items-center justify-center rounded-full border border-white/15 px-5 text-sm text-[#A8A49C]">
                      Film isn’t ready yet
                    </span>
                    <span className="em-chrome-label">Search to build</span>
                  </>
                )}
              </div>
            </article>
          ))}
        </div>
      </div>
    </aside>
  );
}
