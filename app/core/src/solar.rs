//! Sun and Moon positions and solar events at a viewpoint (PRD §7.4 time rules, §8.4 ephemeris).
//!
//! Positions come from `oculus-core` (pinned at `06809e6`): JPL mean elements for the Sun and
//! truncated ELP-2000/82 for the Moon, with aberration, precession, nutation and topocentric
//! parallax, measured against JPL Horizons to 0.19′ for the Sun and 0.13′ for the Moon (PRD
//! §8.4). [`moon_position`] and [`sun_and_moon`] give the Moon's altitude, azimuth and
//! illuminated fraction for the caption (PRD §5.3 "Moon 41% lit, below horizon"). This module
//! adds what the time rules need:
//!
//! - **geometric** altitude (the topocentric altitude of the Sun's centre with no refraction;
//!   `oculus_core::ephem::Apparent::alt`), **refracted** altitude (Sæmundsson, via
//!   `oculus_core::ephem::refracted_alt`) and azimuth, all in degrees;
//! - an **event search**: the first instant in a window at which the geometric altitude crosses a
//!   threshold in a given direction. It scans a 1-minute grid and bisects the bracketing minute to
//!   1 ms. Grid points that provably cannot hold a crossing are skipped: the Sun's altitude changes
//!   by at most [`MAX_ALTITUDE_RATE_DEG_PER_MIN`], so from a point where the altitude is `d`
//!   degrees from the threshold the next `floor(d / rate)` minutes contain no crossing. The result
//!   is the same crossing a dense 1-minute scan finds (tested against one below), for ~40 Sun
//!   evaluations instead of 1,440 per day.
//!
//! Event thresholds (PRD §7.4): sunrise and sunset at −0.833° (upper limb on the horizon after
//! refraction), civil dawn and dusk at −6°, golden hour from +6° to −4° rendered at +2°, blue hour
//! from −4° to −6° rendered at −5°.

use chrono::{DateTime, Duration, NaiveDate, Utc};
use chrono_tz::Tz;
use oculus_core::ephem::{Apparent, Body, apparent, refracted_alt, sun_moon};
use serde::{Deserialize, Serialize};

use crate::tzutil::start_of_local_day;

/// Sunrise and sunset: the Sun's centre at −0.833° geometric altitude.
pub const SUNRISE_SUNSET_DEG: f64 = -0.833;
/// Civil dawn and dusk.
pub const CIVIL_TWILIGHT_DEG: f64 = -6.0;
/// Golden hour: upper bound, rendered instant, lower bound.
pub const GOLDEN_HOUR_HIGH_DEG: f64 = 6.0;
/// Rendered instant of golden hour.
pub const GOLDEN_HOUR_RENDER_DEG: f64 = 2.0;
/// Lower bound of golden hour.
pub const GOLDEN_HOUR_LOW_DEG: f64 = -4.0;
/// Blue hour: upper bound.
pub const BLUE_HOUR_HIGH_DEG: f64 = -4.0;
/// Rendered instant of blue hour.
pub const BLUE_HOUR_RENDER_DEG: f64 = -5.0;
/// Blue hour: lower bound.
pub const BLUE_HOUR_LOW_DEG: f64 = -6.0;

/// A safe upper bound on |d(altitude)/dt| of the Sun, degrees per minute.
///
/// The Earth turns 360° per solar day relative to the Sun (0.2500°/min); a point on the sky moves
/// in altitude at most that fast. The Sun's own motion adds < 0.0003°/min in declination.
pub const MAX_ALTITUDE_RATE_DEG_PER_MIN: f64 = 0.26;

/// How far around a rendered instant to look for the bounds of a golden or blue hour.
const INTERVAL_SEARCH: Duration = Duration::hours(4);

