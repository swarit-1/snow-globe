//! The offline report over a run log, and the two verdicts that need records from two processes
//! (PLAN.md M1 1a checks 8 and 9; PRD §10.4):
//! - [`inject_join`]: `m1-inject`'s posted events against the app's handler calls (check 9);
//! - [`tag_join`]: the app's tag requests against the tags `m1-sck` decoded from captured frames
//!   (the second half of check 8, plus request-to-composited-frame paint times).

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

use serde::Serialize;
use serde_json::{Value, json};

use crate::checks::{CHECKS, STOP_RULE};
use crate::jsonl::{Kind, Record, Status};
use crate::stats::{Summary, summarize};
use crate::tag::Tag;

/// `v[key]` as u64.
pub fn u64_at(v: &Value, key: &str) -> Option<u64> {
    v.get(key).and_then(Value::as_u64)
}

/// `v[key]` as i64.
pub fn i64_at(v: &Value, key: &str) -> Option<i64> {
    v.get(key).and_then(Value::as_i64)
}

/// `v[key]` as &str.
pub fn str_at<'a>(v: &'a Value, key: &str) -> Option<&'a str> {
    v.get(key).and_then(Value::as_str)
}

/// The last result record per slug (results are keyed by `name`).
pub fn latest_results(recs: &[Record]) -> BTreeMap<String, Record> {
    let mut out = BTreeMap::new();
    for r in recs.iter().filter(|r| r.kind == Kind::Result) {
        out.insert(r.name.clone(), r.clone());
    }
    out
}

// ---------------------------------------------------------------------------------------------
// Check 9: injected events against handler calls.
// ---------------------------------------------------------------------------------------------

/// One `CGEventPost` call logged by `m1-inject` (`posted` events).
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Post {
    pub trial: u64,
    /// `mod_down`, `key_down`, `key_up` or `mod_up`.
    pub step: String,
    pub before_ns: u64,
    pub after_ns: u64,
}

/// One global-shortcut handler call logged by the app (`hotkey_edge` events).
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct AppEdge {
    pub pressed: bool,
    pub host_ns: u64,
}

/// Extracts posts from a run.
pub fn posts(recs: &[Record]) -> Vec<Post> {
    recs.iter()
        .filter(|r| r.kind == Kind::Event && r.name == "posted")
        .filter_map(|r| {
            Some(Post {
                trial: u64_at(&r.v, "trial")?,
                step: str_at(&r.v, "step")?.to_string(),
                before_ns: u64_at(&r.v, "host_before_ns")?,
                after_ns: u64_at(&r.v, "host_after_ns")?,
            })
        })
        .collect()
}

/// Extracts the app's handler calls for the main hotkey from a run.
pub fn app_edges(recs: &[Record]) -> Vec<AppEdge> {
    recs.iter()
        .filter(|r| r.kind == Kind::Event && r.name == "hotkey_edge")
        .filter_map(|r| {
            Some(AppEdge {
                pressed: str_at(&r.v, "edge")? == "pressed",
                host_ns: u64_at(&r.v, "host_ns").unwrap_or(r.t_host_ns),
            })
        })
        .collect()
}

/// One injected trial after the join.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct InjectTrial {
    pub trial: u64,
    pub pressed: usize,
    pub released: usize,
    /// Handler entry minus the host clock read just before the `key_down` post. The post
    /// happened somewhere inside its bracket, so this is an upper bound on post-to-handler
    /// latency; subtract `press_post_bracket_ms` for the lower bound.
    pub press_latency_ms: Option<f64>,
    /// The `key_down` post's bracket (host clock after minus before the `CGEventPost` call).
    pub press_post_bracket_ms: Option<f64>,
    /// As `press_latency_ms`, from the `key_up` or `mod_up` post the release followed.
    pub release_latency_ms: Option<f64>,
    pub release_post_bracket_ms: Option<f64>,
    /// Which post the release followed (`key_up` or `mod_up`).
    pub release_after: Option<String>,
    pub ok: bool,
}

