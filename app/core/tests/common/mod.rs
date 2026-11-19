//! Shared test helpers.
//!
//! [`noaa`] is an **independent** implementation of NOAA's solar calculator: the formulas of the
//! NOAA Global Monitoring Laboratory "Solar Calculator" spreadsheet (after Meeus, *Astronomical
//! Algorithms*), written from those formulas for these tests. It shares no code with
//! `lookout_core::solar` or `oculus-core`, so agreement between them is evidence, not a tautology.
//!
//! [`almanac_moon`] is likewise independent: the Astronomical Almanac's low-precision formulae for
//! the Moon (about 0.3° in longitude), made topocentric. It checks the wrapping of oculus-core's
//! Moon (units, site, phase), not the ephemeris itself, which PRD §8.4 measured against Horizons.
#![allow(dead_code)]

pub mod noaa {
    use chrono::{DateTime, Duration, NaiveDate, TimeZone, Timelike, Utc};

    fn sin_d(x: f64) -> f64 {
        x.to_radians().sin()
    }
    fn cos_d(x: f64) -> f64 {
        x.to_radians().cos()
    }
    fn tan_d(x: f64) -> f64 {
        x.to_radians().tan()
    }

    /// Julian day (UT) of an instant.
    pub fn julian_day(t: DateTime<Utc>) -> f64 {
        let secs = t.timestamp() as f64 + f64::from(t.nanosecond()) / 1e9;
        secs / 86400.0 + 2440587.5
    }

    /// Julian centuries since J2000.0 (NOAA uses UT here, without ΔT).
    pub fn julian_century(jd: f64) -> f64 {
        (jd - 2451545.0) / 36525.0
    }

    /// Sun declination (degrees) and equation of time (minutes) — spreadsheet columns I–V.
    pub fn sun_coords(jc: f64) -> (f64, f64) {
        let l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)).rem_euclid(360.0);
        let m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc);
        let e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc);
        let c = sin_d(m) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
            + sin_d(2.0 * m) * (0.019993 - 0.000101 * jc)
            + sin_d(3.0 * m) * 0.000289;
        let true_long = l0 + c;
        let omega = 125.04 - 1934.136 * jc;
        let app_long = true_long - 0.00569 - 0.00478 * sin_d(omega);
        let mean_obliq =
            23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0;
        let obliq = mean_obliq + 0.00256 * cos_d(omega);
        let decl = (sin_d(obliq) * sin_d(app_long)).asin().to_degrees();
        let y = tan_d(obliq / 2.0).powi(2);
        let eot = 4.0
            * (y * sin_d(2.0 * l0) - 2.0 * e * sin_d(m) + 4.0 * e * y * sin_d(m) * cos_d(2.0 * l0)
                - 0.5 * y * y * sin_d(4.0 * l0)
                - 1.25 * e * e * sin_d(2.0 * m))
            .to_degrees();
        (decl, eot)
    }

    /// The Sun's apparent ecliptic longitude (degrees) — spreadsheet columns I–P.
    pub fn sun_apparent_longitude(jc: f64) -> f64 {
        let l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)).rem_euclid(360.0);
        let m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc);
        let c = sin_d(m) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
            + sin_d(2.0 * m) * (0.019993 - 0.000101 * jc)
            + sin_d(3.0 * m) * 0.000289;
        let omega = 125.04 - 1934.136 * jc;
        l0 + c - 0.00569 - 0.00478 * sin_d(omega)
    }

    /// Solar elevation without refraction (degrees) — spreadsheet columns AB–AE.
    pub fn elevation_deg(t: DateTime<Utc>, lat: f64, lon_east: f64) -> f64 {
        let (decl, eot) = sun_coords(julian_century(julian_day(t)));
        let minutes = f64::from(t.hour()) * 60.0
            + f64::from(t.minute())
            + (f64::from(t.second()) + f64::from(t.nanosecond()) / 1e9) / 60.0;
        let tst = (minutes + eot + 4.0 * lon_east).rem_euclid(1440.0);
        let ha = if tst / 4.0 < 0.0 {
            tst / 4.0 + 180.0
        } else {
            tst / 4.0 - 180.0
        };
        let cos_zenith = sin_d(lat) * sin_d(decl) + cos_d(lat) * cos_d(decl) * cos_d(ha);
        90.0 - cos_zenith.clamp(-1.0, 1.0).acos().to_degrees()
    }

    /// When the Sun's centre crosses `alt_deg` (rising or setting) on the day whose solar noon
    /// is nearest `date` 12:00 UT − 4·lon minutes: for every US site that is the local date.
    ///
    /// NOAA's hour-angle formula, evaluated (as the NOAA web calculator does) with the Sun's
    /// coordinates at the estimated event time, iterated to convergence.
    pub fn event_utc(
        date: NaiveDate,
        lat: f64,
        lon_east: f64,
        alt_deg: f64,
        rising: bool,
    ) -> Option<DateTime<Utc>> {
        let base = Utc.from_utc_datetime(&date.and_hms_opt(0, 0, 0)?);
        let jd0 = julian_day(base);
        let mut minutes = 720.0 - 4.0 * lon_east;
        for _ in 0..6 {
            let (decl, eot) = sun_coords(julian_century(jd0 + minutes / 1440.0));
            let cos_ha = (sin_d(alt_deg) - sin_d(lat) * sin_d(decl)) / (cos_d(lat) * cos_d(decl));
            if !(-1.0..=1.0).contains(&cos_ha) {
                return None;
            }
            let ha = cos_ha.acos().to_degrees();
            let noon = 720.0 - 4.0 * lon_east - eot;
            minutes = if rising {
                noon - 4.0 * ha
            } else {
                noon + 4.0 * ha
            };
        }
        Some(base + Duration::milliseconds((minutes * 60_000.0).round() as i64))
    }
}

