//! The trace fields the replay scorer reads (schema 2; PRD §10.5, §10.6, §14; bench/README.md
//! "Scoring"; D-067, D-068), through the public API only.
//!
//! The last test pins the JSON of one fully populated record to [`SCHEMA_TWO_EXAMPLE`], the copy
//! the scorer's tests read out of this file (bench/replay/trace_example.py), so the Rust writer and
//! the Python reader cannot drift apart unnoticed. That record is built by hand on a fake clock; it
//! is a schema example, not a captured trace and not a measurement.

use std::cell::Cell;

use chrono::{DateTime, Utc};
use snow_globe_core::lifecycle::{SceneHash, Tier};
use snow_globe_core::places::PlaceTable;
use snow_globe_core::query::ParsePath;
use snow_globe_core::trace::{
    CacheState, ContextPlace, DisplayState, EngineKind, KnownPlace, Levers, Mark, ModelRetention,
    NetworkInterface, Outcome, PaintSource, PowerSource, QueryTrace, ReplayIdError, ReplayTrial,
    SpanName, SpeechSetup, TRACE_SCHEMA, ThermalState, TraceClock, TraceRecord, TraceState,
    TraceWriter,
};
use snow_globe_core::{LastPlace, Parser, Session};

/// A clock that advances 1 ms per reading; its wall clock is 2026-11-19T18:00:00Z plus the reading.
struct FakeClock {
    ns: Cell<u64>,
}

impl FakeClock {
    fn new() -> FakeClock {
        FakeClock { ns: Cell::new(0) }
    }
}

impl TraceClock for FakeClock {
    fn mono_ns(&self) -> u64 {
        let v = self.ns.get() + 1_000_000;
        self.ns.set(v);
        v
    }
    fn wall(&self) -> DateTime<Utc> {
        let base = DateTime::parse_from_rfc3339("2026-11-19T18:00:00Z").expect("time");
        base.with_timezone(&Utc) + chrono::TimeDelta::nanoseconds(self.ns.get() as i64)
    }
}

fn json(record: &TraceRecord) -> serde_json::Value {
    let mut w = TraceWriter::new(Vec::new());
    w.write(record).expect("write");
    serde_json::from_slice(&w.into_inner()).expect("json")
}

fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2026-11-19T12:00:00-06:00")
        .expect("time")
        .with_timezone(&Utc)
}

#[test]
fn schema_is_two() {
    assert_eq!(TRACE_SCHEMA, 2);
    let clock = FakeClock::new();
    assert_eq!(
        json(&QueryTrace::new(&clock, 0).finish(Outcome::Empty))["schema"],
        2
    );
}

#[test]
fn context_is_the_parsers_last_place_as_manifest_ids() {
    let parser = Parser::embedded();
    let table = parser.places();
    let clock = FakeClock::new();
    let mut session = Session::new(parser);

    // The first query after a launch: the parser has no last place.
    let mut first = QueryTrace::new(&clock, 1);
    first.set_context(session.last(), table);
    session.ask("Rainier sunset", now()).expect("resolves");
    let first = json(&first.finish(Outcome::Painted));
    assert_eq!(first["context"], serde_json::json!({"kind": "empty"}));

    // The follow-up is parsed at the place the session kept, and the trace says which.
    let mut follow = QueryTrace::new(&clock, 2);
    follow.set_context(session.last(), table);
    let q = session.ask("And at sunrise?", now()).expect("resolves");
    assert_eq!(q.viewpoint_id, "rainier.paradise-mountain");
    assert_eq!(
        json(&follow.finish(Outcome::Painted))["context"],
        serde_json::json!({
            "kind": "place",
            "place_id": "rainier",
            "viewpoint_id": "rainier.paradise-mountain"
        })
    );

    // A miss leaves the last place as it was (Session::ask), and so does the trace's context.
    let mut after_miss = QueryTrace::new(&clock, 3);
    session.ask("Golden Gate at 9", now()).expect_err("a miss");
    after_miss.set_context(session.last(), table);
    assert_eq!(
        json(&after_miss.finish(Outcome::NoMatch))["context"]["place_id"],
        "rainier"
    );

    // Not set: recorded as such, never as empty.
    let unset = json(&QueryTrace::new(&clock, 4).finish(Outcome::Error));
    assert_eq!(
        unset["context"],
        serde_json::json!({"kind": "not_recorded"})
    );
}

