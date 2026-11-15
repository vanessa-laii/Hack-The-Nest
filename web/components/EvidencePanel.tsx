/**
 * Claim evidence: before/after frames, sources, verified excerpts (FR-54).
 * Inputs: story chapters, changes, frames, sources; optional active claim id.
 * Outputs: expandable claim rows; intro/outro as quiet chapter strips.
 */

"use client";

import { useState } from "react";
import clsx from "clsx";
import { fileUrl } from "@/lib/api";
import type { Story } from "@/lib/types/story";

type Chapter = Story["chapters"][number];
type Change = Story["changes"][number];
type Frame = Story["frames"][number];
type Source = Story["sources"][number];
type Excerpt = Source["excerpts"][number];
type Claim = Chapter["claims"][number];

export type EvidencePanelProps = {
  story: Story;
  className?: string;
};

type ResolvedExcerpt = {
  excerpt: Excerpt;
  source: Source;
};

function formatHa(areaHa: number): string {
  if (areaHa >= 100) {
    return `≈ ${Math.round(areaHa).toLocaleString("en-US")} ha`;
  }
  const rounded = Number.isInteger(areaHa) ? areaHa : Number(areaHa.toFixed(1));
  return `≈ ${rounded} ha`;
}

function confidenceLabel(
  confidence: Chapter["confidence"],
): { text: string; className: string } | null {
  if (confidence === "cause_unknown") {
    return { text: "Cause unknown", className: "text-[#C4A574]" };
  }
  if (confidence === "confirmed") {
    return { text: "Confirmed", className: "text-[#5BA8A0]" };
  }
  if (confidence === "likely") {
    return { text: "Likely", className: "text-[#5BA8A0]" };
  }
  return null;
}

function resolveExcerpts(
  claim: Claim,
  sources: Story["sources"],
): ResolvedExcerpt[] {
  const byId = new Map<string, ResolvedExcerpt>();
  for (const source of sources) {
    for (const excerpt of source.excerpts) {
      byId.set(excerpt.id, { excerpt, source });
    }
  }
  return claim.excerpt_ids
    .map((id) => byId.get(id))
    .filter((x): x is ResolvedExcerpt => x != null);
}

function frameForYear(frames: Story["frames"], year: number): Frame | undefined {
  return frames.find((f) => f.year === year);
}

function BeforeAfter({
  change,
  frames,
}: {
  change: Change;
  frames: Story["frames"];
}) {
  const before = frameForYear(frames, change.before_year);
  const after = frameForYear(frames, change.after_year);

  return (
    <div className="mt-4 grid gap-3 sm:grid-cols-2">
      {[
        { label: "Before", year: change.before_year, frame: before },
        { label: "After", year: change.after_year, frame: after },
      ].map(({ label, year, frame }) => (
        <figure key={label} className="min-w-0">
          <div className="overflow-hidden rounded-lg border border-white/[0.08] bg-black/40">
            {frame ? (
              // eslint-disable-next-line @next/next/no-img-element -- remote /files from API
              <img
                src={fileUrl(frame.thumb_url || frame.image_url)}
                alt={`${label} frame ${year}`}
                className="aspect-square w-full object-cover"
              />
            ) : (
              <div className="flex aspect-square items-center justify-center text-sm text-[#7A766E]">
                No frame for {year}
              </div>
            )}
          </div>
          <figcaption className="mt-2 font-ui text-sm text-[#A8A49C]">
            <span className="text-[#F4F1EC]">{label}</span>
            <span className="mx-2 text-white/20">·</span>
            {year}
            {change.area_ha > 0 ? (
              <>
                <span className="mx-2 text-white/20">·</span>
                <span
                  className={
                    change.scope === "local" ? "text-[#C4A574]" : "text-[#A8A49C]"
                  }
                >
                  {formatHa(change.area_ha)}
                  {change.scope === "local" ? " local" : ""}
                </span>
              </>
            ) : null}
            {frame ? (
              <span className="mt-0.5 block em-chrome-label text-[#7A766E]">
                Acquired {frame.date_from}
                {frame.date_to !== frame.date_from ? `–${frame.date_to}` : ""}
                {frame.clear_pct != null ? ` · ${Math.round(frame.clear_pct)}% clear` : ""}
              </span>
            ) : null}
          </figcaption>
        </figure>
      ))}
    </div>
  );
}

