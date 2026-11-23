"""Rainier labels v0.2: two blind labellers merged, and how well they agree (PRD §13.8).

PRD §13.8 asks for labelled real frames, "plus a second labeller if available", before any
extractor number is published. Labels v0.2 are two independent, blind labellings of the same
sheets (one per daytime capture of 19-22 Nov 2026, Sun ≥ 3°), made under
``<data dir>/labels/rainier.paradise-mountain/v0.2/protocol.md``:

- each labeller (``a`` and ``b``, AI labellers) read only the protocol, ``sheets.txt`` and the
  sheet images, which carry no time, date or file name; neither saw the other's file, the
  frames, ``sample.json`` (the sheet-to-frame map) or any extractor output;
- per band (summit, upper, mid; the plate marks near not valid) a state: ``visible``,
  ``partial``, ``hidden``, ``in_cloud`` or ``unusable``, plus one confidence (1-3) and a note
  per sheet.

**Merge rule** (the lead's, for extractor v0.2): where the two labellers give a band the same
state, that is its label; where they differ, the band's merged label is ``None``: the frame is
left out of that band's scoring and counted, and a sensitivity analysis scores it both ways
(:mod:`.extractor_v0_2_lodo`).

**Agreement**, per band: raw agreement and Cohen's kappa over the five states, with the
confusion table (rows ``a``, columns ``b``), and the same for the event *visible* against
everything else.

**The committed file** ``labels_rainier_v0_2.json`` keeps eval's labels layout (``kind``,
``version``, ``viewpoint_id``, ``bands``, ``protocol``, ``frames`` with ``key``, ``sha256``,
``image_time_utc`` and ``bands``), where each frame's ``bands`` is the merged label (``None``
where the labellers differ) and ``labellers`` holds both labellings; ``format`` says so
(:data:`MERGED_FORMAT`). It is not v0.1's format: it adds the states ``in_cloud`` and
``unusable``, and has no ``camera_in_cloud``, ``sky``, ``light`` or ``clear_day_reference``.
:func:`.labels.load_labels`, which checks ``kind`` but not ``format``, cannot read it: it stops
on the first frame with ``KeyError: 'camera_in_cloud'``, not with a clear refusal (labels.py
is another package's; a check of ``format`` there would turn that into a ``ValueError``).
:func:`load_merged_labels` reads it, under the same leakage guard: a file that labels a frame
the committed split holds out (placed by its ``image_time_utc`` and by the time in its key) or
that sits under the quarantine folder is refused whole (PRD §13.1, D-022).

The committed file is fixed: the pre-registration of run 20261123-lodo1 cites it by SHA-256.
:func:`build_merged_file` writes a merged file only to a new path, with its version, dates and
frame rule taken from ``sample.json`` and the labellers' files, and the bands' and states'
definitions cited from ``protocol.md`` by SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snow_globe_common.timeutil import parse_iso_utc

from .frames import FrameRecord, parse_frame_key
from .heldout import LeakageError, committed_split, path_quarantine_reason
from .labels import LABELS_KIND, MAX_KEY_TIME_SKEW_S
from .leakage import frame_file_verdict

__all__ = [
    "BANDS_V0_2",
    "LABELLER_FIELDS",
    "LABELLER_STATES",
    "LABELS_RAINIER_V0_2",
    "MERGED_FORMAT",
    "MergedFrame",
    "MergedLabels",
    "SheetFrame",
    "agreement",
    "build_merged_file",
    "cohen_kappa",
    "load_merged_labels",
    "merge",
    "read_labeller_file",
    "read_sample",
]

LABELLER_STATES = ("visible", "partial", "hidden", "in_cloud", "unusable")
BANDS_V0_2 = ("summit", "upper", "mid")
LABELLER_FIELDS = frozenset({*BANDS_V0_2, "confidence", "note"})
MERGED_FORMAT = "two-labeller-merged-v1"
LABELS_RAINIER_V0_2 = Path(__file__).with_name("labels_rainier_v0_2.json")
SAMPLE_KIND = "snow-globe.label-sample"


@dataclass(frozen=True)
class SheetFrame:
    """One sheet of the sample: the capture it shows (from ``sample.json``)."""

    sheet: str
    key: str
    sha256: str
    image_time_utc: str
    local_day: str


def read_sample(path: str | Path) -> dict[str, SheetFrame]:
    """The sample's sheet-to-frame map (``sample.json``), keyed by sheet id."""
    doc = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if doc.get("kind") != SAMPLE_KIND:
        raise ValueError(f"{path}: not a {SAMPLE_KIND} file")
    out: dict[str, SheetFrame] = {}
    for item in doc["sheets"]:
        sheet = str(item["sheet_id"])
        if sheet in out:
            raise ValueError(f"{path}: sheet {sheet} is listed twice")
        out[sheet] = SheetFrame(
            sheet=sheet,
            key=str(item["frame_key"]),
            sha256=str(item["sha256"]),
            image_time_utc=str(item["image_time_utc"]),
            local_day=str(item["local_day"]),
        )
    keys = [s.key for s in out.values()]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{path}: a frame is on two sheets")
    return out