#[test]
fn a_context_the_place_table_does_not_know_is_unlisted() {
    let table = PlaceTable::embedded();
    for (place_id, viewpoint_id) in [
        ("rainier", "rainier.elsewhere"),
        ("point-reyes", "rainier.paradise-mountain"),
        ("is rainier out", "rainier.paradise-mountain"),
    ] {
        let last = LastPlace {
            place_id: place_id.to_string(),
            viewpoint_id: viewpoint_id.to_string(),
        };
        assert_eq!(
            ContextPlace::of(Some(&last), table),
            ContextPlace::Unlisted,
            "{place_id} / {viewpoint_id}"
        );
        assert!(KnownPlace::new(table, place_id, viewpoint_id).is_none());
    }
    let known = KnownPlace::new(table, "acadia", "acadia.mcfarland").expect("a manifest pair");
    assert_eq!(
        (known.place_id(), known.viewpoint_id()),
        ("acadia", "acadia.mcfarland")
    );
    let clock = FakeClock::new();
    let mut t = QueryTrace::new(&clock, 1);
    t.set_context(
        Some(&LastPlace {
            place_id: "golden gate".to_string(),
            viewpoint_id: "golden gate".to_string(),
        }),
        table,
    );
    let line = json(&t.finish(Outcome::NoMatch));
    assert_eq!(line["context"], serde_json::json!({"kind": "unlisted"}));
    assert!(!line.to_string().contains("golden"), "{line}");
}

#[test]
fn display_state_defaults_to_unknown_and_is_recorded() {
    let clock = FakeClock::new();
    assert_eq!(
        json(&QueryTrace::new(&clock, 1).finish(Outcome::Empty))["state"]["display"],
        "unknown"
    );
    for (display, name) in [(DisplayState::On, "on"), (DisplayState::Asleep, "asleep")] {
        let mut t = QueryTrace::new(&clock, 2);
        t.set_state(TraceState {
            display,
            ..TraceState::default()
        });
        assert_eq!(json(&t.finish(Outcome::Painted))["state"]["display"], name);
    }
}

#[test]
fn speech_setup_is_logged_with_every_query() {
    let clock = FakeClock::new();
    let unset = json(&QueryTrace::new(&clock, 1).finish(Outcome::Empty));
    assert_eq!(
        unset["speech"],
        serde_json::json!({
            "engine": "unknown",
            "silence_pad_ms": null,
            "model_retention": "unknown",
            "warm_up": null
        })
    );
    let mut t = QueryTrace::new(&clock, 2);
    t.set_speech(SpeechSetup {
        engine: EngineKind::Parakeet,
        silence_pad_ms: Some(120),
        model_retention: ModelRetention::ProcessLifetime,
        warm_up: Some(false),
    });
    assert_eq!(
        json(&t.finish(Outcome::Painted))["speech"],
        serde_json::json!({
            "engine": "parakeet",
            "silence_pad_ms": 120,
            "model_retention": "process_lifetime",
            "warm_up": false
        })
    );
}

#[test]
fn levers_are_null_until_recorded() {
    let clock = FakeClock::new();
    assert_eq!(
        json(&QueryTrace::new(&clock, 1).finish(Outcome::Empty))["levers"],
        serde_json::json!({"pre_render": null, "partial_paint": null, "paint_capture": null})
    );
    let mut t = QueryTrace::new(&clock, 2);
    t.set_levers(Levers {
        pre_render: Some(false),
        partial_paint: Some(true),
        paint_capture: Some(true),
    });
    assert_eq!(
        json(&t.finish(Outcome::Painted))["levers"],
        serde_json::json!({"pre_render": false, "partial_paint": true, "paint_capture": true})
    );
}

