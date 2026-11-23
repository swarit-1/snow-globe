import { beforeAll, describe, expect, it } from 'vitest';
import { cameraBasis } from '../math/camera.ts';
import { pointHeight } from '../math/shell.ts';
import { SUN_RADIUS_RAD, luminanceY } from '../light.ts';
import {
  AP_GRID,
  AP_SLICES,
  ISOTROPIC_PHASE,
  MULTI_SCATTERING_CELLS,
  MULTI_SCATTERING_LUT_SIZE,
  SKY_VIEW_CELLS,
  SKY_VIEW_LUT_HEIGHT,
  SKY_VIEW_LUT_WIDTH,
  apDepthAt,
  cornetteShanksPhase,
  diskVisibility,
  distanceToTop,
  mediumAt,
  rayleighPhase,
  TRANSMITTANCE_LUT_HEIGHT,
  TRANSMITTANCE_LUT_WIDTH,
  atmosphereParams,
  transmittanceUvToHMu,
  type AtmosphereParams,
  type Rgb,
} from '../atmosphere/model.ts';
import {
  apColumnDirection,
  cellWeight,
  computeAerialPerspective,
  computeBaseLuts,
  computeSkyViewLuts,
  computeTransmittanceLut,
  groundHit,
  integrateScattering,
  lightFromAtmosphere,
  lookupAerialPerspective,
  lookupMultiScattering,
  lookupSkyView,
  lookupTransmittance,
  multiScatteringDirection,
  opticalDepthToTop,
  rayBreakpoints,
  rayEndInAtmosphere,
  shadowCrossings,
  skyIrradiance,
  skyViewRay,
  type AtmosphereBaseLuts,
  type AtmosphereRay,
  type LightInputs,
  type Lut,
} from './atmosphere.ts';

/**
 * Invariants of the Hillaire 2020 tables in the CPU reference (PRD §9.2 pass 1, D-013). Thresholds were
 * declared before these tests ran and come from the output: an 8-bit sRGB level near mid-grey is about
 * 0.4 % of the linear value, so quadrature errors are held to 0.5 %, and convergence to 1 %. The
 * exceptions are the sweeps over the viewpoints' heights and hazes and the aerial-perspective test,
 * whose thresholds were set after an exploratory run (each test says so).
 */
const P = atmosphereParams(0.03e-3, 1640 + 2000);
const H_VIEW = 1650;
const deg = Math.PI / 180;
const sunAt = (altDeg: number, azDeg = 180): [number, number, number] => [Math.sin(azDeg * deg) * Math.cos(altDeg * deg), Math.cos(azDeg * deg) * Math.cos(altDeg * deg), Math.sin(altDeg * deg)];
const CLEAR_INPUTS: LightInputs = {
  solarLux: 128_000,
  tAmb: 1,
  tDirExp: 1,
  tBeamTot: 1,
  moonLux: 0,
  layers: [],
  fogAirWeight: 0,
  sunDiskL: [0, 0, 0],
  moonDiskL: [0, 0, 0],
};

let base: AtmosphereBaseLuts;
beforeAll(() => {
  base = computeBaseLuts(P);
});

/** Clear-sky light for a Sun at altDeg, from fresh sky-view tables. */
function lightAt(p: AtmosphereParams, b: AtmosphereBaseLuts, altDeg: number, hView = H_VIEW) {
  const sun = sunAt(altDeg);
  const sky = computeSkyViewLuts(p, b.transmittance, b.multiScattering, hView, sun[2], -Infinity);
  return { sky, light: lightFromAtmosphere(p, b.transmittance, sky.total, hView, sun, [0, 0, -1], CLEAR_INPUTS) };
}

/**
 * An independent converged reference of integrateScattering for the convergence tests: the ray is cut
 * at its perigee, its haze-top and Earth-shadow crossings and at the distances 100 m x 3^k from its
 * origin, and each piece gets n uniform cells. It shares the integrand (the medium, both tables, the
 * phase functions and the energy-conserving cell) with the production integral but not its cell
 * layout. With MS null it is the multiple-scattering table's isotropic single scattering, with f_ms.
 * 64 cells per piece agree with 512 to 0.02 % on the worst sky-view texels (measured 2026-11-23).
 */
