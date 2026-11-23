"""Tests for score.py and report.py on made-up trials (never voice; never a measurement).

The re-parse is a stage the scorer runs for real (snow-globe-parse); these unit tests feed it
outputs recorded from that binary on 22 Nov 2026 (built from the working tree) for dev rows only,
as unit tests may. test_synthetic.py runs the real binary end to end when it is built. The
Reparser tests run tiny made-up stand-ins for the binary that fail on purpose, to check that a
failed re-parse is never scored.

    python3 -I -B -m unittest discover -s bench/replay -t bench/replay -p 'test_*.py'
"""

from __future__ import annotations

import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "utterances"))

import check
import prd_reading as pr
import report
import score
import trace_example
import traces as tr

ROWS, CLOCKS = check.load()
BY_ID = {r["id"]: r for r in ROWS}
INDEX = {r["id"]: i for i, r in enumerate(ROWS)}
HASH = check.script_hash(ROWS)
PLACES = {vp: s.place_id for vp, s in pr.SITES.items()}

BIG_BEND = {"kind": "place", "place_id": "big-bend", "viewpoint_id": "big-bend.panther-junction"}
NOON = "What about noon tomorrow?"

#: snow-globe-parse outputs recorded on 22 Nov 2026 at clock C1 (machine zone America/Chicago),
#: keyed by (text, clock, context viewpoint).
RECORDED: dict[tuple[str, str, str], dict[str, Any]] = {
    (NOON, "C1", "big-bend.panther-junction"): {
        "place_id": "big-bend",
        "viewpoint_id": "big-bend.panther-junction",
        "valid_time": "2026-11-20T12:00:00-06:00",
        "valid_time_utc": "2026-11-20T18:00:00Z",
        "tz": "America/Chicago",
        "time_source": {"kind": "clock", "text": "noon tomorrow", "rule": "noon"},
        "ambiguities": [],
        "overrides": {},
        "path": "fast_path",
        "confidence": 0.95,
        "place_source": "context",
        "place_match": {"stage": "context", "score": 1.0},
        "horizon": "hrrr",
        "flags": [],
        "_exit": 0,
    },
    (NOON, "C1", ""): {"error": "no_place", "_exit": 2},
    (NOON, "C1", "acadia.mcfarland"): {
        "place_id": "acadia",
        "viewpoint_id": "acadia.mcfarland",
        "valid_time": "2026-11-20T12:00:00-05:00",
        "valid_time_utc": "2026-11-20T17:00:00Z",
        "tz": "America/New_York",
        "time_source": {"kind": "clock", "text": "noon tomorrow", "rule": "noon"},
        "ambiguities": [],
        "overrides": {},
        "path": "fast_path",
        "confidence": 0.95,
        "place_source": "context",
        "place_match": {"stage": "context", "score": 1.0},
        "horizon": "hrrr",
        "flags": [],
        "_exit": 0,
    },
    ("Mount Hood tomorrow morning", "C1", ""): {
        "error": "no_match",
        "text": "mount hood",
        "closest": [],
        "_exit": 2,
    },
    ("Show me Yosemite", "C1", ""): {
        "place_id": "yosemite",
        "viewpoint_id": "yosemite.turtleback",
        "valid_time": "2026-11-19T10:00:00-08:00",
        "valid_time_utc": "2026-11-19T18:00:00Z",
        "tz": "America/Los_Angeles",
        "time_source": {"kind": "now", "text": "", "rule": "no-time"},
        "ambiguities": [],
        "overrides": {},
        "path": "fast_path",
        "confidence": 1.0,
        "place_source": "utterance",
        "place_match": {"stage": "alias", "score": 1.0},
        "horizon": "hrrr",
        "flags": ["live"],
        "_exit": 0,
    },
}


def fake_reparse(text: str, clock: str, context: str | None) -> dict[str, Any]:
    """A recorded output, or the parser's shape for leftover words (its reason echoes them)."""
    return RECORDED.get(
        (text, clock, context or ""),
        {"error": "unrecognised", "reason": f"words that are neither ...: {text}", "_exit": 2},
    )


def failing_reparse(text: str, clock: str, context: str | None) -> dict[str, Any]:
    """What Reparser returns when snow-globe-parse could not be run."""
    return score._failed("exited with a status other than 0 or 2")


