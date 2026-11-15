/**
 * Light live-progress page — SSE replay from mock backend (Phase 0).
 * Inputs: route param id (+ optional ?events= from createStory).
 * Outputs: ProgressFeed; redirects to /story/[id] on done.
 */

import Link from "next/link";
import { Suspense } from "react";
import { ProgressFeed } from "@/components/ProgressFeed";

type BuildPageProps = {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ events?: string }>;
};

async function BuildContent({ params, searchParams }: BuildPageProps) {
  const { id } = await params;
  const { events } = await searchParams;

  return (
    <>
      <header className="relative z-10 mt-10">
        <p className="font-label text-[10px] uppercase tracking-[0.28em] text-[#2997ff]">
          Building
        </p>
        <h1 className="font-display mt-2 text-4xl font-medium tracking-tight text-[#f5f5f7] sm:text-5xl">
          Assembling your film…
        </h1>
        <p className="mt-3 text-[#a1a1a6]">
          Story <code className="text-[#2997ff]">{id}</code>
          {id === "sample-lake-6km" ? (
            <>
              {" "}
              — mock mode replays sample events (~1 s apart).
            </>
          ) : null}
        </p>
      </header>

      <ProgressFeed storyId={id} eventsPath={events ?? null} />
    </>
  );
}

export default function BuildPage({ params, searchParams }: BuildPageProps) {
  return (
    <main className="relative mx-auto flex w-full max-w-2xl flex-1 flex-col px-5 py-10 sm:px-8">
      <Link
        href="/"
        className="relative z-10 mb-2 w-fit text-sm text-[#a1a1a6] transition hover:text-[#2997ff] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff]"
      >
        ← Back 
      </Link>

      <Suspense
        fallback={
          <p className="relative z-10 mt-10 text-[#a1a1a6]">Loading build…</p>
        }
      >
        <BuildContent params={params} searchParams={searchParams} />
      </Suspense>
    </main>
  );
}
