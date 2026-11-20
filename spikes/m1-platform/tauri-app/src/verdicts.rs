//! Pure trial evaluation for the app's checks (PLAN.md M1 1a checks 2-7), kept apart from the
//! AppKit and Tauri code so the pass/fail logic is unit-tested without a window.

use m1_spike_common::jsonl::Status;
use m1_spike_common::netwatch::{Group, Sample, WindowBytes, window_bytes};
use m1_spike_common::stats::{Summary, summarize};
use serde::Serialize;

use crate::audio::CaptureSummary;
use crate::macos::FocusSnapshot;
use crate::speech::{EventKind, SpeechEvent};

/// One panel trial (check 2).
#[derive(Clone, Debug, Default, Serialize)]
pub struct PanelTrial {
    pub index: usize,
    pub pre: Option<FocusSnapshot>,
    pub shown_ns: u64,
    pub show_call_us: f64,
    /// Snapshots labelled "+0ms", "+50ms", "+250ms", "+1000ms".
    pub snaps: Vec<(String, FocusSnapshot)>,
    pub key_up_ns: Option<u64>,
    pub hidden_ns: Option<u64>,
    /// When the typed token arrived on the terminal while (or just after) the panel was up.
    pub typed_ns: Option<u64>,
    /// The owner's answer when they typed into another app instead of the terminal.
    pub owner_confirmed: Option<bool>,
    /// Panel focus events seen during the trial (true = became key).
    pub focus_events: Vec<bool>,
}

/// Grace after the panel hides within which a typed line still counts (Return may come late).
pub const TYPED_GRACE_NS: u64 = 2_000_000_000;

fn focus_kept(pre: &FocusSnapshot, s: &FocusSnapshot, our_pid: i32) -> Vec<String> {
    let mut r = Vec::new();
    if s.frontmost_pid != pre.frontmost_pid {
        r.push(format!(
            "frontmost app changed from {:?} to {:?}",
            pre.frontmost_name, s.frontmost_name
        ));
    }
    if s.frontmost_pid == Some(our_pid) {
        r.push("the spike app became frontmost".into());
    }
    if s.app_active == Some(true) {
        r.push("the spike app became active".into());
    }
    r
}

/// Check 2 for one trial: the reasons it failed (empty = pass).
pub fn panel_trial_failures(t: &PanelTrial, our_pid: i32) -> Vec<String> {
    let mut r = Vec::new();
    let Some(pre) = &t.pre else {
        return vec!["no snapshot before the reveal".into()];
    };
    if pre.frontmost_pid == Some(our_pid) {
        r.push("the spike app was frontmost before the reveal; click another app first".into());
    }
    for label in ["+0ms", "+50ms", "+250ms", "+1000ms"] {
        match t.snaps.iter().find(|(l, _)| l == label) {
            None => r.push(format!("missing {label} snapshot")),
            Some((_, s)) => {
                for why in focus_kept(pre, s, our_pid) {
                    r.push(format!("{label}: {why}"));
                }
                if s.panel_key == Some(true) {
                    r.push(format!("{label}: the panel became key"));
                }
            }
        }
    }
    match t.snaps.iter().find(|(l, _)| l == "+250ms") {
        Some((_, s)) if s.panel_visible == Some(true) && s.panel_on_screen == Some(true) => {}
        Some(_) => r.push("+250ms: the panel was not visible on screen".into()),
        None => {}
    }
    if t.focus_events.iter().any(|&k| k) {
        r.push("the panel received a became-key event".into());
    }
    let typed_ok = match (t.typed_ns, t.hidden_ns) {
        (Some(ty), Some(h)) => ty >= t.shown_ns && ty <= h + TYPED_GRACE_NS,
        (Some(ty), None) => ty >= t.shown_ns,
        _ => false,
    };
    if !typed_ok && t.owner_confirmed != Some(true) {
        r.push("no evidence that typing reached the other app while the panel was up".into());
    }
    r
}

