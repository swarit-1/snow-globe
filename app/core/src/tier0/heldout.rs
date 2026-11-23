//! Held-out frames never enter the Tier 0 library (CLAUDE.md "Held-out data"; D-019, D-022,
//! D-038, D-047, D-070, D-085).
//!
//! **Which split.** The rules come from eval's committed held-out split, so Tier 0 and the
//! evaluation hold out the same periods. eval keeps one file per split version and never edits an
//! old one (`heldout.py`'s `SPLIT_RESOURCES`: "Each file keeps its bytes"), and any change to the
//! split bumps its version (the split's `change_rule`), so a new rule always arrives as a new file.
//! The build script compiles in every `eval/src/snow_globe_eval/heldout_periods*.json`
//! ([`COMMITTED_SPLITS`]) and eval's `CURRENT_SPLIT_VERSION`, read from `heldout.py`
//! ([`EVAL_CURRENT_SPLIT_VERSION`]). [`HeldOutRules::committed`] applies the file with that
//! version, and refuses, so Tier 0 does not run, when two files claim one version, when no file
//! has the current version, or when a file newer than the current one is committed. A new split
//! version therefore takes effect at the next build without a code change, unless its shape is
//! one this module does not read, which is refused too. A test checks the choice against
//! `heldout.py`'s `SPLIT_RESOURCES` and against every split file in eval's folder.
//!
//! Three rules, all read from that split:
//!
//! 1. **The archive edge** (design H's held-out season, D-019): an archived frame is held out when
//!    any date it could be said to fall on, local or UTC, is on or after the split's
//!    `archive_guard.from_local_day` (1 Jun 2026; D-070's reading until the owner confirms D-038).
//!    The dates are those of capture's `held_out_reason` (workers/capture `heldout.py`):
//!    - the date heads of `ImageCreateDateTime` and `LocalTimeString`;
//!    - each of their clocks read as civil time in the camera's zone, both sides of a
//!      daylight-saving fold or gap, as a UTC date and a civil date (snow-globe-common's `wall`
//!      convention, and capture's civil readings);
//!    - `ImageCreateDateTime`'s clock at the zone's standard offset (D-047: NPS keeps it on
//!      standard time all year) and at standard + 1 h, as a UTC date and a civil date: the
//!      instants snow-globe-common's `label` convention can give (the label's offset is standard,
//!      or standard + 1 h for "Daylight");
//!    - for a stamp with a date but no readable clock, every UTC date its local day touches;
//!    - the record's own instants (the sidecar's `image_time_utc` and the file name's stamp), as a
//!      civil date and a UTC date, and the folder's date.
//!
//!    This set holds every date capture's guard reads, and a few more (standard + 1 h where no
//!    label says "Daylight", the file name and the folder), so it can only hold out more; the
//!    tests below replay capture's edge table, zone by zone. It **fails closed**: an archived
//!    record whose NPS stamps are given but yield no date, or which has no readable date at all,
//!    is refused ([`GuardVerdict::Unreadable`]).
//! 2. **The sealed window** (design P, D-019): no frame, live or archived, dated in the split's
//!    `sealed-window` period (15 Dec 2026 – 14 Jan 2027, plus any extension the split records for
//!    an outage) is used. The split counts whole local days; Tier 0 reads the local and the UTC
//!    date of every instant a sidecar gives, so the hours either side of the window's local edges
//!    are refused too. For a live frame those instants are its image time, its file name's stamp,
//!    its fetch time and each of capture's candidate image times (EXIF, the feed's JSON, HTTP
//!    Last-Modified), so a camera clock running fast at the window's end lets no frame through
//!    (`tier0::sidecar`). An archived frame's fetch time is the backfill's download, not its
//!    date, and is not read.
//! 3. **Every other period of the split** (today `summer-2026`, design H's held-out season),
//!    applied the same way to every frame at every viewpoint. Archived frames from it are refused
//!    by rule 1 already; a live frame dated in it would mean a camera clock months off. A period a
//!    later split adds is applied with no code change; one in a shape this module does not read
//!    (an open end, a malformed extension) makes the whole split refused, never skipped.
//!
//! The split's `quarantine.dir` must be `quarantine`, the folder name the library scanner refuses
//! in any letter case (D-022); a split naming another folder is refused until the scanner follows.
//!
//! Not encoded here: D-019's open option (b), under which frames dated after 14 Dec would stay out
//! of Tier 0 until M4 scores. It is the owner's call by 14 Dec; once eval's split records it as a
//! held-out period, rule 3 applies it (TODO(M2): confirm the shape eval gives it).

use std::collections::BTreeSet;
use std::sync::OnceLock;

use chrono::{DateTime, Duration, LocalResult, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc};
use chrono_tz::{OffsetComponents, Tz};
use serde::{Deserialize, Serialize};

use crate::tzutil::start_of_local_day;

// `EVAL_CURRENT_SPLIT_VERSION` and `COMMITTED_SPLITS`, written by the build script (module docs).
include!(concat!(env!("OUT_DIR"), "/heldout_splits.rs"));

