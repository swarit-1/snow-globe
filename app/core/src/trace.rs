//! Per-query latency trace: one JSON line per query (PRD §10.5), with the fields the replay report
//! needs (PRD §10.6, §14; bench/README.md "Scoring").
//!
//! **Timestamps** ([`Mark`]): `key_down`, `speech_first_partial`, `place_matched`,
//! `assets_ready`, `key_up`, `speech_final`, `parsed`, `forecast_ready`, `tier0_paint`,
//! `tier1_paint`, `tier2_sent`, `tier2_first_byte`, `tier2_checked`, `tier2_paint`. Each stamp has
//! a monotonic reading in nanoseconds (`mono_ns`, from the clock's anchor) and the wall clock
//! (`wall`, RFC 3339 UTC with microseconds). Schema 2 adds `audio_first_sample`: the capture time of
//! the session's first audio sample, from which a fixture's speech onset is counted (PRD §14,
//! "first partial ≤ 300 ms after speech onset").
//!
//! **Spans** ([`SpanName`]): OS event → handler, audio buffer delivery, `stt_token`, DNS, TCP,
//! TLS and time to first byte (with a connection-reused flag), proxy-internal spans, provider
//! queue and inference, readback, encode and panel reveal. Schema 2 adds `partial_lag` and
//! `stable_lag`, one per PARTIAL or STABLE speech event: from the end of the audio the event covers
//! to its arrival (PRD §10.6 "partial lag").
//!
//! **Paints** ([`Paint`], schema 2): every composited paint, with its tier, whether its scene came
//! from a stable partial or from the final transcript, which of the query's scenes it showed, and
//! whether that scene is the final parse's (PRD §7.1 lever 2, §10.3: a wrong paint never counts as
//! a hit). Once a tier has a recorded paint, its paint mark is its earliest paint (PRD §10.4: a
//! webview callback's time is a diagnostic, never the paint time).
//!
//! **Context** ([`ContextPlace`], schema 2): the last place the parser was given for this query, as
//! manifest ids, or `empty`. The replay scorer re-parses the final transcript at it, never at the
//! script's context column (bench/README.md "Scoring").
//!
//! **Speech setup** ([`SpeechSetup`], schema 2): the engine, the silence pad (D-067: set and logged
//! with every trial), the model retention and whether a launch-time warm-up ran (D-067).
//!
//! **Levers** ([`Levers`], schema 2): the other settings the bake-off compares on and off: Tier 1
//! pre-render from a stable partial (PRD §7.2), Tier 0 paint from a stable partial (PRD §7.1
//! lever 2, §14) and the harness's paint capture (PRD §10.4's overhead check). The scorer pools
//! trials only within one speech setup and one setting of the levers.
//!
//! **State** ([`TraceState`]): idle duration, cache state (the five PRD §10.6 states), thermal
//! state, power source, the network interface *type* only, and (schema 2) the display state when
//! the query began (D-068: display-asleep trials are reported apart).
//!
//! **Replay** ([`ReplayTrial`], schema 2, replay runs only): the script hash, the utterance id, the
//! take, the harness session and the trial number, so the scorer can join a line to its script row,
//! its fixture's sidecar and its transcript.
//!
//! **Privacy:** traces stay on the machine and never contain transcript text. The record has no
//! free-text field: every string in the output is an enum name, a timestamp, an id from the
//! viewpoint manifests (checked against the place table when it is recorded), or a replay id of a
//! fixed form (a 12-hex-digit script hash; an utterance id such as `t001`, or a real-use
//! recording's UTC key-down time). The transcript itself is represented only by lengths
//! ([`Lengths`]). Tests walk the JSON to prove it.
//!
//! In replay runs the harness completes the record (PRD §10.4): the key stamps come from its
//! listen-only tap and the paint stamps from ScreenCaptureKit, recorded with [`QueryTrace::mark_at`]
//! and [`QueryTrace::paint`] at the captured instants. Host-time alignment with ScreenCaptureKit is
//! TODO(M1): `mono_ns` here counts from a process anchor taken with `std::time::Instant`.
//!
//! TODO(M1), for the lead: PRD §10.4's pixel tag carries (query_gen, tier) only. Under lever 2 a
//! query can paint Tier 0 twice, the partial's scene and then the final's, under the same tag, so
//! the captured frames alone cannot say which composite showed which scene, and the headline
//! "Tier 0 matching the final" needs exactly that. Either the tag also carries the scene index (or
//! a per-query paint sequence), which changes PRD §10.4, or the webview's paint gate logs each
//! composite's scene for the harness to join to the captured frames.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::OpenOptions;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use chrono::{DateTime, NaiveDate, SecondsFormat, TimeDelta, Utc};
use serde::{Deserialize, Serialize, Serializer};

