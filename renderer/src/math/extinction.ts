/**
 * Koschmieder / Beer-Lambert extinction (PRD §9.3):
 *
 *   beta = -ln(eps) / V        T(d) = exp(-beta d)        L = Ls * T(d) + Lsky * (1 - T(d))
 *
 * With eps = 0.02 (the default contrast threshold), -ln(eps) = 3.912, Koschmieder's constant.
 */
import { DEFAULT_VISIBILITY_EPSILON } from './constants.ts';

/** -ln(0.02) = 3.9120230054... */
export const KOSCHMIEDER_CONSTANT = -Math.log(DEFAULT_VISIBILITY_EPSILON);

/** Extinction coefficient (per metre) for a visibility V (metres) and contrast threshold eps. */
export function betaFromVisibility(visibilityM: number, eps = DEFAULT_VISIBILITY_EPSILON): number {
  if (!(visibilityM > 0)) throw new Error('betaFromVisibility: visibility must be > 0');
  if (!(eps > 0 && eps < 1)) throw new Error('betaFromVisibility: eps must be in (0, 1)');
  return -Math.log(eps) / visibilityM;
}

/** Visibility (m) implied by an extinction coefficient (per metre). */
export function visibilityFromBeta(betaPerM: number, eps = DEFAULT_VISIBILITY_EPSILON): number {
  return -Math.log(eps) / betaPerM;
}

/** Beer-Lambert transmittance over a path of length d (m) with uniform extinction beta (per m). */
export function transmittance(betaPerM: number, distanceM: number): number {
  return Math.exp(-betaPerM * distanceM);
}

/** Koschmieder composite: scene radiance seen through a medium whose airlight radiance is lSky. */
export function koschmieder(lScene: number, lSky: number, t: number): number {
  return lScene * t + lSky * (1 - t);
}

/**
 * The part of the forecast-visibility extinction that is fog rather than clear air. Visibility already
 * includes the clear air's extinction, so only the excess over it is applied to the near-surface layer
 * (v0 rule; documented in README). Since renderer 0.3.0 the clear air is the whole atmosphere at the
 * ground (molecules, background aerosol and the scene's haze, at 550 nm), not the haze alone.
 */
export function fogExcessBeta(visibilityM: number | null, clearAirBetaPerM: number, eps = DEFAULT_VISIBILITY_EPSILON): number {
  if (visibilityM === null) return 0;
  return Math.max(0, betaFromVisibility(visibilityM, eps) - clearAirBetaPerM);
}
