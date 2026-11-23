"""A synthetic replay run, for testing and demonstrating the scorer (PRD §10.6). NOT A MEASUREMENT.

    python3 -I -B bench/replay/synthetic.py --out DIR [--seed N]

writes, under DIR (which must be outside the repository and outside the data dir's fixtures):

- `traces.jsonl`: schema-2 trace lines (app/core/src/trace.rs), one per trial;
- `transcripts.jsonl`: one made-up "final transcript" per trial, each marked `"synthetic": true`;
- `fixtures/scripted/<script hash>/<id>.1.json`: one made-up sidecar per row, marked
  `"synthetic": true`; there is no audio, and none is ever written;
- `run.json`: `"kind": "synthetic"` and what was made up.

DIR may be new, empty, or an earlier synthetic run (its files are replaced). Anything else that
those writes would replace is refused: a run.json that is not synthetic's, traces or transcripts
without one, and any file under DIR/fixtures that is not a synthetic sidecar.

Every number is drawn from fixed, made-up distributions (seeded), chosen to exercise every path of
the report: two engine setups, five sessions, all five states, the display asleep and on, paints
from partials (some wrong), follow-ups whose context the app lost or whose row before was
misheard, still-in-speech takes, an empty transcript and a mislabelled idle trial.

**Split discipline** (bench/README.md): a test row is played exactly as check.py parses it. Its
made-up transcript is the script's own text, and its trace's context is the script's context
column: empty for a row that is not a follow-up, else that place's viewpoint (the one
snow-globe-parse's `--context <place>` picks). So its re-parse is the very run whose output
scripted.tsv already stores. The app's running last place, every mishearing and every context
anomaly are on dev rows only. Scoring this run therefore reveals nothing about the parser on the
test rows that check.py has not already shown.

A trace's outcome follows the parser output recorded for the row's own text, not the made-up
transcript, so on the misheard dev rows the scorer may flag that the app's outcome and the
re-parse disagree: that exercises the check, and is no finding about the parser.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "utterances"))

import check
import prd_reading as pr
import traces as tr

DEFAULT_SEED = 1

#: (engine, silence pad ms, model retention, sessions): the first is the "shipped" setup.
ENGINE_SETUPS: tuple[tuple[str, int, str, tuple[int, ...]], ...] = (
    ("dictation_transcriber", 0, "lingering", (1, 2, 3)),
    ("parakeet", 0, "process_lifetime", (4, 5)),
)

#: The made-up warm-up and levers, the same in every trial (the scorer groups by them).
WARM_UP = True
LEVERS = {"pre_render": True, "partial_paint": True, "paint_capture": True}

#: The files generate() writes directly under DIR.
RUN_FILES = ("traces.jsonl", "transcripts.jsonl", "run.json")

#: Made-up medians (ms) by state for the speech stages, and their log-normal spread.
RELEASE_TO_FINAL_MS = {
    "idle": 210,
    "warm": 150,
    "cold": 260,
    "after_wake": 240,
    "after_launch": 300,
}
FIRST_PARTIAL_MS = {"idle": 260, "warm": 170, "cold": 300, "after_wake": 280, "after_launch": 340}
SPREAD = 0.35
DISPLAY_WAKE_MS = 140

#: Dev-row mishearings: id -> made-up final transcript. A dev row not listed is heard as written,
#: or with its numbers in another spelling.
DEV_MISHEARINGS = {
    "d03": "Mount Rainier at sun rise",
    "d09": "grand canyon at nine",
    "d12": "Grand Teton at the sunset",
    "d15": "Yosemite at sunset to morrow",
    "d19": "bar harbor at two",
    "d21": "the smokies at sunset please",
    "d24": "big bend at one p.m.",
    "d26": "panther junction golden hour",
}

#: Dev rows whose made-up transcript loses the place word in one session (session -> ids): the
#: parser then misses, so the app keeps its earlier last place.
DEV_PLACE_LOST = {2: ("d05",)}

#: Dev follow-ups whose context the app "loses" in one session (session -> ids).
DEV_CONTEXT_LOST = {3: ("d27",)}


@dataclass
class Clock:
    """A per-session monotonic clock in nanoseconds, with its wall-clock anchor."""

    anchor: datetime
    mono_ns: int = 0

    def wall(self, mono_ns: int) -> str:
        """RFC 3339 UTC with microseconds, as trace.rs writes it."""
        t = self.anchor + timedelta(microseconds=mono_ns // 1000)
        return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def lognormal(rng: random.Random, median_ms: float, spread: float = SPREAD) -> float:
    """A made-up latency: log-normal with this median."""
    return median_ms * math.exp(rng.gauss(0.0, spread))


def ns(ms_value: float) -> int:
    """Milliseconds to whole nanoseconds."""
    return round(ms_value * 1e6)


def script_context(row: Mapping[str, str]) -> str | None:
    """The viewpoint check.py's `--context` gives a row: none, or its context place's viewpoint
    (snow-globe-parse takes a place's default viewpoint; every manifest place has one)."""
    if not row["context"]:
        return None
    viewpoints = [vp for vp, s in pr.SITES.items() if s.place_id == row["context"]]
    if len(viewpoints) != 1:
        raise ValueError(f"{row['id']}: context {row['context']!r} is not a one-viewpoint place")
    return viewpoints[0]


def resolved_place(row: Mapping[str, str]) -> str | None:
    """The place the recorded parser output gives for a row's own text (scripted.tsv), if it
    resolves to one: what the made-up app session keeps as its last place."""
    outcome = row["parser_outcome"].split("/")[0]
    vp = row["parser_viewpoint_id"]
    if outcome in ("query", "out-of-range-past", "out-of-range-beyond") and vp in pr.SITES:
        return vp
    return None


def made_up_transcript(row: Mapping[str, str], session: int, rng: random.Random) -> str:
    """A made-up final transcript: the script text for test rows (split discipline), and for
    dev rows a listed mishearing, a lost place word, an empty one, or the text with its numbers
    spelt another way."""
    text = row["text"]
    if row["split"] == "test":
        return text
    if row["id"] in DEV_PLACE_LOST.get(session, ()):
        return "point blank at sunrise tomorrow"
    if row["id"] == "d16" and session == 1:
        return ""
    if row["id"] in DEV_MISHEARINGS and session != 3:
        return DEV_MISHEARINGS[row["id"]]
    if rng.random() < 0.5:
        return check.spoken(text)
    return text


def sidecar(row: Mapping[str, str], script_hash: str, seed: int) -> dict[str, Any]:
    """A made-up sidecar for take 1 of a row (bench/README.md "Where recordings live"): offsets
    in ms from the recording's first sample, never a device name."""
    rng = random.Random(f"{seed}:sidecar:{row['id']}")
    words = max(1, len(check.spoken(row["text"]).split()))
    onset = rng.uniform(140, 360)
    speech = words * rng.uniform(260, 340)
    key_up = onset + speech + rng.gauss(120, 110)
    end = onset + speech
    key_up = max(key_up, onset + 200)
    return {
        "synthetic": True,
        "id": row["id"],
        "take": 1,
        "script_hash": script_hash,
        "recorded_at": "2026-11-23T03:00:00Z",
        "channels": 1,
        "sample_rate_hz": 48000,
        "sample_format": "f32",
        "frames": round((key_up + 40) * 48),
        "duration_ms": round(key_up + 40, 1),
        "key_down_ms": -35.0,
        "key_up_ms": round(key_up, 1),
        "speech_onset_ms": round(onset, 1),
        "speech_end_ms": round(end, 1),
        "input_kind": "built_in",
        "consent_scope": "replay-on-device",
    }


def choose_state(rng: random.Random, first: bool) -> tuple[str, int | None]:
    """A made-up state and idle duration (ms) for a trial."""
    if first:
        return "after_launch", None
    r = rng.random()
    if r < 0.62:
        return "idle", rng.randint(30 * 60_000, 75 * 60_000)
    if r < 0.78:
        return "warm", rng.randint(5_000, 55_000)
    if r < 0.89:
        return "cold", rng.randint(60_000, 20 * 60_000)
    return "after_wake", rng.randint(2 * 3_600_000, 6 * 3_600_000)


def trace_line(
    *,
    clock: Clock,
    start_ns: int,
    query_gen: int,
    row: Mapping[str, str],
    transcript: str,
    side: Mapping[str, Any],
    context: str | None,
    state: str,
    idle_ms: int | None,
    display: str,
    engine: tuple[str, int, str],
    replay: Mapping[str, Any],
    resolves: bool,
    rng: random.Random,
) -> tuple[dict[str, Any], int]:
    """One made-up schema-2 trace line, in trace.rs's key order, and the trial's end (ns)."""
    marks: dict[str, int] = {}
    spans: list[dict[str, Any]] = []
    paints: list[dict[str, Any]] = []

    def span(name: str, start: int, end: int, seq: int | None = None) -> None:
        end = max(end, start)
        s: dict[str, Any] = {"name": name, "start_ns": start, "end_ns": end, "dur_ns": end - start}
        if seq is not None:
            s["seq"] = seq
        spans.append(s)

    kd = start_ns
    marks["key_down"] = kd
    span("os_event_to_handler", kd, kd + ns(rng.uniform(0.2, 0.6)))
    span("panel_reveal", kd + ns(0.8), kd + ns(0.8 + lognormal(rng, 9)))
    afs = kd + ns(lognormal(rng, 45 if state in ("idle", "after_wake") else 30))
    marks["audio_first_sample"] = afs
    key_up = afs + ns(side["key_up_ms"])
    for seq, t in enumerate(range(afs, key_up, ns(100))):
        span("audio_buffer_delivery", t, t + ns(rng.uniform(0.05, 0.4)), seq)
    speaking = transcript.strip() != ""
    partials = 0
    stable_arrivals: list[int] = []
    if speaking:
        # The first PARTIAL arrives a made-up first-partial latency after the speech onset, and
        # covers audio up to its arrival less its lag; later ones cover 220-320 ms more each.
        first_ms = side["speech_onset_ms"] + lognormal(rng, FIRST_PARTIAL_MS[state])
        lag = min(lognormal(rng, 120), first_ms - side["speech_onset_ms"] - 20.0)
        audio_end_ms = first_ms - lag
        last_arrival = 0
        while True:
            arrival = max(afs + ns(audio_end_ms + lag), last_arrival + ns(1))
            if partials and arrival > key_up + ns(300):
                break
            span("partial_lag", afs + ns(audio_end_ms), arrival, partials)
            if partials == 0:
                marks["speech_first_partial"] = arrival
            if partials % 2 == 1:
                stable = arrival + ns(lognormal(rng, 80))
                span("stable_lag", afs + ns(audio_end_ms), stable, partials // 2)
                stable_arrivals.append(stable)
            last_arrival = arrival
            partials += 1
            audio_end_ms += rng.uniform(220, 320)
            if audio_end_ms > side["speech_end_ms"]:
                break
            lag = lognormal(rng, 120)
        if resolves:
            first = marks["speech_first_partial"]
            marks["place_matched"] = first + ns(lognormal(rng, 90))
            marks["assets_ready"] = marks["place_matched"] + ns(lognormal(rng, 160))
    marks["key_up"] = key_up
    final = key_up + ns(lognormal(rng, RELEASE_TO_FINAL_MS[state]))
    marks["speech_final"] = final
    outcome = "empty"
    path = None
    end = final
    if speaking:
        parsed = final + ns(rng.uniform(0.08, 0.3))
        marks["parsed"] = parsed
        path = "fast_path"
        end = parsed
        expected = row["parser_outcome"].split("/")[0]
        if resolves and expected == "query":
            outcome = "painted"
            marks["forecast_ready"] = parsed + ns(lognormal(rng, 2.5))
            wake = DISPLAY_WAKE_MS if display == "asleep" else 0
            if stable_arrivals and rng.random() < 0.35:
                # Painted from the last stable partial before key-up (else the first after it).
                before = [s for s in stable_arrivals if s <= key_up]
                at = (before[-1] if before else stable_arrivals[0]) + ns(lognormal(rng, 30) + wake)
                wrong = rng.random() < 0.08
                paints.append(
                    {
                        "tier": "tier0",
                        "source": "partial",
                        "scene": 0,
                        "matches_final": not wrong,
                        "mono_ns": at,
                    }
                )
            scene = 1 if paints and paints[0]["matches_final"] is False else 0
            t0 = parsed + ns(lognormal(rng, 40) + wake)
            t1 = parsed + ns(lognormal(rng, 190) + wake)
            if not paints or paints[0]["matches_final"] is False:
                paints.append(
                    {
                        "tier": "tier0",
                        "source": "final",
                        "scene": scene,
                        "matches_final": True,
                        "mono_ns": t0,
                    }
                )
            paints.append(
                {
                    "tier": "tier1",
                    "source": "final",
                    "scene": scene,
                    "matches_final": True,
                    "mono_ns": t1,
                }
            )
            for p in paints:
                mark = {"tier0": "tier0_paint", "tier1": "tier1_paint"}[p["tier"]]
                marks[mark] = min(marks.get(mark, p["mono_ns"]), p["mono_ns"])
            end = max(p["mono_ns"] for p in paints)
        elif expected.startswith("out-of-range"):
            outcome = "out_of_range"
        elif expected == "fallback":
            outcome, path = "fallback", "fallback"
        else:
            outcome = "no_match"
    order = {name: i for i, name in enumerate(tr.MARKS)}
    line: dict[str, Any] = {
        "schema": tr.SCHEMA,
        "query_gen": query_gen,
        "outcome": outcome,
    }
    if path:
        line["path"] = path
    if context is None:
        line["context"] = {"kind": "empty"}
    else:
        line["context"] = {
            "kind": "place",
            "place_id": pr.SITES[context].place_id,
            "viewpoint_id": context,
        }
    line["speech"] = {
        "engine": engine[0],
        "silence_pad_ms": engine[1],
        "model_retention": engine[2],
        "warm_up": WARM_UP,
    }
    line["levers"] = dict(LEVERS)
    line["marks"] = {
        name: {"mono_ns": v, "wall": clock.wall(v)}
        for name, v in sorted(marks.items(), key=lambda kv: order[kv[0]])
    }
    line["spans"] = spans
    line["paints"] = paints
    line["state"] = {
        "idle_duration_ms": idle_ms,
        "cache_state": state,
        "thermal_state": "nominal",
        "power_source": "ac",
        "network_interface": "wifi",
        "display": display,
    }
    line["lengths"] = {
        "transcript_chars": len(transcript),
        "transcript_words": len(transcript.split()),
        "partials": partials,
    }
    line["replay"] = dict(replay)
    return line, end


def generate(out: Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Write a synthetic run under `out` (module doc) and return its run manifest. Raises
    ValueError, writing nothing, for a folder that `refuse()` refuses."""
    why = refuse(out)
    if why:
        raise ValueError(f"{out} is {why}")
    rows, _ = check.load()
    script_hash = check.script_hash(rows)
    fixtures = out / "fixtures" / "scripted" / script_hash
    fixtures.mkdir(parents=True, exist_ok=True)
    sides = {r["id"]: sidecar(r, script_hash, seed) for r in rows}
    for rid, side in sides.items():
        (fixtures / f"{rid}.1.json").write_text(json.dumps(side, indent=1) + "\n", "utf-8")
    by_id = {r["id"]: r for r in rows}
    # t100 expects an empty context, so it is played first after each launch (bench/README.md).
    play_order = [by_id["t100"], *(r for r in rows if r["id"] != "t100")]
    trace_lines, transcript_lines = [], []
    trials = 0
    for engine, pad, retention, sessions in ENGINE_SETUPS:
        for session in sessions:
            rng = random.Random(f"{seed}:session:{session}")
            clock = Clock(datetime(2026, 11, 24, 3, 0, tzinfo=UTC) + timedelta(days=session))
            mono = ns(4_000)
            last: str | None = None
            mislabelled = False
            for n, row in enumerate(play_order, 1):
                state, idle_ms = choose_state(rng, first=n == 1)
                if state == "idle" and not mislabelled and row["split"] == "dev":
                    idle_ms, mislabelled = 25 * 60_000, True  # a made-up harness mistake
                display = "asleep" if state == "idle" and rng.random() < 0.8 else "on"
                transcript = made_up_transcript(row, session, rng)
                # Split discipline: a test row is played at the script's own context.
                context = script_context(row) if row["split"] == "test" else last
                if row["id"] in DEV_CONTEXT_LOST.get(session, ()):
                    context = None
                garbled = transcript != row["text"] and (
                    row["id"] in DEV_PLACE_LOST.get(session, ()) or not transcript.strip()
                )
                # A follow-up names no place: with the context lost it resolves to none.
                lost = bool(row["context"]) and context is None
                place = None if garbled or lost else resolved_place(row)
                mono += ns(idle_ms if idle_ms is not None else 5_000)
                line, end = trace_line(
                    clock=clock,
                    start_ns=mono,
                    query_gen=n,
                    row=row,
                    transcript=transcript,
                    side=sides[row["id"]],
                    context=context,
                    state=state,
                    idle_ms=idle_ms,
                    display=display,
                    engine=(engine, pad, retention),
                    replay={
                        "script_hash": script_hash,
                        "utterance": row["id"],
                        "take": 1,
                        "session": session,
                        "trial": n,
                    },
                    resolves=place is not None,
                    rng=rng,
                )
                mono = end + ns(1_000)
                if place is not None:
                    last = place
                trace_lines.append(line)
                transcript_lines.append(
                    {
                        "session": session,
                        "trial": n,
                        "utterance": row["id"],
                        "final": transcript,
                        "synthetic": True,
                    }
                )
                trials += 1
    with (out / "traces.jsonl").open("w", encoding="utf-8") as f:
        for line in trace_lines:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")
    with (out / "transcripts.jsonl").open("w", encoding="utf-8") as f:
        for line in transcript_lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    manifest = {
        "kind": "synthetic",
        "statement": "Made up by bench/replay/synthetic.py to test the scorer. Not a "
        "measurement: no audio, no app, no speech engine and no Mac were involved. Test rows "
        "are played at the script's own text and context (split discipline), so their WER is 0 "
        "and their accuracy is the parser's text-level result already in scripted.tsv; the "
        "mishearings and context anomalies are all on dev rows.",
        "seed": seed,
        "script_hash": script_hash,
        "trials": trials,
        "engine_setups": [
            {"engine": e, "silence_pad_ms": p, "model_retention": r, "sessions": list(s)}
            for e, p, r, s in ENGINE_SETUPS
        ],
        "made_up_medians_ms": {
            "release_to_final": RELEASE_TO_FINAL_MS,
            "first_partial_after_onset": FIRST_PARTIAL_MS,
            "display_wake": DISPLAY_WAKE_MS,
        },
    }
    (out / "run.json").write_text(json.dumps(manifest, indent=1) + "\n", "utf-8")
    return manifest


def _synthetic_json(path: Path) -> bool:
    """Whether `path` is a JSON object that says `"synthetic": true`."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return isinstance(obj, dict) and obj.get("synthetic") is True


def refuse(out: Path) -> str | None:
    """Why `out` may not hold a synthetic run, else None: inside the repository; inside the data
    dir's fixtures folder, where real recordings live; or holding a file that generate() would
    replace and that an earlier synthetic run did not write (a run.json whose kind is not
    synthetic, traces or transcripts with no run.json, or anything under fixtures/ that is not a
    synthetic sidecar), so a real run's traces, transcripts or sidecars are never overwritten."""
    resolved = out.resolve()
    for folder, why in (
        (pr.REPO, "inside the repository"),
        (check.data_dir() / "fixtures", "inside the data dir's fixtures, beside real recordings"),
    ):
        try:
            resolved.relative_to(folder.resolve())
        except ValueError:
            continue
        return why
    if not out.exists():
        return None
    if not out.is_dir():
        return "not a folder"
    run_json = out / "run.json"
    if run_json.exists():
        try:
            kind = json.loads(run_json.read_text(encoding="utf-8")).get("kind")
        except (OSError, UnicodeDecodeError, ValueError, AttributeError):
            kind = None
        if kind != "synthetic":
            return "a folder whose run.json is not a synthetic run's"
    elif any((out / name).exists() for name in RUN_FILES):
        return "a folder holding traces or transcripts with no synthetic run.json"
    fixtures = out / "fixtures"
    if fixtures.exists():
        for path in fixtures.rglob("*"):
            if path.is_dir() or path.name == ".DS_Store":
                continue
            if path.suffix != ".json" or not _synthetic_json(path):
                return "a folder whose fixtures/ holds a file that is not a synthetic sidecar"
    return None


def main(argv: Sequence[str]) -> int:
    """Write a synthetic run (module doc)."""
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = ap.parse_args(list(argv))
    why = refuse(args.out)
    if why:
        print(f"synthetic.py: --out is {why}", file=sys.stderr)
        return 1
    manifest = generate(args.out, args.seed)
    print(f"synthetic run (not a measurement): {manifest['trials']} trials in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
