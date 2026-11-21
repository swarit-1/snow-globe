"""The rename to Snow Globe (D-080): capture 0.3.0 reads what 0.1.0 to 0.2.1 wrote.

- Stamps: ``lookout-capture/<v>`` (every record, sidecar and state written before the rename)
  and ``snow-globe-capture/<v>`` name releases the same way (:func:`capture_release`).
- ``fixtures/state_0.2.1_rainier.paradise-mountain.json`` is Rainier's state as the running
  0.2.1 loop (``lookout-capture/0.2.1``) left it at 01:45 UTC on 21 Nov 2026 (metadata only;
  its dedupe lists cut to their newest 3 entries). It records its last poll's answer, so the
  renamed code needs no upgrade bound: its first poll is due 300.002 s after that answer, not a
  bound later. That is why the live switch costs no extra interval, unlike 0.2.0 to 0.2.1
  (D-079), whose state recorded no answers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from snow_globe_common.storage import LocalStorage

from snow_globe_capture import (
    CAPTURE_NAME,
    CAPTURE_VERSION,
    CAPTURE_VERSION_PREFIXES,
    LEGACY_CAPTURE_NAME,
    __version__,
    capture_release,
)
from snow_globe_capture.live import MIN_POLL_INTERVAL_S, POLL_MARGIN_S, status_report
from snow_globe_capture.state import CameraState, camera_state_key

from .conftest import FakeClock, fixture_json
from .test_live import RAINIER, runner, set_rainier

FIXTURE = "state_0.2.1_rainier.paradise-mountain.json"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def test_the_stamp_uses_the_new_name_and_a_new_release():
    assert (CAPTURE_NAME, LEGACY_CAPTURE_NAME) == ("snow-globe-capture", "lookout-capture")
    assert f"snow-globe-capture/{__version__}" == CAPTURE_VERSION
    assert __version__.startswith("0.3."), "the rename bumps the minor version (D-080)"
    assert CAPTURE_VERSION_PREFIXES == ("snow-globe-capture/", "lookout-capture/")


@pytest.mark.parametrize(
    ("stamp", "release"),
    [
        ("lookout-capture/0.1.0", "0.1.0"),
        ("lookout-capture/0.2.1", "0.2.1"),
        ("snow-globe-capture/0.3.0", "0.3.0"),
        ("snow-globe-capture/0.2.1", "0.2.1"),  # the same release under either name
        ("stopgap-0", None),
        ("lookout-capture/", None),
        ("capture/0.2.1", None),
        ("", None),
        (None, None),
        (21, None),
    ],
)
def test_capture_release_reads_both_names(stamp, release):
    assert capture_release(stamp) == release


def test_a_state_recorded_from_the_running_0_2_1_loop_is_read_as_it_is(data_root, fake_nps):
    doc = fixture_json(FIXTURE)
    assert doc["last_image"]["capture_version"] == "lookout-capture/0.2.1"
    assert doc["last_answer_at"] and doc["last_answer_bound"] is None
    storage = LocalStorage(data_root)
    storage.write_json(camera_state_key(RAINIER.id), doc)
    answered = _utc(doc["last_answer_at"])
    slot = answered + timedelta(seconds=MIN_POLL_INTERVAL_S + POLL_MARGIN_S)

    clock = FakeClock(slot - timedelta(milliseconds=1))
    fake_nps.clock = clock
    set_rainier(fake_nps, slot - timedelta(seconds=40), etag='"after-the-rename"')
    row = status_report(storage, [RAINIER], now=clock.now())["cameras"][0]
    assert row["next_poll_basis"] == "answer"  # no upgrade bound, unlike 0.2.0's state
    assert row["last_capture_version"] == "lookout-capture/0.2.1"
    out = runner(storage, fake_nps, clock).run_pass([RAINIER])["outcomes"][RAINIER.id]
    assert out["event"] == "not_due" and fake_nps.requests == []

    clock.current = slot
    runner(storage, fake_nps, clock).run_pass([RAINIER])
    assert fake_nps.requests and fake_nps.requests[0]["method"] == "HEAD"
    # due at once: 300.002 s after the recorded answer
    assert slot <= fake_nps.requests[0]["t"] < slot + timedelta(seconds=1)
    state = CameraState.from_dict(storage.read_json(camera_state_key(RAINIER.id)))
    assert state.last_answer_bound is None  # nothing was bounded on the way in
    assert _utc(state.last_answer_at) >= slot
