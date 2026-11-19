//! Per-query latency trace: one JSON line per query (PRD §10.5).
//!
//! **Timestamps** ([`Mark`]): `key_down`, `speech_first_partial`, `place_matched`,
//! `assets_ready`, `key_up`, `speech_final`, `parsed`, `forecast_ready`, `tier0_paint`,
//! `tier1_paint`, `tier2_sent`, `tier2_first_byte`, `tier2_checked`, `tier2_paint`. Each stamp has
//! a monotonic reading in nanoseconds (`mono_ns`, from the clock's anchor) and the wall clock
//! (`wall`, RFC 3339 UTC with microseconds).
//!
//! **Spans** ([`SpanName`]): OS event → handler, audio buffer delivery, `stt_token`, DNS, TCP,
//! TLS and time to first byte (with a connection-reused flag), proxy-internal spans, provider
//! queue and inference, readback, encode and panel reveal.
//!
//! **State** ([`TraceState`]): idle duration, cache state, thermal state, power source, and the
//! network interface *type* only.
//!
//! **Privacy:** traces stay on the machine and never contain transcript text. The record has no
//! free-text field at all: every string in the output is an enum name or a timestamp, and the
//! transcript is represented only by lengths ([`Lengths`]). A test walks the JSON to prove it.
//!
//! Host-time alignment with ScreenCaptureKit (PRD §10.4) is TODO(M1): `mono_ns` here counts from
//! a process anchor taken with `std::time::Instant`.

use std::collections::BTreeMap;
use std::fs::OpenOptions;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use chrono::{DateTime, NaiveDate, SecondsFormat, Utc};
use serde::{Deserialize, Serialize, Serializer};

/// Version of the line format.
pub const TRACE_SCHEMA: u32 = 1;

/// The PRD §10.5 timestamps.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum Mark {
    /// Hotkey pressed.
    #[serde(rename = "key_down")]
    KeyDown,
    /// First partial transcript.
    #[serde(rename = "speech_first_partial")]
    SpeechFirstPartial,
    /// A partial named a known place (prefetch started).
    #[serde(rename = "place_matched")]
    PlaceMatched,
    /// Plate, forecast and thumbnails local.
    #[serde(rename = "assets_ready")]
    AssetsReady,
    /// Hotkey released (t0 of every latency budget).
    #[serde(rename = "key_up")]
    KeyUp,
    /// Final transcript.
    #[serde(rename = "speech_final")]
    SpeechFinal,
    /// Query parsed.
    #[serde(rename = "parsed")]
    Parsed,
    /// Forecast for the valid time loaded.
    #[serde(rename = "forecast_ready")]
    ForecastReady,
    /// Tier 0 composited.
    #[serde(rename = "tier0_paint")]
    Tier0Paint,
    /// Tier 1 composited.
    #[serde(rename = "tier1_paint")]
    Tier1Paint,
    /// Tier 2 request sent.
    #[serde(rename = "tier2_sent")]
    Tier2Sent,
    /// Tier 2 first response byte.
    #[serde(rename = "tier2_first_byte")]
    Tier2FirstByte,
    /// Tier 2 guardrail checks finished.
    #[serde(rename = "tier2_checked")]
    Tier2Checked,
    /// Tier 2 composited.
    #[serde(rename = "tier2_paint")]
    Tier2Paint,
}

impl Mark {
    /// Every mark, in PRD order.
    pub const ALL: [Mark; 14] = [
        Mark::KeyDown,
        Mark::SpeechFirstPartial,
        Mark::PlaceMatched,
        Mark::AssetsReady,
        Mark::KeyUp,
        Mark::SpeechFinal,
        Mark::Parsed,
        Mark::ForecastReady,
        Mark::Tier0Paint,
        Mark::Tier1Paint,
        Mark::Tier2Sent,
        Mark::Tier2FirstByte,
        Mark::Tier2Checked,
        Mark::Tier2Paint,
    ];
}

fn ser_wall<S: Serializer>(t: &DateTime<Utc>, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&t.to_rfc3339_opts(SecondsFormat::Micros, true))
}

/// A monotonic reading plus the wall clock.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Stamp {
    /// Nanoseconds since the clock's anchor (monotonic).
    pub mono_ns: u64,
    /// Wall clock, UTC.
    #[serde(serialize_with = "ser_wall")]
    pub wall: DateTime<Utc>,
}

