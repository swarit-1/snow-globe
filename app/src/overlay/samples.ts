/**
 * Sample messages for tests, the preview page and the stress harness. The queries are real output of
 * the core's parser (fixtures/core-parser-outputs.json); the labels are illustrative values chosen
 * to match the PRD §5.3 examples (HRRR 18Z Thu, issued 11:27 AM PST, lead 28 h 33 min for "tomorrow
 * at 4" asked on Thu 19 Nov). Frame URLs are app-local `snowglobe://localhost/synthetic/…` names
 * that no shell serves: nothing here points at a real frame, and test loaders draw their own pixels.
 */
import fixture from './fixtures/core-parser-outputs.json' with { type: 'json' };
import { sceneHashOfParts, type SceneHash, type Tier } from './gate.ts';
import type { Credit, Display, Parsed, PhotoLabel, QueryView, RefinedLabel, SimulatedLabel, TierFrameReady } from './protocol.ts';
import { PROTOCOL_VERSION } from './protocol.ts';

interface FixtureCase {
  readonly utterance: string;
  readonly now: string;
  readonly machine_tz: string;
  readonly context: string | null;
  readonly exit: number;
  readonly output: Record<string, unknown>;
}

/** Every recorded parser run. */
export const PARSER_CASES: readonly FixtureCase[] = (fixture as { cases: FixtureCase[] }).cases;

/** The core's query for a recorded utterance. */
export function coreQuery(utterance: string): QueryView {
  const c = PARSER_CASES.find((x) => x.utterance === utterance);
  if (!c || c.exit !== 0) throw new Error(`no parsed fixture for ${JSON.stringify(utterance)}`);
  return structuredClone(c.output) as unknown as QueryView;
}

/**
 * Display names: the place as the manifest's `place` gives it, and the camera named for what its
 * `truth_camera.view_label` says it shows (samples.test.ts checks both against
 * viewpoints/manifests/). Point Reyes's camera stands at the Lighthouse Visitor Center but its
 * viewpoint is Point Reyes Beach ("View of Point Reyes Beach, looking north-northeast").
 */
export const DISPLAY: Readonly<Record<string, Display>> = {
  rainier: { place_name: 'Mount Rainier', viewpoint_name: 'Paradise camera' },
  'point-reyes': { place_name: 'Point Reyes', viewpoint_name: 'Point Reyes Beach camera' },
  'grand-canyon': { place_name: 'Grand Canyon', viewpoint_name: 'Yavapai Point camera' },
  yosemite: { place_name: 'Yosemite', viewpoint_name: 'Turtleback Dome camera' },
};

/** A sample frame URL: app-local, as the shell will serve frames, and never served by anything. */
export const syntheticUrl = (name: string): string => `snowglobe://localhost/synthetic/${name}`;

/** Credit lines from the licence ledger (PRD §15.4). */
export const NPS_CREDIT: Credit = { text: 'Webcam imagery courtesy National Park Service. Derived images are not NPS products.' };
export const HRRR_CREDIT: Credit = { text: 'Forecast data: NOAA HRRR via NOAA Open Data Dissemination' };
export const GFS_CREDIT: Credit = { text: 'Forecast data: NOAA GFS via NOAA Open Data Dissemination' };

/** A scene hash for a query, from its viewpoint, valid instant and overrides (as lifecycle.rs of_query hashes them). */
export function sceneOf(q: QueryView): SceneHash {
  const enc = new TextEncoder();
  const secs = new DataView(new ArrayBuffer(8));
  secs.setBigInt64(0, BigInt(Math.floor(Date.parse(q.valid_time_utc) / 1000)), true);
  return sceneHashOfParts([enc.encode(q.viewpoint_id), new Uint8Array(secs.buffer), enc.encode(JSON.stringify(q.overrides))]);
}

/** A `parsed` message for a recorded utterance. */
export function parsedMessage(gen: number, utterance: string, display?: Display): Parsed {
  const query = coreQuery(utterance);
  return { v: PROTOCOL_VERSION, type: 'parsed', query_gen: gen, scene_hash: sceneOf(query), query, display: display ?? DISPLAY[query.place_id] ?? { place_name: query.place_id, viewpoint_name: 'camera' } };
}

/** Tier 0 for "point reyes at sunset friday": an archive photo from a year earlier, matched on Sun position and forecast class. */
export function photoLabel(over: Partial<PhotoLabel> = {}): PhotoLabel {
  return {
    kind: 'photo',
    taken_at: '2025-11-21T16:58:00-08:00',
    tz: 'America/Los_Angeles',
    source: 'archive',
    camera_name: 'Point Reyes Beach camera',
    matched_on: ['sun_position', 'forecast_class'],
    credits: [NPS_CREDIT],
    ...over,
  };
}

