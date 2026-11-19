//! The hand-written time grammar (PRD §7.3 step 2): find the time expression among the words.
//!
//! [`extract`] scans the normalised words left to right. At each position it tries every matcher
//! below and keeps the longest match (ties go to the earlier matcher in the list); matched words
//! are *consumed* and the rest are left for the place matcher. Words inside an exact place name
//! are protected and never consumed.
//!
//! | Matcher | Examples |
//! |---|---|
//! | now | now, right now, currently, at the moment, live |
//! | relative | in two hours, in 30 minutes, in an hour, in half an hour, in an hour and a half, two hours from now, in a couple of hours, in 3 days |
//! | date | november 21, 21 november, nov 21, 11/21, 11/21/2026, the 21st, the 21st of november, november twenty first |
//! | day | today, tomorrow, the day after tomorrow, yesterday, friday, fri, this friday, next friday, on friday (weekdays may be abbreviated: mon, tue/tues, wed/weds, thu/thur/thurs, fri, sat; "sun" is the Sun) |
//! | solar | sunrise, sunset, sundown, dawn, first light, dusk, golden hour, magic hour, blue hour |
//! | noon / midnight | noon, midday, 12 noon, midnight |
//! | part of day | morning, this morning, in the afternoon, evening, tonight, at night, nite, last night |
//! | clock | at 9, 4:30, 4 30, 4 pm, four, four thirty, four oh five, four o'clock, half past four, quarter to five, ten past four, 16:00 |
//!
//! Number words and digits are both accepted. Two expressions of the same kind ("at 4 at 5") are
//! a conflict, which the parser hands to the fallback (PRD §7.3).

use chrono::Weekday;
use serde::{Deserialize, Serialize};

/// AM or PM.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Meridiem {
    /// Before noon.
    Am,
    /// Noon and after.
    Pm,
}

impl Meridiem {
    /// The other half of the day.
    pub fn flip(self) -> Meridiem {
        match self {
            Meridiem::Am => Meridiem::Pm,
            Meridiem::Pm => Meridiem::Am,
        }
    }
}

/// A named part of the day.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PartOfDay {
    /// Morning.
    Morning,
    /// Afternoon.
    Afternoon,
    /// Evening.
    Evening,
    /// Night.
    Night,
}

/// How a part of the day was said.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PartScope {
    /// "morning", "in the morning", "at night".
    Bare,
    /// "this morning", "this evening": today, rolled to tomorrow if passed.
    This,
    /// "tonight".
    Tonight,
}

/// A part of the day and how it was said.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct PartSpec {
    /// Which part.
    pub part: PartOfDay,
    /// How it was said.
    pub scope: PartScope,
}

/// A solar event word.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SolarWord {
    /// sunrise, sun up.
    Sunrise,
    /// sunset, sundown.
    Sunset,
    /// dawn, daybreak, first light.
    Dawn,
    /// dusk, nightfall.
    Dusk,
    /// golden hour, magic hour.
    GoldenHour,
    /// blue hour.
    BlueHour,
}

/// A day named in the utterance.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum DaySpec {
    /// today.
    Today,
    /// tomorrow.
    Tomorrow,
    /// the day after tomorrow.
    DayAfterTomorrow,
    /// yesterday (always in the past).
    Yesterday,
    /// A weekday; `next` is true for "next friday".
    Weekday {
        /// The weekday.
        day: Weekday,
        /// "next friday" (first occurrence at least one day ahead).
        next: bool,
    },
    /// A calendar date; `month` is `None` for "the 21st".
    Date {
        /// 1–12, or `None` for "the 21st".
        month: Option<u32>,
        /// 1–31.
        day: u32,
        /// Explicit year, if said.
        year: Option<i32>,
    },
}

/// A clock time as said.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Clock {
    /// Hour as said: 1–12 unless `twenty_four`, then 0–23.
    pub hour: u32,
    /// Minute 0–59.
    pub minute: u32,
    /// Explicit AM or PM.
    pub meridiem: Option<Meridiem>,
    /// True for unambiguous 24-hour forms ("16:00", "0:30", "09:00").
    pub twenty_four: bool,
}

/// Noon or midnight.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Special {
    /// 12:00.
    Noon,
    /// The midnight at the end of the day.
    Midnight,
}

/// Everything the grammar found, by kind.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct TimeExpr {
    /// "now" was said.
    pub now: bool,
    /// The day, if one was named.
    pub day: Option<DaySpec>,
    /// The clock time, if one was said.
    pub clock: Option<Clock>,
    /// Noon or midnight.
    pub special: Option<Special>,
    /// A part of the day.
    pub part: Option<PartSpec>,
    /// A solar event.
    pub solar: Option<SolarWord>,
    /// A relative offset from now, seconds.
    pub relative_secs: Option<i64>,
    /// The consumed words, in order, joined by spaces ("tomorrow at 4").
    pub text: String,
}

impl TimeExpr {
    /// True if no time words were found.
    pub fn is_empty(&self) -> bool {
        !self.now
            && self.day.is_none()
            && self.clock.is_none()
            && self.special.is_none()
            && self.part.is_none()
            && self.solar.is_none()
            && self.relative_secs.is_none()
    }
}

/// The grammar found two expressions of one kind.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("more than one {0} in the utterance")]
pub struct TimeConflict(pub &'static str);

/// What [`extract`] returns: the expression, which words it consumed, and any conflict.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Extraction {
    /// The time expression (the first of two conflicting parts is kept).
    pub expr: TimeExpr,
    /// `consumed[i]` is true if word `i` belongs to the time expression.
    pub consumed: Vec<bool>,
    /// Set when two expressions of one kind were found ("at 4 at 5").
    pub conflict: Option<TimeConflict>,
}

