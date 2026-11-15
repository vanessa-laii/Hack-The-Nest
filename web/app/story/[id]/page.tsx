/**
 * Story film page — player-dominant layout matching landing visual language.
 * Inputs: route param id (e.g. sample-lake-6km).
 * Outputs: FilmPlayer + MetricChart + EvidencePanel + quiet about section.
 */

import Link from "next/link";
import { Suspense } from "react";
import { StoryBelowFold } from "@/components/StoryBelowFold";
import {
  friendlyErrorMessage,
  getStory,
  type ApiErrorCode,
} from "@/lib/api";
import type { Story } from "@/lib/types/story";

type StoryPageProps = {
  params: Promise<{ id: string }>;
};

const MONTH_SHORT = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function formatAdmin(admin: Story["place"]["admin"]): string {
  const parts = [
    admin.city,
    admin.county,
    admin.state,
    admin.country,
  ].filter((p): p is string => typeof p === "string" && p.length > 0);
  return parts.join(", ");
}

function formatCreatedAt(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function formatSeason(months: Story["coverage"]["season_months"]): string {
  const first = MONTH_SHORT[months[0]! - 1] ?? String(months[0]);
  const last =
    MONTH_SHORT[months[months.length - 1]! - 1] ??
    String(months[months.length - 1]);
  return months.length === 1 ? first : `${first}–${last}`;
}

function sensorsUsed(frames: Story["frames"]): string[] {
  const set = new Set<string>();
  for (const frame of frames) {
    for (const s of frame.sensors) set.add(s);
  }
  return [...set].sort();
}

function AboutThisFilm({ story }: { story: Story }) {
  const adminLine = formatAdmin(story.place.admin);
  const sensors = sensorsUsed(story.frames);
  const warnings = story.generation.warnings ?? [];
  const validation = story.validation;

  return (
    <section
      className="relative z-10 mt-12 border-t border-white/[0.06] pt-8"
      aria-label="About this film"
    >
      <h2 className="font-label text-[10px] uppercase tracking-[0.28em] text-[#6e6e73]">
        About this film / data
      </h2>

      <dl className="mt-4 space-y-4 text-sm">
        <div>
          <dt className="font-label text-[10px] uppercase tracking-[0.14em] text-[#6e6e73]">
            Place
          </dt>
          <dd className="mt-1 font-ui text-[#f5f5f7]">
            {story.place.display_name}
            {adminLine ? (
              <span className="text-[#a1a1a6]"> · {adminLine}</span>
            ) : null}
          </dd>
          <dd className="mt-1 text-[#a1a1a6]">
            Area ≈ {story.place.aoi_km} km across · 1 pixel ≈ 30 m (Landsat)
          </dd>
        </div>

        <div>
          <dt className="font-label text-[10px] uppercase tracking-[0.14em] text-[#6e6e73]">
            Coverage
          </dt>
          <dd className="mt-1 text-[#f5f5f7]">
            {story.coverage.first_year}–{story.coverage.last_year}
            <span className="text-[#a1a1a6]">
              {" "}
              · season {formatSeason(story.coverage.season_months)}
            </span>
          </dd>
          {story.coverage.years_skipped.length > 0 ? (
            <dd className="mt-2">
              <p className="font-label text-[10px] uppercase tracking-[0.14em] text-[#6e6e73]">
                Years skipped
              </p>
              <ul className="mt-1.5 space-y-1 text-[#a1a1a6]">
                {story.coverage.years_skipped.map((s) => (
                  <li key={s.year}>
                    <span className="text-[#f5f5f7]">{s.year}</span>
                    <span className="mx-2 text-white/20">·</span>
                    {s.reason}
                  </li>
                ))}
              </ul>
            </dd>
          ) : null}
        </div>

        <div>
          <dt className="font-label text-[10px] uppercase tracking-[0.14em] text-[#6e6e73]">
            Status
          </dt>
          <dd className="mt-1 text-[#f5f5f7]">
            {story.status}
            {story.is_sample ? (
              <span className="ml-2 text-[#2997ff]">· Sample / made-up data</span>
            ) : null}
            {story.status === "partial" ? (
              <span className="ml-2 text-[#F0A35E]">
                · Partial — saved after timeout, budget stop, or upstream failure
              </span>
            ) : null}
          </dd>
          <dd className="mt-1 text-[#a1a1a6]">
            Created {formatCreatedAt(story.created_at)}
          </dd>
        </div>

        {sensors.length > 0 ? (
          <div>
            <dt className="font-label text-[10px] uppercase tracking-[0.14em] text-[#6e6e73]">
              Sensors used
            </dt>
            <dd className="mt-1 text-[#a1a1a6]">{sensors.join(", ")}</dd>
          </div>
        ) : null}

        {validation ? (
          <div>
            <dt className="font-label text-[10px] uppercase tracking-[0.14em] text-[#6e6e73]">
              How we checked
            </dt>
            <dd className="mt-1 text-[#f5f5f7]">{validation.summary}</dd>
            <dd className="mt-1 text-[#a1a1a6]">
              Reference:{" "}
              <a
                href={validation.reference_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[#2997ff] underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#2997ff]"
              >
                {validation.reference_name}
              </a>
              <span className="mx-2 text-white/20">·</span>
              metric {validation.metric}
            </dd>
          </div>
        ) : null}

        {warnings.length > 0 ? (
          <div>
            <dt className="font-label text-[10px] uppercase tracking-[0.14em] text-[#6e6e73]">
              Generation notes
            </dt>
            <dd className="mt-1">
              <ul className="space-y-1 text-[#a1a1a6]">
                {warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </dd>
          </div>
        ) : null}

        {story.credits.length > 0 ? (
          <div>
            <dt className="font-label text-[10px] uppercase tracking-[0.14em] text-[#6e6e73]">
              Credits
            </dt>
            <dd className="mt-1">
              <ul className="space-y-1 text-[#a1a1a6]">
                {story.credits.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </dd>
          </div>
        ) : null}
      </dl>

      <p className="mt-6 text-sm text-[#6e6e73]">
        Site-wide imagery credits also appear in the footer. Claim sources live
        in Evidence above.
      </p>

      {process.env.NODE_ENV === "development" ? (
        <p className="mt-3 font-label text-[10px] uppercase tracking-[0.12em] text-[#6e6e73]/80">
          Debug · gen {story.generation.duration_s}s · exa{" "}
          {story.generation.usage.exa_calls} · llm{" "}
          {story.generation.usage.llm_calls} · vision{" "}
          {story.generation.usage.vision_calls}
        </p>
      ) : null}
    </section>
  );
}

async function StoryContent({ params }: StoryPageProps) {
  const { id } = await params;
  const story = await getStory(id);

  if (!story) {
    return (
      <div className="relative z-10 mt-8">
        <p className="font-label text-[10px] uppercase tracking-[0.28em] text-[#F0A35E]">
          Story not found
        </p>
        <h1 className="font-ui mt-2 text-4xl font-medium tracking-tight text-[#f5f5f7]">
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
        <h1 className="font-ui mt-2 text-3xl font-medium tracking-tight text-[#f5f5f7] sm:text-4xl">
          {story.place.display_name}
        </h1>
        <p className="mt-1.5 font-ui text-base text-[#a1a1a6] sm:text-lg">
          {headline}
        </p>
      </header>

      <StoryBelowFold story={story} />
      <AboutThisFilm story={story} />
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
