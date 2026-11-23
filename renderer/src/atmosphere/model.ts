/*! @snow-globe/renderer notice: the atmosphere (src/atmosphere, src/cpu/atmosphere.ts, src/webgl/atmosphere-glsl.ts) is a port of
 * the reference implementation of S. Hillaire, "A Scalable and Production Ready Sky and Atmosphere Rendering Technique",
 * Computer Graphics Forum 39(4), 2020 (https://github.com/sebh/UnrealEngineSkyAtmosphere), which carries this licence:
 *
 * MIT License
 *
 * Copyright (c) 2020 Epic Games, Inc.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
 * documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
 * rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
 * persons to whom the Software is furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
 * Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
 * WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
 * COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
 * OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 *
 * The transmittance lookup table's parameterisation is E. Bruneton's ("Precomputed Atmospheric Scattering", 2008, and its
 * 2017 implementation), as the reference uses it.
 */
/**
 * The Hillaire 2020 atmosphere (PRD §9.2 pass 1, D-013): the medium, its phase functions and the
 * parameterisations of the four lookup tables. Everything here is float64 TypeScript that the CPU
 * reference (cpu/atmosphere.ts) calls and that the GLSL (webgl/atmosphere-glsl.ts) mirrors formula for
 * formula, so the GPU and the reference share one definition of the model.
 *
 * The model is the reference implementation's Earth atmosphere:
 * - Rayleigh scattering (5.802, 13.558, 33.1) x 1e-6 per m at the ground (680, 550, 440 nm), scale height 8 km;
 * - background Mie aerosol, scattering 3.996e-6 and extinction 4.44e-6 per m (single-scattering albedo
 *   0.9), scale height 1.2 km,
 *   Cornette-Shanks phase with g = 0.8;
 * - ozone absorption (0.650, 1.881, 0.085) x 1e-6 per m in a tent profile peaking at 25 km, 15 km half width;
 * - top of the atmosphere 100 km above the ground.
 *
 * Differences from the reference, each chosen for this renderer's cameras and documented in README.md:
 * - The ground sphere is mean sea level on the renderer's Earth (R = 6,371 km), not 6,360 km, so heights
 *   mean what they mean everywhere else in the renderer (metres above MSL).
 * - The scene's clear-air haze (scene.haze, PRD §9.3) is an extra aerosol layer: grey extinction
 *   beta_per_km, single-scattering albedo 0.9, the Mie phase, uniform below HAZE_LAYER_DEPTH_M above the
 *   v0 flat ground. Its optical depth is exact: every ray integration splits its cells where the ray
 *   crosses the haze top, so the layer's sharp top never falls inside a cell.
 * - The Earth's shadow is the fraction of the Sun's disk above the local horizon of the ground sphere
 *   (sunDiskVisibility), a smooth step across the disk, instead of a 0/1 test of the disk's centre.
 * - Texel centres map exactly onto the ends of each lookup table's parameter range, with exact inverse
 *   mappings for lookups (the reference's lookup mapping is off by up to 1/(n + 1) of a texel).
 * - The aerial-perspective volume spans 0.25 km to 500 km on a logarithmic depth scale (the reference:
 *   32 km), because webcam sight lines and cloud slabs reach hundreds of kilometres.
 *
 * Units: metres; lookup-table radiances are per unit solar illuminance (the renderer multiplies by
 * SOLAR_E0_LUX), in linear sRGB with luminance Y = 0.2126 R + 0.7152 G + 0.0722 B.
 */
import { EARTH_RADIUS_M } from '../math/constants.ts';
import type { RGB } from '../light.ts';

