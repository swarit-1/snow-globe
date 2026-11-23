import { describe, expect, it } from 'vitest';
import { CELLS, fromCells, GEN_MASK, readTag, tagCells, type Tag } from './tag.ts';

const tag = (gen: number, tier: number): Tag => ({ gen: gen & GEN_MASK, tier });

/** The spike's torn-strip count: strips cut between tag a (left) and b (right) that differ from both, and those decoding as a third tag. */
function torn(pairs: Iterable<[Tag, Tag]>): [number, number] {
  let total = 0;
  let wrong = 0;
  for (const [ta, tb] of pairs) {
    const a = tagCells(ta.gen, ta.tier);
    const b = tagCells(tb.gen, tb.tier);
    for (let cut = 1; cut < CELLS; cut++) {
      const t = [...a.slice(0, cut), ...b.slice(cut)];
      const same = (x: boolean[]): boolean => x.every((v, i) => v === t[i]);
      if (same(a) || same(b)) continue;
      total += 1;
      const d = fromCells(t);
      if (typeof d !== 'string' && !(d.gen === ta.gen && d.tier === ta.tier) && !(d.gen === tb.gen && d.tier === tb.tier)) wrong += 1;
    }
  }
  return [total, wrong];
}

describe('pixel tag, the layout of spikes/m1-platform/common/src/tag.rs (PRD §10.4)', () => {
  it('round-trips every payload, and wraps at 12 bits', () => {
    for (const g of [0, 1, 2, 7, 255, 256, 1234, 4095]) for (let t = 0; t < 4; t++) expect(fromCells(tagCells(g, t))).toEqual({ gen: g, tier: t });
    expect(fromCells(tagCells(4096 + 5, 1))).toEqual({ gen: 5, tier: 1 });
  });

  it('catches every single and double cell error', () => {
    const good = tagCells(0x0a5c, 2);
    for (let i = 4; i < 23; i++) {
      const c = [...good];
      c[i] = !c[i];
      expect(typeof fromCells(c)).toBe('string');
      for (let j = i + 1; j < 23; j++) {
        const d = [...c];
        d[j] = !d[j];
        expect(typeof fromCells(d)).toBe('string');
      }
    }
    const noSync = [...good];
    noSync[0] = false;
    expect(fromCells(noSync)).toBe('no_sync');
  });

  it('reproduces the spike’s exhaustive torn-strip counts exactly (so the encoders are the same)', () => {
    const consecutive = torn(
      (function* () {
        for (let g = 0; g <= GEN_MASK; g++) {
          const n = (g + 1) & GEN_MASK;
          yield [tag(g, g % 3), tag(n, n % 3)] as [Tag, Tag];
        }
      })(),
    );
    const reveal = torn(
      (function* () {
        for (let g = 0; g <= GEN_MASK; g++) yield [tag(g, 0), tag(g + 1, 0)] as [Tag, Tag];
      })(),
    );
    const skipped = torn(
      (function* () {
        for (let g = 0; g <= GEN_MASK; g++)
          for (let d = 2; d <= 16; d++) {
            const n = (g + d) & GEN_MASK;
            yield [tag(g, g % 3), tag(n, n % 3)] as [Tag, Tag];
          }
      })(),
    );
    expect(consecutive).toEqual([29_930, 0]);
    expect(reveal).toEqual([28_392, 0]);
    expect(skipped).toEqual([612_776, 890]);
  });

  it('decodes a blend of two tags as the new one once it passes 50 % opacity, the PRD paint rule', () => {
    const scale = 2;
    const width = CELLS * 8 * scale;
    const strip = (g: number, t: number): Uint8ClampedArray => {
      const px = new Uint8ClampedArray(width * 8 * scale * 4);
      const cells = tagCells(g, t);
      for (let y = 0; y < 8 * scale; y++)
        for (let x = 0; x < width; x++) {
          const v = cells[Math.floor(x / (8 * scale))] ? 255 : 0;
          px.set([v, v, v, 255], (y * width + x) * 4);
        }
      return px;
    };
    const a = strip(41, 0);
    const b = strip(41, 1);
    const blend = (alpha: number): Uint8ClampedArray => a.map((v, i) => Math.round(alpha * b[i]! + (1 - alpha) * v));
    expect(readTag(blend(0.3), width, scale)).toEqual({ gen: 41, tier: 0 });
    expect(readTag(blend(0.49), width, scale)).toEqual({ gen: 41, tier: 0 });
    expect(readTag(blend(0.51), width, scale)).toEqual({ gen: 41, tier: 1 });
    expect(readTag(blend(1), width, scale)).toEqual({ gen: 41, tier: 1 });
  });
});
