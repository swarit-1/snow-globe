"""Labels v0.2 (PRD §13.8): two blind labellers, Cohen's kappa, the merge rule and its guard."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from snow_globe_eval.extractor_v0_2_labels import (
    LABELLER_STATES,
    LABELS_RAINIER_V0_2,
    agreement,
    build_merged_file,
    cohen_kappa,
    load_merged_labels,
    merge,
    read_labeller_file,
    read_sample,
)
from snow_globe_eval.heldout import LeakageError
from snow_globe_eval.labels import load_labels

VP = "rainier.paradise-mountain"


def test_cohen_kappa_matches_a_hand_computation() -> None:
    # 2 x 2 table [[10, 2], [3, 5]]: p_o = 15/20, p_e = (12*13 + 8*7) / 400 = 0.53
    pairs = (
        [("visible", "visible")] * 10
        + [("visible", "hidden")] * 2
        + [("hidden", "visible")] * 3
        + [("hidden", "hidden")] * 5
    )
    k = cohen_kappa(pairs)
    assert k["raw_agreement"] == 0.75 and k["chance_agreement"] == 0.53
    assert k["kappa"] == pytest.approx((0.75 - 0.53) / 0.47, abs=1e-4)
    assert k["table"]["visible"]["hidden"] == 2 and k["table"]["hidden"]["visible"] == 3
    assert k["first_counts"]["visible"] == 12 and k["second_counts"]["visible"] == 13


def test_cohen_kappa_edges() -> None:
    same = [(s, s) for s in LABELLER_STATES for _ in range(3)]
    assert cohen_kappa(same)["kappa"] == 1.0
    assert cohen_kappa([("hidden", "hidden")] * 4)["kappa"] is None  # p_e = 1
    assert cohen_kappa([])["kappa"] is None
    with pytest.raises(ValueError):
        cohen_kappa([("visible", "clear")])


def _entry(summit: str, upper: str = "hidden", mid: str = "hidden", confidence: int = 3) -> dict:
    return {"summit": summit, "upper": upper, "mid": mid, "confidence": confidence, "note": ""}


def test_read_labeller_file_holds_the_protocol_format(tmp_path: Path) -> None:
    path = tmp_path / "labels-x.json"

    def check(doc: object) -> None:
        path.write_text(json.dumps(doc))
        read_labeller_file(path, ["s001", "s002"])

    good = {"s001": _entry("visible"), "s002": _entry("in_cloud", "in_cloud", "in_cloud")}
    check(good)
    for bad in (
        {"s001": _entry("visible")},  # a sheet missing
        {**good, "s003": _entry("hidden")},  # an unknown key
        {**good, "s001": _entry("clear")},  # not a state
        {**good, "s001": {**_entry("visible"), "confidence": True}},
        {**good, "s001": {**_entry("visible"), "confidence": 4}},
        {**good, "s001": {k: v for k, v in _entry("visible").items() if k != "note"}},
        {**good, "s001": {**_entry("visible"), "extra": 1}},
        [good],
    ):
        with pytest.raises(ValueError):
            check(bad)


def _sample(tmp_path: Path, times: list[str]) -> tuple[Path, dict]:
    sheets = []
    for i, t in enumerate(times, start=1):
        stamp = datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%Y%m%dT%H%M%SZ")
        sha = hashlib.sha256(t.encode()).hexdigest()
        sheets.append(
            {
                "sheet_id": f"s{i:03d}",
                "frame_key": f"frames/{VP}/{t[:10]}/{stamp}_{sha[:12]}.jpg",
                "sha256": sha,
                "image_time_utc": t,
                "local_day": t[:10],
            }
        )
    path = tmp_path / "sample.json"
    path.write_text(json.dumps({"kind": "snow-globe.label-sample", "sheets": sheets}))
    return path, read_sample(path)


def test_merge_keeps_agreements_and_nulls_disagreements(tmp_path: Path) -> None:
    _, sample = _sample(tmp_path, ["2026-11-21T18:00:00.000Z", "2026-11-20T18:00:00.000Z"])
    a = {"s001": _entry("visible", "partial"), "s002": _entry("in_cloud")}
    b = {"s001": _entry("visible", "visible"), "s002": _entry("hidden")}
    doc = merge(
        a, b, sample, viewpoint_id=VP, version="v9", protocol={"labellers": {"a": "x", "b": "y"}}
    )
    assert doc["version"] == "v9"  # the sample's version, never a constant
    first, second = doc["frames"]
    assert first["sheet"] == "s002" and second["sheet"] == "s001"  # key (time) order
    assert first["bands"] == {"summit": None, "upper": "hidden", "mid": "hidden"}
    assert second["bands"] == {"summit": "visible", "upper": None, "mid": "hidden"}
    assert second["labellers"]["a"]["upper"] == "partial"
    assert second["labellers"]["b"]["upper"] == "visible"
    report = agreement(a, b)
    assert report["bands"]["summit"]["states"]["agree"] == 1
    assert report["bands"]["summit"]["disagreements"] == [
        {"sheet": "s002", "a": "in_cloud", "b": "hidden"}
    ]
    assert report["bands"]["summit"]["visible_vs_rest"]["kappa"] == 1.0
    with pytest.raises(ValueError):
        merge(a, {"s001": b["s001"]}, sample, viewpoint_id=VP, version="v9", protocol={})
    with pytest.raises(ValueError, match="version"):
        merge(a, b, sample, viewpoint_id=VP, version="", protocol={})


def _write_merged(tmp_path: Path, times: list[str], *, tamper: bool = False) -> Path:
    _, sample = _sample(tmp_path, times)
    a = {s: _entry("visible") for s in sample}
    b = {s: _entry("visible", "partial") for s in sample}
    doc = merge(
        a, b, sample, viewpoint_id=VP, version="t", protocol={"labellers": {"a": "x", "b": "y"}}
    )
    if tamper:
        doc["frames"][0]["bands"]["upper"] = "visible"
    path = tmp_path / "merged.json"
    path.write_text(json.dumps(doc))
    return path


def test_load_merged_labels_checks_the_merge_rule(tmp_path: Path) -> None:
    ok = load_merged_labels(_write_merged(tmp_path, ["2026-11-21T18:00:00.000Z"]))
    frame = next(iter(ok.frames.values()))
    assert frame.bands == {"summit": "visible", "upper": None, "mid": "hidden"}
    assert frame.label("upper", "b") == "partial" and frame.label("upper") is None
    with pytest.raises(ValueError, match="merge rule"):
        load_merged_labels(_write_merged(tmp_path, ["2026-11-21T18:00:00.000Z"], tamper=True))


def test_load_merged_labels_refuses_held_out_frames_and_the_quarantine(tmp_path: Path) -> None:
    # 20 Dec 2026 is in design P's sealed window at Rainier
    with pytest.raises(LeakageError):
        load_merged_labels(_write_merged(tmp_path, ["2026-12-20T20:00:00.000Z"]))
    path = _write_merged(tmp_path, ["2026-11-21T18:00:00.000Z"])
    quarantined = tmp_path / "quarantine" / "merged.json"
    quarantined.parent.mkdir()
    quarantined.write_bytes(path.read_bytes())
    with pytest.raises(LeakageError):
        load_merged_labels(quarantined, roots=(tmp_path,))
    doc = json.loads(path.read_text())
    doc["frames"][0]["image_time_utc"] = "2026-11-21T18:00:09.000Z"  # 9 s from the key
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="from the key"):
        load_merged_labels(path)


def test_the_committed_labels_v0_2() -> None:
    labels = load_merged_labels()
    assert labels.version == "v0.2" and labels.viewpoint_id == VP
    assert labels.bands == ("summit", "upper", "mid") and len(labels.frames) == 105
    assert sorted(labels.protocol["labellers"]) == ["a", "b"]
    days = sorted({f.image_time_utc[:10] for f in labels.frames.values()})
    assert days[0] == "2026-11-19" and days[-1] <= "2026-11-23"
    # the agreement the file records is the agreement of the labels it holds
    for band in labels.bands:
        recorded = labels.protocol["agreement"][band]
        agree = sum(f.label(band, "a") == f.label(band, "b") for f in labels.frames.values())
        nulls = sum(f.bands[band] is None for f in labels.frames.values())
        assert recorded["agree"] == agree and nulls == 105 - agree
    # the legacy loader cannot read the merged file: it checks the kind, not the format, and
    # stops on the first frame's missing camera_in_cloud (a ValueError only once labels.py
    # checks the format; that file is another package's)
    with pytest.raises(KeyError, match="camera_in_cloud"):
        load_labels(LABELS_RAINIER_V0_2)


def test_the_committed_labels_v0_2_are_the_ones_the_preregistration_cites() -> None:
    """Fixed: run 20261123-lodo1's pre-registration (§2) cites the file by SHA-256."""
    digest = hashlib.sha256(LABELS_RAINIER_V0_2.read_bytes()).hexdigest()
    assert digest == "c8c6e3d431f0485c7055e1416a3f9722158a66471ffe44d9b5d6b1eef9fa5c5d"


