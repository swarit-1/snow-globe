import { describe, expect, it } from 'vitest';
import { advectionDisplacement, windAt, windSeriesIssues, type WindKnot } from './advection.ts';

const SERIES: WindKnot[] = [
  { lead_h: 6, u_ms: 6, v_ms: -2 },
  { lead_h: 7, u_ms: 9, v_ms: -2 },
  { lead_h: 8, u_ms: 3, v_ms: 1 },
];

describe('advection anchored at the run (PRD §9.5)', () => {
  it('is zero at the run initialisation and wind x lead for a constant wind', () => {
    expect(advectionDisplacement(undefined, 6, -2, 0)).toEqual([0, 0]);
    expect(advectionDisplacement(undefined, 6, -2, 3600)).toEqual([21_600, -7_200]);
    expect(advectionDisplacement([{ lead_h: 3, u_ms: 6, v_ms: -2 }], 0, 0, 3600)).toEqual([21_600, -7_200]);
  });

  it('integrates a piecewise-linear wind series exactly', () => {
    // 0-6 h constant 6 m/s, 6-7 h ramps 6 -> 9 (mean 7.5), 7-8 h ramps 9 -> 3 (mean 6), then constant 3
    expect(advectionDisplacement(SERIES, 0, 0, 7 * 3600)).toEqual([(36 + 7.5) * 3600, -14 * 3600]);
    const [dx, dy] = advectionDisplacement(SERIES, 0, 0, 9 * 3600);
    expect(dx).toBeCloseTo((36 + 7.5 + 6 + 3) * 3600, 6);
    expect(dy).toBeCloseTo((-12 - 2 - 0.5 + 1) * 3600, 6);
    // halfway through a ramp
    const [hx] = advectionDisplacement(SERIES, 0, 0, 6.5 * 3600);
    expect(hx).toBeCloseTo(36 * 3600 + 0.5 * 3600 * ((6 + 7.5) / 2), 6);
  });

  it('is continuous across forecast hours even where the wind changes slope', () => {
    // A 1-second step anywhere moves the field by about the local wind speed, never by a jump.
    for (const t of [6 * 3600 - 0.5, 7 * 3600 - 0.5, 8 * 3600 - 0.5]) {
      const a = advectionDisplacement(SERIES, 0, 0, t);
      const b = advectionDisplacement(SERIES, 0, 0, t + 1);
      const [u, v] = windAt(SERIES, t + 0.5);
      expect(Math.hypot(b[0] - a[0] - u, b[1] - a[1] - v)).toBeLessThan(0.01);
    }
  });

  it('reads the wind between knots and holds it beyond the ends', () => {
    expect(windAt(SERIES, 0)).toEqual([6, -2]);
    expect(windAt(SERIES, 6.5 * 3600)).toEqual([7.5, -2]);
    expect(windAt(SERIES, 7.5 * 3600)).toEqual([6, -0.5]);
    expect(windAt(SERIES, 48 * 3600)).toEqual([3, 1]);
  });

  it('rejects malformed series and negative leads', () => {
    expect(windSeriesIssues(SERIES, 400, 384)).toEqual([]);
    expect(windSeriesIssues([], 400, 384).length).toBe(1);
    expect(windSeriesIssues([SERIES[1]!, SERIES[0]!], 400, 384).some((m) => /increasing/.test(m))).toBe(true);
    expect(windSeriesIssues([{ lead_h: -1, u_ms: 0, v_ms: 0 }], 400, 384).length).toBe(1);
    expect(windSeriesIssues([{ lead_h: 1, u_ms: 151, v_ms: 0 }], 400, 384).length).toBe(1);
    expect(() => advectionDisplacement(undefined, 1, 1, -1)).toThrow();
  });
});
