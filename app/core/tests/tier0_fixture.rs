//! Tier 0 over the committed sidecar fixture (`tests/fixtures/tier0/data/`, JSON only; PRD §9.1).
//!
//! The fixture holds real sidecars copied from the data dir (Rainier, Point Reyes and Grand Canyon
//! captures of 19–22 Nov 2026, and archive records from before 1 Jun 2026) and synthetic ones made
//! from them for each rule: the archive edge on either side of UTC midnight on 31 May, summer 2026,
//! the sealed window's edges, Rainier's own-captures rule, unreadable stamps, NPS's own archive
//! field names outside `archive/`, other framings, unusable and missing images, an image that is a
//! link into `quarantine/`, a mismatched or broken sidecar, a folder that is not a viewpoint and a
//! stray file. Each synthetic sidecar says what it is in its `fixture_note`. The quarantine folders
//! themselves are made at run time (`tier0_support`), never committed.

mod tier0_support;

use std::collections::BTreeMap;
use std::path::Path;

use chrono::{DateTime, Duration, TimeZone, Utc};
use snow_globe_core::tier0::ask;
use snow_globe_core::tier0::camera::CameraTable;
use snow_globe_core::tier0::decode::{DISPLAY_MAX_WIDTH, DecodeError, decode_image};
use snow_globe_core::tier0::heldout::HeldOutRules;
use snow_globe_core::tier0::library::{
    CACHE_REL_PATH, CacheMode, ColourMode, EntrySource, Library,
};
use snow_globe_core::tier0::matcher::{MatchOutcome, MatchRequest, find};
use snow_globe_core::tier0::sidecar::Exclusion;

fn build(dir: &Path, mode: CacheMode) -> Library {
    Library::build(
        dir,
        CameraTable::embedded(),
        HeldOutRules::committed().expect("split"),
        mode,
    )
    .expect("the fixture builds")
}

fn keys(lib: &Library, vp: &str) -> Vec<String> {
    lib.viewpoint_indexes(vp)
        .iter()
        .map(|&i| lib.entries()[i].key.clone())
        .collect()
}

fn excluded(lib: &Library, vp: &str) -> BTreeMap<Exclusion, usize> {
    lib.report().viewpoints[vp].excluded.clone()
}

fn utc(y: i32, m: u32, d: u32, h: u32, mi: u32) -> DateTime<Utc> {
    Utc.with_ymd_and_hms(y, m, d, h, mi, 0).single().expect("t")
}

/// The keys of every synthetic or real sidecar the rules exclude: never in the library.
const EXCLUDED_KEYS: &[&str] = &[
    "live:rainier.paradise-mountain/20261215T075900Z_b00000000002",
    "live:rainier.paradise-mountain/20261220T200000Z_b00000000003",
    "archive:rainier.paradise-mountain/a0000007-0000-4000-8000-000000000007",
    "archive:point-reyes.beach/a0000003-0000-4000-8000-000000000003",
    "archive:point-reyes.beach/a0000004-0000-4000-8000-000000000004",
    "archive:grand-canyon.yavapai/a0000006-0000-4000-8000-000000000006",
    "live:point-reyes.beach/20261122T231500Z_373b22a9d7dd",
    "live:point-reyes.beach/20261121T203000Z_b00000000008",
    "live:point-reyes.beach/20261120T200000Z_b0000000000c",
    "archive:point-reyes.beach/d0000000-0000-4000-8000-00000000000d",
];

