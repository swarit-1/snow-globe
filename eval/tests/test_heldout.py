"""The committed held-out split and its guards (D-019, D-022, D-024; PRD §13.2, §13.9).

One test per rule: the committed periods, the archive guard's date rule (lookout-capture's
cases, reproduced without importing capture), the quarantine, the scoring exclusions, and
the loader's strictness. No network; nothing under ~/lookout-data is read.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import random
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import FIXTURES
from lookout_common.manifest import load_manifest
from lookout_common.storage import LocalStorage
from lookout_common.timeutil import parse_nps_archive_time

from lookout_eval.frames import (
    FrameRecord,
    read_sidecar_for_path,
    record_from_path,
    scan_frames,
    storage_reader,
)
from lookout_eval.heldout import (
    HELDOUT_PERIODS_RESOURCE,
    LeakageError,
    QuarantineGuard,
    SealedScoring,
    _standard_time_date,  # the summer reading, checked directly
    archive_guard_reason,
    committed_split,
    key_quarantine_reason,
    load_split,
    nps_dates,
    nps_local_dates,
    nps_utc_dates,
    path_quarantine_reason,
    split_from_dict,
)
from lookout_eval.leakage import FrameStore

SPLIT_FILE = Path(__file__).resolve().parents[1] / "src" / "lookout_eval" / HELDOUT_PERIODS_RESOURCE
CAPTURE_HELDOUT = (
    Path(__file__).resolve().parents[2] / "workers/capture/src/lookout_capture/heldout.py"
)
RAINIER = "rainier.paradise-mountain"
PORE = "point-reyes.beach"
TETON = "grand-teton.science-center"
DESIGN_H_ONLY = (
    "grand-canyon.yavapai",
    "yosemite.turtleback",
    "acadia.mcfarland",
    "smokies.look-rock",
    "big-bend.panther-junction",
)
ARD = (PORE, *DESIGN_H_ONLY, TETON)
ALL = (RAINIER, *ARD)
LA = "America/Los_Angeles"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _raw() -> dict:
    return json.loads(SPLIT_FILE.read_text(encoding="utf-8"))


def _archive(vp: str, moment: datetime, create: str | None, local: str | None) -> FrameRecord:
    key = f"archive/{vp}/{moment:%Y-%m-%d}/{moment:%Y%m%dT%H%M%SZ}_c7dc841d.jpg"
    return FrameRecord(vp, moment, key, "nps-archive", None, None, create, local)


def _live(vp: str, moment: datetime) -> FrameRecord:
    key = f"frames/{vp}/{moment:%Y-%m-%d}/{moment:%Y%m%dT%H%M%SZ}_abcdef012345.jpg"
    return FrameRecord(vp, moment, key, "live")


# --- the committed split ----------------------------------------------------------------------


def test_the_committed_split_is_versioned_and_hashed() -> None:
    split = committed_split()
    raw = SPLIT_FILE.read_bytes()
    assert split.version == 1 and split.sha256 == hashlib.sha256(raw).hexdigest()
    assert split.to_record() == {
        "kind": "lookout.heldout-periods",
        "version": 1,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source": "lookout_eval/heldout_periods.json",
        "decisions": ["D-019", "D-022", "D-024"],
    }
    assert split.decided == date(2026, 11, 19) and split.freeze == date(2026, 12, 14)


def test_the_committed_periods_are_those_of_d019_d022_d024() -> None:
    """Pinned on purpose: changing the split must change this test in the same commit."""
    split = committed_split()
    assert set(split.viewpoints) == set(ALL)
    sealed = split.viewpoint(RAINIER).period("P")
    assert (sealed.first_day, sealed.last_day) == (date(2026, 12, 15), date(2027, 1, 14))
    assert sealed.extensions == ()  # extended by outages; none yet
    assert split.viewpoint(RAINIER).designs == ("P",)
    assert split.viewpoint(PORE).designs == ("P", "H")
    assert split.viewpoint(PORE).period("P") == sealed  # the same sealed window
    for vp in (PORE, *DESIGN_H_ONLY):
        summer = split.viewpoint(vp).period("H")
        assert (summer.first_day, summer.last_day) == (date(2026, 6, 1), date(2026, 8, 31))
    for vp in DESIGN_H_ONLY:
        assert split.viewpoint(vp).designs == ("H",) and split.viewpoint(vp).scored
    teton = split.viewpoint(TETON)
    assert teton.designs == () and not teton.scored and "D-024" in teton.note
    (exclusion,) = split.scoring_exclusions
    assert exclusion.day == date(2026, 7, 15) and exclusion.viewpoints == frozenset(ARD)
    assert exclusion.source == "D-022"
    assert split.archive_guard.from_day == date(2026, 6, 1)
    assert split.quarantine_dir == "quarantine"


def test_the_split_zones_match_the_viewpoint_manifests() -> None:
    for vp, part in committed_split().viewpoints.items():
        assert part.time_zone == load_manifest(vp).time_zone, vp


@pytest.mark.parametrize(
    ("vp", "start_utc"),
    [
        (PORE, "2026-06-01T07:00:00+00:00"),  # PDT
        ("grand-canyon.yavapai", "2026-06-01T07:00:00+00:00"),  # MST all year (Arizona)
        ("big-bend.panther-junction", "2026-06-01T05:00:00+00:00"),  # CDT
        ("acadia.mcfarland", "2026-06-01T04:00:00+00:00"),  # EDT
        ("smokies.look-rock", "2026-06-01T04:00:00+00:00"),
        ("yosemite.turtleback", "2026-06-01T07:00:00+00:00"),
    ],
)
def test_summer_2026_is_held_out_as_local_days(vp: str, start_utc: str) -> None:
    part = committed_split().viewpoint(vp)
    start = datetime.fromisoformat(start_utc)
    held = part.held_out("H")
    assert held.contains(start) and not held.contains(start - timedelta(seconds=1))
    end = held.intervals[0][1]
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(days=92)  # Jun + Jul + Aug
    assert (
        part.period_at(start) is not None and part.period_at(start - timedelta(minutes=1)) is None
    )


def test_the_sealed_window_is_held_out_as_local_days() -> None:
    split = committed_split()
    for vp in (RAINIER, PORE):
        held = split.held_out(vp, "P")
        assert held.contains(datetime(2026, 12, 15, 8, tzinfo=UTC))  # 00:00 PST
        assert not held.contains(datetime(2026, 12, 15, 7, 59, 59, tzinfo=UTC))
        assert held.contains(datetime(2027, 1, 15, 7, 59, 59, tzinfo=UTC))
        assert not held.contains(datetime(2027, 1, 15, 8, tzinfo=UTC))
    assert split.held_out(TETON).intervals == ()  # no design, no held-out period


def test_fit_bounds_follow_the_design() -> None:
    split = committed_split()
    june_pdt, dec_pst = datetime(2026, 6, 1, 7, tzinfo=UTC), datetime(2026, 12, 15, 8, tzinfo=UTC)
    assert split.fit_bound(RAINIER) == dec_pst == split.fit_bound(RAINIER, "P")
    assert split.fit_bound(PORE) == june_pdt == split.fit_bound(PORE, "H")  # strictest
    assert split.fit_bound(PORE, "P") == dec_pst
    assert split.fit_bound(TETON) is None
    with pytest.raises(LeakageError, match="has no design H"):
        split.fit_bound(RAINIER, "H")
    with pytest.raises(LeakageError, match="no committed held-out split"):
        split.viewpoint("golden-gate.bridge")


# --- the archive guard: lookout-capture's rule, reproduced --------------------------------------


def test_the_archive_guard_matches_lookout_captures_cases() -> None:
    """The same five cases as workers/capture/tests/test_archive.py (no import of capture)."""
    assert (
        archive_guard_reason(
            "01/15/2026 12:00:00 PM", "1/15/2026 12:00 PM Pacific Standard Time", LA
        )
        is None
    )
    assert (
        archive_guard_reason(
            "07/15/2025 01:15:00 PM", "7/15/2025 2:15 PM Pacific Daylight Time", LA
        )
        is None
    )
    summer = archive_guard_reason(
        "07/15/2026 01:15:00 PM", "7/15/2026 2:15 PM Pacific Daylight Time", LA
    )
    assert summer is not None and "2026-07-15" in summer
    # Standard-time stamp on 31 May, civil time already 1 Jun: refused.
    edge = archive_guard_reason(
        "05/31/2026 11:30:00 PM", "6/1/2026 12:30 AM Pacific Daylight Time", LA
    )
    assert edge is not None and "2026-06-01" in edge
    assert archive_guard_reason("garbage", "", LA) is not None  # no readable date: refused


def test_local_dates_read_all_three_sources() -> None:
    assert nps_local_dates(
        "05/31/2026 11:30:00 PM", "6/1/2026 12:30 AM Pacific Daylight Time", LA
    ) == [date(2026, 5, 31), date(2026, 6, 1)]
    assert nps_local_dates("garbage", "", LA) == []
    # Arizona keeps MST: 23:30 on 31 May is 06:30 UTC on 1 Jun, still 31 May locally. The UTC
    # reading (the lead, 20 Nov 2026, pending D-038) now holds it out.
    phoenix = nps_local_dates(
        "05/31/2026 11:30:00 PM", "5/31/2026 11:30 PM US Mountain Standard Time", "America/Phoenix"
    )
    assert phoenix == [date(2026, 5, 31)]
    held = archive_guard_reason(
        "05/31/2026 11:30:00 PM",
        "5/31/2026 11:30 PM US Mountain Standard Time",
        "America/Phoenix",
    )
    assert held is not None and "UTC date 2026-06-01" in held
    eastern = archive_guard_reason(
        "05/31/2026 11:30:00 PM", "6/1/2026 12:30 AM Eastern Daylight Time", "America/New_York"
    )
    assert eastern is not None and "2026-06-01" in eastern


def test_each_nps_string_can_decide_alone() -> None:
    """When the UTC time cannot be parsed, each string's own date still counts (fail closed)."""
    # The label's zone disagrees with the viewpoint's, so no UTC time: LocalTimeString decides.
    mismatch = ("05/31/2026 11:30:00 PM", "6/1/2026 12:30 AM Pacific Daylight Time")
    assert nps_local_dates(*mismatch, "America/Denver") == [date(2026, 5, 31), date(2026, 6, 1)]
    assert "2026-06-01" in (archive_guard_reason(*mismatch, "America/Denver") or "")
    # An unreadable clock in ImageCreateDateTime: its date still counts, and decides here.
    clock = ("06/01/2026 25:61", "5/31/2026 11:30 PM Pacific Daylight Time")
    assert nps_local_dates(*clock, LA) == [date(2026, 5, 31), date(2026, 6, 1)]
    assert "2026-06-01" in (archive_guard_reason(*clock, LA) or "")
    # With the frame's own UTC time as a fourth date (eval's records carry one).
    late_utc = datetime(2026, 6, 1, 7, 30, tzinfo=UTC)
    assert archive_guard_reason("", "", LA, utc_time=late_utc) is not None
    # Its UTC date counts as well as its local one: 00:00 UTC on 1 Jun (17:00 PDT) is held out.
    midnight = datetime(2026, 6, 1, tzinfo=UTC)
    assert "UTC date" in (archive_guard_reason("", "", LA, utc_time=midnight) or "")
    assert archive_guard_reason("", "", LA, utc_time=midnight - timedelta(seconds=1)) is None


