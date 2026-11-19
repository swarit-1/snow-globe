"""Tests for the ephemeris wrapper, refraction and the NOAA fallback (PRD §8.4)."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest
from conftest import HAS_OCULUS, needs_oculus

from lookout_eval import ephemeris, noaa_solar
from lookout_eval.ephemeris import (
    EphemerisUnavailableError,
    NoaaBackend,
    apparent_altitude_deg,
    get_ephemeris,
    moon_position,
    refraction_deg,
    sun_position,
    sun_rate_deg_per_min,
)

POINT_REYES = (37.99604, -123.02, 150.0)


def test_saemundsson_reference_values() -> None:
    # Meeus eq. 16.4 by hand: R(0) = 1.02 / tan(10.3 / 5.11 deg) + 0.0019279 arcmin
    expected_0 = (1.02 / math.tan(math.radians(10.3 / 5.11)) + 0.0019279) / 60
    assert refraction_deg(0.0) == pytest.approx(expected_0, rel=1e-12)
    assert refraction_deg(0.0) == pytest.approx(0.4831, abs=1e-4)  # 28.98 arcmin
    assert refraction_deg(10.0) == pytest.approx(0.0902, abs=1e-4)  # 5.41 arcmin
    assert refraction_deg(90.0) == 0.0
    assert refraction_deg(89.99) >= 0.0
    assert refraction_deg(-1.0) > 0.6  # still applied at -1 deg
    assert refraction_deg(-1.01) == 0.0  # cut off below -1 deg
    # pressure and temperature scaling
    assert refraction_deg(5.0, pressure_hpa=505.0) == pytest.approx(refraction_deg(5.0) / 2)
    assert refraction_deg(5.0, temperature_c=10.0) == pytest.approx(refraction_deg(5.0))
    assert refraction_deg(5.0, model="none") == 0.0
    with pytest.raises(ValueError):
        refraction_deg(5.0, model="bennett")  # type: ignore[arg-type]


def test_noaa_refraction_reference_values() -> None:
    t = math.tan(math.radians(10.0))
    expected = (58.1 / t - 0.07 / t**3 + 0.000086 / t**5) / 3600
    assert refraction_deg(10.0, model="noaa") == pytest.approx(expected)
    assert refraction_deg(10.0, model="noaa") == pytest.approx(0.0881, abs=1e-4)
    assert refraction_deg(86.0, model="noaa") == 0.0
    assert refraction_deg(0.0, model="noaa") == pytest.approx(1735 / 3600)
    # below -0.575 deg the spreadsheet keeps a small positive term
    assert refraction_deg(-10.0, model="noaa") == pytest.approx(0.0327, abs=2e-4)


def test_apparent_altitude_adds_refraction() -> None:
    assert apparent_altitude_deg(3.0) == pytest.approx(3.0 + refraction_deg(3.0))
    assert apparent_altitude_deg(-20.0) == -20.0


def test_noaa_backend_has_no_moon() -> None:
    backend = get_ephemeris("noaa")
    assert isinstance(backend, NoaaBackend)
    with pytest.raises(EphemerisUnavailableError, match="Sun only"):
        moon_position(datetime(2026, 11, 19, tzinfo=UTC), 0, 0, ephemeris=backend)


def test_env_var_selects_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ephemeris.ENV_BACKEND, "noaa")
    assert get_ephemeris().name == "noaa-gml-spreadsheet"
    monkeypatch.setenv(ephemeris.ENV_BACKEND, "astrology")
    with pytest.raises(ValueError, match="unknown ephemeris backend"):
        get_ephemeris()


def test_missing_oculus_raises_with_build_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise ImportError("no module named oculus_core")

    monkeypatch.setattr(ephemeris, "_import_oculus", broken)
    monkeypatch.delenv(ephemeris.ENV_BACKEND, raising=False)
    with pytest.raises(EphemerisUnavailableError, match="uv sync"):
        get_ephemeris()


def test_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        sun_position(datetime(2026, 11, 19, 12), 0.0, 0.0, ephemeris=NoaaBackend())


def test_noaa_backend_sun_position_fields() -> None:
    t = datetime(2026, 11, 17, 20, 0, tzinfo=UTC)  # about local noon at Point Reyes
    p = sun_position(t, *POINT_REYES, ephemeris=NoaaBackend())
    assert 30.0 < p.altitude_deg < 35.0
    assert 170.0 < p.azimuth_deg < 190.0
    assert p.apparent_altitude_deg == pytest.approx(p.altitude_deg + refraction_deg(p.altitude_deg))
    assert p.altitude(refracted=True) == p.apparent_altitude_deg
    assert p.backend == "noaa-gml-spreadsheet" and p.refraction_model == "saemundsson"


@needs_oculus
def test_default_backend_is_oculus_and_reports_pinned_rev() -> None:
    backend = get_ephemeris()
    assert backend.name == "oculus-core"
    assert backend.version.endswith("+06809e6")


@needs_oculus
def test_oculus_and_noaa_agree_on_random_samples() -> None:
    """Two independent implementations: altitudes within 0.02 deg (Sun above -5 deg)."""
    rng = random.Random(20261119)
    oculus, noaa = get_ephemeris("oculus"), get_ephemeris("noaa")
    worst_alt = worst_az = 0.0
    checked = 0
    for _ in range(600):
        t = datetime(2021, 1, 1, tzinfo=UTC) + timedelta(seconds=rng.uniform(0, 8 * 3.156e7))
        lat, lon = rng.uniform(-60, 70), rng.uniform(-180, 180)
        alt_o, az_o = oculus.sun_alt_az(t, lat, lon)
        if alt_o < -5:
            continue
        alt_n, az_n = noaa.sun_alt_az(t, lat, lon)
        worst_alt = max(worst_alt, abs(alt_o - alt_n))
        if alt_o < 80:
            daz = (az_o - az_n + 180) % 360 - 180
            worst_az = max(worst_az, abs(daz) * math.cos(math.radians(alt_o)))
        checked += 1
    assert checked > 200
    assert worst_alt < 0.02
    assert worst_az < 0.02


@needs_oculus
def test_oculus_moon_fields_are_sane() -> None:
    m = moon_position(datetime(2026, 11, 19, 6, tzinfo=UTC), *POINT_REYES)
    assert -90 <= m.altitude_deg <= 90 and 0 <= m.azimuth_deg < 360
    assert 0.0 <= m.illuminated_fraction <= 1.0
    assert m.backend == "oculus-core"


def test_sun_rate_sign_morning_and_evening() -> None:
    noaa = NoaaBackend()
    morning = datetime(2026, 11, 17, 16, 0, tzinfo=UTC)  # 08:00 PST, rising
    evening = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)  # 16:00 PST, setting
    assert sun_rate_deg_per_min(morning, *POINT_REYES, ephemeris=noaa) > 0.1
    assert sun_rate_deg_per_min(evening, *POINT_REYES, ephemeris=noaa) < -0.1


def test_backend_choice_does_not_fall_back_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    if HAS_OCULUS:
        assert get_ephemeris("oculus").name == "oculus-core"
    assert noaa_solar.julian_day(datetime(2000, 1, 1, 12, tzinfo=UTC)) == pytest.approx(2451545.0)
