//! Command line of `m1-spike` (PLAN.md M1 1a checks 1-7; README.md has the steps per check).

use m1_spike_common::accel::{self, Accel};
use m1_spike_common::args::Args;
use m1_spike_common::jsonl::new_run_id;
use m1_spike_common::paths::validate_run_id;

pub const USAGE: &str = "usage: m1-spike <check> [options]

checks (PLAN.md M1 1a):
  hotkey   1  press/release, including modifier released first   (no permission)
  panel    2  non-activating panel over another app               (no permission)
  focus    3  click-to-key focus, Esc hands focus back            (no permission)
  reveal   4  panel revealed on key-down, wake-up timed           (no permission)
  cpal     5  microphone opened on key-down                       (Microphone)
  speech   6+7 DictationTranscriber partials + network silence    (Microphone; needs --features speech)
  tags     8,10,11 helper: shows the panel and cycles pixel tags for m1-sck (no permission)

options:
  --run-id ID            run file name under $SNOW_GLOBE_DATA_DIR/spikes/m1/ (default: generated)
  --hotkey ACCEL         global shortcut (default Control+Shift+Space)
  --trials N             trials (hotkey --injected: 20; panel, focus: 3; reveal, cpal, speech: 5)
  --per-order N          hotkey: releases to observe per order (default 10; at most 3N trials)
  --injected             hotkey: no prompts; for m1-inject (check 9)
  --seconds N            hotkey --injected timeout (180) / tags duration (60)
  --idle-secs N          reveal: hidden time before a short-idle trial (30)
  --long-trials N        reveal: trials after a long idle (2)
  --long-idle-secs N     reveal: hidden time before a long-idle trial (360)
  --throttling P         webview background policy: default|suspend|throttle|disabled
  --level L              panel window level: floating|status|popup (default status)
  --hold-after-ms N      panel: keep the panel up after key-up (4000)
  --period-ms N          tags: time between tag changes (250)
  --locale L             speech: BCP 47 locale (en-US)
  --print-text           speech: print partial text to this terminal (never stored)
  --no-netwatch          speech: skip the nettop monitor (check 7 is then skipped)
  --key-state            hotkey: also poll the main key with CGEventSourceKeyState
  --help";

/// Which check this process runs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CheckKind {
    Hotkey,
    Panel,
    Focus,
    Reveal,
    Cpal,
    Speech,
    Tags,
}

impl CheckKind {
    pub fn from_slug(s: &str) -> Option<Self> {
        Some(match s {
            "hotkey" => Self::Hotkey,
            "panel" => Self::Panel,
            "focus" => Self::Focus,
            "reveal" => Self::Reveal,
            "cpal" => Self::Cpal,
            "speech" => Self::Speech,
            "tags" => Self::Tags,
            _ => return None,
        })
    }

    pub fn slug(self) -> &'static str {
        match self {
            Self::Hotkey => "hotkey",
            Self::Panel => "panel",
            Self::Focus => "focus",
            Self::Reveal => "reveal",
            Self::Cpal => "cpal",
            Self::Speech => "speech",
            Self::Tags => "tags",
        }
    }
}

/// Parsed options.
#[derive(Clone, Debug)]
pub struct Config {
    pub check: CheckKind,
    pub run_id: String,
    pub hotkey: String,
    pub accel: Accel,
    pub trials: usize,
    pub per_order: usize,
    pub injected: bool,
    pub seconds: u64,
    pub idle_secs: u64,
    pub long_trials: usize,
    pub long_idle_secs: u64,
    pub throttling: String,
    pub level: String,
    pub hold_after_ms: u64,
    pub period_ms: u64,
    pub locale: String,
    pub print_text: bool,
    pub netwatch: bool,
    pub key_state: bool,
}