/** Height of the top of the atmosphere above MSL (m). */
export const ATMOSPHERE_THICKNESS_M = 100_000;
export const RAYLEIGH_SCATTERING: RGB = [5.802e-6, 13.558e-6, 33.1e-6];
export const RAYLEIGH_SCALE_HEIGHT_M = 8000;
export const MIE_SCATTERING: RGB = [3.996e-6, 3.996e-6, 3.996e-6];
export const MIE_EXTINCTION: RGB = [4.44e-6, 4.44e-6, 4.44e-6];
export const MIE_SCALE_HEIGHT_M = 1200;
export const MIE_PHASE_G = 0.8;
export const OZONE_ABSORPTION: RGB = [0.65e-6, 1.881e-6, 0.085e-6];
export const OZONE_CENTRE_M = 25_000;
export const OZONE_HALF_WIDTH_M = 15_000;
/** Single-scattering albedo of the scene's haze layer: the background Mie aerosol's, 3.996 / 4.44 = 0.9. */
export const HAZE_SINGLE_SCATTERING_ALBEDO = 0.9;
/**
 * Ground albedo for the multiple-scattering table's ground bounce. v0 constant (bare and vegetated ground
 * with some snow); TODO(M2): per viewpoint and season (snow cover at Rainier and the Tetons).
 */
export const GROUND_ALBEDO: RGB = [0.3, 0.3, 0.3];
/**
 * Lowest height (m above MSL) at which the lookup tables are evaluated: a camera or a point below it is
 * evaluated at it (the reference's PLANET_RADIUS_OFFSET, 10 m). Cameras below sea level (the schema
 * allows -500 m) see the sky of a camera at this height: a v0 approximation.
 */
export const ATMOSPHERE_MIN_HEIGHT_M = 10;

// ------------------------------------------------------------------------------------- table sizes
export const TRANSMITTANCE_LUT_WIDTH = 256;
export const TRANSMITTANCE_LUT_HEIGHT = 64;
/** Cells per monotone piece of a transmittance ray (a ray that dips first has two pieces). */
export const TRANSMITTANCE_CELLS = 64;
export const MULTI_SCATTERING_LUT_SIZE = 32;
/** The multiple-scattering table integrates 8 x 8 directions over the sphere at each texel. */
export const MULTI_SCATTERING_SQRT_DIRECTIONS = 8;
/**
 * Cells per piece between a ray's features (cpu/atmosphere.ts integrationEdges) in the multiple-scattering
 * table. Measured 2026-11-23 against an independent converged integral (cpu/atmosphere.test.ts): Psi_ms
 * within 1.35 % at every height and Sun from -12 to 45 degrees, for hazes 0.03-1 per km over grounds at
 * 140 m and 1,640 m.
 */
export const MULTI_SCATTERING_CELLS = 20;
export const SKY_VIEW_LUT_WIDTH = 192;
export const SKY_VIEW_LUT_HEIGHT = 108;
/**
 * Cells per piece between a ray's features in the sky-view table. Measured 2026-11-23 against an
 * independent converged integral (cpu/atmosphere.test.ts; README "Convergence"), on 1,296 texels (every
 * 8th column, every 2nd row) that carry at least 1 % of the brightest one's luminance, for camera
 * heights 150-2,150 m and hazes 0.01-0.3 per km: within 1.30 % for Suns from 0 to 45 degrees and
 * within 3.23 % from -12 to 0 degrees (0.46 % and 2.37 % at the golden camera, 1,650 m and 0.03 per km).
 */
export const SKY_VIEW_CELLS = 24;
/** Aerial perspective: a 32 x 32 screen grid by 32 depth slices, stored as an atlas 32 * 32 wide and 32 high. */
export const AP_GRID = 32;
export const AP_SLICES = 32;
/**
 * Integration cells per depth slice of the aerial-perspective volume. Measured accuracy (2026-11-23,
 * cpu/atmosphere.test.ts; README "Aerial perspective accuracy"), golden camera, Sun towards the view: the
 * stored slices' in-scatter within 0.2 % for Suns of 2 degrees and up and 7.4 % from -4 to 0 degrees; the
 * lookups between slices up to 9.0 % and 36.6 % (inside the haze layer below its top, at sunset); the
 * transmittance within 0.022. In the pixels: at most 2 levels on the goldens, 6 at sunset. TODO(M2): if
 * eval needs a better sunset, 64 slices measured 13.3 % at 0 degrees on the CPU reference.
 */
