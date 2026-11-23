//! `snow-globe-ask`: an utterance in, the closest real photo out (PRD §6.3 steps 3–4, §9.1).
//!
//! ```text
//! snow-globe-ask "<utterance>" [--now <RFC3339>] [--machine-tz <IANA>] [--context <place or viewpoint id>]
//!                [--open] [--data-dir <dir>] [--manifests <dir>] [--no-cache] [--alternatives <n>] [--pretty]
//! ```
//!
//! It parses the utterance with the fast-path parser (as `snow-globe-parse` does: `--now`,
//! `--machine-tz` and `--context` mean the same), loads the library index over the data dir
//! (`$SNOW_GLOBE_DATA_DIR`, else the default, with the data-dir checks; or `--data-dir`), matches
//! the query's viewpoint and valid time, decodes the chosen frame, and prints one JSON object: the
//! query, the frame's path, the caption, the distance, what it matched on, and the stage timings.
//!
//! - Only frames available at `--now` qualify: taken before it and, for a live capture, fetched by
//!   it (PRD §9.1 "Leakage", §13.1; `tier0::matcher`), so a replayed ask never sees a live frame
//!   the app could not yet have had. An archived frame counts from its image time, as in eval.
//! - A one-shot command has no background prefetch, so the decode here is on demand and is
//!   reported as such (`"prefetched": false`); in the app the frame is decoded before key-up
//!   ([`crate::tier0::decode`]).
//! - If the chosen frame's image cannot be decoded, or its path is refused (a link, a quarantined
//!   file), the next closest qualifying frame is tried, up to [`FALLBACK_FRAMES`] in all; each one
//!   skipped is listed with why (`decode.skipped`). If none of them decodes, the command says so
//!   and exits 5; nothing is opened.
//! - `--open` opens the frame with macOS `open`: the stored full frame when its path passes the
//!   same checks, else the shown image that was decoded; never any other file.
//!
//! Exit codes: 0 a frame qualifies and decodes; 2 the utterance did not parse (the parser's error
//! JSON, as `snow-globe-parse`); **3 no frame qualifies**, said plainly on stdout and stderr; 4 the
//! library is unavailable (data dir refused or unreadable, or the held-out split refused); **5
//! frames qualify but none of the closest [`FALLBACK_FRAMES`] could be read**, said plainly; 64
//! usage.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Instant;

use chrono::{DateTime, SecondsFormat, Utc};
use chrono_tz::Tz;
use serde::Serialize;

use crate::cli::{CliOutput, parse_now_arg};
use crate::datadir::{check_data_dir, data_dir};
use crate::parser::{LastPlace, ParseContext, Parser};
use crate::places::PlaceTable;
use crate::query::{Query, fmt_utc};
use crate::resolve::TimeConfig;
use crate::tier0::camera::CameraTable;
use crate::tier0::caption::{Caption, tier0_caption};
use crate::tier0::decode::{DecodeTiming, DecodedSet, Unreadable, first_ready};
use crate::tier0::heldout::HeldOutRules;
use crate::tier0::library::{CacheMode, ColourMode, Entry, EntrySource, ImageRefusal, Library};
use crate::tier0::matcher::{
    DEFAULT_ALTERNATIVES, Delta, Descriptor, MatchOutcome, MatchRequest, MatchTerm, NoFrame,
    Scored, Terms, find,
};
use crate::tier0::sidecar::Exclusion;

/// The note every match carries while the weather class waits for extractor v0.
pub const WEATHER_CLASS_NOTE: &str = "not used: the library has no weather class until extractor v0 \
labels it (TODO(M1.5)); the match is on Sun position and day of year, the descriptor without its \
weather class";

