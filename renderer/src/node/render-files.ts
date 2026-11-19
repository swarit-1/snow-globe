/**
 * File-level render used by `pnpm render` and the determinism test:
 * scene JSON + plate (+ sky mask, depth) -> PNG + metadata JSON (renderer version, GL renderer string,
 * scene hash, render ms). PRD §9.5: the same inputs must give byte-identical PNGs.
 *
 * The plate, mask and depth files must be the ones scene.plate names by SHA-256 (so render stays pure in
 * the scene and scene_hash changes with the plate), and outputs may not be written inside the repo.
 */
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import type { RawImage } from '../cpu/reference.ts';
import { sceneHash, validateScene, type SceneSpec } from '../scene.ts';
import { RENDERER_STAGE, RENDERER_VERSION } from '../version.ts';
import { HeadlessRenderer, type Backend } from './headless.ts';
import { encodePngRGB, readImageFile } from './images.ts';
import { assertOutsideRepo, platformInfo } from './paths.ts';

export interface RenderFilesOptions {
  scenePath: string;
  platePath: string;
  skyMaskPath?: string | null;
  depthPath?: string | null;
  outPath: string;
  /** Path for the metadata JSON; defaults to outPath with .json instead of .png. */
  metaPath?: string;
  backend?: Backend;
  allowVersionMismatch?: boolean;
  /** Reuse an already launched runner (the CLI launches its own). */
  runner?: HeadlessRenderer;
}

export interface InputRecord {
  path: string;
  sha256: string;
  width: number;
  height: number;
}

export interface RenderMetadata {
  renderer_version: string;
  renderer_stage: string;
  scene_renderer_version: string;
  scene_hash: string;
  viewpoint_id: string;
  valid_time_utc: string;
  seed: string;
  backend: Backend;
  gl_renderer: string;
  gl: Record<string, unknown>;
  browser: string;
  platform: ReturnType<typeof platformInfo>;
  inputs: { plate: InputRecord; sky_mask: InputRecord | null; depth: InputRecord | null };
  output: { path: string; sha256: string; width: number; height: number; bytes: number };
  /** render: page-side render() + 1-pixel barrier; readback: full-frame readPixels + row flip; encode: base64 in the page. */
  timing_ms: { render: number; setup: number; upload: number; submit: number; readback: number; encode: number; launch: number | null; total: number };
  created_utc: string;
}

const sha256 = (b: Uint8Array): string => createHash('sha256').update(b).digest('hex');

export function readScene(path: string): SceneSpec {
  return validateScene(JSON.parse(readFileSync(path, 'utf8')));
}

function loadInput(path: string | null | undefined): { image: RawImage; record: InputRecord } | null {
  if (!path) return null;
  const { image, bytes } = readImageFile(path);
  return { image, record: { path, sha256: sha256(bytes), width: image.width, height: image.height } };
}

export function defaultMetaPath(outPath: string): string {
  return outPath.toLowerCase().endsWith('.png') ? `${outPath.slice(0, -4)}.json` : `${outPath}.json`;
}

/** Renders one scene to a PNG and writes its metadata JSON beside it. */
export async function renderToFiles(opts: RenderFilesOptions): Promise<RenderMetadata> {
  const t0 = performance.now();
  const scene = readScene(opts.scenePath);
  if (scene.renderer_version !== RENDERER_VERSION && !opts.allowVersionMismatch) {
    throw new Error(
      `scene.renderer_version is ${scene.renderer_version} but this renderer is ${RENDERER_VERSION}; ` +
        'pass --allow-version-mismatch to render anyway (the metadata records both)',
    );
  }
  const metaPath = opts.metaPath ?? defaultMetaPath(opts.outPath);
  assertOutsideRepo(opts.outPath, 'a render');
  assertOutsideRepo(metaPath, 'render metadata');
  const plate = loadInput(opts.platePath)!;
  const skyMask = loadInput(opts.skyMaskPath);
  const depth = loadInput(opts.depthPath);
  if (scene.plate.has_sky_mask !== Boolean(skyMask)) throw new Error(`scene.plate.has_sky_mask is ${scene.plate.has_sky_mask} but --sky-mask was ${skyMask ? '' : 'not '}given`);
  if (scene.plate.has_depth !== Boolean(depth)) throw new Error(`scene.plate.has_depth is ${scene.plate.has_depth} but --depth was ${depth ? '' : 'not '}given`);
  const named: [string, InputRecord | undefined, string | undefined][] = [
    ['plate', plate.record, scene.plate.sha256],
    ['sky mask', skyMask?.record, scene.plate.sky_mask_sha256],
    ['depth', depth?.record, scene.plate.depth_sha256],
  ];
  for (const [what, rec, expected] of named) {
    if (rec && rec.sha256 !== expected) {
      throw new Error(`the ${what} file ${rec.path} has sha256 ${rec.sha256}, but scene.plate names ${expected ?? 'none'} (plate id ${scene.plate.id})`);
    }
  }

  const ownRunner = !opts.runner;
  const runner = opts.runner ?? (await HeadlessRenderer.launch({ backend: opts.backend ?? 'swiftshader' }));
  try {
    const out = await runner.render(scene, {
      plate: plate.image,
      skyMask: skyMask?.image ?? null,
      depth: depth?.image ?? null,
      sha256: { plate: plate.record.sha256, ...(skyMask ? { skyMask: skyMask.record.sha256 } : {}), ...(depth ? { depth: depth.record.sha256 } : {}) },
    });
    const png = encodePngRGB({ width: out.width, height: out.height, data: out.rgba });
    mkdirSync(dirname(opts.outPath), { recursive: true });
    writeFileSync(opts.outPath, png);
    const meta: RenderMetadata = {
      renderer_version: RENDERER_VERSION,
      renderer_stage: RENDERER_STAGE,
      scene_renderer_version: scene.renderer_version,
      scene_hash: sceneHash(scene),
      viewpoint_id: scene.viewpoint_id,
      valid_time_utc: scene.valid_time_utc,
      seed: scene.seed,
      backend: runner.backend,
      gl_renderer: String(runner.glInfo.unmaskedRenderer ?? runner.glInfo.renderer),
      gl: { ...runner.glInfo },
      browser: `chromium ${runner.browserVersion}`,
      platform: platformInfo(),
      inputs: { plate: plate.record, sky_mask: skyMask?.record ?? null, depth: depth?.record ?? null },
      output: { path: opts.outPath, sha256: sha256(png), width: out.width, height: out.height, bytes: png.length },
      timing_ms: {
        render: out.timing.renderMs,
        setup: out.timing.setupMs,
        upload: out.timing.uploadMs,
        submit: out.timing.submitMs,
        readback: out.timing.readbackMs,
        encode: out.timing.encodeMs,
        launch: ownRunner ? runner.launchMs : null,
        total: performance.now() - t0,
      },
      created_utc: new Date().toISOString(),
    };
    writeFileSync(metaPath, `${JSON.stringify(meta, null, 2)}\n`);
    return meta;
  } finally {
    if (ownRunner) await runner.close();
  }
}
