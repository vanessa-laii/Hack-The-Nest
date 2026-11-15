/**
 * Typed API client for the Revisit backend.
 * Inputs: NEXT_PUBLIC_API_BASE + optional fetch overrides.
 * Outputs: gallery/story helpers; globe merges API gallery onto SEED_PLACES.
 */

import { SEED_PLACES, type PlaceLocation, type PlaceStoryStub } from "./places";
import type { Story } from "./types/story";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") ||
  "http://localhost:8000";

export type GalleryItem = {
  id: string;
  display_name: string;
  first_year: number;
  last_year: number;
  thumb_url: string;
  headline: string;
  chapter_count: number;
  has_narration: boolean;
};

export type ApiErrorCode =
  | "INVALID_REQUEST"
  | "PLACE_NOT_FOUND"
  | "PLACE_AMBIGUOUS"
  | "NO_LAND_DATA"
  | "NOT_READY"
  | "TIMEOUT"
  | "UPSTREAM_ERROR"
  | "INTERNAL";

export type ApiError = {
  error: {
    code: ApiErrorCode;
    message: string;
    candidates?: Array<{
      display_name: string;
      lat: number;
      lon: number;
      kind: string;
    }>;
    partial_story_url?: string;
  };
};

export type CreateStoryBody = {
  query: string;
  lat?: number;
  lon?: number;
  display_name?: string;
  aoi_km?: number;
};

export type CreateStoryResponse = {
  id: string;
  cached: boolean;
  events_url?: string;
  story_url: string;
};

export function isApiError(
  value: CreateStoryResponse | ApiError,
): value is ApiError {
  return value != null && typeof value === "object" && "error" in value;
}

export function eventsUrl(storyId: string): string {
  return `${API_BASE}/api/stories/${storyId}/events`;
}

export function fileUrl(path: string): string {
  if (!path) return "";
  if (path.startsWith("http://") || path.startsWith("https://")) return path;
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${normalized}`;
}

export function friendlyErrorMessage(code: ApiErrorCode): string {
  switch (code) {
    case "INVALID_REQUEST":
      return "Something went wrong with that request.";
    case "PLACE_NOT_FOUND":
      return "We couldn't find that place. Try adding the state or country.";
    case "PLACE_AMBIGUOUS":
      return "Several places match. Pick one to continue.";
    case "NO_LAND_DATA":
      return "Revisit works on land. Try a nearby town.";
    case "NOT_READY":
      return "This story is still building.";
    case "TIMEOUT":
    case "UPSTREAM_ERROR":
    case "INTERNAL":
      return "We hit a snag building that story. Try a cached place from the globe.";
    default:
      return "Something unexpected happened.";
  }
}

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
    blurb: blurb ?? `${g.chapter_count} chapter${g.chapter_count === 1 ? "" : "s"} · from gallery`,
    theme,
    ready: true,
    thumbUrl: g.thumb_url ? fileUrl(g.thumb_url) : undefined,
  };
}

export async function getGallery(): Promise<GalleryItem[]> {
  try {
    // no-store: newly built stories must show as ready on the next homepage load
    // (revalidate: 30 kept caching an empty gallery after Lake Mead finished).
    const res = await fetch(`${API_BASE}/api/stories`, {
      cache: "no-store",
    });
    if (!res.ok) return [];
    const data = (await res.json()) as { stories: GalleryItem[] };
    return data.stories?.length ? data.stories : [];
  } catch {
    return [];
  }
}

/** True when the backend has a playable story.json for this id. */
export async function storyExists(id: string): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/api/stories/${encodeURIComponent(id)}`, {
      method: "GET",
      cache: "no-store",
    });
    return res.ok;
  } catch {
    return false;
  }
}

export async function getStory(id: string): Promise<Story | null> {
  try {
    const res = await fetch(`${API_BASE}/api/stories/${id}`, {
      cache: "no-store",
    });
    if (!res.ok) return null;
    return (await res.json()) as Story;
  } catch {
    return null;
  }
}

/**
 * Merge GET /api/stories onto seed globe places by story id, display name, or proximity.
 * Seeds always show; gallery marks which films are ready. Never drops pins at 0,0.
 */
export async function getPlacesForGlobe(): Promise<PlaceLocation[]> {
  const gallery = await getGallery();
  const byId = new Map(gallery.map((g) => [g.id, g]));

  // If index is empty/stale but story.json exists, still mark seed films ready.
  const missingIds = SEED_PLACES.flatMap((p) =>
    p.stories.map((s) => s.id).filter((id) => !byId.has(id)),
  );
  const existence = await Promise.all(
    missingIds.map(async (id) => [id, await storyExists(id)] as const),
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

  // Remaining gallery rows: attach only when display_name matches a seed (known lat/lon).
  // Never invent 0,0 pins for unknown gallery ids.
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

export async function createStory(
  body: CreateStoryBody,
): Promise<CreateStoryResponse | ApiError> {
  const res = await fetch(`${API_BASE}/api/stories`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) return data as ApiError;
  return data as CreateStoryResponse;
}
