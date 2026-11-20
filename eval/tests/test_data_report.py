"""Tests for ``lookout-eval data-report`` (PRD §16 M0.5; PLAN.md gate 1).

Every test builds a small synthetic data dir under ``tmp_path``: day logs in the jobs' own
record shapes, sidecars and forecast files. No real data is read and nothing is fetched.
"""

from __future__ import annotations

import json
import os
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from lookout_common.timeutil import isoformat_z

from lookout_eval import cli
from lookout_eval.data_report import REPORT_KIND, ReportOptions, build_report, format_report
from lookout_eval.data_report.common import (
    Request,
    Window,
    clip,
    close_time,
    floor_ms,
    max_in_window,
    measure,
    merge,
    provable_spans,
    without_home,
)
from lookout_eval.data_report.forecast import KIND_LATE, KIND_NO_RUN

T0 = datetime(2026, 11, 19, 12, 24, 23, tzinfo=UTC)
#: The forecast scan starts 6 h before the window's first hour (forecast.py ``_SCAN_BACK``).
SCAN_FROM = datetime(2026, 11, 19, 6, tzinfo=UTC)
V1 = "lookout-capture/0.1.0"
V2 = "lookout-capture/0.2.0"
ACADIA, RAINIER, REYES = "acadia.mcfarland", "rainier.paradise-mountain", "point-reyes.beach"
FIELDS7 = [
    "VIS:surface",
    "HGT:cloud ceiling",
    "HGT:cloud base",
    "LCDC:low cloud layer",
    "MCDC:middle cloud layer",
    "HCDC:high cloud layer",
    "HGT:surface",
]
FIELDS8 = [*FIELDS7[:3], "PRES:cloud base", *FIELDS7[3:]]


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def z(moment: datetime) -> str:
    return isoformat_z(moment, "milliseconds")


def hour(h: int) -> datetime:
    """The HRRR cycle at ``h`` UTC on 19 Nov 2026."""
    return datetime(2026, 11, 19, h, tzinfo=UTC)


# --- record builders (the shapes capture 0.1.0/0.2.0 and the forecast worker write) ----------


def cap(moment: datetime, event: str, vp: str | None, **fields: Any) -> dict[str, Any]:
    return {
        "t": z(moment),
        "worker": "capture",
        "viewpoint_id": vp,
        "event": event,
        "runner": "cli-loop",
        "capture_version": V1,
        **fields,
    }


def poll(moment: datetime, vp: str, *, elapsed_ms: float = 100.0, **fields: Any) -> dict[str, Any]:
    stage = "head" if vp == RAINIER else "json"
    return cap(
        moment,
        "request",
        vp,
        stage=stage,
        method="GET",
        status=200,
        elapsed_ms=elapsed_ms,
        **fields,
    )


def get(moment: datetime, vp: str, *, status: int = 200, **fields: Any) -> dict[str, Any]:
    return cap(
        moment,
        "request",
        vp,
        stage="image",
        method="GET",
        status=status,
        elapsed_ms=200.0,
        **fields,
    )


def pass_(moment: datetime, *, duration: float = 10.0, **fields: Any) -> dict[str, Any]:
    return cap(moment, "pass", None, cameras=8, counts={}, duration_s=duration, **fields)


def run_record(
    moment: datetime,
    run: datetime,
    *,
    fetch_started: datetime | None = None,
    lags: Mapping[int, float] | None = None,
    fields: list[str] | None = None,
    live: bool = True,
    viewpoints: Iterable[str] = (ACADIA,),
) -> dict[str, Any]:
    """A ``run`` record written at ``moment``, which is also its fetch's finish."""
    run_id = run.strftime("%Y%m%d%H")
    return {
        "t": z(moment),
        "event": "run",
        "source": "noaa-hrrr-bdp-pds",
        "run": run_id,
        "live": live,
        "run_complete": False,
        "hours": [0, 1, 2],
        "fields": fields or FIELDS7,
        "viewpoints": list(viewpoints),
        "written": [f"forecast/hrrr/{vp}/{run_id}z.json" for vp in viewpoints],
        "skipped": {},
        "s3_lags_s": {str(k): v for k, v in (lags or {0: 3000.0, 1: 3100.0, 2: 3300.0}).items()},
        "fetch_started_utc": z(fetch_started or moment - timedelta(seconds=3)),
        "fetch_finished_utc": z(moment),
    }


def lag_record(moment: datetime, run: datetime, fh: int, lag_s: float = 3000.0) -> dict[str, Any]:
    return {
        "t": z(moment),
        "event": "lag",
        "run": run.strftime("%Y%m%d%H"),
        "fh": fh,
        "lag_s": lag_s,
    }


class Data:
    """A synthetic ``$LOOKOUT_DATA_DIR``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def log(self, folder: str, records: Iterable[Mapping[str, Any]]) -> None:
        by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for record in records:
            by_day[str(record["t"])[:10]].append(record)
        for day, rows in by_day.items():
            path = self.root / folder / f"{day}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")

    def capture(self, *records: Mapping[str, Any]) -> None:
        self.log("capture_log", records)

    def forecast(self, *records: Mapping[str, Any]) -> None:
        self.log("forecast_log", records)

    def json(self, key: str, doc: Any) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")

    def file(self, key: str, data: bytes = b"\xff\xd8 not a real jpeg") -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def frame(self, key: str, fetched: datetime, policy: str | None = None) -> None:
        """A frame or thumbnail file with its sidecar (only the fields the report reads)."""
        self.file(key)
        doc: dict[str, Any] = {"fetched_at_utc": z(fetched)}
        if policy is not None:
            doc["frame_policy"] = policy
        self.json(key.removesuffix(".jpg") + ".json", doc)


def report(data: Data, hours: float = 2.0, **kwargs: Any) -> dict[str, Any]:
    """The report for ``[T0, T0 + hours)``, made at its end unless ``now`` says otherwise."""
    kwargs.setdefault("now", at(hours * 3600))
    return build_report(data.root, since=T0, until=at(hours * 3600), **kwargs)


def passes_every(start_s: float, end_s: float, step: float = 300.0, **fields: Any) -> list[dict]:
    out, s = [], start_s
    while s < end_s:
        out.append(pass_(at(s), **fields))
        s += step
    return out


def cycles(first: datetime, last: datetime) -> list[datetime]:
    """The hourly cycles from ``first`` to ``last``, both included."""
    out, cycle = [], first
    while cycle <= last:
        out.append(cycle)
        cycle += timedelta(hours=1)
    return out


def hourly_runs(
    data: Data, cycle_list: Iterable[datetime], *, late: Mapping[datetime, float] | None = None
) -> None:
    """Runs fetchable at cycle + 55 min, their fetch starting 30 s later (or ``late[cycle]`` s)
    and finishing 3 s after it starts."""
    late = late or {}
    records = []
    for cycle in cycle_list:
        due = cycle + timedelta(seconds=3300)
        fetched = due + timedelta(seconds=late.get(cycle, 30.0))
        records.append(
            run_record(
                fetched + timedelta(seconds=3),
                cycle,
                fetch_started=fetched,
                lags={0: 3000.0, 1: 3200.0, 2: 3300.0},
            )
        )
    data.forecast(*sorted(records, key=lambda r: r["t"]))


def healthy_forecast(data: Data, until: datetime) -> None:
    """A stand-in writing every run on time, from the scan's first cycle to ``until``."""
    hourly_runs(data, [c for c in cycles(SCAN_FROM, until) if c + timedelta(seconds=3333) < until])


def exclusion(rep: Mapping[str, Any], log: str, reason_start: str) -> dict[str, Any]:
    found = [
        row
        for row in rep["exclusions"]
        if row["log"] == log and row["reason"].startswith(reason_start)
    ]
    assert found, f"no {log!r} exclusion starting {reason_start!r}: {rep['exclusions']}"
    return found[0]


# --- helpers ------------------------------------------------------------------------------------


def test_interval_helpers() -> None:
    a, b, c, d = at(0), at(100), at(50), at(300)
    assert merge([(c, d), (a, b), (d, at(400)), (at(500), at(500))]) == [(a, at(400))]
    window = Window(at(10), at(350))
    assert clip([(a, b), (d, at(400)), (at(360), at(370))], window) == [(at(10), b), (d, at(350))]
    assert measure([(a, b), (c, d)]) == 300.0
    # Nothing lost: the close is start + required; a loss before it moves it by its length.
    assert close_time(a, [], 1000.0) == at(1000)
    assert close_time(a, [(at(200), at(500))], 1000.0) == at(1300)
    assert close_time(a, [(at(2000), at(3000))], 1000.0) == at(1000)  # met before the loss
    assert close_time(a, [(at(-50), at(40))], 1000.0) == at(1040)
    assert close_time(a, [(at(-100), at(-50))], 1000.0) == at(1000)  # wholly before the start


def test_rolling_hour_is_half_open() -> None:
    gets = [at(0), at(900), at(1800), at(2700)]
    assert max_in_window([*gets, at(3599.999)], 3600.0) == (5, at(0))
    assert max_in_window([*gets, at(3600)], 3600.0) == (4, at(0))  # 3600 s later: a new hour
    assert max_in_window([], 3600.0) == (0, None)


def test_max_in_window_matches_brute_force() -> None:
    rng = random.Random(20261120)
    for _ in range(300):
        # Whole 5-minute steps make equal times and exact window edges common.
        times = [at(300 * rng.randint(0, 48)) for _ in range(rng.randint(0, 14))]
        times += [at(rng.uniform(0, 14400)) for _ in range(rng.randint(0, 4))]
        width = rng.choice([600.0, 1800.0, 3600.0])
        span = timedelta(seconds=width)
        brute = max((sum(1 for u in times if s <= u < s + span) for s in times), default=0)
        assert max_in_window(times, width)[0] == brute


