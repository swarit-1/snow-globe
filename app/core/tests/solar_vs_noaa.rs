//! Solar validation (PRD §8.4 "Sunrise and sunset against NOAA to ±1 min"): `snow_globe_core::solar`
//! over oculus-core against the independent NOAA implementation in `tests/common`.
//!
//! 25 dates × all 8 viewpoints, including the 1 Nov 2026 and 14 Mar 2027 DST days and the
//! Arizona viewpoint (no DST). Pass rules: sunrise and sunset within ±60 s; geometric altitude
//! within 0.05° at every hour of each local day. Other thresholds (−6°, −5°, −4°, +2°, +6°) are
//! checked to the same ±60 s and reported separately. Run with `--nocapture` to see the table.

mod common;

use chrono::{Duration, NaiveDate, Utc};
use common::{Stats, noaa};
use snow_globe_core::places::PlaceTable;
use snow_globe_core::solar::{Direction, find_crossing, geometric_altitude_deg, local_day_bounds};

const DATES: [&str; 25] = [
    "2026-06-21",
    "2026-10-31",
    "2026-11-01",
    "2026-11-02",
    "2026-11-19",
    "2026-11-20",
    "2026-12-21",
    "2027-01-01",
    "2027-01-15",
    "2027-02-14",
    "2027-03-13",
    "2027-03-14",
    "2027-03-15",
    "2027-03-20",
    "2027-04-15",
    "2027-05-20",
    "2027-06-21",
    "2027-07-04",
    "2027-08-15",
    "2027-09-23",
    "2027-10-15",
    "2027-11-07",
    "2028-02-29",
    "2030-06-15",
    "2035-12-01",
];

#[test]
fn solar_events_and_altitude_match_noaa() {
    let table = PlaceTable::embedded();
    assert_eq!(table.viewpoints().len(), 8);
    let mut sunrise = Stats::default();
    let mut sunset = Stats::default();
    let mut others = Stats::default();
    let mut altitude = Stats::default();
    let mut worst_event = (0.0f64, String::new());
    let mut worst_alt = (0.0f64, String::new());
    let mut dst_days_checked = 0;
    let mut arizona_days_checked = 0;
    for vp in table.viewpoints() {
        let site = vp.site;
        for date_s in DATES {
            let date = NaiveDate::parse_from_str(date_s, "%Y-%m-%d").expect("date");
            if matches!(date_s, "2026-11-01" | "2027-03-14") {
                dst_days_checked += 1;
            }
            if vp.tz == chrono_tz::America::Phoenix {
                arizona_days_checked += 1;
            }
            let (start, end) = local_day_bounds(vp.tz, date);
            for (thr, dir) in [
                (-0.833, Direction::Rising),
                (-0.833, Direction::Setting),
                (-6.0, Direction::Rising),
                (-6.0, Direction::Setting),
                (-5.0, Direction::Rising),
                (-5.0, Direction::Setting),
                (-4.0, Direction::Rising),
                (-4.0, Direction::Setting),
                (2.0, Direction::Rising),
                (2.0, Direction::Setting),
                (6.0, Direction::Rising),
                (6.0, Direction::Setting),
            ] {
                let ours = find_crossing(&site, thr, dir, start, end)
                    .unwrap_or_else(|| panic!("{} {date_s}: no {thr}° {dir:?} crossing", vp.id));
                let theirs = noaa::event_utc(
                    date,
                    site.lat_deg,
                    site.lon_deg,
                    thr,
                    dir == Direction::Rising,
                )
                .unwrap_or_else(|| panic!("{} {date_s}: NOAA has no {thr}° crossing", vp.id));
                assert_eq!(
                    theirs.with_timezone(&vp.tz).date_naive(),
                    date,
                    "{} {date_s} {thr}° {dir:?}: NOAA event is on another local day",
                    vp.id
                );
                let diff_s = (ours - theirs).num_milliseconds() as f64 / 1000.0;
                let label = format!("{} {date_s} {thr:+}° {dir:?}", vp.id);
                if diff_s.abs() > worst_event.0 {
                    worst_event = (diff_s.abs(), label.clone());
                }
                match (thr, dir) {
                    (-0.833, Direction::Rising) => sunrise.push(diff_s),
                    (-0.833, Direction::Setting) => sunset.push(diff_s),
                    _ => others.push(diff_s),
                }
                assert!(
                    diff_s.abs() <= 60.0,
                    "{label}: snow-globe-core {ours} vs NOAA {theirs} ({diff_s:.1} s)"
                );
            }
            let hours = (end - start).num_hours();
            for h in 0..hours {
                let t = start + Duration::hours(h);
                let d = geometric_altitude_deg(&site, t)
                    - noaa::elevation_deg(t, site.lat_deg, site.lon_deg);
                if d.abs() > worst_alt.0 {
                    worst_alt = (d.abs(), format!("{} {}", vp.id, t.with_timezone(&Utc)));
                }
                altitude.push(d);
                assert!(
                    d.abs() <= 0.05,
                    "{} at {t}: altitude differs by {d:.4}°",
                    vp.id
                );
            }
        }
    }
    assert!(DATES.len() >= 20);
    assert_eq!(dst_days_checked, 16, "both DST days at all 8 viewpoints");
    assert_eq!(arizona_days_checked, DATES.len());
    println!(
        "solar validation vs independent NOAA algorithm: {} viewpoints x {} dates",
        table.viewpoints().len(),
        DATES.len()
    );
    for (name, s, unit) in [
        ("sunrise", &sunrise, "s"),
        ("sunset", &sunset, "s"),
        ("other thresholds", &others, "s"),
        ("altitude", &altitude, "deg"),
    ] {
        println!(
            "  {name:<17} n={:<5} max|d|={:.4} {unit}  mean|d|={:.4} {unit}  rms={:.4} {unit}  p95|d|={:.4} {unit}  mean(d)={:+.4} {unit}",
            s.n(),
            s.max_abs(),
            s.mean_abs(),
            s.rms(),
            s.p95_abs(),
            s.mean()
        );
    }
    println!("  worst event: {:.2} s at {}", worst_event.0, worst_event.1);
    println!(
        "  worst altitude: {:.4} deg at {}",
        worst_alt.0, worst_alt.1
    );
}

