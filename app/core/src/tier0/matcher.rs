//! Tier 0's match: (viewpoint, valid time) → the closest real frames (PRD §9.1 "Query").
//!
//! **The descriptor** (PRD §9.1): the Sun's altitude and its azimuth relative to the camera's view
//! azimuth, computed from the ephemeris at the manifest's camera position (`oculus-core`, PRD
//! §8.4), the day of year, and the extractor's weather class. The query side is built the same way
//! at the valid time. The weather class needs extractor v0 to label the library and the forecast
//! file on the query side (TODO(M1.5)). Until then the match uses the rest of the descriptor,
//! **Sun position and day of year**, and every result says the weather class was not used
//! ([`Matched::weather_class_used`]). This reads PRD §9.1's fallback ("the match falls back to
//! Sun position only" when the forecast has not loaded) as dropping the forecast-dependent part,
//! the weather class, and keeping the day of year, which the ephemeris alone gives; dropping the
//! day-of-year term too would be the other reading (the ticket asks for day of year; the lead
//! records the reading in DECISIONS.md).
//!
//! **The distance** between the query `q` and a frame `f` is the Euclidean norm of three terms,
//! each in units of a "just noticeable" difference:
//!
//! ```text
//! d = sqrt(A² + Z² + Y²)
//! A = |g(h_f) − g(h_q)| / 1.0         g(h) = 6° · asinh(max(h, −18°) / 6°)
//! Z = w(h_q) · |Δφ| · cos(h̄) / 15°   Δφ = relative azimuth, wrapped to (−180°, 180°]
//! Y = |Δt mod year| / 45 days
//! ```
//!
//! - **A, altitude.** `g` keeps a degree a degree at the horizon, where light changes fastest
//!   (twilight, golden hour), and compresses high altitudes: one unit is 1° at 0°, about 3.5° at
//!   20° and 7.6° at 45°. Below −18° (astronomical night) every altitude counts as −18°.
//! - **Z, azimuth.** The arc along the almucantar, `|Δφ| · cos(h̄)` with `h̄` the mean of the two
//!   altitudes (azimuth means less as the Sun climbs), weighted by `w(h_q)`: 1 with the query's
//!   Sun at or above 0°, falling linearly to 0 at −12°, because the Sun's direction is invisible
//!   at night. Morning and afternoon frames at one altitude lie on opposite sides of the
//!   meridian, so away from midday this term keeps them apart (lit faces and shadows differ).
//! - **Y, day of year.** The gap between the two instants modulo the tropical year
//!   (365.2422 days), in (−182.6, 182.6] days: season (snow, leaves, haze) beyond what the Sun's
//!   position already says.
//!
//! **Ties** are broken by entry key, so the result never depends on entry order. **A frame
//! qualifies** if it is in the library (every exclusion passed), at the asked viewpoint, was
//! available at the cut-off ([`MatchRequest::as_of`]: in evaluation the issue time, in the CLI the
//! time of asking) and is within [`MAX_DISTANCE`]. Available means taken before the cut-off and
//! fetched by it ([`Entry::available_unix_ms`]): PRD §9.1 "Leakage" ("only frames captured before
//! the issue time") with §13.1's "frames available at issue time", as eval's leakage rule 1 reads
//! them (`image_time <= t` and `available_at <= t`). A live frame is fetched about 12 minutes after
//! it is taken (p50 over 1,189 frames on 23 Nov 2026; max 35), so a replay at a past `--now` does
//! not see a live frame the app could not yet have had. An archived frame counts as available from
//! its image time, as in eval (NPS publishes it then), whenever the backfill fetched it. Otherwise
//! the result is a [`NoFrame`] with a plain message, never a guess.
//!
//! **Not fitted.** These scales and the limit are a provisional, documented choice. The
//! pre-registration fits the analog distance, its weights and the "no close analog" stratum on
//! development data by the 14 Dec freeze (PLAN.md M2; TODO(M2)).
//!
//! **Not yet eval's R3.** PRD §9.1 makes Tier 0 the analog baseline R3 (§13.4), but eval's
//! `baselines.analog` is a separate provisional definition: `sqrt((1.0·Δalt)² + (0.25·Δaz)² +
//! (0·Δdoy)²)` in degrees on the raw geometric altitude and absolute azimuth, no day-of-year term,
//! a 5° "no close analog" limit beyond which it still returns the Sun-only match (flagged), ties
//! to the most recent frame, and the weather class first. Until the pre-registration fixes one
//! distance and the other follows it (TODO(M2); the lead records which in DECISIONS.md), R3's
//! analog and the frame the product shows can differ.

