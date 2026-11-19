//! The parser fast path (PRD §7.3): utterance → [`Query`], with no language model.
//!
//! 1. **Normalise** ([`crate::normalize::tokenize`]): lower-case, punctuation, contractions.
//!    Exact place names are protected first; then multi-word carriers ("show me", "what about",
//!    "is it going to be") are removed, repeatedly, until none is left.
//! 2. **Time**: the hand-written grammar ([`crate::timeparse::extract`]) consumes the time words.
//! 3. **Place**: single filler words are dropped from what is left (never inside a place name)
//!    and the rest is matched against the alias table ([`PlaceTable`], PRD §7.5). "`from <x>`"
//!    picks a viewpoint. No place words → the last place asked about (`place_source: context`).
//! 4. **Resolve** the time at the place ([`crate::resolve`], PRD §7.4) and emit the query.
//!
//! **Every word is accounted for.** Anything the grammar and the place names cannot explain is
//! [`ParseError::Unrecognised`]: left-over time words ("next week", "after", "sunsets", "on
//! thanksgiving"), two times at once, and words next to a place name that are not part of it
//! ("rainier beach", "rainier tomorrow arvo"). The PRD hands those to the on-device LLM fallback,
//! which is TODO(M3), so the fallback rate counts them. The parser never guesses a place, and
//! never drops a word it does not know.
//!
//! **Confidence** is a fixed heuristic, not yet calibrated (TODO(M3): calibrate on the replay
//! set): 1.0 for an alias or exact match, the score for fuzzy or phonetic matches, 0.95 for a
//! place carried over from context; × 0.6 for a place close call, × 0.97 for an AM/PM chip and
//! × 0.98 for a daylight-saving adjustment; rounded to two decimals.

use std::sync::OnceLock;

use chrono::{DateTime, FixedOffset, Utc};
use serde::{Deserialize, Serialize};

use crate::normalize::{MULTIWORD_FILLERS, is_filler, tokenize};
use crate::places::{Candidate, MatchOutcome, PREFETCH_MIN_SCORE, PlaceTable};
use crate::query::{
    Ambiguity, AmbiguityField, Interval, ParsePath, PlaceMatchInfo, PlaceSource, PlaceStage, Query,
    TimeSource, fmt_local,
};
use crate::resolve::{Flag, PlaceClock, ResolveError, TimeConfig, resolve};
use crate::solar::SolarEvent;
use crate::timeparse::{TimeExpr, extract, is_time_word};

/// The place of the previous query, for follow-ups (PRD §7.4 "A time with no place").
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LastPlace {
    /// `place_id`.
    pub place_id: String,
    /// `viewpoint_id`.
    pub viewpoint_id: String,
}

/// What the parser needs besides the words: the instant "now" and the previous place.
#[derive(Debug, Clone, Copy)]
pub struct ParseContext<'a> {
    /// Now, as an instant. The machine's zone plays no other part.
    pub now: DateTime<Utc>,
    /// The previous query's place, if any.
    pub last: Option<&'a LastPlace>,
}

/// Why an out-of-range time was refused (PRD §5.2 "Out of range").
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RangeReason {
    /// Before now.
    Past,
    /// More than 7 days ahead.
    BeyondHorizon,
}

impl std::fmt::Display for RangeReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            RangeReason::Past => "in the past",
            RangeReason::BeyondHorizon => "beyond the 7-day horizon",
        })
    }
}

