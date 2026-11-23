import type { Browser, Page } from 'playwright-core';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { CaptionFit, Snapshot } from '../harness/viewdriver.ts';
import { CAPTION_STEPS } from '../overlay/styles.ts';
import { harnessBundle } from '../scripts/bundle.ts';
import { launch, pageWith } from './browser.ts';

/**
 * The DOM view in headless Chromium (PRD §5.1 anatomy, §5.2 states, §5.4 keys, §5.5 motion and
 * accessibility, §10.4 pixel tag). Each test mounts one overlay in a fresh page and drives it with
 * the protocol's messages, as the core will.
 */
let browser: Browser;
let bundle: string;

beforeAll(async () => {
  bundle = await harnessBundle();
  browser = await launch();
});
afterAll(async () => {
  await browser?.close();
});

const plain = (s: string): string => s.replace(/[  ]/g, ' ');

/** Mounts a driver in a new page and returns the page. */
async function mounted(o: Parameters<typeof pageWith>[2] = {}): Promise<Page> {
  const page = await pageWith(browser, bundle, o);
  await page.evaluate(() => {
    const g = globalThis as unknown as { SnowGlobeHarness: { ViewDriver: new () => unknown }; d: unknown };
    g.d = new g.SnowGlobeHarness.ViewDriver();
  });
  return page;
}

type D = {
  send(m: unknown): boolean;
  snapshot(): Snapshot;
  captionFit(): CaptionFit;
  waitFrames(n: number): Promise<void>;
  waitMs(n: number): Promise<void>;
  key(k: string, mods?: object): void;
  topTag(): unknown;
  destroy(): void;
  sent: { type: string; [k: string]: unknown }[];
  frames: number;
  root: HTMLElement;
};
type S = { parsedMessage(g: number, u: string): { scene_hash: string }; tierFrame(g: number, s: string, t: number): unknown; inboundExamples(): { type: string }[] };

/** Runs `fn(driver, samples, arg)` in the page. */
function inPage<A, R>(page: Page, fn: (d: D, s: S, arg: A) => R | Promise<R>, arg: A): Promise<R> {
  return page.evaluate(
    ([src, a]) => {
      const g = globalThis as unknown as { d: unknown; SnowGlobeHarness: { samples: unknown } };
      // eslint-disable-next-line no-new-func
      return new Function('d', 's', 'arg', `return (${src})(d, s, arg);`)(g.d, g.SnowGlobeHarness.samples, a);
    },
    [fn.toString(), arg] as const,
  ) as Promise<R>;
}

const SETTINGS = { v: 1, type: 'settings', on_device_only: false, cloud_speech: false, tier2: true, locale: 'en-US', units: 'metric', reduce_motion: null, measuring: false };