#[test]
fn replay_ids_have_a_fixed_form() {
    for utterance in ["d01", "d30", "t001", "t100", "20261124T031500Z"] {
        let r = ReplayTrial::new("3b4256f1cf51", utterance, 1, 1, 1).expect(utterance);
        assert_eq!(r.utterance(), utterance);
    }
    let r = ReplayTrial::new("3b4256f1cf51", "t005", 2, 3, 41).expect("ids");
    assert_eq!(
        (r.script_hash(), r.take(), r.session(), r.trial()),
        ("3b4256f1cf51", 2, 3, 41)
    );
    for hash in [
        "3B4256F1CF51",
        "3b4256f1cf5",
        "3b4256f1cf512",
        "3b4256f1cf5g",
        "",
    ] {
        assert_eq!(
            ReplayTrial::new(hash, "t001", 1, 1, 1),
            Err(ReplayIdError::ScriptHash),
            "{hash}"
        );
    }
    for utterance in [
        "t1",
        "t0001",
        "d001",
        "x001",
        "T001",
        "t001 ",
        "rainier at nine",
        "20261124T031500",
        "2026-11-24T03:15:00Z",
        "",
    ] {
        let err = ReplayTrial::new("3b4256f1cf51", utterance, 1, 1, 1).expect_err(utterance);
        assert_eq!(err, ReplayIdError::Utterance, "{utterance:?}");
        if !utterance.is_empty() {
            assert!(
                !err.to_string().contains(utterance),
                "the rejected id is not echoed"
            );
        }
    }
    assert_eq!(
        ReplayTrial::new("3b4256f1cf51", "t001", 0, 1, 1),
        Err(ReplayIdError::Take)
    );
    let clock = FakeClock::new();
    let mut t = QueryTrace::new(&clock, 9);
    t.set_replay(r);
    assert_eq!(
        json(&t.finish(Outcome::Painted))["replay"],
        serde_json::json!({"script_hash": "3b4256f1cf51", "utterance": "t005", "take": 2, "session": 3, "trial": 41})
    );
}

