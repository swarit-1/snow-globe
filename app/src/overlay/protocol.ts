/**
 * The message protocol between the Rust core and the overlay's webview (PRD §6.1, §6.3, §10.3).
 *
 * Inbound messages (core → webview) drive the states of PRD §5.2: a key press starts a query
 * (`query_started`), partial transcripts stream in (`partial`, `audio_level`), the key comes up
 * (`released`), the query is parsed (`parsed`) or scrubbed and flipped (`scene_changed`), tiers
 * start and become ready (`tier_started`, `tier_frame_ready`) or are refused (`tier_rejected`),
 * and the query can end as No match, Out of range, cancelled (Esc) or an error. Settings,
 * the destination chip (PRD §5.1, §15.1) and the network state are not tied to a query.
 *
 * Outbound messages (webview → core) report paints with their page timestamps (diagnostics only,
 * PRD §10.4), acknowledge a destination chip once it is on screen, and forward the keys of PRD §5.4.
 *
 * Rules the transport must keep, which no schema can state:
 * - **Order.** The core delivers inbound messages in the order it sends them, on one channel.
 * - **Generations.** `query_gen` is the core's `QueryGen` (lifecycle.rs): it rises by one per key
 *   press, starting at 1, and stays below 2^53 so a JavaScript number holds it exactly.
 * - **Scene hashes** are 16 lower-case hex digits (the u64 `SceneHash`), never a JSON number.
 *   lifecycle.rs serialises `SceneHash` as a bare number today, so the shell converts it.
 * - **Tiers** are the integers 0, 1 and 2. lifecycle.rs serialises `Tier` as "tier0", "tier1" and
 *   "tier2", so the shell maps them, both ways (inbound tiers and the outbound `paint` report).
 * - **Frames** are served by the shell under an app-local URL (`snowglobe://localhost/…` or Tauri's
 *   `asset://localhost/…`); the schema refuses any other URL, so the webview never fetches a remote
 *   camera (PRD §15.3).
 * - **Every tier gets an answer.** For each scene the core sends a frame or `tier_rejected` for every
 *   tier that is on, so the marks settle and a scene that can show nothing says so.
 * - **The destination chip comes first.** The core sends no byte to a cloud destination in a key
 *   press until `destination_shown` for that destination's `seq` and that `query_gen` arrives (PRD §15.1).
 * - **No text in diagnostics.** Outbound `paint` and `protocol_error` carry ids, codes and times only
 *   (PRD §10.5: traces never contain transcript text).
 *
 * protocol.schema.json is generated from these definitions (scripts/schema.ts writes it; a test checks
 * the committed file is current). `parseInbound` applies the schema plus the semantic rules it cannot
 * express: timestamps are real instants, time zones are real IANA zones, a locale is a real tag, and a
 * query's local and UTC valid times are the same instant.
 */
import {
  arr,
  bool,
  discriminated,
  emptyObj,
  enumOf,
  int,
  lit,
  nullable,
  num,
  obj,
  str,
  type Infer,
  type JsonSchema,
  type ObjV,
  type V,
} from './schema.ts';

/** Protocol version carried in every message as `v`. */
export const PROTOCOL_VERSION = 1 as const;

/** The largest generation a message may carry (2^53 − 1). */
export const MAX_GEN = Number.MAX_SAFE_INTEGER;

/**
 * The longest transcript a message may carry, in code points: about nine minutes of speech. It is a
 * transport guard, far above any display length, so a long hold never makes the core's terminal
 * message (No match) bounce; the view clips what it shows. The core truncates beyond it.
 */
export const TRANSCRIPT_MAX = 10_000;

/** The largest u64 (as a JavaScript number it rounds to 2^64; diagnostics only). */
const U64_MAX = 2 ** 64 - 1;

/** App-local frame URLs: the shell's own scheme, or Tauri's asset protocol (macOS forms). */
export const FRAME_URL_PATTERN = '^(snowglobe|asset)://localhost/';
const FRAME_URL_RE = new RegExp(FRAME_URL_PATTERN, 'u');

/** True if `url` is an app-local frame URL, the only kind the webview loads (PRD §15.3). */
export function isFrameUrl(url: string): boolean {
  return FRAME_URL_RE.test(url);
}

const DATE = '\\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\\d|3[01])';
const TIME = '([01]\\d|2[0-3]):[0-5]\\d:[0-5]\\d(\\.\\d{1,9})?';
/** RFC 3339 with a numeric offset ("2026-11-20T16:00:00-08:00"). */
export const RFC3339_OFFSET = `^${DATE}T${TIME}[+-]([01]\\d|2[0-3]):[0-5]\\d$`;
/** RFC 3339 in UTC ("2026-11-21T00:00:00Z"). */
export const RFC3339_UTC = `^${DATE}T${TIME}Z$`;

