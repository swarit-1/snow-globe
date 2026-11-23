"""The dev P1 table's extractor readings as of an issue time (PRD §13.1, §13.3).

Synthetic band energies and Sun positions (no pixels): a baseline's label must never depend on
a frame imaged or made available after the issue time, the valid frame least of all.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from snow_globe_eval.baselines import Prediction
from snow_globe_eval.extractor_v0 import BandEnergies, ReferenceFrameV0, V0Params
from snow_globe_eval.frames import FrameRecord
from snow_globe_eval.leakage import FrameStore
from snow_globe_eval.p1dev_baselines import AsOfLabelStore, baselines_for
from snow_globe_eval.p1dev_extract import (
    EXTRACTORS,
    POOL_INDEPENDENT_CLASSES,
    Reading,
    V0Extractor,
    readings_as_of_v0,
)

VP = "rainier.paradise-mountain"
ZONE = "America/Los_Angeles"
ORDER = ("summit", "near")
PARAMS = V0Params()


def _energies(summit: float, near: float = 1.0) -> BandEnergies:
    return BandEnergies(
        bands={"summit": summit, "near": near},
        global_energy=1.0,
        rms_contrast=0.5,
        dark_fraction=0.0,
        bright_fraction=0.0,
        luma_range=0.8,
        analysis_size=(960, 540),
    )


def _key(t: datetime) -> str:
    return f"frames/{VP}/{t:%Y-%m-%d}/{t:%Y%m%dT%H%M%SZ}_{'0' * 12}.jpg"


def _local_day(t: datetime) -> str:
    return (t - timedelta(hours=8)).date().isoformat()  # PST, late November


class World:
    """Frames with stored run results, a clear-day pool and availability times."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.energies: dict[str, BandEnergies] = {}
        self.sun: dict[str, tuple[float, float]] = {}
        self.pool: list[ReferenceFrameV0] = []
        self.available: dict[str, datetime] = {}
        self.stored: dict[str, Reading] = {}
        self.times: dict[str, datetime] = {}

    def add(
        self,
        t: datetime,
        summit: float,
        *,
        frame_class: str = "scored",
        pool: bool = False,
        delay: timedelta = timedelta(seconds=30),
        sun: tuple[float, float] = (20.0, 160.0),
    ) -> str:
        key = _key(t)
        self.records[key] = {
            "image_time_utc": t.isoformat().replace("+00:00", "Z"),
            "local_day": _local_day(t),
            "frame_class": frame_class,
        }
        self.energies[key] = _energies(summit)
        self.sun[key] = sun
        self.available[key] = t + delay
        self.times[key] = t
        self.stored[key] = Reading("v0", frame_class, frame_class, None, None)
        if pool:
            self.pool.append(
                ReferenceFrameV0(
                    key, "0" * 64, t, sun[0], sun[1], _local_day(t), self.energies[key]
                )
            )
        return key

    def as_of(self, moment: datetime) -> dict[str, Reading]:
        return readings_as_of_v0(
            self.records,
            self.energies,
            self.sun,
            self.pool,
            moment=moment,
            available=self.available,
            params=PARAMS,
            order=ORDER,
            band="summit",
            stored=self.stored,
        )

    def frame_records(self) -> list[FrameRecord]:
        return [
            FrameRecord(VP, self.times[k], k, "live", None, self.available[k])
            for k in sorted(self.records, key=lambda k: self.times[k])
        ]