/// The PRD §10.5 spans.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SpanName {
    /// OS hotkey event → our handler.
    OsEventToHandler,
    /// One audio buffer from the device to the speech engine.
    AudioBufferDelivery,
    /// Minting the short-lived speech token (cloud engine only).
    SttToken,
    /// DNS lookup.
    Dns,
    /// TCP connect.
    Tcp,
    /// TLS handshake.
    Tls,
    /// Request → first byte.
    Ttfb,
    /// Time inside the Tier 2 proxy.
    ProxyInternal,
    /// Provider-reported queue time.
    ProviderQueue,
    /// Provider-reported inference time.
    ProviderInference,
    /// GPU readback of the Tier 2 input.
    Readback,
    /// JPEG encode of the Tier 2 input.
    Encode,
    /// Panel reveal on key-down.
    PanelReveal,
}

/// A timed span.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Span {
    /// Which span.
    pub name: SpanName,
    /// Start, monotonic ns.
    pub start_ns: u64,
    /// End, monotonic ns.
    pub end_ns: u64,
    /// `end_ns - start_ns`.
    pub dur_ns: u64,
    /// Network spans: whether the connection was reused.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub connection_reused: Option<bool>,
    /// Repeated spans (audio buffers): their index.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub seq: Option<u32>,
}

/// Cache state when the query started (PRD §10.6 states).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CacheState {
    /// Not recorded.
    Unknown,
    /// ≥ 30 min since the last query (the headline state).
    Idle,
    /// < 60 s since the last query.
    Warm,
    /// Caches cleared.
    Cold,
    /// First query after the machine woke.
    AfterWake,
    /// First query after the app launched.
    AfterLaunch,
}

/// macOS thermal state (`ProcessInfo.thermalState`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ThermalState {
    /// Not recorded.
    Unknown,
    /// Nominal.
    Nominal,
    /// Fair.
    Fair,
    /// Serious.
    Serious,
    /// Critical.
    Critical,
}

/// Power source.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PowerSource {
    /// Not recorded.
    Unknown,
    /// Mains.
    Ac,
    /// Battery.
    Battery,
}

/// Network interface **type** (never its name or address).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NetworkInterface {
    /// Not recorded.
    Unknown,
    /// No network.
    None,
    /// Wi-Fi.
    Wifi,
    /// Wired.
    Ethernet,
    /// Cellular or tethered.
    Cellular,
    /// Anything else.
    Other,
}

/// Machine state for the query.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct TraceState {
    /// Milliseconds since the previous query ended, if known.
    pub idle_duration_ms: Option<u64>,
    /// Cache state.
    pub cache_state: CacheState,
    /// Thermal state.
    pub thermal_state: ThermalState,
    /// Power source.
    pub power_source: PowerSource,
    /// Network interface type.
    pub network_interface: NetworkInterface,
}

impl Default for TraceState {
    fn default() -> Self {
        TraceState {
            idle_duration_ms: None,
            cache_state: CacheState::Unknown,
            thermal_state: ThermalState::Unknown,
            power_source: PowerSource::Unknown,
            network_interface: NetworkInterface::Unknown,
        }
    }
}

/// Transcript lengths: the only trace of what was said.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize)]
pub struct Lengths {
    /// Characters in the final transcript.
    pub transcript_chars: u32,
    /// Words in the final transcript.
    pub transcript_words: u32,
    /// Partial transcripts received.
    pub partials: u32,
}

impl Lengths {
    /// Lengths of a transcript (the text itself is not kept).
    pub fn of(transcript: &str, partials: u32) -> Lengths {
        Lengths {
            transcript_chars: u32::try_from(transcript.chars().count()).unwrap_or(u32::MAX),
            transcript_words: u32::try_from(transcript.split_whitespace().count())
                .unwrap_or(u32::MAX),
            partials,
        }
    }
}

/// How the query ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    /// At least one tier painted.
    Painted,
    /// A newer key press superseded it.
    Cancelled,
    /// No place matched.
    NoMatch,
    /// Time out of range.
    OutOfRange,
    /// Sent to the fallback parser.
    Fallback,
    /// Nothing was said.
    Empty,
    /// Something failed.
    Error,
}