def test_the_summer_edge_fails_closed_without_local_time_string() -> None:
    """Review finding: ImageCreateDateTime stays on standard time in summer, so with no
    readable LocalTimeString "05/31/2026 11:30:00 PM" (00:30 PDT on 1 Jun) used to pass."""
    edge = "05/31/2026 11:30:00 PM"
    assert nps_local_dates(edge, "", LA) == [date(2026, 5, 31), date(2026, 6, 1)]
    for local in ("", "garbage", "6/1/2026"):  # missing, unreadable, no clock
        assert "2026-06-01" in (archive_guard_reason(edge, local, LA) or ""), local
    as_civil = datetime(2026, 6, 1, 6, 30, tzinfo=UTC)  # the UTC time if read as 23:30 PDT
    assert "2026-06-01" in (archive_guard_reason(edge, "", LA, utc_time=as_civil) or "")
    # The local readings turn at local midnight: 10:59:59 PM PST is 23:59:59 PDT on 31 May;
    # 11:00 PM PST is midnight. The guard turns earlier, at UTC midnight (the UTC reading):
    # 03:59:59 PM PST (23:59:59 UTC) is allowed, 04:00:00 PM PST is refused.
    assert nps_local_dates("05/31/2026 10:59:59 PM", "", LA) == [date(2026, 5, 31)]
    assert date(2026, 6, 1) in nps_local_dates("05/31/2026 11:00:00 PM", "", LA)
    assert archive_guard_reason("05/31/2026 03:59:59 PM", "", LA) is None
    assert "UTC date" in (archive_guard_reason("05/31/2026 04:00:00 PM", "", LA) or "")
    assert archive_guard_reason("05/31/2026 12:00:00 PM", "", LA) is None  # noon, not midnight
    # No daylight time (Arizona), or winter: the stamp's own date locally.
    assert nps_local_dates(edge, "", "America/Phoenix") == [date(2026, 5, 31)]
    assert nps_local_dates("11/30/2025 11:30:00 PM", "", LA) == [date(2025, 11, 30)]
    # Eastern and Central: the same edge in their own zones.
    for zone in ("America/New_York", "America/Chicago", "America/Denver"):
        assert archive_guard_reason(edge, "", zone) is not None, zone


