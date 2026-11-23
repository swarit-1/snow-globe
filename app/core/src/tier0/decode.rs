//! Background decode of Tier 0 frames, so that at key-up the frame is already decoded (PRD §9.1
//! "Latency": "the key-up path is parse → swap"; §6.3 step 2, §7.2 "Prefetch on partials",
//! §10.2).
//!
//! - [`plan_prefetch`] lists the frames a viewpoint's plausible next 48 hours would match: the
//!   best frame at every grid point from `from` to `from + horizon`, every
//!   [`PREFETCH_STEP_MIN`] minute, on whole minutes, so every clock time the parser gives is a
//!   grid point. The measured run (`snow-globe-tier0 bench`, 02:45 UTC on 23 Nov 2026) chose the
//!   step: of 8,000 random valid times in the next 48 h, the best frame was in the plan for 7,786
//!   (97.3 %) at 5 minutes, 7,973 (99.7 %) at 2 and 7,997 (99.96 %) at 1, for at most 73, 87 and
//!   95 MiB of decoded frames per viewpoint. A miss is decoded on the spot ([`ready_image`]).
//! - [`decode_entries`] decodes them on worker threads into a [`DecodedSet`], and
//!   [`spawn_prefetch`] runs it in the background. A [`CancelToken`] stops it between frames (a
//!   final transcript naming another place cancels the load, PRD §7.2).
//! - [`ready_image`] is the core's side of the swap: the decoded frame from the set (a hit), or
//!   one decoded on the spot (a miss, reported as such), handed to the caller. The rest of PRD
//!   §10.1's swap, handing the pixels to the webview, the texture upload and the paint, is the
//!   overlay's and is not measured here: `snow-globe-tier0 bench` reports only the decoded-set
//!   lookup.
//! - [`first_ready`] walks a match's frames closest first and gives the first that decodes, so a
//!   damaged or refused image falls back to the next frame rather than to nothing.
//!
//! **Opening a file.** Every library image is opened by its checked path ([`decode_image`], through
//! [`Library::image_path`]: no symbolic link, no quarantine folder) and read by
//! [`read_regular_file`], which refuses anything but a regular file and checks that the file it
//! opened is the file it checked.
//!
//! **Wiring, not here:** starting a viewpoint's prefetch when a partial transcript names a place,
//! and cancelling it when the final one names another (PRD §7.2, §6.3 step 2), is the app shell's
//! (TODO(M1): `app/src-tauri`); this module gives it the plan, the background decode and the
//! cancel token.
//!
//! The decoder is `zune-jpeg` (MIT OR Apache-2.0 OR Zlib). Frames come out as 8-bit RGB. A frame
//! at least twice [`DISPLAY_MAX_WIDTH`] wide (a stored 1920 px frame, shown only for captures
//! from before thumbnails) is reduced by a whole-number box filter, 1920 → 640; the 640 px
//! thumbnails and the 999 px archive frames keep their size.

use std::collections::{BTreeSet, HashMap};
use std::io::Read;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::thread::JoinHandle;
use std::time::Instant;

use chrono::{DateTime, Duration, Utc};
use serde::Serialize;
use zune_jpeg::JpegDecoder;
use zune_jpeg::zune_core::bytestream::ZCursor;
use zune_jpeg::zune_core::colorspace::ColorSpace;
use zune_jpeg::zune_core::options::DecoderOptions;

use crate::lifecycle::CancelToken;
use crate::tier0::camera::CameraTable;
use crate::tier0::library::{ImageRef, Library};
use crate::tier0::matcher::{MatchOutcome, MatchRequest, find};

/// Frames at least twice this wide are box-filtered down to about this width.
pub const DISPLAY_MAX_WIDTH: u32 = 640;
/// The decoder refuses images larger than this on either side.
pub const MAX_DECODE_SIDE: usize = 8192;
/// How far ahead the prefetch looks (PRD §9.1 "the viewpoint's plausible next 48 h").
pub const PREFETCH_HORIZON_H: i64 = 48;
/// The prefetch grid, minutes (module docs).
pub const PREFETCH_STEP_MIN: i64 = 1;
/// Worker threads for a background prefetch (it runs while speech is being recognised).
pub const PREFETCH_THREADS: usize = 2;

