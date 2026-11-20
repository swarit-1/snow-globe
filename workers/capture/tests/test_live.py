"""Live capture: fetch-on-change, the hourly cap across restarts, seeding from the stopgap,
times, downscaling, dedupe, back-off, concurrency, S3 (moto) and the Lambda handler."""

from __future__ import annotations

import itertools
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from lookout_common.http import polite_client
from lookout_common.images import exif_summary
from lookout_common.jsonl import read_jsonl
from lookout_common.manifest import load_manifests
from lookout_common.storage import LocalStorage, S3Storage

from lookout_capture import lambda_handler
from lookout_capture.live import (
    MIN_POLL_INTERVAL_S,
    POLL_MARGIN_S,
    CaptureOptions,
    CaptureRunner,
    parse_feed,
    status_report,
)
from lookout_capture.state import CameraState, camera_state_key

from .conftest import FakeClock, FakeNPS, fixture_text, make_jpeg

VPS = load_manifests()
RAINIER = VPS["rainier.paradise-mountain"]
PORE = VPS["point-reyes.beach"]
GRCA = VPS["grand-canyon.yavapai"]
ACAD = VPS["acadia.mcfarland"]
#: One poll spacing as the scheduler applies it: 5 minutes plus the margin (POLL_MARGIN_S).
POLL_STEP_S = MIN_POLL_INTERVAL_S + POLL_MARGIN_S


def runner(storage, fake: FakeNPS, clock: FakeClock, **options) -> CaptureRunner:
    client = polite_client(transport=fake.transport())
    return CaptureRunner(
        storage,
        client,
        clock=clock.now,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        options=CaptureOptions(**options),
    )


def local_exif(moment: datetime, zone: str) -> str:
    return moment.astimezone(ZoneInfo(zone)).strftime("%Y:%m:%d %H:%M:%S")


def set_ard(
    fake: FakeNPS,
    vp,
    *,
    imagedate: str,
    image: bytes,
    image_lm: datetime,
    image_etag: str,
    code: str = "pore",
    json_lm: datetime | None = None,
) -> None:
    feed = fixture_text(f"{code}json.txt")
    original = json.loads(feed)["imagedate"]
    body = feed.replace(original, imagedate).encode()
    fake.set(
        vp.truth_camera.json_url,
        body,
        content_type="text/plain",
        last_modified=json_lm or image_lm,
        etag=f'"j{abs(hash(imagedate)) % 10**8}"',
    )
    fake.set(vp.truth_camera.image_url, image, last_modified=image_lm, etag=image_etag)


def set_rainier(
    fake: FakeNPS, moment: datetime, *, etag: str, pattern: int = 0, width: int = 1920
) -> bytes:
    image = make_jpeg(
        width,
        width * 9 // 16,
        pattern=pattern,
        make="AXIS",
        model="P1387",
        exif_datetime=local_exif(moment, RAINIER.time_zone),
        tz_offset_hours=-8,
        subsec="431",
    )
    fake.set(
        RAINIER.truth_camera.image_url,
        image,
        last_modified=moment + timedelta(seconds=14),
        etag=etag,
    )
    return image


def events(root: Path, day: str = "2026-11-19") -> list[dict]:
    path = root / "capture_log" / f"{day}.jsonl"
    return read_jsonl(path) if path.exists() else []


def image_gets(fake: FakeNPS, url: str) -> list[datetime]:
    return [r["t"] for r in fake.requests if r["method"] == "GET" and r["url"] == url]


# --- feed parsing ----------------------------------------------------------------------------


def test_parse_feed_reads_json_and_falls_back_to_regex():
    assert parse_feed(fixture_text("porejson.txt"))["imagedate"].endswith("PST")
    broken = '{ "name":"x", "imagedate":"Updated 11/18/2026 10:00 PM PST", "bad": , }'
    parsed = parse_feed(broken)
    assert parsed["imagedate"] == "Updated 11/18/2026 10:00 PM PST"
    assert parsed["_parsed_by"] == "regex"


# --- ARD air cams ------------------------------------------------------------------------------