describe('overlay view in headless Chromium', () => {
  it('walks every state of PRD §5.2 with real caption text and no layout shift', async () => {
    const page = await mounted();
    const shots = await inPage(
      page,
      async (d, s, settings) => {
        const out: Record<string, Snapshot> = {};
        d.send(settings);
        d.send({ v: 1, type: 'destination', seq: 1, destinations: [] });
        d.send({ v: 1, type: 'query_started', query_gen: 1 });
        d.send({ v: 1, type: 'audio_level', query_gen: 1, level: 0.6 });
        d.send({ v: 1, type: 'partial', query_gen: 1, text: 'is rainier out tomorrow', stable: false });
        await d.waitFrames(2);
        out.listening = d.snapshot();
        d.send({ v: 1, type: 'released', query_gen: 1 });
        const p = s.parsedMessage(1, 'is rainier out tomorrow at 4');
        d.send(p);
        await d.waitFrames(2);
        out.resolving = d.snapshot();
        d.send(s.tierFrame(1, p.scene_hash, 0));
        await d.waitMs(60);
        out.tier0 = d.snapshot();
        d.send(s.tierFrame(1, p.scene_hash, 1));
        await d.waitMs(40);
        out.fading = d.snapshot();
        await d.waitMs(250);
        out.tier1 = d.snapshot();
        d.send(s.tierFrame(1, p.scene_hash, 2));
        await d.waitMs(300);
        out.tier2 = d.snapshot();
        d.send({ v: 1, type: 'network', online: false });
        d.send({ v: 1, type: 'query_started', query_gen: 2 });
        d.send({ ...s.inboundExamples().find((e) => e.type === 'no_match')!, query_gen: 2 });
        await d.waitFrames(2);
        out.noMatch = d.snapshot();
        d.send({ v: 1, type: 'query_started', query_gen: 3 });
        d.send({ ...s.inboundExamples().find((e) => e.type === 'out_of_range')!, query_gen: 3 });
        await d.waitFrames(2);
        out.outOfRange = d.snapshot();
        d.send({ v: 1, type: 'query_started', query_gen: 4 });
        d.send({ v: 1, type: 'error', query_gen: 4, code: 'microphone_denied', fatal: true });
        await d.waitFrames(2);
        out.error = d.snapshot();
        d.send({ v: 1, type: 'query_started', query_gen: 5 });
        d.send({ v: 1, type: 'cancelled', query_gen: 5, reason: 'esc' });
        await d.waitFrames(2);
        out.idle = d.snapshot();
        return out;
      },
      SETTINGS,
    );
    const { listening, resolving, tier0, fading, tier1, tier2, noMatch, outOfRange, error, idle } = shots as Record<string, Snapshot>;
    expect(listening!.phase).toBe('listening');
    expect(listening!.pillVisible).toBe(true);
    expect(listening!.partial).toBe('is rainier out tomorrow');
    expect(listening!.chips.map((c) => c.text)).toEqual(['on-device']); // the destination chip shows while audio flows
    expect(resolving!.phase).toBe('resolving');
    expect(resolving!.chips.map((c) => c.id)).toEqual(['place', 'time', 'meridiem', 'destination']);
    expect(resolving!.chips.find((c) => c.id === 'meridiem')!.button).toBe(true);
    expect(tier0!.phase).toBe('showing');
    expect(tier0!.caption.line1.startsWith('REAL PHOTO, CLOSEST MATCH · taken ')).toBe(true);
    expect(tier0!.layers.map((l) => l.tier)).toEqual([0]);
    expect(tier0!.marks).toEqual(['shown', 'pending', 'pending']);
    // Tier 1 cross-fades over Tier 0 in 120 ms (PRD §5.5), then Tier 0 is removed.
    expect(fading!.layers.map((l) => [l.tier, l.animations, l.duration])).toEqual([[0, 0, 0], [1, 1, 120]]);
    expect(tier1!.layers.map((l) => [l.tier, l.animations, l.opacity])).toEqual([[1, 0, '1']]);
    expect(plain(tier1!.caption.line1)).toBe('SIMULATED · Fri, Nov 20, 4:00 PM PST · cloud low 80% · ceiling 2,300 m · visibility 12 km');
    expect(tier1!.line2Opacity).toBe('0'); // folded until hover or focus
    expect(plain(tier1!.caption.line2)).toContain('HRRR 18Z Thu, issued Thu, Nov 19, 11:27 AM PST (lead 28 h 33 min)');
    expect(tier1!.caption.credits).toContain('Forecast data: NOAA HRRR');
    expect(plain(tier1!.imageLabel).startsWith('Simulated. Fri, Nov 20, 4:00 PM PST. cloud low 80%.')).toBe(true);
    expect(plain(tier1!.live)).toBe(plain(tier1!.imageLabel));
    expect(tier2!.caption.line1.startsWith('SIMULATED + AI TEXTURE')).toBe(true);
    expect(tier2!.marks).toEqual(['shown', 'shown', 'shown']);
    expect(noMatch!.phase).toBe('no_match');
    expect(noMatch!.layers).toEqual([]);
    expect(noMatch!.message).toContain('No match');
    expect(noMatch!.message).toContain('Great Smoky Mountains');
    expect(outOfRange!.message).toContain('is in the past');
    expect(plain(outOfRange!.message)).toContain('Show Thu, Nov 26, 8:00 AM PST');
    expect(error!.message).toContain('Microphone access is off');
    expect(idle!.phase).toBe('idle');
    expect(idle!.layers).toEqual([]);
    // No state changes the panel's size: the caption block is fixed and line 2 opens over the image.
    for (const s of Object.values(shots as Record<string, Snapshot>)) expect(s.heights).toEqual(listening!.heights);
    expect(listening!.heights.image / 640).toBeCloseTo(9 / 16, 3);
    await page.context().close();
  });

  it('shows tiers without a fade under Reduce Motion, and unfolds line 2 on keyboard focus', async () => {
    const page = await mounted({ reducedMotion: 'reduce' });
    const r = await inPage(
      page,
      async (d, s, settings) => {
        d.send(settings);
        d.send({ v: 1, type: 'query_started', query_gen: 1 });
        const p = s.parsedMessage(1, 'is rainier out tomorrow at 4');
        d.send(p);
        d.send(s.tierFrame(1, p.scene_hash, 0));
        await d.waitMs(60);
        d.send(s.tierFrame(1, p.scene_hash, 1));
        await d.waitFrames(2);
        const after = d.snapshot();
        (document.querySelector('.sg-caption') as HTMLElement).focus();
        await d.waitFrames(2);
        return { after, focused: d.snapshot().line2Opacity };
      },
      SETTINGS,
    );
    expect(r.after.layers.map((l) => [l.tier, l.animations])).toEqual([[1, 0]]);
    expect(r.focused).toBe('1');
    await page.context().close();
  });

  it('answers the PRD §5.4 keys: Esc dismisses, arrows scrub, M flips, C copies with the caption burned in; Command keys pass', async () => {
    const page = await mounted();
    const sent = await inPage(
      page,
      async (d, s, settings) => {
        d.send(settings);
        d.send({ v: 1, type: 'query_started', query_gen: 7 });
        d.send({ v: 1, type: 'released', query_gen: 7 });
        const p = s.parsedMessage(7, 'is rainier out tomorrow at 4');
        d.send(p);
        d.send(s.tierFrame(7, p.scene_hash, 1));
        await d.waitMs(60);
        d.key('ArrowLeft');
        d.key('ArrowRight', { shiftKey: true });
        d.key('m');
        d.key('c', { metaKey: true });
        d.key('c');
        d.key('Escape');
        await d.waitFrames(1);
        return { sent: d.sent.filter((m) => m.type !== 'paint'), layers: d.snapshot().layers.length, phase: d.snapshot().phase };
      },
      SETTINGS,
    );
    expect(sent.sent.map((m) => m.type)).toEqual(['scrub', 'scrub', 'flip_meridiem', 'copy', 'dismiss']);
    expect(sent.sent.slice(0, 2).map((m) => m.minutes)).toEqual([-60, 10]);
    const copy = sent.sent[3]!;
    expect(String(copy.png_base64).startsWith('iVBORw0KGgo')).toBe(true); // a PNG
    expect(copy.refined).toBe(false);
    expect(sent.sent[4]).toEqual({ v: 1, type: 'dismiss', query_gen: 7 });
    expect([sent.layers, sent.phase]).toEqual([0, 'idle']);
    await page.context().close();
  });

  it('draws the pixel tag into the frame in harness mode and it reads back at 2x (PRD §10.4)', async () => {
    const page = await mounted({ dpr: 2 });
    const r = await inPage(
      page,
      async (d, s, settings) => {
        d.send({ ...settings, measuring: true });
        d.send({ v: 1, type: 'query_started', query_gen: 321 });
        const p = s.parsedMessage(321, 'is rainier out tomorrow at 4');
        d.send(p);
        d.send(s.tierFrame(321, p.scene_hash, 1));
        await d.waitMs(60);
        return { tag: d.topTag(), badge: d.snapshot().measuringBadge };
      },
      SETTINGS,
    );
    expect(r.tag).toEqual({ tag: { gen: 321, tier: 1 }, scale: 2 });
    expect(r.badge).toBe(true);
    await page.context().close();
  });

  it('keeps the destination chip on screen before acknowledging it, and opens credit links through the core (PRD §15.1)', async () => {
    const page = await mounted();
    const r = await inPage(
      page,
      async (d, s, settings) => {
        d.send(settings);
        d.send({ v: 1, type: 'query_started', query_gen: 1 });
        const before = d.frames;
        d.send({ v: 1, type: 'destination', seq: 4, destinations: [{ provider: 'Deepgram', carries: ['audio'] }] });
        const chipNow = d.snapshot().chips.find((c) => c.id === 'destination');
        let ackFrames = -1;
        while (ackFrames < 0) {
          await d.waitFrames(1);
          if (d.sent.some((m) => m.type === 'destination_shown' && m.seq === 4)) ackFrames = d.frames - before;
          if (d.frames - before > 20) break;
        }
        const p = s.parsedMessage(1, 'is rainier out tomorrow at 4');
        d.send(p);
        const f = s.tierFrame(1, p.scene_hash, 1) as { label: { credits: { text: string; url?: string }[] } };
        f.label.credits = [{ text: 'Weather data by Open-Meteo.com (modified)', url: 'https://open-meteo.com/' }];
        d.send(f);
        await d.waitMs(60);
        const href = location.href;
        (document.querySelector('.sg-credits a') as HTMLElement).click();
        await d.waitFrames(1);
        return { chipNow, ackFrames, opened: d.sent.filter((m) => m.type === 'open_url'), stayed: location.href === href };
      },
      SETTINGS,
    );
    expect(r.chipNow).toMatchObject({ text: '→ Deepgram', emphasis: 'warning' });
    expect(r.ackFrames).toBeGreaterThanOrEqual(2);
    expect(r.opened).toEqual([{ v: 1, type: 'open_url', url: 'https://open-meteo.com/' }]);
    expect(r.stayed).toBe(true);
    await page.context().close();
  });
});

