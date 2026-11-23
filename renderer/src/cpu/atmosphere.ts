/**
 * CPU reference of the Hillaire 2020 atmosphere (PRD §9.2 pass 1, D-013), in float64.
 *
 * It computes, step for step, what the WebGL passes compute in float32 (webgl/atmosphere-glsl.ts):
 *
 * 1. transmittance table (256 x 64): optical depth from (height, zenith cosine) to the top of the
 *    atmosphere, cached per atmosphere (the haze changes it);
 * 2. multiple-scattering table (32 x 32): Hillaire's Psi_ms = L_2nd / (1 - f_ms) from 8 x 8 directions
 *    of isotropic single scattering plus a Lambertian ground bounce, per (height, Sun zenith cosine);
 * 3. sky-view table (192 x 108) for the camera's height and the Sun: in-scattered radiance along every
 *    direction, single scattering with the Rayleigh and Mie phases plus Psi_ms; a second table holds
 *    the part scattered below the highest cloud slab that shades the camera (shadeTopM), which the
 *    renderer dims by the expected cloud dimming;
 * 4. aerial perspective (32 x 32 screen grid x 32 logarithmic depth slices): in-scattered radiance and
 *    transmittance from the camera to each slice, already dimmed below shadeTopM;
 * 5. the light model (lightFromAtmosphere): direct sunlight and moonlight through the transmittance
 *    table, the clear-sky irradiance integrated from the sky-view table, and the v0 cloud factors.
 *
 * Every integral uses the energy-conserving step of Hillaire (2015, 2020): over a cell of constant
 * extinction sigma_t, the in-scattered radiance is S (1 - exp(-sigma_t dt)) / sigma_t. The in-scatter
 * integrals cut each ray into pieces at its physical features (its lowest point, where it crosses the
 * haze top, and where it crosses the Earth's shadow) and crowd each piece's cells towards both its ends
 * (integrationEdges, inScatterEdges): at sunset most of the light comes from the thin sunlit top of the
 * haze layer, in twilight from the sunlit air tens of kilometres up and hundreds of kilometres away, and
 * in hazy air from the first kilometres from the camera. Cells crowded at the camera alone missed by up
 * to 12.7 % at sunset, and cells crowded at each piece's lower end alone by up to 13 % in haze of 0.3 per
 * km (measured 2026-11-23; README "Convergence"). Cells are also split where a ray crosses shadeTopM.
 * Table lookups are bilinear with clamped edges, exactly as the shader's manual bilinear filtering of its
 * RGBA32F textures (texelFetch, no hardware filtering).
 *
 * Tables are row-major RGBA with row 0 the bottom texel row (j = gl_FragCoord.y - 0.5): the GPU's
 * readPixels order, so the GPU-versus-CPU tests compare arrays index for index.
 */
import { CAMERA_MIN_ADAPT_LUX, FOG_ALBEDO, MOON_RADIUS_RAD, MOON_SKY_FACTOR, NIGHT_SKY_COLOR, NIGHT_SKY_LUMINANCE, SUN_RADIUS_RAD, luminanceY, type RGB } from '../light.ts';
import {
  AP_CELLS_PER_SLICE,
  AP_GRID,
  AP_SLICES,
  ATMOSPHERE_MIN_HEIGHT_M,
  IRRADIANCE_AZIMUTH_CELLS,
  IRRADIANCE_ZENITH_CELLS,
  ISOTROPIC_PHASE,
  MULTI_SCATTERING_CELLS,
  MULTI_SCATTERING_LUT_SIZE,
  MULTI_SCATTERING_SQRT_DIRECTIONS,
  SKY_VIEW_CELLS,
  SKY_VIEW_LUT_HEIGHT,
  SKY_VIEW_LUT_WIDTH,
  TRANSMITTANCE_CELLS,
  TRANSMITTANCE_LUT_HEIGHT,
  TRANSMITTANCE_LUT_WIDTH,
  apDepthAt,
  apSliceCoord,
  atmosphereKey,
  cornetteShanksPhase,
  diskVisibility,
  distanceToTop,
  groundDip,
  multiScatteringHMuSToUv,
  multiScatteringUvToHMuS,
  rayleighPhase,
  skyViewDirectionToUv,
  skyViewUvToDirection,
  transmittanceHMuToUv,
  transmittanceUvToHMu,
  type AtmosphereParams,
  type Rgb,
} from '../atmosphere/model.ts';
import { pointHeight, sphereInside } from '../math/shell.ts';
import type { Vec3 } from '../math/vec3.ts';
import type { CameraBasis } from '../math/camera.ts';

/** An RGBA float table, row-major, row 0 = the bottom texel row. */
export interface Lut {
  width: number;
  height: number;
  data: Float64Array;
}

function newLut(width: number, height: number): Lut {
  return { width, height, data: new Float64Array(width * height * 4) };
}

/**
 * Bilinear lookup with clamped edges at texture coordinate (u, v) in [0, 1] (texel centres at
 * (i + 0.5) / width): the shader's lutSample() in float64. Writes channels 0-2 into out.
 */
export function sampleLut(lut: Lut, u: number, v: number, out: Rgb = [0, 0, 0], channelOffset = 0): Rgb {
  const W = lut.width;
  const H = lut.height;
  const x = Math.min(Math.max(u * W - 0.5, 0), W - 1);
  const y = Math.min(Math.max(v * H - 0.5, 0), H - 1);
  const x0 = Math.min(Math.floor(x), Math.max(W - 2, 0));
  const y0 = Math.min(Math.floor(y), Math.max(H - 2, 0));
  const fx = W > 1 ? x - x0 : 0;
  const fy = H > 1 ? y - y0 : 0;
  const x1 = Math.min(x0 + 1, W - 1);
  const y1 = Math.min(y0 + 1, H - 1);
  const d = lut.data;
  const i00 = (y0 * W + x0) * 4 + channelOffset;
  const i10 = (y0 * W + x1) * 4 + channelOffset;
  const i01 = (y1 * W + x0) * 4 + channelOffset;
  const i11 = (y1 * W + x1) * 4 + channelOffset;
  for (let c = 0; c < 3; c++) {
    const a = d[i00 + c]! + (d[i10 + c]! - d[i00 + c]!) * fx;
    const b = d[i01 + c]! + (d[i11 + c]! - d[i01 + c]!) * fx;
    out[c] = a + (b - a) * fy;
  }
  return out;
}

