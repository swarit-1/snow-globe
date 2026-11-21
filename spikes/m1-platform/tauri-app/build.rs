//! Build script for the M1 1a spike app: runs `tauri-build`, and with `--features speech` links
//! app/speech-macos's static library through its C ABI (PRD §6.5: Swift in-process static
//! library) plus the Swift runtime and the frameworks it needs.
//!
//! The library is built by another work package. Its location is not fixed yet, so the script
//! looks for `lib$SNOW_GLOBE_SPEECH_LIB_NAME.a` (default `libsnow_globe_speech.a`) in
//! `$SNOW_GLOBE_SPEECH_LIB_DIR` when set, else in the usual SwiftPM output folders and under the
//! data dir. Each `SNOW_GLOBE_` variable falls back to its deprecated `LOOKOUT_` name (D-080).

use std::env;
use std::path::{Path, PathBuf};
use std::process::Command;

/// `SNOW_GLOBE_<X>`, else its deprecated `LOOKOUT_<X>` form (D-080): the first that is set and
/// not blank.
fn env_var(name: &str) -> Option<String> {
    let legacy = name
        .strip_prefix("SNOW_GLOBE_")
        .map(|rest| format!("LOOKOUT_{rest}"));
    std::iter::once(name.to_string())
        .chain(legacy)
        .find_map(|n| env::var(&n).ok().filter(|v| !v.trim().is_empty()))
}

/// The data dir in the workers' order (D-080): `$SNOW_GLOBE_DATA_DIR`, else `$LOOKOUT_DATA_DIR`,
/// else `~/snow-globe-data` if it exists, else `~/lookout-data` if it exists, else
/// `~/snow-globe-data`. Like the workers, it stops the build while no variable names it and both
/// folders exist as different folders (something made `~/snow-globe-data` before the rename's
/// live switch, which leaves `~/lookout-data` as a symlink to it).
fn data_dir() -> Option<PathBuf> {
    use std::os::unix::fs::MetadataExt;
    if let Some(named) = env_var("SNOW_GLOBE_DATA_DIR") {
        return Some(PathBuf::from(named.trim()));
    }
    let home = PathBuf::from(env::var_os("HOME").filter(|h| !h.is_empty())?);
    let new = home.join("snow-globe-data");
    let old = home.join("lookout-data");
    if let (Ok(a), Ok(b)) = (std::fs::metadata(&new), std::fs::metadata(&old))
        && (a.dev(), a.ino()) != (b.dev(), b.ino())
    {
        panic!(
            "both {} and {} exist and are different folders, so the data dir is ambiguous \
             (D-080): set SNOW_GLOBE_DATA_DIR to the folder you mean, and tell the lead which \
             one is stray",
            new.display(),
            old.display()
        );
    }
    Some(if new.exists() || !old.exists() {
        new
    } else {
        old
    })
}

fn main() {
    for name in [
        "SNOW_GLOBE_SPEECH_LIB_DIR",
        "SNOW_GLOBE_SPEECH_LIB_NAME",
        "SNOW_GLOBE_DATA_DIR",
        "LOOKOUT_SPEECH_LIB_DIR",
        "LOOKOUT_SPEECH_LIB_NAME",
        "LOOKOUT_DATA_DIR",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    println!("cargo:rerun-if-changed=Info.plist");
    println!("cargo:rerun-if-changed=ui");
    if env::var_os("CARGO_FEATURE_SPEECH").is_some() {
        link_speech();
    }
    tauri_build::build();
}

fn repo_root() -> PathBuf {
    let manifest = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    manifest
        .ancestors()
        .nth(3)
        .map(Path::to_path_buf)
        .unwrap_or(manifest)
}

fn tool_output(cmd: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(cmd).args(args).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8(out.stdout).ok()?.trim().to_string();
    (!s.is_empty()).then_some(s)
}

fn link_speech() {
    let repo = repo_root();
    let name = env_var("SNOW_GLOBE_SPEECH_LIB_NAME").unwrap_or_else(|| "snow_globe_speech".into());
    let file = format!("lib{name}.a");
    let data = data_dir();
    let candidates: Vec<PathBuf> = match env_var("SNOW_GLOBE_SPEECH_LIB_DIR") {
        Some(d) => vec![PathBuf::from(d)],
        None => {
            let mut v = vec![
                repo.join("app/speech-macos/.build/release"),
                repo.join("app/speech-macos/.build/arm64-apple-macosx/release"),
                repo.join("app/speech-macos/.build/debug"),
                repo.join("app/speech-macos/build"),
            ];
            if let Some(d) = data {
                v.push(d.join("cache/speech-macos/release"));
                v.push(d.join("cache/speech-macos"));
            }
            v
        }
    };
    let Some(dir) = candidates.iter().find(|d| d.join(&file).exists()) else {
        let tried: Vec<String> = candidates.iter().map(|d| d.display().to_string()).collect();
        panic!(
            "--features speech: {file} not found. Build app/speech-macos first, or set \
             SNOW_GLOBE_SPEECH_LIB_DIR (and SNOW_GLOBE_SPEECH_LIB_NAME). Looked in: {}",
            tried.join(", ")
        );
    };
    println!("cargo:rerun-if-changed={}", dir.join(&file).display());
    println!("cargo:rustc-link-search=native={}", dir.display());
    println!("cargo:rustc-link-lib=static={name}");

    let header = repo.join("app/speech-macos/include/snow_globe_speech.h");
    match std::fs::read_to_string(&header) {
        Ok(h) => {
            for f in [
                "sg_speech_status",
                "sg_speech_start",
                "sg_speech_push_f32",
                "sg_speech_finish",
                "sg_speech_cancel",
                "sg_speech_free",
                "sg_speech_version",
            ] {
                if !h.contains(f) {
                    panic!(
                        "{} does not declare {f}; the spike's FFI would not match",
                        header.display()
                    );
                }
            }
            println!("cargo:rerun-if-changed={}", header.display());
        }
        Err(_) => println!(
            "cargo:warning=speech: {} not found; linking against the ABI in src/speech.rs as specified",
            header.display()
        ),
    }

    // Swift runtime: the toolchain's static compatibility libraries and the SDK's runtime stubs.
    if let Some(swift) = tool_output("xcrun", &["--sdk", "macosx", "--find", "swift"]) {
        let lib = Path::new(&swift)
            .parent()
            .and_then(Path::parent)
            .map(|p| p.join("lib/swift/macosx"));
        if let Some(lib) = lib.filter(|p| p.exists()) {
            println!("cargo:rustc-link-search=native={}", lib.display());
        }
    }
    if let Some(sdk) = tool_output("xcrun", &["--sdk", "macosx", "--show-sdk-path"]) {
        println!("cargo:rustc-link-search=native={sdk}/usr/lib/swift");
    }
    println!("cargo:rustc-link-arg=-Wl,-rpath,/usr/lib/swift");
    for fw in ["Foundation", "AVFoundation", "CoreMedia", "Speech"] {
        println!("cargo:rustc-link-lib=framework={fw}");
    }
}
