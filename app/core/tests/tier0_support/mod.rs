//! Shared set-up for the Tier 0 integration tests: the committed sidecar fixture
//! (`tests/fixtures/tier0/data/`, sidecar JSON only) copied into a temporary data dir.
//!
//! - Beside every sidecar the copy gets a small synthetic JPEG (`tests/fixtures/tier0/jpegs.rs`),
//!   except [`MISSING_IMAGE`], so that sidecar tests `missing_image`, and [`LINKED_IMAGE`], whose
//!   image is made a symbolic link into the data dir's `quarantine/` folder. The index itself
//!   never opens an image.
//! - The quarantine folders are made here, at run time, so the repository holds no folder of that
//!   name (D-022): `quarantine/` at the top of the data dir (the link's target, a 16×16 grey JPEG),
//!   `frames/quarantine/…/` and `archive/point-reyes.beach/quarantine/`, each with a synthetic
//!   sidecar ([`QUARANTINED`]). They are made unreadable (mode 000), and so is the sidecar in the
//!   unknown viewpoint's folder: had the scanner or a decode tried to list, read or follow into
//!   them, the build would fail or count a `bad_sidecar`, or the decode would fail.
//!   [`Fixture`] restores the modes before the folder is removed.
#![allow(dead_code)]

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

#[path = "../fixtures/tier0/jpegs.rs"]
pub mod jpegs;

/// The sidecar left without an image (`missing_image`).
pub const MISSING_IMAGE: &str =
    "thumbs/point-reyes.beach/2026-11-22/20261122T231500Z_373b22a9d7dd.json";

/// The sidecar whose image is a symbolic link into `quarantine/` (`symlink`).
pub const LINKED_IMAGE: &str =
    "thumbs/point-reyes.beach/2026-11-20/20261120T200000Z_b0000000000c.json";

/// Where [`LINKED_IMAGE`]'s link points, relative to the data dir.
pub const LINK_TARGET: &str = "quarantine/linked-target.jpg";

/// Sidecars made inside quarantine folders at run time: never to be opened.
pub const QUARANTINED: &[&str] = &[
    "frames/quarantine/held-out-breach-2026-11-19/20260715T200000Z_b00000000010.json",
    "archive/point-reyes.beach/quarantine/20260715T200000Z_b00000000011.json",
];

/// Folders and files made unreadable: never to be opened.
pub const LOCKED: &[&str] = &[
    "quarantine",
    "frames/quarantine",
    "archive/point-reyes.beach/quarantine",
    "frames/golden-gate.bridge/2026-11-22/20261122T200000Z_b00000000009.json",
];

/// The committed fixture.
pub fn fixture_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/tier0/data")
}

/// A temporary data dir holding the fixture.
#[derive(Debug)]
pub struct Fixture {
    pub dir: tempfile::TempDir,
}

impl Fixture {
    /// The data dir.
    pub fn path(&self) -> &Path {
        self.dir.path()
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        for rel in LOCKED {
            let p = self.dir.path().join(rel);
            let mode = if p.is_file() { 0o644 } else { 0o755 };
            let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(mode));
        }
    }
}

fn copy_tree(from: &Path, to: &Path, root: &Path) {
    std::fs::create_dir_all(to).expect("mkdir");
    for entry in std::fs::read_dir(from).expect("fixture dir") {
        let entry = entry.expect("entry");
        let src = entry.path();
        let dst = to.join(entry.file_name());
        if entry.file_type().expect("type").is_dir() {
            copy_tree(&src, &dst, root);
        } else {
            std::fs::copy(&src, &dst).expect("copy");
            let name = entry.file_name().to_string_lossy().into_owned();
            let rel = src
                .strip_prefix(root)
                .expect("inside the fixture")
                .to_string_lossy()
                .into_owned();
            if let Some(stem) = name.strip_suffix(".json")
                && !name.starts_with('.')
            {
                let image = to.join(format!("{stem}.jpg"));
                if rel == LINKED_IMAGE {
                    // thumbs/<viewpoint>/<day>/ is three folders below the data dir.
                    std::os::unix::fs::symlink(Path::new("../../..").join(LINK_TARGET), &image)
                        .expect("link");
                } else if rel != MISSING_IMAGE {
                    std::fs::write(image, jpegs::GRADIENT_48X32).expect("image");
                }
            }
        }
    }
}

/// A synthetic sidecar for a quarantine folder, dated in summer 2026 (made at run time).
fn quarantined_sidecar(rel: &str) -> String {
    let stem = Path::new(rel)
        .file_stem()
        .and_then(|s| s.to_str())
        .expect("stem");
    serde_json::json!({
        "viewpoint_id": "point-reyes.beach",
        "source": "live",
        "image_time_utc": "2026-07-15T20:00:00Z",
        "image_time_source": "exif_local",
        "fetched_at_utc": "2026-07-15T20:15:30.384Z",
        "stored_width": 640,
        "stored_height": 360,
        "flags": [],
        "decode_error": null,
        "capture_version": "snow-globe-capture/0.3.0",
        "fixture_note": format!(
            "synthetic, made at test time: {stem} under a quarantine folder; never opened (D-022)"
        ),
    })
    .to_string()
}

/// Copy the fixture into a new temporary data dir, and make and lock the quarantine folders
/// (module docs).
pub fn materialise() -> Fixture {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = fixture_dir();
    copy_tree(&root, dir.path(), &root);
    let target = dir.path().join(LINK_TARGET);
    std::fs::create_dir_all(target.parent().expect("parent")).expect("mkdir");
    std::fs::write(&target, jpegs::GREY_16X16).expect("link target");
    for rel in QUARANTINED {
        let path = dir.path().join(rel);
        std::fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
        std::fs::write(&path, quarantined_sidecar(rel)).expect("sidecar");
        std::fs::write(path.with_extension("jpg"), jpegs::GRADIENT_48X32).expect("image");
    }
    for rel in LOCKED {
        std::fs::set_permissions(dir.path().join(rel), std::fs::Permissions::from_mode(0o000))
            .expect("chmod");
    }
    Fixture { dir }
}