#: The last ImageCreateDateTime of 31 May 2026 (local standard time, NPS's convention) still
#: before UTC midnight, per zone; the same table as lookout-capture's test_archive.py.
UTC_EDGE = {
    "America/Los_Angeles": ("03:59:59 PM", "04:00:00 PM", "Pacific"),
    "America/Phoenix": ("04:59:59 PM", "05:00:00 PM", "US Mountain"),
    "America/Denver": ("04:59:59 PM", "05:00:00 PM", "Mountain"),
    "America/Chicago": ("05:59:59 PM", "06:00:00 PM", "Central"),
    "America/New_York": ("06:59:59 PM", "07:00:00 PM", "Eastern"),
}


@pytest.mark.parametrize("zone", sorted(UTC_EDGE))
def test_the_guard_also_reads_utc_so_31_may_local_evening_is_held_out(zone: str) -> None:
    """The lead's reading of 20 Nov 2026 (pending the owner's D-038): an asset is held out if
    any reading of its date, local or UTC, is on or after 1 Jun 2026. A 31 May local-evening
    frame is already 1 Jun in UTC, so the guard turns at UTC midnight: one second earlier is
    allowed, UTC midnight is refused, with or without a LocalTimeString. Same cases as
    lookout-capture's test of ``held_out_reason``."""
    last_ok, first_out, region = UTC_EDGE[zone]
    daylight = zone != "America/Phoenix"
    kind = "Daylight" if daylight else "Standard"

    def label(stamp: str) -> str:  # NPS's civil-time label: an hour ahead in daylight time
        wall = datetime.strptime(f"05/31/2026 {stamp}", "%m/%d/%Y %I:%M:%S %p")
        civil = wall + timedelta(hours=1 if daylight else 0)
        clock = f"{civil.hour % 12 or 12}:{civil:%M} {'PM' if civil.hour >= 12 else 'AM'}"
        return f"{civil.month}/{civil.day}/{civil.year} {clock} {region} {kind} Time"

    for with_label in (True, False):
        ok = (f"05/31/2026 {last_ok}", label(last_ok) if with_label else "")
        out = (f"05/31/2026 {first_out}", label(first_out) if with_label else "")
        assert nps_local_dates(*out, zone) == [date(2026, 5, 31)]  # local readings: 31 May
        assert archive_guard_reason(*ok, zone) is None, (zone, ok)
        reason = archive_guard_reason(*out, zone)
        assert reason is not None and "UTC date 2026-06-01" in reason, (zone, out, reason)
        assert nps_utc_dates(*out, zone)[-1] == date(2026, 6, 1)


