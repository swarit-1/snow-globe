/**
 * GLSL ES 3.00 sources of the Hillaire 2020 atmosphere (PRD §9.2 pass 1, D-013): the five passes that
 * build its lookup tables each frame, and the lookups the main pass uses. Each function mirrors, formula
 * for formula, its float64 twin in atmosphere/model.ts or cpu/atmosphere.ts (named in its comment), and
 * every constant is injected from TypeScript, so the GPU and the CPU reference cannot drift apart.
 *
 * Passes (all draw a full-screen triangle into RGBA32F targets; tables are read with texelFetch and
 * manual bilinear filtering, never hardware filtering):
 *   transmittance   256 x 64    cached per atmosphere (recomputed when the haze changes)
 *   multi-scatter   32 x 32     cached per atmosphere
 *   sky view        192 x 108   per frame, two targets: total in-scatter and the part below the shade top
 *   irradiance      32 x 1      per frame: the sky-view table's irradiance quadrature, one sum per azimuth
 *   light           12 x 1      per frame: the light model (direct sun, sky irradiance, exposure, slab faces)
 *   aerial persp.   1024 x 32   per frame, two passes: per-slice increments, then their composition
 *
 * The licence notice of the ported reference implementation is kept in the shader sources as a comment
 * (ATMOSPHERE_NOTICE_GLSL) and in the bundle as a legal comment (atmosphere/model.ts).
 */
import {
  AP_CELLS_PER_SLICE,
  AP_FAR_M,
  AP_GRID,
  AP_LOG_SPAN_CONST,
  AP_NEAR_M,
  AP_SLICES,
  ATMOSPHERE_MIN_HEIGHT_M,
  ATMOSPHERE_THICKNESS_M,
  GROUND_ALBEDO,
  IRRADIANCE_AZIMUTH_CELLS,
  IRRADIANCE_ZENITH_CELLS,
  MIE_EXTINCTION,
  MIE_PHASE_G,
  MIE_SCALE_HEIGHT_M,
  MIE_SCATTERING,
  MULTI_SCATTERING_CELLS,
  MULTI_SCATTERING_LUT_SIZE,
  MULTI_SCATTERING_SQRT_DIRECTIONS,
  OZONE_ABSORPTION,
  OZONE_CENTRE_M,
  OZONE_HALF_WIDTH_M,
  RAYLEIGH_SCALE_HEIGHT_M,
  RAYLEIGH_SCATTERING,
  SKY_VIEW_CELLS,
  SKY_VIEW_LUT_HEIGHT,
  SKY_VIEW_LUT_WIDTH,
  TRANSMITTANCE_CELLS,
  TRANSMITTANCE_LUT_HEIGHT,
  TRANSMITTANCE_LUT_WIDTH,
} from '../atmosphere/model.ts';
import { LIGHT_TEXELS, SERIES_OPTICAL_DEPTH } from '../cpu/atmosphere.ts';
import { CAMERA_MIN_ADAPT_LUX, FOG_ALBEDO, MOON_RADIUS_RAD, MOON_SKY_FACTOR, NIGHT_SKY_COLOR, NIGHT_SKY_LUMINANCE, SUN_RADIUS_RAD } from '../light.ts';
import { EARTH_RADIUS_M, MAX_LAYERS } from '../math/constants.ts';
import { glslFloat as f, glslVec3 as v3 } from './glsl-format.ts';

/** The ported reference implementation's licence, as a GLSL comment in every atmosphere shader source. */
export const ATMOSPHERE_NOTICE_GLSL = `/*
 * Atmosphere: a port of the reference implementation of S. Hillaire, "A Scalable and Production Ready Sky and
 * Atmosphere Rendering Technique", Computer Graphics Forum 39(4), 2020 (https://github.com/sebh/UnrealEngineSkyAtmosphere).
 * MIT License. Copyright (c) 2020 Epic Games, Inc. Permission is hereby granted, free of charge, to any person obtaining
 * a copy of this software and associated documentation files (the "Software"), to deal in the Software without
 * restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense,
 * and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the
 * following conditions: The above copyright notice and this permission notice shall be included in all copies or
 * substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 * NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
 * SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 */
`;

/** Sentinel for "no shade top" and "no hit" in GLSL (the TypeScript side uses -Infinity and Infinity). */
export const GLSL_BIG = 1.0e30;
export const SHADE_TOP_NONE = -1.0e9;

/** Precision header shared by every fragment shader. */
export const GLSL_HEADER = `#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;
`;

/**
 * Ray-versus-shell geometry around the spherical Earth (mirrors math/shell.ts line for line): used by
 * the cloud slabs, the fog and the atmosphere.
 */
export const GLSL_GEOMETRY = `
const float R_EARTH = ${f(EARTH_RADIUS_M)};
const float PI = 3.141592653589793;

vec2 sphereInside(float h0, float mu, float h) {
  float B = (R_EARTH + h0) * mu;
  float C = (h0 - h) * (2.0 * R_EARTH + h0 + h);
  float disc = B * B - C;
  if (disc < 0.0) return vec2(1.0, -1.0);
  float sq = sqrt(disc);
  float q = -(B + (B >= 0.0 ? sq : -sq));
  if (q == 0.0) return vec2(0.0);
  float t2 = C / q;
  return vec2(min(q, t2), max(q, t2));
}

vec2 clipIv(vec2 iv, float t0, float t1) { return vec2(max(iv.x, t0), min(iv.y, t1)); }
float ivLen(vec2 iv) { return max(iv.y - iv.x, 0.0); }
float overlapLen(vec2 iv, float ta, float tb) { return max(min(iv.y, tb) - max(iv.x, ta), 0.0); }

void slabIntervals(float h0, float mu, float base, float top, float t0, float t1, out vec2 nearIv, out vec2 farIv) {
  nearIv = vec2(1.0, -1.0);
  farIv = vec2(1.0, -1.0);
  vec2 outer = sphereInside(h0, mu, top);
  if (outer.x > outer.y) return;
  vec2 inner = sphereInside(h0, mu, base);
  if (inner.x > inner.y) { nearIv = clipIv(outer, t0, t1); return; }
  nearIv = clipIv(vec2(outer.x, inner.x), t0, t1);
  farIv = clipIv(vec2(inner.y, outer.y), t0, t1);
}

// Height above MSL at distance t along a ray from height h0 with zenith cosine mu (math/shell.ts pointHeight).
float pointHeight(float h0, float mu, float t) {
  float r0 = R_EARTH + h0;
  float num = t * t + 2.0 * r0 * mu * t + h0 * h0 + 2.0 * R_EARTH * h0;
  float q = sqrt(r0 * r0 + 2.0 * r0 * mu * t + t * t);
  return num / (q + R_EARTH);
}
`;

