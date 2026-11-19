/**
 * CPU reference of the renderer's cloud-mask logic (PRD §9.5), in float64.
 *
 * It reproduces, step for step, what the fragment shader does for pass 3 (webgl/shader.ts):
 * pixel ray -> ray end (sky mask, depth or nominal depth, sea surface) -> slab intervals with Earth
 * curvature -> n = cloudSampleCount(path, step) stratified points along the in-slab path (the step is a
 * fixed fraction of the layer's noise cell) -> cover read at each point's great-circle distance ->
 * N(0, 1) noise at each point's world position -> threshold against that cover -> average of the binary
 * masks (threshold before filter) -> slab opacity alpha = 1 - exp(-beta * (m / n) * L), counted up to the
 * point where the colour path stops sampling an opaque slab (CLOUD_TAU_OPAQUE).
 *
 * Uses: (a) the coverage Monte Carlo (montecarlo.ts) and (b) GPU-versus-CPU agreement tests on small
 * renders (test/gpu-cpu.test.ts). The GPU runs float32, so samples whose noise sits within float32
 * rounding of the threshold may flip; the agreement threshold for that is documented in README.md.
 */
import { pixelRay } from '../math/camera.ts';
import { CLOUD_TAU_OPAQUE, EARTH_RADIUS_M, T_INFINITE_M, cloudSampleCount } from '../math/constants.ts';
import { coverAt } from '../math/cover.ts';
import { cloudMask, filterThenThreshold } from '../math/coverage.ts';
import { rayPointGroundDistance } from '../math/geodesy.ts';
import { cloudNoise, lookupFromArray, makeLattice, type LatticeLookup } from '../math/noise.ts';
import { intervalLength, slabIntervals, sphereInside } from '../math/shell.ts';
import type { Vec3 } from '../math/vec3.ts';
import type { FrameLayer, FrameParams } from '../frame.ts';

/** An RGBA8 image in row-major order, top row first. */
export interface RawImage {
  width: number;
  height: number;
  data: Uint8Array | Uint8ClampedArray;
}

/** Depth PNG encoding: distance along the view ray in metres = R * 65536 + G * 256 + B. */
export const DEPTH_UNKNOWN = 0;
export const DEPTH_SKY = 0xffffff;

