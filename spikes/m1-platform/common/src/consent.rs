//! Check 11's verdict (PLAN.md M1 1a "whether Screen Recording re-consent prompts appear"; PRD
//! §18.1 risk "A Screen Recording re-consent prompt stalls an overnight run").
//!
//! `m1-sck consent` measures one capture start; the owner says whether a prompt appeared, either
//! at the terminal or later with `m1-report note <run> consent prompt_seen=<true|false>` (needed
//! when the helper runs as a bundled app with no terminal). [`from_records`] recombines the two.

use serde_json::{Value, json};

use crate::jsonl::{Kind, Record, Status};

/// Scenarios where a prompt is expected; such runs are recorded but are not pass/fail.
pub const INFORMATIONAL_SCENARIOS: [&str; 3] = ["first-run", "after-rebuild", "new-identity"];

/// Longest acceptable wait for shareable content or the first frame, in ms.
pub const STALL_MS: f64 = 5_000.0;

/// The owner's yes/no answer, from a JSON boolean or one of `yes`, `y`, `true`, `no`, `n`,
/// `false` (any case). The same spellings are accepted by `m1-sck --prompt-seen` and by
/// `m1-report note ... prompt_seen=...`.
pub fn parse_yes_no(v: &Value) -> Option<bool> {
    match v {
        Value::Bool(b) => Some(*b),
        Value::String(s) => match s.trim().to_ascii_lowercase().as_str() {
            "yes" | "y" | "true" => Some(true),
            "no" | "n" | "false" => Some(false),
            _ => None,
        },
        _ => None,
    }
}

/// Check 11 for one run.
pub fn consent(
    scenario: &str,
    preflight_before: bool,
    content_ms: Option<f64>,
    first_frame_ms: Option<f64>,
    prompt_seen: Option<bool>,
) -> (Status, Vec<String>) {
    let mut reasons = Vec::new();
    if prompt_seen == Some(true) {
        reasons.push("the owner saw a Screen Recording prompt or re-consent alert".into());
    }
    match content_ms {
        None => reasons.push("SCShareableContent failed".into()),
        Some(ms) if ms > STALL_MS => reasons.push(format!(
            "SCShareableContent took {ms:.0} ms (a prompt was probably waiting)"
        )),
        _ => {}
    }
    match first_frame_ms {
        None => reasons.push("no captured frame".into()),
        Some(ms) if ms > STALL_MS => reasons.push(format!("first frame after {ms:.0} ms")),
        _ => {}
    }
    if !preflight_before {
        reasons.push("CGPreflightScreenCaptureAccess was false before the run".into());
    }
    if INFORMATIONAL_SCENARIOS.contains(&scenario) {
        return (Status::Incomplete, reasons);
    }
    if prompt_seen.is_none() {
        reasons.push("the owner did not say whether a prompt appeared".into());
        return (Status::Incomplete, reasons);
    }
    (Status::from_bool(reasons.is_empty()), reasons)
}

