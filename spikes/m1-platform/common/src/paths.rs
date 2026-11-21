//! Where spike results go (CLAUDE.md "Data"): `<data dir>/spikes/m1/`, never inside the
//! repository. The data dir is the workers' (D-080): `$SNOW_GLOBE_DATA_DIR`, else the deprecated
//! `$LOOKOUT_DATA_DIR`, else `~/snow-globe-data` if it exists, else `~/lookout-data` if it exists
//! (its name before the rename), else `~/snow-globe-data`; refused while no variable names it and
//! both folders exist as different folders (something made `~/snow-globe-data` before the rename's
//! live switch, which leaves `~/lookout-data` as a symlink to it).

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

/// The data dir (see the module docs). A blank variable counts as unset.
pub fn data_dir() -> Result<PathBuf, String> {
    data_dir_from(
        std::env::var_os("SNOW_GLOBE_DATA_DIR").map(PathBuf::from),
        std::env::var_os("LOOKOUT_DATA_DIR").map(PathBuf::from),
        std::env::var_os("HOME").map(PathBuf::from),
        |p| p.exists(),
        same_folder,
    )
}

/// Whether two existing paths are one folder: same device and inode, symlinks followed.
pub fn same_folder(a: &Path, b: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    match (std::fs::metadata(a), std::fs::metadata(b)) {
        (Ok(x), Ok(y)) => x.dev() == y.dev() && x.ino() == y.ino(),
        _ => false,
    }
}

fn blank(p: &Path) -> bool {
    p.as_os_str()
        .to_str()
        .map_or(p.as_os_str().is_empty(), |s| s.trim().is_empty())
}

/// Pure form of [`data_dir`], for tests: the two variables' values, the home folder, how to
/// tell whether a folder exists, and whether two existing folders are one.
pub fn data_dir_from(
    data_dir: Option<PathBuf>,
    legacy_data_dir: Option<PathBuf>,
    home: Option<PathBuf>,
    exists: impl Fn(&Path) -> bool,
    same: impl Fn(&Path, &Path) -> bool,
) -> Result<PathBuf, String> {
    if let Some(named) = [data_dir, legacy_data_dir]
        .into_iter()
        .flatten()
        .find(|p| !blank(p))
    {
        return Ok(named);
    }
    let home = home.unwrap_or_else(|| PathBuf::from("/tmp"));
    let new = home.join("snow-globe-data");
    let old = home.join("lookout-data");
    if exists(&new) && exists(&old) && !same(&new, &old) {
        return Err(format!(
            "both {} and {} exist and are different folders, so the data dir is ambiguous \
             (D-080): set SNOW_GLOBE_DATA_DIR to the folder you mean, and tell the lead which \
             one is stray",
            new.display(),
            old.display()
        ));
    }
    Ok(if exists(&new) || !exists(&old) {
        new
    } else {
        old
    })
}

/// `<data dir>/spikes/m1`.
pub fn spike_dir() -> Result<PathBuf, String> {
    Ok(data_dir()?.join("spikes").join("m1"))
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
            "refusing to write spike results inside the repository ({}); set SNOW_GLOBE_DATA_DIR \
             to a folder outside it, such as ~/snow-globe-data",
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
    let path = spike_dir()?.join(format!("{run_id}.jsonl"));
    ensure_outside_repo(&path)?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn apart(_: &Path, _: &Path) -> bool {
        false
    }

    #[test]
    fn data_dir_prefers_the_env_var() {
        let none = |_: &Path| false;
        let home = || Some(PathBuf::from("/home/u"));
        assert_eq!(
            data_dir_from(Some("/x/data".into()), None, home(), none, apart),
            Ok(PathBuf::from("/x/data"))
        );
        assert_eq!(
            data_dir_from(Some("".into()), None, home(), none, apart),
            Ok(PathBuf::from("/home/u/snow-globe-data"))
        );
        assert_eq!(
            data_dir_from(None, None, home(), none, apart),
            Ok(PathBuf::from("/home/u/snow-globe-data"))
        );
    }

    #[test]
    fn data_dir_follows_the_rename_order() {
        let home = || Some(PathBuf::from("/home/u"));
        let only_old = |p: &Path| p == Path::new("/home/u/lookout-data");
        let both = |p: &Path| p.starts_with("/home/u");
        let none = |_: &Path| false;
        // the deprecated variable, then the new one over it, and a blank new one is unset
        assert_eq!(
            data_dir_from(None, Some("/old".into()), home(), none, apart),
            Ok(PathBuf::from("/old"))
        );
        assert_eq!(
            data_dir_from(
                Some("/new".into()),
                Some("/old".into()),
                home(),
                none,
                apart
            ),
            Ok(PathBuf::from("/new"))
        );
        assert_eq!(
            data_dir_from(Some(" ".into()), Some("/old".into()), home(), none, apart),
            Ok(PathBuf::from("/old"))
        );
        // the old folder until it is moved, then the new one: after the switch the old name is a
        // symlink to the new folder, so both exist and are one folder
        assert_eq!(
            data_dir_from(None, None, home(), only_old, apart),
            Ok(PathBuf::from("/home/u/lookout-data"))
        );
        assert_eq!(
            data_dir_from(None, None, home(), both, |_, _| true),
            Ok(PathBuf::from("/home/u/snow-globe-data"))
        );
        // both folders apart (the new one made before the switch): refused
        let err = data_dir_from(None, None, home(), both, apart).expect_err("ambiguous");
        assert!(err.contains("ambiguous (D-080)"), "{err}");
        assert_eq!(
            data_dir_from(None, Some("/old".into()), home(), both, apart),
            Ok(PathBuf::from("/old"))
        );
    }

    #[test]
    fn same_folder_follows_symlinks() {
        let tmp = std::env::temp_dir().join(format!("m1-same-folder-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(tmp.join("new")).expect("mkdir");
        std::fs::create_dir_all(tmp.join("other")).expect("mkdir");
        std::os::unix::fs::symlink(tmp.join("new"), tmp.join("old")).expect("symlink");
        assert!(same_folder(&tmp.join("new"), &tmp.join("old")));
        assert!(!same_folder(&tmp.join("new"), &tmp.join("other")));
        assert!(!same_folder(&tmp.join("new"), &tmp.join("missing")));
        std::fs::remove_dir_all(&tmp).expect("cleanup");
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
            redact_home_with("/Users/someone/snow-globe-data/spikes/m1/r.jsonl", h),
            "~/snow-globe-data/spikes/m1/r.jsonl"
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