def _labels_folder(root: Path, stamps: tuple[str, ...]) -> tuple[Path, list[dict]]:
    """A labels folder (sample.json, protocol.md, labels-a/b.json) over synthetic frames."""
    folder = root / "labels" / VP / "v0.3"
    folder.mkdir(parents=True)
    sheets = []
    for i, stamp in enumerate(stamps, start=1):
        day = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
        frames = root / "frames" / VP / day
        frames.mkdir(parents=True, exist_ok=True)
        tmp = frames / "tmp.jpg"
        Image.new("RGB", (64, 36), (i * 40, 90, 120)).save(tmp)
        sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
        path = frames / f"{stamp}_{sha[:12]}.jpg"
        tmp.rename(path)
        t = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        iso = t.isoformat().replace("+00:00", "Z")
        path.with_suffix(".json").write_text(
            json.dumps({"viewpoint_id": VP, "source": "live", "image_time_utc": iso, "sha256": sha})
        )
        sheets.append(
            {
                "sheet_id": f"s{i:03d}",
                "frame_key": f"frames/{VP}/{day}/{path.name}",
                "sha256": sha,
                "image_time_utc": iso,
                "local_day": day,
            }
        )
    sample = {
        "kind": "snow-globe.label-sample",
        "version": "v0.3",
        "viewpoint_id": VP,
        "created_at_utc": "2026-11-30T10:00:00Z",
        "rule": {"taken": "every daytime development capture", "order": "a seeded shuffle"},
        "handover": {"labellers_get": "sheets.txt, the sheet images and protocol.md"},
        "sheets": sheets,
    }
    (folder / "sample.json").write_text(json.dumps(sample))
    (folder / "protocol.md").write_text("# protocol\n")
    a = {"s001": _entry("visible"), "s002": _entry("hidden")}
    (folder / "labels-a.json").write_text(json.dumps(a))
    (folder / "labels-b.json").write_text(json.dumps({**a, "s002": _entry("partial")}))
    return folder, sheets


