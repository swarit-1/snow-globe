"""Shared fixtures for snow-globe-eval tests.

Tests never touch the network and never write under ~/snow-globe-data: anything that writes
gets a temporary SNOW_GLOBE_DATA_DIR. Tests that need the local sample frames or the full NPS
sample response skip when those files are absent (as in CI).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from snow_globe_common.paths import AmbiguousDataDirError, default_data_dir

from snow_globe_eval.ephemeris import NoaaBackend
from snow_globe_eval.scene import NODE_MIN_MAJOR, node_executable

FIXTURES = Path(__file__).parent / "fixtures"
#: Sample files on the dev machine: ``~/snow-globe-data/samples``, or ``~/lookout-data/samples``
#: until the data folder is moved (D-080).
try:
    SAMPLES = default_data_dir() / "samples"
except AmbiguousDataDirError:  # both default folders exist apart (D-080): the sample tests skip
    SAMPLES = Path("/nonexistent/snow-globe-samples")
REPO_RENDERER = Path(__file__).resolve().parents[2] / "renderer"

HAS_OCULUS = importlib.util.find_spec("oculus_core") is not None
needs_oculus = pytest.mark.skipif(not HAS_OCULUS, reason="oculus-core not built in this venv")


def _node_reason() -> str | None:
    """Why the renderer's validateScene cannot run here, or None when it can."""
    node = node_executable()
    if node is None:
        return "node not found"
    try:
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"node --version failed: {exc}"
    if int(version.lstrip("v").split(".")[0]) < NODE_MIN_MAJOR:
        return f"node {version} found; the renderer validator needs {NODE_MIN_MAJOR}+"
    if not (REPO_RENDERER / "src" / "scene.ts").is_file():
        return "renderer/src/scene.ts not present"
    return None


NODE_REASON = _node_reason()
needs_node = pytest.mark.skipif(NODE_REASON is not None, reason=str(NODE_REASON))


class FixedEphemeris:
    """A test double: the Sun sits at a fixed altitude and azimuth."""

    name = "fixed-test-double"
    version = "0"

    def __init__(self, altitude_deg: float, azimuth_deg: float = 180.0) -> None:
        self.altitude_deg = altitude_deg
        self.azimuth_deg = azimuth_deg

    def sun_alt_az(self, moment: datetime, lat_deg: float, lon_deg: float, height_m: float = 0.0):
        return self.altitude_deg, self.azimuth_deg

    def moon_alt_az_illum(
        self, moment: datetime, lat_deg: float, lon_deg: float, height_m: float = 0.0
    ):
        return -30.0, 90.0, 0.5


class NoaaSunFixedMoon:
    """Real Sun positions (the independent NOAA algorithm) and a fixed Moon: no oculus-core.

    The renderer's validateScene() checks the Sun against the valid time and place, so a
    scene meant for it needs a real Sun; it does not check the Moon yet.
    """

    name = "noaa-sun-fixed-moon-test-double"
    version = "0"

    def sun_alt_az(self, moment: datetime, lat_deg: float, lon_deg: float, height_m: float = 0.0):
        return NoaaBackend().sun_alt_az(moment, lat_deg, lon_deg, height_m)

    def moon_alt_az_illum(
        self, moment: datetime, lat_deg: float, lon_deg: float, height_m: float = 0.0
    ):
        return -30.0, 90.0, 0.5


@pytest.fixture
def nps_subset() -> dict:
    return json.loads((FIXTURES / "nps_search_pore_20261117_subset.json").read_text())


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "snow-globe-data"
    root.mkdir()
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    yield root


def sample(name: str) -> Path:
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"local sample {path} not present")
    return path
