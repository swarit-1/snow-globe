//! The spike's run log (PLAN.md M1 1a: one JSONL per run with monotonic timestamps, values, and
//! pass or fail against the PLAN's criterion).
//!
//! Every process taking part in a run (the app, the ScreenCaptureKit helper, the injector, the
//! report) appends to the same `<run-id>.jsonl`. Each record is one `write(2)` on a file opened
//! with `O_APPEND`, so lines from different processes do not interleave. Every record carries:
//! - `t_mono_ns`: nanoseconds since that process opened the log (Rust `Instant`);
//! - `t_host_ns`: the host clock (`CLOCK_UPTIME_RAW`), shared by all processes, for joins.
//!
//! Records never hold audio, transcript text, frames, device names or the owner's identity: no
//! signing-certificate names or team IDs, and paths and arguments have the home folder replaced
//! by `~`. They do hold the frontmost app's name and bundle id in checks 2 and 3 (README.md,
//! "What the spike records").

use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::clock::{Clock, Stamp, Timebase};
use crate::paths;

/// What a record is.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    /// Written once when a process opens the log: versions, arguments, OS.
    Meta,
    /// A timestamped observation.
    Event,
    /// A check's verdict against its PLAN criterion.
    Result,
    /// An owner observation added afterwards (`m1-report note`).
    Note,
}

/// The outcome of a check.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Pass,
    Fail,
    /// The run ended before the criterion could be evaluated (too few trials, missing data).
    Incomplete,
    /// The check was not run (for example the speech library was not linked).
    Skipped,
}

impl Status {
    /// `Some(true)` for pass, `Some(false)` for fail, `None` otherwise.
    pub fn as_pass(self) -> Option<bool> {
        match self {
            Status::Pass => Some(true),
            Status::Fail => Some(false),
            Status::Incomplete | Status::Skipped => None,
        }
    }

    /// Pass when `ok`, fail otherwise.
    pub fn from_bool(ok: bool) -> Self {
        if ok { Status::Pass } else { Status::Fail }
    }
}

/// One line of the run log.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Record {
    pub run_id: String,
    /// The check slug this process is running (see `checks::CHECKS`).
    pub check: String,
    /// The writing process: `m1-spike`, `m1-sck`, `m1-inject` or `m1-report`.
    pub proc: String,
    pub pid: u32,
    pub seq: u64,
    pub t_mono_ns: u64,
    pub t_host_ns: u64,
    pub kind: Kind,
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<Status>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pass: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub criterion: Option<String>,
    #[serde(default)]
    pub v: Value,
}

/// An append-only run log shared by the threads of one process.
#[derive(Debug)]
pub struct RunLog {
    file: Mutex<File>,
    path: PathBuf,
    run_id: String,
    check: String,
    proc_name: String,
    pid: u32,
    seq: AtomicU64,
    clock: Clock,
}

impl RunLog {
    /// Opens `$SNOW_GLOBE_DATA_DIR/spikes/m1/<run-id>.jsonl` for appending (creating the folder) and
    /// writes the meta record.
    pub fn open(run_id: &str, check: &str, proc_name: &str, argv: &[String]) -> io::Result<Self> {
        let path = paths::run_path(run_id).map_err(io::Error::other)?;
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        Self::open_at(&path, run_id, check, proc_name, argv)
    }

