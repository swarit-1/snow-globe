"""The pair's forecast run: lookout-forecast's choice, only the needed fields and hours.

Most tests use small stand-ins for lookout-forecast's modules, so they need neither ecCodes
nor lookout-forecast. The last test runs the real extractor offline against lookout-forecast's
fake bucket (synthetic GRIB2). eval's dev group installs ecCodes for it, and it puts
``workers/forecast/src`` on ``sys.path`` for itself only (the ``real_forecast`` fixture), so it
runs in ``make test``. It skips only where ecCodes is missing, or where the checkout has no
``workers/forecast``; with both present, a lookout-forecast or fake bucket that does not import
fails it, so the test cannot drop out of ``make test`` unseen.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

import pytest
from lookout_common.storage import LocalStorage

from lookout_eval import forecast_io
from lookout_eval.pair_forecast import (
    DEFAULT_MAX_MB,
    SCENE_FIELDS,
    PairForecastError,
    extract_hours,
    fetch_pair_forecast,
    only_hours,
    stored_availability,
)

FORECAST_TESTS = Path(__file__).resolve().parents[2] / "workers" / "forecast" / "tests"
RUN = datetime(2024, 1, 14, 12, tzinfo=UTC)
VALID = datetime(2024, 1, 15, 19, 45, tzinfo=UTC)
#: The fake choice's modelled availability: run start + the conservative 120 min.
AVAILABLE = RUN + timedelta(minutes=120)
KEY = "forecast/hrrr/grand-canyon.yavapai/2024011412z.json"


def _forecast_doc(available: datetime = AVAILABLE, hours: tuple[int, ...] = (31, 32)) -> bytes:
    """The part of a forecast file the summary reads: each hour's recorded availability."""
    stamp = available.strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"hours": [{"fh": fh, "availability_utc": stamp} for fh in hours]}).encode()


def test_scene_fields_cover_every_field_the_scene_and_s_rule_read() -> None:
    read = {
        forecast_io.CEILING,
        forecast_io.CLOUD_BASE,
        forecast_io.CLOUD_TOP,
        forecast_io.LOW_CLOUD,
        forecast_io.MID_CLOUD,
        forecast_io.HIGH_CLOUD,
        forecast_io.VISIBILITY,
        forecast_io.PRECIP_RATE,
        forecast_io.U_WIND,
        forecast_io.V_WIND,
    }
    assert read <= set(SCENE_FIELDS)
    assert "PRES:cloud base" in SCENE_FIELDS  # the cross-check for HGT:cloud base
    assert len(set(SCENE_FIELDS)) == len(SCENE_FIELDS) and DEFAULT_MAX_MB == 60.0


# --- stand-ins for lookout-forecast -------------------------------------------------------