def example(**changes: Any) -> dict[str, Any]:
    """The pinned schema-2 example with top-level or state changes."""
    obj = trace_example.load()
    for key, value in changes.items():
        if key in obj["state"]:
            obj["state"][key] = value
        else:
            obj[key] = value
    return obj


def line(utterance: str, *, session: int = 1, trial: int = 1, **changes: Any) -> tr.TraceLine:
    """The pinned schema-2 example, replayed as another row, with top-level or state changes."""
    obj = example(**changes)
    obj["replay"].update(utterance=utterance, session=session, trial=trial)
    return tr.parse_trace(obj, "test", PLACES)


def trial(
    utterance: str,
    transcript: str | None,
    side: tr.Sidecar | None = None,
    *,
    obj: dict[str, Any] | None = None,
    **kw: Any,
) -> score.Trial:
    """A made-up trial of a script row (from `obj`, a trace line, when given)."""
    if obj is not None:
        obj["replay"].update(utterance=utterance)
        t = tr.parse_trace(obj, "test", PLACES)
    else:
        t = line(utterance, **kw)
    text = None
    if transcript is not None:
        text = tr.Transcript(final=transcript, utterance=utterance, synthetic=True)
    return score.Trial(t, "test", BY_ID[utterance], INDEX[utterance], text, side)


def words(text: str) -> dict[str, int]:
    """The lengths a trace holds for a transcript."""
    return {"transcript_chars": len(text), "transcript_words": len(text.split()), "partials": 3}


SIDE = tr.Sidecar(key_up_ms=1060.0, speech_onset_ms=250.0, speech_end_ms=1100.0, synthetic=True)


