/**
 * The overlay's look (PRD §5: near-black panel, hairline rules, mono numerals, colour only in the
 * image) and its accessibility rules (PRD §5.5): text contrast of at least 4.5:1 against everything it
 * can sit on, and 3:1 for the tier marks in every state (styles.test.ts computes both, over a white,
 * black or saturated image), a stronger palette under Increase Contrast, and no animation under
 * Reduce Motion (the view's effective setting, `data-reduce-motion`, so the core's override and the
 * system setting act alike). The panel's height never changes between states or tiers: the caption
 * has a fixed height, sized so the longest caption fits unclipped at the panel's 480 px width, and its
 * folded second line opens over the image, so nothing shifts (PRD §5.5 "no layout shift").
 */

/** Colour tokens; each pair used for text is checked for contrast. */
export const TOKENS = {
  bg: '#0b0d10',
  image: '#000000',
  fg: '#e8eaed',
  dim: '#a6a9ae',
  faint: '#878a90',
  rule: '#2a2d32',
  chipBg: 'rgba(11, 13, 16, 0.84)',
  warnBg: '#e8eaed',
  warnFg: '#0b0d10',
  highlight: '#ffffff',
} as const;

/** Increase Contrast (prefers-contrast: more). */
export const TOKENS_MORE = {
  fg: '#ffffff',
  dim: '#d2d6db',
  faint: '#a0a3a9',
  rule: '#5e6168',
  chipBg: 'rgba(0, 0, 0, 0.94)',
} as const;

/** Cross-fade length for Tier 1 and Tier 2 (PRD §5.5). */
export const FADE_MS = 120;

/**
 * The caption block's fixed height, in steps of the panel's own width (a container query, so it
 * never changes between states or tiers): the longest caption, with every block unclipped, measured
 * at each step's narrowest width in headless Chromium on 22 Nov 2026 (Tier 2, a GFS run, rain, US
 * units, the offline notice and three credit lines: 134.7 px up to 584 px wide, 104.9 px from 592,
 * 89.3 px from 720), plus 3 px for font metrics. The panel is at least 480 px wide (the M1 spike's).
 */
export const CAPTION_STEPS: readonly { readonly fromPx: number; readonly heightPx: number }[] = [
  { fromPx: 480, heightPx: 138 },
  { fromPx: 592, heightPx: 108 },
  { fromPx: 720, heightPx: 92 },
];

/** The caption block's height at the narrowest panel. */
export const CAPTION_PX = CAPTION_STEPS[0]!.heightPx;

