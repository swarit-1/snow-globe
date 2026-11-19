/**
 * SceneSpec: everything one Tier 1 frame depends on (PRD §9.2), plus validation, canonical hashing and
 * seed derivation (PRD §9.5).
 *
 * The JSON Schema in renderer/scene.schema.json describes the same contract; scene.test.ts checks that
 * the schema and validateScene() accept and reject the same documents, and lists the cross-field rules
 * only validateScene() can check (the schema's description names them too).
 *
 * Heights are metres above mean sea level unless a name says _agl. Angles are degrees. Azimuths are
 * clockwise from true north. Sun and Moon are supplied by the caller for now (TODO(M2): computed inside
 * via oculus-core WASM, C5); validateScene() rejects a Sun more than 1 degree from the solar-position
 * formula for the valid time and the camera (ephemeris/sun.ts). The Moon is not checked yet (TODO(M2)).
 */
import { advectionDisplacement, windAt, windSeriesIssues, type WindKnot } from './math/advection.ts';
import { coverProfileIssues, type CoverKnot } from './math/cover.ts';
import { MAX_LEAD_H, MAX_PROFILE_KNOTS, MAX_WIND_KNOTS, WIND_SERIES_TOLERANCE_MS } from './math/constants.ts';
import { SUN_CHECK_TOLERANCE_DEG, angularSeparationDeg, solarPosition } from './ephemeris/sun.ts';
import { sha256Hex } from './hash/sha256.ts';
import { ISO_UTC_PATTERN, isCanonicalUtcSeconds, parseUtcSeconds } from './time.ts';

export type { WindKnot } from './math/advection.ts';

export type LayerName = 'low' | 'mid' | 'high';
export const LAYER_NAMES: readonly LayerName[] = ['low', 'mid', 'high'];

/** Precipitation type (PRD §9.2 pass 7). Validated and hashed; rendering it is TODO(M2). */
export type PrecipType = 'none' | 'rain' | 'snow' | 'freezing_rain' | 'ice_pellets' | 'mixed';
export const PRECIP_TYPES: readonly PrecipType[] = ['none', 'rain', 'snow', 'freezing_rain', 'ice_pellets', 'mixed'];

export interface CloudLayerSpec {
  name: LayerName;
  base_m_msl: number;
  thickness_m: number;
  cover_profile: CoverKnot[];
}

export interface CameraSpec {
  width: number;
  height: number;
  lat: number;
  lon: number;
  elev_m_msl: number;
  heading_deg: number;
  pitch_deg: number;
  roll_deg: number;
  hfov_deg: number;
  /** Optional camera height above the ground under it (default 10 m); sets the v0 flat-ground height. */
  agl_m?: number;
}

export interface ForecastSpec {
  layers: CloudLayerSpec[];
  ceiling_m_msl: number | null;
  visibility_m: number | null;
  precip_rate_mm_h: number;
  /** Wind at the valid time (m/s). Advects the clouds when there is no wind_series. */
  wind_u_ms: number;
  wind_v_ms: number;
  /** Optional Koschmieder contrast threshold for visibility (default 0.02, so beta = 3.912 / V). */
  visibility_epsilon?: number;
  /**
   * Optional wind against lead time since run_init_utc (for example HRRR's hourly winds). When present,
   * the cloud drift is its exact time integral, so scrubbing across forecast hours is continuous; it must
   * agree with wind_u_ms / wind_v_ms at the valid time within 0.05 m/s.
   */
  wind_series?: WindKnot[];
  /** Optional precipitation type. Validated and hashed; rendering is TODO(M2) (pass 7). */
  precip_type?: PrecipType;
  /** Optional near-surface relative humidity (%). Validated and hashed; rendering is TODO(M2) (pass 6). */
  rh_pct?: number;
  /** Optional boundary-layer height above ground (m). Validated and hashed; rendering is TODO(M2) (haze depth). */
  pbl_height_m_agl?: number;
}

export interface PlateSpec {
  /** Asset id of the plate, for example "rainier.paradise-mountain/plate-v1". */
  id: string;
  /** SHA-256 (hex) of the plate file's bytes: puts the plate in scene_hash (PRD §10.3) and is checked against the file. */
  sha256: string;
  has_sky_mask: boolean;
  /** SHA-256 of the sky-mask file; required exactly when has_sky_mask is true. */
  sky_mask_sha256?: string;
  has_depth: boolean;
  /** SHA-256 of the depth file; required exactly when has_depth is true. */
  depth_sha256?: string;
  /** Optional distance (m) assumed for terrain pixels when has_depth is false (default 15,000 m). */
  nominal_depth_m?: number;
}