/** Tier 1 for "is rainier out tomorrow at 4": the PRD §5.3 example caption. */
export function simulatedLabel(over: Partial<SimulatedLabel> = {}): SimulatedLabel {
  return {
    kind: 'simulated',
    valid_time: '2026-11-20T16:00:00-08:00',
    tz: 'America/Los_Angeles',
    run: { model: 'hrrr', cycle_utc: '2026-11-19T18:00:00Z', available_at: '2026-11-19T19:27:00Z', pinned: false },
    forecast: { cloud_low: 0.8, cloud_mid: 0.15, cloud_high: 0.05, ceiling_m: 2300, visibility_m: 12_000, precip_rate_mm_h: null, precip_type: null },
    sun_altitude_deg: 3.1,
    moon: { illuminated: 0.41, altitude_deg: -12.4 },
    credits: [HRRR_CREDIT, NPS_CREDIT],
    ...over,
  };
}

/** Tier 2: Tier 1 plus ground texture. */
export function refinedLabel(over: Partial<RefinedLabel> = {}): RefinedLabel {
  const { kind: _k, ...sim } = simulatedLabel();
  return { kind: 'refined', ...sim, refine: { model: 'Z-Image-Turbo', provider: 'fal' }, ...over };
}

/** A tier frame message. */
export function tierFrame(gen: number, scene: SceneHash, tier: Tier, url = syntheticUrl(`tier${tier}`)): TierFrameReady {
  const frame = { url, width: 1920, height: 1080 };
  const base = { v: PROTOCOL_VERSION, type: 'tier_frame_ready' as const, query_gen: gen, scene_hash: scene, frame };
  switch (tier) {
    case 0:
      return { ...base, tier: 0, label: photoLabel() };
    case 1:
      return { ...base, tier: 1, label: simulatedLabel() };
    case 2:
      return { ...base, tier: 2, label: refinedLabel() };
  }
}

/** One valid example of every inbound message type (for the schema tests). */
export function inboundExamples(): Record<string, unknown>[] {
  const parsed = parsedMessage(3, 'is rainier out tomorrow at 4');
  const scene = parsed.scene_hash;
  return [
    { v: 1, type: 'query_started', query_gen: 3 },
    { v: 1, type: 'query_started', query_gen: 3, core_ns: 123_456_789 },
    { v: 1, type: 'partial', query_gen: 3, text: 'is rainier out tomorrow', stable: false },
    { v: 1, type: 'audio_level', query_gen: 3, level: 0.42 },
    { v: 1, type: 'released', query_gen: 3 },
    parsed,
    { ...parsedMessage(3, 'grand canyon golden hour'), type: 'scene_changed', cause: 'scrub' },
    { v: 1, type: 'tier_started', query_gen: 3, scene_hash: scene, tier: 1, eta_ms: 40 },
    tierFrame(3, scene, 0),
    tierFrame(3, scene, 1),
    tierFrame(3, scene, 2),
    { v: 1, type: 'tier_rejected', query_gen: 3, scene_hash: scene, tier: 2, reason: 'structure' },
    {
      v: 1,
      type: 'no_match',
      query_gen: 4,
      transcript: 'golden gate at 9',
      closest: [
        { place_id: 'grand-teton', place: 'Grand Teton', score: 0.38 },
        { place_id: 'yosemite', place: 'Yosemite', score: 0.36 },
        { place_id: 'smokies', place: 'Great Smoky Mountains', score: 0.34 },
      ],
    },
    {
      v: 1,
      type: 'out_of_range',
      query_gen: 5,
      reason: 'past',
      place_id: 'rainier',
      viewpoint_id: 'rainier.paradise-mountain',
      tz: 'America/Los_Angeles',
      requested: '2026-11-18T16:00:00-08:00',
      latest_valid: '2026-11-26T08:00:00-08:00',
      display: DISPLAY.rainier,
    },
    { v: 1, type: 'cancelled', query_gen: 3, reason: 'esc' },
    { v: 1, type: 'error', query_gen: 3, code: 'render_failed', fatal: false },
    { v: 1, type: 'error', query_gen: null, code: 'microphone_denied', fatal: true },
    { v: 1, type: 'settings', on_device_only: true, cloud_speech: false, tier2: false, locale: 'en-US', units: 'us', reduce_motion: null, measuring: false },
    { v: 1, type: 'destination', seq: 1, destinations: [] },
    { v: 1, type: 'destination', seq: 2, destinations: [{ provider: 'Deepgram', carries: ['audio'] }] },
    { v: 1, type: 'network', online: false },
    { v: 1, type: 'prefetch', frames: [{ url: syntheticUrl('rainier-0'), width: 640, height: 360 }, { url: syntheticUrl('rainier-1'), width: 640, height: 360 }] },
  ];
}