#[derive(Debug, Clone, Copy)]
enum Comp {
    Now,
    Day(DaySpec),
    Clock(Clock),
    Special(Special),
    Part(PartSpec),
    Solar(SolarWord),
    Relative(i64),
}

struct W<'a> {
    words: &'a [&'a str],
    protected: &'a [bool],
}

impl<'a> W<'a> {
    fn get(&self, i: usize) -> Option<&'a str> {
        (i < self.words.len() && !self.protected.get(i).copied().unwrap_or(false))
            .then(|| self.words[i])
    }
    fn is(&self, i: usize, w: &str) -> bool {
        self.get(i) == Some(w)
    }
    fn any(&self, i: usize, ws: &[&str]) -> bool {
        self.get(i).is_some_and(|x| ws.contains(&x))
    }
}

type Match = (Vec<Comp>, usize);

/// Find the time expression in `words`; `protected[i]` marks words inside an exact place name.
pub fn extract(words: &[&str], protected: &[bool]) -> Extraction {
    let w = W { words, protected };
    let mut consumed = vec![false; words.len()];
    let mut expr = TimeExpr::default();
    let mut conflict = None;
    let mut i = 0;
    while i < words.len() {
        if w.get(i).is_none() {
            i += 1;
            continue;
        }
        let tries: [Option<Match>; 8] = [
            m_now(&w, i),
            m_relative(&w, i),
            m_date(&w, i),
            m_day(&w, i),
            m_solar(&w, i),
            m_special(&w, i),
            m_part(&w, i),
            m_clock(&w, i),
        ];
        let mut best: Option<Match> = None;
        for t in tries.into_iter().flatten() {
            if best.as_ref().is_none_or(|b| t.1 > b.1) {
                best = Some(t);
            }
        }
        match best {
            Some((comps, len)) => {
                for c in comps {
                    if let Err(e) = add(&mut expr, c) {
                        conflict.get_or_insert(e);
                    }
                }
                for flag in consumed.iter_mut().skip(i).take(len) {
                    *flag = true;
                }
                i += len;
            }
            None => i += 1,
        }
    }
    expr.text = words
        .iter()
        .zip(&consumed)
        .filter_map(|(w, &c)| c.then_some(*w))
        .collect::<Vec<_>>()
        .join(" ");
    Extraction {
        expr,
        consumed,
        conflict,
    }
}

fn set_once<T>(slot: &mut Option<T>, value: T, what: &'static str) -> Result<(), TimeConflict> {
    if slot.is_some() {
        return Err(TimeConflict(what));
    }
    *slot = Some(value);
    Ok(())
}

fn add(expr: &mut TimeExpr, c: Comp) -> Result<(), TimeConflict> {
    match c {
        Comp::Now => {
            if expr.now {
                return Err(TimeConflict("\"now\""));
            }
            expr.now = true;
            Ok(())
        }
        Comp::Day(d) => set_once(&mut expr.day, d, "day"),
        Comp::Clock(k) => set_once(&mut expr.clock, k, "clock time"),
        Comp::Special(s) => set_once(&mut expr.special, s, "noon or midnight"),
        Comp::Part(p) => set_once(&mut expr.part, p, "part of the day"),
        Comp::Solar(s) => set_once(&mut expr.solar, s, "solar event"),
        Comp::Relative(r) => set_once(&mut expr.relative_secs, r, "relative time"),
    }
}

// ---------------------------------------------------------------- numbers

fn digits(s: &str) -> Option<u32> {
    (!s.is_empty() && s.len() <= 4 && s.bytes().all(|b| b.is_ascii_digit()))
        .then(|| s.parse().ok())
        .flatten()
}

fn unit_value(w: &str) -> Option<u32> {
    Some(match w {
        "one" => 1,
        "two" => 2,
        "three" => 3,
        "four" => 4,
        "five" => 5,
        "six" => 6,
        "seven" => 7,
        "eight" => 8,
        "nine" => 9,
        _ => return None,
    })
}

fn teen_value(w: &str) -> Option<u32> {
    Some(match w {
        "ten" => 10,
        "eleven" => 11,
        "twelve" => 12,
        "thirteen" => 13,
        "fourteen" => 14,
        "fifteen" => 15,
        "sixteen" => 16,
        "seventeen" => 17,
        "eighteen" => 18,
        "nineteen" => 19,
        _ => return None,
    })
}

fn tens_value(w: &str) -> Option<u32> {
    Some(match w {
        "twenty" => 20,
        "thirty" => 30,
        "forty" => 40,
        "fifty" => 50,
        _ => return None,
    })
}

fn ordinal_unit_value(w: &str) -> Option<u32> {
    Some(match w {
        "first" => 1,
        "second" => 2,
        "third" => 3,
        "fourth" => 4,
        "fifth" => 5,
        "sixth" => 6,
        "seventh" => 7,
        "eighth" => 8,
        "ninth" => 9,
        _ => return None,
    })
}

fn ordinal_other_value(w: &str) -> Option<u32> {
    Some(match w {
        "tenth" => 10,
        "eleventh" => 11,
        "twelfth" => 12,
        "thirteenth" => 13,
        "fourteenth" => 14,
        "fifteenth" => 15,
        "sixteenth" => 16,
        "seventeenth" => 17,
        "eighteenth" => 18,
        "nineteenth" => 19,
        "twentieth" => 20,
        "thirtieth" => 30,
        _ => return None,
    })
}

