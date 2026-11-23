//! Tier 0's decode path on synthetic JPEGs (PRD §9.1 "Latency"): the decoder, the box filter,
//! the 48 h prefetch plan, background decode with cancellation, and the swap.

mod tier0_support;

use std::path::Path;
use std::sync::Arc;

use chrono::{DateTime, Duration, TimeZone, Utc};
use snow_globe_core::lifecycle::GenCounter;
use snow_globe_core::solar::sun_position;
use snow_globe_core::tier0::camera::CameraTable;
use snow_globe_core::tier0::decode::{
    DISPLAY_MAX_WIDTH, DecodedSet, decode_entries, decode_jpeg, first_ready, plan_prefetch,
    ready_image, spawn_prefetch,
};
use snow_globe_core::tier0::library::{ColourMode, Entry, EntrySource, ImageRef, Library};
use tier0_support::jpegs;

#[test]
fn decodes_rgb_and_keeps_small_frames_at_their_size() {
    let (img, _, resize_ms) = decode_jpeg(jpegs::GRADIENT_48X32, DISPLAY_MAX_WIDTH).expect("jpeg");
    assert_eq!((img.width, img.height), (48, 32));
    assert_eq!((img.source_width, img.source_height), (48, 32));
    assert_eq!(img.rgb.len(), 48 * 32 * 3);
    assert_eq!(resize_ms, 0.0);
    // The gradient survives JPEG at quality 90: R = 5x, G = 7y, B = 128, within a few levels.
    for (x, y) in [(4usize, 4usize), (40, 4), (20, 28)] {
        let p = &img.rgb[(y * 48 + x) * 3..][..3];
        let want = [5 * x as i32, 7 * y as i32, 128];
        for c in 0..3 {
            assert!(
                (i32::from(p[c]) - want[c]).abs() <= 12,
                "pixel ({x},{y}) channel {c}: {} vs {}",
                p[c],
                want[c]
            );
        }
    }
}

#[test]
fn a_frame_twice_the_display_width_is_box_filtered() {
    let (img, _, _) = decode_jpeg(jpegs::WIDE_1280X8, DISPLAY_MAX_WIDTH).expect("jpeg");
    assert_eq!((img.source_width, img.source_height), (1280, 8));
    assert_eq!((img.width, img.height), (640, 4));
    // R = x / 5 at full size, so the output pixel at x ≈ (2x + 0.5) / 5.
    let p = &img.rgb[(2 * 640 + 300) * 3..][..3];
    assert!((i32::from(p[0]) - 120).abs() <= 8, "{p:?}");
    assert!((i32::from(p[1]) - 100).abs() <= 8, "{p:?}");
}

#[test]
fn a_greyscale_jpeg_comes_out_as_grey_rgb() {
    let (img, _, _) = decode_jpeg(jpegs::GREY_16X16, DISPLAY_MAX_WIDTH).expect("jpeg");
    assert_eq!(img.rgb.len(), 16 * 16 * 3);
    assert!(
        img.rgb
            .as_chunks::<3>()
            .0
            .iter()
            .all(|p| p[0] == p[1] && p[1] == p[2])
    );
}

fn library_with_images(dir: &Path, n: usize, missing: usize) -> Library {
    let cam = CameraTable::embedded()
        .get("point-reyes.beach")
        .expect("camera");
    let t0: DateTime<Utc> = Utc
        .with_ymd_and_hms(2026, 11, 20, 0, 0, 0)
        .single()
        .expect("t");
    let folder = dir.join("thumbs/point-reyes.beach/2026-11-20");
    std::fs::create_dir_all(&folder).expect("mkdir");
    let entries = (0..n)
        .map(|i| {
            let t = t0 + Duration::minutes(15 * i as i64);
            let stem = format!("{}_{i:012x}", t.format("%Y%m%dT%H%M%SZ"));
            if i >= missing {
                std::fs::write(folder.join(format!("{stem}.jpg")), jpegs::GRADIENT_48X32)
                    .expect("image");
            }
            let sun = sun_position(&cam.site, t);
            Entry::new(
                format!("live:point-reyes.beach/{stem}"),
                cam,
                EntrySource::Live,
                t,
                sun.geometric_altitude_deg,
                sun.azimuth_deg,
                ImageRef {
                    rel_path: format!("thumbs/point-reyes.beach/2026-11-20/{stem}.jpg"),
                    width: Some(48),
                    height: Some(32),
                },
                ColourMode::Colour,
            )
        })
        .collect();
    Library::from_entries(dir, entries)
}

