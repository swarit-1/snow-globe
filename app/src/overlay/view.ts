/**
 * The overlay's DOM (PRD §5.1 anatomy, §5.5 motion and accessibility), framework-free.
 *
 * - **Image area** (16:9): one canvas per tier frame, stacked. Tier 0 and the first frame of a scene
 *   appear at once; Tier 1 and Tier 2 cross-fade over the tier below in 120 ms with the Web Animations
 *   API (compositor-driven, so a busy main thread does not stall it), unless Reduce Motion is on. The
 *   frame is letterboxed, never cropped, so landmarks the caption names stay in view.
 * - **Pixel tag** (PRD §10.4, harness mode only): drawn into each frame's own canvas, so it fades
 *   with the frame and decodes as the new tier from the first composite at ≥ 50 % opacity.
 * - **Chips** top left, **tier marks** top right, the **listening pill** with waveform and partial
 *   transcript, a **status** line at the foot of the image (a non-fatal error; "previous time" while a
 *   scrubbed frame is still on its way), and the **caption** below: real text that VoiceOver reads,
 *   with line 2 folded until hover or keyboard focus. The caption block has a fixed height that holds
 *   the longest caption at the panel's 480 px width unclipped, so no tier shifts the layout.
 * - **Keys** while the panel is focused (PRD §5.4): Esc dismisses; Left and Right scrub an hour (ten
 *   minutes with Shift, and they repeat while held); M flips AM and PM; C copies the frame with its
 *   caption burned in. Plain keys only: with Command, Control or Option held the key belongs to the
 *   system. A held Esc, M or C acts once.
 * - **Reduce Motion** (PRD §5.5): the core's setting if it sent one, else the system's. One effective
 *   value drives the canvas cross-fade and every CSS animation (`data-reduce-motion`).
 *
 * As the paint surface, `commit` and `clear` run synchronously inside the scheduler's frame callback,
 * so the gate check and the draw land in the same composite.
 */
import { SEP, type Caption } from './caption.ts';
import { chipsFor, destinationChip, type Chip } from './chips.ts';
import type { Command, SurfaceSettings } from './controller.ts';
import { formatDateTime } from './format.ts';
import type { PaintKey } from './gate.ts';
import type { CommitRequest, FadeHandle, Prepared, Surface } from './scheduler.ts';
import { exhausted, showingPrevious, type MarkState, type OverlayModel } from './state.ts';
import { CSS, FADE_MS, TOKENS } from './styles.ts';
import { drawTag } from './tag.ts';

export interface ViewOptions {
  readonly root: HTMLElement;
  readonly onCommand?: (c: Command) => void;
  /** Where key presses are heard (the panel's window by default; null for none). */
  readonly keyTarget?: EventTarget | null;
}

interface Layer {
  readonly canvas: HTMLCanvasElement;
  readonly key: PaintKey;
  readonly prepared: Prepared;
  anim: Animation | null;
  /** Taken off screen while its cross-fade was still running. */
  cut: boolean;
}

/** The command for a key press in the focused panel (PRD §5.4), or null if the key is not ours. */
export function keyCommand(key: string, mods: { readonly shift: boolean; readonly meta: boolean; readonly ctrl: boolean; readonly alt: boolean }): Command | null {
  if (mods.meta || mods.ctrl || mods.alt) return null;
  switch (key) {
    case 'Escape':
      return { type: 'dismiss' };
    case 'ArrowLeft':
      return { type: 'scrub', minutes: mods.shift ? -10 : -60 };
    case 'ArrowRight':
      return { type: 'scrub', minutes: mods.shift ? 10 : 60 };
    case 'm':
    case 'M':
      return { type: 'flip_meridiem' };
    case 'c':
    case 'C':
      return { type: 'copy' };
    default:
      return null;
  }
}

/** Only the scrub keys repeat while held; a held Esc, M or C acts once (a held C would encode a PNG per repeat). */
export function repeats(c: Command): boolean {
  return c.type === 'scrub';
}

/** Providers that receive audio, from the destination chip's state ('' if audio stays on device). */
function audioProviders(m: OverlayModel): string {
  return m.destinations.filter((d) => d.carries.includes('audio')).map((d) => d.provider).join(' and ');
}

/**
 * What a fatal error says (the panel stops the query and clears the frame, PRD §5.2). Each line says
 * only what is true of the state the panel is in: after a fatal error nothing is on screen, and audio
 * reached a cloud provider only if cloud speech was on.
 */
