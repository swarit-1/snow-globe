"""Dev-fit and dev-check weeks (PRD §13.9): ISO-week parity of the valid time's local day."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from snow_globe_eval.p1dev_weeks import DEV_CHECK, DEV_FIT, dev_role, dev_week, dev_week_of_time

ZONE = "America/Los_Angeles"


def test_odd_iso_weeks_fit_and_even_weeks_check() -> None:
    assert dev_role(date(2026, 11, 19)) == DEV_FIT  # W47, design P's go-ahead week
    assert dev_role(date(2026, 11, 22)) == DEV_FIT  # Sunday ends W47
    assert dev_role(date(2026, 11, 23)) == DEV_CHECK  # Monday starts W48
    assert dev_role(date(2026, 11, 30)) == DEV_FIT  # W49
    assert dev_role(date(2026, 12, 7)) == DEV_CHECK  # W50
    assert dev_week(date(2026, 12, 14)) == {"iso_year": 2026, "iso_week": 51, "role": DEV_FIT}


def test_weeks_alternate_through_design_p_development() -> None:
    days = [date(2026, 11, 16) + timedelta(days=d) for d in range(28)]
    roles = [dev_role(d) for d in days]
    for k in range(4):  # each whole week has one role, and consecutive weeks differ
        week = roles[7 * k : 7 * k + 7]
        assert len(set(week)) == 1
        assert week[0] == (DEV_FIT if k % 2 == 0 else DEV_CHECK)


def test_the_local_day_decides_not_the_utc_day() -> None:
    # 05:00 UTC on Monday 23 Nov is 21:00 PST on Sunday 22 Nov: still W47
    late = datetime(2026, 11, 23, 5, tzinfo=UTC)
    assert dev_week_of_time(late, ZONE) == {
        "local_day": "2026-11-22",
        "iso_year": 2026,
        "iso_week": 47,
        "role": DEV_FIT,
    }
    assert dev_week_of_time(datetime(2026, 11, 23, 9, tzinfo=UTC), ZONE)["role"] == DEV_CHECK


def test_datetimes_and_naive_times_are_refused() -> None:
    with pytest.raises(TypeError):
        dev_role(datetime(2026, 11, 19, 12, tzinfo=UTC))
    with pytest.raises(ValueError):
        dev_week_of_time(datetime(2026, 11, 19, 12), ZONE)
