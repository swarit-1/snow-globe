/**
 * The overlay's public entry (PRD §6.1: the Overlay component, TypeScript in the webview): mount it
 * in the panel's page, feed it the core's messages, and send what it reports back to the core.
 *
 * ```ts
 * const overlay = mountOverlay(document.body, { send: (msg) => toCore(msg) });
 * fromCore((json) => overlay.receive(json));
 * ```
 */
import { cachedLoader, decodeFrameUrl, FrameCache } from './cache.ts';
import { OverlayController, type ControllerOptions, type SurfaceSettings } from './controller.ts';
import type { Gate } from './gate.ts';
import type { Outbound } from './protocol.ts';
import type { FrameClock, FrameLoader, PaintEvent, PaintScheduler, SchedulerOptions, Surface } from './scheduler.ts';
import { OverlayView } from './view.ts';

export { OverlayController, OVERLAY_VERSION, type Command } from './controller.ts';
export { OverlayView, keyCommand, announcement } from './view.ts';
export { PaintGate, checkPaint, type Gate, type PaintKey, type SceneHash, type Tier } from './gate.ts';
export { PaintScheduler, browserClock, SKIP_WINDOW_MS, type FrameClock, type FrameLoader, type PaintEvent, type Prepared, type Surface } from './scheduler.ts';
export { parseInbound, protocolSchema, outboundProblem, isFrameUrl, PROTOCOL_VERSION, type Inbound, type Outbound } from './protocol.ts';
export { FrameCache, cachedLoader, decodeFrameUrl, type Decode, type Decoded } from './cache.ts';
export { composeCaption, type Caption } from './caption.ts';
export { chipsFor, destinationChip, type Chip } from './chips.ts';
export { reduce, stateName, INITIAL_MODEL, type OverlayModel } from './state.ts';
export { tagCells, fromCells, readTag, CELLS, CELL_PX } from './tag.ts';

/**
 * The app's frame loader with no cache: fetches the frame from its app-local URL (any other URL is
 * refused) and decodes it off the main thread with createImageBitmap. An abort (new query, Esc)
 * stops the fetch. `mountOverlay` uses it behind a `FrameCache` unless told otherwise.
 */
export const fetchLoader: FrameLoader = async (msg, signal) => {
  const d = await decodeFrameUrl(msg.frame.url, signal);
  return { source: d.source, width: d.width, height: d.height, prefetched: false, release: () => d.close() };
};

export interface MountOptions {
  /** Sends a message to the core. */
  readonly send: (m: Outbound) => void;
  /** How frames are loaded; by default from `frameCache`, or decoded on the spot on a miss. */
  readonly loader?: FrameLoader;
  /** The background-decode cache `prefetch` fills (default: a new one, unless `loader` is given; null for none). */
  readonly frameCache?: FrameCache | null;
  readonly clock?: FrameClock;
  readonly gate?: Gate;
  readonly onEvent?: (e: PaintEvent) => void;
  readonly scheduler?: (o: SchedulerOptions) => PaintScheduler;
  readonly skipWindowMs?: number;
  readonly reportPaints?: boolean;
  /** Where keys are heard (default: the page's window; null for none). */
  readonly keyTarget?: EventTarget | null;
  /** Builds the controller (the stress harness passes deliberately broken ones). */
  readonly controller?: (o: ControllerOptions) => OverlayController;
  /** Wraps the view as the paint surface (the stress harness passes a deliberately broken one). */
  readonly surface?: (view: OverlayView) => Surface & SurfaceSettings;
}

export interface MountedOverlay {
  readonly controller: OverlayController;
  readonly view: OverlayView;
  /** The frame cache `prefetch` fills, if any. */
  readonly frameCache: FrameCache | null;
  /** One message from the core (JSON text or a parsed value). False if refused. */
  receive(raw: unknown): boolean;
  destroy(): void;
}

/** Builds the overlay inside `root`. */
export function mountOverlay(root: HTMLElement, o: MountOptions): MountedOverlay {
  let controller: OverlayController | null = null;
  const view = new OverlayView({
    root,
    onCommand: (c) => controller?.command(c),
    ...(o.keyTarget === undefined ? {} : { keyTarget: o.keyTarget }),
  });
  const cache = o.frameCache === undefined ? (o.loader ? null : new FrameCache()) : o.frameCache;
  const co: ControllerOptions = {
    loader: o.loader ?? (cache ? cachedLoader(cache) : fetchLoader),
    surface: o.surface ? o.surface(view) : view,
    send: o.send,
    render: (m) => view.render(m),
    ...(cache ? { prefetch: (frames) => cache.prefetch(frames) } : {}),
    ...(o.clock ? { clock: o.clock } : {}),
    ...(o.gate ? { gate: o.gate } : {}),
    ...(o.onEvent ? { onEvent: o.onEvent } : {}),
    ...(o.scheduler ? { scheduler: o.scheduler } : {}),
    ...(o.skipWindowMs === undefined ? {} : { skipWindowMs: o.skipWindowMs }),
    ...(o.reportPaints === undefined ? {} : { reportPaints: o.reportPaints }),
  };
  const c = o.controller ? o.controller(co) : new OverlayController(co);
  controller = c;
  view.render(c.model);
  return {
    controller: c,
    view,
    frameCache: cache,
    receive: (raw) => c.receive(raw),
    destroy: () => {
      view.destroy();
      cache?.clear();
    },
  };
}

/**
 * For the Tauri shell: mounts the overlay on the page and exposes `window.snowGlobeOverlay.receive`
 * for the core to call with each message's JSON text.
 * TODO(M1): the shell's IPC (the `invoke` command that carries outbound messages, and how the core
 * delivers inbound ones in order) is fixed when app/src-tauri exists, after the platform spike.
 * TODO(M1): the frame transport, agreed with the Tier 0 package (app/core/src/tier0/decode.rs decodes
 * candidates to RGB in Rust): the shell serves frames under `snowglobe://localhost/` (JPEG thumbnails
 * the webview decodes, or the core's decoded RGB), and the core sends `prefetch` with the same URLs
 * it later puts in `tier_frame_ready`, so a prefetched frame is a swap.
 */
export function installGlobal(send: (m: Outbound) => void, root: HTMLElement = document.body): MountedOverlay {
  const overlay = mountOverlay(root, { send });
  (globalThis as unknown as { snowGlobeOverlay: MountedOverlay }).snowGlobeOverlay = overlay;
  overlay.controller.announce();
  return overlay;
}
