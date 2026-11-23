/**
 * Headless runner (PRD §6.1, §9.5): Playwright Chromium with SwiftShader, so evaluation renders the same
 * frames as the app without a GPU. Confirms through WEBGL_debug_renderer_info that SwiftShader is really
 * active and refuses to run otherwise.
 *
 * The page is served from an intercepted origin (http://snow-globe.invalid); no request leaves the machine.
 * Inputs travel as raw RGBA bytes via route handlers; outputs come back as base64 pixels.
 */
import { createHash } from 'node:crypto';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright-core';
import type { RawImage } from '../cpu/reference.ts';
import type { AtmosphereReadback, FloatImage, GlInfo } from '../webgl/renderer.ts';
import type { DebugMode } from '../webgl/shader.ts';
import type { SceneSpec } from '../scene.ts';
import { buildIifeBundle } from './bundle.ts';

export type Backend = 'swiftshader' | 'gpu';

/** Chromium flags that force ANGLE onto SwiftShader (CPU), per the task and PRD §9.5. */
export const SWIFTSHADER_ARGS = ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'];
/** Chromium flags for the Mac's GPU through ANGLE's Metal backend (benchmark comparison only). */
export const GPU_ARGS = ['--use-angle=metal', '--enable-gpu', '--ignore-gpu-blocklist'];

const ORIGIN = 'http://snow-globe.invalid';

export interface HeadlessInputs {
  plate: RawImage;
  skyMask?: RawImage | null;
  depth?: RawImage | null;
  /** SHA-256 of the files the images came from; the page checks each one given against scene.plate. */
  sha256?: { plate?: string; skyMask?: string; depth?: string };
}

export interface HeadlessRenderOptions {
  /** Accept a Sun that does not match the valid time and place (synthetic geometry tests only). */
  allowSyntheticSun?: boolean;
  /** Time each atmosphere pass to a readPixels barrier (benchmarks only; RenderOptions.profile). */
  profile?: boolean;
}

export interface PageTiming {
  /** Page-side wall time of render() + the 1-pixel readPixels barrier: CPU setup, uploads and GPU execution (ms). */
  renderMs: number;
  /** Atmosphere passes the render issued (the transmittance and multiple-scattering tables only when the atmosphere changed). */
  atmospherePasses: string[];
  /** With profile: each atmosphere pass's time to a readPixels barrier (ms). */
  passMs?: Record<string, number>;
  setupMs: number;
  uploadMs: number;
  submitMs: number;
  /** Page-side wall time of the full-frame readPixels plus the row flip to top-down order (ms); 0 if not read. */
  readbackMs: number;
  /** Page-side wall time of base64-encoding the pixels for the trip to Node (ms). The transfer itself is not timed. */
  encodeMs: number;
  uploaded: string[];
}

export interface HeadlessRenderResult {
  width: number;
  height: number;
  rgba: Uint8Array;
  timing: PageTiming;
}

interface InputSpec {
  id: string;
  width: number;
  height: number;
}

function inputId(img: RawImage): string {
  return createHash('sha256').update(img.data).update(`${img.width}x${img.height}`).digest('hex').slice(0, 24);
}

function fromBase64(b64: string): Uint8Array {
  const b = Buffer.from(b64, 'base64');
  return new Uint8Array(b.buffer, b.byteOffset, b.byteLength);
}

export class HeadlessRenderer {
  readonly backend: Backend;
  readonly glInfo: GlInfo;
  readonly browserVersion: string;
  readonly launchMs: number;
  private readonly browser: Browser;
  private readonly context: BrowserContext;
  private readonly page: Page;
  /** Inputs the route handler serves at /input/<id> (shared with the handler installed by launch()). */
  private readonly served: Map<string, Buffer>;

  private constructor(backend: Backend, browser: Browser, context: BrowserContext, page: Page, glInfo: GlInfo, launchMs: number, served: Map<string, Buffer>) {
    this.backend = backend;
    this.browser = browser;
    this.context = context;
    this.page = page;
    this.glInfo = glInfo;
    this.browserVersion = browser.version();
    this.launchMs = launchMs;
    this.served = served;
  }

