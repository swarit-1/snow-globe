"""The whole ephemeris validation, offline, on the recorded Horizons day (PLAN.md M2)."""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest
from conftest import FIXTURES, needs_oculus
from snow_globe_common.manifest import load_manifests

from snow_globe_eval.cli import main
from snow_globe_eval.ephem_validate.plan import PLAN_SCHEMA, Site, load_or_create_plan
from snow_globe_eval.ephem_validate.run import (
    RunOptions,
    _manifest_drift,
    _verdict,
    refraction_reaches,
    run_validation,
    verdict_met,
)
from snow_globe_eval.ephemeris import refraction_deg

HORIZONS = FIXTURES / "ephem" / "horizons"
SKYFIELD = FIXTURES / "ephem" / "skyfield"


def _cache(tmp_path: Path) -> Path:
    """A cache holding the recorded day's four tables and a plan naming that day and site."""
    root = tmp_path / "cache"
    (root / "horizons").mkdir(parents=True)
    for path in HORIZONS.glob("fixture.rainier.*.20261121.*.json"):
        shutil.copy(path, root / "horizons" / path.name)
    plan = {
        "schema": PLAN_SCHEMA,
        "created_utc": "2026-11-23T00:00:00Z",
        "source": "test: the recorded fixtures' site",
        "period": {
            "start_utc": "2026-11-21T00:00:00Z",
            "stop_utc": "2026-11-22T00:00:00Z",
            "step": "1h",
        },
        "sites": [
            {
                "viewpoint_id": "rainier.paradise-mountain",
                "lat_deg": 46.78455,
                "lon_deg": -121.74065,
                "height_m": 1643.0,
                "time_zone": "America/Los_Angeles",
                "view_azimuth_deg": 348.8,
            }
        ],
    }
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return root


def test_refraction_reaches_the_sun_target_where_eval_says() -> None:
    h = refraction_reaches(0.05)
    assert refraction_deg(h - 0.01) > 0.05 > refraction_deg(h + 0.01)
    assert 15.0 < h < 25.0


@needs_oculus
def test_offline_run_on_the_recorded_day(tmp_path: Path) -> None:
    options = RunOptions(
        cache_root=_cache(tmp_path),
        reports_root=tmp_path / "reports",
        offline=True,
        skyfield=False,
        out_dir=tmp_path / "reports" / "run",
    )
    report = run_validation(options, log=lambda _msg: None)
    out = Path(report["out_dir"])
    assert {p.name for p in out.iterdir()} == {
        "report.json",
        "report.md",
        "beyond_target.jsonl",
        "refracted_below_cutoff.jsonl.gz",
        "events.jsonl",
    }
    saved = json.loads((out / "report.json").read_text())
    assert saved["schema"] == "snow-globe.ephem-validate-report/2"
    for body in ("sun", "moon"):
        verdict = saved["verdict"][body]
        assert verdict["geometric_met"] and verdict["refracted_met"], verdict
        assert verdict["horizons_geometric_worst_deg"] <= verdict["target_deg"]
        block = saved["horizons"]["summary"][body]["all_sites"]
        assert block["refraction_model_diff"]["worst"] * 3600.0 < 1e-4  # Horizons = eval's formula
        outside = saved["beyond_target"]["outside_domain_horizons"][body]
        assert outside["n"] == block["refracted_outside_domain"]["n"]
        assert outside["beyond_target"] + outside["agree"] == outside["n"]
    assert verdict_met(saved["verdict"])
    assert saved["verdict"]["sunrise_sunset"]["met"]
    assert saved["sunrise_sunset"]["n_rows"] == 2 * 2  # two local days, sunrise and sunset
    assert (out / "beyond_target.jsonl").read_text() == ""
    below = gzip.decompress((out / "refracted_below_cutoff.jsonl.gz").read_bytes()).decode()
    assert all(json.loads(line)["domain"] == "outside" for line in below.splitlines())
    assert saved["skyfield"] is None
    dip = saved["dip_band"]["rainier.paradise-mountain"]
    assert dip["dip_deg"] == pytest.approx(1.30, abs=0.01) and dip["hfov_deg"] > 0  # 1,643 m
    assert saved["app_core_refraction_offset_deg"] * 3600.0 == pytest.approx(-0.1157, abs=1e-4)
    cam = saved["camera"]
    if cam["renderer"]["ran"]:
        assert cam["parity_directions"]["front_mismatches"] == 0
        assert cam["parity_renderer_vs_independent_px"]["n"] == cam["parity_directions"]["in_front"]
    assert saved["findings"] and "Sunrise and sunset" in saved["findings"][-1]
    assert any(f.startswith("D-002's worst cases") for f in saved["findings"])
    assert any("the same formula" in f for f in saved["findings"])
    text = (out / "report.md").read_text()
    assert "## Verdict" in text and "rainier.paradise-mountain" in text
    assert "## Between each camera's horizon dip" in text and "out of sight" not in text
    with pytest.raises(ValueError, match="never overwritten"):
        run_validation(options, log=lambda _msg: None)


