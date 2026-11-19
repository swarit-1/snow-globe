/**
 * Low-accuracy solar position, used to check the caller's Sun (PRD §9.2: "Sun and Moon are computed
 * inside the module"; until the oculus-core WASM lands (TODO(M2)), the caller supplies them and the
 * renderer rejects a Sun that disagrees with this formula by more than SUN_CHECK_TOLERANCE_DEG).
 *
 * The NOAA Global Monitoring Laboratory solar calculator algorithm, which follows Meeus, Astronomical
 * Algorithms (1991) ch. 25 (low-accuracy Sun) and ch. 28 (equation of time). UT is used as dynamical
 * time (about 69 s, under 0.001 deg of solar longitude) and the Sun's parallax (<= 0.0024 deg) is
 * ignored; both are far below the check's 1-degree tolerance. Altitude is geometric (no refraction),
 * the SceneSpec convention. Azimuth is clockwise from true north.
 */
import { DEG } from '../math/constants.ts';

const UNIX_EPOCH_JD = 2440587.5;

/** A disagreement above this angular distance (degrees) between the caller's Sun and the formula is an error. */
export const SUN_CHECK_TOLERANCE_DEG = 1;

export interface SolarPosition {
  /** Apparent ecliptic longitude (deg). */
  apparentLongitudeDeg: number;
  /** Apparent right ascension (deg, [0, 360)). */
  rightAscensionDeg: number;
  /** Apparent declination (deg). */
  declinationDeg: number;
  /** Equation of time (minutes). */
  equationOfTimeMin: number;
  /** Geometric altitude (deg). */
  altDeg: number;
  /** Azimuth clockwise from north (deg, [0, 360)). */
  azDeg: number;
}

const clamp1 = (v: number): number => Math.max(-1, Math.min(1, v));
const mod = (a: number, n: number): number => ((a % n) + n) % n;

/** Julian Day (UT) of a time in seconds since the Unix epoch. */
export function julianDay(unixSeconds: number): number {
  return UNIX_EPOCH_JD + unixSeconds / 86400;
}

/** Solar position at a time (seconds since the Unix epoch, UT) for a site (degrees; longitude east positive). */
export function solarPosition(unixSeconds: number, latDeg: number, lonDeg: number): SolarPosition {
  const jd = julianDay(unixSeconds);
  const jc = (jd - 2451545.0) / 36525.0;
  const meanLong = mod(280.46646 + jc * (36000.76983 + jc * 0.0003032), 360);
  const meanAnom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc);
  const ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc);
  const centre =
    Math.sin(meanAnom * DEG) * (1.914602 - jc * (0.004817 + 0.000014 * jc)) +
    Math.sin(2 * meanAnom * DEG) * (0.019993 - 0.000101 * jc) +
    Math.sin(3 * meanAnom * DEG) * 0.000289;
  const trueLong = meanLong + centre;
  const omega = 125.04 - 1934.136 * jc;
  const appLong = trueLong - 0.00569 - 0.00478 * Math.sin(omega * DEG);
  const seconds = 21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813));
  const meanObliq = 23 + (26 + seconds / 60) / 60;
  const obliq = meanObliq + 0.00256 * Math.cos(omega * DEG);
  const ra = Math.atan2(Math.cos(obliq * DEG) * Math.sin(appLong * DEG), Math.cos(appLong * DEG)) / DEG;
  const dec = Math.asin(clamp1(Math.sin(obliq * DEG) * Math.sin(appLong * DEG))) / DEG;
  const y = Math.tan((obliq / 2) * DEG) ** 2;
  const l0 = meanLong * DEG;
  const m = meanAnom * DEG;
  const eqTime =
    (4 *
      (y * Math.sin(2 * l0) -
        2 * ecc * Math.sin(m) +
        4 * ecc * y * Math.sin(m) * Math.cos(2 * l0) -
        0.5 * y * y * Math.sin(4 * l0) -
        1.25 * ecc * ecc * Math.sin(2 * m))) /
    DEG;
  const minutesUtc = mod(jd + 0.5, 1) * 1440;
  const trueSolarTime = mod(minutesUtc + eqTime + 4 * lonDeg, 1440);
  const hourAngle = trueSolarTime / 4 - 180;
  const lat = latDeg * DEG;
  const cosZenith = Math.sin(lat) * Math.sin(dec * DEG) + Math.cos(lat) * Math.cos(dec * DEG) * Math.cos(hourAngle * DEG);
  const zenith = Math.acos(clamp1(cosZenith)) / DEG;
  const denom = Math.cos(lat) * Math.sin(zenith * DEG);
  let az: number;
  if (Math.abs(denom) < 1e-12) {
    az = latDeg >= dec ? 180 : 0;
  } else {
    const acosTerm = Math.acos(clamp1((Math.sin(lat) * Math.cos(zenith * DEG) - Math.sin(dec * DEG)) / denom)) / DEG;
    az = hourAngle > 0 ? mod(acosTerm + 180, 360) : mod(540 - acosTerm, 360);
  }
  return {
    apparentLongitudeDeg: mod(appLong, 360),
    rightAscensionDeg: mod(ra, 360),
    declinationDeg: dec,
    equationOfTimeMin: eqTime,
    altDeg: 90 - zenith,
    azDeg: az,
  };
}

/** Angular distance (degrees) between two altitude/azimuth directions (atan2 form: exact at 0 and 180 degrees). */
export function angularSeparationDeg(alt1: number, az1: number, alt2: number, az2: number): number {
  const v = (alt: number, az: number): [number, number, number] => [Math.cos(alt * DEG) * Math.sin(az * DEG), Math.cos(alt * DEG) * Math.cos(az * DEG), Math.sin(alt * DEG)];
  const a = v(alt1, az1);
  const b = v(alt2, az2);
  const cx = a[1] * b[2] - a[2] * b[1];
  const cy = a[2] * b[0] - a[0] * b[2];
  const cz = a[0] * b[1] - a[1] * b[0];
  return Math.atan2(Math.hypot(cx, cy, cz), a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / DEG;
}