use chrono::{DateTime, Utc};
use serde::Serialize;

use crate::query::rfc3339_utc;
use crate::solar::sun_position;
use crate::tier0::camera::{CameraInfo, CameraTable};
use crate::tier0::library::{Entry, Library, wrap_deg};

/// `g`'s scale, degrees.
pub const ALT_SCALE_DEG: f64 = 6.0;
/// Altitudes below this count as this (astronomical night).
pub const ALT_FLOOR_DEG: f64 = -18.0;
/// One unit of the altitude term, in horizon-weighted degrees.
pub const ALT_UNIT: f64 = 1.0;
/// One unit of the azimuth term, degrees of arc.
pub const AZ_UNIT_DEG: f64 = 15.0;
/// The azimuth term has its full weight with the query's Sun at or above this altitude.
pub const AZ_FULL_FROM_DEG: f64 = 0.0;
/// The azimuth term has no weight with the query's Sun at or below this altitude.
pub const AZ_NONE_BELOW_DEG: f64 = -12.0;
/// One unit of the day-of-year term, days.
pub const DOY_UNIT_DAYS: f64 = 45.0;
/// The tropical year, days.
pub const TROPICAL_YEAR_DAYS: f64 = 365.2422;
/// A frame farther than this does not qualify (provisional, TODO(M2)).
pub const MAX_DISTANCE: f64 = 4.0;
/// Alternatives reported beside the best frame, by default.
pub const DEFAULT_ALTERNATIVES: usize = 2;

/// `g(h)`: the Sun's altitude in horizon-weighted degrees (module docs).
pub fn horizon_weighted_altitude(alt_deg: f64) -> f64 {
    let a = alt_deg.clamp(ALT_FLOOR_DEG, 90.0);
    ALT_SCALE_DEG * (a / ALT_SCALE_DEG).asinh()
}

/// `w(h)`: the azimuth term's weight for a query Sun at `alt_deg`.
pub fn azimuth_weight(alt_deg: f64) -> f64 {
    ((alt_deg - AZ_NONE_BELOW_DEG) / (AZ_FULL_FROM_DEG - AZ_NONE_BELOW_DEG)).clamp(0.0, 1.0)
}

/// `b − a` in days, modulo the tropical year, in (−182.6, 182.6].
pub fn year_gap_days(a: DateTime<Utc>, b: DateTime<Utc>) -> f64 {
    let days = (b - a).num_milliseconds() as f64 / 86_400_000.0;
    let r = days.rem_euclid(TROPICAL_YEAR_DAYS);
    if r > TROPICAL_YEAR_DAYS / 2.0 {
        r - TROPICAL_YEAR_DAYS
    } else {
        r
    }
}

/// The Sun's place at a camera at one instant.
#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
pub struct Descriptor {
    /// Geometric altitude, degrees.
    pub sun_altitude_deg: f64,
    /// Azimuth relative to the camera's view azimuth, degrees in (−180, 180]; positive is right
    /// of the view centre.
    pub sun_azimuth_rel_deg: f64,
    /// The instant.
    #[serde(with = "rfc3339_utc")]
    pub instant: DateTime<Utc>,
}

/// The query side of the descriptor at a valid time (PRD §9.1 "Query").
pub fn query_descriptor(camera: &CameraInfo, valid_time: DateTime<Utc>) -> Descriptor {
    let sun = sun_position(&camera.site, valid_time);
    Descriptor {
        sun_altitude_deg: sun.geometric_altitude_deg,
        sun_azimuth_rel_deg: wrap_deg(sun.azimuth_deg, camera.view_azimuth_deg),
        instant: valid_time,
    }
}

/// A frame's descriptor, from its entry.
pub fn entry_descriptor(e: &Entry) -> Descriptor {
    Descriptor {
        sun_altitude_deg: e.sun_altitude_deg,
        sun_azimuth_rel_deg: e.sun_azimuth_rel_deg,
        instant: e.taken_utc,
    }
}

