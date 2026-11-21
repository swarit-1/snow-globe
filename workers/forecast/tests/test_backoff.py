"""Run-level back-off records (CLAUDE.md "Back off on errors"; PRD §11.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from snow_globe_forecast.backoff import (
    failure_key,
    next_delay_s,
    read_failure,
    record_failure,
    resolve_failure,
    settings_fingerprint,
)

RUN = datetime(2026, 11, 19, 7, tzinfo=UTC)
NOW = datetime(2026, 11, 19, 9, 40, tzinfo=UTC)
SETTINGS = settings_fingerprint(
    field_names=["VIS:surface", "HGT:surface"], max_fh=None, max_bytes=None, viewpoint_ids=["a.b"]
)


def test_delays_double_from_ten_minutes_to_at_most_four_hours() -> None:
    assert [next_delay_s(n) / 60 for n in range(1, 8)] == [10, 20, 40, 80, 160, 240, 240]
    with pytest.raises(ValueError):
        next_delay_s(0)


def test_fingerprint_ignores_order() -> None:
    again = settings_fingerprint(
        field_names=["HGT:surface", "VIS:surface"],
        max_fh=None,
        max_bytes=None,
        viewpoint_ids=["a.b"],
    )
    assert again == SETTINGS
    assert (
        settings_fingerprint(
            field_names=["VIS:surface"], max_fh=None, max_bytes=None, viewpoint_ids=["a.b"]
        )
        != SETTINGS
    )


def test_errors_count_up_and_settings_reset_them(storage) -> None:
    first = record_failure(
        storage, RUN, kind="error", error="OSError: x", settings=SETTINGS, now=NOW
    )
    assert (first.attempts, first.next_try_utc) == (1, NOW + timedelta(minutes=10))
    assert first.skip_reason(NOW + timedelta(minutes=5), SETTINGS).startswith("1 failure(s)")
    assert first.skip_reason(NOW + timedelta(minutes=10), SETTINGS) is None
    assert first.skip_reason(NOW, "other settings") is None
    later = NOW + timedelta(minutes=10)
    second = record_failure(
        storage, RUN, kind="error", error="OSError: x", settings=SETTINGS, now=later
    )
    assert second.attempts == 2 and second.first_failed_utc == NOW
    assert second.next_try_utc == later + timedelta(minutes=20)
    assert read_failure(storage, RUN) == second
    fresh = record_failure(storage, RUN, kind="error", error="e", settings="new", now=later)
    assert fresh.attempts == 1


def test_cap_refusals_wait_for_new_settings(storage) -> None:
    cap = record_failure(
        storage, RUN, kind="cap", error="BandwidthCapError: big", settings=SETTINGS, now=NOW
    )
    assert cap.next_try_utc is None
    assert "refused by the cap" in (cap.skip_reason(NOW + timedelta(days=1), SETTINGS) or "")
    assert cap.skip_reason(NOW, "a higher cap") is None


def test_resolve_keeps_history_and_bad_records_never_block(storage) -> None:
    assert resolve_failure(storage, RUN, now=NOW) is False
    record_failure(storage, RUN, kind="error", error="e", settings=SETTINGS, now=NOW)
    assert resolve_failure(storage, RUN, now=NOW) is True
    assert read_failure(storage, RUN) is None
    assert storage.read_json(failure_key(RUN))["last_failure"]["attempts"] == 1
    storage.write_json(failure_key(RUN), {"kind": "error", "attempts": "many"})
    assert read_failure(storage, RUN) is None
    assert failure_key(RUN) == "state/forecast/hrrr/2026111907z.failure.json"
