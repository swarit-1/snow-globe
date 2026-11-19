//! The PRD §7.6 parser table: utterances with expected queries, run with an injected clock.
//!
//! Expected values come from the PRD §7.4 rules worked by hand (weekdays, DST offsets and lead
//! times were cross-checked with Python's `zoneinfo`); solar expectations come from the
//! independent NOAA implementation in `tests/common`, to ±60 s. Run with `--nocapture` to see the
//! count by category.
//!
//! Default clock: Thursday 19 Nov 2026, 10:00 PST (18:00 UTC). In Chicago, the machine's zone,
//! that is 12:00 CST; the places resolve in their own zones.

mod common;

use std::collections::BTreeMap;

use chrono::{DateTime, Duration, NaiveDate, Utc};
use common::noaa;
use lookout_core::parser::RangeReason;
use lookout_core::places::PlaceTable;
use lookout_core::{LastPlace, ParseContext, ParseError, Parser, Query};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Cat {
    Clock,
    Meridiem,
    DayPlusHour,
    Solar,
    PartOfDay,
    Relative,
    Weekday,
    Date,
    FollowUp,
    Dst,
    CrossZone,
    Horizon,
    AliasMisspellingPhonetic,
    Ambiguity,
    NoMatchTop3,
    GrammarRejects,
}

#[derive(Debug, Clone)]
struct At {
    place: &'static str,
    viewpoint: Option<&'static str>,
    vt: &'static str,
    rule: &'static str,
    flags: Vec<&'static str>,
    alt: Option<&'static str>,
    place_alt: Option<&'static str>,
    stage: Option<&'static str>,
    source: &'static str,
}

#[derive(Debug, Clone)]
struct Sun {
    place: &'static str,
    viewpoint: Option<&'static str>,
    date: &'static str,
    alt_deg: f64,
    rising: bool,
    rule: &'static str,
    flags: Vec<&'static str>,
    interval: Option<(f64, f64)>,
    plus_secs: i64,
    source: &'static str,
}

