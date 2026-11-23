/**
 * Test doubles for running the overlay without a browser (unit tests and the Node key-mash checks):
 * a simulated clock with 60 Hz animation frames, a surface that records what it draws, and loaders.
 * None of these are used by the app; the browser stress run (harness/stress.ts) uses the real DOM
 * view, the browser's clock and real image decoding instead.
 */
import type { CommitRequest, FadeHandle, FrameClock, FrameLoader, Prepared, Surface } from './scheduler.ts';
import type { Caption } from './caption.ts';
import type { SurfaceSettings } from './controller.ts';
import type { PaintKey } from './gate.ts';

/** A simulated clock: timers and 60 Hz frame callbacks run only when the test advances time. */
export class FakeClock implements FrameClock {
  #now = 0;
  #seq = 0;
  #timers = new Map<number, { at: number; cb: () => void; seq: number }>();
  #frames = new Map<number, (t: number) => void>();
  readonly frameMs: number;

  constructor(frameMs = 1000 / 60) {
    this.frameMs = frameMs;
  }

  now(): number {
    return this.#now;
  }

  requestFrame(cb: (t: number) => void): number {
    this.#seq += 1;
    this.#frames.set(this.#seq, cb);
    return this.#seq;
  }

  cancelFrame(id: number): void {
    this.#frames.delete(id);
  }

  setTimer(cb: () => void, ms: number): number {
    this.#seq += 1;
    this.#timers.set(this.#seq, { at: this.#now + Math.max(0, ms), cb, seq: this.#seq });
    return this.#seq;
  }

  clearTimer(id: number): void {
    this.#timers.delete(id);
  }

  /** Frame callbacks waiting for the next frame. */
  get framesPending(): number {
    return this.#frames.size;
  }

  #nextFrameAt(): number {
    return (Math.floor(this.#now / this.frameMs + 1e-9) + 1) * this.frameMs;
  }

  /** Runs timers and frames in time order until `t`. Microtasks run between steps when awaited via `advanceAsync`. */
  advance(ms: number): void {
    const end = this.#now + ms;
    for (;;) {
      const timer = [...this.#timers.values()].sort((a, b) => a.at - b.at || a.seq - b.seq)[0];
      const frameAt = this.#frames.size > 0 ? this.#nextFrameAt() : Infinity;
      const next = Math.min(timer?.at ?? Infinity, frameAt);
      if (next > end) break;
      this.#now = next;
      if (timer && timer.at <= frameAt) {
        this.#timers.delete(timer.seq);
        timer.cb();
      } else {
        const cbs = [...this.#frames.values()];
        this.#frames.clear();
        for (const cb of cbs) cb(this.#now);
      }
    }
    this.#now = end;
  }

  /** Like `advance`, but lets promise callbacks (loaders) settle between steps. */
  async advanceAsync(ms: number, step = 1): Promise<void> {
    const end = this.#now + ms;
    while (this.#now < end - 1e-9) {
      await flush();
      this.advance(Math.min(step, end - this.#now));
    }
    await flush();
  }
}

/** Lets pending promise callbacks run. */
export async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

/** Pixels that need no canvas. */
export function fakePrepared(width = 16, height = 9): Prepared & { released: boolean } {
  const p = {
    source: null,
    width,
    height,
    released: false,
    release() {
      p.released = true;
    },
  };
  return p;
}

/** A loader that resolves on the next microtask. */
export const instantLoader: FrameLoader = async () => fakePrepared();

/** A loader whose delay per frame the test controls (resolved through `settle`). */
export class ManualLoader {
  readonly waiting: { key: PaintKey; resolve: (p: Prepared) => void; reject: (e: Error) => void; signal: AbortSignal }[] = [];
  readonly loader: FrameLoader = (msg, signal) =>
    new Promise<Prepared>((resolve, reject) => {
      this.waiting.push({ key: { gen: msg.query_gen, scene: msg.scene_hash, tier: msg.tier }, resolve, reject, signal });
    });
  /** Resolves the oldest waiting load for `tier` (or the oldest of any tier). */
  settle(tier?: number): PaintKey | null {
    const i = this.waiting.findIndex((w) => tier === undefined || w.key.tier === tier);
    if (i < 0) return null;
    const [w] = this.waiting.splice(i, 1);
    w!.resolve(fakePrepared());
    return w!.key;
  }
  fail(tier?: number): void {
    const i = this.waiting.findIndex((w) => tier === undefined || w.key.tier === tier);
    if (i >= 0) this.waiting.splice(i, 1)[0]!.reject(new Error('decode failed'));
  }
}

/** One thing the recording surface saw. */
export type SurfaceLog =
  | { readonly op: 'commit'; readonly key: PaintKey; readonly caption: Caption; readonly faded: boolean; readonly frameMs: number }
  | { readonly op: 'clear' };

/**
 * A surface that records commits and clears, and keeps layers as the DOM view does: a cross-fade
 * lasts `fadeMs` on the fake clock; a frame drawn without a fade replaces every layer below it, and
 * a fade cut that way (or by `clear`) reports itself cancelled.
 */
export class RecordingSurface implements Surface, SurfaceSettings {
  readonly log: SurfaceLog[] = [];
  /** What is on screen now, bottom to top. */
  layers: { key: PaintKey; caption: Caption; cancelled: boolean }[] = [];
  reduceMotion: boolean | null = null;
  measuring = false;
  readonly #clock: FrameClock;
  readonly #fadeMs: number;

  constructor(clock: FrameClock, fadeMs = 120) {
    this.#clock = clock;
    this.#fadeMs = fadeMs;
  }

  configure(s: { reduceMotion: boolean | null; measuring: boolean }): void {
    this.reduceMotion = s.reduceMotion;
    this.measuring = s.measuring;
  }

  commit(req: CommitRequest): { faded: boolean; fade: FadeHandle | null } {
    const top = this.layers[this.layers.length - 1];
    const faded = req.key.tier > 0 && top !== undefined && top.key.gen === req.key.gen && top.key.scene === req.key.scene && this.reduceMotion !== true;
    const layer = { key: req.key, caption: req.caption, cancelled: false };
    if (faded) this.layers = [...this.layers, layer];
    else {
      for (const l of this.layers) l.cancelled = true;
      this.layers = [layer];
    }
    this.log.push({ op: 'commit', key: req.key, caption: req.caption, faded, frameMs: req.frameMs });
    if (!faded) return { faded, fade: null };
    const start = this.#clock.now();
    const fadeMs = this.#fadeMs;
    const clock = this.#clock;
    return {
      faded,
      fade: {
        progress: () => (layer.cancelled ? 0 : Math.min(1, (clock.now() - start) / fadeMs)),
        cancelled: () => layer.cancelled && clock.now() - start < fadeMs,
      },
    };
  }

  clear(): void {
    for (const l of this.layers) l.cancelled = true;
    this.layers = [];
    this.log.push({ op: 'clear' });
  }

  burnIn(caption: Caption): string | null {
    return this.layers.length > 0 ? Buffer.from(`burned:${caption.line1}`).toString('base64') : null;
  }

  /** Keys committed so far. */
  commits(): PaintKey[] {
    return this.log.flatMap((l) => (l.op === 'commit' ? [l.key] : []));
  }
}
