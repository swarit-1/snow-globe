"""Reading forecast-run files (PRD §8.3): interpolation, availability, datums, winds, the ray."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from snow_globe_common.manifest import load_manifest
from synth_forecast import clear_sky, low_deck, make_doc

from snow_globe_eval.forecast_io import (
    CEILING,
    LOW_CLOUD,
    ForecastFileError,
    ForecastRun,
    summit_ray,
)

RUN = datetime(2026, 11, 18, 12, tzinfo=UTC)
SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "workers/forecast/src/snow_globe_forecast/schema/forecast_run.schema.json"
)


@pytest.fixture(scope="module")
def rainier():
    return load_manifest("rainier.paradise-mountain")


def test_synthetic_documents_match_the_forecast_workers_schema(rainier) -> None:
    if not SCHEMA.is_file():
        pytest.skip("workers/forecast schema not present")
    from jsonschema import Draft202012Validator

    doc = make_doc(rainier, RUN, [23, 24], clear_sky)
    errors = list(Draft202012Validator(json.loads(SCHEMA.read_text())).iter_errors(doc))
    assert errors == []


def _ramp(field, fh, point):
    if field == LOW_CLOUD:
        return 10.0 * (fh - 20)  # 30 % at f23, 40 % at f24
    if field == CEILING:
        return None if fh == 24 else 2000.0
    return clear_sky(field, fh, point)


def test_bracket_interpolation_and_availability(rainier, tmp_path) -> None:
    path = tmp_path / "run.json"
    path.write_text(json.dumps(make_doc(rainier, RUN, [23, 24], _ramp)))
    run = ForecastRun.load(path)
    assert run.sha256 and run.run_id == "2026111812" and run.viewpoint_id == rainier.id
    valid = RUN + timedelta(hours=23, minutes=15)
    assert run.bracket(valid) == (23, 24, pytest.approx(0.25))
    assert run.reading(LOW_CLOUD, "camera", valid).value == pytest.approx(32.5)
    on_hour = RUN + timedelta(hours=24)
    assert run.bracket(on_hour) == (24, 24, 0.0)
    available, source = run.availability(valid)
    assert source == "modeled"
    assert available == run.availability(on_hour)[0]  # the later of f23 and f24
    with pytest.raises(ForecastFileError, match="no forecast hour"):
        run.bracket(RUN + timedelta(hours=30))
    with pytest.raises(ForecastFileError, match="before the run start"):
        run.bracket(RUN - timedelta(hours=1))


def test_null_at_one_hour_takes_the_nearer_hour(rainier) -> None:
    run = ForecastRun(make_doc(rainier, RUN, [23, 24], _ramp))
    near_23 = run.height_msl(CEILING, "camera", RUN + timedelta(hours=23, minutes=20))
    assert near_23.value == 2000.0 and near_23.flags
    near_24 = run.height_msl(CEILING, "camera", RUN + timedelta(hours=23, minutes=40))
    assert near_24.value is None and near_24.missing == "null" and near_24.flags


def test_missing_reasons_are_kept_apart(rainier) -> None:
    doc = make_doc(rainier, RUN, [24], clear_sky, missing_from_idx={24: [CEILING]})
    doc["points"][1].update({"i": None, "j": None, "cell_lat": None, "cell_lon": None})
    run = ForecastRun(doc)
    valid = RUN + timedelta(hours=24)
    assert run.reading(LOW_CLOUD, "ray:1.5km", valid).missing == "outside-grid"
    assert run.reading(CEILING, "camera", valid).missing == "not-in-inventory"
    assert run.reading(LOW_CLOUD, "camera", valid).missing is None


def test_height_datums(rainier) -> None:
    run = ForecastRun(make_doc(rainier, RUN, [24], clear_sky))
    valid = RUN + timedelta(hours=24)
    pbl = run.height_msl("HPBL:surface", "camera", valid)  # above ground + 1500 m terrain
    assert pbl.value == pytest.approx(1900.0)
    with pytest.raises(ForecastFileError, match="not a height"):
        run.height_msl("VIS:surface", "camera", valid)


def test_wind_rotation(rainier) -> None:
    doc = make_doc(rainier, RUN, [24], clear_sky)  # grid u = 3, v = 0
    run = ForecastRun(doc)
    valid = RUN + timedelta(hours=24)
    east, north = run.earth_relative_wind("camera", valid)
    angle = math.sin(math.radians(38.5)) * math.radians(rainier.camera.lon + 360 - 262.5)
    assert east.value == pytest.approx(3.0 * math.cos(angle))
    assert north.value == pytest.approx(-3.0 * math.sin(angle))
    doc["points"][0]["lon"] = -97.5  # on LoV the grid axes are east and north
    east, north = ForecastRun(doc).earth_relative_wind("camera", valid)
    assert (east.value, north.value) == (pytest.approx(3.0), pytest.approx(0.0))


def test_summit_ray_samples(rainier) -> None:
    run = ForecastRun(make_doc(rainier, RUN, [24], low_deck))
    samples, detail = summit_ray(run, rainier, rainier.landmarks[0], RUN + timedelta(hours=24))
    distances = [s.distance_km for s in samples]
    assert distances == [0.0, 1.5, 3.0, 4.5, 6.0, 7.44]
    heights = [s.ray_height_m_msl for s in samples]
    assert heights == sorted(heights)
    assert heights[0] == rainier.camera.elevation_m and heights[-1] == pytest.approx(4392.0)
    assert all(s.ceiling_m_msl == 2400.0 for s in samples)
    assert detail["used_view_ray_points"] is True


def test_rejects_other_documents() -> None:
    with pytest.raises(ForecastFileError):
        ForecastRun({"kind": "something-else", "schema_version": 1})
