/**
 * Cached homepage gallery → globe place merge (server-only).
 * Inputs: GET /api/stories + SEED_PLACES; Outputs: PlaceLocation[] for LandingHero.
 */

import { cacheLife, cacheTag } from "next/cache";
import { API_BASE, fileUrl, type GalleryItem } from "./api";
import { SEED_PLACES, type PlaceLocation, type PlaceStoryStub } from "./places";

function normalizeName(name: string): string {
  return name.trim().toLowerCase().replace(/\s+/g, " ");
}

function namesMatch(a: string, b: string): boolean {
  const na = normalizeName(a);
  const nb = normalizeName(b);
  return na === nb || na.includes(nb) || nb.includes(na);
}

function galleryToStub(
  g: GalleryItem,
  theme: string,
  blurb?: string,
): PlaceStoryStub {
  return {
    id: g.id,
    title: g.headline || g.display_name,
    firstYear: g.first_year,
    lastYear: g.last_year,
    blurb:
      blurb ??
      `${g.chapter_count} chapter${g.chapter_count === 1 ? "" : "s"} · from gallery`,
    theme,
    ready: true,
    thumbUrl: g.thumb_url ? fileUrl(g.thumb_url) : undefined,
  };
}

/**
 * Merge GET /api/stories onto seed globe places by story id, display name, or proximity.
 * Seeds always show; gallery marks which films are ready. Never drops pins at 0,0.
 */
export async function getPlacesForGlobe(): Promise<PlaceLocation[]> {
  "use cache";
  cacheTag("gallery");
  cacheLife("minutes");

  let gallery: GalleryItem[] = [];
  try {
    const res = await fetch(`${API_BASE}/api/stories`);
    if (res.ok) {
      const data = (await res.json()) as { stories: GalleryItem[] };
      gallery = data.stories?.length ? data.stories : [];
    }
  } catch {
    gallery = [];
  }

  const byId = new Map(gallery.map((g) => [g.id, g]));

  const missingIds = SEED_PLACES.flatMap((p) =>
    p.stories.map((s) => s.id).filter((id) => !byId.has(id)),
  );
  const existence = await Promise.all(
    missingIds.map(async (id) => {
      try {
        const res = await fetch(
          `${API_BASE}/api/stories/${encodeURIComponent(id)}`,
          { method: "GET" },
        );
        return [id, res.ok] as const;
      } catch {
        return [id, false] as const;
      }
    }),
  );
  const exists = new Set(existence.filter(([, ok]) => ok).map(([id]) => id));

  const merged: PlaceLocation[] = SEED_PLACES.map((place) => {
    const stories = place.stories.map((stub) => {
      const g = byId.get(stub.id);
      if (g) {
        byId.delete(stub.id);
        return {
          ...stub,
          title: g.headline || stub.title,
          firstYear: g.first_year,
          lastYear: g.last_year,
          blurb: stub.blurb,
          ready: true,
          thumbUrl: g.thumb_url ? fileUrl(g.thumb_url) : stub.thumbUrl,
        };
      }
      if (exists.has(stub.id)) {
        return { ...stub, ready: true };
      }
      return { ...stub, ready: false };
    });
    return { ...place, stories };
  });

  for (const g of byId.values()) {
    const byName = merged.find((p) =>
      namesMatch(p.displayName, g.display_name),
    );
    if (!byName) continue;
    if (!byName.stories.some((s) => s.id === g.id)) {
      byName.stories = [
        galleryToStub(g, byName.theme, byName.stories[0]?.blurb),
        ...byName.stories,
      ];
    } else {
      byName.stories = byName.stories.map((s) =>
        s.id === g.id
          ? {
              ...s,
              ready: true,
              thumbUrl: g.thumb_url ? fileUrl(g.thumb_url) : s.thumbUrl,
            }
          : s,
      );
    }
  }

  return merged;
}
