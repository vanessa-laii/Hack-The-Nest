/**
 * Full-bleed realistic Earth globe (R3F).
 * Inputs: places, selectedPlaceId, onSelectPlace, reducedMotion.
 * Outputs: textured spinning earth with stars and glowing place markers.
 */

"use client";

import { OrbitControls, Stars, useTexture } from "@react-three/drei";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { cameraPositionForLatLon } from "@/lib/geo";
import type { PlaceLocation } from "@/lib/places";
import { GLOBE_RADIUS, LocationHotspot } from "./LocationHotspot";

const OVERVIEW_POS = new THREE.Vector3(0, 0.28, 5.3);
const OVERVIEW_TARGET = new THREE.Vector3(0, 0, 0);

type GlobeProps = {
  places: PlaceLocation[];
  selectedPlaceId: string | null;
  onSelectPlace: (id: string) => void;
  reducedMotion?: boolean;
  className?: string;
};

function prepareMap(texture: THREE.Texture, srgb: boolean) {
  if (srgb) texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 8;
  texture.generateMipmaps = true;
  texture.minFilter = THREE.LinearMipmapLinearFilter;
  texture.magFilter = THREE.LinearFilter;
  texture.wrapS = THREE.RepeatWrapping;
  texture.wrapT = THREE.ClampToEdgeWrapping;
  texture.needsUpdate = true;
}

function EarthSphere({ reducedMotion }: { reducedMotion: boolean }) {
  const [dayMap, specularMap, cloudsMap] = useTexture([
    "/textures/earth_day.jpg",
    "/textures/earth_specular.png",
    "/textures/earth_clouds.png",
  ]);

  useEffect(() => {
    prepareMap(dayMap, true);
    prepareMap(specularMap, false);
    prepareMap(cloudsMap, true);
  }, [dayMap, specularMap, cloudsMap]);

  const cloudsRef = useRef<THREE.Mesh>(null);

  useFrame((_, delta) => {
    if (reducedMotion || !cloudsRef.current) return;
    cloudsRef.current.rotation.y += delta * 0.012;
  });

  return (
    <group>
      <mesh>
        <sphereGeometry args={[GLOBE_RADIUS, 128, 128]} />
        <meshPhongMaterial
          map={dayMap}
          specularMap={specularMap}
          specular={new THREE.Color("#8899aa")}
          shininess={28}
          emissive={new THREE.Color("#0a1520")}
          emissiveIntensity={0.08}
        />
      </mesh>

      <mesh ref={cloudsRef} scale={1.008}>
        <sphereGeometry args={[GLOBE_RADIUS, 96, 96]} />
        <meshPhongMaterial
          map={cloudsMap}
          transparent
          opacity={0.32}
          depthWrite={false}
          specular={new THREE.Color("#222222")}
          shininess={4}
        />
      </mesh>

      {/* No atmosphere shells — they read as a halo/ring against charcoal */}
    </group>
  );
}

function CameraFocus({
  place,
  reducedMotion,
  onAnimatingChange,
}: {
  place: PlaceLocation | null;
  reducedMotion: boolean;
  onAnimatingChange: (animating: boolean) => void;
}) {
  const { camera } = useThree();
  const controls = useThree((s) => s.controls) as unknown as {
    target: THREE.Vector3;
    update: () => void;
    enabled: boolean;
  } | null;
  const goalPos = useRef(OVERVIEW_POS.clone());
  const goalTarget = useRef(OVERVIEW_TARGET.clone());
  const active = useRef(false);
  // Only return-to-overview after a real deselect — never on mount (that fights autoRotate).
  const hadSelection = useRef(false);
  const onAnimatingChangeRef = useRef(onAnimatingChange);
  onAnimatingChangeRef.current = onAnimatingChange;

  useEffect(() => {
    if (!place) {
      if (!hadSelection.current) return;
      hadSelection.current = false;
      goalPos.current.copy(OVERVIEW_POS);
      goalTarget.current.copy(OVERVIEW_TARGET);
      active.current = true;
      onAnimatingChangeRef.current(true);
      return;
    }
    hadSelection.current = true;
    // Gentle move-in from overview 5.3 → focus 3.9 (~1.4 delta).
    const cam = cameraPositionForLatLon(place.lat, place.lon, 3.9);
    const look = cameraPositionForLatLon(place.lat, place.lon, GLOBE_RADIUS * 0.05);
    goalPos.current.copy(cam);
    goalTarget.current.copy(look);
    active.current = true;
    onAnimatingChangeRef.current(true);
  }, [place]);

  useFrame((_, delta) => {
    if (!active.current) return;
    const ease = reducedMotion ? 1 : 1 - Math.exp(-delta * 3.0);
    camera.position.lerp(goalPos.current, ease);
    if (controls?.target) {
      controls.target.lerp(goalTarget.current, ease);
      controls.update();
    } else {
      camera.lookAt(goalTarget.current);
    }
    if (
      camera.position.distanceTo(goalPos.current) < 0.01 &&
      (!controls?.target ||
        controls.target.distanceTo(goalTarget.current) < 0.01)
    ) {
      active.current = false;
      onAnimatingChangeRef.current(false);
    }
  });

  return null;
}

