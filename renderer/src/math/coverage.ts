/**
 * The threshold-before-filter coverage rule (PRD §9.2 pass 3: "Noise is thresholded before filtering so
 * coverage doesn't drift with distance").
 *
 * Each sample of the N(0, 1) cloud noise is compared with the normal quantile of the forecast cover read
 * at that sample's own position, giving a binary cloud mask with E[mask] = cover. Only then are samples
 * averaged (filtered). Averaging is linear, so the filtered coverage keeps the right expectation at every
 * distance. Filtering first (averaging the noise, then thresholding) shrinks the noise variance wherever
 * the samples spread out (far away, low elevations), which pushes coverage towards 0 or 1: the drift
 * this rule exists to prevent. filterThenThreshold is kept only as the negative control for tests.
 */
import { normalQuantile } from './normal.ts';

/** Noise threshold for a cover in [0, 1]: P(N(0,1) < threshold) = cover. */
export function coverThreshold(cover: number): number {
  if (cover <= 0) return -Infinity;
  if (cover >= 1) return Infinity;
  return normalQuantile(cover);
}

/** Binary cloud mask for one noise sample against the cover at that sample's position. */
export function cloudMask(noise: number, cover: number): 0 | 1 {
  return noise < coverThreshold(cover) ? 1 : 0;
}

/** The rule: threshold every sample against its own cover, then average the binary masks. */
export function thresholdThenFilter(noises: readonly number[], covers: readonly number[]): number {
  if (noises.length !== covers.length || noises.length === 0) throw new Error('thresholdThenFilter: bad input');
  let s = 0;
  for (let i = 0; i < noises.length; i++) s += cloudMask(noises[i]!, covers[i]!);
  return s / noises.length;
}

/** Negative control only: average the noise and the cover first, then threshold once. Biased. */
export function filterThenThreshold(noises: readonly number[], covers: readonly number[]): number {
  if (noises.length !== covers.length || noises.length === 0) throw new Error('filterThenThreshold: bad input');
  const n = noises.reduce((a, b) => a + b, 0) / noises.length;
  const c = covers.reduce((a, b) => a + b, 0) / covers.length;
  return cloudMask(n, c);
}

/**
 * Total cover of independent ("random overlap") layers: 1 - prod(1 - c_i). The renderer's layers have
 * independent noise fields, so this is what it shows; PRD §9.2 asks for the overlap rule to be checked
 * against HRRR total cloud (TODO(M1.5), PRD §9.2 pass 3: eval compares this with TCDC on dev data).
 */
export function randomOverlapCover(covers: readonly number[]): number {
  return 1 - covers.reduce((p, c) => p * (1 - c), 1);
}
