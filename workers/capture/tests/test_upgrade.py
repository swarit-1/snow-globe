"""The switch from an older loop to this code (the lead re-snapshots the runtime).

- From the frozen 0.1.0 loop: a camera state and frames in the old shape are read as they are,
  the image the old loop handled is not fetched again, its frames stay, the hour they fill is
  not kept twice, and ``status`` tells the policy the running writer used from the one this code
  would use.
- From the running 0.2.0 loop (a state file recorded from it): its state has no answer times,
  so the first poll is bounded from above before anything is sent (README, "Upgrade and crash
  bounds"), and its image GETs count from their reservation + 2 s + the bound. Each bound is
  logged (``answer_bound``) as a record of the moment it is written, and ``status`` says the
  next poll it shows is counted from such a state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from snow_globe_common.paths import frame_key
from snow_globe_common.storage import LocalStorage
from snow_globe_common.timeutil import isoformat_z

from snow_globe_capture.cli import main
from snow_globe_capture.live import MIN_POLL_INTERVAL_S, POLL_MARGIN_S, status_report
from snow_globe_capture.retention import hour_slot, key_stamp
from snow_globe_capture.state import CameraState, camera_state_key

from .conftest import FakeClock, fixture_json
from .test_live import RAINIER, events, image_gets, runner, set_rainier
from .test_live_retention import GRCA, ArdFeed, jpgs

START = datetime(2026, 11, 19, 12, 1, 30, tzinfo=UTC)
OLD_VERSION = "lookout-capture/0.1.0"


def old_state_and_frame(storage: LocalStorage, fake_nps, feed: ArdFeed) -> str:
    """What the 0.1.0 loop leaves behind after storing the feed's current image (11:45 UTC)."""
    feed.update(START)
    json_res = fake_nps.resources[GRCA.truth_camera.json_url]
    image_res = fake_nps.resources[GRCA.truth_camera.image_url]
    imagedate = json.loads(json_res.body)["imagedate"]
    sha = hashlib.sha256(image_res.body).hexdigest()
    key = frame_key(GRCA.id, feed.current, sha)
    storage.put_bytes(key, image_res.body)
    side = key[:-4] + ".json"
    storage.write_json(
        side,
        {
            "viewpoint_id": GRCA.id,
            "source": "live",
            "fetched_at_utc": "2026-11-19T11:58:00.120Z",
            "etag": image_res.etag,
            "json_imagedate_raw": imagedate,
            "sha256": sha,
            "image_time_utc": "2026-11-19T11:45:00Z",
            "stored": "original",
            "capture_version": OLD_VERSION,
            "sidecar_schema": 1,
        },
    )
    lm = json_res.response_headers()["Last-Modified"]
    storage.write_json(
        camera_state_key(GRCA.id),
        {  # exactly the 0.1.0 CameraState fields
            "viewpoint_id": GRCA.id,
            "schema": 1,
            "created_at": "2026-11-19T10:56:15Z",
            "seeded": {"at": "2026-11-19T10:56:15Z", "sidecars_read": 0, "latest_key": None},
            "last_poll_at": "2026-11-19T11:58:00.010Z",
            "polls": 12,
            "json_etag": json_res.etag,
            "json_last_modified": lm,
            "imagedate_raw": imagedate,
            "handled_imagedate_raw": imagedate,
            "head_etag": None,
            "head_last_modified": None,
            "settled_etag": image_res.etag,
            "settled_last_modified": image_res.response_headers()["Last-Modified"],
            "last_image": {
                "key": key,
                "sha256": sha,
                "etag": image_res.etag,
                "image_time_utc": "2026-11-19T11:45:00Z",
                "fetched_at_utc": "2026-11-19T11:58:00.120Z",
                "json_imagedate_raw": imagedate,
                "capture_version": OLD_VERSION,
            },
            "gets": [{"t": "2026-11-19T11:58:00.050Z", "status": "ok", "source": "self"}],
            "recent_shas": {sha: key},
            "known_sidecars": [side],
            "failures": 0,
            "image_failures": 0,
            "not_before": None,
            "last_error": None,
            "counters": {"stored": 4, "unchanged": 8},
        },
    )
    return key