/// A decoded frame, 8-bit RGB, row-major.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodedImage {
    /// Width in pixels.
    pub width: u32,
    /// Height in pixels.
    pub height: u32,
    /// `width × height × 3` bytes.
    pub rgb: Vec<u8>,
    /// The stored image's width.
    pub source_width: u32,
    /// The stored image's height.
    pub source_height: u32,
}

impl DecodedImage {
    /// Bytes of pixel data held.
    pub fn bytes(&self) -> usize {
        self.rgb.len()
    }
}

/// Where the time of one decode went, milliseconds.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize)]
pub struct DecodeTiming {
    /// Checking the path ([`Library::image_path`]); 0 for a file decoded by its path alone.
    pub check_ms: f64,
    /// Reading the file.
    pub read_ms: f64,
    /// Decoding the JPEG.
    pub decode_ms: f64,
    /// The box filter (0 when the frame keeps its size).
    pub resize_ms: f64,
    /// All four.
    pub total_ms: f64,
}

/// Why a frame could not be decoded.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum DecodeError {
    /// The path is refused ([`Library::image_path`]).
    #[error("refused: {0}")]
    Refused(String),
    /// The file could not be read.
    #[error("cannot read {path}: {message}")]
    Io {
        /// The file.
        path: String,
        /// The error.
        message: String,
    },
    /// Not a JPEG the decoder accepts.
    #[error("JPEG: {0}")]
    Jpeg(String),
    /// The decoder panicked (caught; the frame is skipped).
    #[error("the JPEG decoder panicked")]
    Panic,
}

fn ms(t: Instant) -> f64 {
    t.elapsed().as_secs_f64() * 1e3
}

/// Average `f × f` blocks of an RGB image (whole blocks only).
pub fn box_downscale(rgb: &[u8], width: usize, height: usize, f: usize) -> (Vec<u8>, usize, usize) {
    if f <= 1 {
        return (rgb.to_vec(), width, height);
    }
    let (w, h) = (width / f, height / f);
    let mut out = vec![0u8; w * h * 3];
    let area = (f * f) as u32;
    let mut row = vec![0u32; w * 3];
    for oy in 0..h {
        row.iter_mut().for_each(|v| *v = 0);
        for y in oy * f..oy * f + f {
            let line = &rgb[y * width * 3..(y * width + w * f) * 3];
            for (ox, block) in line.chunks_exact(f * 3).enumerate() {
                for px in block.as_chunks::<3>().0 {
                    row[ox * 3] += u32::from(px[0]);
                    row[ox * 3 + 1] += u32::from(px[1]);
                    row[ox * 3 + 2] += u32::from(px[2]);
                }
            }
        }
        for (o, s) in out[oy * w * 3..(oy + 1) * w * 3].iter_mut().zip(&row) {
            *o = u8::try_from((s + area / 2) / area).unwrap_or(u8::MAX);
        }
    }
    (out, w, h)
}

