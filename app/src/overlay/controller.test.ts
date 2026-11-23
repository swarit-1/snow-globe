import { describe, expect, it } from 'vitest';
import { OverlayController } from './controller.ts';
import { FakeClock, flush, instantLoader, RecordingSurface } from './fakes.ts';
import { outboundProblem, type Outbound } from './protocol.ts';
import { DISPLAY, parsedMessage, syntheticUrl, tierFrame } from './samples.ts';
import type { OverlayModel } from './state.ts';

const OUT_OF_RANGE = {
  v: 1,
  type: 'out_of_range',
  query_gen: 1,
  reason: 'beyond_horizon',
  place_id: 'rainier',
  viewpoint_id: 'rainier.paradise-mountain',
  tz: 'America/Los_Angeles',
  requested: '2026-12-05T16:00:00-08:00',
  latest_valid: '2026-11-26T08:00:00-08:00',
  display: DISPLAY.rainier,
};

function rig() {
  const clock = new FakeClock();
  const surface = new RecordingSurface(clock);
  const sent: Outbound[] = [];
  const renders: OverlayModel[] = [];
  const c = new OverlayController({ loader: instantLoader, surface, clock, send: (m) => sent.push(m), render: (m) => renders.push(m) });
  const send = (m: unknown): boolean => c.receive(JSON.stringify(m));
  const step = async (ms = 17): Promise<void> => {
    await flush();
    clock.advance(ms);
    await flush();
  };
  return { clock, surface, sent, renders, c, send, step };
}

const P = parsedMessage(1, 'is rainier out tomorrow at 4');
const S = P.scene_hash;