/// The three terms of the distance, in units (each ≥ 0).
#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
pub struct Terms {
    /// A: altitude.
    pub sun_altitude: f64,
    /// Z: azimuth.
    pub sun_azimuth: f64,
    /// Y: day of year.
    pub day_of_year: f64,
}

/// How the frame differs from the query: frame minus query.
#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
pub struct Delta {
    /// Sun altitude, degrees.
    pub sun_altitude_deg: f64,
    /// Sun relative azimuth, degrees in (−180, 180].
    pub sun_azimuth_deg: f64,
    /// Day of year, days in (−182.6, 182.6]: negative means earlier in the year.
    pub days_of_year: f64,
}

/// The distance from the query to an entry (module docs), with its terms and the raw deltas.
/// `q_hw` is `horizon_weighted_altitude(q.sun_altitude_deg)`, computed once per query.
pub fn distance(q: &Descriptor, q_hw: f64, e: &Entry) -> (f64, Terms, Delta) {
    let d_az = wrap_deg(e.sun_azimuth_rel_deg, q.sun_azimuth_rel_deg);
    let mean_alt = (0.5 * (q.sun_altitude_deg + e.sun_altitude_deg)).clamp(-90.0, 90.0);
    let gap = year_gap_days(q.instant, e.taken_utc);
    let terms = Terms {
        sun_altitude: (e.alt_hw - q_hw).abs() / ALT_UNIT,
        sun_azimuth: azimuth_weight(q.sun_altitude_deg) * d_az.abs() * mean_alt.to_radians().cos()
            / AZ_UNIT_DEG,
        day_of_year: gap.abs() / DOY_UNIT_DAYS,
    };
    let d =
        (terms.sun_altitude.powi(2) + terms.sun_azimuth.powi(2) + terms.day_of_year.powi(2)).sqrt();
    let delta = Delta {
        sun_altitude_deg: e.sun_altitude_deg - q.sun_altitude_deg,
        sun_azimuth_deg: d_az,
        days_of_year: gap,
    };
    (d, terms, delta)
}

/// One scored frame.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Scored {
    /// Its index in [`Library::entries`].
    #[serde(skip)]
    pub index: usize,
    /// Its key.
    pub key: String,
    /// Its distance (module docs).
    pub distance: f64,
    /// The distance's terms.
    pub terms: Terms,
    /// Frame minus query.
    pub delta: Delta,
}

fn better(a: &Scored, b: &Scored) -> bool {
    a.distance
        .total_cmp(&b.distance)
        .then_with(|| a.key.cmp(&b.key))
        .is_lt()
}

/// The `k` closest of `entries` to `q`, closest first, ties by key: a total order, so any order
/// of the same entries gives the same result.
pub fn rank<'a>(
    q: &Descriptor,
    entries: impl IntoIterator<Item = (usize, &'a Entry)>,
    k: usize,
) -> Vec<Scored> {
    let q_hw = horizon_weighted_altitude(q.sun_altitude_deg);
    let mut top: Vec<Scored> = Vec::with_capacity(k.saturating_add(1).min(1024));
    if k == 0 {
        return top;
    }
    for (index, e) in entries {
        let (d, terms, delta) = distance(q, q_hw, e);
        if top.len() == k {
            let worst = &top[k - 1];
            let worse_than_all = d
                .total_cmp(&worst.distance)
                .then_with(|| e.key.as_str().cmp(worst.key.as_str()))
                .is_ge();
            if worse_than_all {
                continue;
            }
        }
        let s = Scored {
            index,
            key: e.key.clone(),
            distance: d,
            terms,
            delta,
        };
        let at = top.partition_point(|t| better(t, &s));
        top.insert(at, s);
        top.truncate(k);
    }
    top
}

/// A descriptor term a match used.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MatchTerm {
    /// The Sun's altitude.
    SunAltitude,
    /// The Sun's azimuth relative to the camera (not used with the query's Sun below −12°).
    SunAzimuth,
    /// The day of year.
    DayOfYear,
}

