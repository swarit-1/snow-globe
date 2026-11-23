"""The P1 plan on recorded NOAA answers (PRD §13.4, §8.3; D-008).

Fixtures recorded on 23 Nov 2026 by ``snow-globe-forecast hindcast plan`` (its response cache):
the ListObjectsV2 answer for run 2026112012 (``list_20261120_t12z_full.xml``) and the f30 and
f31 inventories (``hrrr.20261120.t12z.wrfsfcf3{0,1}.idx``). Any other listing is answered empty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from snow_globe_common.storage import LocalStorage

from snow_globe_forecast.hindcast.cachebucket import CachingBucket
from snow_globe_forecast.hindcast.frames import frames_from_times
from snow_globe_forecast.hindcast.plan import LIVE_FIELD_NAMES, S3Availability, build_plan

from .conftest import FIXTURES, RAINIER

PREFIX = "hrrr.20261120/conus/hrrr.t12z.wrfsfcf"
NOW = datetime(2026, 11, 23, 10, 28, tzinfo=UTC)
#: A real base frame of plate v0.1 (frames/rainier.paradise-mountain/2026-11-21/).
VALID = datetime(2026, 11, 21, 18, 21, 1, 718000, tzinfo=UTC)
EMPTY = (
    '<?xml version="1.0" encoding="UTF-8"?><ListBucketResult '
    'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>noaa-hrrr-bdp-pds</Name>'
    "<Prefix>{prefix}</Prefix><KeyCount>0</KeyCount><MaxKeys>1000</MaxKeys>"
    "<IsTruncated>false</IsTruncated></ListBucketResult>"
)


def _handler(request: httpx.Request) -> httpx.Response:
    url = urlparse(str(request.url))
    if url.path in {"", "/"}:
        prefix = parse_qs(url.query)["prefix"][0]
        if prefix == PREFIX:
            return httpx.Response(
                200, content=(FIXTURES / "list_20261120_t12z_full.xml").read_bytes()
            )
        return httpx.Response(200, text=EMPTY.format(prefix=prefix))
    name = Path(url.path).name  # hrrr.t12z.wrfsfcf30.grib2.idx
    fixture = (
        FIXTURES / f"hrrr.20261120.{name.removeprefix('hrrr.').removesuffix('.grib2.idx')}.idx"
    )
    if url.path.startswith(f"/{PREFIX}") and fixture.exists():
        return httpx.Response(200, content=fixture.read_bytes())
    return httpx.Response(404, text="NoSuchKey")


def _bucket(root: Path) -> CachingBucket:
    return CachingBucket(
        LocalStorage(root / "forecast" / "hindcast"),
        transport=httpx.MockTransport(_handler),
        sleep=lambda _: None,
        clock=lambda: NOW,
    )


def _sizes_from_idx(name: str) -> dict[str, int]:
    """Message lengths of the live fields in a recorded .idx (file size from the listing)."""
    sizes = {
        "hrrr.20261120.t12z.wrfsfcf30.idx": 174_744_422,
        "hrrr.20261120.t12z.wrfsfcf31.idx": 174_181_643,
    }
    rows = [line.split(":") for line in (FIXTURES / name).read_text().strip().splitlines()]
    offsets = [int(r[1]) for r in rows] + [sizes[name]]
    return {
        f"{r[3]}:{r[4]}": offsets[n + 1] - offsets[n]
        for n, r in enumerate(rows)
        if f"{r[3]}:{r[4]}" in LIVE_FIELD_NAMES
    }


def test_recorded_stamps_set_the_availability(data_root: Path) -> None:
    with _bucket(data_root) as bucket:
        lookup = S3Availability(bucket)
        start = datetime(2026, 11, 20, 12, tzinfo=UTC)
        # f31: GRIB2 13:28:24Z, .idx 13:28:29Z (the listing): the later one.
        assert lookup(start, 31) == (
            datetime(2026, 11, 20, 13, 28, 29, tzinfo=UTC),
            "archived-s3-lastmodified",
        )
        assert lookup(start, 30)[0] == datetime(2026, 11, 20, 13, 26, 37, tzinfo=UTC)
        assert lookup(start, 49).name == "UNAVAILABLE"  # type: ignore[union-attr]
        assert lookup.listing(start).complete


def test_plan_on_recorded_answers(data_root: Path, manifests) -> None:
    frames = frames_from_times(manifests[RAINIER], [VALID])
    with _bucket(data_root) as bucket:
        plan = build_plan(frames, bucket, now=NOW)
    (pair,) = plan["pairs"]
    # 18Z of 20 Nov is not listed here, so the 12Z run serves: f30-f31 bracket the frame.
    assert (pair["run"], pair["fh_lo"], pair["fh_hi"]) == ("2026112012", 30, 31)
    assert pair["availability_utc"] == "2026-11-20T13:28:29Z"
    expected_lead = (VALID - datetime(2026, 11, 20, 13, 28, 29, tzinfo=UTC)) / timedelta(hours=1)
    assert pair["lead_h"] == pytest.approx(expected_lead, abs=1e-6)
    assert pair["lead_h"] >= 24.0
    (run,) = plan["runs"]
    f30 = _sizes_from_idx("hrrr.20261120.t12z.wrfsfcf30.idx")
    f31 = _sizes_from_idx("hrrr.20261120.t12z.wrfsfcf31.idx")
    assert set(f30) == set(LIVE_FIELD_NAMES)  # every live field is in the inventory
    expected = sum(f30.values()) + sum(v for k, v in f31.items() if k != "HGT:surface")
    assert run["planned_bytes"] == expected  # the terrain once, with the first hour
    assert run["bytes_by_fh"] == {"30": sum(f30.values()), "31": expected - sum(f30.values())}
    assert plan["transfer"]["requests"] == {"listing": 2, "idx": 2}
