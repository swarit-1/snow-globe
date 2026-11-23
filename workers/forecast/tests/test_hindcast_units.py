"""Hindcast building blocks: its storage tree, the quiet window, the caching bucket, the Sun, the
frames (PRD §13.2, §13.4, §15.3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from snow_globe_common.storage import LocalStorage

from snow_globe_forecast.discovery import list_run
from snow_globe_forecast.extract import BandwidthCapError
from snow_globe_forecast.hindcast.cachebucket import LEDGER_KEY, CachingBucket, NetworkCapError
from snow_globe_forecast.hindcast.frames import collect_frames, in_sealed_window
from snow_globe_forecast.hindcast.quiet import QuietWindow
from snow_globe_forecast.hindcast.storage import (
    HindcastStorage,
    ReadOnlyStorage,
    ReadOnlyStorageError,
)
from snow_globe_forecast.hindcast.sun import solar_altitude_deg
from snow_globe_forecast.hrrr import idx_key, object_key

from .conftest import FIXTURES, RAINIER
from .fakebucket import FakeBucket

RUN = datetime(2026, 11, 20, 12, tzinfo=UTC)
NOW = datetime(2026, 11, 23, 12, 20, tzinfo=UTC)


# -- storage ------------------------------------------------------------------------------
def test_hindcast_storage_maps_the_extractor_keys_into_its_root(data_root: Path) -> None:
    hs = HindcastStorage.at(data_root)
    hs.write_json(f"forecast/hrrr/{RAINIER}/2026112012z.json", {"a": 1})
    hs.append_jsonl("forecast_log/2026-11-23.jsonl", {"event": "run"})
    hs.write_json("state/forecast/hrrr/2026112012z.json", {"m": 1})
    root = data_root / "forecast" / "hindcast"
    assert (root / "hrrr" / RAINIER / "2026112012z.json").is_file()
    assert (root / "log" / "2026-11-23.jsonl").is_file()
    assert (root / "state" / "forecast" / "hrrr" / "2026112012z.json").is_file()
    # Nothing in the live worker's places.
    for live in ("forecast/hrrr", "forecast_log", "state"):
        assert not (data_root / live).exists(), live
    assert hs.read_json(f"forecast/hrrr/{RAINIER}/2026112012z.json") == {"a": 1}
    assert hs.read_json("forecast/hrrr/x.y/2026112012z.json", default=None) is None
    assert hs.list_prefix("forecast/hrrr/") == [f"forecast/hrrr/{RAINIER}/2026112012z.json"]
    assert hs.list_prefix("") == sorted(
        [
            f"forecast/hrrr/{RAINIER}/2026112012z.json",
            "forecast_log/2026-11-23.jsonl",
            "state/forecast/hrrr/2026112012z.json",
        ]
    )
    with hs.lock("forecast-hrrr", timeout_s=1.0):
        assert (root / "state" / "locks" / "forecast-hrrr.lock").is_file()
    assert not (data_root / "state").exists()


@pytest.mark.parametrize(
    "key", ["frames/x.y/a.jpg", "forecast/hindcast/hrrr/a.json", "cache/hrrr/a", "forecast/x"]
)
def test_hindcast_storage_refuses_every_other_key(data_root: Path, key: str) -> None:
    hs = HindcastStorage.at(data_root)
    with pytest.raises(ValueError, match="refuses"):
        hs.put_bytes(key, b"x")
    with pytest.raises(ValueError, match="refuses"):
        hs.exists(key)


def test_read_only_storage_reads_and_never_writes(data_root: Path) -> None:
    inner = LocalStorage(data_root)
    inner.write_json("forecast_log/x.json", {"k": 1})
    ro = ReadOnlyStorage(inner)
    assert ro.read_json("forecast_log/x.json") == {"k": 1}
    assert ro.exists("forecast_log/x.json") and ro.list_prefix("forecast_log/")
    assert ro.read_json("nope.json", default=3) == 3
    for call in (
        lambda: ro.put_bytes("a.json", b"x"),
        lambda: ro.write_json("a.json", {}),
        lambda: ro.append_jsonl("forecast_log/2026-11-23.jsonl", {"event": "x"}),
        lambda: ro.append_jsonl_many("forecast_log/2026-11-23.jsonl", [{}]),
        lambda: ro.lock("forecast-hrrr"),
    ):
        with pytest.raises(ReadOnlyStorageError):
            call()
    assert sorted(p.name for p in (data_root / "forecast_log").iterdir()) == ["x.json"]


# -- the quiet window ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("hh_mm_ss", "seconds"),
    [
        ((10, 49, 59), 0.0),
        ((10, 50, 0), 15 * 60.0),
        ((10, 59, 30), 5.5 * 60.0),
        ((11, 0, 0), 5 * 60.0),
        ((11, 4, 59), 1.0),
        ((11, 5, 0), 0.0),
        ((11, 30, 0), 0.0),
    ],
)
def test_quiet_window_is_50_to_05(hh_mm_ss: tuple[int, int, int], seconds: float) -> None:
    h, m, s = hh_mm_ss
    now = datetime(2026, 11, 23, h, m, s, tzinfo=UTC)
    assert QuietWindow().seconds_until_open(now) == pytest.approx(seconds)


def test_quiet_window_sleeps_until_open_and_rechecks() -> None:
    clock = [datetime(2026, 11, 23, 10, 55, tzinfo=UTC)]
    slept: list[float] = []
    pauses: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    window = QuietWindow(
        clock=lambda: clock[0], sleep=sleep, on_pause=lambda _, s: pauses.append(s)
    )
    assert window.wait() == pytest.approx(600.0)
    assert slept == pauses == [pytest.approx(600.0)]
    assert window.wait() == 0.0 and window.pauses == 1 and window.paused_s == pytest.approx(600)
    with pytest.raises(ValueError):
        QuietWindow(start_minute=5, end_minute=50)


# -- the caching bucket -------------------------------------------------------------------
def _caching(fake: FakeBucket, root: LocalStorage, now: datetime, **kw) -> CachingBucket:
    return CachingBucket(
        root, transport=fake.transport(), sleep=lambda _: None, clock=lambda: now, **kw
    )


def test_caching_bucket_keeps_listings_and_inventories_never_grib(
    data_root: Path, small_grid
) -> None:
    fake = FakeBucket(grid=small_grid)
    fake.add_run(RUN, fields=["VIS:surface", "HGT:surface"], hours=[0, 1])
    root = LocalStorage(data_root / "forecast" / "hindcast")
    with _caching(fake, root, NOW) as bucket:
        listing = list_run(bucket, RUN)
        assert listing.ready_hours == [0, 1]
        text = bucket.get_text(idx_key(RUN, 0))
        bucket.get_range(object_key(RUN, 0), 0, 99)
        assert len(fake.requests) == 3
        assert list_run(bucket, RUN).ready_hours == [0, 1]
        assert bucket.get_text(idx_key(RUN, 0)) == text
        bucket.get_range(object_key(RUN, 0), 0, 99)
        assert len(fake.requests) == 4  # the listing and the .idx came from the cache
        assert bucket.cache_hits == {"listing": 1, "idx": 1}
        assert bucket.listing_fetched_utc(RUN) == NOW
        ledger = bucket.stored_ledger()
    assert ledger["requests"] == {"listing": 1, "idx": 1, "grib": 2}
    assert ledger["bytes"]["grib"] == 200
    kept = root.list_prefix("cache/s3/")
    assert any(k.startswith("cache/s3/list/") for k in kept)
    assert f"cache/s3/idx/{idx_key(RUN, 0)}" in kept
    assert not any(k.endswith(".grib2") for k in kept)
    # The ledger persists and adds up across clients.
    assert root.read_json(LEDGER_KEY)["total_bytes"] == ledger["total_bytes"]
    with _caching(fake, root, NOW) as again:
        again.get_range(object_key(RUN, 1), 0, 9)
        assert again.stored_ledger()["requests"]["grib"] == 3
    assert root.read_json(LEDGER_KEY)["bytes"]["grib"] == 210


def test_caching_bucket_never_keeps_a_listing_of_a_new_run(data_root: Path, small_grid) -> None:
    fake = FakeBucket(grid=small_grid)
    fake.add_run(RUN, fields=["VIS:surface"], hours=[0])
    root = LocalStorage(data_root / "forecast" / "hindcast")
    with _caching(fake, root, RUN + timedelta(hours=23)) as bucket:
        list_run(bucket, RUN)
        list_run(bucket, RUN)
        assert len(fake.requests) == 2 and bucket.listing_fetched_utc(RUN) is None


def test_caching_bucket_waits_out_the_quiet_window_before_network_requests_only(
    data_root: Path, small_grid
) -> None:
    fake = FakeBucket(grid=small_grid)
    fake.add_run(RUN, fields=["VIS:surface"], hours=[0])
    clock = [datetime(2026, 11, 23, 11, 52, tzinfo=UTC)]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    quiet = QuietWindow(clock=lambda: clock[0], sleep=sleep)
    root = LocalStorage(data_root / "forecast" / "hindcast")
    with CachingBucket(
        root, quiet=quiet, transport=fake.transport(), sleep=sleep, clock=lambda: clock[0]
    ) as bucket:
        bucket.get_text(idx_key(RUN, 0))
        assert slept == [pytest.approx(13 * 60.0)] and len(fake.requests) == 1
        clock[0] = datetime(2026, 11, 23, 12, 51, tzinfo=UTC)
        bucket.get_text(idx_key(RUN, 0))  # a cache hit: no request, no pause
        assert len(slept) == 1 and len(fake.requests) == 1


# -- the Sun --------------------------------------------------------------------------------
def test_solar_altitude_matches_the_altitudes_capture_recorded() -> None:
    recorded = json.loads((FIXTURES / "hindcast" / "sun_sidecars.json").read_text())
    lat, lon = recorded["camera"]
    assert len(recorded["rows"]) >= 8
    for row in recorded["rows"]:
        moment = datetime.fromisoformat(row["image_time_utc"].replace("Z", "+00:00"))
        assert solar_altitude_deg(moment, lat, lon) == pytest.approx(row["elevation_deg"], abs=6e-4)


def test_solar_altitude_needs_an_aware_time() -> None:
    with pytest.raises(ValueError):
        solar_altitude_deg(datetime(2026, 11, 21, 20), 46.8, -121.7)


# -- frames ---------------------------------------------------------------------------------
def _sidecar(root: Path, tree: str, moment: datetime, sha: str | None, **extra) -> None:
    folder = root / tree / RAINIER / f"{moment:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{moment:%Y%m%dT%H%M%SZ}_{(sha or 'f' * 12)[:12]}.json"
    body = {"viewpoint_id": RAINIER, **extra}
    body["image_time_utc"] = moment.isoformat().replace("+00:00", "Z")
    if sha is not None:
        body["sha256"] = sha
    (folder / name).write_text(json.dumps(body))


def test_collect_frames_dedupes_trees_skips_the_sealed_window(data_root: Path, manifests) -> None:
    day = datetime(2026, 11, 21, 19, 0, tzinfo=UTC)  # Sun about 22 deg at Rainier
    night = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
    sealed = datetime(2026, 12, 20, 20, 0, tzinfo=UTC)
    edge = datetime(2027, 1, 15, 2, 0, tzinfo=UTC)  # 14 Jan local: still sealed
    _sidecar(data_root, "frames", day, "a" * 64, sun={"elevation_deg": 21.0})
    _sidecar(data_root, "thumbs", day, "a" * 64)
    _sidecar(data_root, "thumbs", day + timedelta(minutes=15), "b" * 64)
    _sidecar(data_root, "frames", night, "c" * 64)
    _sidecar(data_root, "frames", sealed, "d" * 64)
    _sidecar(data_root, "thumbs", edge, "e" * 64)
    (data_root / "frames" / RAINIER / "2026-11-21" / "broken.json").write_text("{")
    folder = data_root / "frames" / RAINIER / "2026-11-21"
    (folder / "no-time.json").write_text(json.dumps({"viewpoint_id": RAINIER}))
    found = collect_frames(ReadOnlyStorage(LocalStorage(data_root)), manifests[RAINIER])
    assert [f.image_time_utc for f in found.frames] == [night, day, day + timedelta(minutes=15)]
    first = found.frames[1]
    assert first.trees == ("frames", "thumbs") and first.key.startswith("frames/")
    assert first.sidecar_sun_deg == 21.0 and first.sun_alt_deg > 20.0
    assert found.frames[2].trees == ("thumbs",) and found.frames[2].key.startswith("thumbs/")
    assert [f.image_time_utc for f in found.daytime(3.0)] == [day, day + timedelta(minutes=15)]
    assert found.skipped == {
        "sealed window 2026-12-15..2027-01-14 (D-019, D-022)": 2,
        "unreadable sidecar": 1,
        "no image_time_utc": 1,
    }
    summary = found.summary(3.0)
    assert summary["daytime"] == 2 and summary["in_both_trees"] == 1
    assert summary["sun_check"]["frames_with_sidecar_sun"] == 1


def test_sealed_window_by_utc_or_local_date(manifests) -> None:
    zone = manifests[RAINIER].zone
    assert in_sealed_window(datetime(2026, 12, 15, 0, 30, tzinfo=UTC), zone)  # 14 Dec local
    assert in_sealed_window(datetime(2027, 1, 15, 7, 0, tzinfo=UTC), zone)  # 14 Jan local
    assert not in_sealed_window(datetime(2027, 1, 15, 8, 0, tzinfo=UTC), zone)
    assert not in_sealed_window(datetime(2026, 12, 14, 7, 59, tzinfo=UTC), zone)


def test_the_quiet_window_is_checked_before_every_attempt_retries_too(data_root: Path) -> None:
    """A request that fails at 10:49:59 is retried after its 1 s back-off, which ends in the
    window: the retry waits until 11:05, so nothing is sent between :50 and :05."""
    clock = [datetime(2026, 11, 23, 10, 49, 59, tzinfo=UTC)]
    sent: list[datetime] = []
    window_sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(clock[0])
        return httpx.Response(503 if len(sent) == 1 else 200, text="1:0:d=2026112012:VIS:surface:")

    def advance(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)

    def window_sleep(seconds: float) -> None:
        window_sleeps.append(seconds)
        advance(seconds)

    quiet = QuietWindow(clock=lambda: clock[0], sleep=window_sleep)
    root = LocalStorage(data_root / "forecast" / "hindcast")
    with CachingBucket(
        root, quiet=quiet, transport=httpx.MockTransport(handler), sleep=advance, clock=lambda: NOW
    ) as bucket:
        bucket.get_text(idx_key(RUN, 0))
    assert sent == [
        datetime(2026, 11, 23, 10, 49, 59, tzinfo=UTC),
        datetime(2026, 11, 23, 11, 5, 0, tzinfo=UTC),
    ]
    assert window_sleeps == [pytest.approx(15 * 60.0)] and quiet.pauses == 1
    assert all(QuietWindow().seconds_until_open(moment) == 0.0 for moment in sent)
    assert root.read_json(LEDGER_KEY)["retries"] == 1


def test_ledger_updates_of_two_clients_at_once_add_up(data_root: Path, small_grid) -> None:
    fake = FakeBucket(grid=small_grid)
    fake.add_run(RUN, fields=["VIS:surface", "HGT:surface"], hours=[0, 1])
    root = LocalStorage(data_root / "forecast" / "hindcast")
    with _caching(fake, root, NOW) as first, _caching(fake, root, NOW) as second:
        first.get_range(object_key(RUN, 0), 0, 999)
        second.get_range(object_key(RUN, 1), 0, 1999)
        first.get_range(object_key(RUN, 0), 0, 499)
        # Each client reads the other's bytes from the stored ledger.
        assert first.stored_ledger()["total_bytes"] == second.stored_ledger()["total_bytes"] == 3500
    stored = root.read_json(LEDGER_KEY)
    assert stored["bytes"] == {"grib": 3500} and stored["requests"] == {"grib": 3}


def test_caching_bucket_refuses_network_once_the_cap_is_reached(
    data_root: Path, small_grid
) -> None:
    fake = FakeBucket(grid=small_grid)
    fake.add_run(RUN, fields=["VIS:surface"], hours=[0, 1])
    root = LocalStorage(data_root / "forecast" / "hindcast")
    with _caching(fake, root, NOW) as bucket:
        text = bucket.get_text(idx_key(RUN, 0))  # kept in the cache
        spent = bucket.stored_ledger()["total_bytes"]
    with _caching(fake, root, NOW, cap_bytes=spent) as capped:
        assert capped.get_text(idx_key(RUN, 0)) == text  # a cache hit is never refused
        with pytest.raises(NetworkCapError, match="no further request"):
            capped.get_text(idx_key(RUN, 1))
        with pytest.raises(BandwidthCapError):  # the fetch reads it as the cap being reached
            capped.get_range(object_key(RUN, 1), 0, 9)
    assert len(fake.requests) == 1
    assert root.read_json(LEDGER_KEY)["total_bytes"] == spent  # refusals add nothing
    with _caching(fake, root, NOW, cap_bytes=spent + 1) as roomy:
        roomy.get_text(idx_key(RUN, 1))  # under the cap: sent
    assert len(fake.requests) == 2
