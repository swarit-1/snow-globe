"""The field catalogue: names, aliases, GRIB identities and datums (PRD §8.3)."""

from __future__ import annotations

import pytest

from snow_globe_forecast.fields import (
    CATALOGUE,
    DEFAULT_FIELD_NAMES,
    TERRAIN_FIELD,
    field_by_name,
    resolve_fields,
)

ORIGINAL_21 = {
    "VIS:surface",
    "HGT:cloud ceiling",
    "HGT:cloud base",
    "HGT:cloud top",
    "LCDC:low cloud layer",
    "MCDC:middle cloud layer",
    "HCDC:high cloud layer",
    "TCDC:entire atmosphere",
    "TCDC:boundary layer cloud layer",
    "RH:2 m above ground",
    "DPT:2 m above ground",
    "TMP:2 m above ground",
    "HPBL:surface",
    "PRATE:surface",
    "CRAIN:surface",
    "CSNOW:surface",
    "CFRZR:surface",
    "CICEP:surface",
    "UGRD:10 m above ground",
    "VGRD:10 m above ground",
    "HGT:surface",
}


def test_catalogue_holds_the_requested_fields_plus_the_cloud_base_pressure() -> None:
    names = {spec.name for spec in CATALOGUE}
    assert names >= ORIGINAL_21
    assert names - ORIGINAL_21 == {"PRES:cloud base"}
    assert len(names) == len(CATALOGUE) == len(DEFAULT_FIELD_NAMES) == 22
    assert len({spec.alias for spec in CATALOGUE}) == 22
    assert TERRAIN_FIELD in names


def test_datums() -> None:
    heights = {spec.name: spec.datum for spec in CATALOGUE if spec.is_height}
    assert heights == {
        "HGT:cloud ceiling": "m above MSL",
        "HGT:cloud base": "m above MSL",
        "HGT:cloud top": "m above MSL",
        "HPBL:surface": "m above ground",
        "HGT:surface": "m above MSL",
    }
    for spec in CATALOGUE:
        assert len(spec.datum_evidence) > 10, spec.name
    assert "phantom low base" in field_by_name("HGT:cloud base").datum_evidence


def test_grib_identities_seen_live() -> None:
    # Read from HRRR 2026111907 f01 with ecCodes 2.50.0 (probe, 19 Nov).
    ident = {
        spec.name: (spec.discipline, spec.category, spec.number, spec.level_type, spec.level_value)
        for spec in CATALOGUE
    }
    assert ident["HPBL:surface"] == (0, 3, 18, 1, 0)
    assert ident["CRAIN:surface"] == (0, 1, 33, 1, 0)
    assert ident["CFRZR:surface"] == (0, 1, 34, 1, 0)
    assert ident["CICEP:surface"] == (0, 1, 35, 1, 0)
    assert ident["CSNOW:surface"] == (0, 1, 36, 1, 0)
    assert ident["HGT:cloud ceiling"] == (0, 3, 5, 215, 0)
    assert ident["TCDC:boundary layer cloud layer"] == (0, 6, 1, 211, 0)
    assert ident["UGRD:10 m above ground"] == (0, 2, 2, 103, 10)


def test_resolve_fields() -> None:
    assert resolve_fields(None) == list(CATALOGUE)
    got = resolve_fields(["VIS", "HGT:cloud base", "lcdc,MCDC", "TERRAIN"])
    assert [s.name for s in got] == [
        "VIS:surface",
        "HGT:cloud base",
        "LCDC:low cloud layer",
        "MCDC:middle cloud layer",
        "HGT:surface",
    ]
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_fields(["HGT"])
    assert [s.name for s in resolve_fields(["TCDC", "BLCDC"])] == [
        "TCDC:entire atmosphere",
        "TCDC:boundary layer cloud layer",
    ]
    with pytest.raises(ValueError, match="unknown"):
        resolve_fields(["TCDC:"])  # never guessed
    with pytest.raises(ValueError, match="unknown"):
        resolve_fields(["FOG"])
    with pytest.raises(KeyError):
        field_by_name("FOG:surface")
