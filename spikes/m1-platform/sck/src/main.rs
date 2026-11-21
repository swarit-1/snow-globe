//! `m1-sck`: the ScreenCaptureKit side of the M1 1a spike (PLAN.md M1 1a checks 8, 10, 11;
//! PRD §10.4 "ScreenCaptureKit captures only the overlay window", "a pixel tag carrying
//! (query_gen, tier) is drawn inside the same surface", "ScreenCaptureKit timestamps and Rust
//! Instant should both be host time; the spike verifies this").
//!
//! - `m1-sck tag`: captures only the spike panel window (desktop-independent window filter),
//!   decodes the pixel tag from every frame, and logs each frame's presentation time, display
//!   time and arrival time. Writes the verdicts for checks 8 (`sck-tag`) and 10 (`clock`).
//! - `m1-sck consent`: one timed capture start plus the owner's observation, for check 11.
//!
//! Frames are read in memory and dropped; only timestamps and decoded tags are kept (PRD §15.2).
//! Needs Screen Recording for the terminal (or for the bundled helper, see README.md).

mod verdict;

use std::io::IsTerminal;
use std::process::{Command, ExitCode};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use m1_spike_common::args::Args;
use m1_spike_common::checks as plan;
use m1_spike_common::clock::{
    PairedRead, Timebase, agreement_from, cmtime_to_ns, duration_ns, host_now_ns, instant_identity,
};
use m1_spike_common::consent;
use m1_spike_common::jsonl::{RunLog, Status, new_run_id};
use m1_spike_common::paths::redact_home;
use m1_spike_common::tag::{FrameView, decode_bgra};
use screencapturekit::prelude::*;
use serde_json::{Value, json};

use crate::verdict::{FrameRec, Target};

const PANEL_TITLE: &str = "Snow Globe M1 spike panel";

const USAGE: &str = "usage:
  m1-sck tag     [--run-id ID] [--seconds 20] [--fps 60] [--title T] [--pid P]
  m1-sck consent [--run-id ID] [--scenario first-run|repeat|after-rebuild|after-reboot|next-day|new-identity]
                 [--prompt-seen yes|no] [--title T] [--pid P]
Start `m1-spike tags` first; both commands capture only its panel window.";

fn rect4(r: &CGRect) -> [f64; 4] {
    [r.origin.x, r.origin.y, r.size.width, r.size.height]
}

fn preflight() -> bool {
    objc2_core_graphics::CGPreflightScreenCaptureAccess()
}

/// Finds the spike panel among the shareable windows.
fn find_window(content: &SCShareableContent, title: &str, pid: Option<i32>) -> Option<SCWindow> {
    content.windows().into_iter().find(|w| {
        w.title().as_deref() == Some(title)
            && pid.is_none_or(|p| w.owning_application().is_some_and(|a| a.process_id() == p))
    })
}

/// Takes paired reads of `Instant` and the host clock for check 10.
fn paired_reads(origin: Instant, n: usize, out: &mut Vec<PairedRead>) {
    for _ in 0..n {
        let host_before_ns = host_now_ns();
        let now = Instant::now();
        let host_after_ns = host_now_ns();
        out.push(PairedRead {
            instant_ns: duration_ns(now.saturating_duration_since(origin)),
            host_before_ns,
            host_after_ns,
        });
        std::thread::sleep(Duration::from_micros(500));
    }
}