/// A frame qualifies.
pub const EXIT_OK: i32 = 0;
/// The utterance did not parse.
pub const EXIT_PARSE_ERROR: i32 = 2;
/// No frame qualifies.
pub const EXIT_NO_FRAME: i32 = 3;
/// The library is unavailable.
pub const EXIT_LIBRARY: i32 = 4;
/// Frames qualify, but none of the closest [`FALLBACK_FRAMES`] could be read.
pub const EXIT_FRAME_UNREADABLE: i32 = 5;
/// Bad arguments (BSD `EX_USAGE`).
pub const EXIT_USAGE: i32 = 64;
/// The most alternatives `--alternatives` may ask for.
pub const MAX_ALTERNATIVES: usize = 50;
/// How many of the closest qualifying frames are tried before the command gives up on reading
/// one (module docs).
pub const FALLBACK_FRAMES: usize = 5;

/// The usage text.
pub const USAGE: &str = "usage: snow-globe-ask \"<utterance>\" [--now <RFC3339>] [--machine-tz <IANA>] \
[--context <place_id|viewpoint_id>] [--open] [--data-dir <dir>] [--manifests <dir>] [--no-cache] \
[--alternatives <n>] [--pretty]";

fn usage(msg: &str) -> CliOutput {
    CliOutput {
        code: EXIT_USAGE,
        stdout: String::new(),
        stderr: format!("snow-globe-ask: {msg}\n{USAGE}\n"),
    }
}

fn ms(t: Instant) -> f64 {
    (t.elapsed().as_secs_f64() * 1e6).round() / 1e3
}

fn emit<T: Serialize + ?Sized>(v: &T, pretty: bool, code: i32, stderr: String) -> CliOutput {
    let text = if pretty {
        serde_json::to_string_pretty(v)
    } else {
        serde_json::to_string(v)
    };
    CliOutput {
        code,
        stdout: format!("{}\n", text.unwrap_or_default()),
        stderr,
    }
}

/// The frame part of the output.
#[derive(Debug, Serialize)]
pub struct FrameOut<'a> {
    /// The image Tier 0 shows (absolute path).
    pub path: String,
    /// The stored full-size frame, when there is one (absolute path).
    pub full_frame_path: Option<String>,
    /// The library key.
    pub key: &'a str,
    /// Live capture or NPS archive.
    pub source: EntrySource,
    /// When it was taken, UTC.
    pub taken_utc: String,
    /// When it was taken, in the camera's zone.
    pub taken_local: String,
    /// When it became available (fetched, for a live capture), UTC with milliseconds.
    pub available_utc: String,
    /// The Sun's altitude in the photo, degrees.
    pub sun_altitude_deg: f64,
    /// The Sun's azimuth relative to the view centre in the photo, degrees.
    pub sun_azimuth_rel_deg: f64,
    /// Local day of year.
    pub day_of_year: u32,
    /// Colour or infrared.
    pub colour_mode: ColourMode,
    /// Flagged stale by capture.
    pub stale: bool,
    /// The sidecars' flags.
    pub flags: &'a [String],
    /// The shown image's size, when known.
    pub image_px: (Option<u32>, Option<u32>),
    /// The manifest's attribution.
    pub licence: &'a str,
}

/// An alternative frame.
#[derive(Debug, Serialize)]
pub struct AltOut {
    /// The library key.
    pub key: String,
    /// Its distance.
    pub distance: f64,
    /// When it was taken, UTC.
    pub taken_utc: String,
    /// Its image (absolute path).
    pub path: String,
}

/// The Tier 0 part of the output.
#[derive(Debug, Serialize)]
pub struct Tier0Out<'a> {
    /// The chosen frame.
    pub frame: FrameOut<'a>,
    /// Its caption.
    pub caption: Caption,
    /// Its distance (`tier0::matcher` docs).
    pub distance: f64,
    /// Its rank among the qualifying frames: 0 for the closest, more when closer ones could not
    /// be read (`decode.skipped`).
    pub rank: usize,
    /// The descriptor terms used.
    pub matched_on: &'a [MatchTerm],
    /// The distance's terms.
    pub terms: Terms,
    /// Frame minus query.
    pub delta: Delta,
    /// Always false for now.
    pub weather_class_used: bool,
    /// Why.
    pub weather_class: &'static str,
    /// The query's descriptor.
    pub query_descriptor: Descriptor,
    /// Candidate frames (available at `--now`).
    pub candidates: usize,
    /// The next closest frames after the chosen one.
    pub alternatives: Vec<AltOut>,
}

