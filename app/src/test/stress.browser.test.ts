import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { Browser } from 'playwright-core';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { CAUGHT_BY, VARIANTS, type Catch, type Variant } from '../harness/mutants.ts';
import type { StressReport } from '../harness/stress.ts';
import { harnessBundle } from '../scripts/bundle.ts';
import { runInBrowser } from '../scripts/stress.ts';
import { launch } from './browser.ts';

/**
 * The webview key-mash stress test in headless Chromium (PRD §10.3; PLAN.md M1 gate "0 stale paints in
 * >= 10,000 cycles"; ticket M1-13). The real overlay runs 64 lanes of 160 cycles (10,240 cycles), then
 * ten deliberately broken overlays run 2,400 cycles each and must each be caught.
 * Reports go to test-results/ (git-ignored) as well as the console.
 */
const REAL = { lanes: 64, cyclesPerLane: 160, seed: 'c0ffee' };
const BROKEN = { lanes: 16, cyclesPerLane: 150, seed: 'badc0de5' };
/** How many catches each broken overlay must show (stale paints and residue are plentiful; hold overruns need long holds, which are rarer). */
const AT_LEAST: Readonly<Record<Catch, number>> = { stalePaints: 10, residue: 10, holdOverruns: 5, unshownAcks: 10 };
/**
 * Per-variant floors where a variant's catches are rarer than its catch type's. esc-cancels-stages-only
 * goes stale only on frames already in flight when Esc is pressed: the verifier of 23 Nov measured 17, 15,
 * 12 and 11 catches in four full runs and 9 to 23 (median 12) in 16 more samples at this size, so a floor
 * of 10 failed about one run in 16 to 20 although the mutant was caught every time. The real overlay
 * must still show 0 of every violation, so any floor of 1 or more proves the mutant is caught.
 */
const AT_LEAST_VARIANT: Readonly<Partial<Record<Exclude<Variant, 'real'>, number>>> = { 'esc-cancels-stages-only': 3 };
const OUT = fileURLToPath(new URL('../test-results/', import.meta.url));

let browser: Browser;
let bundle: string;

beforeAll(async () => {
  bundle = await harnessBundle();
  browser = await launch();
});

afterAll(async () => {
  await browser?.close();
});

function save(name: string, r: unknown): void {
  mkdirSync(OUT, { recursive: true });
  writeFileSync(`${OUT}${name}.json`, `${JSON.stringify(r, null, 2)}\n`);
}

const ZERO = { stalePaints: 0, wrongDrops: 0, residue: 0, surfaceMismatches: 0, holdOverruns: 0, unjustifiedSkips: 0, unshownAcks: 0, missingAcks: 0, stuck: 0, protocolErrors: 0 };

describe('webview key-mash stress in headless Chromium (PRD §10.3)', () => {
  it('makes zero stale paints in more than 10,000 randomised cycles, with every check clean', async () => {
    const t0 = performance.now();
    const r: StressReport = await runInBrowser(browser, bundle, { variant: 'real', ...REAL });
    const totalMs = Math.round(performance.now() - t0);
    save('overlay-stress-real', { ...r, totalMs, browser: browser.version() });
    console.log(
      `real overlay: ${r.cycles} cycles in ${r.lanes} lanes, ${r.wallMs} ms of key mashing (${totalMs} ms with launch and drain); ` +
        `${r.counts.messages} messages, ${r.counts.framesSent} frames sent, ${r.counts.commits} painted (tier 0/1/2: ${r.counts.commitsTier0}/${r.counts.commitsTier1}/${r.counts.commitsTier2}), ` +
        `${r.counts.fades} cross-fades, ${r.counts.skips} Tier 0 skips, ${r.counts.holds} holds, ${r.counts.lateResults} late results, ${r.counts.scrubs} scrubs, ` +
        `${r.counts.escapes} Esc (${r.counts.panelEscapes} from the panel); dropped ${JSON.stringify(r.dropped)}; violations ${JSON.stringify(r.violations)}`,
    );
    for (const [k, v] of Object.entries(r.timings)) console.log(`  ${k}: ${JSON.stringify(v)}`);
    if (r.examples.length) console.log(`  examples: ${r.examples.join(' | ')}`);

    expect(r.cycles).toBeGreaterThanOrEqual(10_000);
    expect(r.violations).toEqual(ZERO);
    // Every drop path and every paint path was exercised.
    expect(r.dropped['generation@receipt'] ?? 0).toBeGreaterThan(1000);
    expect((r.dropped['scene@receipt'] ?? 0) + (r.dropped['scene@scene_change'] ?? 0) + (r.dropped['scene@frame'] ?? 0)).toBeGreaterThan(100);
    expect((r.dropped['tier@receipt'] ?? 0) + (r.dropped['tier@frame'] ?? 0)).toBeGreaterThan(20);
    expect((r.dropped['dismissed@receipt'] ?? 0) + (r.dropped['dismissed@frame'] ?? 0)).toBeGreaterThan(20);
    expect(r.counts.resetDrops ?? 0).toBeGreaterThan(500);
    expect(r.counts.lateResults ?? 0).toBeGreaterThan(1000);
    for (const k of [
      'commitsTier0', 'commitsTier1', 'commitsTier2', 'fades', 'fadesCut', 'skips', 'holds', 'heldThenPainted', 'heldThenSkipped', 'scrubs', 'panelEscapes',
      'duplicatesSent', 'decodeFailures', 'earlyParses', 'noMatchAfterScene', 'outOfRangeAfterScene', 'offersClicked', 'offersAnswered', 'acks',
    ]) {
      expect(r.counts[k] ?? 0, k).toBeGreaterThan(10);
    }
    expect(r.timings.previousMs!.n).toBeGreaterThan(100);
    expect(r.counts.layersAudited ?? 0).toBeGreaterThan(1000);
    // The run must not starve the frame loop, or it would not test real interleavings.
    expect(r.timings.frameIntervalMs!.p50).toBeLessThan(20);
    expect(r.env.crossOriginIsolated).toBe('true');
  });

  it('catches every deliberately broken overlay (as the Rust key-mash catches its broken gates)', async () => {
    const broken = VARIANTS.filter((v): v is Exclude<Variant, 'real'> => v !== 'real');
    const t0 = performance.now();
    const reports = await Promise.all(broken.map((variant) => runInBrowser(browser, bundle, { variant, ...BROKEN })));
    save('overlay-stress-broken', { totalMs: Math.round(performance.now() - t0), browser: browser.version(), reports });
    for (const r of reports) {
      const by = CAUGHT_BY[r.variant as Exclude<Variant, 'real'>];
      console.log(`broken ${r.variant}: ${r.cycles} cycles, ${r.wallMs} ms; caught by ${by}: ${r.violations[by]}; all ${JSON.stringify(r.violations)}`);
      expect(r.cycles).toBeGreaterThanOrEqual(2400);
      const floor = AT_LEAST_VARIANT[r.variant as Exclude<Variant, 'real'>] ?? AT_LEAST[by];
      expect(r.violations[by], `${r.variant} must be caught by ${by}`).toBeGreaterThanOrEqual(floor);
      expect(r.violations.protocolErrors).toBe(0);
    }
  });
});
