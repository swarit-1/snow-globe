//! The eleven PLAN.md M1 1a checks: the PLAN's wording, the criterion each run is judged by, the
//! permission it needs and the binary that runs it. README.md repeats these; the binaries write
//! the `criterion` text below into every result record so a run file stands on its own.

/// One PLAN item.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CheckSpec {
    /// Position in PLAN.md M1 1a (1-11).
    pub n: u8,
    /// Slug used in run ids and result records.
    pub slug: &'static str,
    /// The PLAN.md wording.
    pub plan_item: &'static str,
    /// How the spike decides pass or fail.
    pub criterion: &'static str,
    /// macOS permission the run needs (granted by the owner; the spike never requests one).
    pub permission: &'static str,
    /// Which binary writes the verdict.
    pub verdict_by: &'static str,
}

/// PLAN.md M1 1a: "Stop and report if any check fails."
pub const STOP_RULE: &str = "PLAN.md M1 1a: stop and report if any check fails.";

/// The eleven checks in PLAN order.
pub const CHECKS: [CheckSpec; 11] = [
    CheckSpec {
        n: 1,
        slug: "hotkey",
        plan_item: "hotkey press and release, including when the modifier is released first",
        criterion: "Every hotkey press yields exactly one Pressed and one Released handler call, \
            with no press arriving while the previous one is unreleased and no release without a \
            press, over at least --per-order (default 10) trials observed released key-first and \
            as many observed released modifier-first (each order is asked for until it has been \
            seen that often, at most 3 x --per-order trials).",
        permission: "none",
        verdict_by: "m1-spike hotkey",
    },
    CheckSpec {
        n: 2,
        slug: "panel",
        plan_item: "a non-activating panel over another app that keeps focus",
        criterion: "In every trial the panel is on screen above the other app while that app stays \
            frontmost (same pid at +0, +50, +250 and +1000 ms after the reveal), the spike app is \
            never active, the panel never becomes key, and the text typed during the reveal \
            reaches the other app (the terminal receives it, or the owner confirms it for another app).",
        permission: "none",
        verdict_by: "m1-spike panel",
    },
    CheckSpec {
        n: 3,
        slug: "focus",
        plan_item: "click-to-key focus, with focus returning to the previous text field",
        criterion: "In every trial, clicking the panel makes it the key window while the spike app \
            stays inactive and the other app stays frontmost; the page receives the test keys (M, \
            Left); Esc hides the panel; and the next text typed reaches the previous text field \
            (the terminal receives it, or the owner confirms it for another app).",
        permission: "none",
        verdict_by: "m1-spike focus",
    },
    CheckSpec {
        n: 4,
        slug: "reveal",
        plan_item: "the panel revealed on key-down, with wake-from-suspension timed",
        criterion: "The panel is shown inside the key-down handler, and in every trial the webview \
            reports its first animation frame after the reveal before the key is released (PRD \
            §5.2: wake-up happens during speech). Wake times are reported for each idle state.",
        permission: "none",
        verdict_by: "m1-spike reveal",
    },
    CheckSpec {
        n: 5,
        slug: "cpal",
        plan_item: "cpal capture opened on key-down",
        criterion: "In every trial the default input opens in response to key-down, delivers its \
            first buffer before key-up, carries signal (some non-zero samples), reports no stream \
            error, and is closed on key-up. Time to first buffer and the buffer cadence are reported.",
        permission: "Microphone",
        verdict_by: "m1-spike cpal",
    },
    CheckSpec {
        n: 6,
        slug: "speech",
        plan_item: "the Swift static library (C ABI) returning DictationTranscriber partials",
        criterion: "In every trial sg_speech_start succeeds, at least one PARTIAL with non-empty \
            text arrives, exactly one FINAL arrives and it comes after sg_speech_finish was \
            called, and no ERROR arrives.",
        permission: "Microphone; Speech Recognition if the library asks for it; the en-US \
            dictation asset installed",
        verdict_by: "m1-spike speech (built with --features speech)",
    },
    CheckSpec {
        n: 7,
        slug: "netsilence",
        plan_item: "network silence during DictationTranscriber, checked with a network monitor",
        criterion: "For every speech trial, from key-down to 1 s after FINAL, complete nettop \
            samples cover the whole window and report zero bytes in or out for the spike process \
            and every speech-recognition process (any byte fails). Bytes from a speech-adjacent \
            or general-purpose daemon (Siri, assistant, dictation, speech synthesis, asset, \
            triald, nsurlsessiond) make the run inconclusive, never a pass; a Wi-Fi-off run of \
            check 6 then decides.",
        permission: "none (nettop)",
        verdict_by: "m1-spike speech",
    },
    CheckSpec {
        n: 8,
        slug: "sck-tag",
        plan_item: "a ScreenCaptureKit window-only capture with an in-surface pixel tag",
        criterion: "ScreenCaptureKit captures only the panel window (a desktop-independent \
            window filter, and the tag decodes at the frame's top-left corner, where a display \
            capture would not have it), at least 99 % of complete frames after the first tagged \
            one carry a valid pixel tag, and every decoded tag is one the app drew \
            (sck-tag-join, written by m1-report). contentRect against the window is reported.",
        permission: "Screen Recording (for the terminal or the harness app)",
        verdict_by: "m1-sck tag, then m1-report --write",
    },
    CheckSpec {
        n: 9,
        slug: "inject",
        plan_item: "CGEventPost hotkey injection",
        criterion: "Every key press and release posted with CGEventPost at the HID tap produces \
            exactly one Pressed and one Released in the app's global-shortcut handler, in order, \
            within 1 s of the trial's last post, and no handler call falls outside every trial; \
            post-to-handler latency is reported (from the clock read before each post).",
        permission: "Accessibility for the injecting process (Input Monitoring too with --tap)",
        verdict_by: "m1-inject + m1-spike hotkey --injected, then m1-report --write",
    },
    CheckSpec {
        n: 10,
        slug: "clock",
        plan_item: "host-clock agreement between ScreenCaptureKit timestamps and Instant",
        criterion: "Instant is the host clock (its value matches CLOCK_UPTIME_RAW, and it drifts \
            from it by no more than the read bracket + 1 µs over the run), and frame \
            presentation times are on that clock: no complete frame arrives more than 1 ms \
            before its presentation time, the median presentation-to-arrival lag (Instant mapped \
            to the host clock) is 0-250 ms, and arrival against presentation time has a slope \
            of 1 ± 0.01 (median of pairs). Frames later than 250 ms are counted, not failed.",
        permission: "Screen Recording",
        verdict_by: "m1-sck tag",
    },
    CheckSpec {
        n: 11,
        slug: "consent",
        plan_item: "whether Screen Recording re-consent prompts appear",
        criterion: "A harness binary that already has Screen Recording, run again unchanged (same \
            code-directory hash), starts capturing within 5 s and the owner sees no Screen \
            Recording prompt or re-consent alert. Runs after a rebuild or under a new identity are \
            recorded separately and are not failures by themselves.",
        permission: "Screen Recording",
        verdict_by: "m1-sck consent",
    },
];

