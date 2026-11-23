//! The Tier 0 library index over the data dir (PRD §9.1 "Library"; ticket M1-08).
//!
//! **What is read.** Only `frames/`, `thumbs/` and `archive/` under the data dir, at the layout's
//! depth `<tree>/<viewpoint_id>/<date>/<file>.json` (CLAUDE.md "Data layout"). A folder named
//! `quarantine` in any letter case is counted and never opened (D-022); symbolic links are counted
//! and never followed, whether the link is a folder, a sidecar or the image beside a sidecar (that
//! sidecar is refused as `symlink`, and an image that is not a regular file counts as missing); a
//! folder that is not a known viewpoint is counted and its sidecars are never read; dot files
//! (capture's in-flight `.<name>.tmp`) are skipped. No image is opened.
//!
//! **Opening an image** (decode, `--open`) goes through [`Library::image_path`], which checks the
//! path again at that moment: the layout's shape, no `quarantine` component in any letter case,
//! no symbolic link anywhere below the data dir (the resolved path must be the data dir's own
//! path plus the entry's), and a regular file; the read itself then checks that the file opened
//! is the file checked (`tier0::decode`). A link or a quarantined file put beside a sidecar after
//! the scan is therefore refused too.
//!
//! **One entry per capture.** A live download can have two sidecars, its frame in `frames/` and
//! its 640 px thumbnail in `thumbs/`, with the same file stem (D-028); an archive asset can have
//! its `hires` copy and its `thumbmedium` copy. Each group becomes one [`Entry`]: the image Tier 0
//! shows is the thumbnail for a live capture (else the frame, for captures from before thumbnails,
//! 19–20 Nov 2026) and the hires copy for an archive asset; `full_frame` is the stored frame, when
//! there is one.
//!
//! **What an entry holds** (PRD §9.1 "Each entry is indexed by"): the viewpoint; the UTC image
//! time, and when the frame became available (a live capture's fetch, the latest of its
//! sidecars'; an archived frame's image time: `tier0::sidecar` "Availability"); the Sun's altitude
//! and its azimuth relative to the camera's view azimuth, from the ephemeris at the manifest's
//! camera position (`oculus-core`, PRD §8.4; positive is right of the view centre); the local day
//! of year; the source (`live` or `nps_archive`) and the licence (the manifest's attribution); and
//! flags: colour mode (capture's `monochrome` flag, or a chroma spread under 1.0), stale (capture's
//! Fresh-gate flags) and the sidecars' own flags. The extractor's weather class waits for
//! extractor v0 (TODO(M1.5)).
//!
//! **The cache.** `cache/tier0/library-v2.json` under the data dir ([`CACHE_REL_PATH`]) keeps each
//! sidecar's outcome with the file's size and modification time and what was at its image's name
//! ([`ImageFile`]). A load stats every sidecar, re-reads only those that changed or are new, drops
//! those that are gone, and writes the file back when anything changed. The whole cache is rebuilt
//! when the rules fingerprint changes: the crate version, the held-out split applied (its file,
//! its text and the rules read from it), the manifests' camera fields, the classification
//! constants, and the source of the classifier, the guard, the camera table, this module, the
//! ephemeris wrapper and the local-day helpers, with the crate manifest and the lock file. It is
//! written atomically (a temporary file, then a rename) and never inside a git work tree.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Instant, UNIX_EPOCH};

use chrono::{DateTime, Datelike, Utc};
use serde::{Deserialize, Serialize};

use crate::datadir::enclosing_git_root;
use crate::query::rfc3339_utc;
use crate::tier0::camera::{CameraInfo, CameraTable};
use crate::tier0::heldout::HeldOutRules;
use crate::tier0::matcher::horizon_weighted_altitude;
pub use crate::tier0::sidecar::ImageFile;
use crate::tier0::sidecar::{
    Exclusion, FRAMING_TOLERANCE, MIN_DISPLAY_WIDTH, MONOCHROME_MAX_CHROMA, STALE_FLAGS,
    SidecarOutcome, SidecarPlace, SidecarRecord, Tree, UNUSABLE_FLAGS, classify,
};

/// The cache file's format number; part of its name and of the rules fingerprint. Format 2 (23
/// Nov 2026) adds each record's availability and the kind of file at its image's name.
pub const CACHE_FORMAT: u32 = 2;
/// The cache file, relative to the data dir.
pub const CACHE_REL_PATH: &str = "cache/tier0/library-v2.json";
/// How many example paths each exclusion reason keeps in the report.
pub const EXAMPLES_PER_REASON: usize = 3;

/// Where an entry's frame came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EntrySource {
    /// Our own live capture.
    Live,
    /// The NPS image archive (backfill).
    NpsArchive,
}

/// Whether the frame is in colour (PRD §8.6 "Colour mode").
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ColourMode {
    /// A colour frame.
    Colour,
    /// A monochrome (infrared) night frame: in the library, flagged (PRD §8.6).
    Monochrome,
    /// The sidecar does not say (the stopgap's).
    Unknown,
}

/// One stored image of an entry.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ImageRef {
    /// Path relative to the data dir.
    pub rel_path: String,
    /// Width in pixels, when the sidecar gives it.
    pub width: Option<u32>,
    /// Height in pixels, when the sidecar gives it.
    pub height: Option<u32>,
}

