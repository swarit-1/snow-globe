/**
 * GLSL ES 3.00 sources for renderer v0 (PRD §9.2). One full-screen fragment program runs the passes in
 * order; each pass is a function below, named after the PRD table:
 *
 *   (1) sky v0          skyRadiance()      analytic placeholder; TODO(M1.5) Hillaire 2020 LUTs (D-013).
 *                                          The port's MIT notice goes in a legal comment (slash-star-bang)
 *                                          here and in the shader string; esbuild keeps legal comments in
 *                                          the bundle (legalComments 'eof', tested).
 *   (2) Sun and Moon    diskRadiance()     geometric disks; TODO(M2) Moon phase shape, stars
 *   (3) cloud slabs     layerAlongRay()    slabs with Earth curvature, cover read at the intersection,
 *                                          noise thresholded BEFORE averaging, sample count from the path
 *                                          length (step = 1/16 of the noise cell, 4-128 samples)
 *   (4) scene light v0  groundRadiance()   plate x (direct Sun x cloud transmittance + diffuse);
 *                                          TODO(M2) dehazed plate, normals, DEM horizon shadows
 *   (6) haze and fog    mediaTau()         clear-air haze + near-surface fog (flat-ground v0) with a soft top
 *   (8) camera v0       toneCurve()        exposure + soft-knee tone curve + sRGB; TODO(M2) Kim & Pollefeys
 *
 * Passes 5 (night lights) and 7 (precipitation) are not in v0: TODO(M2).
 *
 * Every constant shared with the CPU reference is injected from TypeScript, and the geometry functions
 * mirror math/shell.ts, math/cover.ts, math/noise.ts and math/coverage.ts line for line.
 */
import {
  CLOUD_MAX_SAMPLES,
  CLOUD_MIN_SAMPLES,
  CLOUD_SKIP_TRANSMITTANCE,
  CLOUD_TAU_OPAQUE,
  EARTH_RADIUS_M,
  FOG_DECAY_BANDS,
  MAX_LAYERS,
  MAX_PROFILE_KNOTS,
  T_INFINITE_M,
} from '../math/constants.ts';
import { ACKLAM_A, ACKLAM_B, ACKLAM_C, ACKLAM_D, ACKLAM_P_LOW } from '../math/normal.ts';
import { LATTICE_MASK, NOISE_OCTAVES } from '../math/noise.ts';
import { SKY_COLORS, type RGB } from '../light.ts';

const f = (x: number): string => {
  const s = x.toPrecision(10);
  return /[.e]/.test(s) ? s : `${s}.0`;
};
const v3 = (c: RGB): string => `vec3(${f(c[0])}, ${f(c[1])}, ${f(c[2])})`;

/**
 * Debug outputs (rendered to a float target), per slab in rgba: coverage = mask coverage kappa over the
 * whole path, target = mean forecast cover at the sample points, alpha = drawn slab opacity. hdr = the
 * exposed linear radiance before any clamp (so NaN and negative values stay visible); ray = direction and
 * ray end.
 */
export const DEBUG_MODES = { none: 0, coverage: 1, hdr: 2, ray: 3, target: 4, alpha: 5 } as const;
export type DebugMode = keyof typeof DEBUG_MODES;

export const VERTEX_SHADER = `#version 300 es
// Full-screen triangle from gl_VertexID; no vertex buffers.
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
`;