use crate::lifecycle::{SceneHash, Tier};
use crate::parser::LastPlace;
use crate::places::PlaceTable;

/// Version of the line format. 2 added `audio_first_sample`, the lag spans, `paints`, `context`,
/// `speech`, `levers`, `state.display` and `replay`.
pub const TRACE_SCHEMA: u32 = 2;

/// The PRD §10.5 timestamps, plus `audio_first_sample`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum Mark {
    /// Hotkey pressed.
    #[serde(rename = "key_down")]
    KeyDown,
    /// Capture time of the session's first audio sample (schema 2). Speech events' audio times
    /// and a fixture's speech onset are counted from it.
    #[serde(rename = "audio_first_sample")]
    AudioFirstSample,
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
    /// Tier 0 composited (the first Tier 0 paint, right or wrong; [`Paint`] says which). Once a
    /// paint is recorded with [`QueryTrace::paint`], this is the earliest recorded Tier 0 paint,
    /// whatever [`QueryTrace::mark`] stamped before (likewise the other tiers' paint marks).
    #[serde(rename = "tier0_paint")]
    Tier0Paint,
    /// Tier 1 composited (the first Tier 1 paint).
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
    /// Tier 2 composited (the first Tier 2 paint).
    #[serde(rename = "tier2_paint")]
    Tier2Paint,
}

impl Mark {
    /// Every mark, in PRD order (`audio_first_sample` after `key_down`, when the audio starts).
    pub const ALL: [Mark; 15] = [
        Mark::KeyDown,
        Mark::AudioFirstSample,
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

    /// The mark stamped by a tier's first paint.
    pub fn paint_of(tier: Tier) -> Mark {
        match tier {
            Tier::Tier0 => Mark::Tier0Paint,
            Tier::Tier1 => Mark::Tier1Paint,
            Tier::Tier2 => Mark::Tier2Paint,
        }
    }
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

/// The PRD §10.5 spans, plus the speech events' lags.
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
    /// One PARTIAL speech event (schema 2): from the end of the audio it covers to its arrival
    /// (PRD §10.6 "partial lag"); `seq` counts the query's PARTIAL events from 0.
    PartialLag,
    /// One STABLE speech event (schema 2), measured as [`SpanName::PartialLag`] is.
    StableLag,
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
    /// Repeated spans (audio buffers, speech events): their index.
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

/// Whether the display was on when the query began (D-068). Idle trials let the display sleep as
/// the Mac's settings do, so the time to the composited pixel can include waking it; the report
/// gives display-asleep trials apart as well as inside the totals.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DisplayState {
    /// Not recorded.
    Unknown,
    /// On (a locked screen with the display lit counts as on).
    On,
    /// Asleep.
    Asleep,
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
    /// Display state when the query began (schema 2).
    pub display: DisplayState,
}

impl Default for TraceState {
    fn default() -> Self {
        TraceState {
            idle_duration_ms: None,
            cache_state: CacheState::Unknown,
            thermal_state: ThermalState::Unknown,
            power_source: PowerSource::Unknown,
            network_interface: NetworkInterface::Unknown,
            display: DisplayState::Unknown,
        }
    }
}

/// Transcript lengths: the only trace of the words heard.
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

/// A place as ids from the viewpoint manifests: a viewpoint and the place it belongs to, both
/// checked against a [`PlaceTable`]. Its fields are private, so a trace can only ever hold ids the
/// table knows, never words.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct KnownPlace {
    place_id: String,
    viewpoint_id: String,
}

