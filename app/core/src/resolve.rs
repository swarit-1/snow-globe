//! Time rules: resolve a parsed time expression to an instant at the place (PRD §7.4).
//!
//! All wall times are the **place's** (its IANA zone from the manifest); the asker's machine zone
//! never enters, it only fixes "now" as an instant. The rules, in the order the table lists them:
//!
//! | Said | Rule (`time_source.rule`) | Resolves to |
//! |---|---|---|
//! | "at 9" | `bare-hour` | the next 9:00 or 21:00, whichever comes first; the other is the AM/PM alternative. "at 12" → the next noon or midnight |
//! | "at 4 pm", "tonight at 9", "this morning at 8", "at 4 in the afternoon", "16:00" | `explicit-hour` | that time today; if passed, tomorrow, flagged `rolled_to_tomorrow`. With "tonight"/"at night" a bare hour 1–3 is after midnight, 4–11 is PM and 12 is midnight; 4 and 5 carry an AM/PM chip (the next morning). Asked before 04:00, "tonight" is the night in progress |
//! | "tomorrow at 4", "friday at 9" | `day-plus-bare-hour` | 7–11 → AM, 12 → noon, 1–6 → PM; the other half of that day is the alternative |
//! | "tomorrow at 4 pm" | `day-plus-explicit-hour` | that time on that day |
//! | "now", nothing | `now` / `no-time` | now, flagged `live` when the viewpoint has a live camera |
//! | "sunrise", "sunset", "dawn", "dusk" | event name | the Sun's centre crossing −0.833° (sunrise/sunset) or −6° (dawn/dusk); without a day, the next occurrence |
//! | "golden hour", "blue hour" | `golden-hour`, `blue-hour` | +6° → −4° rendered at +2°, −4° → −6° rendered at −5°; evening unless "morning" is said; the interval goes in `time_source.interval` |
//! | "morning", "afternoon", "evening", "night" | `part-of-day` | 9:00, 15:00, 19:00, 22:00 ([`TimeConfig`]); rolled forward if passed |
//! | "tonight" alone | `tonight` | 21:00, or 1 h after sunset if later; if that has passed, the next whole hour, flagged `rolled_to_next_hour` |
//! | "noon", "midnight" | `noon`, `midnight` | 12:00; midnight is the end of the named (or current) day |
//! | "in two hours" | `relative` | now plus the offset |
//! | "friday", "this friday", "fri" | (with any of the above) | the next Friday including today if the time is still ahead, else a week later (`rolled_to_next_week`) |
//! | "next friday" | | the first Friday at least one day ahead (so on a Thursday it is tomorrow) |
//! | "the 21st" | | the next such day (PRD §7.4), this month or a later one |
//! | "november 21", "11/21" | | the nearest such date, so a date in the last few months is out of range as past rather than as next year's (a deliberate reading of "the next such day"; see the parser table) |
//! | a day alone ("tomorrow") | `day-only` | 12:00 that day. A day that is today ("today", "thursday" on a Thursday, "november 19" on 19 November) is 12:00 if that is ahead, else now (`day-only-now`) |
//!
//! Then: a nonexistent wall time moves forward (`dst_gap`), an ambiguous one takes the earlier
//! offset (`dst_fold`), or the later one when the earlier has passed and the later has not (asked
//! during the repeated hour); a result in the past (unless it is "now") or more than 7 days ahead
//! is out of range; more than 48 h ahead is the GFS horizon (`gfs_horizon`), otherwise HRRR. An
//! AM/PM alternative that is itself out of range is not offered (no chip).

use chrono::{
    DateTime, Datelike, Duration, NaiveDate, NaiveTime, SubsecRound, Timelike, Utc, Weekday,
};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};

use crate::solar::{self, Site, SolarEvent};
use crate::timeparse::{
    Clock, DaySpec, Meridiem, PartOfDay, PartScope, PartSpec, SolarWord, Special, TimeExpr,
};
use crate::tzutil::{DstAdjust, localize_not_before};

/// Configurable hours for the part-of-day rules and the horizon limits (PRD §7.4).
#[derive(Debug, Clone, PartialEq)]
pub struct TimeConfig {
    /// "morning" (default 9:00).
    pub morning: NaiveTime,
    /// "afternoon" (default 15:00).
    pub afternoon: NaiveTime,
    /// "evening" (default 19:00).
    pub evening: NaiveTime,
    /// "night" (default 22:00).
    pub night: NaiveTime,
    /// "tonight" alone (default 21:00).
    pub tonight: NaiveTime,
    /// How long after sunset "tonight" may move to (default 1 h).
    pub tonight_after_sunset: Duration,
    /// Time used for a day said with no time ("tomorrow"; default 12:00).
    pub day_only: NaiveTime,
    /// Last lead time served by HRRR (default 48 h).
    pub hrrr_horizon: Duration,
    /// Last lead time served at all (default 7 days, GFS).
    pub max_horizon: Duration,
}