/// Recomputes check 11 from the last `consent_measure` event and the last `consent` note that
/// carries `prompt_seen`. `None` unless both exist.
pub fn from_records(recs: &[Record]) -> Option<(Status, Value)> {
    let m = recs
        .iter()
        .rev()
        .find(|r| r.kind == Kind::Event && r.name == "consent_measure")?;
    let note = recs.iter().rev().find(|r| {
        r.kind == Kind::Note && r.name == "consent" && r.v.get("prompt_seen").is_some()
    })?;
    let raw = note.v.get("prompt_seen");
    let prompt_seen = raw.and_then(parse_yes_no);
    let scenario = note
        .v
        .get("scenario")
        .and_then(Value::as_str)
        .or_else(|| m.v.get("scenario").and_then(Value::as_str))
        .unwrap_or("repeat")
        .to_string();
    let pre =
        m.v.get("preflight_before")
            .and_then(Value::as_bool)
            .unwrap_or(false);
    let content_ms = m.v.get("shareable_content_ms").and_then(Value::as_f64);
    let first_ms = m.v.get("first_complete_frame_ms").and_then(Value::as_f64);
    let window_found =
        m.v.get("window_found")
            .and_then(Value::as_bool)
            .unwrap_or(false);
    let (mut status, mut reasons) = consent(&scenario, pre, content_ms, first_ms, prompt_seen);
    if let Some(r) = raw.filter(|_| prompt_seen.is_none()) {
        reasons.push(format!("the note's prompt_seen={r} is not yes or no"));
    }
    if !window_found {
        reasons.push("no spike panel window was captured; start `m1-spike tags` first".into());
        status = Status::Incomplete;
    }
    Some((
        status,
        json!({"scenario": scenario, "prompt_seen": prompt_seen, "measure": m.v, "reasons": reasons,
               "informational": INFORMATIONAL_SCENARIOS.contains(&scenario.as_str())}),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jsonl::{RunLog, read_run};

    #[test]
    fn repeat_runs_fail_on_a_prompt_or_a_stall_and_first_runs_are_informational() {
        assert_eq!(
            consent("repeat", true, Some(40.0), Some(60.0), Some(false)).0,
            Status::Pass
        );
        assert_eq!(
            consent("repeat", true, Some(40.0), Some(60.0), Some(true)).0,
            Status::Fail
        );
        assert_eq!(
            consent(
                "after-reboot",
                true,
                Some(9_000.0),
                Some(9_100.0),
                Some(false)
            )
            .0,
            Status::Fail
        );
        assert_eq!(
            consent("first-run", false, Some(8_000.0), Some(8_100.0), Some(true)).0,
            Status::Incomplete
        );
        assert_eq!(
            consent("repeat", true, Some(40.0), Some(60.0), None).0,
            Status::Incomplete
        );
    }

    #[test]
    fn a_later_note_completes_a_bundled_run() {
        let dir = tempfile::tempdir().expect("tempdir");
        let p = dir.path().join("c.jsonl");
        let sck = RunLog::open_at(&p, "c", "consent", "m1-sck", &[]).expect("open");
        sck.event(
            "consent_measure",
            json!({"scenario": "repeat", "preflight_before": true, "shareable_content_ms": 35.0,
                   "first_complete_frame_ms": 70.0, "window_found": true}),
        )
        .expect("w");
        let (recs, _) = read_run(&p).expect("read");
        assert!(from_records(&recs).is_none());
        let rep = RunLog::open_at(&p, "c", "consent", "m1-report", &[]).expect("open");
        rep.note(
            "consent",
            json!({"prompt_seen": false, "scenario": "after-reboot"}),
        )
        .expect("w");
        let (recs, _) = read_run(&p).expect("read");
        let (s, v) = from_records(&recs).expect("join");
        assert_eq!(s, Status::Pass, "{v}");
        assert_eq!(v["scenario"], "after-reboot");
    }

    /// The reviewer's case: `prompt_seen=no` was stored as a string and read back as "the owner
    /// did not say". Every spelling `m1-sck --prompt-seen` accepts now counts.
    #[test]
    fn notes_accept_the_same_yes_no_spellings_as_the_helper() {
        for (word, want) in [
            ("no", false),
            ("n", false),
            ("No", false),
            ("false", false),
            ("yes", true),
            ("Y", true),
            ("true", true),
        ] {
            assert_eq!(parse_yes_no(&json!(word)), Some(want), "{word}");
        }
        assert_eq!(parse_yes_no(&json!(false)), Some(false));
        assert_eq!(parse_yes_no(&json!("maybe")), None);
        assert_eq!(parse_yes_no(&json!(0)), None);
        let dir = tempfile::tempdir().expect("tempdir");
        let p = dir.path().join("n.jsonl");
        let sck = RunLog::open_at(&p, "n", "consent", "m1-sck", &[]).expect("open");
        sck.event(
            "consent_measure",
            json!({"scenario": "repeat", "preflight_before": true, "shareable_content_ms": 35.0,
                   "first_complete_frame_ms": 70.0, "window_found": true}),
        )
        .expect("w");
        sck.note("consent", json!({"prompt_seen": "no"}))
            .expect("w");
        let (recs, _) = read_run(&p).expect("read");
        let (s, v) = from_records(&recs).expect("join");
        assert_eq!(s, Status::Pass, "{v}");
        sck.note("consent", json!({"prompt_seen": "maybe"}))
            .expect("w");
        let (recs, _) = read_run(&p).expect("read");
        let (s, v) = from_records(&recs).expect("join");
        assert_eq!(s, Status::Incomplete, "{v}");
        assert!(v["reasons"].to_string().contains("not yes or no"), "{v}");
    }
}
