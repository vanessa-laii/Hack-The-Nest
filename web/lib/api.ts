/**
 * Typed API client for the Revisit backend.
 * Inputs: NEXT_PUBLIC_API_BASE + optional fetch overrides.
 * Outputs: gallery/story helpers; falls back to placeholder data offline.
 */

import { PLACEHOLDER_PLACES, type PlaceLocation } from "./places";
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

function placeholderGallery(): GalleryItem[] {
  return PLACEHOLDER_PLACES.flatMap((place) =>
    place.stories.map((story) => ({
      id: story.id,
      display_name: place.displayName,
      first_year: story.firstYear,
      last_year: story.lastYear,
      thumb_url: "",
      headline: story.title,
      chapter_count: 1,
      has_narration: false,
    })),
  );
}

export async function getGallery(): Promise<GalleryItem[]> {
  try {
    const res = await fetch(`${API_BASE}/api/stories`, {
      next: { revalidate: 30 },
    });
    if (!res.ok) return placeholderGallery();
    const data = (await res.json()) as { stories: GalleryItem[] };
    if (!data.stories?.length) return placeholderGallery();
    return data.stories;
  } catch {
    return placeholderGallery();
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
 * Merge gallery API stories onto globe places when ids/coords match.
 * Ensures sample-lake-6km from EM_MOCK=1 is openable from the landing.
 */
export async function getPlacesForGlobe(): Promise<PlaceLocation[]> {
  const gallery = await getGallery();
  const byId = new Map(gallery.map((g) => [g.id, g]));

  const merged = PLACEHOLDER_PLACES.map((place) => {
    const stories = place.stories.map((stub) => {
      const g = byId.get(stub.id);
      if (!g) return stub;
      byId.delete(stub.id);
      return {
        ...stub,
        title: g.headline || stub.title,
        firstYear: g.first_year,
        lastYear: g.last_year,
        blurb: stub.blurb,
      };
    });
    return { ...place, stories };
  });

  // Gallery-only items (e.g. if placeholders diverge): attach as extra markers.
  for (const g of byId.values()) {
    // Sample lake shares Lake Mead coords when the placeholder was already merged.
    const lat = g.id === "sample-lake-6km" ? 36.25 : 0;
    const lon = g.id === "sample-lake-6km" ? -114.39 : 0;
    if (lat === 0 && lon === 0) continue;
    const existing = merged.find(
      (p) => Math.abs(p.lat - lat) < 0.01 && Math.abs(p.lon - lon) < 0.01,
    );
    if (existing) {
      if (!existing.stories.some((s) => s.id === g.id)) {
        existing.stories = [
          {
            id: g.id,
            title: g.headline,
            firstYear: g.first_year,
            lastYear: g.last_year,
            blurb: `[SAMPLE] From gallery API · ${g.display_name}`,
            theme: existing.theme,
          },
          ...existing.stories,
        ];
        existing.displayName = g.display_name;
      }
      continue;
    }
    merged.unshift({
      id: `gallery-${g.id}`,
      displayName: g.display_name,
      lat,
      lon,
      theme: "From gallery",
      stories: [
        {
          id: g.id,
          title: g.headline,
          firstYear: g.first_year,
          lastYear: g.last_year,
          blurb: `[SAMPLE] Gallery story · ${g.chapter_count} chapters`,
          theme: "From gallery",
        },
      ],
    });
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
