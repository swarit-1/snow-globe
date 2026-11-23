//! One capture sidecar in, one library record or one exclusion out (PRD §8.6, §8.7, §9.1).
//!
//! The library is built from the JSON sidecars capture writes beside every stored image
//! (CLAUDE.md "Data layout"): `frames/` (live frames, ≤ 1920 px), `thumbs/` (the 640 px thumbnail
//! of every live download, D-028) and `archive/` (NPS archive backfill). Every sidecar schema on
//! disk is read: the stopgap's (`stopgap-0`, no sizes or flags), capture 0.1.0's (schema 1), and
//! 0.2.x and 0.3.0's (schema 2; the archive's schema 1). No image is opened here.
//!
//! [`classify`] applies, in this order, and reports the first rule a sidecar breaks:
//!
//! | Rule | [`Exclusion`] |
//! |---|---|
//! | its image `<stem>.jpg` is not a symbolic link (links are never followed) | `symlink` |
//! | the JSON reads, and names a viewpoint | `bad_sidecar` |
//! | its `viewpoint_id` is the folder's | `viewpoint_mismatch` |
//! | it has a readable `image_time_utc`; a live frame's `fetched_at_utc` and candidate image times read too, when given | `bad_sidecar` |
//! | an archived frame: the archive edge (1 Jun 2026, local or UTC, fail closed) | `held_out_archive`, `unreadable_archive_date` |
//! | no frame in the sealed window (15 Dec 2026 – 14 Jan 2027, local or UTC, by every instant the sidecar gives) | `sealed_window` |
//! | no frame in another held-out period of the split (`summer-2026`) | `held_out_period` |
//! | an archived frame only where the camera has an archive (Rainier: own captures only, D-019) | `own_captures_only` |
//! | its image is beside it, as a regular file | `missing_image` |
//! | not flagged `decode_failed` or `tiny_file`, and no `decode_error` (D-037's usable-frame gate) | `unusable` |
//! | at least [`MIN_DISPLAY_WIDTH`] px wide (the archive's 100 px `thumbmedium` copies are not) | `too_small` |
//! | the camera's own framing: aspect within [`FRAMING_TOLERANCE`] of the manifest's `image_px` (the archive's 3:2 epoch is a different view, D-024) | `other_framing` |
//!
//! A frame counts as **archived** on any one sign, as eval's guard reads it (D-038): it is under
//! `archive/`, its `source` is `nps-archive`, or it carries an NPS archive field, in capture's
//! names or NPS's own (`asset_id` or `AssetID`, `image_create_datetime_raw` or
//! `ImageCreateDateTime`, `local_time_string` or `LocalTimeString`; eval's `nps_archive_times`
//! reads both spellings). A sidecar giving one field in both spellings is a `bad_sidecar`.
//!
//! **The instants of a live frame** are its `image_time_utc`, its file name's stamp, capture's
//! `fetched_at_utc` and each of `image_time_candidates_utc` (EXIF, the feed's JSON, HTTP
//! Last-Modified); the held-out periods read the local and the UTC date of every one, so a camera
//! clock running fast at the sealed window's end lets no frame through. One that is given but
//! unreadable refuses the sidecar, since it might be the reading inside a period. An archived
//! frame's `fetched_at_utc` is the backfill's download, not its date, and is not read.
//!
//! **Availability.** A record keeps when the frame became available: for a live frame the later
//! of its image time and its fetch time, for an archived frame its image time, as eval's frame
//! records have it (`eval/src/snow_globe_eval/frames.py`, leakage rule 1). The matcher shows a
//! frame only once it was available (PRD §9.1 "Leakage", §13.1).
//!
//! Not checked here, because they need the pixels or plates: the Same view gate (registration
//! against the plate, TODO(M2), PRD §8.6; a re-aim inside the 16:9 epoch is not caught) and
//! D-037's camera-fault class (TODO(M1.5), with extractor v0's analysis pixels). No sidecar on
//! disk on 22 Nov 2026 has a mean luma under 8 or over 245.

use std::collections::BTreeSet;

use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::solar::sun_position;
use crate::tier0::camera::CameraInfo;
use crate::tier0::heldout::{GuardVerdict, HeldOutRules, archive_dates, instant_dates};

