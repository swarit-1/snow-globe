/**
 * Distances on the spherical Earth used to read the forecast where a view ray meets a layer
 * (PRD §8.3 "a sample point sits where a view ray meets each layer height", §9.2 pass 3).
 */
import { EARTH_RADIUS_M } from './constants.ts';
import type { Vec3 } from './vec3.ts';

/**
 * Great-circle distance (m, measured on the MSL sphere) from the camera's foot point to the point at
 * range t along a camera ray. hCam is the camera height above MSL; dir is the unit ENU direction.
 */
export function rayPointGroundDistance(hCam: number, dir: Vec3, t: number, R = EARTH_RADIUS_M): number {
  const horiz = t * Math.hypot(dir[0], dir[1]);
  return R * Math.atan2(horiz, R + hCam + t * dir[2]);
}

/** Great-circle distance (m) from the camera foot point to a point given in the local frame. */
export function localPointGroundDistance(p: Vec3, R = EARTH_RADIUS_M): number {
  return R * Math.atan2(Math.hypot(p[0], p[1]), R + p[2]);
}

/** Geometric dip of the horizon (radians) seen from h metres above a sphere: acos(R / (R + h)). */
export function horizonDip(h: number, R = EARTH_RADIUS_M): number {
  if (h <= 0) return 0;
  return Math.atan(Math.sqrt(2 * R * h + h * h) / R);
}

/**
 * Great-circle distance (m) between two latitude/longitude points (haversine). Used by callers that
 * build cover profiles from forecast grid points.
 */
export function haversineDistance(lat1Deg: number, lon1Deg: number, lat2Deg: number, lon2Deg: number, R = EARTH_RADIUS_M): number {
  const toRad = Math.PI / 180;
  const p1 = lat1Deg * toRad;
  const p2 = lat2Deg * toRad;
  const dp = p2 - p1;
  const dl = (lon2Deg - lon1Deg) * toRad;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}