export const CSS = `
.sg-overlay {
  --sg-bg: ${TOKENS.bg}; --sg-image: ${TOKENS.image}; --sg-fg: ${TOKENS.fg}; --sg-dim: ${TOKENS.dim};
  --sg-faint: ${TOKENS.faint}; --sg-rule: ${TOKENS.rule}; --sg-chip-bg: ${TOKENS.chipBg};
  --sg-warn-bg: ${TOKENS.warnBg}; --sg-warn-fg: ${TOKENS.warnFg}; --sg-highlight: ${TOKENS.highlight};
  --sg-mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
  position: relative; display: flex; flex-direction: column; width: 100%; box-sizing: border-box;
  background: var(--sg-bg); color: var(--sg-fg); overflow: hidden; container-type: inline-size;
  font: 12px/1.4 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
  font-variant-numeric: tabular-nums lining-nums; -webkit-font-smoothing: antialiased;
  -webkit-user-select: none; user-select: none; cursor: default;
}
@media (prefers-contrast: more) {
  .sg-overlay { --sg-fg: ${TOKENS_MORE.fg}; --sg-dim: ${TOKENS_MORE.dim}; --sg-faint: ${TOKENS_MORE.faint};
    --sg-rule: ${TOKENS_MORE.rule}; --sg-chip-bg: ${TOKENS_MORE.chipBg}; }
}
.sg-overlay[data-phase="idle"] > * { visibility: hidden; }
.sg-image { position: relative; width: 100%; aspect-ratio: 16 / 9; background: var(--sg-image); overflow: hidden; }
.sg-layers { position: absolute; inset: 0; }
.sg-layers canvas { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }
.sg-chips { position: absolute; left: 10px; top: 10px; right: 62px; display: flex; flex-wrap: wrap; gap: 5px; align-items: flex-start; }
.sg-overlay[data-measuring] .sg-chips, .sg-overlay[data-measuring] .sg-marks { top: 18px; }
.sg-chip {
  font: 500 11px/1 var(--sg-mono); letter-spacing: 0; padding: 4px 7px 5px; border: 1px solid var(--sg-rule);
  background: var(--sg-chip-bg); color: var(--sg-fg); border-radius: 3px; white-space: nowrap; margin: 0;
}
.sg-chip[data-emphasis="highlight"] { border-color: var(--sg-highlight); color: var(--sg-highlight); }
.sg-chip[data-emphasis="warning"] { background: var(--sg-warn-bg); color: var(--sg-warn-fg); border-color: var(--sg-warn-bg); }
button.sg-chip { cursor: pointer; }
.sg-chip kbd { font: inherit; opacity: 0.8; margin-left: 5px; padding: 0 3px; border: 1px solid var(--sg-rule); border-radius: 2px; }
.sg-overlay :focus-visible { outline: 2px solid var(--sg-highlight); outline-offset: 1px; }
.sg-marks { position: absolute; right: 10px; top: 10px; display: flex; gap: 4px; padding: 5px; background: var(--sg-chip-bg); border-radius: 3px; }
.sg-mark { width: 7px; height: 7px; box-sizing: border-box; border: 1px solid var(--sg-faint); }
.sg-mark[data-state="shown"], .sg-mark[data-state="skipped"] { background: var(--sg-fg); border-color: var(--sg-fg); }
.sg-mark[data-state="rejected"] { border-color: var(--sg-fg); }
.sg-mark[data-state="off"] { border-style: dashed; }
.sg-pill {
  position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%); max-width: calc(100% - 48px);
  display: none; align-items: center; gap: 9px; padding: 7px 14px 7px 10px; border-radius: 999px;
  background: var(--sg-chip-bg); border: 1px solid var(--sg-rule); box-sizing: border-box;
}
.sg-overlay[data-phase="listening"] .sg-pill, .sg-overlay[data-phase="resolving"] .sg-pill { display: flex; }
.sg-wave { width: 44px; height: 16px; flex: none; }
.sg-partial { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; direction: rtl; text-align: left; min-width: 0; }
.sg-partial > span { direction: ltr; unicode-bidi: isolate; }
.sg-partial[data-stable="false"] { color: var(--sg-dim); }
.sg-dots { display: none; gap: 3px; }
.sg-overlay[data-phase="resolving"] .sg-dots { display: inline-flex; }
.sg-overlay[data-phase="resolving"] .sg-wave { display: none; }
.sg-dots i { width: 4px; height: 4px; border-radius: 50%; background: var(--sg-fg); animation: sg-pulse 900ms infinite ease-in-out; }
.sg-dots i:nth-child(2) { animation-delay: 150ms; } .sg-dots i:nth-child(3) { animation-delay: 300ms; }
@keyframes sg-pulse { 0%, 100% { opacity: 0.25; } 50% { opacity: 1; } }
.sg-message { position: absolute; inset: 0; display: none; flex-direction: column; justify-content: center; align-items: center; gap: 8px; padding: 40px 28px 20px; text-align: center; }
.sg-overlay[data-phase="no_match"] .sg-message, .sg-overlay[data-phase="out_of_range"] .sg-message, .sg-overlay[data-phase="error"] .sg-message,
.sg-overlay[data-exhausted] .sg-message { display: flex; }
.sg-overlay[data-exhausted] .sg-pill { display: none; }
.sg-message h2 { margin: 0; font-size: 14px; font-weight: 600; }
.sg-message p { margin: 0; color: var(--sg-dim); max-width: 46ch; }
.sg-message .sg-heard { color: var(--sg-fg); font-size: 13px; overflow-wrap: anywhere; }
.sg-status { position: absolute; left: 10px; right: 10px; bottom: 10px; display: flex; flex-direction: column; align-items: center; gap: 4px; pointer-events: none; }
.sg-overlay[data-phase="listening"] .sg-status, .sg-overlay[data-phase="resolving"] .sg-status { bottom: 56px; }
.sg-overlay[data-measuring] .sg-status { bottom: 30px; }
.sg-previous, .sg-alert { margin: 0; max-width: 100%; box-sizing: border-box; padding: 4px 8px 5px; border-radius: 3px; font: 500 11px/1.35 var(--sg-mono); text-align: center; }
.sg-previous { display: none; background: var(--sg-chip-bg); color: var(--sg-fg); border: 1px solid var(--sg-rule); visibility: hidden; }
/* A scrubbed frame on its way: say so only if it takes longer than a quick scrub (no flash), with no motion. */
.sg-overlay[data-previous] .sg-previous { display: block; animation: sg-reveal 250ms step-end forwards; }
@keyframes sg-reveal { to { visibility: visible; } }
.sg-alert { background: var(--sg-warn-bg); color: var(--sg-warn-fg); }
.sg-alert:empty { display: none; }
.sg-message ul { list-style: none; margin: 2px 0 0; padding: 0; display: flex; flex-wrap: wrap; justify-content: center; gap: 6px; }
.sg-message button { font: 500 12px/1 var(--sg-mono); color: var(--sg-warn-fg); background: var(--sg-warn-bg); border: 0; border-radius: 3px; padding: 7px 10px; cursor: pointer; }
.sg-measuring { position: absolute; right: 10px; bottom: 10px; display: none; font: 600 10px/1 var(--sg-mono); letter-spacing: 0.08em; padding: 3px 6px; background: var(--sg-warn-bg); color: var(--sg-warn-fg); border-radius: 2px; }
.sg-overlay[data-measuring] .sg-measuring { display: block; }
.sg-caption { position: relative; height: ${CAPTION_PX}px; box-sizing: border-box; padding: 7px 12px 6px; border-top: 1px solid var(--sg-rule); font: 11px/1.42 var(--sg-mono); outline: none; }
${CAPTION_STEPS.slice(1).map((s) => `@container (min-width: ${s.fromPx}px) { .sg-caption { height: ${s.heightPx}px; } }`).join('\n')}
.sg-notice { margin: 0; color: var(--sg-fg); font-weight: 600; }
.sg-notice:empty { display: none; }
.sg-line1 { margin: 0; color: var(--sg-fg); }
.sg-line2 {
  position: absolute; left: 0; right: 0; bottom: 100%; margin: 0; padding: 7px 12px; box-sizing: border-box;
  background: rgba(11, 13, 16, 0.94); border-top: 1px solid var(--sg-rule); color: var(--sg-dim);
  opacity: 0; transition: opacity ${FADE_MS}ms linear; pointer-events: none;
}
.sg-line2:empty { display: none; }
.sg-caption:hover .sg-line2, .sg-caption:focus-within .sg-line2 { opacity: 1; }
.sg-credits { margin: 0; color: var(--sg-dim); font-size: 10px; }
.sg-credits a { color: inherit; text-decoration: underline; text-underline-offset: 2px; cursor: pointer; }
.sg-sr { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
/* Reduce Motion, as the view resolves it (the core's setting, else the system's). */
.sg-overlay[data-reduce-motion="true"] .sg-line2 { transition: none; }
.sg-overlay[data-reduce-motion="true"] .sg-dots i { animation: none; opacity: 0.8; }
`;