/// One click-to-key trial (check 3).
#[derive(Clone, Debug, Default, Serialize)]
pub struct FocusTrial {
    pub index: usize,
    pub pre: Option<FocusSnapshot>,
    pub shown_ns: u64,
    /// Snapshot when the panel reported becoming key after the click.
    pub became_key: Option<FocusSnapshot>,
    /// Test keys the page received while key (only M, L, C, arrows, Escape are named).
    pub keys: Vec<String>,
    pub esc_ns: Option<u64>,
    /// Snapshots after Esc: "+50ms", "+250ms".
    pub after_esc: Vec<(String, FocusSnapshot)>,
    pub typed_ns: Option<u64>,
    pub owner_confirmed: Option<bool>,
}

/// Check 3 for one trial: the reasons it failed (empty = pass).
pub fn focus_trial_failures(t: &FocusTrial, our_pid: i32) -> Vec<String> {
    let mut r = Vec::new();
    let Some(pre) = &t.pre else {
        return vec!["no snapshot before the reveal".into()];
    };
    match &t.became_key {
        None => r.push("the panel never became key after the click".into()),
        Some(s) => {
            if s.panel_key != Some(true) {
                r.push("became-key snapshot does not show the panel as key".into());
            }
            for why in focus_kept(pre, s, our_pid) {
                r.push(format!("while key: {why}"));
            }
        }
    }
    let has = |k: &str| t.keys.iter().any(|x| x.eq_ignore_ascii_case(k));
    if !has("m") {
        r.push("the page did not receive M".into());
    }
    if !has("ArrowLeft") {
        r.push("the page did not receive the Left arrow".into());
    }
    match t.esc_ns {
        None => r.push("Esc never arrived".into()),
        Some(_) => {
            for label in ["+50ms", "+250ms"] {
                match t.after_esc.iter().find(|(l, _)| l == label) {
                    None => r.push(format!("missing Esc {label} snapshot")),
                    Some((_, s)) => {
                        if s.panel_key == Some(true) || s.panel_visible == Some(true) {
                            r.push(format!("Esc {label}: the panel is still key or visible"));
                        }
                        for why in focus_kept(pre, s, our_pid) {
                            r.push(format!("Esc {label}: {why}"));
                        }
                    }
                }
            }
        }
    }
    let typed_ok = matches!((t.typed_ns, t.esc_ns), (Some(ty), Some(e)) if ty >= e);
    if !typed_ok && t.owner_confirmed != Some(true) {
        r.push("no evidence that the next typing reached the previous text field".into());
    }
    r
}

/// One reveal trial (check 4).
#[derive(Clone, Debug, Default, Serialize)]
pub struct RevealTrial {
    pub index: usize,
    pub seq: u16,
    /// "short", "long" or "not_idle" (pressed before the idle wait was over).
    pub idle_class: String,
    pub hidden_for_ms: Option<f64>,
    pub key_down_ns: u64,
    pub show_returned_ns: u64,
    pub key_up_ns: Option<u64>,
    pub wake_ns: Option<u64>,
    /// The heartbeat gap the page saw at the reveal (shows suspension vs throttling).
    pub beat_gap_ms: Option<f64>,
    pub page_reloaded: bool,
}

impl RevealTrial {
    /// Key-down to the page's first animation frame report, in ms.
    pub fn wake_ms(&self) -> Option<f64> {
        self.wake_ns
            .map(|w| (w as f64 - self.key_down_ns as f64) / 1e6)
    }
}

/// Check 4 for one trial.
pub fn reveal_trial_failures(t: &RevealTrial) -> Vec<String> {
    let mut r = Vec::new();
    match (t.wake_ns, t.key_up_ns) {
        (None, _) => r.push("the page never reported waking up".into()),
        (Some(w), Some(u)) if w > u => r.push(format!(
            "the page woke {:.1} ms after key-up",
            (w as f64 - u as f64) / 1e6
        )),
        (Some(_), None) => r.push("no key-up recorded".into()),
        _ => {}
    }
    r
}

/// Wake-time summaries per idle class.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct WakeSummary {
    pub idle_class: String,
    pub wake_ms: Option<Summary>,
    pub beat_gap_ms: Option<Summary>,
    pub reloads: usize,
}

