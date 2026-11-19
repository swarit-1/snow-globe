import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Ajv2020 } from 'ajv/dist/2020.js';
import { describe, expect, it } from 'vitest';
import { SceneValidationError, canonicalJson, deriveSeed, leadSeconds, sceneAdvection, sceneHash, validateScene, type SceneSpec } from './scene.ts';
import { RENDERER_VERSION } from './version.ts';

const root = fileURLToPath(new URL('..', import.meta.url));
const schema = JSON.parse(readFileSync(`${root}/scene.schema.json`, 'utf8'));
const goldenDir = `${root}/goldens/scenes`;
const goldens = readdirSync(goldenDir).map((f) => JSON.parse(readFileSync(`${goldenDir}/${f}`, 'utf8')) as SceneSpec);
const base = goldens.find((g) => g.meta?.golden === 'low-deck')!;

const clone = (s: unknown): Record<string, any> => JSON.parse(JSON.stringify(s));

/** Mutations that must be rejected by both the JSON Schema and validateScene. */
const INVALID: [string, (s: Record<string, any>) => void][] = [
  ['missing seed', (s) => delete s.seed],
  ['missing run_init_utc', (s) => delete s.run_init_utc],
  ['run_init_utc without seconds', (s) => (s.run_init_utc = '2026-11-18T12:00Z')],
  ['unknown top-level field', (s) => (s.colour = 'blue')],
  ['misspelled forecast field', (s) => (s.forecast.visiblity_m = 1000)],
  ['bad viewpoint id', (s) => (s.viewpoint_id = 'Rainier Paradise')],
  ['valid time without Z', (s) => (s.valid_time_utc = '2026-11-18T19:00:00')],
  ['month 13', (s) => (s.valid_time_utc = '2026-13-18T19:00:00Z')],
  ['hour 24', (s) => (s.valid_time_utc = '2026-11-18T24:00:00Z')],
  ['non-integer width', (s) => (s.camera.width = 320.5)],
  ['zero hfov', (s) => (s.camera.hfov_deg = 0)],
  ['pitch beyond 90', (s) => (s.camera.pitch_deg = 91)],
  ['sun altitude as string', (s) => (s.sun.alt_deg = '12')],
  ['moon fraction above 1', (s) => (s.moon.illuminated_fraction = 1.2)],
  ['four layers', (s) => s.forecast.layers.push({ ...s.forecast.layers[0], name: 'mid' }, { ...s.forecast.layers[0], name: 'mid' })],
  ['duplicate layer names', (s) => (s.forecast.layers[1].name = 'low')],
  ['unknown layer name', (s) => (s.forecast.layers[0].name = 'fog')],
  ['zero thickness', (s) => (s.forecast.layers[0].thickness_m = 0)],
  ['empty cover profile', (s) => (s.forecast.layers[0].cover_profile = [])],
  ['cover above 1', (s) => (s.forecast.layers[0].cover_profile[0].cover_0_1 = 1.01)],
  ['negative visibility', (s) => (s.forecast.visibility_m = -5)],
  ['missing ceiling (must be null, not absent)', (s) => delete s.forecast.ceiling_m_msl],
  ['negative precipitation', (s) => (s.forecast.precip_rate_mm_h = -1)],
  ['unknown precipitation type', (s) => (s.forecast.precip_type = 'hail')],
  ['RH above 100 %', (s) => (s.forecast.rh_pct = 101)],
  ['negative boundary-layer height', (s) => (s.forecast.pbl_height_m_agl = -1)],
  ['empty wind series', (s) => (s.forecast.wind_series = [])],
  ['wind series lead beyond 384 h', (s) => (s.forecast.wind_series = [{ lead_h: 400, u_ms: 6, v_ms: -2 }])],
  ['wind series knot with an unknown field', (s) => (s.forecast.wind_series = [{ lead_h: 0, u_ms: 6, v_ms: -2, w_ms: 0 }])],
  ['haze as number', (s) => (s.haze = 0.04)],
  ['plate flag not boolean', (s) => (s.plate.has_depth = 1)],
  ['plate without an id', (s) => delete s.plate.id],
  ['plate hash not SHA-256 hex', (s) => (s.plate.sha256 = 'abc123')],
  ['sky-mask hash missing although has_sky_mask', (s) => delete s.plate.sky_mask_sha256],
  ['depth hash present although has_depth is false', (s) => (s.plate.has_depth = false)],
  ['meta not an object', (s) => (s.meta = 'x')],
];