const H_TOP = Math.sqrt(ATMOSPHERE_THICKNESS_M * (2 * EARTH_RADIUS_M + ATMOSPHERE_THICKNESS_M));

/** The atmosphere's constants, uniforms and shared functions (atmosphere/model.ts and the lookups of cpu/atmosphere.ts). */
export const GLSL_ATMOSPHERE_COMMON = `
const float ATM_TOP_H = ${f(ATMOSPHERE_THICKNESS_M)};
const float ATM_H_TOP = ${f(H_TOP)};       // sqrt(top^2 - bottom^2)
const float ATM_MIN_H = ${f(ATMOSPHERE_MIN_HEIGHT_M)};
const vec3 RAY_SCAT = ${v3(RAYLEIGH_SCATTERING)};
const float RAY_H = ${f(RAYLEIGH_SCALE_HEIGHT_M)};
const vec3 MIE_SCAT = ${v3(MIE_SCATTERING)};
const vec3 MIE_EXT = ${v3(MIE_EXTINCTION)};
const float MIE_H = ${f(MIE_SCALE_HEIGHT_M)};
const float MIE_G = ${f(MIE_PHASE_G)};
const vec3 OZONE_ABS = ${v3(OZONE_ABSORPTION)};
const float OZONE_C = ${f(OZONE_CENTRE_M)};
const float OZONE_W = ${f(OZONE_HALF_WIDTH_M)};
const vec3 GROUND_ALBEDO = ${v3(GROUND_ALBEDO)};
const float SUN_RADIUS = ${f(SUN_RADIUS_RAD)};
const float MOON_RADIUS = ${f(MOON_RADIUS_RAD)};
const float BIG = ${f(GLSL_BIG)};
const float SERIES_TAU = ${f(SERIES_OPTICAL_DEPTH)};
const float ISO_PHASE = ${f(1 / (4 * Math.PI))};
const int TW = ${TRANSMITTANCE_LUT_WIDTH};
const int TH = ${TRANSMITTANCE_LUT_HEIGHT};
const int MS_N = ${MULTI_SCATTERING_LUT_SIZE};
const int SV_W = ${SKY_VIEW_LUT_WIDTH};
const int SV_H = ${SKY_VIEW_LUT_HEIGHT};
const int AP_GRID = ${AP_GRID};
const int AP_SLICES = ${AP_SLICES};
const float AP_NEAR = ${f(AP_NEAR_M)};
const float AP_FAR = ${f(AP_FAR_M)};
const float AP_LOG_SPAN = ${f(AP_LOG_SPAN_CONST)};

uniform vec3 u_hazeScat;   // haze scattering (per m) inside the haze layer
uniform vec3 u_hazeExt;    // haze extinction (per m)
uniform float u_hazeTop;   // top of the haze layer (m above MSL)
uniform sampler2D u_transLut;

// cos, acos and asin accurate to float32 rounding on every driver. SwiftShader's built-ins are off by up to
// 1.9e-4 (cos) and 6.7e-5 (acos) absolute (measured 2026-11-23), which tilted sky-view texels near the
// zenith by 0.07 degrees; its atan is accurate (1.6e-7), so acos and asin go through atan.
float sinPoly(float x) {   // |x| <= pi / 2, Taylor series to x^13 (truncation < 7e-10)
  float x2 = x * x;
  return x * (1.0 + x2 * (-1.0 / 6.0 + x2 * (1.0 / 120.0 + x2 * (-1.0 / 5040.0 + x2 * (1.0 / 362880.0 + x2 * (-1.0 / 39916800.0 + x2 / 6227020800.0))))));
}
float cosAcc(float t) { float s = sinPoly(0.5 * clamp(t, 0.0, PI)); return 1.0 - 2.0 * s * s; }   // t in [0, pi]
float acosAcc(float x) { float c = clamp(x, -1.0, 1.0); return atan(sqrt(max((1.0 - c) * (1.0 + c), 0.0)), c); }
float asinAcc(float x) { float c = clamp(x, -1.0, 1.0); return atan(c, sqrt(max((1.0 - c) * (1.0 + c), 0.0))); }

float subUvToUnit(float u, int n) { float fn = float(n); return (u - 0.5 / fn) * (fn / (fn - 1.0)); }
float unitToSubUv(float x, int n) { float fn = float(n); return x * ((fn - 1.0) / fn) + 0.5 / fn; }

// Bilinear lookup with clamped edges (cpu/atmosphere.ts sampleLut).
vec3 lutSample(sampler2D s, ivec2 size, vec2 uv) {
  vec2 sz = vec2(size);
  vec2 xy = clamp(uv * sz - 0.5, vec2(0.0), sz - 1.0);
  vec2 x0 = min(floor(xy), max(sz - 2.0, vec2(0.0)));
  vec2 w = xy - x0;
  ivec2 i0 = ivec2(x0);
  ivec2 i1 = min(i0 + 1, size - 1);
  vec3 a00 = texelFetch(s, i0, 0).rgb;
  vec3 a10 = texelFetch(s, ivec2(i1.x, i0.y), 0).rgb;
  vec3 a01 = texelFetch(s, ivec2(i0.x, i1.y), 0).rgb;
  vec3 a11 = texelFetch(s, i1, 0).rgb;
  vec3 a = a00 + (a10 - a00) * w.x;
  vec3 b = a01 + (a11 - a01) * w.x;
  return a + (b - a) * w.y;
}

// Distance from height h along zenith cosine mu to the top of the atmosphere (model.ts distanceToTop).
float distanceToTop(float h, float mu) {
  float r = R_EARTH + h;
  float rt = R_EARTH + ATM_TOP_H;
  float rmu = r * mu;
  float a = (ATM_TOP_H - h) * (rt + r);
  float sq = sqrt(max(a + rmu * rmu, 0.0));
  if (mu >= 0.0) {
    float den = rmu + sq;
    return den > 0.0 ? max(a / den, 0.0) : 0.0;
  }
  return max(-rmu + sq, 0.0);
}

// Bruneton's transmittance-table mapping (model.ts transmittanceHMuToUv).
vec2 transmittanceHMuToUv(float h, float mu) {
  float hc = clamp(h, 0.0, ATM_TOP_H);
  float rho = sqrt(hc * (2.0 * R_EARTH + hc));
  float d = distanceToTop(hc, mu);
  float dMin = ATM_TOP_H - hc;
  float dMax = rho + ATM_H_TOP;
  float xMu = dMax - dMin > 0.0 ? (d - dMin) / (dMax - dMin) : 0.0;
  float xR = rho / ATM_H_TOP;
  return vec2(unitToSubUv(clamp(xMu, 0.0, 1.0), TW), unitToSubUv(clamp(xR, 0.0, 1.0), TH));
}

vec3 transmittanceAt(float h, float mu) { return lutSample(u_transLut, ivec2(TW, TH), transmittanceHMuToUv(h, mu)); }

// Dip of the ground sphere's horizon from height h, and the visible fraction of a disk (model.ts).
float groundDip(float h) { return h <= 0.0 ? 0.0 : atan(sqrt(h * (2.0 * R_EARTH + h)) / R_EARTH); }
float diskVisibility(float h, float mu, float radius) {
  float elev = asinAcc(mu) + groundDip(h);
  float t = clamp((elev + radius) / (2.0 * radius), 0.0, 1.0);
  return t * t * (3.0 - 2.0 * t);
}

float rayleighPhase(float nu) { return (3.0 / (16.0 * PI)) * (1.0 + nu * nu); }
float csPhase(float g, float nu) {
  float k = ((3.0 / (8.0 * PI)) * (1.0 - g * g)) / (2.0 + g * g);
  float d = 1.0 + g * g - 2.0 * g * nu;
  return (k * (1.0 + nu * nu)) / (d * sqrt(d));
}

// Distance to the ground sphere, or BIG if the ray misses it (cpu/atmosphere.ts groundHit).
float groundHit(float h0, float mu) {
  vec2 iv = sphereInside(h0, mu, 0.0);
  return (iv.x <= iv.y && iv.x > 0.0) ? iv.x : BIG;
}

float rayEndInAtmosphere(float h0, float mu) {
  float tG = groundHit(h0, mu);
  return tG < BIG ? tG : distanceToTop(h0, mu);
}

// The medium at height h (model.ts mediumAt): Rayleigh scattering, Mie (+ haze) scattering, total extinction.
void medium(float h, out vec3 sR, out vec3 sM, out vec3 e) {
  float dR = exp(-h / RAY_H);
  float dM = exp(-h / MIE_H);
  float dO = max(0.0, 1.0 - abs(h - OZONE_C) / OZONE_W);
  float dH = h < u_hazeTop ? 1.0 : 0.0;
  sR = RAY_SCAT * dR;
  sM = MIE_SCAT * dM + u_hazeScat * dH;
  e = sR + MIE_EXT * dM + OZONE_ABS * dO + u_hazeExt * dH;
}

// Energy-conserving cell weight (1 - exp(-sigma dt)) / sigma, with its series below SERIES_TAU (cellWeight).
float cellW(float sigma, float dt) {
  float x = sigma * dt;
  if (x < SERIES_TAU) return dt * (1.0 - x * (0.5 - x * (1.0 / 6.0 - x / 24.0)));
  return (1.0 - exp(-x)) / sigma;
}
vec3 cellWeight(vec3 sigma, float dt) { return vec3(cellW(sigma.r, dt), cellW(sigma.g, dt), cellW(sigma.b, dt)); }

// Where a ray crosses the haze top and the shade top within (tA, tB), sorted, BIG-padded (rayBreakpoints).
vec4 sort4(vec4 v) {
  float a = min(v.x, v.y);
  float b = max(v.x, v.y);
  float c = min(v.z, v.w);
  float d = max(v.z, v.w);
  float e = min(a, c);
  float g = max(a, c);
  float k = min(b, d);
  float l = max(b, d);
  return vec4(e, min(g, k), max(g, k), l);
}
float inRange(float t, float tA, float tB) { return (t > tA && t < tB) ? t : BIG; }
vec4 rayBreakpoints(float h0, float mu, float shadeTop, float tA, float tB) {
  vec4 bp = vec4(BIG);
  vec2 hz = sphereInside(h0, mu, u_hazeTop);
  if (hz.x <= hz.y) { bp.x = inRange(hz.x, tA, tB); bp.y = inRange(hz.y, tA, tB); }
  if (shadeTop > -1.0e8) {
    vec2 sh = sphereInside(h0, mu, shadeTop);
    if (sh.x <= sh.y) { bp.z = inRange(sh.x, tA, tB); bp.w = inRange(sh.y, tA, tB); }
  }
  return sort4(bp);
}

// Where a ray crosses the Earth's shadow within (tA, tB), BIG-padded (cpu/atmosphere.ts shadowCrossings):
// the cylinder of radius R around the Earth-Sun axis, on its night side.
vec2 shadowCrossings(float h0, float mu, float muS, float nu, float tA, float tB) {
  float r0 = R_EARTH + h0;
  float a = 1.0 - nu * nu;
  float b = r0 * (mu - muS * nu);
  float c = h0 * (r0 + R_EARTH) - r0 * r0 * muS * muS;
  float t1 = BIG;
  float t2 = BIG;
  if (a < 1.0e-9) {
    if (b != 0.0) t1 = -c / (2.0 * b);
  } else {
    float disc = b * b - a * c;
    if (disc >= 0.0) {
      float sq = sqrt(disc);
      float q = -(b + (b >= 0.0 ? sq : -sq));
      t1 = q / a;
      if (q != 0.0) t2 = c / q;
    }
  }
  bool ok1 = t1 > tA && t1 < tB && t1 < BIG && r0 * muS + t1 * nu < 0.0;
  bool ok2 = t2 > tA && t2 < tB && t2 < BIG && r0 * muS + t2 * nu < 0.0;
  float u1 = ok1 ? t1 : BIG;
  float u2 = ok2 ? t2 : BIG;
  return vec2(min(u1, u2), max(u1, u2));
}

// Transmittance-table cell layout (cpu/atmosphere.ts densityEdges): 1 one piece crowded at its start; 2
// crowded at its end; 3 two pieces crowded towards the perigee tP (mode 0, uniform, is unused).
int cellMode(float h0, float mu, float tA, float tB, bool uniformCells, out float tP) {
  tP = -(R_EARTH + h0) * mu;
  if (uniformCells) return 0;
  if (tP > tA && tP < tB) return 3;
  if (tP >= tB) return 2;
  return 1;
}
float cellEdge(int k, int n, int mode, float tA, float tB, float tP) {
  if (k == 0) return tA;
  float fn = float(n);
  if (mode == 0) { if (k >= n) return tB; return tA + (tB - tA) * float(k) / fn; }
  if (mode == 1) { if (k >= n) return tB; float u = float(k) / fn; return tA + (tB - tA) * u * u; }
  if (mode == 2) { if (k >= n) return tB; float u = 1.0 - float(k) / fn; return tB - (tB - tA) * u * u; }
  if (k < n) { float u = 1.0 - float(k) / fn; return tP - (tP - tA) * u * u; }
  if (k == n) return tP;
  int k2 = k - n;
  if (k2 >= n) return tB;
  float u = float(k2) / fn;
  return tP + (tB - tP) * u * u;
}

struct Scatter { vec3 L; vec3 Lb; vec3 thr; vec3 fms; };
`;

