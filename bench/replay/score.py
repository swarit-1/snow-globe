"""Score replay traces against the scripted utterances (PRD §10.6, §14, §7.1; bench/README.md
"Scoring"; D-057, D-067, D-068). Ticket M1-09.

Run from the repository root on workers/common's Python 3.13, as check.py is:

    python3 -I -B bench/replay/score.py --traces RUN/traces.jsonl \\
        --transcripts RUN/transcripts.jsonl --fixtures FIXTURES --out OUT [--run RUN/run.json]

`FIXTURES` is the voice-fixtures folder (`<data dir>/fixtures/voice` for real recordings); only
its `.json` sidecars are read. The parser binary is found as check.py finds it (`--bin`,
`$SNOW_GLOBE_PARSE`, or the release build in the cargo target dir); a binary older than its
sources is refused unless `--allow-stale-binary`, and the report then says so. A measured run's
manifest names its build's parser (traces.py), and another parser is refused unless
`--allow-other-parser`, when the report says so at its top.

**Per trial** (a trace line with a `replay` block):
1. Join it to its script row (by utterance id; the trace's script hash must be the script's), its
   final transcript (by session and trial) and its take's sidecar.
2. Re-parse the transcript with snow-globe-parse at the row's reference clock and at the context
   place the trace recorded, never at the script's context column, then compare with the row's
   expected columns exactly as check.py does (`compare()`): `yes` is exact place and time, `soft`
   differs only in a rolled flag or in miss against fallback. A follow-up whose live context is
   not the column's place is wrong on place whatever the re-parse gives (bench/README.md). An
   empty final transcript is wrong on place and time: nothing was heard, so it is not re-parsed
   (the parser would resolve an empty text to the context place, now).
3. Measure every stage from the trace's marks, the paints against the final parse, the partial
   lags, and from the sidecar the first partial after speech onset and whether the owner was
   still speaking at key-up. A tier's paint time is its earliest recorded paint, never a mark
   alone (PRD §10.4: webview callbacks are diagnostics).

**Per group** (setup × split × state × display, the setup being the speech setup and the levers;
idle is the headline, and display-asleep trials are reported apart and inside the totals, D-068)
every metric gets a seeded cluster bootstrap interval (stats.py), and the wrong-paint rates also
get Wilson bounds. The latency targets are shown against idle trials (PRD §10.1 "p95, idle"); the
accuracy targets against the whole test split (PRD §14 "replay test set", §7.1 "on the test
set"), with idle beside them. The report is written as `replay-report.json` and
`replay-report.md`. Neither holds transcript text.

A stage of the scorer that fails on a trial (the re-parse, or the WER normaliser) never counts as
a verdict: the trial is left out of the figures that stage feeds, the report says so at its top,
and the command exits 1 after writing it.

Exit status: 0 when the report was written and every stage ran; 1 when an input is malformed,
nothing could be scored, or a stage failed on some trial (the report is then still written); 2
when the parser binary is missing, older than its sources, or not the measured run's parser.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "utterances"))

import check
import prd_reading as pr
import report
import stats
import traces as tr
import wer

REPORT_VERSION = 2

#: Percentiles reported for every latency metric.
QUANTILES = (0.5, 0.95)

#: Latency stages from the trace's marks: id -> (from mark, to mark, label). All in ms. The paint
#: marks are replaced by each tier's earliest recorded paint (measure()).
MARK_STAGES: dict[str, tuple[str, str, str]] = {
    "hold": ("key_down", "key_up", "key held (key-down → key-up)"),
    "first_partial_after_key_down": (
        "key_down",
        "speech_first_partial",
        "first partial after key-down",
    ),
    "place_matched_after_key_down": ("key_down", "place_matched", "place matched after key-down"),
    "assets_ready_after_key_down": ("key_down", "assets_ready", "assets ready after key-down"),
    "assets_ready_after_key_up": (
        "key_up",
        "assets_ready",
        "assets ready after key-up (negative: before it)",
    ),
    "release_to_final": ("key_up", "speech_final", "release → final transcript"),
    "parse": ("speech_final", "parsed", "parse (final → parsed)"),
    "forecast_after_parse": ("parsed", "forecast_ready", "forecast ready after parse"),
    "tier0_first_paint": ("key_up", "tier0_paint", "key-up → first Tier 0 paint, right or wrong"),
    "tier2_send_after_parse": ("parsed", "tier2_sent", "Tier 2 sent after parse"),
    "tier2_first_byte": ("tier2_sent", "tier2_first_byte", "Tier 2 first byte after send"),
    "tier2_checks": ("tier2_first_byte", "tier2_checked", "Tier 2 checks after first byte"),
    "tier2_paint_after_checks": ("tier2_checked", "tier2_paint", "Tier 2 paint after checks"),
}

#: Latencies from key-up to the first paint of a tier whose scene is the final parse's (a wrong
#: paint never counts as a hit, PRD §10.3).
TIER_STAGES: dict[str, tuple[str, str]] = {
    "tier0": ("tier0", "key-up → Tier 0 paint matching the final (negative: before key-up)"),
    "tier1": ("tier1", "key-up → Tier 1 paint matching the final"),
    "tier2": ("tier2", "key-up → Tier 2 paint matching the final"),
}

#: Other value metrics: id -> label.
OTHER_VALUES: dict[str, str] = {
    "first_partial_after_onset": "first partial after speech onset (sidecar)",
    "partial_lag": "partial lag, per PARTIAL event",
    "stable_lag": "stable lag, per STABLE event",
    "visible_wrong": "visible-wrong time, per wrong paint",
}

#: Spans reported as durations, per occurrence (the lag spans have their own metrics).
SPAN_METRICS = tuple(s for s in tr.SPANS if s not in ("partial_lag", "stable_lag"))

#: Spans that only Tier 2 work records (PRD §10.5): any of them means Tier 2 was tried.
TIER2_SPANS = ("readback", "encode", "proxy_internal", "provider_queue", "provider_inference")

LABELS: dict[str, str] = {k: v[2] for k, v in MARK_STAGES.items()}
LABELS.update({k: v[1] for k, v in TIER_STAGES.items()})
LABELS.update(OTHER_VALUES)
LABELS.update({f"span:{s}": f"span {s}" for s in SPAN_METRICS})

VALUE_METRICS: tuple[str, ...] = (
    *TIER_STAGES,
    *MARK_STAGES,
    *OTHER_VALUES,
    *(f"span:{s}" for s in SPAN_METRICS),
)

#: Ratio metrics: id -> label (each Σ numerator / Σ denominator; the denominator is named).
RATIOS: dict[str, str] = {
    "exact_non_pending": "exact place and time, rows not owner-pending (D-057)",
    "exact_settled": "exact place and time, settled rows (also without the rows the parser "
    "gets wrong on the script's own text)",
    "exact_text_no": "exact place and time, rows the parser gets wrong on the script's text",
    "exact_pending": "exact place and time, owner-pending rows (apart)",
    "exact_or_soft_non_pending": "exact or soft, rows not owner-pending",
    "place_non_pending": "place right, rows not owner-pending",
    "place_settled": "place right, settled rows",
    "place_pending": "place right, owner-pending rows (apart)",
    "fallback": "re-parse sent to the fallback",
    "wer_raw": "WER, raw",
    "wer_normalised": "WER, normalised",
    "wrong_paint_per_partial_paint": "wrong paints from partials, per trial that painted from "
    "a partial",
    "wrong_paint_per_trial": "wrong paints from partials, per judged trial",
    "wrong_final_paint": "trials with a wrong paint from the final (expected 0)",
    "partial_painted": "trials that painted from a partial",
    "flips_per_query": "flips per query that painted",
    "still_in_speech_at_key_up": "utterances still in speech at key-up (each take played "
    "counted once; sidecar)",
    "still_in_speech_per_trial": "still in speech at key-up, per trial (each replay of a take "
    "counted again)",
    "context_differed": "follow-ups whose live context was not the script's",
    "hit_tier0_300": "Tier 0 matching the final within 300 ms of key-up",
    "hit_tier1_1000": "Tier 1 matching the final within 1,000 ms",
    "hit_tier1_400": "Tier 1 matching the final within 400 ms (stretch)",
    "hit_tier2_3000": "Tier 2 matching the final within 3 s",
    "hit_release_to_final_240": "release → final within 240 ms",
    "hit_first_partial_300": "first partial within 300 ms of speech onset",
}

#: The ratios estimated once per recording (an utterance's take), not once per trial.
RECORDING_RATIOS = ("still_in_speech_at_key_up",)
TRIAL_RATIOS = tuple(r for r in RATIOS if r not in RECORDING_RATIOS)

#: The budget hit rates (PRD §10.6 "budget hit rates"): metric -> the denominator, named.
HIT_RATES: dict[str, str] = {
    "hit_tier0_300": "trials due a paint",
    "hit_tier1_1000": "trials due a paint",
    "hit_tier1_400": "trials due a paint",
    "hit_tier2_3000": "trials due a paint (a group that never tried Tier 2 has none)",
    "hit_release_to_final_240": "trials with a final transcript",
    "hit_first_partial_300": "trials with a first partial and an aligned sidecar",
}

#: PRD targets beside the metrics they apply to: (metric, quantile or None, text, on). `on` is
#: `idle` for the latency budgets (PRD §10.1: "p95, OS key-up → composited paint, idle") and
#: `test set` for the accuracy targets, which PRD §14 and §7.1 set on the whole replay test set.
TARGETS: tuple[tuple[str, float | None, str, str], ...] = (
    ("tier0", 0.95, "p95 ≤ 300 ms (PRD §14)", "idle"),
    ("tier1", 0.95, "p95 ≤ 1 s; stretch ≤ 400 ms (PRD §14)", "idle"),
    ("hit_tier2_3000", None, "≥ 80 % of queries (PRD §14)", "idle"),
    ("first_partial_after_onset", 0.5, "p50 ≤ 300 ms (PRD §14)", "idle"),
    ("release_to_final", 0.95, "p95 ≤ 240 ms, an allocation (PRD §14)", "idle"),
    ("parse", 0.95, "5 ms, an allocation (PRD §10.1)", "idle"),
    ("exact_non_pending", None, "≥ 95 % on the replay test set (PRD §14)", "test set"),
    ("fallback", None, "≤ 10 % (PRD §14)", "test set"),
    (
        "wrong_paint_per_partial_paint",
        None,
        "≤ 2 %, Wilson upper bound on the test set (PRD §14, §7.1)",
        "test set",
    ),
)

#: How far the trace's key-up may sit from where the take's sidecar puts it, counted from
#: `audio_first_sample`, before the trial's onset-based latency is left out (ms). Proposed: two
#: buffers of 1,024 frames at 48 kHz plus a few ms of event delivery. TODO(M1): set it from the
#: harness's measured buffer period and key-injection jitter (PRD §10.4, §10.6).
KEY_UP_TOLERANCE_MS = 50.0

#: PRD §10.6's statement of the replay set's known bias, printed in every report.
KNOWN_BIAS = "one speaker, one microphone, one room (PRD §10.6)"

STATES = ("idle", "warm", "cold", "after_wake", "after_launch", "unknown")
DISPLAYS = ("on", "asleep", "unknown")
IDLE_MIN_MS = 30 * 60 * 1000
WARM_MAX_MS = 60 * 1000
SPLITS = ("test", "dev")


# ---------------------------------------------------------------- the parser


def _failed(how: str) -> dict[str, Any]:
    return {"_exit": -1, "error": "parser_failed", "failure": how}


@dataclass
class Reparser:
    """snow-globe-parse on a transcript at a reference clock and a context, cached."""

    binary: Path
    clocks: Mapping[str, str]
    cache: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    calls: int = 0

    def __call__(self, text: str, clock: str, context: str | None) -> dict[str, Any]:
        """The parser's JSON plus `_exit`, or `{"error": "parser_failed", "failure": ...}` when it
        could not be run on the text, exited with another status than 0 or 2, timed out, or
        printed something that is not its JSON. Its messages may echo the transcript, so they
        are never kept: `failure` says only what went wrong."""
        key = (text, clock, context or "")
        if key not in self.cache:
            self.calls += 1
            row = {"id": "replay", "text": text, "clock": clock, "context": context or ""}
            try:
                out = check.run_parser(self.binary, row, self.clocks)
            except subprocess.TimeoutExpired:
                out = _failed("timed out")
            except RuntimeError:
                out = _failed("exited with a status other than 0 or 2")
            except json.JSONDecodeError:
                out = _failed("printed something that is not JSON")
            except TypeError:  # JSON, but not an object
                out = _failed("printed JSON that is not its output")
            except ValueError:  # an argument it cannot be given, such as a NUL character
                out = _failed("could not take the transcript as an argument")
            except OSError:  # missing, not executable, or the argument list too long
                out = _failed("could not be started")
            else:
                try:
                    check.parser_columns(out)  # the shape compare() needs
                except (KeyError, TypeError, ValueError, AttributeError):
                    out = _failed("printed JSON that is not its output")
            self.cache[key] = out
        return self.cache[key]


def parser_columns(out: Mapping[str, Any]) -> dict[str, str]:
    """check.py's parser_* columns for one parser result (a failed run is `parser-failed`)."""
    if out.get("error") == "parser_failed":
        cols = {c: "" for c in pr.PARSER_OUTPUT_COLUMNS}
        cols["parser_outcome"] = "parser-failed"
        return cols
    return check.parser_columns(dict(out))


