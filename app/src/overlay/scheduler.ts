/**
 * The webview's paint path (PRD §10.3 correctness contract, §5.5 motion, D-012 row 11):
 *
 * 1. A tier frame arrives (`offer`). If the gate already refuses it, it is dropped there, before any
 *    decoding. Otherwise its pixels are prepared off the main path (the loader decodes them, or hands
 *    over pixels a `prefetch` decoded in the background, PRD §9.1).
 * 2. When pixels are ready, the scheduler asks for an animation frame.
 * 3. In that frame callback it drops every prepared frame the gate now refuses, picks the highest
 *    tier left for the current scene, and calls `gate.tryPaint` **immediately before** drawing it, in
 *    the same synchronous block: no message, decode or timer can run between the check and the
 *    draw, and the draw reaches the screen in this frame's composite.
 * 4. Tier 0 skip rule (D-012 row 11): if Tier 1 for the same scene is already prepared, or is decoding,
 *    or the core said it is due within the skip window (~100 ms) of Tier 0 being ready, Tier 0 is held
 *    and is skipped if Tier 1 comes in time. It still runs, and its times are reported. While a frame
 *    is held the scheduler takes every animation frame (no timer, so no timer lateness) and paints the
 *    held frame in the last frame callback that starts before its ready time plus the window. So the
 *    hold ends within one frame of the window; only a callback that itself runs late (a busy main
 *    thread) can push the draw a few ms past it. The stress run reports the measured maximum.
 * 5. Every frame's fate is reported with page timestamps: received, prepared, the frame callback's
 *    time, committed, and (for a cross-fade) the first frame at which the fade reached 50 %. A fade cut
 *    short (its layer replaced or cleared before 50 %) is reported without a 50 % time. The first frame
 *    of a scene after a scrub also says how long the previous scene's frame had stayed on screen
 *    (`previous_ms`). These are diagnostics: the measured paint is the composited frame (PRD §10.4).
 *
 * `reset` (a new key press, Esc, a fatal error, a withdrawn scene) aborts every decode, cancels the
 * pending frame, and clears the surface at once, fades included, so nothing of the old query stays
 * visible.
 */
import type { Caption } from './caption.ts';
import type { Gate, PaintDecision, PaintKey, SceneHash, StaleReason, Tier } from './gate.ts';
import type { PaintReport, TierFrameReady, TierStarted } from './protocol.ts';
import { PROTOCOL_VERSION } from './protocol.ts';

/** Time and frames, injectable so the same code runs on a browser and on a simulated clock. */
export interface FrameClock {
  /** Monotonic milliseconds (performance.now in a browser). */
  now(): number;
  /** Calls `cb` before the next paint with the frame's timestamp (requestAnimationFrame). */
  requestFrame(cb: (frameMs: number) => void): number;
  cancelFrame(id: number): void;
  setTimer(cb: () => void, ms: number): number;
  clearTimer(id: number): void;
}

/** The browser's clock: performance.now, requestAnimationFrame, setTimeout. */
export function browserClock(): FrameClock {
  return {
    now: () => performance.now(),
    requestFrame: (cb) => requestAnimationFrame(cb),
    cancelFrame: (id) => cancelAnimationFrame(id),
    setTimer: (cb, ms) => setTimeout(cb, ms) as unknown as number,
    clearTimer: (id) => clearTimeout(id),
  };
}

/** Decoded pixels, ready to draw. */
export interface Prepared {
  /** The image (null where there is no canvas, as in Node tests). */
  readonly source: CanvasImageSource | null;
  readonly width: number;
  readonly height: number;
  /** True if a `prefetch` had decoded these pixels before the frame was asked for; absent if unknown. */
  readonly prefetched?: boolean;
  /** Frees the pixels (ImageBitmap.close). Called once the frame is off screen or dropped. */
  release(): void;
}

/** Prepares a frame's pixels; must reject or resolve, and should stop early when `signal` aborts. */
export type FrameLoader = (msg: TierFrameReady, signal: AbortSignal) => Promise<Prepared>;

