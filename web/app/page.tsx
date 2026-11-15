import { LandingHero } from "@/components/LandingHero";
import { getPlacesForGlobe } from "@/lib/gallery";
import { SEED_PLACES } from "@/lib/places";

export default async function HomePage() {
  let places = SEED_PLACES;
  try {
    places = await getPlacesForGlobe();
  } catch {
    places = SEED_PLACES;
  }

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <LandingHero places={places} />
    </div>
  );
}
