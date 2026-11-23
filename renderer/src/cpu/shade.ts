/**
 * CPU reference of the main pass's colour path (PRD §9.2 passes 1-4, 6 and 8), in float64: the exposed
 * linear radiance of every pixel, before the tone curve, exactly as the shader's 'hdr' debug output
 * computes it (webgl/shader.ts main()).
 *
 * Per pixel: the ray and its end (cpu/reference.ts) -> the cloud slabs' opacities along it, front to
 * back, with the shader's skip of slabs behind opaque nearer ones -> the atmosphere's in-scatter and
 * transmittance between the camera, each slab and the background from the aerial-perspective volume
 * (and the sky-view table for the sky), with the near-surface fog mixed in -> the background: the
 * sky with the Sun and Moon disks, or the plate relit by sunlight through the atmosphere to the
 * terrain point (with the cloud shadow) and the diffuse light of the light pass.
 *
 * Inputs are sampled as the shader samples them only when they are the frame's size (the tests use
 * output-sized plates, masks and depth maps): the plate is then read at texel centres, where the GPU's
 * filtering returns the texel itself.
 */
import { MOON_RADIUS_RAD, SUN_RADIUS_RAD } from '../light.ts';
import { pixelRay } from '../math/camera.ts';
import { CLOUD_SKIP_TRANSMITTANCE, EARTH_RADIUS_M, FOG_DECAY_BANDS, T_INFINITE_M } from '../math/constants.ts';
import { coverAt } from '../math/cover.ts';
import { coverThreshold } from '../math/coverage.ts';
import { localPointGroundDistance } from '../math/geodesy.ts';
import { cloudNoise, lookupFromArray, makeLattice, type LatticeLookup } from '../math/noise.ts';
import { intervalLength, overlapLength, slabIntervals, sphereInside, type Interval } from '../math/shell.ts';
import type { Vec3 } from '../math/vec3.ts';
import { ATMOSPHERE_MIN_HEIGHT_M, diskVisibility } from '../atmosphere/model.ts';
import type { FrameParams } from '../frame.ts';
import { groundHit, lookupAerialPerspective, lookupSkyView, lookupTransmittance, type FrameAtmosphereLuts } from './atmosphere.ts';
import { decodeDepth, pixelRayEnd, rayLayerCoverage, seaHit, type RawImage } from './reference.ts';

type V3 = [number, number, number];

const srgbToLinear = (v: number): number => {
  const c = v / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
};

/** In-scattered radiance (per unit solar illuminance) to the end of the atmosphere, dimmed below the shade top (the shader's skyInScatter). */
export function skyInScatter(frame: FrameParams, atm: FrameAtmosphereLuts, dir: Vec3): V3 {
  const [tot, bel] = lookupSkyView(frame.atmosphere.params, atm.skyView, atm.skyViewBelow, frame.atmosphere.viewHeightM, dir, frame.sunDir);
  const k = 1 - atm.light.cloudDim;
  return [tot[0] - k * bel[0], tot[1] - k * bel[1], tot[2] - k * bel[2]];
}

/** The Sun's and the Moon's anti-aliased disks above the atmosphere (the shader's diskRadiance). */
export function diskRadiance(frame: FrameParams, dir: Vec3): V3 {
  const ang = (a: Vec3): number => {
    const d = Math.hypot(dir[0] - a[0], dir[1] - a[1], dir[2] - a[2]);
    return 2 * Math.asin(Math.min(Math.max(0.5 * d, 0), 1));
  };
  const covS = Math.min(Math.max((SUN_RADIUS_RAD - ang(frame.sunDir)) / frame.pixelAngle + 0.5, 0), 1);
  const covM = Math.min(Math.max((MOON_RADIUS_RAD - ang(frame.moonDir)) / frame.pixelAngle + 0.5, 0), 1);
  const s = frame.light.sunDiskL;
  const m = frame.light.moonDiskL;
  return [covS * s[0] + covM * m[0], covS * s[1] + covM * m[1], covS * s[2] + covM * m[2]];
}

