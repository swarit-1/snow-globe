"""A fully read HTTP response with a size limit, shared by live capture and the archive.

Live capture also gives each request (PRD §15.3; CLAUDE.md "Polite collection";
``workers/capture/README.md``, "Why the rules hold at nps.gov"):

- a **send deadline** (:data:`LIVE_SEND_DEADLINE_S`): a request whose head would be written to
  the socket later than that after its reservation, on the wall clock or on a monotonic clock
  that keeps counting while the machine sleeps (:data:`steady_clock`), is abandoned unsent
  (:class:`SendDeadlinePassed`). That is what bounds, from the code, how late nps.gov can
  receive a live request whose answer this process never recorded (a crash between the send
  and the state save): :data:`UNANSWERED_REQUEST_BOUND_S`;
- a **total deadline** (:data:`LIVE_POLL_DEADLINE_S`, :data:`LIVE_IMAGE_DEADLINE_S`): a body
  still arriving that long after the request started is abandoned
  (:class:`RequestDeadlinePassed`), so a trickling body cannot hold a camera's lock for long;
- a record of **how far it got** (:class:`Progress`), so that a request which failed after it
  may have left this machine, and before any response head came back, is counted as answered
  :data:`FAILED_AFTER_SEND_BOUND_S` after the failure (:func:`may_arrive_after_failure`).
"""

from __future__ import annotations

import functools
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from lookout_common.http import DEFAULT_TIMEOUT

__all__ = [
    "FAILED_AFTER_SEND_BOUND_S",
    "LIVE_IMAGE_DEADLINE_S",
    "LIVE_POLL_DEADLINE_S",
    "LIVE_SEND_DEADLINE_S",
    "UNANSWERED_REQUEST_BOUND_S",
    "WRITE_TIMEOUT_S",
    "Fetched",
    "Progress",
    "RequestDeadlinePassed",
    "ResponseTooLarge",
    "SendDeadlinePassed",
    "fetch",
    "may_arrive_after_failure",
    "steady_clock",
]

#: A live request whose head is not on its way this long after its reservation (the moment
#: its poll or image GET was recorded as about to be sent) is abandoned unsent. It covers the
#: reservation's own state write, the 1 s request spacing, the connection pool (10 s), the
#: TCP connect (10 s) and the TLS handshake (10 s per operation) of the polite client, and
#: the DNS lookup, which no httpx timeout bounds. The deadline is checked right before the
#: request head is written (an httpcore trace hook) and before the request starts at all.
LIVE_SEND_DEADLINE_S = 30.0
#: The polite client's write timeout (``lookout_common.http.DEFAULT_TIMEOUT``): once allowed,
#: the head is handed to the kernel within this, or the request fails.
WRITE_TIMEOUT_S = float(DEFAULT_TIMEOUT.write or 30.0)
#: The latest moment, after its reservation, at which a live request's head can be handed to
#: the kernel: the send deadline plus the write timeout (60 s). An answer this process never
#: recorded (a crash between the send and the state save, or state from code that recorded no
#: answer) is taken to have come this long after the reservation (or the send stamp). httpx's
#: own timeouts cannot give such a bound: each applies to one operation (each read, each
#: write), so a response trickling in never times out, and DNS is not covered at all.
UNANSWERED_REQUEST_BOUND_S = LIVE_SEND_DEADLINE_S + WRITE_TIMEOUT_S
#: A request that failed after its head may have been handed to the kernel, with no response
#: head back (a read timeout, a reset, an interrupt), counts as answered this long after the
#: failure was seen. Until a response head arrives nothing shows that nps.gov has the request:
#: a queue in front of the application (an accept backlog, a busy worker, a front end
#: forwarding to the origin) can hand it on after the client gave up, and TCP keeps
#: retransmitting a head it could not deliver. One extra poll interval per such failure:
#: capture_log had 3 of them in 2,065 live requests (12:24 UTC 19 Nov to 15:17 UTC 20 Nov 2026:
#: two resets while reading, one image read timeout; a TLS handshake timeout sent nothing).
FAILED_AFTER_SEND_BOUND_S = 300.0
#: The longest a live poll (the JSON feed's GET or the park camera's HEAD) may take, from its
#: start to its last byte; past it the body is abandoned (:class:`RequestDeadlinePassed`).
#: The 1,525 polls in capture_log that did not fail took at most 3.8 s (12:24 UTC 19 Nov to 15:17
#: UTC 20 Nov 2026), and the S3 camera lock lives 300 s: a camera's visit (a slot wait of up to
#: 60 s, this, the image GET's deadline and one last 30 s read of each) stays well inside it.
LIVE_POLL_DEADLINE_S = 30.0
#: The same for an image GET (the 536 that did not fail took at most 2.1 s).
LIVE_IMAGE_DEADLINE_S = 60.0
#: httpcore trace events fired right before a request head is written (HTTP/1.1 and HTTP/2).
_SEND_EVENTS = frozenset(
    {"http11.send_request_headers.started", "http2.send_request_headers.started"}
)
#: Failures that prove nothing of the request was written: no connection (DNS, TCP, TLS:
#: httpcore maps a failed handshake to these), no pooled connection, a request httpx refused
#: to send, or a send deadline that had passed.
_NOT_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
    httpx.InvalidURL,
)