/** The multiple-scattering table lookup (cpu/atmosphere.ts lookupMultiScattering). */
export const GLSL_MS_LOOKUP = `
uniform sampler2D u_msLut;
vec3 msAt(float h, float muS) {
  float x = clamp(muS * 0.5 + 0.5, 0.0, 1.0);
  float y = clamp((h - ATM_MIN_H) / (ATM_TOP_H - ATM_MIN_H), 0.0, 1.0);
  return lutSample(u_msLut, ivec2(MS_N), vec2(unitToSubUv(x, MS_N), unitToSubUv(y, MS_N)));
}
`;

/** Sky-view parameterisation (model.ts skyViewUvToDirection / skyViewDirectionToUv). */
export const GLSL_SKY_VIEW_MAPPING = `
vec2 skyViewUvToDirection(float hView, vec2 uv) {
  float x = subUvToUnit(uv.x, SV_W);
  float y = subUvToUnit(uv.y, SV_H);
  float dip = groundDip(hView);
  float zha = 0.5 * PI + dip;
  float beta = 0.5 * PI - dip;
  float vzc;
  if (y < 0.5) {
    float c = 1.0 - 2.0 * y;
    c = 1.0 - c * c;
    vzc = cosAcc(zha * c);
  } else {
    float c = 2.0 * y - 1.0;
    c = c * c;
    vzc = cosAcc(zha + beta * c);
  }
  return vec2(vzc, 1.0 - 2.0 * x * x);
}
vec2 skyViewDirectionToUv(float hView, float vzc, float lvc, bool hitsGround) {
  float dip = groundDip(hView);
  float zha = 0.5 * PI + dip;
  float beta = 0.5 * PI - dip;
  float zen = acosAcc(vzc);
  float y;
  if (!hitsGround) {
    float c = sqrt(max(1.0 - zen / zha, 0.0));
    y = (1.0 - c) * 0.5;
  } else {
    float c = sqrt(max((zen - zha) / beta, 0.0));
    y = c * 0.5 + 0.5;
  }
  float x = sqrt(clamp(0.5 - 0.5 * lvc, 0.0, 1.0));
  return vec2(unitToSubUv(clamp(x, 0.0, 1.0), SV_W), unitToSubUv(clamp(y, 0.0, 1.0), SV_H));
}
// Cosine of the azimuth between a direction and the Sun (cpu/atmosphere.ts lightViewCos).
float lightViewCos(vec3 dir, vec3 sun) {
  float lv = length(dir.xy);
  float ls = length(sun.xy);
  if (lv < 1.0e-6 || ls < 1.0e-6) return 1.0;
  return clamp(dot(dir.xy, sun.xy) / (lv * ls), -1.0, 1.0);
}
`;

