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
    choose_run,
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
        seen["plan_bucket"] = bucket
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

    def listed_availability(bucket):
        seen["listed_bucket"] = bucket

        def lookup(run, fh):
            raise AssertionError("the fake selector never asks")

        return lookup

    def select_run_for_issue(issue, valid, lag_model=None, availability=None):
        seen["selected"] = (issue, valid)
        seen["availability"] = availability
        return _Choice(RUN, 31, 32) if seen.get("no_run") is None else None

    availability.LagModel = LagModel  # type: ignore[attr-defined]
    availability.listed_availability = listed_availability  # type: ignore[attr-defined]
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


def test_the_choice_lists_candidate_runs_on_the_pair_s_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D-054: the choice passes ``listed_availability`` for the same client the extraction
    uses, so an archived hour is never chosen before its own S3 stamps."""
    seen = _fake_forecast(monkeypatch)
    summary = fetch_pair_forecast(
        "grand-canyon.yavapai", VALID, 24.0, storage=LocalStorage(tmp_path), manifests={}
    )
    assert seen["availability"] is not None
    assert seen["listed_bucket"] is seen["plan_bucket"] and seen["bucket_closed"]
    assert summary["choice_transfer"] is None  # the fake bucket keeps no ledger


def test_a_lookout_forecast_without_the_stamp_floor_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lookout-forecast 0.1.0 (the 034c46b snapshot) has no listed_availability."""
    seen = _fake_forecast(monkeypatch)
    monkeypatch.delattr(sys.modules["lookout_forecast.availability"], "listed_availability")
    with pytest.raises(PairForecastError, match=r"0\.0\.0-fake has no listed_availability"):
        fetch_pair_forecast(
            "grand-canyon.yavapai", VALID, 24.0, storage=LocalStorage(tmp_path), manifests={}
        )
    assert "selected" not in seen and seen["extract_calls"] == []


def _hour(fh: int, available: datetime, source: str, lag_s: float, floor: str | None) -> dict:
    """One hour as an archived forecast file records it (lookout-forecast 0.2.0)."""
    return {
        "fh": fh,
        "availability_utc": available.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "availability_source": source,
        "availability_model": {"method": "rule", "lag_s": lag_s, "s3_floor_utc": floor},
    }


@pytest.mark.parametrize(
    ("how", "live", "says", "never"),
    [
        ("reused", False, "re-extract the run (the extractor's force option)", "would write"),
        ("written", False, "the run's S3 stamps changed between the choice's listing", "force"),
        ("written", True, "dated the run as live", "force"),
    ],
)
def test_a_mismatch_says_why_and_suggests_re_extracting_only_when_that_helps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str, live: bool, says: str, never: str
) -> None:
    """Review finding (D-054): the old refusal always said "re-extract", even when the
    extractor had just written the file by the stamp rule and re-extracting would change
    nothing. It now names each side's source, lag and floor, and suggests re-extracting
    only for a reused archived file."""
    seen = _fake_forecast(monkeypatch)
    storage = LocalStorage(tmp_path)
    later = AVAILABLE + timedelta(minutes=7)
    doc = {
        "live": live,
        "hours": [
            _hour(31, later, "archived-s3-lastmodified", 6000.0, "2024-01-14T14:07:00Z"),
            _hour(32, later, "archived-s3-lastmodified", 6000.0, "2024-01-14T14:07:00Z"),
        ],
    }
    if how == "reused":
        seen["covered"] = True
        storage.put_bytes(KEY, json.dumps(doc).encode())
    else:  # the extractor of this call writes the document
        extractor = sys.modules["lookout_forecast.extract"].Extractor
        monkeypatch.setattr(extractor, "extract", _writing(doc, seen))
    with pytest.raises(PairForecastError) as caught:
        fetch_pair_forecast("grand-canyon.yavapai", VALID, 24.0, storage=storage, manifests={})
    message = str(caught.value)
    assert f"({how}) records f31-f32 available at 2024-01-14T14:07:00Z" in message
    assert "f31: archived-s3-lastmodified, model rule 6000 s, S3 floor 2024-01-14T14:07:00Z" in (
        message
    )
    assert "the choice gives 2024-01-14T14:00:00Z" in message
    assert says in message and never not in message


