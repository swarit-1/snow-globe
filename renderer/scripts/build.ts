/** pnpm build — esbuild browser bundles: dist/snow-globe-renderer.js (ESM) and dist/snow-globe-renderer.iife.js. */
import { mkdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { buildDist } from '../src/node/bundle.ts';
import { PACKAGE_ROOT } from '../src/node/paths.ts';

const out = join(PACKAGE_ROOT, 'dist');
mkdirSync(out, { recursive: true });
buildDist(out)
  .then((files) => {
    for (const f of files) console.log(`${f} ${statSync(f).size} B`);
  })
  .catch((e: unknown) => {
    console.error(e instanceof Error ? e.message : e);
    process.exit(1);
  });
