import { describe, expect, it } from 'vitest';
import type { RawImage } from '../cpu/reference.ts';
import { hashWords, unitFromHash } from '../math/noise.ts';
import { pixelDiff, ssim } from './ssim.ts';

function image(w: number, h: number, f: (x: number, y: number) => number): RawImage {
  const data = new Uint8Array(w * h * 4);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const v = Math.max(0, Math.min(255, Math.round(f(x, y))));
      data.set([v, v, v, 255], (y * w + x) * 4);
    }
  }
  return { width: w, height: h, data };
}

const pattern = (x: number, y: number) => 128 + 60 * Math.sin(x / 5) * Math.cos(y / 7);

describe('SSIM (Wang et al. 2004)', () => {
  const a = image(64, 48, pattern);
  it('is exactly 1 for identical images', () => {
    expect(ssim(a, a)).toBe(1);
    expect(pixelDiff(a, a).identical).toBe(true);
  });
  it('is symmetric and decreases with noise', () => {
    const n1 = image(64, 48, (x, y) => pattern(x, y) + 8 * (unitFromHash(hashWords(1, x, y)) - 0.5));
    const n2 = image(64, 48, (x, y) => pattern(x, y) + 40 * (unitFromHash(hashWords(1, x, y)) - 0.5));
    expect(ssim(a, n1)).toBeCloseTo(ssim(n1, a), 12);
    expect(ssim(a, n1)).toBeLessThan(1);
    expect(ssim(a, n2)).toBeLessThan(ssim(a, n1));
    expect(ssim(a, n2)).toBeGreaterThan(0);
  });
  it('a uniform brightness offset costs less than structural damage', () => {
    const offset = image(64, 48, (x, y) => pattern(x, y) + 10);
    const flat = image(64, 48, () => 128);
    expect(ssim(a, offset)).toBeGreaterThan(0.95);
    expect(ssim(a, flat)).toBeLessThan(0.5);
  });
  it('reports pixel differences', () => {
    const b = image(64, 48, (x, y) => (x === 3 && y === 4 ? pattern(x, y) + 7 : pattern(x, y)));
    const d = pixelDiff(a, b);
    expect(d.differingPixels).toBe(1);
    expect(d.maxChannelDiff).toBe(7);
  });
  it('rejects mismatched or tiny images', () => {
    expect(() => ssim(a, image(32, 48, pattern))).toThrow();
    expect(() => ssim(image(8, 8, pattern), image(8, 8, pattern))).toThrow();
  });
});