/** Shared definitions, emitted under $defs and referenced with $ref. */
const DEFS: Record<string, JsonSchema> = {};
function def<T>(name: string, v: V<T>): V<T> {
  if (Object.hasOwn(DEFS, name)) throw new Error(`protocol: ${name} defined twice`);
  DEFS[name] = v.schema;
  return { schema: { $ref: `#/$defs/${name}` }, validate: (x, path) => v.validate(x, path) };
}

const v1 = lit(PROTOCOL_VERSION, { description: 'Protocol version.' });
const gen = int({ min: 1, max: MAX_GEN, description: 'The query generation (lifecycle.rs QueryGen): one per key press, rising from 1.' });
const sceneHash = str({ pattern: '^[0-9a-f]{16}$', description: 'The 64-bit SceneHash of what the frame shows, as 16 lower-case hex digits.' });
const tierAny = enumOfInts([0, 1, 2], 'The tier as an integer: lifecycle.rs Tier0 = 0, Tier1 = 1, Tier2 = 2 (the core serialises "tier0", "tier1", "tier2"; the shell maps them).');
const coreNs = int({
  min: 0,
  max: U64_MAX,
  description: "Optional, diagnostics only: the core's monotonic clock at send, in ns (a u64; above 2^53 a JavaScript number rounds it, so the webview never computes with it).",
});
const offsetTime = str({ pattern: RFC3339_OFFSET, description: 'RFC 3339 with the place offset.' });
const utcTime = str({ pattern: RFC3339_UTC, description: 'RFC 3339 in UTC.' });
const tz = str({ min: 1, max: 64, pattern: '^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$', description: 'An IANA time zone.' });
const placeId = str({ min: 1, max: 64, pattern: '^[a-z0-9]+(-[a-z0-9]+)*$' });
const viewpointId = str({ min: 1, max: 100, pattern: '^[a-z0-9]+(-[a-z0-9]+)*\\.[a-z0-9]+(-[a-z0-9]+)*$' });
const label = (max: number): V<string> => str({ min: 1, max });

function enumOfInts<const L extends readonly number[]>(values: L, description?: string): V<L[number]> {
  const set = new Set<unknown>(values);
  return {
    schema: description === undefined ? { enum: [...values] } : { enum: [...values], description },
    validate: (v, path) => (set.has(v) ? null : `${path}: expected one of ${values.join(', ')}`),
  };
}

// ---------------------------------------------------------------------------------------------
// The core's query (PRD §7.3 plus the four fields of D-040), exactly as snow-globe-core serialises it.

export const TIME_KINDS = ['now', 'clock', 'solar', 'part_of_day', 'relative', 'day'] as const;
export const TIME_RULES = [
  'no-time', 'now', 'relative', 'bare-hour', 'explicit-hour', 'day-plus-bare-hour', 'day-plus-explicit-hour', 'noon',
  'midnight', 'sunrise', 'sunset', 'dawn', 'dusk', 'golden-hour', 'blue-hour', 'part-of-day', 'tonight', 'day-only',
  'day-only-now',
] as const;
export const SOLAR_EVENTS = [
  'sunrise', 'sunset', 'dawn', 'dusk', 'morning_golden_hour', 'evening_golden_hour', 'morning_blue_hour', 'evening_blue_hour',
] as const;
export const FLAGS = ['live', 'rolled_to_tomorrow', 'rolled_to_next_hour', 'rolled_to_next_week', 'dst_gap', 'dst_fold', 'gfs_horizon'] as const;

const interval = obj({
  start: offsetTime,
  end: offsetTime,
  start_altitude_deg: num({ min: -90, max: 90 }),
  end_altitude_deg: num({ min: -90, max: 90 }),
});

const timeSource = obj(
  {
    kind: enumOf(TIME_KINDS),
    text: str({ max: TRANSCRIPT_MAX, description: 'The time words as heard (never written to traces).' }),
    rule: enumOf(TIME_RULES),
    event: enumOf(SOLAR_EVENTS),
    sun_altitude_deg: num({ min: -90, max: 90 }),
    interval,
  },
  { optional: ['event', 'sun_altitude_deg', 'interval'] },
);

/**
 * A chip the user can flip (query.rs `Ambiguity`), told apart by `field` as the core fills it:
 * AM/PM with the other reading as an RFC 3339 time, or two place or viewpoint IDs.
 */
