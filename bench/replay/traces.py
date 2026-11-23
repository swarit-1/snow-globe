"""Read the replay harness's inputs for the scorer (PRD §10.5, §10.6; bench/README.md "Scoring").

- **Traces:** JSON lines in the trace schema of app/core/src/trace.rs, version 2. Every line is
  checked against that schema strictly: a missing or unknown key, a wrong type or an unknown enum
  name is an error naming the line and the key, never a guess. A line without a `replay` block is
  not a replay trial and is skipped (counted).
- **Transcripts:** JSON lines `{"session": int, "trial": int, "final": str}`, one per trial,
  written beside the traces because a trace never holds text (PRD §10.5). An optional `utterance`
  is cross-checked against the trace. TODO(M1): the harness writes this file from the app's final
  transcripts, kept with the recordings' retention (bench/README.md "Scoring").
- **Sidecars:** `<fixtures>/scripted/<script hash>/<id>.<take>.json`, one per recorded take. The
  scorer reads only the fields it needs: `id`, `take`, `script_hash`, `key_up_ms`,
  `speech_onset_ms` and `speech_end_ms`, all offsets in milliseconds from the recording's first
  sample. TODO(M1): the recording tool writes them (bench/README.md "Where recordings live").
  Only the `.json` sidecar is opened, never the audio beside it.
- **Run manifest** (optional): `run.json`, whose `kind` says whether the run is `synthetic` or
  `measured`. A measured run also names its frozen build: `build_commit` (the git commit, hex)
  and `parser_sha256` (the SHA-256 of that build's snow-globe-parse), so the scorer can refuse to
  re-parse with another parser (PRD §10.6 "from a frozen build"). An optional `tier2_enabled`
  (true or false) says whether the run sent Tier 2 at all. TODO(M1): the harness writes it.

Numbers must be finite: `NaN` and `Infinity`, which Python's JSON reader accepts by default, are
refused wherever they appear.

Standard library only.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = 2

MARKS = (
    "key_down",
    "audio_first_sample",
    "speech_first_partial",
    "place_matched",
    "assets_ready",
    "key_up",
    "speech_final",
    "parsed",
    "forecast_ready",
    "tier0_paint",
    "tier1_paint",
    "tier2_sent",
    "tier2_first_byte",
    "tier2_checked",
    "tier2_paint",
)
SPANS = (
    "os_event_to_handler",
    "audio_buffer_delivery",
    "stt_token",
    "dns",
    "tcp",
    "tls",
    "ttfb",
    "proxy_internal",
    "provider_queue",
    "provider_inference",
    "readback",
    "encode",
    "panel_reveal",
    "partial_lag",
    "stable_lag",
)
OUTCOMES = ("painted", "cancelled", "no_match", "out_of_range", "fallback", "empty", "error")
PATHS = ("fast_path", "fallback")
CACHE_STATES = ("unknown", "idle", "warm", "cold", "after_wake", "after_launch")
THERMAL = ("unknown", "nominal", "fair", "serious", "critical")
POWER = ("unknown", "ac", "battery")
NETWORK = ("unknown", "none", "wifi", "ethernet", "cellular", "other")
DISPLAY = ("unknown", "on", "asleep")
ENGINES = ("unknown", "dictation_transcriber", "deepgram_nova3", "parakeet")
RETENTION = ("unknown", "while_in_use", "lingering", "process_lifetime")
TIERS = ("tier0", "tier1", "tier2")
SOURCES = ("partial", "final")
CONTEXT_KINDS = ("not_recorded", "empty", "place", "unlisted")

SCRIPT_HASH = re.compile(r"[0-9a-f]{12}")
SCRIPTED_ID = re.compile(r"d\d{2}|t\d{3}")
REAL_USE_ID = re.compile(r"\d{8}T\d{6}Z")
WALL = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
COMMIT = re.compile(r"[0-9a-f]{7,40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
LEVERS = ("pre_render", "partial_paint", "paint_capture")


class InputError(ValueError):
    """A trace, transcript, sidecar or manifest that does not have the expected shape."""


class _NotFinite(ValueError):
    """`NaN`, `Infinity` or `-Infinity` in a JSON text."""


def _refuse_constant(name: str) -> Any:
    raise _NotFinite(name)


def loads(text: str, where: str) -> Any:
    """`text` decoded as JSON, refusing `NaN` and `±Infinity`; errors name `where`."""
    try:
        return json.loads(text, parse_constant=_refuse_constant)
    except _NotFinite as e:
        raise InputError(f"{where}: {e} is not a finite number") from None
    except json.JSONDecodeError as e:
        raise InputError(f"{where}: not JSON ({e.msg})") from None


@dataclass(frozen=True)
class Span:
    """One trace span (durations in nanoseconds)."""

    name: str
    start_ns: int
    end_ns: int
    dur_ns: int
    seq: int | None
    connection_reused: bool | None


@dataclass(frozen=True)
class Paint:
    """One composited paint."""

    tier: str
    source: str
    scene: int
    matches_final: bool | None
    mono_ns: int


@dataclass(frozen=True)
class Replay:
    """The trace's replay block."""

    script_hash: str
    utterance: str
    take: int
    session: int
    trial: int


