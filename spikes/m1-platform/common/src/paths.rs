//! Where spike results go (CLAUDE.md "Data"): `$LOOKOUT_DATA_DIR/spikes/m1/`, defaulting to
//! `~/lookout-data/spikes/m1/`, and never inside the repository.

use std::path::{Component, Path, PathBuf};

/// The repository root, fixed at compile time from this crate's location
/// (`<repo>/spikes/m1-platform/common`).
pub fn repo_root() -> PathBuf {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    manifest
        .ancestors()
        .nth(3)
        .map(Path::to_path_buf)
        .unwrap_or_else(|| manifest.to_path_buf())
}

/// `$LOOKOUT_DATA_DIR`, or `$HOME/lookout-data` when it is unset or empty.
pub fn data_dir() -> PathBuf {
    data_dir_from(
        std::env::var_os("LOOKOUT_DATA_DIR").map(PathBuf::from),
        std::env::var_os("HOME").map(PathBuf::from),
    )
}

/// Pure form of [`data_dir`], for tests.
pub fn data_dir_from(lookout_data_dir: Option<PathBuf>, home: Option<PathBuf>) -> PathBuf {
    match lookout_data_dir {
        Some(p) if !p.as_os_str().is_empty() => p,
        _ => home
            .unwrap_or_else(|| PathBuf::from("/tmp"))
            .join("lookout-data"),
    }
}

/// `<data dir>/spikes/m1`.
pub fn spike_dir() -> PathBuf {
    data_dir().join("spikes").join("m1")
}

/// Lexically normalises an absolute path (drops `.` and resolves `..`), without touching the disk.
pub fn normalise(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in path.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// True when `child` is `parent` or lies under it. Both are normalised first, and also compared
/// after resolving symlinks when they exist on disk.
pub fn is_inside(child: &Path, parent: &Path) -> bool {
    let lexical = normalise(child).starts_with(normalise(parent));
    let resolved = match (nearest_canonical(child), parent.canonicalize()) {
        (Some(c), Ok(p)) => c.starts_with(p),
        _ => false,
    };
    lexical || resolved
}

fn nearest_canonical(path: &Path) -> Option<PathBuf> {
    let mut probe = normalise(path);
    let mut tail = Vec::new();
    loop {
        if let Ok(c) = probe.canonicalize() {
            let mut out = c;
            for part in tail.iter().rev() {
                out.push(part);
            }
            return Some(out);
        }
        let name = probe.file_name()?.to_os_string();
        tail.push(name);
        if !probe.pop() {
            return None;
        }
    }
}

/// Refuses a results path inside the repository (CLAUDE.md: never write traces into the repo).
pub fn ensure_outside_repo(path: &Path) -> Result<(), String> {
    let root = repo_root();
    if is_inside(path, &root) {
        return Err(format!(
            "refusing to write spike results inside the repository ({}); set LOOKOUT_DATA_DIR \
             to a folder outside it, such as ~/lookout-data",
            path.display()
        ));
    }
    Ok(())
}

/// Checks that a run id is a safe file name: 1 to 100 characters from `[A-Za-z0-9._-]`, not
/// starting with a dot.
pub fn validate_run_id(run_id: &str) -> Result<(), String> {
    let ok_len = !run_id.is_empty() && run_id.len() <= 100;
    let ok_chars = run_id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'));
    if ok_len && ok_chars && !run_id.starts_with('.') {
        Ok(())
    } else {
        Err(format!(
            "bad run id {run_id:?}: use 1-100 characters from A-Z a-z 0-9 . _ - (not starting with a dot)"
        ))
    }
}

/// Replaces the home folder in `s` with `~`, so a logged path or argument does not carry the
/// account name (CLAUDE.md privacy rules). Only whole path prefixes are replaced: `$HOME` itself
/// and `$HOME/...` anywhere in the string.
pub fn redact_home(s: &str) -> String {
    redact_home_with(s, std::env::var("HOME").ok().as_deref())
}

/// Pure form of [`redact_home`], for tests.
pub fn redact_home_with(s: &str, home: Option<&str>) -> String {
    let Some(home) = home
        .map(|h| h.trim_end_matches('/'))
        .filter(|h| h.len() > 1)
    else {
        return s.to_string();
    };
    if s == home {
        return "~".into();
    }
    let mut out = s.replace(&format!("{home}/"), "~/");
    if let Some(prefix) = out.strip_suffix(home)
        && (prefix.is_empty() || prefix.ends_with('='))
    {
        out = format!("{prefix}~");
    }
    out
}

/// `<spike dir>/<run-id>.jsonl`, after validating the id and the location.
pub fn run_path(run_id: &str) -> Result<PathBuf, String> {
    validate_run_id(run_id)?;
    let path = spike_dir().join(format!("{run_id}.jsonl"));
    ensure_outside_repo(&path)?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn data_dir_prefers_the_env_var() {
        assert_eq!(
            data_dir_from(Some("/x/data".into()), Some("/home/u".into())),
            PathBuf::from("/x/data")
        );
        assert_eq!(
            data_dir_from(Some("".into()), Some("/home/u".into())),
            PathBuf::from("/home/u/lookout-data")
        );
        assert_eq!(
            data_dir_from(None, Some("/home/u".into())),
            PathBuf::from("/home/u/lookout-data")
        );
    }

    #[test]
    fn repo_root_holds_this_spike() {
        assert!(
            repo_root()
                .join("spikes/m1-platform/common/Cargo.toml")
                .exists()
        );
    }

    #[test]
    fn refuses_paths_inside_the_repo() {
        let inside = repo_root().join("spikes/m1-platform/out/../x.jsonl");
        assert!(ensure_outside_repo(&inside).is_err());
        let tmp = tempfile::tempdir().expect("tempdir");
        assert!(ensure_outside_repo(&tmp.path().join("r.jsonl")).is_ok());
    }

    #[test]
    fn run_ids_are_safe_file_names() {
        assert!(validate_run_id("20261119T120000Z-hotkey-123").is_ok());
        assert!(validate_run_id("a.b_c-d").is_ok());
        for bad in ["", ".hidden", "a/b", "a b", "..", "é", &"x".repeat(101)] {
            assert!(validate_run_id(bad).is_err(), "{bad:?} should be rejected");
        }
    }

    #[test]
    fn home_is_redacted_from_logged_strings() {
        let h = Some("/Users/someone");
        assert_eq!(
            redact_home_with("/Users/someone/lookout-data/spikes/m1/r.jsonl", h),
            "~/lookout-data/spikes/m1/r.jsonl"
        );
        assert_eq!(redact_home_with("--path=/Users/someone/x", h), "--path=~/x");
        assert_eq!(redact_home_with("/Users/someone", h), "~");
        assert_eq!(redact_home_with("--dir=/Users/someone", h), "--dir=~");
        // A longer name that merely starts with the home path is left alone.
        assert_eq!(
            redact_home_with("/Users/someoneelse/x", h),
            "/Users/someoneelse/x"
        );
        assert_eq!(redact_home_with("hotkey", h), "hotkey");
        assert_eq!(redact_home_with("/x", Some("/")), "/x");
        assert_eq!(redact_home_with("/x", None), "/x");
    }

    #[test]
    fn normalise_resolves_dots() {
        assert_eq!(
            normalise(Path::new("/a/b/../c/./d")),
            PathBuf::from("/a/c/d")
        );
    }
}
