"""SNOW_GLOBE_* variables with the deprecated LOOKOUT_* names as a fallback (D-080)."""

from __future__ import annotations

from pathlib import Path

import pytest

from snow_globe_common import http as lh
from snow_globe_common import manifest as mf
from snow_globe_common import paths
from snow_globe_common import storage as st
from snow_globe_common.env import LEGACY_PREFIX, PREFIX, env_get, env_source, legacy_name

from .conftest import REPO_ROOT


def test_legacy_name_maps_the_prefix_only():
    assert (PREFIX, LEGACY_PREFIX) == ("SNOW_GLOBE_", "LOOKOUT_")
    assert legacy_name("SNOW_GLOBE_DATA_DIR") == "LOOKOUT_DATA_DIR"
    assert legacy_name("SNOW_GLOBE_FORECAST_MAX_MB") == "LOOKOUT_FORECAST_MAX_MB"
    for bad in ("LOOKOUT_DATA_DIR", "DATA_DIR", "SNOW_GLOBE_", "snow_globe_data_dir"):
        with pytest.raises(ValueError):
            legacy_name(bad)


@pytest.mark.parametrize(
    ("environ", "value", "source"),
    [
        ({}, None, None),
        ({"SNOW_GLOBE_X": "new"}, "new", "SNOW_GLOBE_X"),
        ({"LOOKOUT_X": "old"}, "old", "LOOKOUT_X"),
        ({"SNOW_GLOBE_X": "new", "LOOKOUT_X": "old"}, "new", "SNOW_GLOBE_X"),
        # blank counts as unset, so an empty new name never hides the old one
        ({"SNOW_GLOBE_X": "", "LOOKOUT_X": "old"}, "old", "LOOKOUT_X"),
        ({"SNOW_GLOBE_X": "  ", "LOOKOUT_X": "old"}, "old", "LOOKOUT_X"),
        ({"SNOW_GLOBE_X": "", "LOOKOUT_X": " "}, None, None),
        # the value comes back as set; callers strip it
        ({"LOOKOUT_X": " old "}, " old ", "LOOKOUT_X"),
    ],
)
def test_env_get_prefers_the_new_name(environ, value, source):
    assert env_get("SNOW_GLOBE_X", environ=environ) == value
    assert env_source("SNOW_GLOBE_X", environ) == source
    assert env_get("SNOW_GLOBE_X", "dflt", environ=environ) == (value if value else "dflt")


def test_env_get_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOOKOUT_PROBE", "old")
    assert env_get("SNOW_GLOBE_PROBE") == "old"
    monkeypatch.setenv("SNOW_GLOBE_PROBE", "new")
    assert env_get("SNOW_GLOBE_PROBE") == "new"


# --- the data dir (D-080's order) -----------------------------------------------------------
@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home with no data-dir variable set."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.delenv("SNOW_GLOBE_DATA_DIR", raising=False)
    monkeypatch.delenv("LOOKOUT_DATA_DIR", raising=False)
    return fake


def test_default_is_snow_globe_data_when_neither_folder_exists(home: Path):
    assert paths.default_data_dir() == home / "snow-globe-data"
    assert paths.data_dir() == (home / "snow-globe-data").resolve()
    assert not (home / "snow-globe-data").exists()  # resolution creates nothing


def test_default_keeps_lookout_data_until_it_is_moved(home: Path):
    (home / "lookout-data").mkdir()
    assert paths.data_dir() == (home / "lookout-data").resolve()