function referenceScattering(p: AtmosphereParams, T: Lut, MS: Lut | null, ray: AtmosphereRay, tA: number, tB: number, n = 64): { L: Rgb; throughput: Rgb; fms: Rgb } {
  const R = p.bottomRadiusM;
  const { h0, mu, muS, nu } = ray;
  const knots = [tA, tB];
  const tP = -(R + h0) * mu;
  if (tP > tA && tP < tB) knots.push(tP);
  knots.push(...rayBreakpoints(h0, mu, [p.hazeTopM], tA, tB), ...shadowCrossings(R, h0, mu, muS, nu, tA, tB));
  for (let g = 100; g < tB; g *= 3) if (g > tA) knots.push(g);
  knots.sort((a, b) => a - b);
  const pR = rayleighPhase(nu);
  const pM = cornetteShanksPhase(p.miePhaseG, nu);
  const L: Rgb = [0, 0, 0];
  const thr: Rgb = [1, 1, 1];
  const fms: Rgb = [0, 0, 0];
  const ts: Rgb = [0, 0, 0];
  const ms: Rgb = [0, 0, 0];
  for (let k = 1; k < knots.length; k++) {
    const a = knots[k - 1]!;
    const b = knots[k]!;
    if (!(b > a)) continue;
    for (let j = 0; j < n; j++) {
      const ta = a + ((b - a) * j) / n;
      const tb = j === n - 1 ? b : a + ((b - a) * (j + 1)) / n;
      const tm = 0.5 * (ta + tb);
      const h = pointHeight(h0, mu, tm);
      const muSt = Math.min(Math.max(((R + h0) * muS + tm * nu) / (R + h), -1), 1);
      const m = mediumAt(p, h, h < p.hazeTopM);
      lookupTransmittance(p, T, h, muSt, ts);
      const vis = diskVisibility(h, muSt, SUN_RADIUS_RAD, R);
      if (MS) lookupMultiScattering(p, MS, h, muSt, ms);
      for (let c = 0; c < 3; c++) {
        const w = cellWeight(m.ext[c]!, tb - ta);
        const src = MS ? vis * ts[c]! * (m.scatRay[c]! * pR + m.scatMie[c]! * pM) + ms[c]! * m.scat[c]! : vis * ts[c]! * m.scat[c]! * ISOTROPIC_PHASE;
        L[c]! += thr[c]! * src * w;
        if (!MS) fms[c]! += thr[c]! * m.scat[c]! * w;
        thr[c]! *= Math.exp(-m.ext[c]! * (tb - ta));
      }
    }
  }
  return { L, throughput: thr, fms };
}

/**
 * Largest relative error, in any channel, of the production sky-view integral (SKY_VIEW_CELLS per piece)
 * against referenceScattering, over every 8th azimuth column and every 2nd row of the table (1,296
 * texels), counting only texels that carry at least 1 % of the brightest one's luminance (what an
 * exposed frame shows).
 */
function skyViewWorst(p: AtmosphereParams, b: AtmosphereBaseLuts, hView: number, altDeg: number): number {
  const muS = Math.sin(altDeg * deg);
  const tables = { T: b.transmittance, MS: b.multiScattering };
  const rows: { ref: Rgb; prod: Rgb }[] = [];
  for (let j = 0; j < SKY_VIEW_LUT_HEIGHT; j += 2) {
    for (let i = 0; i < SKY_VIEW_LUT_WIDTH; i += 8) {
      const ray = skyViewRay(p, hView, muS, (i + 0.5) / SKY_VIEW_LUT_WIDTH, (j + 0.5) / SKY_VIEW_LUT_HEIGHT);
      const tB = rayEndInAtmosphere(p, hView, ray.mu);
      const prod = integrateScattering(p, tables, ray, { tA: 0, tB, cells: SKY_VIEW_CELLS, uniform: false, isotropic: false, shadeTopM: -Infinity }).L;
      rows.push({ ref: referenceScattering(p, b.transmittance, b.multiScattering, ray, 0, tB).L, prod });
    }
  }
  const maxY = Math.max(...rows.map((r) => luminanceY(r.ref)));
  let worst = 0;
  for (const r of rows) {
    if (luminanceY(r.ref) < 0.01 * maxY) continue;
    for (let c = 0; c < 3; c++) worst = Math.max(worst, Math.abs(r.prod[c]! - r.ref[c]!) / r.ref[c]!);
  }
  return worst;
}

