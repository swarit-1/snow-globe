/**
 * Builds the browser bundle with esbuild. The headless runner builds in memory from src/ on every launch,
 * so a render can never use a stale bundle; `pnpm build` writes the same bundles to dist/.
 *
 * Legal comments (slash-star-bang, @license, @preserve) are kept and moved to the end of the bundle:
 * PRD §15.4 and D-013 require third-party notices, such as the Hillaire port's MIT notice (TODO(M1.5)),
 * to survive bundling. bundle.test.ts checks the current notices are present in every build.
 */
import { fileURLToPath } from 'node:url';
import { build, type BuildOptions } from 'esbuild';

export const PACKAGE_ROOT = fileURLToPath(new URL('../..', import.meta.url));
export const BROWSER_ENTRY = fileURLToPath(new URL('../browser.ts', import.meta.url));

const common: BuildOptions = {
  entryPoints: [BROWSER_ENTRY],
  bundle: true,
  platform: 'browser',
  target: ['es2022'],
  legalComments: 'eof',
  logLevel: 'silent',
};

/** IIFE bundle setting globalThis.LookoutRenderer, as a string (for the headless page). */
export async function buildIifeBundle(): Promise<string> {
  const r = await build({ ...common, format: 'iife', globalName: 'LookoutRenderer', write: false, minify: false });
  return r.outputFiles![0]!.text;
}

/** The ESM bundle (minified, as `pnpm build` writes it) as a string, for tests. */
export async function buildEsmBundle(): Promise<string> {
  const r = await build({ ...common, format: 'esm', write: false, minify: true });
  return r.outputFiles![0]!.text;
}

/** Writes dist/lookout-renderer.js (ESM) and dist/lookout-renderer.iife.js. */
export async function buildDist(outDir: string): Promise<string[]> {
  const esm = `${outDir}/lookout-renderer.js`;
  const iife = `${outDir}/lookout-renderer.iife.js`;
  await build({ ...common, format: 'esm', outfile: esm, sourcemap: true, minify: true });
  await build({ ...common, format: 'iife', globalName: 'LookoutRenderer', outfile: iife, sourcemap: true, minify: true });
  return [esm, iife];
}
