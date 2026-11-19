import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { buildFrame } from '../frame.ts';
import { altAzToDirection } from '../math/camera.ts';
import { CLOUD_MAX_SAMPLES, CLOUD_MIN_SAMPLES, CLOUD_TAU_OPAQUE, T_INFINITE_M } from '../math/constants.ts';
import { lookupFromArray, makeLattice } from '../math/noise.ts';
import { sphereInside } from '../math/shell.ts';
import { validateScene, type SceneSpec } from '../scene.ts';
import { DEPTH_SKY, cpuCoverage, decodeDepth, encodeDepth, pixelRayEnd, rayLayerCoverage, seaHit } from './reference.ts';

const root = fileURLToPath(new URL('../..', import.meta.url));
const scene = (name: string): SceneSpec => validateScene(JSON.parse(readFileSync(`${root}/goldens/scenes/${name}.json`, 'utf8')));
const clone = (s: SceneSpec): SceneSpec => JSON.parse(JSON.stringify(s));

describe('depth encoding (RGB24 metres along the ray)', () => {
  it('round-trips distances and special codes', () => {
    for (const m of [1, 37, 255, 256, 65_535, 65_536, 13_282, 1_000_000]) {
      const [r, g, b] = encodeDepth(m);
      expect(decodeDepth(r, g, b)).toBe(m);
    }
    expect(decodeDepth(...encodeDepth(Infinity))).toBe(Infinity);
    expect(encodeDepth(Infinity)).toEqual([255, 255, 255]);
    expect(decodeDepth(0, 0, 0)).toBeNull();
    expect(DEPTH_SKY).toBe(16777215);
  });
});

describe('ray ends', () => {
  const f = buildFrame(scene('clear'));
  it('sky rays run to infinity, or to the sea surface if they dip into it', () => {
    expect(pixelRayEnd(f, altAzToDirection(10, 30), 1, null)).toEqual({ tEnd: T_INFINITE_M, isSky: true });
    const down = altAzToDirection(-5, 30);
    const sea = seaHit(1650, down);
    expect(sea).toBeCloseTo(sphereInside(1650, down[2], 0).lo, 6);
    expect(pixelRayEnd(f, down, 1, null).tEnd).toBe(sea);
  });
  it('terrain rays end at the depth map; without one, at the flat ground (downward) or the nominal depth', () => {
    expect(pixelRayEnd(f, altAzToDirection(-5, 30), 0, 1234).tEnd).toBe(1234);
    // 10 m above the flat ground, 5 degrees down: ~115 m (curvature makes it a hair shorter)
    const down = pixelRayEnd(f, altAzToDirection(-5, 30), 0, null).tEnd;
    expect(down).toBeCloseTo(sphereInside(1650, altAzToDirection(-5, 30)[2], 1640).lo, 9);
    expect(down).toBeCloseTo(10 / Math.sin((5 * Math.PI) / 180), 0);
    // nearly level: the flat ground is beyond the nominal depth, so the nominal depth wins
    expect(pixelRayEnd(f, altAzToDirection(-0.01, 30), 0, null).tEnd).toBe(15_000);
    expect(pixelRayEnd(f, altAzToDirection(5, 30), null, null)).toEqual({ tEnd: 15_000, isSky: false });
    expect(pixelRayEnd(f, altAzToDirection(5, 30), 0.49, Infinity).tEnd).toBe(15_000);
    expect(pixelRayEnd(f, altAzToDirection(5, 30), 0.5, 1234).isSky).toBe(true);
  });
});