def read_labeller_file(path: str | Path, sheet_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """One labeller's file, checked against the protocol's format; every sheet must be there.

    The protocol's format: one JSON object whose keys are exactly the sheet ids, each value
    exactly ``summit``, ``upper``, ``mid`` (a state of :data:`LABELLER_STATES`),
    ``confidence`` (1, 2 or 3) and ``note`` (a string). Any departure raises ``ValueError``.
    """
    doc = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: not one JSON object")
    wanted = list(sheet_ids)
    missing = sorted(set(wanted) - set(doc))
    extra = sorted(set(doc) - set(wanted))
    if missing or extra:
        raise ValueError(f"{path}: missing sheets {missing[:5]}, unknown keys {extra[:5]}")
    for sheet, entry in doc.items():
        if not isinstance(entry, dict) or set(entry) != LABELLER_FIELDS:
            raise ValueError(f"{path}: {sheet} must have exactly {sorted(LABELLER_FIELDS)}")
        for band in BANDS_V0_2:
            if entry[band] not in LABELLER_STATES:
                raise ValueError(f"{path}: {sheet} {band} {entry[band]!r} is not a state")
        confidence = entry["confidence"]
        if isinstance(confidence, bool) or confidence not in (1, 2, 3):
            raise ValueError(f"{path}: {sheet} confidence {confidence!r} is not 1, 2 or 3")
        if not isinstance(entry["note"], str):
            raise ValueError(f"{path}: {sheet} note is not a string")
    return {sheet: dict(doc[sheet]) for sheet in wanted}


def cohen_kappa(
    pairs: Sequence[tuple[str, str]], categories: Sequence[str] = LABELLER_STATES
) -> dict[str, Any]:
    """Raw agreement and Cohen's kappa for paired labels, with the confusion table.

    kappa = (p_o − p_e) / (1 − p_e), where p_o is the share of pairs that agree and p_e the
    agreement expected by chance from the two labellers' own state frequencies (Cohen 1960).
    kappa is None when p_e is 1 (both labellers used one and the same state throughout) or
    there are no pairs. The table's rows are the first labeller's states, its columns the
    second's.
    """
    known = set(categories)
    for first, second in pairs:
        if first not in known or second not in known:
            raise ValueError(f"unknown state in pair ({first!r}, {second!r})")
    n = len(pairs)
    table = {r: {c: 0 for c in categories} for r in categories}
    for first, second in pairs:
        table[first][second] += 1
    agree = sum(table[c][c] for c in categories)
    if n == 0:
        return {"n": 0, "agree": 0, "raw_agreement": None, "kappa": None, "table": table}
    rows = Counter(first for first, _ in pairs)
    cols = Counter(second for _, second in pairs)
    p_o = agree / n
    p_e = sum(rows[c] * cols[c] for c in categories) / (n * n)
    kappa = None if p_e >= 1.0 else (p_o - p_e) / (1.0 - p_e)
    return {
        "n": n,
        "agree": agree,
        "raw_agreement": round(p_o, 4),
        "chance_agreement": round(p_e, 4),
        "kappa": None if kappa is None else round(kappa, 4),
        "table": table,
        "first_counts": {c: rows[c] for c in categories},
        "second_counts": {c: cols[c] for c in categories},
    }


def agreement(
    a: Mapping[str, Mapping[str, Any]],
    b: Mapping[str, Mapping[str, Any]],
    bands: Sequence[str] = BANDS_V0_2,
) -> dict[str, Any]:
    """Per band: five-state agreement and kappa, and the same for *visible* vs the rest."""
    sheets = sorted(set(a) & set(b))
    out: dict[str, Any] = {"sheets": len(sheets), "bands": {}}
    for band in bands:
        pairs = [(str(a[s][band]), str(b[s][band])) for s in sheets]
        binary = [
            (
                "visible" if x == "visible" else "not_visible",
                "visible" if y == "visible" else "not_visible",
            )
            for x, y in pairs
        ]
        out["bands"][band] = {
            "states": cohen_kappa(pairs),
            "visible_vs_rest": cohen_kappa(binary, ("visible", "not_visible")),
            "disagreements": [
                {"sheet": s, "a": x, "b": y}
                for s, (x, y) in zip(sheets, pairs, strict=True)
                if x != y
            ],
        }
    return out


def merge(
    a: Mapping[str, Mapping[str, Any]],
    b: Mapping[str, Mapping[str, Any]],
    sample: Mapping[str, SheetFrame],
    *,
    viewpoint_id: str,
    version: str,
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    """The merged labels document (the module docstring's rule), frames in key order.

    ``version`` is the labels' version, the sample's (``sample.json``'s ``version``).
    """
    if set(a) != set(sample) or set(b) != set(sample):
        raise ValueError("both labellers must label exactly the sample's sheets")
    frames = []
    for sheet in sorted(sample, key=lambda s: sample[s].key):
        meta = sample[sheet]
        merged = {
            band: (a[sheet][band] if a[sheet][band] == b[sheet][band] else None)
            for band in BANDS_V0_2
        }
        frames.append(
            {
                "key": meta.key,
                "sha256": meta.sha256,
                "image_time_utc": meta.image_time_utc,
                "sheet": sheet,
                "bands": merged,
                "labellers": {
                    "a": {f: a[sheet][f] for f in (*BANDS_V0_2, "confidence", "note")},
                    "b": {f: b[sheet][f] for f in (*BANDS_V0_2, "confidence", "note")},
                },
            }
        )
    if not version:
        raise ValueError("a merged labels file needs its version (the sample's)")
    return {
        "kind": LABELS_KIND,
        "version": version,
        "format": MERGED_FORMAT,
        "viewpoint_id": viewpoint_id,
        "bands": list(BANDS_V0_2),
        "states": list(LABELLER_STATES),
        "protocol": dict(protocol),
        "frames": frames,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _sample_meta(path: Path) -> dict[str, Any]:
    """``sample.json`` without its sheets: version, viewpoint, rule, sampling, handover."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("kind") != SAMPLE_KIND:
        raise ValueError(f"{path}: not a {SAMPLE_KIND} file")
    for field in ("version", "viewpoint_id"):
        if not doc.get(field):
            raise ValueError(f"{path}: no {field}; a merged file takes it from the sample")
    return {k: v for k, v in doc.items() if k != "sheets"}


def _mtime_utc(path: Path) -> str:
    moment = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def build_merged_file(
    labels_dir: str | Path,
    out_path: str | Path,
    *,
    data_root: Path,
    viewpoint_id: str = "rainier.paradise-mountain",
    labellers: Sequence[str] = ("a", "b"),
    descriptions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Merge ``labels-a.json`` and ``labels-b.json`` of ``labels_dir`` into ``out_path``.

    Reads the folder's ``sample.json`` (the sheet-to-frame map, the labels' version, the
    frame rule) and ``protocol.md``. Before any frame byte is read, each sheet's frame must
    pass the committed split's guard for development use (:func:`.leakage.frame_file_verdict`,
    metadata only); its SHA-256 must then match the sample's. ``out_path`` must not exist (a
    merged file is never written over, so the committed one is never rebuilt in place); the
    file is written whole or not at all. Paths are written relative to the data dir. The
    file records what the merge can know: the sample's rule, the files' SHA-256s and
    modification times, and what ``protocol.md`` and the sample's handover told each labeller
    to read, not whether they kept to it. ``descriptions`` says who or what each labeller is
    (for example "an AI labeller"); without it the file says it was not given. Returns the
    agreement report, which the file's protocol also carries.
    """
    out = Path(out_path).expanduser()
    if out.exists():
        raise ValueError(f"{out} exists; a merged labels file is never written over")
    folder = Path(labels_dir).expanduser()
    reason = path_quarantine_reason(folder, roots=(data_root,))
    if reason is not None:
        raise LeakageError(f"labels folder {folder}: {reason}")
    sample_path = folder / "sample.json"
    info = _sample_meta(sample_path)
    if info["viewpoint_id"] != viewpoint_id:
        raise ValueError(f"{sample_path} is for {info['viewpoint_id']}, not {viewpoint_id}")
    sample = read_sample(sample_path)
    files = {who: folder / f"labels-{who}.json" for who in labellers}
    first, second = (read_labeller_file(files[w], list(sample)) for w in labellers)
    for meta in sample.values():
        frame = data_root / meta.key
        verdict = frame_file_verdict(frame, data_root=data_root)
        if not verdict["development"]:
            raise LeakageError(f"{meta.key}: not a development frame: {verdict['reasons']}")
        if _sha256(frame) != meta.sha256:
            raise ValueError(f"{meta.key}: SHA-256 differs from sample.json")
    report = agreement(first, second)
    protocol_md = folder / "protocol.md"
    counts = {
        band: dict(
            Counter(
                "disagree" if first[s][band] != second[s][band] else str(first[s][band])
                for s in sample
            )
        )
        for band in BANDS_V0_2
    }
    protocol_sha = _sha256(protocol_md)
    given = (info.get("handover") or {}).get("labellers_get") or "not recorded in sample.json"
    told = descriptions or {}
    rule = info.get("rule") or {}
    local_days = sorted({s.local_day for s in sample.values()})
    frames_note = (
        f"{len(sample)} frames of {info['viewpoint_id']}, one sheet each, local days "
        f"{local_days[0]} to {local_days[-1]}"
        if local_days
        else "no frames"
    )
    for name, text in (
        ("taken", rule.get("taken")),
        ("daytime", rule.get("daytime")),
        ("sampling", (info.get("sampling") or {}).get("why")),
        ("sheet order", rule.get("order")),
    ):
        if text:
            frames_note += f"; {name}: {text}"
    protocol: dict[str, Any] = {
        "labellers": {
            who: f"{told.get(who, 'who or what this labeller is was not given to the merge')}; "
            f"protocol.md (SHA-256 {protocol_sha}) and sample.json's handover gave it only "
            f"{given}, and told it to open nothing else and to work alone. The merge cannot "
            "check that it did."
            for who in labellers
        },
        "labeller_files_modified_utc": {who: _mtime_utc(files[who]) for who in labellers},
        "sample_created_at_utc": info.get("created_at_utc"),
        "frames": frames_note,
        "protocol_md": {"path": _relative(protocol_md, data_root), "sha256": protocol_sha},
        "sample_json": {"path": _relative(sample_path, data_root), "sha256": _sha256(sample_path)},
        "labels_files": {
            who: {"path": _relative(files[who], data_root), "sha256": _sha256(files[who])}
            for who in labellers
        },
        "bands": f"{', '.join(BANDS_V0_2)}, as protocol.md (SHA-256 above) defines them",
        "states": f"{', '.join(LABELLER_STATES)}, as protocol.md (SHA-256 above) defines them",
        "merge_rule": "where labellers a and b give a band the same state, that is its label "
        "(bands); where they differ the band's label is null: the frame is left out of that "
        "band's scoring and counted, and a sensitivity analysis scores it both ways",
        "merged_counts": counts,
        "agreement": {
            band: {
                "n": item["states"]["n"],
                "agree": item["states"]["agree"],
                "raw_agreement": item["states"]["raw_agreement"],
                "kappa": item["states"]["kappa"],
                "visible_vs_rest_kappa": item["visible_vs_rest"]["kappa"],
                "table_rows_a_columns_b": item["states"]["table"],
                "disagreements": item["disagreements"],
            }
            for band, item in report["bands"].items()
        },
        "relabel_pass": None,
        "second_labeller": labellers[1],
        "labelled_renders": 0,
        "still_to_do": "TODO(M2): re-label 50 sheets a week later without reading the first "
        "file; label ~50 renders; reach ~150 frames per metric (PRD §13.8)",
    }
    doc = merge(
        first,
        second,
        sample,
        viewpoint_id=viewpoint_id,
        version=str(info["version"]),
        protocol=protocol,
    )
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if out.exists():  # appeared while the frames were checked
        tmp.unlink()
        raise ValueError(f"{out} exists; a merged labels file is never written over")
    os.replace(tmp, out)
    return report


@dataclass(frozen=True)
class MergedFrame:
    """One frame's merged labels (None where the labellers differ) and both labellings."""

    key: str
    sha256: str
    image_time_utc: str
    sheet: str
    bands: dict[str, str | None]
    labellers: dict[str, dict[str, Any]]

    def label(self, band: str, who: str = "merged") -> str | None:
        """The merged label (``who="merged"``) or one labeller's (``"a"`` or ``"b"``)."""
        if who == "merged":
            return self.bands[band]
        return str(self.labellers[who][band])


@dataclass(frozen=True)
class MergedLabels:
    """A merged labels file: protocol, bands, frames by key, and the file's SHA-256."""

    viewpoint_id: str
    version: str
    protocol: dict[str, Any]
    bands: tuple[str, ...]
    frames: dict[str, MergedFrame]
    source: str
    sha256: str

    def to_record(self) -> dict[str, Any]:
        """Provenance for result records."""
        return {
            "source": self.source,
            "sha256": self.sha256,
            "version": self.version,
            "frames": len(self.frames),
            "labellers": sorted(self.protocol.get("labellers", {})),
        }


def load_merged_labels(
    path: str | Path | None = None, *, roots: Iterable[str | Path | None] = ()
) -> MergedLabels:
    """Read a merged labels file (default: Rainier v0.2) under the leakage guard.

    Refused whole: a file under the quarantine folder; a file labelling any frame the
    committed split holds out, placed by its ``image_time_utc`` and by the time in its key;
    a label whose two times differ by more than :data:`.labels.MAX_KEY_TIME_SKEW_S`. A merged
    state must be the labellers' common state, or None exactly where they differ.
    """
    file_path = Path(path).expanduser() if path is not None else LABELS_RAINIER_V0_2
    reason = path_quarantine_reason(file_path, roots=roots)
    if reason is not None:
        raise LeakageError(f"labels {file_path}: {reason}")
    data = file_path.read_bytes()
    doc = json.loads(data)
    if doc.get("kind") != LABELS_KIND or doc.get("format") != MERGED_FORMAT:
        raise ValueError(f"{file_path}: not a merged {LABELS_KIND} file ({MERGED_FORMAT})")
    bands = tuple(doc["bands"])
    split = committed_split()
    frames: dict[str, MergedFrame] = {}
    for item in doc["frames"]:
        key = item["key"]
        parsed = parse_frame_key(key)
        if parsed.viewpoint_id != doc["viewpoint_id"]:
            raise ValueError(f"{key}: not a frame of {doc['viewpoint_id']}")
        if key in frames:
            raise ValueError(f"{key} is labelled twice")
        labelled_time = parse_iso_utc(item["image_time_utc"])
        source = "nps-archive" if parsed.kind == "archive" else "live"
        for moment in (labelled_time, parsed.image_time):
            record = FrameRecord(
                viewpoint_id=parsed.viewpoint_id, image_time=moment, key=key, source=source
            )
            held = split.held_out_reason(record)
            if held is not None:
                raise LeakageError(f"{file_path}: labels a held-out frame {key} ({held})")
        skew = abs((labelled_time - parsed.image_time).total_seconds())
        if skew > MAX_KEY_TIME_SKEW_S:
            raise ValueError(
                f"{key}: image_time_utc {item['image_time_utc']} is {skew:.0f} s from the key"
            )
        who = item["labellers"]
        if set(who) != {"a", "b"}:
            raise ValueError(f"{key}: labellers must be exactly a and b")
        merged = item["bands"]
        if set(merged) != set(bands):
            raise ValueError(f"{key}: bands must be {bands}")
        for band in bands:
            first, second = who["a"][band], who["b"][band]
            if first not in LABELLER_STATES or second not in LABELLER_STATES:
                raise ValueError(f"{key}: {band} has a state outside {LABELLER_STATES}")
            expected = first if first == second else None
            if merged[band] != expected:
                raise ValueError(f"{key}: merged {band} {merged[band]!r} breaks the merge rule")
        frames[key] = MergedFrame(
            key=key,
            sha256=str(item["sha256"]),
            image_time_utc=str(item["image_time_utc"]),
            sheet=str(item["sheet"]),
            bands=dict(merged),
            labellers={w: dict(v) for w, v in who.items()},
        )
    return MergedLabels(
        viewpoint_id=str(doc["viewpoint_id"]),
        version=str(doc["version"]),
        protocol=dict(doc["protocol"]),
        bands=bands,
        frames=frames,
        source=str(file_path),
        sha256=hashlib.sha256(data).hexdigest(),
    )