def test_default_refuses_while_both_folders_exist_apart(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Something created ~/snow-globe-data before the switch moved the data (D-080): neither is
    picked, because the new one would bring a second NPS lock and empty state and logs."""
    (home / "lookout-data").mkdir()
    (home / "snow-globe-data").mkdir()
    for resolve in (paths.default_data_dir, paths.data_dir):
        with pytest.raises(paths.AmbiguousDataDirError, match="different folders") as caught:
            resolve()
        assert isinstance(caught.value, paths.UnsafeDataDirError)  # callers' handlers catch it
    assert sorted(p.name for p in home.iterdir()) == ["lookout-data", "snow-globe-data"]
    # A variable names the folder meant, so nothing is ambiguous.
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(home / "lookout-data"))
    assert paths.data_dir() == (home / "lookout-data").resolve()
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(home / "snow-globe-data"))
    assert paths.data_dir() == (home / "snow-globe-data").resolve()


def test_default_uses_the_new_name_when_it_is_a_symlink_to_the_old_folder(home: Path):
    (home / "lookout-data").mkdir()
    (home / "snow-globe-data").symlink_to(home / "lookout-data")
    assert paths.default_data_dir() == home / "snow-globe-data"
    assert paths.data_dir() == (home / "lookout-data").resolve()


def test_after_the_move_the_old_name_is_a_symlink_to_the_new_folder(home: Path):
    (home / "snow-globe-data").mkdir()
    (home / "lookout-data").symlink_to(home / "snow-globe-data")
    assert paths.data_dir() == (home / "snow-globe-data").resolve()
    # an old shell's LOOKOUT_DATA_DIR=~/lookout-data still lands in the same folder
    assert paths.check_data_dir(home / "lookout-data") == (home / "snow-globe-data").resolve()


def test_default_follows_a_symlinked_new_folder_but_not_a_broken_one(home: Path, tmp_path: Path):
    (home / "lookout-data").mkdir()
    (home / "snow-globe-data").symlink_to(tmp_path / "missing")
    assert paths.data_dir() == (home / "lookout-data").resolve()


def test_lookout_data_dir_still_names_the_data_dir(home: Path, monkeypatch: pytest.MonkeyPatch):
    (home / "snow-globe-data").mkdir()
    old = home / "elsewhere-old"
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(old))
    assert paths.data_dir() == old.resolve()  # a variable beats both default folders


def test_snow_globe_data_dir_beats_lookout_data_dir(home: Path, monkeypatch: pytest.MonkeyPatch):
    new, old = home / "new", home / "old"
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(old))
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(new))
    assert paths.data_dir() == new.resolve()
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", "   ")  # blank: unset
    assert paths.data_dir() == old.resolve()


def test_the_guard_applies_to_the_legacy_variable_and_default(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(REPO_ROOT / "data"))
    with pytest.raises(paths.UnsafeDataDirError, match="git work tree"):
        paths.data_dir()
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(home / "Documents" / "data"))
    (home / "Documents").mkdir()
    with pytest.raises(paths.UnsafeDataDirError, match="cloud-synced"):
        paths.data_dir()
    monkeypatch.setenv("LOOKOUT_ALLOW_SYNCED_DATA", "1")  # the old override name still works
    assert paths.data_dir() == (home / "Documents" / "data").resolve()


# --- the other readers ----------------------------------------------------------------------
def test_user_agent_falls_back_to_the_legacy_variable(monkeypatch: pytest.MonkeyPatch):
    assert lh.user_agent() == "SnowGlobe/0.1 (webcam research capture; fetch-on-change)"
    monkeypatch.setenv("LOOKOUT_USER_AGENT", "Old/1.0 (ci)")
    assert lh.user_agent() == "Old/1.0 (ci)"
    monkeypatch.setenv("SNOW_GLOBE_USER_AGENT", "New/1.0 (ci)")
    assert lh.user_agent() == "New/1.0 (ci)"


def test_storage_reads_the_legacy_variables(monkeypatch: pytest.MonkeyPatch, data_root: Path):
    monkeypatch.setenv("LOOKOUT_STORAGE", "gcs")
    with pytest.raises(ValueError, match="must be 'local' or 's3'"):
        st.storage_from_env()
    monkeypatch.setenv("LOOKOUT_STORAGE", "s3")
    with pytest.raises(ValueError, match="needs SNOW_GLOBE_S3_BUCKET"):
        st.storage_from_env()
    monkeypatch.setenv("SNOW_GLOBE_STORAGE", "local")  # the new name wins
    assert isinstance(st.storage_from_env(), st.LocalStorage)


def test_manifest_dir_reads_the_legacy_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    manifests = REPO_ROOT / "viewpoints" / "manifests"
    monkeypatch.setenv("LOOKOUT_MANIFEST_DIR", str(manifests))
    assert mf.manifest_dir() == manifests.resolve()
    monkeypatch.setenv("LOOKOUT_MANIFEST_DIR", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError, match="LOOKOUT_MANIFEST_DIR="):
        mf.manifest_dir()  # the message names the variable that was read
    monkeypatch.setenv("SNOW_GLOBE_MANIFEST_DIR", str(manifests))
    assert mf.manifest_dir() == manifests.resolve()