RESOLVED = ("query", "out-of-range-past", "out-of-range-beyond")


def place_right(row: Mapping[str, str], cols: Mapping[str, str]) -> bool:
    """Whether the re-parse ended at the right place (PRD §7.1 "place accuracy after the parser"):
    a resolved row must resolve to its viewpoint (either one of a close call); a miss must not
    resolve to any place; a fallback row must reach the fallback or resolve to its viewpoint."""
    expected = row["expected_outcome"]
    got = cols["parser_outcome"].split("/")[0]
    allowed = set(row["expected_viewpoint_id"].split("|"))
    resolved = got in RESOLVED
    if expected in RESOLVED:
        return resolved and cols["parser_viewpoint_id"] in allowed
    if expected == "miss":
        return not resolved
    return got == "fallback" or (resolved and cols["parser_viewpoint_id"] in allowed)


def category(row: Mapping[str, str]) -> str:
    """`owner-pending`; `text-level-no` for a row whose stored agreement is `no` (the parser gets
    it wrong on the script's own text, reported apart until the lead resolves it); else
    `settled` (bench/README.md "Scoring")."""
    if check.PENDING in check.tags_of(dict(row)):
        return "owner-pending"
    if row["agreement"] == "no":
        return "text-level-no"
    return "settled"


def app_reading(line: tr.TraceLine) -> str | None:
    """What the app's outcome says about the place: `place`, `no place`, `fallback`, or None when
    it says nothing the re-parse can check (cancelled, error, empty, or a place the fallback
    parser found, which the fast-path re-parse cannot reproduce)."""
    if line.outcome == "fallback":
        return "fallback"
    if line.outcome in ("painted", "out_of_range"):
        return None if line.path == "fallback" else "place"
    if line.outcome == "no_match":
        return "no place"
    return None