function Scene({
  places,
  selectedPlaceId,
  onSelectPlace,
  reducedMotion,
}: Omit<GlobeProps, "className">) {
  const selected = useMemo(
    () => places.find((p) => p.id === selectedPlaceId) ?? null,
    [places, selectedPlaceId],
  );
  const [focusAnimating, setFocusAnimating] = useState(false);

  return (
    <>
      <ambientLight intensity={0.22} />
      <directionalLight
        position={[5.5, 2.4, 2.8]}
        intensity={2.15}
        color="#fff4e6"
      />
      <directionalLight
        position={[-3.5, -0.8, -2.5]}
        intensity={0.45}
        color="#6aa8ff"
      />
      <pointLight position={[2.2, 1.2, 3.2]} intensity={0.55} color="#5BA8A0" />
      <Stars
        radius={120}
        depth={55}
        count={reducedMotion ? 600 : 2200}
        factor={3.2}
        saturation={0}
        fade
        speed={reducedMotion ? 0 : 0.15}
      />
      <EarthSphere reducedMotion={!!reducedMotion} />
      {places.map((place) => (
        <LocationHotspot
          key={place.id}
          place={place}
          selected={place.id === selectedPlaceId}
          onSelect={onSelectPlace}
        />
      ))}
      <CameraFocus
        place={selected}
        reducedMotion={!!reducedMotion}
        onAnimatingChange={setFocusAnimating}
      />
      <OrbitControls
        makeDefault
        enablePan={false}
        enableZoom
        minDistance={2.7}
        maxDistance={7.0}
        rotateSpeed={0.5}
        zoomSpeed={0.6}
        autoRotate={!reducedMotion && !selectedPlaceId && !focusAnimating}
        autoRotateSpeed={0.35}
        dampingFactor={0.08}
        enableDamping
      />
    </>
  );
}

export default function Globe({
  places,
  selectedPlaceId,
  onSelectPlace,
  reducedMotion = false,
  className,
}: GlobeProps) {
  return (
    <div
      className={className}
      role="img"
      aria-label="Interactive Earth globe with sample place markers"
    >
      <Canvas
        camera={{ position: [0, 0.28, 5.3], fov: 40, near: 0.1, far: 250 }}
        dpr={[1, 2]}
        gl={{
          antialias: true,
          alpha: true,
          powerPreference: "high-performance",
        }}
        onCreated={({ gl }) => {
          gl.toneMapping = THREE.ACESFilmicToneMapping;
          gl.toneMappingExposure = 1.05;
          gl.outputColorSpace = THREE.SRGBColorSpace;
        }}
        style={{ background: "transparent", width: "100%", height: "100%" }}
      >
        <Suspense fallback={null}>
          <Scene
            places={places}
            selectedPlaceId={selectedPlaceId}
            onSelectPlace={onSelectPlace}
            reducedMotion={reducedMotion}
          />
        </Suspense>
      </Canvas>
    </div>
  );
}