def test_every_utc_reading_counts_and_a_date_without_a_clock_fails_closed() -> None:
    """``nps_utc_dates``: the instant under both conventions, each local clock converted in the
    camera's zone (both sides of a DST fold), and for a reading with a date but no readable
    clock every UTC date its local day touches. Same cases as lookout-capture's."""
    assert nps_utc_dates("", "5/31/2026 11:30 PM Pacific Daylight Time", LA) == [date(2026, 6, 1)]
    assert nps_utc_dates("", "5/31/2026", LA) == [date(2026, 5, 31), date(2026, 6, 1)]
    assert archive_guard_reason("", "5/31/2026", LA) is not None
    assert date(2026, 6, 1) in nps_utc_dates("05/31/2026 25:61", "", LA)
    assert nps_utc_dates("11/01/2026 01:30:00 AM", "", LA) == [date(2026, 11, 1)]
    instant = datetime(2026, 6, 1, 0, 30, tzinfo=UTC)  # 17:30 PDT on 31 May
    assert archive_guard_reason("01/15/2026 12:00:00 PM", "", LA, utc_time=instant) is not None
    assert nps_dates("", "", LA, utc_time=instant) == [date(2026, 5, 31), date(2026, 6, 1)]
    with pytest.raises(ValueError, match="aware"):
        nps_dates("", "", LA, utc_time=datetime(2026, 6, 1))
    assert archive_guard_reason("01/15/2026 11:30:00 PM", "", LA) is None
    assert archive_guard_reason("05/30/2026 11:45:00 PM", "", LA) is None