/** Psi_ms at (h, muS) as computeMultiScatteringLut forms it, from the production integral or from referenceScattering. */
function psiMs(p: AtmosphereParams, T: Lut, h: number, muS: number, reference: boolean): number[] {
  const sunY = Math.sqrt(Math.max(1 - muS * muS, 0));
  const sL = [0, 0, 0];
  const sF = [0, 0, 0];
  for (let k = 0; k < 64; k++) {
    const d = multiScatteringDirection(k);
    const nu = d.sinPhiSinTheta * sunY + d.mu * muS;
    const tG = groundHit(h, d.mu);
    const hits = Number.isFinite(tG);
    const tB = hits ? tG : distanceToTop(p, h, d.mu);
    const ray = { h0: h, mu: d.mu, muS, nu };
    const r = reference
      ? referenceScattering(p, T, null, ray, 0, tB)
      : integrateScattering(p, { T, MS: null }, ray, { tA: 0, tB, cells: MULTI_SCATTERING_CELLS, uniform: false, isotropic: true, shadeTopM: -Infinity });
    if (hits) {
      const muSg = Math.min(Math.max(((p.bottomRadiusM + h) * muS + tG * nu) / p.bottomRadiusM, -1), 1);
      const tg = lookupTransmittance(p, T, 0, muSg);
      const vis = diskVisibility(0, muSg, SUN_RADIUS_RAD, p.bottomRadiusM);
      for (let c = 0; c < 3; c++) r.L[c]! += r.throughput[c]! * vis * tg[c]! * Math.max(muSg, 0) * (p.groundAlbedo[c]! / Math.PI);
    }
    for (let c = 0; c < 3; c++) {
      sL[c]! += r.L[c]!;
      sF[c]! += r.fms[c]!;
    }
  }
  return [0, 1, 2].map((c) => sL[c]! / 64 / (1 - sF[c]! / 64));
}

describe('transmittance table', () => {
  it('holds values in [0, 1] whose optical depth rises along every row as the ray gets longer and lower', () => {
    const T = base.transmittance;
    for (let j = 0; j < TRANSMITTANCE_LUT_HEIGHT; j++) {
      const prev = [0, 0, 0];
      for (let i = 0; i < TRANSMITTANCE_LUT_WIDTH; i++) {
        for (let c = 0; c < 3; c++) {
          const t = T.data[(j * TRANSMITTANCE_LUT_WIDTH + i) * 4 + c]!;
          expect(t).toBeGreaterThanOrEqual(0);
          expect(t).toBeLessThanOrEqual(1);
          const tau = -Math.log(Math.max(t, 1e-300));
          expect(tau, `row ${j} texel ${i} channel ${c}`).toBeGreaterThanOrEqual(prev[c]! - 1e-12);
          prev[c] = tau;
        }
      }
    }
  });

  it('rises with height towards the zenith and is 1 at the top', () => {
    let prev = -1;
    for (const h of [0, 1000, 5000, 20_000, 60_000, 100_000]) {
      const t = lookupTransmittance(P, base.transmittance, h, 1)[1]!;
      expect(t).toBeGreaterThan(prev);
      prev = t;
    }
    expect(prev).toBeCloseTo(1, 12);
  });

  it('matches exp(-optical depth) integrated with 4096 cells per piece within 0.5 % wherever T > 1e-3', () => {
    let worst = 0;
    for (let j = 0; j < TRANSMITTANCE_LUT_HEIGHT; j += 3) {
      for (let i = 0; i < TRANSMITTANCE_LUT_WIDTH; i += 5) {
        const { h, mu } = transmittanceUvToHMu(P, (i + 0.5) / TRANSMITTANCE_LUT_WIDTH, (j + 0.5) / TRANSMITTANCE_LUT_HEIGHT);
        const tau = opticalDepthToTop(P, h, mu, 4096);
        for (let c = 0; c < 3; c++) {
          const exact = Math.exp(-tau[c]!);
          if (exact > 1e-3) worst = Math.max(worst, Math.abs(base.transmittance.data[(j * TRANSMITTANCE_LUT_WIDTH + i) * 4 + c]! - exact) / exact);
        }
      }
    }
    console.log(`transmittance table vs 4096-cell quadrature: max relative error ${worst.toExponential(2)}`);
    expect(worst).toBeLessThan(5e-3);
  });

  it('includes the haze layer exactly: a horizontal ray at 1,650 m loses exp(-beta * chord) to it', () => {
    const noHaze = atmosphereParams(0, 1640 + 2000);
    const a = opticalDepthToTop(P, 1650, 0, 4096)[1]!;
    const b = opticalDepthToTop(noHaze, 1650, 0, 4096)[1]!;
    const R = P.bottomRadiusM;
    const chord = Math.sqrt((R + 3640) ** 2 - (R + 1650) ** 2); // inside the haze sphere, from the camera outward
    expect(a - b).toBeCloseTo(0.03e-3 * chord, 6);
  });
});

