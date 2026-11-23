/**
 * Playwright helpers for the browser tests: launch headless Chromium (the installed headless shell,
 * with its default compositing; the renderer's SwiftShader flags only on request, see `launch`) and
 * serve a page from an intercepted origin, so no request leaves the machine.
 */
import { chromium, type Browser, type Page } from 'playwright-core';

/** An https origin (a secure context, so cross-origin isolation applies); every request to it is answered in process. */
export const ORIGIN = 'https://snow-globe.invalid';
/** The renderer's flags (ANGLE on SwiftShader), used only when OVERLAY_BROWSER_ARGS=swiftshader. */
export const SWIFTSHADER_ARGS = ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'];

/**
 * Launches the headless shell with its default compositing. The overlay draws 2D canvases and CSS
 * only (no WebGL until Tier 1 lands in M2), and the renderer's SwiftShader GL flags starve the frame
 * callbacks the stress run tests. Measured on 22 Nov 2026 (`scripts/stress.ts`, M4, load average
 * 8 to 13 from concurrent work), frame interval and commits:
 * - 16 lanes × 60 cycles, three runs each: p50 16.67 ms in both modes; SwiftShader p95 16.7–33.3,
 *   p99 66.7–116.7, max 300–500 ms and 421–439 commits; default p99 16.67, max ≤ 33.3 ms and
 *   438–537 commits.
 * - 64 lanes × 40 cycles, two runs each: SwiftShader p95 83.3, p99 133.3, max 367–517 ms and 628–629
 *   commits; default p95 and p99 16.67 ms and 1,333–1,336 commits.
 * So the tails, not the median, are what SwiftShader costs: half the paints at the stress run's 64
 * lanes. OVERLAY_BROWSER_ARGS=swiftshader restores the flags for comparison.
 */
export async function launch(): Promise<Browser> {
  const args = process.env.OVERLAY_BROWSER_ARGS === 'swiftshader' ? SWIFTSHADER_ARGS : [];
  return chromium.launch({ headless: true, args });
}

/** A page at ORIGIN whose only script is `bundle`; every other request is refused. */
export async function pageWith(browser: Browser, bundle: string, o: { width?: number; height?: number; dpr?: number; reducedMotion?: 'reduce' | 'no-preference' } = {}): Promise<Page> {
  const context = await browser.newContext({
    viewport: { width: o.width ?? 1000, height: o.height ?? 900 },
    deviceScaleFactor: o.dpr ?? 1,
    reducedMotion: o.reducedMotion ?? 'no-preference',
    serviceWorkers: 'block',
  });
  const page = await context.newPage();
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) return route.abort();
    // Cross-origin isolation gives performance.now() 5 µs resolution instead of 100 µs.
    const isolation = { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp' };
    if (url.pathname === '/') {
      return route.fulfill({ status: 200, contentType: 'text/html', headers: isolation, body: '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>overlay test</title><style>body{margin:0;background:#202124}</style></head><body><script src="/bundle.js"></script></body></html>' });
    }
    if (url.pathname === '/bundle.js') return route.fulfill({ status: 200, contentType: 'text/javascript', headers: isolation, body: bundle });
    return route.fulfill({ status: 404, body: 'not found' });
  });
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  (page as Page & { errors: string[] }).errors = errors;
  await page.goto(`${ORIGIN}/`);
  return page;
}
