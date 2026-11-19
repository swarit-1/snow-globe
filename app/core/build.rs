//! Embed `viewpoints/manifests/*.json` in the crate so the parser's alias table needs no files at
//! run time (PRD §7.5). The generated `$OUT_DIR/manifests.rs` holds `(file stem, JSON text)` pairs
//! via `include_str!`, so editing a manifest rebuilds the crate.

use std::path::PathBuf;
use std::{env, fs};

fn main() {
    let crate_dir =
        PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("cargo sets CARGO_MANIFEST_DIR"));
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
    let out_dir = PathBuf::from(env::var("OUT_DIR").expect("cargo sets OUT_DIR"));
    fs::write(out_dir.join("manifests.rs"), out).expect("write manifests.rs");
}
