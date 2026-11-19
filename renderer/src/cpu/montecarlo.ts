/**
 * Cloud-coverage Monte Carlo (PRD §9.5, §13.1 "Cloud Monte Carlo: rendered vs view-ray target fraction,
 * within 5 points"; the M1.5 gate evidence).
 *
 * For each seed, cover level and ray (elevation bands x azimuths), one slab is crossed twice:
 *
 * - drawn: the renderer's own sampling (cloudSampleCount along the in-slab path, threshold before filter,
 *   the opaque stop) gives the slab opacity alpha that the shader composites. test/gpu-cpu.test.ts checks
 *   that the GPU's alpha debug output equals this per pixel.
 * - view-ray target: the same cloud field integrated along the same ray at a fine reference step
 *   (referenceStepFraction of the octave-0 noise cell, no sample cap): what the ray really meets. A slant
 *   ray crosses many columns, so the share of rays that meet cloud is larger than the area cover, the
 *   more so the lower the ray (PRD §9.5: "a camera sees slant rays crossing many columns").
 *
 * Reported per (cover, elevation band):
 * - meanCover: forecast cover read at the drawn sample points (an area fraction).
 * - kappa: mask coverage, the mean of the binary masks before opacity. By linearity E[kappa] = meanCover,
 *   so kappa only checks the noise marginal and the threshold-before-filter rule. It is NOT the rendered
 *   sky fraction (renderer 0.1.0 reported it as "rendered"; that gate could not see what was drawn).
 * - drawnFraction / targetFraction: share of rays whose drawn / converged slab opacity is >= 0.5. The
 *   drawn one is the rendered sky fraction; the converged one is the view-ray target fraction.
 * - drawnOpacity / targetOpacity: mean opacity (alpha-weighted cloud fraction), the meaningful statistic
 *   for semi-transparent cirrus, whose opacity often never reaches 0.5.
 *
 * Gate: |drawnFraction - targetFraction| <= 5 points and |drawnOpacity - targetOpacity| <= 5 points in
 * every row. Negative controls (variants of the drawn path only; the target always uses the correct
 * model) show the gate has power.
 */
import { SLAB_TYPES, type SlabKind, LAYER_SLOT } from '../clouds.ts';
import { altAzToDirection } from '../math/camera.ts';
import { CLOUD_MAX_SAMPLES, CLOUD_MIN_SAMPLES, CLOUD_STEP_CELL_FRACTION, CLOUD_TAU_OPAQUE, EARTH_RADIUS_M, T_INFINITE_M, cloudSampleCount } from '../math/constants.ts';
import { coverAt, type CoverKnot } from '../math/cover.ts';
import { coverThreshold } from '../math/coverage.ts';
import { rayPointGroundDistance } from '../math/geodesy.ts';
import { cloudNoise, layerNoiseTransforms, lazyLattice, seedToUint32 } from '../math/noise.ts';
import { intervalLength, slabIntervals, sphereInside } from '../math/shell.ts';
import type { Vec3 } from '../math/vec3.ts';
import type { CoverageVariant } from './reference.ts';

export type ProfileShape = 'constant' | 'ramp';

export interface MonteCarloConfig {
  seeds: number;
  covers: number[];
  /** Elevation bands in degrees, [lo, hi); negative bands look down at a deck below the camera. */
  bands: [number, number][];
  elevationsPerBand: number;
  azimuths: number;
  hCamM: number;
  layerKind: Exclude<SlabKind, 'ceiling'>;
  /** Slab base relative to the camera (m); negative for a deck below the camera. */
  layerBaseAboveCameraM: number;
  layerThicknessM: number;
  /** Ground height (m above MSL) where downward rays end after crossing the slab; null: no ground. */
  groundM: number | null;
  profile: ProfileShape;
  /** Constant wind (m/s) and lead time (s): the field's drift since the run started (math/advection.ts). */
  windU: number;
  windV: number;
  leadS: number;
  variant: CoverageVariant;
  seedPrefix: string;
  /** Step of the converged reference, as a fraction of the octave-0 noise cell. */
  referenceStepFraction: number;
  /** Replaces the renderer's sampling rule on the drawn path (convergence tests only). */
  drawnSampling?: { stepCellFraction: number; maxSamples: number };
}