/// Where the observer stands: the camera position from the viewpoint manifest.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Site {
    /// Geodetic latitude, degrees north.
    pub lat_deg: f64,
    /// Longitude, degrees east (west is negative).
    pub lon_deg: f64,
    /// Height above sea level, metres (used for topocentric parallax).
    pub elevation_m: f64,
}

/// The Sun as seen from a site at one instant.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct SunPosition {
    /// Topocentric altitude of the Sun's centre with no refraction, degrees.
    pub geometric_altitude_deg: f64,
    /// Altitude after atmospheric refraction (Sæmundsson 1986, 1010 hPa, 10 °C), degrees.
    pub refracted_altitude_deg: f64,
    /// Azimuth from north through east, degrees in [0, 360).
    pub azimuth_deg: f64,
}

/// Whether the Sun is going up or down through a threshold.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    /// Altitude increasing (morning).
    Rising,
    /// Altitude decreasing (evening).
    Setting,
}

fn unix_ms(t: DateTime<Utc>) -> f64 {
    // Nanosecond timestamps cover 1677–2262, far wider than oculus-core's 1800–2050 validity.
    match t.timestamp_nanos_opt() {
        Some(ns) => ns as f64 / 1.0e6,
        None => t.timestamp_millis() as f64,
    }
}

/// The Moon as seen from a site at one instant.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct MoonPosition {
    /// Topocentric altitude of the Moon's centre with no refraction, degrees.
    pub geometric_altitude_deg: f64,
    /// Altitude after atmospheric refraction (as for the Sun), degrees.
    pub refracted_altitude_deg: f64,
    /// Azimuth from north through east, degrees in [0, 360).
    pub azimuth_deg: f64,
    /// Illuminated fraction of the disc as seen from the site: 0 new, 1 full.
    pub illuminated_fraction: f64,
}

fn sun_from(a: &Apparent) -> SunPosition {
    SunPosition {
        geometric_altitude_deg: a.alt.to_degrees(),
        refracted_altitude_deg: refracted_alt(a.alt).to_degrees(),
        azimuth_deg: a.az.to_degrees(),
    }
}

/// Sun position for a site and UTC instant (PRD §8.4 "Apparent altitude with atmospheric refraction").
pub fn sun_position(site: &Site, t: DateTime<Utc>) -> SunPosition {
    let a = apparent(
        Body::Sun,
        unix_ms(t),
        site.lat_deg.to_radians(),
        site.lon_deg.to_radians(),
        site.elevation_m,
    );
    sun_from(&a)
}

/// Moon position and phase for a site and UTC instant (PRD §8.4; §14 target ≤ 0.3°).
pub fn moon_position(site: &Site, t: DateTime<Utc>) -> MoonPosition {
    sun_and_moon(site, t).1
}

/// The Sun and the Moon together from one ephemeris evaluation, as the caption needs both.
pub fn sun_and_moon(site: &Site, t: DateTime<Utc>) -> (SunPosition, MoonPosition) {
    let (sun, moon) = sun_moon(
        unix_ms(t),
        site.lat_deg.to_radians(),
        site.lon_deg.to_radians(),
        site.elevation_m,
    );
    (
        sun_from(&sun),
        MoonPosition {
            geometric_altitude_deg: moon.alt.to_degrees(),
            refracted_altitude_deg: refracted_alt(moon.alt).to_degrees(),
            azimuth_deg: moon.az.to_degrees(),
            illuminated_fraction: moon.illuminated,
        },
    )
}

/// Geometric altitude of the Sun's centre, degrees (the quantity every event threshold uses).
pub fn geometric_altitude_deg(site: &Site, t: DateTime<Utc>) -> f64 {
    sun_position(site, t).geometric_altitude_deg
}

fn crosses(dir: Direction, before: f64, after: f64) -> bool {
    match dir {
        Direction::Rising => before < 0.0 && after >= 0.0,
        Direction::Setting => before >= 0.0 && after < 0.0,
    }
}

