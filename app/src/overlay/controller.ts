/**
 * The overlay controller: the one place where the core's messages meet the state machine (PRD §5.2),
 * the paint gate and the paint path (PRD §10.3). It has no DOM: the view, the frame loader and the
 * clock are injected, so the same controller runs in the app's webview, in headless Chromium for the
 * stress run, and in Node for unit tests.
 *
 * Order of work for each message, all synchronous:
 * 1. validate it (protocol.ts); a refused message changes nothing and is reported as `protocol_error`;
 * 2. update the gate first (begin, set or withdraw the scene, dismiss), then the paint path (reset,
 *    drop), then the model, then render. So by the time anything can paint, the gate already knows.
 *    Whether a scene message or an outcome takes effect is decided by the same predicates for the
 *    gate and the model (state.ts `acceptsScene`, `acceptsOutcome`), so the two never disagree.
 * 3. settle: if nothing can come for the selected scene, take the previous scene's frame off screen;
 *    acknowledge the destination chip once it has been on screen (PRD §15.1).
 *
 * Esc reaches the overlay two ways: from the core's global shortcut as `cancelled` (reason `esc`), or
 * as a key in the focused panel (PRD §5.4). Both close the gate at once; the second also tells the
 * core (`dismiss`), which hides the panel and hands focus back.
 *
 * The destination acknowledgement (`destination_shown`) is per key press: the chip must have been on
 * screen in this reveal before the core may send this key press's first byte to a cloud provider. It
 * is sent two frame callbacks after the chip is rendered in a visible phase (never while idle, when
 * the panel shows nothing), and never for a destination that contradicts the on-device-only setting:
 * that is refused with a `protocol_error` and shown as a warning.
 */
import { composeCaption, type Caption } from './caption.ts';
import { PaintGate, type Gate } from './gate.ts';
import { parseInbound, PROTOCOL_VERSION, type FrameRef, type Inbound, type Outbound, type TierFrameReady } from './protocol.ts';
import { PaintScheduler, browserClock, type FrameClock, type FrameLoader, type PaintEvent, type SchedulerOptions, type Surface } from './scheduler.ts';
import { acceptsOutcome, acceptsScene, exhausted, INITIAL_MODEL, reduce, type LocalEvent, type OverlayModel } from './state.ts';

/** Overlay version, reported in `ready`. */
export const OVERLAY_VERSION = '0.1.0';

/** What the surface needs to know about the settings. */
export interface SurfaceSettings {
  configure(s: { readonly reduceMotion: boolean | null; readonly measuring: boolean }): void;
  /** The frame on screen with its caption burned in, as base64 PNG (PRD §5.5), or null if none. */
  burnIn?(caption: Caption): string | null;
}

/** User actions from the view (PRD §5.4). */
export type Command =
  | { readonly type: 'dismiss' }
  | { readonly type: 'scrub'; readonly minutes: number }
  | { readonly type: 'flip_meridiem' }
  | { readonly type: 'copy' }
  | { readonly type: 'use_offer' }
  | { readonly type: 'open_url'; readonly url: string };

export interface ControllerOptions {
  readonly loader: FrameLoader;
  readonly surface: Surface & SurfaceSettings;
  readonly send: (m: Outbound) => void;
  readonly render?: (m: OverlayModel) => void;
  readonly clock?: FrameClock;
  readonly gate?: Gate;
  readonly onEvent?: (e: PaintEvent) => void;
  readonly skipWindowMs?: number;
  /** Builds the paint scheduler (the stress harness passes deliberately broken ones). */
  readonly scheduler?: (o: SchedulerOptions) => PaintScheduler;
  /** Send `paint` reports to the core (default true). */
  readonly reportPaints?: boolean;
  /** Receives the frames of a `prefetch` (the app's frame cache, index.ts); without it prefetches are ignored. */
  readonly prefetch?: (frames: readonly FrameRef[]) => void;
}

/** True if the core names a cloud destination while on-device only is set (PRD §15.1). */
export function contradictsOnDeviceOnly(m: OverlayModel): boolean {
  return m.settings.onDeviceOnly && m.destinations.length > 0;
}

/** The overlay controller. */
export class OverlayController {
  readonly gate: Gate;
  readonly scheduler: PaintScheduler;
  readonly clock: FrameClock;
  protected readonly o: ControllerOptions;
  #model: OverlayModel = INITIAL_MODEL;
  /** The (seq, generation) whose acknowledgement was sent or is waiting for its frames. */
  #ackKey: string | null = null;