export function errorText(code: string, m: OverlayModel): readonly [string, string] {
  switch (code) {
    case 'speech_unavailable': {
      const to = audioProviders(m);
      return ['Speech recognition is unavailable', to ? `Speech could not start. Cloud speech is on, so audio may have reached ${to}.` : 'On-device speech could not start. Nothing was sent anywhere.'];
    }
    case 'microphone_denied':
      return ['Microphone access is off', 'Turn it on in System Settings, Privacy & Security, Microphone.'];
    case 'not_understood':
      return ['Didn’t catch that', 'Try a place and a time, such as “Rainier tomorrow at 4”.'];
    case 'assets_unavailable':
      return ['This viewpoint is not available offline', 'Its plate and frames are not cached yet.'];
    case 'render_failed':
      return ['The frame could not be drawn', 'The query was stopped. Ask again to try once more.'];
    default:
      return ['Something went wrong', 'The query was stopped.'];
  }
}

/**
 * What a non-fatal error says, as a status line over the frame (the query goes on). It names what
 * stays on screen only when that is so.
 */
export function noticeText(code: string, m: OverlayModel): string {
  switch (code) {
    case 'render_failed':
      return m.shownTier === 0
        ? 'The simulated frame could not be drawn. The real photo stays on screen.'
        : m.shownTier === null
          ? 'The simulated frame could not be drawn.'
          : 'A refined frame could not be drawn. The simulation stays on screen.';
    case 'assets_unavailable':
      return 'Not available offline: this viewpoint’s plate and frames are not cached yet.';
    case 'speech_unavailable': {
      const to = audioProviders(m);
      return to ? `Speech had a problem. Cloud speech is on, so audio may have reached ${to}.` : 'On-device speech had a problem. Nothing was sent anywhere.';
    }
    case 'microphone_denied':
      return 'Microphone access is off.';
    case 'not_understood':
      return 'Didn’t catch all of that.';
    default:
      return 'Something went wrong in one stage.';
  }
}

const MARK_WORDS: Readonly<Record<MarkState, string>> = {
  pending: 'pending',
  shown: 'shown',
  skipped: 'ready, a higher tier is shown',
  rejected: 'rejected, late or failed',
  off: 'off',
};
const TIER_WORDS = ['real photo', 'simulation', 'AI texture'];

/** Longest transcript the panel prints in full; longer ones keep their start and end (the message carries up to 10,000). */
export const TRANSCRIPT_SHOWN = 280;

/** `text` cut to about `max` code points, keeping its start and end around an ellipsis. */
export function clipText(text: string, max = TRANSCRIPT_SHOWN): string {
  const cps = [...text];
  if (cps.length <= max) return text;
  const head = Math.ceil((max - 3) * 0.6);
  const tail = max - 3 - head;
  return `${cps.slice(0, head).join('').trimEnd()} … ${cps.slice(cps.length - tail).join('').trimStart()}`;
}

/**
 * Keeps a number with its unit ("2,300 m", "28 h"), and a time with its AM/PM and zone ("4:00 PM PST",
 * "16:00 GMT-8"), on one line: those spaces become no-break spaces. The words are unchanged.
 */
export function glue(text: string): string {
  return text
    .replace(/(\d) (?=[A-Za-z%°µ])/g, '$1 ')
    .replace(/< (?=\d)/g, '< ')
    .replace(/([AP]M) (?=[A-Z]{2,5}\b)/g, '$1 ')
    .replace(/ (?=GMT[+\-−]\d)/g, ' ');
}

/** The caption's first line as sentences, for the image's label and announcements. */
function sentences(c: Caption): string {
  const lead = c.lead.charAt(0) + c.lead.slice(1).toLowerCase().replace('ai texture', 'AI texture');
  const rest = c.line1.split(SEP).slice(1);
  return [...(c.notice ? [c.notice] : []), lead, ...rest].map((s) => (s.endsWith('.') ? s : `${s}.`)).join(' ');
}