#[derive(Debug, Clone)]
enum Fail {
    NoPlace,
    NoMatch(Option<&'static str>),
    OutOfRange(RangeReason),
    Unrecognised,
    InvalidDate,
    UnknownViewpoint,
}

#[derive(Debug, Clone)]
enum Exp {
    At(At),
    Sun(Sun),
    Fail(Fail),
}

fn at(place: &'static str, vt: &'static str, rule: &'static str) -> At {
    At {
        place,
        viewpoint: None,
        vt,
        rule,
        flags: vec![],
        alt: None,
        place_alt: None,
        stage: None,
        source: "utterance",
    }
}

impl At {
    fn alt(mut self, a: &'static str) -> Self {
        self.alt = Some(a);
        self
    }
    fn flags(mut self, f: &[&'static str]) -> Self {
        self.flags = f.to_vec();
        self
    }
    fn stage(mut self, s: &'static str) -> Self {
        self.stage = Some(s);
        self
    }
    fn place_alt(mut self, p: &'static str) -> Self {
        self.place_alt = Some(p);
        self
    }
    fn vp(mut self, v: &'static str) -> Self {
        self.viewpoint = Some(v);
        self
    }
    fn context(mut self) -> Self {
        self.source = "context";
        self.stage = Some("context");
        self
    }
    fn e(self) -> Exp {
        Exp::At(self)
    }
}

fn sun(
    place: &'static str,
    date: &'static str,
    alt_deg: f64,
    rising: bool,
    rule: &'static str,
) -> Sun {
    Sun {
        place,
        viewpoint: None,
        date,
        alt_deg,
        rising,
        rule,
        flags: vec![],
        interval: None,
        plus_secs: 0,
        source: "utterance",
    }
}

impl Sun {
    fn interval(mut self, start_deg: f64, end_deg: f64) -> Self {
        self.interval = Some((start_deg, end_deg));
        self
    }
    fn flags(mut self, f: &[&'static str]) -> Self {
        self.flags = f.to_vec();
        self
    }
    fn plus(mut self, secs: i64) -> Self {
        self.plus_secs = secs;
        self
    }
    fn vp(mut self, v: &'static str) -> Self {
        self.viewpoint = Some(v);
        self
    }
    fn context(mut self) -> Self {
        self.source = "context";
        self
    }
    fn e(self) -> Exp {
        Exp::Sun(self)
    }
}

struct Case {
    cat: Cat,
    utt: &'static str,
    now: &'static str,
    ctx: Option<&'static str>,
    exp: Exp,
}

const T10: &str = "2026-11-19T10:00:00-08:00";
const T13: &str = "2026-11-19T13:00:00-08:00";
const T16: &str = "2026-11-19T16:00:00-08:00";
const T2230: &str = "2026-11-19T22:30:00-08:00";
const RISE: bool = true;
const SET: bool = false;
const SS: f64 = -0.833;
const GFS: &str = "gfs_horizon";
const ROLLED: &str = "rolled_to_tomorrow";

fn cases() -> Vec<Case> {
    use Cat::*;
    let c = |cat, utt, now, exp| Case {
        cat,
        utt,
        now,
        ctx: None,
        exp,
    };
    let cc = |cat, utt, now, ctx, exp| Case {
        cat,
        utt,
        now,
        ctx: Some(ctx),
        exp,
    };
    let fail = Exp::Fail;
    vec![
        // ------------------------------------------------------------ clock (bare hours)
        c(
            Clock,
            "rainier at 9",
            T10,
            at("rainier", "2026-11-19T21:00:00-08:00", "bare-hour")
                .alt("2026-11-20T09:00:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at 11",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "bare-hour")
                .alt("2026-11-19T23:00:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at 4:30",
            T10,
            at("rainier", "2026-11-19T16:30:00-08:00", "bare-hour")
                .alt("2026-11-20T04:30:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at four thirty",
            T10,
            at("rainier", "2026-11-19T16:30:00-08:00", "bare-hour")
                .alt("2026-11-20T04:30:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at four o'clock",
            T10,
            at("rainier", "2026-11-19T16:00:00-08:00", "bare-hour")
                .alt("2026-11-20T04:00:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at half past four",
            T10,
            at("rainier", "2026-11-19T16:30:00-08:00", "bare-hour")
                .alt("2026-11-20T04:30:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at quarter past four",
            T10,
            at("rainier", "2026-11-19T16:15:00-08:00", "bare-hour")
                .alt("2026-11-20T04:15:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at quarter to five",
            T10,
            at("rainier", "2026-11-19T16:45:00-08:00", "bare-hour")
                .alt("2026-11-20T04:45:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at ten past four",
            T10,
            at("rainier", "2026-11-19T16:10:00-08:00", "bare-hour")
                .alt("2026-11-20T04:10:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at twenty to five",
            T10,
            at("rainier", "2026-11-19T16:40:00-08:00", "bare-hour")
                .alt("2026-11-20T04:40:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at four oh five",
            T10,
            at("rainier", "2026-11-19T16:05:00-08:00", "bare-hour")
                .alt("2026-11-20T04:05:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at 4 30",
            T10,
            at("rainier", "2026-11-19T16:30:00-08:00", "bare-hour")
                .alt("2026-11-20T04:30:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at 16:00",
            T10,
            at("rainier", "2026-11-19T16:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Clock,
            "rainier at 21:30",
            T10,
            at("rainier", "2026-11-19T21:30:00-08:00", "explicit-hour").e(),
        ),
        c(
            Clock,
            "rainier at 09:15",
            T10,
            at("rainier", "2026-11-20T09:15:00-08:00", "explicit-hour")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            Clock,
            "rainier at 1",
            T10,
            at("rainier", "2026-11-19T13:00:00-08:00", "bare-hour")
                .alt("2026-11-20T01:00:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at 10",
            T10,
            at("rainier", "2026-11-19T10:00:00-08:00", "bare-hour")
                .alt("2026-11-19T22:00:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at 12",
            T10,
            at("rainier", "2026-11-19T12:00:00-08:00", "bare-hour")
                .alt("2026-11-20T00:00:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at 12:15",
            T10,
            at("rainier", "2026-11-19T12:15:00-08:00", "bare-hour")
                .alt("2026-11-20T00:15:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "rainier at 11:59",
            T10,
            at("rainier", "2026-11-19T11:59:00-08:00", "bare-hour")
                .alt("2026-11-19T23:59:00-08:00")
                .e(),
        ),
        c(
            Clock,
            "grand canyon at 9",
            T10,
            at("grand-canyon", "2026-11-19T21:00:00-07:00", "bare-hour")
                .alt("2026-11-20T09:00:00-07:00")
                .e(),
        ),
        c(
            Clock,
            "acadia at 9",
            T10,
            at("acadia", "2026-11-19T21:00:00-05:00", "bare-hour")
                .alt("2026-11-20T09:00:00-05:00")
                .e(),
        ),
        // ------------------------------------------------------------ meridiem
        c(
            Meridiem,
            "rainier at 4 pm",
            T10,
            at("rainier", "2026-11-19T16:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier at 4pm",
            T10,
            at("rainier", "2026-11-19T16:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier at 4:30 p.m.",
            T10,
            at("rainier", "2026-11-19T16:30:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier at 4 p m",
            T10,
            at("rainier", "2026-11-19T16:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier at 9 am",
            T10,
            at("rainier", "2026-11-20T09:00:00-08:00", "explicit-hour")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            Meridiem,
            "rainier at 11 a.m.",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier tonight at 9",
            T10,
            at("rainier", "2026-11-19T21:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier at 9 tonight",
            T10,
            at("rainier", "2026-11-19T21:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier this morning at 8",
            T10,
            at("rainier", "2026-11-20T08:00:00-08:00", "explicit-hour")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            Meridiem,
            "rainier this morning at 11",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier at 4 in the afternoon",
            T10,
            at("rainier", "2026-11-19T16:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier at 7 in the morning",
            T10,
            at("rainier", "2026-11-20T07:00:00-08:00", "explicit-hour")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            Meridiem,
            "rainier at 9 at night",
            T10,
            at("rainier", "2026-11-19T21:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier at 11 pm",
            T10,
            at("rainier", "2026-11-19T23:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier at 12 am",
            T10,
            at("rainier", "2026-11-20T00:00:00-08:00", "explicit-hour")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            Meridiem,
            "rainier at 12 pm",
            T10,
            at("rainier", "2026-11-19T12:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier tonight at 1",
            T10,
            at("rainier", "2026-11-20T01:00:00-08:00", "explicit-hour").e(),
        ),
        // "tonight" with a bare hour: 1–3 after midnight, 4–11 PM, 12 midnight. 4 and 5 are a
        // guess, so they carry an AM/PM chip whose alternative is the next morning.
        c(
            Meridiem,
            "rainier tonight at 5",
            T10,
            at("rainier", "2026-11-19T17:00:00-08:00", "explicit-hour")
                .alt("2026-11-20T05:00:00-08:00")
                .e(),
        ),
        c(
            Meridiem,
            "rainier tonight at 4",
            T10,
            at("rainier", "2026-11-19T16:00:00-08:00", "explicit-hour")
                .alt("2026-11-20T04:00:00-08:00")
                .e(),
        ),
        c(
            Meridiem,
            "rainier tonight at 3",
            T10,
            at("rainier", "2026-11-20T03:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier at 5 at night",
            T10,
            at("rainier", "2026-11-19T17:00:00-08:00", "explicit-hour")
                .alt("2026-11-20T05:00:00-08:00")
                .e(),
        ),
        // Asked in the small hours, "tonight" is the night in progress.
        c(
            Meridiem,
            "rainier tonight at 1",
            "2026-11-20T00:30:00-08:00",
            at("rainier", "2026-11-20T01:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier tonight at 2",
            "2026-11-20T02:30:00-08:00",
            at("rainier", "2026-11-21T02:00:00-08:00", "explicit-hour")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            Meridiem,
            "rainier tonight at 11",
            "2026-11-20T00:30:00-08:00",
            at("rainier", "2026-11-20T23:00:00-08:00", "explicit-hour").e(),
        ),
        c(
            Meridiem,
            "rainier at noon",
            T10,
            at("rainier", "2026-11-19T12:00:00-08:00", "noon").e(),
        ),
        c(
            Meridiem,
            "rainier at midnight",
            T10,
            at("rainier", "2026-11-20T00:00:00-08:00", "midnight").e(),
        ),
        c(
            Meridiem,
            "rainier at 12 noon",
            T10,
            at("rainier", "2026-11-19T12:00:00-08:00", "noon").e(),
        ),
        c(
            Meridiem,
            "rainier tonight at 9",
            T2230,
            at("rainier", "2026-11-20T21:00:00-08:00", "explicit-hour")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            Meridiem,
            "rainier at noon",
            T13,
            at("rainier", "2026-11-20T12:00:00-08:00", "noon")
                .flags(&[ROLLED])
                .e(),
        ),
        // ------------------------------------------------------------ day plus hour
        c(
            DayPlusHour,
            "Is Rainier out tomorrow at 4?",
            T10,
            at("rainier", "2026-11-20T16:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T04:00:00-08:00")
                .e(),
        ),
        c(
            DayPlusHour,
            "rainier tomorrow at 9",
            T10,
            at("rainier", "2026-11-20T09:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T21:00:00-08:00")
                .e(),
        ),
        c(
            DayPlusHour,
            "rainier tomorrow at 12",
            T10,
            at("rainier", "2026-11-20T12:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T00:00:00-08:00")
                .e(),
        ),
        c(
            DayPlusHour,
            "rainier tomorrow at 7",
            T10,
            at("rainier", "2026-11-20T07:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T19:00:00-08:00")
                .e(),
        ),
        c(
            DayPlusHour,
            "rainier tomorrow at 6",
            T10,
            at("rainier", "2026-11-20T18:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T06:00:00-08:00")
                .e(),
        ),
        c(
            DayPlusHour,
            "rainier tomorrow at 11",
            T10,
            at("rainier", "2026-11-20T11:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T23:00:00-08:00")
                .e(),
        ),
        c(
            DayPlusHour,
            "rainier tomorrow at 1",
            T10,
            at("rainier", "2026-11-20T13:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T01:00:00-08:00")
                .e(),
        ),
        c(
            DayPlusHour,
            "rainier friday at 9",
            T10,
            at("rainier", "2026-11-20T09:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T21:00:00-08:00")
                .e(),
        ),
        c(
            DayPlusHour,
            "point reyes saturday at 3",
            T10,
            at(
                "point-reyes",
                "2026-11-21T15:00:00-08:00",
                "day-plus-bare-hour",
            )
            .alt("2026-11-21T03:00:00-08:00")
            .flags(&[GFS])
            .e(),
        ),
        c(
            DayPlusHour,
            "rainier the day after tomorrow at 10",
            T10,
            at("rainier", "2026-11-21T10:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-21T22:00:00-08:00")
                .e(),
        ),
        c(
            DayPlusHour,
            "rainier tomorrow at 4 pm",
            T10,
            at(
                "rainier",
                "2026-11-20T16:00:00-08:00",
                "day-plus-explicit-hour",
            )
            .e(),
        ),
        c(
            DayPlusHour,
            "rainier tomorrow at 16:00",
            T10,
            at(
                "rainier",
                "2026-11-20T16:00:00-08:00",
                "day-plus-explicit-hour",
            )
            .e(),
        ),
        c(
            DayPlusHour,
            "rainier tomorrow at 4:30",
            T10,
            at("rainier", "2026-11-20T16:30:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T04:30:00-08:00")
                .e(),
        ),
        c(
            DayPlusHour,
            "rainier today at 2",
            T10,
            // 02:00 today has passed, so no AM/PM chip is offered.
            at("rainier", "2026-11-19T14:00:00-08:00", "day-plus-bare-hour").e(),
        ),
        c(
            DayPlusHour,
            "rainier today at 9",
            T10,
            at("rainier", "2026-11-19T21:00:00-08:00", "day-plus-bare-hour").e(),
        ),
        c(
            DayPlusHour,
            "rainier tomorrow morning at 7",
            T10,
            at(
                "rainier",
                "2026-11-20T07:00:00-08:00",
                "day-plus-explicit-hour",
            )
            .e(),
        ),
        c(
            DayPlusHour,
            "rainier thursday at 9",
            T10,
            at("rainier", "2026-11-19T21:00:00-08:00", "day-plus-bare-hour").e(),
        ),
        c(
            DayPlusHour,
            "rainier thursday at 8 am",
            T10,
            at(
                "rainier",
                "2026-11-26T08:00:00-08:00",
                "day-plus-explicit-hour",
            )
            .flags(&["rolled_to_next_week", GFS])
            .e(),
        ),
        // ------------------------------------------------------------ solar (checked against NOAA, ±60 s)
        c(
            Solar,
            "point reyes at sunset",
            T10,
            sun("point-reyes", "2026-11-19", SS, SET, "sunset").e(),
        ),
        c(
            Solar,
            "rainier at sunrise",
            T10,
            sun("rainier", "2026-11-20", SS, RISE, "sunrise").e(),
        ),
        c(
            Solar,
            "Point Reyes at sunset Friday",
            T10,
            sun("point-reyes", "2026-11-20", SS, SET, "sunset").e(),
        ),
        c(
            Solar,
            "rainier sunrise tomorrow",
            T10,
            sun("rainier", "2026-11-20", SS, RISE, "sunrise").e(),
        ),
        c(
            Solar,
            "grand canyon golden hour",
            T10,
            sun("grand-canyon", "2026-11-19", 2.0, SET, "golden-hour")
                .interval(6.0, -4.0)
                .e(),
        ),
        c(
            Solar,
            "grand canyon morning golden hour",
            T10,
            sun("grand-canyon", "2026-11-20", 2.0, RISE, "golden-hour")
                .interval(-4.0, 6.0)
                .e(),
        ),
        c(
            Solar,
            "rainier golden hour tomorrow morning",
            T10,
            sun("rainier", "2026-11-20", 2.0, RISE, "golden-hour")
                .interval(-4.0, 6.0)
                .e(),
        ),
        c(
            Solar,
            "point reyes blue hour",
            T10,
            sun("point-reyes", "2026-11-19", -5.0, SET, "blue-hour")
                .interval(-4.0, -6.0)
                .e(),
        ),
        c(
            Solar,
            "point reyes blue hour in the morning",
            T10,
            sun("point-reyes", "2026-11-20", -5.0, RISE, "blue-hour")
                .interval(-6.0, -4.0)
                .e(),
        ),
        c(
            Solar,
            "rainier at dawn",
            T10,
            sun("rainier", "2026-11-20", -6.0, RISE, "dawn").e(),
        ),
        c(
            Solar,
            "rainier at dusk",
            T10,
            sun("rainier", "2026-11-19", -6.0, SET, "dusk").e(),
        ),
        c(
            Solar,
            "rainier sundown",
            T10,
            sun("rainier", "2026-11-19", SS, SET, "sunset").e(),
        ),
        c(
            Solar,
            "rainier sun up",
            T10,
            sun("rainier", "2026-11-20", SS, RISE, "sunrise").e(),
        ),
        c(
            Solar,
            "acadia sunset on saturday",
            T10,
            sun("acadia", "2026-11-21", SS, SET, "sunset")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Solar,
            "rainier sunset today",
            T10,
            sun("rainier", "2026-11-19", SS, SET, "sunset").e(),
        ),
        c(
            Solar,
            "rainier sunset tonight",
            T2230,
            sun("rainier", "2026-11-20", SS, SET, "sunset")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            Solar,
            "big bend golden hour this morning",
            T10,
            sun("big-bend", "2026-11-20", 2.0, RISE, "golden-hour")
                .interval(-4.0, 6.0)
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            Solar,
            "yosemite sunrise on the 21st",
            T10,
            sun("yosemite", "2026-11-21", SS, RISE, "sunrise").e(),
        ),
        // ------------------------------------------------------------ part of day
        c(
            PartOfDay,
            "rainier this morning",
            T10,
            at("rainier", "2026-11-20T09:00:00-08:00", "part-of-day")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            PartOfDay,
            "rainier morning",
            T10,
            at("rainier", "2026-11-20T09:00:00-08:00", "part-of-day")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            PartOfDay,
            "rainier in the morning",
            T10,
            at("rainier", "2026-11-20T09:00:00-08:00", "part-of-day")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            PartOfDay,
            "rainier this afternoon",
            T10,
            at("rainier", "2026-11-19T15:00:00-08:00", "part-of-day").e(),
        ),
        c(
            PartOfDay,
            "rainier afternoon",
            T10,
            at("rainier", "2026-11-19T15:00:00-08:00", "part-of-day").e(),
        ),
        c(
            PartOfDay,
            "rainier this evening",
            T10,
            at("rainier", "2026-11-19T19:00:00-08:00", "part-of-day").e(),
        ),
        c(
            PartOfDay,
            "rainier evening",
            T10,
            at("rainier", "2026-11-19T19:00:00-08:00", "part-of-day").e(),
        ),
        c(
            PartOfDay,
            "rainier at night",
            T10,
            at("rainier", "2026-11-19T22:00:00-08:00", "part-of-day").e(),
        ),
        c(
            PartOfDay,
            "rainier tonight",
            T10,
            at("rainier", "2026-11-19T21:00:00-08:00", "tonight").e(),
        ),
        c(
            PartOfDay,
            "rainier tonight",
            T2230,
            at("rainier", "2026-11-19T23:00:00-08:00", "tonight")
                .flags(&["rolled_to_next_hour"])
                .e(),
        ),
        // June: sunset at Paradise is after 21:00, so "tonight" is 1 h after sunset.
        c(
            PartOfDay,
            "rainier tonight",
            "2027-06-21T12:00:00-07:00",
            sun("rainier", "2027-06-21", SS, SET, "tonight")
                .plus(3600)
                .e(),
        ),
        c(
            PartOfDay,
            "rainier tomorrow nite",
            T10,
            at("rainier", "2026-11-20T22:00:00-08:00", "part-of-day").e(),
        ),
        c(
            PartOfDay,
            "rainier tomorrow morning",
            T10,
            at("rainier", "2026-11-20T09:00:00-08:00", "part-of-day").e(),
        ),
        c(
            PartOfDay,
            "rainier friday evening",
            T10,
            at("rainier", "2026-11-20T19:00:00-08:00", "part-of-day").e(),
        ),
        c(
            PartOfDay,
            "rainier tomorrow night",
            T10,
            at("rainier", "2026-11-20T22:00:00-08:00", "part-of-day").e(),
        ),
        c(
            PartOfDay,
            "rainier this afternoon",
            T16,
            at("rainier", "2026-11-20T15:00:00-08:00", "part-of-day")
                .flags(&[ROLLED])
                .e(),
        ),
        c(
            PartOfDay,
            "smokies this evening",
            T10,
            at("smokies", "2026-11-19T19:00:00-05:00", "part-of-day").e(),
        ),
        // ------------------------------------------------------------ relative
        c(
            Relative,
            "rainier in two hours",
            T10,
            at("rainier", "2026-11-19T12:00:00-08:00", "relative").e(),
        ),
        c(
            Relative,
            "rainier in 30 minutes",
            T10,
            at("rainier", "2026-11-19T10:30:00-08:00", "relative").e(),
        ),
        c(
            Relative,
            "rainier in one hour",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "relative").e(),
        ),
        c(
            Relative,
            "rainier in half an hour",
            T10,
            at("rainier", "2026-11-19T10:30:00-08:00", "relative").e(),
        ),
        c(
            Relative,
            "rainier in an hour and a half",
            T10,
            at("rainier", "2026-11-19T11:30:00-08:00", "relative").e(),
        ),
        c(
            Relative,
            "rainier in 90 minutes",
            T10,
            at("rainier", "2026-11-19T11:30:00-08:00", "relative").e(),
        ),
        c(
            Relative,
            "rainier two hours from now",
            T10,
            at("rainier", "2026-11-19T12:00:00-08:00", "relative").e(),
        ),
        c(
            Relative,
            "rainier in a couple of hours",
            T10,
            at("rainier", "2026-11-19T12:00:00-08:00", "relative").e(),
        ),
        c(
            Relative,
            "rainier in 3 days",
            T10,
            at("rainier", "2026-11-22T10:00:00-08:00", "relative")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Relative,
            "rainier in 1.5 hours",
            T10,
            at("rainier", "2026-11-19T11:30:00-08:00", "relative").e(),
        ),
        c(
            Relative,
            "point reyes in 45 minutes",
            T10,
            at("point-reyes", "2026-11-19T10:45:00-08:00", "relative").e(),
        ),
        c(
            Relative,
            "grand canyon in two hours",
            T10,
            at("grand-canyon", "2026-11-19T13:00:00-07:00", "relative").e(),
        ),
        // ------------------------------------------------------------ weekday (today is Thursday)
        c(
            Weekday,
            "rainier friday",
            T10,
            at("rainier", "2026-11-20T12:00:00-08:00", "day-only").e(),
        ),
        c(
            Weekday,
            "rainier this friday",
            T10,
            at("rainier", "2026-11-20T12:00:00-08:00", "day-only").e(),
        ),
        // "next friday" = the first Friday at least one day ahead, so on a Thursday it is tomorrow.
        c(
            Weekday,
            "rainier next friday",
            T10,
            at("rainier", "2026-11-20T12:00:00-08:00", "day-only").e(),
        ),
        c(
            Weekday,
            "rainier thursday",
            T10,
            at("rainier", "2026-11-19T12:00:00-08:00", "day-only").e(),
        ),
        c(
            Weekday,
            "rainier next thursday",
            T10,
            fail(Fail::OutOfRange(RangeReason::BeyondHorizon)),
        ),
        c(
            Weekday,
            "rainier monday",
            T10,
            at("rainier", "2026-11-23T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Weekday,
            "rainier on sunday at noon",
            T10,
            at("rainier", "2026-11-22T12:00:00-08:00", "noon")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Weekday,
            "rainier saturday morning",
            T10,
            at("rainier", "2026-11-21T09:00:00-08:00", "part-of-day").e(),
        ),
        c(
            Weekday,
            "rainier wednesday",
            T10,
            at("rainier", "2026-11-25T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Weekday,
            "rainier next wednesday",
            T10,
            at("rainier", "2026-11-25T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        // A day said alone that is today behaves like "today": noon if it is still ahead, else
        // now. Asked at 13:00 on Thursday, "thursday", "november 19" and "the 19th" agree.
        c(
            Weekday,
            "rainier thursday",
            T13,
            at("rainier", "2026-11-19T13:00:00-08:00", "day-only-now")
                .flags(&["live"])
                .e(),
        ),
        c(
            Weekday,
            "rainier sat at 9",
            T10,
            at("rainier", "2026-11-21T09:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-21T21:00:00-08:00")
                .e(),
        ),
        c(
            Weekday,
            "rainier on fri",
            T10,
            at("rainier", "2026-11-20T12:00:00-08:00", "day-only").e(),
        ),
        c(
            Weekday,
            "rainier tues",
            T10,
            at("rainier", "2026-11-24T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Weekday,
            "rainier weds",
            T10,
            at("rainier", "2026-11-25T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        // 4 → PM by the day-plus-hour rule; 04:00 today has passed, so no AM/PM chip is offered.
        c(
            Weekday,
            "rainier thurs at 4",
            T10,
            at("rainier", "2026-11-19T16:00:00-08:00", "day-plus-bare-hour").e(),
        ),
        c(
            Weekday,
            "rainier on friday at 4 pm",
            T10,
            at(
                "rainier",
                "2026-11-20T16:00:00-08:00",
                "day-plus-explicit-hour",
            )
            .e(),
        ),
        c(
            Weekday,
            "rainier this coming saturday",
            T10,
            at("rainier", "2026-11-21T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        // ------------------------------------------------------------ dates
        c(
            Date,
            "rainier november 21",
            T10,
            at("rainier", "2026-11-21T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Date,
            "rainier 21 november",
            T10,
            at("rainier", "2026-11-21T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Date,
            "rainier nov 21",
            T10,
            at("rainier", "2026-11-21T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Date,
            "rainier 11/21",
            T10,
            at("rainier", "2026-11-21T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Date,
            "rainier the 21st",
            T10,
            at("rainier", "2026-11-21T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Date,
            "rainier november 21st at 9",
            T10,
            at("rainier", "2026-11-21T09:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-21T21:00:00-08:00")
                .e(),
        ),
        c(
            Date,
            "rainier on the 21st of november at 4 pm",
            T10,
            at(
                "rainier",
                "2026-11-21T16:00:00-08:00",
                "day-plus-explicit-hour",
            )
            .flags(&[GFS])
            .e(),
        ),
        c(
            Date,
            "rainier november twenty first",
            T10,
            at("rainier", "2026-11-21T12:00:00-08:00", "day-only")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Date,
            "rainier 11/21/2026 at 10",
            T10,
            at("rainier", "2026-11-21T10:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-21T22:00:00-08:00")
                .e(),
        ),
        c(
            Date,
            "rainier november 20",
            T10,
            at("rainier", "2026-11-20T12:00:00-08:00", "day-only").e(),
        ),
        c(
            Date,
            "rainier the 20th at noon",
            T10,
            at("rainier", "2026-11-20T12:00:00-08:00", "noon").e(),
        ),
        c(
            Date,
            "rainier november 18",
            T10,
            fail(Fail::OutOfRange(RangeReason::Past)),
        ),
        c(
            Date,
            "rainier november 30",
            T10,
            fail(Fail::OutOfRange(RangeReason::BeyondHorizon)),
        ),
        // "the Nth" is the next such day (PRD §7.4): 18 Dec and 31 Dec, both beyond 7 days.
        c(
            Date,
            "rainier the 18th",
            T10,
            fail(Fail::OutOfRange(RangeReason::BeyondHorizon)),
        ),
        c(
            Date,
            "rainier the 31st",
            T10,
            fail(Fail::OutOfRange(RangeReason::BeyondHorizon)),
        ),
        c(
            Date,
            "rainier november 19",
            T13,
            at("rainier", "2026-11-19T13:00:00-08:00", "day-only-now")
                .flags(&["live"])
                .e(),
        ),
        c(
            Date,
            "rainier the 19th",
            T13,
            at("rainier", "2026-11-19T13:00:00-08:00", "day-only-now")
                .flags(&["live"])
                .e(),
        ),
        c(Date, "rainier february 30", T10, fail(Fail::InvalidDate)),
        c(
            Date,
            "rainier dec 1",
            T10,
            fail(Fail::OutOfRange(RangeReason::BeyondHorizon)),
        ),
        // ------------------------------------------------------------ follow-up context
        cc(
            FollowUp,
            "What about midnight?",
            T10,
            "rainier",
            at("rainier", "2026-11-20T00:00:00-08:00", "midnight")
                .context()
                .e(),
        ),
        cc(
            FollowUp,
            "and at sunset?",
            T10,
            "point-reyes",
            sun("point-reyes", "2026-11-19", SS, SET, "sunset")
                .context()
                .e(),
        ),
        cc(
            FollowUp,
            "in two hours",
            T10,
            "point-reyes",
            at("point-reyes", "2026-11-19T12:00:00-08:00", "relative")
                .context()
                .e(),
        ),
        cc(
            FollowUp,
            "how about tomorrow at 9",
            T10,
            "rainier",
            at("rainier", "2026-11-20T09:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T21:00:00-08:00")
                .context()
                .e(),
        ),
        c(FollowUp, "what about midnight", T10, fail(Fail::NoPlace)),
        c(FollowUp, "at 9", T10, fail(Fail::NoPlace)),
        cc(
            FollowUp,
            "point reyes at 9",
            T10,
            "rainier",
            at("point-reyes", "2026-11-19T21:00:00-08:00", "bare-hour")
                .alt("2026-11-20T09:00:00-08:00")
                .e(),
        ),
        cc(
            FollowUp,
            "what about now",
            T10,
            "grand-canyon",
            at("grand-canyon", "2026-11-19T11:00:00-07:00", "now")
                .flags(&["live"])
                .context()
                .e(),
        ),
        cc(
            FollowUp,
            "",
            T10,
            "rainier",
            at("rainier", "2026-11-19T10:00:00-08:00", "no-time")
                .flags(&["live"])
                .context()
                .e(),
        ),
        cc(
            FollowUp,
            "golden gate at 9",
            T10,
            "rainier",
            fail(Fail::NoMatch(None)),
        ),
        cc(
            FollowUp,
            "from yavapai at sunset",
            T10,
            "rainier",
            sun("grand-canyon", "2026-11-19", SS, SET, "sunset")
                .vp("grand-canyon.yavapai")
                .e(),
        ),
        cc(
            FollowUp,
            "at noon",
            T10,
            "smokies",
            at("smokies", "2026-11-20T12:00:00-05:00", "noon")
                .flags(&[ROLLED])
                .context()
                .e(),
        ),
        cc(
            FollowUp,
            "the national park at noon",
            T10,
            "rainier",
            at("rainier", "2026-11-19T12:00:00-08:00", "noon")
                .context()
                .e(),
        ),
        // ------------------------------------------------------------ daylight saving
        // Fold: 1 Nov 2026, 01:00–02:00 happens twice in Los Angeles; the earlier offset (PDT) wins.
        c(
            Dst,
            "rainier at 1:30 am",
            "2026-10-31T22:00:00-07:00",
            at("rainier", "2026-11-01T01:30:00-07:00", "explicit-hour")
                .flags(&[ROLLED, "dst_fold"])
                .e(),
        ),
        c(
            Dst,
            "rainier at 1:30",
            "2026-10-31T22:00:00-07:00",
            at("rainier", "2026-11-01T01:30:00-07:00", "bare-hour")
                .alt("2026-11-01T13:30:00-08:00")
                .flags(&["dst_fold"])
                .e(),
        ),
        c(
            Dst,
            "rainier tomorrow at 1:30 am",
            "2026-10-31T22:00:00-07:00",
            at(
                "rainier",
                "2026-11-01T01:30:00-07:00",
                "day-plus-explicit-hour",
            )
            .flags(&["dst_fold"])
            .e(),
        ),
        c(
            Dst,
            "grand canyon tomorrow at 1:30 am",
            "2026-10-31T22:00:00-07:00",
            at(
                "grand-canyon",
                "2026-11-01T01:30:00-07:00",
                "day-plus-explicit-hour",
            )
            .e(),
        ),
        c(
            Dst,
            "grand teton tomorrow at 1:15 am",
            "2026-10-31T20:00:00-06:00",
            at(
                "grand-teton",
                "2026-11-01T01:15:00-06:00",
                "day-plus-explicit-hour",
            )
            .flags(&["dst_fold"])
            .e(),
        ),
        // Asked during the repeat: the first 01:30 (PDT) has passed, the second (PST) is ahead.
        c(
            Dst,
            "rainier at 1:30 am",
            "2026-11-01T01:15:00-08:00",
            at("rainier", "2026-11-01T01:30:00-08:00", "explicit-hour")
                .flags(&["dst_fold"])
                .e(),
        ),
        c(
            Dst,
            "rainier at 1:30",
            "2026-11-01T01:45:00-07:00",
            at("rainier", "2026-11-01T01:30:00-08:00", "bare-hour")
                .alt("2026-11-01T13:30:00-08:00")
                .flags(&["dst_fold"])
                .e(),
        ),
        c(
            Dst,
            "rainier in two hours",
            "2026-11-01T00:30:00-07:00",
            at("rainier", "2026-11-01T01:30:00-08:00", "relative").e(),
        ),
        c(
            Dst,
            "rainier tomorrow at 4",
            "2026-10-31T20:00:00-07:00",
            at("rainier", "2026-11-01T16:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-01T04:00:00-08:00")
                .e(),
        ),
        c(
            Dst,
            "rainier sunset on november 1",
            "2026-10-31T20:00:00-07:00",
            sun("rainier", "2026-11-01", SS, SET, "sunset").e(),
        ),
        // Gap: 14 Mar 2027, 02:00–03:00 does not exist in Los Angeles; it moves forward by the gap.
        c(
            Dst,
            "rainier tomorrow at 2:30 am",
            "2027-03-13T20:00:00-08:00",
            at(
                "rainier",
                "2027-03-14T03:30:00-07:00",
                "day-plus-explicit-hour",
            )
            .flags(&["dst_gap"])
            .e(),
        ),
        c(
            Dst,
            "rainier at 2:30 am",
            "2027-03-13T20:00:00-08:00",
            at("rainier", "2027-03-14T03:30:00-07:00", "explicit-hour")
                .flags(&[ROLLED, "dst_gap"])
                .e(),
        ),
        c(
            Dst,
            "rainier at 2:30",
            "2027-03-14T01:00:00-08:00",
            at("rainier", "2027-03-14T03:30:00-07:00", "bare-hour")
                .alt("2027-03-14T14:30:00-07:00")
                .flags(&["dst_gap"])
                .e(),
        ),
        c(
            Dst,
            "grand canyon tomorrow at 2:30 am",
            "2027-03-13T20:00:00-07:00",
            at(
                "grand-canyon",
                "2027-03-14T02:30:00-07:00",
                "day-plus-explicit-hour",
            )
            .e(),
        ),
        c(
            Dst,
            "grand canyon at 2:30",
            "2027-03-14T01:00:00-07:00",
            at("grand-canyon", "2027-03-14T02:30:00-07:00", "bare-hour")
                .alt("2027-03-14T14:30:00-07:00")
                .e(),
        ),
        c(
            Dst,
            "rainier at midnight",
            "2027-03-13T20:00:00-08:00",
            at("rainier", "2027-03-14T00:00:00-08:00", "midnight").e(),
        ),
        c(
            Dst,
            "rainier tomorrow at 4",
            "2027-03-13T20:00:00-08:00",
            at("rainier", "2027-03-14T16:00:00-07:00", "day-plus-bare-hour")
                .alt("2027-03-14T04:00:00-07:00")
                .e(),
        ),
        c(
            Dst,
            "point reyes sunrise",
            "2027-03-13T20:00:00-08:00",
            sun("point-reyes", "2027-03-14", SS, RISE, "sunrise").e(),
        ),
        c(
            Dst,
            "acadia tomorrow at 2:30 am",
            "2027-03-13T20:00:00-05:00",
            at(
                "acadia",
                "2027-03-14T03:30:00-04:00",
                "day-plus-explicit-hour",
            )
            .flags(&["dst_gap"])
            .e(),
        ),
        // ------------------------------------------------------------ cross-zone: machine in Chicago
        c(
            CrossZone,
            "rainier at 9",
            "2026-11-19T22:30:00-06:00",
            at("rainier", "2026-11-19T21:00:00-08:00", "bare-hour")
                .alt("2026-11-20T09:00:00-08:00")
                .e(),
        ),
        c(
            CrossZone,
            "rainier tomorrow at 4",
            "2026-11-20T00:30:00-06:00",
            at("rainier", "2026-11-20T16:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T04:00:00-08:00")
                .e(),
        ),
        c(
            CrossZone,
            "grand canyon tomorrow at 4",
            "2026-11-20T00:30:00-06:00",
            at(
                "grand-canyon",
                "2026-11-20T16:00:00-07:00",
                "day-plus-bare-hour",
            )
            .alt("2026-11-20T04:00:00-07:00")
            .e(),
        ),
        c(
            CrossZone,
            "rainier today at 11 pm",
            "2026-11-20T00:30:00-06:00",
            at(
                "rainier",
                "2026-11-19T23:00:00-08:00",
                "day-plus-explicit-hour",
            )
            .e(),
        ),
        c(
            CrossZone,
            "rainier friday at 9",
            "2026-11-20T00:30:00-06:00",
            at("rainier", "2026-11-20T09:00:00-08:00", "day-plus-bare-hour")
                .alt("2026-11-20T21:00:00-08:00")
                .e(),
        ),
        c(
            CrossZone,
            "rainier thursday",
            "2026-11-20T00:30:00-06:00",
            // Friday in Chicago, still Thursday 22:30 at Paradise: "thursday" is today there.
            at("rainier", "2026-11-19T22:30:00-08:00", "day-only-now")
                .flags(&["live"])
                .e(),
        ),
        c(
            CrossZone,
            "point reyes today",
            "2026-11-20T00:30:00-06:00",
            at("point-reyes", "2026-11-19T22:30:00-08:00", "day-only-now")
                .flags(&["live"])
                .e(),
        ),
        c(
            CrossZone,
            "grand canyon at noon",
            "2026-11-19T12:00:00-06:00",
            at("grand-canyon", "2026-11-19T12:00:00-07:00", "noon").e(),
        ),
        c(
            CrossZone,
            "rainier at noon",
            "2026-11-19T12:00:00-06:00",
            at("rainier", "2026-11-19T12:00:00-08:00", "noon").e(),
        ),
        c(
            CrossZone,
            "grand canyon now",
            "2026-11-19T12:00:00-06:00",
            at("grand-canyon", "2026-11-19T11:00:00-07:00", "now")
                .flags(&["live"])
                .e(),
        ),
        c(
            CrossZone,
            "grand canyon tomorrow at 9",
            "2027-03-13T20:00:00-06:00",
            at(
                "grand-canyon",
                "2027-03-14T09:00:00-07:00",
                "day-plus-bare-hour",
            )
            .alt("2027-03-14T21:00:00-07:00")
            .e(),
        ),
        c(
            CrossZone,
            "grand canyon tomorrow at 9",
            "2026-10-31T23:00:00-05:00",
            at(
                "grand-canyon",
                "2026-11-01T09:00:00-07:00",
                "day-plus-bare-hour",
            )
            .alt("2026-11-01T21:00:00-07:00")
            .e(),
        ),
        c(
            CrossZone,
            "rainier tonight",
            "2026-11-19T23:30:00-06:00",
            at("rainier", "2026-11-19T22:00:00-08:00", "tonight")
                .flags(&["rolled_to_next_hour"])
                .e(),
        ),
        c(
            CrossZone,
            "big bend at 9",
            "2026-11-19T12:00:00-06:00",
            at("big-bend", "2026-11-19T21:00:00-06:00", "bare-hour")
                .alt("2026-11-20T09:00:00-06:00")
                .e(),
        ),
        // ------------------------------------------------------------ horizon
        c(
            Horizon,
            "rainier in 48 hours",
            T10,
            at("rainier", "2026-11-21T10:00:00-08:00", "relative").e(),
        ),
        c(
            Horizon,
            "rainier in 47 hours and 59 minutes",
            T10,
            at("rainier", "2026-11-21T09:59:00-08:00", "relative").e(),
        ),
        c(
            Horizon,
            "rainier in 49 hours",
            T10,
            at("rainier", "2026-11-21T11:00:00-08:00", "relative")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Horizon,
            "rainier in 2 days",
            T10,
            at("rainier", "2026-11-21T10:00:00-08:00", "relative").e(),
        ),
        c(
            Horizon,
            "rainier in 7 days",
            T10,
            at("rainier", "2026-11-26T10:00:00-08:00", "relative")
                .flags(&[GFS])
                .e(),
        ),
        c(
            Horizon,
            "rainier in 8 days",
            T10,
            fail(Fail::OutOfRange(RangeReason::BeyondHorizon)),
        ),
        c(
            Horizon,
            "rainier in 170 hours",
            T10,
            fail(Fail::OutOfRange(RangeReason::BeyondHorizon)),
        ),
        c(
            Horizon,
            "rainier next thursday at 11 am",
            T10,
            fail(Fail::OutOfRange(RangeReason::BeyondHorizon)),
        ),
        c(
            Horizon,
            "rainier yesterday",
            T10,
            fail(Fail::OutOfRange(RangeReason::Past)),
        ),
        c(
            Horizon,
            "rainier last night",
            T10,
            fail(Fail::OutOfRange(RangeReason::Past)),
        ),
        c(
            Horizon,
            "rainier today at 9 am",
            T10,
            fail(Fail::OutOfRange(RangeReason::Past)),
        ),
        c(
            Horizon,
            "rainier sunset today",
            T2230,
            fail(Fail::OutOfRange(RangeReason::Past)),
        ),
        // ------------------------------------------------------------ aliases, misspellings, phonetic
        // "in an hour" is 19:00 UTC at every place.
        c(
            AliasMisspellingPhonetic,
            "rainier in an hour",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "mount rainier in an hour",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "Mt. Rainier in an hour",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "is the mountain out in an hour",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "point rays in an hour",
            T10,
            at("point-reyes", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "mount rainer in an hour",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "tahoma in an hour",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "yosemite in an hour",
            T10,
            at("yosemite", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "acadia in an hour",
            T10,
            at("acadia", "2026-11-19T14:00:00-05:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "the smokies in an hour",
            T10,
            at("smokies", "2026-11-19T14:00:00-05:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "big bend in an hour",
            T10,
            at("big-bend", "2026-11-19T13:00:00-06:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "grand tetons in an hour",
            T10,
            at("grand-teton", "2026-11-19T12:00:00-07:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "point reyes national seashore in an hour",
            T10,
            at("point-reyes", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "great smokey mountains in an hour",
            T10,
            at("smokies", "2026-11-19T14:00:00-05:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "how does look rock look in an hour",
            T10,
            at("smokies", "2026-11-19T14:00:00-05:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "Rainier's view in an hour",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "POINT REYES in an hour",
            T10,
            at("point-reyes", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "teton science center in an hour",
            T10,
            at("grand-teton", "2026-11-19T12:00:00-07:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "yosemity national park in an hour",
            T10,
            at("yosemite", "2026-11-19T11:00:00-08:00", "relative")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "grand canyon from yavapai in an hour",
            T10,
            at("grand-canyon", "2026-11-19T12:00:00-07:00", "relative")
                .vp("grand-canyon.yavapai")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "from panther junction in an hour",
            T10,
            at("big-bend", "2026-11-19T13:00:00-06:00", "relative")
                .vp("big-bend.panther-junction")
                .stage("alias")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "yosemitee in an hour",
            T10,
            at("yosemite", "2026-11-19T11:00:00-08:00", "relative")
                .stage("fuzzy")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "grand canyn in an hour",
            T10,
            at("grand-canyon", "2026-11-19T12:00:00-07:00", "relative")
                .stage("fuzzy")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "point reyez in an hour",
            T10,
            at("point-reyes", "2026-11-19T11:00:00-08:00", "relative")
                .stage("fuzzy")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "acadiah in an hour",
            T10,
            at("acadia", "2026-11-19T14:00:00-05:00", "relative")
                .stage("fuzzy")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "mount rayneer in an hour",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "relative")
                .stage("phonetic")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "rayneer in an hour",
            T10,
            at("rainier", "2026-11-19T11:00:00-08:00", "relative")
                .stage("phonetic")
                .e(),
        ),
        c(
            AliasMisspellingPhonetic,
            "akadeeya in an hour",
            T10,
            at("acadia", "2026-11-19T14:00:00-05:00", "relative")
                .stage("phonetic")
                .e(),
        ),
        // ------------------------------------------------------------ ambiguity (chips)
        c(
            Ambiguity,
            "grand canyon or grand teton at noon",
            T10,
            at("grand-canyon", "2026-11-19T12:00:00-07:00", "noon")
                .place_alt("grand-teton")
                .stage("alias")
                .e(),
        ),
        c(
            Ambiguity,
            "rainier or point reyes at noon",
            T10,
            at("point-reyes", "2026-11-19T12:00:00-08:00", "noon")
                .place_alt("rainier")
                .stage("alias")
                .e(),
        ),
        c(
            Ambiguity,
            "yosemite or acadia in an hour",
            T10,
            at("acadia", "2026-11-19T14:00:00-05:00", "relative")
                .place_alt("yosemite")
                .stage("alias")
                .e(),
        ),
        c(
            Ambiguity,
            "the smokies or acadia in an hour",
            T10,
            // The longer name wins the tie ("the smokies" is two words).
            at("smokies", "2026-11-19T14:00:00-05:00", "relative")
                .place_alt("acadia")
                .stage("alias")
                .e(),
        ),
        c(
            Ambiguity,
            "tetons or yosemite tomorrow at 4",
            T10,
            // Same length and status: the place ID decides. Denver is on MST (−07:00).
            at(
                "grand-teton",
                "2026-11-20T16:00:00-07:00",
                "day-plus-bare-hour",
            )
            .alt("2026-11-20T04:00:00-07:00")
            .place_alt("yosemite")
            .e(),
        ),
        c(
            Ambiguity,
            "point reyes tomorrow at 12:30",
            T10,
            at(
                "point-reyes",
                "2026-11-20T12:30:00-08:00",
                "day-plus-bare-hour",
            )
            .alt("2026-11-20T00:30:00-08:00")
            .e(),
        ),
        c(
            Ambiguity,
            "rainier at 8",
            T10,
            at("rainier", "2026-11-19T20:00:00-08:00", "bare-hour")
                .alt("2026-11-20T08:00:00-08:00")
                .e(),
        ),
        // The same place named twice is not a close call.
        c(
            Ambiguity,
            "big bend and big bend park in an hour",
            T10,
            at("big-bend", "2026-11-19T13:00:00-06:00", "relative")
                .stage("alias")
                .e(),
        ),
        // ------------------------------------------------------------ no match: three closest, never a guess
        c(
            NoMatchTop3,
            "Golden Gate at 9",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "zion at sunset",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "yellowstone tomorrow",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(NoMatchTop3, "seattle now", T10, fail(Fail::NoMatch(None))),
        c(
            NoMatchTop3,
            "the moon at midnight",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "mount hood at 9",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "denali tomorrow at 4",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "yosemitee valley at noon",
            T10,
            fail(Fail::NoMatch(Some("yosemite"))),
        ),
        cc(
            NoMatchTop3,
            "golden gate bridge in an hour",
            T10,
            "rainier",
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "point reyes from the moon",
            T10,
            fail(Fail::UnknownViewpoint),
        ),
        // Generic place words alone, or with a word of a place Lookout does not have, are a miss
        // with suggestions; each of these once matched a known place at full confidence.
        c(
            NoMatchTop3,
            "science center in an hour",
            T10,
            fail(Fail::NoMatch(Some("grand-teton"))),
        ),
        // A first word shared by two names is a miss that suggests both (was a close call).
        c(
            NoMatchTop3,
            "grand at noon",
            T10,
            fail(Fail::NoMatch(Some("grand-canyon"))),
        ),
        c(
            NoMatchTop3,
            "mount in an hour",
            T10,
            fail(Fail::NoMatch(Some("rainier"))),
        ),
        c(
            NoMatchTop3,
            "sunset point",
            T10,
            fail(Fail::NoMatch(Some("point-reyes"))),
        ),
        c(
            NoMatchTop3,
            "big at 9",
            T10,
            fail(Fail::NoMatch(Some("big-bend"))),
        ),
        c(
            NoMatchTop3,
            "pebble beach at sunset",
            T10,
            fail(Fail::NoMatch(Some("point-reyes"))),
        ),
        c(
            NoMatchTop3,
            "long beach at noon",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "venice beach at 9",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "myrtle beach tomorrow",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "pacific science center at 9",
            T10,
            fail(Fail::NoMatch(Some("grand-teton"))),
        ),
        c(
            NoMatchTop3,
            "rocky mountain national park at 9",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "stone mountain at 9",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "table mountain at noon",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(
            NoMatchTop3,
            "mountain view at 9",
            T10,
            fail(Fail::NoMatch(None)),
        ),
        c(NoMatchTop3, "reno at 9", T10, fail(Fail::NoMatch(None))),
        c(NoMatchTop3, "ryan at 9", T10, fail(Fail::NoMatch(None))),
        c(NoMatchTop3, "ron at 9", T10, fail(Fail::NoMatch(None))),
        c(NoMatchTop3, "rhine at 9", T10, fail(Fail::NoMatch(None))),
        // ------------------------------------------------------------ grammar rejects (LLM fallback, TODO(M3))
        c(
            GrammarRejects,
            "rainier next week",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier an hour after sunset",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier at 4 at 5",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier sunset at 5",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier later today",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier this weekend",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier now tomorrow",
            T10,
            fail(Fail::Unrecognised),
        ),
        // Words the grammar and the place names cannot explain go to the fallback; each of these
        // was once answered with "now" or a wrong day at full confidence.
        c(
            GrammarRejects,
            "point reyes sunsets",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier on thanksgiving",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier christmas eve",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier sunrize",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier sunet",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier mornings",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier in a bit",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier shortly",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier at fourish",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "rainier tomorrow arvo",
            T10,
            fail(Fail::Unrecognised),
        ),
        // A word next to a place name that is not part of it may name somewhere else.
        c(
            GrammarRejects,
            "rainier beach at 9",
            T10,
            fail(Fail::Unrecognised),
        ),
        c(
            GrammarRejects,
            "paradise valley at 9",
            T10,
            fail(Fail::Unrecognised),
        ),
        cc(
            GrammarRejects,
            "what about thanksgiving",
            T10,
            "rainier",
            fail(Fail::Unrecognised),
        ),
    ]
}

fn name<T: serde::Serialize>(v: &T) -> String {
    serde_json::to_value(v)
        .ok()
        .and_then(|x| x.as_str().map(str::to_string))
        .unwrap_or_default()
}

fn flags_of(q: &Query) -> Vec<String> {
    let mut f: Vec<String> = q.flags.iter().map(name).collect();
    f.sort();
    f
}

fn sorted(v: &[&str]) -> Vec<String> {
    let mut f: Vec<String> = v.iter().map(|s| s.to_string()).collect();
    f.sort();
    f
}

fn common_checks(
    q: &Query,
    table: &PlaceTable,
    flags: &[&str],
    source: &str,
) -> Result<(), String> {
    let (_, vp) = table
        .viewpoint(&q.viewpoint_id)
        .ok_or("unknown viewpoint")?;
    if q.tz != vp.tz.name() {
        return Err(format!("tz {} vs {}", q.tz, vp.tz.name()));
    }
    if q.valid_time != q.valid_time_utc {
        return Err("valid_time and valid_time_utc are different instants".into());
    }
    if flags_of(q) != sorted(flags) {
        return Err(format!(
            "flags {:?}, expected {:?}",
            flags_of(q),
            sorted(flags)
        ));
    }
    let gfs = q.flags.iter().any(|f| name(f) == "gfs_horizon");
    if name(&q.horizon) != if gfs { "gfs" } else { "hrrr" } {
        return Err(format!(
            "horizon {} inconsistent with flags",
            name(&q.horizon)
        ));
    }
    if name(&q.place_source) != source {
        return Err(format!(
            "place_source {}, expected {source}",
            name(&q.place_source)
        ));
    }
    if !(0.0..=1.0).contains(&q.confidence) {
        return Err(format!("confidence {}", q.confidence));
    }
    Ok(())
}

fn check_at(e: &At, q: &Query, table: &PlaceTable) -> Result<(), String> {
    let vt = lookout_core::query::fmt_local(&q.valid_time);
    if q.place_id != e.place || vt != e.vt || name(&q.time_source.rule) != e.rule {
        return Err(format!(
            "got {} {} {}, expected {} {} {}",
            q.place_id,
            vt,
            name(&q.time_source.rule),
            e.place,
            e.vt,
            e.rule
        ));
    }
    if let Some(v) = e.viewpoint
        && q.viewpoint_id != v
    {
        return Err(format!("viewpoint {}, expected {v}", q.viewpoint_id));
    }
    let meridiem = q
        .ambiguities
        .iter()
        .find(|a| name(&a.field) == "meridiem")
        .map(|a| a.alternative.as_str());
    if meridiem != e.alt {
        return Err(format!(
            "meridiem alternative {meridiem:?}, expected {:?}",
            e.alt
        ));
    }
    let place_alt = q
        .ambiguities
        .iter()
        .find(|a| name(&a.field) == "place")
        .map(|a| a.alternative.as_str());
    if place_alt != e.place_alt {
        return Err(format!(
            "place alternative {place_alt:?}, expected {:?}",
            e.place_alt
        ));
    }
    if let Some(stage) = e.stage
        && name(&q.place_match.stage) != stage
    {
        return Err(format!(
            "stage {}, expected {stage}",
            name(&q.place_match.stage)
        ));
    }
    common_checks(q, table, &e.flags, e.source)
}

fn check_sun(e: &Sun, q: &Query, table: &PlaceTable) -> Result<(), String> {
    let vp = match e.viewpoint {
        Some(v) => table.viewpoint(v).ok_or("unknown viewpoint")?.1,
        None => {
            let (_, p) = table.place(e.place).ok_or("unknown place")?;
            &table.viewpoints()[p.default_viewpoint]
        }
    };
    if q.place_id != e.place || q.viewpoint_id != vp.id || name(&q.time_source.rule) != e.rule {
        return Err(format!(
            "got {} {} {}, expected {} {} {}",
            q.place_id,
            q.viewpoint_id,
            name(&q.time_source.rule),
            e.place,
            vp.id,
            e.rule
        ));
    }
    let date = NaiveDate::parse_from_str(e.date, "%Y-%m-%d").map_err(|x| x.to_string())?;
    let site = vp.site;
    let expected = noaa::event_utc(date, site.lat_deg, site.lon_deg, e.alt_deg, e.rising)
        .ok_or("NOAA: no event")?
        + Duration::seconds(e.plus_secs);
    let got = q.valid_time_utc;
    let diff = (got - expected).num_seconds();
    if diff.abs() > 60 {
        return Err(format!("valid time {got} vs NOAA {expected} ({diff} s)"));
    }
    if got.with_timezone(&vp.tz).date_naive() != expected.with_timezone(&vp.tz).date_naive() {
        return Err(format!(
            "local date of {got} differs from NOAA's {expected}"
        ));
    }
    match (e.interval, &q.time_source.interval) {
        (None, None) => {}
        (Some((a, b)), Some(iv)) => {
            let ea = noaa::event_utc(date, site.lat_deg, site.lon_deg, a, e.rising)
                .ok_or("NOAA: no start")?;
            let eb = noaa::event_utc(date, site.lat_deg, site.lon_deg, b, e.rising)
                .ok_or("NOAA: no end")?;
            let (sa, sb) = (iv.start.with_timezone(&Utc), iv.end.with_timezone(&Utc));
            if (sa - ea).num_seconds().abs() > 60 || (sb - eb).num_seconds().abs() > 60 {
                return Err(format!("interval {sa}..{sb} vs NOAA {ea}..{eb}"));
            }
            if (iv.start_altitude_deg, iv.end_altitude_deg) != (a, b) {
                return Err(format!(
                    "interval altitudes {} {}",
                    iv.start_altitude_deg, iv.end_altitude_deg
                ));
            }
            if !(sa <= got && got <= sb) {
                return Err("rendered instant outside the interval".into());
            }
        }
        (want, got) => return Err(format!("interval {got:?}, expected {want:?}")),
    }
    if e.plus_secs == 0 && q.time_source.sun_altitude_deg != Some(e.alt_deg) {
        return Err(format!(
            "sun_altitude_deg {:?}",
            q.time_source.sun_altitude_deg
        ));
    }
    common_checks(q, table, &e.flags, e.source)
}

fn check_fail(e: &Fail, err: &ParseError, table: &PlaceTable) -> Result<(), String> {
    match (e, err) {
        (Fail::NoPlace, ParseError::NoPlace)
        | (Fail::Unrecognised, ParseError::Unrecognised { .. })
        | (Fail::InvalidDate, ParseError::InvalidDate { .. })
        | (Fail::UnknownViewpoint, ParseError::UnknownViewpoint { .. }) => Ok(()),
        (Fail::OutOfRange(want), ParseError::OutOfRange { reason, .. }) if want == reason => Ok(()),
        (Fail::NoMatch(first), ParseError::NoMatch { closest, .. }) => {
            if closest.len() != 3 {
                return Err(format!("{} closest places, expected 3", closest.len()));
            }
            let mut ids: Vec<&str> = closest.iter().map(|c| c.place_id.as_str()).collect();
            if closest.windows(2).any(|w| w[0].score < w[1].score) {
                return Err("closest places are not sorted by score".into());
            }
            ids.sort();
            ids.dedup();
            if ids.len() != 3 || ids.iter().any(|id| table.place(id).is_none()) {
                return Err(format!(
                    "closest places are not three known places: {closest:?}"
                ));
            }
            match first {
                Some(f) if closest[0].place_id != *f => {
                    Err(format!("closest[0] {}, expected {f}", closest[0].place_id))
                }
                _ => Ok(()),
            }
        }
        _ => Err(format!("error {err:?}, expected {e:?}")),
    }
}

#[test]
fn parser_table() {
    let parser = Parser::embedded();
    let table = parser.places();
    let cases = cases();
    let mut failures = Vec::new();
    let mut counts: BTreeMap<Cat, usize> = BTreeMap::new();
    for case in &cases {
        *counts.entry(case.cat).or_default() += 1;
        let now = DateTime::parse_from_rfc3339(case.now)
            .expect("test clock")
            .with_timezone(&Utc);
        let last = case.ctx.map(|p| {
            let (_, place) = table.place(p).expect("context place");
            LastPlace {
                place_id: p.to_string(),
                viewpoint_id: table.viewpoints()[place.default_viewpoint].id.clone(),
            }
        });
        let result = parser.parse(
            case.utt,
            &ParseContext {
                now,
                last: last.as_ref(),
            },
        );
        let outcome = match (&case.exp, &result) {
            (Exp::At(e), Ok(q)) => check_at(e, q, table),
            (Exp::Sun(e), Ok(q)) => check_sun(e, q, table),
            (Exp::Fail(e), Err(err)) => check_fail(e, err, table),
            (_, other) => Err(format!("unexpected result {other:?}")),
        };
        if let Err(msg) = outcome {
            failures.push(format!(
                "[{:?}] {:?} @ {}: {msg}",
                case.cat, case.utt, case.now
            ));
        }
    }
    println!("parser table: {} cases", cases.len());
    for (cat, n) in &counts {
        println!("  {cat:?}: {n}");
    }
    assert!(
        failures.is_empty(),
        "{} of {} cases failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
    assert!(
        cases.len() >= 150,
        "PRD §7.6 asks for at least 150 utterances"
    );
    for (cat, n) in &counts {
        assert!(*n >= 5, "category {cat:?} has only {n} cases");
    }
}

#[test]
fn golden_hour_asked_during_golden_hour_is_tonights() {
    // Asked between the +6° and +2° crossings: the rendered instant (+2°) is still ahead, so the
    // current golden hour is used, not tomorrow's.
    let parser = Parser::embedded();
    let table = parser.places();
    let (_, vp) = table.viewpoint("point-reyes.beach").expect("viewpoint");
    let date = NaiveDate::from_ymd_opt(2026, 11, 19).expect("date");
    let start = noaa::event_utc(date, vp.site.lat_deg, vp.site.lon_deg, 6.0, false).expect("start");
    let render =
        noaa::event_utc(date, vp.site.lat_deg, vp.site.lon_deg, 2.0, false).expect("render");
    let now = start + (render - start) / 2;
    let q = parser
        .parse("point reyes golden hour", &ParseContext { now, last: None })
        .expect("parses");
    assert!(
        (q.valid_time_utc - render).num_seconds().abs() <= 60,
        "{} vs {render}",
        q.valid_time_utc
    );
    let iv = q.time_source.interval.expect("interval");
    assert!(
        iv.start.with_timezone(&Utc) < now,
        "the interval began before the question"
    );
}

#[test]
fn prd_example_json_exactly() {
    let parser = Parser::embedded();
    let now = DateTime::parse_from_rfc3339(T10)
        .expect("time")
        .with_timezone(&Utc);
    let q = parser
        .parse(
            "Is Rainier out tomorrow at 4?",
            &ParseContext { now, last: None },
        )
        .expect("parses");
    let v: serde_json::Value = serde_json::from_str(&q.to_json()).expect("json");
    let prd: serde_json::Value = serde_json::from_str(
        r#"{"place_id": "rainier", "viewpoint_id": "rainier.paradise-mountain",
            "valid_time": "2026-11-20T16:00:00-08:00", "valid_time_utc": "2026-11-21T00:00:00Z",
            "tz": "America/Los_Angeles",
            "time_source": {"kind": "clock", "text": "tomorrow at 4", "rule": "day-plus-bare-hour"},
            "ambiguities": [{"field": "meridiem", "chosen": "pm", "alternative": "2026-11-20T04:00:00-08:00"}],
            "overrides": {}, "path": "fast_path", "confidence": 0.97}"#,
    )
    .expect("PRD JSON");
    for (k, want) in prd.as_object().expect("object") {
        assert_eq!(&v[k], want, "field {k}");
    }
}