impl KnownPlace {
    /// The ids, if `viewpoint_id` is a viewpoint of `places` and belongs to `place_id`.
    pub fn new(places: &PlaceTable, place_id: &str, viewpoint_id: &str) -> Option<KnownPlace> {
        let (_, viewpoint) = places.viewpoint(viewpoint_id)?;
        (viewpoint.place_id == place_id).then(|| KnownPlace {
            place_id: place_id.to_string(),
            viewpoint_id: viewpoint_id.to_string(),
        })
    }

    /// `place_id`.
    pub fn place_id(&self) -> &str {
        &self.place_id
    }

    /// `viewpoint_id`.
    pub fn viewpoint_id(&self) -> &str {
        &self.viewpoint_id
    }
}

/// The last place the parser was given for this query (PRD §7.4 "A time with no place"; the
/// `last` of [`crate::ParseContext`]). Recorded for every query, follow-up or not, so the scorer
/// can re-parse the final transcript exactly as the app parsed it (bench/README.md "Scoring").
/// Serialised with a `kind` tag: `{"kind": "place", "place_id": ..., "viewpoint_id": ...}`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ContextPlace {
    /// Not recorded (the default). The scorer cannot score place or time on such a line.
    NotRecorded,
    /// The parser had no last place: the first query after a launch, or nothing resolved yet.
    Empty,
    /// The parser's last place.
    Place(KnownPlace),
    /// The last place named ids the place table does not know. Never expected; it keeps the rule
    /// that a trace holds only ids the manifests define.
    Unlisted,
}

impl ContextPlace {
    /// The context for a parser given `last`, checked against `places`.
    pub fn of(last: Option<&LastPlace>, places: &PlaceTable) -> ContextPlace {
        match last {
            None => ContextPlace::Empty,
            Some(l) => KnownPlace::new(places, &l.place_id, &l.viewpoint_id)
                .map_or(ContextPlace::Unlisted, ContextPlace::Place),
        }
    }
}

/// The speech engines of PRD §7.1.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EngineKind {
    /// Not recorded.
    Unknown,
    /// A1: SpeechAnalyzer `DictationTranscriber`, on device (the default, D-015).
    DictationTranscriber,
    /// D: Deepgram Nova-3, bake-off only.
    DeepgramNova3,
    /// P: Parakeet (FluidAudio), on device.
    Parakeet,
}

/// The on-device model's retention between sessions (`SpeechAnalyzer.Options.ModelRetention`),
/// which the bake-off varies because it affects idle latency.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelRetention {
    /// Not recorded, or not applicable (a cloud engine).
    Unknown,
    /// Kept only while in use.
    WhileInUse,
    /// Kept for a while after use (the library's default).
    Lingering,
    /// Kept for the life of the process.
    ProcessLifetime,
}

/// How speech was set up for the query (schema 2; D-067: the shell and the harness set the silence
/// pad explicitly and log it with every trial, and the bake-off measures it with model retention
/// and a launch-time warm-up).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct SpeechSetup {
    /// The engine.
    pub engine: EngineKind,
    /// The burst silence pad pushed at key-up, ms (PRD §7.1 finalization lever 1), if recorded.
    pub silence_pad_ms: Option<u32>,
    /// The model retention.
    pub model_retention: ModelRetention,
    /// Whether the engine was warmed up when the app launched (a throwaway session before the
    /// first query, D-067), if recorded.
    pub warm_up: Option<bool>,
}

impl Default for SpeechSetup {
    fn default() -> Self {
        SpeechSetup {
            engine: EngineKind::Unknown,
            silence_pad_ms: None,
            model_retention: ModelRetention::Unknown,
            warm_up: None,
        }
    }
}

