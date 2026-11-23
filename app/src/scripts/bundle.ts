/**
 * Builds the overlay's browser bundles with esbuild, in memory or to app/dist/ (git-ignored; the
 * root .gitignore keeps /app/dist/ for this bundle). The headless tests build from source on every
 * run, so they can never test a stale bundle.
 */
import { fileURLToPath } from 'node:url';
import { build, type BuildOptions } from 'esbuild';

export const PACKAGE_ROOT = fileURLToPath(new URL('..', import.meta.url));
export const DIST_DIR = fileURLToPath(new URL('../../dist/', import.meta.url));

const common: BuildOptions = { bundle: true, platform: 'browser', target: ['es2022', 'safari17'], legalComments: 'eof', logLevel: 'silent', write: false };

/** The harness page bundle (overlay + samples + stress run) as an IIFE string. */
export async function harnessBundle(): Promise<string> {
  const r = await build({ ...common, entryPoints: [fileURLToPath(new URL('../harness/page.ts', import.meta.url))], format: 'iife' });
  return r.outputFiles![0]!.text;
}

/** The preview page bundle as an IIFE string. */
export async function previewBundle(): Promise<string> {
  const r = await build({ ...common, entryPoints: [fileURLToPath(new URL('../preview/preview.ts', import.meta.url))], format: 'iife' });
  return r.outputFiles![0]!.text;
}

/** The overlay module as ESM (what the Tauri shell will load), minified, as a string. */
export async function overlayEsm(minify = true): Promise<string> {
  const r = await build({ ...common, entryPoints: [fileURLToPath(new URL('../overlay/index.ts', import.meta.url))], format: 'esm', minify });
  return r.outputFiles![0]!.text;
}