export interface SceneSpec {
  viewpoint_id: string;
  valid_time_utc: string;
  /** Initialisation time of the forecast run, canonical YYYY-MM-DDTHH:MM:SSZ. Clouds are advected from here. */
  run_init_utc: string;
  renderer_version: string;
  /** deriveSeed(viewpoint_id, run_init_utc): the hash of viewpoint and forecast run (PRD §9.5). */
  seed: string;
  camera: CameraSpec;
  sun: { alt_deg: number; az_deg: number };
  moon: { alt_deg: number; az_deg: number; illuminated_fraction: number };
  forecast: ForecastSpec;
  haze: { beta_per_km: number };
  plate: PlateSpec;
  /** Optional free-form provenance (forecast file, run, issuer). Hashed, never read by the renderer. */
  meta?: Record<string, unknown>;
}

export interface ValidateOptions {
  /**
   * Accept a Sun that does not match the valid time and place. Only for synthetic geometry tests (Sun
   * sweeps, disk placement, fuzzing); production paths (the CLI, eval) never set it.
   */
  allowSyntheticSun?: boolean;
}

export class SceneValidationError extends Error {
  readonly issues: string[];
  constructor(issues: string[]) {
    super(`invalid SceneSpec:\n  - ${issues.join('\n  - ')}`);
    this.name = 'SceneValidationError';
    this.issues = issues;
  }
}

const ISO_UTC = new RegExp(ISO_UTC_PATTERN);
const VIEWPOINT_ID = /^[a-z0-9]+(?:[-.][a-z0-9]+)*$/;
const ASSET_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]*$/;
const SHA256_HEX = /^[0-9a-f]{64}$/;

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

const isFiniteNumber = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

class Checker {
  readonly issues: string[] = [];

  object(v: unknown, path: string, required: string[], optional: string[] = []): v is Obj {
    if (!isObj(v)) {
      this.issues.push(`${path} must be an object`);
      return false;
    }
    for (const k of required) if (!(k in v)) this.issues.push(`${path}.${k} is required`);
    const allowed = new Set([...required, ...optional]);
    for (const k of Object.keys(v)) if (!allowed.has(k)) this.issues.push(`${path}.${k} is not a known field`);
    return true;
  }

  /** Records an issue unless v is a finite number in range; returns whether it was. */
  number(v: unknown, path: string, min: number, max: number, opts: { exclusiveMin?: boolean; integer?: boolean } = {}): boolean {
    if (!isFiniteNumber(v)) {
      this.issues.push(`${path} must be a finite number`);
      return false;
    }
    const before = this.issues.length;
    if (opts.integer && !Number.isInteger(v)) this.issues.push(`${path} must be an integer`);
    if (opts.exclusiveMin ? !(v > min) : !(v >= min)) this.issues.push(`${path} must be ${opts.exclusiveMin ? '>' : '>='} ${min}`);
    if (!(v <= max)) this.issues.push(`${path} must be <= ${max}`);
    return this.issues.length === before;
  }

  nullableNumber(v: unknown, path: string, min: number, max: number, exclusiveMin = false): void {
    if (v === null) return;
    this.number(v, path, min, max, { exclusiveMin });
  }

  /** Records an issue unless v is a non-empty string within maxLen matching re; returns whether it was. */
  string(v: unknown, path: string, re?: RegExp, maxLen = 200): boolean {
    if (typeof v !== 'string' || v.length === 0) {
      this.issues.push(`${path} must be a non-empty string`);
      return false;
    }
    const before = this.issues.length;
    if (v.length > maxLen) this.issues.push(`${path} must be at most ${maxLen} characters`);
    if (re && !re.test(v)) this.issues.push(`${path} has an invalid format`);
    return this.issues.length === before;
  }

  boolean(v: unknown, path: string): boolean {
    if (typeof v !== 'boolean') {
      this.issues.push(`${path} must be a boolean`);
      return false;
    }
    return true;
  }
}

/**
 * Validates an untrusted value as a SceneSpec and returns it typed. Unknown fields are rejected, because
 * a frozen scene with a misspelled field would otherwise render silently wrong. Throws
 * SceneValidationError listing every problem.
 *
 * Cross-field rules (not expressible in the JSON Schema): the seed is deriveSeed(viewpoint_id,
 * run_init_utc); timestamps name real instants; the valid time lies 0-384 h after the run's
 * initialisation; layer names are unique (the schema checks this too); cover-profile distances and
 * wind-series lead times strictly increase; the wind series agrees with the valid-time wind; mask and
 * depth hashes are present exactly when their flags are set (the schema checks this too); and the Sun
 * agrees with the solar-position formula (unless opts.allowSyntheticSun).
 */
