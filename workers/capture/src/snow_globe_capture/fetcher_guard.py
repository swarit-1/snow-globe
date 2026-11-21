"""One NPS fetcher per machine (CLAUDE.md "Never run two NPS fetchers at once"; D-021).

Every console command that sends requests to www.nps.gov (``once``, ``loop``,
``archive ...`` and ``archive-check``) runs inside :func:`nps_fetcher_lock`, which:

1. takes a host-wide ``flock`` on ``<local state dir>/locks/nps-fetcher.lock`` without
   waiting, so a second fetcher fails at once and is told who holds the lock (pid,
   command, start time);
2. takes the same lock in each *default* data folder that exists, ``~/snow-globe-data`` and
   ``~/lookout-data`` (its name before the rename, D-080), whichever data dir this process
   resolved (:func:`host_lock_paths`). So the lock stays host-wide when two processes resolve
   different data dirs, through a variable or a folder created early: the running
   ``lookout-capture`` 0.2.1 loop locks ``~/lookout-data/state/locks/nps-fetcher.lock``, and
   every renamed fetcher respects a live lock there. After the rename's live switch
   ``~/lookout-data`` is a symlink to ``~/snow-globe-data``, and the two paths are one file,
   locked once;
3. refuses to start while a fetcher that may not share any of these locks is active: another
   ``snow-globe-capture`` or ``lookout-capture`` process running a fetching command
   (:func:`capture_fetchers`), a running Rainier stopgap (``stopgap_rainier.py``, D-021), or
   stopgap-shaped records (``"vp"`` without ``"worker"``) in the local ``capture_log`` from
   the last :data:`FOREIGN_LOG_WINDOW` (two 5-minute poll slots plus a minute). After the
   stopgap is stopped, capture can start once that window has passed.

``loop`` repeats check 3 before every pass and stops if a foreign fetcher appears.

The lock is always on local disk, whatever ``SNOW_GLOBE_STORAGE`` says, because the rule is
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

from snow_globe_common import paths
from snow_globe_common.timeutil import isoformat_z, utcnow

from .state import parse_time

__all__ = [
    "CAPTURE_SCRIPTS",
    "FETCHING_COMMANDS",
    "FOREIGN_LOG_WINDOW",
    "FOREIGN_SCRIPTS",
    "LOCK_FILE",
    "FetcherBusy",
    "FetcherGuard",
    "ProcessInfo",
    "capture_fetchers",
    "foreign_processes",
    "host_home",
    "host_lock_paths",
    "list_processes",
    "nps_fetcher_lock",
    "recent_foreign_log_records",
]

LOCK_FILE = "nps-fetcher.lock"
#: Scripts known to fetch from nps.gov without taking the lock (D-021's stopgap).
FOREIGN_SCRIPTS: tuple[str, ...] = ("stopgap_rainier.py",)
#: The capture console script under its name and its name before the rename (D-080).
CAPTURE_SCRIPTS: tuple[str, ...] = ("snow-globe-capture", "lookout-capture")
#: Its commands that fetch from nps.gov (``archive`` only with these subcommands).
FETCHING_COMMANDS: tuple[str, ...] = ("once", "loop", "archive-check")
_FETCHING_ARCHIVE = ("days", "day", "sample")
_CAPTURE_COMMANDS = (
    *FETCHING_COMMANDS,
    "archive",
    "status",
    "cadence",
    "storage",
    "storage-report",
)
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


def _fetching_command(tokens: Sequence[str]) -> bool:
    """True if the words after the capture script name a command that fetches from nps.gov.

    The first word that is one of the script's commands decides, so an option's value (such
    as ``--manifests <folder>``) is skipped; ``archive`` fetches with ``days``, ``day`` and
    ``sample`` but not with ``queue``.
    """
    for i, token in enumerate(tokens):
        if token not in _CAPTURE_COMMANDS:
            continue
        if token != "archive":
            return token in FETCHING_COMMANDS
        rest = [t for t in tokens[i + 1 :] if t in (*_FETCHING_ARCHIVE, "queue")]
        return bool(rest) and rest[0] in _FETCHING_ARCHIVE
    return False


def capture_fetchers(processes: Sequence[ProcessInfo]) -> list[ProcessInfo]:
    """Other Python processes running a fetching ``snow-globe-capture``/``lookout-capture`` command.

    Such a process takes the NPS fetcher lock in its own data dir, which may not be one this
    process locks (a variable naming a scratch folder, or old code). Wrappers that only start
    one (``caffeinate``, ``uv run``, a shell) are not Python processes, the offline commands
    (``status``, ``cadence``, ``storage``, ``archive queue ...``) are not fetchers, and this
    process never counts itself.
    """
    me = os.getpid()
    hits = []
    for proc in processes:
        if proc.pid == me or "python" not in proc.command.lower():
            continue
        tokens = proc.command.split()
        for i, token in enumerate(tokens):
            if os.path.basename(token) in CAPTURE_SCRIPTS:
                if _fetching_command(tokens[i + 1 :]):
                    hits.append(proc)
                break
    return hits


def _capture_words(command: str) -> str:
    """``lookout-capture loop --interval 300 ...``: a capture command line from its script name."""
    tokens = command.split()
    for i, token in enumerate(tokens):
        if os.path.basename(token) in CAPTURE_SCRIPTS:
            return " ".join([os.path.basename(token), *tokens[i + 1 :]])[:160]
    return command[:160]


def host_home() -> Path:
    """The home folder whose default data folders :func:`host_lock_paths` locks.

    Looked up at call time, so the tests can point it at a scratch folder.
    """
    return Path.home()


def host_lock_paths(home: Path | None = None) -> list[Path]:
    """``state/locks/nps-fetcher.lock`` in each default data folder that exists (D-080).

    ``~/snow-globe-data`` and ``~/lookout-data`` (:data:`snow_globe_common.paths.DEFAULT_DATA_DIR`
    and :data:`~snow_globe_common.paths.LEGACY_DATA_DIR`), symlinks followed; a folder that does
    not exist is never created, and a plain file is skipped. Both may name one folder (after the
    switch ``~/lookout-data`` is a symlink to ``~/snow-globe-data``): :func:`nps_fetcher_lock`
    then locks the one file once.
    """
    base = home if home is not None else host_home()
    found = []
    for name in (paths.DEFAULT_DATA_DIR, paths.LEGACY_DATA_DIR):
        folder = base / name.removeprefix("~/")
        if folder.is_dir():
            found.append(folder / "state" / "locks" / LOCK_FILE)
    return found


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
        """Raise :class:`FetcherBusy` if another capture fetcher or a stopgap runs, or a
        stopgap logged in the last window."""
        problems: list[str] = []
        listed = self.processes()
        if listed is not None:
            for proc in capture_fetchers(listed):
                problems.append(f"process {proc.pid} is running {_capture_words(proc.command)!r}")
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
    on local disk. The lock in ``<state dir>/locks`` is taken first, then the one in each
    default data folder that exists (:func:`host_lock_paths`; each lock file once, by device
    and inode), all without waiting; the holder's pid, command and start time are written
    into each, and cleared on release, as the 0.2.1 code does with its one lock.
    ``processes`` defaults to :func:`list_processes` (looked up at call time, so tests can
    replace it).
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
    held: list[int] = []  # descriptors this call locked, in order
    opened: list[int] = []
    seen: set[tuple[int, int]] = set()
    try:
        for path in (lock_path, *host_lock_paths()):
            if path != lock_path:
                path.parent.mkdir(parents=True, exist_ok=True)  # its data folder exists
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            opened.append(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) in seen:
                continue  # the same file by another path (a symlinked data folder)
            seen.add((info.st_dev, info.st_ino))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                where = (
                    ""
                    if path == lock_path
                    else (
                        " (a default data folder's lock, which every fetcher takes whatever data "
                        "dir it uses, D-080)"
                    )
                )
                raise FetcherBusy(
                    f"another NPS fetcher holds {path}{where}: {_holder(fd)} (CLAUDE.md: never "
                    "run two NPS fetchers at once)"
                ) from None
            held.append(fd)
        try:
            guard.check_foreign()
            record = json.dumps(
                {
                    "pid": os.getpid(),
                    "command": command,
                    "started_at": isoformat_z(clock(), "seconds"),
                }
            ).encode()
            for fd in held:
                os.ftruncate(fd, 0)
                os.pwrite(fd, record, 0)
            yield guard
        finally:
            for fd in held:
                os.ftruncate(fd, 0)
    finally:
        for fd in reversed(held):
            fcntl.flock(fd, fcntl.LOCK_UN)
        for fd in opened:
            os.close(fd)
