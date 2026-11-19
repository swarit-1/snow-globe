import { describe, expect, it } from 'vitest';
import { DEFAULT_MC_CONFIG, formatMonteCarloReport, runCoverageMonteCarlo, type MonteCarloReport } from './montecarlo.ts';

/**
 * PRD §9.5 cloud test on two more layer types (see montecarlo.test.ts for the gate and its thresholds):
 * a thin slab, and semi-transparent cirrus, whose opacity often never reaches 0.5, so the mean-opacity
 * comparison carries the test there.
 */
function gate(rep: MonteCarloReport): void {
  console.log(formatMonteCarloReport(rep));
  expect(rep.config.seeds).toBeGreaterThanOrEqual(1000);
  expect(new Set(rep.rows.map((r) => r.cover)).size).toBe(9);
  expect(rep.maxAbsDiffFraction).toBeLessThanOrEqual(0.05);
  expect(rep.maxAbsDiffOpacity).toBeLessThanOrEqual(0.05);
  expect(rep.maxAbsKappaDiff).toBeLessThanOrEqual(0.05);
}

describe('coverage Monte Carlo (PRD §9.5): other layer types', () => {
  it('thin mid layer, constant cover (1,000 seeds)', () => {
    gate(runCoverageMonteCarlo({ ...DEFAULT_MC_CONFIG, profile: 'constant', layerThicknessM: 50, layerKind: 'mid', layerBaseAboveCameraM: 3000 }));
  });

  it('semi-transparent cirrus, constant cover (1,000 seeds)', () => {
    gate(runCoverageMonteCarlo({ ...DEFAULT_MC_CONFIG, layerKind: 'high', layerBaseAboveCameraM: 6500, layerThicknessM: 1500, profile: 'constant' }));
  });
});
