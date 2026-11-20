"""A changed field set in the scheduled worker, and everything that reads what it rewrote.

On 20 Nov at 02:39 UTC the lead added ``PRES:cloud base`` to the local stand-in's profile, so
``run_covered`` found every run of the 12 h look-back unwritten: each was extracted again
(about 24 MB), its files rewritten and its ``run`` and ``lag`` events logged a second time.
These tests replay that with the fake bucket and check the readers: the lag log and model,
``status``, the survey logger, live availability (design P) and archived availability
(eval's pair re-uses a stored file only if its recorded availability equals the current
model's). A change to a set that is not a superset must cost nothing (PRD §11.2, §13.9).
"""

from __future__ import annotations

import functools
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from lookout_common.jsonl import read_jsonl

from lookout_forecast import cli, lambda_handler
from lookout_forecast.availability import (
    LagModel,
    select_run_for_issue,
    select_stored_run,
    stored_availability,
)
from lookout_forecast.discovery import list_run
from lookout_forecast.extract import ExtractOptions, Extractor
from lookout_forecast.fields import resolve_fields
from lookout_forecast.laglog import SurveyLogger, lag_events, read_lag_log
from lookout_forecast.output import validate_document

from .conftest import RAINIER

NOW = datetime(2026, 11, 19, 9, 40, tzinfo=UTC)
RUNS = [datetime(2026, 11, 19, hour, tzinfo=UTC) for hour in (5, 6, 7)]
FIELDS = ["VIS:surface", "HGT:cloud base", "PRES:cloud base", "MCDC:middle cloud layer"]
FIELDS += ["HGT:surface"]
#: The stand-in's profiles before and after 02:39 UTC on 20 Nov (here 3 and 4 fields), and a
#: swap that is not a superset of either.
SEVEN = ExtractOptions(fields=tuple(resolve_fields(["VIS", "CLOUDBASE", "TERRAIN"])), max_fh=2)
EIGHT = replace(SEVEN, fields=(*SEVEN.fields, *resolve_fields(["PRES:cloud base"])))
SWAPPED = replace(SEVEN, fields=tuple(resolve_fields(["VIS", "MCDC", "TERRAIN"])))


@pytest.fixture
def tick(fake, bucket, storage, manifests, small_grid):
    for run in RUNS:
        fake.add_run(run, fields=FIELDS, hours=range(49 if run.hour == 6 else 19))
    return functools.partial(
        lambda_handler.process_pending,
        storage,
        bucket,
        {RAINIER: manifests[RAINIER]},
        grid=small_grid,
        lookback_h=4,
        max_runs=10,
    )


def _status(summary: dict) -> dict[str, str]:
    return {row["run"]: row["status"] for row in summary["runs"]}


def _log(storage) -> list[dict]:
    return read_jsonl(storage.path(f"forecast_log/{NOW:%Y-%m-%d}.jsonl"))


