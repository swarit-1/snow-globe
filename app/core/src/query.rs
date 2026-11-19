//! The structured query the parser emits (PRD §7.3), and its JSON form.
//!
//! The first ten fields are exactly the PRD §7.3 example:
//!
//! ```json
//! {"place_id": "rainier", "viewpoint_id": "rainier.paradise-mountain",
//!  "valid_time": "2026-11-20T16:00:00-08:00", "valid_time_utc": "2026-11-21T00:00:00Z",
//!  "tz": "America/Los_Angeles",
//!  "time_source": {"kind": "clock", "text": "tomorrow at 4", "rule": "day-plus-bare-hour"},
//!  "ambiguities": [{"field": "meridiem", "chosen": "pm", "alternative": "2026-11-20T04:00:00-08:00"}],
//!  "overrides": {}, "path": "fast_path", "confidence": 0.97}
//! ```
//!
//! Four fields follow them, all needed by the overlay chips (PRD §5.1) and the caption:
//! `place_source` (`utterance` or `context`), `place_match` (`stage`, `score`), `horizon`
//! (`hrrr` or `gfs`) and `flags` (`live`, `rolled_to_tomorrow`, `rolled_to_next_hour`,
//! `rolled_to_next_week`, `dst_gap`, `dst_fold`, `gfs_horizon`). Solar rules add `event`,
//! `sun_altitude_deg` and, for golden and blue hour, `interval` to `time_source`.

use chrono::{DateTime, FixedOffset, SecondsFormat, Utc};
use serde::{Deserialize, Serialize};

use crate::places::MatchStage;
use crate::resolve::{Flag, Horizon, TimeKind, TimeRule};
use crate::solar::SolarEvent;

/// RFC 3339 with the place's offset and whole seconds ("2026-11-20T16:00:00-08:00").
pub mod rfc3339_offset {
    use chrono::{DateTime, FixedOffset, SecondsFormat};
    use serde::{Deserialize, Deserializer, Serializer};

    /// Serialise with whole seconds and a numeric offset.
    pub fn serialize<S: Serializer>(t: &DateTime<FixedOffset>, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&t.to_rfc3339_opts(SecondsFormat::Secs, false))
    }

    /// Parse RFC 3339.
    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<DateTime<FixedOffset>, D::Error> {
        let s = String::deserialize(d)?;
        DateTime::parse_from_rfc3339(&s).map_err(serde::de::Error::custom)
    }
}

/// RFC 3339 in UTC with a `Z` and whole seconds ("2026-11-21T00:00:00Z").
pub mod rfc3339_utc {
    use chrono::{DateTime, SecondsFormat, Utc};
    use serde::{Deserialize, Deserializer, Serializer};

    /// Serialise with whole seconds and `Z`.
    pub fn serialize<S: Serializer>(t: &DateTime<Utc>, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&t.to_rfc3339_opts(SecondsFormat::Secs, true))
    }

    /// Parse RFC 3339 (any offset) into UTC.
    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<DateTime<Utc>, D::Error> {
        let s = String::deserialize(d)?;
        DateTime::parse_from_rfc3339(&s)
            .map(|t| t.with_timezone(&Utc))
            .map_err(serde::de::Error::custom)
    }
}

/// Format a zoned instant the way the query does.
pub fn fmt_local(t: &DateTime<FixedOffset>) -> String {
    t.to_rfc3339_opts(SecondsFormat::Secs, false)
}

/// Format a UTC instant the way the query does.
pub fn fmt_utc(t: &DateTime<Utc>) -> String {
    t.to_rfc3339_opts(SecondsFormat::Secs, true)
}

/// A golden- or blue-hour interval (the scrub bar spans it; PRD §5.1).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Interval {
    /// Start, in the place's zone.
    #[serde(with = "rfc3339_offset")]
    pub start: DateTime<FixedOffset>,
    /// End, in the place's zone.
    #[serde(with = "rfc3339_offset")]
    pub end: DateTime<FixedOffset>,
    /// Sun altitude at the start, degrees.
    pub start_altitude_deg: f64,
    /// Sun altitude at the end, degrees.
    pub end_altitude_deg: f64,
}

/// Where the valid time came from (PRD §7.3 `time_source`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TimeSource {
    /// Kind of time said.
    pub kind: TimeKind,
    /// The time words as heard, normalised ("tomorrow at 4"); empty when no time was said.
    pub text: String,
    /// The PRD §7.4 rule applied.
    pub rule: TimeRule,
    /// Solar rules: the event.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event: Option<SolarEvent>,
    /// Solar rules: the Sun's geometric altitude at the rendered instant, degrees.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sun_altitude_deg: Option<f64>,
    /// Golden and blue hour: the interval.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub interval: Option<Interval>,
}

/// What an ambiguity chip is about.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AmbiguityField {
    /// AM or PM (the alternative is an RFC 3339 time).
    Meridiem,
    /// Which place (the alternative is a `place_id`).
    Place,
    /// Which viewpoint (the alternative is a `viewpoint_id`).
    Viewpoint,
}

/// One chip the overlay can flip (PRD §5.1, §7.3).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ambiguity {
    /// What is ambiguous.
    pub field: AmbiguityField,
    /// What was chosen ("pm", or a place ID).
    pub chosen: String,
    /// The other reading (an RFC 3339 time, or a place ID).
    pub alternative: String,
}

/// How the query was produced.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ParsePath {
    /// The hand-written grammar (this crate).
    FastPath,
    /// The on-device LLM fallback (TODO(M3): Foundation Models, PRD §7.3).
    Fallback,
}

