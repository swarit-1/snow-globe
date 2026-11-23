import { describe, expect, it } from 'vitest';
import { cachedLoader, FrameCache, type Decode, type Decoded } from './cache.ts';
import { flush } from './fakes.ts';
import { syntheticUrl, tierFrame } from './samples.ts';

/** A decode the test resolves by hand, recording what was asked and what was freed. */
function manualDecode() {
  const asked: string[] = [];
  const closed: string[] = [];
  const pending = new Map<string, { resolve: (d: Decoded) => void; reject: (e: Error) => void; signal: AbortSignal }>();
  const decode: Decode = (url, signal) =>
    new Promise<Decoded>((resolve, reject) => {
      asked.push(url);
      pending.set(url, { resolve, reject, signal });
    });
  const finish = (url: string): void => {
    pending.get(url)!.resolve({ source: null, width: 640, height: 360, close: () => closed.push(url) });
    pending.delete(url);
  };
  const fail = (url: string): void => {
    pending.get(url)!.reject(new Error('decode failed'));
    pending.delete(url);
  };
  return { decode, asked, closed, pending, finish, fail };
}

const frame = (name: string) => ({ url: syntheticUrl(name), width: 640, height: 360 });

describe('frame cache (PRD §9.1: background decode, so the key-up path is a swap)', () => {
  it('decodes prefetched frames in the background and hands them over at key-up with no new decode', async () => {
    const d = manualDecode();
    const cache = new FrameCache({ decode: d.decode });
    cache.prefetch([frame('a'), frame('b')]);
    expect(d.asked).toEqual([syntheticUrl('a'), syntheticUrl('b')]);
    d.finish(syntheticUrl('a'));
    await flush();
    const loader = cachedLoader(cache, d.decode);
    const p = await loader(tierFrame(1, '00000000000000aa', 0, syntheticUrl('a')), new AbortController().signal);
    expect(p.prefetched).toBe(true);
    expect(d.asked.length).toBe(2); // no decode on the key-up path
    expect(cache.stats).toMatchObject({ prefetched: 2, hits: 1, misses: 0 });
  });

  it('decodes a frame that was not prefetched on the spot, and says so', async () => {
    const d = manualDecode();
    const cache = new FrameCache({ decode: d.decode });
    const loader = cachedLoader(cache, d.decode);
    const promise = loader(tierFrame(1, '00000000000000aa', 0, syntheticUrl('z')), new AbortController().signal);
    d.finish(syntheticUrl('z'));
    const p = await promise;
    expect(p.prefetched).toBe(false);
    expect(cache.stats.misses).toBe(1);
    p.release();
    expect(d.closed).toEqual([syntheticUrl('z')]);
  });

  it('falls back to a decode on the spot when the background decode failed', async () => {
    const d = manualDecode();
    const cache = new FrameCache({ decode: d.decode });
    cache.prefetch([frame('a')]);
    d.fail(syntheticUrl('a'));
    await flush();
    expect(cache.has(syntheticUrl('a'))).toBe(false);
    const promise = cachedLoader(cache, d.decode)(tierFrame(1, '00000000000000aa', 0, syntheticUrl('a')), new AbortController().signal);
    d.finish(syntheticUrl('a'));
    expect((await promise).prefetched).toBe(false);
    expect(cache.stats.failed).toBe(1);
  });

  it('is bounded by count and by bytes, least recently used first, and frees what it evicts', async () => {
    const d = manualDecode();
    const cache = new FrameCache({ decode: d.decode, maxEntries: 3, maxBytes: 640 * 360 * 4 * 10 });
    cache.prefetch([frame('a'), frame('b'), frame('c')]);
    for (const n of ['a', 'b', 'c']) d.finish(syntheticUrl(n));
    await flush();
    cache.prefetch([frame('a')]); // touched: now the most recent
    cache.prefetch([frame('d')]);
    expect([cache.has(syntheticUrl('a')), cache.has(syntheticUrl('b')), cache.has(syntheticUrl('c')), cache.has(syntheticUrl('d'))]).toEqual([true, false, true, true]);
    expect(d.closed).toEqual([syntheticUrl('b')]);
    const small = new FrameCache({ decode: manualDecode().decode, maxBytes: 640 * 360 * 4 * 2 });
    small.prefetch([frame('a'), frame('b'), frame('c')]);
    expect([small.size, small.bytes]).toEqual([2, 640 * 360 * 4 * 2]);
  });

  it('keeps leased pixels alive after eviction until the lease ends (a frame on screen is never freed under it)', async () => {
    const d = manualDecode();
    const cache = new FrameCache({ decode: d.decode, maxEntries: 1 });
    cache.prefetch([frame('a')]);
    d.finish(syntheticUrl('a'));
    await flush();
    const lease = await cache.lease(syntheticUrl('a'))!;
    cache.prefetch([frame('b')]); // evicts a
    expect(d.closed).toEqual([]);
    lease.release();
    lease.release(); // twice is harmless
    expect(d.closed).toEqual([syntheticUrl('a')]);
  });

  it('aborts the decode of a frame evicted before it finished', () => {
    const d = manualDecode();
    const cache = new FrameCache({ decode: d.decode, maxEntries: 1 });
    cache.prefetch([frame('a')]);
    const signal = d.pending.get(syntheticUrl('a'))!.signal;
    cache.prefetch([frame('b')]);
    expect(signal.aborted).toBe(true);
    expect(cache.stats.evicted).toBe(1);
  });
});
