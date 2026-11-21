import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, unlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, sep } from 'node:path';
import { describe, expect, it } from 'vitest';
import { PACKAGE_ROOT, assertOutsideRepo, dataDir, envVar, repoRoot } from './paths.ts';

/** The rename (D-080): the new names first, the old ones as a fallback. */
describe('the data dir after the rename', () => {
  it('reads SNOW_GLOBE_* first, then the deprecated LOOKOUT_* name', () => {
    expect(envVar('SNOW_GLOBE_X', {})).toBeUndefined();
    expect(envVar('SNOW_GLOBE_X', { LOOKOUT_X: 'old' })).toBe('old');
    expect(envVar('SNOW_GLOBE_X', { SNOW_GLOBE_X: 'new', LOOKOUT_X: 'old' })).toBe('new');
    expect(envVar('SNOW_GLOBE_X', { SNOW_GLOBE_X: ' ', LOOKOUT_X: 'old' })).toBe('old');
    expect(envVar('HOME', { LOOKOUT_HOME: 'x' })).toBeUndefined();
  });

  it('defaults to ~/snow-globe-data, or ~/lookout-data until it is moved', () => {
    const home = mkdtempSync(join(tmpdir(), 'snow-globe-home-test-'));
    try {
      expect(dataDir({}, home)).toBe(join(home, 'snow-globe-data'));
      mkdirSync(join(home, 'lookout-data'));
      expect(dataDir({}, home)).toBe(join(home, 'lookout-data'));
      mkdirSync(join(home, 'snow-globe-data'));
      // Both folders apart (~/snow-globe-data made before the switch): refused, as the workers refuse.
      expect(() => dataDir({}, home)).toThrow(/ambiguous \(D-080\)/);
      expect(dataDir({ LOOKOUT_DATA_DIR: '/old' }, home)).toBe('/old');
      expect(dataDir({ LOOKOUT_DATA_DIR: '/old', SNOW_GLOBE_DATA_DIR: '/new' }, home)).toBe('/new');
      // After the switch ~/lookout-data is a symlink to ~/snow-globe-data: the new name wins.
      rmSync(join(home, 'lookout-data'), { recursive: true });
      symlinkSync(join(home, 'snow-globe-data'), join(home, 'lookout-data'));
      expect(dataDir({}, home)).toBe(join(home, 'snow-globe-data'));
      // A dangling ~/snow-globe-data link does not exist.
      unlinkSync(join(home, 'lookout-data'));
      rmSync(join(home, 'snow-globe-data'), { recursive: true });
      mkdirSync(join(home, 'lookout-data'));
      symlinkSync(join(home, 'nowhere'), join(home, 'snow-globe-data'));
      expect(dataDir({}, home)).toBe(join(home, 'lookout-data'));
    } finally {
      rmSync(home, { recursive: true, force: true });
    }
  });
});

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
    const dir = mkdtempSync(join(tmpdir(), 'snow-globe-paths-test-'));
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
