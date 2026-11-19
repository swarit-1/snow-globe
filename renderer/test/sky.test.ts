/**
 * Sky v0 on the GPU: no NaNs, infinities or negative radiances at any Sun altitude (including below the
 * horizon) or for random valid scenes, no jumps through sunset and twilight, and the Sun disk where the
 * camera model puts it (PRD §9.2 passes 1-2). The Sun is placed freely (allowSyntheticSun): these tests
 * are about the sky model's numerics and geometry, not the ephemeris.
 *
 * The hdr debug output is the exposed radiance before any clamp. Renderer 0.1.0 emitted max(L, 0), which
 * made negative values impossible and, on SwiftShader (max(NaN, 0.0) = 0), hid NaN as well.
 */
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { buildFrame } from '../src/frame.ts';
import { projectDirection } from '../src/math/camera.ts';
import { hashWords, unitFromHash } from '../src/math/noise.ts';
import type { RawImage } from '../src/cpu/reference.ts';
import { goldenScenePath, loadSyntheticInputs } from '../src/node/goldens.ts';
import { HeadlessRenderer } from '../src/node/headless.ts';
import { readScene } from '../src/node/render-files.ts';
import { LAYER_NAMES, validateScene, type SceneSpec } from '../src/scene.ts';

const SYNTHETIC_SUN = { allowSyntheticSun: true };

const W = 48;
const H = 27;

function sweepScene(alt: number): SceneSpec {
  const s = readScene(goldenScenePath('low-deck'));
  s.camera.width = W;
  s.camera.height = H;
  s.sun.alt_deg = alt;
  s.sun.az_deg = 40; // near the view direction so twilight colours and the glow are in frame
  return s;
}