/** Cloud transmittance of the direct beam from a local point towards the Sun, one mask sample per slab (the shader's sunTransmittance). */
export function sunTransmittance(frame: FrameParams, lookup: LatticeLookup, Pg: Vec3): number {
  const R = EARTH_RADIUS_M;
  const Q: Vec3 = [Pg[0], Pg[1], R + Pg[2]];
  const qlen = Math.hypot(Q[0], Q[1], Q[2]);
  const h0 = (Pg[0] * Pg[0] + Pg[1] * Pg[1] + Pg[2] * Pg[2] + 2 * R * Pg[2]) / (qlen + R);
  const s = frame.sunDir;
  const mu = (Q[0] * s[0] + Q[1] * s[1] + Q[2] * s[2]) / qlen;
  let T = 1;
  for (const layer of frame.layers) {
    const [a, b] = slabIntervals(h0, mu, layer.baseM, layer.topM, 0, T_INFINITE_M);
    const la = intervalLength(a);
    const lb = intervalLength(b);
    const L = la + lb;
    if (L <= 0) continue;
    const tm = la > 0 ? a.lo + 0.5 * la : b.lo + 0.5 * lb;
    let m = 1;
    if (!layer.solid) {
      const X: Vec3 = [Pg[0] + tm * s[0], Pg[1] + tm * s[1], Pg[2] + tm * s[2]];
      const cover = coverAt(layer.profile, localPointGroundDistance(X) / 1000);
      m = cloudNoise(lookup, layer.transforms, X[0], X[1]) < coverThreshold(cover) ? 1 : 0;
    }
    T *= 1 - m * (1 - Math.exp(-layer.betaPerM * L));
  }
  return T;
}

/** The plate relit at the terrain point at distance tGround (the shader's groundRadiance); plateLinear is the plate's linear colour there. */
export function groundRadiance(frame: FrameParams, atm: FrameAtmosphereLuts, lookup: LatticeLookup, plateLinear: V3, dir: Vec3, tGround: number): V3 {
  const R = EARTH_RADIUS_M;
  const Pg: Vec3 = [tGround * dir[0], tGround * dir[1], frame.hCam + tGround * dir[2]];
  const Q: Vec3 = [Pg[0], Pg[1], R + Pg[2]];
  const qlen = Math.hypot(Q[0], Q[1], Q[2]);
  const hP = (Pg[0] * Pg[0] + Pg[1] * Pg[1] + Pg[2] * Pg[2] + 2 * R * Pg[2]) / (qlen + R);
  const s = frame.sunDir;
  const muP = (Q[0] * s[0] + Q[1] * s[1] + Q[2] * s[2]) / qlen;
  const p = frame.atmosphere.params;
  const hA = Math.min(Math.max(hP, ATMOSPHERE_MIN_HEIGHT_M), p.topRadiusM - p.bottomRadiusM);
  const tr = lookupTransmittance(p, atm.transmittance, hA, muP);
  const k = frame.light.solarLux * diskVisibility(hA, muP, SUN_RADIUS_RAD, R) * Math.max(muP, 0);
  const eDir: V3 = [k * tr[0], k * tr[1], k * tr[2]];
  const Ts = eDir[0] > 0 || eDir[1] > 0 || eDir[2] > 0 ? sunTransmittance(frame, lookup, Pg) : 1;
  const dif = atm.light.eDif;
  return [0, 1, 2].map((c) => (plateLinear[c]! * (eDir[c]! * Ts + dif[c]!)) / Math.PI) as V3;
}

interface Ap {
  S: V3;
  T: V3;
}