/// Why no query was produced. Serialised with an `error` tag for the CLI and the overlay.
#[derive(Debug, Clone, PartialEq, thiserror::Error, Serialize)]
#[serde(tag = "error", rename_all = "snake_case")]
pub enum ParseError {
    /// A time with no place, and no earlier place to follow up on.
    #[error("no place was named and there is no earlier place to follow up on")]
    NoPlace,
    /// Place words that match no known place: the three closest, never a guess (PRD §7.5).
    #[error("no known place matches {text:?}")]
    NoMatch {
        /// The place words heard.
        text: String,
        /// The three closest places.
        closest: Vec<Candidate>,
    },
    /// "`from <x>`" names no viewpoint of the place.
    #[error("{text:?} is not a viewpoint of {place_id}")]
    UnknownViewpoint {
        /// The place.
        place_id: String,
        /// The viewpoint words heard.
        text: String,
        /// The place's viewpoints.
        viewpoints: Vec<String>,
    },
    /// The time is in the past or beyond the horizon (PRD §5.2).
    #[error("{requested} is {reason}")]
    OutOfRange {
        /// Past or beyond the horizon.
        reason: RangeReason,
        /// The place (kept so a follow-up can reuse it).
        place_id: String,
        /// The viewpoint.
        viewpoint_id: String,
        /// The time asked for, in the place's zone.
        requested: String,
        /// The latest valid time (now + 7 days), offered instead.
        latest_valid: String,
    },
    /// Words the time grammar and the place names cannot explain (time words outside the
    /// grammar, two times at once, or words beside a place name that are not part of it); this
    /// goes to the LLM fallback (TODO(M3)).
    #[error("time words not understood: {reason}")]
    Unrecognised {
        /// What the grammar could not handle.
        reason: String,
    },
    /// A date that does not exist ("february 30").
    #[error("no such date: {text:?}")]
    InvalidDate {
        /// The date words heard.
        text: String,
    },
    /// The Sun does not reach the event's altitude that day.
    #[error("no {event:?} at {viewpoint_id} on {date}")]
    NoSolarEvent {
        /// The event.
        event: SolarEvent,
        /// The viewpoint.
        viewpoint_id: String,
        /// The local date.
        date: String,
    },
}

impl ParseError {
    /// Short machine name of the error (`no_match`, `out_of_range`, ...).
    pub fn kind(&self) -> &'static str {
        match self {
            ParseError::NoPlace => "no_place",
            ParseError::NoMatch { .. } => "no_match",
            ParseError::UnknownViewpoint { .. } => "unknown_viewpoint",
            ParseError::OutOfRange { .. } => "out_of_range",
            ParseError::Unrecognised { .. } => "unrecognised",
            ParseError::InvalidDate { .. } => "invalid_date",
            ParseError::NoSolarEvent { .. } => "no_solar_event",
        }
    }

    /// JSON text: `{"error": "<kind>", ...}`.
    pub fn to_json(&self) -> String {
        serde_json::to_string(self).unwrap_or_default()
    }
}

/// The words of an utterance after normalisation and time extraction.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Analysis {
    /// The normalised words after carrier phrases were removed (fillers still in).
    pub normalized: Vec<String>,
    /// Canonical words: normalised, fillers removed, original order (time and place words).
    pub kept: Vec<String>,
    /// The time expression.
    pub time: TimeExpr,
    /// Set when the grammar found two times of one kind.
    pub conflict: Option<String>,
    /// Place words (before "from").
    pub place_words: Vec<String>,
    /// Viewpoint words (after "from"), if "from" was followed by any.
    pub viewpoint_words: Option<Vec<String>>,
    /// Time words the grammar did not consume ("next week", "after").
    pub stray_time_words: Vec<String>,
}

impl Analysis {
    /// True if everything the parser reads from the two analyses is the same.
    fn parses_like(&self, other: &Analysis) -> bool {
        self.time == other.time
            && self.conflict == other.conflict
            && self.place_words == other.place_words
            && self.viewpoint_words == other.viewpoint_words
            && self.stray_time_words == other.stray_time_words
    }
}

/// A place hint from a partial transcript (PRD §7.2 "Prefetch on partials").
#[derive(Debug, Clone, PartialEq)]
pub struct PrefetchHint {
    /// The viewpoint whose assets to load.
    pub viewpoint_id: String,
    /// Match score (≥ [`PREFETCH_MIN_SCORE`]).
    pub score: f64,
}

#[derive(Debug, Clone)]
struct PlacePick {
    viewpoint: usize,
    source: PlaceSource,
    stage: PlaceStage,
    score: f64,
    close_call: Option<Ambiguity>,
}

/// The fast-path parser: an alias table plus the time configuration.
#[derive(Debug, Clone)]
pub struct Parser {
    places: PlaceTable,
    config: TimeConfig,
}

impl Parser {
    /// A parser over `places` with `config`.
    pub fn new(places: PlaceTable, config: TimeConfig) -> Parser {
        Parser { places, config }
    }

