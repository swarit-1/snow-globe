"""Image processing, image-time choice (DST folds, overrules) and the rate-cap rules."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

from lookout_common.images import exif_summary
from PIL import Image

from lookout_capture.fetch import UNANSWERED_REQUEST_BOUND_S
from lookout_capture.frames import (
    TINY_FILE_BYTES,
    choose_image_time,
    process_image,
)
from lookout_capture.state import (
    STAMP_RESOLUTION_S,
    UNCONFIRMED_SEND_SLACK_S,
    CameraState,
    cap_decision,
    get_answer_bound,
    iso_ms_up,
)
from lookout_capture.sun import solar_elevation_deg

from .conftest import make_jpeg

LA = "America/Los_Angeles"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- process_image ------------------------------------------------------------------------------


def test_narrow_frames_keep_original_bytes():
    body = make_jpeg(1920, 1080, exif_datetime="2026:11:18 22:30:00", tz_offset_hours=-8)
    out = process_image(body)
    assert out.stored == body and out.stored_mode == "original" and out.jpeg_quality is None
    assert (out.original_width, out.stored_width) == (1920, 1920)
    assert out.sha256 == out.stored_sha256 and out.exif["OffsetTimeOriginal"] == "-08:00"


def test_wide_frames_are_downscaled_with_exif_kept():
    body = make_jpeg(
        3840, 2160, exif_datetime="2026:11:18 20:00:00", artist="National Park Service"
    )
    out = process_image(body)
    assert out.stored_mode == "downscaled-1920" and out.jpeg_quality == 90
    assert (out.stored_width, out.stored_height) == (1920, 1080)
    kept = exif_summary(out.stored)
    assert kept["datetime"] == "2026:11:18 20:00:00" and kept["artist"] == "National Park Service"
    assert out.sha256 != out.stored_sha256 and out.exif_raw_bytes > 0
    assert 0 <= out.luma_mean <= 255 and out.chroma_spread > 0 and len(out.dhash64) == 16


def test_undecodable_and_tiny_frames_are_flagged_not_dropped():
    broken = process_image(b"\xff\xd8\xff\xe0" + b"\x00" * 40)
    assert not broken.decoded and "decode_failed" in broken.flags
    assert broken.stored_mode == "undecodable-original" and broken.stored.startswith(b"\xff\xd8")
    # A frame-sized image under the byte bar is a likely placeholder.
    buffer = io.BytesIO()
    Image.new("L", (640, 360), 0).save(buffer, "JPEG", quality=10)
    blank = buffer.getvalue()
    assert len(blank) < TINY_FILE_BYTES and "tiny_file" in process_image(blank).flags
    # A real fog frame (999x666, 12,568 bytes in the archive) must not be flagged.
    fog = make_jpeg(999, 666, color=(150, 160, 170))
    assert len(fog) > TINY_FILE_BYTES and "tiny_file" not in process_image(fog).flags
    # Thumbnails are small by design and are not flagged.
    assert "tiny_file" not in process_image(make_jpeg(100, 56)).flags


# --- choose_image_time -------------------------------------------------------------------------


def test_exif_with_offset_wins_and_reports_disagreements():
    times = choose_image_time(
        tz_name=LA,
        fetched_at=utc(2026, 11, 19, 6, 30, 30),
        exif_datetime="2026:11:18 22:30:00",
        exif_offset="-08:00",
        exif_subsec="431",
        last_modified_utc=utc(2026, 11, 19, 6, 30, 13),
    )
    assert times.source == "exif_offset"
    assert times.image_time_utc == utc(2026, 11, 19, 6, 30, 0) + timedelta(microseconds=431000)
    assert times.disagreement_min["http_last_modified_minus_exif"] == 0.209
    assert "exif:offset-from-exif" in times.flags


def test_exif_in_a_dst_fold_is_resolved_by_imagedate():
    # 1:30 AM on 1 Nov 2026 happens twice in Los Angeles (PDT, then PST).
    pst = choose_image_time(
        tz_name=LA,
        fetched_at=utc(2026, 11, 1, 9, 50),
        exif_datetime="2026:11:01 01:30:00",
        imagedate_utc=utc(2026, 11, 1, 9, 30),
    )  # "1:30 AM PST"
    assert pst.image_time_utc == utc(2026, 11, 1, 9, 30)
    assert "exif_fold_resolved_by_imagedate" in pst.flags
    assert "exif:ambiguous-earlier-offset" in pst.flags
    pdt = choose_image_time(
        tz_name=LA,
        fetched_at=utc(2026, 11, 1, 8, 50),
        exif_datetime="2026:11:01 01:30:00",
        imagedate_utc=utc(2026, 11, 1, 8, 30),
    )  # "1:30 AM PDT"
    assert pdt.image_time_utc == utc(2026, 11, 1, 8, 30)
    # Without any reference the earlier offset is taken (PRD §7.4), and flagged.
    alone = choose_image_time(
        tz_name=LA, fetched_at=utc(2026, 11, 1, 10, 0), exif_datetime="2026:11:01 01:30:00"
    )
    assert alone.image_time_utc == utc(2026, 11, 1, 8, 30)
    assert "exif:ambiguous-earlier-offset" in alone.flags


def test_exif_in_a_dst_gap_moves_forward_and_is_flagged():
    times = choose_image_time(
        tz_name=LA, fetched_at=utc(2027, 3, 14, 11, 0), exif_datetime="2027:03:14 02:30:00"
    )
    assert times.image_time_utc == utc(2027, 3, 14, 10, 30)
    assert "exif:nonexistent-moved-forward" in times.flags


def test_arizona_exif_has_no_dst():
    times = choose_image_time(
        tz_name="America/Phoenix",
        fetched_at=utc(2026, 7, 15, 22, 0),
        exif_datetime="2026:07:15 14:30:00",
        imagedate_utc=utc(2026, 7, 15, 21, 30),
    )
    assert times.image_time_utc == utc(2026, 7, 15, 21, 30)
    assert times.disagreement_min["exif_minus_json_imagedate"] == 0.0


def test_fallbacks_and_conflict_flags():
    only_lm = choose_image_time(
        tz_name=LA, fetched_at=utc(2026, 11, 19, 7), last_modified_utc=utc(2026, 11, 19, 6, 59)
    )
    assert only_lm.source == "http_last_modified"
    nothing = choose_image_time(tz_name=LA, fetched_at=utc(2026, 11, 19, 7))
    assert nothing.source == "fetched_at" and "image_time_from_fetch" in nothing.flags
    # EXIF 2 h off and Last-Modified far from imagedate: kept, but flagged as a conflict.
    conflict = choose_image_time(
        tz_name=LA,
        fetched_at=utc(2026, 11, 19, 9),
        exif_datetime="2026:11:18 20:00:00",
        imagedate_utc=utc(2026, 11, 19, 6),
        last_modified_utc=utc(2026, 11, 19, 8, 30),
    )
    assert conflict.source == "exif_local" and "time_conflict" in conflict.flags
    future = choose_image_time(
        tz_name=LA, fetched_at=utc(2026, 11, 19, 6), exif_datetime="2026:11:18 23:00:00"
    )
    assert "image_time_in_future" in future.flags


# --- the cap ---------------------------------------------------------------------------------


def _gets(*answers: datetime) -> list[dict]:
    """GET records as live capture leaves them once answered: ``answered_at`` is the answer."""
    return [
        {
            "t": (t - timedelta(seconds=0.25)).isoformat(),
            "reserved_at": (t - timedelta(seconds=1.25)).isoformat(),
            "answered_at": t.isoformat(),
            "status": "ok",
            "source": "self",
        }
        for t in answers
    ]


def test_cap_counts_records_without_an_answer_from_their_upper_bound():
    """The lead's decision (20 Nov 2026): the cap counts answer times. A record with none counts
    from the latest moment nps.gov can have received it:
    - reserved and never answered (a crash between the send and the save), or sent by 0.2.1
      before answer times: the later of its reservation and its send + the bound;
    - written by code before 0.2.1 (``t`` is the reservation): t + 2 s + the bound;
    - folded in from a sidecar (the fetch time, read after the full response): t + 1 ms."""
    t0 = utc(2026, 11, 19, 7, 30)
    times = [t0 + timedelta(minutes=15 * i) for i in range(4)]
    hour = t0 + timedelta(hours=1)
    reserved = [
        {"t": t.isoformat(), "reserved_at": t.isoformat(), "status": "reserved", "source": "self"}
        for t in times
    ]
    sent_only = [
        {
            "t": t.isoformat(),
            "reserved_at": (t - timedelta(seconds=1)).isoformat(),
            "status": "ok",
            "source": "self",
        }
        for t in times
    ]
    old_code = [{"t": t.isoformat(), "status": "ok", "source": "self"} for t in times]
    folded = [{"t": t.isoformat(), "status": "ok", "source": "sidecar:x"} for t in times]
    expected = {
        "reserved": UNANSWERED_REQUEST_BOUND_S,
        "sent_only": UNANSWERED_REQUEST_BOUND_S,
        "old_code": UNCONFIRMED_SEND_SLACK_S + UNANSWERED_REQUEST_BOUND_S,
        "folded": STAMP_RESOLUTION_S,
    }
    for name, gets in (
        ("reserved", reserved),
        ("sent_only", sent_only),
        ("old_code", old_code),
        ("folded", folded),
    ):
        at_hour = cap_decision(gets, hour, max_per_hour=4, min_spacing_s=0)
        assert not at_hour.allowed and at_hour.reason == "hourly_cap", name
        assert at_hour.next_allowed_at == hour + timedelta(seconds=expected[name]), name
        assert get_answer_bound(gets[0]) == t0 + timedelta(seconds=expected[name]), name
    assert UNANSWERED_REQUEST_BOUND_S == 60.0  # the 30 s send deadline + the 30 s write timeout
    assert cap_decision(_gets(*times), hour, max_per_hour=4, min_spacing_s=0).allowed


def test_cap_allows_four_per_rolling_hour_of_answers_and_names_the_next_slot():
    """A GET may be sent only while fewer than 4 of the camera's GETs were answered in the last
    3600 s, so the 5th is sent no earlier than the 1st's answer + 3600 s."""
    t0 = utc(2026, 11, 19, 7, 30)
    gets = _gets(
        t0, t0 + timedelta(minutes=15), t0 + timedelta(minutes=30), t0 + timedelta(minutes=45)
    )
    blocked = cap_decision(gets, t0 + timedelta(minutes=59), max_per_hour=4, min_spacing_s=0)
    assert not blocked.allowed and blocked.reason == "hourly_cap"
    assert blocked.next_allowed_at == t0 + timedelta(hours=1) and blocked.gets_last_hour == 4
    edge = t0 + timedelta(hours=1)
    assert not cap_decision(
        gets, edge - timedelta(microseconds=1), max_per_hour=4, min_spacing_s=0
    ).allowed
    assert cap_decision(gets, edge, max_per_hour=4, min_spacing_s=0).allowed
    # A bound in the future (a GET reserved 10 s ago, never answered) counts in the window.
    pending = [
        *gets[1:],
        {
            "t": edge.isoformat(),
            "reserved_at": edge.isoformat(),
            "status": "reserved",
            "source": "self",
        },
    ]
    later = cap_decision(pending, edge + timedelta(seconds=10), max_per_hour=4, min_spacing_s=0)
    assert not later.allowed and later.gets_last_hour == 4