/// What to match.
#[derive(Debug, Clone, PartialEq)]
pub struct MatchRequest {
    /// The viewpoint.
    pub viewpoint_id: String,
    /// The valid time.
    pub valid_time: DateTime<Utc>,
    /// The cut-off: only frames taken strictly before it and fetched by it qualify (the time of
    /// asking, or the issue time in evaluation; PRD §9.1 "Leakage", §13.1; module docs). `None`
    /// lets every frame in the library qualify.
    pub as_of: Option<DateTime<Utc>>,
    /// How many alternatives to report beside the best frame.
    pub alternatives: usize,
    /// The qualifying limit on the distance.
    pub max_distance: f64,
}

impl MatchRequest {
    /// A request with the default alternatives and limit, and no cut-off.
    pub fn new(viewpoint_id: &str, valid_time: DateTime<Utc>) -> MatchRequest {
        MatchRequest {
            viewpoint_id: viewpoint_id.to_string(),
            valid_time,
            as_of: None,
            alternatives: DEFAULT_ALTERNATIVES,
            max_distance: MAX_DISTANCE,
        }
    }
}

/// A qualifying frame.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Matched {
    /// The chosen frame.
    pub best: Scored,
    /// The next closest qualifying frames, closest first.
    pub alternatives: Vec<Scored>,
    /// The query's descriptor.
    pub query: Descriptor,
    /// The descriptor terms the match used.
    pub matched_on: Vec<MatchTerm>,
    /// Always false until extractor v0 labels the library (TODO(M1.5)): the weather class is the
    /// part of the descriptor that falls back (module docs).
    pub weather_class_used: bool,
    /// Frames of the viewpoint that were candidates (available at the cut-off).
    pub candidates: usize,
}

/// Why no frame qualifies.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum NoFrameReason {
    /// Not a known camera.
    UnknownViewpoint,
    /// The library holds no frame from this camera.
    EmptyLibrary,
    /// No frame from this camera was available at the cut-off: each was taken at or after it, or
    /// fetched after it.
    NoneAvailable,
    /// The closest frame is beyond [`MatchRequest::max_distance`].
    NoCloseAnalog,
}

/// No frame qualifies, said plainly.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NoFrame {
    /// Why.
    pub reason: NoFrameReason,
    /// A plain sentence for people.
    pub message: String,
    /// The query's descriptor, when the camera is known.
    pub query: Option<Descriptor>,
    /// The closest frame, when there was one (not shown).
    pub nearest: Option<Scored>,
    /// Frames of the viewpoint that were candidates.
    pub candidates: usize,
}

/// What a match gave.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum MatchOutcome {
    /// A frame qualifies.
    Matched(Matched),
    /// None does.
    NoFrame(NoFrame),
}

