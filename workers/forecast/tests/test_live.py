"""Live smoke against NOAA's bucket (network; excluded by default; run with ``-m network -s``).

The task's smoke: the most recent complete HRRR run, f00-f02, fields VIS, HGT:cloud
ceiling, HGT:cloud base, LCDC, MCDC, HCDC and HGT:surface, all 8 viewpoints, files written.
It downloads about 30 MB and prints what it measured as JSON. Files go to a temporary
data dir unless ``FORECAST_SMOKE_DATA_DIR`` names one (e.g. ``~/snow-globe-data`` to keep them
as M0.5 evidence).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from snow_globe_forecast import cli
from snow_globe_forecast.output import validate_document

SMOKE_FIELDS = ["VIS", "HGT:cloud ceiling", "HGT:cloud base", "LCDC", "MCDC", "HCDC", "HGT:surface"]


def _summary(event: dict, docs: dict[str, dict], seconds: float) -> dict:
    rainier = docs["rainier.paradise-mountain"]
    names = rainier["field_names"]
    base = names.index("HGT:cloud base")
    ceiling = names.index("HGT:cloud ceiling")
    sample = []
    for k, point in enumerate(rainier["points"]):
        if point["kind"] in {"camera", "ray", "landmark"}:
            sample.append(
                {
                    "id": point["id"],
                    "i": point["i"],
                    "j": point["j"],
                    "model_terrain_m": point["model_terrain_m"],
                    "cloud_base_m_msl_f00_f01_f02": [
                        rainier["values"][base][h][k] for h in range(3)
                    ],
                    "ceiling_m_msl_f00": rainier["values"][ceiling][0][k],
                }
            )
    cameras = {}
    for vp_id, doc in sorted(docs.items()):
        names = doc["field_names"]
        cameras[vp_id] = {
            "cell": [doc["points"][0]["i"], doc["points"][0]["j"]],
            "model_terrain_m": doc["points"][0]["model_terrain_m"],
            **{name: doc["values"][f][0][0] for f, name in enumerate(names)},
        }
    hours = rainier["hours"]
    return {
        "run": rainier["run_id"],
        "live": rainier["live"],
        "wall_seconds": round(seconds, 2),
        "transfer": event.get("transfer"),
        "seconds_total": event.get("seconds_total"),
        "seconds_download_summed": event.get("seconds_download"),
        "seconds_decode": event.get("seconds_decode"),
        "planned_bytes": event.get("planned_bytes"),
        "bytes_by_field": event.get("bytes_by_field"),
        "lag_min_by_fh": {h["fh"]: round(h["lag_s"] / 60, 2) for h in hours},
        "availability_by_fh": {h["fh"]: h["availability_utc"] for h in hours},
        "s3_lag_min_all_hours": {
            k: round(v / 60, 2) for k, v in (event.get("s3_lags_s") or {}).items()
        },
        "requests_by_fh": {h["fh"]: h["requests"] for h in hours},
        "fields": {
            name: {
                k: rainier["fields"][name][k]
                for k in ("shortName", "typeOfLevel", "units", "datum")
            }
            for name in rainier["field_names"]
        },
        "rainier_camera_ray_landmark": sample,
        "camera_cells_f00": cameras,
        "file_bytes": {vp: len(json.dumps(d, separators=(",", ":"))) for vp, d in docs.items()},
    }


@pytest.mark.network
def test_live_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    target = Path(os.environ.get("FORECAST_SMOKE_DATA_DIR") or tmp_path / "snow-globe-data")
    target = target.expanduser()
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(target))
    started = time.monotonic()
    rc = cli.main(
        ["latest", "--max-fh", "2", "--fields", *SMOKE_FIELDS, "--max-mb", "150", "--json"]
    )
    seconds = time.monotonic() - started
    out = capsys.readouterr().out
    assert rc == 0, out
    event = json.loads(out) if out.lstrip().startswith("{") else {}
    run = event.get("run") or out.split()[1]
    docs = {}
    for vp_dir in sorted((target / "forecast" / "hrrr").iterdir()):
        path = vp_dir / f"{run}z.json"
        assert path.exists(), path
        doc = json.loads(path.read_text())
        validate_document(doc)
        assert doc["forecast_hours"] == [0, 1, 2]
        # The static terrain is fetched with f00 only and repeated for f01 and f02.
        terrain = doc["field_names"].index("HGT:surface")
        assert doc["values"][terrain][1] == doc["values"][terrain][0] == doc["values"][terrain][2]
        assert [r["field"] == "HGT:surface" for h in doc["hours"] for r in h["byte_ranges"]].count(
            True
        ) == 1
        docs[vp_dir.name] = doc
    assert len(docs) == 8
    assert not [p for p in (target / "cache" / "hrrr").rglob("*.grib2*") if p.is_file()]
    if event:
        assert event["transfer"]["total_bytes"] < 150e6
        # This run's own counts. Its listing was made by the newest-complete search and handed
        # to the extractor (0.2.0), so the run lists nothing of its own.
        assert "listing" not in event["transfer"]["requests"]
        assert event["transfer"]["requests"]["idx"] == 3
    with capsys.disabled():
        print(json.dumps(_summary(event, docs, seconds), indent=1))