class Rules(unittest.TestCase):
    """The scoring rules, row by row."""

    def test_categories(self) -> None:
        self.assertEqual(score.category(BY_ID["d27"]), "settled")
        self.assertEqual(score.category(BY_ID["d29"]), "owner-pending")
        self.assertEqual(score.category(BY_ID["d06"]), "text-level-no")

    def test_place_right(self) -> None:
        def cols(outcome: str, vp: str = "") -> dict[str, str]:
            return {"parser_outcome": outcome, "parser_viewpoint_id": vp}

        d27, d28, d30 = BY_ID["d27"], BY_ID["d28"], BY_ID["d30"]
        self.assertTrue(score.place_right(d27, cols("query", "big-bend.panther-junction")))
        self.assertFalse(score.place_right(d27, cols("query", "acadia.mcfarland")))
        self.assertFalse(score.place_right(d27, cols("miss/no_place", "no-match")))
        self.assertTrue(score.place_right(d28, cols("miss/no_match", "no-match")))  # a miss
        self.assertTrue(score.place_right(d28, cols("fallback/unrecognised")))  # also refuses
        self.assertFalse(score.place_right(d28, cols("query", "rainier.paradise-mountain")))
        self.assertTrue(score.place_right(d30, cols("fallback/unrecognised")))  # Rainier weekend
        self.assertTrue(score.place_right(d30, cols("query", "rainier.paradise-mountain")))
        self.assertFalse(score.place_right(d30, cols("miss/no_match", "no-match")))
        d29 = BY_ID["d29"]  # a close call: either place
        self.assertTrue(score.place_right(d29, cols("query", "grand-teton.science-center")))
        self.assertTrue(score.place_right(d29, cols("query", "grand-canyon.yavapai")))

    def test_a_follow_up_is_reparsed_at_the_live_context(self) -> None:
        s = score.score_trial(
            trial("d27", NOON, SIDE, context=BIG_BEND, lengths=words(NOON)), fake_reparse
        )
        self.assertEqual(
            (s.agreement, s.exact, s.place, s.context_status), ("yes", True, True, "matched")
        )
        self.assertEqual(s.issues, [])
        self.assertEqual(s.failures, [])

    def test_a_follow_up_with_another_live_context_is_wrong_on_place(self) -> None:
        acadia = {"kind": "place", "place_id": "acadia", "viewpoint_id": "acadia.mcfarland"}
        s = score.score_trial(
            trial("d27", NOON, SIDE, context=acadia, lengths=words(NOON)), fake_reparse
        )
        self.assertEqual((s.exact, s.place, s.context_status), (False, False, "differed"))
        self.assertTrue(s.difference.startswith("live context acadia.mcfarland, but the script"))
        empty = score.score_trial(
            trial("d27", NOON, SIDE, context={"kind": "empty"}, lengths=words(NOON)), fake_reparse
        )
        self.assertEqual((empty.parser_outcome, empty.place), ("miss/no_place", False))
        # The app painted, the re-parse found no place: the two disagree, and the report says so.
        self.assertIn("the app's outcome says place, the re-parse no place", empty.issues)

    def test_a_context_not_recorded_cannot_be_scored_on_place(self) -> None:
        s = score.score_trial(
            trial(
                "d16",
                "Show me Yosemite",
                SIDE,
                context={"kind": "not_recorded"},
                lengths=words("Show me Yosemite"),
            ),
            fake_reparse,
        )
        self.assertIsNone(s.exact)
        self.assertIn("context not_recorded", s.unscorable)
        self.assertIsNotNone(s.wer_normalised)  # WER needs no context

    def test_an_empty_transcript_is_wrong_and_never_reparsed(self) -> None:
        # snow-globe-parse resolves "" at a context to that place now, which is d16's expected
        # answer: re-parsing it would score a trial where nothing was heard as exact.
        def no_reparse(text: str, clock: str, context: str | None) -> dict[str, Any]:
            raise AssertionError("an empty transcript was re-parsed")

        yosemite = {"kind": "place", "place_id": "yosemite", "viewpoint_id": "yosemite.turtleback"}
        for text in ("", "   "):
            s = score.score_trial(
                trial(
                    "d16",
                    text,
                    SIDE,
                    context=yosemite,
                    outcome="empty",
                    paints=[],
                    lengths=words(text),
                ),
                no_reparse,
            )
            self.assertEqual(
                (s.exact, s.exact_or_soft, s.place, s.fallback, s.agreement),
                (False, False, False, None, "no"),
            )
            self.assertIn("empty final transcript (scored wrong on place and time)", s.issues)
            self.assertEqual(s.wer_raw.deletions, s.wer_raw.reference_words)
            self.assertEqual(score.observation(s).ratios["exact_non_pending"], (0.0, 1.0))
            self.assertNotIn("fallback", score.observation(s).ratios)  # not re-parsed
        # An empty transcript beside an outcome that painted is a contradiction, flagged.
        s = score.score_trial(trial("d16", "", SIDE, lengths=words("")), no_reparse)
        self.assertIn("an empty final transcript, but the app's outcome is painted", s.issues)
        s = score.score_trial(
            trial(
                "d16",
                "Show me Yosemite",
                SIDE,
                context={"kind": "empty"},
                outcome="empty",
                lengths=words("Show me Yosemite"),
            ),
            fake_reparse,
        )
        self.assertIn("the app's outcome is empty, but the final transcript has words", s.issues)

    def test_a_failed_reparse_is_never_a_verdict(self) -> None:
        # d28 expects a miss: scored as "no place resolved", a failure would count as right.
        s = score.score_trial(
            trial(
                "d28",
                "Mount Hood tomorrow morning",
                SIDE,
                context={"kind": "empty"},
                lengths=words("Mount Hood tomorrow morning"),
            ),
            failing_reparse,
        )
        self.assertEqual((s.exact, s.place, s.fallback, s.agreement), (None, None, None, None))
        self.assertEqual(
            s.failures,
            ["re-parse failed: snow-globe-parse exited with a status other than 0 or 2"],
        )
        ratios = score.observation(s).ratios
        for name in ("exact_non_pending", "place_non_pending", "fallback"):
            self.assertNotIn(name, ratios)
        self.assertIn("wer_normalised", ratios)  # WER needs no parser

    def test_missing_transcript_and_sidecar(self) -> None:
        s = score.score_trial(trial("d16", None, None), fake_reparse)
        self.assertEqual(s.unscorable, ["no sidecar", "no transcript"])
        self.assertIsNone(s.exact)
        self.assertIn("release_to_final", s.latencies)  # the timings still count

    def test_lengths_that_do_not_match_are_flagged(self) -> None:
        s = score.score_trial(trial("d16", "Show me Yosemite", SIDE), fake_reparse)
        self.assertIn("the transcript's lengths are not the trace's", s.issues)

    def test_a_no_place_row_needs_an_empty_context(self) -> None:
        s = score.score_trial(trial("t100", None, SIDE, context=BIG_BEND), fake_reparse)
        self.assertEqual(s.context_status, "differed")
        self.assertIn("first after a launch", s.context_reason)