/// Check 9's verdict.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct InjectJoin {
    pub status: Status,
    pub trials: Vec<InjectTrial>,
    /// Handler calls outside every trial's window: before the first post, between trials, or
    /// more than [`INJECT_TAIL_MS`] after a trial's last post.
    pub stray_edges: usize,
    pub press_latency_ms: Option<Summary>,
    pub release_latency_ms: Option<Summary>,
    /// The widest post bracket in the run (the latencies' own uncertainty).
    pub max_post_bracket_ms: Option<f64>,
    pub failures: Vec<String>,
}

/// A trial's handler calls must arrive within this long of its last post (the injector waits
/// `--gap-ms`, 1200 ms by default, before the next trial).
pub const INJECT_TAIL_MS: u64 = 1_000;

/// Joins posts and handler calls by time (PLAN.md M1 1a check 9). Each trial's window runs from
/// its first post to [`INJECT_TAIL_MS`] after its last post, or to the next trial's first post
/// if that comes sooner; handler calls outside every window are stray. `None` when the run has
/// no posts.
pub fn inject_join(posts: &[Post], edges: &[AppEdge]) -> Option<InjectJoin> {
    let mut by_trial: BTreeMap<u64, Vec<&Post>> = BTreeMap::new();
    for p in posts {
        by_trial.entry(p.trial).or_default().push(p);
    }
    if by_trial.is_empty() {
        return None;
    }
    // (trial, first post's before, last post's after)
    let spans: Vec<(u64, u64, u64)> = by_trial
        .iter()
        .map(|(t, ps)| {
            let first = ps.iter().map(|p| p.before_ns).min().unwrap_or(0);
            let last = ps.iter().map(|p| p.after_ns).max().unwrap_or(first);
            (*t, first, last)
        })
        .collect();
    let mut trials = Vec::new();
    let mut used = BTreeSet::new();
    let mut failures = Vec::new();
    let mut max_bracket_ns = 0u64;
    let bracket_ms = |p: &Post| p.after_ns.saturating_sub(p.before_ns) as f64 / 1e6;
    for (k, (trial, start, last)) in spans.iter().enumerate() {
        let tail_end = last.saturating_add(INJECT_TAIL_MS * 1_000_000);
        let end = spans.get(k + 1).map_or(tail_end, |s| s.1.min(tail_end));
        let ps = &by_trial[trial];
        for p in ps {
            max_bracket_ns = max_bracket_ns.max(p.after_ns.saturating_sub(p.before_ns));
        }
        let in_window: Vec<(usize, &AppEdge)> = edges
            .iter()
            .enumerate()
            .filter(|(_, e)| e.host_ns >= *start && e.host_ns < end)
            .collect();
        for (i, _) in &in_window {
            used.insert(*i);
        }
        let pressed: Vec<&AppEdge> = in_window
            .iter()
            .filter(|(_, e)| e.pressed)
            .map(|(_, e)| *e)
            .collect();
        let released: Vec<&AppEdge> = in_window
            .iter()
            .filter(|(_, e)| !e.pressed)
            .map(|(_, e)| *e)
            .collect();
        let key_down = ps.iter().find(|p| p.step == "key_down");
        let press_latency_ms = match (key_down, pressed.first()) {
            (Some(kd), Some(e)) => Some((e.host_ns as f64 - kd.before_ns as f64) / 1e6),
            _ => None,
        };
        let (release_latency_ms, release_post_bracket_ms, release_after) = match released.first() {
            Some(r) => {
                let trigger = ps
                    .iter()
                    .filter(|p| {
                        (p.step == "key_up" || p.step == "mod_up") && p.before_ns <= r.host_ns
                    })
                    .max_by_key(|p| p.before_ns);
                match trigger {
                    Some(p) => (
                        Some((r.host_ns as f64 - p.before_ns as f64) / 1e6),
                        Some(bracket_ms(p)),
                        Some(p.step.clone()),
                    ),
                    None => (None, None, None),
                }
            }
            None => (None, None, None),
        };
        let ordered = match (pressed.first(), released.first()) {
            (Some(p), Some(r)) => p.host_ns < r.host_ns,
            _ => false,
        };
        // The handler can run once CGEventPost has been entered, so the press is judged
        // against the host clock read just before the call, not just after it.
        let press_after_post = press_latency_ms.is_some_and(|l| l >= 0.0);
        let ok = pressed.len() == 1
            && released.len() == 1
            && ordered
            && press_after_post
            && release_latency_ms.is_some();
        if !ok {
            failures.push(format!(
                "trial {trial}: {} pressed, {} released, ordered={ordered}, press after post={press_after_post}",
                pressed.len(),
                released.len()
            ));
        }
        trials.push(InjectTrial {
            trial: *trial,
            pressed: pressed.len(),
            released: released.len(),
            press_latency_ms,
            press_post_bracket_ms: key_down.map(|p| bracket_ms(p)),
            release_latency_ms,
            release_post_bracket_ms,
            release_after,
            ok,
        });
    }
    let stray_edges = (0..edges.len()).filter(|i| !used.contains(i)).count();
    if stray_edges > 0 {
        failures.push(format!(
            "{stray_edges} handler call(s) outside every injected trial (before the first post, \
             between trials, or over {INJECT_TAIL_MS} ms after a trial's last post)"
        ));
    }
    let press: Vec<f64> = trials.iter().filter_map(|t| t.press_latency_ms).collect();
    let release: Vec<f64> = trials.iter().filter_map(|t| t.release_latency_ms).collect();
    Some(InjectJoin {
        status: Status::from_bool(failures.is_empty()),
        trials,
        stray_edges,
        press_latency_ms: summarize(&press),
        release_latency_ms: summarize(&release),
        max_post_bracket_ms: Some(max_bracket_ns as f64 / 1e6),
        failures,
    })
}

