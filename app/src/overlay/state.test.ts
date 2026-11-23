import { describe, expect, it } from 'vitest';
import { simulatedCaption } from './caption.ts';
import type { Inbound } from './protocol.ts';
import { coreQuery, DISPLAY, parsedMessage, simulatedLabel } from './samples.ts';
import { acceptsOutcome, acceptsScene, exhausted, INITIAL_MODEL, reduce, showingPrevious, stateName, type LocalEvent, type OverlayModel } from './state.ts';

const caption = simulatedCaption(simulatedLabel(), { display: DISPLAY.rainier!, query: coreQuery('is rainier out tomorrow at 4'), fmt: { locale: 'en-US', units: 'metric' }, online: true });
const run = (events: (Inbound | LocalEvent)[], from: OverlayModel = INITIAL_MODEL): OverlayModel => events.reduce(reduce, from);
const started = (g: number): Inbound => ({ v: 1, type: 'query_started', query_gen: g });
const parsed = parsedMessage(1, 'is rainier out tomorrow at 4');
const S = parsed.scene_hash;
const painted = (g: number, tier: 0 | 1 | 2, scene = S): LocalEvent => ({ type: 'painted', gen: g, scene, tier, caption });
const tier2On: Inbound = { v: 1, type: 'settings', on_device_only: false, cloud_speech: false, tier2: true, locale: 'en-US', units: 'metric', reduce_motion: null, measuring: false };
const outOfRange = (g: number, latest: string | null = '2026-11-26T08:00:00-08:00'): Inbound => ({
  v: 1,
  type: 'out_of_range',
  query_gen: g,
  reason: 'beyond_horizon',
  place_id: 'rainier',
  viewpoint_id: 'rainier.paradise-mountain',
  tz: 'America/Los_Angeles',
  requested: '2026-12-05T16:00:00-08:00',
  latest_valid: latest,
  display: DISPLAY.rainier!,
});
const noMatch = (g: number): Inbound => ({ v: 1, type: 'no_match', query_gen: g, transcript: 'golden gate at 9', closest: [] });
const offered = (g: number): Inbound => ({ ...parsedMessage(g, 'grand canyon golden hour'), type: 'scene_changed', cause: 'use_offer' }) as Inbound;

