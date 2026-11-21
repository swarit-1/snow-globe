//! Wall-clock to instant conversion at a place, with the PRD §7.4 daylight-saving policy.
//!
//! - A **nonexistent** wall time (spring-forward gap) moves forward by the size of the gap
//!   (02:30 on 14 Mar 2027 in Los Angeles becomes 03:30 PDT) and is flagged [`DstAdjust::Gap`].
//! - An **ambiguous** wall time (fall-back fold) takes the earlier offset, i.e. the first
//!   occurrence (01:30 on 1 Nov 2026 in Los Angeles becomes 01:30 PDT, −07:00), and is flagged
//!   [`DstAdjust::Fold`].
//!
//! This is the same policy as `snow_globe_common.timeutil.localize` on the Python side.
//! [`localize_not_before`] adds one refinement for the query parser: asked *during* the repeated
//! hour, an ambiguous time whose first occurrence has passed takes its second occurrence.

use chrono::{
    DateTime, Duration, LocalResult, NaiveDate, NaiveDateTime, NaiveTime, Offset, TimeZone, Utc,
};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};

/// What the daylight-saving policy had to do to turn a wall time into an instant.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DstAdjust {
    /// The wall time did not exist; it was moved forward by the gap.
    Gap,
    /// The wall time happened twice; the first occurrence (earlier offset) was taken.
    Fold,
}

/// Attach the place's zone to a wall time and return the instant plus any DST adjustment.
///
/// Implements the last two sentences of the PRD §7.4 table ("A daylight-saving gap or repeat").
pub fn localize(tz: Tz, naive: NaiveDateTime) -> (DateTime<Tz>, Option<DstAdjust>) {
    match tz.from_local_datetime(&naive) {
        LocalResult::Single(dt) => (dt, None),
        LocalResult::Ambiguous(a, b) => (a.min(b), Some(DstAdjust::Fold)),
        LocalResult::None => {
            // Use the UTC offset in force before the transition. A day earlier is well clear of
            // the gap for every real zone, so this is the "before" offset.
            let probe = naive - Duration::hours(24);
            let before = tz.offset_from_utc_datetime(&probe).fix();
            let utc = naive - Duration::seconds(i64::from(before.local_minus_utc()));
            (tz.from_utc_datetime(&utc), Some(DstAdjust::Gap))
        }
    }
}

/// [`localize`], except that an ambiguous wall time whose first occurrence is before `not_before`
/// while its second is not takes the second occurrence (still flagged [`DstAdjust::Fold`]).
///
/// PRD §7.4 resolves a time to the next such time and takes the earlier offset for an ambiguous
/// one. Asked at 01:15 PST on 1 Nov 2026 (the repeat), the earlier 01:30 (PDT) is 45 minutes past
/// and the later one (PST) is 15 minutes ahead, so "1:30" is the later one rather than tomorrow.
pub fn localize_not_before(
    tz: Tz,
    naive: NaiveDateTime,
    not_before: DateTime<Utc>,
) -> (DateTime<Tz>, Option<DstAdjust>) {
    match tz.from_local_datetime(&naive) {
        LocalResult::Ambiguous(a, b) => {
            let (first, second) = if a <= b { (a, b) } else { (b, a) };
            if first.with_timezone(&Utc) < not_before && second.with_timezone(&Utc) >= not_before {
                (second, Some(DstAdjust::Fold))
            } else {
                (first, Some(DstAdjust::Fold))
            }
        }
        _ => localize(tz, naive),
    }
}

/// [`localize`] for a date plus a time of day.
pub fn localize_date_time(
    tz: Tz,
    date: NaiveDate,
    time: NaiveTime,
) -> (DateTime<Tz>, Option<DstAdjust>) {
    localize(tz, date.and_time(time))
}

