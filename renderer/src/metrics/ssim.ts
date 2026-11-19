/**
 * SSIM (Wang, Bovik, Sheikh & Simoncelli 2004) for golden-image checks (PRD §9.5: SSIM >= 0.98 against
 * the golden). Computed on 8-bit luma (Rec. 601: 0.299 R + 0.587 G + 0.114 B) with the paper's
 * defaults: 11 x 11 Gaussian window, sigma 1.5, K1 = 0.01, K2 = 0.03, L = 255, valid windows only
 * (no padding), averaged over the image.
 */
import type { RawImage } from '../cpu/reference.ts';

const K1 = 0.01;
const K2 = 0.03;
const L = 255;
const C1 = (K1 * L) ** 2;
const C2 = (K2 * L) ** 2;

function gaussianKernel(size = 11, sigma = 1.5): Float64Array {
  const k = new Float64Array(size);
  const c = (size - 1) / 2;
  let s = 0;
  for (let i = 0; i < size; i++) {
    k[i] = Math.exp(-((i - c) ** 2) / (2 * sigma * sigma));
    s += k[i]!;
  }
  for (let i = 0; i < size; i++) k[i]! /= s;
  return k;
}

export function luma(img: RawImage): Float64Array {
  const n = img.width * img.height;
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++) out[i] = 0.299 * img.data[i * 4]! + 0.587 * img.data[i * 4 + 1]! + 0.114 * img.data[i * 4 + 2]!;
  return out;
}

/** Separable 'valid' convolution of a w x h plane with a 1-D kernel in x then y. */
function filterValid(src: Float64Array, w: number, h: number, k: Float64Array): { data: Float64Array; w: number; h: number } {
  const n = k.length;
  const w1 = w - n + 1;
  const h1 = h - n + 1;
  const tmp = new Float64Array(w1 * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w1; x++) {
      let s = 0;
      for (let i = 0; i < n; i++) s += k[i]! * src[y * w + x + i]!;
      tmp[y * w1 + x] = s;
    }
  }
  const out = new Float64Array(w1 * h1);
  for (let y = 0; y < h1; y++) {
    for (let x = 0; x < w1; x++) {
      let s = 0;
      for (let i = 0; i < n; i++) s += k[i]! * tmp[(y + i) * w1 + x]!;
      out[y * w1 + x] = s;
    }
  }
  return { data: out, w: w1, h: h1 };
}

/** Mean SSIM between two same-size images (1 = identical). */
export function ssim(a: RawImage, b: RawImage): number {
  if (a.width !== b.width || a.height !== b.height) throw new Error('ssim: size mismatch');
  if (a.width < 11 || a.height < 11) throw new Error('ssim: images must be at least 11 x 11');
  const x = luma(a);
  const y = luma(b);
  const n = x.length;
  const xx = new Float64Array(n);
  const yy = new Float64Array(n);
  const xy = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    xx[i] = x[i]! * x[i]!;
    yy[i] = y[i]! * y[i]!;
    xy[i] = x[i]! * y[i]!;
  }
  const k = gaussianKernel();
  const mx = filterValid(x, a.width, a.height, k);
  const my = filterValid(y, a.width, a.height, k).data;
  const sxx = filterValid(xx, a.width, a.height, k).data;
  const syy = filterValid(yy, a.width, a.height, k).data;
  const sxy = filterValid(xy, a.width, a.height, k).data;
  let total = 0;
  const m = mx.data.length;
  for (let i = 0; i < m; i++) {
    const ux = mx.data[i]!;
    const uy = my[i]!;
    const vx = sxx[i]! - ux * ux;
    const vy = syy[i]! - uy * uy;
    const cxy = sxy[i]! - ux * uy;
    total += ((2 * ux * uy + C1) * (2 * cxy + C2)) / ((ux * ux + uy * uy + C1) * (vx + vy + C2));
  }
  return total / m;
}

/** Pixel-level difference summary between two RGBA images (alpha ignored). */
export function pixelDiff(a: RawImage, b: RawImage): { identical: boolean; differingPixels: number; maxChannelDiff: number; meanAbsDiff: number } {
  if (a.width !== b.width || a.height !== b.height) throw new Error('pixelDiff: size mismatch');
  let differing = 0;
  let maxD = 0;
  let sum = 0;
  const n = a.width * a.height;
  for (let i = 0; i < n; i++) {
    let px = false;
    for (let c = 0; c < 3; c++) {
      const d = Math.abs(a.data[i * 4 + c]! - b.data[i * 4 + c]!);
      if (d > 0) px = true;
      maxD = Math.max(maxD, d);
      sum += d;
    }
    if (px) differing++;
  }
  return { identical: differing === 0, differingPixels: differing, maxChannelDiff: maxD, meanAbsDiff: sum / (3 * n) };
}