/// One trace line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct TraceRecord {
    /// [`TRACE_SCHEMA`].
    pub schema: u32,
    /// The query generation (PRD §10.3).
    pub query_gen: u64,
    /// How it ended.
    pub outcome: Outcome,
    /// Parser path, if parsing happened (`fast_path` or `fallback`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<crate::query::ParsePath>,
    /// Timestamps by mark.
    pub marks: BTreeMap<Mark, Stamp>,
    /// Spans, in the order recorded.
    pub spans: Vec<Span>,
    /// Machine state.
    pub state: TraceState,
    /// Transcript lengths.
    pub lengths: Lengths,
}

/// Source of monotonic and wall time.
pub trait TraceClock {
    /// Monotonic nanoseconds since this clock's anchor.
    fn mono_ns(&self) -> u64;
    /// Wall clock, UTC.
    fn wall(&self) -> DateTime<Utc>;
}

/// The real clock: `Instant` from an anchor, plus the system wall clock.
#[derive(Debug, Clone, Copy)]
pub struct SystemClock {
    anchor: Instant,
}

impl SystemClock {
    /// A clock anchored now.
    pub fn new() -> SystemClock {
        SystemClock {
            anchor: Instant::now(),
        }
    }
}

impl Default for SystemClock {
    fn default() -> Self {
        SystemClock::new()
    }
}

impl TraceClock for SystemClock {
    fn mono_ns(&self) -> u64 {
        u64::try_from(self.anchor.elapsed().as_nanos()).unwrap_or(u64::MAX)
    }
    fn wall(&self) -> DateTime<Utc> {
        Utc::now()
    }
}

/// Builds one [`TraceRecord`] while a query runs.
#[derive(Debug)]
pub struct QueryTrace<'c, C: TraceClock> {
    clock: &'c C,
    record: TraceRecord,
}

impl<'c, C: TraceClock> QueryTrace<'c, C> {
    /// Start a trace for generation `query_gen`.
    pub fn new(clock: &'c C, query_gen: u64) -> Self {
        QueryTrace {
            clock,
            record: TraceRecord {
                schema: TRACE_SCHEMA,
                query_gen,
                outcome: Outcome::Error,
                path: None,
                marks: BTreeMap::new(),
                spans: Vec::new(),
                state: TraceState::default(),
                lengths: Lengths::default(),
            },
        }
    }

    /// Record `mark` now. The first stamp of a mark wins (a repeated mark is ignored).
    pub fn mark(&mut self, mark: Mark) -> Stamp {
        let stamp = Stamp {
            mono_ns: self.clock.mono_ns(),
            wall: self.clock.wall(),
        };
        *self.record.marks.entry(mark).or_insert(stamp)
    }

    /// A monotonic reading for span starts.
    pub fn now_ns(&self) -> u64 {
        self.clock.mono_ns()
    }

    /// Record a span from `start_ns` to `end_ns`.
    pub fn span(&mut self, name: SpanName, start_ns: u64, end_ns: u64) {
        self.push_span(name, start_ns, end_ns, None, None);
    }

    /// Record a network span with its connection-reused flag.
    pub fn net_span(
        &mut self,
        name: SpanName,
        start_ns: u64,
        end_ns: u64,
        connection_reused: bool,
    ) {
        self.push_span(name, start_ns, end_ns, Some(connection_reused), None);
    }

    /// Record one of a repeated span (audio buffers).
    pub fn seq_span(&mut self, name: SpanName, start_ns: u64, end_ns: u64, seq: u32) {
        self.push_span(name, start_ns, end_ns, None, Some(seq));
    }

    fn push_span(
        &mut self,
        name: SpanName,
        start_ns: u64,
        end_ns: u64,
        reused: Option<bool>,
        seq: Option<u32>,
    ) {
        let end_ns = end_ns.max(start_ns);
        self.record.spans.push(Span {
            name,
            start_ns,
            end_ns,
            dur_ns: end_ns - start_ns,
            connection_reused: reused,
            seq,
        });
    }

    /// Set the machine state.
    pub fn set_state(&mut self, state: TraceState) {
        self.record.state = state;
    }

    /// Record the final transcript's lengths. The text is read here and dropped.
    pub fn set_transcript(&mut self, transcript: &str, partials: u32) {
        self.record.lengths = Lengths::of(transcript, partials);
    }

    /// Record the parser path.
    pub fn set_path(&mut self, path: crate::query::ParsePath) {
        self.record.path = Some(path);
    }

