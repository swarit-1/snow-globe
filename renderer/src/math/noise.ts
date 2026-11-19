/*! @lookout/renderer notice: the lowbias32 integer hash is by Chris Wellons (hash-prospector, public domain). */
/**
 * Deterministic seeded value noise for cloud cover, anchored in world coordinates and advected by the
 * wind (PRD §9.2 pass 3, §9.5 "the seed is a hash of the viewpoint and the forecast run, and cloud noise
 * drifts with the forecast wind across valid time").
 *
 * Construction (shared bit-for-bit by the GPU lattice texture and the CPU reference):
 * - A 256 x 256 lattice of independent N(0, 1) values, generated from the seed by an integer hash and
 *   Box-Muller in float64, then rounded to float32 (the texture format).
 * - Each octave samples the lattice through its own rotation, scale (cell size / 2^octave) and offset,
 *   all hashed from (seed, layer slot, octave), so octaves and layers never share lattice points in
 *   practice.
 * - Within an octave, the four surrounding lattice values are blended with smoothstep weights w and the
 *   blend is divided by sqrt(sum w^2). A weighted sum of independent N(0, 1) values divided by the norm
 *   of its weights is exactly N(0, 1), so every point of every octave has an N(0, 1) marginal.
 * - Octave amplitudes are normalised so their squares sum to 1, so the fBm sum is exactly N(0, 1) too.
 *
 * Because the marginal is exactly N(0, 1) everywhere, the cover threshold is just the normal quantile of
 * the cover (coverage.ts), and E[mask] equals the forecast cover at every world point.
 *
 * World anchoring: noise is a function of the point's horizontal position (x east, y north, metres) in
 * the camera's local frame, which is fixed to the Earth for a viewpoint. Advection: the field at valid
 * time t is the run's field at its initialisation time t0 shifted by the displacement D(t), the time
 * integral of the wind over [t0, t] (math/advection.ts). The shift is folded into each octave's offset in
 * float64 on the CPU and reduced modulo the lattice period, so float32 on the GPU never sees large
 * numbers, and since D is continuous in t, scrubbing is continuous.
 */
import { sha256 } from '../hash/sha256.ts';

export const LATTICE_SIZE = 256;
export const LATTICE_MASK = LATTICE_SIZE - 1;
export const NOISE_OCTAVES = 4;
export const NOISE_GAIN = 0.5;
export const NOISE_LACUNARITY = 2;

/** Per-octave amplitudes, normalised so that the sum of squares is 1 (keeps the fBm exactly N(0, 1)). */
export const NOISE_AMPLITUDES: readonly number[] = (() => {
  const raw = Array.from({ length: NOISE_OCTAVES }, (_, o) => NOISE_GAIN ** o);
  const norm = Math.sqrt(raw.reduce((s, a) => s + a * a, 0));
  return raw.map((a) => a / norm);
})();

/** Chris Wellons' "lowbias32" integer hash (public domain). */
export function lowbias32(x: number): number {
  let h = x >>> 0;
  h ^= h >>> 16;
  h = Math.imul(h, 0x7feb352d);
  h ^= h >>> 15;
  h = Math.imul(h, 0x846ca68b);
  h ^= h >>> 16;
  return h >>> 0;
}

/** Folds 32-bit words into one hash. */
export function hashWords(seed: number, ...words: number[]): number {
  let h = lowbias32(seed ^ 0x9e3779b9);
  for (const w of words) h = lowbias32((h ^ lowbias32((w + 0x9e3779b9) >>> 0)) >>> 0);
  return h;
}

/** Maps a 32-bit hash to the open interval (0, 1). */
export function unitFromHash(h: number): number {
  return ((h >>> 0) + 0.5) / 4294967296;
}

/** The noise seed integer: the first four bytes (big-endian) of SHA-256 of the scene's seed string. */
export function seedToUint32(seed: string): number {
  const d = sha256(new TextEncoder().encode(seed));
  return ((d[0]! << 24) | (d[1]! << 16) | (d[2]! << 8) | d[3]!) >>> 0;
}

const TAG_LAT_U1 = 0x4c415431; // 'LAT1'
const TAG_LAT_U2 = 0x4c415432; // 'LAT2'
const TAG_ROT = 0x524f5420; // 'ROT '
const TAG_OFX = 0x4f465358; // 'OFSX'
const TAG_OFY = 0x4f465359; // 'OFSY'

/** The N(0, 1) lattice value at integer lattice point (ix, iy) for a seed, rounded to float32. */
export function latticeValue(seedU32: number, ix: number, iy: number): number {
  const x = ix & LATTICE_MASK;
  const y = iy & LATTICE_MASK;
  const u1 = unitFromHash(hashWords(seedU32, TAG_LAT_U1, x, y));
  const u2 = unitFromHash(hashWords(seedU32, TAG_LAT_U2, x, y));
  return Math.fround(Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2));
}