fn hm(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap_or(NaiveTime::MIN)
}

impl Default for TimeConfig {
    fn default() -> Self {
        TimeConfig {
            morning: hm(9, 0),
            afternoon: hm(15, 0),
            evening: hm(19, 0),
            night: hm(22, 0),
            tonight: hm(21, 0),
            tonight_after_sunset: Duration::hours(1),
            day_only: hm(12, 0),
            hrrr_horizon: Duration::hours(48),
            max_horizon: Duration::days(7),
        }
    }
}

/// What kind of time was said (`time_source.kind`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TimeKind {
    /// Now, or no time said.
    Now,
    /// A clock time, noon or midnight.
    Clock,
    /// A solar event.
    Solar,
    /// A part of the day, or "tonight".
    PartOfDay,
    /// An offset from now.
    Relative,
    /// A day with no time.
    Day,
}

/// The rule that produced the time (`time_source.rule`; see the module table).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum TimeRule {
    /// No time words at all.
    NoTime,
    /// "now".
    Now,
    /// "in two hours".
    Relative,
    /// "at 9".
    BareHour,
    /// "at 4 pm", "tonight at 9", "16:00".
    ExplicitHour,
    /// "tomorrow at 4".
    DayPlusBareHour,
    /// "tomorrow at 4 pm".
    DayPlusExplicitHour,
    /// "noon".
    Noon,
    /// "midnight".
    Midnight,
    /// "sunrise".
    Sunrise,
    /// "sunset".
    Sunset,
    /// "dawn" (civil).
    Dawn,
    /// "dusk" (civil).
    Dusk,
    /// "golden hour".
    GoldenHour,
    /// "blue hour".
    BlueHour,
    /// "morning", "afternoon", "evening", "night".
    PartOfDay,
    /// "tonight" alone.
    Tonight,
    /// "tomorrow", "friday", "november 21" with no time.
    DayOnly,
    /// "today" alone after its noon has passed: now.
    DayOnlyNow,
}

/// Chips the overlay shows (PRD §5.1) and flags for the caption.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Flag {
    /// Show the live camera frame (the "now" rule).
    Live,
    /// The time had passed today, so tomorrow was used (date chip highlighted).
    RolledToTomorrow,
    /// "tonight" had passed, so the next whole hour was used.
    RolledToNextHour,
    /// The named weekday is today but the time had passed, so a week later was used.
    RolledToNextWeek,
    /// The wall time fell in a daylight-saving gap and moved forward.
    DstGap,
    /// The wall time happened twice; the earlier offset was used.
    DstFold,
    /// More than 48 h ahead: served by a pinned GFS run, and the caption says so.
    GfsHorizon,
}

/// Which forecast serves the valid time (PRD §7.4 "Horizon").
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Horizon {
    /// HRRR, up to 48 h ahead.
    Hrrr,
    /// A pinned GFS run, 48 h to 7 days ahead.
    Gfs,
}

/// Solar details of a resolved time.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ResolvedSolar {
    /// The event.
    pub event: SolarEvent,
    /// Altitude of the rendered instant, degrees.
    pub render_altitude_deg: f64,
    /// Golden or blue hour: (start, end, altitude at start, altitude at end).
    pub interval: Option<(DateTime<Tz>, DateTime<Tz>, f64, f64)>,
}

/// A resolved time at the place.
#[derive(Debug, Clone, PartialEq)]
pub struct Resolved {
    /// The valid time, in the place's zone.
    pub instant: DateTime<Tz>,
    /// Kind of time said.
    pub kind: TimeKind,
    /// Rule used.
    pub rule: TimeRule,
    /// Flags, sorted.
    pub flags: Vec<Flag>,
    /// When the hour was ambiguous: (meridiem chosen, the alternative instant).
    pub meridiem: Option<(Meridiem, DateTime<Tz>)>,
    /// Solar details, for solar rules.
    pub solar: Option<ResolvedSolar>,
    /// Forecast horizon.
    pub horizon: Horizon,
}