// ---------------------------------------------------------------------------------------------
// Check 8 (second half): tag requests against decoded frames.
// ---------------------------------------------------------------------------------------------

/// A tag the app asked the page to draw (`tag_request` events).
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct TagRequest {
    pub tag: Tag,
    pub host_ns: u64,
}

/// One captured frame (`frame` events from `m1-sck`).
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct TagFrame {
    /// Presentation time on the host clock, or arrival time when absent.
    pub time_ns: u64,
    pub tag: Option<Tag>,
}

/// Extracts tag requests from a run.
pub fn tag_requests(recs: &[Record]) -> Vec<TagRequest> {
    recs.iter()
        .filter(|r| r.kind == Kind::Event && r.name == "tag_request")
        .filter_map(|r| {
            Some(TagRequest {
                tag: Tag {
                    gen_: u16::try_from(u64_at(&r.v, "gen")?).ok()?,
                    tier: u8::try_from(u64_at(&r.v, "tier")?).ok()?,
                },
                host_ns: u64_at(&r.v, "host_ns").unwrap_or(r.t_host_ns),
            })
        })
        .collect()
}

/// Extracts complete captured frames from a run.
pub fn tag_frames(recs: &[Record]) -> Vec<TagFrame> {
    recs.iter()
        .filter(|r| r.kind == Kind::Event && r.name == "frame")
        .filter(|r| {
            r.v.get("complete")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        })
        .map(|r| {
            let time_ns = i64_at(&r.v, "pts_host_ns")
                .and_then(|p| u64::try_from(p).ok())
                .or_else(|| u64_at(&r.v, "arrival_host_ns"))
                .unwrap_or(r.t_host_ns);
            let tag = r.v.get("tag").and_then(|t| {
                Some(Tag {
                    gen_: u16::try_from(u64_at(t, "gen_")?).ok()?,
                    tier: u8::try_from(u64_at(t, "tier")?).ok()?,
                })
            });
            TagFrame { time_ns, tag }
        })
        .collect()
}

/// The tag join's verdict.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct TagJoin {
    pub status: Status,
    pub requested: usize,
    /// Requests made while frames were arriving and held for at least `min_hold_ms`.
    pub expected: usize,
    pub seen: usize,
    pub missed: usize,
    /// Decoded tags the app never requested (a misread or a stale surface).
    pub unknown_tags: usize,
    /// Request to the first composited frame carrying the tag, in ms, for requests made while
    /// the capture was running (a request already in effect when it started would measure the
    /// capture start instead).
    pub paint_ms: Option<Summary>,
    pub failures: Vec<String>,
}

