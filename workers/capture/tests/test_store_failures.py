"""A download that cannot be stored is never fetched again (CLAUDE.md: download only on change),
the store backs off, and a design-P thumbnail that a failed write or a crash left out is made
later from the stored frame (PRD §8.7: a thumbnail for every frame)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest
from lookout_common.storage import LocalStorage

from lookout_capture.state import CameraState, camera_state_key

from .conftest import FakeClock
from .test_live import events, image_gets, runner, set_rainier
from .test_live_retention import GRCA, RAINIER, ArdFeed, jpgs


class RefusingStorage(LocalStorage):
    """Local storage whose writes under some prefixes fail, as an IAM grant missing in S3 would."""

    def __init__(self, root, refuse: tuple[str, ...]) -> None:
        super().__init__(root)
        self.refuse = refuse
        self.refused = 0

    def put_bytes(self, key, data, *, content_type=None, if_absent=False):
        if self.refuse and key.startswith(self.refuse):
            self.refused += 1
            raise PermissionError(f"AccessDenied: PutObject {key}")
        return super().put_bytes(key, data, content_type=content_type, if_absent=if_absent)


class Crash(BaseException):
    """The process dies (a kill, a Lambda timeout): nothing catches it."""


class CrashingStorage(LocalStorage):
    """Local storage that dies once, at the first write under ``prefix`` ending in ``suffix``."""

    def __init__(self, root, prefix: str, suffix: str = "") -> None:
        super().__init__(root)
        self.prefix, self.suffix = prefix, suffix
        self.armed = True

    def put_bytes(self, key, data, *, content_type=None, if_absent=False):
        if self.armed and key.startswith(self.prefix) and key.endswith(self.suffix):
            self.armed = False
            raise Crash(key)
        return super().put_bytes(key, data, content_type=content_type, if_absent=if_absent)


def state_of(storage, vp) -> CameraState:
    return CameraState.from_dict(storage.read_json(camera_state_key(vp.id)))


def test_a_failing_store_never_downloads_the_same_image_again(data_root, fake_nps):
    storage = RefusingStorage(data_root, ("thumbs/", "cache/staging/"))
    clock = FakeClock(datetime(2026, 11, 19, 12, 1, 30, tzinfo=UTC))
    fake_nps.clock = clock
    feed = ArdFeed(fake_nps, GRCA, "grca")
    feed.update(clock.now())  # one image, and the feed stays on it for two hours
    outcomes = []
    for _ in range(24):
        outcomes.append(runner(storage, fake_nps, clock).run_pass([GRCA])["outcomes"][GRCA.id])
        clock.advance(300)
    assert len(image_gets(fake_nps, GRCA.truth_camera.image_url)) == 1
    assert outcomes[0]["event"] == "error" and outcomes[0]["stage"] == "store"
    assert {o["event"] for o in outcomes[1:]} <= {"unchanged", "not_due"}
    state = state_of(storage, GRCA)
    assert state.store_failures == 1 and state.handled_imagedate_raw == state.imagedate_raw
    assert state.last_error["stage"] == "store" and state.counters["store_failed"] == 1
    assert [g["status"] for g in state.gets] in ([], ["ok"])  # the GET itself succeeded
    errors = [e for e in events(data_root) if e["event"] == "error"]
    assert len(errors) == 1 and errors[0]["stage"] == "store"
    assert "AccessDenied" in errors[0]["error"] and errors[0]["json_last_modified"]
    # The store recovers: the next new image is stored and the failure count resets.
    storage.refuse = ()
    clock.advance(900)
    feed.update(clock.now())
    out = runner(storage, fake_nps, clock).run_pass([GRCA])["outcomes"][GRCA.id]
    assert out["event"] == "stored" and state_of(storage, GRCA).store_failures == 0


def test_a_store_that_keeps_failing_backs_off_on_a_busy_camera(data_root, clock, fake_nps):
    storage = RefusingStorage(data_root, ("frames/",))
    for i in range(48):  # four hours; the park camera has a new image at every poll
        set_rainier(fake_nps, clock.now(), etag=f'"b{i}"', pattern=i)
        runner(storage, fake_nps, clock).run_pass([RAINIER])
        clock.advance(300)
    gets = image_gets(fake_nps, RAINIER.truth_camera.image_url)
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(gets)]
    assert gaps == sorted(gaps) and gaps[-1] >= 3600  # 5, 10, 20, 40 min, then 1 h
    assert 5 <= len(gets) <= 8
    assert state_of(storage, RAINIER).store_failures == len(gets)
    heads = [r for r in fake_nps.requests if r["method"] == "HEAD"]
    assert len(heads) < 48  # polling backed off too


def test_a_design_p_thumbnail_whose_write_failed_is_made_from_the_stored_frame(
    data_root, clock, fake_nps
):
    storage = RefusingStorage(data_root, ("thumbs/",))
    set_rainier(fake_nps, clock.now(), etag='"t1"')
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "stored" and "AccessDenied" in out["thumb_error"]
    assert storage.exists(out["key"]) and not storage.exists(out["thumb_key"])
    pending = state_of(storage, RAINIER).thumbs_pending
    assert [(p["frame_key"], p["thumb_key"]) for p in pending] == [(out["key"], out["thumb_key"])]
    storage.refuse = ()
    clock.advance(60)  # not due yet: the repair needs no poll and no request
    before = len(fake_nps.requests)
    assert runner(storage, fake_nps, clock).run_pass([RAINIER])["counts"] == {"not_due": 1}
    assert len(fake_nps.requests) == before
    doc = storage.read_json(out["thumb_key"][:-4] + ".json")
    assert doc["thumb_made_from"] == "stored_frame" and doc["variant"] == "thumb"
    assert doc["stored_width"] == 640 and doc["full_frame"]["key"] == out["key"]
    assert state_of(storage, RAINIER).thumbs_pending == []
    assert [e["event"] for e in events(data_root)].count("thumb_repaired") == 1


def test_a_crash_between_frame_and_thumbnail_still_ends_with_both(data_root, clock, fake_nps):
    storage = CrashingStorage(data_root, "thumbs/")
    set_rainier(fake_nps, clock.now(), etag='"c0"')
    with pytest.raises(Crash):
        runner(storage, fake_nps, clock).run_pass([RAINIER])
    assert len(jpgs(storage, "frames/")) == 1 and not jpgs(storage, "thumbs/")
    for i in range(1, 5):  # the camera moves on; a fresh process every pass
        clock.advance(300)
        set_rainier(fake_nps, clock.now(), etag=f'"c{i}"', pattern=i)
        runner(storage, fake_nps, clock).run_pass([RAINIER])
    frames, thumbs = jpgs(storage, "frames/"), jpgs(storage, "thumbs/")
    assert len(frames) >= 2  # 720 s pacing: one more GET in these 20 min
    assert [k.replace("frames/", "", 1) for k in frames] == [
        k.replace("thumbs/", "", 1) for k in thumbs
    ]


def test_a_missing_frame_or_a_frame_that_will_not_decode_is_given_up(data_root, clock, fake_nps):
    storage = RefusingStorage(data_root, ("thumbs/",))
    set_rainier(fake_nps, clock.now(), etag='"g1"')
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    storage.refuse = ()
    storage.path(out["key"]).write_bytes(b"\xff\xd8 not a jpeg any more")
    for _ in range(3):  # three tries, then it is dropped and logged
        clock.advance(30)
        runner(storage, fake_nps, clock).run_pass([RAINIER])
    assert state_of(storage, RAINIER).thumbs_pending == []
    given_up = [e for e in events(data_root) if e["event"] == "thumb_given_up"]
    assert len(given_up) == 1 and given_up[0]["reason"] == "attempts"
    assert not storage.exists(out["thumb_key"])


def test_a_thumbnail_left_without_its_sidecar_keeps_its_bytes_and_gets_one(
    data_root, clock, fake_nps
):
    storage = CrashingStorage(data_root, "thumbs/", ".json")  # dies after the thumbnail JPEG
    set_rainier(fake_nps, clock.now(), etag='"s0"')
    with pytest.raises(Crash):
        runner(storage, fake_nps, clock).run_pass([RAINIER])
    (thumb,) = jpgs(storage, "thumbs/")
    made = storage.get_bytes(thumb)
    assert not storage.exists(thumb[:-4] + ".json")
    clock.advance(300)
    set_rainier(fake_nps, clock.now(), etag='"s1"', pattern=1)
    runner(storage, fake_nps, clock).run_pass([RAINIER])
    doc = storage.read_json(thumb[:-4] + ".json")
    assert storage.get_bytes(thumb) == made  # never rewritten
    assert doc["thumb_made_from"] == "live_capture" and doc["stored_bytes"] == len(made)
    assert doc["full_frame"]["key"] == "frames/" + thumb[len("thumbs/") :]