/// The Moon from the Astronomical Almanac's low-precision formulae (section D, "Low-precision
/// formulas for the Moon's coordinates", valid 1950–2050: about 0.3° in ecliptic longitude, 0.2°
/// in latitude, 0.003° in horizontal parallax), with the observer's geocentric position from
/// Meeus ch. 11 for the topocentric place.
pub mod almanac_moon {
    use super::noaa;
    use chrono::{DateTime, Utc};

    fn sin_d(x: f64) -> f64 {
        x.to_radians().sin()
    }
    fn cos_d(x: f64) -> f64 {
        x.to_radians().cos()
    }

    /// Topocentric place and phase of the Moon.
    #[derive(Debug, Clone, Copy)]
    pub struct MoonPlace {
        /// Altitude of the centre, no refraction, degrees.
        pub alt_deg: f64,
        /// Azimuth from north through east, degrees.
        pub az_deg: f64,
        /// Illuminated fraction, from the topocentric elongation from the Sun.
        pub illuminated: f64,
    }

    /// The Moon at `t` from a site (geodetic latitude, east longitude, height).
    pub fn moon(t: DateTime<Utc>, lat: f64, lon_east: f64, height_m: f64) -> MoonPlace {
        let jd = noaa::julian_day(t);
        let tc = (jd - 2451545.0) / 36525.0;
        let lambda = 218.32 + 481267.881 * tc + 6.29 * sin_d(135.0 + 477198.87 * tc)
            - 1.27 * sin_d(259.3 - 413335.36 * tc)
            + 0.66 * sin_d(235.7 + 890534.22 * tc)
            + 0.21 * sin_d(269.9 + 954397.74 * tc)
            - 0.19 * sin_d(357.5 + 35999.05 * tc)
            - 0.11 * sin_d(186.5 + 966404.03 * tc);
        let beta = 5.13 * sin_d(93.3 + 483202.02 * tc) + 0.28 * sin_d(228.2 + 960400.89 * tc)
            - 0.28 * sin_d(318.3 + 6003.15 * tc)
            - 0.17 * sin_d(217.6 - 407332.21 * tc);
        let parallax = 0.9508
            + 0.0518 * cos_d(135.0 + 477198.87 * tc)
            + 0.0095 * cos_d(259.3 - 413335.36 * tc)
            + 0.0078 * cos_d(235.7 + 890534.22 * tc)
            + 0.0028 * cos_d(269.9 + 954397.74 * tc);
        let r = 1.0 / sin_d(parallax); // Earth radii
        let eps = 23.439291 - 0.0130042 * tc;
        // Geocentric equatorial rectangular coordinates, Earth radii.
        let x = r * cos_d(beta) * cos_d(lambda);
        let y = r * (cos_d(eps) * cos_d(beta) * sin_d(lambda) - sin_d(eps) * sin_d(beta));
        let z = r * (sin_d(eps) * cos_d(beta) * sin_d(lambda) + cos_d(eps) * sin_d(beta));
        // The observer, Earth radii (Meeus ch. 11), at the local mean sidereal time.
        let lst = (280.46061837 + 360.98564736629 * (jd - 2451545.0) + lon_east).rem_euclid(360.0);
        let u = (0.99664719 * lat.to_radians().tan()).atan();
        let h = height_m / 6378140.0;
        let rho_sin = 0.99664719 * u.sin() + h * sin_d(lat);
        let rho_cos = u.cos() + h * cos_d(lat);
        let (xt, yt, zt) = (
            x - rho_cos * cos_d(lst),
            y - rho_cos * sin_d(lst),
            z - rho_sin,
        );
        let ra = yt.atan2(xt).to_degrees();
        let dec = zt.atan2((xt * xt + yt * yt).sqrt()).to_degrees();
        let ha = lst - ra;
        let alt = (sin_d(lat) * sin_d(dec) + cos_d(lat) * cos_d(dec) * cos_d(ha))
            .clamp(-1.0, 1.0)
            .asin()
            .to_degrees();
        let az = (-cos_d(dec) * sin_d(ha))
            .atan2(sin_d(dec) * cos_d(lat) - cos_d(dec) * sin_d(lat) * cos_d(ha))
            .to_degrees()
            .rem_euclid(360.0);
        // Phase angle ≈ 180° − elongation (the Sun's distance makes the difference < 0.15°).
        let ls = noaa::sun_apparent_longitude(noaa::julian_century(jd));
        let sun = (cos_d(ls), cos_d(eps) * sin_d(ls), sin_d(eps) * sin_d(ls));
        let norm = (xt * xt + yt * yt + zt * zt).sqrt();
        let cos_elongation = (xt * sun.0 + yt * sun.1 + zt * sun.2) / norm;
        MoonPlace {
            alt_deg: alt,
            az_deg: az,
            illuminated: (1.0 - cos_elongation) / 2.0,
        }
    }
}

/// Simple summary statistics over absolute errors.
#[derive(Debug, Default, Clone)]
pub struct Stats {
    pub values: Vec<f64>,
}

impl Stats {
    pub fn push(&mut self, v: f64) {
        self.values.push(v);
    }
    pub fn n(&self) -> usize {
        self.values.len()
    }
    pub fn max_abs(&self) -> f64 {
        self.values.iter().fold(0.0, |m, v| m.max(v.abs()))
    }
    pub fn mean(&self) -> f64 {
        self.values.iter().sum::<f64>() / self.values.len().max(1) as f64
    }
    pub fn mean_abs(&self) -> f64 {
        self.values.iter().map(|v| v.abs()).sum::<f64>() / self.values.len().max(1) as f64
    }
    pub fn rms(&self) -> f64 {
        (self.values.iter().map(|v| v * v).sum::<f64>() / self.values.len().max(1) as f64).sqrt()
    }
    pub fn p95_abs(&self) -> f64 {
        let mut v: Vec<f64> = self.values.iter().map(|x| x.abs()).collect();
        v.sort_by(f64::total_cmp);
        if v.is_empty() {
            return 0.0;
        }
        v[((v.len() as f64 * 0.95).ceil() as usize).clamp(1, v.len()) - 1]
    }
}
