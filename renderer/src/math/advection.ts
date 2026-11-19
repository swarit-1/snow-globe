/**
 * Cloud advection anchored at the forecast run (PRD §9.5: "the seed is a hash of the viewpoint and the
 * forecast run, and cloud noise drifts with the forecast wind across valid time. So scrubbing is
 * continuous").
 *
 * The noise field of a run is fixed at the run's initialisation time t0 (its seed comes from the run).
 * At valid time t it has drifted by the time integral of the wind over [t0, t]:
 *
 *   D(t) = integral from t0 to t of w(s) ds
 *
 * D is continuous in t whatever the wind does, so scrubbing never jumps, and it depends on the wind only
 * through that integral: a wind that differs by 1e-7 m/s (a float32 GRIB decode, another producer's
 * rounding) moves the field by 1e-7 m/s x lead time, millimetres over a 48 h run. Anchoring at the Unix
 * epoch instead (renderer 0.1.0) multiplied any wind difference by ~1.8e9 s.
 *
 * The wind is either a series of knots against lead time (piecewise linear between knots, held constant
 * before the first and after the last), integrated exactly, or, without a series, the valid-time wind
 * held constant since the run started (a documented v0 approximation). Without a series, a producer that
 * changes the valid-time wind from frame to frame still gets continuous drift, but its pace is off by
 * (dw/dt) x lead: scrubbing producers should send the series.
 *
 * TODO(M2): per-layer winds at cloud height. One scene-level wind moves every layer; today's producers
 * have only HRRR's 10 m wind, a weak proxy for how clouds aloft move.
 */

export interface WindKnot {
  /** Hours since the run's initialisation time. */
  lead_h: number;
  /** Eastward wind (m/s). */
  u_ms: number;
  /** Northward wind (m/s). */
  v_ms: number;
}

/** Wind (m/s) at a lead time (seconds) from a validated series: linear between knots, constant beyond. */
export function windAt(series: readonly WindKnot[], leadS: number): [number, number] {
  const n = series.length;
  if (n === 0) throw new Error('windAt: empty series');
  const h = leadS / 3600;
  const first = series[0]!;
  if (n === 1 || h <= first.lead_h) return [first.u_ms, first.v_ms];
  for (let j = 1; j < n; j++) {
    const k1 = series[j]!;
    if (h <= k1.lead_h) {
      const k0 = series[j - 1]!;
      const a = (h - k0.lead_h) / (k1.lead_h - k0.lead_h);
      return [k0.u_ms + (k1.u_ms - k0.u_ms) * a, k0.v_ms + (k1.v_ms - k0.v_ms) * a];
    }
  }
  const last = series[n - 1]!;
  return [last.u_ms, last.v_ms];
}

/**
 * Advection displacement (east, north; metres) of the cloud field from the run's initialisation to a
 * lead time of leadS seconds (>= 0). Integrates the piecewise-linear series exactly (trapezoids on each
 * linear piece); without a series, the constant wind (windU, windV) times leadS.
 */
export function advectionDisplacement(series: readonly WindKnot[] | undefined, windU: number, windV: number, leadS: number): [number, number] {
  if (!(leadS >= 0)) throw new Error('advectionDisplacement: lead time must be >= 0');
  if (leadS === 0) return [0, 0];
  if (!series || series.length === 0) return [windU * leadS, windV * leadS];
  // Breakpoints (s) inside (0, leadS): every knot time; the wind is linear between consecutive breakpoints.
  const cuts = [0];
  for (const k of series) {
    const t = k.lead_h * 3600;
    if (t > 0 && t < leadS) cuts.push(t);
  }
  cuts.push(leadS);
  let dx = 0;
  let dy = 0;
  for (let i = 1; i < cuts.length; i++) {
    const a = cuts[i - 1]!;
    const b = cuts[i]!;
    if (b <= a) continue;
    const [ua, va] = windAt(series, a);
    const [ub, vb] = windAt(series, b);
    dx += 0.5 * (ua + ub) * (b - a);
    dy += 0.5 * (va + vb) * (b - a);
  }
  return [dx, dy];
}

/** Problems with a wind series, or an empty list if it is valid. */
export function windSeriesIssues(series: readonly WindKnot[], maxKnots: number, maxLeadH: number): string[] {
  const issues: string[] = [];
  if (series.length === 0) issues.push('wind_series must have at least one knot');
  if (series.length > maxKnots) issues.push(`wind_series has ${series.length} knots; at most ${maxKnots} are supported`);
  let prev = -Infinity;
  series.forEach((k, i) => {
    if (!Number.isFinite(k.lead_h) || k.lead_h < 0 || k.lead_h > maxLeadH) issues.push(`wind_series[${i}].lead_h must be in [0, ${maxLeadH}]`);
    for (const c of ['u_ms', 'v_ms'] as const) {
      if (!Number.isFinite(k[c]) || Math.abs(k[c]) > 150) issues.push(`wind_series[${i}].${c} must be a finite number in [-150, 150]`);
    }
    if (!(k.lead_h > prev)) issues.push(`wind_series lead_h must be strictly increasing (knot ${i})`);
    prev = k.lead_h;
  });
  return issues;
}