export function encodeDepth(metres: number): [number, number, number] {
  const v = !Number.isFinite(metres) ? DEPTH_SKY : Math.min(Math.max(Math.round(metres), 1), DEPTH_SKY - 1);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

/** Decodes a depth texel: null for unknown, Infinity for sky, otherwise metres. */
export function decodeDepth(r: number, g: number, b: number): number | null {
  const v = r * 65536 + g * 256 + b;
  if (v === DEPTH_UNKNOWN) return null;
  if (v === DEPTH_SKY) return Infinity;
  return v;
}

/** Where a sky ray stops: the sea surface (MSL sphere) if it meets it, otherwise "infinity". */
export function seaHit(hCam: number, dir: Vec3): number {
  if (hCam <= 0) return T_INFINITE_M;
  const iv = sphereInside(hCam, dir[2], 0);
  return iv.lo <= iv.hi && iv.lo > 0 ? iv.lo : T_INFINITE_M;
}

/** Where a downward ray meets the v0 flat ground (the shell at frame.hGround), or Infinity. */
export function flatGroundHit(frame: Pick<FrameParams, 'hCam' | 'hGround'>, dir: Vec3): number {
  if (!(dir[2] < 0)) return Infinity;
  const iv = sphereInside(frame.hCam, dir[2], frame.hGround);
  return iv.lo <= iv.hi && iv.lo >= 0 ? iv.lo : Infinity;
}

/**
 * Ray end for one pixel, as the shader computes it. skyValue is the sky-mask value in [0, 1] (null when
 * the plate has no mask: then every pixel is terrain); depthM is the decoded depth (null when absent).
 * Terrain without a usable depth ends at the nominal depth, or sooner where a downward ray meets the
 * v0 flat ground (TODO(M2): DEM depth).
 */
export function pixelRayEnd(frame: FrameParams, dir: Vec3, skyValue: number | null, depthM: number | null): { tEnd: number; isSky: boolean } {
  const isSky = skyValue !== null && skyValue >= 0.5;
  if (isSky) return { tEnd: seaHit(frame.hCam, dir), isSky };
  if (depthM !== null && Number.isFinite(depthM) && depthM > 0) return { tEnd: depthM, isSky };
  return { tEnd: Math.min(frame.nominalDepthM, flatGroundHit(frame, dir)), isSky };
}

/**
 * 'correct' is the renderer. The others are negative controls for the Monte Carlo: averaging noise before
 * thresholding, dropping the noise's variance normalisation, flat-Earth geometry, and the constant 4
 * samples per slab of renderer 0.1.0 (which under-drew broken decks near the horizon).
 */
export type CoverageVariant = 'correct' | 'filter-before-threshold' | 'unnormalized' | 'flat-earth' | 'four-samples';

export interface LayerSample {
  /** Mask coverage: mean of the thresholded samples over the whole in-slab path (its expectation is target). */
  kappa: number;
  /** Mean forecast cover at the same sample points: E[kappa]. Not the view-ray target fraction (montecarlo.ts). */
  target: number;
  /** Drawn slab opacity 1 - exp(-beta * (m / n) * L), with m counted up to the opaque stop (what the shader composites). */
  alpha: number;
  /** In-slab path length within [0, tEnd] (m). */
  pathLength: number;
  /** Ray parameter of the path's midpoint (compositing order). */
  tMid: number;
  /** Mask samples taken along the path (0 for solid slabs and misses). */
  samples: number;
}

const NO_HIT: LayerSample = Object.freeze({ kappa: 0, target: 0, alpha: 0, pathLength: 0, tMid: 0, samples: 0 });

export interface RayLayerOptions {
  /** Negative-control variants for the Monte Carlo; the renderer is 'correct'. */
  variant?: CoverageVariant;
  /** Overrides the renderer's sample count (convergence studies only). */
  samples?: number;
}

/** The layer fields the CPU reference reads. */
export type ReferenceLayer = Pick<FrameLayer, 'baseM' | 'topM' | 'solid' | 'profile' | 'transforms' | 'betaPerM' | 'stepM'>;

/**
 * Coverage and drawn opacity of one slab along one camera ray, exactly as the shader computes them.
 */
export function rayLayerCoverage(
  frame: Pick<FrameParams, 'hCam'>,
  layer: ReferenceLayer,
  lookup: LatticeLookup,
  dir: Vec3,
  tEnd: number,
  opts: RayLayerOptions = {},
): LayerSample {
  const variant = opts.variant ?? 'correct';
  const R = variant === 'flat-earth' ? 1e12 : EARTH_RADIUS_M;
  const [near, far] = slabIntervals(frame.hCam, dir[2], layer.baseM, layer.topM, 0, tEnd, R);
  const l1 = intervalLength(near);
  const l2 = intervalLength(far);
  const L = l1 + l2;
  if (L <= 0) return NO_HIT;
  const sMid = 0.5 * L;
  const tMid = sMid < l1 ? near.lo + sMid : far.lo + (sMid - l1);
  if (layer.solid) return { kappa: 1, target: 1, alpha: 1 - Math.exp(-layer.betaPerM * L), pathLength: L, tMid, samples: 0 };
  const n = opts.samples ?? (variant === 'four-samples' ? 4 : cloudSampleCount(L, layer.stepM));
  const tauPerSample = (layer.betaPerM * L) / n;
  const noises: number[] = [];
  const covers: number[] = [];
  for (let k = 0; k < n; k++) {
    const s = ((k + 0.5) / n) * L;
    const t = s < l1 ? near.lo + s : far.lo + (s - l1);
    covers.push(coverAt(layer.profile, rayPointGroundDistance(frame.hCam, dir, t, R) / 1000));
    noises.push(cloudNoise(lookup, layer.transforms, t * dir[0], t * dir[1], variant !== 'unnormalized'));
  }
  const target = covers.reduce((a, b) => a + b, 0) / n;
  if (variant === 'filter-before-threshold') {
    const kappa = filterThenThreshold(noises, covers);
    return { kappa, target, alpha: 1 - Math.exp(-layer.betaPerM * kappa * L), pathLength: L, tMid, samples: n };
  }
  let m = 0;
  let mDrawn = -1;
  for (let k = 0; k < n; k++) {
    m += cloudMask(noises[k]!, covers[k]!);
    if (mDrawn < 0 && m * tauPerSample >= CLOUD_TAU_OPAQUE) mDrawn = m;
  }
  if (mDrawn < 0) mDrawn = m;
  return { kappa: m / n, target, alpha: 1 - Math.exp(-mDrawn * tauPerSample), pathLength: L, tMid, samples: n };
}

export interface CpuCoverageResult {
  width: number;
  height: number;
  layers: FrameLayer[];
  /** Per layer, per pixel (row-major, top row first): mask coverage kappa over the whole path. */
  kappa: Float32Array[];
  /** Per layer, per pixel: mean forecast cover at the sample points. */
  target: Float32Array[];
  /** Per layer, per pixel: drawn slab opacity. */
  alpha: Float32Array[];
}

/**
 * Per-pixel coverage of every slab for a frame, mirroring the shader's coverage, target and alpha debug
 * outputs. Masks and depth must be the frame's size (the tests use output-sized inputs so no filtering
 * is involved).
 */
export function cpuCoverage(frame: FrameParams, inputs: { skyMask?: RawImage | null; depth?: RawImage | null } = {}): CpuCoverageResult {
  const { width, height } = frame;
  for (const img of [inputs.skyMask, inputs.depth]) {
    if (img && (img.width !== width || img.height !== height)) throw new Error('cpuCoverage: masks must match the frame size');
  }
  const lookup = lookupFromArray(makeLattice(frame.seedU32));
  const kappa = frame.layers.map(() => new Float32Array(width * height));
  const target = frame.layers.map(() => new Float32Array(width * height));
  const alpha = frame.layers.map(() => new Float32Array(width * height));
  for (let j = 0; j < height; j++) {
    for (let i = 0; i < width; i++) {
      const idx = j * width + i;
      const dir = pixelRay(frame.basis, i + 0.5, j + 0.5);
      const skyValue = inputs.skyMask ? inputs.skyMask.data[idx * 4]! / 255 : null;
      const depthM = inputs.depth ? decodeDepth(inputs.depth.data[idx * 4]!, inputs.depth.data[idx * 4 + 1]!, inputs.depth.data[idx * 4 + 2]!) : null;
      const { tEnd } = pixelRayEnd(frame, dir, skyValue, depthM);
      frame.layers.forEach((layer, li) => {
        const s = rayLayerCoverage(frame, layer, lookup, dir, tEnd);
        kappa[li]![idx] = s.kappa;
        target[li]![idx] = s.target;
        alpha[li]![idx] = s.alpha;
      });
    }
  }
  return { width, height, layers: frame.layers, kappa, target, alpha };
}
