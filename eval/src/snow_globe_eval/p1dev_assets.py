"""The dev P1 table's fitted assets, checked by the backtest pair's rule (PRD §13.1, §13.9).

The one-pair path (:mod:`.backtest_pair`, ``_check_fitted_asset``) refuses a plate or a
clear-day reference that a forecast issued at the issue time could not have used:

- taken at or after the issue time, or available only after it (§13.1: nothing may postdate
  the issue time);
- taken on the valid time's local day (it shares the valid day's weather);
- held out, or at or after the viewpoint's fit bound (§13.9);

and it refuses a pair whose truth frame is the plate. The dev table renders every frame over
plate v0.1, the plate its brief names, and measures truth and render against extractor v0's
clear-day pool, so for most frames these assets postdate the issue time. The table therefore
does not refuse them per frame; it **records** the check for every frame
(:func:`fitted_asset_check`: each plate input frame, base, geometry or stars, and each
clear-day reference of the truth and the render), **excludes from P1** a truth frame that is
one of the plate's own input frames (the render is drawn over a median of it, or masked by a
sky segmented from it), and the table adds sensitivity blocks restricted to the frames whose
assets pass (:mod:`.p1dev_table`). Whether the dev table may use assets dated after the issue
time is the lead's ruling; until then the primary uses them and says so.

PRD §13.9's feature admission fits on dev-fit weeks and checks on dev-check weeks, so each
check also names the dev-week roles of the assets (:mod:`.p1dev_weeks`): a dev-check frame
measured with an asset from a dev-check week is counted by the table. Restricting dev-check
frames to dev-fit assets is TODO(M2), when dev-check frames exist.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from snow_globe_common.timeutil import isoformat_z, parse_iso_utc

from .frames import FrameRecord
from .heldout import HeldOutSplit
from .p1dev_weeks import DEV_CHECK, dev_week_of_time

__all__ = ["RULE", "PlateInput", "fitted_asset_check", "plate_inputs"]

RULE = (
    "backtest_pair._check_fitted_asset: a plate input frame or clear-day reference must be "
    "taken before the issue time and available by then, not on the valid time's local day, "
    "not held out and before the fit bound; the truth frame must not be one of the plate's "
    "input frames"
)


@dataclass(frozen=True)
class PlateInput:
    """One frame a plate was made from, with its role (``base``, geometry, stars)."""

    key: str
    role: str
    image_time: datetime | None


def plate_inputs(plate_doc: Mapping[str, Any]) -> list[PlateInput]:
    """Every frame ``plate.json`` names as an input (``inputs.frames``, else ``base.frames``)."""
    out: dict[str, PlateInput] = {}
    for item in (plate_doc.get("inputs") or {}).get("frames") or []:
        key = item.get("key") or item.get("frame")
        if not key:
            continue
        when = item.get("image_time_utc")
        out[str(key)] = PlateInput(
            str(key), str(item.get("role") or "input"), parse_iso_utc(when) if when else None
        )
    for item in (plate_doc.get("base") or {}).get("frames") or []:
        key = item.get("frame") or item.get("key")
        if key and str(key) not in out:
            out[str(key)] = PlateInput(str(key), "base", None)
    return sorted(out.values(), key=lambda p: p.key)


def _problems(
    key: str,
    image_time: datetime | None,
    record: FrameRecord | None,
    *,
    issue: datetime,
    valid_day: str,
    zone: ZoneInfo,
    split: HeldOutSplit,
    fit_bound: datetime | None,
) -> list[str]:
    """Why one asset frame fails the backtest pair's rule (empty when it passes)."""
    when = record.image_time if record is not None else image_time
    if when is None:
        return [f"{key}: no frame record and no image time"]
    out = []
    if when >= issue:
        out.append("taken at or after the issue time")
    elif record is None:
        out.append("no frame record: its availability is unknown")
    elif record.available_at > issue:
        out.append(f"available only at {isoformat_z(record.available_at)}, after the issue time")
    if when.astimezone(zone).date().isoformat() == valid_day:
        out.append("taken on the valid time's local day")
    if record is not None:
        held = split.held_out_reason(record)
        if held is not None:
            out.append(f"held out ({held})")
    if fit_bound is not None and when >= fit_bound:
        out.append(f"not before the fit bound {isoformat_z(fit_bound)} (PRD §13.9)")
    return out


