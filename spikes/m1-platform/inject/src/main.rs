//! `m1-inject`: posts the hotkey with `CGEventPost` at the HID tap (PLAN.md M1 1a check 9; PRD
//! §10.4 "Replay injects key events with CGEventPost at the HID tap, so the real hotkey path
//! fires").
//!
//! Run it while `m1-spike hotkey --injected` waits with the same run id; `m1-report show <run>
//! --write` then joins every post with the app's handler calls. With `--tap`, a listen-only
//! session event tap also records the OS timestamps of the hotkey's own key events (PRD §10.4:
//! the harness, not the app, holds a listen-only tap and records only the hotkey's timestamps).
//!
//! Posting needs Accessibility for this process's app (System Settings > Privacy & Security >
//! Accessibility); `--tap` also needs Input Monitoring. The injector only checks access
//! (`CGPreflight*`) and never requests it, so it never raises a prompt itself.

use std::ffi::c_void;
use std::process::ExitCode;
use std::ptr::NonNull;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicPtr, Ordering};
use std::time::Duration;

use m1_spike_common::accel::{self, Accel};
use m1_spike_common::args::Args;
use m1_spike_common::clock::{host_now_ns, mach_now_ticks};
use m1_spike_common::jsonl::{RunLog, Status, new_run_id};
use objc2_core_foundation::{CFMachPort, CFRetained, CFRunLoop, kCFRunLoopDefaultMode};
use objc2_core_graphics::{
    CGEvent, CGEventField, CGEventFlags, CGEventMask, CGEventSource, CGEventSourceStateID,
    CGEventTapLocation, CGEventTapOptions, CGEventTapPlacement, CGEventTapProxy, CGEventType,
    CGPreflightListenEventAccess, CGPreflightPostEventAccess,
};
use serde_json::json;

const USAGE: &str = "usage: m1-inject --run-id ID [--hotkey Control+Shift+Space] [--trials 20]
       [--order alternate|key-first|modifier-first] [--hold-ms 600] [--modifier-wait-ms 400]
       [--gap-ms 1200] [--start-delay-ms 3000] [--tap]
Start `m1-spike hotkey --injected --run-id ID --trials N` first, with the same ID and N.";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Release {
    KeyFirst,
    ModifierFirst,
}

/// Plan of one run.
#[derive(Clone, Debug)]
struct Plan {
    accel: Accel,
    trials: usize,
    order: String,
    hold_ms: u64,
    modifier_wait_ms: u64,
    gap_ms: u64,
    start_delay_ms: u64,
    tap: bool,
}

impl Plan {
    fn release_for(&self, trial: usize) -> Release {
        match self.order.as_str() {
            "key-first" => Release::KeyFirst,
            "modifier-first" => Release::ModifierFirst,
            _ if trial.is_multiple_of(2) => Release::KeyFirst,
            _ => Release::ModifierFirst,
        }
    }
}

/// One posting step: which key, down or up, the flag state after it, and its label.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Step {
    key_code: u16,
    down: bool,
    flags_changed: bool,
    flags: u64,
    label: &'static str,
    /// Pause after this step, in ms.
    pause_ms: u64,
}

/// The posting sequence for one trial (pure, so it is unit-tested).
fn steps(plan: &Plan, release: Release) -> Vec<Step> {
    let mods = plan.accel.mods.keys();
    let mut out = Vec::new();
    let mut flags = 0u64;
    for m in &mods {
        flags |= m.flags;
        out.push(Step {
            key_code: m.key_code,
            down: true,
            flags_changed: true,
            flags,
            label: "mod_down",
            pause_ms: 15,
        });
    }
    out.push(Step {
        key_code: plan.accel.key_code,
        down: true,
        flags_changed: false,
        flags,
        label: "key_down",
        pause_ms: plan.hold_ms,
    });
    let mods_up = |flags: &mut u64, out: &mut Vec<Step>, last_pause: u64| {
        for (i, m) in mods.iter().enumerate().rev() {
            *flags &= !m.flags;
            let pause = if i == 0 { last_pause } else { 15 };
            out.push(Step {
                key_code: m.key_code,
                down: false,
                flags_changed: true,
                flags: *flags,
                label: "mod_up",
                pause_ms: pause,
            });
        }
    };
    match release {
        Release::KeyFirst => {
            out.push(Step {
                key_code: plan.accel.key_code,
                down: false,
                flags_changed: false,
                flags,
                label: "key_up",
                pause_ms: 15,
            });
            mods_up(&mut flags, &mut out, plan.gap_ms);
        }
        Release::ModifierFirst => {
            mods_up(&mut flags, &mut out, plan.modifier_wait_ms);
            out.push(Step {
                key_code: plan.accel.key_code,
                down: false,
                flags_changed: false,
                flags: 0,
                label: "key_up",
                pause_ms: plan.gap_ms,
            });
        }
    }
    out
}

