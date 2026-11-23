//! Tier 0's caption, the honesty layer (PRD §5.3, §5.5, §15.4).
//!
//! It says three things: this is a **real photo**, **when** it was taken (date, clock and zone,
//! in the camera's zone, PRD §5.5 "Always print the time zone and the date"), and **what it was
//! matched on**. The PRD's form is
//!
//! ```text
//! REAL PHOTO, CLOSEST MATCH · taken 14 Oct, 5:45 PM PDT · matched on Sun position and forecast class
//! ```
//!
//! Until the weather class exists (TODO(M1.5)) the match is on Sun position and time of year,
//! and the caption says exactly that. The credit follows the manifest's attribution
//! ("Courtesy National Park Service", as on the LIVE caption). The second line, which folds away
//! in the overlay, gives the photo's Sun against the asked one and how far apart the dates are;
//! an infrared night frame says so.

use chrono::{DateTime, Datelike, Utc};
use serde::Serialize;

use crate::tier0::camera::CameraInfo;
use crate::tier0::library::{ColourMode, Entry, EntrySource};
use crate::tier0::matcher::{Descriptor, MatchTerm, Scored};

/// The two caption lines.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Caption {
    /// Always shown.
    pub line1: String,
    /// Shown on hover.
    pub line2: String,
}

/// "Sat 21 Nov, 3:52 PM PST", with the year when it is not `ref_year`.
pub fn taken_text(t: DateTime<Utc>, camera: &CameraInfo, ref_year: i32) -> String {
    let local = t.with_timezone(&camera.tz);
    if local.year() == ref_year {
        local.format("%a %-d %b, %-I:%M %p %Z").to_string()
    } else {
        local.format("%a %-d %b %Y, %-I:%M %p %Z").to_string()
    }
}

/// The words for what a match used.
pub fn matched_on_text(terms: &[MatchTerm], weather_class_used: bool) -> String {
    let sun = if terms.contains(&MatchTerm::SunAzimuth) {
        "Sun position"
    } else {
        "Sun altitude"
    };
    let mut parts = vec![sun.to_string()];
    if terms.contains(&MatchTerm::DayOfYear) {
        parts.push("time of year".to_string());
    }
    if weather_class_used {
        parts.push("forecast class".to_string());
    }
    match parts.len() {
        1 => parts.remove(0),
        _ => {
            let last = parts.pop().unwrap_or_default();
            format!("{} and {last}", parts.join(", "))
        }
    }
}

fn days_text(days: f64) -> String {
    let n = days.abs();
    let when = if days < 0.0 { "earlier" } else { "later" };
    if n < 0.5 {
        "same time of year".to_string()
    } else if n < 1.5 {
        format!("1 day {when} in the year")
    } else {
        format!("{n:.0} days {when} in the year")
    }
}

/// The caption of a Tier 0 frame (module docs). `valid_time` is the asked time.
pub fn tier0_caption(
    entry: &Entry,
    camera: &CameraInfo,
    scored: &Scored,
    query: &Descriptor,
    matched_on: &[MatchTerm],
    weather_class_used: bool,
    valid_time: DateTime<Utc>,
) -> Caption {
    let ref_year = valid_time.with_timezone(&camera.tz).year();
    let source = match entry.source {
        EntrySource::Live => "",
        EntrySource::NpsArchive => " (NPS archive)",
    };
    let line1 = format!(
        "REAL PHOTO, CLOSEST MATCH · taken {}{source} · matched on {} · {}",
        taken_text(entry.taken_utc, camera, ref_year),
        matched_on_text(matched_on, weather_class_used),
        camera.credit()
    );
    let mut line2 = vec![
        camera.display_name(),
        format!(
            "Sun {:.1}° in the photo, {:.1}° at the asked time",
            entry.sun_altitude_deg, query.sun_altitude_deg
        ),
        days_text(scored.delta.days_of_year),
    ];
    if entry.colour_mode == ColourMode::Monochrome {
        line2.push("infrared night frame".to_string());
    }
    if !weather_class_used {
        line2.push("weather not matched".to_string());
    }
    Caption {
        line1,
        line2: line2.join(" · "),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tier0::camera::CameraTable;
    use chrono::TimeZone;

    #[test]
    fn times_carry_the_zone_and_the_date() {
        let c = CameraTable::embedded()
            .get("rainier.paradise-mountain")
            .expect("camera");
        let t = Utc
            .with_ymd_and_hms(2026, 11, 21, 23, 52, 0)
            .single()
            .expect("t");
        assert_eq!(taken_text(t, c, 2026), "Sat 21 Nov, 3:52 PM PST");
        let summer = Utc
            .with_ymd_and_hms(2025, 7, 15, 3, 5, 0)
            .single()
            .expect("t");
        assert_eq!(taken_text(summer, c, 2026), "Mon 14 Jul 2025, 8:05 PM PDT");
        let az = CameraTable::embedded()
            .get("grand-canyon.yavapai")
            .expect("camera");
        assert_eq!(taken_text(t, az, 2026), "Sat 21 Nov, 4:52 PM MST");
    }

    #[test]
    fn matched_on_says_what_was_used() {
        use MatchTerm::*;
        assert_eq!(
            matched_on_text(&[SunAltitude, SunAzimuth, DayOfYear], false),
            "Sun position and time of year"
        );
        assert_eq!(
            matched_on_text(&[SunAltitude, DayOfYear], false),
            "Sun altitude and time of year"
        );
        assert_eq!(
            matched_on_text(&[SunAltitude, SunAzimuth, DayOfYear], true),
            "Sun position, time of year and forecast class"
        );
        assert_eq!(days_text(-2.4), "2 days earlier in the year");
        assert_eq!(days_text(0.2), "same time of year");
        assert_eq!(days_text(1.2), "1 day later in the year");
    }
}