/** The fog along one ray (the shader's setupFog, fogTau). */
class RayFog {
  readonly env: Interval;
  private readonly frame: FrameParams;
  private readonly iv: Interval;
  private readonly near: Interval[] = [];
  private readonly far: Interval[] = [];
  constructor(frame: FrameParams, dir: Vec3) {
    this.frame = frame;
    const f = frame.fog;
    const empty = { lo: 1, hi: -1 };
    if (!(f.betaPerM > 0)) {
      this.iv = empty;
      this.env = empty;
      return;
    }
    const clip = (iv: Interval): Interval => ({ lo: Math.max(iv.lo, 0), hi: Math.min(iv.hi, T_INFINITE_M) });
    this.iv = clip(sphereInside(frame.hCam, dir[2], f.topM));
    for (let k = 0; k < FOG_DECAY_BANDS; k++) {
      const lo = f.topM + k * f.decayScaleM;
      const [a, b] = slabIntervals(frame.hCam, dir[2], lo, lo + f.decayScaleM, 0, T_INFINITE_M);
      this.near.push(a);
      this.far.push(b);
    }
    this.env = clip(sphereInside(frame.hCam, dir[2], f.topM + FOG_DECAY_BANDS * f.decayScaleM));
  }
  tau(ta: number, tb: number): number {
    if (!(this.frame.fog.betaPerM > 0)) return 0;
    let tf = overlapLength(this.iv, ta, tb);
    for (let k = 0; k < FOG_DECAY_BANDS; k++) tf += Math.exp(-(k + 0.5)) * (overlapLength(this.near[k]!, ta, tb) + overlapLength(this.far[k]!, ta, tb));
    return tf * this.frame.fog.betaPerM;
  }
}

