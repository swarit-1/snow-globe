import { describe, expect, it } from 'vitest';
import { EARTH_RADIUS_M as R } from './constants.ts';
import { intervalLength, isEmpty, localPointHeight, pointHeight, rayOriginParams, slabIntervals, sphereInside } from './shell.ts';

/** Reference: solve |C + t d| = R + h with C = (0, 0, R + h0) in float64 by the textbook formula. */
function naiveRoots(h0: number, mu: number, h: number): [number, number] | null {
  const b = (R + h0) * mu;
  const c = (R + h0) ** 2 - (R + h) ** 2;
  const disc = b * b - c;
  if (disc < 0) return null;
  return [-b - Math.sqrt(disc), -b + Math.sqrt(disc)];
}

/** The same stable algorithm as sphereInside, but with every operation rounded to float32 (the GPU's highp). */
function sphereInsideF32(h0: number, mu: number, h: number): [number, number] {
  const f = Math.fround;
  const RR = f(R);
  const B = f(f(RR + f(h0)) * f(mu));
  const C = f(f(f(h0) - f(h)) * f(f(f(2 * RR) + f(h0)) + f(h)));
  const disc = f(f(B * B) - C);
  const sq = f(Math.sqrt(disc));
  const q = f(-f(B + (B >= 0 ? sq : -sq)));
  const t2 = f(C / q);
  return q <= t2 ? [q, t2] : [t2, q];
}

/** The textbook formula rounded to float32: what a naive shader would do. */
function naiveF32(h0: number, mu: number, h: number): number {
  const f = Math.fround;
  const b = f(f(f(R) + f(h0)) * f(mu));
  const c = f(f(f(f(R) + f(h0)) ** 2) - f(f(f(R) + f(h)) ** 2));
  return f(-b + f(Math.sqrt(f(f(b * b) - c))));
}