def _steady_clock() -> Callable[[], float]:
    """A monotonic clock (seconds) that keeps counting while the machine sleeps.

    ``time.monotonic()`` is ``mach_absolute_time()`` on macOS and ``CLOCK_MONOTONIC`` on Linux,
    and neither advances while the system sleeps: a send deadline read on it would let a
    request leave long after its reservation in wall-clock terms, and the in-process guard
    against wall-clock steps would read every answer recorded before a sleep as that much
    later. macOS's ``CLOCK_MONOTONIC_RAW`` (``mach_continuous_time``) and Linux's
    ``CLOCK_BOOTTIME`` count the sleep; neither is stepped. Falls back to ``time.monotonic``.
    """
    name = "CLOCK_MONOTONIC_RAW" if sys.platform == "darwin" else "CLOCK_BOOTTIME"
    clock_id = getattr(time, name, None)
    if clock_id is None:
        return time.monotonic
    try:
        time.clock_gettime(clock_id)
    except OSError:
        return time.monotonic
    return functools.partial(time.clock_gettime, clock_id)


#: The monotonic clock live capture uses (:func:`_steady_clock`).
steady_clock: Callable[[], float] = _steady_clock()


class ResponseTooLarge(httpx.HTTPError):
    """The body grew past the caller's byte limit; the download was abandoned."""

    def __init__(self, url: str, limit: int) -> None:
        super().__init__(f"{url} is larger than {limit} bytes")


class SendDeadlinePassed(httpx.TimeoutException):
    """The request was abandoned before its head was written: its send deadline had passed.

    Nothing of it reached nps.gov. It is a timeout like any other to the caller (logged as a
    failed request, then backed off).
    """


class RequestDeadlinePassed(httpx.TimeoutException):
    """The body was still arriving at the request's total deadline; the download was abandoned.

    The response head had arrived, so nps.gov had the request before this was raised.
    """