/** Transmittance to the top of the atmosphere from height h along zenith cosine mu (table lookup). */
export function lookupTransmittance(p: AtmosphereParams, T: Lut, h: number, mu: number, out: Rgb = [0, 0, 0]): Rgb {
  const [u, v] = transmittanceHMuToUv(p, h, mu);
  return sampleLut(T, u, v, out);
}

/** Psi_ms at height h for Sun zenith cosine muS (table lookup). */
export function lookupMultiScattering(p: AtmosphereParams, MS: Lut, h: number, muS: number, out: Rgb = [0, 0, 0]): Rgb {
  const [u, v] = multiScatteringHMuSToUv(p, h, muS);
  return sampleLut(MS, u, v, out);
}

// ------------------------------------------------------------------------------------------ ray integration
/** A ray from height h0 with zenith cosine mu; muS = Sun zenith cosine at its origin, nu = cos(view, Sun). */
export interface AtmosphereRay {
  h0: number;
  mu: number;
  muS: number;
  nu: number;
}

export interface ScatterResult {
  /** In-scattered radiance per unit solar illuminance. */
  L: Rgb;
  /** The part of L scattered below shadeTopM. */
  Lbelow: Rgb;
  /** Transmittance over the integrated range. */
  throughput: Rgb;
  /** Hillaire's f_ms integrand (isotropic mode only): scattering reaching the origin, as if the light were 1 everywhere. */
  fms: Rgb;
}

export interface IntegrateOptions {
  tA: number;
  tB: number;
  /** Cells per piece between the ray's features (integrationEdges), or uniform cells over [tA, tB] when uniform is true. */
  cells: number;
  uniform: boolean;
  /** Isotropic phase and no multiple-scattering term (the multiple-scattering table's single scattering), with f_ms. */
  isotropic: boolean;
  /** Cells below this height also accumulate into Lbelow; -Infinity for none. */
  shadeTopM: number;
}

/** Tables an integration reads: the transmittance table always, the multiple-scattering table unless isotropic. */
export interface IntegrateTables {
  T: Lut;
  MS: Lut | null;
}

/** Below this optical depth a cell's (1 - exp(-x)) / sigma_t uses its series, which float32 needs (cancellation). */
export const SERIES_OPTICAL_DEPTH = 0.01;

/** (1 - exp(-sigma dt)) / sigma, the energy-conserving cell weight, stable for small sigma dt. */
export function cellWeight(sigma: number, dt: number): number {
  const x = sigma * dt;
  if (x < SERIES_OPTICAL_DEPTH) return dt * (1 - x * (0.5 - x * (1 / 6 - x / 24)));
  return (1 - Math.exp(-x)) / sigma;
}

/**
 * Crossings of a ray with the spheres at the given heights, within (tA, tB), sorted. Non-finite heights
 * (no shade top) are skipped.
 */
export function rayBreakpoints(h0: number, mu: number, heights: readonly number[], tA: number, tB: number): number[] {
  const out: number[] = [];
  for (const h of heights) {
    if (!Number.isFinite(h)) continue;
    const iv = sphereInside(h0, mu, h);
    if (!(iv.lo <= iv.hi)) continue;
    for (const t of [iv.lo, iv.hi]) if (t > tA && t < tB) out.push(t);
  }
  return out.sort((a, b) => a - b);
}

/**
 * Where a ray enters or leaves the Earth's shadow within (tA, tB): the points whose Sun lies exactly on
 * the ground sphere's horizon, that is where the ray crosses the cylinder of radius R around the axis
 * through the Earth's centre towards the Sun, on its night side. The ray is (h0, mu) with Sun zenith
 * cosine muS at its origin and nu = cos(view, Sun).
 */
export function shadowCrossings(R: number, h0: number, mu: number, muS: number, nu: number, tA: number, tB: number): number[] {
  const r0 = R + h0;
  const a = 1 - nu * nu;
  const b = r0 * (mu - muS * nu);
  const c = (r0 - R) * (r0 + R) - r0 * r0 * muS * muS;
  const out: number[] = [];
  const consider = (t: number): void => {
    if (t > tA && t < tB && r0 * muS + t * nu < 0) out.push(t);
  };
  if (a < 1e-9) {
    if (b !== 0) consider(-c / (2 * b));
  } else {
    const disc = b * b - a * c;
    if (disc >= 0) {
      const sq = Math.sqrt(disc);
      const q = -(b + (b >= 0 ? sq : -sq));
      consider(q / a);
      if (q !== 0) consider(c / q);
    }
  }
  return out.sort((x, y) => x - y);
}

/** Cell edges of [tA, tB] for an optical-depth integral: n cells per monotone piece, crowded towards the ray's lowest point (the transmittance table). */
export function densityEdges(h0: number, mu: number, tA: number, tB: number, n: number, R: number): number[] {
  const tP = -(R + h0) * mu;
  const knots = [tA];
  if (tP > tA && tP < tB) knots.push(tP);
  knots.push(tB);
  return piecewiseEdges(knots, tP, n);
}

/**
 * n cells per piece between consecutive knots, crowded quadratically towards each piece's lower end:
 * its far end on the way down to the perigee tP, its near end after it.
 */
function piecewiseEdges(knots: readonly number[], tP: number, n: number): number[] {
  const edges = [knots[0]!];
  for (let k = 1; k < knots.length; k++) {
    const a = knots[k - 1]!;
    const b = knots[k]!;
    if (!(b > a)) continue;
    const descending = b <= tP;
    for (let j = 1; j < n; j++) {
      const u = j / n;
      edges.push(descending ? b - (b - a) * (1 - u) * (1 - u) : a + (b - a) * u * u);
    }
    edges.push(b);
  }
  if (edges.length === 1) edges.push(knots[knots.length - 1]!);
  return edges;
}