/// Builds a window-only stream for `w` at its native pixel size.
fn window_stream(w: &SCWindow, fps: u32, log: &RunLog) -> Result<(SCStream, Target), String> {
    let filter = SCContentFilter::create()
        .with_window(w)
        .build()
        .map_err(|e| format!("content filter: {e:?}"))?;
    let rect = filter.content_rect();
    let scale = f64::from(filter.point_pixel_scale());
    let scale = if scale.is_finite() && scale > 0.0 {
        scale
    } else {
        2.0
    };
    let width_px = (rect.size.width * scale).round().max(1.0) as u32;
    let height_px = (rect.size.height * scale).round().max(1.0) as u32;
    let target = Target {
        window_id: w.window_id(),
        title: w.title().unwrap_or_default(),
        owner_pid: w.owning_application().map(|a| a.process_id()),
        filter_style: format!("{:?}", filter.style()),
        width_px,
        height_px,
        point_pixel_scale: scale,
        window_frame_pt: rect4(&w.frame()),
    };
    let config = SCStreamConfiguration::new()
        .with_width(width_px)
        .with_height(height_px)
        .with_pixel_format(PixelFormat::BGRA)
        .with_shows_cursor(false)
        .with_queue_depth(6)
        .with_minimum_frame_interval(&CMTime::new(1, fps.clamp(1, 240) as i32))
        .with_ignores_shadows_single_window(true)
        .map_err(|e| format!("stream configuration: {e:?}"))?;
    let f = w.frame();
    log.ev(
        "capture_target",
        json!({"target": target, "content_rect_pt": [rect.origin.x, rect.origin.y, rect.size.width, rect.size.height],
               "window_frame_pt": [f.origin.x, f.origin.y, f.size.width, f.size.height],
               "window_on_screen": w.is_on_screen(), "fps": fps}),
    );
    let stream = SCStream::new(&filter, &config).map_err(|e| format!("SCStream: {e:?}"))?;
    Ok((stream, target))
}

/// Reads one frame: timestamps, completeness, size and the tag.
fn read_frame(
    sample: &CMSampleBuffer,
    target: &Target,
    origin: Instant,
    origin_host_ns: u64,
    tb: Timebase,
) -> FrameRec {
    let arrival_ns = host_now_ns();
    let instant_host_ns = origin_host_ns.saturating_add(duration_ns(
        Instant::now().saturating_duration_since(origin),
    ));
    let pts = sample.presentation_timestamp();
    let pts_ns = if pts.flags & 1 == 1 {
        cmtime_to_ns(pts.value, pts.timescale)
    } else {
        None
    };
    let info = sample.frame_info();
    let complete = info
        .as_ref()
        .and_then(|i| i.frame_status)
        .is_some_and(|s| s.is_complete());
    let display_ns = info
        .as_ref()
        .and_then(|i| i.display_time)
        .map(|t| tb.ticks_to_ns(t));
    let mut rec = FrameRec {
        complete,
        pts_ns,
        display_ns,
        arrival_ns,
        instant_host_ns,
        tag: None,
        tag_err: None,
        buffer_matches_config: false,
        content_rect: info
            .as_ref()
            .and_then(|i| i.content_rect.as_ref())
            .map(rect4),
        content_scale: info.as_ref().and_then(|i| i.content_scale),
        scale_factor: info.as_ref().and_then(|i| i.scale_factor),
        screen_rect: info
            .as_ref()
            .and_then(|i| i.screen_rect.as_ref())
            .map(rect4),
    };
    if !complete {
        return rec;
    }
    let Some(pb) = sample.pixel_buffer() else {
        return rec;
    };
    let Ok(guard) = pb.lock_read_only() else {
        return rec;
    };
    let (w, h, bpr) = (guard.width(), guard.height(), guard.bytes_per_row());
    rec.buffer_matches_config = w == target.width_px as usize && h == target.height_px as usize;
    // SAFETY: the read-only lock keeps the mapping valid and unchanged while the slice is used;
    // nothing else in this process writes to the buffer.
    if let Some(data) = unsafe { guard.as_slice() } {
        let view = FrameView {
            data,
            width: w,
            height: h,
            bytes_per_row: bpr,
        };
        match decode_bgra(&view, target.point_pixel_scale) {
            Ok(t) => rec.tag = Some(t),
            Err(e) => rec.tag_err = Some(e),
        }
    }
    rec
}

fn frame_event(seq: u64, r: &FrameRec) -> Value {
    json!({
        "seq": seq,
        "complete": r.complete,
        "pts_host_ns": r.pts_ns,
        "display_host_ns": r.display_ns,
        "arrival_host_ns": r.arrival_ns,
        "instant_host_ns": r.instant_host_ns,
        "pts_to_arrival_ms": r.pts_ns.map(|p| (r.instant_host_ns as f64 - p as f64) / 1e6),
        "tag": r.tag,
        "tag_error": r.tag_err,
        "buffer_matches_config": r.buffer_matches_config,
        "content_rect": r.content_rect,
        "content_scale": r.content_scale,
        "scale_factor": r.scale_factor,
        "screen_rect": r.screen_rect,
    })
}

