//! Parse-latency micro-benchmark (PRD §10.1 budget: "parse 5" ms of the Tier 0 chain).
//!
//! Times `Parser::parse` on the real embedded alias table, one utterance at a time, single
//! thread, with a fixed clock. It measures the parser only: no speech, no IPC, no rendering, and
//! a warm process (the first call, which builds the alias table, is reported separately).
//!
//! `cargo run --release --example parse_bench [rounds]`

use std::time::Instant;

use chrono::{DateTime, Utc};
use snow_globe_core::{ParseContext, Parser};

const UTTERANCES: &[(&str, &str)] = &[
    ("clock", "show me rainier at 9"),
    ("clock", "is rainier out tomorrow at 4?"),
    ("clock", "point reyes at half past four"),
    ("clock", "grand canyon friday at 9 pm"),
    ("clock", "rainier the day after tomorrow at quarter to five"),
    ("relative", "point reyes in two hours"),
    ("relative", "rainier in an hour and a half"),
    ("part_of_day", "yosemite tomorrow morning"),
    ("part_of_day", "is the mountain out tonight"),
    ("date", "rainier november 21st at 9"),
    ("date", "acadia on the 22nd"),
    ("solar", "point reyes at sunset friday"),
    ("solar", "rainier at sunrise"),
    ("solar", "grand canyon golden hour"),
    ("solar", "point reyes blue hour in the morning"),
    ("solar", "big bend at dusk tomorrow"),
    ("place_fuzzy", "yosemitee at noon"),
    ("place_phonetic", "rayneer at noon"),
    ("no_match", "golden gate at 9"),
    ("now", "point reyes now"),
];

fn pct(sorted: &[f64], p: f64) -> f64 {
    sorted[(((sorted.len() as f64) * p).ceil() as usize).clamp(1, sorted.len()) - 1]
}

fn main() {
    let rounds: usize = std::env::args()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(500);
    let now: DateTime<Utc> = DateTime::parse_from_rfc3339("2026-11-19T10:00:00-08:00")
        .expect("clock")
        .with_timezone(&Utc);
    let ctx = ParseContext { now, last: None };

    let t0 = Instant::now();
    let parser = Parser::embedded();
    let _ = parser.parse(UTTERANCES[0].1, &ctx);
    let first_us = t0.elapsed().as_secs_f64() * 1e6;

    let mut by_kind: std::collections::BTreeMap<&str, Vec<f64>> = Default::default();
    let mut all = Vec::new();
    for _ in 0..rounds {
        for (kind, u) in UTTERANCES {
            let t = Instant::now();
            let r = parser.parse(u, &ctx);
            let us = t.elapsed().as_secs_f64() * 1e6;
            std::hint::black_box(&r);
            by_kind.entry(kind).or_default().push(us);
            all.push(us);
        }
    }
    println!(
        "parse latency, release build, warm, {rounds} rounds x {} utterances (microseconds)",
        UTTERANCES.len()
    );
    println!("first call (builds the alias table from the embedded manifests): {first_us:.0} us");
    println!(
        "{:<15} {:>7} {:>9} {:>9} {:>9}",
        "kind", "n", "p50", "p95", "max"
    );
    for (kind, v) in by_kind
        .iter_mut()
        .chain(std::iter::once((&"all", &mut all)))
    {
        v.sort_by(f64::total_cmp);
        println!(
            "{:<15} {:>7} {:>9.1} {:>9.1} {:>9.1}",
            kind,
            v.len(),
            pct(v, 0.5),
            pct(v, 0.95),
            v[v.len() - 1]
        );
    }
}