@dataclass(frozen=True)
class TraceLine:
    """One schema-2 trace line, checked."""

    query_gen: int
    outcome: str
    path: str | None
    context_kind: str
    context_place_id: str | None
    context_viewpoint_id: str | None
    engine: str
    silence_pad_ms: int | None
    model_retention: str
    warm_up: bool | None
    pre_render: bool | None
    partial_paint: bool | None
    paint_capture: bool | None
    marks: Mapping[str, int]
    spans: tuple[Span, ...]
    paints: tuple[Paint, ...]
    idle_duration_ms: int | None
    cache_state: str
    thermal_state: str
    power_source: str
    network_interface: str
    display: str
    transcript_chars: int
    transcript_words: int
    partials: int
    replay: Replay | None

    @property
    def setup(self) -> Setup:
        """The speech setup and the levers: trials are only pooled within one."""
        return Setup(
            self.engine,
            self.silence_pad_ms,
            self.model_retention,
            self.warm_up,
            self.pre_render,
            self.partial_paint,
            self.paint_capture,
        )


@dataclass(frozen=True)
class Setup:
    """What a trial was run with besides its state: the speech setup (D-067) and the levers
    (PRD §7.1, §7.2, §10.4). `None` is "not recorded"."""

    engine: str
    silence_pad_ms: int | None
    model_retention: str
    warm_up: bool | None
    pre_render: bool | None
    partial_paint: bool | None
    paint_capture: bool | None

    def sort_key(self) -> tuple[Any, ...]:
        """A total order: "not recorded" first, then off, then on; no pad before any pad."""

        def flag(v: bool | None) -> int:
            return -1 if v is None else int(v)

        flags = (self.warm_up, self.pre_render, self.partial_paint, self.paint_capture)
        pad = -1 if self.silence_pad_ms is None else self.silence_pad_ms
        return (self.engine, pad, self.model_retention, *map(flag, flags))


def _keys(obj: Any, where: str, required: Iterable[str], optional: Iterable[str] = ()) -> None:
    if not isinstance(obj, dict):
        raise InputError(f"{where}: expected an object")
    required, optional = set(required), set(optional)
    missing = sorted(required - obj.keys())
    unknown = sorted(obj.keys() - required - optional)
    if missing:
        raise InputError(f"{where}: missing {', '.join(missing)}")
    if unknown:
        raise InputError(f"{where}: unknown {', '.join(unknown)}")


def _int(obj: Any, where: str, *, nullable: bool = False, minimum: int = 0) -> int | None:
    if obj is None and nullable:
        return None
    if isinstance(obj, bool) or not isinstance(obj, int) or obj < minimum:
        raise InputError(f"{where}: expected an integer >= {minimum}")
    return obj


def _enum(obj: Any, where: str, names: tuple[str, ...]) -> str:
    if obj not in names:
        raise InputError(f"{where}: {obj!r} is not one of {', '.join(names)}")
    return obj