def reparse_reading(cols: Mapping[str, str]) -> str | None:
    """The same classes for a re-parse (None for an invalid date or a missing solar event)."""
    got = cols["parser_outcome"].split("/")[0]
    if got in RESOLVED:
        return "place"
    return {"miss": "no place", "fallback": "fallback"}.get(got)


# ---------------------------------------------------------------- one trial


@dataclass
class Trial:
    """One replay trace line joined to its script row, transcript and sidecar."""

    line: tr.TraceLine
    source: str
    row: Mapping[str, str]
    index: int
    transcript: tr.Transcript | None
    sidecar: tr.Sidecar | None
    sidecar_problem: str | None = None

    @property
    def replay(self) -> tr.Replay:
        """The replay block (always present on a Trial)."""
        assert self.line.replay is not None
        return self.line.replay


@dataclass
class TrialScore:
    """Everything the report needs from one trial. Holds no transcript text."""

    session: int
    trial: int
    utterance: str
    take: int
    split: str
    category: str
    follow_up: bool
    setup: tr.Setup
    state: str
    display: str
    idle_duration_ms: int | None
    outcome: str
    context: str
    wer_raw: wer.Errors | None = None
    wer_normalised: wer.Errors | None = None
    parser_outcome: str | None = None
    parser_viewpoint_id: str | None = None
    agreement: str | None = None
    difference: str | None = None
    exact: bool | None = None
    exact_or_soft: bool | None = None
    place: bool | None = None
    fallback: bool | None = None
    context_status: str | None = None
    context_reason: str | None = None
    latencies: dict[str, float] = field(default_factory=dict)
    spans: dict[str, list[float]] = field(default_factory=dict)
    partial_lags: list[float] = field(default_factory=list)
    stable_lags: list[float] = field(default_factory=list)
    partial_painted: bool = False
    partial_judged: bool = False
    wrong_partial_paint: bool = False
    wrong_final_paint: bool = False
    painted: bool = False
    judged: bool = False
    paint_due: bool = False
    flips: int = 0
    visible_wrong_ms: list[float] = field(default_factory=list)
    unreplaced_wrong: int = 0
    tier2_activity: bool = False
    still_in_speech: bool | None = None
    unscorable: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _ms(marks: Mapping[str, int], a: str, b: str) -> float | None:
    if a in marks and b in marks:
        return (marks[b] - marks[a]) / 1e6
    return None


def measure(t: Trial, s: TrialScore) -> None:
    """Fill the latency, span, paint and sidecar fields of `s` from the trace."""
    line = t.line
    marks = dict(line.marks)
    for tier in tr.TIERS:
        # A tier's paint time is its earliest recorded (composited) paint; a paint mark with no
        # paint behind it is a webview or IPC time, never the paint time (PRD §10.4).
        mark = f"{tier}_paint"
        times = [p.mono_ns for p in line.paints if p.tier == tier]
        if times:
            marks[mark] = min(times)
        elif mark in marks:
            del marks[mark]
            s.issues.append(f"a {mark} mark but no {tier} paint recorded (left out)")
    for name, (a, b, _) in MARK_STAGES.items():
        v = _ms(marks, a, b)
        if v is not None:
            s.latencies[name] = v
    for name, (tier, _) in TIER_STAGES.items():
        hits = [p.mono_ns for p in line.paints if p.tier == tier and p.matches_final is True]
        if hits and "key_up" in marks:
            s.latencies[name] = (min(hits) - marks["key_up"]) / 1e6
    if t.sidecar is not None:
        s.still_in_speech = t.sidecar.still_in_speech_at_key_up
        aligned = True
        if "audio_first_sample" in marks and "key_up" in marks:
            offset_ms = (marks["key_up"] - marks["audio_first_sample"]) / 1e6
            aligned = abs(offset_ms - t.sidecar.key_up_ms) <= KEY_UP_TOLERANCE_MS
            if not aligned:
                s.issues.append(
                    "key-up is not where the take's sidecar puts it (first partial after onset "
                    "left out)"
                )
        if aligned and "audio_first_sample" in marks and "speech_first_partial" in marks:
            onset_ns = marks["audio_first_sample"] + t.sidecar.speech_onset_ms * 1e6
            s.latencies["first_partial_after_onset"] = (
                marks["speech_first_partial"] - onset_ns
            ) / 1e6
    for span in line.spans:
        if span.name == "partial_lag":
            s.partial_lags.append(span.dur_ns / 1e6)
        elif span.name == "stable_lag":
            s.stable_lags.append(span.dur_ns / 1e6)
        else:
            s.spans.setdefault(span.name, []).append(span.dur_ns / 1e6)
    s.tier2_activity = (
        any(m.startswith("tier2_") for m in line.marks)
        or any(span.name in TIER2_SPANS for span in line.spans)
        or any(p.tier == "tier2" for p in line.paints)
    )
    paints = sorted(line.paints, key=lambda p: p.mono_ns)
    s.painted = bool(paints)
    s.judged = line.outcome != "cancelled" and all(p.matches_final is not None for p in paints)
    partial = [p for p in paints if p.source == "partial"]
    s.partial_painted = bool(partial)
    s.partial_judged = bool(partial) and all(p.matches_final is not None for p in partial)
    s.wrong_partial_paint = any(p.matches_final is False for p in partial)
    s.wrong_final_paint = any(p.source == "final" and p.matches_final is False for p in paints)
    s.paint_due = line.outcome in ("painted", "error") or any(p.matches_final for p in paints)
    s.flips = sum(1 for a, b in pairwise(paints) if a.scene != b.scene)
    for i, p in enumerate(paints):
        if p.matches_final is False:
            nxt = next((q for q in paints[i + 1 :] if q.scene != p.scene), None)
            if nxt is None:
                s.unreplaced_wrong += 1
            else:
                s.visible_wrong_ms.append((nxt.mono_ns - p.mono_ns) / 1e6)
    if line.cache_state == "idle" and (
        line.idle_duration_ms is not None and line.idle_duration_ms < IDLE_MIN_MS
    ):
        s.issues.append("labelled idle, but idle for less than 30 min")
    if line.cache_state == "warm" and (
        line.idle_duration_ms is not None and line.idle_duration_ms >= WARM_MAX_MS
    ):
        s.issues.append("labelled warm, but idle for 60 s or more")
    if line.outcome == "cancelled":
        s.issues.append("superseded by a newer key press (its transcript may be cut short)")


