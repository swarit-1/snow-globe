//! The in-surface pixel tag (PRD §10.4 "Tag": a pixel tag carrying (query_gen, tier) is drawn
//! inside the same surface as the content; PLAN.md M1 1a check 8).
//!
//! The webview draws a strip of 25 square cells, 8 CSS points each, at the top-left corner of the
//! panel's content. Each cell is pure white (1) or pure black (0):
//!
//! | cells | meaning |
//! |---|---|
//! | 0..4 | sync pattern 1 0 1 0 |
//! | 4..16 | query_gen, 12 bits, most significant first |
//! | 16..18 | tier, 2 bits |
//! | 18..23 | CRC-5 (x^5 + x^2 + 1) over the 14 data bits |
//! | 23..25 | end pattern 0 1 |
//!
//! The polynomial is primitive, so the 19-bit codeword detects every single- and double-cell
//! error (a CRC-4 would not at this length). Torn redraws between consecutive tags never decode
//! as a third tag; between tags further apart, 0.15 % do (measured exhaustively in the tests).
//!
//! Rust computes the cells ([`cells`]) and the page only paints them, so the encoder exists once.
//! The ScreenCaptureKit helper samples the centre of each cell in the captured BGRA frame
//! ([`decode_bgra`]). Two levels and a 50 % threshold survive colour management and scaling.

use serde::{Deserialize, Serialize};

/// Number of cells in the strip.
pub const CELLS: usize = 25;
/// Cell edge in CSS points.
pub const CELL_PT: f64 = 8.0;
/// Distance of the strip from the content's top-left corner, in CSS points.
pub const ORIGIN_PT: (f64, f64) = (0.0, 0.0);
/// Largest query_gen the tag carries (12 bits); larger values wrap.
pub const GEN_MASK: u16 = 0x0fff;

const SYNC: [bool; 4] = [true, false, true, false];
const END: [bool; 2] = [false, true];

/// The payload: which query generation and which tier the frame shows.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Tag {
    pub gen_: u16,
    pub tier: u8,
}

/// Why a frame did not yield a tag.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub enum TagError {
    /// The frame is smaller than the strip at the given scale.
    TooSmall,
    /// The sync or end pattern is wrong: no tag here, or the frame is torn.
    NoSync,
    /// Sync is right but the CRC is not: a corrupted or half-drawn tag.
    BadCrc,
}

/// CRC-5 with polynomial x^5 + x^2 + 1 over `nbits` bits of `data`, most significant first.
pub fn crc5(data: u32, nbits: u32) -> u8 {
    let mut crc: u8 = 0;
    for i in (0..nbits).rev() {
        let bit = ((data >> i) & 1) as u8;
        let top = (crc >> 4) & 1;
        crc = (crc << 1) & 0x1f;
        if top ^ bit == 1 {
            crc ^= 0x05;
        }
    }
    crc
}

fn payload(gen_: u16, tier: u8) -> u32 {
    (u32::from(gen_ & GEN_MASK) << 2) | u32::from(tier & 0x03)
}

/// The 25 cells for (gen, tier). `gen_` wraps at 12 bits and `tier` at 2 bits.
pub fn cells(gen_: u16, tier: u8) -> [bool; CELLS] {
    let data = payload(gen_, tier);
    let crc = crc5(data, 14);
    let mut out = [false; CELLS];
    out[..4].copy_from_slice(&SYNC);
    for i in 0..14 {
        out[4 + i] = (data >> (13 - i)) & 1 == 1;
    }
    for i in 0..5 {
        out[18 + i] = (crc >> (4 - i)) & 1 == 1;
    }
    out[23..].copy_from_slice(&END);
    out
}

/// Decodes cells back to a tag, checking sync, end pattern and CRC.
pub fn from_cells(c: &[bool; CELLS]) -> Result<Tag, TagError> {
    if c[..4] != SYNC || c[23..] != END {
        return Err(TagError::NoSync);
    }
    let mut data = 0u32;
    for &b in &c[4..18] {
        data = (data << 1) | u32::from(b);
    }
    let mut crc = 0u8;
    for &b in &c[18..23] {
        crc = (crc << 1) | u8::from(b);
    }
    if crc5(data, 14) != crc {
        return Err(TagError::BadCrc);
    }
    Ok(Tag {
        gen_: (data >> 2) as u16,
        tier: (data & 0x03) as u8,
    })
}

/// A borrowed BGRA frame (8 bits per channel, rows `bytes_per_row` apart).
#[derive(Clone, Copy, Debug)]
pub struct FrameView<'a> {
    pub data: &'a [u8],
    pub width: usize,
    pub height: usize,
    pub bytes_per_row: usize,
}

