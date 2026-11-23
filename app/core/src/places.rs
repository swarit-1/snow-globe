//! The place and viewpoint table, built from `viewpoints/manifests/*.json`, and the matcher that
//! turns place words in an utterance into a viewpoint (PRD §7.5).
//!
//! **Matching order** (each stage runs only if the previous one found nothing):
//! 1. **alias**: a contiguous run of words equals one of a manifest's `aliases`;
//! 2. **exact**: it equals a canonical name (the place's display name, its `place_id`, the
//!    `place_id` with the viewpoint suffix, or the place named in the camera's `view_label`);
//! 3. **fuzzy**: [`fuzzy_score`] of the place words (generic words such as "national park"
//!    removed) against every name, accepted at ≥ [`FUZZY_ACCEPT`];
//! 4. **phonetic**: Double Metaphone keys matched word by word (score [`PHONETIC_FULL`] when
//!    every word matches; accepted at ≥ [`PHONETIC_ACCEPT`]).
//!
//! Stages 1–2 look for names anywhere in the place words; stages 3–4 score the place words as a
//! whole, so words a name cannot explain lower the score (see [`PlaceTable::match_place`]).
//!
//! **Never a guess** (PRD §7.5). Snow Globe knows eight places, and people will name others:
//! - The bare viewpoint suffix of an ID ("beach" in `point-reyes.beach`, "science center" in
//!   `grand-teton.science-center`) only picks a viewpoint inside its own place ("Point Reyes from
//!   the beach"). It never finds a place: Long Beach and the Pacific Science Center exist.
//! - A fuzzy or phonetic match must rest on a word that is not a feature word
//!   ([`crate::normalize::FEATURE_WORDS`]: "mountain", "beach", ...), so "stone mountain" and
//!   "mountain view" do not become "the mountain".
//! - A phonetic key of fewer than three letters only counts when neither word has a longer one
//!   ([`Codes::matches`]), so "reno" (RN) is not "rainier" (RN / RNR).
//! - Words that an alias or exact hit leaves over are returned as `unexplained` in
//!   [`MatchOutcome::Found`], unless they are generic place words or words of the matched
//!   place's own names ("great" in "great smokey mountains"). The parser then hands the utterance
//!   to the fallback instead of answering ("rainier beach", "paradise valley").
//!
//! **Close calls:** when the best two places score within [`MARGIN`] of each other, the best is
//! kept and the result is flagged ambiguous, which the overlay shows as a disambiguation chip.
//! Exact hits on two different places (for example "Rainier or Point Reyes") are always a close
//! call. **A miss** returns the three closest places by score, never a guess.
//!
//! **Viewpoints:** "`from <x>`" picks a viewpoint ("Grand Canyon from Yavapai"); otherwise the
//! place's default viewpoint is used, unless the matched alias belongs only to another viewpoint
//! of that place.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::OnceLock;

use chrono_tz::Tz;
use serde::{Deserialize, Serialize};

use crate::normalize::{is_feature_word, is_generic_place_word, tokenize};
use crate::phonetic::{Codes, codes};
use crate::solar::Site;

mod embedded {
    include!(concat!(env!("OUT_DIR"), "/manifests.rs"));
}

/// Fuzzy score ([`fuzzy_score`]) at which a place is accepted.
pub const FUZZY_ACCEPT: f64 = 0.80;
/// Score of a phonetic match in which every word's key matches.
pub const PHONETIC_FULL: f64 = 0.90;
/// Phonetic score at which a place is accepted (in practice: every word matches).
pub const PHONETIC_ACCEPT: f64 = 0.85;
/// Two places closer than this are a close call (disambiguation chip).
pub const MARGIN: f64 = 0.05;
/// Score a partial transcript needs before the core prefetches a viewpoint (PRD §7.2).
pub const PREFETCH_MIN_SCORE: f64 = 0.90;
/// A whole name counts as said when each of its words is at least this like a word said: one
/// letter in four may differ ("big ben" for "big bend").
pub const WHOLE_NAME_WORD: f64 = 0.75;
/// The same letters with the spaces in other places ("pointreyes") count at this similarity.
pub const RESPACED_ACCEPT: f64 = 0.85;

/// Build order of a viewpoint (PRD §8.2); earlier statuses win exact ties.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Status {
    /// Makes MSV claims (Rainier, Point Reyes).
    Msv,
    /// Built after the MSV (Grand Canyon).
    Third,
    /// Captured now, built later.
    V1Candidate,
}

/// One camera viewpoint (one manifest).
#[derive(Debug, Clone, PartialEq)]
pub struct Viewpoint {
    /// `<place>.<view>`, for example `rainier.paradise-mountain`.
    pub id: String,
    /// The place this viewpoint belongs to.
    pub place_id: String,
    /// The place's display name.
    pub place_name: String,
    /// The manifest's aliases.
    pub aliases: Vec<String>,
    /// True for the place's default viewpoint.
    pub default: bool,
    /// The place's IANA zone; all times resolve here (PRD §7.4).
    pub tz: Tz,
    /// Build order.
    pub status: Status,
    /// Camera position (observer for solar events).
    pub site: Site,
    /// The operator's label for the view, if any.
    pub view_label: Option<String>,
    /// True if the manifest names a live image URL (the "now" rule shows the live frame).
    pub has_live_camera: bool,
}

/// A place: one or more viewpoints sharing a name and a zone.
#[derive(Debug, Clone, PartialEq)]
pub struct Place {
    /// `place_id`, for example `point-reyes`.
    pub id: String,
    /// Display name, for example "Point Reyes".
    pub name: String,
    /// IANA zone of the place.
    pub tz: Tz,
    /// Best (earliest) build status among its viewpoints.
    pub status: Status,
    /// Indexes into [`PlaceTable::viewpoints`].
    pub viewpoints: Vec<usize>,
    /// Index of the default viewpoint.
    pub default_viewpoint: usize,
}

#[derive(Deserialize)]
struct RawManifest {
    id: String,
    place_id: String,
    place: String,
    aliases: Vec<String>,
    default: bool,
    time_zone: String,
    status: Status,
    camera: RawCamera,
    truth_camera: RawTruthCamera,
}

#[derive(Deserialize)]
struct RawCamera {
    lat: f64,
    lon: f64,
    elevation_m: f64,
}

#[derive(Deserialize)]
struct RawTruthCamera {
    image_url: Option<String>,
    #[serde(default)]
    view_label: Option<String>,
}