/** Aerial-perspective depth mapping (model.ts apDepthAt / apSliceCoord). */
export const GLSL_AP_MAPPING = `
float apDepthAt(float w) { return AP_NEAR * (exp(w * AP_LOG_SPAN) - 1.0); }
float apSliceCoord(float t) { return min(log(1.0 + max(t, 0.0) / AP_NEAR) / AP_LOG_SPAN, 1.0); }
`;

/**
 * The integrator (cpu/atmosphere.ts integrateScattering), specialised at build time: 'sky' for the
 * sky-view table and the aerial perspective (Rayleigh and Mie phases plus Psi_ms), 'iso' for the
 * multiple-scattering table (isotropic phase, no Psi_ms, with f_ms).
 */
function integratorGlsl(kind: 'sky' | 'iso'): string {
  const iso = kind === 'iso';
  const name = iso ? 'integrateIso' : 'integrateSky';
  const cell = iso ? 'cellIso' : 'cellSky';
  return `
void ${cell}(float ta, float tb, float h0, float mu, float muS, float nu, float pR, float pM, float shadeTop, inout Scatter res) {
  float dt = tb - ta;
  if (!(dt > 0.0)) return;
  float tm = 0.5 * (ta + tb);
  float h = pointHeight(h0, mu, tm);
  float r = R_EARTH + h;
  float muSt = clamp(((R_EARTH + h0) * muS + tm * nu) / r, -1.0, 1.0);
  vec3 sR;
  vec3 sM;
  vec3 e;
  medium(h, sR, sM, e);
  vec3 ts = transmittanceAt(h, muSt);
  float vis = diskVisibility(h, muSt, SUN_RADIUS);
  vec3 s = sR + sM;
  ${iso ? 'vec3 src = vis * ts * s * ISO_PHASE;' : 'vec3 src = vis * ts * (sR * pR + sM * pM) + msAt(h, muSt) * s;'}
  vec3 w = cellWeight(e, dt);
  vec3 add = res.thr * src * w;
  res.L += add;
  if (h < shadeTop) res.Lb += add;
  ${iso ? 'res.fms += res.thr * s * w;' : ''}
  res.thr *= exp(-e * dt);
}

// cpu/atmosphere.ts integrateScattering with integrationEdges: knotted layouts cut the ray at its perigee,
// its haze-top crossings and its Earth-shadow crossings and lay out n cells per piece as inScatterEdges
// does (both ends crowded, except the last piece of a rising ray: its start); uniform layouts split their
// n cells there instead. Both split cells at the shade top.
Scatter ${name}(float h0, float mu, float muS, float nu, float tA, float tB, int n, bool uniformCells, float shadeTop) {
  Scatter res = Scatter(vec3(0.0), vec3(0.0), vec3(1.0), vec3(0.0));
  float pR = rayleighPhase(nu);
  float pM = csPhase(MIE_G, nu);
  float tP = -(R_EARTH + h0) * mu;
  vec2 hz = sphereInside(h0, mu, u_hazeTop);
  if (!(hz.x <= hz.y)) hz = vec2(BIG);
  vec2 sc = shadowCrossings(h0, mu, muS, nu, tA, tB);
  vec2 sh = vec2(BIG);
  if (shadeTop > -1.0e8) { sh = sphereInside(h0, mu, shadeTop); if (!(sh.x <= sh.y)) sh = vec2(BIG); }
  float kn[7];
  int nk = 1;
  kn[0] = tA;
  float bp[6];
  int nb = 0;
  float cand[6];
  if (uniformCells) {
    cand[0] = hz.x; cand[1] = hz.y; cand[2] = sh.x; cand[3] = sh.y; cand[4] = sc.x; cand[5] = sc.y;
  } else {
    cand[0] = tP; cand[1] = hz.x; cand[2] = hz.y; cand[3] = sc.x; cand[4] = sc.y; cand[5] = BIG;
  }
  // inner knots (knotted) or breakpoints (uniform): those within (tA, tB), insertion-sorted
  float srt[6];
  int ns = 0;
  for (int q = 0; q < 6; ++q) {
    float t = cand[q];
    if (t > tA && t < tB && t < BIG) {
      int k = ns;
      for (int m = 0; m < 6; ++m) { if (k > 0 && srt[k - 1] > t) { srt[k] = srt[k - 1]; k--; } }
      srt[k] = t;
      ns++;
    }
  }
  if (uniformCells) {
    for (int q = 0; q < 6; ++q) { if (q < ns) bp[q] = srt[q]; }
    nb = ns;
  } else {
    for (int q = 0; q < 6; ++q) { if (q < ns) { kn[nk] = srt[q]; nk++; } }
    // the shade top only splits cells
    float s0 = (sh.x > tA && sh.x < tB) ? sh.x : BIG;
    float s1 = (sh.y > tA && sh.y < tB) ? sh.y : BIG;
    bp[0] = min(s0, s1);
    bp[1] = max(s0, s1);
    nb = (bp[0] < BIG ? 1 : 0) + (bp[1] < BIG ? 1 : 0);
  }
  kn[nk] = tB;
  nk++;
  int bi = 0;
  float ta = tA;
  for (int pc = 0; pc < 6; ++pc) {
    if (pc >= nk - 1) break;
    float a = kn[pc];
    float b = kn[pc + 1];
    if (!(b > a)) continue;
    bool startOnly = !(b <= tP) && pc == nk - 2;
    for (int j = 1; j <= 32; ++j) {
      if (j > n) break;
      float u = float(j) / float(n);
      float tb;
      if (j == n) tb = b;
      else if (uniformCells) tb = a + (b - a) * u;
      else if (startOnly) tb = a + (b - a) * u * u;
      else tb = a + (b - a) * u * u * (3.0 - 2.0 * u);
      for (int q = 0; q < 6; ++q) {
        if (bi < nb && bp[bi] < tb) {
          if (bp[bi] > ta) {
            ${cell}(ta, bp[bi], h0, mu, muS, nu, pR, pM, shadeTop, res);
            ta = bp[bi];
          }
          bi++;
        }
      }
      ${cell}(ta, tb, h0, mu, muS, nu, pR, pM, shadeTop, res);
      ta = tb;
    }
  }
  return res;
}
`;
}

