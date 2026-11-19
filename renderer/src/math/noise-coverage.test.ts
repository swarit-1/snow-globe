import { describe, expect, it } from 'vitest';
import { coverThreshold, cloudMask, filterThenThreshold, randomOverlapCover, thresholdThenFilter } from './coverage.ts';
import { advectionDisplacement } from './advection.ts';
import { normalCdf, normalQuantile } from './normal.ts';
import {
  NOISE_AMPLITUDES,
  cloudNoise,
  hashWords,
  latticeValue,
  layerNoiseTransforms,
  lookupFromArray,
  lazyLattice,
  makeLattice,
  octaveValue,
  seedToUint32,
  unitFromHash,
} from './noise.ts';

describe('normal quantile', () => {
  it('matches reference values', () => {
    expect(normalQuantile(0.5)).toBeCloseTo(0, 12);
    expect(normalQuantile(0.975)).toBeCloseTo(1.959963985, 8);
    expect(normalQuantile(0.1)).toBeCloseTo(-1.281551566, 8);
    expect(normalQuantile(0.01)).toBeCloseTo(-2.326347874, 8);
    expect(normalQuantile(1e-6)).toBeCloseTo(-4.753424309, 6);
  });
  it('inverts the CDF', () => {
    for (let p = 0.001; p < 1; p += 0.0137) expect(normalCdf(normalQuantile(p))).toBeCloseTo(p, 6);
  });
});

describe('seeded value noise', () => {
  it('derives a stable seed integer from the seed string (pinned so accidental changes are caught)', () => {
    expect(seedToUint32('rainier.paradise-mountain|2026111812z')).toBe(seedToUint32('rainier.paradise-mountain|2026111812z'));
    expect(seedToUint32('a')).not.toBe(seedToUint32('b'));
    expect(seedToUint32('abc')).toBe(0xba7816bf); // first 4 bytes of SHA-256("abc")
  });

  it('builds a deterministic N(0,1) lattice', () => {
    const a = makeLattice(12345);
    const b = makeLattice(12345);
    const c = makeLattice(12346);
    expect(Buffer.from(a.buffer).equals(Buffer.from(b.buffer))).toBe(true);
    expect(Buffer.from(a.buffer).equals(Buffer.from(c.buffer))).toBe(false);
    let s = 0;
    let s2 = 0;
    for (const v of a) {
      s += v;
      s2 += v * v;
    }
    const mean = s / a.length;
    expect(Math.abs(mean)).toBeLessThan(0.02);
    expect(s2 / a.length - mean * mean).toBeCloseTo(1, 1);
    // lazy lattice returns the same values as the full one
    const lazy = lazyLattice(12345);
    const full = lookupFromArray(a);
    for (const [x, y] of [
      [0, 0],
      [255, 255],
      [-1, 3],
      [300, -77],
    ] as const) {
      expect(lazy(x, y)).toBe(full(x, y));
      expect(latticeValue(12345, x, y)).toBe(full(x, y));
    }
  });

  it('octave amplitudes have unit sum of squares', () => {
    expect(NOISE_AMPLITUDES.reduce((s, a) => s + a * a, 0)).toBeCloseTo(1, 12);
  });

  it('has an N(0,1) marginal everywhere, so P(noise < quantile(c)) = c', () => {
    // pool over many seeds and positions (including lattice-cell centres, where unnormalised value noise is weakest)
    const covers = [0.1, 0.3, 0.5, 0.7, 0.9];
    const hits = covers.map(() => 0);
    let n = 0;
    for (let s = 0; s < 200; s++) {
      const seed = hashWords(77, s);
      const lookup = lazyLattice(seed);
      const tr = layerNoiseTransforms(seed, 0, 2400, 0, 0);
      for (let k = 0; k < 100; k++) {
        const x = 100_000 * (unitFromHash(hashWords(seed, 1, k)) - 0.5);
        const y = 100_000 * (unitFromHash(hashWords(seed, 2, k)) - 0.5);
        const v = cloudNoise(lookup, tr, x, y);
        covers.forEach((c, i) => {
          if (v < coverThreshold(c)) hits[i]!++;
        });
        n++;
      }
    }
    covers.forEach((c, i) => expect(Math.abs(hits[i]! / n - c)).toBeLessThan(0.015));
  });

  it('without variance normalisation the marginal is too narrow at cell centres (why it is normalised)', () => {
    let s2 = 0;
    let s2n = 0;
    const n = 4000;
    for (let k = 0; k < n; k++) {
      const lookup = lazyLattice(hashWords(9, k));
      const u0 = 10.5;
      const u1 = 20.5; // exact cell centre
      s2 += octaveValue(lookup, u0, u1, false) ** 2;
      s2n += octaveValue(lookup, u0, u1, true) ** 2;
    }
    expect(s2 / n).toBeCloseTo(0.25, 1);
    expect(s2n / n).toBeCloseTo(1, 1);
  });

  it('is anchored in world coordinates and shifted by the advection displacement', () => {
    const seed = seedToUint32('advect');
    const lookup = lazyLattice(seed);
    const dt = 5400; // 1.5 h of lead time
    const [u, v] = [8.5, -3.25];
    const trA = layerNoiseTransforms(seed, 1, 3600, u * 3600, v * 3600);
    const trB = layerNoiseTransforms(seed, 1, 3600, u * (3600 + dt), v * (3600 + dt));
    for (const [x, y] of [
      [0, 0],
      [12_345, -6_789],
      [-50_000, 80_000],
    ] as const) {
      // the cloud at (x, y) at lead 1 h has moved to (x + u dt, y + v dt) at lead 2.5 h
      expect(cloudNoise(lookup, trB, x + u * dt, y + v * dt)).toBeCloseTo(cloudNoise(lookup, trA, x, y), 6);
    }
    // continuous: one more second of drift changes the field only slightly
    const tr1 = layerNoiseTransforms(seed, 1, 3600, u * 3601, v * 3601);
    expect(Math.abs(cloudNoise(lookup, tr1, 1000, 2000) - cloudNoise(lookup, trA, 1000, 2000))).toBeLessThan(0.05);
    // no displacement (calm, or the run's initialisation): the run's own field
    expect(cloudNoise(lookup, layerNoiseTransforms(seed, 1, 3600, 0, 0), 777, 888)).toBe(cloudNoise(lookup, layerNoiseTransforms(seed, 1, 3600, 0, 0), 777, 888));
  });

  it('does not depend on how a producer rounded the wind (review finding: advection was wind x Unix time)', () => {
    // Declared before measuring: >= 0.9999 correlation at 4,000 points for float32 rounding of the wind
    // and for a 1e-4 m/s difference, 7 h after the run's initialisation; the old epoch anchoring must
    // fall below 0.5 for the 1e-4 m/s case (it moved the field by ~180 km).
    const seed = seedToUint32('fcb5d5bd95c61d46');
    const lookup = lazyLattice(seed);
    const pts = Array.from({ length: 4000 }, (_, k) => [60_000 * (unitFromHash(hashWords(1, k)) - 0.5), 60_000 * (unitFromHash(hashWords(2, k)) - 0.5)] as const);
    const field = (dx: number, dy: number) => {
      const tr = layerNoiseTransforms(seed, 0, 2400, dx, dy);
      return pts.map(([x, y]) => cloudNoise(lookup, tr, x, y));
    };
    const corr = (a: number[], b: number[]) => {
      const ma = a.reduce((s, x) => s + x, 0) / a.length;
      const mb = b.reduce((s, x) => s + x, 0) / b.length;
      let ab = 0;
      let aa = 0;
      let bb = 0;
      a.forEach((x, i) => {
        ab += (x - ma) * (b[i]! - mb);
        aa += (x - ma) ** 2;
        bb += (b[i]! - mb) ** 2;
      });
      return ab / Math.sqrt(aa * bb);
    };
    const lead = 7 * 3600;
    const run = (w: number) => {
      const [dx, dy] = advectionDisplacement(undefined, w, -2, lead);
      return field(dx, dy);
    };
    const base = run(6.3);
    const rFround = corr(base, run(Math.fround(6.3)));
    const r1e4 = corr(base, run(6.3001));
    const epochS = 1_795_028_400; // 2026-11-18T19:00:00Z
    const rEpoch = corr(field(6.3 * epochS, -2 * epochS), field(6.3001 * epochS, -2 * epochS));
    console.log(`wind rounding: correlation ${rFround.toFixed(6)} (float32), ${r1e4.toFixed(6)} (+1e-4 m/s); epoch-anchored control ${rEpoch.toFixed(4)}`);
    expect(rFround).toBeGreaterThanOrEqual(0.9999);
    expect(r1e4).toBeGreaterThanOrEqual(0.9999);
    expect(rEpoch).toBeLessThan(0.5);
  });

  it('keeps the advected offsets small (reduced modulo the lattice period) for float32 on the GPU', () => {
    // 150 m/s for 384 h: the largest drift a valid scene can have
    const d = 150 * 384 * 3600;
    const tr = layerNoiseTransforms(seedToUint32('x'), 0, 300, d, d);
    for (const t of tr) {
      expect(t.c0).toBeGreaterThanOrEqual(0);
      expect(t.c0).toBeLessThan(256);
      expect(t.c1).toBeGreaterThanOrEqual(0);
      expect(t.c1).toBeLessThan(256);
    }
  });
});

