"""Synthetic frame stores for leakage and baseline tests (no real frames, no network)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from lookout_eval.frames import FrameRecord
from lookout_eval.leakage import FrameStore, HeldOut, SealedScoring

VP = "rainier.paradise-mountain"
START = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)


def key_for(moment: datetime, vp: str = VP, tag: str = "abcdef012345") -> str:
    return f"frames/{vp}/{moment:%Y-%m-%d}/{moment:%Y%m%dT%H%M%SZ}_{tag}.jpg"


def hourly_records(start: datetime = START, hours: int = 24 * 7, vp: str = VP) -> list[FrameRecord]:
    """One frame per hour; available two minutes after its image time (a live fetch)."""
    return records_every(start, timedelta(hours=1), hours, vp=vp)


def records_every(
    start: datetime,
    step: timedelta,
    count: int,
    *,
    vp: str = VP,
    delay: timedelta = timedelta(minutes=2),
) -> list[FrameRecord]:
    """``count`` frames every ``step``, each available ``delay`` after its image time."""
    out = []
    for i in range(count):
        t = start + i * step
        out.append(FrameRecord(vp, t, key_for(t, vp), "live", None, t + delay))
    return out


def label_by(
    records: list[FrameRecord], fn: Callable[[FrameRecord], dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    return {r.key: fn(r) for r in records}


def make_store(
    records: list[FrameRecord],
    labels: dict[str, dict[str, Any]],
    *,
    held_out: HeldOut | None = None,
    record_accesses: bool = True,
    sealed: SealedScoring | None = None,
) -> FrameStore:
    return FrameStore(
        records,
        held_out=held_out,
        labels=labels,
        reader=lambda r: r.key.encode(),
        record_accesses=record_accesses,
        sealed=sealed,
    )
