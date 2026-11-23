/**
 * The TypeScript mirror of app/core/tests/keymash.rs (PRD §10.3: a randomised key-mash stress test,
 * >= 10,000 cycles with injected stage delays, must show zero stale paints).
 *
 * It is the Rust discrete-event simulation ported line for line: the same SplitMix64 stream, the same
 * event order (time, then insertion), the same stage delays on a simulated microsecond clock, the
 * same 60 % chance that a cancelled stage drops its work, and the same oracle. The only thing it
 * swaps is the gate: it drives the webview's `PaintGate` (overlay/gate.ts). Because the streams are
 * identical, the counts it reports for the Rust seeds must equal the Rust test's counts exactly; that
 * is the evidence that the webview gate decides as the core's gate does (keymash.test.ts pins them).
 *
 * The browser stress run (stress.ts) is the other half: the real overlay on the browser's real event
 * loop, where messages, decodes, animation frames and fades interleave for real.
 */
import { PaintGate, sceneHashFromBigInt, type Gate as OverlayGate, type PaintDecision, type PaintKey, type StaleReason, type Tier } from '../overlay/gate.ts';
import { EventHeap } from './heap.ts';
import { Rng } from './rng.ts';

/** Rust test constants. */
export const VSYNC_US = 16_667;
export const CYCLES_PER_SEED = 10_000;
export const SEEDS: readonly bigint[] = [0x00c0ffeen, 0x5eed0002n, 0xbadc0de5n, 0x0ddba115n];

/** The core's generation counter and cancellation tokens (lifecycle.rs `GenCounter`, `CancelToken`). */
export class GenCounter {
  current = 0;
  begin(): CancelToken {
    this.current += 1;
    return new CancelToken(this.current, this);
  }
}

/** Cancellation for the work of one generation. */
export class CancelToken {
  #explicit = false;
  readonly generation: number;
  readonly #counter: GenCounter;
  constructor(generation: number, counter: GenCounter) {
    this.generation = generation;
    this.#counter = counter;
  }
  /** True once a newer generation began, or after `cancel`. */
  isCancelled(): boolean {
    return this.#explicit || this.#counter.current !== this.generation;
  }
  cancel(): void {
    this.#explicit = true;
  }
}

/** The Rust test's `Gate` trait: what the simulation calls. */
export interface SimGate {
  begin(gen: number): void;
  setScene(gen: number, scene: string): void;
  /** Esc for the query whose token is `token`. */
  escape(token: CancelToken): void;
  tryPaint(key: PaintKey): PaintDecision;
}

/** The real webview gate, with Esc as lifecycle.rs `escape`: cancel the token and dismiss the gate. */
export function realGate(gate: OverlayGate = new PaintGate()): SimGate {
  return {
    begin: (g) => void gate.begin(g),
    setScene: (g, s) => void gate.setScene(g, s),
    escape: (token) => {
      token.cancel();
      gate.dismiss();
    },
    tryPaint: (k) => gate.tryPaint(k),
  };
}

/** Negative control: checks the generation only (no scene, no tier order, Esc ignored). */
export function generationOnlyGate(): SimGate {
  let current = 0;
  return {
    begin: (g) => {
      current = g;
    },
    setScene: () => {},
    escape: (token) => token.cancel(),
    tryPaint: (k) => (k.gen === current ? { accept: true } : { accept: false, reason: 'generation' }),
  };
}

/** Negative controls: the real gate with one rule broken (the Rust test's `Fault`). */
export type Fault = 'esc-cancels-token-only' | 'no-tier-order' | 'no-scene-check';

export const FAULTS: readonly Fault[] = ['esc-cancels-token-only', 'no-tier-order', 'no-scene-check'];

/** The real gate with `fault` (the Rust test's `FaultyGate`). */
export function faultyGate(fault: Fault): SimGate {
  const inner = new PaintGate();
  return {
    begin: (g) => void inner.begin(g),
    setScene: (g, s) => void inner.setScene(g, s),
    escape: (token) => {
      token.cancel();
      if (fault !== 'esc-cancels-token-only') inner.dismiss();
    },
    tryPaint: (k) => {
      const d = inner.tryPaint(k);
      if (!d.accept && ((d.reason === 'tier' && fault === 'no-tier-order') || (d.reason === 'scene' && fault === 'no-scene-check'))) {
        return { accept: true };
      }
      return d;
    },
  };
}

type Ev =
  | { k: 'KeyDown' }
  | { k: 'Esc' }
  | { k: 'KeyUp'; g: number }
  | { k: 'SttFinal'; g: number }
  | { k: 'Parsed'; g: number; scene: string }
  | { k: 'Scrub'; g: number }
  | { k: 'StageDone'; g: number; scene: string; tier: Tier }
  | { k: 'Composite'; g: number; scene: string; tier: Tier };

/** What one simulated run did (the Rust test's `Report`). */
export interface Report {
  presses: number;
  escapes: number;
  events: number;
  composites: number;
  accepted: number;
  stalePaints: number;
  wrongDrops: number;
  dropped: Record<StaleReason, number>;
  cancelledStages: number;
  lateResults: number;
  scrubs: number;
  generationsPainted: number;
  simSeconds: number;
}

/** What the user should be seeing, kept from the simulation's own events (never from the gate). */
class Oracle {
  press: number;
  dismissed = false;
  scene: string | null = null;
  shown: Tier | null = null;
  constructor(press: number) {
    this.press = press;
  }
  /** PRD §10.3 from the user's side: the latest key press, no Esc since, the scene selected now, a higher tier. */
  fresh(gen: number, scene: string, tier: Tier): boolean {
    return gen === this.press && !this.dismissed && this.scene === scene && (this.shown === null || tier > this.shown);
  }
}

