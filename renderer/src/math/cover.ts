/**
 * Cover-profile interpolation (PRD §9.2 pass 3: "the forecast is read at the intersection point").
 *
 * A layer's cover profile lists forecast cover (0-1) against great-circle distance from the camera
 * along the view. Between knots cover is linear in distance; beyond the first and last knot it is held
 * constant. The GLSL coverAt() mirrors this exactly.
 */

export interface CoverKnot {
  distance_km: number;
  cover_0_1: number;
}

/** Forecast cover at a great-circle distance (km) from the camera. The profile must be validated. */
export function coverAt(profile: readonly CoverKnot[], distanceKm: number): number {
  const n = profile.length;
  if (n === 0) throw new Error('coverAt: empty profile');
  let k0 = profile[0]!;
  if (n === 1 || distanceKm <= k0.distance_km) return k0.cover_0_1;
  for (let j = 1; j < n; j++) {
    const k1 = profile[j]!;
    if (distanceKm <= k1.distance_km) {
      const a = (distanceKm - k0.distance_km) / (k1.distance_km - k0.distance_km);
      return k0.cover_0_1 + (k1.cover_0_1 - k0.cover_0_1) * a;
    }
    k0 = k1;
  }
  return k0.cover_0_1;
}

/** Problems with a cover profile, or an empty list if it is valid. */
export function coverProfileIssues(profile: readonly CoverKnot[], maxKnots: number): string[] {
  const issues: string[] = [];
  if (profile.length === 0) issues.push('cover_profile must have at least one knot');
  if (profile.length > maxKnots) issues.push(`cover_profile has ${profile.length} knots; at most ${maxKnots} are supported`);
  let prev = -Infinity;
  profile.forEach((k, i) => {
    if (!Number.isFinite(k.distance_km) || k.distance_km < 0) issues.push(`cover_profile[${i}].distance_km must be a finite number >= 0`);
    if (!Number.isFinite(k.cover_0_1) || k.cover_0_1 < 0 || k.cover_0_1 > 1) issues.push(`cover_profile[${i}].cover_0_1 must be in [0, 1]`);
    if (!(k.distance_km > prev)) issues.push(`cover_profile distances must be strictly increasing (knot ${i})`);
    prev = k.distance_km;
  });
  return issues;
}
