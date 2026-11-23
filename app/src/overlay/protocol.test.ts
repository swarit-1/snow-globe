import { readFileSync } from 'node:fs';
import { Ajv2020 } from 'ajv/dist/2020.js';
import { describe, expect, it } from 'vitest';
import { schemaText, SCHEMA_PATH } from '../scripts/schema.ts';
import { inboundShapeProblem, isFrameUrl, outboundProblem, parseInbound, protocolSchema, TRANSCRIPT_MAX, type Inbound } from './protocol.ts';
import { coreQuery, inboundExamples, PARSER_CASES, parsedMessage, syntheticUrl, tierFrame } from './samples.ts';

const schema = protocolSchema();
const ajv = new Ajv2020({ allErrors: true, strict: true });
const ajvInbound = ajv.compile(schema);
const ajvOutbound = ajv.getSchema(`${schema.$id as string}#/$defs/outbound`)!;

type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
const clone = <T>(v: T): T => structuredClone(v);

/** Every path to a value in `v` (object keys and array indexes). */
function paths(v: Json, at: (string | number)[] = []): (string | number)[][] {
  const out: (string | number)[][] = [];
  if (Array.isArray(v)) v.forEach((x, i) => out.push([...at, i], ...paths(x, [...at, i])));
  else if (v !== null && typeof v === 'object') for (const [k, x] of Object.entries(v)) out.push([...at, k], ...paths(x, [...at, k]));
  return out;
}

function setAt(root: Json, path: (string | number)[], value: Json | undefined): Json {
  const r = clone(root);
  let cur: any = r;
  for (const p of path.slice(0, -1)) cur = cur[p];
  const last = path[path.length - 1]!;
  if (value === undefined) {
    if (Array.isArray(cur)) cur.splice(last as number, 1);
    else delete cur[last];
  } else cur[last] = value;
  return r;
}

const REPLACEMENTS: (Json | undefined)[] = [undefined, null, true, 0, -1, 1.5, 2 ** 60, '', 'x'.repeat(2100), '2026-02-30T10:00:00Z', [], {}, ['a'], { z: 1 }];

/** Extra keys: an unknown one, and names of Object.prototype members, which must be refused like any other. */
const EXTRA_KEYS = ['unexpected_key', 'constructor', '__proto__', 'toString', 'hasOwnProperty', 'valueOf'];

/** `msg` with an own property `key` added at `path`, built through JSON text (an assignment to `__proto__` would not create one). */
function withKey(msg: Json, path: (string | number)[], key: string): Json {
  const marker = '"__extra_marker__":1';
  const r = setAt(msg, [...path, '__extra_marker__'], 1);
  return JSON.parse(JSON.stringify(r).replace(marker, `${JSON.stringify(key)}:1`)) as Json;
}

/** Many near-misses of a valid message: each value deleted or replaced, and extra keys at each object. */
function mutations(msg: Json): Json[] {
  const out: Json[] = [];
  for (const p of paths(msg)) for (const r of REPLACEMENTS) out.push(setAt(msg, p, r));
  for (const p of [[], ...paths(msg)]) {
    const target = p.reduce<any>((c, k) => c[k], msg);
    if (target !== null && typeof target === 'object' && !Array.isArray(target)) for (const k of EXTRA_KEYS) out.push(withKey(msg, p, k));
  }
  return out;
}