function el<K extends keyof HTMLElementTagNameMap>(doc: Document, tag: K, cls?: string, attrs: Record<string, string> = {}): HTMLElementTagNameMap[K] {
  const e = doc.createElement(tag);
  if (cls) e.className = cls;
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

function ensureStyles(doc: Document): void {
  if (doc.getElementById('sg-overlay-css')) return;
  const s = el(doc, 'style', undefined, { id: 'sg-overlay-css' });
  s.textContent = CSS;
  doc.head.appendChild(s);
}

/** The overlay view: renders the model and is the paint surface. */
export class OverlayView implements Surface, SurfaceSettings {
  readonly el: HTMLElement;
  readonly #doc: Document;
  readonly #o: ViewOptions;
  readonly #image: HTMLElement;
  readonly #layersEl: HTMLElement;
  readonly #chips: HTMLElement;
  readonly #marks: HTMLElement;
  readonly #wave: HTMLCanvasElement;
  readonly #partial: HTMLElement;
  readonly #partialText: HTMLElement;
  readonly #message: HTMLElement;
  readonly #previous: HTMLElement;
  readonly #alert: HTMLElement;
  readonly #notice: HTMLElement;
  readonly #line1: HTMLElement;
  readonly #line2: HTMLElement;
  readonly #credits: HTMLElement;
  readonly #live: HTMLElement;
  #layers: Layer[] = [];
  #pool: HTMLCanvasElement[] = [];
  #size = { w: 0, h: 0 };
  #reduceMotionSetting: boolean | null = null;
  #measuring = false;
  #mq: MediaQueryList | null;
  #levels: number[] = [];
  #resize: ResizeObserver | null = null;
  #rendered = new Map<string, string>();

  constructor(o: ViewOptions) {
    this.#o = o;
    const doc = o.root.ownerDocument;
    this.#doc = doc;
    ensureStyles(doc);
    const root = el(doc, 'div', 'sg-overlay', { 'data-phase': 'idle', lang: 'en' });
    this.#image = el(doc, 'div', 'sg-image');
    this.#layersEl = el(doc, 'div', 'sg-layers', { role: 'img', 'aria-label': '' });
    this.#chips = el(doc, 'div', 'sg-chips', { role: 'group', 'aria-label': 'Query' });
    this.#marks = el(doc, 'div', 'sg-marks', { role: 'img' });
    for (let i = 0; i < 3; i++) this.#marks.appendChild(el(doc, 'span', 'sg-mark', { 'data-state': 'pending' }));
    const pill = el(doc, 'div', 'sg-pill');
    this.#wave = el(doc, 'canvas', 'sg-wave', { 'aria-hidden': 'true', width: '88', height: '32' });
    const dots = el(doc, 'span', 'sg-dots', { 'aria-hidden': 'true' });
    for (let i = 0; i < 3; i++) dots.appendChild(el(doc, 'i'));
    this.#partial = el(doc, 'span', 'sg-partial', { 'data-stable': 'false' });
    this.#partialText = el(doc, 'span');
    this.#partial.appendChild(this.#partialText);
    pill.append(this.#wave, dots, this.#partial);
    this.#message = el(doc, 'div', 'sg-message');
    const status = el(doc, 'div', 'sg-status');
    this.#previous = el(doc, 'p', 'sg-previous');
    this.#previous.textContent = 'Previous time · new frame loading';
    this.#alert = el(doc, 'p', 'sg-alert');
    status.append(this.#previous, this.#alert);
    const measuring = el(doc, 'div', 'sg-measuring');
    measuring.textContent = 'MEASURING';
    // TODO(M2): the scrub bar of PRD §5.1 (PLAN.md M2: a bare slider). Left and Right already send `scrub`.
    this.#image.append(this.#layersEl, this.#message, this.#chips, this.#marks, pill, status, measuring);
    const caption = el(doc, 'div', 'sg-caption', { tabindex: '0', 'aria-label': 'Caption' });
    this.#notice = el(doc, 'p', 'sg-notice');
    this.#line1 = el(doc, 'p', 'sg-line1');
    this.#line2 = el(doc, 'p', 'sg-line2');
    this.#credits = el(doc, 'p', 'sg-credits');
    caption.append(this.#notice, this.#line1, this.#line2, this.#credits);
    this.#live = el(doc, 'div', 'sg-sr', { 'aria-live': 'polite', 'aria-atomic': 'true' });
    root.append(this.#image, caption, this.#live);
    o.root.appendChild(root);
    this.el = root;

    const win = doc.defaultView;
    this.#mq = win?.matchMedia?.('(prefers-reduced-motion: reduce)') ?? null;
    this.#mq?.addEventListener?.('change', this.#onMotion);
    this.#applyMotion();
    const r = this.#image.getBoundingClientRect();
    this.#size = { w: r.width, h: r.height };
    if (win && 'ResizeObserver' in win) {
      this.#resize = new win.ResizeObserver((entries) => {
        const box = entries[0]?.contentRect;
        if (box) this.#size = { w: box.width, h: box.height };
      });
      this.#resize.observe(this.#image);
    }
    const keys = o.keyTarget === undefined ? win : o.keyTarget;
    keys?.addEventListener('keydown', this.#onKey as EventListener);
    root.addEventListener('click', this.#onClick);
  }

  /** Removes the overlay and its listeners. */
  destroy(): void {
    this.clear();
    this.#resize?.disconnect();
    this.#mq?.removeEventListener?.('change', this.#onMotion);
    const keys = this.#o.keyTarget === undefined ? this.#doc.defaultView : this.#o.keyTarget;
    keys?.removeEventListener('keydown', this.#onKey as EventListener);
    this.el.remove();
  }

  readonly #onKey = (e: KeyboardEvent): void => {
    const c = keyCommand(e.key, { shift: e.shiftKey, meta: e.metaKey, ctrl: e.ctrlKey, alt: e.altKey });
    if (c === null) return;
    e.preventDefault();
    if (e.repeat && !repeats(c)) return;
    this.#o.onCommand?.(c);
  };

  readonly #onClick = (e: MouseEvent): void => {
    const t = (e.target as Element | null)?.closest('[data-action]');
    if (!t) return;
    const action = t.getAttribute('data-action');
    if (action === 'flip_meridiem') this.#o.onCommand?.({ type: 'flip_meridiem' });
    else if (action === 'use_offer') this.#o.onCommand?.({ type: 'use_offer' });
    else if (action === 'open_url') {
      e.preventDefault(); // the panel never navigates; the core opens the link
      this.#o.onCommand?.({ type: 'open_url', url: t.getAttribute('data-url') ?? '' });
    }
  };

  readonly #onMotion = (): void => this.#applyMotion();

  #applyMotion(): void {
    this.el.setAttribute('data-reduce-motion', String(this.reduceMotion()));
  }

  // ------------------------------------------------------------------------------------------
  // Surface

  configure(s: { readonly reduceMotion: boolean | null; readonly measuring: boolean }): void {
    this.#reduceMotionSetting = s.reduceMotion;
    this.#measuring = s.measuring;
    this.el.toggleAttribute('data-measuring', s.measuring);
    this.#applyMotion();
  }

  /** Reduce Motion: the core's setting if it sent one, else the system's (PRD §5.5). */
  reduceMotion(): boolean {
    return this.#reduceMotionSetting ?? this.#mq?.matches ?? false;
  }

  /** The frames on screen, bottom to top (for the stress harness's surface audit). */
  get layers(): readonly { readonly canvas: HTMLCanvasElement; readonly key: PaintKey; readonly fading: boolean }[] {
    return this.#layers.map((l) => ({ canvas: l.canvas, key: l.key, fading: l.anim !== null }));
  }

  commit(req: CommitRequest): { faded: boolean; fade: FadeHandle | null } {
    const top = this.#layers[this.#layers.length - 1];
    const fade = req.key.tier > 0 && top !== undefined && top.key.gen === req.key.gen && top.key.scene === req.key.scene && !this.reduceMotion();
    const canvas = this.#pool.pop() ?? this.#doc.createElement('canvas');
    canvas.setAttribute('aria-hidden', 'true');
    this.#draw(canvas, req);
    const layer: Layer = { canvas, key: req.key, prepared: req.prepared, anim: null, cut: false };
    this.#layers.push(layer);
    this.#layersEl.appendChild(canvas);
    this.#setCaption(req.caption);
    if (!fade) {
      canvas.style.opacity = '1';
      this.#dropBelow(layer);
      return { faded: false, fade: null };
    }
    canvas.style.opacity = '0';
    const anim = canvas.animate([{ opacity: 0 }, { opacity: 1 }], { duration: FADE_MS, easing: 'linear', fill: 'forwards' });
    layer.anim = anim;
    anim.onfinish = () => {
      if (layer.anim !== anim) return;
      canvas.style.opacity = '1';
      layer.anim = null;
      anim.cancel();
      this.#dropBelow(layer);
    };
    return {
      faded: true,
      fade: {
        progress: () => (layer.anim === anim ? Math.min(1, Number(anim.currentTime ?? 0) / FADE_MS) : layer.cut ? 0 : 1),
        cancelled: () => layer.cut,
      },
    };
  }

  clear(): void {
    for (const l of this.#layers) this.#retire(l);
    this.#layers = [];
    this.#setCaption(null);
  }

  #retire(l: Layer): void {
    if (l.anim) {
      const a = l.anim;
      l.anim = null;
      l.cut = true; // off screen before its fade finished: it never stood at full opacity
      a.cancel();
    }
    l.canvas.remove();
    l.prepared.release();
    if (this.#pool.length < 6) this.#pool.push(l.canvas);
  }

  #dropBelow(layer: Layer): void {
    const i = this.#layers.indexOf(layer);
    if (i <= 0) return;
    for (const l of this.#layers.slice(0, i)) this.#retire(l);
    this.#layers = this.#layers.slice(i);
  }

  #draw(canvas: HTMLCanvasElement, req: CommitRequest): void {
    const dpr = this.#doc.defaultView?.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(this.#size.w * dpr));
    const h = Math.max(1, Math.round(this.#size.h * dpr));
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;
    const ctx = canvas.getContext('2d', { alpha: false });
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = '#000000';
    ctx.fillRect(0, 0, w, h);
    const p = req.prepared;
    if (p.source && p.width > 0 && p.height > 0) {
      const s = Math.min(w / p.width, h / p.height); // letterbox: never crop
      const dw = p.width * s;
      const dh = p.height * s;
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(p.source, (w - dw) / 2, (h - dh) / 2, dw, dh);
    }
    if (this.#measuring) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawTag(ctx, req.key.gen, req.key.tier);
    }
    canvas.dataset.gen = String(req.key.gen);
    canvas.dataset.scene = req.key.scene;
    canvas.dataset.tier = String(req.key.tier);
  }

  #setCaption(c: Caption | null): void {
    this.#notice.textContent = glue(c?.notice ?? '');
    this.#line1.textContent = glue(c?.line1 ?? '');
    this.#line2.textContent = glue(c?.line2 ?? '');
    this.#credits.replaceChildren();
    if (c) {
      c.credits.forEach((cr, i) => {
        if (i > 0) this.#credits.append(' · ');
        if (cr.url) {
          const a = el(this.#doc, 'a', undefined, { href: cr.url, 'data-action': 'open_url', 'data-url': cr.url });
          a.textContent = cr.text;
          this.#credits.append(a);
        } else this.#credits.append(cr.text);
      });
    }
    this.#layersEl.setAttribute('aria-label', c ? sentences(c) : '');
  }

  /** The frame on screen with its caption burned into the pixels (PRD §5.5), as base64 PNG. */
  burnIn(caption: Caption): string | null {
    const top = this.#layers[this.#layers.length - 1];
    const src = top?.prepared.source;
    if (!top || !src) return null;
    const pw = top.prepared.width;
    const ph = top.prepared.height;
    const font = Math.max(12, Math.round(pw / 70));
    const lineH = Math.round(font * 1.5);
    const c = this.#doc.createElement('canvas');
    const ctx0 = c.getContext('2d');
    if (!ctx0) return null;
    ctx0.font = `${font}px ui-monospace, Menlo, monospace`;
    const pad = Math.round(font);
    const lines = [...(caption.notice ? [caption.notice] : []), ...wrap(ctx0, caption.line1, pw - 2 * pad), ...wrap(ctx0, caption.credits.map((x) => x.text).join(SEP), pw - 2 * pad)];
    c.width = pw;
    c.height = ph + pad + lines.length * lineH;
    const ctx = c.getContext('2d')!;
    ctx.drawImage(src, 0, 0, pw, ph);
    ctx.fillStyle = TOKENS.bg;
    ctx.fillRect(0, ph, pw, c.height - ph);
    ctx.font = `${font}px ui-monospace, Menlo, monospace`;
    ctx.textBaseline = 'top';
    lines.forEach((line, i) => {
      ctx.fillStyle = i < lines.length - 1 || lines.length === 1 ? TOKENS.fg : TOKENS.dim;
      ctx.fillText(line, pad, ph + pad / 2 + i * lineH);
    });
    return c.toDataURL('image/png').replace(/^data:image\/png;base64,/, '');
  }

  // ------------------------------------------------------------------------------------------
  // Rendering the model

  #once(key: string, value: string, apply: () => void): void {
    if (this.#rendered.get(key) === value) return;
    this.#rendered.set(key, value);
    apply();
  }

  /** Renders everything but the frames (which the surface draws), touching only what changed. */
  render(m: OverlayModel): void {
    this.#once('phase', m.phase, () => this.el.setAttribute('data-phase', m.phase));
    const empty = exhausted(m) && (m.phase === 'resolving' || m.phase === 'showing');
    this.#once('exhausted', String(empty), () => this.el.toggleAttribute('data-exhausted', empty));
    const previous = showingPrevious(m) && m.phase === 'showing';
    this.#once('previous', String(previous), () => this.el.toggleAttribute('data-previous', previous));
    this.#once('partial', `${m.stable}|${m.transcript}`, () => {
      // The pill shows the end of the transcript; the DOM never holds more than that.
      const cps = [...m.transcript];
      this.#partialText.textContent = cps.length > 400 ? `…${cps.slice(cps.length - 400).join('')}` : m.transcript;
      this.#partial.setAttribute('data-stable', String(m.stable));
    });
    if (m.phase === 'listening') {
      this.#levels.push(m.level);
      if (this.#levels.length > 22) this.#levels.shift();
      this.#drawWave();
    } else if (this.#levels.length > 0 && m.phase === 'idle') this.#levels = [];
    this.#renderChips(m);
    this.#renderMarks(m);
    this.#renderMessage(m, empty);
    const notice = m.error !== null && !m.error.fatal && (m.phase === 'listening' || m.phase === 'resolving' || m.phase === 'showing') ? noticeText(m.error.code, m) : '';
    this.#once('alert', notice, () => {
      this.#alert.textContent = notice;
    });
    const say = announcement(m);
    this.#once('live', say, () => {
      this.#live.textContent = say;
    });
  }

  #renderChips(m: OverlayModel): void {
    const fmt = { locale: m.settings.locale, units: m.settings.units };
    const destination = { destinations: m.destinations, onDeviceOnly: m.settings.onDeviceOnly };
    const active = m.phase !== 'idle';
    // No match and Out of range name their own place and time in the message; the chips keep only the destination.
    const withQuery = m.query !== null && m.display !== null && m.phase !== 'no_match' && m.phase !== 'out_of_range';
    const chips: Chip[] = !active ? [] : withQuery ? chipsFor(m.query!, m.display!, { fmt, destination, online: m.online }) : [destinationChip(destination)];
    this.#once('chips', JSON.stringify(chips), () => {
      this.#chips.replaceChildren(
        ...chips.map((c) => {
          const e = el(this.#doc, c.action ? 'button' : 'span', 'sg-chip', { 'data-chip': c.id, 'data-emphasis': c.emphasis, 'aria-label': c.label, title: c.label });
          if (c.action) {
            e.setAttribute('data-action', c.action);
            e.setAttribute('type', 'button');
          }
          e.append(c.text);
          if (c.key) {
            const k = el(this.#doc, 'kbd', undefined, { 'aria-hidden': 'true' });
            k.textContent = c.key;
            e.append(k);
          }
          return e;
        }),
      );
    });
  }

  #renderMarks(m: OverlayModel): void {
    const visible = m.phase === 'showing' || m.phase === 'resolving' || (m.phase === 'listening' && m.scene !== null);
    const value = visible ? m.marks.join(',') : 'hidden';
    this.#once('marks', value, () => {
      this.#marks.style.visibility = visible ? 'visible' : 'hidden';
      m.marks.forEach((s, i) => this.#marks.children[i]?.setAttribute('data-state', s));
      this.#marks.setAttribute('aria-label', `Tiers: ${m.marks.map((s, i) => `${TIER_WORDS[i]} ${MARK_WORDS[s]}`).join(', ')}.`);
    });
  }

  #renderMessage(m: OverlayModel, empty: boolean): void {
    const doc = this.#doc;
    const providers = audioProviders(m);
    const when = empty && m.query ? formatDateTime(m.query.valid_time, m.query.tz, m.settings.locale) : '';
    const key = `${m.phase}|${empty}|${when}|${JSON.stringify(m.noMatch)}|${JSON.stringify(m.outOfRange)}|${JSON.stringify(m.error)}|${providers}|${m.settings.locale}`;
    this.#once('message', key, () => {
      const parts: HTMLElement[] = [];
      const h2 = (t: string): HTMLElement => {
        const e = el(doc, 'h2');
        e.textContent = t;
        return e;
      };
      const p = (t: string, cls?: string): HTMLElement => {
        const e = el(doc, 'p', cls);
        e.textContent = t;
        return e;
      };
      if (m.phase === 'no_match' && m.noMatch) {
        parts.push(h2('No match'), p(`“${clipText(m.noMatch.transcript)}”`, 'sg-heard'));
        if (m.noMatch.closest.length > 0) {
          const ul = el(doc, 'ul', undefined, { 'aria-label': 'Closest known places' });
          for (const c of m.noMatch.closest) {
            const li = el(doc, 'li', 'sg-chip');
            li.textContent = c.place;
            ul.append(li);
          }
          parts.push(p('Closest known places:'), ul);
        }
      } else if (m.phase === 'out_of_range' && m.outOfRange) {
        const o = m.outOfRange;
        const asked = formatDateTime(o.requested, o.tz, m.settings.locale);
        parts.push(h2('Out of range'), p(o.reason === 'past' ? `${asked} is in the past.` : `${asked} is beyond the 7-day forecast horizon.`));
        if (o.latest_valid) {
          const latest = formatDateTime(o.latest_valid, o.tz, m.settings.locale);
          parts.push(p(`Forecasts for ${o.display.place_name} reach ${latest}.`));
          const b = el(doc, 'button', undefined, { type: 'button', 'data-action': 'use_offer' });
          b.textContent = `Show ${latest}`;
          parts.push(b);
        }
      } else if (m.phase === 'error' && m.error) {
        const [title, more] = errorText(m.error.code, m);
        parts.push(h2(title), p(more));
      } else if (empty) {
        parts.push(h2('Nothing to show for this time'), p(`No frame could be made for ${when}: every tier was rejected or failed.`));
      }
      this.#message.replaceChildren(...parts);
    });
  }

  #drawWave(): void {
    const ctx = this.#wave.getContext('2d');
    if (!ctx) return;
    const { width, height } = this.#wave;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = TOKENS.fg;
    const n = 22;
    const bw = width / n;
    for (let i = 0; i < n; i++) {
      const v = this.#levels[this.#levels.length - n + i] ?? 0;
      const bh = Math.max(2, Math.round(v * height));
      ctx.fillRect(Math.round(i * bw), Math.round((height - bh) / 2), Math.max(1, Math.round(bw * 0.6)), bh);
    }
  }
}

