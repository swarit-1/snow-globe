from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from lookout_common import timeutil as tu
from lookout_common.timeutil import (
    FLAG_AMBIGUOUS,
    FLAG_NONEXISTENT,
    FLAG_OFFSET_FROM_EXIF,
    AmbiguousTimeError,
    NonexistentTimeError,
)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- basics -------------------------------------------------------------------------
def test_utcnow_is_aware_utc():
    now = tu.utcnow()
    assert now.tzinfo is UTC


def test_to_utc_refuses_naive():
    with pytest.raises(ValueError):
        tu.to_utc(datetime(2026, 11, 19))


def test_isoformat_z_and_parse_roundtrip():
    moment = datetime(2026, 11, 18, 22, 40, 14, 123456, tzinfo=timezone(timedelta(hours=-8)))
    assert tu.isoformat_z(moment, "milliseconds") == "2026-11-19T06:40:14.123Z"
    assert tu.isoformat_z(moment.replace(microsecond=0)) == "2026-11-19T06:40:14Z"
    assert tu.parse_iso_utc("2026-11-19T06:40:14Z") == utc(2026, 11, 19, 6, 40, 14)
    assert tu.parse_iso_utc("2026-11-18T22:40:14-08:00") == utc(2026, 11, 19, 6, 40, 14)
    with pytest.raises(ValueError):
        tu.parse_iso_utc("2026-11-19T06:40:14")


# --- NPS live feed ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Strings copied from the 19 Nov 2026 feed samples.
        ("Updated 11/18/2026 10:00 PM PST", utc(2026, 11, 19, 6, 0)),  # porejson
        ("Updated 11/18/2026 11:00 PM MST", utc(2026, 11, 19, 6, 0)),  # grcajson (Arizona)
        ("Updated 11/19/2026 01:00 AM EST", utc(2026, 11, 19, 6, 0)),  # acadjson
        ("Updated 11/19/2026 12:00 AM CST", utc(2026, 11, 19, 6, 0)),  # bibejson, midnight
        ("11/18/2026 11:00 PM MST", utc(2026, 11, 19, 6, 0)),  # no 'Updated' prefix
        ("Updated 07/04/2026 12:00 PM PDT", utc(2026, 7, 4, 19, 0)),  # noon
        ("Updated 07/04/2026 12:15 AM MDT", utc(2026, 7, 4, 6, 15)),
        ("Updated 07/04/2026 9:05 am CDT", utc(2026, 7, 4, 14, 5)),
        ("Updated 07/04/2026 09:05 PM EDT", utc(2026, 7, 5, 1, 5)),
    ],
)
def test_parse_nps_imagedate(text: str, expected: datetime):
    assert tu.parse_nps_imagedate(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Updated 11/18/2026 10:00 PM AKST",
        "Updated 11/18/2026 10:00 PM UTC",
        "Updated 11/18/2026 13:00 PM PST",
        "Updated 11/18/2026 10:00 PST",
        "Updated yesterday",
        "",
    ],
)
def test_parse_nps_imagedate_rejects(text: str):
    with pytest.raises(ValueError):
        tu.parse_nps_imagedate(text)


# --- NPS archive --------------------------------------------------------------------
def test_archive_sample_from_point_reyes():
    # First result of search_pore_20261117.json.
    got = tu.parse_nps_archive_time(
        "11/17/2026 12:00:00 AM", "11/17/2026 12:00 AM Pacific Standard Time", "America/Los_Angeles"
    )
    assert got == utc(2026, 11, 17, 8, 0)
    last = tu.parse_nps_archive_time(
        "11/17/2026 11:45:00 PM", "11/17/2026 11:45 PM Pacific Standard Time", "America/Los_Angeles"
    )
    assert last == utc(2026, 11, 18, 7, 45)


@pytest.mark.parametrize("name", ["Mountain Standard Time", "US Mountain Standard Time"])
@pytest.mark.parametrize("convention", ["label", "wall"])
def test_archive_grand_canyon_is_mst_all_year(name: str, convention: str):
    july = tu.parse_nps_archive_time(
        "7/1/2026 12:00:00 PM",
        f"7/1/2026 12:00 PM {name}",
        "America/Phoenix",
        convention=convention,
    )
    assert july == utc(2026, 7, 1, 19, 0)
    # The LA spring-forward night is an ordinary night in Arizona.
    march = tu.parse_nps_archive_time(
        "3/14/2027 2:30:00 AM",
        f"3/14/2027 2:30 AM {name}",
        "America/Phoenix",
        convention=convention,
    )
    assert march == utc(2027, 3, 14, 9, 30)


