"""The NOAA solar algorithm against Meeus's worked examples (Astronomical Algorithms, 1991)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from snow_globe_eval.noaa_solar import (
    julian_day,
    noaa_refraction_deg,
    solar_position,
    solar_position_jd,
)


def test_meeus_example_25a_sun_low_accuracy() -> None:
    # 1992 Oct 13.0 TD = JD 2448908.5: apparent longitude 199.90895, RA 198.38083, dec -7.78507
    p = solar_position_jd(2448908.5, 0.0, 0.0)
    assert p.apparent_longitude_deg == pytest.approx(199.90895, abs=5e-5)
    assert p.right_ascension_deg == pytest.approx(198.38083, abs=5e-5)
    assert p.declination_deg == pytest.approx(-7.78507, abs=5e-5)


def test_meeus_example_28b_equation_of_time() -> None:
    # Same date, eq. 28.3 (Smart): E = +3.427351 deg = +13 min 42.7 s
    p = solar_position_jd(2448908.5, 0.0, 0.0)
    assert p.equation_of_time_min == pytest.approx(13.7118, abs=0.005)


def test_julian_day() -> None:
    assert julian_day(datetime(2000, 1, 1, 12, tzinfo=UTC)) == pytest.approx(2451545.0)
    pst = timezone(timedelta(hours=-8))
    assert julian_day(datetime(2000, 1, 1, 4, tzinfo=pst)) == pytest.approx(2451545.0)
    with pytest.raises(ValueError):
        julian_day(datetime(2000, 1, 1, 12))


def test_noon_azimuth_and_altitude() -> None:
    # At solar noon on the equinox at 40 N the Sun is due south at about 50 deg.
    t = datetime(2026, 3, 20, 12, 7, tzinfo=UTC)  # equation of time ~ -7 min at lon 0
    p = solar_position(t, 40.0, 0.0)
    assert p.elevation_deg == pytest.approx(50.0, abs=0.3)
    assert p.azimuth_deg == pytest.approx(180.0, abs=1.0)


def test_refraction_piecewise_branches() -> None:
    assert noaa_refraction_deg(89.0) == 0.0
    assert 0.0 < noaa_refraction_deg(20.0) < noaa_refraction_deg(5.5)
    assert noaa_refraction_deg(2.0) > noaa_refraction_deg(5.5)
    assert noaa_refraction_deg(-5.0) > 0.0  # small positive term far below the horizon


def test_latitude_range_is_checked() -> None:
    with pytest.raises(ValueError):
        solar_position_jd(2451545.0, 91.0, 0.0)
