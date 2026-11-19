"""Shared fixtures: every test runs with a clean LOOKOUT_* environment and a temp data dir."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLES = Path.home() / "lookout-data" / "samples"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any LOOKOUT_* or AWS_* variables from the developer's shell."""
    for name in list(os.environ):
        if name.startswith(("LOOKOUT_", "AWS_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh data dir, exported as LOOKOUT_DATA_DIR."""
    root = tmp_path / "lookout-data"
    root.mkdir()
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(root))
    return root


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials so boto3 never reaches a real account (moto intercepts calls)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