/** Full-screen-triangle vertex shader shared by every pass. */
export const PASS_VERTEX_SHADER = `#version 300 es
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
`;

const PRELUDE = `${GLSL_HEADER}${ATMOSPHERE_NOTICE_GLSL}${GLSL_GEOMETRY}${GLSL_ATMOSPHERE_COMMON}`;

/** Pass 1a: the transmittance table (cpu/atmosphere.ts computeTransmittanceLut, opticalDepthToTop). */
export const TRANSMITTANCE_FS = `${PRELUDE}
out vec4 o_color;

void main() {
  vec2 uv = gl_FragCoord.xy / vec2(float(TW), float(TH));
  float xMu = subUvToUnit(uv.x, TW);
  float xR = subUvToUnit(uv.y, TH);
  float rho = ATM_H_TOP * xR;
  float r = sqrt(rho * rho + R_EARTH * R_EARTH);
  float h0 = (rho * rho) / (r + R_EARTH);
  float dMin = ATM_TOP_H - h0;
  float dMax = rho + ATM_H_TOP;
  float d = dMin + xMu * (dMax - dMin);
  float mu = d == 0.0 ? 1.0 : ((ATM_H_TOP - rho) * (ATM_H_TOP + rho) - d * d) / (2.0 * r * d);
  mu = clamp(mu, -1.0, 1.0);
  float tTop = distanceToTop(h0, mu);
  vec4 bp = rayBreakpoints(h0, mu, -2.0e9, 0.0, tTop);
  float tP;
  int mode = cellMode(h0, mu, 0.0, tTop, false, tP);
  const int n = ${TRANSMITTANCE_CELLS};
  int ncells = mode == 3 ? 2 * n : n;
  vec3 tau = vec3(0.0);
  int bi = 0;
  float ta = 0.0;
  for (int k = 1; k <= 2 * n; ++k) {
    if (k > ncells) break;
    float tb = cellEdge(k, n, mode, 0.0, tTop, tP);
    for (int q = 0; q < 4; ++q) {
      if (bi < 4 && bp[bi] < tb) {
        if (bp[bi] > ta) {
          vec3 sR; vec3 sM; vec3 e;
          medium(pointHeight(h0, mu, 0.5 * (ta + bp[bi])), sR, sM, e);
          tau += e * (bp[bi] - ta);
          ta = bp[bi];
        }
        bi++;
      }
    }
    if (tb > ta) {
      vec3 sR; vec3 sM; vec3 e;
      medium(pointHeight(h0, mu, 0.5 * (ta + tb)), sR, sM, e);
      tau += e * (tb - ta);
    }
    ta = tb;
  }
  o_color = vec4(exp(-tau), 1.0);
}
`;