/**
 * The in-scatter layout: n cells per piece between consecutive knots. Every piece is crowded at both
 * ends (smoothstep, u^2 (3 - 2u)) except the last piece of a rising ray, which is crowded at its start
 * only (u^2). A piece's start is where the transmittance from the origin is highest, so in dense haze
 * the light comes from there; its far end is the perigee or the ground (the densest air), or the haze
 * top (at a low Sun only the top few tens of metres of the haze layer are sunlit). The last piece of a
 * rising ray ends at the top of the atmosphere, where nothing scatters.
 */
function inScatterEdges(knots: readonly number[], tP: number, n: number): number[] {
  const edges = [knots[0]!];
  for (let k = 1; k < knots.length; k++) {
    const a = knots[k - 1]!;
    const b = knots[k]!;
    if (!(b > a)) continue;
    const startOnly = !(b <= tP) && k === knots.length - 1;
    for (let j = 1; j < n; j++) {
      const u = j / n;
      edges.push(startOnly ? a + (b - a) * u * u : a + (b - a) * u * u * (3 - 2 * u));
    }
    edges.push(b);
  }
  if (edges.length === 1) edges.push(knots[knots.length - 1]!);
  return edges;
}

/**
 * Cell edges of an in-scatter integral over [tA, tB]: uniform cells, or n cells per piece between the
 * ray's features (its perigee, the haze-top crossings and the Earth-shadow crossings), laid out by
 * inScatterEdges.
 */
export function integrationEdges(p: AtmosphereParams, ray: AtmosphereRay, tA: number, tB: number, n: number, uniform: boolean): number[] {
  const R = p.bottomRadiusM;
  if (!(tB > tA)) return [tA, tA];
  if (uniform) {
    const edges = [tA];
    for (let k = 1; k < n; k++) edges.push(tA + ((tB - tA) * k) / n);
    edges.push(tB);
    return edges;
  }
  const tP = -(R + ray.h0) * ray.mu;
  const inner: number[] = [];
  if (tP > tA && tP < tB) inner.push(tP);
  inner.push(...rayBreakpoints(ray.h0, ray.mu, [p.hazeTopM], tA, tB));
  inner.push(...shadowCrossings(R, ray.h0, ray.mu, ray.muS, ray.nu, tA, tB));
  inner.sort((x, y) => x - y);
  return inScatterEdges([tA, ...inner, tB], tP, n);
}

/**
 * Integrates in-scattered radiance (per unit solar illuminance) along a ray over [tA, tB] with
 * Hillaire's energy-conserving cells (IntegrateScatteredLuminance in the reference).
 */
export function integrateScattering(p: AtmosphereParams, tables: IntegrateTables, ray: AtmosphereRay, opts: IntegrateOptions): ScatterResult {
  const R = p.bottomRadiusM;
  const { h0, mu, muS, nu } = ray;
  const r0 = R + h0;
  const pR = rayleighPhase(nu);
  const pM = cornetteShanksPhase(p.miePhaseG, nu);
  const L: Rgb = [0, 0, 0];
  const Lb: Rgb = [0, 0, 0];
  const thr: Rgb = [1, 1, 1];
  const fms: Rgb = [0, 0, 0];
  const tsun: Rgb = [0, 0, 0];
  const ms: Rgb = [0, 0, 0];
  // Knotted layouts already cut cells at the haze top and the Earth's shadow; uniform ones split there.
  const bps = opts.uniform
    ? [...rayBreakpoints(h0, mu, [p.hazeTopM, opts.shadeTopM], opts.tA, opts.tB), ...shadowCrossings(R, h0, mu, muS, nu, opts.tA, opts.tB)].sort((a, b) => a - b)
    : rayBreakpoints(h0, mu, [opts.shadeTopM], opts.tA, opts.tB);
  const edges = integrationEdges(p, ray, opts.tA, opts.tB, opts.cells, opts.uniform);
  const cell = (ta: number, tb: number): void => {
    const dt = tb - ta;
    if (!(dt > 0)) return;
    const tm = 0.5 * (ta + tb);
    const h = pointHeight(h0, mu, tm);
    const r = R + h;
    const muSt = Math.min(Math.max((r0 * muS + tm * nu) / r, -1), 1);
    const inside = h < p.hazeTopM;
    const dR = Math.exp(-h / p.rayleighScaleHeightM);
    const dM = Math.exp(-h / p.mieScaleHeightM);
    const dO = Math.max(0, 1 - Math.abs(h - p.ozoneCentreM) / p.ozoneHalfWidthM);
    const dH = inside ? 1 : 0;
    lookupTransmittance(p, tables.T, h, muSt, tsun);
    const vis = diskVisibility(h, muSt, SUN_RADIUS_RAD, R);
    if (!opts.isotropic) lookupMultiScattering(p, tables.MS!, h, muSt, ms);
    const below = h < opts.shadeTopM;
    for (let c = 0; c < 3; c++) {
      const sR = p.rayleighScattering[c]! * dR;
      const sM = p.mieScattering[c]! * dM + p.hazeScattering[c]! * dH;
      const s = sR + sM;
      const e = sR + p.mieExtinction[c]! * dM + p.ozoneAbsorption[c]! * dO + p.hazeExtinction[c]! * dH;
      const src = opts.isotropic ? vis * tsun[c]! * s * ISOTROPIC_PHASE : vis * tsun[c]! * (sR * pR + sM * pM) + ms[c]! * s;
      const w = cellWeight(e, dt);
      const add = thr[c]! * src * w;
      L[c]! += add;
      if (below) Lb[c]! += add;
      if (opts.isotropic) fms[c]! += thr[c]! * s * w;
      thr[c]! *= Math.exp(-e * dt);
    }
  };
  let bi = 0;
  for (let k = 1; k < edges.length; k++) {
    let ta = edges[k - 1]!;
    const tb = edges[k]!;
    while (bi < bps.length && bps[bi]! < tb) {
      if (bps[bi]! > ta) {
        cell(ta, bps[bi]!);
        ta = bps[bi]!;
      }
      bi++;
    }
    cell(ta, tb);
  }
  return { L, Lbelow: Lb, throughput: thr, fms };
}

