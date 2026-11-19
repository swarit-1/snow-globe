"""The scheduled Lambda entry point: idempotent, newest first, bounded (PRD §11.2)."""

from __future__ import annotations

import functools
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import eccodes
import pytest
from lookout_common.jsonl import read_jsonl
from lookout_common.storage import LockTimeoutError

from lookout_forecast import backoff, lambda_handler
from lookout_forecast.bucket import HrrrBucket
from lookout_forecast.extract import ExtractOptions, Extractor
from lookout_forecast.fields import resolve_fields

from .conftest import RAINIER

FIELDS = ["VIS:surface", "HGT:surface"]
NOW = datetime(2026, 11, 19, 9, 40, tzinfo=UTC)
R06 = datetime(2026, 11, 19, 6, tzinfo=UTC)
R07 = datetime(2026, 11, 19, 7, tzinfo=UTC)
OPTIONS = ExtractOptions(fields=tuple(resolve_fields(["VIS", "TERRAIN"])), max_fh=None)


@pytest.fixture
def setup(fake, bucket, storage, manifests, small_grid):
    for hour in (5, 6, 7):
        fake.add_run(
            datetime(2026, 11, 19, hour, tzinfo=UTC),
            fields=FIELDS,
            hours=range(19) if hour != 6 else range(49),
        )
    fake.add_run(datetime(2026, 11, 19, 8, tzinfo=UTC), fields=FIELDS, hours=range(12))
    fake.add_run(datetime(2026, 11, 19, 3, tzinfo=UTC), fields=FIELDS, hours=range(17))  # stale
    run = functools.partial(
        lambda_handler.process_pending,
        storage,
        bucket,
        {RAINIER: manifests[RAINIER]},
        options=OPTIONS,
        grid=small_grid,
        lookback_h=8,
    )
    return run


def _status(summary: dict) -> dict[str, str]:
    return {row["run"]: row["status"] for row in summary["runs"]}


def _log(storage, day: datetime = NOW) -> list[dict]:
    path = storage.path(f"forecast_log/{day:%Y-%m-%d}.jsonl")
    return read_jsonl(path) if path.exists() else []


def test_processes_complete_and_stale_runs_once(setup, storage) -> None:
    first = setup(now=NOW, max_runs=10)
    status = _status(first)
    assert status["2026111909"] == "not on S3"
    assert status["2026111908"].startswith("waiting")
    for run in ("2026111907", "2026111906", "2026111905"):
        assert status[run] == "extracted"
    assert status["2026111903"] == "extracted (stale, incomplete)"
    assert status["2026111904"].startswith("not on S3")
    assert first["extracted"] == 4
    assert storage.read_json("state/forecast/hrrr/2026111903z.json")["missing_hours"] == [17, 18]
    second = _status(setup(now=NOW, max_runs=10))
    for run in ("2026111907", "2026111906", "2026111905", "2026111903"):
        assert second[run] == "written"


def test_run_limit_and_time_budget(setup) -> None:
    status = _status(setup(now=NOW, max_runs=1))
    assert status["2026111907"] == "extracted"
    assert status["2026111906"].startswith("deferred: run limit")
    status = _status(setup(now=NOW, remaining_s=lambda: 10.0))
    assert status["2026111906"] == "deferred: time budget"


def test_handler_reads_its_settings_from_the_environment(
    fake, small_grid, data_root, manifests, monkeypatch
) -> None:
    fake.add_run(datetime(2026, 11, 19, 7, tzinfo=UTC), fields=FIELDS, hours=range(19))
    monkeypatch.setenv("LOOKOUT_FORECAST_FIELDS", "VIS,TERRAIN")
    monkeypatch.setenv("LOOKOUT_FORECAST_MAX_FH", "1")
    monkeypatch.setenv("LOOKOUT_FORECAST_LOOKBACK_H", "3")
    monkeypatch.setattr(
        lambda_handler, "HrrrBucket", lambda: HrrrBucket(transport=fake.transport())
    )
    monkeypatch.setattr(lambda_handler, "load_manifests", lambda: {RAINIER: manifests[RAINIER]})
    monkeypatch.setattr(
        lambda_handler,
        "process_pending",
        functools.partial(lambda_handler.process_pending, grid=small_grid, now=NOW),
    )

    class Context:
        def get_remaining_time_in_millis(self) -> int:
            return 500_000

    summary = lambda_handler.handler({}, Context())
    status = _status(summary)
    assert status["2026111907"] == "extracted"
    doc = (data_root / "forecast/hrrr" / RAINIER / "2026111907z.json").read_text()
    assert '"forecast_hours":[0,1]' in doc
    assert summary["now_utc"] == "2026-11-19T09:40:00Z"
    assert NOW - timedelta(hours=3) <= datetime(2026, 11, 19, 7, tzinfo=UTC)


def test_rows_and_run_events_report_each_run_alone(setup, storage) -> None:
    """Review finding 2 (T1): later runs of one call used to include the earlier runs' bytes."""
    summary = setup(now=NOW, max_runs=10)
    rows = {row["run"]: row for row in summary["runs"] if row["status"].startswith("extracted")}
    events = {r["run"]: r for r in _log(storage) if r["event"] == "run"}
    assert set(rows) == set(events) == {"2026111907", "2026111906", "2026111905", "2026111903"}
    for run_id, row in rows.items():
        assert row["grib_bytes"] == events[run_id]["transfer"]["bytes"]["grib"]
        assert row["grib_bytes"] == events[run_id]["planned_bytes"]
    assert rows["2026111905"]["grib_bytes"] == rows["2026111907"]["grib_bytes"]  # same shape
    assert rows["2026111905"]["bytes"] == rows["2026111907"]["bytes"]
    assert summary["transfer"]["total_bytes"] >= sum(row["bytes"] for row in rows.values())