describe('multiple-scattering table', () => {
  it('is finite and non-negative, with f_ms in [0, 0.6]: the series 1 / (1 - f_ms) converges', () => {
    const n = MULTI_SCATTERING_LUT_SIZE;
    let fmax = 0;
    for (let k = 0; k < n * n; k++) {
      for (let c = 0; c < 3; c++) {
        const v = base.multiScattering.data[k * 4 + c]!;
        expect(Number.isFinite(v)).toBe(true);
        expect(v).toBeGreaterThanOrEqual(0);
      }
      fmax = Math.max(fmax, base.multiScattering.data[k * 4 + 3]!);
    }
    console.log(`multiple scattering: max f_ms ${fmax.toFixed(4)}`);
    expect(fmax).toBeGreaterThan(0);
    expect(fmax).toBeLessThanOrEqual(0.6);
  });

  it('is brighter by day than at night, at the ground', () => {
    const n = MULTI_SCATTERING_LUT_SIZE;
    const at = (i: number) => luminanceY([base.multiScattering.data[i * 4]!, base.multiScattering.data[i * 4 + 1]!, base.multiScattering.data[i * 4 + 2]!]);
    expect(at(n - 1)).toBeGreaterThan(100 * at(0)); // Sun at the zenith vs at the nadir (row 0: the ground)
  });
});