/** Distance along the ray to the ground sphere (MSL), or Infinity if the ray misses it. */
export function groundHit(h0: number, mu: number): number {
  const iv = sphereInside(h0, mu, 0);
  return iv.lo <= iv.hi && iv.lo > 0 ? iv.lo : Infinity;
}

/** Optical depth (per channel) from height h0 along zenith cosine mu to the top: the transmittance table's integrand. */
export function opticalDepthToTop(p: AtmosphereParams, h0: number, mu: number, cells = TRANSMITTANCE_CELLS): Rgb {
  const R = p.bottomRadiusM;
  const tTop = distanceToTop(p, h0, mu);
  const tau: Rgb = [0, 0, 0];
  const bps = rayBreakpoints(h0, mu, [p.hazeTopM], 0, tTop);
  const edges = densityEdges(h0, mu, 0, tTop, cells, R);
  const cell = (ta: number, tb: number): void => {
    const dt = tb - ta;
    if (!(dt > 0)) return;
    const h = pointHeight(h0, mu, 0.5 * (ta + tb));
    const dR = Math.exp(-h / p.rayleighScaleHeightM);
    const dM = Math.exp(-h / p.mieScaleHeightM);
    const dO = Math.max(0, 1 - Math.abs(h - p.ozoneCentreM) / p.ozoneHalfWidthM);
    const dH = h < p.hazeTopM ? 1 : 0;
    for (let c = 0; c < 3; c++) {
      tau[c]! += (p.rayleighScattering[c]! * dR + p.mieExtinction[c]! * dM + p.ozoneAbsorption[c]! * dO + p.hazeExtinction[c]! * dH) * dt;
    }
  };
  let bi = 0;
  for (let k = 1; k < edges.length; k++) {
    let ta = edges[k - 1]!;
    const tb = edges[k]!;
    while (bi < bps.length && bps[bi]! < tb) {
      if (bps[bi]! > ta) {
        cell(ta, bps[bi]!);
        ta = bps[bi]!;
      }
      bi++;
    }
    cell(ta, tb);
  }
  return tau;
}

// ------------------------------------------------------------------------------------------ tables
/** Pass 1a: the transmittance table. RGB = exp(-optical depth to the top); A = 1. */
export function computeTransmittanceLut(p: AtmosphereParams): Lut {
  const W = TRANSMITTANCE_LUT_WIDTH;
  const H = TRANSMITTANCE_LUT_HEIGHT;
  const lut = newLut(W, H);
  for (let j = 0; j < H; j++) {
    for (let i = 0; i < W; i++) {
      const { h, mu } = transmittanceUvToHMu(p, (i + 0.5) / W, (j + 0.5) / H);
      const tau = opticalDepthToTop(p, h, mu);
      const o = (j * W + i) * 4;
      lut.data[o] = Math.exp(-tau[0]);
      lut.data[o + 1] = Math.exp(-tau[1]);
      lut.data[o + 2] = Math.exp(-tau[2]);
      lut.data[o + 3] = 1;
    }
  }
  return lut;
}

/** One of the multiple-scattering table's 8 x 8 directions (the reference's uniform sphere grid). */
export function multiScatteringDirection(k: number): { mu: number; sinPhiCosTheta: number; sinPhiSinTheta: number } {
  const n = MULTI_SCATTERING_SQRT_DIRECTIONS;
  const i = Math.floor(k / n);
  const j = k - i * n;
  const theta = (2 * Math.PI * (i + 0.5)) / n;
  const mu = 1 - (2 * (j + 0.5)) / n;
  const sinPhi = Math.sqrt(Math.max(1 - mu * mu, 0));
  return { mu, sinPhiCosTheta: sinPhi * Math.cos(theta), sinPhiSinTheta: sinPhi * Math.sin(theta) };
}

/**
 * Pass 1b: the multiple-scattering table (Hillaire 2020 section 5.2). For each (height, Sun zenith
 * cosine): L_2nd and f_ms averaged over 64 directions of isotropic single scattering, the ray ending at
 * the top or at the ground (where a Lambertian bounce of the direct sunlight adds albedo / pi), then
 * Psi_ms = L_2nd / (1 - f_ms). RGB = Psi_ms; A = the largest f_ms over the channels (an energy check: < 1).
 */
