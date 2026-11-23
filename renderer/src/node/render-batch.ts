/**
 * Batch rendering for evaluation (PRD §13.2 "Every pair renders Tier 1 headless"; PLAN M1.5's dev-only P1
 * table): many SceneSpecs through one launched HeadlessRenderer, so Chromium's launch, the bundle build and
 * the first frame's shader compile are paid once per batch instead of once per frame.
 *
 * Jobs are JSON Lines, one object per line, every path absolute:
 *
 *   {"id": "20261121T182101Z", "scene": "/…/scene.json", "plate": "/…/base.png",
 *    "sky_mask": "/…/sky.png", "depth": null, "out": "/…/render.png", "meta": "/…/render.json"}
 *
 * Each job goes through renderToFiles, so it gets exactly the checks of `pnpm render`: the scene is
 * validated, its renderer_version must be this renderer's, the plate, sky mask and depth files must be the
 * ones scene.plate names by SHA-256, and outputs may not be written inside the repo. A job that fails is
 * reported and the batch goes on with the next one; the runner is closed once at the end.
 *
 * The identity of the code that renders (rendererIdentity) is the renderer version, the SHA-256 of the
 * in-memory browser bundle (everything that runs in the page) and of the Node-side files this entry
 * imports, and the git state, so a caller can tell whether two renders came from the same code.
 */
import { createHash } from 'node:crypto';
import { isAbsolute } from 'node:path';
import { buildIifeBundle } from './bundle.ts';
import { HeadlessRenderer, type Backend } from './headless.ts';
import { gitState, sourceHashes, type GitState } from './provenance.ts';
import { defaultMetaPath, renderToFiles, type RenderFilesOptions, type RenderMetadata } from './render-files.ts';
import { RENDERER_STAGE, RENDERER_VERSION } from '../version.ts';

export interface BatchJob {
  id: string;
  scene: string;
  plate: string;
  sky_mask: string | null;
  depth: string | null;
  out: string;
  meta: string | null;
}

export interface BatchResult {
  kind: 'render-batch-result';
  id: string;
  ok: boolean;
  out: string;
  meta: string;
  sha256: string | null;
  scene_hash: string | null;
  render_ms: number | null;
  total_ms: number | null;
  error: string | null;
}

export interface RendererIdentity {
  renderer_version: string;
  renderer_stage: string;
  /** SHA-256 of the IIFE bundle the headless page runs (built in memory from src/, as every launch does). */
  bundle_sha256: string;
  /** SHA-256 of each Node-side file the given entry imports at run time (provenance.sourceHashes). */
  node_sources_sha256: Record<string, string>;
  /** One SHA-256 over the version, the bundle and the Node-side files: equal digests, same rendering code. */
  digest: string;
  git: GitState;
}

const KEYS = new Set(['id', 'scene', 'plate', 'sky_mask', 'depth', 'out', 'meta']);

function optionalPath(value: unknown, what: string, line: number): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'string' || !isAbsolute(value)) throw new Error(`jobs line ${line}: ${what} must be an absolute path or null`);
  return value;
}

/** Parses a JSON Lines job list (blank lines and lines starting with # are skipped); throws on any bad line. */
export function parseJobs(text: string): BatchJob[] {
  const jobs: BatchJob[] = [];
  const ids = new Set<string>();
  text.split('\n').forEach((raw, index) => {
    const line = index + 1;
    const trimmed = raw.trim();
    if (trimmed === '' || trimmed.startsWith('#')) return;
    let value: unknown;
    try {
      value = JSON.parse(trimmed);
    } catch (e) {
      throw new Error(`jobs line ${line}: not JSON (${e instanceof Error ? e.message : String(e)})`);
    }
    if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error(`jobs line ${line}: expected an object`);
    const o = value as Record<string, unknown>;
    for (const k of Object.keys(o)) if (!KEYS.has(k)) throw new Error(`jobs line ${line}: unknown field ${k}`);
    for (const k of ['scene', 'plate', 'out'] as const) {
      if (typeof o[k] !== 'string' || !isAbsolute(o[k])) throw new Error(`jobs line ${line}: ${k} must be an absolute path`);
    }
    const id = o.id === undefined ? String(jobs.length) : o.id;
    if (typeof id !== 'string' || id === '') throw new Error(`jobs line ${line}: id must be a non-empty string`);
    if (ids.has(id)) throw new Error(`jobs line ${line}: duplicate id ${id}`);
    ids.add(id);
    jobs.push({
      id,
      scene: o.scene as string,
      plate: o.plate as string,
      sky_mask: optionalPath(o.sky_mask, 'sky_mask', line),
      depth: optionalPath(o.depth, 'depth', line),
      out: o.out as string,
      meta: optionalPath(o.meta, 'meta', line),
    });
  });
  return jobs;
}