describe('sky-view table', () => {
  it('is finite and non-negative, and the part below the shade top never exceeds the total', () => {
    const sun = sunAt(22);
    const sky = computeSkyViewLuts(P, base.transmittance, base.multiScattering, H_VIEW, sun[2], 9500);
    for (let k = 0; k < sky.total.data.length; k++) {
      if (k % 4 === 3) continue;
      expect(Number.isFinite(sky.total.data[k]!)).toBe(true);
      expect(sky.total.data[k]!).toBeGreaterThanOrEqual(0);
      expect(sky.below.data[k]!).toBeGreaterThanOrEqual(0);
      expect(sky.below.data[k]!).toBeLessThanOrEqual(sky.total.data[k]! * (1 + 1e-12));
    }
  });

  const DAY_SUNS = [45, 10, 2, 0];
  const DUSK_SUNS = [-2, -4, -6, -9, -12];

  it('has converged by day, at sunset and through twilight at the golden camera (1,650 m, haze 0.03 per km)', () => {
    // Against the independent reference, over texels that carry >= 1 % of the brightest one (what an
    // exposed frame shows): within 1 % for Suns at or above the horizon, within 3 % from -12 deg
    // (nautical dusk) up to it. The first layout, cells crowded at the camera only, missed by up to
    // 12.7 % at sunset and 9.3 % at -12 deg; README "Convergence" has the measured tables.
    const day = DAY_SUNS.map((a) => [a, skyViewWorst(P, base, H_VIEW, a)] as const);
    const dusk = DUSK_SUNS.map((a) => [a, skyViewWorst(P, base, H_VIEW, a)] as const);
    console.log(`sky-view convergence, 1,650 m, haze 0.03 per km: ${[...day, ...dusk].map(([a, w]) => `${a} deg ${(100 * w).toFixed(2)} %`).join(', ')}`);
    for (const [, w] of day) expect(w).toBeLessThan(0.01);
    for (const [, w] of dusk) expect(w).toBeLessThan(0.03);
  });

  it('has converged at the viewpoints\' camera heights (150-2,150 m) and hazes 0.01-0.3 per km', () => {
    // Thresholds set after an exploratory sweep (2026-11-23) found at most 1.30 % by day and 3.23 % in
    // twilight with this layout. The layout before it (cells crowded at each piece's lower end only)
    // missed by up to 10.5 % by day and 13.1 % in twilight here: in hazy air most of the light comes
    // from the first kilometres from the camera, and at a low Sun from the sunlit top of the haze layer.
    let dayMax = 0;
    let duskMax = 0;
    const lines: string[] = [];
    for (const hCam of [150, 808, 1650, 2150]) {
      for (const haze of [0.01, 0.03, 0.1, 0.3]) {
        const p = atmosphereParams(haze * 1e-3, hCam - 10 + 2000);
        const b = computeBaseLuts(p);
        const d = Math.max(...DAY_SUNS.map((a) => skyViewWorst(p, b, hCam, a)));
        const n = Math.max(...DUSK_SUNS.map((a) => skyViewWorst(p, b, hCam, a)));
        dayMax = Math.max(dayMax, d);
        duskMax = Math.max(duskMax, n);
        lines.push(`${hCam} m / ${haze}: ${(100 * d).toFixed(2)} / ${(100 * n).toFixed(2)} %`);
      }
    }
    console.log(`sky-view convergence (day / twilight): ${lines.join('; ')}; max ${(100 * dayMax).toFixed(2)} / ${(100 * duskMax).toFixed(2)} %`);
    expect(dayMax).toBeLessThan(0.015);
    expect(duskMax).toBeLessThan(0.035);
  });

  it('multiple scattering has converged: Psi_ms within 2 % of the reference at any height and Sun, hazes 0.03-1 per km', () => {
    // The layout before this one was off by up to 11.6 % at 0.3 per km and 16 % at 1 per km (50 km, -4 deg).
    let worst = 0;
    let where = '';
    for (const hGround of [140, 1640]) {
      for (const haze of [0.03, 0.1, 0.3, 1]) {
        const p = atmosphereParams(haze * 1e-3, hGround + 2000);
        const T = computeTransmittanceLut(p);
        for (const h of [10, hGround + 10, 6500, 20_000, 50_000]) {
          for (const alt of [...DAY_SUNS, ...DUSK_SUNS]) {
            const muS = Math.sin(alt * deg);
            const a = psiMs(p, T, h, muS, false);
            const r = psiMs(p, T, h, muS, true);
            for (let c = 0; c < 3; c++) {
              const e = Math.abs(a[c]! - r[c]!) / r[c]!;
              if (e > worst) {
                worst = e;
                where = `ground ${hGround} m, haze ${haze} per km, h ${h} m, Sun ${alt} deg`;
              }
            }
          }
        }
      }
    }
    console.log(`multiple-scattering convergence: max ${(100 * worst).toFixed(2)} % against the reference (${where})`);
    expect(worst).toBeLessThan(0.02);
  });

  it('aerial perspective: measured accuracy of the stored slices and of the lookups between them', () => {
    // The golden camera (1,650 m, heading 30 deg, pitch 3 deg, hfov 60 deg), haze 0.03 per km, the Sun
    // towards the view (azimuth 30 deg). Every froxel column, every slice end (the stored values) and every
    // slice middle (the lookup's interpolation in the logarithmic slice coordinate), at points up to 12 km
    // high and before the ray's end, against referenceScattering. In-scatter S: relative error where S is at
    // least 1e-3 of the largest; transmittance T: absolute error. Thresholds set after an exploratory run
    // (2026-11-23); they guard the measured accuracy, which README "Aerial perspective" reports.
    const basis = cameraBasis({ width: 320, height: 180, heading_deg: 30, pitch_deg: 3, roll_deg: 0, hfov_deg: 60 });
    const W = AP_GRID * AP_SLICES;
    const res: { alt: number; endS: number; midS: number; midS99: number; endT: number; midT: number }[] = [];
    for (const alt of [22, 2, 0, -2, -4]) {
      const sun = sunAt(alt, 30);
      const ap = computeAerialPerspective(P, base.transmittance, base.multiScattering, basis, H_VIEW, sun, -Infinity, 1);
      const samples: { mid: boolean; sRel: number; tAbs: number; y: number }[] = [];
      for (let j = 0; j < AP_GRID; j += 2) {
        for (let i = 0; i < AP_GRID; i += 2) {
          const dir = apColumnDirection(basis, i, j);
          const ray: AtmosphereRay = { h0: H_VIEW, mu: dir[2], muS: sun[2], nu: dir[0] * sun[0] + dir[1] * sun[1] + dir[2] * sun[2] };
          const tEnd = rayEndInAtmosphere(P, H_VIEW, dir[2]);
          for (let k = 1; k <= AP_SLICES; k++) {
            for (const mid of [false, true]) {
              const t = apDepthAt((mid ? k - 0.5 : k) / AP_SLICES);
              if (t >= tEnd || pointHeight(H_VIEW, dir[2], t) > 12_000) continue;
              const ref = referenceScattering(P, base.transmittance, base.multiScattering, ray, 0, t);
              let S: Rgb;
              let T: Rgb;
              if (mid) [S, T] = lookupAerialPerspective(ap.scatter, ap.transmittance, (i + 0.5) / AP_GRID, (j + 0.5) / AP_GRID, t);
              else {
                const o = (j * W + (k - 1) * AP_GRID + i) * 4;
                S = [ap.scatter.data[o]!, ap.scatter.data[o + 1]!, ap.scatter.data[o + 2]!];
                T = [ap.transmittance.data[o]!, ap.transmittance.data[o + 1]!, ap.transmittance.data[o + 2]!];
              }
              let sRel = 0;
              let tAbs = 0;
              for (let c = 0; c < 3; c++) {
                sRel = Math.max(sRel, Math.abs(S[c]! - ref.L[c]!) / ref.L[c]!);
                tAbs = Math.max(tAbs, Math.abs(T[c]! - ref.throughput[c]!));
              }
              samples.push({ mid, sRel, tAbs, y: luminanceY(ref.L) });
            }
          }
        }
      }
      const maxY = Math.max(...samples.map((s) => s.y));
      const rel = (mid: boolean) => samples.filter((s) => s.mid === mid && s.y >= 1e-3 * maxY).map((s) => s.sRel).sort((a, b) => a - b);
      const ends = rel(false);
      const mids = rel(true);
      res.push({
        alt,
        endS: ends[ends.length - 1]!,
        midS: mids[mids.length - 1]!,
        midS99: mids[Math.floor(0.99 * (mids.length - 1))]!,
        endT: Math.max(...samples.filter((s) => !s.mid).map((s) => s.tAbs)),
        midT: Math.max(...samples.filter((s) => s.mid).map((s) => s.tAbs)),
      });
    }
    const pc = (x: number) => `${(100 * x).toFixed(2)} %`;
    console.log(`aerial perspective (Sun towards the view): ${res.map((r) => `Sun ${r.alt} deg: slice ends S ${pc(r.endS)}, T ${r.endT.toExponential(1)}; lookups S ${pc(r.midS)} (p99 ${pc(r.midS99)}), T ${r.midT.toExponential(1)}`).join('; ')}`);
    for (const r of res) {
      expect(r.endS).toBeLessThan(r.alt >= 2 ? 0.005 : 0.1);
      expect(r.endT).toBeLessThan(1e-3);
      expect(r.midS).toBeLessThan(r.alt >= 2 ? 0.12 : 0.45);
      expect(r.midT).toBeLessThan(0.03);
    }
  });

  it('is symmetric about the Sun vertical plane: mirrored directions read the same radiance', () => {
    const sun = sunAt(15, 120);
    const sky = computeSkyViewLuts(P, base.transmittance, base.multiScattering, H_VIEW, sun[2], -Infinity);
    // reflect across the vertical plane through the Sun (azimuth 120 deg)
    const nx = Math.cos(120 * deg);
    const ny = -Math.sin(120 * deg);
    let worst = 0;
    for (let k = 0; k < 400; k++) {
      const el = -20 + 110 * ((k * 0.6180339887) % 1);
      const az = 360 * ((k * 0.7548776662) % 1);
      const d: [number, number, number] = [Math.sin(az * deg) * Math.cos(el * deg), Math.cos(az * deg) * Math.cos(el * deg), Math.sin(el * deg)];
      const dot = d[0] * nx + d[1] * ny;
      const m: [number, number, number] = [d[0] - 2 * dot * nx, d[1] - 2 * dot * ny, d[2]];
      const [a] = lookupSkyView(P, sky.total, sky.below, H_VIEW, d, sun);
      const [b] = lookupSkyView(P, sky.total, sky.below, H_VIEW, m, sun);
      for (let c = 0; c < 3; c++) worst = Math.max(worst, Math.abs(a[c]! - b[c]!) / a[c]!);
    }
    expect(worst).toBeLessThan(1e-9);
  });

  it('aerial perspective is symmetric about the Sun vertical plane: mirrored froxel columns hold the same values', () => {
    const sun = sunAt(10, 75);
    const basis = cameraBasis({ width: 320, height: 180, heading_deg: 75, pitch_deg: 2, roll_deg: 0, hfov_deg: 60 });
    const ap = computeAerialPerspective(P, base.transmittance, base.multiScattering, basis, H_VIEW, sun, -Infinity, 1);
    let worst = 0;
    const W = AP_GRID * AP_SLICES;
    for (let j = 0; j < AP_GRID; j++) {
      for (let k = 0; k < AP_SLICES; k++) {
        for (let i = 0; i < AP_GRID / 2; i++) {
          const a = (j * W + k * AP_GRID + i) * 4;
          const b = (j * W + k * AP_GRID + (AP_GRID - 1 - i)) * 4;
          for (let c = 0; c < 3; c++) {
            const sa = ap.scatter.data[a + c]!;
            const sb = ap.scatter.data[b + c]!;
            if (sa > 0) worst = Math.max(worst, Math.abs(sa - sb) / sa);
            worst = Math.max(worst, Math.abs(ap.transmittance.data[a + c]! - ap.transmittance.data[b + c]!));
          }
        }
      }
    }
    expect(worst).toBeLessThan(1e-9);
  });
});

