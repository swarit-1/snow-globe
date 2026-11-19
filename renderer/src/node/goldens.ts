/**
 * Golden images (PRD §9.5): three synthetic scenes (clear, low deck below the camera, fog) rendered on
 * Chromium/SwiftShader from the committed synthetic plate. The manifest records the platform and GL
 * renderer that produced them: on the same platform the test demands identical pixels; elsewhere it
 * falls back to the PRD's SSIM >= 0.98 rule and says so.
 *
 * These are the only renders written inside the repo, and only because the plate is procedural (no
 * camera pixels). updateGoldens() refuses any scene that is not the synthetic viewpoint on the synthetic
 * plate: goldens of real NPS plates (M2) belong under $LOOKOUT_DATA_DIR, never in this public repo
 * (CLAUDE.md, PRD §15.1). The synthetic-only exception still needs recording in CLAUDE.md or DECISIONS.md.
 */
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { RawImage } from '../cpu/reference.ts';
import { SYNTHETIC_PLATE_ID, SYNTHETIC_VIEWPOINT_ID, generateSyntheticPlate } from '../synthetic/world.ts';
import { sceneHash, type SceneSpec } from '../scene.ts';
import { RENDERER_VERSION } from '../version.ts';
import { HeadlessRenderer } from './headless.ts';
import { encodePngGray, encodePngRGB, readImageFile } from './images.ts';
import { GOLDEN_DIR, GOLDEN_NAMES, GOLDEN_SCENE_DIR, SYNTHETIC_PLATE_FILES, SYNTHETIC_PLATE_MAX_BYTES, platformInfo, type GoldenName } from './paths.ts';
import { readScene } from './render-files.ts';

export const GOLDEN_MANIFEST = join(GOLDEN_DIR, 'manifest.json');

export interface GoldenManifest {
  renderer_version: string;
  backend: 'swiftshader';
  gl_renderer: string;
  browser: string;
  platform: { os: string; arch: string };
  plate: Record<'plate' | 'skyMask' | 'depth', { file: string; sha256: string; bytes: number }>;
  goldens: Record<GoldenName, { file: string; sha256: string; bytes: number; scene_hash: string }>;
}

const sha256 = (b: Uint8Array): string => createHash('sha256').update(b).digest('hex');

export function goldenScenePath(name: GoldenName): string {
  return join(GOLDEN_SCENE_DIR, `${name}.json`);
}

export function goldenImagePath(name: GoldenName): string {
  return join(GOLDEN_DIR, `${name}.png`);
}

/** Encodes the synthetic plate set exactly as committed. */
export function encodeSyntheticPlate(): { plate: Buffer; skyMask: Buffer; depth: Buffer; stats: ReturnType<typeof generateSyntheticPlate>['stats'] } {
  const p = generateSyntheticPlate();
  return { plate: encodePngRGB(p.plate), skyMask: encodePngGray(p.skyMask), depth: encodePngRGB(p.depth), stats: p.stats };
}

export interface SyntheticInputs {
  plate: RawImage;
  skyMask: RawImage;
  depth: RawImage;
  /** SHA-256 of the committed files (the golden scenes name them in scene.plate). */
  sha256: { plate: string; skyMask: string; depth: string };
}

/** The committed synthetic inputs, decoded, with their file hashes. */
export function loadSyntheticInputs(): SyntheticInputs {
  const plate = readImageFile(SYNTHETIC_PLATE_FILES.plate);
  const skyMask = readImageFile(SYNTHETIC_PLATE_FILES.skyMask);
  const depth = readImageFile(SYNTHETIC_PLATE_FILES.depth);
  return {
    plate: plate.image,
    skyMask: skyMask.image,
    depth: depth.image,
    sha256: { plate: sha256(plate.bytes), skyMask: sha256(skyMask.bytes), depth: sha256(depth.bytes) },
  };
}

/** Throws unless a scene is the synthetic viewpoint on the synthetic plate (the only goldens allowed in the repo). */
export function assertSyntheticGolden(scene: SceneSpec, name: string): void {
  if (scene.viewpoint_id !== SYNTHETIC_VIEWPOINT_ID || scene.plate.id !== SYNTHETIC_PLATE_ID) {
    throw new Error(
      `golden ${name}: only the synthetic viewpoint (${SYNTHETIC_VIEWPOINT_ID}) on ${SYNTHETIC_PLATE_ID} may be rendered into the repo; ` +
        `goldens of real plates belong under $LOOKOUT_DATA_DIR`,
    );
  }
}