def test_close_time_matches_a_sweep() -> None:
    rng = random.Random(7)
    for _ in range(200):
        lost = []
        for _ in range(rng.randint(0, 5)):
            start = rng.randint(-600, 6000)
            lost.append((start, start + rng.randint(1, 1500)))
        required = rng.randint(1, 5000)
        lost_seconds = {s for a, b in lost for s in range(max(a, 0), b)}
        moment, covered = 0, 0
        while covered < required:  # one second at a time, from the start
            covered += moment not in lost_seconds
            moment += 1
        intervals = [(at(a), at(b)) for a, b in lost]
        assert close_time(at(0), intervals, float(required)) == at(moment), (lost, required)


def test_without_home_rewrites_every_string() -> None:
    home = Path("/Users/someone")
    doc = {
        "data_dir": "/Users/someone/lookout-data",
        "lock": {"command": "loop --archive-queue /Users/someone/lookout-data/q.json"},
        "list": ["/Users/someone", "/Users/someoneelse/x", 3, None],
    }
    assert without_home(doc, home) == {
        "data_dir": "~/lookout-data",
        "lock": {"command": "loop --archive-queue ~/lookout-data/q.json"},
        "list": ["~", "/Users/someoneelse/x", 3, None],
    }


# --- capture ------------------------------------------------------------------------------------


