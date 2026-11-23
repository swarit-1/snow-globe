/**
 * An in-page driver for the DOM view tests (test/view.browser.test.ts): mounts one overlay with a
 * synthetic frame loader, feeds it messages, waits for animation frames, and reads back what is on
 * screen (layout, text, ARIA, animations, pixel tag). Synthetic frames are gradients drawn here; no
 * camera pixels are involved.
 */
import { mountOverlay, type MountedOverlay } from '../overlay/index.ts';
import type { Outbound } from '../overlay/protocol.ts';
import { readTag, CELLS, CELL_PX } from '../overlay/tag.ts';

export interface Snapshot {
  readonly phase: string | null;
  readonly heights: { readonly panel: number; readonly image: number; readonly caption: number };
  readonly chips: { readonly id: string; readonly text: string; readonly label: string; readonly emphasis: string; readonly button: boolean }[];
  readonly marks: string[];
  readonly marksLabel: string;
  readonly pillVisible: boolean;
  readonly partial: string;
  readonly message: string;
  readonly caption: { readonly notice: string; readonly line1: string; readonly line2: string; readonly credits: string };
  readonly line2Opacity: string;
  readonly imageLabel: string;
  readonly live: string;
  readonly layers: { readonly gen: number; readonly tier: number; readonly opacity: string; readonly animations: number; readonly duration: number }[];
  readonly measuringBadge: boolean;
  /** The status line's non-fatal error notice ('' if none) and whether it is displayed. */
  readonly alert: string;
  readonly alertVisible: boolean;
  /** Whether the "previous time" badge is visible now (it appears 250 ms after a scrub). */
  readonly previousVisible: boolean;
  readonly reduceMotion: string | null;
  /** Running animations on the resolving dots, and line 2's transition. */
  readonly dotAnimations: number;
  readonly line2Transition: string;
}

/** How the caption fits its fixed block: every part's overflow past the block's content box, in px (0 when it fits). */
export interface CaptionFit {
  readonly heightPx: number;
  readonly overflowPx: number;
  readonly overflowXPx: number;
  readonly lines: { readonly notice: number; readonly line1: number; readonly credits: number };
}

export class ViewDriver {
  readonly sent: Outbound[] = [];
  readonly overlay: MountedOverlay;
  readonly root: HTMLElement;
  frames = 0;