export const AP_CELLS_PER_SLICE = 2;
/** Depth scale of the logarithmic slice distribution (m): slices are fine within a few times this distance. */
export const AP_NEAR_M = 250;
/** Far end of the aerial-perspective volume (m); farther points use the last slice. */
export const AP_FAR_M = 500_000;
/** Hemisphere quadrature of the sky irradiance from the sky-view table: cells in sin^2(zenith) and in azimuth. */
export const IRRADIANCE_ZENITH_CELLS = 32;
export const IRRADIANCE_AZIMUTH_CELLS = 32;

// ------------------------------------------------------------------------------------- parameters
export type Rgb = [number, number, number];

/** The atmosphere one frame renders with: the reference Earth plus the scene's haze layer. */
export interface AtmosphereParams {
  bottomRadiusM: number;
  topRadiusM: number;
  rayleighScattering: RGB;
  rayleighScaleHeightM: number;
  mieScattering: RGB;
  mieExtinction: RGB;
  mieScaleHeightM: number;
  miePhaseG: number;
  ozoneAbsorption: RGB;
  ozoneCentreM: number;
  ozoneHalfWidthM: number;
  /** Haze scattering and extinction coefficients (per m) inside the haze layer. */
  hazeScattering: RGB;
  hazeExtinction: RGB;
  /** Top of the haze layer (m above MSL); the layer fills everything below it. */
  hazeTopM: number;
  groundAlbedo: RGB;
}

/** The reference Earth atmosphere with a haze layer of extinction hazeBetaPerM (per m) below hazeTopM. */
export function atmosphereParams(hazeBetaPerM: number, hazeTopM: number): AtmosphereParams {
  const b = Math.max(hazeBetaPerM, 0);
  const s = b * HAZE_SINGLE_SCATTERING_ALBEDO;
  return {
    bottomRadiusM: EARTH_RADIUS_M,
    topRadiusM: EARTH_RADIUS_M + ATMOSPHERE_THICKNESS_M,
    rayleighScattering: RAYLEIGH_SCATTERING,
    rayleighScaleHeightM: RAYLEIGH_SCALE_HEIGHT_M,
    mieScattering: MIE_SCATTERING,
    mieExtinction: MIE_EXTINCTION,
    mieScaleHeightM: MIE_SCALE_HEIGHT_M,
    miePhaseG: MIE_PHASE_G,
    ozoneAbsorption: OZONE_ABSORPTION,
    ozoneCentreM: OZONE_CENTRE_M,
    ozoneHalfWidthM: OZONE_HALF_WIDTH_M,
    hazeScattering: [s, s, s],
    hazeExtinction: [b, b, b],
    hazeTopM,
    groundAlbedo: GROUND_ALBEDO,
  };
}

/**
 * Cache key of the parameters that the transmittance and multiple-scattering tables depend on: the
 * renderer recomputes those two tables only when it changes (in practice, when the haze changes).
 */
export function atmosphereKey(p: AtmosphereParams): string {
  return JSON.stringify([
    p.bottomRadiusM,
    p.topRadiusM,
    p.rayleighScattering,
    p.rayleighScaleHeightM,
    p.mieScattering,
    p.mieExtinction,
    p.mieScaleHeightM,
    p.miePhaseG,
    p.ozoneAbsorption,
    p.ozoneCentreM,
    p.ozoneHalfWidthM,
    p.hazeScattering,
    p.hazeExtinction,
    p.hazeTopM,
    p.groundAlbedo,
  ]);
}

/** Scattering and extinction coefficients (per m) at one point of the medium. */
export interface Medium {
  /** Rayleigh scattering. */
  scatRay: Rgb;
  /** Mie scattering: background aerosol plus haze (both use the Mie phase). */
  scatMie: Rgb;
  /** Total scattering. */
  scat: Rgb;
  /** Total extinction (scattering + Mie and haze absorption + ozone). */
  ext: Rgb;
}