const ambiguity = discriminated(
  'field',
  [
    obj({ field: lit('meridiem'), chosen: enumOf(['am', 'pm']), alternative: offsetTime }),
    obj({ field: lit('place'), chosen: placeId, alternative: placeId }),
    obj({ field: lit('viewpoint'), chosen: viewpointId, alternative: viewpointId }),
  ],
  { description: 'query.rs Ambiguity: meridiem ("am" or "pm", the other reading as RFC 3339), or the chosen and the other place or viewpoint ID.' },
);

export const queryV = def('query', obj(
  {
    place_id: placeId,
    viewpoint_id: viewpointId,
    valid_time: offsetTime,
    valid_time_utc: utcTime,
    tz,
    time_source: timeSource,
    ambiguities: arr(ambiguity, { max: 4 }),
    overrides: emptyObj({ description: 'Overrides are Later (PRD §4.3); version 1 carries only {}.' }),
    path: enumOf(['fast_path', 'fallback']),
    confidence: num({ min: 0, max: 1 }),
    place_source: enumOf(['utterance', 'context']),
    place_match: obj({ stage: enumOf(['alias', 'exact', 'fuzzy', 'phonetic', 'context']), score: num({ min: 0, max: 1 }) }),
    horizon: enumOf(['hrrr', 'gfs']),
    flags: arr(enumOf(FLAGS), { max: FLAGS.length, unique: true }),
  },
  { description: 'The structured query of PRD §7.3, as snow-globe-core serialises it (with the D-040 fields).' },
));

/** Names for the chips and captions, from the viewpoint manifest. */
const display = def('display', obj(
  {
    place_name: label(80),
    viewpoint_name: label(80),
    alternative_place: label(80),
  },
  { optional: ['alternative_place'], description: 'Names from the manifest; alternative_place names the other reading of a place ambiguity.' },
));

/** A credit line from the licence ledger (PRD §15.4), with a link where its terms ask for one. */
const credit = obj({ text: label(200), url: str({ min: 1, max: 500, pattern: '^https://' }) }, { optional: ['url'] });
const credits = def('credits', arr(credit, { min: 1, max: 4, description: 'Credit lines from the licence ledger (PRD §15.4).' }));

// ---------------------------------------------------------------------------------------------
// Frames and their labels (the caption data of PRD §5.3).

const frame = def('frame', obj(
  {
    url: str({ min: 1, max: 2048, pattern: FRAME_URL_PATTERN, description: 'Where the webview loads the pixels: an app-local URL the shell serves, never a remote camera (PRD §15.3).' }),
    width: int({ min: 1, max: 8192 }),
    height: int({ min: 1, max: 8192 }),
  },
  { description: 'The frame to paint.' },
));

export const MATCHED_ON = ['sun_position', 'time_of_year', 'forecast_class'] as const;

const photoLabel = def('photo_label', obj({
  kind: lit('photo'),
  taken_at: offsetTime,
  tz,
  source: enumOf(['capture', 'archive']),
  camera_name: label(80),
  matched_on: arr(enumOf(MATCHED_ON), { min: 1, max: MATCHED_ON.length, unique: true }),
  credits,
}));

const run = obj({
  model: enumOf(['hrrr', 'gfs']),
  cycle_utc: utcTime,
  available_at: utcTime,
  pinned: bool(),
});

const forecast = obj({
  cloud_low: num({ min: 0, max: 1 }),
  cloud_mid: num({ min: 0, max: 1 }),
  cloud_high: num({ min: 0, max: 1 }),
  ceiling_m: nullable(num({ min: 0, max: 30_000 })),
  visibility_m: nullable(num({ min: 0, max: 1_000_000 })),
  precip_rate_mm_h: nullable(num({ min: 0, max: 500 })),
  precip_type: nullable(enumOf(['rain', 'snow', 'mixed', 'freezing'])),
});

const moon = obj({ illuminated: num({ min: 0, max: 1 }), altitude_deg: num({ min: -90, max: 90 }) });

const simProps = {
  valid_time: offsetTime,
  tz,
  run,
  forecast,
  sun_altitude_deg: num({ min: -90, max: 90 }),
  moon,
  credits,
};

const simulatedLabel = def('simulated_label', obj({ kind: lit('simulated'), ...simProps }));
const refinedLabel = def('refined_label', obj({ kind: lit('refined'), ...simProps, refine: obj({ model: label(80), provider: label(40) }) }));

// ---------------------------------------------------------------------------------------------
// Inbound messages.