/** Exposed linear radiance (before the tone curve) of every pixel, RGBA, top row first: the shader's 'hdr' debug output. */
export function cpuShade(frame: FrameParams, atm: FrameAtmosphereLuts, inputs: { plate: RawImage; skyMask?: RawImage | null; depth?: RawImage | null }): { width: number; height: number; data: Float64Array } {
  const { width: W, height: H } = frame;
  for (const img of [inputs.plate, inputs.skyMask, inputs.depth]) {
    if (img && (img.width !== W || img.height !== H)) throw new Error('cpuShade: inputs must match the frame size');
  }
  const lookup = lookupFromArray(makeLattice(frame.seedU32));
  const p = frame.atmosphere.params;
  const hView = frame.atmosphere.viewHeightM;
  const E0 = frame.light.solarLux;
  const light = atm.light;
  const out = new Float64Array(W * H * 4);
  for (let j = 0; j < H; j++) {
    for (let i = 0; i < W; i++) {
      const idx = j * W + i;
      const dir = pixelRay(frame.basis, i + 0.5, j + 0.5);
      const fx = (i + 0.5) / W;
      const fy = (H - j - 0.5) / H;
      const skyV = inputs.skyMask ? inputs.skyMask.data[idx * 4]! / 255 : 0;
      const depthM = inputs.depth ? decodeDepth(inputs.depth.data[idx * 4]!, inputs.depth.data[idx * 4 + 1]!, inputs.depth.data[idx * 4 + 2]!) : null;
      const { tEnd, isSky } = pixelRayEnd(frame, dir, inputs.skyMask ? skyV : null, depthM);
      const tSkyEnd = seaHit(frame.hCam, dir);

      // Slabs, front to back by path midpoint, skipping those behind opaque nearer ones (colour path).
      const hits = frame.layers
        .map((layer, li) => ({ li, s: rayLayerCoverage(frame, layer, lookup, dir, tEnd) }))
        .filter((h) => h.s.pathLength > 0)
        .sort((a, b) => a.s.tMid - b.s.tMid);
      const events: { t: number; a: number; L: V3 }[] = [];
      let tSlabs = 1;
      for (const h of hits) {
        if (tSlabs < CLOUD_SKIP_TRANSMITTANCE) break;
        if (h.s.alpha <= 0) continue;
        events.push({ t: h.s.tMid, a: h.s.alpha, L: light.layerRadiance[h.li]! as V3 });
        tSlabs *= 1 - h.s.alpha;
      }

      const fog = new RayFog(frame, dir);
      let fogSrc: V3 = [0, 0, 0];
      if (frame.fog.betaPerM > 0) {
        const lxy = Math.hypot(dir[0], dir[1]);
        const hz: Vec3 = lxy > 1e-4 ? [dir[0] / lxy, dir[1] / lxy, 0] : [0, 1, 0];
        const sk = skyInScatter(frame, atm, hz);
        const w = frame.light.fogAirWeight;
        fogSrc = [0, 1, 2].map((c) => E0 * sk[c]! + (light.fogAir[c]! - E0 * sk[c]!) * w) as V3;
      }
      const apAt = (t: number): Ap => {
        const [S, T] = lookupAerialPerspective(atm.apScatter, atm.apTransmittance, fx, fy, t);
        return { S: S as V3, T: T as V3 };
      };
      // One piece of [ta, tb] (the shader's addPiece); returns the radiance added and the new fog transmittance.
      const addPiece = (ta: number, tb: number, A: Ap, B: Ap, L: V3, Tf: number): number => {
        const dS = [0, 1, 2].map((c) => Math.max(B.S[c]! - A.S[c]!, 0) * E0);
        const tauF = fog.tau(ta, tb);
        if (tauF <= 0) {
          for (let c = 0; c < 3; c++) L[c] += Tf * dS[c]!;
          return Tf;
        }
        const Ts = Math.exp(-tauF);
        const avg = tauF < 1e-3 ? 1 - 0.5 * tauF : (1 - Ts) / tauF;
        for (let c = 0; c < 3; c++) L[c] += Tf * (dS[c]! * avg + A.T[c]! * fogSrc[c]! * (1 - Ts));
        return Tf * Ts;
      };
      const addSegment = (ta: number, tb: number, A: Ap, B: Ap, L: V3, Tf: number): number => {
        if (frame.fog.betaPerM > 0 && fog.env.lo <= fog.env.hi) {
          for (const sp of [fog.env.lo, fog.env.hi]) {
            if (sp > ta && sp < tb) {
              const M = apAt(sp);
              Tf = addPiece(ta, sp, A, M, L, Tf);
              ta = sp;
              A = M;
            }
          }
        }
        return addPiece(ta, tb, A, B, L, Tf);
      };

      const L: V3 = [0, 0, 0];
      let Tc = 1;
      let Tf = 1;
      let prev: Ap = { S: [0, 0, 0], T: [1, 1, 1] };
      let tPrev = 0;
      for (const e of events) {
        const cur = apAt(e.t);
        const seg: V3 = [0, 0, 0];
        Tf = addSegment(tPrev, e.t, prev, cur, seg, Tf);
        for (let c = 0; c < 3; c++) L[c] += Tc * seg[c]! + Tc * Tf * cur.T[c]! * e.a * e.L[c]!;
        Tc *= 1 - e.a;
        prev = cur;
        tPrev = e.t;
      }
      const wSky = inputs.skyMask ? skyV : 0;
      const tail: V3 = [0, 0, 0];
      if (wSky > 0) {
        const lt: V3 = [0, 0, 0];
        const Tft = addSegment(tPrev, Math.max(tSkyEnd, tPrev), prev, { S: skyInScatter(frame, atm, dir), T: [0, 0, 0] }, lt, Tf);
        if (!Number.isFinite(groundHit(hView, dir[2]))) {
          const tr = lookupTransmittance(p, atm.transmittance, hView, dir[2]);
          const disk = diskRadiance(frame, dir);
          for (let c = 0; c < 3; c++) lt[c] += Tft * (tr[c]! * disk[c]! + light.skyBackground[c]!);
        }
        for (let c = 0; c < 3; c++) tail[c] += wSky * lt[c]!;
      }
      if (wSky < 1) {
        const tT = isSky ? frame.nominalDepthM : tEnd;
        const lt: V3 = [0, 0, 0];
        const endT = apAt(tT);
        const Tft = addSegment(tPrev, Math.max(tT, tPrev), prev, endT, lt, Tf);
        const px = inputs.plate.data;
        const plate: V3 = [srgbToLinear(px[idx * 4]!), srgbToLinear(px[idx * 4 + 1]!), srgbToLinear(px[idx * 4 + 2]!)];
        const g = groundRadiance(frame, atm, lookup, plate, dir, tT);
        for (let c = 0; c < 3; c++) tail[c] += (1 - wSky) * (lt[c]! + Tft * endT.T[c]! * g[c]!);
      }
      const o = idx * 4;
      for (let c = 0; c < 3; c++) out[o + c] = (L[c]! + Tc * tail[c]!) * light.exposure;
      out[o + 3] = 1;
    }
  }
  return { width: W, height: H, data: out };
}