class TheParserStage(unittest.TestCase):
    """Reparser turns every way snow-globe-parse can fail into a named failure, never a guess."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def stand_in(self, name: str, body: str) -> Path:
        """A made-up executable that behaves badly in one way."""
        path = self.dir / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return path

    def failure(self, binary: Path, text: str = "Show me Yosemite") -> str | None:
        out = score.Reparser(binary, CLOCKS)(text, "C1", None)
        self.assertEqual(score.parser_columns(out)["parser_outcome"], "parser-failed")
        return out.get("failure")

    def test_every_failure_is_named(self) -> None:
        cases = {
            self.stand_in("exit1", "exit 1"): "exited with a status other than 0 or 2",
            self.stand_in("garbage", "echo not json; exit 0"): "printed something that is not JSON",
            self.stand_in("list", "echo '[1, 2]'; exit 0"): "printed JSON that is not its output",
            self.stand_in("partial", 'echo \'{"place_id": "x"}\'; exit 0'): (
                "printed JSON that is not its output"
            ),
            self.dir / "missing": "could not be started",
        }
        for binary, why in cases.items():
            self.assertEqual(self.failure(binary), why, binary.name)
        ok = self.stand_in("ok", 'echo \'{"error": "no_place"}\'; exit 2')
        self.assertEqual(
            self.failure(ok, "made-up \x00 text"), "could not take the transcript as an argument"
        )

    def test_a_good_run_is_cached(self) -> None:
        ok = self.stand_in("ok", 'echo \'{"error": "no_place"}\'; exit 2')
        reparse = score.Reparser(ok, CLOCKS)
        first = reparse("What about noon?", "C1", None)
        self.assertEqual(score.parser_columns(first)["parser_outcome"], "miss/no_place")
        reparse("What about noon?", "C1", None)
        self.assertEqual(reparse.calls, 1)


class Measurements(unittest.TestCase):
    """Latencies, paints and lags, on the pinned example's numbers."""

    def setUp(self) -> None:
        self.s = score.score_trial(trial("d16", None, SIDE), fake_reparse)

    def test_stages(self) -> None:
        lat = self.s.latencies
        self.assertAlmostEqual(lat["release_to_final"], 190.0)
        self.assertAlmostEqual(lat["parse"], 1.0)
        self.assertAlmostEqual(lat["hold"], 1100.0)
        self.assertAlmostEqual(lat["tier0"], 220.0)  # the paint matching the final
        self.assertAlmostEqual(lat["tier0_first_paint"], 50.0)  # the wrong one, from a partial
        self.assertAlmostEqual(lat["tier1"], 380.0)
        self.assertAlmostEqual(lat["first_partial_after_onset"], 1420 - (1040 + 250))
        self.assertAlmostEqual(lat["assets_ready_after_key_up"], -400.0)
        self.assertNotIn("tier2", lat)
        self.assertFalse(self.s.tier2_activity)

    def test_paints(self) -> None:
        s = self.s
        self.assertEqual(
            (s.wrong_partial_paint, s.partial_judged, s.wrong_final_paint), (True, True, False)
        )
        self.assertEqual((s.flips, s.visible_wrong_ms, s.unreplaced_wrong), (1, [170.0], 0))
        self.assertTrue(s.paint_due)

    def test_a_paint_time_is_a_recorded_paint_never_a_mark_alone(self) -> None:
        # A webview callback's stamp 1 ms after key-up: the composited paint is still 50 ms.
        obj = example()
        obj["marks"]["tier0_paint"]["mono_ns"] = 2_101_000_000
        s = score.score_trial(trial("d16", None, SIDE, obj=obj), fake_reparse)
        self.assertAlmostEqual(s.latencies["tier0_first_paint"], 50.0)
        self.assertEqual(s.issues, [])
        # A paint mark with no paint behind it is left out, and flagged.
        obj = example()
        obj["paints"] = [p for p in obj["paints"] if p["tier"] != "tier0"]
        s = score.score_trial(trial("d16", None, SIDE, obj=obj), fake_reparse)
        self.assertNotIn("tier0_first_paint", s.latencies)
        self.assertNotIn("tier0", s.latencies)
        self.assertIn("a tier0_paint mark but no tier0 paint recorded (left out)", s.issues)

    def test_key_up_must_be_where_the_sidecar_puts_it(self) -> None:
        # The example's key-up is 1,060 ms after the first sample, as SIDE says.
        late = tr.Sidecar(
            key_up_ms=1060.0 + 5_000, speech_onset_ms=250.0, speech_end_ms=1100.0, synthetic=True
        )
        s = score.score_trial(trial("d16", None, late), fake_reparse)
        self.assertNotIn("first_partial_after_onset", s.latencies)
        self.assertIn(
            "key-up is not where the take's sidecar puts it (first partial after onset left out)",
            s.issues,
        )
        near = tr.Sidecar(
            key_up_ms=1060.0 + score.KEY_UP_TOLERANCE_MS,
            speech_onset_ms=250.0,
            speech_end_ms=1100.0,
            synthetic=True,
        )
        s = score.score_trial(trial("d16", None, near), fake_reparse)
        self.assertIn("first_partial_after_onset", s.latencies)
        self.assertEqual(s.issues, [])

    def test_tier2_activity(self) -> None:
        obj = example()
        obj["spans"].append({"name": "readback", "start_ns": 1, "end_ns": 2, "dur_ns": 1})
        s = score.score_trial(trial("d16", None, SIDE, obj=obj), fake_reparse)
        self.assertTrue(s.tier2_activity)

    def test_lags_and_spans(self) -> None:
        self.assertEqual(self.s.partial_lags, [80.0, 120.0])
        self.assertEqual(self.s.stable_lags, [150.0])
        self.assertEqual(self.s.spans["audio_buffer_delivery"], [1.0, 1.0])
        self.assertFalse(self.s.still_in_speech is None)
        self.assertTrue(self.s.still_in_speech)  # speech ends at 1,100 ms, the key at 1,060 ms

    def test_state_labels_are_checked(self) -> None:
        s = score.score_trial(trial("d16", None, SIDE, idle_duration_ms=600_000), fake_reparse)
        self.assertIn("labelled idle, but idle for less than 30 min", s.issues)


