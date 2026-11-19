/**
 * Paths for the renderer's Node tools. Real data (frames, plates, renders, benchmark outputs) lives
 * outside the repo under $LOOKOUT_DATA_DIR (default ~/lookout-data), per the CLAUDE.md hard rule; the
 * only images in the repo are the synthetic plate and its goldens (procedural, no camera pixels), and
 * assertOutsideRepo() keeps every other output out.
 */
import { existsSync, realpathSync } from 'node:fs';
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

export function dataDir(): string {
  return process.env.LOOKOUT_DATA_DIR ?? join(homedir(), 'lookout-data');
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
 * GRIB files inside the repo; outputs belong under $LOOKOUT_DATA_DIR.
 */
export function assertOutsideRepo(path: string, what: string): void {
  const root = repoRoot();
  if (root === null) return;
  const real = realpathLoose(path);
  if (real === root || real.startsWith(root + sep)) {
    throw new Error(`refusing to write ${what} inside the repository (${real}); write it under $LOOKOUT_DATA_DIR (${dataDir()}) instead`);
  }
}
