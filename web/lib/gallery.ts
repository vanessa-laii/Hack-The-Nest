/**
 * Cached homepage gallery → globe place merge (server-only).
 * Inputs: DATA_DIR stories/index.json (+ optional API); Outputs: PlaceLocation[].
 */

import { cacheLife, cacheTag } from "next/cache";
import { API_BASE, fileUrl, type GalleryItem } from "./api";
import {
  readGalleryFromDisk,
  storyExistsOnDisk,
} from "./localStories";
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

async function loadGallery(): Promise<GalleryItem[]> {
  const fromDisk = await readGalleryFromDisk();
  if (fromDisk.length) return fromDisk;
  if (!API_BASE) return [];
  try {
    const res = await fetch(`${API_BASE}/api/stories`);
    if (!res.ok) return [];
    const data = (await res.json()) as { stories: GalleryItem[] };
    return data.stories?.length ? data.stories : [];
  } catch {
    return [];
  }
}

/**
 * Merge gallery onto seed globe places by story id or display name.
 * Prefers on-disk DATA_DIR so play-only deploys need no FastAPI.
 */
export async function getPlacesForGlobe(): Promise<PlaceLocation[]> {
  "use cache";
  cacheTag("gallery");
  cacheLife("minutes");

  const gallery = await loadGallery();
  const byId = new Map(gallery.map((g) => [g.id, g]));

  const missingIds = SEED_PLACES.flatMap((p) =>
    p.stories.map((s) => s.id).filter((id) => !byId.has(id)),
  );
  const existence = await Promise.all(
    missingIds.map(async (id) => {
      if (await storyExistsOnDisk(id)) return [id, true] as const;
      if (!API_BASE) return [id, false] as const;
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
    if (byName.stories.some((s) => s.id === g.id)) {
      byName.stories = byName.stories.map((s) =>
        s.id === g.id
          ? {
              ...s,
              ready: true,
              thumbUrl: g.thumb_url ? fileUrl(g.thumb_url) : s.thumbUrl,
            }
          : s,
      );
      continue;
    }
    const only = byName.stories.length === 1 ? byName.stories[0] : null;
    if (only) {
      byName.stories = [galleryToStub(g, byName.theme, only.blurb)];
    } else {
      byName.stories = [
        galleryToStub(g, byName.theme, byName.stories[0]?.blurb),
        ...byName.stories,
      ];
    }
  }

  return merged;
}