def test_archive_daylight_label_is_respected():
    got = tu.parse_nps_archive_time(
        "7/1/2026 12:00:00 PM", "7/1/2026 12:00 PM Pacific Daylight Time", "America/Los_Angeles"
    )
    assert got == utc(2026, 7, 1, 19, 0)


def test_archive_summer_create_time_in_standard_time_is_accepted():
    # Real `pore` asset, 15 Jul 2021 (archive check, 2026-11-19): ImageCreateDateTime stays on
    # PST in summer; LocalTimeString is PDT civil time. Both mean 21:30 UTC, and NPS's own
    # SolarElevationAngle (67.29 deg) matches 21:30 UTC at Point Reyes.
    args = ("07/15/2021 01:30:00 PM", "7/15/2021 2:30 PM Pacific Daylight Time",
            "America/Los_Angeles")
    assert tu.parse_nps_archive_time(*args) == utc(2021, 7, 15, 21, 30)
    assert tu.parse_nps_archive_time(*args, convention="wall") == utc(2021, 7, 15, 21, 30)
    # Any other disagreement is still refused.
    with pytest.raises(ValueError, match="disagree"):
        tu.parse_nps_archive_time("07/15/2021 03:30:00 PM",
                                  "7/15/2021 2:30 PM Pacific Daylight Time", "America/Los_Angeles")
    with pytest.raises(ValueError, match="disagree"):
        tu.parse_nps_archive_time("01/15/2021 01:30:00 PM",
                                  "1/15/2021 2:30 PM Pacific Standard Time", "America/Los_Angeles")


def test_archive_conventions_differ_only_for_standard_labels_in_summer():
    args = (
        "7/1/2026 12:00:00 PM",
        "7/1/2026 12:00 PM Pacific Standard Time",
        "America/Los_Angeles",
    )
    assert tu.parse_nps_archive_time(*args, convention="label") == utc(2026, 7, 1, 20, 0)
    assert tu.parse_nps_archive_time(*args, convention="wall") == utc(2026, 7, 1, 19, 0)


def test_archive_fold_needs_a_daylight_label_in_wall_mode():
    standard = ("11/1/2026 1:30:00 AM", "11/1/2026 1:30 AM Pacific Standard Time")
    daylight = ("11/1/2026 1:30:00 AM", "11/1/2026 1:30 AM Pacific Daylight Time")
    with pytest.raises(AmbiguousTimeError):
        tu.parse_nps_archive_time(*standard, "America/Los_Angeles", convention="wall")
    first = tu.parse_nps_archive_time(*daylight, "America/Los_Angeles", convention="wall")
    assert first == utc(2026, 11, 1, 8, 30)
    # Label mode is never ambiguous: the name fixes the offset.
    assert tu.parse_nps_archive_time(*standard, "America/Los_Angeles") == utc(2026, 11, 1, 9, 30)
    assert tu.parse_nps_archive_time(*daylight, "America/Los_Angeles") == utc(2026, 11, 1, 8, 30)


def test_archive_gap_raises_in_wall_mode():
    args = (
        "3/14/2027 2:30:00 AM",
        "3/14/2027 2:30 AM Pacific Standard Time",
        "America/Los_Angeles",
    )
    with pytest.raises(NonexistentTimeError):
        tu.parse_nps_archive_time(*args, convention="wall")
    assert tu.parse_nps_archive_time(*args) == utc(2027, 3, 14, 10, 30)


@pytest.mark.parametrize(
    ("created", "label", "zone"),
    [
        (
            "11/17/2026 12:00:00 AM",
            "11/17/2026 12:15 AM Pacific Standard Time",
            "America/Los_Angeles",
        ),
        (
            "11/17/2026 12:00:00 AM",
            "11/17/2026 12:00 AM Eastern Standard Time",
            "America/Los_Angeles",
        ),
        ("11/17/2026 12:00:00 AM", "11/17/2026 12:00 AM Atlantic Standard Time", "America/Halifax"),
        ("7/1/2026 12:00:00 PM", "7/1/2026 12:00 PM Mountain Daylight Time", "America/Phoenix"),
        (
            "1/15/2026 12:00:00 PM",
            "1/15/2026 12:00 PM Pacific Daylight Time",
            "America/Los_Angeles",
        ),
        ("garbage", "11/17/2026 12:00 AM Pacific Standard Time", "America/Los_Angeles"),
        ("11/17/2026 12:00:00 AM", "no zone here", "America/Los_Angeles"),
    ],
)
def test_archive_inconsistent_input_raises(created: str, label: str, zone: str):
    with pytest.raises(ValueError):
        tu.parse_nps_archive_time(created, label, zone)