const msg = <const T extends string, P extends Record<string, V<unknown>>>(type: T, props: P, description: string) =>
  obj({ v: v1, type: lit(type), ...props, core_ns: coreNs }, { optional: ['core_ns'], description });

const queryStarted = msg('query_started', { query_gen: gen }, 'Key down: a new query began; everything older is cancelled (PRD §5.2 Listening).');
const partial = msg(
  'partial',
  { query_gen: gen, text: str({ max: TRANSCRIPT_MAX }), stable: bool({ description: 'True for a stable partial (PRD §7.1 lever 2).' }) },
  'A partial transcript for the listening pill (the view shows its end).',
);
const audioLevel = msg('audio_level', { query_gen: gen, level: num({ min: 0, max: 1 }) }, 'Microphone level for the waveform (a level only, never audio).');
const released = msg('released', { query_gen: gen }, 'Key up: listening ends and the query resolves (PRD §5.2 Resolving).');
const parsed = msg(
  'parsed',
  { query_gen: gen, scene_hash: sceneHash, query: queryV, display },
  'The query was parsed and resolved to a scene; the chips can show. It may come on a stable partial, before key-up (PRD §7.2); a later No match or Out of range for the query then withdraws the scene.',
);
const sceneChanged = msg(
  'scene_changed',
  { query_gen: gen, scene_hash: sceneHash, cause: enumOf(['scrub', 'meridiem_flip', 'place_choice', 'use_offer']), query: queryV, display },
  'The same query moved to another scene (scrubbed, AM/PM flipped, another place chosen, or the Out of range offer taken); tiers start again. It is also the answer to use_offer.',
);
const tierStarted = msg(
  'tier_started',
  { query_gen: gen, scene_hash: sceneHash, tier: tierAny, eta_ms: num({ min: 0, max: 60_000 }) },
  'A tier began rendering and is expected in eta_ms (feeds the Tier 0 skip rule, D-012 row 11).',
);
const frameProps = { v: v1, type: lit('tier_frame_ready'), query_gen: gen, scene_hash: sceneHash, frame, core_ns: coreNs };
const tierFrameReady = discriminated(
  'tier',
  [
    obj({ ...frameProps, tier: lit(0), label: photoLabel }, { optional: ['core_ns'] }),
    obj({ ...frameProps, tier: lit(1), label: simulatedLabel }, { optional: ['core_ns'] }),
    obj({ ...frameProps, tier: lit(2), label: refinedLabel }, { optional: ['core_ns'] }),
  ],
  { description: 'A tier frame is ready to paint. Tier 0 is a real photo, Tier 1 simulated, Tier 2 simulated with AI texture.' },
);
const tierRejected = msg(
  'tier_rejected',
  {
    query_gen: gen,
    scene_hash: sceneHash,
    tier: tierAny,
    reason: enumOf(['registration', 'structure', 'brightness', 'colour', 'fog', 'late', 'spend_cap', 'provider_error', 'disabled']),
  },
  'A tier will not paint for this scene (PRD §9.7: Tier 2 rejected or late shows a hollow third mark).',
);
const noMatch = msg(
  'no_match',
  {
    query_gen: gen,
    transcript: str({ max: TRANSCRIPT_MAX }),
    closest: arr(obj({ place_id: placeId, place: label(80), score: num({ min: 0, max: 1 }) }), { max: 3 }),
  },
  'No place matched: show the transcript and the closest known places, never a guess (PRD §5.2, §7.5). Withdraws any scene of the query: nothing of it stays on screen.',
);
const outOfRange = msg(
  'out_of_range',
  {
    query_gen: gen,
    reason: enumOf(['past', 'beyond_horizon']),
    place_id: placeId,
    viewpoint_id: viewpointId,
    tz,
    requested: offsetTime,
    latest_valid: nullable(offsetTime),
    display,
  },
  'The time is in the past or beyond 7 days: show the horizon and offer the latest valid time (PRD §5.2, §7.4). Withdraws any scene of the query; taking the offer (use_offer) brings scene_changed.',
);
const cancelled = msg(
  'cancelled',
  { query_gen: gen, reason: enumOf(['esc', 'no_speech', 'shutdown']) },
  'The query ended (Esc, or key-up with no speech): the overlay goes idle and nothing of it paints again (PRD §5.2).',
);
export const ERROR_CODES = ['speech_unavailable', 'microphone_denied', 'not_understood', 'assets_unavailable', 'render_failed', 'internal'] as const;
const error = msg(
  'error',
  { query_gen: nullable(gen), code: enumOf(ERROR_CODES), fatal: bool({ description: 'True if the query cannot go on; it then ends like Esc.' }) },
  'Something failed. Codes only: no free text, so no transcript can reach a log.',
);
const settings = msg(
  'settings',
  {
    on_device_only: bool({ description: 'Disables every cloud path (PRD §15.1).' }),
    cloud_speech: bool({ description: 'The cloud speech toggle (off by default, D-015).' }),
    tier2: bool({ description: 'Whether Tier 2 refinement is on.' }),
    locale: str({ min: 2, max: 35, pattern: '^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$' }),
    units: enumOf(['metric', 'us']),
    reduce_motion: nullable(bool({ description: 'Overrides the system setting; null follows prefers-reduced-motion.' })),
    measuring: bool({ description: 'Harness mode: draw the pixel tag and show MEASURING (PRD §10.4, §15.2).' }),
  },
  'App settings that change what the overlay shows.',
);
export const PROVIDERS_CARRY = ['audio', 'text', 'image'] as const;
const destination = msg(
  'destination',
  {
    seq: int({ min: 1, max: MAX_GEN }),
    destinations: arr(obj({ provider: label(40), carries: arr(enumOf(PROVIDERS_CARRY), { min: 1, max: 3, unique: true }) }), { max: 4 }),
  },
  'Where audio, text or images will go. Empty means on-device. In each key press the core sends no byte to a cloud destination until destination_shown for this seq and that query_gen arrives (PRD §15.1).',
);
const network = msg('network', { online: bool() }, 'Network state: offline shows cached tiers with a "forecast from" time (PRD §5.2).');
const prefetch = msg(
  'prefetch',
  { frames: arr(frame, { min: 1, max: 16 }) },
  'Frames the core expects to ask for soon (a stable partial named the place, PRD §6.3 step 2, §9.1): the webview decodes them in the background, so a later tier_frame_ready for the same URL is a swap. Nothing paints from it.',
);