fn post(source: &CGEventSource, s: &Step) -> Result<(u64, u64), String> {
    let ev = CGEvent::new_keyboard_event(Some(source), s.key_code, s.down)
        .ok_or("CGEventCreateKeyboardEvent returned NULL")?;
    if s.flags_changed {
        CGEvent::set_type(Some(&ev), CGEventType::FlagsChanged);
    }
    CGEvent::set_flags(Some(&ev), CGEventFlags(s.flags));
    let before = host_now_ns();
    CGEvent::post(CGEventTapLocation::HIDEventTap, Some(&ev));
    Ok((before, host_now_ns()))
}

/// State the listen-only tap's callback reads.
struct TapCtx {
    log: Arc<RunLog>,
    watched: Vec<u16>,
    port: AtomicPtr<CFMachPort>,
}

unsafe extern "C-unwind" fn tap_callback(
    _proxy: CGEventTapProxy,
    ty: CGEventType,
    event: NonNull<CGEvent>,
    user: *mut c_void,
) -> *mut CGEvent {
    let arrival_host_ns = host_now_ns();
    let arrival_mach_ticks = mach_now_ticks();
    if user.is_null() {
        return event.as_ptr();
    }
    // SAFETY: `user` is the TapCtx pointer passed to tap_create; it outlives the tap.
    let ctx = unsafe { &*(user as *const TapCtx) };
    if ty == CGEventType::TapDisabledByTimeout || ty == CGEventType::TapDisabledByUserInput {
        let port = ctx.port.load(Ordering::Acquire);
        if !port.is_null() {
            // SAFETY: the port pointer was stored from a live CFRetained<CFMachPort> that the tap
            // thread keeps alive while the run loop runs.
            CGEvent::tap_enable(unsafe { &*port }, true);
        }
        ctx.log.ev("tap_reenabled", json!({"type": ty.0}));
        return event.as_ptr();
    }
    // SAFETY: the system passes a valid event for the duration of the callback.
    let ev = unsafe { event.as_ref() };
    let key_code = CGEvent::integer_value_field(Some(ev), CGEventField::KeyboardEventKeycode);
    if u16::try_from(key_code).is_ok_and(|k| ctx.watched.contains(&k)) {
        ctx.log.ev(
            "tap_event",
            json!({"type": ty.0, "key_code": key_code, "flags": CGEvent::flags(Some(ev)).0,
                   "cg_timestamp": CGEvent::timestamp(Some(ev)),
                   "arrival_host_ns": arrival_host_ns, "arrival_mach_ticks": arrival_mach_ticks,
                   "autorepeat": CGEvent::integer_value_field(Some(ev), CGEventField::KeyboardEventAutorepeat)}),
        );
    }
    event.as_ptr()
}