/// Find the closest real frames for a request (module docs).
pub fn find(library: &Library, cameras: &CameraTable, req: &MatchRequest) -> MatchOutcome {
    let Some(camera) = cameras.get(&req.viewpoint_id) else {
        return MatchOutcome::NoFrame(NoFrame {
            reason: NoFrameReason::UnknownViewpoint,
            message: format!(
                "No real photo qualifies: {} is not a known camera.",
                req.viewpoint_id
            ),
            query: None,
            nearest: None,
            candidates: 0,
        });
    };
    let q = query_descriptor(camera, req.valid_time);
    let indexes = library.viewpoint_indexes(&req.viewpoint_id);
    let name = camera.display_name();
    if indexes.is_empty() {
        return MatchOutcome::NoFrame(NoFrame {
            reason: NoFrameReason::EmptyLibrary,
            message: format!(
                "No real photo qualifies: the library holds no frame from {name} that passes the \
                 exclusions."
            ),
            query: Some(q),
            nearest: None,
            candidates: 0,
        });
    }
    let entries = library.entries();
    let cut = req.as_of.map(|t| (t, t.timestamp_millis()));
    let candidates: Vec<(usize, &Entry)> = indexes
        .iter()
        .map(|&i| (i, &entries[i]))
        .filter(|(_, e)| cut.is_none_or(|(t, t_ms)| e.taken_utc < t && e.available_unix_ms <= t_ms))
        .collect();
    if candidates.is_empty() {
        let cut = req
            .as_of
            .map(|t| crate::query::fmt_utc(&t))
            .unwrap_or_default();
        return MatchOutcome::NoFrame(NoFrame {
            reason: NoFrameReason::NoneAvailable,
            message: format!(
                "No real photo qualifies: every frame from {name} in the library was taken at or \
                 after {cut}, or fetched after it, and Tier 0 shows only frames it already had at \
                 the time of asking."
            ),
            query: Some(q),
            nearest: None,
            candidates: 0,
        });
    }
    let n = candidates.len();
    // At least one, so the best exists: `candidates` is not empty here.
    let k = req.alternatives.saturating_add(1).min(n);
    let mut ranked = rank(&q, candidates, k);
    let best = ranked.remove(0);
    if best.distance > req.max_distance {
        return MatchOutcome::NoFrame(NoFrame {
            reason: NoFrameReason::NoCloseAnalog,
            message: format!(
                "No real photo qualifies: the closest frame from {name} is {:.2} from the asked \
                 Sun position and time of year, beyond the limit of {:.1}.",
                best.distance, req.max_distance
            ),
            query: Some(q),
            nearest: Some(best),
            candidates: n,
        });
    }
    ranked.retain(|s| s.distance <= req.max_distance);
    let mut matched_on = vec![MatchTerm::SunAltitude];
    if azimuth_weight(q.sun_altitude_deg) > 0.0 {
        matched_on.push(MatchTerm::SunAzimuth);
    }
    matched_on.push(MatchTerm::DayOfYear);
    MatchOutcome::Matched(Matched {
        best,
        alternatives: ranked,
        query: q,
        matched_on,
        weather_class_used: false,
        candidates: n,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tier0::library::{ColourMode, EntrySource, ImageRef};
    use chrono::{Duration, TimeZone};
    use std::path::Path;

    const VP: &str = "point-reyes.beach";

    fn cam() -> &'static CameraInfo {
        CameraTable::embedded().get(VP).expect("camera")
    }

    fn utc(y: i32, m: u32, d: u32, h: u32, mi: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(y, m, d, h, mi, 0).single().expect("t")
    }

    /// An entry whose Sun is the real Sun at the camera at `t`.
    fn real(key: &str, t: DateTime<Utc>) -> Entry {
        let d = query_descriptor(cam(), t);
        let az = (d.sun_azimuth_rel_deg + cam().view_azimuth_deg).rem_euclid(360.0);
        Entry::new(
            key.to_string(),
            cam(),
            EntrySource::Live,
            t,
            d.sun_altitude_deg,
            az,
            ImageRef {
                rel_path: format!("thumbs/{VP}/{key}.jpg"),
                width: Some(640),
                height: Some(360),
            },
            ColourMode::Colour,
        )
    }

    fn lib(entries: Vec<Entry>) -> Library {
        Library::from_entries(Path::new("/data"), entries)
    }

    fn best(l: &Library, t: DateTime<Utc>) -> String {
        match find(l, CameraTable::embedded(), &MatchRequest::new(VP, t)) {
            MatchOutcome::Matched(m) => m.best.key,
            MatchOutcome::NoFrame(n) => panic!("{n:?}"),
        }
    }

    #[test]
    fn g_is_degrees_at_the_horizon_and_compressed_above() {
        assert!(horizon_weighted_altitude(0.0).abs() < 1e-12);
        let slope = |a: f64| horizon_weighted_altitude(a + 0.01) - horizon_weighted_altitude(a);
        assert!((slope(0.0) / 0.01 - 1.0).abs() < 1e-3);
        assert!((0.01 / slope(45.0) - 7.6).abs() < 0.1);
        assert_eq!(
            horizon_weighted_altitude(-40.0),
            horizon_weighted_altitude(-18.0)
        );
        assert_eq!(azimuth_weight(5.0), 1.0);
        assert_eq!(azimuth_weight(-6.0), 0.5);
        assert_eq!(azimuth_weight(-20.0), 0.0);
    }

    #[test]
    fn year_gap_wraps_the_new_year() {
        let a = utc(2026, 12, 30, 12, 0);
        let b = utc(2027, 1, 2, 12, 0);
        assert!((year_gap_days(a, b) - 3.0).abs() < 1e-9);
        assert!((year_gap_days(b, a) + 3.0).abs() < 1e-9);
        // One year apart, the same clock time: about zero.
        let c = utc(2025, 11, 23, 12, 0);
        assert!(year_gap_days(c, utc(2026, 11, 23, 12, 0)).abs() < 0.3);
    }

    #[test]
    fn picks_the_frame_at_the_same_sun_on_the_nearest_day() {
        // Frames every 15 min on 20 Nov; ask for 23 Nov at 14:07 PST (22:07 UTC).
        let mut entries = Vec::new();
        for i in 0..96 {
            let t = utc(2026, 11, 20, 0, 0) + Duration::minutes(15 * i);
            entries.push(real(&format!("k{i:02}"), t));
        }
        let l = lib(entries);
        // 22:07 UTC on 23 Nov; the Sun three days earlier at 22:00–22:15 UTC is closest.
        let chosen = best(&l, utc(2026, 11, 23, 22, 7));
        assert!(chosen == "k88" || chosen == "k89", "{chosen}");
    }

    #[test]
    fn morning_never_stands_in_for_evening() {
        // Two frames at nearly the same altitude: one morning, one afternoon.
        let morning = real("morning", utc(2026, 11, 20, 17, 30)); // 09:30 PST
        let evening = real("evening", utc(2026, 11, 20, 22, 50)); // 14:50 PST
        assert!((morning.sun_altitude_deg - evening.sun_altitude_deg).abs() < 4.0);
        let l = lib(vec![morning, evening]);
        assert_eq!(best(&l, utc(2026, 11, 22, 17, 30)), "morning");
        assert_eq!(best(&l, utc(2026, 11, 22, 22, 50)), "evening");
    }

    #[test]
    fn at_night_the_azimuth_is_ignored_and_the_nearest_date_wins() {
        let a = real("a-night-20", utc(2026, 11, 20, 10, 0)); // 02:00 PST
        let b = real("b-night-22", utc(2026, 11, 22, 4, 0)); // 20:00 PST
        let l = lib(vec![a, b]);
        let m = match find(
            &l,
            CameraTable::embedded(),
            &MatchRequest::new(VP, utc(2026, 11, 24, 10, 0)),
        ) {
            MatchOutcome::Matched(m) => m,
            MatchOutcome::NoFrame(n) => panic!("{n:?}"),
        };
        assert_eq!(m.best.key, "b-night-22");
        assert_eq!(m.best.terms.sun_altitude, 0.0, "both below −18°");
        assert_eq!(m.best.terms.sun_azimuth, 0.0);
        assert!(!m.matched_on.contains(&MatchTerm::SunAzimuth));
        assert!(!m.weather_class_used);
    }

    #[test]
    fn the_cut_off_and_the_limit_say_so_plainly() {
        let early = real("early", utc(2026, 11, 20, 20, 0));
        let late = real("late", utc(2026, 11, 22, 20, 0));
        let l = lib(vec![early, late]);
        let mut req = MatchRequest::new(VP, utc(2026, 11, 23, 20, 0));
        req.as_of = Some(utc(2026, 11, 21, 0, 0));
        match find(&l, CameraTable::embedded(), &req) {
            MatchOutcome::Matched(m) => {
                assert_eq!(m.best.key, "early", "the later frame is after the cut-off");
                assert_eq!(m.candidates, 1);
            }
            MatchOutcome::NoFrame(n) => panic!("{n:?}"),
        }
        req.as_of = Some(utc(2026, 11, 19, 0, 0));
        let MatchOutcome::NoFrame(n) = find(&l, CameraTable::embedded(), &req) else {
            panic!("no frame is before 19 Nov")
        };
        assert_eq!(n.reason, NoFrameReason::NoneAvailable);
        assert!(n.message.starts_with("No real photo qualifies"));
        // A summer midday frame for a November night is no analog.
        let summer = real("summer", utc(2026, 5, 20, 20, 0));
        let l = lib(vec![summer]);
        let MatchOutcome::NoFrame(n) = find(
            &l,
            CameraTable::embedded(),
            &MatchRequest::new(VP, utc(2026, 11, 24, 10, 0)),
        ) else {
            panic!("too far")
        };
        assert_eq!(n.reason, NoFrameReason::NoCloseAnalog);
        assert!(n.nearest.is_some_and(|s| s.distance > MAX_DISTANCE));
        let MatchOutcome::NoFrame(n) = find(
            &lib(vec![]),
            CameraTable::embedded(),
            &MatchRequest::new(VP, utc(2026, 11, 24, 10, 0)),
        ) else {
            panic!("empty")
        };
        assert_eq!(n.reason, NoFrameReason::EmptyLibrary);
        let MatchOutcome::NoFrame(n) = find(
            &lib(vec![]),
            CameraTable::embedded(),
            &MatchRequest::new("golden-gate.bridge", utc(2026, 11, 24, 10, 0)),
        ) else {
            panic!("unknown")
        };
        assert_eq!(n.reason, NoFrameReason::UnknownViewpoint);
    }

    #[test]
    fn a_frame_fetched_after_the_cut_off_is_not_seen() {
        // Taken at 23:30 UTC, fetched at 23:47:31 (the real Point Reyes frame of 22 Nov 2026).
        let taken = utc(2026, 11, 22, 23, 30);
        let mut late = real("late-fetch", taken);
        late.available_unix_ms = (taken + Duration::milliseconds(1_051_384)).timestamp_millis();
        let earlier = real("earlier", utc(2026, 11, 22, 23, 15));
        let l = lib(vec![late, earlier]);
        let ask = |as_of: DateTime<Utc>| {
            let mut req = MatchRequest::new(VP, utc(2026, 11, 22, 23, 30));
            req.as_of = Some(as_of);
            match find(&l, CameraTable::embedded(), &req) {
                MatchOutcome::Matched(m) => (m.best.key, m.candidates),
                MatchOutcome::NoFrame(n) => panic!("{n:?}"),
            }
        };
        // At 23:35 the frame was taken but not yet fetched: the 23:15 frame is shown.
        assert_eq!(ask(utc(2026, 11, 22, 23, 35)), ("earlier".to_string(), 1));
        // Fetched at 23:47:31.384; at that instant it is available, a millisecond before it is not.
        let fetched = taken + Duration::milliseconds(1_051_384);
        assert_eq!(ask(fetched).0, "late-fetch");
        assert_eq!(
            ask(fetched - Duration::milliseconds(1)).0,
            "earlier",
            "a millisecond before its fetch"
        );
    }

    #[test]
    fn ties_go_to_the_lower_key_in_any_order() {
        let t = utc(2026, 11, 20, 20, 0);
        let a = real("a", t);
        let b = real("b", t);
        let q = query_descriptor(cam(), utc(2026, 11, 21, 20, 0));
        let fwd = rank(&q, [(0, &a), (1, &b)], 2);
        let rev = rank(&q, [(1, &b), (0, &a)], 2);
        assert_eq!(fwd, rev);
        assert_eq!(fwd[0].key, "a");
        assert_eq!(fwd[0].distance, fwd[1].distance);
        assert!(rank(&q, [(0, &a)], 0).is_empty());
    }

    #[test]
    fn a_huge_alternative_count_is_capped_by_the_candidates() {
        let l = lib(vec![
            real("a", utc(2026, 11, 20, 20, 0)),
            real("b", utc(2026, 11, 21, 20, 0)),
        ]);
        let mut req = MatchRequest::new(VP, utc(2026, 11, 22, 20, 0));
        req.alternatives = usize::MAX;
        let MatchOutcome::Matched(m) = find(&l, CameraTable::embedded(), &req) else {
            panic!("a match")
        };
        assert_eq!(m.best.key, "b");
        assert_eq!(m.alternatives.len(), 1);
        assert!(rank(&m.query, l.entries().iter().enumerate(), usize::MAX).len() == 2);
    }

    #[test]
    fn the_same_descriptor_is_distance_zero() {
        let t = utc(2026, 11, 20, 20, 0);
        let e = real("e", t);
        let q = entry_descriptor(&e);
        let (d, terms, delta) = distance(&q, horizon_weighted_altitude(q.sun_altitude_deg), &e);
        assert_eq!(d, 0.0);
        assert_eq!(terms.day_of_year, 0.0);
        assert_eq!(delta.sun_azimuth_deg, 0.0);
    }
}