/** The medium at height h (m above MSL); insideHaze says whether the point is inside the haze layer. */
export function mediumAt(p: AtmosphereParams, h: number, insideHaze: boolean): Medium {
  const dR = Math.exp(-h / p.rayleighScaleHeightM);
  const dM = Math.exp(-h / p.mieScaleHeightM);
  const dO = Math.max(0, 1 - Math.abs(h - p.ozoneCentreM) / p.ozoneHalfWidthM);
  const dH = insideHaze ? 1 : 0;
  const scatRay: Rgb = [0, 0, 0];
  const scatMie: Rgb = [0, 0, 0];
  const scat: Rgb = [0, 0, 0];
  const ext: Rgb = [0, 0, 0];
  for (let c = 0; c < 3; c++) {
    scatRay[c] = p.rayleighScattering[c]! * dR;
    scatMie[c] = p.mieScattering[c]! * dM + p.hazeScattering[c]! * dH;
    scat[c] = scatRay[c]! + scatMie[c]!;
    ext[c] = scatRay[c]! + p.mieExtinction[c]! * dM + p.ozoneAbsorption[c]! * dO + p.hazeExtinction[c]! * dH;
  }
  return { scatRay, scatMie, scat, ext };
}

/** Total clear-air extinction (per m) at height h inside the haze layer if h is below its top (used for the fog excess). */
export function clearAirExtinction(p: AtmosphereParams, h: number): Rgb {
  return mediumAt(p, h, h < p.hazeTopM).ext;
}

/** Rayleigh phase function, normalised over the sphere; nu is the cosine of the scattering angle. */
export function rayleighPhase(nu: number): number {
  return (3 / (16 * Math.PI)) * (1 + nu * nu);
}

/**
 * Cornette-Shanks phase function (normalised; g is the asymmetry). nu = dot(view direction, Sun
 * direction), so nu = 1 looks straight at the Sun: the forward-scattering peak.
 */
export function cornetteShanksPhase(g: number, nu: number): number {
  const k = ((3 / (8 * Math.PI)) * (1 - g * g)) / (2 + g * g);
  const d = 1 + g * g - 2 * g * nu;
  return (k * (1 + nu * nu)) / (d * Math.sqrt(d));
}

/** Isotropic phase function. */
export const ISOTROPIC_PHASE = 1 / (4 * Math.PI);

/** Dip of the ground sphere's horizon (rad) seen from height h (m above MSL); 0 at or below MSL. */
export function groundDip(h: number, R = EARTH_RADIUS_M): number {
  if (h <= 0) return 0;
  return Math.atan(Math.sqrt(h * (2 * R + h)) / R);
}

export function smoothstep01(e0: number, e1: number, x: number): number {
  const t = Math.min(Math.max((x - e0) / (e1 - e0), 0), 1);
  return t * t * (3 - 2 * t);
}

/**
 * Fraction of a disk of angular radius radiusRad (the Sun, or the Moon) that is above the horizon of the
 * ground sphere, seen from height h with the body at zenith cosine mu: a smooth step across the disk
 * around elevation = -dip. This is the Earth's shadow; refraction is not modelled (TODO(M2): it delays
 * sunset by about 0.57 degrees of solar depression).
 */
export function diskVisibility(h: number, mu: number, radiusRad: number, R = EARTH_RADIUS_M): number {
  const elev = Math.asin(Math.min(Math.max(mu, -1), 1)) + groundDip(h, R);
  return smoothstep01(-radiusRad, radiusRad, elev);
}

// ------------------------------------------------------------------------------------- texel mappings
/** Texel-centre coordinate in [0.5/n, 1 - 0.5/n] to [0, 1]: the first and last texel centres hit the ends. */
export function subUvToUnit(u: number, n: number): number {
  return (u - 0.5 / n) * (n / (n - 1));
}

/** Exact inverse of subUvToUnit. */
export function unitToSubUv(x: number, n: number): number {
  return x * ((n - 1) / n) + 0.5 / n;
}