/// One real frame Tier 0 can show (module docs).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Entry {
    /// Unique and stable: `live:<viewpoint>/<stem>` or `archive:<viewpoint>/<asset id>`.
    pub key: String,
    /// The viewpoint.
    pub viewpoint_id: String,
    /// Live capture or NPS archive.
    pub source: EntrySource,
    /// The image time.
    #[serde(with = "rfc3339_utc")]
    pub taken_utc: DateTime<Utc>,
    /// The image time with milliseconds, since the Unix epoch.
    pub taken_unix_ms: i64,
    /// When the frame became available, milliseconds since the Unix epoch: a live capture's fetch
    /// time (the latest of its sidecars'), never before its image time; an archived frame's image
    /// time (module docs). The matcher's cut-off reads it (PRD §9.1 "Leakage", §13.1).
    pub available_unix_ms: i64,
    /// Where capture took the image time from.
    pub image_time_source: Option<String>,
    /// The Sun's geometric altitude at the camera at the image time, degrees.
    pub sun_altitude_deg: f64,
    /// The Sun's azimuth, degrees from north through east.
    pub sun_azimuth_deg: f64,
    /// The Sun's azimuth relative to the camera's view azimuth, degrees in (−180, 180]; positive
    /// is right of the view centre.
    pub sun_azimuth_rel_deg: f64,
    /// The local day of year (1–366) at the camera.
    pub day_of_year: u32,
    /// The image Tier 0 shows.
    pub image: ImageRef,
    /// The stored full-size frame, when there is one.
    pub full_frame: Option<ImageRef>,
    /// Colour or infrared.
    pub colour_mode: ColourMode,
    /// Capture flagged the image as possibly older than its fetch (it stays in the library).
    pub stale: bool,
    /// The sidecars' flags, sorted, without repeats.
    pub flags: Vec<String>,
    /// The licence: the manifest's attribution.
    pub licence: String,
    /// The capture version that wrote the shown image's sidecar.
    pub capture_version: Option<String>,
    /// The NPS asset ID, for archive entries.
    pub asset_id: Option<String>,
    /// [`horizon_weighted_altitude`] of the Sun's altitude, kept for the matcher.
    #[serde(skip)]
    pub alt_hw: f64,
}

/// Signed difference `a − b` of two azimuths, wrapped to (−180, 180].
pub fn wrap_deg(a: f64, b: f64) -> f64 {
    let d = (a - b).rem_euclid(360.0);
    if d > 180.0 { d - 360.0 } else { d }
}

impl Entry {
    /// An entry from its parts, with the derived fields filled in (for synthetic libraries and
    /// tools; the scanner builds entries from sidecars).
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        key: String,
        camera: &CameraInfo,
        source: EntrySource,
        taken_utc: DateTime<Utc>,
        sun_altitude_deg: f64,
        sun_azimuth_deg: f64,
        image: ImageRef,
        colour_mode: ColourMode,
    ) -> Entry {
        Entry {
            key,
            viewpoint_id: camera.viewpoint_id.clone(),
            source,
            taken_utc,
            taken_unix_ms: taken_utc.timestamp_millis(),
            available_unix_ms: taken_utc.timestamp_millis(),
            image_time_source: None,
            sun_altitude_deg,
            sun_azimuth_deg,
            sun_azimuth_rel_deg: wrap_deg(sun_azimuth_deg, camera.view_azimuth_deg),
            day_of_year: taken_utc.with_timezone(&camera.tz).ordinal(),
            image,
            full_frame: None,
            colour_mode,
            stale: false,
            flags: Vec::new(),
            licence: camera.attribution.clone(),
            capture_version: None,
            asset_id: None,
            alt_hw: horizon_weighted_altitude(sun_altitude_deg),
        }
    }
}

/// How the cache is used.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CacheMode {
    /// Read it, refresh it, write it back if anything changed.
    Use,
    /// Ignore what is there; read every sidecar; write a fresh cache.
    Rebuild,
    /// Neither read nor write it.
    Off,
}

/// What happened to the cache in a build.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CacheReport {
    /// The mode asked for.
    pub mode: CacheMode,
    /// The cache file.
    pub path: Option<String>,
    /// `hit` (nothing re-read), `refreshed`, `rebuilt`, `created` or `off`.
    pub status: String,
    /// Why it was rebuilt, if it was.
    pub rebuilt_because: Option<String>,
    /// Sidecars read and classified in this build.
    pub reparsed: usize,
    /// Sidecars taken from the cache.
    pub reused: usize,
    /// Cache rows dropped because their sidecar is gone.
    pub removed: usize,
    /// True if the cache file was written.
    pub written: bool,
    /// Why it was not written, when it should have been.
    pub write_error: Option<String>,
}

/// Build timings, milliseconds.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct BuildTimings {
    /// Listing folders and reading file sizes and times.
    pub scan_ms: f64,
    /// Reading the cache file.
    pub cache_read_ms: f64,
    /// Reading and classifying the sidecars that changed.
    pub classify_ms: f64,
    /// Grouping sidecars into entries.
    pub merge_ms: f64,
    /// Writing the cache file.
    pub cache_write_ms: f64,
    /// All of it.
    pub total_ms: f64,
}

/// One viewpoint's part of the library.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct ViewpointReport {
    /// Sidecars found in its folders.
    pub sidecars: usize,
    /// Sidecars in the library.
    pub indexed_sidecars: usize,
    /// Entries (captures) in the library.
    pub entries: usize,
    /// Entries from live capture.
    pub live: usize,
    /// Entries from the NPS archive.
    pub archive: usize,
    /// Entries flagged monochrome (infrared).
    pub monochrome: usize,
    /// Entries flagged stale.
    pub stale: usize,
    /// Entries whose shown image is a stored full frame (no thumbnail was made).
    pub shown_from_full_frame: usize,
    /// Excluded sidecars, by reason.
    pub excluded: BTreeMap<Exclusion, usize>,
    /// Up to [`EXAMPLES_PER_REASON`] excluded paths per reason, relative to the data dir.
    pub examples: BTreeMap<Exclusion, Vec<String>>,
    /// The earliest image time in the library.
    pub first_taken_utc: Option<String>,
    /// The latest image time in the library.
    pub last_taken_utc: Option<String>,
}