pub fn wake_summaries(trials: &[RevealTrial]) -> Vec<WakeSummary> {
    let mut classes: Vec<String> = trials.iter().map(|t| t.idle_class.clone()).collect();
    classes.sort();
    classes.dedup();
    classes
        .into_iter()
        .map(|c| {
            let ts: Vec<&RevealTrial> = trials.iter().filter(|t| t.idle_class == c).collect();
            WakeSummary {
                wake_ms: summarize(&ts.iter().filter_map(|t| t.wake_ms()).collect::<Vec<_>>()),
                beat_gap_ms: summarize(
                    &ts.iter().filter_map(|t| t.beat_gap_ms).collect::<Vec<_>>(),
                ),
                reloads: ts.iter().filter(|t| t.page_reloaded).count(),
                idle_class: c,
            }
        })
        .collect()
}

/// One audio trial (checks 5 and 6).
#[derive(Clone, Debug, Serialize)]
pub struct AudioTrial {
    pub index: usize,
    pub key_down_ns: u64,
    pub cmd_received_ns: u64,
    pub playing_ns: Option<u64>,
    pub open_error: Option<String>,
    pub key_up_ns: u64,
    /// When the audio thread received the key-up (the hand-off from the shortcut handler).
    pub stop_received_ns: u64,
    /// After the cpal stream was dropped (CoreAudio stopped and disposed).
    pub closed_ns: u64,
    pub capture: CaptureSummary,
    pub speech: Option<SpeechOutcome>,
}

fn ms_between(earlier_ns: u64, later_ns: u64) -> f64 {
    (later_ns as f64 - earlier_ns as f64) / 1e6
}

impl AudioTrial {
    /// Key-down handler to the first buffer, in ms.
    pub fn first_buffer_ms(&self) -> Option<f64> {
        self.capture
            .first_callback_host_ns
            .map(|f| ms_between(self.key_down_ns, f))
    }

    /// Key-up to FINAL, in ms: D-004's release-to-final measure. In this spike it includes the
    /// hand-off to the audio thread and the stream teardown before `lk_speech_finish`
    /// ([`Self::key_up_to_finish_ms`]) as well as the library's own time
    /// ([`Self::finish_to_final_ms`]).
    pub fn release_to_final_ms(&self) -> Option<f64> {
        let s = self.speech.as_ref()?;
        s.final_ns.map(|f| ms_between(self.key_up_ns, f))
    }

    /// Key-up to the `lk_speech_finish` call, in ms (hand-off plus stream teardown).
    pub fn key_up_to_finish_ms(&self) -> Option<f64> {
        let s = self.speech.as_ref()?;
        s.finish_called_ns.map(|f| ms_between(self.key_up_ns, f))
    }

    /// The `lk_speech_finish` call to FINAL, in ms (the library's flush and finalize).
    pub fn finish_to_final_ms(&self) -> Option<f64> {
        let s = self.speech.as_ref()?;
        match (s.finish_called_ns, s.final_ns) {
            (Some(c), Some(f)) => Some(ms_between(c, f)),
            _ => None,
        }
    }
}

/// What the speech library did in one trial.
#[derive(Clone, Debug, Serialize)]
pub struct SpeechOutcome {
    pub start_error: Option<String>,
    pub finish_rc: Option<i32>,
    /// Host clock just before and just after `lk_speech_finish`.
    pub finish_called_ns: Option<u64>,
    pub finish_returned_ns: Option<u64>,
    pub terminal_within_timeout: bool,
    pub events: Vec<SpeechEvent>,
    /// The first FINAL's arrival.
    pub final_ns: Option<u64>,
}

impl SpeechOutcome {
    pub fn count(&self, k: EventKind) -> usize {
        self.events.iter().filter(|e| e.kind == k).count()
    }

    /// FINAL events that arrived before `lk_speech_finish` was called (all of them when it was
    /// never called).
    pub fn finals_before_finish(&self) -> usize {
        self.events
            .iter()
            .filter(|e| e.kind == EventKind::Final)
            .filter(|e| self.finish_called_ns.is_none_or(|c| e.host_ns < c))
            .count()
    }
}

