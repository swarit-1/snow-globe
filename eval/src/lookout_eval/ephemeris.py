"""Sun and Moon positions for evaluation, from oculus-core (PRD §8.4).

The app and evaluation run the same ephemeris: oculus-core pinned at
``1b5410b01f127514fa37656eff2b7d73be7ea8da`` (D-025), built with maturin into this venv (the
``oculus`` dependency group, crate ``core/``, feature ``python``). Its ``sun_moon`` returns
the *apparent* topocentric position (aberration, nutation, parallax) **without refraction**:
that is the geometric altitude here.

**Provenance.** A record names the build that actually ran: :attr:`OculusBackend.version` is
the crate version plus the commit the installed ``oculus-core-py`` was built from, read from
its PEP 610 ``direct_url.json`` (``vcs_info.commit_id``, which uv writes for a git
dependency), for example ``0.1.0+1b5410b``, once the imported ``oculus_core`` is checked to be
that distribution's own file. A build with no such commit (a local ``maturin develop``, a wheel
from elsewhere, a module imported from another folder) falls back to the pin, marked as
unverified: ``0.1.0+1b5410b.pinned``. :data:`OCULUS_PINNED_REV` is the pin in ``pyproject.toml`` and
``uv.lock``, and a test keeps the three equal. (Until 20 Nov the suffix was this constant
alone, which still said ``06809e6`` after the pin moved, D-025.)

**Refraction** is added on top, because oculus-core does not apply it (PRD §8.4
"Additions"):

- ``"saemundsson"`` (default): Meeus, *Astronomical Algorithms* eq. 16.4 (Sæmundsson 1986),
  ``R = 1.02 / tan(h + 10.3 / (h + 5.11))`` arcminutes at 1010 hPa and 10 °C, scaled by
  ``(P / 1010) · (283 / (273 + T))``. Applied for a geometric altitude of −1° and above;
  below that the Sun is well under the horizon and the apparent altitude is the geometric
  one.
- ``"noaa"``: the NOAA spreadsheet's piecewise model (:mod:`.noaa_solar`), kept so the NPS
  archive's ``SolarElevationAngle`` can be compared like for like.
- ``"none"``.

**Backends.** ``LOOKOUT_EPHEMERIS`` picks ``oculus`` (default) or ``noaa``. There is no
silent fallback: if oculus-core is not importable, :func:`get_ephemeris` raises
:class:`EphemerisUnavailableError` with the build command, unless ``noaa`` was asked for.
The NOAA backend has no Moon. Every result names its backend, so a pair record shows what
produced it.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from importlib import metadata
from typing import Any, Literal, Protocol

from . import noaa_solar

__all__ = [
    "ENV_BACKEND",
    "OCULUS_DISTRIBUTION",
    "OCULUS_PINNED_REV",
    "EphemerisBackend",
    "EphemerisUnavailableError",
    "MoonPosition",
    "NoaaBackend",
    "OculusBackend",
    "RefractionModel",
    "SunPosition",
    "apparent_altitude_deg",
    "get_ephemeris",
    "installed_oculus_rev",
    "moon_position",
    "oculus_rev",
    "refraction_deg",
    "sun_position",
    "sun_rate_deg_per_min",
]

ENV_BACKEND = "LOOKOUT_EPHEMERIS"
#: The pin of the ``oculus`` group in pyproject.toml and uv.lock (D-025); a test keeps them equal.
OCULUS_PINNED_REV = "1b5410b01f127514fa37656eff2b7d73be7ea8da"
#: The distribution that provides the ``oculus_core`` module.
OCULUS_DISTRIBUTION = "oculus-core-py"
REFRACTION_MIN_ALT_DEG = -1.0
_COMMIT = re.compile(r"[0-9a-f]{40}")

type RefractionModel = Literal["saemundsson", "noaa", "none"]

_BUILD_HINT = (
    "oculus-core is not importable. Build it into this venv with `uv sync` (the default "
    "'oculus' dependency group; needs a Rust toolchain and access to oculus rev "
    f"{OCULUS_PINNED_REV[:7]}), or set {ENV_BACKEND}=noaa for the independent NOAA "
    "solar algorithm (Sun only)."
)


class EphemerisUnavailableError(RuntimeError):
    """The requested ephemeris backend (or body) is not available."""


def _provides(dist: Any, module_file: str) -> bool:
    """Whether ``module_file`` (an imported module's ``__file__``) is one of ``dist``'s files."""
    target = os.path.realpath(module_file)
    for item in dist.files or ():
        if str(item).replace("\\", "/").endswith("oculus_core/__init__.py"):
            return os.path.realpath(str(dist.locate_file(item))) == target
    return False


def installed_oculus_rev(
    module_file: str | None = None, distribution: str = OCULUS_DISTRIBUTION
) -> str | None:
    """The git commit the installed oculus-core bindings were built from, or None.

    Read from the distribution's PEP 610 ``direct_url.json`` (``vcs_info.commit_id``). None
    when the distribution is not installed, has no ``direct_url.json``, or was not installed
    from git; a value that is not a full 40-hex commit is ignored. With ``module_file`` (the
    imported ``oculus_core.__file__``), also None unless that file is the distribution's own,
    so a build imported from elsewhere is never given this distribution's commit.
    """
    try:
        dist = metadata.distribution(distribution)
    except metadata.PackageNotFoundError:
        return None
    text = dist.read_text("direct_url.json")
    if not text or (module_file is not None and not _provides(dist, module_file)):
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    vcs = doc.get("vcs_info") if isinstance(doc, dict) else None
    commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
    return commit if isinstance(commit, str) and _COMMIT.fullmatch(commit) else None


def oculus_rev(module_file: str | None = None) -> tuple[str, bool]:
    """The oculus-core commit to name in records, and whether the installed build said so.

    ``(installed commit, True)`` when :func:`installed_oculus_rev` finds one, else
    ``(OCULUS_PINNED_REV, False)``: the pin, not verified against the build.
    """
    installed = installed_oculus_rev(module_file)
    return (installed, True) if installed is not None else (OCULUS_PINNED_REV, False)


def _utc_ms(moment: datetime) -> float:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("ephemeris needs a timezone-aware datetime")
    return moment.timestamp() * 1000.0


def refraction_deg(
    geometric_alt_deg: float,
    *,
    model: RefractionModel = "saemundsson",
    pressure_hpa: float = 1010.0,
    temperature_c: float = 10.0,
) -> float:
    """Atmospheric refraction (degrees, ≥ 0) to add to a geometric altitude.

    See the module docstring for the models. Pressure and temperature apply to
    ``"saemundsson"`` only; the NOAA model has none.
    """
    if model == "none":
        return 0.0
    if model == "noaa":
        return noaa_solar.noaa_refraction_deg(geometric_alt_deg)
    if model != "saemundsson":
        raise ValueError(f"unknown refraction model {model!r}")
    h = geometric_alt_deg
    if h < REFRACTION_MIN_ALT_DEG:
        return 0.0
    if h >= 90.0:
        return 0.0
    arcmin = 1.02 / math.tan(math.radians(h + 10.3 / (h + 5.11)))
    # Meeus adds 0.0019279' so that R is exactly 0 at the zenith; keep it non-negative.
    arcmin = max(0.0, arcmin + 0.0019279)
    scale = (pressure_hpa / 1010.0) * (283.0 / (273.0 + temperature_c))
    return arcmin * scale / 60.0


def apparent_altitude_deg(
    geometric_alt_deg: float,
    *,
    model: RefractionModel = "saemundsson",
    pressure_hpa: float = 1010.0,
    temperature_c: float = 10.0,
) -> float:
    """Geometric altitude plus refraction (see :func:`refraction_deg`)."""
    return geometric_alt_deg + refraction_deg(
        geometric_alt_deg, model=model, pressure_hpa=pressure_hpa, temperature_c=temperature_c
    )


@dataclass(frozen=True, slots=True)
class SunPosition:
    """The Sun at one place and time. ``altitude_deg`` is geometric (no refraction)."""

    altitude_deg: float
    azimuth_deg: float
    apparent_altitude_deg: float
    refraction_model: str
    backend: str

    def altitude(self, *, refracted: bool) -> float:
        """The refracted or geometric altitude."""
        return self.apparent_altitude_deg if refracted else self.altitude_deg


@dataclass(frozen=True, slots=True)
class MoonPosition:
    """The Moon at one place and time. ``altitude_deg`` is geometric (no refraction)."""

    altitude_deg: float
    azimuth_deg: float
    apparent_altitude_deg: float
    illuminated_fraction: float
    refraction_model: str
    backend: str


class EphemerisBackend(Protocol):
    """A source of geometric (unrefracted) topocentric positions."""

    name: str

    @property
    def version(self) -> str: ...

    def sun_alt_az(
        self, moment: datetime, lat_deg: float, lon_deg: float, height_m: float = 0.0
    ) -> tuple[float, float]: ...

    def moon_alt_az_illum(
        self, moment: datetime, lat_deg: float, lon_deg: float, height_m: float = 0.0
    ) -> tuple[float, float, float]: ...


class OculusBackend:
    """oculus-core's ``sun_moon`` (Standish 1992 Sun; truncated ELP-2000/82 Moon)."""

    name = "oculus-core"

    def __init__(self, module: Any) -> None:
        self._module = module

    @property
    def version(self) -> str:
        """Crate version plus the commit of the build (``.pinned`` if only the pin is known)."""
        rev, verified = oculus_rev(getattr(self._module, "__file__", None))
        return f"{self._module.version()}+{rev[:7]}" + ("" if verified else ".pinned")

    def _call(self, moment: datetime, lat_deg: float, lon_deg: float, height_m: float) -> Any:
        return self._module.sun_moon(
            _utc_ms(moment), float(lat_deg), float(lon_deg), float(height_m)
        )

    def sun_alt_az(
        self, moment: datetime, lat_deg: float, lon_deg: float, height_m: float = 0.0
    ) -> tuple[float, float]:
        """Geometric Sun altitude and azimuth (degrees, azimuth clockwise from north)."""
        r = self._call(moment, lat_deg, lon_deg, height_m)
        return float(r["sun_alt"]), float(r["sun_az"]) % 360.0

    def moon_alt_az_illum(
        self, moment: datetime, lat_deg: float, lon_deg: float, height_m: float = 0.0
    ) -> tuple[float, float, float]:
        """Geometric Moon altitude, azimuth and illuminated fraction."""
        r = self._call(moment, lat_deg, lon_deg, height_m)
        return float(r["moon_alt"]), float(r["moon_az"]) % 360.0, float(r["moon_illum"])


class NoaaBackend:
    """The independent NOAA algorithm (:mod:`.noaa_solar`); Sun only."""

    name = "noaa-gml-spreadsheet"
    version = "meeus-1991-ch25"

    def sun_alt_az(
        self, moment: datetime, lat_deg: float, lon_deg: float, height_m: float = 0.0
    ) -> tuple[float, float]:
        """Geometric Sun altitude and azimuth (no parallax; ``height_m`` is ignored)."""
        p = noaa_solar.solar_position(moment, lat_deg, lon_deg)
        return p.elevation_deg, p.azimuth_deg

    def moon_alt_az_illum(
        self, moment: datetime, lat_deg: float, lon_deg: float, height_m: float = 0.0
    ) -> tuple[float, float, float]:
        """The NOAA backend has no Moon; raises."""
        raise EphemerisUnavailableError("the NOAA backend computes the Sun only; " + _BUILD_HINT)


@lru_cache(maxsize=1)
def _import_oculus() -> Any:
    import oculus_core  # optional, compiled dependency (the 'oculus' group)

    return oculus_core


def get_ephemeris(name: str | None = None) -> EphemerisBackend:
    """Return the backend ``name`` (``oculus`` or ``noaa``), else ``$LOOKOUT_EPHEMERIS``.

    Defaults to oculus-core and raises :class:`EphemerisUnavailableError` if it is missing;
    nothing falls back silently.
    """
    choice = (name or os.environ.get(ENV_BACKEND) or "oculus").strip().lower()
    if choice in {"oculus", "oculus-core"}:
        try:
            return OculusBackend(_import_oculus())
        except ImportError as exc:
            raise EphemerisUnavailableError(_BUILD_HINT) from exc
    if choice in {"noaa", "noaa-gml-spreadsheet"}:
        return NoaaBackend()
    raise ValueError(f"unknown ephemeris backend {choice!r}; use 'oculus' or 'noaa'")


def sun_position(
    moment: datetime,
    lat_deg: float,
    lon_deg: float,
    height_m: float = 0.0,
    *,
    ephemeris: EphemerisBackend | None = None,
    refraction: RefractionModel = "saemundsson",
    pressure_hpa: float = 1010.0,
    temperature_c: float = 10.0,
) -> SunPosition:
    """The Sun's geometric and refracted altitude and its azimuth (PRD §8.4)."""
    backend = ephemeris or get_ephemeris()
    alt, az = backend.sun_alt_az(moment, lat_deg, lon_deg, height_m)
    apparent = apparent_altitude_deg(
        alt, model=refraction, pressure_hpa=pressure_hpa, temperature_c=temperature_c
    )
    return SunPosition(alt, az, apparent, refraction, backend.name)


def moon_position(
    moment: datetime,
    lat_deg: float,
    lon_deg: float,
    height_m: float = 0.0,
    *,
    ephemeris: EphemerisBackend | None = None,
    refraction: RefractionModel = "saemundsson",
    pressure_hpa: float = 1010.0,
    temperature_c: float = 10.0,
) -> MoonPosition:
    """The Moon's geometric and refracted altitude, azimuth and illuminated fraction."""
    backend = ephemeris or get_ephemeris()
    alt, az, illum = backend.moon_alt_az_illum(moment, lat_deg, lon_deg, height_m)
    apparent = apparent_altitude_deg(
        alt, model=refraction, pressure_hpa=pressure_hpa, temperature_c=temperature_c
    )
    return MoonPosition(alt, az, apparent, illum, refraction, backend.name)


def sun_rate_deg_per_min(
    moment: datetime,
    lat_deg: float,
    lon_deg: float,
    height_m: float = 0.0,
    *,
    ephemeris: EphemerisBackend | None = None,
    half_step_s: float = 30.0,
) -> float:
    """d(geometric altitude)/dt in degrees per minute, by a central difference."""
    backend = ephemeris or get_ephemeris()
    step = timedelta(seconds=half_step_s)
    before, _ = backend.sun_alt_az(moment - step, lat_deg, lon_deg, height_m)
    after, _ = backend.sun_alt_az(moment + step, lat_deg, lon_deg, height_m)
    return (after - before) / (2.0 * half_step_s / 60.0)
