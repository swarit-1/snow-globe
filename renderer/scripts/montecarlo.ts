/**
 * pnpm montecarlo [--case above|deck|thin|cirrus] [--seeds N] [--profile ramp|constant] [--variant correct|...] [--json]
 *
 * Prints the cloud-coverage Monte Carlo (PRD §9.5): the rendered sky fraction (share of rays whose drawn
 * slab opacity is >= 0.5) and the mean drawn opacity against the view-ray target (the same field along
 * the same rays at a converged step), by elevation band, >= 1,000 seeds x cover 0.1-0.9. Mask coverage
 * kappa is shown beside the mean column cover; it is not the gate. This is the M1.5 gate evidence.
 *
 * Every run records its provenance: RENDERER_VERSION, the git commit and whether the renderer's files
 * differ from it, the time, the load average, and the SHA-256 of every source file the Monte Carlo's
 * computation runs (src/cpu/montecarlo.ts and its imports), so a result carries over to a new renderer
 * version exactly when those files are byte-identical. With
 * --json, stdout is one JSON document (the report, its provenance and the gate verdict); the verdict line
 * goes to stderr.
 */
import { DECK_BELOW_MC_CONFIG, DEFAULT_MC_CONFIG, formatMonteCarloReport, runCoverageMonteCarlo, type MonteCarloConfig, type ProfileShape } from '../src/cpu/montecarlo.ts';
import type { CoverageVariant } from '../src/cpu/reference.ts';
import { fileURLToPath } from 'node:url';
import { parseArgs } from '../src/node/args.ts';
import { provenance, sourceHashes } from '../src/node/provenance.ts';

const CASES: Record<string, MonteCarloConfig> = {
  above: DEFAULT_MC_CONFIG,
  deck: DECK_BELOW_MC_CONFIG,
  thin: { ...DEFAULT_MC_CONFIG, profile: 'constant', layerThicknessM: 50, layerKind: 'mid', layerBaseAboveCameraM: 3000 },
  cirrus: { ...DEFAULT_MC_CONFIG, layerKind: 'high', layerBaseAboveCameraM: 6500, layerThicknessM: 1500, profile: 'constant' },
};

const { values, flags } = parseArgs(process.argv.slice(2), { values: ['case', 'seeds', 'profile', 'variant'], flags: ['json'] });
const caseName = values.case ?? 'above';
const base = CASES[caseName];
if (!base) throw new Error(`--case must be one of ${Object.keys(CASES).join(', ')}`);
const before = provenance();
const rep = runCoverageMonteCarlo({
  ...base,
  seeds: values.seeds ? Number(values.seeds) : base.seeds,
  profile: (values.profile as ProfileShape | undefined) ?? base.profile,
  variant: (values.variant as CoverageVariant | undefined) ?? 'correct',
});
const sources = sourceHashes(fileURLToPath(new URL('../src/cpu/montecarlo.ts', import.meta.url)));
const verdict = `rendered vs view-ray target within 5 points everywhere: ${rep.maxAbsDiff <= 0.05 ? 'yes' : 'NO'} (max ${(100 * rep.maxAbsDiff).toFixed(2)} pts)`;
const prov = { case: caseName, ...before, load_average_after: provenance().load_average, sources };
if (flags.has('json')) {
  console.log(JSON.stringify({ ...rep, provenance: prov, gate: { max_abs_diff: rep.maxAbsDiff, within_5_points: rep.maxAbsDiff <= 0.05 } }, null, 2));
  console.error(verdict);
} else {
  const g = before.git;
  console.log(`renderer ${before.renderer_version}, commit ${g.commit ?? 'unknown'}${g.dirty ? ' (renderer files differ from it)' : ''}, ${before.measured_utc}, load ${before.load_average.join(' ')}`);
  console.log(formatMonteCarloReport(rep));
  console.log(verdict);
}