/// Decode JPEG bytes to RGB, reducing frames at least twice `max_width` wide (module docs).
/// Returns the image and the decode and resize times in milliseconds.
pub fn decode_jpeg(bytes: &[u8], max_width: u32) -> Result<(DecodedImage, f64, f64), DecodeError> {
    let t = Instant::now();
    let decoded = catch_unwind(AssertUnwindSafe(|| {
        let options = DecoderOptions::default()
            .jpeg_set_out_colorspace(ColorSpace::RGB)
            .set_max_width(MAX_DECODE_SIDE)
            .set_max_height(MAX_DECODE_SIDE);
        let mut decoder = JpegDecoder::new_with_options(ZCursor::new(bytes), options);
        let pixels = decoder.decode().map_err(|e| format!("{e:?}"))?;
        let (w, h) = decoder
            .dimensions()
            .ok_or_else(|| "no dimensions after decode".to_string())?;
        Ok::<_, String>((pixels, w, h))
    }));
    let (pixels, w, h) = match decoded {
        Ok(Ok(v)) => v,
        Ok(Err(e)) => return Err(DecodeError::Jpeg(e)),
        Err(_) => return Err(DecodeError::Panic),
    };
    if pixels.len() != w * h * 3 {
        return Err(DecodeError::Jpeg(format!(
            "{} bytes for {w}x{h} RGB",
            pixels.len()
        )));
    }
    let decode_ms = ms(t);
    let t = Instant::now();
    let max = usize::try_from(max_width).unwrap_or(usize::MAX).max(1);
    let factor = w / max;
    let (rgb, ow, oh) = if factor >= 2 {
        box_downscale(&pixels, w, h, factor)
    } else {
        (pixels, w, h)
    };
    let resize_ms = if factor >= 2 { ms(t) } else { 0.0 };
    let side = |n: usize| u32::try_from(n).unwrap_or(u32::MAX);
    Ok((
        DecodedImage {
            width: side(ow),
            height: side(oh),
            rgb,
            source_width: side(w),
            source_height: side(h),
        },
        decode_ms,
        resize_ms,
    ))
}

#[cfg(unix)]
fn same_file(checked: &std::fs::Metadata, opened: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;
    opened.file_type().is_file() && checked.dev() == opened.dev() && checked.ino() == opened.ino()
}

#[cfg(not(unix))]
fn same_file(_checked: &std::fs::Metadata, opened: &std::fs::Metadata) -> bool {
    opened.file_type().is_file()
}

/// Read a file without following a symbolic link: the path must be a regular file when checked,
/// and the file opened must be that same file (device and inode), so a link or another file put in
/// its place in between is refused.
pub fn read_regular_file(path: &Path) -> std::io::Result<Vec<u8>> {
    let checked = std::fs::symlink_metadata(path)?;
    if !checked.file_type().is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "not a regular file (a link, a folder or a device), so never opened",
        ));
    }
    let mut file = std::fs::File::open(path)?;
    let opened = file.metadata()?;
    if !same_file(&checked, &opened) {
        return Err(std::io::Error::other(
            "the file changed between its check and its opening",
        ));
    }
    let mut bytes = Vec::with_capacity(usize::try_from(opened.len()).unwrap_or(0));
    file.read_to_end(&mut bytes)?;
    Ok(bytes)
}

/// Read ([`read_regular_file`]) and decode ([`decode_jpeg`]) one file.
pub fn decode_file(
    path: &Path,
    max_width: u32,
) -> Result<(DecodedImage, DecodeTiming), DecodeError> {
    let t = Instant::now();
    let bytes = read_regular_file(path).map_err(|e| DecodeError::Io {
        path: path.display().to_string(),
        message: e.to_string(),
    })?;
    let read_ms = ms(t);
    let (img, decode_ms, resize_ms) = decode_jpeg(&bytes, max_width)?;
    Ok((
        img,
        DecodeTiming {
            check_ms: 0.0,
            read_ms,
            decode_ms,
            resize_ms,
            total_ms: read_ms + decode_ms + resize_ms,
        },
    ))
}

/// Decode a library image by its checked path ([`Library::image_path`], then [`decode_file`]):
/// the way Tier 0 opens every library image. The check's time is in the timing.
pub fn decode_image(
    library: &Library,
    image: &ImageRef,
    max_width: u32,
) -> Result<(DecodedImage, DecodeTiming), DecodeError> {
    let t = Instant::now();
    let path = library
        .image_path(image)
        .map_err(|r| DecodeError::Refused(r.to_string()))?;
    let check_ms = ms(t);
    let (img, mut timing) = decode_file(&path, max_width)?;
    timing.check_ms = check_ms;
    timing.total_ms += check_ms;
    Ok((img, timing))
}