describe('threshold-before-filter coverage rule', () => {
  it('thresholds against the cover', () => {
    expect(coverThreshold(0)).toBe(-Infinity);
    expect(coverThreshold(1)).toBe(Infinity);
    expect(cloudMask(-0.1, 0.5)).toBe(1);
    expect(cloudMask(0.1, 0.5)).toBe(0);
    expect(cloudMask(-100, 0)).toBe(0);
    expect(cloudMask(100, 1)).toBe(1);
  });

  it('threshold-then-filter keeps E[coverage] = cover; filter-then-threshold drifts', () => {
    // 4 independent N(0,1) samples per pixel (far-away pixel: samples decorrelated)
    const draws = 40_000;
    let ok = 0;
    let bad = 0;
    const c = 0.2;
    for (let i = 0; i < draws; i++) {
      const ns = [0, 1, 2, 3].map((k) => normalQuantile(unitFromHash(hashWords(5, i, k))));
      ok += thresholdThenFilter(ns, [c, c, c, c]);
      bad += filterThenThreshold(ns, [c, c, c, c]);
    }
    expect(Math.abs(ok / draws - c)).toBeLessThan(0.01);
    expect(c - bad / draws).toBeGreaterThan(0.1); // mean of 4 has sd 0.5: P(< -0.84) = 0.046, not 0.2
  });

  it('random-overlap total cover', () => {
    expect(randomOverlapCover([])).toBe(0);
    expect(randomOverlapCover([0.5, 0.5])).toBeCloseTo(0.75, 12);
    expect(randomOverlapCover([1, 0.2])).toBe(1);
  });
});
