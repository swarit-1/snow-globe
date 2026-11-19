/**
 * Determinism (PRD §9.5): the same inputs give byte-identical PNGs, across separate browser launches
 * and through the `pnpm render` CLI; another forecast run changes the clouds; scrubbing is continuous,
 * also across forecast hours with changing winds; and a producer's rounding of the wind does not move
 * them. Outputs go to a temporary directory outside the repo and are removed afterwards.
 */
import { execFile } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { pixelDiff } from '../src/metrics/ssim.ts';
import { goldenScenePath, loadSyntheticInputs } from '../src/node/goldens.ts';
import { HeadlessRenderer } from '../src/node/headless.ts';
import { PACKAGE_ROOT, SYNTHETIC_PLATE_FILES } from '../src/node/paths.ts';
import { readScene, renderToFiles } from '../src/node/render-files.ts';
import { windAt } from '../src/math/advection.ts';
import { deriveSeed, leadSeconds, type SceneSpec } from '../src/scene.ts';

const run = promisify(execFile);
const dir = mkdtempSync(join(tmpdir(), 'lookout-renderer-test-'));
const scenePath = goldenScenePath('low-deck');
const fileArgs = { scenePath, platePath: SYNTHETIC_PLATE_FILES.plate, skyMaskPath: SYNTHETIC_PLATE_FILES.skyMask, depthPath: SYNTHETIC_PLATE_FILES.depth };

afterAll(() => rmSync(dir, { recursive: true, force: true }));

describe('byte-identical renders', () => {
  it('two separate browser launches write byte-identical PNGs for the same inputs', async () => {
    const a = await renderToFiles({ ...fileArgs, outPath: join(dir, 'a.png') });
    const b = await renderToFiles({ ...fileArgs, outPath: join(dir, 'b.png') });
    expect(readFileSync(join(dir, 'a.png')).equals(readFileSync(join(dir, 'b.png')))).toBe(true);
    expect(a.output.sha256).toBe(b.output.sha256);
    expect(a.scene_hash).toBe(b.scene_hash);
    expect(a.gl_renderer).toMatch(/SwiftShader/);
    const meta = JSON.parse(readFileSync(join(dir, 'a.json'), 'utf8'));
    for (const k of ['renderer_version', 'gl_renderer', 'scene_hash', 'timing_ms']) expect(meta).toHaveProperty(k);
    console.log(`determinism: ${a.output.sha256} twice; render ${a.timing_ms.render.toFixed(1)} / ${b.timing_ms.render.toFixed(1)} ms, total incl. launch ${a.timing_ms.total.toFixed(0)} / ${b.timing_ms.total.toFixed(0)} ms`);
  });

  it('the pnpm render CLI runs end to end and matches the library output', async () => {
    const out = join(dir, 'cli.png');
    const { stdout } = await run(
      process.execPath,
      [join(PACKAGE_ROOT, 'scripts/render.ts'), '--scene', scenePath, '--plate', fileArgs.platePath, '--sky-mask', fileArgs.skyMaskPath, '--depth', fileArgs.depthPath, '--out', out],
      { cwd: PACKAGE_ROOT },
    );
    const summary = JSON.parse(stdout);
    expect(summary.gl_renderer).toMatch(/SwiftShader/);
    expect(existsSync(join(dir, 'cli.json'))).toBe(true);
    expect(readFileSync(out).equals(readFileSync(join(dir, 'a.png')))).toBe(true);
  });

  it('refuses a scene issued for another renderer version unless told to allow it', async () => {
    const old = JSON.parse(readFileSync(scenePath, 'utf8'));
    old.renderer_version = '0.0.9';
    const oldPath = join(dir, 'old-version.json');
    writeFileSync(oldPath, JSON.stringify(old));
    await expect(renderToFiles({ ...fileArgs, scenePath: oldPath, outPath: join(dir, 'old.png') })).rejects.toThrow(/renderer_version/);
    const meta = await renderToFiles({ ...fileArgs, scenePath: oldPath, outPath: join(dir, 'old.png'), allowVersionMismatch: true });
    expect(meta.scene_renderer_version).toBe('0.0.9');
  });

  it('refuses inputs that disagree with the scene plate flags', async () => {
    await expect(renderToFiles({ ...fileArgs, skyMaskPath: null, outPath: join(dir, 'y.png') })).rejects.toThrow(/has_sky_mask/);
    await expect(renderToFiles({ ...fileArgs, depthPath: null, outPath: join(dir, 'y.png') })).rejects.toThrow(/has_depth/);
  });

  it('refuses plate, mask or depth files other than the ones scene.plate names (render stays pure in the scene)', async () => {
    for (const field of ['sha256', 'sky_mask_sha256', 'depth_sha256'] as const) {
      const other = JSON.parse(readFileSync(scenePath, 'utf8'));
      other.plate[field] = 'f'.repeat(64);
      const p = join(dir, `other-${field}.json`);
      writeFileSync(p, JSON.stringify(other));
      await expect(renderToFiles({ ...fileArgs, scenePath: p, outPath: join(dir, 'z.png') })).rejects.toThrow(/scene\.plate names/);
    }
    expect(existsSync(join(dir, 'z.png'))).toBe(false);
  });

  it('refuses to write a render inside the repository (CLAUDE.md: renders live under $LOOKOUT_DATA_DIR)', async () => {
    const inside = join(PACKAGE_ROOT, 'refused-render.png');
    await expect(renderToFiles({ ...fileArgs, outPath: inside })).rejects.toThrow(/inside the repository/);
    expect(existsSync(inside)).toBe(false);
    await expect(
      run(process.execPath, [join(PACKAGE_ROOT, 'scripts/render.ts'), '--scene', scenePath, '--plate', fileArgs.platePath, '--sky-mask', fileArgs.skyMaskPath, '--depth', fileArgs.depthPath, '--out', inside], { cwd: PACKAGE_ROOT }),
    ).rejects.toThrow(/inside the repository/);
    expect(existsSync(inside)).toBe(false);
  });
});

