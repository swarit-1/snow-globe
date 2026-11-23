//! Property tests for Tier 0 (PRD §9.1, §13.1; CLAUDE.md "Held-out data").
//!
//! 1. **The result does not depend on entry order.** Over random libraries (with deliberate ties:
//!    frames on a 15-minute grid, some at the same instant) and random queries, ranking the
//!    entries in any order, or building the library from them in any order, gives the same frames
//!    at the same distances in the same order.
//! 2. **An excluded frame is never returned, nor one not yet available.** Over random sets of
//!    sidecars written to a temporary data dir (live thumbnails around the sealed window, each
//!    fetched 0–40 minutes after it was taken; archive records around the 1 Jun 2026 edge with
//!    stamps written as NPS writes them, each downloaded inside the sealed window; Rainier archive
//!    records; unreadable stamps), an independent oracle, which reads only each frame's true
//!    instant (and a live frame's fetch) in UTC and in the camera's zone, says which frames the
//!    rules exclude. None of them is in the library, and no query ever returns one, as best or
//!    alternative. Every frame the oracle allows away from the edges is in the library, so the
//!    guard is not simply refusing everything. Queries with a cut-off near a frame's time see
//!    exactly the frames taken before it and fetched by it: the oracle's count is the matcher's
//!    candidate count, and no frame returned was fetched after the cut-off.
//! 3. **The guard keeps the instant.** With NPS stamps mutated at random, an archived record whose
//!    true local or UTC date is on or after 1 Jun 2026 is never allowed, because the record's
//!    own instant is always read.

use std::path::Path;

use chrono::{DateTime, Duration, NaiveDate, TimeZone, Utc};
use chrono_tz::{OffsetComponents, Tz};
use proptest::prelude::*;
use snow_globe_core::solar::sun_position;
use snow_globe_core::tier0::camera::{CameraInfo, CameraTable};
use snow_globe_core::tier0::heldout::{GuardVerdict, HeldOutRules, archive_dates};
use snow_globe_core::tier0::library::{
    CacheMode, ColourMode, Entry, EntrySource, ImageRef, Library,
};
use snow_globe_core::tier0::matcher::{MatchOutcome, MatchRequest, find, query_descriptor, rank};

fn cams() -> &'static [CameraInfo] {
    CameraTable::embedded().cameras()
}

fn base() -> DateTime<Utc> {
    Utc.with_ymd_and_hms(2026, 11, 15, 0, 0, 0)
        .single()
        .expect("t")
}

/// An entry whose Sun is the real Sun at the camera at `t`.
fn real_entry(key: String, cam: &CameraInfo, t: DateTime<Utc>) -> Entry {
    let sun = sun_position(&cam.site, t);
    Entry::new(
        key.clone(),
        cam,
        EntrySource::Live,
        t,
        sun.geometric_altitude_deg,
        sun.azimuth_deg,
        ImageRef {
            rel_path: format!("thumbs/{}/{key}.jpg", cam.viewpoint_id),
            width: Some(640),
            height: Some(360),
        },
        ColourMode::Colour,
    )
}

/// Fisher–Yates with a SplitMix64 stream.
fn shuffle<T>(v: &mut [T], mut seed: u64) {
    for i in (1..v.len()).rev() {
        seed = seed.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = seed;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^= z >> 31;
        v.swap(i, (z % (i as u64 + 1)) as usize);
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(192))]

    #[test]
    fn the_match_does_not_depend_on_entry_order(
        slots in prop::collection::vec((0u32..2 * 96 * 14, 0u32..1000), 1..80),
        vp in 0usize..8,
        query_min in 0i64..(21 * 24 * 60),
        seed in any::<u64>(),
    ) {
        let cam = &cams()[vp];
        // 15-minute slots over two weeks: repeats give identical distances, so ties are common.
        let entries: Vec<Entry> = slots
            .iter()
            .enumerate()
            .map(|(i, (slot, name))| {
                let t = base() + Duration::minutes(i64::from(slot / 2) * 15);
                real_entry(format!("k{name:04}-{i:03}"), cam, t)
            })
            .collect();
        let q = query_descriptor(cam, base() + Duration::minutes(query_min));
        let mut shuffled = entries.clone();
        shuffle(&mut shuffled, seed);
        let k = 5;
        let a = rank(&q, entries.iter().enumerate(), k);
        let b = rank(&q, shuffled.iter().enumerate(), k);
        let strip = |v: Vec<snow_globe_core::tier0::matcher::Scored>| {
            v.into_iter().map(|s| (s.key, s.distance)).collect::<Vec<_>>()
        };
        prop_assert_eq!(strip(a), strip(b));
        let la = Library::from_entries(Path::new("/data"), entries);
        let lb = Library::from_entries(Path::new("/data"), shuffled);
        let mut req = MatchRequest::new(&cam.viewpoint_id, base() + Duration::minutes(query_min));
        req.alternatives = 4;
        req.max_distance = f64::INFINITY;
        let (ma, mb) = (find(&la, CameraTable::embedded(), &req), find(&lb, CameraTable::embedded(), &req));
        let keys = |m: MatchOutcome| match m {
            MatchOutcome::Matched(m) => std::iter::once(m.best)
                .chain(m.alternatives)
                .map(|s| (s.key, s.distance))
                .collect::<Vec<_>>(),
            MatchOutcome::NoFrame(n) => vec![(format!("{:?}", n.reason), 0.0)],
        };
        prop_assert_eq!(keys(ma), keys(mb));
    }
}