impl Config {
    /// Parses argv (without the program name). `Ok(None)` means `--help`.
    pub fn parse(argv: &[String]) -> Result<Option<Self>, String> {
        let a = Args::parse(
            argv.iter().cloned(),
            &["injected", "print-text", "no-netwatch", "key-state", "help"],
        )?;
        if a.flag("help") || a.command.is_none() {
            return Ok(None);
        }
        let slug = a.command.clone().unwrap_or_default();
        let check = CheckKind::from_slug(&slug).ok_or_else(|| format!("unknown check {slug:?}"))?;
        if !a.positional.is_empty() {
            return Err(format!("unexpected arguments {:?}", a.positional));
        }
        let injected = a.flag("injected");
        let default_trials = match check {
            CheckKind::Hotkey => 20,
            CheckKind::Panel | CheckKind::Focus => 3,
            CheckKind::Reveal | CheckKind::Cpal | CheckKind::Speech => 5,
            CheckKind::Tags => 0,
        };
        let default_seconds = if check == CheckKind::Tags { 60 } else { 180 };
        let run_id = match a.get("run-id") {
            Some(id) => id.to_string(),
            None => new_run_id(check.slug()),
        };
        validate_run_id(&run_id)?;
        let hotkey = a.get("hotkey").unwrap_or("Control+Shift+Space").to_string();
        let accel = accel::parse(&hotkey)?;
        if accel.mods.is_option_only() {
            return Err(format!(
                "{hotkey}: macOS 15+ rejects shortcuts whose only modifiers are Option or Option+Shift (PRD §5.4)"
            ));
        }
        if !accel.mods.any() {
            return Err(format!(
                "{hotkey}: the hold-to-talk hotkey needs a modifier"
            ));
        }
        let cfg = Self {
            check,
            run_id,
            hotkey,
            accel,
            trials: a.get_or("trials", default_trials)?,
            per_order: a.get_or("per-order", 10usize)?,
            injected,
            seconds: a.get_or("seconds", default_seconds)?,
            idle_secs: a.get_or("idle-secs", 30u64)?,
            long_trials: a.get_or("long-trials", 2usize)?,
            long_idle_secs: a.get_or("long-idle-secs", 360u64)?,
            throttling: a.get("throttling").unwrap_or("default").to_string(),
            level: a.get("level").unwrap_or("status").to_string(),
            hold_after_ms: a.get_or("hold-after-ms", 4000u64)?,
            period_ms: a.get_or("period-ms", 250u64)?.max(20),
            locale: a.get("locale").unwrap_or("en-US").to_string(),
            print_text: a.flag("print-text"),
            netwatch: !a.flag("no-netwatch"),
            key_state: a.flag("key-state"),
        };
        crate::panel::level_from(&cfg.level)?;
        crate::panel::throttling_from(&cfg.throttling)?;
        a.finish()?;
        Ok(Some(cfg))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(s: &str) -> Result<Option<Config>, String> {
        Config::parse(&s.split_whitespace().map(str::to_string).collect::<Vec<_>>())
    }

    #[test]
    fn defaults_per_check() {
        let c = parse("reveal --run-id r1").expect("ok").expect("cfg");
        assert_eq!(
            (c.check, c.trials, c.long_trials, c.idle_secs),
            (CheckKind::Reveal, 5, 2, 30)
        );
        assert_eq!(c.accel.key_code, 0x31);
        let t = parse("tags").expect("ok").expect("cfg");
        assert_eq!((t.seconds, t.period_ms), (60, 250));
        assert!(t.run_id.contains("-tags-"));
        let h = parse("hotkey --injected --trials 4")
            .expect("ok")
            .expect("cfg");
        assert!(h.injected && h.trials == 4 && h.seconds == 180);
    }

    #[test]
    fn rejects_bad_input() {
        assert!(parse("nope").is_err());
        assert!(parse("cpal --trails 3").is_err());
        assert!(parse("hotkey --hotkey Option+Space").is_err());
        assert!(parse("hotkey --hotkey Space").is_err());
        assert!(parse("panel --level top").is_err());
        assert!(parse("panel --run-id ../x").is_err());
        assert!(parse("speech extra").is_err());
        assert!(parse("--help").expect("ok").is_none());
    }
}