def test_pacing_counts_from_the_last_answer_and_never_loosens_the_cap():
    t0 = utc(2026, 11, 19, 7, 30)
    spaced = cap_decision(_gets(t0), t0 + timedelta(minutes=5), max_per_hour=4, min_spacing_s=720)
    assert not spaced.allowed and spaced.reason == "spacing"
    assert spaced.next_allowed_at == t0 + timedelta(seconds=720)
    assert cap_decision(
        _gets(t0), t0 + timedelta(minutes=12), max_per_hour=4, min_spacing_s=720
    ).allowed


def test_answer_and_reservation_stamps_round_up_and_send_stamps_down():
    """Stamps that bound a receipt from above (answers, reservations) never read earlier than
    the clock; the send stamp (a lower bound) is truncated."""
    moment = utc(2026, 11, 19, 7, 30) + timedelta(microseconds=123_401)
    assert iso_ms_up(moment) == "2026-11-19T07:30:00.124Z"
    assert iso_ms_up(moment.replace(microsecond=123_000)) == "2026-11-19T07:30:00.123Z"
    state = CameraState(viewpoint_id="x")
    record = state.add_get(moment, status="reserved", reserved=True)
    assert record["t"] == "2026-11-19T07:30:00.123Z"
    assert record["reserved_at"] == "2026-11-19T07:30:00.124Z"


