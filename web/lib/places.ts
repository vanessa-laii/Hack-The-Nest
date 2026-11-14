/**
 * Placeholder gallery places for the landing globe.
 * Inputs: none (static seed data).
 * Outputs: PlaceLocation[] with lat/lon and sample story stubs.
 */

export type PlaceStoryStub = {
  id: string;
  title: string;
  firstYear: number;
  lastYear: number;
  blurb: string;
  theme: string;
};

export type PlaceLocation = {
  id: string;
  displayName: string;
  lat: number;
  lon: number;
  theme: string;
  stories: PlaceStoryStub[];
};

export const PLACEHOLDER_PLACES: PlaceLocation[] = [
  {
    id: "lake-mead",
    displayName: "Lake Mead, Nevada",
    lat: 36.25,
    lon: -114.39,
    theme: "Water change",
    stories: [
      {
        id: "placeholder-lake-mead",
        title: "Lake Mead shoreline",
        firstYear: 1984,
        lastYear: 2024,
        blurb:
          "[SAMPLE] Placeholder film slot for reservoir shoreline change visible in Landsat imagery.",
        theme: "Water change",
      },
    ],
  },
  {
    id: "paradise-ca",
    displayName: "Paradise, California",
    lat: 39.76,
    lon: -121.62,
    theme: "Wildfire",
    stories: [
      {
        id: "placeholder-paradise",
        title: "Paradise landscape recovery",
        firstYear: 1984,
        lastYear: 2024,
        blurb:
          "[SAMPLE] Placeholder film slot for post-fire landscape change. Not a verified claim.",
        theme: "Wildfire",
      },
    ],
  },
  {
    id: "summerlin",
    displayName: "Summerlin, Nevada",
    lat: 36.17,
    lon: -115.33,
    theme: "Urban growth",
    stories: [
      {
        id: "placeholder-summerlin",
        title: "Summerlin expansion",
        firstYear: 1984,
        lastYear: 2024,
        blurb:
          "[SAMPLE] Placeholder film slot for urban growth patterns over decades.",
        theme: "Urban growth",
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
        id: "placeholder-palm-jumeirah",
        title: "Palm Jumeirah form",
        firstYear: 1984,
        lastYear: 2024,
        blurb:
          "[SAMPLE] Placeholder film slot for coastal reclamation visible from orbit.",
        theme: "Land reclamation",
      },
    ],
  },
  {
    id: "central-park",
    displayName: "Central Park, New York",
    lat: 40.78,
    lon: -73.97,
    theme: "Little change",
    stories: [
      {
        id: "placeholder-central-park",
        title: "Central Park control",
        firstYear: 1984,
        lastYear: 2024,
        blurb:
          "[SAMPLE] Placeholder control site — relatively stable park canopy for comparison.",
        theme: "Little change",
      },
    ],
  },
  {
    id: "ypsilanti",
    displayName: "Ypsilanti, Michigan",
    lat: 42.24,
    lon: -83.61,
    theme: "Local demo",
    stories: [
      {
        id: "placeholder-ypsilanti",
        title: "Ypsilanti local demo",
        firstYear: 1984,
        lastYear: 2024,
        blurb:
          "[SAMPLE] Local demo placeholder for hackathon walkthroughs near Ann Arbor.",
        theme: "Local demo",
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
  return PLACEHOLDER_PLACES.find((p) => p.id === id);
}

export function getStoryStub(storyId: string): {
  place: PlaceLocation;
  story: PlaceStoryStub;
} | undefined {
  for (const place of PLACEHOLDER_PLACES) {
    const story = place.stories.find((s) => s.id === storyId);
    if (story) return { place, story };
  }
  return undefined;
}
