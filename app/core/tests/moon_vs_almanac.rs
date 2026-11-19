//! The Moon (PRD §8.4, §14 "Moon ≤ 0.3°"; §5.3 caption "Moon 41% lit"): `lookout_core::solar`'s
//! wrapper of oculus-core against the independent Astronomical Almanac low-precision Moon in
//! `tests/common`.
//!
//! The reference is good to about 0.3°, so this checks the wrapping (radians to degrees, the
//! site, the topocentric place, the phase) to 0.5° and 0.01 in illuminated fraction; the
//! ephemeris itself was measured against JPL Horizons to 0.13′ (PRD §8.4). Run with
//! `--nocapture` to see the statistics.

mod common;

use chrono::{DateTime, Duration, Utc};
use common::{Stats, almanac_moon};
use lookout_core::places::PlaceTable;
use lookout_core::solar::moon_position;

#[test]
fn moon_matches_the_almanac_formulae() {
    let table = PlaceTable::embedded();
    let start = DateTime::parse_from_rfc3339("2026-11-01T00:00:00Z")
        .expect("start")
        .with_timezone(&Utc);
    // 400 instants over 2026-11 .. 2028-11, 43 h 47 min apart, so every hour of the day and every
    // phase is visited.
    let step = Duration::minutes(43 * 60 + 47);
    let mut separation = Stats::default();
    let mut phase = Stats::default();
    let mut worst = (0.0f64, String::new());
    for vp in table.viewpoints() {
        for i in 0..400 {
            let t = start + step * i;
            let ours = moon_position(&vp.site, t);
            let reference =
                almanac_moon::moon(t, vp.site.lat_deg, vp.site.lon_deg, vp.site.elevation_m);
            let (a1, a2) = (
                ours.geometric_altitude_deg.to_radians(),
                reference.alt_deg.to_radians(),
            );
            let daz = (ours.azimuth_deg - reference.az_deg).to_radians();
            let cos_sep = a1.sin() * a2.sin() + a1.cos() * a2.cos() * daz.cos();
            let sep = cos_sep.clamp(-1.0, 1.0).acos().to_degrees();
            separation.push(sep);
            phase.push(ours.illuminated_fraction - reference.illuminated);
            if sep > worst.0 {
                worst = (sep, format!("{} {t}", vp.id));
            }
        }
    }
    println!(
        "Moon place vs almanac: n={} max {:.3}° ({}), mean {:.3}°, rms {:.3}°, p95 {:.3}°",
        separation.n(),
        separation.max_abs(),
        worst.1,
        separation.mean_abs(),
        separation.rms(),
        separation.p95_abs()
    );
    println!(
        "Moon illuminated fraction vs almanac: n={} max {:.4}, mean |d| {:.4}, bias {:+.4}",
        phase.n(),
        phase.max_abs(),
        phase.mean_abs(),
        phase.mean()
    );
    assert!(separation.max_abs() <= 0.5, "{}", separation.max_abs());
    assert!(phase.max_abs() <= 0.01, "{}", phase.max_abs());
}