/// The decode part of the output.
#[derive(Debug, Default, Serialize)]
pub struct DecodeOut {
    /// False in this command: the frame is decoded on demand (module docs).
    pub prefetched: bool,
    /// The stored image's size.
    pub source_px: Option<(u32, u32)>,
    /// The decoded size.
    pub decoded_px: Option<(u32, u32)>,
    /// Where the chosen frame's decode time went.
    pub timing_ms: Option<DecodeTiming>,
    /// Closer frames that could not be read, closest first, with why.
    pub skipped: Vec<Unreadable>,
}

/// The library part of the output.
#[derive(Debug, Serialize)]
pub struct LibraryOut<'a> {
    /// The data dir.
    pub data_dir: &'a str,
    /// Entries in the library.
    pub entries: usize,
    /// Entries at the asked viewpoint.
    pub viewpoint_entries: usize,
    /// Sidecars excluded at the asked viewpoint, by reason.
    pub excluded_at_viewpoint: Option<&'a BTreeMap<Exclusion, usize>>,
    /// The cache's status.
    pub cache: Option<&'a str>,
    /// The held-out split's version.
    pub split_version: u32,
}

/// Stage timings, milliseconds.
#[derive(Debug, Serialize)]
pub struct TimingsOut {
    /// The parser.
    pub parse: f64,
    /// Loading the library (cache check included).
    pub index: f64,
    /// The match (the query's ephemeris included).
    #[serde(rename = "match")]
    pub match_ms: f64,
    /// The on-demand decode, every frame tried included.
    pub decode: Option<f64>,
    /// The whole command.
    pub total: f64,
}

/// What `--open` did.
#[derive(Debug, Serialize)]
pub struct OpenedOut {
    /// The file opened (every link resolved), or the one refused.
    pub path: String,
    /// Why it failed, if it did.
    pub error: Option<String>,
    /// Why the shown image was opened rather than the stored full frame, when it was.
    pub note: Option<String>,
}

/// Frames qualify but none could be read.
#[derive(Debug, Serialize)]
pub struct UnreadableOut<'a> {
    /// A plain sentence for people.
    pub message: String,
    /// The frames tried, closest first, with why each failed.
    pub tried: &'a [Unreadable],
    /// Candidate frames (available at `--now`).
    pub candidates: usize,
}

#[derive(Serialize)]
struct MatchOut<'a> {
    status: &'static str,
    query: &'a Query,
    tier0: Tier0Out<'a>,
    decode: DecodeOut,
    opened: Option<OpenedOut>,
    library: LibraryOut<'a>,
    timings_ms: TimingsOut,
}

#[derive(Serialize)]
struct NoFrameOut<'a> {
    status: &'static str,
    query: &'a Query,
    tier0: &'a NoFrame,
    library: LibraryOut<'a>,
    timings_ms: TimingsOut,
}

#[derive(Serialize)]
struct FrameUnreadableOut<'a> {
    status: &'static str,
    query: &'a Query,
    tier0: UnreadableOut<'a>,
    library: LibraryOut<'a>,
    timings_ms: TimingsOut,
}

#[derive(Serialize)]
struct UnavailableOut<'a> {
    status: &'static str,
    query: &'a Query,
    error: String,
}