/// What a build found.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct LibraryReport {
    /// The data dir.
    pub data_dir: String,
    /// The held-out split's version the rules came from.
    pub split_version: u32,
    /// JSON sidecars found under a known viewpoint (excluded ones included).
    pub sidecars: usize,
    /// Entries in the library.
    pub entries: usize,
    /// Per viewpoint, every known viewpoint listed.
    pub viewpoints: BTreeMap<String, ViewpointReport>,
    /// Excluded sidecars by reason, over every viewpoint.
    pub excluded: BTreeMap<Exclusion, usize>,
    /// Folders that are not a known viewpoint, with the sidecars in them (never read).
    pub unknown_folders: BTreeMap<String, usize>,
    /// Paths refused before any viewpoint was known: quarantine folders, symbolic links and
    /// misplaced sidecars.
    pub paths_refused: BTreeMap<Exclusion, usize>,
    /// The cache.
    pub cache: Option<CacheReport>,
    /// Timings.
    pub timings: BuildTimings,
}

/// Why a library could not be built.
#[derive(Debug, thiserror::Error)]
pub enum LibraryError {
    /// The data dir is not a folder.
    #[error("data dir {0} is not a folder")]
    NotADir(PathBuf),
    /// The data dir is, or is inside, a quarantine folder.
    #[error("data dir {0} is inside a quarantine folder (D-022)")]
    Quarantine(PathBuf),
    /// A folder could not be listed.
    #[error("cannot list {path}: {source}")]
    Io {
        /// The folder.
        path: PathBuf,
        /// The error.
        source: std::io::Error,
    },
}

/// Why an entry's image may not be opened ([`Library::image_path`]).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{rel_path}: {why}")]
pub struct ImageRefusal {
    /// The image, relative to the data dir.
    pub rel_path: String,
    /// Why.
    pub why: String,
}

