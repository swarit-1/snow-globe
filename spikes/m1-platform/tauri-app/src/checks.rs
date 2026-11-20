//! The spike app's runtime: one `Spike` per process, driven by the global shortcut, the page,
//! the terminal and the audio thread (PLAN.md M1 1a checks 1-7; PRD §5.2, §5.4, §6.3 step 1,
//! §7.1, §10.2-10.6).
//!
//! Every handler runs on the main thread (the shortcut handler and sync commands already do;
//! terminal lines, timers and audio results are posted there), so the `State` mutex is never
//! contended. Every observation is written to the run log as it happens; the verdicts at the end
//! are computed from those same observations by the pure functions in `verdicts`.

use std::io::BufRead;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread::JoinHandle;
use std::time::Duration;

use m1_spike_common::clock::{Stamp, host_now_ns};
use m1_spike_common::hotkey::{self, Edge, EdgeEvent, Order};
use m1_spike_common::jsonl::{RunLog, Status};
use m1_spike_common::netwatch::{NetState, net_state};
use m1_spike_common::{checks as plan, tag};
use objc2_core_graphics::{CGEventSource, CGEventSourceStateID};
use serde_json::{Value, json};
use tauri::{AppHandle, Manager, Wry};
use tauri_nspanel::PanelHandle;
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

use crate::audio::{self, CaptureStats};
use crate::config::{CheckKind, Config};
use crate::macos::{self, FocusSnapshot};
use crate::netwatch::NetWatch;
use crate::panel::PANEL_LABEL;
use crate::speech::{self, EventKind, SpeechSession, SpeechSink};
use crate::verdicts::{
    self, AudioTrial, FocusTrial, PanelTrial, RevealTrial, SpeechOutcome, TYPED_GRACE_NS,
};

/// Platform crate versions this build pins (Cargo.toml), logged with every run.
pub const VERSIONS: [(&str, &str); 5] = [
    ("tauri", "2.12.7"),
    ("tauri-plugin-global-shortcut", "2.4.1"),
    ("global-hotkey", "0.8.0"),
    ("tauri-nspanel", "2.1.1"),
    ("cpal", "0.18.2"),
];

const PANEL_TOKEN: &str = "abc";
const FOCUS_TOKEN: &str = "back";

#[derive(Debug, Default)]
struct State {
    pressed: bool,
    releases: usize,
    /// Check 1: releases observed key-first and modifier-first so far (they drive the prompts).
    key_first_seen: usize,
    modifier_first_seen: usize,
    edges: Vec<EdgeEvent>,
    panel_trials: Vec<PanelTrial>,
    panel_waiting_answer: Option<usize>,
    panel_done: usize,
    focus_trials: Vec<FocusTrial>,
    focus_open: Option<usize>,
    focus_waiting_answer: Option<usize>,
    focus_done: usize,
    reveal_trials: Vec<RevealTrial>,
    hidden_at_ns: u64,
    seq: u16,
    boots: usize,
    audio_trials: Vec<AudioTrial>,
    audio_active: bool,
    audio_started: usize,
    /// Check 7: the interfaces with a route when the speech run started.
    net_at_start: Option<NetState>,
    tag_gen: u16,
}

/// Check 7: how many 250 ms waits for the nettop sample covering the last trial (8 s).
const NET_WAIT_TRIES: u32 = 32;

#[derive(Debug)]
struct Poller {
    stop: Arc<AtomicBool>,
    mods_up_ns: Arc<AtomicU64>,
    mods_seen_held: Arc<AtomicBool>,
    key_seen_down: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

#[derive(Debug)]
enum AudioCmd {
    Start { index: usize, key_down: Stamp },
    Stop { index: usize, key_up: Stamp },
    Quit,
}

/// The running check.
pub struct Spike {
    pub app: AppHandle,
    pub log: Arc<RunLog>,
    pub cfg: Config,
    hotkey: Shortcut,
    esc: Shortcut,
    our_pid: i32,
    panel: PanelHandle<Wry>,
    mask: u64,
    st: Mutex<State>,
    poller: Mutex<Option<Poller>>,
    audio_tx: Mutex<Option<Sender<AudioCmd>>>,
    net: Mutex<Option<NetWatch>>,
    esc_registered: AtomicBool,
    finished: AtomicBool,
}

impl std::fmt::Debug for Spike {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Spike")
            .field("check", &self.cfg.check)
            .finish_non_exhaustive()
    }
}

impl Spike {
    pub fn new(
        app: AppHandle,
        cfg: Config,
        log: Arc<RunLog>,
        hotkey: Shortcut,
        esc: Shortcut,
        panel: PanelHandle<Wry>,
    ) -> Self {
        let mask = macos::modifier_mask(&cfg.accel.mods);
        Self {
            app,
            log,
            cfg,
            hotkey,
            esc,
            our_pid: std::process::id() as i32,
            panel,
            mask,
            st: Mutex::new(State::default()),
            poller: Mutex::new(None),
            audio_tx: Mutex::new(None),
            net: Mutex::new(None),
            esc_registered: AtomicBool::new(false),
            finished: AtomicBool::new(false),
        }
    }