/** Pass 1b: the multiple-scattering table (cpu/atmosphere.ts computeMultiScatteringLut). */
export const MULTI_SCATTERING_FS = `${PRELUDE}
${integratorGlsl('iso')}
// sin(2 pi (i + 0.5) / 8) for the 8 x 8 directions, computed in float64 (no driver trig).
const float MS_SIN_THETA[${MULTI_SCATTERING_SQRT_DIRECTIONS}] = float[${MULTI_SCATTERING_SQRT_DIRECTIONS}](${Array.from({ length: MULTI_SCATTERING_SQRT_DIRECTIONS }, (_, i) => f(Math.sin((2 * Math.PI * (i + 0.5)) / MULTI_SCATTERING_SQRT_DIRECTIONS))).join(', ')});
out vec4 o_color;

void main() {
  vec2 uv = gl_FragCoord.xy / vec2(float(MS_N));
  float muS = 2.0 * subUvToUnit(uv.x, MS_N) - 1.0;
  float h = ATM_MIN_H + subUvToUnit(uv.y, MS_N) * (ATM_TOP_H - ATM_MIN_H);
  float sunY = sqrt(max(1.0 - muS * muS, 0.0));
  const int SQ = ${MULTI_SCATTERING_SQRT_DIRECTIONS};
  vec3 sumL = vec3(0.0);
  vec3 sumF = vec3(0.0);
  for (int k = 0; k < SQ * SQ; ++k) {
    int i = k / SQ;
    int j = k - i * SQ;
    float mu = 1.0 - 2.0 * (float(j) + 0.5) / float(SQ);
    float sinPhi = sqrt(max(1.0 - mu * mu, 0.0));
    float nu = sinPhi * MS_SIN_THETA[i] * sunY + mu * muS;
    float tG = groundHit(h, mu);
    bool hits = tG < BIG;
    float tB = hits ? tG : distanceToTop(h, mu);
    Scatter r = integrateIso(h, mu, muS, nu, 0.0, tB, ${MULTI_SCATTERING_CELLS}, false, -2.0e9);
    if (hits) {
      float muSg = clamp(((R_EARTH + h) * muS + tG * nu) / R_EARTH, -1.0, 1.0);
      vec3 tg = transmittanceAt(0.0, muSg);
      float vis = diskVisibility(0.0, muSg, SUN_RADIUS);
      r.L += r.thr * vis * tg * max(muSg, 0.0) * (GROUND_ALBEDO / PI);
    }
    sumL += r.L;
    sumF += r.fms;
  }
  float nd = float(SQ * SQ);
  vec3 f = sumF / nd;
  o_color = vec4((sumL / nd) / (1.0 - f), max(f.r, max(f.g, f.b)));
}
`;

/** Pass 1c: the sky-view tables (cpu/atmosphere.ts computeSkyViewLuts): total and below the shade top. */
export const SKY_VIEW_FS = `${PRELUDE}${GLSL_MS_LOOKUP}${GLSL_SKY_VIEW_MAPPING}
${integratorGlsl('sky')}
uniform float u_viewH;
uniform float u_muS;
uniform float u_shadeTop;
layout(location = 0) out vec4 o_total;
layout(location = 1) out vec4 o_below;

void main() {
  vec2 uv = gl_FragCoord.xy / vec2(float(SV_W), float(SV_H));
  vec2 d = skyViewUvToDirection(u_viewH, uv);
  float sinV = sqrt(max(1.0 - d.x * d.x, 0.0));
  float sinS = sqrt(max(1.0 - u_muS * u_muS, 0.0));
  float nu = sinV * d.y * sinS + d.x * u_muS;
  float tB = rayEndInAtmosphere(u_viewH, d.x);
  Scatter r = integrateSky(u_viewH, d.x, u_muS, nu, 0.0, tB, ${SKY_VIEW_CELLS}, false, u_shadeTop);
  o_total = vec4(r.L, 1.0);
  o_below = vec4(r.Lb, 1.0);
}
`;

const T = LIGHT_TEXELS;

/**
 * Pass 1d (first half): the clear-sky irradiance quadrature over the sky-view table (cpu/atmosphere.ts
 * skyIrradiance), one fragment per azimuth summing its zenith cells, so no fragment walks all 1,024
 * lookups in series (the GPU runs the 32 sums in parallel). The light pass adds the 32 sums.
 */
