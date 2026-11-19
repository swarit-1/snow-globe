import { describe, expect, it } from 'vitest';
import { EARTH_RADIUS_M as R, DEG } from './constants.ts';
import { coverAt, coverProfileIssues } from './cover.ts';
import { KOSCHMIEDER_CONSTANT, betaFromVisibility, fogExcessBeta, koschmieder, transmittance, visibilityFromBeta } from './extinction.ts';
import { haversineDistance, horizonDip, localPointGroundDistance, rayPointGroundDistance } from './geodesy.ts';
import { sphereInside } from './shell.ts';

describe('great-circle distance of an intersection point', () => {
  it('equals the arc length R * theta of the point under the ray', () => {
    const dir = [Math.cos(10 * DEG), 0, Math.sin(10 * DEG)] as const;
    const t = sphereInside(1650, dir[2], 2650).hi;
    const x = t * dir[0];
    const z = 1650 + t * dir[2];
    expect(rayPointGroundDistance(1650, dir, t)).toBeCloseTo(R * Math.atan2(x, R + z), 6);
    expect(localPointGroundDistance([x, 0, z])).toBeCloseTo(R * Math.atan2(x, R + z), 6);
  });

  it('agrees with haversine between the camera and the point on a north-going ray', () => {
    // point 113 km north at the layer height: compare with latitude difference along a meridian
    const t = sphereInside(1650, 0, 2650).hi;
    const d = rayPointGroundDistance(1650, [0, 1, 0], t);
    const dLatDeg = d / R / DEG;
    expect(haversineDistance(46.78, -121.74, 46.78 + dLatDeg, -121.74)).toBeCloseTo(d, 3);
    expect(d).toBeLessThan(t); // the arc under a straight chord at height is shorter than the slant range
  });

  it('computes the horizon dip', () => {
    expect(horizonDip(0)).toBe(0);
    expect(horizonDip(1000) / DEG).toBeCloseTo(Math.acos(R / (R + 1000)) / DEG, 9);
    expect(horizonDip(1000) / DEG).toBeCloseTo(1.015, 2);
  });
});

describe('cover-profile interpolation', () => {
  const profile = [
    { distance_km: 5, cover_0_1: 0.2 },
    { distance_km: 15, cover_0_1: 0.8 },
    { distance_km: 40, cover_0_1: 0.5 },
  ];
  it('is linear between knots and constant beyond the ends', () => {
    expect(coverAt(profile, 0)).toBe(0.2);
    expect(coverAt(profile, 5)).toBe(0.2);
    expect(coverAt(profile, 10)).toBeCloseTo(0.5, 12);
    expect(coverAt(profile, 15)).toBeCloseTo(0.8, 12);
    expect(coverAt(profile, 27.5)).toBeCloseTo(0.65, 12);
    expect(coverAt(profile, 400)).toBe(0.5);
    expect(coverAt([{ distance_km: 3, cover_0_1: 0.7 }], 100)).toBe(0.7);
  });
  it('rejects bad profiles', () => {
    expect(coverProfileIssues(profile, 16)).toEqual([]);
    expect(coverProfileIssues([], 16).length).toBe(1);
    expect(coverProfileIssues([{ distance_km: 5, cover_0_1: 0.2 }, { distance_km: 5, cover_0_1: 0.3 }], 16).join()).toMatch(/strictly increasing/);
    expect(coverProfileIssues([{ distance_km: -1, cover_0_1: 1.2 }], 16).length).toBe(2);
  });
});

describe('Koschmieder / Beer-Lambert', () => {
  it('beta = -ln(eps)/V, 3.912/V at eps = 0.02', () => {
    expect(KOSCHMIEDER_CONSTANT).toBeCloseTo(3.912023, 6);
    expect(betaFromVisibility(1000)).toBeCloseTo(3.912023e-3, 9);
    expect(betaFromVisibility(1000, 0.05)).toBeCloseTo(-Math.log(0.05) / 1000, 12);
    expect(visibilityFromBeta(betaFromVisibility(2500))).toBeCloseTo(2500, 9);
  });
  it('contrast falls to eps at the visibility distance', () => {
    expect(transmittance(betaFromVisibility(800), 800)).toBeCloseTo(0.02, 12);
    expect(transmittance(0.001, 0)).toBe(1);
  });
  it('composites scene and airlight', () => {
    expect(koschmieder(10, 2, 1)).toBe(10);
    expect(koschmieder(10, 2, 0)).toBe(2);
    expect(koschmieder(10, 2, 0.25)).toBeCloseTo(4, 12);
  });
  it('applies only the excess of visibility extinction over the clear-air haze to fog', () => {
    expect(fogExcessBeta(null, 1e-4)).toBe(0);
    expect(fogExcessBeta(1000, 1e-4)).toBeCloseTo(3.912023e-3 - 1e-4, 9);
    expect(fogExcessBeta(100_000, 1e-4)).toBe(0);
    expect(() => betaFromVisibility(0)).toThrow();
  });
});