describe('ray vs spherical shell (Earth curvature, R = 6,371,000 m)', () => {
  it('vertical rays reach a layer after exactly its height difference', () => {
    const iv = sphereInside(1650, 1, 2650);
    expect(iv.hi).toBeCloseTo(1000, 9);
    expect(iv.lo).toBeLessThan(0);
  });

  it('a horizontal ray meets a layer 1 km up at sqrt(2 R dh) (curvature), not never (flat Earth)', () => {
    const iv = sphereInside(1650, 0, 2650);
    const expected = Math.sqrt((R + 2650) ** 2 - (R + 1650) ** 2);
    expect(iv.hi).toBeCloseTo(expected, 6);
    expect(iv.hi / 1000).toBeCloseTo(112.9, 1);
  });

  it('matches the float64 textbook solution over a wide range of geometry', () => {
    let worst = 0;
    for (let k = 0; k < 2000; k++) {
      const h0 = -100 + ((k * 7919) % 9000);
      const h = -100 + ((k * 104729) % 15000);
      const mu = Math.cos((k * 0.6180339887 * Math.PI) % Math.PI);
      const iv = sphereInside(h0, mu, h);
      const ref = naiveRoots(h0, mu, h);
      if (ref === null) {
        expect(isEmpty(iv)).toBe(true);
        continue;
      }
      // compare where the textbook form is itself well-conditioned
      for (const [a, b] of [
        [iv.lo, ref[0]],
        [iv.hi, ref[1]],
      ] as const) {
        const err = Math.abs(a - b) / Math.max(1, Math.abs(b));
        worst = Math.max(worst, err);
      }
    }
    expect(worst).toBeLessThan(1e-6);
  });

  it('stays metre-accurate in float32 where the textbook formula fails', () => {
    // camera at 1650 m, layers 100 m to 8 km above, rays from 0.5 deg to the zenith
    let worstStable = 0;
    let worstNaive = 0;
    for (const dh of [100, 500, 1000, 3000, 8000]) {
      for (const elDeg of [0.5, 2, 5, 10, 30, 60, 90]) {
        const mu = Math.sin((elDeg * Math.PI) / 180);
        const exact = sphereInside(1650, mu, 1650 + dh).hi;
        const stable = sphereInsideF32(1650, mu, 1650 + dh)[1];
        worstStable = Math.max(worstStable, Math.abs(stable - exact));
        worstNaive = Math.max(worstNaive, Math.abs(naiveF32(1650, mu, 1650 + dh) - exact));
      }
    }
    console.log(`float32 intersection error: stable form ${worstStable.toFixed(3)} m, textbook form ${worstNaive.toFixed(3)} m`);
    expect(worstStable).toBeLessThan(1.0);
    // why the shader uses the factored, cancellation-free form: the textbook form is an order of magnitude worse
    expect(worstNaive).toBeGreaterThan(5);
    expect(worstNaive).toBeGreaterThan(10 * worstStable);
  });

  it('the solved intersection point lies at the layer height', () => {
    for (const [h0, mu, h] of [
      [1650, 0.05, 2400],
      [100, 0.001, 600],
      [3000, -0.2, 1200],
    ] as const) {
      const iv = sphereInside(h0, mu, h);
      const t = iv.hi > 0 && h > h0 ? iv.hi : iv.lo;
      expect(pointHeight(h0, mu, t)).toBeCloseTo(h, 5);
    }
  });

  it('slab cases: below, inside, above, and a grazing double crossing', () => {
    // camera below a 2000-2600 m slab, looking 30 deg up: one crossing from base to top (the far interval,
    // since the near one would lie behind the camera)
    const mu = Math.sin(Math.PI / 6);
    let [a, b] = slabIntervals(1650, mu, 2000, 2600, 0, 1e9);
    expect(intervalLength(a)).toBe(0);
    expect(b.lo).toBeCloseTo(sphereInside(1650, mu, 2000).hi, 6);
    expect(b.hi).toBeCloseTo(sphereInside(1650, mu, 2600).hi, 6);

    // camera inside the slab looking up: the path starts at the camera
    [a, b] = slabIntervals(2200, mu, 2000, 2600, 0, 1e9);
    expect(intervalLength(a)).toBe(0);
    expect(b.lo).toBe(0);
    expect(b.hi).toBeCloseTo(sphereInside(2200, mu, 2600).hi, 6);

    // camera above a 1000-1400 m deck looking 5 deg down: enters the top, leaves through the base
    const down = -Math.sin((5 * Math.PI) / 180);
    [a, b] = slabIntervals(1650, down, 1000, 1400, 0, 1e9);
    expect(a.lo).toBeCloseTo(sphereInside(1650, down, 1400).lo, 6);
    expect(a.hi).toBeCloseTo(sphereInside(1650, down, 1000).lo, 6);
    expect(pointHeight(1650, down, a.lo)).toBeCloseTo(1400, 4);
    // and a ground hit at 4 km clips the path
    [a] = slabIntervals(1650, down, 1000, 1400, 0, 4000);
    expect(a.hi).toBe(4000);

    // camera above the deck looking 0.8 deg down: the ray bottoms out at ~1029 m (curvature), so it dips
    // into the top of the deck and climbs out again without reaching the base
    const graze = -Math.sin((0.8 * Math.PI) / 180);
    [a, b] = slabIntervals(1650, graze, 1000, 1500, 0, 1e9);
    expect(intervalLength(a)).toBeGreaterThan(0);
    expect(isEmpty(sphereInside(1650, graze, 1000))).toBe(true);
    expect(intervalLength(b)).toBe(0);
  });

  it('origin parameters of a camera point are its height and direction z', () => {
    const p = rayOriginParams([0, 0, 1650], [0.6, 0, 0.8]);
    expect(p.h0).toBeCloseTo(1650, 9);
    expect(p.mu).toBeCloseTo(0.8, 12);
    // a point 30 km east at local z = 0 is above the curved surface by ~70.6 m
    expect(localPointHeight([30000, 0, 0])).toBeCloseTo(Math.hypot(30000, R) - R, 6);
  });
});