/// Why a time could not be resolved.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum ResolveError {
    /// Two time expressions that cannot both hold ("now" with "tomorrow", "sunset at 5").
    #[error("conflicting time expressions: {0}")]
    Conflict(&'static str),
    /// A date that does not exist ("february 30").
    #[error("no such date: month {month:?} day {day} year {year:?}")]
    InvalidDate {
        /// Month said, if any.
        month: Option<u32>,
        /// Day said.
        day: u32,
        /// Year said, if any.
        year: Option<i32>,
    },
    /// The Sun does not reach the event's altitude that day.
    #[error("no {event:?} on {date}")]
    NoSolarEvent {
        /// The event.
        event: SolarEvent,
        /// Local date searched.
        date: NaiveDate,
    },
    /// The time is in the past.
    #[error("{requested} is in the past")]
    Past {
        /// The time that was asked for.
        requested: DateTime<Tz>,
    },
    /// The time is more than 7 days ahead.
    #[error("{requested} is beyond the 7-day horizon")]
    BeyondHorizon {
        /// The time that was asked for.
        requested: DateTime<Tz>,
    },
}

#[derive(Debug, Clone, Copy)]
struct Cand {
    t: DateTime<Tz>,
    dst: Option<DstAdjust>,
}

#[derive(Debug, Clone, Copy)]
enum DayMode {
    /// A named day: past is out of range.
    Fixed(NaiveDate),
    /// A weekday that may be today: a passed time moves a week on.
    Weekday(NaiveDate),
    /// No day, or "this morning"/"tonight": a passed time moves to tomorrow.
    Rolling(NaiveDate),
}

struct Ctx<'a> {
    tz: Tz,
    site: &'a Site,
    now: DateTime<Utc>,
    local_now: DateTime<Tz>,
    today: NaiveDate,
    cfg: &'a TimeConfig,
}

fn days(n: i64) -> Duration {
    Duration::days(n)
}

/// Before this local hour, "tonight" still means the night in progress: asked at 00:30,
/// "tonight at 1" is half an hour away, not tomorrow night.
pub const SMALL_HOURS_END: u32 = 4;

/// With "tonight" or "at night", a bare hour in this range is read after midnight (1–3 AM).
const NIGHT_AM_HOURS: std::ops::RangeInclusive<u32> = 1..=3;

/// With "tonight" or "at night", a bare hour in this range is read as PM but is a guess (4 and
/// 5 AM are night too), so the AM reading is offered as a chip.
const NIGHT_GUESS_HOURS: std::ops::RangeInclusive<u32> = 4..=5;

/// Round up to a whole second (so an instant at or after now stays at or after now).
fn ceil_second(t: DateTime<Tz>) -> DateTime<Tz> {
    let truncated = t.trunc_subsecs(0);
    if truncated < t {
        truncated + Duration::seconds(1)
    } else {
        truncated
    }
}

fn first_on_or_after(d: NaiveDate, wd: Weekday) -> NaiveDate {
    let ahead = (7 + wd.num_days_from_monday() - d.weekday().num_days_from_monday()) % 7;
    d + days(i64::from(ahead))
}

fn to24(hour: u32, mer: Meridiem) -> u32 {
    hour % 12 + if mer == Meridiem::Pm { 12 } else { 0 }
}

/// PRD §7.4 "A day plus a bare hour": 7–11 means AM, 12 means noon, 1–6 means PM.
fn day_hour_meridiem(hour: u32) -> Meridiem {
    if (7..=11).contains(&hour) {
        Meridiem::Am
    } else {
        Meridiem::Pm
    }
}

