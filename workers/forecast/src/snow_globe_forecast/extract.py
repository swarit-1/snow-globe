"""Extract one HRRR run for every viewpoint (PRD §8.3, §11.2; CLAUDE.md "NOAA").

For a run, :class:`Extractor`:

1. lists the run (one ListObjectsV2 request, unless the caller passes the listing it has
   just made) and checks the hours it needs exist: ``0..max_fh``, or exactly
   :attr:`ExtractOptions.hours`;
2. maps every viewpoint's sample points to grid cells (:mod:`.points`);
3. stops before any other request if no viewpoint's file would be written
   (:func:`write_decisions`: every file already holds these fields and hours, or holds
   fields or hours this request lacks and is never narrowed);
4. fetches each hour's ``.idx`` and plans the byte ranges of the requested fields
   (refusing to start if the plan exceeds the byte cap). ``HGT:surface``, the model's
   static terrain, is planned for the first hour only, whether or not it was requested;
   it fills ``model_terrain_m`` and, if requested, every hour of its field;
5. downloads each hour's ranges into ``$SNOW_GLOBE_DATA_DIR/cache/hrrr/<run>/``, decodes them
   once for all viewpoints, and deletes the subset at once;
6. writes one JSON file per viewpoint, a state marker, ``lag`` events (live runs only) and
   a ``run`` event to ``forecast_log/<day>.jsonl``.

Nothing is written for a run unless every requested hour decoded; the next scheduled
attempt starts again from the listing. A re-extraction (a wider field set or more hours)
rewrites the files and logs the run's ``run`` and ``lag`` events again; readers keep one
lag per (run, hour) (:mod:`.laglog`).
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from snow_globe_common.manifest import Viewpoint
from snow_globe_common.paths import cache_dir, forecast_key, forecast_log_key, state_key
from snow_globe_common.storage import Storage
from snow_globe_common.timeutil import isoformat_z, utcnow

from . import __version__
from .availability import ArchivedStamps, LagModel, archived_availability
from .bucket import HrrrBucket
from .decode import DecodedMessage, decode_subset, ecc_version
from .discovery import HourFiles, RunListing, list_run
from .fields import CATALOGUE, TERRAIN_FIELD, FieldSpec, field_by_name
from .grid import HRRR_CONUS, LambertConformalGrid
from .hrrr import (
    MODEL,
    PRODUCT,
    SOURCE,
    STORAGE_SOURCE,
    as_run_start,
    format_run_id,
    idx_key,
    max_fh,
    object_key,
    object_url,
    run_hours,
    valid_time,
)
from .idx import (
    IdxEntry,
    IdxError,
    MessageRange,
    RangeGroup,
    group_contiguous,
    message_ranges,
    parse_idx,
    select_entry,
)
from .laglog import lag_events
from .output import KIND, SCHEMA_VERSION, encode_document, validate_document
from .points import GriddedPoint, flat_indices, sample_points

__all__ = [
    "SKIP_ALREADY_WRITTEN",
    "SKIP_WOULD_NARROW",
    "BandwidthCapError",
    "ExtractOptions",
    "Extractor",
    "RunIncompleteError",
    "RunNotFoundError",
    "RunResult",
    "marker_key",
    "plan_run",
    "requested_hours",
    "run_covered",
    "write_decisions",
]

#: Why :func:`write_decisions` leaves a viewpoint's existing file alone.
SKIP_ALREADY_WRITTEN = "already written with these fields and hours"
SKIP_WOULD_NARROW = "existing file has fields or hours this run lacks"

LOCK_NAME = "forecast-hrrr"
MODEL_VERSION_NOTE = (
    "Not encoded in GRIB2: generatingProcessIdentifier is 83 (NCEP ON388 Table A: HRRR) for "
    "every version. PRD 8.1 dates HRRRv4 from Dec 2020; tell versions apart by run date."
)
#: Evidence that HGT:surface may be fetched once per run and repeated for every hour.
TERRAIN_STATIC_EVIDENCE = (
    "HGT:surface is the model's static terrain: on run 2026111906 it was bit-for-bit "
    "identical at f00, f01, f18 and f48 (all 1,905,141 cells, 2,153,695-byte messages)."
)


class RunNotFoundError(LookupError):
    """No object of the run is in the bucket."""


class RunIncompleteError(RuntimeError):
    """Some hours the extraction needs are not on S3 yet."""

    def __init__(self, run_id: str, missing: Sequence[int]) -> None:
        super().__init__(f"run {run_id} is missing f{', f'.join(f'{h:02d}' for h in missing)}")
        self.run_id = run_id
        self.missing = list(missing)


class BandwidthCapError(RuntimeError):
    """The planned download is larger than the configured cap; nothing was fetched."""

    def __init__(self, message: str, *, planned_bytes: int, cap_bytes: int) -> None:
        super().__init__(message)
        self.planned_bytes = planned_bytes
        self.cap_bytes = cap_bytes


@dataclass(frozen=True)
class ExtractOptions:
    """What to extract and how hard to try.

    The hours are ``0..max_fh`` (every hour the run produces when ``max_fh`` is None), or
    exactly ``hours`` when given: for example the two hours that bracket one valid time of
    a backtest pair (``snow-globe-forecast run --hours``). ``hours`` is stored sorted and
    without repeats, and cannot be combined with ``max_fh``.
    """

    fields: tuple[FieldSpec, ...] = CATALOGUE
    max_fh: int | None = None
    viewpoint_ids: tuple[str, ...] | None = None
    live_window_h: float = 24.0
    allow_incomplete: bool = False
    max_bytes: int | None = None
    workers: int = 4
    force: bool = False
    hours: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if self.hours is None:
            return
        if self.max_fh is not None:
            raise ValueError("give either hours or max_fh, not both")
        hours = tuple(self.hours)
        if not hours:
            raise ValueError("hours is empty")
        if not all(isinstance(fh, int) and not isinstance(fh, bool) and fh >= 0 for fh in hours):
            raise ValueError(f"hours must be integers >= 0, got {list(hours)!r}")
        object.__setattr__(self, "hours", tuple(sorted(set(hours))))


@dataclass
class _HourPlan:
    fh: int
    files: HourFiles
    ranges: list[MessageRange]
    groups: list[RangeGroup]
    missing_fields: list[str]
    terrain_only: bool

    @property
    def planned_bytes(self) -> int:
        return sum(group.length for group in self.groups)


@dataclass
class _HourDone:
    plan: _HourPlan
    fetched_utc: datetime
    download_s: float
    decode_s: float
    decoded: dict[str, DecodedMessage]


@dataclass
class RunResult:
    """What one extraction did, for logs, the CLI and the Lambda response."""

    run_id: str
    live: bool
    run_complete: bool
    hours: list[int]
    field_names: list[str]
    viewpoints: list[str]
    written: list[str]
    planned_bytes: int
    #: Requests, bytes and seconds of this extraction only: its listing included, unless the
    #: caller handed one over (``listing=``), in which case the caller's counts hold it.
    transfer: dict[str, Any]
    bytes_by_field: dict[str, dict[int, int]]
    seconds_total: float
    seconds_download: float
    seconds_decode: float
    s3_lags_s: dict[int, float]
    fetch_started_utc: datetime
    fetch_finished_utc: datetime
    skipped: dict[str, str] = field(default_factory=dict)
    documents: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def to_event(self) -> dict[str, Any]:
        """The ``run`` record for ``forecast_log``."""
        return {
            "event": "run",
            "source": SOURCE,
            "run": self.run_id,
            "live": self.live,
            "run_complete": self.run_complete,
            "hours": self.hours,
            "fields": self.field_names,
            "viewpoints": self.viewpoints,
            "written": self.written,
            "skipped": self.skipped,
            "s3_lags_s": {str(h): v for h, v in self.s3_lags_s.items()},
            "planned_bytes": self.planned_bytes,
            "transfer": self.transfer,
            "bytes_by_field": {
                k: {str(h): b for h, b in v.items()} for k, v in self.bytes_by_field.items()
            },
            "seconds_total": round(self.seconds_total, 3),
            "seconds_download": round(self.seconds_download, 3),
            "seconds_decode": round(self.seconds_decode, 3),
            "fetch_started_utc": isoformat_z(self.fetch_started_utc, "milliseconds"),
            "fetch_finished_utc": isoformat_z(self.fetch_finished_utc, "milliseconds"),
        }


def marker_key(run_start: datetime) -> str:
    """State marker of an extracted run: ``state/forecast/hrrr/<YYYYMMDDHH>z.json``."""
    return state_key(f"forecast/{STORAGE_SOURCE}/{format_run_id(run_start)}z.json")


def _explicit_hours(run_start: datetime, hours: Sequence[int]) -> list[int]:
    """``hours`` sorted, after checking that the run produces every one of them."""
    last = max_fh(run_start)
    beyond = sorted({fh for fh in hours if fh > last})
    if beyond:
        raise ValueError(
            f"run {format_run_id(run_start)} stops at f{last:02d}; it has no "
            + ", ".join(f"f{fh:02d}" for fh in beyond)
        )
    return sorted(set(hours))


def requested_hours(run_start: datetime, options: ExtractOptions) -> list[int]:
    """The forecast hours ``options`` ask of this run (PRD §8.3).

    Exactly :attr:`ExtractOptions.hours` when given (``ValueError`` if the run stops
    earlier), else ``0..max_fh``. The second goes through this module's ``run_hours``, which
    eval's backtest pair still replaces for one call (``snow_globe_eval.pair_forecast``).
    """
    if options.hours is not None:
        return _explicit_hours(run_start, options.hours)
    return run_hours(run_start, options.max_fh)


def _write_decision(
    entry: Mapping[str, Any] | None,
    file_exists: bool,
    field_names: Sequence[str],
    hours: Sequence[int],
    force: bool,
) -> str | None:
    """Why one viewpoint's file would not be written (None: it would be)."""
    if not entry or force or not file_exists:
        return None
    old_fields = set(entry.get("field_names", []))
    old_hours = set(entry.get("forecast_hours", []))
    if set(field_names) <= old_fields and set(hours) <= old_hours:
        return SKIP_ALREADY_WRITTEN
    if not (old_fields <= set(field_names) and old_hours <= set(hours)):
        # Never replace a file with one that lacks some of its fields or hours.
        return SKIP_WOULD_NARROW
    return None