    /// Opens a log at an explicit path (used by `open`, by `m1-report` for a run file named on
    /// its command line, and by tests). Refuses a path inside the repository before creating
    /// anything (CLAUDE.md: never write traces into the repo).
    pub fn open_at(
        path: &Path,
        run_id: &str,
        check: &str,
        proc_name: &str,
        argv: &[String],
    ) -> io::Result<Self> {
        paths::validate_run_id(run_id).map_err(io::Error::other)?;
        let absolute = if path.is_absolute() {
            path.to_path_buf()
        } else {
            std::env::current_dir()?.join(path)
        };
        paths::ensure_outside_repo(&absolute).map_err(io::Error::other)?;
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        let log = Self {
            file: Mutex::new(file),
            path: path.to_path_buf(),
            run_id: run_id.to_string(),
            check: check.to_string(),
            proc_name: proc_name.to_string(),
            pid: std::process::id(),
            seq: AtomicU64::new(0),
            clock: Clock::start(),
        };
        let argv: Vec<String> = argv.iter().map(|a| paths::redact_home(a)).collect();
        log.meta(json!({
            "utc": chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
            "argv": argv,
            "spike_version": env!("CARGO_PKG_VERSION"),
            "os_product_version": sysctl_string("kern.osproductversion"),
            "os_build": sysctl_string("kern.osversion"),
            "hw_model": sysctl_string("hw.model"),
            "arch": std::env::consts::ARCH,
            "mach_timebase": Timebase::current(),
            "clock_origin_host_ns": log.clock.origin_host_ns(),
        }))?;
        Ok(log)
    }

    /// The file this log appends to.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The run id.
    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    /// This process's clock (origin = when the log was opened).
    pub fn clock(&self) -> &Clock {
        &self.clock
    }

    /// Reads both clocks now.
    pub fn stamp(&self) -> Stamp {
        self.clock.stamp()
    }

    fn write_record(
        &self,
        stamp: Stamp,
        kind: Kind,
        name: &str,
        status: Option<Status>,
        criterion: Option<&str>,
        v: Value,
    ) -> io::Result<()> {
        let rec = Record {
            run_id: self.run_id.clone(),
            check: self.check.clone(),
            proc: self.proc_name.clone(),
            pid: self.pid,
            seq: self.seq.fetch_add(1, Ordering::Relaxed),
            t_mono_ns: stamp.mono_ns,
            t_host_ns: stamp.host_ns,
            kind,
            name: name.to_string(),
            status,
            pass: status.and_then(Status::as_pass),
            criterion: criterion.map(str::to_string),
            v,
        };
        let mut line = serde_json::to_string(&rec).map_err(io::Error::other)?;
        line.push('\n');
        let mut f = self
            .file
            .lock()
            .map_err(|_| io::Error::other("run log lock poisoned"))?;
        f.write_all(line.as_bytes())
    }

    /// Writes the meta record (versions, OS, arguments).
    pub fn meta(&self, v: Value) -> io::Result<()> {
        self.write_record(self.stamp(), Kind::Meta, "meta", None, None, v)
    }

    /// Writes an event stamped now.
    pub fn event(&self, name: &str, v: Value) -> io::Result<()> {
        self.write_record(self.stamp(), Kind::Event, name, None, None, v)
    }

    /// Writes an event with a stamp taken earlier (for example at handler entry).
    pub fn event_at(&self, stamp: Stamp, name: &str, v: Value) -> io::Result<()> {
        self.write_record(stamp, Kind::Event, name, None, None, v)
    }

    /// Writes a check verdict: `check` is the check slug the verdict is for (a run can produce
    /// verdicts for more than one check, such as speech and network silence).
    pub fn result(&self, check: &str, status: Status, criterion: &str, v: Value) -> io::Result<()> {
        self.write_record(
            self.stamp(),
            Kind::Result,
            check,
            Some(status),
            Some(criterion),
            v,
        )
    }

    /// Writes an owner observation.
    pub fn note(&self, name: &str, v: Value) -> io::Result<()> {
        self.write_record(self.stamp(), Kind::Note, name, None, None, v)
    }

    /// Writes an event and reports a failure on stderr instead of returning it. Checks use this
    /// on hot paths where a logging error must not stop the measurement.
    pub fn ev(&self, name: &str, v: Value) {
        if let Err(e) = self.event(name, v) {
            eprintln!("m1: could not write {name} to {}: {e}", self.path.display());
        }
    }

    /// [`RunLog::event_at`] with errors reported on stderr.
    pub fn ev_at(&self, stamp: Stamp, name: &str, v: Value) {
        if let Err(e) = self.event_at(stamp, name, v) {
            eprintln!("m1: could not write {name} to {}: {e}", self.path.display());
        }
    }
}

