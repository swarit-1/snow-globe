import { describe, expect, it } from 'vitest';
import { buildEsmBundle, buildIifeBundle } from './bundle.ts';

/**
 * PRD §15.4 and D-013: third-party notices must stay in the bundle. The renderer bundle credits two
 * algorithms and carries the MIT notice of the Hillaire 2020 reference implementation it ports (also in
 * the atmosphere's shader sources). With esbuild's old legalComments: 'none' the notices were stripped,
 * so this test fails on that setting.
 */
describe('browser bundle keeps third-party notices', () => {
  it('keeps the legal comments in the headless IIFE build and the minified ESM build', async () => {
    const builds: [string, string][] = [
      ['iife', await buildIifeBundle()],
      ['esm (minified, as dist/)', await buildEsmBundle()],
    ];
    for (const [name, text] of builds) {
      expect(text, name).toContain('the lowbias32 integer hash is by Chris Wellons');
      expect(text, name).toContain('the inverse normal CDF uses the rational approximation by Peter J. Acklam');
      expect(text, name).toContain('sebh/UnrealEngineSkyAtmosphere');
      expect(text, name).toContain('Copyright (c) 2020 Epic Games, Inc.');
      expect(text, name).toContain('The above copyright notice and this permission notice shall be included in all copies or substantial portions of the');
    }
  });
});
