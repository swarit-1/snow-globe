/**
 * Light constants and colour helpers shared by the light model (PRD §9.2 passes 1, 2, 4 and 8).
 *
 * Renderer 0.3.0 replaced the v0 placeholder sky (a zenith-luminance table, day/twilight/night colour
 * weights and a Kasten-Young air mass) with the Hillaire 2020 atmosphere: sky radiance, direct sunlight
 * and moonlight, and the sky irradiance on the plate now all come from the atmosphere's lookup tables
 * (atmosphere/model.ts, cpu/atmosphere.ts, webgl/atmosphere-glsl.ts), so the sky colour and the light
 * on the plate agree. What stays here are the constants of the parts that are not the atmosphere:
 * the Sun and Moon above it, the night sky, the camera's exposure floor and the fog's albedo.
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

export function smoothstep(e0: number, e1: number, x: number): number {
  const t = Math.min(Math.max((x - e0) / (e1 - e0), 0), 1);
  return t * t * (3 - 2 * t);
}

/** Extraterrestrial solar illuminance (lux), white in linear sRGB (each channel 128,000 lux, Y = 128,000). */
export const SOLAR_E0_LUX = 128_000;

/** Full-Moon illuminance at the top of the atmosphere (lux). */
export const MOON_FULL_LUX = 0.27;

/** Moon illuminance above the atmosphere for an illuminated fraction k. Phase law v0: k^3 (TODO(M2) Krisciunas & Schaefer 1991 via oculus-core). */
export function moonTopOfAtmosphereLux(illuminatedFraction: number): number {
  const k = Math.min(Math.max(illuminatedFraction, 0), 1);
  return MOON_FULL_LUX * k ** 3;
}

/**
 * Moonlit-sky luminance per lux of horizontal moonlight (v0; about 19 mag/arcsec^2 for a full Moon at
 * 45 degrees). The moonlit sky is uniform over the dome. TODO(M2): Krisciunas & Schaefer 1991 from
 * oculus-core (PRD §9.2 pass 2).
 */
export const MOON_SKY_FACTOR = 0.02;

/** Colour of the night sky and the v0 moonlit sky (unit luminance). */
export const NIGHT_SKY_COLOR: RGB = normalizeY([0.55, 0.65, 1.0]);

/**
 * Natural night-sky luminance (airglow and starlight, cd/m^2): 10^-3.75, about 21.9 mag/arcsec^2, the
 * v0 table's value at a Sun altitude of -90 degrees. Added to sky pixels and to the sky irradiance at
 * every Sun altitude; the Sun-lit atmosphere outshines it from a few degrees of solar depression up.
 */
export const NIGHT_SKY_LUMINANCE = 10 ** -3.75;

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