/// Why a manifest set could not be loaded.
#[derive(Debug, thiserror::Error)]
pub enum PlaceTableError {
    /// The directory or a file could not be read.
    #[error("cannot read {path}: {source}")]
    Io {
        /// What was being read.
        path: String,
        /// The underlying error.
        source: std::io::Error,
    },
    /// A manifest is not valid JSON or lacks a required field.
    #[error("manifest {name}: {source}")]
    Json {
        /// File stem of the manifest.
        name: String,
        /// The underlying error.
        source: serde_json::Error,
    },
    /// A manifest breaks a rule the table depends on.
    #[error("manifest {name}: {message}")]
    Invalid {
        /// File stem of the manifest.
        name: String,
        /// What is wrong.
        message: String,
    },
    /// There were no manifests at all.
    #[error("no viewpoint manifests found")]
    Empty,
}

/// Which matcher stage produced a hit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MatchStage {
    /// A manifest alias, word for word.
    Alias,
    /// A canonical name, word for word.
    Exact,
    /// Token-set fuzzy match.
    Fuzzy,
    /// Double Metaphone match.
    Phonetic,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum NameKind {
    Alias,
    Exact,
}

#[derive(Debug, Clone)]
struct Name {
    viewpoint: usize,
    place: usize,
    kind: NameKind,
    /// A bare viewpoint suffix ("beach"): it picks a viewpoint within its own place only.
    viewpoint_only: bool,
    tokens: Vec<String>,
    core: Vec<String>,
    core_codes: Vec<Codes>,
    token_codes: Vec<Codes>,
}

/// A place the matcher considered, with its score (used for misses and chips).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Candidate {
    /// `place_id`.
    pub place_id: String,
    /// Display name.
    pub place: String,
    /// Best similarity in [0, 1]. On a miss it can be high for words that only name a kind of
    /// place ("science center" scores 1 against "teton science center" yet is not accepted).
    pub score: f64,
}

/// A matched place (or viewpoint) with the stage and score that matched it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Hit {
    /// Index into [`PlaceTable::places`].
    pub place: usize,
    /// Index into [`PlaceTable::viewpoints`].
    pub viewpoint: usize,
    /// The stage that matched.
    pub stage: MatchStage,
    /// Score in [0, 1]; 1 for alias and exact hits.
    pub score: f64,
}

/// Result of matching place words.
#[derive(Debug, Clone, PartialEq)]
pub enum MatchOutcome {
    /// A place (or viewpoint) matched.
    Found {
        /// The chosen hit.
        best: Hit,
        /// The best hit for a different place (or viewpoint), if any.
        runner_up: Option<Hit>,
        /// True when `runner_up` scored within [`MARGIN`] of `best`.
        ambiguous: bool,
        /// Words that no matched name explains, in order. Only the alias and exact stages leave
        /// words over (the fuzzy and phonetic stages score every word). Generic place words and
        /// words of the matched places' own names do not count. Non-empty means the words may
        /// name somewhere else ("rainier beach"), so the parser must not answer on the fast path.
        unexplained: Vec<String>,
    },
    /// The words named no place in particular ("the park"); treat as no place said.
    NothingSaid,
    /// Nothing matched; the closest places, best first.
    Miss {
        /// Up to three candidates.
        closest: Vec<Candidate>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Scope {
    Places,
    Viewpoints(Option<usize>),
}

/// Viewpoints and places, plus the indexes the matcher needs.
#[derive(Debug, Clone)]
pub struct PlaceTable {
    viewpoints: Vec<Viewpoint>,
    places: Vec<Place>,
    names: Vec<Name>,
    by_text: HashMap<String, Vec<usize>>,
    max_name_len: usize,
    /// Every word of every name of each place (indexed like `places`).
    vocabulary: Vec<HashSet<String>>,
}

/// Normalised word sequence of a name, using the same tokenizer as utterances.
fn name_tokens(text: &str) -> Vec<String> {
    tokenize(text)
}

fn core_of(tokens: &[String]) -> Vec<String> {
    tokens
        .iter()
        .filter(|t| !is_generic_place_word(t))
        .cloned()
        .collect()
}

/// The place in an operator label such as "View from Yavapai Point, looking northwest".
fn label_place(label: &str) -> Option<String> {
    let lower = label.to_lowercase();
    let rest = lower
        .strip_prefix("view from ")
        .or_else(|| lower.strip_prefix("view of "))?;
    let core = rest.split(", looking").next().unwrap_or(rest).trim();
    let core = core.strip_prefix("the ").unwrap_or(core);
    let core = core.strip_suffix(" area").unwrap_or(core);
    (!core.is_empty()).then(|| core.to_string())
}

/// `(file stem, JSON text)` of every manifest compiled into the crate from
/// `viewpoints/manifests/`, sorted by file name. Tier 0 reads the camera fields the alias table
/// does not keep (view azimuth, attribution, archive code; [`crate::tier0::camera`]).
pub fn embedded_manifest_texts() -> &'static [(&'static str, &'static str)] {
    embedded::EMBEDDED_MANIFESTS
}

/// `(file stem, JSON text)` of every `*.json` manifest in a directory, sorted by file name.
pub fn manifest_texts_from_dir(dir: &Path) -> Result<Vec<(String, String)>, PlaceTableError> {
    let io = |source| PlaceTableError::Io {
        path: dir.display().to_string(),
        source,
    };
    let mut files: Vec<_> = std::fs::read_dir(dir)
        .map_err(io)?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    let mut texts = Vec::with_capacity(files.len());
    for path in files {
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or_default()
            .to_string();
        let text = std::fs::read_to_string(&path).map_err(|source| PlaceTableError::Io {
            path: path.display().to_string(),
            source,
        })?;
        texts.push((stem, text));
    }
    Ok(texts)
}

impl PlaceTable {
    /// The table compiled into the crate from `viewpoints/manifests/` at build time.
    pub fn embedded() -> &'static PlaceTable {
        static TABLE: OnceLock<PlaceTable> = OnceLock::new();
        TABLE.get_or_init(|| {
            PlaceTable::from_manifest_texts(embedded::EMBEDDED_MANIFESTS.iter().copied())
                .expect("the embedded viewpoint manifests are valid (checked by tests)")
        })
    }

    /// Load every `*.json` manifest in a directory at run time.
    pub fn from_dir(dir: &Path) -> Result<PlaceTable, PlaceTableError> {
        let texts = manifest_texts_from_dir(dir)?;
        PlaceTable::from_manifest_texts(texts.iter().map(|(a, b)| (a.as_str(), b.as_str())))
    }

