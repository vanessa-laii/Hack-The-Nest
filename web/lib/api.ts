/**
 * Typed API client for the Earth Memory backend.
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
      return "Earth Memory works on land. Try a nearby town.";
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

export async function getPlacesForGlobe(): Promise<PlaceLocation[]> {
  // Phase 0: offline-friendly placeholder markers. Later: merge with gallery API.
  return PLACEHOLDER_PLACES;
}

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
