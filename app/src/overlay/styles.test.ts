import { describe, expect, it } from 'vitest';
import { contrast, CSS, over, parseColour, TOKENS, TOKENS_MORE } from './styles.ts';

const rgb = (c: string): [number, number, number] => {
  const [r, g, b] = parseColour(c);
  return [r, g, b];
};
const WHITE = '#ffffff';
const BLACK = '#000000';
const LINE2_BG = 'rgba(11, 13, 16, 0.94)';

describe('contrast (PRD §5.5; WCAG 2.2 AA: 4.5:1 for text, 3:1 for marks)', () => {
  for (const [name, t] of [
    ['default', TOKENS],
    ['increase contrast', { ...TOKENS, ...TOKENS_MORE }],
  ] as const) {
    it(`${name}: caption and message text on the panel`, () => {
      expect(contrast(rgb(t.fg), rgb(t.bg))).toBeGreaterThanOrEqual(4.5);
      expect(contrast(rgb(t.dim), rgb(t.bg))).toBeGreaterThanOrEqual(4.5);
      expect(contrast(rgb(t.dim), rgb(t.image))).toBeGreaterThanOrEqual(4.5);
    });

    it(`${name}: chips stay readable over any image, from pure white to pure black`, () => {
      for (const image of [WHITE, BLACK, '#808080', '#ffcc66']) {
        const chip = over(t.chipBg, image);
        expect(contrast(rgb(t.fg), chip), `fg over ${image}`).toBeGreaterThanOrEqual(4.5);
        expect(contrast(rgb(t.highlight), chip), `highlight over ${image}`).toBeGreaterThanOrEqual(4.5);
        const line2 = over(LINE2_BG, image);
        expect(contrast(rgb(t.dim), line2), `line 2 over ${image}`).toBeGreaterThanOrEqual(4.5);
      }
      expect(contrast(rgb(t.warnFg), rgb(t.warnBg))).toBeGreaterThanOrEqual(4.5);
    });

    it(`${name}: every tier mark state stands out 3:1 as drawn (its composited colour), over any image`, () => {
      // The colour each state's border is drawn in, and the opacity it is drawn at (from the CSS).
      for (const state of ['pending', 'shown', 'skipped', 'rejected', 'off'] as const) {
        const { colour, opacity } = markStyle(state, t);
        for (const image of [WHITE, BLACK, '#808080', '#ffcc66', '#3366ff']) {
          const chip = over(t.chipBg, image);
          const drawn = rgb(colour).map((v, i) => v * opacity + chip[i]! * (1 - opacity));
          expect(contrast(drawn, chip), `${state} over ${image}`).toBeGreaterThanOrEqual(3);
        }
      }
    });
  }

  it('draws no tier mark state translucent (an opacity would lower the contrast the mark test checks)', () => {
    for (const rule of CSS.match(/\.sg-mark[^{]*\{[^}]*\}/g) ?? []) expect(rule, rule).not.toMatch(/opacity/);
  });

  it('uses no colour outside the image (PRD §5: colour only in the image)', () => {
    const colours = [...Object.values(TOKENS), ...Object.values(TOKENS_MORE)];
    for (const c of colours) {
      const [r, g, b] = parseColour(c);
      expect(Math.max(r, g, b) - Math.min(r, g, b), c).toBeLessThanOrEqual(10);
    }
  });

  it('turns every animation off under the effective Reduce Motion, and keeps the caption height fixed', () => {
    expect(CSS).toContain('.sg-overlay[data-reduce-motion="true"] .sg-line2 { transition: none; }');
    expect(CSS).toContain('.sg-overlay[data-reduce-motion="true"] .sg-dots i { animation: none;');
    // One source of truth: no rule keys off the media query alone (the core's override must win).
    expect(CSS).not.toContain('prefers-reduced-motion');
    expect(CSS).toMatch(/\.sg-caption \{ position: relative; height: \d+px;/);
    expect(CSS).toMatch(/aspect-ratio: 16 \/ 9/);
    // Nothing in the caption block is clipped or clamped: the block is sized for the longest caption.
    for (const [, selectors, body] of CSS.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
      if (!/\.sg-(caption|notice|line1|credits)\b/.test(selectors!)) continue;
      expect(body, selectors).not.toMatch(/line-clamp|text-overflow|overflow:\s*hidden|white-space:\s*nowrap/);
    }
  });
});

/**
 * The colour and opacity a tier mark state is drawn with, read from the CSS itself: the base
 * `.sg-mark` border, overridden by any `[data-state=…]` rule's border colour and opacity.
 */
function markStyle(state: 'pending' | 'shown' | 'skipped' | 'rejected' | 'off', t: Record<string, string>): { colour: string; opacity: number } {
  const token = (v: string): string => {
    const name = /var\(--sg-([a-z-]+)\)/.exec(v)?.[1] ?? '';
    const key = name.replace(/-([a-z])/g, (_, c: string) => c.toUpperCase());
    const value = t[key];
    if (value === undefined) throw new Error(`no token for ${v}`);
    return value;
  };
  let colour = '';
  let opacity = 1;
  for (const [, selectors, body] of CSS.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
    const sel = selectors!.split(',').map((s) => s.trim());
    const base = sel.includes('.sg-mark');
    const mine = sel.includes(`.sg-mark[data-state="${state}"]`);
    if (!base && !mine) continue;
    const border = /border(?:-color)?:\s*[^;]*?(var\(--sg-[a-z-]+\))/.exec(body!)?.[1];
    if (border) colour = token(border);
    const op = /opacity:\s*([\d.]+)/.exec(body!)?.[1];
    if (op && mine) opacity = Number(op);
  }
  if (!colour) throw new Error(`no border colour for ${state}`);
  return { colour, opacity };
}