/// The library: entries grouped by viewpoint, and the report of how it was built.
#[derive(Debug, Clone)]
pub struct Library {
    data_dir: PathBuf,
    /// The data dir with every link resolved, when it resolves ([`Library::image_path`]).
    canonical_dir: Option<PathBuf>,
    entries: Vec<Entry>,
    by_viewpoint: BTreeMap<String, Vec<usize>>,
    report: LibraryReport,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct CachedFile {
    p: String,
    len: u64,
    mtime_ns: u64,
    jpg: ImageFile,
    o: SidecarOutcome,
}

#[derive(Debug, Serialize, Deserialize)]
struct CacheFile {
    format: u32,
    rules: String,
    files: Vec<CachedFile>,
}

/// One sidecar found by the scan.
#[derive(Debug, Clone)]
struct Found {
    rel: String,
    abs: PathBuf,
    tree: Tree,
    viewpoint: String,
    day: String,
    stem: String,
    len: u64,
    mtime_ns: u64,
    image: ImageFile,
}

/// FNV-1a, 64 bit: a stable hash for the rules fingerprint (std's hasher may change between
/// Rust releases, which would only cost a rebuild, but this never does).
pub fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in bytes {
        h ^= u64::from(*b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

/// The source of everything a cached outcome is computed by: the classifier, the held-out guard,
/// the camera table (D-019's own-captures rule is code there), this module (the scan that finds a
/// sidecar's image, and the step that reads and classifies it), the ephemeris wrapper, the
/// local-day helpers, the crate manifest and the lock file (which pin `oculus-core` and the zone
/// data of `chrono-tz`). Editing any of them changes the rules fingerprint, so no cached outcome
/// outlives the code that made it.
const CLASSIFIER_SOURCES: [&str; 8] = [
    include_str!("sidecar.rs"),
    include_str!("heldout.rs"),
    include_str!("camera.rs"),
    include_str!("library.rs"),
    include_str!("../solar.rs"),
    include_str!("../tzutil.rs"),
    include_str!("../../Cargo.toml"),
    include_str!("../../../Cargo.lock"),
];

/// The rules fingerprint (module docs, "The cache").
pub fn rules_fingerprint(cameras: &CameraTable, rules: &HeldOutRules) -> String {
    let mut text = format!(
        "format {CACHE_FORMAT}\ncrate {}\nmin width {MIN_DISPLAY_WIDTH}\nframing {FRAMING_TOLERANCE}\n\
         unusable {UNUSABLE_FLAGS:?}\nstale {STALE_FLAGS:?}\nmono {MONOCHROME_MAX_CHROMA}\n\
         split {} {}\n{rules:?}\ncameras\n{}",
        env!("CARGO_PKG_VERSION"),
        rules.split_file,
        rules.split_fnv,
        cameras.fingerprint()
    );
    for source in CLASSIFIER_SOURCES {
        text.push_str(source);
    }
    format!("{:016x}", fnv1a64(text.as_bytes()))
}

fn is_quarantine(name: &str) -> bool {
    name.eq_ignore_ascii_case("quarantine")
}

fn mtime_ns(meta: &std::fs::Metadata) -> u64 {
    meta.modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map_or(0, |d| u64::try_from(d.as_nanos()).unwrap_or(u64::MAX))
}

fn ms(t: Instant) -> f64 {
    t.elapsed().as_secs_f64() * 1e3
}

struct Scan {
    found: Vec<Found>,
    unknown_folders: BTreeMap<String, usize>,
    paths_refused: BTreeMap<Exclusion, usize>,
    /// Viewpoint-level refusals (symlinks, quarantine folders and misplaced files inside a
    /// viewpoint's folders).
    refused_in_viewpoint: Vec<(String, Exclusion, String)>,
}

fn list(dir: &Path) -> Result<Vec<std::fs::DirEntry>, LibraryError> {
    let rd = std::fs::read_dir(dir).map_err(|source| LibraryError::Io {
        path: dir.to_path_buf(),
        source,
    })?;
    let mut out: Vec<std::fs::DirEntry> = rd.filter_map(Result::ok).collect();
    out.sort_by_key(std::fs::DirEntry::file_name);
    Ok(out)
}

fn count_json(dir: &Path) -> usize {
    // Counts the sidecars of an unknown folder by name only; nothing is opened.
    let mut n = 0;
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&d) else {
            continue;
        };
        for e in rd.filter_map(Result::ok) {
            let Ok(ft) = e.file_type() else { continue };
            let name = e.file_name();
            let name = name.to_string_lossy();
            if ft.is_dir() && !is_quarantine(&name) {
                stack.push(e.path());
            } else if ft.is_file() && name.ends_with(".json") && !name.starts_with('.') {
                n += 1;
            }
        }
    }
    n
}

/// A day folder of a known viewpoint, to list.
struct DayJob {
    tree: Tree,
    viewpoint: String,
    day: String,
    path: PathBuf,
    rel_day: String,
}

type Refusal = (String, Exclusion, String);

/// List one day folder: its sidecars, and what in it is refused unread. What sits at each
/// `*.jpg` name is taken from the listing itself, which never follows a link.
fn scan_day(job: &DayJob) -> Result<(Vec<Found>, Vec<Refusal>), LibraryError> {
    let mut found = Vec::new();
    let mut refused = Vec::new();
    let files = list(&job.path)?;
    let images: HashMap<String, ImageFile> = files
        .iter()
        .filter_map(|f| {
            let name = f.file_name().to_string_lossy().into_owned();
            if !name.ends_with(".jpg") {
                return None;
            }
            let kind = match f.file_type() {
                Ok(ft) if ft.is_symlink() => ImageFile::Link,
                Ok(ft) if ft.is_file() => ImageFile::Regular,
                _ => ImageFile::Other,
            };
            Some((name, kind))
        })
        .collect();
    for f in files {
        let fname = f.file_name().to_string_lossy().into_owned();
        if fname.starts_with('.') {
            continue;
        }
        let Ok(ft) = f.file_type() else { continue };
        let rel = format!("{}/{fname}", job.rel_day);
        if ft.is_symlink() {
            if fname.ends_with(".json") {
                refused.push((job.viewpoint.clone(), Exclusion::Symlink, rel));
            }
            continue;
        }
        if ft.is_dir() {
            if is_quarantine(&fname) {
                refused.push((job.viewpoint.clone(), Exclusion::Quarantine, rel));
            } else if count_json(&f.path()) > 0 {
                refused.push((job.viewpoint.clone(), Exclusion::Misplaced, rel));
            }
            continue;
        }
        let Some(stem) = fname.strip_suffix(".json") else {
            continue;
        };
        let Ok(meta) = f.metadata() else { continue };
        found.push(Found {
            rel,
            abs: f.path(),
            tree: job.tree,
            viewpoint: job.viewpoint.clone(),
            day: job.day.clone(),
            stem: stem.to_string(),
            len: meta.len(),
            mtime_ns: mtime_ns(&meta),
            image: images
                .get(&format!("{stem}.jpg"))
                .copied()
                .unwrap_or(ImageFile::Absent),
        });
    }
    Ok((found, refused))
}

fn scan(data_dir: &Path, cameras: &CameraTable) -> Result<Scan, LibraryError> {
    let mut s = Scan {
        found: Vec::new(),
        unknown_folders: BTreeMap::new(),
        paths_refused: BTreeMap::new(),
        refused_in_viewpoint: Vec::new(),
    };
    let mut jobs = Vec::new();
    for tree in Tree::ALL {
        let tree_dir = data_dir.join(tree.dir());
        match std::fs::symlink_metadata(&tree_dir) {
            Err(_) => continue,
            Ok(m) if m.file_type().is_symlink() => {
                *s.paths_refused.entry(Exclusion::Symlink).or_default() += 1;
                continue;
            }
            Ok(m) if !m.is_dir() => continue,
            Ok(_) => {}
        }
        for vp_entry in list(&tree_dir)? {
            let name = vp_entry.file_name().to_string_lossy().into_owned();
            if name.starts_with('.') {
                continue;
            }
            let Ok(ft) = vp_entry.file_type() else {
                continue;
            };
            if ft.is_symlink() {
                *s.paths_refused.entry(Exclusion::Symlink).or_default() += 1;
                continue;
            }
            if ft.is_dir() && is_quarantine(&name) {
                *s.paths_refused.entry(Exclusion::Quarantine).or_default() += 1;
                continue;
            }
            if !ft.is_dir() {
                if name.ends_with(".json") {
                    *s.paths_refused.entry(Exclusion::Misplaced).or_default() += 1;
                }
                continue;
            }
            if cameras.get(&name).is_none() {
                *s.unknown_folders
                    .entry(format!("{}/{name}", tree.dir()))
                    .or_default() += count_json(&vp_entry.path());
                continue;
            }
            for day_entry in list(&vp_entry.path())? {
                let day = day_entry.file_name().to_string_lossy().into_owned();
                if day.starts_with('.') {
                    continue;
                }
                let rel_day = format!("{}/{name}/{day}", tree.dir());
                let Ok(ft) = day_entry.file_type() else {
                    continue;
                };
                if ft.is_symlink() {
                    s.refused_in_viewpoint
                        .push((name.clone(), Exclusion::Symlink, rel_day));
                    continue;
                }
                if ft.is_dir() && is_quarantine(&day) {
                    s.refused_in_viewpoint
                        .push((name.clone(), Exclusion::Quarantine, rel_day));
                    continue;
                }
                if !ft.is_dir() {
                    if day.ends_with(".json") {
                        s.refused_in_viewpoint
                            .push((name.clone(), Exclusion::Misplaced, rel_day));
                    }
                    continue;
                }
                jobs.push(DayJob {
                    tree,
                    viewpoint: name.clone(),
                    day,
                    path: day_entry.path(),
                    rel_day,
                });
            }
        }
    }
    for result in parallel_map(&jobs, scan_day) {
        let (found, refused) = result?;
        s.found.extend(found);
        s.refused_in_viewpoint.extend(refused);
    }
    Ok(s)
}

/// Map `f` over `items` on up to 8 threads; the results come back in item order. A worker that
/// panics leaves its items to be done again on this thread.
fn parallel_map<T: Sync, R: Send>(items: &[T], f: impl Fn(&T) -> R + Sync) -> Vec<R> {
    let threads = std::thread::available_parallelism()
        .map_or(4, std::num::NonZero::get)
        .clamp(1, 8)
        .min(items.len());
    if threads <= 1 {
        return items.iter().map(&f).collect();
    }
    let next = AtomicUsize::new(0);
    let mut slots: Vec<Option<R>> = (0..items.len()).map(|_| None).collect();
    std::thread::scope(|scope| {
        let handles: Vec<_> = (0..threads)
            .map(|_| {
                scope.spawn(|| {
                    let mut done = Vec::new();
                    loop {
                        let i = next.fetch_add(1, Ordering::Relaxed);
                        let Some(item) = items.get(i) else { break };
                        done.push((i, f(item)));
                    }
                    done
                })
            })
            .collect();
        for h in handles {
            if let Ok(done) = h.join() {
                for (i, r) in done {
                    slots[i] = Some(r);
                }
            }
        }
    });
    slots
        .into_iter()
        .zip(items)
        .map(|(r, item)| r.unwrap_or_else(|| f(item)))
        .collect()
}

fn classify_found(f: &Found, cameras: &CameraTable, rules: &HeldOutRules) -> SidecarOutcome {
    let Some(camera) = cameras.get(&f.viewpoint) else {
        return SidecarOutcome::Excluded {
            reason: Exclusion::UnknownViewpoint,
            detail: String::new(),
        };
    };
    let place = SidecarPlace {
        tree: f.tree,
        day_dir: &f.day,
        stem: &f.stem,
        image: f.image,
    };
    if f.image == ImageFile::Link {
        // Refused before the sidecar is read: its image is a link, never followed.
        return classify(&[], place, camera, rules);
    }
    match std::fs::read(&f.abs) {
        Ok(bytes) => classify(&bytes, place, camera, rules),
        Err(e) => SidecarOutcome::Excluded {
            reason: Exclusion::BadSidecar,
            detail: format!("read: {e}"),
        },
    }
}

/// Classify `jobs` on up to 8 threads; results in job order.
fn classify_parallel(
    jobs: &[&Found],
    cameras: &CameraTable,
    rules: &HeldOutRules,
) -> Vec<SidecarOutcome> {
    parallel_map(jobs, |f| classify_found(f, cameras, rules))
}

fn read_cache(path: &Path, rules: &str) -> Result<HashMap<String, CachedFile>, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("no cache ({e})"))?;
    let file: CacheFile =
        serde_json::from_slice(&bytes).map_err(|e| format!("unreadable cache ({e})"))?;
    if file.format != CACHE_FORMAT {
        return Err(format!(
            "cache format {} is not {CACHE_FORMAT}",
            file.format
        ));
    }
    if file.rules != rules {
        return Err(
            "the rules changed (crate version, split, manifests, constants, classifier code or \
             locked dependencies)"
                .to_string(),
        );
    }
    Ok(file.files.into_iter().map(|f| (f.p.clone(), f)).collect())
}