    /// The parser over the manifests compiled into the crate, with default hours.
    pub fn embedded() -> &'static Parser {
        static PARSER: OnceLock<Parser> = OnceLock::new();
        PARSER.get_or_init(|| Parser::new(PlaceTable::embedded().clone(), TimeConfig::default()))
    }

    /// The alias table.
    pub fn places(&self) -> &PlaceTable {
        &self.places
    }

    /// The time configuration.
    pub fn config(&self) -> &TimeConfig {
        &self.config
    }

    /// Mark the words inside exact place names (filler removal and the time grammar keep out).
    fn protect(&self, words: &[String]) -> Vec<bool> {
        let refs: Vec<&str> = words.iter().map(String::as_str).collect();
        let mut protected = vec![false; words.len()];
        for (s, e) in self.places.name_spans(&refs) {
            protected[s..e].iter_mut().for_each(|p| *p = true);
        }
        protected
    }

    /// Remove multi-word carriers outside protected names until none is left (removing one can
    /// join two others: "show what is me" → "show me"), so that the result is a fixed point.
    fn strip_carriers(&self, mut words: Vec<String>) -> (Vec<String>, Vec<bool>) {
        loop {
            let protected = self.protect(&words);
            let mut out: Vec<String> = Vec::with_capacity(words.len());
            let mut changed = false;
            let mut i = 0;
            'scan: while i < words.len() {
                for phrase in MULTIWORD_FILLERS {
                    let end = i + phrase.len();
                    if end <= words.len()
                        && words[i..end]
                            .iter()
                            .map(String::as_str)
                            .eq(phrase.iter().copied())
                        && !protected[i..end].iter().any(|&p| p)
                    {
                        i = end;
                        changed = true;
                        continue 'scan;
                    }
                }
                out.push(std::mem::take(&mut words[i]));
                i += 1;
            }
            if !changed {
                return (out, protected);
            }
            words = out;
        }
    }

    /// Normalise and split an utterance into time words and place words (PRD §7.3 steps 1–2).
    pub fn analyse(&self, utterance: &str) -> Analysis {
        let (normalized, prot) = self.strip_carriers(tokenize(utterance));
        let words: Vec<&str> = normalized.iter().map(String::as_str).collect();
        let ex = extract(&words, &prot);
        // Canonical words, each marked true if it is a "from" outside the time expression.
        let mut kept: Vec<(String, bool)> = Vec::new();
        // Words left for the place matcher.
        let mut rest: Vec<&str> = Vec::new();
        let mut stray = Vec::new();
        for (k, w) in words.iter().enumerate() {
            if ex.consumed[k] {
                kept.push((w.to_string(), false));
            } else if prot[k] || !is_filler(w) {
                if !prot[k] && is_time_word(w) {
                    stray.push(w.to_string());
                }
                rest.push(w);
                kept.push((w.to_string(), *w == "from"));
            }
        }
        // "`from <x>`" picks a viewpoint (PRD §7.5). A "from" with nothing after it means nothing,
        // and so do repeats: only the first "from" before viewpoint words stays canonical.
        let (place_words, viewpoint_words) = match rest.iter().position(|w| *w == "from") {
            Some(p) => {
                let view: Vec<String> = rest[p + 1..]
                    .iter()
                    .filter(|w| **w != "from")
                    .map(|s| s.to_string())
                    .collect();
                (
                    rest[..p].iter().map(|s| s.to_string()).collect(),
                    (!view.is_empty()).then_some(view),
                )
            }
            None => (rest.iter().map(|s| s.to_string()).collect(), None),
        };
        let mut keep_from = viewpoint_words.is_some();
        let kept = kept
            .into_iter()
            .filter(|(_, is_from)| !*is_from || std::mem::take(&mut keep_from))
            .map(|(w, _)| w)
            .collect();
        Analysis {
            normalized,
            kept,
            time: ex.expr,
            conflict: ex.conflict.map(|c| c.to_string()),
            place_words,
            viewpoint_words,
            stray_time_words: stray,
        }
    }

    /// The canonical form of an utterance: normalised, fillers removed (PRD §7.6 "canonical
    /// form stable"; also the key of the fallback's cache, PRD §7.3). It parses to the same query
    /// as the original, and is a fixed point.
    ///
    /// Dropping a filler can join two words into a time the original did not say ("at 4 and 30"
    /// would become "at 4 30"). The candidate is therefore analysed again, and if it does not
    /// parse like the original, the normalised words with their fillers are used instead.
    pub fn canonical(&self, utterance: &str) -> String {
        let a = self.analyse(utterance);
        let candidate = a.kept.join(" ");
        let again = self.analyse(&candidate);
        if again.kept == a.kept && again.parses_like(&a) {
            candidate
        } else {
            a.normalized.join(" ")
        }
    }

    /// Parse an utterance into a query (PRD §7.3).
    pub fn parse(&self, utterance: &str, ctx: &ParseContext<'_>) -> Result<Query, ParseError> {
        let a = self.analyse(utterance);
        if let Some(reason) = &a.conflict {
            return Err(ParseError::Unrecognised {
                reason: reason.clone(),
            });
        }
        if !a.stray_time_words.is_empty() {
            return Err(ParseError::Unrecognised {
                reason: format!(
                    "time words outside the grammar: {}",
                    a.stray_time_words.join(" ")
                ),
            });
        }
        let pick = self.pick_place(&a, ctx)?;
        let vp = &self.places.viewpoints()[pick.viewpoint];
        let place_clock = PlaceClock {
            tz: vp.tz,
            site: &vp.site,
            live_camera: vp.has_live_camera,
        };
        let r = resolve(&a.time, place_clock, ctx.now, &self.config).map_err(|e| {
            let latest = fmt_local(
                &(ctx.now + self.config.max_horizon)
                    .with_timezone(&vp.tz)
                    .fixed_offset(),
            );
            let out = |reason, t: DateTime<FixedOffset>| ParseError::OutOfRange {
                reason,
                place_id: vp.place_id.clone(),
                viewpoint_id: vp.id.clone(),
                requested: fmt_local(&t),
                latest_valid: latest.clone(),
            };
            match e {
                ResolveError::Conflict(reason) => ParseError::Unrecognised {
                    reason: reason.to_string(),
                },
                ResolveError::InvalidDate { .. } => ParseError::InvalidDate {
                    text: a.time.text.clone(),
                },
                ResolveError::NoSolarEvent { event, date } => ParseError::NoSolarEvent {
                    event,
                    viewpoint_id: vp.id.clone(),
                    date: date.to_string(),
                },
                ResolveError::Past { requested } => {
                    out(RangeReason::Past, requested.fixed_offset())
                }
                ResolveError::BeyondHorizon { requested } => {
                    out(RangeReason::BeyondHorizon, requested.fixed_offset())
                }
            }
        })?;

        let mut ambiguities = Vec::new();
        if let Some((chosen, alt)) = r.meridiem {
            ambiguities.push(Ambiguity {
                field: AmbiguityField::Meridiem,
                chosen: if chosen == crate::timeparse::Meridiem::Am {
                    "am"
                } else {
                    "pm"
                }
                .to_string(),
                alternative: fmt_local(&alt.fixed_offset()),
            });
        }
        if let Some(chip) = pick.close_call.clone() {
            ambiguities.push(chip);
        }
        let mut confidence = match pick.stage {
            PlaceStage::Alias | PlaceStage::Exact => 1.0,
            PlaceStage::Fuzzy | PlaceStage::Phonetic => pick.score,
            PlaceStage::Context => 0.95,
        };
        if pick.close_call.is_some() {
            confidence *= 0.6;
        }
        if r.meridiem.is_some() {
            confidence *= 0.97;
        }
        if r.flags.contains(&Flag::DstGap) || r.flags.contains(&Flag::DstFold) {
            confidence *= 0.98;
        }
        let solar = r.solar;
        Ok(Query {
            place_id: vp.place_id.clone(),
            viewpoint_id: vp.id.clone(),
            valid_time: r.instant.fixed_offset(),
            valid_time_utc: r.instant.with_timezone(&Utc),
            tz: vp.tz.name().to_string(),
            time_source: TimeSource {
                kind: r.kind,
                text: a.time.text.clone(),
                rule: r.rule,
                event: solar.map(|s| s.event),
                sun_altitude_deg: solar.map(|s| s.render_altitude_deg),
                interval: solar
                    .and_then(|s| s.interval)
                    .map(|(start, end, da, db)| Interval {
                        start: start.fixed_offset(),
                        end: end.fixed_offset(),
                        start_altitude_deg: da,
                        end_altitude_deg: db,
                    }),
            },
            ambiguities,
            overrides: serde_json::Map::new(),
            path: ParsePath::FastPath,
            confidence: (confidence * 100.0).round() / 100.0,
            place_source: pick.source,
            place_match: PlaceMatchInfo {
                stage: pick.stage,
                score: (pick.score * 100.0).round() / 100.0,
            },
            horizon: r.horizon,
            flags: r.flags,
        })
    }

    fn pick_place(&self, a: &Analysis, ctx: &ParseContext<'_>) -> Result<PlacePick, ParseError> {
        let table = &self.places;
        let left_over = |words: &[String]| ParseError::Unrecognised {
            reason: format!(
                "words that are neither a time nor part of the place name: {}",
                words.join(" ")
            ),
        };
        let outcome = (!a.place_words.is_empty()).then(|| table.match_place(&a.place_words));
        match outcome {
            Some(MatchOutcome::Found {
                best,
                runner_up,
                ambiguous,
                unexplained,
            }) => {
                if !unexplained.is_empty() {
                    return Err(left_over(&unexplained));
                }
                let close_call = ambiguous.then_some(runner_up).flatten().map(|r| Ambiguity {
                    field: AmbiguityField::Place,
                    chosen: table.places()[best.place].id.clone(),
                    alternative: table.places()[r.place].id.clone(),
                });
                let mut pick = PlacePick {
                    viewpoint: best.viewpoint,
                    source: PlaceSource::Utterance,
                    stage: best.stage.into(),
                    score: best.score,
                    close_call,
                };
                if let Some(vw) = &a.viewpoint_words {
                    match table.match_viewpoint(Some(best.place), vw) {
                        MatchOutcome::Found {
                            best: v,
                            unexplained,
                            ..
                        } => {
                            if !unexplained.is_empty() {
                                return Err(left_over(&unexplained));
                            }
                            pick.viewpoint = v.viewpoint;
                        }
                        _ => {
                            let place = &table.places()[best.place];
                            return Err(ParseError::UnknownViewpoint {
                                place_id: place.id.clone(),
                                text: vw.join(" "),
                                viewpoints: place
                                    .viewpoints
                                    .iter()
                                    .map(|&i| table.viewpoints()[i].id.clone())
                                    .collect(),
                            });
                        }
                    }
                }
                Ok(pick)
            }
            Some(MatchOutcome::Miss { closest }) => Err(ParseError::NoMatch {
                text: a.place_words.join(" "),
                closest,
            }),
            Some(MatchOutcome::NothingSaid) | None => {
                if let Some(vw) = &a.viewpoint_words {
                    match table.match_viewpoint(None, vw) {
                        MatchOutcome::Found {
                            best,
                            runner_up,
                            ambiguous,
                            unexplained,
                        } => {
                            if !unexplained.is_empty() {
                                return Err(left_over(&unexplained));
                            }
                            let close_call =
                                ambiguous.then_some(runner_up).flatten().map(|r| Ambiguity {
                                    field: AmbiguityField::Viewpoint,
                                    chosen: table.viewpoints()[best.viewpoint].id.clone(),
                                    alternative: table.viewpoints()[r.viewpoint].id.clone(),
                                });
                            return Ok(PlacePick {
                                viewpoint: best.viewpoint,
                                source: PlaceSource::Utterance,
                                stage: best.stage.into(),
                                score: best.score,
                                close_call,
                            });
                        }
                        MatchOutcome::Miss { closest } => {
                            return Err(ParseError::NoMatch {
                                text: vw.join(" "),
                                closest,
                            });
                        }
                        MatchOutcome::NothingSaid => {}
                    }
                }
                let last = ctx.last.ok_or(ParseError::NoPlace)?;
                let viewpoint = table
                    .viewpoint(&last.viewpoint_id)
                    .map(|(i, _)| i)
                    .or_else(|| {
                        table
                            .place(&last.place_id)
                            .map(|(_, p)| p.default_viewpoint)
                    })
                    .ok_or(ParseError::NoPlace)?;
                Ok(PlacePick {
                    viewpoint,
                    source: PlaceSource::Context,
                    stage: PlaceStage::Context,
                    score: 1.0,
                    close_call: None,
                })
            }
        }
    }

    /// A viewpoint to prefetch from a partial transcript, if one place matches with a score of
    /// at least [`PREFETCH_MIN_SCORE`] and no close call (PRD §7.2). Left-over words do not stop
    /// a prefetch: a partial often ends inside a word, and a wrong load is cancelled by the final
    /// transcript.
    pub fn prefetch_hint(&self, partial: &str) -> Option<PrefetchHint> {
        let a = self.analyse(partial);
        if a.place_words.is_empty() {
            return None;
        }
        match self.places.match_place(&a.place_words) {
            MatchOutcome::Found {
                best,
                ambiguous: false,
                ..
            } if best.score >= PREFETCH_MIN_SCORE => Some(PrefetchHint {
                viewpoint_id: self.places.viewpoints()[best.viewpoint].id.clone(),
                score: best.score,
            }),
            _ => None,
        }
    }
}

