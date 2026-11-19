"""One NPS fetcher per machine (CLAUDE.md "Never run two NPS fetchers at once"; D-021).

Every console command that sends requests to www.nps.gov (``once``, ``loop``,
``archive ...`` and ``archive-check``) runs inside :func:`nps_fetcher_lock`, which:

1. takes a host-wide ``flock`` on ``<local state dir>/locks/nps-fetcher.lock`` without
   waiting, so a second fetcher fails at once and is told who holds the lock (pid,
   command, start time);
2. refuses to start while a fetcher that does not take this lock is active: a running
   Rainier stopgap (``stopgap_rainier.py``, D-021), or stopgap-shaped records (``"vp"``
   without ``"worker"``) in the local ``capture_log`` from the last
   :data:`FOREIGN_LOG_WINDOW` (two 5-minute poll slots plus a minute). After the stopgap
   is stopped, capture can start once that window has passed.

``loop`` repeats check 2 before every pass and stops if a foreign fetcher appears.

The lock is always on local disk, whatever ``LOOKOUT_STORAGE`` says, because the rule is
about this machine. The Lambda handler does not take it: on AWS, concurrent invocations
are handled by the per-camera S3 locks, and the stopgap must be stopped before the AWS job
goes live (D-021).
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from lookout_common import paths
from lookout_common.timeutil import isoformat_z, utcnow

from .state import parse_time

__all__ = [
    "FOREIGN_LOG_WINDOW",
    "FOREIGN_SCRIPTS",
    "LOCK_FILE",
    "FetcherBusy",
    "FetcherGuard",
    "ProcessInfo",
    "foreign_processes",
    "list_processes",
    "nps_fetcher_lock",
    "recent_foreign_log_records",
]

LOCK_FILE = "nps-fetcher.lock"
#: Scripts known to fetch from nps.gov without taking the lock (D-021's stopgap).
FOREIGN_SCRIPTS: tuple[str, ...] = ("stopgap_rainier.py",)
#: The stopgap logs every poll (one HEAD per 300 s); two slots plus a minute.
FOREIGN_LOG_WINDOW = timedelta(minutes=11)
_LOG_TAIL_BYTES = 512 * 1024


class FetcherBusy(RuntimeError):
    """Another NPS fetcher is active on this machine, so nothing was sent."""


@dataclass(frozen=True)
class ProcessInfo:
    """One running process: its pid and full command line."""

    pid: int
    command: str


def list_processes() -> list[ProcessInfo] | None:
    """Every process on this machine (``ps -axww -o pid=,command=``), or None if ps fails."""
    try:
        done = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    rows: list[ProcessInfo] = []
    for line in done.stdout.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if pid_text.isdigit():
            rows.append(ProcessInfo(int(pid_text), command.strip()))
    return rows


def foreign_processes(
    processes: Sequence[ProcessInfo], *, scripts: Sequence[str] = FOREIGN_SCRIPTS
) -> list[ProcessInfo]:
    """Python processes running one of ``scripts`` (an editor or ``grep`` naming it is not one)."""
    me = os.getpid()
    hits = []
    for proc in processes:
        if proc.pid == me or "python" not in proc.command.lower():
            continue
        if any(os.path.basename(token) in scripts for token in proc.command.split()):
            hits.append(proc)
    return hits


def _tail(path: Path, size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        start = max(0, end - size)
        handle.seek(start)
        data = handle.read()
    if start > 0:
        data = data.split(b"\n", 1)[1] if b"\n" in data else b""
    return data


def recent_foreign_log_records(
    log_dir: Path, now: datetime, *, window: timedelta = FOREIGN_LOG_WINDOW
) -> list[dict[str, Any]]:
    """Stopgap-shaped records (``"vp"``, no ``"worker"``) in ``capture_log`` within ``window``."""
    days: set[date] = {now.date(), (now - window).date()}
    hits: list[dict[str, Any]] = []
    for day in sorted(days):
        path = log_dir / f"{day.isoformat()}.jsonl"
        try:
            data = _tail(path, _LOG_TAIL_BYTES)
        except FileNotFoundError:
            continue
        for raw in data.splitlines():
            try:
                record = json.loads(raw)
            except ValueError:
                continue  # a torn final line, or a foreign writer's junk
            if not isinstance(record, dict) or "vp" not in record or "worker" in record:
                continue
            when = parse_time(record.get("t"))
            if when is not None and now - window <= when <= now + timedelta(minutes=1):
                hits.append(record)
    return hits


@dataclass
class FetcherGuard:
    """What the lock holder may ask again later: is a foreign fetcher active now?"""

    command: str
    lock_path: Path
    log_dir: Path
    clock: Callable[[], datetime]
    processes: Callable[[], list[ProcessInfo] | None]

    def check_foreign(self) -> None:
        """Raise :class:`FetcherBusy` if a stopgap process runs or logged in the last window."""
        problems: list[str] = []
        listed = self.processes()
        if listed is not None:
            for proc in foreign_processes(listed):
                problems.append(f"process {proc.pid} is running {proc.command[:160]!r}")
        now = self.clock()
        records = recent_foreign_log_records(self.log_dir, now)
        if records:
            last = max(records, key=lambda r: str(r.get("t")))
            when = parse_time(last.get("t"))
            clear_at = isoformat_z(when + FOREIGN_LOG_WINDOW, "seconds") if when else "?"
            problems.append(
                f"{len(records)} stopgap-shaped capture_log record(s) since "
                f"{isoformat_z(now - FOREIGN_LOG_WINDOW, 'seconds')}; the last is "
                f"{last.get('event')!r} for {last.get('vp')!r} at {last.get('t')} "
                f"(clear from {clear_at} if the stopgap is stopped)"
            )
        if problems:
            raise FetcherBusy(
                "another NPS fetcher is active on this machine (CLAUDE.md: never run two at "
                "once; D-021: stop the Rainier stopgap before capture or archive runs): "
                + "; ".join(problems)
            )


def _holder(fd: int) -> str:
    try:
        raw = os.pread(fd, 4096, 0)
        info = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        info = {}
    if not isinstance(info, dict) or not info:
        return "an unknown process"
    return f"pid {info.get('pid')} running {info.get('command')!r} since {info.get('started_at')}"


@contextmanager
def nps_fetcher_lock(
    command: str,
    *,
    state_dir: Path | None = None,
    log_dir: Path | None = None,
    clock: Callable[[], datetime] = utcnow,
    processes: Callable[[], list[ProcessInfo] | None] | None = None,
) -> Iterator[FetcherGuard]:
    """Hold the machine's single NPS-fetcher slot for a ``with`` block, or raise FetcherBusy.

    ``state_dir`` and ``log_dir`` default to ``<data dir>/state`` and ``<data dir>/capture_log``
    on local disk. ``processes`` defaults to :func:`list_processes` (looked up at call time,
    so tests can replace it).
    """
    folder = (state_dir or paths.state_dir()) / "locks"
    folder.mkdir(parents=True, exist_ok=True)
    lock_path = folder / LOCK_FILE
    guard = FetcherGuard(
        command=command,
        lock_path=lock_path,
        log_dir=log_dir or (paths.data_dir() / "capture_log"),
        clock=clock,
        processes=processes or (lambda: list_processes()),
    )
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FetcherBusy(
                f"another NPS fetcher holds {lock_path}: {_holder(fd)} (CLAUDE.md: never run "
                "two NPS fetchers at once)"
            ) from None
        try:
            guard.check_foreign()
            info = {
                "pid": os.getpid(),
                "command": command,
                "started_at": isoformat_z(clock(), "seconds"),
            }
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps(info).encode(), 0)
            yield guard
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
