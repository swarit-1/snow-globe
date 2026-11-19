"""The lookout-forecast command line against the fake bucket (PRD §8.3, §11.2)."""

from __future__ import annotations

import functools
import json
from datetime import UTC, datetime, timedelta

import pytest
from lookout_common.storage import LockTimeoutError

from lookout_forecast import cli, discovery
from lookout_forecast.bucket import HrrrBucket
from lookout_forecast.extract import Extractor
from lookout_forecast.output import validate_document

from .conftest import RAINIER

R05 = datetime(2026, 11, 19, 5, tzinfo=UTC)
R07 = datetime(2026, 11, 19, 7, tzinfo=UTC)
NOW = datetime(2026, 11, 19, 9, 40, tzinfo=UTC)
FIELDS = ["VIS:surface", "LCDC:low cloud layer", "HGT:surface"]
ARGS = ["--only", RAINIER, "--fields", "VIS", "LCDC", "TERRAIN"]


@pytest.fixture
def wired(fake, small_grid, data_root, monkeypatch):
    fake.add_run(R05, fields=FIELDS)
    fake.add_run(R07, fields=FIELDS)
    fake.add_run(datetime(2026, 11, 19, 8, tzinfo=UTC), fields=FIELDS, hours=range(5))
    monkeypatch.setattr(cli, "HrrrBucket", lambda: HrrrBucket(transport=fake.transport()))
    monkeypatch.setattr(
        cli, "Extractor", functools.partial(Extractor, grid=small_grid, clock=lambda: NOW)
    )
    monkeypatch.setattr(discovery, "utcnow", lambda: NOW)
    monkeypatch.setattr(cli, "utcnow", lambda: NOW)
    return fake


def test_run_writes_a_valid_file(wired, data_root, capsys) -> None:
    assert cli.main(["run", "2026111907", "--max-fh", "1", *ARGS]) == 0
    out = capsys.readouterr().out
    assert "run 2026111907: live, complete on S3; hours f00-f01" in out
    doc = json.loads((data_root / "forecast/hrrr" / RAINIER / "2026111907z.json").read_text())
    validate_document(doc)
    assert doc["field_names"] == FIELDS


def test_latest_picks_the_newest_complete_run_and_is_idempotent(wired, data_root, capsys) -> None:
    assert cli.main(["latest", "--max-fh", "0", "--json", *ARGS]) == 0
    event = json.loads(capsys.readouterr().out)
    assert event["run"] == "2026111907" and event["files"] == [
        f"forecast/hrrr/{RAINIER}/2026111907z.json"
    ]
    assert event["transfer"]["requests"]["grib"] >= 1
    assert cli.main(["latest", "--max-fh", "0", *ARGS]) == 0
    assert "already written" in capsys.readouterr().out


def test_exit_codes(wired, capsys) -> None:
    assert cli.main(["run", "2026111908", *ARGS]) == cli.EXIT_NOT_READY
    assert cli.main(["run", "2026111904", *ARGS]) == cli.EXIT_NOT_READY
    assert cli.main(["run", "2026111907", "--max-mb", "0.001", *ARGS]) == cli.EXIT_CAP
    assert cli.main(["run", "2026111907", "--fields", "HGT"]) == cli.EXIT_ERROR
    assert "ambiguous" in capsys.readouterr().err
    assert cli.main(["run", "2026111908", "--max-fh", "4", *ARGS]) == 0  # its f00-f04 exist


def test_backfill_skips_missing_runs(wired, data_root, capsys) -> None:
    rc = cli.main(
        [
            "backfill",
            "--start",
            "2026111905",
            "--end",
            "2026111907",
            "--max-fh",
            "0",
            "--json",
            *ARGS,
        ]
    )
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    statuses = {r["run"]: r["status"] for r in summary["runs"]}
    assert statuses["2026111905"] == "written" and statuses["2026111907"] == "written"
    assert statuses["2026111906"].startswith("skipped")
    rc = cli.main(
        [
            "backfill",
            "--start",
            "2026111905",
            "--end",
            "2026111907",
            "--max-fh",
            "0",
            "--synoptic-only",
            "--json",
            *ARGS,
        ]
    )
    assert [r["run"] for r in json.loads(capsys.readouterr().out)["runs"]] == ["2026111906"]


