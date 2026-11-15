/**
 * Full-bleed Revisit landing: globe is the page; copy floats like the inspiration layout.
 * Inputs: seed gallery places list.
 * Outputs: cinematic interactive first viewport.
 */

"use client";

import { useEffect, useMemo, useState } from "react";
import { SEED_PLACES, type PlaceLocation } from "@/lib/places";
import { GlobeCanvas } from "./GlobeCanvas";
import { PlacePicker } from "./PlacePicker";
import { PlaceVideoPanel } from "./PlaceVideoPanel";
import { SearchBox } from "./SearchBox";

type LandingHeroProps = {
  places?: PlaceLocation[];
};

export function LandingHero({ places = SEED_PLACES }: LandingHeroProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [reducedMotion, setReducedMotion] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => setReducedMotion(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  const selected = useMemo(
    () => places.find((p) => p.id === selectedId) ?? null,
    [places, selectedId],
  );

  return (
    <section className="relative h-full min-h-0 flex-1 overflow-hidden bg-[#121214]">
      {/* No radial vignette — open charcoal; only faint corner wash + grain */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 z-[1] bg-[linear-gradient(to_bottom,rgba(18,18,20,0.2)_0%,transparent_18%,transparent_82%,rgba(18,18,20,0.28)_100%)]"
      />
      <div aria-hidden className="grain pointer-events-none absolute inset-0 z-[1] opacity-50" />

      {/* Full-page globe */}
      <div className="absolute inset-0 z-0">
        <GlobeCanvas
          places={places}
          selectedPlaceId={selectedId}
          onSelectPlace={setSelectedId}
          reducedMotion={reducedMotion}
          className="h-full w-full"
        />
      </div>

      {/* Top-left display copy — no header/nav, no mono eyebrow */}
      <div className="pointer-events-none absolute left-5 top-10 z-20 max-w-[15rem] sm:left-10 sm:top-[16%] sm:max-w-xs lg:left-14 lg:max-w-sm">
        <h1 className="font-display text-5xl font-medium leading-[0.95] tracking-tight text-[#f5f5f7] sm:text-6xl lg:text-7xl">
          Revisit
        </h1>
        <p className="mt-5 italic hidden max-w-[16rem] text-sm leading-relaxed text-[#a1a1a6] sm:block">
          videos around the world across the decades.
        </p>
      </div>

      {/* Place picker + selected-place card */}
      <aside
        id="explore"
        className="pointer-events-none absolute inset-x-4 bottom-8 z-20 md:inset-x-auto md:bottom-auto md:right-8 md:top-1/2 md:w-[22rem] md:-translate-y-1/2 lg:right-12"
      >
        <div className="pointer-events-auto flex flex-col gap-3 md:gap-4">
          <PlacePicker
            places={places}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />

          {selected ? (
            <div id="films">
              <PlaceVideoPanel
                place={selected}
                onClose={() => setSelectedId(null)}
              />
            </div>
          ) : null}
        </div>
      </aside>
    </section>
  );
}
