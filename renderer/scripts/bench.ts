/**
 * pnpm bench [--frames 30] [--backends swiftshader,gpu] [--sizes 1280x720,1920x1080] [--out file.json]
 *
 * Render time per frame, measured in the page as render() + a 1-pixel readPixels barrier (Chromium's
 * gl.finish() does not wait for the GPU). Two scenes: 'clear' (no slabs) and 'heavy' (three slabs, fog,
 * haze, Sun shadows). Inputs are the synthetic plate set resampled to the frame size. Each steady frame
 * advances the valid time by 1 min and moves the Sun with it (uniforms only), as scrubbing does.
 * Readback is reported in two parts, both page side: readPixels + row flip, then base64 encoding for the
 * trip to Node; the transfer itself is not timed.
 *
 * SwiftShader is a CPU rasteriser: its numbers are NOT representative of WKWebView on the M4 GPU. The
 * PRD targets (<= 8 ms at 1080p steady state, <= 50 ms first frame) are measured later in WKWebView
 * (M2); the 'gpu' backend here is Chromium's ANGLE/Metal path, a closer but still different stack.
 *
 * The report records its provenance (RENDERER_VERSION, the git commit and whether the renderer's files
 * differ from it) and the machine's load average before and after each backend, since other work on a
 * fanless machine moves these times by up to 2x.
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { RawImage } from '../src/cpu/reference.ts';
import { parseArgs, userPath } from '../src/node/args.ts';
import { goldenScenePath, loadSyntheticInputs } from '../src/node/goldens.ts';
import { HeadlessRenderer, type Backend } from '../src/node/headless.ts';
import { resample } from '../src/node/images.ts';
import { platformInfo, rendererCacheDir } from '../src/node/paths.ts';
import { gitState, loadAverage } from '../src/node/provenance.ts';
import { readScene } from '../src/node/render-files.ts';
import { solarPosition } from '../src/ephemeris/sun.ts';
import { parseUtcSeconds } from '../src/time.ts';
import type { SceneSpec } from '../src/scene.ts';
import { RENDERER_VERSION } from '../src/version.ts';

const { values } = parseArgs(process.argv.slice(2), { values: ['frames', 'backends', 'sizes', 'out'], flags: [] });
const FRAMES = Number(values.frames ?? 30);
const BACKENDS = (values.backends ?? 'swiftshader,gpu').split(',') as Backend[];
const SIZES = (values.sizes ?? '1280x720,1920x1080').split(',').map((s) => s.split('x').map(Number) as [number, number]);

function scenes(width: number, height: number): Record<string, SceneSpec> {
  const clear = readScene(goldenScenePath('clear'));
  const heavy = readScene(goldenScenePath('low-deck'));
  for (const s of [clear, heavy]) {
    s.camera.width = width;
    s.camera.height = height;
  }
  heavy.forecast.layers = [
    { name: 'low', base_m_msl: 2300, thickness_m: 600, cover_profile: [{ distance_km: 0, cover_0_1: 0.45 }, { distance_km: 30, cover_0_1: 0.7 }] },
    { name: 'mid', base_m_msl: 4800, thickness_m: 900, cover_profile: [{ distance_km: 0, cover_0_1: 0.5 }] },
    { name: 'high', base_m_msl: 8500, thickness_m: 1500, cover_profile: [{ distance_km: 0, cover_0_1: 0.4 }] },
  ];
  heavy.forecast.visibility_m = 8000;
  return { clear, heavy };
}

function stats(xs: number[]): { n: number; p50: number; p95: number; mean: number; min: number; max: number } {
  const s = [...xs].sort((a, b) => a - b);
  const q = (p: number) => s[Math.min(s.length - 1, Math.ceil(p * s.length) - 1)]!;
  const r = (v: number) => Number(v.toFixed(2));
  return { n: s.length, p50: r(q(0.5)), p95: r(q(0.95)), mean: r(s.reduce((a, b) => a + b, 0) / s.length), min: r(s[0]!), max: r(s[s.length - 1]!) };
}

async function benchBackend(backend: Backend): Promise<Record<string, unknown>> {
  let runner: HeadlessRenderer;
  try {
    runner = await HeadlessRenderer.launch({ backend });
  } catch (e) {
    return { backend, available: false, reason: e instanceof Error ? e.message : String(e) };
  }
  const glRenderer = String(runner.glInfo.unmaskedRenderer ?? runner.glInfo.renderer);
  const software = /swiftshader|llvmpipe|software/i.test(glRenderer);
  const result: Record<string, unknown> = {
    backend,
    available: !(backend === 'gpu' && software),
    gl_renderer: glRenderer,
    browser: `chromium ${runner.browserVersion}`,
    launch_ms: Number(runner.launchMs.toFixed(0)),
    load_average_before: loadAverage(),
    runs: [] as unknown[],
  };
  if (backend === 'gpu' && software) {
    result.reason = 'Chromium fell back to a software renderer; no GPU numbers';
    await runner.close();
    return result;
  }
  const base = loadSyntheticInputs();
  try {
    let first = true;
    for (const [w, h] of SIZES) {
      const inputs: { plate: RawImage; skyMask: RawImage; depth: RawImage } = {
        plate: resample(base.plate, w, h, 'bilinear'),
        skyMask: resample(base.skyMask, w, h, 'nearest'),
        depth: resample(base.depth, w, h, 'nearest'),
      };
      for (const [name, scene] of Object.entries(scenes(w, h))) {
        const firstFrame = await runner.renderTimed(scene, inputs);
        const t0 = Date.parse(scene.valid_time_utc);
        const steady: number[] = [];
        for (let i = 0; i < FRAMES + 2; i++) {
          const valid = new Date(t0 + (i + 1) * 60_000).toISOString().replace('.000Z', 'Z');
          const sun = solarPosition(parseUtcSeconds(valid)!, scene.camera.lat, scene.camera.lon);
          const s = { ...scene, valid_time_utc: valid, sun: { alt_deg: sun.altDeg, az_deg: sun.azDeg } };
          const t = await runner.renderTimed(s, inputs);
          if (i >= 2) steady.push(t.renderMs);
        }
        const rb = await runner.render(scene, inputs);
        (result.runs as unknown[]).push({
          size: `${w}x${h}`,
          scene: name,
          first_frame_ms: Number(firstFrame.renderMs.toFixed(2)),
          first_frame_note: first ? 'includes shader compile, JIT and uploads' : 'program already compiled; includes uploads',
          first_frame_uploaded: firstFrame.uploaded,
          steady_ms: stats(steady),
          readpixels_and_flip_ms: Number(rb.timing.readbackMs.toFixed(1)),
          base64_encode_ms: Number(rb.timing.encodeMs.toFixed(1)),
        });
        console.error(`${backend} ${w}x${h} ${name}: first ${firstFrame.renderMs.toFixed(1)} ms, steady p50 ${stats(steady).p50} ms p95 ${stats(steady).p95} ms`);
        first = false;
        await runner.clearInputs();
      }
    }
  } finally {
    await runner.close();
  }
  result.load_average_after = loadAverage();
  return result;
}

async function main(): Promise<void> {
  const report: Record<string, unknown> = {
    renderer_version: RENDERER_VERSION,
    git: gitState(),
    measured_utc: new Date().toISOString(),
    platform: platformInfo(),
    method:
      'page-side wall time of render() + 1-pixel readPixels barrier; steady = same inputs, valid time +1 min per frame with the Sun moved to match (uniforms only), after 2 warm-up frames; ' +
      'readback = full-frame readPixels + row flip and, separately, base64 encoding, both in the page (transfer to Node not timed)',
    caveat: 'SwiftShader is a CPU rasteriser and not representative of WKWebView on the GPU; PRD targets (<= 8 ms @1080p steady, <= 50 ms first frame) are measured in WKWebView in M2',
    frames: FRAMES,
    backends: [] as unknown[],
  };
  for (const b of BACKENDS) (report.backends as unknown[]).push(await benchBackend(b));
  const json = `${JSON.stringify(report, null, 2)}\n`;
  const out = values.out ? userPath(values.out) : join(rendererCacheDir(), `bench-${new Date().toISOString().replace(/[:.]/g, '-')}.json`);
  mkdirSync(join(out, '..'), { recursive: true });
  writeFileSync(out, json);
  console.log(json);
  console.error(`written to ${out}`);
}

main().catch((e: unknown) => {
  console.error(e instanceof Error ? e.stack : e);
  process.exit(1);
});