fn bisect(
    f: &impl Fn(DateTime<Utc>) -> f64,
    dir: Direction,
    mut lo: DateTime<Utc>,
    mut hi: DateTime<Utc>,
    mut f_lo: f64,
) -> DateTime<Utc> {
    // Invariant: crosses(dir, f(lo), f(hi)). 60 s halves to 1 ms in 16 steps.
    while hi - lo > Duration::milliseconds(1) {
        let mid = lo + (hi - lo) / 2;
        let f_mid = f(mid);
        if crosses(dir, f_lo, f_mid) {
            hi = mid;
        } else {
            lo = mid;
            f_lo = f_mid;
        }
    }
    lo + (hi - lo) / 2
}

/// First instant in `[start, end)` at which the Sun's geometric altitude crosses `threshold_deg`
/// in direction `dir`, or `None` if it does not (PRD §7.4 solar rules).
///
/// Grid points are `start + n` whole minutes; the bracketing minute is bisected to 1 ms. Points
/// that cannot hold a crossing are skipped using [`MAX_ALTITUDE_RATE_DEG_PER_MIN`].
pub fn find_crossing(
    site: &Site,
    threshold_deg: f64,
    dir: Direction,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
) -> Option<DateTime<Utc>> {
    if end <= start {
        return None;
    }
    let f = |t: DateTime<Utc>| geometric_altitude_deg(site, t) - threshold_deg;
    let mut t_prev = start;
    let mut f_prev = f(start);
    loop {
        let safe_minutes = (f_prev.abs() / MAX_ALTITUDE_RATE_DEG_PER_MIN).floor();
        let step = if safe_minutes >= 2.0 {
            safe_minutes.min(24.0 * 60.0) as i64
        } else {
            1
        };
        let t_next = (t_prev + Duration::minutes(step)).min(end);
        let f_next = f(t_next);
        if crosses(dir, f_prev, f_next) {
            return Some(bisect(&f, dir, t_prev, t_next, f_prev));
        }
        if t_next >= end {
            return None;
        }
        t_prev = t_next;
        f_prev = f_next;
    }
}

/// The same search as [`find_crossing`] but evaluating every grid minute (no skipping).
///
/// Kept for the equivalence test; it costs ~1,440 Sun evaluations per day searched.
pub fn find_crossing_dense(
    site: &Site,
    threshold_deg: f64,
    dir: Direction,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
) -> Option<DateTime<Utc>> {
    if end <= start {
        return None;
    }
    let f = |t: DateTime<Utc>| geometric_altitude_deg(site, t) - threshold_deg;
    let mut t_prev = start;
    let mut f_prev = f(start);
    while t_prev < end {
        let t_next = (t_prev + Duration::minutes(1)).min(end);
        let f_next = f(t_next);
        if crosses(dir, f_prev, f_next) {
            return Some(bisect(&f, dir, t_prev, t_next, f_prev));
        }
        t_prev = t_next;
        f_prev = f_next;
    }
    None
}

/// The first and last instants of the local calendar day `date` at the place, as `[start, end)`.
///
/// DST-aware: 1 Nov 2026 in Los Angeles is 25 h long and 14 Mar 2027 is 23 h.
pub fn local_day_bounds(tz: Tz, date: NaiveDate) -> (DateTime<Utc>, DateTime<Utc>) {
    let next = date.succ_opt().unwrap_or(date);
    (
        start_of_local_day(tz, date).with_timezone(&Utc),
        start_of_local_day(tz, next).with_timezone(&Utc),
    )
}

/// A named solar event from the PRD §7.4 table.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SolarEvent {
    /// Sun's centre rising through −0.833°.
    Sunrise,
    /// Sun's centre setting through −0.833°.
    Sunset,
    /// Civil dawn: rising through −6°.
    Dawn,
    /// Civil dusk: setting through −6°.
    Dusk,
    /// −4° → +6° rising, rendered at +2°.
    MorningGoldenHour,
    /// +6° → −4° setting, rendered at +2°.
    EveningGoldenHour,
    /// −6° → −4° rising, rendered at −5°.
    MorningBlueHour,
    /// −4° → −6° setting, rendered at −5°.
    EveningBlueHour,
}