/// True if `w` is a number word (cardinal or ordinal) the grammar knows.
pub fn is_number_word(w: &str) -> bool {
    unit_value(w).is_some()
        || teen_value(w).is_some()
        || tens_value(w).is_some()
        || ordinal_unit_value(w).is_some()
        || ordinal_other_value(w).is_some()
        || w == "zero"
}

/// A cardinal number in digits or words: (value, words used).
fn number_at(w: &W, i: usize) -> Option<(u32, usize)> {
    let s = w.get(i)?;
    if let Some(v) = digits(s) {
        return Some((v, 1));
    }
    if let Some(t) = tens_value(s) {
        if let Some(u) = w.get(i + 1).and_then(unit_value) {
            return Some((t + u, 2));
        }
        return Some((t, 1));
    }
    if s == "zero" {
        return Some((0, 1));
    }
    teen_value(s).or_else(|| unit_value(s)).map(|v| (v, 1))
}

/// An ordinal in digits ("21st") or words ("twenty first"): (value, words used, had a suffix or word form).
fn ordinal_at(w: &W, i: usize) -> Option<(u32, usize)> {
    let s = w.get(i)?;
    for suffix in ["st", "nd", "rd", "th"] {
        if let Some(n) = s.strip_suffix(suffix)
            && let Some(v) = digits(n)
        {
            return (1..=31).contains(&v).then_some((v, 1));
        }
    }
    if let Some(t) = tens_value(s)
        && let Some(u) = w.get(i + 1).and_then(ordinal_unit_value)
    {
        return Some((t + u, 2));
    }
    ordinal_unit_value(s)
        .or_else(|| ordinal_other_value(s))
        .map(|v| (v, 1))
}

fn month_value(w: &str) -> Option<u32> {
    Some(match w {
        "january" | "jan" => 1,
        "february" | "feb" => 2,
        "march" | "mar" => 3,
        "april" | "apr" => 4,
        "may" => 5,
        "june" | "jun" => 6,
        "july" | "jul" => 7,
        "august" | "aug" => 8,
        "september" | "sep" | "sept" => 9,
        "october" | "oct" => 10,
        "november" | "nov" => 11,
        "december" | "dec" => 12,
        _ => return None,
    })
}

/// A weekday, written out or abbreviated ("sat", "weds", "thurs"). "sun" is not read as Sunday:
/// it is far more often the Sun.
fn weekday_value(w: &str) -> Option<Weekday> {
    Some(match w {
        "monday" | "mon" => Weekday::Mon,
        "tuesday" | "tue" | "tues" => Weekday::Tue,
        "wednesday" | "wed" | "weds" => Weekday::Wed,
        "thursday" | "thu" | "thur" | "thurs" => Weekday::Thu,
        "friday" | "fri" => Weekday::Fri,
        "saturday" | "sat" => Weekday::Sat,
        "sunday" => Weekday::Sun,
        _ => return None,
    })
}

/// Time words the grammar knows, compared by spelling in [`is_time_word`] to catch plurals and
/// misspellings ("sunsets", "sunrize", "evenings"). Short words and "march"/"may" are left out:
/// one letter from them is another word ("moon" is not "noon", "marsh" is not "march").
const SPELLING_NEIGHBOURS: &[&str] = &[
    "today",
    "tomorrow",
    "yesterday",
    "tonight",
    "morning",
    "afternoon",
    "evening",
    "night",
    "midnight",
    "sunrise",
    "sunset",
    "sundown",
    "daybreak",
    "twilight",
    "weekend",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "january",
    "february",
    "april",
    "august",
    "september",
    "october",
    "november",
    "december",
    "minutes",
    "oclock",
];

/// True for words that are about time. Left over after the grammar has run, such a word means the
/// grammar did not understand the time, so the parser hands the utterance to the fallback instead
/// of guessing (PRD §7.3). Besides the grammar's own words this covers holidays and vague times
/// ("thanksgiving", "shortly", "in a bit"), "-ish" times ("fourish", "9ish"), and words within one
/// edit (a transposition counts as one) in five letters of a time word ("sunsets", "sunrize",
/// "evenings", "tonihgt").
///
/// "golden" and "blue" are not on the list: alone they are more likely part of a place name
/// ("Golden Gate"), and "golden hour" / "blue hour" are always consumed as pairs.
pub fn is_time_word(w: &str) -> bool {
    if is_grammar_time_word(w) {
        return true;
    }
    if let Some(stem) = w.strip_suffix("ish")
        && !stem.is_empty()
        && is_grammar_time_word(stem)
    {
        return true;
    }
    w.chars().count() >= 5
        && SPELLING_NEIGHBOURS
            .iter()
            .any(|t| strsim::normalized_damerau_levenshtein(w, t) >= 0.8)
}

fn is_grammar_time_word(w: &str) -> bool {
    const WORDS: &[&str] = &[
        "am",
        "pm",
        "oclock",
        "hour",
        "hours",
        "hr",
        "hrs",
        "minute",
        "minutes",
        "min",
        "mins",
        "day",
        "days",
        "week",
        "weeks",
        "weekend",
        "month",
        "months",
        "year",
        "years",
        "after",
        "before",
        "until",
        "till",
        "since",
        "ago",
        "later",
        "earlier",
        "soon",
        "next",
        "last",
        "past",
        "half",
        "quarter",
        "morning",
        "afternoon",
        "evening",
        "night",
        "tonight",
        "tonite",
        "today",
        "tomorrow",
        "yesterday",
        "noon",
        "midnight",
        "midday",
        "sunrise",
        "sunset",
        "sundown",
        "sunup",
        "dawn",
        "dusk",
        "daybreak",
        "twilight",
        "early",
        "late",
        "o",
        "oh",
        "nite",
        "dawns",
        "dusks",
        "noons",
        "arvo",
        "fortnight",
        "moment",
        "shortly",
        "bit",
        "while",
        "awhile",
        "sometime",
        "someday",
        "anytime",
        "eventually",
        "holiday",
        "holidays",
        "eve",
        "thanksgiving",
        "christmas",
        "xmas",
        "halloween",
        "easter",
        "hanukkah",
        "solstice",
        "equinox",
    ];
    WORDS.contains(&w)
        || is_number_word(w)
        || weekday_value(w).is_some()
        || (w != "may" && month_value(w).is_some())
        || w.bytes().next().is_some_and(|b| b.is_ascii_digit())
}

