/**
 * A procedural test world for goldens and benchmarks: a volcano 13 km away (summit ~4,390 m, like
 * Rainier from Paradise), a valley at ~1,100-1,300 m in between, and the camera on a ledge at 1,650 m
 * whose edge is 15 m in front of it, so the camera looks straight down into the valley: a deck below the
 * camera (the "sea of clouds" and marine-layer case, PRD §9.3) fills a large share of the frame.
 * Renderer 0.1.0 put the ledge edge 400 m out, which hid the valley: removing its golden deck changed
 * 48 of 57,600 pixels.
 *
 * The plate, sky mask and depth map are ray-cast from the renderer's own camera model with Earth
 * curvature, so they agree exactly with the renderer's view rays. The plate is lit by an even overcast
 * sky and has no haze (PRD §8.5 says plates are dehazed albedo proxies). No camera imagery is involved,
 * so these files are safe to commit (unlike NPS frames, plates or renders, which stay outside the repo).
 */
import { cameraBasis, pixelRay } from '../math/camera.ts';
import { DEG } from '../math/constants.ts';
import { hashWords, unitFromHash } from '../math/noise.ts';
import { pointHeight } from '../math/shell.ts';
import { encodeDepth, type RawImage } from '../cpu/reference.ts';
import type { CameraSpec } from '../scene.ts';

/** The synthetic camera (golden size 320 x 180). */
export const SYNTHETIC_CAMERA: CameraSpec = {
  width: 320,
  height: 180,
  lat: 46.786,
  lon: -121.735,
  elev_m_msl: 1650,
  heading_deg: 30,
  pitch_deg: 3,
  roll_deg: 0,
  hfov_deg: 60,
  agl_m: 10,
};

export const SYNTHETIC_VIEWPOINT_ID = 'synthetic.test-ridge';

/** Asset id of the synthetic plate set (bump the suffix whenever the world or the camera changes). */
export const SYNTHETIC_PLATE_ID = 'synthetic.test-ridge/plate-v2';

const VIEW_AZ = 30 * DEG;
const VOLCANO = { x: 13000 * Math.sin(VIEW_AZ), y: 13000 * Math.cos(VIEW_AZ) };
const BASE_M = 1300;
const SUMMIT_GAIN_M = 3092;

function valueNoise2(x: number, y: number, salt: number): number {
  const ix = Math.floor(x);
  const iy = Math.floor(y);
  const fx = x - ix;
  const fy = y - iy;
  const sx = fx * fx * (3 - 2 * fx);
  const sy = fy * fy * (3 - 2 * fy);
  const v = (a: number, b: number) => unitFromHash(hashWords(salt, a | 0, b | 0));
  return (1 - sy) * ((1 - sx) * v(ix, iy) + sx * v(ix + 1, iy)) + sy * ((1 - sx) * v(ix, iy + 1) + sx * v(ix + 1, iy + 1));
}

function terrainWithoutPlateau(x: number, y: number): number {
  const r = Math.hypot(x - VOLCANO.x, y - VOLCANO.y);
  const volcano = SUMMIT_GAIN_M * Math.exp(-((r / 4300) ** 1.45));
  const along = x * Math.sin(VIEW_AZ) + y * Math.cos(VIEW_AZ);
  const across = x * Math.cos(VIEW_AZ) - y * Math.sin(VIEW_AZ);
  const valley = -380 * Math.exp(-(((along - 4500) / 2200) ** 2));
  const leftRidge = 900 * Math.exp(-(((across + 5200) / 1800) ** 2)) * Math.exp(-(((along - 7000) / 5000) ** 2));
  const rightRidge = 650 * Math.exp(-(((across - 6000) / 2000) ** 2)) * Math.exp(-(((along - 6000) / 4500) ** 2));
  const detail = 45 * (valueNoise2(x / 700, y / 700, 11) - 0.5) + 18 * (valueNoise2(x / 180, y / 180, 12) - 0.5);
  return BASE_M + volcano + valley + leftRidge + rightRidge + detail;
}

/** The camera's ledge: flat behind and under the camera, then a cliff ~15 m in front of it dropping ~330 m into the valley. */
function plateauShape(x: number, y: number): number {
  const along = x * Math.sin(VIEW_AZ) + y * Math.cos(VIEW_AZ);
  const across = x * Math.cos(VIEW_AZ) - y * Math.sin(VIEW_AZ);
  return (1 / (1 + Math.exp((along - 15) / 3))) * Math.exp(-((across / 4000) ** 2));
}

/** Plateau height chosen so the ground under the camera is exactly elev - agl. */
const PLATEAU_AMP = (SYNTHETIC_CAMERA.elev_m_msl - (SYNTHETIC_CAMERA.agl_m ?? 10) - terrainWithoutPlateau(0, 0)) / plateauShape(0, 0);

/** Terrain height (m above MSL) at local horizontal position (x east, y north, metres from the camera). */
export function terrainHeight(x: number, y: number): number {
  return terrainWithoutPlateau(x, y) + PLATEAU_AMP * plateauShape(x, y);
}

