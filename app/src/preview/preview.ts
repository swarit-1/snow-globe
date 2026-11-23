/**
 * The overlay preview page (for the owner to look at every state of PRD §5.2 with sample captions).
 * Each panel is the real overlay module, mounted and driven by the same protocol messages the core
 * will send. The images are drawn here procedurally (sky, ridge, cloud bands): no camera frame is
 * used, so the page is safe to open anywhere. Built by `pnpm preview` into app/dist/preview.html.
 */
import { mountOverlay } from '../overlay/index.ts';
import type { FrameLoader } from '../overlay/scheduler.ts';
import type { Tier } from '../overlay/gate.ts';
import { DISPLAY, GFS_CREDIT, NPS_CREDIT, parsedMessage, photoLabel, refinedLabel, sceneOf, coreQuery, simulatedLabel, syntheticUrl, tierFrame } from '../overlay/samples.ts';

type Msg = Record<string, unknown>;

interface Panel {
  readonly title: string;
  readonly note: string;
  readonly messages: (Msg | { wait: number })[];
  readonly reduceMotion?: boolean;
  readonly openLine2?: boolean;
}

const settings = (o: Partial<Record<string, unknown>> = {}): Msg => ({ v: 1, type: 'settings', on_device_only: false, cloud_speech: false, tier2: true, locale: 'en-US', units: 'metric', reduce_motion: null, measuring: false, ...o });
const started = (g: number): Msg => ({ v: 1, type: 'query_started', query_gen: g });
const released = (g: number): Msg => ({ v: 1, type: 'released', query_gen: g });
const local: Msg = { v: 1, type: 'destination', seq: 1, destinations: [] };
/** A speech-like run of microphone levels for the waveform. */
const levels = (g: number): Msg[] => Array.from({ length: 22 }, (_, i) => ({ v: 1, type: 'audio_level', query_gen: g, level: Math.min(1, 0.15 + 0.7 * Math.abs(Math.sin(i * 0.9)) * (0.6 + 0.4 * Math.cos(i * 0.37))) }));

/** A procedural landscape for a frame: `place` picks the ridge, `tier` the look. */
function drawScene(place: string, tier: Tier, w = 960, h = 540): HTMLCanvasElement {
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  const g = c.getContext('2d')!;
  const dusk = place.includes('sunset') || place.includes('golden');
  const sky = g.createLinearGradient(0, 0, 0, h * 0.7);
  sky.addColorStop(0, dusk ? '#29324f' : '#4b6a92');
  sky.addColorStop(1, dusk ? '#e39a63' : tier === 0 ? '#b9c3cc' : '#c9d3db');
  g.fillStyle = sky;
  g.fillRect(0, 0, w, h);
  // Cloud bands (a low deck for the Rainier example).
  g.globalAlpha = 0.55;
  g.fillStyle = '#e9edf1';
  for (let i = 0; i < 5; i++) g.fillRect(0, h * (0.32 + i * 0.035), w, h * 0.02 + (i % 2) * 6);
  g.globalAlpha = 1;
  // A ridge: a cone for the mountain viewpoints, a headland for the coast.
  g.fillStyle = place.includes('reyes') ? '#2f3b2c' : '#d8dde2';
  g.beginPath();
  if (place.includes('reyes')) {
    g.moveTo(0, h * 0.62);
    g.bezierCurveTo(w * 0.3, h * 0.5, w * 0.6, h * 0.66, w, h * 0.58);
  } else {
    g.moveTo(w * 0.18, h * 0.7);
    g.lineTo(w * 0.52, h * 0.24);
    g.lineTo(w * 0.86, h * 0.7);
  }
  g.lineTo(w, h);
  g.lineTo(0, h);
  g.closePath();
  g.fill();
  g.fillStyle = '#26301f';
  g.fillRect(0, h * 0.74, w, h * 0.26);
  // Tier looks: the photo has sensor grain, the refined frame has ground texture, the simulation neither.
  if (tier !== 1) {
    const img = g.getImageData(0, tier === 2 ? Math.floor(h * 0.6) : 0, w, tier === 2 ? Math.ceil(h * 0.4) : h);
    let seed = 7 + tier;
    for (let i = 0; i < img.data.length; i += 4) {
      seed = (seed * 1103515245 + 12345) >>> 0;
      const n = ((seed >>> 16) & 0xff) / 255 - 0.5;
      const k = tier === 2 ? 26 : 10;
      img.data[i] = img.data[i]! + n * k;
      img.data[i + 1] = img.data[i + 1]! + n * k;
      img.data[i + 2] = img.data[i + 2]! + n * k;
    }
    g.putImageData(img, 0, tier === 2 ? Math.floor(h * 0.6) : 0);
  }
  return c;
}

