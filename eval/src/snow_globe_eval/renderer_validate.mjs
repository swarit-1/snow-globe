// Validates SceneSpec JSON files with the renderer's own validateScene() (renderer/src/scene.ts).
//
//   node renderer_validate.mjs <renderer_dir> [--allow-synthetic-sun] scene.json...
//
// Prints a JSON array of {file, ok, issues}, one per scene. Used by snow_globe_eval.scene
// renderer_validate(). Needs Node 24: it imports the renderer's TypeScript directly (type
// stripping); validateScene() needs no npm packages and no browser.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [rendererDir, ...rest] = process.argv.slice(2);
if (!rendererDir) {
  console.error('usage: node renderer_validate.mjs <renderer_dir> [--allow-synthetic-sun] scene.json...');
  process.exit(2);
}
const allowSyntheticSun = rest.includes('--allow-synthetic-sun');
const files = rest.filter((arg) => !arg.startsWith('--'));
const { validateScene } = await import(pathToFileURL(join(rendererDir, 'src', 'scene.ts')).href);
const results = [];
for (const file of files) {
  try {
    validateScene(JSON.parse(readFileSync(file, 'utf8')), { allowSyntheticSun });
    results.push({ file, ok: true, issues: [] });
  } catch (err) {
    if (err && err.name === 'SceneValidationError' && Array.isArray(err.issues)) {
      results.push({ file, ok: false, issues: err.issues });
    } else {
      throw err;
    }
  }
}
process.stdout.write(JSON.stringify(results));
