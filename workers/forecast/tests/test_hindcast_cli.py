"""``snow-globe-forecast hindcast plan|fetch|compare|leads|status`` on the fake bucket (PRD §13.4).

The CLI's own quiet window is kept (``QuietWindow`` with a fixed clock outside :50-:05 and a
sleep that must not be called), so every request goes through it as in a real run.
"""

from __future__ import annotations

import functools
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from snow_globe_common.jsonl import read_jsonl
from snow_globe_common.storage import LocalStorage

from snow_globe_forecast import cli
from snow_globe_forecast.hindcast import cli as hindcast_cli
from snow_globe_forecast.hindcast.cachebucket import LEDGER_KEY, CachingBucket
from snow_globe_forecast.hindcast.extractor import HindcastExtractor
from snow_globe_forecast.hindcast.plan import LIVE_FIELD_NAMES
from snow_globe_forecast.hindcast.quiet import QuietWindow
from snow_globe_forecast.hindcast.storage import HindcastStorage

from .conftest import FIXTURES, RAINIER
from .fakebucket import FakeBucket

R12 = datetime(2026, 11, 20, 12, tzinfo=UTC)
R18 = datetime(2026, 11, 20, 18, tzinfo=UTC)
#: A moment outside the quiet window, for the CLI's QuietWindow.
OPEN = datetime(2026, 11, 23, 12, 20, tzinfo=UTC)


def _no_sleep(seconds: float) -> None:
    raise AssertionError(f"the quiet window slept {seconds} s outside :50-:05")


@pytest.fixture
def offline_cli(data_root: Path, small_grid, monkeypatch: pytest.MonkeyPatch) -> FakeBucket:
    """The CLI wired to the fake bucket (its quiet window on a fixed open clock, the small
    grid) and two frames."""
    fake = FakeBucket(grid=small_grid)
    for run in (R12, R18):
        fake.add_run(
            run,
            fields=list(LIVE_FIELD_NAMES),
            hours=range(24, 34),
            lag_s={fh: 3000.0 + 120.0 * fh for fh in range(49)},
        )
    (data_root / "forecast_log").mkdir()
    shutil.copy(
        FIXTURES / "hindcast" / "live_lag_events.jsonl", data_root / "forecast_log/2026-11-20.jsonl"
    )
    folder = data_root / "frames" / RAINIER / "2026-11-21"
    folder.mkdir(parents=True)
    for stamp, sha in (("20261121T182100Z", "a" * 64), ("20261121T200500Z", "b" * 64)):
        moment = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        body = {"image_time_utc": moment.isoformat().replace("+00:00", "Z"), "sha256": sha}
        (folder / f"{stamp}_{sha[:12]}.json").write_text(json.dumps(body))

    def bucket(root, **kw):
        assert isinstance(kw["quiet"], QuietWindow) and kw["cap_bytes"] > 0
        return CachingBucket(root, transport=fake.transport(), sleep=lambda _: None, **kw)

    monkeypatch.setattr(hindcast_cli, "CachingBucket", bucket)
    monkeypatch.setattr(
        hindcast_cli,
        "QuietWindow",
        functools.partial(QuietWindow, clock=lambda: OPEN, sleep=_no_sleep),
    )
    monkeypatch.setattr(
        hindcast_cli, "HindcastExtractor", functools.partial(HindcastExtractor, grid=small_grid)
    )
    return fake


