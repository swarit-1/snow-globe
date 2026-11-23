/**
 * The browser key-mash stress run (PRD §10.3: a randomised key-mash stress test, >= 10,000 cycles with
 * injected stage delays, must show zero stale paints). It runs in a real page (headless Chromium via
 * Playwright in test/stress.browser.test.ts) and drives the real overlay, DOM view included, on the
 * browser's own event loop: messages arrive as separate tasks, frames decode through
 * createImageBitmap, paints happen in requestAnimationFrame callbacks, and cross-fades run on the Web
 * Animations API. Nothing in the overlay is replaced; only the core is simulated.
 *
 * Each lane is one overlay with its own simulated core. The core follows the Rust key-mash's model
 * (app/core/tests/keymash.rs), on real timers with delays scaled to the webview's frame rate: key
 * presses, often faster than the pipeline (mashing); release; final transcript; parse; Tier 0, 1 and
 * 2 stages with heavy-tailed delays; scrubs; Esc from the core's shortcut and from the focused panel;
 * No match, Out of range and fatal errors. Beyond the Rust model: a stable partial sometimes resolves
 * a scene before key-up (PRD §7.2) whose stages run, and the final transcript then often ends the
 * query as No match or Out of range, withdrawing that scene; and the Out of range offer is sometimes
 * taken with a real click on its button, which the core answers with `scene_changed`. A stage whose
 * query was cancelled, or whose scene was withdrawn, drops its work only 60 % of the time and
 * delivers it late otherwise, to stress the gate. The webview side adds its own delays: each decode
 * waits 0–14 ms (5 % up to 90 ms) and 1 % fail. Messages reach the overlay in order, one task each,
 * as JSON text, with occasional transport stalls.
 *
 * An oracle keeps, from the messages as they are delivered (never from the overlay's state), what the
 * user should be seeing: the latest key press, whether Esc came since, the scene selected now (none
 * after a No match or Out of range), and the best tier painted for it. Checks:
 * - **stale paints:** a frame drawn that is not fresh by the oracle at the moment of drawing (must be 0);
 * - **wrong drops:** a fresh frame the gate refused;
 * - **residue:** a frame of an older query, of a dismissed one, or of a withdrawn scene, still on screen
 *   at a later frame;
 * - **surface mismatches:** the pixel tag or the pixels on a layer are not the frame it claims to be;
 * - **hold overruns:** a Tier 0 held by the skip rule past its window by more than HOLD_SLACK_MS once
 *   two frames have passed since the deadline (the real scheduler paints by the first of them);
 * - **unjustified skips:** a Tier 0 skipped without a higher tier of its scene drawn in the same step;
 * - **unshown acks:** a `destination_shown` while the panel did not show that destination's chip in
 *   that key press (checked against the DOM the frame before, PRD §15.1);
 * - **missing acks:** a destination chip on screen for five frames with no acknowledgement;
 * - **stuck:** frames still waiting after the run has drained;
 * - **protocol errors:** any message the overlay refused.
 *
 * The run is randomised from a seed but not bit-reproducible: real timing decides the interleaving.
 */
import { sceneHashFromBigInt, type PaintKey, type Tier } from '../overlay/gate.ts';
import { mountOverlay, type MountedOverlay } from '../overlay/index.ts';
import type { Outbound } from '../overlay/protocol.ts';
import { coreQuery, DISPLAY, photoLabel, refinedLabel, simulatedLabel, syntheticUrl } from '../overlay/samples.ts';
import { FRAME_MS, SKIP_WINDOW_MS, type FrameLoader, type PaintEvent } from '../overlay/scheduler.ts';
import { readTag, CELLS, CELL_PX } from '../overlay/tag.ts';
import { variantOptions, type Variant } from './mutants.ts';
import { Rng } from './rng.ts';

export interface StressOptions {
  readonly variant: Variant;
  readonly lanes: number;
  readonly cyclesPerLane: number;
  /** Seed as hex text (it crosses the Playwright boundary). */
  readonly seed: string;
  /** Lane width in CSS px (the image is 16:9 of it). */
  readonly laneWidth?: number;
  /** How long to wait after the last event before the final checks, ms. */
  readonly drainMs?: number;
}

/** Percentiles of a sample, in ms. */
export interface Pct {
  readonly n: number;
  readonly p50: number;
  readonly p95: number;
  readonly p99: number;
  readonly max: number;
}

export interface Violations {
  stalePaints: number;
  wrongDrops: number;
  residue: number;
  surfaceMismatches: number;
  holdOverruns: number;
  unjustifiedSkips: number;
  unshownAcks: number;
  missingAcks: number;
  stuck: number;
  protocolErrors: number;
}

