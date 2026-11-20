from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from lookout_common import paths
from lookout_common.paths import UnsafeDataDirError

from .conftest import REPO_ROOT

PST = timezone(timedelta(hours=-8))
SHA = "9e88bc13788ff06480a4d50c305477b4621fc31eb1480f34b405f2886021dbca"


def test_data_dir_defaults_to_home_lookout_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LOOKOUT_DATA_DIR")
    assert paths.data_dir() == (tmp_path / "lookout-data").resolve()


def test_data_dir_from_env(data_root: Path):
    assert paths.data_dir() == data_root.resolve()


def test_data_dir_inside_this_repo_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(REPO_ROOT / "data"))
    with pytest.raises(UnsafeDataDirError, match="git work tree"):
        paths.data_dir()


def test_data_dir_inside_any_git_tree_is_refused_even_with_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = tmp_path / "other-repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(repo / "nested" / "data"))
    monkeypatch.setenv("LOOKOUT_ALLOW_SYNCED_DATA", "1")
    with pytest.raises(UnsafeDataDirError):
        paths.data_dir()


@pytest.mark.parametrize("synced", ["Documents", "Desktop", "Library/Mobile Documents"])
def test_synced_folders_are_refused_unless_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synced: str
):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / synced / "lookout-data"
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(target))
    with pytest.raises(UnsafeDataDirError, match="cloud-synced"):
        paths.data_dir()
    monkeypatch.setenv("LOOKOUT_ALLOW_SYNCED_DATA", "1")
    assert paths.data_dir() == target.resolve()


def test_symlink_into_synced_folder_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    real = tmp_path / "Documents" / "data"
    real.mkdir(parents=True)
    link = tmp_path / "innocent-link"
    link.symlink_to(real)
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(link))
    with pytest.raises(UnsafeDataDirError):
        paths.data_dir()


def test_case_variant_of_synced_folder_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Review finding: on a case-insensitive disk ~/documents *is* ~/Documents (iCloud)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    variant = tmp_path / "documents"
    if not variant.exists():
        pytest.skip("case-sensitive filesystem: ~/documents is a different folder here")
    for spelling in (variant / "lookout-data", tmp_path / "DOCUMENTS" / "deep" / "data"):
        monkeypatch.setenv("LOOKOUT_DATA_DIR", str(spelling))
        with pytest.raises(UnsafeDataDirError, match="cloud-synced"):
            paths.data_dir()
        assert not spelling.exists()  # the guard creates nothing
    monkeypatch.setenv("LOOKOUT_DATA_DIR", str(tmp_path / "documents-elsewhere"))
    assert paths.data_dir() == (tmp_path / "documents-elsewhere").resolve()


def test_firmlinked_data_volume_path_to_documents_is_refused():
    """Review finding: /System/Volumes/Data/Users/<me>/Documents is the same iCloud folder."""
    documents = Path.home() / "Documents"
    firmlinked = Path("/System/Volumes/Data") / documents.relative_to("/")
    if not (documents.is_dir() and firmlinked.is_dir()):
        pytest.skip("no macOS firmlinked Data volume with a ~/Documents here")
    if not os.path.samefile(documents, firmlinked):
        pytest.skip("the Data-volume path is not the same folder here")
    probe = firmlinked / "lookout-data-firmlink-probe"
    assert not probe.exists()
    with pytest.raises(UnsafeDataDirError, match="cloud-synced"):
        paths.check_data_dir(probe)
    assert not probe.exists()


def test_case_variant_of_the_repo_is_still_refused_by_the_git_check():
    variant = Path(str(REPO_ROOT).swapcase())
    if not variant.exists():
        pytest.skip("case-sensitive filesystem")
    with pytest.raises(UnsafeDataDirError, match="git work tree"):
        paths.check_data_dir(variant / "data")


def test_tilde_in_env_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOOKOUT_DATA_DIR", "~/elsewhere")
    assert paths.data_dir() == (tmp_path / "elsewhere").resolve()


def test_state_and_cache_dirs_are_created(data_root: Path):
    assert paths.state_dir() == data_root.resolve() / "state"
    assert paths.cache_dir() == data_root.resolve() / "cache"
    assert (data_root / "state").is_dir() and (data_root / "cache").is_dir()


def test_frame_key_matches_the_stopgap_layout():
    t = datetime(2026, 11, 19, 6, 40, 14, 57851, tzinfo=UTC)
    key = paths.frame_key("rainier.paradise-mountain", t, SHA)
    assert key == "frames/rainier.paradise-mountain/2026-11-19/20261119T064014Z_9e88bc13788f.jpg"
    assert paths.sidecar_key(key).endswith("20261119T064014Z_9e88bc13788f.json")


def test_frame_key_converts_to_utc_and_uses_the_utc_date():
    local = datetime(2026, 11, 18, 22, 40, 14, tzinfo=PST)  # 06:40:14Z next day
    key = paths.frame_key("point-reyes.beach", local, SHA.upper())
    assert key == "frames/point-reyes.beach/2026-11-19/20261119T064014Z_9e88bc13788f.jpg"