/** What the live region says for a model (PRD §5.5: VoiceOver hears what the panel shows). */
export function announcement(m: OverlayModel): string {
  const notice = m.error !== null && !m.error.fatal ? ` ${noticeText(m.error.code, m)}` : '';
  switch (m.phase) {
    case 'idle':
      return '';
    case 'listening':
      return `Listening.${notice}`;
    case 'resolving':
      if (exhausted(m) && m.query) return `Nothing to show for ${formatDateTime(m.query.valid_time, m.query.tz, m.settings.locale)}.`;
      return `Looking it up.${notice}`;
    case 'showing':
      if (exhausted(m) && m.query) return `Nothing to show for ${formatDateTime(m.query.valid_time, m.query.tz, m.settings.locale)}.`;
      if (showingPrevious(m) && m.query) return `Loading ${formatDateTime(m.query.valid_time, m.query.tz, m.settings.locale)}. The previous time is still on screen.${notice}`;
      return m.caption ? `${sentences(m.caption)}${notice}` : notice.trim();
    case 'no_match':
      return `No match for ${m.noMatch ? clipText(m.noMatch.transcript) : 'that'}.${m.noMatch && m.noMatch.closest.length > 0 ? ` Closest known places: ${m.noMatch.closest.map((c) => c.place).join(', ')}.` : ''}`;
    case 'out_of_range':
      return m.outOfRange?.reason === 'past' ? 'Out of range: that time is in the past.' : 'Out of range: that time is beyond the 7-day horizon.';
    case 'error':
      return `${errorText(m.error?.code ?? 'internal', m)[0]}.`;
  }
}

/** Splits `text` into lines that fit `width` (canvas text, for the burned-in caption). */
function wrap(ctx: CanvasRenderingContext2D, text: string, width: number): string[] {
  if (!text) return [];
  const words = text.split(' ');
  const lines: string[] = [];
  let line = '';
  for (const w of words) {
    const next = line ? `${line} ${w}` : w;
    if (ctx.measureText(next).width > width && line) {
      lines.push(line);
      line = w;
    } else line = next;
  }
  if (line) lines.push(line);
  return lines;
}