#[test]
fn noaa_reference_passes_almanac_sanity_checks() {
    // Checks of the reference itself, so a broken reference cannot make the comparison pass:
    // the June-solstice declination is +23.44°, and the equation of time peaks near +16.4 min
    // in early November and bottoms near −14.2 min in mid-February.
    let at = |y, m, d| {
        NaiveDate::from_ymd_opt(y, m, d)
            .expect("date")
            .and_hms_opt(12, 0, 0)
            .expect("time")
            .and_utc()
    };
    let jc = |t| noaa::julian_century(noaa::julian_day(t));
    let (decl, _) = noaa::sun_coords(jc(at(2027, 6, 21)));
    assert!((decl - 23.44).abs() < 0.01, "solstice declination {decl}");
    let (_, eot_nov) = noaa::sun_coords(jc(at(2026, 11, 3)));
    assert!(
        (eot_nov - 16.4).abs() < 0.15,
        "equation of time 3 Nov {eot_nov}"
    );
    let (_, eot_feb) = noaa::sun_coords(jc(at(2027, 2, 11)));
    assert!(
        (eot_feb + 14.2).abs() < 0.15,
        "equation of time 11 Feb {eot_feb}"
    );
    // Elevation at local solar noon equals 90 − |lat − decl|, with decl taken at that noon.
    let (_, eot) = noaa::sun_coords(jc(at(2026, 11, 19)));
    let noon = at(2026, 11, 19) - Duration::hours(12)
        + Duration::milliseconds(((720.0 + 4.0 * 121.74 - eot) * 60_000.0) as i64);
    let (decl, _) = noaa::sun_coords(jc(noon));
    let el = noaa::elevation_deg(noon, 46.78, -121.74);
    assert!(
        (el - (90.0 - (46.78 - decl))).abs() < 0.02,
        "noon elevation {el}"
    );
}