def test_a_mismatch_names_the_deciding_hour_and_an_hour_with_no_trusted_stamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review finding (no test pinned these): the refusal names the hour that sets the file's
    availability, the later of the two, and tells an hour whose S3 stamps were all rewritten
    (floor None: no trusted stamp) from one written before the floor existed (no field)."""
    seen = _fake_forecast(monkeypatch)
    seen["covered"] = True
    storage = LocalStorage(tmp_path)
    stamped = "archived-s3-lastmodified"
    doc = {
        "live": False,
        "hours": [
            _hour(31, AVAILABLE + timedelta(minutes=7), stamped, 6000.0, "2024-01-14T14:07:00Z"),
            _hour(32, AVAILABLE + timedelta(minutes=9), "modeled", 7740.0, None),
        ],
    }
    storage.put_bytes(KEY, json.dumps(doc).encode())
    with pytest.raises(PairForecastError) as caught:
        fetch_pair_forecast("grand-canyon.yavapai", VALID, 24.0, storage=storage, manifests={})
    message = str(caught.value)
    assert "(reused) records f31-f32 available at 2024-01-14T14:09:00Z (f32: modeled, model " in (
        message
    )
    assert "rule 7740 s, no trusted S3 stamp)" in message and "f31:" not in message
    assert "S3 floor not recorded" in message  # the stand-in choice records no floor field


def _writing(doc: dict, seen: dict):
    """A fake ``Extractor.extract`` that writes ``doc`` as the forecast file."""

    def extract(self, run_start, options):
        key = f"forecast/hrrr/{options.viewpoint_ids[0]}/{run_start:%Y%m%d%H}z.json"
        self.storage.put_bytes(key, json.dumps(doc).encode())
        seen["extract_calls"].append((run_start, [31, 32], options))
        return _Result([31, 32], [key])

    return extract


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


# --- the stamp floor on recorded S3 listings (D-054; needs lookout-forecast 0.2.0) -------------


FIXTURES = FORECAST_TESTS / "fixtures"
RUN18 = RUN + timedelta(hours=6)
ISSUE = VALID - timedelta(hours=24)  # the M0.5 pair's issue time, 2024-01-14T19:45Z
#: The later of each hour's recorded GRIB2 and .idx stamps (``list_20240114_t1?z_full.xml``).
F32_12Z = datetime(2024, 1, 14, 13, 29, 37, tzinfo=UTC)
F31_12Z = datetime(2024, 1, 14, 13, 28, 11, tzinfo=UTC)
F26_18Z = datetime(2024, 1, 14, 19, 24, 33, tzinfo=UTC)


def z(moment: datetime) -> str:
    """A time as the forecast code writes it: RFC 3339 UTC, whole seconds."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _replay(asked: list[str], *runs: datetime) -> object:
    """Serve the recorded S3 listing of each run in lookout-forecast's tests/fixtures, and fail
    any other request: nothing here is fetched or invented."""
    import httpx
    from lookout_forecast.hrrr import run_prefix

    answers = {
        run_prefix(run): (FIXTURES / f"list_{run:%Y%m%d}_t{run:%H}z_full.xml").read_bytes()
        for run in runs
    }

    def handler(request: httpx.Request) -> httpx.Response:
        prefix = request.url.params.get("prefix", "")
        asked.append(prefix)
        if prefix not in answers:
            raise AssertionError(f"no recorded listing for {prefix!r}")
        return httpx.Response(200, content=answers[prefix])

    return httpx.MockTransport(handler)


def _low_model(*cycles: int) -> object:
    """20 lags of 40 min for every hour of these cycles (rule 1): earlier than any recorded
    stamp, as the November-fitted model was for some older runs (D-054)."""
    from lookout_forecast.availability import LagModel

    return LagModel.from_events(
        {"event": "lag", "run": f"202611{day + 1:02d}{cycle:02d}", "fh": fh, "lag_s": 2400.0}
        for cycle in cycles
        for day in range(20)
        for fh in range(49)
    )