/// NPS's stamps for an instant: `ImageCreateDateTime` in local standard time, `LocalTimeString`
/// in civil time with its zone name (snow-globe-common's `parse_nps_archive_time` docs).
fn nps_stamps(t: DateTime<Utc>, tz: Tz) -> (String, String) {
    let offset = tz.offset_from_utc_datetime(&t.naive_utc());
    let standard = offset.base_utc_offset();
    let daylight = !offset.dst_offset().is_zero();
    let wall_std = t.naive_utc() + standard;
    let civil = t.with_timezone(&tz);
    let region = match tz.name() {
        "America/Los_Angeles" => "Pacific",
        "America/Phoenix" => "US Mountain",
        "America/Denver" => "Mountain",
        "America/Chicago" => "Central",
        _ => "Eastern",
    };
    (
        wall_std.format("%m/%d/%Y %I:%M:%S %p").to_string(),
        format!(
            "{} {region} {} Time",
            civil.format("%-m/%-d/%Y %-I:%M %p"),
            if daylight { "Daylight" } else { "Standard" }
        ),
    )
}

fn d(y: i32, m: u32, dd: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, dd).expect("date")
}

/// The oracle: the rules read from a frame's true instant, and a live frame's fetch.
fn oracle_excludes(
    archived: bool,
    garbage: bool,
    cam: &CameraInfo,
    t: DateTime<Utc>,
    fetched: DateTime<Utc>,
) -> bool {
    let local = t.with_timezone(&cam.tz).date_naive();
    let utc = t.date_naive();
    let sealed = |x: NaiveDate| (d(2026, 12, 15)..=d(2027, 1, 14)).contains(&x);
    let summer = |x: NaiveDate| (d(2026, 6, 1)..=d(2026, 8, 31)).contains(&x);
    if sealed(local) || sealed(utc) || summer(local) || summer(utc) {
        return true;
    }
    if !archived {
        let (f_local, f_utc) = (
            fetched.with_timezone(&cam.tz).date_naive(),
            fetched.date_naive(),
        );
        return sealed(f_local) || sealed(f_utc);
    }
    garbage || cam.own_captures_only() || local >= d(2026, 6, 1) || utc >= d(2026, 6, 1)
}

#[derive(Debug, Clone)]
struct Planned {
    key: String,
    viewpoint_id: String,
    archived: bool,
    excluded: bool,
    t: DateTime<Utc>,
    /// When the frame became available: a live frame's fetch, an archived frame's own time.
    available: DateTime<Utc>,
}

