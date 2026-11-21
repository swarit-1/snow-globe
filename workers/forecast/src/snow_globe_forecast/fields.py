"""The HRRR ``wrfsfc`` fields Snow Globe extracts, with each height field's datum (PRD §8.1, §8.3).

Names are the ``VAR:LEVEL`` strings of the ``.idx`` inventory, checked against live
inventories of the 2026-11-19 06Z run (f00, f01, f06, f48): each appears exactly once
per hour, as ``anl`` at f00 and ``N hour fcst`` afterwards.

Every decoded message is also checked against the GRIB2 product identity recorded here
(discipline, parameter category, parameter number, type and value of the first fixed
surface), so a wrong byte range can never be stored under the wrong name.

**Datums** (PRD §8.3 asks for each to be pinned, with evidence). ``datum`` says what the
*value* of a height field is measured from; it is ``n/a`` for fields whose values are
not heights (the level of a 2 m field is in ``typeOfLevel``/``level``).

Evidence for the four height fields comes from three places:

1. GRIB2 metadata: ``HGT`` is WMO parameter 0-3-5, *geopotential height* (unit gpm),
   which is defined relative to mean sea level. ``HPBL`` is WMO parameter 0-3-18,
   whose metadata names no datum.
2. NOAA-EMC UPP source (``develop`` at ``41077f7``, 2026-11-10). HRRR's ``wrfsfc`` file is
   the ``WRFTWO`` set in ``parm/hrrr_postcntrl.xml``. ``INITPOST.F`` sets the model
   interface heights ``ZINT = (PH + PHB) / g`` (geopotential, above MSL) and
   ``ZINT(LM+1) = FIS / g``, the terrain (lines 831-850, 981).
3. A whole-grid check on real data (``snow-globe-forecast datum-check``; HRRR 2026111907 f00,
   1,905,141 cells): a height above MSL is never below the model terrain, while a depth
   above ground over high terrain almost always is. The numbers are in each evidence
   string and in the README. ``HGT:cloud base`` passes on 97.5 % of cells and fails on the
   rest in a way ``PRES:cloud base`` shows to be invalid data, not a different datum.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "CATALOGUE",
    "DEFAULT_FIELD_NAMES",
    "TERRAIN_FIELD",
    "Datum",
    "FieldSpec",
    "field_by_name",
    "resolve_fields",
]

Datum = Literal["m above MSL", "m above ground", "n/a"]

_UPP = "NOAA-EMC/UPP develop@41077f7 (2026-11-10)"
_GPM = (
    "GRIB2 metadata: discipline 0, category 3, number 5 = WMO 'Geopotential height' (gpm), "
    "a height relative to mean sea level."
)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One extracted field: inventory name, GRIB2 identity, datum and its evidence."""

    name: str
    alias: str
    discipline: int
    category: int
    number: int
    level_type: int
    level_value: int
    datum: Datum
    datum_evidence: str
    description: str
    upp_shortname: str | None = None

    @property
    def variable(self) -> str:
        """The inventory variable (``HGT`` of ``HGT:cloud base``)."""
        return self.name.split(":", 1)[0]

    @property
    def is_height(self) -> bool:
        """True if the field's values are heights (so the datum matters)."""
        return self.datum != "n/a"


def _f(
    name: str,
    alias: str,
    product: tuple[int, int, int],
    level: tuple[int, int],
    datum: Datum,
    evidence: str,
    description: str,
    upp: str | None = None,
) -> FieldSpec:
    return FieldSpec(name, alias, *product, *level, datum, evidence, description, upp)


_NA = "n/a: the value is not a height."