def test_ard_change_fetch_store_downscale_and_times(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    shot = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)  # 10:00 PM PST
    image = make_jpeg(
        3840,
        2160,
        exif_datetime=local_exif(shot, PORE.time_zone),
        artist="National Park Service",
        software="ARS_Digicam",
    )
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/18/2026 10:00 PM PST",
        image=image,
        image_lm=shot + timedelta(minutes=15, seconds=24),
        image_etag='"img-1"',
    )
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "stored", out
    assert (
        out["image_time_utc"] == "2026-11-19T06:00:00Z" and out["image_time_source"] == "exif_local"
    )
    assert out["disagreement_min"]["exif_minus_json_imagedate"] == 0.0
    assert out["disagreement_min"]["http_last_modified_minus_exif"] == 15.4
    key = out["key"]
    assert key.startswith("frames/point-reyes.beach/2026-11-19/20261119T060000Z_")
    side = storage.read_json(key[:-4] + ".json")
    assert (side["original_width"], side["stored_width"], side["stored_height"]) == (
        3840,
        1920,
        1080,
    )
    assert side["stored"] == "downscaled-1920" and side["jpeg_quality"] == 90
    assert side["sha256"] != side["stored_sha256"]
    assert side["json_imagedate_raw"] == "Updated 11/18/2026 10:00 PM PST"
    assert side["json_imagedate_utc"] == "2026-11-19T06:00:00Z"
    assert side["exif"]["Artist"] == "National Park Service" and side["source"] == "live"
    assert side["user_agent"].startswith("Lookout/0.1") and len(side["dhash64"]) == 16
    assert side["luma_mean"] > 0 and side["fetched_at_utc"].endswith("Z")
    stored = exif_summary(storage.get_bytes(key))  # EXIF bytes survive the re-encode
    assert stored["datetime"] == "2026:11:18 22:00:00" and stored["width"] == 1920
    logged = [e for e in events(data_root) if e.get("viewpoint_id")]
    assert [e["event"] for e in logged if e["event"] != "request"] == ["stored"]
    # Every nps.gov request is logged: one feed GET, one image GET.
    requests = [(e["stage"], e["method"], e["status"]) for e in logged if e["event"] == "request"]
    assert requests == [("json", "GET", 200), ("image", "GET", 200)]
    assert len(requests) == len(fake_nps.requests)
    # The User-Agent names the project, never a person; no cookies are sent.
    assert {r["headers"]["user-agent"] for r in fake_nps.requests} == {
        "Lookout/0.1 (webcam research capture; fetch-on-change)"
    }
    assert not any("cookie" in r["headers"] for r in fake_nps.requests)


def test_ard_unchanged_imagedate_or_304_never_fetches_the_image(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    shot = datetime(2026, 11, 19, 7, 0, tzinfo=UTC)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/18/2026 11:00 PM PST",
        image=make_jpeg(exif_datetime=local_exif(shot, PORE.time_zone)),
        image_lm=shot + timedelta(minutes=15),
        image_etag='"i1"',
    )
    runner(storage, fake_nps, clock).run_pass([PORE])
    assert len(image_gets(fake_nps, PORE.truth_camera.image_url)) == 1
    # Same JSON validators: the server answers 304; then a new JSON with the same imagedate.
    clock.advance(POLL_STEP_S)
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out == {"event": "unchanged", "reason": "json_not_modified"}
    feed = fake_nps.resources[PORE.truth_camera.json_url]
    feed.etag, feed.last_modified = '"j-other"', shot + timedelta(minutes=30)
    clock.advance(POLL_STEP_S)
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "unchanged" and out["reason"] == "imagedate_same"
    assert len(image_gets(fake_nps, PORE.truth_camera.image_url)) == 1
    json_requests = [r for r in fake_nps.requests if r["url"] == PORE.truth_camera.json_url]
    assert "if-none-match" in json_requests[1]["headers"]


def test_poll_interval_is_never_under_five_minutes(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now() - timedelta(minutes=1), etag='"a"')
    runner(storage, fake_nps, clock).run_pass([RAINIER])
    before = len(fake_nps.requests)
    first_poll = fake_nps.requests[0]["t"]
    clock.current = first_poll + timedelta(seconds=299)
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "not_due" and len(fake_nps.requests) == before
    # Exactly 5 minutes is still inside the scheduling margin: not due.
    clock.current = first_poll + timedelta(seconds=MIN_POLL_INTERVAL_S)
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "not_due" and len(fake_nps.requests) == before
    clock.current = first_poll + timedelta(seconds=POLL_STEP_S)
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "unchanged" and len(fake_nps.requests) == before + 1