/// Matches each request with the first frame carrying its tag at or after the request and
/// before the next request (PLAN.md M1 1a check 8; PRD §10.4 "Tag"). `None` when either side is
/// missing.
pub fn tag_join(requests: &[TagRequest], frames: &[TagFrame], min_hold_ms: f64) -> Option<TagJoin> {
    if requests.is_empty() || frames.is_empty() {
        return None;
    }
    let mut reqs = requests.to_vec();
    reqs.sort_by_key(|r| r.host_ns);
    let mut fr = frames.to_vec();
    fr.sort_by_key(|f| f.time_ns);
    let cap_start = fr.first().map_or(0, |f| f.time_ns);
    let cap_end = fr.last().map_or(0, |f| f.time_ns);
    let requested_set: BTreeSet<Tag> = reqs.iter().map(|r| r.tag).collect();
    let unknown_tags = fr
        .iter()
        .filter(|f| f.tag.is_some_and(|t| !requested_set.contains(&t)))
        .count();
    let mut expected = 0;
    let mut seen = 0;
    let mut missed = 0;
    let mut paint = Vec::new();
    for (i, r) in reqs.iter().enumerate() {
        let next = reqs.get(i + 1).map_or(u64::MAX, |n| n.host_ns);
        let held_ms = (next.saturating_sub(r.host_ns)) as f64 / 1e6;
        let first = fr
            .iter()
            .find(|f| f.time_ns >= r.host_ns && f.time_ns < next && f.tag == Some(r.tag));
        if let Some(f) = first {
            seen += 1;
            if r.host_ns >= cap_start {
                paint.push((f.time_ns as f64 - r.host_ns as f64) / 1e6);
            }
        }
        let inside = r.host_ns >= cap_start && next <= cap_end;
        if inside && held_ms >= min_hold_ms {
            expected += 1;
            if first.is_none() {
                missed += 1;
            }
        }
    }
    let mut failures = Vec::new();
    if unknown_tags > 0 {
        failures.push(format!(
            "{unknown_tags} frame(s) carried a tag the app never drew"
        ));
    }
    if missed > 0 {
        failures.push(format!(
            "{missed} drawn tag(s) never appeared in a captured frame before the next one"
        ));
    }
    if expected == 0 {
        failures.push("no tag request fell inside the capture".into());
    }
    let status = if expected == 0 {
        Status::Incomplete
    } else {
        Status::from_bool(unknown_tags == 0 && missed == 0)
    };
    Some(TagJoin {
        status,
        requested: reqs.len(),
        expected,
        seen,
        missed,
        unknown_tags,
        paint_ms: summarize(&paint),
        failures,
    })
}

/// Join verdicts that can be computed from this run, as (slug, status, criterion, values).
pub fn join_results(recs: &[Record]) -> Vec<(String, Status, String, Value)> {
    let mut out = Vec::new();
    if let Some(j) = inject_join(&posts(recs), &app_edges(recs)) {
        out.push((
            "inject".to_string(),
            j.status,
            crate::checks::criterion("inject").to_string(),
            serde_json::to_value(&j).unwrap_or(Value::Null),
        ));
    }
    if let Some((status, v)) = crate::consent::from_records(recs) {
        out.push((
            "consent".to_string(),
            status,
            crate::checks::criterion("consent").to_string(),
            v,
        ));
    }
    if let Some(j) = tag_join(&tag_requests(recs), &tag_frames(recs), 100.0) {
        out.push((
            "sck-tag-join".to_string(),
            j.status,
            "Every decoded tag is one the app drew, and every tag held for at least 100 ms while \
             the capture ran appears in a captured frame before the next tag."
                .to_string(),
            serde_json::to_value(&j).unwrap_or(Value::Null),
        ));
    }
    out
}

fn status_word(s: Option<Status>) -> &'static str {
    match s {
        Some(Status::Pass) => "PASS",
        Some(Status::Fail) => "FAIL",
        Some(Status::Incomplete) => "INCOMPLETE",
        Some(Status::Skipped) => "SKIPPED",
        None => "not run",
    }
}