    /// Finish with an outcome.
    pub fn finish(mut self, outcome: Outcome) -> TraceRecord {
        self.record.outcome = outcome;
        self.record
    }
}

/// Writes trace records as JSON lines.
#[derive(Debug)]
pub struct TraceWriter<W: Write> {
    out: W,
}

impl<W: Write> TraceWriter<W> {
    /// Write to any sink.
    pub fn new(out: W) -> Self {
        TraceWriter { out }
    }

    /// Append one record as one line, and flush.
    pub fn write(&mut self, record: &TraceRecord) -> io::Result<()> {
        let mut line = serde_json::to_vec(record).map_err(io::Error::other)?;
        line.push(b'\n');
        self.out.write_all(&line)?;
        self.out.flush()
    }

    /// The sink.
    pub fn into_inner(self) -> W {
        self.out
    }
}

impl TraceWriter<std::fs::File> {
    /// Open `path` for appending, creating it and its directory.
    pub fn append(path: &Path) -> io::Result<Self> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        Ok(TraceWriter::new(file))
    }
}

/// `<data dir>/traces/<YYYY-MM-DD>.jsonl` for a UTC date, after the data-dir safety checks
/// (outside any git work tree and any cloud-synced folder; see [`crate::datadir`]).
pub fn trace_path(date: NaiveDate) -> Result<PathBuf, crate::datadir::DataDirError> {
    Ok(crate::datadir::data_dir()?
        .join("traces")
        .join(format!("{}.jsonl", date.format("%Y-%m-%d"))))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    /// A clock that advances 1 ms per reading.
    struct FakeClock {
        ns: Cell<u64>,
    }

    impl TraceClock for FakeClock {
        fn mono_ns(&self) -> u64 {
            let v = self.ns.get() + 1_000_000;
            self.ns.set(v);
            v
        }
        fn wall(&self) -> DateTime<Utc> {
            DateTime::from_timestamp_nanos(1_795_000_000_000_000_000 + self.ns.get() as i64)
        }
    }

    const UTTERANCE: &str = "Is Rainier out tomorrow at 4? Show me Point Reyes golden hour";

    fn full_record(clock: &FakeClock) -> TraceRecord {
        let mut t = QueryTrace::new(clock, 42);
        for m in Mark::ALL {
            t.mark(m);
        }
        let s = t.now_ns();
        t.span(SpanName::OsEventToHandler, s, s + 10);
        for (i, name) in [SpanName::AudioBufferDelivery, SpanName::AudioBufferDelivery]
            .into_iter()
            .enumerate()
        {
            t.seq_span(name, s + 20, s + 30, i as u32);
        }
        for name in [SpanName::Dns, SpanName::Tcp, SpanName::Tls, SpanName::Ttfb] {
            t.net_span(name, s, s + 5, true);
        }
        for name in [
            SpanName::SttToken,
            SpanName::ProxyInternal,
            SpanName::ProviderQueue,
            SpanName::ProviderInference,
            SpanName::Readback,
            SpanName::Encode,
            SpanName::PanelReveal,
        ] {
            t.span(name, s, s + 7);
        }
        t.set_state(TraceState {
            idle_duration_ms: Some(1_800_000),
            cache_state: CacheState::Idle,
            thermal_state: ThermalState::Nominal,
            power_source: PowerSource::Battery,
            network_interface: NetworkInterface::Wifi,
        });
        t.set_transcript(UTTERANCE, 6);
        t.set_path(crate::query::ParsePath::FastPath);
        t.finish(Outcome::Painted)
    }

    fn written(record: &TraceRecord) -> String {
        let mut w = TraceWriter::new(Vec::new());
        w.write(record).expect("write");
        String::from_utf8(w.into_inner()).expect("utf8")
    }

    #[test]
    fn one_line_with_prd_field_names() {
        let clock = FakeClock { ns: Cell::new(0) };
        let line = written(&full_record(&clock));
        assert!(line.ends_with('\n') && line.matches('\n').count() == 1);
        for name in [
            "key_down",
            "speech_first_partial",
            "place_matched",
            "assets_ready",
            "key_up",
            "speech_final",
            "parsed",
            "forecast_ready",
            "tier0_paint",
            "tier1_paint",
            "tier2_sent",
            "tier2_first_byte",
            "tier2_checked",
            "tier2_paint",
            "os_event_to_handler",
            "audio_buffer_delivery",
            "stt_token",
            "dns",
            "tcp",
            "tls",
            "ttfb",
            "proxy_internal",
            "provider_queue",
            "provider_inference",
            "readback",
            "encode",
            "panel_reveal",
            "connection_reused",
            "idle_duration_ms",
            "cache_state",
            "thermal_state",
            "power_source",
            "network_interface",
            "mono_ns",
            "wall",
        ] {
            assert!(
                line.contains(&format!("\"{name}\"")),
                "missing {name}: {line}"
            );
        }
        let v: serde_json::Value = serde_json::from_str(&line).expect("json");
        assert_eq!(v["lengths"]["transcript_chars"], UTTERANCE.chars().count());
        assert_eq!(v["lengths"]["transcript_words"], 12);
        assert_eq!(v["marks"]["key_down"]["mono_ns"], 1_000_000);
    }

    #[test]
    fn no_utterance_text_reaches_the_output() {
        let clock = FakeClock { ns: Cell::new(0) };
        let line = written(&full_record(&clock)).to_lowercase();
        assert!(!line.contains(&UTTERANCE.to_lowercase()));
        let output_words: std::collections::HashSet<&str> = line
            .split(|c: char| !c.is_alphanumeric())
            .filter(|w| !w.is_empty())
            .collect();
        for word in UTTERANCE
            .to_lowercase()
            .split(|c: char| !c.is_alphanumeric())
            .filter(|w| w.len() >= 2)
        {
            assert!(
                !output_words.contains(word),
                "transcript word {word:?} leaked: {line}"
            );
        }
        // Structural proof: every string value is an enum name or a timestamp.
        fn walk(v: &serde_json::Value, strings: &mut Vec<String>) {
            match v {
                serde_json::Value::String(s) => strings.push(s.clone()),
                serde_json::Value::Array(a) => a.iter().for_each(|x| walk(x, strings)),
                serde_json::Value::Object(o) => o.values().for_each(|x| walk(x, strings)),
                _ => {}
            }
        }
        let v: serde_json::Value = serde_json::from_str(&line).expect("json");
        let mut strings = Vec::new();
        walk(&v, &mut strings);
        let enum_names = [
            "painted",
            "cancelled",
            "no_match",
            "out_of_range",
            "fallback",
            "empty",
            "error",
            "fast_path",
            "os_event_to_handler",
            "audio_buffer_delivery",
            "stt_token",
            "dns",
            "tcp",
            "tls",
            "ttfb",
            "proxy_internal",
            "provider_queue",
            "provider_inference",
            "readback",
            "encode",
            "panel_reveal",
            "unknown",
            "idle",
            "warm",
            "cold",
            "after_wake",
            "after_launch",
            "nominal",
            "fair",
            "serious",
            "critical",
            "ac",
            "battery",
            "none",
            "wifi",
            "ethernet",
            "cellular",
            "other",
        ];
        for s in strings {
            let is_time = DateTime::parse_from_rfc3339(&s).is_ok();
            assert!(
                is_time || enum_names.contains(&s.as_str()),
                "free text in trace: {s:?}"
            );
        }
    }

    #[test]
    fn first_mark_wins_and_spans_never_go_negative() {
        let clock = FakeClock { ns: Cell::new(0) };
        let mut t = QueryTrace::new(&clock, 1);
        let a = t.mark(Mark::KeyUp);
        let b = t.mark(Mark::KeyUp);
        assert_eq!(a, b);
        t.span(SpanName::Encode, 50, 10);
        let r = t.finish(Outcome::Cancelled);
        assert_eq!(r.spans[0].dur_ns, 0);
    }

    #[test]
    fn appends_lines_to_a_file() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("traces").join("2026-11-19.jsonl");
        let clock = FakeClock { ns: Cell::new(0) };
        for generation in 0..3 {
            let mut w = TraceWriter::append(&path).expect("open");
            w.write(&QueryTrace::new(&clock, generation).finish(Outcome::Empty))
                .expect("write");
        }
        let text = std::fs::read_to_string(&path).expect("read");
        assert_eq!(text.lines().count(), 3);
        for line in text.lines() {
            serde_json::from_str::<serde_json::Value>(line).expect("each line is JSON");
        }
    }

    #[test]
    fn system_clock_is_monotonic() {
        let c = SystemClock::new();
        let a = c.mono_ns();
        let b = c.mono_ns();
        assert!(b >= a);
    }
}
