"""What the ephemeris validation measures, and where (PLAN.md M2 "Ephemeris validation").

- **Sites**: each viewpoint's camera position from its manifest (latitude, longitude, height
  above sea level), read once when the Horizons tables are planned. Each cached table records
  the site it was fetched for, and every comparison uses that recorded site, so a later
  manifest correction never mixes two positions in one comparison.
- **Period**: the 12 months from 1 Jul 2026 00:00 to 30 Jun 2027 23:00 UTC, hourly (8,760
  epochs). They include design P's sealed window (15 Dec 2026 – 14 Jan 2027; no frame is
  involved, only positions), both solstices and both equinoxes, and the months the MSV runs
  live.
- **Tables** per site: Sun and Moon, airless (quantities 2, 4, 10, 20) and refracted (4).

The sites are written to ``<cache>/plan.json`` the first time (:func:`load_or_create_plan`) and
read back on every later run, so a re-run asks for exactly the cached tables and needs no
network; ``replan=True`` reads the manifests again (and fetches tables for any moved site).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snow_globe_common.manifest import Viewpoint, load_manifests
from snow_globe_common.timeutil import isoformat_z, utcnow

from .files import atomic_write_json
from .horizons import HorizonsQuery

__all__ = [
    "AIRLESS_QUANTITIES",
    "BODIES",
    "PERIOD_START",
    "PERIOD_STEP",
    "PERIOD_STOP",
    "PLAN_SCHEMA",
    "REFRACTED_QUANTITIES",
    "PlannedTable",
    "Site",
    "load_or_create_plan",
    "plan_tables",
    "sites_from_manifests",
]

PERIOD_START = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
PERIOD_STOP = datetime(2027, 6, 30, 23, 0, tzinfo=UTC)
PERIOD_STEP = "1h"
BODIES = ("sun", "moon")
#: Apparent RA/Dec, apparent azimuth and elevation, illuminated fraction, range.
AIRLESS_QUANTITIES = "2,4,10,20"
#: Refracted azimuth and elevation only.
REFRACTED_QUANTITIES = "4"
PLAN_SCHEMA = "snow-globe.ephem-validate-plan/1"


@dataclass(frozen=True, slots=True)
class Site:
    """A viewpoint's camera position, as the tables were planned for it."""

    viewpoint_id: str
    lat_deg: float
    lon_deg: float
    height_m: float
    time_zone: str
    view_azimuth_deg: float

    @classmethod
    def from_viewpoint(cls, viewpoint: Viewpoint) -> Site:
        """The camera position of a validated manifest."""
        return cls(
            viewpoint_id=viewpoint.id,
            lat_deg=viewpoint.camera.lat,
            lon_deg=viewpoint.camera.lon,
            height_m=viewpoint.camera.elevation_m,
            time_zone=viewpoint.time_zone,
            view_azimuth_deg=viewpoint.camera.view_azimuth_deg,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Site:
        """Inverse of :meth:`to_dict`."""
        return cls(
            viewpoint_id=str(data["viewpoint_id"]),
            lat_deg=float(data["lat_deg"]),
            lon_deg=float(data["lon_deg"]),
            height_m=float(data["height_m"]),
            time_zone=str(data["time_zone"]),
            view_azimuth_deg=float(data["view_azimuth_deg"]),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "viewpoint_id": self.viewpoint_id,
            "lat_deg": self.lat_deg,
            "lon_deg": self.lon_deg,
            "height_m": self.height_m,
            "time_zone": self.time_zone,
            "view_azimuth_deg": self.view_azimuth_deg,
        }


def sites_from_manifests(ids: Iterable[str] | None = None) -> list[Site]:
    """Every viewpoint's site (or only ``ids``), sorted by viewpoint id."""
    manifests = load_manifests()
    wanted = sorted(manifests) if ids is None else sorted(set(ids))
    missing = [i for i in wanted if i not in manifests]
    if missing:
        raise ValueError(f"no manifest for {', '.join(missing)}")
    return [Site.from_viewpoint(manifests[i]) for i in wanted]


@dataclass(frozen=True, slots=True)
class PlannedTable:
    """One Horizons table to have: the site, the query and the cache label."""

    site: Site
    query: HorizonsQuery
    label: str


def plan_tables(
    sites: Sequence[Site],
    *,
    start: datetime = PERIOD_START,
    stop: datetime = PERIOD_STOP,
    step: str = PERIOD_STEP,
) -> list[PlannedTable]:
    """The Horizons tables for ``sites``: per site, Sun then Moon, airless then refracted."""
    out: list[PlannedTable] = []
    span = f"{start:%Y%m%d}-{stop:%Y%m%d}.{step}"
    for site in sites:
        for body in BODIES:
            for apparent, quantities in (
                ("airless", AIRLESS_QUANTITIES),
                ("refracted", REFRACTED_QUANTITIES),
            ):
                query = HorizonsQuery(
                    body=body,  # type: ignore[arg-type]
                    lat_deg=site.lat_deg,
                    lon_deg=site.lon_deg,
                    height_m=site.height_m,
                    start=start,
                    stop=stop,
                    step=step,
                    apparent=apparent,  # type: ignore[arg-type]
                    quantities=quantities,
                )
                label = f"{site.viewpoint_id}.{body}.{apparent}.{span}"
                out.append(PlannedTable(site, query, label))
    return out


def load_or_create_plan(
    cache_root: Path, *, replan: bool = False, ids: Iterable[str] | None = None
) -> tuple[list[Site], dict[str, Any]]:
    """The planned sites: from ``<cache_root>/plan.json``, else from the manifests (and saved).

    With ``replan`` the manifests are read again and the file replaced. ``ids`` limits a new
    plan to those viewpoints; a saved plan is returned whole (callers filter it).
    """
    path = Path(cache_root) / "plan.json"
    if path.is_file() and not replan:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema") != PLAN_SCHEMA:
            raise ValueError(f"{path} is not an ephemeris-validation plan")
        return [Site.from_dict(item) for item in doc["sites"]], doc
    sites = sites_from_manifests(ids)
    doc = {
        "schema": PLAN_SCHEMA,
        "created_utc": isoformat_z(utcnow()),
        "source": "viewpoints/manifests/*.json camera (lat, lon, elevation_m)",
        "period": {
            "start_utc": isoformat_z(PERIOD_START),
            "stop_utc": isoformat_z(PERIOD_STOP),
            "step": PERIOD_STEP,
        },
        "sites": [site.to_dict() for site in sites],
    }
    atomic_write_json(path, doc, indent=2)
    return sites, doc