    /// Build the table from `(file stem, JSON text)` pairs.
    pub fn from_manifest_texts<'a>(
        manifests: impl IntoIterator<Item = (&'a str, &'a str)>,
    ) -> Result<PlaceTable, PlaceTableError> {
        let mut viewpoints = Vec::new();
        for (stem, text) in manifests {
            let raw: RawManifest =
                serde_json::from_str(text).map_err(|source| PlaceTableError::Json {
                    name: stem.to_string(),
                    source,
                })?;
            let invalid = |message: String| PlaceTableError::Invalid {
                name: stem.to_string(),
                message,
            };
            if raw.id != stem {
                return Err(invalid(format!(
                    "id {} does not match the file name",
                    raw.id
                )));
            }
            if raw.id.split('.').next() != Some(raw.place_id.as_str()) {
                return Err(invalid(format!(
                    "place_id {} is not the prefix of {}",
                    raw.place_id, raw.id
                )));
            }
            let tz: Tz = raw
                .time_zone
                .parse()
                .map_err(|_| invalid(format!("unknown IANA zone {}", raw.time_zone)))?;
            viewpoints.push(Viewpoint {
                id: raw.id,
                place_id: raw.place_id,
                place_name: raw.place,
                aliases: raw.aliases,
                default: raw.default,
                tz,
                status: raw.status,
                site: Site {
                    lat_deg: raw.camera.lat,
                    lon_deg: raw.camera.lon,
                    elevation_m: raw.camera.elevation_m,
                },
                view_label: raw.truth_camera.view_label,
                has_live_camera: raw.truth_camera.image_url.is_some_and(|u| !u.is_empty()),
            });
        }
        if viewpoints.is_empty() {
            return Err(PlaceTableError::Empty);
        }
        viewpoints.sort_by(|a, b| a.id.cmp(&b.id));

        // Places, in a stable order: status, then id.
        let mut place_ids: Vec<&str> = viewpoints.iter().map(|v| v.place_id.as_str()).collect();
        place_ids.sort();
        place_ids.dedup();
        let mut places = Vec::new();
        for pid in place_ids {
            let members: Vec<usize> = (0..viewpoints.len())
                .filter(|&i| viewpoints[i].place_id == pid)
                .collect();
            let defaults: Vec<usize> = members
                .iter()
                .copied()
                .filter(|&i| viewpoints[i].default)
                .collect();
            if defaults.len() != 1 {
                return Err(PlaceTableError::Invalid {
                    name: pid.to_string(),
                    message: format!(
                        "place has {} default viewpoints, expected exactly 1",
                        defaults.len()
                    ),
                });
            }
            let first = &viewpoints[members[0]];
            if members.iter().any(|&i| viewpoints[i].tz != first.tz) {
                return Err(PlaceTableError::Invalid {
                    name: pid.to_string(),
                    message: "viewpoints of one place disagree on time_zone".to_string(),
                });
            }
            let status = members
                .iter()
                .map(|&i| viewpoints[i].status)
                .min()
                .unwrap_or(Status::V1Candidate);
            places.push(Place {
                id: pid.to_string(),
                name: viewpoints[defaults[0]].place_name.clone(),
                tz: first.tz,
                status,
                viewpoints: members,
                default_viewpoint: defaults[0],
            });
        }
        places.sort_by(|a, b| (a.status, &a.id).cmp(&(b.status, &b.id)));

        // Names: aliases, then canonical names.
        let place_index: HashMap<String, usize> = places
            .iter()
            .enumerate()
            .map(|(i, p)| (p.id.clone(), i))
            .collect();
        let mut names: Vec<Name> = Vec::new();
        let mut alias_owner: HashMap<String, usize> = HashMap::new();
        for (vi, v) in viewpoints.iter().enumerate() {
            let pi = place_index[&v.place_id];
            for alias in &v.aliases {
                let tokens = name_tokens(alias);
                let key = tokens.join(" ");
                if let Some(&other) = alias_owner.get(&key)
                    && other != pi
                {
                    return Err(PlaceTableError::Invalid {
                        name: v.id.clone(),
                        message: format!("alias {alias:?} is also used by {}", places[other].id),
                    });
                }
                alias_owner.insert(key, pi);
                names.push(Name::new(vi, pi, NameKind::Alias, false, tokens));
            }
        }
        for (vi, v) in viewpoints.iter().enumerate() {
            let pi = place_index[&v.place_id];
            let (place_part, view_part) = v.id.split_once('.').unwrap_or((v.id.as_str(), ""));
            let place_words = place_part.replace('-', " ");
            let view_words = view_part.replace('-', " ");
            // (text, viewpoint only). Earlier entries win duplicates, so an alias or label equal to
            // the bare suffix keeps that text usable for finding the place.
            let mut exact = vec![
                (v.place_name.clone(), false),
                (place_words.clone(), false),
                (format!("{place_words} {view_words}"), false),
            ];
            if let Some(label) = v.view_label.as_deref().and_then(label_place) {
                exact.push((label, false));
            }
            exact.push((view_words, true));
            for (text, viewpoint_only) in exact {
                let tokens = name_tokens(&text);
                if tokens.is_empty() {
                    continue;
                }
                let key = tokens.join(" ");
                if let Some(&other) = alias_owner.get(&key)
                    && other != pi
                {
                    continue; // never let a canonical name shadow another place's alias
                }
                if names
                    .iter()
                    .any(|n| n.viewpoint == vi && n.tokens == tokens)
                {
                    continue;
                }
                names.push(Name::new(vi, pi, NameKind::Exact, viewpoint_only, tokens));
            }
        }
        let mut by_text: HashMap<String, Vec<usize>> = HashMap::new();
        for (i, n) in names.iter().enumerate() {
            by_text.entry(n.tokens.join(" ")).or_default().push(i);
        }
        let max_name_len = names.iter().map(|n| n.tokens.len()).max().unwrap_or(1);
        let mut vocabulary = vec![HashSet::new(); places.len()];
        for n in &names {
            vocabulary[n.place].extend(n.tokens.iter().cloned());
        }
        Ok(PlaceTable {
            viewpoints,
            places,
            names,
            by_text,
            max_name_len,
            vocabulary,
        })
    }

    /// All viewpoints, sorted by ID.
    pub fn viewpoints(&self) -> &[Viewpoint] {
        &self.viewpoints
    }

    /// All places, in priority order (MSV first).
    pub fn places(&self) -> &[Place] {
        &self.places
    }

    /// Place by `place_id`.
    pub fn place(&self, place_id: &str) -> Option<(usize, &Place)> {
        self.places
            .iter()
            .enumerate()
            .find(|(_, p)| p.id == place_id)
    }

    /// Viewpoint by ID.
    pub fn viewpoint(&self, viewpoint_id: &str) -> Option<(usize, &Viewpoint)> {
        self.viewpoints
            .iter()
            .enumerate()
            .find(|(_, v)| v.id == viewpoint_id)
    }

    /// Index of a viewpoint's place.
    pub fn place_of(&self, viewpoint: usize) -> usize {
        let pid = &self.viewpoints[viewpoint].place_id;
        self.places.iter().position(|p| &p.id == pid).unwrap_or(0)
    }

    /// Token ranges `[start, end)` that spell a known name exactly. The parser keeps filler
    /// removal and the time grammar out of these ranges (so "look rock" survives "look").
    pub fn name_spans(&self, tokens: &[&str]) -> Vec<(usize, usize)> {
        let mut spans = Vec::new();
        for s in 0..tokens.len() {
            for e in (s + 1..=tokens.len().min(s + self.max_name_len)).rev() {
                if self.by_text.contains_key(&tokens[s..e].join(" ")) {
                    spans.push((s, e));
                    break;
                }
            }
        }
        spans
    }

    /// Match place words against every place (PRD §7.5).
    pub fn match_place(&self, tokens: &[String]) -> MatchOutcome {
        self.cascade(tokens, Scope::Places)
    }

    /// Match "`from <x>`" words against viewpoints, of one place if given.
    pub fn match_viewpoint(&self, place: Option<usize>, tokens: &[String]) -> MatchOutcome {
        self.cascade(tokens, Scope::Viewpoints(place))
    }

    fn in_scope(&self, name: &Name, scope: Scope) -> bool {
        match scope {
            Scope::Places | Scope::Viewpoints(None) => !name.viewpoint_only,
            Scope::Viewpoints(Some(p)) => name.place == p,
        }
    }

    fn group(&self, name: &Name, scope: Scope) -> usize {
        match scope {
            Scope::Places => name.place,
            Scope::Viewpoints(_) => name.viewpoint,
        }
    }

    /// Tie-break key: lower is preferred (MSV first, then default viewpoints, then id).
    fn rank(&self, hit: &Hit, scope: Scope) -> (Status, bool, String) {
        let p = &self.places[hit.place];
        let v = &self.viewpoints[hit.viewpoint];
        match scope {
            Scope::Places => (p.status, false, p.id.clone()),
            Scope::Viewpoints(_) => (v.status, !v.default, v.id.clone()),
        }
    }

    /// The viewpoint a place-level hit on `name` stands for: the default viewpoint, unless the
    /// name belongs only to a non-default viewpoint of that place.
    fn viewpoint_for(&self, name: &Name, scope: Scope) -> usize {
        if let Scope::Viewpoints(_) = scope {
            return name.viewpoint;
        }
        let place = &self.places[name.place];
        let on_default = self.by_text.get(&name.tokens.join(" ")).is_some_and(|ids| {
            ids.iter()
                .any(|&i| self.names[i].viewpoint == place.default_viewpoint)
        });
        if on_default || self.viewpoints[name.viewpoint].default {
            place.default_viewpoint
        } else {
            name.viewpoint
        }
    }

    fn cascade(&self, tokens: &[String], scope: Scope) -> MatchOutcome {
        let occurrences = self.occurrences(tokens, scope);
        for kind in [NameKind::Alias, NameKind::Exact] {
            if let Some(outcome) = self.exact_stage(tokens, &occurrences, scope, kind) {
                return outcome;
            }
        }
        let core = core_of(tokens);
        if core.is_empty() {
            return MatchOutcome::NothingSaid;
        }
        let fuzzy = self.scored_stage(tokens, &core, scope, MatchStage::Fuzzy);
        if let Some(outcome) = Self::accept(&fuzzy.accepted, FUZZY_ACCEPT) {
            return outcome;
        }
        let phonetic = self.scored_stage(tokens, &core, scope, MatchStage::Phonetic);
        if let Some(outcome) = Self::accept(&phonetic.accepted, PHONETIC_ACCEPT) {
            return outcome;
        }
        // Miss: closest places by the better of the two scores, anchored or not.
        let mut best_by_place: HashMap<usize, f64> = HashMap::new();
        for (&p, &score) in fuzzy.by_place.iter().chain(phonetic.by_place.iter()) {
            let e = best_by_place.entry(p).or_insert(0.0);
            *e = e.max(score);
        }
        let mut closest: Vec<(usize, f64)> = match scope {
            Scope::Viewpoints(Some(p)) => vec![(p, best_by_place.get(&p).copied().unwrap_or(0.0))],
            Scope::Places | Scope::Viewpoints(None) => (0..self.places.len())
                .map(|p| (p, best_by_place.get(&p).copied().unwrap_or(0.0)))
                .collect(),
        };
        closest.sort_by(|a, b| {
            b.1.total_cmp(&a.1).then_with(|| {
                (self.places[a.0].status, &self.places[a.0].id)
                    .cmp(&(self.places[b.0].status, &self.places[b.0].id))
            })
        });
        MatchOutcome::Miss {
            closest: closest
                .into_iter()
                .take(3)
                .map(|(p, s)| Candidate {
                    place_id: self.places[p].id.clone(),
                    place: self.places[p].name.clone(),
                    score: (s * 100.0).round() / 100.0,
                })
                .collect(),
        }
    }

    /// Every occurrence of a name in scope, as `(start, end, name index)`.
    fn occurrences(&self, tokens: &[String], scope: Scope) -> Vec<(usize, usize, usize)> {
        let mut found = Vec::new();
        for s in 0..tokens.len() {
            for e in s + 1..=tokens.len().min(s + self.max_name_len) {
                if let Some(ids) = self.by_text.get(&tokens[s..e].join(" ")) {
                    for &i in ids {
                        if self.in_scope(&self.names[i], scope) {
                            found.push((s, e, i));
                        }
                    }
                }
            }
        }
        found
    }

    fn exact_stage(
        &self,
        tokens: &[String],
        occurrences: &[(usize, usize, usize)],
        scope: Scope,
        kind: NameKind,
    ) -> Option<MatchOutcome> {
        // Exact occurrences of this kind: (start, end, name index).
        let mut hits: Vec<(usize, usize, usize)> = occurrences
            .iter()
            .copied()
            .filter(|&(_, _, i)| self.names[i].kind == kind)
            .collect();
        // Drop hits contained in a longer hit ("point reyes" inside "point reyes lighthouse").
        let all = hits.clone();
        hits.retain(|&(s, e, _)| {
            !all.iter()
                .any(|&(s2, e2, _)| s2 <= s && e <= e2 && (e2 - s2) > (e - s))
        });
        if hits.is_empty() {
            return None;
        }
        let mut groups: Vec<(Hit, usize)> = Vec::new();
        for &(s, e, i) in &hits {
            let n = &self.names[i];
            let hit = Hit {
                place: n.place,
                viewpoint: self.viewpoint_for(n, scope),
                stage: if kind == NameKind::Alias {
                    MatchStage::Alias
                } else {
                    MatchStage::Exact
                },
                score: 1.0,
            };
            let g = self.group(n, scope);
            match groups
                .iter_mut()
                .find(|(h, _)| self.group_of_hit(h, scope) == g)
            {
                Some(existing) => {
                    if e - s > existing.1 {
                        *existing = (hit, e - s);
                    }
                }
                None => groups.push((hit, e - s)),
            }
        }
        groups.sort_by(|a, b| {
            b.1.cmp(&a.1)
                .then_with(|| self.rank(&a.0, scope).cmp(&self.rank(&b.0, scope)))
        });
        let best = groups[0].0;
        let runner_up = groups.get(1).map(|g| g.0);
        let unexplained = self.unexplained(tokens, occurrences, [Some(best), runner_up]);
        Some(MatchOutcome::Found {
            best,
            runner_up,
            ambiguous: runner_up.is_some(),
            unexplained,
        })
    }

    /// Place words outside every name occurrence that are neither generic nor words of the
    /// matched places' own names.
    fn unexplained(
        &self,
        tokens: &[String],
        occurrences: &[(usize, usize, usize)],
        matched: [Option<Hit>; 2],
    ) -> Vec<String> {
        let mut covered = vec![false; tokens.len()];
        for &(s, e, _) in occurrences {
            covered[s..e].iter_mut().for_each(|c| *c = true);
        }
        tokens
            .iter()
            .zip(&covered)
            .filter(|&(t, &c)| {
                !c && !is_generic_place_word(t)
                    && !matched
                        .iter()
                        .flatten()
                        .any(|h| self.vocabulary[h.place].contains(t.as_str()))
            })
            .map(|(t, _)| t.clone())
            .collect()
    }

    fn group_of_hit(&self, hit: &Hit, scope: Scope) -> usize {
        match scope {
            Scope::Places => hit.place,
            Scope::Viewpoints(_) => hit.viewpoint,
        }
    }

    /// Score the place text as a whole against every name, best per group (place or viewpoint).
    ///
    /// The whole text is compared, not its sub-runs: a single shared word ("mount") would
    /// otherwise match every name containing it ("mount desert island") and turn "mount
    /// rayneer" into a false close call. Correctly spelled names inside longer text are already
    /// found by the exact stages, which do search sub-runs.
    fn scored_stage(
        &self,
        tokens: &[String],
        core: &[String],
        scope: Scope,
        stage: MatchStage,
    ) -> Scored {
        let words: Vec<&str> = core.iter().map(String::as_str).collect();
        let full: Vec<&str> = tokens.iter().map(String::as_str).collect();
        let (core_codes, full_codes): (Vec<Codes>, Vec<Codes>) = if stage == MatchStage::Phonetic {
            (
                core.iter().map(|w| codes(w)).collect(),
                tokens.iter().map(|w| codes(w)).collect(),
            )
        } else {
            (Vec::new(), Vec::new())
        };
        let mut best: HashMap<usize, Hit> = HashMap::new();
        let mut by_place: HashMap<usize, f64> = HashMap::new();
        for n in self
            .names
            .iter()
            .filter(|n| self.in_scope(n, scope) && !n.core.is_empty())
        {
            let theirs: Vec<&str> = n.core.iter().map(String::as_str).collect();
            let their_full: Vec<&str> = n.tokens.iter().map(String::as_str).collect();
            let (score, anchored) = match stage {
                MatchStage::Phonetic => {
                    let (score, distinctive) =
                        phonetic_score(&words, &core_codes, &theirs, &n.core_codes);
                    let whole_name = n
                        .token_codes
                        .iter()
                        .all(|nc| full_codes.iter().any(|qc| qc.matches(nc)));
                    (score, distinctive || whole_name)
                }
                _ => (
                    fuzzy_score(&words, &theirs),
                    fuzzy_anchored(&words, &theirs, &full, &their_full),
                ),
            };
            let e = by_place.entry(n.place).or_insert(0.0);
            *e = e.max(score);
            if !anchored {
                continue;
            }
            let hit = Hit {
                place: n.place,
                viewpoint: self.viewpoint_for(n, scope),
                stage,
                score,
            };
            let g = self.group(n, scope);
            let replace = match best.get(&g) {
                None => true,
                Some(old) => {
                    score > old.score
                        || (score == old.score
                            && self.viewpoints[hit.viewpoint].default
                            && !self.viewpoints[old.viewpoint].default)
                }
            };
            if replace {
                best.insert(g, hit);
            }
        }
        let mut accepted: Vec<Hit> = best.into_values().collect();
        accepted.sort_by(|a, b| {
            b.score
                .total_cmp(&a.score)
                .then_with(|| self.rank(a, scope).cmp(&self.rank(b, scope)))
        });
        Scored { accepted, by_place }
    }

    fn accept(hits: &[Hit], threshold: f64) -> Option<MatchOutcome> {
        let best = *hits.first()?;
        if best.score < threshold {
            return None;
        }
        let runner_up = hits.get(1).copied();
        let ambiguous = runner_up.is_some_and(|r| best.score - r.score <= MARGIN + 1e-9);
        Some(MatchOutcome::Found {
            best,
            runner_up,
            ambiguous,
            unexplained: Vec::new(),
        })
    }
}