/// Open a file with macOS `open` (the `--open` flag).
pub fn open_with_macos(path: &Path) -> Result<(), String> {
    if !cfg!(target_os = "macos") {
        return Err("--open needs macOS `open`".to_string());
    }
    let status = std::process::Command::new("open")
        .arg(path)
        .status()
        .map_err(|e| format!("open: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("open exited with {status}"))
    }
}

/// Run the command on `args` (without the program name), opening files with macOS `open`.
pub fn run(args: &[String]) -> CliOutput {
    run_with(args, &open_with_macos)
}

/// What `--open` opens for an entry (module docs): its stored full frame when that path passes
/// [`Library::image_path`], else its shown image, with a note saying why; an error when neither
/// passes.
pub fn open_target(
    library: &Library,
    entry: &Entry,
) -> (Result<PathBuf, ImageRefusal>, Option<String>) {
    match entry.full_frame.as_ref().map(|f| library.image_path(f)) {
        Some(Ok(p)) => (Ok(p), None),
        Some(Err(refusal)) => (
            library.image_path(&entry.image),
            Some(format!(
                "the full frame is refused ({refusal}); the shown image is opened instead"
            )),
        ),
        None => (library.image_path(&entry.image), None),
    }
}

struct Args<'a> {
    utterance: &'a str,
    now: DateTime<Utc>,
    context: Option<&'a str>,
    open: bool,
    data_dir: Option<&'a str>,
    manifests: Option<&'a str>,
    cache: CacheMode,
    alternatives: usize,
    pretty: bool,
}

fn parse_args(args: &[String]) -> Result<Args<'_>, CliOutput> {
    let mut utterance = None;
    let mut now_arg = None;
    let mut tz_arg = None;
    let mut out = Args {
        utterance: "",
        now: Utc::now(),
        context: None,
        open: false,
        data_dir: None,
        manifests: None,
        cache: CacheMode::Use,
        alternatives: DEFAULT_ALTERNATIVES,
        pretty: false,
    };
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let mut value = |name: &str| {
            it.next()
                .map(String::as_str)
                .ok_or_else(|| usage(&format!("{name} needs a value")))
        };
        match a.as_str() {
            "--now" => now_arg = Some(value("--now")?),
            "--machine-tz" => tz_arg = Some(value("--machine-tz")?),
            "--context" => out.context = Some(value("--context")?),
            "--data-dir" => out.data_dir = Some(value("--data-dir")?),
            "--manifests" => out.manifests = Some(value("--manifests")?),
            "--alternatives" => {
                let v = value("--alternatives")?;
                out.alternatives = v
                    .parse()
                    .ok()
                    .filter(|n| *n <= MAX_ALTERNATIVES)
                    .ok_or_else(|| {
                        usage(&format!(
                            "--alternatives {v:?} is not a count from 0 to {MAX_ALTERNATIVES}"
                        ))
                    })?;
            }
            "--open" => out.open = true,
            "--no-cache" => out.cache = CacheMode::Off,
            "--pretty" => out.pretty = true,
            "-h" | "--help" => {
                return Err(CliOutput {
                    code: 0,
                    stdout: format!("{USAGE}\n"),
                    stderr: String::new(),
                });
            }
            s if s.starts_with("--") => return Err(usage(&format!("unknown option {s}"))),
            s => {
                if utterance.replace(s).is_some() {
                    return Err(usage("give the utterance as one quoted argument"));
                }
            }
        }
    }
    out.utterance = utterance.ok_or_else(|| usage("missing the utterance"))?;
    let machine_tz = match tz_arg {
        None => None,
        Some(name) => Some(
            name.parse::<Tz>()
                .map_err(|_| usage(&format!("unknown IANA zone {name:?}")))?,
        ),
    };
    if let Some(text) = now_arg {
        out.now = parse_now_arg(text, machine_tz).map_err(|m| usage(&m))?;
    }
    Ok(out)
}