export const FRAGMENT_SHADER = `#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;

#define MAX_LAYERS ${MAX_LAYERS}
#define MAX_KNOTS ${MAX_PROFILE_KNOTS}
#define OCTAVES ${NOISE_OCTAVES}
#define CLOUD_MIN_SAMPLES ${CLOUD_MIN_SAMPLES}
#define CLOUD_MAX_SAMPLES ${CLOUD_MAX_SAMPLES}
#define LATTICE_MASK ${LATTICE_MASK}
#define FOG_DECAY_BANDS ${FOG_DECAY_BANDS}

const float R_EARTH = ${f(EARTH_RADIUS_M)};
const float PI = 3.141592653589793;
const float T_INF = ${f(T_INFINITE_M)};
const float TAU_OPAQUE = ${f(CLOUD_TAU_OPAQUE)};
// The colour path skips slabs behind nearer slabs whose combined transmittance is below this: they could
// change the pixel by less than 0.1 % (0.26 of an 8-bit level). Debug outputs always sample every slab.
const float SKIP_TRANSMITTANCE = ${f(CLOUD_SKIP_TRANSMITTANCE)};

// Camera (local ENU frame at the camera's foot point; Earth centre at (0, 0, -R)).
uniform vec2 u_resolution;
uniform vec3 u_camF;
uniform vec3 u_camR;
uniform vec3 u_camU;
uniform vec2 u_tanHalf;
uniform float u_hCam;
uniform float u_hGround;   // v0 flat-ground height (camera height minus its height above ground)
uniform vec3 u_sunDir;
uniform vec3 u_moonDir;

// Inputs.
uniform sampler2D u_plate;    // SRGB8_ALPHA8: sampling returns linear values
uniform sampler2D u_skyMask;  // R: 1 = sky
uniform sampler2D u_depth;    // RGB24 metres along the ray; 0 unknown, 0xFFFFFF sky
uniform sampler2D u_lattice;  // R32F 256 x 256 N(0,1) lattice
uniform int u_hasSkyMask;
uniform int u_hasDepth;
uniform float u_nominalDepth;

// Slabs.
uniform int u_layerCount;
uniform vec4 u_layerGeom[MAX_LAYERS];       // base_m, top_m, beta_per_m, solid
uniform float u_layerStep[MAX_LAYERS];      // cloud-mask sample step along view rays (m)
uniform vec3 u_layerRadiance[MAX_LAYERS];   // face radiance seen by this camera
uniform int u_profileCount[MAX_LAYERS];
uniform vec4 u_profile[MAX_LAYERS * MAX_KNOTS / 2]; // (km, cover) pairs, two per vec4
uniform vec4 u_noiseM[MAX_LAYERS * OCTAVES];        // per octave: m00 m01 m10 m11
uniform vec4 u_noiseC[MAX_LAYERS * OCTAVES / 2];    // per octave: c0 c1, two per vec4
uniform vec4 u_noiseAmp;

// Haze and fog.
uniform float u_hazeBeta;
uniform float u_hazeTop;
uniform float u_fogBeta;
uniform float u_fogTop;
uniform float u_fogDecayH;

// Light and camera.
uniform vec3 u_eDir;
uniform vec3 u_eDif;
uniform vec3 u_sunDiskL;
uniform vec3 u_moonDiskL;
uniform vec4 u_sky;        // zenith luminance, day, twilight, night weights
uniform vec3 u_skyMoon;
uniform vec3 u_glow;
uniform float u_cloudDim;
uniform vec3 u_fogAir;
uniform float u_fogAirW;
uniform float u_exposure;
uniform float u_sunRadius;
uniform float u_moonRadius;
uniform float u_pixelAngle;
uniform int u_sunUp;
uniform int u_debugMode;

out vec4 o_color;

// ---------------------------------------------------------------- geometry (mirrors math/shell.ts)
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

// Great-circle distance (km) from the camera's foot point to a local point (mirrors math/geodesy.ts).
float groundDistKm(vec3 P) { return R_EARTH * atan(length(P.xy), R_EARTH + P.z) * 0.001; }

// ---------------------------------------------------------------- cover profile (mirrors math/cover.ts)
vec2 knotAt(int li, int j) {
  int idx = li * MAX_KNOTS + j;
  vec4 v = u_profile[idx >> 1];
  return ((idx & 1) == 0) ? v.xy : v.zw;
}

float coverAt(int li, float dkm) {
  int n = u_profileCount[li];
  vec2 k0 = knotAt(li, 0);
  if (n <= 1 || dkm <= k0.x) return k0.y;
  for (int j = 1; j < MAX_KNOTS; ++j) {
    if (j >= n) break;
    vec2 k1 = knotAt(li, j);
    if (dkm <= k1.x) {
      float a = (dkm - k0.x) / (k1.x - k0.x);
      return k0.y + (k1.y - k0.y) * a;
    }
    k0 = k1;
  }
  return k0.y;
}

// ---------------------------------------------------------------- threshold (mirrors math/normal.ts, math/coverage.ts)
float normalQuantile(float p) {
  if (p < ${f(ACKLAM_P_LOW)}) {
    float q = sqrt(-2.0 * log(p));
    return (((((${f(ACKLAM_C[0])} * q + ${f(ACKLAM_C[1])}) * q + ${f(ACKLAM_C[2])}) * q + ${f(ACKLAM_C[3])}) * q + ${f(ACKLAM_C[4])}) * q + ${f(ACKLAM_C[5])}) /
           ((((${f(ACKLAM_D[0])} * q + ${f(ACKLAM_D[1])}) * q + ${f(ACKLAM_D[2])}) * q + ${f(ACKLAM_D[3])}) * q + 1.0);
  }
  if (p > ${f(1 - ACKLAM_P_LOW)}) {
    float q = sqrt(-2.0 * log(1.0 - p));
    return -(((((${f(ACKLAM_C[0])} * q + ${f(ACKLAM_C[1])}) * q + ${f(ACKLAM_C[2])}) * q + ${f(ACKLAM_C[3])}) * q + ${f(ACKLAM_C[4])}) * q + ${f(ACKLAM_C[5])}) /
            ((((${f(ACKLAM_D[0])} * q + ${f(ACKLAM_D[1])}) * q + ${f(ACKLAM_D[2])}) * q + ${f(ACKLAM_D[3])}) * q + 1.0);
  }
  float q = p - 0.5;
  float r = q * q;
  return (((((${f(ACKLAM_A[0])} * r + ${f(ACKLAM_A[1])}) * r + ${f(ACKLAM_A[2])}) * r + ${f(ACKLAM_A[3])}) * r + ${f(ACKLAM_A[4])}) * r + ${f(ACKLAM_A[5])}) * q /
         (((((${f(ACKLAM_B[0])} * r + ${f(ACKLAM_B[1])}) * r + ${f(ACKLAM_B[2])}) * r + ${f(ACKLAM_B[3])}) * r + ${f(ACKLAM_B[4])}) * r + 1.0);
}

float coverThreshold(float c) {
  if (c <= 0.0) return -1.0e30;
  if (c >= 1.0) return 1.0e30;
  return normalQuantile(c);
}

// ---------------------------------------------------------------- noise (mirrors math/noise.ts)
float latticeAt(ivec2 i) { return texelFetch(u_lattice, i & ivec2(LATTICE_MASK), 0).r; }

float octaveValue(vec2 u) {
  vec2 fl = floor(u);
  ivec2 i = ivec2(fl);
  vec2 t = u - fl;
  vec2 s = t * t * (3.0 - 2.0 * t);
  float w00 = (1.0 - s.x) * (1.0 - s.y);
  float w10 = s.x * (1.0 - s.y);
  float w01 = (1.0 - s.x) * s.y;
  float w11 = s.x * s.y;
  float v = w00 * latticeAt(i) + w10 * latticeAt(i + ivec2(1, 0)) + w01 * latticeAt(i + ivec2(0, 1)) + w11 * latticeAt(i + ivec2(1, 1));
  return v / sqrt(w00 * w00 + w10 * w10 + w01 * w01 + w11 * w11);
}

float cloudNoise(int li, vec2 xy) {
  float n = 0.0;
  for (int o = 0; o < OCTAVES; ++o) {
    int k = li * OCTAVES + o;
    vec4 m = u_noiseM[k];
    vec4 cc = u_noiseC[k >> 1];
    vec2 c = ((k & 1) == 0) ? cc.xy : cc.zw;
    vec2 u = vec2(m.x * xy.x + m.y * xy.y, m.z * xy.x + m.w * xy.y) + c;
    n += u_noiseAmp[o] * octaveValue(u);
  }
  return n;
}

// ---------------------------------------------------------------- pass 3: cloud slabs (mirrors cpu/reference.ts)
struct LayerHit { float kappa; float alpha; float pathLen; float tMid; float target; };

// Where a view ray crosses one slab: the in-slab intervals within [0, tEnd], the path length and its
// midpoint (the compositing order). Geometry only, no noise, so it is cheap to do for every slab first.
struct SlabGeom { vec2 nearIv; vec2 farIv; float l1; float L; float tMid; };

SlabGeom slabGeom(int li, vec3 dir, float tEnd) {
  SlabGeom sg;
  vec4 g = u_layerGeom[li];
  slabIntervals(u_hCam, dir.z, g.x, g.y, 0.0, tEnd, sg.nearIv, sg.farIv);
  sg.l1 = ivLen(sg.nearIv);
  sg.L = sg.l1 + ivLen(sg.farIv);
  float sMid = 0.5 * sg.L;
  sg.tMid = sMid < sg.l1 ? sg.nearIv.x + sMid : sg.farIv.x + (sMid - sg.l1);
  return sg;
}

// fullPath: sample the whole in-slab path (coverage, target and alpha debug outputs). Otherwise the colour
// path stops once the slab is opaque (optical depth >= TAU_OPAQUE); alpha is the same either way, because
// it counts mask samples only up to that point.
LayerHit layerAlongRay(int li, vec3 dir, SlabGeom sg, bool fullPath) {
  LayerHit r = LayerHit(0.0, 0.0, 0.0, 0.0, 0.0);
  vec4 g = u_layerGeom[li];
  float l1 = sg.l1;
  float L = sg.L;
  if (L <= 0.0) return r;
  r.pathLen = L;
  r.tMid = sg.tMid;
  if (g.w > 0.5) { r.kappa = 1.0; r.target = 1.0; r.alpha = 1.0 - exp(-g.z * L); return r; }
  int n = int(clamp(ceil(L / u_layerStep[li]), float(CLOUD_MIN_SAMPLES), float(CLOUD_MAX_SAMPLES)));
  float tauPerSample = g.z * L / float(n);
  float m = 0.0;
  float mDrawn = -1.0;
  float tgt = 0.0;
  for (int k = 0; k < CLOUD_MAX_SAMPLES; ++k) {
    if (k >= n) break;
    float s = (float(k) + 0.5) / float(n) * L;
    float t = s < l1 ? sg.nearIv.x + s : sg.farIv.x + (s - l1);
    vec3 P = vec3(0.0, 0.0, u_hCam) + t * dir;
    float c = coverAt(li, groundDistKm(P));
    // Threshold first (binary mask at this sample's own cover), then average below.
    m += cloudNoise(li, P.xy) < coverThreshold(c) ? 1.0 : 0.0;
    tgt += c;
    if (mDrawn < 0.0 && m * tauPerSample >= TAU_OPAQUE) {
      mDrawn = m;
      if (!fullPath) break;
    }
  }
  if (mDrawn < 0.0) mDrawn = m;
  r.kappa = m / float(n);
  r.target = tgt / float(n);
  r.alpha = 1.0 - exp(-mDrawn * tauPerSample);
  return r;
}

// Cloud transmittance of the direct beam from a local point towards the Sun (one mask sample per slab).
float sunTransmittance(vec3 Pg) {
  vec3 Q = vec3(Pg.xy, R_EARTH + Pg.z);
  float qlen = length(Q);
  float h0 = (dot(Pg.xy, Pg.xy) + Pg.z * Pg.z + 2.0 * R_EARTH * Pg.z) / (qlen + R_EARTH);
  float mu = dot(Q, u_sunDir) / qlen;
  float T = 1.0;
  for (int li = 0; li < MAX_LAYERS; ++li) {
    if (li >= u_layerCount) break;
    vec4 g = u_layerGeom[li];
    vec2 a;
    vec2 b;
    slabIntervals(h0, mu, g.x, g.y, 0.0, T_INF, a, b);
    float la = ivLen(a);
    float lb = ivLen(b);
    float L = la + lb;
    if (L <= 0.0) continue;
    float tm = la > 0.0 ? a.x + 0.5 * la : b.x + 0.5 * lb;
    float m = 1.0;
    if (g.w < 0.5) {
      vec3 X = Pg + tm * u_sunDir;
      m = cloudNoise(li, X.xy) < coverThreshold(coverAt(li, groundDistKm(X))) ? 1.0 : 0.0;
    }
    T *= 1.0 - m * (1.0 - exp(-g.z * L));
  }
  return T;
}

// ---------------------------------------------------------------- pass 1: sky v0 (placeholder)
// Zenith luminance and day/twilight/night weights come from light.ts per frame; this function only
// shapes them over the dome. All terms are clamped so no input gives NaN, and every term is
// continuous in Sun altitude. TODO(M1.5): Hillaire 2020 transmittance / multiple-scattering / sky-view /
// aerial-perspective LUTs (D-013, PLAN M1.5).
vec3 skyRadiance(vec3 v) {
  float e = max(v.z, 0.0);
  float g = exp(-6.0 * e);
  float a = 0.5;
  float lv = length(v.xy);
  float ls = length(u_sunDir.xy);
  if (lv > 1e-5 && ls > 1e-5) a = 0.5 + 0.5 * dot(v.xy / lv, u_sunDir.xy / ls);
  float a2 = a * a;
  vec3 cDay = mix(${v3(SKY_COLORS.dayZenith)}, ${v3(SKY_COLORS.dayHorizon)}, g) * (1.0 + 1.5 * g);
  vec3 cTw = mix(${v3(SKY_COLORS.twilightZenith)}, mix(${v3(SKY_COLORS.twilightAway)}, ${v3(SKY_COLORS.twilightToward)}, a2), g) * (1.0 + g * (0.5 + 2.5 * a2));
  vec3 cNight = ${v3(SKY_COLORS.night)} * (1.0 + 0.5 * g);
  vec3 L = u_sky.x * (u_sky.y * cDay + u_sky.z * cTw + u_sky.w * cNight);
  float ang = 2.0 * asin(clamp(0.5 * length(v - u_sunDir), 0.0, 1.0));
  L += u_glow * exp(-ang / 0.15);
  L += u_skyMoon * (1.0 + 0.5 * g);
  return L;
}

// ---------------------------------------------------------------- pass 2: Sun and Moon disks
vec3 diskRadiance(vec3 v) {
  float angS = 2.0 * asin(clamp(0.5 * length(v - u_sunDir), 0.0, 1.0));
  float angM = 2.0 * asin(clamp(0.5 * length(v - u_moonDir), 0.0, 1.0));
  float covS = clamp((u_sunRadius - angS) / u_pixelAngle + 0.5, 0.0, 1.0);
  float covM = clamp((u_moonRadius - angM) / u_pixelAngle + 0.5, 0.0, 1.0);
  return covS * u_sunDiskL + covM * u_moonDiskL;
}

// ---------------------------------------------------------------- pass 4: scene light v0
vec3 groundRadiance(vec2 uv, vec3 dir, float tGround) {
  vec3 plate = texture(u_plate, uv).rgb;
  vec3 Pg = vec3(0.0, 0.0, u_hCam) + tGround * dir;
  float Ts = (u_sunUp == 1) ? sunTransmittance(Pg) : 1.0;
  vec3 E = u_eDir * Ts + u_eDif;
  return plate * E / PI;
}

// ---------------------------------------------------------------- pass 6: haze and fog
vec2 g_hazeIv;
vec2 g_fogIv;
vec2 g_decayNear[FOG_DECAY_BANDS];
vec2 g_decayFar[FOG_DECAY_BANDS];

void setupMedia(vec3 dir, float tEnd) {
  g_hazeIv = clipIv(sphereInside(u_hCam, dir.z, u_hazeTop), 0.0, tEnd);
  g_fogIv = vec2(1.0, -1.0);
  for (int k = 0; k < FOG_DECAY_BANDS; ++k) { g_decayNear[k] = vec2(1.0, -1.0); g_decayFar[k] = vec2(1.0, -1.0); }
  if (u_fogBeta > 0.0) {
    g_fogIv = clipIv(sphereInside(u_hCam, dir.z, u_fogTop), 0.0, tEnd);
    for (int k = 0; k < FOG_DECAY_BANDS; ++k) {
      float lo = u_fogTop + float(k) * u_fogDecayH;
      slabIntervals(u_hCam, dir.z, lo, lo + u_fogDecayH, 0.0, tEnd, g_decayNear[k], g_decayFar[k]);
    }
  }
}

// Optical depth (haze, fog) over [ta, tb]: uniform haze in the boundary layer; fog below its top and
// exp(-(k + 0.5)) * beta in band k above it.
vec2 mediaTau(float ta, float tb) {
  float th = u_hazeBeta * overlapLen(g_hazeIv, ta, tb);
  float tf = 0.0;
  if (u_fogBeta > 0.0) {
    tf = overlapLen(g_fogIv, ta, tb);
    for (int k = 0; k < FOG_DECAY_BANDS; ++k) {
      tf += exp(-(float(k) + 0.5)) * (overlapLen(g_decayNear[k], ta, tb) + overlapLen(g_decayFar[k], ta, tb));
    }
    tf *= u_fogBeta;
  }
  return vec2(th, tf);
}

// L = Ls * T + Lair * (1 - T), front to back, with haze and fog airlight mixed by optical depth. Thin
// near-surface extinction scatters like haze (horizon-sky airlight); dense fog gets the multiple-
// scattering airlight (PRD §9.3 "thick fog uses a multiple-scattering airlight approximation").
void composeMedia(float ta, float tb, vec3 lHaze, inout vec3 L, inout float T) {
  vec2 tau = mediaTau(ta, tb);
  float tt = tau.x + tau.y;
  if (tt > 0.0) {
    vec3 air = (tau.x * lHaze + tau.y * mix(lHaze, u_fogAir, u_fogAirW)) / tt;
    float Ts = exp(-tt);
    L += T * (1.0 - Ts) * air;
    T *= Ts;
  }
}

// ---------------------------------------------------------------- pass 8: camera v0
vec3 toneCurve(vec3 x) {
  vec3 hi = 0.8 + 0.2 * (1.0 - exp(-(x - 0.8) / 0.2));
  return mix(x, hi, step(0.8, x));
}

vec3 srgbOetf(vec3 c) {
  vec3 lo = 12.92 * c;
  vec3 hi = 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055;
  return mix(lo, hi, step(0.0031308, c));
}

void main() {
  vec2 frag = gl_FragCoord.xy;
  vec2 ndc = vec2(2.0 * frag.x / u_resolution.x - 1.0, 2.0 * frag.y / u_resolution.y - 1.0);
  vec3 dir = normalize(u_camF + (ndc.x * u_tanHalf.x) * u_camR + (ndc.y * u_tanHalf.y) * u_camU);
  vec2 uv = vec2(frag.x / u_resolution.x, 1.0 - frag.y / u_resolution.y);

  // Ray end: sky rays run to the sea surface or "infinity"; terrain rays to the depth map or nominal depth.
  float skyV = 0.0;
  if (u_hasSkyMask == 1) skyV = texture(u_skyMask, uv).r;
  bool isSky = u_hasSkyMask == 1 && skyV >= 0.5;
  float tEnd;
  if (isSky) {
    tEnd = T_INF;
    if (u_hCam > 0.0) {
      vec2 sea = sphereInside(u_hCam, dir.z, 0.0);
      if (sea.x <= sea.y && sea.x > 0.0) tEnd = sea.x;
    }
  } else {
    tEnd = u_nominalDepth;
    bool haveDepth = false;
    if (u_hasDepth == 1) {
      ivec2 ds = textureSize(u_depth, 0);
      ivec2 dp = clamp(ivec2(uv * vec2(ds)), ivec2(0), ds - 1);
      vec3 d8 = floor(texelFetch(u_depth, dp, 0).rgb * 255.0 + 0.5);
      float dm = d8.r * 65536.0 + d8.g * 256.0 + d8.b;
      if (dm > 0.0 && dm < 16777215.0) { tEnd = dm; haveDepth = true; }
    }
    // No depth: a downward terrain ray ends on the v0 flat ground, capped at the nominal depth.
    if (!haveDepth && dir.z < 0.0) {
      vec2 gnd = sphereInside(u_hCam, dir.z, u_hGround);
      if (gnd.x <= gnd.y && gnd.x >= 0.0) tEnd = min(tEnd, gnd.x);
    }
  }
  if (u_debugMode == 3) { o_color = vec4(dir, tEnd); return; }

  // Pass 3: slabs along the ray. Geometry first, to order the slabs front to back by path midpoint;
  // then the noise, front to back, so the colour path can skip slabs hidden behind opaque nearer ones.
  bool fullPath = u_debugMode == 1 || u_debugMode == 4 || u_debugMode == 5;
  SlabGeom sgs[MAX_LAYERS];
  int order[MAX_LAYERS];
  float kap[MAX_LAYERS];
  float tgt[MAX_LAYERS];
  float alp[MAX_LAYERS];
  int nHit = 0;
  for (int li = 0; li < MAX_LAYERS; ++li) {
    kap[li] = 0.0;
    tgt[li] = 0.0;
    alp[li] = 0.0;
    order[li] = 0;
    if (li >= u_layerCount) continue;
    sgs[li] = slabGeom(li, dir, tEnd);
    if (sgs[li].L <= 0.0) continue;
    int pos = nHit;
    while (pos > 0 && sgs[order[pos - 1]].tMid > sgs[li].tMid) {
      order[pos] = order[pos - 1];
      pos--;
    }
    order[pos] = li;
    nHit++;
  }
  float evT[MAX_LAYERS];
  float evA[MAX_LAYERS];
  vec3 evL[MAX_LAYERS];
  int nEv = 0;
  float tSlabs = 1.0;
  for (int e = 0; e < MAX_LAYERS; ++e) {
    if (e >= nHit) break;
    if (!fullPath && tSlabs < SKIP_TRANSMITTANCE) break;
    int li = order[e];
    LayerHit h = layerAlongRay(li, dir, sgs[li], fullPath);
    kap[li] = h.kappa;
    tgt[li] = h.target;
    alp[li] = h.alpha;
    if (h.alpha <= 0.0) continue;
    evT[nEv] = h.tMid;
    evA[nEv] = h.alpha;
    evL[nEv] = u_layerRadiance[li];
    nEv++;
    tSlabs *= 1.0 - h.alpha;
  }
  if (u_debugMode == 1) { o_color = vec4(kap[0], kap[1], kap[2], kap[3]); return; }
  if (u_debugMode == 4) { o_color = vec4(tgt[0], tgt[1], tgt[2], tgt[3]); return; }
  if (u_debugMode == 5) { o_color = vec4(alp[0], alp[1], alp[2], alp[3]); return; }

  // Passes 1, 2, 4: background radiance (sky, disks, relit plate), blended at mask edges.
  vec3 lSky = skyRadiance(dir) + diskRadiance(dir);
  vec3 lBg = lSky;
  if (!(u_hasSkyMask == 1 && skyV >= 1.0)) {
    vec3 lGround = groundRadiance(uv, dir, isSky ? u_nominalDepth : tEnd);
    lBg = mix(lGround, lSky, skyV);
  }

  // Pass 6 composited with pass 3, front to back.
  setupMedia(dir, tEnd);
  float lxy = length(dir.xy);
  vec3 hz = lxy > 1e-4 ? vec3(dir.xy / lxy, 0.0) : vec3(0.0, 1.0, 0.0);
  vec3 lHaze = skyRadiance(hz) * u_cloudDim;
  vec3 L = vec3(0.0);
  float T = 1.0;
  float tPrev = 0.0;
  for (int e = 0; e < MAX_LAYERS; ++e) {
    if (e >= nEv) break;
    composeMedia(tPrev, evT[e], lHaze, L, T);
    L += T * evA[e] * evL[e];
    T *= 1.0 - evA[e];
    tPrev = evT[e];
  }
  composeMedia(tPrev, tEnd, lHaze, L, T);
  L += T * lBg;

  // Pass 8: camera v0. The hdr debug output is taken before the clamp, so a NaN or a negative radiance
  // reaches the test instead of being hidden by max() (SwiftShader's max(NaN, 0.0) returns 0).
  if (u_debugMode == 2) { o_color = vec4(L * u_exposure, 1.0); return; }
  vec3 x = max(L, vec3(0.0)) * u_exposure;
  o_color = vec4(srgbOetf(clamp(toneCurve(x), 0.0, 1.0)), 1.0);
}
`;
