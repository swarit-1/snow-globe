"""The dev P1 table's baselines for one frame (PRD §13.6, §13.1; D-036).

Every baseline is :mod:`.baselines`'s, unchanged, and reads frames only through the leakage
guard (:class:`.leakage.FrameStore`), so none can see a frame imaged or fetched after the issue
time:

- **persistence (a)**, the latest daytime frame with a usable class available at the issue
  time (:func:`.baselines.persistence_latest`);
- **persistence (b)**, the frame at valid − 24 h from frames available at the issue time, or at
  valid − 24·k h beyond a 24 h lead, as D-036 implements it
  (:func:`.baselines.persistence_valid_minus_24h`, ±30 min);
- **climatology**, the visible frequency for the valid time's month and hour (local standard
  time), fitted on development frames imaged and available by the issue time and before the
  fit bound (:func:`.baselines.climatology`; falls back to the hour, then the overall rate,
  when a bin holds fewer than ``min_count`` frames; the yes/no cut is the fitted base rate);
- **S_rule**, "visible" when HRRR's ceiling stays above the camera-to-summit ray and low +
  mid cover is under 50 % (:func:`.baselines.s_rule`, :func:`.forecast_io.summit_ray`), from
  the same forecast run the render uses, at the same issue time;
- **S_mos** is fitted at the pre-registration freeze: not computed here (TODO(M2)).

The store's labels are the extractor's classes of the real frames (:mod:`.p1dev_extract`):
``visible``, ``hidden`` and ``in_cloud`` can make a forecast (in-cloud as "not visible");
``partial``, ``camera_fault``, ``excluded``, ``no_reference`` and ``night`` never do. On the
first capture day nothing is available yet, so persistence and climatology have no forecast
there (``no-usable-frame-before-issue``, ``no-development-frames``).

**Labels as of the issue time** (:class:`AsOfLabelStore`). A frame's class is itself a product
of other frames: the extractor's clear-day references. Classes made once over the whole run let
a frame from before the issue time carry a class computed against frames imaged after it, the
valid frame among them, and so let the outcome reach the baseline. So the table's store makes
its labels **per issue time**: at issue time *t* every frame is labelled by the extractor with
references imaged and available by *t* (:meth:`.p1dev_extract.P1Extractor.readings_as_of`),
and the leakage guard then hands out only frames imaged and available by *t*. The same store
serves the persistence choice and climatology.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from snow_globe_common.manifest import Landmark, Viewpoint

from .baselines import (
    DevCase,
    PersistenceChoice,
    Prediction,
    choose_persistence,
    climatology,
    persistence_latest,
    persistence_valid_minus_24h,
    s_rule,
)
from .forecast_io import LOW_CLOUD, MID_CLOUD, ForecastFileError, ForecastRun, summit_ray
from .frames import FrameRecord
from .leakage import Access, FitView, FrameStore, IssueTimeView, LeakageError

__all__ = [
    "BASELINES",
    "AsOfLabelStore",
    "baselines_for",
    "label_store",
    "s_rule_for",
    "stronger_persistence",
]

#: The baselines the table reports, in its order (S_mos is TODO(M2)).
BASELINES: tuple[str, ...] = ("persistence_a", "persistence_b", "climatology", "s_rule")


def label_store(
    records: Iterable[FrameRecord],
    classes: Mapping[str, str | None],
    *,
    label: str,
    record_accesses: bool = False,
) -> FrameStore:
    """A leakage-guarded store of ``records``, each labelled with its extractor class.

    Only records with a class are kept, so the store is exactly the frames the extractor
    classified. The store refuses quarantined records and labels on held-out frames itself.
    ``record_accesses`` logs every read (the leakage audit in the tests).
    """
    kept = [r for r in records if r.key in classes]
    return FrameStore(
        kept,
        labels={r.key: {label: classes[r.key]} for r in kept if classes[r.key] is not None},
        record_accesses=record_accesses,
    )


def _aware(moment: datetime, what: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return moment


class AsOfLabelStore(FrameStore):
    """A leakage-guarded store whose labels are made as of each issue time (PRD §13.1).

    ``labels_at(t)`` returns the class of every frame as a baseline issued at ``t`` may know it
    (the extractor with references available by ``t``, :mod:`.p1dev_extract`). Each view is
    served by a :class:`.leakage.FrameStore` labelled at its own time, so every rule of the
    guard still applies (frames imaged and available by ``t``; nothing held out). This store
    itself carries no labels: a reader that bypasses :meth:`as_of` and :meth:`for_fit` sees none,
    and a fit with no ``until`` is refused, since its labels would have no time.
    """

    def __init__(
        self,
        records: Iterable[FrameRecord],
        labels_at: Callable[[datetime], Mapping[str, str | None]],
        *,
        label: str,
        record_accesses: bool = False,
    ) -> None:
        self._all = list(records)
        super().__init__(self._all, record_accesses=record_accesses)
        self._labels_at = labels_at
        self.label_name = label
        self._at: dict[datetime, FrameStore] = {}

    def at(self, moment: datetime) -> FrameStore:
        """The store labelled as of ``moment`` (made once per moment)."""
        _aware(moment, "moment")
        store = self._at.get(moment)
        if store is None:
            classes = self._labels_at(moment)
            keys = {r.key for r in self._all}
            unknown = sorted(set(classes) - keys)
            if unknown:
                raise ValueError(f"labels for {len(unknown)} frames not in the store: {unknown[0]}")
            # every record stays (the views cut at the moment); only classified frames carry
            # a label, and the guard refuses labels on held-out frames
            store = FrameStore(
                self._all,
                labels={k: {self.label_name: v} for k, v in classes.items() if v is not None},
                record_accesses=self.record_accesses,
            )
            self._at[moment] = store
        return store

    def as_of(
        self, issue_time: datetime, *, purpose: str, allow_held_out: bool = False
    ) -> IssueTimeView:
        """A view for a prediction issued at ``issue_time``, with labels as of that time."""
        return self.at(issue_time).as_of(issue_time, purpose=purpose, allow_held_out=allow_held_out)

    def for_fit(
        self, *, purpose: str, until: datetime | None = None, design: str | None = None
    ) -> FitView:
        """A fit view up to ``until``, labelled as of ``until`` (which is required)."""
        if until is None:
            raise LeakageError(
                f"fit '{purpose}': this store labels frames as of an issue time; give until"
            )
        return self.at(until).for_fit(purpose=purpose, until=until, design=design)

    @property
    def accesses(self) -> tuple[Access, ...]:
        """Logged accesses of every per-time store (empty unless ``record_accesses``)."""
        return tuple(a for store in self._at.values() for a in store.accesses)

    def clear_log(self) -> None:
        """Forget logged accesses."""
        for store in self._at.values():
            store.clear_log()


def s_rule_for(
    *,
    run: ForecastRun,
    viewpoint: Viewpoint,
    landmark: Landmark,
    issue_time: datetime,
    valid_time: datetime,
    available: datetime,
    camera_height_m_msl: float,
) -> Prediction:
    """S_rule from one forecast run, as the backtest pair computes it (no forecast on a gap)."""
    try:
        samples, ray = summit_ray(
            run, viewpoint, landmark, valid_time, camera_height_m_msl=camera_height_m_msl
        )
        point = run.landmark_point(landmark)["id"]
        low = run.reading(LOW_CLOUD, point, valid_time).value
        mid = run.reading(MID_CLOUD, point, valid_time).value
    except ForecastFileError as exc:
        return Prediction(
            "s_rule",
            viewpoint.id,
            issue_time,
            valid_time,
            None,
            flags=("forecast-field-unavailable",),
            detail={"reason": str(exc)},
        )
    prediction = s_rule(
        viewpoint_id=viewpoint.id,
        issue_time=issue_time,
        valid_time=valid_time,
        forecast_available_time=available,
        ray_samples=samples,
        summit_height_m_msl=landmark.height_m,
        low_cover_pct=low,
        mid_cover_pct=mid,
    )
    return replace(prediction, detail={**prediction.detail, "ray": ray})


def baselines_for(
    store: FrameStore,
    *,
    viewpoint_id: str,
    time_zone: str,
    issue_time: datetime,
    valid_time: datetime,
    label: str,
    tolerance: timedelta = timedelta(minutes=30),
    climatology_min_count: int = 10,
    s_rule_prediction: Prediction | None = None,
) -> dict[str, Prediction]:
    """Persistence (a), (b) and climatology for one (issue, valid), plus S_rule if given.

    Without a forecast, S_rule is a no-forecast prediction flagged ``no-forecast-run``.
    """
    view = store.as_of(issue_time, purpose="p1dev-persistence")
    out = {
        "persistence_a": persistence_latest(view, viewpoint_id, valid_time=valid_time, label=label),
        "persistence_b": persistence_valid_minus_24h(
            view, viewpoint_id, valid_time=valid_time, label=label, tolerance=tolerance
        ),
        "climatology": climatology(
            store,
            viewpoint_id,
            issue_time=issue_time,
            valid_time=valid_time,
            time_zone=time_zone,
            label=label,
            min_count=climatology_min_count,
        ),
    }
    out["s_rule"] = s_rule_prediction or Prediction(
        "s_rule", viewpoint_id, issue_time, valid_time, None, flags=("no-forecast-run",)
    )
    return out


def stronger_persistence(
    store: FrameStore,
    cases: Sequence[DevCase],
    *,
    label: str,
    tolerance: timedelta = timedelta(minutes=30),
) -> tuple[PersistenceChoice, dict[str, Any]]:
    """:func:`.baselines.choose_persistence` on ``cases`` (one lead), with a JSON-ready record."""
    choice = choose_persistence(store, cases, label=label, tolerance=tolerance)
    return choice, choice.to_dict()