/// Runs a listen-only session tap on its own thread until `stop` is set.
fn spawn_tap(
    log: Arc<RunLog>,
    watched: Vec<u16>,
    stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let ctx = Box::new(TapCtx {
            log: log.clone(),
            watched,
            port: AtomicPtr::new(std::ptr::null_mut()),
        });
        let mask: CGEventMask = (1u64 << CGEventType::KeyDown.0)
            | (1u64 << CGEventType::KeyUp.0)
            | (1u64 << CGEventType::FlagsChanged.0);
        let ctx_ptr = &*ctx as *const TapCtx as *mut c_void;
        // SAFETY: tap_callback matches CGEventTapCallBack and ctx_ptr stays valid until after the
        // run loop below has stopped and the tap is invalidated.
        let port: Option<CFRetained<CFMachPort>> = unsafe {
            CGEvent::tap_create(
                CGEventTapLocation::SessionEventTap,
                CGEventTapPlacement::HeadInsertEventTap,
                CGEventTapOptions::ListenOnly,
                mask,
                Some(tap_callback),
                ctx_ptr,
            )
        };
        let Some(port) = port else {
            log.ev(
                "tap_failed",
                json!({"reason": "CGEventTapCreate returned NULL (Input Monitoring not granted?)"}),
            );
            return;
        };
        ctx.port
            .store(CFRetained::as_ptr(&port).as_ptr(), Ordering::Release);
        let Some(source) = CFMachPort::new_run_loop_source(None, Some(&port), 0) else {
            log.ev("tap_failed", json!({"reason": "no run loop source"}));
            return;
        };
        let Some(rl) = CFRunLoop::current() else {
            log.ev("tap_failed", json!({"reason": "no run loop"}));
            return;
        };
        // SAFETY: kCFRunLoopDefaultMode is an immutable CoreFoundation constant.
        let mode = unsafe { kCFRunLoopDefaultMode };
        rl.add_source(Some(&source), mode);
        CGEvent::tap_enable(&port, true);
        log.ev(
            "tap_started",
            json!({"location": "session", "options": "listen_only", "watched": ctx.watched}),
        );
        while !stop.load(Ordering::Relaxed) {
            CFRunLoop::run_in_mode(mode, 0.2, false);
        }
        CGEvent::tap_enable(&port, false);
        port.invalidate();
        ctx.port.store(std::ptr::null_mut(), Ordering::Release);
        drop(ctx);
    })
}

fn run() -> Result<ExitCode, String> {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let args = Args::parse(argv.clone(), &["tap", "help"])?;
    if args.flag("help") {
        println!("{USAGE}");
        return Ok(ExitCode::SUCCESS);
    }
    let hotkey = args
        .get("hotkey")
        .unwrap_or("Control+Shift+Space")
        .to_string();
    let plan = Plan {
        accel: accel::parse(&hotkey)?,
        trials: args.get_or("trials", 20usize)?,
        order: args.get("order").unwrap_or("alternate").to_string(),
        hold_ms: args.get_or("hold-ms", 600u64)?,
        modifier_wait_ms: args.get_or("modifier-wait-ms", 400u64)?,
        gap_ms: args.get_or("gap-ms", 1200u64)?,
        start_delay_ms: args.get_or("start-delay-ms", 3000u64)?,
        tap: args.flag("tap"),
    };
    if !matches!(
        plan.order.as_str(),
        "alternate" | "key-first" | "modifier-first"
    ) {
        return Err(format!(
            "--order {:?}: use alternate, key-first or modifier-first",
            plan.order
        ));
    }
    let run_id = args
        .get("run-id")
        .map(str::to_string)
        .unwrap_or_else(|| new_run_id("inject"));
    args.finish()?;
    let log =
        Arc::new(RunLog::open(&run_id, "inject", "m1-inject", &argv).map_err(|e| e.to_string())?);
    println!("[m1-inject] run {run_id}\n  log: {}", log.path().display());
    let post_ok = CGPreflightPostEventAccess();
    let listen_ok = CGPreflightListenEventAccess();
    log.ev("preflight", json!({"post_event_access": post_ok, "listen_event_access": listen_ok, "hotkey": hotkey, "accel": plan.accel}));
    if !post_ok {
        let why = "no event-posting access: add the app you run this from (Terminal, iTerm, ...) under System Settings > Privacy & Security > Accessibility, then rerun";
        log.result(
            "inject-preflight",
            Status::Incomplete,
            "CGPreflightPostEventAccess must be true before posting",
            json!({"reasons": [why]}),
        )
        .map_err(|e| e.to_string())?;
        println!("[m1-inject] {why}");
        return Ok(ExitCode::from(2));
    }
    let stop = Arc::new(AtomicBool::new(false));
    let tap = if plan.tap {
        if listen_ok {
            let mut watched: Vec<u16> = plan.accel.mods.keys().iter().map(|k| k.key_code).collect();
            watched.push(plan.accel.key_code);
            Some(spawn_tap(log.clone(), watched, stop.clone()))
        } else {
            println!("[m1-inject] --tap: Input Monitoring not granted; continuing without the tap");
            log.ev(
                "tap_skipped",
                json!({"reason": "CGPreflightListenEventAccess false"}),
            );
            None
        }
    } else {
        None
    };
    let source = CGEventSource::new(CGEventSourceStateID::HIDSystemState)
        .ok_or("CGEventSourceCreate failed")?;
    println!(
        "[m1-inject] posting {} x {} in {} ms; do not touch the keyboard",
        plan.trials, hotkey, plan.start_delay_ms
    );
    std::thread::sleep(Duration::from_millis(plan.start_delay_ms));
    for trial in 0..plan.trials {
        let release = plan.release_for(trial);
        for s in steps(&plan, release) {
            let (before, after) = post(&source, &s)?;
            log.ev(
                "posted",
                json!({"trial": trial, "step": s.label, "key_code": s.key_code, "down": s.down,
                       "flags": s.flags, "release": format!("{release:?}"),
                       "host_before_ns": before, "host_after_ns": after}),
            );
            std::thread::sleep(Duration::from_millis(s.pause_ms));
        }
    }
    std::thread::sleep(Duration::from_millis(500));
    stop.store(true, Ordering::Relaxed);
    if let Some(h) = tap {
        let _ = h.join();
    }
    log.ev(
        "inject_done",
        json!({"trials": plan.trials, "order": plan.order}),
    );
    println!("[m1-inject] done. Next: m1-report show {run_id} --write");
    Ok(ExitCode::SUCCESS)
}