/// Check 5 for one trial.
pub fn cpal_trial_failures(t: &AudioTrial) -> Vec<String> {
    let mut r = Vec::new();
    if let Some(e) = &t.open_error {
        r.push(format!("could not open the input: {e}"));
        return r;
    }
    match t.capture.first_callback_host_ns {
        None => r.push("no buffer arrived".into()),
        Some(f) if f > t.key_up_ns => r.push("the first buffer arrived after key-up".into()),
        _ => {}
    }
    if t.capture.callbacks > 0 && t.capture.nonzero_samples == 0 {
        r.push(
            "every sample was zero (Microphone permission denied, or the input is muted)".into(),
        );
    }
    if t.capture.errors > 0 {
        r.push(format!(
            "{} stream error(s): {:?}",
            t.capture.errors, t.capture.last_error
        ));
    }
    if t.closed_ns < t.key_up_ns {
        r.push("the stream closed before key-up".into());
    }
    r
}

/// Check 6 for one trial.
pub fn speech_trial_failures(t: &AudioTrial) -> Vec<String> {
    let mut r = cpal_trial_failures(t);
    let Some(s) = &t.speech else {
        r.push("no speech session".into());
        return r;
    };
    if let Some(e) = &s.start_error {
        r.push(format!("lk_speech_start failed: {e}"));
        return r;
    }
    let partials = s
        .events
        .iter()
        .filter(|e| e.kind == EventKind::Partial && e.text_len > 0)
        .count();
    if partials == 0 {
        r.push("no PARTIAL with text".into());
    }
    let finals = s.count(EventKind::Final);
    if finals != 1 {
        r.push(format!("{finals} FINAL event(s), expected exactly 1"));
    }
    let early = s.finals_before_finish();
    if early > 0 {
        r.push(format!(
            "{early} FINAL event(s) arrived before lk_speech_finish was called"
        ));
    }
    let errors = s.count(EventKind::Error);
    if errors > 0 {
        r.push(format!("{errors} ERROR event(s)"));
    }
    if !s.terminal_within_timeout {
        r.push("no FINAL or ERROR within 10 s of lk_speech_finish".into());
    }
    r
}

/// One speech trial's network window (check 7).
#[derive(Clone, Debug, Serialize)]
pub struct NetWindow {
    pub trial: usize,
    pub start_ns: u64,
    pub end_ns: u64,
    pub bytes: WindowBytes,
}

/// The end of a trial's network window: 1 s after FINAL, or after the stream closed when no
/// FINAL came.
pub fn net_window_end(t: &AudioTrial) -> u64 {
    t.speech
        .as_ref()
        .and_then(|s| s.final_ns)
        .unwrap_or(t.closed_ns)
        .saturating_add(1_000_000_000)
}

fn flagged_names(windows: &[&NetWindow], groups: &[Group]) -> String {
    let mut names: Vec<String> = windows
        .iter()
        .flat_map(|w| w.bytes.flagged.iter())
        .filter(|f| groups.contains(&f.group))
        .map(|f| f.exe.clone().unwrap_or_else(|| f.name.clone()))
        .collect();
    names.sort();
    names.dedup();
    names.join(", ")
}

