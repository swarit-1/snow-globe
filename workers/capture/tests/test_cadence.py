"""Update cadence from logs and sidecars (no network): consecutive-poll pairs, gaps, the
stopgap's records, ARD 304s and imagedates, sidecar-only frames, and the console command."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from snow_globe_common.manifest import load_manifests
from snow_globe_common.storage import LocalStorage

from snow_globe_capture.cadence import camera_cadence, quantile, summarise
from snow_globe_capture.cli import main

VPS = load_manifests()
RAINIER = VPS["rainier.paradise-mountain"]
GRCA = VPS["grand-canyon.yavapai"]
NOW = datetime(2026, 11, 19, 22, 0, tzinfo=UTC)


def write_log(root: Path, *records: dict) -> None:
    folder = root / "capture_log"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "2026-11-19.jsonl").open("a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def sidecar(storage: LocalStorage, key: str, **fields) -> None:
    storage.write_json(key[:-4] + ".json", {"viewpoint_id": fields.pop("vp", RAINIER.id), **fields})


def cap(t: str, event: str, vp: str = RAINIER.id, **fields) -> dict:
    return {"t": t, "worker": "capture", "viewpoint_id": vp, "event": event, **fields}


def test_quantiles_interpolate_like_numpy():
    data = [1.0, 2.0, 3.0, 4.0]
    assert quantile(data, 0.5) == 2.5
    assert quantile(data, 0.1) == pytest.approx(1.3)
    assert quantile(data, 0.9) == pytest.approx(3.7)
    assert quantile([], 0.5) is None
    assert summarise([]) == {
        "n": 0,
        "median": None,
        "p10": None,
        "p90": None,
        "min": None,
        "max": None,
    }


def test_park_camera_intervals_use_consecutive_polls_only(data_root):
    storage = LocalStorage(data_root)
    folder = "frames/rainier.paradise-mountain/2026-11-19/"
    for stamp, lm in (("063013", "06:30:13"), ("063513", "06:35:13"), ("064014", "06:40:14")):
        sidecar(
            storage,
            f"{folder}20261119T{stamp}Z_aaaaaaaaaaa{stamp[-1]}.jpg",
            http_last_modified=f"Thu, 19 Nov 2026 {lm} GMT",
            capture_version="stopgap-0",
            fetched_at_utc=f"2026-11-19T{lm}.1+00:00",
        )
    key = f"{folder}20261119T123402Z_bbbbbbbbbbbb.jpg"
    sidecar(
        storage,
        key,
        head_last_modified="Thu, 19 Nov 2026 12:34:13 GMT",
        http_last_modified="Thu, 19 Nov 2026 12:34:13 GMT",
        fetched_at_utc="2026-11-19T12:34:20Z",
    )
    write_log(
        data_root,
        {
            "vp": RAINIER.id,
            "event": "stored",
            "file": "2026-11-19/20261119T063013Z_aaaaaaaaaaa3.jpg",
            "t": "2026-11-19T06:30:13.2+00:00",
        },
        {
            "vp": RAINIER.id,
            "event": "stored",
            "file": "2026-11-19/20261119T063513Z_aaaaaaaaaaa3.jpg",
            "t": "2026-11-19T06:35:13.5+00:00",
        },
        {
            "vp": RAINIER.id,
            "event": "stored",
            "file": "2026-11-19/20261119T064014Z_aaaaaaaaaaa4.jpg",
            "t": "2026-11-19T06:40:14.0+00:00",
        },
        {"vp": RAINIER.id, "event": "rate_capped", "t": "2026-11-19T06:45:14+00:00"},
        {"vp": RAINIER.id, "event": "rate_capped", "t": "2026-11-19T06:50:14+00:00"},
        # the laptop sleeps until 12:24
        cap(
            "2026-11-19T12:24:35Z", "unchanged", http_last_modified="Thu, 19 Nov 2026 12:24:14 GMT"
        ),
        cap(
            "2026-11-19T12:29:36Z",
            "rate_capped",
            http_last_modified="Thu, 19 Nov 2026 12:29:13 GMT",
        ),
        cap("2026-11-19T12:34:21Z", "stored", key=key),
        cap("2026-11-19T12:39:40Z", "error", stage="head", status=503),
        cap("2026-11-19T12:39:40Z", "backoff", seconds=300),
        cap(
            "2026-11-19T12:44:41Z", "unchanged", http_last_modified="Thu, 19 Nov 2026 12:44:14 GMT"
        ),
        cap(
            "2026-11-19T12:49:42Z", "unchanged", http_last_modified="Thu, 19 Nov 2026 12:44:14 GMT"
        ),
        cap(
            "2026-11-19T12:49:42Z", "unchanged", vp=GRCA.id, json_last_modified="x"
        ),  # other camera
    )
    report = camera_cadence(storage, RAINIER, now=NOW, hours=24)
    series = report["series"]["last_modified"]
    assert report["polls"]["by_source"] == {"capture": 6, "stopgap": 5}
    assert series["consecutive_pairs"] == {"changed": 4, "gap": 1, "same": 1, "unknown": 4}
    assert series["interval_s"]["n"] == 4 and series["interval_s"]["median"] == 300.0
    assert (
        series["interval_s"]["p10"] == pytest.approx(299.3) and series["interval_s"]["max"] == 301.0
    )
    assert series["histogram_10s"] == {"300": 4}
    assert series["intervals_within_3s_of_whole_minutes"] == "4/4"
    assert report["gaps_over_max"] == 1 and report["max_gap_s"] == 540.0
    assert series["distinct_values"] == 7
    short = camera_cadence(storage, RAINIER, now=NOW, hours=10)  # 12:00 onwards only
    assert short["polls"]["by_source"] == {"capture": 6}


def test_ard_feeds_carry_304s_forward_and_have_an_imagedate_series(data_root):
    storage = LocalStorage(data_root)
    key = "thumbs/grand-canyon.yavapai/2026-11-19/20261119T123000Z_cccccccccccc.jpg"
    sidecar(
        storage,
        key,
        vp=GRCA.id,
        json_last_modified="Thu, 19 Nov 2026 12:40:30 GMT",
        json_imagedate_raw="Updated 11/19/2026 05:30 AM MST",
        fetched_at_utc="2026-11-19T12:41:00Z",
    )
    write_log(
        data_root,
        cap(
            "2026-11-19T12:26:00Z",
            "unchanged",
            vp=GRCA.id,
            reason="imagedate_same",
            json_last_modified="Thu, 19 Nov 2026 12:25:32 GMT",
            imagedate_raw="Updated 11/19/2026 05:15 AM MST",
        ),
        cap(
            "2026-11-19T12:31:00Z",
            "unchanged",
            vp=GRCA.id,
            reason="json_not_modified",
            imagedate_raw="Updated 11/19/2026 05:15 AM MST",
        ),
        cap(
            "2026-11-19T12:36:00Z",
            "unchanged",
            vp=GRCA.id,
            reason="json_not_modified",
            imagedate_raw="Updated 11/19/2026 05:15 AM MST",
        ),
        cap("2026-11-19T12:41:01Z", "stored", vp=GRCA.id, key=key),
        cap(
            "2026-11-19T12:46:00Z",
            "rate_capped",
            vp=GRCA.id,
            imagedate_raw="Updated 11/19/2026 05:45 AM MST",
        ),
    )
    report = camera_cadence(storage, GRCA, now=NOW)
    lm = report["series"]["last_modified"]
    assert lm["what"] == "JSON feed Last-Modified"
    assert lm["consecutive_pairs"] == {"changed": 1, "same": 2, "unknown": 1}
    assert lm["interval_s"]["median"] == 898.0
    image = report["series"]["imagedate"]
    assert image["consecutive_pairs"] == {"changed": 2, "same": 2}
    assert image["interval_s"]["median"] == 900.0 and image["distinct_values"] == 3


def test_frames_without_log_records_still_count_once(data_root):
    storage = LocalStorage(data_root)
    for root in ("frames", "thumbs"):  # one download: its frame and its thumbnail
        sidecar(
            storage,
            f"{root}/rainier.paradise-mountain/2026-11-19/20261119T150759Z_dddddddddddd.jpg",
            head_last_modified="Thu, 19 Nov 2026 15:08:10 GMT",
            fetched_at_utc="2026-11-19T15:08:12Z",
        )
    sidecar(
        storage,
        "frames/rainier.paradise-mountain/2026-11-19/20261119T151259Z_eeeeeeeeeeee.jpg",
        head_last_modified="Thu, 19 Nov 2026 15:13:10 GMT",
        fetched_at_utc="2026-11-19T15:13:12Z",
    )
    report = camera_cadence(storage, RAINIER, now=NOW)
    assert report["polls"]["by_source"] == {"sidecar": 2} and report["logs_read"] == []
    assert report["series"]["last_modified"]["interval_s"]["median"] == 300.0


def test_cadence_command_needs_no_network_and_no_fetcher_lock(data_root, capsys, monkeypatch):
    import snow_globe_common.http as http

    from snow_globe_capture.fetcher_guard import nps_fetcher_lock

    monkeypatch.setattr(
        http, "polite_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    write_log(
        data_root,
        cap(
            "2026-11-19T12:24:35Z", "unchanged", http_last_modified="Thu, 19 Nov 2026 12:24:14 GMT"
        ),
        cap(
            "2026-11-19T12:29:36Z", "unchanged", http_last_modified="Thu, 19 Nov 2026 12:29:13 GMT"
        ),
    )
    with nps_fetcher_lock("the running loop", processes=lambda: []):
        assert main(["cadence", RAINIER.id, "--hours", "48", "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["viewpoint_id"] == RAINIER.id and report["hours"] == 48
        assert main(["cadence", RAINIER.id]) == 0
        out = capsys.readouterr().out
        assert "median" in out and "age at poll" in out and "=>" in out


def test_a_304_repeats_only_the_poll_just_before_it(data_root):
    """The review's case: a poll whose new value was not logged, then a 304, must not stretch
    the next interval back to the older logged value."""
    storage = LocalStorage(data_root)
    write_log(
        data_root,
        cap(
            "2026-11-19T12:00:00Z",
            "unchanged",
            vp=GRCA.id,
            reason="imagedate_same",
            json_last_modified="Thu, 19 Nov 2026 11:58:00 GMT",
        ),
        # A new feed (v2) whose image was a duplicate: the old code logged no Last-Modified.
        cap("2026-11-19T12:05:00Z", "duplicate", vp=GRCA.id, reason="not_modified"),
        cap("2026-11-19T12:10:00Z", "unchanged", vp=GRCA.id, reason="json_not_modified"),
        cap(
            "2026-11-19T12:15:00Z",
            "unchanged",
            vp=GRCA.id,
            reason="imagedate_same",
            json_last_modified="Thu, 19 Nov 2026 12:08:00 GMT",
        ),
    )
    series = camera_cadence(storage, GRCA, now=NOW)["series"]["last_modified"]
    assert series["consecutive_pairs"] == {"unknown": 3}
    assert series["interval_s"]["n"] == 0  # not one 600 s interval
    # Logged by today's code, the 304 carries its own value and nothing is inferred.
    write_log(
        data_root,
        cap(
            "2026-11-19T12:20:00Z",
            "unchanged",
            vp=GRCA.id,
            reason="json_not_modified",
            json_last_modified="Thu, 19 Nov 2026 12:08:00 GMT",
        ),
    )
    series = camera_cadence(storage, GRCA, now=NOW)["series"]["last_modified"]
    assert series["consecutive_pairs"] == {"same": 1, "unknown": 3}


def test_a_camera_faster_than_its_polls_is_measured_by_the_age_at_poll(data_root):
    """A park camera updating every 60 s, polled every 300-330 s: every pair changes, so the
    intervals are the poll spacing, and the age at poll is what measures the camera."""
    storage = LocalStorage(data_root)
    records = []
    base = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    offsets = [0, 305, 612, 914, 1230, 1536, 1840, 2155, 2470, 2771, 3090, 3402]
    for i, offset in enumerate(offsets):
        asked = base.timestamp() + offset + 7.0  # seconds past each poll's minute vary
        lm = datetime.fromtimestamp(asked - (asked % 60) + 13, UTC)  # uploads at :13
        if lm.timestamp() > asked:
            lm = datetime.fromtimestamp(lm.timestamp() - 60, UTC)
        when = datetime.fromtimestamp(asked, UTC)
        stamp = when.isoformat().replace("+00:00", "Z")
        records.append(cap(stamp, "request", stage="head", method="HEAD"))
        records.append(
            cap(
                stamp,
                "rate_capped" if i % 2 else "unchanged",
                http_last_modified=lm.strftime("%a, %d %b %Y %H:%M:%S GMT"),
            )
        )
    write_log(data_root, *records)
    series = camera_cadence(storage, RAINIER, now=NOW)["series"]["last_modified"]
    assert series["consecutive_pairs"] == {"changed": 11}
    assert series["poll_limited"] is True and series["values_on_minute_grid"] is True
    ages = series["age_at_poll_s"]
    assert ages["n"] == 12 and ages["min"] >= 0 and ages["max"] < 60
    verdict = series["cadence"]
    assert verdict["basis"] == "age_at_poll" and verdict["updates_at_least_every_s"] == ages["max"]
    assert "only measure the poll spacing" in verdict["text"]
    # The interval median is the poll spacing, not the camera's 60 s.
    assert 290 <= series["interval_s"]["median"] <= 330


def test_a_camera_slower_than_its_polls_is_measured_by_its_intervals(data_root):
    storage = LocalStorage(data_root)
    records = []
    for i in range(12):  # an ARD imagedate: new every 15 min, polled every 5 min
        minute = 5 * i
        shown = (minute // 15) * 15
        records.append(
            cap(
                f"2026-11-19T12:{minute:02d}:30Z",
                "unchanged",
                vp=GRCA.id,
                reason="imagedate_same",
                imagedate_raw=f"Updated 11/19/2026 04:{shown:02d} AM MST",
            )
        )
    write_log(data_root, *records)
    series = camera_cadence(storage, GRCA, now=NOW)["series"]["imagedate"]
    assert series["poll_limited"] is False and series["consecutive_pairs"]["same"] == 8
    assert series["cadence"]["basis"] == "intervals"
    assert series["cadence"]["update_interval_median_s"] == 900.0


def test_cadence_reads_the_validators_logged_by_this_version(data_root, fake_nps, clock):
    """End to end: polls logged by live capture itself carry every value the tool needs."""
    from .test_live import POLL_STEP_S, runner, set_rainier

    storage = LocalStorage(data_root)
    for i in range(6):
        set_rainier(fake_nps, clock.now() - timedelta(seconds=40), etag=f'"v{i}"', pattern=i)
        runner(storage, fake_nps, clock).run_pass([RAINIER])
        clock.advance(POLL_STEP_S)
    report = camera_cadence(storage, RAINIER, now=clock.now())
    series = report["series"]["last_modified"]
    assert report["polls"]["total"] == 6 and report["polls"]["with_answer_time"] == 6
    assert series["consecutive_pairs"] == {"changed": 5}  # stored and rate-capped polls alike
    assert series["age_at_poll_s"]["n"] == 6
    assert series["age_at_poll_s"]["min"] == pytest.approx(26.0, abs=1.0)


def test_poll_spacing_is_measured_between_the_polls_sends(data_root):
    """Review finding (20 Nov 2026): ``poll_spacing_s`` was taken between the poll *outcome*
    records, which come after a poll's answer and after any image GET it led to, so its
    minimum read under 300 s for polls nps.gov saw 300 s apart. It is now taken between the
    sends in the polls' request records (``sent_at``; for a 0.2.0 record, ``t`` minus
    ``elapsed_ms``)."""
    storage = LocalStorage(data_root)
    write_log(
        data_root,
        cap(
            "2026-11-19T10:00:00.200Z",
            "request",
            stage="head",
            sent_at="2026-11-19T10:00:00.100Z",
            answered_at="2026-11-19T10:00:00.200Z",
            elapsed_ms=100.0,
        ),
        cap("2026-11-19T10:00:05.900Z", "request", stage="image", elapsed_ms=5600.0),
        cap("2026-11-19T10:00:06.000Z", "duplicate", reason="not_modified"),
        cap(
            "2026-11-19T10:05:00.300Z",
            "request",
            stage="head",
            sent_at="2026-11-19T10:05:00.202Z",
            answered_at="2026-11-19T10:05:00.300Z",
            elapsed_ms=98.0,
        ),
        cap(
            "2026-11-19T10:05:00.301Z",
            "unchanged",
            reason="validators_same",
            head_last_modified="Thu, 19 Nov 2026 10:04:14 GMT",
        ),
        # A record of 0.2.0, which logged no send: t - elapsed_ms.
        cap("2026-11-19T10:10:01.400Z", "request", stage="head", elapsed_ms=150.0),
        cap(
            "2026-11-19T10:10:01.401Z",
            "unchanged",
            reason="validators_same",
            head_last_modified="Thu, 19 Nov 2026 10:09:14 GMT",
        ),
    )
    out = camera_cadence(storage, RAINIER, now=NOW)
    spacing = out["poll_spacing_s"]
    assert spacing["n"] == 2 and spacing["min"] == 300.1  # 10:00:00.100 -> 10:05:00.202
    assert spacing["max"] == 301.0  # 10:05:00.202 -> 10:10:01.250
    assert out["polls"]["with_send_time"] == 3