/// Captures for `seconds` and returns every frame read.
fn capture(
    log: &Arc<RunLog>,
    stream: &mut SCStream,
    target: &Target,
    seconds: f64,
    stop_after_first_complete: bool,
) -> Result<(Vec<FrameRec>, Option<u64>, u64), String> {
    let frames: Arc<Mutex<Vec<FrameRec>>> = Arc::new(Mutex::new(Vec::new()));
    let first_complete_ns = Arc::new(Mutex::new(None::<u64>));
    let (f2, fc2, l2, t2) = (
        frames.clone(),
        first_complete_ns.clone(),
        log.clone(),
        target.clone(),
    );
    let origin = log.clock().origin();
    let origin_host_ns = log.clock().origin_host_ns();
    let tb = Timebase::current();
    let handler = move |sample: CMSampleBuffer, of: SCStreamOutputType| {
        if of != SCStreamOutputType::Screen {
            return;
        }
        let rec = read_frame(&sample, &t2, origin, origin_host_ns, tb);
        let seq = f2.lock().map(|v| v.len() as u64).unwrap_or(0);
        l2.ev("frame", frame_event(seq, &rec));
        if rec.complete
            && let Ok(mut g) = fc2.lock()
        {
            g.get_or_insert(rec.arrival_ns);
        }
        if let Ok(mut v) = f2.lock() {
            v.push(rec);
        }
    };
    stream
        .add_output_handler(handler, SCStreamOutputType::Screen)
        .map_err(|e| format!("output handler: {e:?}"))?;
    let start_ns = host_now_ns();
    stream
        .start_capture()
        .map_err(|e| format!("start_capture: {e:?}"))?;
    log.ev(
        "capture_started",
        json!({"start_called_ns": start_ns, "returned_ns": host_now_ns()}),
    );
    let deadline = Instant::now() + Duration::from_secs_f64(seconds);
    while Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
        if stop_after_first_complete
            && first_complete_ns
                .lock()
                .map(|g| g.is_some())
                .unwrap_or(false)
        {
            break;
        }
    }
    stream
        .stop_capture()
        .map_err(|e| format!("stop_capture: {e:?}"))?;
    log.ev("capture_stopped", json!({"host_ns": host_now_ns()}));
    let out = frames.lock().map(|v| v.clone()).unwrap_or_default();
    let first = first_complete_ns.lock().ok().and_then(|g| *g);
    Ok((out, first, start_ns))
}

fn result(log: &RunLog, slug: &str, status: Status, v: Value) {
    let reasons = v
        .get("reasons")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join("; ")
        })
        .unwrap_or_default();
    match log.result(slug, status, plan::criterion(slug), v) {
        Ok(()) => println!("[m1-sck] RESULT {slug}: {status:?} {reasons}"),
        Err(e) => eprintln!("m1-sck: could not write {slug}: {e}"),
    }
    if status == Status::Fail {
        println!("[m1-sck] {}", plan::STOP_RULE);
    }
}

fn shareable(log: &RunLog) -> (Option<SCShareableContent>, Option<f64>) {
    let t0 = host_now_ns();
    let r = SCShareableContent::create()
        .with_on_screen_windows_only(false)
        .with_exclude_desktop_windows(true)
        .get();
    let ms = (host_now_ns() - t0) as f64 / 1e6;
    match r {
        Ok(c) => {
            log.ev(
                "shareable_content",
                json!({"ms": ms, "windows": c.windows().len()}),
            );
            (Some(c), Some(ms))
        }
        Err(e) => {
            log.ev(
                "shareable_content",
                json!({"ms": ms, "error": format!("{e:?}")}),
            );
            (None, None)
        }
    }
}