/// Check 7 (PLAN.md M1 1a "network silence during DictationTranscriber, checked with a network
/// monitor"; PRD §7.1). For each trial, from key-down to 1 s after FINAL:
/// - any byte from the spike or a speech-recognition process fails;
/// - a window not fully covered by complete nettop samples, or fewer than `want` trials, is
///   incomplete;
/// - any byte from a speech-adjacent or general-purpose daemon makes the run inconclusive
///   (incomplete, never a pass): those daemons move data for many clients, so the Wi-Fi-off run
///   of check 6 decides;
/// - a run that started with no routable interface is the Wi-Fi-off cross-check itself, and is
///   incomplete here (its evidence is the speech result).
pub fn netsilence(
    trials: &[AudioTrial],
    samples: &[Sample],
    self_pid: u32,
    want: usize,
    online_at_start: bool,
) -> (Status, Vec<String>, Vec<NetWindow>) {
    let windows: Vec<NetWindow> = trials
        .iter()
        .map(|t| {
            let end_ns = net_window_end(t);
            NetWindow {
                trial: t.index + 1,
                start_ns: t.key_down_ns,
                end_ns,
                bytes: window_bytes(samples, t.key_down_ns, end_ns, self_pid),
            }
        })
        .collect();
    let failing: Vec<&NetWindow> = windows
        .iter()
        .filter(|w| w.bytes.failing_bytes() > 0)
        .collect();
    let background: Vec<&NetWindow> = windows
        .iter()
        .filter(|w| w.bytes.background_bytes > 0)
        .collect();
    let uncovered = windows.iter().filter(|w| !w.bytes.covered).count();
    let mut reasons = Vec::new();
    if !failing.is_empty() {
        reasons.push(format!(
            "{} trial window(s) with traffic from the spike or a speech-recognition process ({})",
            failing.len(),
            flagged_names(&failing, &[Group::SelfProc, Group::Speech])
        ));
    }
    if uncovered > 0 {
        reasons.push(format!(
            "{uncovered} trial window(s) not fully covered by complete nettop samples"
        ));
    }
    if trials.len() < want {
        reasons.push(format!("only {} of {want} trials ran", trials.len()));
    }
    if !background.is_empty() {
        reasons.push(format!(
            "inconclusive: {} moved bytes during {} trial window(s); these daemons serve many \
             clients, so the Wi-Fi-off run of check 6 decides (README.md, check 7)",
            flagged_names(&background, &[Group::Background]),
            background.len()
        ));
    }
    if !online_at_start {
        reasons.push(
            "offline run (no routable network interface at the start): this is the Wi-Fi-off \
             cross-check, and its evidence is the speech result"
                .into(),
        );
    }
    let status = if !failing.is_empty() {
        Status::Fail
    } else if reasons.is_empty() {
        Status::Pass
    } else {
        Status::Incomplete
    };
    (status, reasons, windows)
}

