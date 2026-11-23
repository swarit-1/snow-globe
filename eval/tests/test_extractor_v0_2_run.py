"""``snow-globe-eval extractor-v0.2`` end to end on a synthetic data dir (PRD §13.8)."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from snow_globe_common.manifest import load_manifest
from synth_v0_2 import scene, to_rgb, write_plate

from snow_globe_eval.extractor_v0 import PlateBands
from snow_globe_eval.extractor_v0_2 import V02Params, load_sky_mask
from snow_globe_eval.extractor_v0_2_labels import (
    MergedFrame,
    SheetFrame,
    load_merged_labels,
    merge,
)
from snow_globe_eval.extractor_v0_2_run import extract_features, register, run
from snow_globe_eval.heldout import LeakageError

VP = "rainier.paradise-mountain"


class _DayNight:
    """Test ephemeris: Sun at 21 deg between 16 and 23 UTC, else below the horizon."""

    name = "test-day-night"
    version = "0"

    def sun_alt_az(self, moment, lat_deg, lon_deg, height_m=0.0):  # type: ignore[no-untyped-def]
        hour = moment.astimezone(UTC).hour
        return (21.0, 160.0) if 16 <= hour < 23 else (-20.0, 0.0)

    def moon_alt_az_illum(self, moment, lat_deg, lon_deg, height_m=0.0):  # type: ignore[no-untyped-def]
        return -30.0, 90.0, 0.5


def _frame(root: Path, stamp: str, image: Image.Image) -> tuple[str, str, str]:
    day = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    folder = root / "frames" / VP / day
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "tmp.jpg"
    image.save(tmp, quality=95)
    sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
    path = folder / f"{stamp}_{sha[:12]}.jpg"
    tmp.rename(path)
    t = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    iso = t.isoformat().replace("+00:00", "Z")
    side = {"viewpoint_id": VP, "source": "live", "image_time_utc": iso, "sha256": sha}
    path.with_suffix(".json").write_text(json.dumps(side))
    return f"frames/{VP}/{day}/{path.name}", sha, iso


KINDS = (
    ("sunny", False, "visible"),
    ("sunny", False, "visible"),
    ("overcast", False, "visible"),
    ("overcast", True, "hidden"),
    ("overcast", True, "hidden"),
    ("sunny", True, "hidden"),
)


def _data_dir(
    tmp_path: Path, days: tuple[str, ...] = ("20261120", "20261121", "20261122")
) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "data"
    plate = write_plate(root / "plates" / VP / "v0.1", viewpoint=VP, base=to_rgb(scene("sunny")))
    sample: dict[str, SheetFrame] = {}
    a: dict[str, dict] = {}
    v0_frames = []
    n = 0
    for day in days:
        for i, (light, cloud, state) in enumerate(KINDS):
            n += 1
            stamp = f"{day}T18{i:02d}00Z"
            key, sha, iso = _frame(root, stamp, to_rgb(scene(light, cloud=cloud, seed=n)))
            sheet = f"s{n:03d}"
            sample[sheet] = SheetFrame(sheet, key, sha, iso, f"{day[:4]}-{day[4:6]}-{day[6:]}")
            a[sheet] = {"summit": state, "upper": state, "mid": state, "confidence": 3, "note": ""}
            v0_frames.append(
                {
                    "key": key,
                    "sha256": sha,
                    "image_time_utc": iso,
                    "bands": {"summit": state, "upper": state},
                    "camera_in_cloud": False,
                    "sky": "clear",
                    "light": "direct",
                    "clear_day_reference": light == "sunny" and state == "visible",
                }
            )
    # night: listed, skipped
    _frame(root, "20261121T060000Z", to_rgb(scene("overcast", seed=99)))
    doc = merge(
        a, a, sample, viewpoint_id=VP, version="t", protocol={"labellers": {"a": "t", "b": "t"}}
    )
    doc["bands"] = ["summit", "upper"]
    for frame in doc["frames"]:
        frame["bands"].pop("mid")
    labels = tmp_path / "merged.json"
    labels.write_text(json.dumps(doc))
    v0_labels = tmp_path / "v0.json"
    v0_labels.write_text(
        json.dumps(
            {
                "kind": "snow-globe.frame-labels",
                "version": "t",
                "viewpoint_id": VP,
                "bands": ["summit", "upper"],
                "protocol": {"labeller": "test"},
                "frames": v0_frames,
            }
        )
    )
    return root, plate, labels, v0_labels


def test_run_refuses_to_score_without_a_preregistration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, plate, labels, v0_labels = _data_dir(tmp_path)
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    with pytest.raises(FileNotFoundError, match="pre-registration"):
        run(
            data_root=root,
            viewpoint_id=VP,
            plate_dir=plate,
            labels_path=labels,
            v0_labels_path=v0_labels,
            out_dir=tmp_path / "out",
            preregistration=tmp_path / "missing.md",
            run_id="t",
            ephemeris=_DayNight(),
        )
    assert not (tmp_path / "out").exists()


def _kwargs(tmp_path: Path, root: Path, plate: Path, labels: Path, v0_labels: Path) -> dict:
    prereg = tmp_path / "preregistration.md"
    if not prereg.exists():
        prereg.write_text("# fixed before scoring\n")
    return {
        "data_root": root,
        "viewpoint_id": VP,
        "plate_dir": plate,
        "labels_path": labels,
        "v0_labels_path": v0_labels,
        "preregistration": prereg,
        "run_id": "t",
        "n_boot": 300,
        "ephemeris": _DayNight(),
    }


OUTPUTS = ("run.json", "folds.json", "scores.json", "report.md", "features.jsonl")


def test_run_end_to_end_and_a_finished_run_is_never_written_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, plate, labels, v0_labels = _data_dir(tmp_path)
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    kwargs = _kwargs(tmp_path, root, plate, labels, v0_labels)
    out = tmp_path / "out"
    doc = run(out_dir=out, **kwargs)
    features = doc["features"]
    assert features["computed"] == 18 and features["reused"] == 0
    assert features["superseded_lines"] == 0
    on_disk = hashlib.sha256((out / "features.jsonl").read_bytes()).hexdigest()
    assert features["jsonl_sha256"] == on_disk
    assert set(features["stamp"]["inputs"]) >= {"code_sha256", "plate_json_sha256", "camera"}
    prereg = kwargs["preregistration"]
    assert doc["preregistration"]["sha256"] == hashlib.sha256(prereg.read_bytes()).hexdigest()
    assert doc["days"] == ["2026-11-20", "2026-11-21", "2026-11-22"]
    assert set(doc["methods"]) == {"v0.2", "v0.2-A", "v0", "v0-fitted-tau"}
    summit = doc["methods"]["v0.2"]["scores"]["bands"]["summit"]["primary"]["pooled"]
    assert summit["n"] == 18 and summit["f1"] == 1.0
    assert doc["bar"]["met"] is True and doc["publishable"] is False
    rows = [json.loads(line) for line in (out / "predictions.jsonl").read_text().splitlines()]
    assert len(rows) == 18 and all(set(r["methods"]) == set(doc["methods"]) for r in rows)
    for name in OUTPUTS:
        assert (out / name).is_file()
    report = (out / "report.md").read_text()
    assert "| v0.2 | summit |" in report and "## Camera in cloud" in report
    assert doc["deployed_fit"]["thresholds"]["summit"] is not None
    cloud = json.loads((out / "run.json").read_text())["methods"]["v0.2"]["camera_in_cloud"]
    assert cloud["band"] == "summit" and cloud["labelled_in_cloud"] == 0
    strat = doc["stratification"]
    assert strat["visible_by_local_day"] == {d: 3 for d in doc["days"]}
    assert any(r.startswith("not stratified") for r in doc["not_publishable_because"])
    # a second run into the finished folder is refused, and nothing in it changes
    before = {name: (out / name).read_bytes() for name in (*OUTPUTS, "predictions.jsonl")}
    with pytest.raises(ValueError, match="finished run"):
        run(out_dir=out, **kwargs)
    assert {name: (out / name).read_bytes() for name in before} == before


def test_a_resumed_run_is_the_uninterrupted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopped after 7 frames, with a line cut short: the resumed run gives the same outputs."""
    root, plate, labels, v0_labels = _data_dir(tmp_path)
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    kwargs = _kwargs(tmp_path, root, plate, labels, v0_labels)
    whole, resumed = tmp_path / "whole", tmp_path / "resumed"
    run(out_dir=whole, **kwargs)
    lines = (whole / "features.jsonl").read_text().splitlines()
    resumed.mkdir()
    (resumed / "features.jsonl").write_text("\n".join(lines[:7]) + "\n" + lines[7][:40])
    doc = run(out_dir=resumed, **kwargs)
    assert (doc["features"]["computed"], doc["features"]["reused"]) == (11, 7)
    assert doc["features"]["superseded_lines"] == 1  # the cut-short line, dropped
    for name in ("features.jsonl", "folds.json", "predictions.jsonl", "scores.json"):
        assert (resumed / name).read_bytes() == (whole / name).read_bytes(), name


