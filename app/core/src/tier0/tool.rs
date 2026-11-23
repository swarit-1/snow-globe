//! `snow-globe-tier0`: the library report and the measured run (PRD §9.1, §10.1, §10.4).
//!
//! ```text
//! snow-globe-tier0 index [--data-dir <dir>] [--manifests <dir>] [--rebuild | --no-cache] [--pretty]
//! snow-globe-tier0 bench [--data-dir <dir>] [--now <RFC3339>] [--queries <n>] [--hit-queries <n>]
//!                        [--days <n>] [--seed <n>] [--cold-runs <n>] [--warm-runs <n>]
//!                        [--threads <n>] [--step-min <n>] [--keyup-rounds <n>] [--out <file>]
//! ```
//!
//! `index` builds (or refreshes) the library and prints its report: entries and exclusions per
//! viewpoint, the cache's status and the build's timings.
//!
//! `bench` is the measured run on a real data dir. Nothing in it is mocked: the real sidecars,
//! the real ephemeris, the real JPEG files.
//! 1. **Index build**: `--cold-runs` builds with no index cache (every sidecar read and
//!    classified; the operating system's file cache is warm after the first), then one build that
//!    writes the cache, then `--warm-runs` loads through the cache.
//! 2. **Match latency**: `--queries` matches (default 2,000), the viewpoint cycling through all 8
//!    and the valid time uniform over the next `--days` (default 7) from `--now` (default: the
//!    clock), with only frames available at `--now` qualifying ([`MatchRequest::as_of`]). Each is
//!    timed around [`find`], which includes the query's ephemeris.
//! 3. **Prefetch and decode**: each viewpoint's 48 h plan ([`plan_prefetch`], a `--step-min`
//!    grid, default 1); every planned frame decoded once on one thread, timed per frame
//!    ([`decode_image`]: the path check, read, decode, resize); then each plan decoded again in
//!    the background shape ([`decode_entries`], `--threads` workers, default 2), timed as a whole.
//! 4. **Hit rate**: `--hit-queries` valid times per viewpoint (default 1,000), uniform over the
//!    next 48 h: the share whose best frame is in the decoded set, so nothing is decoded at
//!    key-up.
//! 5. **The key-up path in the core**: [`KEYUP_UTTERANCES`] (two per viewpoint, written here, not
//!    the replay set) × `--keyup-rounds` (default 50), each parsed at `--now`, matched, and looked
//!    up in the decoded set (or decoded on the spot on a miss), every stage timed. This is the
//!    core's share of PRD §10.1's Tier 0 chain: its parse (5 ms allowed), its match, and the
//!    decoded-set lookup that starts the swap. The swap itself (the hand-over to the webview,
//!    the texture upload and the paint; ≤ 10 ms allowed), speech and the composite are the
//!    overlay's and are not measured here.
//!
//! The JSON report goes to stdout (and to `--out`), a summary table to stderr.

use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::Instant;

use chrono::{DateTime, Duration, Utc};
use serde_json::{Value, json};

use crate::cli::{CliOutput, parse_now_arg};
use crate::datadir::{check_data_dir, data_dir};
use crate::query::fmt_utc;
use crate::tier0::camera::CameraTable;
use crate::tier0::decode::{
    DISPLAY_MAX_WIDTH, DecodedSet, PREFETCH_HORIZON_H, PREFETCH_STEP_MIN, PREFETCH_THREADS,
    decode_entries, decode_image, plan_prefetch,
};
use crate::tier0::heldout::HeldOutRules;
use crate::tier0::library::{CACHE_REL_PATH, CacheMode, Library};
use crate::tier0::matcher::{MatchOutcome, MatchRequest, find};

/// Bad arguments (BSD `EX_USAGE`).
pub const EXIT_USAGE: i32 = 64;

/// The utterances of the key-up measurement: two per viewpoint, across the time rules. Written
/// for this measurement; the replay script's dev and test rows are not used here.
pub const KEYUP_UTTERANCES: &[&str] = &[
    "is rainier out tomorrow at 4",
    "show me the mountain at sunset",
    "point reyes at sunset friday",
    "point reyes tomorrow morning",
    "grand canyon golden hour",
    "grand canyon at noon tomorrow",
    "the tetons tonight",
    "grand teton at 9 am tomorrow",
    "yosemite tomorrow at 3 pm",
    "turtleback dome at dawn",
    "acadia at sunrise",
    "mcfarland hill in two hours",
    "smokies at 4 pm on wednesday",
    "look rock tomorrow evening",
    "big bend at dusk tomorrow",
    "panther junction at midnight",
];
/// The library is unavailable.
pub const EXIT_LIBRARY: i32 = 4;