describe('overlay controller', () => {
  it('runs a whole query from JSON messages: listening, chips, Tier 0, Tier 1', async () => {
    const r = rig();
    r.send({ v: 1, type: 'query_started', query_gen: 1 });
    expect(r.c.model.phase).toBe('listening');
    r.send({ v: 1, type: 'released', query_gen: 1 });
    r.send(P);
    expect(r.c.model.phase).toBe('resolving');
    r.send(tierFrame(1, S, 0));
    await r.step();
    expect(r.c.model.phase).toBe('showing');
    expect(r.c.model.caption?.kind).toBe('photo');
    r.send(tierFrame(1, S, 1));
    await r.step();
    expect([r.c.model.shownTier, r.c.model.caption?.kind]).toEqual([1, 'simulated']);
    expect(r.surface.commits().map((k) => k.tier)).toEqual([0, 1]);
    for (const m of r.sent) expect(outboundProblem(m), JSON.stringify(m)).toBeNull();
  });

  it('refuses invalid messages with a protocol_error and changes nothing', () => {
    const r = rig();
    expect(r.c.receive('{not json')).toBe(false);
    expect(r.c.receive(JSON.stringify({ v: 1, type: 'query_started', query_gen: '1' }))).toBe(false);
    expect(r.c.receive(JSON.stringify({ v: 2, type: 'query_started', query_gen: 1 }))).toBe(false);
    expect(r.sent.map((m) => (m.type === 'protocol_error' ? m.code : m.type))).toEqual(['invalid_json', 'invalid_message', 'unsupported_version']);
    expect(r.c.model.phase).toBe('idle');
  });

  it('refuses a message for a generation whose query_started has not arrived', async () => {
    const r = rig();
    r.send({ v: 1, type: 'query_started', query_gen: 1 });
    r.send(parsedMessage(2, 'is rainier out tomorrow at 4'));
    r.send(tierFrame(2, S, 0));
    await r.step();
    expect(r.surface.commits()).toEqual([]);
    expect(r.sent.filter((m) => m.type === 'protocol_error').map((m) => (m.type === 'protocol_error' ? m.code : ''))).toEqual(['generation_ahead', 'generation_ahead']);
  });

  it('closes the gate on Esc from the core, so a frame that was already on its way never paints', async () => {
    const r = rig();
    r.send({ v: 1, type: 'query_started', query_gen: 1 });
    r.send(P);
    r.send(tierFrame(1, S, 1)); // decoding
    r.send({ v: 1, type: 'cancelled', query_gen: 1, reason: 'esc' });
    r.send(tierFrame(1, S, 2)); // late
    await r.step(200);
    expect(r.surface.commits()).toEqual([]);
    expect(r.c.model.phase).toBe('idle');
    expect(r.c.gate.state.dismissed).toBe(true);
  });

  it('handles Esc in the focused panel: closes the gate at once and tells the core (PRD §5.4)', async () => {
    const r = rig();
    r.send({ v: 1, type: 'query_started', query_gen: 1 });
    r.send(P);
    r.send(tierFrame(1, S, 0));
    await r.step();
    r.c.command({ type: 'dismiss' });
    expect(r.surface.layers).toEqual([]);
    expect(r.sent.at(-1)).toEqual({ v: 1, type: 'dismiss', query_gen: 1 });
    r.send(tierFrame(1, S, 1));
    await r.step();
    expect(r.surface.commits().map((k) => k.tier)).toEqual([0]);
  });

  it('acknowledges a destination only once its chip has been on screen for a full frame in this key press (PRD §15.1)', () => {
    const r = rig();
    const acks = (): Outbound[] => r.sent.filter((m) => m.type === 'destination_shown');
    r.send({ v: 1, type: 'destination', seq: 1, destinations: [{ provider: 'Deepgram', carries: ['audio'] }] });
    expect(r.renders.at(-1)!.destinations[0]!.provider).toBe('Deepgram');
    r.clock.advance(200);
    expect(acks()).toEqual([]); // idle: the panel shows no chip, so nothing may leave on its strength
    r.send({ v: 1, type: 'query_started', query_gen: 1 }); // the reveal renders the chip
    r.clock.advance(17);
    expect(acks()).toEqual([]);
    r.clock.advance(17);
    expect(acks()).toMatchObject([{ seq: 1, query_gen: 1 }]);
    // Each key press is a new reveal and needs its own acknowledgement.
    r.send({ v: 1, type: 'cancelled', query_gen: 1, reason: 'esc' });
    r.send({ v: 1, type: 'query_started', query_gen: 2 });
    r.clock.advance(34);
    expect(acks()).toMatchObject([{ seq: 1, query_gen: 1 }, { seq: 1, query_gen: 2 }]);
    // A press dismissed before its chip was up for a full frame is never acknowledged.
    r.send({ v: 1, type: 'query_started', query_gen: 3 });
    r.clock.advance(17);
    r.c.command({ type: 'dismiss' });
    r.clock.advance(100);
    expect(acks().map((m) => (m.type === 'destination_shown' ? m.query_gen : 0))).toEqual([1, 2]);
    for (const m of r.sent) expect(outboundProblem(m), JSON.stringify(m)).toBeNull();
  });

  it('refuses a cloud destination while on-device only is set: a protocol_error, the chip as a warning, and never an acknowledgement', () => {
    const r = rig();
    r.send({ v: 1, type: 'settings', on_device_only: true, cloud_speech: false, tier2: false, locale: 'en-US', units: 'metric', reduce_motion: null, measuring: false });
    r.send({ v: 1, type: 'query_started', query_gen: 1 });
    r.send({ v: 1, type: 'destination', seq: 1, destinations: [{ provider: 'Deepgram', carries: ['audio'] }] });
    r.clock.advance(200);
    expect(r.sent.map((m) => m.type)).toEqual(['protocol_error']);
    expect(r.sent[0]).toMatchObject({ code: 'invalid_message', message_type: 'destination' });
    expect(r.c.model.destinations.length).toBe(1); // the chip says what the core claims, as a warning
    // Once the core falls back to on-device, that is acknowledged.
    r.send({ v: 1, type: 'destination', seq: 2, destinations: [] });
    r.clock.advance(34);
    expect(r.sent.filter((m) => m.type === 'destination_shown')).toMatchObject([{ seq: 2, query_gen: 1 }]);
  });

  it('withdraws a scene shown on a stable partial when the query ends as No match or Out of range: nothing of it stays or paints later', async () => {
    for (const outcome of ['no_match', 'out_of_range'] as const) {
      const r = rig();
      r.send({ v: 1, type: 'query_started', query_gen: 1 });
      r.send({ v: 1, type: 'partial', query_gen: 1, text: 'is rainier out', stable: true });
      r.send(P);
      r.send(tierFrame(1, S, 0));
      await r.step();
      expect(r.surface.layers.map((l) => l.key.tier)).toEqual([0]);
      r.send({ v: 1, type: 'released', query_gen: 1 });
      r.send(outcome === 'no_match' ? { v: 1, type: 'no_match', query_gen: 1, transcript: 'golden gate at 9', closest: [] } : OUT_OF_RANGE);
      expect(r.surface.layers).toEqual([]);
      expect([r.c.model.phase, r.c.gate.state.scene, r.c.gate.state.dismissed]).toEqual([outcome, null, false]);
      r.send(tierFrame(1, S, 1)); // late, for the withdrawn scene
      await r.step(200);
      expect(r.surface.commits().map((k) => k.tier)).toEqual([0]);
      expect(r.sent.find((m) => m.type === 'paint' && m.tier === 1)).toMatchObject({ outcome: 'dropped', reason: 'scene' });
    }
  });

  it('completes the Out of range offer end to end: use_offer, the core’s scene_changed, tiers, chips and keys (PRD §5.2)', async () => {
    const r = rig();
    r.send({ v: 1, type: 'query_started', query_gen: 1 });
    r.send({ v: 1, type: 'released', query_gen: 1 });
    r.send(OUT_OF_RANGE);
    r.c.command({ type: 'use_offer' });
    expect(r.sent.at(-1)).toEqual({ v: 1, type: 'use_offer', query_gen: 1, valid_time: '2026-11-26T08:00:00-08:00' });
    const answer = { ...parsedMessage(1, 'is rainier out tomorrow at 4'), type: 'scene_changed', cause: 'use_offer' };
    r.send(answer);
    expect([r.c.model.phase, r.c.model.outOfRange, r.c.model.query?.place_id, r.c.gate.state.scene]).toEqual(['resolving', null, 'rainier', S]);
    r.send(tierFrame(1, S, 0));
    await r.step();
    r.send(tierFrame(1, S, 1));
    await r.step();
    expect(r.surface.commits().map((k) => k.tier)).toEqual([0, 1]);
    expect([r.c.model.phase, r.c.model.shownTier, r.c.model.caption?.kind]).toEqual(['showing', 1, 'simulated']);
    r.c.command({ type: 'scrub', minutes: 60 });
    r.c.command({ type: 'copy' });
    expect(r.sent.filter((m) => m.type === 'scrub' || m.type === 'copy').map((m) => m.type)).toEqual(['scrub', 'copy']);
  });

  it('scrubs back out of an Out of range reached by scrubbing, and not out of one with no query', () => {
    const r = rig();
    r.send({ v: 1, type: 'query_started', query_gen: 1 });
    r.send({ v: 1, type: 'released', query_gen: 1 });
    r.send(OUT_OF_RANGE);
    r.c.command({ type: 'scrub', minutes: -60 });
    expect(r.sent.filter((m) => m.type === 'scrub')).toEqual([]); // nothing was ever resolved
    r.send({ v: 1, type: 'query_started', query_gen: 2 });
    r.send({ v: 1, type: 'released', query_gen: 2 });
    r.send(parsedMessage(2, 'is rainier out tomorrow at 4'));
    r.send({ ...OUT_OF_RANGE, query_gen: 2 }); // a scrub went past the horizon
    r.c.command({ type: 'scrub', minutes: -60 });
    expect(r.sent.filter((m) => m.type === 'scrub')).toEqual([{ v: 1, type: 'scrub', query_gen: 2, minutes: -60 }]);
  });

  it('takes the previous scene’s frame off screen once every tier of the scrubbed-to scene is rejected or failed', async () => {
    const r = rig();
    r.send({ v: 1, type: 'query_started', query_gen: 1 });
    r.send({ v: 1, type: 'released', query_gen: 1 });
    r.send(P);
    r.send(tierFrame(1, S, 1));
    await r.step();
    const scrubbed = { ...parsedMessage(1, 'grand canyon golden hour'), type: 'scene_changed', cause: 'scrub' };
    const S2 = scrubbed.scene_hash;
    r.send(scrubbed);
    expect(r.surface.layers.map((l) => l.key.scene)).toEqual([S]); // kept while the new scene is on its way
    r.send({ v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: S2, tier: 0, reason: 'disabled' });
    expect(r.surface.layers.length).toBe(1);
    r.send({ v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: S2, tier: 1, reason: 'structure' });
    expect(r.surface.layers).toEqual([]); // Tier 2 is off: nothing can come
    expect([r.c.model.shownScene, r.c.model.caption, r.c.model.phase]).toEqual([null, null, 'showing']);
  });

  it('reports how long the previous scene’s frame stayed up after a scrub, on the frame that replaces it', async () => {
    const r = rig();
    r.send({ v: 1, type: 'query_started', query_gen: 1 });
    r.send(P);
    r.send(tierFrame(1, S, 1));
    await r.step();
    const scrubbed = { ...parsedMessage(1, 'grand canyon golden hour'), type: 'scene_changed', cause: 'scrub' };
    r.send(scrubbed);
    await r.step(40); // nothing of the new scene yet
    r.send(tierFrame(1, scrubbed.scene_hash, 0));
    await r.step();
    const rep = r.sent.find((m) => m.type === 'paint' && m.scene_hash === scrubbed.scene_hash && m.outcome === 'painted');
    expect(rep).toBeDefined();
    if (rep?.type === 'paint') {
      expect(rep.previous_ms).toBeGreaterThan(40);
      expect(rep.previous_ms).toBeLessThan(80);
    }
    expect(r.sent.find((m) => m.type === 'paint' && m.scene_hash === S && m.outcome === 'painted')).not.toHaveProperty('previous_ms');
  });

  it('hands prefetched frames to the frame cache and changes nothing else', () => {
    const got: unknown[] = [];
    const clock = new FakeClock();
    const c = new OverlayController({ loader: instantLoader, surface: new RecordingSurface(clock), clock, send: () => {}, prefetch: (f) => got.push(f) });
    const before = c.model;
    expect(c.receive({ v: 1, type: 'prefetch', frames: [{ url: syntheticUrl('a'), width: 640, height: 360 }] })).toBe(true);
    expect(got).toEqual([[{ url: syntheticUrl('a'), width: 640, height: 360 }]]);
    expect(c.model).toBe(before);
  });

  it('forwards scrub, AM/PM flip, copy and the Out of range offer only when they apply', async () => {
    const r = rig();
    r.c.command({ type: 'scrub', minutes: 60 });
    r.c.command({ type: 'flip_meridiem' });
    expect(r.sent).toEqual([]);
    r.send({ v: 1, type: 'query_started', query_gen: 1 });
    r.send({ v: 1, type: 'released', query_gen: 1 });
    r.send(P);
    r.send(tierFrame(1, S, 1));
    await r.step();
    r.c.command({ type: 'scrub', minutes: -10 });
    r.c.command({ type: 'flip_meridiem' });
    r.c.command({ type: 'copy' });
    r.c.command({ type: 'open_url', url: 'javascript:alert(1)' });
    expect(r.sent.filter((m) => m.type !== 'paint').map((m) => m.type)).toEqual(['scrub', 'flip_meridiem', 'copy']);
    r.send({ v: 1, type: 'query_started', query_gen: 2 });
    r.send({ ...OUT_OF_RANGE, query_gen: 2 });
    r.c.command({ type: 'use_offer' });
    expect(r.sent.at(-1)).toEqual({ v: 1, type: 'use_offer', query_gen: 2, valid_time: '2026-11-26T08:00:00-08:00' });
  });

  it('passes the display settings to the surface', () => {
    const r = rig();
    r.send({ v: 1, type: 'settings', on_device_only: false, cloud_speech: false, tier2: true, locale: 'en-GB', units: 'us', reduce_motion: true, measuring: true });
    expect([r.surface.reduceMotion, r.surface.measuring]).toEqual([true, true]);
  });
});