/** Parses #rrggbb or rgba(r, g, b, a) into channels 0–255 and alpha 0–1. */
export function parseColour(c: string): [number, number, number, number] {
  const hex = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(c);
  if (hex) return [parseInt(hex[1]!, 16), parseInt(hex[2]!, 16), parseInt(hex[3]!, 16), 1];
  const rgba = /^rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)$/.exec(c);
  if (rgba) return [Number(rgba[1]), Number(rgba[2]), Number(rgba[3]), Number(rgba[4])];
  throw new Error(`unparsed colour ${c}`);
}

/** `top` composited over the opaque colour `under`. */
export function over(top: string, under: string): [number, number, number] {
  const [r, g, b, a] = parseColour(top);
  const [r2, g2, b2] = parseColour(under);
  return [r * a + r2 * (1 - a), g * a + g2 * (1 - a), b * a + b2 * (1 - a)];
}

/** WCAG 2 relative luminance of sRGB channels 0–255. */
export function luminance([r, g, b]: readonly [number, number, number] | readonly number[]): number {
  const lin = (v: number): number => {
    const s = v / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r!) + 0.7152 * lin(g!) + 0.0722 * lin(b!);
}

/** WCAG 2 contrast ratio between two opaque colours. */
export function contrast(a: readonly number[], b: readonly number[]): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
