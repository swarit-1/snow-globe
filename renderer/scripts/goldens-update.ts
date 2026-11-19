/**
 * pnpm goldens:update — regenerates the synthetic plate (goldens/plate/*.png, plate <= 100 KB), points the
 * golden scenes at it (plate.id and the three file hashes), renders the three goldens (clear, low-deck,
 * fog) on Chromium/SwiftShader, and rewrites goldens/manifest.json. It refuses any scene that is not the
 * synthetic viewpoint on the synthetic plate. Run it only when a pixel change is intended, and review the
 * new PNGs before committing them.
 */
import { updateGoldens } from '../src/node/goldens.ts';

updateGoldens()
  .then((m) => console.log(`goldens written with ${m.gl_renderer} on ${m.platform.os}/${m.platform.arch} (${m.browser})`))
  .catch((e: unknown) => {
    console.error(e instanceof Error ? e.message : e);
    process.exit(1);
  });