/// The folder of the committed splits, relative to the repository root.
pub const SPLIT_DIR: &str = "eval/src/snow_globe_eval";
/// The `kind` the applied split must declare.
pub const SPLIT_KIND: &str = "snow-globe.heldout-periods";
/// The period every split must have: design P's sealed window (rule 2).
pub const SEALED_PERIOD: &str = "sealed-window";
/// The quarantine folder's name, which the library scanner refuses in any letter case (D-022).
pub const QUARANTINE_DIR: &str = "quarantine";

/// Why the split could not be read.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum HeldOutError {
    /// Not JSON, or a field is missing.
    #[error("held-out split: {0}")]
    Unreadable(String),
    /// A rule Tier 0 depends on is not in the form it expects (it refuses rather than guess).
    #[error("held-out split: {0}")]
    Invalid(String),
}

/// A held-out period of the split other than the sealed window (rule 3 of the module docs).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HeldOutPeriod {
    /// The split's name for it (`summer-2026`).
    pub name: String,
    /// The period and its extensions, as inclusive day ranges.
    pub ranges: Vec<(NaiveDate, NaiveDate)>,
}

/// The rules Tier 0 applies, read from the split.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HeldOutRules {
    /// The split's `version`.
    pub split_version: u32,
    /// The split's file name in eval's folder (empty for a split read from text).
    pub split_file: String,
    /// FNV-1a 64 of the split's text, hex: part of the library cache's rules fingerprint.
    pub split_fnv: String,
    /// Archived frames dated on or after this day (local or UTC) are held out.
    pub archive_from: NaiveDate,
    /// The sealed window and its extensions, as inclusive day ranges.
    pub sealed: Vec<(NaiveDate, NaiveDate)>,
    /// Every other period of the split, by name.
    pub periods: Vec<HeldOutPeriod>,
}

#[derive(Deserialize)]
struct RawSplit {
    kind: String,
    version: u32,
    periods: std::collections::BTreeMap<String, RawPeriod>,
    archive_guard: RawGuard,
    quarantine: RawQuarantine,
}

#[derive(Deserialize)]
struct RawPeriod {
    first_day: String,
    last_day: String,
    #[serde(default)]
    extensions: Vec<serde_json::Value>,
}

#[derive(Deserialize)]
struct RawGuard {
    from_local_day: String,
}

#[derive(Deserialize)]
struct RawQuarantine {
    dir: String,
}

#[derive(Deserialize)]
struct RawVersion {
    version: u32,
}

fn day(text: &str, what: &str) -> Result<NaiveDate, HeldOutError> {
    NaiveDate::parse_from_str(text, "%Y-%m-%d")
        .map_err(|_| HeldOutError::Invalid(format!("{what} {text:?} is not YYYY-MM-DD")))
}

/// A period and its extensions as day ranges; every extension must name a `first_day` and a
/// `last_day`, and no range may end before it starts.
fn period_ranges(name: &str, p: &RawPeriod) -> Result<Vec<(NaiveDate, NaiveDate)>, HeldOutError> {
    let mut ranges = vec![(
        day(&p.first_day, &format!("{name}.first_day"))?,
        day(&p.last_day, &format!("{name}.last_day"))?,
    )];
    for ext in &p.extensions {
        let field = |field: &str| {
            ext.get(field).and_then(|v| v.as_str()).ok_or_else(|| {
                HeldOutError::Invalid(format!("an extension of {name} has no {field}: {ext}"))
            })
        };
        ranges.push((
            day(field("first_day")?, &format!("{name} extension first_day"))?,
            day(field("last_day")?, &format!("{name} extension last_day"))?,
        ));
    }
    if ranges.iter().any(|(a, b)| a > b) {
        return Err(HeldOutError::Invalid(format!(
            "a range of {name} ends before it starts"
        )));
    }
    Ok(ranges)
}

fn fnv_hex(text: &str) -> String {
    format!("{:016x}", crate::tier0::library::fnv1a64(text.as_bytes()))
}

