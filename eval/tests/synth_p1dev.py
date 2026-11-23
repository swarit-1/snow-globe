"""Synthetic hindcast run files in the live field profile, for the dev P1 table's tests.

The hindcast extracts the live worker's fields (D-050), not the M0.5 pair's: no cloud top,
precipitation or 10 m wind. :func:`live_doc` builds :func:`synth_forecast.make_doc`'s document
and keeps only those fields, so the tests read what the real hindcast files hold.

Like a real hindcast file, each hour records its S3 stamps as ``availability_utc`` and the lag
model's prediction in ``availability_model`` (``lag_s``, ``s3_floor_utc``). By default the
model equals the stamps, so both availability rules agree; ``model_lag_s`` sets another model
(the real files of 23 Nov hold 7,200 s, the conservative fallback, later than every stamp),
and ``record_model=False`` records none.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from snow_globe_common.manifest import Viewpoint
from synth_forecast import Values, make_doc


def _z(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


#: The scene fields of the live profile that synth_forecast knows (PRES:cloud base and
#: HGT:surface are in the live files too; the scene reads neither).
LIVE_FIELDS: tuple[str, ...] = (
    "VIS:surface",
    "HGT:cloud ceiling",
    "HGT:cloud base",
    "LCDC:low cloud layer",
    "MCDC:middle cloud layer",
    "HCDC:high cloud layer",
)


def live_doc(
    vp: Viewpoint,
    run_start: datetime,
    hours: list[int],
    values: Values,
    *,
    availability: Callable[[int], datetime] | None = None,
    source: str = "archived-s3-lastmodified",
    model_lag_s: Callable[[int], float] | None = None,
    record_model: bool = True,
) -> dict[str, Any]:
    """A run document with only :data:`LIVE_FIELDS` (as the hindcast writes them)."""
    avail = availability or (lambda fh: run_start + timedelta(minutes=50 + 2 * fh))
    doc = make_doc(vp, run_start, hours, values, availability=avail, source=source)
    keep = [i for i, name in enumerate(doc["field_names"]) if name in LIVE_FIELDS]
    doc["field_names"] = [doc["field_names"][i] for i in keep]
    doc["values"] = [doc["values"][i] for i in keep]
    doc["fields"] = {name: doc["fields"][name] for name in doc["field_names"]}
    for hour in doc["hours"]:
        if not record_model:
            hour["availability_model"] = None
            continue
        # the stamp as the file records it (whole seconds), so the default model equals it
        stamp = datetime.fromisoformat(hour["availability_utc"].replace("Z", "+00:00"))
        lag = (
            (stamp - run_start).total_seconds()
            if model_lag_s is None
            else float(model_lag_s(int(hour["fh"])))
        )
        hour["availability_model"] = {
            "method": "synthetic-equal-to-stamps"
            if model_lag_s is None
            else "conservative-fallback",
            "lag_s": lag,
            "samples": 0,
            "min_samples": 20,
            "s3_floor_utc": _z(stamp),
            "s3_stamps_ignored": [],
        }
    return doc


def write_run(folder: Path, doc: dict[str, Any]) -> Path:
    """Write a run document as ``<run>z.json`` in ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{doc['run_id']}z.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path
