//! The data directory, with the same rules as `snow_globe_common.paths` (CLAUDE.md "Data").
//!
//! In order (D-080): `$SNOW_GLOBE_DATA_DIR`; else the deprecated `$LOOKOUT_DATA_DIR`; else
//! `~/snow-globe-data` if it exists; else `~/lookout-data` if it exists (the folder's name before
//! the rename); else `~/snow-globe-data`. A blank variable counts as unset. Refused while no
//! variable names it and both folders exist as different folders (something made
//! `~/snow-globe-data` before the rename's live switch, which leaves `~/lookout-data` as a symlink
//! to it). Refused when it is
//! inside any git work tree (always), or inside `~/Documents`, `~/Desktop`,
//! `~/Library/Mobile Documents` or `~/Library/CloudStorage` (cloud-synced on this machine) unless
//! `SNOW_GLOBE_ALLOW_SYNCED_DATA=1` (or the deprecated `LOOKOUT_ALLOW_SYNCED_DATA=1`). Traces are
//! written under it, never inside the repo.

use std::path::{Component, Path, PathBuf};

/// Environment variable naming the data directory.
pub const ENV_DATA_DIR: &str = "SNOW_GLOBE_DATA_DIR";
/// Environment variable that allows a synced data directory.
pub const ENV_ALLOW_SYNCED: &str = "SNOW_GLOBE_ALLOW_SYNCED_DATA";
/// The prefix of every variable read here, and the prefix before the rename, still read as a
/// fallback when the new name is unset (D-080).
pub const ENV_PREFIX: &str = "SNOW_GLOBE_";
/// See [`ENV_PREFIX`].
pub const LEGACY_ENV_PREFIX: &str = "LOOKOUT_";
/// The default data folder under the home directory.
pub const DEFAULT_DIR_NAME: &str = "snow-globe-data";
/// The default folder's name before the rename (D-080), used while it exists and the new one
/// does not.
pub const LEGACY_DIR_NAME: &str = "lookout-data";
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
    /// No variable names the data dir and both default folders exist as different folders.
    #[error(
        "both {new} and {old} exist and are different folders, so the data dir is ambiguous (D-080): until the rename's live switch the data is in {old}, and after it {old} is a symlink to {new}. Set {ENV_DATA_DIR} to the folder you mean, and tell the lead which one is stray"
    )]
    Ambiguous {
        /// `<home>/snow-globe-data`.
        new: PathBuf,
        /// `<home>/lookout-data`.
        old: PathBuf,
    },
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .filter(|h| !h.is_empty())
        .map(PathBuf::from)
}

fn truthy(v: Option<String>) -> bool {
    v.is_some_and(|s| {
        matches!(
            s.trim().to_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        )
    })
}

/// `name` (a `SNOW_GLOBE_<X>` variable), else its deprecated `LOOKOUT_<X>` form, looked up with
/// `lookup`: the first that is set and not blank (D-080, as `snow_globe_common.env.env_get`).
pub fn env_var_with(name: &str, lookup: impl Fn(&str) -> Option<String>) -> Option<String> {
    let legacy = name
        .strip_prefix(ENV_PREFIX)
        .map(|rest| format!("{LEGACY_ENV_PREFIX}{rest}"));
    std::iter::once(name.to_string())
        .chain(legacy)
        .find_map(|n| lookup(&n).filter(|v| !v.trim().is_empty()))
}

/// [`env_var_with`] on the process environment.
pub fn env_var(name: &str) -> Option<String> {
    env_var_with(name, |n| std::env::var(n).ok())
}

/// Whether two existing paths are one folder: same device and inode, symlinks followed.
fn same_folder(a: &Path, b: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        match (std::fs::metadata(a), std::fs::metadata(b)) {
            (Ok(x), Ok(y)) => x.dev() == y.dev() && x.ino() == y.ino(),
            _ => false,
        }
    }
    #[cfg(not(unix))]
    {
        matches!((a.canonicalize(), b.canonicalize()), (Ok(x), Ok(y)) if x == y)
    }
}