describe('overlay states (PRD §5.2)', () => {
  it('walks idle → listening → resolving → tier0 → tier1 → tier2', () => {
    let m = run([started(1)]);
    expect(stateName(m)).toBe('listening');
    m = run([{ v: 1, type: 'partial', query_gen: 1, text: 'is rainier out', stable: false }, { v: 1, type: 'audio_level', query_gen: 1, level: 0.5 }], m);
    expect([m.transcript, m.level]).toEqual(['is rainier out', 0.5]);
    m = run([{ v: 1, type: 'released', query_gen: 1 }], m);
    expect(stateName(m)).toBe('resolving');
    m = run([parsed], m);
    expect([stateName(m), m.scene, m.query?.place_id]).toEqual(['resolving', S, 'rainier']);
    m = run([painted(1, 0)], m);
    expect([stateName(m), m.marks]).toEqual(['tier0', ['shown', 'pending', 'off']]);
    m = run([painted(1, 1)], m);
    expect(stateName(m)).toBe('tier1');
    m = run([{ v: 1, type: 'settings', on_device_only: false, cloud_speech: false, tier2: true, locale: 'en-US', units: 'metric', reduce_motion: null, measuring: false }], m);
    expect(m.marks[2]).toBe('pending');
    m = run([painted(1, 2)], m);
    expect([stateName(m), m.marks]).toEqual(['tier2', ['shown', 'shown', 'shown']]);
  });

  it('marks a skipped Tier 0 as arrived when Tier 1 is shown first, and a rejected Tier 2 as hollow', () => {
    const m = run([tier2On, started(1), { v: 1, type: 'released', query_gen: 1 }, parsed, painted(1, 1), { v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: S, tier: 2, reason: 'late' }]);
    expect(m.marks).toEqual(['skipped', 'shown', 'rejected']);
  });

  it('keeps an off mark off when the core rejects that tier (off is the on-device-only setting showing, PRD §15.1)', () => {
    const m = run([started(1), parsed, painted(1, 1), { v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: S, tier: 2, reason: 'disabled' }]);
    expect(m.marks).toEqual(['skipped', 'shown', 'off']);
  });

  it('marks a tier whose pixels failed to load as hollow, unless it is already shown', () => {
    const failed = (tier: 0 | 1 | 2): LocalEvent => ({ type: 'failed', gen: 1, scene: S, tier });
    const m = run([tier2On, started(1), parsed, failed(1), painted(1, 0)]);
    expect(m.marks).toEqual(['shown', 'rejected', 'pending']);
    expect(run([failed(0)], m).marks[0]).toBe('shown');
  });

  it('goes idle on Esc from any state, and ignores the dismissed query afterwards', () => {
    for (const before of [[started(1)], [started(1), parsed], [started(1), parsed, painted(1, 0)]]) {
      const m = run([...before, { v: 1, type: 'cancelled', query_gen: 1, reason: 'esc' }]);
      expect(m.phase).toBe('idle');
      const later = run([parsed, painted(1, 1), { v: 1, type: 'released', query_gen: 1 }, { v: 1, type: 'no_match', query_gen: 1, transcript: 'x', closest: [] }], m);
      expect(later).toBe(m);
    }
  });

  it('goes idle on key-up with no speech', () => {
    expect(run([started(1), { v: 1, type: 'cancelled', query_gen: 1, reason: 'no_speech' }]).phase).toBe('idle');
  });

  it('lets a new key press cancel everything, and keeps the settings and destination', () => {
    const s: Inbound = { v: 1, type: 'settings', on_device_only: true, cloud_speech: false, tier2: true, locale: 'en-GB', units: 'us', reduce_motion: true, measuring: false };
    const m = run([s, { v: 1, type: 'destination', seq: 1, destinations: [] }, started(1), parsed, painted(1, 1), started(2)]);
    expect([m.phase, m.gen, m.scene, m.caption, m.shownTier]).toEqual(['listening', 2, null, null, null]);
    expect(m.settings.locale).toBe('en-GB');
    expect(m.marks[2]).toBe('off'); // on-device only: no Tier 2
    expect(m.destinationSeq).toBe(1);
  });

  it('ignores messages from older generations and older or repeated key presses', () => {
    const m = run([started(5), { v: 1, type: 'released', query_gen: 5 }]);
    expect(run([started(4), started(5), { v: 1, type: 'partial', query_gen: 4, text: 'old', stable: true }, { ...parsed, query_gen: 4 }], m)).toBe(m);
  });

  it('keeps the frame on screen after a scrub until the new scene paints, and restarts the marks', () => {
    let m = run([started(1), { v: 1, type: 'released', query_gen: 1 }, parsed, painted(1, 1)]);
    const scrubbed = { ...parsedMessage(1, 'grand canyon golden hour'), type: 'scene_changed', cause: 'scrub' } as Inbound;
    m = run([scrubbed], m);
    expect([m.phase, m.shownScene, m.shownTier, m.marks[1]]).toEqual(['showing', S, null, 'pending']);
    expect(m.caption).toBe(caption); // still the caption of the frame on screen
    const S2 = (scrubbed as { scene_hash: string }).scene_hash;
    m = run([painted(1, 0, S), painted(1, 0, S2)], m);
    expect([m.shownScene, m.shownTier]).toEqual([S2, 0]);
  });

  it('shows No match, Out of range and fatal errors, and keeps non-fatal ones as a notice', () => {
    const nm = run([started(1), { v: 1, type: 'released', query_gen: 1 }, { v: 1, type: 'no_match', query_gen: 1, transcript: 'golden gate at 9', closest: [] }]);
    expect([nm.phase, nm.noMatch?.transcript]).toEqual(['no_match', 'golden gate at 9']);
    const oor = run([started(1), { v: 1, type: 'out_of_range', query_gen: 1, reason: 'past', place_id: 'rainier', viewpoint_id: 'rainier.paradise-mountain', tz: 'America/Los_Angeles', requested: '2026-11-18T16:00:00-08:00', latest_valid: null, display: DISPLAY.rainier! }]);
    expect([oor.phase, oor.outOfRange?.reason]).toEqual(['out_of_range', 'past']);
    const soft = run([started(1), { v: 1, type: 'error', query_gen: 1, code: 'render_failed', fatal: false }]);
    expect([soft.phase, soft.error]).toEqual(['listening', { code: 'render_failed', fatal: false }]);
    const hard = run([started(1), { v: 1, type: 'error', query_gen: 1, code: 'speech_unavailable', fatal: true }]);
    expect(hard.phase).toBe('error');
  });

  it('withdraws a scene shown on a stable partial when the query ends as No match or Out of range (PRD §7.2)', () => {
    for (const outcome of [noMatch(1), outOfRange(1)]) {
      const before = run([started(1), { v: 1, type: 'partial', query_gen: 1, text: 'is rainier out', stable: true }, parsed, painted(1, 0), { v: 1, type: 'released', query_gen: 1 }]);
      expect([before.phase, before.shownScene]).toEqual(['showing', S]);
      expect(acceptsOutcome(before, 1)).toBe(true);
      const m = run([outcome], before);
      expect([m.phase, m.scene, m.shownScene, m.shownTier, m.caption]).toEqual([outcome.type, null, null, null, null]);
      // A late frame of the withdrawn scene changes nothing.
      expect(run([painted(1, 1)], m)).toBe(m);
    }
  });

  it('completes the Out of range offer: the answer moves the panel to resolving, then showing (PRD §5.2)', () => {
    let m = run([started(1), { v: 1, type: 'released', query_gen: 1 }, outOfRange(1)]);
    expect([m.phase, acceptsScene(m, 1), acceptsScene(m, 2)]).toEqual(['out_of_range', true, false]);
    const answer = offered(1) as { scene_hash: string };
    m = run([offered(1)], m);
    expect([m.phase, m.outOfRange, m.scene, m.query?.place_id]).toEqual(['resolving', null, answer.scene_hash, 'grand-canyon']);
    m = run([painted(1, 0, answer.scene_hash)], m);
    expect([m.phase, m.shownTier, m.caption]).toEqual(['showing', 0, caption]);
  });

  it('lets a No match be answered by a chosen place, and nothing after Esc or a fatal error', () => {
    const nm = run([started(1), { v: 1, type: 'released', query_gen: 1 }, noMatch(1)]);
    const chosen = run([{ ...offered(1), cause: 'place_choice' } as Inbound], nm);
    expect([chosen.phase, chosen.noMatch]).toEqual(['resolving', null]);
    const esc = run([{ v: 1, type: 'cancelled', query_gen: 1, reason: 'esc' }], nm);
    expect(acceptsScene(esc, 1)).toBe(false);
    expect(run([offered(1)], esc)).toBe(esc);
    const fatal = run([{ v: 1, type: 'error', query_gen: 1, code: 'internal', fatal: true }], nm);
    expect(acceptsScene(fatal, 1)).toBe(false);
  });

  it('says when nothing can come for the selected scene, and drops the previous scene’s frame then', () => {
    const scrubbed = { ...parsedMessage(1, 'grand canyon golden hour'), type: 'scene_changed', cause: 'scrub' } as Inbound;
    const S2 = (scrubbed as { scene_hash: string }).scene_hash;
    let m = run([started(1), { v: 1, type: 'released', query_gen: 1 }, parsed, painted(1, 1), scrubbed]);
    expect([showingPrevious(m), exhausted(m)]).toEqual([true, false]);
    const reject = (tier: 0 | 1 | 2): Inbound => ({ v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: S2, tier, reason: 'disabled' });
    m = run([reject(0)], m);
    expect(exhausted(m)).toBe(false); // Tier 1 may still come (Tier 2 is off)
    m = run([reject(1)], m);
    expect(exhausted(m)).toBe(true);
    m = run([{ type: 'cleared', gen: 1 }], m);
    expect([m.shownScene, m.caption, showingPrevious(m), exhausted(m)]).toEqual([null, null, false, true]);
  });

  it('applies destinations in sequence order only, and the network state', () => {
    const m = run([{ v: 1, type: 'destination', seq: 2, destinations: [{ provider: 'Deepgram', carries: ['audio'] }] }, { v: 1, type: 'destination', seq: 1, destinations: [] }, { v: 1, type: 'network', online: false }]);
    expect([m.destinationSeq, m.destinations.length, m.online]).toEqual([2, 1, false]);
  });
});