  constructor(o: ControllerOptions) {
    this.o = o;
    this.gate = o.gate ?? new PaintGate();
    this.clock = o.clock ?? browserClock();
    const so: SchedulerOptions = {
      gate: this.gate,
      clock: this.clock,
      loader: o.loader,
      surface: o.surface,
      caption: (msg) => this.captionFor(msg),
      onPainted: (key, caption) => this.#update({ type: 'painted', gen: key.gen, scene: key.scene, tier: key.tier, caption }),
      onFailed: (key) => this.#update({ type: 'failed', gen: key.gen, scene: key.scene, tier: key.tier }),
      ...(o.reportPaints === false ? {} : { onReport: (r) => o.send(r) }),
      ...(o.onEvent ? { onEvent: o.onEvent } : {}),
      ...(o.skipWindowMs === undefined ? {} : { skipWindowMs: o.skipWindowMs }),
    };
    this.scheduler = o.scheduler ? o.scheduler(so) : new PaintScheduler(so);
  }

  /** What the view shows. */
  get model(): OverlayModel {
    return this.#model;
  }

  /** Tells the core the page is up. */
  announce(): void {
    this.o.send({ v: PROTOCOL_VERSION, type: 'ready', overlay_version: OVERLAY_VERSION, time_origin_ms: performance.timeOrigin });
  }

  /** One message from the core: JSON text or a parsed value. Returns false if it was refused. */
  receive(raw: unknown): boolean {
    const r = parseInbound(raw);
    if (!r.ok) {
      this.o.send({ v: PROTOCOL_VERSION, type: 'protocol_error', code: r.problem.code, message_type: r.problem.messageType, detail: r.problem.detail });
      return false;
    }
    this.apply(r.msg);
    return true;
  }