// ---------------------------------------------------------------- matchers

fn m_now(w: &W, i: usize) -> Option<Match> {
    let len = if w.any(i, &["now", "currently", "live"]) {
        1
    } else if w.is(i, "right") && w.is(i + 1, "now") {
        2
    } else if w.is(i, "at") && w.is(i + 1, "the") && w.is(i + 2, "moment") {
        3
    } else if w.is(i, "at") && w.is(i + 1, "present") {
        2
    } else if w.is(i, "as") && w.is(i + 1, "of") && w.is(i + 2, "now") {
        3
    } else {
        return None;
    };
    Some((vec![Comp::Now], len))
}

fn unit_secs(w: &W, i: usize) -> Option<f64> {
    Some(match w.get(i)? {
        "minute" | "minutes" | "min" | "mins" => 60.0,
        "hour" | "hours" | "hr" | "hrs" => 3600.0,
        "day" | "days" => 86400.0,
        _ => return None,
    })
}

fn quantity_at(w: &W, i: usize) -> Option<(f64, usize)> {
    let s = w.get(i)?;
    if (s == "a" || s == "an") && w.is(i + 1, "couple") {
        return Some((2.0, if w.is(i + 2, "of") { 3 } else { 2 }));
    }
    if s == "couple" {
        return Some((2.0, if w.is(i + 1, "of") { 2 } else { 1 }));
    }
    if s == "a" || s == "an" || s == "one" {
        return Some((1.0, 1));
    }
    if let Some((a, b)) = s.split_once('.')
        && digits(a).is_some()
        && digits(b).is_some()
    {
        return s.parse::<f64>().ok().map(|v| (v, 1));
    }
    number_at(w, i).map(|(v, l)| (f64::from(v), l))
}

/// A duration such as "two hours", "an hour and a half", "half an hour": (seconds, words used).
fn duration_at(w: &W, i: usize) -> Option<(i64, usize)> {
    let hour = |k: usize| unit_secs(w, k) == Some(3600.0);
    if w.is(i, "half") && w.any(i + 1, &["an", "a"]) && hour(i + 2) {
        return Some((1800, 3));
    }
    if w.is(i, "half") && hour(i + 1) {
        return Some((1800, 2));
    }
    if w.is(i, "a") && w.is(i + 1, "half") && hour(i + 2) {
        return Some((1800, 3));
    }
    let (mut q, lq) = quantity_at(w, i)?;
    let mut k = i + lq;
    if w.is(k, "and") && w.is(k + 1, "a") && w.is(k + 2, "half") {
        q += 0.5;
        k += 3;
    }
    let unit = unit_secs(w, k)?;
    k += 1;
    let mut secs = q * unit;
    if w.is(k, "and") && w.is(k + 1, "a") && w.is(k + 2, "half") {
        secs += 0.5 * unit;
        k += 3;
    } else if w.is(k, "and")
        && let Some((q2, l2)) = quantity_at(w, k + 1)
        && let Some(u2) = unit_secs(w, k + 1 + l2)
        && u2 < unit
    {
        secs += q2 * u2;
        k += 2 + l2;
    }
    (secs > 0.0).then_some((secs.round() as i64, k - i))
}

fn m_relative(w: &W, i: usize) -> Option<Match> {
    let mut j = i;
    let has_in = w.is(j, "in");
    if has_in {
        j += 1;
    }
    let (secs, len) = duration_at(w, j)?;
    let mut k = j + len;
    let from_now = w.is(k, "from") && w.is(k + 1, "now");
    if from_now {
        k += 2;
    }
    (has_in || from_now).then_some((vec![Comp::Relative(secs)], k - i))
}

fn year_at(w: &W, i: usize) -> Option<(i32, usize)> {
    let s = w.get(i)?;
    if s.len() == 4
        && let Some(y) = digits(s)
    {
        return (1900..=2100).contains(&y).then_some((y as i32, 1));
    }
    // "twenty twenty six" → 2026
    if s == "twenty"
        && let Some((rest, l)) = number_at(w, i + 1)
        && (10..=99).contains(&rest)
    {
        return Some((2000 + rest as i32, 1 + l));
    }
    None
}

fn slash_date(s: &str) -> Option<DaySpec> {
    let parts: Vec<&str> = s.split('/').collect();
    if !(parts.len() == 2 || parts.len() == 3) {
        return None;
    }
    let month = digits(parts[0])?;
    let day = digits(parts[1])?;
    let year = match parts.get(2) {
        None => None,
        Some(y) if y.len() == 2 => Some(2000 + digits(y)? as i32),
        Some(y) if y.len() == 4 => Some(digits(y)? as i32),
        Some(_) => return None,
    };
    ((1..=12).contains(&month) && (1..=31).contains(&day)).then_some(DaySpec::Date {
        month: Some(month),
        day,
        year,
    })
}