    fn st(&self) -> MutexGuard<'_, State> {
        self.st.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn slug(&self) -> &'static str {
        self.cfg.check.slug()
    }

    /// Prints to the terminal and mirrors the line on the panel.
    fn say(&self, msg: &str) {
        println!("[m1-spike {}] {msg}", self.slug());
        let js = format!(
            "window.__spike && window.__spike.setText({})",
            serde_json::to_string(msg).unwrap_or_else(|_| "\"\"".into())
        );
        self.eval(&js);
    }

    fn eval(&self, js: &str) {
        if let Some(w) = self.app.get_webview_window(PANEL_LABEL)
            && let Err(e) = w.eval(js)
        {
            self.log.ev("eval_error", json!({"error": e.to_string()}));
        }
    }

    fn snap(&self) -> FocusSnapshot {
        macos::snapshot(Some(self.panel.as_panel()))
    }

    /// Runs `f` on the main thread after `ms` milliseconds.
    fn after(self: &Arc<Self>, ms: u64, f: impl FnOnce(&Arc<Spike>) + Send + 'static) {
        let s = self.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(ms));
            let s2 = s.clone();
            if s.app.run_on_main_thread(move || f(&s2)).is_err() {
                eprintln!("m1-spike: could not reach the main thread");
            }
        });
    }

    fn register_esc(&self) {
        if !self.esc_registered.swap(true, Ordering::SeqCst)
            && let Err(e) = self.app.global_shortcut().register(self.esc)
        {
            self.esc_registered.store(false, Ordering::SeqCst);
            self.log
                .ev("esc_register_failed", json!({"error": e.to_string()}));
        }
    }

    fn unregister_esc(&self) {
        if self.esc_registered.swap(false, Ordering::SeqCst)
            && let Err(e) = self.app.global_shortcut().unregister(self.esc)
        {
            self.log
                .ev("esc_unregister_failed", json!({"error": e.to_string()}));
        }
    }

    fn show_panel(&self) -> (u64, u64) {
        let t0 = host_now_ns();
        self.panel.show();
        (t0, host_now_ns())
    }

    fn hide_panel(&self) -> u64 {
        self.panel.hide();
        self.unregister_esc();
        host_now_ns()
    }

    // -----------------------------------------------------------------------------------------
    // Start-up
    // -----------------------------------------------------------------------------------------

    /// Logs the set-up, starts the helper threads and prints the first instruction.
    pub fn start(self: &Arc<Self>) {
        let geometry = macos::geometry(self.panel.as_panel());
        let startup = self.snap();
        self.log.ev(
            "startup",
            json!({
                "check": self.slug(),
                "hotkey": self.cfg.hotkey,
                "accel": self.cfg.accel,
                "throttling": self.cfg.throttling,
                "level": self.cfg.level,
                "versions": VERSIONS.iter().map(|(k, v)| json!({"crate": k, "version": v})).collect::<Vec<_>>(),
                "tauri_version": tauri::VERSION,
                "pid": self.our_pid,
                "speech_linked": speech::library_linked(),
                "speech_version": speech::version(),
                "panel_geometry": geometry,
                "focus": startup,
                "criterion": plan::criterion(self.slug()),
            }),
        );
        self.eval(&format!(
            "window.__spike && window.__spike.setCheck({:?})",
            self.slug()
        ));
        self.st().hidden_at_ns = host_now_ns();
        self.spawn_stdin();
        println!(
            "[m1-spike {}] run {}\n  log: {}\n  hotkey: {}   type q + Return to stop early",
            self.slug(),
            self.log.run_id(),
            self.log.path().display(),
            self.cfg.hotkey
        );
        if let Some(c) = plan::spec(self.slug()) {
            println!(
                "  PLAN M1 1a #{}: {}\n  pass when: {}",
                c.n, c.plan_item, c.criterion
            );
        }
        match self.cfg.check {
            CheckKind::Hotkey => self.hotkey_prompt(),
            CheckKind::Panel => self.say(&format!(
                "trial 1/{}: click into the terminal (or another app's text field), hold {} about 1 s and let go; while the panel is up, type {PANEL_TOKEN} and press Return",
                self.cfg.trials, self.cfg.hotkey
            )),
            CheckKind::Focus => self.focus_prompt(),
            CheckKind::Reveal => self.reveal_prompt(),
            CheckKind::Cpal => {
                self.spawn_audio();
                self.say(&format!(
                    "trial 1/{}: hold {}, say a few words, let go",
                    self.cfg.trials, self.cfg.hotkey
                ));
            }
            CheckKind::Speech => self.speech_start(),
            CheckKind::Tags => self.tags_start(),
        }
    }

    fn spawn_stdin(self: &Arc<Self>) {
        let s = self.clone();
        std::thread::spawn(move || {
            let stdin = std::io::stdin();
            for line in stdin.lock().lines() {
                let Ok(line) = line else { break };
                let stamp = s.log.stamp();
                let s2 = s.clone();
                if s.app
                    .run_on_main_thread(move || s2.on_line(stamp, line))
                    .is_err()
                {
                    break;
                }
            }
        });
    }

    // -----------------------------------------------------------------------------------------
    // Inputs
    // -----------------------------------------------------------------------------------------

    /// Global-shortcut handler (main thread). `stamp` is taken at handler entry.
    pub fn on_shortcut(self: &Arc<Self>, stamp: Stamp, shortcut: &Shortcut, state: ShortcutState) {
        if *shortcut == self.esc {
            if state == ShortcutState::Pressed {
                self.log
                    .ev_at(stamp, "esc", json!({"host_ns": stamp.host_ns}));
                self.escape(stamp);
            }
            return;
        }
        if *shortcut != self.hotkey {
            return;
        }
        match state {
            ShortcutState::Pressed => self.key_down(stamp),
            ShortcutState::Released => self.key_up(stamp),
        }
    }

    fn key_down(self: &Arc<Self>, stamp: Stamp) {
        let (already, requested) = {
            let mut st = self.st();
            let already = st.pressed;
            st.pressed = true;
            (already, self.requested_order(&st))
        };
        let flags = macos::modifier_flags();
        self.log.ev_at(
            stamp,
            "hotkey_edge",
            json!({"edge": "pressed", "host_ns": stamp.host_ns, "while_held": already,
                   "modifier_flags": flags, "requested": requested}),
        );
        if self.cfg.check == CheckKind::Hotkey {
            self.st().edges.push(EdgeEvent {
                edge: Edge::Pressed,
                host_ns: stamp.host_ns,
                modifiers_up_ns: None,
                modifiers_held_at_release: None,
                key_down_at_release: None,
                requested,
            });
            if !already {
                self.start_poller();
            }
            return;
        }
        if already {
            return;
        }
        match self.cfg.check {
            CheckKind::Panel => self.panel_down(stamp),
            CheckKind::Focus => self.focus_down(stamp),
            CheckKind::Reveal => self.reveal_down(stamp),
            CheckKind::Cpal | CheckKind::Speech => self.audio_down(stamp),
            CheckKind::Hotkey | CheckKind::Tags => {}
        }
    }

    fn key_up(self: &Arc<Self>, stamp: Stamp) {
        // Read the modifiers first, before joining the poller (up to one 2 ms tick): whether they
        // are still held tells a key-first release from one that fired on the modifier going up.
        let flags = macos::modifier_flags();
        let mods_held = macos::modifiers_held(flags, self.mask);
        let was = std::mem::replace(&mut self.st().pressed, false);
        let (mods_up, key_down, mods_seen) = self.stop_poller();
        let requested = self.requested_order(&self.st());
        self.log.ev_at(
            stamp,
            "hotkey_edge",
            json!({"edge": "released", "host_ns": stamp.host_ns, "was_pressed": was,
                   "modifiers_up_ns": mods_up, "modifiers_seen_held": mods_seen,
                   "modifiers_held_at_release": mods_held,
                   "key_down_at_release": key_down, "modifier_flags": flags,
                   "release_after_modifiers_ms": mods_up.map(|u| (stamp.host_ns as f64 - u as f64) / 1e6),
                   "requested": requested}),
        );
        match self.cfg.check {
            CheckKind::Hotkey => {
                self.hotkey_up(stamp, mods_up, Some(mods_held), key_down, requested);
            }
            CheckKind::Panel => self.panel_up(stamp),
            CheckKind::Reveal => self.reveal_up(stamp),
            CheckKind::Cpal | CheckKind::Speech => self.audio_up(stamp),
            CheckKind::Focus | CheckKind::Tags => {}
        }
    }

    fn escape(self: &Arc<Self>, stamp: Stamp) {
        match self.cfg.check {
            CheckKind::Focus => self.focus_escape(stamp),
            CheckKind::Panel => {
                let t = self.hide_panel();
                self.log.ev("panel_hidden_by_esc", json!({"host_ns": t}));
            }
            CheckKind::Tags => self.finish("Esc"),
            _ => {}
        }
    }

    /// Messages from the page (sync Tauri command, main thread).
    pub fn on_js(self: &Arc<Self>, stamp: Stamp, ev: Value) {
        let kind = ev
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        match kind.as_str() {
            "tag_drawn" => self.log.ev_at(stamp, "tag_drawn", ev),
            "wake" => {
                self.log.ev_at(stamp, "page_wake", ev.clone());
                self.reveal_wake(stamp, &ev);
            }
            "boot" => {
                self.log.ev_at(stamp, "page_boot", ev);
                self.eval(&format!(
                    "window.__spike && window.__spike.setCheck({:?})",
                    self.slug()
                ));
                self.reveal_boot(stamp);
            }
            "key" => {
                let key = ev
                    .get("key")
                    .and_then(Value::as_str)
                    .unwrap_or("other")
                    .to_string();
                let snap = self.snap();
                self.log.ev_at(
                    stamp,
                    "page_key",
                    json!({"key": key, "page": ev, "focus": snap}),
                );
                if self.cfg.check == CheckKind::Focus {
                    let open = self.st().focus_open;
                    if key == "Escape" && !self.esc_registered.load(Ordering::SeqCst) {
                        self.focus_escape(stamp);
                    } else if let Some(i) = open {
                        // Count a key only while the panel is key (the page can see keys only
                        // then anyway; this records it explicitly).
                        if snap.panel_key == Some(true) {
                            let mut st = self.st();
                            if let Some(t) = st.focus_trials.get_mut(i) {
                                t.keys.push(key);
                            }
                        }
                    }
                }
            }
            _ => self.log.ev_at(stamp, &format!("page_{kind}"), ev),
        }
    }

    /// Panel key-window changes (Tauri `WindowEvent::Focused`, main thread).
    pub fn on_window_focus(self: &Arc<Self>, stamp: Stamp, focused: bool) {
        let snap = self.snap();
        self.log.ev_at(
            stamp,
            "panel_focus",
            json!({"focused": focused, "focus": snap}),
        );
        let mut st = self.st();
        if let Some(t) = st.panel_trials.last_mut()
            && t.hidden_ns.is_none()
        {
            t.focus_events.push(focused);
        }
        if focused
            && let Some(i) = st.focus_open
            && let Some(t) = st.focus_trials.get_mut(i)
            && t.became_key.is_none()
        {
            t.became_key = Some(snap);
        }
    }

    /// A line typed into the terminal (posted to the main thread).
    fn on_line(self: &Arc<Self>, stamp: Stamp, line: String) {
        let l = line.trim().to_ascii_lowercase();
        // Only the expected tokens are logged, never other typing.
        let known = matches!(
            l.as_str(),
            "q" | "quit" | "y" | "n" | PANEL_TOKEN | FOCUS_TOKEN
        );
        self.log.ev_at(
            stamp,
            "terminal_line",
            json!({"token": if known { l.as_str() } else { "other" }}),
        );
        if l == "q" || l == "quit" {
            self.finish("owner stopped the run");
            return;
        }
        match self.cfg.check {
            CheckKind::Panel => self.panel_line(stamp, &l),
            CheckKind::Focus => self.focus_line(stamp, &l),
            _ => {}
        }
    }

    // -----------------------------------------------------------------------------------------
    // Check 1: hotkey press and release
    // -----------------------------------------------------------------------------------------

    fn max_hotkey_trials(&self) -> usize {
        self.cfg.per_order * hotkey::MAX_TRIALS_PER_ORDER
    }

    /// The order to ask for in the current guided check 1 trial (`None` in other checks, in
    /// injected runs, and once both orders are done).
    fn requested_order(&self, st: &State) -> Option<Order> {
        if self.cfg.check != CheckKind::Hotkey || self.cfg.injected {
            return None;
        }
        hotkey::next_request(
            self.cfg.per_order,
            self.max_hotkey_trials(),
            st.releases,
            st.key_first_seen,
            st.modifier_first_seen,
        )
    }

    fn hotkey_prompt(&self) {
        if self.cfg.injected {
            self.say(&format!(
                "waiting for {} injected presses from m1-inject (timeout {} s); do not touch the keyboard",
                self.cfg.trials, self.cfg.seconds
            ));
            let s = self.clone_arc();
            s.after(self.cfg.seconds * 1000, |s| s.finish("timeout"));
            return;
        }
        let (n, kf, mf, requested) = {
            let st = self.st();
            (
                st.releases,
                st.key_first_seen,
                st.modifier_first_seen,
                self.requested_order(&st),
            )
        };
        let per = self.cfg.per_order;
        let max = self.max_hotkey_trials();
        let key = &self.cfg.accel.key_name;
        let msg = match requested {
            None => return,
            Some(Order::KeyFirst) => format!(
                "trial {} (key first, {kf}/{per} seen; at most {max} trials): hold {} about 1 s, let go of {key}, then of the modifiers",
                n + 1,
                self.cfg.hotkey
            ),
            Some(_) => format!(
                "trial {} (modifier first, {mf}/{per} seen; at most {max} trials): hold {}, let go of the modifiers, keep {key} down about 1 s more, then let go of {key}",
                n + 1,
                self.cfg.hotkey
            ),
        };
        self.say(&msg);
    }

    fn clone_arc(&self) -> Arc<Spike> {
        self.app.state::<Arc<Spike>>().inner().clone()
    }

    fn start_poller(&self) {
        let stop = Arc::new(AtomicBool::new(false));
        let mods_up_ns = Arc::new(AtomicU64::new(0));
        let mods_seen_held = Arc::new(AtomicBool::new(false));
        let key_seen_down = Arc::new(AtomicBool::new(false));
        let (s, m, h, k) = (
            stop.clone(),
            mods_up_ns.clone(),
            mods_seen_held.clone(),
            key_seen_down.clone(),
        );
        let mask = self.mask;
        let key_state = self.cfg.key_state;
        let key_code = self.cfg.accel.key_code;
        let handle = std::thread::spawn(move || {
            while !s.load(Ordering::Relaxed) {
                let held = macos::modifiers_held(macos::modifier_flags(), mask);
                if held {
                    h.store(true, Ordering::Relaxed);
                } else if h.load(Ordering::Relaxed) && m.load(Ordering::Relaxed) == 0 {
                    m.store(host_now_ns(), Ordering::Relaxed);
                }
                if key_state && key_is_down(key_code) {
                    k.store(true, Ordering::Relaxed);
                }
                std::thread::sleep(Duration::from_millis(2));
            }
        });
        if let Ok(mut g) = self.poller.lock() {
            *g = Some(Poller {
                stop,
                mods_up_ns,
                mods_seen_held,
                key_seen_down,
                handle: Some(handle),
            });
        }
    }

    /// Stops the poller: (modifiers-up time, key down at release if readable, modifiers seen held).
    fn stop_poller(&self) -> (Option<u64>, Option<bool>, bool) {
        let p = self.poller.lock().ok().and_then(|mut g| g.take());
        let key_now = self
            .cfg
            .key_state
            .then(|| key_is_down(self.cfg.accel.key_code));
        let Some(mut p) = p else {
            return (None, None, false);
        };
        p.stop.store(true, Ordering::Relaxed);
        if let Some(h) = p.handle.take() {
            let _ = h.join();
        }
        let up = p.mods_up_ns.load(Ordering::Relaxed);
        let key = if p.key_seen_down.load(Ordering::Relaxed) {
            key_now
        } else {
            None
        };
        (
            (up != 0).then_some(up),
            key,
            p.mods_seen_held.load(Ordering::Relaxed),
        )
    }

    fn hotkey_up(
        self: &Arc<Self>,
        stamp: Stamp,
        mods_up: Option<u64>,
        mods_held: Option<bool>,
        key_down: Option<bool>,
        requested: Option<Order>,
    ) {
        let k = hotkey::classify(stamp.host_ns, mods_up, mods_held, key_down, requested);
        let (releases, next) = {
            let mut st = self.st();
            st.edges.push(EdgeEvent {
                edge: Edge::Released,
                host_ns: stamp.host_ns,
                modifiers_up_ns: mods_up,
                modifiers_held_at_release: mods_held,
                key_down_at_release: key_down,
                requested,
            });
            st.releases += 1;
            match k.order {
                Order::KeyFirst => st.key_first_seen += 1,
                Order::ModifierFirst => st.modifier_first_seen += 1,
                Order::Simultaneous => {}
            }
            (st.releases, self.requested_order(&st))
        };
        if self.cfg.injected {
            if releases >= self.cfg.trials {
                self.finish("all trials done");
            }
            return;
        }
        println!(
            "  released: observed {:?}, trigger {:?} (basis {:?})",
            k.order, k.trigger, k.basis
        );
        if next.is_none() {
            self.finish("all trials done");
        } else {
            self.hotkey_prompt();
        }
    }

    fn hotkey_result(&self) {
        let edges = self.st().edges.clone();
        let p = hotkey::pair(&edges);
        let min = if self.cfg.injected {
            0
        } else {
            self.cfg.per_order
        };
        let v = hotkey::verdict(&p, min);
        // Injected runs feed check 9 (decided by m1-report's join); they never stand in for the
        // guided check 1.
        let slug = if self.cfg.injected {
            "hotkey-injected"
        } else {
            "hotkey"
        };
        self.write_result(
            slug,
            v.status,
            plan::criterion("hotkey"),
            json!({"verdict": v, "trials": p.trials}),
        );
    }

    // -----------------------------------------------------------------------------------------
    // Check 2: non-activating panel over another app
    // -----------------------------------------------------------------------------------------

    fn panel_down(self: &Arc<Self>, stamp: Stamp) {
        if self.st().panel_waiting_answer.is_some() {
            self.say("answer y or n first");
            return;
        }
        let pre = self.snap();
        let (t0, t1) = self.show_panel();
        let after0 = self.snap();
        let idx = {
            let mut st = self.st();
            let idx = st.panel_trials.len();
            st.panel_trials.push(PanelTrial {
                index: idx,
                pre: Some(pre.clone()),
                shown_ns: t1,
                show_call_us: (t1 - t0) as f64 / 1e3,
                snaps: vec![("+0ms".into(), after0.clone())],
                ..Default::default()
            });
            idx
        };
        self.log.ev_at(
            stamp,
            "panel_shown",
            json!({"trial": idx + 1, "key_down_ns": stamp.host_ns, "show_called_ns": t0, "show_returned_ns": t1,
                   "pre": pre, "snap": {"label": "+0ms", "focus": after0}}),
        );
        self.register_esc();
        for (label, ms) in [("+50ms", 50u64), ("+250ms", 250), ("+1000ms", 1000)] {
            self.after(ms, move |s| {
                let snap = s.snap();
                s.log.ev(
                    "panel_snapshot",
                    json!({"trial": idx + 1, "label": label, "focus": snap}),
                );
                if let Some(t) = s.st().panel_trials.get_mut(idx) {
                    t.snaps.push((label.to_string(), snap));
                }
            });
        }
    }

    fn panel_up(self: &Arc<Self>, stamp: Stamp) {
        let idx = {
            let mut st = self.st();
            let Some(t) = st.panel_trials.last_mut() else {
                return;
            };
            if t.key_up_ns.is_some() {
                return;
            }
            t.key_up_ns = Some(stamp.host_ns);
            t.index
        };
        self.after(self.cfg.hold_after_ms, move |s| s.panel_hide(idx));
    }

    fn panel_hide(self: &Arc<Self>, idx: usize) {
        let t = self.hide_panel();
        let snap = self.snap();
        self.log.ev(
            "panel_hidden",
            json!({"trial": idx + 1, "host_ns": t, "focus": snap}),
        );
        if let Some(tr) = self.st().panel_trials.get_mut(idx) {
            tr.hidden_ns = Some(t);
        }
        self.after(TYPED_GRACE_NS / 1_000_000, move |s| s.panel_settle(idx));
    }

    fn panel_settle(self: &Arc<Self>, idx: usize) {
        let typed = self
            .st()
            .panel_trials
            .get(idx)
            .and_then(|t| t.typed_ns)
            .is_some();
        if typed {
            self.panel_trial_done(idx);
        } else {
            let name = self
                .st()
                .panel_trials
                .get(idx)
                .and_then(|t| t.pre.as_ref())
                .and_then(|p| p.frontmost_name.clone())
                .unwrap_or_else(|| "the other app".into());
            self.st().panel_waiting_answer = Some(idx);
            self.say(&format!(
                "no {PANEL_TOKEN} reached this terminal. If you typed into {name}, did your typing land there while the panel was up? type y or n"
            ));
        }
    }

    fn panel_line(self: &Arc<Self>, stamp: Stamp, l: &str) {
        if l == PANEL_TOKEN {
            let mut st = self.st();
            if let Some(t) = st.panel_trials.last_mut()
                && t.typed_ns.is_none()
            {
                t.typed_ns = Some(stamp.host_ns);
            }
            return;
        }
        if l == "y" || l == "n" {
            let w = self.st().panel_waiting_answer.take();
            if let Some(idx) = w {
                if let Some(t) = self.st().panel_trials.get_mut(idx) {
                    t.owner_confirmed = Some(l == "y");
                }
                self.log
                    .note(
                        "panel_owner_answer",
                        json!({"trial": idx + 1, "typing_reached_other_app": l == "y"}),
                    )
                    .unwrap_or_else(|e| eprintln!("m1-spike: {e}"));
                self.panel_trial_done(idx);
            }
        }
    }

    fn panel_trial_done(self: &Arc<Self>, idx: usize) {
        let (failures, done) = {
            let mut st = self.st();
            st.panel_done += 1;
            let f = st
                .panel_trials
                .get(idx)
                .map(|t| verdicts::panel_trial_failures(t, self.our_pid))
                .unwrap_or_default();
            (f, st.panel_done)
        };
        self.log.ev(
            "panel_trial",
            json!({"trial": idx + 1, "pass": failures.is_empty(), "failures": failures}),
        );
        println!(
            "  trial {}: {}",
            idx + 1,
            if failures.is_empty() {
                "pass".to_string()
            } else {
                failures.join("; ")
            }
        );
        if done >= self.cfg.trials {
            self.finish("all trials done");
        } else {
            self.say(&format!(
                "trial {}/{}: click the other app, hold {} about 1 s and let go; while the panel is up, type {PANEL_TOKEN} and Return",
                done + 1,
                self.cfg.trials,
                self.cfg.hotkey
            ));
        }
    }

    // -----------------------------------------------------------------------------------------
    // Check 3: click-to-key focus and the hand-back on Esc
    // -----------------------------------------------------------------------------------------

    fn focus_prompt(&self) {
        let done = self.st().focus_done;
        self.say(&format!(
            "trial {}/{}: 1) click into the terminal (or another app's text field); 2) press and release {}; 3) click the panel; 4) press M, then the Left arrow; 5) press Esc; 6) without clicking, type {FOCUS_TOKEN} and Return",
            done + 1,
            self.cfg.trials,
            self.cfg.hotkey
        ));
    }

    fn focus_down(self: &Arc<Self>, stamp: Stamp) {
        let busy = {
            let st = self.st();
            st.focus_open.is_some() || st.focus_waiting_answer.is_some()
        };
        if busy {
            return;
        }
        let pre = self.snap();
        let (_t0, t1) = self.show_panel();
        let idx = {
            let mut st = self.st();
            let idx = st.focus_trials.len();
            st.focus_trials.push(FocusTrial {
                index: idx,
                pre: Some(pre.clone()),
                shown_ns: t1,
                ..Default::default()
            });
            st.focus_open = Some(idx);
            idx
        };
        self.register_esc();
        self.log.ev_at(
            stamp,
            "focus_panel_shown",
            json!({"trial": idx + 1, "pre": pre, "shown_ns": t1}),
        );
        self.say("panel up: click it, press M, then the Left arrow, then Esc");
    }

    fn focus_escape(self: &Arc<Self>, stamp: Stamp) {
        let Some(idx) = self.st().focus_open else {
            return;
        };
        let t = self.hide_panel();
        if let Some(tr) = self.st().focus_trials.get_mut(idx) {
            tr.esc_ns = Some(stamp.host_ns);
        }
        self.log.ev_at(
            stamp,
            "focus_esc",
            json!({"trial": idx + 1, "hidden_ns": t}),
        );
        for (label, ms) in [("+50ms", 50u64), ("+250ms", 250)] {
            self.after(ms, move |s| {
                let snap = s.snap();
                s.log.ev(
                    "focus_after_esc",
                    json!({"trial": idx + 1, "label": label, "focus": snap}),
                );
                if let Some(t) = s.st().focus_trials.get_mut(idx) {
                    t.after_esc.push((label.to_string(), snap));
                }
            });
        }
        self.say(&format!(
            "Esc: now type {FOCUS_TOKEN} and Return without clicking anything"
        ));
        self.after(30_000, move |s| {
            let pending = {
                let st = s.st();
                st.focus_open == Some(idx)
                    && st.focus_trials.get(idx).is_some_and(|t| t.typed_ns.is_none())
            };
            if pending {
                s.st().focus_waiting_answer = Some(idx);
                s.say("no text reached this terminal. If you used another app, did your typing land in its text field? type y or n");
            }
        });
    }

    fn focus_line(self: &Arc<Self>, stamp: Stamp, l: &str) {
        let open = self.st().focus_open;
        if l == FOCUS_TOKEN {
            if let Some(idx) = open {
                let esc_done = self
                    .st()
                    .focus_trials
                    .get(idx)
                    .is_some_and(|t| t.esc_ns.is_some());
                if esc_done {
                    if let Some(t) = self.st().focus_trials.get_mut(idx) {
                        t.typed_ns = Some(stamp.host_ns);
                    }
                    // Let the +250 ms snapshot land first.
                    self.after(300, move |s| s.focus_trial_done(idx));
                }
            }
            return;
        }
        if l == "y" || l == "n" {
            let waiting = self.st().focus_waiting_answer.take();
            if let Some(idx) = waiting {
                if let Some(t) = self.st().focus_trials.get_mut(idx) {
                    t.owner_confirmed = Some(l == "y");
                }
                self.log
                    .note(
                        "focus_owner_answer",
                        json!({"trial": idx + 1, "typing_reached_previous_field": l == "y"}),
                    )
                    .unwrap_or_else(|e| eprintln!("m1-spike: {e}"));
                self.focus_trial_done(idx);
            }
        }
    }

    fn focus_trial_done(self: &Arc<Self>, idx: usize) {
        let (failures, done) = {
            let mut st = self.st();
            if st.focus_open != Some(idx) {
                return;
            }
            st.focus_open = None;
            st.focus_done += 1;
            let f = st
                .focus_trials
                .get(idx)
                .map(|t| verdicts::focus_trial_failures(t, self.our_pid))
                .unwrap_or_default();
            (f, st.focus_done)
        };
        self.log.ev(
            "focus_trial",
            json!({"trial": idx + 1, "pass": failures.is_empty(), "failures": failures}),
        );
        println!(
            "  trial {}: {}",
            idx + 1,
            if failures.is_empty() {
                "pass".to_string()
            } else {
                failures.join("; ")
            }
        );
        if done >= self.cfg.trials {
            self.finish("all trials done");
        } else {
            self.focus_prompt();
        }
    }

    // -----------------------------------------------------------------------------------------
    // Check 4: reveal on key-down and wake from suspension
    // -----------------------------------------------------------------------------------------

    fn reveal_needed_ms(&self, done: usize) -> u64 {
        if done < self.cfg.trials {
            self.cfg.idle_secs * 1000
        } else {
            self.cfg.long_idle_secs * 1000
        }
    }

    fn reveal_prompt(self: &Arc<Self>) {
        let done = self.st().reveal_trials.len();
        let total = self.cfg.trials + self.cfg.long_trials;
        if done >= total {
            return;
        }
        let need = self.reveal_needed_ms(done);
        let hidden_at = self.st().hidden_at_ns;
        let waited = (host_now_ns().saturating_sub(hidden_at)) / 1_000_000;
        let wait = need.saturating_sub(waited);
        self.say(&format!(
            "trial {}/{total}: wait for 'ready' ({} s hidden), then hold {} about 1.5 s and let go",
            done + 1,
            need / 1000,
            self.cfg.hotkey
        ));
        self.after(wait, move |s| {
            let still = s.st().reveal_trials.len() == done;
            if still {
                println!("[m1-spike reveal] ready (panel hidden {} s)", need / 1000);
            }
        });
    }

    fn reveal_down(self: &Arc<Self>, stamp: Stamp) {
        let (seq, hidden_for_ms, idx) = {
            let mut st = self.st();
            st.seq = st.seq.wrapping_add(1) & tag::GEN_MASK;
            let hidden_for_ms = (stamp.host_ns.saturating_sub(st.hidden_at_ns)) as f64 / 1e6;
            (st.seq, hidden_for_ms, st.reveal_trials.len())
        };
        let (t0, t1) = self.show_panel();
        let cells: Vec<u8> = tag::cells(seq, 0).iter().map(|&b| u8::from(b)).collect();
        let js = format!(
            "window.__spike && window.__spike.reveal({seq}, {}, {seq}, 0)",
            json!(cells)
        );
        self.eval(&js);
        let t2 = host_now_ns();
        let need = self.reveal_needed_ms(idx) as f64;
        let idle_class = if hidden_for_ms + 1.0 < need {
            "not_idle"
        } else if idx < self.cfg.trials {
            "short"
        } else {
            "long"
        };
        self.log.ev_at(
            stamp,
            "reveal_show",
            json!({"trial": idx + 1, "seq": seq, "hidden_for_ms": hidden_for_ms, "idle_class": idle_class,
                   "key_down_ns": stamp.host_ns, "show_called_ns": t0, "show_returned_ns": t1, "eval_sent_ns": t2,
                   "throttling": self.cfg.throttling}),
        );
        // For the paint join (m1-report): key-down is when this tag was asked for.
        self.log.ev_at(
            stamp,
            "tag_request",
            json!({"gen": seq, "tier": 0, "host_ns": stamp.host_ns, "source": "reveal"}),
        );
        self.st().reveal_trials.push(RevealTrial {
            index: idx,
            seq,
            idle_class: idle_class.into(),
            hidden_for_ms: Some(hidden_for_ms),
            key_down_ns: stamp.host_ns,
            show_returned_ns: t1,
            ..Default::default()
        });
    }

    fn reveal_wake(self: &Arc<Self>, stamp: Stamp, ev: &Value) {
        if self.cfg.check != CheckKind::Reveal {
            return;
        }
        let seq = ev.get("seq").and_then(Value::as_u64).unwrap_or(u64::MAX);
        let mut st = self.st();
        if let Some(t) = st
            .reveal_trials
            .iter_mut()
            .find(|t| u64::from(t.seq) == seq)
            && t.wake_ns.is_none()
        {
            t.wake_ns = Some(stamp.host_ns);
            t.beat_gap_ms = ev.get("beat_gap_ms").and_then(Value::as_f64);
        }
    }

    fn reveal_boot(self: &Arc<Self>, _stamp: Stamp) {
        let pending = {
            let mut st = self.st();
            st.boots += 1;
            if st.boots <= 1 || self.cfg.check != CheckKind::Reveal {
                None
            } else {
                st.reveal_trials
                    .last_mut()
                    .filter(|t| t.wake_ns.is_none())
                    .map(|t| {
                        t.page_reloaded = true;
                        t.seq
                    })
            }
        };
        // The page was reloaded (WebKit can unload a hidden view after about 5 minutes); ask the
        // new page to report its wake for the open trial.
        if let Some(seq) = pending {
            let cells: Vec<u8> = tag::cells(seq, 0).iter().map(|&b| u8::from(b)).collect();
            self.eval(&format!(
                "window.__spike && window.__spike.reveal({seq}, {}, {seq}, 0)",
                json!(cells)
            ));
        }
    }

    fn reveal_up(self: &Arc<Self>, stamp: Stamp) {
        let idx = {
            let mut st = self.st();
            let Some(t) = st.reveal_trials.last_mut() else {
                return;
            };
            if t.key_up_ns.is_some() {
                return;
            }
            t.key_up_ns = Some(stamp.host_ns);
            t.index
        };
        let hidden = self.hide_panel();
        self.st().hidden_at_ns = hidden;
        // Give a late wake report up to 3 s to arrive before judging the trial.
        self.after(3_000, move |s| {
            let t = s.st().reveal_trials.get(idx).cloned();
            if let Some(t) = t {
                let failures = verdicts::reveal_trial_failures(&t);
                s.log.ev(
                    "reveal_trial",
                    json!({"trial": idx + 1, "wake_ms": t.wake_ms(), "pass": failures.is_empty(),
                                                 "failures": failures, "trial_data": t}),
                );
                println!(
                    "  trial {} ({}): wake {} ms{}",
                    idx + 1,
                    t.idle_class,
                    t.wake_ms().map_or("none".into(), |w| format!("{w:.1}")),
                    if failures.is_empty() {
                        String::new()
                    } else {
                        format!(" FAIL: {}", failures.join("; "))
                    }
                );
            }
            let done = s.st().reveal_trials.len();
            if done >= s.cfg.trials + s.cfg.long_trials {
                s.finish("all trials done");
            } else {
                s.reveal_prompt();
            }
        });
    }

    // -----------------------------------------------------------------------------------------
    // Checks 5-7: cpal capture, DictationTranscriber partials, network silence
    // -----------------------------------------------------------------------------------------

    fn spawn_audio(self: &Arc<Self>) {
        let (tx, rx) = mpsc::channel();
        if let Ok(mut g) = self.audio_tx.lock() {
            *g = Some(tx);
        }
        let s = self.clone();
        std::thread::Builder::new()
            .name("audio-control".into())
            .spawn(move || audio_thread(s, rx))
            .map(|_| ())
            .unwrap_or_else(|e| eprintln!("m1-spike: audio thread: {e}"));
    }

    fn send_audio(&self, cmd: AudioCmd) {
        if let Some(tx) = self.audio_tx.lock().ok().and_then(|g| g.clone()) {
            let _ = tx.send(cmd);
        }
    }

    fn speech_start(self: &Arc<Self>) {
        let linked = speech::library_linked();
        let status = speech::status(&self.cfg.locale);
        self.log.ev(
            "speech_library",
            json!({"linked": linked, "version": speech::version(), "locale": self.cfg.locale,
                   "status": status.as_ref().ok(), "status_meaning": status.as_ref().ok().map(|c| speech::status_meaning(*c)),
                   "error": status.as_ref().err()}),
        );
        match status {
            Err(e) => {
                let why = format!(
                    "speech library not linked ({e}); rebuild with --features speech once app/speech-macos is built"
                );
                self.write_result(
                    "speech",
                    Status::Skipped,
                    plan::criterion("speech"),
                    json!({"reasons": [why.clone()]}),
                );
                self.write_result(
                    "netsilence",
                    Status::Skipped,
                    plan::criterion("netsilence"),
                    json!({"reasons": [why.clone()]}),
                );
                self.say(&why);
                self.finish_now();
                return;
            }
            Ok(code) if code != 0 => {
                let why = format!(
                    "lk_speech_status({}) = {code}: {}",
                    self.cfg.locale,
                    speech::status_meaning(code)
                );
                self.write_result(
                    "speech",
                    Status::Incomplete,
                    plan::criterion("speech"),
                    json!({"reasons": [why.clone()], "status": code}),
                );
                self.write_result(
                    "netsilence",
                    Status::Incomplete,
                    plan::criterion("netsilence"),
                    json!({"reasons": [why.clone()]}),
                );
                self.say(&format!("{why}. See README.md, check 6."));
                self.finish_now();
                return;
            }
            Ok(_) => {}
        }
        // Recorded so the Wi-Fi-off cross-check run shows it really was offline (check 7).
        let net = net_state();
        self.log
            .ev("network_state", json!({"at": "start", "state": net}));
        self.st().net_at_start = Some(net);
        if self.cfg.netwatch {
            match NetWatch::start(self.log.clone()) {
                Ok(n) => {
                    self.log.ev("netwatch_started", json!({"host_ns": n.started_host_ns, "args": m1_spike_common::netwatch::NETTOP_ARGS}));
                    if let Ok(mut g) = self.net.lock() {
                        *g = Some(n);
                    }
                }
                Err(e) => self.log.ev("netwatch_error", json!({"error": e})),
            }
        }
        self.spawn_audio();
        self.say(&format!(
            "trial 1/{}: wait 3 s for the network monitor, then hold {}, say a place and a time (\"Mount Rainier at sunset tomorrow\"), let go",
            self.cfg.trials, self.cfg.hotkey
        ));
    }

    fn audio_down(self: &Arc<Self>, stamp: Stamp) {
        let idx = {
            let mut st = self.st();
            if st.audio_active || st.audio_started >= self.cfg.trials {
                return;
            }
            st.audio_active = true;
            st.audio_started += 1;
            st.audio_started - 1
        };
        self.send_audio(AudioCmd::Start {
            index: idx,
            key_down: stamp,
        });
    }

    fn audio_up(self: &Arc<Self>, stamp: Stamp) {
        let idx = {
            let st = self.st();
            if !st.audio_active {
                return;
            }
            st.audio_started - 1
        };
        self.send_audio(AudioCmd::Stop {
            index: idx,
            key_up: stamp,
        });
    }

    fn audio_done(self: &Arc<Self>, trial: AudioTrial) {
        let speech_mode = self.cfg.check == CheckKind::Speech;
        let failures = if speech_mode {
            verdicts::speech_trial_failures(&trial)
        } else {
            verdicts::cpal_trial_failures(&trial)
        };
        let release_to_final_ms = trial.release_to_final_ms();
        let first_partial_ms = trial.speech.as_ref().and_then(|s| {
            s.events
                .iter()
                .find(|e| e.kind == EventKind::Partial && e.text_len > 0)
                .map(|e| (e.host_ns as f64 - trial.key_down_ns as f64) / 1e6)
        });
        self.log.ev(
            if speech_mode { "speech_trial" } else { "cpal_trial" },
            json!({"trial": trial.index + 1, "pass": failures.is_empty(), "failures": failures,
                   "first_buffer_ms": trial.first_buffer_ms(), "release_to_final_ms": release_to_final_ms,
                   "key_up_to_finish_ms": trial.key_up_to_finish_ms(),
                   "finish_to_final_ms": trial.finish_to_final_ms(),
                   "key_down_to_first_partial_ms": first_partial_ms, "data": trial}),
        );
        println!(
            "  trial {}: first buffer {} ms, {} buffers, level {:.1} dBFS{}{}",
            trial.index + 1,
            trial
                .first_buffer_ms()
                .map_or("none".into(), |v| format!("{v:.1}")),
            trial.capture.callbacks,
            trial.capture.rms_dbfs,
            release_to_final_ms.map_or(String::new(), |v| format!(
                ", key-up to FINAL {v:.0} ms (to finish call {}, finish to FINAL {})",
                trial
                    .key_up_to_finish_ms()
                    .map_or("?".into(), |x| format!("{x:.1} ms")),
                trial
                    .finish_to_final_ms()
                    .map_or("?".into(), |x| format!("{x:.0} ms"))
            )),
            if failures.is_empty() {
                String::new()
            } else {
                format!(" FAIL: {}", failures.join("; "))
            }
        );
        let (done, last_window_end) = {
            let mut st = self.st();
            st.audio_active = false;
            st.audio_trials.push(trial);
            let end = st
                .audio_trials
                .iter()
                .map(verdicts::net_window_end)
                .max()
                .unwrap_or(0);
            (st.audio_trials.len(), end)
        };
        if done >= self.cfg.trials {
            if speech_mode && self.cfg.netwatch {
                self.say("trials done; waiting for the nettop sample that covers the last trial");
                self.finish_when_net_covers(last_window_end, NET_WAIT_TRIES);
            } else {
                self.finish("all trials done");
            }
        } else {
            self.say(&format!(
                "trial {}/{}: hold {}, speak, let go",
                done + 1,
                self.cfg.trials,
                self.cfg.hotkey
            ));
        }
    }

    fn audio_results(&self) {
        let trials = self.st().audio_trials.clone();
        let speech_mode = self.cfg.check == CheckKind::Speech;
        let per: Vec<Vec<String>> = trials
            .iter()
            .map(|t| {
                if speech_mode {
                    verdicts::speech_trial_failures(t)
                } else {
                    verdicts::cpal_trial_failures(t)
                }
            })
            .collect();
        let (status, reasons) = verdicts::fold(&per, self.cfg.trials);
        let first: Vec<f64> = trials
            .iter()
            .filter_map(AudioTrial::first_buffer_ms)
            .collect();
        let intervals: Vec<f64> = trials
            .iter()
            .filter_map(|t| t.capture.interval_ms.as_ref().map(|s| s.p50))
            .collect();
        let summary = json!({
            "trials": trials.len(),
            "first_buffer_ms": m1_spike_common::stats::summarize(&first),
            "interval_p50_ms_per_trial": intervals,
            "reasons": reasons,
        });
        if !speech_mode {
            self.write_result("cpal", status, plan::criterion("cpal"), summary);
            return;
        }
        let split = |f: fn(&AudioTrial) -> Option<f64>| -> Vec<f64> {
            trials.iter().filter_map(f).collect()
        };
        let summarize = m1_spike_common::stats::summarize;
        let net_at_start = self.st().net_at_start.clone();
        let mut v = summary;
        v["release_to_final_ms"] = json!(summarize(&split(AudioTrial::release_to_final_ms)));
        v["key_up_to_finish_ms"] = json!(summarize(&split(AudioTrial::key_up_to_finish_ms)));
        v["finish_to_final_ms"] = json!(summarize(&split(AudioTrial::finish_to_final_ms)));
        v["network_at_start"] = json!(net_at_start);
        self.write_result("speech", status, plan::criterion("speech"), v);
        // Check 7.
        let net = self
            .net
            .lock()
            .ok()
            .and_then(|g| g.as_ref().map(NetWatch::samples));
        match net {
            None => self.write_result(
                "netsilence",
                if self.cfg.netwatch { Status::Incomplete } else { Status::Skipped },
                plan::criterion("netsilence"),
                json!({"reasons": [if self.cfg.netwatch { "nettop did not start" } else { "--no-netwatch" }]}),
            ),
            Some(samples) => {
                // Only a successful read with no routable interface marks the offline run.
                let online = net_at_start
                    .as_ref()
                    .is_none_or(|n| n.online || n.error.is_some());
                let (status, reasons, windows) = verdicts::netsilence(
                    &trials,
                    &samples,
                    std::process::id(),
                    self.cfg.trials,
                    online,
                );
                self.write_result(
                    "netsilence",
                    status,
                    plan::criterion("netsilence"),
                    json!({"windows": windows, "samples": samples.len(), "reasons": reasons,
                           "network_at_start": net_at_start, "network_at_end": net_state()}),
                );
            }
        }
    }

    /// Check 7: finishes once a complete nettop sample reaches `end_ns` (the last trial
    /// window's end), checking every 250 ms, at most `tries` more times. A window still not
    /// covered then is judged incomplete, never silently passed.
    fn finish_when_net_covers(self: &Arc<Self>, end_ns: u64, tries: u32) {
        let covered = self
            .net
            .lock()
            .ok()
            .and_then(|g| g.as_ref().map(|n| n.covers_until(end_ns)))
            .unwrap_or(true);
        if covered || tries == 0 {
            self.log.ev(
                "netwatch_wait",
                json!({"window_end_ns": end_ns, "covered": covered}),
            );
            self.finish("all trials done");
            return;
        }
        self.after(250, move |s| s.finish_when_net_covers(end_ns, tries - 1));
    }

    // -----------------------------------------------------------------------------------------
    // Tags (supports checks 8, 10 and 11, which m1-sck decides)
    // -----------------------------------------------------------------------------------------

    fn tags_start(self: &Arc<Self>) {
        let (_t0, t1) = self.show_panel();
        let g = macos::geometry(self.panel.as_panel());
        self.log
            .ev("tags_panel_shown", json!({"host_ns": t1, "geometry": g}));
        self.say(&format!(
            "panel up for {} s, tag every {} ms. In another terminal run m1-sck (README.md checks 8, 10, 11). Esc or q stops.",
            self.cfg.seconds, self.cfg.period_ms
        ));
        let s = self.clone();
        let period = self.cfg.period_ms;
        let total_ms = self.cfg.seconds * 1000;
        std::thread::spawn(move || {
            let mut elapsed = 0u64;
            while elapsed < total_ms && !s.finished.load(Ordering::SeqCst) {
                let s2 = s.clone();
                let _ = s.app.run_on_main_thread(move || s2.tag_tick());
                std::thread::sleep(Duration::from_millis(period));
                elapsed += period;
            }
            let s2 = s.clone();
            let _ = s.app.run_on_main_thread(move || s2.finish("tags done"));
        });
    }

    fn tag_tick(self: &Arc<Self>) {
        let gen_ = {
            let mut st = self.st();
            st.tag_gen = st.tag_gen.wrapping_add(1) & tag::GEN_MASK;
            st.tag_gen
        };
        let tier = (gen_ % 3) as u8;
        let cells: Vec<u8> = tag::cells(gen_, tier)
            .iter()
            .map(|&b| u8::from(b))
            .collect();
        let stamp = self.log.stamp();
        self.log.ev_at(
            stamp,
            "tag_request",
            json!({"gen": gen_, "tier": tier, "host_ns": stamp.host_ns, "source": "tags"}),
        );
        self.eval(&format!(
            "window.__spike && window.__spike.setTag({}, {gen_}, {tier}, {gen_})",
            json!(cells)
        ));
    }

    // -----------------------------------------------------------------------------------------
    // Results and exit
    // -----------------------------------------------------------------------------------------

    fn write_result(&self, slug: &str, status: Status, criterion: &str, v: Value) {
        match self.log.result(slug, status, criterion, v.clone()) {
            Ok(()) => {
                let reasons = v
                    .get("reasons")
                    .or_else(|| v.get("verdict").and_then(|x| x.get("reasons")))
                    .and_then(Value::as_array)
                    .map(|a| {
                        a.iter()
                            .filter_map(Value::as_str)
                            .collect::<Vec<_>>()
                            .join("; ")
                    })
                    .unwrap_or_default();
                println!("[m1-spike] RESULT {slug}: {status:?} {reasons}");
                if status == Status::Fail {
                    println!("[m1-spike] {}", plan::STOP_RULE);
                }
            }
            Err(e) => eprintln!("m1-spike: could not write the {slug} result: {e}"),
        }
    }

    /// Writes this check's verdicts, stops helpers and exits the app.
    pub fn finish(self: &Arc<Self>, reason: &str) {
        if self.finished.swap(true, Ordering::SeqCst) {
            return;
        }
        self.log.ev("finish", json!({"reason": reason}));
        match self.cfg.check {
            CheckKind::Hotkey => self.hotkey_result(),
            CheckKind::Panel => {
                let trials = self.st().panel_trials.clone();
                let per: Vec<Vec<String>> = trials
                    .iter()
                    .filter(|t| t.typed_ns.is_some() || t.owner_confirmed.is_some())
                    .map(|t| verdicts::panel_trial_failures(t, self.our_pid))
                    .collect();
                let (status, reasons) = verdicts::fold(&per, self.cfg.trials);
                self.write_result(
                    "panel",
                    status,
                    plan::criterion("panel"),
                    json!({"reasons": reasons, "trials": trials}),
                );
            }
            CheckKind::Focus => {
                let trials = self.st().focus_trials.clone();
                let done = self.st().focus_done;
                let per: Vec<Vec<String>> = trials
                    .iter()
                    .take(done)
                    .map(|t| verdicts::focus_trial_failures(t, self.our_pid))
                    .collect();
                let (status, reasons) = verdicts::fold(&per, self.cfg.trials);
                self.write_result(
                    "focus",
                    status,
                    plan::criterion("focus"),
                    json!({"reasons": reasons, "trials": trials}),
                );
            }
            CheckKind::Reveal => {
                let trials = self.st().reveal_trials.clone();
                let per: Vec<Vec<String>> =
                    trials.iter().map(verdicts::reveal_trial_failures).collect();
                let (status, reasons) =
                    verdicts::fold(&per, self.cfg.trials + self.cfg.long_trials);
                self.write_result(
                    "reveal",
                    status,
                    plan::criterion("reveal"),
                    json!({"reasons": reasons, "by_idle_class": verdicts::wake_summaries(&trials),
                           "throttling": self.cfg.throttling, "page_boots": self.st().boots, "trials": trials}),
                );
            }
            CheckKind::Cpal | CheckKind::Speech => self.audio_results(),
            CheckKind::Tags => {}
        }
        self.finish_now();
    }

    fn finish_now(self: &Arc<Self>) {
        self.finished.store(true, Ordering::SeqCst);
        self.send_audio(AudioCmd::Quit);
        if let Some(n) = self.net.lock().ok().and_then(|mut g| g.take()) {
            n.stop();
        }
        self.unregister_esc();
        println!(
            "[m1-spike] done. Report: m1-report show {}",
            self.log.run_id()
        );
        self.app.exit(0);
    }
}

