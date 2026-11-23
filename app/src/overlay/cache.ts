/**
 * The webview's frame cache (PRD §9.1 "Latency": the viewpoint's likely frames are decoded in the
 * background, so the key-up path is parse → swap; §6.3 step 2; §10.2 "GPU textures in memory").
 *
 * The core sends `prefetch` with the frames it expects to ask for soon (a stable partial named the
 * place). The cache decodes them off the main path into ImageBitmaps, bounded by bytes and by count
 * (least recently used goes first). When `tier_frame_ready` names a cached URL, the loader hands the
 * scheduler a lease on those pixels: no fetch and no decode on the key-up path. A miss is decoded on
 * the spot, and every paint report says which it was (`prefetched`).
 *
 * Leases: a cached frame may be on screen while the cache evicts it, so its pixels are freed only
 * when both have let go (`evicted` and no lease left).
 */
import { isFrameUrl, type FrameRef, type TierFrameReady } from './protocol.ts';
import type { FrameLoader, Prepared } from './scheduler.ts';

/** Decoded pixels the cache can hold. */
export interface Decoded {
  readonly source: CanvasImageSource | null;
  readonly width: number;
  readonly height: number;
  close(): void;
}

/** Fetches and decodes one frame URL. */
export type Decode = (url: string, signal: AbortSignal) => Promise<Decoded>;

/**
 * The app's decode: the frame from its app-local URL, decoded with createImageBitmap (off the main
 * thread in WebKit and Chromium). Refuses any other URL, so the webview never fetches a remote camera.
 */
export const decodeFrameUrl: Decode = async (url, signal) => {
  if (!isFrameUrl(url)) throw new Error('not an app-local frame URL');
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`frame fetch failed: ${res.status}`);
  const bitmap = await createImageBitmap(await res.blob());
  if (signal.aborted) {
    bitmap.close();
    throw new Error('aborted');
  }
  return { source: bitmap, width: bitmap.width, height: bitmap.height, close: () => bitmap.close() };
};

interface Slot {
  readonly url: string;
  readonly bytes: number;
  readonly abort: AbortController;
  readonly promise: Promise<Decoded>;
  decoded: Decoded | null;
  leases: number;
  evicted: boolean;
}

export interface FrameCacheOptions {
  readonly decode?: Decode;
  /** Decoded bytes held at most (RGBA, from the frames' sizes). Default 64 MiB. */
  readonly maxBytes?: number;
  /** Frames held at most. Default 32. */
  readonly maxEntries?: number;
}

/** Counts for tests and diagnostics. */
export interface FrameCacheStats {
  prefetched: number;
  hits: number;
  misses: number;
  evicted: number;
  failed: number;
}

/** A bounded cache of decoded frames, filled by `prefetch`. */
export class FrameCache {
  readonly stats: FrameCacheStats = { prefetched: 0, hits: 0, misses: 0, evicted: 0, failed: 0 };
  /** How this cache decodes; `cachedLoader` decodes misses the same way. */
  readonly decode: Decode;
  readonly #maxBytes: number;
  readonly #maxEntries: number;
  /** Insertion order is recency: the first slot is the least recently used. */
  readonly #slots = new Map<string, Slot>();
  #bytes = 0;

  constructor(o: FrameCacheOptions = {}) {
    this.decode = o.decode ?? decodeFrameUrl;
    this.#maxBytes = o.maxBytes ?? 64 * 2 ** 20;
    this.#maxEntries = o.maxEntries ?? 32;
  }

  /** Frames held (decoded or decoding). */
  get size(): number {
    return this.#slots.size;
  }

  /** Decoded bytes held. */
  get bytes(): number {
    return this.#bytes;
  }

  /** True if `url` is held (decoded or decoding). */
  has(url: string): boolean {
    return this.#slots.has(url);
  }

  /** Starts decoding every frame not yet held; frames already held become the most recent. */
  prefetch(frames: readonly FrameRef[]): void {
    for (const f of frames) {
      const held = this.#slots.get(f.url);
      if (held) {
        this.#touch(held);
        continue;
      }
      const abort = new AbortController();
      const promise = this.decode(f.url, abort.signal);
      const slot: Slot = { url: f.url, bytes: f.width * f.height * 4, abort, promise, decoded: null, leases: 0, evicted: false };
      this.#slots.set(f.url, slot);
      this.#bytes += slot.bytes;
      this.stats.prefetched += 1;
      promise.then(
        (d) => {
          slot.decoded = d;
          if (slot.evicted && slot.leases === 0) d.close();
        },
        () => {
          if (slot.evicted) return; // aborted by its eviction
          this.stats.failed += 1;
          this.#remove(slot);
        },
      );
      this.#evictOver();
    }
  }

  /**
   * A lease on the pixels for `url` if it is held, else null (a miss). The lease resolves when the
   * background decode does; its `release` gives the pixels back.
   */
  lease(url: string): Promise<Prepared> | null {
    const slot = this.#slots.get(url);
    if (!slot) {
      this.stats.misses += 1;
      return null;
    }
    this.stats.hits += 1;
    this.#touch(slot);
    slot.leases += 1;
    let released = false;
    const release = (): void => {
      if (released) return;
      released = true;
      slot.leases -= 1;
      if (slot.evicted && slot.leases === 0) slot.decoded?.close();
    };
    return slot.promise.then(
      (d) => ({ source: d.source, width: d.width, height: d.height, prefetched: true, release }),
      (e: unknown) => {
        release();
        throw e;
      },
    );
  }

  /** Frees everything not leased (leased pixels go when their lease ends). */
  clear(): void {
    for (const slot of [...this.#slots.values()]) this.#evict(slot);
  }

  #touch(slot: Slot): void {
    this.#slots.delete(slot.url);
    this.#slots.set(slot.url, slot);
  }

  #remove(slot: Slot): void {
    if (this.#slots.get(slot.url) === slot) {
      this.#slots.delete(slot.url);
      this.#bytes -= slot.bytes;
    }
  }

  #evict(slot: Slot): void {
    this.#remove(slot);
    slot.evicted = true;
    this.stats.evicted += 1;
    if (slot.decoded === null) slot.abort.abort();
    else if (slot.leases === 0) slot.decoded.close();
  }

  #evictOver(): void {
    while (this.#slots.size > this.#maxEntries || (this.#bytes > this.#maxBytes && this.#slots.size > 1)) {
      const oldest = this.#slots.values().next().value as Slot;
      this.#evict(oldest);
    }
  }
}

/** A loader that takes a frame from the cache when it was prefetched, else decodes it on the spot (with the cache's decoder). */
export function cachedLoader(cache: FrameCache, decode: Decode = cache.decode): FrameLoader {
  const direct = async (msg: TierFrameReady, signal: AbortSignal): Promise<Prepared> => {
    const d = await decode(msg.frame.url, signal);
    return { source: d.source, width: d.width, height: d.height, prefetched: false, release: () => d.close() };
  };
  return (msg, signal) => {
    const lease = cache.lease(msg.frame.url);
    // A prefetch whose decode failed is not the end: decode on the spot.
    return lease ? lease.catch(() => direct(msg, signal)) : direct(msg, signal);
  };
}