impl Ctx<'_> {
    fn at(&self, d: NaiveDate, time: NaiveTime) -> Cand {
        let (t, dst) = localize_not_before(self.tz, d.and_time(time), self.now);
        Cand { t, dst }
    }

    fn future(&self, t: &DateTime<Tz>) -> bool {
        t.with_timezone(&Utc) >= self.now
    }

    fn day_mode(&self, day: Option<DaySpec>) -> Result<DayMode, ResolveError> {
        let today = self.today;
        Ok(match day {
            None => DayMode::Rolling(today),
            Some(DaySpec::Today) => DayMode::Fixed(today),
            Some(DaySpec::Tomorrow) => DayMode::Fixed(today + days(1)),
            Some(DaySpec::DayAfterTomorrow) => DayMode::Fixed(today + days(2)),
            Some(DaySpec::Yesterday) => DayMode::Fixed(today - days(1)),
            Some(DaySpec::Weekday { day, next: false }) => {
                DayMode::Weekday(first_on_or_after(today, day))
            }
            Some(DaySpec::Weekday { day, next: true }) => {
                DayMode::Fixed(first_on_or_after(today + days(1), day))
            }
            Some(DaySpec::Date { month, day, year }) => {
                DayMode::Fixed(self.resolve_date(month, day, year)?)
            }
        })
    }

    /// The date for "november 21" (no year: the nearest such date) or "the 21st" (no month: the
    /// next such day, PRD §7.4).
    fn resolve_date(
        &self,
        month: Option<u32>,
        day: u32,
        year: Option<i32>,
    ) -> Result<NaiveDate, ResolveError> {
        let today = self.today;
        let mut candidates = Vec::new();
        match (month, year) {
            (Some(m), Some(y)) => candidates.extend(NaiveDate::from_ymd_opt(y, m, day)),
            (Some(m), None) => {
                for y in today.year() - 1..=today.year() + 1 {
                    candidates.extend(NaiveDate::from_ymd_opt(y, m, day));
                }
            }
            (None, _) => {
                // The first month from this one that has the day and where it is not past.
                let next = (0..=12)
                    .filter_map(|delta: i32| {
                        let index = today.year() * 12 + today.month0() as i32 + delta;
                        NaiveDate::from_ymd_opt(
                            index.div_euclid(12),
                            index.rem_euclid(12) as u32 + 1,
                            day,
                        )
                    })
                    .find(|d| *d >= today);
                return next.ok_or(ResolveError::InvalidDate { month, day, year });
            }
        }
        candidates
            .into_iter()
            .min_by_key(|d| ((*d - today).num_days().abs(), *d < today))
            .ok_or(ResolveError::InvalidDate { month, day, year })
    }

    /// Apply a day mode to a per-date function (see [`DayMode`]).
    fn apply<T>(
        &self,
        mode: DayMode,
        f: impl Fn(NaiveDate) -> Result<T, ResolveError>,
        instant: impl Fn(&T) -> DateTime<Tz>,
    ) -> Result<(T, Option<Flag>), ResolveError> {
        match mode {
            DayMode::Fixed(d) => {
                let v = f(d)?;
                if self.future(&instant(&v)) {
                    Ok((v, None))
                } else {
                    Err(ResolveError::Past {
                        requested: instant(&v),
                    })
                }
            }
            DayMode::Weekday(d) => {
                let v = f(d)?;
                if self.future(&instant(&v)) {
                    Ok((v, None))
                } else {
                    Ok((f(d + days(7))?, Some(Flag::RolledToNextWeek)))
                }
            }
            DayMode::Rolling(d) => {
                let v = f(d)?;
                if self.future(&instant(&v)) {
                    Ok((v, None))
                } else {
                    Ok((f(d + days(1))?, Some(Flag::RolledToTomorrow)))
                }
            }
        }
    }

    /// A clock time whose half of the day is known (from AM/PM, 24-hour form or a part of day).
    fn explicit_on(&self, d: NaiveDate, clock: &Clock, part: Option<PartSpec>) -> Cand {
        if clock.twenty_four {
            return self.at(d, hm(clock.hour, clock.minute));
        }
        if let Some(mer) = clock.meridiem {
            return self.at(d, hm(to24(clock.hour, mer), clock.minute));
        }
        let h = clock.hour;
        // The night that follows the evening of `d`: 12 is midnight, 1–3 after midnight, 4–11 PM.
        let night = |d: NaiveDate| match h {
            12 => self.at(d + days(1), hm(0, clock.minute)),
            h if NIGHT_AM_HOURS.contains(&h) => self.at(d + days(1), hm(h, clock.minute)),
            _ => self.at(d, hm(to24(h, Meridiem::Pm), clock.minute)),
        };
        match part.map(|p| (p.part, p.scope)) {
            Some((_, PartScope::Tonight)) | Some((PartOfDay::Night, _)) => night(d),
            Some((PartOfDay::Evening, _)) if h == 12 => night(d),
            Some((PartOfDay::Morning, _)) => self.at(d, hm(to24(h, Meridiem::Am), clock.minute)),
            _ => self.at(d, hm(to24(h, Meridiem::Pm), clock.minute)),
        }
    }

    /// "at 9": the next 9:00 or 21:00, with the other as the alternative.
    fn bare_hour(&self, clock: &Clock) -> (Cand, Meridiem, Cand) {
        let next = |time: NaiveTime| {
            let c = self.at(self.today, time);
            if self.future(&c.t) {
                c
            } else {
                self.at(self.today + days(1), time)
            }
        };
        let am = next(hm(to24(clock.hour, Meridiem::Am), clock.minute));
        let pm = next(hm(to24(clock.hour, Meridiem::Pm), clock.minute));
        if am.t <= pm.t {
            (am, Meridiem::Am, pm)
        } else {
            (pm, Meridiem::Pm, am)
        }
    }

    /// "tomorrow at 4": the 7–11 AM / 12 noon / 1–6 PM rule on the named day.
    fn day_plus_bare_hour(
        &self,
        mode: DayMode,
        clock: &Clock,
    ) -> Result<(Cand, Meridiem, Cand, Option<Flag>), ResolveError> {
        let pref = day_hour_meridiem(clock.hour);
        let on = |d: NaiveDate, m: Meridiem| self.at(d, hm(to24(clock.hour, m), clock.minute));
        let (d, weekday) = match mode {
            DayMode::Fixed(d) => (d, false),
            DayMode::Weekday(d) => (d, true),
            DayMode::Rolling(d) => (d, false),
        };
        let (p, a) = (on(d, pref), on(d, pref.flip()));
        if self.future(&p.t) {
            return Ok((p, pref, a, None));
        }
        if self.future(&a.t) {
            return Ok((a, pref.flip(), p, None));
        }
        if weekday {
            let d7 = d + days(7);
            return Ok((
                on(d7, pref),
                pref,
                on(d7, pref.flip()),
                Some(Flag::RolledToNextWeek),
            ));
        }
        Err(ResolveError::Past { requested: p.t })
    }

    /// "tonight" alone: 21:00, or 1 h after sunset if later; if passed, the next whole hour.
    fn tonight(&self) -> (Cand, Option<Flag>) {
        let base = self.at(self.today, self.cfg.tonight);
        let after_sunset =
            solar::event_on_local_date(self.site, self.tz, self.today, SolarEvent::Sunset).map(
                |e| ceil_second((e.render + self.cfg.tonight_after_sunset).with_timezone(&self.tz)),
            );
        let cand = match after_sunset {
            Some(t) if t > base.t => Cand { t, dst: None },
            _ => base,
        };
        if self.future(&cand.t) {
            return (cand, None);
        }
        let ln = self.local_now;
        let floor = ln
            - Duration::minutes(i64::from(ln.minute()))
            - Duration::seconds(i64::from(ln.second()))
            - Duration::nanoseconds(i64::from(ln.nanosecond()));
        (
            Cand {
                t: floor + Duration::hours(1),
                dst: None,
            },
            Some(Flag::RolledToNextHour),
        )
    }

    fn part_hour(&self, part: PartOfDay) -> NaiveTime {
        match part {
            PartOfDay::Morning => self.cfg.morning,
            PartOfDay::Afternoon => self.cfg.afternoon,
            PartOfDay::Evening => self.cfg.evening,
            PartOfDay::Night => self.cfg.night,
        }
    }
}

