/**
 * Geographic helpers for the globe.
 * Inputs: latitude, longitude, optional radius.
 * Outputs: [x, y, z] positions on a unit sphere (Three.js coords).
 */

import * as THREE from "three";

/** Convert WGS84 lat/lon to a point on a sphere (Y-up). */
export function latLonToVector3(
  lat: number,
  lon: number,
  radius = 1,
): THREE.Vector3 {
  const phi = (90 - lat) * (Math.PI / 180);
  const theta = (lon + 180) * (Math.PI / 180);
  return new THREE.Vector3(
    -radius * Math.sin(phi) * Math.cos(theta),
    radius * Math.cos(phi),
    radius * Math.sin(phi) * Math.sin(theta),
  );
}

/** Camera position outside a surface point (default keeps a wide globe frame). */
export function cameraPositionForLatLon(
  lat: number,
  lon: number,
  distance = 5.3,
): THREE.Vector3 {
  return latLonToVector3(lat, lon, distance);
}
