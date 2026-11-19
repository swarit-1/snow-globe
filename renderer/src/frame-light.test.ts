import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { buildFrame } from './frame.ts';
import {
  airMass,
  luminanceY,
  moonNormalIlluminance,
  skyDiffuseIlluminance,
  skyWeights,
  sunNormalIlluminance,
  sunVisibility,
  zenithLuminance,
} from './light.ts';
import { validateScene, type SceneSpec } from './scene.ts';

const root = fileURLToPath(new URL('..', import.meta.url));
const scene = (name: string): SceneSpec => validateScene(JSON.parse(readFileSync(`${root}/goldens/scenes/${name}.json`, 'utf8')));
const clone = (s: SceneSpec): SceneSpec => JSON.parse(JSON.stringify(s));

const SAMPLES = (() => {
  const a: number[] = [];
  for (let x = -90; x <= 90.0001; x += 0.01) a.push(Math.round(x * 100) / 100);
  return a;
})();

/** Largest absolute change between neighbouring samples spaced h degrees apart over [-90, 90]. */
function maxStep(f: (alt: number) => number, h: number): number {
  let worst = 0;
  const n = Math.round(180 / h);
  let prev = f(-90);
  for (let i = 1; i <= n; i++) {
    const v = f(-90 + i * h);
    worst = Math.max(worst, Math.abs(v - prev));
    prev = v;
  }
  return worst;
}

/**
 * Continuity check: for a continuous (Lipschitz) function, shrinking the step 10x shrinks the largest
 * step ~10x; at a jump it stays the same. Returns maxStep(0.01) / maxStep(0.001).
 */
function stepRatio(f: (alt: number) => number): number {
  return maxStep(f, 0.01) / maxStep(f, 0.001);
}

describe('sky v0 and light model: finite, non-negative, continuous through sunset and twilight', () => {
  const fns: [string, (alt: number) => number][] = [
    ['zenith luminance', zenithLuminance],
    ['sky diffuse illuminance', (a) => luminanceY(skyDiffuseIlluminance(a))],
    ['direct normal sunlight', (a) => luminanceY(sunNormalIlluminance(a))],
    ['moonlight (k = 0.7)', (a) => luminanceY(moonNormalIlluminance(a, 0.7))],
    ['air mass', airMass],
  ];
  for (const [name, f] of fns) {
    it(`${name} is finite and >= 0 for every altitude`, () => {
      for (const a of SAMPLES) {
        const v = f(a);
        expect(Number.isFinite(v), `${name} at ${a}`).toBe(true);
        expect(v).toBeGreaterThanOrEqual(0);
      }
    });
  }

  for (const [name, f] of fns) {
    it(`${name} has no jumps anywhere in [-90, 90] (largest step shrinks ~10x when the step does)`, () => {
      const r = stepRatio(f);
      expect(r, `${name}: ratio ${r}`).toBeGreaterThan(5);
    });
  }

  it('the continuity check does catch a jump (control)', () => {
    expect(stepRatio((a) => (a < 0.3 ? 1 : 2) + a / 90)).toBeLessThan(1.5);
  });

  it('day, twilight and night weights partition unity smoothly', () => {
    let prev = skyWeights(-90);
    for (const a of SAMPLES) {
      const w = skyWeights(a);
      expect(w.day + w.twilight + w.night).toBeCloseTo(1, 12);
      for (const k of ['day', 'twilight', 'night'] as const) {
        expect(w[k]).toBeGreaterThanOrEqual(0);
        expect(Math.abs(w[k] - prev[k])).toBeLessThan(0.003);
      }
      prev = w;
    }
    expect(skyWeights(30).day).toBe(1);
    expect(skyWeights(-30).night).toBe(1);
    expect(skyWeights(-8).twilight).toBe(1);
  });

  it('orders brightness sensibly: day > sunset > civil > nautical > night', () => {
    expect(zenithLuminance(45)).toBeGreaterThan(zenithLuminance(0));
    expect(zenithLuminance(0)).toBeGreaterThan(zenithLuminance(-6));
    expect(zenithLuminance(-6)).toBeGreaterThan(zenithLuminance(-12));
    expect(zenithLuminance(-12)).toBeGreaterThan(zenithLuminance(-30));
    expect(sunVisibility(-1)).toBe(0);
    expect(sunVisibility(1)).toBe(1);
    // the low Sun is redder than the high Sun
    const lo = sunNormalIlluminance(2);
    const hi = sunNormalIlluminance(60);
    expect(lo[2] / lo[0]).toBeLessThan(hi[2] / hi[0]);
  });

  it('buildFrame gives finite light and exposure at every Sun altitude, including below the horizon', () => {
    const s = clone(scene('low-deck'));
    for (let a = -90; a <= 90; a += 0.5) {
      s.sun.alt_deg = a;
      const L = buildFrame(s).light;
      const values = [...L.eDir, ...L.eDif, L.eCamY, L.exposure, ...L.sunDiskL, ...L.glow, L.cloudDim, ...L.fogAir, L.fogAirWeight, L.sky.zenithL];
      for (const v of values) expect(Number.isFinite(v), `alt ${a}`).toBe(true);
      expect(L.exposure).toBeGreaterThan(0);
      expect(L.cloudDim).toBeGreaterThanOrEqual(0);
      expect(L.cloudDim).toBeLessThanOrEqual(1);
    }
  });
});