impl HeldOutRules {
    /// The rules of the split eval calls current, chosen from the committed files (module docs,
    /// "Which split"), parsed once.
    pub fn committed() -> Result<&'static HeldOutRules, HeldOutError> {
        static RULES: OnceLock<Result<HeldOutRules, HeldOutError>> = OnceLock::new();
        RULES
            .get_or_init(|| {
                HeldOutRules::from_committed(COMMITTED_SPLITS, EVAL_CURRENT_SPLIT_VERSION)
            })
            .as_ref()
            .map_err(Clone::clone)
    }

    /// The file name and text of the split [`HeldOutRules::committed`] applies.
    pub fn committed_text() -> Result<(&'static str, &'static str), HeldOutError> {
        let rules = HeldOutRules::committed()?;
        COMMITTED_SPLITS
            .iter()
            .copied()
            .find(|(name, _)| *name == rules.split_file)
            .ok_or_else(|| {
                HeldOutError::Invalid(format!("{} is not compiled in", rules.split_file))
            })
    }

    /// Choose among split files (`(file name, text)`) the one with version `current`, refusing
    /// two files with one version, a file newer than `current`, or no file at `current`.
    pub fn from_committed(
        files: &[(&str, &str)],
        current: u32,
    ) -> Result<HeldOutRules, HeldOutError> {
        let mut versions: Vec<(u32, &str, &str)> = Vec::with_capacity(files.len());
        for (name, text) in files {
            let v: RawVersion = serde_json::from_str(text)
                .map_err(|e| HeldOutError::Unreadable(format!("{name}: {e}")))?;
            if let Some((_, other, _)) = versions.iter().find(|(x, _, _)| *x == v.version) {
                return Err(HeldOutError::Invalid(format!(
                    "{other} and {name} both claim split version {}",
                    v.version
                )));
            }
            versions.push((v.version, name, text));
        }
        if let Some((v, name, _)) = versions.iter().find(|(v, _, _)| *v > current) {
            return Err(HeldOutError::Invalid(format!(
                "{name} is split version {v}, newer than eval's CURRENT_SPLIT_VERSION {current}: \
                 Tier 0 refuses to run until eval makes it current (or the file is removed)"
            )));
        }
        let Some((_, name, text)) = versions.iter().find(|(v, _, _)| *v == current) else {
            return Err(HeldOutError::Invalid(format!(
                "no committed split file has eval's CURRENT_SPLIT_VERSION {current}"
            )));
        };
        let mut rules = HeldOutRules::from_split_json(text)?;
        rules.split_file = (*name).to_string();
        Ok(rules)
    }

    /// Read the rules from a split file's text (module docs). Every period must have a
    /// `first_day` and a `last_day`, and so must every extension; anything else is refused, never
    /// skipped.
    pub fn from_split_json(text: &str) -> Result<HeldOutRules, HeldOutError> {
        let raw: RawSplit =
            serde_json::from_str(text).map_err(|e| HeldOutError::Unreadable(e.to_string()))?;
        if raw.kind != SPLIT_KIND {
            return Err(HeldOutError::Invalid(format!(
                "kind {:?} is not {SPLIT_KIND:?}",
                raw.kind
            )));
        }
        if raw.quarantine.dir != QUARANTINE_DIR {
            return Err(HeldOutError::Invalid(format!(
                "quarantine.dir {:?} is not {QUARANTINE_DIR:?}, the folder the library scanner \
                 refuses",
                raw.quarantine.dir
            )));
        }
        let archive_from = day(
            &raw.archive_guard.from_local_day,
            "archive_guard.from_local_day",
        )?;
        let sealed_period = raw
            .periods
            .get(SEALED_PERIOD)
            .ok_or_else(|| HeldOutError::Invalid(format!("no {SEALED_PERIOD} period")))?;
        let sealed = period_ranges(SEALED_PERIOD, sealed_period)?;
        let mut periods = Vec::new();
        for (name, p) in &raw.periods {
            if name != SEALED_PERIOD {
                periods.push(HeldOutPeriod {
                    name: name.clone(),
                    ranges: period_ranges(name, p)?,
                });
            }
        }
        Ok(HeldOutRules {
            split_version: raw.version,
            split_file: String::new(),
            split_fnv: fnv_hex(text),
            archive_from,
            sealed,
            periods,
        })
    }

    /// True if `date` falls in the sealed window or one of its extensions.
    pub fn in_sealed_window(&self, date: NaiveDate) -> bool {
        self.sealed.iter().any(|(a, b)| (*a..=*b).contains(&date))
    }

    /// The first date of `dates` inside the sealed window, if any.
    pub fn sealed_date(&self, dates: &BTreeSet<NaiveDate>) -> Option<NaiveDate> {
        dates.iter().copied().find(|d| self.in_sealed_window(*d))
    }

    /// The first period (by name) other than the sealed window that holds one of `dates`, and
    /// that date (rule 3 of the module docs).
    pub fn period_date(&self, dates: &BTreeSet<NaiveDate>) -> Option<(&str, NaiveDate)> {
        self.periods.iter().find_map(|p| {
            dates
                .iter()
                .copied()
                .find(|d| p.ranges.iter().any(|(a, b)| (*a..=*b).contains(d)))
                .map(|d| (p.name.as_str(), d))
        })
    }

    /// The archive guard's verdict on an archived record's dates (rule 1 of the module docs).
    pub fn archive_verdict(&self, dates: &ArchiveDates) -> GuardVerdict {
        if dates.raw_given && dates.from_stamp_heads.is_empty() {
            return GuardVerdict::Unreadable(
                "NPS stamps are given but no date can be read from them".to_string(),
            );
        }
        let latest_local = dates.local.iter().next_back().copied();
        let latest_utc = dates.utc.iter().next_back().copied();
        let latest = match (latest_local, latest_utc) {
            (None, None) => {
                return GuardVerdict::Unreadable("no readable date".to_string());
            }
            (a, b) => a.max(b),
        };
        match latest {
            Some(date) if date >= self.archive_from => GuardVerdict::HeldOut {
                date,
                which: if latest_local == Some(date) {
                    DateKind::Local
                } else {
                    DateKind::Utc
                },
            },
            _ => GuardVerdict::Allowed,
        }
    }
}

/// What the archive guard decided.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GuardVerdict {
    /// Every reading is before the edge.
    Allowed,
    /// A reading is on or after the edge.
    HeldOut {
        /// The latest date read.
        date: NaiveDate,
        /// Whether that date is a local or a UTC reading.
        which: DateKind,
    },
    /// The guard cannot date the record, so it refuses it (fails closed).
    Unreadable(String),
}