def _bool(obj: Any, where: str, *, nullable: bool = False) -> bool | None:
    if obj is None and nullable:
        return None
    if not isinstance(obj, bool):
        raise InputError(f"{where}: expected true or false")
    return obj


def parse_trace(obj: Any, where: str, place_ids: Mapping[str, str]) -> TraceLine:
    """Check one decoded trace line against schema 2; `place_ids` maps each manifest
    viewpoint id to its place id (a context must name a pair the manifests define)."""
    _keys(
        obj,
        where,
        (
            "schema",
            "query_gen",
            "outcome",
            "context",
            "speech",
            "levers",
            "marks",
            "spans",
            "paints",
            "state",
            "lengths",
        ),
        ("path", "replay"),
    )
    if obj["schema"] != SCHEMA:
        raise InputError(f"{where}: schema {obj['schema']!r}, the scorer reads schema {SCHEMA}")
    ctx = obj["context"]
    if not isinstance(ctx, dict):
        raise InputError(f"{where}.context: expected an object")
    kind = _enum(ctx.get("kind"), f"{where}.context.kind", CONTEXT_KINDS)
    place_id = viewpoint_id = None
    if kind == "place":
        _keys(ctx, f"{where}.context", ("kind", "place_id", "viewpoint_id"))
        place_id, viewpoint_id = ctx["place_id"], ctx["viewpoint_id"]
        if place_ids.get(viewpoint_id) != place_id:
            raise InputError(f"{where}.context: not a manifest place and viewpoint")
    else:
        _keys(ctx, f"{where}.context", ("kind",))
    speech = obj["speech"]
    _keys(speech, f"{where}.speech", ("engine", "silence_pad_ms", "model_retention", "warm_up"))
    levers = obj["levers"]
    _keys(levers, f"{where}.levers", LEVERS)
    lever = {k: _bool(levers[k], f"{where}.levers.{k}", nullable=True) for k in LEVERS}
    marks: dict[str, int] = {}
    if not isinstance(obj["marks"], dict):
        raise InputError(f"{where}.marks: expected an object")
    for name, stamp in obj["marks"].items():
        _enum(name, f"{where}.marks", MARKS)
        _keys(stamp, f"{where}.marks.{name}", ("mono_ns", "wall"))
        marks[name] = _int(stamp["mono_ns"], f"{where}.marks.{name}.mono_ns")
        if not isinstance(stamp["wall"], str) or not WALL.fullmatch(stamp["wall"]):
            raise InputError(f"{where}.marks.{name}.wall: not RFC 3339 UTC with microseconds")
    spans = []
    if not isinstance(obj["spans"], list):
        raise InputError(f"{where}.spans: expected a list")
    for i, s in enumerate(obj["spans"]):
        w = f"{where}.spans[{i}]"
        _keys(s, w, ("name", "start_ns", "end_ns", "dur_ns"), ("seq", "connection_reused"))
        span = Span(
            name=_enum(s["name"], f"{w}.name", SPANS),
            start_ns=_int(s["start_ns"], f"{w}.start_ns"),
            end_ns=_int(s["end_ns"], f"{w}.end_ns"),
            dur_ns=_int(s["dur_ns"], f"{w}.dur_ns"),
            seq=_int(s.get("seq"), f"{w}.seq", nullable=True),
            connection_reused=_bool(s.get("connection_reused"), f"{w}.reused", nullable=True),
        )
        if span.end_ns - span.start_ns != span.dur_ns:
            raise InputError(f"{w}: dur_ns is not end_ns - start_ns")
        spans.append(span)
    paints = []
    if not isinstance(obj["paints"], list):
        raise InputError(f"{where}.paints: expected a list")
    for i, p in enumerate(obj["paints"]):
        w = f"{where}.paints[{i}]"
        _keys(p, w, ("tier", "source", "scene", "matches_final", "mono_ns"))
        paints.append(
            Paint(
                tier=_enum(p["tier"], f"{w}.tier", TIERS),
                source=_enum(p["source"], f"{w}.source", SOURCES),
                scene=_int(p["scene"], f"{w}.scene"),
                matches_final=_bool(p["matches_final"], f"{w}.matches_final", nullable=True),
                mono_ns=_int(p["mono_ns"], f"{w}.mono_ns"),
            )
        )
    state = obj["state"]
    _keys(
        state,
        f"{where}.state",
        (
            "idle_duration_ms",
            "cache_state",
            "thermal_state",
            "power_source",
            "network_interface",
            "display",
        ),
    )
    lengths = obj["lengths"]
    _keys(lengths, f"{where}.lengths", ("transcript_chars", "transcript_words", "partials"))
    replay = None
    if "replay" in obj:
        r = obj["replay"]
        w = f"{where}.replay"
        _keys(r, w, ("script_hash", "utterance", "take", "session", "trial"))
        if not isinstance(r["script_hash"], str) or not SCRIPT_HASH.fullmatch(r["script_hash"]):
            raise InputError(f"{w}.script_hash: not 12 lower-case hex digits")
        utt = r["utterance"]
        if not isinstance(utt, str) or not (
            SCRIPTED_ID.fullmatch(utt) or REAL_USE_ID.fullmatch(utt)
        ):
            raise InputError(f"{w}.utterance: not a scripted id or a real-use time")
        replay = Replay(
            script_hash=r["script_hash"],
            utterance=utt,
            take=_int(r["take"], f"{w}.take", minimum=1),
            session=_int(r["session"], f"{w}.session"),
            trial=_int(r["trial"], f"{w}.trial"),
        )
    path = obj.get("path")
    return TraceLine(
        query_gen=_int(obj["query_gen"], f"{where}.query_gen"),
        outcome=_enum(obj["outcome"], f"{where}.outcome", OUTCOMES),
        path=None if path is None else _enum(path, f"{where}.path", PATHS),
        context_kind=kind,
        context_place_id=place_id,
        context_viewpoint_id=viewpoint_id,
        engine=_enum(speech["engine"], f"{where}.speech.engine", ENGINES),
        silence_pad_ms=_int(
            speech["silence_pad_ms"], f"{where}.speech.silence_pad_ms", nullable=True
        ),
        model_retention=_enum(
            speech["model_retention"], f"{where}.speech.model_retention", RETENTION
        ),
        warm_up=_bool(speech["warm_up"], f"{where}.speech.warm_up", nullable=True),
        pre_render=lever["pre_render"],
        partial_paint=lever["partial_paint"],
        paint_capture=lever["paint_capture"],
        marks=marks,
        spans=tuple(spans),
        paints=tuple(paints),
        idle_duration_ms=_int(state["idle_duration_ms"], f"{where}.state.idle", nullable=True),
        cache_state=_enum(state["cache_state"], f"{where}.state.cache_state", CACHE_STATES),
        thermal_state=_enum(state["thermal_state"], f"{where}.state.thermal_state", THERMAL),
        power_source=_enum(state["power_source"], f"{where}.state.power_source", POWER),
        network_interface=_enum(
            state["network_interface"], f"{where}.state.network_interface", NETWORK
        ),
        display=_enum(state["display"], f"{where}.state.display", DISPLAY),
        transcript_chars=_int(lengths["transcript_chars"], f"{where}.lengths.chars"),
        transcript_words=_int(lengths["transcript_words"], f"{where}.lengths.words"),
        partials=_int(lengths["partials"], f"{where}.lengths.partials"),
        replay=replay,
    )