/// The usage text.
pub const USAGE: &str = "usage: snow-globe-tier0 index [--data-dir <dir>] [--manifests <dir>] [--rebuild | --no-cache] [--pretty]\n       \
snow-globe-tier0 bench [--data-dir <dir>] [--now <RFC3339>] [--queries <n>] [--hit-queries <n>] [--days <n>] [--seed <n>] [--cold-runs <n>] [--warm-runs <n>] [--threads <n>] [--step-min <n>] [--keyup-rounds <n>] [--out <file>]";

fn usage(msg: &str) -> CliOutput {
    CliOutput {
        code: EXIT_USAGE,
        stdout: String::new(),
        stderr: format!("snow-globe-tier0: {msg}\n{USAGE}\n"),
    }
}

fn fail(msg: &str) -> CliOutput {
    CliOutput {
        code: EXIT_LIBRARY,
        stdout: String::new(),
        stderr: format!("snow-globe-tier0: {msg}\n"),
    }
}

/// Nearest-rank percentile of a sorted slice (as `examples/parse_bench.rs`).
pub fn pct(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    sorted[(((sorted.len() as f64) * p).ceil() as usize).clamp(1, sorted.len()) - 1]
}

fn summary(v: &mut [f64]) -> Value {
    v.sort_by(f64::total_cmp);
    let mean = if v.is_empty() {
        f64::NAN
    } else {
        v.iter().sum::<f64>() / v.len() as f64
    };
    json!({
        "n": v.len(),
        "p50": round3(pct(v, 0.50)),
        "p95": round3(pct(v, 0.95)),
        "p99": round3(pct(v, 0.99)),
        "max": round3(v.last().copied().unwrap_or(f64::NAN)),
        "mean": round3(mean),
    })
}

fn round3(x: f64) -> f64 {
    (x * 1000.0).round() / 1000.0
}

/// SplitMix64, for reproducible query times.
#[derive(Debug, Clone)]
pub struct SplitMix(pub u64);

impl SplitMix {
    /// The next 64 random bits.
    pub fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// Uniform in `[0, n)` (n > 0).
    pub fn below(&mut self, n: u64) -> u64 {
        self.next_u64() % n.max(1)
    }
}

struct Common {
    data_dir: Option<String>,
    manifests: Option<String>,
    pretty: bool,
}

fn resolve_dir(arg: Option<&str>) -> Result<PathBuf, String> {
    match arg {
        Some(d) => {
            let home = std::env::var_os("HOME")
                .map(PathBuf::from)
                .unwrap_or_default();
            check_data_dir(Path::new(d), &home).map_err(|e| e.to_string())
        }
        None => data_dir().map_err(|e| e.to_string()),
    }
}

fn cameras_for(manifests: Option<&str>) -> Result<CameraTable, String> {
    match manifests {
        None => Ok(CameraTable::embedded().clone()),
        Some(d) => CameraTable::from_dir(Path::new(d)).map_err(|e| e.to_string()),
    }
}

/// Run the command on `args` (without the program name).
pub fn run(args: &[String]) -> CliOutput {
    let Some((sub, rest)) = args.split_first() else {
        return usage("give a subcommand: index or bench");
    };
    match sub.as_str() {
        "index" => run_index(rest),
        "bench" => run_bench(rest),
        "-h" | "--help" => CliOutput {
            code: 0,
            stdout: format!("{USAGE}\n"),
            stderr: String::new(),
        },
        other => usage(&format!("unknown subcommand {other:?}")),
    }
}