/// A conversation: remembers the last place for follow-ups ("what about midnight?").
#[derive(Debug)]
pub struct Session<'p> {
    parser: &'p Parser,
    last: Option<LastPlace>,
}

impl<'p> Session<'p> {
    /// A session with no previous place.
    pub fn new(parser: &'p Parser) -> Session<'p> {
        Session { parser, last: None }
    }

    /// The place the next follow-up will use.
    pub fn last(&self) -> Option<&LastPlace> {
        self.last.as_ref()
    }

    /// Parse `utterance` at `now`, then remember its place. A refused time (out of range) still
    /// names a place, so it is remembered too.
    pub fn ask(&mut self, utterance: &str, now: DateTime<Utc>) -> Result<Query, ParseError> {
        let result = self.parser.parse(
            utterance,
            &ParseContext {
                now,
                last: self.last.as_ref(),
            },
        );
        match &result {
            Ok(q) => {
                self.last = Some(LastPlace {
                    place_id: q.place_id.clone(),
                    viewpoint_id: q.viewpoint_id.clone(),
                })
            }
            Err(ParseError::OutOfRange {
                place_id,
                viewpoint_id,
                ..
            }) => {
                self.last = Some(LastPlace {
                    place_id: place_id.clone(),
                    viewpoint_id: viewpoint_id.clone(),
                });
            }
            Err(_) => {}
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-11-19T10:00:00-08:00")
            .expect("time")
            .with_timezone(&Utc)
    }

    #[test]
    fn prd_example_end_to_end() {
        let q = Parser::embedded()
            .parse(
                "Is Rainier out tomorrow at 4?",
                &ParseContext {
                    now: now(),
                    last: None,
                },
            )
            .expect("parses");
        let json = q.to_json();
        assert!(
            json.starts_with(r#"{"place_id":"rainier","viewpoint_id":"rainier.paradise-mountain","valid_time":"2026-11-20T16:00:00-08:00","valid_time_utc":"2026-11-21T00:00:00Z","tz":"America/Los_Angeles","time_source":{"kind":"clock","text":"tomorrow at 4","rule":"day-plus-bare-hour"},"ambiguities":[{"field":"meridiem","chosen":"pm","alternative":"2026-11-20T04:00:00-08:00"}],"overrides":{},"path":"fast_path","confidence":0.97"#),
            "{json}"
        );
    }

    #[test]
    fn canonical_drops_fillers_but_keeps_names() {
        let p = Parser::embedded();
        assert_eq!(
            p.canonical("Is Rainier out tomorrow at 4?"),
            "rainier tomorrow at 4"
        );
        assert_eq!(p.canonical("is the mountain out"), "the mountain");
        assert_eq!(
            p.canonical("how does look rock look at quarter to five"),
            "look rock at quarter to five"
        );
        // A stray trailing "from" is dropped without touching the time words after it.
        assert_eq!(p.canonical("rainier from tomorrow"), "rainier tomorrow");
        assert_eq!(p.canonical("rainier tomorrow from"), "rainier tomorrow");
        // Every "from" that picks no viewpoint goes, and repeats before viewpoint words collapse.
        assert_eq!(p.canonical("from from"), "");
        assert_eq!(p.canonical("rainier from from"), "rainier");
        assert_eq!(
            p.canonical("grand canyon from from yavapai"),
            "grand canyon from yavapai"
        );
    }

    #[test]
    fn canonical_form_keeps_fillers_that_separate_time_words() {
        // Found by the adversarial review's word-salad fuzz: dropping "and" or "the" made a time
        // the original did not say ("at 4 30" is 16:30, "half past four" is 16:30).
        let p = Parser::embedded();
        let ctx = ParseContext {
            now: now(),
            last: None,
        };
        for u in [
            "rainier at 4 and 30",
            "rainier at half the past four",
            "show what is me rainier at 9",
        ] {
            let c = p.canonical(u);
            assert_eq!(p.canonical(&c), c, "{u:?}: {c:?} is not a fixed point");
            assert_eq!(p.parse(u, &ctx), p.parse(&c, &ctx), "{u:?} vs {c:?}");
        }
        assert_eq!(p.canonical("rainier at 4 and 30"), "rainier at 4 and 30");
        assert_eq!(p.canonical("show what is me rainier at 9"), "rainier at 9");
    }

    #[test]
    fn left_over_words_go_to_the_fallback() {
        let p = Parser::embedded();
        let ctx = ParseContext {
            now: now(),
            last: None,
        };
        for u in [
            "rainier beach at 9",
            "paradise valley at 9",
            "point reyes sunsets",
            "rainier tomorrow arvo",
            "grand canyon from yavapai lodge",
        ] {
            assert!(
                matches!(p.parse(u, &ctx), Err(ParseError::Unrecognised { .. })),
                "{u:?}: {:?}",
                p.parse(u, &ctx)
            );
        }
        // Words about the picture are fillers, not left-overs.
        let q = p
            .parse("show me the clouds and the sky over rainier tomorrow", &ctx)
            .expect("parses");
        assert_eq!(q.place_id, "rainier");
    }

    #[test]
    fn session_remembers_the_place() {
        let p = Parser::embedded();
        let mut s = Session::new(p);
        assert_eq!(
            s.ask("what about midnight", now()),
            Err(ParseError::NoPlace)
        );
        s.ask("point reyes at 9", now()).expect("ok");
        let q = s.ask("what about midnight?", now()).expect("ok");
        assert_eq!(
            (q.place_id.as_str(), q.place_source),
            ("point-reyes", PlaceSource::Context)
        );
    }

    #[test]
    fn prefetch_needs_a_confident_single_place() {
        let p = Parser::embedded();
        assert_eq!(p.prefetch_hint("show me the"), None);
        assert_eq!(
            p.prefetch_hint("show me point reyes")
                .map(|h| h.viewpoint_id)
                .as_deref(),
            Some("point-reyes.beach")
        );
        assert_eq!(
            p.prefetch_hint("grand"),
            None,
            "a close call does not prefetch"
        );
        assert_eq!(p.prefetch_hint("show me pebble beach"), None);
        assert_eq!(p.prefetch_hint("show me mountain view"), None);
        // A partial that ends inside a word still prefetches the place it names.
        assert_eq!(
            p.prefetch_hint("show me rainier tomor")
                .map(|h| h.viewpoint_id)
                .as_deref(),
            Some("rainier.paradise-mountain")
        );
    }

    #[test]
    fn place_names_are_neither_fillers_nor_time_words() {
        // A name word that the filler list or the time grammar could take would break that name
        // whenever it is said without the rest. "look" (in "look rock") and "the" are fillers
        // on purpose; exact names protect them.
        let table = PlaceTable::embedded();
        for v in table.viewpoints() {
            for alias in &v.aliases {
                for w in tokenize(alias) {
                    assert!(
                        !is_filler(&w) || ["look", "the"].contains(&w.as_str()),
                        "{alias:?}: {w} is a filler"
                    );
                    assert!(!is_time_word(&w), "{alias:?}: {w} is a time word");
                }
            }
        }
    }
}
