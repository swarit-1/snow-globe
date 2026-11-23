/**
 * Runs the browser key-mash stress (harness/stress.ts) in headless Chromium and prints its report as
 * JSON (PRD §10.3). `pnpm stress` runs the real overlay with 64 lanes of 160 cycles (10,240 in all);
 * flags change the variant, the lane count and the cycles.
 *
 *   node scripts/stress.ts [--variant real] [--lanes 64] [--cycles 160] [--seed c0ffee] [--out report.json]
 */
import { writeFileSync } from 'node:fs';
import { launch, pageWith } from '../test/browser.ts';
import type { StressOptions, StressReport } from '../harness/stress.ts';
import type { Variant } from '../harness/mutants.ts';
import { harnessBundle } from './bundle.ts';

/** Runs one stress variant in its own page of `browser`. */
export async function runInBrowser(browser: Awaited<ReturnType<typeof launch>>, bundle: string, o: StressOptions): Promise<StressReport> {
  const page = await pageWith(browser, bundle, { width: 1000, height: 1000 });
  try {
    return (await page.evaluate(
      (opts) => (globalThis as unknown as { SnowGlobeHarness: { runStress: (o: unknown) => Promise<unknown> } }).SnowGlobeHarness.runStress(opts),
      o,
    )) as StressReport;
  } finally {
    await page.context().close();
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const args = process.argv.slice(2);
  const get = (name: string, dflt: string): string => {
    const i = args.indexOf(`--${name}`);
    return i >= 0 && args[i + 1] !== undefined ? args[i + 1]! : dflt;
  };
  const o: StressOptions = { variant: get('variant', 'real') as Variant, lanes: Number(get('lanes', '64')), cyclesPerLane: Number(get('cycles', '160')), seed: get('seed', 'c0ffee') };
  const browser = await launch();
  try {
    const t0 = performance.now();
    const report = await runInBrowser(browser, await harnessBundle(), o);
    const text = JSON.stringify({ ...report, totalMs: Math.round(performance.now() - t0), browser: browser.version() }, null, 2);
    const out = get('out', '');
    if (out) writeFileSync(out, `${text}\n`);
    console.log(text);
  } finally {
    await browser.close();
  }
}