/** Every inbound message, told apart by `type` (tier_frame_ready further by `tier`). */
const INBOUND_BY_TYPE = {
  query_started: queryStarted,
  partial,
  audio_level: audioLevel,
  released,
  parsed,
  scene_changed: sceneChanged,
  tier_started: tierStarted,
  tier_frame_ready: tierFrameReady,
  tier_rejected: tierRejected,
  no_match: noMatch,
  out_of_range: outOfRange,
  cancelled,
  error,
  settings,
  destination,
  network,
  prefetch,
} as const;

export type InboundType = keyof typeof INBOUND_BY_TYPE;
export const INBOUND_TYPES = Object.keys(INBOUND_BY_TYPE) as InboundType[];

export type QueryStarted = Infer<typeof queryStarted>;
export type Partial = Infer<typeof partial>;
export type AudioLevel = Infer<typeof audioLevel>;
export type Released = Infer<typeof released>;
export type Parsed = Infer<typeof parsed>;
export type SceneChanged = Infer<typeof sceneChanged>;
export type TierStarted = Infer<typeof tierStarted>;
export type TierFrameReady = Infer<typeof tierFrameReady>;
export type TierRejected = Infer<typeof tierRejected>;
export type NoMatch = Infer<typeof noMatch>;
export type OutOfRange = Infer<typeof outOfRange>;
export type Cancelled = Infer<typeof cancelled>;
export type ErrorMsg = Infer<typeof error>;
export type Settings = Infer<typeof settings>;
export type Destination = Infer<typeof destination>;
export type NetworkMsg = Infer<typeof network>;
export type Prefetch = Infer<typeof prefetch>;
export type Inbound =
  | QueryStarted | Partial | AudioLevel | Released | Parsed | SceneChanged | TierStarted | TierFrameReady
  | TierRejected | NoMatch | OutOfRange | Cancelled | ErrorMsg | Settings | Destination | NetworkMsg | Prefetch;

export type QueryView = Infer<typeof queryV>;
export type Ambiguity = QueryView['ambiguities'][number];
export type Display = Infer<typeof display>;
export type Credit = Infer<typeof credit>;
export type FrameRef = Infer<typeof frame>;
export type PhotoLabel = Infer<typeof photoLabel>;
export type SimulatedLabel = Infer<typeof simulatedLabel>;
export type RefinedLabel = Infer<typeof refinedLabel>;
export type FrameLabel = PhotoLabel | SimulatedLabel | RefinedLabel;
export type ForecastInfo = Infer<typeof forecast>;
export type RunInfo = Infer<typeof run>;

// ---------------------------------------------------------------------------------------------
// Outbound messages.

