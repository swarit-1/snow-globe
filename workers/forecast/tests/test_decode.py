"""ecCodes decoding with identity, time, length, grid and bitmap checks (PRD §8.3)."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from lookout_forecast.decode import DecodeError, decode_subset, round_sig
from lookout_forecast.fields import field_by_name
from lookout_forecast.grid import GridMismatchError
from lookout_forecast.idx import MessageRange

from .conftest import RUN
from .fakebucket import identity, make_message, sub_grid


def _write(tmp_path, grid, specs_values, fh=2, run=RUN, missing=None):
    blobs, ranges, offset = [], [], 0
    ii, jj = np.meshgrid(np.arange(grid.nx), np.arange(grid.ny))
    for n, (name, base) in enumerate(specs_values, start=1):
        values = base + 100.0 * ii.ravel() + jj.ravel()
        mask = missing(ii.ravel(), jj.ravel()) if missing and name == "HGT:cloud base" else None
        blob = make_message(grid, identity(name), run, fh, values, missing=mask)
        ranges.append(MessageRange(name, n, offset, offset + len(blob) - 1, ""))
        blobs.append(blob)
        offset += len(blob)
    path = tmp_path / "x.subset.grib2"
    path.write_bytes(b"".join(blobs))
    return path, ranges


def test_values_at_points_and_bitmap(tmp_path, small_grid) -> None:
    names = [("VIS:surface", 20000.0), ("HGT:cloud base", 1000.0)]
    path, ranges = _write(tmp_path, small_grid, names, missing=lambda i, j: (i + j) % 7 == 0)
    specs = {n: field_by_name(n) for n, _ in names}
    cells = [(3, 5), (7, 0), (39, 39), None]
    flat = [None if c is None else small_grid.flat_index(*c) for c in cells]
    decoded = decode_subset(
        path, ranges, specs=specs, grid=small_grid, run_start=RUN, fh=2, flat=flat
    )
    vis, base = decoded
    assert vis.values == [20305.0, 20700.0, 23939.0, None]
    assert base.values == [1305.0, None, 4939.0, None]  # (7, 0): 7 % 7 == 0 is missing
    assert base.full_missing == sum((i + j) % 7 == 0 for i in range(40) for j in range(40))
    assert vis.meta["shortName"] == "vis" and base.meta["typeOfLevel"] == "cloudBase"
    assert base.meta["level"] == 0 and base.meta["bitmapPresent"] == 1


def test_wrong_identity_is_rejected(tmp_path, small_grid) -> None:
    path, ranges = _write(tmp_path, small_grid, [("LCDC:low cloud layer", 1.0)])
    ranges = [MessageRange("MCDC:middle cloud layer", 1, ranges[0].start, ranges[0].end, "")]
    with pytest.raises(DecodeError, match="identity"):
        decode_subset(
            path,
            ranges,
            specs={"MCDC:middle cloud layer": field_by_name("MCDC:middle cloud layer")},
            grid=small_grid,
            run_start=RUN,
            fh=2,
            flat=[0],
        )


def test_wrong_run_or_hour_is_rejected(tmp_path, small_grid) -> None:
    path, ranges = _write(tmp_path, small_grid, [("VIS:surface", 1.0)], fh=3)
    specs = {"VIS:surface": field_by_name("VIS:surface")}
    with pytest.raises(DecodeError, match="f02"):
        decode_subset(path, ranges, specs=specs, grid=small_grid, run_start=RUN, fh=2, flat=[0])
    with pytest.raises(DecodeError, match="reference time"):
        decode_subset(
            path,
            ranges,
            specs=specs,
            grid=small_grid,
            run_start=RUN + timedelta(hours=1),
            fh=3,
            flat=[0],
        )


def test_wrong_length_or_count_is_rejected(tmp_path, small_grid) -> None:
    path, ranges = _write(
        tmp_path, small_grid, [("VIS:surface", 1.0), ("RH:2 m above ground", 1.0)]
    )
    specs = {r.field: field_by_name(r.field) for r in ranges}
    bad = [MessageRange("VIS:surface", 1, ranges[0].start, ranges[0].end - 1, "")]
    with pytest.raises(DecodeError, match="bytes"):
        decode_subset(path, bad, specs=specs, grid=small_grid, run_start=RUN, fh=2, flat=[0])
    with pytest.raises(DecodeError, match="more messages"):
        decode_subset(path, ranges[:1], specs=specs, grid=small_grid, run_start=RUN, fh=2, flat=[0])


def test_grid_mismatch_is_rejected(tmp_path, small_grid) -> None:
    path, ranges = _write(tmp_path, small_grid, [("VIS:surface", 1.0)])
    other = sub_grid(10, 10, 40, 40)
    with pytest.raises(GridMismatchError):
        decode_subset(
            path,
            ranges,
            specs={"VIS:surface": field_by_name("VIS:surface")},
            grid=other,
            run_start=RUN,
            fh=2,
            flat=[0],
        )


def test_round_sig() -> None:
    assert round_sig(1234567.0) == 1234570.0
    assert round_sig(0.000123456789) == 0.000123457
    assert round_sig(-29.62672233581543) == -29.6267
    assert round_sig(0.0) == 0.0