/// Narrower images are not shown as Tier 0 frames (`too_small`).
pub const MIN_DISPLAY_WIDTH: u32 = 320;
/// A stored image whose aspect ratio differs from the camera's by more than this fraction is
/// another framing (`other_framing`): 999×666 (1.50) against 1920×1080 (1.78) is 16 % off, while
/// the archive's 999×562 is 0.02 % off.
pub const FRAMING_TOLERANCE: f64 = 0.02;
/// Sidecar flags that make a frame unusable (D-037).
pub const UNUSABLE_FLAGS: &[&str] = &["decode_failed", "tiny_file"];
/// Sidecar flags that mean the image may be older than its fetch or re-served (capture's Fresh
/// gate inputs); such a frame keeps its place in the library, flagged stale.
pub const STALE_FLAGS: &[&str] = &[
    "stale_feed",
    "stale_image_time",
    "same_last_modified_new_bytes",
    "last_modified_long_after_image_time",
];
/// Capture's `MONOCHROME_MAX_CHROMA`: a chroma spread below this is an infrared frame.
pub const MONOCHROME_MAX_CHROMA: f64 = 1.0;

/// The three trees a sidecar can be in.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Tree {
    /// `frames/`: live frames.
    Frames,
    /// `thumbs/`: 640 px thumbnails of live downloads.
    Thumbs,
    /// `archive/`: NPS archive backfill.
    Archive,
}

impl Tree {
    /// Every tree, in scan order.
    pub const ALL: [Tree; 3] = [Tree::Frames, Tree::Thumbs, Tree::Archive];

    /// The folder name under the data dir.
    pub fn dir(self) -> &'static str {
        match self {
            Tree::Frames => "frames",
            Tree::Thumbs => "thumbs",
            Tree::Archive => "archive",
        }
    }
}

/// What the folder listing shows at `<stem>.jpg` beside a sidecar (no link is followed).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ImageFile {
    /// Nothing.
    Absent,
    /// A regular file.
    Regular,
    /// A symbolic link: never followed, and its sidecar is refused (`symlink`).
    Link,
    /// Something else, such as a folder or a pipe: no image (`missing_image`).
    Other,
}

/// Why a sidecar, or a path, is not in the library.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Exclusion {
    /// An archived frame dated on or after the archive edge, by a local or a UTC date (D-019,
    /// D-022, D-070).
    HeldOutArchive,
    /// An archived frame the guard cannot date (it fails closed).
    UnreadableArchiveDate,
    /// A frame dated in the sealed window (D-019).
    SealedWindow,
    /// A frame dated in another held-out period of the split (`summer-2026`; `tier0::heldout`
    /// rule 3).
    HeldOutPeriod,
    /// An archived frame at a camera whose library is our own captures only (D-019: Rainier).
    OwnCapturesOnly,
    /// A folder (and everything in it) that is not a known viewpoint; never read.
    UnknownViewpoint,
    /// The sidecar's `viewpoint_id` is not its folder's.
    ViewpointMismatch,
    /// No image beside the sidecar, or no regular file there.
    MissingImage,
    /// Capture flagged the image `decode_failed` or `tiny_file` (D-037).
    Unusable,
    /// Narrower than [`MIN_DISPLAY_WIDTH`].
    TooSmall,
    /// Not the camera's framing ([`FRAMING_TOLERANCE`]).
    OtherFraming,
    /// Not JSON, no viewpoint or image time, or an instant given but unreadable.
    BadSidecar,
    /// A sidecar outside `<tree>/<viewpoint>/<date>/`.
    Misplaced,
    /// A symbolic link (a sidecar, its image or a folder); never followed.
    Symlink,
    /// A folder named `quarantine` (any letter case); never read (D-022).
    Quarantine,
}

impl Exclusion {
    /// The reason's name, as in the JSON.
    pub fn name(self) -> &'static str {
        match self {
            Exclusion::HeldOutArchive => "held_out_archive",
            Exclusion::UnreadableArchiveDate => "unreadable_archive_date",
            Exclusion::SealedWindow => "sealed_window",
            Exclusion::HeldOutPeriod => "held_out_period",
            Exclusion::OwnCapturesOnly => "own_captures_only",
            Exclusion::UnknownViewpoint => "unknown_viewpoint",
            Exclusion::ViewpointMismatch => "viewpoint_mismatch",
            Exclusion::MissingImage => "missing_image",
            Exclusion::Unusable => "unusable",
            Exclusion::TooSmall => "too_small",
            Exclusion::OtherFraming => "other_framing",
            Exclusion::BadSidecar => "bad_sidecar",
            Exclusion::Misplaced => "misplaced",
            Exclusion::Symlink => "symlink",
            Exclusion::Quarantine => "quarantine",
        }
    }

    /// True for the held-out rules (CLAUDE.md "Held-out data").
    pub fn is_held_out(self) -> bool {
        matches!(
            self,
            Exclusion::HeldOutArchive
                | Exclusion::UnreadableArchiveDate
                | Exclusion::SealedWindow
                | Exclusion::HeldOutPeriod
        )
    }
}