def score_trial(
    t: Trial, reparse: Callable[[str, str, str | None], Mapping[str, Any]]
) -> TrialScore:
    """Score one trial: WER, the re-parse at the recorded context, and the measurements."""
    line, row, rp = t.line, t.row, t.replay
    ctx = {
        "place": line.context_viewpoint_id or "",
        "empty": "empty",
        "not_recorded": "not_recorded",
        "unlisted": "unlisted",
    }[line.context_kind]
    s = TrialScore(
        session=rp.session,
        trial=rp.trial,
        utterance=rp.utterance,
        take=rp.take,
        split=row["split"],
        category=category(row),
        follow_up=bool(row["context"]),
        setup=line.setup,
        state=line.cache_state,
        display=line.display,
        idle_duration_ms=line.idle_duration_ms,
        outcome=line.outcome,
        context=ctx,
    )
    measure(t, s)
    known_context = line.context_kind in ("place", "empty")
    if row["context"] and known_context:
        live = line.context_place_id or ""
        s.context_status = "matched" if live == row["context"] else "differed"
    elif "no-place" in check.tags_of(dict(row)) and line.context_kind == "place":
        s.context_status = "differed"
        s.context_reason = "expected an empty context: play it first after a launch"
    if t.sidecar is None:
        s.unscorable.append(t.sidecar_problem or "no sidecar")
    if t.transcript is None:
        s.unscorable.append("no transcript")
        return s
    text = t.transcript.final
    if len(text.split()) != line.transcript_words or len(text) != line.transcript_chars:
        s.issues.append("the transcript's lengths are not the trace's")
    try:
        s.wer_raw = wer.raw_errors(row["text"], text)
        s.wer_normalised = wer.normalised_errors(row["text"], text)
    except (ValueError, RecursionError):
        s.wer_raw = s.wer_normalised = None
        s.failures.append("WER not computed: the normaliser failed")
    blank = not text.strip()
    if blank and line.outcome not in ("empty", "cancelled", "error"):
        s.issues.append(f"an empty final transcript, but the app's outcome is {line.outcome}")
    if not blank and line.outcome == "empty":
        s.issues.append("the app's outcome is empty, but the final transcript has words")
    if blank:
        # Nothing was heard: wrong on place and time. Not re-parsed, so not in the fallback rate.
        s.issues.append("empty final transcript (scored wrong on place and time)")
        s.agreement, s.difference = "no", "the final transcript is empty"
        s.exact = s.exact_or_soft = s.place = False
        return s
    if not known_context:
        s.unscorable.append(f"context {line.context_kind}")
        return s
    out = reparse(text, row["clock"], line.context_viewpoint_id)
    if out.get("error") == "parser_failed":
        s.failures.append(f"re-parse failed: snow-globe-parse {out.get('failure', 'failed')}")
        return s
    cols = parser_columns(out)
    scored = dict(row)
    scored.update(cols)
    s.agreement, s.difference = check.compare(scored)
    s.parser_outcome, s.parser_viewpoint_id = cols["parser_outcome"], cols["parser_viewpoint_id"]
    s.place = place_right(row, cols)
    s.fallback = cols["parser_outcome"].split("/")[0] == "fallback"
    app, again = app_reading(line), reparse_reading(cols)
    if app is not None and again is not None and app != again:
        s.issues.append(f"the app's outcome says {app}, the re-parse {again}")
    if row["context"] and s.context_status == "differed":
        # bench/README.md: a live context that differs from the column makes the row wrong on
        # place, whatever the re-parse gives.
        s.place = False
        s.agreement = "no"
        s.difference = f"live context {ctx}, but the script's is {row['context']}" + (
            f"; {s.difference}" if s.difference else ""
        )
    s.exact = s.agreement == "yes"
    s.exact_or_soft = s.agreement in ("yes", "soft")
    return s


def explain_contexts(scores: Sequence[TrialScore], rows: Sequence[Mapping[str, str]]) -> None:
    """For each follow-up whose live context differed, say why (bench/README.md "Scoring"):
    `row-before-not-played` (the trial just before it in its session was not the script row
    before it, or ran another setup), `row-before-unscorable` (it was, but its place could not be
    judged: no transcript, no recorded context, or a failed re-parse), `row-before-misheard` (its
    re-parse did not resolve to the follow-up's place, or it heard nothing) or `app-lost-it` (it
    did resolve there, yet the app's context was another)."""
    by_key = {(s.session, s.trial): s for s in scores}
    order = {r["id"]: i for i, r in enumerate(rows)}
    for s in scores:
        if s.context_status != "differed" or s.context_reason:
            continue
        prev = by_key.get((s.session, s.trial - 1))
        row_before = rows[order[s.utterance] - 1]["id"] if order[s.utterance] else None
        if prev is None or prev.utterance != row_before or prev.setup != s.setup:
            s.context_reason = "row-before-not-played"
            continue
        if prev.exact is None:
            s.context_reason = "row-before-unscorable"
            continue
        place = (
            pr.SITES[prev.parser_viewpoint_id].place_id
            if (
                prev.parser_outcome
                and prev.parser_outcome.split("/")[0] in RESOLVED
                and prev.parser_viewpoint_id in pr.SITES
            )
            else None
        )
        expected = rows[order[s.utterance]]["context"]
        s.context_reason = "app-lost-it" if place == expected else "row-before-misheard"


# ---------------------------------------------------------------- reading a run


@dataclass
class Run:
    """The scorable trials of a run, and what was left out."""

    trials: list[Trial]
    lines: int = 0
    not_replay: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    transcripts_without_trace: int = 0
    synthetic_inputs: bool = False


