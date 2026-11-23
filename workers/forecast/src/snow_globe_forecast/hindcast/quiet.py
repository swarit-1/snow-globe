"""The quiet window: minutes past each hour in which the hindcast sends no request (PRD §15.3).

The live forecast stand-in ticks every 5 minutes and downloads a new run's f00-f02 as soon as
they are listed, so most of its fetches start a few minutes before the hour, but not all. Of
its 75 live fetches from 06:00 UTC on 20 Nov to 09:40 UTC on 23 Nov 2026 (``run`` events of
``forecast_log/*.jsonl``, read 23 Nov), 61 started between :50 and :05 (51 of them between :55
and :00); 14 started later, 13 of those between :05 and :15 (runs published late, the latest
22:14:50) and one at 09:36:48 on 23 Nov (run 2026112308, after the laptop slept, D-104). Each
took 1.5 to 6.2 s. The hindcast must not compete with it for the laptop's link: before every
network attempt, retries included, the hindcast calls :meth:`QuietWindow.wait`, which sleeps
through ``:50`` to ``:05`` by default (the task's window; ``hindcast ... --quiet-end-minute 15``
would also cover 13 of those 14). A request already in flight at ``:50`` finishes (one GRIB
range is a few MB).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from snow_globe_common.timeutil import utcnow

__all__ = ["DEFAULT_END_MINUTE", "DEFAULT_START_MINUTE", "QuietWindow"]

DEFAULT_START_MINUTE = 50
DEFAULT_END_MINUTE = 5


@dataclass
class QuietWindow:
    """Sleep through ``start_minute``-``end_minute`` past each hour (wrapping round the hour).

    ``on_pause(now, seconds)`` is called before each sleep (the fetch logs it). ``paused_s``
    and ``pauses`` count what this window has slept.
    """

    start_minute: int = DEFAULT_START_MINUTE
    end_minute: int = DEFAULT_END_MINUTE
    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], None] = time.sleep
    on_pause: Callable[[datetime, float], None] | None = None
    paused_s: float = field(default=0.0, init=False)
    pauses: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not 0 <= self.end_minute < self.start_minute <= 59:
            raise ValueError(
                "the quiet window must wrap round the hour: 0 <= end_minute < start_minute <= 59"
            )

    def seconds_until_open(self, now: datetime) -> float:
        """Seconds from ``now`` until requests may be sent again (0 outside the window)."""
        minute = now.minute + now.second / 60.0 + now.microsecond / 60e6
        if minute >= self.start_minute:
            return (60.0 - minute + self.end_minute) * 60.0
        if minute < self.end_minute:
            return (self.end_minute - minute) * 60.0
        return 0.0

    def wait(self) -> float:
        """Sleep until the window is open; return the seconds slept.

        The clock is read again after each sleep (a laptop that slept may wake anywhere),
        at most three times.
        """
        slept = 0.0
        for _ in range(3):
            now = self.clock()
            seconds = self.seconds_until_open(now)
            if seconds <= 0.0:
                break
            if self.on_pause is not None:
                self.on_pause(now, seconds)
            self.sleep(seconds)
            slept += seconds
            self.pauses += 1
        self.paused_s += slept
        return slept