fn solar_event(word: SolarWord, part: Option<PartSpec>) -> SolarEvent {
    let morning = part.is_some_and(|p| p.part == PartOfDay::Morning);
    match word {
        SolarWord::Sunrise => SolarEvent::Sunrise,
        SolarWord::Sunset => SolarEvent::Sunset,
        SolarWord::Dawn => SolarEvent::Dawn,
        SolarWord::Dusk => SolarEvent::Dusk,
        SolarWord::GoldenHour if morning => SolarEvent::MorningGoldenHour,
        SolarWord::GoldenHour => SolarEvent::EveningGoldenHour,
        SolarWord::BlueHour if morning => SolarEvent::MorningBlueHour,
        SolarWord::BlueHour => SolarEvent::EveningBlueHour,
    }
}

fn solar_rule(ev: SolarEvent) -> TimeRule {
    match ev {
        SolarEvent::Sunrise => TimeRule::Sunrise,
        SolarEvent::Sunset => TimeRule::Sunset,
        SolarEvent::Dawn => TimeRule::Dawn,
        SolarEvent::Dusk => TimeRule::Dusk,
        SolarEvent::MorningGoldenHour | SolarEvent::EveningGoldenHour => TimeRule::GoldenHour,
        SolarEvent::MorningBlueHour | SolarEvent::EveningBlueHour => TimeRule::BlueHour,
    }
}