const loader: FrameLoader = async (msg) => {
  const bmp = await createImageBitmap(drawScene(msg.frame.url, msg.tier));
  return { source: bmp, width: bmp.width, height: bmp.height, release: () => bmp.close() };
};

const rainier = parsedMessage(1, 'is rainier out tomorrow at 4');
const reyes = parsedMessage(1, 'point reyes at sunset friday');
const canyon = parsedMessage(1, 'grand canyon golden hour');
const R = rainier.scene_hash;
const frame = (scene: string, tier: Tier, url: string, label?: Msg): Msg => ({ ...(tierFrame(1, scene, tier, url) as unknown as Msg), ...(label ? { label } : {}) });

const PANELS: Panel[] = [
  {
    title: 'Listening',
    note: 'Key held: the pill shows the waveform and the live partial transcript. The destination chip says audio stays on this Mac.',
    messages: [settings(), local, started(1), ...levels(1), { v: 1, type: 'partial', query_gen: 1, text: 'is rainier out tomorrow', stable: false }],
  },
  {
    title: 'Listening, cloud speech on',
    note: 'With the cloud toggle on (bake-off only, D-015), the chip names the provider before the first byte leaves.',
    messages: [settings({ cloud_speech: true }), { v: 1, type: 'destination', seq: 1, destinations: [{ provider: 'Deepgram', carries: ['audio'] }] }, started(1), ...levels(1), { v: 1, type: 'partial', query_gen: 1, text: 'point reyes at sunset', stable: true }],
  },
  {
    title: 'Resolving',
    note: 'Key up: the chips show the place, the valid time with date and zone, and an AM/PM flip (M) for the ambiguous "at 4".',
    messages: [settings(), local, started(1), { v: 1, type: 'partial', query_gen: 1, text: 'is rainier out tomorrow at 4', stable: true }, released(1), rainier],
  },
  {
    title: 'Tier 0: real photo, closest match',
    note: 'A real archive photo, with when it was taken (a year earlier, so the year shows) and what it matched on.',
    messages: [settings(), local, started(1), released(1), reyes, frame(reyes.scene_hash, 0, syntheticUrl('reyes-sunset'))],
  },
  {
    title: 'Tier 1: simulated',
    note: 'The PRD §5.3 example. Hover the caption to unfold line 2 (run, issue time, lead, Sun, Moon).',
    messages: [settings(), local, started(1), released(1), rainier, frame(R, 0, syntheticUrl('rainier')), { wait: 50 }, frame(R, 1, syntheticUrl('rainier'))],
  },
  {
    title: 'Tier 1, line 2 unfolded',
    note: 'The folded second line, shown open. It opens over the image, so the panel never changes height.',
    openLine2: true,
    messages: [settings(), local, started(1), released(1), rainier, frame(R, 1, syntheticUrl('rainier'))],
  },
  {
    title: 'Tier 2: simulated + AI texture',
    note: 'All three marks filled. The caption says the sky and haze are the simulation’s; the texture provider is named on line 2.',
    messages: [settings(), { v: 1, type: 'destination', seq: 1, destinations: [{ provider: 'fal', carries: ['image', 'text'] }] }, started(1), released(1), rainier, frame(R, 1, syntheticUrl('rainier')), { wait: 200 }, frame(R, 2, syntheticUrl('rainier'))],
  },
  {
    title: 'Tier 2 rejected',
    note: 'The guardrails refused Tier 2 (PRD §9.7): Tier 1 stays and the third mark is hollow.',
    messages: [settings(), local, started(1), released(1), rainier, frame(R, 1, syntheticUrl('rainier')), { v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: R, tier: 2, reason: 'structure' }],
  },
  {
    title: 'On-device only',
    note: 'The setting is on: the chip says so, and the Tier 2 mark is off (dashed), because refinement would send a frame out.',
    messages: [settings({ on_device_only: true, tier2: false }), local, started(1), released(1), rainier, frame(R, 1, syntheticUrl('rainier'))],
  },
  {
    title: 'Offline',
    note: 'No network: cached tiers, with where the forecast is from above the caption, and an offline chip.',
    messages: [settings(), local, { v: 1, type: 'network', online: false }, started(1), released(1), rainier, frame(R, 1, syntheticUrl('rainier'))],
  },
  {
    title: 'No match',
    note: '"Golden Gate at 9" before any licence: the transcript and the three closest known places, never a guess.',
    messages: [settings(), local, started(1), released(1), { v: 1, type: 'no_match', query_gen: 1, transcript: 'golden gate at 9', closest: [{ place_id: 'grand-teton', place: 'Grand Teton', score: 0.38 }, { place_id: 'yosemite', place: 'Yosemite', score: 0.36 }, { place_id: 'smokies', place: 'Great Smoky Mountains', score: 0.34 }] }],
  },
  {
    title: 'Out of range',
    note: '"Rainier yesterday at 4": the horizon and an offer of the latest valid time (the core’s parser output).',
    messages: [settings(), local, started(1), released(1), { v: 1, type: 'out_of_range', query_gen: 1, reason: 'past', place_id: 'rainier', viewpoint_id: 'rainier.paradise-mountain', tz: 'America/Los_Angeles', requested: '2026-11-18T16:00:00-08:00', latest_valid: '2026-11-26T08:00:00-08:00', display: DISPLAY.rainier }],
  },
  {
    title: 'Follow-up, rolled to tomorrow',
    note: '"Rainier at 7 am" after 7 had passed: the date chip is highlighted. A follow-up with no place names the last place.',
    messages: [settings(), local, started(1), released(1), { ...parsedMessage(1, 'rainier at 7 am') }, frame(sceneOf(coreQuery('rainier at 7 am')), 1, syntheticUrl('rainier'), { ...simulatedLabel({ valid_time: '2026-11-20T07:00:00-08:00' }) } as unknown as Msg)],
  },
  {
    title: 'A follow-up with no place',
    note: '"What about midnight?" after asking about Rainier: the chip names the last place asked about (PRD §7.4).',
    messages: [settings(), local, started(1), { v: 1, type: 'partial', query_gen: 1, text: 'what about midnight', stable: true }, released(1), parsedMessage(1, 'what about midnight')],
  },
  {
    title: 'A daylight-saving fold',
    note: '"Rainier Sunday at 1:30 am", asked on Fri 30 Oct: 1:30 happens twice on 1 Nov, so the earlier offset is used and flagged.',
    messages: [settings(), local, started(1), { v: 1, type: 'partial', query_gen: 1, text: 'rainier sunday at 1:30 am', stable: true }, released(1), parsedMessage(1, 'rainier sunday at 1:30 am')],
  },
  {
    title: 'Beyond 48 hours: GFS',
    note: '"Rainier next Wednesday at noon": a pinned GFS run serves it, and the chip and caption say so.',
    messages: [
      settings({ units: 'us' }),
      local,
      started(1),
      released(1),
      parsedMessage(1, 'rainier next wednesday at noon'),
      frame(sceneOf(coreQuery('rainier next wednesday at noon')), 1, syntheticUrl('rainier'), simulatedLabel({ valid_time: '2026-11-25T12:00:00-08:00', run: { model: 'gfs', cycle_utc: '2026-11-19T12:00:00Z', available_at: '2026-11-19T16:10:00Z', pinned: true }, credits: [GFS_CREDIT, NPS_CREDIT] }) as unknown as Msg),
    ],
  },
  {
    title: 'Golden hour, en-GB',
    note: '"Grand Canyon golden hour" in a British English locale: 24-hour time and day-month dates; the chip names the event and its span.',
    messages: [settings({ locale: 'en-GB' }), local, started(1), released(1), canyon, frame(canyon.scene_hash, 1, syntheticUrl('canyon-golden'), simulatedLabel({ valid_time: '2026-11-19T17:02:45-07:00', tz: 'America/Phoenix', sun_altitude_deg: 2 }) as unknown as Msg)],
  },
  {
    title: 'Error',
    note: 'A fatal error ends the query like Esc and says what to do. Errors carry codes only, never text.',
    messages: [settings(), local, started(1), { v: 1, type: 'error', query_gen: 1, code: 'microphone_denied', fatal: true }],
  },
  {
    title: 'Harness mode (measuring)',
    note: 'Only for the M1 paint-capture harness: the pixel tag (query_gen, tier) at the top left, drawn in the frame itself, and the MEASURING badge.',
    messages: [settings({ measuring: true }), local, { ...started(1), query_gen: 321 }, { ...released(1), query_gen: 321 }, { ...rainier, query_gen: 321 }, { ...frame(R, 1, syntheticUrl('rainier')), query_gen: 321 }],
  },
  {
    title: 'Tier 0 from our own captures',
    note: 'A frame from our own captures (Rainier has no archive, D-019), taken before the question was asked (PRD §9.1), matched on Sun position only because the forecast had not loaded.',
    messages: [settings(), local, started(1), released(1), rainier, frame(R, 0, syntheticUrl('rainier'), photoLabel({ taken_at: '2026-11-18T15:45:00-08:00', source: 'capture', camera_name: 'Paradise camera', matched_on: ['sun_position'] }) as unknown as Msg)],
  },
  {
    title: 'Tier 2, Increase Contrast',
    note: 'With Increase Contrast the greys brighten (a media query); the palette stays colourless outside the image.',
    messages: [settings(), local, started(1), released(1), rainier, frame(R, 1, syntheticUrl('rainier')), frame(R, 2, syntheticUrl('rainier'), refinedLabel() as unknown as Msg)],
  },
  {
    title: 'A non-fatal error',
    note: 'The simulation could not be drawn: the real photo stays, and a status line says so (and only says the photo stays when one is on screen).',
    messages: [settings(), local, started(1), released(1), reyes, frame(reyes.scene_hash, 0, syntheticUrl('reyes-sunset')), { wait: 60 }, { v: 1, type: 'error', query_gen: 1, code: 'render_failed', fatal: false }],
  },
  {
    title: 'Scrubbed, the new time on its way',
    note: 'Right arrow moved to 5 PM: the chips show the new time; the 4 PM frame keeps its own, true caption, and after 250 ms a badge says it is the previous time.',
    messages: [
      settings(),
      local,
      started(1),
      released(1),
      rainier,
      frame(R, 1, syntheticUrl('rainier')),
      { wait: 60 },
      { ...parsedMessage(1, 'is rainier out tomorrow at 4'), type: 'scene_changed', cause: 'scrub', scene_hash: 'feedc0de00000005', query: { ...coreQuery('is rainier out tomorrow at 4'), valid_time: '2026-11-20T17:00:00-08:00', valid_time_utc: '2026-11-21T01:00:00Z', ambiguities: [] } },
    ],
  },
  {
    title: 'Nothing to show for this time',
    note: 'Every tier of the scene was rejected or failed: the panel says so instead of waiting, and the marks are hollow.',
    messages: [
      settings(),
      local,
      started(1),
      released(1),
      rainier,
      { v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: R, tier: 0, reason: 'disabled' },
      { v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: R, tier: 1, reason: 'structure' },
      { v: 1, type: 'tier_rejected', query_gen: 1, scene_hash: R, tier: 2, reason: 'spend_cap' },
    ],
  },
];

