"""The switch from the frozen 0.1.0 loop to this code (the lead re-snapshots the runtime): a camera
state and frames in the old shape are read as they are, the image the old loop handled is not
fetched again, its frames stay, the hour they fill is not kept twice, and ``status`` tells the
policy the running writer used from the one this code would use."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from lookout_common.paths import frame_key
from lookout_common.storage import LocalStorage

from lookout_capture.cli import main
from lookout_capture.live import status_report
from lookout_capture.retention import hour_slot, key_stamp
from lookout_capture.state import CameraState, camera_state_key

from .conftest import FakeClock
from .test_live import events, image_gets, runner
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
    # The first pass of the new code: the feed still shows the image the old loop stored.
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