export interface StressReport {
  readonly variant: Variant;
  readonly lanes: number;
  readonly cycles: number;
  readonly wallMs: number;
  readonly counts: Record<string, number>;
  readonly dropped: Record<string, number>;
  readonly violations: Violations;
  readonly examples: string[];
  readonly timings: Record<string, Pct>;
  readonly env: Record<string, string | number>;
}

/**
 * The skip rule's allowance beyond its window before a hold counts as an overrun: one frame and a
 * little callback lateness. The real scheduler paints a held frame in the last frame callback before
 * its deadline, so it never needs it; a hold also counts as late only once two animation frames have
 * passed since the deadline, so a stalled page cannot fake an overrun.
 */
export const HOLD_SLACK_MS = Math.ceil(FRAME_MS) + 3;

const UTTERANCES = ['is rainier out tomorrow at 4', 'point reyes at sunset friday', 'grand canyon golden hour', 'rainier at 9 pm', 'what about midnight', 'yosemite blue hour saturday morning'];

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));
const keyStr = (k: PaintKey): string => `${k.gen}|${k.scene}|${k.tier}`;

/** The colour a synthetic frame is filled with, from its key (checked on screen by the audit). */
export function colourOf(k: PaintKey): [number, number, number] {
  let h = 0x811c9dc5;
  for (const ch of keyStr(k)) h = Math.imul(h ^ ch.charCodeAt(0), 0x01000193) >>> 0;
  return [40 + (h & 0x7f), 40 + ((h >>> 8) & 0x7f), 40 + ((h >>> 16) & 0x7f)];
}

function pct(xs: number[]): Pct {
  if (xs.length === 0) return { n: 0, p50: 0, p95: 0, p99: 0, max: 0 };
  const s = [...xs].sort((a, b) => a - b);
  const at = (q: number): number => s[Math.min(s.length - 1, Math.floor(q * (s.length - 1)))]!;
  const r = (v: number): number => Math.round(v * 1000) / 1000;
  return { n: s.length, p50: r(at(0.5)), p95: r(at(0.95)), p99: r(at(0.99)), max: r(s[s.length - 1]!) };
}

/** What the user should see, from delivered messages only. */
class Oracle {
  press = 0;
  dismissed = false;
  scene: string | null = null;
  shown: Tier | null = null;
  /** The press ended as No match or Out of range: none of its frames may stay on screen. */
  withdrawn = false;
  fresh(k: PaintKey): boolean {
    return k.gen === this.press && !this.dismissed && this.scene === k.scene && (this.shown === null || k.tier > this.shown);
  }
}

interface Samples {
  receiveMs: number[];
  msgToCommitMs: number[];
  readyToCommitMs: number[];
  checkToCommitMs: number[];
  heldMs: number[];
  fadeHalfMs: number[];
  previousMs: number[];
}

/** The destination chip's text for a destination list, as chips.ts writes it. */
const chipText = (providers: readonly string[]): string => (providers.length === 0 ? 'on-device' : `→ ${providers.join(', ')}`);

class Lane {
  readonly overlay: MountedOverlay;
  readonly box: HTMLElement;
  readonly oracle = new Oracle();
  readonly v: Violations = { stalePaints: 0, wrongDrops: 0, residue: 0, surfaceMismatches: 0, holdOverruns: 0, unjustifiedSkips: 0, unshownAcks: 0, missingAcks: 0, stuck: 0, protocolErrors: 0 };
  readonly c: Record<string, number> = {};
  readonly dropped: Record<string, number> = {};
  readonly examples: string[] = [];
  readonly t: Samples;
  readonly #core: Rng;
  readonly #webview: Rng;
  readonly #cycles: number;
  #gen = 0;
  #explicit = new Set<number>();
  #coreScene = new Map<number, string>();
  /** Scenes the core withdrew (No match or Out of range after a stable partial): their stages are cancelled. */
  #abandoned = new Set<string>();
  /** Presses whose stable partial already resolved a scene before key-up. */
  #early = new Set<number>();
  #outstanding = 0;
  #queue: string[] = [];
  #pumping = false;
  #port: MessagePort;
  #lastCommit: Extract<PaintEvent, { kind: 'commit' }> | null = null;
  /** Tier 0 frames held by the skip rule: our own deadline (ready + window, never the scheduler's claim) and frames seen past it. */
  #holds = new Map<string, { deadline: number; framesPast: number }>();
  #audited = new WeakMap<HTMLCanvasElement, string>();
  #residueSeen = new WeakMap<HTMLCanvasElement, string>();
  #destSeq = 0;
  /** The chip text each destination seq must show. */
  #destText = new Map<number, string>();
  /** What the last audit frame saw: `${gen}|${chip text}` while the panel showed a destination chip, else null. */
  #chipSeen: string | null = null;
  /** Acknowledgements received, as `${seq}|${gen}`. */
  #acks = new Set<string>();
  /** The (seq, gen) on screen and for how many audit frames in a row. */
  #ackWatch: { key: string | null; frames: number } = { key: null, frames: 0 };
  #utter = 0;