/// Folds per-trial failures into a check status: fail if any trial failed, incomplete if fewer
/// than `want` trials ran.
pub fn fold(per_trial: &[Vec<String>], want: usize) -> (Status, Vec<String>) {
    let mut reasons = Vec::new();
    for (i, f) in per_trial.iter().enumerate() {
        for why in f {
            reasons.push(format!("trial {}: {why}", i + 1));
        }
    }
    let status = if !reasons.is_empty() {
        Status::Fail
    } else if per_trial.len() < want {
        reasons.push(format!("only {} of {want} trials ran", per_trial.len()));
        Status::Incomplete
    } else {
        Status::Pass
    };
    (status, reasons)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snap(front: i32, active: bool, visible: bool, key: bool) -> FocusSnapshot {
        FocusSnapshot {
            host_ns: 0,
            frontmost_pid: Some(front),
            frontmost_bundle: Some("com.apple.Terminal".into()),
            frontmost_name: Some("Terminal".into()),
            app_active: Some(active),
            panel_visible: Some(visible),
            panel_key: Some(key),
            panel_on_screen: Some(visible),
            panel_level: Some(25),
        }
    }

    fn good_panel_trial() -> PanelTrial {
        PanelTrial {
            index: 0,
            pre: Some(snap(100, false, false, false)),
            shown_ns: 1_000,
            show_call_us: 300.0,
            snaps: ["+0ms", "+50ms", "+250ms", "+1000ms"]
                .iter()
                .map(|l| ((*l).to_string(), snap(100, false, true, false)))
                .collect(),
            key_up_ns: Some(2_000),
            hidden_ns: Some(5_000),
            typed_ns: Some(4_000),
            owner_confirmed: None,
            focus_events: vec![],
        }
    }

    #[test]
    fn a_non_activating_reveal_passes() {
        assert!(panel_trial_failures(&good_panel_trial(), 999).is_empty());
    }

    #[test]
    fn activation_key_status_or_missing_typing_fail() {
        let mut t = good_panel_trial();
        t.snaps[1].1 = snap(999, true, true, false);
        let f = panel_trial_failures(&t, 999);
        assert!(
            f.iter().any(|x| x.contains("frontmost app changed")),
            "{f:?}"
        );
        assert!(f.iter().any(|x| x.contains("became active")), "{f:?}");
        let mut t = good_panel_trial();
        t.snaps[3].1 = snap(100, false, true, true);
        assert!(
            panel_trial_failures(&t, 999)
                .iter()
                .any(|x| x.contains("became key"))
        );
        let mut t = good_panel_trial();
        t.typed_ns = None;
        assert!(!panel_trial_failures(&t, 999).is_empty());
        t.owner_confirmed = Some(true);
        assert!(panel_trial_failures(&t, 999).is_empty());
    }

    #[test]
    fn click_to_key_and_esc_hand_back() {
        let t = FocusTrial {
            index: 0,
            pre: Some(snap(100, false, false, false)),
            shown_ns: 10,
            became_key: Some(snap(100, false, true, true)),
            keys: vec!["m".into(), "ArrowLeft".into()],
            esc_ns: Some(50),
            after_esc: vec![
                ("+50ms".into(), snap(100, false, false, false)),
                ("+250ms".into(), snap(100, false, false, false)),
            ],
            typed_ns: Some(90),
            owner_confirmed: None,
        };
        assert!(focus_trial_failures(&t, 999).is_empty());
        let mut bad = t.clone();
        bad.became_key = Some(snap(999, true, true, true));
        let f = focus_trial_failures(&bad, 999);
        assert!(f.iter().any(|x| x.contains("while key")), "{f:?}");
        let mut late = t;
        late.typed_ns = Some(20); // typed before Esc does not count
        assert!(!focus_trial_failures(&late, 999).is_empty());
    }

    #[test]
    fn wake_after_key_up_fails_and_summaries_split_by_idle_class() {
        let ok = RevealTrial {
            idle_class: "short".into(),
            key_down_ns: 1_000_000,
            key_up_ns: Some(900_000_000),
            wake_ns: Some(41_000_000),
            beat_gap_ms: Some(30_000.0),
            ..Default::default()
        };
        assert!(reveal_trial_failures(&ok).is_empty());
        assert!((ok.wake_ms().expect("wake") - 40.0).abs() < 1e-9);
        let late = RevealTrial {
            wake_ns: Some(950_000_000),
            ..ok.clone()
        };
        assert!(!reveal_trial_failures(&late).is_empty());
        let long = RevealTrial {
            idle_class: "long".into(),
            page_reloaded: true,
            ..ok.clone()
        };
        let s = wake_summaries(&[ok, long]);
        assert_eq!(s.len(), 2);
        assert_eq!(s[0].idle_class, "long");
        assert_eq!(s[0].reloads, 1);
    }

    fn capture(first: Option<u64>, nonzero: u64, errors: u64) -> CaptureSummary {
        CaptureSummary {
            callbacks: u64::from(first.is_some()) * 10,
            frames: 4800,
            frames_per_callback_min: Some(480),
            frames_per_callback_max: Some(480),
            first_callback_host_ns: first,
            last_callback_host_ns: first,
            first_capture_ns: first,
            interval_ms: None,
            capture_to_callback_ms: None,
            rms_dbfs: -40.0,
            peak: 0.1,
            nonzero_samples: nonzero,
            samples: 4800,
            errors,
            last_error: None,
            push: None,
        }
    }

    fn trial(c: CaptureSummary, speech: Option<SpeechOutcome>) -> AudioTrial {
        AudioTrial {
            index: 0,
            key_down_ns: 1_000,
            cmd_received_ns: 1_100,
            playing_ns: Some(20_000),
            open_error: None,
            key_up_ns: 1_000_000,
            stop_received_ns: 1_000_200,
            closed_ns: 1_000_500,
            capture: c,
            speech,
        }
    }

    #[test]
    fn cpal_trials_need_real_signal_before_key_up() {
        assert!(cpal_trial_failures(&trial(capture(Some(30_000), 4000, 0), None)).is_empty());
        let silent = cpal_trial_failures(&trial(capture(Some(30_000), 0, 0), None));
        assert!(
            silent.iter().any(|x| x.contains("Microphone permission")),
            "{silent:?}"
        );
        assert!(!cpal_trial_failures(&trial(capture(None, 0, 0), None)).is_empty());
        assert!(!cpal_trial_failures(&trial(capture(Some(2_000_000), 10, 0), None)).is_empty());
        assert!(!cpal_trial_failures(&trial(capture(Some(30_000), 10, 1), None)).is_empty());
    }

    fn ev_at(kind: EventKind, text_len: usize, host_ns: u64) -> SpeechEvent {
        SpeechEvent {
            kind,
            raw_kind: 0,
            host_ns,
            text_len,
            words: 1,
            audio_start_s: 0.0,
            audio_end_s: 0.0,
            error_code: 0,
        }
    }

    fn ev(kind: EventKind, text_len: usize) -> SpeechEvent {
        // After the finish call in `outcome` (2_000_000) unless placed otherwise.
        ev_at(kind, text_len, 2_500_000)
    }

    /// A clean session: key-up at 1.0 ms, finish called at 2.0 ms, FINAL at 2.5 ms.
    fn outcome() -> SpeechOutcome {
        SpeechOutcome {
            start_error: None,
            finish_rc: Some(0),
            finish_called_ns: Some(2_000_000),
            finish_returned_ns: Some(2_100_000),
            terminal_within_timeout: true,
            events: vec![
                ev_at(EventKind::Ready, 0, 10_000),
                ev_at(EventKind::Partial, 7, 500_000),
                ev(EventKind::Final, 12),
            ],
            final_ns: Some(2_500_000),
        }
    }

    #[test]
    fn speech_needs_a_partial_and_exactly_one_final() {
        let good = outcome();
        let c = capture(Some(30_000), 4000, 0);
        assert!(speech_trial_failures(&trial(c.clone(), Some(good.clone()))).is_empty());
        let mut two_finals = good.clone();
        two_finals.events.push(ev(EventKind::Final, 3));
        assert!(!speech_trial_failures(&trial(c.clone(), Some(two_finals))).is_empty());
        let mut empty_partial = good;
        empty_partial.events = vec![ev(EventKind::Partial, 0), ev(EventKind::Final, 1)];
        assert!(!speech_trial_failures(&trial(c, Some(empty_partial))).is_empty());
    }

    /// The reviewer's case: "exactly one FINAL arrives after lk_speech_finish" counted FINALs
    /// without checking when they came.
    #[test]
    fn a_final_before_lk_speech_finish_fails() {
        let mut early = outcome();
        early.events[2] = ev_at(EventKind::Final, 12, 1_500_000);
        early.final_ns = Some(1_500_000);
        let f = speech_trial_failures(&trial(capture(Some(30_000), 4000, 0), Some(early)));
        assert!(
            f.iter().any(|x| x.contains("before lk_speech_finish")),
            "{f:?}"
        );
        assert_eq!(outcome().finals_before_finish(), 0);
    }

    /// The reviewer's case: key-up to FINAL included the stream teardown; it is now split into
    /// key-up to the finish call and the finish call to FINAL.
    #[test]
    fn release_to_final_is_split_at_the_finish_call() {
        let t = trial(capture(Some(30_000), 4000, 0), Some(outcome()));
        assert_eq!(t.release_to_final_ms(), Some(1.5));
        assert_eq!(t.key_up_to_finish_ms(), Some(1.0));
        assert_eq!(t.finish_to_final_ms(), Some(0.5));
        let no_speech = trial(capture(Some(30_000), 4000, 0), None);
        assert_eq!(no_speech.release_to_final_ms(), None);
    }

    const S: u64 = 1_000_000_000;

    fn net_sample(index: u64, start_s: u64, rows: Vec<(&str, Option<&str>, u32, u64)>) -> Sample {
        Sample {
            index,
            start_host_ns: start_s * S,
            end_host_ns: (start_s + 1) * S,
            rows: rows
                .into_iter()
                .map(
                    |(name, exe, pid, bytes)| m1_spike_common::netwatch::ProcBytes {
                        name: name.into(),
                        pid,
                        bytes_in: bytes,
                        bytes_out: 0,
                        exe: exe.map(str::to_string),
                    },
                )
                .collect(),
            bad_rows: 0,
        }
    }

    /// A speech trial from 100.5 s to FINAL at 102.0 s: its window runs to 103.0 s.
    fn net_trial() -> AudioTrial {
        let mut o = outcome();
        o.final_ns = Some(102 * S);
        o.finish_called_ns = Some(101 * S);
        AudioTrial {
            key_down_ns: 100 * S + S / 2,
            key_up_ns: 101 * S,
            stop_received_ns: 101 * S,
            closed_ns: 101 * S,
            ..trial(capture(Some(100 * S + S / 2 + 1), 4000, 0), Some(o))
        }
    }

    /// Samples 99..105 s (the first is the cumulative baseline); `extra` rows go in the sample
    /// covering 101-102 s.
    fn net_samples(extra: Vec<(&str, Option<&str>, u32, u64)>) -> Vec<Sample> {
        (0..6u64)
            .map(|k| {
                let rows = if k == 2 {
                    extra.clone()
                } else {
                    vec![("apsd", None, 376, 500)]
                };
                net_sample(k, 99 + k, rows)
            })
            .collect()
    }

    #[test]
    fn network_silence_passes_with_only_unnamed_traffic() {
        let (s, reasons, w) = netsilence(&[net_trial()], &net_samples(vec![]), 4242, 1, true);
        assert_eq!(s, Status::Pass, "{reasons:?}");
        assert!(w[0].bytes.covered);
        assert_eq!(w[0].end_ns, 103 * S);
        assert!(w[0].bytes.other_bytes > 0 && w[0].bytes.flagged.is_empty());
    }

    #[test]
    fn traffic_from_the_spike_or_a_recogniser_fails() {
        for row in [
            ("m1-spike", None, 4242, 10),
            ("localspeechreco", Some("localspeechrecognition"), 9001, 10),
            (
                "com.apple.Speec",
                Some("com.apple.SpeechRecognitionCore.brokerd"),
                900,
                10,
            ),
        ] {
            let (s, reasons, _) =
                netsilence(&[net_trial()], &net_samples(vec![row]), 4242, 1, true);
            assert_eq!(s, Status::Fail, "{row:?}: {reasons:?}");
            assert!(
                reasons[0].contains("speech-recognition process"),
                "{reasons:?}"
            );
        }
    }

    /// The reviewer's case: busy general-purpose daemons (nsurlsessiond, triald, asset daemons,
    /// Siri) failed the check; they now make it inconclusive, never a pass.
    #[test]
    fn traffic_from_background_daemons_is_inconclusive_not_a_failure() {
        for row in [
            ("nsurlsessiond", Some("nsurlsessiond"), 300, 2048),
            ("triald", Some("triald"), 301, 10),
            ("mobileassetd", None, 302, 10),
            ("assistantd", Some("assistantd"), 303, 10),
            ("com.apple.Speec", None, 304, 10),
            (
                "com.apple.Speec",
                Some("com.apple.speech.speechsynthesisd"),
                305,
                10,
            ),
        ] {
            let (s, reasons, _) =
                netsilence(&[net_trial()], &net_samples(vec![row]), 4242, 1, true);
            assert_eq!(s, Status::Incomplete, "{row:?}: {reasons:?}");
            assert!(reasons[0].starts_with("inconclusive"), "{reasons:?}");
            assert!(reasons[0].contains("Wi-Fi-off"), "{reasons:?}");
        }
    }

    #[test]
    fn an_uncovered_window_a_short_run_or_an_offline_run_is_incomplete() {
        // Samples stop at 102 s, before the window's end at 103 s.
        let short: Vec<Sample> = net_samples(vec![]).into_iter().take(3).collect();
        let (s, reasons, w) = netsilence(&[net_trial()], &short, 4242, 1, true);
        assert_eq!(s, Status::Incomplete, "{reasons:?}");
        assert!(!w[0].bytes.covered);
        let (s, reasons, _) = netsilence(&[net_trial()], &net_samples(vec![]), 4242, 5, true);
        assert_eq!(s, Status::Incomplete);
        assert!(reasons.iter().any(|r| r.contains("only 1 of 5")));
        let (s, reasons, _) = netsilence(&[net_trial()], &net_samples(vec![]), 4242, 1, false);
        assert_eq!(s, Status::Incomplete);
        assert!(reasons.iter().any(|r| r.contains("offline run")));
        // A failure outranks everything else.
        let (s, _, _) = netsilence(
            &[net_trial()],
            &net_samples(vec![("m1-spike", None, 4242, 1)]),
            4242,
            5,
            false,
        );
        assert_eq!(s, Status::Fail);
    }

    #[test]
    fn fold_reports_incomplete_runs() {
        assert_eq!(fold(&[vec![], vec![]], 2).0, Status::Pass);
        assert_eq!(fold(&[vec![]], 2).0, Status::Incomplete);
        assert_eq!(fold(&[vec!["x".into()]], 1).0, Status::Fail);
    }
}