/** A fresh driver of `width` px in the page (the default one is 640 px). */
type G = { SnowGlobeHarness: { ViewDriver: new (w: number) => D } };

describe('overlay view in headless Chromium: the review fixes', () => {
  it('keeps the longest captions unclipped at every panel width from 480 px, with a height fixed per width step (PRD §5.3, §5.5)', async () => {
    const page = await mounted();
    const widths = [480, 520, 560, 591, 592, 640, 680, 719, 720, 800, 1000];
    const fits = await inPage(
      page,
      async (_d, s, a) => {
        const out: { width: number; combo: string; fit: CaptionFit }[] = [];
        const combos = [
          { combo: 'Tier 2, GFS, rain, US, offline, 3 credits', tier: 2, gfs: true, rain: 1.3, online: false, units: 'us', locale: 'en-US' },
          { combo: 'Tier 2, HRRR, a trace of rain, metric, offline', tier: 2, gfs: false, rain: 0.04, online: false, units: 'metric', locale: 'en-US' },
          { combo: 'Tier 1, GFS, snow, US, offline', tier: 1, gfs: true, rain: 2.2, online: false, units: 'us', locale: 'en-US' },
          { combo: 'Tier 1, HRRR, en-GB, offline', tier: 1, gfs: false, rain: null, online: false, units: 'metric', locale: 'en-GB' },
          { combo: 'Tier 0, archive, offline', tier: 0, gfs: false, rain: null, online: false, units: 'us', locale: 'en-US' },
        ];
        for (const width of a.widths) {
          for (const c of combos) {
            const d = new (globalThis as unknown as G).SnowGlobeHarness.ViewDriver(width);
            d.send({ ...a.settings, locale: c.locale, units: c.units, reduce_motion: true });
            if (!c.online) d.send({ v: 1, type: 'network', online: false });
            d.send({ v: 1, type: 'query_started', query_gen: 1 });
            d.send({ v: 1, type: 'released', query_gen: 1 });
            const p = s.parsedMessage(1, 'rainier next wednesday at noon');
            d.send(p);
            const f = s.tierFrame(1, p.scene_hash, c.tier) as { label: Record<string, unknown> };
            if (c.tier > 0) {
              f.label.valid_time = '2026-11-25T12:00:00-08:00';
              if (c.gfs) f.label.run = { model: 'gfs', cycle_utc: '2026-11-19T12:00:00Z', available_at: '2026-11-19T16:10:00Z', pinned: true };
              f.label.forecast = { cloud_low: 0.1, cloud_mid: 0.6, cloud_high: 0.2, ceiling_m: 2300, visibility_m: 12000, precip_rate_mm_h: c.rain, precip_type: c.rain === null ? null : c.rain > 2 ? 'snow' : 'rain' };
              f.label.credits = [
                { text: c.gfs ? 'Forecast data: NOAA GFS via NOAA Open Data Dissemination' : 'Forecast data: NOAA HRRR via NOAA Open Data Dissemination' },
                { text: 'Webcam imagery courtesy National Park Service. Derived images are not NPS products.' },
                { text: 'Weather data by Open-Meteo.com (modified)', url: 'https://open-meteo.com/' },
              ];
            }
            d.send(f);
            await d.waitMs(60);
            out.push({ width, combo: c.combo, fit: d.captionFit() });
            d.destroy();
          }
        }
        return out;
      },
      { settings: SETTINGS, widths },
    );
    for (const { width, combo, fit } of fits) {
      const step = [...CAPTION_STEPS].reverse().find((st) => width >= st.fromPx)!;
      expect(fit.heightPx, `${width} px`).toBe(step.heightPx);
      expect(fit.overflowPx, `${combo} at ${width} px: ${JSON.stringify(fit)}`).toBeLessThanOrEqual(0.5);
      expect(fit.overflowXPx, `${combo} at ${width} px`).toBeLessThanOrEqual(0.5);
    }
    const worst = fits.filter((f) => f.width === 480).map((f) => f.fit.lines);
    console.log(`caption fit: ${fits.length} renders, 0 clipped; lines at 480 px (notice, line 1, credits): ${JSON.stringify(worst)}`);
    await page.context().close();
  });

  it('reports a cross-fade cut short by a scrub without a 50 % time, and the previous frame’s time on the replacing paint', async () => {
    const page = await mounted();
    const paints = await inPage(
      page,
      async (d, s, settings) => {
        d.send(settings);
        d.send({ v: 1, type: 'query_started', query_gen: 1 });
        const p = s.parsedMessage(1, 'is rainier out tomorrow at 4');
        d.send(p);
        d.send(s.tierFrame(1, p.scene_hash, 0));
        await d.waitMs(60);
        d.send(s.tierFrame(1, p.scene_hash, 1));
        await d.waitFrames(2); // Tier 1 is fading in
        const q = s.parsedMessage(1, 'grand canyon golden hour');
        d.send({ ...q, type: 'scene_changed', cause: 'scrub' });
        d.send(s.tierFrame(1, q.scene_hash, 0));
        await d.waitMs(300);
        return { first: p.scene_hash, second: q.scene_hash, reports: d.sent.filter((m) => m.type === 'paint' && m.outcome === 'painted') };
      },
      SETTINGS,
    );
    const t1 = paints.reports.find((m) => m.scene_hash === paints.first && m.tier === 1)!;
    expect(t1.faded).toBe(true);
    expect(t1).not.toHaveProperty('half_ms');
    const replaced = paints.reports.find((m) => m.scene_hash === paints.second)!;
    expect(replaced.previous_ms as number).toBeGreaterThan(0);
    await page.context().close();
  });

  it('acts once on a held M or C, and lets a held arrow keep scrubbing', async () => {
    const page = await mounted();
    const r = await inPage(
      page,
      async (d, s, settings) => {
        d.send(settings);
        d.send({ v: 1, type: 'query_started', query_gen: 1 });
        d.send({ v: 1, type: 'released', query_gen: 1 });
        const p = s.parsedMessage(1, 'is rainier out tomorrow at 4');
        d.send(p);
        d.send(s.tierFrame(1, p.scene_hash, 1));
        await d.waitMs(60);
        for (const k of ['m', 'c', 'ArrowRight']) {
          d.key(k);
          for (let i = 0; i < 9; i++) d.key(k, { repeat: true });
        }
        const count = (t: string): number => d.sent.filter((m) => m.type === t).length;
        return { flips: count('flip_meridiem'), copies: count('copy'), scrubs: count('scrub') };
      },
      SETTINGS,
    );
    expect(r).toEqual({ flips: 1, copies: 1, scrubs: 10 });
    await page.context().close();
  });

  it('follows the core’s Reduce Motion setting in both directions for every animation, and the system setting when it sends none', async () => {
    const run = async (system: 'reduce' | 'no-preference', setting: boolean | null) => {
      const page = await mounted({ reducedMotion: system });
      const r = await inPage(
        page,
        async (d, s, a) => {
          d.send({ ...a.settings, reduce_motion: a.setting });
          d.send({ v: 1, type: 'query_started', query_gen: 1 });
          d.send({ v: 1, type: 'released', query_gen: 1 });
          await d.waitFrames(3);
          const resolving = d.snapshot();
          const p = s.parsedMessage(1, 'is rainier out tomorrow at 4');
          d.send(p);
          d.send(s.tierFrame(1, p.scene_hash, 0));
          await d.waitMs(60);
          d.send(s.tierFrame(1, p.scene_hash, 1));
          await d.waitFrames(2);
          const fade = d.snapshot().layers.at(-1)!.animations;
          return { attr: resolving.reduceMotion, dots: resolving.dotAnimations, line2: resolving.line2Transition, fade };
        },
        { settings: SETTINGS, setting },
      );
      await page.context().close();
      return r;
    };
    const still = { attr: 'true', dots: 0, line2: '0s', fade: 0 };
    const moving = { attr: 'false', dots: 3, line2: '0.12s', fade: 1 };
    expect(await run('no-preference', true)).toEqual(still); // the override wins over the system
    expect(await run('reduce', false)).toEqual(moving);
    expect(await run('reduce', null)).toEqual(still); // no override: the system's setting
    expect(await run('no-preference', null)).toEqual(moving);
  });

  it('shows a non-fatal error as a status line, and a fatal one with copy true of the cleared panel', async () => {
    const page = await mounted();
    const r = await inPage(
      page,
      async (d, s, settings) => {
        d.send(settings);
        d.send({ v: 1, type: 'query_started', query_gen: 1 });
        d.send({ v: 1, type: 'released', query_gen: 1 });
        const p = s.parsedMessage(1, 'is rainier out tomorrow at 4');
        d.send(p);
        d.send(s.tierFrame(1, p.scene_hash, 0));
        await d.waitMs(60);
        d.send({ v: 1, type: 'error', query_gen: 1, code: 'render_failed', fatal: false });
        await d.waitFrames(2);
        const soft = d.snapshot();
        d.send({ v: 1, type: 'error', query_gen: 1, code: 'render_failed', fatal: true });
        await d.waitFrames(2);
        const hard = d.snapshot();
        d.send({ v: 1, type: 'destination', seq: 1, destinations: [{ provider: 'Deepgram', carries: ['audio'] }] });
        d.send({ v: 1, type: 'query_started', query_gen: 2 });
        d.send({ v: 1, type: 'error', query_gen: 2, code: 'speech_unavailable', fatal: true });
        await d.waitFrames(2);
        return { soft, hard, speech: d.snapshot().message };
      },
      SETTINGS,
    );
    expect([r.soft.phase, r.soft.layers.length, r.soft.alertVisible]).toEqual(['showing', 1, true]);
    expect(r.soft.alert).toBe('The simulated frame could not be drawn. The real photo stays on screen.');
    expect(r.soft.live).toContain('The real photo stays on screen.');
    expect([r.hard.phase, r.hard.layers.length]).toEqual(['error', 0]);
    expect(r.hard.message).not.toContain('stays on screen');
    expect(r.hard.message).toContain('The frame could not be drawn');
    expect(r.speech).toContain('audio may have reached Deepgram');
    expect(r.speech).not.toContain('Nothing was sent');
    await page.context().close();
  });

  it('marks a scrubbed frame as the previous time after 250 ms, and says so when nothing can come for the new time', async () => {
    const page = await mounted();
    const r = await inPage(
      page,
      async (d, s, settings) => {
        d.send(settings);
        d.send({ v: 1, type: 'query_started', query_gen: 1 });
        d.send({ v: 1, type: 'released', query_gen: 1 });
        const p = s.parsedMessage(1, 'is rainier out tomorrow at 4');
        d.send(p);
        d.send(s.tierFrame(1, p.scene_hash, 1));
        await d.waitMs(60);
        const q = s.parsedMessage(1, 'grand canyon golden hour');
        d.send({ ...q, type: 'scene_changed', cause: 'scrub' });
        await d.waitFrames(2);
        const quick = d.snapshot().previousVisible;
        await d.waitMs(320);
        const slow = d.snapshot();
        d.send({ v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: q.scene_hash, tier: 0, reason: 'disabled' });
        d.send({ v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: q.scene_hash, tier: 1, reason: 'structure' });
        d.send({ v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: q.scene_hash, tier: 2, reason: 'spend_cap' });
        await d.waitFrames(2);
        return { quick, slow, empty: d.snapshot() };
      },
      SETTINGS,
    );
    expect(r.quick).toBe(false); // a quick scrub does not flash the badge
    expect([r.slow.previousVisible, r.slow.layers.length]).toEqual([true, 1]);
    expect(r.slow.live).toContain('The previous time is still on screen.');
    expect([r.empty.previousVisible, r.empty.layers.length, r.empty.pillVisible]).toEqual([false, 0, false]);
    expect(r.empty.message).toContain('Nothing to show for this time');
    expect(r.empty.marks).toEqual(['rejected', 'rejected', 'rejected']);
    await page.context().close();
  });

  it('withdraws a stable partial’s frame on No match, and completes the Out of range offer with a click (PRD §5.2, §7.2)', async () => {
    const page = await mounted();
    const r = await inPage(
      page,
      async (d, s, settings) => {
        d.send(settings);
        d.send({ v: 1, type: 'query_started', query_gen: 1 });
        d.send({ v: 1, type: 'partial', query_gen: 1, text: 'is rainier out', stable: true });
        const p = s.parsedMessage(1, 'is rainier out tomorrow at 4');
        d.send(p);
        d.send(s.tierFrame(1, p.scene_hash, 0));
        await d.waitMs(60);
        const early = d.snapshot().layers.length;
        d.send({ v: 1, type: 'released', query_gen: 1 });
        d.send({ v: 1, type: 'no_match', query_gen: 1, transcript: 'golden gate at 9', closest: [] });
        await d.waitFrames(2);
        const noMatch = d.snapshot();
        d.send({ v: 1, type: 'query_started', query_gen: 2 });
        d.send({ v: 1, type: 'released', query_gen: 2 });
        d.send({ ...s.inboundExamples().find((e) => e.type === 'out_of_range')!, query_gen: 2 });
        await d.waitFrames(2);
        (d.root.querySelector('[data-action="use_offer"]') as HTMLElement).click();
        const offer = d.sent.at(-1);
        const q = s.parsedMessage(2, 'is rainier out tomorrow at 4');
        d.send({ ...q, type: 'scene_changed', cause: 'use_offer' });
        d.send(s.tierFrame(2, q.scene_hash, 1));
        await d.waitMs(80);
        return { early, noMatch, offer, answered: d.snapshot() };
      },
      SETTINGS,
    );
    expect(r.early).toBe(1);
    expect([r.noMatch.phase, r.noMatch.layers.length]).toEqual(['no_match', 0]);
    expect(r.offer).toMatchObject({ type: 'use_offer', query_gen: 2 });
    expect([r.answered.phase, r.answered.layers.map((l) => l.tier), r.answered.message]).toEqual(['showing', [1], '']);
    expect(r.answered.chips.map((c) => c.id)).toEqual(['place', 'time', 'meridiem', 'destination']);
    expect(r.answered.caption.line1.startsWith('SIMULATED')).toBe(true);
    await page.context().close();
  });

  it('acknowledges no destination while the panel is hidden, only once its chip is up in a key press (PRD §15.1)', async () => {
    const page = await mounted();
    const r = await inPage(
      page,
      async (d, _s, settings) => {
        d.send(settings);
        d.send({ v: 1, type: 'destination', seq: 1, destinations: [{ provider: 'Deepgram', carries: ['audio'] }] });
        await d.waitFrames(10);
        const whileIdle = d.sent.filter((m) => m.type === 'destination_shown').length;
        const before = d.frames;
        d.send({ v: 1, type: 'query_started', query_gen: 1 });
        while (!d.sent.some((m) => m.type === 'destination_shown') && d.frames - before < 20) await d.waitFrames(1);
        const result = { whileIdle, ack: d.sent.find((m) => m.type === 'destination_shown'), frames: d.frames - before, chip: d.snapshot().chips.at(-1)?.text };
        // On-device only, and the core still names a cloud destination: refused, shown as a warning, never acknowledged.
        d.send({ ...settings, on_device_only: true });
        d.send({ v: 1, type: 'query_started', query_gen: 2 });
        d.send({ v: 1, type: 'destination', seq: 2, destinations: [{ provider: 'Deepgram', carries: ['audio'] }] });
        await d.waitFrames(10);
        const conflict = d.snapshot().chips.at(-1);
        return {
          ...result,
          conflictChip: conflict ? { text: conflict.text, emphasis: conflict.emphasis, label: conflict.label } : null,
          conflictAcks: d.sent.filter((m) => m.type === 'destination_shown' && m.seq === 2).length,
          conflictErrors: d.sent.filter((m) => m.type === 'protocol_error' && m.message_type === 'destination').length,
        };
      },
      SETTINGS,
    );
    expect(r.whileIdle).toBe(0);
    expect(r.ack).toMatchObject({ seq: 1, query_gen: 1 });
    expect(r.frames).toBeGreaterThanOrEqual(2);
    expect(r.chip).toBe('→ Deepgram');
    expect(r.conflictChip).toMatchObject({ text: '→ Deepgram', emphasis: 'warning' });
    expect(r.conflictChip!.label).toContain('contradicts the on-device-only setting');
    expect([r.conflictAcks, r.conflictErrors]).toEqual([0, 1]);
    await page.context().close();
  });
});