const PAGE_CSS = `
:root { color-scheme: dark; }
body { margin: 0; background: #16181c; color: #d8dbe0; font: 13px/1.5 -apple-system, BlinkMacSystemFont, system-ui, sans-serif; }
header { padding: 28px 28px 8px; max-width: 1100px; }
header h1 { margin: 0 0 6px; font-size: 18px; font-weight: 600; color: #f1f3f5; }
header p { margin: 0 0 6px; color: #a6a9ae; max-width: 80ch; }
main { display: grid; grid-template-columns: repeat(auto-fill, minmax(480px, 1fr)); gap: 28px 24px; padding: 20px 28px 48px; }
figure { margin: 0; }
figcaption { margin: 10px 2px 0; }
figcaption b { display: block; color: #f1f3f5; font-weight: 600; }
figcaption span { color: #a6a9ae; }
.frame { width: 480px; max-width: 100%; border-radius: 10px; overflow: hidden; box-shadow: 0 0 0 1px #2a2d32, 0 12px 30px rgba(0,0,0,.45); }
.sg-preview-open .sg-line2 { opacity: 1 !important; }
.hc .sg-overlay { --sg-fg: #ffffff; --sg-dim: #d2d6db; --sg-faint: #a0a3a9; --sg-rule: #5e6168; --sg-chip-bg: rgba(0, 0, 0, 0.94); }
`;