/** Rays above the horizon through a thick low deck 1 km above a camera at 1,650 m, cover rising with distance. */
export const DEFAULT_MC_CONFIG: MonteCarloConfig = {
  seeds: 1000,
  covers: [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
  bands: [
    [0, 2],
    [2, 5],
    [5, 10],
    [10, 20],
    [20, 45],
    [45, 90],
  ],
  elevationsPerBand: 6,
  azimuths: 12,
  hCamM: 1650,
  layerKind: 'low',
  layerBaseAboveCameraM: 1000,
  layerThicknessM: 600,
  groundM: null,
  profile: 'ramp',
  windU: 7,
  windV: -3,
  leadS: 6 * 3600,
  variant: 'correct',
  seedPrefix: 'mc-seed',
  referenceStepFraction: 1 / 64,
};

/** Rays below the horizon onto a 1,000-1,400 m deck under a camera at 1,650 m; the ground at 900 m ends them. */
export const DECK_BELOW_MC_CONFIG: MonteCarloConfig = {
  ...DEFAULT_MC_CONFIG,
  bands: [
    [-2, -1],
    [-5, -2],
    [-10, -5],
    [-20, -10],
    [-45, -20],
    [-90, -45],
  ],
  layerBaseAboveCameraM: -650,
  layerThicknessM: 400,
  groundM: 900,
  seedPrefix: 'mc-deck-seed',
};

/** The cover profile used for a nominal cover c: constant, or a ramp from c - 0.15 (camera) to c + 0.15 (>= 120 km). */
export function mcProfile(shape: ProfileShape, c: number): CoverKnot[] {
  if (shape === 'constant') return [{ distance_km: 0, cover_0_1: c }];
  const clamp = (v: number) => Math.min(1, Math.max(0, v));
  return [
    { distance_km: 0, cover_0_1: clamp(c - 0.15) },
    { distance_km: 120, cover_0_1: clamp(c + 0.15) },
  ];
}

export interface MonteCarloRow {
  cover: number;
  band: [number, number];
  rays: number;
  /** Raw forecast cover at the camera's own column (what the target is not). */
  rawCameraCover: number;
  /** Mean forecast cover at the drawn sample points: the expectation of kappa. */
  meanCover: number;
  /** Mean mask coverage (before opacity). Not the rendered sky fraction. */
  kappa: number;
  /** kappa - meanCover. */
  kappaDiff: number;
  /** Rendered sky fraction: share of rays whose drawn opacity is >= 0.5. */
  drawnFraction: number;
  /** View-ray target fraction: share of rays whose converged opacity is >= 0.5. */
  targetFraction: number;
  /** drawnFraction - targetFraction. */
  diffFraction: number;
  /** Mean drawn opacity. */
  drawnOpacity: number;
  /** Mean converged opacity. */
  targetOpacity: number;
  /** drawnOpacity - targetOpacity. */
  diffOpacity: number;
  /** Standard deviation across seeds of the band-mean drawn fraction. */
  seedSdFraction: number;
  /** Mean renderer samples per ray. */
  samples: number;
  /** Mean reference samples per ray (the whole path; the opaque stop usually ends it sooner). */
  referenceSamples: number;
}

export interface MonteCarloReport {
  config: MonteCarloConfig;
  /** The drawn path's sampling rule (the renderer's unless config.drawnSampling replaced it). */
  sampling: { stepCellFraction: number; minSamples: number; maxSamples: number; tauOpaque: number };
  rows: MonteCarloRow[];
  maxAbsDiffFraction: number;
  maxAbsDiffOpacity: number;
  maxAbsKappaDiff: number;
  /** The gate statistic: the larger of maxAbsDiffFraction and maxAbsDiffOpacity. */
  maxAbsDiff: number;
  /** Reference noise evaluations actually made (after the opaque stop), for the run-time record. */
  referenceEvaluations: number;
  elapsedMs: number;
}

interface RaySamples {
  n: number;
  L: number;
  x: Float64Array;
  y: Float64Array;
  /** Per cover level, per sample: the threshold of the forecast cover there. */
  thr: Float64Array[];
  /** Per cover level: mean forecast cover over the samples. */
  meanCover: Float64Array;
}

interface Ray {
  band: number;
  drawn: RaySamples;
  ref: RaySamples;
}

/** Ray end: the first meeting with the ground shell, or "infinity" when there is no ground or it is missed. */
function rayEnd(hCam: number, dir: Vec3, groundM: number | null, R: number): number {
  if (groundM === null || !(dir[2] < 0)) return T_INFINITE_M;
  const iv = sphereInside(hCam, dir[2], groundM, R);
  return iv.lo <= iv.hi && iv.lo >= 0 ? iv.lo : T_INFINITE_M;
}

function raySamples(
  cfg: MonteCarloConfig,
  dir: Vec3,
  base: number,
  top: number,
  R: number,
  count: (L: number) => number,
  profiles: CoverKnot[][],
): RaySamples {
  const tEnd = rayEnd(cfg.hCamM, dir, cfg.groundM, R);
  const [near, far] = slabIntervals(cfg.hCamM, dir[2], base, top, 0, tEnd, R);
  const l1 = intervalLength(near);
  const L = l1 + intervalLength(far);
  const n = L > 0 ? count(L) : 0;
  const x = new Float64Array(n);
  const y = new Float64Array(n);
  const thr = profiles.map(() => new Float64Array(n));
  const meanCover = new Float64Array(profiles.length);
  for (let k = 0; k < n; k++) {
    const s = ((k + 0.5) / n) * L;
    const t = s < l1 ? near.lo + s : far.lo + (s - l1);
    x[k] = t * dir[0];
    y[k] = t * dir[1];
    const dKm = rayPointGroundDistance(cfg.hCamM, dir, t, R) / 1000;
    profiles.forEach((p, ci) => {
      const c = coverAt(p, dKm);
      thr[ci]![k] = coverThreshold(c);
      meanCover[ci]! += c / n;
    });
  }
  return { n, L, x, y, thr, meanCover };
}

/** Runs the coverage Monte Carlo and returns one row per (cover, elevation band). */
export function runCoverageMonteCarlo(cfg: MonteCarloConfig = DEFAULT_MC_CONFIG): MonteCarloReport {
  const t0 = performance.now();
  const base = cfg.hCamM + cfg.layerBaseAboveCameraM;
  const top = base + cfg.layerThicknessM;
  const renderR = cfg.variant === 'flat-earth' ? 1e12 : EARTH_RADIUS_M;
  const type = SLAB_TYPES[cfg.layerKind];
  const step = type.cellSizeM * CLOUD_STEP_CELL_FRACTION;
  const refStep = type.cellSizeM * cfg.referenceStepFraction;
  const profiles = cfg.covers.map((c) => mcProfile(cfg.profile, c));
  const beta = type.betaPerM;
  const override = cfg.drawnSampling;
  const drawnCount = (L: number): number => {
    if (cfg.variant === 'four-samples') return 4;
    if (override) return Math.min(override.maxSamples, Math.max(CLOUD_MIN_SAMPLES, Math.ceil(L / (type.cellSizeM * override.stepCellFraction))));
    return cloudSampleCount(L, step);
  };

  const rays: Ray[] = [];
  cfg.bands.forEach(([lo, hi], b) => {
    for (let i = 0; i < cfg.elevationsPerBand; i++) {
      const el = lo + ((i + 0.5) * (hi - lo)) / cfg.elevationsPerBand;
      for (let j = 0; j < cfg.azimuths; j++) {
        const dir = altAzToDirection(el, ((j + 0.5) * 360) / cfg.azimuths);
        rays.push({
          band: b,
          drawn: raySamples(cfg, dir, base, top, renderR, drawnCount, profiles),
          ref: raySamples(cfg, dir, base, top, EARTH_RADIUS_M, (L) => Math.max(CLOUD_MIN_SAMPLES, Math.ceil(L / refStep)), profiles),
        });
      }
    }
  });

  const nb = cfg.bands.length;
  const nc = cfg.covers.length;
  const bandCount = new Array<number>(nb).fill(0);
  for (const r of rays) bandCount[r.band]!++;
  const cell = (ci: number, b: number) => ci * nb + b;
  const sumKappa = new Float64Array(nc * nb);
  const sumDrawnOp = new Float64Array(nc * nb);
  const sumDrawnFrac = new Float64Array(nc * nb);
  const sumRefOp = new Float64Array(nc * nb);
  const sumRefFrac = new Float64Array(nc * nb);
  const sumSeedFrac = new Float64Array(nc * nb);
  const sumSeedFracSq = new Float64Array(nc * nb);
  const seedFrac = new Float64Array(nc * nb);
  const maxN = Math.max(1, ...rays.map((r) => r.drawn.n));
  const noise = new Float64Array(maxN);
  const mRef = new Int32Array(nc);
  const saturated = new Uint8Array(nc);
  const [dispE, dispN] = [cfg.windU * cfg.leadS, cfg.windV * cfg.leadS];
  const normalize = cfg.variant !== 'unnormalized';
  let refEvaluations = 0;

  for (let s = 0; s < cfg.seeds; s++) {
    const seedU32 = seedToUint32(`${cfg.seedPrefix}-${s}`);
    const lookup = lazyLattice(seedU32);
    const transforms = layerNoiseTransforms(seedU32, LAYER_SLOT[cfg.layerKind], type.cellSizeM, dispE, dispN);
    seedFrac.fill(0);
    for (const r of rays) {
      // Drawn: the renderer's sampling.
      const d = r.drawn;
      if (d.n > 0) {
        for (let k = 0; k < d.n; k++) noise[k] = cloudNoise(lookup, transforms, d.x[k]!, d.y[k]!, normalize);
        const tauPerSample = (beta * d.L) / d.n;
        for (let ci = 0; ci < nc; ci++) {
          let kappa: number;
          let alpha: number;
          if (cfg.variant === 'filter-before-threshold') {
            let nMean = 0;
            for (let k = 0; k < d.n; k++) nMean += noise[k]! / d.n;
            kappa = nMean < coverThreshold(d.meanCover[ci]!) ? 1 : 0;
            alpha = 1 - Math.exp(-beta * kappa * d.L);
          } else {
            const th = d.thr[ci]!;
            let m = 0;
            let mDrawn = -1;
            for (let k = 0; k < d.n; k++) {
              if (noise[k]! < th[k]!) m++;
              if (mDrawn < 0 && m * tauPerSample >= CLOUD_TAU_OPAQUE) mDrawn = m;
            }
            if (mDrawn < 0) mDrawn = m;
            kappa = m / d.n;
            alpha = 1 - Math.exp(-mDrawn * tauPerSample);
          }
          const slot = cell(ci, r.band);
          sumKappa[slot]! += kappa;
          sumDrawnOp[slot]! += alpha;
          if (alpha >= 0.5) {
            sumDrawnFrac[slot]!++;
            seedFrac[slot]!++;
          }
        }
      }
      // View-ray target: the same field along the same ray at the converged step (correct model always).
      const q = r.ref;
      if (q.n > 0) {
        mRef.fill(0);
        saturated.fill(0);
        const tauPerSample = (beta * q.L) / q.n;
        let open = nc;
        for (let k = 0; k < q.n && open > 0; k++) {
          const v = cloudNoise(lookup, transforms, q.x[k]!, q.y[k]!, true);
          refEvaluations++;
          for (let ci = 0; ci < nc; ci++) {
            if (saturated[ci]) continue;
            if (v < q.thr[ci]![k]!) {
              mRef[ci]!++;
              if (mRef[ci]! * tauPerSample >= CLOUD_TAU_OPAQUE) {
                saturated[ci] = 1;
                open--;
              }
            }
          }
        }
        for (let ci = 0; ci < nc; ci++) {
          const alpha = 1 - Math.exp(-mRef[ci]! * tauPerSample);
          const slot = cell(ci, r.band);
          sumRefOp[slot]! += alpha;
          if (alpha >= 0.5) sumRefFrac[slot]!++;
        }
      }
    }
    for (let ci = 0; ci < nc; ci++) {
      for (let b = 0; b < nb; b++) {
        const v = seedFrac[cell(ci, b)]! / bandCount[b]!;
        sumSeedFrac[cell(ci, b)]! += v;
        sumSeedFracSq[cell(ci, b)]! += v * v;
      }
    }
  }

  const rows: MonteCarloRow[] = [];
  let maxAbsDiffFraction = 0;
  let maxAbsDiffOpacity = 0;
  let maxAbsKappaDiff = 0;
  const raysIn = (b: number) => rays.filter((r) => r.band === b);
  cfg.covers.forEach((c, ci) => {
    cfg.bands.forEach((band, b) => {
      const inBand = raysIn(b);
      const nRays = cfg.seeds * bandCount[b]!;
      const slot = cell(ci, b);
      const meanCover = inBand.reduce((a, r) => a + (r.drawn.n > 0 ? r.drawn.meanCover[ci]! : 0), 0) / inBand.length;
      const kappa = sumKappa[slot]! / nRays;
      const drawnFraction = sumDrawnFrac[slot]! / nRays;
      const targetFraction = sumRefFrac[slot]! / nRays;
      const drawnOpacity = sumDrawnOp[slot]! / nRays;
      const targetOpacity = sumRefOp[slot]! / nRays;
      const seedMean = sumSeedFrac[slot]! / cfg.seeds;
      const row: MonteCarloRow = {
        cover: c,
        band,
        rays: bandCount[b]!,
        rawCameraCover: coverAt(profiles[ci]!, 0),
        meanCover,
        kappa,
        kappaDiff: kappa - meanCover,
        drawnFraction,
        targetFraction,
        diffFraction: drawnFraction - targetFraction,
        drawnOpacity,
        targetOpacity,
        diffOpacity: drawnOpacity - targetOpacity,
        seedSdFraction: Math.sqrt(Math.max(sumSeedFracSq[slot]! / cfg.seeds - seedMean * seedMean, 0)),
        samples: inBand.reduce((a, r) => a + r.drawn.n, 0) / inBand.length,
        referenceSamples: inBand.reduce((a, r) => a + r.ref.n, 0) / inBand.length,
      };
      maxAbsDiffFraction = Math.max(maxAbsDiffFraction, Math.abs(row.diffFraction));
      maxAbsDiffOpacity = Math.max(maxAbsDiffOpacity, Math.abs(row.diffOpacity));
      maxAbsKappaDiff = Math.max(maxAbsKappaDiff, Math.abs(row.kappaDiff));
      rows.push(row);
    });
  });
  return {
    config: { ...cfg },
    sampling: {
      stepCellFraction: override?.stepCellFraction ?? CLOUD_STEP_CELL_FRACTION,
      minSamples: CLOUD_MIN_SAMPLES,
      maxSamples: override?.maxSamples ?? CLOUD_MAX_SAMPLES,
      tauOpaque: CLOUD_TAU_OPAQUE,
    },
    rows,
    maxAbsDiffFraction,
    maxAbsDiffOpacity,
    maxAbsKappaDiff,
    maxAbsDiff: Math.max(maxAbsDiffFraction, maxAbsDiffOpacity),
    referenceEvaluations: refEvaluations,
    elapsedMs: performance.now() - t0,
  };
}

const pts = (v: number) => (100 * v).toFixed(2).padStart(6);

/** Plain-text table of a report, for logs and the M1.5 gate evidence. */
export function formatMonteCarloReport(rep: MonteCarloReport): string {
  const c = rep.config;
  const lines = [
    `coverage Monte Carlo: variant=${c.variant} profile=${c.profile} seeds=${c.seeds} layer=${c.layerKind} ` +
      `base=${c.layerBaseAboveCameraM >= 0 ? '+' : ''}${c.layerBaseAboveCameraM} m thickness=${c.layerThicknessM} m ground=${c.groundM ?? 'none'} ` +
      `sampling: step=cell/${(1 / rep.sampling.stepCellFraction).toFixed(0)} n=${rep.sampling.minSamples}-${rep.sampling.maxSamples} ` +
      `tau_opaque=${rep.sampling.tauOpaque}; reference step=cell/${(1 / c.referenceStepFraction).toFixed(0)} (${rep.elapsedMs.toFixed(0)} ms)`,
    `max |drawn - target|: fraction ${pts(rep.maxAbsDiffFraction).trim()} pts, opacity ${pts(rep.maxAbsDiffOpacity).trim()} pts; ` +
      `max |kappa - mean cover| ${pts(rep.maxAbsKappaDiff).trim()} pts`,
    'cover  band(deg)    raw  meanCov  kappa | drawnF targetF  diffF(pts) | drawnOp targetOp diffOp(pts) | sdF    n   nRef',
  ];
  for (const r of rep.rows) {
    lines.push(
      `${r.cover.toFixed(1).padStart(5)}  ${`[${r.band[0]},${r.band[1]})`.padEnd(10)} ${r.rawCameraCover.toFixed(3)}  ${r.meanCover.toFixed(3)}  ${r.kappa.toFixed(3)} | ` +
        `${r.drawnFraction.toFixed(3)}  ${r.targetFraction.toFixed(3)}   ${pts(r.diffFraction)}   | ${r.drawnOpacity.toFixed(3)}   ${r.targetOpacity.toFixed(3)}     ${pts(r.diffOpacity)}   | ` +
        `${r.seedSdFraction.toFixed(3)} ${r.samples.toFixed(0).padStart(4)} ${r.referenceSamples.toFixed(0).padStart(5)}`,
    );
  }
  return lines.join('\n');
}
