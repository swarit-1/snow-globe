import { mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { Browser } from 'playwright-core';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { previewHtml } from '../scripts/preview.ts';
import { launch, ORIGIN } from './browser.ts';

/**
 * The preview page (pnpm preview → app/dist/preview.html) builds, every panel reaches its state with
 * every sample message accepted, and nothing errors. A screenshot goes to test-results/ (git-ignored).
 */
let browser: Browser;
beforeAll(async () => {
  browser = await launch();
});
afterAll(async () => {
  await browser?.close();
});

describe('overlay preview page', () => {
  it('renders every state with its sample messages accepted', async () => {
    const html = await previewHtml();
    const context = await browser.newContext({ viewport: { width: 1560, height: 1000 }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    const logs: string[] = [];
    page.on('console', (m) => logs.push(`${m.type()}: ${m.text()}`));
    page.on('pageerror', (e) => logs.push(`pageerror: ${String(e)}`));
    await context.route('**/*', (r) => (r.request().url() === `${ORIGIN}/` ? r.fulfill({ status: 200, contentType: 'text/html', body: html }) : r.abort()));
    await page.goto(`${ORIGIN}/`);
    await page.waitForSelector('body[data-ready="true"]', { timeout: 30_000 });
    const phases = await page.$$eval('figure .sg-overlay', (els) => els.map((e) => e.getAttribute('data-phase')));
    expect(logs).toEqual([]);
    expect(phases.length).toBeGreaterThanOrEqual(20);
    for (const want of ['listening', 'resolving', 'showing', 'no_match', 'out_of_range', 'error']) expect(phases).toContain(want);
    const lines = await page.$$eval('figure .sg-line1', (els) => els.map((e) => e.textContent ?? ''));
    expect(lines.some((l) => l.startsWith('REAL PHOTO, CLOSEST MATCH'))).toBe(true);
    expect(lines.some((l) => l.startsWith('SIMULATED + AI TEXTURE'))).toBe(true);
    const out = fileURLToPath(new URL('../test-results/', import.meta.url));
    mkdirSync(out, { recursive: true });
    await page.screenshot({ path: `${out}overlay-preview.png`, fullPage: true });
    await context.close();
  });
});
