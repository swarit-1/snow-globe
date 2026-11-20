"""The ``lookout-eval`` console script."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FIXTURES, needs_oculus
from lookout_common.manifest import load_manifest
from PIL import Image
from synth_forecast import low_deck, make_doc
from synth_images import PEAK_BOX, landscape, to_rgb

from lookout_eval.cli import main

VP_ID = "rainier.paradise-mountain"
REPO_RENDERER = Path(__file__).resolve().parents[2] / "renderer"


@needs_oculus
def test_ephem_check_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "report.json"
    code = main(
        [
            "ephem-check",
            "--search",
            str(FIXTURES / "nps_search_pore_20261117_subset.json"),
            "--json",
            str(out),
        ]
    )
    assert code == 0
    assert "verdict: refracted" in capsys.readouterr().out
    assert json.loads(out.read_text())["verdict"] == "refracted"


def _frame(root: Path, kind: str, moment: datetime, tag: str, image) -> Path:
    folder = root / kind / VP_ID / f"{moment:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{moment:%Y%m%dT%H%M%SZ}_{tag}.jpg"
    image.save(path, quality=92)
    return path


@needs_oculus
def test_backtest_pair_scene_only(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = datetime(2026, 11, 18, 12, tzinfo=UTC)
    valid = datetime(2026, 11, 19, 20, tzinfo=UTC)
    refs = [
        _frame(data_dir, "frames", valid - timedelta(days=d), f"{d:012x}", to_rgb(landscape(d)))
        for d in (2, 3)
    ]
    listing = tmp_path / "refs.txt"
    listing.write_text("\n".join(str(p) for p in refs) + "\n# a comment\n")
    truth = _frame(data_dir, "frames", valid, "8a1c9d2b", to_rgb(landscape(9)))
    forecast = tmp_path / "2026111812z.json"
    forecast.write_text(json.dumps(make_doc(load_manifest(VP_ID), run, [32], low_deck)))
    renderer = tmp_path / "renderer"
    (renderer / "src").mkdir(parents=True)
    (renderer / "package.json").write_text("{}")
    shutil.copy(REPO_RENDERER / "src" / "version.ts", renderer / "src" / "version.ts")
    code = main(
        [
            "backtest-pair",
            "--viewpoint",
            VP_ID,
            "--valid",
            "2026-11-19T20:00:00Z",
            "--lead",
            "24",
            "--truth",
            str(truth),
            "--forecast",
            str(forecast),
            "--landmark-box",
            ",".join(str(v) for v in PEAK_BOX),
            "--landmark-frame-size",
            "640x360",
            "--references-file",
            str(listing),
            "--renderer-dir",
            str(renderer),
            "--hfov",
            "50",
        ]
    )
    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and summary["status"] == "scene-only"  # no schema in this renderer dir
    record = json.loads(Path(summary["record"]).read_text())
    assert Path(summary["record"]).is_relative_to(data_dir)
    scene = json.loads(Path(record["scene"]["path"]).read_text())
    assert scene["camera"]["hfov_deg"] == 50.0
    assert len(record["inputs"]["references"]) == 2


def test_errors_exit_with_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lookout_eval import ephem_check
    from lookout_eval.ephemeris import EphemerisUnavailableError

    def unavailable(name=None):
        raise EphemerisUnavailableError("oculus-core is not importable (test)")

    monkeypatch.setattr(ephem_check, "get_ephemeris", unavailable)  # as in a CI venv
    code = main(["ephem-check", "--search", str(tmp_path / "missing.json")])
    assert code == 2
    assert "FileNotFoundError" in capsys.readouterr().err


def test_backtest_pair_held_out_adds_days_and_guards_the_truth_before_opening_it(
    data_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--held-out` adds days to the committed split; a held-out truth is refused unopened."""
    monkeypatch.setenv("LOOKOUT_EPHEMERIS", "noaa")  # the refusal comes before any Moon
    valid = datetime(2026, 11, 19, 20, tzinfo=UTC)
    truth = _frame(data_dir, "frames", valid, "8a1c9d2b", to_rgb(landscape(9)))
    ref = _frame(data_dir, "frames", valid - timedelta(days=2), "0" * 12, to_rgb(landscape(2)))
    base = [
        "backtest-pair",
        "--viewpoint",
        VP_ID,
        "--valid",
        "2026-11-19T20:00:00Z",
        "--lead",
        "24",
        "--truth",
        str(truth),
        "--forecast",
        str(tmp_path / "unused.json"),
        "--landmark-box",
        ",".join(str(v) for v in PEAK_BOX),
        "--reference",
        str(ref),
    ]
    opened: list[str] = []
    real_open = Image.open
    monkeypatch.setattr(Image, "open", lambda p, *a, **k: opened.append(str(p)) or real_open(p))
    code = main([*base, "--held-out", "2026-11-19:2026-11-19"])
    err = capsys.readouterr().err
    assert code == 2 and "LeakageError" in err and "is held out" in err
    assert opened == []  # refused before the CLI opened the truth for its size
    # an archived truth from 19 Nov 2026 is past the archive guard (D-019)
    archived = _frame(data_dir, "archive", valid, "8a1c9d2b", to_rgb(landscape(9)))
    code = main([*base[:8], str(archived), *base[9:]])
    assert code == 2 and "archive guard (D-019)" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main([*base, "--held-out", "2026-11-19"])
    assert "YYYY-MM-DD:YYYY-MM-DD" in capsys.readouterr().err