/// What a scored stage found.
struct Scored {
    /// Anchored hits, best per group, best first.
    accepted: Vec<Hit>,
    /// Best score per place, anchored or not (for the closest places of a miss).
    by_place: HashMap<usize, f64>,
}

impl Name {
    fn new(
        viewpoint: usize,
        place: usize,
        kind: NameKind,
        viewpoint_only: bool,
        tokens: Vec<String>,
    ) -> Name {
        let core = core_of(&tokens);
        let core_codes = core.iter().map(|w| codes(w)).collect();
        let token_codes = tokens.iter().map(|w| codes(w)).collect();
        Name {
            viewpoint,
            place,
            kind,
            viewpoint_only,
            tokens,
            core,
            core_codes,
            token_codes,
        }
    }
}

/// Normalised Levenshtein similarity in [0, 1].
fn ratio(a: &str, b: &str) -> f64 {
    strsim::normalized_levenshtein(a, b)
}

/// Fuzzy similarity of place words to a name's words, in [0, 1] (PRD §7.5 "token-set fuzzy",
/// made one-sided).
///
/// The score is the higher of (a) the mean, over the query's words, of each word's best
/// normalised Levenshtein similarity to a word of the name, and (b) the similarity of the two
/// texts written out in full ("pointreyes" against "point reyes"). Name words the query leaves out
/// cost nothing, so "reyes" scores 1 against "point reyes". Query words the name cannot explain
/// lower the score: "rainier visitor center" scores about 0.5 against "rainier", where a
/// token-set ratio gives 1. A score alone is not a match; see [`fuzzy_anchored`].
pub fn fuzzy_score(query: &[&str], name: &[&str]) -> f64 {
    if query.is_empty() || name.is_empty() {
        return 0.0;
    }
    let per_word = query
        .iter()
        .map(|q| name.iter().map(|n| ratio(q, n)).fold(0.0, f64::max))
        .sum::<f64>()
        / query.len() as f64;
    per_word.max(ratio(&query.join(" "), &name.join(" ")))
}

