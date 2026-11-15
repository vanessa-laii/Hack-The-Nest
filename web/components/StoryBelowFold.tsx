/**
 * Client shell under the film: live year → MetricChart + EvidencePanel.
 * Inputs: story; receives onYearChange from StoryFilm via StoryMain.
 * Outputs: chart + evidence synced to the playing frame year.
 */

"use client";

import { useState } from "react";
import { EvidencePanel } from "@/components/EvidencePanel";
import { MetricChart } from "@/components/MetricChart";
import { StoryFilm } from "@/components/StoryFilm";
import type { Story } from "@/lib/types/story";

export type StoryBelowFoldProps = {
  story: Story;
};

export function StoryBelowFold({ story }: StoryBelowFoldProps) {
  const [activeYear, setActiveYear] = useState<number | undefined>();

  return (
    <>
      <StoryFilm story={story} onYearChange={setActiveYear} />
      <MetricChart
        series={story.metrics.series}
        changes={story.changes}
        yearsSkipped={story.coverage.years_skipped}
        activeYear={activeYear}
      />
      <EvidencePanel story={story} />
    </>
  );
}
