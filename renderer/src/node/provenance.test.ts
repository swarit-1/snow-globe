import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { RENDERER_VERSION } from '../version.ts';
import { PACKAGE_ROOT } from './paths.ts';
import { gitState, loadAverage, provenance, sourceHashes } from './provenance.ts';

describe('provenance of measured runs', () => {
  it('hashes an entry and its run-time imports, skipping type-only imports', () => {
    const h = sourceHashes(join(PACKAGE_ROOT, 'src/cpu/montecarlo.ts'));
    expect(Object.keys(h)).toContain('src/cpu/montecarlo.ts');
    expect(Object.keys(h)).toContain('src/clouds.ts');
    expect(Object.keys(h)).toContain('src/math/noise.ts');
    // montecarlo.ts imports cpu/reference.ts only as a type: not part of what runs
    expect(Object.keys(h)).not.toContain('src/cpu/reference.ts');
    const own = createHash('sha256').update(readFileSync(join(PACKAGE_ROOT, 'src/clouds.ts'), 'utf8')).digest('hex');
    expect(h['src/clouds.ts']).toBe(own);
    for (const v of Object.values(h)) expect(v).toMatch(/^[0-9a-f]{64}$/);
  });

  it('records the renderer version, the git state, the time and three load averages', () => {
    const p = provenance();
    expect(p.renderer_version).toBe(RENDERER_VERSION);
    expect(Number.isNaN(Date.parse(p.measured_utc))).toBe(false);
    expect(p.load_average).toHaveLength(3);
    for (const x of loadAverage()) expect(x).toBeGreaterThanOrEqual(0);
    const g = gitState();
    if (g.commit !== null) {
      expect(g.commit).toMatch(/^[0-9a-f]{40}$/);
      expect(typeof g.dirty).toBe('boolean');
    } else {
      expect(g.dirty).toBeNull();
    }
  });
});