fn run_index(args: &[String]) -> CliOutput {
    let mut c = Common {
        data_dir: None,
        manifests: None,
        pretty: false,
    };
    let mut mode = CacheMode::Use;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--data-dir" => match it.next() {
                Some(v) => c.data_dir = Some(v.clone()),
                None => return usage("--data-dir needs a value"),
            },
            "--manifests" => match it.next() {
                Some(v) => c.manifests = Some(v.clone()),
                None => return usage("--manifests needs a value"),
            },
            "--rebuild" => mode = CacheMode::Rebuild,
            "--no-cache" => mode = CacheMode::Off,
            "--pretty" => c.pretty = true,
            other => return usage(&format!("unknown option {other:?}")),
        }
    }
    let (dir, cameras, rules) = match setup(&c) {
        Ok(x) => x,
        Err(e) => return fail(&e),
    };
    match Library::build(&dir, &cameras, rules, mode) {
        Ok(lib) => {
            let text = if c.pretty {
                serde_json::to_string_pretty(lib.report())
            } else {
                serde_json::to_string(lib.report())
            };
            CliOutput {
                code: 0,
                stdout: format!("{}\n", text.unwrap_or_default()),
                stderr: String::new(),
            }
        }
        Err(e) => fail(&e.to_string()),
    }
}

fn setup(c: &Common) -> Result<(PathBuf, CameraTable, &'static HeldOutRules), String> {
    let dir = resolve_dir(c.data_dir.as_deref())?;
    let cameras = cameras_for(c.manifests.as_deref())?;
    let rules = HeldOutRules::committed().map_err(|e| e.to_string())?;
    Ok((dir, cameras, rules))
}

struct BenchArgs {
    common: Common,
    now: DateTime<Utc>,
    queries: usize,
    hit_queries: usize,
    days: i64,
    seed: u64,
    cold_runs: usize,
    warm_runs: usize,
    threads: usize,
    step_min: i64,
    keyup_rounds: usize,
    out: Option<String>,
}

fn parse_bench(args: &[String]) -> Result<BenchArgs, CliOutput> {
    let mut b = BenchArgs {
        common: Common {
            data_dir: None,
            manifests: None,
            pretty: false,
        },
        now: Utc::now(),
        queries: 2000,
        hit_queries: 1000,
        days: 7,
        seed: 0x5EED_7140,
        cold_runs: 3,
        warm_runs: 5,
        threads: PREFETCH_THREADS,
        step_min: PREFETCH_STEP_MIN,
        keyup_rounds: 50,
        out: None,
    };
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let mut value = |name: &str| {
            it.next()
                .cloned()
                .ok_or_else(|| usage(&format!("{name} needs a value")))
        };
        fn num<T: std::str::FromStr>(name: &str, v: &str) -> Result<T, CliOutput> {
            v.parse()
                .map_err(|_| usage(&format!("{name} {v:?} is not a number")))
        }
        match a.as_str() {
            "--data-dir" => b.common.data_dir = Some(value("--data-dir")?),
            "--now" => {
                let v = value("--now")?;
                b.now = parse_now_arg(&v, None).map_err(|m| usage(&m))?;
            }
            "--queries" => b.queries = num("--queries", &value("--queries")?)?,
            "--hit-queries" => b.hit_queries = num("--hit-queries", &value("--hit-queries")?)?,
            "--days" => b.days = num("--days", &value("--days")?)?,
            "--seed" => b.seed = num("--seed", &value("--seed")?)?,
            "--cold-runs" => b.cold_runs = num("--cold-runs", &value("--cold-runs")?)?,
            "--warm-runs" => b.warm_runs = num("--warm-runs", &value("--warm-runs")?)?,
            "--threads" => b.threads = num("--threads", &value("--threads")?)?,
            "--step-min" => b.step_min = num("--step-min", &value("--step-min")?)?,
            "--keyup-rounds" => b.keyup_rounds = num("--keyup-rounds", &value("--keyup-rounds")?)?,
            "--out" => b.out = Some(value("--out")?),
            other => return Err(usage(&format!("unknown option {other:?}"))),
        }
    }
    Ok(b)
}

fn run_bench(args: &[String]) -> CliOutput {
    let b = match parse_bench(args) {
        Ok(b) => b,
        Err(out) => return out,
    };
    let (dir, cameras, rules) = match setup(&b.common) {
        Ok(x) => x,
        Err(e) => return fail(&e),
    };
    match bench(&b, &dir, &cameras, rules) {
        Ok((report, table)) => {
            let text = serde_json::to_string_pretty(&report).unwrap_or_default();
            let mut stderr = table;
            if let Some(path) = &b.out
                && let Err(e) = std::fs::write(path, format!("{text}\n"))
            {
                stderr.push_str(&format!("snow-globe-tier0: cannot write {path}: {e}\n"));
            }
            CliOutput {
                code: 0,
                stdout: format!("{text}\n"),
                stderr,
            }
        }
        Err(e) => fail(&e),
    }
}