def write_decisions(
    storage: Storage,
    run_start: datetime,
    *,
    viewpoint_ids: Sequence[str],
    field_names: Sequence[str],
    hours: Sequence[int],
    force: bool = False,
) -> dict[str, str | None]:
    """For each viewpoint, why its file would not be written by this request (None: it would).

    A file is left alone when it already holds these fields and hours
    (:data:`SKIP_ALREADY_WRITTEN`) or holds fields or hours the request lacks
    (:data:`SKIP_WOULD_NARROW`: a file is never narrowed), unless ``force``. The same rule
    decides at write time, so when every value is a reason, extracting the run would write
    nothing, and nothing should be downloaded for it. A run whose fields changed to a set
    that is not a superset is such a case: without this check, every tick would download it
    again and write nothing.
    """
    marker = storage.read_json(marker_key(run_start), default=None)
    entries = marker.get("viewpoints", {}) if isinstance(marker, dict) else {}
    return {
        vp: _write_decision(
            entries.get(vp),
            storage.exists(forecast_key(STORAGE_SOURCE, vp, run_start)),
            field_names,
            hours,
            force,
        )
        for vp in viewpoint_ids
    }


def run_covered(
    storage: Storage,
    run_start: datetime,
    *,
    viewpoint_ids: Sequence[str],
    field_names: Sequence[str],
    hours: Sequence[int],
    accept_missing: bool = False,
) -> bool:
    """True if the marker says every viewpoint already has these fields and hours.

    With ``accept_missing``, hours the marker records as missing from S3 at extraction
    time (an incomplete run processed once it went stale) count as covered.
    """
    marker = storage.read_json(marker_key(run_start), default=None)
    if not isinstance(marker, dict):
        return False
    if accept_missing:
        hours = [fh for fh in hours if fh not in set(marker.get("missing_hours", []))]
    done = marker.get("viewpoints", {})
    for vp in viewpoint_ids:
        entry = done.get(vp)
        if not entry or not storage.exists(forecast_key(STORAGE_SOURCE, vp, run_start)):
            return False
        if not set(field_names) <= set(entry.get("field_names", [])):
            return False
        if not set(hours) <= set(entry.get("forecast_hours", [])):
            return False
    return True