def test_cli_plan_fetch_compare_status(offline_cli, data_root: Path, capsys) -> None:
    assert cli.main(["hindcast", "plan"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "2026112012  f30-f31" in out and "2026112018  f26-f27" in out
    plan = json.loads((data_root / "forecast/hindcast/plan/p1-lead24h.json").read_text())
    assert [r["run"] for r in plan["runs"]] == ["2026112012", "2026112018"]
    assert plan["totals"]["runs_not_written"] == ["2026112012", "2026112018"]
    assert plan["totals"]["hindcast_bytes_spent"] == plan["transfer"]["total_bytes"]
    history = list((data_root / "forecast/hindcast/plan/history").iterdir())
    assert [p.name.startswith("p1-lead24h-") for p in history] == [True]

    assert cli.main(["hindcast", "fetch"]) == cli.EXIT_OK
    assert "done: 2 runs written now" in capsys.readouterr().out
    assert cli.main(["hindcast", "fetch"]) == cli.EXIT_OK  # resumed: nothing to do
    assert "done: 0 runs written now, 2 already" in capsys.readouterr().out

    # Planned again once written: nothing is pending, so the cap check counts nothing more.
    assert cli.main(["hindcast", "plan"]) == cli.EXIT_OK
    capsys.readouterr()
    again = json.loads((data_root / "forecast/hindcast/plan/p1-lead24h.json").read_text())
    assert again["totals"]["runs_not_written"] == [] and again["totals"]["within_cap"]
    assert again["transfer"]["total_bytes"] == 0  # listings and inventories from the cache

    assert cli.main(["hindcast", "compare"]) == cli.EXIT_OK
    assert "live_model_at_write: 4 hours" in capsys.readouterr().out
    compared = json.loads(
        (data_root / "forecast/hindcast/reports/availability-compare.json").read_text()
    )
    assert compared["min_samples"] == 20
    assert set(compared["live_lags_pooled_by_fh"]) == {"26", "27", "30", "31"}
    assert all("live_model_pooled_samples" in row for row in compared["rows"])

    assert cli.main(["hindcast", "status"]) == cli.EXIT_OK
    status = json.loads(capsys.readouterr().out)
    assert status["files"] == [
        f"forecast/hrrr/{RAINIER}/2026112012z.json",
        f"forecast/hrrr/{RAINIER}/2026112018z.json",
    ]
    assert list(status["plans"]) == ["plan/p1-lead24h.json"]
    assert status["progress_runs"] == ["2026112012", "2026112018"] and status["errors"] == 0
    assert status["network_total"]["requests"]["grib"] == len(offline_cli.grib_requests())
    for live in ("forecast/hrrr", "state", "cache"):
        assert not (data_root / live).exists(), live


def test_cli_plan_over_the_cap_exits_4(offline_cli, data_root: Path, capsys) -> None:
    assert cli.main(["hindcast", "plan"]) == cli.EXIT_OK
    capsys.readouterr()
    spent = json.loads((data_root / "forecast/hindcast" / LEDGER_KEY).read_text())["total_bytes"]
    # The listings and inventories are spent and kept; the cap leaves 1 kB for the GRIB.
    cap_mb = str((spent + 1000) / 1e6)
    assert cli.main(["hindcast", "plan", "--max-total-mb", cap_mb]) == cli.EXIT_CAP
    assert "within: False" in capsys.readouterr().out
    plan = json.loads((data_root / "forecast/hindcast/plan/p1-lead24h.json").read_text())
    assert plan["totals"]["plan_alone_within_cap"] is False
    assert plan["totals"]["hindcast_bytes_spent"] == spent
    assert cli.main(["hindcast", "fetch", "--max-total-mb", cap_mb]) == cli.EXIT_CAP
    assert "over-cap" in capsys.readouterr().out
    assert not offline_cli.grib_requests()


def test_cli_refuses_every_request_once_the_cap_is_reached(
    offline_cli, data_root: Path, capsys
) -> None:
    hs = HindcastStorage.at(data_root)
    hs.root.write_json(LEDGER_KEY, {"requests": {"grib": 1}, "bytes": {"grib": 2_000_000_000}})
    assert cli.main(["hindcast", "plan"]) == cli.EXIT_CAP
    assert "cap reached" in capsys.readouterr().out
    args = ["hindcast", "leads", "--sample-runs", "2026112012", "--step-min", "60"]
    assert cli.main(args) == cli.EXIT_CAP
    assert "cap reached" in capsys.readouterr().out
    assert offline_cli.requests == []  # not one request sent
    assert not (data_root / "forecast/hindcast/plan/p1-lead24h.json").exists()


def test_cli_plan_at_another_lead_keeps_p1s_plan(offline_cli, data_root: Path, capsys) -> None:
    assert cli.main(["hindcast", "plan"]) == cli.EXIT_OK
    assert cli.main(["hindcast", "plan", "--lead-h", "25"]) == cli.EXIT_OK
    capsys.readouterr()
    folder = data_root / "forecast/hindcast/plan"
    p24 = json.loads((folder / "p1-lead24h.json").read_text())
    p25 = json.loads((folder / "p1-lead25h.json").read_text())
    assert p24["lead_h"] == 24.0 and p25["lead_h"] == 25.0
    assert {r["run"]: r["hours"] for r in p24["runs"]} == {
        "2026112012": [30, 31],
        "2026112018": [26, 27],
    }
    # 25 h before 20:05Z the 18Z run's f26 was not out yet: 12Z serves both frames.
    assert {r["run"]: r["hours"] for r in p25["runs"]} == {"2026112012": [30, 31, 32, 33]}
    names = sorted(p.name for p in (folder / "history").iterdir())
    assert [n.split("-2026")[0] for n in names] == ["p1-lead24h", "p1-lead25h"]
    assert cli.main(["hindcast", "fetch", "--lead-h", "25"]) == cli.EXIT_OK
    assert "done: 1 runs written now" in capsys.readouterr().out
    assert cli.main(["hindcast", "fetch"]) == cli.EXIT_OK  # P1's 24 h plan: 12Z holds f30-f31
    assert "done: 1 runs written now, 1 already" in capsys.readouterr().out
    doc = json.loads(
        (data_root / "forecast/hindcast/hrrr" / RAINIER / "2026112012z.json").read_text()
    )
    assert doc["forecast_hours"] == [30, 31, 32, 33]


def test_cli_waits_out_the_quiet_window_and_logs_it(
    offline_cli, data_root: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [datetime(2026, 11, 23, 12, 52, tzinfo=UTC)]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(
        hindcast_cli,
        "QuietWindow",
        functools.partial(QuietWindow, clock=lambda: clock[0], sleep=sleep),
    )
    assert cli.main(["hindcast", "plan"]) == cli.EXIT_OK
    assert "quiet window: pausing 13.0 min from 2026-11-23T12:52:00Z" in capsys.readouterr().out
    assert slept == [pytest.approx(13 * 60.0)]  # once: the clock then reads 13:05
    events = read_jsonl(data_root / "forecast/hindcast/log/2026-11-23.jsonl")
    pauses = [e for e in events if e["event"] == "hindcast-quiet-window"]
    assert [e["seconds"] for e in pauses] == [780.0]


def test_cli_commands_never_run_together(
    offline_cli, data_root: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hindcast_cli, "COMMAND_LOCK_TIMEOUT_S", 0.2)
    assert cli.main(["hindcast", "plan"]) == cli.EXIT_OK
    capsys.readouterr()
    root = LocalStorage(data_root / "forecast" / "hindcast")
    with root.lock(hindcast_cli.COMMAND_LOCK, timeout_s=1.0):  # another command is running
        for command in (["fetch"], ["plan"], ["leads", "--sample-runs", "2026112012"]):
            assert cli.main(["hindcast", *command]) == cli.EXIT_ERROR, command
            assert "another hindcast command is running" in capsys.readouterr().out
        assert cli.main(["hindcast", "status"]) == cli.EXIT_OK  # offline: takes no lock
    assert not offline_cli.grib_requests()


def test_cli_rejects_a_quiet_window_that_does_not_wrap(offline_cli, capsys) -> None:
    args = ["hindcast", "plan", "--quiet-start-minute", "5", "--quiet-end-minute", "50"]
    assert cli.main(args) == cli.EXIT_ERROR
    assert "wrap round the hour" in capsys.readouterr().err


def test_cli_fetch_without_a_plan(data_root: Path, capsys) -> None:
    assert cli.main(["hindcast", "fetch"]) == cli.EXIT_ERROR
    assert "no plan at plan/p1-lead24h.json" in capsys.readouterr().out


def test_cli_leads_prices_design_p_from_sample_runs(
    data_root: Path, small_grid, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    fake = FakeBucket(grid=small_grid)
    r13 = datetime(2026, 11, 20, 13, tzinfo=UTC)
    fake.add_run(R12, fields=list(LIVE_FIELD_NAMES))  # every hour, f00-f48
    fake.add_run(r13, fields=list(LIVE_FIELD_NAMES))  # f00-f18
    (data_root / "forecast_log").mkdir()
    shutil.copy(
        FIXTURES / "hindcast" / "live_lag_events.jsonl", data_root / "forecast_log/2026-11-20.jsonl"
    )

    def bucket(root, **kw):
        return CachingBucket(root, transport=fake.transport(), sleep=lambda _: None, **kw)

    monkeypatch.setattr(hindcast_cli, "CachingBucket", bucket)
    monkeypatch.setattr(
        hindcast_cli,
        "QuietWindow",
        functools.partial(QuietWindow, clock=lambda: OPEN, sleep=_no_sleep),
    )
    args = ["hindcast", "leads", "--sample-runs", "2026112012", "2026112013", "--step-min", "30"]
    assert cli.main(args) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "48 h: never served" in out and "MB/day at catalogue-22" in out
    report = json.loads((data_root / "forecast/hindcast/reports/design-p-leads.json").read_text())
    assert report["bytes"]["sample_runs"] == ["2026112012", "2026112013"]
    assert len(report["bytes"]["mb_per_hour_synoptic"]) == 49
    assert len(report["bytes"]["mb_per_hour_other"]) == 19
    for estimate in report["estimates"].values():
        assert estimate["per_lead"]["48"]["hours_by_cycle"] == {}
        assert set(estimate["per_lead"]["24"]["hours_by_cycle"]) <= {"00", "06", "12", "18"}
    priced = report["mb_per_day"]["all_valid_times"]
    assert priced["design_p_only"]["runs_per_day"] == 24
    assert priced["with_the_standin_f00_f02"]["mb_per_day"] >= priced["design_p_only"]["mb_per_day"]
    assert not priced["design_p_only"]["unpriced_hours"]
    # Every profile prices the same hours; the fake files hold only the 8 live fields, so the
    # others' extra fields are absent from every inventory (and listed as such).
    profiles = report["field_profiles"]
    assert list(profiles) == ["standin-8", "scene-12", "catalogue-22"]
    assert report["fields_profile"] == "standin-8" and profiles["standin-8"]["absent"] == {}
    assert profiles["standin-8"]["mb_per_day"] == report["mb_per_day"]
    assert (
        len(profiles["catalogue-22"]["fields"]) == 22 and len(profiles["scene-12"]["fields"]) == 12
    )
    assert set(profiles["scene-12"]["absent"]) == {
        "HGT:cloud top",
        "PRATE:surface",
        "UGRD:10 m above ground",
        "VGRD:10 m above ground",
    }
    assert not fake.grib_requests()  # priced from the inventories alone