def test_the_switch_from_the_old_loop_keeps_its_frames_and_fetches_nothing_twice(
    data_root, fake_nps, capsys
):
    storage = LocalStorage(data_root)
    clock = FakeClock(START + timedelta(minutes=2))
    fake_nps.clock = clock
    feed = ArdFeed(fake_nps, GRCA, "grca")
    old_key = old_state_and_frame(storage, fake_nps, feed)
    old_bytes = storage.get_bytes(old_key)
    before = status_report(storage, [GRCA], now=clock.now())["cameras"][0]
    assert before["frame_policy"] == "hourly-nearest" and before["frame_policy_recorded"] == "every"
    assert before["last_capture_version"] == OLD_VERSION
    assert main(["status"]) == 0
    assert "every*" in capsys.readouterr().out
    # The first pass of the new code: the old state recorded no answer time, so its last poll
    # (sent at 11:58:00.010) counts as answered when this run read it (12:03:30), and the camera
    # is not polled again before 12:08:30.002. Then the feed still shows the old loop's image.
    out = runner(storage, fake_nps, clock).run_pass([GRCA])["outcomes"][GRCA.id]
    assert out == {"event": "not_due", "due_at": "2026-11-19T12:08:31Z"} and not fake_nps.requests
    bounds = {e["what"]: e for e in events(data_root) if e["event"] == "answer_bound"}
    assert bounds["poll"]["kind"] == "legacy_state"
    assert bounds["poll"]["answer_bound_at"] == "2026-11-19T12:03:30.000Z"
    assert bounds["image_get"]["answer_bound_at"] == "2026-11-19T11:59:02.050Z"  # t + 62 s
    clock.current = datetime(2026, 11, 19, 12, 8, 30, 2000, tzinfo=UTC)
    out = runner(storage, fake_nps, clock).run_pass([GRCA])["outcomes"][GRCA.id]
    assert out["event"] == "unchanged" and image_gets(fake_nps, GRCA.truth_camera.image_url) == []
    for _ in range(30):  # 2.5 hours of 5-minute passes
        clock.advance(300)
        feed.update(clock.now())
        runner(storage, fake_nps, clock).run_pass([GRCA])
    frames = jpgs(storage, "frames/")
    assert storage.get_bytes(old_key) == old_bytes and old_key in frames
    hours = [hour_slot(key_stamp(k)) for k in frames]
    assert len(hours) == len(set(hours))  # the 12:00 hour is the old frame's, kept once
    dropped = [e for e in events(data_root) if e["event"] == "staging_dropped"]
    assert dropped[0]["reason"] == "hour_already_kept" and dropped[0]["hour_utc"] == (
        "2026-11-19T12:00:00Z"
    )
    assert [e["key"] for e in events(data_root) if e["event"] == "promoted"]
    gets = image_gets(fake_nps, GRCA.truth_camera.image_url)
    assert len(gets) == len(jpgs(storage, "thumbs/")) >= 8  # a thumbnail of every download
    state = CameraState.from_dict(storage.read_json(camera_state_key(GRCA.id)))
    assert state.last_image["frame_policy"] == "hourly-nearest" and state.counters["stored"] >= 12
    after = status_report(storage, [GRCA], now=clock.now())["cameras"][0]
    assert after["frame_policy_recorded"] == "hourly-nearest"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_a_state_recorded_from_the_running_0_2_0_loop_is_bounded_before_its_first_poll(
    data_root, fake_nps
):
    """``fixtures/state_0.2.0_rainier.paradise-mountain.json`` is Rainier's state as the running
    0.2.0 loop left it at 13:01 UTC on 20 Nov 2026 (metadata only; its dedupe lists cut to their
    newest 3 entries). It has no answer times: the last poll was sent at 13:01:58.317, and each
    image GET carries its reservation time. Restarted 2 min later, this code:
    - bounds that poll's answer by the later of the send + 60 s and the moment it read the
      state, and sends no poll before that bound + 300.002 s;
    - bounds each GET's answer by its reservation + 2 s + 60 s (0.2.0 sent after a state write
      and the 1 s request spacing; the recorded fetch 1.196 s after the last reservation is
      within it), so the image cap is never released early by the switch.
    """
    doc = fixture_json("state_0.2.0_rainier.paradise-mountain.json")
    assert "last_answer_at" not in doc and doc["last_image"]["capture_version"].endswith("0.2.0")
    storage = LocalStorage(data_root)
    storage.write_json(camera_state_key(RAINIER.id), doc)
    sent = _utc(doc["last_poll_at"])
    read_at = sent + timedelta(minutes=2)
    clock = FakeClock(read_at)
    fake_nps.clock = clock
    set_rainier(fake_nps, read_at - timedelta(seconds=30), etag='"after-switch"')
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "not_due" and fake_nps.requests == []
    state = CameraState.from_dict(storage.read_json(camera_state_key(RAINIER.id)))
    assert state.last_answer_bound == "legacy_state"
    assert _utc(state.last_answer_at) == max(sent + timedelta(seconds=60), read_at)
    for record, recorded in zip(state.gets, doc["gets"], strict=True):
        assert record["answer_bound"] == "legacy_record"
        assert _utc(record["answered_at"]) == _utc(recorded["t"]) + timedelta(seconds=62)
    last_fetch = _utc(doc["last_image"]["fetched_at_utc"])
    assert last_fetch <= _utc(state.gets[-1]["answered_at"])  # the real answer is inside it
    slot = _utc(state.last_answer_at) + timedelta(seconds=MIN_POLL_INTERVAL_S + POLL_MARGIN_S)
    clock.current = slot - timedelta(milliseconds=1)
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "not_due" and fake_nps.requests == []
    clock.current = slot
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert fake_nps.requests and fake_nps.requests[0]["method"] == "HEAD"
    assert fake_nps.requests[0]["t"] >= slot
    # The new image is fetched: 0.2.0's GETs at 12:04-12:51 count until 13:05:21-13:53:58.
    assert out["event"] == "stored", out
    state = CameraState.from_dict(storage.read_json(camera_state_key(RAINIER.id)))
    assert state.last_answer_bound is None and state.gets[-1]["answered_at"]