export const IRRADIANCE_FS = `${PRELUDE}${GLSL_SKY_VIEW_MAPPING}
uniform sampler2D u_skyLut0;
uniform float u_viewH;
// cos(pi (a + 0.5) / 32): the quadrature's azimuths, computed in float64 (no driver trig).
const float IRR_COS_AZ[${IRRADIANCE_AZIMUTH_CELLS}] = float[${IRRADIANCE_AZIMUTH_CELLS}](${Array.from({ length: IRRADIANCE_AZIMUTH_CELLS }, (_, a) => f(Math.cos((Math.PI * (a + 0.5)) / IRRADIANCE_AZIMUTH_CELLS))).join(', ')});
out vec4 o_color;

void main() {
  int a = int(gl_FragCoord.x);
  float lvc = IRR_COS_AZ[a];
  vec3 sky = vec3(0.0);
  for (int b = 0; b < ${IRRADIANCE_ZENITH_CELLS}; ++b) {
    float sin2 = (float(b) + 0.5) / ${f(IRRADIANCE_ZENITH_CELLS)};
    float cosZ = sqrt(1.0 - sin2);
    sky += lutSample(u_skyLut0, ivec2(SV_W, SV_H), skyViewDirectionToUv(u_viewH, cosZ, lvc, false));
  }
  o_color = vec4(sky, 1.0);
}
`;

/** Pass 1d: the light model (cpu/atmosphere.ts lightFromAtmosphere and packLight); one fragment per output texel. */
export const LIGHT_FS = `${PRELUDE}
#define MAX_LAYERS ${MAX_LAYERS}
uniform sampler2D u_irrLut;
uniform float u_viewH;
uniform vec3 u_sunDir;
uniform vec3 u_moonDir;
uniform float u_solarLux;
uniform float u_tAmb;
uniform float u_tDirExp;
uniform float u_tBeamTot;
uniform float u_moonLux;
uniform int u_layerCount;
uniform vec4 u_layerLight[MAX_LAYERS];   // top_m, tTot, wBelow, wAbove
out vec4 o_color;

float lumY(vec3 c) { return 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b; }

void main() {
  int k = int(gl_FragCoord.x);
  float muS = u_sunDir.z;
  vec3 tS = transmittanceAt(u_viewH, muS);
  float vS = diskVisibility(u_viewH, muS, SUN_RADIUS);
  vec3 eDir = u_solarLux * tS * vS * max(muS, 0.0);
  // Clear-sky irradiance: the irradiance pass's per-azimuth sums of the sky-view table, added up.
  vec3 sky = vec3(0.0);
  for (int a = 0; a < ${IRRADIANCE_AZIMUTH_CELLS}; ++a) sky += texelFetch(u_irrLut, ivec2(a, 0), 0).rgb;
  vec3 eSky = u_solarLux * sky * (PI / ${f(IRRADIANCE_AZIMUTH_CELLS * IRRADIANCE_ZENITH_CELLS)});
  float muM = u_moonDir.z;
  vec3 eMoonN = u_moonLux * transmittanceAt(u_viewH, muM) * diskVisibility(u_viewH, muM, MOON_RADIUS);
  vec3 eMoonH = eMoonN * max(muM, 0.0);
  vec3 skyBg = ${v3(NIGHT_SKY_COLOR)} * (${f(NIGHT_SKY_LUMINANCE)} + ${f(MOON_SKY_FACTOR)} * lumY(eMoonH));
  vec3 eSkyClear = eSky + PI * skyBg + eMoonH;
  vec3 eDif = eSkyClear * u_tAmb + eDir * max(u_tBeamTot - u_tDirExp, 0.0);
  vec3 eCam = eDir * u_tDirExp + eDif;
  float eCamY = lumY(eCam);
  float eClearY = lumY(eDir + eSkyClear);
  float cloudDim = eClearY > 0.0 ? min(1.0, eCamY / eClearY) : 1.0;
  float exposure = PI / max(eCamY, ${f(CAMERA_MIN_ADAPT_LUX)});
  vec4 o = vec4(0.0);
  if (k == ${T.eDir}) o = vec4(eDir, exposure);
  else if (k == ${T.eDif}) o = vec4(eDif, cloudDim);
  else if (k == ${T.eCam}) o = vec4(eCam, eCamY);
  else if (k == ${T.eSkyClear}) o = vec4(eSkyClear, lumY(eSky));
  else if (k == ${T.eSky}) o = vec4(eSky, 0.0);
  else if (k == ${T.fogAir}) o = vec4(eCam * ${f(FOG_ALBEDO / Math.PI)}, 0.0);
  else if (k == ${T.skyBackground}) o = vec4(skyBg, 0.0);
  else if (k == ${T.eMoonN}) o = vec4(eMoonN, 0.0);
  else if (k >= ${T.layer0} && k < ${T.layer0} + u_layerCount) {
    vec4 l = u_layerLight[k - ${T.layer0}];
    float hTop = clamp(l.x, ATM_MIN_H, ATM_TOP_H);
    vec3 tl = transmittanceAt(hTop, muS);
    float vl = diskVisibility(hTop, muS, SUN_RADIUS);
    // sin(altitude + dip) = sin(alt) cos(dip) + cos(alt) sin(dip), with no driver trig:
    // cos(dip) = R / (R + h), sin(dip) = sqrt(h (2R + h)) / (R + h).
    float cosDip = R_EARTH / (R_EARTH + hTop);
    float sinDip = sqrt(hTop * (2.0 * R_EARTH + hTop)) / (R_EARTH + hTop);
    float sinAltL = muS * cosDip + sqrt(max(1.0 - muS * muS, 0.0)) * sinDip;
    float shape = 0.7 * max(sinAltL, 0.0) + 0.3 * vl;
    vec3 eAbove = u_solarLux * tl * vl * shape + eSkyClear;
    o = vec4((eAbove / PI) * (l.y * l.z + (1.0 - l.y) * l.w), 0.0);
  }
  o_color = o;
}
`;