fn write_cache(data_dir: &Path, path: &Path, file: &CacheFile) -> Result<(), String> {
    if let Some(root) = enclosing_git_root(data_dir) {
        return Err(format!(
            "not written: the data dir is inside the git work tree {}",
            root.display()
        ));
    }
    let dir = path.parent().ok_or("no parent folder")?;
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .ok_or("no file name")?;
    std::fs::create_dir_all(dir).map_err(|e| format!("mkdir {}: {e}", dir.display()))?;
    let tmp = dir.join(format!(".{name}.{}.tmp", std::process::id()));
    let bytes = serde_json::to_vec(file).map_err(|e| e.to_string())?;
    std::fs::write(&tmp, bytes).map_err(|e| format!("write {}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("rename to {}: {e}", path.display())
    })
}

fn colour_mode(r: &SidecarRecord) -> Option<ColourMode> {
    if r.flags.iter().any(|f| f == "monochrome") {
        return Some(ColourMode::Monochrome);
    }
    r.chroma_spread.map(|c| {
        if c < MONOCHROME_MAX_CHROMA {
            ColourMode::Monochrome
        } else {
            ColourMode::Colour
        }
    })
}

fn image_ref(r: &SidecarRecord) -> ImageRef {
    ImageRef {
        rel_path: r.image_rel_path(),
        width: r.width,
        height: r.height,
    }
}

/// Group a viewpoint's indexed sidecars into entries (module docs, "One entry per capture").
fn merge(records: Vec<SidecarRecord>, camera: &CameraInfo) -> Vec<Entry> {
    let mut groups: BTreeMap<String, Vec<SidecarRecord>> = BTreeMap::new();
    for r in records {
        let key = if r.archived {
            let id = r.asset_id.clone().unwrap_or_else(|| {
                r.stem
                    .strip_suffix("_thumbmedium")
                    .unwrap_or(&r.stem)
                    .to_string()
            });
            format!("archive:{}/{id}", r.viewpoint_id)
        } else {
            format!("live:{}/{}", r.viewpoint_id, r.stem)
        };
        groups.entry(key).or_default().push(r);
    }
    let mut out = Vec::with_capacity(groups.len());
    for (key, mut group) in groups {
        // The shown image: a live thumbnail, else the widest copy; ties by tree then stem.
        group.sort_by(|a, b| {
            let thumb = |r: &SidecarRecord| r.tree != Tree::Thumbs;
            (thumb(a), std::cmp::Reverse(a.width), a.tree, &a.stem).cmp(&(
                thumb(b),
                std::cmp::Reverse(b.width),
                b.tree,
                &b.stem,
            ))
        });
        let shown = &group[0];
        let archived = group.iter().any(|r| r.archived);
        let full = group.iter().find(|r| r.tree == Tree::Frames).map(image_ref);
        let mut flags: Vec<String> = group.iter().flat_map(|r| r.flags.clone()).collect();
        flags.sort();
        flags.dedup();
        let mode = colour_mode(shown)
            .or_else(|| group.iter().find_map(colour_mode))
            .unwrap_or(ColourMode::Unknown);
        let taken = shown.taken_utc();
        let mut e = Entry::new(
            key,
            camera,
            if archived {
                EntrySource::NpsArchive
            } else {
                EntrySource::Live
            },
            taken,
            shown.sun_altitude_deg,
            shown.sun_azimuth_deg,
            image_ref(shown),
            mode,
        );
        e.taken_unix_ms = shown.taken_unix_ms;
        // Available once every copy was: the latest fetch of its sidecars, never before its time.
        e.available_unix_ms = group
            .iter()
            .map(|r| r.available_unix_ms)
            .fold(shown.taken_unix_ms, i64::max);
        e.image_time_source = shown.image_time_source.clone();
        e.full_frame = full;
        e.stale = flags.iter().any(|f| STALE_FLAGS.contains(&f.as_str()));
        e.flags = flags;
        e.capture_version = shown.capture_version.clone();
        e.asset_id = group.iter().find_map(|r| r.asset_id.clone());
        out.push(e);
    }
    out
}