def test_the_m05_pair_s_choice_is_unchanged_by_the_stamp_floor(
    tmp_path: Path, real_forecast: types.ModuleType
) -> None:
    """The recorded pair (docs/evidence/m05-backtest-pair.md): with the lag model of its day
    (the conservative 120 min for these hours), 2024011418 is skipped at 20:00Z and 2024011412
    f31/f32 is chosen at 14:00Z. Its stamps (13:28:11Z, 13:29:37Z) are earlier than the model,
    so the floor changes nothing."""
    from lookout_forecast.availability import LagModel
    from lookout_forecast.bucket import HrrrBucket
    from lookout_forecast.hrrr import run_prefix

    asked: list[str] = []
    with HrrrBucket(transport=_replay(asked, RUN, RUN18), sleep=lambda s: None) as bucket:
        choice, _ = choose_run(
            ISSUE, VALID, storage=LocalStorage(tmp_path), bucket=bucket, lag_model=LagModel()
        )
        listings = bucket.ledger.snapshot()["requests"]
    assert (choice.run_id, choice.fh_lo, choice.fh_hi, choice.weight_hi) == (
        "2024011412",
        31,
        32,
        0.75,
    )
    assert choice.availability_utc == AVAILABLE and choice.availability_source == "modeled"
    assert (choice.lead_h, choice.lead_from_run_start_h) == (29.75, 31.75)
    assert choice.skipped == (("2024011418", "not available until 2024-01-14T20:00:00Z"),)
    note = choice.availability_model
    assert note["method"] == "conservative-fallback" and note["lag_s"] == 7200.0
    assert note["s3_floor_utc"] in (z(F31_12Z), z(F32_12Z)) and note["s3_stamps_ignored"] == []
    assert asked == [run_prefix(RUN18), run_prefix(RUN)] and listings == {"listing": 2}


def test_a_run_whose_stamps_are_later_than_the_model_is_chosen_by_its_stamps(
    tmp_path: Path, real_forecast: types.ModuleType
) -> None:
    """D-054's finding on recorded listings: a model earlier than the stamps would choose
    2024011418 at 18:50Z, half an hour before f26 was on S3. With the floor it is skipped
    until 19:24:33Z, its f26 stamp, and 2024011412 is chosen by its own stamps."""
    from lookout_forecast.availability import select_run_for_issue
    from lookout_forecast.bucket import HrrrBucket

    model = _low_model(12, 18)
    early = datetime(2024, 1, 14, 18, 50, tzinfo=UTC)
    alone = select_run_for_issue(early, VALID, lag_model=model)  # the old choice: the model only
    assert alone is not None and alone.run_id == "2024011418"
    assert alone.availability_utc == RUN18 + timedelta(minutes=40) < F26_18Z  # the leak
    asked: list[str] = []
    storage = LocalStorage(tmp_path)
    with HrrrBucket(transport=_replay(asked, RUN, RUN18), sleep=lambda s: None) as bucket:
        choice, _ = choose_run(early, VALID, storage=storage, bucket=bucket, lag_model=model)
        at_stamp, _ = choose_run(F26_18Z, VALID, storage=storage, bucket=bucket, lag_model=model)
    assert choice.skipped == (("2024011418", f"not available until {z(F26_18Z)}"),)
    assert (choice.run_id, choice.availability_utc) == ("2024011412", F32_12Z)
    assert choice.availability_source == "archived-s3-lastmodified"
    assert choice.availability_model["s3_floor_utc"] == z(F32_12Z)
    assert choice.availability_model["lag_s"] == 2400.0  # the model it beat
    assert (at_stamp.run_id, at_stamp.availability_utc) == ("2024011418", F26_18Z)
    assert at_stamp.availability_source == "archived-s3-lastmodified"


def test_rewritten_idx_stamps_never_hold_a_run_back(
    tmp_path: Path, real_forecast: types.ModuleType
) -> None:
    """2020120312's recorded listing has every .idx stamped 17 Feb 2021 (a rewrite): the
    choice ignores it and floors f30 at its GRIB2 stamp (13:26:24Z) + 300 s."""
    from lookout_forecast.bucket import HrrrBucket

    run = datetime(2020, 12, 3, 12, tzinfo=UTC)
    asked: list[str] = []
    with HrrrBucket(transport=_replay(asked, run), sleep=lambda s: None) as bucket:
        choice, _ = choose_run(
            datetime(2020, 12, 3, 14, tzinfo=UTC),
            datetime(2020, 12, 4, 18, tzinfo=UTC),
            storage=LocalStorage(tmp_path),
            bucket=bucket,
            lag_model=_low_model(12),
        )
    assert (choice.run_id, choice.fh_lo, choice.fh_hi) == ("2020120312", 30, 30)
    assert choice.availability_utc == datetime(2020, 12, 3, 13, 31, 24, tzinfo=UTC)
    assert choice.availability_source == "archived-s3-lastmodified"
    assert choice.availability_model["s3_stamps_ignored"] == ["idx"]


