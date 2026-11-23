import { describe, expect, it } from 'vitest';
import type { RenderMetadata } from './render-files.ts';
import { parseJobs, runBatch, type BatchDeps, type ClosableRunner } from './render-batch.ts';

const job = (id: string, extra: Record<string, unknown> = {}): string =>
  JSON.stringify({ id, scene: `/d/${id}/scene.json`, plate: '/d/base.png', sky_mask: '/d/sky.png', depth: null, out: `/d/${id}/render.png`, meta: `/d/${id}/render.json`, ...extra });

class FakeRunner implements ClosableRunner {
  readonly launchMs = 12.5;
  closed = 0;
  async close(): Promise<void> {
    this.closed += 1;
  }
}

function fakeMeta(out: string): RenderMetadata {
  return {
    scene_hash: `hash-of-${out}`,
    output: { path: out, sha256: 'a'.repeat(64), width: 4, height: 2, bytes: 10 },
    timing_ms: { render: 3, setup: 0, upload: 0, submit: 0, readback: 0, encode: 0, launch: null, total: 5 },
  } as unknown as RenderMetadata;
}

describe('render batch jobs', () => {
  it('parses JSON Lines, skipping blanks and comments, and defaults the optional paths to null', () => {
    const jobs = parseJobs(`${job('a')}\n\n# a comment\n${JSON.stringify({ scene: '/s.json', plate: '/p.png', out: '/o.png' })}\n`);
    expect(jobs.map((j) => j.id)).toEqual(['a', '1']);
    expect(jobs[0]).toMatchObject({ sky_mask: '/d/sky.png', depth: null, meta: '/d/a/render.json' });
    expect(jobs[1]).toMatchObject({ sky_mask: null, depth: null, meta: null });
  });

  it('refuses relative paths, unknown fields, duplicate ids and lines that are not objects', () => {
    expect(() => parseJobs(job('a', { scene: 'rel/scene.json' }))).toThrow(/scene must be an absolute path/);
    expect(() => parseJobs(job('a', { sky_mask: 'sky.png' }))).toThrow(/sky_mask must be an absolute path/);
    expect(() => parseJobs(job('a', { extra: 1 }))).toThrow(/unknown field extra/);
    expect(() => parseJobs(`${job('a')}\n${job('a')}`)).toThrow(/duplicate id a/);
    expect(() => parseJobs('[1, 2]')).toThrow(/expected an object/);
    expect(() => parseJobs('{not json')).toThrow(/not JSON/);
  });
});

describe('runBatch', () => {
  it('launches one runner, renders every job in order with it, reports failures and closes it once', async () => {
    const runner = new FakeRunner();
    let launches = 0;
    const seen: string[] = [];
    const deps: BatchDeps<FakeRunner> = {
      launch: async (backend) => {
        launches += 1;
        expect(backend).toBe('swiftshader');
        return runner;
      },
      render: async (opts) => {
        expect(opts.runner).toBe(runner);
        seen.push(opts.scenePath);
        if (opts.scenePath.includes('/b/')) throw new Error('scene.plate names another plate');
        return fakeMeta(opts.outPath);
      },
    };
    const streamed: string[] = [];
    const jobs = parseJobs([job('a'), job('b'), job('c')].join('\n'));
    const { results, launchMs } = await runBatch(jobs, { onResult: (r) => streamed.push(r.id) }, deps);
    expect(launches).toBe(1);
    expect(runner.closed).toBe(1);
    expect(launchMs).toBe(12.5);
    expect(seen).toEqual(['/d/a/scene.json', '/d/b/scene.json', '/d/c/scene.json']);
    expect(streamed).toEqual(['a', 'b', 'c']);
    expect(results.map((r) => r.ok)).toEqual([true, false, true]);
    expect(results[1]).toMatchObject({ id: 'b', ok: false, sha256: null, error: 'scene.plate names another plate' });
    expect(results[0]).toMatchObject({ out: '/d/a/render.png', meta: '/d/a/render.json', render_ms: 3, total_ms: 5, scene_hash: 'hash-of-/d/a/render.png' });
  });

  it('launches nothing for an empty batch', async () => {
    const deps: BatchDeps<FakeRunner> = {
      launch: async () => {
        throw new Error('should not launch');
      },
      render: async () => {
        throw new Error('should not render');
      },
    };
    await expect(runBatch([], {}, deps)).resolves.toEqual({ results: [], launchMs: null });
  });

  it('closes the runner even when a render callback throws past the job', async () => {
    const runner = new FakeRunner();
    const deps: BatchDeps<FakeRunner> = {
      launch: async () => runner,
      render: async (opts) => fakeMeta(opts.outPath),
    };
    const boom = (): void => {
      throw new Error('listener failed');
    };
    await expect(runBatch(parseJobs(job('a')), { onResult: boom }, deps)).rejects.toThrow(/listener failed/);
    expect(runner.closed).toBe(1);
  });
});