#[test]
fn paints_are_judged_against_the_final_parse() {
    let clock = FakeClock::new();
    let (a, b) = (SceneHash::of_parts(&[b"a"]), SceneHash::of_parts(&[b"b"]));

    // A stable partial paints scene A; the final parse resolves to scene B.
    let mut t = QueryTrace::new(&clock, 1);
    t.paint(Tier::Tier0, PaintSource::Partial, a, 5_000_000);
    t.resolve_paints(Some(b));
    t.paint(Tier::Tier0, PaintSource::Final, b, 9_000_000);
    t.paint(Tier::Tier1, PaintSource::Final, b, 12_000_000);
    let r = t.finish(Outcome::Painted);
    let got: Vec<_> = r
        .paints
        .iter()
        .map(|p| (p.tier, p.source, p.scene, p.matches_final, p.mono_ns))
        .collect();
    assert_eq!(
        got,
        vec![
            (Tier::Tier0, PaintSource::Partial, 0, Some(false), 5_000_000),
            (Tier::Tier0, PaintSource::Final, 1, Some(true), 9_000_000),
            (Tier::Tier1, PaintSource::Final, 1, Some(true), 12_000_000),
        ]
    );
    // The tier marks keep the first paint of each tier, wrong or right.
    assert_eq!(r.marks[&Mark::Tier0Paint].mono_ns, 5_000_000);
    assert_eq!(r.marks[&Mark::Tier1Paint].mono_ns, 12_000_000);
    let v = json(&r);
    assert_eq!(v["paints"][0]["source"], "partial");
    assert_eq!(v["paints"][0]["tier"], "tier0");
    assert_eq!(v["paints"][0]["matches_final"], false);

    // Superseded before the final parse: the paint cannot be judged.
    let mut cancelled = QueryTrace::new(&clock, 2);
    cancelled.paint(Tier::Tier0, PaintSource::Partial, a, 1);
    let v = json(&cancelled.finish(Outcome::Cancelled));
    assert_eq!(v["paints"][0]["matches_final"], serde_json::Value::Null);

    // The final parse resolved to no scene (a miss): every paint was wrong.
    let mut miss = QueryTrace::new(&clock, 3);
    miss.paint(Tier::Tier0, PaintSource::Partial, a, 1);
    miss.resolve_paints(None);
    assert_eq!(
        miss.finish(Outcome::NoMatch).paints[0].matches_final,
        Some(false)
    );

    // Paints recorded out of order (frames read after the fact): the mark is the earliest.
    let mut late = QueryTrace::new(&clock, 5);
    late.paint(Tier::Tier1, PaintSource::Final, b, 8_000_000);
    late.paint(Tier::Tier1, PaintSource::Final, b, 6_000_000);
    late.paint(Tier::Tier1, PaintSource::Final, b, 7_000_000);
    let r = late.finish(Outcome::Painted);
    assert_eq!(r.marks[&Mark::Tier1Paint].mono_ns, 6_000_000);
    assert_eq!(r.paints.len(), 3);

    // A stamp from mark() (say a webview callback, 1-3 vsyncs before compositing) is never the
    // paint time: the first recorded paint replaces it, even when it is later, and a stamp after
    // a paint is ignored (PRD §10.4).
    let fresh = FakeClock::new();
    let mut early = QueryTrace::new(&fresh, 6);
    let callback = early.mark(Mark::Tier0Paint);
    assert_eq!(callback.mono_ns, 1_000_000);
    early.paint(Tier::Tier0, PaintSource::Final, a, 34_000_000);
    early.mark(Mark::Tier0Paint);
    early.mark_at(Mark::Tier0Paint, 2_000_000);
    early.paint(Tier::Tier0, PaintSource::Final, a, 40_000_000);
    let r = early.finish(Outcome::Painted);
    assert_eq!(r.marks[&Mark::Tier0Paint].mono_ns, 34_000_000);
    assert_eq!(r.paints.len(), 2);

    // The scene hash itself is never written, only its index.
    let mut t = QueryTrace::new(&clock, 4);
    t.paint(Tier::Tier0, PaintSource::Final, a, 1);
    let line = json(&t.finish(Outcome::Painted)).to_string();
    assert!(!line.contains(&a.0.to_string()), "{line}");
}

#[test]
fn speech_event_lag_counts_from_the_first_audio_sample() {
    let clock = FakeClock::new();
    let mut t = QueryTrace::new(&clock, 1);
    assert!(
        !t.speech_event_lag(SpanName::PartialLag, 0, 0.5, 900_000_000),
        "no audio origin yet"
    );
    t.mark_at(Mark::AudioFirstSample, 100_000_000);
    assert!(t.speech_event_lag(SpanName::PartialLag, 0, 0.5, 900_000_000));
    assert!(t.speech_event_lag(SpanName::StableLag, 0, 0.5, 950_000_000));
    // Arriving before the audio it covers ends (clock skew) is recorded as no lag.
    assert!(t.speech_event_lag(SpanName::PartialLag, 1, 2.0, 1_000_000_000));
    for (name, audio_end_s) in [
        (SpanName::Encode, 0.5),
        (SpanName::PartialLag, -0.1),
        (SpanName::PartialLag, f64::NAN),
        (SpanName::PartialLag, f64::INFINITY),
    ] {
        assert!(!t.speech_event_lag(name, 2, audio_end_s, 1_000_000_000));
    }
    let r = t.finish(Outcome::Painted);
    let spans: Vec<_> = r
        .spans
        .iter()
        .map(|s| (s.name, s.seq, s.start_ns, s.dur_ns))
        .collect();
    assert_eq!(
        spans,
        vec![
            (SpanName::PartialLag, Some(0), 600_000_000, 300_000_000),
            (SpanName::StableLag, Some(0), 600_000_000, 350_000_000),
            (SpanName::PartialLag, Some(1), 2_100_000_000, 0),
        ]
    );
}