fn run_tag(args: &Args, log: Arc<RunLog>) -> Result<ExitCode, String> {
    let seconds: f64 = args.get_or("seconds", 20.0)?;
    let fps: u32 = args.get_or("fps", 60)?;
    let title = args.get("title").unwrap_or(PANEL_TITLE).to_string();
    let pid: Option<i32> = args
        .get("pid")
        .map(str::parse)
        .transpose()
        .map_err(|e| format!("--pid: {e}"))?;
    args.finish()?;
    let pre = preflight();
    log.ev("preflight", json!({"screen_capture_access": pre}));
    if !pre {
        println!(
            "[m1-sck] Screen Recording is not granted to this process's app yet; macOS may ask now (README.md, check 8)."
        );
    }
    // Check 10, part 1: paired Instant/host reads before the capture...
    let origin = Instant::now();
    let mut reads = Vec::new();
    paired_reads(origin, 50, &mut reads);
    let (content, _) = shareable(&log);
    let content = content.ok_or("SCShareableContent failed (Screen Recording not granted?)")?;
    let Some(window) = find_window(&content, &title, pid) else {
        let why = format!("no window titled {title:?}; start `m1-spike tags` first");
        result(
            &log,
            "sck-tag",
            Status::Incomplete,
            json!({"reasons": [why]}),
        );
        return Ok(ExitCode::from(1));
    };
    let (mut stream, target) = window_stream(&window, fps, &log)?;
    println!(
        "[m1-sck] capturing window {} ({}x{} px) for {seconds} s",
        target.window_id, target.width_px, target.height_px
    );
    let (frames, _, _) = capture(&log, &mut stream, &target, seconds, false)?;
    // ...and part 2: paired reads after it, so the drift covers the whole run.
    paired_reads(origin, 50, &mut reads);
    let agreement = agreement_from(&reads);
    let identity = instant_identity();
    let (s8, v8) = verdict::sck_tag(&frames, &target);
    result(&log, "sck-tag", s8, v8);
    let (s10, v10) = verdict::clock(&frames, &identity, agreement.as_ref());
    result(&log, "clock", s10, v10);
    println!(
        "[m1-sck] next: m1-report show {} --write (adds the tag join)",
        log.run_id()
    );
    Ok(if s8 == Status::Fail || s10 == Status::Fail {
        ExitCode::from(1)
    } else {
        ExitCode::SUCCESS
    })
}

/// Code-signing identity of this executable (what TCC keys Screen Recording on): Identifier,
/// CDHash and the Signature kind, never the certificate's subject or team id
/// ([`verdict::codesign_summary`]). The path is home-relative, so the log does not carry the
/// account name.
fn signing_identity() -> Value {
    let exe = std::env::current_exe().ok();
    let path = exe
        .as_ref()
        .map(|p| redact_home(&p.display().to_string()))
        .unwrap_or_default();
    let mut v = json!({"exe": path});
    if let Some(p) = &exe
        && let Ok(out) = Command::new("/usr/bin/codesign")
            .arg("-dvvv")
            .arg(p)
            .output()
    {
        let text = String::from_utf8_lossy(&out.stderr).to_string();
        if let (Value::Object(m), Value::Object(s)) = (&mut v, verdict::codesign_summary(&text)) {
            m.extend(s);
        }
    }
    v["term_program"] = json!(std::env::var("TERM_PROGRAM").ok());
    v["launched_from_terminal"] = json!(std::io::stdin().is_terminal());
    v
}

fn ask_prompt_seen() -> Option<bool> {
    if !std::io::stdin().is_terminal() {
        return None;
    }
    println!(
        "[m1-sck] Did any Screen Recording prompt or \"bypass the private window picker\" alert appear during this run? [y/n]"
    );
    let mut line = String::new();
    std::io::stdin().read_line(&mut line).ok()?;
    consent::parse_yes_no(&json!(line.trim()))
}