export function computeMultiScatteringLut(p: AtmosphereParams, T: Lut): Lut {
  const n = MULTI_SCATTERING_LUT_SIZE;
  const lut = newLut(n, n);
  const nd = MULTI_SCATTERING_SQRT_DIRECTIONS * MULTI_SCATTERING_SQRT_DIRECTIONS;
  const tg: Rgb = [0, 0, 0];
  for (let j = 0; j < n; j++) {
    for (let i = 0; i < n; i++) {
      const { h, muS } = multiScatteringUvToHMuS(p, (i + 0.5) / n, (j + 0.5) / n);
      const sunY = Math.sqrt(Math.max(1 - muS * muS, 0));
      const sumL = [0, 0, 0];
      const sumF = [0, 0, 0];
      for (let k = 0; k < nd; k++) {
        const d = multiScatteringDirection(k);
        const nu = d.sinPhiSinTheta * sunY + d.mu * muS;
        const tG = groundHit(h, d.mu);
        const tTop = distanceToTop(p, h, d.mu);
        const hitsGround = Number.isFinite(tG);
        const tB = hitsGround ? tG : tTop;
        const res = integrateScattering(p, { T, MS: null }, { h0: h, mu: d.mu, muS, nu }, { tA: 0, tB, cells: MULTI_SCATTERING_CELLS, uniform: false, isotropic: true, shadeTopM: -Infinity });
        if (hitsGround) {
          const muSg = Math.min(Math.max(((p.bottomRadiusM + h) * muS + tG * nu) / p.bottomRadiusM, -1), 1);
          lookupTransmittance(p, T, 0, muSg, tg);
          const vis = diskVisibility(0, muSg, SUN_RADIUS_RAD, p.bottomRadiusM);
          for (let c = 0; c < 3; c++) res.L[c]! += res.throughput[c]! * vis * tg[c]! * Math.max(muSg, 0) * (p.groundAlbedo[c]! / Math.PI);
        }
        for (let c = 0; c < 3; c++) {
          sumL[c]! += res.L[c]!;
          sumF[c]! += res.fms[c]!;
        }
      }
      const o = (j * n + i) * 4;
      let fmax = 0;
      for (let c = 0; c < 3; c++) {
        const L2 = sumL[c]! / nd;
        const f = sumF[c]! / nd;
        lut.data[o + c] = L2 / (1 - f);
        fmax = Math.max(fmax, f);
      }
      lut.data[o + 3] = fmax;
    }
  }
  return lut;
}

/** The ray parameters of a sky-view texel: the Sun in the x-z plane, the view at azimuth acos(lightViewCos) from it. */
export function skyViewRay(p: AtmosphereParams, hView: number, muS: number, u: number, v: number): AtmosphereRay {
  const { viewZenithCos, lightViewCos } = skyViewUvToDirection(p, hView, u, v);
  const sinV = Math.sqrt(Math.max(1 - viewZenithCos * viewZenithCos, 0));
  const sinS = Math.sqrt(Math.max(1 - muS * muS, 0));
  return { h0: hView, mu: viewZenithCos, muS, nu: sinV * lightViewCos * sinS + viewZenithCos * muS };
}

/** Where a ray from the camera stops in the atmosphere: the ground sphere or the top. */
export function rayEndInAtmosphere(p: AtmosphereParams, h0: number, mu: number): number {
  const tG = groundHit(h0, mu);
  return Number.isFinite(tG) ? tG : distanceToTop(p, h0, mu);
}

/**
 * Pass 1c: the sky-view tables for a camera at hView (m above MSL, already clamped into the atmosphere)
 * and a Sun at zenith cosine muS. total: in-scattered radiance per unit solar illuminance; below: the part
 * scattered below shadeTopM.
 */
export function computeSkyViewLuts(p: AtmosphereParams, T: Lut, MS: Lut, hView: number, muS: number, shadeTopM: number): { total: Lut; below: Lut } {
  const W = SKY_VIEW_LUT_WIDTH;
  const H = SKY_VIEW_LUT_HEIGHT;
  const total = newLut(W, H);
  const below = newLut(W, H);
  for (let j = 0; j < H; j++) {
    for (let i = 0; i < W; i++) {
      const ray = skyViewRay(p, hView, muS, (i + 0.5) / W, (j + 0.5) / H);
      const tB = rayEndInAtmosphere(p, hView, ray.mu);
      const res = integrateScattering(p, { T, MS }, ray, { tA: 0, tB, cells: SKY_VIEW_CELLS, uniform: false, isotropic: false, shadeTopM });
      const o = (j * W + i) * 4;
      for (let c = 0; c < 3; c++) {
        total.data[o + c] = res.L[c]!;
        below.data[o + c] = res.Lbelow[c]!;
      }
      total.data[o + 3] = 1;
      below.data[o + 3] = 1;
    }
  }
  return { total, below };
}

/** Cosine of the azimuth between a view direction and the Sun (1 when either is vertical): the sky-view table's u. */
export function lightViewCos(dir: Vec3, sun: Vec3): number {
  const lv = Math.hypot(dir[0], dir[1]);
  const ls = Math.hypot(sun[0], sun[1]);
  if (lv < 1e-6 || ls < 1e-6) return 1;
  return Math.min(Math.max((dir[0] * sun[0] + dir[1] * sun[1]) / (lv * ls), -1), 1);
}

/** Sky-view lookup for a camera direction: returns [total, below] radiance per unit solar illuminance. */
export function lookupSkyView(p: AtmosphereParams, total: Lut, below: Lut, hView: number, dir: Vec3, sun: Vec3): [Rgb, Rgb] {
  const hitsGround = Number.isFinite(groundHit(hView, dir[2]));
  const [u, v] = skyViewDirectionToUv(p, hView, dir[2], lightViewCos(dir, sun), hitsGround);
  return [sampleLut(total, u, v), sampleLut(below, u, v)];
}

/** Direction of the aerial-perspective froxel column (i, j); j counts from the bottom of the frame, like gl_FragCoord. */
export function apColumnDirection(basis: CameraBasis, i: number, j: number): Vec3 {
  const nx = (2 * (i + 0.5)) / AP_GRID - 1;
  const ny = (2 * (j + 0.5)) / AP_GRID - 1;
  const sx = nx * basis.tanHalfH;
  const sy = ny * basis.tanHalfV;
  const d: Vec3 = [
    basis.forward[0] + sx * basis.right[0] + sy * basis.up[0],
    basis.forward[1] + sx * basis.right[1] + sy * basis.up[1],
    basis.forward[2] + sx * basis.right[2] + sy * basis.up[2],
  ];
  const l = Math.hypot(d[0], d[1], d[2]);
  return [d[0] / l, d[1] / l, d[2] / l];
}

/**
 * Pass 1d: the aerial-perspective volume, in two steps as the GPU does it. Increments: each froxel
 * integrates its own depth slice from transmittance 1 (AP_CELLS_PER_SLICE uniform cells), dimming what
 * is scattered below shadeTopM by cloudDim. Prefix: each froxel composes the increments of the slices up
 * to it, S_k = sum_m T(0, D_(m-1)) dS_m and T_k = prod_m T_m. Atlas layout: x = slice * 32 + i, y = j.
 */