@pytest.mark.parametrize(
    ("vp", "code", "when", "imagedate", "expected"),
    [
        (
            PORE,
            "pore",
            datetime(2026, 11, 19, 6, 0, tzinfo=UTC),
            "Updated 11/18/2026 10:00 PM PST",
            "2026-11-19T06:00:00Z",
        ),
        (
            PORE,
            "pore",
            datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
            "Updated 07/15/2026 02:30 PM PDT",
            "2026-07-15T21:30:00Z",
        ),
        (
            GRCA,
            "grca",
            datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
            "Updated 07/15/2026 02:30 PM MST",
            "2026-07-15T21:30:00Z",
        ),
        (
            ACAD,
            "acad",
            datetime(2026, 11, 19, 6, 0, tzinfo=UTC),
            "Updated 11/19/2026 01:00 AM EST",
            "2026-11-19T06:00:00Z",
        ),
    ],
)
def test_imagedate_and_exif_agree_across_zones(
    data_root, fake_nps, vp, code, when, imagedate, expected
):
    storage = LocalStorage(data_root)
    clock = FakeClock(when + timedelta(minutes=20))
    fake_nps.clock = clock
    image = make_jpeg(exif_datetime=local_exif(when, vp.time_zone))
    set_ard(
        fake_nps,
        vp,
        imagedate=imagedate,
        image=image,
        image_lm=when + timedelta(minutes=15),
        image_etag='"z"',
        code=code,
    )
    out = runner(storage, fake_nps, clock).run_pass([vp])["outcomes"][vp.id]
    assert out["event"] == "stored", out
    assert out["image_time_utc"] == expected
    side = storage.read_json(out["key"][:-4] + ".json")
    assert side["json_imagedate_utc"] == expected
    assert side["image_time_disagreement_min"]["exif_minus_json_imagedate"] == 0.0


def test_exif_on_standard_time_in_summer_is_overruled_by_imagedate(data_root, fake_nps):
    storage = LocalStorage(data_root)
    when = datetime(2026, 7, 15, 21, 30, tzinfo=UTC)  # 2:30 PM PDT
    clock = FakeClock(when + timedelta(minutes=20))
    fake_nps.clock = clock
    image = make_jpeg(exif_datetime="2026:07:15 13:30:00")  # camera clock on PST
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 07/15/2026 02:30 PM PDT",
        image=image,
        image_lm=when + timedelta(minutes=15),
        image_etag='"s"',
    )
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["image_time_utc"] == "2026-07-15T21:30:00Z"
    assert out["image_time_source"] == "json_imagedate"
    assert out["disagreement_min"]["exif_minus_json_imagedate"] == -60.0
    assert "exif_time_overruled" in out["flags"]


def test_dedupe_by_304_and_by_sha(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    shot = datetime(2026, 11, 19, 7, 0, tzinfo=UTC)
    image = make_jpeg(exif_datetime=local_exif(shot, PORE.time_zone))
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/18/2026 11:00 PM PST",
        image=image,
        image_lm=shot,
        image_etag='"same"',
    )
    runner(storage, fake_nps, clock).run_pass([PORE])
    # New imagedate, same image and ETag: the conditional GET gets 304 (stale feed).
    clock.advance(900)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/18/2026 11:15 PM PST",
        image=image,
        image_lm=shot,
        image_etag='"same"',
    )
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "duplicate" and out["reason"] == "not_modified"
    assert "stale_feed" in out["flags"]
    # New imagedate, same bytes under a new ETag: downloaded, recognised by SHA-256.
    clock.advance(900)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/18/2026 11:30 PM PST",
        image=image,
        image_lm=shot + timedelta(minutes=30),
        image_etag='"renamed"',
    )
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "duplicate" and out["reason"] == "same_sha256"
    assert {"duplicate_sha", "stale_feed"} <= set(out["flags"])
    frames = [k for k in storage.list_prefix("frames/") if k.endswith(".jpg")]
    assert len(frames) == 1
    # The imagedate is settled: the next poll does not fetch again.
    clock.advance(900)
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "unchanged"


def test_capped_imagedate_is_fetched_later_even_if_the_feed_then_answers_304(
    data_root, clock, fake_nps
):
    storage = LocalStorage(data_root)
    shot = datetime(2026, 11, 19, 7, 0, tzinfo=UTC)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/18/2026 11:00 PM PST",
        image=make_jpeg(exif_datetime=local_exif(shot, PORE.time_zone)),
        image_lm=shot,
        image_etag='"a"',
    )
    runner(storage, fake_nps, clock).run_pass([PORE])
    later = shot + timedelta(minutes=15)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/18/2026 11:15 PM PST",
        image=make_jpeg(exif_datetime=local_exif(later, PORE.time_zone), pattern=3),
        image_lm=later,
        image_etag='"b"',
    )
    clock.advance(POLL_STEP_S)
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "rate_capped" and out["reason"] == "spacing"
    clock.advance(POLL_STEP_S)  # the feed is unchanged now: 304, but the new image is still owed
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "rate_capped"
    clock.advance(POLL_STEP_S)
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "stored" and out["image_time_utc"] == "2026-11-19T07:15:00Z"
    statuses = [r for r in fake_nps.requests if r["url"] == PORE.truth_camera.json_url]
    assert len(statuses) == 4


