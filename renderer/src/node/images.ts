/**
 * Node-side image I/O for the headless runner. Inputs are decoded in Node (pngjs, jpeg-js) and passed to
 * the page as raw RGBA, so browser decoders and colour management never touch them; outputs are encoded
 * with fixed PNG settings, so the same pixels always give the same bytes (PRD §9.5 determinism).
 */
import { readFileSync } from 'node:fs';
import jpeg from 'jpeg-js';
import { PNG } from 'pngjs';
import type { RawImage } from '../cpu/reference.ts';

export function decodePng(buf: Uint8Array): RawImage {
  const png = PNG.sync.read(Buffer.from(buf.buffer, buf.byteOffset, buf.byteLength));
  return { width: png.width, height: png.height, data: new Uint8Array(png.data.buffer, png.data.byteOffset, png.data.byteLength) };
}

export function decodeJpeg(buf: Uint8Array): RawImage {
  const img = jpeg.decode(Buffer.from(buf.buffer, buf.byteOffset, buf.byteLength), { useTArray: true, formatAsRGBA: true, maxMemoryUsageInMB: 2048 });
  return { width: img.width, height: img.height, data: new Uint8Array(img.data.buffer, img.data.byteOffset, img.data.byteLength) };
}

/** Decodes a PNG or JPEG by its magic bytes. */
export function decodeImage(buf: Uint8Array): RawImage {
  if (buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) return decodePng(buf);
  if (buf[0] === 0xff && buf[1] === 0xd8) return decodeJpeg(buf);
  throw new Error('decodeImage: only PNG and JPEG are supported');
}

export function readImageFile(path: string): { image: RawImage; bytes: Uint8Array } {
  const bytes = new Uint8Array(readFileSync(path));
  return { image: decodeImage(bytes), bytes };
}

/** Encodes RGBA pixels as an 8-bit RGB PNG (alpha dropped) with fixed settings. */
export function encodePngRGB(img: RawImage): Buffer {
  const png = new PNG({ width: img.width, height: img.height, colorType: 2, inputColorType: 6, bitDepth: 8 });
  png.data = Buffer.from(img.data.buffer, img.data.byteOffset, img.data.byteLength);
  return PNG.sync.write(png, { colorType: 2, inputColorType: 6, bitDepth: 8, deflateLevel: 9, deflateStrategy: 3, filterType: -1 });
}

/** Encodes RGBA pixels as an 8-bit greyscale PNG from the red channel (for masks). */
export function encodePngGray(img: RawImage): Buffer {
  const png = new PNG({ width: img.width, height: img.height, colorType: 0, inputColorType: 6, bitDepth: 8 });
  png.data = Buffer.from(img.data.buffer, img.data.byteOffset, img.data.byteLength);
  return PNG.sync.write(png, { colorType: 0, inputColorType: 6, bitDepth: 8, deflateLevel: 9, deflateStrategy: 3, filterType: -1 });
}

/** Resamples an RGBA image (nearest neighbour, or bilinear in sRGB space). Used only by the benchmark. */
export function resample(img: RawImage, width: number, height: number, mode: 'nearest' | 'bilinear'): RawImage {
  const out = new Uint8Array(width * height * 4);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const sx = ((x + 0.5) * img.width) / width - 0.5;
      const sy = ((y + 0.5) * img.height) / height - 0.5;
      const o = (y * width + x) * 4;
      if (mode === 'nearest') {
        const ix = Math.min(img.width - 1, Math.max(0, Math.round(sx)));
        const iy = Math.min(img.height - 1, Math.max(0, Math.round(sy)));
        for (let c = 0; c < 4; c++) out[o + c] = img.data[(iy * img.width + ix) * 4 + c]!;
      } else {
        const x0 = Math.max(0, Math.floor(sx));
        const y0 = Math.max(0, Math.floor(sy));
        const x1 = Math.min(img.width - 1, x0 + 1);
        const y1 = Math.min(img.height - 1, y0 + 1);
        const fx = Math.min(1, Math.max(0, sx - x0));
        const fy = Math.min(1, Math.max(0, sy - y0));
        for (let c = 0; c < 4; c++) {
          const p = (xx: number, yy: number) => img.data[(yy * img.width + xx) * 4 + c]!;
          const v = (1 - fy) * ((1 - fx) * p(x0, y0) + fx * p(x1, y0)) + fy * ((1 - fx) * p(x0, y1) + fx * p(x1, y1));
          out[o + c] = Math.round(v);
        }
      }
    }
  }
  return { width, height, data: out };
}
