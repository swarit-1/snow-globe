"""Inventories and byte ranges, against real HRRR .idx files of 2026-11-19 06Z."""

from __future__ import annotations

import pytest

from lookout_forecast.fields import CATALOGUE
from lookout_forecast.idx import (
    IdxError,
    MessageRange,
    group_contiguous,
    message_ranges,
    parse_idx,
    select_entry,
)

from .conftest import idx_text

F00 = "hrrr.20261119.t06z.wrfsfcf00.idx"
HOURS = ("00", "01", "06", "48")


def test_parse_real_inventory() -> None:
    entries = parse_idx(idx_text(F00))
    assert len(entries) == 170
    first = entries[0]
    assert (first.message, first.offset, first.name, first.forecast) == (
        1,
        0,
        "REFC:entire atmosphere",
        "anl",
    )
    assert entries[2].variable.startswith("var discipline=0")  # a name with spaces and '='


@pytest.mark.parametrize("fh", HOURS)
def test_every_catalogue_field_is_in_live_inventories_exactly_once(fh: str) -> None:
    entries = parse_idx(idx_text(f"hrrr.20261119.t06z.wrfsfcf{fh}.idx"))
    for spec in CATALOGUE:
        matches = [e for e in entries if e.name == spec.name]
        assert len(matches) == 1, spec.name
        entry = select_entry(entries, spec.name)
        assert entry.forecast == ("anl" if fh == "00" else f"{int(fh)} hour fcst")


def test_vis_range_matches_herbie_inventory() -> None:
    # Herbie 2026.10.0 gave bytes=1364280-2852420 for :VIS:surface of this file (trial, 19 Nov).
    [vis] = message_ranges(parse_idx(idx_text(F00)), ["VIS:surface"], file_size=134877665)
    assert (vis.start, vis.end, vis.length) == (1364280, 2852420, 1488141)


def test_ranges_are_in_file_order_and_grouped_without_gaps() -> None:
    entries = parse_idx(idx_text("hrrr.20261119.t06z.wrfsfcf06.idx"))
    names = [
        "HGT:cloud base",
        "LCDC:low cloud layer",
        "MCDC:middle cloud layer",
        "HCDC:high cloud layer",
        "VIS:surface",
    ]
    ranges = message_ranges(entries, names, file_size=10**9)
    assert [r.field for r in ranges] == [
        "VIS:surface",
        "LCDC:low cloud layer",
        "MCDC:middle cloud layer",
        "HCDC:high cloud layer",
        "HGT:cloud base",
    ]
    groups = group_contiguous(ranges)
    # VIS alone; LCDC, MCDC, HCDC adjacent (messages 116-118); cloud base alone (121).
    assert [len(g.messages) for g in groups] == [1, 3, 1]
    assert sum(g.length for g in groups) == sum(r.length for r in ranges)
    assert groups[1].header == f"bytes={groups[1].start}-{groups[1].end}"


def test_last_message_needs_the_file_size() -> None:
    entries = parse_idx("1:0:d=2026111906:A:surface:anl:\n2:100:d=2026111906:B:surface:anl:\n")
    with pytest.raises(IdxError):
        message_ranges(entries, ["B:surface"], file_size=None)
    [b] = message_ranges(entries, ["B:surface"], file_size=150)
    assert (b.start, b.end) == (100, 149)


def test_missing_and_ambiguous_fields() -> None:
    text = (
        "1:0:d=2026111906:APCP:surface:0-6 hour acc fcst:\n"
        "2:10:d=2026111906:APCP:surface:5-6 hour acc fcst:\n"
        "3:20:d=2026111906:TMP:surface:6 hour fcst:\n"
        "4:30:d=2026111906:TMP:surface:5-6 hour ave fcst:\n"
    )
    entries = parse_idx(text)
    with pytest.raises(IdxError, match="not in the inventory"):
        select_entry(entries, "VIS:surface")
    with pytest.raises(IdxError, match="ambiguous"):
        select_entry(entries, "APCP:surface")
    assert select_entry(entries, "TMP:surface").message == 3  # the instantaneous one


def test_malformed_inventories_are_rejected() -> None:
    with pytest.raises(IdxError):
        parse_idx("")
    with pytest.raises(IdxError):
        parse_idx("1:0:d=x:A:surface\n")
    with pytest.raises(IdxError):
        parse_idx("1:10:d=x:A:surface:anl:\n")  # first offset must be 0
    with pytest.raises(IdxError):
        parse_idx("1:0:d=x:A:surface:anl:\n2:0:d=x:B:surface:anl:\n")


def test_overlapping_ranges_are_an_error() -> None:
    a = MessageRange("A", 1, 0, 99, "")
    b = MessageRange("B", 2, 50, 120, "")
    with pytest.raises(IdxError):
        group_contiguous([a, b])