def _load_capture_rule(monkeypatch: pytest.MonkeyPatch):
    """lookout-capture's heldout.py, loaded from its file (eval does not depend on capture).

    It is registered in ``sys.modules`` only for this test (its dataclass needs that).
    """
    if not CAPTURE_HELDOUT.is_file():
        pytest.skip("workers/capture is not in this checkout")
    name = "_capture_heldout_rule"
    spec = importlib.util.spec_from_file_location(name, CAPTURE_HELDOUT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # it grew a dependency eval's venv lacks
        pytest.skip(f"lookout_capture/heldout.py needs {exc.name}")
    return module


_LABELS = {
    "America/Los_Angeles": "Pacific",
    "America/Phoenix": "US Mountain",
    "America/Denver": "Mountain",
    "America/Chicago": "Central",
    "America/New_York": "Eastern",
}


def _clock(moment: datetime, *, nps_create: bool) -> str:
    """NPS's two clock forms: "05/31/2026 01:30:00 PM" (ImageCreateDateTime) and
    "5/31/2026 1:30 PM" (the head of LocalTimeString)."""
    hour = moment.hour % 12 or 12
    meridiem = "PM" if moment.hour >= 12 else "AM"
    if nps_create:
        return f"{moment:%m/%d/%Y} {hour:02d}:{moment:%M:%S} {meridiem}"
    return f"{moment.month}/{moment.day}/{moment.year} {hour}:{moment:%M} {meridiem}"


def _nps_case(rng: random.Random) -> tuple[str, str, str]:
    """One archive asset's (ImageCreateDateTime, LocalTimeString, zone), right or broken."""
    zone_name = rng.choice(sorted(_LABELS))
    zone = ZoneInfo(zone_name)
    where = rng.random()
    if where < 0.5:  # within two hours of the guard's local midnight: the edge
        civil = datetime(2026, 6, 1, tzinfo=zone) + timedelta(minutes=rng.randrange(-120, 120))
    elif where < 0.8:
        civil = datetime(2026, 5, 29, tzinfo=zone) + timedelta(minutes=rng.randrange(5 * 1440))
    else:
        civil = datetime(2021, 1, 1, tzinfo=zone) + timedelta(minutes=rng.randrange(2400 * 1440))
    standard = civil.replace(tzinfo=None) - (civil.dst() or timedelta(0))
    region, kind = _LABELS[zone_name], "Daylight" if civil.dst() else "Standard"
    create = _clock(standard, nps_create=True)
    label = f"{_clock(civil.replace(tzinfo=None), nps_create=False)} {region} {kind} Time"
    roll = rng.random()
    if roll < 0.15:
        label = ""
    elif roll < 0.25:
        label = "garbage"
    elif roll < 0.35:  # the label shows the standard-time clock
        label = f"{_clock(standard, nps_create=False)} {region} {kind} Time"
    elif roll < 0.45:  # another zone's name
        label = label.replace(region, rng.choice(sorted(set(_LABELS.values()))))
    elif roll < 0.5:
        create = rng.choice(["", "garbage", f"{civil:%m/%d/%Y} 25:61"])
    return create, label, zone_name


def test_the_guard_is_in_parity_with_lookout_captures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fuzz against capture's own rule (its source file, loaded in this test only).

    The two read exactly the same dates, local and UTC, and refuse exactly the same assets,
    with and without the instant behind an archive key (eval's ``utc_time``, capture's
    ``instant``). Since D-047 capture also reads the standard-time date; since the lead's UTC
    reading (20 Nov 2026) both read the UTC dates. Every case stamped 31 May whose civil time
    or UTC date is 1 Jun (the summer edge) is refused, whatever its label says.
    """
    capture = _load_capture_rule(monkeypatch)
    guard = committed_split().archive_guard.from_day
    rng = random.Random(20261120)
    edge = utc_only = 0
    for _ in range(6000):
        create, label, zone = _nps_case(rng)
        case = (create, label, zone)
        local = nps_local_dates(create, label, zone)
        utc = nps_utc_dates(create, label, zone)
        assert local == capture.asset_local_dates(create, label, zone), case
        assert utc == capture.asset_utc_dates(create, label, zone), case
        instant = None
        if rng.random() < 0.3:
            instant = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(minutes=rng.randrange(-900, 900))
        ours = nps_dates(create, label, zone, utc_time=instant)
        assert ours == capture.asset_dates(create, label, zone, instant=instant), case
        refused = archive_guard_reason(create, label, zone, utc_time=instant) is not None
        theirs = capture.held_out_reason(create, label, zone, instant=instant) is not None
        assert refused == theirs, (case, instant)
        standard = _standard_time_date(create, zone)
        if standard is not None and standard >= guard:
            assert refused, case
            stamped = datetime.strptime(create.split(" ", 1)[0], "%m/%d/%Y").date()
            edge += stamped < guard  # stamped 31 May, civil time 1 Jun
        if refused and local and max(local) < guard and instant is None:
            utc_only += 1  # refused only by a UTC reading: 31 May local evening
    assert edge > 20 and utc_only > 50  # the fuzz reaches both edges


def test_any_archive_sign_brings_the_archive_guard() -> None:
    """An archive key, an nps-archive source or NPS's archive times: each alone is enough."""
    split = committed_split()
    september = datetime(2026, 9, 15, 20, tzinfo=UTC)  # past the guard; in no Rainier period
    live_key = f"frames/{RAINIER}/2026-09-15/20260915T200000Z_abcdef012345.jpg"
    archive_key = f"archive/{RAINIER}/2026-09-15/20260915T200000Z_c7dc841d.jpg"
    nps = ("09/15/2026 12:00:00 PM", "9/15/2026 1:00 PM Pacific Daylight Time")
    signs = {
        "source only": FrameRecord(RAINIER, september, live_key, "nps-archive"),
        "key only": FrameRecord(RAINIER, september, archive_key, "live"),
        "NPS times only": FrameRecord(RAINIER, september, live_key, "live", None, None, *nps),
    }
    for name, record in signs.items():
        assert record.is_archive, name
        assert "archive guard" in (split.held_out_reason(record) or ""), name
    plain = FrameRecord(RAINIER, september, live_key, "live")
    assert not plain.is_archive and split.held_out_reason(plain) is None


def test_the_guard_date_agrees_with_lookout_capture() -> None:
    """Drift check: capture's HELD_OUT_FROM and the committed archive guard name one date."""
    if not CAPTURE_HELDOUT.is_file():
        pytest.skip("workers/capture is not in this checkout")
    match = re.search(
        r"^HELD_OUT_FROM = date\((\d{4}), (\d{1,2}), (\d{1,2})\)$",
        CAPTURE_HELDOUT.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, "HELD_OUT_FROM not found in lookout_capture/heldout.py"
    capture_date = date(*(int(g) for g in match.groups()))
    assert capture_date == committed_split().archive_guard.from_day


def test_the_guard_on_real_nps_strings() -> None:
    """The recorded Point Reyes search of 17 Nov 2026 (metadata): every asset is past the guard,
    dated 17 Nov locally by all three sources; from 16:00 PST on it is 18 Nov in UTC."""
    results = json.loads((FIXTURES / "nps_search_pore_20261117_subset.json").read_text())
    assets = [r["Asset"] for r in results["Results"]]
    assert len(assets) == 26
    for asset in assets:
        create, label = asset["ImageCreateDateTime"], asset["LocalTimeString"]
        assert nps_local_dates(create, label, LA) == [date(2026, 11, 17)], asset
        instant = parse_nps_archive_time(create, label, LA)
        assert nps_utc_dates(create, label, LA) == [instant.date()], asset
        reason = archive_guard_reason(create, label, LA)
        assert reason is not None and max(date(2026, 11, 17), instant.date()).isoformat() in reason


def test_the_guard_on_records() -> None:
    split = committed_split()
    # The summer edge with a UTC time read as civil 23:30 PDT on 31 May: NPS's 1 Jun decides.
    edge = _archive(
        PORE,
        datetime(2026, 6, 1, 6, 30, tzinfo=UTC),
        "05/31/2026 11:30:00 PM",
        "6/1/2026 12:30 AM Pacific Daylight Time",
    )
    assert "2026-06-01" in (split.archive_reason(edge) or "")
    # The same instant without NPS strings is 31 May locally but 1 Jun in UTC: held out now.
    assert "UTC date" in (split.archive_reason(_archive(PORE, edge.image_time, None, None)) or "")
    evening = datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)  # 16:59:59 PDT
    assert split.archive_reason(_archive(PORE, evening, None, None)) is None
    # NPS strings that do not parse fail closed, even with a usable UTC time.
    broken = _archive(PORE, datetime(2026, 1, 15, 20, tzinfo=UTC), "garbage", "")
    assert "no readable date" in (split.archive_reason(broken) or "")
    # Live frames are not archived frames: the guard does not apply (the periods do).
    assert split.archive_reason(_live(PORE, datetime(2026, 11, 19, 20, tzinfo=UTC))) is None
    # At every viewpoint, Rainier (no archive) and Grand Teton (no design) included.
    for vp in ALL:
        late = _archive(vp, datetime(2026, 9, 15, 20, tzinfo=UTC), None, None)
        assert split.held_out_reason(late) is not None, vp


def test_a_frame_dated_15_jul_2026() -> None:
    """The breach day (D-022): held out at Point Reyes and excluded from scoring at the ARD
    cameras; at Grand Teton, past the archive guard and never scored."""
    split = committed_split()
    moment = datetime(2026, 7, 15, 20, 15, tzinfo=UTC)
    hires = _archive(
        PORE, moment, "07/15/2026 12:15:00 PM", "7/15/2026 1:15 PM Pacific Daylight Time"
    )
    assert hires.key.endswith("20260715T201500Z_c7dc841d.jpg")  # the hires viewed in the breach
    assert "summer-2026" in (split.held_out_reason(hires) or "")
    assert "2026-07-15" in (split.archive_reason(hires) or "")
    assert "D-022" in (split.scoring_exclusion_reason(PORE, moment) or "")
    teton = _archive(TETON, moment, None, None)
    assert "archive guard" in (split.held_out_reason(teton) or "")
    assert "excluded from scored metrics" in (split.scoring_exclusion_reason(TETON, moment) or "")
    # a live Rainier frame that day: not held out (Rainier is design P), not excluded
    assert split.held_out_reason(_live(RAINIER, moment)) is None
    assert split.scoring_exclusion_reason(RAINIER, moment) is None


@pytest.mark.parametrize(
    ("vp", "local_midnight_utc"),
    [
        (PORE, datetime(2026, 7, 15, 7, tzinfo=UTC)),
        ("grand-canyon.yavapai", datetime(2026, 7, 15, 7, tzinfo=UTC)),
        ("big-bend.panther-junction", datetime(2026, 7, 15, 5, tzinfo=UTC)),
        ("acadia.mcfarland", datetime(2026, 7, 15, 4, tzinfo=UTC)),
        ("smokies.look-rock", datetime(2026, 7, 15, 4, tzinfo=UTC)),
        ("yosemite.turtleback", datetime(2026, 7, 15, 7, tzinfo=UTC)),
    ],
)
def test_15_jul_is_excluded_as_a_local_day(vp: str, local_midnight_utc: datetime) -> None:
    split = committed_split()
    second = timedelta(seconds=1)
    assert split.scoring_exclusion_reason(vp, local_midnight_utc) is not None
    assert split.scoring_exclusion_reason(vp, local_midnight_utc - second) is None
    assert split.scoring_exclusion_reason(vp, local_midnight_utc + timedelta(days=1) - second)
    assert split.scoring_exclusion_reason(vp, local_midnight_utc + timedelta(days=1)) is None


def test_the_scoring_mask() -> None:
    split = committed_split()
    times = [datetime(2026, 7, d, 20, tzinfo=UTC) for d in (14, 15, 16)]
    assert split.scoring_excluded_mask(PORE, times).tolist() == [False, True, False]
    assert split.scoring_excluded_mask(RAINIER, times).tolist() == [False, False, False]
    assert split.scoring_excluded_mask(TETON, times).tolist() == [True, True, True]


# --- the quarantine ----------------------------------------------------------------------------


def test_quarantined_keys_and_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "lookout-data"
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(root))
    assert key_quarantine_reason("quarantine/held-out-breach/archive/x.jpg")
    assert key_quarantine_reason("archive/point-reyes.beach/2026-01-15/x.jpg") is None
    assert key_quarantine_reason("archive/quarantine-notes/x.jpg") is None  # exact name only
    inside = root / "quarantine" / "breach" / "archive" / PORE / "2026-07-15" / "a.jpg"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"")
    assert path_quarantine_reason(inside)
    # a symlink into the quarantine is refused where it points
    link = root / "archive" / PORE / "2026-07-15" / "b.jpg"
    link.parent.mkdir(parents=True)
    link.symlink_to(inside)
    assert path_quarantine_reason(link)
    # a quarantine folder that is itself a symlink, reached through its target
    elsewhere = tmp_path / "offsite"
    elsewhere.mkdir()
    other_root = tmp_path / "other-data"
    other_root.mkdir()
    (other_root / "quarantine").symlink_to(elsewhere)
    assert path_quarantine_reason(elsewhere / "c.jpg", roots=(other_root,))
    assert path_quarantine_reason(elsewhere / "c.jpg") is None
    assert path_quarantine_reason(root / "archive" / PORE / "2026-01-15" / "d.jpg") is None


