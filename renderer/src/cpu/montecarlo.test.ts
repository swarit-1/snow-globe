import { describe, expect, it } from 'vitest';
import { DECK_BELOW_MC_CONFIG, DEFAULT_MC_CONFIG, formatMonteCarloReport, runCoverageMonteCarlo, type MonteCarloReport } from './montecarlo.ts';

/**
 * PRD §9.5 cloud test (the M1.5 gate): >= 1,000 seeds x cover 0.1-0.9, the rendered sky fraction within
 * 5 points of the view-ray target fraction, by elevation band. "Rendered" is what the renderer draws (slab
 * opacity from its own sampling, which test/gpu-cpu.test.ts ties to the GPU per pixel); the target is the
 * same field integrated along the same rays at a converged step. Mask coverage kappa is checked
 * separately against the mean column cover: that only tests the noise marginal (E[kappa] = cover).
 *
 * Thresholds, declared before measuring: 5 points (PRD) for both the >= 0.5-opacity fraction and the
 * mean opacity in every row; 5 points for kappa against the mean cover. Other layer types, convergence
 * and the negative controls are in montecarlo-layers, montecarlo-convergence and montecarlo-controls.
 */
function gate(rep: MonteCarloReport): void {
  console.log(formatMonteCarloReport(rep));
  expect(rep.config.seeds).toBeGreaterThanOrEqual(1000);
  expect(new Set(rep.rows.map((r) => r.cover)).size).toBe(9);
  expect(rep.maxAbsDiffFraction).toBeLessThanOrEqual(0.05);
  expect(rep.maxAbsDiffOpacity).toBeLessThanOrEqual(0.05);
  expect(rep.maxAbsKappaDiff).toBeLessThanOrEqual(0.05);
}

describe('coverage Monte Carlo (PRD §9.5, M1.5 gate)', () => {
  it('thick low deck above the camera, cover rising with distance (1,000 seeds)', () => {
    const rep = runCoverageMonteCarlo(DEFAULT_MC_CONFIG);
    gate(rep);
    // The target is neither the raw cover of the camera's column nor the mean column cover along the
    // ray: slant rays near the horizon cross many columns and almost always meet cloud.
    const low = rep.rows.find((r) => r.cover === 0.3 && r.band[0] === 0)!;
    expect(low.meanCover - low.rawCameraCover).toBeGreaterThan(0.1);
    expect(low.targetFraction - low.meanCover).toBeGreaterThan(0.2);
  });

  it('deck below the camera, rays below the horizon ending at the ground (1,000 seeds)', () => {
    const rep = runCoverageMonteCarlo(DECK_BELOW_MC_CONFIG);
    gate(rep);
    expect(rep.rows.every((r) => r.band[1] <= 0)).toBe(true);
  });
});