/// [`run`] with the opener given (tests pass one that records the path).
#[allow(clippy::too_many_lines)]
pub fn run_with(args: &[String], open: &dyn Fn(&Path) -> Result<(), String>) -> CliOutput {
    let t_total = Instant::now();
    let a = match parse_args(args) {
        Ok(a) => a,
        Err(out) => return out,
    };

    // Parse (PRD §7.3).
    let parser_owned;
    let cameras_owned;
    let (parser, cameras): (&Parser, &CameraTable) = match a.manifests {
        None => (Parser::embedded(), CameraTable::embedded()),
        Some(dir) => {
            let dir = Path::new(dir);
            match (PlaceTable::from_dir(dir), CameraTable::from_dir(dir)) {
                (Ok(places), Ok(cams)) => {
                    parser_owned = Parser::new(places, TimeConfig::default());
                    cameras_owned = cams;
                    (&parser_owned, &cameras_owned)
                }
                (Err(e), _) | (_, Err(e)) => return usage(&e.to_string()),
            }
        }
    };
    let last = match a.context {
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
    let t = Instant::now();
    let parsed = parser.parse(
        a.utterance,
        &ParseContext {
            now: a.now,
            last: last.as_ref(),
        },
    );
    let parse_ms = ms(t);
    let query = match parsed {
        Ok(q) => q,
        Err(e) => {
            return emit(
                &e,
                a.pretty,
                EXIT_PARSE_ERROR,
                format!("snow-globe-ask: {e}\n"),
            );
        }
    };

    // The library (PRD §9.1 "Library").
    let t = Instant::now();
    let library = (|| -> Result<Library, String> {
        let dir: PathBuf = match a.data_dir {
            Some(d) => {
                let home = std::env::var_os("HOME")
                    .map(PathBuf::from)
                    .unwrap_or_default();
                check_data_dir(Path::new(d), &home).map_err(|e| e.to_string())?
            }
            None => data_dir().map_err(|e| e.to_string())?,
        };
        let rules = HeldOutRules::committed().map_err(|e| e.to_string())?;
        Library::build(&dir, cameras, rules, a.cache).map_err(|e| e.to_string())
    })();
    let index_ms = ms(t);
    let library = match library {
        Ok(l) => l,
        Err(e) => {
            let stderr = format!("snow-globe-ask: {e}\n");
            let v = UnavailableOut {
                status: "library_unavailable",
                query: &query,
                error: e,
            };
            return emit(&v, a.pretty, EXIT_LIBRARY, stderr);
        }
    };
    let report = library.report();
    let library_out = || LibraryOut {
        data_dir: &report.data_dir,
        entries: report.entries,
        viewpoint_entries: library.viewpoint_indexes(&query.viewpoint_id).len(),
        excluded_at_viewpoint: report
            .viewpoints
            .get(&query.viewpoint_id)
            .map(|v| &v.excluded),
        cache: report.cache.as_ref().map(|c| c.status.as_str()),
        split_version: report.split_version,
    };

    // The match (PRD §9.1 "Query"): only frames available at the time of asking qualify. Enough
    // alternatives are asked for to fall back on when the closest cannot be read.
    let mut req = MatchRequest::new(&query.viewpoint_id, query.valid_time_utc);
    req.as_of = Some(a.now);
    req.alternatives = a.alternatives.max(FALLBACK_FRAMES - 1);
    let t = Instant::now();
    let outcome = find(&library, cameras, &req);
    let match_ms = ms(t);

    let m = match outcome {
        MatchOutcome::Matched(m) => m,
        MatchOutcome::NoFrame(n) => {
            let v = NoFrameOut {
                status: "no_frame",
                query: &query,
                tier0: &n,
                library: library_out(),
                timings_ms: TimingsOut {
                    parse: parse_ms,
                    index: index_ms,
                    match_ms,
                    decode: None,
                    total: ms(t_total),
                },
            };
            return emit(&v, a.pretty, EXIT_NO_FRAME, format!("{}\n", n.message));
        }
    };
    let Some(camera) = cameras.get(&query.viewpoint_id) else {
        let v = UnavailableOut {
            status: "library_unavailable",
            query: &query,
            error: format!("no camera for {}", query.viewpoint_id),
        };
        return emit(&v, a.pretty, EXIT_LIBRARY, String::new());
    };

    // Decode the closest frame that can be read (on demand here; prefetched in the app).
    let ranked: Vec<&Scored> = std::iter::once(&m.best).chain(&m.alternatives).collect();
    let tried: Vec<usize> = ranked
        .iter()
        .take(FALLBACK_FRAMES)
        .map(|s| s.index)
        .collect();
    let t = Instant::now();
    let (ready, skipped) = first_ready(&library, &DecodedSet::new(), &tried);
    let decode_ms = ms(t);
    let timings = |decode: f64| TimingsOut {
        parse: parse_ms,
        index: index_ms,
        match_ms,
        decode: Some(decode),
        total: ms(t_total),
    };
    let Some(ready) = ready else {
        let name = camera.display_name();
        let message = match skipped.len() {
            1 => format!(
                "No real photo could be shown: the one frame from {name} that qualifies could \
                 not be read."
            ),
            n => format!(
                "No real photo could be shown: the {n} closest frames from {name} that qualify \
                 could not be read."
            ),
        };
        let v = FrameUnreadableOut {
            status: "frame_unreadable",
            query: &query,
            tier0: UnreadableOut {
                message: message.clone(),
                tried: &skipped,
                candidates: m.candidates,
            },
            library: library_out(),
            timings_ms: timings(decode_ms),
        };
        return emit(&v, a.pretty, EXIT_FRAME_UNREADABLE, format!("{message}\n"));
    };
    let chosen = ranked[ready.position];
    let entry = &library.entries()[chosen.index];
    let caption = tier0_caption(
        entry,
        camera,
        chosen,
        &m.query,
        &m.matched_on,
        m.weather_class_used,
        query.valid_time_utc,
    );
    let path = library.path_of(&entry.image);
    let full_path = entry.full_frame.as_ref().map(|f| library.path_of(f));
    let decode_out = DecodeOut {
        prefetched: false,
        source_px: Some((ready.image.source_width, ready.image.source_height)),
        decoded_px: Some((ready.image.width, ready.image.height)),
        timing_ms: ready.timing,
        skipped: skipped.clone(),
    };

    let mut stderr = String::new();
    let opened = a.open.then(|| {
        let (target, note) = open_target(&library, entry);
        let out = match target {
            Ok(p) => OpenedOut {
                error: open(&p).err(),
                path: p.display().to_string(),
                note,
            },
            Err(refusal) => OpenedOut {
                path: path.display().to_string(),
                error: Some(refusal.to_string()),
                note,
            },
        };
        if let Some(e) = &out.error {
            stderr.push_str(&format!("snow-globe-ask: {e}\n"));
        }
        out
    });
    let alternatives: Vec<AltOut> = ranked[ready.position + 1..]
        .iter()
        .take(a.alternatives)
        .map(|s| {
            let e = &library.entries()[s.index];
            AltOut {
                key: s.key.clone(),
                distance: s.distance,
                taken_utc: fmt_utc(&e.taken_utc),
                path: library.path_of(&e.image).display().to_string(),
            }
        })
        .collect();
    let available = DateTime::from_timestamp_millis(entry.available_unix_ms)
        .unwrap_or(entry.taken_utc)
        .to_rfc3339_opts(SecondsFormat::Millis, true);
    let v = MatchOut {
        status: "match",
        query: &query,
        tier0: Tier0Out {
            frame: FrameOut {
                path: path.display().to_string(),
                full_frame_path: full_path.map(|p| p.display().to_string()),
                key: &entry.key,
                source: entry.source,
                taken_utc: fmt_utc(&entry.taken_utc),
                taken_local: entry
                    .taken_utc
                    .with_timezone(&camera.tz)
                    .fixed_offset()
                    .to_rfc3339_opts(SecondsFormat::Secs, false),
                available_utc: available,
                sun_altitude_deg: entry.sun_altitude_deg,
                sun_azimuth_rel_deg: entry.sun_azimuth_rel_deg,
                day_of_year: entry.day_of_year,
                colour_mode: entry.colour_mode,
                stale: entry.stale,
                flags: &entry.flags,
                image_px: (entry.image.width, entry.image.height),
                licence: &entry.licence,
            },
            caption,
            distance: chosen.distance,
            rank: ready.position,
            matched_on: &m.matched_on,
            terms: chosen.terms,
            delta: chosen.delta,
            weather_class_used: m.weather_class_used,
            weather_class: WEATHER_CLASS_NOTE,
            query_descriptor: m.query,
            candidates: m.candidates,
            alternatives,
        },
        decode: decode_out,
        opened,
        library: library_out(),
        timings_ms: timings(decode_ms),
    };
    emit(&v, a.pretty, EXIT_OK, stderr)
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
        assert_eq!(run(&args(&["x", "--now"])).code, EXIT_USAGE);
        assert_eq!(
            run(&args(&["x", "--alternatives", "many"])).code,
            EXIT_USAGE
        );
        assert_eq!(run(&args(&["x", "--bogus"])).code, EXIT_USAGE);
        assert_eq!(run(&args(&["x", "y"])).code, EXIT_USAGE);
        let help = run(&args(&["--help"]));
        assert_eq!(help.code, 0);
        assert!(help.stdout.starts_with("usage: snow-globe-ask"));
    }

    #[test]
    fn a_parse_error_is_the_parsers_json() {
        let out = run(&args(&[
            "Golden Gate at 9",
            "--now",
            "2026-11-19T12:00:00-06:00",
        ]));
        assert_eq!(out.code, EXIT_PARSE_ERROR, "{out:?}");
        assert!(
            out.stdout.starts_with(r#"{"error":"no_match""#),
            "{}",
            out.stdout
        );
    }

    #[cfg(unix)]
    #[test]
    fn open_prefers_the_full_frame_and_never_opens_a_link() {
        use crate::tier0::camera::CameraTable;
        use crate::tier0::library::ImageRef;
        use std::os::unix::fs::symlink;
        let tmp = tempfile::tempdir().expect("tempdir");
        let rel = |tree: &str| format!("{tree}/point-reyes.beach/2026-11-22/a.jpg");
        for tree in ["frames", "thumbs"] {
            let p = tmp.path().join(rel(tree));
            std::fs::create_dir_all(p.parent().expect("parent")).expect("mkdir");
            std::fs::write(&p, b"x").expect("write");
        }
        let image = |tree: &str| ImageRef {
            rel_path: rel(tree),
            width: None,
            height: None,
        };
        let cam = CameraTable::embedded()
            .get("point-reyes.beach")
            .expect("camera");
        let t = DateTime::from_timestamp(1_795_000_000, 0).expect("t");
        let mut e = Entry::new(
            "live:point-reyes.beach/a".to_string(),
            cam,
            EntrySource::Live,
            t,
            10.0,
            200.0,
            image("thumbs"),
            ColourMode::Colour,
        );
        e.full_frame = Some(image("frames"));
        let lib = Library::from_entries(tmp.path(), vec![e]);
        let entry = &lib.entries()[0];
        let (target, note) = open_target(&lib, entry);
        assert!(target.expect("full frame").ends_with(rel("frames")));
        assert!(note.is_none());
        // The full frame swapped for a link after the scan: the shown image, and a note why.
        let full = tmp.path().join(rel("frames"));
        std::fs::remove_file(&full).expect("rm");
        symlink(tmp.path().join(rel("thumbs")), &full).expect("link");
        let (target, note) = open_target(&lib, entry);
        assert!(target.expect("shown image").ends_with(rel("thumbs")));
        assert!(
            note.is_some_and(|n| n.contains("full frame is refused")),
            "a note"
        );
        // Both links: nothing is opened.
        let shown = tmp.path().join(rel("thumbs"));
        std::fs::remove_file(&shown).expect("rm");
        symlink(&full, &shown).expect("link");
        assert!(open_target(&lib, entry).0.is_err());
    }

    #[test]
    fn a_refused_data_dir_is_library_unavailable() {
        // The crate's own folder is inside the git work tree, so it is refused.
        let here = env!("CARGO_MANIFEST_DIR");
        let out = run(&args(&[
            "rainier at noon tomorrow",
            "--now",
            "2026-11-19T12:00:00-08:00",
            "--data-dir",
            here,
        ]));
        assert_eq!(out.code, EXIT_LIBRARY, "{out:?}");
        assert!(out.stdout.contains("library_unavailable"));
    }
}