def test_the_catalogue_never_reads_the_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "lookout-data"
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(root))
    folder = root / "quarantine" / "breach" / "archive" / PORE / "2026-07-15"
    folder.mkdir(parents=True)
    frame = folder / "20260715T201500Z_c7dc841d.jpg"
    frame.write_bytes(b"")
    frame.with_suffix(".json").write_text("{}")
    with pytest.raises(LeakageError, match="D-022"):
        record_from_path(frame, data_root=root)
    with pytest.raises(LeakageError, match="D-022"):
        read_sidecar_for_path(frame)
    with pytest.raises(LeakageError, match="quarantined"):
        scan_frames(LocalStorage(root / "quarantine" / "breach", check=False), PORE)


def _case_insensitive(folder: Path) -> bool:
    probe = folder / "case-probe"
    probe.mkdir()
    try:
        return (folder / "CASE-PROBE").exists()
    finally:
        probe.rmdir()


def _quarantined_frame(root: Path) -> Path:
    """A frame and sidecar under <root>/quarantine/, spelled as the split names the folder."""
    folder = root / "quarantine" / "held-out-breach" / "frames" / RAINIER / "2026-11-18"
    folder.mkdir(parents=True)
    frame = folder / "20261118T200000Z_aaaaaaaaaaaa.jpg"
    frame.write_bytes(b"QUARANTINED-BYTES")
    frame.with_suffix(".json").write_text(
        json.dumps({"viewpoint_id": RAINIER, "image_time_utc": "2026-11-18T20:00:00Z"})
    )
    return frame


