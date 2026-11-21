//! Property tests (PRD §7.6, §13.1): over random clocks (late Oct 2026 – early Apr 2027, so both
//! DST changes are crossed), random places and random phrasings:
//!
//! 1. a resolved time is never in the past unless the ask was "now";
//! 2. horizon flags are consistent with the lead time, and nothing beyond 7 days is served;
//! 3. the canonical form is stable: canonical(canonical(u)) = canonical(u), and u and
//!    canonical(u) parse to the same result, both for phrasings and for word salad drawn from the
//!    grammar's own vocabulary (where dropping a filler can join two time words);
//! 4. the machine zone never changes the answer: "now" given as a wall time in a random machine
//!    zone (with `--machine-tz`) and as the same instant in UTC print the same bytes;
//! 5. the query JSON round-trips byte for byte.

use chrono::{DateTime, Duration, TimeZone, Utc};
use chrono_tz::Tz;
use proptest::prelude::*;
use snow_globe_core::parser::RangeReason;
use snow_globe_core::query::ParsePath;
use snow_globe_core::resolve::{Flag, Horizon, TimeKind};
use snow_globe_core::{ParseContext, ParseError, Parser, Query};

const PLACES: &[&str] = &[
    "rainier",
    "mount rainier",
    "the mountain",
    "paradise",
    "point reyes",
    "pt reyes",
    "point rays",
    "grand canyon",
    "yavapai point",
    "grand teton",
    "the tetons",
    "yosemite",
    "turtleback dome",
    "acadia",
    "mcfarland hill",
    "smokies",
    "look rock",
    "big bend",
    "panther junction",
    "grand canyon from yavapai",
];

const WEEKDAYS: &[&str] = &[
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
];
const PARTS: &[&str] = &["morning", "afternoon", "evening", "night"];
const MONTHS: &[&str] = &[
    "october", "november", "december", "january", "february", "march", "april",
];
const SOLAR: &[&str] = &[
    "sunrise",
    "sunset",
    "dawn",
    "dusk",
    "golden hour",
    "blue hour",
    "morning golden hour",
    "blue hour in the morning",
];
const PREFIXES: &[&str] = &[
    "",
    "show me ",
    "is ",
    "what about ",
    "how does ",
    "hey lookout show me ",
    "hey snow globe show me ",
    "um ",
];
const SUFFIXES: &[&str] = &["", "?", " please", " look", " out?"];

fn time_phrase() -> impl Strategy<Value = String> {
    let h12 = 1u32..=12;
    let h24 = 0u32..=23;
    let mm = prop::sample::select(vec![0u32, 5, 15, 30, 45, 59]);
    prop_oneof![
        Just(String::new()),
        Just("now".to_string()),
        h12.clone().prop_map(|h| format!("at {h}")),
        (
            h12.clone(),
            prop::sample::select(vec!["am", "pm", "a.m.", "p.m."])
        )
            .prop_map(|(h, m)| format!("at {h} {m}")),
        (h12.clone(), mm.clone()).prop_map(|(h, m)| format!("at {h}:{m:02}")),
        (h24, mm).prop_map(|(h, m)| format!("at {h:02}:{m:02}")),
        h12.clone().prop_map(|h| format!("tomorrow at {h}")),
        (prop::sample::select(WEEKDAYS), h12.clone()).prop_map(|(d, h)| format!("{d} at {h}")),
        (
            prop::sample::select(WEEKDAYS),
            prop::sample::select(vec!["", "this ", "next ", "on "])
        )
            .prop_map(|(d, p)| format!("{p}{d}")),
        (1u32..=72).prop_map(|n| format!("in {n} hours")),
        (1u32..=600).prop_map(|n| format!("in {n} minutes")),
        prop::sample::select(PARTS).prop_map(|p| p.to_string()),
        (
            prop::sample::select(PARTS),
            prop::sample::select(vec!["this ", "tomorrow ", "friday "])
        )
            .prop_map(|(p, d)| format!("{d}{p}")),
        Just("tonight".to_string()),
        h12.clone().prop_map(|h| format!("tonight at {h}")),
        prop::sample::select(SOLAR).prop_map(|s| s.to_string()),
        (
            prop::sample::select(SOLAR),
            prop::sample::select(vec!["tomorrow", "today", "saturday", "tonight"])
        )
            .prop_map(|(s, d)| format!("{s} {d}")),
        prop::sample::select(vec![
            "noon",
            "midnight",
            "at noon",
            "at midnight",
            "midnight tonight"
        ])
        .prop_map(str::to_string),
        (prop::sample::select(MONTHS), 1u32..=31).prop_map(|(m, d)| format!("{m} {d}")),
        (1u32..=31).prop_map(|d| format!(
            "the {d}{}",
            match d {
                1 | 21 | 31 => "st",
                2 | 22 => "nd",
                3 | 23 => "rd",
                _ => "th",
            }
        )),
        h12.clone().prop_map(|h| format!("half past {h}")),
        h12.prop_map(|h| format!("quarter to {h}")),
        Just("in an hour and a half".to_string()),
        Just("yesterday".to_string()),
    ]
}

