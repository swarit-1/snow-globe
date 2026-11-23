/**
 * Builds what the Tauri shell will load (PRD §6.1: the overlay is TypeScript in the webview): the
 * overlay module as one minified ESM file, app/dist/overlay.js (git-ignored; the root .gitignore
 * keeps /app/dist/ for this package). It also checks the committed protocol.schema.json is current
 * and stops if not, since the shell's Rust tests validate against that file.
 *
 *   pnpm build            # then app/dist/overlay.js
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { DIST_DIR, overlayEsm } from './bundle.ts';
import { schemaText, SCHEMA_PATH } from './schema.ts';

if (import.meta.url === `file://${process.argv[1]}`) {
  if (readFileSync(SCHEMA_PATH, 'utf8') !== schemaText()) {
    console.error(`protocol.schema.json is stale: run \`node scripts/schema.ts\` and commit it (${SCHEMA_PATH})`);
    process.exit(1);
  }
  mkdirSync(DIST_DIR, { recursive: true });
  const out = `${DIST_DIR}overlay.js`;
  const js = await overlayEsm(true);
  writeFileSync(out, js);
  console.log(`wrote ${out} (${(Buffer.byteLength(js) / 1024).toFixed(1)} KiB)`);
}