const pageMs = num({ min: 0, max: 1e12, description: 'performance.now() of the page, in ms (diagnostic, PRD §10.4).' });
export const PAINT_OUTCOMES = ['painted', 'skipped', 'dropped', 'failed'] as const;
export const PAINT_REASONS = ['generation', 'dismissed', 'scene', 'tier', 'superseded', 'reset', 'load_failed'] as const;

const outMsg = <const T extends string, P extends Record<string, V<unknown>>, O extends keyof P & string = never>(
  type: T,
  props: P,
  description: string,
  optional: readonly O[] = [],
) => obj({ v: v1, type: lit(type), ...props }, { optional, description });

const ready = outMsg(
  'ready',
  { overlay_version: label(40), time_origin_ms: num({ min: 0, max: 1e14, description: 'performance.timeOrigin (Unix ms).' }) },
  'The page is loaded; the core may send messages.',
);
const paint = outMsg(
  'paint',
  {
    query_gen: gen,
    scene_hash: sceneHash,
    tier: tierAny,
    outcome: enumOf(PAINT_OUTCOMES),
    reason: enumOf(PAINT_REASONS),
    faded: bool({ description: 'True if the tier cross-faded in (PRD §5.5).' }),
    held_ms: num({ min: 0, max: 60_000, description: 'How long the Tier 0 skip rule held this frame.' }),
    prefetched: bool({ description: 'True if the pixels were decoded in the background before the frame was asked for (a prefetch hit), false if decoded on the key-up path.' }),
    previous_ms: num({
      min: 0,
      max: 600_000,
      description: "On the first frame of a scene after a scrub, AM/PM flip or offer: how long the previous scene's frame had stayed on screen since the scene changed (visible-wrong time, PRD §10.3).",
    }),
    received_ms: pageMs,
    prepared_ms: pageMs,
    frame_ms: pageMs,
    committed_ms: pageMs,
    half_ms: num({
      min: 0,
      max: 1e12,
      description: 'The frame callback at which the tier first stood at 50 % opacity or more. A painted report without it: the frame was drawn but replaced or cleared before its cross-fade reached 50 %.',
    }),
  },
  'What became of a tier frame, with page timestamps. Paint times here are diagnostics; the measured paint is the composited frame (PRD §10.4).',
  ['reason', 'held_ms', 'prefetched', 'previous_ms', 'prepared_ms', 'frame_ms', 'committed_ms', 'half_ms'],
);
const destinationShown = outMsg(
  'destination_shown',
  {
    seq: int({ min: 1, max: MAX_GEN }),
    query_gen: gen,
    frame_ms: num({ min: 0, max: 1e12 }),
  },
  'The destination chip for seq has been on screen for a full frame during key press query_gen: in that key press the first byte may leave now. Never sent while the panel is hidden, nor for a destination that contradicts on-device only.',
);
const dismiss = outMsg('dismiss', { query_gen: nullable(gen) }, 'Esc in the focused panel: hide and hand focus back (PRD §5.4).');
const scrub = outMsg(
  'scrub',
  { query_gen: gen, minutes: int({ min: -1440, max: 1440 }) },
  'Left or Right: scrub one hour; with Shift, ten minutes (PRD §5.4). From the time the query last asked for, so also back out of an Out of range reached by scrubbing. The core answers with scene_changed or out_of_range.',
);
const flipMeridiem = outMsg('flip_meridiem', { query_gen: gen }, 'M: flip AM and PM (PRD §5.4).');
const copy = outMsg(
  'copy',
  { query_gen: gen, png_base64: str({ min: 1, max: 40_000_000 }), refined: bool() },
  'C: the frame with its caption burned into the pixels (PRD §5.5); refined frames also need IPTC metadata, which the core adds.',
);
const useOffer = outMsg(
  'use_offer',
  { query_gen: gen, valid_time: offsetTime },
  'The user took the Out of range offer of the latest valid time. The core answers with scene_changed (cause use_offer) for the same query_gen.',
);
const openUrl = outMsg('open_url', { url: str({ min: 1, max: 500, pattern: '^https://' }) }, 'Open a credit link in the browser (the panel never navigates).');
const protocolError = outMsg(
  'protocol_error',
  {
    code: enumOf(['invalid_json', 'invalid_message', 'unsupported_version', 'generation_ahead']),
    message_type: str({ max: 40, pattern: '^[a-z_]*$' }),
    detail: str({ max: 300, description: 'Where the message failed (a path and a rule), never its content.' }),
  },
  'A message from the core was refused.',
);

