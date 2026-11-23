/**
 * The in-surface pixel tag (PRD §10.4: a pixel tag carrying (query_gen, tier) is drawn inside the same
 * surface as the content), bit for bit the layout of the M1 spike's decoder
 * (spikes/m1-platform/common/src/tag.rs), so its ScreenCaptureKit reader decodes the overlay's frames:
 *
 * | cells  | meaning                                   |
 * |--------|-------------------------------------------|
 * | 0..4   | sync pattern 1 0 1 0                      |
 * | 4..16  | query_gen, 12 bits, most significant first |
 * | 16..18 | tier, 2 bits                              |
 * | 18..23 | CRC-5 (x^5 + x^2 + 1) over the 14 data bits |
 * | 23..25 | end pattern 0 1                           |
 *
 * 25 square cells of 8 CSS px at the top-left corner of the frame, pure white (1) or black (0).
 *
 * Unlike the spike, which keeps its tag opaque, the overlay draws the tag into each tier's own layer,
 * so it fades with the tier. Each cell that differs between two tags is then a blend of black and
 * white, and reads as the new tag once the new layer passes 50 % opacity: the tag decodes as the new
 * tier in exactly the frames the PRD counts as painted ("the first composited frame in which the tier
 * is at ≥ 50 % opacity").
 */

/** Number of cells. */
export const CELLS = 25;
/** Cell edge in CSS px. */
export const CELL_PX = 8;
/** Largest query_gen the tag carries (12 bits); larger values wrap. */
export const GEN_MASK = 0x0fff;

const SYNC = [true, false, true, false] as const;
const END = [false, true] as const;

/** CRC-5 with polynomial x^5 + x^2 + 1 over `nbits` bits of `data`, most significant first. */
export function crc5(data: number, nbits: number): number {
  let crc = 0;
  for (let i = nbits - 1; i >= 0; i--) {
    const bit = (data >>> i) & 1;
    const top = (crc >>> 4) & 1;
    crc = (crc << 1) & 0x1f;
    if ((top ^ bit) === 1) crc ^= 0x05;
  }
  return crc;
}

/** The 25 cells for (gen, tier); gen wraps at 12 bits and tier at 2. */
export function tagCells(gen: number, tier: number): boolean[] {
  const data = ((gen & GEN_MASK) << 2) | (tier & 0x03);
  const crc = crc5(data, 14);
  const out: boolean[] = [...SYNC];
  for (let i = 0; i < 14; i++) out.push(((data >>> (13 - i)) & 1) === 1);
  for (let i = 0; i < 5; i++) out.push(((crc >>> (4 - i)) & 1) === 1);
  out.push(...END);
  return out;
}

/** A decoded tag. */
export interface Tag {
  readonly gen: number;
  readonly tier: number;
}

/** Why cells did not decode. */
export type TagError = 'no_sync' | 'bad_crc';

/** Decodes 25 cells, checking the sync and end patterns and the CRC. */
export function fromCells(c: readonly boolean[]): Tag | TagError {
  if (c.length !== CELLS) return 'no_sync';
  for (let i = 0; i < 4; i++) if (c[i] !== SYNC[i]) return 'no_sync';
  if (c[23] !== END[0] || c[24] !== END[1]) return 'no_sync';
  let data = 0;
  for (let i = 4; i < 18; i++) data = (data << 1) | (c[i] ? 1 : 0);
  let crc = 0;
  for (let i = 18; i < 23; i++) crc = (crc << 1) | (c[i] ? 1 : 0);
  if (crc5(data, 14) !== crc) return 'bad_crc';
  return { gen: data >>> 2, tier: data & 0x03 };
}

/** Draws the tag for (gen, tier) at the top-left of a 2D context whose transform is in CSS px. */
export function drawTag(ctx: CanvasRenderingContext2D, gen: number, tier: number): void {
  const cells = tagCells(gen, tier);
  for (let i = 0; i < CELLS; i++) {
    ctx.fillStyle = cells[i] ? '#ffffff' : '#000000';
    ctx.fillRect(i * CELL_PX, 0, CELL_PX, CELL_PX);
  }
}

/**
 * Reads the tag back from RGBA pixels (the top strip of a canvas), sampling each cell's centre at
 * `scale` device pixels per CSS px with a 50 % threshold on the mean of R, G and B, as the spike does.
 */
export function readTag(rgba: Uint8ClampedArray, width: number, scale: number): Tag | TagError {
  const y = Math.floor((CELL_PX / 2) * scale);
  const cells: boolean[] = [];
  for (let i = 0; i < CELLS; i++) {
    const x = Math.floor((i + 0.5) * CELL_PX * scale);
    const p = (y * width + x) * 4;
    cells.push((rgba[p]! + rgba[p + 1]! + rgba[p + 2]!) / 3 >= 128);
  }
  return fromCells(cells);
}