def read_jsonl(path: Path) -> Iterator[tuple[int, Any]]:
    """(line number, decoded object) for every non-blank line of a JSON-lines file."""
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                yield n, loads(line, f"{path}:{n}")


@dataclass(frozen=True)
class Transcript:
    """One trial's final transcript (kept outside the trace)."""

    final: str
    utterance: str | None
    synthetic: bool


def read_transcripts(path: Path) -> dict[tuple[int, int], Transcript]:
    """{(session, trial): transcript}; a repeated key is an error."""
    out: dict[tuple[int, int], Transcript] = {}
    for n, obj in read_jsonl(path):
        where = f"{path.name}:{n}"
        _keys(obj, where, ("session", "trial", "final"), ("utterance", "synthetic"))
        key = (_int(obj["session"], f"{where}.session"), _int(obj["trial"], f"{where}.trial"))
        if not isinstance(obj["final"], str):
            raise InputError(f"{where}.final: expected a string")
        if key in out:
            raise InputError(f"{where}: a second transcript for session {key[0]} trial {key[1]}")
        utt = obj.get("utterance")
        if utt is not None and not isinstance(utt, str):
            raise InputError(f"{where}.utterance: expected a string")
        out[key] = Transcript(
            final=obj["final"],
            utterance=utt,
            synthetic=bool(_bool(obj.get("synthetic", False), f"{where}.synthetic")),
        )
    return out