def test_the_quarantine_is_matched_in_any_letter_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding: on this Mac's case-insensitive APFS, <root>/QUARANTINE/... opens the
    quarantined files, and the check used to compare the folder name as spelled."""
    root = tmp_path / "lookout-data"
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(root))
    frame = _quarantined_frame(root)
    tail = frame.relative_to(root / "quarantine")
    for spelling in ("QUARANTINE", "Quarantine", "qUaRaNtInE"):
        variant = root / spelling / tail
        assert path_quarantine_reason(variant), spelling
        assert path_quarantine_reason(variant, roots=(root,)), spelling
        assert key_quarantine_reason(f"{spelling}/{tail.as_posix()}"), spelling
        with pytest.raises(LeakageError, match="D-022"):
            read_sidecar_for_path(variant)
        with pytest.raises(LeakageError, match="D-022"):
            record_from_path(variant, data_root=root)
    assert key_quarantine_reason("archive/Quarantine-notes/x.jpg") is None  # whole names only
    assert key_quarantine_reason(f"frames/{RAINIER}/2026-11-18/x.jpg") is None


def test_the_quarantine_is_matched_by_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quarantine folder that is a symlink, reached by a case variant of its target: no
    'quarantine' component and no matching spelling, so only the folder's identity tells."""
    if not _case_insensitive(tmp_path):
        pytest.skip("a case-sensitive file system: spelling variants are other folders")
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(tmp_path / "lookout-data"))
    offsite = tmp_path / "offsite"
    (offsite / "breach").mkdir(parents=True)
    (offsite / "breach" / "c.jpg").write_bytes(b"QUARANTINED-BYTES")
    other_root = tmp_path / "other-data"
    other_root.mkdir()
    (other_root / "quarantine").symlink_to(offsite)
    variant = tmp_path / "OFFSITE" / "breach" / "c.jpg"
    assert variant.read_bytes() == b"QUARANTINED-BYTES"  # the same file under another name
    reason = path_quarantine_reason(variant, roots=(other_root,))
    assert reason is not None and "device and inode" in reason
    cached = QuarantineGuard((other_root,), cache_dirs=True)
    assert cached.reason(variant) == reason and cached.reason(variant) == reason
    assert path_quarantine_reason(tmp_path / "OFFSITE", roots=(other_root,))  # the folder itself
    assert path_quarantine_reason(variant) is None  # not a quarantine of these roots


def test_each_quarantine_layer_alone_catches_a_linked_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quarantine folder that is a symlink elsewhere: the resolved-path check catches its
    target even when the identity check is unavailable (defence in depth)."""
    import lookout_eval.heldout as heldout

    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(tmp_path / "lookout-data"))
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    (offsite / "c.jpg").write_bytes(b"QUARANTINED-BYTES")
    other_root = tmp_path / "other-data"
    other_root.mkdir()
    (other_root / "quarantine").symlink_to(offsite)
    target = offsite / "c.jpg"
    assert path_quarantine_reason(target, roots=(other_root,))
    monkeypatch.setattr(heldout, "_identity", lambda path: None)  # no identities at all
    reason = path_quarantine_reason(target, roots=(other_root,))
    assert reason is not None and f"is under {offsite.resolve()}" in reason


def test_the_catalogue_refuses_links_into_the_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding: scan_frames listed a frames/ file that links into the quarantine, and
    a key-based reader then read the quarantined bytes."""
    root = tmp_path / "lookout-data"
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(root))
    frame = _quarantined_frame(root)
    storage = LocalStorage(root, check=False)
    day = root / "frames" / RAINIER / "2026-11-18"
    day.mkdir(parents=True)
    link = day / frame.name
    link.symlink_to(frame)
    link.with_suffix(".json").symlink_to(frame.with_suffix(".json"))
    with pytest.raises(LeakageError, match=r"scan_frames: frames/.*D-022"):
        scan_frames(storage, RAINIER)
    # Only the sidecar links into the quarantine: still refused, before it is read.
    link.unlink()
    link.write_bytes(b"a development frame")
    with pytest.raises(LeakageError, match=r"\.json is under a quarantine/ folder"):
        scan_frames(storage, RAINIER)
    # A whole viewpoint folder that links into the quarantine.
    for path in day.iterdir():
        path.unlink()
    day.rmdir()
    (root / "frames" / RAINIER).rmdir()
    (root / "frames" / RAINIER).symlink_to(frame.parents[1])
    with pytest.raises(LeakageError, match="D-022"):
        scan_frames(storage, RAINIER)