/// A plain-text table of one run: every PLAN check with the latest verdict found, then the joins.
pub fn render(recs: &[Record], bad_lines: usize) -> String {
    let results = latest_results(recs);
    let joins = join_results(recs);
    let mut s = String::new();
    let run_id = recs.first().map_or("?", |r| r.run_id.as_str());
    let _ = writeln!(
        s,
        "run {run_id}: {} records ({bad_lines} unreadable)",
        recs.len()
    );
    let _ = writeln!(s, " #  check       verdict     by");
    let mut any_fail = false;
    for c in CHECKS {
        let joined = joins.iter().find(|j| j.0 == c.slug);
        let (status, by) = match (results.get(c.slug), joined) {
            (Some(r), _) => (r.status, r.proc.clone()),
            (None, Some(j)) => (Some(j.1), "m1-report (join)".to_string()),
            (None, None) => (None, String::new()),
        };
        any_fail |= status == Some(Status::Fail);
        let _ = writeln!(
            s,
            "{:>2}  {:<11} {:<11} {}",
            c.n,
            c.slug,
            status_word(status),
            by
        );
    }
    for (slug, status, _, v) in &joins {
        any_fail |= *status == Status::Fail;
        let detail = v
            .get("failures")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join("; ")
            })
            .unwrap_or_default();
        let _ = writeln!(s, "join {slug}: {} {detail}", status_word(Some(*status)));
    }
    for r in results.values() {
        if r.status == Some(Status::Fail) {
            let _ = writeln!(s, "  {} failed: {}", r.name, short(&r.v));
        }
    }
    if any_fail {
        let _ = writeln!(s, "{STOP_RULE}");
    }
    s
}

fn short(v: &Value) -> String {
    let reasons = v.get("reasons").or_else(|| v.get("failures"));
    match reasons.and_then(Value::as_array) {
        Some(a) => a
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join("; "),
        None => {
            let t = v.to_string();
            t.chars().take(200).collect()
        }
    }
}

/// The value list for an owner note: `key=value` pairs, values parsed as JSON when they parse.
pub fn note_values(pairs: &[String]) -> Result<Value, String> {
    let mut m = serde_json::Map::new();
    for p in pairs {
        let (k, v) = p
            .split_once('=')
            .ok_or_else(|| format!("{p:?} is not key=value"))?;
        if k.is_empty() {
            return Err(format!("{p:?} has an empty key"));
        }
        let val = serde_json::from_str(v).unwrap_or_else(|_| json!(v));
        m.insert(k.to_string(), val);
    }
    Ok(Value::Object(m))
}