class Contexts(unittest.TestCase):
    """Why a follow-up's live context differed."""

    def scored(self, utterance: str, session: int, n: int, **kw: Any) -> score.TrialScore:
        s = score.score_trial(trial(utterance, None, SIDE, session=session, trial=n), fake_reparse)
        for key, value in kw.items():
            setattr(s, key, value)
        return s

    def test_reasons(self) -> None:
        resolved = {
            "parser_outcome": "query",
            "parser_viewpoint_id": "big-bend.panther-junction",
            "exact": True,
        }
        missed = {
            "parser_outcome": "miss/no_match",
            "parser_viewpoint_id": "no-match",
            "exact": False,
        }
        heard_nothing = {"exact": False}  # an empty transcript: not re-parsed, scored wrong
        follow = {"context_status": "differed", "context_reason": None}
        scores = [
            self.scored("d26", 1, 1, **resolved),
            self.scored("d27", 1, 2, **follow),  # the row before resolved here: the app lost it
            self.scored("d26", 2, 1, **missed),
            self.scored("d27", 2, 2, **follow),  # the row before was misheard
            self.scored("d25", 3, 1, **resolved),
            self.scored("d27", 3, 2, **follow),  # the row before it was not played before it
            self.scored("d27", 4, 1, **follow),  # nothing before it in its session
            self.scored("d26", 5, 1),  # no transcript: exact None, its place unknown
            self.scored("d27", 5, 2, **follow),
            self.scored("d26", 6, 1, **heard_nothing),
            self.scored("d27", 6, 2, **follow),
        ]
        score.explain_contexts(scores, ROWS)
        self.assertEqual(
            [s.context_reason for s in scores if s.utterance == "d27"],
            [
                "app-lost-it",
                "row-before-misheard",
                "row-before-not-played",
                "row-before-not-played",
                "row-before-unscorable",
                "row-before-misheard",
            ],
        )