/** What the surface draws. */
export interface CommitRequest {
  readonly key: PaintKey;
  readonly msg: TierFrameReady;
  readonly prepared: Prepared;
  readonly caption: Caption;
  readonly frameMs: number;
}

/** A running cross-fade. */
export interface FadeHandle {
  /** 0 to 1; 1 once finished. */
  progress(): number;
  /** True once the fading layer was taken off screen (replaced or cleared) before the fade finished. */
  cancelled(): boolean;
}

/** Where frames are drawn (the DOM view in a browser, a recorder in tests). */
export interface Surface {
  /** Draws the frame now, synchronously, with its caption. Says whether it cross-fades in. */
  commit(req: CommitRequest): { readonly faded: boolean; readonly fade: FadeHandle | null };
  /** Removes every frame and caption now, stopping fades. */
  clear(): void;
}

/** What happened to a frame, for observers (the stress harness, tests). Times are `clock.now()`. */
export type PaintEvent =
  | { readonly kind: 'drop'; readonly key: PaintKey; readonly reason: StaleReason; readonly at: 'receipt' | 'frame' | 'scene_change'; readonly tMs: number }
  | { readonly kind: 'reset'; readonly key: PaintKey; readonly tMs: number }
  | { readonly kind: 'hold'; readonly key: PaintKey; readonly tMs: number; readonly preparedMs: number; readonly deadlineMs: number }
  | {
      readonly kind: 'commit';
      readonly key: PaintKey;
      readonly tMs: number;
      readonly frameMs: number;
      readonly receivedMs: number;
      readonly preparedMs: number;
      readonly heldMs: number;
      readonly faded: boolean;
      /** Time from the gate's check to the end of the draw (the "immediately before" gap). */
      readonly checkToCommitMs: number;
      /** Set on the first frame of a scene that replaced the previous scene's frame. */
      readonly previousMs: number | null;
    }
  | { readonly kind: 'skip'; readonly key: PaintKey; readonly by: Tier; readonly tMs: number; readonly preparedMs: number }
  | { readonly kind: 'half'; readonly key: PaintKey; readonly frameMs: number; readonly commitFrameMs: number }
  /** A cross-fade whose layer was taken off screen before it reached 50 %. */
  | { readonly kind: 'cut'; readonly key: PaintKey; readonly frameMs: number; readonly commitFrameMs: number }
  | { readonly kind: 'fail'; readonly key: PaintKey; readonly tMs: number };

/** The D-012 row 11 window: skip Tier 0 when Tier 1 is ready within about 100 ms. */
export const SKIP_WINDOW_MS = 100;

/** One frame at 60 Hz: a held Tier 0 paints in the last frame that starts before its deadline. */
export const FRAME_MS = 1000 / 60;

export interface SchedulerOptions {
  readonly gate: Gate;
  readonly clock: FrameClock;
  readonly loader: FrameLoader;
  readonly surface: Surface;
  /** Composes the caption for a frame at commit time (so it uses the settings current then). */
  readonly caption: (msg: TierFrameReady) => Caption;
  /** Called synchronously right after a frame is drawn. */
  readonly onPainted?: (key: PaintKey, caption: Caption) => void;
  /** Called when a frame's pixels could not be loaded (its tier will not paint). */
  readonly onFailed?: (key: PaintKey) => void;
  /** Every frame's report (sent to the core as `paint`). */
  readonly onReport?: (r: PaintReport) => void;
  readonly onEvent?: (e: PaintEvent) => void;
  readonly skipWindowMs?: number;
}

/** A frame in the paint path, from receipt to its outcome. */
export interface Entry {
  readonly key: PaintKey;
  readonly msg: TierFrameReady;
  readonly receivedMs: number;
  readonly abort: AbortController;
  state: 'loading' | 'ready';
  prepared: Prepared | null;
  preparedMs: number;
  heldSinceMs: number | null;
}