def test_build_merged_file_checks_every_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    folder, sheets = _labels_folder(root, ("20261121T180000Z", "20261121T181500Z"))
    out = tmp_path / "merged.json"
    report = build_merged_file(folder, out, data_root=root, descriptions={"a": "a test labeller"})
    assert report["bands"]["summit"]["states"]["agree"] == 1
    assert not list(tmp_path.glob("*.tmp"))  # written whole
    merged = load_merged_labels(out)
    assert merged.version == "v0.3"  # from sample.json
    assert [f.bands["summit"] for f in merged.frames.values()] == ["visible", None]
    protocol = merged.protocol
    assert protocol["protocol_md"]["path"] == f"labels/{VP}/v0.3/protocol.md"
    assert "2026-11-21 to 2026-11-21" in protocol["frames"]
    assert "every daytime development capture" in protocol["frames"]
    assert set(protocol["labeller_files_modified_utc"]) == {"a", "b"}
    assert protocol["labellers"]["a"].startswith("a test labeller; ")
    assert "not given to the merge" in protocol["labellers"]["b"]
    assert "cannot check" in protocol["labellers"]["b"]  # instructed, not claimed
    assert protocol["second_labeller"] == "b"
    assert str(root) not in out.read_text()  # paths relative to the data dir only
    # never written over, so the committed labels cannot be rebuilt in place
    before = out.read_bytes()
    with pytest.raises(ValueError, match="never written over"):
        build_merged_file(folder, out, data_root=root)
    with pytest.raises(ValueError, match="never written over"):
        build_merged_file(folder, LABELS_RAINIER_V0_2, data_root=root)
    assert out.read_bytes() == before
    # a frame whose bytes changed since the sample was drawn is refused
    frame = root / sheets[0]["frame_key"]
    frame.write_bytes(frame.read_bytes() + b"\0")
    with pytest.raises(ValueError, match="SHA-256"):
        build_merged_file(folder, tmp_path / "again.json", data_root=root)
    assert not (tmp_path / "again.json").exists()
    # a sample of another viewpoint is refused
    with pytest.raises(ValueError, match=r"not test\.view"):
        build_merged_file(folder, tmp_path / "other.json", data_root=root, viewpoint_id="test.view")


def test_build_merged_file_refuses_a_held_out_frame_before_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    # 20 Dec 2026 is in design P's sealed window at Rainier (a synthetic frame)
    folder, sheets = _labels_folder(root, ("20261121T180000Z", "20261220T200000Z"))
    read: list[Path] = []
    import snow_globe_eval.extractor_v0_2_labels as module

    original = module._sha256

    def spy(path: Path) -> str:
        read.append(Path(path))
        return original(path)

    monkeypatch.setattr(module, "_sha256", spy)
    with pytest.raises(LeakageError, match="not a development frame"):
        build_merged_file(folder, tmp_path / "merged.json", data_root=root)
    held = root / sheets[1]["frame_key"]
    assert held not in read
    assert not (tmp_path / "merged.json").exists()
