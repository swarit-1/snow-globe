import { describe, expect, it } from 'vitest';
import { EARTH_RADIUS_M } from '../math/constants.ts';
import { sphereInside } from '../math/shell.ts';
import {
  AP_FAR_M,
  AP_NEAR_M,
  AP_SLICES,
  MIE_EXTINCTION,
  MIE_PHASE_G,
  MIE_SCATTERING,
  MULTI_SCATTERING_LUT_SIZE,
  RAYLEIGH_SCATTERING,
  SKY_VIEW_LUT_HEIGHT,
  SKY_VIEW_LUT_WIDTH,
  TRANSMITTANCE_LUT_HEIGHT,
  TRANSMITTANCE_LUT_WIDTH,
  apDepthAt,
  apSliceCoord,
  atmosphereKey,
  atmosphereParams,
  cornetteShanksPhase,
  diskVisibility,
  distanceToTop,
  groundDip,
  mediumAt,
  multiScatteringHMuSToUv,
  multiScatteringUvToHMuS,
  rayleighPhase,
  skyViewDirectionToUv,
  skyViewUvToDirection,
  subUvToUnit,
  transmittanceHMuToUv,
  transmittanceUvToHMu,
  unitToSubUv,
} from './model.ts';

/**
 * The Hillaire 2020 atmosphere's definitions (PRD §9.2 pass 1, D-013): the table parameterisations are
 * exact inverses at every texel centre, the phase functions are normalised, the medium is the reference
 * Earth's, and the Earth's shadow is a continuous step across the solar disk.
 */
const p = atmosphereParams(0.03e-3, 1640 + 2000);

describe('lookup-table parameterisations', () => {
  it('sub-texel mapping: texel centres reach both ends, and the inverse is exact', () => {
    expect(subUvToUnit(0.5 / 64, 64)).toBeCloseTo(0, 14);
    expect(subUvToUnit(63.5 / 64, 64)).toBeCloseTo(1, 14);
    for (let x = 0; x <= 1; x += 0.0625) expect(subUvToUnit(unitToSubUv(x, 108), 108)).toBeCloseTo(x, 14);
  });

  it('transmittance: every texel centre maps to a ray above the horizon and back to its own texel', () => {
    let worst = 0;
    for (let j = 0; j < TRANSMITTANCE_LUT_HEIGHT; j++) {
      for (let i = 0; i < TRANSMITTANCE_LUT_WIDTH; i++) {
        const u = (i + 0.5) / TRANSMITTANCE_LUT_WIDTH;
        const v = (j + 0.5) / TRANSMITTANCE_LUT_HEIGHT;
        const { h, mu } = transmittanceUvToHMu(p, u, v);
        expect(h).toBeGreaterThanOrEqual(0);
        expect(h).toBeLessThanOrEqual(p.topRadiusM - p.bottomRadiusM + 1e-6);
        // Above the horizon: the ray never meets the ground (a grazing ray at the table's edge may touch it).
        const iv = sphereInside(h, mu, 0);
        if (iv.lo <= iv.hi && i < TRANSMITTANCE_LUT_WIDTH - 1) expect(iv.lo).toBeLessThanOrEqual(0);
        const [u2, v2] = transmittanceHMuToUv(p, h, mu);
        worst = Math.max(worst, Math.abs(u2 - u) * TRANSMITTANCE_LUT_WIDTH, Math.abs(v2 - v) * TRANSMITTANCE_LUT_HEIGHT);
      }
    }
    expect(worst).toBeLessThan(1e-6); // in texels
  });

  it('multiple scattering: round trip at every texel centre', () => {
    const n = MULTI_SCATTERING_LUT_SIZE;
    for (let j = 0; j < n; j++) {
      for (let i = 0; i < n; i++) {
        const { h, muS } = multiScatteringUvToHMuS(p, (i + 0.5) / n, (j + 0.5) / n);
        const [u, v] = multiScatteringHMuSToUv(p, h, muS);
        expect(u * n - 0.5).toBeCloseTo(i, 9);
        expect(v * n - 0.5).toBeCloseTo(j, 9);
      }
    }
  });

  it('sky view: round trip at every texel centre for a camera at 10 m, 1,650 m and 9 km; the horizon splits the rows', () => {
    for (const hView of [10, 1650, 9000]) {
      let worst = 0;
      for (let j = 0; j < SKY_VIEW_LUT_HEIGHT; j++) {
        for (let i = 0; i < SKY_VIEW_LUT_WIDTH; i++) {
          const u = (i + 0.5) / SKY_VIEW_LUT_WIDTH;
          const v = (j + 0.5) / SKY_VIEW_LUT_HEIGHT;
          const { viewZenithCos, lightViewCos } = skyViewUvToDirection(p, hView, u, v);
          const iv = sphereInside(hView, viewZenithCos, 0);
          const hitsGround = iv.lo <= iv.hi && iv.lo > 0;
          expect(hitsGround, `row ${j}`).toBe(j >= SKY_VIEW_LUT_HEIGHT / 2);
          const [u2, v2] = skyViewDirectionToUv(p, hView, viewZenithCos, lightViewCos, hitsGround);
          worst = Math.max(worst, Math.abs(u2 - u) * SKY_VIEW_LUT_WIDTH, Math.abs(v2 - v) * SKY_VIEW_LUT_HEIGHT);
        }
      }
      expect(worst, `hView ${hView}`).toBeLessThan(1e-6);
    }
  });

  it('sky view: texels crowd at the horizon (the 30 degrees around it get over twice the rows of a uniform mapping)', () => {
    const zen = (j: number) => (Math.acos(skyViewUvToDirection(p, 1650, 0.5, (j + 0.5) / SKY_VIEW_LUT_HEIGHT).viewZenithCos) * 180) / Math.PI;
    const horizon = 90 + (groundDip(1650) * 180) / Math.PI;
    const rows = Array.from({ length: SKY_VIEW_LUT_HEIGHT }, (_, j) => zen(j)).filter((z) => Math.abs(z - horizon) < 15).length;
    expect(rows).toBeGreaterThan(2 * SKY_VIEW_LUT_HEIGHT * (30 / 180));
  });

  it('aerial perspective: logarithmic slices from the camera to 500 km, and the slice coordinate inverts the depth', () => {
    expect(apDepthAt(0)).toBe(0);
    expect(apDepthAt(1)).toBeCloseTo(AP_FAR_M, 6);
    expect(apDepthAt(1 / AP_SLICES)).toBeLessThan(AP_NEAR_M);
    for (let w = 0; w <= 1; w += 1 / 64) expect(apSliceCoord(apDepthAt(w))).toBeCloseTo(w, 12);
    expect(apSliceCoord(1e9)).toBe(1);
  });

  it('distance to the top matches a ray-sphere intersection, including rays that dip first', () => {
    for (const h of [0, 1650, 40_000, 99_000]) {
      for (const mu of [1, 0.5, 0.01, 0, -0.01, -0.05]) {
        const iv = sphereInside(h, mu, p.topRadiusM - p.bottomRadiusM);
        expect(distanceToTop(p, h, mu)).toBeCloseTo(iv.hi, 3);
      }
    }
  });
});

