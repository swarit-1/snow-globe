"""One NPS fetcher per machine: the host-wide lock, stopgap detection, and the CLI exit code."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lookout_common.storage import LocalStorage

import lookout_capture.fetcher_guard as guard_module
from lookout_capture.cli import EXIT_FETCHER_BUSY, main
from lookout_capture.fetcher_guard import (
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
from lookout_capture.fetcher_guard import nps_fetcher_lock
root = Path(sys.argv[1])
with nps_fetcher_lock("holder-process", state_dir=root / "state", log_dir=root / "capture_log",
                      processes=lambda: []):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def test_a_fetcher_in_another_process_blocks_this_one(tmp_path, data_root):
    script = tmp_path / "hold.py"
    script.write_text(_HOLD)
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
    import lookout_capture.live as live

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