class Extractor:
    """Runs extractions against one bucket client and one storage backend."""

    def __init__(
        self,
        storage: Storage,
        bucket: HrrrBucket,
        manifests: Mapping[str, Viewpoint],
        *,
        lag_model: LagModel | None = None,
        grid: LambertConformalGrid = HRRR_CONUS,
        cache_root: Path | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.storage = storage
        self.bucket = bucket
        self.manifests = dict(manifests)
        self._lag_model = lag_model
        self.grid = grid
        self._cache_root = cache_root
        self.clock = clock
        #: Viewpoints the last extraction did not write, with the reason.
        self.skipped: dict[str, str] = {}
        self._missing_at_write: list[int] = []

    # -- helpers --------------------------------------------------------------------
    @property
    def lag_model(self) -> LagModel:
        """The availability model for archived runs, read from the log on first use."""
        if self._lag_model is None:
            self._lag_model = LagModel.from_storage(self.storage)
        return self._lag_model

    @property
    def cache_root(self) -> Path:
        """``$SNOW_GLOBE_DATA_DIR/cache/hrrr`` unless a root was given."""
        root = self._cache_root if self._cache_root is not None else cache_dir() / STORAGE_SOURCE
        root.mkdir(parents=True, exist_ok=True)
        return root

    def clean_cache(self) -> int:
        """Delete subset files left by an interrupted run (ours only); return how many."""
        removed = 0
        for path in self.cache_root.glob("*/*"):
            if path.is_file() and path.name.endswith((".subset.grib2", ".subset.grib2.part")):
                path.unlink(missing_ok=True)
                removed += 1
        for folder in self.cache_root.iterdir():
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        return removed

    def viewpoints(self, ids: Sequence[str] | None) -> list[Viewpoint]:
        """Viewpoints to process, in a stable order."""
        if ids is None:
            return [self.manifests[k] for k in sorted(self.manifests)]
        unknown = [vp for vp in ids if vp not in self.manifests]
        if unknown:
            raise KeyError(f"unknown viewpoint(s): {', '.join(unknown)}")
        return [self.manifests[k] for k in ids]

    def _plan_hour(self, files: HourFiles, names: list[str], first: bool) -> _HourPlan:
        assert files.grib is not None and files.idx is not None
        entries = parse_idx(self.bucket.get_text(files.idx.key))
        ranges, missing, terrain_only = _hour_ranges(
            entries, names, first=first, file_size=files.grib.size
        )
        return _HourPlan(files.fh, files, ranges, group_contiguous(ranges), missing, terrain_only)

    def _download(self, run_dir: Path, plan: _HourPlan) -> tuple[Path, datetime, float]:
        assert plan.files.grib is not None
        target = run_dir / f"{Path(plan.files.grib.key).name.removesuffix('.grib2')}.subset.grib2"
        part = target.with_name(target.name + ".part")
        started = time.monotonic()
        fetched = self.clock()
        try:
            with part.open("wb") as handle:
                for group in plan.groups:
                    handle.write(self.bucket.get_range(plan.files.grib.key, group.start, group.end))
            os.replace(part, target)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return target, fetched, time.monotonic() - started

    # -- the run ----------------------------------------------------------------------
    def extract(
        self,
        run_start: datetime,
        options: ExtractOptions | None = None,
        *,
        listing: RunListing | None = None,
    ) -> RunResult:
        """Extract one run for the chosen viewpoints and write the files (see module doc).

        ``listing`` is a listing of this run the caller has just made (the Lambda lists a
        run to decide whether it is ready); it is used instead of listing the run again.
        When no viewpoint's file would be written (:func:`write_decisions`), nothing is
        downloaded or logged: the result has no hours, ``written`` is empty and
        ``skipped`` says why.
        """
        opts = options or ExtractOptions()
        run_start = as_run_start(run_start)
        run_id = format_run_id(run_start)
        t0 = time.monotonic()
        started = self.clock()
        ledger_before = self.bucket.ledger.snapshot()
        wanted_hours = requested_hours(run_start, opts)  # before any request
        if listing is None:
            listing = list_run(self.bucket, run_start, now=started)
        elif listing.run_start != run_start:
            raise ValueError(f"the listing given is of run {listing.run_id}, not {run_id}")
        if not listing.exists:
            raise RunNotFoundError(f"run {run_id} is not in the bucket")
        missing = [fh for fh in wanted_hours if fh in listing.missing_hours]
        if missing and not opts.allow_incomplete:
            raise RunIncompleteError(run_id, missing)
        hours = [fh for fh in wanted_hours if fh not in missing]
        if not hours:
            raise RunIncompleteError(run_id, missing)
        live = started - run_start <= timedelta(hours=opts.live_window_h)
        viewpoints = self.viewpoints(opts.viewpoint_ids)
        specs = {spec.name: spec for spec in opts.fields}
        names = list(specs)
        all_specs = {**specs, TERRAIN_FIELD: field_by_name(TERRAIN_FIELD)}

        point_sets = {vp.id: sample_points(vp, self.grid) for vp in viewpoints}
        flat: list[int | None] = []
        offsets: dict[str, tuple[int, int]] = {}
        for vp_id, points in point_sets.items():
            offsets[vp_id] = (len(flat), len(flat) + len(points))
            flat.extend(flat_indices(self.grid, points))

        with self.storage.lock(LOCK_NAME, timeout_s=30.0, ttl_s=900.0):
            self.clean_cache()
            decisions = write_decisions(
                self.storage,
                run_start,
                viewpoint_ids=[vp.id for vp in viewpoints],
                field_names=names,
                hours=hours,
                force=opts.force,
            )
            if all(reason is not None for reason in decisions.values()):
                # Nothing would be written: no inventory, no GRIB, no log events.
                self.skipped = {vp: str(reason) for vp, reason in decisions.items()}
                return RunResult(
                    run_id=run_id,
                    live=live,
                    run_complete=listing.complete,
                    hours=[],
                    field_names=names,
                    viewpoints=[vp.id for vp in viewpoints],
                    written=[],
                    planned_bytes=0,
                    transfer=self.bucket.ledger.since(ledger_before),
                    bytes_by_field={},
                    seconds_total=time.monotonic() - t0,
                    seconds_download=0.0,
                    seconds_decode=0.0,
                    s3_lags_s=_s3_lags(listing),
                    fetch_started_utc=started,
                    fetch_finished_utc=self.clock(),
                    skipped=dict(self.skipped),
                )
            plans = self._plan_all(listing, hours, names, opts.workers)
            planned = sum(plan.planned_bytes for plan in plans)
            if opts.max_bytes is not None and planned > opts.max_bytes:
                raise BandwidthCapError(
                    f"run {run_id}: planned {planned / 1e6:.1f} MB exceeds the cap of "
                    f"{opts.max_bytes / 1e6:.1f} MB; nothing downloaded",
                    planned_bytes=planned,
                    cap_bytes=opts.max_bytes,
                )
            run_dir = self.cache_root / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            try:
                done = self._fetch_and_decode(
                    run_dir, plans, all_specs, run_start, flat, opts.workers
                )
            finally:
                shutil.rmtree(run_dir, ignore_errors=True)
            finished = self.clock()
            documents = self._documents(
                listing,
                done,
                names,
                specs,
                viewpoints,
                point_sets,
                offsets,
                live,
                started,
                finished,
                missing,
            )
            self._missing_at_write = missing
            written = self._write(run_start, documents, names, done, opts.force)
            lag_by_fh = _s3_lags(listing)
            result = RunResult(
                run_id=run_id,
                live=live,
                run_complete=listing.complete,
                hours=[item.plan.fh for item in done],
                field_names=names,
                viewpoints=[vp.id for vp in viewpoints],
                written=written,
                planned_bytes=planned,
                transfer=self.bucket.ledger.since(ledger_before),
                bytes_by_field=_bytes_by_field(done),
                seconds_total=time.monotonic() - t0,
                seconds_download=sum(item.download_s for item in done),
                seconds_decode=sum(item.decode_s for item in done),
                s3_lags_s=lag_by_fh,
                fetch_started_utc=started,
                fetch_finished_utc=finished,
                skipped=dict(self.skipped),
                documents=documents,
            )
            records: list[dict[str, Any]] = []
            if live:
                # Every hour the listing shows, not only the extracted ones: the lag model
                # wants all of them, and readers de-duplicate by (run, fh).
                records.extend(lag_events(listing))
            records.append(result.to_event())
            self.storage.append_jsonl_many(forecast_log_key(finished), records)
        return result

    def _plan_all(
        self, listing: RunListing, hours: list[int], names: list[str], workers: int
    ) -> list[_HourPlan]:
        first = hours[0]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [
                pool.submit(self._plan_hour, listing.hour(fh), names, fh == first) for fh in hours
            ]
            plans = [future.result() for future in futures]
        for name in names:
            if name == TERRAIN_FIELD:
                if TERRAIN_FIELD in plans[0].missing_fields:
                    raise IdxError(
                        f"{TERRAIN_FIELD} (fetched once, from the first hour) is not in the "
                        f"f{first:02d} inventory of run {listing.run_id}"
                    )
            elif all(name in plan.missing_fields for plan in plans):
                raise IdxError(f"field {name!r} is in no inventory of run {listing.run_id}")
        return plans

    def _fetch_and_decode(
        self,
        run_dir: Path,
        plans: list[_HourPlan],
        specs: Mapping[str, FieldSpec],
        run_start: datetime,
        flat: list[int | None],
        workers: int,
    ) -> list[_HourDone]:
        results: dict[int, _HourDone] = {}
        queue: list[_HourPlan] = []
        for plan in plans:
            if plan.ranges:
                queue.append(plan)
            else:  # nothing to fetch (e.g. only the static terrain asked for, past f00)
                results[plan.fh] = _HourDone(plan, self.clock(), 0.0, 0.0, {})
        pending: dict[Future[tuple[Path, datetime, float]], _HourPlan] = {}
        limit = max(1, workers)
        with ThreadPoolExecutor(max_workers=limit) as pool:
            try:
                while queue or pending:
                    while queue and len(pending) < limit:
                        plan = queue.pop(0)
                        pending[pool.submit(self._download, run_dir, plan)] = plan
                    finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in finished:
                        plan = pending.pop(future)
                        path, fetched, download_s = future.result()
                        t_decode = time.monotonic()
                        try:
                            decoded = decode_subset(
                                path,
                                plan.ranges,
                                specs=specs,
                                grid=self.grid,
                                run_start=run_start,
                                fh=plan.fh,
                                flat=flat,
                            )
                        finally:
                            path.unlink(missing_ok=True)  # CLAUDE.md: delete GRIB after extraction
                        results[plan.fh] = _HourDone(
                            plan,
                            fetched,
                            download_s,
                            time.monotonic() - t_decode,
                            {item.field: item for item in decoded},
                        )
            except BaseException:
                for future in pending:
                    future.cancel()
                raise
        return [results[plan.fh] for plan in plans]

    def _documents(
        self,
        listing: RunListing,
        done: list[_HourDone],
        names: list[str],
        specs: Mapping[str, FieldSpec],
        viewpoints: list[Viewpoint],
        point_sets: Mapping[str, list[GriddedPoint]],
        offsets: Mapping[str, tuple[int, int]],
        live: bool,
        started: datetime,
        finished: datetime,
        missing: Sequence[int],
    ) -> dict[str, dict[str, Any]]:
        run_start = listing.run_start
        terrain = done[0].decoded.get(TERRAIN_FIELD)
        hours_json = [self._hour_json(listing, item, live) for item in done]
        fields_json = {name: _field_json(specs[name], done, name) for name in names}
        docs: dict[str, dict[str, Any]] = {}
        for vp in viewpoints:
            lo, hi = offsets[vp.id]
            points = point_sets[vp.id]
            terrain_values = terrain.values[lo:hi] if terrain is not None else [None] * len(points)
            values = [
                [
                    item.decoded[name].values[lo:hi]
                    if name in item.decoded
                    # Static terrain: decoded once, from the first hour (TERRAIN_STATIC_EVIDENCE).
                    else list(terrain_values)
                    if name == TERRAIN_FIELD
                    else [None] * len(points)
                    for item in done
                ]
                for name in names
            ]
            doc: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "kind": KIND,
                "source": SOURCE,
                "model": MODEL,
                "model_version": None,
                "model_version_note": MODEL_VERSION_NOTE,
                "product": PRODUCT,
                "viewpoint_id": vp.id,
                "run_id": listing.run_id,
                "run_start_utc": isoformat_z(run_start),
                "run_max_fh": max_fh(run_start),
                "run_complete": listing.complete,
                "live": live,
                "listed_utc": isoformat_z(listing.listed_utc, "milliseconds"),
                "fetch_started_utc": isoformat_z(started, "milliseconds"),
                "fetch_finished_utc": isoformat_z(finished, "milliseconds"),
                "software": {"snow_globe_forecast": __version__, "eccodes": ecc_version()},
                "grid": self.grid.to_dict(),
                "forecast_hours": [item.plan.fh for item in done],
                # Asked for but not on S3 (the state marker uses the same list).
                "missing_hours": sorted(set(missing)),
                "hours": hours_json,
                "points": [_point_json(point, terrain_values[k]) for k, point in enumerate(points)],
                "field_names": names,
                "fields": fields_json,
                "values": values,
                "notes": _notes(vp, done, names, live),
            }
            validate_document(doc)
            docs[vp.id] = doc
        return docs

    def _hour_json(self, listing: RunListing, item: _HourDone, live: bool) -> dict[str, Any]:
        files = item.plan.files
        assert files.grib is not None and files.idx is not None
        available = files.availability_utc
        assert available is not None
        run_start = listing.run_start
        model: dict[str, Any] | None
        if live:
            availability, source, model = available, "live-s3-lastmodified", None
        else:  # modelled, but never before the hour's own trusted stamps (PRD §13.9)
            availability, source, model = archived_availability(
                self.lag_model,
                run_start,
                item.plan.fh,
                ArchivedStamps(files.grib.last_modified, files.idx.last_modified),
            )
        return {
            "fh": item.plan.fh,
            "valid_time_utc": isoformat_z(valid_time(run_start, item.plan.fh)),
            "url": object_url(object_key(run_start, item.plan.fh)),
            "idx_url": object_url(idx_key(run_start, item.plan.fh)),
            "grib_size_bytes": files.grib.size,
            "grib_last_modified_utc": isoformat_z(files.grib.last_modified),
            "idx_last_modified_utc": isoformat_z(files.idx.last_modified),
            "availability_utc": isoformat_z(availability),
            "availability_source": source,
            "availability_model": model,
            "lag_s": round((availability - run_start).total_seconds(), 3),
            "fetched_utc": isoformat_z(item.fetched_utc, "milliseconds"),
            "byte_ranges": [
                {
                    "field": r.field,
                    "message": r.message,
                    "start": r.start,
                    "end": r.end,
                    "bytes": r.length,
                    "terrain_only": item.plan.terrain_only and r.field == TERRAIN_FIELD,
                }
                for r in item.plan.ranges
            ],
            "requests": [[g.start, g.end] for g in item.plan.groups],
            "bytes_downloaded": item.plan.planned_bytes,
            "fields_missing_from_idx": item.plan.missing_fields,
        }

    def _write(
        self,
        run_start: datetime,
        documents: Mapping[str, dict[str, Any]],
        names: list[str],
        done: list[_HourDone],
        force: bool,
    ) -> list[str]:
        marker = self.storage.read_json(marker_key(run_start), default=None)
        if not isinstance(marker, dict):
            marker = {"run_id": format_run_id(run_start), "viewpoints": {}}
        hours = [item.plan.fh for item in done]
        written: list[str] = []
        self.skipped = {}
        for vp_id, doc in documents.items():
            key = forecast_key(STORAGE_SOURCE, vp_id, run_start)
            reason = _write_decision(
                marker["viewpoints"].get(vp_id), self.storage.exists(key), names, hours, force
            )
            if reason is not None:
                self.skipped[vp_id] = reason
                continue
            self.storage.put_bytes(key, encode_document(doc), content_type="application/json")
            marker["viewpoints"][vp_id] = {
                "field_names": names,
                "forecast_hours": hours,
                "written_utc": isoformat_z(self.clock(), "milliseconds"),
                "live": doc["live"],
            }
            written.append(key)
        marker["run_start_utc"] = isoformat_z(run_start)
        marker["missing_hours"] = sorted(set(self._missing_at_write))
        self.storage.write_json(marker_key(run_start), marker)
        return written