class _Choice:
    def __init__(self, run_start: datetime, fh_lo: int, fh_hi: int) -> None:
        self.run_start_utc, self.fh_lo, self.fh_hi = run_start, fh_lo, fh_hi
        self.availability_utc = AVAILABLE

    def to_dict(self) -> dict:
        return {
            "run": f"{self.run_start_utc:%Y%m%d%H}",
            "fh_lo": self.fh_lo,
            "fh_hi": self.fh_hi,
            "availability_utc": self.availability_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


class _Result:
    def __init__(self, hours: list[int], written: list[str]) -> None:
        self.hours, self.written = hours, written

    def to_event(self) -> dict:
        return {"event": "run", "hours": self.hours, "transfer": {"bytes": {"grib": 1}}}


def _fake_forecast(monkeypatch: pytest.MonkeyPatch, *, bytes_per_hour: int = 1_000_000) -> dict:
    """Install a fake lookout_forecast package; returns what its pieces saw."""
    seen: dict = {"extract_calls": [], "plans": []}

    def run_hours(run_start: datetime, limit: int | None = None) -> list[int]:
        last = 48 if run_start.hour % 6 == 0 else 18
        return list(range((last if limit is None else min(last, limit)) + 1))

    extract = types.ModuleType("lookout_forecast.extract")
    extract.run_hours = run_hours  # type: ignore[attr-defined]

    def plan_run(bucket, run_start, *, fields=(), max_fh=None, workers=4):
        hours = extract.run_hours(run_start, max_fh)  # the module global, as the real one does
        seen["plans"].append(hours)
        return {
            "hours": hours,
            "total_bytes": bytes_per_hour * len(hours),
            "bytes_by_fh": {fh: bytes_per_hour for fh in hours},
            "bytes_by_field_by_fh": {},
            "fields_missing_from_idx": {},
            "transfer": {"requests": {"idx": len(hours)}},
        }

    class ExtractOptions:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class Extractor:
        def __init__(self, storage, bucket, manifests, lag_model=None) -> None:
            self.storage = storage

        def extract(self, run_start, options):
            hours = extract.run_hours(run_start, None)
            seen["extract_calls"].append((run_start, hours, options))
            key = f"forecast/hrrr/{options.viewpoint_ids[0]}/{run_start:%Y%m%d%H}z.json"
            self.storage.put_bytes(key, _forecast_doc(seen.get("writes_available", AVAILABLE)))
            return _Result(hours if seen.get("lie") is None else seen["lie"], [key])

    def run_covered(storage, run_start, **kwargs) -> bool:
        return bool(seen.get("covered"))

    extract.plan_run = plan_run  # type: ignore[attr-defined]
    extract.ExtractOptions = ExtractOptions  # type: ignore[attr-defined]
    extract.Extractor = Extractor  # type: ignore[attr-defined]
    extract.run_covered = run_covered  # type: ignore[attr-defined]

    availability = types.ModuleType("lookout_forecast.availability")

    class LagModel:
        @classmethod
        def from_storage(cls, storage):
            return cls()

    def select_run_for_issue(issue, valid, lag_model=None):
        seen["selected"] = (issue, valid)
        return _Choice(RUN, 31, 32) if seen.get("no_run") is None else None

    availability.LagModel = LagModel  # type: ignore[attr-defined]
    availability.select_run_for_issue = select_run_for_issue  # type: ignore[attr-defined]

    fields = types.ModuleType("lookout_forecast.fields")
    fields.resolve_fields = lambda names: list(names)  # type: ignore[attr-defined]
    hrrr = types.ModuleType("lookout_forecast.hrrr")
    hrrr.STORAGE_SOURCE = "hrrr"  # type: ignore[attr-defined]
    bucket = types.ModuleType("lookout_forecast.bucket")

    class HrrrBucket:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            seen["bucket_closed"] = True

    bucket.HrrrBucket = HrrrBucket  # type: ignore[attr-defined]
    package = types.ModuleType("lookout_forecast")
    package.__version__ = "0.0.0-fake"  # type: ignore[attr-defined]
    package.__file__ = "/fake/lookout_forecast/__init__.py"
    for name, module in {
        "availability": availability,
        "extract": extract,
        "fields": fields,
        "hrrr": hrrr,
        "bucket": bucket,
    }.items():
        setattr(package, name, module)
        monkeypatch.setitem(sys.modules, f"lookout_forecast.{name}", module)
    monkeypatch.setitem(sys.modules, "lookout_forecast", package)
    seen["extract_module"] = extract
    seen["original_run_hours"] = run_hours
    return seen


def test_only_hours_substitutes_and_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_forecast(monkeypatch)
    module = seen["extract_module"]
    with only_hours([32, 31, 31]) as wanted:
        assert wanted == [31, 32]
        assert module.run_hours(RUN) == [31, 32]
        assert module.run_hours(RUN, 31) == [31]
        with pytest.raises(PairForecastError, match="does not produce"):
            module.run_hours(datetime(2024, 1, 14, 13, tzinfo=UTC))  # an 18 h cycle
    assert module.run_hours is seen["original_run_hours"]
    with pytest.raises(RuntimeError), only_hours([31]):
        raise RuntimeError("boom")
    assert module.run_hours is seen["original_run_hours"]  # restored after an error too


def test_extract_hours_fetches_only_the_bracketing_hours(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _fake_forecast(monkeypatch)
    storage = LocalStorage(tmp_path)
    done = extract_hours(
        storage=storage,
        bucket=object(),
        manifests={},
        run_start=RUN,
        hours=[31, 32],
        viewpoint_id="grand-canyon.yavapai",
    )
    assert seen["plans"] == [[31, 32]]
    (run_start, hours, options) = seen["extract_calls"][0]
    assert run_start == RUN and hours == [31, 32]
    assert options.viewpoint_ids == ("grand-canyon.yavapai",)
    assert options.max_bytes == int(DEFAULT_MAX_MB * 1e6)
    assert list(options.fields) == list(SCENE_FIELDS)
    assert done["result"]["hours"] == [31, 32]
    assert seen["extract_module"].run_hours is seen["original_run_hours"]


def test_the_cap_refuses_before_any_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _fake_forecast(monkeypatch, bytes_per_hour=31_000_000)
    with pytest.raises(PairForecastError, match="exceeds the cap of 60 MB; nothing downloaded"):
        extract_hours(
            storage=LocalStorage(tmp_path),
            bucket=object(),
            manifests={},
            run_start=RUN,
            hours=[31, 32],
            viewpoint_id="grand-canyon.yavapai",
        )
    assert seen["extract_calls"] == []


def test_the_cap_counts_every_hour_if_the_substitution_is_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review finding: nothing showed what happens if the planner stops seeing the patched
    ``run_hours``. It then plans f00-f48 of the 12Z run (49 hours, 808 MB at 16.5 MB an hour,
    far past the 60 MB cap), and the pair refuses before any extraction."""
    seen = _fake_forecast(monkeypatch, bytes_per_hour=16_500_000)
    module = seen["extract_module"]
    original = seen["original_run_hours"]
    real_plan_run = module.plan_run

    def plan_run_ignoring_the_patch(bucket, run_start, *, fields=(), max_fh=None, workers=4):
        patched = module.run_hours
        module.run_hours = original  # as if plan_run had bound run_hours at import
        try:
            return real_plan_run(bucket, run_start, fields=fields, max_fh=max_fh, workers=workers)
        finally:
            module.run_hours = patched

    monkeypatch.setattr(module, "plan_run", plan_run_ignoring_the_patch)
    with pytest.raises(PairForecastError, match=r"planned hours \[0, 1, 2, .*48\] differ"):
        extract_hours(
            storage=LocalStorage(tmp_path),
            bucket=object(),
            manifests={},
            run_start=RUN,
            hours=[31, 32],
            viewpoint_id="grand-canyon.yavapai",
        )
    (planned,) = seen["plans"]
    assert planned == list(range(49))
    assert 16_500_000 * len(planned) > DEFAULT_MAX_MB * 1e6  # the cap would refuse it too
    assert seen["extract_calls"] == [] and not (tmp_path / "forecast").exists()
    assert module.run_hours is original  # the substitution is undone


def test_other_hours_than_asked_are_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _fake_forecast(monkeypatch)
    seen["lie"] = list(range(33))  # as if the substitution had not taken
    with pytest.raises(PairForecastError, match="extracted hours"):
        extract_hours(
            storage=LocalStorage(tmp_path),
            bucket=object(),
            manifests={},
            run_start=RUN,
            hours=[31, 32],
            viewpoint_id="grand-canyon.yavapai",
        )


def test_fetch_chooses_extracts_and_hashes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _fake_forecast(monkeypatch)
    storage = LocalStorage(tmp_path)
    summary = fetch_pair_forecast(
        "grand-canyon.yavapai", VALID, 24.0, storage=storage, manifests={}
    )
    assert seen["selected"] == (VALID - timedelta(hours=24), VALID)
    assert summary["hours"] == [31, 32] and summary["reused_existing_file"] is False
    assert summary["issue_time_utc"] == "2024-01-14T19:45:00Z"
    assert summary["file"]["key"] == KEY
    assert len(summary["file"]["sha256"]) == 64 and seen["bucket_closed"]
    assert summary["file"]["availability_utc"] == summary["choice"]["availability_utc"]
    assert summary["code"]["lookout_forecast_version"] == "0.0.0-fake"


def test_fetch_reuses_a_file_that_already_covers_the_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _fake_forecast(monkeypatch)
    storage = LocalStorage(tmp_path)
    storage.put_bytes(KEY, _forecast_doc())
    seen["covered"] = True
    summary = fetch_pair_forecast(
        "grand-canyon.yavapai", VALID, 24.0, storage=storage, manifests={}
    )
    assert summary["reused_existing_file"] is True and summary["result"] is None
    assert seen["extract_calls"] == [] and seen["plans"] == [[31, 32]]  # inventories only
    assert summary["file"]["availability_utc"] == "2024-01-14T14:00:00Z"


def test_a_reused_file_with_another_availability_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review finding: a reused file keeps the availability modelled when it was extracted,
    while the choice uses today's lag model, and the pair's leakage check reads the file. A
    file that disagrees with the choice is refused, earlier or later."""
    seen = _fake_forecast(monkeypatch)
    storage = LocalStorage(tmp_path)
    seen["covered"] = True
    for stored in (AVAILABLE - timedelta(minutes=30), AVAILABLE + timedelta(minutes=5)):
        storage.put_bytes(KEY, _forecast_doc(stored))
        with pytest.raises(PairForecastError, match=r"\(reused\) records f31-f32 available at"):
            fetch_pair_forecast("grand-canyon.yavapai", VALID, 24.0, storage=storage, manifests={})
    assert seen["extract_calls"] == []
    # One hour missing from the file is refused too.
    storage.put_bytes(KEY, _forecast_doc(hours=(31,)))
    with pytest.raises(PairForecastError, match="no availability for f32"):
        fetch_pair_forecast("grand-canyon.yavapai", VALID, 24.0, storage=storage, manifests={})


def test_a_fresh_file_must_record_the_choice_s_availability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _fake_forecast(monkeypatch)
    seen["writes_available"] = AVAILABLE + timedelta(minutes=1)
    with pytest.raises(PairForecastError, match=r"\(written\) records"):
        fetch_pair_forecast(
            "grand-canyon.yavapai", VALID, 24.0, storage=LocalStorage(tmp_path), manifests={}
        )


def test_stored_availability_is_the_later_of_the_two_hours() -> None:
    doc = {
        "hours": [
            {"fh": 31, "availability_utc": "2024-01-14T13:58:00Z"},
            {"fh": 32, "availability_utc": "2024-01-14T14:00:00+00:00"},
            {"fh": 33, "availability_utc": "2024-01-14T14:05:00Z"},
        ]
    }
    assert stored_availability(doc, [31, 32]) == AVAILABLE
    assert stored_availability(doc, [33]) == AVAILABLE + timedelta(minutes=5)
    with pytest.raises(PairForecastError, match="no availability for f34"):
        stored_availability(doc, [33, 34])


def test_no_run_is_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _fake_forecast(monkeypatch)
    seen["no_run"] = True
    with pytest.raises(PairForecastError, match="no HRRR run covers"):
        fetch_pair_forecast(
            "grand-canyon.yavapai", VALID, 24.0, storage=LocalStorage(tmp_path), manifests={}
        )


# --- the real extractor, offline (needs ecCodes) -------------------------------------------


FORECAST_SRC = FORECAST_TESTS.parent / "src"
FAKEBUCKET = "lookout_forecast_fakebucket"


def _is_forecast_module(name: str) -> bool:
    return name == FAKEBUCKET or name.split(".", 1)[0] == "lookout_forecast"


@pytest.fixture
def real_forecast(monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
    """lookout-forecast's fake bucket module, with the real lookout-forecast importable.

    eval's dev group installs ecCodes at the forecast worker's versions. Where lookout-forecast
    is not installed (the default), its source goes on ``sys.path`` for this test only, and
    every ``lookout_forecast`` module it imported is dropped afterwards, so tests that check
    eval without lookout-forecast (``backtest-pair --forecast auto``) still see it missing.
    With ecCodes and the checkout's ``workers/forecast`` both present, an import failure is a
    test failure, not a skip (module docstring).
    """
    if importlib.util.find_spec("eccodes") is None:
        pytest.skip("ecCodes is not installed here (uv sync installs it with eval's dev group)")
    in_checkout = FORECAST_SRC.is_dir()

    def missing(reason: str) -> NoReturn:
        if in_checkout:
            pytest.fail(f"{reason}, although ecCodes and {FORECAST_SRC} are present")
        pytest.skip(reason)

    before = {name for name in sys.modules if _is_forecast_module(name)}
    if importlib.util.find_spec("lookout_forecast") is None and in_checkout:
        monkeypatch.syspath_prepend(str(FORECAST_SRC))
    try:
        try:
            from lookout_forecast.extract import Extractor  # noqa: F401
        except ImportError as exc:
            missing(f"lookout-forecast does not import here: {exc}")
        source = FORECAST_TESTS / "fakebucket.py"
        spec = importlib.util.spec_from_file_location(FAKEBUCKET, source)
        if not source.is_file() or spec is None or spec.loader is None:
            missing(f"{source} not found")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses look their module up while it executes
        spec.loader.exec_module(module)
        yield module
    finally:
        for name in [n for n in sys.modules if _is_forecast_module(n) and n not in before]:
            del sys.modules[name]


def test_the_real_extractor_fetches_only_the_two_hours(
    tmp_path: Path, real_forecast: types.ModuleType
) -> None:
    fakebucket = real_forecast
    from lookout_common.manifest import load_manifests
    from lookout_forecast.bucket import HrrrBucket
    from lookout_forecast.grid import HRRR_CONUS
    from lookout_forecast.hrrr import object_key

    manifests = load_manifests(Path(__file__).resolve().parents[2] / "viewpoints" / "manifests")
    cam = manifests["grand-canyon.yavapai"].camera
    ci, cj = HRRR_CONUS.nearest_ij(cam.lat, cam.lon)
    grid = fakebucket.sub_grid(ci - 20, cj - 20, 40, 40)
    fake = fakebucket.FakeBucket(grid=grid)
    fake.add_run(RUN, fields=[*SCENE_FIELDS, "HGT:surface"], hours=range(49))
    storage = LocalStorage(tmp_path / "data")
    clock_value = RUN + timedelta(days=3)  # long after the run: an archived run
    with HrrrBucket(transport=fake.transport(), sleep=lambda s: None) as bucket:
        done = extract_hours(
            storage=storage,
            bucket=bucket,
            manifests={"grand-canyon.yavapai": manifests["grand-canyon.yavapai"]},
            run_start=RUN,
            hours=[31, 32],
            viewpoint_id="grand-canyon.yavapai",
            extractor_options={
                "grid": grid,
                "cache_root": tmp_path / "cache" / "hrrr",
                "clock": lambda: clock_value,
            },
        )
    assert done["result"]["hours"] == [31, 32]
    asked = {request.url.path.lstrip("/") for request in fake.grib_requests()}
    assert asked == {object_key(RUN, 31), object_key(RUN, 32)}
    doc = storage.read_json("forecast/hrrr/grand-canyon.yavapai/2024011412z.json")
    assert doc["forecast_hours"] == [31, 32] and doc["missing_hours"] == []
    assert all(h["availability_source"] == "modeled" for h in doc["hours"])
