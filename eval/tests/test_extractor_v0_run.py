"""Extractor v0's run and validation (PRD §13.8) on a synthetic data dir and synthetic results."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from synth_images import fogged, landscape, to_rgb
from test_extractor_v0 import peak_and_ground, write_plate

from snow_globe_eval.extractor_v0 import BandResult, FrameResult, V0Params
from snow_globe_eval.extractor_v0_run import binary_scores, run, validate
from snow_globe_eval.labels import FrameLabel, LabelSet

VP = "rainier.paradise-mountain"


def test_binary_scores() -> None:
    s = binary_scores([(True, True)] * 3 + [(True, False)] + [(False, True)] + [(False, False)] * 5)
    assert (s["hits"], s["misses"], s["false_alarms"], s["correct_negatives"]) == (3, 1, 1, 5)
    assert s["f1"] == pytest.approx(0.75) and s["pss"] == pytest.approx(0.75 - 1 / 6, abs=1e-4)
    assert binary_scores([])["f1"] is None


def _result(state: str, frame_class: str = "scored") -> FrameResult:
    bands = {
        "summit": BandResult(
            1.0, 1.0, 1.0 if state == "visible" else 0.45 if state == "partial" else 0.1, state
        )
    }
    return FrameResult(frame_class, None, 20.0, 160.0, bands, None, 1.0, 1.0)


def _labels(
    states: list[str], pool: tuple[int, ...] = (), protocol: dict | None = None
) -> LabelSet:
    frames = {
        f"k{i}": FrameLabel(
            f"k{i}",
            "0" * 64,
            "2026-11-21T18:00:00Z",
            {"summit": s},
            False,
            "overcast",
            "soft",
            i in pool,
        )
        for i, s in enumerate(states)
    }
    return LabelSet(VP, "test", protocol or {}, ("summit",), frames, "test", "0" * 64)


def test_validate_counts_extractor_partial_as_not_visible() -> None:
    labels = _labels(["visible", "visible", "visible", "hidden", "hidden", "partial"])
    results = {
        "k0": _result("visible"),
        "k1": _result("partial"),  # a visible summit called partial: a miss in the primary form
        "k2": _result("visible"),
        "k3": _result("hidden"),
        "k4": _result("hidden"),
        "k5": _result("hidden"),
    }
    v = validate(results, labels, ("summit",), V0Params())
    band = v["bands"]["summit"]
    assert (
        band["label_partial_excluded"]["misses"] == 1 and band["label_partial_excluded"]["n"] == 5
    )
    assert band["both_partial_excluded"]["f1"] == 1.0
    assert band["label_by_class"]["visible"]["partial"] == 1
    assert v["publishable"] is False and any("150" in r for r in v["not_publishable_because"])


def test_validate_leaves_out_the_reference_pool() -> None:
    # k0-k2 are the clear-day pool: labelled visible and scored against one another
    labels = _labels(["visible"] * 5 + ["hidden"], pool=(0, 1, 2))
    results = {f"k{i}": _result("visible") for i in range(4)}
    results["k4"] = _result("hidden")  # a visible summit outside the pool, missed
    results["k5"] = _result("hidden")
    days = {f"k{i}": "2026-11-20" if i < 3 else "2026-11-21" for i in range(6)}
    v = validate(
        results, labels, ("summit",), V0Params(), pool_keys=["k0", "k1", "k2"], local_days=days
    )
    band = v["bands"]["summit"]
    judged = band["label_partial_excluded"]
    assert (judged["n"], judged["hits"], judged["misses"]) == (3, 1, 1)
    assert judged["f1"] == pytest.approx(0.6667) and band["meets_f1_bar"] is False
    assert band["pool_frames_only_sanity_check"]["hits"] == 3
    assert band["diagnostic_pool_days_left_out"]["n"] == 3
    assert v["reference_pool_frames_left_out"] == 3 and v["judged_frames_scored_or_not"] == 3
    # counted as before the fix, the pool would have lifted F1 to 0.889 and over the bar
    everything = validate(results, labels, ("summit",), V0Params())
    assert everything["bands"]["summit"]["label_partial_excluded"]["f1"] == pytest.approx(0.8889)


def test_overridden_thresholds_never_meet_the_bar() -> None:
    labels = _labels(["visible", "hidden"])
    results = {"k0": _result("visible"), "k1": _result("hidden")}
    fixed = validate(results, labels, ("summit",), V0Params())
    assert fixed["bands"]["summit"]["meets_f1_bar"] is True
    assert fixed["thresholds_overridden"] is False
    tuned = validate(results, labels, ("summit",), V0Params(tau_hi=0.5))
    assert tuned["thresholds_overridden"] is True
    assert tuned["bands"]["summit"]["meets_f1_bar"] is False and tuned["publishable"] is False
    assert any("overridden" in r for r in tuned["not_publishable_because"])


def test_publishable_needs_every_condition_of_13_8() -> None:
    labels = _labels(["visible", "hidden"])
    results = {"k0": _result("visible"), "k1": _result("hidden")}
    reasons = validate(results, labels, ("summit",), V0Params())["not_publishable_because"]
    assert any("re-label" in r for r in reasons)
    assert any("second labeller" in r for r in reasons)
    assert any("renders" in r for r in reasons)
    bad_plate = validate(
        results,
        labels,
        ("summit",),
        V0Params(),
        band_validity={"summit": {"valid": False, "not_valid_because": "skyline off"}},
    )
    assert bad_plate["bands"]["summit"]["plate_band_valid"] is False
    assert any("not valid (skyline off)" in r for r in bad_plate["not_publishable_because"])


def test_validate_counts_unscored_frames() -> None:
    labels = _labels(["visible", "hidden"])
    results = {"k0": _result("visible", "no_reference"), "k1": _result("hidden")}
    v = validate(results, labels, ("summit",), V0Params())
    assert v["bands"]["summit"]["labelled_not_scored"] == {"no_reference": 1}
    assert v["scored_sample"]["scored"] == 1


class _DayNight:
    """Test ephemeris: Sun at 21 deg between 16 and 23 UTC, else below the horizon."""

    name = "test-day-night"
    version = "0"

    def sun_alt_az(self, moment, lat_deg, lon_deg, height_m=0.0):
        hour = moment.astimezone(UTC).hour
        return (21.0, 160.0) if 16 <= hour < 23 else (-20.0, 0.0)

    def moon_alt_az_illum(self, moment, lat_deg, lon_deg, height_m=0.0):
        return -30.0, 90.0, 0.5


def _frame(
    root: Path, stamp: str, image: Image.Image, sidecar_sha: str | None = None
) -> tuple[str, str]:
    day = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    folder = root / "frames" / VP / day
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "tmp.jpg"
    image.save(tmp, quality=95)
    sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
    path = folder / f"{stamp}_{sha[:12]}.jpg"
    tmp.rename(path)
    t = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    side = {
        "viewpoint_id": VP,
        "source": "live",
        "image_time_utc": t.isoformat().replace("+00:00", "Z"),
        "fetched_at_utc": t.isoformat().replace("+00:00", "Z"),
        "sha256": sidecar_sha or sha,
    }
    path.with_suffix(".json").write_text(json.dumps(side))
    return f"frames/{VP}/{day}/{path.name}", sha


def _label(key: str, sha: str, stamp: str, peak: str, clear: bool) -> dict:
    return {
        "key": key,
        "sha256": sha,
        "image_time_utc": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:00Z",
        "bands": {"peak": peak, "ground": "visible"},
        "camera_in_cloud": False,
        "sky": "clear",
        "light": "direct",
        "clear_day_reference": clear,
    }


def _write_labels(path: Path, frames: list[dict]) -> Path:
    doc = {
        "kind": "snow-globe.frame-labels",
        "version": "t",
        "viewpoint_id": VP,
        "bands": ["peak", "ground"],
        "protocol": {"labeller": "test"},
        "frames": frames,
    }
    path.write_text(json.dumps(doc))
    return path


def test_reference_pool_holds_only_frames_fits_may_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    plate_dir = write_plate(root / "plates" / VP / "v0", peak_and_ground(), viewpoint=VP)
    labels = []
    # two clear days after the sealed window: development frames, but past the fit bound
    for s, stamp in enumerate(("20270120T180000Z", "20270120T181500Z"), start=1):
        key, sha = _frame(root, stamp, to_rgb(landscape(s)))
        labels.append(_label(key, sha, stamp, "visible", True))
    key, sha = _frame(root, "20261121T180000Z", to_rgb(landscape(9)))
    labels.append(_label(key, sha, "20261121T180000Z", "visible", False))
    report = run(
        data_root=root,
        viewpoint_id=VP,
        plate_dir=plate_dir,
        labels_path=_write_labels(tmp_path / "labels.json", labels),
        out_dir=tmp_path / "out",
        ephemeris=_DayNight(),
    )
    summary = report["summary"]
    assert summary["reference_pool"] == []
    assert len(summary["clear_day_refused_for_fits"]) == 2
    assert summary["frame_classes"] == {"no_reference": 3}


def test_frames_failing_a_gate_are_excluded_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    scene = landscape(1)
    plate_dir = write_plate(
        root / "plates" / VP / "v0", peak_and_ground(), viewpoint=VP, base=to_rgb(scene)
    )
    labels = []
    for k, stamp in enumerate(("20261120T180000Z", "20261120T181500Z", "20261120T183000Z")):
        key, sha = _frame(root, stamp, to_rgb(np.clip(scene + 0.01 * k, 0, 1)))
        labels.append(_label(key, sha, stamp, "visible", True))
    first_key, first_sha = _frame(root, "20261121T180000Z", to_rgb(scene))
    # the next capture repeats the previous image's SHA-256: Fresh fails
    stale_key, _ = _frame(
        root, "20261121T181500Z", to_rgb(np.clip(scene + 0.02, 0, 1)), sidecar_sha=first_sha
    )
    # the camera moved: the scene sits 24 px to the right of the plate
    moved_key, _ = _frame(root, "20261121T183000Z", to_rgb(np.roll(scene, 24, axis=1)))
    report = run(
        data_root=root,
        viewpoint_id=VP,
        plate_dir=plate_dir,
        labels_path=_write_labels(tmp_path / "labels.json", labels),
        out_dir=tmp_path / "out",
        ephemeris=_DayNight(),
    )
    rows = {
        json.loads(line)["key"]: json.loads(line)
        for line in (tmp_path / "out" / "results.jsonl").read_text().splitlines()
    }
    assert rows[first_key]["frame_class"] == "scored"
    assert rows[stale_key]["frame_class"] == "excluded" and "fresh" in rows[stale_key]["reason"]
    assert rows[moved_key]["frame_class"] == "excluded"
    assert "same_view" in rows[moved_key]["reason"]
    assert rows[moved_key]["same_view_shift"]["dx_px"] == pytest.approx(24, abs=2)
    gates = report["summary"]["gates_daytime_frames"]
    assert gates["excluded_by"]["fresh"] == 1 and gates["excluded_by"]["same_view"] == 1
    assert gates["not_checked"]["clear_lens"] == 6 and gates["not_checked"]["colour_mode"] == 6
    assert report["summary"]["code"]["files_sha256"]["extractor_v0_run.py"]


def test_run_end_to_end_on_a_synthetic_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    plate_dir = write_plate(root / "plates" / VP / "v0", peak_and_ground(), viewpoint=VP)
    frames = {}
    for s, stamp in enumerate(
        ("20261120T180000Z", "20261120T181500Z", "20261120T183000Z"), start=1
    ):
        frames[stamp] = (_frame(root, stamp, to_rgb(landscape(s))), "clear")
    frames["20261121T180000Z"] = (_frame(root, "20261121T180000Z", to_rgb(landscape(9))), "visible")
    veil = np.where(np.arange(360)[:, None] < 210, fogged(landscape(10), 0.85), landscape(10))
    frames["20261121T181500Z"] = (_frame(root, "20261121T181500Z", to_rgb(veil)), "capped")
    frames["20261121T060000Z"] = (_frame(root, "20261121T060000Z", to_rgb(landscape(11))), "night")
    label_frames = []
    for stamp, ((key, sha), kind) in frames.items():
        if kind == "night":
            continue
        summit = "hidden" if kind == "capped" else "visible"
        label_frames.append(
            {
                "key": key,
                "sha256": sha,
                "image_time_utc": (
                    f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:00Z"
                ),
                "bands": {"peak": summit, "ground": "visible"},
                "camera_in_cloud": False,
                "sky": "clear",
                "light": "direct",
                "clear_day_reference": kind == "clear",
            }
        )
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps(
            {
                "kind": "snow-globe.frame-labels",
                "version": "t",
                "viewpoint_id": VP,
                "bands": ["peak", "ground"],
                "protocol": {"labeller": "test"},
                "frames": label_frames,
            }
        )
    )
    out = tmp_path / "out"
    report = run(
        data_root=root,
        viewpoint_id=VP,
        plate_dir=plate_dir,
        labels_path=labels_path,
        out_dir=out,
        ephemeris=_DayNight(),
    )
    summary, validation = report["summary"], report["validation"]
    assert summary["frame_classes"] == {"no_reference": 3, "scored": 2, "night": 1}
    assert (
        summary["daytime_without_usable_reference"] == 3
    )  # the clear day has no other-day reference
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    by_time = {r["image_time_utc"][:16]: r for r in rows}
    assert by_time["2026-11-21T18:00"]["bands"]["peak"]["class"] == "visible"
    assert by_time["2026-11-21T18:15"]["bands"]["peak"]["class"] == "hidden"
    assert by_time["2026-11-21T18:15"]["obscuration_height"] == "ground"
    assert validation["bands"]["peak"]["label_partial_excluded"]["f1"] == 1.0
    assert (out / "ratio_series.csv").read_text().count("\n") == 7
    assert json.loads((out / "summary.json").read_text())["counts_toward_metrics"] is False