def test_a_sidecar_is_checked_against_the_given_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data root whose quarantine folder is a symlink elsewhere: a sidecar that links there
    is refused only when the check knows that root, so record_from_path passes it on."""
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(tmp_path / "lookout-data"))
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    (offsite / "breach.json").write_text("{}")
    root = tmp_path / "other-data"
    root.mkdir()
    (root / "quarantine").symlink_to(offsite)
    folder = root / "frames" / RAINIER / "2026-11-18"
    folder.mkdir(parents=True)
    frame = folder / "20261118T200000Z_aaaaaaaaaaaa.jpg"
    frame.write_bytes(b"a development frame")
    frame.with_suffix(".json").symlink_to(offsite / "breach.json")
    assert read_sidecar_for_path(frame) == {}  # $LOOKOUT_DATA_DIR does not name this root
    with pytest.raises(LeakageError, match="D-022"):
        read_sidecar_for_path(frame, roots=(root,))
    with pytest.raises(LeakageError, match="D-022"):
        record_from_path(frame, data_root=root)


def test_the_store_reader_checks_the_quarantine_at_every_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link made after the catalogue was scanned is refused when the store reads it."""
    root = tmp_path / "lookout-data"
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(root))
    quarantined = _quarantined_frame(root)
    day = root / "frames" / RAINIER / "2026-11-18"
    day.mkdir(parents=True)
    frame = day / quarantined.name
    frame.write_bytes(b"a development frame")
    frame.with_suffix(".json").write_text(
        json.dumps({"viewpoint_id": RAINIER, "image_time_utc": "2026-11-18T20:00:00Z"})
    )
    storage = LocalStorage(root, check=False)
    (record,) = scan_frames(storage, RAINIER)
    store = FrameStore([record], reader=storage_reader(storage))
    view = store.as_of(datetime(2026, 11, 19, tzinfo=UTC), purpose="test")
    assert view.read_bytes(record) == b"a development frame"
    frame.unlink()
    frame.symlink_to(quarantined)  # swapped for a link after the scan
    with pytest.raises(LeakageError, match="D-022"):
        view.read_bytes(record)


def test_sealed_scoring_needs_a_full_commit() -> None:
    assert SealedScoring(COMMIT).preregistration_commit == COMMIT
    for bad in ("abc123", COMMIT.upper(), COMMIT + "0", "g" * 40):
        with pytest.raises(ValueError, match="40-character"):
            SealedScoring(bad)


# --- the loader ---------------------------------------------------------------------------------


def _load(data: dict):
    return split_from_dict(data, sha256="0" * 64, source="test")


def test_an_extension_extends_the_sealed_window(tmp_path: Path) -> None:
    data = _raw()
    data["version"] = 2
    data["periods"]["sealed-window"]["extensions"] = [
        {"first_day": "2027-01-15", "last_day": "2027-01-17", "reason": "test outage of 3 days"}
    ]
    path = tmp_path / "split.json"
    path.write_text(json.dumps(data))
    split = load_split(path)
    assert split.version == 2 and split.sha256 != committed_split().sha256
    for vp in (RAINIER, PORE):
        held = split.held_out(vp, "P")
        assert held.contains(datetime(2027, 1, 18, 7, 59, tzinfo=UTC))
        assert not held.contains(datetime(2027, 1, 18, 8, tzinfo=UTC))
        assert split.fit_bound(vp, "P") == committed_split().fit_bound(vp, "P")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(kind="other"), "kind must be"),
        (lambda d: d.update(schema_version=2), "schema_version"),
        (lambda d: d.update(version=0), "positive integer"),
        (lambda d: d.update(surprise=True), "unknown keys"),
        (lambda d: d.pop("archive_guard"), "missing"),
        (lambda d: d["archive_guard"].update(viewpoints=[RAINIER]), "must be 'all'"),
        (lambda d: d["archive_guard"].update(from_local_day="2026-6-1"), "YYYY-MM-DD"),
        (lambda d: d["periods"]["summer-2026"].update(last_day="2026-05-31"), "ends before"),
        (lambda d: d["periods"]["summer-2026"].update(design="X"), "design must be"),
        (lambda d: d["viewpoints"][RAINIER]["designs"].update(P="summer-2026"), "design-H period"),
        (lambda d: d["viewpoints"][RAINIER]["designs"].update(H="nowhere"), "names no period"),
        (lambda d: d["viewpoints"][TETON].update(scored=True), "scored exactly when"),
        (lambda d: d["viewpoints"][RAINIER].update(time_zone="Mars/Olympus"), "IANA zone"),
        (lambda d: d["scoring_exclusions"][0]["viewpoints"].append("x.y"), "viewpoints of this"),
        (lambda d: d["quarantine"].update(dir="a/b"), "one folder name"),
        (
            lambda d: d["periods"]["sealed-window"]["extensions"].append(
                {"first_day": "2027-01-20", "last_day": "2027-01-21", "reason": "gap"}
            ),
            "the day after",
        ),
        (
            lambda d: d["periods"]["sealed-window"]["extensions"].append(
                {"first_day": "2027-01-15", "last_day": "2027-01-15", "reason": " "}
            ),
            "reason",
        ),
    ],
)
def test_the_loader_is_strict(change, message: str) -> None:
    data = copy.deepcopy(_raw())
    change(data)
    with pytest.raises(ValueError, match=message):
        _load(data)


def test_duplicate_keys_are_refused(tmp_path: Path) -> None:
    text = SPLIT_FILE.read_text(encoding="utf-8")
    doubled = text.replace(
        '"viewpoints": {\n', '"viewpoints": {\n    "rainier.paradise-mountain": {},\n', 1
    )
    assert doubled != text
    path = tmp_path / "split.json"
    path.write_text(doubled)
    with pytest.raises(ValueError, match="duplicate key"):
        load_split(path)