/// What the library keeps of one included sidecar (cached in `cache/tier0/`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SidecarRecord {
    /// The tree it was found in.
    pub tree: Tree,
    /// Its viewpoint (the folder's, checked against the sidecar's own).
    pub viewpoint_id: String,
    /// The day folder.
    pub day_dir: String,
    /// The file stem (`20261122T000859Z_49c9b6ffe52c`, `..._thumbmedium` for archive thumbnails).
    pub stem: String,
    /// Archived on any sign (module docs).
    pub archived: bool,
    /// The NPS asset ID, for archived frames.
    pub asset_id: Option<String>,
    /// The archive copy: `hires` or `thumbmedium`.
    pub archive_kind: Option<String>,
    /// The image time, milliseconds since the Unix epoch (UTC).
    pub taken_unix_ms: i64,
    /// When the frame became available, milliseconds since the Unix epoch (module docs,
    /// "Availability").
    pub available_unix_ms: i64,
    /// Where capture took the image time from (`exif_offset`, `exif_local`, `nps_archive_label`,
    /// `http_last_modified`).
    pub image_time_source: Option<String>,
    /// Stored image width, when the sidecar gives it.
    pub width: Option<u32>,
    /// Stored image height, when the sidecar gives it.
    pub height: Option<u32>,
    /// Capture's chroma spread (0–255 scale), when given.
    pub chroma_spread: Option<f64>,
    /// The sidecar's `flags`.
    pub flags: Vec<String>,
    /// Which capture version wrote it.
    pub capture_version: Option<String>,
    /// The Sun's geometric altitude at the camera at the image time, from the ephemeris (PRD §8.4).
    pub sun_altitude_deg: f64,
    /// The Sun's azimuth from north through east, degrees.
    pub sun_azimuth_deg: f64,
}

impl SidecarRecord {
    /// The image time.
    pub fn taken_utc(&self) -> DateTime<Utc> {
        DateTime::from_timestamp_millis(self.taken_unix_ms).unwrap_or_default()
    }

    /// The image file beside the sidecar, relative to the data dir.
    pub fn image_rel_path(&self) -> String {
        format!(
            "{}/{}/{}/{}.jpg",
            self.tree.dir(),
            self.viewpoint_id,
            self.day_dir,
            self.stem
        )
    }
}

/// What one sidecar gave.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum SidecarOutcome {
    /// It is in the library.
    Indexed(Box<SidecarRecord>),
    /// It is not, and why.
    Excluded {
        /// The first rule it breaks.
        reason: Exclusion,
        /// A short note (the held-out date, the size, the JSON error).
        detail: String,
    },
}

#[derive(Deserialize)]
struct RawSidecar {
    viewpoint_id: Option<String>,
    source: Option<String>,
    kind: Option<String>,
    image_time_utc: Option<String>,
    image_time_source: Option<String>,
    fetched_at_utc: Option<String>,
    image_time_candidates_utc: Option<serde_json::Value>,
    stored_width: Option<u32>,
    stored_height: Option<u32>,
    chroma_spread: Option<f64>,
    flags: Option<Vec<String>>,
    decode_error: Option<serde_json::Value>,
    capture_version: Option<String>,
    #[serde(alias = "AssetID")]
    asset_id: Option<String>,
    #[serde(alias = "ImageCreateDateTime")]
    image_create_datetime_raw: Option<String>,
    #[serde(alias = "LocalTimeString")]
    local_time_string: Option<String>,
}

/// Where a sidecar sits.
#[derive(Debug, Clone, Copy)]
pub struct SidecarPlace<'a> {
    /// The tree.
    pub tree: Tree,
    /// The day folder's name (a UTC date for capture's layout).
    pub day_dir: &'a str,
    /// The file stem.
    pub stem: &'a str,
    /// What is at `<stem>.jpg` in the same folder.
    pub image: ImageFile,
}

/// The UTC instant in a file stem's `YYYYMMDDTHHMMSSZ` prefix.
pub fn stem_instant(stem: &str) -> Option<DateTime<Utc>> {
    let stamp = stem.get(..16)?;
    NaiveDateTime::parse_from_str(stamp, "%Y%m%dT%H%M%SZ")
        .ok()
        .map(|n| n.and_utc())
}

fn excluded(reason: Exclusion, detail: impl Into<String>) -> SidecarOutcome {
    SidecarOutcome::Excluded {
        reason,
        detail: detail.into(),
    }
}

fn parse_instant(text: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(text)
        .ok()
        .map(|t| t.with_timezone(&Utc))
}

/// A live sidecar's fetch time and capture's candidate image times.
type LiveInstants = (Option<DateTime<Utc>>, Vec<DateTime<Utc>>);