describe('frame parameters (slabs, ceiling, fog, haze)', () => {
  it('orders declared layers by slot and keeps the cover profile', () => {
    const s = clone(scene('low-deck'));
    s.forecast.layers.reverse();
    const f = buildFrame(s);
    expect(f.layers.map((l) => l.kind)).toEqual(['low', 'high']);
    expect(f.layers[0]!.topM).toBe(1400);
    expect(f.layers[0]!.profile.length).toBe(3);
  });

  it('adds a solid 300 m ceiling slab only when the ceiling is not inside a declared layer', () => {
    const s = clone(scene('low-deck'));
    s.forecast.ceiling_m_msl = 2500;
    let f = buildFrame(s);
    const ceil = f.layers.find((l) => l.kind === 'ceiling')!;
    expect(ceil.solid).toBe(true);
    expect([ceil.baseM, ceil.topM]).toEqual([2500, 2800]);
    s.forecast.ceiling_m_msl = 1200; // inside the 1000-1400 m low layer
    f = buildFrame(s);
    expect(f.layers.some((l) => l.kind === 'ceiling')).toBe(false);
  });

  it('confines near-surface fog to 100 m above the v0 flat ground, raised to a surface-based deck top', () => {
    const s = clone(scene('fog'));
    let f = buildFrame(s);
    expect(f.hGround).toBe(1640);
    expect(f.fog.topM).toBe(1740);
    expect(f.fog.betaPerM).toBeCloseTo(3.912023 / 1500 - 0.05 / 1000, 9);
    // a ceiling at the ground with no declared low layer: fog fills the 300 m ceiling slab
    s.forecast.ceiling_m_msl = 1650;
    f = buildFrame(s);
    expect(f.fog.topM).toBe(1950);
    // a ceiling well above the near-surface layer does not raise the fog
    s.forecast.ceiling_m_msl = 3000;
    f = buildFrame(s);
    expect(f.fog.topM).toBe(1740);
    // no visibility: no fog
    s.forecast.visibility_m = null;
    expect(buildFrame(s).fog.betaPerM).toBe(0);
  });

  it('puts clear-air haze in a 2 km boundary layer and blends fog airlight by density', () => {
    const f = buildFrame(scene('clear'));
    expect(f.haze.betaPerM).toBeCloseTo(3e-5, 12);
    expect(f.haze.topM).toBe(3640);
    expect(f.light.fogAirWeight).toBeLessThan(0.06); // 50 km visibility: haze-like airlight
    expect(buildFrame(scene('fog')).light.fogAirWeight).toBeGreaterThan(0.9); // 1.5 km: dense fog
  });

  it('dims the expected illumination under an overcast deck above the camera', () => {
    const s = clone(scene('clear'));
    const clear = buildFrame(s).light.eCamY;
    s.forecast.layers = [{ name: 'low', base_m_msl: 2500, thickness_m: 600, cover_profile: [{ distance_km: 0, cover_0_1: 1 }] }];
    const overcast = buildFrame(s).light.eCamY;
    expect(overcast).toBeLessThan(0.5 * clear);
    expect(overcast).toBeGreaterThan(0.05 * clear);
  });
});