@dataclass(frozen=True)
class Sidecar:
    """What the scorer reads from a recording's sidecar (offsets in ms from its first sample)."""

    key_up_ms: float
    speech_onset_ms: float
    speech_end_ms: float
    synthetic: bool

    @property
    def still_in_speech_at_key_up(self) -> bool:
        """The owner was still speaking when the key went up (PRD §10.6 report line)."""
        return self.speech_end_ms > self.key_up_ms


def _number(obj: Any, where: str) -> float:
    if isinstance(obj, bool) or not isinstance(obj, int | float) or not math.isfinite(obj):
        raise InputError(f"{where}: expected a finite number")
    return float(obj)


def sidecar_path(fixtures: Path, script_hash: str, utterance: str, take: int) -> Path:
    """Where a scripted take's sidecar lives (bench/README.md "Where recordings live")."""
    return fixtures / "scripted" / script_hash / f"{utterance}.{take}.json"


def read_sidecar(path: Path, script_hash: str, utterance: str, take: int) -> Sidecar:
    """A take's sidecar, checked against the trial that played it."""
    obj = loads(path.read_text(encoding="utf-8"), str(path))
    if not isinstance(obj, dict):
        raise InputError(f"{path}: expected an object")
    for key in ("id", "take", "script_hash", "key_up_ms", "speech_onset_ms", "speech_end_ms"):
        if key not in obj:
            raise InputError(f"{path}: missing {key}")
    if (obj["id"], obj["take"], obj["script_hash"]) != (utterance, take, script_hash):
        raise InputError(f"{path}: names another take than the trace")
    return Sidecar(
        key_up_ms=_number(obj["key_up_ms"], f"{path}.key_up_ms"),
        speech_onset_ms=_number(obj["speech_onset_ms"], f"{path}.speech_onset_ms"),
        speech_end_ms=_number(obj["speech_end_ms"], f"{path}.speech_end_ms"),
        synthetic=obj.get("synthetic") is True,
    )


def read_run_manifest(path: Path) -> dict[str, Any]:
    """`run.json` (module doc): an object whose `kind` is `synthetic` or `measured`; a measured
    run names its build (`build_commit`) and that build's parser (`parser_sha256`), and
    `tier2_enabled`, if present, is true or false."""
    obj = loads(path.read_text(encoding="utf-8"), str(path))
    if not isinstance(obj, dict) or obj.get("kind") not in ("synthetic", "measured"):
        raise InputError(f"{path}: kind must be synthetic or measured")
    if "tier2_enabled" in obj and not isinstance(obj["tier2_enabled"], bool):
        raise InputError(f"{path}: tier2_enabled must be true or false")
    commit, parser = obj.get("build_commit"), obj.get("parser_sha256")
    if obj["kind"] == "measured" and (commit is None or parser is None):
        raise InputError(
            f"{path}: a measured run names its frozen build (build_commit) and that build's "
            "parser (parser_sha256)"
        )
    if commit is not None and not (isinstance(commit, str) and COMMIT.fullmatch(commit)):
        raise InputError(f"{path}: build_commit must be a git commit in lower-case hex")
    if parser is not None and not (isinstance(parser, str) and SHA256.fullmatch(parser)):
        raise InputError(f"{path}: parser_sha256 must be 64 lower-case hex digits")
    return obj