  /**
   * Launches Chromium and loads the renderer. For 'swiftshader' the unmasked GL renderer string must name
   * SwiftShader, or this throws: a CPU-path result must never silently come from another backend.
   */
  static async launch(opts: { backend?: Backend } = {}): Promise<HeadlessRenderer> {
    const backend = opts.backend ?? 'swiftshader';
    const t0 = performance.now();
    const bundle = await buildIifeBundle();
    const args = backend === 'swiftshader' ? SWIFTSHADER_ARGS : GPU_ARGS;
    // The GPU comparison prefers full Chromium (new headless); where only Playwright's headless shell is
    // installed it uses that, which also reaches Metal ("ANGLE Metal Renderer: Apple M4", 2026-11-23).
    // SwiftShader runs in the headless shell. The bench refuses software renderers on the GPU path.
    const browser =
      backend === 'gpu'
        ? await chromium.launch({ headless: true, channel: 'chromium', args }).catch(() => chromium.launch({ headless: true, args }))
        : await chromium.launch({ headless: true, args });
    try {
      const context = await browser.newContext({ viewport: { width: 64, height: 64 }, deviceScaleFactor: 1, serviceWorkers: 'block' });
      const page = await context.newPage();
      const served = new Map<string, Buffer>();
      await context.route('**/*', async (route) => {
        const url = new URL(route.request().url());
        if (url.origin !== ORIGIN) return route.abort();
        if (url.pathname === '/') {
          return route.fulfill({
            status: 200,
            contentType: 'text/html',
            body: '<!doctype html><html><head><meta charset="utf-8"></head><body><canvas id="c" width="1" height="1"></canvas><script src="/snow-globe-renderer.js"></script></body></html>',
          });
        }
        if (url.pathname === '/snow-globe-renderer.js') return route.fulfill({ status: 200, contentType: 'text/javascript', body: bundle });
        const m = /^\/input\/([0-9a-f]+)$/.exec(url.pathname);
        const body = m ? served.get(m[1]!) : undefined;
        if (body) return route.fulfill({ status: 200, contentType: 'application/octet-stream', body });
        return route.fulfill({ status: 404, body: 'not found' });
      });
      await page.goto(`${ORIGIN}/`);
      const glInfo = (await page.evaluate(() => {
        const g = globalThis as unknown as { SnowGlobeRenderer: { SnowGlobeRenderer: new (c: HTMLCanvasElement) => { glInfo(): unknown } }; __snowGlobe?: unknown };
        const r = new g.SnowGlobeRenderer.SnowGlobeRenderer(document.getElementById('c') as HTMLCanvasElement);
        g.__snowGlobe = { renderer: r, cache: new Map() };
        return r.glInfo();
      })) as GlInfo;
      const renderer = String(glInfo.unmaskedRenderer ?? glInfo.renderer);
      if (backend === 'swiftshader' && !/swiftshader/i.test(renderer)) {
        throw new Error(`SwiftShader requested but WebGL reports "${renderer}"`);
      }
      return new HeadlessRenderer(backend, browser, context, page, glInfo, performance.now() - t0, served);
    } catch (e) {
      await browser.close();
      throw e;
    }
  }

  private spec(img: RawImage | null | undefined): InputSpec | null {
    if (!img) return null;
    if (img.data.length !== img.width * img.height * 4) throw new Error('input images must be RGBA');
    const id = inputId(img);
    if (!this.served.has(id)) this.served.set(id, Buffer.from(img.data.buffer, img.data.byteOffset, img.data.byteLength));
    return { id, width: img.width, height: img.height };
  }

  private async run(
    scene: SceneSpec,
    inputs: HeadlessInputs,
    debug: DebugMode,
    read: boolean,
    opts: HeadlessRenderOptions,
  ): Promise<{ timing: PageTiming; pixels: string | null; width: number; height: number }> {
    const arg = {
      scene,
      debug,
      read,
      allowSyntheticSun: opts.allowSyntheticSun === true,
      profile: opts.profile === true,
      sha: inputs.sha256 ?? {},
      plate: this.spec(inputs.plate),
      skyMask: this.spec(inputs.skyMask),
      depth: this.spec(inputs.depth),
    };
    return this.page.evaluate(async (a) => {
      type Img = { width: number; height: number; data: Uint8Array };
      const g = globalThis as unknown as {
        __snowGlobe: {
          renderer: {
            render(s: unknown, i: unknown, o: unknown): { setupMs: number; uploadMs: number; submitMs: number; uploaded: string[]; atmospherePasses: string[]; passMs?: Record<string, number> };
            finish(): void;
            readRGBA(): Uint8Array;
            readFloat(): { width: number; height: number; data: Float32Array };
          };
          cache: Map<string, Img>;
        };
      };
      const st = g.__snowGlobe;
      const get = async (s: { id: string; width: number; height: number } | null): Promise<Img | null> => {
        if (!s) return null;
        const hit = st.cache.get(s.id);
        if (hit) return hit;
        const res = await fetch(`/input/${s.id}`);
        if (!res.ok) throw new Error(`input ${s.id} missing`);
        const img = { width: s.width, height: s.height, data: new Uint8Array(await res.arrayBuffer()) };
        st.cache.set(s.id, img);
        return img;
      };
      const plate = await get(a.plate);
      const skyMask = await get(a.skyMask);
      const depth = await get(a.depth);
      const t0 = performance.now();
      const info = st.renderer.render(
        a.scene,
        { plate, skyMask, depth, plateSha256: a.sha.plate, skyMaskSha256: a.sha.skyMask, depthSha256: a.sha.depth },
        { debug: a.debug, allowSyntheticSun: a.allowSyntheticSun, profile: a.profile },
      );
      st.renderer.finish();
      const t1 = performance.now();
      let pixels: string | null = null;
      let width = 0;
      let height = 0;
      let t2 = t1;
      if (a.read) {
        let bytes: Uint8Array;
        if (a.debug === 'none') {
          bytes = st.renderer.readRGBA();
          width = (a.scene as { camera: { width: number } }).camera.width;
          height = (a.scene as { camera: { height: number } }).camera.height;
        } else {
          const fl = st.renderer.readFloat();
          bytes = new Uint8Array(fl.data.buffer);
          width = fl.width;
          height = fl.height;
        }
        t2 = performance.now();
        let s = '';
        const chunk = 0x8000;
        for (let i = 0; i < bytes.length; i += chunk) s += String.fromCharCode(...bytes.subarray(i, i + chunk));
        pixels = btoa(s);
      }
      const t3 = performance.now();
      return {
        timing: {
          renderMs: t1 - t0,
          atmospherePasses: info.atmospherePasses,
          ...(info.passMs ? { passMs: info.passMs } : {}),
          setupMs: info.setupMs,
          uploadMs: info.uploadMs,
          submitMs: info.submitMs,
          readbackMs: t2 - t1,
          encodeMs: t3 - t2,
          uploaded: info.uploaded,
        },
        pixels,
        width,
        height,
      };
    }, arg);
  }