CATALOGUE: tuple[FieldSpec, ...] = (
    _f(
        "VIS:surface",
        "VIS",
        (0, 19, 0),
        (1, 0),
        "n/a",
        "n/a: horizontal visibility in metres (UPP CALVIS_GSD, MDLFLD.f IGET(410)).",
        "Visibility at the surface (UPP GSD diagnostic; fog only when the model has cloud "
        "water within ~100 m of the ground, docs/research/forecast.md)",
        "GSD_VIS_ON_SURFACE",
    ),
    _f(
        "HGT:cloud ceiling",
        "CEILING",
        (0, 3, 5),
        (215, 0),
        "m above MSL",
        f"{_GPM} {_UPP}: GSD_HGT_ON_CLOUD_CEILING is IGET(408), CLDRAD.f lines 1956-2189: "
        "cloud base from ZMID (MSL) interpolation, clamped by "
        "'zcldbase = max(zcldbase,FIS(I,J)*GI+5.)' (line 2054, terrain + 5 m), and the LIFR "
        "count converts it to AGL with 'zcld = CLDZ(i,j) - FIS(I,J)*GI' (line 2177). "
        "No ceiling = GRIB missing (SPVAL). Data, HRRR 2026111907 f00, whole grid: 0 of "
        "502,664 cells below the model terrain (0 of 18,640 where terrain > 2000 m); lowest "
        "ceiling 34.6 m above the terrain.",
        "Cloud ceiling height (GSD legacy diagnostic)",
        "GSD_HGT_ON_CLOUD_CEILING",
    ),
    _f(
        "HGT:cloud base",
        "CLOUDBASE",
        (0, 3, 5),
        (2, 0),
        "m above MSL",
        f"{_GPM} {_UPP}: HGT_ON_CLOUD_BASE is IGET(178), CLDRAD.f lines 1886-1945 (same at "
        "tag v4.1.0.beta03): for RAPR (HRRR) CLDZ = ZINT(IBOT+1) + hypsometric thickness to "
        "the cloud-base pressure, or ZINT(LM); ZINT is geopotential height above MSL. No cloud "
        "= GRIB missing (SPVAL). Data, HRRR 2026111907 f00, 891,599 cloudy cells: where HGT "
        "and PRES:cloud base agree, HGT - terrain - (height PRES implies) is 9 m (median), "
        "p99 122 m. QUALITY: 110,670 cells (12.4 %) have HGT 7.4-8.3 km below what PRES "
        "implies; those columns hold high cloud only (mean HCDC 63 %, LCDC 3.7 %; LCDC "
        "77.7 % where low bases agree), so PRES is right and HGT is a phantom low base. Only "
        "21,868 of them (2.45 %) are below the terrain; the rest look plausible. Use HGT:cloud "
        "base only where it agrees with PRES:cloud base (snow_globe_forecast.evidence).",
        "Cloud base height (total, grid-scale + subgrid)",
        "HGT_ON_CLOUD_BASE",
    ),
    _f(
        "PRES:cloud base",
        "CLOUDBASEPRES",
        (0, 3, 0),
        (2, 0),
        "n/a",
        "n/a: pressure (Pa). Not in the original field list: added as the cross-check for "
        "HGT:cloud base, which is ~8 km too low in 12.4 % of cloudy cells (see that field). "
        "It is the next message after HGT:cloud base in every wrfsfc file, so it adds bytes, "
        "not requests.",
        "Pressure at the cloud base (Pa); GRIB missing where there is no cloud",
        "PRES_ON_CLOUD_BASE",
    ),
    _f(
        "HGT:cloud top",
        "CLOUDTOP",
        (0, 3, 5),
        (3, 0),
        "m above MSL",
        f"{_GPM} {_UPP}: HGT_ON_CLOUD_TOP is IGET(179), CLDRAD.f lines 2757-2815: "
        "CLDZ = ZMID(I,J,ITOP), a model mid-layer geopotential height above MSL; RAPR "
        "writes SPVAL (GRIB missing) where there is no cloud. Data, HRRR 2026111907 f00: 0 of "
        "457,194 cells below the model terrain; lowest top 42.0 m above the terrain.",
        "Cloud top height",
        "HGT_ON_CLOUD_TOP",
    ),
    _f("LCDC:low cloud layer", "LCDC", (0, 6, 3), (214, 0), "n/a", _NA, "Low cloud cover (%)"),
    _f(
        "MCDC:middle cloud layer", "MCDC", (0, 6, 4), (224, 0), "n/a", _NA, "Middle cloud cover (%)"
    ),
    _f("HCDC:high cloud layer", "HCDC", (0, 6, 5), (234, 0), "n/a", _NA, "High cloud cover (%)"),
    _f("TCDC:entire atmosphere", "TCDC", (0, 6, 1), (10, 0), "n/a", _NA, "Total cloud cover (%)"),
    _f(
        "TCDC:boundary layer cloud layer",
        "BLCDC",
        (0, 6, 1),
        (211, 0),
        "n/a",
        _NA,
        "Boundary-layer cloud cover (%); idx name verified live 2026-11-19",
    ),
    _f(
        "RH:2 m above ground", "RH", (0, 1, 1), (103, 2), "n/a", _NA, "Relative humidity at 2 m (%)"
    ),
    _f("DPT:2 m above ground", "DPT", (0, 0, 6), (103, 2), "n/a", _NA, "Dew point at 2 m (K)"),
    _f("TMP:2 m above ground", "TMP", (0, 0, 0), (103, 2), "n/a", _NA, "Temperature at 2 m (K)"),
    _f(
        "HPBL:surface",
        "HPBL",
        (0, 3, 18),
        (1, 0),
        "m above ground",
        "GRIB2 metadata: WMO parameter 0-3-18 'Planetary boundary layer height' (m), "
        f"ecCodes 'blh', names no datum. {_UPP}: HPBL_ON_SURFACE is IGET(221), MISCLN.f "
        "lines 2159-2165 write PBLH, read unchanged from the WRF variable 'PBLH' (INITPOST.F lines "
        "2859-2864), a depth above the model surface; the GSD gust PBL in the same file is "
        "explicitly 'zpbltop - zsf' (line 2890). Data, HRRR 2026111907 f00: HPBL is below "
        "the terrain height in 100 % of the 86,217 cells where terrain > 2000 m (median HPBL "
        "181 m, median terrain 2275 m) and its minimum is 9.9 m: a depth above ground.",
        "Planetary boundary layer height",
        "HPBL_ON_SURFACE",
    ),
    _f(
        "PRATE:surface",
        "PRATE",
        (0, 1, 7),
        (1, 0),
        "n/a",
        _NA,
        "Precipitation rate (kg m-2 s-1), instantaneous",
    ),
    _f("CRAIN:surface", "CRAIN", (0, 1, 33), (1, 0), "n/a", _NA, "Categorical rain (0/1)"),
    _f("CSNOW:surface", "CSNOW", (0, 1, 36), (1, 0), "n/a", _NA, "Categorical snow (0/1)"),
    _f("CFRZR:surface", "CFRZR", (0, 1, 34), (1, 0), "n/a", _NA, "Categorical freezing rain (0/1)"),
    _f("CICEP:surface", "CICEP", (0, 1, 35), (1, 0), "n/a", _NA, "Categorical ice pellets (0/1)"),
    _f(
        "UGRD:10 m above ground",
        "UGRD",
        (0, 2, 2),
        (103, 10),
        "n/a",
        "n/a: wind component. GRIB uvRelativeToGrid = 1: x-component along the Lambert grid, "
        "not eastward; rotate with LambertConformalGrid.earth_relative_wind.",
        "10 m wind, grid x-component (m s-1)",
    ),
    _f(
        "VGRD:10 m above ground",
        "VGRD",
        (0, 2, 3),
        (103, 10),
        "n/a",
        "n/a: wind component. GRIB uvRelativeToGrid = 1: y-component along the Lambert grid, "
        "not northward; rotate with LambertConformalGrid.earth_relative_wind.",
        "10 m wind, grid y-component (m s-1)",
    ),
    _f(
        "HGT:surface",
        "TERRAIN",
        (0, 3, 5),
        (1, 0),
        "m above MSL",
        f"{_GPM} {_UPP}: HGT_ON_SURFACE is IGET(025), SURFCE.f lines 227 and 296-309 write "
        "ZSFC = ZINT(I,J,LM+1) = FIS/g (INITPOST.F line 981), the WRF terrain height 'HGT' "
        "(INITPOST.F lines 822-828). This is the model's smoothed terrain, not the DEM.",
        "Model terrain height",
        "HGT_ON_SURFACE",
    ),
)