export function computeAerialPerspective(
  p: AtmosphereParams,
  T: Lut,
  MS: Lut,
  basis: CameraBasis,
  hView: number,
  sun: Vec3,
  shadeTopM: number,
  cloudDim: number,
): { incScatter: Lut; incTransmittance: Lut; scatter: Lut; transmittance: Lut } {
  const W = AP_GRID * AP_SLICES;
  const Hh = AP_GRID;
  const incS = newLut(W, Hh);
  const incT = newLut(W, Hh);
  const S = newLut(W, Hh);
  const Tr = newLut(W, Hh);
  for (let j = 0; j < AP_GRID; j++) {
    for (let i = 0; i < AP_GRID; i++) {
      const dir = apColumnDirection(basis, i, j);
      const ray: AtmosphereRay = { h0: hView, mu: dir[2], muS: sun[2], nu: dir[0] * sun[0] + dir[1] * sun[1] + dir[2] * sun[2] };
      const tEnd = rayEndInAtmosphere(p, hView, dir[2]);
      for (let k = 0; k < AP_SLICES; k++) {
        const ta = apDepthAt(k / AP_SLICES);
        const tb = Math.min(apDepthAt((k + 1) / AP_SLICES), tEnd);
        const o = (j * W + k * AP_GRID + i) * 4;
        if (tb > ta) {
          const res = integrateScattering(p, { T, MS }, ray, { tA: ta, tB: tb, cells: AP_CELLS_PER_SLICE, uniform: true, isotropic: false, shadeTopM });
          for (let c = 0; c < 3; c++) {
            incS.data[o + c] = res.L[c]! - (1 - cloudDim) * res.Lbelow[c]!;
            incT.data[o + c] = res.throughput[c]!;
          }
        } else {
          for (let c = 0; c < 3; c++) incT.data[o + c] = 1;
        }
        incS.data[o + 3] = 1;
        incT.data[o + 3] = 1;
      }
    }
  }
  for (let j = 0; j < AP_GRID; j++) {
    for (let i = 0; i < AP_GRID; i++) {
      for (let k = 0; k < AP_SLICES; k++) {
        const acc = [0, 0, 0];
        const tr = [1, 1, 1];
        for (let m = 0; m <= k; m++) {
          const q = (j * W + m * AP_GRID + i) * 4;
          for (let c = 0; c < 3; c++) {
            acc[c]! += tr[c]! * incS.data[q + c]!;
            tr[c]! *= incT.data[q + c]!;
          }
        }
        const o = (j * W + k * AP_GRID + i) * 4;
        for (let c = 0; c < 3; c++) {
          S.data[o + c] = acc[c]!;
          Tr.data[o + c] = tr[c]!;
        }
        S.data[o + 3] = 1;
        Tr.data[o + 3] = 1;
      }
    }
  }
  return { incScatter: incS, incTransmittance: incT, scatter: S, transmittance: Tr };
}

/**
 * Aerial perspective at screen position (fx, fy) in [0, 1] (gl_FragCoord / resolution, y up) and depth t:
 * trilinear in the volume, with S = 0 and T = 1 at the camera. Returns [S, T].
 */
export function lookupAerialPerspective(S: Lut, Tr: Lut, fx: number, fy: number, t: number): [Rgb, Rgb] {
  const gx = Math.min(Math.max(fx * AP_GRID - 0.5, 0), AP_GRID - 1);
  const gy = Math.min(Math.max(fy * AP_GRID - 0.5, 0), AP_GRID - 1);
  const x0 = Math.min(Math.floor(gx), AP_GRID - 2);
  const y0 = Math.min(Math.floor(gy), AP_GRID - 2);
  const wx = gx - x0;
  const wy = gy - y0;
  const s = apSliceCoord(t) * AP_SLICES - 1;
  const outS: Rgb = [0, 0, 0];
  const outT: Rgb = [0, 0, 0];
  const at = (lut: Lut, k: number, c: number): number => {
    const W = lut.width;
    const d = lut.data;
    const a00 = d[(y0 * W + k * AP_GRID + x0) * 4 + c]!;
    const a10 = d[(y0 * W + k * AP_GRID + x0 + 1) * 4 + c]!;
    const a01 = d[((y0 + 1) * W + k * AP_GRID + x0) * 4 + c]!;
    const a11 = d[((y0 + 1) * W + k * AP_GRID + x0 + 1) * 4 + c]!;
    const a = a00 + (a10 - a00) * wx;
    const b = a01 + (a11 - a01) * wx;
    return a + (b - a) * wy;
  };
  if (s < 0) {
    const w = s + 1;
    for (let c = 0; c < 3; c++) {
      outS[c] = w * at(S, 0, c);
      outT[c] = 1 + w * (at(Tr, 0, c) - 1);
    }
    return [outS, outT];
  }
  const k0 = Math.min(Math.floor(s), AP_SLICES - 2);
  const wk = Math.min(s - k0, 1);
  for (let c = 0; c < 3; c++) {
    const s0 = at(S, k0, c);
    const t0 = at(Tr, k0, c);
    outS[c] = s0 + (at(S, k0 + 1, c) - s0) * wk;
    outT[c] = t0 + (at(Tr, k0 + 1, c) - t0) * wk;
  }
  return [outS, outT];
}

// ------------------------------------------------------------------------------------------ light
/**
 * Inputs of the light model that come from the scene and the forecast cover (frame.ts builds them): the
 * v0 cloud factors, the Moon's phase, and the slabs whose face radiance the light pass computes.
 */
