"""Polite HTTP for every Lookout fetcher (PRD §15.3; CLAUDE.md "Polite collection").

- :func:`polite_client` returns an ``httpx.Client`` that identifies the project in its
  User-Agent (never the owner) on every request, redirects included, uses bounded
  timeouts and keeps no cookies.
- :class:`MinIntervalLimiter` spaces requests that share a name, across every process
  on this machine, using ``fcntl.flock`` on a file under ``state/ratelimit/`` that
  records the last request time. ``hold()`` also gives "one request at a time".
  Only on AWS Lambda, which has no shared disk, does it fall back to an in-process
  limiter; cross-invocation spacing there must come from the scheduler (one
  concurrent run). ``LOOKOUT_STORAGE=s3`` alone never weakens it.
- :func:`backoff_delay` and :meth:`MinIntervalLimiter.defer` implement "back off on
  errors": deferring pushes the next allowed request out for every process.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Literal

import httpx

from .timeutil import utcnow

__all__ = [
    "BACKOFF_CAP_S",
    "DEFAULT_TIMEOUT",
    "DEFAULT_USER_AGENT",
    "ENV_LAMBDA_FUNCTION",
    "ENV_USER_AGENT",
    "MinIntervalLimiter",
    "NullCookieJar",
    "backoff_delay",
    "on_aws_lambda",
    "polite_client",
    "retry_after_s",
    "user_agent",
]

ENV_USER_AGENT = "LOOKOUT_USER_AGENT"
#: Set by the AWS Lambda runtime in every function's environment.
ENV_LAMBDA_FUNCTION = "AWS_LAMBDA_FUNCTION_NAME"
#: CLAUDE.md: the default UA names the project, never a person.
DEFAULT_USER_AGENT = "Lookout/0.1 (webcam research capture; fetch-on-change)"
#: 10 s to connect, 30 s to read or write a body, 10 s waiting for a pooled connection.
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0, pool=10.0)
#: The longest back-off :func:`backoff_delay` gives by default: one hour.
BACKOFF_CAP_S = 3600.0

_NAME_RE = re.compile(r"^[a-z0-9]+(?:[-_.][a-z0-9]+)*$")


class NullCookieJar(CookieJar):
    """A cookie jar that never stores anything, so no request carries a cookie."""

    def set_cookie(self, cookie: Any) -> None:
        return None

    def extract_cookies(self, response: Any, request: Any) -> None:
        return None


def user_agent() -> str:
    """Return ``$LOOKOUT_USER_AGENT`` if set (non-empty), else :data:`DEFAULT_USER_AGENT`."""
    return (os.environ.get(ENV_USER_AGENT) or "").strip() or DEFAULT_USER_AGENT


def on_aws_lambda() -> bool:
    """True inside an AWS Lambda function (PRD §11.3): ``AWS_LAMBDA_FUNCTION_NAME`` is set."""
    return bool((os.environ.get(ENV_LAMBDA_FUNCTION) or "").strip())


def _pin_user_agent(value: str) -> Callable[[httpx.Request], None]:
    """A request hook that sets the User-Agent to ``value`` just before each send."""

    def hook(request: httpx.Request) -> None:
        request.headers["User-Agent"] = value

    return hook


def polite_client(
    *,
    timeout: httpx.Timeout | float | None = None,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Return an ``httpx.Client`` set up for polite collection (PRD §15.3).

    - User-Agent from :func:`user_agent`, read once when the client is made. A request
      hook sets it on every request, redirects included, so neither ``headers`` here
      nor a per-request ``headers=`` can replace it (CLAUDE.md "Privacy").
    - :data:`DEFAULT_TIMEOUT` unless ``timeout`` is given.
    - Cookies are never persisted (:class:`NullCookieJar`), and at most 5 redirects.

    ``transport`` exists for tests (``httpx.MockTransport``). Use the client as a
    context manager so connections close.
    """
    agent = user_agent()
    merged = {"User-Agent": agent}
    if headers:
        merged.update({k: v for k, v in headers.items() if k.lower() != "user-agent"})
    return httpx.Client(
        headers=merged,
        timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
        cookies=NullCookieJar(),
        follow_redirects=follow_redirects,
        max_redirects=5,
        transport=transport,
        event_hooks={"request": [_pin_user_agent(agent)]},
    )