def _hour_ranges(
    entries: Sequence[IdxEntry], names: Sequence[str], *, first: bool, file_size: int
) -> tuple[list[MessageRange], list[str], bool]:
    """Byte ranges one hour needs: (ranges, requested names absent from the idx, terrain_only).

    The terrain (:data:`TERRAIN_FIELD`) is static, so it is planned for the first hour of
    a run only: always there (it fills ``model_terrain_m``), never in later hours.
    ``terrain_only`` is True when the first hour fetches it without it being requested.
    """
    wanted = [name for name in names if name != TERRAIN_FIELD]
    if first:
        wanted.append(TERRAIN_FIELD)
    present: list[str] = []
    missing: list[str] = []
    for name in wanted:
        try:
            select_entry(entries, name)
            present.append(name)
        except IdxError:
            if name in names:
                missing.append(name)
    ranges = message_ranges(entries, present, file_size=file_size)
    return ranges, missing, first and TERRAIN_FIELD not in names


def _s3_lags(listing: RunListing) -> dict[int, float]:
    """S3 lag (the later stamp - run start, seconds) of every ready hour of a listing."""
    out: dict[int, float] = {}
    for fh in listing.ready_hours:
        available = listing.hour(fh).availability_utc
        assert available is not None
        out[fh] = round((available - listing.run_start).total_seconds(), 3)
    return out