/// A local (camera zone) or a UTC reading of a date.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DateKind {
    /// The civil date in the camera's zone.
    Local,
    /// The UTC date.
    Utc,
}

/// Every date an archived record could be said to fall on (rule 1 of the module docs).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ArchiveDates {
    /// Civil dates in the camera's zone.
    pub local: BTreeSet<NaiveDate>,
    /// UTC dates.
    pub utc: BTreeSet<NaiveDate>,
    /// Dates read from the NPS stamps' date heads and from `ImageCreateDateTime` read as local
    /// standard time: capture's "local dates", whose absence with stamps given refuses the record.
    pub from_stamp_heads: BTreeSet<NaiveDate>,
    /// True if either NPS stamp is given (not blank).
    pub raw_given: bool,
}

impl ArchiveDates {
    /// Every date, local and UTC.
    pub fn all(&self) -> BTreeSet<NaiveDate> {
        self.local.union(&self.utc).copied().collect()
    }
}

/// The civil and UTC dates of instants in a zone (both readings of each).
pub fn instant_dates(
    instants: &[DateTime<Utc>],
    tz: Tz,
) -> (BTreeSet<NaiveDate>, BTreeSet<NaiveDate>) {
    let mut local = BTreeSet::new();
    let mut utc = BTreeSet::new();
    for t in instants {
        local.insert(t.with_timezone(&tz).date_naive());
        utc.insert(t.date_naive());
    }
    (local, utc)
}

/// Every date of an archived record: its NPS stamps (`ImageCreateDateTime`, `LocalTimeString`),
/// its instants and any extra UTC dates (the folder's), in the camera's zone `tz`.
pub fn archive_dates(
    image_create_datetime: Option<&str>,
    local_time_string: Option<&str>,
    instants: &[DateTime<Utc>],
    extra_utc_dates: &[NaiveDate],
    tz: Tz,
) -> ArchiveDates {
    let icd = image_create_datetime.unwrap_or("");
    let lts = local_time_string.unwrap_or("");
    let mut out = ArchiveDates {
        raw_given: !icd.trim().is_empty() || !lts.trim().is_empty(),
        ..ArchiveDates::default()
    };
    for text in [icd, lts] {
        if let Some(d) = us_date_head(text) {
            out.local.insert(d);
            out.from_stamp_heads.insert(d);
        }
    }
    let icd_wall = us_clock(icd, true);
    let lts_wall = us_clock(lts, false);
    for (text, wall) in [(icd, icd_wall), (lts, lts_wall)] {
        match wall {
            Some(w) => {
                for inst in civil_readings(w, tz) {
                    out.utc.insert(inst.date_naive());
                    out.local.insert(inst.with_timezone(&tz).date_naive());
                }
            }
            None => {
                if let Some(d) = us_date_head(text) {
                    out.utc.extend(local_day_utc_dates(d, tz));
                }
            }
        }
    }
    if let Some(w) = icd_wall {
        // D-047's standard-time reading, and the `label` convention's two possible offsets.
        let standard_offset = standard_offset(w, tz);
        let standard = Utc.from_utc_datetime(&(w - standard_offset));
        let daylight = Utc.from_utc_datetime(&(w - standard_offset - Duration::hours(1)));
        // Capture counts the standard-time reading among the local dates whose absence (with
        // stamps given) refuses a record.
        out.from_stamp_heads
            .insert(standard.with_timezone(&tz).date_naive());
        for inst in [standard, daylight] {
            out.utc.insert(inst.date_naive());
            out.local.insert(inst.with_timezone(&tz).date_naive());
        }
    }
    let (local, utc) = instant_dates(instants, tz);
    out.local.extend(local);
    out.utc.extend(utc);
    out.utc.extend(extra_utc_dates.iter().copied());
    out
}

/// The zone's standard (non-daylight) UTC offset around a wall time.
fn standard_offset(wall: NaiveDateTime, tz: Tz) -> Duration {
    tz.offset_from_utc_datetime(&wall).base_utc_offset()
}

/// A wall clock read as civil time in the zone: one instant, or both sides of a fold, or, inside
/// a spring-forward gap, the instants at the offsets before and after it (as Python's `fold=0`
/// and `fold=1` do).
fn civil_readings(wall: NaiveDateTime, tz: Tz) -> Vec<DateTime<Utc>> {
    match tz.from_local_datetime(&wall) {
        LocalResult::Single(t) => vec![t.with_timezone(&Utc)],
        LocalResult::Ambiguous(a, b) => vec![a.with_timezone(&Utc), b.with_timezone(&Utc)],
        LocalResult::None => {
            let standard = standard_offset(wall, tz);
            vec![
                Utc.from_utc_datetime(&(wall - standard)),
                Utc.from_utc_datetime(&(wall - standard - Duration::hours(1))),
            ]
        }
    }
}