async function main(): Promise<void> {
  const style = document.createElement('style');
  style.textContent = PAGE_CSS;
  document.head.appendChild(style);
  document.title = 'Snow Globe overlay preview';
  const header = document.createElement('header');
  header.innerHTML = '<h1>Snow Globe overlay: every state</h1><p>Each panel below is the real overlay module (app/src/overlay), driven by the same protocol messages the Rust core will send. The images are procedural stand-ins drawn by this page; no camera frame is used. Captions are composed live from sample labels that match the PRD §5.3 examples.</p><p>Hover a caption to unfold its second line. Times are always in the place’s zone, with the date.</p>';
  document.body.appendChild(header);
  const grid = document.createElement('main');
  document.body.appendChild(grid);
  for (const p of PANELS) {
    const fig = document.createElement('figure');
    const holder = document.createElement('div');
    holder.className = 'frame';
    if (p.openLine2) holder.classList.add('sg-preview-open');
    if (p.title.includes('Increase Contrast')) holder.classList.add('hc');
    fig.appendChild(holder);
    const cap = document.createElement('figcaption');
    const b = document.createElement('b');
    b.textContent = p.title;
    const s = document.createElement('span');
    s.textContent = p.note;
    cap.append(b, s);
    fig.appendChild(cap);
    grid.appendChild(fig);
    const overlay = mountOverlay(holder, { send: () => {}, loader, keyTarget: null });
    for (const m of p.messages) {
      if ('wait' in m) await new Promise((r) => setTimeout(r, m.wait as number));
      else if (!overlay.receive(JSON.stringify(m))) console.warn(`preview: refused ${String((m as Msg).type)} in ${p.title}`);
    }
  }
  await new Promise((r) => setTimeout(r, 400));
  document.body.dataset.ready = 'true';
}

void main();