/// The settings besides the speech setup that the bake-off measures on and off (schema 2), each
/// `None` (`null`) when not recorded. Trials are pooled only within one setting of all of them.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize)]
pub struct Levers {
    /// Tier 1 may pre-render from a stable partial (PRD §7.2: release → final is measured with
    /// pre-render on and off).
    pub pre_render: Option<bool>,
    /// Tier 0 may paint from a stable partial (PRD §7.1 lever 2; PRD §14 "or partial paint
    /// disabled").
    pub partial_paint: Option<bool>,
    /// The harness captured paints with ScreenCaptureKit (PRD §10.4: capture on and off must
    /// differ by less than one vsync at p95). With it off no paint is recorded.
    pub paint_capture: Option<bool>,
}

/// Where a painted scene came from (PRD §7.1 lever 2: Tier 0 may paint from a stable partial).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PaintSource {
    /// A stable partial transcript, before the final.
    Partial,
    /// The final transcript.
    Final,
}

/// One composited paint (schema 2; PRD §10.3, §10.4).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Paint {
    /// The tier.
    pub tier: Tier,
    /// Where its scene came from.
    pub source: PaintSource,
    /// Which of the query's scenes it showed: 0 for the first scene recorded, then the next index
    /// for each new one. Two paints showed the same scene exactly when their indices are equal,
    /// so a change of index between paints in time order is a flip.
    pub scene: u32,
    /// Whether the scene is the one the final parse resolved to: `false` when it is another one
    /// or the final parse resolved to none (a miss, the fallback, out of range); `null` while the
    /// final parse is unknown (a query superseded before it). A paint with `false` is a wrong
    /// paint (PRD §10.3).
    pub matches_final: Option<bool>,
    /// When it was composited, monotonic ns (PRD §10.4's paint definition).
    pub mono_ns: u64,
}

/// What a [`ReplayTrial`] id was refused for. The rejected value is never echoed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum ReplayIdError {
    /// Not 12 lower-case hex digits (bench/README.md "Where recordings live").
    #[error("a script hash is 12 lower-case hex digits")]
    ScriptHash,
    /// Not a scripted id (`d01`-`d99`, `t001`-`t999`) or a real-use key-down time
    /// (`YYYYMMDDTHHMMSSZ`).
    #[error("an utterance id is d<2 digits>, t<3 digits> or a UTC time YYYYMMDDTHHMMSSZ")]
    Utterance,
    /// Takes count from 1.
    #[error("takes count from 1")]
    Take,
}

/// Which fixture a replayed query played, and where it sits in the harness's run (schema 2; PRD
/// §10.6). Its fields are private and checked, so only ids of a fixed form reach the trace.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ReplayTrial {
    script_hash: String,
    utterance: String,
    take: u32,
    session: u32,
    trial: u32,
}

fn all_bytes(s: &str, len: usize, ok: impl Fn(u8) -> bool) -> bool {
    s.len() == len && s.bytes().all(ok)
}

fn is_scripted_id(s: &str) -> bool {
    let digits = |rest: &str, n: usize| all_bytes(rest, n, |b| b.is_ascii_digit());
    s.strip_prefix('d').is_some_and(|r| digits(r, 2))
        || s.strip_prefix('t').is_some_and(|r| digits(r, 3))
}

fn is_key_down_time(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 16
        && b[8] == b'T'
        && b[15] == b'Z'
        && b[..8].iter().chain(&b[9..15]).all(u8::is_ascii_digit)
}

impl ReplayTrial {
    /// A trial of utterance `utterance` (take `take`) of the script `script_hash`, played as
    /// trial `trial` (counting from 1 in play order) of harness session `session` (one
    /// unattended run, such as one night).
    pub fn new(
        script_hash: &str,
        utterance: &str,
        take: u32,
        session: u32,
        trial: u32,
    ) -> Result<ReplayTrial, ReplayIdError> {
        if !all_bytes(script_hash, 12, |b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
            return Err(ReplayIdError::ScriptHash);
        }
        if !(is_scripted_id(utterance) || is_key_down_time(utterance)) {
            return Err(ReplayIdError::Utterance);
        }
        if take == 0 {
            return Err(ReplayIdError::Take);
        }
        Ok(ReplayTrial {
            script_hash: script_hash.to_string(),
            utterance: utterance.to_string(),
            take,
            session,
            trial,
        })
    }