fn m_date(w: &W, i: usize) -> Option<Match> {
    let mut j = i;
    if w.is(j, "on") {
        j += 1;
    }
    let s = w.get(j)?;
    if s.contains('/') {
        return slash_date(s).map(|d| (vec![Comp::Day(d)], j + 1 - i));
    }
    // [the] Month [the] Day [Year]
    if let Some(month) = month_value(s) {
        let mut k = j + 1;
        if w.is(k, "the") {
            k += 1;
        }
        let (day, l) = ordinal_at(w, k).or_else(|| number_at(w, k))?;
        if !(1..=31).contains(&day) {
            return None;
        }
        k += l;
        let year = year_at(w, k).map(|(y, l)| {
            k += l;
            y
        });
        return Some((
            vec![Comp::Day(DaySpec::Date {
                month: Some(month),
                day,
                year,
            })],
            k - i,
        ));
    }
    // [the] Day [of] Month [Year]   or   the Day (ordinal, month implied)
    let mut k = j;
    let the = w.is(k, "the");
    if the {
        k += 1;
    }
    let ordinal = ordinal_at(w, k);
    let (day, l) = ordinal.or_else(|| number_at(w, k))?;
    if !(1..=31).contains(&day) {
        return None;
    }
    let mut k2 = k + l;
    if w.is(k2, "of") {
        k2 += 1;
    }
    if let Some(month) = w.get(k2).and_then(month_value) {
        k2 += 1;
        let year = year_at(w, k2).map(|(y, l)| {
            k2 += l;
            y
        });
        return Some((
            vec![Comp::Day(DaySpec::Date {
                month: Some(month),
                day,
                year,
            })],
            k2 - i,
        ));
    }
    // "the 21st", "21st", "the twenty first": an ordinal on its own names a day of a month.
    let digit_suffix = w
        .get(k)
        .is_some_and(|x| x.bytes().next().is_some_and(|b| b.is_ascii_digit()));
    if ordinal.is_some() && (the || digit_suffix) {
        return Some((
            vec![Comp::Day(DaySpec::Date {
                month: None,
                day,
                year: None,
            })],
            k + l - i,
        ));
    }
    None
}

fn m_day(w: &W, i: usize) -> Option<Match> {
    let mut j = i;
    if w.any(j, &["on", "for"]) {
        j += 1;
    }
    let day = match w.get(j)? {
        "today" => (DaySpec::Today, 1),
        "tomorrow" | "tmrw" | "tmr" | "tomorow" | "tommorow" | "tommorrow" => {
            (DaySpec::Tomorrow, 1)
        }
        "yesterday" => (DaySpec::Yesterday, 1),
        "the" if w.is(j + 1, "day") && w.is(j + 2, "after") && w.is(j + 3, "tomorrow") => {
            (DaySpec::DayAfterTomorrow, 4)
        }
        "day" if w.is(j + 1, "after") && w.is(j + 2, "tomorrow") => (DaySpec::DayAfterTomorrow, 3),
        "next" => (
            DaySpec::Weekday {
                day: w.get(j + 1).and_then(weekday_value)?,
                next: true,
            },
            2,
        ),
        "this" if w.is(j + 1, "coming") => (
            DaySpec::Weekday {
                day: w.get(j + 2).and_then(weekday_value)?,
                next: false,
            },
            3,
        ),
        "this" | "coming" => (
            DaySpec::Weekday {
                day: w.get(j + 1).and_then(weekday_value)?,
                next: false,
            },
            2,
        ),
        other => (
            DaySpec::Weekday {
                day: weekday_value(other)?,
                next: false,
            },
            1,
        ),
    };
    Some((vec![Comp::Day(day.0)], j + day.1 - i))
}

fn m_solar(w: &W, i: usize) -> Option<Match> {
    let mut j = i;
    if w.any(j, &["at", "around", "about", "by", "during", "for"]) {
        j += 1;
    }
    if w.is(j, "the") {
        j += 1;
    }
    let a = w.get(j)?;
    let b = w.get(j + 1);
    let (word, len) = match (a, b) {
        ("sunrise" | "sunup", _) => (SolarWord::Sunrise, 1),
        ("sun", Some("rise" | "up")) => (SolarWord::Sunrise, 2),
        ("sunset" | "sundown", _) => (SolarWord::Sunset, 1),
        ("sun", Some("set" | "down")) => (SolarWord::Sunset, 2),
        ("dawn" | "daybreak", _) => (SolarWord::Dawn, 1),
        ("first", Some("light")) => (SolarWord::Dawn, 2),
        ("dusk" | "nightfall", _) => (SolarWord::Dusk, 1),
        ("golden" | "magic", Some("hour" | "hours")) => (SolarWord::GoldenHour, 2),
        ("blue", Some("hour" | "hours")) => (SolarWord::BlueHour, 2),
        _ => return None,
    };
    Some((vec![Comp::Solar(word)], j + len - i))
}

fn special_word(s: Option<&str>) -> Option<Special> {
    match s? {
        "noon" | "midday" | "noontime" => Some(Special::Noon),
        "midnight" => Some(Special::Midnight),
        _ => None,
    }
}

fn m_special(w: &W, i: usize) -> Option<Match> {
    let mut j = i;
    if w.any(j, &["at", "around", "about", "by"]) {
        j += 1;
    }
    if w.any(j, &["12", "twelve"])
        && let Some(sp) = special_word(w.get(j + 1))
    {
        return Some((vec![Comp::Special(sp)], j + 2 - i));
    }
    let sp = special_word(w.get(j))?;
    let mut end = j + 1;
    if sp == Special::Noon && w.is(end, "time") {
        end += 1;
    }
    Some((vec![Comp::Special(sp)], end - i))
}

