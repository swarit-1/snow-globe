"""Skyfield as an independent reference, run in an isolated environment (PRD §8.4 "Checks").

PRD §8.4 asks for "1,000 random place-and-time samples against Skyfield with a JPL
ephemeris". Skyfield is **not** a dependency of eval (its lock file and venv are shared), so:

1. :func:`ensure_de421` downloads JPL's DE421 kernel once into ``<cache>/skyfield/`` (polite:
   the shared ``jpl-ssd`` limiter, the project's generic User-Agent, back-off on errors) and
   records its size and SHA-256.
2. :func:`draw_samples` draws the samples from a fixed seed (:data:`SAMPLE_SEED`): places
   uniform on the sphere, heights 0–4,500 m, instants uniform from 1 Jan 1998 (the oldest NPS
   archive frames) to 1 Jan 2031.
3. :func:`skyfield_output` writes the job (the samples, the Horizons epochs of every site for a
   reference-against-reference check, and the sunrise/sunset search) to
   ``<cache>/skyfield/input-<key>.json`` and runs :mod:`.skyfield_ref` with
   ``uv run --no-project`` and the exact pins :data:`SKYFIELD_PINS`, ``python -I``, from an
   empty temporary folder. The output is cached as ``output-<key>.json``: the key is the job's
   SHA-256 (kernel by hash, not path), so a re-run reads it and needs neither uv nor network.

The proposal for the lead is an optional dependency group, ``ephem-ref`` = the pins below, so
that a later run need not build the environment itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from snow_globe_common.http import MinIntervalLimiter, backoff_delay, polite_client, retry_after_s
from snow_globe_common.timeutil import isoformat_z, utcnow

from .files import atomic_write_json, sha256_file
from .horizons import make_limiter

__all__ = [
    "DE421_URL",
    "REFRACTION",
    "SAMPLE_COUNT",
    "SAMPLE_SEED",
    "SKYFIELD_IERS_TABLE_END",
    "SKYFIELD_PINS",
    "SkyfieldUnavailableError",
    "build_job",
    "draw_samples",
    "ensure_de421",
    "job_key",
    "kernel_record",
    "skyfield_command",
    "skyfield_output",
]

#: The isolated environment: Skyfield and everything it imports, pinned exactly (resolved on
#: 23 Nov 2026 with ``--exclude-newer 2026-11-01``). Licences: MIT, MIT, MIT, BSD-3-Clause
#: (with bundled 0BSD/MIT/Zlib/CC0 parts), MPL-2.0.
SKYFIELD_PINS: tuple[str, ...] = (
    "skyfield==1.55",
    "jplephem==2.24",
    "sgp4==2.27",
    "numpy==2.5.4",
    "certifi==2026.10.23",
)
SKYFIELD_PYTHON = "3.13"
#: The last day of Skyfield 1.55's bundled UT1 − UTC table (``skyfield/data/iers.npz``: 19,745
#: daily rows from 1973-01-02, read in the isolated environment on 23 Nov 2026). Up to it
#: Skyfield uses the IERS values and predictions shipped with that version; after it Skyfield
#: extrapolates. A fact of the pinned version, used only in the report's wording.
SKYFIELD_IERS_TABLE_END = date(2027, 1, 23)
#: Skyfield's own URL for the kernel (``skyfield.iokit._JPL``); fetched by this module instead.
DE421_URL = "https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de421.bsp"
#: The fixed seed and count of the random samples.
SAMPLE_SEED = 20261123
SAMPLE_COUNT = 1000
SAMPLE_START = datetime(1998, 1, 1, tzinfo=UTC)
SAMPLE_STOP = datetime(2031, 1, 1, tzinfo=UTC)
SAMPLE_MAX_HEIGHT_M = 4500.0
#: The atmosphere eval's refraction assumes, given to Skyfield's refraction too.
REFRACTION: Mapping[str, float] = {"temperature_C": 10.0, "pressure_mbar": 1010.0}
JOB_SCHEMA = "snow-globe.ephem-skyfield-input/1"
OUTPUT_SCHEMA = "snow-globe.ephem-skyfield-output/1"
_SCRIPT = Path(__file__).with_name("skyfield_ref.py")
_DE421_MIN_BYTES = 10_000_000
_TIMEOUT = httpx.Timeout(300.0, connect=15.0, pool=15.0)


class SkyfieldUnavailableError(RuntimeError):
    """The Skyfield output is not cached and cannot be made (offline, no uv, a failed run)."""


def ensure_de421(
    cache_root: Path,
    *,
    offline: bool = False,
    client: httpx.Client | None = None,
    limiter: MinIntervalLimiter | None = None,
    max_attempts: int = 3,
    log: Callable[[str], None] | None = None,
) -> Path:
    """The DE421 kernel under ``<cache_root>/skyfield/``, downloaded once if missing.

    A download is checked (HTTP 200, at least 10 MB, the ``DAF/SPK`` signature) before it is
    renamed into place; its URL, size, SHA-256, time and User-Agent go to ``de421.bsp.json``.
    """
    folder = Path(cache_root) / "skyfield" / "kernels"
    path = folder / "de421.bsp"
    meta = folder / "de421.bsp.json"
    if path.is_file() and meta.is_file():
        return path
    if offline:
        raise SkyfieldUnavailableError(f"{path} is missing and fetching is off")
    folder.mkdir(parents=True, exist_ok=True)
    limiter = limiter or make_limiter(Path(cache_root))
    own = client is None
    client = client or polite_client(timeout=_TIMEOUT)
    try:
        failures = 0
        while True:
            fd, tmp = tempfile.mkstemp(prefix=".de421.", dir=folder)
            status: int | None = None
            agent: str | None = None
            retry: float | None = None
            started = time.monotonic()
            try:
                with os.fdopen(fd, "wb") as handle, limiter.hold():
                    started = time.monotonic()
                    try:
                        with client.stream("GET", DE421_URL) as response:
                            status = response.status_code
                            agent = response.request.headers.get("User-Agent")
                            retry = retry_after_s(response)
                            if status == 200:
                                for chunk in response.iter_bytes(1 << 20):
                                    handle.write(chunk)
                    except httpx.TransportError as exc:
                        status = None
                        if log:
                            log(f"DE421 download: {type(exc).__name__}: {exc}")
                    handle.flush()
                    os.fsync(handle.fileno())
                    if status is None or status in (429, 500, 502, 503, 504):
                        failures += 1
                        wait = backoff_delay(failures, base_s=30.0, cap_s=900.0)
                        if retry is not None and math.isfinite(retry):
                            wait = max(wait, retry)
                        limiter.defer(wait)
                if status == 200:
                    size = os.path.getsize(tmp)
                    with open(tmp, "rb") as check:
                        signature = check.read(8)
                    if size < _DE421_MIN_BYTES or not signature.startswith(b"DAF/SPK"):
                        raise SkyfieldUnavailableError(
                            f"DE421 download is not an SPK kernel ({size} bytes)"
                        )
                    os.chmod(tmp, 0o644)
                    os.replace(tmp, path)
                    atomic_write_json(
                        meta,
                        {
                            "url": DE421_URL,
                            "size_bytes": size,
                            "sha256": sha256_file(path),
                            "fetched_utc": isoformat_z(utcnow()),
                            "elapsed_s": round(time.monotonic() - started, 3),
                            "http_status": status,
                            "user_agent": agent,
                        },
                        indent=2,
                    )
                    if log:
                        log(f"downloaded {path} ({size} bytes)")
                    return path
                if status is not None and status not in (429, 500, 502, 503, 504):
                    raise SkyfieldUnavailableError(f"DE421 download: HTTP {status}")
            finally:
                Path(tmp).unlink(missing_ok=True)
            if failures >= max_attempts:
                raise SkyfieldUnavailableError(f"DE421 download failed {failures} times")
            if log:
                log(f"DE421 download: {status}; retrying after the back-off ({failures})")
    finally:
        if own:
            client.close()


def draw_samples(
    n: int = SAMPLE_COUNT,
    *,
    seed: int = SAMPLE_SEED,
    start: datetime = SAMPLE_START,
    stop: datetime = SAMPLE_STOP,
    max_height_m: float = SAMPLE_MAX_HEIGHT_M,
) -> list[dict[str, Any]]:
    """``n`` random places and instants from ``seed`` (NumPy PCG64), reproducible.

    Latitude from ``asin(U(-1, 1))`` (uniform on the sphere), longitude ``U(-180, 180)``,
    height ``U(0, max_height_m)``, instant uniform in ``[start, stop)`` to the millisecond.
    """
    rng = np.random.default_rng(seed)
    lat = np.degrees(np.arcsin(rng.uniform(-1.0, 1.0, n)))
    lon = rng.uniform(-180.0, 180.0, n)
    height = rng.uniform(0.0, max_height_m, n)
    t0, t1 = start.timestamp() * 1000.0, stop.timestamp() * 1000.0
    ms = np.floor(rng.uniform(t0, t1, n))
    return [
        {
            "id": i,
            "unix_ms": float(ms[i]),
            "lat_deg": round(float(lat[i]), 6),
            "lon_deg": round(float(lon[i]), 6),
            "height_m": round(float(height[i]), 1),
        }
        for i in range(n)
    ]


def job_key(job: Mapping[str, Any]) -> str:
    """SHA-256 of the job without the kernel's path (the kernel counts by its hash)."""
    keyed = {k: v for k, v in job.items() if k != "kernel"}
    canonical = json.dumps(keyed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_job(
    *,
    kernel: Path,
    kernel_sha256: str,
    samples: Sequence[Mapping[str, Any]],
    sites: Sequence[Mapping[str, Any]],
    events: Mapping[str, Any],
) -> dict[str, Any]:
    """The JSON job :mod:`.skyfield_ref` reads."""
    return {
        "schema": JOB_SCHEMA,
        "kernel": str(kernel),
        "kernel_sha256": kernel_sha256,
        "pins": list(SKYFIELD_PINS),
        "refraction": dict(REFRACTION),
        "samples": list(samples),
        "sites": list(sites),
        "events": dict(events),
    }


def skyfield_command(script: Path, job_path: Path, out_path: Path, uv: str) -> list[str]:
    """The isolated command: ``uv run --no-project`` with the pins, ``python -I``."""
    command = [uv, "run", "--no-project", "--python", SKYFIELD_PYTHON]
    for pin in SKYFIELD_PINS:
        command += ["--with", pin]
    return [*command, "python", "-I", str(script), str(job_path), str(out_path)]


def _isolated_env() -> dict[str, str]:
    """The environment without anything that would point uv or Python at eval's venv."""
    drop = {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH", "PYTHONHOME", "CONDA_PREFIX"}
    return {k: v for k, v in os.environ.items() if k not in drop}


def skyfield_output(
    cache_root: Path,
    job: Mapping[str, Any],
    *,
    offline: bool = False,
    uv: str | None = None,
    timeout_s: float = 1800.0,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Skyfield's answers to ``job``: cached, else computed in the isolated environment."""
    folder = Path(cache_root) / "skyfield"
    key = job_key(job)[:20]
    out_path = folder / f"output-{key}.json"
    if out_path.is_file():
        result = json.loads(out_path.read_text(encoding="utf-8"))
        if result.get("schema") != OUTPUT_SCHEMA:
            raise SkyfieldUnavailableError(f"{out_path} is not a Skyfield output")
        result["path"] = str(out_path)
        return result
    if offline:
        raise SkyfieldUnavailableError(f"no cached Skyfield output {out_path.name} and offline")
    uv = uv or shutil.which("uv")
    if not uv:
        raise SkyfieldUnavailableError("uv is not on the PATH; it builds Skyfield's env")
    job_path = folder / f"input-{key}.json"
    atomic_write_json(job_path, job)
    with tempfile.TemporaryDirectory(prefix="ephem-skyfield-") as scratch:
        tmp_out = Path(scratch) / "output.json"
        command = skyfield_command(_SCRIPT, job_path, tmp_out, uv)
        if log:
            log("running Skyfield: " + " ".join([*command[:4], "…", *command[-4:]]))
        try:
            done = subprocess.run(
                command,
                cwd=scratch,
                env=_isolated_env(),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SkyfieldUnavailableError(f"Skyfield run failed: {exc}") from exc
        if done.returncode != 0 or not tmp_out.is_file():
            raise SkyfieldUnavailableError(
                f"Skyfield run exited {done.returncode}: {done.stderr.strip()[-2000:]}"
            )
        result = json.loads(tmp_out.read_text(encoding="utf-8"))
    if result.get("schema") != OUTPUT_SCHEMA or result.get("input_sha256") != sha256_file(job_path):
        raise SkyfieldUnavailableError("the Skyfield output does not answer this job")
    if not result.get("isolated"):
        raise SkyfieldUnavailableError("Skyfield did not run under python -I")
    atomic_write_json(out_path, result)
    result["path"] = str(out_path)
    return result


def kernel_record(kernel: Path) -> dict[str, Any]:
    """The kernel's download record (``de421.bsp.json``)."""
    meta = kernel.with_name(kernel.name + ".json")
    if not meta.is_file():
        raise SkyfieldUnavailableError(f"{meta} is missing")
    return json.loads(meta.read_text(encoding="utf-8"))