/// The record pinned by the scorer's copy: a follow-up replay trial with every schema-2 field.
fn example_record(clock: &FakeClock) -> TraceRecord {
    let parser = Parser::embedded();
    let ms = |m: u64| m * 1_000_000;
    let mut t = QueryTrace::new(clock, 31);
    t.mark_at(Mark::KeyDown, ms(1_000));
    t.mark_at(Mark::AudioFirstSample, ms(1_040));
    t.span(SpanName::OsEventToHandler, ms(1_000), ms(1_000) + 400_000);
    t.span(SpanName::PanelReveal, ms(1_001), ms(1_009));
    t.seq_span(SpanName::AudioBufferDelivery, ms(1_040), ms(1_041), 0);
    t.seq_span(SpanName::AudioBufferDelivery, ms(1_050), ms(1_051), 1);
    t.mark_at(Mark::SpeechFirstPartial, ms(1_420));
    assert!(t.speech_event_lag(SpanName::PartialLag, 0, 0.30, ms(1_420)));
    t.mark_at(Mark::PlaceMatched, ms(1_430));
    assert!(t.speech_event_lag(SpanName::PartialLag, 1, 0.80, ms(1_960)));
    assert!(t.speech_event_lag(SpanName::StableLag, 0, 0.80, ms(1_990)));
    t.mark_at(Mark::AssetsReady, ms(1_700));
    t.mark_at(Mark::KeyUp, ms(2_100));
    let (sunrise_wrong, sunrise) = (SceneHash::of_parts(&[b"x"]), SceneHash::of_parts(&[b"y"]));
    t.paint(Tier::Tier0, PaintSource::Partial, sunrise_wrong, ms(2_150));
    t.mark_at(Mark::SpeechFinal, ms(2_290));
    t.mark_at(Mark::Parsed, ms(2_291));
    t.resolve_paints(Some(sunrise));
    t.mark_at(Mark::ForecastReady, ms(2_300));
    t.paint(Tier::Tier0, PaintSource::Final, sunrise, ms(2_320));
    t.paint(Tier::Tier1, PaintSource::Final, sunrise, ms(2_480));
    t.set_state(TraceState {
        idle_duration_ms: Some(1_860_000),
        cache_state: CacheState::Idle,
        thermal_state: ThermalState::Nominal,
        power_source: PowerSource::Ac,
        network_interface: NetworkInterface::Wifi,
        display: DisplayState::Asleep,
    });
    let last = LastPlace {
        place_id: "rainier".to_string(),
        viewpoint_id: "rainier.paradise-mountain".to_string(),
    };
    t.set_context(Some(&last), parser.places());
    t.set_speech(SpeechSetup {
        engine: EngineKind::DictationTranscriber,
        silence_pad_ms: Some(0),
        model_retention: ModelRetention::Lingering,
        warm_up: Some(true),
    });
    t.set_levers(Levers {
        pre_render: Some(false),
        partial_paint: Some(true),
        paint_capture: Some(true),
    });
    t.set_replay(ReplayTrial::new("3b4256f1cf51", "t032", 1, 2, 31).expect("ids"));
    t.set_transcript("And at sunrise?", 3);
    t.set_path(ParsePath::FastPath);
    t.finish(Outcome::Painted)
}