/// A random instant between 25 Oct 2026 and 5 Apr 2027 (both DST changes), whole seconds.
fn clock() -> impl Strategy<Value = DateTime<Utc>> {
    let start = Utc
        .with_ymd_and_hms(2026, 10, 25, 0, 0, 0)
        .single()
        .expect("start")
        .timestamp();
    let end = Utc
        .with_ymd_and_hms(2027, 4, 5, 0, 0, 0)
        .single()
        .expect("end")
        .timestamp();
    (start..end).prop_map(|s| DateTime::from_timestamp(s, 0).expect("in range"))
}

fn shout(s: &str, mask: u64) -> String {
    s.chars()
        .enumerate()
        .map(|(i, c)| {
            if mask >> (i % 64) & 1 == 1 {
                c.to_ascii_uppercase()
            } else {
                c
            }
        })
        .collect()
}

/// Utterances built from a place, a time phrase and filler, in either order, with random case.
fn utterance() -> impl Strategy<Value = String> {
    (
        prop::sample::select(PLACES),
        time_phrase(),
        prop::sample::select(PREFIXES),
        prop::sample::select(SUFFIXES),
        any::<bool>(),
        any::<u64>(),
    )
        .prop_map(|(place, time, pre, suf, time_first, mask)| {
            let body = if time_first {
                format!("{time} {place}")
            } else {
                format!("{place} {time}")
            };
            shout(&format!("{pre}{}{suf}", body.trim()), mask)
        })
}

/// Words the grammar, the fillers and the place names use, including the ones that join or split
/// time phrases ("and", "the", "to", "of", "from", "half", "past").
const SALAD: &[&str] = &[
    "rainier", "point", "reyes", "grand", "canyon", "teton", "the", "mountain", "from", "yavapai",
    "look", "rock", "beach", "at", "4", "30", "16:00", "and", "half", "past", "four", "quarter",
    "to", "five", "tomorrow", "tonight", "sunset", "golden", "hour", "in", "an", "two", "hours",
    "a", "minutes", "of", "november", "21st", "am", "pm", "noon", "midnight", "next", "friday",
    "sat", "oclock", "o", "oh", "show", "me", "is", "it", "what", "about", "like", "view", "or",
    "please", "now", "morning", "night", "this", "on", "day", "after", "for", "around", "by",
];

fn salad() -> impl Strategy<Value = String> {
    prop::collection::vec(prop::sample::select(SALAD), 1..9).prop_map(|w| w.join(" "))
}

/// Zones a machine might be in: Chicago (this machine), Los Angeles, Phoenix (no DST), Tokyo,
/// Kiritimati (UTC+14) and Pago Pago (UTC−11).
const MACHINE_ZONES: &[&str] = &[
    "America/Chicago",
    "America/Los_Angeles",
    "America/Phoenix",
    "Asia/Tokyo",
    "Pacific/Kiritimati",
    "Pacific/Pago_Pago",
];

fn parse(u: &str, now: DateTime<Utc>) -> Result<Query, ParseError> {
    Parser::embedded().parse(u, &ParseContext { now, last: None })
}