interface Fading {
  readonly key: PaintKey;
  readonly fade: FadeHandle;
  readonly report: PaintReport;
  readonly commitFrameMs: number;
}

/** The key of a `tier_started` hint. */
export const hintKey = (gen: number, scene: SceneHash, tier: Tier): string => `${gen}|${scene}|${tier}`;

/** The paint scheduler. One per overlay. */
export class PaintScheduler {
  protected readonly o: SchedulerOptions;
  protected readonly window: number;
  /** Frames received and not yet painted, skipped or dropped. */
  protected entries: Entry[] = [];
  /** When each (generation, scene, tier) is due, from `tier_started`, until that frame arrives. */
  protected readonly hints = new Map<string, number>();
  #frameId: number | null = null;
  #fading: Fading[] = [];
  /** The frame on screen (the last drawn), or null after a reset. */
  #shown: PaintKey | null = null;
  /** Since when the frame on screen has been of a scene that is no longer selected (a scrub), or null. */
  #previousSince: number | null = null;

  constructor(o: SchedulerOptions) {
    this.o = o;
    this.window = o.skipWindowMs ?? SKIP_WINDOW_MS;
  }

  /** Frames not yet painted or dropped (for tests). */
  get pending(): number {
    return this.entries.length;
  }

  /**
   * A tier frame arrived from the core.
   * TODO(M2): Tier 1 will render in this webview (PRD §6.1); its frames are to enter here the same way,
   * with a loader that renders instead of decoding.
   */
  offer(msg: TierFrameReady): void {
    const key: PaintKey = { gen: msg.query_gen, scene: msg.scene_hash, tier: msg.tier };
    const now = this.o.clock.now();
    // The announced frame has come: its hint no longer holds a Tier 0 (if it fails, Tier 0 goes).
    if (this.hints.delete(hintKey(key.gen, key.scene, key.tier))) this.requestFrame();
    if (!this.admit(key, now)) return;
    const entry: Entry = { key, msg, receivedMs: now, abort: new AbortController(), state: 'loading', prepared: null, preparedMs: 0, heldSinceMs: null };
    this.entries.push(entry);
    let promise: Promise<Prepared>;
    try {
      promise = this.o.loader(msg, entry.abort.signal);
    } catch {
      promise = Promise.reject(new Error('loader threw'));
    }
    promise.then(
      (p) => this.#onPrepared(entry, p),
      () => this.#onFailed(entry),
    );
  }

  /** The core says `tier` of this scene will not come (rejected): a Tier 0 held for it goes now. */
  withdraw(gen: number, scene: SceneHash, tier: Tier): void {
    if (this.hints.delete(hintKey(gen, scene, tier))) this.requestFrame();
  }

  /** The core says `tier` of this scene is due in `eta_ms` (feeds the skip rule). */
  expect(msg: TierStarted): void {
    if (msg.query_gen !== this.o.gate.state.gen) return;
    this.hints.set(hintKey(msg.query_gen, msg.scene_hash, msg.tier), this.o.clock.now() + msg.eta_ms);
    this.requestFrame(); // a held Tier 0 may now be released early, or a new hold may start
  }

  /**
   * Receipt-time check: drop a frame the gate already refuses. Safe because the core's messages
   * arrive in order, so a frame refused now (older generation, after Esc, another scene, a tier
   * already shown) cannot become the one to show; the authoritative check is still at paint time.
   */
  protected admit(key: PaintKey, now: number): boolean {
    const d = this.o.gate.check(key);
    if (d.accept) return true;
    this.emit({ kind: 'drop', key, reason: d.reason, at: 'receipt', tMs: now });
    this.report(key, 'dropped', d.reason, { received_ms: now });
    return false;
  }

