"""The ``lookout-eval`` console script."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FIXTURES, needs_oculus
from lookout_common.manifest import load_manifest
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
    truth = _frame(data_dir, "archive", valid, "8a1c9d2b", to_rgb(landscape(9)))
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


def test_backtest_pair_held_out_override(
    data_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--held-out` replaces the default sealed window; a held-out truth frame is refused."""
    monkeypatch.setenv("LOOKOUT_EPHEMERIS", "noaa")  # the refusal comes before any Moon
    valid = datetime(2026, 11, 19, 20, tzinfo=UTC)
    truth = _frame(data_dir, "archive", valid, "8a1c9d2b", to_rgb(landscape(9)))
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
        "--landmark-frame-size",
        "640x360",
        "--reference",
        str(ref),
    ]
    code = main([*base, "--held-out", "2026-11-19:2026-11-19"])
    err = capsys.readouterr().err
    assert code == 2 and "LeakageError" in err and "is held out" in err
    with pytest.raises(SystemExit):
        main([*base, "--held-out", "2026-11-19"])
    assert "YYYY-MM-DD:YYYY-MM-DD" in capsys.readouterr().err