proptest! {
    // No failure-persistence files: they would be written into the repo. A failing case prints
    // its seed and minimal input instead.
    #![proptest_config(ProptestConfig { cases: 768, failure_persistence: None, ..ProptestConfig::default() })]

    #[test]
    fn resolved_time_is_never_in_the_past_unless_now(u in utterance(), now in clock()) {
        if let Ok(q) = parse(&u, now) {
            if q.time_source.kind == TimeKind::Now {
                prop_assert!(q.valid_time_utc <= now && now - q.valid_time_utc < Duration::seconds(1));
                prop_assert!(q.has_flag(Flag::Live));
            } else {
                prop_assert!(q.valid_time_utc >= now, "{u:?} at {now}: {} is in the past", q.valid_time_utc);
            }
        }
    }

    #[test]
    fn horizon_flags_are_consistent(u in utterance(), now in clock()) {
        match parse(&u, now) {
            Ok(q) => {
                let lead = q.valid_time_utc - now;
                prop_assert!(lead <= Duration::days(7), "{u:?}: lead {lead}");
                let gfs = lead > Duration::hours(48);
                prop_assert_eq!(q.horizon == Horizon::Gfs, gfs, "{:?} lead {}", u, lead);
                prop_assert_eq!(q.has_flag(Flag::GfsHorizon), gfs);
                prop_assert_eq!(q.path, ParsePath::FastPath);
            }
            Err(ParseError::OutOfRange { reason, requested, .. }) => {
                let t = DateTime::parse_from_rfc3339(&requested).expect("rfc3339").with_timezone(&Utc);
                match reason {
                    RangeReason::Past => prop_assert!(t < now, "{u:?}: {t} is not past"),
                    RangeReason::BeyondHorizon => prop_assert!(t - now > Duration::days(7), "{u:?}: {t} is not beyond 7 days"),
                }
            }
            Err(_) => {}
        }
    }

    #[test]
    fn canonical_form_is_stable(u in utterance(), now in clock()) {
        let p = Parser::embedded();
        let c1 = p.canonical(&u);
        let c2 = p.canonical(&c1);
        prop_assert_eq!(&c1, &c2, "canonical form of {:?} is not a fixed point", u);
        prop_assert_eq!(parse(&u, now), parse(&c1, now), "{:?} and its canonical form {:?} parse differently", u, c1);
    }

    #[test]
    fn machine_zone_does_not_change_the_answer(u in utterance(), now in clock(), zone in prop::sample::select(MACHINE_ZONES)) {
        // "now" as a wall time with no offset is read in --machine-tz; that zone must play no
        // other part. Instants in a DST repeat are skipped: their wall time names two instants.
        let tz: Tz = zone.parse().expect("zone");
        let wall = now.with_timezone(&tz).naive_local();
        prop_assume!(snow_globe_core::tzutil::localize(tz, wall).0.with_timezone(&Utc) == now);
        let run = |args: &[&str]| {
            let mut v = vec![u.clone()];
            v.extend(args.iter().map(|a| a.to_string()));
            snow_globe_core::cli::run(&v)
        };
        let local = run(&["--now", &wall.format("%Y-%m-%dT%H:%M:%S").to_string(), "--machine-tz", zone]);
        let utc = run(&["--now", &now.to_rfc3339(), "--machine-tz", "Asia/Tokyo"]);
        prop_assert_eq!(local.code, utc.code, "{} in {}", now, zone);
        prop_assert_eq!(local.stdout, utc.stdout);
    }

    #[test]
    fn canonical_form_of_word_salad_is_stable(u in salad(), now in clock()) {
        let p = Parser::embedded();
        let c1 = p.canonical(&u);
        prop_assert_eq!(p.canonical(&c1), c1.clone(), "canonical form of {:?} is not a fixed point", u);
        prop_assert_eq!(parse(&u, now), parse(&c1, now), "{:?} and its canonical form {:?} parse differently", u, c1);
    }

    #[test]
    fn parser_never_panics_on_arbitrary_text(s in "\\PC{0,80}", now in clock()) {
        let p = Parser::embedded();
        let _ = parse(&s, now);
        let c = p.canonical(&s);
        prop_assert_eq!(p.canonical(&c), c.clone(), "canonical form of {:?} is not a fixed point", s);
    }

    #[test]
    fn query_json_round_trips(u in utterance(), now in clock()) {
        if let Ok(q) = parse(&u, now) {
            let json = q.to_json();
            let back: Query = serde_json::from_str(&json).expect("parses back");
            prop_assert_eq!(&back, &q);
            prop_assert_eq!(back.to_json(), json);
        }
    }
}

#[test]
fn generator_reaches_every_outcome() {
    // Guard against a vacuous property run: the generator must produce successes, each kind of
    // out-of-range error, and both horizons.
    use proptest::strategy::ValueTree;
    use proptest::test_runner::TestRunner;
    let mut runner = TestRunner::deterministic();
    let (mut ok, mut gfs, mut past, mut beyond, mut now_kind) = (0, 0, 0, 0, 0);
    for _ in 0..2000 {
        let u = utterance().new_tree(&mut runner).expect("tree").current();
        let now = clock().new_tree(&mut runner).expect("tree").current();
        match parse(&u, now) {
            Ok(q) => {
                ok += 1;
                gfs += usize::from(q.horizon == Horizon::Gfs);
                now_kind += usize::from(q.time_source.kind == TimeKind::Now);
            }
            Err(ParseError::OutOfRange {
                reason: RangeReason::Past,
                ..
            }) => past += 1,
            Err(ParseError::OutOfRange {
                reason: RangeReason::BeyondHorizon,
                ..
            }) => beyond += 1,
            Err(_) => {}
        }
    }
    println!(
        "generator mix over 2000 draws: ok {ok}, gfs {gfs}, now {now_kind}, past {past}, beyond {beyond}"
    );
    assert!(ok > 1000 && gfs > 50 && past > 10 && beyond > 50 && now_kind > 50);
}
