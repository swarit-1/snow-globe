/**
 * pnpm render --scene s.json --plate p.jpg [--sky-mask m.png] [--depth d.png] --out out.png
 *              [--backend swiftshader|gpu] [--allow-version-mismatch]
 *
 * Renders one SceneSpec headless (Chromium + SwiftShader by default) and writes out.png plus out.json
 * (renderer_version, GL renderer string, scene hash, render ms). Relative paths resolve against the
 * directory pnpm was run from. Renders of real camera plates belong under $SNOW_GLOBE_DATA_DIR, not in git.
 */
import { parseArgs, userPath } from '../src/node/args.ts';
import { renderToFiles } from '../src/node/render-files.ts';

const USAGE = 'usage: pnpm render --scene s.json --plate p.jpg [--sky-mask m.png] [--depth d.png] --out out.png [--backend swiftshader|gpu] [--allow-version-mismatch]';

async function main(): Promise<void> {
  const { values, flags } = parseArgs(process.argv.slice(2), {
    values: ['scene', 'plate', 'sky-mask', 'depth', 'out', 'backend', 'meta'],
    flags: ['allow-version-mismatch', 'help'],
  });
  if (flags.has('help') || !values.scene || !values.plate || !values.out) {
    console.error(USAGE);
    process.exit(flags.has('help') ? 0 : 2);
  }
  const backend = (values.backend ?? 'swiftshader') as 'swiftshader' | 'gpu';
  if (backend !== 'swiftshader' && backend !== 'gpu') throw new Error('--backend must be swiftshader or gpu');
  const meta = await renderToFiles({
    scenePath: userPath(values.scene),
    platePath: userPath(values.plate),
    skyMaskPath: values['sky-mask'] ? userPath(values['sky-mask']) : null,
    depthPath: values.depth ? userPath(values.depth) : null,
    outPath: userPath(values.out),
    ...(values.meta ? { metaPath: userPath(values.meta) } : {}),
    backend,
    allowVersionMismatch: flags.has('allow-version-mismatch'),
  });
  console.log(
    JSON.stringify(
      {
        out: meta.output.path,
        sha256: meta.output.sha256,
        scene_hash: meta.scene_hash,
        gl_renderer: meta.gl_renderer,
        render_ms: Number(meta.timing_ms.render.toFixed(1)),
        total_ms: Number(meta.timing_ms.total.toFixed(0)),
      },
      null,
      2,
    ),
  );
}

main().catch((e: unknown) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