/// Reads a run log. Lines that do not parse are counted, not fatal.
pub fn read_run(path: &Path) -> io::Result<(Vec<Record>, usize)> {
    let f = File::open(path)?;
    let mut out = Vec::new();
    let mut bad = 0usize;
    for line in BufReader::new(f).lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<Record>(&line) {
            Ok(r) => out.push(r),
            Err(_) => bad += 1,
        }
    }
    Ok((out, bad))
}

/// A run id of the form `<UTC yyyymmddThhmmssZ>-<check>-<pid>`.
pub fn new_run_id(check: &str) -> String {
    let utc = chrono::Utc::now().format("%Y%m%dT%H%M%SZ");
    let slug: String = check
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect();
    format!("{utc}-{slug}-{}", std::process::id())
}

/// Reads a string sysctl such as `kern.osproductversion`. `None` if it is missing.
pub fn sysctl_string(name: &str) -> Option<String> {
    let cname = std::ffi::CString::new(name).ok()?;
    let mut len: libc::size_t = 0;
    // SAFETY: a null old-value pointer asks sysctlbyname for the length only.
    let rc = unsafe {
        libc::sysctlbyname(
            cname.as_ptr(),
            std::ptr::null_mut(),
            &mut len,
            std::ptr::null_mut(),
            0,
        )
    };
    if rc != 0 || len == 0 || len > 4096 {
        return None;
    }
    let mut buf = vec![0u8; len];
    // SAFETY: buf has `len` writable bytes, as sysctlbyname was told.
    let rc = unsafe {
        libc::sysctlbyname(
            cname.as_ptr(),
            buf.as_mut_ptr().cast(),
            &mut len,
            std::ptr::null_mut(),
            0,
        )
    };
    if rc != 0 {
        return None;
    }
    buf.truncate(len);
    while buf.last() == Some(&0) {
        buf.pop();
    }
    String::from_utf8(buf).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn open_tmp(dir: &Path, run: &str, proc_name: &str) -> RunLog {
        RunLog::open_at(
            &dir.join(format!("{run}.jsonl")),
            run,
            "hotkey",
            proc_name,
            &[],
        )
        .expect("open log")
    }

    #[test]
    fn writes_meta_events_and_results_with_both_clocks() {
        let dir = tempfile::tempdir().expect("tempdir");
        let log = open_tmp(dir.path(), "r1", "m1-spike");
        log.event("pressed", json!({"trial": 1})).expect("event");
        log.result("hotkey", Status::Pass, "criterion text", json!({"n": 1}))
            .expect("result");
        let (recs, bad) = read_run(log.path()).expect("read");
        assert_eq!(bad, 0);
        assert_eq!(recs.len(), 3);
        assert_eq!(recs[0].kind, Kind::Meta);
        assert!(recs[0].v.get("mach_timebase").is_some());
        assert_eq!(recs[1].name, "pressed");
        assert_eq!(recs[1].status, None);
        assert_eq!(recs[2].kind, Kind::Result);
        assert_eq!(recs[2].pass, Some(true));
        assert_eq!(recs[2].criterion.as_deref(), Some("criterion text"));
        // Sequence numbers and both clocks advance.
        assert!(recs.windows(2).all(|w| w[0].seq < w[1].seq));
        assert!(recs.windows(2).all(|w| w[0].t_host_ns <= w[1].t_host_ns));
        assert!(recs.windows(2).all(|w| w[0].t_mono_ns <= w[1].t_mono_ns));
    }

    #[test]
    fn incomplete_results_have_no_pass_value() {
        let dir = tempfile::tempdir().expect("tempdir");
        let log = open_tmp(dir.path(), "r2", "m1-spike");
        log.result("cpal", Status::Incomplete, "c", json!({}))
            .expect("result");
        let text = std::fs::read_to_string(log.path()).expect("read");
        let last = text.lines().last().expect("line");
        assert!(last.contains("\"status\":\"incomplete\""));
        assert!(!last.contains("\"pass\""));
    }

    #[test]
    fn two_writers_append_whole_lines() {
        let dir = tempfile::tempdir().expect("tempdir");
        let a = std::sync::Arc::new(open_tmp(dir.path(), "r3", "m1-spike"));
        let b = std::sync::Arc::new(open_tmp(dir.path(), "r3", "m1-sck"));
        let mut handles = Vec::new();
        for log in [a.clone(), b.clone()] {
            handles.push(std::thread::spawn(move || {
                for k in 0..500 {
                    log.event("frame", json!({"k": k, "pad": "x".repeat(200)}))
                        .expect("event");
                }
            }));
        }
        for h in handles {
            h.join().expect("join");
        }
        let (recs, bad) = read_run(a.path()).expect("read");
        assert_eq!(bad, 0);
        assert_eq!(recs.len(), 2 + 1000);
        assert_eq!(recs.iter().filter(|r| r.proc == "m1-sck").count(), 501);
    }

    #[test]
    fn rejects_bad_run_ids_and_counts_bad_lines() {
        let dir = tempfile::tempdir().expect("tempdir");
        assert!(RunLog::open_at(&dir.path().join("x.jsonl"), "../x", "c", "p", &[]).is_err());
        let p = dir.path().join("mixed.jsonl");
        let log = RunLog::open_at(&p, "mixed", "c", "p", &[]).expect("open");
        drop(log);
        let mut f = OpenOptions::new().append(true).open(&p).expect("append");
        f.write_all(b"not json\n\n").expect("write");
        let (recs, bad) = read_run(&p).expect("read");
        assert_eq!((recs.len(), bad), (1, 1));
    }

    /// The reviewer's case: `m1-report show <path> --write` and `m1-report note <path>` opened an
    /// explicit path without the repository guard. The guard now lives in `open_at` itself.
    #[test]
    fn refuses_to_open_a_log_inside_the_repository() {
        let inside = crate::paths::repo_root().join("spikes/m1-platform/refused-by-a-test.jsonl");
        let e = RunLog::open_at(&inside, "r", "c", "p", &[]).expect_err("inside the repo");
        assert!(e.to_string().contains("inside the repository"), "{e}");
        assert!(!inside.exists(), "the refused file was created");
        let dotted = crate::paths::repo_root().join("spikes/../refused-by-a-test.jsonl");
        assert!(RunLog::open_at(&dotted, "r", "c", "p", &[]).is_err());
        assert!(
            !crate::paths::repo_root()
                .join("refused-by-a-test.jsonl")
                .exists()
        );
    }

    #[test]
    fn argv_is_logged_without_the_home_folder() {
        let dir = tempfile::tempdir().expect("tempdir");
        let home = std::env::var("HOME").expect("HOME");
        let argv = vec![
            "show".to_string(),
            format!("{home}/snow-globe-data/spikes/m1/x.jsonl"),
        ];
        let log = RunLog::open_at(&dir.path().join("h.jsonl"), "h", "c", "m1-report", &argv)
            .expect("open");
        let (recs, _) = read_run(log.path()).expect("read");
        let logged = recs[0].v["argv"].to_string();
        assert!(!logged.contains(&home), "{logged}");
        assert!(
            logged.contains("~/snow-globe-data/spikes/m1/x.jsonl"),
            "{logged}"
        );
    }

    #[test]
    fn run_ids_are_valid_file_names() {
        let id = new_run_id("sck tag");
        assert!(crate::paths::validate_run_id(&id).is_ok(), "{id}");
        assert!(id.contains("-sck_tag-"));
    }

    #[test]
    fn reads_the_os_version() {
        let v = sysctl_string("kern.osproductversion").expect("macOS product version");
        assert!(v.chars().next().is_some_and(|c| c.is_ascii_digit()), "{v}");
        assert!(sysctl_string("no.such.sysctl").is_none());
    }
}
