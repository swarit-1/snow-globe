"""One NPS fetcher per machine: the host-wide lock, stopgap detection, and the CLI exit code."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from snow_globe_common.storage import LocalStorage

import snow_globe_capture.fetcher_guard as guard_module
from snow_globe_capture.cli import EXIT_FETCHER_BUSY, main
from snow_globe_capture.fetcher_guard import (
    FOREIGN_LOG_WINDOW,
    FetcherBusy,
    ProcessInfo,
    foreign_processes,
    nps_fetcher_lock,
    recent_foreign_log_records,
)

NOW = datetime(2026, 11, 19, 11, 30, tzinfo=UTC)
STOPGAP = ProcessInfo(
    79758,
    "/Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/"
    "Python -I /private/tmp/claude-501/x/scratchpad/scripts/stopgap_rainier.py",
)


def _lock(data_root: Path, *, processes=None, clock=lambda: NOW):
    return nps_fetcher_lock(
        "test",
        state_dir=data_root / "state",
        log_dir=data_root / "capture_log",
        clock=clock,
        processes=processes or (lambda: []),
    )


def _log(data_root: Path, *records: dict) -> None:
    folder = data_root / "capture_log"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "2026-11-19.jsonl").open("a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_second_fetcher_in_the_same_process_fails_fast_and_names_the_holder(data_root):
    with _lock(data_root), pytest.raises(FetcherBusy, match=f"pid {os.getpid()}"), _lock(data_root):
        pass
    with _lock(data_root):  # released on exit, so it can be taken again
        pass


_HOLD = """
import sys, time
from pathlib import Path
from snow_globe_capture.fetcher_guard import nps_fetcher_lock
root = Path(sys.argv[1])
with nps_fetcher_lock("holder-process", state_dir=root / "state", log_dir=root / "capture_log",
                      processes=lambda: []):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def test_a_fetcher_in_another_process_blocks_this_one(tmp_path, data_root):
    from .conftest import subprocess_prelude

    script = tmp_path / "hold.py"
    script.write_text(subprocess_prelude() + _HOLD)
    proc = subprocess.Popen(
        [sys.executable, "-I", str(script), str(data_root), "3"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        busy = rf"pid {proc.pid} running 'holder-process'"
        with pytest.raises(FetcherBusy, match=busy), _lock(data_root):
            pass
    finally:
        proc.wait(timeout=30)
    with _lock(data_root):  # the holder exited: the slot is free
        pass


def test_stopgap_process_is_detected_but_editors_and_grep_are_not():
    others = [
        ProcessInfo(1, "vim /tmp/scripts/stopgap_rainier.py"),
        ProcessInfo(2, "grep -E stopgap_rainier.py"),
        ProcessInfo(3, "/usr/bin/python3 /tmp/other_script.py"),
        ProcessInfo(os.getpid(), "python3 stopgap_rainier.py"),  # never ourselves
    ]
    assert foreign_processes([*others, STOPGAP]) == [STOPGAP]
    assert foreign_processes(others) == []


def test_running_stopgap_refuses_the_lock(data_root):
    with (
        pytest.raises(FetcherBusy, match="process 79758 is running"),
        _lock(data_root, processes=lambda: [STOPGAP]),
    ):
        pass
    with _lock(data_root, processes=lambda: None):  # ps unavailable and no log: allowed
        pass


def test_recent_stopgap_log_records_refuse_and_old_ones_do_not(data_root):
    recent = (NOW - timedelta(minutes=4)).isoformat()
    old = (NOW - FOREIGN_LOG_WINDOW - timedelta(minutes=1)).isoformat()
    _log(
        data_root,
        {"vp": "rainier.paradise-mountain", "event": "stored", "t": old},
        {"t": recent, "worker": "capture", "viewpoint_id": "x", "event": "pass"},
    )
    with _lock(data_root):  # an old stopgap record and our own records: allowed
        pass
    _log(data_root, {"vp": "rainier.paradise-mountain", "event": "rate_capped", "t": recent})
    with (data_root / "capture_log" / "2026-11-19.jsonl").open("a") as handle:
        handle.write('{"torn": ')  # a write cut short
    with (
        pytest.raises(FetcherBusy, match="stopgap-shaped capture_log record") as caught,
        _lock(data_root),
    ):
        pass
    assert "rate_capped" in str(caught.value) and "clear from" in str(caught.value)
    hits = recent_foreign_log_records(data_root / "capture_log", NOW)
    assert [h["event"] for h in hits] == ["rate_capped"]


def test_log_check_reads_yesterday_just_after_utc_midnight(data_root):
    folder = data_root / "capture_log"
    folder.mkdir(parents=True)
    just_before = datetime(2026, 11, 19, 23, 58, tzinfo=UTC)
    (folder / "2026-11-19.jsonl").write_text(
        json.dumps(
            {"vp": "rainier.paradise-mountain", "event": "stored", "t": just_before.isoformat()}
        )
        + "\n"
    )
    after_midnight = datetime(2026, 11, 20, 0, 3, tzinfo=UTC)
    assert len(recent_foreign_log_records(folder, after_midnight)) == 1


def test_cli_fetch_commands_exit_3_while_another_fetcher_holds_the_lock(data_root, capsys):
    with nps_fetcher_lock("someone else", processes=lambda: []):
        assert main(["once", "--only", "rainier.paradise-mountain"]) == EXIT_FETCHER_BUSY
        assert main(["archive", "days", "point-reyes.beach"]) == EXIT_FETCHER_BUSY
        assert main(["archive-check", "--viewpoint", "point-reyes.beach"]) == EXIT_FETCHER_BUSY
        assert main(["status", "--json"]) == 0  # no network, no lock
    err = capsys.readouterr().err
    assert err.count("refused, nothing was sent") == 3 and "someone else" in err
    # Nothing was sent or written by the refused commands.
    assert not (data_root / "capture_log").exists()
    assert not LocalStorage(data_root).list_prefix("frames/")


def test_cli_refuses_when_the_stopgap_runs(data_root, monkeypatch, capsys):
    monkeypatch.setattr(guard_module, "list_processes", lambda: [STOPGAP])
    assert main(["once"]) == EXIT_FETCHER_BUSY
    assert "stopgap_rainier.py" in capsys.readouterr().err


def test_loop_stops_when_a_foreign_fetcher_appears(data_root, monkeypatch, capsys):
    import snow_globe_capture.live as live

    calls = {"passes": 0}

    def fake_pass(self, viewpoints, **kwargs):
        calls["passes"] += 1
        return {"counts": {}, "errors": 0, "outcomes": {}}

    monkeypatch.setattr(live.CaptureRunner, "run_pass", fake_pass)
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    seen = iter([[], [], [STOPGAP]])  # lock check, first pass check, then the stopgap appears
    monkeypatch.setattr(guard_module, "list_processes", lambda: next(seen))
    started = time.monotonic()
    code = main(["loop", "--interval", "0", "--jitter", "0", "--only", "point-reyes.beach"])
    assert code == EXIT_FETCHER_BUSY and calls["passes"] == 1
    assert time.monotonic() - started < 10
    log = (data_root / "capture_log").glob("*.jsonl")
    events = [json.loads(line)["event"] for path in log for line in path.read_text().splitlines()]
    assert "loop_stopped" in events


# --- host-wide across both default data folders (D-080) -------------------------------------

#: The lock protocol of the running lookout-capture 0.2.1 loop, which locks only
#: ``<its data dir>/state/locks/nps-fetcher.lock``: ``flock(LOCK_EX | LOCK_NB)`` on the file,
#: then its pid, command and start time written into it, cleared on release. Held until a line
#: arrives on stdin; prints "busy" and exits 3 if the lock is taken.
_OLD_LOOP = """
import fcntl, json, os, sys
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("busy", flush=True)
    sys.exit(3)
os.ftruncate(fd, 0)
info = {"pid": os.getpid(), "command": "lookout-capture loop --interval 300",
        "started_at": "2026-11-20T19:56:14Z"}
os.pwrite(fd, json.dumps(info).encode(), 0)
print("held", flush=True)
sys.stdin.readline()
os.ftruncate(fd, 0)
"""


class _OldLoop:
    """A process holding a lock file the way the 0.2.1 loop does (see :data:`_OLD_LOOP`)."""

    def __init__(self, tmp_path: Path, lock_file: Path) -> None:
        script = tmp_path / "old_loop.py"
        script.write_text(_OLD_LOOP)
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(script), str(lock_file)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        self.first_line = self.proc.stdout.readline().strip()

    def stop(self) -> int:
        if self.proc.poll() is None:
            assert self.proc.stdin is not None
            self.proc.stdin.write("stop\n")
            self.proc.stdin.flush()
        return self.proc.wait(timeout=30)


def _old_lock(home: Path) -> Path:
    return home / "lookout-data" / "state" / "locks" / "nps-fetcher.lock"


def test_a_live_lock_in_lookout_data_refuses_a_fetcher_whose_data_dir_is_elsewhere(
    tmp_path, data_root, no_host_lock_home
):
    """The running 0.2.1 loop holds ~/lookout-data's lock; a renamed fetcher whose
    SNOW_GLOBE_DATA_DIR names a scratch folder is still refused, and the holder's record
    is left as it was."""
    home = no_host_lock_home
    (home / "lookout-data").mkdir(parents=True)
    old = _OldLoop(tmp_path, _old_lock(home))
    try:
        assert old.first_line == "held"
        before = _old_lock(home).read_text()
        with (
            pytest.raises(
                FetcherBusy,
                match=rf"lookout-data/state/locks/nps-fetcher.lock .*pid {old.proc.pid}",
            ),
            _lock(data_root),
        ):
            pass
        assert _old_lock(home).read_text() == before  # never truncated by the refused process
        # The refused call let go of the scratch data dir's own lock again.
        fd = os.open(data_root / "state" / "locks" / "nps-fetcher.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        assert old.stop() == 0
    with _lock(data_root):  # the old loop stopped: the slot is free
        pass


def test_a_snow_globe_data_folder_made_early_does_not_split_the_lock(
    tmp_path, monkeypatch, no_host_lock_home
):
    """Both default folders exist apart (something made ~/snow-globe-data before the switch) and
    the variable names the new one: the live lock in ~/lookout-data still refuses it."""
    home = no_host_lock_home
    (home / "lookout-data").mkdir(parents=True)
    (home / "snow-globe-data").mkdir()
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(home / "snow-globe-data"))
    old = _OldLoop(tmp_path, _old_lock(home))
    try:
        assert old.first_line == "held"
        with (
            pytest.raises(FetcherBusy, match="a default data folder's lock"),
            nps_fetcher_lock("x", processes=lambda: []),
        ):
            pass
    finally:
        assert old.stop() == 0


def test_old_code_is_refused_while_a_renamed_fetcher_holds_the_slot(
    tmp_path, data_root, no_host_lock_home
):
    """The reverse direction: while a renamed fetcher runs with any data dir, it also holds
    ~/lookout-data's lock, so old code started then is refused; on release the record is cleared."""
    home = no_host_lock_home
    (home / "lookout-data").mkdir(parents=True)
    with _lock(data_root):
        held = json.loads(_old_lock(home).read_text())
        assert held["pid"] == os.getpid() and held["command"] == "test"
        old = _OldLoop(tmp_path, _old_lock(home))
        assert old.first_line == "busy" and old.stop() == 3
    assert _old_lock(home).read_text() == ""
    old = _OldLoop(tmp_path, _old_lock(home))
    try:
        assert old.first_line == "held"
    finally:
        assert old.stop() == 0


def test_after_the_switch_the_symlinked_folder_is_one_lock(
    tmp_path, monkeypatch, no_host_lock_home
):
    """~/lookout-data is a symlink to ~/snow-globe-data: one file, locked once (no self-conflict),
    and an old snapshot locking it through the old name is refused."""
    home = no_host_lock_home
    (home / "snow-globe-data").mkdir(parents=True)
    (home / "lookout-data").symlink_to(home / "snow-globe-data")
    monkeypatch.setenv("SNOW_GLOBE_DATA_DIR", str(home / "snow-globe-data"))
    with nps_fetcher_lock("new loop", processes=lambda: []) as guard:
        assert guard.lock_path == home / "snow-globe-data" / "state" / "locks" / "nps-fetcher.lock"
        old = _OldLoop(tmp_path, _old_lock(home))
        assert old.first_line == "busy" and old.stop() == 3
        with pytest.raises(FetcherBusy, match="pid"), _lock(tmp_path / "elsewhere"):
            pass
    assert sorted(p.name for p in (home / "snow-globe-data" / "state" / "locks").iterdir()) == [
        "nps-fetcher.lock"
    ]


def test_no_default_folder_is_created(data_root, no_host_lock_home):
    with _lock(data_root):
        pass
    assert not no_host_lock_home.exists()
    assert guard_module.host_lock_paths(no_host_lock_home) == []


def test_host_lock_paths_skip_missing_dangling_and_plain_files(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "snow-globe-data").symlink_to(home / "nowhere")
    (home / "lookout-data").write_text("not a folder")
    assert guard_module.host_lock_paths(home) == []
    (home / "lookout-data").unlink()
    (home / "lookout-data").mkdir()
    assert guard_module.host_lock_paths(home) == [_old_lock(home)]
    assert not (home / "lookout-data" / "state").exists()  # listing creates nothing


_LOOP_0_2_1 = (
    "/Users/me/lookout-data/cache/runtime/3cd2ca7/workers/capture/.venv/bin/python "
    "/Users/me/lookout-data/cache/runtime/3cd2ca7/workers/capture/.venv/bin/lookout-capture "
    "loop --interval 300 --archive-queue /Users/me/lookout-data/state/archive/queue.json"
)


def test_other_capture_fetchers_are_detected_by_process_under_either_name():
    venv = "/x/workers/capture/.venv/bin"
    fetchers = [
        ProcessInfo(11, _LOOP_0_2_1),
        ProcessInfo(12, f"{venv}/python {venv}/snow-globe-capture once --only point-reyes.beach"),
        ProcessInfo(
            13,
            f"{venv}/python {venv}/snow-globe-capture --manifests /m archive day pore 2025-01-02",
        ),
        ProcessInfo(14, f"{venv}/python3 {venv}/lookout-capture archive-check --budget 10"),
        ProcessInfo(
            15,
            f"{venv}/python {venv}/snow-globe-capture archive sample acadia.mcfarland 2025-01-02",
        ),
        ProcessInfo(16, f"{venv}/python {venv}/lookout-capture archive days point-reyes.beach"),
    ]
    others = [
        ProcessInfo(21, "caffeinate -s " + _LOOP_0_2_1.split(" ", 1)[1]),  # the loop's own wrapper
        ProcessInfo(22, "uv run --no-sync snow-globe-capture once"),  # uv starts the python child
        ProcessInfo(23, f"{venv}/python {venv}/snow-globe-capture status --json"),
        ProcessInfo(
            24, f"{venv}/python {venv}/snow-globe-capture archive queue add pore 2025-01-02"
        ),
        ProcessInfo(25, f"{venv}/python {venv}/lookout-capture cadence rainier.paradise-mountain"),
        ProcessInfo(26, f"{venv}/python {venv}/snow-globe-capture storage --day 2026-11-20"),
        ProcessInfo(27, "vim workers/capture/src/snow_globe_capture/cli.py"),
        ProcessInfo(28, "grep -r snow-globe-capture loop docs"),
        ProcessInfo(
            os.getpid(), f"{venv}/python {venv}/snow-globe-capture loop"
        ),  # never ourselves
    ]
    assert guard_module.capture_fetchers([*fetchers, *others]) == fetchers
    assert guard_module.capture_fetchers(others) == []


def test_a_running_lookout_capture_loop_refuses_even_with_no_shared_lock(
    data_root, monkeypatch, capsys
):
    """Old code whose data dir is not a default folder shares no lock with this process: its
    process line still refuses the fetch, with exit status 3, before anything is sent."""
    monkeypatch.setattr(guard_module, "list_processes", lambda: [ProcessInfo(70525, _LOOP_0_2_1)])
    assert main(["once", "--only", "rainier.paradise-mountain"]) == EXIT_FETCHER_BUSY
    err = capsys.readouterr().err
    assert "process 70525 is running" in err and "lookout-capture" in err
    assert not (data_root / "capture_log").exists()
    assert main(["status", "--json"]) == 0  # offline commands take no lock and check nothing
