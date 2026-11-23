"""Skyfield reference positions and sunrise/sunset (PRD §8.4 "Checks"), for an isolated env.

**Run only in an isolated environment** (Skyfield is not a dependency of eval)::

    uv run --no-project --python 3.13 --with skyfield==1.55 --with jplephem==2.24 \\
        --with sgp4==2.27 --with numpy==2.5.4 --with certifi==2026.10.23 \\
        python -I skyfield_ref.py INPUT.json OUTPUT.json

:mod:`.skyfield_run` writes INPUT and runs that command. This file imports nothing from Snow
Globe and never touches the network: the JPL kernel (DE421) is opened from the path INPUT
names (``load_file``), and the time scale is Skyfield's built-in one (``builtin=True``: its
shipped IERS ΔT and UT1−UTC table and leap seconds; no download).

INPUT (JSON): ``kernel`` (path), ``refraction`` (``temperature_C``, ``pressure_mbar``),
``samples`` (``id``, ``unix_ms``, ``lat_deg``, ``lon_deg``, ``height_m``), ``sites`` (``id``,
``lat_deg``, ``lon_deg``, ``height_m``, ``unix_ms``: the epochs to evaluate there) and
``events`` (``horizon_deg``, ``sites``: ``id``, ``lat_deg``, ``lon_deg``, ``height_m``,
``start_unix_ms``, ``stop_unix_ms``).

OUTPUT (JSON): the versions, the kernel's size and SHA-256, and per sample and per site epoch
the Sun's and Moon's airless apparent altitude and azimuth, the refracted altitude and azimuth
(Skyfield's ``altaz(temperature_C, pressure_mbar)``), the Moon's illuminated fraction, UT1−UTC
and ΔT; per events site every sunrise and sunset (``find_risings``/``find_settings``, the Sun's
centre at ``horizon_deg``, geometric).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from datetime import UTC, datetime
from importlib import metadata

SCHEMA = "snow-globe.ephem-skyfield-output/1"


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _times(ts, unix_ms):  # type: ignore[no-untyped-def]
    """A Skyfield Time array from Unix milliseconds, through calendar fields (UTC)."""
    import numpy as np

    stamps = [datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC) for ms in unix_ms]
    year = np.array([s.year for s in stamps])
    month = np.array([s.month for s in stamps])
    day = np.array([s.day for s in stamps])
    hour = np.array([s.hour for s in stamps])
    minute = np.array([s.minute for s in stamps])
    second = np.array([s.second + s.microsecond / 1e6 for s in stamps], dtype=float)
    return ts.utc(year, month, day, hour, minute, second)


def _positions(site, t, sun, moon, refraction):  # type: ignore[no-untyped-def]
    """Airless and refracted alt/az of the Sun and Moon, the Moon's phase, UT1−UTC and ΔT."""
    out = {}
    for name, body in (("sun", sun), ("moon", moon)):
        apparent = site.at(t).observe(body).apparent()
        alt, az, _ = apparent.altaz()
        alt_r, az_r, _ = apparent.altaz(
            temperature_C=refraction["temperature_C"], pressure_mbar=refraction["pressure_mbar"]
        )
        out[name] = {
            "alt_deg": [round(float(v), 10) for v in alt.degrees],
            "az_deg": [round(float(v), 10) for v in az.degrees],
            "alt_refr_deg": [round(float(v), 10) for v in alt_r.degrees],
            "az_refr_deg": [round(float(v), 10) for v in az_r.degrees],
        }
        if name == "moon":
            frac = apparent.fraction_illuminated(sun)
            out[name]["illuminated_fraction"] = [round(float(v), 10) for v in frac]
    out["dut1_s"] = [round(float(v), 6) for v in t.dut1]
    out["delta_t_s"] = [round(float(v), 6) for v in t.delta_t]
    return out


def main(argv: list[str]) -> int:
    """Read INPUT, compute with Skyfield, write OUTPUT (see the module docstring)."""
    if len(argv) != 3:
        print("usage: python -I skyfield_ref.py INPUT.json OUTPUT.json", file=sys.stderr)
        return 2
    from skyfield import almanac
    from skyfield.api import load, load_file, wgs84

    with open(argv[1], encoding="utf-8") as handle:
        job = json.load(handle)
    ts = load.timescale(builtin=True)
    eph = load_file(job["kernel"])
    earth, sun, moon = eph["earth"], eph["sun"], eph["moon"]
    refraction = job["refraction"]

    samples = []
    for item in job.get("samples", []):
        site = earth + wgs84.latlon(item["lat_deg"], item["lon_deg"], elevation_m=item["height_m"])
        t = _times(ts, [item["unix_ms"]])
        pos = _positions(site, t, sun, moon, refraction)
        row = {"id": item["id"], "dut1_s": pos["dut1_s"][0], "delta_t_s": pos["delta_t_s"][0]}
        for body in ("sun", "moon"):
            row[body] = {k: v[0] for k, v in pos[body].items()}
        samples.append(row)

    sites = []
    for item in job.get("sites", []):
        site = earth + wgs84.latlon(item["lat_deg"], item["lon_deg"], elevation_m=item["height_m"])
        t = _times(ts, item["unix_ms"])
        sites.append({"id": item["id"], **_positions(site, t, sun, moon, refraction)})

    events = []
    spec = job.get("events") or {}
    for item in spec.get("sites", []):
        site = earth + wgs84.latlon(item["lat_deg"], item["lon_deg"], elevation_m=item["height_m"])
        t0, t1 = _times(ts, [item["start_unix_ms"], item["stop_unix_ms"]])
        for kind, finder in (("rise", almanac.find_risings), ("set", almanac.find_settings)):
            times, crosses = finder(site, sun, t0, t1, horizon_degrees=spec["horizon_deg"])
            for when, ok in zip(times, crosses, strict=True):
                stamp = when.utc_datetime()
                events.append(
                    {
                        "site": item["id"],
                        "kind": kind,
                        "unix_ms": round(stamp.timestamp() * 1000.0, 3),
                        "crosses": bool(ok),
                    }
                )

    versions = {}
    for dist in ("skyfield", "jplephem", "sgp4", "numpy", "certifi"):
        try:
            versions[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            versions[dist] = None
    result = {
        "schema": SCHEMA,
        "input_sha256": _sha256(argv[1]),
        "versions": versions,
        "python": platform.python_version(),
        "isolated": bool(sys.flags.isolated),
        "kernel": {
            "path": job["kernel"],
            "size_bytes": os.path.getsize(job["kernel"]),
            "sha256": _sha256(job["kernel"]),
            "segments": [str(s) for s in eph.segments][:20],
        },
        "timescale": {"builtin": True},
        "refraction": refraction,
        "samples": samples,
        "sites": sites,
        "events": events,
    }
    with open(argv[2], "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