#[test]
fn the_prefetch_plan_covers_48_hours_and_decodes_in_the_background() {
    let tmp = tempfile::tempdir().expect("tempdir");
    // Two days of frames every 15 minutes, the first three without an image on disk.
    let lib = Arc::new(library_with_images(tmp.path(), 192, 3));
    let now: DateTime<Utc> = Utc
        .with_ymd_and_hms(2026, 11, 22, 0, 0, 0)
        .single()
        .expect("t");
    let plan = plan_prefetch(
        &lib,
        CameraTable::embedded(),
        "point-reyes.beach",
        now,
        48,
        5,
        Some(now),
    );
    assert_eq!(plan.grid_points, 48 * 12 + 1);
    assert_eq!(plan.no_frame_points, 0);
    assert!(!plan.indexes.is_empty() && plan.indexes.len() <= 192);
    assert!(
        plan.indexes.windows(2).all(|w| w[0] < w[1]),
        "ascending, no repeats"
    );
    // The default grid is every whole minute: a start 30 s past the minute begins at the minute,
    // and the finer grid can only add frames to the plan.
    let fine = plan_prefetch(
        &lib,
        CameraTable::embedded(),
        "point-reyes.beach",
        now + Duration::seconds(30),
        48,
        snow_globe_core::tier0::decode::PREFETCH_STEP_MIN,
        Some(now),
    );
    assert_eq!(fine.from, now);
    assert_eq!(fine.grid_points, 48 * 60 + 1);
    assert!(plan.indexes.iter().all(|i| fine.indexes.contains(i)));

    // Background decode of every frame (missing images reported, not fatal); each is opened by
    // its checked path, whose check is timed.
    let set = Arc::new(DecodedSet::new());
    let all: Vec<usize> = (0..lib.entries().len()).collect();
    let report = spawn_prefetch(Arc::clone(&lib), all.clone(), 2, Arc::clone(&set), None)
        .join()
        .expect("prefetch thread");
    assert_eq!(report.decoded.len(), 189);
    assert_eq!(report.failed.len(), 3);
    assert!(
        report
            .decoded
            .iter()
            .all(|d| d.timing.check_ms > 0.0 && d.timing.total_ms >= d.timing.check_ms)
    );
    assert!(!report.cancelled);
    assert_eq!(set.len(), 189);
    assert_eq!(set.bytes(), 189 * 48 * 32 * 3);
    // A second pass finds them decoded.
    let again = decode_entries(&lib, &all, 2, &set, None);
    assert_eq!(again.already_decoded, 189);
    assert!(again.decoded.is_empty());

    // The swap: a hit needs no decode; a miss decodes on the spot and is reported as one.
    let (img, timing) = ready_image(&lib, &set, 100).expect("hit");
    assert!(timing.is_none());
    assert_eq!((img.width, img.height), (48, 32));
    let fresh = DecodedSet::new();
    let (_, timing) = ready_image(&lib, &fresh, 100).expect("miss");
    assert!(timing.is_some_and(|t| t.total_ms >= t.decode_ms));
    assert!(ready_image(&lib, &fresh, 0).is_err(), "no image on disk");

    // A match's frames, closest first: the first that decodes is taken, the others reported.
    let (ready, skipped) = first_ready(&lib, &fresh, &[0, 1, 5, 6]);
    let ready = ready.expect("the third decodes");
    assert_eq!((ready.position, ready.index), (2, 5));
    assert_eq!(skipped.len(), 2);
    assert!(
        skipped.iter().all(|s| s.error.starts_with("refused")),
        "{skipped:?}"
    );
    let (none, skipped) = first_ready(&lib, &fresh, &[0, 1, 2]);
    assert!(none.is_none() && skipped.len() == 3);
}

#[test]
fn a_new_query_cancels_the_prefetch() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let lib = Arc::new(library_with_images(tmp.path(), 50, 0));
    let generations = GenCounter::new();
    let (_, token) = generations.begin();
    let _ = generations.begin(); // a new key press: the old generation's work stops
    let set = Arc::new(DecodedSet::new());
    let report = spawn_prefetch(
        Arc::clone(&lib),
        (0..50).collect(),
        2,
        Arc::clone(&set),
        Some(token),
    )
    .join()
    .expect("thread");
    assert!(report.cancelled);
    assert!(report.decoded.is_empty());
    assert!(set.is_empty());
}