/**
 * Rules only validateScene can check (cross-field or calendar); the schema description lists them. Each
 * mutated scene must pass the schema, which pins the gap down exactly.
 */
const INVALID_SEMANTIC: [string, (s: Record<string, any>) => void, RegExp][] = [
  ['non-increasing profile distances', (s) => (s.forecast.layers[0].cover_profile[1].distance_km = 0), /strictly increasing/],
  ['30 February', (s) => (s.valid_time_utc = '2026-02-30T19:00:00Z'), /not a real date/],
  ['seed not derived from viewpoint and run', (s) => (s.seed = '0123456789abcdef'), /deriveSeed/],
  ['another run with the old seed', (s) => (s.run_init_utc = '2026-11-18T13:00:00Z'), /deriveSeed/],
  ['valid time before the run', (s) => (s.valid_time_utc = '2026-11-18T11:00:00Z'), /before scene.run_init_utc/],
  ['valid time more than 384 h after the run', (s) => (s.valid_time_utc = '2026-12-05T19:00:00Z'), /more than 384 h/],
  ['wind series leads not increasing', (s) => (s.forecast.wind_series = [{ lead_h: 2, u_ms: 6, v_ms: -2 }, { lead_h: 1, u_ms: 6, v_ms: -2 }]), /strictly increasing/],
  ['wind series that disagrees with the valid-time wind', (s) => (s.forecast.wind_series = [{ lead_h: 0, u_ms: 1, v_ms: 1 }]), /wind_series gives/],
  ['Sun 5 degrees from where it is at the valid time', (s) => (s.sun.alt_deg += 5), /scene.sun is 5\.\d+ deg/],
  ['Sun at a local-time-for-UTC error (8 h off)', (s) => (s.valid_time_utc = '2026-11-19T03:00:00Z'), /scene.sun is/],
];

describe('SceneSpec validation', () => {
  const ajv = new Ajv2020({ allErrors: true, strict: true });
  const schemaValidate = ajv.compile(schema);

  it('accepts the golden scenes in both validators', () => {
    expect(goldens.length).toBe(3);
    for (const g of goldens) {
      expect(() => validateScene(g)).not.toThrow();
      expect(schemaValidate(g), JSON.stringify(schemaValidate.errors)).toBe(true);
      expect(g.renderer_version).toBe(RENDERER_VERSION);
    }
  });

  it('accepts optional fields and nulls', () => {
    const s = clone(base);
    s.forecast.ceiling_m_msl = null;
    s.forecast.visibility_m = null;
    s.forecast.visibility_epsilon = 0.05;
    s.forecast.precip_type = 'snow';
    s.forecast.rh_pct = 97.5;
    s.forecast.pbl_height_m_agl = 420;
    s.forecast.wind_series = [
      { lead_h: 0, u_ms: 4, v_ms: -2 },
      { lead_h: 6, u_ms: 6, v_ms: -2 },
      { lead_h: 8, u_ms: 6, v_ms: -2 },
    ];
    s.camera.agl_m = 3;
    s.plate.has_depth = false;
    delete s.plate.depth_sha256;
    s.plate.nominal_depth_m = 9000;
    s.forecast.layers = [];
    delete s.meta;
    expect(() => validateScene(s)).not.toThrow();
    expect(schemaValidate(s), JSON.stringify(schemaValidate.errors)).toBe(true);
  });

  for (const [name, mutate] of INVALID) {
    it(`rejects: ${name}`, () => {
      const s = clone(base);
      mutate(s);
      expect(() => validateScene(s)).toThrow(SceneValidationError);
      expect(schemaValidate(s)).toBe(false);
    });
  }

  for (const [name, mutate, message] of INVALID_SEMANTIC) {
    it(`rejects (validateScene only): ${name}`, () => {
      const s = clone(base);
      mutate(s);
      expect(schemaValidate(s), `the schema should accept this (${JSON.stringify(schemaValidate.errors)})`).toBe(true);
      try {
        validateScene(s);
        throw new Error('should have thrown');
      } catch (e) {
        expect(e).toBeInstanceOf(SceneValidationError);
        expect((e as SceneValidationError).issues.some((m) => message.test(m)), (e as Error).message).toBe(true);
      }
    });
  }

  it('lets synthetic geometry tests place the Sun anywhere, and only when asked', () => {
    const s = clone(base);
    s.sun = { alt_deg: -90, az_deg: 41.7 };
    expect(() => validateScene(s)).toThrow(/scene.sun/);
    expect(() => validateScene(s, { allowSyntheticSun: true })).not.toThrow();
  });

  it('lists every problem at once', () => {
    const s = clone(base);
    s.camera.width = -1;
    s.sun.alt_deg = 200;
    try {
      validateScene(s);
      throw new Error('should have thrown');
    } catch (e) {
      expect((e as SceneValidationError).issues.length).toBeGreaterThanOrEqual(2);
    }
  });
});

