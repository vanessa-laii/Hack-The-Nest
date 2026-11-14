/**
 * Stub live-progress page while a story is building.
 * Inputs: route param id.
 * Outputs: "building…" shell for Phase 1 ProgressFeed.
 */

import Link from "next/link";
import { Suspense } from "react";

type BuildPageProps = {
  params: Promise<{ id: string }>;
};

const STAGES = [
  "Geocode",
  "Scenes",
  "Composite",
  "Metrics",
  "Changes",
  "Research",
  "Verify",
  "Script",
  "Save",
] as const;

async function BuildContent({ params }: BuildPageProps) {
  const { id } = await params;

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
          Story <code className="text-[#2997ff]">{id}</code> — live progress
          feed arrives in Phase 1.
        </p>
      </header>

      <ol
        className="relative z-10 mt-10 space-y-3"
        aria-label="Pipeline stages"
      >
        {STAGES.map((stage, i) => (
          <li
            key={stage}
            className="flex items-center gap-3 rounded-xl border border-white/8 bg-white/[0.02] px-4 py-3 text-sm text-[#a1a1a6]"
          >
            <span className="font-label text-[11px] text-[#6e6e73]">
              {String(i + 1).padStart(2, "0")}
            </span>
            {stage}
          </li>
        ))}
      </ol>
    </>
  );
}

export default function BuildPage({ params }: BuildPageProps) {
  return (
    <main className="relative mx-auto flex w-full max-w-2xl flex-1 flex-col px-5 py-10 sm:px-8">
      <Link
        href="/"
        className="relative z-10 w-fit text-sm text-[#a1a1a6] transition hover:text-[#2997ff] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff]"
      >
        ← Back to globe
      </Link>

      <Suspense
        fallback={
          <p className="relative z-10 mt-10 text-[#a1a1a6]">Loading build…</p>
        }
      >
        <BuildContent params={params} />
      </Suspense>
    </main>
  );
}