fn part_word(s: Option<&str>) -> Option<PartOfDay> {
    match s? {
        "morning" => Some(PartOfDay::Morning),
        "afternoon" => Some(PartOfDay::Afternoon),
        "evening" => Some(PartOfDay::Evening),
        "night" | "nite" | "overnight" => Some(PartOfDay::Night),
        _ => None,
    }
}

fn m_part(w: &W, i: usize) -> Option<Match> {
    let bare = |p| {
        Comp::Part(PartSpec {
            part: p,
            scope: PartScope::Bare,
        })
    };
    if w.is(i, "last") && w.is(i + 1, "night") {
        return Some((
            vec![Comp::Day(DaySpec::Yesterday), bare(PartOfDay::Night)],
            2,
        ));
    }
    if w.any(i, &["tonight", "tonite"]) {
        return Some((
            vec![Comp::Part(PartSpec {
                part: PartOfDay::Night,
                scope: PartScope::Tonight,
            })],
            1,
        ));
    }
    if w.is(i, "this") {
        let p = part_word(w.get(i + 1))?;
        return Some((
            vec![Comp::Part(PartSpec {
                part: p,
                scope: PartScope::This,
            })],
            2,
        ));
    }
    if w.is(i, "in") && w.is(i + 1, "the") {
        return part_word(w.get(i + 2)).map(|p| (vec![bare(p)], 3));
    }
    if w.is(i, "at") && w.is(i + 1, "night") {
        return Some((vec![bare(PartOfDay::Night)], 2));
    }
    part_word(w.get(i)).map(|p| (vec![bare(p)], 1))
}

fn meridiem_at(w: &W, i: usize) -> Option<(Meridiem, usize)> {
    match w.get(i)? {
        "am" => Some((Meridiem::Am, 1)),
        "pm" => Some((Meridiem::Pm, 1)),
        "a" if w.is(i + 1, "m") => Some((Meridiem::Am, 2)),
        "p" if w.is(i + 1, "m") => Some((Meridiem::Pm, 2)),
        _ => None,
    }
}

/// An hour 1–12 in digits or words, for "half past four" and "quarter to five".
fn hour12_at(w: &W, i: usize) -> Option<(u32, usize)> {
    let s = w.get(i)?;
    let v = digits(s)
        .filter(|_| s.len() <= 2)
        .or_else(|| teen_value(s).or_else(|| unit_value(s)))?;
    (1..=12).contains(&v).then_some((v, 1))
}

struct ClockCore {
    hour: u32,
    minute: u32,
    twenty_four: bool,
    /// For "quarter to five": minutes to subtract from `hour`:00.
    back: u32,
    /// True if "o'clock" was already consumed.
    oclock: bool,
    len: usize,
}

fn clock_core(w: &W, j: usize) -> Option<ClockCore> {
    let s = w.get(j)?;
    // half / quarter / N  past|after|to|till|before|of  H
    let lead = if s == "half" {
        Some((30, 1, true))
    } else if s == "quarter" {
        Some((15, 1, false))
    } else if s == "a" && w.is(j + 1, "quarter") {
        Some((15, 2, false))
    } else {
        number_at(w, j)
            .filter(|(n, _)| (1..=29).contains(n))
            .map(|(n, l)| {
                if w.any(j + l, &["minute", "minutes", "mins", "min"]) {
                    (n, l + 1, false)
                } else {
                    (n, l, false)
                }
            })
    };
    if let Some((mins, l1, is_half)) = lead {
        let k = j + l1;
        let with_unit = l1 > 1 && !w.is(j, "a");
        let forward = w.any(k, &["past", "after"]);
        let back = !is_half && w.any(k, &["to", "till", "til", "before", "of"]);
        let round = matches!(mins, 5 | 10 | 15 | 20 | 25) || with_unit;
        if (forward || (back && round))
            && let Some((h, lh)) = hour12_at(w, k + 1)
        {
            return Some(if forward {
                ClockCore {
                    hour: h,
                    minute: mins,
                    twenty_four: false,
                    back: 0,
                    oclock: false,
                    len: l1 + 1 + lh,
                }
            } else {
                ClockCore {
                    hour: h,
                    minute: 0,
                    twenty_four: false,
                    back: mins,
                    oclock: false,
                    len: l1 + 1 + lh,
                }
            });
        }
    }
    // H:MM
    if let Some((hs, ms)) = s.split_once(':') {
        let (h, m) = (digits(hs)?, digits(ms)?);
        if hs.len() > 2 || ms.len() != 2 || h > 23 || m > 59 {
            return None;
        }
        let tf = h == 0 || h >= 13 || (hs.len() == 2 && hs.starts_with('0'));
        return Some(ClockCore {
            hour: h,
            minute: m,
            twenty_four: tf,
            back: 0,
            oclock: false,
            len: 1,
        });
    }
    // digits H [MM]
    if let Some(h) = digits(s) {
        if s.len() > 2 || h > 23 {
            return None;
        }
        let tf = h == 0 || h >= 13 || (s.len() == 2 && s.starts_with('0'));
        if let Some(ms) = w.get(j + 1)
            && ms.len() == 2
            && let Some(m) = digits(ms).filter(|m| *m <= 59)
        {
            return Some(ClockCore {
                hour: h,
                minute: m,
                twenty_four: tf,
                back: 0,
                oclock: false,
                len: 2,
            });
        }
        return Some(ClockCore {
            hour: h,
            minute: 0,
            twenty_four: tf,
            back: 0,
            oclock: false,
            len: 1,
        });
    }
    // word H [oclock | oh N | minutes]
    let h = teen_value(s)
        .or_else(|| unit_value(s))
        .filter(|h| (1..=12).contains(h))?;
    if w.is(j + 1, "oclock") {
        return Some(ClockCore {
            hour: h,
            minute: 0,
            twenty_four: false,
            back: 0,
            oclock: true,
            len: 2,
        });
    }
    if w.any(j + 1, &["oh", "o"])
        && let Some(m) = w.get(j + 2).and_then(unit_value)
    {
        return Some(ClockCore {
            hour: h,
            minute: m,
            twenty_four: false,
            back: 0,
            oclock: false,
            len: 3,
        });
    }
    if let Some((m, l)) = number_at(w, j + 1) {
        let word_form = w
            .get(j + 1)
            .is_some_and(|x| !x.bytes().next().is_some_and(|b| b.is_ascii_digit()));
        if word_form && (10..=59).contains(&m) {
            return Some(ClockCore {
                hour: h,
                minute: m,
                twenty_four: false,
                back: 0,
                oclock: false,
                len: 1 + l,
            });
        }
    }
    Some(ClockCore {
        hour: h,
        minute: 0,
        twenty_four: false,
        back: 0,
        oclock: false,
        len: 1,
    })
}

