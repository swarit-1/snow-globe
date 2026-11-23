import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { buildFrame } from './frame.ts';
import { computeBaseLuts, computeFrameAtmosphere } from './cpu/atmosphere.ts';
import { clearAirExtinction } from './atmosphere/model.ts';
import { validateScene, type SceneSpec } from './scene.ts';

const root = fileURLToPath(new URL('..', import.meta.url));
const scene = (name: string): SceneSpec => validateScene(JSON.parse(readFileSync(`${root}/goldens/scenes/${name}.json`, 'utf8')));
const clone = (s: SceneSpec): SceneSpec => JSON.parse(JSON.stringify(s));

describe('light model on the atmosphere (PRD §9.2 passes 2, 4 and 8)', () => {
  it('buildFrame gives finite light inputs, and the light is finite with a positive exposure at every Sun altitude', () => {
    const s = clone(scene('low-deck'));
    const base = computeBaseLuts(buildFrame(s).atmosphere.params);
    for (let a = -90; a <= 90; a += 7.5) {
      s.sun.alt_deg = a;
      const f = buildFrame(validateScene(s, { allowSyntheticSun: true }));
      const inputs = [f.light.solarLux, f.light.tAmb, f.light.tDirExp, f.light.tBeamTot, f.light.moonLux, f.light.fogAirWeight, ...f.light.sunDiskL, ...f.light.moonDiskL];
      for (const v of inputs) expect(Number.isFinite(v), `alt ${a}`).toBe(true);
      const L = computeFrameAtmosphere(f, base).light;
      const values = [...L.eDir, ...L.eDif, ...L.eCam, L.eCamY, L.exposure, L.cloudDim, ...L.fogAir, ...L.skyBackground, ...L.layerRadiance.flat()];
      for (const v of values) {
        expect(Number.isFinite(v), `alt ${a}`).toBe(true);
        expect(v).toBeGreaterThanOrEqual(0);
      }
      expect(L.exposure).toBeGreaterThan(0);
      expect(L.cloudDim).toBeLessThanOrEqual(1);
    }
  });

  it('the light on the plate is the sky that is drawn: the diffuse light is the sky-view table integrated over the hemisphere', () => {
    const f = buildFrame(scene('clear'));
    const atm = computeFrameAtmosphere(f);
    // No clouds and no Moon: the diffuse light is the Sun-lit sky's irradiance plus the night sky's (pi L_night).
    const nightIrr = atm.light.skyBackground.map((x) => Math.PI * x);
    for (let c = 0; c < 3; c++) expect(atm.light.eDif[c]).toBeCloseTo(atm.light.eSky[c]! + nightIrr[c]!, 6);
    expect(atm.light.eSky[1]).toBeGreaterThan(1000);
  });

  it('dims the expected illumination under an overcast deck above the camera', () => {
    const s = clone(scene('clear'));
    const clearF = buildFrame(s);
    const base = computeBaseLuts(clearF.atmosphere.params);
    const clear = computeFrameAtmosphere(clearF, base).light;
    s.forecast.layers = [{ name: 'low', base_m_msl: 2500, thickness_m: 600, cover_profile: [{ distance_km: 0, cover_0_1: 1 }] }];
    const f = buildFrame(s);
    expect(f.atmosphere.shadeTopM).toBe(3100);
    const overcast = computeFrameAtmosphere(f, base).light;
    expect(overcast.eCamY).toBeLessThan(0.5 * clear.eCamY);
    expect(overcast.eCamY).toBeGreaterThan(0.05 * clear.eCamY);
    expect(overcast.cloudDim).toBeLessThan(0.5);
    expect(clear.cloudDim).toBe(1);
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
    // the excess over the clear air at the ground: molecules, background aerosol and the 0.05 /km haze (550 nm)
    expect(f.fog.betaPerM).toBeCloseTo(3.912023 / 1500 - clearAirExtinction(f.atmosphere.params, 1640)[1], 9);
    expect(clearAirExtinction(f.atmosphere.params, 1640)[1]).toBeCloseTo(0.05e-3 + 13.558e-6 * Math.exp(-1640 / 8000) + 4.44e-6 * Math.exp(-1640 / 1200), 12);
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

  it('puts the clear-air haze into the atmosphere as a 2 km boundary layer and blends fog airlight by density', () => {
    const f = buildFrame(scene('clear'));
    expect(f.haze.betaPerM).toBeCloseTo(3e-5, 12);
    expect(f.haze.topM).toBe(3640);
    expect(f.atmosphere.params.hazeExtinction[1]).toBeCloseTo(3e-5, 12);
    expect(f.atmosphere.params.hazeTopM).toBe(3640);
    expect(f.light.fogAirWeight).toBeLessThan(0.06); // 50 km visibility: haze-like airlight
    expect(buildFrame(scene('fog')).light.fogAirWeight).toBeGreaterThan(0.9); // 1.5 km: dense fog
  });

  it('only slabs above the camera\'s ground shade it; the camera height is clamped into the atmosphere', () => {
    const f = buildFrame(scene('low-deck')); // deck 1,000-1,400 m below the 1,640 m ground; cirrus 8-9.5 km
    expect(f.atmosphere.shadeTopM).toBe(9500);
    const s = clone(scene('clear'));
    expect(buildFrame(s).atmosphere.shadeTopM).toBe(-Infinity);
    s.camera.elev_m_msl = -400;
    expect(buildFrame(s).atmosphere.viewHeightM).toBe(10);
  });
});