impl Library {
    /// A library over given entries (synthetic libraries in tests and tools). Entries are kept in
    /// key order, so the result never depends on the order they are given in.
    pub fn from_entries(data_dir: &Path, mut entries: Vec<Entry>) -> Library {
        entries.sort_by(|a, b| a.key.cmp(&b.key));
        entries.dedup_by(|a, b| a.key == b.key);
        let mut by_viewpoint: BTreeMap<String, Vec<usize>> = BTreeMap::new();
        for (i, e) in entries.iter().enumerate() {
            by_viewpoint
                .entry(e.viewpoint_id.clone())
                .or_default()
                .push(i);
        }
        let report = LibraryReport {
            data_dir: data_dir.display().to_string(),
            entries: entries.len(),
            ..LibraryReport::default()
        };
        Library {
            data_dir: data_dir.to_path_buf(),
            canonical_dir: data_dir.canonicalize().ok(),
            entries,
            by_viewpoint,
            report,
        }
    }

    /// Build the library from the sidecars under `data_dir` (module docs).
    pub fn build(
        data_dir: &Path,
        cameras: &CameraTable,
        rules: &HeldOutRules,
        mode: CacheMode,
    ) -> Result<Library, LibraryError> {
        let t_total = Instant::now();
        let mut timings = BuildTimings::default();
        if !data_dir.is_dir() {
            return Err(LibraryError::NotADir(data_dir.to_path_buf()));
        }
        let resolved = data_dir
            .canonicalize()
            .unwrap_or_else(|_| data_dir.to_path_buf());
        if resolved
            .components()
            .any(|c| is_quarantine(&c.as_os_str().to_string_lossy()))
        {
            return Err(LibraryError::Quarantine(resolved));
        }

        let t = Instant::now();
        let scanned = scan(data_dir, cameras)?;
        timings.scan_ms = ms(t);

        let rules_fp = rules_fingerprint(cameras, rules);
        let cache_path = data_dir.join(CACHE_REL_PATH);
        let t = Instant::now();
        let cache_existed = cache_path.is_file();
        let (mut cached, rebuilt_because) = match mode {
            CacheMode::Use => match read_cache(&cache_path, &rules_fp) {
                Ok(map) => (map, None),
                Err(why) => (HashMap::new(), Some(why)),
            },
            CacheMode::Rebuild => (HashMap::new(), Some("asked to rebuild".to_string())),
            CacheMode::Off => (HashMap::new(), None),
        };
        timings.cache_read_ms = ms(t);

        // Reuse what has not changed; classify the rest.
        let mut outcomes: Vec<Option<SidecarOutcome>> = Vec::with_capacity(scanned.found.len());
        let mut todo: Vec<usize> = Vec::new();
        for (i, f) in scanned.found.iter().enumerate() {
            match cached.remove(&f.rel) {
                Some(c) if c.len == f.len && c.mtime_ns == f.mtime_ns && c.jpg == f.image => {
                    outcomes.push(Some(c.o));
                }
                _ => {
                    outcomes.push(None);
                    todo.push(i);
                }
            }
        }
        let removed = cached.len();
        let t = Instant::now();
        let jobs: Vec<&Found> = todo.iter().map(|&i| &scanned.found[i]).collect();
        for (slot, o) in todo.iter().zip(classify_parallel(&jobs, cameras, rules)) {
            outcomes[*slot] = Some(o);
        }
        timings.classify_ms = ms(t);
        let outcomes: Vec<SidecarOutcome> = outcomes
            .into_iter()
            .map(|o| {
                o.unwrap_or(SidecarOutcome::Excluded {
                    reason: Exclusion::BadSidecar,
                    detail: "not classified".to_string(),
                })
            })
            .collect();

        // Report and merge.
        let t = Instant::now();
        let mut report = LibraryReport {
            data_dir: data_dir.display().to_string(),
            split_version: rules.split_version,
            sidecars: scanned.found.len(),
            unknown_folders: scanned.unknown_folders.clone(),
            paths_refused: scanned.paths_refused.clone(),
            ..LibraryReport::default()
        };
        for c in cameras.cameras() {
            report
                .viewpoints
                .insert(c.viewpoint_id.clone(), ViewpointReport::default());
        }
        let mut per_vp: BTreeMap<String, Vec<SidecarRecord>> = BTreeMap::new();
        for (f, o) in scanned.found.iter().zip(&outcomes) {
            let vr = report.viewpoints.entry(f.viewpoint.clone()).or_default();
            vr.sidecars += 1;
            match o {
                SidecarOutcome::Indexed(r) => {
                    vr.indexed_sidecars += 1;
                    per_vp
                        .entry(f.viewpoint.clone())
                        .or_default()
                        .push(r.as_ref().clone());
                }
                SidecarOutcome::Excluded { reason, .. } => {
                    *vr.excluded.entry(*reason).or_default() += 1;
                    let ex = vr.examples.entry(*reason).or_default();
                    if ex.len() < EXAMPLES_PER_REASON {
                        ex.push(f.rel.clone());
                    }
                }
            }
        }
        for (vp, reason, rel) in &scanned.refused_in_viewpoint {
            let vr = report.viewpoints.entry(vp.clone()).or_default();
            *vr.excluded.entry(*reason).or_default() += 1;
            let ex = vr.examples.entry(*reason).or_default();
            if ex.len() < EXAMPLES_PER_REASON {
                ex.push(rel.clone());
            }
        }
        let mut entries = Vec::new();
        for (vp, records) in per_vp {
            if let Some(camera) = cameras.get(&vp) {
                entries.extend(merge(records, camera));
            }
        }
        let mut library = Library::from_entries(data_dir, entries);
        for (vp, idx) in &library.by_viewpoint {
            let vr = report.viewpoints.entry(vp.clone()).or_default();
            vr.entries = idx.len();
            for &i in idx {
                let e = &library.entries[i];
                match e.source {
                    EntrySource::Live => vr.live += 1,
                    EntrySource::NpsArchive => vr.archive += 1,
                }
                vr.monochrome += usize::from(e.colour_mode == ColourMode::Monochrome);
                vr.stale += usize::from(e.stale);
                vr.shown_from_full_frame += usize::from(e.image.rel_path.starts_with("frames/"));
            }
            vr.first_taken_utc = idx
                .iter()
                .map(|&i| library.entries[i].taken_utc)
                .min()
                .map(|t| crate::query::fmt_utc(&t));
            vr.last_taken_utc = idx
                .iter()
                .map(|&i| library.entries[i].taken_utc)
                .max()
                .map(|t| crate::query::fmt_utc(&t));
        }
        for vr in report.viewpoints.values() {
            for (reason, n) in &vr.excluded {
                *report.excluded.entry(*reason).or_default() += n;
            }
        }
        report.entries = library.entries.len();
        timings.merge_ms = ms(t);

        // Write the cache back if anything changed.
        let changed = !todo.is_empty() || removed > 0 || rebuilt_because.is_some();
        let mut cache_report = CacheReport {
            mode,
            path: (mode != CacheMode::Off).then(|| cache_path.display().to_string()),
            status: "off".to_string(),
            rebuilt_because: rebuilt_because.clone(),
            reparsed: todo.len(),
            reused: scanned.found.len() - todo.len(),
            removed,
            written: false,
            write_error: None,
        };
        if mode != CacheMode::Off {
            cache_report.status = match (mode, &rebuilt_because, changed) {
                (CacheMode::Use, Some(_), _) if !cache_existed => "created",
                (_, Some(_), _) => "rebuilt",
                (_, None, false) => "hit",
                (_, None, true) => "refreshed",
            }
            .to_string();
            if changed {
                let t = Instant::now();
                let file = CacheFile {
                    format: CACHE_FORMAT,
                    rules: rules_fp,
                    files: scanned
                        .found
                        .iter()
                        .zip(outcomes)
                        .map(|(f, o)| CachedFile {
                            p: f.rel.clone(),
                            len: f.len,
                            mtime_ns: f.mtime_ns,
                            jpg: f.image,
                            o,
                        })
                        .collect(),
                };
                match write_cache(data_dir, &cache_path, &file) {
                    Ok(()) => cache_report.written = true,
                    Err(e) => cache_report.write_error = Some(e),
                }
                timings.cache_write_ms = ms(t);
            }
        }
        report.cache = Some(cache_report);
        timings.total_ms = ms(t_total);
        report.timings = timings;
        library.report = report;
        Ok(library)
    }