/** The Rust `simulate`, line for line. */
export function simulate(seed: bigint, cycles: number, gate: SimGate): Report {
  const rng = new Rng(seed);
  const counter = new GenCounter();
  const tokens = new Map<number, CancelToken>();
  const queue = new EventHeap<Ev>();
  let oracle = new Oracle(0);
  const report: Report = {
    presses: 0,
    escapes: 0,
    events: 0,
    composites: 0,
    accepted: 0,
    stalePaints: 0,
    wrongDrops: 0,
    dropped: { generation: 0, dismissed: 0, scene: 0, tier: 0 },
    cancelledStages: 0,
    lateResults: 0,
    scrubs: 0,
    generationsPainted: 0,
    simSeconds: 0,
  };
  const painted = new Set<number>();
  let lastT = 0;
  const cancelled = (g: number): boolean => tokens.get(g)?.isCancelled() ?? false;
  queue.push(0, { k: 'KeyDown' });
  for (let item = queue.pop(); item !== undefined; item = queue.pop()) {
    const { t, ev } = item;
    report.events += 1;
    lastT = t;
    switch (ev.k) {
      case 'KeyDown': {
        report.presses += 1;
        const token = counter.begin();
        const g = token.generation;
        tokens.set(g, token);
        gate.begin(g);
        oracle = new Oracle(g);
        const mashing = rng.chance(0.5);
        const hold = mashing ? rng.range(20_000, 150_000) : rng.range(300_000, 2_000_000);
        queue.push(t + hold, { k: 'KeyUp', g });
        if (report.presses < cycles) {
          const gap = mashing ? rng.range(5_000, 250_000) : rng.range(250_000, 5_000_000);
          queue.push(t + gap, { k: 'KeyDown' });
        }
        if (rng.chance(0.05)) queue.push(t + rng.range(50_000, 3_000_000), { k: 'Esc' });
        break;
      }
      case 'Esc': {
        // Esc (PRD §5.2): the overlay goes idle until the next key press.
        report.escapes += 1;
        const token = tokens.get(oracle.press);
        if (token) gate.escape(token);
        oracle.dismissed = true;
        break;
      }
      case 'KeyUp': {
        if (cancelled(ev.g) && rng.chance(0.6)) {
          report.cancelledStages += 1;
          break;
        }
        queue.push(t + rng.heavy(60_000, 450_000, 3_000_000), { k: 'SttFinal', g: ev.g });
        break;
      }
      case 'SttFinal': {
        if (cancelled(ev.g) && rng.chance(0.6)) {
          report.cancelledStages += 1;
          break;
        }
        const scene = sceneHashFromBigInt(rng.next());
        queue.push(t + rng.range(100, 5_000), { k: 'Parsed', g: ev.g, scene });
        break;
      }
      case 'Parsed': {
        const { g, scene } = ev;
        gate.setScene(g, scene);
        if (g === oracle.press && !oracle.dismissed && oracle.scene !== scene) {
          oracle.scene = scene;
          oracle.shown = null;
        }
        queue.push(t + rng.range(2_000, 90_000), { k: 'StageDone', g, scene, tier: 0 });
        queue.push(t + rng.heavy(10_000, 400_000, 1_500_000), { k: 'StageDone', g, scene, tier: 1 });
        if (rng.chance(0.85)) queue.push(t + rng.heavy(500_000, 3_000_000, 9_000_000), { k: 'StageDone', g, scene, tier: 2 });
        if (rng.chance(0.35)) queue.push(t + rng.range(30_000, 1_500_000), { k: 'Scrub', g });
        break;
      }
      case 'Scrub': {
        const { g } = ev;
        if (g !== oracle.press || oracle.dismissed || oracle.scene === null) break; // only the query on screen
        report.scrubs += 1;
        const scene = sceneHashFromBigInt(rng.next());
        gate.setScene(g, scene);
        oracle.scene = scene;
        oracle.shown = null;
        queue.push(t + rng.range(1_000, 30_000), { k: 'StageDone', g, scene, tier: 0 });
        queue.push(t + rng.range(3_000, 60_000), { k: 'StageDone', g, scene, tier: 1 });
        if (rng.chance(0.3)) queue.push(t + rng.range(400_000, 3_000_000), { k: 'StageDone', g, scene, tier: 2 });
        if (rng.chance(0.4)) queue.push(t + rng.range(10_000, 500_000), { k: 'Scrub', g });
        break;
      }
      case 'StageDone': {
        if (cancelled(ev.g)) {
          if (rng.chance(0.6)) {
            report.cancelledStages += 1;
            break;
          }
          report.lateResults += 1;
        }
        const nextVsync = (Math.floor(t / VSYNC_US) + 1 + rng.range(0, 3)) * VSYNC_US;
        queue.push(nextVsync, { k: 'Composite', g: ev.g, scene: ev.scene, tier: ev.tier });
        break;
      }
      case 'Composite': {
        report.composites += 1;
        const key: PaintKey = { gen: ev.g, scene: ev.scene, tier: ev.tier };
        const fresh = oracle.fresh(ev.g, ev.scene, ev.tier);
        const d = gate.tryPaint(key);
        if (d.accept) {
          report.accepted += 1;
          painted.add(ev.g);
          if (fresh) oracle.shown = ev.tier;
          else report.stalePaints += 1;
        } else {
          report.dropped[d.reason] += 1;
          if (fresh) report.wrongDrops += 1;
        }
        break;
      }
    }
  }
  report.generationsPainted = painted.size;
  report.simSeconds = lastT / 1e6;
  return report;
}
