/**
 * Golden images on Chromium/SwiftShader (PRD §9.5). On the platform and GL renderer that produced the
 * goldens (goldens/manifest.json) the render must match pixel for pixel; SSIM is always computed and
 * reported. On any other platform the PRD rule applies instead: SSIM >= 0.98.
 *
 * Each golden must also show its named feature: removing the feature from the scene must change at
 * least 10 % of the pixels (declared before measuring). Renderer 0.1.0's low-deck golden failed this:
 * its deck changed 48 of 57,600 pixels, so a broken deck could not have been detected.
 */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { pixelDiff, ssim } from '../src/metrics/ssim.ts';
import { assertSyntheticGolden, encodeSyntheticPlate, goldenImagePath, goldenScenePath, loadSyntheticInputs, readGoldenManifest } from '../src/node/goldens.ts';
import { HeadlessRenderer } from '../src/node/headless.ts';
import { decodePng, readImageFile } from '../src/node/images.ts';
import { GOLDEN_NAMES, SYNTHETIC_PLATE_FILES, SYNTHETIC_PLATE_MAX_BYTES, platformInfo, type GoldenName } from '../src/node/paths.ts';
import { readScene } from '../src/node/render-files.ts';
import { sceneHash, type SceneSpec } from '../src/scene.ts';
import { RENDERER_VERSION } from '../src/version.ts';

const sha = (b: Uint8Array) => createHash('sha256').update(b).digest('hex');
const manifest = readGoldenManifest();

/** For each golden, the scene without its named feature (null: no feature to remove). */
const WITHOUT_FEATURE: Record<GoldenName, ((s: SceneSpec) => void) | null> = {
  clear: null,
  'low-deck': (s) => (s.forecast.layers = s.forecast.layers.filter((l) => l.name !== 'low')),
  fog: (s) => (s.forecast.visibility_m = null),
};

describe('synthetic plate (committed fixture)', () => {
  it('is 320 x 180, <= 100 KB, and regenerates pixel for pixel from the script', () => {
    const committed = {
      plate: readFileSync(SYNTHETIC_PLATE_FILES.plate),
      skyMask: readFileSync(SYNTHETIC_PLATE_FILES.skyMask),
      depth: readFileSync(SYNTHETIC_PLATE_FILES.depth),
    };
    expect(committed.plate.length).toBeLessThanOrEqual(SYNTHETIC_PLATE_MAX_BYTES);
    const img = readImageFile(SYNTHETIC_PLATE_FILES.plate).image;
    expect([img.width, img.height]).toEqual([320, 180]);
    const regen = encodeSyntheticPlate();
    // Pass/fail on decoded pixels: the compressed bytes depend on the zlib build (CI is linux/x86-64,
    // the fixture was made on darwin/arm64). The byte hashes are reported for information only.
    for (const k of ['plate', 'skyMask', 'depth'] as const) {
      const a = decodePng(regen[k]);
      const b = decodePng(committed[k]);
      expect([a.width, a.height], k).toEqual([b.width, b.height]);
      expect(Buffer.from(a.data).equals(Buffer.from(b.data)), `${k} pixels`).toBe(true);
    }
    const sameBytes = (['plate', 'skyMask', 'depth'] as const).every((k) => sha(regen[k]) === sha(committed[k]));
    console.log(
      `synthetic plate ${committed.plate.length} B, mask ${committed.skyMask.length} B, depth ${committed.depth.length} B; ` +
        `sky ${(100 * regen.stats.skyFraction).toFixed(1)} %, summit ${regen.stats.summitDepthM?.toFixed(0)} m away; PNG bytes identical too: ${sameBytes}`,
    );
  });

  it('is the plate the golden scenes name (id and file hashes)', () => {
    const inputs = loadSyntheticInputs();
    for (const name of GOLDEN_NAMES) {
      const s = readScene(goldenScenePath(name));
      expect(() => assertSyntheticGolden(s, name)).not.toThrow();
      expect(s.plate.sha256).toBe(inputs.sha256.plate);
      expect(s.plate.sky_mask_sha256).toBe(inputs.sha256.skyMask);
      expect(s.plate.depth_sha256).toBe(inputs.sha256.depth);
    }
  });

  it('refuses to make goldens of anything but the synthetic plate (real-plate goldens stay out of the repo)', () => {
    const s = readScene(goldenScenePath('clear'));
    expect(() => assertSyntheticGolden({ ...s, viewpoint_id: 'rainier.paradise-mountain' }, 'x')).toThrow(/LOOKOUT_DATA_DIR/);
    expect(() => assertSyntheticGolden({ ...s, plate: { ...s.plate, id: 'rainier.paradise-mountain/plate-v1' } }, 'x')).toThrow(/LOOKOUT_DATA_DIR/);
  });
});

describe('golden renders on SwiftShader', () => {
  let runner: HeadlessRenderer;
  beforeAll(async () => {
    runner = await HeadlessRenderer.launch({ backend: 'swiftshader' });
  });
  afterAll(async () => {
    await runner?.close();
  });

  it('has a manifest for this renderer version', () => {
    expect(manifest).not.toBeNull();
    expect(manifest!.renderer_version).toBe(RENDERER_VERSION);
    expect(manifest!.gl_renderer).toMatch(/SwiftShader/);
  });

  for (const name of GOLDEN_NAMES) {
    it(`${name}: matches its golden`, async () => {
      const scene = readScene(goldenScenePath(name));
      expect(sceneHash(scene)).toBe(manifest!.goldens[name].scene_hash);
      // The synthetic inputs carry their file hashes, so the page also checks them against scene.plate.
      const out = await runner.render(scene, loadSyntheticInputs());
      const rendered = { width: out.width, height: out.height, data: out.rgba };
      const golden = readImageFile(goldenImagePath(name)).image;
      const d = pixelDiff(rendered, golden);
      const s = ssim(rendered, golden);
      const p = platformInfo();
      const samePlatform = manifest!.platform.os === p.os && manifest!.platform.arch === p.arch && manifest!.gl_renderer === String(runner.glInfo.unmaskedRenderer);
      console.log(
        `golden ${name}: SSIM ${s.toFixed(6)}, differing pixels ${d.differingPixels}, max channel diff ${d.maxChannelDiff}, ` +
          `render ${out.timing.renderMs.toFixed(1)} ms, mode ${samePlatform ? 'exact' : 'SSIM >= 0.98 (different platform)'}`,
      );
      if (samePlatform) expect(d.identical, `${name} differs from its golden in ${d.differingPixels} pixels`).toBe(true);
      expect(s).toBeGreaterThanOrEqual(0.98);
    });

    const remove = WITHOUT_FEATURE[name];
    if (remove) {
      it(`${name}: shows its named feature (removing it changes >= 10 % of the pixels)`, async () => {
        const scene = readScene(goldenScenePath(name));
        const without = JSON.parse(JSON.stringify(scene)) as SceneSpec;
        remove(without);
        const a = await runner.render(scene, loadSyntheticInputs());
        const b = await runner.render(without, loadSyntheticInputs());
        const d = pixelDiff({ width: a.width, height: a.height, data: a.rgba }, { width: b.width, height: b.height, data: b.rgba });
        const share = d.differingPixels / (a.width * a.height);
        console.log(`golden ${name}: removing its feature changes ${d.differingPixels} pixels (${(100 * share).toFixed(1)} %), mean |diff| ${d.meanAbsDiff.toFixed(2)}`);
        expect(share).toBeGreaterThanOrEqual(0.1);
      });
    }
  }
});