#[test]
fn the_index_holds_exactly_the_frames_that_pass() {
    let fx = tier0_support::materialise();
    let lib = build(fx.path(), CacheMode::Off);
    let r = lib.report();
    assert_eq!(r.sidecars, 31, "sidecars under the known viewpoints");
    assert_eq!(r.entries, 12);
    assert_eq!(
        r.split_version,
        HeldOutRules::committed().expect("split").split_version
    );

    assert_eq!(
        keys(&lib, "rainier.paradise-mountain"),
        [
            "live:rainier.paradise-mountain/20261119T063013Z_edf5e91d5e3f",
            "live:rainier.paradise-mountain/20261122T000859Z_49c9b6ffe52c",
            "live:rainier.paradise-mountain/20261214T235900Z_b00000000001",
            "live:rainier.paradise-mountain/20270115T080000Z_b00000000004",
        ]
    );
    assert_eq!(
        excluded(&lib, "rainier.paradise-mountain"),
        BTreeMap::from([
            (Exclusion::SealedWindow, 2),
            (Exclusion::OwnCapturesOnly, 1)
        ])
    );
    assert_eq!(
        keys(&lib, "point-reyes.beach"),
        [
            "archive:point-reyes.beach/655839de-ea90-4435-9c28-67d2e646fa23",
            "archive:point-reyes.beach/a0000001-0000-4000-8000-000000000001",
            "archive:point-reyes.beach/a0000002-0000-4000-8000-000000000002",
            "live:point-reyes.beach/20261122T104500Z_b00000000005",
            "live:point-reyes.beach/20261122T233000Z_f60b95856be1",
        ]
    );
    assert_eq!(
        excluded(&lib, "point-reyes.beach"),
        BTreeMap::from([
            (Exclusion::HeldOutArchive, 3),
            (Exclusion::ViewpointMismatch, 1),
            (Exclusion::MissingImage, 1),
            (Exclusion::Unusable, 3),
            (Exclusion::TooSmall, 1),
            (Exclusion::OtherFraming, 1),
            (Exclusion::BadSidecar, 1),
            (Exclusion::Misplaced, 1),
            (Exclusion::Symlink, 1),
            (Exclusion::Quarantine, 1),
        ])
    );
    // The image linked into quarantine/ (a locked folder) refused its sidecar unread.
    assert_eq!(
        lib.report().viewpoints["point-reyes.beach"].examples[&Exclusion::Symlink],
        [tier0_support::LINKED_IMAGE]
    );
    assert_eq!(
        keys(&lib, "grand-canyon.yavapai"),
        [
            "archive:grand-canyon.yavapai/a0000005-0000-4000-8000-000000000005",
            "live:grand-canyon.yavapai/20261122T000000Z_a31f9602e162",
            "live:grand-canyon.yavapai/20261122T001500Z_98bc567c62d1",
        ]
    );
    assert_eq!(
        excluded(&lib, "grand-canyon.yavapai"),
        BTreeMap::from([(Exclusion::UnreadableArchiveDate, 1)])
    );
    for vp in [
        "acadia.mcfarland",
        "big-bend.panther-junction",
        "grand-teton.science-center",
        "smokies.look-rock",
        "yosemite.turtleback",
    ] {
        assert!(keys(&lib, vp).is_empty(), "{vp}");
        assert!(excluded(&lib, vp).is_empty(), "{vp}");
    }
    // Paths refused unread: the unknown folder's sidecar was counted, never opened (its mode is
    // 000, so a read would have been a bad_sidecar), and the quarantine folder was never listed.
    assert_eq!(
        r.unknown_folders,
        BTreeMap::from([("frames/golden-gate.bridge".to_string(), 1)])
    );
    assert_eq!(
        r.paths_refused,
        BTreeMap::from([(Exclusion::Quarantine, 1)])
    );
    for key in EXCLUDED_KEYS {
        assert!(
            lib.by_key(key).is_none(),
            "{key} must not be in the library"
        );
    }

    // A live capture with a frame and a thumbnail is one entry showing the thumbnail.
    let (_, pr) = lib
        .by_key("live:point-reyes.beach/20261122T233000Z_f60b95856be1")
        .expect("entry");
    assert_eq!(
        pr.image.rel_path,
        "thumbs/point-reyes.beach/2026-11-22/20261122T233000Z_f60b95856be1.jpg"
    );
    assert_eq!(
        pr.full_frame.as_ref().map(|f| f.rel_path.as_str()),
        Some("frames/point-reyes.beach/2026-11-22/20261122T233000Z_f60b95856be1.jpg")
    );
    assert_eq!(pr.source, EntrySource::Live);
    assert_eq!(pr.colour_mode, ColourMode::Colour);
    // Capture's NOAA calculator said 13.424° for this frame; the ephemeris agrees to 0.1°.
    assert!(
        (pr.sun_altitude_deg - 13.424).abs() < 0.1,
        "{}",
        pr.sun_altitude_deg
    );
    assert!(pr.licence.contains("National Park Service"));
    // The stopgap's frame has no thumbnail, so the frame itself is shown.
    let (_, stopgap) = lib
        .by_key("live:rainier.paradise-mountain/20261119T063013Z_edf5e91d5e3f")
        .expect("entry");
    assert!(stopgap.image.rel_path.starts_with("frames/"));
    assert_eq!(stopgap.colour_mode, ColourMode::Unknown);
    // The archive asset's hires copy is shown; its 100 px thumbmedium is excluded as too small.
    let (_, hires) = lib
        .by_key("archive:point-reyes.beach/655839de-ea90-4435-9c28-67d2e646fa23")
        .expect("entry");
    assert_eq!(hires.image.width, Some(999));
    assert_eq!(hires.source, EntrySource::NpsArchive);
    // Flags.
    let (_, night) = lib
        .by_key("live:point-reyes.beach/20261122T104500Z_b00000000005")
        .expect("entry");
    assert_eq!(night.colour_mode, ColourMode::Monochrome);
    assert!(night.stale);
    assert!(night.flags.contains(&"sun_below_3deg".to_string()));
    assert!(night.sun_altitude_deg < -18.0);
}

