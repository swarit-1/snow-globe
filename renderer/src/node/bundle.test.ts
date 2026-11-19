import { describe, expect, it } from 'vitest';
import { buildEsmBundle, buildIifeBundle } from './bundle.ts';

/**
 * PRD §15.4 and D-013: third-party notices must stay in the bundle. The renderer bundle currently credits
 * two algorithms; the Hillaire 2020 port (TODO(M1.5)) adds its MIT notice the same way. With esbuild's
 * old legalComments: 'none' both notices were stripped, so this test fails on that setting.
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
    }
  });
});
