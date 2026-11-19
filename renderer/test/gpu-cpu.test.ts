/**
 * GPU (SwiftShader, float32) against the CPU reference (float64) on small renders.
 *
 * Agreement threshold, declared before measuring and documented in README.md:
 * - ray directions: max component difference <= 1e-5; ray ends: relative difference <= 1e-5;
 * - mean cover at the sample points ('target' output): max |difference| <= 1e-3 per layer;
 * - coverage kappa (the thresholded mask): >= 99.5 % of pixels identical per layer, and the mean kappa
 *   within 0.002. A pixel may legitimately differ only when a sample's noise lies within float32 rounding
 *   of its threshold, or when float32 puts the path length on the other side of a sample-count step;
 * - drawn slab opacity alpha (what is composited, and what the Monte Carlo's "drawn" path computes):
 *   >= 99.5 % of pixels within 2e-3 per layer, and the mean alpha within 0.002.
 */
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { buildFrame } from '../src/frame.ts';
import { pixelRay } from '../src/math/camera.ts';
import { cpuCoverage, decodeDepth, pixelRayEnd, type RawImage } from '../src/cpu/reference.ts';
import { goldenScenePath, loadSyntheticInputs } from '../src/node/goldens.ts';
import { HeadlessRenderer } from '../src/node/headless.ts';
import { readScene } from '../src/node/render-files.ts';
import type { SceneSpec } from '../src/scene.ts';

const solid = (w: number, h: number, v: number): RawImage => ({ width: w, height: h, data: new Uint8Array(w * h * 4).fill(v).map((x, i) => (i % 4 === 3 ? 255 : x)) });

function threeLayerSkyScene(w: number, h: number): SceneSpec {
  const s = readScene(goldenScenePath('clear'));
  s.camera.width = w;
  s.camera.height = h;
  s.camera.pitch_deg = 25;
  s.camera.hfov_deg = 100;
  s.plate.has_depth = false;
  delete s.plate.depth_sha256;
  s.forecast.layers = [
    { name: 'low', base_m_msl: 2400, thickness_m: 700, cover_profile: [{ distance_km: 0, cover_0_1: 0.35 }, { distance_km: 20, cover_0_1: 0.7 }, { distance_km: 80, cover_0_1: 0.5 }] },
    { name: 'mid', base_m_msl: 4500, thickness_m: 900, cover_profile: [{ distance_km: 0, cover_0_1: 0.5 }] },
    { name: 'high', base_m_msl: 8500, thickness_m: 1500, cover_profile: [{ distance_km: 0, cover_0_1: 0.6 }, { distance_km: 150, cover_0_1: 0.2 }] },
  ];
  s.forecast.ceiling_m_msl = 6000;
  return s;
}

interface Agreement {
  layer: number;
  identical: number;
  meanGpu: number;
  meanCpu: number;
  maxTargetDiff: number;
  alphaAgree: number;
  meanAlphaGpu: number;
  meanAlphaCpu: number;
  /** Share of pixels where the CPU reference draws this slab at opacity >= 0.5. */
  drawnShare: number;
}

