import { describe, expect, it } from 'vitest';
import { simulatedCaption } from './caption.ts';
import { FakeClock, flush, ManualLoader, RecordingSurface } from './fakes.ts';
import { PaintGate, type Tier } from './gate.ts';
import { outboundProblem, type PaintReport, type TierStarted } from './protocol.ts';
import { coreQuery, DISPLAY, simulatedLabel, tierFrame } from './samples.ts';
import { PaintScheduler, type PaintEvent } from './scheduler.ts';

const S = '00000000000000aa';
const S2 = '00000000000000bb';
const caption = simulatedCaption(simulatedLabel(), { display: DISPLAY.rainier!, query: coreQuery('is rainier out tomorrow at 4'), fmt: { locale: 'en-US', units: 'metric' }, online: true });

function rig(o: { window?: number } = {}) {
  const clock = new FakeClock();
  const gate = new PaintGate();
  const loader = new ManualLoader();
  const surface = new RecordingSurface(clock);
  const events: PaintEvent[] = [];
  const reports: PaintReport[] = [];
  const s = new PaintScheduler({
    gate,
    clock,
    loader: loader.loader,
    surface,
    caption: () => caption,
    onEvent: (e) => events.push(e),
    onReport: (r) => reports.push(r),
    ...(o.window === undefined ? {} : { skipWindowMs: o.window }),
  });
  gate.begin(1);
  gate.setScene(1, S);
  const offer = (tier: Tier, gen = 1, scene = S): void => s.offer(tierFrame(gen, scene, tier));
  const hint = (tier: Tier, eta: number, gen = 1, scene = S): void => s.expect({ v: 1, type: 'tier_started', query_gen: gen, scene_hash: scene, tier, eta_ms: eta } satisfies TierStarted);
  const settle = async (tier?: Tier): Promise<void> => {
    loader.settle(tier);
    await flush();
  };
  const kinds = (k: PaintEvent['kind']) => events.filter((e) => e.kind === k);
  return { clock, gate, loader, surface, events, reports, s, offer, hint, settle, kinds };
}