/// Looks up a check by slug.
pub fn spec(slug: &str) -> Option<&'static CheckSpec> {
    CHECKS.iter().find(|c| c.slug == slug)
}

/// The criterion text for a slug (empty when unknown).
pub fn criterion(slug: &str) -> &'static str {
    spec(slug).map(|c| c.criterion).unwrap_or("")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn eleven_checks_in_plan_order_with_unique_slugs() {
        assert_eq!(CHECKS.len(), 11);
        for (i, c) in CHECKS.iter().enumerate() {
            assert_eq!(usize::from(c.n), i + 1);
            assert!(!c.criterion.is_empty() && !c.plan_item.is_empty());
        }
        let mut slugs: Vec<_> = CHECKS.iter().map(|c| c.slug).collect();
        slugs.sort_unstable();
        slugs.dedup();
        assert_eq!(slugs.len(), 11);
        assert_eq!(spec("clock").map(|c| c.n), Some(10));
        assert!(criterion("nope").is_empty());
    }

    #[test]
    fn plan_wording_matches_plan_md() {
        let plan = std::fs::read_to_string(crate::paths::repo_root().join("PLAN.md"))
            .expect("PLAN.md at the repo root");
        let squash = |s: &str| {
            s.replace('`', "")
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
        };
        let plan = squash(&plan);
        for c in CHECKS {
            assert!(
                plan.contains(&squash(c.plan_item)),
                "PLAN.md lacks {:?}",
                c.plan_item
            );
        }
    }
}
