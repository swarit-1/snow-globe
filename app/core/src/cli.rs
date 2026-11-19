//! `lookout-parse`: print the query for an utterance (PRD §7.3), for debugging and scripts.
//!
//! ```text
//! lookout-parse "<utterance>" [--now <RFC3339>] [--machine-tz <IANA>] [--context <place or viewpoint id>]
//!               [--manifests <dir>] [--pretty]
//! ```
//!
//! - `--now` is an RFC 3339 instant ("2026-11-19T10:00:00-06:00"). A wall time without an offset
//!   ("2026-11-19T10:00") is read in `--machine-tz`. Default: the system clock.
//! - `--machine-tz` is the asker's zone. It only fixes "now" as an instant: every time in the
//!   output is resolved in the *place's* zone (PRD §7.4).
//! - `--context` is the previous query's place, for follow-ups ("what about midnight?").
//! - `--manifests` loads manifests from a directory instead of the ones compiled in.
//!
//! Prints the query JSON and exits 0, or prints `{"error": ...}` and exits 2. Usage errors exit 64.

use std::path::Path;

use chrono::{DateTime, NaiveDateTime, Utc};
use chrono_tz::Tz;

use crate::parser::{LastPlace, ParseContext, Parser};
use crate::places::PlaceTable;
use crate::resolve::TimeConfig;
use crate::tzutil::localize;

/// Exit code for a parse error (no match, out of range, ...).
pub const EXIT_PARSE_ERROR: i32 = 2;
/// Exit code for bad arguments (BSD `EX_USAGE`).
pub const EXIT_USAGE: i32 = 64;

/// The usage text.
pub const USAGE: &str = "usage: lookout-parse \"<utterance>\" [--now <RFC3339>] [--machine-tz <IANA>] \
[--context <place_id|viewpoint_id>] [--manifests <dir>] [--pretty]";

/// What a CLI run produced.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliOutput {
    /// Process exit code.
    pub code: i32,
    /// Text for standard output.
    pub stdout: String,
    /// Text for standard error.
    pub stderr: String,
}

fn usage(msg: &str) -> CliOutput {
    CliOutput {
        code: EXIT_USAGE,
        stdout: String::new(),
        stderr: format!("lookout-parse: {msg}\n{USAGE}\n"),
    }
}

/// Turn `--now` into an instant: RFC 3339, or a wall time in the machine zone.
pub fn parse_now_arg(text: &str, machine_tz: Option<Tz>) -> Result<DateTime<Utc>, String> {
    if let Ok(t) = DateTime::parse_from_rfc3339(text) {
        return Ok(t.with_timezone(&Utc));
    }
    for fmt in [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ] {
        if let Ok(naive) = NaiveDateTime::parse_from_str(text, fmt) {
            let tz = machine_tz
                .ok_or_else(|| format!("--now {text:?} has no offset; give --machine-tz"))?;
            return Ok(localize(tz, naive).0.with_timezone(&Utc));
        }
    }
    Err(format!("--now {text:?} is not an RFC 3339 time"))
}