def _add_skyfield(cache: Path) -> str:
    """The recorded Skyfield job's answer in the cache, and a stand-in kernel with its record.

    The job key counts the kernel by its SHA-256 (from the record), not its bytes or path, so
    the recorded answer is found and nothing runs; the stand-in is never opened.
    """
    folder = cache / "skyfield"
    (folder / "kernels").mkdir(parents=True)
    for path in SKYFIELD.glob("*.json"):
        shutil.copy(path, folder / path.name)
    (job_path,) = sorted(SKYFIELD.glob("input-*.json"))
    job = json.loads(job_path.read_text())
    (folder / "kernels" / "de421.bsp").write_bytes(b"DAF/SPK stand-in for the tests")
    record = {"url": "test", "size_bytes": 16788480, "sha256": job["kernel_sha256"]}
    (folder / "kernels" / "de421.bsp.json").write_text(json.dumps(record))
    (out_path,) = sorted(SKYFIELD.glob("output-*.json"))
    return out_path.name


@needs_oculus
def test_offline_run_with_the_recorded_skyfield_job(tmp_path: Path) -> None:
    """Review finding: the Skyfield stage of a run (job, comparisons, references, report)."""
    cache = _cache(tmp_path)
    output_name = _add_skyfield(cache)
    options = RunOptions(
        cache_root=cache,
        reports_root=tmp_path / "reports",
        offline=True,
        camera=False,
        sample_count=5,
        out_dir=tmp_path / "out",
    )
    report = run_validation(options, log=lambda _msg: None)
    saved = json.loads((Path(report["out_dir"]) / "report.json").read_text())
    sky = saved["skyfield"]
    assert sky["output_file"] == output_name  # the recorded answer, found by the job's key
    assert sky["samples"]["n"] == 5 and sky["samples"]["canonical"] is False
    assert sky["versions"]["skyfield"] == "1.55" and sky["isolated"] is True
    agree = sky["reference_agreement_skyfield_vs_horizons"]
    for body in ("sun", "moon"):
        assert agree[body]["horizons_minus_eval_model"]["worst"] * 3600.0 < 1e-4
        assert agree[body]["geometric"]["worst"] * 3600.0 < 5.0
        v = saved["verdict"][body]
        assert v["skyfield_geometric_worst_deg"] <= v["target_deg"] and v["met"]
    rows = [
        json.loads(line) for line in (tmp_path / "out" / "events.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 4 and all("skyfield_utc" in r for r in rows)
    assert saved["sunrise_sunset"]["all"]["ours_minus_skyfield_s"]["worst"] < 5.0
    assert any("Skyfield's Bennett formula" in f for f in saved["findings"])
    assert any("against Skyfield" in f for f in saved["findings"] if f.startswith("D-002"))
    text = (tmp_path / "out" / "report.md").read_text()
    assert "## Skyfield (DE421), 5 random places and instants" in text
    assert "## The two references against each other" in text
    assert "Refraction models alone by band, Sun: Skyfield" in text
    assert "2027-01-23" in text and "2026-11-20" in text


def _shift_refracted_row(cache: Path, degrees: float) -> None:
    """Move the Sun's first refracted elevation of the recorded day by ``degrees`` (a test)."""
    (path,) = sorted((cache / "horizons").glob("fixture.rainier.sun.refracted.20261121.*.json"))
    record = json.loads(path.read_text())
    lines = record["response"]["result"].splitlines()
    i = next(k for k, line in enumerate(lines) if line.strip() == "$$SOE") + 1
    parts = lines[i].split(",")
    parts[4] = f"{float(parts[4]) + degrees:.9f}"
    lines[i] = ",".join(parts)
    record["response"]["result"] = "\n".join(lines)
    path.write_text(json.dumps(record))


@needs_oculus
def test_a_refracted_error_in_the_domain_misses_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A 1 deg refracted error at the Sun's 3 deg (in the domain) fails the verdict, exit 1."""
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    cache = _cache(tmp_path)
    _shift_refracted_row(cache, 1.0)
    out = tmp_path / "out"
    flags = ["--offline", "--no-skyfield", "--no-camera", "--cache", str(cache), "--out", str(out)]
    code = main(["ephem-validate", *flags])
    assert code == 1
    printed = capsys.readouterr().out
    summary = json.loads(printed[printed.index("{") :])
    assert summary["all_met"] is False
    assert summary["verdict"]["sun"]["refracted_met"] is False
    assert summary["verdict"]["sun"]["geometric_met"] is True
    assert summary["verdict"]["moon"]["met"] is True
    (row,) = [json.loads(line) for line in (out / "beyond_target.jsonl").read_text().splitlines()]
    assert row["time_utc"] == "2026-11-21T00:00:00Z" and row["quantity"] == "refracted"
    assert row["cause"] == "refraction model or ephemeris"
    assert row["sep_deg"] == pytest.approx(1.0, abs=0.01)
    text = (out / "report.md").read_text()
    assert "**no** (1)" in text
    assert (
        "| horizons | rainier.paradise-mountain | sun | refracted | 2026-11-21T00:00:00Z |" in text
    )
    saved = json.loads((out / "report.json").read_text())
    assert any(f.startswith("Cases beyond target inside the domain") for f in saved["findings"])


def test_verdict_counts_cases_in_the_domain_and_needs_every_target() -> None:
    horizons = {
        b: {
            "all_sites": {
                "geometric": {"worst": 0.001, "p99": 0.001},
                "refracted": {"worst": 0.65, "p99": 0.001},
                "cutoff_straddles": 1,
            }
        }
        for b in ("sun", "moon")
    }
    events = {
        "tolerance_s": 60.0,
        "all": {"ours_minus_noaa_s": {"worst": 3.0, "beyond_60_s": 0}},
        "missing_ours": 0,
        "missing_noaa": 0,
    }
    beyond = {"horizons/sun/refracted/target": 1, "horizons/moon/refracted/outside": 500}
    v = _verdict(horizons, None, events, beyond)
    assert (v["sun"]["geometric_met"], v["sun"]["refracted_met"], v["sun"]["met"]) == (
        True,
        False,
        False,
    )
    assert v["sun"]["refracted_beyond_target_in_domain"] == 1
    assert v["moon"]["met"] is True  # cases outside the domain do not count
    assert v["sunrise_sunset"]["met"] is True and verdict_met(v) is False
    v = _verdict(horizons, None, {**events, "missing_ours": 1}, {})
    assert v["sunrise_sunset"]["met"] is False and verdict_met(v) is False
    assert verdict_met(_verdict(horizons, None, events, {})) is True


def test_a_new_plan_comes_from_the_manifests_and_is_then_frozen(tmp_path: Path) -> None:
    ids = ["point-reyes.beach", "acadia.mcfarland"]
    sites, doc = load_or_create_plan(tmp_path, ids=ids)
    manifests = load_manifests()
    assert sites == [Site.from_viewpoint(manifests[i]) for i in sorted(ids)]
    saved = json.loads((tmp_path / "plan.json").read_text())
    assert saved == doc and saved["schema"] == PLAN_SCHEMA and saved["period"]["step"] == "1h"
    saved["sites"][0]["lat_deg"] += 0.01  # as if the manifest had moved since: the plan holds
    (tmp_path / "plan.json").write_text(json.dumps(saved))
    again, _ = load_or_create_plan(tmp_path)
    assert again[0].lat_deg == pytest.approx(sites[0].lat_deg + 0.01)
    replanned, _ = load_or_create_plan(tmp_path, replan=True, ids=ids)
    assert replanned == sites
    with pytest.raises(ValueError, match="no manifest"):
        load_or_create_plan(tmp_path / "other", ids=["nowhere.test"])
    (tmp_path / "plan.json").write_text(json.dumps({"schema": "other"}))
    with pytest.raises(ValueError, match="not an ephemeris-validation plan"):
        load_or_create_plan(tmp_path)


def test_manifest_drift_reports_a_moved_site_only() -> None:
    vp = load_manifests()["point-reyes.beach"]
    here = Site.from_viewpoint(vp)
    assert _manifest_drift([here]) == []
    moved = Site(
        here.viewpoint_id,
        here.lat_deg + 0.009,
        here.lon_deg,
        here.height_m + 30.0,
        here.time_zone,
        here.view_azimuth_deg,
    )
    (entry,) = _manifest_drift([moved])
    assert entry["moved_m"] == pytest.approx(1000.0, rel=0.01)
    assert entry["height_change_m"] == -30.0
    assert entry["current"]["lat_deg"] == vp.camera.lat
    (gone,) = _manifest_drift([Site("nowhere.test", 0.0, 0.0, 0.0, "UTC", 0.0)])
    assert gone == {"viewpoint_id": "nowhere.test", "current": None}


@needs_oculus
def test_cli_runs_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    cache = _cache(tmp_path)
    code = main(
        [
            "ephem-validate",
            "--offline",
            "--no-skyfield",
            "--no-camera",
            "--cache",
            str(cache),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    summary = json.loads(printed[printed.index("{") :])
    assert summary["verdict"]["sun"]["geometric_met"] is True
    assert (tmp_path / "out" / "report.md").is_file()


def test_cli_offline_without_a_cache_fails_cleanly(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    cache = _cache(tmp_path)
    for path in (cache / "horizons").iterdir():
        path.unlink()
    code = main(["ephem-validate", "--offline", "--cache", str(cache)])
    assert code == 2
    assert "fetching is off" in capsys.readouterr().err
