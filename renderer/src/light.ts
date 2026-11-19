/**
 * v0 illumination and clear-sky luminance model. A documented PLACEHOLDER for PRD §9.2 passes 1, 2 and
 * 4, not a physical sky: TODO(M1.5) replace with the Hillaire 2020 lookup tables (transmittance, multiple
 * scattering, sky view, aerial perspective), which PLAN.md M1.5 and D-013 place in renderer v0; TODO(M2)
 * the oculus-core moonlit sky (Krisciunas & Schaefer 1991).
 *
 * What it guarantees, and tests check:
 * - every quantity is finite and non-negative for any Sun altitude in [-90, 90] degrees (no NaNs at
 *   negative altitudes);
 * - every quantity is continuous in Sun altitude through sunset and twilight (piecewise-linear tables
 *   and smoothsteps; no branches that jump).
 *
 * Units: illuminance in lux, luminance in cd/m^2, colours in linear sRGB with luminance
 * Y = 0.2126 R + 0.7152 G + 0.0722 B.
 */
import { DEG } from './math/constants.ts';

export type RGB = readonly [number, number, number];

export function luminanceY(c: RGB): number {
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}

export function normalizeY(c: RGB): RGB {
  const y = luminanceY(c);
  return [c[0] / y, c[1] / y, c[2] / y];
}

export function scaleRGB(c: RGB, s: number): RGB {
  return [c[0] * s, c[1] * s, c[2] * s];
}

export function addRGB(a: RGB, b: RGB): RGB {
  return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}

export function mulRGB(a: RGB, b: RGB): RGB {
  return [a[0] * b[0], a[1] * b[1], a[2] * b[2]];
}

export function smoothstep(e0: number, e1: number, x: number): number {
  const t = Math.min(Math.max((x - e0) / (e1 - e0), 0), 1);
  return t * t * (3 - 2 * t);
}

/**
 * log10 of clear-sky zenith luminance (cd/m^2) against Sun altitude (degrees). Daytime values follow the
 * CIE clear-sky zenith formula's range (~0.4 kcd/m^2 at sunrise to ~5 kcd/m^2 high Sun); twilight values
 * follow typical zenith sky brightness (about 0 at -6 deg, 18-19 mag/arcsec^2 at -12 deg, 22 at -18 deg).
 */
export const ZENITH_LOG10_TABLE: readonly (readonly [number, number])[] = [
  [-90, -3.75],
  [-18, -3.7],
  [-15, -3.2],
  [-12, -2.3],
  [-9, -1.2],
  [-6, 0.0],
  [-3, 1.2],
  [0, 2.4],
  [3, 2.8],
  [6, 3.0],
  [10, 3.15],
  [20, 3.35],
  [40, 3.55],
  [60, 3.65],
  [90, 3.7],
];

export function zenithLuminance(altDeg: number): number {
  const t = ZENITH_LOG10_TABLE;
  const a = Math.min(Math.max(altDeg, -90), 90);
  for (let i = 1; i < t.length; i++) {
    const [x1, y1] = t[i]!;
    if (a <= x1) {
      const [x0, y0] = t[i - 1]!;
      return 10 ** (y0 + ((y1 - y0) * (a - x0)) / (x1 - x0));
    }
  }
  return 10 ** t[t.length - 1]![1];
}

/** Partition of unity between day, twilight and night sky colours. */
export function skyWeights(altDeg: number): { day: number; twilight: number; night: number } {
  const day = smoothstep(-4, 6, altDeg);
  const night = 1 - smoothstep(-18, -10, altDeg);
  return { day, twilight: Math.max(0, 1 - day - night), night };
}

/** Kasten-Young (1989) relative air mass, evaluated at max(altitude, 0) so it stays finite below the horizon. */
export function airMass(altDeg: number): number {
  const a = Math.max(altDeg, 0);
  return 1 / (Math.sin(a * DEG) + 0.50572 * (a + 6.07995) ** -1.6364);
}

/** Broadband optical depth of a clear atmosphere per channel (Rayleigh + background aerosol), v0 constant. */
export const ATMOSPHERE_TAU: RGB = [0.14, 0.19, 0.31];