const OUTBOUND_BY_TYPE = {
  ready,
  paint,
  destination_shown: destinationShown,
  dismiss,
  scrub,
  flip_meridiem: flipMeridiem,
  copy,
  use_offer: useOffer,
  open_url: openUrl,
  protocol_error: protocolError,
} as const;

export type OutboundType = keyof typeof OUTBOUND_BY_TYPE;
export type PaintReport = Infer<typeof paint>;
export type Outbound =
  | Infer<typeof ready> | PaintReport | Infer<typeof destinationShown> | Infer<typeof dismiss> | Infer<typeof scrub>
  | Infer<typeof flipMeridiem> | Infer<typeof copy> | Infer<typeof useOffer> | Infer<typeof openUrl> | Infer<typeof protocolError>;

// ---------------------------------------------------------------------------------------------
// Validation.

function byType<M extends Record<string, V<unknown>>>(map: M): V<unknown> {
  for (const [t, v] of Object.entries(map)) {
    const variants = (v.schema.oneOf as JsonSchema[] | undefined) ?? [v.schema];
    for (const s of variants) {
      const c = ((s.properties as Record<string, JsonSchema> | undefined)?.type as { const?: unknown } | undefined)?.const;
      if (c !== t) throw new Error(`protocol: variant for ${t} does not fix type`);
    }
  }
  const all = Object.values(map).flatMap((v) => (v.schema.oneOf as JsonSchema[] | undefined) ?? [v.schema]);
  return {
    schema: { oneOf: all },
    validate(v, path) {
      if (typeof v !== 'object' || v === null || Array.isArray(v)) return `${path}: expected an object`;
      const t = (v as Record<string, unknown>).type;
      const variant = typeof t === 'string' && Object.hasOwn(map, t) ? map[t] : undefined;
      if (variant === undefined) return `${path}.type: unknown message type`;
      return variant.validate(v, path);
    },
  };
}

const inboundV = byType(INBOUND_BY_TYPE);
const outboundV = byType(OUTBOUND_BY_TYPE);

/** A real instant: Date.parse accepts it and the calendar date is the one written (no 30 February). */
export function isRealInstant(s: string): boolean {
  const ms = Date.parse(s);
  if (!Number.isFinite(ms)) return false;
  const m = /^(\d{4})-(\d{2})-(\d{2})T/.exec(s);
  if (m === null) return false;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const probe = new Date(Date.UTC(y, mo - 1, d));
  return probe.getUTCFullYear() === y && probe.getUTCMonth() === mo - 1 && probe.getUTCDate() === d;
}

const zoneCache = new Map<string, boolean>();
/** True if `zone` is an IANA zone this engine knows. */
export function isTimeZone(zone: string): boolean {
  let ok = zoneCache.get(zone);
  if (ok === undefined) {
    try {
      new Intl.DateTimeFormat('en-US', { timeZone: zone });
      ok = true;
    } catch {
      ok = false;
    }
    zoneCache.set(zone, ok);
  }
  return ok;
}

/** True if `tag` is a BCP 47 locale this engine can format dates in (else Intl would fall back silently). */
export function isLocale(tag: string): boolean {
  try {
    return Intl.getCanonicalLocales(tag).length === 1 && Intl.DateTimeFormat.supportedLocalesOf(tag).length === 1;
  } catch {
    return false;
  }
}

/** The semantic rules the schema cannot express; returns the first problem or null. */
export function semanticProblem(m: Inbound): string | null {
  const instants: [string, string][] = [];
  const zones: [string, string][] = [];
  const addQuery = (q: QueryView, at: string): string | null => {
    instants.push([`${at}.valid_time`, q.valid_time], [`${at}.valid_time_utc`, q.valid_time_utc]);
    zones.push([`${at}.tz`, q.tz]);
    if (q.time_source.interval) instants.push([`${at}.time_source.interval.start`, q.time_source.interval.start], [`${at}.time_source.interval.end`, q.time_source.interval.end]);
    q.ambiguities.forEach((a, i) => {
      if (a.field === 'meridiem') instants.push([`${at}.ambiguities[${i}].alternative`, a.alternative]);
    });
    if (Date.parse(q.valid_time) !== Date.parse(q.valid_time_utc)) return `${at}: valid_time and valid_time_utc are different instants`;
    return null;
  };
  switch (m.type) {
    case 'parsed':
    case 'scene_changed': {
      const e = addQuery(m.query, 'message.query');
      if (e) return e;
      break;
    }
    case 'tier_frame_ready': {
      const l = m.label;
      zones.push(['message.label.tz', l.tz]);
      if (l.kind === 'photo') instants.push(['message.label.taken_at', l.taken_at]);
      else instants.push(['message.label.valid_time', l.valid_time], ['message.label.run.cycle_utc', l.run.cycle_utc], ['message.label.run.available_at', l.run.available_at]);
      break;
    }
    case 'out_of_range':
      zones.push(['message.tz', m.tz]);
      instants.push(['message.requested', m.requested]);
      if (m.latest_valid !== null) instants.push(['message.latest_valid', m.latest_valid]);
      break;
    case 'settings':
      if (!isLocale(m.locale)) return 'message.locale: not a locale this engine knows';
      break;
    default:
      break;
  }
  for (const [path, s] of instants) if (!isRealInstant(s)) return `${path}: not a real instant`;
  for (const [path, z] of zones) if (!isTimeZone(z)) return `${path}: not a known time zone`;
  return null;
}