    /// The data dir the entries' paths are relative to.
    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    /// Every entry, in key order.
    pub fn entries(&self) -> &[Entry] {
        &self.entries
    }

    /// The indexes of a viewpoint's entries (empty for an unknown viewpoint).
    pub fn viewpoint_indexes(&self, viewpoint_id: &str) -> &[usize] {
        self.by_viewpoint
            .get(viewpoint_id)
            .map_or(&[], Vec::as_slice)
    }

    /// The entry with a key.
    pub fn by_key(&self, key: &str) -> Option<(usize, &Entry)> {
        self.entries
            .binary_search_by(|e| e.key.as_str().cmp(key))
            .ok()
            .map(|i| (i, &self.entries[i]))
    }

    /// The absolute path of an entry's image, for showing it (no check: to open the file, use
    /// [`Library::image_path`]).
    pub fn path_of(&self, image: &ImageRef) -> PathBuf {
        self.data_dir.join(&image.rel_path)
    }

    /// The path to open an entry's image by, checked now (module docs, "Opening an image"): the
    /// layout's `<tree>/<viewpoint>/<day>/<file>.jpg`, no `quarantine` component in any letter
    /// case before or after resolving, no symbolic link anywhere below the data dir, and a
    /// regular file. The path returned has every link resolved.
    pub fn image_path(&self, image: &ImageRef) -> Result<PathBuf, ImageRefusal> {
        let refuse = |why: String| ImageRefusal {
            rel_path: image.rel_path.clone(),
            why,
        };
        let parts: Vec<&str> = image.rel_path.split('/').collect();
        let shaped = parts.len() == 4
            && Tree::ALL.iter().any(|t| t.dir() == parts[0])
            && parts
                .iter()
                .all(|p| !p.is_empty() && *p != "." && *p != ".." && !p.contains('\\'))
            && parts[3].ends_with(".jpg");
        if !shaped {
            return Err(refuse(
                "not a <tree>/<viewpoint>/<day>/<file>.jpg path".to_string(),
            ));
        }
        if parts.iter().any(|p| is_quarantine(p)) {
            return Err(refuse("names a quarantine folder (D-022)".to_string()));
        }
        let Some(root) = &self.canonical_dir else {
            return Err(refuse(format!(
                "the data dir {} does not resolve",
                self.data_dir.display()
            )));
        };
        let expected = root.join(&image.rel_path);
        let resolved = self
            .data_dir
            .join(&image.rel_path)
            .canonicalize()
            .map_err(|e| refuse(format!("cannot resolve: {e}")))?;
        if resolved
            .components()
            .any(|c| is_quarantine(&c.as_os_str().to_string_lossy()))
        {
            return Err(refuse(format!(
                "resolves under a quarantine folder (D-022): {}",
                resolved.display()
            )));
        }
        if resolved != expected {
            return Err(refuse(format!(
                "resolves to {} through a symbolic link; links are never followed",
                resolved.display()
            )));
        }
        let meta = std::fs::symlink_metadata(&resolved)
            .map_err(|e| refuse(format!("cannot stat: {e}")))?;
        if !meta.file_type().is_file() {
            return Err(refuse("not a regular file".to_string()));
        }
        Ok(resolved)
    }