/// True if a fuzzy match rests on more than words for a kind of place ([`is_feature_word`]):
///
/// - (a) a query word that is not a feature word is at least [`FUZZY_ACCEPT`] like such a word
///   of the name ("yosemitee", "point reyez");
/// - (b) every word of the name as written, "the" included, is at least [`WHOLE_NAME_WORD`] like
///   a word said: the whole name was said, perhaps misspelled ("grand canyn", "big ben");
/// - (c) the words run together are at least [`RESPACED_ACCEPT`] alike ("pointreyes").
///
/// `query` and `name` are the words without generic place words (as scored); `query_full` and
/// `name_full` are all the words. Only anchored matches are accepted, so "grand" alone, "mountain"
/// (Mountain View with the filler "view" removed: not "the mountain") and "rocky mountains"
/// (0.8 against "smoky mountains" as whole texts) are misses.
pub fn fuzzy_anchored(
    query: &[&str],
    name: &[&str],
    query_full: &[&str],
    name_full: &[&str],
) -> bool {
    let distinctive = query.iter().filter(|q| !is_feature_word(q)).any(|q| {
        name.iter()
            .filter(|n| !is_feature_word(n))
            .any(|n| ratio(q, n) >= FUZZY_ACCEPT)
    });
    let whole_name = !name_full.is_empty()
        && name_full
            .iter()
            .all(|n| query_full.iter().any(|q| ratio(q, n) >= WHOLE_NAME_WORD));
    let respaced = !query_full.is_empty()
        && ratio(&query_full.concat(), &name_full.concat()) >= RESPACED_ACCEPT;
    distinctive || whole_name || respaced
}