/// Decoded frames by entry key, shared between the prefetch and the swap.
#[derive(Debug, Default)]
pub struct DecodedSet {
    images: RwLock<HashMap<String, Arc<DecodedImage>>>,
}

impl DecodedSet {
    /// An empty set.
    pub fn new() -> DecodedSet {
        DecodedSet::default()
    }

    /// The decoded frame of an entry, if it is in the set.
    pub fn get(&self, key: &str) -> Option<Arc<DecodedImage>> {
        self.images.read().ok().and_then(|m| m.get(key).cloned())
    }

    /// Add a decoded frame.
    pub fn insert(&self, key: String, image: Arc<DecodedImage>) {
        if let Ok(mut m) = self.images.write() {
            m.insert(key, image);
        }
    }

    /// Frames held.
    pub fn len(&self) -> usize {
        self.images.read().map_or(0, |m| m.len())
    }

    /// True if no frame is held.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Pixel bytes held.
    pub fn bytes(&self) -> usize {
        self.images
            .read()
            .map_or(0, |m| m.values().map(|i| i.bytes()).sum())
    }
}

/// What a viewpoint's next hours would match (module docs).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PrefetchPlan {
    /// The viewpoint.
    pub viewpoint_id: String,
    /// The first grid point (the step's whole multiple at or before the asked start).
    #[serde(with = "crate::query::rfc3339_utc")]
    pub from: DateTime<Utc>,
    /// The horizon, hours.
    pub horizon_h: i64,
    /// The grid step, minutes.
    pub step_min: i64,
    /// Grid points matched.
    pub grid_points: usize,
    /// Grid points where no frame qualifies.
    pub no_frame_points: usize,
    /// The frames to decode: library indexes, ascending.
    #[serde(skip)]
    pub indexes: Vec<usize>,
    /// Their keys.
    pub keys: Vec<String>,
    /// Time to plan, milliseconds.
    pub plan_ms: f64,
}

/// Plan a viewpoint's prefetch: the best frame at every grid point from `from` to
/// `from + horizon_h` hours, every `step_min` minutes on whole multiples of the step (UTC), with
/// the same cut-off a key-up match uses ([`MatchRequest::as_of`]).
pub fn plan_prefetch(
    library: &Library,
    cameras: &CameraTable,
    viewpoint_id: &str,
    from: DateTime<Utc>,
    horizon_h: i64,
    step_min: i64,
    as_of: Option<DateTime<Utc>>,
) -> PrefetchPlan {
    let t = Instant::now();
    let step = step_min.max(1);
    let step_s = step * 60;
    let first = DateTime::from_timestamp(from.timestamp() - from.timestamp().rem_euclid(step_s), 0)
        .unwrap_or(from);
    let last = from + Duration::hours(horizon_h.max(0));
    let points = (last - first).num_seconds() / step_s + 1;
    let mut chosen = BTreeSet::new();
    let mut no_frame = 0;
    let mut req = MatchRequest::new(viewpoint_id, from);
    req.alternatives = 0;
    req.as_of = as_of;
    for i in 0..points {
        req.valid_time = first + Duration::minutes(i * step);
        match find(library, cameras, &req) {
            MatchOutcome::Matched(m) => {
                chosen.insert(m.best.index);
            }
            MatchOutcome::NoFrame(_) => no_frame += 1,
        }
    }
    let indexes: Vec<usize> = chosen.into_iter().collect();
    PrefetchPlan {
        viewpoint_id: viewpoint_id.to_string(),
        from: first,
        horizon_h,
        step_min: step,
        grid_points: usize::try_from(points).unwrap_or(0),
        no_frame_points: no_frame,
        keys: indexes
            .iter()
            .map(|&i| library.entries()[i].key.clone())
            .collect(),
        indexes,
        plan_ms: ms(t),
    }
}

/// One decoded frame in a prefetch.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct DecodeRecord {
    /// The entry.
    pub key: String,
    /// The stored image's size.
    pub source_px: (u32, u32),
    /// The decoded size.
    pub decoded_px: (u32, u32),
    /// Where the time went.
    pub timing: DecodeTiming,
}