/// [`example_record`] as JSON: the scorer's copy. bench/replay/trace_example.py reads the raw
/// string below out of this file, so keep its `const` line and its `r#"` delimiters as they are.
const SCHEMA_TWO_EXAMPLE: &str = r#"
{
  "schema": 2,
  "query_gen": 31,
  "outcome": "painted",
  "path": "fast_path",
  "context": {
    "kind": "place",
    "place_id": "rainier",
    "viewpoint_id": "rainier.paradise-mountain"
  },
  "speech": {
    "engine": "dictation_transcriber",
    "silence_pad_ms": 0,
    "model_retention": "lingering",
    "warm_up": true
  },
  "levers": {"pre_render": false, "partial_paint": true, "paint_capture": true},
  "marks": {
    "key_down": {"mono_ns": 1000000000, "wall": "2026-11-19T18:00:01.000000Z"},
    "audio_first_sample": {"mono_ns": 1040000000, "wall": "2026-11-19T18:00:01.040000Z"},
    "speech_first_partial": {"mono_ns": 1420000000, "wall": "2026-11-19T18:00:01.420000Z"},
    "place_matched": {"mono_ns": 1430000000, "wall": "2026-11-19T18:00:01.430000Z"},
    "assets_ready": {"mono_ns": 1700000000, "wall": "2026-11-19T18:00:01.700000Z"},
    "key_up": {"mono_ns": 2100000000, "wall": "2026-11-19T18:00:02.100000Z"},
    "speech_final": {"mono_ns": 2290000000, "wall": "2026-11-19T18:00:02.290000Z"},
    "parsed": {"mono_ns": 2291000000, "wall": "2026-11-19T18:00:02.291000Z"},
    "forecast_ready": {"mono_ns": 2300000000, "wall": "2026-11-19T18:00:02.300000Z"},
    "tier0_paint": {"mono_ns": 2150000000, "wall": "2026-11-19T18:00:02.150000Z"},
    "tier1_paint": {"mono_ns": 2480000000, "wall": "2026-11-19T18:00:02.480000Z"}
  },
  "spans": [
    {"name": "os_event_to_handler", "start_ns": 1000000000, "end_ns": 1000400000, "dur_ns": 400000},
    {"name": "panel_reveal", "start_ns": 1001000000, "end_ns": 1009000000, "dur_ns": 8000000},
    {"name": "audio_buffer_delivery", "start_ns": 1040000000, "end_ns": 1041000000, "dur_ns": 1000000, "seq": 0},
    {"name": "audio_buffer_delivery", "start_ns": 1050000000, "end_ns": 1051000000, "dur_ns": 1000000, "seq": 1},
    {"name": "partial_lag", "start_ns": 1340000000, "end_ns": 1420000000, "dur_ns": 80000000, "seq": 0},
    {"name": "partial_lag", "start_ns": 1840000000, "end_ns": 1960000000, "dur_ns": 120000000, "seq": 1},
    {"name": "stable_lag", "start_ns": 1840000000, "end_ns": 1990000000, "dur_ns": 150000000, "seq": 0}
  ],
  "paints": [
    {"tier": "tier0", "source": "partial", "scene": 0, "matches_final": false, "mono_ns": 2150000000},
    {"tier": "tier0", "source": "final", "scene": 1, "matches_final": true, "mono_ns": 2320000000},
    {"tier": "tier1", "source": "final", "scene": 1, "matches_final": true, "mono_ns": 2480000000}
  ],
  "state": {
    "idle_duration_ms": 1860000,
    "cache_state": "idle",
    "thermal_state": "nominal",
    "power_source": "ac",
    "network_interface": "wifi",
    "display": "asleep"
  },
  "lengths": {"transcript_chars": 15, "transcript_words": 3, "partials": 3},
  "replay": {
    "script_hash": "3b4256f1cf51",
    "utterance": "t032",
    "take": 1,
    "session": 2,
    "trial": 31
  }
}
"#;

#[test]
fn schema_two_example_matches_the_scorers_copy() {
    let clock = FakeClock::new();
    let mut w = TraceWriter::new(Vec::new());
    w.write(&example_record(&clock)).expect("write");
    let line = String::from_utf8(w.into_inner()).expect("utf8");
    assert!(line.ends_with('\n') && line.matches('\n').count() == 1);
    let written: serde_json::Value = serde_json::from_str(&line).expect("json");
    let pinned: serde_json::Value = serde_json::from_str(SCHEMA_TWO_EXAMPLE).expect("json");
    assert_eq!(
        written, pinned,
        "the trace JSON changed: update SCHEMA_TWO_EXAMPLE and the scorer's reader \
         (bench/replay/traces.py) together. The writer now gives:\n{line}"
    );
}