/// Inputs to [`resolve`] that describe the place.
#[derive(Debug, Clone, Copy)]
pub struct PlaceClock<'a> {
    /// The place's zone.
    pub tz: Tz,
    /// The camera position.
    pub site: &'a Site,
    /// True if the viewpoint has a live camera (the "now" rule sets `live`).
    pub live_camera: bool,
}

/// Resolve `expr` at a place for the instant `now` (PRD §7.4).
pub fn resolve(
    expr: &TimeExpr,
    place: PlaceClock<'_>,
    now: DateTime<Utc>,
    cfg: &TimeConfig,
) -> Result<Resolved, ResolveError> {
    let local_now = now.with_timezone(&place.tz);
    let ctx = Ctx {
        tz: place.tz,
        site: place.site,
        now,
        local_now,
        today: local_now.date_naive(),
        cfg,
    };

    let others = expr.day.is_some()
        || expr.clock.is_some()
        || expr.special.is_some()
        || expr.part.is_some()
        || expr.solar.is_some();
    if expr.now && (others || expr.relative_secs.is_some()) {
        return Err(ResolveError::Conflict("\"now\" with another time"));
    }
    if expr.relative_secs.is_some() && others {
        return Err(ResolveError::Conflict("a relative time with another time"));
    }
    if expr.solar.is_some() && (expr.clock.is_some() || expr.special.is_some()) {
        return Err(ResolveError::Conflict("a solar event with a clock time"));
    }
    if expr.clock.is_some() && expr.special.is_some() {
        return Err(ResolveError::Conflict("two clock times"));
    }

    let live = |rule: TimeRule| {
        let flags = if place.live_camera {
            vec![Flag::Live]
        } else {
            Vec::new()
        };
        Resolved {
            instant: local_now.trunc_subsecs(0),
            kind: TimeKind::Now,
            rule,
            flags,
            meridiem: None,
            solar: None,
            horizon: Horizon::Hrrr,
        }
    };

    // A "this"/"tonight" scope only applies when no other day was named.
    let part = expr.part.map(|p| match (p.scope, expr.day) {
        (PartScope::This | PartScope::Tonight, Some(d)) if d != DaySpec::Today => PartSpec {
            part: p.part,
            scope: PartScope::Bare,
        },
        _ => p,
    });
    let scoped_today =
        part.is_some_and(|p| p.scope != PartScope::Bare) || expr.day == Some(DaySpec::Today);

    let mut flags: Vec<Flag> = Vec::new();
    let mut meridiem = None;
    let mut solar_out = None;
    let (cand, kind, rule) = if expr.is_empty() {
        return Ok(live(TimeRule::NoTime));
    } else if expr.now {
        return Ok(live(TimeRule::Now));
    } else if let Some(secs) = expr.relative_secs {
        let t = ceil_second((now + Duration::seconds(secs)).with_timezone(&place.tz));
        (
            Cand { t, dst: None },
            TimeKind::Relative,
            TimeRule::Relative,
        )
    } else if let Some(word) = expr.solar {
        let ev = solar_event(word, part);
        let to_local = |e: solar::EventTimes| (ceil_second(e.render.with_timezone(&place.tz)), e);
        let (render, times) = if expr.day.is_none() && !scoped_today {
            let e = solar::next_event(place.site, ev, now).ok_or(ResolveError::NoSolarEvent {
                event: ev,
                date: ctx.today,
            })?;
            to_local(e)
        } else {
            let mode = if expr.day.is_none() {
                DayMode::Rolling(ctx.today)
            } else {
                ctx.day_mode(expr.day)?
            };
            let ((render, e), flag) = ctx.apply(
                mode,
                |d| {
                    solar::event_on_local_date(place.site, place.tz, d, ev)
                        .map(to_local)
                        .ok_or(ResolveError::NoSolarEvent { event: ev, date: d })
                },
                |(t, _)| *t,
            )?;
            flags.extend(flag);
            (render, e)
        };
        let spec = ev.spec();
        solar_out = Some(ResolvedSolar {
            event: ev,
            render_altitude_deg: spec.render_deg,
            interval: times
                .interval
                .zip(spec.interval_deg)
                .map(|((a, b), (da, db))| {
                    (
                        a.with_timezone(&place.tz).round_subsecs(0),
                        b.with_timezone(&place.tz).round_subsecs(0),
                        da,
                        db,
                    )
                }),
        });
        (
            Cand {
                t: render,
                dst: None,
            },
            TimeKind::Solar,
            solar_rule(ev),
        )
    } else if let Some(sp) = expr.special {
        let mode = ctx.day_mode(expr.day)?;
        let (c, flag) = ctx.apply(
            mode,
            |d| {
                Ok(match sp {
                    Special::Noon => ctx.at(d, hm(12, 0)),
                    Special::Midnight => ctx.at(d + days(1), NaiveTime::MIN),
                })
            },
            |c| c.t,
        )?;
        flags.extend(flag);
        (
            c,
            TimeKind::Clock,
            if sp == Special::Noon {
                TimeRule::Noon
            } else {
                TimeRule::Midnight
            },
        )
    } else if let Some(clock) = expr.clock {
        let explicit = clock.twenty_four || clock.meridiem.is_some() || part.is_some();
        match (explicit, expr.day) {
            (true, day) => {
                let night_scope = part
                    .is_some_and(|p| p.scope == PartScope::Tonight || p.part == PartOfDay::Night);
                let bare_night_hour = night_scope && clock.meridiem.is_none() && !clock.twenty_four;
                let mut mode = ctx.day_mode(day)?;
                if bare_night_hour
                    && day.is_none()
                    && NIGHT_AM_HOURS.contains(&clock.hour)
                    && ctx.local_now.hour() < SMALL_HOURS_END
                {
                    // The night in progress began yesterday evening.
                    mode = DayMode::Rolling(ctx.today - days(1));
                }
                let (c, flag) =
                    ctx.apply(mode, |d| Ok(ctx.explicit_on(d, &clock, part)), |c| c.t)?;
                flags.extend(flag);
                if bare_night_hour && NIGHT_GUESS_HOURS.contains(&clock.hour) {
                    // Read as PM; the AM reading of the same night is the next morning.
                    let morning = c.t.date_naive() + days(1);
                    let alt = ctx.at(morning, hm(clock.hour, clock.minute));
                    meridiem = Some((Meridiem::Pm, alt.t));
                }
                let rule = if day.is_some() {
                    TimeRule::DayPlusExplicitHour
                } else {
                    TimeRule::ExplicitHour
                };
                (c, TimeKind::Clock, rule)
            }
            (false, None) => {
                let (c, m, alt) = ctx.bare_hour(&clock);
                meridiem = Some((m, alt.t));
                (c, TimeKind::Clock, TimeRule::BareHour)
            }
            (false, day) => {
                let mode = ctx.day_mode(day)?;
                let (c, m, alt, flag) = ctx.day_plus_bare_hour(mode, &clock)?;
                flags.extend(flag);
                meridiem = Some((m, alt.t));
                (c, TimeKind::Clock, TimeRule::DayPlusBareHour)
            }
        }
    } else if let Some(p) = part {
        if p.scope == PartScope::Tonight && (expr.day.is_none() || expr.day == Some(DaySpec::Today))
        {
            let (c, flag) = ctx.tonight();
            flags.extend(flag);
            (c, TimeKind::PartOfDay, TimeRule::Tonight)
        } else {
            let hour = ctx.part_hour(p.part);
            let mode = ctx.day_mode(expr.day)?;
            let mode = match (mode, expr.day) {
                (DayMode::Fixed(d), Some(DaySpec::Today)) => DayMode::Rolling(d),
                (m, _) => m,
            };
            let (c, flag) = ctx.apply(mode, |d| Ok(ctx.at(d, hour)), |c| c.t)?;
            flags.extend(flag);
            (c, TimeKind::PartOfDay, TimeRule::PartOfDay)
        }
    } else if let Some(day) = expr.day {
        let mode = ctx.day_mode(Some(day))?;
        let names_today = match mode {
            DayMode::Fixed(d) | DayMode::Weekday(d) | DayMode::Rolling(d) => d == ctx.today,
        };
        if names_today {
            // "today", "thursday" on a Thursday, "november 19" on 19 November.
            let noon = ctx.at(ctx.today, cfg.day_only);
            if !ctx.future(&noon.t) {
                return Ok(live(TimeRule::DayOnlyNow));
            }
            (noon, TimeKind::Day, TimeRule::DayOnly)
        } else {
            let (c, flag) = ctx.apply(mode, |d| Ok(ctx.at(d, cfg.day_only)), |c| c.t)?;
            flags.extend(flag);
            (c, TimeKind::Day, TimeRule::DayOnly)
        }
    } else {
        return Ok(live(TimeRule::NoTime));
    };

    match cand.dst {
        Some(DstAdjust::Gap) => flags.push(Flag::DstGap),
        Some(DstAdjust::Fold) => flags.push(Flag::DstFold),
        None => {}
    }
    let instant = cand.t;
    if !ctx.future(&instant) {
        return Err(ResolveError::Past { requested: instant });
    }
    let lead = instant.with_timezone(&Utc) - now;
    if lead > cfg.max_horizon {
        return Err(ResolveError::BeyondHorizon { requested: instant });
    }
    let horizon = if lead > cfg.hrrr_horizon {
        flags.push(Flag::GfsHorizon);
        Horizon::Gfs
    } else {
        Horizon::Hrrr
    };
    // Flipping AM/PM must never land on a time the overlay would refuse.
    let meridiem = meridiem.filter(|(_, alt)| {
        let lead = alt.with_timezone(&Utc) - now;
        lead >= Duration::zero() && lead <= cfg.max_horizon
    });
    flags.sort();
    flags.dedup();
    Ok(Resolved {
        instant,
        kind,
        rule,
        flags,
        meridiem,
        solar: solar_out,
        horizon,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::timeparse::extract;

    const PARADISE: Site = Site {
        lat_deg: 46.78455,
        lon_deg: -121.74065,
        elevation_m: 1643.0,
    };

    fn clock(now: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(now)
            .expect("rfc3339")
            .with_timezone(&Utc)
    }

    fn run(utterance: &str, now: &str) -> Result<Resolved, ResolveError> {
        let toks = crate::normalize::tokenize(utterance);
        let words: Vec<&str> = toks.iter().map(String::as_str).collect();
        let expr = extract(&words, &vec![false; words.len()]).expr;
        let now = clock(now);
        let place = PlaceClock {
            tz: chrono_tz::America::Los_Angeles,
            site: &PARADISE,
            live_camera: true,
        };
        resolve(&expr, place, now, &TimeConfig::default())
    }

    fn at(utterance: &str, now: &str) -> String {
        run(utterance, now)
            .map(|r| r.instant.to_rfc3339())
            .unwrap_or_else(|e| format!("error: {e}"))
    }

    const THU_10AM: &str = "2026-11-19T10:00:00-08:00";

    #[test]
    fn prd_example() {
        let r = run("tomorrow at 4", THU_10AM).expect("ok");
        assert_eq!(r.instant.to_rfc3339(), "2026-11-20T16:00:00-08:00");
        assert_eq!(r.rule, TimeRule::DayPlusBareHour);
        let (m, alt) = r.meridiem.expect("ambiguity");
        assert_eq!(
            (m, alt.to_rfc3339().as_str()),
            (Meridiem::Pm, "2026-11-20T04:00:00-08:00")
        );
    }

    #[test]
    fn bare_hours() {
        assert_eq!(at("at 9", THU_10AM), "2026-11-19T21:00:00-08:00");
        assert_eq!(at("at 11", THU_10AM), "2026-11-19T11:00:00-08:00");
        assert_eq!(at("at 12", THU_10AM), "2026-11-19T12:00:00-08:00");
        assert_eq!(
            at("at 12", "2026-11-19T13:00:00-08:00"),
            "2026-11-20T00:00:00-08:00"
        );
    }

    #[test]
    fn rolls_and_ranges() {
        let r = run("at 8 am", THU_10AM).expect("ok");
        assert_eq!(r.instant.to_rfc3339(), "2026-11-20T08:00:00-08:00");
        assert_eq!(r.flags, vec![Flag::RolledToTomorrow]);
        assert!(matches!(
            run("today at 8 am", THU_10AM),
            Err(ResolveError::Past { .. })
        ));
        assert!(matches!(
            run("november 30", THU_10AM),
            Err(ResolveError::BeyondHorizon { .. })
        ));
        assert_eq!(
            run("saturday at noon", THU_10AM).expect("ok").horizon,
            Horizon::Gfs
        );
    }

    #[test]
    fn dates_resolve_to_the_nearest() {
        assert_eq!(at("the 21st", THU_10AM), "2026-11-21T12:00:00-08:00");
        assert!(matches!(
            run("november 18", THU_10AM),
            Err(ResolveError::Past { .. })
        ));
        assert!(matches!(
            run("february 30", THU_10AM),
            Err(ResolveError::InvalidDate { .. })
        ));
    }

    #[test]
    fn conflicts() {
        assert!(matches!(
            run("now tomorrow", THU_10AM),
            Err(ResolveError::Conflict(_))
        ));
        assert!(matches!(
            run("sunset at 5", THU_10AM),
            Err(ResolveError::Conflict(_))
        ));
    }
}
