//! Runs the network monitor for check 7 (PLAN.md M1 1a "network silence during
//! DictationTranscriber, checked with a network monitor").
//!
//! `nettop` runs in delta mode under `script` (a pseudo-terminal, so its samples are not held in
//! a pipe buffer); see `m1_spike_common::netwatch` for the format and the grouping. Before a
//! complete 1 s sample is kept, every pid in it that moved bytes is resolved to its executable
//! name (nettop cuts names to 15 characters). Each sample is logged as a `nettop_sample` event
//! with per-group byte totals; only the spike process and speech or background daemons are
//! named, never other apps.

use std::collections::HashMap;
use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

use m1_spike_common::clock::host_now_ns;
use m1_spike_common::jsonl::RunLog;
use m1_spike_common::netwatch::{
    NETTOP_ARGS, Parser, Sample, exe_name, resolve_exes, window_bytes,
};
use serde_json::json;

/// A running `nettop` and the samples it has produced so far.
#[derive(Debug)]
pub struct NetWatch {
    child: Mutex<Option<Child>>,
    samples: Arc<Mutex<Vec<Sample>>>,
    pub started_host_ns: u64,
}

impl NetWatch {
    /// Starts `script -q /dev/null /usr/bin/nettop ...` and a reader thread.
    pub fn start(log: Arc<RunLog>) -> Result<Self, String> {
        let mut child = Command::new("/usr/bin/script")
            .args(NETTOP_ARGS)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("could not start nettop: {e}"))?;
        let stdout = child.stdout.take().ok_or("nettop has no stdout")?;
        let samples = Arc::new(Mutex::new(Vec::new()));
        let out = samples.clone();
        let self_pid = std::process::id();
        std::thread::Builder::new()
            .name("nettop-reader".into())
            .spawn(move || {
                let mut parser = Parser::new();
                let mut reader = BufReader::new(stdout);
                let mut buf = Vec::new();
                let mut exes: HashMap<u32, Option<String>> = HashMap::new();
                loop {
                    buf.clear();
                    match reader.read_until(b'\n', &mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {}
                    }
                    let t = host_now_ns();
                    let line = String::from_utf8_lossy(&buf);
                    if let Some(mut s) = parser.feed(&line, t) {
                        resolve_exes(&mut s, &mut exes, exe_name);
                        record(&log, &out, s, self_pid);
                    }
                }
                if let Some(mut s) = parser.finish() {
                    resolve_exes(&mut s, &mut exes, exe_name);
                    record(&log, &out, s, self_pid);
                }
            })
            .map_err(|e| e.to_string())?;
        Ok(Self {
            child: Mutex::new(Some(child)),
            samples,
            started_host_ns: host_now_ns(),
        })
    }

    /// The complete samples so far.
    pub fn samples(&self) -> Vec<Sample> {
        self.samples.lock().map(|v| v.clone()).unwrap_or_default()
    }

    /// True once a complete delta sample reaches `end_ns`, so a window ending there can be
    /// judged (check 7).
    pub fn covers_until(&self, end_ns: u64) -> bool {
        self.samples
            .lock()
            .map(|v| {
                v.iter()
                    .any(|s| !s.is_baseline() && s.end_host_ns >= end_ns)
            })
            .unwrap_or(false)
    }

    /// Stops `script` (and with it `nettop`, which loses its terminal).
    pub fn stop(&self) {
        if let Ok(mut g) = self.child.lock()
            && let Some(mut c) = g.take()
        {
            if let Ok(pid) = i32::try_from(c.id()) {
                // SAFETY: sending SIGTERM to our own child process.
                unsafe {
                    libc::kill(pid, libc::SIGTERM);
                }
            }
            let _ = c.wait();
        }
    }
}

impl Drop for NetWatch {
    fn drop(&mut self) {
        self.stop();
    }
}

fn record(log: &RunLog, out: &Mutex<Vec<Sample>>, s: Sample, self_pid: u32) {
    let w = window_bytes(
        std::slice::from_ref(&s),
        s.start_host_ns,
        s.end_host_ns,
        self_pid,
    );
    log.ev(
        "nettop_sample",
        json!({
            "index": s.index,
            "baseline": s.is_baseline(),
            "start_host_ns": s.start_host_ns,
            "end_host_ns": s.end_host_ns,
            "rows": s.rows.len(),
            "bad_rows": s.bad_rows,
            "self_bytes": w.self_bytes,
            "speech_bytes": w.speech_bytes,
            "background_bytes": w.background_bytes,
            "other_bytes": w.other_bytes,
            "other_processes": w.other_processes,
            "flagged": if s.is_baseline() { json!([]) } else { json!(w.flagged) },
        }),
    );
    if let Ok(mut v) = out.lock() {
        v.push(s);
    }
}