def _role(moment: datetime | None, time_zone: str) -> str | None:
    return None if moment is None else str(dev_week_of_time(moment, time_zone)["role"])


def fitted_asset_check(
    *,
    truth_key: str,
    issue: datetime,
    valid: datetime,
    time_zone: str,
    viewpoint_id: str,
    plate: Sequence[PlateInput],
    references: Iterable[str],
    records: Mapping[str, FrameRecord],
    split: HeldOutSplit,
) -> dict[str, Any]:
    """One frame's fitted assets under :data:`RULE`, as a JSON-ready dict (module docstring).

    ``references`` are the clear-day frames the truth and the render were measured against;
    ``records`` give each frame's image and availability times (metadata only).
    """
    zone = ZoneInfo(time_zone)
    valid_day = valid.astimezone(zone).date().isoformat()
    fit_bound = split.fit_bound(viewpoint_id)
    common = {
        "issue": issue,
        "valid_day": valid_day,
        "zone": zone,
        "split": split,
        "fit_bound": fit_bound,
    }
    plate_failing = []
    plate_roles: set[str] = set()
    for item in plate:
        record = records.get(item.key)
        why = _problems(item.key, item.image_time, record, **common)
        when = record.image_time if record is not None else item.image_time
        role = _role(when, time_zone)
        if role is not None:
            plate_roles.add(role)
        if why:
            plate_failing.append(
                {
                    "key": item.key,
                    "role": item.role,
                    "image_time_utc": None if when is None else isoformat_z(when),
                    "why": why,
                }
            )
    ref_keys = sorted(set(references))
    ref_failing = []
    ref_roles: set[str] = set()
    for key in ref_keys:
        record = records.get(key)
        why = _problems(key, None, record, **common)
        role = _role(None if record is None else record.image_time, time_zone)
        if role is not None:
            ref_roles.add(role)
        if why:
            ref_failing.append(
                {
                    "key": key,
                    "image_time_utc": None if record is None else isoformat_z(record.image_time),
                    "why": why,
                }
            )
    truth_role = next((item.role for item in plate if item.key == truth_key), None)

    def count(failing: Sequence[Mapping[str, Any]], text: str) -> int:
        return sum(any(text in why for why in item["why"]) for item in failing)

    return {
        "rule": RULE,
        "truth_is_plate_input": truth_role,
        "plate": {
            "inputs": len(plate),
            "passes": not plate_failing,
            "failing": plate_failing,
            "at_or_after_issue": count(plate_failing, "at or after the issue time"),
            "base_at_or_after_issue": sum(
                item["role"] == "base" and "taken at or after the issue time" in item["why"]
                for item in plate_failing
            ),
            "on_valid_local_day": count(plate_failing, "valid time's local day"),
            "base_on_valid_local_day": sum(
                item["role"] == "base" and "taken on the valid time's local day" in item["why"]
                for item in plate_failing
            ),
            "dev_week_roles": sorted(plate_roles),
        },
        "references": {
            "n": len(ref_keys),
            "passes": not ref_failing,
            "failing": ref_failing,
            "at_or_after_issue": count(ref_failing, "at or after the issue time"),
            "dev_week_roles": sorted(ref_roles),
        },
        "uses_dev_check_week_asset": DEV_CHECK in plate_roles | ref_roles,
        "plate_off_valid_day": count(plate_failing, "valid time's local day") == 0,
        "passes": not plate_failing and not ref_failing and truth_role is None,
    }
