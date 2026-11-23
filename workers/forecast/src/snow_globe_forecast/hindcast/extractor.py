"""The hindcast mode of the extractor: archived runs in the live files' format (PRD §13.2, §13.4).

:class:`HindcastExtractor` is the live :class:`..extract.Extractor` with three differences:

- it writes through a :class:`.storage.HindcastStorage`, so its files land in
  ``<data dir>/forecast/hindcast/hrrr/<viewpoint_id>/<YYYYMMDDHH>z.json``, its ``run`` events in
  ``forecast/hindcast/log/<day>.jsonl``, its markers and locks in ``forecast/hindcast/state/``,
  and its GRIB subsets in ``forecast/hindcast/cache/grib/`` (deleted after decoding); never in
  ``forecast/hrrr/`` or ``forecast_log/``;
- every file is ``"live": false``, and a run less than 24 h old is refused (its stamps could
  still change, and a live run belongs to the live worker);
- an hour's ``availability_utc`` is its S3 ``LastModified``, the later of the GRIB2 and
  ``.idx`` stamps, exactly as a live file records it (:func:`.plan.hindcast_availability`),
  with ``availability_source`` ``archived-s3-lastmodified`` and ``availability_model`` the
  **live lag model's** prediction for the hour (the p95 rules of
  :class:`..availability.LagModel` over the live lags of ``forecast_log/*.jsonl`` only, read
  when the file is written), plus the stamp floor. So each hour carries both, for comparison
  (:mod:`.compare`); the availability itself never depends on the model, and is never
  earlier than S3 says.

``archived-s3-lastmodified`` therefore means, in a hindcast file, "the availability is the
hour's own S3 stamps, by the hindcast's rule", whatever the model says; in the worker's own
archived files it means the stamps were later than the model (D-054). A hindcast hour can be
told by its ``availability_model.lag_s`` (the model) being later than its ``lag_s`` (the
stamps), as it is for every hour written on 23 Nov 2026.

**Two rules for one file.** The hindcast's rule is the task's: availability = the S3 stamps.
D-008, D-027 and PRD §8.3 model an archived run as run start + the p95 lag, never before the
stamps (D-054), and that is what :func:`..availability.select_stored_run` (through
``availability_from_document``) does with any ``"live": false`` file: it ignores the recorded
``availability_utc`` and re-models each hour from a lag model, never earlier than the stamps.
Given no ``lag_model``, it reads the lag log of the storage it is handed, and a
:class:`.storage.HindcastStorage`'s log holds no lag events, so every hour would sit on the
120 min fallback: give it the live data dir's model explicitly
(``LagModel.from_storage(<live data dir>)``), and expect a different run for some frames even
then. To pair frames under the hindcast's rule, read ``availability_utc`` (as eval's P1 dev
table does). The lead decides which rule P1 uses.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from snow_globe_common.manifest import Viewpoint
from snow_globe_common.paths import forecast_key
from snow_globe_common.storage import LocalStorage, Storage
from snow_globe_common.timeutil import isoformat_z, utcnow

from ..availability import LagModel
from ..bucket import HrrrBucket
from ..discovery import STAMP_TRUST_H, HourFiles, RunListing
from ..extract import ExtractOptions, Extractor, RunResult
from ..grid import HRRR_CONUS, LambertConformalGrid
from ..hrrr import STORAGE_SOURCE, as_run_start, format_run_id
from ..laglog import METHOD_LIVE
from .plan import SOURCE_S3, hindcast_availability
from .storage import HindcastStorage

__all__ = [
    "GRIB_CACHE",
    "HINDCAST_NOTE",
    "HindcastExtractor",
    "HindcastRunTooNewError",
    "live_lag_model",
]

#: Where the hindcast's GRIB subsets wait for decoding, in the hindcast root (deleted at once).
GRIB_CACHE = "cache/grib"

HINDCAST_NOTE = (
    "Hindcast file (snow-globe-forecast hindcast; <data dir>/forecast/hindcast/, never the live "
    "worker's forecast/hrrr/): live is false, and each hour's availability_utc is its S3 "
    "LastModified, the later of grib_last_modified_utc and idx_last_modified_utc, exactly as a "
    "live file records it (listed from S3 when the hindcast ran; a stamp more than 24 h after "
    "the run start is a rewritten object and is not trusted, s3_stamps_ignored), so it is never "
    "earlier than S3 says. availability_source is archived-s3-lastmodified, and "
    "availability_model is the live lag model's prediction for the hour, kept for comparison "
    "only: snow_globe_forecast.availability.LagModel over the live lags of "
    "forecast_log/*.jsonl when this file was written (no survey lags), method and lag_s as for "
    "an archived file, with the S3 floor in s3_floor_utc. Here archived-s3-lastmodified means "
    "the stamps are the availability by the hindcast's rule, not that they were later than the "
    "model. Lead = valid_time_utc - availability_utc (PRD 8.3, D-008). D-008, D-027 and PRD 8.3 "
    "model an archived hour instead (the p95 lag, never before the stamps, D-054): "
    "snow_globe_forecast.availability.select_stored_run re-models this file's hours that way "
    "and ignores availability_utc, from the lag log of the storage it reads unless given a "
    "lag_model (a hindcast storage's log has no lags: pass the live data dir's LagModel)."
)


class HindcastRunTooNewError(ValueError):
    """The run is less than 24 h old: not archived yet (and the live worker's)."""


def live_lag_model(storage: Storage) -> LagModel:
    """The live lag model: :class:`..availability.LagModel` over ``forecast_log/*.jsonl``'s live
    lags only (``storage`` is the live data dir, read only)."""
    return LagModel.from_storage(storage, methods={METHOD_LIVE})


class HindcastExtractor(Extractor):
    """:class:`..extract.Extractor` in hindcast mode (see the module docstring).

    ``live_model`` is the live lag model (:func:`live_lag_model`). ``live_storage`` (the live
    data dir, read only) lets each file say whether the live worker stored the run too.
    """

    archived_note = HINDCAST_NOTE

    def __init__(
        self,
        storage: HindcastStorage,
        bucket: HrrrBucket,
        manifests: Mapping[str, Viewpoint],
        *,
        live_model: LagModel,
        live_storage: Storage | None = None,
        grid: LambertConformalGrid = HRRR_CONUS,
        cache_root: Path | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not isinstance(storage, HindcastStorage):
            raise TypeError("a HindcastExtractor writes only through a HindcastStorage")
        if cache_root is None:
            # Never the live worker's cache/hrrr/: Extractor.clean_cache deletes the subsets it
            # finds there, which would be the live stand-in's while it extracts.
            if not isinstance(storage.root, LocalStorage):
                raise ValueError("give cache_root for a hindcast storage that is not local")
            cache_root = storage.root.root / GRIB_CACHE
        super().__init__(
            storage,
            bucket,
            manifests,
            lag_model=live_model,
            grid=grid,
            cache_root=cache_root,
            clock=clock,
        )
        self.live_storage = live_storage

    def _archived_hour(
        self, run_start: datetime, fh: int, files: HourFiles
    ) -> tuple[datetime, str, dict[str, Any]]:
        """The S3 availability of one hour, with the live lag model's prediction beside it."""
        assert files.grib is not None and files.idx is not None
        available, floor, ignored = hindcast_availability(
            run_start, files.grib.last_modified, files.idx.last_modified
        )
        note = self.lag_model.describe(run_start, fh)
        note["s3_floor_utc"] = isoformat_z(floor)
        note["s3_stamps_ignored"] = list(ignored)
        return available, SOURCE_S3, note

    def _documents(
        self,
        listing: RunListing,
        done: list[Any],
        names: list[str],
        specs: Mapping[str, Any],
        viewpoints: list[Viewpoint],
        point_sets: Mapping[str, list[Any]],
        offsets: Mapping[str, tuple[int, int]],
        live: bool,
        started: datetime,
        finished: datetime,
        missing: Sequence[int],
    ) -> dict[str, dict[str, Any]]:
        docs = super()._documents(
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
        for vp_id, doc in docs.items():
            doc["notes"].append(self._live_note(vp_id, listing.run_start))
        return docs

    def _live_note(self, viewpoint_id: str, run_start: datetime) -> str:
        if self.live_storage is None:
            return "Whether the live worker stored this run was not checked."
        key = forecast_key(STORAGE_SOURCE, viewpoint_id, run_start)
        doc = self.live_storage.read_json(key, default=None)
        if not isinstance(doc, Mapping):
            return f"The live worker has no file for this run ({key} does not exist)."
        return (
            f"The live worker stored this run too: {key} (forecast hours "
            f"{doc.get('forecast_hours')}, fetch_finished_utc {doc.get('fetch_finished_utc')})."
        )

    def extract(
        self,
        run_start: datetime,
        options: ExtractOptions | None = None,
        *,
        listing: RunListing | None = None,
    ) -> RunResult:
        """Extract one archived run in hindcast mode (refuses a run less than 24 h old)."""
        start = as_run_start(run_start)
        age = self.clock() - start
        if age < timedelta(hours=STAMP_TRUST_H):
            raise HindcastRunTooNewError(
                f"run {format_run_id(start)} is {age.total_seconds() / 3600:.1f} h old; a "
                f"hindcast takes runs at least {STAMP_TRUST_H:g} h old"
            )
        # live_window_h = 0: with the run at least 24 h old, the file is never live.
        opts = replace(options or ExtractOptions(), live_window_h=0.0)
        return super().extract(start, opts, listing=listing)