  constructor(parent: HTMLElement, index: number, o: StressOptions, samples: Samples) {
    const seed = BigInt(`0x${o.seed}`) + BigInt(index) * 0x9e3779b97f4a7c15n;
    this.#core = new Rng(seed);
    this.#webview = new Rng(seed ^ 0x5bd1e995n);
    this.#cycles = o.cyclesPerLane;
    this.t = samples;
    this.box = document.createElement('div');
    this.box.style.cssText = `width:${o.laneWidth ?? 240}px;display:inline-block;vertical-align:top;margin:0 2px 2px 0`;
    parent.appendChild(this.box);
    const ch = new MessageChannel();
    ch.port1.onmessage = () => this.#pump();
    this.#port = ch.port2;
    this.overlay = mountOverlay(this.box, {
      send: (m) => this.#outbound(m),
      loader: this.#loader,
      onEvent: (e) => this.#onEvent(e),
      keyTarget: this.box,
      ...variantOptions(o.variant),
    });
    this.#inc('lanes');
  }

  #inc(k: string, n = 1): void {
    this.c[k] = (this.c[k] ?? 0) + n;
  }

  #example(s: string): void {
    if (this.examples.length < 8) this.examples.push(s);
  }

  get done(): boolean {
    return this.#outstanding === 0 && this.#queue.length === 0 && (this.c.presses ?? 0) >= this.#cycles;
  }

  // -- the simulated core ------------------------------------------------------------------

  #cancelled(g: number): boolean {
    return this.#explicit.has(g) || g !== this.#gen;
  }

  #at(ms: number, fn: () => void): void {
    this.#outstanding += 1;
    setTimeout(() => {
      this.#outstanding -= 1;
      fn();
    }, ms);
  }

  #send(m: Record<string, unknown>): void {
    this.#queue.push(JSON.stringify({ v: 1, ...m }));
    this.#inc('messages');
    if (!this.#pumping) {
      this.#pumping = true;
      // Occasionally the transport stalls for a few ms (a busy IPC hop); order is always kept.
      if (this.#core.chance(0.05)) {
        this.#outstanding += 1;
        setTimeout(() => {
          this.#outstanding -= 1;
          this.#port.postMessage(0);
        }, this.#core.uniform(1, 8));
      } else this.#port.postMessage(0);
    }
  }

  /** Delivers one message per task, oracle first, then the overlay (the order of the transport). */
  #pump(): void {
    const json = this.#queue.shift();
    if (json === undefined) {
      this.#pumping = false;
      return;
    }
    this.#observe(JSON.parse(json) as Record<string, unknown>);
    const t0 = performance.now();
    const ok = this.overlay.receive(json);
    this.t.receiveMs.push(performance.now() - t0);
    if (!ok) this.v.protocolErrors += 1;
    if (this.#queue.length > 0) this.#port.postMessage(0);
    else this.#pumping = false;
  }

  start(): void {
    this.#send({ type: 'settings', on_device_only: false, cloud_speech: false, tier2: true, locale: this.#core.chance(0.5) ? 'en-US' : 'en-GB', units: this.#core.chance(0.5) ? 'metric' : 'us', reduce_motion: this.#core.chance(0.25) ? true : null, measuring: true });
    this.#destination();
    this.#keyDown();
  }

  #destination(): void {
    this.#destSeq += 1;
    const providers = this.#destSeq % 2 === 0 ? ['Deepgram'] : [];
    this.#destText.set(this.#destSeq, chipText(providers));
    this.#send({ type: 'destination', seq: this.#destSeq, destinations: providers.map((provider) => ({ provider, carries: ['audio'] })) });
  }

  #keyDown(): void {
    const rng = this.#core;
    this.#inc('presses');
    this.#gen += 1;
    const g = this.#gen;
    this.#send({ type: 'query_started', query_gen: g });
    // Three kinds of press: mashing (45 %), asking again soon (40 %), and dwelling on the answer (15 %).
    const kind = rng.float();
    const mashing = kind < 0.45;
    const dwell = kind >= 0.85;
    const hold = mashing ? rng.uniform(2, 20) : rng.uniform(20, 120);
    const partials = rng.range(0, 3);
    for (let i = 0; i < partials; i++) {
      this.#at((hold * (i + 1)) / (partials + 1), () => {
        if (this.#cancelled(g)) return;
        this.#send({ type: 'audio_level', query_gen: g, level: rng.float() });
        this.#send({ type: 'partial', query_gen: g, text: UTTERANCES[g % UTTERANCES.length]!.slice(0, 6 + i * 6), stable: i > 0 });
        // A stable partial naming a known place resolves a scene before key-up (PRD §7.2), and its
        // tiers start; the final transcript may still end the query another way.
        if (i > 0 && !this.#early.has(g) && rng.chance(0.4)) {
          this.#early.add(g);
          this.#inc('earlyParses');
          this.#parsed(g, sceneHashFromBigInt(rng.next()));
        }
      });
    }
    this.#at(hold, () => this.#keyUp(g));
    if ((this.c.presses ?? 0) < this.#cycles) this.#at(mashing ? rng.uniform(1, 30) : dwell ? rng.uniform(400, 1500) : rng.uniform(30, 260), () => this.#keyDown());
    if (rng.chance(0.08)) this.#at(rng.uniform(3, 300), () => this.#esc());
    if (rng.chance(0.01)) this.#at(rng.uniform(0, 50), () => this.#destination());
  }

  #esc(): void {
    this.#inc('escapes');
    const g = this.#gen;
    if (this.#core.chance(0.3)) {
      // Esc in the focused panel (PRD §5.4): a real keydown on the lane; the overlay tells the core.
      this.#inc('panelEscapes');
      this.oracle.dismissed = true;
      this.box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    } else {
      this.#explicit.add(g);
      this.#send({ type: 'cancelled', query_gen: g, reason: 'esc' });
    }
  }

  #keyUp(g: number): void {
    const rng = this.#core;
    if (this.#cancelled(g) && rng.chance(0.6)) return void this.#inc('cancelledStages');
    this.#send({ type: 'released', query_gen: g });
    this.#at(rng.heavyFloat(4, 30, 200), () => this.#sttFinal(g));
  }

  #sttFinal(g: number): void {
    const rng = this.#core;
    if (this.#cancelled(g) && rng.chance(0.6)) return void this.#inc('cancelledStages');
    const early = this.#early.has(g);
    const r = rng.float();
    // After a scene shown on a stable partial, the final transcript ends the query as No match or
    // Out of range far more often than usual, so the withdrawal path is well exercised.
    const noMatch = early ? 0.2 : 0.03;
    const outOfRange = early ? 0.4 : 0.05;
    if (r < noMatch) {
      this.#inc(early ? 'noMatchAfterScene' : 'noMatch');
      this.#withdraw(g);
      this.#send({ type: 'no_match', query_gen: g, transcript: 'golden gate at 9', closest: [{ place_id: 'yosemite', place: 'Yosemite', score: 0.36 }] });
      return;
    }
    if (r < outOfRange) {
      this.#inc(early ? 'outOfRangeAfterScene' : 'outOfRange');
      this.#withdraw(g);
      this.#send({ type: 'out_of_range', query_gen: g, reason: 'past', place_id: 'rainier', viewpoint_id: 'rainier.paradise-mountain', tz: 'America/Los_Angeles', requested: '2026-11-18T16:00:00-08:00', latest_valid: '2026-11-26T08:00:00-08:00', display: DISPLAY.rainier });
      // Half the time the user takes the offer: a real click on the panel's button, a little later.
      if (rng.chance(0.5)) this.#at(rng.uniform(20, 200), () => this.#clickOffer(g));
      return;
    }
    if (early && rng.chance(0.7)) {
      // The final transcript agrees with the stable partial: the same scene again, its tiers running.
      const scene = this.#coreScene.get(g);
      if (scene !== undefined) {
        this.#inc('finalSameScene');
        this.#send({ type: 'parsed', query_gen: g, scene_hash: scene, ...this.#query() });
        return;
      }
    }
    if (r < outOfRange + 0.01) {
      this.#inc('noSpeech');
      this.#explicit.add(g);
      this.#send({ type: 'cancelled', query_gen: g, reason: 'no_speech' });
      return;
    }
    if (r < outOfRange + 0.015) {
      this.#inc('fatalErrors');
      this.#explicit.add(g);
      this.#send({ type: 'error', query_gen: g, code: 'render_failed', fatal: true });
      return;
    }
    if (early) this.#withdraw(g); // the final transcript names another scene: the early one's stages are cancelled
    const scene = sceneHashFromBigInt(rng.next());
    this.#at(rng.uniform(0.1, 2), () => this.#parsed(g, scene));
  }

  /** The core gives up the press's current scene: its stages are cancelled (60 % drop, the rest late). */
  #withdraw(g: number): void {
    const scene = this.#coreScene.get(g);
    if (scene !== undefined) this.#abandoned.add(`${g}|${scene}`);
    this.#coreScene.delete(g);
  }

  /** Clicks the Out of range offer's button, if the panel shows it (the user took the offer). */
  #clickOffer(g: number): void {
    if (g !== this.#gen || this.#explicit.has(g)) return;
    const button = this.box.querySelector<HTMLElement>('[data-action="use_offer"]');
    if (button === null || button.offsetParent === null) return;
    this.#inc('offersClicked');
    button.click();
  }

  /** The core's answer to use_offer: the query at the latest valid time, as a new scene with its tiers. */
  #answerOffer(g: number): void {
    const rng = this.#core;
    if (g !== this.#gen || this.#explicit.has(g)) return;
    const scene = sceneHashFromBigInt(rng.next());
    this.#coreScene.set(g, scene);
    this.#inc('offersAnswered');
    this.#send({ type: 'scene_changed', query_gen: g, scene_hash: scene, cause: 'use_offer', ...this.#query() });
    this.#at(rng.uniform(0.5, 8), () => this.#stage(g, scene, 0));
    this.#at(rng.uniform(2, 60), () => this.#stage(g, scene, 1));
    if (rng.chance(0.5)) this.#at(rng.heavyFloat(60, 300, 1200), () => this.#stage(g, scene, 2));
  }

  #query(): Record<string, unknown> {
    this.#utter += 1;
    const q = coreQuery(UTTERANCES[this.#utter % UTTERANCES.length]!);
    return { query: q, display: DISPLAY[q.place_id] };
  }

  #parsed(g: number, scene: string): void {
    const rng = this.#core;
    this.#coreScene.set(g, scene);
    this.#send({ type: 'parsed', query_gen: g, scene_hash: scene, ...this.#query() });
    this.#at(rng.uniform(0.5, 12), () => this.#stage(g, scene, 0));
    // Tier 1: usually within 60 ms, 12 % late (up to 250 ms); announced 70 % of the time; 2 % rejected
    // after the announcement, and 5 % silently lost (a renderer that died), so a hold has to time out.
    const t1 = rng.chance(0.12) ? rng.uniform(60, 250) : rng.uniform(2, 60);
    if (rng.chance(0.7)) this.#send({ type: 'tier_started', query_gen: g, scene_hash: scene, tier: 1, eta_ms: t1 * rng.uniform(0.6, 1.4) });
    const fate = rng.float();
    if (fate < 0.02) this.#at(t1, () => this.#reject(g, scene, 1));
    else if (fate < 0.07) this.#inc('tier1Lost');
    else this.#at(t1, () => this.#stage(g, scene, 1));
    if (rng.chance(0.8)) {
      const t2 = rng.heavyFloat(60, 300, 1200);
      if (rng.chance(0.15)) this.#at(t2, () => this.#reject(g, scene, 2));
      else this.#at(t2, () => this.#stage(g, scene, 2));
    }
    if (rng.chance(0.35)) this.#at(rng.uniform(5, 250), () => this.#scrub(g));
  }

  #scrub(g: number): void {
    const rng = this.#core;
    if (g !== this.#gen || this.#explicit.has(g) || !this.#coreScene.has(g)) return; // only the query on screen
    this.#inc('scrubs');
    const scene = sceneHashFromBigInt(rng.next());
    this.#coreScene.set(g, scene);
    this.#send({ type: 'scene_changed', query_gen: g, scene_hash: scene, cause: rng.chance(0.2) ? 'meridiem_flip' : 'scrub', ...this.#query() });
    this.#at(rng.uniform(0.2, 5), () => this.#stage(g, scene, 0));
    this.#at(rng.uniform(0.5, 12), () => this.#stage(g, scene, 1));
    if (rng.chance(0.3)) this.#at(rng.uniform(40, 300), () => this.#stage(g, scene, 2));
    if (rng.chance(0.4)) this.#at(rng.uniform(2, 100), () => this.#scrub(g));
  }

  #reject(g: number, scene: string, tier: Tier): void {
    if (this.#cancelled(g) && this.#core.chance(0.6)) return void this.#inc('cancelledStages');
    this.#inc(`rejectedTier${tier}`);
    this.#send({ type: 'tier_rejected', query_gen: g, scene_hash: scene, tier, reason: tier === 2 ? 'structure' : 'disabled' });
  }

  #stage(g: number, scene: string, tier: Tier): void {
    if (this.#cancelled(g) || this.#abandoned.has(`${g}|${scene}`)) {
      if (this.#core.chance(0.6)) return void this.#inc('cancelledStages');
      this.#inc('lateResults');
    }
    const label = tier === 0 ? photoLabel() : tier === 1 ? simulatedLabel() : refinedLabel();
    const frame = { url: syntheticUrl(`${g}/${scene}/${tier}`), width: 1920, height: 1080 };
    this.#inc('framesSent');
    this.#send({ type: 'tier_frame_ready', query_gen: g, scene_hash: scene, tier, frame, label });
    // 3 % of frames are sent again a little later (a re-render), which the tier rule must refuse.
    if (this.#core.chance(0.03)) {
      this.#inc('duplicatesSent');
      this.#at(this.#core.uniform(5, 80), () => this.#send({ type: 'tier_frame_ready', query_gen: g, scene_hash: scene, tier, frame, label }));
    }
  }

  #outbound(m: Outbound): void {
    switch (m.type) {
      case 'dismiss':
        // The panel's Esc reaches the core, which cancels that query's work.
        if (m.query_gen !== null) this.#explicit.add(m.query_gen);
        break;
      case 'destination_shown': {
        // The chip must have shown this destination, in this key press, the frame before (PRD §15.1).
        this.#inc('acks');
        this.#acks.add(`${m.seq}|${m.query_gen}`);
        const want = `${m.query_gen}|${this.#destText.get(m.seq) ?? '?'}`;
        if (this.#chipSeen !== want || this.#chipNow() !== want) {
          this.v.unshownAcks += 1;
          this.#example(`destination_shown seq ${m.seq} gen ${m.query_gen} while the panel showed ${this.#chipNow() ?? 'nothing'} (last frame ${this.#chipSeen ?? 'nothing'})`);
        }
        break;
      }
      case 'use_offer':
        this.#at(this.#core.uniform(1, 5), () => this.#answerOffer(m.query_gen));
        break;
      case 'protocol_error':
        this.v.protocolErrors += 1;
        this.#example(`protocol_error ${m.code} ${m.message_type}: ${m.detail}`);
        break;
      default:
        break;
    }
  }

  // -- the webview side's injected delays ---------------------------------------------------

  readonly #loader: FrameLoader = async (msg, signal) => {
    const rng = this.#webview;
    const delay = rng.heavyFloat(0, 14, 90);
    const fail = rng.chance(0.01);
    await sleep(delay);
    if (signal.aborted) throw new Error('aborted');
    if (fail) throw new Error('injected decode failure');
    const [r, g, b] = colourOf({ gen: msg.query_gen, scene: msg.scene_hash, tier: msg.tier });
    const w = 32;
    const h = 18;
    const px = new Uint8ClampedArray(w * h * 4);
    for (let i = 0; i < w * h; i++) px.set([r, g, b, 255], i * 4);
    const bmp = await createImageBitmap(new ImageData(px, w, h));
    if (signal.aborted) {
      bmp.close();
      throw new Error('aborted');
    }
    return { source: bmp, width: w, height: h, release: () => bmp.close() };
  };

  // -- the oracle and the checks -----------------------------------------------------------

  #observe(m: Record<string, unknown>): void {
    const o = this.oracle;
    const g = m.query_gen as number;
    switch (m.type) {
      case 'query_started':
        if (g > o.press) {
          o.press = g;
          o.dismissed = false;
          o.scene = null;
          o.shown = null;
        }
        break;
      case 'cancelled':
        if (g === o.press) o.dismissed = true;
        break;
      case 'error':
        if (m.fatal === true && (g === o.press || m.query_gen === null)) o.dismissed = true;
        break;
      case 'no_match':
      case 'out_of_range':
        // The press ended without a scene: whatever a stable partial had shown is withdrawn.
        if (g === o.press && !o.dismissed) {
          o.scene = null;
          o.shown = null;
          o.withdrawn = true;
        }
        break;
      case 'parsed':
      case 'scene_changed':
        if (g === o.press && !o.dismissed) {
          if (o.scene !== m.scene_hash) {
            o.scene = m.scene_hash as string;
            o.shown = null;
          }
          o.withdrawn = false; // an answer (the offer taken) gives the press a scene again
        }
        break;
      default:
        break;
    }
  }

  /** What the panel shows now: `${gen}|${destination chip text}`, or null while it shows no chip. */
  #chipNow(): string | null {
    const root = this.overlay.view.el;
    if (root.getAttribute('data-phase') === 'idle') return null;
    const chip = root.querySelector('.sg-chips [data-chip="destination"]');
    return chip === null ? null : `${this.overlay.controller.model.gen}|${chip.textContent ?? ''}`;
  }

  #onEvent(e: PaintEvent): void {
    const o = this.oracle;
    switch (e.kind) {
      case 'commit': {
        this.#inc('commits');
        this.#inc(`commitsTier${e.key.tier}`);
        if (e.faded) this.#inc('fades');
        if (o.fresh(e.key)) o.shown = e.key.tier;
        else {
          this.v.stalePaints += 1;
          this.#example(`stale paint ${keyStr(e.key)}: oracle press ${o.press} dismissed ${o.dismissed} scene ${o.scene} shown ${o.shown}`);
        }
        this.t.msgToCommitMs.push(e.tMs - e.receivedMs);
        this.t.readyToCommitMs.push(e.tMs - e.preparedMs);
        this.t.checkToCommitMs.push(e.checkToCommitMs);
        if (e.previousMs !== null) this.t.previousMs.push(e.previousMs);
        const held = this.#holds.get(keyStr(e.key));
        if (held !== undefined) {
          this.#inc('heldThenPainted');
          this.t.heldMs.push(e.tMs - e.preparedMs);
          if (this.#late(held, e.tMs)) this.#overrun(`held ${(e.tMs - e.preparedMs).toFixed(1)} ms then painted`);
        }
        this.#holds.delete(keyStr(e.key));
        this.#lastCommit = e;
        break;
      }
      case 'drop':
        this.dropped[`${e.reason}@${e.at}`] = (this.dropped[`${e.reason}@${e.at}`] ?? 0) + 1;
        if (o.fresh(e.key)) {
          this.v.wrongDrops += 1;
          this.#example(`fresh frame dropped ${keyStr(e.key)} (${e.reason} at ${e.at})`);
        }
        this.#endHold(e.key, e.tMs, 'dropped');
        break;
      case 'reset':
        this.#inc('resetDrops');
        if (o.fresh(e.key)) {
          this.v.wrongDrops += 1;
          this.#example(`fresh frame dropped by reset ${keyStr(e.key)}`);
        }
        this.#endHold(e.key, e.tMs, 'reset');
        break;
      case 'skip': {
        this.#inc('skips');
        const c = this.#lastCommit;
        const justified = c !== null && c.tMs === e.tMs && c.key.gen === e.key.gen && c.key.scene === e.key.scene && c.key.tier === e.by && e.by > e.key.tier;
        if (!justified) {
          this.v.unjustifiedSkips += 1;
          this.#example(`unjustified skip ${keyStr(e.key)}`);
        }
        const held = this.#holds.get(keyStr(e.key));
        if (held !== undefined) {
          this.#inc('heldThenSkipped');
          this.t.heldMs.push(e.tMs - e.preparedMs);
          if (this.#late(held, e.tMs)) this.#overrun(`held ${(e.tMs - e.preparedMs).toFixed(1)} ms then skipped`);
          this.#holds.delete(keyStr(e.key));
        }
        break;
      }
      case 'hold':
        this.#inc('holds');
        this.#holds.set(keyStr(e.key), { deadline: e.preparedMs + SKIP_WINDOW_MS, framesPast: 0 });
        break;
      case 'half':
        if (e.frameMs !== e.commitFrameMs) this.t.fadeHalfMs.push(e.frameMs - e.commitFrameMs);
        break;
      case 'cut':
        this.#inc('fadesCut');
        break;
      case 'fail':
        this.#inc('decodeFailures');
        this.#endHold(e.key, e.tMs, 'failed');
        break;
    }
  }

  /**
   * D-012 row 11 allows a Tier 0 to wait at most the window after it was ready. Allowing for a late
   * timer and frame, a hold is late once it outlives the deadline by more than HOLD_SLACK_MS and
   * three animation frames have passed since the deadline (so a slow page cannot fake an overrun).
   */
  #late(h: { deadline: number; framesPast: number }, t: number): boolean {
    return t > h.deadline + HOLD_SLACK_MS && h.framesPast >= 2;
  }

  /** A held frame left the paint path some other way: it still must not have outlived its window. */
  #endHold(key: PaintKey, t: number, how: string): void {
    const held = this.#holds.get(keyStr(key));
    if (held === undefined) return;
    this.#holds.delete(keyStr(key));
    if (this.#late(held, t)) this.#overrun(`held ${(t - held.deadline + SKIP_WINDOW_MS).toFixed(1)} ms, then ${how}`);
  }

  #overrun(what: string): void {
    this.v.holdOverruns += 1;
    this.#example(`hold overrun: ${what}`);
  }

  /** Checks what is on screen now (called once per animation frame). */
  audit(now: number): void {
    const o = this.oracle;
    // The destination chip as this frame shows it, and whether it has waited too long for its ack.
    this.#chipSeen = this.#chipNow();
    const model = this.overlay.controller.model;
    const ackKey = this.#chipSeen !== null && model.destinationSeq > 0 ? `${model.destinationSeq}|${model.gen}` : null;
    if (ackKey !== null && ackKey === this.#ackWatch.key) this.#ackWatch.frames += 1;
    else this.#ackWatch = { key: ackKey, frames: 1 };
    if (ackKey !== null && this.#ackWatch.frames === 5 && !this.#acks.has(ackKey)) {
      this.v.missingAcks += 1;
      this.#example(`destination ${ackKey} on screen for 5 frames with no destination_shown`);
    }
    for (const l of this.overlay.view.layers) {
      const id = keyStr(l.key);
      if ((l.key.gen !== o.press || o.dismissed || o.withdrawn) && this.#residueSeen.get(l.canvas) !== id) {
        this.#residueSeen.set(l.canvas, id);
        this.v.residue += 1;
        this.#example(`residue: ${id} on screen while press ${o.press} dismissed ${o.dismissed} withdrawn ${o.withdrawn}`);
      }
      if (this.#audited.get(l.canvas) === id) continue;
      this.#audited.set(l.canvas, id);
      this.#inc('layersAudited');
      const ctx = l.canvas.getContext('2d');
      if (!ctx) continue;
      const scale = l.canvas.width / Math.max(1, l.canvas.clientWidth || l.canvas.width);
      const strip = ctx.getImageData(0, 0, Math.ceil(CELLS * CELL_PX * scale), Math.ceil(CELL_PX * scale));
      const tag = readTag(strip.data, strip.width, scale);
      const centre = ctx.getImageData(l.canvas.width >> 1, l.canvas.height >> 1, 1, 1).data;
      const [r, g, b] = colourOf(l.key);
      const tagOk = typeof tag !== 'string' && tag.gen === (l.key.gen & 0x0fff) && tag.tier === l.key.tier;
      const pixelsOk = Math.abs(centre[0]! - r) <= 2 && Math.abs(centre[1]! - g) <= 2 && Math.abs(centre[2]! - b) <= 2;
      if (!tagOk || !pixelsOk) {
        this.v.surfaceMismatches += 1;
        this.#example(`surface mismatch on ${id}: tag ${JSON.stringify(tag)}, centre ${[...centre].slice(0, 3)} want ${[r, g, b]}`);
      }
    }
    for (const [k, h] of this.#holds) {
      if (now > h.deadline) h.framesPast += 1;
      if (this.#late(h, now) && now > h.deadline + 4 * HOLD_SLACK_MS) {
        this.#holds.delete(k);
        this.#overrun(`still held ${(now - h.deadline + SKIP_WINDOW_MS).toFixed(0)} ms after it was ready`);
      }
    }
  }

  /** The last checks once everything has drained. */
  finish(): void {
    const pending = this.overlay.controller.scheduler.pending;
    if (pending > 0) {
      this.v.stuck += pending;
      this.#example(`${pending} frames still waiting after the drain`);
    }
    this.v.holdOverruns += this.#holds.size;
    this.#inc('destinationsSent', this.#destSeq);
  }
}

/** Runs the stress test in this page and returns the report. */
export async function runStress(o: StressOptions, parent: HTMLElement = document.body): Promise<StressReport> {
  const samples: Samples = { receiveMs: [], msgToCommitMs: [], readyToCommitMs: [], checkToCommitMs: [], heldMs: [], fadeHalfMs: [], previousMs: [] };
  const frames: number[] = [];
  const lanes: Lane[] = [];
  for (let i = 0; i < o.lanes; i++) lanes.push(new Lane(parent, i, o, samples));
  let running = true;
  let last = -1;
  const loop = (t: number): void => {
    if (last >= 0) frames.push(t - last);
    last = t;
    for (const lane of lanes) lane.audit(performance.now());
    if (running) requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
  const t0 = performance.now();
  for (const lane of lanes) lane.start();
  while (!lanes.every((l) => l.done)) await sleep(50);
  const wallMs = performance.now() - t0;
  // Drain: decodes and paints still in flight finish; then anything left waiting is stuck.
  const drainEnd = performance.now() + (o.drainMs ?? 5000);
  await sleep(1500);
  while (performance.now() < drainEnd && lanes.some((l) => l.overlay.controller.scheduler.pending > 0)) await sleep(50);
  running = false;
  await new Promise((r) => requestAnimationFrame(r));
  for (const lane of lanes) {
    lane.audit(performance.now());
    lane.finish();
  }
  const sum = <T extends Record<string, number>>(get: (l: Lane) => T): Record<string, number> => {
    const out: Record<string, number> = {};
    for (const l of lanes) for (const [k, v] of Object.entries(get(l))) out[k] = (out[k] ?? 0) + v;
    return out;
  };
  const violations = sum((l) => l.v as unknown as Record<string, number>) as unknown as Violations;
  const counts = sum((l) => l.c);
  return {
    variant: o.variant,
    lanes: o.lanes,
    cycles: counts.presses ?? 0,
    wallMs: Math.round(wallMs),
    counts,
    dropped: sum((l) => l.dropped),
    violations,
    examples: lanes.flatMap((l) => l.examples).slice(0, 12),
    timings: {
      receiveMs: pct(samples.receiveMs),
      msgToCommitMs: pct(samples.msgToCommitMs),
      readyToCommitMs: pct(samples.readyToCommitMs),
      checkToCommitMs: pct(samples.checkToCommitMs),
      heldMs: pct(samples.heldMs),
      fadeHalfMs: pct(samples.fadeHalfMs),
      previousMs: pct(samples.previousMs),
      frameIntervalMs: pct(frames),
    },
    env: { userAgent: navigator.userAgent, dpr: devicePixelRatio, cores: navigator.hardwareConcurrency, crossOriginIsolated: String(globalThis.crossOriginIsolated === true) },
  };
}