describe('GPU vs CPU reference', () => {
  let runner: HeadlessRenderer;
  beforeAll(async () => {
    runner = await HeadlessRenderer.launch({ backend: 'swiftshader' });
  });
  afterAll(async () => {
    await runner?.close();
  });

  async function compare(scene: SceneSpec, inputs: { plate: RawImage; skyMask?: RawImage | null; depth?: RawImage | null }): Promise<Agreement[]> {
    const frame = buildFrame(scene);
    const cpu = cpuCoverage(frame, { skyMask: inputs.skyMask ?? null, depth: inputs.depth ?? null });
    const gk = await runner.renderDebug(scene, inputs, 'coverage');
    const gt = await runner.renderDebug(scene, inputs, 'target');
    const ga = await runner.renderDebug(scene, inputs, 'alpha');
    const n = frame.width * frame.height;
    return frame.layers.map((_, li) => {
      let same = 0;
      let sg = 0;
      let sc = 0;
      let maxT = 0;
      let aSame = 0;
      let sag = 0;
      let sac = 0;
      let drawn = 0;
      for (let i = 0; i < n; i++) {
        const g = gk.data[i * 4 + li]!;
        const c = cpu.kappa[li]![i]!;
        if (Math.abs(g - c) < 1e-6) same++;
        sg += g;
        sc += c;
        maxT = Math.max(maxT, Math.abs(gt.data[i * 4 + li]! - cpu.target[li]![i]!));
        const ag = ga.data[i * 4 + li]!;
        const ac = cpu.alpha[li]![i]!;
        if (Math.abs(ag - ac) <= 2e-3) aSame++;
        sag += ag;
        sac += ac;
        if (ac >= 0.5) drawn++;
      }
      return {
        layer: li,
        identical: same / n,
        meanGpu: sg / n,
        meanCpu: sc / n,
        maxTargetDiff: maxT,
        alphaAgree: aSame / n,
        meanAlphaGpu: sag / n,
        meanAlphaCpu: sac / n,
        drawnShare: drawn / n,
      };
    });
  }

  function check(name: string, rows: Agreement[]): void {
    for (const r of rows) {
      console.log(
        `${name} layer ${r.layer}: kappa identical ${(100 * r.identical).toFixed(3)} %, mean kappa GPU ${r.meanGpu.toFixed(4)} CPU ${r.meanCpu.toFixed(4)}, ` +
          `max target diff ${r.maxTargetDiff.toExponential(2)}; alpha within 2e-3 ${(100 * r.alphaAgree).toFixed(3)} %, ` +
          `mean alpha GPU ${r.meanAlphaGpu.toFixed(4)} CPU ${r.meanAlphaCpu.toFixed(4)}; drawn (alpha >= 0.5) on ${(100 * r.drawnShare).toFixed(1)} % of pixels`,
      );
      expect(r.identical).toBeGreaterThanOrEqual(0.995);
      expect(Math.abs(r.meanGpu - r.meanCpu)).toBeLessThanOrEqual(0.002);
      expect(r.maxTargetDiff).toBeLessThanOrEqual(1e-3);
      expect(r.alphaAgree).toBeGreaterThanOrEqual(0.995);
      expect(Math.abs(r.meanAlphaGpu - r.meanAlphaCpu)).toBeLessThanOrEqual(0.002);
    }
  }

  it('ray directions and ray ends match the camera model', async () => {
    const scene = readScene(goldenScenePath('low-deck'));
    const inputs = loadSyntheticInputs();
    const frame = buildFrame(scene);
    const r = await runner.renderDebug(scene, inputs, 'ray');
    let maxDir = 0;
    let maxEnd = 0;
    for (let j = 0; j < frame.height; j++) {
      for (let i = 0; i < frame.width; i++) {
        const k = (j * frame.width + i) * 4;
        const d = pixelRay(frame.basis, i + 0.5, j + 0.5);
        for (let c = 0; c < 3; c++) maxDir = Math.max(maxDir, Math.abs(r.data[k + c]! - d[c]!));
        const depth = decodeDepth(inputs.depth.data[k]!, inputs.depth.data[k + 1]!, inputs.depth.data[k + 2]!);
        const end = pixelRayEnd(frame, d, inputs.skyMask.data[k]! / 255, depth).tEnd;
        maxEnd = Math.max(maxEnd, Math.abs(r.data[k + 3]! - end) / end);
      }
    }
    console.log(`rays: max direction diff ${maxDir.toExponential(2)}, max relative ray-end diff ${maxEnd.toExponential(2)}`);
    expect(maxDir).toBeLessThanOrEqual(1e-5);
    expect(maxEnd).toBeLessThanOrEqual(1e-5);
  });

  it('coverage agrees: all-sky view, three layers plus a ceiling slab, distance-varying cover (160 x 90)', async () => {
    const scene = threeLayerSkyScene(160, 90);
    check('sky', await compare(scene, { plate: solid(160, 90, 128), skyMask: solid(160, 90, 255) }));
  });

  it('coverage agrees: synthetic plate with sky mask and depth, deck below the camera (320 x 180)', async () => {
    const rows = await compare(readScene(goldenScenePath('low-deck')), loadSyntheticInputs());
    check('low-deck', rows);
    // not vacuous: the deck is drawn on a real share of the frame (renderer 0.1.0: ~48 pixels)
    expect(rows[0]!.drawnShare).toBeGreaterThanOrEqual(0.1);
  });

  it('coverage agrees: deck below the camera over flat ground, no mask or depth, every ray below the horizon (160 x 90)', async () => {
    // A single field realization can be largely clear within a few km of the camera (kappa matches the
    // cover only on average: 0.393 vs 0.373 over 60 runs of this geometry, measured 2026-11-19), so the
    // view is aimed where this run's deck is present; the drawn-share check below keeps it non-vacuous.
    const s = readScene(goldenScenePath('low-deck'));
    s.camera = { ...s.camera, width: 160, height: 90, heading_deg: 120, pitch_deg: -35, hfov_deg: 80, agl_m: 1000 }; // ground at 650 m
    s.plate = { id: s.plate.id, sha256: s.plate.sha256, has_sky_mask: false, has_depth: false, nominal_depth_m: 200_000 };
    s.forecast.layers = [{ name: 'low', base_m_msl: 1000, thickness_m: 400, cover_profile: [{ distance_km: 0, cover_0_1: 0.5 }, { distance_km: 4, cover_0_1: 0.8 }] }];
    const frame = buildFrame(s);
    expect(pixelRay(frame.basis, 0.5, 0.5)[2]).toBeLessThan(0); // the top row looks down too
    const rows = await compare(s, { plate: solid(160, 90, 128) });
    check('deck-below-flat', rows);
    expect(rows[0]!.drawnShare).toBeGreaterThanOrEqual(0.1);
  });

  it('coverage agrees: no mask and no depth (terrain at the flat ground or the nominal depth)', async () => {
    const scene = threeLayerSkyScene(96, 54);
    scene.plate.has_sky_mask = false;
    delete scene.plate.sky_mask_sha256;
    scene.plate.nominal_depth_m = 40_000;
    check('nominal', await compare(scene, { plate: solid(96, 54, 128) }));
  });
});