describe('seed, valid time and scrubbing', () => {
  let runner: HeadlessRenderer;
  const inputs = loadSyntheticInputs();
  const base = readScene(scenePath);
  const at = (patch: (s: SceneSpec) => void): SceneSpec => {
    const s = JSON.parse(JSON.stringify(base)) as SceneSpec;
    patch(s);
    return s;
  };
  const meanDiff = async (a: SceneSpec, b: SceneSpec, opts: { allowSyntheticSun?: boolean } = {}) => {
    const ra = await runner.render(a, inputs, opts);
    const rb = await runner.render(b, inputs, opts);
    return pixelDiff({ width: ra.width, height: ra.height, data: ra.rgba }, { width: rb.width, height: rb.height, data: rb.rgba });
  };
  beforeAll(async () => {
    runner = await HeadlessRenderer.launch();
  });
  afterAll(async () => {
    await runner?.close();
  });

  it('re-rendering in one session is identical', async () => {
    expect((await meanDiff(base, base)).identical).toBe(true);
  });

  it('another forecast run (its own run_init_utc and seed) gives different clouds', async () => {
    const d = await meanDiff(
      base,
      at((s) => {
        s.run_init_utc = '2026-11-18T13:00:00Z';
        s.seed = deriveSeed(s.viewpoint_id, s.run_init_utc);
      }),
    );
    expect(d.identical).toBe(false);
  });

  it("a producer's rounding of the wind moves almost no pixels (review finding: advection was wind x Unix time)", async () => {
    // Declared before measuring: float32 rounding of a 6.3 m/s wind changes <= 0.1 % of pixels (renderer
    // 0.1.0: 4,083 of 57,600 = 7.1 %), and a 0.01 m/s difference (252 m of drift after 7 h) changes less
    // than one 60 s scrub step at that wind (378 m of drift).
    const w = at((s) => (s.forecast.wind_u_ms = 6.3));
    const t = Date.parse(base.valid_time_utc);
    const n = base.camera.width * base.camera.height;
    const dFround = await meanDiff(w, at((s) => (s.forecast.wind_u_ms = Math.fround(6.3))));
    const d001 = await meanDiff(w, at((s) => (s.forecast.wind_u_ms = 6.31)));
    const d60 = await meanDiff(
      w,
      at((s) => {
        s.forecast.wind_u_ms = 6.3;
        s.valid_time_utc = new Date(t + 60_000).toISOString().replace('.000Z', 'Z');
      }),
    );
    console.log(
      `wind rounding: float32 changes ${dFround.differingPixels} of ${n} pixels; +0.01 m/s mean |diff| ${d001.meanAbsDiff.toFixed(3)} ` +
        `vs a 60 s scrub ${d60.meanAbsDiff.toFixed(3)} (8-bit levels)`,
    );
    expect(dFround.differingPixels / n).toBeLessThanOrEqual(0.001);
    expect(d001.meanAbsDiff).toBeLessThan(d60.meanAbsDiff);
  });

  it('scrubbing across a forecast hour with changing hourly winds stays as smooth as with a constant wind (review finding)', async () => {
    // Hourly winds 6 -> 9 -> 3 m/s around lead 7 h (the slope changes at 19:00). Declared before
    // measuring: a 60 s step across 19:00 changes the frame by 0.5-2x what the same step does under a
    // constant 9 m/s wind (renderer 0.1.0 decorrelated the clouds, about like a 3 h jump).
    const series = [
      { lead_h: 6, u_ms: 6, v_ms: -2 },
      { lead_h: 7, u_ms: 9, v_ms: -2 },
      { lead_h: 8, u_ms: 3, v_ms: -2 },
    ];
    const t = Date.parse(base.valid_time_utc);
    const iso = (ms: number) => new Date(ms).toISOString().replace('.000Z', 'Z');
    const withSeries = (ms: number) =>
      at((s) => {
        s.valid_time_utc = iso(ms);
        s.forecast.wind_series = series;
        [s.forecast.wind_u_ms, s.forecast.wind_v_ms] = windAt(series, leadSeconds(s));
      });
    const constant = (ms: number) =>
      at((s) => {
        s.valid_time_utc = iso(ms);
        s.forecast.wind_u_ms = 9;
        s.forecast.wind_v_ms = -2;
      });
    const dSeries = await meanDiff(withSeries(t - 30_000), withSeries(t + 30_000));
    const dConst = await meanDiff(constant(t - 30_000), constant(t + 30_000));
    const ratio = dSeries.meanAbsDiff / dConst.meanAbsDiff;
    console.log(`hour boundary: 60 s step mean |diff| ${dSeries.meanAbsDiff.toFixed(3)} with hourly winds vs ${dConst.meanAbsDiff.toFixed(3)} constant (ratio ${ratio.toFixed(2)})`);
    expect(ratio).toBeGreaterThanOrEqual(0.5);
    expect(ratio).toBeLessThanOrEqual(2);
  });

  it('scrubbing is continuous: the change shrinks with the time step instead of jumping', async () => {
    // The Sun is held where it is at the base time (a synthetic Sun) so only the clouds move.
    const t = Date.parse(base.valid_time_utc);
    const later = (ms: number) => at((s) => (s.valid_time_utc = new Date(t + ms).toISOString().replace('Z', '').replace(/\.\d+$/, '') + 'Z'));
    const fixedSun = { allowSyntheticSun: true };
    const d6 = await meanDiff(base, later(6_000), fixedSun);
    const d60 = await meanDiff(base, later(60_000), fixedSun);
    const d3h = await meanDiff(base, later(3 * 3600_000), fixedSun);
    console.log(`scrub: mean |diff| ${d6.meanAbsDiff.toFixed(3)} after 6 s, ${d60.meanAbsDiff.toFixed(3)} after 60 s, ${d3h.meanAbsDiff.toFixed(3)} after 3 h (8-bit levels)`);
    expect(d6.meanAbsDiff).toBeLessThan(d60.meanAbsDiff);
    expect(d60.meanAbsDiff).toBeLessThan(d3h.meanAbsDiff);
    // edges move with the wind: 10x less time should give several times less change (a reseed would not)
    expect(d6.meanAbsDiff * 3).toBeLessThan(d60.meanAbsDiff);
    expect(d60.meanAbsDiff * 3).toBeLessThan(d3h.meanAbsDiff);
  });
});