/// The UTC dates of the first and the last instant of a local day (a date read without a clock:
/// the guard takes every UTC date the day touches).
fn local_day_utc_dates(d: NaiveDate, tz: Tz) -> [NaiveDate; 2] {
    let first = start_of_local_day(tz, d).with_timezone(&Utc);
    let next = d.succ_opt().unwrap_or(d);
    let last = start_of_local_day(tz, next).with_timezone(&Utc) - Duration::microseconds(1);
    [first.date_naive(), last.date_naive()]
}

/// Read `M/D/YYYY` at the head of a stamp (the text before its first space), as capture's
/// `_us_date` does.
pub fn us_date_head(text: &str) -> Option<NaiveDate> {
    let head = text.trim().split(' ').next().unwrap_or("");
    let mut c = Scan::new(head);
    let date = c.date()?;
    c.at_end().then_some(date)
}

/// Read an NPS clock, `M/D/YYYY h:mm[:ss] AM|PM`, as capture's `_NPS_CLOCK_RE` (`whole`: the
/// whole text, as `ImageCreateDateTime`) or `_LABEL_CLOCK_RE` (a prefix followed by white space
/// or the end, as `LocalTimeString`'s zone name follows) does. The hour must be 1–12.
pub fn us_clock(text: &str, whole: bool) -> Option<NaiveDateTime> {
    let mut c = Scan::new(text);
    c.spaces();
    let date = c.date()?;
    if c.spaces() == 0 {
        return None;
    }
    let hour = c.number(1, 2)?;
    c.byte(b':')?;
    let minute = c.number(2, 2)?;
    let second = if c.peek() == Some(b':') {
        c.byte(b':')?;
        c.number(2, 2)?
    } else {
        0
    };
    c.spaces();
    let pm = match c.next_lower()? {
        b'a' => false,
        b'p' => true,
        _ => return None,
    };
    if c.next_lower()? != b'm' {
        return None;
    }
    if whole {
        c.spaces();
        if !c.at_end() {
            return None;
        }
    } else if !(c.at_end() || c.peek().is_some_and(|b| b.is_ascii_whitespace())) {
        return None;
    }
    if !(1..=12).contains(&hour) {
        return None;
    }
    let hour24 = hour % 12 + if pm { 12 } else { 0 };
    let time = NaiveTime::from_hms_opt(hour24, minute, second)?;
    Some(date.and_time(time))
}

/// A tiny byte scanner for the NPS stamps.
struct Scan<'a> {
    s: &'a [u8],
    i: usize,
}