/// A live sidecar's fetch time and capture's candidate image times (module docs, "The instants
/// of a live frame"); one given but unreadable is an error.
fn live_instants(raw: &RawSidecar) -> Result<LiveInstants, String> {
    let fetched = match raw.fetched_at_utc.as_deref() {
        None => None,
        Some(text) => {
            Some(parse_instant(text).ok_or_else(|| format!("unreadable fetched_at_utc {text:?}"))?)
        }
    };
    let mut candidates = Vec::new();
    match &raw.image_time_candidates_utc {
        None | Some(serde_json::Value::Null) => {}
        Some(serde_json::Value::Object(map)) => {
            for (name, value) in map {
                match value {
                    serde_json::Value::Null => {}
                    serde_json::Value::String(text) => {
                        candidates.push(parse_instant(text).ok_or_else(|| {
                            format!("unreadable image_time_candidates_utc.{name} {text:?}")
                        })?);
                    }
                    other => {
                        return Err(format!(
                            "image_time_candidates_utc.{name} is {other}, not a time"
                        ));
                    }
                }
            }
        }
        Some(other) => {
            return Err(format!(
                "image_time_candidates_utc is {other}, not an object"
            ));
        }
    }
    Ok((fetched, candidates))
}

/// Classify one sidecar's text found at `place` in `camera`'s folder (module docs).
pub fn classify(
    text: &[u8],
    place: SidecarPlace<'_>,
    camera: &CameraInfo,
    rules: &HeldOutRules,
) -> SidecarOutcome {
    if place.image == ImageFile::Link {
        return excluded(
            Exclusion::Symlink,
            format!("{}.jpg is a symbolic link, never followed", place.stem),
        );
    }
    let raw: RawSidecar = match serde_json::from_slice(text) {
        Ok(r) => r,
        Err(e) => return excluded(Exclusion::BadSidecar, format!("JSON: {e}")),
    };
    let Some(vp) = raw.viewpoint_id.as_deref() else {
        return excluded(Exclusion::BadSidecar, "no viewpoint_id");
    };
    if vp != camera.viewpoint_id {
        return excluded(
            Exclusion::ViewpointMismatch,
            format!("viewpoint_id {vp} in the folder of {}", camera.viewpoint_id),
        );
    }
    let Some(taken) = raw.image_time_utc.as_deref().and_then(parse_instant) else {
        return excluded(Exclusion::BadSidecar, "no readable image_time_utc");
    };
    let archived = place.tree == Tree::Archive
        || raw.source.as_deref() == Some("nps-archive")
        || raw.asset_id.is_some()
        || raw.image_create_datetime_raw.is_some()
        || raw.local_time_string.is_some();
    let mut instants = vec![taken];
    instants.extend(stem_instant(place.stem));
    let mut available = taken;
    if !archived {
        match live_instants(&raw) {
            Ok((fetched, candidates)) => {
                if let Some(f) = fetched {
                    available = available.max(f);
                    instants.push(f);
                }
                instants.extend(candidates);
            }
            Err(why) => return excluded(Exclusion::BadSidecar, why),
        }
    }
    let folder_date = NaiveDate::parse_from_str(place.day_dir, "%Y-%m-%d").ok();
    let dates: BTreeSet<NaiveDate> = if archived {
        let dates = archive_dates(
            raw.image_create_datetime_raw.as_deref(),
            raw.local_time_string.as_deref(),
            &instants,
            folder_date.as_slice(),
            camera.tz,
        );
        match rules.archive_verdict(&dates) {
            GuardVerdict::Allowed => {}
            GuardVerdict::HeldOut { date, which } => {
                return excluded(
                    Exclusion::HeldOutArchive,
                    format!(
                        "{which:?} date {date} is on or after {}",
                        rules.archive_from
                    )
                    .to_lowercase(),
                );
            }
            GuardVerdict::Unreadable(why) => {
                return excluded(Exclusion::UnreadableArchiveDate, why);
            }
        }
        dates.all()
    } else {
        let (local, utc) = instant_dates(&instants, camera.tz);
        local.into_iter().chain(utc).chain(folder_date).collect()
    };
    if let Some(date) = rules.sealed_date(&dates) {
        return excluded(
            Exclusion::SealedWindow,
            format!("dated {date}, inside the sealed window"),
        );
    }
    if let Some((period, date)) = rules.period_date(&dates) {
        return excluded(
            Exclusion::HeldOutPeriod,
            format!("dated {date}, inside the held-out period {period}"),
        );
    }
    if archived && camera.own_captures_only() {
        return excluded(
            Exclusion::OwnCapturesOnly,
            format!(
                "{} has no NPS archive; its library is our own captures",
                camera.viewpoint_id
            ),
        );
    }
    match place.image {
        ImageFile::Regular => {}
        ImageFile::Absent | ImageFile::Link => {
            return excluded(Exclusion::MissingImage, format!("no {}.jpg", place.stem));
        }
        ImageFile::Other => {
            return excluded(
                Exclusion::MissingImage,
                format!("{}.jpg is not a regular file", place.stem),
            );
        }
    }
    let flags = raw.flags.unwrap_or_default();
    if let Some(f) = flags.iter().find(|f| UNUSABLE_FLAGS.contains(&f.as_str())) {
        return excluded(Exclusion::Unusable, format!("flagged {f}"));
    }
    if let Some(err) = raw.decode_error.as_ref().filter(|v| !v.is_null()) {
        return excluded(Exclusion::Unusable, format!("decode_error {err}"));
    }
    if let Some(w) = raw.stored_width.filter(|w| *w < MIN_DISPLAY_WIDTH) {
        return excluded(Exclusion::TooSmall, format!("{w} px wide"));
    }
    if let (Some(w), Some(h), Some((cw, ch))) =
        (raw.stored_width, raw.stored_height, camera.image_px)
        && h > 0
    {
        let aspect = f64::from(w) / f64::from(h);
        let want = f64::from(cw) / f64::from(ch);
        if ((aspect - want) / want).abs() > FRAMING_TOLERANCE {
            return excluded(
                Exclusion::OtherFraming,
                format!("{w}x{h}, not the camera's {cw}x{ch} framing"),
            );
        }
    }
    let sun = sun_position(&camera.site, taken);
    SidecarOutcome::Indexed(Box::new(SidecarRecord {
        tree: place.tree,
        viewpoint_id: camera.viewpoint_id.clone(),
        day_dir: place.day_dir.to_string(),
        stem: place.stem.to_string(),
        archived,
        asset_id: raw.asset_id,
        archive_kind: raw.kind,
        taken_unix_ms: taken.timestamp_millis(),
        available_unix_ms: available.timestamp_millis(),
        image_time_source: raw.image_time_source,
        width: raw.stored_width,
        height: raw.stored_height,
        chroma_spread: raw.chroma_spread,
        flags,
        capture_version: raw.capture_version,
        sun_altitude_deg: sun.geometric_altitude_deg,
        sun_azimuth_deg: sun.azimuth_deg,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tier0::camera::CameraTable;

    fn cam(id: &str) -> &'static CameraInfo {
        CameraTable::embedded().get(id).expect("camera")
    }

    fn rules() -> &'static HeldOutRules {
        HeldOutRules::committed().expect("split")
    }

    fn place<'a>(tree: Tree, day: &'a str, stem: &'a str) -> SidecarPlace<'a> {
        SidecarPlace {
            tree,
            day_dir: day,
            stem,
            image: ImageFile::Regular,
        }
    }

    fn reason(o: &SidecarOutcome) -> Option<Exclusion> {
        match o {
            SidecarOutcome::Excluded { reason, .. } => Some(*reason),
            SidecarOutcome::Indexed(_) => None,
        }
    }

    fn live_json(vp: &str, t: &str) -> serde_json::Value {
        serde_json::json!({
            "viewpoint_id": vp, "source": "live", "image_time_utc": t,
            "image_time_source": "exif_local", "stored_width": 640, "stored_height": 360,
            "chroma_spread": 20.0, "flags": [], "decode_error": null,
            "capture_version": "snow-globe-capture/0.3.0", "variant": "thumb"
        })
    }

    fn live(vp: &str, t: &str) -> String {
        live_json(vp, t).to_string()
    }

    fn archive(vp: &str, t: &str, icd: &str, lts: &str, w: u32, h: u32) -> String {
        serde_json::json!({
            "viewpoint_id": vp, "source": "nps-archive", "kind": "hires", "image_time_utc": t,
            "asset_id": "b984004f-2b3b-42ef-9eae-79fcfd4c9b25",
            "image_create_datetime_raw": icd, "local_time_string": lts,
            "image_time_source": "nps_archive_label", "stored_width": w, "stored_height": h,
            "chroma_spread": 20.0, "flags": [], "capture_version": "snow-globe-capture/0.3.0"
        })
        .to_string()
    }

    fn indexed(o: SidecarOutcome) -> SidecarRecord {
        match o {
            SidecarOutcome::Indexed(r) => *r,
            SidecarOutcome::Excluded { reason, detail } => panic!("{reason:?}: {detail}"),
        }
    }

    #[test]
    fn a_live_thumbnail_is_indexed_with_its_sun() {
        let text = live("point-reyes.beach", "2026-11-22T23:30:00Z");
        let r = indexed(classify(
            text.as_bytes(),
            place(Tree::Thumbs, "2026-11-22", "20261122T233000Z_f60b95856be1"),
            cam("point-reyes.beach"),
            rules(),
        ));
        // Capture's NOAA calculator gave 13.424° for this frame (sidecar of 22 Nov 2026).
        assert!(
            (r.sun_altitude_deg - 13.42).abs() < 0.1,
            "{}",
            r.sun_altitude_deg
        );
        assert_eq!(
            r.image_rel_path(),
            "thumbs/point-reyes.beach/2026-11-22/20261122T233000Z_f60b95856be1.jpg"
        );
        // No fetch time given: available from its image time.
        assert_eq!(r.available_unix_ms, r.taken_unix_ms);
    }

    #[test]
    fn the_sealed_window_is_refused_by_local_or_utc_date() {
        let c = cam("rainier.paradise-mountain");
        let check = |t: &str| {
            reason(&classify(
                live("rainier.paradise-mountain", t).as_bytes(),
                place(Tree::Frames, &t[..10], "x"),
                c,
                rules(),
            ))
        };
        assert_eq!(check("2026-12-14T23:59:00Z"), None, "14 Dec, 15:59 PST");
        // 23:59 PST on 14 Dec is already 15 Dec in UTC: refused.
        assert_eq!(check("2026-12-15T07:59:00Z"), Some(Exclusion::SealedWindow));
        assert_eq!(check("2026-12-25T20:00:00Z"), Some(Exclusion::SealedWindow));
        assert_eq!(check("2027-01-15T07:59:00Z"), Some(Exclusion::SealedWindow));
        assert_eq!(check("2027-01-15T08:00:00Z"), None, "15 Jan, 00:00 PST");
    }

    #[test]
    fn the_window_reads_every_instant_of_a_live_frame() {
        let c = cam("rainier.paradise-mountain");
        let check = |extra: serde_json::Value| {
            let mut j = live_json("rainier.paradise-mountain", "2027-01-15T08:05:00Z");
            for (k, v) in extra.as_object().expect("object") {
                j[k] = v.clone();
            }
            classify(
                j.to_string().as_bytes(),
                place(Tree::Frames, "2027-01-15", "20270115T080500Z_b0000000000a"),
                c,
                rules(),
            )
        };
        // 00:05 PST on 15 Jan, after the window, fetched 13 min later: allowed.
        let ok = indexed(check(
            serde_json::json!({"fetched_at_utc": "2027-01-15T08:18:00.250Z"}),
        ));
        assert_eq!(
            ok.available_unix_ms - ok.taken_unix_ms,
            13 * 60_000 + 250,
            "available from its fetch"
        );
        // The camera clock runs fast: capture fetched it at 23:58 PST on 14 Jan, inside the
        // window, so the frame was taken inside it. Refused.
        assert_eq!(
            reason(&check(
                serde_json::json!({"fetched_at_utc": "2027-01-15T07:58:00Z"})
            )),
            Some(Exclusion::SealedWindow)
        );
        // So is a frame whose EXIF time says 14 Jan, whatever the chosen image time says.
        assert_eq!(
            reason(&check(serde_json::json!({
                "fetched_at_utc": "2027-01-15T08:18:00Z",
                "image_time_candidates_utc": {"exif": "2027-01-15T07:55:00Z", "json_imagedate": null}
            }))),
            Some(Exclusion::SealedWindow)
        );
        // An instant given but unreadable might be the one inside the window: refused.
        for bad in [
            serde_json::json!({"fetched_at_utc": "yesterday"}),
            serde_json::json!({"image_time_candidates_utc": {"exif": "2027-01-15 08:05"}}),
            serde_json::json!({"image_time_candidates_utc": {"exif": 1_800_000_000}}),
            serde_json::json!({"image_time_candidates_utc": ["2027-01-15T08:05:00Z"]}),
        ] {
            assert_eq!(
                reason(&check(bad.clone())),
                Some(Exclusion::BadSidecar),
                "{bad}"
            );
        }
        // A fetch before the image time (never seen on disk) leaves it available from its time.
        let early = indexed(check(
            serde_json::json!({"fetched_at_utc": "2027-01-15T08:04:00Z"}),
        ));
        assert_eq!(early.available_unix_ms, early.taken_unix_ms);
    }

    #[test]
    fn an_archived_frames_download_time_is_neither_its_date_nor_its_availability() {
        // A summer 2025 frame backfilled on 20 Dec 2026, inside the sealed window.
        let mut j: serde_json::Value = serde_json::from_str(&archive(
            "point-reyes.beach",
            "2025-07-15T20:15:00Z",
            "07/15/2025 12:15:00 PM",
            "7/15/2025 1:15 PM Pacific Daylight Time",
            999,
            562,
        ))
        .expect("json");
        j["fetched_at_utc"] = serde_json::json!("2026-12-20T11:36:11.122Z");
        let r = indexed(classify(
            j.to_string().as_bytes(),
            place(Tree::Archive, "2025-07-15", "20250715T201500Z_b984004f"),
            cam("point-reyes.beach"),
            rules(),
        ));
        assert_eq!(r.available_unix_ms, r.taken_unix_ms);
    }

    #[test]
    fn a_live_frame_in_another_held_out_period_is_refused() {
        // A live frame stamped in summer 2026 (a camera clock months off): design H's season.
        let o = classify(
            live("point-reyes.beach", "2026-07-15T20:00:00Z").as_bytes(),
            place(Tree::Thumbs, "2026-07-15", "20260715T200000Z_b0000000000e"),
            cam("point-reyes.beach"),
            rules(),
        );
        assert_eq!(reason(&o), Some(Exclusion::HeldOutPeriod), "{o:?}");
        assert!(Exclusion::HeldOutPeriod.is_held_out());
    }

    #[test]
    fn archive_edge_rainier_and_framing() {
        let pr = cam("point-reyes.beach");
        let check = |text: String, stem: &str, day: &str| {
            reason(&classify(
                text.as_bytes(),
                place(Tree::Archive, day, stem),
                pr,
                rules(),
            ))
        };
        let ok = archive(
            "point-reyes.beach",
            "2026-05-30T23:45:00Z",
            "05/30/2026 03:45:00 PM",
            "5/30/2026 4:45 PM Pacific Daylight Time",
            999,
            562,
        );
        assert_eq!(check(ok, "20260530T234500Z_b984004f", "2026-05-30"), None);
        let edge = archive(
            "point-reyes.beach",
            "2026-06-01T00:00:00Z",
            "05/31/2026 04:00:00 PM",
            "5/31/2026 5:00 PM Pacific Daylight Time",
            999,
            562,
        );
        assert_eq!(
            check(edge, "20260601T000000Z_b984004f", "2026-06-01"),
            Some(Exclusion::HeldOutArchive)
        );
        let three_two = archive(
            "point-reyes.beach",
            "2023-01-15T20:00:00Z",
            "01/15/2023 12:00:00 PM",
            "1/15/2023 12:00 PM Pacific Standard Time",
            999,
            666,
        );
        assert_eq!(
            check(three_two, "20230115T200000Z_b984004f", "2023-01-15"),
            Some(Exclusion::OtherFraming)
        );
        let tiny = archive(
            "point-reyes.beach",
            "2025-07-15T19:30:00Z",
            "07/15/2025 11:30:00 AM",
            "7/15/2025 12:30 PM Pacific Daylight Time",
            100,
            56,
        );
        assert_eq!(
            check(tiny, "20250715T193000Z_b984004f_thumbmedium", "2025-07-15"),
            Some(Exclusion::TooSmall)
        );
        // Rainier has no archive: even an old, well-formed archive record is refused.
        let rainier = archive(
            "rainier.paradise-mountain",
            "2025-07-15T19:30:00Z",
            "07/15/2025 11:30:00 AM",
            "7/15/2025 12:30 PM Pacific Daylight Time",
            999,
            562,
        );
        assert_eq!(
            reason(&classify(
                rainier.as_bytes(),
                place(Tree::Archive, "2025-07-15", "20250715T193000Z_b984004f"),
                cam("rainier.paradise-mountain"),
                rules(),
            )),
            Some(Exclusion::OwnCapturesOnly)
        );
    }

    #[test]
    fn an_archive_sign_outside_archive_still_meets_the_guard() {
        // A frame under frames/ that carries NPS archive stamps counts as archived (D-038).
        let text = archive(
            "point-reyes.beach",
            "2026-07-15T21:15:00Z",
            "07/15/2026 01:15:00 PM",
            "7/15/2026 2:15 PM Pacific Daylight Time",
            999,
            562,
        )
        .replace("\"nps-archive\"", "\"live\"");
        let o = classify(
            text.as_bytes(),
            place(Tree::Frames, "2026-07-15", "20260715T211500Z_b984004f"),
            cam("point-reyes.beach"),
            rules(),
        );
        assert_eq!(reason(&o), Some(Exclusion::HeldOutArchive), "{o:?}");
    }

    #[test]
    fn npss_own_spellings_are_archive_signs_and_doubled_fields_are_refused() {
        let pr = cam("point-reyes.beach");
        let at = place(Tree::Frames, "2026-07-15", "20260715T201500Z_bbbbbbbbbbbb");
        // NPS's names only, under frames/, source "live": still archived, so still held out.
        let nps = serde_json::json!({
            "viewpoint_id": "point-reyes.beach", "source": "live",
            "image_time_utc": "2026-07-15T20:15:00Z",
            "ImageCreateDateTime": "07/15/2026 12:15:00 PM",
            "LocalTimeString": "7/15/2026 1:15 PM Pacific Daylight Time",
            "AssetID": "b984004f-2b3b-42ef-9eae-79fcfd4c9b25",
            "stored_width": 999, "stored_height": 562
        });
        let o = classify(nps.to_string().as_bytes(), at, pr, rules());
        assert_eq!(reason(&o), Some(Exclusion::HeldOutArchive), "{o:?}");
        // Each name alone is a sign.
        for name in ["ImageCreateDateTime", "LocalTimeString", "AssetID"] {
            let mut one = live_json("point-reyes.beach", "2026-07-15T20:15:00Z");
            one[name] = nps[name].clone();
            let o = classify(one.to_string().as_bytes(), at, pr, rules());
            assert_eq!(reason(&o), Some(Exclusion::HeldOutArchive), "{name}: {o:?}");
        }
        // An old frame in NPS's names is indexed as an archive record, with its asset ID.
        let old = nps
            .to_string()
            .replace("2026", "2025")
            .replace("\"live\"", "\"nps-archive\"");
        let r = indexed(classify(
            old.as_bytes(),
            place(Tree::Archive, "2025-07-15", "20250715T201500Z_b984004f"),
            pr,
            rules(),
        ));
        assert!(r.archived);
        assert_eq!(
            r.asset_id.as_deref(),
            Some("b984004f-2b3b-42ef-9eae-79fcfd4c9b25")
        );
        // One field in both spellings: which to believe is not guessed.
        let mut doubled: serde_json::Value = serde_json::from_str(&old).expect("json");
        doubled["image_create_datetime_raw"] = serde_json::json!("07/15/2025 12:15:00 PM");
        let o = classify(
            doubled.to_string().as_bytes(),
            place(Tree::Archive, "2025-07-15", "20250715T201500Z_b984004f"),
            pr,
            rules(),
        );
        assert_eq!(reason(&o), Some(Exclusion::BadSidecar), "{o:?}");
    }

    #[test]
    fn bad_and_unusable_sidecars() {
        let pr = cam("point-reyes.beach");
        let at = place(Tree::Thumbs, "2026-11-22", "20261122T233000Z_f60b95856be1");
        assert_eq!(
            reason(&classify(b"{not json", at, pr, rules())),
            Some(Exclusion::BadSidecar)
        );
        let other = live("acadia.mcfarland", "2026-11-22T23:30:00Z");
        assert_eq!(
            reason(&classify(other.as_bytes(), at, pr, rules())),
            Some(Exclusion::ViewpointMismatch)
        );
        let flagged = live("point-reyes.beach", "2026-11-22T23:30:00Z")
            .replace("\"flags\":[]", "\"flags\":[\"tiny_file\"]");
        assert_eq!(
            reason(&classify(flagged.as_bytes(), at, pr, rules())),
            Some(Exclusion::Unusable)
        );
        let ok = live("point-reyes.beach", "2026-11-22T23:30:00Z");
        for (image, want) in [
            (ImageFile::Absent, Exclusion::MissingImage),
            (ImageFile::Other, Exclusion::MissingImage),
            (ImageFile::Link, Exclusion::Symlink),
        ] {
            let p = SidecarPlace { image, ..at };
            assert_eq!(
                reason(&classify(ok.as_bytes(), p, pr, rules())),
                Some(want),
                "{image:?}"
            );
        }
        // A linked image refuses the sidecar before its text is read.
        let p = SidecarPlace {
            image: ImageFile::Link,
            ..at
        };
        assert_eq!(
            reason(&classify(b"{not json", p, pr, rules())),
            Some(Exclusion::Symlink)
        );
    }

    #[test]
    fn the_stopgap_schema_is_read() {
        // stopgap-0 (19 Nov 2026): no sizes, flags or chroma; the time has a +00:00 offset.
        let text = r#"{"viewpoint_id": "rainier.paradise-mountain", "source": "live",
            "fetched_at_utc": "2026-11-19T06:30:13.139884+00:00",
            "image_time_utc": "2026-11-19T06:30:13+00:00", "image_time_source": "http_last_modified",
            "stored": "original", "capture_version": "stopgap-0"}"#;
        let r = indexed(classify(
            text.as_bytes(),
            place(Tree::Frames, "2026-11-19", "20261119T063013Z_edf5e91d5e3f"),
            cam("rainier.paradise-mountain"),
            rules(),
        ));
        assert_eq!(r.width, None);
        assert_eq!(r.taken_utc().to_rfc3339(), "2026-11-19T06:30:13+00:00");
        assert_eq!(r.available_unix_ms - r.taken_unix_ms, 139);
    }

    #[test]
    fn stems_carry_their_utc_stamp() {
        assert_eq!(
            stem_instant("20261122T000859Z_49c9b6ffe52c").map(|t| t.to_rfc3339()),
            Some("2026-11-22T00:08:59+00:00".to_string())
        );
        assert_eq!(stem_instant("x"), None);
    }
}