/// Per viewpoint: match latencies (µs), best distances, and outcome counts.
type VpSamples = (Vec<f64>, Vec<f64>, BTreeMap<String, usize>);

#[allow(clippy::too_many_lines)]
fn bench(
    b: &BenchArgs,
    dir: &Path,
    cameras: &CameraTable,
    rules: &HeldOutRules,
) -> Result<(Value, String), String> {
    // 1. Index build.
    let mut cold = Vec::new();
    let mut cold_last = None;
    for _ in 0..b.cold_runs.max(1) {
        let lib = Library::build(dir, cameras, rules, CacheMode::Off).map_err(|e| e.to_string())?;
        cold.push(lib.report().timings.total_ms);
        cold_last = Some(lib.report().timings.clone());
    }
    let first = Library::build(dir, cameras, rules, CacheMode::Use).map_err(|e| e.to_string())?;
    let write_build = json!({
        "total_ms": round3(first.report().timings.total_ms),
        "cache": first.report().cache,
    });
    let mut warm = Vec::new();
    let mut warm_status = Vec::new();
    let mut lib = first;
    for _ in 0..b.warm_runs.max(1) {
        lib = Library::build(dir, cameras, rules, CacheMode::Use).map_err(|e| e.to_string())?;
        warm.push(lib.report().timings.total_ms);
        warm_status.push(
            lib.report()
                .cache
                .as_ref()
                .map(|c| c.status.clone())
                .unwrap_or_default(),
        );
    }
    let warm_last = lib.report().timings.clone();
    let cache_bytes = std::fs::metadata(dir.join(CACHE_REL_PATH)).map_or(0, |m| m.len());
    let cold_runs = cold.clone();
    let warm_runs = warm.clone();

    // 2. Match latency.
    let viewpoints: Vec<String> = cameras
        .cameras()
        .iter()
        .map(|c| c.viewpoint_id.clone())
        .collect();
    let mut rng = SplitMix(b.seed);
    let span_ms = u64::try_from(b.days.max(1) * 86_400_000).unwrap_or(u64::MAX);
    let mut all_us = Vec::with_capacity(b.queries);
    let mut per_vp: BTreeMap<String, VpSamples> = BTreeMap::new();
    for i in 0..b.queries {
        let vp = &viewpoints[i % viewpoints.len()];
        let offset = i64::try_from(rng.below(span_ms)).unwrap_or(0);
        let mut req = MatchRequest::new(vp, b.now + Duration::milliseconds(offset));
        req.as_of = Some(b.now);
        let t = Instant::now();
        let outcome = find(&lib, cameras, &req);
        let us = t.elapsed().as_secs_f64() * 1e6;
        all_us.push(us);
        let slot = per_vp.entry(vp.clone()).or_default();
        slot.0.push(us);
        match outcome {
            MatchOutcome::Matched(m) => {
                slot.1.push(m.best.distance);
                *slot.2.entry("matched".to_string()).or_default() += 1;
            }
            MatchOutcome::NoFrame(n) => {
                let name = serde_json::to_value(n.reason)
                    .ok()
                    .and_then(|v| v.as_str().map(str::to_string))
                    .unwrap_or_default();
                *slot.2.entry(name).or_default() += 1;
            }
        }
    }
    let mut match_vp = serde_json::Map::new();
    let mut all_dist = Vec::new();
    for (vp, (mut us, mut dist, outcomes)) in per_vp {
        all_dist.extend(dist.iter().copied());
        match_vp.insert(
            vp,
            json!({"latency_us": summary(&mut us), "best_distance": summary(&mut dist), "outcomes": outcomes}),
        );
    }

    // 3. Prefetch plans and decode.
    let mut prefetch_vp = serde_json::Map::new();
    let mut decode_all = Vec::new();
    let mut check_all = Vec::new();
    let mut read_all = Vec::new();
    let mut decode_only = Vec::new();
    let mut by_px: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    let mut failures = Vec::new();
    let mut sets: BTreeMap<String, (HashSet<usize>, DecodedSet)> = BTreeMap::new();
    for vp in &viewpoints {
        let plan = plan_prefetch(
            &lib,
            cameras,
            vp,
            b.now,
            PREFETCH_HORIZON_H,
            b.step_min,
            Some(b.now),
        );
        let mut bytes = 0usize;
        for &i in &plan.indexes {
            let e = &lib.entries()[i];
            match decode_image(&lib, &e.image, DISPLAY_MAX_WIDTH) {
                Ok((img, timing)) => {
                    decode_all.push(timing.total_ms);
                    check_all.push(timing.check_ms);
                    read_all.push(timing.read_ms);
                    decode_only.push(timing.decode_ms + timing.resize_ms);
                    by_px
                        .entry(format!("{}x{}", img.source_width, img.source_height))
                        .or_default()
                        .push(timing.total_ms);
                    bytes += img.bytes();
                }
                Err(err) => failures.push(json!({"key": e.key, "error": err.to_string()})),
            }
        }
        let set = DecodedSet::new();
        let bg = decode_entries(&lib, &plan.indexes, b.threads, &set, None);
        prefetch_vp.insert(
            vp.clone(),
            json!({
                "grid_points": plan.grid_points,
                "no_frame_points": plan.no_frame_points,
                "frames": plan.indexes.len(),
                "plan_ms": round3(plan.plan_ms),
                "decoded_mib": round3(bytes as f64 / 1_048_576.0),
                "background_wall_ms": round3(bg.wall_ms),
                "background_threads": bg.threads,
                "background_failed": bg.failed.len(),
            }),
        );
        sets.insert(vp.clone(), (plan.indexes.iter().copied().collect(), set));
    }

    // 4. Hit rate over the next 48 h, and the decoded-set lookup.
    let mut hit_vp = serde_json::Map::new();
    let (mut hits_all, mut asked_all) = (0usize, 0usize);
    let mut lookup_us = Vec::new();
    let span48 = u64::try_from(PREFETCH_HORIZON_H * 3_600_000).unwrap_or(u64::MAX);
    for vp in &viewpoints {
        let Some((planned, set)) = sets.get(vp) else {
            continue;
        };
        let (mut hits, mut misses, mut none) = (0usize, 0usize, 0usize);
        for _ in 0..b.hit_queries {
            let offset = i64::try_from(rng.below(span48)).unwrap_or(0);
            let mut req = MatchRequest::new(vp, b.now + Duration::milliseconds(offset));
            req.as_of = Some(b.now);
            req.alternatives = 0;
            match find(&lib, cameras, &req) {
                MatchOutcome::Matched(m) => {
                    if planned.contains(&m.best.index) {
                        hits += 1;
                        let t = Instant::now();
                        let img = set.get(&m.best.key);
                        lookup_us.push(t.elapsed().as_secs_f64() * 1e6);
                        std::hint::black_box(&img);
                    } else {
                        misses += 1;
                    }
                }
                MatchOutcome::NoFrame(_) => none += 1,
            }
        }
        hits_all += hits;
        asked_all += hits + misses;
        hit_vp.insert(
            vp.clone(),
            json!({
                "hits": hits, "misses": misses, "no_frame": none,
                "hit_rate": round3(hits as f64 / ((hits + misses).max(1)) as f64),
            }),
        );
    }

    // 5. The key-up path in the core: parse → match → decoded-set lookup.
    let parser = crate::parser::Parser::embedded();
    let ctx = crate::parser::ParseContext {
        now: b.now,
        last: None,
    };
    let (mut k_parse, mut k_match, mut k_lookup, mut k_total) =
        (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    let (mut k_hits, mut k_misses, mut k_unparsed, mut k_no_frame) =
        (0usize, 0usize, 0usize, 0usize);
    let mut k_unparsed_list: Vec<String> = Vec::new();
    for _ in 0..b.keyup_rounds.max(1) {
        for u in KEYUP_UTTERANCES {
            let t0 = Instant::now();
            let parsed = parser.parse(u, &ctx);
            let t1 = Instant::now();
            let Ok(q) = parsed else {
                k_unparsed += 1;
                if !k_unparsed_list.iter().any(|x| x == u) {
                    k_unparsed_list.push((*u).to_string());
                }
                continue;
            };
            let mut req = MatchRequest::new(&q.viewpoint_id, q.valid_time_utc);
            req.as_of = Some(b.now);
            req.alternatives = 0;
            let outcome = find(&lib, cameras, &req);
            let t2 = Instant::now();
            let MatchOutcome::Matched(m) = outcome else {
                k_no_frame += 1;
                continue;
            };
            let found = match sets
                .get(&q.viewpoint_id)
                .and_then(|(_, set)| set.get(&m.best.key))
            {
                Some(img) => {
                    k_hits += 1;
                    Some(img)
                }
                None => {
                    k_misses += 1;
                    let e = &lib.entries()[m.best.index];
                    decode_image(&lib, &e.image, DISPLAY_MAX_WIDTH)
                        .ok()
                        .map(|(img, _)| std::sync::Arc::new(img))
                }
            };
            let t3 = Instant::now();
            std::hint::black_box(&found);
            k_parse.push((t1 - t0).as_secs_f64() * 1e6);
            k_match.push((t2 - t1).as_secs_f64() * 1e6);
            k_lookup.push((t3 - t2).as_secs_f64() * 1e6);
            k_total.push((t3 - t0).as_secs_f64() * 1e6);
        }
    }
    let mut k_total_sorted = k_total.clone();
    k_total_sorted.sort_by(f64::total_cmp);

    let mut decode_sorted = decode_all.clone();
    decode_sorted.sort_by(f64::total_cmp);
    let mut by_px_json = serde_json::Map::new();
    for (k, mut v) in by_px {
        by_px_json.insert(k, summary(&mut v));
    }
    let report = lib.report();
    let mut all_us_sorted = all_us.clone();
    all_us_sorted.sort_by(f64::total_cmp);
    let out = json!({
        "kind": "snow-globe.tier0-bench",
        "version": 2,
        "crate_version": env!("CARGO_PKG_VERSION"),
        "profile": if cfg!(debug_assertions) { "debug" } else { "release" },
        "os": std::env::consts::OS,
        "arch": std::env::consts::ARCH,
        "cpus": std::thread::available_parallelism().map_or(0, std::num::NonZero::get),
        "now": fmt_utc(&b.now),
        "data_dir": dir.display().to_string(),
        "index": {
            "cold_runs_ms": cold_runs.iter().map(|x| round3(*x)).collect::<Vec<_>>(),
            "cold_last_breakdown_ms": cold_last,
            "cache_write_build": write_build,
            "warm_runs_ms": warm_runs.iter().map(|x| round3(*x)).collect::<Vec<_>>(),
            "warm_statuses": warm_status,
            "warm_last_breakdown_ms": warm_last,
            "cache_file_bytes": cache_bytes,
        },
        "library": {
            "sidecars": report.sidecars,
            "entries": report.entries,
            "split_version": report.split_version,
            "split_file": rules.split_file,
            "not_available_at_now": lib
                .entries()
                .iter()
                .filter(|e| e.taken_utc < b.now && e.available_unix_ms > b.now.timestamp_millis())
                .count(),
            "excluded": report.excluded,
            "unknown_folders": report.unknown_folders,
            "paths_refused": report.paths_refused,
            "viewpoints": report.viewpoints,
        },
        "match": {
            "queries": b.queries,
            "days": b.days,
            "seed": b.seed,
            "latency_us": summary(&mut all_us),
            "best_distance": summary(&mut all_dist),
            "per_viewpoint": match_vp,
        },
        "prefetch": {
            "horizon_h": PREFETCH_HORIZON_H,
            "step_min": b.step_min,
            "per_viewpoint": prefetch_vp,
        },
        "decode": {
            "per_frame_total_ms": summary(&mut decode_all),
            "per_frame_path_check_ms": summary(&mut check_all),
            "per_frame_read_ms": summary(&mut read_all),
            "per_frame_decode_and_resize_ms": summary(&mut decode_only),
            "by_source_px": by_px_json,
            "failures": failures,
        },
        "keyup_core_path": {
            "note": "parse, match and the decoded-set lookup only: the swap (hand-over to the \
                     webview, texture upload, paint) is the overlay's and is not measured here",
            "utterances": KEYUP_UTTERANCES.len(),
            "rounds": b.keyup_rounds.max(1),
            "parse_us": summary(&mut k_parse),
            "match_us": summary(&mut k_match),
            "decoded_set_lookup_us": summary(&mut k_lookup),
            "total_us": summary(&mut k_total),
            "decoded_set_hits": k_hits,
            "decoded_set_misses_decoded_on_the_spot": k_misses,
            "no_frame": k_no_frame,
            "unparsed": k_unparsed,
            "unparsed_utterances": k_unparsed_list,
        },
        "hit_rate": {
            "queries_per_viewpoint": b.hit_queries,
            "overall": round3(hits_all as f64 / asked_all.max(1) as f64),
            "per_viewpoint": hit_vp,
            "decoded_set_lookup_us": summary(&mut lookup_us),
        },
    });
    let mut table = String::new();
    table.push_str(&format!(
        "tier0 bench at {} on {} ({} build)\n",
        fmt_utc(&b.now),
        dir.display(),
        if cfg!(debug_assertions) {
            "debug"
        } else {
            "release"
        }
    ));
    table.push_str(&format!(
        "index: cold {:?} ms; warm {:?} ms ({}); {} entries from {} sidecars; split {}\n",
        cold_runs.iter().map(|x| round3(*x)).collect::<Vec<_>>(),
        warm_runs.iter().map(|x| round3(*x)).collect::<Vec<_>>(),
        warm_status.join(", "),
        report.entries,
        report.sidecars,
        rules.split_file
    ));
    table.push_str(&format!(
        "{:<28} {:>7} {:>6} {:>7} {:>9}\n",
        "viewpoint", "entries", "live", "archive", "excluded"
    ));
    for (vp, r) in &report.viewpoints {
        let ex: usize = r.excluded.values().sum();
        table.push_str(&format!(
            "{vp:<28} {:>7} {:>6} {:>7} {:>9}\n",
            r.entries, r.live, r.archive, ex
        ));
    }
    table.push_str(&format!(
        "match: {} queries, p50 {:.1} us, p95 {:.1} us, max {:.1} us\n",
        all_us_sorted.len(),
        pct(&all_us_sorted, 0.5),
        pct(&all_us_sorted, 0.95),
        all_us_sorted.last().copied().unwrap_or(f64::NAN)
    ));
    table.push_str(&format!(
        "key-up core path (parse, match, decoded-set lookup): {} asks, p50 {:.1} us, p95 {:.1} us; {} lookups hit, {} decoded on the spot\n",
        k_total_sorted.len(),
        pct(&k_total_sorted, 0.5),
        pct(&k_total_sorted, 0.95),
        k_hits,
        k_misses
    ));
    table.push_str(&format!(
        "decode: {} frames, p50 {:.3} ms, p95 {:.3} ms per frame; 48 h hit rate {:.3}\n",
        decode_sorted.len(),
        pct(&decode_sorted, 0.5),
        pct(&decode_sorted, 0.95),
        hits_all as f64 / asked_all.max(1) as f64
    ));
    Ok((out, table))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn usage_errors() {
        assert_eq!(run(&args(&[])).code, EXIT_USAGE);
        assert_eq!(run(&args(&["nope"])).code, EXIT_USAGE);
        assert_eq!(run(&args(&["index", "--bogus"])).code, EXIT_USAGE);
        assert_eq!(run(&args(&["bench", "--queries", "x"])).code, EXIT_USAGE);
        assert_eq!(run(&args(&["--help"])).code, 0);
    }

    #[test]
    fn percentiles_are_nearest_rank() {
        let v: Vec<f64> = (1..=100).map(f64::from).collect();
        assert_eq!(pct(&v, 0.5), 50.0);
        assert_eq!(pct(&v, 0.95), 95.0);
        assert_eq!(pct(&v, 1.0), 100.0);
        assert!(pct(&[], 0.5).is_nan());
    }

    #[test]
    fn the_rng_is_reproducible() {
        let mut a = SplitMix(1);
        let mut b = SplitMix(1);
        assert_eq!(a.next_u64(), b.next_u64());
        assert!(a.below(10) < 10);
    }
}
