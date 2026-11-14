/**
 * Client-only dynamic wrapper for the R3F globe (no SSR).
 * Inputs: places + selection callbacks.
 * Outputs: lazily loaded full-bleed Globe canvas.
 */

"use client";

import dynamic from "next/dynamic";
import type { PlaceLocation } from "@/lib/places";

const Globe = dynamic(() => import("./Globe"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full w-full items-center justify-center bg-[#121214]">
      <div className="h-24 w-24 animate-pulse rounded-full border border-[#2997ff]/25 bg-[#2997ff]/10 shadow-[0_0_40px_rgba(41,151,255,0.25)]" />
    </div>
  ),
});

type GlobeCanvasProps = {
  places: PlaceLocation[];
  selectedPlaceId: string | null;
  onSelectPlace: (id: string) => void;
  reducedMotion?: boolean;
  className?: string;
};

export function GlobeCanvas(props: GlobeCanvasProps) {
  return <Globe {...props} />;
}