/// Whether the place was said or carried over from the previous query.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlaceSource {
    /// Named in this utterance.
    Utterance,
    /// The last place asked about (PRD §7.4 "A time with no place").
    Context,
}

/// Which matcher stage picked the place, and its score.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct PlaceMatchInfo {
    /// `alias`, `exact`, `fuzzy`, `phonetic`, or `context`.
    pub stage: PlaceStage,
    /// Score in [0, 1].
    pub score: f64,
}

/// [`MatchStage`] plus `context`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlaceStage {
    /// A manifest alias.
    Alias,
    /// A canonical name.
    Exact,
    /// Spelling similarity ([`crate::places::fuzzy_score`]).
    Fuzzy,
    /// Double Metaphone.
    Phonetic,
    /// Carried over from the previous query.
    Context,
}

impl From<MatchStage> for PlaceStage {
    fn from(s: MatchStage) -> Self {
        match s {
            MatchStage::Alias => PlaceStage::Alias,
            MatchStage::Exact => PlaceStage::Exact,
            MatchStage::Fuzzy => PlaceStage::Fuzzy,
            MatchStage::Phonetic => PlaceStage::Phonetic,
        }
    }
}

/// The structured query (PRD §7.3).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Query {
    /// Place ID, for example `rainier`.
    pub place_id: String,
    /// Viewpoint ID, for example `rainier.paradise-mountain`.
    pub viewpoint_id: String,
    /// Valid time with the place's offset.
    #[serde(with = "rfc3339_offset")]
    pub valid_time: DateTime<FixedOffset>,
    /// Valid time in UTC.
    #[serde(with = "rfc3339_utc")]
    pub valid_time_utc: DateTime<Utc>,
    /// The place's IANA zone.
    pub tz: String,
    /// Where the time came from.
    pub time_source: TimeSource,
    /// Chips: AM/PM and close calls between places.
    pub ambiguities: Vec<Ambiguity>,
    /// User overrides applied after parsing (empty from the parser; the overlay fills it).
    pub overrides: serde_json::Map<String, serde_json::Value>,
    /// `fast_path` (grammar) or `fallback` (LLM).
    pub path: ParsePath,
    /// Heuristic confidence in [0, 1]; see `Parser` for the formula (not yet calibrated).
    pub confidence: f64,
    /// Whether the place was said or carried over.
    pub place_source: PlaceSource,
    /// How the place matched.
    pub place_match: PlaceMatchInfo,
    /// HRRR or GFS.
    pub horizon: Horizon,
    /// Flags for chips and caption.
    pub flags: Vec<Flag>,
}

impl Query {
    /// JSON text (compact, field order as in PRD §7.3).
    pub fn to_json(&self) -> String {
        serde_json::to_string(self).unwrap_or_default()
    }

    /// The meridiem ambiguity, if any.
    pub fn meridiem_ambiguity(&self) -> Option<&Ambiguity> {
        self.ambiguities
            .iter()
            .find(|a| a.field == AmbiguityField::Meridiem)
    }

    /// True if `flag` is set.
    pub fn has_flag(&self, flag: Flag) -> bool {
        self.flags.contains(&flag)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn example() -> Query {
        Query {
            place_id: "rainier".into(),
            viewpoint_id: "rainier.paradise-mountain".into(),
            valid_time: DateTime::parse_from_rfc3339("2026-11-20T16:00:00-08:00").expect("time"),
            valid_time_utc: DateTime::parse_from_rfc3339("2026-11-21T00:00:00Z")
                .expect("time")
                .with_timezone(&Utc),
            tz: "America/Los_Angeles".into(),
            time_source: TimeSource {
                kind: TimeKind::Clock,
                text: "tomorrow at 4".into(),
                rule: TimeRule::DayPlusBareHour,
                event: None,
                sun_altitude_deg: None,
                interval: None,
            },
            ambiguities: vec![Ambiguity {
                field: AmbiguityField::Meridiem,
                chosen: "pm".into(),
                alternative: "2026-11-20T04:00:00-08:00".into(),
            }],
            overrides: serde_json::Map::new(),
            path: ParsePath::FastPath,
            confidence: 0.97,
            place_source: PlaceSource::Utterance,
            place_match: PlaceMatchInfo {
                stage: PlaceStage::Alias,
                score: 1.0,
            },
            horizon: Horizon::Hrrr,
            flags: vec![],
        }
    }

    #[test]
    fn json_starts_with_the_prd_shape() {
        let json = example().to_json();
        let prd = r#"{"place_id":"rainier","viewpoint_id":"rainier.paradise-mountain","valid_time":"2026-11-20T16:00:00-08:00","valid_time_utc":"2026-11-21T00:00:00Z","tz":"America/Los_Angeles","time_source":{"kind":"clock","text":"tomorrow at 4","rule":"day-plus-bare-hour"},"ambiguities":[{"field":"meridiem","chosen":"pm","alternative":"2026-11-20T04:00:00-08:00"}],"overrides":{},"path":"fast_path","confidence":0.97"#;
        assert!(json.starts_with(prd), "{json}");
        assert!(json.ends_with(r#""place_source":"utterance","place_match":{"stage":"alias","score":1.0},"horizon":"hrrr","flags":[]}"#), "{json}");
    }

    #[test]
    fn json_round_trip_is_byte_stable() {
        let q = example();
        let a = q.to_json();
        let back: Query = serde_json::from_str(&a).expect("parse");
        assert_eq!(back, q);
        assert_eq!(back.to_json(), a);
    }
}