export function validateScene(input: unknown, opts: ValidateOptions = {}): SceneSpec {
  const c = new Checker();
  if (
    !c.object(
      input,
      'scene',
      ['viewpoint_id', 'valid_time_utc', 'run_init_utc', 'renderer_version', 'seed', 'camera', 'sun', 'moon', 'forecast', 'haze', 'plate'],
      ['meta'],
    )
  ) {
    throw new SceneValidationError(c.issues);
  }
  const s = input;
  const viewpointOk = c.string(s.viewpoint_id, 'scene.viewpoint_id', VIEWPOINT_ID, 100);

  let validS: number | null = null;
  if (c.string(s.valid_time_utc, 'scene.valid_time_utc', ISO_UTC, 40)) {
    validS = parseUtcSeconds(s.valid_time_utc as string);
    if (validS === null) c.issues.push('scene.valid_time_utc is not a real date and time');
  }
  let initS: number | null = null;
  if (c.string(s.run_init_utc, 'scene.run_init_utc', undefined, 40)) {
    if (!isCanonicalUtcSeconds(s.run_init_utc as string)) {
      c.issues.push('scene.run_init_utc must be a real instant written YYYY-MM-DDTHH:MM:SSZ');
    } else {
      initS = parseUtcSeconds(s.run_init_utc as string);
    }
  }
  let leadS: number | null = null;
  if (validS !== null && initS !== null) {
    leadS = validS - initS;
    if (leadS < 0) {
      c.issues.push('scene.valid_time_utc is before scene.run_init_utc');
      leadS = null;
    } else if (leadS > MAX_LEAD_H * 3600) {
      c.issues.push(`scene.valid_time_utc is more than ${MAX_LEAD_H} h after scene.run_init_utc`);
      leadS = null;
    }
  }
  c.string(s.renderer_version, 'scene.renderer_version', undefined, 40);
  if (c.string(s.seed, 'scene.seed', undefined, 200) && viewpointOk && initS !== null) {
    const expected = deriveSeed(s.viewpoint_id as string, s.run_init_utc as string);
    if (s.seed !== expected) c.issues.push(`scene.seed must be deriveSeed(viewpoint_id, run_init_utc) = ${expected}`);
  }

  let cameraOk = false;
  if (c.object(s.camera, 'scene.camera', ['width', 'height', 'lat', 'lon', 'elev_m_msl', 'heading_deg', 'pitch_deg', 'roll_deg', 'hfov_deg'], ['agl_m'])) {
    const cam = s.camera;
    const before = c.issues.length;
    c.number(cam.width, 'scene.camera.width', 1, 8192, { integer: true });
    c.number(cam.height, 'scene.camera.height', 1, 8192, { integer: true });
    c.number(cam.lat, 'scene.camera.lat', -90, 90);
    c.number(cam.lon, 'scene.camera.lon', -180, 180);
    c.number(cam.elev_m_msl, 'scene.camera.elev_m_msl', -500, 9000);
    c.number(cam.heading_deg, 'scene.camera.heading_deg', -360, 360);
    c.number(cam.pitch_deg, 'scene.camera.pitch_deg', -90, 90);
    c.number(cam.roll_deg, 'scene.camera.roll_deg', -180, 180);
    c.number(cam.hfov_deg, 'scene.camera.hfov_deg', 0, 179, { exclusiveMin: true });
    if (cam.agl_m !== undefined) c.number(cam.agl_m, 'scene.camera.agl_m', 0, 1000);
    cameraOk = c.issues.length === before;
  }
  let sunOk = false;
  if (c.object(s.sun, 'scene.sun', ['alt_deg', 'az_deg'])) {
    const a = c.number(s.sun.alt_deg, 'scene.sun.alt_deg', -90, 90);
    const z = c.number(s.sun.az_deg, 'scene.sun.az_deg', -360, 360);
    sunOk = a && z;
  }
  if (c.object(s.moon, 'scene.moon', ['alt_deg', 'az_deg', 'illuminated_fraction'])) {
    c.number(s.moon.alt_deg, 'scene.moon.alt_deg', -90, 90);
    c.number(s.moon.az_deg, 'scene.moon.az_deg', -360, 360);
    c.number(s.moon.illuminated_fraction, 'scene.moon.illuminated_fraction', 0, 1);
  }
  if (
    c.object(
      s.forecast,
      'scene.forecast',
      ['layers', 'ceiling_m_msl', 'visibility_m', 'precip_rate_mm_h', 'wind_u_ms', 'wind_v_ms'],
      ['visibility_epsilon', 'wind_series', 'precip_type', 'rh_pct', 'pbl_height_m_agl'],
    )
  ) {
    const f = s.forecast;
    if (!Array.isArray(f.layers)) {
      c.issues.push('scene.forecast.layers must be an array');
    } else {
      if (f.layers.length > LAYER_NAMES.length) c.issues.push(`scene.forecast.layers has at most ${LAYER_NAMES.length} entries`);
      const seen = new Set<string>();
      f.layers.forEach((layer: unknown, i: number) => {
        const p = `scene.forecast.layers[${i}]`;
        if (!c.object(layer, p, ['name', 'base_m_msl', 'thickness_m', 'cover_profile'])) return;
        if (typeof layer.name !== 'string' || !(LAYER_NAMES as readonly string[]).includes(layer.name)) {
          c.issues.push(`${p}.name must be one of ${LAYER_NAMES.join(', ')}`);
        } else if (seen.has(layer.name)) {
          c.issues.push(`${p}.name "${layer.name}" appears twice`);
        } else {
          seen.add(layer.name);
        }
        c.number(layer.base_m_msl, `${p}.base_m_msl`, -500, 20000);
        c.number(layer.thickness_m, `${p}.thickness_m`, 0, 15000, { exclusiveMin: true });
        if (!Array.isArray(layer.cover_profile)) {
          c.issues.push(`${p}.cover_profile must be an array`);
        } else {
          const knots: CoverKnot[] = [];
          layer.cover_profile.forEach((k: unknown, j: number) => {
            if (c.object(k, `${p}.cover_profile[${j}]`, ['distance_km', 'cover_0_1'])) {
              knots.push({ distance_km: k.distance_km as number, cover_0_1: k.cover_0_1 as number });
            }
          });
          if (knots.length === layer.cover_profile.length) {
            for (const msg of coverProfileIssues(knots, MAX_PROFILE_KNOTS)) c.issues.push(`${p}: ${msg}`);
          }
        }
      });
    }
    c.nullableNumber(f.ceiling_m_msl, 'scene.forecast.ceiling_m_msl', -500, 20000);
    c.nullableNumber(f.visibility_m, 'scene.forecast.visibility_m', 0, 1_000_000, true);
    c.number(f.precip_rate_mm_h, 'scene.forecast.precip_rate_mm_h', 0, 500);
    const uOk = c.number(f.wind_u_ms, 'scene.forecast.wind_u_ms', -150, 150);
    const vOk = c.number(f.wind_v_ms, 'scene.forecast.wind_v_ms', -150, 150);
    if (f.visibility_epsilon !== undefined) {
      c.number(f.visibility_epsilon, 'scene.forecast.visibility_epsilon', 0, 0.5, { exclusiveMin: true });
    }
    if (f.precip_type !== undefined && !(PRECIP_TYPES as readonly unknown[]).includes(f.precip_type)) {
      c.issues.push(`scene.forecast.precip_type must be one of ${PRECIP_TYPES.join(', ')}`);
    }
    if (f.rh_pct !== undefined) c.number(f.rh_pct, 'scene.forecast.rh_pct', 0, 100);
    if (f.pbl_height_m_agl !== undefined) c.number(f.pbl_height_m_agl, 'scene.forecast.pbl_height_m_agl', 0, 10000);
    if (f.wind_series !== undefined) {
      if (!Array.isArray(f.wind_series)) {
        c.issues.push('scene.forecast.wind_series must be an array');
      } else {
        const knots: WindKnot[] = [];
        f.wind_series.forEach((k: unknown, j: number) => {
          if (c.object(k, `scene.forecast.wind_series[${j}]`, ['lead_h', 'u_ms', 'v_ms'])) {
            knots.push({ lead_h: k.lead_h as number, u_ms: k.u_ms as number, v_ms: k.v_ms as number });
          }
        });
        if (knots.length === f.wind_series.length) {
          const issues = windSeriesIssues(knots, MAX_WIND_KNOTS, MAX_LEAD_H);
          for (const msg of issues) c.issues.push(`scene.forecast: ${msg}`);
          if (issues.length === 0 && uOk && vOk && leadS !== null) {
            const [u, v] = windAt(knots, leadS);
            const off = Math.hypot(u - (f.wind_u_ms as number), v - (f.wind_v_ms as number));
            if (off > WIND_SERIES_TOLERANCE_MS) {
              c.issues.push(
                `scene.forecast.wind_series gives (${u.toFixed(3)}, ${v.toFixed(3)}) m/s at the valid time, ` +
                  `${off.toFixed(3)} m/s from wind_u_ms / wind_v_ms (tolerance ${WIND_SERIES_TOLERANCE_MS})`,
              );
            }
          }
        }
      }
    }
  }
  if (c.object(s.haze, 'scene.haze', ['beta_per_km'])) c.number(s.haze.beta_per_km, 'scene.haze.beta_per_km', 0, 10);
  if (c.object(s.plate, 'scene.plate', ['id', 'sha256', 'has_sky_mask', 'has_depth'], ['sky_mask_sha256', 'depth_sha256', 'nominal_depth_m'])) {
    const p = s.plate;
    c.string(p.id, 'scene.plate.id', ASSET_ID, 200);
    c.string(p.sha256, 'scene.plate.sha256', SHA256_HEX, 64);
    for (const [flag, field] of [
      ['has_sky_mask', 'sky_mask_sha256'],
      ['has_depth', 'depth_sha256'],
    ] as const) {
      if (!c.boolean(p[flag], `scene.plate.${flag}`)) continue;
      if (p[flag] === true) {
        if (p[field] === undefined) c.issues.push(`scene.plate.${field} is required when ${flag} is true`);
        else c.string(p[field], `scene.plate.${field}`, SHA256_HEX, 64);
      } else if (p[field] !== undefined) {
        c.issues.push(`scene.plate.${field} must be absent when ${flag} is false`);
      }
    }
    if (p.nominal_depth_m !== undefined) c.number(p.nominal_depth_m, 'scene.plate.nominal_depth_m', 1, 1_000_000);
  }
  if (s.meta !== undefined && !isObj(s.meta)) c.issues.push('scene.meta must be an object');

  if (!opts.allowSyntheticSun && sunOk && cameraOk && validS !== null) {
    const cam = s.camera as unknown as CameraSpec;
    const sun = s.sun as { alt_deg: number; az_deg: number };
    const ref = solarPosition(validS, cam.lat, cam.lon);
    const sep = angularSeparationDeg(sun.alt_deg, sun.az_deg, ref.altDeg, ref.azDeg);
    if (sep > SUN_CHECK_TOLERANCE_DEG) {
      c.issues.push(
        `scene.sun is ${sep.toFixed(2)} deg from the Sun at valid_time_utc for the camera ` +
          `(computed alt ${ref.altDeg.toFixed(3)}, az ${ref.azDeg.toFixed(3)}; tolerance ${SUN_CHECK_TOLERANCE_DEG} deg)`,
      );
    }
  }

  if (c.issues.length > 0) throw new SceneValidationError(c.issues);
  return input as unknown as SceneSpec;
}

