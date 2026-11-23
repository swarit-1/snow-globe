"""Star directions (plates.stars): sidereal time, precession, refraction, altitude geometry."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from snow_globe_plates.stars import (
    J2000_JD,
    StarObservation,
    apparent_alt_az,
    gmst_deg,
    julian_date,
    precess_j2000,
    refraction_deg,
    star_directions,
)


def test_julian_date_and_gmst_at_j2000() -> None:
    t = datetime(2000, 1, 1, 12, tzinfo=UTC)
    assert julian_date(t) == pytest.approx(J2000_JD)
    # Meeus eq. 12.4 at J2000.0: 280.46061837 deg (18h 41m 50.55s).
    assert gmst_deg(J2000_JD) == pytest.approx(280.46061837, abs=1e-9)
    # Meeus example 12.a: 1987 Apr 10, 0h UT -> 13h 10m 46.3668s = 197.693195 deg.
    assert gmst_deg(2446895.5) == pytest.approx(197.693195, abs=1e-5)
    with pytest.raises(ValueError):
        julian_date(datetime(2000, 1, 1))


def test_precession_is_identity_at_j2000_and_moves_ra_by_the_known_rate() -> None:
    ra, dec = precess_j2000([10.0, 200.0], [20.0, -30.0], J2000_JD)
    assert np.allclose(ra, [10.0, 200.0], atol=1e-9) and np.allclose(dec, [20.0, -30.0], atol=1e-9)
    # A star at RA 0h, Dec 0 gains about m = 3.07 s of RA a year: 26.9 years -> 0.344 deg.
    ra2, dec2 = precess_j2000([0.0], [0.0], J2000_JD + 26.9 * 365.25)
    assert float(ra2[0]) == pytest.approx(0.344, abs=0.002)
    assert float(dec2[0]) == pytest.approx(26.9 * 20.04 / 3600.0, abs=0.002)


def test_refraction_values() -> None:
    r = refraction_deg([0.0, 10.0, 45.0, 90.0], pressure_hpa=1010.0, temperature_c=10.0)
    assert float(r[0]) * 60 == pytest.approx(29.0, abs=1.0)  # about half a degree at the horizon
    assert float(r[1]) * 60 == pytest.approx(5.3, abs=0.2)
    assert float(r[2]) * 60 == pytest.approx(1.0, abs=0.05)
    assert float(r[3]) == pytest.approx(0.0, abs=1e-3)
    assert float(refraction_deg([-5.0])[0]) == 0.0


def test_pole_star_altitude_is_the_latitude() -> None:
    # A star at the J2000 pole stays within the precession drift (0.37 deg by 2026) of the pole.
    t = datetime(2026, 11, 20, 12, tzinfo=UTC)
    az, alt = apparent_alt_az([0.0], [90.0], t, 46.78, -121.74, pressure_hpa=0.0)
    assert float(alt[0]) == pytest.approx(46.78, abs=0.4)
    assert min(float(az[0]), 360.0 - float(az[0])) < 1.0


def test_meridian_transit_gives_due_south_or_north() -> None:
    t = datetime(2026, 11, 20, 6, tzinfo=UTC)
    jd = julian_date(t)
    lst = (gmst_deg(jd) - 121.74) % 360.0
    # Stars precessed to date and placed exactly on the meridian.
    for dec, want_az, want_alt in ((10.0, 180.0, 90 - 46.78 + 10), (70.0, 0.0, 90 - (70 - 46.78))):
        ra0, dec0 = _j2000_for(lst, dec, jd)
        az, alt = apparent_alt_az([ra0], [dec0], t, 46.78, -121.74, pressure_hpa=0.0)
        assert float(az[0]) % 360 == pytest.approx(want_az, abs=0.01)
        assert float(alt[0]) == pytest.approx(want_alt, abs=0.01)


def _j2000_for(ra_date: float, dec_date: float, jd: float) -> tuple[float, float]:
    """Invert the precession by fixed-point iteration (test helper)."""
    ra, dec = ra_date, dec_date
    for _ in range(20):
        r, d = precess_j2000([ra], [dec], jd)
        ra += ra_date - float(r[0])
        dec += dec_date - float(d[0])
    return ra, dec


def test_star_directions_are_unit_vectors() -> None:
    t = datetime(2026, 11, 20, 12, tzinfo=UTC)
    stars = [
        StarObservation(300.0, 60.0, 3.0, t, (0.0, 0.0)),
        StarObservation(250.0, 50.0, 2.0, t, (1.0, 1.0)),
    ]
    d = star_directions(stars, 46.78, -121.74)
    assert d.shape == (2, 3) and np.allclose(np.linalg.norm(d, axis=1), 1.0)
