/**
 * Whole-AOI metric series chart with place-scope change annotations (FR-52).
 * Inputs: metrics.series, changes, optional activeYear / highlightedChangeId.
 * Outputs: SVG chart + metric toggle; local-scope changes shown as ha notes only.
 */

"use client";

import { useState } from "react";
import clsx from "clsx";
import type { Story } from "@/lib/types/story";

type MetricKey = "ndvi_mean" | "water_frac" | "built_frac";
type Change = Story["changes"][number];
type SeriesPoint = Story["metrics"]["series"][number];
type Skipped = Story["coverage"]["years_skipped"][number];

const METRIC_LABELS: Record<MetricKey, string> = {
  ndvi_mean: "NDVI (land)",
  water_frac: "Water fraction",
  built_frac: "Built fraction",
};

const METRIC_KEYS: MetricKey[] = ["ndvi_mean", "water_frac", "built_frac"];

export type MetricChartProps = {
  series: SeriesPoint[];
  changes: Story["changes"];
  yearsSkipped?: Skipped[];
  activeYear?: number;
  highlightedChangeId?: string;
  className?: string;
};

function defaultMetric(changes: Story["changes"]): MetricKey {
  const place = changes.find((c) => c.scope === "place");
  return place?.metric ?? "water_frac";
}

function formatMag(metric: MetricKey, value: number): string {
  const sign = value > 0 ? "+" : "";
  if (metric === "ndvi_mean") return `${sign}${value.toFixed(2)}`;
  return `${sign}${(value * 100).toFixed(0)} pp`;
}

function formatHa(areaHa: number): string {
  if (areaHa >= 100) {
    return `≈ ${Math.round(areaHa).toLocaleString("en-US")} ha`;
  }
  const rounded = Number.isInteger(areaHa) ? areaHa : Number(areaHa.toFixed(1));
  return `≈ ${rounded} ha`;
}