describe('protocol schema', () => {
  it('is the committed protocol.schema.json (run `node scripts/schema.ts` after changing protocol.ts)', () => {
    expect(readFileSync(SCHEMA_PATH, 'utf8')).toBe(schemaText());
  });

  it('compiles under Ajv strict mode (JSON Schema 2020-12)', () => {
    expect(typeof ajvInbound).toBe('function');
    expect(typeof ajvOutbound).toBe('function');
  });

  it('accepts one example of every inbound type in both the schema and parseInbound', () => {
    const examples = inboundExamples();
    const types = new Set(examples.map((e) => e.type));
    expect(types.size).toBe(17);
    for (const e of examples) {
      expect(ajvInbound(e), `${String(e.type)}: ${JSON.stringify(ajvInbound.errors)}`).toBe(true);
      const r = parseInbound(JSON.stringify(e));
      expect(r.ok, `${String(e.type)}: ${r.ok ? '' : r.problem.detail}`).toBe(true);
    }
  });

  it('accepts every query the core parser printed (real output, fixtures/core-parser-outputs.json)', () => {
    const parsed = PARSER_CASES.filter((c) => c.exit === 0);
    expect(parsed.length).toBeGreaterThanOrEqual(12);
    for (const c of parsed) {
      const m = parsedMessage(7, c.utterance);
      expect(ajvInbound(m), `${c.utterance}: ${JSON.stringify(ajvInbound.errors)}`).toBe(true);
      const r = parseInbound(m);
      expect(r.ok, `${c.utterance}: ${r.ok ? '' : r.problem.detail}`).toBe(true);
    }
  });

  it('maps the parser errors to no_match and out_of_range messages', () => {
    const noMatch = PARSER_CASES.find((c) => c.output.error === 'no_match')!;
    const nm = { v: 1, type: 'no_match', query_gen: 2, transcript: noMatch.utterance, closest: noMatch.output.closest };
    expect(parseInbound(nm).ok).toBe(true);
    const oor = PARSER_CASES.find((c) => c.output.error === 'out_of_range')!.output;
    const om = {
      v: 1,
      type: 'out_of_range',
      query_gen: 2,
      reason: oor.reason,
      place_id: oor.place_id,
      viewpoint_id: oor.viewpoint_id,
      tz: 'America/Los_Angeles',
      requested: oor.requested,
      latest_valid: oor.latest_valid,
      display: { place_name: 'Mount Rainier', viewpoint_name: 'Paradise camera' },
    };
    expect(parseInbound(om).ok).toBe(true);
  });

  it('agrees with Ajv on thousands of near-miss messages (the validator and the schema are one rule)', () => {
    let n = 0;
    let rejected = 0;
    for (const e of inboundExamples()) {
      for (const m of mutations(e as Json)) {
        const a = ajvInbound(m) === true;
        let b: boolean;
        try {
          b = inboundShapeProblem(m) === null;
        } catch (err) {
          throw new Error(`the validator threw on ${JSON.stringify(m).slice(0, 300)}: ${String(err)}`);
        }
        expect(b, `schema says ${a} for ${JSON.stringify(m).slice(0, 300)}`).toBe(a);
        // parseInbound is the schema plus semantic rules: never looser.
        if (!a) expect(parseInbound(m).ok).toBe(false);
        n += 1;
        if (!a) rejected += 1;
      }
    }
    console.log(`schema agreement: ${n} near-miss messages, ${rejected} refused by both, the rest accepted by both`);
    expect(n).toBeGreaterThan(3000);
    expect(rejected / n).toBeGreaterThan(0.8);
  });

  it('refuses what the schema cannot say: unreal dates, unknown zones and locales, two different valid instants', () => {
    const cases: [string, Json][] = [];
    const p = parsedMessage(1, 'is rainier out tomorrow at 4') as unknown as Json;
    cases.push(['30 February', setAt(setAt(p, ['query', 'valid_time'], '2026-02-30T16:00:00-08:00'), ['query', 'valid_time_utc'], '2026-03-03T00:00:00Z')]);
    cases.push(['an unknown zone', setAt(p, ['query', 'tz'], 'America/Atlantis')]);
    cases.push(['valid_time and valid_time_utc disagree', setAt(p, ['query', 'valid_time_utc'], '2026-11-21T01:00:00Z')]);
    const f = tierFrame(1, '0123456789abcdef', 0) as unknown as Json;
    cases.push(['a photo taken on 31 April', setAt(f, ['label', 'taken_at'], '2025-04-31T10:00:00-07:00')]);
    const s = inboundExamples().find((e) => e.type === 'settings') as Json;
    cases.push(['an unknown locale', setAt(s, ['locale'], 'xx-ZZZZZZZZ')]);
    for (const [name, m] of cases) {
      expect(ajvInbound(m), `${name}: the schema accepts it`).toBe(true);
      const r = parseInbound(m);
      expect(r.ok, name).toBe(false);
    }
  });

  it('keeps a tier and its label together: a simulated frame can never carry a photo label', () => {
    const f = tierFrame(1, '0123456789abcdef', 1) as unknown as Json;
    const wrong = setAt(f, ['label'], tierFrame(1, '0123456789abcdef', 0).label as unknown as Json);
    expect(ajvInbound(wrong)).toBe(false);
    expect(parseInbound(wrong).ok).toBe(false);
    const wrong2 = setAt(tierFrame(1, '0123456789abcdef', 0) as unknown as Json, ['label'], tierFrame(1, '0123456789abcdef', 1).label as unknown as Json);
    expect(parseInbound(wrong2).ok).toBe(false);
  });

  it('carries scene hashes as hex strings, never as numbers (a u64 does not fit a JavaScript number)', () => {
    const f = tierFrame(1, '0123456789abcdef', 1) as unknown as Json;
    expect(parseInbound(setAt(f, ['scene_hash'], 12_345_678_901_234_567_890)).ok).toBe(false);
    expect(parseInbound(setAt(f, ['scene_hash'], '0123456789ABCDEF')).ok).toBe(false);
    expect(parseInbound(setAt(f, ['query_gen'], 2 ** 53)).ok).toBe(false);
    expect(parseInbound(setAt(f, ['query_gen'], 0)).ok).toBe(false);
  });

  it('reports refusals by path and rule, never with the content (no transcript can reach a log)', () => {
    const secret = 'MARKER-is-rainier-out-tomorrow';
    const bad: unknown[] = [
      { v: 1, type: 'partial', query_gen: 1, text: secret + 'x'.repeat(TRANSCRIPT_MAX), stable: true },
      { v: 1, type: 'partial', query_gen: 1, text: secret, stable: 'yes' },
      { v: 1, type: 'no_match', query_gen: 1, transcript: secret, closest: [{ place_id: secret, place: 'x', score: 2 }] },
      { v: 1, type: 'partial', query_gen: 1, text: secret, stable: true, [secret]: 1 },
      `{"v":1,"type":"partial","text":"${secret}"`,
      { v: 2, type: secret },
    ];
    for (const b of bad) {
      const r = parseInbound(b);
      expect(r.ok).toBe(false);
      if (!r.ok) {
        expect(r.problem.detail).not.toContain('MARKER');
        expect(r.problem.messageType).not.toContain('MARKER');
      }
    }
  });

  it('accepts the outbound messages the overlay sends, and refuses malformed ones', () => {
    const good: Record<string, unknown>[] = [
      { v: 1, type: 'ready', overlay_version: '0.1.0', time_origin_ms: 1_763_000_000_000 },
      { v: 1, type: 'paint', query_gen: 3, scene_hash: '0123456789abcdef', tier: 0, outcome: 'painted', faded: false, received_ms: 10, prepared_ms: 12, frame_ms: 16.7, committed_ms: 17, half_ms: 16.7 },
      { v: 1, type: 'paint', query_gen: 3, scene_hash: '0123456789abcdef', tier: 1, outcome: 'dropped', reason: 'scene', faded: false, received_ms: 10 },
      { v: 1, type: 'destination_shown', seq: 2, query_gen: 3, frame_ms: 33.4 },
      { v: 1, type: 'paint', query_gen: 3, scene_hash: '0123456789abcdef', tier: 0, outcome: 'painted', faded: false, prefetched: true, previous_ms: 41.5, received_ms: 10, frame_ms: 16.7, committed_ms: 17, half_ms: 16.7 },
      { v: 1, type: 'dismiss', query_gen: null },
      { v: 1, type: 'scrub', query_gen: 3, minutes: -10 },
      { v: 1, type: 'flip_meridiem', query_gen: 3 },
      { v: 1, type: 'copy', query_gen: 3, png_base64: 'iVBORw0KGgo=', refined: false },
      { v: 1, type: 'use_offer', query_gen: 5, valid_time: '2026-11-26T08:00:00-08:00' },
      { v: 1, type: 'open_url', url: 'https://open-meteo.com/' },
      { v: 1, type: 'protocol_error', code: 'invalid_message', message_type: 'partial', detail: 'message.stable: expected a boolean, got string' },
    ];
    for (const g of good) {
      expect(ajvOutbound(g), `${String(g.type)}: ${JSON.stringify(ajvOutbound.errors)}`).toBe(true);
      expect(outboundProblem(g)).toBeNull();
      for (const m of mutations(g as Json)) expect(outboundProblem(m) === null, JSON.stringify(m).slice(0, 200)).toBe(ajvOutbound(m) === true);
    }
    expect(outboundProblem({ v: 1, type: 'open_url', url: 'http://example.com' })).not.toBeNull();
  });

  it('refuses a key named after an Object.prototype member like any other unknown key, and never throws', () => {
    for (const k of EXTRA_KEYS) {
      const inbound = `{"v":1,"type":"network","online":true,${JSON.stringify(k)}:1}`;
      const r = parseInbound(inbound);
      expect(r.ok, k).toBe(false);
      if (!r.ok) expect(r.problem.detail).toBe('message: unexpected property');
      const outbound = JSON.parse(`{"v":1,"type":"flip_meridiem","query_gen":3,${JSON.stringify(k)}:1}`) as unknown;
      expect(outboundProblem(outbound), k).toBe('message: unexpected property');
    }
  });

  it('types each ambiguity by its field: AM/PM with a real RFC 3339 alternative, or place and viewpoint IDs (query.rs Ambiguity)', () => {
    const p = parsedMessage(1, 'is rainier out tomorrow at 4') as unknown as Json;
    expect(parseInbound(p).ok).toBe(true);
    const at = ['query', 'ambiguities', 0];
    const cases: [string, Json, boolean][] = [
      ['an alternative that is not a time', setAt(p, [...at, 'alternative'], '4 AM'), false],
      ['an alternative on 30 February', setAt(p, [...at, 'alternative'], '2026-02-30T04:00:00-08:00'), false],
      ['chosen neither am nor pm', setAt(p, [...at, 'chosen'], 'evening'), false],
      ['a place ambiguity with place IDs', setAt(p, at, { field: 'place', chosen: 'rainier', alternative: 'point-reyes' }), true],
      ['a place ambiguity with a time', setAt(p, at, { field: 'place', chosen: 'rainier', alternative: '2026-11-20T04:00:00-08:00' }), false],
      ['a viewpoint ambiguity with viewpoint IDs', setAt(p, at, { field: 'viewpoint', chosen: 'rainier.paradise-mountain', alternative: 'rainier.paradise-east' }), true],
      ['an unknown field', setAt(p, at, { field: 'units', chosen: 'us', alternative: 'metric' }), false],
    ];
    for (const [name, m, ok] of cases) {
      expect(parseInbound(m).ok, name).toBe(ok);
      if (name !== 'an alternative on 30 February') expect(ajvInbound(m), name).toBe(ok); // the schema says the shape; the date rule is semantic
    }
  });

  it('carries a long transcript (a long hold): never refuses a terminal message over a display length', () => {
    const long = 'is rainier out '.repeat(667).slice(0, TRANSCRIPT_MAX);
    expect(parseInbound({ v: 1, type: 'partial', query_gen: 1, text: long, stable: false }).ok).toBe(true);
    expect(parseInbound({ v: 1, type: 'no_match', query_gen: 1, transcript: long, closest: [] }).ok).toBe(true);
    expect(parseInbound({ v: 1, type: 'no_match', query_gen: 1, transcript: `${long}x`, closest: [] }).ok).toBe(false);
  });

  it('accepts a u64 diagnostic clock (core_ns), so no message is refused after 104 days of uptime', () => {
    const f = tierFrame(1, '0123456789abcdef', 0) as unknown as Json;
    for (const ns of [9.1e15, 2 ** 63, 18_446_744_073_709_551_615]) {
      const m = setAt(f, ['core_ns'], ns);
      expect(parseInbound(m).ok, String(ns)).toBe(true);
      expect(ajvInbound(m), String(ns)).toBe(true);
    }
    expect(parseInbound(setAt(f, ['core_ns'], -1)).ok).toBe(false);
  });

  it('loads frames only from app-local URLs, never a remote camera (PRD §15.3)', () => {
    const f = tierFrame(1, '0123456789abcdef', 0) as unknown as Json;
    for (const url of ['https://www.nps.gov/webcams-mora/mountain.jpg', 'http://localhost/frame.jpg', 'file:///Users/x/frame.jpg', 'data:image/jpeg;base64,AAAA', 'snowglobe://evil.example/frame.jpg', 'blob:https://x/1']) {
      expect(parseInbound(setAt(f, ['frame', 'url'], url)).ok, url).toBe(false);
      expect(isFrameUrl(url), url).toBe(false);
    }
    for (const url of [syntheticUrl('tier0'), 'asset://localhost/%2FUsers%2Fx%2Fthumbs%2Fa.jpg']) {
      expect(parseInbound(setAt(f, ['frame', 'url'], url)).ok, url).toBe(true);
      expect(isFrameUrl(url), url).toBe(true);
    }
    const pre = { v: 1, type: 'prefetch', frames: [{ url: 'https://www.nps.gov/x.jpg', width: 640, height: 360 }] };
    expect(parseInbound(pre).ok).toBe(false);
  });

  it('narrows message types for TypeScript', () => {
    const r = parseInbound(parsedMessage(9, 'grand canyon golden hour'));
    expect(r.ok).toBe(true);
    if (r.ok && r.msg.type === 'parsed') {
      const m: Inbound = r.msg;
      expect(m.type).toBe('parsed');
      expect(r.msg.query.time_source.interval?.start).toBe(coreQuery('grand canyon golden hour').time_source.interval!.start);
    }
  });
});
