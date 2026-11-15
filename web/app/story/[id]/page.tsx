/**
 * Story film page — player-dominant layout matching landing visual language.
 * Inputs: route param id (e.g. sample-lake-6km).
 * Outputs: FilmPlayer shell + quiet place header; captions ON by default.
 */

import Link from "next/link";
import { Suspense } from "react";
import { StoryFilm } from "@/components/StoryFilm";
import {
  friendlyErrorMessage,
  getStory,
  type ApiErrorCode,
} from "@/lib/api";

type StoryPageProps = {
  params: Promise<{ id: string }>;
};

async function StoryContent({ params }: StoryPageProps) {
  const { id } = await params;
  const story = await getStory(id);

  if (!story) {
    return (
      <div className="relative z-10 mt-8">
        <p className="font-label text-[10px] uppercase tracking-[0.28em] text-[#F0A35E]">
          Story not found
        </p>
        <h1 className="font-display mt-2 text-4xl font-medium tracking-tight text-[#f5f5f7]">
          Couldn’t load this film
        </h1>
        <p className="mt-3 max-w-lg text-[#a1a1a6]">
          {friendlyErrorMessage("NOT_READY" as ApiErrorCode)} Id{" "}
          <code className="text-[#2997ff]">{id}</code> — start the backend with{" "}
          <code className="text-[#2997ff]">EM_MOCK=1</code> and open{" "}
          <Link
            href="/story/sample-lake-6km"
            className="text-[#2997ff] underline-offset-2 hover:underline"
          >
            sample-lake-6km
          </Link>
          .
        </p>
        <Link
          href="/"
          className="mt-6 inline-flex min-h-11 items-center rounded-full border border-white/15 px-5 text-sm text-[#f5f5f7] transition hover:border-[#2997ff]/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff]"
        >
          Back to globe
        </Link>
      </div>
    );
  }

  const headline =
    story.chapters.find((c) => c.kind === "change")?.headline ??
    story.chapters[0]?.headline ??
    story.place.display_name;

  return (
    <>
      <header className="relative z-10 mb-6">
        <p className="font-label text-[10px] uppercase tracking-[0.28em] text-[#2997ff]">
          {story.is_sample ? "Sample film" : "Film"}
          {story.status === "partial" ? (
            <span className="ml-2 text-[#F0A35E]">· Partial</span>
          ) : null}
        </p>
        <h1 className="font-display mt-2 text-3xl font-medium tracking-tight text-[#f5f5f7] sm:text-4xl">
          {story.place.display_name}
        </h1>
        <p className="mt-1.5 font-ui text-base text-[#a1a1a6] sm:text-lg">
          {headline}
        </p>
      </header>

      {/* Dominant player */}
      <StoryFilm story={story} />

      {/* Quiet chapter list — not card soup */}
      {/* <section className="relative z-10 mt-10" aria-label="Chapters">
        <h2 className="font-label text-[10px] uppercase tracking-[0.28em] text-[#6e6e73]">
          Chapters
        </h2>
        <ol className="mt-3 divide-y divide-white/[0.06] border-t border-white/[0.06]">
          {story.chapters.map((ch) => (
            <li key={ch.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-3">
              <span className="font-ui text-[15px] font-semibold text-[#f5f5f7]">
                {ch.headline}
              </span>
              <span className="font-label text-[10px] uppercase tracking-[0.14em] text-[#6e6e73]">
                {ch.start_year}–{ch.end_year}
                {ch.confidence === "cause_unknown" ? (
                  <>
                    <span className="mx-2 text-white/20">·</span>
                    <span className="text-[#F0A35E]">Cause unknown</span>
                  </>
                ) : null}
              </span>
            </li>
          ))}
        </ol>
      </section> */}
    </>
  );
}

export default function StoryPage({ params }: StoryPageProps) {
  return (
    <main className="relative mx-auto flex w-full max-w-5xl flex-1 flex-col px-5 py-8 sm:px-8 sm:py-10">
      <Link
        href="/"
        className="relative z-10 mb-3 w-fit text-sm text-[#a1a1a6] transition hover:text-[#2997ff] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff]"
      >
        ← Back to globe
      </Link>

      <Suspense
        fallback={
          <p className="relative z-10 mt-8 text-[#a1a1a6]">Loading film…</p>
        }
      >
        <StoryContent params={params} />
      </Suspense>
    </main>
  );
}