class Groups(unittest.TestCase):
    """Groups, denominators and the report built from them."""

    @classmethod
    def setUpClass(cls) -> None:
        texts = {
            "d26": "Panther Junction golden hour",
            "d27": NOON,
            "d28": "Mount Hood tomorrow morning",
            "d16": "Show me Yosemite xylophone",  # made-up leftover word: must never be reported
            "d29": "Grand at sunset",
        }
        trials = []
        n = 0
        for session in (1, 2):
            for utterance, text in texts.items():
                for state, display in (("idle", "asleep"), ("idle", "on"), ("warm", "on")):
                    n += 1
                    ctx = BIG_BEND if utterance == "d27" else {"kind": "empty"}
                    trials.append(
                        trial(
                            utterance,
                            text,
                            SIDE,
                            session=session,
                            trial=n,
                            context=ctx,
                            cache_state=state,
                            display=display,
                            lengths=words(text),
                            idle_duration_ms=2_000_000 if state == "idle" else 20_000,
                        )
                    )
        cls.replay_run = score.Run(trials=trials, lines=len(trials), synthetic_inputs=True)
        cls.scores = score.score_run(cls.replay_run, ROWS, fake_reparse)
        cls.doc = cls.build(cls.scores, None)

    @classmethod
    def build(cls, scores: list[score.TrialScore], manifest: dict | None) -> dict[str, Any]:
        return score.build_report(
            cls.replay_run,
            scores,
            ROWS,
            script_hash=HASH,
            resamples=60,
            seed=1,
            origin=score.provenance(manifest, True),
            inputs={"parser_binary": "recorded outputs", "parser_sha256": "", "parser_runs": 0},
        )

    def group(self, doc: dict | None = None, **key: Any) -> dict[str, Any]:
        found = [g for g in (doc or self.doc)["groups"] if all(g[k] == v for k, v in key.items())]
        self.assertEqual(len(found), 1, key)
        return found[0]

    def test_specs(self) -> None:
        names = {(g["split"], g["state"], g["display"], g["subset"]) for g in self.doc["groups"]}
        self.assertIn(("dev", "idle", "asleep", "all rows"), names)
        self.assertIn(("dev", "idle", "all", "follow-ups"), names)
        self.assertNotIn(("dev", "cold", "all", "all rows"), names)  # empty groups are left out
        self.assertFalse(any(split == "test" for split, *_ in names))
        setup = self.doc["groups"][0]["setup"]
        self.assertEqual((setup["warm_up"], setup["pre_render"]), (True, False))

    def test_denominators(self) -> None:
        g = self.group(split="dev", state="idle", display="all", subset="all rows")
        self.assertEqual(g["trials"], 20)
        self.assertEqual(g["ratios"]["exact_pending"]["n"], 4)  # d29, apart
        self.assertEqual(g["ratios"]["exact_non_pending"]["n"], 16)
        self.assertEqual(g["ratios"]["exact_settled"]["n"], 16)
        self.assertNotIn("exact_text_no", g["ratios"])  # no such row played
        self.assertEqual(g["bootstrap"]["method"], "utterance x session (pigeonhole)")
        self.assertEqual(g["display_asleep_trials"], 10)
        self.assertNotIn("hit_tier2_3000", g["ratios"])  # Tier 2 never tried
        self.assertEqual((g["tier2_attempted"], g["tier2_basis"]), (False, "trace activity"))
        w = g["wilson"]["wrong_paint_per_partial_paint"]
        self.assertEqual((w["k"], w["n"]), (20, 20))  # the example's partial paint is wrong
        follow = self.group(split="dev", state="idle", display="all", subset="follow-ups")
        self.assertEqual(follow["trials"], 4)

    def test_still_in_speech_counts_each_take_once(self) -> None:
        # 20 idle trials replay 5 takes (one per row, twice in each of 2 sessions).
        g = self.group(split="dev", state="idle", display="all", subset="all rows")
        per_take, per_trial = (
            g["ratios"]["still_in_speech_at_key_up"],
            g["ratios"]["still_in_speech_per_trial"],
        )
        self.assertEqual((per_take["numerator"], per_take["n"]), (5, 5))
        self.assertEqual((per_trial["numerator"], per_trial["n"]), (20, 20))
        self.assertEqual(g["recordings"], 5)
        self.assertEqual(g["bootstrap"]["recordings_method"], "utterance")

    def test_tier2_from_the_run_manifest(self) -> None:
        doc = self.build(self.scores, {"kind": "synthetic", "tier2_enabled": True})
        g = self.group(doc, split="dev", state="idle", display="all", subset="all rows")
        self.assertEqual((g["tier2_attempted"], g["tier2_basis"]), (True, "run manifest"))
        hit = g["ratios"]["hit_tier2_3000"]
        self.assertEqual((hit["numerator"], hit["value"]), (0.0, 0.0))  # tried, never in time
        for group in doc["groups"]:
            group["split"] = "test"  # to render the headline and the hit rates
        md = report.markdown(doc)
        self.assertIn(
            "| Tier 2 matching the final within 3 s | 0.0 % | 0.0 % to 0.0 % | 0 of 20 |", md
        )
        off = self.build(self.scores, {"kind": "synthetic", "tier2_enabled": False})
        for group in off["groups"]:
            group["split"] = "test"
        self.assertIn("not attempted (the run manifest says Tier 2 was off)", report.markdown(off))

    def test_failures_are_counted_shown_and_fail_the_command(self) -> None:
        self.assertEqual(score.exit_status(self.doc), 0)
        failed = score.score_run(self.replay_run, ROWS, failing_reparse)
        doc = self.build(failed, None)
        self.assertEqual(doc["lines"]["trials_with_failures"], 30)  # every trial is re-parsed
        self.assertEqual(score.exit_status(doc), 1)
        g = self.group(doc, split="dev", state="idle", display="all", subset="all rows")
        self.assertNotIn("exact_non_pending", g["ratios"])
        self.assertNotIn("place_non_pending", g["ratios"])
        self.assertIn("wer_normalised", g["ratios"])
        md = report.markdown(doc)
        self.assertIn("> **A stage of the scorer failed on 30 trials**", md)
        self.assertIn("score.py exited 1", md)

    def test_no_transcript_text_reaches_the_report(self) -> None:
        text = json.dumps(self.doc) + report.markdown(self.doc)
        self.assertNotIn("xylophone", text)
        for t in self.replay_run.trials:
            if t.transcript and t.transcript.final != t.row["text"]:
                self.assertNotIn(t.transcript.final, text)

    def test_the_markdown_leads_with_the_provenance(self) -> None:
        md = report.markdown(self.doc)
        self.assertTrue(md.startswith("# Snow Globe replay report\n\n> **SYNTHETIC FIXTURE"))
        self.assertIn("nothing here is assessed against them", md)
        self.assertIn("> Known bias: one speaker, one microphone, one room (PRD §10.6).", md)
        self.assertIn("No idle trials on the test split", md)
        self.assertIn("### By state and display: dev split", md)
        doc = copy.deepcopy(self.doc)
        doc["provenance"] = {"kind": "measured", "statement": "Measured.", "run_manifest": None}
        self.assertNotIn("nothing here is assessed", report.markdown(doc))

    def test_the_markdown_puts_each_target_on_its_own_group(self) -> None:
        # Relabel the dev groups as test groups to render the test-split sections.
        doc = copy.deepcopy(self.doc)
        for g in doc["groups"]:
            g["split"] = "test"
        md = report.markdown(doc)
        head = md.split("### Headline: idle trials, test split")[1].split("###")[0]
        self.assertIn("p95 ≤ 300 ms (PRD §14)", head)
        self.assertNotIn("≥ 95 %", head)  # the accuracy targets are on the whole test set
        targets = md.split("### Targets on the test set: every state, idle beside")[1].split("###")[
            0
        ]
        self.assertIn("≥ 95 % on the replay test set (PRD §14)", targets)
        # The wrong-paint target shows Wilson, never the bootstrap (which is 0 to 0 at 0 wrong):
        # 30 of 30 over every state (lower end 1 / (1 + z²/30)), 20 of 20 idle (1 / (1 + z²/20)).
        rows = [r for r in targets.splitlines() if "Wilson" in r]
        self.assertEqual(len(rows), 2)
        self.assertIn("100.0 % (30 of 30) | Wilson two-sided 95 % 88.6 % to 100.0 %", rows[0])
        self.assertIn("100.0 % (20 of 20) | Wilson two-sided 95 % 83.9 % to 100.0 %", rows[1])
        self.assertIn("one-sided 95 % upper bound 100.0 %", rows[0])
        self.assertFalse(any("bootstrap" in r for r in rows))
        self.assertIn("### Budget hit rates: idle, test split", md)
        for metric in score.HIT_RATES:
            self.assertIn(f"| {score.RATIOS[metric]} |", md)
        self.assertIn("### Wrong paints: test split, every state", md)
        self.assertIn("### Accuracy: test split, every state", md)

    def test_the_report_is_json(self) -> None:
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        score.write_report(self.doc, out)
        json.loads((out / "replay-report.json").read_text(), parse_constant=self.fail)
        doc = copy.deepcopy(self.doc)
        doc["groups"][0]["values_ms"]["parse"]["p50"]["value"] = float("nan")
        with self.assertRaises(ValueError):
            score.write_report(doc, out / "nan")
        self.assertFalse((out / "nan").exists())