describe('medium and phase functions', () => {
  it('is the reference Earth atmosphere at sea level, plus the haze inside its layer only', () => {
    const m0 = mediumAt(p, 0, false);
    for (let c = 0; c < 3; c++) {
      expect(m0.scatRay[c]).toBeCloseTo(RAYLEIGH_SCATTERING[c]!, 15);
      expect(m0.scatMie[c]).toBeCloseTo(MIE_SCATTERING[c]!, 15);
      expect(m0.ext[c]).toBeCloseTo(RAYLEIGH_SCATTERING[c]! + MIE_EXTINCTION[c]!, 15);
    }
    const mh = mediumAt(p, 0, true);
    expect(mh.ext[1] - m0.ext[1]).toBeCloseTo(0.03e-3, 15);
    expect(mh.scatMie[1] - m0.scatMie[1]).toBeCloseTo(0.9 * 0.03e-3, 15);
    // ozone peaks at 25 km and vanishes outside 10-40 km
    expect(mediumAt(p, 25_000, false).ext[1] - mediumAt(p, 25_000, false).scat[1]).toBeGreaterThan(1.8e-6);
    const m45 = mediumAt(p, 45_000, false);
    expect(m45.ext[1] - m45.scat[1] - 0.444e-6 * Math.exp(-45_000 / 1200)).toBeCloseTo(0, 18);
  });

  it('phase functions integrate to 1 over the sphere and peak forward', () => {
    const n = 200_000;
    let r = 0;
    let m = 0;
    for (let k = 0; k < n; k++) {
      const nu = -1 + (2 * (k + 0.5)) / n;
      r += 2 * Math.PI * rayleighPhase(nu) * (2 / n);
      m += 2 * Math.PI * cornetteShanksPhase(MIE_PHASE_G, nu) * (2 / n);
    }
    expect(r).toBeCloseTo(1, 6);
    expect(m).toBeCloseTo(1, 3);
    expect(cornetteShanksPhase(MIE_PHASE_G, 1)).toBeGreaterThan(100 * cornetteShanksPhase(MIE_PHASE_G, -1));
  });

  it('the Earth shadow: 0 below the disk, 1 above it, continuous across it, and later for a higher observer', () => {
    const R = 0.2666 * (Math.PI / 180);
    const at = (h: number, altDeg: number) => diskVisibility(h, Math.sin((altDeg * Math.PI) / 180), R);
    expect(at(10, -1)).toBe(0);
    expect(at(10, 1)).toBe(1);
    expect(at(10, -0.1)).toBeGreaterThan(0);
    expect(at(10, -0.1)).toBeLessThan(1);
    // from 4,000 m the horizon dips 2.03 degrees, so the Sun at -1.5 degrees is still up
    expect(at(4000, -1.5)).toBe(1);
    let prev = at(1650, -3);
    let worst = 0;
    for (let a = -3; a <= 1; a += 0.001) {
      const v = at(1650, a);
      worst = Math.max(worst, Math.abs(v - prev));
      prev = v;
    }
    expect(worst).toBeLessThan(0.01);
  });

  it('the cache key changes with the haze and nothing else of the scene', () => {
    expect(atmosphereKey(atmosphereParams(0.03e-3, 3640))).toBe(atmosphereKey(atmosphereParams(0.03e-3, 3640)));
    expect(atmosphereKey(atmosphereParams(0.04e-3, 3640))).not.toBe(atmosphereKey(atmosphereParams(0.03e-3, 3640)));
    expect(atmosphereKey(atmosphereParams(0.03e-3, 3641))).not.toBe(atmosphereKey(atmosphereParams(0.03e-3, 3640)));
    expect(p.bottomRadiusM).toBe(EARTH_RADIUS_M);
  });
});