export function MetricChart({
  series,
  changes,
  yearsSkipped = [],
  activeYear,
  highlightedChangeId,
  className,
}: MetricChartProps) {
  const [metric, setMetric] = useState<MetricKey>(() => defaultMetric(changes));

  if (!series.length) {
    return (
      <section
        className={clsx("relative z-10 mt-10", className)}
        aria-label="Metrics"
      >
        <h2 className="em-chrome-label text-[#7A766E]">Metrics</h2>
        <p className="mt-3 text-sm text-[#A8A49C]">No metric series for this film.</p>
      </section>
    );
  }

  const years = series.map((p) => p.year);
  const values = series.map((p) => p[metric]);
  const minYear = years[0]!;
  const maxYear = years[years.length - 1]!;
  const yearSpan = Math.max(maxYear - minYear, 1);

  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const pad = Math.max((rawMax - rawMin) * 0.12, 0.02);
  const yMin = rawMin - pad;
  const yMax = rawMax + pad;
  const ySpan = Math.max(yMax - yMin, 0.01);

  const W = 640;
  const H = 220;
  const padL = 48;
  const padR = 16;
  const padT = 16;
  const padB = 32;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  const xOf = (year: number) =>
    padL + ((year - minYear) / yearSpan) * innerW;
  const yOf = (v: number) => padT + ((yMax - v) / ySpan) * innerH;

  const points = series
    .map((p) => `${xOf(p.year).toFixed(1)},${yOf(p[metric]).toFixed(1)}`)
    .join(" ");

  const placeChanges = changes.filter(
    (c): c is Change => c.scope === "place" && c.metric === metric,
  );
  const localChanges = changes.filter((c) => c.scope === "local");

  const tickYears = [minYear, maxYear];
  if (years.length > 2) {
    const mid = years[Math.floor(years.length / 2)]!;
    if (mid !== minYear && mid !== maxYear) tickYears.splice(1, 0, mid);
  }

  return (
    <section
      className={clsx("relative z-10 mt-10", className)}
      aria-label="Metrics over time"
    >
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h2 className="em-chrome-label text-[#7A766E]">Whole-area metrics</h2>
        <div
          className="flex flex-wrap gap-1"
          role="group"
          aria-label="Metric to chart"
        >
          {METRIC_KEYS.map((key) => (
            <button
              key={key}
              type="button"
              aria-pressed={metric === key}
              onClick={() => setMetric(key)}
              className={clsx(
                "min-h-9 rounded-full border px-3 text-[12px] transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5BA8A0]",
                metric === key
                  ? "border-[#5BA8A0]/60 bg-[#5BA8A0]/15 text-[#F4F1EC]"
                  : "border-white/10 text-[#A8A49C] hover:border-white/25 hover:text-[#F4F1EC]",
              )}
            >
              {METRIC_LABELS[key]}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-4 border-t border-white/[0.06] pt-4">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          className="h-auto w-full"
          role="img"
          aria-label={`${METRIC_LABELS[metric]} from ${minYear} to ${maxYear}`}
        >
          {/* skipped-year marks */}
          {yearsSkipped.map((skip) => {
            if (skip.year < minYear || skip.year > maxYear) return null;
            const x = xOf(skip.year);
            return (
              <g key={`skip-${skip.year}`}>
                <line
                  x1={x}
                  y1={padT}
                  x2={x}
                  y2={padT + innerH}
                  stroke="rgba(196,165,116,0.35)"
                  strokeWidth={1}
                  strokeDasharray="3 4"
                >
                  <title>
                    {skip.year} skipped: {skip.reason}
                  </title>
                </line>
              </g>
            );
          })}

          {/* place-scope year-range bands */}
          {placeChanges.map((chg) => {
            const x1 = xOf(Math.max(chg.start_year, minYear));
            const x2 = xOf(Math.min(chg.end_year, maxYear));
            const active =
              highlightedChangeId === chg.id ||
              (activeYear != null &&
                activeYear >= chg.start_year &&
                activeYear <= chg.end_year);
            return (
              <rect
                key={`band-${chg.id}`}
                x={Math.min(x1, x2)}
                y={padT}
                width={Math.max(Math.abs(x2 - x1), 2)}
                height={innerH}
                fill={
                  active ? "rgba(91,168,160,0.16)" : "rgba(91,168,160,0.08)"
                }
              >
                <title>
                  {chg.id}: {formatMag(chg.metric, chg.magnitude)} (
                  {chg.start_year}–{chg.end_year})
                </title>
              </rect>
            );
          })}

          {/* baseline grid */}
          <line
            x1={padL}
            y1={padT + innerH}
            x2={padL + innerW}
            y2={padT + innerH}
            stroke="rgba(255,255,255,0.12)"
            strokeWidth={1}
          />
          <line
            x1={padL}
            y1={padT}
            x2={padL}
            y2={padT + innerH}
            stroke="rgba(255,255,255,0.12)"
            strokeWidth={1}
          />

          <polyline
            fill="none"
            stroke="#5BA8A0"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
            points={points}
          />

          {series.map((p) => (
            <circle
              key={p.year}
              cx={xOf(p.year)}
              cy={yOf(p[metric])}
              r={activeYear === p.year ? 4.5 : 3}
              fill={activeYear === p.year ? "#F4F1EC" : "#5BA8A0"}
            >
              <title>
                {p.year}: {p[metric].toFixed(3)}
              </title>
            </circle>
          ))}

          {/* active year marker */}
          {activeYear != null &&
          activeYear >= minYear &&
          activeYear <= maxYear ? (
            <line
              x1={xOf(activeYear)}
              y1={padT}
              x2={xOf(activeYear)}
              y2={padT + innerH}
              stroke="#5BA8A0"
              strokeWidth={1.5}
              opacity={0.85}
            />
          ) : null}

          {/* y ticks */}
          <text
            x={padL - 8}
            y={padT + 4}
            textAnchor="end"
            className="fill-[#7A766E]"
            fontSize={10}
            fontFamily="var(--font-label), monospace"
          >
            {yMax.toFixed(2)}
          </text>
          <text
            x={padL - 8}
            y={padT + innerH}
            textAnchor="end"
            className="fill-[#7A766E]"
            fontSize={10}
            fontFamily="var(--font-label), monospace"
          >
            {yMin.toFixed(2)}
          </text>

          {tickYears.map((y) => (
            <text
              key={`tick-${y}`}
              x={xOf(y)}
              y={H - 8}
              textAnchor="middle"
              className="fill-[#7A766E]"
              fontSize={10}
              fontFamily="var(--font-label), monospace"
            >
              {y}
            </text>
          ))}
        </svg>

        {placeChanges.length > 0 ? (
          <ul className="mt-3 space-y-1.5">
            {placeChanges.map((chg) => (
              <li
                key={chg.id}
                className={clsx(
                  "font-ui text-sm text-[#A8A49C]",
                  highlightedChangeId === chg.id && "text-[#5BA8A0]",
                )}
              >
                <span className="text-[#F4F1EC]">
                  {chg.start_year}–{chg.end_year}
                </span>
                <span className="mx-2 text-white/20">·</span>
                <span className="text-[#5BA8A0]">
                  {formatMag(chg.metric, chg.magnitude)}
                </span>
                <span className="mx-2 text-white/20">·</span>
                place scope
                {chg.region.description ? (
                  <>
                    <span className="mx-2 text-white/20">·</span>
                    {chg.region.description}
                  </>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}

        {localChanges.length > 0 ? (
          <ul className="mt-3 space-y-1.5 border-t border-white/[0.06] pt-3">
            {localChanges.map((chg) => (
              <li key={chg.id} className="font-ui text-sm text-[#A8A49C]">
                <span className="text-[#C4A574]">{formatHa(chg.area_ha)}</span>
                <span className="mx-2 text-white/20">·</span>
                local change {chg.start_year}–{chg.end_year}
                {chg.region.description ? (
                  <>
                    <span className="mx-2 text-white/20">·</span>
                    {chg.region.description}
                  </>
                ) : null}
                <span className="ml-2 text-[#6e6e73]">
                  (not plotted as whole-area series)
                </span>
              </li>
            ))}
          </ul>
        ) : null}

        {yearsSkipped.length > 0 ? (
          <p className="em-chrome-label mt-3 text-[#7A766E]">
            Dashed marks: skipped years (
            {yearsSkipped.map((s) => s.year).join(", ")})
          </p>
        ) : null}
      </div>
    </section>
  );
}