const clamp01 = (x: number): number => Math.min(Math.max(x, 0), 1);

/** sqrt(top^2 - bottom^2): the distance to the top along a ray grazing the ground. */
export function horizonToTopM(p: AtmosphereParams): number {
  return Math.sqrt((p.topRadiusM - p.bottomRadiusM) * (p.topRadiusM + p.bottomRadiusM));
}

/** Distance (m) from height h along zenith cosine mu to the top of the atmosphere (ignoring the ground). */
export function distanceToTop(p: AtmosphereParams, h: number, mu: number): number {
  const r = p.bottomRadiusM + h;
  const rt = p.topRadiusM;
  const rmu = r * mu;
  // (rt - r)(rt + r), with rt - r written as (thickness - h): float32 keeps it to the metre (the GLSL twin).
  const a = (rt - p.bottomRadiusM - h) * (rt + r);
  const sq = Math.sqrt(Math.max(a + rmu * rmu, 0));
  if (mu >= 0) {
    const den = rmu + sq;
    return den > 0 ? Math.max(a / den, 0) : 0;
  }
  return Math.max(-rmu + sq, 0);
}

/**
 * Transmittance table texel (u, v in (0, 1), texel centres) to the ray it stores: height h above MSL and
 * zenith cosine mu, by Bruneton's mapping (x_r = rho / H, x_mu = (d - d_min) / (d_max - d_min)). The
 * table covers every ray from the ground to the top that does not hit the ground.
 */
export function transmittanceUvToHMu(p: AtmosphereParams, u: number, v: number): { h: number; mu: number } {
  const xMu = subUvToUnit(u, TRANSMITTANCE_LUT_WIDTH);
  const xR = subUvToUnit(v, TRANSMITTANCE_LUT_HEIGHT);
  const H = horizonToTopM(p);
  const rho = H * xR;
  const Rb = p.bottomRadiusM;
  const r = Math.sqrt(rho * rho + Rb * Rb);
  const h = (rho * rho) / (r + Rb);
  const dMin = p.topRadiusM - Rb - h;
  const dMax = rho + H;
  const d = dMin + xMu * (dMax - dMin);
  const mu = d === 0 ? 1 : ((H - rho) * (H + rho) - d * d) / (2 * r * d);
  return { h, mu: Math.min(Math.max(mu, -1), 1) };
}

/** Inverse of transmittanceUvToHMu (clamped into the table): the texture coordinate for a ray. */
export function transmittanceHMuToUv(p: AtmosphereParams, h: number, mu: number): [number, number] {
  const hc = Math.min(Math.max(h, 0), p.topRadiusM - p.bottomRadiusM);
  const H = horizonToTopM(p);
  const rho = Math.sqrt(hc * (2 * p.bottomRadiusM + hc));
  const d = distanceToTop(p, hc, mu);
  const dMin = p.topRadiusM - p.bottomRadiusM - hc;
  const dMax = rho + H;
  const xMu = dMax - dMin > 0 ? (d - dMin) / (dMax - dMin) : 0;
  const xR = rho / H;
  return [unitToSubUv(clamp01(xMu), TRANSMITTANCE_LUT_WIDTH), unitToSubUv(clamp01(xR), TRANSMITTANCE_LUT_HEIGHT)];
}

/** Multiple-scattering table texel to (height above MSL, Sun zenith cosine). */
export function multiScatteringUvToHMuS(p: AtmosphereParams, u: number, v: number): { h: number; muS: number } {
  const n = MULTI_SCATTERING_LUT_SIZE;
  const muS = 2 * subUvToUnit(u, n) - 1;
  const h = ATMOSPHERE_MIN_HEIGHT_M + subUvToUnit(v, n) * (p.topRadiusM - p.bottomRadiusM - ATMOSPHERE_MIN_HEIGHT_M);
  return { h, muS };
}

