"""Run discovery: completeness, out-of-order hours, availability stamps (PRD §8.3, §11.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lookout_forecast.bucket import S3Object, parse_list_page
from lookout_forecast.discovery import find_latest_complete, group_runs, list_run

from .conftest import FIXTURES, RUN


def _obj(run: datetime, fh: int, lag_min: float, *, idx: bool = False) -> S3Object:
    key = f"hrrr.{run:%Y%m%d}/conus/hrrr.t{run:%H}z.wrfsfcf{fh:02d}.grib2" + (".idx" if idx else "")
    return S3Object(key, run + timedelta(minutes=lag_min), 10 if idx else 1000, "e")


def test_real_listing_of_a_complete_run() -> None:
    page = parse_list_page((FIXTURES / "list_t07z_full.xml").read_bytes())
    runs = group_runs(page.objects, listed_utc=datetime(2026, 11, 19, 9, tzinfo=UTC))
    listing = runs[datetime(2026, 11, 19, 7, tzinfo=UTC)]
    assert listing.complete and listing.ready_hours == list(range(19))
    f00 = listing.hour(0)
    assert f00.availability_utc == max(f00.grib.last_modified, f00.idx.last_modified)
    assert f00.grib_url.endswith("hrrr.t07z.wrfsfcf00.grib2")


def test_hours_arrive_out_of_order_and_need_both_objects() -> None:
    objects = [_obj(RUN, fh, 50 + fh) for fh in range(49)]
    objects += [_obj(RUN, fh, 50.1 + fh, idx=True) for fh in range(49) if fh != 8]
    objects = [o for o in objects if "f09.grib2" not in o.key or o.key.endswith(".idx")]
    listing = group_runs(objects, listed_utc=RUN)[RUN]
    assert not listing.complete
    assert listing.missing_hours == [8, 9]
    assert 10 in listing.ready_hours


def test_other_products_are_ignored() -> None:
    other = S3Object("hrrr.20261119/conus/hrrr.t06z.wrfprsf00.grib2", RUN, 5, "e")
    assert group_runs([other], listed_utc=RUN) == {}


def test_list_run_and_latest_complete(fake, bucket) -> None:
    run = datetime(2026, 11, 19, 7, tzinfo=UTC)
    older = datetime(2026, 11, 19, 5, tzinfo=UTC)
    fake.add_run(older, fields=["VIS:surface"], hours=range(19))
    fake.add_run(run, fields=["VIS:surface"], hours=range(10))  # still publishing
    now = datetime(2026, 11, 19, 8, 30, tzinfo=UTC)
    listing = list_run(bucket, run, now=now)
    assert listing.exists and not listing.complete and listing.missing_hours == list(range(10, 19))
    empty = list_run(bucket, datetime(2026, 11, 19, 8, tzinfo=UTC), now=now)
    assert not empty.exists and not empty.complete
    latest = find_latest_complete(bucket, now=now, lookback_h=6)
    assert latest is not None and latest.run_start == older
    assert find_latest_complete(bucket, now=now, lookback_h=1) is None