  constructor(width = 640) {
    this.root = document.createElement('div');
    this.root.style.width = `${width}px`;
    document.body.appendChild(this.root);
    this.overlay = mountOverlay(this.root, {
      send: (m) => this.sent.push(m),
      loader: async (msg) => {
        const w = 160;
        const h = 90;
        const px = new Uint8ClampedArray(w * h * 4);
        for (let y = 0; y < h; y++)
          for (let x = 0; x < w; x++) {
            const sky = y < h * 0.55;
            const t = msg.tier;
            px.set(sky ? [60 + t * 40, 90 + (y * 120) / h, 170 - t * 20, 255] : [40 + x / 4, 60 + t * 25, 45, 255], (y * w + x) * 4);
          }
        const bmp = await createImageBitmap(new ImageData(px, w, h));
        return { source: bmp, width: w, height: h, release: () => bmp.close() };
      },
    });
    const tick = (): void => {
      this.frames += 1;
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  send(msg: unknown): boolean {
    return this.overlay.receive(JSON.stringify(msg));
  }

  async waitFrames(n: number): Promise<void> {
    for (let i = 0; i < n; i++) await new Promise((r) => requestAnimationFrame(r));
  }

  async waitMs(ms: number): Promise<void> {
    await new Promise((r) => setTimeout(r, ms));
  }

  key(key: string, mods: { shiftKey?: boolean; metaKey?: boolean; repeat?: boolean } = {}): void {
    window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, ...mods }));
  }

  /** Removes this driver's overlay (its listeners and frames) from the page. */
  destroy(): void {
    this.overlay.destroy();
    this.root.remove();
  }

  snapshot(): Snapshot {
    const q = <T extends Element>(sel: string): T => this.root.querySelector(sel) as T;
    const panel = q<HTMLElement>('.sg-overlay');
    const caption = q<HTMLElement>('.sg-caption');
    const image = q<HTMLElement>('.sg-image');
    const layers = [...this.root.querySelectorAll<HTMLCanvasElement>('.sg-layers canvas')];
    return {
      phase: panel.getAttribute('data-phase'),
      heights: { panel: panel.getBoundingClientRect().height, image: image.getBoundingClientRect().height, caption: caption.getBoundingClientRect().height },
      chips: [...this.root.querySelectorAll<HTMLElement>('.sg-chips .sg-chip')].map((c) => ({
        id: c.dataset.chip ?? '',
        text: (c.childNodes[0]?.textContent ?? '').trim(),
        label: c.getAttribute('aria-label') ?? '',
        emphasis: c.dataset.emphasis ?? '',
        button: c.tagName === 'BUTTON',
      })),
      marks: [...this.root.querySelectorAll<HTMLElement>('.sg-mark')].map((m) => m.dataset.state ?? ''),
      marksLabel: q<HTMLElement>('.sg-marks').getAttribute('aria-label') ?? '',
      pillVisible: getComputedStyle(q('.sg-pill')).display !== 'none',
      partial: q<HTMLElement>('.sg-partial').textContent ?? '',
      message: q<HTMLElement>('.sg-message').textContent ?? '',
      caption: { notice: q('.sg-notice').textContent ?? '', line1: q('.sg-line1').textContent ?? '', line2: q('.sg-line2').textContent ?? '', credits: q('.sg-credits').textContent ?? '' },
      line2Opacity: getComputedStyle(q('.sg-line2')).opacity,
      imageLabel: q<HTMLElement>('.sg-layers').getAttribute('aria-label') ?? '',
      live: q<HTMLElement>('[aria-live]').textContent ?? '',
      layers: layers.map((c) => {
        const anims = c.getAnimations();
        const timing = anims[0]?.effect?.getComputedTiming();
        return { gen: Number(c.dataset.gen), tier: Number(c.dataset.tier), opacity: getComputedStyle(c).opacity, animations: anims.length, duration: Number(timing?.duration ?? 0) };
      }),
      measuringBadge: getComputedStyle(q('.sg-measuring')).display !== 'none',
      alert: q<HTMLElement>('.sg-alert').textContent ?? '',
      alertVisible: getComputedStyle(q('.sg-alert')).display !== 'none',
      previousVisible: getComputedStyle(q('.sg-previous')).display !== 'none' && getComputedStyle(q('.sg-previous')).visibility === 'visible',
      reduceMotion: panel.getAttribute('data-reduce-motion'),
      dotAnimations: [...this.root.querySelectorAll('.sg-dots i')].reduce((n, e) => n + e.getAnimations().filter((a) => a.playState === 'running').length, 0),
      line2Transition: getComputedStyle(q('.sg-line2')).transitionDuration,
    };
  }

  /** How the caption fits its block now. */
  captionFit(): CaptionFit {
    const cap = this.root.querySelector('.sg-caption') as HTMLElement;
    const cs = getComputedStyle(cap);
    const box = cap.getBoundingClientRect();
    const bottom = box.bottom - parseFloat(cs.paddingBottom);
    const right = box.right - parseFloat(cs.paddingRight);
    let overflowPx = 0;
    let overflowXPx = 0;
    const lines = { notice: 0, line1: 0, credits: 0 };
    for (const [name, sel] of [['notice', '.sg-notice'], ['line1', '.sg-line1'], ['credits', '.sg-credits']] as const) {
      const e = cap.querySelector(sel) as HTMLElement;
      if (getComputedStyle(e).display === 'none') continue;
      const r = e.getBoundingClientRect();
      overflowPx = Math.max(overflowPx, r.bottom - bottom, e.scrollHeight - e.clientHeight);
      overflowXPx = Math.max(overflowXPx, r.right - right, e.scrollWidth - e.clientWidth);
      lines[name] = Math.round(r.height / parseFloat(getComputedStyle(e).lineHeight));
    }
    return { heightPx: box.height, overflowPx: Math.max(0, overflowPx), overflowXPx: Math.max(0, overflowXPx), lines };
  }

  /** Reads the pixel tag back from the top layer's canvas. */
  topTag(): unknown {
    const layers = [...this.root.querySelectorAll<HTMLCanvasElement>('.sg-layers canvas')];
    const c = layers[layers.length - 1];
    if (!c) return null;
    const scale = c.width / c.getBoundingClientRect().width;
    const ctx = c.getContext('2d')!;
    const strip = ctx.getImageData(0, 0, Math.ceil(CELLS * CELL_PX * scale), Math.ceil(CELL_PX * scale));
    return { tag: readTag(strip.data, strip.width, scale), scale };
  }
}

(globalThis as unknown as { SnowGlobeViewDriver: typeof ViewDriver }).SnowGlobeViewDriver = ViewDriver;