def test_status_offline_and_online(wired, data_root, capsys) -> None:
    cli.main(["run", "2026111907", "--max-fh", "0", *ARGS])
    capsys.readouterr()
    assert cli.main(["status", "--offline", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["forecast_files"][RAINIER] == {"files": 1, "latest": "2026111907z.json"}
    assert status["lag_events"] == 19  # every hour of the live 07Z run
    assert "bucket_runs" not in status
    assert cli.main(["status", "--hours", "3", "--json"]) == 0
    online = json.loads(capsys.readouterr().out)
    assert [r["run"] for r in online["bucket_runs"]] == ["2026111909", "2026111908", "2026111907"]
    assert online["bucket_runs"][2]["complete"] and online["bucket_runs"][2]["written_viewpoints"]


def test_plan_reports_bytes_from_inventories_only(wired, capsys) -> None:
    assert (
        cli.main(["plan", "2026111905", "--max-fh", "2", "--fields", "VIS", "LCDC", "--json"]) == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["hours"] == [0, 1, 2]
    by_field = plan["bytes_by_field_by_fh"]
    assert set(by_field) == {"VIS:surface", "LCDC:low cloud layer", "HGT:surface (terrain only)"}
    assert list(by_field["HGT:surface (terrain only)"]) == ["0"]  # fetched once, like a run
    assert plan["total_bytes"] == sum(sum(v.values()) for v in by_field.values())
    assert plan["transfer"]["requests"] == {"listing": 1, "idx": 3}  # this plan only
    assert 0 < plan["fraction_of_files"] < 1
    wired.requests.clear()
    assert cli.main(["plan", "2026111905", "--max-fh", "0"]) == 0
    assert "total" in capsys.readouterr().out


def test_cli_rejects_dates_backwards(wired, capsys) -> None:
    assert cli.main(["backfill", "--start", "2026111907", "--end", "2026111905"]) == cli.EXIT_ERROR


def test_now_is_recent() -> None:
    assert timedelta(hours=24) > NOW - R07  # the fixture's runs count as live


def test_environment_defaults_apply_without_flags(wired, monkeypatch, capsys) -> None:
    monkeypatch.setenv("LOOKOUT_FORECAST_FIELDS", "VIS,TERRAIN")
    monkeypatch.setenv("LOOKOUT_FORECAST_MAX_FH", "0")
    monkeypatch.setenv("LOOKOUT_FORECAST_MAX_MB", "0.0001")
    assert cli.main(["run", "2026111907", "--only", RAINIER]) == cli.EXIT_CAP
    assert cli.main(["run", "2026111907", "--only", RAINIER, "--max-mb", "100", "--json"]) == 0
    event = json.loads(capsys.readouterr().out)
    assert event["fields"] == ["VIS:surface", "HGT:surface"] and event["hours"] == [0]


def test_laptop_runs_are_capped_by_default(wired, monkeypatch, capsys) -> None:
    """Review finding 6: with a clean environment nothing limited the download."""
    args = cli.build_parser().parse_args(["latest"])
    assert cli._options(args).max_bytes == 150_000_000
    monkeypatch.setenv("LOOKOUT_FORECAST_MAX_MB", "40")
    assert cli._options(args).max_bytes == 40_000_000
    monkeypatch.setenv("LOOKOUT_FORECAST_MAX_MB", "0")
    assert cli.main(["run", "2026111907", *ARGS]) == cli.EXIT_ERROR
    assert "must be > 0" in capsys.readouterr().err
    monkeypatch.delenv("LOOKOUT_FORECAST_MAX_MB")
    monkeypatch.setattr(cli, "DEFAULT_CLI_MAX_MB", 0.001)  # 1 kB: below any fake run
    assert cli.main(["run", "2026111907", *ARGS]) == cli.EXIT_CAP
    err = capsys.readouterr().err
    assert "nothing downloaded" in err and "--max-mb" in err
    assert wired.grib_requests() == []
    assert cli.main(["run", "2026111907", "--max-mb", "50", *ARGS]) == 0
    with pytest.raises(SystemExit):
        cli.main(["run", "2026111907", "--max-mb", "0"])


def test_backfill_never_overshoots_its_total(wired, capsys) -> None:
    """Review finding 6 (T10): a 10 kB total used to let the first run download 250 kB."""
    rc = cli.main(
        ["backfill", "--start", "2026111905", "--end", "2026111907", "--max-fh", "0",
         "--max-total-mb", "0.01", "--json", *ARGS]
    )  # fmt: skip
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["runs"][0]["run"] == "2026111905"
    assert summary["runs"][0]["status"].startswith("stopped: total cap reached")
    assert len(summary["runs"]) == 1
    assert summary["transfer"]["bytes"].get("grib", 0) == 0
    assert wired.grib_requests() == []


def test_backfill_budget_is_passed_to_each_run(wired, capsys) -> None:
    rc = cli.main(
        ["backfill", "--start", "2026111905", "--end", "2026111907", "--max-fh", "0",
         "--json", *ARGS]
    )  # fmt: skip
    assert rc == 0
    first = json.loads(capsys.readouterr().out)
    per_run = first["runs"][0]["grib_bytes"]
    assert first["total_cap_bytes"] == 1_000_000_000 and per_run > 0
    # A total that holds one run but not two: the second is refused before downloading.
    rc = cli.main(
        ["backfill", "--start", "2026111905", "--end", "2026111907", "--max-fh", "1",
         "--max-total-mb", str(1.5 * per_run * 2 / 1e6), "--json", *ARGS]
    )  # fmt: skip
    summary = json.loads(capsys.readouterr().out)
    statuses = [r["status"] for r in summary["runs"]]
    assert statuses[0] == "written" and statuses[-1].startswith("stopped: total cap reached")
    assert summary["transfer"]["bytes"]["grib"] <= summary["total_cap_bytes"]


def test_backfill_skips_runs_over_the_per_run_cap_and_survives_errors(
    wired, monkeypatch, capsys
) -> None:
    rc = cli.main(
        ["backfill", "--start", "2026111905", "--end", "2026111907", "--max-fh", "0",
         "--max-mb", "0.001", "--json", *ARGS]
    )  # fmt: skip
    statuses = {r["run"]: r["status"] for r in json.loads(capsys.readouterr().out)["runs"]}
    assert rc == 0 and statuses["2026111905"].startswith("skipped: run 2026111905: planned")
    assert statuses["2026111907"].startswith("skipped: run 2026111907: planned")  # not stopped
    real = Extractor.extract

    def flaky(self, run_start, options=None):
        if run_start == R05:
            raise OSError(28, "No space left on device")
        return real(self, run_start, options)

    monkeypatch.setattr(Extractor, "extract", flaky)
    rc = cli.main(
        ["backfill", "--start", "2026111905", "--end", "2026111907", "--max-fh", "0", "--json",
         *ARGS]
    )  # fmt: skip
    statuses = {r["run"]: r["status"] for r in json.loads(capsys.readouterr().out)["runs"]}
    assert rc == 0 and statuses["2026111905"].startswith("error: OSError")
    assert statuses["2026111907"] == "written"


def test_a_busy_lock_is_reported_not_raised(wired, monkeypatch, capsys) -> None:
    def locked(self, run_start, options=None):
        raise LockTimeoutError("lock 'forecast-hrrr' busy for 30.0 s")

    monkeypatch.setattr(Extractor, "extract", locked)
    assert cli.main(["run", "2026111907", *ARGS]) == cli.EXIT_NOT_READY
    assert "busy" in capsys.readouterr().err


def test_lag_survey_lists_only_and_writes_nothing(wired, data_root, capsys) -> None:
    assert cli.main(["lag-survey", "--start", "2026111905", "--end", "2026111908"]) == 0
    survey = json.loads(capsys.readouterr().out)
    assert survey["runs"] == 3 and survey["lags"] == 19 + 19 + 5  # 06Z is not in the bucket
    assert survey["incomplete_runs"] == {
        "2026111906": list(range(49)),
        "2026111908": list(range(5, 19)),
    }
    assert survey["transfer"]["requests"] == {"listing": 4}
    assert survey["per_fh"]["0"]["n"] == 3 and survey["later_than_fallback"] == 0
    assert not any(p.is_file() for p in data_root.rglob("*"))