fn write_sidecar(
    dir: &Path,
    i: usize,
    kind: u8,
    cam: &CameraInfo,
    t: DateTime<Utc>,
    garbage: bool,
    lag_ms: i64,
) -> Planned {
    let fetched = t + Duration::milliseconds(lag_ms);
    let stamp = t.format("%Y%m%dT%H%M%SZ").to_string();
    let day = t.format("%Y-%m-%d").to_string();
    let vp = &cam.viewpoint_id;
    let archived = kind != 0;
    let (rel_dir, stem, key, json) = if archived {
        let asset = format!("{i:08x}-0000-4000-8000-{i:012x}");
        let (icd, lts) = if garbage {
            ("13/45/2026 99:99:99 XM".to_string(), "never".to_string())
        } else {
            nps_stamps(t, cam.tz)
        };
        // Downloaded during the sealed window: a backfill's fetch is not the frame's date.
        let json = serde_json::json!({
            "viewpoint_id": vp, "source": "nps-archive", "kind": "hires", "asset_id": asset,
            "image_create_datetime_raw": icd, "local_time_string": lts,
            "image_time_utc": format!("{}", t.format("%Y-%m-%dT%H:%M:%SZ")),
            "fetched_at_utc": "2026-12-20T11:36:11.122Z",
            "image_time_source": "nps_archive_label", "stored_width": 999, "stored_height": 562,
            "chroma_spread": 20.0, "flags": [], "capture_version": "snow-globe-capture/0.3.0"
        });
        (
            format!("archive/{vp}/{day}"),
            format!("{stamp}_{:08x}", i),
            format!("archive:{vp}/{asset}"),
            json,
        )
    } else {
        let stem = format!("{stamp}_{i:012x}");
        let json = serde_json::json!({
            "viewpoint_id": vp, "source": "live", "variant": "thumb",
            "image_time_utc": format!("{}", t.format("%Y-%m-%dT%H:%M:%SZ")),
            "fetched_at_utc": format!("{}", fetched.format("%Y-%m-%dT%H:%M:%S%.3fZ")),
            "image_time_source": "exif_local", "stored_width": 640, "stored_height": 360,
            "chroma_spread": 20.0, "flags": [], "decode_error": null,
            "capture_version": "snow-globe-capture/0.3.0"
        });
        (
            format!("thumbs/{vp}/{day}"),
            stem.clone(),
            format!("live:{vp}/{stem}"),
            json,
        )
    };
    let folder = dir.join(&rel_dir);
    std::fs::create_dir_all(&folder).expect("mkdir");
    std::fs::write(folder.join(format!("{stem}.json")), json.to_string()).expect("sidecar");
    std::fs::write(folder.join(format!("{stem}.jpg")), b"").expect("image");
    Planned {
        key,
        viewpoint_id: vp.clone(),
        archived,
        excluded: oracle_excludes(archived, garbage, cam, t, fetched),
        t,
        available: if archived { t } else { fetched },
    }
}

