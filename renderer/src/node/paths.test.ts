import { mkdtempSync, realpathSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, sep } from 'node:path';
import { describe, expect, it } from 'vitest';
import { PACKAGE_ROOT, assertOutsideRepo, dataDir, repoRoot } from './paths.ts';

/** CLAUDE.md: never write frames, plates, renders, traces or GRIB files inside the repo. */
describe('outputs stay outside the repository', () => {
  it('finds the repository root above the package', () => {
    const root = repoRoot();
    expect(root).not.toBeNull();
    expect(realpathSync(PACKAGE_ROOT).startsWith(root! + sep)).toBe(true);
  });

  it('refuses paths inside the repository, including ones that do not exist yet', () => {
    expect(() => assertOutsideRepo(join(PACKAGE_ROOT, 'goldens', 'x.png'), 'a render')).toThrow(/inside the repository/);
    expect(() => assertOutsideRepo(join(repoRoot()!, 'not', 'there', 'yet.png'), 'a render')).toThrow(/inside the repository/);
    expect(() => assertOutsideRepo(PACKAGE_ROOT, 'a render')).toThrow(/inside the repository/);
  });

  it('refuses a path that reaches the repository through a symlink', () => {
    const dir = mkdtempSync(join(tmpdir(), 'lookout-paths-test-'));
    try {
      symlinkSync(PACKAGE_ROOT, join(dir, 'link'));
      expect(() => assertOutsideRepo(join(dir, 'link', 'out.png'), 'a render')).toThrow(/inside the repository/);
      expect(() => assertOutsideRepo(join(dir, 'out.png'), 'a render')).not.toThrow();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('allows the data directory', () => {
    expect(() => assertOutsideRepo(join(dataDir(), 'cache', 'renderer', 'x.png'), 'a render')).not.toThrow();
  });
});
