/**
 * The Hillaire 2020 atmosphere on the GPU (SwiftShader, float32) against the CPU reference (float64)
 * (PRD §9.2 pass 1, D-013): every lookup table and the light pass texel for texel, whole frames pixel for
 * pixel, the light on the plate as the integral of the sky that is drawn, and the sky's symmetry about
 * the Sun's vertical plane.
 *
 * Tolerances (README "GPU vs CPU agreement"):
 * - tables: every texel and channel within 1e-3 relative plus 1e-6 of the table's largest value (float32
 *   arithmetic over up to ~150 cells, and metre-level float32 geometry where a slice ends on the ground);
 * - one exception, in the transmittance table: a ray whose lowest point lies just below the haze top
 *   crosses the haze in a chord of length 2 sqrt(2 r delta), which float32 cannot place (its ray is a few
 *   centimetres of perigee height off the float64 one). Such a texel may differ by more, but must then lie
 *   within the tolerance of the CPU's values for the rays within ZENITH_COSINE_SLACK of its zenith cosine
 *   (about 0.2 m of perigee height at mu = -0.15), and at most MAX_CONDITIONED of them per table may need it;
 * - frames ('hdr' output, the exposed radiance before the tone curve): >= 99.5 % of pixels within 1 % in
 *   every channel (plus 1e-4 of the frame's mean), and the mean relative difference <= 1e-3. A pixel may
 *   differ more where float32 flips a cloud-mask sample at its threshold (as in gpu-cpu.test.ts).
 * These were set after an exploratory comparison found SwiftShader's cos and acos off by up to 1.9e-4 and
 * 6.7e-5 (the atmosphere GLSL now avoids them); the measured maxima are logged and in README.md. The
 * transmittance exception was added after a sweep of 13 camera heights x 3 hazes (2026-11-23) found 21
 * texel-channels outside the tolerance, every one on a ray whose perigee lay 0.1-5 m below the haze top.
 */
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { buildFrame, type FrameParams } from '../src/frame.ts';
import { TRANSMITTANCE_LUT_HEIGHT, TRANSMITTANCE_LUT_WIDTH, transmittanceUvToHMu, type AtmosphereParams } from '../src/atmosphere/model.ts';
import { computeFrameAtmosphere, opticalDepthToTop, skyIrradiance, type Lut } from '../src/cpu/atmosphere.ts';
import { cpuShade } from '../src/cpu/shade.ts';
import type { RawImage } from '../src/cpu/reference.ts';
import { goldenScenePath, loadSyntheticInputs } from '../src/node/goldens.ts';
import { HeadlessRenderer } from '../src/node/headless.ts';
import { readScene } from '../src/node/render-files.ts';
import type { SceneSpec } from '../src/scene.ts';
import type { AtmosphereReadback, FloatImage } from '../src/webgl/renderer.ts';

const SYNTHETIC_SUN = { allowSyntheticSun: true };
const solid = (w: number, h: number, v: number): RawImage => ({ width: w, height: h, data: new Uint8Array(w * h * 4).fill(v).map((x, i) => (i % 4 === 3 ? 255 : x)) });

/** Zenith-cosine slack of the transmittance table's conditioned texels (see the header). */
const ZENITH_COSINE_SLACK = 2e-7;
/** At most this many texel-channels per table may need the slack. */
const MAX_CONDITIONED = 16;

/**
 * The CPU's transmittance range over the rays within ZENITH_COSINE_SLACK of a transmittance texel's
 * zenith cosine (channel c of texel index px), for the conditioned texels near the haze top.
 */
function transmittanceBracket(p: AtmosphereParams, px: number, c: number): [number, number] {
  const i = px % TRANSMITTANCE_LUT_WIDTH;
  const j = Math.floor(px / TRANSMITTANCE_LUT_WIDTH);
  const { h, mu } = transmittanceUvToHMu(p, (i + 0.5) / TRANSMITTANCE_LUT_WIDTH, (j + 0.5) / TRANSMITTANCE_LUT_HEIGHT);
  const lo = Math.exp(-opticalDepthToTop(p, h, Math.max(mu - ZENITH_COSINE_SLACK, -1))[c]!);
  const hi = Math.exp(-opticalDepthToTop(p, h, Math.min(mu + ZENITH_COSINE_SLACK, 1))[c]!);
  return [Math.min(lo, hi), Math.max(lo, hi)];
}

