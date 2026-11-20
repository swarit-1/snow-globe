//! Build script for the M1 1a spike app: runs `tauri-build`, and with `--features speech` links
//! app/speech-macos's static library through its C ABI (PRD §6.5: Swift in-process static
//! library) plus the Swift runtime and the frameworks it needs.
//!
//! The library is built by another work package. Its location is not fixed yet, so the script
//! looks for `lib$LOOKOUT_SPEECH_LIB_NAME.a` (default `liblookout_speech.a`) in
//! `$LOOKOUT_SPEECH_LIB_DIR` when set, else in the usual SwiftPM output folders.

use std::env;
use std::path::{Path, PathBuf};
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-env-changed=LOOKOUT_SPEECH_LIB_DIR");
    println!("cargo:rerun-if-env-changed=LOOKOUT_SPEECH_LIB_NAME");
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
    let name = env::var("LOOKOUT_SPEECH_LIB_NAME").unwrap_or_else(|_| "lookout_speech".into());
    let file = format!("lib{name}.a");
    let data = env::var_os("LOOKOUT_DATA_DIR")
        .map(PathBuf::from)
        .filter(|p| !p.as_os_str().is_empty())
        .or_else(|| env::var_os("HOME").map(|h| PathBuf::from(h).join("lookout-data")));
    let candidates: Vec<PathBuf> = match env::var_os("LOOKOUT_SPEECH_LIB_DIR") {
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
             LOOKOUT_SPEECH_LIB_DIR (and LOOKOUT_SPEECH_LIB_NAME). Looked in: {}",
            tried.join(", ")
        );
    };
    println!("cargo:rerun-if-changed={}", dir.join(&file).display());
    println!("cargo:rustc-link-search=native={}", dir.display());
    println!("cargo:rustc-link-lib=static={name}");

    let header = repo.join("app/speech-macos/include/lookout_speech.h");
    match std::fs::read_to_string(&header) {
        Ok(h) => {
            for f in [
                "lk_speech_status",
                "lk_speech_start",
                "lk_speech_push_f32",
                "lk_speech_finish",
                "lk_speech_cancel",
                "lk_speech_free",
                "lk_speech_version",
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
