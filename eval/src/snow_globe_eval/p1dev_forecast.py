"""The dev P1 table's forecast: the latest hindcast run available at the issue time (PRD §8.3).

PRD §13.1: nothing a prediction uses may postdate its issue time.

PLAN M1.5's dev-only P1 table pairs each Rainier daytime frame with a 24 h forecast. The
issue time is valid − lead, and the forecast is **the latest hindcast file whose availability
is at or before the issue time** (D-008: "Lead time = valid − availability"). The files are
snow-globe-forecast run files (``snow-globe.forecast-run`` schema version 1, read by
:class:`.forecast_io.ForecastRun`) under ``<data dir>/forecast/hindcast/hrrr/<viewpoint>/``.

**When an hour was available** is one of two rules (:data:`AVAILABILITY_RULES`, the table's
``--availability``; every choice, record and table names the rule it used):

- ``d054`` (**the default**, the decided rule for an archived run): run start plus the lag
  model's lag (D-027), but never before the hour's own S3 stamps (D-054), i.e. the later of
  ``run start + availability_model.lag_s`` and the publication floor (the file's
  ``availability_model.s3_floor_utc``, and the floor its recorded ``grib_last_modified_utc`` and
  ``idx_last_modified_utc`` set by D-054's rule, whichever is later). The lag is the one each
  hour records: the forecast worker's live lag model when the hindcast file was written. D-027
  asks for the model "recomputed at selection from the current log"; this table never reads
  ``forecast_log/``, so it takes the recorded model, and says so. An archived hour that records
  no lag model is unusable under this rule. A live file's hour (``"live": true``) is available at
  the later of its S3 stamp and the file's ``fetch_finished_utc`` (D-027's live rule).
- ``s3-stamps``: the hour's recorded ``availability_utc``, which for a hindcast file is its S3
  ``LastModified`` stamps alone, the hindcast's own rule (snow-globe-forecast's
  ``hindcast/extractor.py``: "The lead decides which rule P1 uses"). Kept as a labelled
  sensitivity until the lead or owner rules.

**The choice** (:meth:`HindcastCatalogue.choose`), for one (issue, valid) under one rule:

1. a file serves the valid time only if it holds both bracketing forecast hours
   (:meth:`.forecast_io.ForecastRun.bracket`; values are interpolated between them, §8.3);
2. its availability for the valid time is the later of those two hours' availabilities;
3. a file whose availability is after the issue time is never used (PRD §13.1: nothing may
   postdate the issue time), and among the rest the newest run start wins: the rule of
   snow-globe-forecast's ``select_run_for_issue``. Under ``d054`` this is what its
   ``select_stored_run`` does for archived files, with the recorded lag model in place of one
   recomputed from the log; under ``s3-stamps`` it is the hindcast plan's rule.

**Files are checked as they are catalogued**, before any is chosen:

- the folder and every file are refused (:class:`.heldout.LeakageError`) if they lie under the
  quarantine folder or link into it (D-022);
- a file that does not parse, or is for another viewpoint, is left out and listed in
  ``problems``; two files holding one run make the catalogue ambiguous (``ValueError``);
- an hour whose recorded availability is earlier than its own recorded S3 stamps
  (``grib_last_modified_utc``, ``idx_last_modified_utc``) is unusable and listed: an hour can
  never have been readable before NOAA published it (D-054). A file can still serve valid
  times that need only its other hours.

**The live field profile.** The hindcast extracts the live worker's fields (D-050: visibility,
ceiling, cloud base and its pressure, low, mid and high cover, terrain), the profile design P's
live files carry. :func:`.scene.build_scene` also reads ``HGT:cloud top``, ``PRATE:surface`` and
the 10 m winds, and raises when a file lacks them. :func:`scene_run` gives it the file's run
with those fields present but **missing at every hour** (listed in each hour's
``fields_missing_from_idx``, read as ``not-in-inventory``), so the builder takes its own
documented fallbacks, as for a null value: no cloud-top cap on a layer, precipitation 0 and
wind 0, the last two flagged in the scene. Nothing else changes, the run keeps the file's
SHA-256, and the scene's meta lists the fields read as missing. TODO(M1.5): the scene builder
itself should accept the live profile (:mod:`.scene` is not this package's to change).
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from snow_globe_common.timeutil import isoformat_z, parse_iso_utc

from .forecast_io import (
    CLOUD_TOP,
    PRECIP_RATE,
    U_WIND,
    V_WIND,
    ForecastFileError,
    ForecastRun,
)
from .heldout import LeakageError, path_quarantine_reason

__all__ = [
    "AVAILABILITY_RULES",
    "DEFAULT_AVAILABILITY_RULE",
    "HINDCAST_SUBDIR",
    "SCENE_OPTIONAL_FIELDS",
    "ForecastChoice",
    "HindcastCatalogue",
    "HindcastFile",
    "hindcast_dir",
    "hour_availability",
    "other_rule",
    "publication_floor",
    "scene_run",
]

#: Where the hindcast runs live, under the data dir (one folder per viewpoint).
HINDCAST_SUBDIR = ("forecast", "hindcast", "hrrr")

#: The availability rules (see the module docstring), with what each one means.
AVAILABILITY_RULES: dict[str, str] = {
    "d054": "the later of run start + the hour's recorded lag model (D-027) and its S3 "
    "publication floor (D-054); a live hour: the later of its S3 stamp and the file's fetch "
    "finish (D-027). The lag model is the one each hour records (the live log's model when the "
    "file was written), not one recomputed from forecast_log/ at selection",
    "s3-stamps": "each hour's recorded availability_utc: for a hindcast file its S3 "
    "LastModified stamps alone (the hindcast's rule, pending the lead's ruling)",
}
DEFAULT_AVAILABILITY_RULE = "d054"
#: D-054 (snow-globe-forecast ``discovery.STAMP_TRUST_H``): a stamp counts only this long after
#: the run start; a later one belongs to a rewritten object.
STAMP_TRUST_H = 24.0
#: D-054 (``discovery.IDX_AFTER_GRIB_MARGIN_S``): with only the GRIB2 stamp trusted, the hour
#: counts as usable this long after it.
IDX_AFTER_GRIB_MARGIN_S = 300.0


def other_rule(rule: str) -> str:
    """The availability rule that is not ``rule`` (for the table's listing of differences)."""
    if rule not in AVAILABILITY_RULES:
        raise ValueError(f"availability rule must be one of {sorted(AVAILABILITY_RULES)}")
    return next(name for name in AVAILABILITY_RULES if name != rule)


def publication_floor(
    run_start: datetime, grib: datetime | None, idx: datetime | None
) -> datetime | None:
    """The earliest an hour can have been usable, from its own S3 stamps (D-054).

    The same rule as snow-globe-forecast's ``discovery.publication_floor`` (eval does not import
    the forecast package): a stamp counts only within :data:`STAMP_TRUST_H` of the run start;
    with both stamps trusted the floor is the later one; with only the ``.idx`` stamp, that
    stamp; with only the GRIB2 stamp, that stamp plus :data:`IDX_AFTER_GRIB_MARGIN_S`; with
    neither, None.
    """
    limit = run_start + timedelta(hours=STAMP_TRUST_H)
    grib = grib if grib is not None and grib <= limit else None
    idx = idx if idx is not None and idx <= limit else None
    if grib is not None and idx is not None:
        return max(grib, idx)
    if idx is not None:
        return idx
    if grib is not None:
        return grib + timedelta(seconds=IDX_AFTER_GRIB_MARGIN_S)
    return None


def hindcast_dir(data_root: Path, viewpoint_id: str) -> Path:
    """``<data dir>/forecast/hindcast/hrrr/<viewpoint_id>``."""
    return data_root.joinpath(*HINDCAST_SUBDIR, viewpoint_id)


#: Fields the scene builder reads but the live profile lacks, with each one's datum
#: (snow-globe-forecast's ``fields.py``): read as missing at every hour (:func:`scene_run`).
SCENE_OPTIONAL_FIELDS: dict[str, str] = {
    CLOUD_TOP: "m above MSL",
    PRECIP_RATE: "n/a",
    U_WIND: "n/a",
    V_WIND: "n/a",
}


def scene_run(run: ForecastRun) -> tuple[ForecastRun, tuple[str, ...]]:
    """``run`` with :data:`SCENE_OPTIONAL_FIELDS` it lacks present but missing at every hour.

    Returns the run (the same object when nothing is absent) and the fields added. The copy
    keeps the file's path and SHA-256, and every value the file holds.
    """
    names = list(run.doc.get("field_names") or [])
    absent = tuple(name for name in SCENE_OPTIONAL_FIELDS if name not in names)
    if not absent:
        return run, ()
    doc = copy.deepcopy(dict(run.doc))
    n_hours, n_points = len(doc["forecast_hours"]), len(doc["points"])
    for name in absent:
        doc["field_names"].append(name)
        doc["fields"][name] = {
            "datum": SCENE_OPTIONAL_FIELDS[name],
            "absent_from_file": True,
            "description": "not in this file (the live field profile, D-050): missing at every "
            "hour (snow_globe_eval.p1dev_forecast.scene_run)",
        }
        doc["values"].append([[None] * n_points for _ in range(n_hours)])
    for hour in doc["hours"]:
        hour["fields_missing_from_idx"] = sorted(
            set(hour.get("fields_missing_from_idx") or []) | set(absent)
        )
    return ForecastRun(doc, run.path, run.sha256), absent


def _time(value: Any) -> datetime | None:
    try:
        return parse_iso_utc(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def hour_availability(run: ForecastRun, fh: int, rule: str) -> tuple[datetime, str] | str:
    """When forecast hour ``fh`` of ``run`` was available under ``rule``, and the source.

    Returns ``(time, source)``, or a string saying why the hour has no availability under the
    rule (it is then unusable). See the module docstring for the two rules.
    """
    if rule not in AVAILABILITY_RULES:
        raise ValueError(f"availability rule must be one of {sorted(AVAILABILITY_RULES)}")
    hour = run.hour(fh)
    recorded = _time(hour.get("availability_utc"))
    if rule == "s3-stamps":
        if recorded is None:
            return "no readable availability_utc"
        return recorded, f"s3-stamps ({hour.get('availability_source')})"
    if run.doc.get("live"):
        if recorded is None:
            return "no readable availability_utc"
        fetched = _time(run.doc.get("fetch_finished_utc"))
        if fetched is not None and fetched > recorded:
            return fetched, "d054-live-fetch-finished (D-027)"
        return recorded, f"d054-live ({hour.get('availability_source')})"
    model = hour.get("availability_model")
    lag = model.get("lag_s") if isinstance(model, Mapping) else None
    try:
        lag_s = float(lag) if lag is not None else None
    except (TypeError, ValueError):
        lag_s = None
    if lag_s is None:
        return "no recorded lag model (availability_model.lag_s), which D-027 needs"
    modelled = run.run_start + timedelta(seconds=lag_s)
    floors = [
        publication_floor(
            run.run_start,
            _time(hour.get("grib_last_modified_utc")),
            _time(hour.get("idx_last_modified_utc")),
        ),
        _time(model.get("s3_floor_utc")) if isinstance(model, Mapping) else None,
    ]
    known = [f for f in floors if f is not None]
    floor = max(known) if known else None
    method = model.get("method") if isinstance(model, Mapping) else None
    if floor is not None and floor > modelled:
        return floor, "d054-s3-floor (the stamps are later than the lag model)"
    return modelled, f"d054-lag-model ({method}, lag {lag_s:.0f} s)"


@dataclass(frozen=True)
class HindcastFile:
    """One catalogued run file: its identity and the hours it may serve."""

    path: Path
    sha256: str
    run: ForecastRun
    #: Hours that may not be used under any rule, with why (availability before its own S3
    #: stamps, D-054).
    unusable_hours: Mapping[int, str] = field(default_factory=dict)
    #: Per rule, each hour's availability and source, or why it has none (then unusable).
    availability_by_rule: Mapping[str, Mapping[int, tuple[datetime, str] | str]] = field(
        default_factory=dict
    )

    @property
    def run_id(self) -> str:
        """``YYYYMMDDHH``."""
        return self.run.run_id

    @property
    def run_start(self) -> datetime:
        """The run's initialisation, UTC."""
        return self.run.run_start

    def hour_available(self, fh: int, rule: str) -> tuple[datetime, str] | str:
        """Hour ``fh``'s availability under ``rule`` (see :func:`hour_availability`)."""
        by_rule = self.availability_by_rule.get(rule)
        if by_rule is not None and fh in by_rule:
            return by_rule[fh]
        return hour_availability(self.run, fh, rule)

    def to_record(self) -> dict[str, Any]:
        """Provenance for the run record."""
        return {
            "name": self.path.name,
            "sha256": self.sha256,
            "run": self.run_id,
            "live": self.run.doc.get("live"),
            "forecast_hours": self.run.forecast_hours,
            "fields": list(self.run.doc.get("field_names") or []),
            "unusable_hours": {str(k): v for k, v in sorted(self.unusable_hours.items())},
            "availability_by_rule": {
                rule: {
                    str(fh): (
                        value if isinstance(value, str) else [isoformat_z(value[0]), value[1]]
                    )
                    for fh, value in sorted(hours.items())
                }
                for rule, hours in sorted(self.availability_by_rule.items())
            },
        }


def _unusable_hours(run: ForecastRun) -> dict[int, str]:
    """Hours dated before their own recorded S3 stamps, or with no readable availability."""
    out: dict[int, str] = {}
    for fh in run.forecast_hours:
        hour = run.hour(fh)
        available = _time(hour.get("availability_utc"))
        if available is None:
            out[fh] = "no readable availability_utc"
            continue
        for name in ("grib_last_modified_utc", "idx_last_modified_utc"):
            stamp = _time(hour.get(name))
            if stamp is not None and available < stamp:
                out[fh] = (
                    f"availability {isoformat_z(available)} is before its own {name} "
                    f"{isoformat_z(stamp)} (D-054)"
                )
                break
    return out


def _availability_by_rule(run: ForecastRun) -> dict[str, dict[int, tuple[datetime, str] | str]]:
    return {
        rule: {fh: hour_availability(run, fh, rule) for fh in run.forecast_hours}
        for rule in AVAILABILITY_RULES
    }


@dataclass(frozen=True)
class ForecastChoice:
    """The forecast chosen for one (issue, valid), or None, with the newer runs passed over."""

    issue_time: datetime
    valid_time: datetime
    file: HindcastFile | None
    fh_lo: int | None = None
    fh_hi: int | None = None
    weight_hi: float | None = None
    available: datetime | None = None
    availability_source: str | None = None
    #: Runs newer than the chosen one (or every run, when none was chosen), with why each was
    #: passed over, newest first.
    passed_over: tuple[tuple[str, str], ...] = ()
    #: The availability rule the choice was made under (:data:`AVAILABILITY_RULES`).
    rule: str = DEFAULT_AVAILABILITY_RULE

    def __post_init__(self) -> None:
        if self.rule not in AVAILABILITY_RULES:
            raise ValueError(f"availability rule must be one of {sorted(AVAILABILITY_RULES)}")
        if self.file is not None and (self.available is None or self.available > self.issue_time):
            raise LeakageError(
                f"forecast run {self.file.run_id} was available at "
                f"{None if self.available is None else isoformat_z(self.available)}, after the "
                f"issue time {isoformat_z(self.issue_time)}"
            )

    @property
    def run_id(self) -> str | None:
        """The chosen run (``YYYYMMDDHH``), or None."""
        return None if self.file is None else self.file.run_id

    def summary(self) -> dict[str, Any]:
        """The rule, run and availability in one line (the table's other-rule listing)."""
        return {
            "availability_rule": self.rule,
            "run": self.run_id,
            "fh": None if self.file is None else [self.fh_lo, self.fh_hi],
            "available_utc": None if self.available is None else isoformat_z(self.available),
            "availability_source": self.availability_source,
        }

    def to_record(self) -> dict[str, Any] | None:
        """JSON-ready form, or None when no file serves this pair."""
        if self.file is None or self.available is None:
            return None
        run = self.file.run

        def hour_record(fh: int) -> dict[str, Any]:
            out: dict[str, Any] = {
                name: run.hour(fh).get(name)
                for name in (
                    "fh",
                    "valid_time_utc",
                    "availability_utc",
                    "availability_source",
                    "availability_model",
                    "grib_last_modified_utc",
                    "idx_last_modified_utc",
                )
            }
            for name in AVAILABILITY_RULES:
                value = self.file.hour_available(fh, name) if self.file is not None else None
                out[f"available_under_{name.replace('-', '_')}"] = (
                    value
                    if value is None or isinstance(value, str)
                    else {"utc": isoformat_z(value[0]), "source": value[1]}
                )
            return out

        return {
            "path": str(self.file.path),
            "sha256": self.file.sha256,
            "run": self.file.run_id,
            "run_start_utc": run.run_start_utc,
            "live": run.doc.get("live"),
            "fh_lo": self.fh_lo,
            "fh_hi": self.fh_hi,
            "weight_hi": self.weight_hi,
            "availability_rule": self.rule,
            "available_utc": isoformat_z(self.available),
            "availability_source": self.availability_source,
            "hours": [
                hour_record(fh) for fh in sorted({int(self.fh_lo or 0), int(self.fh_hi or 0)})
            ],
            "lead_from_availability_h": round(
                (self.valid_time - self.available).total_seconds() / 3600.0, 6
            ),
            "lead_from_run_start_h": round(
                (self.valid_time - self.file.run_start).total_seconds() / 3600.0, 6
            ),
            "available_before_issue_by_h": round(
                (self.issue_time - self.available).total_seconds() / 3600.0, 6
            ),
            "newer_runs_passed_over": [list(item) for item in self.passed_over],
        }


class HindcastCatalogue:
    """The hindcast run files of one viewpoint (see the module docstring)."""

    def __init__(
        self,
        folder: Path,
        viewpoint_id: str,
        files: Sequence[HindcastFile],
        problems: Sequence[str] = (),
        *,
        rule: str = DEFAULT_AVAILABILITY_RULE,
    ) -> None:
        if rule not in AVAILABILITY_RULES:
            raise ValueError(f"availability rule must be one of {sorted(AVAILABILITY_RULES)}")
        self.folder = folder
        self.viewpoint_id = viewpoint_id
        #: The rule :meth:`choose` applies unless told another.
        self.rule = rule
        by_run: dict[str, HindcastFile] = {}
        for item in files:
            if item.run_id in by_run:
                raise ValueError(
                    f"two hindcast files hold run {item.run_id}: {by_run[item.run_id].path.name} "
                    f"and {item.path.name}; the catalogue is ambiguous"
                )
            by_run[item.run_id] = item
        self.files: tuple[HindcastFile, ...] = tuple(
            sorted(by_run.values(), key=lambda f: f.run_start, reverse=True)
        )
        self.problems: tuple[str, ...] = tuple(problems)

    @classmethod
    def scan(
        cls,
        folder: Path,
        *,
        viewpoint_id: str,
        roots: Iterable[Path | None] = (),
        rule: str = DEFAULT_AVAILABILITY_RULE,
    ) -> HindcastCatalogue:
        """Catalogue every ``*.json`` run file in ``folder`` (an absent folder is empty).

        ``rule`` is the availability rule :meth:`choose` applies by default; every file's
        hours are dated under both rules, so a choice under the other can be listed beside it.
        """
        roots = tuple(roots)
        reason = path_quarantine_reason(folder, roots=roots)
        if reason is not None:
            raise LeakageError(f"hindcast folder {folder}: {reason}")
        if not folder.is_dir():
            return cls(folder, viewpoint_id, (), (f"no hindcast folder {folder}",), rule=rule)
        files: list[HindcastFile] = []
        problems: list[str] = []
        for path in sorted(folder.glob("*.json")):
            reason = path_quarantine_reason(path, roots=roots)
            if reason is not None:
                raise LeakageError(f"hindcast file {path}: {reason}")
            try:
                run = ForecastRun.load(path)
            except (OSError, ValueError) as exc:  # JSON and ForecastFileError are ValueErrors
                problems.append(f"{path.name}: not read ({type(exc).__name__}: {exc})"[:400])
                continue
            if run.viewpoint_id != viewpoint_id:
                problems.append(f"{path.name}: for {run.viewpoint_id}, not {viewpoint_id}")
                continue
            try:
                unusable = _unusable_hours(run)
                by_rule = _availability_by_rule(run)  # needs a readable run start
            except (KeyError, TypeError, ValueError) as exc:
                problems.append(f"{path.name}: unreadable hours ({type(exc).__name__}: {exc})")
                continue
            for fh, why in sorted(unusable.items()):
                problems.append(f"{path.name}: f{fh:02d} unusable: {why}")
            for name, hours in sorted(by_rule.items()):
                for fh, value in sorted(hours.items()):
                    if isinstance(value, str) and fh not in unusable:
                        problems.append(f"{path.name}: f{fh:02d} unusable under {name}: {value}")
            files.append(HindcastFile(path, str(run.sha256), run, unusable, by_rule))
        return cls(folder, viewpoint_id, files, problems, rule=rule)

    def choose(
        self, issue_time: datetime, valid_time: datetime, *, rule: str | None = None
    ) -> ForecastChoice:
        """The newest run whose bracketing hours were available by ``issue_time``.

        Under ``rule`` (:data:`AVAILABILITY_RULES`; the catalogue's own rule by default).
        """
        chosen_rule = self.rule if rule is None else rule
        if chosen_rule not in AVAILABILITY_RULES:
            raise ValueError(f"availability rule must be one of {sorted(AVAILABILITY_RULES)}")
        for moment, what in ((issue_time, "issue_time"), (valid_time, "valid_time")):
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError(f"{what} must be timezone-aware")
        if valid_time < issue_time:
            raise ValueError("valid_time is before issue_time")
        passed: list[tuple[str, str]] = []
        for item in self.files:  # newest run first
            if item.run_start > valid_time:
                passed.append((item.run_id, "starts after the valid time"))
                continue
            try:
                fh_lo, fh_hi, weight = item.run.bracket(valid_time)
            except ForecastFileError as exc:
                passed.append((item.run_id, str(exc)))
                continue
            needed = sorted({fh_lo, fh_hi})
            bad = [fh for fh in needed if fh in item.unusable_hours]
            if bad:
                passed.append(
                    (item.run_id, f"f{bad[0]:02d} unusable: {item.unusable_hours[bad[0]]}")
                )
                continue
            dated = [(fh, item.hour_available(fh, chosen_rule)) for fh in needed]
            undated = [(fh, why) for fh, why in dated if isinstance(why, str)]
            if undated:
                fh, why = undated[0]
                passed.append((item.run_id, f"f{fh:02d} unusable under {chosen_rule}: {why}"))
                continue
            times = [value for _, value in dated if not isinstance(value, str)]
            available, source = max(times, key=lambda value: value[0])
            if available > issue_time:
                passed.append(
                    (
                        item.run_id,
                        f"available {isoformat_z(available)} ({chosen_rule}), after the issue time",
                    )
                )
                continue
            return ForecastChoice(
                issue_time,
                valid_time,
                item,
                fh_lo,
                fh_hi,
                weight,
                available,
                source,
                tuple(passed),
                chosen_rule,
            )
        return ForecastChoice(
            issue_time, valid_time, None, passed_over=tuple(passed), rule=chosen_rule
        )

    def to_record(self) -> dict[str, Any]:
        """The catalogue's provenance: folder, files (name, SHA-256, run, hours) and problems."""
        return {
            "folder": str(self.folder),
            "viewpoint_id": self.viewpoint_id,
            "availability_rule": self.rule,
            "availability_rules": dict(AVAILABILITY_RULES),
            "n_files": len(self.files),
            "files": [f.to_record() for f in self.files],
            "problems": list(self.problems),
        }