describe('paint scheduler (PRD §10.3)', () => {
  it('paints a prepared frame in the next frame callback, checking the gate right before the draw', async () => {
    const r = rig();
    r.offer(0);
    r.clock.advance(5);
    await r.settle(0);
    expect(r.surface.commits()).toEqual([]); // not before the frame
    r.clock.advance(20);
    expect(r.surface.commits()).toEqual([{ gen: 1, scene: S, tier: 0 }]);
    const c = r.kinds('commit')[0] as Extract<PaintEvent, { kind: 'commit' }>;
    expect(c.frameMs).toBeCloseTo(1000 / 60, 6); // the first frame boundary after the decode finished at 5 ms
    expect(c.preparedMs).toBe(5);
    expect(c.checkToCommitMs).toBe(0);
    expect(r.gate.state.shown).toBe(0);
    const rep = r.reports.find((x) => x.outcome === 'painted')!;
    expect(rep).toMatchObject({ tier: 0, faded: false, received_ms: 0, prepared_ms: 5, half_ms: c.frameMs });
    for (const x of r.reports) expect(outboundProblem(x)).toBeNull();
  });

  it('drops a frame of an older generation at receipt, without decoding it', () => {
    const r = rig();
    r.gate.begin(2);
    r.offer(0, 1);
    expect(r.loader.waiting.length).toBe(0);
    expect(r.kinds('drop')).toMatchObject([{ reason: 'generation', at: 'receipt' }]);
  });

  it('drops a frame whose scene changed while it decoded (a scrub), and never paints it', async () => {
    const r = rig();
    r.offer(1);
    r.gate.setScene(1, S2);
    await r.settle();
    r.clock.advance(50);
    expect(r.surface.commits()).toEqual([]);
    expect(r.kinds('drop')).toMatchObject([{ reason: 'scene', at: 'frame' }]);
  });

  it('never paints after Esc, however late the decode finishes', async () => {
    const r = rig();
    r.offer(1);
    r.offer(2);
    r.gate.dismiss();
    r.s.reset();
    await r.settle();
    await r.settle();
    r.clock.advance(500);
    expect(r.surface.commits()).toEqual([]);
    expect(r.loader.waiting.length).toBe(0);
    expect(r.kinds('reset').length).toBe(2);
  });

  it('cross-fades Tier 1 over Tier 0 and reports the frame at which the fade passed 50 % (PRD §5.5)', async () => {
    const r = rig();
    r.offer(0);
    await r.settle(0);
    r.clock.advance(17);
    r.offer(1);
    await r.settle(1);
    r.clock.advance(17);
    const log = r.surface.log.filter((l) => l.op === 'commit');
    expect(log.map((l) => (l.op === 'commit' ? [l.key.tier, l.faded] : null))).toEqual([[0, false], [1, true]]);
    expect(r.reports.filter((x) => x.outcome === 'painted' && x.tier === 1)).toEqual([]); // waits for 50 %
    r.clock.advance(100);
    const t1 = r.reports.find((x) => x.outcome === 'painted' && x.tier === 1)!;
    expect(t1.faded).toBe(true);
    expect(t1.half_ms! - t1.frame_ms!).toBeGreaterThanOrEqual(60);
    expect(t1.half_ms! - t1.frame_ms!).toBeLessThan(60 + 17);
  });

  it('shows Tier 1 without a fade under Reduce Motion', async () => {
    const r = rig();
    r.surface.configure({ reduceMotion: true, measuring: false });
    r.offer(0);
    await r.settle(0);
    r.clock.advance(17);
    r.offer(1);
    await r.settle(1);
    r.clock.advance(17);
    expect(r.surface.log.filter((l) => l.op === 'commit').map((l) => (l.op === 'commit' ? l.faded : null))).toEqual([false, false]);
  });

  describe('Tier 0 skip rule (D-012 row 11)', () => {
    it('skips Tier 0 when Tier 1 is ready in the same frame', async () => {
      const r = rig();
      r.offer(0);
      r.offer(1);
      await r.settle(0);
      await r.settle(1);
      r.clock.advance(17);
      expect(r.surface.commits().map((k) => k.tier)).toEqual([1]);
      expect(r.kinds('skip')).toMatchObject([{ key: { tier: 0 }, by: 1 }]);
      expect(r.reports.find((x) => x.tier === 0)).toMatchObject({ outcome: 'skipped', reason: 'superseded', prepared_ms: 0 });
    });

    it('holds Tier 0 while Tier 1 is decoding, and skips it when Tier 1 lands inside the window', async () => {
      const r = rig();
      r.offer(0);
      r.offer(1);
      await r.settle(0);
      r.clock.advance(40);
      expect(r.surface.commits()).toEqual([]);
      expect(r.kinds('hold')).toMatchObject([{ key: { tier: 0 }, deadlineMs: 100 }]);
      await r.settle(1);
      r.clock.advance(17);
      expect(r.surface.commits().map((k) => k.tier)).toEqual([1]);
      expect(r.kinds('skip').length).toBe(1);
    });

    it('holds Tier 0 when the core says Tier 1 is due inside the window', async () => {
      const r = rig();
      r.hint(1, 60);
      r.offer(0);
      await r.settle(0);
      r.clock.advance(50);
      expect(r.surface.commits()).toEqual([]);
      r.offer(1);
      await r.settle(1);
      r.clock.advance(17);
      expect(r.surface.commits().map((k) => k.tier)).toEqual([1]);
    });

    it('paints Tier 0 at once when Tier 1 is due after the window, or nothing says it is coming', async () => {
      for (const eta of [400, null]) {
        const r = rig();
        if (eta !== null) r.hint(1, eta);
        r.offer(0);
        await r.settle(0);
        r.clock.advance(17);
        expect(r.surface.commits().map((k) => k.tier)).toEqual([0]);
        expect(r.kinds('hold')).toEqual([]);
      }
    });

    it('never holds Tier 0 past its ready time plus the window: it paints in the last frame before the deadline', async () => {
      const r = rig();
      r.hint(1, 30);
      r.offer(0);
      r.clock.advance(3);
      await r.settle(0);
      r.clock.advance(70);
      expect(r.surface.commits()).toEqual([]);
      r.clock.advance(40);
      const c = r.kinds('commit')[0] as Extract<PaintEvent, { kind: 'commit' }>;
      expect(c.key.tier).toBe(0);
      expect(c.tMs - c.preparedMs).toBeLessThanOrEqual(100);
      expect(c.tMs - c.preparedMs).toBeGreaterThan(100 - 17);
      expect(c.heldMs).toBe(c.tMs - c.preparedMs);
      expect(r.reports.find((x) => x.outcome === 'painted')!.held_ms).toBe(c.heldMs);
    });

    it('stops holding when Tier 1 fails to decode', async () => {
      const r = rig();
      r.offer(0);
      r.offer(1);
      await r.settle(0);
      r.clock.advance(20);
      r.loader.fail(1);
      await flush();
      r.clock.advance(17);
      expect(r.surface.commits().map((k) => k.tier)).toEqual([0]);
      expect(r.reports.find((x) => x.outcome === 'failed')).toMatchObject({ tier: 1, reason: 'load_failed' });
    });

    it('uses up an announcement when its frame arrives: an announced Tier 1 that then fails releases Tier 0 at once', async () => {
      for (const announced of [true, false]) {
        const r = rig();
        if (announced) r.hint(1, 30);
        r.offer(0);
        await r.settle(0);
        r.clock.advance(5);
        r.offer(1); // the announced frame came
        r.clock.advance(17);
        r.loader.fail(1);
        await flush();
        r.clock.advance(17);
        const c = r.kinds('commit')[0] as Extract<PaintEvent, { kind: 'commit' }> | undefined;
        expect(c?.key.tier, `announced ${announced}`).toBe(0);
        expect(c!.tMs - c!.preparedMs, `announced ${announced}`).toBeLessThan(40); // not the whole 100 ms window
      }
    });

    it('holds without timers: it looks at the held frame in every frame, and paints it in the last one before the deadline', async () => {
      const r = rig();
      r.hint(1, 40);
      r.offer(0);
      r.clock.advance(7);
      await r.settle(0);
      let frames = 0;
      for (let t = 0; t < 120 && r.kinds('commit').length === 0; t += 1000 / 60) {
        r.clock.advance(1000 / 60);
        frames += 1;
      }
      const c = r.kinds('commit')[0] as Extract<PaintEvent, { kind: 'commit' }>;
      expect(c.tMs - c.preparedMs).toBeLessThanOrEqual(100);
      expect(c.tMs - c.preparedMs).toBeGreaterThan(100 - 1000 / 60);
      expect(frames).toBe(6);
    });
  });

  describe('cross-fades cut short', () => {
    it('reports a fade whose layer was replaced before 50 % without a 50 % time', async () => {
      const r = rig();
      r.offer(0);
      await r.settle(0);
      r.clock.advance(17);
      r.offer(1);
      await r.settle(1);
      r.clock.advance(17); // Tier 1 commits and starts fading
      r.gate.setScene(1, S2); // a scrub
      r.s.sceneChanged();
      r.offer(0, 1, S2);
      await r.settle(0);
      r.clock.advance(17); // the new scene's Tier 0 replaces every layer, the fading Tier 1 too
      r.clock.advance(200);
      const t1 = r.reports.find((x) => x.outcome === 'painted' && x.tier === 1 && x.scene_hash === S)!;
      expect(t1.faded).toBe(true);
      expect(t1.half_ms).toBeUndefined();
      expect(r.kinds('cut')).toMatchObject([{ key: { tier: 1, scene: S } }]);
      expect(r.kinds('half').filter((e) => e.key.tier === 1)).toEqual([]);
      const s2 = r.reports.find((x) => x.outcome === 'painted' && x.scene_hash === S2)!;
      expect(s2.previous_ms).toBeGreaterThan(0);
    });
  });

  describe('Tier 0 skip rule, continued (D-012 row 11)', () => {
    it('waits for a frame still on its way before the controller may clear the previous scene', async () => {
      const r = rig();
      r.offer(1);
      expect(r.s.waitingFor(1, S)).toBe(true);
      expect(r.s.waitingFor(1, S2)).toBe(false);
      await r.settle(1);
      r.clock.advance(17);
      expect(r.s.waitingFor(1, S)).toBe(false);
      r.s.clearShown();
      expect(r.surface.layers).toEqual([]);
    });

    it('applies only to the first paint of a scene, and the window can be turned off', async () => {
      const r = rig({ window: 0 });
      r.offer(0);
      r.offer(1);
      await r.settle(0);
      r.clock.advance(17);
      expect(r.surface.commits().map((k) => k.tier)).toEqual([0]);
    });
  });

  it('drops a Tier 0 that arrives after Tier 1 was shown (tier order)', async () => {
    const r = rig();
    r.offer(1);
    await r.settle(1);
    r.clock.advance(17);
    r.offer(0);
    expect(r.kinds('drop')).toMatchObject([{ key: { tier: 0 }, reason: 'tier', at: 'receipt' }]);
  });

  it('clears the surface and stops a running fade on reset, and reports the fade without a half time', async () => {
    const r = rig();
    r.offer(0);
    await r.settle(0);
    r.clock.advance(17);
    r.offer(1);
    await r.settle(1);
    r.clock.advance(17);
    r.gate.begin(2);
    r.s.reset();
    expect(r.surface.layers).toEqual([]);
    expect(r.surface.log[r.surface.log.length - 1]).toEqual({ op: 'clear' });
    const t1 = r.reports.find((x) => x.outcome === 'painted' && x.tier === 1)!;
    expect(t1.half_ms).toBeUndefined();
    r.clock.advance(200);
    expect(r.clock.framesPending).toBe(0);
  });
});