def _extract(root: Path, plate: Path, labels: Path, cache: Path, viewpoint=None) -> dict:  # type: ignore[no-untyped-def]
    vp = viewpoint or load_manifest(VP)
    bands = PlateBands.load(plate)
    _, counts = extract_features(
        data_root=root,
        viewpoint=vp,
        plate=bands,
        sky_full=load_sky_mask(plate),
        labels=load_merged_labels(labels),
        params=V02Params(),
        cache_path=cache,
        ephemeris=_DayNight(),  # type: ignore[arg-type]
    )
    return counts


def test_a_changed_input_recomputes_the_cached_features(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, plate, labels, _ = _data_dir(tmp_path, days=("20261121",))
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    cache = tmp_path / "cache" / "features.jsonl"
    assert _extract(root, plate, labels, cache)["computed"] == 6
    assert _extract(root, plate, labels, cache)["reused"] == 6
    # another camera (the Sun, the Daylight gate): every frame again
    vp = load_manifest(VP)
    moved = replace(vp, camera=replace(vp.camera, elevation_m=vp.camera.elevation_m + 50.0))
    assert _extract(root, plate, labels, cache, viewpoint=moved)["computed"] == 6
    # another plate: the sky mask cut to its top quarter, plate.json updated to match
    sky = np.asarray(Image.open(plate / "sky.png")).copy()
    sky[sky.shape[0] // 4 :] = 0
    Image.fromarray(sky).save(plate / "sky.png")
    doc = json.loads((plate / "plate.json").read_text())
    doc["files"]["sky.png"]["sha256"] = hashlib.sha256((plate / "sky.png").read_bytes()).hexdigest()
    (plate / "plate.json").write_text(json.dumps(doc))
    counts = _extract(root, plate, labels, cache)
    assert (counts["computed"], counts["reused"]) == (6, 0)
    # one frame's sidecar changed (and so the next frame's previous capture): two again
    side = next((root / "frames" / VP / "2026-11-21").glob("*T1801*.json"))
    side.write_text(json.dumps({**json.loads(side.read_text()), "http_last_modified": "x"}))
    counts = _extract(root, plate, labels, cache)
    assert (counts["computed"], counts["reused"]) == (2, 4)
    assert len(cache.read_text().splitlines()) == 6  # only the lines used are kept


def test_extract_features_guards_the_split_and_the_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, plate, labels_path, _ = _data_dir(tmp_path, days=("20261121",))
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    cache = tmp_path / "cache" / "features.jsonl"
    labels = load_merged_labels(labels_path)
    common = {
        "data_root": root,
        "viewpoint": load_manifest(VP),
        "plate": PlateBands.load(plate),
        "sky_full": load_sky_mask(plate),
        "params": V02Params(),
        "cache_path": cache,
        "ephemeris": _DayNight(),
    }
    extract_features(labels=labels, **common)  # type: ignore[arg-type]
    # a labelled frame the split holds out (a synthetic frame in the sealed window) is refused
    key, sha, iso = _frame(root, "20261220T200000Z", to_rgb(scene("sunny", seed=5)))
    frame = MergedFrame(key, sha, iso, "s999", {"summit": "visible", "upper": "visible"}, {})
    held = replace(labels, frames={**labels.frames, key: frame})
    with pytest.raises(LeakageError, match="labelled but not development"):
        extract_features(labels=held, **common)  # type: ignore[arg-type]
    (root / key).unlink()
    (root / key).with_suffix(".json").unlink()
    # a labelled frame whose bytes changed is refused, even with its features cached
    first = root / next(iter(sorted(labels.frames)))
    first.write_bytes(first.read_bytes() + b"\0")
    with pytest.raises(ValueError, match="labelled SHA-256"):
        extract_features(labels=labels, **common)  # type: ignore[arg-type]
    # a labelled frame that is gone stops the run
    first.unlink()
    with pytest.raises(FileNotFoundError, match="labelled frames not found"):
        extract_features(labels=labels, **common)  # type: ignore[arg-type]


def test_run_from_args_writes_under_the_data_dir_and_never_over_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import snow_globe_eval.extractor_v0_2_run as module

    root, plate, labels, v0_labels = _data_dir(tmp_path)
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    monkeypatch.setattr(module, "get_ephemeris", _DayNight)
    prereg = tmp_path / "preregistration.md"
    prereg.write_text("# fixed\n")
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    argv = ["extractor-v0.2", "--run-id", "r1", "--n-boot", "50", "--plate", str(plate)]
    argv += ["--labels", str(labels), "--v0-labels", str(v0_labels)]
    argv += ["--preregistration", str(prereg)]
    args = parser.parse_args(argv)
    assert args.handler(args) == 0
    out = root / "scores" / "extractor-v0.2" / VP / "r1"
    assert (out / "run.json").is_file()
    with pytest.raises(ValueError, match="finished run"):
        args.handler(args)
    # --merge-labels writes only to an explicit, new path
    merge_args = parser.parse_args(["extractor-v0.2", "--merge-labels", str(tmp_path)])
    with pytest.raises(ValueError, match="needs --labels"):
        merge_args.handler(merge_args)
    existing = parser.parse_args(
        ["extractor-v0.2", "--merge-labels", str(tmp_path), "--labels", str(labels)]
    )
    with pytest.raises(ValueError, match="never written over"):
        existing.handler(existing)


def test_the_subcommand_registers() -> None:
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["extractor-v0.2", "--run-id", "x", "--n-boot", "10"])
    assert args.run_id == "x" and args.n_boot == 10 and callable(args.handler)
