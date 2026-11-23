import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { Browser } from 'playwright-core';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { harnessBundle } from '../scripts/bundle.ts';
import { launch, pageWith } from './browser.ts';

/**
 * The background decode of PRD §9.1 ("thumbnails ... are decoded in the background, so the key-up
 * path is parse → swap"), measured through the real overlay in headless Chromium: Tier 0 frames that
 * a `prefetch` named are handed over from the frame cache; frames it did not name are decoded on the
 * key-up path. The frames are synthetic 640 × 360 JPEGs made in the page (gradient and noise, so they
 * compress like photos), decoded with createImageBitmap as the app decodes; no camera frame is used.
 * The decoder reads them from memory, so the fetch from the shell's URL scheme is not in these times.
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

interface Pct {
  n: number;
  p50: number;
  p95: number;
  max: number;
}

describe('frame cache in headless Chromium (PRD §9.1, §10.1 swap ≤ 10 ms)', () => {
  it('takes prefetched Tier 0 frames off the key-up path: a swap instead of a JPEG decode', async () => {
    const page = await pageWith(browser, bundle);
    const r = await page.evaluate(async () => {
      const g = globalThis as unknown as {
        SnowGlobeHarness: {
          overlay: {
            FrameCache: new (o: unknown) => { stats: Record<string, number> };
            mountOverlay: (root: HTMLElement, o: unknown) => { receive(m: unknown): boolean };
          };
          samples: { parsedMessage(g: number, u: string): { scene_hash: string }; tierFrame(g: number, s: string, t: number, url: string): unknown; syntheticUrl(n: string): string };
        };
      };
      const { overlay, samples } = g.SnowGlobeHarness;
      const N = 40;
      const W = 640;
      const H = 360;
      const sleep = (ms: number): Promise<void> => new Promise((res) => setTimeout(res, ms));
      // Synthetic JPEG frames: a sky gradient, a ridge and noise, different per frame.
      const blobs = new Map<string, Blob>();
      let seed = 12345;
      for (let i = 0; i < N; i++) {
        const c = new OffscreenCanvas(W, H);
        const x = c.getContext('2d')!;
        const grad = x.createLinearGradient(0, 0, 0, H);
        grad.addColorStop(0, `rgb(${40 + i},${80 + i},${150 - i})`);
        grad.addColorStop(1, `rgb(${200 - i},${190},${170 + i})`);
        x.fillStyle = grad;
        x.fillRect(0, 0, W, H);
        x.fillStyle = '#d8dde2';
        x.beginPath();
        x.moveTo(W * 0.15, H * 0.72);
        x.lineTo(W * (0.45 + i / 400), H * 0.25);
        x.lineTo(W * 0.85, H * 0.72);
        x.fill();
        const img = x.getImageData(0, 0, W, H);
        for (let k = 0; k < img.data.length; k += 4) {
          seed = (seed * 1103515245 + 12345) >>> 0;
          const n = ((seed >>> 16) & 0x3f) - 32;
          img.data[k] = img.data[k]! + n;
          img.data[k + 1] = img.data[k + 1]! + n;
          img.data[k + 2] = img.data[k + 2]! + n;
        }
        x.putImageData(img, 0, 0);
        blobs.set(samples.syntheticUrl(`jpeg/${i}`), await c.convertToBlob({ type: 'image/jpeg', quality: 0.85 }));
      }
      const jpegBytes = [...blobs.values()].map((b) => b.size);
      // The decode the app does (createImageBitmap), reading the bytes from memory.
      const decode = async (url: string, signal: AbortSignal) => {
        const blob = blobs.get(url);
        if (!blob) throw new Error('no such frame');
        const bmp = await createImageBitmap(blob);
        if (signal.aborted) {
          bmp.close();
          throw new Error('aborted');
        }
        return { source: bmp, width: bmp.width, height: bmp.height, close: () => bmp.close() };
      };
      // The decode alone, one frame at a time, for reference.
      const decodeMs: number[] = [];
      for (const url of blobs.keys()) {
        const t0 = performance.now();
        const d = await decode(url, new AbortController().signal);
        decodeMs.push(performance.now() - t0);
        d.close();
      }
      const cache = new overlay.FrameCache({ decode });
      const sent: Record<string, unknown>[] = [];
      const root = document.createElement('div');
      root.style.width = '640px';
      document.body.appendChild(root);
      const o = overlay.mountOverlay(root, { send: (m: Record<string, unknown>) => sent.push(m), frameCache: cache, keyTarget: null });
      o.receive({ v: 1, type: 'settings', on_device_only: false, cloud_speech: false, tier2: false, locale: 'en-US', units: 'metric', reduce_motion: null, measuring: false });
      for (let i = 0; i < N; i++) {
        const gen = i + 1;
        const url = samples.syntheticUrl(`jpeg/${i}`);
        const prefetched = i % 2 === 1;
        o.receive({ v: 1, type: 'query_started', query_gen: gen });
        // A stable partial named the place: the core prefetches the likely frames while the key is held.
        if (prefetched) o.receive({ v: 1, type: 'prefetch', frames: [{ url, width: W, height: H }] });
        await sleep(150);
        o.receive({ v: 1, type: 'released', query_gen: gen });
        const p = samples.parsedMessage(gen, 'is rainier out tomorrow at 4');
        o.receive(p);
        o.receive(samples.tierFrame(gen, p.scene_hash, 0, url));
        await sleep(120);
      }
      const paints = sent.filter((m) => m.type === 'paint' && m.outcome === 'painted');
      return { paints, decodeMs, jpegBytes, stats: cache.stats };
    });
    await page.context().close();

    const pct = (xs: number[]): Pct => {
      const s = [...xs].sort((a, b) => a - b);
      const at = (q: number): number => Math.round(s[Math.min(s.length - 1, Math.floor(q * (s.length - 1)))]! * 1000) / 1000;
      return { n: s.length, p50: at(0.5), p95: at(0.95), max: Math.round(s[s.length - 1]! * 1000) / 1000 };
    };
    const of = (hit: boolean, a: string, b: string): number[] =>
      r.paints.filter((m) => m.prefetched === hit).map((m) => (m[a] as number) - (m[b] as number));
    const result = {
      jpegKiB: pct(r.jpegBytes.map((b) => b / 1024)),
      decodeAloneMs: pct(r.decodeMs),
      prefetched: { receivedToPreparedMs: pct(of(true, 'prepared_ms', 'received_ms')), receivedToCommittedMs: pct(of(true, 'committed_ms', 'received_ms')) },
      decodedOnKeyUp: { receivedToPreparedMs: pct(of(false, 'prepared_ms', 'received_ms')), receivedToCommittedMs: pct(of(false, 'committed_ms', 'received_ms')) },
      cache: r.stats,
      browser: browser.version(),
    };
    const out = fileURLToPath(new URL('../test-results/', import.meta.url));
    mkdirSync(out, { recursive: true });
    writeFileSync(`${out}overlay-prefetch.json`, `${JSON.stringify(result, null, 2)}\n`);
    console.log(`prefetch: ${JSON.stringify(result)}`);

    expect(r.paints.length).toBe(40);
    expect(result.cache).toMatchObject({ prefetched: 20, hits: 20, misses: 20, failed: 0 });
    expect(result.prefetched.receivedToPreparedMs.n).toBe(20);
    // A prefetched frame skips the decode: its wait for pixels is shorter than every decode on the spot.
    expect(result.prefetched.receivedToPreparedMs.max).toBeLessThan(Math.min(...of(false, 'prepared_ms', 'received_ms')));
  });
});
