//! The data directory, with the same safety rules as `lookout_common.paths` (CLAUDE.md "Data").
//!
//! `$LOOKOUT_DATA_DIR`, default `~/lookout-data`. Refused when it is inside any git work tree
//! (always), or inside `~/Documents`, `~/Desktop`, `~/Library/Mobile Documents` or
//! `~/Library/CloudStorage` (cloud-synced on this machine) unless `LOOKOUT_ALLOW_SYNCED_DATA=1`.
//! Traces are written under it, never inside the repo.

use std::path::{Component, Path, PathBuf};

/// Environment variable naming the data directory.
pub const ENV_DATA_DIR: &str = "LOOKOUT_DATA_DIR";
/// Environment variable that allows a synced data directory.
pub const ENV_ALLOW_SYNCED: &str = "LOOKOUT_ALLOW_SYNCED_DATA";
/// Folders a sync client mirrors to the cloud on this kind of machine (relative to home).
pub const SYNCED_ROOTS: &[&str] = &[
    "Documents",
    "Desktop",
    "Library/Mobile Documents",
    "Library/CloudStorage",
];

/// Why a data directory was refused.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum DataDirError {
    /// Inside a git work tree.
    #[error(
        "data dir {dir} is inside the git work tree {git_root}; set {ENV_DATA_DIR} to a folder outside any repo"
    )]
    InsideGit {
        /// The directory.
        dir: PathBuf,
        /// The enclosing work tree.
        git_root: PathBuf,
    },
    /// Inside a cloud-synced folder.
    #[error(
        "data dir {dir} is inside {synced}, which is cloud-synced; set {ENV_ALLOW_SYNCED}=1 to override"
    )]
    Synced {
        /// The directory.
        dir: PathBuf,
        /// The synced root.
        synced: PathBuf,
    },
    /// No home directory to expand `~`.
    #[error("cannot find the home directory")]
    NoHome,
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .filter(|h| !h.is_empty())
        .map(PathBuf::from)
}

fn truthy(v: Option<std::ffi::OsString>) -> bool {
    v.and_then(|s| s.into_string().ok()).is_some_and(|s| {
        matches!(
            s.trim().to_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        )
    })
}

/// Resolve symlinks for the part of `path` that exists and normalise the rest lexically.
fn resolve(path: &Path) -> PathBuf {
    let mut existing = path.to_path_buf();
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    while !existing.exists() {
        match (
            existing.file_name().map(|n| n.to_os_string()),
            existing.parent(),
        ) {
            (Some(name), Some(parent)) => {
                tail.push(name);
                existing = parent.to_path_buf();
            }
            _ => break,
        }
    }
    let mut out = existing.canonicalize().unwrap_or(existing);
    for name in tail.into_iter().rev() {
        out.push(name);
    }
    let mut clean = PathBuf::new();
    for c in out.components() {
        match c {
            Component::ParentDir => {
                clean.pop();
            }
            Component::CurDir => {}
            other => clean.push(other.as_os_str()),
        }
    }
    clean
}

/// The nearest ancestor of `path` (inclusive) holding a `.git` entry.
pub fn enclosing_git_root(path: &Path) -> Option<PathBuf> {
    path.ancestors()
        .find(|p| p.join(".git").exists())
        .map(Path::to_path_buf)
}

/// Check a candidate data directory; return it resolved. Reads `LOOKOUT_ALLOW_SYNCED_DATA`.
pub fn check_data_dir(path: &Path, home: &Path) -> Result<PathBuf, DataDirError> {
    check_data_dir_with(path, home, truthy(std::env::var_os(ENV_ALLOW_SYNCED)))
}

/// [`check_data_dir`] with the synced-folder override given explicitly.
pub fn check_data_dir_with(
    path: &Path,
    home: &Path,
    allow_synced: bool,
) -> Result<PathBuf, DataDirError> {
    let dir = resolve(path);
    if let Some(git_root) = enclosing_git_root(&dir) {
        return Err(DataDirError::InsideGit { dir, git_root });
    }
    if !allow_synced {
        let home = resolve(home);
        for root in SYNCED_ROOTS {
            let synced = resolve(&home.join(root));
            if dir.starts_with(&synced) {
                return Err(DataDirError::Synced { dir, synced });
            }
        }
    }
    Ok(dir)
}

/// The checked data directory from `$LOOKOUT_DATA_DIR` or `~/lookout-data` (not created here).
pub fn data_dir() -> Result<PathBuf, DataDirError> {
    let home = home_dir().ok_or(DataDirError::NoHome)?;
    let raw = std::env::var(ENV_DATA_DIR)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let raw = raw.unwrap_or_else(|| "~/lookout-data".to_string());
    let path = match raw.strip_prefix("~/") {
        Some(rest) => home.join(rest),
        None if raw == "~" => home.clone(),
        None => PathBuf::from(raw),
    };
    check_data_dir(&path, &home)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refuses_a_git_work_tree() {
        let tmp = tempfile::tempdir().expect("tempdir");
        std::fs::create_dir(tmp.path().join(".git")).expect("mkdir");
        let err = check_data_dir_with(
            &tmp.path().join("data/traces"),
            Path::new("/nonexistent-home"),
            true,
        )
        .expect_err("inside git");
        assert!(matches!(err, DataDirError::InsideGit { .. }), "{err}");
    }

    #[test]
    fn refuses_synced_folders() {
        let home = tempfile::tempdir().expect("tempdir");
        std::fs::create_dir_all(home.path().join("Documents")).expect("mkdir");
        let err = check_data_dir_with(&home.path().join("Documents/lookout"), home.path(), false)
            .expect_err("synced");
        assert!(matches!(err, DataDirError::Synced { .. }), "{err}");
        assert!(
            check_data_dir_with(&home.path().join("Documents/lookout"), home.path(), true).is_ok(),
            "override"
        );
        let ok = check_data_dir_with(&home.path().join("lookout-data"), home.path(), false)
            .expect("plain folder is fine");
        assert!(ok.ends_with("lookout-data"));
    }

    #[test]
    fn this_repo_is_refused() {
        // The crate lives in the repo, so its own directory must be refused.
        let here = Path::new(env!("CARGO_MANIFEST_DIR"));
        assert!(matches!(
            check_data_dir_with(here, Path::new("/nonexistent-home"), true),
            Err(DataDirError::InsideGit { .. })
        ));
    }
}
