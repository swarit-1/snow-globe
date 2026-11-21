"""Frame quality gates (PRD §8.6) on synthetic sidecars (the snow-globe-capture layout)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import FixedEphemeris
from snow_globe_common.manifest import load_manifest

from snow_globe_eval.gates import (
    GATES,
    FrameGates,
    GateResult,
    colour_mode_gate,
    daylight_gate,
    excluded_mask,
    exclusion_counts,
    frame_gates,
    fresh_gate,
    usable_frame_gate,
)
from snow_globe_eval.scores import contingency

VP = load_manifest("rainier.paradise-mountain")
T = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)


def _sidecar(sha: str, time: str, last_modified: str | None, **extra):
    return {
        "viewpoint_id": VP.id,
        "image_time_utc": time,
        "http_last_modified": last_modified,
        "sha256": sha,
        **extra,
    }


def test_fresh_gate() -> None:
    first = _sidecar("a" * 64, "2026-11-19T19:45:13Z", "Thu, 19 Nov 2026 19:45:13 GMT")
    second = _sidecar("b" * 64, "2026-11-19T20:00:13Z", "Thu, 19 Nov 2026 20:00:13 GMT")
    assert fresh_gate(second, first).passed is True
    assert fresh_gate(first, None).passed is None
    frozen = _sidecar("a" * 64, "2026-11-19T20:00:13Z", "Thu, 19 Nov 2026 20:00:13 GMT")
    result = fresh_gate(frozen, first)
    assert result.passed is False and "SHA-256" in result.reason
    # archive frames have no Last-Modified: skipped and named, the rest still compared
    archive_a = _sidecar("c" * 64, "2021-01-15T20:15:00Z", None)
    archive_b = _sidecar("d" * 64, "2021-01-15T20:30:00Z", None)
    result = fresh_gate(archive_b, archive_a)
    assert result.passed is True and "not compared: HTTP Last-Modified" in result.reason
    assert fresh_gate({"sha256": None}, {"sha256": None}).passed is None


def test_colour_mode_gate_needs_a_calibrated_cut_off() -> None:
    assert colour_mode_gate(0.01, max_chroma_spread=None).passed is None
    assert colour_mode_gate(None, max_chroma_spread=1.0).passed is None
    assert colour_mode_gate(0.01, max_chroma_spread=1.0).passed is False
    assert colour_mode_gate(29.2, max_chroma_spread=1.0).passed is True


def test_daylight_gate_uses_the_ephemeris_threshold_inclusively() -> None:
    assert daylight_gate(VP, T, ephemeris=FixedEphemeris(3.0)).passed is True
    below = daylight_gate(VP, T, ephemeris=FixedEphemeris(2.99))
    assert below.passed is False and "threshold 3 deg" in below.reason
    assert daylight_gate(VP, T, min_sun_alt_deg=10.0, ephemeris=FixedEphemeris(5.0)).passed is False


def test_usable_frame_gate() -> None:
    assert usable_frame_gate().passed is True
    assert usable_frame_gate(capture_flags=["tiny_file"]).passed is False
    assert usable_frame_gate(capture_flags=["exif_orientation"]).passed is True
    result = usable_frame_gate(camera_fault="dark")
    assert result.passed is False and "camera fault (dark)" in result.reason


def test_frame_gates_name_what_was_not_checked() -> None:
    side = _sidecar("b" * 64, "2026-11-19T20:00:13Z", None, chroma_spread=25.0, flags=[])
    gates = frame_gates(viewpoint=VP, image_time=T, sidecar=side, ephemeris=FixedEphemeris(20.0))
    assert gates.scored and gates.excluded_by == ()
    assert set(gates.not_checked) == {"fresh", "same_view", "clear_lens", "colour_mode"}
    assert gates.to_dict()["results"][0]["gate"] == "fresh"
    night = frame_gates(
        viewpoint=VP,
        image_time=T,
        sidecar=side,
        camera_fault="dark",
        max_chroma_spread=1.0,
        ephemeris=FixedEphemeris(-20.0),
    )
    assert not night.scored and set(night.excluded_by) == {"daylight", "usable_frame"}
    assert night.result("colour_mode").passed is True
    with pytest.raises(ValueError, match="one result per gate"):
        FrameGates((GateResult("daylight", True),))


def test_exclusions_feed_the_scores_mask_and_are_counted() -> None:
    day = frame_gates(viewpoint=VP, image_time=T, ephemeris=FixedEphemeris(20.0))
    night = frame_gates(viewpoint=VP, image_time=T, ephemeris=FixedEphemeris(-5.0))
    fault = frame_gates(
        viewpoint=VP, image_time=T, camera_fault="saturated", ephemeris=FixedEphemeris(20.0)
    )
    gates = [day, night, fault, day]
    mask = excluded_mask(gates)
    assert mask.tolist() == [False, True, True, False]
    table = contingency([True, True, False, False], [True, False, False, False], excluded=mask)
    assert table.excluded == 2 and table.n == 2
    counts = exclusion_counts(gates)
    assert counts["frames"] == 4 and counts["excluded"] == 2
    assert counts["excluded_by"]["daylight"] == 1 and counts["excluded_by"]["usable_frame"] == 1
    assert counts["not_checked"]["same_view"] == 4 and set(counts["excluded_by"]) == set(GATES)