class Loading(unittest.TestCase):
    """Joining the inputs, and what the join leaves out."""

    def test_a_transcript_with_no_trace_line_is_counted(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        obj = example()
        obj["replay"].update(utterance="d16", session=1, trial=1)
        (d / "traces.jsonl").write_text(json.dumps(obj) + "\n")
        (d / "transcripts.jsonl").write_text(
            '{"session": 1, "trial": 1, "final": "Show me Yosemite"}\n'
            '{"session": 1, "trial": 2, "final": "Show me Yosemite"}\n'
            '{"session": 3, "trial": 9, "final": "Show me Yosemite"}\n'
        )
        run = score.load_run([d / "traces.jsonl"], d / "transcripts.jsonl", None, ROWS, HASH)
        self.assertEqual((len(run.trials), run.transcripts_without_trace), (1, 2))


class Provenance(unittest.TestCase):
    """A report is never called measured unless the run says so and nothing is synthetic."""

    def test_kinds(self) -> None:
        measured = {"kind": "measured", "build_commit": "8fa6b6a"}
        self.assertEqual(score.provenance(None, False).kind, "unknown")
        self.assertEqual(score.provenance(None, True).kind, "synthetic")
        self.assertEqual(score.provenance(measured, True).kind, "synthetic")
        self.assertEqual(score.provenance({"kind": "synthetic"}, False).kind, "synthetic")
        got = score.provenance(measured, False)
        self.assertEqual(got.kind, "measured")
        self.assertIn("8fa6b6a", got.statement)


class Command(unittest.TestCase):
    """The CLI's refusals, before any parser run."""

    def run_main(self, *argv: str) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stderr(err):
            code = score.main(list(argv))
        return code, err.getvalue()

    def test_too_few_resamples(self) -> None:
        code, err = self.run_main(
            "--traces", "x.jsonl", "--out", "/nonexistent", "--resamples", "9999"
        )
        self.assertEqual(code, 1)
        self.assertIn("10000", err)

    def test_out_inside_the_repository(self) -> None:
        code, err = self.run_main("--traces", "x.jsonl", "--out", str(pr.REPO / "bench" / "out"))
        self.assertEqual(code, 1)
        self.assertIn("inside the repository", err)

    def test_a_measured_run_is_reparsed_only_with_its_own_parser(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        binary = d / "snow-globe-parse"  # a made-up stand-in, newer than every source
        binary.write_text("#!/bin/sh\nexit 1\n")
        binary.chmod(0o755)
        run_json = d / "run.json"
        base = ["--traces", str(d / "none.jsonl"), "--out", str(d / "out"), "--bin", str(binary)]
        run_json.write_text('{"kind": "measured"}')
        code, err = self.run_main(*base, "--run", str(run_json))
        self.assertEqual(code, 1)
        self.assertIn("names its frozen build", err)
        other = {"kind": "measured", "build_commit": "8fa6b6a", "parser_sha256": "0" * 64}
        run_json.write_text(json.dumps(other))
        code, err = self.run_main(*base, "--run", str(run_json))
        self.assertEqual(code, 2)
        self.assertIn("is not the run's parser", err)
        self.assertIn("--allow-other-parser", err)
        self.assertFalse((d / "out").exists())


if __name__ == "__main__":
    unittest.main()
