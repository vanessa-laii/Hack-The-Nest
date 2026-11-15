import { LandingHero } from "@/components/LandingHero";
import { getPlacesForGlobe } from "@/lib/api";
import { PLACEHOLDER_PLACES } from "@/lib/places";

export default async function HomePage() {
  let places = PLACEHOLDER_PLACES;
  try {
    places = await getPlacesForGlobe();
  } catch {
    places = PLACEHOLDER_PLACES;
  }

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <LandingHero places={places} />
    </div>
  );
}