@dataclass
class Fetched:
    """Status, headers and the whole (transfer-decoded) body of one response."""

    status_code: int
    headers: httpx.Headers
    content: bytes
    url: str

    @property
    def text(self) -> str:
        """The body as UTF-8 text, replacing undecodable bytes."""
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The body parsed as JSON."""
        return json.loads(self.content)


@dataclass
class Progress:
    """How far one request got, filled in by :func:`fetch`.

    ``response_head`` is set once a response head has arrived (also when the body then
    fails): nps.gov had the request by then.
    """

    response_head: bool = False


def may_arrive_after_failure(exc: BaseException, progress: Progress) -> bool:
    """True if nps.gov may still receive a request that failed with ``exc`` after it failed.

    False once a response head arrived (nps.gov answered, so it had the request), and for the
    failures that prove nothing was written (:data:`_NOT_SENT`, :class:`SendDeadlinePassed`).
    Anything else (a read timeout or a reset before any answer, an interrupt, an error not
    known to come before the write) may have left this machine and so counts as unanswered:
    the caller bounds its answer by :data:`FAILED_AFTER_SEND_BOUND_S` after the failure.
    """
    if progress.response_head:
        return False
    return not isinstance(exc, (SendDeadlinePassed, *_NOT_SENT))


def _seconds_late(
    send_by: float | None,
    monotonic: Callable[[], float],
    send_by_wall: datetime | None,
    wall: Callable[[], datetime] | None,
) -> float | None:
    """How far past its send deadline a request is, on either clock; None if within it."""
    late = float("-inf")
    if send_by is not None:
        late = monotonic() - send_by
    if send_by_wall is not None and wall is not None:
        late = max(late, (wall() - send_by_wall).total_seconds())
    return late if late > 0 else None


def send_deadline_hook(
    send_by: float | None,
    monotonic: Callable[[], float] = steady_clock,
    *,
    send_by_wall: datetime | None = None,
    wall: Callable[[], datetime] | None = None,
) -> Callable[[str, dict[str, Any]], None]:
    """An httpcore ``trace`` callback that refuses to write a request head past its deadline.

    ``send_by`` is a reading of ``monotonic``, ``send_by_wall`` one of ``wall``; the head is
    refused if either clock is past its value. The callback raises :class:`SendDeadlinePassed`
    at the event fired right before the head is written, so a request that waited too long for
    DNS, a pooled connection, the TCP connect or the TLS handshake (or across a system sleep)
    is never sent.
    """

    def trace(event: str, info: dict[str, Any]) -> None:
        if event in _SEND_EVENTS:
            late = _seconds_late(send_by, monotonic, send_by_wall, wall)
            if late is not None:
                raise SendDeadlinePassed(
                    f"send deadline passed ({late:.3f} s late): request not sent"
                )

    return trace


def fetch(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    max_bytes: int = 40_000_000,
    send_by: float | None = None,
    monotonic: Callable[[], float] = steady_clock,
    send_by_wall: datetime | None = None,
    wall: Callable[[], datetime] | None = None,
    total_s: float | None = None,
    progress: Progress | None = None,
) -> Fetched:
    """Send one request and read the body, giving up past ``max_bytes`` (ResponseTooLarge).

    - With ``send_by`` (a ``monotonic`` reading) and/or ``send_by_wall`` (a ``wall`` reading)
      the request is abandoned unsent, raising :class:`SendDeadlinePassed`, if either moment
      has passed before the request starts or right before its head is written
      (:func:`send_deadline_hook`).
    - With ``total_s`` a body still arriving ``total_s`` after the request started (on
      ``monotonic``) is abandoned, raising :class:`RequestDeadlinePassed`; checked when the
      response head arrives and after each chunk, so the last read can add one read timeout.
    - ``progress`` is told when the response head arrived (:class:`Progress`).
    """
    started = monotonic()
    extensions: dict[str, Any] | None = None
    if send_by is not None or send_by_wall is not None:
        if _seconds_late(send_by, monotonic, send_by_wall, wall) is not None:
            raise SendDeadlinePassed("send deadline passed before the request: not sent")
        hook = send_deadline_hook(send_by, monotonic, send_by_wall=send_by_wall, wall=wall)
        extensions = {"trace": hook}

    def check_total() -> None:
        if total_s is not None and monotonic() - started > total_s:
            raise RequestDeadlinePassed(f"{url} took longer than {total_s:g} s: abandoned")

    with client.stream(
        method, url, headers=headers, json=json_body, extensions=extensions
    ) as response:
        if progress is not None:
            progress.response_head = True
        check_total()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLarge(url, max_bytes)
            chunks.append(chunk)
            check_total()
        return Fetched(response.status_code, response.headers, b"".join(chunks), url)