describe('scene hashing, seeds and lead time', () => {
  it('hashes canonical JSON, independent of key order', () => {
    const a = clone(base);
    const b = JSON.parse(canonicalJson(a));
    const reordered = Object.fromEntries(Object.entries(b).reverse());
    expect(sceneHash(reordered as unknown as SceneSpec)).toBe(sceneHash(a as unknown as SceneSpec));
    expect(canonicalJson({ b: 1, a: [2, { d: 3, c: null }] })).toBe('{"a":[2,{"c":null,"d":3}],"b":1}');
    const c = clone(base);
    c.forecast.wind_u_ms += 0.001;
    expect(sceneHash(c as unknown as SceneSpec)).not.toBe(sceneHash(a as unknown as SceneSpec));
  });

  it('changes the scene hash when the plate, mask or depth does (review finding: it did not)', () => {
    const h = sceneHash(base);
    for (const field of ['sha256', 'sky_mask_sha256', 'depth_sha256', 'id'] as const) {
      const s = clone(base);
      s.plate[field] = field === 'id' ? 'synthetic.test-ridge/plate-v3' : 'f'.repeat(64);
      expect(sceneHash(s as unknown as SceneSpec), field).not.toBe(h);
    }
  });

  it('derives the seed from viewpoint and forecast run only', () => {
    const s1 = deriveSeed('rainier.paradise-mountain', '2026-11-18T12:00:00Z');
    expect(s1).toMatch(/^[0-9a-f]{16}$/);
    expect(deriveSeed('rainier.paradise-mountain', '2026-11-18T12:00:00Z')).toBe(s1);
    expect(deriveSeed('rainier.paradise-mountain', '2026-11-18T13:00:00Z')).not.toBe(s1);
    expect(deriveSeed('point-reyes.beach', '2026-11-18T12:00:00Z')).not.toBe(s1);
    expect(deriveSeed(base.viewpoint_id, base.run_init_utc)).toBe(base.seed);
  });

  it('measures the lead from the run and advects from there', () => {
    expect(leadSeconds(base)).toBe(7 * 3600);
    expect(sceneAdvection(base)).toEqual([base.forecast.wind_u_ms * 7 * 3600, base.forecast.wind_v_ms * 7 * 3600]);
    const s = clone(base) as unknown as SceneSpec;
    s.forecast.wind_series = [
      { lead_h: 0, u_ms: 0, v_ms: 0 },
      { lead_h: 7, u_ms: base.forecast.wind_u_ms, v_ms: base.forecast.wind_v_ms },
    ];
    validateScene(s);
    expect(sceneAdvection(s)[0]).toBeCloseTo(0.5 * base.forecast.wind_u_ms * 7 * 3600, 6);
  });
});
