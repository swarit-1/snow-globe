"""Shared fixtures: every test runs with a clean SNOW_GLOBE_* environment and a temp data dir."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from snow_globe_common.paths import AmbiguousDataDirError, default_data_dir

REPO_ROOT = Path(__file__).resolve().parents[3]
#: Sample frames on the dev machine: ``~/snow-globe-data/samples``, or ``~/lookout-data/samples``
#: until the data folder is moved (D-080).
try:
    SAMPLES = default_data_dir() / "samples"
except AmbiguousDataDirError:  # both default folders exist apart (D-080): the sample tests skip
    SAMPLES = Path("/nonexistent/snow-globe-samples")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Drop any SNOW_GLOBE_*, LOOKOUT_* or AWS_* variables from the developer's shell.

    The deprecated LOOKOUT_* names count because every reader falls back to them (D-080).

    SNOW_GLOBE_DATA_DIR then points at a folder inside this test's tmp_path, so a test that
    forgets ``data_root`` still never reaches the real ``~/snow-globe-data``. A test of the
    built-in default must ``delenv`` it (and set HOME to a temp folder).
    """
    for name in list(os.environ):
        if name.startswith(("SNOW_GLOBE_", "LOOKOUT_", "AWS_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(tmp_path / "default-data-dir"))


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh data dir, exported as SNOW_GLOBE_DATA_DIR."""
    root = tmp_path / "snow-globe-data"
    root.mkdir()
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(root))
    return root


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials so boto3 never reaches a real account (moto intercepts calls)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