def test_an_unanswered_poll_reservation_counts_as_answered_at_its_bound():
    """A process killed between a poll's send and the state save leaves its reservation; the
    next run counts that poll as answered UNANSWERED_REQUEST_BOUND_S after it, logs it, and
    leaves no reservation behind (state written by this code before the kill)."""
    now = utc(2026, 11, 19, 8, 0, 3)
    state = CameraState(
        viewpoint_id="x",
        last_poll_at="2026-11-19T07:55:00.010Z",
        last_answer_at="2026-11-19T07:55:00.120Z",
        poll_reserved_at="2026-11-19T08:00:00.500Z",
    )
    assert state.poll_basis() == utc(2026, 11, 19, 8, 1, 0, 500_000)  # the reservation's bound
    changes = state.settle_unanswered(now)
    assert changes == [
        {
            "what": "poll",
            "kind": "unanswered_reservation",
            "reserved_at": "2026-11-19T08:00:00.500Z",
            "answer_bound_at": "2026-11-19T08:01:00.500Z",
        }
    ]
    assert state.poll_reserved_at is None and state.last_answer_bound == "unanswered_reservation"
    assert state.poll_due_at(300.002) == utc(2026, 11, 19, 8, 6, 0, 502_000)
    assert state.settle_unanswered(now) == []  # settled once