/// `CGEventSourceKeyState` for the main key (only with `--key-state`; it may need Input Monitoring).
fn key_is_down(key_code: u16) -> bool {
    CGEventSource::key_state(CGEventSourceStateID::HIDSystemState, key_code)
}

/// One trial in flight on the audio control thread.
struct Active {
    index: usize,
    key_down: Stamp,
    cmd_received_ns: u64,
    playing_ns: Option<u64>,
    open_error: Option<String>,
    stats: Arc<CaptureStats>,
    stream: Option<cpal::Stream>,
    session: Option<SpeechSession>,
    sink: Option<Arc<SpeechSink>>,
    start_error: Option<String>,
}

/// Opens the microphone (and the speech session) on key-down, closes them on key-up, and posts
/// each finished trial to the main thread. Runs off the main thread so opening the device never
/// delays the panel reveal (PRD §6.3 step 1).
fn audio_thread(spike: Arc<Spike>, rx: Receiver<AudioCmd>) {
    let speech_mode = spike.cfg.check == CheckKind::Speech;
    let mut active: Option<Active> = None;
    while let Ok(cmd) = rx.recv() {
        match cmd {
            AudioCmd::Quit => break,
            AudioCmd::Start { index, key_down } => {
                if active.is_some() {
                    continue;
                }
                let cmd_received_ns = host_now_ns();
                let stats = Arc::new(CaptureStats::new());
                let mut a = Active {
                    index,
                    key_down,
                    cmd_received_ns,
                    playing_ns: None,
                    open_error: None,
                    stats: stats.clone(),
                    stream: None,
                    session: None,
                    sink: None,
                    start_error: None,
                };
                let mut info_v = Value::Null;
                let mut speech_started_ns = None;
                match audio::default_input() {
                    Err(e) => a.open_error = Some(e),
                    Ok((device, config, info)) => {
                        info_v = json!(info);
                        let mut pusher = None;
                        if speech_mode {
                            let sink = Arc::new(SpeechSink::new(index + 1, spike.cfg.print_text));
                            match SpeechSession::start(
                                &spike.cfg.locale,
                                f64::from(info.sample_rate),
                                sink.clone(),
                            ) {
                                Ok(s) => {
                                    speech_started_ns = Some(host_now_ns());
                                    pusher = Some(s.pusher());
                                    a.session = Some(s);
                                }
                                Err(e) => a.start_error = Some(e),
                            }
                            a.sink = Some(sink);
                        }
                        match audio::open_stream(&device, config, stats, pusher) {
                            Ok(s) => {
                                a.playing_ns = Some(host_now_ns());
                                a.stream = Some(s);
                            }
                            Err(e) => a.open_error = Some(e),
                        }
                    }
                }
                spike.log.ev(
                    "audio_open",
                    json!({"trial": index + 1, "key_down_ns": key_down.host_ns, "cmd_received_ns": cmd_received_ns,
                           "speech_started_ns": speech_started_ns, "playing_ns": a.playing_ns,
                           "open_ms": a.playing_ns.map(|p| (p as f64 - key_down.host_ns as f64) / 1e6),
                           "error": a.open_error, "speech_error": a.start_error, "input": info_v}),
                );
                active = Some(a);
            }
            AudioCmd::Stop { index, key_up } => {
                let stop_received_ns = host_now_ns();
                let Some(mut a) = active.take() else { continue };
                if a.index != index {
                    spike
                        .log
                        .ev("audio_mismatch", json!({"stop": index, "active": a.index}));
                }
                // The stream goes first, so no push can follow lk_speech_finish. Its teardown is
                // timed separately (key_up_to_finish_ms) from the library's finish_to_final_ms.
                drop(a.stream.take());
                let closed_ns = host_now_ns();
                let speech = if speech_mode {
                    let events_of = |s: &Option<Arc<SpeechSink>>| {
                        s.as_ref().map(|k| k.events()).unwrap_or_default()
                    };
                    Some(match a.session.take() {
                        Some(session) => {
                            let finish_called_ns = host_now_ns();
                            let rc = session.finish();
                            let finish_returned_ns = host_now_ns();
                            let ok = a
                                .sink
                                .as_ref()
                                .is_some_and(|k| k.wait_terminal(Duration::from_secs(10)));
                            session.close();
                            let events = events_of(&a.sink);
                            let final_ns = events
                                .iter()
                                .find(|e| e.kind == EventKind::Final)
                                .map(|e| e.host_ns);
                            SpeechOutcome {
                                start_error: None,
                                finish_rc: Some(rc),
                                finish_called_ns: Some(finish_called_ns),
                                finish_returned_ns: Some(finish_returned_ns),
                                terminal_within_timeout: ok,
                                events,
                                final_ns,
                            }
                        }
                        None => SpeechOutcome {
                            start_error: Some(
                                a.start_error.clone().unwrap_or_else(|| "no session".into()),
                            ),
                            finish_rc: None,
                            finish_called_ns: None,
                            finish_returned_ns: None,
                            terminal_within_timeout: false,
                            events: events_of(&a.sink),
                            final_ns: None,
                        },
                    })
                } else {
                    None
                };
                let trial = AudioTrial {
                    index: a.index,
                    key_down_ns: a.key_down.host_ns,
                    cmd_received_ns: a.cmd_received_ns,
                    playing_ns: a.playing_ns,
                    open_error: a.open_error.take(),
                    key_up_ns: key_up.host_ns,
                    stop_received_ns,
                    closed_ns,
                    capture: a.stats.summary(),
                    speech,
                };
                let s2 = spike.clone();
                if spike
                    .app
                    .run_on_main_thread(move || s2.audio_done(trial))
                    .is_err()
                {
                    break;
                }
            }
        }
    }
}
