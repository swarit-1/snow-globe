/**
 * Writes the overlay preview page, app/dist/preview.html: one self-contained file (the bundle is
 * inlined) that opens from disk in any browser. app/dist/ is git-ignored (the root .gitignore keeps
 * it for this package's bundle).
 *
 *   pnpm preview            # then open app/dist/preview.html
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { DIST_DIR, previewBundle } from './bundle.ts';

/** The preview page's HTML with its script inlined. */
export async function previewHtml(): Promise<string> {
  const js = (await previewBundle()).replace(/<\/script/gi, '<\\/script');
  return `<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Snow Globe overlay preview</title></head><body><script>${js}</script></body></html>\n`;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  mkdirSync(DIST_DIR, { recursive: true });
  const out = `${DIST_DIR}preview.html`;
  writeFileSync(out, await previewHtml());
  console.log(`wrote ${out}`);
}