def test_the_pair_s_fresh_file_records_its_choice_when_the_stamps_win(
    tmp_path: Path, real_forecast: types.ModuleType
) -> None:
    """The whole pair path on lookout-forecast's own code, offline (its fake bucket): with a
    lag log that models the 12Z hours at 40 min, earlier than their stamps, the choice and the
    file it writes agree on the stamps. Before the fix the choice used the model alone and
    the pair was refused with a misleading "re-extract" hint."""
    fakebucket = real_forecast
    from lookout_common.manifest import load_manifests
    from lookout_forecast.bucket import HrrrBucket
    from lookout_forecast.grid import HRRR_CONUS

    manifests = load_manifests(Path(__file__).resolve().parents[2] / "viewpoints" / "manifests")
    vp = manifests["grand-canyon.yavapai"]
    ci, cj = HRRR_CONUS.nearest_ij(vp.camera.lat, vp.camera.lon)
    grid = fakebucket.sub_grid(ci - 20, cj - 20, 40, 40)
    fake = fakebucket.FakeBucket(grid=grid)
    fake.add_run(RUN, fields=[*SCENE_FIELDS, "HGT:surface"], hours=range(49))
    f32 = RUN + timedelta(seconds=3000 + 60 * 32 + 4)  # the fake's f32 .idx stamp, 13:22:04Z
    storage = LocalStorage(tmp_path / "data")
    for day in range(20):  # a live lag log: 40 min for every 12Z hour, 20 runs (rule 1)
        storage.append_jsonl_many(
            f"forecast_log/2026-11-{day + 1:02d}.jsonl",
            [
                {
                    "t": f"2026-11-{day + 1:02d}T13:00:00Z",
                    "event": "lag",
                    "fh": fh,
                    "run": f"202611{day + 1:02d}12",
                    "lag_s": 2400.0,
                }
                for fh in range(49)
            ],
        )
    clock_value = RUN + timedelta(days=3)  # long after the run: archived
    with HrrrBucket(transport=fake.transport(), sleep=lambda s: None) as bucket:
        summary = fetch_pair_forecast(
            "grand-canyon.yavapai",
            VALID,
            24.0,
            storage=storage,
            manifests={"grand-canyon.yavapai": vp},
            bucket=bucket,
            extractor_options={
                "grid": grid,
                "cache_root": tmp_path / "cache" / "hrrr",
                "clock": lambda: clock_value,
            },
        )
    choice = summary["choice"]
    assert choice["run"] == "2024011412" and choice["availability_utc"] == z(f32)
    assert choice["availability_source"] == "archived-s3-lastmodified"
    assert choice["availability_model"]["lag_s"] == 2400.0
    assert summary["file"]["availability_utc"] == choice["availability_utc"]
    doc = storage.read_json(KEY)
    assert {h["fh"]: h["availability_source"] for h in doc["hours"]} == {
        31: "archived-s3-lastmodified",
        32: "archived-s3-lastmodified",
    }
    # The choice listed 2024011418 (not in this bucket: skipped) and 2024011412.
    assert summary["choice_transfer"]["requests"] == {"listing": 2}
    assert choice["skipped"] == [["2024011418", "f25 not stored"]]