def test_a_wider_field_set_rewrites_the_runs_and_every_reader_copes(
    tick, fake, bucket, storage, data_root, capsys
) -> None:
    first = _status(tick(now=NOW, options=SEVEN))
    assert [first[f"{run:%Y%m%d%H}"] for run in RUNS] == ["extracted"] * 3
    key = f"forecast/hrrr/{RAINIER}/2026111906z.json"
    before = storage.read_json(key)
    pairs = len([r for r in _log(storage) if r["event"] == "lag"])  # 19 + 49 + 19 listed hours
    assert pairs == 87

    later = NOW + timedelta(minutes=5)
    fake.requests.clear()
    second = _status(tick(now=later, options=EIGHT))
    assert [second[f"{run:%Y%m%d%H}"] for run in RUNS] == ["extracted"] * 3
    # One listing per extracted run: the handler's own listing is handed to the extractor.
    assert sum(r.url.path in {"", "/"} for r in fake.requests) == 3 + 2  # + 2 runs not on S3
    after = storage.read_json(key)
    validate_document(after)
    assert set(before["field_names"]) < set(after["field_names"])
    assert after["fetch_finished_utc"] > before["fetch_finished_utc"]

    log = _log(storage)
    assert sorted(r["run"] for r in log if r["event"] == "run") == sorted(
        [f"{run:%Y%m%d%H}" for run in RUNS] * 2
    )
    assert len([r for r in log if r["event"] == "lag"]) == 2 * pairs

    # The lag log and the model keep one lag per (run, hour).
    lag_log = read_lag_log(storage)
    assert lag_log.by_method == {"live": pairs} and lag_log.duplicates == {"live": pairs}
    model = LagModel.from_storage(storage)
    assert sum(model.pooled_samples.values()) == pairs
    assert model.samples[(6, 0)] == 1

    # status counts files once per run and reports the dropped duplicates.
    assert cli.main(["status", "--offline", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["forecast_files"][RAINIER] == {"files": 3, "latest": "2026111907z.json"}
    assert status["lag_events"] == pairs
    assert status["lag_log"]["duplicates_dropped"] == {"live": pairs, "survey": 0}

    # A survey of the same runs logs nothing new, and its stamps equal the logged ones.
    logger = SurveyLogger(storage, clock=lambda: later)
    for run in RUNS:
        assert logger.add(list_run(bucket, run, now=later)) == 0
    logger.flush()
    report = logger.report()
    assert report["logged"] == 0
    assert report["agreement_with_logged"]["live"]["equal"] == pairs

    # Design P: a rewritten live file is available at its new fetch, never earlier.
    lookup = stored_availability(storage, RAINIER)
    when, source = lookup(RUNS[1], 2)  # type: ignore[misc]
    assert source == "live-fetch-finished" and when == later


def test_a_field_set_that_is_not_a_superset_costs_nothing(tick, fake, storage) -> None:
    tick(now=NOW, options=EIGHT)
    events = len(_log(storage))
    for minutes in (5, 10):
        fake.requests.clear()
        status = _status(tick(now=NOW + timedelta(minutes=minutes), options=SWAPPED))
        for run in RUNS:
            assert status[f"{run:%Y%m%d%H}"].startswith("kept: nothing to write")
        assert "files are never narrowed" in status["2026111906"]
        listed = {r.url.params.get("prefix", "") for r in fake.requests}
        assert not any(f"t{run:%H}z" in prefix for run in RUNS for prefix in listed)
        assert not [r for r in fake.requests if r.url.path.endswith((".idx", ".grib2"))]
    assert len(_log(storage)) == events  # no run, lag or error events
    assert not [k for k in storage.list_prefix("state/") if k.endswith(".failure.json")]
    # The stored files keep their fields; a superset of them is written as usual.
    doc = storage.read_json(f"forecast/hrrr/{RAINIER}/2026111906z.json")
    assert "PRES:cloud base" in doc["field_names"]
    wider = replace(EIGHT, fields=(*EIGHT.fields, *resolve_fields(["MCDC"])))
    assert _status(tick(now=NOW + timedelta(minutes=15), options=wider))["2026111906"] == (
        "extracted"
    )


def test_a_rewritten_archived_file_records_the_current_model(
    fake, bucket, storage, manifests, small_grid
) -> None:
    """eval's pair re-uses a stored archived file only while its recorded availability equals
    the current model's. Lags logged later (here a survey) change the model; a forced
    re-extraction then records the new value, so the reuse check passes again."""
    run = datetime(2026, 11, 16, 7, tzinfo=UTC)
    fake.add_run(run, fields=FIELDS, hours=range(19))
    clock = datetime(2026, 11, 19, 9, 40, tzinfo=UTC)
    options = ExtractOptions(fields=tuple(resolve_fields(["VIS", "TERRAIN"])), max_fh=6)

    def extractor() -> Extractor:  # a new one each time: the lag model is read on first use
        return Extractor(
            storage, bucket, {RAINIER: manifests[RAINIER]}, grid=small_grid, clock=lambda: clock
        )

    extractor().extract(run, options)
    stored = storage.read_json(f"forecast/hrrr/{RAINIER}/2026111607z.json")["hours"][5]
    assert stored["availability_model"]["method"] == "conservative-fallback"
    assert stored["availability_utc"] == "2026-11-16T09:00:00Z"

    # 25 earlier 07Z runs surveyed at f05: the cell now has a p95 of its own.
    listing = list_run(bucket, run, now=clock)
    surveyed = []
    for k in range(25):
        event = dict(lag_events(listing, hours=[5], method="survey")[0])
        surveyed.append(dict(event, run=f"202610{k + 1:02d}07", lag_s=73.5 * 60))
    storage.append_jsonl_many("forecast_log/lag_survey/2026-11-19.jsonl", surveyed)
    model = LagModel.from_storage(storage)
    issue, valid = run + timedelta(minutes=73.5), run + timedelta(hours=5)
    choice = select_run_for_issue(issue, valid, lag_model=model)
    assert choice is not None and choice.run_id == "2026111607"
    assert choice.availability_model is not None
    assert choice.availability_model["samples_by_method"] == {"live": 0, "survey": 25}
    assert choice.availability_utc == run + timedelta(minutes=73.5)
    assert stored["availability_utc"] != "2026-11-16T08:13:30Z"  # the pair would refuse it

    extractor().extract(run, replace(options, force=True))
    rewritten = storage.read_json(f"forecast/hrrr/{RAINIER}/2026111607z.json")
    validate_document(rewritten)
    assert rewritten["hours"][5]["availability_utc"] == "2026-11-16T08:13:30Z"
    assert rewritten["hours"][5]["availability_model"]["samples_by_method"]["survey"] == 25
    chosen = select_stored_run(storage, RAINIER, issue, valid, lag_model=model)
    assert chosen is not None and chosen.availability_utc == choice.availability_utc