def load_run(
    trace_paths: Sequence[Path],
    transcripts_path: Path | None,
    fixtures: Path | None,
    rows: Sequence[Mapping[str, str]],
    script_hash: str,
) -> Run:
    """Read and join every input (module doc, step 1). Malformed input raises tr.InputError. A
    transcript whose (session, trial) no trace line names is counted: its trial left no trace
    (the app may have died before writing it), so it is missing from every denominator."""
    place_ids = {vp: s.place_id for vp, s in pr.SITES.items()}
    by_id = {r["id"]: (i, r) for i, r in enumerate(rows)}
    transcripts = tr.read_transcripts(transcripts_path) if transcripts_path else {}
    run = Run(trials=[])
    seen: set[tuple[int, int]] = set()
    named: set[tuple[int, int]] = set()

    def reject(reason: str) -> None:
        run.rejected[reason] = run.rejected.get(reason, 0) + 1

    for path in trace_paths:
        for n, obj in tr.read_jsonl(path):
            run.lines += 1
            line = tr.parse_trace(obj, f"{path.name}:{n}", place_ids)
            rp = line.replay
            if rp is None:
                run.not_replay += 1
                continue
            named.add((rp.session, rp.trial))
            if rp.script_hash != script_hash:
                reject("another script version (script hash)")
                continue
            if rp.utterance not in by_id:
                reject("not a scripted row (real-use trials are scored apart: TODO(M1))")
                continue
            if (rp.session, rp.trial) in seen:
                reject("a second line for the same session and trial")
                continue
            seen.add((rp.session, rp.trial))
            index, row = by_id[rp.utterance]
            transcript = transcripts.get((rp.session, rp.trial))
            if transcript is not None and transcript.utterance not in (None, rp.utterance):
                reject("the transcript names another utterance")
                continue
            sidecar, problem = None, None
            if fixtures is None:
                problem = "no fixtures folder given"
            else:
                spath = tr.sidecar_path(fixtures, rp.script_hash, rp.utterance, rp.take)
                if spath.is_file():
                    sidecar = tr.read_sidecar(spath, rp.script_hash, rp.utterance, rp.take)
                else:
                    problem = "no sidecar"
            run.synthetic_inputs |= bool(
                (transcript and transcript.synthetic) or (sidecar and sidecar.synthetic)
            )
            run.trials.append(Trial(line, path.name, row, index, transcript, sidecar, problem))
    run.transcripts_without_trace = len(transcripts.keys() - named)
    run.synthetic_inputs |= any(t.synthetic for t in transcripts.values())
    return run


# ---------------------------------------------------------------- groups


def observation(s: TrialScore) -> stats.Observation:
    """A trial's values and ratio terms for the bootstrap (metric ids as in VALUE_METRICS and
    TRIAL_RATIOS; a ratio the trial is not counted in is left out of it)."""
    values: dict[str, list[float]] = {k: [v] for k, v in s.latencies.items()}
    values["partial_lag"] = s.partial_lags
    values["stable_lag"] = s.stable_lags
    values["visible_wrong"] = s.visible_wrong_ms
    for name, durations in s.spans.items():
        values[f"span:{name}"] = durations
    ratios: dict[str, tuple[float, float]] = {}

    def share(name: str, counted: bool, hit: bool | None) -> None:
        if counted:
            ratios[name] = (1.0 if hit else 0.0, 1.0)

    judged = s.exact is not None
    pending = s.category == "owner-pending"
    share("exact_non_pending", judged and not pending, s.exact)
    share("exact_settled", judged and s.category == "settled", s.exact)
    share("exact_text_no", judged and s.category == "text-level-no", s.exact)
    share("exact_pending", judged and pending, s.exact)
    share("exact_or_soft_non_pending", judged and not pending, s.exact_or_soft)
    share("place_non_pending", s.place is not None and not pending, s.place)
    share("place_settled", s.place is not None and s.category == "settled", s.place)
    share("place_pending", s.place is not None and pending, s.place)
    share("fallback", s.fallback is not None, s.fallback)
    if s.wer_raw is not None and s.wer_normalised is not None:
        ratios["wer_raw"] = (float(s.wer_raw.edits), float(s.wer_raw.reference_words))
        ratios["wer_normalised"] = (
            float(s.wer_normalised.edits),
            float(s.wer_normalised.reference_words),
        )
    share("wrong_paint_per_partial_paint", s.partial_judged, s.wrong_partial_paint)
    share("wrong_paint_per_trial", s.judged, s.wrong_partial_paint)
    share("wrong_final_paint", s.painted and s.judged, s.wrong_final_paint)
    share("partial_painted", s.judged, s.partial_painted)
    if s.painted:
        ratios["flips_per_query"] = (float(s.flips), 1.0)
    share("still_in_speech_per_trial", s.still_in_speech is not None, s.still_in_speech)
    share(
        "context_differed",
        s.follow_up and s.context_status is not None,
        s.context_status == "differed",
    )
    due = s.paint_due
    lat = s.latencies
    share("hit_tier0_300", due, lat.get("tier0", float("inf")) <= 300)
    share("hit_tier1_1000", due, lat.get("tier1", float("inf")) <= 1000)
    share("hit_tier1_400", due, lat.get("tier1", float("inf")) <= 400)
    share("hit_tier2_3000", due, lat.get("tier2", float("inf")) <= 3000)
    share(
        "hit_release_to_final_240", "release_to_final" in lat, lat.get("release_to_final", 0) <= 240
    )
    share(
        "hit_first_partial_300",
        "first_partial_after_onset" in lat,
        lat.get("first_partial_after_onset", 0) <= 300,
    )
    return stats.Observation(s.utterance, s.session, values, ratios)


def recording_observations(members: Sequence[TrialScore]) -> list[stats.Observation]:
    """One observation per recording (utterance and take) played in a group, for the ratios that
    are a property of the recording rather than of a replay of it (RECORDING_RATIOS; PRD §10.6
    "the share of utterances still in speech at key-up"). Clustered by utterance only: replaying
    a take in another session does not change it."""
    seen: dict[tuple[str, int], bool] = {}
    for s in members:
        if s.still_in_speech is not None:
            seen.setdefault((s.utterance, s.take), s.still_in_speech)
    return [
        stats.Observation(u, None, {}, {"still_in_speech_at_key_up": (float(v), 1.0)})
        for (u, _), v in sorted(seen.items())
    ]


@dataclass(frozen=True)
class GroupSpec:
    """Which trials a group holds."""

    setup: tr.Setup
    split: str
    state: str
    display: str
    subset: str = "all rows"

    @property
    def name(self) -> str:
        """A stable name (it also seeds the group's bootstrap)."""
        return f"{setup_name(self.setup)}|{self.split}|{self.state}|{self.display}|{self.subset}"

    def holds(self, s: TrialScore) -> bool:
        """Whether trial `s` belongs here."""
        return (
            s.setup == self.setup
            and s.split == self.split
            and self.state in ("all", s.state)
            and self.display in ("all", s.display)
            and (self.subset == "all rows" or s.follow_up)
        )


