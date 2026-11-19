/**
 * Pinhole camera model in the camera's local East-North-Up frame (PRD §9.2).
 *
 * Conventions (the GLSL uses the same basis, passed as uniforms):
 * - x = east, y = north, z = up.
 * - heading_deg: azimuth of the optical axis, clockwise from true north (0 = north, 90 = east).
 * - pitch_deg: elevation of the optical axis, up positive.
 * - roll_deg: rotation about the optical axis, positive = camera turned clockwise as seen from behind it
 *   (so the image content appears rotated counter-clockwise).
 * - hfov_deg: full horizontal field of view; pixels are square, so the vertical half-angle tangent is
 *   tan(hfov/2) * height / width.
 * - Pixel coordinates: origin at the top-left corner of the image, x right, y down; pixel (i, j) has its
 *   centre at (i + 0.5, j + 0.5).
 */
import { DEG } from './constants.ts';
import { cross, normalize, type Vec3 } from './vec3.ts';

export interface CameraPose {
  width: number;
  height: number;
  heading_deg: number;
  pitch_deg: number;
  roll_deg: number;
  hfov_deg: number;
}

export interface CameraBasis {
  /** Unit optical axis. */
  forward: Vec3;
  /** Unit image-right vector. */
  right: Vec3;
  /** Unit image-up vector. */
  up: Vec3;
  /** tan(hfov / 2). */
  tanHalfH: number;
  /** tan(hfov / 2) * height / width. */
  tanHalfV: number;
  width: number;
  height: number;
}

/** Unit direction for an altitude/azimuth pair (azimuth clockwise from north), in ENU. */
export function altAzToDirection(altDeg: number, azDeg: number): Vec3 {
  const alt = altDeg * DEG;
  const az = azDeg * DEG;
  const c = Math.cos(alt);
  return [Math.sin(az) * c, Math.cos(az) * c, Math.sin(alt)];
}

/** Altitude and azimuth (degrees, azimuth in [0, 360)) of an ENU direction. */
export function directionToAltAz(d: Vec3): { alt_deg: number; az_deg: number } {
  const n = normalize(d);
  const alt = Math.asin(Math.max(-1, Math.min(1, n[2]))) / DEG;
  let az = Math.atan2(n[0], n[1]) / DEG;
  if (az < 0) az += 360;
  return { alt_deg: alt, az_deg: az };
}

/** Builds the camera basis from a pose (see the module comment for conventions). */
export function cameraBasis(pose: CameraPose): CameraBasis {
  const h = pose.heading_deg * DEG;
  const p = pose.pitch_deg * DEG;
  const r = pose.roll_deg * DEG;
  const forward: Vec3 = [Math.sin(h) * Math.cos(p), Math.cos(h) * Math.cos(p), Math.sin(p)];
  const right0: Vec3 = [Math.cos(h), -Math.sin(h), 0];
  const up0 = cross(right0, forward);
  const cr = Math.cos(r);
  const sr = Math.sin(r);
  const right: Vec3 = [
    right0[0] * cr - up0[0] * sr,
    right0[1] * cr - up0[1] * sr,
    right0[2] * cr - up0[2] * sr,
  ];
  const up: Vec3 = [up0[0] * cr + right0[0] * sr, up0[1] * cr + right0[1] * sr, up0[2] * cr + right0[2] * sr];
  const tanHalfH = Math.tan((pose.hfov_deg * DEG) / 2);
  return {
    forward,
    right,
    up,
    tanHalfH,
    tanHalfV: (tanHalfH * pose.height) / pose.width,
    width: pose.width,
    height: pose.height,
  };
}

/**
 * Unit view-ray direction through image point (px, py) in pixel coordinates (top-left origin;
 * pixel centres at +0.5). Matches the shader's gl_FragCoord-based ray exactly.
 */
export function pixelRay(b: CameraBasis, px: number, py: number): Vec3 {
  const nx = (2 * px) / b.width - 1;
  const ny = 1 - (2 * py) / b.height;
  const sx = nx * b.tanHalfH;
  const sy = ny * b.tanHalfV;
  return normalize([
    b.forward[0] + sx * b.right[0] + sy * b.up[0],
    b.forward[1] + sx * b.right[1] + sy * b.up[1],
    b.forward[2] + sx * b.right[2] + sy * b.up[2],
  ]);
}

/**
 * Projects a world direction into pixel coordinates; returns null when it is behind the camera.
 * Used to check, for example, that the Sun disk lands where the pose says it should (PRD §8.5 pose check).
 */
export function projectDirection(b: CameraBasis, d: Vec3): { px: number; py: number } | null {
  const z = d[0] * b.forward[0] + d[1] * b.forward[1] + d[2] * b.forward[2];
  if (z <= 0) return null;
  const x = (d[0] * b.right[0] + d[1] * b.right[1] + d[2] * b.right[2]) / z;
  const y = (d[0] * b.up[0] + d[1] * b.up[1] + d[2] * b.up[2]) / z;
  const nx = x / b.tanHalfH;
  const ny = y / b.tanHalfV;
  return { px: ((nx + 1) * b.width) / 2, py: ((1 - ny) * b.height) / 2 };
}
