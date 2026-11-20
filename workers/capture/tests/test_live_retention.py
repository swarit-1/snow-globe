"""PRD §8.7 storage through live capture: thumbnails for every download, every frame kept at the
two design-P cameras, the frame nearest each UTC hour at the others, crash recovery, S3
staging, and one capture_log write per pass in S3 mode (the infra cost finding)."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from lookout_common.manifest import load_manifests
from lookout_common.storage import LocalStorage, S3Storage

from lookout_capture.live import status_report
from lookout_capture.retention import hour_slot, key_stamp
from lookout_capture.state import CameraState, camera_state_key

from .conftest import FakeClock, FakeNPS, make_jpeg
from .test_live import events, runner, set_ard, set_rainier

VPS = load_manifests()
GRCA = VPS["grand-canyon.yavapai"]
PORE = VPS["point-reyes.beach"]
RAINIER = VPS["rainier.paradise-mountain"]
ACAD = VPS["acadia.mcfarland"]
ZONES = {"grca": ("America/Phoenix", "MST"), "pore": ("America/Los_Angeles", "PST")}


class ArdFeed:
    """A fake ARD air cam: a new image every 15 min, on the feed 6.5 min after its time."""

    def __init__(self, fake: FakeNPS, vp, code: str, *, width: int = 1280) -> None:
        self.fake, self.vp, self.code, self.width = fake, vp, code, width
        self.current: datetime | None = None
        self.published: list[datetime] = []

    def update(self, now: datetime) -> None:
        latest = now - timedelta(minutes=6, seconds=30)
        slot = latest.replace(minute=latest.minute - latest.minute % 15, second=0, microsecond=0)
        if slot == self.current:
            return
        self.current = slot
        self.published.append(slot)
        zone, abbr = ZONES[self.code]
        local = slot.astimezone(ZoneInfo(zone))
        set_ard(
            self.fake,
            self.vp,
            imagedate=f"Updated {local:%m/%d/%Y %I:%M %p} {abbr}",
            image=make_jpeg(
                self.width,
                self.width * 9 // 16,
                pattern=len(self.published),
                exif_datetime=local.strftime("%Y:%m:%d %H:%M:%S"),
            ),
            image_lm=slot + timedelta(minutes=6, seconds=30),
            image_etag=f'"{self.code}-{slot:%H%M}"',
            code=self.code,
        )


def jpgs(storage, prefix: str) -> list[str]:
    return [k for k in storage.list_prefix(prefix) if k.endswith(".jpg")]


def run_hours(storage, fake, clock, feed: ArdFeed, vp, *, passes: int, **options) -> list[set]:
    """5-minute passes; returns the frames/ keys seen after each pass."""
    seen = []
    for _ in range(passes):
        feed.update(clock.now())
        runner(storage, fake, clock, **options).run_pass([vp])
        seen.append(set(jpgs(storage, "frames/")))
        clock.advance(300)
    return seen


# --- hourly cameras --------------------------------------------------------------------------


def test_hourly_camera_keeps_the_frame_nearest_each_hour_and_a_thumb_of_every_download(
    data_root, fake_nps
):
    storage = LocalStorage(data_root)
    clock = FakeClock(datetime(2026, 11, 19, 12, 1, 30, tzinfo=UTC))
    fake_nps.clock = clock
    feed = ArdFeed(fake_nps, GRCA, "grca")
    seen = run_hours(storage, fake_nps, clock, feed, GRCA, passes=40)  # 3 h 20 min
    # frames/ only ever grows: nothing under it is deleted, ever.
    for before, after in itertools.pairwise(seen):
        assert before <= after
    stored = [e for e in events(data_root) if e["event"] == "stored"]
    thumbs = jpgs(storage, "thumbs/")
    assert len(stored) == len(thumbs) >= 12
    assert {e["key"] for e in stored} == set(thumbs)  # the durable key is the thumbnail
    assert all(e["frame_policy"] == "hourly-nearest" and e["staged_key"] for e in stored)
    side = storage.read_json(thumbs[0][:-4] + ".json")
    assert (
        side["variant"] == "thumb" and side["stored_width"] == 640 and side["stored_height"] == 360
    )
    assert side["stored"] == "thumb-640" and side["full_frame"]["policy"] == "hourly-nearest"
    assert side["full_frame"]["stored_width"] == 1280 and side["json_imagedate_raw"]
    # One frame per decided hour, the downloaded one nearest the hour.
    frames = jpgs(storage, "frames/")
    downloaded = sorted(key_stamp(k) for k in thumbs)
    decided = {hour_slot(t) for t in downloaded if clock.now() >= hour_slot(t) + timedelta(hours=1)}
    assert {hour_slot(key_stamp(k)) for k in frames} == decided and len(frames) == len(decided)
    for key in frames:
        hour = hour_slot(key_stamp(key))
        best = min(
            (t for t in downloaded if hour_slot(t) == hour), key=lambda t: (abs(t - hour), t)
        )
        assert key_stamp(key) == best
        doc = storage.read_json(key[:-4] + ".json")
        assert doc["hourly_selection"]["hour_utc"] == hour.isoformat().replace("+00:00", "Z")
        assert doc["frame_policy"] == "hourly-nearest" and doc["thumb"]["key"].startswith("thumbs/")
    # Staging holds only the hours not yet decided.
    staged = jpgs(storage, "cache/staging/")
    assert staged and all(
        clock.now() < hour_slot(key_stamp(k)) + timedelta(hours=1) for k in staged
    )
    promoted = [e for e in events(data_root) if e["event"] == "promoted"]
    assert len(promoted) == len(frames)
    state = CameraState.from_dict(storage.read_json(camera_state_key(GRCA.id)))
    assert {e["key"] for e in state.staged} == set(staged)
    assert state.last_promotion and state.last_promotion["key"] in frames


def test_staging_survives_a_lost_state_and_a_restart(data_root, fake_nps):
    storage = LocalStorage(data_root)
    clock = FakeClock(datetime(2026, 11, 19, 12, 1, 30, tzinfo=UTC))
    fake_nps.clock = clock
    feed = ArdFeed(fake_nps, GRCA, "grca")
    run_hours(storage, fake_nps, clock, feed, GRCA, passes=6)
    staged = jpgs(storage, "cache/staging/")
    assert staged and not jpgs(storage, "frames/")
    # The process dies after staging but before the state remembers it: forget the index.
    doc = storage.read_json(camera_state_key(GRCA.id))
    doc["staged"] = []
    storage.write_json(camera_state_key(GRCA.id), doc)
    clock.advance(3 * 3600)  # the laptop sleeps; a fresh runner starts afterwards
    feed.update(clock.now())
    runner(storage, fake_nps, clock).run_pass([GRCA])
    frames = jpgs(storage, "frames/")
    assert {hour_slot(key_stamp(k)) for k in frames} == {hour_slot(key_stamp(k)) for k in staged}
    assert not set(staged) & set(jpgs(storage, "cache/staging/"))


def test_hourly_cameras_still_decide_hours_while_the_host_is_backing_off(data_root, fake_nps):
    storage = LocalStorage(data_root)
    clock = FakeClock(datetime(2026, 11, 19, 12, 1, 30, tzinfo=UTC))
    fake_nps.clock = clock
    feed = ArdFeed(fake_nps, GRCA, "grca")
    run_hours(storage, fake_nps, clock, feed, GRCA, passes=4)
    clock.advance(3600)
    storage.write_json(
        "state/capture/hosts/www.nps.gov.json",
        {"not_before": (clock.now() + timedelta(hours=1)).isoformat()},
    )
    before = len(fake_nps.requests)
    out = runner(storage, fake_nps, clock).run_pass([GRCA])["outcomes"][GRCA.id]
    assert out["event"] == "skipped" and out["reason"] == "host_backoff"
    assert len(fake_nps.requests) == before and jpgs(storage, "frames/")


def test_old_policy_frames_stay_and_their_hours_are_not_kept_twice(data_root, fake_nps):
    storage = LocalStorage(data_root)
    clock = FakeClock(datetime(2026, 11, 19, 12, 1, 30, tzinfo=UTC))
    fake_nps.clock = clock
    old = "frames/grand-canyon.yavapai/2026-11-19/20261119T114500Z_000000000000.jpg"
    storage.put_bytes(old, b"stored by the frozen older capture")
    feed = ArdFeed(fake_nps, GRCA, "grca")
    run_hours(storage, fake_nps, clock, feed, GRCA, passes=16)
    hours = [hour_slot(key_stamp(k)) for k in jpgs(storage, "frames/")]
    assert len(hours) == len(set(hours))  # never two frames for one hour
    assert storage.get_bytes(old) == b"stored by the frozen older capture"
    dropped = [e for e in events(data_root) if e["event"] == "staging_dropped"]
    assert dropped and dropped[0]["reason"] == "hour_already_kept"


# --- every-frame cameras ---------------------------------------------------------------------


def test_design_p_cameras_keep_every_frame_and_a_thumbnail_of_each(data_root, fake_nps):
    storage = LocalStorage(data_root)
    clock = FakeClock(datetime(2026, 11, 19, 12, 1, 30, tzinfo=UTC))
    fake_nps.clock = clock
    feed = ArdFeed(fake_nps, PORE, "pore", width=3840)
    run_hours(storage, fake_nps, clock, feed, PORE, passes=14)
    frames, thumbs = jpgs(storage, "frames/"), jpgs(storage, "thumbs/")
    assert len(frames) == len(thumbs) >= 4
    assert [k.replace("frames/", "", 1) for k in frames] == [
        k.replace("thumbs/", "", 1) for k in thumbs
    ]
    assert not storage.list_prefix("cache/staging/")
    side = storage.read_json(frames[0][:-4] + ".json")
    assert side["frame_policy"] == "every" and side["stored_width"] == 1920
    assert side["thumb"]["key"] == thumbs[0] and side["thumb"]["stored_width"] == 640
    tside = storage.read_json(thumbs[0][:-4] + ".json")
    assert tside["full_frame"]["key"] == frames[0] and tside["full_frame"]["stored_width"] == 1920
    assert tside["sha256"] == side["sha256"] and tside["stored_bytes"] < side["stored_bytes"]
    stored = [e for e in events(data_root) if e["event"] == "stored"]
    assert {e["key"] for e in stored} == set(frames)


def test_an_undecodable_download_gets_no_thumbnail(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    fake_nps.set(
        RAINIER.truth_camera.image_url,
        b"\xff\xd8\xff\xe0" + b"\x00" * 64,
        last_modified=clock.now(),
        etag='"bad"',
    )
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "stored" and "decode_failed" in out["flags"]
    assert out["thumb_key"] is None and not storage.list_prefix("thumbs/")
    assert storage.exists(out["key"])


def test_status_reports_policy_staging_and_the_last_decision(data_root, fake_nps):
    storage = LocalStorage(data_root)
    clock = FakeClock(datetime(2026, 11, 19, 12, 1, 30, tzinfo=UTC))
    fake_nps.clock = clock
    feed = ArdFeed(fake_nps, GRCA, "grca")
    run_hours(storage, fake_nps, clock, feed, GRCA, passes=20)
    report = status_report(storage, [GRCA, PORE], now=clock.now())
    rows = {r["viewpoint_id"]: r for r in report["cameras"]}
    assert rows[GRCA.id]["frame_policy"] == "hourly-nearest" and rows[GRCA.id]["staged"] >= 1
    assert rows[GRCA.id]["last_promotion"]["key"].startswith("frames/grand-canyon.yavapai/")
    assert report["events_today"]["capture:promoted"] >= 1


# --- S3 ---------------------------------------------------------------------------------------


def test_s3_staging_is_promoted_and_deleted_and_listed_only_now_and_then(
    s3_bucket, fake_nps, data_root
):
    storage = S3Storage("lookout-test", "dev", client=s3_bucket)
    clock = FakeClock(datetime(2026, 11, 19, 12, 1, 30, tzinfo=UTC))
    fake_nps.clock = clock
    listed: list[str] = []
    real_list = storage.list_prefix

    def recording(prefix: str = "") -> list[str]:
        listed.append(prefix)
        return real_list(prefix)

    storage.list_prefix = recording  # type: ignore[method-assign]
    feed = ArdFeed(fake_nps, ACAD, "grca")  # Acadia's manifest, Grand Canyon's fixture feed
    run_hours(storage, fake_nps, clock, feed, ACAD, passes=24)  # 2 hours
    staging_lists = [p for p in listed if p.startswith("cache/staging/")]
    assert staging_lists == ["cache/staging/frames/acadia.mcfarland/"]  # first run only
    frames = jpgs(storage, "frames/")
    assert frames and all(k.startswith("frames/acadia.mcfarland/") for k in frames)
    staged = [k for k in real_list("cache/staging/") if k.endswith(".jpg")]
    assert len(staged) <= 8
    for key in frames:  # every promoted hour's staged copies were deleted from S3
        hour = hour_slot(key_stamp(key))
        assert not [k for k in staged if hour_slot(key_stamp(k)) == hour]
    assert not [k for k in real_list("state/locks/")]
    clock.advance(6 * 3600)
    runner(storage, fake_nps, clock).run_pass([ACAD])
    assert [p for p in listed if p.startswith("cache/staging/")][-1] == staging_lists[0]
    assert len([p for p in listed if p.startswith("cache/staging/")]) == 2


class CountingS3:
    """Wraps a boto3 S3 client and counts the calls that touch capture_log/."""

    def __init__(self, client) -> None:
        self._client = client
        self.log_puts = 0
        self.log_gets = 0

    def put_object(self, **kwargs):
        if "capture_log/" in kwargs.get("Key", ""):
            self.log_puts += 1
        return self._client.put_object(**kwargs)

    def get_object(self, **kwargs):
        if "capture_log/" in kwargs.get("Key", ""):
            self.log_gets += 1
        return self._client.get_object(**kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


def test_s3_mode_writes_each_pass_log_in_one_append(s3_bucket, clock, fake_nps, data_root):
    counting = CountingS3(s3_bucket)
    storage = S3Storage("lookout-test", "dev", client=counting)
    many: list[int] = []
    singles: list[dict] = []
    real_many = storage.append_jsonl_many

    def spy_many(key, records):
        rows = list(records)
        many.append(len(rows))
        return real_many(key, rows)

    storage.append_jsonl_many = spy_many  # type: ignore[method-assign]
    storage.append_jsonl = lambda key, record: singles.append(record)  # type: ignore[method-assign]
    set_rainier(fake_nps, clock.now() - timedelta(seconds=30), etag='"b1"')
    shot = clock.now() - timedelta(minutes=10)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/19/2026 12:00 AM PST",
        image=make_jpeg(
            exif_datetime=shot.astimezone(ZoneInfo(PORE.time_zone)).strftime("%Y:%m:%d %H:%M:%S")
        ),
        image_lm=shot,
        image_etag='"p1"',
    )
    summary = runner(storage, fake_nps, clock).run_pass([RAINIER, PORE, GRCA])
    assert summary["counts"]["stored"] == 2
    assert singles == [] and len(many) == 1  # one write for the whole pass
    assert counting.log_puts == 1 and counting.log_gets == 1
    lines = storage.get_bytes("capture_log/2026-11-19.jsonl").decode().splitlines()
    kinds = [json.loads(line)["event"] for line in lines]
    assert kinds[-1] == "pass" and kinds.count("request") == 5 and kinds.count("stored") == 2
    assert many == [len(lines)]
    times = [json.loads(line)["t"] for line in lines]
    assert times == sorted(times)  # records keep their own times and order
    clock.advance(400)
    runner(storage, fake_nps, clock).run_pass([RAINIER, PORE, GRCA])
    assert len(many) == 2 and counting.log_puts == 2


def test_local_mode_appends_each_record_at_once(data_root, clock, fake_nps, monkeypatch):
    storage = LocalStorage(data_root)
    singles: list[str] = []
    real = storage.append_jsonl

    def spy(key, record):
        singles.append(record["event"])
        return real(key, record)

    monkeypatch.setattr(storage, "append_jsonl", spy)
    set_rainier(fake_nps, clock.now(), etag='"l1"')
    runner(storage, fake_nps, clock).run_pass([RAINIER])
    assert singles == ["request", "request", "stored", "pass"]


def test_a_failed_batched_log_write_never_stops_capture(s3_bucket, clock, fake_nps, data_root):
    storage = S3Storage("lookout-test", "dev", client=s3_bucket)

    def broken(key, records):
        raise RuntimeError("S3 is down")

    storage.append_jsonl_many = broken  # type: ignore[method-assign]
    set_rainier(fake_nps, clock.now(), etag='"f1"')
    run = runner(storage, fake_nps, clock)
    out = run.run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "stored" and run.log_errors == 1 and "S3 is down" in run.last_log_error


@pytest.mark.parametrize("mode", ["always", "never"])
def test_log_batching_can_be_forced(data_root, clock, fake_nps, mode):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"m1"')
    run = runner(storage, fake_nps, clock, batch_logs=mode)
    run.run_pass([RAINIER])
    assert [e["event"] for e in events(data_root)] == ["request", "request", "stored", "pass"]
    assert run._log_buffer is None


def test_records_a_failed_batch_could_not_write_go_out_with_the_next_pass(
    s3_bucket, clock, fake_nps, data_root, capsys
):
    storage = S3Storage("lookout-test", "dev", client=s3_bucket)
    real = storage.append_jsonl_many
    calls = {"n": 0}

    def flaky(key, records):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("S3 is down")
        return real(key, records)

    storage.append_jsonl_many = flaky  # type: ignore[method-assign]
    set_rainier(fake_nps, clock.now(), etag='"k1"')
    run = runner(storage, fake_nps, clock)
    assert run.run_pass([RAINIER])["outcomes"][RAINIER.id]["event"] == "stored"
    assert run.log_errors == 1
    printed = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [p["event"] for p in printed] == ["request", "request", "stored", "pass"]
    assert all(p["capture_log_unwritten"] == "capture_log/2026-11-19.jsonl" for p in printed)
    clock.advance(400)
    run.run_pass([RAINIER])  # the same runner (the CLI loop): the first pass's records go first
    lines = storage.get_bytes("capture_log/2026-11-19.jsonl").decode().splitlines()
    kinds = [json.loads(line)["event"] for line in lines]
    assert kinds[:4] == ["request", "request", "stored", "pass"] and kinds[-1] == "pass"
    assert kinds.count("request") == 3 and run.log_records_dropped == 0


def test_a_pass_near_its_deadline_writes_its_records_before_each_camera(
    s3_bucket, clock, fake_nps, data_root
):
    storage = S3Storage("lookout-test", "dev", client=s3_bucket)
    batches: list[list[str]] = []
    real = storage.append_jsonl_many

    def spy(key, records):
        rows = list(records)
        batches.append([r["event"] for r in rows])
        return real(key, rows)

    storage.append_jsonl_many = spy  # type: ignore[method-assign]
    set_rainier(fake_nps, clock.now(), etag='"d1"')
    shot = clock.now() - timedelta(minutes=10)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/19/2026 12:00 AM PST",
        image=make_jpeg(
            exif_datetime=shot.astimezone(ZoneInfo(PORE.time_zone)).strftime("%Y:%m:%d %H:%M:%S")
        ),
        image_lm=shot,
        image_etag='"p1"',
    )
    run = runner(storage, fake_nps, clock)
    run.run_pass([RAINIER, PORE], time_left=lambda: 45.0, min_time_left_s=25.0)
    # 45 s left is under 2 x 25 s: the first camera's records went out before the second.
    assert len(batches) == 2 and batches[1][-1] == "pass" and "pass" not in batches[0]
    assert sum(len(b) for b in batches) == len(
        storage.get_bytes("capture_log/2026-11-19.jsonl").decode().splitlines()
    )
