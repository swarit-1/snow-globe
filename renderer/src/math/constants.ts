/**
 * Shared physical and geometric constants for the renderer (PRD §9.2, §9.3).
 *
 * Every constant the GLSL needs is injected from here into the shader source, so the GPU path
 * and the CPU reference cannot drift apart.
 */

/** Mean Earth radius in metres. Heights everywhere are metres above mean sea level (MSL). */
export const EARTH_RADIUS_M = 6_371_000;

/** Degrees to radians. */
export const DEG = Math.PI / 180;

/** Default Koschmieder contrast threshold: beta = -ln(eps) / V, so 3.912 / V when eps = 0.02 (PRD §9.3). */
export const DEFAULT_VISIBILITY_EPSILON = 0.02;

/** Near-surface fog depth: forecast visibility applies only to ray segments below this height above ground (PRD §9.3). */
export const NEAR_SURFACE_FOG_DEPTH_M = 100;

/** Scale of the soft fog top: above the fog top, beta decays as exp(-(h - top) / H) in piecewise-constant bands. */
export const FOG_DECAY_SCALE_M = 25;

/** Number of decay bands above the fog top (band k has beta * exp(-(k + 0.5))). */
export const FOG_DECAY_BANDS = 4;

/** Default camera height above the ground under it, used for the v0 flat-ground model when the scene does not say. */
export const DEFAULT_CAMERA_AGL_M = 10;

/** v0 clear-air haze is uniform below this height above the camera's ground (a well-mixed boundary layer). */
export const HAZE_LAYER_DEPTH_M = 2000;

/** Distance assumed for terrain pixels when the plate has no depth map (v0 fallback; TODO(M2) DEM depth). */
export const DEFAULT_NOMINAL_DEPTH_M = 15_000;

/** Thickness of the solid ceiling slab added when the forecast ceiling is not inside a declared layer (v0 rule). */
export const CEILING_SLAB_THICKNESS_M = 300;

/** Ray parameter used for "no end" (sky rays that never meet the sea surface). */
export const T_INFINITE_M = 1.0e9;

/**
 * Cloud-mask sampling along each view ray's path through a slab (threshold per sample, then average;
 * PRD §9.2 pass 3, §9.5). The step is a fixed fraction of the layer's octave-0 noise cell, so the
 * sample count follows the path length instead of being a constant: a 4-sample constant (renderer
 * 0.1.0) under-drew broken decks near the horizon by up to 23 points. 1/16 of the cell was chosen from
 * a convergence study (2026-11-19, 150 seeds x 24 azimuths, low/mid/high layers, cover 0.1-0.6,
 * elevations 0.5-45 deg): it lands within ~1.3 points of a 1/128-cell reference everywhere, and halving
 * it again moves no band by more than ~1.5 points.
 */
export const CLOUD_STEP_CELL_FRACTION = 1 / 16;

/** Fewest cloud-mask samples per slab crossing (short, steep paths). */
export const CLOUD_MIN_SAMPLES = 4;

/**
 * Most cloud-mask samples per slab crossing (the shader's loop bound). Only paths longer than 128 steps
 * reach it (19.2 km for low cloud: a 600 m deck seen below ~1.8 deg elevation), where the drawn fraction
 * is close to saturation; the Monte Carlo gate measures what the cap costs there.
 */
export const CLOUD_MAX_SAMPLES = 128;

/**
 * Optical depth at which the colour path stops sampling a slab: its opacity is then >= 1 - e^-7 =
 * 0.99909 whatever the rest of the path holds, so stopping changes the pixel by < 0.1 %. The coverage and
 * target debug outputs always sample the whole path.
 */
export const CLOUD_TAU_OPAQUE = 7;

/**
 * The colour path stops sampling farther slabs once the nearer slabs' combined transmittance along the
 * view ray is below this: what they could add is < 0.1 % of the pixel (0.26 of an 8-bit level). The
 * coverage, target and alpha debug outputs, the CPU reference and the Monte Carlo sample every slab.
 */
export const CLOUD_SKIP_TRANSMITTANCE = 1e-3;

/** Number of cloud-mask samples for an in-slab path of length pathM with a sample step of stepM. */
export function cloudSampleCount(pathM: number, stepM: number): number {
  return Math.min(CLOUD_MAX_SAMPLES, Math.max(CLOUD_MIN_SAMPLES, Math.ceil(pathM / stepM)));
}

/** Maximum slabs the shader handles: low, mid, high, plus the ceiling slab. */
export const MAX_LAYERS = 4;

/** Maximum knots in one cover profile. */
export const MAX_PROFILE_KNOTS = 16;

/** Longest lead time a scene may have (hours after the run's initialisation; GFS runs reach 384 h). */
export const MAX_LEAD_H = 384;

/** Maximum knots in a wind series (hourly from 0 to 384 h is 385). */
export const MAX_WIND_KNOTS = 400;

/**
 * Tolerance (m/s) between the scene's valid-time wind and its wind series read at the valid time. Looser
 * than any producer's rounding, tight enough to catch a series from another run or a misaligned lead.
 */
export const WIND_SERIES_TOLERANCE_MS = 0.05;