  /** A new key press, Esc, a fatal error or a withdrawn scene: drop everything and clear the surface now. */
  reset(): void {
    const now = this.o.clock.now();
    for (const e of this.entries) {
      e.abort.abort();
      e.prepared?.release();
      this.emit({ kind: 'reset', key: e.key, tMs: now });
      this.report(e.key, 'dropped', 'reset', this.times(e));
    }
    this.entries = [];
    this.hints.clear();
    for (const f of this.#fading) {
      // Cleared mid-fade: drawn, but never at 50 % opacity, so the report carries no 50 % time.
      this.emit({ kind: 'cut', key: f.key, frameMs: now, commitFrameMs: f.commitFrameMs });
      this.o.onReport?.(f.report);
    }
    this.#fading = [];
    if (this.#frameId !== null) this.o.clock.cancelFrame(this.#frameId);
    this.#frameId = null;
    this.#shown = null;
    this.#previousSince = null;
    this.o.surface.clear();
  }

  /** True while a frame of (`gen`, `scene`) is still loading or waiting to paint. */
  waitingFor(gen: number, scene: SceneHash): boolean {
    return this.entries.some((e) => e.key.gen === gen && e.key.scene === scene);
  }

  /**
   * Takes the frame on screen off, fades included, keeping the frames on their way: nothing can
   * come for the selected scene, so the previous scene's frame must not stand in for it.
   */
  clearShown(): void {
    const now = this.o.clock.now();
    for (const f of this.#fading) {
      this.emit({ kind: 'cut', key: f.key, frameMs: now, commitFrameMs: f.commitFrameMs });
      this.o.onReport?.(f.report);
    }
    this.#fading = [];
    this.#shown = null;
    this.#previousSince = null;
    this.o.surface.clear();
  }

  /** The scene changed (scrub, flip, offer): drop frames of other scenes now, so their decodes stop. */
  sceneChanged(): void {
    const now = this.o.clock.now();
    this.entries = this.entries.filter((e) => {
      const d = this.o.gate.check(e.key);
      if (d.accept) return true;
      e.abort.abort();
      e.prepared?.release();
      this.emit({ kind: 'drop', key: e.key, reason: d.reason, at: 'scene_change', tMs: now });
      this.report(e.key, 'dropped', d.reason, this.times(e));
      return false;
    });
    // The frame on screen is now the previous scene's: from here it is visible-wrong time (PRD §10.3).
    if (this.#shown !== null && this.#shown.scene !== this.o.gate.state.scene && this.#previousSince === null) this.#previousSince = now;
    this.requestFrame();
  }

  #onPrepared(entry: Entry, p: Prepared): void {
    if (!this.entries.includes(entry)) {
      p.release(); // reset or dropped while decoding
      return;
    }
    entry.prepared = p;
    entry.preparedMs = this.o.clock.now();
    entry.state = 'ready';
    this.requestFrame();
  }

  #onFailed(entry: Entry): void {
    const i = this.entries.indexOf(entry);
    if (i < 0) return;
    this.entries.splice(i, 1);
    const now = this.o.clock.now();
    this.emit({ kind: 'fail', key: entry.key, tMs: now });
    this.report(entry.key, 'failed', 'load_failed', { received_ms: entry.receivedMs });
    this.o.onFailed?.(entry.key);
    this.requestFrame(); // a Tier 0 held for this frame may go now
  }