def _bytes_by_field(done: Sequence[_HourDone]) -> dict[str, dict[int, int]]:
    out: dict[str, dict[int, int]] = {}
    for item in done:
        for r in item.plan.ranges:
            label = (
                f"{r.field} (terrain only)"
                if item.plan.terrain_only and r.field == TERRAIN_FIELD
                else r.field
            )
            out.setdefault(label, {})[item.plan.fh] = r.length
    return out


def _field_json(spec: FieldSpec, done: Sequence[_HourDone], name: str) -> dict[str, Any]:
    metas = [item.decoded[name].meta for item in done if name in item.decoded]
    if not metas:
        raise IdxError(f"field {name!r} decoded in no hour")
    first = metas[0]
    for meta in metas[1:]:
        for key in ("shortName", "typeOfLevel", "units", "level"):
            if meta[key] != first[key]:
                raise ValueError(
                    f"{name}: {key} changes between hours ({first[key]} -> {meta[key]})"
                )
    return {
        "shortName": str(first["shortName"]),
        "name": str(first["name"]),
        "typeOfLevel": str(first["typeOfLevel"]),
        "level": int(first["level"]),
        "units": str(first["units"]),
        "datum": spec.datum,
        "datum_evidence": spec.datum_evidence,
        "grib": {
            "discipline": int(first["discipline"]),
            "parameterCategory": int(first["parameterCategory"]),
            "parameterNumber": int(first["parameterNumber"]),
            "typeOfFirstFixedSurface": int(first["typeOfFirstFixedSurface"]),
        },
        "uvRelativeToGrid": first["uvRelativeToGrid"]
        if spec.variable in {"UGRD", "VGRD"}
        else None,
        "description": (
            f"{spec.description}; static, so fetched once per run (first hour) and repeated "
            "for every hour"
            if name == TERRAIN_FIELD
            else spec.description
        ),
    }