/**
 * Max relative difference of a GPU table against its CPU twin, and the texel-channels over the declared
 * tolerance. With bracket (the transmittance table), a texel over it counts as conditioned instead when
 * the GPU value is within the tolerance of the CPU's range over the slack.
 */
function compareTable(name: string, g: FloatImage, c: Lut, bracket?: (px: number, ch: number) => [number, number]): { worstRel: number; over: number; conditioned: number; worstConditioned: number } {
  expect([g.width, g.height], name).toEqual([c.width, c.height]);
  let max = 0;
  for (let i = 0; i < c.data.length; i++) if (i % 4 !== 3) max = Math.max(max, Math.abs(c.data[i]!));
  let worstRel = 0;
  let over = 0;
  let conditioned = 0;
  let worstConditioned = 0;
  for (let i = 0; i < c.data.length; i++) {
    if (i % 4 === 3) continue;
    const a = g.data[i]!;
    const b = c.data[i]!;
    expect(Number.isFinite(a), `${name}[${i}]`).toBe(true);
    const err = Math.abs(a - b);
    const tol = 1e-3 * Math.abs(b) + 1e-6 * max;
    if (err > tol) {
      const br = bracket?.(Math.floor(i / 4), i % 4);
      if (br && a >= br[0] - tol && a <= br[1] + tol) {
        conditioned++;
        worstConditioned = Math.max(worstConditioned, err / Math.abs(b));
        continue;
      }
      over++;
    }
    if (Math.abs(b) > 1e-6 * max) worstRel = Math.max(worstRel, err / Math.abs(b));
  }
  return { worstRel, over, conditioned, worstConditioned };
}

function sunScene(base: SceneSpec, alt: number): SceneSpec {
  const s = JSON.parse(JSON.stringify(base)) as SceneSpec;
  s.sun.alt_deg = alt;
  return s;
}

/** The clear golden scene with the camera at another height and another haze (the same Sun). */
function cameraScene(elevM: number, hazePerKm: number): SceneSpec {
  const s = readScene(goldenScenePath('clear'));
  s.camera.elev_m_msl = elevM;
  s.haze.beta_per_km = hazePerKm;
  return s;
}