@pytest.mark.parametrize(
    ("vp", "when", "digest", "error"),
    [
        ("rainier.paradise-mountain", datetime(2026, 11, 19, 6, 40), SHA, ValueError),
        ("rainier.paradise-mountain", datetime(2026, 11, 19, tzinfo=UTC), "abc", ValueError),
        ("rainier.paradise-mountain", datetime(2026, 11, 19, tzinfo=UTC), "z" * 64, ValueError),
        ("../etc", datetime(2026, 11, 19, tzinfo=UTC), SHA, ValueError),
        ("Rainier.Paradise", datetime(2026, 11, 19, tzinfo=UTC), SHA, ValueError),
        ("rainier", datetime(2026, 11, 19, tzinfo=UTC), SHA, ValueError),
        ("rainier.paradise-mountain", "2026-11-19", SHA, TypeError),
    ],
)
def test_frame_key_rejects_bad_input(vp, when, digest, error):
    with pytest.raises(error):
        paths.frame_key(vp, when, digest)


def test_thumb_key_shares_the_frame_stem_under_thumbs():
    local = datetime(2026, 11, 18, 22, 40, 14, 57851, tzinfo=PST)  # 06:40:14Z next day
    key = paths.thumb_key("point-reyes.beach", local, SHA.upper())
    assert key == "thumbs/point-reyes.beach/2026-11-19/20261119T064014Z_9e88bc13788f.jpg"
    frame = paths.frame_key("point-reyes.beach", local, SHA)
    assert key.removeprefix("thumbs/") == frame.removeprefix("frames/")
    assert paths.sidecar_key(key) == key[: -len(".jpg")] + ".json"
    with pytest.raises(ValueError):
        paths.thumb_key("point-reyes.beach", datetime(2026, 11, 19, 6, 40), SHA)  # naive
    with pytest.raises(ValueError):
        paths.thumb_key("point-reyes.beach", local, "abc")
    with pytest.raises(ValueError):
        paths.thumb_key("../etc", local, SHA)


def test_archive_key_uses_first_eight_asset_chars():
    t = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    key = paths.archive_key("point-reyes.beach", t, "50C46AC9-87eb-4f24-bcb6-5928653027d3")
    assert key == "archive/point-reyes.beach/2026-11-17/20261117T080000Z_50c46ac9.jpg"
    with pytest.raises(ValueError):
        paths.archive_key("point-reyes.beach", t, "not-hex!")


def test_log_keys_use_utc_dates():
    assert paths.capture_log_key(date(2026, 11, 19)) == "capture_log/2026-11-19.jsonl"
    late_pst = datetime(2026, 11, 18, 23, 30, tzinfo=PST)
    assert paths.capture_log_key(late_pst) == "capture_log/2026-11-19.jsonl"
    assert paths.forecast_log_key(date(2026, 11, 30)) == "forecast_log/2026-11-30.jsonl"
    with pytest.raises(ValueError):
        paths.capture_log_key(datetime(2026, 11, 19, 1, 0))


def test_forecast_key():
    run = datetime(2026, 11, 19, 6, tzinfo=UTC)
    key = paths.forecast_key("hrrr", "rainier.paradise-mountain", run)
    assert key == "forecast/hrrr/rainier.paradise-mountain/2026111906z.json"
    with pytest.raises(ValueError, match="on the hour"):
        paths.forecast_key("hrrr", "rainier.paradise-mountain", run.replace(minute=30))
    with pytest.raises(ValueError):
        paths.forecast_key("HRRR/../x", "rainier.paradise-mountain", run)


@pytest.mark.parametrize("name", ["capture/pore.json", "ratelimit", "a.b-c_d/e.json"])
def test_state_key_accepts_safe_names(name: str):
    assert paths.state_key(name) == f"state/{name}"


@pytest.mark.parametrize("name", ["", "../x", "/abs", "a//b", ".hidden", "a/./b", "a\\b", "a b"])
def test_state_key_rejects_unsafe_names(name: str):
    with pytest.raises(ValueError):
        paths.state_key(name)


@pytest.mark.parametrize(
    "key",
    ["", "/a", "a/", "a//b", "a/../b", "./a", "a\\b", "a\x00b", ".hidden", "state/.marker"],
)
def test_validate_key_rejects(key: str):
    with pytest.raises(ValueError):
        paths.validate_key(key)


@pytest.mark.parametrize("key", ["frames/a.b/x.jpg", "state/locks/nps-archive.lock", "a/b.c.d"])
def test_validate_key_accepts_dots_inside_segments(key: str):
    assert paths.validate_key(key) == key


def test_local_path_maps_keys_under_root(tmp_path: Path):
    assert paths.local_path("frames/x/y.jpg", tmp_path) == tmp_path / "frames" / "x" / "y.jpg"


def test_compact_utc():
    assert paths.compact_utc(datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=UTC)) == (
        "20260102T030405Z"
    )