#[test]
fn the_cache_is_created_reused_refreshed_and_rebuilt() {
    let fx = tier0_support::materialise();
    let dir = fx.path();
    let off = build(dir, CacheMode::Off);
    let created = build(dir, CacheMode::Use);
    let status = |l: &Library| l.report().cache.as_ref().expect("cache").status.clone();
    assert_eq!(status(&created), "created");
    assert!(dir.join(CACHE_REL_PATH).is_file());
    let hit = build(dir, CacheMode::Use);
    assert_eq!(status(&hit), "hit");
    assert_eq!(hit.report().cache.as_ref().map(|c| c.reparsed), Some(0));
    // The cache gives exactly what a fresh read gives.
    assert_eq!(hit.entries(), off.entries());
    assert_eq!(hit.report().viewpoints, off.report().viewpoints);

    // A changed sidecar is read again; a removed one is dropped.
    let thumb =
        dir.join("thumbs/grand-canyon.yavapai/2026-11-22/20261122T001500Z_98bc567c62d1.json");
    let mut text = std::fs::read_to_string(&thumb).expect("read");
    text.push('\n');
    std::fs::write(&thumb, text).expect("write");
    let refreshed = build(dir, CacheMode::Use);
    assert_eq!(status(&refreshed), "refreshed");
    assert_eq!(
        refreshed.report().cache.as_ref().map(|c| c.reparsed),
        Some(1)
    );
    std::fs::remove_file(&thumb).expect("rm");
    let fewer = build(dir, CacheMode::Use);
    assert_eq!(fewer.report().cache.as_ref().map(|c| c.removed), Some(1));
    assert_eq!(fewer.entries().len(), off.entries().len() - 1);
    // An image that disappears takes its entry with it, though its sidecar did not change.
    std::fs::remove_file(
        dir.join("thumbs/grand-canyon.yavapai/2026-11-22/20261122T000000Z_a31f9602e162.jpg"),
    )
    .expect("rm");
    let no_thumb = build(dir, CacheMode::Use);
    let (_, e) = no_thumb
        .by_key("live:grand-canyon.yavapai/20261122T000000Z_a31f9602e162")
        .expect("the frame is still there");
    assert!(
        e.image.rel_path.starts_with("frames/"),
        "{}",
        e.image.rel_path
    );
    // A damaged cache is rebuilt, never trusted.
    std::fs::write(dir.join(CACHE_REL_PATH), b"{oops").expect("write");
    let rebuilt = build(dir, CacheMode::Use);
    assert_eq!(status(&rebuilt), "rebuilt");
    assert_eq!(rebuilt.entries(), no_thumb.entries());
    assert_eq!(status(&build(dir, CacheMode::Rebuild)), "rebuilt");
}