describe('energy and plausibility of the light', () => {
  it('conserves energy: a non-absorbing atmosphere over a black ground delivers at most the incident flux to the ground', () => {
    const q: AtmosphereParams = { ...P, mieExtinction: P.mieScattering, ozoneAbsorption: [0, 0, 0], hazeExtinction: P.hazeScattering, groundAlbedo: [0, 0, 0] };
    const b = computeBaseLuts(q);
    for (const alt of [10, 30, 60, 89]) {
      const { light } = lightAt(q, b, alt, 10);
      const mu = Math.sin(alt * deg);
      const ratio = [0, 1, 2].map((c) => (light.eDir[c]! + light.eSky[c]!) / (128_000 * mu));
      console.log(`non-absorbing, black ground, Sun ${alt} deg: (E_dir + E_sky) / (E0 mu) = ${ratio.map((x) => x.toFixed(4)).join(' ')}`);
      for (const r of ratio) {
        expect(r).toBeLessThanOrEqual(1);
        if (alt >= 30) expect(r).toBeGreaterThan(0.5);
      }
    }
  });

  it('with ground albedo 0.3 and absorption, never more than E0 mu / (1 - albedo) reaches the ground', () => {
    for (const alt of [5, 22, 60]) {
      const { light } = lightAt(P, base, alt);
      const mu = Math.sin(alt * deg);
      for (let c = 0; c < 3; c++) expect(light.eDir[c]! + light.eSky[c]!).toBeLessThanOrEqual((128_000 * mu) / (1 - 0.3));
    }
  });

  it('zenith sky near the CIE clear-sky zenith luminance by day, and dimming through twilight like typical twilight skies', () => {
    const zenith = (alt: number) => {
      const { sky } = lightAt(P, base, alt);
      const [L] = lookupSkyView(P, sky.total, sky.below, H_VIEW, [0, 0, 1], sunAt(alt));
      return 128_000 * luminanceY(L);
    };
    // CIE clear sky (turbidity 2.5): L_z = (1.376 T - 1.81) tan(alt) + 0.38 kcd/m^2. Plausibility only: within 1.5x.
    for (const alt of [22, 60]) {
      const cie = 1000 * ((1.376 * 2.5 - 1.81) * Math.tan(alt * deg) + 0.38);
      const z = zenith(alt);
      console.log(`zenith luminance, Sun ${alt} deg: ${z.toFixed(0)} cd/m^2 (CIE clear sky ${cie.toFixed(0)})`);
      expect(z / cie).toBeGreaterThan(1 / 1.5);
      expect(z / cie).toBeLessThan(1.5);
    }
    // Twilight: civil (-6) ~1 cd/m^2, nautical (-12) ~5e-3 cd/m^2 (about 18-19 mag/arcsec^2), within 4x. These
    // are the removed v0 placeholder table's literature-typical values, not measurements made by this
    // project, and they have not been checked against a published source (no reference lookup offline).
    const civil = zenith(-6);
    const nautical = zenith(-12);
    console.log(`zenith luminance at -6 deg ${civil.toExponential(2)}, at -12 deg ${nautical.toExponential(2)} cd/m^2`);
    expect(civil).toBeGreaterThan(1 / 4);
    expect(civil).toBeLessThan(4);
    expect(nautical).toBeGreaterThan(5e-3 / 4);
    expect(nautical).toBeLessThan(5e-3 * 4);
    expect(zenith(45)).toBeGreaterThan(zenith(0));
    expect(zenith(0)).toBeGreaterThan(civil);
    expect(civil).toBeGreaterThan(nautical);
    expect(nautical).toBeGreaterThan(zenith(-18));
  });

  it('reddens the low Sun and keeps the light finite and continuous through sunset', () => {
    const lo = lightAt(P, base, 2).light.eDir;
    const hi = lightAt(P, base, 60).light.eDir;
    expect(lo[2] / lo[0]).toBeLessThan(0.5 * (hi[2] / hi[0]));
    // Around the Sun's setting below the 1,650 m horizon (-1.30 deg): the largest step of E_dir + E_sky
    // over 0.1 deg shrinks about tenfold when the step does (a jump would not shrink).
    const total = (alt: number) => {
      const { light } = lightAt(P, base, alt);
      return luminanceY(light.eDir) + luminanceY(light.eSky);
    };
    const maxStep = (h: number) => {
      let worst = 0;
      let prev = total(-1.35);
      for (let k = 1; k <= 10; k++) {
        const v = total(-1.35 + k * h);
        expect(Number.isFinite(v)).toBe(true);
        worst = Math.max(worst, Math.abs(v - prev));
        prev = v;
      }
      return worst;
    };
    const coarse = maxStep(0.02);
    const fine = maxStep(0.002);
    console.log(`sunset continuity: largest step ${coarse.toExponential(3)} lux per 0.02 deg, ${fine.toExponential(3)} per 0.002 deg`);
    expect(coarse / fine).toBeGreaterThan(5);
  });

  it('the irradiance quadrature integrates a uniform sky to pi times its radiance', () => {
    const uniform = { width: 192, height: 108, data: new Float64Array(192 * 108 * 4).fill(0.25) };
    const e = skyIrradiance(P, uniform, H_VIEW);
    for (const v of e) expect(v).toBeCloseTo(0.25 * Math.PI, 12);
  });
});