fn run_consent(args: &Args, log: Arc<RunLog>) -> Result<ExitCode, String> {
    let scenario = args.get("scenario").unwrap_or("repeat").to_string();
    let prompt_arg = args.get("prompt-seen").map(str::to_ascii_lowercase);
    let title = args.get("title").unwrap_or(PANEL_TITLE).to_string();
    let pid: Option<i32> = args
        .get("pid")
        .map(str::parse)
        .transpose()
        .map_err(|e| format!("--pid: {e}"))?;
    args.finish()?;
    let identity = signing_identity();
    let pre = preflight();
    log.ev(
        "consent_start",
        json!({"scenario": scenario, "preflight_before": pre, "identity": identity}),
    );
    let (content, content_ms) = shareable(&log);
    let mut first_frame_ms = None;
    let mut window_found = false;
    if let Some(c) = &content
        && let Some(w) = find_window(c, &title, pid)
    {
        window_found = true;
        let (mut stream, target) = window_stream(&w, 30, &log)?;
        let (frames, first, start_ns) = capture(&log, &mut stream, &target, 6.0, true)?;
        first_frame_ms = first.map(|f| (f as f64 - start_ns as f64) / 1e6);
        log.ev(
            "consent_capture",
            json!({"frames": frames.len(), "first_complete_frame_ms": first_frame_ms}),
        );
    }
    let post = preflight();
    // The measurement alone, so `m1-report` can redo the verdict once the owner adds a note.
    log.ev(
        "consent_measure",
        json!({"scenario": scenario, "preflight_before": pre, "preflight_after": post,
               "shareable_content_ms": content_ms, "first_complete_frame_ms": first_frame_ms,
               "window_found": window_found}),
    );
    let prompt_seen = match prompt_arg.as_deref() {
        Some(a) => Some(
            consent::parse_yes_no(&json!(a))
                .ok_or_else(|| format!("--prompt-seen {a:?}: use yes or no"))?,
        ),
        None => ask_prompt_seen(),
    };
    let (status, mut reasons) =
        consent::consent(&scenario, pre, content_ms, first_frame_ms, prompt_seen);
    if !window_found {
        reasons.push(format!(
            "no window titled {title:?}; start `m1-spike tags` first"
        ));
    }
    let status = if window_found {
        status
    } else {
        Status::Incomplete
    };
    result(
        &log,
        "consent",
        status,
        json!({"scenario": scenario, "preflight_before": pre, "preflight_after": post,
               "shareable_content_ms": content_ms, "first_complete_frame_ms": first_frame_ms,
               "prompt_seen": prompt_seen, "identity": identity, "reasons": reasons,
               "informational": consent::INFORMATIONAL_SCENARIOS.contains(&scenario.as_str())}),
    );
    if prompt_seen.is_none() {
        println!(
            "[m1-sck] record what you saw: m1-report note {} consent prompt_seen=<true|false> scenario={scenario}",
            log.run_id()
        );
    }
    Ok(if status == Status::Fail {
        ExitCode::from(1)
    } else {
        ExitCode::SUCCESS
    })
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let args = match Args::parse(argv.clone(), &["help"]) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("m1-sck: {e}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let cmd = args.command.clone().unwrap_or_default();
    if args.flag("help") || !matches!(cmd.as_str(), "tag" | "consent") {
        println!("{USAGE}");
        return if args.flag("help") {
            ExitCode::SUCCESS
        } else {
            ExitCode::from(2)
        };
    }
    let check = if cmd == "tag" { "sck-tag" } else { "consent" };
    let run_id = args
        .get("run-id")
        .map(str::to_string)
        .unwrap_or_else(|| new_run_id(check));
    let log = match RunLog::open(&run_id, check, "m1-sck", &argv) {
        Ok(l) => Arc::new(l),
        Err(e) => {
            eprintln!("m1-sck: cannot open the run log: {e}");
            return ExitCode::from(2);
        }
    };
    println!("[m1-sck] run {run_id}\n  log: {}", log.path().display());
    let r = if cmd == "tag" {
        run_tag(&args, log)
    } else {
        run_consent(&args, log)
    };
    match r {
        Ok(code) => code,
        Err(e) => {
            eprintln!("m1-sck: {e}");
            ExitCode::from(1)
        }
    }
}