/** What a batch needs from its runner: something to close once every job is done. */
export interface ClosableRunner {
  close(): Promise<void>;
  readonly launchMs: number;
}

export interface BatchDeps<R extends ClosableRunner> {
  /** Launches the one shared runner. */
  launch: (backend: Backend) => Promise<R>;
  /** Renders one job with that runner. */
  render: (opts: Omit<RenderFilesOptions, 'runner'> & { runner: R }) => Promise<RenderMetadata>;
}

export const DEFAULT_DEPS: BatchDeps<HeadlessRenderer> = {
  launch: (backend) => HeadlessRenderer.launch({ backend }),
  render: (opts) => renderToFiles(opts),
};

export interface RunBatchOptions {
  backend?: Backend;
  allowVersionMismatch?: boolean;
  /** Called after each job, in job order (the CLI prints it at once, so a caller sees progress). */
  onResult?: (result: BatchResult) => void;
}

/**
 * Renders every job with one runner, in order. Returns one result per job and the runner's launch time
 * (null for an empty batch, which launches nothing).
 */
export async function runBatch<R extends ClosableRunner>(
  jobs: readonly BatchJob[],
  opts: RunBatchOptions,
  deps: BatchDeps<R>,
): Promise<{ results: BatchResult[]; launchMs: number | null }> {
  const results: BatchResult[] = [];
  if (jobs.length === 0) return { results, launchMs: null };
  const backend = opts.backend ?? 'swiftshader';
  const runner = await deps.launch(backend);
  try {
    for (const job of jobs) {
      const meta = job.meta ?? defaultMetaPath(job.out);
      let result: BatchResult;
      try {
        const m = await deps.render({
          scenePath: job.scene,
          platePath: job.plate,
          skyMaskPath: job.sky_mask,
          depthPath: job.depth,
          outPath: job.out,
          metaPath: meta,
          backend,
          allowVersionMismatch: opts.allowVersionMismatch === true,
          runner,
        });
        result = {
          kind: 'render-batch-result',
          id: job.id,
          ok: true,
          out: m.output.path,
          meta,
          sha256: m.output.sha256,
          scene_hash: m.scene_hash,
          render_ms: m.timing_ms.render,
          total_ms: m.timing_ms.total,
          error: null,
        };
      } catch (e) {
        result = {
          kind: 'render-batch-result',
          id: job.id,
          ok: false,
          out: job.out,
          meta,
          sha256: null,
          scene_hash: null,
          render_ms: null,
          total_ms: null,
          error: (e instanceof Error ? e.message : String(e)).slice(0, 2000),
        };
      }
      results.push(result);
      opts.onResult?.(result);
    }
  } finally {
    await runner.close();
  }
  return { results, launchMs: runner.launchMs };
}

/** The identity of the rendering code (see the module comment); `entry` is the script that renders. */
export async function rendererIdentity(entry: string): Promise<RendererIdentity> {
  const bundle = await buildIifeBundle();
  const bundleSha = createHash('sha256').update(bundle).digest('hex');
  const nodeSources = sourceHashes(entry);
  const digest = createHash('sha256')
    .update(`${RENDERER_VERSION}\n${bundleSha}\n`)
    .update(
      Object.entries(nodeSources)
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([k, v]) => `${k} ${v}\n`)
        .join(''),
    )
    .digest('hex');
  return {
    renderer_version: RENDERER_VERSION,
    renderer_stage: RENDERER_STAGE,
    bundle_sha256: bundleSha,
    node_sources_sha256: nodeSources,
    digest,
    git: gitState(),
  };
}