impl FrameView<'_> {
    /// Mean of R, G and B at (x, y), or `None` outside the frame.
    pub fn luma(&self, x: usize, y: usize) -> Option<u8> {
        if x >= self.width || y >= self.height {
            return None;
        }
        let i = y
            .checked_mul(self.bytes_per_row)?
            .checked_add(x.checked_mul(4)?)?;
        let px = self.data.get(i..i + 4)?;
        let (b, g, r) = (u16::from(px[0]), u16::from(px[1]), u16::from(px[2]));
        Some(((b + g + r) / 3) as u8)
    }
}

/// Samples the cell centres at `scale` pixels per point, offset by `(dx, dy)` pixels.
fn sample(frame: &FrameView<'_>, scale: f64, dx: f64, dy: f64) -> Result<[bool; CELLS], TagError> {
    let mut out = [false; CELLS];
    let y = ((ORIGIN_PT.1 + CELL_PT / 2.0) * scale + dy).floor();
    if y < 0.0 {
        return Err(TagError::TooSmall);
    }
    for (i, cell) in out.iter_mut().enumerate() {
        let x = ((ORIGIN_PT.0 + (i as f64 + 0.5) * CELL_PT) * scale + dx).floor();
        if x < 0.0 {
            return Err(TagError::TooSmall);
        }
        let l = frame
            .luma(x as usize, y as usize)
            .ok_or(TagError::TooSmall)?;
        *cell = l >= 128;
    }
    Ok(out)
}

/// Decodes the tag from a captured frame. `scale` is the frame's pixels per CSS point (2 on a
/// Retina panel captured at native size). The nominal position is tried first, then 1 and 2
/// pixel offsets and the scales 2 and 1, so a shadow inset or a rounding difference does not lose
/// the tag. Returns the first decode that passes sync and CRC.
pub fn decode_bgra(frame: &FrameView<'_>, scale: f64) -> Result<Tag, TagError> {
    let mut best = TagError::TooSmall;
    let mut scales = vec![scale];
    for s in [2.0, 1.0] {
        if (s - scale).abs() > 1e-6 {
            scales.push(s);
        }
    }
    for s in scales {
        if !(s.is_finite() && s > 0.0) {
            continue;
        }
        for (dx, dy) in [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (1.0, 0.0), (0.0, 1.0)] {
            match sample(frame, s, dx, dy).and_then(|c| from_cells(&c)) {
                Ok(t) => return Ok(t),
                Err(e) => best = worst_to_best(best, e),
            }
        }
    }
    Err(best)
}

fn worst_to_best(a: TagError, b: TagError) -> TagError {
    fn rank(e: TagError) -> u8 {
        match e {
            TagError::TooSmall => 0,
            TagError::NoSync => 1,
            TagError::BadCrc => 2,
        }
    }
    if rank(b) > rank(a) { b } else { a }
}