/// What a prefetch did.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct PrefetchReport {
    /// Frames decoded, in completion order.
    pub decoded: Vec<DecodeRecord>,
    /// Frames that failed, with why.
    pub failed: Vec<(String, String)>,
    /// Frames already in the set.
    pub already_decoded: usize,
    /// True if the cancel token stopped it.
    pub cancelled: bool,
    /// Worker threads.
    pub threads: usize,
    /// Wall time, milliseconds.
    pub wall_ms: f64,
}

/// Decode a library's entries into `set` on `threads` workers, stopping between frames once
/// `cancel` is cancelled. Blocks until done; [`spawn_prefetch`] runs it in the background.
pub fn decode_entries(
    library: &Library,
    indexes: &[usize],
    threads: usize,
    set: &DecodedSet,
    cancel: Option<&CancelToken>,
) -> PrefetchReport {
    let t = Instant::now();
    let todo: Vec<usize> = indexes
        .iter()
        .copied()
        .filter(|&i| set.get(&library.entries()[i].key).is_none())
        .collect();
    let threads = threads.clamp(1, 16).min(todo.len().max(1));
    let next = AtomicUsize::new(0);
    let report = Mutex::new(PrefetchReport {
        already_decoded: indexes.len() - todo.len(),
        threads,
        ..PrefetchReport::default()
    });
    std::thread::scope(|scope| {
        for _ in 0..threads {
            scope.spawn(|| {
                loop {
                    if cancel.is_some_and(CancelToken::is_cancelled) {
                        if let Ok(mut r) = report.lock() {
                            r.cancelled = true;
                        }
                        break;
                    }
                    let n = next.fetch_add(1, Ordering::Relaxed);
                    let Some(&i) = todo.get(n) else { break };
                    let e = &library.entries()[i];
                    let result = decode_image(library, &e.image, DISPLAY_MAX_WIDTH);
                    let Ok(mut r) = report.lock() else { break };
                    match result {
                        Ok((img, timing)) => {
                            r.decoded.push(DecodeRecord {
                                key: e.key.clone(),
                                source_px: (img.source_width, img.source_height),
                                decoded_px: (img.width, img.height),
                                timing,
                            });
                            set.insert(e.key.clone(), Arc::new(img));
                        }
                        Err(err) => r.failed.push((e.key.clone(), err.to_string())),
                    }
                }
            });
        }
    });
    let mut r = report.into_inner().unwrap_or_default();
    r.wall_ms = ms(t);
    r
}

/// Run [`decode_entries`] on a background thread.
pub fn spawn_prefetch(
    library: Arc<Library>,
    indexes: Vec<usize>,
    threads: usize,
    set: Arc<DecodedSet>,
    cancel: Option<CancelToken>,
) -> JoinHandle<PrefetchReport> {
    std::thread::spawn(move || decode_entries(&library, &indexes, threads, &set, cancel.as_ref()))
}

/// The core's side of the swap: an entry's decoded frame from the set (a hit), else decoded now
/// (a miss, with its timing) and added to the set.
pub fn ready_image(
    library: &Library,
    set: &DecodedSet,
    index: usize,
) -> Result<(Arc<DecodedImage>, Option<DecodeTiming>), DecodeError> {
    let e = &library.entries()[index];
    if let Some(img) = set.get(&e.key) {
        return Ok((img, None));
    }
    let (img, timing) = decode_image(library, &e.image, DISPLAY_MAX_WIDTH)?;
    let img = Arc::new(img);
    set.insert(e.key.clone(), Arc::clone(&img));
    Ok((img, Some(timing)))
}

/// A frame of a match that could not be shown, and why.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Unreadable {
    /// The entry.
    pub key: String,
    /// Its image (absolute path, not resolved).
    pub path: String,
    /// Why it could not be decoded.
    pub error: String,
}

