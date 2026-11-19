import { describe, expect, it } from 'vitest';
import { DECK_BELOW_MC_CONFIG, DEFAULT_MC_CONFIG, runCoverageMonteCarlo, type MonteCarloConfig } from './montecarlo.ts';

/**
 * Negative controls for the coverage Monte Carlo: the same experiment with a deliberately broken drawn
 * path (the target always uses the correct model) must miss the 5-point gate somewhere, which shows the
 * gate can detect each error. 300 seeds each.
 */
const THIN: MonteCarloConfig = { ...DEFAULT_MC_CONFIG, profile: 'constant', layerThicknessM: 50, layerKind: 'mid', layerBaseAboveCameraM: 3000 };

describe('coverage Monte Carlo negative controls', () => {
  for (const variant of ['filter-before-threshold', 'unnormalized', 'four-samples'] as const) {
    it(`"${variant}" misses by more than 5 points somewhere (thick low deck)`, () => {
      const rep = runCoverageMonteCarlo({ ...DEFAULT_MC_CONFIG, seeds: 300, variant });
      const worst = rep.rows.reduce((a, b) => (Math.abs(b.diffFraction) > Math.abs(a.diffFraction) ? b : a));
      console.log(`${variant}: max |drawn - target| ${(100 * rep.maxAbsDiff).toFixed(1)} pts, worst at cover ${worst.cover}, band [${worst.band}]`);
      expect(rep.maxAbsDiff).toBeGreaterThan(0.05);
    });
  }

  it('flat-Earth geometry misses by more than 5 points on the thin layer and on the deck below the camera', () => {
    // On the thick deck above the camera it misses by only 5.8 points (measured 2026-11-19, 300 seeds),
    // so that case is reported, not gated; curvature is also enforced by the exact-geometry tests.
    for (const [name, cfg] of [
      ['thin layer', THIN],
      ['deck below', DECK_BELOW_MC_CONFIG],
      ['thick deck above (reported only)', DEFAULT_MC_CONFIG],
    ] as const) {
      const rep = runCoverageMonteCarlo({ ...cfg, seeds: 300, variant: 'flat-earth' });
      console.log(`flat-earth, ${name}: max |drawn - target| ${(100 * rep.maxAbsDiff).toFixed(1)} pts`);
      if (cfg !== DEFAULT_MC_CONFIG) expect(rep.maxAbsDiff).toBeGreaterThan(0.05);
    }
  });
});
