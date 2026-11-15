/**
 * Light live-progress page — SSE from createStory events_url.
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
        <p className="em-chrome-label text-[#5BA8A0]">
          Building
        </p>
        <h1 className="font-display mt-2 text-4xl font-medium tracking-tight text-[#F4F1EC] sm:text-5xl">
          Assembling your film…
        </h1>
        <p className="mt-3 text-[#A8A49C]">
          Story <code className="text-[#5BA8A0]">{id}</code>
          {" "}
          — watching live build events.
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
        className="relative z-10 mb-2 w-fit text-sm text-[#A8A49C] transition hover:text-[#5BA8A0] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
      >
        ← Back to globe
      </Link>

      <Suspense
        fallback={
          <p className="relative z-10 mt-10 text-[#A8A49C]">Loading build…</p>
        }
      >
        <BuildContent params={params} searchParams={searchParams} />
      </Suspense>
    </main>
  );
}