def setup_name(setup: tr.Setup) -> str:
    """A setup as one stable string."""
    return (
        f"{setup.engine}|pad={setup.silence_pad_ms}|retention={setup.model_retention}|"
        f"warm_up={setup.warm_up}|pre_render={setup.pre_render}|"
        f"partial_paint={setup.partial_paint}|paint_capture={setup.paint_capture}"
    )


def setup_json(setup: tr.Setup) -> dict[str, Any]:
    """A setup as JSON (`null` is "not recorded")."""
    return {
        "engine": setup.engine,
        "silence_pad_ms": setup.silence_pad_ms,
        "model_retention": setup.model_retention,
        "warm_up": setup.warm_up,
        "pre_render": setup.pre_render,
        "partial_paint": setup.partial_paint,
        "paint_capture": setup.paint_capture,
    }


def group_specs(scores: Sequence[TrialScore]) -> list[GroupSpec]:
    """Every non-empty group: per setup and split, all states and each state, each with all
    displays and each display; plus the idle follow-ups (D-068)."""
    specs: list[GroupSpec] = []
    setups = sorted({s.setup for s in scores}, key=tr.Setup.sort_key)
    for setup in setups:
        for split in SPLITS:
            for state in ("all", *STATES):
                for display in ("all", *DISPLAYS):
                    spec = GroupSpec(setup, split, state, display)
                    if any(spec.holds(s) for s in scores):
                        specs.append(spec)
            spec = GroupSpec(setup, split, "idle", "all", "follow-ups")
            if any(spec.holds(s) for s in scores):
                specs.append(spec)
    return specs


def _estimate_json(e: stats.Estimate) -> dict[str, Any]:
    return {
        "value": e.value,
        "ci": [e.low, e.high],
        "n": e.n,
        "valid_resamples": e.valid_resamples,
    }


def _wilson_json(k: float, n: float) -> dict[str, Any] | None:
    if n <= 0:
        return None
    k_i, n_i = round(k), round(n)
    lo, hi = stats.wilson(k_i, n_i)
    return {
        "k": k_i,
        "n": n_i,
        "rate": k_i / n_i,
        "two_sided_95": [lo, hi],
        "one_sided_95_upper": stats.wilson(k_i, n_i, stats.Z_ONE_SIDED_95)[1],
    }


def _totals(observations: Iterable[stats.Observation]) -> dict[str, list[float]]:
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for o in observations:
        for name, (num, den) in o.ratios.items():
            totals[name][0] += num
            totals[name][1] += den
    return totals


def summarise_group(
    spec: GroupSpec,
    members: Sequence[TrialScore],
    result: stats.BootstrapResult,
    recordings: stats.BootstrapResult,
    tier2_enabled: bool | None = None,
) -> dict[str, Any]:
    """The JSON for one group. `recordings` is the bootstrap of recording_observations();
    `tier2_enabled` is the run manifest's word on Tier 2, if it gives one."""
    totals = _totals(observation(s) for s in members)
    totals.update(_totals(recording_observations(members)))
    values = {
        name: {f"p{round(q * 100)}": _estimate_json(result.quantiles[(name, q)]) for q in QUANTILES}
        for name in VALUE_METRICS
        if result.quantiles[(name, QUANTILES[0])].n
    }
    if tier2_enabled is None:
        tier2_attempted, tier2_basis = any(s.tier2_activity for s in members), "trace activity"
    else:
        tier2_attempted, tier2_basis = tier2_enabled, "run manifest"
    estimates = {**result.ratios, **recordings.ratios}
    ratios = {
        name: {**_estimate_json(estimates[name]), "numerator": totals[name][0]}
        for name in RATIOS
        # Tier 2 is after the MSV core (PRD §9.6): a group that never tried it has no hit rate.
        if estimates[name].n and (tier2_attempted or name != "hit_tier2_3000")
    }
    reasons: dict[str, int] = defaultdict(int)
    for s in members:
        if s.context_status == "differed":
            reasons[s.context_reason or "unexplained"] += 1
    return {
        "name": spec.name,
        "setup": setup_json(spec.setup),
        "split": spec.split,
        "state": spec.state,
        "display": spec.display,
        "subset": spec.subset,
        "trials": len(members),
        "utterances": result.utterances,
        "sessions": result.sessions,
        "recordings": round(estimates["still_in_speech_at_key_up"].n),
        "bootstrap": {
            "method": result.method,
            "resamples": result.resamples,
            "seed": result.seed,
            "recordings_method": recordings.method,
            "recordings_seed": recordings.seed,
        },
        "values_ms": values,
        "ratios": ratios,
        "wilson": {
            name: _wilson_json(*totals[name])
            for name in ("wrong_paint_per_partial_paint", "wrong_paint_per_trial")
            if totals[name][1] > 0
        },
        "context_differed_by_reason": dict(sorted(reasons.items())),
        "display_asleep_trials": sum(s.display == "asleep" for s in members),
        "unreplaced_wrong_paints": sum(s.unreplaced_wrong for s in members),
        "tier2_attempted": tier2_attempted,
        "tier2_basis": tier2_basis,
    }


def _yes_no_mixed(flags: Iterable[bool | None]) -> str | None:
    seen = {f for f in flags if f is not None}
    if not seen:
        return None
    if len(seen) > 1:
        return "mixed"
    return "yes" if seen == {True} else "no"


def per_utterance(scores: Sequence[TrialScore], rows: Sequence[Mapping[str, str]]) -> list[dict]:
    """Per setup and row: counts and medians over every trial of the row (no bootstrap)."""
    by: dict[tuple[tr.Setup, str], list[TrialScore]] = defaultdict(list)
    for s in scores:
        by[(s.setup, s.utterance)].append(s)
    order = {r["id"]: i for i, r in enumerate(rows)}
    out = []
    for (setup, utt), members in sorted(
        by.items(), key=lambda kv: (kv[0][0].sort_key(), order[kv[0][1]])
    ):
        row = rows[order[utt]]
        scored = [s for s in members if s.exact is not None]

        def med(metric: str, ms: Sequence[TrialScore] = members) -> float | None:
            vals = [s.latencies[metric] for s in ms if metric in s.latencies]
            return median(vals) if vals else None

        raw = [s.wer_raw for s in members if s.wer_raw]
        norm = [s.wer_normalised for s in members if s.wer_normalised]
        out.append(
            {
                "setup": setup_json(setup),
                "id": utt,
                "split": row["split"],
                "category": category(row),
                "text": row["text"],
                "trials": len(members),
                "scored": len(scored),
                "exact": sum(bool(s.exact) for s in scored),
                "place": sum(bool(s.place) for s in scored),
                "wer_raw": wer.corpus_wer(raw),
                "wer_normalised": wer.corpus_wer(norm),
                "tier0_p50_ms": med("tier0"),
                "release_to_final_p50_ms": med("release_to_final"),
                "first_partial_after_onset_p50_ms": med("first_partial_after_onset"),
                "wrong_partial_paints": sum(s.wrong_partial_paint for s in members),
                "partial_paint_trials": sum(s.partial_judged for s in members),
                "still_in_speech_at_key_up": _yes_no_mixed(s.still_in_speech for s in members),
                "context_differed": sum(s.context_status == "differed" for s in members),
                "failures": sum(bool(s.failures) for s in members),
                "agreements": dict(
                    sorted(
                        (a, sum(s.agreement == a for s in scored))
                        for a in {s.agreement for s in scored}
                    )
                ),
            }
        )
    return out


