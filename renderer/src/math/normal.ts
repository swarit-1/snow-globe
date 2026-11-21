/*! @snow-globe/renderer notice: the inverse normal CDF uses the rational approximation by Peter J. Acklam. */
/**
 * Standard normal distribution helpers.
 *
 * The cloud noise has an exactly N(0, 1) marginal (see noise.ts), so the cover threshold is the normal
 * quantile of the cover: P(noise < quantile(c)) = c at every point. normalQuantile is Acklam's rational
 * approximation (relative error < 1.2e-9 in float64); the GLSL twin uses the same coefficients.
 */

export const ACKLAM_A = [
  -3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2, 1.38357751867269e2, -3.066479806614716e1,
  2.506628277459239,
] as const;
export const ACKLAM_B = [
  -5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1,
] as const;
export const ACKLAM_C = [
  -7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838, -2.549732539343734, 4.374664141464968,
  2.938163982698783,
] as const;
export const ACKLAM_D = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416] as const;
export const ACKLAM_P_LOW = 0.02425;

/** Inverse of the standard normal CDF for p in (0, 1). */
export function normalQuantile(p: number): number {
  if (!(p > 0 && p < 1)) {
    if (p === 0) return -Infinity;
    if (p === 1) return Infinity;
    throw new Error(`normalQuantile: p must be in [0, 1], got ${p}`);
  }
  const [a1, a2, a3, a4, a5, a6] = ACKLAM_A;
  const [b1, b2, b3, b4, b5] = ACKLAM_B;
  const [c1, c2, c3, c4, c5, c6] = ACKLAM_C;
  const [d1, d2, d3, d4] = ACKLAM_D;
  if (p < ACKLAM_P_LOW) {
    const q = Math.sqrt(-2 * Math.log(p));
    return (((((c1 * q + c2) * q + c3) * q + c4) * q + c5) * q + c6) / ((((d1 * q + d2) * q + d3) * q + d4) * q + 1);
  }
  if (p > 1 - ACKLAM_P_LOW) {
    const q = Math.sqrt(-2 * Math.log(1 - p));
    return -(((((c1 * q + c2) * q + c3) * q + c4) * q + c5) * q + c6) / ((((d1 * q + d2) * q + d3) * q + d4) * q + 1);
  }
  const q = p - 0.5;
  const r = q * q;
  return (
    ((((((a1 * r + a2) * r + a3) * r + a4) * r + a5) * r + a6) * q) /
    (((((b1 * r + b2) * r + b3) * r + b4) * r + b5) * r + 1)
  );
}

/**
 * Complementary error function: the Numerical Recipes erfcc Chebyshev fit, fractional error
 * < 1.2e-7 everywhere. Used by tests to check coverage against the exact normal CDF.
 */
export function erfc(x: number): number {
  const z = Math.abs(x);
  const t = 1 / (1 + 0.5 * z);
  const r =
    t *
    Math.exp(
      -z * z -
        1.26551223 +
        t *
          (1.00002368 +
            t *
              (0.37409196 +
                t * (0.09678418 + t * (-0.18628806 + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587 + t * (-0.82215223 + t * 0.17087277)))))))),
    );
  return x >= 0 ? r : 2 - r;
}

/** Standard normal CDF. */
export function normalCdf(x: number): number {
  return 0.5 * erfc(-x / Math.SQRT2);
}
