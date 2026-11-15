/**
 * Seed gallery places for the landing globe (real catalog ids).
 * Inputs: none (static seed from spikes/s3-changes/places.json + eval/gold_places.yaml).
 * Outputs: PlaceLocation[] with lat/lon and story stubs keyed by real story_id.
 */

export type PlaceStoryStub = {
  id: string;
  title: string;
  firstYear: number;
  lastYear: number;
  blurb: string;
  theme: string;
  /** True when GET /api/stories lists this id (film may exist on disk). */
  ready?: boolean;
  /** Latest Landsat thumb from gallery (`GalleryItem.thumb_url`), via fileUrl. */
  thumbUrl?: string;
};

export type PlaceLocation = {
  id: string;
  displayName: string;
  lat: number;
  lon: number;
  theme: string;
  stories: PlaceStoryStub[];
};

export const SEED_PLACES: PlaceLocation[] = [
  {
    id: "lake-mead",
    displayName: "Lake Mead, Nevada",
    lat: 36.25,
    lon: -114.39,
    theme: "Water change",
    stories: [
      {
        id: "lake-mead-nevada-us-6km",
        title: "Lake Mead, Nevada",
        firstYear: 1984,
        lastYear: 2024,
        blurb: "Water change · drought / reservoir levels",
        theme: "Water change",
        ready: false,
      },
    ],
  },
  {
    id: "paradise",
    displayName: "Paradise, California",
    lat: 39.76,
    lon: -121.62,
    theme: "Wildfire",
    stories: [
      {
        id: "paradise-california-us-6km",
        title: "Paradise, California",
        firstYear: 1984,
        lastYear: 2024,
        blurb: "Wildfire · burn / landscape recovery",
        theme: "Wildfire",
        ready: false,
      },
    ],
  },
  {
    id: "summerlin",
    displayName: "Summerlin, Nevada",
    lat: 36.17,
    lon: -115.33,
    theme: "Urban vegetation",
    stories: [
      {
        id: "summerlin-south-nevada-us-6km",
        title: "Summerlin, Nevada",
        firstYear: 1984,
        lastYear: 2024,
        blurb: "Urban vegetation · master-planned community growth",
        theme: "Urban vegetation",
        ready: false,
      },
    ],
  },
  {
    id: "palm-jumeirah",
    displayName: "Palm Jumeirah, Dubai",
    lat: 25.11,
    lon: 55.14,
    theme: "Land reclamation",
    stories: [
      {
        id: "palm-jumeirah-united-arab-emirates-ae-6km",
        title: "Palm Jumeirah, Dubai",
        firstYear: 1984,
        lastYear: 2024,
        blurb: "Land reclamation · water to land",
        theme: "Land reclamation",
        ready: false,
      },
    ],
  },
  // Central Park + Ypsilanti omitted: NO_LASTING_CHANGES — nothing meaningful to show.
  {
    id: "willow-run",
    displayName: "Willow Run, Ypsilanti Township, Michigan",
    lat: 42.2424,
    lon: -83.5552,
    theme: "Demolition · test track",
    stories: [
      {
        id: "willow-run-michigan-us-6km",
        title: "Willow Run, Michigan",
        firstYear: 1984,
        lastYear: 2024,
        blurb: "Demolition · plant cleared, then AV test track",
        theme: "Demolition · test track",
        ready: false,
      },
    ],
  },
  {
    id: "delray",
    displayName: "Delray, Detroit, Michigan",
    lat: 42.289,
    lon: -83.112,
    theme: "Bridge",
    stories: [
      {
        id: "delray-detroit-michigan-us-6km",
        title: "Delray, Detroit, Michigan",
        firstYear: 1984,
        lastYear: 2026,
        blurb: "Bridge · Gordie Howe International Bridge construction",
        theme: "Bridge",
        ready: false,
      },
    ],
  },
];

/** Split "City, Region" for elegant typography (city display / region muted). */
export function splitPlaceName(displayName: string): {
  primary: string;
  secondary: string | null;
} {
  const idx = displayName.indexOf(",");
  if (idx === -1) return { primary: displayName, secondary: null };
  return {
    primary: displayName.slice(0, idx).trim(),
    secondary: displayName.slice(idx + 1).trim() || null,
  };
}

export function getPlaceById(id: string): PlaceLocation | undefined {
  return SEED_PLACES.find((p) => p.id === id);
}

export function getStoryStub(storyId: string): {
  place: PlaceLocation;
  story: PlaceStoryStub;
} | undefined {
  for (const place of SEED_PLACES) {
    const story = place.stories.find((s) => s.id === storyId);
    if (story) return { place, story };
  }
  return undefined;
}