def trial_json(s: TrialScore) -> dict[str, Any]:
    """One trial's record for the JSON report (ids, verdicts and numbers; no transcript)."""
    return {
        "session": s.session,
        "trial": s.trial,
        "utterance": s.utterance,
        "take": s.take,
        "split": s.split,
        "category": s.category,
        "setup": setup_json(s.setup),
        "state": s.state,
        "display": s.display,
        "idle_duration_ms": s.idle_duration_ms,
        "outcome": s.outcome,
        "context": s.context,
        "context_status": s.context_status,
        "context_reason": s.context_reason,
        "parser_outcome": s.parser_outcome,
        "parser_viewpoint_id": s.parser_viewpoint_id,
        "agreement": s.agreement,
        "difference": s.difference,
        "place": s.place,
        "wer_raw": None if s.wer_raw is None else [s.wer_raw.edits, s.wer_raw.reference_words],
        "wer_normalised": None
        if s.wer_normalised is None
        else [s.wer_normalised.edits, s.wer_normalised.reference_words],
        "latencies_ms": s.latencies,
        "partial_lags_ms": s.partial_lags,
        "wrong_partial_paint": s.wrong_partial_paint,
        "flips": s.flips,
        "still_in_speech_at_key_up": s.still_in_speech,
        "unscorable": s.unscorable,
        "issues": s.issues,
        "failures": s.failures,
    }


# ---------------------------------------------------------------- the report


@dataclass(frozen=True)
class Provenance:
    """Where the inputs came from, as far as the scorer can tell."""

    kind: str
    statement: str
    manifest: Mapping[str, Any] | None


def provenance(manifest: Mapping[str, Any] | None, synthetic_inputs: bool) -> Provenance:
    """`synthetic` if the run manifest or any transcript or sidecar says so; `measured` only when
    the manifest says so and nothing is marked synthetic; else `unknown`."""
    if synthetic_inputs or (manifest and manifest.get("kind") == "synthetic"):
        return Provenance(
            "synthetic",
            "SYNTHETIC FIXTURE: made-up traces, transcripts and sidecars (bench/replay/"
            "synthetic.py). Not a measurement: no number below describes Snow Globe, the speech "
            "engines or this Mac.",
            manifest,
        )
    if manifest and manifest.get("kind") == "measured":
        return Provenance(
            "measured",
            f"Measured run of build {manifest.get('build_commit')}, as its run manifest states.",
            manifest,
        )
    return Provenance(
        "unknown",
        "No run manifest: the inputs' origin is unknown, so this is not a measured result.",
        manifest,
    )


def build_report(
    run: Run,
    scores: Sequence[TrialScore],
    rows: Sequence[Mapping[str, str]],
    *,
    script_hash: str,
    resamples: int,
    seed: int,
    origin: Provenance,
    inputs: Mapping[str, Any],
    jobs: int = 1,
) -> dict[str, Any]:
    """The report as one JSON-ready dict (report.py renders it as Markdown)."""
    specs = group_specs(scores)
    qm = {name: QUANTILES for name in VALUE_METRICS}
    manifest = origin.manifest or {}
    tier2_enabled = manifest.get("tier2_enabled")
    tasks = []
    for spec in specs:
        members = [s for s in scores if spec.holds(s)]
        tasks.append((spec, members))
    jobs_args = []
    for spec, members in tasks:
        jobs_args.append(
            (
                [observation(s) for s in members],
                qm,
                TRIAL_RATIOS,
                resamples,
                stats.group_seed(seed, spec.name),
            )
        )
        jobs_args.append(
            (
                recording_observations(members),
                {},
                RECORDING_RATIOS,
                resamples,
                stats.group_seed(seed, f"{spec.name}|recordings"),
                False,
            )
        )
    if jobs > 1 and len(jobs_args) > 2:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(stats.bootstrap, *a) for a in jobs_args]
            results = [f.result() for f in futures]
    else:
        results = [stats.bootstrap(*a) for a in jobs_args]
    groups = [
        summarise_group(spec, members, results[2 * i], results[2 * i + 1], tier2_enabled)
        for i, (spec, members) in enumerate(tasks)
    ]
    unscorable: dict[str, int] = defaultdict(int)
    issues: dict[str, int] = defaultdict(int)
    failures: dict[str, int] = defaultdict(int)
    for s in scores:
        for reason in s.unscorable:
            unscorable[reason] += 1
        for issue in s.issues:
            issues[issue] += 1
        for failure in s.failures:
            failures[failure] += 1
    return {
        "report": "Snow Globe replay report",
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provenance": {
            "kind": origin.kind,
            "statement": origin.statement,
            "run_manifest": origin.manifest,
        },
        "inputs": dict(inputs),
        "script": {
            "hash": script_hash,
            "rows": len(rows),
            "owner_pending": sorted(r["id"] for r in rows if category(r) == "owner-pending"),
            "text_level_no": sorted(r["id"] for r in rows if category(r) == "text-level-no"),
        },
        "method": {
            "bootstrap": f"seeded cluster bootstrap, {resamples} resamples, base seed {seed}; "
            "clusters are utterances, and utterances x sessions (pigeonhole, Owen 2007) when a "
            "group spans two or more harness sessions; the share of utterances still in speech "
            "at key-up is resampled by utterance over the takes played; 95 % percentile intervals",
            "quantiles": "type 7 (linear between closest ranks)",
            "wilson": "score interval without continuity correction; two-sided 95 % "
            f"(z = {stats.Z_TWO_SIDED_95:.6f}) and one-sided 95 % upper (z = "
            f"{stats.Z_ONE_SIDED_95:.6f})",
            "wer": "corpus WER, Σ(S + D + I) / ΣN over trials; raw and normalised (wer.py: N is "
            "the reference's words with numbers spelt out, and a place said in any alias of the "
            "reference's place is no error)",
            "exact": "check.py compare() of the re-parse at the row's reference clock and the "
            "trace's recorded context; a follow-up with a live context other than the "
            "script's is wrong on place; an empty final transcript is wrong on place and time",
            "paint_time": "a tier's earliest recorded paint (a paint mark with no paint is left "
            "out)",
            "key_up_tolerance_ms": KEY_UP_TOLERANCE_MS,
            "targets": "latency budgets against idle trials (PRD §10.1); exact place and time, "
            "the fallback rate and wrong paints against the whole test split (PRD §14, §7.1)",
            "known_bias": KNOWN_BIAS,
        },
        "lines": {
            "read": run.lines,
            "not_replay": run.not_replay,
            "rejected": dict(sorted(run.rejected.items())),
            "trials": len(scores),
            "transcripts_without_trace": run.transcripts_without_trace,
            "unscorable": dict(sorted(unscorable.items())),
            "issues": dict(sorted(issues.items())),
            "failures": dict(sorted(failures.items())),
            "trials_with_failures": sum(bool(s.failures) for s in scores),
        },
        "labels": {"values": {k: LABELS[k] for k in VALUE_METRICS}, "ratios": RATIOS},
        "hit_rates": HIT_RATES,
        "targets": [{"metric": m, "quantile": q, "target": t, "on": on} for m, q, t, on in TARGETS],
        "groups": groups,
        "per_utterance": per_utterance(scores, rows),
        "trials": [trial_json(s) for s in sorted(scores, key=lambda s: (s.session, s.trial))],
    }