def test_the_valid_frame_never_labels_an_earlier_frame_for_a_baseline() -> None:
    """The review's fixture: an earlier frame's only clear-day reference is the valid frame."""
    world = World()
    earlier = world.add(datetime(2026, 11, 20, 17, 59, tzinfo=UTC), 0.9)
    valid_t = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)
    valid = world.add(valid_t, 1.0, pool=True)  # the valid frame, labelled clear day
    issue = valid_t - timedelta(hours=24)
    # the run's own (whole-pool) reading: the earlier frame is visible against the valid frame
    leaky = world.as_of(valid_t + timedelta(days=1))[earlier]
    assert leaky.p1_class == "visible" and leaky.references == (valid,)
    # as of the issue time the valid frame does not exist yet: no reference, no class
    honest = world.as_of(issue)
    assert set(honest) == {earlier}
    assert honest[earlier].frame_class == "no_reference" and honest[earlier].references == ()

    # through the baselines: the leaky labels made a persistence forecast, the as-of labels none
    records = world.frame_records()
    leaky_store = FrameStore(records, labels={earlier: {"summit": "visible"}})
    store = AsOfLabelStore(
        records,
        lambda t: {k: r.p1_class for k, r in world.as_of(t).items()},
        label="summit",
        record_accesses=True,
    )
    kwargs: dict[str, Any] = {
        "viewpoint_id": VP,
        "time_zone": ZONE,
        "issue_time": issue,
        "valid_time": valid_t,
        "label": "summit",
    }
    assert baselines_for(leaky_store, **kwargs)["persistence_a"].value is True
    out: dict[str, Prediction] = baselines_for(store, **kwargs)
    assert out["persistence_a"].value is None
    assert out["persistence_a"].flags == ("no-usable-frame-before-issue",)
    assert out["persistence_b"].value is None and out["climatology"].value is None
    assert all(a.image_time <= issue for a in store.accesses)
    view = store.as_of(issue, purpose="test")
    assert view.label(records[0], "summit") == "no_reference"  # the label as of the issue
    with pytest.raises(Exception, match="give until"):
        store.for_fit(purpose="test")  # labels without a time are refused


def test_nothing_after_the_moment_changes_a_reading_as_of_it() -> None:
    """Property: readings as of t ignore every frame and pool frame imaged or fetched after t."""
    rng = random.Random(7)
    start = datetime(2026, 11, 19, 16, tzinfo=UTC)

    def build(after: datetime | None, seed: int) -> World:
        local = random.Random(seed)
        world = World()
        for day in range(5):
            for step in range(16):
                t = start + timedelta(days=day, minutes=30 * step)
                energy = local.uniform(0.1, 1.2)
                if after is not None and t > after:
                    energy = local.uniform(0.0, 3.0)  # the future, scrambled
                world.add(
                    t,
                    energy,
                    pool=local.random() < 0.3,
                    sun=(18.0 + 0.2 * step, 150.0 + 2.0 * step),
                    delay=timedelta(minutes=local.choice([0, 1, 40])),
                )
        return world

    checked = changed_future = 0
    for _ in range(25):
        moment = start + timedelta(minutes=rng.uniform(0, 5 * 24 * 60))
        world = build(None, 3)
        base = world.as_of(moment)
        assert base == build(moment, 3).as_of(moment)
        checked += len(base)
        future = world.as_of(moment + timedelta(days=3))
        changed_future += sum(future[k] != base[k] for k in base)
        for key, reading in base.items():
            assert world.times[key] <= moment and world.available[key] <= moment
            for ref in reading.references:
                assert world.times[ref] <= moment and world.available[ref] <= moment
    assert checked > 300 and changed_future > 0  # the property is not vacuous


def test_unknown_availability_fails_closed_and_pool_free_classes_are_kept() -> None:
    world = World()
    t = datetime(2026, 11, 20, 18, tzinfo=UTC)
    ref = world.add(t - timedelta(days=1), 1.0, pool=True)
    night = world.add(t - timedelta(hours=12), 0.1, frame_class="night")
    fault = world.add(t - timedelta(minutes=30), 0.0, frame_class="camera_fault")
    frame = world.add(t, 0.8)
    assert set(POOL_INDEPENDENT_CLASSES) >= {"night", "camera_fault", "excluded"}
    moment = t + timedelta(minutes=5)
    readings = world.as_of(moment)
    assert readings[night] is world.stored[night] and readings[fault] is world.stored[fault]
    assert readings[frame].p1_class == "visible" and readings[frame].references == (ref,)
    del world.available[ref]  # a pool frame whose availability is unknown is never used
    assert world.as_of(moment)[frame].frame_class == "no_reference"
    del world.available[frame]  # nor is a frame without one labelled
    assert frame not in world.as_of(moment)
    with pytest.raises(ValueError):
        world.as_of(datetime(2026, 11, 20, 18))  # naive


def test_extractors_are_factories_of_the_protocol() -> None:
    assert EXTRACTORS["v0"] is V0Extractor
    for name in ("readings_as_of", "reference_times", "provisional_notes", "measure"):
        assert callable(getattr(V0Extractor, name))
