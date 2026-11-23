"""JPL Horizons observer tables for the ephemeris validation (PRD §8.4 "Checks"; PLAN.md M2).

The reference positions come from the Horizons API (``https://ssd.jpl.nasa.gov/api/horizons.api``)
as *observer* tables: the apparent azimuth and elevation (quantity 4) of the Sun (``10``) or the
Moon (``301``) for a geodetic site, either airless or with Horizons' own refraction, plus, in the
airless tables, the apparent right ascension and declination (quantity 2), the illuminated
fraction (10) and the range (20). These are the "Horizons vectors" of PRD §8.4: the same kind of
table D-002's golden test used ("JPL Horizons, observer table, airless apparent, UT"),
regenerated for each viewpoint.

**Politeness** (CLAUDE.md; the package brief): one request at a time and at least 2 s apart,
across every process on this machine (:class:`~snow_globe_common.http.MinIntervalLimiter`
``hold()``, its state under the cache folder), through
:func:`~snow_globe_common.http.polite_client` (the project's generic User-Agent, no cookies),
with exponential back-off on a 429, a 5xx or a transport error. Every response is written to
the cache before it is used and is never fetched again: a query's cache key is the SHA-256 of
its exact parameters, so a re-run is offline (``offline=True`` refuses to fetch at all).

**Time scale.** Horizons observer tables for an Earth site are in UT, which Horizons takes as
UTC after 1962; oculus-core takes UTC milliseconds, so the epochs are used as they are.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import numpy as np
from numpy.typing import NDArray
from snow_globe_common.http import (
    MinIntervalLimiter,
    backoff_delay,
    polite_client,
    retry_after_s,
    user_agent,
)
from snow_globe_common.timeutil import isoformat_z, utcnow

from .files import atomic_write_json

__all__ = [
    "API_URL",
    "BODY_COMMANDS",
    "CACHE_SCHEMA",
    "LIMITER_NAME",
    "MIN_INTERVAL_S",
    "HorizonsCache",
    "HorizonsError",
    "HorizonsQuery",
    "HorizonsQueryError",
    "HorizonsTable",
    "HorizonsUnavailableError",
    "fetch",
    "make_limiter",
    "parse_result",
]

API_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"
#: Horizons target codes.
BODY_COMMANDS: Mapping[str, str] = {"sun": "10", "moon": "301"}
#: One request at a time, at least this far apart, end to start (the package brief: >= 2 s).
MIN_INTERVAL_S = 2.0
#: Shared by every request to ssd.jpl.nasa.gov from this package (the API and the DE421 kernel).
LIMITER_NAME = "jpl-ssd"
CACHE_SCHEMA = "snow-globe.horizons-cache/1"
#: Retried with back-off; anything else is a bad query and is raised at once.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
#: A full year of hourly rows takes Horizons a while to build.
_TIMEOUT = httpx.Timeout(240.0, connect=15.0, pool=15.0)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}
_DATE_RE = re.compile(
    r"^(?:b)?(\d{4})-([A-Z][a-z]{2})-(\d{2}) (\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?$"
)
_STEP_RE = re.compile(r"^(\d+)\s*(m|h|d)$")

type Body = Literal["sun", "moon"]
type Apparent = Literal["airless", "refracted"]


class HorizonsError(RuntimeError):
    """A Horizons request failed."""


class HorizonsQueryError(HorizonsError):
    """Horizons rejected the query (HTTP 400, an ``error`` field, or no ephemeris in the text)."""


class HorizonsUnavailableError(HorizonsError):
    """No cached response and no fetch possible (offline, or the retries ran out)."""


def _horizons_time(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Horizons query times must be timezone-aware")
    utc = moment.astimezone(UTC)
    if utc.second or utc.microsecond:
        raise ValueError("Horizons query times are whole minutes")
    return utc.strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True, slots=True)
class HorizonsQuery:
    """One observer table: a body at a geodetic site over ``[start, stop]`` every ``step``.

    ``height_m`` is passed to Horizons as the height above its reference ellipsoid (WGS-84);
    the manifests give metres above sea level, and the same number goes to oculus-core, so the
    comparison is like for like (a 30 m datum difference moves the Moon by < 0.02 arcsec).
    """

    body: Body
    lat_deg: float
    lon_deg: float
    height_m: float
    start: datetime
    stop: datetime
    step: str
    apparent: Apparent
    quantities: str

    def __post_init__(self) -> None:
        if self.body not in BODY_COMMANDS:
            raise ValueError(f"body must be one of {sorted(BODY_COMMANDS)}")
        if self.apparent not in ("airless", "refracted"):
            raise ValueError("apparent must be 'airless' or 'refracted'")
        if not -90.0 <= self.lat_deg <= 90.0 or not -180.0 <= self.lon_deg <= 180.0:
            raise ValueError("site latitude or longitude out of range")
        if not _STEP_RE.match(self.step):
            raise ValueError("step must look like '1h', '2m' or '1d'")
        if self.stop < self.start:
            raise ValueError("stop is before start")
        _horizons_time(self.start)
        _horizons_time(self.stop)

    @property
    def step_delta(self) -> timedelta:
        """The step as a timedelta."""
        match = _STEP_RE.match(self.step)
        assert match is not None
        n, unit = int(match.group(1)), match.group(2)
        return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]

    def epochs(self) -> list[datetime]:
        """Every epoch the table should hold, ``start`` to ``stop`` inclusive."""
        out: list[datetime] = []
        moment, step = self.start.astimezone(UTC), self.step_delta
        stop = self.stop.astimezone(UTC)
        while moment <= stop:
            out.append(moment)
            moment += step
        return out

    def params(self) -> dict[str, str]:
        """The exact API parameters (values quoted as the Horizons API documentation does)."""
        site = f"{self.lon_deg:.6f},{self.lat_deg:.6f},{self.height_m / 1000.0:.4f}"
        return {
            "format": "json",
            "COMMAND": f"'{BODY_COMMANDS[self.body]}'",
            "OBJ_DATA": "'NO'",
            "MAKE_EPHEM": "'YES'",
            "EPHEM_TYPE": "'OBSERVER'",
            "CENTER": "'coord@399'",
            "COORD_TYPE": "'GEODETIC'",
            "SITE_COORD": f"'{site}'",
            "START_TIME": f"'{_horizons_time(self.start)}'",
            "STOP_TIME": f"'{_horizons_time(self.stop)}'",
            "STEP_SIZE": f"'{self.step}'",
            "QUANTITIES": f"'{self.quantities}'",
            "APPARENT": f"'{self.apparent.upper()}'",
            "REF_SYSTEM": "'ICRF'",
            "CAL_FORMAT": "'CAL'",
            "TIME_DIGITS": "'MINUTES'",
            "ANG_FORMAT": "'DEG'",
            "EXTRA_PREC": "'YES'",
            "CSV_FORMAT": "'YES'",
            "RANGE_UNITS": "'AU'",
            "SKIP_DAYLT": "'NO'",
        }

    def key(self) -> str:
        """SHA-256 (hex) of the canonical parameters: the cache key."""
        canonical = json.dumps(self.params(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def describe(self) -> dict[str, Any]:
        """JSON-ready form for records."""
        return {
            "body": self.body,
            "lat_deg": self.lat_deg,
            "lon_deg": self.lon_deg,
            "height_m": self.height_m,
            "start_utc": isoformat_z(self.start),
            "stop_utc": isoformat_z(self.stop),
            "step": self.step,
            "apparent": self.apparent,
            "quantities": self.quantities,
        }


@dataclass(frozen=True)
class HorizonsCache:
    """Cached responses under ``<root>/horizons/``, one JSON file per query key."""

    root: Path

    @property
    def folder(self) -> Path:
        """Where the responses live."""
        return Path(self.root) / "horizons"

    def find(self, query: HorizonsQuery) -> Path | None:
        """The cached file for ``query``, whatever label it was saved under, or None."""
        short = query.key()[:20]
        hits = sorted(self.folder.glob(f"*{short}.json")) if self.folder.is_dir() else []
        return hits[0] if hits else None

    def path_for(self, query: HorizonsQuery, label: str | None) -> Path:
        """Where a new response for ``query`` is written."""
        short = query.key()[:20]
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-.") if label else ""
        return self.folder / (f"{safe}.{short}.json" if safe else f"{short}.json")

    def load(self, query: HorizonsQuery) -> dict[str, Any] | None:
        """The cached record for ``query`` (checked against its key), or None."""
        path = self.find(query)
        if path is None:
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("schema") != CACHE_SCHEMA or record.get("key") != query.key():
            raise HorizonsError(f"{path} is not the cached response of this query")
        record["path"] = str(path)
        return record


def make_limiter(cache_root: Path) -> MinIntervalLimiter:
    """The shared ssd.jpl.nasa.gov limiter: >= 2 s end to start, state under the cache root."""
    return MinIntervalLimiter(LIMITER_NAME, MIN_INTERVAL_S, state_dir=cache_root)


def _check_response(query: HorizonsQuery, response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise HorizonsQueryError(f"HTTP {response.status_code}: not JSON") from exc
    if not isinstance(body, dict):
        raise HorizonsQueryError("the response is not a JSON object")
    if body.get("error"):
        raise HorizonsQueryError(f"Horizons error: {str(body['error'])[:500]}")
    result = body.get("result")
    if not isinstance(result, str) or "$$SOE" not in result or "$$EOE" not in result:
        tail = result[-800:] if isinstance(result, str) else repr(result)[:200]
        raise HorizonsQueryError(f"no ephemeris in the response ({query.body}): {tail}")
    return body


def fetch(
    query: HorizonsQuery,
    *,
    cache: HorizonsCache,
    label: str | None = None,
    offline: bool = False,
    client: httpx.Client | None = None,
    limiter: MinIntervalLimiter | None = None,
    max_attempts: int = 4,
    backoff_base_s: float = 30.0,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """The cached record for ``query``, fetching it first if it is not cached.

    The record holds the query, the request URL, when it was fetched, the HTTP status, the
    time the request took and the API's JSON response (``response``: ``signature`` and the
    ``result`` text). A response is cached only when it holds an ephemeris (``$$SOE`` …
    ``$$EOE``). A 429, 5xx or transport error defers every request under the limiter by an
    exponential back-off (or the ``Retry-After`` asked for, if longer) and is retried up to
    ``max_attempts`` times; then :class:`HorizonsUnavailableError`. A rejected query raises
    :class:`HorizonsQueryError` at once and is not cached.
    """
    cached = cache.load(query)
    if cached is not None:
        return cached
    if offline:
        raise HorizonsUnavailableError(
            f"no cached Horizons response for {query.describe()} and fetching is off"
        )
    limiter = limiter or make_limiter(Path(cache.root))
    own_client = client is None
    client = client or polite_client(timeout=_TIMEOUT)
    try:
        failures = 0
        while True:
            status: int | None = None
            error: str | None = None
            with limiter.hold():
                started = time.monotonic()
                try:
                    response = client.get(API_URL, params=query.params())
                    status = response.status_code
                except httpx.TransportError as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    response = None
                if response is not None and status not in _RETRY_STATUS:
                    if status == 200:
                        body = _check_response(query, response)
                        elapsed = time.monotonic() - started
                        record = {
                            "schema": CACHE_SCHEMA,
                            "key": query.key(),
                            "label": label,
                            "query": query.describe(),
                            "params": query.params(),
                            "url": str(response.request.url),
                            "user_agent": response.request.headers.get("User-Agent"),
                            "fetched_utc": isoformat_z(utcnow()),
                            "http_status": status,
                            "elapsed_s": round(elapsed, 3),
                            "response": body,
                        }
                        path = cache.path_for(query, label)
                        atomic_write_json(path, record)
                        record["path"] = str(path)
                        if log:
                            log(f"fetched {path.name} in {elapsed:.1f} s")
                        return record
                    text = response.text[:500]
                    raise HorizonsQueryError(f"HTTP {status}: {text}")
                failures += 1
                wait = backoff_delay(failures, base_s=backoff_base_s, cap_s=900.0)
                asked = retry_after_s(response) if response is not None else None
                if asked is not None and math.isfinite(asked):
                    wait = max(wait, asked)
                limiter.defer(wait)
            if log:
                log(f"Horizons {status or error}; backing off {wait:.0f} s ({failures})")
            if failures >= max_attempts:
                raise HorizonsUnavailableError(
                    f"Horizons failed {failures} times ({status or error}); re-run to resume"
                )
    finally:
        if own_client:
            client.close()


def current_user_agent() -> str:
    """The User-Agent the fetches send (the project's generic one unless overridden)."""
    return user_agent()


@dataclass(frozen=True)
class HorizonsTable:
    """A parsed observer table: epochs, the quantity columns and the header facts.

    ``values`` maps canonical names to float arrays (NaN where Horizons printed ``n.a.``):
    ``ra_deg``, ``dec_deg`` (apparent, of date), ``az_deg``, ``el_deg`` (airless or refracted
    as the query asked), ``illum_pct``, ``delta_au``, ``deldot_km_s``.
    """

    times_utc: tuple[datetime, ...]
    values: Mapping[str, NDArray[np.float64]]
    columns: tuple[str, ...]
    solar_flags: tuple[str, ...]
    lunar_flags: tuple[str, ...]
    header: Mapping[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.times_utc)


_HEADER_KEYS = (
    "Target body name",
    "Center body name",
    "Center-site name",
    "Center geodetic",
    "Center cylindric",
    "Center radii",
    "Target radii",
    "Atmos refraction",
    "RA format",
    "Time format",
    "Time zone",
    "Reference frame",
    "EOP file",
    "EOP coverage",
    "Start time",
    "Stop  time",
    "Step-size",
)

_COLUMN_NAMES = (
    ("ra_deg", ("R.A.",)),
    ("dec_deg", ("DEC",)),
    ("az_deg", ("Azi", "Azimuth")),
    ("el_deg", ("Elev",)),
    ("illum_pct", ("Illu",)),
    ("deldot_km_s", ("deldot",)),
    ("delta_au", ("delta",)),
)


def _parse_time(text: str) -> datetime:
    match = _DATE_RE.match(text.strip())
    if not match:
        raise HorizonsQueryError(f"unreadable Horizons date {text!r}")
    year, mon, day, hour, minute, sec, frac = match.groups()
    moment = datetime(
        int(year), _MONTHS[mon], int(day), int(hour), int(minute), int(sec or 0), tzinfo=UTC
    )
    if frac:
        moment += timedelta(seconds=float(f"0.{frac}"))
    return moment


def _float(text: str) -> float:
    value = text.strip()
    if not value or value.lower() in {"n.a.", "n.a", "na"}:
        return math.nan
    return float(value)


def parse_result(result: str) -> HorizonsTable:
    """Parse the ``result`` text of a CSV observer table (see :class:`HorizonsTable`)."""
    lines = result.splitlines()
    try:
        soe = next(i for i, line in enumerate(lines) if line.strip() == "$$SOE")
        eoe = next(i for i, line in enumerate(lines) if line.strip() == "$$EOE")
    except StopIteration as exc:
        raise HorizonsQueryError("no $$SOE/$$EOE block in the Horizons result") from exc
    header_line = next(
        (line for line in reversed(lines[:soe]) if "Date__" in line or "Date_" in line), None
    )
    if header_line is None:
        raise HorizonsQueryError("no column header before $$SOE")
    columns = tuple(part.strip() for part in header_line.split(","))
    index: dict[str, int] = {}
    for i, name in enumerate(columns):
        for canonical, prefixes in _COLUMN_NAMES:
            if canonical in index:
                continue
            if any(name.startswith(p) for p in prefixes):
                # "delta" must not take "deldot"
                if canonical == "delta_au" and name.startswith("deldot"):
                    continue
                index[canonical] = i
                break
    times: list[datetime] = []
    solar: list[str] = []
    lunar: list[str] = []
    raw: dict[str, list[float]] = {name: [] for name in index}
    for line in lines[soe + 1 : eoe]:
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        times.append(_parse_time(parts[0]))
        solar.append(parts[1] if len(parts) > 1 else "")
        lunar.append(parts[2] if len(parts) > 2 else "")
        for name, i in index.items():
            raw[name].append(_float(parts[i]) if i < len(parts) else math.nan)
    header: dict[str, str] = {}
    for line in lines[:soe]:
        stripped = line.strip()
        for key in _HEADER_KEYS:
            if stripped.startswith(key) and key not in header:
                header[key] = stripped[len(key) :].lstrip(" :").strip()
    return HorizonsTable(
        times_utc=tuple(times),
        values={name: np.asarray(vals, dtype=np.float64) for name, vals in raw.items()},
        columns=columns,
        solar_flags=tuple(solar),
        lunar_flags=tuple(lunar),
        header=header,
    )