/// Thresholds that define a [`SolarEvent`].
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct EventSpec {
    /// Altitude of the rendered instant, degrees.
    pub render_deg: f64,
    /// Direction of every crossing in the event.
    pub direction: Direction,
    /// For intervals: (altitude at the start, altitude at the end), in time order.
    pub interval_deg: Option<(f64, f64)>,
}

impl SolarEvent {
    /// The thresholds for this event (PRD §7.4).
    pub fn spec(self) -> EventSpec {
        use Direction::{Rising, Setting};
        let (render_deg, direction, interval_deg) = match self {
            SolarEvent::Sunrise => (SUNRISE_SUNSET_DEG, Rising, None),
            SolarEvent::Sunset => (SUNRISE_SUNSET_DEG, Setting, None),
            SolarEvent::Dawn => (CIVIL_TWILIGHT_DEG, Rising, None),
            SolarEvent::Dusk => (CIVIL_TWILIGHT_DEG, Setting, None),
            SolarEvent::MorningGoldenHour => (
                GOLDEN_HOUR_RENDER_DEG,
                Rising,
                Some((GOLDEN_HOUR_LOW_DEG, GOLDEN_HOUR_HIGH_DEG)),
            ),
            SolarEvent::EveningGoldenHour => (
                GOLDEN_HOUR_RENDER_DEG,
                Setting,
                Some((GOLDEN_HOUR_HIGH_DEG, GOLDEN_HOUR_LOW_DEG)),
            ),
            SolarEvent::MorningBlueHour => (
                BLUE_HOUR_RENDER_DEG,
                Rising,
                Some((BLUE_HOUR_LOW_DEG, BLUE_HOUR_HIGH_DEG)),
            ),
            SolarEvent::EveningBlueHour => (
                BLUE_HOUR_RENDER_DEG,
                Setting,
                Some((BLUE_HOUR_HIGH_DEG, BLUE_HOUR_LOW_DEG)),
            ),
        };
        EventSpec {
            render_deg,
            direction,
            interval_deg,
        }
    }
}

/// When a solar event happens: the rendered instant and, for golden and blue hour, the interval.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EventTimes {
    /// The instant to render (the event itself for sunrise, sunset, dawn and dusk).
    pub render: DateTime<Utc>,
    /// `[start, end]` of a golden or blue hour; `None` for single-instant events.
    pub interval: Option<(DateTime<Utc>, DateTime<Utc>)>,
}

/// The first occurrence of `event` whose rendered instant lies in `[start, end)`.
pub fn event_in_window(
    site: &Site,
    event: SolarEvent,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
) -> Option<EventTimes> {
    let spec = event.spec();
    let render = find_crossing(site, spec.render_deg, spec.direction, start, end)?;
    let interval = match spec.interval_deg {
        None => None,
        Some((start_deg, end_deg)) => {
            let begin = find_crossing(
                site,
                start_deg,
                spec.direction,
                render - INTERVAL_SEARCH,
                render,
            );
            let finish = find_crossing(
                site,
                end_deg,
                spec.direction,
                render,
                render + INTERVAL_SEARCH,
            );
            match (begin, finish) {
                (Some(b), Some(f)) => Some((b, f)),
                _ => None,
            }
        }
    };
    Some(EventTimes { render, interval })
}

/// The next occurrence of `event` with its rendered instant at or after `after` (searches 48 h).
pub fn next_event(site: &Site, event: SolarEvent, after: DateTime<Utc>) -> Option<EventTimes> {
    event_in_window(site, event, after, after + Duration::hours(48))
}