def _point_json(point: GriddedPoint, terrain: float | None) -> dict[str, Any]:
    return {
        "id": point.id,
        "kind": point.kind,
        "lat": round(point.lat, 6),
        "lon": round(point.lon, 6),
        "i": point.i,
        "j": point.j,
        "cell_lat": None if point.cell_lat is None else round(point.cell_lat, 6),
        "cell_lon": None if point.cell_lon is None else round(point.cell_lon, 6),
        "model_terrain_m": terrain,
        "distance_km": round(point.distance_km, 4),
        "azimuth_deg": None if point.azimuth_deg is None else round(point.azimuth_deg % 360.0, 3),
        "name": point.name,
    }


def _notes(vp: Viewpoint, done: Sequence[_HourDone], names: Sequence[str], live: bool) -> list[str]:
    notes = [
        "Values are [field][forecast hour][point]; null = GRIB bitmap missing (for HGT:cloud "
        "base/ceiling/top: no cloud or no ceiling), a point outside the grid, or a field absent "
        "from that hour's inventory. Values are rounded to 6 significant digits.",
        "Ray points sample the view azimuth every 1.5 km to 45 km; the cell where the view ray "
        "meets each layer height is chosen in M2, once the camera pose exists (PRD 8.3).",
        "model_terrain_m is HGT:surface (m above MSL) from the first hour; it is the model's "
        "smoothed 3 km terrain, not the DEM: one cell can span a rim and a floor.",
        "Lead = valid_time_utc - availability_utc (PRD 8.3).",
    ]
    if TERRAIN_FIELD in names:
        notes.append(
            f"{TERRAIN_STATIC_EVIDENCE} It is therefore downloaded once, with the first hour, "
            "and its values are repeated for every hour; later hours list no byte range for it."
        )
    if done and TERRAIN_FIELD not in done[0].decoded:
        notes.append(
            f"model_terrain_m is null: {TERRAIN_FIELD} was not in the "
            f"f{done[0].plan.fh:02d} inventory, the only hour it is fetched from."
        )
    if "HGT:cloud base" in names:
        notes.append(
            "HGT:cloud base is m above MSL but often invalid: on 2026-11-19 07Z f00, 12.4 % of "
            "cloudy cells had a phantom low base ~8 km below the height PRES:cloud base implies "
            "(high-cloud-only columns); 2.45 % were even below the terrain. Use it only where "
            "it agrees with PRES:cloud base (see datum_evidence)."
        )
    if any(spec in names for spec in ("UGRD:10 m above ground", "VGRD:10 m above ground")):
        notes.append(
            "UGRD/VGRD are grid-relative (uvRelativeToGrid = 1); rotate by "
            "n*(lon - LoV), n = sin(38.5 deg), before reading them as east/north."
        )
    if not live:
        notes.append(
            "Archived run: availability_utc is modeled (run start + the lag model of "
            "snow_globe_forecast.availability.LagModel when this file was written: the p95 of "
            "the lags logged for this cycle and hour, live or surveyed from S3 listings, or a "
            "conservative 120 min until enough are logged; availability_model says which and "
            "counts the samples by method), but never before the hour's own S3 LastModified "
            "stamps: where they are later than the model, they are the availability "
            "(availability_source archived-s3-lastmodified, availability_model.s3_floor_utc). "
            "A stamp more than 24 h after the run start is a rewritten object and is ignored "
            "(s3_stamps_ignored). availability.select_stored_run recomputes the model from the "
            "current log and applies the same stamps."
        )
    missing = sorted({name for item in done for name in item.plan.missing_fields})
    if missing:
        notes.append(f"Fields absent from some inventories (nulls there): {', '.join(missing)}.")
    if not vp.landmarks:
        notes.append(
            "This viewpoint's manifest has no landmarks yet, so there are no landmark points."
        )
    return notes