describe('sky v0 on SwiftShader', () => {
  let runner: HeadlessRenderer;
  const inputs = loadSyntheticInputs();
  beforeAll(async () => {
    runner = await HeadlessRenderer.launch();
  });
  afterAll(async () => {
    await runner?.close();
  });

  async function meanHdr(alt: number): Promise<{ mean: number; bad: number }> {
    const r = await runner.renderDebug(sweepScene(alt), inputs, 'hdr', SYNTHETIC_SUN);
    let s = 0;
    let bad = 0;
    for (let i = 0; i < W * H; i++) {
      for (let c = 0; c < 3; c++) {
        const v = r.data[i * 4 + c]!;
        if (!Number.isFinite(v) || v < 0) bad++;
        else s += v;
      }
    }
    return { mean: s / (3 * W * H), bad };
  }

  it('renders finite, non-negative values for every Sun altitude from -90 to 90 degrees', async () => {
    const alts = [-90, -60, -45, -30];
    for (let a = -20; a <= 20; a += 0.5) alts.push(a);
    alts.push(30, 45, 60, 75, 90);
    let bad = 0;
    for (const a of alts) bad += (await meanHdr(a)).bad;
    console.log(`sky sweep: ${alts.length} Sun altitudes, ${bad} non-finite or negative values`);
    expect(bad).toBe(0);
  });

  // Sky-only sweep for continuity: no cloud slabs (their binary shadows move by design) and an all-sky mask.
  async function meanSkyHdr(alt: number): Promise<number> {
    const s = sweepScene(alt);
    s.forecast.layers = [];
    s.forecast.ceiling_m_msl = null;
    const sky: RawImage = { width: W, height: H, data: new Uint8Array(W * H * 4).fill(255) };
    const r = await runner.renderDebug(s, { plate: inputs.plate, skyMask: sky, depth: inputs.depth }, 'hdr', SYNTHETIC_SUN);
    let sum = 0;
    for (let i = 0; i < W * H; i++) for (let c = 0; c < 3; c++) sum += r.data[i * 4 + c]!;
    return sum / (3 * W * H);
  }

  it('has no jumps through sunset and twilight (largest step shrinks when the step does)', async () => {
    // around each transition of the v0 model: disk set (-0.9, 0.4), twilight weights (-4, 6), knots (0, -6, -10, -18)
    for (const centre of [0.4, -0.9, -4, -6, -10, -18, 6]) {
      const steps = async (h: number) => {
        let worst = 0;
        let prev = await meanSkyHdr(centre - 0.25);
        for (let a = centre - 0.25 + h; a <= centre + 0.25 + 1e-9; a += h) {
          const m = await meanSkyHdr(a);
          worst = Math.max(worst, Math.abs(Math.log((m + 1e-9) / (prev + 1e-9))));
          prev = m;
        }
        return worst;
      };
      const coarse = await steps(0.05);
      const fine = await steps(0.005);
      console.log(`continuity around ${centre} deg: max |dlog| ${coarse.toExponential(2)} per 0.05 deg, ${fine.toExponential(2)} per 0.005 deg`);
      expect(coarse / Math.max(fine, 1e-12)).toBeGreaterThan(4);
    }
  });

  it('draws the Sun disk at the pixel the camera model predicts', async () => {
    const s = readScene(goldenScenePath('clear'));
    s.camera.width = 160;
    s.camera.height = 90;
    s.sun.alt_deg = 14.3;
    s.sun.az_deg = 41.7;
    const frame = buildFrame(s);
    const expected = projectDirection(frame.basis, frame.sunDir)!;
    const sky: RawImage = { width: 160, height: 90, data: new Uint8Array(160 * 90 * 4).fill(255) };
    const r = await runner.renderDebug(s, { plate: inputs.plate, skyMask: sky, depth: inputs.depth }, 'hdr', SYNTHETIC_SUN);
    let sx = 0;
    let sy = 0;
    let sw = 0;
    for (let j = 0; j < 90; j++) {
      for (let i = 0; i < 160; i++) {
        const v = r.data[(j * 160 + i) * 4 + 1]!;
        if (v > 1000) {
          sx += v * (i + 0.5);
          sy += v * (j + 0.5);
          sw += v;
        }
      }
    }
    const got = { px: sx / sw, py: sy / sw };
    console.log(`Sun disk centroid (${got.px.toFixed(3)}, ${got.py.toFixed(3)}) vs projected (${expected.px.toFixed(3)}, ${expected.py.toFixed(3)})`);
    expect(Math.hypot(got.px - expected.px, got.py - expected.py)).toBeLessThan(0.5);
  });

  it('renders finite, non-negative radiance for 300 random valid scenes (seeded fuzz)', async () => {
    const base = readScene(goldenScenePath('low-deck'));
    const u = (k: number, i: number) => unitFromHash(hashWords(0x5eed, k, i));
    let bad = 0;
    let values = 0;
    for (let k = 0; k < 300; k++) {
      const s = JSON.parse(JSON.stringify(base)) as SceneSpec;
      s.camera.width = 32;
      s.camera.height = 18;
      s.camera.elev_m_msl = -400 + 9000 * u(k, 1);
      s.camera.agl_m = 1000 * u(k, 2) ** 3;
      s.camera.heading_deg = 360 * u(k, 3) - 180;
      s.camera.pitch_deg = 180 * u(k, 4) - 90;
      s.camera.roll_deg = 360 * u(k, 5) - 180;
      s.camera.hfov_deg = 1 + 178 * u(k, 6);
      s.sun = { alt_deg: 180 * u(k, 7) - 90, az_deg: 720 * u(k, 8) - 360 };
      s.moon = { alt_deg: 180 * u(k, 9) - 90, az_deg: 360 * u(k, 10), illuminated_fraction: u(k, 11) };
      const nLayers = Math.floor(4 * u(k, 12));
      s.forecast.layers = LAYER_NAMES.slice(0, nLayers).map((name, i) => ({
        name,
        base_m_msl: -500 + 20_000 * u(k, 20 + i),
        thickness_m: 1 + 14_999 * u(k, 30 + i) ** 2,
        cover_profile: [
          { distance_km: 0, cover_0_1: u(k, 40 + i) },
          { distance_km: 1 + 200 * u(k, 50 + i), cover_0_1: u(k, 60 + i) },
        ],
      }));
      s.forecast.ceiling_m_msl = u(k, 13) < 0.5 ? null : -500 + 20_500 * u(k, 14);
      s.forecast.visibility_m = u(k, 15) < 0.2 ? null : 10 ** (-3 + 9 * u(k, 16));
      s.haze.beta_per_km = 10 * u(k, 17) ** 3;
      validateScene(s, SYNTHETIC_SUN);
      const r = await runner.renderDebug(s, { plate: inputs.plate, skyMask: inputs.skyMask, depth: inputs.depth }, 'hdr', SYNTHETIC_SUN);
      for (let i = 0; i < 32 * 18; i++) {
        for (let c = 0; c < 3; c++) {
          const v = r.data[i * 4 + c]!;
          values++;
          if (!Number.isFinite(v) || v < 0) bad++;
        }
      }
    }
    console.log(`fuzz: 300 random scenes, ${values} values, ${bad} non-finite or negative`);
    expect(bad).toBe(0);
  });
});