def test_park_duplicate_of_an_older_frame_is_settled(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    first = set_rainier(fake_nps, clock.now(), etag='"a"', pattern=1)
    runner(storage, fake_nps, clock).run_pass([RAINIER])
    clock.advance(900)
    set_rainier(fake_nps, clock.now(), etag='"b"', pattern=2)
    runner(storage, fake_nps, clock).run_pass([RAINIER])
    clock.advance(900)
    fake_nps.set(RAINIER.truth_camera.image_url, first, last_modified=clock.now(), etag='"c"')
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "duplicate" and out["reason"] == "same_sha256"
    clock.advance(900)
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "unchanged"
    assert len(image_gets(fake_nps, RAINIER.truth_camera.image_url)) == 3


def test_decode_failure_and_dimension_change_are_flagged(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    shot = datetime(2026, 11, 19, 7, 0, tzinfo=UTC)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/18/2026 11:00 PM PST",
        image=make_jpeg(640, 360, exif_datetime=local_exif(shot, PORE.time_zone)),
        image_lm=shot,
        image_etag='"a"',
    )
    runner(storage, fake_nps, clock).run_pass([PORE])
    clock.advance(900)
    later = shot + timedelta(minutes=15)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/18/2026 11:15 PM PST",
        image=make_jpeg(800, 600, exif_datetime=local_exif(later, PORE.time_zone)),
        image_lm=later,
        image_etag='"b"',
    )
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert "dimensions_changed" in out["flags"]
    clock.advance(900)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/18/2026 11:30 PM PST",
        image=b"\xff\xd8\xff\xe0" + b"\x00" * 50,
        image_lm=later + timedelta(minutes=15),
        image_etag='"c"',
    )
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "stored" and "decode_failed" in out["flags"]
    assert out["image_time_source"] == "json_imagedate"


def test_non_jpeg_body_is_an_error_not_a_frame(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/18/2026 11:00 PM PST",
        image=b"<html>offline</html>",
        image_lm=clock.now(),
        image_etag='"h"',
    )
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "error" and "not a JPEG" in out["error"]
    assert not [k for k in storage.list_prefix("frames/") if k.endswith(".jpg")]


# --- the hourly cap, pacing and restarts --------------------------------------------------------


def test_cap_holds_across_restarts_for_a_camera_changing_every_5_min(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    url = RAINIER.truth_camera.image_url
    for i in range(30):  # 2.5 hours of 5-minute polls, a new runner (restart) every pass
        set_rainier(fake_nps, clock.now() - timedelta(seconds=30), etag=f'"e{i}"', pattern=i)
        runner(storage, fake_nps, clock).run_pass([RAINIER])
        clock.advance(POLL_STEP_S)
    gets = image_gets(fake_nps, url)
    for g in gets:  # any rolling hour
        assert sum(1 for h in gets if g - timedelta(seconds=3600) < h <= g) <= 4
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(gets)]
    assert min(gaps) >= 720  # pacing: frames spread through the hour
    assert 9 <= len(gets) <= 11
    heads = [r for r in fake_nps.requests if r["method"] == "HEAD"]
    assert len(heads) == 30
    capped = [e for e in events(data_root) if e["event"] == "rate_capped"]
    assert capped and all(e["reason"] in {"spacing", "hourly_cap"} for e in capped)


