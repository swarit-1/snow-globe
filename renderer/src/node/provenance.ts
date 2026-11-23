/**
 * Provenance of a measured run (PRD §9.5 gate evidence and the renderer's benchmarks): which renderer
 * produced it (RENDERER_VERSION, the git commit and whether the renderer's files differ from it), when, how
 * busy the machine was (the 1, 5 and 15 minute load averages, as `sysctl -n vm.loadavg` prints them), and
 * the SHA-256 of the source files a script ran, so a result can be tied to the exact code that made it.
 * Everything is local: git runs on the working tree, nothing leaves the machine.
 */
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { loadavg } from 'node:os';
import { dirname, relative, resolve } from 'node:path';
import { RENDERER_VERSION } from '../version.ts';
import { PACKAGE_ROOT } from './paths.ts';

export interface GitState {
  /** The commit HEAD points at, or null when git is unavailable. */
  commit: string | null;
  /** True when a file under renderer/ differs from that commit (tracked changes or untracked files). */
  dirty: boolean | null;
}

/** HEAD's commit and whether the renderer package's files differ from it. */
export function gitState(cwd: string = PACKAGE_ROOT): GitState {
  try {
    const commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
    const status = execFileSync('git', ['status', '--porcelain', '--', '.'], { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
    return { commit, dirty: status.trim().length > 0 };
  } catch {
    return { commit: null, dirty: null };
  }
}

/** The 1, 5 and 15 minute load averages, rounded to 2 decimals. */
export function loadAverage(): [number, number, number] {
  const [a, b, c] = loadavg();
  const r = (x: number | undefined) => Number((x ?? 0).toFixed(2));
  return [r(a), r(b), r(c)];
}

/**
 * The files an entry script imports at run time, itself included (relative imports only, type-only
 * imports skipped), with each one's SHA-256, paths relative to the package root, in import order.
 */
export function sourceHashes(entry: string): Record<string, string> {
  const seen = new Set<string>();
  const out: Record<string, string> = {};
  const walk = (file: string): void => {
    if (seen.has(file)) return;
    seen.add(file);
    const text = readFileSync(file, 'utf8');
    for (const m of text.matchAll(/(?:^|\n)\s*import\s+(type\s+)?[^;]*?from\s+'(\.[^']+)'/g)) {
      if (m[1] === undefined) walk(resolve(dirname(file), m[2]!));
    }
    out[relative(PACKAGE_ROOT, file)] = createHash('sha256').update(text).digest('hex');
  };
  walk(resolve(entry));
  return out;
}

export interface Provenance {
  renderer_version: string;
  git: GitState;
  measured_utc: string;
  load_average: [number, number, number];
}

/** RENDERER_VERSION, the git state, the time and the load average now. */
export function provenance(): Provenance {
  return { renderer_version: RENDERER_VERSION, git: gitState(), measured_utc: new Date().toISOString(), load_average: loadAverage() };
}