/// Renders cells into a BGRA buffer the way the page paints them (for tests and for a
/// self-check in `m1-sck`). `lo`/`hi` are the dark and light levels; pixels outside the strip get
/// `background`.
#[allow(clippy::too_many_arguments)] // a test and self-check helper; the arguments are the frame
pub fn render_bgra(
    c: &[bool; CELLS],
    scale: f64,
    width: usize,
    height: usize,
    bytes_per_row: usize,
    lo: u8,
    hi: u8,
    background: u8,
) -> Vec<u8> {
    let mut buf = vec![0u8; bytes_per_row * height];
    for y in 0..height {
        for x in 0..width {
            let xp = x as f64 / scale - ORIGIN_PT.0;
            let yp = y as f64 / scale - ORIGIN_PT.1;
            let v = if (0.0..CELL_PT).contains(&yp) && xp >= 0.0 && xp < CELL_PT * CELLS as f64 {
                let cell = (xp / CELL_PT) as usize;
                if c[cell] { hi } else { lo }
            } else {
                background
            };
            let i = y * bytes_per_row + x * 4;
            buf[i..i + 4].copy_from_slice(&[v, v, v, 255]);
        }
    }
    buf
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_payload_round_trips_through_cells() {
        for gen_ in [0u16, 1, 2, 7, 255, 256, 1234, 4095] {
            for tier in 0..4u8 {
                let c = cells(gen_, tier);
                assert_eq!(from_cells(&c), Ok(Tag { gen_, tier }));
            }
        }
        // Wraps at 12 bits.
        assert_eq!(
            from_cells(&cells(4096 + 5, 1)),
            Ok(Tag { gen_: 5, tier: 1 })
        );
    }

    #[test]
    fn crc_catches_every_single_and_double_bit_error() {
        let good = cells(0x0a5c, 2);
        for i in 4..23 {
            let mut c = good;
            c[i] = !c[i];
            assert!(from_cells(&c).is_err(), "single flip at {i} not caught");
            for j in (i + 1)..23 {
                let mut d = c;
                d[j] = !d[j];
                assert!(from_cells(&d).is_err(), "double flip at {i},{j} not caught");
            }
        }
        let mut no_sync = good;
        no_sync[0] = false;
        assert_eq!(from_cells(&no_sync), Err(TagError::NoSync));
    }

    #[test]
    fn decodes_rendered_frames_at_retina_and_1x_with_soft_levels() {
        for (scale, w, h) in [(2.0, 960usize, 600usize), (1.0, 480, 300)] {
            let bpr = w * 4 + 64; // padded rows, as CVPixelBuffer often has
            let c = cells(321, 1);
            let buf = render_bgra(&c, scale, w, h, bpr, 18, 236, 40);
            let f = FrameView {
                data: &buf,
                width: w,
                height: h,
                bytes_per_row: bpr,
            };
            assert_eq!(decode_bgra(&f, scale), Ok(Tag { gen_: 321, tier: 1 }));
        }
    }

    #[test]
    fn decodes_when_the_scale_hint_is_wrong_and_reports_missing_tags() {
        let c = cells(9, 3);
        let buf = render_bgra(&c, 2.0, 600, 40, 2400, 0, 255, 0);
        let f = FrameView {
            data: &buf,
            width: 600,
            height: 40,
            bytes_per_row: 2400,
        };
        assert_eq!(decode_bgra(&f, 1.37), Ok(Tag { gen_: 9, tier: 3 }));
        let blank = vec![0u8; 2400 * 40];
        let fb = FrameView {
            data: &blank,
            width: 600,
            height: 40,
            bytes_per_row: 2400,
        };
        assert_eq!(decode_bgra(&fb, 2.0), Err(TagError::NoSync));
        let tiny = FrameView {
            data: &blank[..64],
            width: 4,
            height: 4,
            bytes_per_row: 16,
        };
        assert_eq!(decode_bgra(&tiny, 2.0), Err(TagError::TooSmall));
    }

    /// Every strip whose left part is tag `a` and right part tag `b` (a redraw torn at any cell
    /// boundary): (strips that differ from both, strips that decode as a third tag).
    fn torn(pairs: impl Iterator<Item = (Tag, Tag)>) -> (u32, u32) {
        let (mut total, mut wrong) = (0u32, 0u32);
        for (ta, tb) in pairs {
            let (a, b) = (cells(ta.gen_, ta.tier), cells(tb.gen_, tb.tier));
            for cut in 1..CELLS {
                let mut t = a;
                t[cut..].copy_from_slice(&b[cut..]);
                if t == a || t == b {
                    continue;
                }
                total += 1;
                if from_cells(&t).is_ok_and(|d| d != ta && d != tb) {
                    wrong += 1;
                }
            }
        }
        (total, wrong)
    }

    /// Torn redraws, measured exhaustively (19 Nov 2026), cut at every cell boundary:
    /// - every pair of consecutive tags as `m1-spike tags` draws them (gen g then g+1, wrapping
    ///   at 4095, tier = gen % 3): 0 of 29,930 torn strips decode as a third tag;
    /// - the same as `reveal` draws them (tier 0): 0 of 28,392;
    /// - tags 2 to 16 generations apart (redraws the capture skipped): 890 of 612,776 (0.15 %)
    ///   decode as a third tag. That is why the report also fails on any decoded tag the app
    ///   never drew.
    #[test]
    fn torn_strips_between_consecutive_tags_never_decode_as_a_third_tag() {
        let tag = |g: u16, tier: u8| Tag {
            gen_: g & GEN_MASK,
            tier,
        };
        let consecutive = torn((0..=GEN_MASK).map(|g| {
            let n = (g + 1) & GEN_MASK;
            (tag(g, (g % 3) as u8), tag(n, (n % 3) as u8))
        }));
        let reveal = torn((0..=GEN_MASK).map(|g| (tag(g, 0), tag((g + 1) & GEN_MASK, 0))));
        let skipped = torn((0..=GEN_MASK).flat_map(|g| {
            (2..=16u16).map(move |d| {
                let n = (g + d) & GEN_MASK;
                (tag(g, (g % 3) as u8), tag(n, (n % 3) as u8))
            })
        }));
        eprintln!("torn: consecutive {consecutive:?}, reveal {reveal:?}, 2-16 apart {skipped:?}");
        assert_eq!(consecutive, (29_930, 0));
        assert_eq!(reveal, (28_392, 0));
        assert_eq!(skipped, (612_776, 890));
    }
}