/// A frame time for a kind: live around the sealed window, archive around the 1 Jun edge.
fn frame_time(kind: u8, near_edge: bool, u: u32) -> DateTime<Utc> {
    let (from, span_s) = match (kind != 0, near_edge) {
        (false, true) => (Utc.with_ymd_and_hms(2026, 12, 12, 0, 0, 0), 38 * 86_400),
        (false, false) => (Utc.with_ymd_and_hms(2026, 11, 15, 0, 0, 0), 105 * 86_400),
        (true, true) => (Utc.with_ymd_and_hms(2026, 5, 29, 0, 0, 0), 6 * 86_400),
        (true, false) => (Utc.with_ymd_and_hms(2025, 6, 1, 0, 0, 0), 456 * 86_400),
    };
    // Whole minutes, as the archive's 15-minute cadence and the stamps' clocks have.
    let minutes = i64::from(u) % (span_s / 60);
    from.single().expect("t") + Duration::minutes(minutes)
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(48))]

    #[test]
    fn an_excluded_frame_is_never_returned(
        frames in prop::collection::vec(
            (0u8..2, 0usize..8, any::<u32>(), any::<bool>(), 0u8..10, 0i64..(40 * 60_000)),
            1..30,
        ),
        queries in prop::collection::vec((0usize..8, any::<u32>()), 12),
        cut_offs in prop::collection::vec((any::<u32>(), 0i64..(45 * 60_000)), 6),
    ) {
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut planned = Vec::new();
        for (i, (kind, vp, u, near_edge, garbage_roll, lag_ms)) in frames.iter().enumerate() {
            let cam = &cams()[*vp];
            let t = frame_time(*kind, *near_edge, *u);
            let garbage = *kind != 0 && *garbage_roll == 0;
            planned.push(write_sidecar(tmp.path(), i, *kind, cam, t, garbage, *lag_ms));
        }
        let lib = Library::build(
            tmp.path(),
            CameraTable::embedded(),
            HeldOutRules::committed().expect("split"),
            CacheMode::Off,
        )
        .expect("builds");
        let excluded: std::collections::HashSet<&str> =
            planned.iter().filter(|p| p.excluded).map(|p| p.key.as_str()).collect();
        for e in lib.entries() {
            prop_assert!(!excluded.contains(e.key.as_str()), "{} is in the library", e.key);
        }
        let edge = Utc.with_ymd_and_hms(2026, 5, 31, 0, 0, 0).single().expect("t");
        for p in &planned {
            let must_be_in = !p.excluded && (!p.archived || p.t < edge);
            if must_be_in {
                prop_assert!(lib.by_key(&p.key).is_some(), "{} is missing", p.key);
            }
        }
        let span = Utc.with_ymd_and_hms(2027, 3, 1, 0, 0, 0).single().expect("t")
            - Utc.with_ymd_and_hms(2025, 6, 1, 0, 0, 0).single().expect("t");
        for (vp, u) in &queries {
            let t = Utc.with_ymd_and_hms(2025, 6, 1, 0, 0, 0).single().expect("t")
                + Duration::seconds(i64::from(*u) % span.num_seconds());
            let mut req = MatchRequest::new(&cams()[*vp].viewpoint_id, t);
            req.alternatives = 5;
            req.max_distance = f64::INFINITY;
            if let MatchOutcome::Matched(m) = find(&lib, CameraTable::embedded(), &req) {
                for s in std::iter::once(&m.best).chain(&m.alternatives) {
                    prop_assert!(!excluded.contains(s.key.as_str()), "{} returned", s.key);
                }
            }
        }
        // Cut-offs just after a frame was taken (0–45 min): the matcher sees exactly the frames
        // the oracle says were taken before the cut-off and fetched by it.
        let by_key: std::collections::HashMap<&str, &Planned> =
            planned.iter().map(|p| (p.key.as_str(), p)).collect();
        for (u, after_ms) in &cut_offs {
            let p = &planned[*u as usize % planned.len()];
            let as_of = p.t + Duration::milliseconds(*after_ms);
            let mut req = MatchRequest::new(&p.viewpoint_id, as_of);
            req.as_of = Some(as_of);
            req.alternatives = 30;
            req.max_distance = f64::INFINITY;
            let seen = |q: &Planned| q.t < as_of && q.available <= as_of;
            let expected = lib
                .viewpoint_indexes(&p.viewpoint_id)
                .iter()
                .filter(|&&i| by_key.get(lib.entries()[i].key.as_str()).is_some_and(|q| seen(q)))
                .count();
            match find(&lib, CameraTable::embedded(), &req) {
                MatchOutcome::Matched(m) => {
                    prop_assert_eq!(m.candidates, expected);
                    for s in std::iter::once(&m.best).chain(&m.alternatives) {
                        let q = by_key[s.key.as_str()];
                        prop_assert!(seen(q), "{} returned at {}, available {}", s.key, as_of, q.available);
                    }
                }
                MatchOutcome::NoFrame(n) => prop_assert_eq!(expected, 0, "{:?}", n.reason),
            }
        }
    }

    #[test]
    fn the_guard_keeps_the_instant(
        minute in 0i64..(8 * 24 * 60),
        vp in 0usize..8,
        mutation in 0u8..6,
    ) {
        let cam = &cams()[vp];
        let t = Utc.with_ymd_and_hms(2026, 5, 28, 0, 0, 0).single().expect("t")
            + Duration::minutes(minute);
        let (icd, lts) = nps_stamps(t, cam.tz);
        let (icd, lts) = match mutation {
            0 => (icd, lts),
            1 => (icd, String::new()),
            2 => (String::new(), lts),
            3 => (icd.replace(" PM", " AM").replace(" AM", " PM"), lts),
            4 => ("garbage".to_string(), "garbage".to_string()),
            _ => (icd[..10].to_string(), lts[..lts.find(' ').unwrap_or(lts.len())].to_string()),
        };
        let dates = archive_dates(Some(&icd), Some(&lts), &[t], &[], cam.tz);
        let verdict = HeldOutRules::committed().expect("split").archive_verdict(&dates);
        let truly_out = t.with_timezone(&cam.tz).date_naive() >= d(2026, 6, 1)
            || t.date_naive() >= d(2026, 6, 1);
        if truly_out {
            prop_assert!(verdict != GuardVerdict::Allowed, "{icd:?} {lts:?} at {t} allowed");
        }
        if mutation == 0 {
            // Stamps as NPS writes them: the verdict is exactly the true dates'.
            prop_assert_eq!(verdict == GuardVerdict::Allowed, !truly_out, "{} {} at {}", icd, lts, t);
        }
    }
}
