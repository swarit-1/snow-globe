"""P1's hindcast fetch plan: the HRRR runs and hours a 24 h lead needs (PRD §13.4, §8.3; D-008).

For every daytime frame captured (Sun >= 3 deg, :mod:`.frames`), the prediction a 24 h lead
allows is the one issued 24 h before the frame's valid time from the newest HRRR run whose
forecast for that valid time was **available** by then. Lead = valid - availability (PRD §8.3,
D-008), so the actual lead is 24 h or more. Availability is the hours' S3 ``LastModified``:
the later of the GRIB2 and ``.idx`` stamps, as a live file records it
(:func:`hindcast_availability`), listed now from S3 (the runs are days old, so their stamps
are the publication times; a stamp more than 24 h after its run start is a rewritten object
and is never trusted, :func:`..discovery.publication_floor`).

The choice is :func:`..availability.select_run_for_issue`: a valid time between two hours
needs both (values are interpolated linearly), the later availability counts, and a missing
or late run falls back to an older one. Only the 00/06/12/18Z runs reach f48, so every chosen
run is synoptic and its hours are f24 or later. The plan lists, per run, the hours needed and
the bytes the extractor will download for them (:func:`..extract.plan_run`: the ``.idx`` byte
ranges of the live worker's fields, ``HGT:surface`` once per run), before any GRIB byte is
fetched. Its cap check counts what the hindcast has already downloaded and what the runs not
written yet still need (``totals.within_cap``), the rule :func:`.fetch.fetch_plan` applies.

**This rule is the task's, not D-008's.** D-008, D-027 and PRD §8.3 model an archived run's
availability as run start + the p95 lag (D-054: never before the hour's S3 stamps), while the
hindcast takes the stamps themselves, so the two can choose different runs for a frame. The
lead decides which rule P1 uses; the README's "Hindcast mode" gives the measured difference.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from snow_globe_common.timeutil import isoformat_z, utcnow

from .. import __version__
from ..availability import HourAvailability, Lookup, RunChoice, select_run_for_issue
from ..bucket import HrrrBucket
from ..discovery import RunListing, list_run, publication_floor
from ..extract import plan_run
from ..fields import FieldSpec, resolve_fields
from ..hrrr import as_run_start, format_run_id, max_fh, parse_run_id
from .frames import CapturedFrame, FrameSet

__all__ = [
    "DEFAULT_CAP_MB",
    "LIVE_FIELD_NAMES",
    "P1_LEAD_H",
    "P1_MIN_SUN_ALT_DEG",
    "PLAN_KEY",
    "PLAN_KIND",
    "HindcastStampError",
    "S3Availability",
    "build_plan",
    "hindcast_availability",
    "plan_key",
    "plan_run_starts",
    "plan_runs",
]

#: PRD §13.4: P1 is scored at a 24 h lead.
P1_LEAD_H = 24.0
#: PRD §13.4: daytime is Sun >= 3 deg (eval's ``P1_MIN_SUN_ALT_DEG``; geometric altitude).
P1_MIN_SUN_ALT_DEG = 3.0
#: The live worker's fields: the stand-in's profile since 02:39 UTC on 20 Nov 2026 (D-050), as
#: ``cache/runtime/bin/forecast_loop.sh`` and the Makefile's ``FORECAST_FIELDS_DEFAULT`` set it.
LIVE_FIELD_NAMES: tuple[str, ...] = (
    "VIS:surface",
    "HGT:cloud ceiling",
    "HGT:cloud base",
    "PRES:cloud base",
    "LCDC:low cloud layer",
    "MCDC:middle cloud layer",
    "HCDC:high cloud layer",
    "HGT:surface",
)
#: The task's cap on the hindcast's whole download.
DEFAULT_CAP_MB = 2000.0
PLAN_KIND = "snow-globe.hindcast-plan"


def plan_key(lead_h: float = P1_LEAD_H) -> str:
    """The current plan at one lead, in the hindcast root: ``plan/p1-lead<lead>h.json``.

    Each lead has its own key, so a plan at another lead never replaces P1's 24 h plan; every
    plan is also kept under ``plan/history/``.
    """
    return f"plan/p1-lead{lead_h:g}h.json"


#: The current P1 plan (24 h lead).
PLAN_KEY = plan_key(P1_LEAD_H)
#: The source of every hindcast hour: its availability is its own S3 stamps, by the hindcast's
#: rule (see :data:`..availability.SOURCE_ARCHIVED_STAMP`).
SOURCE_S3 = "archived-s3-lastmodified"

#: ``covered(run start, hours)``: True when the hindcast already holds those hours of the run.
CoveredFn = Callable[[datetime, Sequence[int]], bool]


class HindcastStampError(ValueError):
    """Neither S3 stamp of an hour can be trusted, so its availability is unknown."""


def hindcast_availability(
    run_start: datetime, grib_utc: datetime | None, idx_utc: datetime | None
) -> tuple[datetime, datetime, tuple[str, ...]]:
    """``(availability, floor, ignored)`` of one hour of an archived run, for the hindcast.

    The availability is the later of the hour's GRIB2 and ``.idx`` ``LastModified`` stamps,
    exactly as a live file records it (:attr:`..discovery.HourFiles.availability_utc`), and
    never before :func:`..discovery.publication_floor`'s floor, so a hindcast file is never
    stamped as available earlier than S3 says. ``ignored`` names a stamp more than 24 h after
    the run start (a rewritten object; the availability is then that late stamp, which is
    conservative). Raises :class:`HindcastStampError` when neither stamp is trusted.
    """
    start = as_run_start(run_start)
    floor, ignored = publication_floor(start, grib_utc, idx_utc)
    if floor is None:
        raise HindcastStampError(
            f"run {format_run_id(start)}: no S3 stamp within 24 h of the run start "
            f"(GRIB2 {grib_utc}, .idx {idx_utc}); its availability is unknown"
        )
    stamps = [stamp for stamp in (grib_utc, idx_utc) if stamp is not None]
    return max([floor, *stamps]), floor, ignored


class S3Availability:
    """An ``availability`` callback for :func:`..availability.select_run_for_issue`: each run
    asked about is listed once (one request; kept by :class:`.cachebucket.CachingBucket`), and
    a listed hour is available at :func:`hindcast_availability`. An hour not on S3, or with no
    trusted stamp, is :attr:`..availability.Lookup.UNAVAILABLE`."""

    def __init__(self, bucket: HrrrBucket) -> None:
        self.bucket = bucket
        self.listings: dict[datetime, RunListing] = {}
        self.untrusted: set[tuple[str, int]] = set()

    def listing(self, run: datetime) -> RunListing:
        start = as_run_start(run)
        if start not in self.listings:
            self.listings[start] = list_run(self.bucket, start)
        return self.listings[start]

    def __call__(self, run: datetime, fh: int) -> HourAvailability:
        listing = self.listing(run)
        if fh not in listing.ready_hours:
            return Lookup.UNAVAILABLE
        files = listing.hour(fh)
        assert files.grib is not None and files.idx is not None
        try:
            available, _, _ = hindcast_availability(
                listing.run_start, files.grib.last_modified, files.idx.last_modified
            )
        except HindcastStampError:
            self.untrusted.add((listing.run_id, fh))
            return Lookup.UNAVAILABLE
        return (available, SOURCE_S3)


@dataclass(frozen=True)
class FramePair:
    """One daytime frame and the run a ``lead_h`` lead lets it use (None: no run did)."""

    frame: CapturedFrame
    issue_utc: datetime
    choice: RunChoice | None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "frame": self.frame.key,
            "valid_utc": isoformat_z(self.frame.image_time_utc, "milliseconds"),
            "issue_utc": isoformat_z(self.issue_utc, "milliseconds"),
            "sun_alt_deg": self.frame.sun_alt_deg,
        }
        if self.choice is None:
            out["run"] = None
            return out
        c = self.choice
        out.update(
            {
                "run": c.run_id,
                "fh_lo": c.fh_lo,
                "fh_hi": c.fh_hi,
                "weight_hi": c.weight_hi,
                "availability_utc": isoformat_z(c.availability_utc),
                "lead_h": c.lead_h,
                "lead_from_run_start_h": c.lead_from_run_start_h,
                "skipped": [list(item) for item in c.skipped],
            }
        )
        return out


@dataclass
class RunNeed:
    """The hours one run must supply, and the frames that need them."""

    run_start: datetime
    hours: set[int] = field(default_factory=set)
    frames: int = 0


def plan_runs(
    frames: Sequence[CapturedFrame],
    availability: S3Availability,
    *,
    lead_h: float = P1_LEAD_H,
    max_lookback_h: int = 60,
) -> tuple[list[FramePair], dict[datetime, RunNeed]]:
    """Pair each frame with the run its ``lead_h`` lead allows, and collect each run's hours."""
    pairs: list[FramePair] = []
    needs: dict[datetime, RunNeed] = {}
    for frame in frames:
        valid = frame.image_time_utc
        issue = valid - timedelta(hours=lead_h)
        choice = select_run_for_issue(
            issue, valid, availability=availability, max_lookback_h=max_lookback_h
        )
        pairs.append(FramePair(frame, issue, choice))
        if choice is None:
            continue
        need = needs.setdefault(choice.run_start_utc, RunNeed(choice.run_start_utc))
        need.hours.update({choice.fh_lo, choice.fh_hi})
        need.frames += 1
    return pairs, dict(sorted(needs.items()))


