/**
 * Glowing place marker on the globe.
 * Inputs: place lat/lon, selection state, click handler.
 * Outputs: bright core + soft halo (labels live in the side panel).
 */

"use client";

import { ThreeEvent, useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { latLonToVector3 } from "@/lib/geo";
import type { PlaceLocation } from "@/lib/places";

const GLOBE_RADIUS = 1.6;
const ACCENT = "#2997ff";
const SELECTED = "#f5f5f7";

type LocationHotspotProps = {
  place: PlaceLocation;
  selected: boolean;
  onSelect: (placeId: string) => void;
};

export function LocationHotspot({
  place,
  selected,
  onSelect,
}: LocationHotspotProps) {
  const position = useMemo(
    () => latLonToVector3(place.lat, place.lon, GLOBE_RADIUS + 0.025),
    [place.lat, place.lon],
  );
  const haloRef = useRef<THREE.Mesh>(null);
  // THREE.Timer replaces deprecated THREE.Clock / R3F state.clock (three r183+).
  const timer = useMemo(() => new THREE.Timer(), []);

  useEffect(() => {
    timer.connect(document);
    return () => {
      timer.dispose();
      document.body.style.cursor = "auto";
    };
  }, [timer]);

  useFrame(() => {
    if (!haloRef.current) return;
    timer.update();
    const pulse = 1.6 + Math.sin(timer.getElapsed() * 2.2) * 0.25;
    haloRef.current.scale.setScalar(selected ? pulse + 0.35 : pulse);
  });

  function handleClick(e: ThreeEvent<MouseEvent>) {
    e.stopPropagation();
    onSelect(place.id);
  }

  const color = selected ? SELECTED : ACCENT;

  return (
    <group position={position}>
      <mesh
        onClick={handleClick}
        onPointerOver={(e) => {
          e.stopPropagation();
          document.body.style.cursor = "pointer";
        }}
        onPointerOut={() => {
          document.body.style.cursor = "auto";
        }}
      >
        <sphereGeometry args={[selected ? 0.038 : 0.028, 16, 16]} />
        <meshBasicMaterial color={color} toneMapped={false} />
      </mesh>
      <mesh ref={haloRef}>
        <sphereGeometry args={[0.028, 16, 16]} />
        <meshBasicMaterial
          color={color}
          transparent
          opacity={selected ? 0.35 : 0.22}
          toneMapped={false}
          depthWrite={false}
        />
      </mesh>
    </group>
  );
}

export { GLOBE_RADIUS };