/// The data directory when no variable names one: `<home>/snow-globe-data` if it exists, else
/// `<home>/lookout-data` if it exists, else `<home>/snow-globe-data` (D-080). Symlinks are
/// followed, so after the move, which leaves `~/lookout-data` as a link to the new folder, the
/// new name wins. Refused ([`DataDirError::Ambiguous`]) while both exist as different folders.
/// Nothing is created.
pub fn default_data_dir(home: &Path) -> Result<PathBuf, DataDirError> {
    let new = home.join(DEFAULT_DIR_NAME);
    let old = home.join(LEGACY_DIR_NAME);
    if new.exists() && old.exists() && !same_folder(&new, &old) {
        return Err(DataDirError::Ambiguous { new, old });
    }
    Ok(if new.exists() || !old.exists() {
        new
    } else {
        old
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

/// Check a candidate data directory; return it resolved. Reads `SNOW_GLOBE_ALLOW_SYNCED_DATA`
/// (or the deprecated `LOOKOUT_ALLOW_SYNCED_DATA`).
pub fn check_data_dir(path: &Path, home: &Path) -> Result<PathBuf, DataDirError> {
    check_data_dir_with(path, home, truthy(env_var(ENV_ALLOW_SYNCED)))
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

/// The unchecked data directory: the named one (see the module docs), else
/// [`default_data_dir`]. `~` and `~/...` expand to `home`.
pub fn data_dir_path(
    home: &Path,
    lookup: impl Fn(&str) -> Option<String>,
) -> Result<PathBuf, DataDirError> {
    match env_var_with(ENV_DATA_DIR, lookup).map(|s| s.trim().to_string()) {
        Some(raw) => Ok(match raw.strip_prefix("~/") {
            Some(rest) => home.join(rest),
            None if raw == "~" => home.to_path_buf(),
            None => PathBuf::from(raw),
        }),
        None => default_data_dir(home),
    }
}

/// The checked data directory (see the module docs; not created here).
pub fn data_dir() -> Result<PathBuf, DataDirError> {
    let home = home_dir().ok_or(DataDirError::NoHome)?;
    let path = data_dir_path(&home, |n| std::env::var(n).ok())?;
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
        let err = check_data_dir_with(
            &home.path().join("Documents/snow-globe"),
            home.path(),
            false,
        )
        .expect_err("synced");
        assert!(matches!(err, DataDirError::Synced { .. }), "{err}");
        assert!(
            check_data_dir_with(&home.path().join("Documents/snow-globe"), home.path(), true)
                .is_ok(),
            "override"
        );
        let ok = check_data_dir_with(&home.path().join("snow-globe-data"), home.path(), false)
            .expect("plain folder is fine");
        assert!(ok.ends_with("snow-globe-data"));
    }

    fn env<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |name| {
            pairs
                .iter()
                .find(|(k, _)| *k == name)
                .map(|(_, v)| (*v).to_string())
        }
    }

    #[test]
    fn env_var_prefers_the_new_name_then_the_old_one() {
        let new = "SNOW_GLOBE_X";
        assert_eq!(env_var_with(new, env(&[])), None);
        assert_eq!(
            env_var_with(new, env(&[("LOOKOUT_X", "old")])).as_deref(),
            Some("old")
        );
        let both = [("SNOW_GLOBE_X", "new"), ("LOOKOUT_X", "old")];
        assert_eq!(env_var_with(new, env(&both)).as_deref(), Some("new"));
        let blank = [("SNOW_GLOBE_X", "  "), ("LOOKOUT_X", "old")];
        assert_eq!(env_var_with(new, env(&blank)).as_deref(), Some("old"));
        // a name without the prefix has no legacy form
        assert_eq!(env_var_with("HOME", env(&[("LOOKOUT_HOME", "x")])), None);
    }

    #[test]
    fn data_dir_follows_the_rename_order() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let home = tmp.path();
        let new = home.join("snow-globe-data");
        let old = home.join("lookout-data");
        assert_eq!(
            data_dir_path(home, env(&[])),
            Ok(new.clone()),
            "neither folder exists"
        );
        std::fs::create_dir(&old).expect("mkdir");
        assert_eq!(
            data_dir_path(home, env(&[])),
            Ok(old.clone()),
            "the old folder, until it is moved"
        );
        std::fs::create_dir(&new).expect("mkdir");
        assert_eq!(
            data_dir_path(home, env(&[])),
            Err(DataDirError::Ambiguous {
                new: new.clone(),
                old: old.clone()
            }),
            "both folders apart: refused, nothing picked"
        );
        let named = [("LOOKOUT_DATA_DIR", "/legacy")];
        assert_eq!(
            data_dir_path(home, env(&named)),
            Ok(PathBuf::from("/legacy"))
        );
        let both = [
            ("LOOKOUT_DATA_DIR", "/legacy"),
            ("SNOW_GLOBE_DATA_DIR", "~/elsewhere"),
        ];
        assert_eq!(data_dir_path(home, env(&both)), Ok(home.join("elsewhere")));
        let blank = [
            ("LOOKOUT_DATA_DIR", "/legacy"),
            ("SNOW_GLOBE_DATA_DIR", " "),
        ];
        assert_eq!(
            data_dir_path(home, env(&blank)),
            Ok(PathBuf::from("/legacy"))
        );
        std::fs::remove_dir(&old).expect("rmdir");
        assert_eq!(
            data_dir_path(home, env(&[])),
            Ok(new),
            "the new folder alone"
        );
        let dangling = home.join("nowhere");
        std::fs::remove_dir(home.join("snow-globe-data")).expect("rmdir");
        std::fs::create_dir(&old).expect("mkdir");
        std::os::unix::fs::symlink(&dangling, home.join("snow-globe-data")).expect("symlink");
        assert_eq!(
            data_dir_path(home, env(&[])),
            Ok(old),
            "a dangling new link does not exist"
        );
    }

    #[test]
    fn data_dir_follows_the_symlink_left_by_the_move() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let home = tmp.path();
        std::fs::create_dir(home.join("snow-globe-data")).expect("mkdir");
        std::os::unix::fs::symlink(home.join("snow-globe-data"), home.join("lookout-data"))
            .expect("symlink");
        assert_eq!(
            data_dir_path(home, env(&[])),
            Ok(home.join("snow-globe-data"))
        );
        let via_old = check_data_dir_with(&home.join("lookout-data"), home, false).expect("ok");
        let direct = check_data_dir_with(&home.join("snow-globe-data"), home, false).expect("ok");
        assert_eq!(via_old, direct, "the old name resolves to the new folder");
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