/**
 * Points a golden scene file at the committed plate files (plate.id and the three file hashes), keeping
 * every other field. Returns true if the file changed.
 */
function syncGoldenScenePlate(name: GoldenName, hashes: SyntheticInputs['sha256']): boolean {
  const path = goldenScenePath(name);
  const before = readFileSync(path, 'utf8');
  const raw = JSON.parse(before) as { plate: Record<string, unknown> };
  raw.plate = { ...raw.plate, id: SYNTHETIC_PLATE_ID, sha256: hashes.plate, sky_mask_sha256: hashes.skyMask, depth_sha256: hashes.depth };
  const after = `${JSON.stringify(raw, null, 2)}\n`;
  if (after === before) return false;
  writeFileSync(path, after);
  return true;
}

export function readGoldenManifest(): GoldenManifest | null {
  return existsSync(GOLDEN_MANIFEST) ? (JSON.parse(readFileSync(GOLDEN_MANIFEST, 'utf8')) as GoldenManifest) : null;
}

/** Regenerates the synthetic plate files and every golden image, then rewrites the manifest. */
export async function updateGoldens(log: (s: string) => void = console.log): Promise<GoldenManifest> {
  const enc = encodeSyntheticPlate();
  if (enc.plate.length > SYNTHETIC_PLATE_MAX_BYTES) throw new Error(`synthetic plate is ${enc.plate.length} bytes; the limit is ${SYNTHETIC_PLATE_MAX_BYTES}`);
  writeFileSync(SYNTHETIC_PLATE_FILES.plate, enc.plate);
  writeFileSync(SYNTHETIC_PLATE_FILES.skyMask, enc.skyMask);
  writeFileSync(SYNTHETIC_PLATE_FILES.depth, enc.depth);
  log(`synthetic plate: ${enc.plate.length} B, mask ${enc.skyMask.length} B, depth ${enc.depth.length} B (${enc.stats.elapsedMs.toFixed(0)} ms)`);
  const inputs = loadSyntheticInputs();
  for (const name of GOLDEN_NAMES) if (syncGoldenScenePlate(name, inputs.sha256)) log(`golden scene ${name}: plate id and hashes updated`);
  const runner = await HeadlessRenderer.launch({ backend: 'swiftshader' });
  try {
    const goldens = {} as GoldenManifest['goldens'];
    for (const name of GOLDEN_NAMES) {
      const scene = readScene(goldenScenePath(name));
      assertSyntheticGolden(scene, name);
      const out = await runner.render(scene, inputs);
      const png = encodePngRGB({ width: out.width, height: out.height, data: out.rgba });
      writeFileSync(goldenImagePath(name), png);
      goldens[name] = { file: `${name}.png`, sha256: sha256(png), bytes: png.length, scene_hash: sceneHash(scene) };
      log(`golden ${name}: ${png.length} B, render ${out.timing.renderMs.toFixed(1)} ms`);
    }
    const p = platformInfo();
    const manifest: GoldenManifest = {
      renderer_version: RENDERER_VERSION,
      backend: 'swiftshader',
      gl_renderer: String(runner.glInfo.unmaskedRenderer ?? runner.glInfo.renderer),
      browser: `chromium ${runner.browserVersion}`,
      platform: { os: p.os, arch: p.arch },
      plate: {
        plate: { file: 'plate/synthetic-plate.png', sha256: sha256(enc.plate), bytes: enc.plate.length },
        skyMask: { file: 'plate/synthetic-sky.png', sha256: sha256(enc.skyMask), bytes: enc.skyMask.length },
        depth: { file: 'plate/synthetic-depth.png', sha256: sha256(enc.depth), bytes: enc.depth.length },
      },
      goldens,
    };
    writeFileSync(GOLDEN_MANIFEST, `${JSON.stringify(manifest, null, 2)}\n`);
    return manifest;
  } finally {
    await runner.close();
  }
}