def _from_the_running_0_2_0_loop(data_root, fake_nps):
    """The 0.2.0 fixture state, and a clock 2 min after its last poll."""
    doc = fixture_json("state_0.2.0_rainier.paradise-mountain.json")
    storage = LocalStorage(data_root)
    storage.write_json(camera_state_key(RAINIER.id), doc)
    read_at = _utc(doc["last_poll_at"]) + timedelta(minutes=2)
    clock = FakeClock(read_at)
    fake_nps.clock = clock
    return doc, storage, clock


def test_the_bounds_of_an_upgrade_are_logged_at_the_moment_they_are_written(data_root, fake_nps):
    """Review finding (20 Nov 2026): each image GET's ``answer_bound`` record carried the GET's
    own old stamp as ``t``, which replaced the record's write time, so the switch would have
    written records dated up to 2 h back (into the previous day's time, in a new day's file).
    The GET's stamps now go under ``get_t``/``get_reserved_at``, and no field can replace a
    record's ``t``: every line of the log is in write order and dated when it was written."""
    doc, storage, clock = _from_the_running_0_2_0_loop(data_root, fake_nps)
    run = runner(storage, fake_nps, clock)
    assert run.run_pass([RAINIER])["outcomes"][RAINIER.id]["event"] == "not_due"
    records = events(data_root, clock.now().date().isoformat())
    stamp = isoformat_z(clock.now(), "milliseconds")
    assert records and all(r["t"] == stamp for r in records), {r["t"] for r in records}
    gets = [r for r in records if r["event"] == "answer_bound" and r["what"] == "image_get"]
    assert [g["get_t"] for g in gets] == [g["t"] for g in doc["gets"]]
    run._log(RAINIER.id, "probe", t="1999-01-01T00:00:00.000Z", worker="x", runner="y")
    probe = events(data_root, clock.now().date().isoformat())[-1]
    assert (probe["t"], probe["worker"], probe["runner"]) == (stamp, "capture", "cli")


def test_status_says_when_the_next_poll_is_counted_from_older_code_s_state(
    data_root, fake_nps, capsys
):
    """Review finding (20 Nov 2026): run from the repo against the running 0.2.0's state,
    ``status`` showed every camera's next poll at "now + 300 s" with no note, though 0.2.0
    itself polls about 301 s after its own send. Each row now says what its next poll is
    counted from (``next_poll_basis``), and the text output flags the older code's states."""
    _doc, storage, clock = _from_the_running_0_2_0_loop(data_root, fake_nps)
    row = status_report(storage, [RAINIER], now=clock.now())["cameras"][0]
    assert row["next_poll_basis"] == "legacy_state:now"
    assert main(["status"]) == 0
    assert "no answer time recorded" in capsys.readouterr().out
    runner(storage, fake_nps, clock).run_pass([RAINIER])  # this code settles the bound
    row = status_report(storage, [RAINIER], now=clock.now())["cameras"][0]
    assert row["next_poll_basis"] == "bound:legacy_state"