/// Checks an owner note before `m1-report note` writes it. For check 11 (`consent`),
/// `prompt_seen` must be a yes/no answer ([`crate::consent::parse_yes_no`]) and is stored as a
/// boolean, so the verdict reads it whichever spelling was typed.
pub fn normalise_note(check: &str, mut v: Value) -> Result<Value, String> {
    if check == "consent"
        && let Some(p) = v.get("prompt_seen")
    {
        let b = crate::consent::parse_yes_no(p)
            .ok_or_else(|| format!("prompt_seen={p}: use yes or no (or true, false, y, n)"))?;
        v["prompt_seen"] = json!(b);
    }
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jsonl::{RunLog, read_run};

    const MS: u64 = 1_000_000;

    fn post(trial: u64, step: &str, t_ms: u64) -> Post {
        Post {
            trial,
            step: step.into(),
            before_ns: t_ms * MS,
            after_ns: t_ms * MS + 20_000,
        }
    }

    #[test]
    fn injection_join_measures_latency_and_passes_clean_runs() {
        let mut ps = Vec::new();
        let mut es = Vec::new();
        for k in 0..3u64 {
            let t = 1000 + k * 2000;
            ps.push(post(k, "mod_down", t));
            ps.push(post(k, "key_down", t + 10));
            ps.push(post(k, "key_up", t + 810));
            ps.push(post(k, "mod_up", t + 820));
            es.push(AppEdge {
                pressed: true,
                host_ns: (t + 11) * MS,
            });
            es.push(AppEdge {
                pressed: false,
                host_ns: (t + 812) * MS,
            });
        }
        let j = inject_join(&ps, &es).expect("join");
        assert_eq!(j.status, Status::Pass, "{:?}", j.failures);
        assert_eq!(j.trials.len(), 3);
        // Measured from the stamp before each post (an upper bound), with the 20 µs bracket.
        let pl = j.press_latency_ms.expect("latency");
        assert!((pl.p50 - 1.0).abs() < 1e-9, "{pl:?}");
        assert_eq!(j.trials[0].press_post_bracket_ms, Some(0.02));
        assert!((j.trials[0].release_latency_ms.expect("release") - 2.0).abs() < 1e-9);
        assert_eq!(j.max_post_bracket_ms, Some(0.02));
        assert_eq!(j.trials[0].release_after.as_deref(), Some("key_up"));
    }

    /// The reviewer's case: the injector thread is preempted between CGEventPost returning and
    /// its second clock read, so the handler entry falls inside the post's bracket. That press
    /// is correct and must not fail the trial.
    #[test]
    fn a_press_inside_the_post_bracket_passes() {
        let ps = vec![
            Post {
                trial: 0,
                step: "key_down".into(),
                before_ns: 1_000_000_000,
                after_ns: 1_000_900_000,
            },
            post(0, "key_up", 1800),
        ];
        let es = vec![
            AppEdge {
                pressed: true,
                host_ns: 1_000_500_000,
            },
            AppEdge {
                pressed: false,
                host_ns: 1_801_000_000,
            },
        ];
        let j = inject_join(&ps, &es).expect("join");
        assert_eq!(j.status, Status::Pass, "{:?}", j.failures);
        assert!((j.trials[0].press_latency_ms.expect("latency") - 0.5).abs() < 1e-9);
        assert!((j.trials[0].press_post_bracket_ms.expect("bracket") - 0.9).abs() < 1e-9);
    }

    /// The reviewer's case: before the fix the last trial's window ran to the end of time, so a
    /// press 20 s after the run was blamed on trial 2 and nothing was ever stray.
    #[test]
    fn handler_calls_outside_every_trial_are_stray() {
        let mut ps = Vec::new();
        let mut es = Vec::new();
        for k in 0..3u64 {
            let t = 1000 + k * 2000;
            ps.push(post(k, "key_down", t));
            ps.push(post(k, "key_up", t + 600));
            es.push(AppEdge {
                pressed: true,
                host_ns: (t + 1) * MS,
            });
            es.push(AppEdge {
                pressed: false,
                host_ns: (t + 601) * MS,
            });
        }
        let clean = inject_join(&ps, &es).expect("join");
        assert_eq!(clean.status, Status::Pass, "{:?}", clean.failures);
        assert_eq!(clean.stray_edges, 0);
        // A press 20 s after the last post, and one before the first post.
        es.push(AppEdge {
            pressed: true,
            host_ns: (5600 + 20_000) * MS,
        });
        es.push(AppEdge {
            pressed: true,
            host_ns: 500 * MS,
        });
        let j = inject_join(&ps, &es).expect("join");
        assert_eq!(j.stray_edges, 2);
        assert_eq!(j.status, Status::Fail);
        assert!(j.trials.iter().all(|t| t.ok), "{:?}", j.trials);
        assert!(j.failures[0].contains("outside every injected trial"));
        // A late release 1.5 s after a trial's last post is outside that trial's window.
        let late = vec![
            post(0, "key_down", 1000),
            post(0, "key_up", 1600),
            post(1, "key_down", 4000),
            post(1, "key_up", 4600),
        ];
        let late_es = vec![
            AppEdge {
                pressed: true,
                host_ns: 1001 * MS,
            },
            AppEdge {
                pressed: false,
                host_ns: 3100 * MS,
            },
        ];
        let j = inject_join(&late, &late_es).expect("join");
        assert_eq!((j.stray_edges, j.trials[0].released), (1, 0));
    }

    #[test]
    fn injection_join_fails_on_a_missing_release_and_stray_edges() {
        let ps = vec![
            post(0, "key_down", 1000),
            post(0, "key_up", 1800),
            post(1, "key_down", 3000),
            post(1, "key_up", 3800),
        ];
        let es = vec![
            AppEdge {
                pressed: true,
                host_ns: 1001 * MS,
            },
            AppEdge {
                pressed: true,
                host_ns: 3001 * MS,
            },
            AppEdge {
                pressed: false,
                host_ns: 3801 * MS,
            },
        ];
        let j = inject_join(&ps, &es).expect("join");
        assert_eq!(j.status, Status::Fail);
        assert!(!j.trials[0].ok && j.trials[1].ok);
        assert!(inject_join(&[], &es).is_none());
    }

    #[test]
    fn modifier_first_release_is_attributed_to_the_modifier_post() {
        let ps = vec![
            post(0, "mod_down", 1000),
            post(0, "key_down", 1010),
            post(0, "mod_up", 1500),
            post(0, "key_up", 2500),
        ];
        let es = vec![
            AppEdge {
                pressed: true,
                host_ns: 1011 * MS,
            },
            AppEdge {
                pressed: false,
                host_ns: 1501 * MS,
            },
        ];
        let j = inject_join(&ps, &es).expect("join");
        assert_eq!(j.trials[0].release_after.as_deref(), Some("mod_up"));
        assert_eq!(j.status, Status::Pass);
    }

    fn req(gen_: u16, t_ms: u64) -> TagRequest {
        TagRequest {
            tag: Tag {
                gen_,
                tier: (gen_ % 3) as u8,
            },
            host_ns: t_ms * MS,
        }
    }

    fn frame(gen_: Option<u16>, t_ms: u64) -> TagFrame {
        TagFrame {
            time_ns: t_ms * MS,
            tag: gen_.map(|g| Tag {
                gen_: g,
                tier: (g % 3) as u8,
            }),
        }
    }

    #[test]
    fn tag_join_reports_paint_times_and_passes_when_every_tag_shows() {
        let reqs: Vec<_> = (0..5).map(|k| req(k, 1000 + u64::from(k) * 250)).collect();
        let mut frames = Vec::new();
        for t in (990..2300).step_by(8) {
            // The tag in effect 25 ms after each request.
            let k = (t as i64 - 1025).div_euclid(250);
            let g = if (0..5).contains(&k) {
                Some(k as u16)
            } else {
                None
            };
            frames.push(frame(g, t));
        }
        let j = tag_join(&reqs, &frames, 100.0).expect("join");
        assert_eq!(j.status, Status::Pass, "{:?}", j.failures);
        assert_eq!((j.unknown_tags, j.missed), (0, 0));
        let p = j.paint_ms.expect("paint");
        assert!(p.min >= 25.0 && p.max < 34.0, "{p:?}");
    }

    #[test]
    fn tag_join_fails_on_unknown_and_missed_tags() {
        let reqs = vec![req(1, 1000), req(2, 1250), req(3, 1500), req(4, 1750)];
        let frames = vec![
            frame(Some(1), 1010),
            frame(Some(9), 1260),
            frame(Some(3), 1510),
            frame(Some(4), 1760),
            frame(None, 2100),
        ];
        let j = tag_join(&reqs, &frames, 100.0).expect("join");
        assert_eq!(j.status, Status::Fail);
        assert_eq!((j.unknown_tags, j.missed), (1, 1));
        assert!(tag_join(&[], &frames, 100.0).is_none());
    }

    /// The reviewer's case: true paint is 30 ms, but the capture starts 240 ms after a request,
    /// whose first frame then measures the capture start. Before the fix: n=32, max 240 ms.
    #[test]
    fn paint_times_leave_out_the_request_in_effect_when_the_capture_starts() {
        // A request every 250 ms from t=1000 ms; each tag shows from request + 30 ms.
        let reqs: Vec<_> = (0..33).map(|k| req(k, 1000 + u64::from(k) * 250)).collect();
        let mut frames = Vec::new();
        // The capture starts 240 ms after the first request; frames every 8 ms.
        for t in (1240..1000 + 33 * 250).step_by(8) {
            let k = (t as i64 - 1030).div_euclid(250);
            frames.push(frame((0..33).contains(&k).then_some(k as u16), t));
        }
        let j = tag_join(&reqs, &frames, 100.0).expect("join");
        assert_eq!(j.status, Status::Pass, "{:?}", j.failures);
        let p = j.paint_ms.expect("paint");
        assert_eq!(p.n, 32, "{p:?}");
        assert!(p.max < 40.0 && p.min >= 30.0, "{p:?}");
        // The request in effect at the start was still seen, just not timed.
        assert_eq!(j.seen, 33);
    }

    /// A tag that shows up only after the next request was drawn (a stale surface) is missed.
    #[test]
    fn a_tag_seen_only_after_the_next_request_is_missed() {
        let reqs = vec![req(1, 1000), req(2, 1250), req(3, 1500), req(4, 1750)];
        let frames = vec![
            frame(Some(1), 1010),
            frame(Some(1), 1260),
            frame(Some(2), 1300),
            frame(Some(3), 1510),
            frame(Some(4), 1760),
            frame(Some(4), 2100),
        ];
        let j = tag_join(&reqs, &frames, 100.0).expect("join");
        assert_eq!((j.missed, j.unknown_tags), (0, 0), "{j:?}");
        let late = vec![
            frame(Some(1), 1010),
            frame(Some(3), 1510),
            frame(Some(2), 1520),
            frame(Some(4), 1760),
            frame(Some(4), 2100),
        ];
        let j = tag_join(&reqs, &late, 100.0).expect("join");
        assert_eq!(j.missed, 1, "{j:?}");
        assert_eq!(j.status, Status::Fail);
    }

    #[test]
    fn renders_a_run_with_results_and_joins() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("rr.jsonl");
        let app = RunLog::open_at(&path, "rr", "hotkey", "m1-spike", &[]).expect("open");
        let inj = RunLog::open_at(&path, "rr", "inject", "m1-inject", &[]).expect("open");
        app.result("hotkey", Status::Pass, "c", json!({}))
            .expect("w");
        app.result(
            "cpal",
            Status::Fail,
            "c",
            json!({"reasons": ["silent buffers"]}),
        )
        .expect("w");
        let t = app.stamp().host_ns;
        inj.event(
            "posted",
            json!({"trial": 0, "step": "key_down", "host_before_ns": t, "host_after_ns": t + 10}),
        )
        .expect("w");
        app.event(
            "hotkey_edge",
            json!({"edge": "pressed", "host_ns": t + 1_000}),
        )
        .expect("w");
        inj.event("posted", json!({"trial": 0, "step": "key_up", "host_before_ns": t + 5_000_000, "host_after_ns": t + 5_000_010}))
            .expect("w");
        app.event(
            "hotkey_edge",
            json!({"edge": "released", "host_ns": t + 5_100_000}),
        )
        .expect("w");
        let (recs, bad) = read_run(&path).expect("read");
        let text = render(&recs, bad);
        assert!(text.contains(" 1  hotkey      PASS"), "{text}");
        assert!(text.contains(" 5  cpal        FAIL"), "{text}");
        assert!(text.contains(" 9  inject      PASS"), "{text}");
        assert!(text.contains("silent buffers"), "{text}");
        assert!(text.contains("stop and report"), "{text}");
    }

    #[test]
    fn note_values_parse_json_or_strings() {
        let v = note_values(&["prompt_seen=false".into(), "app=TextEdit".into()]).expect("note");
        assert_eq!(v, json!({"prompt_seen": false, "app": "TextEdit"}));
        assert!(note_values(&["novalue".into()]).is_err());
    }

    #[test]
    fn consent_notes_store_prompt_seen_as_a_boolean_or_are_refused() {
        let v = note_values(&["prompt_seen=no".into(), "scenario=repeat".into()]).expect("note");
        assert_eq!(
            normalise_note("consent", v).expect("ok"),
            json!({"prompt_seen": false, "scenario": "repeat"})
        );
        let y = note_values(&["prompt_seen=Yes".into()]).expect("note");
        assert_eq!(
            normalise_note("consent", y).expect("ok")["prompt_seen"],
            true
        );
        let bad = note_values(&["prompt_seen=maybe".into()]).expect("note");
        assert!(normalise_note("consent", bad.clone()).is_err());
        // Other checks' notes are stored as given.
        assert_eq!(normalise_note("panel", bad.clone()).expect("ok"), bad);
    }
}