  /** Renders a frame and returns its RGBA8 pixels (top row first) with page-side timing. */
  async render(scene: SceneSpec, inputs: HeadlessInputs, opts: HeadlessRenderOptions = {}): Promise<HeadlessRenderResult> {
    const r = await this.run(scene, inputs, 'none', true, opts);
    return { width: r.width, height: r.height, rgba: fromBase64(r.pixels!), timing: r.timing };
  }

  /** Renders a debug output (coverage, target, alpha, hdr, ray) as float RGBA (top row first). */
  async renderDebug(
    scene: SceneSpec,
    inputs: HeadlessInputs,
    debug: Exclude<DebugMode, 'none'>,
    opts: HeadlessRenderOptions = {},
  ): Promise<{ width: number; height: number; data: Float32Array; timing: PageTiming }> {
    const r = await this.run(scene, inputs, debug, true, opts);
    const bytes = fromBase64(r.pixels!);
    const copy = new Uint8Array(bytes.length);
    copy.set(bytes);
    return { width: r.width, height: r.height, data: new Float32Array(copy.buffer), timing: r.timing };
  }

  /** Renders without reading pixels back (for timing). */
  async renderTimed(scene: SceneSpec, inputs: HeadlessInputs, opts: HeadlessRenderOptions = {}): Promise<PageTiming> {
    return (await this.run(scene, inputs, 'none', false, opts)).timing;
  }

  /**
   * Reads back the atmosphere tables of the last render (float RGBA, GL row order: row 0 at the bottom),
   * for the GPU-versus-CPU and determinism tests.
   */
  async readAtmosphere(): Promise<AtmosphereReadback> {
    const raw = await this.page.evaluate(() => {
      const g = globalThis as unknown as { __snowGlobe: { renderer: { readAtmosphere(): Record<string, { width: number; height: number; data: Float32Array }> } } };
      const all = g.__snowGlobe.renderer.readAtmosphere();
      const out: Record<string, { width: number; height: number; b64: string }> = {};
      for (const [k, v] of Object.entries(all)) {
        const bytes = new Uint8Array(v.data.buffer, v.data.byteOffset, v.data.byteLength);
        let s = '';
        for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        out[k] = { width: v.width, height: v.height, b64: btoa(s) };
      }
      return out;
    });
    const out = {} as Record<string, FloatImage>;
    for (const [k, v] of Object.entries(raw)) {
      const bytes = fromBase64(v.b64);
      const copy = new Uint8Array(bytes.length);
      copy.set(bytes);
      out[k] = { width: v.width, height: v.height, data: new Float32Array(copy.buffer) };
    }
    return out as unknown as AtmosphereReadback;
  }

  /** Drops the page's input cache and the runner's served inputs. */
  async clearInputs(): Promise<void> {
    this.served.clear();
    await this.page.evaluate(() => (globalThis as unknown as { __snowGlobe: { cache: Map<string, unknown> } }).__snowGlobe.cache.clear());
  }

  async close(): Promise<void> {
    await this.context.close();
    await this.browser.close();
  }
}