describe('slab coverage along one ray (CPU reference of pass 3)', () => {
  const s = clone(scene('low-deck'));
  const f = buildFrame(s);
  const lookup = lookupFromArray(makeLattice(f.seedU32));
  const layerAt = (cover: number, base: number, top: number, solid = false) => ({ ...f.layers[0]!, baseM: base, topM: top, solid, profile: [{ distance_km: 0, cover_0_1: cover }] });

  it('cover 0 gives no cloud and cover 1 gives full cloud; targets equal the cover', () => {
    const dir = altAzToDirection(20, 40);
    const c0 = rayLayerCoverage(f, layerAt(0, 2500, 3000), lookup, dir, T_INFINITE_M);
    const c1 = rayLayerCoverage(f, layerAt(1, 2500, 3000), lookup, dir, T_INFINITE_M);
    expect([c0.kappa, c0.target]).toEqual([0, 0]);
    expect([c1.kappa, c1.target]).toEqual([1, 1]);
    expect(c1.pathLength).toBeCloseTo(500 / Math.sin((20 * Math.PI) / 180), -1);
  });

  it('solid slabs are full cloud without noise', () => {
    const c = rayLayerCoverage(f, layerAt(0.2, 2500, 2800, true), lookup, altAzToDirection(5, 0), T_INFINITE_M);
    expect(c.kappa).toBe(1);
  });

  it('a terrain hit in front of the slab hides it (cloud only in front of the depth)', () => {
    const dir = altAzToDirection(10, 30);
    const far = rayLayerCoverage(f, layerAt(1, 2500, 3000), lookup, dir, T_INFINITE_M);
    const blocked = rayLayerCoverage(f, layerAt(1, 2500, 3000), lookup, dir, 2000);
    expect(far.pathLength).toBeGreaterThan(0);
    expect(blocked.pathLength).toBe(0);
  });

  it('a camera inside the slab starts the path at the camera', () => {
    const c = rayLayerCoverage({ hCam: 2600 }, layerAt(1, 2500, 3000), lookup, altAzToDirection(90, 0), T_INFINITE_M);
    expect(c.pathLength).toBeCloseTo(400, 6);
    expect(c.tMid).toBeCloseTo(200, 6);
  });

  it('a deck below the camera is seen from above and ends at the ground', () => {
    const dir = altAzToDirection(-10, 30);
    // a downward sky ray ends at the sea surface (with an infinite end it would cross the slab again
    // on the far side of the Earth)
    const c = rayLayerCoverage(f, layerAt(1, 1000, 1400), lookup, dir, seaHit(1650, dir));
    const tTop = sphereInside(1650, dir[2], 1400).lo;
    const tBase = sphereInside(1650, dir[2], 1000).lo;
    expect(c.pathLength).toBeCloseTo(tBase - tTop, 6);
  });

  it('noise is anchored in the world: camera heading and pitch do not change the clouds along a ray', () => {
    const a = clone(s);
    const b = clone(s);
    b.camera.heading_deg = 95;
    b.camera.pitch_deg = 20;
    const fa = buildFrame(a);
    const fb = buildFrame(b);
    for (const [el, az] of [
      [3, 10],
      [12, 200],
      [40, 77],
    ] as const) {
      const dir = altAzToDirection(el, az);
      for (let li = 0; li < fa.layers.length; li++) {
        expect(rayLayerCoverage(fb, fb.layers[li]!, lookup, dir, T_INFINITE_M)).toEqual(rayLayerCoverage(fa, fa.layers[li]!, lookup, dir, T_INFINITE_M));
      }
    }
  });

  it('takes as many samples as the path needs: one per 1/16 of the noise cell, 4 to 128', () => {
    const layer = layerAt(0.5, 2500, 2900); // low: 2,400 m cell, so a 150 m step
    expect(layer.stepM).toBe(150);
    const n = (el: number) => rayLayerCoverage(f, layer, lookup, altAzToDirection(el, 40), T_INFINITE_M).samples;
    expect(n(90)).toBe(CLOUD_MIN_SAMPLES); // 400 m path
    const p5 = rayLayerCoverage(f, layer, lookup, altAzToDirection(5, 40), T_INFINITE_M);
    expect(p5.samples).toBe(Math.ceil(p5.pathLength / 150));
    expect(n(0.3)).toBe(CLOUD_MAX_SAMPLES);
    expect(rayLayerCoverage(f, layer, lookup, altAzToDirection(5, 40), T_INFINITE_M, { variant: 'four-samples' }).samples).toBe(4);
  });

  it('draws opacity from the thresholded masks and stops sampling an opaque slab', () => {
    for (const el of [2, 8, 30]) {
      const r = rayLayerCoverage(f, layerAt(0.45, 2500, 2900), lookup, altAzToDirection(el, 75), T_INFINITE_M);
      expect(Math.abs(r.kappa * r.samples - Math.round(r.kappa * r.samples))).toBeLessThan(1e-9);
      // alpha counts masks only up to the opaque stop, so it never exceeds the full-path opacity
      const full = 1 - Math.exp(-0.03 * r.kappa * r.pathLength);
      expect(r.alpha).toBeLessThanOrEqual(full + 1e-12);
      expect(full - r.alpha).toBeLessThanOrEqual(Math.exp(-CLOUD_TAU_OPAQUE));
    }
    const solidCloud = rayLayerCoverage(f, layerAt(1, 2500, 2900), lookup, altAzToDirection(10, 75), T_INFINITE_M);
    expect(solidCloud.kappa).toBe(1);
    expect(solidCloud.alpha).toBeGreaterThanOrEqual(1 - Math.exp(-CLOUD_TAU_OPAQUE));
  });

  it('per-pixel coverage matches the frame size and keeps opacity within [0, 1]', () => {
    const t = clone(s);
    t.camera.width = 32;
    t.camera.height = 18;
    t.plate.has_sky_mask = false;
    t.plate.has_depth = false;
    const fr = buildFrame(t);
    const r = cpuCoverage(fr);
    expect(r.kappa.length).toBe(2);
    expect(r.kappa[0]!.length).toBe(32 * 18);
    for (const a of r.alpha[1]!) {
      expect(a).toBeGreaterThanOrEqual(0);
      expect(a).toBeLessThanOrEqual(1);
    }
  });
});