  protected requestFrame(): void {
    if (this.#frameId !== null) return;
    this.#frameId = this.o.clock.requestFrame((frameMs) => {
      this.#frameId = null;
      this.onFrame(frameMs);
    });
  }

  /** The frame callback: the paint point. */
  protected onFrame(frameMs: number): void {
    this.#trackFades(frameMs);
    const now = this.o.clock.now();
    // Drop what the gate now refuses (loading frames too, so their decodes stop).
    this.entries = this.entries.filter((e) => {
      const d = this.checkAtFrame(e.key);
      if (d.accept) return true;
      e.abort.abort();
      e.prepared?.release();
      this.emit({ kind: 'drop', key: e.key, reason: d.reason, at: 'frame', tMs: now });
      this.report(e.key, 'dropped', d.reason, this.times(e));
      return false;
    });
    const ready = this.entries.filter((e) => e.state === 'ready');
    let holding = false;
    if (ready.length > 0) {
      let best = ready[0]!;
      for (const e of ready) if (e.key.tier > best.key.tier) best = e;
      const deadline = this.holdDeadline(best, now);
      if (deadline !== null) {
        if (best.heldSinceMs === null) {
          best.heldSinceMs = now;
          this.emit({ kind: 'hold', key: best.key, tMs: now, preparedMs: best.preparedMs, deadlineMs: deadline });
        }
        holding = true;
      } else {
        this.paint(best, ready, frameMs);
      }
    }
    // A held frame is looked at again in every frame, so the last one before its deadline paints it.
    if (holding || this.#fading.length > 0) this.requestFrame();
  }

  /**
   * D-012 row 11: when to hold a ready Tier 0 instead of painting it. Returns the deadline, or null
   * to paint now. Held only while nothing is shown for the scene, only for Tier 1 of the same scene
   * that is decoding or announced as due within the window, and only while the next frame callback
   * would still start before the frame's ready time plus the window: the last callback before the
   * deadline paints it.
   */
  protected holdDeadline(best: Entry, now: number): number | null {
    if (best.key.tier !== 0 || this.window <= 0) return null;
    if (this.o.gate.state.shown !== null) return null;
    const deadline = best.preparedMs + this.window;
    if (now + FRAME_MS > deadline) return null;
    const { gen, scene } = best.key;
    const decoding = this.entries.some((e) => e.state === 'loading' && e.key.tier === 1 && e.key.gen === gen && e.key.scene === scene);
    const due = this.hints.get(hintKey(gen, scene, 1));
    return decoding || (due !== undefined && due <= deadline) ? deadline : null;
  }

  /** The gate's check for frames waiting in a frame callback. */
  protected checkAtFrame(key: PaintKey): PaintDecision {
    return this.o.gate.check(key);
  }

  /** The gate's check-and-record immediately before the draw. */
  protected acceptAtPaint(key: PaintKey): PaintDecision {
    return this.o.gate.tryPaint(key);
  }

  /** Checks the gate and draws `best` in one synchronous step; the rest of `ready` below it are skipped. */
  protected paint(best: Entry, ready: readonly Entry[], frameMs: number): void {
    const caption = this.o.caption(best.msg);
    const checkedMs = this.o.clock.now();
    const d = this.acceptAtPaint(best.key); // immediately before compositing (PRD §10.3)
    if (!d.accept) {
      this.remove(best);
      this.emit({ kind: 'drop', key: best.key, reason: d.reason, at: 'frame', tMs: checkedMs });
      this.report(best.key, 'dropped', d.reason, this.times(best));
      return;
    }
    this.remove(best);
    this.draw(best, ready, frameMs, caption, checkedMs);
  }

  /** Draws an accepted frame and reports it; the ready frames below it are skipped. */
  protected draw(best: Entry, ready: readonly Entry[], frameMs: number, caption: Caption, checkedMs: number): void {
    const r = this.o.surface.commit({ key: best.key, msg: best.msg, prepared: best.prepared!, caption, frameMs });
    const committedMs = this.o.clock.now();
    const heldMs = best.heldSinceMs === null ? 0 : committedMs - best.preparedMs;
    const previousMs = this.#previousSince === null ? null : committedMs - this.#previousSince;
    this.#previousSince = null;
    this.#shown = best.key;
    this.emit({
      kind: 'commit',
      key: best.key,
      tMs: committedMs,
      frameMs,
      receivedMs: best.receivedMs,
      preparedMs: best.preparedMs,
      heldMs,
      faded: r.faded,
      checkToCommitMs: committedMs - checkedMs,
      previousMs,
    });
    this.o.onPainted?.(best.key, caption);
    const prefetched = best.prepared?.prefetched;
    const report = this.#build(best.key, 'painted', undefined, {
      ...this.times(best),
      frame_ms: frameMs,
      committed_ms: committedMs,
      ...(heldMs > 0 ? { held_ms: Math.min(heldMs, 60_000) } : {}),
      ...(prefetched === undefined ? {} : { prefetched }),
      ...(previousMs === null ? {} : { previous_ms: Math.min(previousMs, 600_000) }),
      ...(r.faded ? {} : { half_ms: frameMs }),
    }, r.faded);
    if (r.faded && r.fade) {
      this.#fading.push({ key: best.key, fade: r.fade, report, commitFrameMs: frameMs });
      this.requestFrame();
    } else {
      this.emit({ kind: 'half', key: best.key, frameMs, commitFrameMs: frameMs });
      this.o.onReport?.(report);
    }
    for (const e of ready) {
      if (e === best || e.key.tier >= best.key.tier) continue;
      this.remove(e);
      e.prepared?.release();
      this.emit({ kind: 'skip', key: e.key, by: best.key.tier, tMs: committedMs, preparedMs: e.preparedMs });
      this.report(e.key, 'skipped', 'superseded', this.times(e));
    }
  }

  /** Reports each running fade once it stands at 50 % (or, if its layer went first, without a 50 % time). */
  #trackFades(frameMs: number): void {
    if (this.#fading.length === 0) return;
    this.#fading = this.#fading.filter((f) => {
      if (f.fade.cancelled()) {
        this.emit({ kind: 'cut', key: f.key, frameMs, commitFrameMs: f.commitFrameMs });
        this.o.onReport?.(f.report);
        return false;
      }
      if (f.fade.progress() < 0.5) return true;
      this.emit({ kind: 'half', key: f.key, frameMs, commitFrameMs: f.commitFrameMs });
      this.o.onReport?.({ ...f.report, half_ms: frameMs });
      return false;
    });
  }

  protected remove(e: Entry): void {
    const i = this.entries.indexOf(e);
    if (i >= 0) this.entries.splice(i, 1);
  }

  protected times(e: Entry): { received_ms: number; prepared_ms?: number } {
    return e.state === 'ready' ? { received_ms: e.receivedMs, prepared_ms: e.preparedMs } : { received_ms: e.receivedMs };
  }

  protected emit(e: PaintEvent): void {
    this.o.onEvent?.(e);
  }

  #build(
    key: PaintKey,
    outcome: PaintReport['outcome'],
    reason: PaintReport['reason'] | undefined,
    t: Partial<Pick<PaintReport, 'received_ms' | 'prepared_ms' | 'frame_ms' | 'committed_ms' | 'half_ms' | 'held_ms' | 'prefetched' | 'previous_ms'>>,
    faded = false,
  ): PaintReport {
    return {
      v: PROTOCOL_VERSION,
      type: 'paint',
      query_gen: key.gen,
      scene_hash: key.scene,
      tier: key.tier,
      outcome,
      ...(reason === undefined ? {} : { reason }),
      faded,
      received_ms: t.received_ms ?? 0,
      ...(t.prepared_ms === undefined ? {} : { prepared_ms: t.prepared_ms }),
      ...(t.frame_ms === undefined ? {} : { frame_ms: t.frame_ms }),
      ...(t.committed_ms === undefined ? {} : { committed_ms: t.committed_ms }),
      ...(t.half_ms === undefined ? {} : { half_ms: t.half_ms }),
      ...(t.held_ms === undefined ? {} : { held_ms: t.held_ms }),
      ...(t.prefetched === undefined ? {} : { prefetched: t.prefetched }),
      ...(t.previous_ms === undefined ? {} : { previous_ms: t.previous_ms }),
    };
  }

  protected report(key: PaintKey, outcome: PaintReport['outcome'], reason: PaintReport['reason'], t: Partial<Pick<PaintReport, 'received_ms' | 'prepared_ms'>>): void {
    if (this.o.onReport) this.o.onReport(this.#build(key, outcome, reason, t));
  }
}