/// Run the CLI on `args` (without the program name).
pub fn run(args: &[String]) -> CliOutput {
    let mut utterance: Option<&str> = None;
    let mut now_arg: Option<&str> = None;
    let mut tz_arg: Option<&str> = None;
    let mut context: Option<&str> = None;
    let mut manifests: Option<&str> = None;
    let mut pretty = false;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let mut value = |name: &str| {
            it.next()
                .map(String::as_str)
                .ok_or_else(|| format!("{name} needs a value"))
        };
        let res = match a.as_str() {
            "--now" => value("--now").map(|v| now_arg = Some(v)),
            "--machine-tz" => value("--machine-tz").map(|v| tz_arg = Some(v)),
            "--context" => value("--context").map(|v| context = Some(v)),
            "--manifests" => value("--manifests").map(|v| manifests = Some(v)),
            "--pretty" => {
                pretty = true;
                Ok(())
            }
            "-h" | "--help" => {
                return CliOutput {
                    code: 0,
                    stdout: format!("{USAGE}\n"),
                    stderr: String::new(),
                };
            }
            s if s.starts_with("--") => Err(format!("unknown option {s}")),
            s => match utterance {
                None => {
                    utterance = Some(s);
                    Ok(())
                }
                Some(_) => Err("give the utterance as one quoted argument".to_string()),
            },
        };
        if let Err(msg) = res {
            return usage(&msg);
        }
    }
    let Some(utterance) = utterance else {
        return usage("missing the utterance");
    };
    let machine_tz = match tz_arg.map(str::parse::<Tz>) {
        None => None,
        Some(Ok(tz)) => Some(tz),
        Some(Err(_)) => {
            return usage(&format!(
                "unknown IANA zone {:?}",
                tz_arg.unwrap_or_default()
            ));
        }
    };
    let now = match now_arg {
        None => Utc::now(),
        Some(text) => match parse_now_arg(text, machine_tz) {
            Ok(t) => t,
            Err(msg) => return usage(&msg),
        },
    };
    let owned;
    let parser: &Parser = match manifests {
        None => Parser::embedded(),
        Some(dir) => match PlaceTable::from_dir(Path::new(dir)) {
            Ok(table) => {
                owned = Parser::new(table, TimeConfig::default());
                &owned
            }
            Err(e) => return usage(&e.to_string()),
        },
    };
    let last = match context {
        None => None,
        Some(id) => {
            let table = parser.places();
            if let Some((_, v)) = table.viewpoint(id) {
                Some(LastPlace {
                    place_id: v.place_id.clone(),
                    viewpoint_id: v.id.clone(),
                })
            } else if let Some((_, p)) = table.place(id) {
                Some(LastPlace {
                    place_id: p.id.clone(),
                    viewpoint_id: table.viewpoints()[p.default_viewpoint].id.clone(),
                })
            } else {
                return usage(&format!(
                    "--context {id:?} is not a known place or viewpoint"
                ));
            }
        }
    };
    let ctx = ParseContext {
        now,
        last: last.as_ref(),
    };
    match parser.parse(utterance, &ctx) {
        Ok(q) => {
            let json = if pretty {
                serde_json::to_string_pretty(&q)
            } else {
                serde_json::to_string(&q)
            };
            CliOutput {
                code: 0,
                stdout: format!("{}\n", json.unwrap_or_default()),
                stderr: String::new(),
            }
        }
        Err(e) => {
            let json = if pretty {
                serde_json::to_string_pretty(&e)
            } else {
                serde_json::to_string(&e)
            };
            CliOutput {
                code: EXIT_PARSE_ERROR,
                stdout: format!("{}\n", json.unwrap_or_default()),
                stderr: format!("lookout-parse: {e}\n"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn now_forms() {
        let chicago: Tz = "America/Chicago".parse().expect("zone");
        let a = parse_now_arg("2026-11-19T10:00:00-06:00", None).expect("rfc3339");
        let b = parse_now_arg("2026-11-19T10:00", Some(chicago)).expect("wall time");
        assert_eq!(a, b);
        assert!(parse_now_arg("2026-11-19T10:00", None).is_err());
        assert!(parse_now_arg("tomorrow", Some(chicago)).is_err());
    }

    #[test]
    fn runs_and_reports_errors() {
        let ok = run(&args(&[
            "Is Rainier out tomorrow at 4?",
            "--now",
            "2026-11-19T12:00:00-06:00",
            "--machine-tz",
            "America/Chicago",
        ]));
        assert_eq!(ok.code, 0, "{ok:?}");
        assert!(
            ok.stdout
                .contains(r#""valid_time":"2026-11-20T16:00:00-08:00""#),
            "{}",
            ok.stdout
        );
        let miss = run(&args(&[
            "Golden Gate at 9",
            "--now",
            "2026-11-19T12:00:00-06:00",
        ]));
        assert_eq!(miss.code, EXIT_PARSE_ERROR);
        assert!(
            miss.stdout.starts_with(r#"{"error":"no_match""#),
            "{}",
            miss.stdout
        );
        assert_eq!(run(&args(&["--now"])).code, EXIT_USAGE);
        assert_eq!(
            run(&args(&["x", "--machine-tz", "Mars/Base"])).code,
            EXIT_USAGE
        );
        let follow = run(&args(&[
            "what about midnight",
            "--now",
            "2026-11-19T12:00:00-06:00",
            "--context",
            "point-reyes",
        ]));
        assert!(
            follow.stdout.contains(r#""place_source":"context""#),
            "{}",
            follow.stdout
        );
    }
}
