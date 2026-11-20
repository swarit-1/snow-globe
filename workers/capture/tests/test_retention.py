"""PRD §8.7 retention: hour slots, staging keys, the hourly decision, crash recovery, and that
only capture's own staged copies can ever be deleted (locally and in S3)."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from lookout_common.paths import frame_key, sidecar_key, thumb_key
from lookout_common.storage import LocalStorage, S3Storage
from lookout_common.timeutil import isoformat_z
from PIL import Image

from lookout_capture.frames import make_thumbnail, sha256_hex
from lookout_capture.retention import (
    EVERY_FRAME_VIEWPOINTS,
    POLICY_EVERY,
    POLICY_HOURLY,
    delete_staged,
    frame_key_of_staged,
    frame_policy,
    frame_sidecar_from_thumb,
    frames_in_window,
    hour_slot,
    key_stamp,
    promote_ready_hours,
    scan_staged,
    staging_key,
    thumb_key_of_frame,
    thumb_sidecar,
)

from .conftest import make_jpeg

VP = "grand-canyon.yavapai"
H = datetime(2026, 11, 19, 13, 0, tzinfo=UTC)


def utc(hour: int, minute: int = 0, second: int = 0, day: int = 19) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)


def stage(
    storage,
    when: datetime,
    *,
    pattern: int = 0,
    sidecar: bool = True,
    thumb: bool = True,
    flags: list[str] | None = None,
    vp: str = VP,
) -> str:
    """Stage one full-size frame the way live capture does; return its staging key."""
    body = make_jpeg(640, 360, pattern=pattern)
    sha = sha256_hex(body)
    fkey = frame_key(vp, when, sha)
    skey = staging_key(fkey)
    doc = {
        "viewpoint_id": vp,
        "source": "live",
        "image_time_utc": isoformat_z(when),
        "sha256": sha,
        "stored": "original",
        "stored_width": 640,
        "stored_height": 360,
        "stored_bytes": len(body),
        "stored_sha256": sha,
        "jpeg_quality": None,
        "flags": flags or [],
        "frame_policy": POLICY_HOURLY,
    }
    if thumb:
        with Image.open(io.BytesIO(body)) as image:
            small = make_thumbnail(image, width=320)
        tkey = thumb_key(vp, when, sha)
        doc["thumb"] = {"key": tkey, **small.to_sidecar()}
        storage.put_bytes(tkey, small.data)
        storage.write_json(
            sidecar_key(tkey),
            thumb_sidecar(doc, small, frame_key=fkey, policy=POLICY_HOURLY, staged_key=skey),
        )
    storage.put_bytes(skey, body)
    if sidecar:
        storage.write_json(sidecar_key(skey), doc)
    return skey


def entries(*keys: str) -> list[dict]:
    return [{"key": k} for k in keys]


# --- keys, slots, policy ----------------------------------------------------------------------


def test_policy_keeps_every_frame_only_at_the_two_design_p_cameras():
    assert {"rainier.paradise-mountain", "point-reyes.beach"} == EVERY_FRAME_VIEWPOINTS
    assert frame_policy("rainier.paradise-mountain") == POLICY_EVERY
    assert frame_policy("point-reyes.beach") == POLICY_EVERY
    for other in (
        "grand-canyon.yavapai",
        "grand-teton.science-center",
        "yosemite.turtleback",
        "acadia.mcfarland",
        "smokies.look-rock",
        "big-bend.panther-junction",
    ):
        assert frame_policy(other) == POLICY_HOURLY


def test_hour_slot_takes_the_nearest_hour_with_half_open_windows():
    assert hour_slot(utc(12, 29, 59)) == utc(12)
    assert hour_slot(utc(12, 30, 0)) == utc(13)
    assert hour_slot(utc(13, 29, 59)) == utc(13)
    assert hour_slot(utc(23, 45, day=19)) == utc(0, day=20)


def test_staging_frame_and_thumb_keys_share_the_file_name():
    fkey = "frames/grand-canyon.yavapai/2026-11-19/20261119T124500Z_88cf15169fed.jpg"
    skey = staging_key(fkey)
    assert skey == "cache/staging/" + fkey and frame_key_of_staged(skey) == fkey
    assert thumb_key_of_frame(fkey).startswith("thumbs/grand-canyon.yavapai/2026-11-19/")
    assert key_stamp(skey) == utc(12, 45)
    assert key_stamp("frames/x.y/2026-11-19/notastamp.jpg") is None
    with pytest.raises(ValueError):
        staging_key("thumbs/" + fkey[len("frames/") :])
    with pytest.raises(ValueError):
        frame_key_of_staged(fkey)


def test_thumb_sidecar_round_trips_to_the_frame_sidecar(data_root):
    storage = LocalStorage(data_root)
    skey = stage(storage, utc(12, 45))
    fkey = frame_key_of_staged(skey)
    tkey = thumb_key_of_frame(fkey)
    tdoc = storage.read_json(sidecar_key(tkey))
    assert tdoc["variant"] == "thumb" and tdoc["stored"] == "thumb-320"
    assert tdoc["stored_width"] == 320 and tdoc["full_frame"]["key_if_promoted"] == fkey
    # An hourly thumbnail never names a full frame as if it existed: only 1 in ~4 is kept.
    assert "key" not in tdoc["full_frame"] and tdoc["full_frame"]["staged_key"] == skey
    assert tdoc["full_frame"]["stored_width"] == 640 and "thumb" not in tdoc
    rebuilt = frame_sidecar_from_thumb(tdoc, tkey)
    original = storage.read_json(sidecar_key(skey))
    assert rebuilt["stored_width"] == 640 and rebuilt["stored_bytes"] == original["stored_bytes"]
    assert (
        rebuilt["thumb"]["key"] == tkey and rebuilt["image_time_utc"] == original["image_time_utc"]
    )
    assert rebuilt["sidecar_rebuilt_from"] == sidecar_key(tkey)


# --- deleting only staged copies ------------------------------------------------------------


def test_delete_refuses_everything_outside_staging(data_root):
    storage = LocalStorage(data_root)
    for key in (
        "frames/grand-canyon.yavapai/2026-11-19/20261119T124500Z_88cf15169fed.jpg",
        "thumbs/grand-canyon.yavapai/2026-11-19/20261119T124500Z_88cf15169fed.jpg",
        "archive/grand-canyon.yavapai/2025-01-15/20250115T190000Z_41af76bd.jpg",
        "cache/nps-archive/search/grca/2025-01-15.json",
        "state/capture/grand-canyon.yavapai.json",
    ):
        storage.put_bytes(key, b"keep me")
        with pytest.raises(ValueError, match="only staged copies"):
            delete_staged(storage, key)
        assert storage.get_bytes(key) == b"keep me"
    with pytest.raises(ValueError):
        delete_staged(storage, "cache/staging/../frames/x.jpg")


def test_delete_staged_locally_removes_empty_staging_folders_only(data_root):
    storage = LocalStorage(data_root)
    skey = stage(storage, utc(12, 45))
    assert delete_staged(storage, skey) and delete_staged(storage, sidecar_key(skey))
    assert not delete_staged(storage, skey)  # already gone
    day_folder = storage.path(skey).parent
    assert not day_folder.exists() and not day_folder.parent.exists()
    assert storage.path("cache/staging/frames").is_dir()  # the root of staging stays
    assert storage.list_prefix("thumbs/")  # untouched


def test_delete_staged_in_s3(s3_bucket, data_root):
    storage = S3Storage("lookout-test", "dev", client=s3_bucket)
    skey = stage(storage, utc(12, 45))
    assert storage.exists(skey)
    assert delete_staged(storage, skey) is True
    assert not storage.exists(skey) and storage.exists(sidecar_key(skey))
    frame = frame_key_of_staged(skey)
    storage.put_bytes(frame, b"x")
    with pytest.raises(ValueError):
        delete_staged(storage, frame)
    assert storage.exists(frame)


# --- the hourly decision ----------------------------------------------------------------------


def test_nearest_candidate_is_promoted_once_the_hour_has_settled(data_root):
    storage = LocalStorage(data_root)
    keys = [
        stage(storage, utc(12, 30), pattern=1),
        stage(storage, utc(12, 45), pattern=2),
        stage(storage, utc(13, 0, 2), pattern=3),
        stage(storage, utc(13, 15), pattern=4),
        stage(storage, utc(13, 30), pattern=5),  # belongs to 14:00
    ]
    early = promote_ready_hours(storage, VP, entries(*keys), utc(13, 59, 59))
    assert early.events == [] and len(early.remaining) == 5
    result = promote_ready_hours(storage, VP, entries(*keys), utc(14, 0))
    promoted = [e for e in result.events if e["event"] == "promoted"]
    assert len(promoted) == 1 and promoted[0]["key"] == frame_key_of_staged(keys[2])
    assert promoted[0]["distance_s"] == 2.0 and promoted[0]["candidates"] == 4
    assert sorted(promoted[0]["dropped"]) == sorted(
        k.rsplit("/", 1)[-1] for k in keys[:2] + keys[3:4]
    )
    assert [e["key"] for e in result.remaining] == [keys[4]]
    target = frame_key_of_staged(keys[2])
    side = storage.read_json(sidecar_key(target))
    selection = side["hourly_selection"]
    assert selection["hour_utc"] == "2026-11-19T13:00:00Z" and selection["candidates"] == 4
    assert selection["window_utc"] == ["2026-11-19T12:30:00Z", "2026-11-19T13:30:00Z"]
    assert side["thumb"]["key"].startswith("thumbs/") and side["sha256"]
    # Only the one frame reached frames/; the hour's staged copies are gone; thumbs stay.
    assert [k for k in storage.list_prefix("frames/") if k.endswith(".jpg")] == [target]
    assert scan_staged(storage, VP) == [keys[4]]
    assert len([k for k in storage.list_prefix("thumbs/") if k.endswith(".jpg")]) == 5
    assert result.written_sidecars == [sidecar_key(target)]
    assert result.last and result.last["key"] == target


def test_ties_go_to_the_earlier_frame_and_decodable_beats_nearer(data_root):
    storage = LocalStorage(data_root)
    before, after = stage(storage, utc(12, 50), pattern=1), stage(storage, utc(13, 10), pattern=2)
    result = promote_ready_hours(storage, VP, entries(after, before), utc(15))
    assert result.events[0]["key"] == frame_key_of_staged(before)
    broken = stage(storage, utc(14, 0), pattern=3, flags=["decode_failed"], thumb=False)
    good = stage(storage, utc(14, 20), pattern=4)
    result = promote_ready_hours(storage, VP, entries(broken, good), utc(16))
    assert result.events[0]["key"] == frame_key_of_staged(good)


def test_an_hour_already_kept_in_frames_keeps_it_and_drops_the_staged_copies(data_root):
    storage = LocalStorage(data_root)
    old = "frames/grand-canyon.yavapai/2026-11-19/20261119T124500Z_000000000000.jpg"
    storage.put_bytes(old, b"kept by older capture code")
    late = stage(storage, utc(13, 0))
    result = promote_ready_hours(storage, VP, entries(late), utc(14))
    assert [e["event"] for e in result.events] == ["staging_dropped"]
    assert result.events[0]["reason"] == "hour_already_kept"
    assert storage.get_bytes(old) == b"kept by older capture code"
    assert not storage.exists(frame_key_of_staged(late)) and not storage.exists(late)
    assert storage.exists(thumb_key_of_frame(frame_key_of_staged(late)))


def test_a_promotion_cut_short_by_a_crash_is_completed(data_root):
    storage = LocalStorage(data_root)
    skey = stage(storage, utc(13, 0))
    target = frame_key_of_staged(skey)
    storage.put_bytes(target, storage.get_bytes(skey))  # the JPEG was copied, then a crash
    result = promote_ready_hours(storage, VP, entries(skey), utc(14))
    assert [e["event"] for e in result.events] == ["promoted"] and result.events[0]["recovered"]
    assert storage.read_json(sidecar_key(target))["hourly_selection"]["recovered"] is True
    assert not storage.exists(skey)
    # A leftover staged copy of a finished promotion is only cleaned up, not logged again.
    storage.put_bytes(skey, storage.get_bytes(target))
    again = promote_ready_hours(storage, VP, entries(skey), utc(14, 5))
    assert again.events == [] and not storage.exists(skey)


def test_a_lost_staged_sidecar_is_rebuilt_from_the_thumbnail(data_root):
    storage = LocalStorage(data_root)
    skey = stage(storage, utc(13, 0), sidecar=False)
    result = promote_ready_hours(storage, VP, entries(skey), utc(14))
    side = storage.read_json(sidecar_key(frame_key_of_staged(skey)))
    assert result.events[0]["event"] == "promoted"
    assert side["sidecar_rebuilt_from"].startswith("thumbs/") and side["stored_width"] == 640


def test_candidates_without_a_usable_jpeg_or_sidecar_are_dropped(data_root):
    storage = LocalStorage(data_root)
    lone_json = stage(storage, utc(13, 0), pattern=1)
    storage.path(lone_json).unlink()
    bare = stage(storage, utc(13, 5), pattern=2, sidecar=False, thumb=False)
    result = promote_ready_hours(storage, VP, entries(lone_json, bare), utc(14))
    assert [e["reason"] for e in result.events] == ["no_usable_candidate"]
    assert scan_staged(storage, VP) == [] and not storage.list_prefix("frames/")


def test_a_failed_write_keeps_every_staged_copy_for_the_next_pass(data_root, monkeypatch):
    storage = LocalStorage(data_root)
    skey = stage(storage, utc(13, 0))
    real = storage.put_bytes

    def disk_full(key, data, **kwargs):
        if key.startswith("frames/"):
            raise OSError(28, "No space left on device")
        return real(key, data, **kwargs)

    monkeypatch.setattr(storage, "put_bytes", disk_full)
    result = promote_ready_hours(storage, VP, entries(skey), utc(14))
    assert [e["event"] for e in result.events] == ["staging_error"]
    assert result.remaining == [{"key": skey}] and storage.exists(skey)
    monkeypatch.setattr(storage, "put_bytes", real)
    result = promote_ready_hours(storage, VP, result.remaining, utc(14, 5))
    assert result.events[0]["event"] == "promoted" and not storage.exists(skey)


def test_windows_across_utc_midnight_read_both_date_folders(data_root):
    storage = LocalStorage(data_root)
    old = "frames/grand-canyon.yavapai/2026-11-19/20261119T234500Z_000000000000.jpg"
    storage.put_bytes(old, b"x")
    found = frames_in_window(storage, VP, utc(23, 30), utc(0, 30, day=20))
    assert list(found) == ["20261119T234500Z_000000000000.jpg"]
    late = stage(storage, utc(0, 10, day=20))
    result = promote_ready_hours(storage, VP, entries(late), utc(1, 0, day=20))
    assert result.events[0]["reason"] == "hour_already_kept"


def test_bad_or_repeated_entries_are_harmless(data_root):
    storage = LocalStorage(data_root)
    skey = stage(storage, utc(13, 0))
    result = promote_ready_hours(
        storage, VP, [*entries(skey, skey), {"key": "frames/x"}, {}], utc(14)
    )
    assert [e["event"] for e in result.events] == ["staging_dropped", "promoted"]
    assert result.events[0]["reason"] == "bad_key"


def test_staged_copies_are_found_by_listing_after_a_lost_state(data_root):
    storage = LocalStorage(data_root)
    a = stage(storage, utc(13, 0), pattern=1)
    b = stage(storage, utc(13, 15), pattern=2)
    storage.path(b).unlink()  # a lone .json still counts, so it gets cleaned up
    assert scan_staged(storage, VP) == sorted([a, b])
    assert scan_staged(storage, "point-reyes.beach") == []


def test_promoted_frame_matches_the_staged_bytes(data_root):
    storage = LocalStorage(data_root)
    skey = stage(storage, utc(13, 0))
    data = storage.get_bytes(skey)
    promote_ready_hours(storage, VP, entries(skey), utc(14) + timedelta(seconds=1))
    assert storage.get_bytes(frame_key_of_staged(skey)) == data