  /** Applies a validated message. */
  apply(m: Inbound): void {
    if ('query_gen' in m && m.type !== 'query_started' && m.query_gen !== null && m.query_gen > this.gate.state.gen) {
      // The core never sends a query's messages before its query_started (in-order delivery).
      this.o.send({ v: PROTOCOL_VERSION, type: 'protocol_error', code: 'generation_ahead', message_type: m.type, detail: 'message.query_gen: no query_started for this generation yet' });
      return;
    }
    switch (m.type) {
      case 'query_started':
        this.onQueryStarted(m.query_gen);
        break;
      case 'parsed':
      case 'scene_changed':
        if (acceptsScene(this.#model, m.query_gen) && this.gate.setScene(m.query_gen, m.scene_hash)) this.scheduler.sceneChanged();
        break;
      case 'no_match':
      case 'out_of_range':
        if (acceptsOutcome(this.#model, m.query_gen)) this.onOutcome(m.query_gen);
        break;
      case 'tier_started':
        this.scheduler.expect(m);
        return; // the model changes when a frame paints
      case 'tier_rejected':
        this.scheduler.withdraw(m.query_gen, m.scene_hash, m.tier);
        break;
      case 'tier_frame_ready':
        this.scheduler.offer(m);
        return; // the model changes when it paints
      case 'cancelled':
        if (m.query_gen === this.gate.state.gen) this.onCancelled();
        break;
      case 'error':
        if (m.fatal && (m.query_gen === null || m.query_gen === this.gate.state.gen)) this.onCancelled();
        break;
      case 'settings':
        this.o.surface.configure({ reduceMotion: m.reduce_motion, measuring: m.measuring });
        break;
      case 'destination':
        if (m.destinations.length > 0 && this.#model.settings.onDeviceOnly) {
          this.o.send({ v: PROTOCOL_VERSION, type: 'protocol_error', code: 'invalid_message', message_type: 'destination', detail: 'message.destinations: a cloud destination while on-device only is set' });
        }
        break;
      case 'prefetch':
        this.o.prefetch?.(m.frames);
        return; // pixels only
      default:
        break;
    }
    this.#update(m);
  }

  /** One step of the model, then render and settle. */
  #update(e: Inbound | LocalEvent): void {
    const before = this.#model;
    this.#model = reduce(before, e);
    if (this.#model !== before) this.o.render?.(this.#model);
    this.#settle();
  }

  /** A key press began generation `gen`: close the gate to everything older and clear the surface. */
  protected onQueryStarted(gen: number): void {
    if (this.gate.begin(gen)) this.scheduler.reset();
  }

  /** Esc, key-up with no speech, or a fatal error: nothing of this query paints again. */
  protected onCancelled(): void {
    this.gate.dismiss();
    this.scheduler.reset();
  }

  /**
   * No match or Out of range for the current query: its scene (shown on a stable partial, PRD §7.2) is
   * withdrawn, so nothing of it stays on screen or paints later; the gate stays open for an answer
   * of the same key press (the Out of range offer).
   */
  protected onOutcome(gen: number): void {
    this.gate.clearScene(gen);
    this.scheduler.reset();
  }

  /** True while the panel shows the destination chip: every phase but idle renders it (view.ts). */
  protected chipShown(m: OverlayModel): boolean {
    return m.phase !== 'idle';
  }

  #settle(): void {
    const m = this.#model;
    // Every tier of the selected scene was rejected or failed: the previous scene's frame must not
    // stand in for it any longer (frames still on their way for the scene are kept).
    if (exhausted(m) && m.shownScene !== null && m.scene !== null && !this.scheduler.waitingFor(m.gen, m.scene)) {
      this.scheduler.clearShown();
      this.#update({ type: 'cleared', gen: m.gen });
      return;
    }
    this.#maybeAck();
  }

  /** Acknowledges the destination once its chip has been on screen for a full frame (two frame callbacks) in this key press. */
  #maybeAck(): void {
    const m = this.#model;
    if (m.destinationSeq === 0 || !this.chipShown(m) || contradictsOnDeviceOnly(m)) return;
    const seq = m.destinationSeq;
    const gen = m.gen;
    const key = `${seq}|${gen}`;
    if (this.#ackKey === key) return;
    this.#ackKey = key;
    this.clock.requestFrame(() => {
      this.clock.requestFrame((frameMs) => {
        const now = this.#model;
        if (now.destinationSeq !== seq || now.gen !== gen || !this.chipShown(now) || contradictsOnDeviceOnly(now)) {
          if (this.#ackKey === key) this.#ackKey = null;
          return;
        }
        this.o.send({ v: PROTOCOL_VERSION, type: 'destination_shown', seq, query_gen: gen, frame_ms: frameMs });
      });
    });
  }

  /** A user action in the panel (PRD §5.4). */
  command(c: Command): void {
    const m = this.#model;
    const gen = m.gen > 0 ? m.gen : null;
    const send = this.o.send;
    switch (c.type) {
      case 'dismiss':
        if (m.phase !== 'idle') {
          this.onCancelled();
          this.#update({ type: 'local_dismiss' });
        }
        send({ v: PROTOCOL_VERSION, type: 'dismiss', query_gen: gen });
        break;
      case 'scrub':
        // From a shown or resolving query, and back out of an Out of range reached by scrubbing.
        if (gen !== null && m.query !== null && (m.phase === 'showing' || m.phase === 'resolving' || m.phase === 'out_of_range')) {
          send({ v: PROTOCOL_VERSION, type: 'scrub', query_gen: gen, minutes: c.minutes });
        }
        break;
      case 'flip_meridiem':
        if (gen !== null && m.query?.ambiguities.some((a) => a.field === 'meridiem') && (m.phase === 'showing' || m.phase === 'resolving')) {
          send({ v: PROTOCOL_VERSION, type: 'flip_meridiem', query_gen: gen });
        }
        break;
      case 'copy': {
        if (gen === null || m.caption === null || m.phase !== 'showing') break;
        const png = this.o.surface.burnIn?.(m.caption) ?? null;
        // TODO(M3): refined frames also carry IPTC trainedAlgorithmicMedia (PRD §5.5); the core adds it to the export.
        if (png !== null) send({ v: PROTOCOL_VERSION, type: 'copy', query_gen: gen, png_base64: png, refined: m.caption.kind === 'refined' });
        break;
      }
      case 'use_offer':
        if (gen !== null && m.phase === 'out_of_range' && m.outOfRange?.latest_valid) {
          send({ v: PROTOCOL_VERSION, type: 'use_offer', query_gen: gen, valid_time: m.outOfRange.latest_valid });
        }
        break;
      case 'open_url':
        if (/^https:\/\//.test(c.url)) send({ v: PROTOCOL_VERSION, type: 'open_url', url: c.url });
        break;
    }
  }

  /** The caption for a frame, from the current display names, settings and network state. */
  protected captionFor(msg: TierFrameReady): Caption {
    const m = this.#model;
    return composeCaption(msg.label, {
      display: m.display ?? { place_name: '', viewpoint_name: '' },
      query: m.query,
      fmt: { locale: m.settings.locale, units: m.settings.units },
      online: m.online,
    });
  }
}