/** Distance along the ray to the terrain, or null for sky (nothing within maxT). */
export function castTerrainRay(hCam: number, dir: readonly [number, number, number], maxT = 160_000): number | null {
  let prevT = 0;
  let t = 1;
  while (t < maxT) {
    const diff = pointHeight(hCam, dir[2], t) - terrainHeight(t * dir[0], t * dir[1]);
    if (diff <= 0) {
      let lo = prevT;
      let hi = t;
      for (let i = 0; i < 22; i++) {
        const mid = 0.5 * (lo + hi);
        if (pointHeight(hCam, dir[2], mid) - terrainHeight(mid * dir[0], mid * dir[1]) > 0) lo = mid;
        else hi = mid;
      }
      return 0.5 * (lo + hi);
    }
    prevT = t;
    t += Math.min(Math.max(0.4 * diff, 0.5 + 0.004 * t), 2000);
  }
  return null;
}

const srgbEncode = (c: number): number => {
  const v = Math.min(Math.max(c, 0), 1);
  return v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055;
};

function albedoAt(x: number, y: number, h: number, slope: number): [number, number, number] {
  const n1 = valueNoise2(x / 400, y / 400, 21);
  const n2 = valueNoise2(x / 250, y / 250, 22);
  const tex = 0.85 + 0.3 * valueNoise2(x / 60, y / 60, 23);
  let a: [number, number, number];
  if (h > 2650 + 240 * (n1 - 0.5) && slope < 1.1) a = [0.82, 0.85, 0.9];
  else if (h > 2150 + 200 * (n2 - 0.5) || slope > 0.85) a = [0.3, 0.28, 0.26];
  else if (h > 1600) a = [0.26, 0.3, 0.15];
  else a = [0.07, 0.13, 0.07];
  return [a[0] * tex, a[1] * tex, a[2] * tex];
}

export interface SyntheticPlate {
  plate: RawImage;
  skyMask: RawImage;
  depth: RawImage;
  stats: { skyFraction: number; minDepthM: number; maxDepthM: number; summitDepthM: number | null; elapsedMs: number };
}

/**
 * Ray-casts the synthetic plate set at any size (2 x 2 supersampling for colour and mask; depth from the
 * pixel-centre ray). Deterministic: same size, same bytes.
 */
export function generateSyntheticPlate(width = SYNTHETIC_CAMERA.width, height = SYNTHETIC_CAMERA.height): SyntheticPlate {
  const t0 = performance.now();
  const basis = cameraBasis({ ...SYNTHETIC_CAMERA, width, height });
  const hCam = SYNTHETIC_CAMERA.elev_m_msl;
  const plate = new Uint8Array(width * height * 4);
  const mask = new Uint8Array(width * height * 4);
  const depth = new Uint8Array(width * height * 4);
  const sky = [0.7, 0.74, 0.8];
  let skyCount = 0;
  let minD = Infinity;
  let maxD = 0;
  const eps = 5;
  for (let j = 0; j < height; j++) {
    for (let i = 0; i < width; i++) {
      const lin = [0, 0, 0];
      let skySub = 0;
      for (const [ox, oy] of [
        [0.25, 0.25],
        [0.75, 0.25],
        [0.25, 0.75],
        [0.75, 0.75],
      ] as const) {
        const dir = pixelRay(basis, i + ox, j + oy);
        const t = castTerrainRay(hCam, dir);
        if (t === null) {
          skySub++;
          for (let c = 0; c < 3; c++) lin[c]! += sky[c]! / 4;
          continue;
        }
        const x = t * dir[0];
        const y = t * dir[1];
        const h = terrainHeight(x, y);
        const gx = (terrainHeight(x + eps, y) - terrainHeight(x - eps, y)) / (2 * eps);
        const gy = (terrainHeight(x, y + eps) - terrainHeight(x, y - eps)) / (2 * eps);
        const nz = 1 / Math.hypot(gx, gy, 1);
        const a = albedoAt(x, y, h, Math.hypot(gx, gy));
        const shade = 0.6 + 0.4 * nz; // even overcast: sky view of a tilted surface
        for (let c = 0; c < 3; c++) lin[c]! += (a[c]! * shade) / 4;
      }
      const o = (j * width + i) * 4;
      for (let c = 0; c < 3; c++) plate[o + c] = Math.round(255 * srgbEncode(lin[c]!));
      plate[o + 3] = 255;
      const m = Math.round((255 * skySub) / 4);
      mask[o] = mask[o + 1] = mask[o + 2] = m;
      mask[o + 3] = 255;
      const tc = castTerrainRay(hCam, pixelRay(basis, i + 0.5, j + 0.5));
      const [r, g, b] = encodeDepth(tc === null ? Infinity : tc);
      depth[o] = r;
      depth[o + 1] = g;
      depth[o + 2] = b;
      depth[o + 3] = 255;
      if (tc === null) skyCount++;
      else {
        minD = Math.min(minD, tc);
        maxD = Math.max(maxD, tc);
      }
    }
  }
  // distance to the summit along its own ray, for reference
  const sx = VOLCANO.x;
  const sy = VOLCANO.y;
  const summitDepthM = Math.hypot(sx, sy, terrainHeight(sx, sy) - hCam);
  return {
    plate: { width, height, data: plate },
    skyMask: { width, height, data: mask },
    depth: { width, height, data: depth },
    stats: { skyFraction: skyCount / (width * height), minDepthM: minD, maxDepthM: maxD, summitDepthM, elapsedMs: performance.now() - t0 },
  };
}
