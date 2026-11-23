/**
 * node scripts/render-batch.ts --jobs jobs.jsonl [--backend swiftshader|gpu] [--allow-version-mismatch]
 * node scripts/render-batch.ts --identity
 *
 * Renders every job of a JSON Lines job list (src/node/render-batch.ts) with one headless runner, so the
 * browser launch and the first frame's compile are paid once. Prints JSON Lines to stdout: a
 * `render-batch-start` line with the renderer's identity (version, bundle SHA-256, Node-side source
 * SHA-256s, git state), one `render-batch-result` line per job as it finishes, and a `render-batch-summary`
 * line. With --identity it prints only the identity line and renders nothing.
 *
 * Exit 0 when every job rendered, 1 when any job failed, 2 on bad arguments. Outputs must lie outside the
 * repo (renderToFiles refuses otherwise); evaluation writes them under $SNOW_GLOBE_DATA_DIR/renders/.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parseArgs, userPath } from '../src/node/args.ts';
import { DEFAULT_DEPS, parseJobs, rendererIdentity, runBatch } from '../src/node/render-batch.ts';

const USAGE = 'usage: node scripts/render-batch.ts --jobs jobs.jsonl [--backend swiftshader|gpu] [--allow-version-mismatch] | --identity';
const ENTRY = fileURLToPath(import.meta.url);

function emit(line: unknown): void {
  process.stdout.write(`${JSON.stringify(line)}\n`);
}

async function main(): Promise<number> {
  const { values, flags } = parseArgs(process.argv.slice(2), { values: ['jobs', 'backend'], flags: ['allow-version-mismatch', 'identity', 'help'] });
  if (flags.has('help')) {
    console.error(USAGE);
    return 0;
  }
  const identity = await rendererIdentity(ENTRY);
  const backend = (values.backend ?? 'swiftshader') as 'swiftshader' | 'gpu';
  if (backend !== 'swiftshader' && backend !== 'gpu') {
    console.error('--backend must be swiftshader or gpu');
    return 2;
  }
  if (flags.has('identity')) {
    emit({ kind: 'render-batch-start', backend, jobs: 0, ...identity });
    return 0;
  }
  if (!values.jobs) {
    console.error(USAGE);
    return 2;
  }
  const jobs = parseJobs(readFileSync(userPath(values.jobs), 'utf8'));
  const t0 = performance.now();
  emit({ kind: 'render-batch-start', backend, jobs: jobs.length, ...identity });
  const { results, launchMs } = await runBatch(jobs, { backend, allowVersionMismatch: flags.has('allow-version-mismatch'), onResult: emit }, DEFAULT_DEPS);
  const failed = results.filter((r) => !r.ok).length;
  emit({ kind: 'render-batch-summary', jobs: jobs.length, ok: results.length - failed, failed, launch_ms: launchMs, wall_ms: performance.now() - t0 });
  return failed === 0 ? 0 : 1;
}

// exitCode, not exit(): on macOS stdout to a pipe is asynchronous, and exit() could drop result lines.
main().then(
  (code) => {
    process.exitCode = code;
  },
  (e: unknown) => {
    console.error(e instanceof Error ? e.message : e);
    process.exitCode = 2;
  },
);