impl<'a> Scan<'a> {
    fn new(s: &'a str) -> Scan<'a> {
        Scan {
            s: s.as_bytes(),
            i: 0,
        }
    }
    fn peek(&self) -> Option<u8> {
        self.s.get(self.i).copied()
    }
    fn at_end(&self) -> bool {
        self.i >= self.s.len()
    }
    fn next_lower(&mut self) -> Option<u8> {
        let b = self.peek()?;
        self.i += 1;
        Some(b.to_ascii_lowercase())
    }
    fn byte(&mut self, want: u8) -> Option<()> {
        (self.peek()? == want).then(|| self.i += 1)
    }
    /// White space skipped, counted.
    fn spaces(&mut self) -> usize {
        let start = self.i;
        while self.peek().is_some_and(|b| b.is_ascii_whitespace()) {
            self.i += 1;
        }
        self.i - start
    }
    /// `min..=max` ASCII digits, greedy.
    fn number(&mut self, min: usize, max: usize) -> Option<u32> {
        let start = self.i;
        while self.i - start < max && self.peek().is_some_and(|b| b.is_ascii_digit()) {
            self.i += 1;
        }
        let n = self.i - start;
        if n < min {
            return None;
        }
        std::str::from_utf8(&self.s[start..self.i])
            .ok()?
            .parse()
            .ok()
    }
    /// `M/D/YYYY` (one or two digits for the month and day, four for the year).
    fn date(&mut self) -> Option<NaiveDate> {
        let month = self.number(1, 2)?;
        self.byte(b'/')?;
        let dom = self.number(1, 2)?;
        self.byte(b'/')?;
        let year = self.number(4, 4)?;
        if self.peek().is_some_and(|b| b.is_ascii_digit()) {
            return None;
        }
        NaiveDate::from_ymd_opt(i32::try_from(year).ok()?, month, dom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const LA: Tz = chrono_tz::America::Los_Angeles;

    fn d(y: i32, m: u32, dd: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, dd).expect("date")
    }

    fn rules() -> &'static HeldOutRules {
        HeldOutRules::committed().expect("the committed split parses")
    }

    /// The guard's verdict on two stamps alone, as capture's `held_out_reason(icd, lts, tz)`.
    fn verdict(icd: &str, lts: &str, tz: Tz) -> GuardVerdict {
        rules().archive_verdict(&archive_dates(Some(icd), Some(lts), &[], &[], tz))
    }

    fn held(icd: &str, lts: &str, tz: Tz) -> bool {
        verdict(icd, lts, tz) != GuardVerdict::Allowed
    }

    /// A file of eval's split folder, read when the test runs.
    fn eval_file(name: &str) -> String {
        let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join(SPLIT_DIR)
            .join(name);
        std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()))
    }

    #[test]
    fn the_applied_split_is_the_one_eval_calls_current() {
        let py = eval_file("heldout.py");
        // eval's CURRENT_SPLIT_VERSION and SPLIT_RESOURCES, read here independently of the build
        // script: `SPLIT_RESOURCES ... {1: "heldout_periods.json", 2: "heldout_periods_v2.json"}`.
        let current: u32 = py
            .lines()
            .find_map(|l| {
                let rest = l.strip_prefix("CURRENT_SPLIT_VERSION")?;
                let value = rest.split_once('=')?.1;
                value.split('#').next()?.trim().parse().ok()
            })
            .expect("a `CURRENT_SPLIT_VERSION = <n>` line in heldout.py");
        assert_eq!(
            EVAL_CURRENT_SPLIT_VERSION, current,
            "the build script's reading"
        );
        let start = py.find("SPLIT_RESOURCES").expect("SPLIT_RESOURCES");
        let open = start + py[start..].find('{').expect("{");
        let close = open + py[open..].find('}').expect("}");
        let resources: std::collections::BTreeMap<u32, String> = py[open + 1..close]
            .split(',')
            .filter(|s| !s.trim().is_empty())
            .map(|pair| {
                let (v, name) = pair.split_once(':').expect("<version>: <file>");
                let v = v.trim().parse().expect("a version number");
                (v, name.trim().trim_matches('"').to_string())
            })
            .collect();
        let r = rules();
        assert_eq!(r.split_version, current);
        assert_eq!(
            Some(&r.split_file),
            resources.get(&current),
            "SPLIT_RESOURCES[CURRENT_SPLIT_VERSION]"
        );
        assert_eq!(
            r.split_fnv,
            fnv_hex(HeldOutRules::committed_text().expect("text").1)
        );

        // Every split in eval's folder is compiled in with its bytes (a stale build fails here),
        // and is named so the build script finds it; SPLIT_RESOURCES names exactly those files.
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join(SPLIT_DIR);
        let mut on_disk = std::collections::BTreeMap::new();
        for e in std::fs::read_dir(&dir).expect("eval's folder") {
            let name = e.expect("entry").file_name().to_string_lossy().into_owned();
            if !name.ends_with(".json") {
                continue;
            }
            let text = eval_file(&name);
            let kind = serde_json::from_str::<serde_json::Value>(&text)
                .ok()
                .and_then(|v| v["kind"].as_str().map(str::to_string))
                .unwrap_or_default();
            let is_split = kind.ends_with(".heldout-periods");
            assert_eq!(
                is_split,
                name.starts_with("heldout_periods"),
                "{name}: a split must be named heldout_periods*.json, and only a split may be"
            );
            if is_split {
                on_disk.insert(name, text);
            }
        }
        let compiled: std::collections::BTreeMap<String, String> = COMMITTED_SPLITS
            .iter()
            .map(|(n, t)| ((*n).to_string(), (*t).to_string()))
            .collect();
        assert_eq!(
            compiled, on_disk,
            "the splits compiled in are not the ones in eval's folder: a split file was added, \
             removed or renamed without heldout.py changing (a change there re-runs the build \
             script). Name it in SPLIT_RESOURCES, or touch app/core/build.rs, and rebuild"
        );
        let named: BTreeSet<&str> = resources.values().map(String::as_str).collect();
        let files: BTreeSet<&str> = compiled.keys().map(String::as_str).collect();
        assert_eq!(named, files, "SPLIT_RESOURCES names every split file");
    }

    #[test]
    fn the_current_split_holds_out_what_d019_and_d070_say() {
        let r = rules();
        assert_eq!(r.archive_from, d(2026, 6, 1));
        assert_eq!(r.sealed, vec![(d(2026, 12, 15), d(2027, 1, 14))]);
        assert_eq!(
            r.periods,
            vec![HeldOutPeriod {
                name: "summer-2026".to_string(),
                ranges: vec![(d(2026, 6, 1), d(2026, 8, 31))],
            }]
        );
        assert!(r.in_sealed_window(d(2026, 12, 15)));
        assert!(r.in_sealed_window(d(2027, 1, 14)));
        assert!(!r.in_sealed_window(d(2026, 12, 14)));
        assert!(!r.in_sealed_window(d(2027, 1, 15)));
        assert_eq!(
            r.period_date(&BTreeSet::from([d(2026, 5, 31), d(2026, 7, 15)])),
            Some(("summer-2026", d(2026, 7, 15)))
        );
        assert_eq!(r.period_date(&BTreeSet::from([d(2026, 9, 1)])), None);
    }

    #[test]
    fn the_choice_among_split_files_fails_closed() {
        let (_, text) = HeldOutRules::committed_text().expect("text");
        let at = |v: u32| {
            let mut j: serde_json::Value = serde_json::from_str(text).expect("json");
            j["version"] = serde_json::json!(v);
            j.to_string()
        };
        let (v2, v3) = (at(2), at(3));
        // The current version is chosen, whatever the files' order.
        let r = HeldOutRules::from_committed(&[("b.json", &v3), ("a.json", &v2)], 3).expect("v3");
        assert_eq!((r.split_version, r.split_file.as_str()), (3, "b.json"));
        // A file newer than the current version is refused, not ignored.
        assert!(HeldOutRules::from_committed(&[("a.json", &v2), ("b.json", &v3)], 2).is_err());
        // Two files with one version; no file at the current version; a broken file.
        assert!(HeldOutRules::from_committed(&[("a.json", &v2), ("b.json", &v2)], 2).is_err());
        assert!(HeldOutRules::from_committed(&[("a.json", &v2)], 3).is_err());
        assert!(HeldOutRules::from_committed(&[("a.json", &v2), ("b.json", "{oops")], 2).is_err());
    }

    #[test]
    fn every_period_is_read_and_anything_unreadable_is_refused() {
        let (_, text) = HeldOutRules::committed_text().expect("text");
        let base: serde_json::Value = serde_json::from_str(text).expect("json");
        // An outage extension of the sealed window.
        let mut with = base.clone();
        with["periods"]["sealed-window"]["extensions"] = serde_json::json!([
            {"first_day": "2027-01-15", "last_day": "2027-01-16", "reason": "outage"}
        ]);
        let r = HeldOutRules::from_split_json(&with.to_string()).expect("parses");
        assert!(r.in_sealed_window(d(2027, 1, 16)));
        assert!(!r.in_sealed_window(d(2027, 1, 17)));
        // A period a later split adds is applied with no code change.
        let mut more = base.clone();
        more["periods"]["post-freeze"] = serde_json::json!(
            {"design": "P", "first_day": "2026-12-15", "last_day": "2027-03-31", "extensions": []}
        );
        let r = HeldOutRules::from_split_json(&more.to_string()).expect("parses");
        assert_eq!(
            r.period_date(&BTreeSet::from([d(2027, 2, 1)])),
            Some(("post-freeze", d(2027, 2, 1)))
        );
        // Shapes it does not read are refused, never skipped.
        let cases: [(&[&str], serde_json::Value); 7] = [
            (
                &["periods", "sealed-window", "extensions"],
                serde_json::json!([{"days": 2}]),
            ),
            (
                &["periods", "post-freeze"],
                serde_json::json!({"first_day": "2026-12-15", "last_day": null}),
            ),
            (
                &["periods", "summer-2026", "last_day"],
                serde_json::json!("2026-08-31x"),
            ),
            (
                &["periods", "summer-2026", "first_day"],
                serde_json::json!("2026-09-01"),
            ),
            (&["kind"], serde_json::json!("something-else")),
            (&["quarantine", "dir"], serde_json::json!("held-out")),
            (
                &["archive_guard", "from_local_day"],
                serde_json::json!(null),
            ),
        ];
        for (path, value) in cases {
            let mut bad = base.clone();
            let mut at = &mut bad;
            for k in path {
                at = &mut at[*k];
            }
            *at = value;
            assert!(
                HeldOutRules::from_split_json(&bad.to_string()).is_err(),
                "{path:?}"
            );
        }
        let mut no_sealed = base;
        no_sealed["periods"]
            .as_object_mut()
            .expect("periods")
            .remove(SEALED_PERIOD);
        assert!(HeldOutRules::from_split_json(&no_sealed.to_string()).is_err());
    }

    #[test]
    fn clocks_parse_like_capture() {
        let t = us_clock("05/31/2026 11:30:00 PM", true).expect("clock");
        assert_eq!(t.to_string(), "2026-05-31 23:30:00");
        assert_eq!(
            us_clock("5/31/2026 12:05 AM Pacific Daylight Time", false)
                .expect("label")
                .to_string(),
            "2026-05-31 00:05:00"
        );
        assert!(us_clock("5/31/2026 12:05 AMPacific", false).is_none());
        assert!(us_clock("05/31/2026 11:30:00 PM trailing", true).is_none());
        assert!(
            us_clock("05/31/2026 13:30:00 PM", true).is_none(),
            "hour 13"
        );
        assert!(us_clock("05/31/2026 25:61", true).is_none());
        assert!(
            us_clock("05/31/26 11:30 PM", true).is_none(),
            "two-digit year"
        );
        assert!(us_clock("11:30PM", true).is_none());
        assert_eq!(us_date_head("  5/31/2026 garbage"), Some(d(2026, 5, 31)));
        assert_eq!(us_date_head("02/30/2026"), None);
        assert_eq!(us_date_head("garbage"), None);
    }

    // The cases of capture's tests (workers/capture/tests/test_archive.py), D-047 and D-070.

    #[test]
    fn reads_every_local_date_and_fails_closed() {
        assert!(!held(
            "01/15/2026 12:00:00 PM",
            "1/15/2026 12:00 PM Pacific Standard Time",
            LA
        ));
        assert!(!held(
            "07/15/2025 01:15:00 PM",
            "7/15/2025 2:15 PM Pacific Daylight Time",
            LA
        ));
        assert_eq!(
            verdict(
                "07/15/2026 01:15:00 PM",
                "7/15/2026 2:15 PM Pacific Daylight Time",
                LA
            ),
            GuardVerdict::HeldOut {
                date: d(2026, 7, 15),
                which: DateKind::Local
            }
        );
        assert!(held(
            "05/31/2026 11:30:00 PM",
            "6/1/2026 12:30 AM Pacific Daylight Time",
            LA
        ));
        assert!(matches!(
            verdict("garbage", "", LA),
            GuardVerdict::Unreadable(_)
        ));
    }

    #[test]
    fn the_summer_edge_without_local_time_string_is_refused() {
        assert!(held("05/31/2026 11:30:00 PM", "", LA));
        assert!(
            !held("05/31/2026 12:00:00 PM", "", LA),
            "noon, not midnight"
        );
        assert!(held(
            "05/31/2026 11:30:00 PM",
            "",
            chrono_tz::America::New_York
        ));
        assert!(!held("01/15/2026 11:30:00 PM", "", LA));
        assert!(!held("05/30/2026 11:45:00 PM", "", LA));
    }

    #[test]
    fn the_guard_reads_utc_so_31_may_local_evening_is_held_out() {
        // capture's UTC_EDGE: the last ImageCreateDateTime of 31 May 2026 (local standard time)
        // before UTC midnight, per zone.
        let edges: [(Tz, &str, &str, &str); 5] = [
            (LA, "03:59:59 PM", "04:00:00 PM", "Pacific"),
            (
                chrono_tz::America::Phoenix,
                "04:59:59 PM",
                "05:00:00 PM",
                "US Mountain",
            ),
            (
                chrono_tz::America::Denver,
                "04:59:59 PM",
                "05:00:00 PM",
                "Mountain",
            ),
            (
                chrono_tz::America::Chicago,
                "05:59:59 PM",
                "06:00:00 PM",
                "Central",
            ),
            (
                chrono_tz::America::New_York,
                "06:59:59 PM",
                "07:00:00 PM",
                "Eastern",
            ),
        ];
        for (tz, last_ok, first_out, region) in edges {
            let daylight = tz != chrono_tz::America::Phoenix;
            let label = |stamp: &str| {
                let wall = NaiveDateTime::parse_from_str(
                    &format!("05/31/2026 {stamp}"),
                    "%m/%d/%Y %I:%M:%S %p",
                )
                .expect("stamp");
                let civil = wall + Duration::hours(if daylight { 1 } else { 0 });
                let kind = if daylight { "Daylight" } else { "Standard" };
                format!(
                    "{} {region} {kind} Time",
                    civil.format("%-m/%-d/%Y %-I:%M %p")
                )
            };
            for with_label in [true, false] {
                let ok_lts = if with_label {
                    label(last_ok)
                } else {
                    String::new()
                };
                let out_lts = if with_label {
                    label(first_out)
                } else {
                    String::new()
                };
                let ok = verdict(&format!("05/31/2026 {last_ok}"), &ok_lts, tz);
                assert_eq!(ok, GuardVerdict::Allowed, "{tz} {last_ok} {ok_lts}");
                let out = verdict(&format!("05/31/2026 {first_out}"), &out_lts, tz);
                assert_eq!(
                    out,
                    GuardVerdict::HeldOut {
                        date: d(2026, 6, 1),
                        which: DateKind::Utc
                    },
                    "{tz} {first_out} {out_lts}"
                );
            }
        }
    }

    #[test]
    fn every_utc_reading_counts_and_a_date_without_a_clock_fails_closed() {
        let utc = |icd: &str, lts: &str| archive_dates(Some(icd), Some(lts), &[], &[], LA).utc;
        assert!(utc("", "5/31/2026 11:30 PM Pacific Daylight Time").contains(&d(2026, 6, 1)));
        assert!(utc("", "5/31/2026").contains(&d(2026, 6, 1)));
        assert!(held("", "5/31/2026", LA));
        assert!(utc("05/31/2026 25:61", "").contains(&d(2026, 6, 1)));
        assert!(utc("11/01/2026 01:30:00 AM", "").contains(&d(2026, 11, 1)));
        // The record's own instant is read too, local and UTC: 17:30 PDT on 31 May is 1 Jun UTC.
        let instant = Utc
            .with_ymd_and_hms(2026, 6, 1, 0, 30, 0)
            .single()
            .expect("t");
        let dates = archive_dates(Some("01/15/2026 12:00:00 PM"), None, &[instant], &[], LA);
        assert!(rules().archive_verdict(&dates) != GuardVerdict::Allowed);
        let only_instant = archive_dates(None, None, &[instant], &[], LA);
        assert_eq!(
            only_instant.all().into_iter().collect::<Vec<_>>(),
            vec![d(2026, 5, 31), d(2026, 6, 1)]
        );
        // No stamps and no instant: nothing to date it by, so refused.
        assert!(matches!(
            rules().archive_verdict(&archive_dates(None, None, &[], &[], LA)),
            GuardVerdict::Unreadable(_)
        ));
    }

    #[test]
    fn arizona_keeps_standard_time() {
        let az = chrono_tz::America::Phoenix;
        let dates = archive_dates(Some("05/31/2026 11:30:00 PM"), None, &[], &[], az);
        // 23:30 MST on 31 May is 06:30 UTC on 1 Jun: held out by its UTC date.
        assert!(dates.utc.contains(&d(2026, 6, 1)));
        assert_eq!(
            rules().archive_verdict(&dates),
            GuardVerdict::HeldOut {
                date: d(2026, 6, 1),
                which: DateKind::Utc
            }
        );
    }
}
