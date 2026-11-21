/**
 * Paths for the renderer's Node tools. Real data (frames, plates, renders, benchmark outputs) lives
 * outside the repo under the data dir (dataDir(): $SNOW_GLOBE_DATA_DIR, else ~/snow-globe-data or, until
 * it is moved, ~/lookout-data), per the CLAUDE.md hard rule; the
 * only images in the repo are the synthetic plate and its goldens (procedural, no camera pixels), and
 * assertOutsideRepo() keeps every other output out.
 */
import { existsSync, realpathSync, statSync } from 'node:fs';
import { homedir, arch, platform, release } from 'node:os';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

export const PACKAGE_ROOT = fileURLToPath(new URL('../..', import.meta.url));
export const GOLDEN_DIR = join(PACKAGE_ROOT, 'goldens');
export const GOLDEN_SCENE_DIR = join(GOLDEN_DIR, 'scenes');
export const GOLDEN_PLATE_DIR = join(GOLDEN_DIR, 'plate');
export const GOLDEN_NAMES = ['clear', 'low-deck', 'fog'] as const;
export type GoldenName = (typeof GOLDEN_NAMES)[number];

export const SYNTHETIC_PLATE_FILES = {
  plate: join(GOLDEN_PLATE_DIR, 'synthetic-plate.png'),
  skyMask: join(GOLDEN_PLATE_DIR, 'synthetic-sky.png'),
  depth: join(GOLDEN_PLATE_DIR, 'synthetic-depth.png'),
} as const;

/** The committed synthetic plate must stay small (task: <= 100 KB). */
export const SYNTHETIC_PLATE_MAX_BYTES = 100 * 1024;

/**
 * `SNOW_GLOBE_<X>`, else its deprecated `LOOKOUT_<X>` form (D-080): the first that is set and not blank,
 * the workers' rule (snow_globe_common.env).
 */
export function envVar(name: string, env: NodeJS.ProcessEnv = process.env): string | undefined {
  const legacy = name.startsWith('SNOW_GLOBE_') ? `LOOKOUT_${name.slice('SNOW_GLOBE_'.length)}` : undefined;
  for (const candidate of legacy === undefined ? [name] : [name, legacy]) {
    const value = env[candidate];
    if (value !== undefined && value.trim() !== '') return value;
  }
  return undefined;
}

/** True when two existing paths are one folder: same device and inode, symlinks followed. */
function sameFolder(a: string, b: string): boolean {
  const x = statSync(a, { throwIfNoEntry: false });
  const y = statSync(b, { throwIfNoEntry: false });
  return x !== undefined && y !== undefined && x.dev === y.dev && x.ino === y.ino;
}

/**
 * The data dir, resolved as the workers resolve it (D-080): $SNOW_GLOBE_DATA_DIR, else the deprecated
 * $LOOKOUT_DATA_DIR, else ~/snow-globe-data if it exists, else ~/lookout-data if it exists (its name
 * before the rename), else ~/snow-globe-data. Nothing is created. Throws while no variable names it and
 * both folders exist as different folders (something made ~/snow-globe-data before the rename's live
 * switch, which leaves ~/lookout-data as a symlink to it), as the workers refuse then.
 */
export function dataDir(env: NodeJS.ProcessEnv = process.env, home: string = homedir()): string {
  const named = envVar('SNOW_GLOBE_DATA_DIR', env);
  if (named !== undefined) return named.trim();
  const current = join(home, 'snow-globe-data');
  const legacy = join(home, 'lookout-data');
  if (existsSync(current) && existsSync(legacy) && !sameFolder(current, legacy)) {
    throw new Error(
      `both ${current} and ${legacy} exist and are different folders, so the data dir is ambiguous (D-080): ` +
        `until the rename's live switch the data is in ${legacy}, and after it ${legacy} is a symlink to ` +
        `${current}. Set SNOW_GLOBE_DATA_DIR to the folder you mean, and tell the lead which one is stray`,
    );
  }
  if (existsSync(current)) return current;
  if (existsSync(legacy)) return legacy;
  return current;
}

/** Scratch and cache space for the renderer's own outputs (deletable). */
export function rendererCacheDir(): string {
  return join(dataDir(), 'cache', 'renderer');
}

export function platformInfo(): { os: string; release: string; arch: string; node: string } {
  return { os: platform(), release: release(), arch: arch(), node: process.version };
}

/** The repository root: the nearest ancestor of the package that holds a .git entry, or null outside a checkout. */
export function repoRoot(): string | null {
  let dir = PACKAGE_ROOT;
  for (;;) {
    if (existsSync(join(dir, '.git'))) return realpathSync(dir);
    const up = dirname(dir);
    if (up === dir) return null;
    dir = up;
  }
}

/** Resolves symlinks through the nearest existing ancestor (the path itself may not exist yet). */
function realpathLoose(p: string): string {
  let head = resolve(p);
  const tail: string[] = [];
  while (!existsSync(head)) {
    const up = dirname(head);
    if (up === head) break;
    tail.unshift(head.slice(up.length + (up.endsWith(sep) ? 0 : 1)));
    head = up;
  }
  return join(realpathSync(head), ...tail);
}

/**
 * Throws if path lies inside the repository. CLAUDE.md: never write frames, plates, renders, traces or
 * GRIB files inside the repo; outputs belong under $SNOW_GLOBE_DATA_DIR.
 */
export function assertOutsideRepo(path: string, what: string): void {
  const root = repoRoot();
  if (root === null) return;
  const real = realpathLoose(path);
  if (real === root || real.startsWith(root + sep)) {
    throw new Error(`refusing to write ${what} inside the repository (${real}); write it under $SNOW_GLOBE_DATA_DIR (${dataDir()}) instead`);
  }
}