def test_counts_per_viewpoint_and_what_is_excluded(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(
        {"vp": RAINIER, "event": "stored", "t": z(at(-3600))},  # the retired stopgap's shape
        {"t": z(at(60)), "worker": "archive", "event": "archive_request", "status": 200},
        cap(at(70), "request", ACADIA, stage="json", status=200, runner="cli-once"),
        *passes_every(20, 7200),
        poll(at(30), ACADIA),
        get(at(31), ACADIA),
        cap(at(32), "stored", ACADIA, key="frames/acadia.mcfarland/2026-11-19/a_1.jpg"),
        poll(at(330), ACADIA),
        cap(at(331), "unchanged", ACADIA, reason="imagedate_same"),
        poll(at(630), ACADIA),
        cap(at(631), "rate_capped", ACADIA, reason="hourly_cap"),
        cap(at(930), "request", ACADIA, stage="json", status=None, error="ConnectTimeout"),
        cap(at(931), "error", ACADIA, stage="json"),
        cap(at(932), "backoff", ACADIA, stage="json", seconds=300.0),
        poll(at(1230), RAINIER),
        get(at(1231), RAINIER, status=404),
        cap(at(1232), "error", RAINIER, stage="image", status=404),
        poll(at(7300), ACADIA),  # after the window
    )
    rep = report(data)
    acadia = rep["capture"]["viewpoints"][ACADIA]
    assert acadia["polls"] == 4  # the failed poll counts as a poll
    assert acadia["image_gets"] == 1 and acadia["downloads_stored"] == 1
    assert acadia["frames"]["full_size"] == 1 and acadia["frames"]["thumbnails"] == 0
    assert acadia["frames"]["by_policy"][0]["policy"] == "legacy-every"
    assert acadia["rate_capped"] == {"total": 1, "by_reason": {"hourly_cap": 1}}
    assert acadia["errors"] == {
        "error_records": 1,
        "by_stage": {"json": 1},
        "failed_requests": 1,
        "backoffs": 1,
    }
    assert acadia["unchanged"] == {"imagedate_same": 1}
    rainier = rep["capture"]["viewpoints"][RAINIER]
    assert rainier["polls"] == 1 and rainier["image_gets"] == 1
    assert rainier["image_get_status"] == {"404": 1} and rainier["errors"]["failed_requests"] == 1
    assert rep["capture"]["passes"]["count"] == 24
    assert exclusion(rep, "capture_log", "before the window")["count"] == 1
    assert exclusion(rep, "capture_log", "after the window")["count"] == 1
    assert exclusion(rep, "capture_log", "archive event")["events"] == {"archive_request": 1}
    once = exclusion(rep, "capture_log", "runner 'cli-once'")
    assert once["count"] == 1 and once["refs"][0]["ref"] == "capture_log/2026-11-19.jsonl:3"
    assert rep["capture"]["outages"] == [] and rep["capture"]["restarts"] == []
    plan = rep["clock"]["plan_md_capture_only"]
    assert plan["lost_h"] == 0.0 and plan["covered_h"] == 2.0
    # The gate rule counts the forecast job too, and this data dir has no forecast record.
    assert rep["clock"]["lost_h"] == 2.0 and rep["clock"]["by_job"]["forecast"]["lost_h"] == 2.0


def test_poll_gaps_are_measured_on_send_times(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    # Records 300.5 s apart, but the second waited 1 s for its answer: sent 299.5 s apart.
    data.capture(
        *passes_every(20, 7200),
        poll(at(100), ACADIA, elapsed_ms=0.0),
        poll(at(400.5), ACADIA, elapsed_ms=1000.0),
        poll(at(1300.5), ACADIA, elapsed_ms=0.0),
    )
    gaps = report(data)["capture"]["viewpoints"][ACADIA]["poll_gap_s"]
    assert gaps["shortest_on_send_times"] == {
        "s": 299.5,
        "from_utc": z(at(100)),
        "to_utc": z(at(399.5)),
    }
    assert gaps["longest"]["s"] == 901.0


def test_poll_gaps_use_sent_at_where_capture_wrote_it(tmp_path: Path) -> None:
    """Capture 0.2.1 writes ``sent_at``, the send itself; ``t`` minus ``elapsed_ms`` is only
    within ~1 ms of it. Here the two readings disagree on purpose (t − elapsed_ms would give
    a 299.6 s first gap): the report must take ``sent_at``, and fall back to
    ``t − elapsed_ms`` on a record without it (capture 0.2.0)."""
    data = Data(tmp_path / "d")
    data.capture(
        *passes_every(20, 7200),
        poll(at(100.4), ACADIA, elapsed_ms=100.0, sent_at=z(at(100))),
        poll(at(400.0), ACADIA, elapsed_ms=100.0, sent_at=z(at(400.002))),
        poll(at(700.11), ACADIA, elapsed_ms=100.0, sent_at=z(at(700.01))),
        poll(at(1000.12), ACADIA, elapsed_ms=100.0),  # no sent_at: sent at 1000.02
    )
    gaps = report(data)["capture"]["viewpoints"][ACADIA]["poll_gap_s"]
    assert gaps["shortest_on_send_times"] == {
        "s": 300.002,
        "from_utc": z(at(100)),
        "to_utc": z(at(400.002)),
    }
    assert gaps["longest"]["s"] == 300.01


def test_rolling_hour_count_in_the_report(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    gets = [get(at(s), REYES) for s in (100, 1000, 1900, 2800, 3690, 4600)]
    data.capture(*passes_every(20, 7200), *gets)
    most = report(data)["capture"]["viewpoints"][REYES]["max_image_gets_in_rolling_hour"]
    # Send times are t - 200 ms: 99.8 .. 3689.8 s holds 5 GETs within 3600 s.
    assert most["count"] == 5 and most["within_rule"] is False
    assert most["window_from_utc"] == z(at(100) - timedelta(milliseconds=200))


def test_provable_spans_and_floor_ms() -> None:
    a, b, c = (Request(at(s), at(s), at(s + 0.5)) for s in (0.0, 300.0, 600.2))
    assert provable_spans([c, a, b], 2) == [(299.5, a, b), (299.7, b, c)]  # in send order
    assert provable_spans([c, a, b], 3) == [(599.7, a, c)]
    assert provable_spans([a, b], 3) == [] and provable_spans([], 2) == []
    with pytest.raises(ValueError, match="at least 2"):
        provable_spans([a, b], 1)
    # A request whose send cannot be placed (earliest None) proves nothing as a run's last.
    d = Request(at(900.0), None, at(900.5))
    assert provable_spans([a, b, c, d], 2)[-1] == (None, c, d)
    assert provable_spans([d, a], 2) == [(None, a, d)]  # still ordered by the scheduler's send
    # Rounded down, so a bound never shows above its value (299.9996 s is not 300.000 s), and
    # 1 us short of 300 s is under it (the allowance absorbs float error, not microseconds).
    assert floor_ms(299.9996) == 299.999 and floor_ms(299.59) == 299.59 and floor_ms(300.0) == 300.0
    assert floor_ms(299.999999) == 299.999


def test_poll_gaps_at_nps_run_from_the_previous_answer(tmp_path: Path) -> None:
    """The lead's rule (20 Nov): nps.gov saw two polls at least earliest(N+1) - latest(N)
    apart. Review finding: the stamps are milliseconds, so the bounds widen by what the stamp
    can hide. latest = answered_at (rounded up by capture), else t + 1 ms (t is written after
    the answer but truncated); earliest = sent_at (truncated, before the send), else
    t - elapsed_ms - 0.05 ms (elapsed_ms is rounded to 0.1 ms). Exactly 300 s is not under."""
    data = Data(tmp_path / "d")
    data.capture(
        *passes_every(20, 7200),
        poll(at(100.0), ACADIA, elapsed_ms=50.0),  # latest 100.001 (t + 1 ms)
        # earliest 400.0 (sent_at): 299.999 s, under (it was 300.0 s on t alone)
        poll(at(401.0), ACADIA, sent_at=z(at(400.0)), answered_at=z(at(400.9))),
        poll(at(701.3), ACADIA, sent_at=z(at(700.9)), answered_at=z(at(701.2))),  # 300.0, not
        # earliest 1002.0 - 0.8 - 0.00005 = 1001.19995: 299.99995 s after 701.2, under
        poll(at(1002.0), ACADIA, elapsed_ms=800.0),
        poll(at(1302.5), ACADIA, elapsed_ms=0.0),  # earliest 1302.49995: 300.499 s after 1002.001
    )
    row = report(data)["capture"]["viewpoints"][ACADIA]
    assert row["poll_gap_at_nps"] == {
        "measured": 4,
        "shortest": {
            "s": 299.999,
            "first_answer_bound_utc": z(at(100.001)),
            "last_send_bound_utc": z(at(400.0)),
        },
        "unproven": 2,
        "under_limit": 2,
        "unbounded": 0,
        "limit_s": 300.0,
    }
    # Send to send (the scheduler's sends: sent_at, else t - elapsed_ms), never under 300.05 s.
    assert row["poll_gap_s"]["shortest_on_send_times"]["s"] == 300.05


def test_a_request_without_sent_at_or_elapsed_ms_proves_nothing(tmp_path: Path) -> None:
    """Review finding: a poll logged with no sent_at and no elapsed_ms (here a 30 s
    ReadTimeout) was placed at its t, after its answer, so its gap at nps.gov came out 329.9 s
    though nps.gov can have seen it 299.9 s after the previous answer. Such a pair is now
    unbounded, and unproven; the send-to-send column still places it at t."""
    data = Data(tmp_path / "d")
    timeout = cap(
        at(430.0), "request", ACADIA, stage="json", method="GET", status=None, error="ReadTimeout"
    )  # sent at 400.0 in truth, 299.9 s after the first poll's answer (100.1)
    data.capture(
        *passes_every(20, 7200),
        poll(at(100.1), ACADIA, elapsed_ms=100.0),
        timeout,
        poll(at(800.1), ACADIA, elapsed_ms=100.0),  # earliest 799.99995: 369.999 s after 430.001
        poll(at(1200.0), ACADIA, elapsed_ms=-1.0),  # not a duration: no bound either
        poll(at(1600.0), ACADIA, elapsed_ms=True),  # a bool is not a number
        poll(at(2000.0), ACADIA, elapsed_ms="100.0"),
    )
    rep = report(data)
    row = rep["capture"]["viewpoints"][ACADIA]
    nps = row["poll_gap_at_nps"]
    assert (nps["measured"], nps["unproven"], nps["under_limit"], nps["unbounded"]) == (5, 4, 0, 4)
    # The one pair with a bound runs from the timeout's answer (t + 1 ms) to the next send.
    assert nps["shortest"] == {
        "s": 369.998,
        "first_answer_bound_utc": z(at(430.001)),
        "last_send_bound_utc": z(at(799.99995)),
    }
    assert row["poll_gap_s"]["shortest_on_send_times"]["s"] == 330.0  # the timeout placed at t
    text = format_report(rep)
    assert f"{ACADIA:<28}{5:>11}{'369.998 s':>13}{4:>10}" in text
    assert f"  {ACADIA}: 4 poll pairs and 0 GET windows are unproven because a request's send" in (
        text
    )


def test_image_get_spans_at_nps_cover_five_consecutive_gets(tmp_path: Path) -> None:
    """Send times keep 5 GETs 3600.05 s apart, but the first took 0.2 s to answer, so nps.gov
    may have seen all 5 within 3599.849 s (the bounds widened for the millisecond stamps):
    within the rule on send times, not provably at nps.gov. A span of exactly 3600 s is not
    under the rolling hour."""
    data = Data(tmp_path / "d")
    sends = [100.0, 1000.0, 1900.0, 2800.0, 3700.05]  # every GET answers 0.2 s later
    data.capture(
        *passes_every(20, 7200),
        *(get(at(s + 0.2), REYES) for s in sends),
        get(at(4600.4), REYES, sent_at=z(at(4600.201))),  # 3600.0 s after 1000.201
    )
    row = report(data)["capture"]["viewpoints"][REYES]
    assert row["max_image_gets_in_rolling_hour"]["count"] == 4
    assert row["max_image_gets_in_rolling_hour"]["within_rule"] is True
    assert row["image_gets_at_nps"] == {
        "measured": 2,
        "shortest": {  # 3700.05 - 0.00005 - (100.2 + 0.001) = 3599.84895, rounded down
            "s": 3599.848,
            "first_answer_bound_utc": z(at(100.201)),
            "last_send_bound_utc": z(at(3700.04995)),
        },
        "unproven": 1,  # the second window, 4600.201 - 1000.201 = 3600.0 s, is not under
        "under_limit": 1,
        "unbounded": 0,
        "limit_s": 3600.0,
    }
    rep = report(data)
    bases = rep["rules"]["bases"]
    assert bases["image_gets_at_nps"].startswith("earliest receipt of the 5th - latest receipt")
    assert "else t + 1 ms" in bases["latest_receipt"]
    assert "else t - elapsed_ms - 0.05 ms" in bases["earliest_receipt"]
    text = format_report(rep)
    assert "Polite collection at nps.gov (PRD §15.3)" in text
    assert f"{REYES:<28}{0:>11}{'-':>13}{0:>10}{2:>16}{'3599.848 s':>14}{1:>10}" in text
    assert "least gap = the next poll's earliest receipt - the previous poll's latest" in text
    assert "unproven: not provably 300 s (3600 s for 5 GETs) apart" in text
    assert "longest gap and min send gap are send to send" in text


def test_capture_outages_extend_the_clock(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    # Passes every 300 s, a 2 h hole (the Mac asleep), passes again, then nothing for 1000 s.
    data.capture(*passes_every(10, 3611), *passes_every(10810, 13211))
    until_s = 13210 + 1000
    healthy_forecast(data, at(until_s))
    rep = report(data, hours=until_s / 3600, options=ReportOptions(clock_hours=72.0))
    rows = rep["capture"]["outages"]
    assert [(r["kind"], r["gap_s"], r["lost_s"], r["ongoing"]) for r in rows] == [
        ("gap between passes", 7200.0, 6900.0, False),
        ("no pass since", 1000.0, 700.0, True),
    ]
    assert rows[0]["start_utc"] == z(at(3610)) and rows[0]["end_utc"] == z(at(10810))
    assert rows[0]["missed_passes_est"] == 23
    clock = rep["clock"]
    lost = 6900.0 + 700.0
    assert clock["lost_h"] == round(lost / 3600, 3)
    assert clock["covered_h"] == round((until_s - lost) / 3600, 3)
    assert clock["met"] is False
    assert clock["earliest_close_utc"] == z(at(72 * 3600 + lost))
    assert clock["rule"].startswith("gate rule (the lead's decision of 20 Nov, settling D-061")
    assert clock["by_job"]["capture"]["lost_h"] == clock["lost_h"]
    # A stand-in that wrote every run on time adds nothing: the gate rule is PLAN.md's figure.
    assert clock["by_job"]["forecast"]["lost_h"] == 0.0 and rep["forecast"]["outages"] == []
    plan = clock["plan_md_capture_only"]
    assert plan["lost_h"] == clock["lost_h"] and plan["earliest_close_utc"] == z(
        at(72 * 3600 + lost)
    )
    assert plan["rule"].startswith("PLAN.md's capture-only figure")
    assert clock["parts_lost_h"] == {
        "capture_outages": round(lost / 3600, 3),
        "capture_restart_excess": 0.0,
        "forecast_outages": 0.0,
    }
    assert exclusion(rep, "outages", "capture outage")["count"] == 2


def test_a_capture_gap_straddling_since_loses_only_its_part_inside(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    # The last pass before the window at -1000 s, the next at 500 s: 1500 s apart.
    data.capture(*passes_every(-4000, -999), *passes_every(500, 3600))
    rows = report(data, hours=1.0)["capture"]["outages"]
    assert [(r["start_utc"], r["end_utc"], r["gap_s"], r["lost_s"]) for r in rows] == [
        (z(at(-1000)), z(at(500)), 1500.0, 500.0)  # lost from -700 s, clipped to the window
    ]


def test_the_clock_is_met_once_enough_is_covered(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(*passes_every(10, 3611), *passes_every(5410, 14400))
    healthy_forecast(data, at(14400))
    rep = report(data, hours=4.0, options=ReportOptions(clock_hours=2.0))
    # Lost: 3610 + 300 .. 5410 = 1500 s, so 2 h are covered at 7200 + 1500 s.
    assert rep["clock"]["met"] is True
    assert rep["clock"]["met_at_utc"] == z(at(8700))
    assert rep["clock"]["remaining_h"] == 0.0


def test_no_scheduled_pass_at_all_is_one_ongoing_outage(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(cap(at(100), "pass", None, duration_s=1.0, runner="cli-once"))
    rep = report(data, hours=1.0)
    assert [(r["kind"], r["lost_s"], r["ongoing"]) for r in rep["capture"]["outages"]] == [
        ("no scheduled pass in the logs", 3600.0, True)
    ]
    assert rep["clock"]["covered_h"] == 0.0


def test_a_loop_that_stopped_before_the_window_is_down_all_window(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(*(pass_(at(-3000 + 300 * k)) for k in range(5)))  # last pass at -1800 s
    rep = report(data, hours=1.0)
    assert [
        (r["kind"], r["start_utc"], r["lost_s"], r["ongoing"]) for r in rep["capture"]["outages"]
    ] == [("no pass since", z(at(-1800)), 3600.0, True)]
    assert rep["capture"]["passes"]["count"] == 0 and rep["clock"]["covered_h"] == 0.0


def test_restarts_from_version_spacing_and_loop_stopped(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    old = passes_every(10, 1211)  # 10 .. 1210 s, 0.1.0
    data.capture(
        *old,
        cap(at(1300), "unchanged", ACADIA, reason="imagedate_same"),  # the old loop's last record
        pass_(at(1360.0), duration=0.01, capture_version=V2),  # new process: starts 150 s later
        *passes_every(1670, 3000, capture_version=V2),
        cap(at(3050), "loop_stopped", None, capture_version=V2, error="ForeignFetcherError: x"),
        pass_(at(3200), duration=1.0, capture_version=V2),
        *passes_every(3510, 7200, capture_version=V2),
    )
    rows = report(data)["capture"]["restarts"]
    assert len(rows) == 2
    first, second = rows
    assert first["stopped_after_utc"] == z(at(1300)) and first["from_version"] == V1
    assert first["started_utc"] == z(at(1360) - timedelta(seconds=0.01))
    assert first["to_version"] == V2 and first["an_outage"] is False
    assert any("capture_version" in e for e in first["evidence"])
    assert any("under the 300 s interval" in e for e in first["evidence"])
    assert any(e.startswith("loop_stopped at") for e in second["evidence"])


def test_a_loop_stopped_record_without_a_version_change_is_a_restart(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(
        *passes_every(10, 1811),
        cap(at(1900), "loop_stopped", None, error="KeyboardInterrupt"),
        *passes_every(3010, 7200),  # the same version, 1200 s after the last old pass
    )
    rep = report(data)
    rows = rep["capture"]["restarts"]
    assert len(rows) == 1 and rows[0]["from_version"] == rows[0]["to_version"] == V1
    assert rows[0]["stopped_after_utc"] == z(at(1900)) and rows[0]["an_outage"] is True
    assert rows[0]["evidence"] == [f"loop_stopped at {z(at(1900))}: KeyboardInterrupt"]
    assert [(r["gap_s"], r["lost_s"]) for r in rep["capture"]["outages"]] == [(1200.0, 900.0)]


def _restart_like_20_nov(data: Data, *, until_s: float = 7200.0) -> None:
    """The 02:39 restart's shape (D-049): the old loop polls 3 cameras every 300 s, the last
    pass starting at 1500 s; the new loop's first pass, at 1660 s, finds every camera not yet
    due; its next pass, at 1985 s, polls them 485.0, 486.0 and 486.5 s after their last polls
    (send to send: every poll's send is its t - 100 ms)."""
    records = []
    for s in range(0, 1501, 300):  # the old loop, capture 0.1.0
        records += [poll(at(s + 1.0), ACADIA), poll(at(s + 2.0), RAINIER), poll(at(s + 3.0), REYES)]
        records.append(pass_(at(s + 10.0)))
    records.append(pass_(at(1660.01), duration=0.01, capture_version=V2))  # polls nothing
    s = 1985.0
    while s + 10.0 < until_s:  # the new loop, capture 0.2.0, every 325 s
        records += [
            poll(at(s + 1.0), ACADIA, capture_version=V2),
            poll(at(s + 3.0), RAINIER, capture_version=V2),
            poll(at(s + 4.5), REYES, capture_version=V2),
            pass_(at(s + 10.0), capture_version=V2),
        ]
        s += 325.0
    data.capture(*sorted(records, key=lambda r: r["t"]))


def test_a_restart_s_excess_is_the_longest_camera_gap_less_one_interval(tmp_path: Path) -> None:
    """The lead's gate rule (20 Nov, settling D-061): a restart loses the longest any camera
    went from its last poll before it to its first after, less one 300 s interval. Passes
    150 s apart are no outage, so PLAN.md's capture-only figure loses nothing."""
    data = Data(tmp_path / "d")
    _restart_like_20_nov(data)
    healthy_forecast(data, at(7200))
    rep = report(data)
    (row,) = rep["capture"]["restarts"]
    assert row["an_outage"] is False and row["pass_gap_s"] == 150.01
    assert row["poll_gaps_s"] == {ACADIA: 485.0, RAINIER: 486.0, REYES: 486.5}
    assert row["longest_poll_gap"] == {
        "viewpoint_id": REYES,
        "s": 486.5,
        "from_utc": z(at(1502.9)),
        "to_utc": z(at(1989.4)),
        "ongoing": False,
    }
    assert row["excess_s"] == 186.5 and row["lost_s"] == 186.5
    assert rep["capture"]["outages"] == []
    clock = rep["clock"]
    excess_h = round(186.5 / 3600, 3)
    assert clock["lost_h"] == excess_h and clock["by_job"]["capture"]["lost_h"] == excess_h
    assert clock["parts_lost_h"]["capture_restart_excess"] == excess_h
    assert clock["earliest_close_utc"] == z(at(72 * 3600 + 186.5))
    assert clock["plan_md_capture_only"]["lost_h"] == 0.0
    assert clock["plan_md_capture_only"]["earliest_close_utc"] == z(at(72 * 3600))
    excess = exclusion(rep, "outages", "capture restart excess")
    assert excess["refs"] == [
        {"start": z(at(1502.9)), "end": z(at(1989.4)), "lost_s": 186.5, "viewpoint_id": REYES}
    ]
    text = format_report(rep)
    assert f"restart excess ({REYES}'s polls)" in text
    assert "excess 186.500 s (polls 485.000-486.500 s apart across it, longest" in text


def test_a_restart_that_is_also_an_outage_is_counted_once(tmp_path: Path) -> None:
    """Passes 1200 s apart across a stop: the outage loses 1810+300 .. 3010 s; the restart's
    excess, from the camera's polls (1801 .. 3001.9 s), overlaps it and adds only its edge."""
    data = Data(tmp_path / "d")
    records = []
    for s in range(1800 - 300 * 6, 1801, 300):
        records += [poll(at(s + 1.1), ACADIA), pass_(at(s + 10.0))]
    records.append(cap(at(1900), "loop_stopped", None, error="KeyboardInterrupt"))
    for s in range(3000, 7200, 300):
        records += [poll(at(s + 2.0), ACADIA), pass_(at(s + 10.0))]
    data.capture(*sorted(records, key=lambda r: r["t"]))
    healthy_forecast(data, at(7200))
    rep = report(data)
    (row,) = rep["capture"]["restarts"]
    assert row["an_outage"] is True and row["excess_s"] == 900.9  # 3001.9 - 1801.0 - 300
    assert [(r["gap_s"], r["lost_s"]) for r in rep["capture"]["outages"]] == [(1200.0, 900.0)]
    # Outage 2110 .. 3010 s, excess 2101 .. 3001.9 s: their union is 2101 .. 3010 s.
    assert rep["clock"]["lost_h"] == round(909.0 / 3600, 3)
    assert rep["clock"]["plan_md_capture_only"]["lost_h"] == round(900.0 / 3600, 3)
    parts = rep["clock"]["parts_lost_h"]
    assert parts["capture_outages"] == 0.25 and parts["capture_restart_excess"] == 0.25


def test_a_restart_before_since_counts_its_excess_inside_the_window(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    _restart_like_20_nov(data)
    healthy_forecast(data, at(7200))
    since = at(1700)  # after the new loop's first pass, before the excess (1802.9 .. 1989.4 s)
    rep = build_report(data.root, since=since, until=at(7200), now=at(7200))
    (row,) = rep["capture"]["restarts"]
    assert row["started_utc"] == z(at(1660)) and row["lost_s"] == 186.5
    assert rep["clock"]["parts_lost_h"]["capture_restart_excess"] == round(186.5 / 3600, 3)
    # From 1900 s the excess is only partly inside: 1900 .. 1989.4 s.
    later = build_report(data.root, since=at(1900), until=at(7200), now=at(7200))
    assert later["capture"]["restarts"][0]["lost_s"] == 89.4
    # From 2000 s it loses nothing inside the window, so it is not listed.
    assert (
        build_report(data.root, since=at(2000), until=at(7200), now=at(7200))["capture"]["restarts"]
        == []
    )


def test_a_loop_stopped_record_before_since_still_marks_the_restart(tmp_path: Path) -> None:
    """The only sign is a ``loop_stopped`` record before the window: the same version, pass
    starts 300 s apart. The new loop's first pass (1800 s) polls nothing; Acadia's next poll
    comes 625 s after its last, so the excess runs 1800.9 .. 2125.9 s, 225.9 s of it inside a
    window from 1900 s."""
    data = Data(tmp_path / "d")
    records = []
    for s in range(0, 1501, 300):
        records += [poll(at(s + 1.0), ACADIA), pass_(at(s + 10.0))]
    records.append(cap(at(1520), "loop_stopped", None, error="SIGTERM"))
    records.append(pass_(at(1800.01), duration=0.01))
    for s in range(2125, 7200, 325):
        records += [poll(at(s + 1.0), ACADIA), pass_(at(s + 10.0))]
    data.capture(*sorted(records, key=lambda r: r["t"]))
    rep = build_report(data.root, since=at(1900), until=at(7200), now=at(7200))
    (row,) = rep["capture"]["restarts"]
    assert row["evidence"] == [f"loop_stopped at {z(at(1520))}: SIGTERM"]
    assert row["stopped_after_utc"] == z(at(1520)) and row["an_outage"] is False
    assert (row["excess_s"], row["lost_s"]) == (325.0, 225.9)


def test_a_restart_with_no_poll_after_it_yet_is_measured_to_the_window_end(
    tmp_path: Path,
) -> None:
    data = Data(tmp_path / "d")
    _restart_like_20_nov(data, until_s=1900.0)  # the logs end before the new loop polls
    # The window ends at 1900 s: the new loop has not polled yet (its next pass is at 1985 s).
    rep = build_report(data.root, since=T0, until=at(1900), now=at(1900))
    (row,) = rep["capture"]["restarts"]
    gap = row["longest_poll_gap"]
    assert gap["viewpoint_id"] == ACADIA and gap["ongoing"] is True  # the oldest last poll
    assert gap["to_utc"] == z(at(1900)) and row["excess_s"] == 99.1  # 1900 - 1500.9 - 300
    assert row["poll_gaps_s"] == {ACADIA: 399.1, RAINIER: 398.1, REYES: 397.1}
    assert row["lost_s"] == row["excess_s"]
    # Read later, the logs hold the next polls: the gap is the real one, and still ongoing at
    # the window's end; only the part inside the window is lost.
    data2 = Data(tmp_path / "e")
    _restart_like_20_nov(data2)
    rep2 = build_report(data2.root, since=T0, until=at(1900), now=at(7200))
    gap2 = rep2["capture"]["restarts"][0]["longest_poll_gap"]
    assert gap2["viewpoint_id"] == REYES and gap2["to_utc"] == z(at(1989.4))
    assert gap2["ongoing"] is True and rep2["capture"]["restarts"][0]["lost_s"] == 97.1


def _old_loop_then(
    data: Data,
    *new: dict[str, Any],
    new_from: float,
    until_s: float = 7200.0,
    new_loop: Mapping[str, Any] | None = None,
) -> None:
    """The old loop (0.1.0, cli-loop) polls Acadia every 300 s, its last pass written at 1510 s
    (its last record; the last poll sent at 1500.9 s); then ``new`` records, then the new loop
    (``new_loop``'s fields, default capture 0.2.0) polling Acadia and passing every 300 s from
    ``new_from``."""
    fields = dict(new_loop or {"capture_version": V2})
    records: list[dict[str, Any]] = []
    for s in range(0, 1501, 300):
        records += [poll(at(s + 1.0), ACADIA), pass_(at(s + 10.0))]
    records += new
    s = new_from
    while s + 10.0 < until_s:
        records += [poll(at(s + 1.0), ACADIA, **fields), pass_(at(s + 10.0), **fields)]
        s += 300.0
    data.capture(*sorted(records, key=lambda r: r["t"]))
    healthy_forecast(data, at(until_s))


def test_a_new_loop_that_polls_at_once_is_split_at_the_old_loop_s_last_record(
    tmp_path: Path,
) -> None:
    """Review finding: the new loop's first pass start is its t less duration_s, rounded to
    0.01 s, so a poll sent 1 ms after the pass began could fall before that start and count as
    the old loop's. Here the pass began at 1660.000 s, polled Acadia at once (sent 1660.001 s)
    and was written at 1660.504 s with duration 0.5 s ("started" 1660.004 s). Split at the old
    loop's last record (1510 s), Acadia went 1500.9 -> 1660.001 s: 159.101 s, no excess."""
    data = Data(tmp_path / "d")
    _old_loop_then(
        data,
        poll(at(1660.101), ACADIA, capture_version=V2),
        pass_(at(1660.504), duration=0.5, capture_version=V2),
        new_from=1960.0,
    )
    rep = report(data)
    (row,) = rep["capture"]["restarts"]
    assert row["started_utc"] == z(at(1660.004)) and row["stopped_after_utc"] == z(at(1510.0))
    assert row["poll_gaps_s"] == {ACADIA: 159.101}
    assert (row["excess_s"], row["lost_s"]) == (0.0, 0.0)
    assert rep["clock"]["parts_lost_h"]["capture_restart_excess"] == 0.0


@pytest.mark.parametrize(
    ("change", "new_loop"),
    [("capture_version", {"capture_version": V2}), ("runner", {"runner": "lambda"})],
)
def test_a_new_loop_that_sleeps_in_its_first_pass_is_split_at_the_old_loop_s_last_record(
    tmp_path: Path, change: str, new_loop: dict[str, Any]
) -> None:
    """Review finding: duration_s is on the monotonic clock, which does not count sleep. The
    new loop polls Acadia at 1700.0 s, the Mac sleeps 900 s, and the pass is written at
    2601.0 s with duration 1.1 s, so it seems to start at 2599.9 s, after that poll. The
    changed version (or runner) tells the loops apart: the restart's gap for Acadia is
    1500.9 -> 1700.0 s (no excess); the sleep is the outage between the passes (lost
    1810 .. 2601 s), not excess on top of it (1810 .. 2900.9 s)."""
    data = Data(tmp_path / "d")
    _old_loop_then(
        data,
        poll(at(1700.1), ACADIA, **new_loop),
        pass_(at(2601.0), duration=1.1, **new_loop),
        new_from=2900.0,
        new_loop=new_loop,
    )
    rep = report(data)
    (row,) = rep["capture"]["restarts"]
    assert row["evidence"][0].startswith(change)
    assert row["poll_gaps_s"] == {ACADIA: 199.1}
    assert (row["excess_s"], row["lost_s"]) == (0.0, 0.0)
    outages = rep["capture"]["outages"]
    assert [(r["start_utc"], r["end_utc"], r["lost_s"]) for r in outages] == [
        (z(at(1510.0)), z(at(2601.0)), 791.0)
    ]
    assert rep["clock"]["lost_h"] == round(791.0 / 3600, 3)


def test_a_restart_after_a_loop_stopped_record_is_split_at_the_stop(tmp_path: Path) -> None:
    """The same version on both sides, so only the loop_stopped record (1520 s, the old loop's
    last words) tells the loops apart; the new loop's first pass slept as above. Split at the
    stop, Acadia went 1500.9 -> 1700.0 s; split at the computed start it would seem to go
    1700.0 -> 2900.9 s."""
    data = Data(tmp_path / "d")
    records: list[dict[str, Any]] = []
    for s in range(0, 1501, 300):
        records += [poll(at(s + 1.0), ACADIA), pass_(at(s + 10.0))]
    records += [
        cap(at(1520.0), "loop_stopped", None, error="ForeignFetcherError: x"),
        poll(at(1700.1), ACADIA),
        pass_(at(2601.0), duration=1.1),
    ]
    for s in range(2900, 7200, 300):
        records += [poll(at(s + 1.0), ACADIA), pass_(at(s + 10.0))]
    data.capture(*sorted(records, key=lambda r: r["t"]))
    healthy_forecast(data, at(7200))
    (row,) = report(data)["capture"]["restarts"]
    assert row["evidence"] == [f"loop_stopped at {z(at(1520.0))}: ForeignFetcherError: x"]
    assert row["stopped_after_utc"] == z(at(1520.0))
    assert row["poll_gaps_s"] == {ACADIA: 199.1} and row["excess_s"] == 0.0


def test_the_old_loop_s_last_poll_is_before_the_restart(tmp_path: Path) -> None:
    """The old loop is killed mid-pass right after polling Acadia (sent 1800.9 s), so that
    poll is its last record; the new loop polls Acadia next at 2250.9 s. A poll written at the
    split itself is the old loop's: the gap is 450.0 s, excess 150.0 s."""
    data = Data(tmp_path / "d")
    _old_loop_then(
        data,
        poll(at(1801.0), ACADIA),
        pass_(at(1950.01), duration=0.01, capture_version=V2),  # polls nothing
        new_from=2250.0,
    )
    (row,) = report(data)["capture"]["restarts"]
    assert row["stopped_after_utc"] == z(at(1801.0)) and row["duration_s"] == 149.0
    assert row["poll_gaps_s"] == {ACADIA: 450.0}
    assert (row["excess_s"], row["lost_s"]) == (150.0, 150.0)


def test_a_cli_once_poll_does_not_measure_a_restart(tmp_path: Path) -> None:
    """Review finding (no test pinned it): only the scheduled runners' polls are the
    continuous record, as everywhere in the report, so a ``lookout-capture once`` poll during
    the restart does not shorten its gap (485.0 s from 1500.9 s, not 199.0 s)."""
    data = Data(tmp_path / "d")
    _old_loop_then(
        data,
        pass_(at(1660.01), duration=0.01, capture_version=V2),
        poll(at(1700.0), ACADIA, runner="cli-once", capture_version=V2),
        new_from=1985.0,
    )
    (row,) = report(data)["capture"]["restarts"]
    assert row["poll_gaps_s"] == {ACADIA: 485.0} and row["excess_s"] == 185.0


def test_a_camera_first_polled_after_a_restart_is_left_out(tmp_path: Path) -> None:
    """Review finding (no test pinned it): a camera the old loop never polled has no gap
    across the restart; only Acadia's counts."""
    data = Data(tmp_path / "d")
    _old_loop_then(
        data,
        pass_(at(1660.01), duration=0.01, capture_version=V2),
        poll(at(1987.0), REYES, capture_version=V2),
        new_from=1985.0,
    )
    (row,) = report(data)["capture"]["restarts"]
    assert row["poll_gaps_s"] == {ACADIA: 485.0}
    assert row["longest_poll_gap"]["viewpoint_id"] == ACADIA


def test_pass_starts_exactly_295_s_apart_are_one_loop(tmp_path: Path) -> None:
    """Pass starts under 295 s apart mark a restart; exactly 295 s is not under."""
    data = Data(tmp_path / "d")
    data.capture(pass_(at(10.0)), pass_(at(305.0)), *passes_every(610, 7200))
    assert report(data)["capture"]["restarts"] == []


def test_one_loop_with_jitter_is_not_a_restart(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    # Pass starts 300-330 s apart, durations 2-20 s: what one loop process does.
    records, s = [], 30.0
    for i in range(20):
        duration = 2.0 + (i % 5) * 4.5
        records.append(pass_(at(s + duration), duration=duration))
        s += 300.0 + (i * 7) % 31
    data.capture(*records)
    assert report(data)["capture"]["restarts"] == []


def test_a_pass_across_a_dark_wake_is_an_outage_not_a_restart(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    # A pass that began at 2110 s, slept, and was written at 3310 s with a 12 s duration that
    # leaves the sleep out, so it seems to start at 3298 s; the next one starts 312 s later.
    data.capture(
        *passes_every(10, 1811),
        pass_(at(3310), duration=12.0),
        *passes_every(3620, 7200),
    )
    rep = report(data)
    assert rep["capture"]["restarts"] == []
    assert [(r["gap_s"], r["lost_s"]) for r in rep["capture"]["outages"]] == [(1500.0, 1200.0)]


def test_storage_policies_and_the_disk_check(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    legacy = "frames/acadia.mcfarland/2026-11-19/20261119T123000Z_aaaaaaaaaaaa.jpg"
    every = "frames/point-reyes.beach/2026-11-19/20261119T130000Z_bbbbbbbbbbbb.jpg"
    every_thumb = every.replace("frames/", "thumbs/", 1)
    hourly = "frames/acadia.mcfarland/2026-11-19/20261119T133000Z_cccccccccccc.jpg"
    hourly_thumb = hourly.replace("frames/", "thumbs/", 1)
    gone = "frames/acadia.mcfarland/2026-11-19/20261119T124500Z_dddddddddddd.jpg"
    stray = "frames/acadia.mcfarland/2026-11-19/20261119T140000Z_eeeeeeeeeeee.jpg"
    data.capture(
        *passes_every(20, 7200),
        cap(at(400), "stored", ACADIA, key=legacy),
        cap(at(500), "stored", ACADIA, key=gone),
        cap(
            at(1800),
            "stored",
            REYES,
            key=every,
            frame_key=every,
            thumb_key=every_thumb,
            frame_policy="every",
            capture_version=V2,
        ),
        cap(
            at(2400),
            "stored",
            ACADIA,
            key=hourly_thumb,
            frame_key=hourly,
            thumb_key=hourly_thumb,
            frame_policy="hourly-nearest",
            capture_version=V2,
        ),
        cap(at(6000), "promoted", ACADIA, key=hourly, thumb_key=hourly_thumb, capture_version=V2),
    )
    data.frame(legacy, at(400))
    data.frame(every, at(1800), "every")
    data.frame(every_thumb, at(1800), "every")
    data.frame(hourly, at(2400), "hourly-nearest")
    data.frame(hourly_thumb, at(2400), "hourly-nearest")
    data.frame(stray, at(3000))  # on disk, fetched inside the window, never logged
    data.frame("frames/acadia.mcfarland/2026-11-19/20261119T100000Z_ffffffffffff.jpg", at(-7200))
    rep = report(data)
    acadia = rep["capture"]["viewpoints"][ACADIA]["frames"]
    assert acadia["full_size"] == 3 and acadia["thumbnails"] == 1 and acadia["promoted"] == 1
    rows = [
        (p["policy"], p["capture_version"], p["downloads"], p["full_size"], p["thumbnails"])
        for p in acadia["by_policy"]
    ]
    assert rows == [
        ("legacy-every", V1, 2, 2, 0),
        ("hourly-nearest", V2, 1, 1, 1),
    ]
    full = acadia["disk"]["full_size"]
    assert (full["logged"], full["logged_present"], full["logged_missing"]) == (3, 2, 1)
    assert full["missing_examples"] == [gone]
    assert full["on_disk_in_window"] == 3 and full["on_disk_not_logged"] == 1
    assert full["not_logged_examples"] == [stray]
    assert full["on_disk_by_policy"] == {"hourly-nearest": 1, "legacy-every": 2}
    reyes = rep["capture"]["viewpoints"][REYES]["frames"]
    assert reyes["full_size"] == 1 and reyes["thumbnails"] == 1
    assert reyes["disk"]["thumbnails"]["logged_present"] == 1
    text = format_report(rep)
    assert "acadia.mcfarland full_size: 1 logged but not on disk, 1 on disk" in text
    assert "legacy 2, hourly 1" in text


def test_a_staged_hourly_frame_is_not_full_size_until_promoted(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    frame = "frames/acadia.mcfarland/2026-11-19/20261119T133000Z_cccccccccccc.jpg"
    thumb = frame.replace("frames/", "thumbs/", 1)
    data.capture(
        *passes_every(20, 7200),
        cap(
            at(2400),
            "stored",
            ACADIA,
            key=thumb,
            frame_key=frame,
            thumb_key=thumb,
            staged_key=f"cache/staging/{frame}",
            frame_policy="hourly-nearest",
            capture_version=V2,
        ),  # its hour is not decided yet: no "promoted" record
    )
    data.frame(thumb, at(2400), "hourly-nearest")
    frames = report(data)["capture"]["viewpoints"][ACADIA]["frames"]
    assert frames["full_size"] == 0 and frames["thumbnails"] == 1 and frames["promoted"] == 0
    assert [(p["downloads"], p["full_size"], p["thumbnails"]) for p in frames["by_policy"]] == [
        (1, 0, 1)
    ]
    full, thumbs = frames["disk"]["full_size"], frames["disk"]["thumbnails"]
    assert (full["logged"], full["logged_missing"], full["on_disk_in_window"]) == (0, 0, 0)
    assert (thumbs["logged"], thumbs["logged_present"], thumbs["logged_missing"]) == (1, 1, 0)
    # Promoted after the window: the frame is on disk now, fetched inside the window, and named
    # only by a later record. It is not full size in this window, and not an unlogged file.
    data.capture(cap(at(7500), "promoted", ACADIA, key=frame, thumb_key=thumb, capture_version=V2))
    data.frame(frame, at(2400), "hourly-nearest")
    rep = report(data, now=at(7600))
    frames = rep["capture"]["viewpoints"][ACADIA]["frames"]
    full = frames["disk"]["full_size"]
    assert frames["full_size"] == 0 and full["on_disk_in_window"] == 1
    assert (full["on_disk_logged_after_window"], full["on_disk_not_logged"]) == (1, 0)
    assert full["logged_after_window_examples"] == [frame]
    text = format_report(rep)
    assert "none unlogged" in text and "logged only after it" in text


def test_state_is_read_as_it_is_now(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(*passes_every(20, 7200), poll(at(100), ACADIA))
    data.json(
        "state/capture/acadia.mcfarland.json",
        {
            "last_poll_at": z(at(7000)),
            "polls": 9,
            "gets": [{"t": z(at(3000))}, {"t": z(at(5000))}, {"t": z(at(6900))}],
            "staged": [{"key": "cache/staging/frames/x.jpg"}],
            "failures": 0,
        },
    )
    data.json("state/locks/nps-fetcher.lock", {"pid": 1, "command": "x", "started_at": z(T0)})
    before = {p: p.stat().st_mtime_ns for p in data.root.rglob("*") if p.is_file()}
    rep = report(data)
    state = rep["capture"]["viewpoints"][ACADIA]["state_now"]
    assert state["read_utc"] == z(at(7200)) and state["last_poll_age_s"] == 200.0
    assert state["polls_total"] == 9 and state["staged_now"] == 1
    assert state["gets_in_hour_before_read"] == 2
    assert rep["capture"]["current_fetcher_lock"] == {"pid": 1, "command": "x", "started_at": z(T0)}
    # A window that ended an hour before the report still shows the state at the reading.
    past = build_report(data.root, since=T0, until=at(3600), now=at(7200))
    assert past["capture"]["viewpoints"][ACADIA]["state_now"] == state
    assert {p: p.stat().st_mtime_ns for p in data.root.rglob("*") if p.is_file()} == before


def test_the_home_directory_is_written_as_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    data = Data(home / "lookout-data")
    data.capture(*passes_every(20, 7200))
    queue = home / "lookout-data" / "state" / "archive" / "queue.json"
    data.json(
        "state/locks/nps-fetcher.lock",
        {"pid": 1, "command": f"lookout-capture loop --archive-queue {queue}", "started_at": "x"},
    )
    monkeypatch.setenv("HOME", str(home))
    rep = report(data)
    assert rep["data_dir"] == "~/lookout-data"
    assert rep["capture"]["current_fetcher_lock"]["command"] == (
        "lookout-capture loop --archive-queue ~/lookout-data/state/archive/queue.json"
    )
    assert str(home) not in json.dumps(rep)
    assert "data dir: ~/lookout-data" in format_report(rep)


# --- forecast -----------------------------------------------------------------------------------


def _forecast_file(
    data: Data, vp: str, run: datetime, fetch_started: datetime, **extra: Any
) -> None:
    run_id = run.strftime("%Y%m%d%H")
    data.json(
        f"forecast/hrrr/{vp}/{run_id}z.json",
        {
            "run_id": run_id,
            "live": True,
            "forecast_hours": [0, 1, 2],
            "field_names": FIELDS7,
            "fetch_started_utc": z(fetch_started),
            "missing_hours": [],
            **extra,
        },
    )


def test_forecast_runs_count_once_and_archived_runs_are_excluded(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    r12 = hour(12)
    r13 = r12 + timedelta(hours=1)
    old = hour(6)
    first, again = at(1800), at(6000)  # 12:54:23 and 14:04:23
    data.capture(*passes_every(20, 7200))
    data.forecast(
        run_record(at(-3600), old, fetch_started=at(-3603)),  # written before the window
        run_record(
            first,
            r12,
            fetch_started=first - timedelta(seconds=3),
            lags={0: 3000.0, 1: 3100.0, 2: 3200.0},
        ),
        lag_record(first, r12, 0),
        run_record(
            at(2000),
            datetime(2024, 1, 14, 12, tzinfo=UTC),
            live=False,
            viewpoints=["grand-canyon.yavapai"],
        ),
        run_record(at(5400), r13, lags={0: 3000.0, 1: 3100.0, 2: 3200.0}),
        run_record(again, r12, fields=FIELDS8, fetch_started=again - timedelta(seconds=3)),
        lag_record(again, r12, 0),  # logged again by the re-extraction
    )
    _forecast_file(data, ACADIA, r12, again - timedelta(seconds=3), field_names=FIELDS8)
    _forecast_file(data, ACADIA, r13, at(5397))
    _forecast_file(data, ACADIA, old, at(-3603))
    data.json(
        "forecast/hrrr/grand-canyon.yavapai/2024011412z.json",
        {"run_id": "2024011412", "live": False},
    )
    data.log(
        "forecast_log/lag_survey",
        [{"t": z(at(4000)), "event": "lag", "run": "2026111800", "fh": 1, "method": "survey"}],
    )
    rep = report(data)
    acadia = rep["forecast"]["viewpoints"][ACADIA]
    assert acadia["runs"] == 2 and acadia["first_run"] == "2026111912"
    assert acadia["last_run"] == "2026111913" and acadia["re_extractions"] == 1
    assert acadia["runs_due_in_window"] == 2 and acadia["catch_ups"] == []
    assert acadia["hours"] == {"0,1,2": 2}
    assert acadia["field_sets_on_disk"] == [
        {"fields": FIELDS8, "runs": 1},
        {"fields": FIELDS7, "runs": 1},
    ]
    assert acadia["field_sets_at_first_write"] == [{"fields": FIELDS7, "runs": 2}]
    assert acadia["files_rewritten_by_later_writes"] == ["2026111912"]
    assert rep["forecast"]["records_counted"] == {"lag": 1, "run": 2}
    archived = exclusion(rep, "forecast_log", "archived forecast run")
    assert archived["count"] == 1 and archived["refs"][0]["run"] == "2024011412"
    assert archived["refs"][0]["ref"] == "forecast_log/2026-11-19.jsonl:4"
    again_row = exclusion(rep, "forecast_log", "re-extraction")
    assert again_row["refs"][0]["fields_added"] == ["PRES:cloud base"]
    assert again_row["refs"][0]["first_written_in_window"] is True
    assert exclusion(rep, "forecast_log", "duplicate lag")["count"] == 1
    assert exclusion(rep, "forecast_log", "before the window")["count"] == 1
    assert exclusion(rep, "forecast_log/lag_survey", "survey lag")["count"] == 1
    not_first = exclusion(rep, "forecast/hrrr", "live forecast file whose run was not first")
    assert not_first["count"] == 1
    assert exclusion(rep, "forecast/hrrr", "archived forecast file")["refs"][0]["key"] == (
        "forecast/hrrr/grand-canyon.yavapai/2024011412z.json"
    )
    changes = rep["forecast"]["settings_changes"]
    assert [(c["fields_added"], c["fields_removed"]) for c in changes] == [
        (["PRES:cloud base"], [])
    ]
    row = {r["run"]: r for r in rep["forecast"]["runs"]}["2026111912"]
    # Fetchable at 12:53:20; the first write finished at 12:54:23 (D-027's availability).
    assert row["writes_in_window"] == 2 and row["fetch_delay_s"] == 63.0
    assert row["first_fetch_finished_utc"] == z(first) and row["catch_up"] is False


def test_a_rewrite_of_a_run_first_written_before_the_window_is_not_a_run(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    r11, r12 = hour(11), hour(12)
    data.capture(*passes_every(20, 3600))
    data.forecast(
        run_record(at(-600), r11),  # 12:14:23, before the window
        run_record(at(1800), r11, fields=FIELDS8),  # re-extracted inside it
        run_record(at(2000), r12),  # first written inside it
    )
    _forecast_file(data, ACADIA, r11, at(1797), field_names=FIELDS8)
    _forecast_file(data, ACADIA, r12, at(1997))
    rep = report(data, hours=1.0)
    forecast = rep["forecast"]
    acadia = forecast["viewpoints"][ACADIA]
    assert acadia["runs"] == 1 and acadia["first_run"] == acadia["last_run"] == "2026111912"
    assert acadia["re_extractions"] == 1
    assert forecast["records_counted"] == {"run": 1}
    assert acadia["runs"] == forecast["records_counted"].get("run", 0)  # one viewpoint per run
    rewrite = exclusion(rep, "forecast_log", "re-extraction")
    assert rewrite["count"] == 1 and rewrite["refs"][0]["run"] == "2026111911"
    assert rewrite["refs"][0]["first_written_in_window"] is False
    assert exclusion(rep, "forecast/hrrr", "live forecast file whose run")["refs"] == [
        {"key": "forecast/hrrr/acadia.mcfarland/2026111911z.json"}
    ]
    rows = {r["run"]: r for r in forecast["runs"]}
    assert rows["2026111911"]["first_written_in_window"] is False
    assert rows["2026111911"]["catch_up"] is True


def test_a_viewpoint_first_written_by_a_later_write_counts_there(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    r11 = hour(11)
    data.capture(*passes_every(20, 3600))
    data.forecast(
        run_record(at(-600), r11, viewpoints=[ACADIA]),
        run_record(at(1800), r11, fields=FIELDS8, viewpoints=[ACADIA, RAINIER]),
    )
    rep = report(data, hours=1.0)
    views = rep["forecast"]["viewpoints"]
    assert (views[ACADIA]["runs"], views[ACADIA]["re_extractions"]) == (0, 1)
    assert (views[RAINIER]["runs"], views[RAINIER]["re_extractions"]) == (1, 0)
    assert views[RAINIER]["catch_ups"] == ["2026111911"]  # due 11:55, before the window
    assert views[RAINIER]["runs_due_in_window"] == 0
    assert rep["forecast"]["records_counted"] == {"run": 1}  # it first wrote Rainier


def test_catch_ups_are_counted_and_listed_apart(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(*passes_every(20, 7200))
    healthy_forecast(data, at(7200))  # 06Z .. 13Z, each on time
    data.forecast(run_record(at(120), hour(0)), run_record(at(125), hour(1)))  # due 00:55, 01:55
    rep = report(data)
    acadia = rep["forecast"]["viewpoints"][ACADIA]
    assert acadia["runs"] == 4 and acadia["runs_due_in_window"] == 2
    assert acadia["catch_ups"] == ["2026111900", "2026111901"]
    rows = {r["run"]: r for r in rep["forecast"]["runs"]}
    assert rows["2026111900"]["catch_up"] is True and rows["2026111900"]["late"] is False
    assert rows["2026111912"]["catch_up"] is False
    assert rep["forecast"]["outages"] == []
    text = format_report(rep)
    assert "catch-up" in text and f"{ACADIA:<28}{4:>5}{2:>9}" in text


def test_late_runs_are_a_forecast_outage_until_the_next_run_written(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    # 13Z and 14Z wait out a sleep; at wake the stand-in fetches the newest (14Z) first.
    wake = hour(14) + timedelta(seconds=3300 + 1500)
    due13 = hour(13) + timedelta(seconds=3300)
    hourly_runs(
        data,
        cycles(SCAN_FROM, hour(16)),
        late={hour(13): (wake - due13).total_seconds() + 5, hour(14): 1500.0},
    )
    data.capture(*passes_every(20, 5 * 3600))
    rep = report(data, hours=5.0)
    rows = rep["forecast"]["outages"]
    assert len(rows) == 1
    # 14Z's write finished 3 s after wake: the first run written after 13Z was due.
    assert rows[0]["start_utc"] == z(due13) and rows[0]["end_utc"] == z(wake + timedelta(seconds=3))
    assert rows[0]["runs"] == ["2026111913", "2026111914"] and rows[0]["estimated"] is False
    assert rows[0]["lost_s"] == pytest.approx((wake - due13).total_seconds() + 3 - 300.0)
    assert rep["forecast"]["runs_late"] == ["2026111913", "2026111914"]
    late_flags = {r["run"]: r["late"] for r in rep["forecast"]["runs"]}
    assert late_flags["2026111913"] and not late_flags["2026111912"]
    # Capture ran throughout: PLAN.md's figure lost nothing; the gate rule's is the forecast's.
    clock = rep["clock"]
    assert clock["plan_md_capture_only"]["lost_h"] == 0.0
    assert clock["by_job"]["capture"]["lost_h"] == 0.0
    assert clock["lost_h"] == clock["by_job"]["forecast"]["lost_h"] > 0
    assert KIND_LATE in format_report(rep)
    assert exclusion(rep, "outages", "forecast outage")["count"] == 1


def test_a_run_a_little_late_is_not_an_outage(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    # 13Z's fetch starts 896 s after it was due and finishes 899 s after: within 900 s.
    hourly_runs(data, cycles(SCAN_FROM, hour(15)), late={hour(13): 896.0})
    data.capture(*passes_every(20, 4 * 3600))
    rep = report(data, hours=4.0)
    assert rep["forecast"]["outages"] == [] and rep["forecast"]["runs_late"] == []
    assert len(rep["forecast"]["quiet_gaps_over_threshold"]) >= 1  # hourly records: quiet gaps


@pytest.mark.parametrize("how", ["slept mid-fetch", "an error just after the due time"])
def test_a_run_finished_late_is_late_and_an_outage(tmp_path: Path, how: str) -> None:
    data = Data(tmp_path / "d")
    due13 = hour(13) + timedelta(seconds=3300)
    finished = due13 + timedelta(seconds=4000)
    hourly_runs(data, cycles(SCAN_FROM, hour(12)))
    started = due13 + timedelta(seconds=60 if how == "slept mid-fetch" else 3997)
    data.forecast(run_record(finished, hour(13), fetch_started=started))
    if how != "slept mid-fetch":
        data.forecast({"t": z(due13 + timedelta(seconds=60)), "event": "error", "run": "x"})
    # 14Z waits behind 13Z, then 15Z is on time.
    data.forecast(run_record(finished + timedelta(seconds=8), hour(14)))
    hourly_runs(data, [hour(15)])
    data.capture(*passes_every(20, 4 * 3600))
    forecast = report(data, hours=4.0)["forecast"]
    assert forecast["runs_late"] == ["2026111913"]
    assert [(r["start_utc"], r["end_utc"], r["lost_s"]) for r in forecast["outages"]] == [
        (z(due13), z(finished), 3700.0)
    ]
    assert {r["run"]: r["fetch_delay_s"] for r in forecast["runs"]}["2026111913"] == 4000.0


def test_a_cycle_never_written_is_an_estimated_outage(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    hourly_runs(data, [c for c in cycles(SCAN_FROM, hour(15)) if c != hour(14)])
    data.capture(*passes_every(20, 4 * 3600))
    rep = report(data, hours=4.0)
    rows = rep["forecast"]["outages"]
    assert len(rows) == 1 and rows[0]["runs"] == ["2026111914"] and rows[0]["estimated"] is True
    due14 = hour(14) + timedelta(seconds=3300)  # the median measured lag
    assert rows[0]["start_utc"] == z(due14)
    assert rows[0]["end_utc"] == z(hour(15) + timedelta(seconds=3333))  # 15Z written: alive
    assert rep["forecast"]["viewpoints"][ACADIA]["cycles_never_written"] == ["2026111914"]


def test_the_newest_cycle_waits_until_it_is_overdue(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    hourly_runs(data, cycles(SCAN_FROM, hour(13)))
    data.capture(*passes_every(20, 4 * 3600))
    # 14Z is due at 14:55; until 15:00 it is waiting, by 15:20 it is an ongoing outage.
    since_to = (hour(15) - T0).total_seconds() / 3600
    waiting = report(data, hours=since_to)["forecast"]
    assert waiting["outages"] == [] and waiting["cycles_waiting"] == ["2026111914"]
    later = (hour(15) + timedelta(minutes=20) - T0).total_seconds() / 3600
    rows = report(data, hours=later)["forecast"]["outages"]
    assert [(r["runs"], r["ongoing"], r["estimated"]) for r in rows] == [
        (["2026111914"], True, True)
    ]


def test_a_forecast_outage_straddling_since_loses_only_its_part_inside(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    since = hour(14) + timedelta(minutes=30)
    # The Mac slept from 13:00 to 15:09: 14Z is written 880 s after it was due (not late),
    # 13Z, due at 13:55 before the window, right after it.
    hourly_runs(
        data,
        cycles(SCAN_FROM, hour(15)),
        late={hour(13): 4482.0, hour(14): 877.0},
    )
    s0 = (since - T0).total_seconds()
    data.capture(*passes_every(s0 + 20, s0 + 7200))
    rep = build_report(data.root, since=since, until=since + timedelta(hours=2), now=at(s0 + 7200))
    rows = rep["forecast"]["outages"]
    end14 = hour(14) + timedelta(seconds=3300 + 880)  # 15:09:40, 14Z's write finished
    assert [(r["start_utc"], r["end_utc"], r["runs"], r["lost_s"]) for r in rows] == [
        (z(hour(13) + timedelta(seconds=3300)), z(end14), ["2026111913"], 2380.0)
    ]
    assert rep["forecast"]["runs_late"] == []  # 13Z was due before the window


def test_a_stand_in_dead_since_before_the_window_is_an_ongoing_outage(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    since = hour(12)
    hourly_runs(data, cycles(SCAN_FROM, hour(11)))  # the last write finished at 11:55:33
    s0 = (since - T0).total_seconds()
    data.capture(*passes_every(s0 + 20, s0 + 6 * 3600))
    until = since + timedelta(hours=6)
    rep = build_report(data.root, since=since, until=until, now=until)
    forecast = rep["forecast"]
    assert forecast["median_fetchable_lag_s"] is None
    assert forecast["lag_for_cycles_never_written"] == {
        "s": 3300.0,
        "source": "median of every complete run in the logs read",
    }
    assert [
        (r["kind"], r["start_utc"], r["end_utc"], r["lost_s"], r["ongoing"], r["estimated"])
        for r in forecast["outages"]
    ] == [(KIND_LATE, z(hour(12) + timedelta(seconds=3300)), z(until), 5 * 3600.0, True, True)]
    clock = rep["clock"]
    assert clock["by_job"]["forecast"]["lost_h"] == 5.0
    assert clock["plan_md_capture_only"]["lost_h"] == 0.0 and clock["lost_h"] == 5.0
    assert forecast["cycles_waiting"] == ["2026111917"]  # due 17:55, not overdue at 18:00


def test_no_live_run_in_the_logs_loses_the_whole_window(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(*passes_every(20, 7200))
    data.forecast({"t": z(at(600)), "event": "error", "run": "2026111912", "error": "x"})
    rep = report(data)
    forecast = rep["forecast"]
    rows = forecast["outages"]
    assert [(r["kind"], r["start_utc"], r["lost_s"], r["ongoing"]) for r in rows] == [
        (KIND_NO_RUN, z(T0), 7200.0, True)
    ]
    assert forecast["records_counted"] == {"error": 1}
    assert forecast["lag_for_cycles_never_written"]["source"].startswith("D-027")
    assert rep["clock"]["plan_md_capture_only"]["lost_h"] == 0.0
    assert rep["clock"]["covered_h"] == 0.0
    # With no forecast record at all, the same.
    empty = Data(tmp_path / "e")
    empty.capture(*passes_every(20, 7200))
    assert report(empty)["clock"]["by_job"]["forecast"]["lost_h"] == 2.0


def test_with_no_complete_run_unwritten_cycles_use_the_fallback_lag(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(*passes_every(20, 3 * 3600))
    data.forecast(run_record(at(600), hour(11), lags={0: 3000.0, 1: 3100.0}))  # f02 has no lag
    rep = report(data, hours=3.0)
    forecast = rep["forecast"]
    assert forecast["runs_incomplete"] == ["2026111911"]
    assert forecast["lag_for_cycles_never_written"]["s"] == 7200.0
    # 12Z is then due at 14:00, so the stand-in is down from 14:05 to the window's end.
    assert forecast["outages"][-1]["end_utc"] == z(at(3 * 3600))
    assert "cycles never written are dated by 7200.0 s" in format_report(rep)


def test_union_counts_an_overlap_once(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    # Capture: passes to 13:29:43, then from 15:30:00, so it loses 13:34:43-15:30:00 (6917 s).
    data.capture(*passes_every(20, 3921), *passes_every(11137, 5 * 3600))
    # Forecast: 14Z is written only at 15:55:35, right after 15Z (15:55:30), so the stand-in
    # loses 15:00:00-15:55:30 (3330 s), which overlaps capture's loss by 30 min.
    hourly_runs(data, cycles(SCAN_FROM, hour(16)), late={hour(14): 3632.0, hour(15): 27.0})
    rep = report(data, hours=5.0)
    clock = rep["clock"]
    plan = clock["plan_md_capture_only"]
    assert plan["lost_h"] == round(6917 / 3600, 3)
    assert clock["by_job"]["forecast"]["lost_h"] == round(3330 / 3600, 3)
    assert clock["lost_h"] == round(8447 / 3600, 3)  # 13:34:43-15:55:30, not 6917 + 3330 s
    assert clock["earliest_close_utc"] == z(at(72 * 3600 + 8447))
    assert plan["earliest_close_utc"] == z(at(72 * 3600 + 6917))
    text = format_report(rep)
    assert "72 h clock, gate rule (the lead's of 20 Nov" in text
    assert "PLAN.md's capture-only figure" in text and "forecast alone" in text


# --- reading ------------------------------------------------------------------------------------


def test_damaged_lines_are_reported_and_the_report_writes_nothing(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(*passes_every(20, 7200))
    log = data.root / "capture_log" / "2026-11-19.jsonl"
    lines = log.read_text().splitlines(keepends=True)
    lines.insert(3, '{"t": "2026-11-19T12:40:00Z", "event": "pa\n')
    lines.insert(5, "[1, 2]\n")
    log.write_text("".join(lines) + '{"t": "2026-11-19T14:00:00Z", "ev')  # a write in progress
    data.log("capture_log", [{"t": "2026-11-10T00:00:00Z", "event": "x"}])  # far outside
    before = {
        p: (p.stat().st_size, p.stat().st_mtime_ns) for p in data.root.rglob("*") if p.is_file()
    }
    folders = sorted(p for p in data.root.rglob("*") if p.is_dir())
    rep = report(data)
    after = {
        p: (p.stat().st_size, p.stat().st_mtime_ns) for p in data.root.rglob("*") if p.is_file()
    }
    assert after == before and sorted(p for p in data.root.rglob("*") if p.is_dir()) == folders
    damaged = exclusion(rep, "capture_log", "damaged log line (invalid-json)")
    assert damaged["refs"][0]["ref"] == "capture_log/2026-11-19.jsonl:4"
    assert exclusion(rep, "capture_log", "damaged log line (not-an-object)")["count"] == 1
    assert exclusion(rep, "capture_log", "unterminated last line")["count"] == 1
    skipped = exclusion(rep, "capture_log", "log file outside the window")
    assert skipped["refs"][0]["key"] == "capture_log/2026-11-10.jsonl"
    assert rep["inputs"]["capture_log"] == ["capture_log/2026-11-19.jsonl"]


def test_a_symlink_into_the_quarantine_is_never_followed(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(*passes_every(20, 7200))
    hidden = "quarantine/frames-x/2026-11-19/20261119T130000Z_aaaaaaaaaaaa"
    data.json(hidden + ".json", {"fetched_at_utc": z(at(100))})
    (data.root / "frames").mkdir()
    os.symlink(data.root / "quarantine" / "frames-x", data.root / "frames" / ACADIA)
    rep = report(data)
    disk = rep["capture"]["viewpoints"][ACADIA]["frames"]["disk"]["full_size"]
    assert disk["on_disk_in_window"] == 0
    assert rep["inputs"]["unreadable_count"] >= 1
    assert "quarantine" in rep["inputs"]["unreadable"][0]["reason"]


def test_window_rules(tmp_path: Path) -> None:
    data = Data(tmp_path / "d")
    data.capture(pass_(T0), pass_(at(7200)))  # exactly at since: in; exactly at until: out
    rep = report(data)
    assert rep["capture"]["passes"]["count"] == 1
    assert exclusion(rep, "capture_log", "after the window")["count"] == 1
    with pytest.raises(ValueError, match="must be after"):
        build_report(data.root, since=T0, until=T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_report(data.root, since=datetime(2026, 11, 19), until=at(10))


# --- the command --------------------------------------------------------------------------------


def test_cli_prints_json_or_a_table(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = Data(data_dir)
    data.capture(*passes_every(20, 7200), poll(at(100), ACADIA))
    args = ["data-report", "--since", z(T0), "--until", z(at(7200))]
    assert cli.main([*args, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["kind"] == REPORT_KIND and doc["window"]["hours"] == 2.0
    # Every manifest viewpoint gets a row, with zeros where nothing was logged.
    assert len(doc["capture"]["viewpoints"]) == 8
    assert doc["capture"]["viewpoints"][RAINIER]["polls"] == 0
    out = tmp_path / "report.json"
    assert cli.main([*args, "--json", str(out), "--capture-gap-s", "600"]) == 0
    text = capsys.readouterr().out
    assert "72 h clock, gate rule" in text and "2.00 h covered" in text  # PLAN.md's figure
    assert "Exclusions" in text
    assert json.loads(out.read_text())["rules"]["capture_outage_gap_s"] == 600.0
    assert cli.main(["data-report", "--since", z(T0), "--until", z(T0)]) == 2
    assert "must be after" in capsys.readouterr().err