/** The full lattice as the GPU sees it: row-major, row iy, column ix. */
export function makeLattice(seedU32: number): Float32Array {
  const out = new Float32Array(LATTICE_SIZE * LATTICE_SIZE);
  for (let iy = 0; iy < LATTICE_SIZE; iy++) {
    for (let ix = 0; ix < LATTICE_SIZE; ix++) out[iy * LATTICE_SIZE + ix] = latticeValue(seedU32, ix, iy);
  }
  return out;
}

/** Lattice lookup with wrap-around. */
export type LatticeLookup = (ix: number, iy: number) => number;

export function lookupFromArray(lattice: Float32Array): LatticeLookup {
  return (ix, iy) => lattice[(iy & LATTICE_MASK) * LATTICE_SIZE + (ix & LATTICE_MASK)]!;
}

/** A lazily filled lattice for a seed (used by the Monte Carlo, which visits few lattice points per seed). */
export function lazyLattice(seedU32: number): LatticeLookup {
  const values = new Float32Array(LATTICE_SIZE * LATTICE_SIZE);
  const filled = new Uint8Array(LATTICE_SIZE * LATTICE_SIZE);
  return (ix, iy) => {
    const k = (iy & LATTICE_MASK) * LATTICE_SIZE + (ix & LATTICE_MASK);
    if (filled[k] === 0) {
      values[k] = latticeValue(seedU32, ix, iy);
      filled[k] = 1;
    }
    return values[k]!;
  };
}

/** Lattice coordinates u = M (x, y) + c for one octave. */
export interface OctaveTransform {
  m00: number;
  m01: number;
  m10: number;
  m11: number;
  c0: number;
  c1: number;
}

/**
 * Per-octave transforms for one layer slot: rotation and offset hashed from (seed, slot, octave), scale
 * 1 / (cellSize / 2^octave), and the advection shift (dispEastM, dispNorthM) folded into the offset in
 * float64 and reduced modulo the lattice period. The shift is the field's drift since the forecast run's
 * initialisation (math/advection.ts), never wind x absolute time.
 */
export function layerNoiseTransforms(
  seedU32: number,
  slot: number,
  cellSizeM: number,
  dispEastM: number,
  dispNorthM: number,
): OctaveTransform[] {
  const out: OctaveTransform[] = [];
  const ox = dispEastM;
  const oy = dispNorthM;
  for (let o = 0; o < NOISE_OCTAVES; o++) {
    const cell = cellSizeM / NOISE_LACUNARITY ** o;
    const theta = 2 * Math.PI * unitFromHash(hashWords(seedU32, TAG_ROT, slot, o));
    const s = 1 / cell;
    const m00 = s * Math.cos(theta);
    const m01 = -s * Math.sin(theta);
    const m10 = s * Math.sin(theta);
    const m11 = s * Math.cos(theta);
    const dx = LATTICE_SIZE * unitFromHash(hashWords(seedU32, TAG_OFX, slot, o));
    const dy = LATTICE_SIZE * unitFromHash(hashWords(seedU32, TAG_OFY, slot, o));
    const c0 = dx - (m00 * ox + m01 * oy);
    const c1 = dy - (m10 * ox + m11 * oy);
    out.push({
      m00,
      m01,
      m10,
      m11,
      c0: c0 - LATTICE_SIZE * Math.floor(c0 / LATTICE_SIZE),
      c1: c1 - LATTICE_SIZE * Math.floor(c1 / LATTICE_SIZE),
    });
  }
  return out;
}

/** One octave of variance-normalised value noise at lattice coordinates (u0, u1); N(0, 1) marginal. */
export function octaveValue(lookup: LatticeLookup, u0: number, u1: number, normalizeVariance = true): number {
  const fx = Math.floor(u0);
  const fy = Math.floor(u1);
  const ix = fx | 0;
  const iy = fy | 0;
  const tx = u0 - fx;
  const ty = u1 - fy;
  const sx = tx * tx * (3 - 2 * tx);
  const sy = ty * ty * (3 - 2 * ty);
  const w00 = (1 - sx) * (1 - sy);
  const w10 = sx * (1 - sy);
  const w01 = (1 - sx) * sy;
  const w11 = sx * sy;
  const v = w00 * lookup(ix, iy) + w10 * lookup(ix + 1, iy) + w01 * lookup(ix, iy + 1) + w11 * lookup(ix + 1, iy + 1);
  if (!normalizeVariance) return v;
  return v / Math.sqrt(w00 * w00 + w10 * w10 + w01 * w01 + w11 * w11);
}

/** fBm cloud noise at a world point (x east, y north, metres): exactly N(0, 1) at every point. */
export function cloudNoise(
  lookup: LatticeLookup,
  transforms: readonly OctaveTransform[],
  x: number,
  y: number,
  normalizeVariance = true,
): number {
  let n = 0;
  for (let o = 0; o < transforms.length; o++) {
    const t = transforms[o]!;
    n += NOISE_AMPLITUDES[o]! * octaveValue(lookup, t.m00 * x + t.m01 * y + t.c0, t.m10 * x + t.m11 * y + t.c1, normalizeVariance);
  }
  return n;
}