/** Extraterrestrial solar illuminance (lux). */
export const SOLAR_E0_LUX = 128_000;

/** Fraction of the solar disk above the horizon: 0 below -0.9 deg, 1 above 0.4 deg (unrefracted altitude). */
export function sunVisibility(altDeg: number): number {
  return smoothstep(-0.9, 0.4, altDeg);
}

/** Clear-atmosphere transmittance towards a body at this altitude (air mass clamped at the horizon). */
export function atmosphereTransmittance(altDeg: number): RGB {
  const m = airMass(altDeg);
  return [Math.exp(-ATMOSPHERE_TAU[0] * m), Math.exp(-ATMOSPHERE_TAU[1] * m), Math.exp(-ATMOSPHERE_TAU[2] * m)];
}

/** Direct normal solar illuminance at the ground (rgb lux), including disk visibility. */
export function sunNormalIlluminance(altDeg: number): RGB {
  return scaleRGB(atmosphereTransmittance(altDeg), SOLAR_E0_LUX * sunVisibility(altDeg));
}

const rgbNorm = (r: number, g: number, b: number): RGB => normalizeY([r, g, b]);

/** Sky colours (unit luminance), injected into the GLSL sky as constants. */
export const SKY_COLORS = {
  dayZenith: rgbNorm(0.28, 0.5, 1.0),
  dayHorizon: rgbNorm(0.75, 0.85, 1.0),
  twilightZenith: rgbNorm(0.22, 0.3, 0.75),
  twilightToward: rgbNorm(1.0, 0.5, 0.2),
  twilightAway: rgbNorm(0.5, 0.48, 0.65),
  night: rgbNorm(0.55, 0.65, 1.0),
} as const;

/** Diffuse sky illuminance is pi * SKY_IRRADIANCE_FACTOR * zenith luminance (the horizon is brighter than the zenith). */
export const SKY_IRRADIANCE_FACTOR = 1.6;

/** Clear-sky diffuse horizontal illuminance (rgb lux), from the Sun-lit sky only. */
export function skyDiffuseIlluminance(altDeg: number): RGB {
  const w = skyWeights(altDeg);
  const c = addRGB(
    addRGB(scaleRGB(mixRGB(SKY_COLORS.dayZenith, SKY_COLORS.dayHorizon, 0.5), w.day), scaleRGB(mixRGB(SKY_COLORS.twilightZenith, SKY_COLORS.twilightAway, 0.5), w.twilight)),
    scaleRGB(SKY_COLORS.night, w.night),
  );
  return scaleRGB(c, Math.PI * SKY_IRRADIANCE_FACTOR * zenithLuminance(altDeg));
}

export function mixRGB(a: RGB, b: RGB, t: number): RGB {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}

/** Full-Moon illuminance at the top of the atmosphere (lux). */
export const MOON_FULL_LUX = 0.27;

/** Moonlit-sky zenith luminance per lux of horizontal moonlight (v0; gives ~19 mag/arcsec^2 for a full Moon at 45 deg). */
export const MOON_SKY_FACTOR = 0.02;

/** Direct normal moonlight (rgb lux). Phase law v0: illuminated_fraction^3 (TODO(M2) Krisciunas & Schaefer). */
export function moonNormalIlluminance(altDeg: number, illuminatedFraction: number): RGB {
  const k = Math.min(Math.max(illuminatedFraction, 0), 1);
  return scaleRGB(atmosphereTransmittance(altDeg), MOON_FULL_LUX * k ** 3 * smoothstep(-0.9, 0.4, altDeg));
}

/** Angular radii (rad) of the solar and lunar disks. */
export const SUN_RADIUS_RAD = 0.2666 * DEG;
export const MOON_RADIUS_RAD = 0.259 * DEG;

/** Solid angle of a small disk of angular radius r. */
export function diskSolidAngle(r: number): number {
  return 2 * Math.PI * (1 - Math.cos(r));
}

/** Darkest adapting illuminance the v0 camera can expose for (lux); below it frames go dark, as webcams do. */
export const CAMERA_MIN_ADAPT_LUX = 5;

/** Fraction of light a dense fog scatters back towards the camera (multiple-scattering airlight, v0). */
export const FOG_ALBEDO = 0.85;