/** Why a message was refused. */
export interface ProtocolProblem {
  readonly code: 'invalid_json' | 'invalid_message' | 'unsupported_version';
  readonly messageType: string;
  readonly detail: string;
}

export type ParseResult = { readonly ok: true; readonly msg: Inbound } | { readonly ok: false; readonly problem: ProtocolProblem };

/** The message's type if it is a known one, else '' (an unknown type is never echoed: it could be content). */
const typeOf = (v: unknown): string => {
  const t = typeof v === 'object' && v !== null ? (v as Record<string, unknown>).type : undefined;
  return typeof t === 'string' && Object.hasOwn(INBOUND_BY_TYPE, t) ? t : '';
};

/**
 * Validates one inbound message: JSON text (what the Tauri shell delivers) or an already-parsed value.
 * Applies the schema, then the semantic rules. A refusal names the path and the rule, never the
 * message's content, so it is safe to log.
 */
export function parseInbound(raw: unknown): ParseResult {
  let value: unknown = raw;
  if (typeof raw === 'string') {
    try {
      value = JSON.parse(raw);
    } catch {
      return { ok: false, problem: { code: 'invalid_json', messageType: '', detail: 'not JSON' } };
    }
  }
  const messageType = typeOf(value);
  if (typeof value === 'object' && value !== null && (value as Record<string, unknown>).v !== PROTOCOL_VERSION) {
    return { ok: false, problem: { code: 'unsupported_version', messageType, detail: `message.v: expected ${PROTOCOL_VERSION}` } };
  }
  const e = inboundV.validate(value, 'message');
  if (e !== null) return { ok: false, problem: { code: 'invalid_message', messageType, detail: e.slice(0, 300) } };
  const m = value as Inbound;
  const s = semanticProblem(m);
  if (s !== null) return { ok: false, problem: { code: 'invalid_message', messageType, detail: s.slice(0, 300) } };
  return { ok: true, msg: m };
}

/** The schema part of inbound validation only (no semantic rules), for checking it against the schema. */
export function inboundShapeProblem(v: unknown): string | null {
  return inboundV.validate(v, 'message');
}

/** Validates an outbound message (the harness and tests check everything the overlay sends). */
export function outboundProblem(m: unknown): string | null {
  return outboundV.validate(m, 'message');
}

/** The protocol's JSON Schema (2020-12): inbound at the root, outbound under $defs.outbound. */
export function protocolSchema(): JsonSchema {
  return {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    $id: 'https://snow-globe.invalid/schemas/overlay-protocol.schema.json',
    title: 'Snow Globe overlay protocol, version 1',
    description:
      'Messages from the Rust core to the overlay webview (the root), and from the webview to the core ($defs.outbound), PRD sections 5, 6.3 and 10.3. ' +
      'Generated from app/src/overlay/protocol.ts; do not edit by hand. Rules no schema states, which parseInbound() also enforces: timestamps are real instants, ' +
      'time zones are known IANA zones, the locale is a known tag, and a query\'s valid_time and valid_time_utc are the same instant. Transport rules: messages arrive ' +
      'in send order on one channel, query_gen rises by one per key press from 1, and diagnostics never carry transcript text.',
    ...inboundV.schema,
    $defs: { ...DEFS, outbound: outboundV.schema },
  };
}

/** The inbound validators, for tests that check each variant. */
export const INBOUND_VALIDATORS: Readonly<Record<InboundType, V<unknown>>> = INBOUND_BY_TYPE;
/** The outbound validators. */
export const OUTBOUND_VALIDATORS: Readonly<Record<OutboundType, V<unknown> | ObjV<unknown>>> = OUTBOUND_BY_TYPE;