def test_get_is_reserved_before_the_request(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"x"')

    def boom(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            raise httpx.ConnectError("reset", request=request)
        return fake_nps.handler(request)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    run = CaptureRunner(
        storage, client, clock=clock.now, sleep=clock.sleep, monotonic=clock.monotonic
    )
    out = run.run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "error" and out["stage"] == "image"
    state = CameraState.from_dict(storage.read_json(camera_state_key(RAINIER.id)))
    assert [g["status"] for g in state.gets] == ["error"]
    # The HEAD succeeded (feed counter 0); the image GET failed (image counter 1).
    assert state.failures == 0 and state.image_failures == 1 and state.not_before is not None
    kinds = [e["event"] for e in events(data_root) if e.get("viewpoint_id")]
    assert [k for k in kinds if k != "request"] == ["error", "backoff"]
    failed = [e for e in events(data_root) if e["event"] == "request" and e.get("error")]
    assert len(failed) == 1 and failed[0]["stage"] == "image" and failed[0]["status"] is None


# --- seeding from the stopgap --------------------------------------------------------------


def write_stopgap_frame(
    storage: LocalStorage, fetched: datetime, image: bytes, *, last_modified: str, etag: str
) -> str:
    import hashlib

    sha = hashlib.sha256(image).hexdigest()
    stamp = fetched - timedelta(seconds=20)
    stem = f"frames/{RAINIER.id}/{stamp:%Y-%m-%d}/{stamp:%Y%m%dT%H%M%SZ}_{sha[:12]}"
    storage.put_bytes(stem + ".jpg", image)
    storage.write_json(
        stem + ".json",
        {
            "viewpoint_id": RAINIER.id,
            "source": "live",
            "source_url": RAINIER.truth_camera.image_url,
            "fetched_at_utc": fetched.isoformat(),
            "http_last_modified": last_modified,
            "etag": etag,
            "content_length": len(image),
            "sha256": sha,
            "image_time_utc": stamp.isoformat(),
            "image_time_source": "http_last_modified",
            "stored": "original",
            "user_agent": "Lookout/0.1 (webcam research capture; fetch-on-change)",
            "capture_version": "stopgap-0",
        },
    )
    return stem + ".jpg"


def test_first_pass_seeds_from_stopgap_and_does_not_redownload(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    base = datetime(2026, 11, 19, 7, 30, 36, tzinfo=UTC)
    latest_image = b""
    for i in range(4):
        fetched = base + timedelta(minutes=5 * i)
        latest_image = make_jpeg(1920, 1080, pattern=i)
        lm = f"Thu, 19 Nov 2026 07:{30 + 5 * i}:16 GMT"
        write_stopgap_frame(storage, fetched, latest_image, last_modified=lm, etag=f'"s{i}"')
    # The camera still serves the stopgap's last image.
    fake_nps.set(
        RAINIER.truth_camera.image_url,
        latest_image,
        last_modified=datetime(2026, 11, 19, 7, 45, 16, tzinfo=UTC),
        etag='"s3"',
    )
    run = runner(storage, fake_nps, clock)
    out = run.run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "unchanged" and out["reason"] == "validators_same"
    assert image_gets(fake_nps, RAINIER.truth_camera.image_url) == []
    state = CameraState.from_dict(storage.read_json(camera_state_key(RAINIER.id)))
    assert state.seeded["sidecars_read"] == 4 and state.seeded["gets_last_hour"] == 4
    assert len(state.recent_shas) == 4
    # A new image appears, but the stopgap's four GETs fill the rolling hour.
    clock.advance(POLL_STEP_S)
    set_rainier(fake_nps, clock.now(), etag='"new-1"', pattern=9)
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "rate_capped" and out["reason"] == "hourly_cap"
    assert out["next_allowed_at"] == "2026-11-19T08:30:36Z"
    # After 07:30:36 + 1 h one slot frees and the change is fetched.
    clock.current = datetime(2026, 11, 19, 8, 30, 40, tzinfo=UTC)
    set_rainier(fake_nps, clock.now() - timedelta(seconds=40), etag='"new-2"', pattern=10)
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "stored" and out["image_time_source"] == "exif_offset"
    assert len(image_gets(fake_nps, RAINIER.truth_camera.image_url)) == 1


def test_frames_stored_by_another_writer_are_folded_in_every_pass(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now() - timedelta(minutes=1), etag='"a"')
    runner(storage, fake_nps, clock).run_pass([RAINIER])  # stores image "a"
    # Meanwhile the stopgap stores a newer image the camera still serves.
    clock.advance(POLL_STEP_S)
    newer = make_jpeg(1920, 1080, pattern=42)
    write_stopgap_frame(
        storage,
        clock.now() - timedelta(seconds=10),
        newer,
        last_modified="Thu, 19 Nov 2026 08:05:16 GMT",
        etag='"b"',
    )
    fake_nps.set(
        RAINIER.truth_camera.image_url,
        newer,
        last_modified=datetime(2026, 11, 19, 8, 5, 16, tzinfo=UTC),
        etag='"b"',
    )
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "unchanged"
    assert len(image_gets(fake_nps, RAINIER.truth_camera.image_url)) == 1


# --- back-off ----------------------------------------------------------------------------------


def test_errors_back_off_exponentially_and_recover(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    fake_nps.set(PORE.truth_camera.json_url, b"", status=500, content_type="text/plain")
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "error" and out["backoff_s"] == 300
    clock.advance(301)
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["backoff_s"] == 600
    clock.advance(POLL_STEP_S)
    assert (
        runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]["event"] == "not_due"
    )
    clock.advance(301)
    shot = clock.now() - timedelta(minutes=20)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/19/2026 12:00 AM PST",
        image=make_jpeg(exif_datetime=local_exif(shot, PORE.time_zone)),
        image_lm=shot,
        image_etag='"ok"',
    )
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "stored"
    state = CameraState.from_dict(storage.read_json(camera_state_key(PORE.id)))
    assert state.failures == 0 and state.not_before is None
    kinds = [e["event"] for e in events(data_root) if e.get("viewpoint_id")]
    assert kinds.count("backoff") == 2


def test_503_pauses_every_camera_on_the_host(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    fake_nps.fail_next[PORE.truth_camera.json_url] = 1
    set_rainier(fake_nps, clock.now(), etag='"r"')
    summary = runner(storage, fake_nps, clock).run_pass([PORE, RAINIER])
    outcomes = summary["outcomes"]
    assert outcomes[PORE.id]["event"] == "error" and outcomes[PORE.id]["status"] == 503
    assert outcomes[RAINIER.id] == {
        "event": "skipped",
        "reason": "host_backoff",
        "until": outcomes[RAINIER.id]["until"],
    }
    assert not [r for r in fake_nps.requests if "webcams-mora" in r["url"]]


# --- concurrency, S3 and Lambda ----------------------------------------------------------------


def test_concurrent_passes_fetch_each_image_once(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"c"')
    shot = clock.now() - timedelta(minutes=20)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/19/2026 12:00 AM PST",
        image=make_jpeg(exif_datetime=local_exif(shot, PORE.time_zone)),
        image_lm=shot,
        image_etag='"p"',
    )
    barrier = threading.Barrier(3)
    results = []

    def go() -> None:
        barrier.wait()
        results.append(runner(storage, fake_nps, clock, request_gap_s=0).run_pass([RAINIER, PORE]))

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(image_gets(fake_nps, RAINIER.truth_camera.image_url)) == 1
    assert len(image_gets(fake_nps, PORE.truth_camera.image_url)) == 1
    stored = [r["outcomes"][v]["event"] for r in results for v in (RAINIER.id, PORE.id)]
    assert stored.count("stored") == 2
    assert set(stored) <= {"stored", "skipped", "not_due"}


def test_s3_backend_end_to_end(s3_bucket, clock, fake_nps, data_root):
    storage = S3Storage("lookout-test", "dev", client=s3_bucket)
    set_rainier(fake_nps, clock.now(), etag='"s3"')
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "stored"
    keys = storage.list_prefix("")
    assert out["key"] in keys and out["key"][:-4] + ".json" in keys
    assert camera_state_key(RAINIER.id) in keys
    assert "capture_log/2026-11-19.jsonl" in keys
    assert not [k for k in keys if k.startswith("state/locks/")]  # released
    clock.advance(60)
    again = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert again["event"] == "not_due"
    log = storage.get_bytes("capture_log/2026-11-19.jsonl").decode().splitlines()
    kinds = [json.loads(line)["event"] for line in log]
    assert [k for k in kinds if k != "request"] == ["stored", "pass", "pass"]
    assert kinds.count("request") == 2  # HEAD and GET; the second pass was not due


def test_lambda_handler_runs_one_pass_on_s3(s3_bucket, monkeypatch, fake_nps, data_root):
    monkeypatch.setenv("LOOKOUT_STORAGE", "s3")
    monkeypatch.setenv("LOOKOUT_S3_BUCKET", "lookout-test")
    import lookout_capture.live as live

    real = live.polite_client
    monkeypatch.setattr(
        live, "polite_client", lambda transport=None: real(transport=fake_nps.transport())
    )
    now = datetime.now(UTC)
    fake_nps.clock = None
    set_rainier(fake_nps, now - timedelta(minutes=1), etag='"l"')

    class Context:
        def get_remaining_time_in_millis(self) -> int:
            return 110_000

    summary = lambda_handler.handler({"only": [RAINIER.id]}, Context())
    assert summary["runner"] == "lambda" and summary["counts"] == {"stored": 1}
    keys = S3Storage("lookout-test", client=s3_bucket).list_prefix("frames/")
    assert len([k for k in keys if k.endswith(".jpg")]) == 1


# --- loop and status ----------------------------------------------------------------------------


def test_loop_runs_passes_and_stops_cleanly(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"l1"')
    run = runner(storage, fake_nps, clock)
    passes = run.loop([RAINIER], interval_s=300, jitter_s=30, max_passes=3, install_signals=False)
    assert passes == 3
    heads = [r["t"] for r in fake_nps.requests if r["method"] == "HEAD"]
    assert len(heads) == 3
    assert all(300 <= (b - a).total_seconds() <= 332 for a, b in itertools.pairwise(heads))
    # A signal stops the loop after the camera in hand.
    run2 = runner(storage, fake_nps, clock)
    run2._on_signal(2, None)
    assert run2.loop([RAINIER], max_passes=5, install_signals=False) == 0
    with pytest.raises(KeyboardInterrupt):
        run2._on_signal(2, None)


def test_status_report_summarises_state_and_log(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"st"')
    runner(storage, fake_nps, clock).run_pass([RAINIER])
    report = status_report(storage, [RAINIER, PORE], now=clock.now())
    row = {r["viewpoint_id"]: r for r in report["cameras"]}
    assert row[RAINIER.id]["gets_last_hour"] == 1 and row[RAINIER.id]["last_image_key"]
    assert row[PORE.id] == {"viewpoint_id": PORE.id, "state": "none"}
    assert report["events_today"]["capture:stored"] == 1


def test_loop_never_spins_on_a_sub_microsecond_remainder(data_root, fake_nps):
    class TruncatingClock(FakeClock):
        def sleep(self, seconds: float) -> None:  # timedelta drops anything under 1 us
            self.slept.append(seconds)
            self.current += timedelta(seconds=max(0.0, seconds))

    class FixedRng:
        def uniform(self, a: float, b: float) -> float:
            return 12.0000004  # leaves a 0.4 us remainder

    clock = TruncatingClock(datetime(2026, 11, 19, 8, 0, 30, tzinfo=UTC))
    fake_nps.clock = clock
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"t"')
    run = CaptureRunner(
        storage,
        polite_client(transport=fake_nps.transport()),
        clock=clock.now,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        rng=FixedRng(),
    )  # type: ignore[arg-type]
    assert run.loop([RAINIER], interval_s=300, max_passes=2, install_signals=False) == 2
    assert len(clock.slept) < 1000


# --- review fixes, 19 Nov 2026 -------------------------------------------------------------------


def test_image_errors_back_off_exponentially_even_when_the_feed_answers(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/19/2026 12:00 AM PST",
        image=b"",
        image_lm=clock.now(),
        image_etag='"x"',
    )
    fake_nps.resources[PORE.truth_camera.image_url].status = 500
    backoffs = []
    for _ in range(24):  # two hours of 5-minute passes, the feed answering every time
        out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
        if out["event"] == "error":
            assert out["stage"] == "image"
            backoffs.append(out["backoff_s"])
        clock.advance(POLL_STEP_S)
    gets = image_gets(fake_nps, PORE.truth_camera.image_url)
    assert backoffs == [300, 600, 1200, 2400, 3600]
    assert len(gets) == 5  # 8 before the fix: one every 901 s at a fixed 300 s back-off
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(gets)]
    assert gaps == sorted(gaps)
    state = CameraState.from_dict(storage.read_json(camera_state_key(PORE.id)))
    assert state.image_failures == 5 and state.failures == 0
    # The image comes back: the next GET stores it and resets the counter.
    clock.advance(3600)
    shot = clock.now() - timedelta(minutes=10)
    fake_nps.set(
        PORE.truth_camera.image_url,
        make_jpeg(exif_datetime=local_exif(shot, PORE.time_zone)),
        last_modified=shot,
        etag='"y"',
    )
    out = runner(storage, fake_nps, clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "stored"
    state = CameraState.from_dict(storage.read_json(camera_state_key(PORE.id)))
    assert state.image_failures == 0 and state.not_before is None


def test_an_unreadable_state_file_does_not_stop_the_other_cameras(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    storage.put_bytes(camera_state_key(PORE.id), b"{ not json")
    set_rainier(fake_nps, clock.now(), etag='"ok"')
    summary = runner(storage, fake_nps, clock).run_pass([PORE, RAINIER])
    assert summary["outcomes"][PORE.id]["event"] == "error"
    assert summary["outcomes"][PORE.id]["stage"] == "internal"
    assert summary["outcomes"][RAINIER.id]["event"] == "stored"


def _grey_jpeg(level: int, pattern: int) -> bytes:
    import io

    from PIL import Image

    image = Image.new("RGB", (1920, 1080), (level, level, level))
    for i in range(6):
        x, y = (i * 211 + pattern * 97) % 1700, (i * 131 + pattern * 53) % 900
        shade = (i * 37 + pattern * 11) % 200
        image.paste((shade, shade, shade), (x, y, x + 200, y + 150))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def test_live_sidecars_carry_the_gate_inputs(data_root, clock, fake_nps):
    storage = LocalStorage(data_root)
    # A grey (infrared) night frame at Rainier, 08:00 UTC = midnight PST.
    url = RAINIER.truth_camera.image_url
    fake_nps.set(
        url, _grey_jpeg(40, 1), last_modified=clock.now() - timedelta(seconds=20), etag='"g1"'
    )
    first = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert first["event"] == "stored"
    assert {"monochrome", "sun_below_3deg"} <= set(first["flags"])
    side = storage.read_json(first["key"][:-4] + ".json")
    assert side["sun"]["daylight"] is False and side["sun"]["elevation_deg"] < -50
    assert side["chroma_spread"] < 1.0 and side["sharpness_lapvar"] > 0
    assert side["dhash_bits_to_previous"] is None  # nothing earlier to compare with
    clock.advance(900)
    fake_nps.set(
        url, _grey_jpeg(45, 2), last_modified=clock.now() - timedelta(seconds=20), etag='"g2"'
    )
    second = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    side = storage.read_json(second["key"][:-4] + ".json")
    assert isinstance(side["dhash_bits_to_previous"], int)
    assert 0 <= side["dhash_bits_to_previous"] <= 64
    # A colour frame at Point Reyes in the afternoon: no monochrome, no low-Sun flag.
    noon = datetime(2026, 11, 19, 21, 0, tzinfo=UTC)  # 1:00 PM PST
    day_clock = FakeClock(noon + timedelta(minutes=10))
    fake_nps.clock = day_clock
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/19/2026 01:00 PM PST",
        image=make_jpeg(exif_datetime=local_exif(noon, PORE.time_zone)),
        image_lm=noon + timedelta(minutes=6, seconds=30),
        image_etag='"d"',
    )
    out = runner(storage, fake_nps, day_clock).run_pass([PORE])["outcomes"][PORE.id]
    assert out["event"] == "stored", out
    assert not {"monochrome", "sun_below_3deg"} & set(out["flags"])
    side = storage.read_json(out["key"][:-4] + ".json")
    assert side["sun"]["daylight"] is True and side["sun"]["apparent_elevation_deg"] > 20


def test_poll_spacing_is_measured_from_the_send_and_never_under_300_s(data_root, clock, fake_nps):
    """Critic finding (20 Nov 2026): 8 of 504 poll gaps were 299.06-299.7 s, measured on send
    times, because ``last_poll_at`` was stamped before the request's spacing wait. Polls are
    now stamped at the send and scheduled with POLL_MARGIN_S, so every send-to-send gap of a
    camera's JSON GET or HEAD is at least MIN_POLL_INTERVAL_S (CLAUDE.md: at most every 5 min).

    In the first pass one camera waits behind the other's requests (a 7 s spacing wait).
    Then each camera is polled alone, first in its own pass, at its first due second: with
    the old stamp the camera that waited got a 293 s gap.
    """
    storage = LocalStorage(data_root)
    set_rainier(fake_nps, clock.now(), etag='"r0"')
    shot = clock.now() - timedelta(minutes=20)
    set_ard(
        fake_nps,
        PORE,
        imagedate="Updated 11/19/2026 12:00 AM PST",
        image=make_jpeg(exif_datetime=local_exif(shot, PORE.time_zone)),
        image_lm=shot,
        image_etag='"p0"',
    )
    runner(storage, fake_nps, clock, request_gap_s=7.0).run_pass([RAINIER, PORE])
    waiting = {RAINIER.id: RAINIER, PORE.id: PORE}
    for _ in range(400):  # 1 s steps
        if not waiting:
            break
        clock.advance(1.0)
        for vp_id, vp in list(waiting.items()):
            out = runner(storage, fake_nps, clock, request_gap_s=7.0).run_pass([vp])
            if out["outcomes"][vp_id]["event"] != "not_due":
                del waiting[vp_id]
    assert not waiting
    log = [
        json.loads(line)
        for line in (data_root / "capture_log" / "2026-11-19.jsonl").read_text().splitlines()
    ]
    first_pass_order = []
    for vp, stage in ((RAINIER, "head"), (PORE, "json")):
        sends = [
            datetime.fromisoformat(r["t"].replace("Z", "+00:00"))
            - timedelta(milliseconds=r["elapsed_ms"])
            for r in log
            if r["event"] == "request" and r["viewpoint_id"] == vp.id and r["stage"] == stage
        ]
        assert len(sends) == 2, (vp.id, sends)
        first_pass_order.append((sends[0], vp.id))
        gap = (sends[1] - sends[0]).total_seconds()
        assert MIN_POLL_INTERVAL_S <= gap <= MIN_POLL_INTERVAL_S + POLL_MARGIN_S + 1.0, (vp.id, gap)
        state = CameraState.from_dict(json.loads((data_root / camera_state_key(vp.id)).read_text()))
        assert state.last_poll_at is not None  # the stamp is the send, not the pass start
        stamp = datetime.fromisoformat(state.last_poll_at.replace("Z", "+00:00"))
        assert abs((stamp - sends[1]).total_seconds()) < 0.002
    # The scenario really had one camera waiting behind the other in the first pass.
    (t_a, _), (t_b, _) = sorted(first_pass_order)
    assert (t_b - t_a).total_seconds() >= 7.0