/** Inverse of multiScatteringUvToHMuS, clamped into the table. */
export function multiScatteringHMuSToUv(p: AtmosphereParams, h: number, muS: number): [number, number] {
  const n = MULTI_SCATTERING_LUT_SIZE;
  const x = clamp01(muS * 0.5 + 0.5);
  const y = clamp01((h - ATMOSPHERE_MIN_HEIGHT_M) / (p.topRadiusM - p.bottomRadiusM - ATMOSPHERE_MIN_HEIGHT_M));
  return [unitToSubUv(x, n), unitToSubUv(y, n)];
}

/**
 * Sky-view table texel to a view direction, for a camera at height hView (Hillaire 2020 section 5.3):
 * u is the azimuth from the Sun's vertical plane (cos = lightViewCos, squared mapping so texels crowd
 * towards the Sun; the table covers 0 to 180 degrees because the sky is symmetric about that plane);
 * v runs from the zenith (0) through the ground sphere's horizon (0.5) to the nadir (1), with a
 * squared mapping that crowds texels at the horizon.
 */
export function skyViewUvToDirection(p: AtmosphereParams, hView: number, u: number, v: number): { viewZenithCos: number; lightViewCos: number } {
  const x = subUvToUnit(u, SKY_VIEW_LUT_WIDTH);
  const y = subUvToUnit(v, SKY_VIEW_LUT_HEIGHT);
  const dip = groundDip(hView, p.bottomRadiusM);
  const zenithHorizonAngle = Math.PI / 2 + dip;
  const beta = Math.PI / 2 - dip;
  let viewZenithCos: number;
  if (y < 0.5) {
    let c = 1 - 2 * y;
    c = 1 - c * c;
    viewZenithCos = Math.cos(zenithHorizonAngle * c);
  } else {
    let c = 2 * y - 1;
    c = c * c;
    viewZenithCos = Math.cos(zenithHorizonAngle + beta * c);
  }
  const lightViewCos = 1 - 2 * x * x;
  return { viewZenithCos, lightViewCos };
}

/** Inverse of skyViewUvToDirection: the table coordinate of a direction (intersectsGround: the ray meets the ground sphere). */
export function skyViewDirectionToUv(p: AtmosphereParams, hView: number, viewZenithCos: number, lightViewCos: number, intersectsGround: boolean): [number, number] {
  const dip = groundDip(hView, p.bottomRadiusM);
  const zenithHorizonAngle = Math.PI / 2 + dip;
  const beta = Math.PI / 2 - dip;
  const zen = Math.acos(Math.min(Math.max(viewZenithCos, -1), 1));
  let y: number;
  if (!intersectsGround) {
    let c = 1 - zen / zenithHorizonAngle;
    c = Math.sqrt(Math.max(c, 0));
    y = (1 - c) * 0.5;
  } else {
    let c = (zen - zenithHorizonAngle) / beta;
    c = Math.sqrt(Math.max(c, 0));
    y = c * 0.5 + 0.5;
  }
  const x = Math.sqrt(clamp01(0.5 - 0.5 * lightViewCos));
  return [unitToSubUv(clamp01(x), SKY_VIEW_LUT_WIDTH), unitToSubUv(clamp01(y), SKY_VIEW_LUT_HEIGHT)];
}

const AP_LOG_SPAN = Math.log1p(AP_FAR_M / AP_NEAR_M);

/** Depth (m) at slice coordinate w in [0, 1] of the aerial-perspective volume (logarithmic). */
export function apDepthAt(w: number): number {
  return AP_NEAR_M * Math.expm1(w * AP_LOG_SPAN);
}

/** Slice coordinate w in [0, 1] of a depth (m), clamped at the far end. */
export function apSliceCoord(t: number): number {
  return Math.min(Math.log1p(Math.max(t, 0) / AP_NEAR_M) / AP_LOG_SPAN, 1);
}

/** The constant ln(1 + AP_FAR_M / AP_NEAR_M) (injected into the GLSL). */
export const AP_LOG_SPAN_CONST = AP_LOG_SPAN;