function ChangeChapterBlock({
  chapter,
  change,
  frames,
  sources,
  openClaimId,
  onToggleClaim,
}: {
  chapter: Chapter;
  change: Change | undefined;
  frames: Story["frames"];
  sources: Story["sources"];
  openClaimId: string | null;
  onToggleClaim: (id: string) => void;
}) {
  const conf = confidenceLabel(chapter.confidence);
  const isUnknown = chapter.confidence === "cause_unknown";

  return (
    <article className="py-6">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="font-ui text-lg font-semibold text-[#F4F1EC]">
          {chapter.headline}
        </h3>
        <span className="font-label text-[12px] tabular-nums text-[#7A766E]">
          {chapter.start_year}–{chapter.end_year}
        </span>
        {conf ? (
          <span
            className={clsx(
              "text-[12px]",
              conf.className,
            )}
          >
            {conf.text}
          </span>
        ) : null}
      </header>

      {change?.sensor_switch_flag ? (
        <p
          className="mt-3 border-l-2 border-[#C4A574]/60 pl-3 text-sm text-[#C4A574]"
          role="status"
        >
          Sensor switch near this change — may be an artefact.
        </p>
      ) : null}

      {change ? <BeforeAfter change={change} frames={frames} /> : null}

      {change?.mask_url ? (
        <p className="mt-3 em-chrome-label text-[#7A766E]">
          Change mask available
          <span className="mx-2 text-white/20">·</span>
          <a
            href={fileUrl(change.mask_url)}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[#5BA8A0] underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
          >
            Open mask overlay
          </a>
          {change.region.description ? (
            <span className="mt-1 block normal-case tracking-normal font-ui text-sm text-[#A8A49C]">
              Region: {change.region.description}
            </span>
          ) : null}
        </p>
      ) : change?.region.description ? (
        <p className="mt-3 text-sm text-[#A8A49C]">
          Region: {change.region.description}
          {change.scope === "local" ? (
            <span className="ml-2 text-[#C4A574]">
              ({formatHa(change.area_ha)})
            </span>
          ) : null}
        </p>
      ) : null}

      {isUnknown ? (
        <p className="mt-4 text-sm text-[#A8A49C]">
          No verified cause found for this change. Claims are empty by design.
        </p>
      ) : (
        <ul className="mt-4 divide-y divide-white/[0.06] border-t border-white/[0.06]">
          {chapter.claims.map((claim) => {
            const open = openClaimId === claim.id;
            const resolved = resolveExcerpts(claim, sources);
            return (
              <li key={claim.id} className="py-3">
                <button
                  type="button"
                  aria-expanded={open}
                  onClick={() => onToggleClaim(claim.id)}
                  className="w-full text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
                >
                  <span className="font-ui text-[15px] leading-snug text-[#F4F1EC]">
                    {claim.text}
                  </span>
                  <span className="mt-1 block em-chrome-label text-[#5BA8A0]">
                    {open ? "Hide sources" : "Show sources & excerpts"}
                  </span>
                </button>
                {open ? (
                  <div className="mt-3 space-y-3 pl-0 sm:pl-3">
                    {resolved.length === 0 ? (
                      <p className="text-sm text-[#A8A49C]">
                        No matching excerpts for these ids.
                      </p>
                    ) : (
                      resolved.map(({ excerpt, source }) => (
                        <blockquote
                          key={excerpt.id}
                          className="border-l-2 border-[#5BA8A0]/40 pl-3"
                        >
                          <p className="text-sm leading-relaxed text-[#F4F1EC]">
                            “{excerpt.text}”
                          </p>
                          <footer className="mt-2 text-sm text-[#A8A49C]">
                            <a
                              href={source.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-[#5BA8A0] underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]"
                            >
                              {source.title}
                            </a>
                            {source.publisher ? (
                              <>
                                <span className="mx-2 text-white/20">·</span>
                                {source.publisher}
                              </>
                            ) : null}
                            <span className="mx-2 text-white/20">·</span>
                            {source.domain}
                            <span className="mx-2 text-white/20">·</span>
                            {source.authority}
                            {source.published_date ? (
                              <>
                                <span className="mx-2 text-white/20">·</span>
                                {source.published_date}
                              </>
                            ) : null}
                          </footer>
                        </blockquote>
                      ))
                    )}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      {chapter.vision_check.verdict !== "not_run" ||
      chapter.vision_check.note ? (
        <p className="mt-4 em-chrome-label text-[#7A766E]">
          Vision check: {chapter.vision_check.verdict}
          {chapter.vision_check.note ? (
            <span className="mt-1 block normal-case tracking-normal font-ui text-sm text-[#A8A49C]">
              {chapter.vision_check.note}
            </span>
          ) : null}
        </p>
      ) : null}
    </article>
  );
}

function QuietChapterStrip({ chapter }: { chapter: Chapter }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-4">
      <span className="em-chrome-label capitalize text-[#7A766E]">
        {chapter.kind}
      </span>
      <span className="font-ui text-[15px] font-semibold text-[#F4F1EC]">
        {chapter.headline}
      </span>
      <span className="font-label text-[12px] tabular-nums text-[#7A766E]">
        {chapter.start_year}–{chapter.end_year}
      </span>
    </div>
  );
}

export function EvidencePanel({ story, className }: EvidencePanelProps) {
  const [openClaimId, setOpenClaimId] = useState<string | null>(null);
  const changeById = new Map(story.changes.map((c) => [c.id, c]));

  const onToggleClaim = (id: string) => {
    setOpenClaimId((prev) => (prev === id ? null : id));
  };

  return (
    <section
      className={clsx("relative z-10 mt-12", className)}
      aria-label="Evidence"
    >
      <h2 className="em-chrome-label text-[#7A766E]">
        Evidence
      </h2>
      <p className="mt-2 max-w-2xl text-sm text-[#A8A49C]">
        Every claim links to a verified excerpt. Click a claim to open its
        sources.
      </p>

      <div className="mt-4 divide-y divide-white/[0.06] border-y border-white/[0.06]">
        {story.chapters.map((chapter) => {
          if (chapter.kind !== "change") {
            return <QuietChapterStrip key={chapter.id} chapter={chapter} />;
          }
          const change = chapter.change_id
            ? changeById.get(chapter.change_id)
            : undefined;
          return (
            <ChangeChapterBlock
              key={chapter.id}
              chapter={chapter}
              change={change}
              frames={story.frames}
              sources={story.sources}
              openClaimId={openClaimId}
              onToggleClaim={onToggleClaim}
            />
          );
        })}
      </div>
    </section>
  );
}