def retry_after_s(response: httpx.Response) -> float | None:
    """Seconds asked for by a ``Retry-After`` header (delta or HTTP date), or None."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - utcnow()).total_seconds())


def backoff_delay(failures: int, *, base_s: float = 60.0, cap_s: float = BACKOFF_CAP_S) -> float:
    """Exponential back-off: ``base_s * 2**(failures-1)`` capped at ``cap_s`` (0 if no failures)."""
    if failures <= 0:
        return 0.0
    return float(min(cap_s, base_s * 2 ** min(failures - 1, 30)))


class _ProcessSlot:
    """In-process state shared by every limiter with the same name in one process."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last = 0.0
        self.not_before = 0.0


_PROCESS_SLOTS: dict[str, _ProcessSlot] = {}
_PROCESS_SLOTS_GUARD = threading.Lock()


def _process_slot(name: str) -> _ProcessSlot:
    with _PROCESS_SLOTS_GUARD:
        return _PROCESS_SLOTS.setdefault(name, _ProcessSlot())


class MinIntervalLimiter:
    """Keep at least ``min_interval_s`` between requests that share ``name`` (PRD §15.3).

    Modes:

    - ``"file"`` coordinates every process on this machine through
      ``<state_dir>/ratelimit/<name>.lock`` (``flock``) and ``<name>.json`` (the last
      request time and any back-off, replaced atomically).
    - ``"process"`` coordinates the threads of this process only.
    - ``"auto"`` (the default) picks ``"process"`` only on AWS Lambda
      (``AWS_LAMBDA_FUNCTION_NAME`` set), which has no shared disk, and ``"file"``
      everywhere else, whatever ``LOOKOUT_STORAGE`` says. On Lambda, spacing across
      invocations must come from the scheduler (reserved concurrency 1).

    Times are wall-clock seconds so separate processes agree. If the stored time is
    in the future (clock stepped back), the spacing wait is capped at one interval.

    ``wait()`` before a request spaces request *starts*. ``with limiter.hold():``
    around a request also serialises requests across processes and spaces them end
    to start; the NPS archive uses ``hold()`` with 5 s (CLAUDE.md: one at a time,
    >= 5 s apart). Both sleep with the lock released, then check again, so a long
    ``defer()`` back-off never makes another process's ``defer()`` or
    ``next_allowed_in()`` wait. ``hold()`` keeps the lock while its request runs
    (that is what makes it one at a time), so a ``defer()`` from another process can
    wait for that one request to finish, never longer.

    ``next_allowed_in()`` reads the shared state without the lock and never blocks.
    It cannot see a request in flight in another process, so treat it as a lower
    bound; it does see every recorded back-off, which is what a scheduled worker
    needs to decide whether to skip a cycle.

    A state file that cannot be parsed (writes are atomic, so only a foreign or
    hand-edited file) is read as "a request just happened and a back-off of
    ``corrupt_hold_off_s`` is active" (default :data:`BACKOFF_CAP_S`, one hour: the
    longest back-off :func:`backoff_delay` gives). The next locked call writes that
    state back, so an unreadable file can lengthen a back-off but never shorten it.
    """

    def __init__(
        self,
        name: str,
        min_interval_s: float,
        *,
        state_dir: str | os.PathLike[str] | None = None,
        mode: Literal["auto", "file", "process"] = "auto",
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        corrupt_hold_off_s: float = BACKOFF_CAP_S,
    ) -> None:
        if not _NAME_RE.match(name):
            raise ValueError(f"limiter name {name!r} must be a lower-case slug like 'nps-archive'")
        if min_interval_s < 0:
            raise ValueError("min_interval_s must be >= 0")
        if corrupt_hold_off_s < 0:
            raise ValueError("corrupt_hold_off_s must be >= 0")
        if mode == "auto":
            mode = "process" if on_aws_lambda() else "file"
        if mode not in {"file", "process"}:
            raise ValueError(f"unknown limiter mode {mode!r}")
        self.name = name
        self.min_interval_s = float(min_interval_s)
        self.mode = mode
        self.corrupt_hold_off_s = float(corrupt_hold_off_s)
        self._clock = clock
        self._sleep = sleep
        self._thread_lock = threading.Lock()
        self._local = threading.local()
        self._lock_path: Path | None = None
        self._state_path: Path | None = None
        if mode == "file":
            if state_dir is None:
                from .paths import state_dir as default_state_dir

                state_dir = default_state_dir()
            folder = Path(state_dir) / "ratelimit"
            folder.mkdir(parents=True, exist_ok=True)
            self._lock_path = folder / f"{name}.lock"
            self._state_path = folder / f"{name}.json"

    def __repr__(self) -> str:
        return f"MinIntervalLimiter({self.name!r}, {self.min_interval_s}, mode={self.mode!r})"

    # -- shared-state primitives -------------------------------------------------
    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with self._thread_lock:
            if self.mode == "process":
                slot = _process_slot(self.name)
                with slot.lock:
                    yield
                return
            assert self._lock_path is not None
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read(self) -> tuple[float, float, bool]:
        """Return ``(last, not_before, unreadable)``; safe without the lock (atomic file)."""
        if self.mode == "process":
            slot = _process_slot(self.name)
            return slot.last, slot.not_before, False
        assert self._state_path is not None
        try:
            data = json.loads(self._state_path.read_text())
            last = float(data.get("last_unix", 0.0))
            not_before = float(data.get("not_before_unix", 0.0))
            if not (math.isfinite(last) and math.isfinite(not_before)):
                raise ValueError("non-finite time in limiter state")
            return last, not_before, False
        except FileNotFoundError:
            return 0.0, 0.0, False
        except (ValueError, TypeError, AttributeError):
            # A torn or foreign file. A back-off it held may be lost, so assume the
            # longest one rather than none (the conservative side).
            now = self._clock()
            return now, now + self.corrupt_hold_off_s, True

    def _read_locked(self) -> tuple[float, float]:
        """Read under the lock; write the conservative state back if the file was unreadable."""
        last, not_before, unreadable = self._read()
        if unreadable:
            self._write(last, not_before)
        return last, not_before

    def _write(self, last: float, not_before: float) -> None:
        if self.mode == "process":
            slot = _process_slot(self.name)
            slot.last, slot.not_before = last, not_before
            return
        assert self._state_path is not None
        payload = json.dumps(
            {
                "name": self.name,
                "min_interval_s": self.min_interval_s,
                "last_unix": last,
                "not_before_unix": not_before,
                "pid": os.getpid(),
            }
        )
        fd, tmp = tempfile.mkstemp(prefix=f".{self.name}.", dir=self._state_path.parent)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._state_path)

    def _delay(self, now: float, last: float, not_before: float) -> float:
        spacing = min(max(0.0, last + self.min_interval_s - now), self.min_interval_s)
        hold_off = max(0.0, not_before - now)
        return max(spacing, hold_off)

    # -- public API ----------------------------------------------------------------
    def _holding(self) -> bool:
        return getattr(self._local, "holding", False)

    def wait(self) -> float:
        """Block until a request may start, record it as started now, return seconds waited.

        Sleeps with the lock released and then checks again; if nobody changed the
        state meanwhile, the request may start (which keeps the one-interval cap
        when the clock has stepped back).
        """
        if self._holding():
            raise RuntimeError("wait() inside hold() of the same limiter would deadlock")
        waited = 0.0
        slept_on: tuple[float, float] | None = None
        while True:
            with self._exclusive():
                last, not_before = self._read_locked()
                delay = self._delay(self._clock(), last, not_before)
                if delay <= 0 or (last, not_before) == slept_on:
                    self._write(self._clock(), not_before)
                    return waited
            self._sleep(delay)
            waited += delay
            slept_on = (last, not_before)

    @contextmanager
    def hold(self) -> Iterator[float]:
        """Hold the slot for a whole request: one at a time, spaced end-to-start.

        Yields the seconds waited. Waiting happens with the lock released; the lock is
        held only from the moment the request may start until it ends. The finish time
        is recorded even if the block raises. ``defer()`` may be called inside the
        block; it takes effect on exit.
        """
        if self._holding():
            raise RuntimeError("hold() is not re-entrant")
        waited = 0.0
        slept_on: tuple[float, float] | None = None
        while True:
            with self._exclusive():
                last, not_before = self._read_locked()
                delay = self._delay(self._clock(), last, not_before)
                if delay <= 0 or (last, not_before) == slept_on:
                    self._local.holding = True
                    self._local.pending_not_before = not_before
                    try:
                        yield waited
                    finally:
                        pending = self._local.pending_not_before
                        self._local.holding = False
                        self._write(self._clock(), max(not_before, pending))
                    return
            self._sleep(delay)
            waited += delay
            slept_on = (last, not_before)

    def defer(self, seconds: float) -> None:
        """Forbid any request under this name for ``seconds`` from now (error back-off).

        Never shortens a back-off that is already recorded.
        """
        until = self._clock() + max(0.0, seconds)
        if self._holding():
            self._local.pending_not_before = max(self._local.pending_not_before, until)
            return
        with self._exclusive():
            last, not_before = self._read_locked()
            self._write(last, max(not_before, until))

    def next_allowed_in(self) -> float:
        """Seconds until the next request may start (0 if now); does not reserve it.

        Reads the state without the lock, so it never blocks; a request in flight in
        another process is not counted (see the class docstring).
        """
        if self._holding():
            return 0.0
        last, not_before, _ = self._read()
        return self._delay(self._clock(), last, not_before)