    /// How the library was built.
    pub fn report(&self) -> &LibraryReport {
        &self.report
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn azimuths_wrap() {
        assert_eq!(wrap_deg(10.0, 350.0), 20.0);
        assert_eq!(wrap_deg(350.0, 10.0), -20.0);
        assert_eq!(wrap_deg(180.0, 0.0), 180.0);
        assert_eq!(wrap_deg(0.0, 180.0), 180.0);
        assert_eq!(wrap_deg(90.0, 90.0), 0.0);
    }

    #[test]
    fn fnv_is_stable() {
        // FNV-1a 64 test vectors.
        assert_eq!(fnv1a64(b""), 0xcbf2_9ce4_8422_2325);
        assert_eq!(fnv1a64(b"a"), 0xaf63_dc4c_8601_ec8c);
    }

    #[test]
    fn from_entries_orders_by_key() {
        let cam = CameraTable::embedded()
            .get("point-reyes.beach")
            .expect("camera");
        let t = DateTime::from_timestamp(1_800_000_000, 0).expect("t");
        let mk = |k: &str| {
            Entry::new(
                k.to_string(),
                cam,
                EntrySource::Live,
                t,
                10.0,
                200.0,
                ImageRef {
                    rel_path: format!("thumbs/{k}.jpg"),
                    width: Some(640),
                    height: Some(360),
                },
                ColourMode::Colour,
            )
        };
        let lib = Library::from_entries(Path::new("/x"), vec![mk("b"), mk("a"), mk("b")]);
        let keys: Vec<&str> = lib.entries().iter().map(|e| e.key.as_str()).collect();
        assert_eq!(keys, ["a", "b"]);
        assert_eq!(lib.viewpoint_indexes("point-reyes.beach"), &[0, 1]);
        assert!(lib.viewpoint_indexes("nowhere").is_empty());
        assert_eq!(lib.by_key("b").map(|(i, _)| i), Some(1));
        // Point Reyes looks NNE (22.5°): a Sun at 200° is 177.5° to its right.
        assert!((lib.entries()[0].sun_azimuth_rel_deg - 177.5).abs() < 1e-9);
        assert_eq!(lib.entries()[0].available_unix_ms, t.timestamp_millis());
    }

    #[cfg(unix)]
    #[test]
    fn an_image_is_opened_only_by_its_own_checked_path() {
        use std::os::unix::fs::symlink;
        let tmp = tempfile::tempdir().expect("tempdir");
        let data = tmp.path().join("data");
        let day = data.join("thumbs/point-reyes.beach/2026-11-22");
        std::fs::create_dir_all(&day).expect("mkdir");
        std::fs::create_dir_all(data.join("quarantine")).expect("mkdir");
        std::fs::write(data.join("quarantine/secret.jpg"), b"x").expect("write");
        std::fs::write(day.join("a.jpg"), b"x").expect("write");
        symlink(day.join("a.jpg"), day.join("b.jpg")).expect("link");
        symlink("../../../quarantine/secret.jpg", day.join("c.jpg")).expect("link");
        std::fs::create_dir(day.join("d.jpg")).expect("mkdir");
        // The data dir itself may be reached through a link (since D-080's switch the old
        // folder name is one).
        let via = tmp.path().join("via");
        symlink(&data, &via).expect("link");
        let at = |rel: &str| ImageRef {
            rel_path: rel.to_string(),
            width: None,
            height: None,
        };
        for root in [&data, &via] {
            let lib = Library::from_entries(root, Vec::new());
            let ok = lib
                .image_path(&at("thumbs/point-reyes.beach/2026-11-22/a.jpg"))
                .expect("a regular file in its own folder");
            assert_eq!(
                ok,
                data.canonicalize()
                    .expect("resolves")
                    .join("thumbs/point-reyes.beach/2026-11-22/a.jpg")
            );
            for (rel, why) in [
                ("thumbs/point-reyes.beach/2026-11-22/b.jpg", "symbolic link"),
                ("thumbs/point-reyes.beach/2026-11-22/c.jpg", "quarantine"),
                (
                    "thumbs/point-reyes.beach/2026-11-22/d.jpg",
                    "not a regular file",
                ),
                (
                    "thumbs/point-reyes.beach/2026-11-22/e.jpg",
                    "cannot resolve",
                ),
                ("thumbs/point-reyes.beach/Quarantine/a.jpg", "quarantine"),
                ("thumbs/point-reyes.beach/../2026-11-22/a.jpg", "path"),
                ("quarantine/secret.jpg", "path"),
                ("thumbs/point-reyes.beach/2026-11-22/a.json", "path"),
            ] {
                let err = lib.image_path(&at(rel)).expect_err(rel);
                assert!(err.why.contains(why), "{rel}: {err}");
            }
        }
        let nowhere = Library::from_entries(&tmp.path().join("missing"), Vec::new());
        assert!(
            nowhere
                .image_path(&at("thumbs/point-reyes.beach/2026-11-22/a.jpg"))
                .is_err()
        );
    }
}
