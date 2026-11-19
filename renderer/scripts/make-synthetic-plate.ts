/**
 * pnpm plate:synthetic — writes only the synthetic plate set (plate, sky mask, depth) to goldens/plate/.
 * The plate is ray-cast procedurally (no camera imagery) and must stay <= 100 KB. If the files change, run
 * pnpm goldens:update next: the golden scenes name the plate by file hash.
 */
import { writeFileSync } from 'node:fs';
import { encodeSyntheticPlate } from '../src/node/goldens.ts';
import { SYNTHETIC_PLATE_FILES, SYNTHETIC_PLATE_MAX_BYTES } from '../src/node/paths.ts';

const enc = encodeSyntheticPlate();
if (enc.plate.length > SYNTHETIC_PLATE_MAX_BYTES) {
  console.error(`synthetic plate is ${enc.plate.length} bytes; the limit is ${SYNTHETIC_PLATE_MAX_BYTES}`);
  process.exit(1);
}
writeFileSync(SYNTHETIC_PLATE_FILES.plate, enc.plate);
writeFileSync(SYNTHETIC_PLATE_FILES.skyMask, enc.skyMask);
writeFileSync(SYNTHETIC_PLATE_FILES.depth, enc.depth);
console.log(JSON.stringify({ plate_bytes: enc.plate.length, sky_mask_bytes: enc.skyMask.length, depth_bytes: enc.depth.length, ...enc.stats }, null, 2));