/// Phonetic similarity: [`PHONETIC_FULL`] × the Dice share of words whose keys match, and whether
/// a matched pair of words has no feature word in it (anchor (a) of [`fuzzy_anchored`]; the
/// caller adds (b), every word of the name matched by key).
fn phonetic_score(
    query: &[&str],
    query_codes: &[Codes],
    name: &[&str],
    name_codes: &[Codes],
) -> (f64, bool) {
    if query.is_empty() || name.is_empty() {
        return (0.0, false);
    }
    let mut used = vec![false; name.len()];
    let mut matched = 0usize;
    let mut anchored = false;
    for (q, qc) in query.iter().zip(query_codes) {
        if let Some(j) = (0..name.len()).find(|&j| !used[j] && qc.matches(&name_codes[j])) {
            used[j] = true;
            matched += 1;
            anchored |= !is_feature_word(q) && !is_feature_word(name[j]);
        }
    }
    (
        PHONETIC_FULL * (2 * matched) as f64 / (query.len() + name.len()) as f64,
        anchored,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(s: &str) -> Vec<String> {
        tokenize(s)
    }

    fn found(table: &PlaceTable, s: &str) -> (String, MatchStage, bool) {
        match table.match_place(&t(s)) {
            MatchOutcome::Found {
                best, ambiguous, ..
            } => (table.places()[best.place].id.clone(), best.stage, ambiguous),
            other => panic!("{s:?}: {other:?}"),
        }
    }

    #[test]
    fn embedded_table_has_the_eight_viewpoints() {
        let table = PlaceTable::embedded();
        let ids: Vec<&str> = table.viewpoints().iter().map(|v| v.id.as_str()).collect();
        assert_eq!(
            ids,
            [
                "acadia.mcfarland",
                "big-bend.panther-junction",
                "grand-canyon.yavapai",
                "grand-teton.science-center",
                "point-reyes.beach",
                "rainier.paradise-mountain",
                "smokies.look-rock",
                "yosemite.turtleback",
            ]
        );
        assert_eq!(table.places()[0].id, "point-reyes", "MSV places sort first");
        assert_eq!(table.places()[1].id, "rainier");
        assert_eq!(table.places()[2].id, "grand-canyon");
        let gc = table.viewpoint("grand-canyon.yavapai").expect("present").1;
        assert_eq!(gc.tz, chrono_tz::America::Phoenix);
    }

    fn found_with_leftovers(table: &PlaceTable, s: &str) -> (String, Vec<String>) {
        match table.match_place(&t(s)) {
            MatchOutcome::Found {
                best, unexplained, ..
            } => (table.places()[best.place].id.clone(), unexplained),
            other => panic!("{s:?}: {other:?}"),
        }
    }

    fn closest(table: &PlaceTable, s: &str) -> Vec<String> {
        match table.match_place(&t(s)) {
            MatchOutcome::Miss { closest } => closest.into_iter().map(|c| c.place_id).collect(),
            other => panic!("{s:?} should miss: {other:?}"),
        }
    }

    #[test]
    fn fuzzy_score_basics() {
        assert_eq!(fuzzy_score(&["reyes"], &["point", "reyes"]), 1.0);
        let s = fuzzy_score(&["grand", "canyn"], &["grand", "canyon"]);
        assert!((s - 11.0 / 12.0).abs() < 1e-9, "{s}");
        assert!(fuzzy_score(&["golden", "gate"], &["grand", "teton"]) < 0.5);
        // Words the name cannot explain lower the score (a token-set ratio gives 1.0 here).
        let s = fuzzy_score(&["rainier", "visitor", "center"], &["rainier"]);
        assert!(s < FUZZY_ACCEPT, "{s}");
        // Written out in full: one word against two.
        assert!(fuzzy_score(&["pointreyes"], &["point", "reyes"]) > 0.9);
    }

    #[test]
    fn fuzzy_matches_must_be_anchored() {
        let anchored = |q: &[&str], n: &[&str]| fuzzy_anchored(q, n, q, n);
        // (a) a distinctive word.
        assert!(anchored(&["reyes"], &["point", "reyes"]));
        assert!(anchored(&["yosemitee"], &["yosemite"]));
        // (b) the whole name, misspelled.
        assert!(anchored(&["grand", "canyn"], &["grand", "canyon"]));
        assert!(anchored(&["big", "ben"], &["big", "bend"]));
        // (c) the same letters, spaced differently.
        assert!(anchored(&["pointreyes"], &["point", "reyes"]));
        // Only words for a kind of place, or a part of a name made of them: not anchored.
        assert!(!anchored(&["grand"], &["grand", "canyon"]));
        assert!(!anchored(&["point"], &["point", "reyes"]));
        assert!(!anchored(&["beach"], &["great", "beach"]));
        assert!(!anchored(&["rocky", "mountains"], &["smoky", "mountains"]));
        assert!(fuzzy_score(&["rocky", "mountains"], &["smoky", "mountains"]) >= FUZZY_ACCEPT);
        // "the mountain" is a name; "mountain" is not, even though their cores are the same. (In
        // the parser "the" is a filler unless an exact name protects it, so a misspelled "the
        // mountian" reaches the matcher as "mountian" and misses, with Rainier first.)
        assert!(!fuzzy_anchored(
            &["mountain"],
            &["mountain"],
            &["mountain"],
            &["the", "mountain"]
        ));
        assert!(anchored(&["south", "rin"], &["south", "rim"]));
        // Two misspelt words: run together they are only 0.82 alike, so only (b) anchors it.
        assert!(strsim::normalized_levenshtein("grndcanyn", "grandcanyon") < RESPACED_ACCEPT);
        assert!(anchored(&["grnd", "canyn"], &["grand", "canyon"]));
    }

    #[test]
    fn stages_in_order() {
        let table = PlaceTable::embedded();
        assert_eq!(
            found(table, "point rays"),
            ("point-reyes".into(), MatchStage::Alias, false)
        );
        assert_eq!(
            found(table, "mount rainer"),
            ("rainier".into(), MatchStage::Alias, false)
        );
        // "rainier paradise mountain" contains the alias "rainier paradise", so the alias stage
        // wins; "smokies look rock" (place ID plus viewpoint suffix) is only a canonical name.
        assert_eq!(
            found(table, "rainier paradise mountain"),
            ("rainier".into(), MatchStage::Alias, false)
        );
        assert_eq!(
            found(table, "teton science center"),
            ("grand-teton".into(), MatchStage::Alias, false)
        );
        assert_eq!(
            found(table, "yosemitee"),
            ("yosemite".into(), MatchStage::Fuzzy, false)
        );
        // "mount" is a feature word and "rayneer" is only 0.71 like "rainer", so the fuzzy stage
        // has no anchor; both words share Double Metaphone keys with "mount rainer".
        assert_eq!(
            found(table, "mount rayneer"),
            ("rainier".into(), MatchStage::Phonetic, false)
        );
        assert_eq!(
            found(table, "grand canyn"),
            ("grand-canyon".into(), MatchStage::Fuzzy, false)
        );
        assert_eq!(
            found(table, "big ben"),
            ("big-bend".into(), MatchStage::Fuzzy, false)
        );
        assert_eq!(
            found(table, "grnd canyn"),
            ("grand-canyon".into(), MatchStage::Fuzzy, false)
        );
        // Spelled too differently for fuzzy (< 0.80) but the same Double Metaphone keys.
        assert_eq!(
            found(table, "rayneer"),
            ("rainier".into(), MatchStage::Phonetic, false)
        );
        assert_eq!(
            found(table, "akadeeya"),
            ("acadia".into(), MatchStage::Phonetic, false)
        );
    }

    #[test]
    fn places_snow_globe_does_not_have_miss() {
        // Each of these once matched a known place at full confidence (adversarial review,
        // 2026-11-19): a bare viewpoint suffix ("beach", "science center") as an exact name, the
        // alias "the mountain" reduced to "mountain" by a token-set ratio, and the two-letter
        // Double Metaphone key RN that "rainier" shares with "reno".
        let table = PlaceTable::embedded();
        for s in [
            "pebble beach",
            "long beach",
            "venice beach",
            "myrtle beach",
            "beach",
            "pacific science center",
            "science center",
            "rocky mountain national park",
            "rocky mountains",
            "stone mountain",
            "table mountain",
            "mountain",
            "reno",
            "ryan",
            "ron",
            "rhine",
            "canyon",
            "antelope canyon",
        ] {
            assert_eq!(closest(table, s).len(), 3, "{s}");
        }
        // The closest places are still the right suggestions.
        assert_eq!(closest(table, "science center")[0], "grand-teton");
        assert_eq!(closest(table, "pebble beach")[0], "point-reyes");
    }

    #[test]
    fn leftover_words_are_reported_unless_the_place_explains_them() {
        let table = PlaceTable::embedded();
        assert_eq!(
            found_with_leftovers(table, "rainier beach"),
            ("rainier".into(), vec!["beach".to_string()])
        );
        assert_eq!(
            found_with_leftovers(table, "paradise valley"),
            ("rainier".into(), vec!["valley".to_string()])
        );
        assert_eq!(
            found_with_leftovers(table, "point reyes sunsets"),
            ("point-reyes".into(), vec!["sunsets".to_string()])
        );
        // "great" is a word of the Smokies' own names; "park" is generic.
        assert_eq!(
            found_with_leftovers(table, "great smokey mountains"),
            ("smokies".into(), vec![])
        );
        assert_eq!(
            found_with_leftovers(table, "point reyes park"),
            ("point-reyes".into(), vec![])
        );
    }

    #[test]
    fn short_phonetic_keys_need_short_keys() {
        let table = PlaceTable::embedded();
        assert_eq!(
            found(table, "rayneer"),
            ("rainier".into(), MatchStage::Phonetic, false)
        );
        // "reno" shares only the two-letter key RN with "rainier", so the phonetic stage gives
        // it nothing; its closest places come from spelling alone (0.4 against "reyes").
        match table.match_place(&t("reno")) {
            MatchOutcome::Miss { closest } => {
                assert!(
                    closest.iter().all(|c| c.score < FUZZY_ACCEPT),
                    "{closest:?}"
                );
                assert_eq!(closest[0].place_id, "point-reyes");
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn a_shared_first_word_alone_misses_with_both_places_first() {
        // "grand", "mount", "point" and "great" begin the names of two places each, and of many
        // places Snow Globe does not have ("Grand Mesa", "Mount Hood", "Sunset Point"): a miss
        // that suggests both, never a guess.
        let table = PlaceTable::embedded();
        for (s, first, second) in [
            ("grand", "grand-canyon", "grand-teton"),
            ("mount", "rainier", "acadia"),
            ("point", "point-reyes", "grand-canyon"),
            ("great", "point-reyes", "smokies"),
        ] {
            let c = closest(table, s);
            assert_eq!((c[0].as_str(), c[1].as_str()), (first, second), "{s}");
        }
        for s in ["big", "south", "sunset point"] {
            assert_eq!(closest(table, s).len(), 3, "{s}");
        }
    }

    /// A two-place table whose only names are long made-up words, so a query's fuzzy score
    /// against each is set by how many letters differ: 1 − d/len.
    fn margin_table(a: &str, b: &str) -> PlaceTable {
        let m = |id: &str, place: &str, alias: &str| {
            format!(
                r#"{{"id":"{id}","place_id":"{place}","place":"{place}","aliases":["{alias}"],"default":true,"time_zone":"UTC","status":"msv","camera":{{"lat":0,"lon":0,"elevation_m":0}},"truth_camera":{{"image_url":"https://x"}}}}"#
            )
        };
        let (ma, mb) = (m("alpha.x", "alpha", a), m("bravo.y", "bravo", b));
        PlaceTable::from_manifest_texts([("alpha.x", ma.as_str()), ("bravo.y", mb.as_str())])
            .expect("valid manifests")
    }

    /// `base` with the letters at `positions` replaced by "z".
    fn with_z(base: &str, positions: &[usize]) -> String {
        base.chars()
            .enumerate()
            .map(|(i, c)| if positions.contains(&i) { 'z' } else { c })
            .collect()
    }

    #[test]
    fn close_call_margin_decides_the_chip() {
        // 30 letters: 3 differences score 0.90, 4 score 0.867, 5 score 0.833.
        let query = "abcdefghijklmnopqrstuvwxyabcde";
        let a = with_z(query, &[1, 2, 3]);
        for (b_diffs, chip) in [(&[5, 6, 7, 8][..], true), (&[5, 6, 7, 8, 9][..], false)] {
            let table = margin_table(&a, &with_z(query, b_diffs));
            match table.match_place(&t(query)) {
                MatchOutcome::Found {
                    best,
                    runner_up,
                    ambiguous,
                    ..
                } => {
                    let r = runner_up.expect("runner-up");
                    assert_eq!(table.places()[best.place].id, "alpha");
                    assert!((best.score - 0.9).abs() < 1e-9, "{}", best.score);
                    assert_eq!(
                        ambiguous, chip,
                        "scores {:.3} and {:.3}, margin {MARGIN}",
                        best.score, r.score
                    );
                }
                other => panic!("{other:?}"),
            }
        }
        // A difference of exactly the margin is still a close call: 20 letters, 2 vs 3 changes.
        let query = "abcdefghijklmnopqrst";
        let table = margin_table(&with_z(query, &[1, 2]), &with_z(query, &[5, 6, 7]));
        assert!(matches!(
            table.match_place(&t(query)),
            MatchOutcome::Found {
                ambiguous: true,
                ..
            }
        ));
    }

    #[test]
    fn view_suffix_picks_a_viewpoint_only_inside_its_place() {
        let table = PlaceTable::embedded();
        let (pr, _) = table.place("point-reyes").expect("place");
        match table.match_viewpoint(Some(pr), &t("the beach")) {
            MatchOutcome::Found {
                best, unexplained, ..
            } => {
                assert_eq!(table.viewpoints()[best.viewpoint].id, "point-reyes.beach");
                assert!(unexplained.is_empty());
            }
            other => panic!("{other:?}"),
        }
        assert!(matches!(
            table.match_viewpoint(None, &t("beach")),
            MatchOutcome::Miss { .. }
        ));
    }

    #[test]
    fn misses_list_three_places() {
        let table = PlaceTable::embedded();
        match table.match_place(&t("golden gate")) {
            MatchOutcome::Miss { closest } => assert_eq!(closest.len(), 3),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn generic_words_alone_say_nothing() {
        let table = PlaceTable::embedded();
        assert_eq!(
            table.match_place(&t("the national park")),
            MatchOutcome::NothingSaid
        );
    }

    #[test]
    fn viewpoint_by_label_and_suffix() {
        let table = PlaceTable::embedded();
        let (gc, _) = table.place("grand-canyon").expect("place");
        match table.match_viewpoint(Some(gc), &t("yavapai")) {
            MatchOutcome::Found { best, .. } => assert_eq!(
                table.viewpoints()[best.viewpoint].id,
                "grand-canyon.yavapai"
            ),
            other => panic!("{other:?}"),
        }
        match table.match_viewpoint(None, &t("panther junction")) {
            MatchOutcome::Found { best, .. } => assert_eq!(
                table.viewpoints()[best.viewpoint].id,
                "big-bend.panther-junction"
            ),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn name_spans_protect_aliases() {
        let table = PlaceTable::embedded();
        let words = ["how", "does", "look", "rock", "look", "tomorrow"];
        assert_eq!(table.name_spans(&words), vec![(2, 4)]);
    }

    #[test]
    fn rejects_a_shared_alias() {
        let a = r#"{"id":"a.x","place_id":"a","place":"A","aliases":["same"],"default":true,"time_zone":"UTC","status":"msv","camera":{"lat":0,"lon":0,"elevation_m":0},"truth_camera":{"image_url":"https://x"}}"#;
        let b = r#"{"id":"b.y","place_id":"b","place":"B","aliases":["same"],"default":true,"time_zone":"UTC","status":"msv","camera":{"lat":0,"lon":0,"elevation_m":0},"truth_camera":{"image_url":"https://x"}}"#;
        let err =
            PlaceTable::from_manifest_texts([("a.x", a), ("b.y", b)]).expect_err("shared alias");
        assert!(err.to_string().contains("also used"), "{err}");
    }

    #[test]
    fn rejects_two_defaults_and_bad_zone() {
        let m = |id: &str, default: bool, tz: &str| {
            format!(
                r#"{{"id":"{id}","place_id":"p","place":"P","aliases":["{id}"],"default":{default},"time_zone":"{tz}","status":"msv","camera":{{"lat":0,"lon":0,"elevation_m":0}},"truth_camera":{{"image_url":null}}}}"#
            )
        };
        let (x, y) = (m("p.x", true, "UTC"), m("p.y", true, "UTC"));
        assert!(
            PlaceTable::from_manifest_texts([("p.x", x.as_str()), ("p.y", y.as_str())]).is_err()
        );
        let z = m("p.z", true, "Mars/Olympus");
        assert!(PlaceTable::from_manifest_texts([("p.z", z.as_str())]).is_err());
    }
}
