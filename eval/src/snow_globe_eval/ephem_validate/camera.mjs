// The renderer's own camera transform (renderer/src/math/camera.ts), for the ephemeris
// validation (PRD §8.4 "A transform into each viewpoint's camera frame, giving a pixel position
// and a light direction").
//
//   node camera.mjs <renderer_dir> <input.json> <output.json>
//
// Input: {"cases": [{"pose": {width, height, heading_deg, pitch_deg, roll_deg, hfov_deg},
//                    "alt_az": [[alt_deg, az_deg], ...]}, ...]}
// Output: {"cases": [{"enu": [[x, y, z], ...],            altAzToDirection (the light direction)
//                     "px": [[px, py] | null, ...],      projectDirection(cameraBasis(pose), enu)
//                     "back": [[alt, az] | null, ...]}]}  directionToAltAz(pixelRay(basis, px, py))
// Needs Node 24: it imports the renderer's TypeScript directly (type stripping); camera.ts needs
// no npm packages and no browser.
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [rendererDir, input, output] = process.argv.slice(2);
if (!rendererDir || !input || !output) {
  console.error('usage: node camera.mjs <renderer_dir> <input.json> <output.json>');
  process.exit(2);
}
const camera = await import(pathToFileURL(join(rendererDir, 'src', 'math', 'camera.ts')).href);
const job = JSON.parse(readFileSync(input, 'utf8'));
const cases = [];
for (const item of job.cases) {
  const basis = camera.cameraBasis(item.pose);
  const enu = [];
  const px = [];
  const back = [];
  for (const [alt, az] of item.alt_az) {
    const d = camera.altAzToDirection(alt, az);
    enu.push(d);
    const p = camera.projectDirection(basis, d);
    if (p === null) {
      px.push(null);
      back.push(null);
    } else {
      px.push([p.px, p.py]);
      const r = camera.directionToAltAz(camera.pixelRay(basis, p.px, p.py));
      back.push([r.alt_deg, r.az_deg]);
    }
  }
  cases.push({ enu, px, back });
}
writeFileSync(output, JSON.stringify({ cases }));
