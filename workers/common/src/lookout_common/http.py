"""Polite HTTP for every Lookout fetcher (PRD §15.3; CLAUDE.md "Polite collection").

- :func:`polite_client` returns an ``httpx.Client`` that identifies the project in its
  User-Agent (never the owner) on every request, redirects included, uses bounded
  timeouts and keeps no cookies.
- :class:`MinIntervalLimiter` spaces requests that share a name, across every process
  on this machine, using ``fcntl.flock`` on a file under ``state/ratelimit/`` that
  records the last request time. ``hold()`` also gives "one request at a time". The
  spacing holds on the wall clock (what the shared state and the logs use) and, between
  one process's own requests, on a sleep-inclusive monotonic clock too.
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

from .timeutil import steady_clock, utcnow

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


#: :data:`steady_clock` time of this process's last recorded request, per limiter (process
#: mode: its name; file mode: its state file's real path), shared by every instance on the
#: default clock. An instance with an injected ``monotonic`` keeps its own.
_OWN_LAST: dict[str, float] = {}
_OWN_LAST_GUARD = threading.Lock()

#: The longest back-off :meth:`MinIntervalLimiter.defer` records: 30 days. A longer one,
#: infinity included (a Retry-After of hundreds of digits), is recorded as this, so the state
#: stays finite and readable.
_DEFER_CAP_S = 30 * 86_400.0
#: The longest single sleep while a request waits. The clocks and the state are read again
#: after each, so a long back-off is waited out in pieces and ``time.sleep`` (which overflows
#: past about 9.2e9 s) is never asked for more.
_SLEEP_CAP_S = 3600.0
#: Sleeps in a row after which a ``monotonic`` clock that has not moved is taken as a broken
#: fake (a ``sleep`` that never lets time pass): the wait raises instead of spinning forever.
_STALL_LIMIT = 1000


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

    ``wait()`` before a request spaces request *starts*. ``with limiter.hold():``
    around a request also serialises requests across processes and spaces them end
    to start; the NPS archive uses ``hold()`` with 5.05 s (CLAUDE.md: one at a time,
    >= 5 s apart; the 50 ms is a margin for the log's own stamps, D-077). Both sleep
    with the lock released, then check again, so a long
    ``defer()`` back-off never makes another process's ``defer()`` or
    ``next_allowed_in()`` wait. ``hold()`` keeps the lock while its request runs
    (that is what makes it one at a time), so a ``defer()`` from another process can
    wait for that one request to finish, never longer.

    Clocks (D-077). The shared state holds wall-clock seconds (``clock``), so separate
    processes agree; it is the only reference they share. Real elapsed time is read on
    ``monotonic``: by default :data:`~lookout_common.timeutil.steady_clock` (it counts
    while the machine sleeps and is never stepped), unless both ``clock`` and ``sleep``
    are injected (a fake machine), when it is ``clock`` itself so the fake stays
    self-consistent. With only one of them injected, real time is still measured, so a
    frozen fake clock cannot hang a wait and a fake ``sleep`` still waits in real time.
    A request starts only when all three hold:

    1. the wall clock reads at least ``min_interval_s`` past the recorded last request,
       and any back-off (``defer()``) is over;
    2. at least ``min_interval_s`` has passed on ``monotonic`` since this process's own
       last request under this limiter (every instance on the default clock shares it),
       so a wall clock that steps forward never shortens the real gap between this
       process's requests;
    3. at least as much real time has passed since this state was first read as it asked
       for then (the wall-clock spacing, capped at one interval, or the back-off left,
       whichever is longer). The first sleep already gives this whenever ``sleep`` sleeps
       at least as long as asked, as ``time.sleep`` does; the rule is a floor for a
       ``sleep`` that returns early, so that even then a wall clock that steps forward
       during the wait cannot shorten it.

    After every sleep both clocks and the state are read again and any remainder is waited
    out, so a wall clock that loses time during the sleep (a slew, or a small step back)
    cannot let a request start early on the wall clock. No single sleep is longer than
    an hour.

    The bound. A wall clock that steps back can leave the recorded times in the future.
    Rule 1 then gives way once ``min_interval_s`` more real time has passed than rule 3
    asked for, so one recorded state never makes a request wait more than
    ``max(min_interval_s, the back-off left at the first read, on the wall clock) +
    min_interval_s`` of real time: ``2 * min_interval_s`` without a back-off. A wall clock
    that steps back by S before the first read moves every recorded time S later, so a
    back-off that ended less than S ago comes back for up to S (the safe side: a request
    then waits longer, never starts early). Rule 1 holds whenever the wall clock steps
    back by at most one interval. Rule 2 never needs more than one interval after this
    process's own last request, so it is always met by then; rule 3 never gives way.
    Another process's request taking its turn starts the count again, as before.
    Termination needs only that ``sleep`` advances ``monotonic``; if it has not moved
    across 1,000 sleeps in a row (a broken fake), the wait raises ``RuntimeError``.

    Back-offs are recorded on the wall clock only. They hold in real time while a caller
    waits inside the limiter (rule 3), but a wall clock that steps forward while nobody
    waits shortens one in real time, for this process and every other, as the shared
    www.nps.gov host hold's wall-clock times do. Across processes only the wall clock
    counts too: rule 2 is per process.

    ``next_allowed_in()`` reads the shared state without the lock and never blocks. It
    gives the first sleep ``hold()`` or ``wait()`` would take now (every rule and the
    bound, this process's own last request included), which is the whole wait unless the
    state or the wall clock changes meanwhile. It cannot see a request in flight in
    another process, so treat it as a lower bound; it does see every recorded back-off,
    which is what a scheduled worker needs to decide whether to skip a cycle.

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
        monotonic: Callable[[], float] | None = None,
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
        if monotonic is None:
            # A fake machine (clock and sleep both injected) keeps its own time; with either
            # one real, elapsed time is the real steady clock's (see the class docstring).
            fake_machine = clock is not time.time and sleep is not time.sleep
            monotonic = clock if fake_machine else steady_clock
        self._monotonic = monotonic
        self._own_last: float | None = None
        self._own_key: str | None = None
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
        if self._monotonic is steady_clock:
            where = name if self._state_path is None else os.path.realpath(self._state_path)
            self._own_key = f"{mode}:{where}"

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

    # -- this process's own last request, on the monotonic clock --------------------------
    def _own_last_real(self) -> float | None:
        if self._own_key is None:
            return self._own_last
        with _OWN_LAST_GUARD:
            return _OWN_LAST.get(self._own_key)

    def _note_own(self, real: float) -> None:
        if self._own_key is None:
            self._own_last = real
            return
        with _OWN_LAST_GUARD:
            _OWN_LAST[self._own_key] = real

    def _remaining(
        self,
        now: float,
        real: float,
        last: float,
        not_before: float,
        first_read: float,
        asked: float,
    ) -> float:
        """Seconds before a request may start (<= 0: now), by the class docstring's rules.

        ``now`` and ``real`` are the wall and monotonic clocks' readings; ``first_read`` is
        the monotonic time this state was first read and ``asked`` the wait it gave then.
        """
        interval = self.min_interval_s
        asked_left = first_read + asked - real  # rule 3
        wall_left = max(last + interval - now, not_before - now)  # rule 1
        own = self._own_last_real()
        own_left = -math.inf if own is None else own + interval - real  # rule 2
        bound_left = asked_left + interval  # rule 1 gives way here; rule 2 is met by then
        return max(asked_left, min(max(wall_left, own_left), bound_left))

    @contextmanager
    def _turn(self) -> Iterator[tuple[float, float]]:
        """Wait until a request may start, then yield ``(waited, not_before)`` under the lock.

        Sleeps with the lock released, at most :data:`_SLEEP_CAP_S` at a time; after every
        sleep it reads the state and both clocks again and waits out any remainder. A
        changed state (another request took its turn, or a ``defer()``) starts the count
        again, as :meth:`next_allowed_in` would. ``waited`` is the real time that passed on
        ``monotonic`` from the call until now, sleeps' lateness and lock waits included.
        Raises ``RuntimeError`` if ``monotonic`` has not moved across :data:`_STALL_LIMIT`
        sleeps in a row.
        """
        entered = self._monotonic()
        seen: tuple[float, float] | None = None
        first_read = asked = 0.0
        slept_at: float | None = None
        stalled = 0
        while True:
            with self._exclusive():
                last, not_before = self._read_locked()
                now, real = self._clock(), self._monotonic()
                if slept_at is not None:
                    stalled = stalled + 1 if real <= slept_at else 0
                    if stalled >= _STALL_LIMIT:
                        raise RuntimeError(
                            f"{self!r}: its monotonic clock {self._monotonic!r} did not move "
                            f"across {stalled} sleeps in a row; a fake sleep must let time pass"
                        )
                if (last, not_before) != seen:
                    seen = (last, not_before)
                    first_read, asked = real, self._delay(now, last, not_before)
                delay = self._remaining(now, real, last, not_before, first_read, asked)
                if delay <= 0:
                    yield max(0.0, real - entered), not_before
                    return
            slept_at = real
            self._sleep(min(delay, _SLEEP_CAP_S))

    # -- public API ----------------------------------------------------------------
    def _holding(self) -> bool:
        return getattr(self._local, "holding", False)

    def wait(self) -> float:
        """Block until a request may start, record it as started now, return seconds waited.

        Sleeps with the lock released, then reads the clocks and the state again and waits
        out any remainder, within the class docstring's bound when the wall clock has
        stepped back. The seconds waited are real time on ``monotonic``, not the sum of
        the sleeps asked for.
        """
        if self._holding():
            raise RuntimeError("wait() inside hold() of the same limiter would deadlock")
        with self._turn() as (waited, not_before):
            start, real = self._clock(), self._monotonic()
            self._note_own(real)
            self._write(start, not_before)
        return waited

    @contextmanager
    def hold(self) -> Iterator[float]:
        """Hold the slot for a whole request: one at a time, spaced end-to-start.

        Yields the seconds waited (real time on ``monotonic``, as :meth:`wait` returns).
        Waiting happens with the lock released; the lock is
        held only from the moment the request may start until it ends. The finish time
        (wall clock, and this process's monotonic reading) is recorded even if the block
        raises. ``defer()`` may be called inside the block; it takes effect on exit.
        """
        if self._holding():
            raise RuntimeError("hold() is not re-entrant")
        with self._turn() as (waited, not_before):
            self._local.holding = True
            self._local.pending_not_before = not_before
            try:
                yield waited
            finally:
                pending = self._local.pending_not_before
                self._local.holding = False
                end, real = self._clock(), self._monotonic()
                self._note_own(real)
                self._write(end, max(not_before, pending))

    def defer(self, seconds: float) -> None:
        """Forbid any request under this name for ``seconds`` from now (error back-off).

        Never shortens a back-off that is already recorded. A back-off of more than 30
        days, infinity included, is recorded as 30 days, so the state stays finite and
        readable (an infinite one used to be read back as a torn file: one hour). NaN is
        an unknown back-off and is recorded as ``corrupt_hold_off_s``, as an unreadable
        state file is, never as none. Zero or less records none.
        """
        seconds = float(seconds)
        if math.isnan(seconds):
            seconds = self.corrupt_hold_off_s
        until = self._clock() + min(max(0.0, seconds), _DEFER_CAP_S)
        if self._holding():
            self._local.pending_not_before = max(self._local.pending_not_before, until)
            return
        with self._exclusive():
            last, not_before = self._read_locked()
            self._write(last, max(not_before, until))

    def next_allowed_in(self) -> float:
        """Seconds until the next request may start (0 if now); does not reserve it.

        The first sleep :meth:`hold` or :meth:`wait` would take now, by every rule and the
        bound in the class docstring, so after a wall-clock step it counts this process's
        own last request (rule 2) and the step-back bound too. Reads the state without the
        lock, so it never blocks; a request in flight in another process is not counted
        (see the class docstring).
        """
        if self._holding():
            return 0.0
        last, not_before, _ = self._read()
        now, real = self._clock(), self._monotonic()
        asked = self._delay(now, last, not_before)
        return max(0.0, self._remaining(now, real, last, not_before, real, asked))
