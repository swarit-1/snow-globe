import { describe, expect, it } from 'vitest';
import { parseUtcSeconds } from '../time.ts';
import { angularSeparationDeg, julianDay, solarPosition } from './sun.ts';

const unix = (jd: number) => (jd - 2440587.5) * 86400;

describe('low-accuracy solar position (Sun check for SceneSpec)', () => {
  it('matches Meeus, Astronomical Algorithms, Example 25.a (1992 October 13.0)', () => {
    const p = solarPosition(unix(2448908.5), 0, 0);
    expect(julianDay(unix(2448908.5))).toBeCloseTo(2448908.5, 9);
    expect(p.apparentLongitudeDeg).toBeCloseTo(199.90895, 4);
    expect(p.rightAscensionDeg).toBeCloseTo(198.38083, 4);
    expect(p.declinationDeg).toBeCloseTo(-7.78507, 4);
  });

  it('puts the noon Sun due south at 90 - (latitude - declination) for a northern site', () => {
    // Scan one day at 10 s steps for the highest Sun at Paradise (46.786 N, 121.735 W).
    const t0 = parseUtcSeconds('2026-11-18T12:00:00Z')!;
    let best = { t: t0, alt: -90 };
    for (let t = t0; t < t0 + 86_400; t += 10) {
      const a = solarPosition(t, 46.786, -121.735).altDeg;
      if (a > best.alt) best = { t, alt: a };
    }
    const p = solarPosition(best.t, 46.786, -121.735);
    expect(p.azDeg).toBeCloseTo(180, 0);
    expect(p.altDeg).toBeCloseTo(90 - (46.786 - p.declinationDeg), 2);
  });

  it('gives morning azimuths east of south and afternoon ones west of south', () => {
    const am = solarPosition(parseUtcSeconds('2026-11-18T17:00:00Z')!, 46.786, -121.735);
    const pm = solarPosition(parseUtcSeconds('2026-11-18T22:00:00Z')!, 46.786, -121.735);
    expect(am.azDeg).toBeGreaterThan(90);
    expect(am.azDeg).toBeLessThan(180);
    expect(pm.azDeg).toBeGreaterThan(180);
    expect(pm.azDeg).toBeLessThan(270);
    // at night the geometric altitude is negative
    expect(solarPosition(parseUtcSeconds('2026-11-19T08:00:00Z')!, 46.786, -121.735).altDeg).toBeLessThan(-30);
  });

  it('measures angular separation on the sphere', () => {
    expect(angularSeparationDeg(10, 20, 10, 20)).toBe(0);
    expect(angularSeparationDeg(0, 0, 0, 90)).toBeCloseTo(90, 9);
    expect(angularSeparationDeg(89.5, 0, 89.5, 180)).toBeCloseTo(1, 9);
    expect(angularSeparationDeg(-90, 0, 90, 0)).toBeCloseTo(180, 9);
  });
});