def test_heldout_prints_the_committed_split(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["heldout"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["kind"] == "lookout.heldout-periods" and summary["version"] >= 1
    rainier = summary["viewpoints"][VP_ID]
    assert rainier["held_out_utc"] == [["2026-12-15T08:00:00Z", "2027-01-15T08:00:00Z"]]
    assert summary["viewpoints"]["grand-teton.science-center"]["scored"] is False


def test_heldout_check_reports_each_frame(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dev = _frame(
        data_dir, "frames", datetime(2026, 11, 19, 20, tzinfo=UTC), "1" * 12, to_rgb(landscape(1))
    )
    summer = data_dir / "archive" / "point-reyes.beach" / "2026-07-15"
    summer.mkdir(parents=True)
    july = summer / "20260715T201500Z_c7dc841d.jpg"
    july.write_bytes(b"not opened")  # the check reads metadata only
    july.with_suffix(".json").write_text(
        json.dumps(
            {
                "viewpoint_id": "point-reyes.beach",
                "source": "nps-archive",
                "image_create_datetime_raw": "07/15/2026 12:15:00 PM",
                "local_time_string": "7/15/2026 1:15 PM Pacific Daylight Time",
                "image_time_utc": "2026-07-15T20:15:00Z",
            }
        )
    )
    quarantined = data_dir / "quarantine" / "x" / "frames" / VP_ID / "2026-11-18"
    quarantined.mkdir(parents=True)
    (quarantined / "20261118T200000Z_222222222222.jpg").write_bytes(b"")
    code = main(
        [
            "heldout",
            "--check",
            str(dev),
            "--check",
            str(july),
            "--check",
            str(quarantined / "20261118T200000Z_222222222222.jpg"),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    first, second, third = report["frames"]
    assert first["development"] and first["fit"] and first["scoring_exclusion"] is None
    assert not second["development"] and second["archive"]
    assert "summer-2026" in second["reasons"][0]
    assert "D-022" in second["scoring_exclusion"]
    assert not third["development"] and "D-022" in third["reasons"][0]
    assert main(["heldout", "--check", str(dev)]) == 0


def test_heldout_check_refuses_a_missing_file(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding: a path that does not exist was reported usable, with exit 0."""
    missing = data_dir / "frames" / VP_ID / "2026-11-19" / "20261119T200000Z_abcdef012345.jpg"
    assert main(["heldout", "--check", str(missing)]) == 1
    (verdict,) = json.loads(capsys.readouterr().out)["frames"]
    assert not verdict["development"] and not verdict["fit"]
    assert "does not exist" in verdict["reasons"][0]


def test_heldout_check_fit_follows_the_design(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding: no test expected fit False. A Point Reyes live frame of 19 Nov 2026 is
    after summer 2026, so only design P's bound (15 Dec 2026) lets a fit use it."""
    folder = data_dir / "frames" / "point-reyes.beach" / "2026-11-19"
    folder.mkdir(parents=True)
    frame = folder / "20261119T200000Z_abcdef012345.jpg"
    frame.write_bytes(b"not opened")  # metadata only
    frame.with_suffix(".json").write_text(
        json.dumps({"viewpoint_id": "point-reyes.beach", "source": "live"})
    )
    verdicts = {}
    for design in (None, "H", "P"):
        argv = ["heldout", "--check", str(frame)] + ([] if design is None else ["--design", design])
        assert main(argv) == 0  # development use is fine either way
        (verdicts[design],) = json.loads(capsys.readouterr().out)["frames"]
    for design in (None, "H"):
        assert verdicts[design]["development"] and not verdicts[design]["fit"], design
        assert "fit bound 2026-06-01T07:00:00Z" in verdicts[design]["reasons"][0], design
    assert verdicts["P"]["development"] and verdicts["P"]["fit"] and verdicts["P"]["reasons"] == []


@needs_oculus
def test_backtest_pair_with_named_boxes_and_a_tag(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = datetime(2026, 11, 18, 12, tzinfo=UTC)
    valid = datetime(2026, 11, 19, 20, tzinfo=UTC)
    ref = _frame(data_dir, "frames", valid - timedelta(days=2), "0" * 12, to_rgb(landscape(2)))
    truth = _frame(data_dir, "frames", valid, "8a1c9d2b", to_rgb(landscape(9)))
    forecast = tmp_path / "2026111812z.json"
    forecast.write_text(json.dumps(make_doc(load_manifest(VP_ID), run, [32], low_deck)))
    renderer = tmp_path / "renderer"
    (renderer / "src").mkdir(parents=True)
    (renderer / "package.json").write_text("{}")
    shutil.copy(REPO_RENDERER / "src" / "version.ts", renderer / "src" / "version.ts")
    boxes = tmp_path / "boxes.json"
    boxes.write_text(
        json.dumps(
            {
                "kind": "lookout.landmark-boxes",
                "version": "v-1",
                "viewpoints": {
                    VP_ID: {
                        "primary": "peak",
                        "boxes": [{"name": "peak", "box": [0.39, 0.14, 0.61, 0.56]}],
                    }
                },
            }
        )
    )
    base = [
        "backtest-pair",
        "--viewpoint",
        VP_ID,
        "--valid",
        "2026-11-19T20:00:00Z",
        "--lead",
        "24",
        "--truth",
        str(truth),
        "--forecast",
        str(forecast),
        "--reference",
        str(ref),
        "--renderer-dir",
        str(renderer),
    ]
    code = main([*base, "--boxes", str(boxes), "--tag", "run1"])
    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and summary["status"] == "scene-only"
    record = json.loads(Path(summary["record"]).read_text())
    assert record["tag"] == "run1" and summary["record"].endswith(".run1.json")
    assert record["inputs"]["boxes"]["primary"] == "peak"
    assert record["inputs"]["region"]["frame_size"] == [1, 1]
    with pytest.raises(SystemExit):  # one of --landmark-box and --boxes, never both
        main([*base, "--boxes", str(boxes), "--landmark-box", "1,2,3,4"])
    with pytest.raises(SystemExit):
        main(base)
    capsys.readouterr()


def _pair_argv(truth: Path, ref: Path, tmp_path: Path) -> list[str]:
    return [
        "backtest-pair",
        "--viewpoint",
        VP_ID,
        "--valid",
        "2026-11-19T20:00:00Z",
        "--lead",
        "24",
        "--truth",
        str(truth),
        "--forecast",
        str(tmp_path / "unused.json"),
        "--reference",
        str(ref),
        "--scene-only",
    ]


def test_cli_boxes_for_an_uncovered_viewpoint_exit_2(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding (scratch test 5): `--boxes v-1` at a viewpoint the committed file does
    not cover ended in an uncaught KeyError traceback, not exit 2 with a message."""
    valid = datetime(2026, 11, 19, 20, tzinfo=UTC)
    truth = _frame(data_dir, "frames", valid, "8a1c9d2b", to_rgb(landscape(9)))
    ref = _frame(data_dir, "frames", valid - timedelta(days=2), "0" * 12, to_rgb(landscape(2)))
    code = main([*_pair_argv(truth, ref, tmp_path), "--boxes", "v-1"])
    err = capsys.readouterr().err
    assert code == 2 and "ValueError" in err
    assert "has no boxes for rainier.paradise-mountain" in err and "Traceback" not in err


def test_cli_refuses_quarantined_listing_and_boxes_files_unread(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding: `--references-file` and `--boxes FILE` were read with no quarantine
    check (D-022). Both are refused before they are opened: here they are unreadable, so a
    read would fail with another error."""
    valid = datetime(2026, 11, 19, 20, tzinfo=UTC)
    truth = _frame(data_dir, "frames", valid, "8a1c9d2b", to_rgb(landscape(9)))
    ref = _frame(data_dir, "frames", valid - timedelta(days=2), "0" * 12, to_rgb(landscape(2)))
    breach = data_dir / "quarantine" / "held-out-breach-2026-11-19"
    breach.mkdir(parents=True)
    listing = breach / "refs.txt"
    listing.write_text(f"{ref}\n")
    boxes = breach / "boxes.json"
    boxes.write_text(
        json.dumps(
            {
                "kind": "lookout.landmark-boxes",
                "version": "v-1",
                "viewpoints": {
                    VP_ID: {
                        "primary": "peak",
                        "boxes": [{"name": "peak", "box": [0.39, 0.14, 0.61, 0.56]}],
                    }
                },
            }
        )
    )
    for path in (listing, boxes):
        path.chmod(0)
    argv = _pair_argv(truth, ref, tmp_path)
    code = main([*argv, "--boxes", str(boxes)])
    err = capsys.readouterr().err
    assert code == 2 and "LeakageError: landmark boxes" in err and "quarantine" in err
    box = ",".join(str(v) for v in PEAK_BOX)
    code = main([*argv, "--landmark-box", box, "--references-file", str(listing)])
    err = capsys.readouterr().err
    assert code == 2 and "LeakageError: references file" in err and "quarantine" in err
    for path in (listing, boxes):
        path.chmod(0o600)
