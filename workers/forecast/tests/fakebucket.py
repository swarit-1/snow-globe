"""A fake ``noaa-hrrr-bdp-pds`` for offline tests: synthetic GRIB2 + .idx + ListObjectsV2.

Messages are real GRIB2 (built with ecCodes from its in-memory GRIB2 sample) on a small
Lambert conformal grid that is an exact sub-grid of HRRR CONUS around a chosen cell, so
real viewpoint manifests map onto it. Values are exact (IEEE packing) and encode where
they came from: ``value = 100000 * field_slot + 10000 * fh + 100 * i + j``.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import eccodes
import httpx
import numpy as np

from snow_globe_forecast.fields import FieldSpec, field_by_name
from snow_globe_forecast.grid import HRRR_CONUS, LambertConformalGrid
from snow_globe_forecast.hrrr import BASE_URL, idx_key, object_key, run_hours

#: Messages Snow Globe never asks for, put in the files so ranges must skip them.
FILLER = ("REFC:entire atmosphere", "PRES:cloud top", "GUST:surface")
_FILLER_ID = {
    "REFC:entire atmosphere": (0, 16, 196, 10, 0),
    "PRES:cloud top": (0, 3, 0, 3, 0),
    "GUST:surface": (0, 2, 22, 1, 0),
}


def sub_grid(i0: int, j0: int, nx: int, ny: int) -> LambertConformalGrid:
    """An ``nx`` x ``ny`` piece of HRRR CONUS whose first point is HRRR cell (i0, j0)."""
    lat, lon = HRRR_CONUS.ij_latlon(i0, j0)
    return LambertConformalGrid(
        nx=nx,
        ny=ny,
        lat1=round(lat, 6),
        lon1=round(lon % 360.0, 6),
        lad=HRRR_CONUS.lad,
        lov=HRRR_CONUS.lov,
        latin1=HRRR_CONUS.latin1,
        latin2=HRRR_CONUS.latin2,
        dx=HRRR_CONUS.dx,
        dy=HRRR_CONUS.dy,
        radius_m=HRRR_CONUS.radius_m,
    )


def expected_value(slot: int, fh: int, i: int, j: int) -> float:
    """The value the fake writes for cell (i, j) of a field in ``slot`` at hour ``fh``."""
    return float(100000 * slot + 10000 * fh + 100 * i + j)


def missing_cell(i: int, j: int) -> bool:
    """Cells the fake marks missing (bitmap) in fields that have a bitmap."""
    return (i + j) % 7 == 0


def make_message(
    grid: LambertConformalGrid,
    ident: tuple[int, int, int, int, int],
    run_start: datetime,
    fh: int,
    values: np.ndarray,
    *,
    missing: np.ndarray | None = None,
) -> bytes:
    """One GRIB2 message (template 3.30, IEEE packing) with the given identity and values."""
    gid = eccodes.codes_grib_new_from_samples("GRIB2")
    try:
        eccodes.codes_set(gid, "gridDefinitionTemplateNumber", 30)
        for key, value in [
            ("shapeOfTheEarth", 6),
            ("Nx", grid.nx),
            ("Ny", grid.ny),
            ("latitudeOfFirstGridPointInDegrees", grid.lat1),
            ("longitudeOfFirstGridPointInDegrees", grid.lon1 % 360.0),
            ("LaDInDegrees", grid.lad),
            ("LoVInDegrees", grid.lov % 360.0),
            ("Latin1InDegrees", grid.latin1),
            ("Latin2InDegrees", grid.latin2),
            ("DxInMetres", grid.dx),
            ("DyInMetres", grid.dy),
            ("scanningMode", 64),
        ]:
            eccodes.codes_set(gid, key, value)
        discipline, category, number, surface, level = ident
        eccodes.codes_set(gid, "discipline", discipline)
        eccodes.codes_set(gid, "parameterCategory", category)
        eccodes.codes_set(gid, "parameterNumber", number)
        eccodes.codes_set(gid, "typeOfFirstFixedSurface", surface)
        if surface == 103:
            eccodes.codes_set(gid, "scaleFactorOfFirstFixedSurface", 0)
            eccodes.codes_set(gid, "scaledValueOfFirstFixedSurface", level)
        eccodes.codes_set(gid, "dataDate", int(run_start.strftime("%Y%m%d")))
        eccodes.codes_set(gid, "dataTime", run_start.hour * 100)
        eccodes.codes_set(gid, "indicatorOfUnitOfTimeRange", 1)
        eccodes.codes_set(gid, "forecastTime", fh)
        eccodes.codes_set(gid, "packingType", "grid_ieee")
        data = np.asarray(values, dtype=np.float64).copy()
        if missing is not None and missing.any():
            eccodes.codes_set(gid, "bitmapPresent", 1)
            eccodes.codes_set(gid, "missingValue", -1.0e30)
            data[missing] = -1.0e30
        eccodes.codes_set_values(gid, data)
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)


def identity(name: str) -> tuple[int, int, int, int, int]:
    """GRIB identity of a catalogue field or a filler message."""
    if name in _FILLER_ID:
        return _FILLER_ID[name]
    spec: FieldSpec = field_by_name(name)
    return (spec.discipline, spec.category, spec.number, spec.level_type, spec.level_value)


@dataclass
class FakeFile:
    """One forecast hour: GRIB bytes, idx text and listing stamps."""

    grib: bytes
    idx: str
    grib_modified: datetime
    idx_modified: datetime


@dataclass
class FakeBucket:
    """Serves runs over ``httpx.MockTransport`` and records every request."""

    grid: LambertConformalGrid
    files: dict[str, FakeFile] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    fail_next: dict[str, int] = field(default_factory=dict)
    ignore_range: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- building -------------------------------------------------------------------
    def add_run(
        self,
        run_start: datetime,
        *,
        fields: Sequence[str],
        hours: Iterable[int] | None = None,
        lag_s: Mapping[int, float] | None = None,
        idx_lag_s: float = 4.0,
        bitmap_fields: Iterable[str] = (
            "HGT:cloud base",
            "HGT:cloud ceiling",
            "HGT:cloud top",
            "PRES:cloud base",
        ),
        drop_from_idx: Mapping[int, Sequence[str]] | None = None,
        rename_in_idx: Mapping[str, str] | None = None,
        fillers: bool = True,
    ) -> None:
        """Add a run whose files hold ``fields`` (in the given order) plus filler messages."""
        bitmap = set(bitmap_fields)
        lags = dict(lag_s or {})
        ii, jj = np.meshgrid(np.arange(self.grid.nx), np.arange(self.grid.ny))
        ii, jj = ii.ravel(), jj.ravel()
        for fh in run_hours(run_start) if hours is None else hours:
            if fillers:
                half = len(fields) // 2
                names = [FILLER[0], *fields[:half], FILLER[1], *fields[half:], FILLER[2]]
            else:
                names = list(fields)
            dropped = set((drop_from_idx or {}).get(fh, ()))
            blobs: list[bytes] = []
            lines: list[str] = []
            offset = 0
            for n, name in enumerate(names, start=1):
                slot = list(fields).index(name) if name in fields else 9
                values = 100000.0 * slot + 10000.0 * fh + 100.0 * ii + jj
                missing = None
                if name in bitmap:
                    missing = (ii + jj) % 7 == 0
                blob = make_message(
                    self.grid, identity(name), run_start, fh, values, missing=missing
                )
                desc = "anl" if fh == 0 else f"{fh} hour fcst"
                shown = f"DROPPED{n}:nowhere" if name in dropped else name
                shown = (rename_in_idx or {}).get(shown, shown)
                lines.append(f"{n}:{offset}:d={run_start:%Y%m%d%H}:{shown}:{desc}:")
                blobs.append(blob)
                offset += len(blob)
            modified = run_start + timedelta(seconds=lags.get(fh, 3000.0 + 60.0 * fh))
            self.files[object_key(run_start, fh)] = FakeFile(
                b"".join(blobs),
                "\n".join(lines) + "\n",
                modified,
                modified + timedelta(seconds=idx_lag_s),
            )

    def remove_hour(self, run_start: datetime, fh: int) -> None:
        """Make one hour of a run disappear (an incomplete run)."""
        self.files.pop(object_key(run_start, fh), None)

    # -- serving ----------------------------------------------------------------------
    def listing_xml(self, prefix: str) -> bytes:
        rows = []
        for key in sorted(self.files):
            f = self.files[key]
            for k, size, when in (
                (key, len(f.grib), f.grib_modified),
                (key + ".idx", len(f.idx.encode()), f.idx_modified),
            ):
                if k.startswith(prefix):
                    stamp = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                    rows.append(
                        f"<Contents><Key>{k}</Key><LastModified>{stamp}</LastModified>"
                        f"<ETag>&quot;x&quot;</ETag><Size>{size}</Size>"
                        f"<StorageClass>STANDARD</StorageClass></Contents>"
                    )
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Name>noaa-hrrr-bdp-pds</Name><Prefix>{prefix}</Prefix><KeyCount>{len(rows)}</KeyCount>"
            "<MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated>"
            + "".join(rows)
            + "</ListBucketResult>"
        )
        return body.encode()

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            path = urlparse(str(request.url)).path.lstrip("/")
            failures = self.fail_next.get(path, 0)
            if failures:
                self.fail_next[path] = failures - 1
                return httpx.Response(503, text="slow down")
        if path == "":
            prefix = parse_qs(urlparse(str(request.url)).query).get("prefix", [""])[0]
            return httpx.Response(200, content=self.listing_xml(prefix))
        if path.endswith(".idx"):
            f = self.files.get(path.removesuffix(".idx"))
            if f is None:
                return httpx.Response(404, text="NoSuchKey")
            return httpx.Response(200, content=f.idx.encode())
        f = self.files.get(path)
        if f is None:
            return httpx.Response(404, text="NoSuchKey")
        header = request.headers.get("range")
        if header is None or self.ignore_range:
            return httpx.Response(200, content=f.grib)
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", header)
        assert match, header
        start, end = int(match[1]), int(match[2])
        return httpx.Response(
            206,
            content=f.grib[start : end + 1],
            headers={"Content-Range": f"bytes {start}-{end}/{len(f.grib)}"},
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def grib_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(".grib2")]


def url_of(key: str) -> str:
    return f"{BASE_URL}/{key}"


__all__ = [
    "FakeBucket",
    "expected_value",
    "idx_key",
    "make_message",
    "missing_cell",
    "sub_grid",
    "url_of",
]
