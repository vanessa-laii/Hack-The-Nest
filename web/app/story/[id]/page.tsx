/**
 * Stub story player shell for placeholder films.
 * Inputs: route param id.
 * Outputs: placeholder player UI (no real crossfade yet).
 */

import Link from "next/link";
import { Suspense } from "react";
import { getStoryStub } from "@/lib/places";

type StoryPageProps = {
  params: Promise<{ id: string }>;
};

async function StoryContent({ params }: StoryPageProps) {
  const { id } = await params;
  const match = getStoryStub(id);

  return (
    <>
      <header className="relative z-10 mt-8">
        <p className="font-label text-[10px] uppercase tracking-[0.28em] text-[#2997ff]">
          Story player · stub
        </p>
        <h1 className="font-display mt-2 text-4xl font-medium tracking-tight text-[#f5f5f7] sm:text-5xl">
          {match?.story.title ?? "Unknown story"}
        </h1>
        <p className="mt-2 text-[#a1a1a6]">
          {match
            ? `${match.place.displayName} · ${match.story.firstYear}–${match.story.lastYear}`
            : `id: ${id}`}
        </p>
        <span className="mt-3 inline-block rounded-full bg-[#2997ff]/15 px-3 py-0.5 font-label text-[9px] uppercase tracking-wider text-[#2997ff]">
          Placeholder
        </span>
      </header>

      <div className="relative z-10 mt-8 aspect-video w-full overflow-hidden rounded-2xl border border-white/10 bg-gradient-to-br from-[#1a1a1d] to-[#121214]">
        <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
          <p className="font-display text-2xl text-[#f5f5f7]/85">
            Film player coming in Phase 1
          </p>
          <p className="max-w-md text-sm text-[#a1a1a6]">
            {match?.story.blurb ??
              "This id is not in the placeholder gallery. Real story.json playback will land here."}
          </p>
          <button
            type="button"
            disabled
            className="mt-2 min-h-11 cursor-not-allowed rounded-full bg-white/10 px-5 text-sm text-[#6e6e73]"
            aria-disabled="true"
          >
            Play / Pause (soon)
          </button>
        </div>
      </div>

      <p className="relative z-10 mt-6 text-xs text-[#6e6e73]">
        Keyboard: Space = play/pause · ←/→ = previous/next year (wired in
        FilmPlayer).
      </p>
    </>
  );
}

export default function StoryPage({ params }: StoryPageProps) {
  return (
    <main className="relative mx-auto flex w-full max-w-4xl flex-1 flex-col px-5 py-10 sm:px-8">
      <Link
        href="/"
        className="relative z-10 w-fit text-sm text-[#a1a1a6] transition hover:text-[#2997ff] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff]"
      >
        ← Back to globe
      </Link>

      <Suspense
        fallback={
          <p className="relative z-10 mt-8 text-[#a1a1a6]">Loading story…</p>
        }
      >
        <StoryContent params={params} />
      </Suspense>
    </main>
  );
}