def test_archive_unknown_convention():
    with pytest.raises(ValueError):
        tu.parse_nps_archive_time(
            "11/17/2026 12:00:00 AM",
            "11/17/2026 12:00 AM Pacific Standard Time",
            "America/Los_Angeles",
            convention="guess",  # type: ignore[arg-type]
        )


# --- EXIF -------------------------------------------------------------------------------
def test_exif_rainier_sample():
    # ~/lookout-data/samples/mountain.jpg: DateTime 2026:11:18 20:21:03, Last-Modified 04:21:14Z.
    got, flags = tu.exif_local_to_utc("2026:11:18 20:21:03", "America/Los_Angeles")
    assert got == utc(2026, 11, 19, 4, 21, 3) and flags == frozenset()


def test_exif_offset_and_subsec():
    got, flags = tu.exif_local_to_utc(
        "2026:11:18 20:21:03\x00", "America/Los_Angeles", offset="-08:00", subsec="194"
    )
    assert got == utc(2026, 11, 19, 4, 21, 3, 194000)
    assert flags == {FLAG_OFFSET_FROM_EXIF}


def test_exif_fold_takes_the_earlier_offset():
    got, flags = tu.exif_local_to_utc("2026:11:01 01:30:00", "America/Los_Angeles")
    assert got == utc(2026, 11, 1, 8, 30)  # 01:30 PDT, the first of the two
    assert flags == {FLAG_AMBIGUOUS}


def test_exif_gap_moves_forward():
    got, flags = tu.exif_local_to_utc("2027:03:14 02:30:00", "America/Los_Angeles")
    assert got == utc(2027, 3, 14, 10, 30)  # shown as 03:30 PDT
    assert flags == {FLAG_NONEXISTENT}
    got_ny, flags_ny = tu.exif_local_to_utc("2027:03:14 02:30:00", "America/New_York")
    assert got_ny == utc(2027, 3, 14, 7, 30) and flags_ny == {FLAG_NONEXISTENT}


def test_exif_phoenix_has_no_dst_flags():
    for stamp, expected in [
        ("2026:11:01 01:30:00", utc(2026, 11, 1, 8, 30)),
        ("2027:03:14 02:30:00", utc(2027, 3, 14, 9, 30)),
        ("2026:07:01 12:00:00", utc(2026, 7, 1, 19, 0)),
    ]:
        assert tu.exif_local_to_utc(stamp, "America/Phoenix") == (expected, frozenset())


@pytest.mark.parametrize("stamp", ["0000:00:00 00:00:00", "", "2026-11-18", "2026:13:01 00:00:00"])
def test_exif_rejects(stamp: str):
    with pytest.raises(ValueError):
        tu.exif_local_to_utc(stamp, "America/Los_Angeles")


def test_exif_rejects_bad_offset():
    with pytest.raises(ValueError):
        tu.exif_local_to_utc("2026:11:18 20:21:03", "America/Los_Angeles", offset="PST")


# --- localize policies -----------------------------------------------------------------
def test_localize_policies():
    fold = datetime(2026, 11, 1, 1, 30)
    later, flags = tu.localize(fold, "America/Los_Angeles", on_ambiguous="later")
    assert later == utc(2026, 11, 1, 9, 30) and flags == {FLAG_AMBIGUOUS}
    with pytest.raises(AmbiguousTimeError):
        tu.localize(fold, "America/Los_Angeles", on_ambiguous="raise")
    with pytest.raises(NonexistentTimeError):
        tu.localize(datetime(2027, 3, 14, 2, 30), "America/Los_Angeles", on_nonexistent="raise")
    with pytest.raises(ValueError):
        tu.localize(datetime(2026, 1, 1, tzinfo=UTC), "America/Los_Angeles")
    # Denver's fold, for the Grand Teton camera.
    got, flags = tu.localize(datetime(2026, 11, 1, 1, 15), "America/Denver")
    assert got == utc(2026, 11, 1, 7, 15) and flags == {FLAG_AMBIGUOUS}
