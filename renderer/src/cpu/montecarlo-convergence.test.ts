import { describe, expect, it } from 'vitest';
import { DECK_BELOW_MC_CONFIG, DEFAULT_MC_CONFIG, runCoverageMonteCarlo } from './montecarlo.ts';

/**
 * Convergence of the renderer's cloud sampling (review finding: a constant 4 samples left near-horizon decks
 * under-drawn and unconverged). Threshold declared before measuring: halving the step and doubling the cap
 * moves no (cover, band) row of the drawn fraction or mean opacity by 2 points or more. Same seeds (paired).
 */
describe('cloud sampling convergence', () => {
  it('is converged: halving the step and doubling the cap moves no band by 2 points or more (300 seeds, paired)', () => {
    for (const cfg of [DEFAULT_MC_CONFIG, DECK_BELOW_MC_CONFIG]) {
      const base = runCoverageMonteCarlo({ ...cfg, seeds: 300 });
      const fine = runCoverageMonteCarlo({ ...cfg, seeds: 300, drawnSampling: { stepCellFraction: 1 / 32, maxSamples: 256 } });
      let worst = 0;
      base.rows.forEach((r, i) => {
        const f = fine.rows[i]!;
        worst = Math.max(worst, Math.abs(f.drawnFraction - r.drawnFraction), Math.abs(f.drawnOpacity - r.drawnOpacity));
      });
      console.log(`convergence (${cfg.seedPrefix}): max move ${(100 * worst).toFixed(2)} pts when the step halves`);
      expect(worst).toBeLessThan(0.02);
    }
  });
});