#[test]
fn held_out_frames_are_never_matched() {
    let fx = tier0_support::materialise();
    let lib = build(fx.path(), CacheMode::Off);
    let cams = CameraTable::embedded();
    // Every 3 h through the archive edge and summer, and through the sealed window.
    let spans = [
        (utc(2026, 5, 25, 0, 0), utc(2026, 9, 1, 0, 0)),
        (utc(2026, 12, 10, 0, 0), utc(2027, 1, 20, 0, 0)),
    ];
    let mut asked = 0;
    for vp in [
        "rainier.paradise-mountain",
        "point-reyes.beach",
        "grand-canyon.yavapai",
    ] {
        for (from, to) in spans {
            let mut t = from;
            while t < to {
                let mut req = MatchRequest::new(vp, t);
                req.alternatives = 10;
                req.max_distance = f64::INFINITY;
                if let MatchOutcome::Matched(m) = find(&lib, cams, &req) {
                    for s in std::iter::once(&m.best).chain(&m.alternatives) {
                        assert!(
                            !EXCLUDED_KEYS.contains(&s.key.as_str()),
                            "{vp} {t}: {}",
                            s.key
                        );
                    }
                }
                asked += 1;
                t += Duration::hours(3);
            }
        }
    }
    assert!(asked > 3 * 1000, "{asked}");
    // A summer 2026 afternoon at Point Reyes matches summer 2025, a year earlier, never 2026's.
    let req = MatchRequest::new("point-reyes.beach", utc(2026, 7, 15, 20, 15));
    let MatchOutcome::Matched(m) = find(&lib, cams, &req) else {
        panic!("a match")
    };
    assert_eq!(
        m.best.key,
        "archive:point-reyes.beach/655839de-ea90-4435-9c28-67d2e646fa23"
    );
}

fn args(a: &[&str]) -> Vec<String> {
    a.iter().map(|s| s.to_string()).collect()
}

