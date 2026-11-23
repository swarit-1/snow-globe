//! Build-time embeds, so the crate needs no files at run time.
//!
//! 1. `viewpoints/manifests/*.json` for the parser's alias table (PRD §7.5). The generated
//!    `$OUT_DIR/manifests.rs` holds `(file stem, JSON text)` pairs via `include_str!`, so editing a
//!    manifest rebuilds the crate.
//! 2. eval's committed held-out splits for Tier 0 (D-085; `tier0::heldout`). The generated
//!    `$OUT_DIR/heldout_splits.rs` holds every `eval/src/snow_globe_eval/heldout_periods*.json` as
//!    `(file name, JSON text)` and eval's `CURRENT_SPLIT_VERSION`, read from `heldout.py`. Tier 0
//!    applies the split eval calls current, and refuses to run when a newer split file is
//!    committed beside it, so a new split version can never leave Tier 0 on the old rules.
//!    `heldout.py` and each split file are watched: a version bump edits `heldout.py`, which
//!    re-runs this script and picks up the new file. (The whole folder is not watched: every
//!    edit to eval's Python would then rebuild the crate.)

use std::path::{Path, PathBuf};
use std::{env, fs};

fn main() {
    let crate_dir =
        PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("cargo sets CARGO_MANIFEST_DIR"));
    let out_dir = PathBuf::from(env::var("OUT_DIR").expect("cargo sets OUT_DIR"));
    embed_manifests(&crate_dir, &out_dir);
    embed_heldout_splits(&crate_dir, &out_dir);
}

fn embed_manifests(crate_dir: &Path, out_dir: &Path) {
    let dir = crate_dir.join("../../viewpoints/manifests");
    println!("cargo:rerun-if-changed={}", dir.display());
    let mut files: Vec<PathBuf> = fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", dir.display()))
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    assert!(!files.is_empty(), "no manifests in {}", dir.display());
    let mut out = String::from(
        "/// `(file stem, JSON text)` for each manifest in `viewpoints/manifests/` at build time.\n\
         pub static EMBEDDED_MANIFESTS: &[(&str, &str)] = &[\n",
    );
    for path in &files {
        println!("cargo:rerun-if-changed={}", path.display());
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .expect("UTF-8 file name");
        let abs = path.canonicalize().expect("manifest path resolves");
        out.push_str(&format!(
            "    ({:?}, include_str!({:?})),\n",
            stem,
            abs.display().to_string()
        ));
    }
    out.push_str("];\n");
    fs::write(out_dir.join("manifests.rs"), out).expect("write manifests.rs");
}

/// `CURRENT_SPLIT_VERSION = <n>` (or `CURRENT_SPLIT_VERSION: int = <n>`) at the start of a line.
fn current_split_version(py: &str) -> Option<u32> {
    py.lines().find_map(|line| {
        let rest = line.strip_prefix("CURRENT_SPLIT_VERSION")?.trim_start();
        let value = match rest.strip_prefix(':') {
            Some(annotated) => annotated.split_once('=')?.1,
            None => rest.strip_prefix('=')?,
        };
        value.split('#').next()?.trim().parse().ok()
    })
}

fn embed_heldout_splits(crate_dir: &Path, out_dir: &Path) {
    let dir = crate_dir.join("../../eval/src/snow_globe_eval");
    let py = dir.join("heldout.py");
    println!("cargo:rerun-if-changed={}", py.display());
    let text =
        fs::read_to_string(&py).unwrap_or_else(|e| panic!("cannot read {}: {e}", py.display()));
    let current = current_split_version(&text).unwrap_or_else(|| {
        panic!(
            "{} has no `CURRENT_SPLIT_VERSION = <n>` line: Tier 0 applies the held-out split eval \
             calls current, so it cannot be built without it",
            py.display()
        )
    });
    let mut files: Vec<PathBuf> = fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", dir.display()))
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("heldout_periods") && n.ends_with(".json"))
        })
        .collect();
    files.sort();
    assert!(
        !files.is_empty(),
        "no heldout_periods*.json in {}",
        dir.display()
    );
    let mut out = format!(
        "/// eval's `CURRENT_SPLIT_VERSION` (`eval/src/snow_globe_eval/heldout.py`) at build time.\n\
         pub const EVAL_CURRENT_SPLIT_VERSION: u32 = {current};\n\
         /// `(file name, JSON text)` of every committed held-out split\n\
         /// (`eval/src/snow_globe_eval/heldout_periods*.json`) at build time, by file name.\n\
         pub static COMMITTED_SPLITS: &[(&str, &str)] = &[\n"
    );
    for path in &files {
        println!("cargo:rerun-if-changed={}", path.display());
        let name = path
            .file_name()
            .and_then(|s| s.to_str())
            .expect("UTF-8 file name");
        let abs = path.canonicalize().expect("split path resolves");
        out.push_str(&format!(
            "    ({:?}, include_str!({:?})),\n",
            name,
            abs.display().to_string()
        ));
    }
    out.push_str("];\n");
    fs::write(out_dir.join("heldout_splits.rs"), out).expect("write heldout_splits.rs");
}