def _pair_with_stamps(
    tmp_path: Path, fakebucket: types.ModuleType, rewritten: tuple[str, ...]
) -> tuple[dict, dict]:
    """The whole pair path, as in the test above, with some of run 2024011412's S3 stamps
    rewritten 30 days after the run (D-054: a stamp over 24 h after the run start is a rewrite
    and is ignored). Returns the summary and the forecast file."""
    from lookout_common.manifest import load_manifests
    from lookout_forecast.bucket import HrrrBucket
    from lookout_forecast.grid import HRRR_CONUS

    manifests = load_manifests(Path(__file__).resolve().parents[2] / "viewpoints" / "manifests")
    vp = manifests["grand-canyon.yavapai"]
    ci, cj = HRRR_CONUS.nearest_ij(vp.camera.lat, vp.camera.lon)
    grid = fakebucket.sub_grid(ci - 20, cj - 20, 40, 40)
    fake = fakebucket.FakeBucket(grid=grid)
    fake.add_run(RUN, fields=[*SCENE_FIELDS, "HGT:surface"], hours=range(49))
    for files in fake.files.values():
        if "grib" in rewritten:
            files.grib_modified = RUN + timedelta(days=30)
        if "idx" in rewritten:
            files.idx_modified = RUN + timedelta(days=30)
    storage = LocalStorage(tmp_path / "data")
    for day in range(20):  # 40 min for every 12Z hour, 20 runs (rule 1)
        storage.append_jsonl_many(
            f"forecast_log/2026-11-{day + 1:02d}.jsonl",
            [
                {
                    "t": f"2026-11-{day + 1:02d}T13:00:00Z",
                    "event": "lag",
                    "fh": fh,
                    "run": f"202611{day + 1:02d}12",
                    "lag_s": 2400.0,
                }
                for fh in range(49)
            ],
        )
    clock_value = RUN + timedelta(days=3)
    with HrrrBucket(transport=fake.transport(), sleep=lambda s: None) as bucket:
        summary = fetch_pair_forecast(
            "grand-canyon.yavapai",
            VALID,
            24.0,
            storage=storage,
            manifests={"grand-canyon.yavapai": vp},
            bucket=bucket,
            extractor_options={
                "grid": grid,
                "cache_root": tmp_path / "cache" / "hrrr",
                "clock": lambda: clock_value,
            },
        )
    return summary, storage.read_json(KEY)


def test_the_pair_s_file_agrees_with_its_choice_when_the_idx_stamps_were_rewritten(
    tmp_path: Path, real_forecast: types.ModuleType
) -> None:
    """Review finding (the whole path was tested only with trusted stamps): with every .idx
    stamp rewritten, the choice and the file it writes both floor f32 at its GRIB2 stamp
    (13:22:00Z) + 300 s, and both name the ignored stamp."""
    from lookout_forecast.discovery import IDX_AFTER_GRIB_MARGIN_S

    summary, doc = _pair_with_stamps(tmp_path, real_forecast, ("idx",))
    choice = summary["choice"]
    f32_grib = RUN + timedelta(seconds=3000 + 60 * 32)  # the fake's f32 GRIB2 stamp
    assert choice["run"] == "2024011412"
    assert choice["availability_utc"] == z(f32_grib + timedelta(seconds=IDX_AFTER_GRIB_MARGIN_S))
    assert choice["availability_source"] == "archived-s3-lastmodified"
    assert choice["availability_model"]["s3_stamps_ignored"] == ["idx"]
    assert summary["file"]["availability_utc"] == choice["availability_utc"]
    hours = {h["fh"]: h for h in doc["hours"]}
    assert hours[32]["availability_model"]["s3_stamps_ignored"] == ["idx"]


def test_the_pair_s_file_agrees_with_its_choice_when_every_stamp_was_rewritten(
    tmp_path: Path, real_forecast: types.ModuleType
) -> None:
    """With both stamps rewritten no stamp is trusted: the choice and the file use the lag
    model alone (40 min, rule 1), and the choice records no floor."""
    summary, doc = _pair_with_stamps(tmp_path, real_forecast, ("grib", "idx"))
    choice = summary["choice"]
    assert choice["run"] == "2024011412"
    assert choice["availability_utc"] == z(RUN + timedelta(minutes=40))
    assert choice["availability_source"] == "modeled"
    assert choice["availability_model"]["s3_floor_utc"] is None
    assert choice["availability_model"]["s3_stamps_ignored"] == ["grib", "idx"]
    assert summary["file"]["availability_utc"] == choice["availability_utc"]
    assert {h["availability_source"] for h in doc["hours"]} == {"modeled"}