#[test]
fn ask_end_to_end_on_the_fixture() {
    let fx = tier0_support::materialise();
    let dir = fx.path().to_string_lossy().into_owned();
    let opened = std::cell::RefCell::new(Vec::new());
    let opener = |p: &Path| {
        opened.borrow_mut().push(p.to_path_buf());
        Ok(())
    };
    let out = ask::run_with(
        &args(&[
            "point reyes tomorrow at 3 pm",
            "--now",
            "2026-11-23T12:00:00-08:00",
            "--data-dir",
            &dir,
            "--open",
            "--no-cache",
        ]),
        &opener,
    );
    assert_eq!(out.code, ask::EXIT_OK, "{out:?}");
    let v: serde_json::Value = serde_json::from_str(out.stdout.trim()).expect("one JSON object");
    assert!(
        out.stdout
            .starts_with(r#"{"status":"match","query":{"place_id":"point-reyes""#)
    );
    assert_eq!(v["query"]["valid_time"], "2026-11-24T15:00:00-08:00");
    let frame = &v["tier0"]["frame"];
    assert_eq!(
        frame["key"], "live:point-reyes.beach/20261122T233000Z_f60b95856be1",
        "the afternoon capture, not the night one or the summer archive"
    );
    assert!(
        frame["path"]
            .as_str()
            .expect("path")
            .ends_with("thumbs/point-reyes.beach/2026-11-22/20261122T233000Z_f60b95856be1.jpg")
    );
    let caption = v["tier0"]["caption"]["line1"].as_str().expect("caption");
    assert_eq!(
        caption,
        "REAL PHOTO, CLOSEST MATCH · taken Sun 22 Nov, 3:30 PM PST · matched on Sun position and \
         time of year · Courtesy National Park Service"
    );
    assert_eq!(
        v["tier0"]["matched_on"],
        serde_json::json!(["sun_altitude", "sun_azimuth", "day_of_year"])
    );
    assert_eq!(v["tier0"]["weather_class_used"], false);
    let d = v["tier0"]["distance"].as_f64().expect("distance");
    assert!(d < snow_globe_core::tier0::matcher::MAX_DISTANCE, "{d}");
    assert_eq!(v["decode"]["decoded_px"], serde_json::json!([48, 32]));
    assert_eq!(v["decode"]["prefetched"], false);
    for stage in ["parse", "index", "match", "decode", "total"] {
        assert!(v["timings_ms"][stage].is_number(), "{stage}");
    }
    // --open opens the stored full frame.
    let opened = opened.borrow();
    assert_eq!(opened.len(), 1);
    assert!(
        opened[0]
            .ends_with("frames/point-reyes.beach/2026-11-22/20261122T233000Z_f60b95856be1.jpg")
    );
    assert_eq!(
        v["opened"]["path"].as_str().map(Path::new),
        Some(opened[0].as_path())
    );
    drop(opened);

    // A follow-up keeps the place (--context): midnight matches the infrared night frame, and
    // the caption says it is one.
    let out = ask::run_with(
        &args(&[
            "what about midnight",
            "--now",
            "2026-11-23T12:00:00-08:00",
            "--context",
            "point-reyes",
            "--data-dir",
            &dir,
            "--no-cache",
        ]),
        &opener,
    );
    assert_eq!(out.code, ask::EXIT_OK, "{out:?}");
    let v: serde_json::Value = serde_json::from_str(out.stdout.trim()).expect("json");
    assert_eq!(v["query"]["place_source"], "context");
    assert_eq!(
        v["tier0"]["frame"]["key"],
        "live:point-reyes.beach/20261122T104500Z_b00000000005"
    );
    assert_eq!(v["tier0"]["frame"]["colour_mode"], "monochrome");
    assert_eq!(
        v["tier0"]["matched_on"],
        serde_json::json!(["sun_altitude", "day_of_year"]),
        "the Sun's direction is not matched at night"
    );
    assert!(
        v["tier0"]["caption"]["line2"]
            .as_str()
            .is_some_and(|l| l.contains("infrared night frame")),
        "{}",
        v["tier0"]["caption"]["line2"]
    );
    assert_eq!(
        v["opened"],
        serde_json::Value::Null,
        "no --open, nothing opened"
    );
    let bad = ask::run_with(
        &args(&[
            "what about midnight",
            "--context",
            "atlantis",
            "--data-dir",
            &dir,
        ]),
        &opener,
    );
    assert_eq!(bad.code, ask::EXIT_USAGE);

    // Asked before any Rainier frame was taken: no frame qualifies, and it says so plainly.
    let out = ask::run_with(
        &args(&[
            "rainier tomorrow at noon",
            "--now",
            "2026-11-18T12:00:00-08:00",
            "--data-dir",
            &dir,
            "--no-cache",
        ]),
        &opener,
    );
    assert_eq!(out.code, ask::EXIT_NO_FRAME, "{out:?}");
    let v: serde_json::Value = serde_json::from_str(out.stdout.trim()).expect("json");
    assert_eq!(v["status"], "no_frame");
    assert_eq!(v["tier0"]["reason"], "none_available");
    assert!(
        out.stderr.starts_with("No real photo qualifies"),
        "{}",
        out.stderr
    );
}

/// `snow-globe-ask` on the fixture with a recording opener: (exit code, JSON, paths opened).
fn ask_json(dir: &Path, words: &[&str]) -> (i32, serde_json::Value, Vec<std::path::PathBuf>) {
    let opened = std::cell::RefCell::new(Vec::new());
    let opener = |p: &Path| {
        opened.borrow_mut().push(p.to_path_buf());
        Ok(())
    };
    let dir = dir.to_string_lossy().into_owned();
    let mut argv: Vec<String> = words.iter().map(|s| s.to_string()).collect();
    argv.extend(["--data-dir".to_string(), dir, "--no-cache".to_string()]);
    let out = ask::run_with(&argv, &opener);
    let v = serde_json::from_str(out.stdout.trim()).unwrap_or(serde_json::Value::Null);
    (out.code, v, opened.into_inner())
}

#[test]
fn a_frame_is_shown_only_once_it_was_fetched() {
    // The Point Reyes capture taken at 23:30 UTC on 22 Nov was fetched at 23:47:31.384.
    let fx = tier0_support::materialise();
    let key = "live:point-reyes.beach/20261122T233000Z_f60b95856be1";
    let (code, v, _) = ask_json(
        fx.path(),
        &["point reyes now", "--now", "2026-11-22T23:35:00Z"],
    );
    assert!(code == ask::EXIT_OK || code == ask::EXIT_NO_FRAME, "{v}");
    assert_ne!(
        v["tier0"]["frame"]["key"], key,
        "taken, but not yet fetched"
    );
    let (code, v, _) = ask_json(
        fx.path(),
        &["point reyes now", "--now", "2026-11-22T23:48:00Z"],
    );
    assert_eq!(code, ask::EXIT_OK, "{v}");
    assert_eq!(v["tier0"]["frame"]["key"], key);
    assert_eq!(
        v["tier0"]["frame"]["available_utc"],
        "2026-11-22T23:47:31.384Z"
    );
    assert_eq!(v["tier0"]["rank"], 0);
}

#[test]
fn links_and_odd_images_are_never_followed() {
    let fx = tier0_support::materialise();
    let dir = fx.path();
    // A folder where an image should be is no image.
    let odd = "thumbs/grand-canyon.yavapai/2026-11-22/20261122T001500Z_98bc567c62d1";
    std::fs::remove_file(dir.join(format!("{odd}.jpg"))).expect("rm");
    std::fs::create_dir(dir.join(format!("{odd}.jpg"))).expect("mkdir");
    // A stored full frame that is a link into quarantine/: its sidecar is refused unread, so the
    // entry keeps only its thumbnail, and nothing (decode or --open) can reach the link.
    let full = dir.join("frames/grand-canyon.yavapai/2026-11-22/20261122T000000Z_a31f9602e162.jpg");
    std::fs::remove_file(&full).expect("rm");
    std::os::unix::fs::symlink(
        Path::new("../../..").join(tier0_support::LINK_TARGET),
        &full,
    )
    .expect("link");
    let lib = build(dir, CacheMode::Off);
    assert_eq!(
        excluded(&lib, "grand-canyon.yavapai"),
        BTreeMap::from([
            (Exclusion::UnreadableArchiveDate, 1),
            (Exclusion::MissingImage, 1),
            (Exclusion::Symlink, 1)
        ])
    );
    assert!(
        lib.by_key("live:grand-canyon.yavapai/20261122T001500Z_98bc567c62d1")
            .is_none()
    );
    let (_, kept) = lib
        .by_key("live:grand-canyon.yavapai/20261122T000000Z_a31f9602e162")
        .expect("the thumbnail's entry");
    assert!(kept.full_frame.is_none(), "{kept:?}");
    assert!(kept.image.rel_path.starts_with("thumbs/"));
    // An image replaced by a link after the index was built is refused when it is opened.
    let (_, e) = lib
        .by_key("live:point-reyes.beach/20261122T233000Z_f60b95856be1")
        .expect("entry");
    assert!(decode_image(&lib, &e.image, DISPLAY_MAX_WIDTH).is_ok());
    let shown = dir.join(&e.image.rel_path);
    std::fs::remove_file(&shown).expect("rm");
    std::os::unix::fs::symlink(
        Path::new("../../..").join(tier0_support::LINK_TARGET),
        &shown,
    )
    .expect("link");
    let err = decode_image(&lib, &e.image, DISPLAY_MAX_WIDTH).expect_err("a link");
    assert!(matches!(err, DecodeError::Refused(_)), "{err}");
    // Linked to another regular file of the library: still refused, as a link.
    std::fs::remove_file(&shown).expect("rm");
    std::os::unix::fs::symlink(
        dir.join("frames/point-reyes.beach/2026-11-22/20261122T233000Z_f60b95856be1.jpg"),
        &shown,
    )
    .expect("link");
    let err = decode_image(&lib, &e.image, DISPLAY_MAX_WIDTH).expect_err("a link");
    assert!(err.to_string().contains("symbolic link"), "{err}");
}

#[test]
fn an_unreadable_frame_falls_back_and_then_says_so() {
    let fx = tier0_support::materialise();
    let dir = fx.path();
    let best = "live:grand-canyon.yavapai/20261122T000000Z_a31f9602e162";
    let next = "live:grand-canyon.yavapai/20261122T001500Z_98bc567c62d1";
    let words = [
        "grand canyon tomorrow at 5 pm",
        "--now",
        "2026-11-23T12:00:00-07:00",
        "--open",
    ];
    let (code, v, opened) = ask_json(dir, &words);
    assert_eq!(code, ask::EXIT_OK, "{v}");
    assert_eq!(v["tier0"]["frame"]["key"], best);
    assert_eq!(opened.len(), 1);

    // Its shown image is damaged: the next closest frame is shown, and the skip is reported.
    let shown =
        dir.join("thumbs/grand-canyon.yavapai/2026-11-22/20261122T000000Z_a31f9602e162.jpg");
    let bytes = std::fs::read(&shown).expect("read");
    std::fs::write(&shown, &bytes[..bytes.len() / 3]).expect("truncate");
    let (code, v, opened) = ask_json(dir, &words);
    assert_eq!(code, ask::EXIT_OK, "{v}");
    assert_eq!(v["tier0"]["frame"]["key"], next);
    assert_eq!(v["tier0"]["rank"], 1);
    assert_eq!(v["decode"]["skipped"][0]["key"], best);
    assert!(
        v["decode"]["skipped"][0]["error"]
            .as_str()
            .is_some_and(|e| e.starts_with("JPEG")),
        "{v}"
    );
    assert!(
        v["tier0"]["caption"]["line1"]
            .as_str()
            .is_some_and(|c| c.contains("5:15 PM MST")),
        "the caption is the frame shown: {v}"
    );
    assert_eq!(opened.len(), 1);
    assert!(
        opened[0]
            .ends_with("thumbs/grand-canyon.yavapai/2026-11-22/20261122T001500Z_98bc567c62d1.jpg"),
        "--open opens the frame shown: {opened:?}"
    );

    // Every qualifying frame unreadable: it says so, exits 5 and opens nothing.
    let other =
        dir.join("thumbs/grand-canyon.yavapai/2026-11-22/20261122T001500Z_98bc567c62d1.jpg");
    std::fs::write(&other, b"not a jpeg").expect("damage");
    let (code, v, opened) = ask_json(dir, &words);
    assert_eq!(code, ask::EXIT_FRAME_UNREADABLE, "{v}");
    assert_eq!(v["status"], "frame_unreadable");
    assert!(
        v["tier0"]["message"]
            .as_str()
            .is_some_and(|m| m.starts_with("No real photo could be shown")),
        "{v}"
    );
    let tried: Vec<&str> = v["tier0"]["tried"]
        .as_array()
        .expect("tried")
        .iter()
        .filter_map(|t| t["key"].as_str())
        .collect();
    assert_eq!(tried[..2], [best, next]);
    assert!(opened.is_empty(), "nothing is opened: {opened:?}");
}