def test_state_from_code_without_answer_times_is_bounded_from_its_send_and_when_it_is_read():
    """0.2.0 (or 0.2.1 before answer times) stamped only the send. Its poll ended before it saved
    and before this run read the state, so the answer is bounded by the send + the bound and
    by the moment this run reads it, whichever is later. Its GET records (``t`` is the
    reservation) get t + 2 s + the bound, not the read time (that would hold images back)."""
    sent = utc(2026, 11, 19, 7, 55, 0, 10_000)
    soon = CameraState(viewpoint_id="x", last_poll_at="2026-11-19T07:55:00.010Z")
    assert soon.poll_basis() == sent + timedelta(seconds=UNANSWERED_REQUEST_BOUND_S)
    soon.settle_unanswered(sent + timedelta(seconds=20))  # restarted 20 s after the send
    assert soon.last_answer_at == "2026-11-19T07:56:00.010Z"
    assert soon.last_answer_bound == "legacy_state"
    late = CameraState(
        viewpoint_id="x",
        last_poll_at="2026-11-19T07:55:00.010Z",
        gets=[{"t": "2026-11-19T07:55:01.000Z", "status": "ok", "source": "self"}],
    )
    read_at = utc(2026, 11, 19, 7, 58, 0, 1)  # restarted 3 min later: the read bounds it
    assert late.poll_basis(now=read_at) == read_at
    changes = late.settle_unanswered(read_at)
    assert late.last_answer_at == "2026-11-19T07:58:00.001Z"
    assert late.gets[0]["answered_at"] == "2026-11-19T07:56:03.000Z"
    assert late.gets[0]["answer_bound"] == "legacy_record"
    assert [c["what"] for c in changes] == ["poll", "image_get"]


def test_state_roundtrip_ignores_unknown_keys_and_prunes():
    now = utc(2026, 11, 19, 9)
    state = CameraState.from_dict({"viewpoint_id": "rainier.paradise-mountain", "future": 1})
    state.add_get(now - timedelta(hours=3), status="ok")
    state.add_get(now - timedelta(minutes=10), status="ok")
    state.known_sidecars = ["frames/x.y/2026-11-17/a.json", "frames/x.y/2026-11-18/b.json"]
    for i in range(300):
        state.remember_sha(f"{i:064x}", f"k{i}")
    state.prune(now)
    assert len(state.gets) == 1 and state.known_sidecars == ["frames/x.y/2026-11-18/b.json"]
    assert len(state.recent_shas) == 240 and f"{299:064x}" in state.recent_shas
    assert CameraState.from_dict(state.to_dict()) == state


def test_sun_matches_a_known_value():
    # NPS gave 26.644 deg for Point Reyes at 10:00 AM PST on 17 Nov 2026 (archive sample).
    got = solar_elevation_deg(utc(2026, 11, 17, 18, 0), 37.99604, -123.02)
    assert abs(got - 26.644) < 0.1


# --- image time: only a DST-shaped gap is overruled (review finding, 19 Nov 2026) -------------


def test_correct_exif_is_kept_when_imagedate_is_an_hour_early():
    # EXIF 14:30 PDT (21:30Z) is right; imagedate says 20:30Z; the upload came 6.5 min after
    # the real time. Last-Modified is 66.5 min after imagedate: no agreement, so EXIF stays.
    times = choose_image_time(
        tz_name=LA,
        fetched_at=utc(2026, 7, 15, 21, 40),
        exif_datetime="2026:07:15 14:30:00",
        imagedate_utc=utc(2026, 7, 15, 20, 30),
        last_modified_utc=utc(2026, 7, 15, 21, 36, 30),
    )
    assert times.image_time_utc == utc(2026, 7, 15, 21, 30) and times.source == "exif_local"
    assert "time_conflict" in times.flags and "exif_time_overruled" not in times.flags


def test_stale_exif_under_a_new_imagedate_keeps_its_old_time():
    # A 2 h old image re-served under a fresh imagedate: not DST-shaped, so EXIF (08:45Z)
    # is kept and the caller's stale_image_time check can fire.
    times = choose_image_time(
        tz_name=LA,
        fetched_at=utc(2026, 11, 19, 10, 56),
        exif_datetime="2026:11:19 00:45:00",
        imagedate_utc=utc(2026, 11, 19, 10, 45),
        last_modified_utc=utc(2026, 11, 19, 10, 51, 30),
    )
    assert times.image_time_utc == utc(2026, 11, 19, 8, 45)
    assert "time_conflict" in times.flags and "exif_time_overruled" not in times.flags


