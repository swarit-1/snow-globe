"""The rename to Snow Globe (D-080): forecast files written before it stay valid and readable.

``fixtures/forecast_run_lookout_0.1.0_smokies.look-rock_2026111908z.json`` is a real file the
forecast stand-in (``lookout-forecast`` 0.1.0) wrote at 03:54 UTC on 19 Nov 2026, byte for byte:
``kind: lookout.forecast-run`` and ``software.lookout_forecast``. Every one of the 393 files on
disk on 21 Nov had that shape, and each validated with this code (the code builder's check).
The renamed worker writes ``snow-globe.forecast-run`` and ``software.snow_globe_forecast``.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from snow_globe_common.paths import forecast_key

from snow_globe_forecast.availability import select_stored_run, stored_availability
from snow_globe_forecast.output import KIND, LEGACY_KIND, DocumentError, validate_document

from .conftest import FIXTURES
from .test_output import doc  # noqa: F401  (the fixture of a freshly written document)

RECORDED = "forecast_run_lookout_0.1.0_smokies.look-rock_2026111908z.json"
SMOKIES = "smokies.look-rock"
RUN = datetime(2026, 11, 19, 8, tzinfo=UTC)


def recorded() -> dict:
    return json.loads((FIXTURES / RECORDED).read_text())


def test_the_writer_names_the_new_kind_and_worker(doc) -> None:  # noqa: F811
    assert (KIND, LEGACY_KIND) == ("snow-globe.forecast-run", "lookout.forecast-run")
    assert doc["kind"] == KIND
    assert set(doc["software"]) == {"snow_globe_forecast", "eccodes"}


def test_a_file_written_before_the_rename_still_validates() -> None:
    old = recorded()
    assert old["kind"] == LEGACY_KIND and set(old["software"]) == {"lookout_forecast", "eccodes"}
    validate_document(old)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["software"].update(lookout_forecast="0.2.0"),  # names both workers
        lambda d: d["software"].pop("snow_globe_forecast"),  # names none
        lambda d: d.update(kind="lookout.something-else"),
        lambda d: d.update(kind="snow-globe.forecast"),
    ],
)
def test_the_kind_and_worker_must_be_one_of_the_two_names(doc, mutate) -> None:  # noqa: F811
    broken = copy.deepcopy(doc)
    mutate(broken)
    with pytest.raises(DocumentError, match="schema"):
        validate_document(broken)


def test_an_old_file_serves_stored_run_selection(storage) -> None:
    old = recorded()
    storage.write_json(forecast_key("hrrr", SMOKIES, RUN), old)
    fetched = datetime.fromisoformat(old["fetch_finished_utc"].replace("Z", "+00:00"))
    lookup = stored_availability(storage, SMOKIES)
    available, source = lookup(RUN, 1)
    assert source == "live-fetch-finished" and available == fetched
    issue = fetched + timedelta(minutes=5)
    choice = select_stored_run(storage, SMOKIES, issue, RUN + timedelta(hours=1, minutes=30))
    assert choice is not None and choice.run_id == "2026111908"
    assert (choice.fh_lo, choice.fh_hi) == (1, 2)