fn main() -> ExitCode {
    match run() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("m1-inject: {e}\n{USAGE}");
            ExitCode::from(2)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use m1_spike_common::accel::{FLAG_CONTROL, FLAG_SHIFT};

    fn plan(order: &str) -> Plan {
        Plan {
            accel: accel::parse("Control+Shift+Space").expect("accel"),
            trials: 4,
            order: order.into(),
            hold_ms: 600,
            modifier_wait_ms: 400,
            gap_ms: 1200,
            start_delay_ms: 0,
            tap: false,
        }
    }

    #[test]
    fn key_first_releases_the_key_then_the_modifiers_in_reverse() {
        let s = steps(&plan("key-first"), Release::KeyFirst);
        let labels: Vec<&str> = s.iter().map(|x| x.label).collect();
        assert_eq!(
            labels,
            [
                "mod_down", "mod_down", "key_down", "key_up", "mod_up", "mod_up"
            ]
        );
        assert_eq!(s[2].key_code, 0x31);
        assert!(s[2].flags & FLAG_CONTROL != 0 && s[2].flags & FLAG_SHIFT != 0);
        assert_eq!(s[3].flags, s[2].flags);
        assert_eq!(s.last().map(|x| x.flags), Some(0));
        assert_eq!(s[4].key_code, accel::KVK_SHIFT);
        assert_eq!(s.last().map(|x| x.pause_ms), Some(1200));
    }

    #[test]
    fn modifier_first_lets_go_of_the_modifiers_and_waits() {
        let s = steps(&plan("modifier-first"), Release::ModifierFirst);
        let labels: Vec<&str> = s.iter().map(|x| x.label).collect();
        assert_eq!(
            labels,
            [
                "mod_down", "mod_down", "key_down", "mod_up", "mod_up", "key_up"
            ]
        );
        assert_eq!(s[4].pause_ms, 400);
        assert_eq!(s[5].flags, 0);
        assert!(
            s.iter()
                .filter(|x| x.label != "key_down" && x.label != "key_up")
                .all(|x| x.flags_changed)
        );
    }

    #[test]
    fn alternate_order_switches_every_trial() {
        let p = plan("alternate");
        assert_eq!(p.release_for(0), Release::KeyFirst);
        assert_eq!(p.release_for(1), Release::ModifierFirst);
        assert_eq!(plan("key-first").release_for(1), Release::KeyFirst);
    }
}
