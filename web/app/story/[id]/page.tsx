/**
 * Story film page — player-dominant layout matching landing visual language.
 * Inputs: route param id (e.g. lake-mead-nevada-us-6km).
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
      <h2 className="em-chrome-label text-[#7A766E]">
        About this film / data
      </h2>

      <dl className="mt-4 space-y-4 text-sm">
        <div>
          <dt className="em-chrome-label text-[#7A766E]">
            Place
          </dt>
          <dd className="mt-1 font-ui text-[#F4F1EC]">
            {story.place.display_name}
            {adminLine ? (
              <span className="text-[#A8A49C]"> · {adminLine}</span>
            ) : null}
          </dd>
          <dd className="mt-1 text-[#A8A49C]">
            Area ≈ {story.place.aoi_km} km across · 1 pixel ≈ 30 m (Landsat)
          </dd>
        </div>

        <div>
          <dt className="em-chrome-label text-[#7A766E]">
            Coverage
          </dt>
          <dd className="mt-1 text-[#F4F1EC]">
            {story.coverage.first_year}–{story.coverage.last_year}
            <span className="text-[#A8A49C]">
              {" "}
              · season {formatSeason(story.coverage.season_months)}
            </span>
          </dd>
          {story.coverage.years_skipped.length > 0 ? (
            <dd className="mt-2">
              <p className="em-chrome-label text-[#7A766E]">
                Years skipped
              </p>
              <ul className="mt-1.5 space-y-1 text-[#A8A49C]">
                {story.coverage.years_skipped.map((s) => (
                  <li key={s.year}>
                    <span className="text-[#F4F1EC]">{s.year}</span>
                    <span className="mx-2 text-white/20">·</span>
                    {s.reason}
                  </li>
                ))}
              </ul>
            </dd>
          ) : null}
        </div>

        <div>
          <dt className="em-chrome-label text-[#7A766E]">
            Status
          </dt>
          <dd className="mt-1 text-[#F4F1EC]">
            {story.status}
            {story.is_sample ? (
              <span className="ml-2 text-[#5BA8A0]">· Sample / made-up data</span>
            ) : null}
            {story.status === "partial" ? (
              <span className="ml-2 text-[#C4A574]">
                · Partial — saved after timeout, budget stop, or upstream failure
              </span>
            ) : null}
          </dd>
          <dd className="mt-1 text-[#A8A49C]">
            Created {formatCreatedAt(story.created_at)}
          </dd>
        </div>

        {sensors.length > 0 ? (
          <div>
            <dt className="em-chrome-label text-[#7A766E]">
              Sensors used
            </dt>
            <dd className="mt-1 text-[#A8A49C]">{sensors.join(", ")}</dd>
          </div>
        ) : null}

        {validation ? (
          <div>
            <dt className="em-chrome-label text-[#7A766E]">
              How we checked
            </dt>
            <dd className="mt-1 text-[#F4F1EC]">{validation.summary}</dd>
            <dd className="mt-1 text-[#A8A49C]">
              Reference:{" "}
              <a
                href={validation.reference_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[#5BA8A0] underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
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
            <dt className="em-chrome-label text-[#7A766E]">
              Generation notes
            </dt>
            <dd className="mt-1">
              <ul className="space-y-1 text-[#A8A49C]">
                {warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </dd>
          </div>
        ) : null}

        {story.credits.length > 0 ? (
          <div>
            <dt className="em-chrome-label text-[#7A766E]">
              Credits
            </dt>
            <dd className="mt-1">
              <ul className="space-y-1 text-[#A8A49C]">
                {story.credits.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </dd>
          </div>
        ) : null}
      </dl>

      <p className="mt-6 text-sm text-[#7A766E]">
        Site-wide imagery credits also appear in the footer. Claim sources live
        in Evidence above.
      </p>

      {process.env.NODE_ENV === "development" ? (
        <p className="mt-3 em-chrome-label text-[#7A766E]/80">
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
        <p className="em-chrome-label text-[#C4A574]">
          Story not found
        </p>
        <h1 className="font-ui mt-2 text-4xl font-medium tracking-tight text-[#F4F1EC]">
          Couldn’t load this film
        </h1>
        <p className="mt-3 max-w-lg text-[#A8A49C]">
          {friendlyErrorMessage("NOT_READY" as ApiErrorCode)} Id{" "}
          <code className="text-[#5BA8A0]">{id}</code> isn’t on the server yet.
          Search from the globe to build a film, or open a place once it appears
          in the gallery.
        </p>
        <Link
          href="/"
          className="mt-6 inline-flex min-h-11 items-center rounded-full border border-white/15 px-5 text-sm text-[#F4F1EC] transition hover:border-[#5BA8A0]/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
        >
          ← Back to globe
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
        <p className="em-chrome-label text-[#5BA8A0]">
          {story.is_sample ? "Sample film" : "Film"}
          {story.status === "partial" ? (
            <span className="ml-2 text-[#C4A574]">· Partial</span>
          ) : null}
        </p>
        <h1 className="font-ui mt-2 text-3xl font-medium tracking-tight text-[#F4F1EC] sm:text-4xl">
          {story.place.display_name}
        </h1>
        <p className="mt-1.5 font-ui text-base text-[#A8A49C] sm:text-lg">
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
        className="relative z-10 mb-3 w-fit text-sm text-[#A8A49C] transition hover:text-[#5BA8A0] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
      >
        ← Back to globe
      </Link>

      <Suspense
        fallback={
          <p className="relative z-10 mt-8 text-[#A8A49C]">Loading film…</p>
        }
      >
        <StoryContent params={params} />
      </Suspense>
    </main>
  );
}
