"""Shared fixtures: clean LOOKOUT_* environment, a temp data dir, real manifests, a fake bucket."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lookout_common.manifest import load_manifests
from lookout_common.storage import LocalStorage

from lookout_forecast.bucket import HrrrBucket
from lookout_forecast.grid import HRRR_CONUS

from .fakebucket import FakeBucket, sub_grid

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFESTS = REPO_ROOT / "viewpoints" / "manifests"

#: The run used by most offline tests: a synoptic (48 h) cycle.
RUN = datetime(2026, 11, 19, 6, tzinfo=UTC)
RAINIER = "rainier.paradise-mountain"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop LOOKOUT_*, AWS_* and HERBIE_* variables from the developer's shell."""
    for name in list(os.environ):
        if name.startswith(("LOOKOUT_", "AWS_", "HERBIE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOOKOUT_MANIFEST_DIR", str(MANIFESTS))


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh data dir, exported as LOOKOUT_DATA_DIR."""
    root = tmp_path / "lookout-data"
    root.mkdir()
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(root))
    return root


@pytest.fixture
def storage(data_root: Path) -> LocalStorage:
    return LocalStorage(data_root)


@pytest.fixture(scope="session")
def manifests():
    return load_manifests(MANIFESTS)


@pytest.fixture(scope="session")
def grid_fixture() -> dict:
    return json.loads((FIXTURES / "hrrr_conus_grid.json").read_text())


@pytest.fixture
def small_grid():
    """A 40 x 40 HRRR sub-grid centred on the Rainier camera cell."""
    ci, cj = HRRR_CONUS.nearest_ij(46.78455, -121.74065)
    return sub_grid(ci - 20, cj - 20, 40, 40)


@pytest.fixture
def fake(small_grid) -> FakeBucket:
    return FakeBucket(grid=small_grid)


@pytest.fixture
def bucket(fake: FakeBucket):
    sleeps: list[float] = []
    with HrrrBucket(transport=fake.transport(), sleep=sleeps.append) as client:
        client.sleeps = sleeps  # type: ignore[attr-defined]
        yield client


def idx_text(name: str) -> str:
    return (FIXTURES / name).read_text()