def _lead_stats(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": round(min(values), 3),
        "median": round(median(values), 3),
        "max": round(max(values), 3),
    }


def build_plan(
    frame_set: FrameSet,
    bucket: HrrrBucket,
    *,
    lead_h: float = P1_LEAD_H,
    min_sun_alt_deg: float = P1_MIN_SUN_ALT_DEG,
    fields: Sequence[FieldSpec] | None = None,
    cap_mb: float = DEFAULT_CAP_MB,
    now: datetime | None = None,
    spent_bytes: int = 0,
    covered: CoveredFn | None = None,
) -> dict[str, Any]:
    """The P1 fetch plan as a JSON-ready document (see the module docstring).

    Network: one listing per candidate synoptic run and one ``.idx`` per needed hour (no
    GRIB), all through ``bucket`` (a :class:`.cachebucket.CachingBucket` keeps them).

    The cap check (``totals.within_cap``) is what :func:`.fetch.fetch_plan` applies:
    ``spent_bytes`` (the hindcast's download before this plan) plus this plan's own transfer,
    plus the planned bytes of every run ``covered`` says is not written yet (every run
    without ``covered``), must fit in ``cap_mb``. ``plan_alone_within_cap`` checks the
    planned bytes alone.
    """
    created = now or utcnow()
    specs = list(fields) if fields is not None else resolve_fields(LIVE_FIELD_NAMES)
    names = [spec.name for spec in specs]
    ledger_before = bucket.ledger.snapshot()
    daytime = frame_set.daytime(min_sun_alt_deg)
    lookup = S3Availability(bucket)
    pairs, needs = plan_runs(daytime, lookup, lead_h=lead_h)
    runs: list[dict[str, Any]] = []
    for run_start, need in needs.items():
        if max_fh(run_start) != 48:  # pragma: no cover - select_run_for_issue cannot pick one
            raise AssertionError(f"run {format_run_id(run_start)} stops at f18")
        sizes = plan_run(bucket, run_start, fields=specs, hours=sorted(need.hours), workers=1)
        missing = sorted(set(need.hours) - set(sizes["hours"]))
        if missing:
            raise RuntimeError(
                f"run {format_run_id(run_start)} lacks f{missing} on S3, which its listing "
                "showed a moment ago"
            )
        runs.append(
            {
                "run": format_run_id(run_start),
                "run_start_utc": isoformat_z(run_start),
                "hours": sorted(need.hours),
                "frames": need.frames,
                "planned_bytes": int(sizes["total_bytes"]),
                "bytes_by_fh": {str(fh): b for fh, b in sizes["bytes_by_fh"].items()},
                "bytes_by_field_by_fh": {
                    name: {str(fh): b for fh, b in per.items()}
                    for name, per in sizes["bytes_by_field_by_fh"].items()
                },
                "grib_file_bytes_by_fh": {
                    str(fh): b for fh, b in sizes["grib_file_bytes_by_fh"].items()
                },
                "fraction_of_files": sizes["fraction_of_files"],
                "fields_missing_from_idx": {
                    str(fh): absent for fh, absent in sizes["fields_missing_from_idx"].items()
                },
            }
        )
    planned = sum(run["planned_bytes"] for run in runs)
    cap_bytes = int(cap_mb * 1e6)
    paired = [pair for pair in pairs if pair.choice is not None]
    hours_total = sum(len(run["hours"]) for run in runs)
    transfer = bucket.ledger.since(ledger_before)
    pending = [
        run
        for run in runs
        if covered is None or not covered(parse_run_id(run["run"]), run["hours"])
    ]
    pending_bytes = sum(run["planned_bytes"] for run in pending)
    spent = int(spent_bytes) + int(transfer["total_bytes"])
    return {
        "kind": PLAN_KIND,
        "plan_version": 1,
        "created_utc": isoformat_z(created, "seconds"),
        "software": {"snow_globe_forecast": __version__},
        "viewpoint_id": frame_set.viewpoint_id,
        "endpoint": (
            "P1 (PRD §13.4): every captured daytime frame, paired with the newest HRRR run "
            f"whose forecast for its valid time was available at least {lead_h:g} h before it"
        ),
        "lead_h": lead_h,
        "lead_definition": "valid - availability (PRD §8.3, D-008)",
        "availability_rule": (
            "S3 LastModified of the hour, the later of the GRIB2 and .idx stamps (as a live file "
            "records it), listed from S3 at plan time; never before the stamps (a stamp more "
            "than 24 h after the run start is not trusted)"
        ),
        "selection": (
            "snow_globe_forecast.availability.select_run_for_issue (both bracketing hours)"
        ),
        "fields": names,
        "frames": frame_set.summary(min_sun_alt_deg),
        "pairs": [pair.to_dict() for pair in pairs],
        "unpaired": len(pairs) - len(paired),
        "untrusted_hours": sorted([run, fh] for run, fh in lookup.untrusted),
        "runs_listed": [format_run_id(run) for run in sorted(lookup.listings)],
        "runs": runs,
        "lead_h_achieved": _lead_stats([p.choice.lead_h for p in paired if p.choice]),
        "lead_from_run_start_h": _lead_stats(
            [p.choice.lead_from_run_start_h for p in paired if p.choice]
        ),
        "frames_by_run": dict(Counter(p.choice.run_id for p in paired if p.choice)),
        "totals": {
            "runs": len(runs),
            "forecast_hours": hours_total,
            "idx_files": hours_total,
            "planned_bytes": planned,
            "planned_mb": round(planned / 1e6, 3),
            "cap_bytes": cap_bytes,
            "runs_not_written": [run["run"] for run in pending],
            "pending_planned_bytes": pending_bytes,
            "hindcast_bytes_spent": spent,
            "within_cap": spent + pending_bytes <= cap_bytes,
            "plan_alone_within_cap": planned <= cap_bytes,
        },
        "transfer": transfer,
        "cache_hits": dict(getattr(bucket, "cache_hits", {})),
    }


def plan_run_starts(plan: Mapping[str, Any]) -> list[tuple[datetime, list[int], int]]:
    """``(run start, hours, planned bytes)`` of every run in a plan document, oldest first."""
    out = [
        (parse_run_id(str(run["run"])), [int(h) for h in run["hours"]], int(run["planned_bytes"]))
        for run in plan.get("runs", [])
    ]
    return sorted(out)