export interface LightInputs {
  /** Top-of-atmosphere solar illuminance (lux). */
  solarLux: number;
  /** Expected transmission of the diffuse sky light through the cover over the camera (v0 two-stream). */
  tAmb: number;
  /** Expected direct-beam transmission towards the Sun. */
  tDirExp: number;
  /** Expected total (direct + forward-scattered) beam transmission towards the Sun. */
  tBeamTot: number;
  /** Top-of-atmosphere illuminance of the Moon at its phase (lux). */
  moonLux: number;
  /** Per slab: the height its faces are lit at (its top), its two-stream total transmission, and the camera's mix of its base (wBelow) and top (wAbove) faces. */
  layers: { topM: number; tTot: number; wBelow: number; wAbove: number }[];
  /** Blend from the horizon-sky airlight to the dense-fog airlight for the near-surface fog. */
  fogAirWeight: number;
  /** Radiance of the Sun's and the Moon's disks above the atmosphere (rgb cd/m^2). */
  sunDiskL: RGB;
  moonDiskL: RGB;
}

/** Texels of the light pass's output (a 12 x 1 RGBA32F texture); the shader reads them by these indices. */
export const LIGHT_TEXELS = {
  /** rgb: direct sunlight on a horizontal surface at the camera (lux); a: exposure. */
  eDir: 0,
  /** rgb: expected diffuse illuminance at the camera; a: cloud dimming of the clear-sky light. */
  eDif: 1,
  /** rgb: expected total illuminance at the camera; a: its luminance. */
  eCam: 2,
  /** rgb: clear-sky diffuse illuminance (Sun-lit sky + night sky + moonlight); a: luminance of the Sun-lit sky part. */
  eSkyClear: 3,
  /** rgb: clear-sky irradiance of the Sun-lit sky alone, integrated from the sky-view table. */
  eSky: 4,
  /** rgb: multiple-scattering airlight of dense near-surface fog. */
  fogAir: 5,
  /** rgb: radiance of the night sky and the moonlit sky (v0), added to sky pixels. */
  skyBackground: 6,
  /** rgb: direct moonlight at normal incidence at the camera (lux). */
  eMoonN: 7,
  /** rgb: face radiance of slab 0..3 (cd/m^2). */
  layer0: 8,
  count: 12,
} as const;

/** Clear-sky irradiance (per unit solar illuminance) on a horizontal surface: the sky-view table integrated over the upper hemisphere. */
export function skyIrradiance(p: AtmosphereParams, total: Lut, hView: number): Rgb {
  const nu = IRRADIANCE_ZENITH_CELLS;
  const np = IRRADIANCE_AZIMUTH_CELLS;
  const out: Rgb = [0, 0, 0];
  const s: Rgb = [0, 0, 0];
  for (let a = 0; a < np; a++) {
    const lvc = Math.cos((Math.PI * (a + 0.5)) / np);
    for (let b = 0; b < nu; b++) {
      const sin2 = (b + 0.5) / nu;
      const cosZ = Math.sqrt(1 - sin2);
      const [u, v] = skyViewDirectionToUv(p, hView, cosZ, lvc, false);
      sampleLut(total, u, v, s);
      for (let c = 0; c < 3; c++) out[c]! += s[c]!;
    }
  }
  const w = Math.PI / (nu * np);
  return [out[0] * w, out[1] * w, out[2] * w];
}

/** The light model's outputs (the light pass's texture, unpacked). */
export interface AtmosphereLight {
  eDir: Rgb;
  eDif: Rgb;
  eCam: Rgb;
  eCamY: number;
  exposure: number;
  cloudDim: number;
  eSkyClear: Rgb;
  eSky: Rgb;
  fogAir: Rgb;
  skyBackground: Rgb;
  eMoonN: Rgb;
  layerRadiance: Rgb[];
}

/**
 * The light model (PRD §9.2 passes 2, 4 and 8, wired to the atmosphere): direct sunlight through the
 * transmittance table times the Earth's-shadow disk fraction; the clear-sky irradiance from the
 * sky-view table, plus the v0 night and moonlit skies and moonlight; then the v0 cloud factors, the
 * exposure (a white diffuser under the expected illuminance maps to 1, floor 5 lux), the cloud dimming,
 * the dense-fog airlight and each slab's face radiance (sunlight at the slab's top above its own
 * horizon, 70 % cosine-weighted and 30 % isotropic, plus the clear-sky light).
 */
