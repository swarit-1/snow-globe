"""The Skyfield stage without Skyfield: samples, the job, the command, a recorded output (§8.4).

Skyfield is not a dependency of eval; it runs only in an isolated ``uv run --no-project``
environment. These tests check everything around it, and compare oculus-core with a small
recorded Skyfield output (``fixtures/ephem/skyfield``: the job ``run._skyfield_job`` builds for
Rainier's recorded Horizons day with 5 samples: the 25 hourly epochs and four local days of
sunrises and sunsets, 20–23 Nov 2026; recorded on 23 Nov 2026 in the isolated environment,
offline, on the cached DE421). The kernel path in it is shown as ``~``, so the output's
``input_sha256`` names the job file as it was run, before that substitution.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest
from conftest import FIXTURES, needs_oculus
from snow_globe_common.http import MinIntervalLimiter

from snow_globe_eval.ephem_validate import skyfield_run
from snow_globe_eval.ephem_validate.compare import compare_body
from snow_globe_eval.ephem_validate.horizons import parse_result
from snow_globe_eval.ephem_validate.plan import Site
from snow_globe_eval.ephem_validate.run import (
    _reference_agreement,
    _sample_positions,
    _skyfield_comparisons,
    eop_coverage,
)
from snow_globe_eval.ephem_validate.skyfield_run import (
    DE421_URL,
    SKYFIELD_PINS,
    SkyfieldUnavailableError,
    draw_samples,
    ensure_de421,
    job_key,
    kernel_record,
    skyfield_command,
    skyfield_output,
)
from snow_globe_eval.ephem_validate.stats import TARGET_DEG, separation_deg

SKY = FIXTURES / "ephem" / "skyfield"
HORIZONS = FIXTURES / "ephem" / "horizons"


def _fixture() -> tuple[dict, dict]:
    (job_path,) = sorted(SKY.glob("input-*.json"))
    (out_path,) = sorted(SKY.glob("output-*.json"))
    return json.loads(job_path.read_text()), json.loads(out_path.read_text())


def test_samples_are_reproducible_and_in_range() -> None:
    a, b = draw_samples(), draw_samples()
    assert a == b and len(a) == 1000
    lat = np.array([s["lat_deg"] for s in a])
    assert lat.min() >= -90 and lat.max() <= 90
    # Uniform on the sphere: about half the samples within 30 deg of the equator.
    assert 0.45 < np.mean(np.abs(lat) < 30.0) < 0.55
    assert all(0.0 <= s["height_m"] <= 4500.0 for s in a)
    t = [datetime.fromtimestamp(s["unix_ms"] / 1000.0, tz=UTC) for s in a]
    assert min(t).year >= 1998 and max(t) < datetime(2031, 1, 1, tzinfo=UTC)
    assert draw_samples(5) == draw_samples(5)
    assert draw_samples(5, seed=1) != draw_samples(5)


def test_job_key_ignores_the_kernel_path_only() -> None:
    job, _ = _fixture()
    moved = {**job, "kernel": "/elsewhere/de421.bsp"}
    assert job_key(moved) == job_key(job)
    assert job_key({**job, "samples": job["samples"][:2]}) != job_key(job)
    (path,) = sorted(SKY.glob("output-*.json"))
    assert path.name == f"output-{job_key(job)[:20]}.json"


def test_the_isolated_command() -> None:
    command = skyfield_command(Path("ref.py"), Path("in.json"), Path("out.json"), "uv")
    assert command[:5] == ["uv", "run", "--no-project", "--python", "3.13"]
    for pin in SKYFIELD_PINS:
        assert pin in command and "==" in pin
    assert command[-5:] == ["python", "-I", "ref.py", "in.json", "out.json"]


def test_isolated_env_drops_the_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/x/.venv")
    monkeypatch.setenv("PYTHONPATH", "src")
    env = skyfield_run._isolated_env()
    assert "VIRTUAL_ENV" not in env and "PYTHONPATH" not in env


def test_reference_script_imports_only_stdlib_skyfield_and_numpy() -> None:
    """skyfield_ref.py runs under ``python -I`` in its own env: it must not need Snow Globe."""
    source = Path(skyfield_run.__file__).with_name("skyfield_ref.py").read_text()
    allowed = {"__future__", "hashlib", "json", "os", "platform", "sys", "datetime", "importlib"}
    allowed |= {"skyfield", "numpy"}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        assert set(names) <= allowed, names


def test_cached_output_is_read_offline_and_missing_is_refused(tmp_path: Path) -> None:
    job, _ = _fixture()
    folder = tmp_path / "skyfield"
    folder.mkdir()
    for path in SKY.glob("*.json"):
        shutil.copy(path, folder / path.name)
    result = skyfield_output(tmp_path, job, offline=True)
    assert result["versions"]["skyfield"] == "1.55" and result["isolated"] is True
    with pytest.raises(SkyfieldUnavailableError, match="offline"):
        skyfield_output(tmp_path, {**job, "samples": job["samples"][:1]}, offline=True)
    with pytest.raises(SkyfieldUnavailableError, match="fetching is off"):
        ensure_de421(tmp_path, offline=True)


@needs_oculus
def test_oculus_against_the_recorded_skyfield_output() -> None:
    job, out = _fixture()
    rows = {r["id"]: r for r in out["samples"]}
    pos = _sample_positions(job["samples"])
    for body in ("sun", "moon"):
        ref = [rows[s["id"]][body] for s in job["samples"]]
        c = compare_body(
            source="skyfield",
            site_id="fixture",
            body=body,
            unix_ms=pos.unix_ms,
            ref_alt=np.array([r["alt_deg"] for r in ref]),
            ref_az=np.array([r["az_deg"] for r in ref]),
            ref_alt_refr=np.array([r["alt_refr_deg"] for r in ref]),
            ref_az_refr=np.array([r["az_refr_deg"] for r in ref]),
            positions=pos,
        )
        assert float(np.max(c.sep_geo)) <= TARGET_DEG[body]
        assert float(np.max(c.sep_refr[c.refraction_domain], initial=0.0)) <= TARGET_DEG[body]
    # The recorded site epochs: Skyfield against the recorded Horizons day differs only by
    # Earth rotation (a few arcseconds), never by more than the Sun target.
    site = out["sites"][0]
    (path,) = sorted(
        (FIXTURES / "ephem" / "horizons").glob("fixture.rainier.sun.airless.20261121.*")
    )
    from snow_globe_eval.ephem_validate.horizons import parse_result

    table = parse_result(json.loads(path.read_text())["response"]["result"])
    sep = separation_deg(
        site["sun"]["alt_deg"],
        site["sun"]["az_deg"],
        table.values["el_deg"],
        table.values["az_deg"],
    )
    assert float(np.max(sep)) * 3600.0 < 10.0
    kinds = sorted({e["kind"] for e in out["events"]})
    assert kinds == ["rise", "set"] and len(out["events"]) == 8


# --- the run's Skyfield stage on the recorded job (review finding: these had no test) -------


def _horizons_day() -> tuple[list[Site], dict[tuple[str, str, str], object]]:
    site = Site("rainier.paradise-mountain", 46.78455, -121.74065, 1643.0, "America/Los_Angeles", 0)
    tables = {}
    for body in ("sun", "moon"):
        for apparent in ("airless", "refracted"):
            (path,) = sorted(HORIZONS.glob(f"fixture.rainier.{body}.{apparent}.20261121.*.json"))
            record = json.loads(path.read_text())
            tables[(site.viewpoint_id, body, apparent)] = parse_result(record["response"]["result"])
    return [site], tables


@needs_oculus
def test_skyfield_comparisons_on_the_recorded_job() -> None:
    job, out = _fixture()
    items, meta = _skyfield_comparisons(job, out)
    assert [c.body for c in items] == ["sun", "moon"]
    assert meta["n"] == 5 and meta["canonical"] is False and meta["seed"] == 20261123
    for c in items:
        assert len(c) == 5 and c.source == "skyfield"
        assert float(np.max(c.sep_geo)) <= TARGET_DEG[c.body]
        # UT1 - UTC taken out: never worse than |UT1 - UTC| of rotation (0.9 s = 13.5 arcsec).
        ut1 = meta["geometric_with_skyfield_ut1"][c.body]
        assert ut1["n"] == 5 and ut1["worst"] <= float(np.max(c.sep_geo)) + 13.5 / 3600.0
        # The models alone: Skyfield's iterated Bennett against eval's Saemundsson at one altitude.
        dom = c.refraction_domain
        assert float(np.max(np.abs(c.drefraction_model[dom]), initial=0.0)) * 3600.0 < 30.0
    assert "2027-01-23" in meta["note"]
    with pytest.raises(ValueError, match="every sample"):
        _skyfield_comparisons({**job, "samples": job["samples"][:4]}, out)


@needs_oculus
def test_reference_agreement_on_the_recorded_day() -> None:
    _, out = _fixture()
    sites, tables = _horizons_day()
    agree = _reference_agreement(sites, tables, out)
    for body in ("sun", "moon"):
        a = agree[body]
        assert a["geometric"]["n"] == 25 and a["geometric"]["worst"] * 3600.0 < 5.0
        assert a["declination"]["worst"] * 3600.0 < 0.1  # the references differ in rotation only
        assert 0.1 < a["hour_angle_s"]["min"] <= a["hour_angle_s"]["max"] < 0.3
        # Horizons' refraction is eval's formula: at one altitude they agree to 1e-4 arcsec.
        assert a["horizons_minus_eval_model"]["worst"] * 3600.0 < 1e-4
        assert a["skyfield_minus_eval_model"]["n"] == a["refracted_domain"]["n"] > 0
        # 21 Nov 2026 is after Horizons' measured EOP (to 20 Nov), on its predictions.
        assert set(a["hour_angle_minus_skyfield_dut1_spread_s"]) == {"eop_predicted"}
    assert agree["horizons_eop_coverage"] == {
        "data_to": "2026-11-20",
        "predictions_to": "2027-02-15",
    }
    assert agree["skyfield_ut1_table_to"] == "2027-01-23"
    reading = agree["reading"]
    assert "extrapolated after" in reading and "its predictions to 2027-02-15" in reading
    assert "held" not in reading  # no epoch of the recorded day is past Horizons' predictions


def test_eop_coverage_is_read_from_the_header_line() -> None:
    line = "DATA-BASED 1962-JAN-20 TO 2026-NOV-20. PREDICTS-> 2027-FEB-15"
    assert eop_coverage(line) == (date(2026, 11, 20), date(2027, 2, 15))
    assert eop_coverage(None) is None and eop_coverage("no EOP") is None


# --- the DE421 download, on a mock transport (review finding: only offline was tested) ------

_SPK = b"DAF/SPK " + bytes(range(256)) * 39_100  # just over the 10 MB floor


def _fast_limiter() -> MinIntervalLimiter:
    now = [1_000_000.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return MinIntervalLimiter("jpl-test", 2.0, mode="process", clock=clock, sleep=sleep)


def test_de421_is_downloaded_once_and_recorded(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_SPK)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        path = ensure_de421(tmp_path, client=client, limiter=_fast_limiter())
        again = ensure_de421(tmp_path, client=client, limiter=_fast_limiter())
    assert path == again and len(seen) == 1 and str(seen[0].url) == DE421_URL
    assert path.read_bytes() == _SPK
    record = kernel_record(path)
    assert record["sha256"] == hashlib.sha256(_SPK).hexdigest()
    assert record["size_bytes"] == len(_SPK) and record["http_status"] == 200
    assert sorted(p.name for p in path.parent.iterdir()) == ["de421.bsp", "de421.bsp.json"]


def test_de421_backs_off_on_503_then_downloads(tmp_path: Path) -> None:
    answers = [httpx.Response(503, text="busy"), httpx.Response(200, content=_SPK)]
    logs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return answers.pop(0)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        path = ensure_de421(tmp_path, client=client, limiter=_fast_limiter(), log=logs.append)
    assert answers == [] and path.is_file()
    assert any("retrying after the back-off" in line for line in logs)


@pytest.mark.parametrize(
    ("status", "body", "match"),
    [
        (200, b"DAF/SPK too short", "not an SPK kernel"),
        (200, b"<html>" + bytes(10_000_000), "not an SPK kernel"),
        (404, b"missing", "HTTP 404"),
    ],
)
def test_a_bad_de421_download_is_refused_and_leaves_nothing(
    tmp_path: Path, status: int, body: bytes, match: str
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, content=body)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SkyfieldUnavailableError, match=match),
    ):
        ensure_de421(tmp_path, client=client, limiter=_fast_limiter())
    assert len(calls) == 1  # never retried: only 429 and 5xx are
    assert list((tmp_path / "skyfield" / "kernels").iterdir()) == []


def test_de421_gives_up_after_the_retries(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SkyfieldUnavailableError, match="failed 2 times"),
    ):
        ensure_de421(tmp_path, client=client, limiter=_fast_limiter(), max_attempts=2)
    assert list((tmp_path / "skyfield" / "kernels").iterdir()) == []
