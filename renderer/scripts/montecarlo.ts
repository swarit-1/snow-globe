/**
 * pnpm montecarlo [--case above|deck|thin|cirrus] [--seeds N] [--profile ramp|constant] [--variant correct|...] [--json]
 *
 * Prints the cloud-coverage Monte Carlo (PRD §9.5): the rendered sky fraction (share of rays whose drawn
 * slab opacity is >= 0.5) and the mean drawn opacity against the view-ray target (the same field along
 * the same rays at a converged step), by elevation band, >= 1,000 seeds x cover 0.1-0.9. Mask coverage
 * kappa is shown beside the mean column cover; it is not the gate. This is the M1.5 gate evidence.
 */
import { DECK_BELOW_MC_CONFIG, DEFAULT_MC_CONFIG, formatMonteCarloReport, runCoverageMonteCarlo, type MonteCarloConfig, type ProfileShape } from '../src/cpu/montecarlo.ts';
import type { CoverageVariant } from '../src/cpu/reference.ts';
import { parseArgs } from '../src/node/args.ts';

const CASES: Record<string, MonteCarloConfig> = {
  above: DEFAULT_MC_CONFIG,
  deck: DECK_BELOW_MC_CONFIG,
  thin: { ...DEFAULT_MC_CONFIG, profile: 'constant', layerThicknessM: 50, layerKind: 'mid', layerBaseAboveCameraM: 3000 },
  cirrus: { ...DEFAULT_MC_CONFIG, layerKind: 'high', layerBaseAboveCameraM: 6500, layerThicknessM: 1500, profile: 'constant' },
};

const { values, flags } = parseArgs(process.argv.slice(2), { values: ['case', 'seeds', 'profile', 'variant'], flags: ['json'] });
const base = CASES[values.case ?? 'above'];
if (!base) throw new Error(`--case must be one of ${Object.keys(CASES).join(', ')}`);
const rep = runCoverageMonteCarlo({
  ...base,
  seeds: values.seeds ? Number(values.seeds) : base.seeds,
  profile: (values.profile as ProfileShape | undefined) ?? base.profile,
  variant: (values.variant as CoverageVariant | undefined) ?? 'correct',
});
console.log(flags.has('json') ? JSON.stringify(rep, null, 2) : formatMonteCarloReport(rep));
console.log(`rendered vs view-ray target within 5 points everywhere: ${rep.maxAbsDiff <= 0.05 ? 'yes' : 'NO'} (max ${(100 * rep.maxAbsDiff).toFixed(2)} pts)`);