    /// The script hash.
    pub fn script_hash(&self) -> &str {
        &self.script_hash
    }

    /// The utterance id.
    pub fn utterance(&self) -> &str {
        &self.utterance
    }

    /// The take.
    pub fn take(&self) -> u32 {
        self.take
    }

    /// The harness session.
    pub fn session(&self) -> u32 {
        self.session
    }

    /// The trial number within the session.
    pub fn trial(&self) -> u32 {
        self.trial
    }
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
    /// The parser's last place (schema 2).
    pub context: ContextPlace,
    /// Speech setup (schema 2).
    pub speech: SpeechSetup,
    /// The levers' settings (schema 2).
    pub levers: Levers,
    /// Timestamps by mark.
    pub marks: BTreeMap<Mark, Stamp>,
    /// Spans, in the order recorded.
    pub spans: Vec<Span>,
    /// Paints, in the order recorded (schema 2).
    pub paints: Vec<Paint>,
    /// Machine state.
    pub state: TraceState,
    /// Transcript lengths.
    pub lengths: Lengths,
    /// Replay runs only: which fixture was played (schema 2).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub replay: Option<ReplayTrial>,
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

/// Nanoseconds as a signed delta, saturating.
fn delta_ns(ns: u64) -> TimeDelta {
    TimeDelta::nanoseconds(i64::try_from(ns).unwrap_or(i64::MAX))
}

/// Builds one [`TraceRecord`] while a query runs.
#[derive(Debug)]
pub struct QueryTrace<'c, C: TraceClock> {
    clock: &'c C,
    record: TraceRecord,
    /// The query's scenes, in the order first painted ([`Paint::scene`] indexes it).
    scenes: Vec<SceneHash>,
    /// The final parse's scene once known: `Some(None)` when it resolved to no scene.
    final_scene: Option<Option<SceneHash>>,
    /// The paint marks that a recorded paint set (only those are composited times).
    painted: BTreeSet<Mark>,
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
                context: ContextPlace::NotRecorded,
                speech: SpeechSetup::default(),
                levers: Levers::default(),
                marks: BTreeMap::new(),
                spans: Vec::new(),
                paints: Vec::new(),
                state: TraceState::default(),
                lengths: Lengths::default(),
                replay: None,
            },
            scenes: Vec::new(),
            final_scene: None,
            painted: BTreeSet::new(),
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

    /// Record `mark` at the monotonic instant `mono_ns`, measured elsewhere (a key event from the
    /// harness's tap, a ScreenCaptureKit frame, an audio capture time) and already on this clock.
    /// Its wall time is the clock's wall time shifted by the same amount. The first stamp wins.
    pub fn mark_at(&mut self, mark: Mark, mono_ns: u64) -> Stamp {
        let now_ns = self.clock.mono_ns();
        let now_wall = self.clock.wall();
        let wall = if mono_ns <= now_ns {
            now_wall - delta_ns(now_ns - mono_ns)
        } else {
            now_wall + delta_ns(mono_ns - now_ns)
        };
        *self
            .record
            .marks
            .entry(mark)
            .or_insert(Stamp { mono_ns, wall })
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

    /// Record the lag of a PARTIAL (`SpanName::PartialLag`) or STABLE (`SpanName::StableLag`)
    /// speech event `seq` that covers audio up to `audio_end_s` (seconds since the session's first
    /// sample, as the speech library reports it) and arrived at `arrived_ns`: a span from the
    /// `audio_first_sample` mark plus `audio_end_s` to `arrived_ns` (PRD §10.6 "partial lag").
    /// Returns false, recording nothing, for another span name, a negative or non-finite audio
    /// time, or before `audio_first_sample` is marked. An arrival before the audio's end is
    /// recorded as 0 ns.
    pub fn speech_event_lag(
        &mut self,
        name: SpanName,
        seq: u32,
        audio_end_s: f64,
        arrived_ns: u64,
    ) -> bool {
        if !matches!(name, SpanName::PartialLag | SpanName::StableLag)
            || !audio_end_s.is_finite()
            || audio_end_s < 0.0
        {
            return false;
        }
        let Some(origin) = self.record.marks.get(&Mark::AudioFirstSample) else {
            return false;
        };
        // Whole nanoseconds (a float-to-integer `as` saturates, so a huge audio time is u64::MAX).
        let offset_ns = (audio_end_s * 1e9).round() as u64;
        let audio_end_ns = origin.mono_ns.saturating_add(offset_ns);
        self.push_span(name, audio_end_ns, arrived_ns, None, Some(seq));
        true
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

    /// Record a composited paint of `tier` showing `scene` (from `source`) at `mono_ns`, on this
    /// clock. The tier's mark ([`Mark::paint_of`]) is its earliest recorded paint, even when paints
    /// are recorded out of order (the harness may read ScreenCaptureKit's frames after the fact),
    /// and the first paint replaces a stamp that [`QueryTrace::mark`] or [`QueryTrace::mark_at`]
    /// made: a requestAnimationFrame or IPC time fires 1-3 vsyncs before compositing and is never
    /// the paint time (PRD §10.4). The scene hash itself is not kept: the paint holds the scene's
    /// index among the query's scenes.
    ///
    /// TODO(M1): see the module doc. Capture tags a frame with (query_gen, tier) only, so the
    /// harness needs another source for `scene` when a tier paints two scenes in one query.
    pub fn paint(&mut self, tier: Tier, source: PaintSource, scene: SceneHash, mono_ns: u64) {
        let index = self
            .scenes
            .iter()
            .position(|s| *s == scene)
            .unwrap_or_else(|| {
                self.scenes.push(scene);
                self.scenes.len() - 1
            });
        let matches_final = self.final_scene.map(|f| f == Some(scene));
        self.record.paints.push(Paint {
            tier,
            source,
            scene: u32::try_from(index).unwrap_or(u32::MAX),
            matches_final,
            mono_ns,
        });
        let mark = Mark::paint_of(tier);
        let earlier_paint = self.painted.contains(&mark)
            && self
                .record
                .marks
                .get(&mark)
                .is_some_and(|m| m.mono_ns <= mono_ns);
        if !earlier_paint {
            self.record.marks.remove(&mark);
            self.mark_at(mark, mono_ns);
            self.painted.insert(mark);
        }
    }

    /// Record the final parse's scene (`None` when it resolved to none: a miss, the fallback, out
    /// of range), and judge every paint against it, those recorded so far and those to come.
    /// Until this is called, paints carry `matches_final: null`.
    pub fn resolve_paints(&mut self, final_scene: Option<SceneHash>) {
        self.final_scene = Some(final_scene);
        for paint in &mut self.record.paints {
            let shown = self.scenes.get(paint.scene as usize).copied();
            paint.matches_final = Some(shown.is_some() && shown == final_scene);
        }
    }

    /// Set the machine state.
    pub fn set_state(&mut self, state: TraceState) {
        self.record.state = state;
    }

    /// Record the last place the parser was given (`last`, as in [`crate::ParseContext`]),
    /// checked against `places`.
    pub fn set_context(&mut self, last: Option<&LastPlace>, places: &PlaceTable) {
        self.record.context = ContextPlace::of(last, places);
    }

    /// Record the speech setup.
    pub fn set_speech(&mut self, setup: SpeechSetup) {
        self.record.speech = setup;
    }

    /// Record the levers' settings.
    pub fn set_levers(&mut self, levers: Levers) {
        self.record.levers = levers;
    }

    /// Mark the query as a replay trial.
    pub fn set_replay(&mut self, replay: ReplayTrial) {
        self.record.replay = Some(replay);
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

    fn rainier() -> LastPlace {
        LastPlace {
            place_id: "rainier".to_string(),
            viewpoint_id: "rainier.paradise-mountain".to_string(),
        }
    }

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
        assert!(t.speech_event_lag(SpanName::PartialLag, 0, 0.25, s + 300_000_000));
        assert!(t.speech_event_lag(SpanName::StableLag, 0, 0.25, s + 310_000_000));
        let a = SceneHash::of_parts(&[b"a"]);
        t.paint(Tier::Tier0, PaintSource::Partial, a, s + 400);
        t.resolve_paints(Some(a));
        t.set_state(TraceState {
            idle_duration_ms: Some(1_800_000),
            cache_state: CacheState::Idle,
            thermal_state: ThermalState::Nominal,
            power_source: PowerSource::Battery,
            network_interface: NetworkInterface::Wifi,
            display: DisplayState::Asleep,
        });
        // The context names a place the utterance also names: only its manifest ids may appear.
        t.set_context(Some(&rainier()), PlaceTable::embedded());
        t.set_speech(SpeechSetup {
            engine: EngineKind::DictationTranscriber,
            silence_pad_ms: Some(0),
            model_retention: ModelRetention::Lingering,
            warm_up: Some(true),
        });
        t.set_levers(Levers {
            pre_render: Some(true),
            partial_paint: Some(true),
            paint_capture: Some(true),
        });
        t.set_replay(ReplayTrial::new("3b4256f1cf51", "t001", 1, 2, 7).expect("ids"));
        t.set_transcript(UTTERANCE, 6);
        t.set_path(crate::query::ParsePath::FastPath);
        t.finish(Outcome::Painted)
    }

    fn written(record: &TraceRecord) -> String {
        let mut w = TraceWriter::new(Vec::new());
        w.write(record).expect("write");
        String::from_utf8(w.into_inner()).expect("utf8")
    }

    /// Every enum value a schema-2 trace can hold.
    const ENUM_NAMES: &[&str] = &[
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
        "partial_lag",
        "stable_lag",
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
        "on",
        "asleep",
        "not_recorded",
        "place",
        "unlisted",
        "dictation_transcriber",
        "deepgram_nova3",
        "parakeet",
        "while_in_use",
        "lingering",
        "process_lifetime",
        "tier0",
        "tier1",
        "tier2",
        "partial",
        "final",
    ];

    /// The keys whose string values are ids, and the check each must pass.
    fn id_value_ok(key: &str, value: &str) -> bool {
        let table = PlaceTable::embedded();
        match key {
            "place_id" => table.place(value).is_some(),
            "viewpoint_id" => table.viewpoint(value).is_some(),
            "script_hash" => all_bytes(value, 12, |b| matches!(b, b'0'..=b'9' | b'a'..=b'f')),
            "utterance" => is_scripted_id(value) || is_key_down_time(value),
            _ => false,
        }
    }

    /// Every (key, string value) pair in the JSON, and every key.
    fn walk(
        v: &serde_json::Value,
        key: &str,
        strings: &mut Vec<(String, String)>,
        keys: &mut Vec<String>,
    ) {
        match v {
            serde_json::Value::String(s) => strings.push((key.to_string(), s.clone())),
            serde_json::Value::Array(a) => a.iter().for_each(|x| walk(x, key, strings, keys)),
            serde_json::Value::Object(o) => {
                for (k, x) in o {
                    keys.push(k.clone());
                    walk(x, k, strings, keys);
                }
            }
            _ => {}
        }
    }

    #[test]
    fn one_line_with_prd_field_names() {
        let clock = FakeClock { ns: Cell::new(0) };
        let line = written(&full_record(&clock));
        assert!(line.ends_with('\n') && line.matches('\n').count() == 1);
        for name in [
            "key_down",
            "audio_first_sample",
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
            "partial_lag",
            "stable_lag",
            "connection_reused",
            "idle_duration_ms",
            "cache_state",
            "thermal_state",
            "power_source",
            "network_interface",
            "display",
            "context",
            "speech",
            "warm_up",
            "levers",
            "pre_render",
            "partial_paint",
            "paint_capture",
            "paints",
            "replay",
            "mono_ns",
            "wall",
        ] {
            assert!(
                line.contains(&format!("\"{name}\"")),
                "missing {name}: {line}"
            );
        }
        let v: serde_json::Value = serde_json::from_str(&line).expect("json");
        assert_eq!(v["schema"], 2);
        assert_eq!(v["lengths"]["transcript_chars"], UTTERANCE.chars().count());
        assert_eq!(v["lengths"]["transcript_words"], 12);
        assert_eq!(v["marks"]["key_down"]["mono_ns"], 1_000_000);
        assert_eq!(v["context"]["kind"], "place");
        assert_eq!(v["context"]["viewpoint_id"], "rainier.paradise-mountain");
        assert_eq!(v["state"]["display"], "asleep");
        assert_eq!(v["speech"]["silence_pad_ms"], 0);
        assert_eq!(v["speech"]["warm_up"], true);
        assert_eq!(v["levers"]["partial_paint"], true);
        assert_eq!(v["replay"]["utterance"], "t001");
        assert_eq!(v["paints"][0]["matches_final"], true);
    }

    #[test]
    fn no_utterance_text_reaches_the_output() {
        let clock = FakeClock { ns: Cell::new(0) };
        let line = written(&full_record(&clock));
        assert!(!line.to_lowercase().contains(&UTTERANCE.to_lowercase()));
        let v: serde_json::Value = serde_json::from_str(&line).expect("json");
        let (mut strings, mut keys) = (Vec::new(), Vec::new());
        walk(&v, "", &mut strings, &mut keys);
        // Structural proof: every string value is an enum name, a timestamp, or an id that the
        // manifests (or the replay id rules) define, under an id key.
        let mut free_words: std::collections::HashSet<String> = keys
            .iter()
            .flat_map(|k| k.split('_').map(str::to_string).collect::<Vec<_>>())
            .collect();
        for (key, s) in &strings {
            let is_time = DateTime::parse_from_rfc3339(s).is_ok();
            let is_id = id_value_ok(key, s);
            assert!(
                is_time || is_id || ENUM_NAMES.contains(&s.as_str()),
                "free text in trace: {key}: {s:?}"
            );
            if !is_id && !is_time {
                free_words.insert(s.to_lowercase());
            }
        }
        // Outside the manifest ids, no word of the transcript appears anywhere.
        for word in UTTERANCE
            .to_lowercase()
            .split(|c: char| !c.is_alphanumeric())
            .filter(|w| w.len() >= 2)
        {
            assert!(
                !free_words.contains(word),
                "transcript word {word:?} leaked: {line}"
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
    fn mark_at_shifts_the_wall_clock_with_the_monotonic_one() {
        let clock = FakeClock { ns: Cell::new(0) };
        let mut t = QueryTrace::new(&clock, 1);
        let now = t.mark(Mark::KeyDown);
        let earlier = t.mark_at(Mark::KeyUp, now.mono_ns - 250_000);
        let later = t.mark_at(Mark::Parsed, now.mono_ns + 3_000_000_000);
        // The fake clock advanced 1 ms per reading, and its wall clock moves with it.
        let shift = |s: &Stamp| (s.wall - now.wall).num_nanoseconds().expect("small");
        assert_eq!(
            shift(&earlier),
            i64::try_from(earlier.mono_ns).expect("small") - now.mono_ns as i64
        );
        assert_eq!(shift(&later), 3_000_000_000);
        assert_eq!(
            t.mark_at(Mark::KeyUp, 7).mono_ns,
            earlier.mono_ns,
            "first wins"
        );
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
            let v: serde_json::Value = serde_json::from_str(line).expect("each line is JSON");
            assert_eq!(v["context"]["kind"], "not_recorded");
            assert!(
                v.get("replay").is_none(),
                "replay appears only in replay runs"
            );
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
