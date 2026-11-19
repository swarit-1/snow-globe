/**
 * Ray versus spherical shells around a spherical Earth (PRD §9.2 pass 3, §9.3).
 *
 * A cloud slab is the shell between radii R + base and R + top; the fog and haze layers are the
 * ball below some height. A ray is described by the height h0 (m above MSL) of its origin and the
 * cosine mu between its direction and the local vertical at the origin. For a camera ray, mu is the
 * z component of the ENU direction.
 *
 * Numerics: everything is written so that float32 (the GPU's highp) keeps metre-level precision with
 * R = 6.371e6 m. The two tricks are (1) c = |Q|^2 - (R + h)^2 is computed as (h0 - h)(2R + h0 + h), and
 * (2) the roots of t^2 + 2Bt + c = 0 use the cancellation-free form q = -(B + sign(B) sqrt(B^2 - c)),
 * t = q and t = c / q. The GLSL twin in webgl/shader.ts mirrors these functions line for line.
 */
import { EARTH_RADIUS_M } from './constants.ts';
import type { Vec3 } from './vec3.ts';

/** A closed parameter interval [lo, hi] along a ray; empty when lo > hi. */
export interface Interval {
  lo: number;
  hi: number;
}

export const EMPTY_INTERVAL: Interval = Object.freeze({ lo: 1, hi: -1 });

export function isEmpty(iv: Interval): boolean {
  return !(iv.lo <= iv.hi);
}

export function intervalLength(iv: Interval): number {
  return Math.max(iv.hi - iv.lo, 0);
}

export function clipInterval(iv: Interval, t0: number, t1: number): Interval {
  return { lo: Math.max(iv.lo, t0), hi: Math.min(iv.hi, t1) };
}

/** Length of the overlap between an interval and [ta, tb]. */
export function overlapLength(iv: Interval, ta: number, tb: number): number {
  return Math.max(Math.min(iv.hi, tb) - Math.max(iv.lo, ta), 0);
}

/**
 * The parameter interval over which the ray (origin height h0, vertical cosine mu) is inside the sphere
 * of radius R + h, that is at or below height h. Returns EMPTY_INTERVAL if it never is. The interval
 * may start at a negative t (the origin is already inside).
 */
export function sphereInside(h0: number, mu: number, h: number, R = EARTH_RADIUS_M): Interval {
  const B = (R + h0) * mu;
  const C = (h0 - h) * (2 * R + h0 + h);
  const disc = B * B - C;
  if (disc < 0) return EMPTY_INTERVAL;
  const sq = Math.sqrt(disc);
  const q = -(B + (B >= 0 ? sq : -sq));
  if (q === 0) return { lo: 0, hi: 0 };
  const t1 = q;
  const t2 = C / q;
  return t1 <= t2 ? { lo: t1, hi: t2 } : { lo: t2, hi: t1 };
}

/**
 * The parts of the ray inside the slab between heights base and top, clipped to [t0, t1].
 * Returns [near, far]: the slab is the outer ball minus the inner ball, so a ray can cross it twice
 * (for example a camera above a deck looking past the deck's inner edge). Either may be empty.
 */
export function slabIntervals(
  h0: number,
  mu: number,
  base: number,
  top: number,
  t0: number,
  t1: number,
  R = EARTH_RADIUS_M,
): [Interval, Interval] {
  const outer = sphereInside(h0, mu, top, R);
  if (isEmpty(outer)) return [EMPTY_INTERVAL, EMPTY_INTERVAL];
  const inner = sphereInside(h0, mu, base, R);
  if (isEmpty(inner)) return [clipInterval(outer, t0, t1), EMPTY_INTERVAL];
  return [
    clipInterval({ lo: outer.lo, hi: inner.lo }, t0, t1),
    clipInterval({ lo: inner.hi, hi: outer.hi }, t0, t1),
  ];
}

/** Total path length inside the slab within [t0, t1]. */
export function slabPathLength(h0: number, mu: number, base: number, top: number, t0: number, t1: number): number {
  const [a, b] = slabIntervals(h0, mu, base, top, t0, t1);
  return intervalLength(a) + intervalLength(b);
}

/**
 * Height above MSL of the point at distance t along a ray from height h0 with vertical cosine mu.
 * Uses h = (t^2 + 2(R + h0) mu t + h0^2 + 2R h0) / (|Q| + R), which keeps precision in float32.
 */
export function pointHeight(h0: number, mu: number, t: number, R = EARTH_RADIUS_M): number {
  const num = t * t + 2 * (R + h0) * mu * t + h0 * h0 + 2 * R * h0;
  const q = Math.sqrt((R + h0) * (R + h0) + 2 * (R + h0) * mu * t + t * t);
  return num / (q + R);
}

/**
 * Origin parameters (height above MSL and vertical cosine) of a ray starting at local point p
 * (camera foot point at the origin, z up, Earth centre at (0, 0, -R)) with unit direction d.
 */
export function rayOriginParams(p: Vec3, d: Vec3, R = EARTH_RADIUS_M): { h0: number; mu: number } {
  const qz = R + p[2];
  const qlen = Math.sqrt(p[0] * p[0] + p[1] * p[1] + qz * qz);
  const h0 = (p[0] * p[0] + p[1] * p[1] + p[2] * p[2] + 2 * R * p[2]) / (qlen + R);
  const mu = (p[0] * d[0] + p[1] * d[1] + qz * d[2]) / qlen;
  return { h0, mu };
}

/** Height above MSL of a point given in the local frame (camera foot point at the origin). */
export function localPointHeight(p: Vec3, R = EARTH_RADIUS_M): number {
  const qz = R + p[2];
  const qlen = Math.sqrt(p[0] * p[0] + p[1] * p[1] + qz * qz);
  return (p[0] * p[0] + p[1] * p[1] + p[2] * p[2] + 2 * R * p[2]) / (qlen + R);
}