/// Start of the local calendar day `date` at the place (its first instant).
///
/// Midnight itself can fall in a DST gap in a few zones; the gap rule then moves it forward.
pub fn start_of_local_day(tz: Tz, date: NaiveDate) -> DateTime<Tz> {
    localize_date_time(tz, date, NaiveTime::MIN).0
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;

    fn naive(s: &str) -> NaiveDateTime {
        NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M").expect("valid test time")
    }

    #[test]
    fn spring_gap_moves_forward_by_the_gap() {
        let (dt, adj) = localize(chrono_tz::America::Los_Angeles, naive("2027-03-14T02:30"));
        assert_eq!(adj, Some(DstAdjust::Gap));
        assert_eq!(dt.to_rfc3339(), "2027-03-14T03:30:00-07:00");
        assert_eq!(
            dt.with_timezone(&Utc).to_rfc3339(),
            "2027-03-14T10:30:00+00:00"
        );
    }

    #[test]
    fn fall_fold_takes_the_earlier_offset() {
        let (dt, adj) = localize(chrono_tz::America::Los_Angeles, naive("2026-11-01T01:30"));
        assert_eq!(adj, Some(DstAdjust::Fold));
        assert_eq!(dt.to_rfc3339(), "2026-11-01T01:30:00-07:00");
    }

    #[test]
    fn during_the_repeat_the_second_occurrence_is_next() {
        let tz = chrono_tz::America::Los_Angeles;
        let at = |s: &str| {
            DateTime::parse_from_rfc3339(s)
                .expect("rfc3339")
                .with_timezone(&Utc)
        };
        let wall = naive("2026-11-01T01:30");
        // Before the first 01:30: the first (PDT).
        let (dt, adj) = localize_not_before(tz, wall, at("2026-11-01T00:30:00-07:00"));
        assert_eq!(
            (dt.to_rfc3339().as_str(), adj),
            ("2026-11-01T01:30:00-07:00", Some(DstAdjust::Fold))
        );
        // Between the two: the second (PST), 15 and 45 minutes ahead.
        for now in ["2026-11-01T01:45:00-07:00", "2026-11-01T01:15:00-08:00"] {
            let (dt, adj) = localize_not_before(tz, wall, at(now));
            assert_eq!(
                (dt.to_rfc3339().as_str(), adj),
                ("2026-11-01T01:30:00-08:00", Some(DstAdjust::Fold)),
                "{now}"
            );
        }
        // After both: the first again (the caller rolls to the next day).
        let (dt, _) = localize_not_before(tz, wall, at("2026-11-01T02:00:00-08:00"));
        assert_eq!(dt.to_rfc3339(), "2026-11-01T01:30:00-07:00");
        // Unambiguous times are untouched.
        let plain = naive("2026-11-19T10:00");
        assert_eq!(
            localize_not_before(tz, plain, at("2026-11-19T12:00:00-08:00")),
            localize(tz, plain)
        );
    }

    #[test]
    fn phoenix_has_neither_gap_nor_fold() {
        for s in ["2027-03-14T02:30", "2026-11-01T01:30"] {
            let (dt, adj) = localize(chrono_tz::America::Phoenix, naive(s));
            assert_eq!(adj, None, "{s}");
            assert_eq!(dt.offset().fix().local_minus_utc(), -7 * 3600, "{s}");
        }
    }

    #[test]
    fn new_york_and_denver_gaps() {
        let (ny, adj) = localize(chrono_tz::America::New_York, naive("2027-03-14T02:15"));
        assert_eq!(
            (ny.to_rfc3339().as_str(), adj),
            ("2027-03-14T03:15:00-04:00", Some(DstAdjust::Gap))
        );
        let (den, adj) = localize(chrono_tz::America::Denver, naive("2027-03-14T02:59"));
        assert_eq!(
            (den.to_rfc3339().as_str(), adj),
            ("2027-03-14T03:59:00-06:00", Some(DstAdjust::Gap))
        );
    }

    #[test]
    fn ordinary_time_is_untouched() {
        let (dt, adj) = localize(chrono_tz::America::Chicago, naive("2026-11-19T10:00"));
        assert_eq!(adj, None);
        assert_eq!(dt.to_rfc3339(), "2026-11-19T10:00:00-06:00");
    }

    #[test]
    fn day_start_on_dst_days() {
        let tz = chrono_tz::America::Los_Angeles;
        let d1 = NaiveDate::from_ymd_opt(2026, 11, 1).expect("date");
        let d2 = NaiveDate::from_ymd_opt(2026, 11, 2).expect("date");
        let len = start_of_local_day(tz, d2) - start_of_local_day(tz, d1);
        assert_eq!(
            len,
            Duration::hours(25),
            "1 Nov 2026 is 25 h long in Los Angeles"
        );
        let s1 = NaiveDate::from_ymd_opt(2027, 3, 14).expect("date");
        let s2 = NaiveDate::from_ymd_opt(2027, 3, 15).expect("date");
        assert_eq!(
            start_of_local_day(tz, s2) - start_of_local_day(tz, s1),
            Duration::hours(23)
        );
    }
}