#: The field whose values fill ``model_terrain_m`` for every sample point.
TERRAIN_FIELD = "HGT:surface"
DEFAULT_FIELD_NAMES: tuple[str, ...] = tuple(spec.name for spec in CATALOGUE)

_BY_NAME = {spec.name.lower(): spec for spec in CATALOGUE}
_BY_ALIAS = {spec.alias.lower(): spec for spec in CATALOGUE}
_BY_VARIABLE: dict[str, list[FieldSpec]] = {}
for _spec in CATALOGUE:
    _BY_VARIABLE.setdefault(_spec.variable.lower(), []).append(_spec)


def field_by_name(name: str) -> FieldSpec:
    """Look a field up by its exact inventory name (case-insensitive)."""
    try:
        return _BY_NAME[name.strip().lower()]
    except KeyError:
        raise KeyError(f"unknown field {name!r}; known: {', '.join(DEFAULT_FIELD_NAMES)}") from None


def resolve_fields(tokens: Iterable[str] | None) -> list[FieldSpec]:
    """Turn CLI tokens into catalogue fields, in catalogue order.

    A token may be the inventory name (``"HGT:cloud base"``), the alias (``CLOUDBASE``;
    ``TCDC`` is total cloud, ``BLCDC`` boundary-layer cloud) or a bare variable that is
    unique in the catalogue (``VIS``, ``LCDC``). ``HGT`` is ambiguous and needs its level.
    Comma-separated tokens are split. None or an empty list means every field.
    """
    if not tokens:
        return list(CATALOGUE)
    chosen: set[str] = set()
    for raw in tokens:
        for token in (part.strip() for part in raw.split(",")):
            if not token:
                continue
            key = token.lower()
            spec = _BY_NAME.get(key) or _BY_ALIAS.get(key)
            if spec is None:
                candidates = _BY_VARIABLE.get(key, [])
                if len(candidates) == 1:
                    spec = candidates[0]
                elif candidates:
                    names = ", ".join(c.name for c in candidates)
                    raise ValueError(f"field {token!r} is ambiguous; say one of: {names}")
                else:
                    raise ValueError(
                        f"unknown field {token!r}; known: {', '.join(DEFAULT_FIELD_NAMES)}"
                    )
            chosen.add(spec.name)
    return [spec for spec in CATALOGUE if spec.name in chosen]


def names(specs: Sequence[FieldSpec]) -> list[str]:
    """The inventory names of ``specs``, in order."""
    return [spec.name for spec in specs]