def plan_run(
    bucket: HrrrBucket,
    run_start: datetime,
    *,
    fields: Sequence[FieldSpec] = CATALOGUE,
    max_fh: int | None = None,
    workers: int = 4,
    hours: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Bytes each field would cost per forecast hour, from the inventories alone.

    Plans exactly what :meth:`Extractor.extract` would download (the terrain once, with
    the first hour) for ``0..max_fh``, or exactly ``hours`` when given (as
    :attr:`ExtractOptions.hours`). Downloads only the listing and the ``.idx`` files
    (about 10 KB each), no GRIB data.
    """
    run_start = as_run_start(run_start)
    if hours is not None and max_fh is not None:
        raise ValueError("give either hours or max_fh, not both")
    wanted = (
        _explicit_hours(run_start, hours) if hours is not None else run_hours(run_start, max_fh)
    )
    ledger_before = bucket.ledger.snapshot()
    listing = list_run(bucket, run_start)
    ready = [fh for fh in wanted if fh in listing.ready_hours]
    names = [spec.name for spec in fields]

    def one(fh: int) -> tuple[int, dict[str, int], int, list[str]]:
        files = listing.hour(fh)
        assert files.grib is not None and files.idx is not None
        entries = parse_idx(bucket.get_text(files.idx.key))
        ranges, absent, _ = _hour_ranges(
            entries, names, first=fh == ready[0], file_size=files.grib.size
        )
        return fh, {r.field: r.length for r in ranges}, files.grib.size, absent

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = list(pool.map(one, ready))
    labels = names if TERRAIN_FIELD in names else [*names, f"{TERRAIN_FIELD} (terrain only)"]
    per_field = {
        label: {
            fh: sizes[label.removesuffix(" (terrain only)")]
            for fh, sizes, _, _ in rows
            if label.removesuffix(" (terrain only)") in sizes
        }
        for label in labels
    }
    per_hour = {fh: sum(sizes.values()) for fh, sizes, _, _ in rows}
    file_bytes = {fh: size for fh, _, size, _ in rows}
    return {
        "run": listing.run_id,
        "hours": ready,
        "missing_hours": listing.missing_hours,
        "fields": names,
        "fields_missing_from_idx": {fh: absent for fh, _, _, absent in rows if absent},
        "bytes_by_field_by_fh": per_field,
        "bytes_by_fh": per_hour,
        "total_bytes": sum(per_hour.values()),
        "grib_file_bytes_by_fh": file_bytes,
        "fraction_of_files": (
            sum(per_hour.values()) / sum(file_bytes.values()) if file_bytes else None
        ),
        "transfer": bucket.ledger.since(ledger_before),
    }