def test_one_hour_error_shared_by_exif_and_imagedate_is_flagged():
    times = choose_image_time(
        tz_name=LA,
        fetched_at=utc(2026, 7, 15, 21, 40),
        exif_datetime="2026:07:15 13:30:00",
        imagedate_utc=utc(2026, 7, 15, 20, 30),
        last_modified_utc=utc(2026, 7, 15, 21, 36, 30),
    )
    assert times.image_time_utc == utc(2026, 7, 15, 20, 30)
    assert "last_modified_long_after_image_time" in times.flags


def test_dst_shaped_gap_with_agreeing_upload_is_still_overruled():
    # Camera clock on PST in summer (13:30 = 20:30Z read as PDT); imagedate 21:30Z; upload
    # 6.5 min later, as measured live. That is the one case EXIF loses.
    times = choose_image_time(
        tz_name=LA,
        fetched_at=utc(2026, 7, 15, 21, 40),
        exif_datetime="2026:07:15 13:30:00",
        imagedate_utc=utc(2026, 7, 15, 21, 30),
        last_modified_utc=utc(2026, 7, 15, 21, 36, 30),
    )
    assert times.image_time_utc == utc(2026, 7, 15, 21, 30) and times.source == "json_imagedate"
    assert "exif_time_overruled" in times.flags
    assert "last_modified_long_after_image_time" not in times.flags
    # The same gap with the upload 30 min after imagedate is a conflict, not an overrule.
    late = choose_image_time(
        tz_name=LA,
        fetched_at=utc(2026, 7, 15, 22, 10),
        exif_datetime="2026:07:15 13:30:00",
        imagedate_utc=utc(2026, 7, 15, 21, 30),
        last_modified_utc=utc(2026, 7, 15, 22, 0),
    )
    assert late.source == "exif_local" and "time_conflict" in late.flags


# --- the capture-time gate inputs (PRD §8.6) ---------------------------------------------------


def test_laplacian_variance_ranks_sharp_above_blurred_and_flat_is_zero():
    from PIL import ImageFilter

    from lookout_capture.frames import laplacian_variance

    sharp = Image.open(io.BytesIO(make_jpeg(1280, 720, pattern=3)))
    blurred = sharp.filter(ImageFilter.GaussianBlur(4))
    flat = Image.new("RGB", (800, 450), (120, 130, 140))
    s, b = laplacian_variance(sharp), laplacian_variance(blurred)
    assert s > 5 * b > 0
    assert laplacian_variance(flat) == 0.0
    # Measured on a copy at most 640 px wide: the score barely moves with the input size.
    half = sharp.resize((640, 360), Image.Resampling.BOX)
    assert abs(laplacian_variance(half) - s) <= 0.05 * s
    # A one-pixel checkerboard hits the full +-1020 Laplacian range without clipping.
    board = Image.new("L", (64, 64))
    board.putdata([255 * ((x + y) % 2) for y in range(64) for x in range(64)])
    assert laplacian_variance(board) == 1020.0**2


def test_sun_position_and_gate_flags():
    from lookout_capture.frames import gate_flags, sun_position

    noon = sun_position(utc(2026, 11, 17, 20, 0), 37.99604, -123.02)  # Point Reyes, midday
    night = sun_position(utc(2026, 11, 19, 10, 45), 37.99604, -123.02)
    assert noon["daylight"] and 30 < noon["elevation_deg"] < 33
    assert noon["apparent_elevation_deg"] > noon["elevation_deg"]
    assert not night["daylight"] and night["elevation_deg"] < -40
    grey = process_image(make_jpeg(640, 360, color=(90, 90, 90), pattern=0))
    colour = process_image(make_jpeg(640, 360, color=(60, 120, 200)))
    assert "sun_below_3deg" in gate_flags(colour, night)
    assert gate_flags(colour, noon) == []
    # A synthetic frame has coloured patches, so build a truly grey one for "monochrome".
    buffer = io.BytesIO()
    Image.new("RGB", (640, 360), (70, 70, 70)).save(buffer, "JPEG", quality=90)
    mono = process_image(buffer.getvalue())
    assert mono.chroma_spread is not None and mono.chroma_spread < 1.0
    assert "monochrome" in gate_flags(mono, noon)
    assert grey.sharpness_lapvar is not None and grey.sharpness_lapvar > 0