/** JSON with object keys sorted at every level and no whitespace: the input to scene hashing. */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('canonicalJson: non-finite number');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  const obj = value as Obj;
  const keys = Object.keys(obj)
    .filter((k) => obj[k] !== undefined)
    .sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(obj[k])}`).join(',')}}`;
}

/**
 * SHA-256 (hex) of the canonical JSON of a scene: the scene_hash used in metadata and paint gating. It
 * covers the plate, mask and depth through their file hashes in scene.plate.
 */
export function sceneHash(scene: SceneSpec): string {
  return sha256Hex(canonicalJson(scene));
}

/**
 * The seed for a (viewpoint, forecast run) pair (PRD §9.5): the same run always gives the same clouds,
 * and noise then drifts with the wind across valid time, so scrubbing stays continuous.
 * runInitUtc is the run's initialisation time in the canonical form, for example "2026-11-18T12:00:00Z".
 */
export function deriveSeed(viewpointId: string, runInitUtc: string): string {
  return sha256Hex(`lookout-seed-v1|${viewpointId}|${runInitUtc}`).slice(0, 16);
}

/** Valid time as seconds since the Unix epoch (the scene must be validated). */
export function validTimeSeconds(scene: SceneSpec): number {
  const t = parseUtcSeconds(scene.valid_time_utc);
  if (t === null) throw new Error('validTimeSeconds: invalid valid_time_utc');
  return t;
}

/** Seconds from the run's initialisation to the valid time (the scene must be validated). */
export function leadSeconds(scene: SceneSpec): number {
  const t0 = parseUtcSeconds(scene.run_init_utc);
  if (t0 === null) throw new Error('leadSeconds: invalid run_init_utc');
  return validTimeSeconds(scene) - t0;
}

/** The cloud field's drift (east, north; metres) since the run's initialisation (math/advection.ts). */
export function sceneAdvection(scene: SceneSpec): [number, number] {
  const f = scene.forecast;
  return advectionDisplacement(f.wind_series, f.wind_u_ms, f.wind_v_ms, leadSeconds(scene));
}