def exit_status(doc: Mapping[str, Any]) -> int:
    """0, or 1 when a stage of the scorer failed on some trial (module doc)."""
    return 1 if doc["lines"]["trials_with_failures"] else 0


def score_run(
    run: Run,
    rows: Sequence[Mapping[str, str]],
    reparse: Callable[[str, str, str | None], Mapping[str, Any]],
) -> list[TrialScore]:
    """Score every trial, then explain the follow-ups whose context differed."""
    scores = [score_trial(t, reparse) for t in run.trials]
    explain_contexts(scores, rows)
    return scores


def sha256(path: Path) -> str:
    """Hex SHA-256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_report(doc: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    """Write replay-report.json and replay-report.md into `out`. A number that is not finite
    is refused (ValueError) rather than written as a bare NaN, which is not JSON."""
    text = json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False) + "\n"
    md = report.markdown(doc)
    out.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out / "replay-report.json", out / "replay-report.md"
    json_path.write_text(text, encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    return json_path, md_path


def inside(path: Path, folder: Path) -> bool:
    """Whether `path` is `folder` or inside it, after resolving links."""
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def main(argv: Sequence[str]) -> int:
    """Score a replay run and write the report (module doc)."""
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--traces", type=Path, nargs="+", required=True, help="trace JSONL files")
    ap.add_argument("--transcripts", type=Path, help="the run's transcripts JSONL")
    ap.add_argument("--fixtures", type=Path, help="the voice-fixtures folder (sidecars only)")
    ap.add_argument("--run", type=Path, help="the run manifest (run.json)")
    ap.add_argument("--out", type=Path, required=True, help="folder for the two report files")
    ap.add_argument("--bin", help="path to snow-globe-parse")
    ap.add_argument("--allow-stale-binary", action="store_true")
    ap.add_argument(
        "--allow-other-parser",
        action="store_true",
        help="re-parse a measured run with a parser other than its build's (the report says so)",
    )
    ap.add_argument("--resamples", type=int, default=stats.MIN_RESAMPLES)
    ap.add_argument("--seed", type=int, default=20261122)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = ap.parse_args(list(argv))

    if args.resamples < stats.MIN_RESAMPLES:
        print(f"--resamples must be at least {stats.MIN_RESAMPLES}", file=sys.stderr)
        return 1
    if inside(args.out, pr.REPO):
        print(
            "--out is inside the repository; reports go beside the run, outside git",
            file=sys.stderr,
        )
        return 1
    binary = check.find_binary(args.bin)
    if not binary.exists():
        print(f"snow-globe-parse not found at {binary}; build it (see check.py)", file=sys.stderr)
        return 2
    newer = check.newer_sources(binary)
    if newer and not args.allow_stale_binary:
        print(
            f"snow-globe-parse at {binary} is older than {', '.join(newer[:5])}; rebuild it",
            file=sys.stderr,
        )
        return 2
    rows, clocks = check.load()
    script_hash = check.script_hash(rows)
    try:
        manifest = tr.read_run_manifest(args.run) if args.run else None
    except (tr.InputError, OSError) as e:
        print(f"score.py: {e}", file=sys.stderr)
        return 1
    binary_sha = sha256(binary)
    run_parser = (manifest or {}).get("parser_sha256")
    matches_run = None if run_parser is None else run_parser == binary_sha
    measured = (manifest or {}).get("kind") == "measured"
    if matches_run is False and measured and not args.allow_other_parser:
        print(
            f"score.py: snow-globe-parse at {binary} (SHA-256 {binary_sha[:12]}…) is not the "
            f"run's parser ({run_parser[:12]}…); build the run's commit "
            f"{(manifest or {}).get('build_commit')}, or pass --allow-other-parser",
            file=sys.stderr,
        )
        return 2
    try:
        run = load_run(args.traces, args.transcripts, args.fixtures, rows, script_hash)
    except (tr.InputError, OSError) as e:
        print(f"score.py: {e}", file=sys.stderr)
        return 1
    if not run.trials:
        print("score.py: no replay trial could be read", file=sys.stderr)
        return 1
    reparse = Reparser(binary, clocks)
    scores = score_run(run, rows, reparse)
    origin = provenance(manifest, run.synthetic_inputs)
    doc = build_report(
        run,
        scores,
        rows,
        script_hash=script_hash,
        resamples=args.resamples,
        seed=args.seed,
        origin=origin,
        jobs=args.jobs,
        inputs={
            "traces": [str(p) for p in args.traces],
            "transcripts": str(args.transcripts) if args.transcripts else None,
            "fixtures": str(args.fixtures) if args.fixtures else None,
            "run_manifest": str(args.run) if args.run else None,
            "parser_binary": str(binary),
            "parser_sha256": binary_sha,
            "run_parser_sha256": run_parser,
            "parser_matches_run": matches_run,
            "parser_older_than_sources": newer,
            "parser_runs": reparse.calls,
        },
    )
    json_path, md_path = write_report(doc, args.out)
    print(f"{origin.kind}: {len(scores)} trials scored; wrote {md_path} and {json_path}")
    status = exit_status(doc)
    if status:
        print(
            f"score.py: a stage failed on {doc['lines']['trials_with_failures']} trials "
            f"({'; '.join(doc['lines']['failures'])}); they are left out of the figures it "
            "feeds, and the report says so at its top",
            file=sys.stderr,
        )
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