/// The occurrence of `event` on the local calendar day `date` at the place.
pub fn event_on_local_date(
    site: &Site,
    tz: Tz,
    date: NaiveDate,
    event: SolarEvent,
) -> Option<EventTimes> {
    let (start, end) = local_day_bounds(tz, date);
    event_in_window(site, event, start, end)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    const RAINIER: Site = Site {
        lat_deg: 46.78455,
        lon_deg: -121.74065,
        elevation_m: 1643.0,
    };
    const GRAND_CANYON: Site = Site {
        lat_deg: 36.065981,
        lon_deg: -112.118236,
        elevation_m: 2150.0,
    };

    fn utc(y: i32, m: u32, d: u32, h: u32, mi: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(y, m, d, h, mi, 0)
            .single()
            .expect("valid test instant")
    }

    /// Tiny deterministic generator for sampling test cases (SplitMix64).
    struct Rng(u64);
    impl Rng {
        fn next(&mut self) -> u64 {
            self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = self.0;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            z ^ (z >> 31)
        }
        fn uniform(&mut self, lo: f64, hi: f64) -> f64 {
            lo + (hi - lo) * ((self.next() >> 11) as f64 / (1u64 << 53) as f64)
        }
    }

    #[test]
    fn noon_sun_is_south_and_midnight_sun_is_down() {
        // Apparent solar noon at Paradise (−121.74°) on 19 Nov: 12:00 + 8 h 07 min − 14.6 min
        // (equation of time) ≈ 19:52 UTC.
        let p = sun_position(&RAINIER, utc(2026, 11, 19, 19, 52));
        assert!((p.azimuth_deg - 180.0).abs() < 1.0, "az {}", p.azimuth_deg);
        // Noon altitude ≈ 90 − 46.78 − 19.6 (declination) ≈ 23.6°.
        assert!(
            (p.geometric_altitude_deg - 23.6).abs() < 0.5,
            "alt {}",
            p.geometric_altitude_deg
        );
        assert!(p.refracted_altitude_deg > p.geometric_altitude_deg);
        let night = sun_position(&RAINIER, utc(2026, 11, 20, 8, 10));
        assert!(night.geometric_altitude_deg < -50.0);
    }

    #[test]
    fn refraction_at_the_horizon_is_about_half_a_degree() {
        let t = find_crossing(
            &RAINIER,
            0.0,
            Direction::Setting,
            utc(2026, 11, 19, 18, 0),
            utc(2026, 11, 20, 6, 0),
        )
        .expect("sets");
        let p = sun_position(&RAINIER, t);
        let r = p.refracted_altitude_deg - p.geometric_altitude_deg;
        assert!((r - 0.48).abs() < 0.05, "refraction {r}°");
    }

    #[test]
    fn crossings_have_the_right_altitude_and_direction() {
        let start = utc(2026, 11, 19, 8, 0);
        let end = start + Duration::hours(24);
        for thr in [
            SUNRISE_SUNSET_DEG,
            CIVIL_TWILIGHT_DEG,
            GOLDEN_HOUR_HIGH_DEG,
            BLUE_HOUR_RENDER_DEG,
        ] {
            for dir in [Direction::Rising, Direction::Setting] {
                let t =
                    find_crossing(&RAINIER, thr, dir, start, end).expect("crossing within a day");
                let alt = geometric_altitude_deg(&RAINIER, t);
                assert!((alt - thr).abs() < 0.001, "{thr} {dir:?}: {alt}");
                let later = geometric_altitude_deg(&RAINIER, t + Duration::minutes(5));
                match dir {
                    Direction::Rising => assert!(later > alt),
                    Direction::Setting => assert!(later < alt),
                }
            }
        }
    }

    #[test]
    fn skipping_search_equals_dense_one_minute_scan() {
        // 200 random sites, windows and thresholds across 2026–2028.
        let mut rng = Rng(0x5EED_0001);
        let base = utc(2026, 1, 1, 0, 0);
        let mut found = 0;
        for _ in 0..200 {
            let site = Site {
                lat_deg: rng.uniform(-60.0, 60.0),
                lon_deg: rng.uniform(-180.0, 180.0),
                elevation_m: rng.uniform(0.0, 3000.0),
            };
            let start = base + Duration::seconds(rng.uniform(0.0, 3.0 * 365.0 * 86400.0) as i64);
            let end = start + Duration::minutes(rng.uniform(30.0, 2.0 * 1440.0) as i64);
            let thr = rng.uniform(-10.0, 10.0);
            let dir = if rng.next().is_multiple_of(2) {
                Direction::Rising
            } else {
                Direction::Setting
            };
            let fast = find_crossing(&site, thr, dir, start, end);
            let dense = find_crossing_dense(&site, thr, dir, start, end);
            match (fast, dense) {
                (Some(a), Some(b)) => {
                    assert!((a - b).num_milliseconds().abs() <= 1, "{a} vs {b}");
                    found += 1;
                }
                (None, None) => {}
                other => panic!("mismatch {other:?} for {site:?} {thr} {dir:?} {start}..{end}"),
            }
        }
        assert!(found > 80, "too few crossings exercised: {found}");
    }

    #[test]
    fn golden_hour_interval_brackets_the_render_instant() {
        let tz = chrono_tz::America::Phoenix;
        let date = NaiveDate::from_ymd_opt(2026, 11, 20).expect("date");
        let ev = event_on_local_date(&GRAND_CANYON, tz, date, SolarEvent::EveningGoldenHour)
            .expect("event");
        let (a, b) = ev.interval.expect("interval");
        assert!(a < ev.render && ev.render < b);
        assert!((geometric_altitude_deg(&GRAND_CANYON, a) - 6.0).abs() < 0.001);
        assert!((geometric_altitude_deg(&GRAND_CANYON, b) + 4.0).abs() < 0.001);
        let morning = event_on_local_date(&GRAND_CANYON, tz, date, SolarEvent::MorningBlueHour)
            .expect("event");
        let (a, b) = morning.interval.expect("interval");
        assert!((geometric_altitude_deg(&GRAND_CANYON, a) + 6.0).abs() < 0.001);
        assert!((geometric_altitude_deg(&GRAND_CANYON, b) + 4.0).abs() < 0.001);
    }

    #[test]
    fn next_event_after_the_morning_event_has_passed_is_tomorrow() {
        let after = utc(2026, 11, 19, 18, 0); // 10:00 PST, after today's sunrise
        let ev = next_event(&RAINIER, SolarEvent::Sunrise, after).expect("sunrise");
        assert_eq!(
            ev.render
                .with_timezone(&chrono_tz::America::Los_Angeles)
                .date_naive(),
            NaiveDate::from_ymd_opt(2026, 11, 20).expect("date")
        );
    }

    #[test]
    fn sun_and_moon_agree_with_the_single_calls() {
        let t = utc(2026, 11, 24, 7, 0);
        let (sun, moon) = sun_and_moon(&RAINIER, t);
        assert_eq!(sun, sun_position(&RAINIER, t));
        assert_eq!(moon, moon_position(&RAINIER, t));
        assert!((0.0..=1.0).contains(&moon.illuminated_fraction));
        assert!((0.0..360.0).contains(&moon.azimuth_deg));
        assert!(moon.refracted_altitude_deg >= moon.geometric_altitude_deg);
    }

    #[test]
    fn empty_window_has_no_crossing() {
        let t = utc(2026, 11, 19, 0, 0);
        assert_eq!(find_crossing(&RAINIER, 0.0, Direction::Rising, t, t), None);
        // The Sun never reaches +80° at Paradise.
        assert_eq!(
            find_crossing(&RAINIER, 80.0, Direction::Rising, t, t + Duration::days(2)),
            None
        );
    }
}
