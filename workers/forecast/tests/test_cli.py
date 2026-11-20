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
from lookout_forecast.hrrr import run_prefix
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
    # The search listed 09Z, 08Z and 07Z once each; the extraction re-used the 07Z listing.
    assert "listing" not in event["transfer"]["requests"]
    assert sum(r.url.path in {"", "/"} for r in wired.requests) == 3
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


def test_lag_survey_log_writes_survey_events_once(wired, data_root, monkeypatch, capsys) -> None:
    """D-027: --log logs each listed (run, hour) once, in the survey log only."""
    from lookout_forecast import laglog

    monkeypatch.setattr(laglog, "utcnow", lambda: NOW)
    args = ["lag-survey", "--start", "2026111905", "--end", "2026111908", "--log"]
    assert cli.main(args) == 0
    survey = json.loads(capsys.readouterr().out)
    assert survey["log"]["logged"] == 19 + 19 + 5 and survey["end_rule"] == "given"
    assert survey["log"]["keys"] == ["forecast_log/lag_survey/2026-11-19.jsonl"]
    assert survey["transfer"]["requests"] == {"listing": 4}  # listings only, no GRIB
    cells = survey["lag_model_cells"]
    assert cells["before"]["left_fallback"] == 0 and cells["after"]["cells"] == 576
    files = sorted(p.relative_to(data_root).as_posix() for p in data_root.rglob("*") if p.is_file())
    assert files == [
        "forecast_log/lag_survey/2026-11-19.jsonl",
        "state/locks/forecast-lag-survey.lock",
    ]  # never the live log, forecast_log/<day>.jsonl
    assert cli.main(args) == 0  # again: nothing new
    again = json.loads(capsys.readouterr().out)["log"]
    assert again["logged"] == 0 and again["already_logged"] == {"live": 0, "survey": 43}
    assert again["agreement_with_logged"]["survey"]["equal"] == 43
    assert cli.main(["status", "--offline", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["lag_events"] == 43
    assert status["lag_log"]["by_method"] == {"live": 0, "survey": 43}
    assert status["lag_model_cells_live_only"]["left_fallback"] == 0


def test_lag_survey_ends_at_the_newest_complete_run_by_default(wired, capsys) -> None:
    assert cli.main(["lag-survey", "--start", "2026111905"]) == 0
    survey = json.loads(capsys.readouterr().out)
    assert survey["last_run"] == "2026111907" and survey["end_rule"] == "the newest complete run"
    assert survey["lags"] == 19 + 19 and "log" not in survey
    # Review finding: the run the search found was listed twice. The search lists 09Z, 08Z
    # and 07Z; the survey lists 05Z and 06Z and re-uses the search's 07Z listing.
    prefixes = [r.url.params.get("prefix") for r in wired.requests if r.url.path in {"", "/"}]
    assert prefixes.count(run_prefix(R07)) == 1 and len(prefixes) == 5
    assert survey["listings_reused"] == ["2026111907"]
    assert survey["transfer"]["requests"] == {"listing": 2}
    assert survey["transfer_with_end_search"]["requests"] == {"listing": 5}


def test_status_counts_lags_by_method(wired, data_root, monkeypatch, capsys) -> None:
    cli.main(["run", "2026111907", "--max-fh", "0", *ARGS])  # 19 live lags
    from lookout_forecast import laglog

    monkeypatch.setattr(laglog, "utcnow", lambda: NOW)
    cli.main(["lag-survey", "--start", "2026111905", "--end", "2026111907", "--log"])
    capsys.readouterr()
    assert cli.main(["status", "--offline", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["lag_log"]["by_method"] == {"live": 19, "survey": 19}
    assert status["availability_model"]["f00"]["pooled_samples_by_method"] == {
        "live": 1,
        "survey": 1,
    }
    assert cli.main(["status", "--offline"]) == 0
    text = capsys.readouterr().out
    assert "lag events: 38 (live 19, survey 19" in text and "have left the 120 min" in text


def test_lag_check_lists_runs_and_writes_nothing(wired, data_root, capsys) -> None:
    assert cli.main(["lag-check", "2026111905", "2026111907"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [run["run"] for run in out["runs"]] == ["2026111905", "2026111907"]
    hour = out["runs"][0]["hours"][0]
    # The fake publishes f00 at +3000 s and its .idx 4 s later.
    assert hour["lag_min"] == round(3004 / 60, 2) and hour["model_lag_min"] == 120.0
    assert hour["model_method"] == "conservative-fallback"
    assert hour["live_only_model_method"] == "conservative-fallback"
    assert out["runs"][0]["hours_later_than_model"] == 0
    assert out["transfer"]["requests"] == {"listing": 2}
    assert not any(p.is_file() for p in data_root.rglob("*"))


def test_run_hours_extracts_exactly_those_hours(wired, data_root, monkeypatch, capsys) -> None:
    monkeypatch.setenv("LOOKOUT_FORECAST_MAX_FH", "0")  # --hours wins over the environment
    assert cli.main(["run", "2026111907", "--hours", "3", "1", "--json", *ARGS]) == 0
    event = json.loads(capsys.readouterr().out)
    assert event["hours"] == [1, 3]
    doc = json.loads((data_root / "forecast/hrrr" / RAINIER / "2026111907z.json").read_text())
    assert doc["forecast_hours"] == [1, 3]
    grib = {r.url.path.rsplit(".", 1)[0][-2:] for r in wired.grib_requests()}
    assert grib == {"01", "03"}
    wired.requests.clear()
    assert cli.main(["run", "2026111907", "--hours", "1", "3", *ARGS]) == 0
    assert "nothing to write, nothing downloaded" in capsys.readouterr().out
    assert wired.grib_requests() == []
    assert cli.main(["run", "2026111907", "--hours", "1", "--max-fh", "2", *ARGS]) == 1
    assert "not both" in capsys.readouterr().err
    assert cli.main(["run", "2026111907", "--hours", "24", *ARGS]) == 1
    assert "stops at f18" in capsys.readouterr().err


def test_plan_hours(wired, capsys) -> None:
    assert cli.main(["plan", "2026111907", "--hours", "5", "2", "--fields", "VIS", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["hours"] == [2, 5] and plan["transfer"]["requests"] == {"listing": 1, "idx": 2}


def test_tick_is_one_handler_call(monkeypatch, capsys) -> None:
    """``tick`` runs the Lambda's code path: handler({}, None), nothing changed on the way."""
    from lookout_forecast import lambda_handler

    calls: list[tuple] = []
    summary = {"now_utc": "2026-11-19T09:40:00Z", "extracted": 0, "runs": [], "transfer": {}}

    def spy(event, context):
        calls.append((event, context))
        return summary

    monkeypatch.setattr(lambda_handler, "handler", spy)
    assert cli.main(["tick"]) == cli.EXIT_ERROR  # no LOOKOUT_FORECAST_MAX_MB: refused
    assert "LOOKOUT_FORECAST_MAX_MB is not set" in capsys.readouterr().err and calls == []
    monkeypatch.setenv("LOOKOUT_FORECAST_MAX_MB", "40")
    assert cli.main(["tick", "--json"]) == 0
    assert calls == [({}, None)] and json.loads(capsys.readouterr().out) == summary
    monkeypatch.delenv("LOOKOUT_FORECAST_MAX_MB")
    assert cli.main(["tick", "--uncapped"]) == 0 and len(calls) == 2
    assert "extracted 0 run(s)" in capsys.readouterr().out


def test_tick_exits_non_zero_when_a_run_fails(monkeypatch, capsys) -> None:
    """Review finding: a loop calling ``tick --json`` could not see failures in the status."""
    from lookout_forecast import lambda_handler

    cap = "error: BandwidthCapError: run 2026111907: planned 0.0 MB exceeds the cap"
    rows = {
        "ok": [{"run": "2026111907", "status": "written"}],
        "earlier failure": [{"run": "2026111907", "status": "backing off: cap refusal"}],
        "cap": [{"run": "2026111907", "status": cap, "bytes": 0}],
        "other": [
            {"run": "2026111907", "status": cap, "bytes": 0},
            {"run": "2026111906", "status": "error: DecodeError: bad message", "bytes": 9},
        ],
    }
    want = {"ok": 0, "earlier failure": 0, "cap": cli.EXIT_CAP, "other": cli.EXIT_ERROR}
    monkeypatch.setenv("LOOKOUT_FORECAST_MAX_MB", "40")
    for name, runs in rows.items():
        summary = {"now_utc": "2026-11-19T09:40:00Z", "extracted": 0, "runs": runs, "transfer": {}}
        monkeypatch.setattr(lambda_handler, "handler", lambda event, context, s=summary: s)
        assert cli.main(["tick", "--json"]) == want[name], name
        assert json.loads(capsys.readouterr().out) == summary  # printed unchanged


def test_tick_end_to_end_with_the_environment_profile(
    fake, small_grid, data_root, manifests, monkeypatch, capsys
) -> None:
    from lookout_forecast import lambda_handler

    fake.add_run(R07, fields=FIELDS, hours=range(19))
    for name, value in {
        "LOOKOUT_FORECAST_FIELDS": "VIS,TERRAIN",
        "LOOKOUT_FORECAST_MAX_FH": "1",
        "LOOKOUT_FORECAST_MAX_MB": "50",
        "LOOKOUT_FORECAST_LOOKBACK_H": "3",
        "LOOKOUT_FORECAST_MAX_RUNS": "1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        lambda_handler, "HrrrBucket", lambda: HrrrBucket(transport=fake.transport())
    )
    monkeypatch.setattr(lambda_handler, "load_manifests", lambda: {RAINIER: manifests[RAINIER]})
    monkeypatch.setattr(
        lambda_handler,
        "process_pending",
        functools.partial(lambda_handler.process_pending, grid=small_grid, now=NOW),
    )
    assert cli.main(["tick"]) == 0
    out = capsys.readouterr().out
    assert "extracted 1 run(s)" in out and "run 2026111907: extracted, 2 hour(s), 1 file(s)" in out
    doc = json.loads((data_root / "forecast/hrrr" / RAINIER / "2026111907z.json").read_text())
    assert doc["forecast_hours"] == [0, 1] and doc["field_names"] == ["VIS:surface", "HGT:surface"]
    assert cli.main(["tick", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert {r["run"]: r["status"] for r in summary["runs"]}["2026111907"] == "written"


def test_save_listings_records_the_answers_as_received(wired, tmp_path, capsys) -> None:
    folder = tmp_path / "listings"
    assert cli.main(["lag-check", "2026111905", "--save-listings", str(folder)]) == 0
    capsys.readouterr()
    saved = folder / "hrrr.20261119_conus_hrrr.t05z.wrfsfcf.xml"
    assert [p.name for p in folder.iterdir()] == [saved.name]
    assert saved.read_bytes() == wired.listing_xml("hrrr.20261119/conus/hrrr.t05z.wrfsfcf")