fn m_clock(w: &W, i: usize) -> Option<Match> {
    let mut j = i;
    if w.any(j, &["at", "around", "about", "by"]) {
        j += 1;
    }
    let core = clock_core(w, j)?;
    let mut k = j + core.len;
    if !core.oclock && core.minute == 0 && core.back == 0 && w.is(k, "oclock") {
        k += 1;
    }
    let meridiem = meridiem_at(w, k).map(|(m, l)| {
        k += l;
        m
    });
    if meridiem.is_some() && (core.twenty_four || !(1..=12).contains(&core.hour)) {
        return None;
    }
    let clock = if core.back > 0 {
        match meridiem {
            Some(m) => {
                let base = (core.hour % 12 + if m == Meridiem::Pm { 12 } else { 0 }) * 60;
                let total = (base + 24 * 60 - core.back) % (24 * 60);
                Clock {
                    hour: total / 60,
                    minute: total % 60,
                    meridiem: None,
                    twenty_four: true,
                }
            }
            None => {
                let hour = if core.hour == 1 { 12 } else { core.hour - 1 };
                Clock {
                    hour,
                    minute: 60 - core.back,
                    meridiem: None,
                    twenty_four: false,
                }
            }
        }
    } else {
        Clock {
            hour: core.hour,
            minute: core.minute,
            meridiem,
            twenty_four: core.twenty_four,
        }
    };
    Some((vec![Comp::Clock(clock)], k - i))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ex(s: &str) -> TimeExpr {
        let toks = crate::normalize::tokenize(s);
        let words: Vec<&str> = toks.iter().map(String::as_str).collect();
        let e = extract(&words, &vec![false; words.len()]);
        assert_eq!(e.conflict, None, "{s:?}");
        e.expr
    }

    fn clock(s: &str) -> Clock {
        ex(s).clock.unwrap_or_else(|| panic!("no clock in {s:?}"))
    }

    fn c(hour: u32, minute: u32, meridiem: Option<Meridiem>, twenty_four: bool) -> Clock {
        Clock {
            hour,
            minute,
            meridiem,
            twenty_four,
        }
    }

    #[test]
    fn clock_forms() {
        use Meridiem::*;
        assert_eq!(clock("at 9"), c(9, 0, None, false));
        assert_eq!(clock("4:30"), c(4, 30, None, false));
        assert_eq!(clock("4 30"), c(4, 30, None, false));
        assert_eq!(clock("four thirty"), c(4, 30, None, false));
        assert_eq!(clock("four forty five"), c(4, 45, None, false));
        assert_eq!(clock("four oh five"), c(4, 5, None, false));
        assert_eq!(clock("four o'clock"), c(4, 0, None, false));
        assert_eq!(clock("half past four"), c(4, 30, None, false));
        assert_eq!(clock("quarter past four"), c(4, 15, None, false));
        assert_eq!(clock("quarter to five"), c(4, 45, None, false));
        assert_eq!(clock("ten past four"), c(4, 10, None, false));
        assert_eq!(clock("twenty to five"), c(4, 40, None, false));
        assert_eq!(clock("quarter to one"), c(12, 45, None, false));
        assert_eq!(clock("4:30 p.m."), c(4, 30, Some(Pm), false));
        assert_eq!(clock("4 pm"), c(4, 0, Some(Pm), false));
        assert_eq!(clock("9am"), c(9, 0, Some(Am), false));
        assert_eq!(clock("16:00"), c(16, 0, None, true));
        assert_eq!(clock("09:30"), c(9, 30, None, true));
        assert_eq!(clock("quarter to 12 pm"), c(11, 45, None, true));
    }

    #[test]
    fn days_and_dates() {
        assert_eq!(ex("tomorrow").day, Some(DaySpec::Tomorrow));
        assert_eq!(
            ex("the day after tomorrow").day,
            Some(DaySpec::DayAfterTomorrow)
        );
        assert_eq!(
            ex("next friday").day,
            Some(DaySpec::Weekday {
                day: Weekday::Fri,
                next: true
            })
        );
        assert_eq!(
            ex("this friday").day,
            Some(DaySpec::Weekday {
                day: Weekday::Fri,
                next: false
            })
        );
        let d = |m, day, y| {
            Some(DaySpec::Date {
                month: m,
                day,
                year: y,
            })
        };
        assert_eq!(ex("november 21").day, d(Some(11), 21, None));
        assert_eq!(ex("21 november").day, d(Some(11), 21, None));
        assert_eq!(ex("nov 21st").day, d(Some(11), 21, None));
        assert_eq!(ex("11/21").day, d(Some(11), 21, None));
        assert_eq!(ex("11/21/2026").day, d(Some(11), 21, Some(2026)));
        assert_eq!(ex("the 21st").day, d(None, 21, None));
        assert_eq!(
            ex("the twenty first of november").day,
            d(Some(11), 21, None)
        );
        assert_eq!(
            ex("november the twenty first twenty twenty six").day,
            d(Some(11), 21, Some(2026))
        );
    }

    #[test]
    fn relative_forms() {
        assert_eq!(ex("in two hours").relative_secs, Some(7200));
        assert_eq!(ex("in 30 minutes").relative_secs, Some(1800));
        assert_eq!(ex("in an hour").relative_secs, Some(3600));
        assert_eq!(ex("in half an hour").relative_secs, Some(1800));
        assert_eq!(ex("in an hour and a half").relative_secs, Some(5400));
        assert_eq!(ex("in two and a half hours").relative_secs, Some(9000));
        assert_eq!(ex("in 1.5 hours").relative_secs, Some(5400));
        assert_eq!(ex("two hours from now").relative_secs, Some(7200));
        assert_eq!(ex("in a couple of hours").relative_secs, Some(7200));
        assert_eq!(ex("in an hour and 15 minutes").relative_secs, Some(4500));
        assert_eq!(
            ex("two hours").relative_secs,
            None,
            "a bare duration is not a time"
        );
    }

    #[test]
    fn solar_and_parts() {
        assert_eq!(ex("at sunset").solar, Some(SolarWord::Sunset));
        assert_eq!(ex("sun down").solar, Some(SolarWord::Sunset));
        assert_eq!(ex("golden hour").solar, Some(SolarWord::GoldenHour));
        let e = ex("morning golden hour");
        assert_eq!(e.part.map(|p| p.part), Some(PartOfDay::Morning));
        assert_eq!(
            ex("tonight").part,
            Some(PartSpec {
                part: PartOfDay::Night,
                scope: PartScope::Tonight
            })
        );
        assert_eq!(
            ex("this morning").part,
            Some(PartSpec {
                part: PartOfDay::Morning,
                scope: PartScope::This
            })
        );
        assert_eq!(
            ex("in the afternoon").part,
            Some(PartSpec {
                part: PartOfDay::Afternoon,
                scope: PartScope::Bare
            })
        );
        let last = ex("last night");
        assert_eq!(last.day, Some(DaySpec::Yesterday));
        assert_eq!(ex("12 noon").special, Some(Special::Noon));
        assert_eq!(ex("at midnight").special, Some(Special::Midnight));
    }

    #[test]
    fn text_is_the_consumed_words() {
        let toks = crate::normalize::tokenize("rainier tomorrow at 4");
        let words: Vec<&str> = toks.iter().map(String::as_str).collect();
        let e = extract(&words, &[false; 4]);
        assert_eq!(e.expr.text, "tomorrow at 4");
        assert_eq!(e.consumed, vec![false, true, true, true]);
    }

    #[test]
    fn protected_words_are_skipped() {
        let words = ["sunset", "point", "at", "sunset"];
        let e = extract(&words, &[true, true, false, false]);
        assert_eq!(e.consumed, vec![false, false, true, true]);
    }

    #[test]
    fn conflicts() {
        let words = ["at", "4", "at", "5"];
        assert_eq!(
            extract(&words, &[false; 4]).conflict,
            Some(TimeConflict("clock time"))
        );
    }

    #[test]
    fn invalid_meridiem_is_not_a_clock() {
        assert_eq!(ex("16:00 pm").clock, None);
        assert_eq!(ex("at 25").clock, None);
    }

    #[test]
    fn weekday_abbreviations() {
        for (w, day) in [
            ("mon", Weekday::Mon),
            ("tues", Weekday::Tue),
            ("weds", Weekday::Wed),
            ("thurs", Weekday::Thu),
            ("on fri", Weekday::Fri),
            ("sat", Weekday::Sat),
        ] {
            assert_eq!(
                ex(w).day,
                Some(DaySpec::Weekday { day, next: false }),
                "{w}"
            );
        }
        assert_eq!(ex("sun").day, None, "\"sun\" is the Sun, not Sunday");
        assert_eq!(
            ex("tomorrow nite").part.map(|p| p.part),
            Some(PartOfDay::Night)
        );
    }

    #[test]
    fn time_words_include_plurals_misspellings_and_holidays() {
        for w in [
            "sunsets",
            "sunrize",
            "sunet",
            "evenings",
            "mornings",
            "dawns",
            "tonihgt",
            "thanksgiving",
            "christmas",
            "eve",
            "shortly",
            "bit",
            "fourish",
            "noonish",
            "9ish",
            "arvo",
            "sat",
        ] {
            assert!(is_time_word(w), "{w} should be a time word");
        }
        // Place words and look-alikes that must stay place words.
        for w in [
            "moon", "marsh", "golden", "blue", "gate", "mount", "rainier", "reyes", "paradise",
            "tahoma", "teton", "smokies", "zion", "seattle", "hood",
        ] {
            assert!(!is_time_word(w), "{w} should not be a time word");
        }
    }

    #[test]
    fn may_needs_a_day() {
        assert_eq!(ex("may i see rainier").day, None);
        assert_eq!(
            ex("may 3").day,
            Some(DaySpec::Date {
                month: Some(5),
                day: 3,
                year: None
            })
        );
    }
}