describe('Hillaire 2020 atmosphere: GPU against the CPU reference', () => {
  let runner: HeadlessRenderer;
  beforeAll(async () => {
    runner = await HeadlessRenderer.launch({ backend: 'swiftshader' });
  });
  afterAll(async () => {
    await runner?.close();
  });

  const cases: [string, () => SceneSpec, boolean][] = [
    ['clear, Sun 22 deg', () => readScene(goldenScenePath('clear')), false],
    ['low deck with cirrus, sunset (Sun 0 deg)', () => sunScene(readScene(goldenScenePath('low-deck')), 0), true],
    ['low deck with cirrus, civil dusk (Sun -6 deg)', () => sunScene(readScene(goldenScenePath('low-deck')), -6), true],
    ['fog, Sun 13 deg', () => readScene(goldenScenePath('fog')), false],
    // Away from the goldens' camera: a coastal camera, a hazy mid-height one, and a high one in thick haze
    // (the sweep's worst conditioned transmittance texel, 4.8 %, is in the last).
    ['camera at 150 m, haze 0.1 per km', () => cameraScene(150, 0.1), true],
    ['camera at 808 m, haze 0.3 per km', () => cameraScene(808, 0.3), true],
    ['camera at 6,000 m, haze 0.3 per km', () => cameraScene(6000, 0.3), true],
  ];

  for (const [name, make, synthetic] of cases) {
    it(`tables and light pass agree texel for texel: ${name}`, async () => {
      const scene = make();
      await runner.render(scene, loadSyntheticInputs(), synthetic ? SYNTHETIC_SUN : {});
      const gpu: AtmosphereReadback = await runner.readAtmosphere();
      const frame = buildFrame(scene);
      const cpu = computeFrameAtmosphere(frame);
      const pairs: [string, FloatImage, Lut][] = [
        ['transmittance', gpu.transmittance, cpu.transmittance],
        ['multiScattering', gpu.multiScattering, cpu.multiScattering],
        ['skyView', gpu.skyView, cpu.skyView],
        ['skyViewBelow', gpu.skyViewBelow, cpu.skyViewBelow],
        ['light', gpu.light, cpu.lightLut],
        ['apIncScatter', gpu.apIncScatter, cpu.apIncScatter],
        ['apIncTransmittance', gpu.apIncTransmittance, cpu.apIncTransmittance],
        ['apScatter', gpu.apScatter, cpu.apScatter],
        ['apTransmittance', gpu.apTransmittance, cpu.apTransmittance],
      ];
      const bracket = (px: number, ch: number) => transmittanceBracket(frame.atmosphere.params, px, ch);
      const rows = pairs.map(([n, g, c]) => ({ n, ...compareTable(n, g, c, n === 'transmittance' ? bracket : undefined) }));
      console.log(
        `${name}: ${rows.map((r) => `${r.n} ${r.worstRel.toExponential(1)}${r.over ? ` (${r.over} over)` : ''}${r.conditioned ? ` (${r.conditioned} conditioned, up to ${r.worstConditioned.toExponential(1)})` : ''}`).join(', ')}`,
      );
      for (const r of rows) {
        expect(r.over, `${name}: ${r.n}`).toBe(0);
        expect(r.conditioned, `${name}: ${r.n} conditioned`).toBeLessThanOrEqual(MAX_CONDITIONED);
      }
    });
  }

  it('the light on the plate is the sky that is drawn: the light pass integrates the GPU\'s own sky-view table', async () => {
    const scene = readScene(goldenScenePath('clear'));
    await runner.render(scene, loadSyntheticInputs());
    const gpu = await runner.readAtmosphere();
    const frame = buildFrame(scene);
    const table: Lut = { width: gpu.skyView.width, height: gpu.skyView.height, data: Float64Array.from(gpu.skyView.data) };
    const e = skyIrradiance(frame.atmosphere.params, table, frame.atmosphere.viewHeightM);
    let worst = 0;
    for (let c = 0; c < 3; c++) worst = Math.max(worst, Math.abs(gpu.light.data[4 * 4 + c]! - frame.light.solarLux * e[c]!) / (frame.light.solarLux * e[c]!));
    console.log(`sky irradiance: light pass vs the integral of the drawn sky-view table, max relative difference ${worst.toExponential(2)}`);
    expect(worst).toBeLessThan(1e-4);
  });

  async function compareFrames(name: string, scene: SceneSpec, inputs: { plate: RawImage; skyMask?: RawImage | null; depth?: RawImage | null }, opts = {}): Promise<void> {
    const g = await runner.renderDebug(scene, inputs, 'hdr', opts);
    const frame: FrameParams = buildFrame(scene);
    const c = cpuShade(frame, computeFrameAtmosphere(frame), inputs);
    const n = c.width * c.height;
    let mean = 0;
    for (let i = 0; i < n; i++) for (let ch = 0; ch < 3; ch++) mean += c.data[i * 4 + ch]! / (3 * n);
    let within = 0;
    let sumRel = 0;
    let worst = 0;
    for (let i = 0; i < n; i++) {
      let ok = true;
      for (let ch = 0; ch < 3; ch++) {
        const a = g.data[i * 4 + ch]!;
        const b = c.data[i * 4 + ch]!;
        expect(Number.isFinite(a)).toBe(true);
        const rel = Math.abs(a - b) / (Math.abs(b) + 1e-4 * mean);
        sumRel += rel / 3;
        worst = Math.max(worst, rel);
        if (rel > 0.01) ok = false;
      }
      if (ok) within++;
    }
    console.log(`${name}: ${((100 * within) / n).toFixed(3)} % of pixels within 1 %, mean relative difference ${(sumRel / n).toExponential(2)}, worst ${worst.toExponential(2)}`);
    expect(within / n).toBeGreaterThanOrEqual(0.995);
    expect(sumRel / n).toBeLessThanOrEqual(1e-3);
  }

  for (const golden of ['clear', 'low-deck', 'fog'] as const) {
    it(`frames agree pixel for pixel: the ${golden} golden scene (plate, mask and depth)`, async () => {
      await compareFrames(golden, readScene(goldenScenePath(golden)), loadSyntheticInputs());
    });
  }

  it('frames agree pixel for pixel: an all-sky view at sunset towards the Sun, and at civil dusk with a deck', async () => {
    const W = 160;
    const H = 90;
    const s = readScene(goldenScenePath('low-deck'));
    s.camera = { ...s.camera, width: W, height: H, heading_deg: 240, pitch_deg: 6, hfov_deg: 90 };
    s.plate = { id: s.plate.id, sha256: s.plate.sha256, has_sky_mask: true, sky_mask_sha256: s.plate.sky_mask_sha256, has_depth: false };
    s.sun = { alt_deg: 0.5, az_deg: 240 };
    s.forecast.layers = s.forecast.layers.filter((l) => l.name === 'high');
    await compareFrames('all-sky sunset', s, { plate: solid(W, H, 128), skyMask: solid(W, H, 255) }, SYNTHETIC_SUN);
    const t = readScene(goldenScenePath('low-deck'));
    t.camera = { ...t.camera, width: W, height: H, heading_deg: 240, pitch_deg: -2, hfov_deg: 90 };
    t.plate = { id: t.plate.id, sha256: t.plate.sha256, has_sky_mask: false, has_depth: false, nominal_depth_m: 60_000 };
    t.sun = { alt_deg: -6, az_deg: 240 };
    await compareFrames('terrain and deck at civil dusk', t, { plate: solid(W, H, 150) }, SYNTHETIC_SUN);
  });

  it('is symmetric about the Sun vertical plane: mirrored pixels and froxel columns match', async () => {
    const W = 128;
    const H = 72;
    const s = readScene(goldenScenePath('clear'));
    s.camera = { ...s.camera, width: W, height: H, heading_deg: 200, pitch_deg: 10, roll_deg: 0, hfov_deg: 100 };
    s.plate = { id: s.plate.id, sha256: s.plate.sha256, has_sky_mask: true, sky_mask_sha256: s.plate.sky_mask_sha256, has_depth: false };
    s.forecast.visibility_m = null;
    s.sun = { alt_deg: 8, az_deg: 200 };
    const r = await runner.renderDebug(s, { plate: solid(W, H, 128), skyMask: solid(W, H, 255) }, 'hdr', SYNTHETIC_SUN);
    let worst = 0;
    for (let j = 0; j < H; j++) {
      for (let i = 0; i < W / 2; i++) {
        for (let c = 0; c < 3; c++) {
          const a = r.data[(j * W + i) * 4 + c]!;
          const b = r.data[(j * W + (W - 1 - i)) * 4 + c]!;
          worst = Math.max(worst, Math.abs(a - b) / Math.max(Math.abs(a), Math.abs(b), 1e-12));
        }
      }
    }
    const atm = await runner.readAtmosphere();
    let worstAp = 0;
    const AW = atm.apScatter.width;
    for (let j = 0; j < 32; j++) {
      for (let k = 0; k < 32; k++) {
        for (let i = 0; i < 16; i++) {
          for (let c = 0; c < 3; c++) {
            const a = atm.apScatter.data[(j * AW + k * 32 + i) * 4 + c]!;
            const b = atm.apScatter.data[(j * AW + k * 32 + (31 - i)) * 4 + c]!;
            if (Math.max(a, b) > 0) worstAp = Math.max(worstAp, Math.abs(a - b) / Math.max(a, b));
          }
        }
      }
    }
    console.log(`symmetry about the Sun vertical plane: worst mirrored-pixel difference ${worst.toExponential(2)}, froxel columns ${worstAp.toExponential(2)}`);
    expect(worst).toBeLessThan(1e-4);
    expect(worstAp).toBeLessThan(1e-4);
  });
});