def test_one_failing_run_does_not_stop_the_others(setup, storage, monkeypatch) -> None:
    """Review finding 4 (T4): an ecCodes error in 07Z used to abort the call before 06Z."""
    real = Extractor.extract

    def flaky(self, run_start, options=None):
        if run_start == R07:
            raise eccodes.GribInternalError("decoding failed")
        return real(self, run_start, options)

    monkeypatch.setattr(Extractor, "extract", flaky)
    summary = setup(now=NOW, max_runs=10)
    status = _status(summary)
    assert status["2026111907"] == "error: GribInternalError: decoding failed"
    for run in ("2026111906", "2026111905"):
        assert status[run] == "extracted"
    assert storage.exists(f"forecast/hrrr/{RAINIER}/2026111906z.json")
    errors = [r for r in _log(storage) if r["event"] == "error"]
    assert len(errors) == 1 and errors[0]["run"] == "2026111907"
    assert errors[0]["kind"] == "error" and errors[0]["attempts"] == 1
    assert errors[0]["next_try_utc"] == "2026-11-19T09:50:00Z" and errors[0]["bytes"] > 0
    failure = backoff.read_failure(storage, R07)
    assert failure is not None and failure.next_try_utc == NOW + timedelta(minutes=10)


def test_a_busy_lock_defers_the_rest_of_the_call(setup, storage, monkeypatch) -> None:
    """Review finding 4 (T5): LockTimeoutError used to escape the handler."""
    calls: list[datetime] = []

    def locked(self, run_start, options=None):
        calls.append(run_start)
        raise LockTimeoutError("lock 'forecast-hrrr' busy for 30.0 s")

    monkeypatch.setattr(Extractor, "extract", locked)
    status = _status(setup(now=NOW, max_runs=10))
    assert status["2026111907"].startswith("deferred: lock busy")
    for run in ("2026111906", "2026111905", "2026111903"):
        assert status[run] == "deferred: lock busy"
    assert calls == [R07]  # the other runs did not each wait 30 s for the same lock
    assert backoff.read_failure(storage, R07) is None
    assert not [r for r in _log(storage) if r["event"] == "error"]


def test_a_capped_run_is_not_replanned_every_tick(
    fake, bucket, storage, manifests, small_grid
) -> None:
    """Review finding 5 (T9): 49 inventories and an error event per tick, forever."""
    fake.add_run(R06, fields=FIELDS, hours=range(49))
    capped = replace(OPTIONS, max_bytes=1000)
    run = functools.partial(
        lambda_handler.process_pending,
        storage,
        bucket,
        {RAINIER: manifests[RAINIER]},
        grid=small_grid,
        lookback_h=4,
    )
    idx_per_tick, statuses = [], []
    for tick in range(3):
        fake.requests.clear()
        summary = run(now=NOW + timedelta(minutes=5 * tick), options=capped)
        idx_per_tick.append(sum(r.url.path.endswith(".idx") for r in fake.requests))
        statuses.append(_status(summary)["2026111906"])
    assert idx_per_tick == [49, 0, 0]
    assert statuses[0].startswith("error: BandwidthCapError")
    assert all(s.startswith("backing off: refused by the cap") for s in statuses[1:])
    assert [r["kind"] for r in _log(storage) if r["event"] == "error"] == ["cap"]
    # A higher cap is a new setting: the run is tried again at once, and the record resolved.
    summary = run(now=NOW + timedelta(minutes=15), options=replace(capped, max_bytes=10**9))
    assert _status(summary)["2026111906"] == "extracted"
    assert backoff.read_failure(storage, R06) is None
    assert storage.read_json(backoff.failure_key(R06))["resolved_utc"] == "2026-11-19T09:55:00Z"


def test_errors_back_off_exponentially_then_recover(
    fake, bucket, storage, manifests, small_grid, monkeypatch
) -> None:
    fake.add_run(R07, fields=FIELDS, hours=range(19))
    real = Extractor.extract
    broken = {"on": True}
    tried: list[datetime] = []

    def flaky(self, run_start, options=None):
        tried.append(self.clock())
        if broken["on"]:
            raise OSError(28, "No space left on device")
        return real(self, run_start, options)

    monkeypatch.setattr(Extractor, "extract", flaky)
    run = functools.partial(
        lambda_handler.process_pending,
        storage,
        bucket,
        {RAINIER: manifests[RAINIER]},
        options=OPTIONS,
        grid=small_grid,
        lookback_h=3,
    )
    for minutes in range(0, 75, 5):  # 15 ticks, 5 minutes apart
        if minutes == 70:
            broken["on"] = False
        status = _status(run(now=NOW + timedelta(minutes=minutes)))["2026111907"]
    # Tries at +0, then 10, 20 and 40 minutes after each failure: +10, +30, +70 (fixed).
    assert [int((t - NOW).total_seconds() // 60) for t in tried] == [0, 10, 30, 70]
    assert status == "extracted"
    assert [r["attempts"] for r in _log(storage) if r["event"] == "error"] == [1, 2, 3]
    assert backoff.read_failure(storage, R07) is None


def test_a_failing_listing_defers_the_rest_of_the_call(setup, fake, storage) -> None:
    fake.fail_next[""] = 100  # every ListObjectsV2 answers 503
    summary = setup(now=NOW, max_runs=10)
    status = _status(summary)
    assert status["2026111909"].startswith("error: BucketError: giving up")
    assert status["2026111907"] == "deferred: the bucket listing failed"
    assert summary["transfer"]["retries"] == 3  # one listing, four attempts
    assert [r["run"] for r in _log(storage) if r["event"] == "error"] == ["2026111909"]
    assert backoff.read_failure(storage, datetime(2026, 11, 19, 9, tzinfo=UTC)) is None