/// The frame [`first_ready`] found.
#[derive(Debug, Clone)]
pub struct Ready {
    /// Its position in the list given.
    pub position: usize,
    /// Its library index.
    pub index: usize,
    /// The decoded frame.
    pub image: Arc<DecodedImage>,
    /// The decode's timing, when it was decoded now (`None`: taken from the set).
    pub timing: Option<DecodeTiming>,
}

/// The first of `indexes` (a match's frames, closest first) whose image is ready
/// ([`ready_image`]), with every frame skipped before it and why; `None` with all of them skipped
/// when none decodes.
pub fn first_ready(
    library: &Library,
    set: &DecodedSet,
    indexes: &[usize],
) -> (Option<Ready>, Vec<Unreadable>) {
    let mut skipped = Vec::new();
    for (position, &index) in indexes.iter().enumerate() {
        let e = &library.entries()[index];
        match ready_image(library, set, index) {
            Ok((image, timing)) => {
                return (
                    Some(Ready {
                        position,
                        index,
                        image,
                        timing,
                    }),
                    skipped,
                );
            }
            Err(err) => skipped.push(Unreadable {
                key: e.key.clone(),
                path: library.path_of(&e.image).display().to_string(),
                error: err.to_string(),
            }),
        }
    }
    (None, skipped)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn box_filter_averages_whole_blocks() {
        // 4x2 RGB, factor 2 → 2x1.
        let rgb: Vec<u8> = vec![
            0, 0, 0, 10, 10, 10, 100, 0, 0, 200, 0, 0, //
            20, 20, 20, 30, 30, 30, 100, 0, 0, 200, 0, 1,
        ];
        let (out, w, h) = box_downscale(&rgb, 4, 2, 2);
        assert_eq!((w, h), (2, 1));
        assert_eq!(out, vec![15, 15, 15, 150, 0, 0]);
        let (same, w, h) = box_downscale(&rgb, 4, 2, 1);
        assert_eq!((w, h), (4, 2));
        assert_eq!(same, rgb);
    }

    #[test]
    fn garbage_is_an_error_not_a_panic() {
        assert!(decode_jpeg(b"not a jpeg", DISPLAY_MAX_WIDTH).is_err());
        assert!(decode_jpeg(&[0xff, 0xd8, 0xff, 0xe0, 0, 4], DISPLAY_MAX_WIDTH).is_err());
        assert!(decode_jpeg(&[], DISPLAY_MAX_WIDTH).is_err());
    }

    #[test]
    fn a_missing_file_is_an_io_error() {
        let err =
            decode_file(Path::new("/nonexistent/x.jpg"), DISPLAY_MAX_WIDTH).expect_err("missing");
        assert!(matches!(err, DecodeError::Io { .. }), "{err}");
    }

    #[cfg(unix)]
    #[test]
    fn a_link_or_a_folder_is_never_read() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let file = tmp.path().join("a.jpg");
        std::fs::write(&file, b"bytes").expect("write");
        std::os::unix::fs::symlink(&file, tmp.path().join("link.jpg")).expect("link");
        std::fs::create_dir(tmp.path().join("dir.jpg")).expect("mkdir");
        assert_eq!(read_regular_file(&file).expect("a regular file"), b"bytes");
        for name in ["link.jpg", "dir.jpg"] {
            let err = read_regular_file(&tmp.path().join(name)).expect_err(name);
            assert_eq!(
                err.kind(),
                std::io::ErrorKind::InvalidInput,
                "{name}: {err}"
            );
        }
    }

    #[test]
    fn the_set_counts_bytes() {
        let set = DecodedSet::new();
        assert!(set.is_empty());
        set.insert(
            "k".to_string(),
            Arc::new(DecodedImage {
                width: 2,
                height: 1,
                rgb: vec![0; 6],
                source_width: 2,
                source_height: 1,
            }),
        );
        assert_eq!((set.len(), set.bytes()), (1, 6));
        assert!(set.get("k").is_some() && set.get("x").is_none());
    }
}