export function lightFromAtmosphere(p: AtmosphereParams, T: Lut, skyTotal: Lut, hView: number, sun: Vec3, moon: Vec3, inputs: LightInputs): AtmosphereLight {
  const E0 = inputs.solarLux;
  const muS = sun[2];
  const tS = lookupTransmittance(p, T, hView, muS);
  const vS = diskVisibility(hView, muS, SUN_RADIUS_RAD, p.bottomRadiusM);
  const eDir: Rgb = [0, 0, 0];
  for (let c = 0; c < 3; c++) eDir[c] = E0 * tS[c]! * vS * Math.max(muS, 0);
  const sky = skyIrradiance(p, skyTotal, hView);
  const eSky: Rgb = [E0 * sky[0], E0 * sky[1], E0 * sky[2]];
  const muM = moon[2];
  const tM = lookupTransmittance(p, T, hView, muM);
  const vM = diskVisibility(hView, muM, MOON_RADIUS_RAD, p.bottomRadiusM);
  const eMoonN: Rgb = [0, 0, 0];
  for (let c = 0; c < 3; c++) eMoonN[c] = inputs.moonLux * tM[c]! * vM;
  const eMoonH: Rgb = [eMoonN[0] * Math.max(muM, 0), eMoonN[1] * Math.max(muM, 0), eMoonN[2] * Math.max(muM, 0)];
  const moonSkyL = MOON_SKY_FACTOR * luminanceY(eMoonH);
  const skyBackground: Rgb = [0, 0, 0];
  for (let c = 0; c < 3; c++) skyBackground[c] = NIGHT_SKY_COLOR[c]! * (NIGHT_SKY_LUMINANCE + moonSkyL);
  const eSkyClear: Rgb = [0, 0, 0];
  for (let c = 0; c < 3; c++) eSkyClear[c] = eSky[c]! + Math.PI * skyBackground[c]! + eMoonH[c]!;
  const beamScatter = Math.max(inputs.tBeamTot - inputs.tDirExp, 0);
  const eDif: Rgb = [0, 0, 0];
  const eCam: Rgb = [0, 0, 0];
  for (let c = 0; c < 3; c++) {
    eDif[c] = eSkyClear[c]! * inputs.tAmb + eDir[c]! * beamScatter;
    eCam[c] = eDir[c]! * inputs.tDirExp + eDif[c]!;
  }
  const eCamY = luminanceY(eCam);
  const eClearY = luminanceY([eDir[0] + eSkyClear[0], eDir[1] + eSkyClear[1], eDir[2] + eSkyClear[2]]);
  const cloudDim = eClearY > 0 ? Math.min(1, eCamY / eClearY) : 1;
  const fogAir: Rgb = [eCam[0] * (FOG_ALBEDO / Math.PI), eCam[1] * (FOG_ALBEDO / Math.PI), eCam[2] * (FOG_ALBEDO / Math.PI)];
  const altS = Math.asin(Math.min(Math.max(muS, -1), 1));
  const layerRadiance = inputs.layers.map((l) => {
    const hTop = Math.min(Math.max(l.topM, ATMOSPHERE_MIN_HEIGHT_M), p.topRadiusM - p.bottomRadiusM);
    const tl = lookupTransmittance(p, T, hTop, muS);
    const vl = diskVisibility(hTop, muS, SUN_RADIUS_RAD, p.bottomRadiusM);
    const altL = altS + groundDip(hTop, p.bottomRadiusM);
    const shape = 0.7 * Math.max(Math.sin(altL), 0) + 0.3 * vl;
    const out: Rgb = [0, 0, 0];
    for (let c = 0; c < 3; c++) {
      const eAbove = E0 * tl[c]! * vl * shape + eSkyClear[c]!;
      out[c] = (eAbove / Math.PI) * (l.tTot * l.wBelow + (1 - l.tTot) * l.wAbove);
    }
    return out;
  });
  return {
    eDir,
    eDif,
    eCam,
    eCamY,
    exposure: Math.PI / Math.max(eCamY, CAMERA_MIN_ADAPT_LUX),
    cloudDim,
    eSkyClear,
    eSky,
    fogAir,
    skyBackground,
    eMoonN,
    layerRadiance,
  };
}

/** Packs the light into the light pass's texel layout (LIGHT_TEXELS), as the GPU writes it. */
export function packLight(light: AtmosphereLight, sunSkyY: number): Lut {
  const lut = newLut(LIGHT_TEXELS.count, 1);
  const put = (k: number, rgb: Rgb, a: number) => {
    lut.data.set([rgb[0], rgb[1], rgb[2], a], k * 4);
  };
  put(LIGHT_TEXELS.eDir, light.eDir, light.exposure);
  put(LIGHT_TEXELS.eDif, light.eDif, light.cloudDim);
  put(LIGHT_TEXELS.eCam, light.eCam, light.eCamY);
  put(LIGHT_TEXELS.eSkyClear, light.eSkyClear, sunSkyY);
  put(LIGHT_TEXELS.eSky, light.eSky, 0);
  put(LIGHT_TEXELS.fogAir, light.fogAir, 0);
  put(LIGHT_TEXELS.skyBackground, light.skyBackground, 0);
  put(LIGHT_TEXELS.eMoonN, light.eMoonN, 0);
  light.layerRadiance.forEach((r, i) => put(LIGHT_TEXELS.layer0 + i, r, 0));
  return lut;
}

// ------------------------------------------------------------------------------------------ whole frame
/** The two tables that depend only on the atmosphere (cached per atmosphereKey). */
export interface AtmosphereBaseLuts {
  key: string;
  transmittance: Lut;
  multiScattering: Lut;
}

export function computeBaseLuts(p: AtmosphereParams): AtmosphereBaseLuts {
  const transmittance = computeTransmittanceLut(p);
  return { key: atmosphereKey(p), transmittance, multiScattering: computeMultiScatteringLut(p, transmittance) };
}

/** Everything the atmosphere passes compute for one frame. */
export interface FrameAtmosphereLuts extends AtmosphereBaseLuts {
  skyView: Lut;
  skyViewBelow: Lut;
  light: AtmosphereLight;
  lightLut: Lut;
  apIncScatter: Lut;
  apIncTransmittance: Lut;
  apScatter: Lut;
  apTransmittance: Lut;
}

/** What the atmosphere passes need from a frame (FrameParams satisfies it). */
export interface AtmosphereFrameInputs {
  basis: CameraBasis;
  sunDir: Vec3;
  moonDir: Vec3;
  atmosphere: { params: AtmosphereParams; viewHeightM: number; shadeTopM: number };
  light: LightInputs;
}

/** Runs every atmosphere pass of one frame on the CPU, reusing base (the cached tables) when its key matches. */
export function computeFrameAtmosphere(frame: AtmosphereFrameInputs, base?: AtmosphereBaseLuts): FrameAtmosphereLuts {
  const p = frame.atmosphere.params;
  const b = base && base.key === atmosphereKey(p) ? base : computeBaseLuts(p);
  const hView = frame.atmosphere.viewHeightM;
  const shade = frame.atmosphere.shadeTopM;
  const sky = computeSkyViewLuts(p, b.transmittance, b.multiScattering, hView, frame.sunDir[2], shade);
  const light = lightFromAtmosphere(p, b.transmittance, sky.total, hView, frame.sunDir, frame.moonDir, frame.light);
  const ap = computeAerialPerspective(p, b.transmittance, b.multiScattering, frame.basis, hView, frame.sunDir, shade, light.cloudDim);
  return {
    ...b,
    skyView: sky.total,
    skyViewBelow: sky.below,
    light,
    lightLut: packLight(light, luminanceY(light.eSky)),
    apIncScatter: ap.incScatter,
    apIncTransmittance: ap.incTransmittance,
    apScatter: ap.scatter,
    apTransmittance: ap.transmittance,
  };
}