/** Pass 1e: aerial-perspective increments, one depth slice per froxel (cpu/atmosphere.ts computeAerialPerspective). */
export const AP_INCREMENT_FS = `${PRELUDE}${GLSL_MS_LOOKUP}${GLSL_AP_MAPPING}
${integratorGlsl('sky')}
uniform sampler2D u_lightLut;
uniform vec3 u_camF;
uniform vec3 u_camR;
uniform vec3 u_camU;
uniform vec2 u_tanHalf;
uniform float u_viewH;
uniform vec3 u_sunDir;
uniform float u_shadeTop;
layout(location = 0) out vec4 o_scatter;
layout(location = 1) out vec4 o_transmittance;

void main() {
  ivec2 p = ivec2(gl_FragCoord.xy);
  int k = p.x / AP_GRID;
  int i = p.x - k * AP_GRID;
  int j = p.y;
  vec2 ndc = vec2(2.0 * (float(i) + 0.5) / float(AP_GRID) - 1.0, 2.0 * (float(j) + 0.5) / float(AP_GRID) - 1.0);
  vec3 dir = normalize(u_camF + (ndc.x * u_tanHalf.x) * u_camR + (ndc.y * u_tanHalf.y) * u_camU);
  float tEnd = rayEndInAtmosphere(u_viewH, dir.z);
  float ta = apDepthAt(float(k) / float(AP_SLICES));
  float tb = min(apDepthAt(float(k + 1) / float(AP_SLICES)), tEnd);
  vec3 S = vec3(0.0);
  vec3 Tr = vec3(1.0);
  if (tb > ta) {
    Scatter r = integrateSky(u_viewH, dir.z, u_sunDir.z, dot(dir, u_sunDir), ta, tb, ${AP_CELLS_PER_SLICE}, true, u_shadeTop);
    float cloudDim = texelFetch(u_lightLut, ivec2(${T.eDif}, 0), 0).a;
    S = r.L - (1.0 - cloudDim) * r.Lb;
    Tr = r.thr;
  }
  o_scatter = vec4(S, 1.0);
  o_transmittance = vec4(Tr, 1.0);
}
`;

/** Pass 1f: aerial-perspective composition of the increments along each froxel column. */
export const AP_PREFIX_FS = `${GLSL_HEADER}
const int AP_GRID = ${AP_GRID};
uniform sampler2D u_apInc0;
uniform sampler2D u_apInc1;
layout(location = 0) out vec4 o_scatter;
layout(location = 1) out vec4 o_transmittance;

void main() {
  ivec2 p = ivec2(gl_FragCoord.xy);
  int k = p.x / AP_GRID;
  int i = p.x - k * AP_GRID;
  vec3 acc = vec3(0.0);
  vec3 tr = vec3(1.0);
  for (int m = 0; m < ${AP_SLICES}; ++m) {
    if (m > k) break;
    ivec2 q = ivec2(m * AP_GRID + i, p.y);
    acc += tr * texelFetch(u_apInc0, q, 0).rgb;
    tr *= texelFetch(u_apInc1, q, 0).rgb;
  }
  o_scatter = vec4(acc, 1.0);
  o_transmittance = vec4(tr, 1.0);
}
`;

/** Lookups the main pass uses: the light texels, the sky's in-scatter and the aerial perspective. */
export const GLSL_MAIN_ATMOSPHERE = `${GLSL_SKY_VIEW_MAPPING}${GLSL_AP_MAPPING}
uniform sampler2D u_skyLut0;
uniform sampler2D u_skyLut1;
uniform sampler2D u_apS;
uniform sampler2D u_apT;
uniform sampler2D u_lightLut;
uniform float u_viewH;
uniform float u_solarLux;

vec4 lightTexel(int k) { return texelFetch(u_lightLut, ivec2(k, 0), 0); }

// In-scattered radiance per unit solar illuminance along a camera direction to the end of the atmosphere,
// with the part scattered below the shade top dimmed by the cloud dimming (cpu/shade.ts skyInScatter).
vec3 skyInScatter(vec3 dir) {
  bool hits = groundHit(u_viewH, dir.z) < BIG;
  vec2 uv = skyViewDirectionToUv(u_viewH, dir.z, lightViewCos(dir, u_sunDir), hits);
  vec3 tot = lutSample(u_skyLut0, ivec2(SV_W, SV_H), uv);
  vec3 bel = lutSample(u_skyLut1, ivec2(SV_W, SV_H), uv);
  return tot - (1.0 - lightTexel(${T.eDif}).a) * bel;
}

// Aerial perspective at screen position fuv (gl_FragCoord / resolution) and depth t: in-scatter (per unit
// solar illuminance) and transmittance from the camera, trilinear (cpu/atmosphere.ts lookupAerialPerspective).
struct Ap { vec3 S; vec3 T; };
vec3 apTexel(sampler2D s, int k, ivec2 c0, vec2 w) {
  int x = k * AP_GRID;
  vec3 a00 = texelFetch(s, ivec2(x + c0.x, c0.y), 0).rgb;
  vec3 a10 = texelFetch(s, ivec2(x + c0.x + 1, c0.y), 0).rgb;
  vec3 a01 = texelFetch(s, ivec2(x + c0.x, c0.y + 1), 0).rgb;
  vec3 a11 = texelFetch(s, ivec2(x + c0.x + 1, c0.y + 1), 0).rgb;
  vec3 a = a00 + (a10 - a00) * w.x;
  vec3 b = a01 + (a11 - a01) * w.x;
  return a + (b - a) * w.y;
}
Ap apLookup(vec2 fuv, float t) {
  vec2 g = clamp(fuv * float(AP_GRID) - 0.5, vec2(0.0), vec2(float(AP_GRID - 1)));
  vec2 g0 = min(floor(g), vec2(float(AP_GRID - 2)));
  vec2 w = g - g0;
  ivec2 c0 = ivec2(g0);
  float s = apSliceCoord(t) * float(AP_SLICES) - 1.0;
  if (s < 0.0) {
    float a = s + 1.0;
    return Ap(a * apTexel(u_apS, 0, c0, w), vec3(1.0) + a * (apTexel(u_apT, 0, c0, w) - vec3(1.0)));
  }
  int k0 = min(int(floor(s)), AP_SLICES - 2);
  float wk = min(s - float(k0), 1.0);
  vec3 s0 = apTexel(u_apS, k0, c0, w);
  vec3 t0 = apTexel(u_apT, k0, c0, w);
  return Ap(s0 + (apTexel(u_apS, k0 + 1, c0, w) - s0) * wk, t0 + (apTexel(u_apT, k0 + 1, c0, w) - t0) * wk);
}
`;
