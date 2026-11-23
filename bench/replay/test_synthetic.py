"""Tests for synthetic.py, and the scorer end to end on its made-up run (never a measurement).

The end-to-end tests run the real snow-globe-parse, so they are skipped, never faked, when the
binary is missing or older than its sources (as check.py's live tests are).

    python3 -I -B -m unittest discover -s bench/replay -t bench/replay -p 'test_*.py'
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "utterances"))

import check
import prd_reading as pr
import report
import score
import synthetic
import traces as tr

ROWS, CLOCKS = check.load()
BY_ID = {r["id"]: r for r in ROWS}
HASH = check.script_hash(ROWS)
BINARY = check.find_binary(None)
STALE = check.newer_sources(BINARY) if BINARY.exists() else []
PLACES = {vp: s.place_id for vp, s in pr.SITES.items()}


class Generated(unittest.TestCase):
    """What synthetic.py writes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = Path(tempfile.mkdtemp())
        cls.manifest = synthetic.generate(cls.dir, seed=3)
        cls.lines = [o for _, o in tr.read_jsonl(cls.dir / "traces.jsonl")]
        cls.transcripts = tr.read_transcripts(cls.dir / "transcripts.jsonl")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.dir, True)

    def test_every_line_is_schema_two(self) -> None:
        self.assertEqual(len(self.lines), self.manifest["trials"])
        self.assertEqual(self.manifest["trials"], 5 * len(ROWS))
        for i, obj in enumerate(self.lines):
            tr.parse_trace(obj, f"line {i}", PLACES)

    def test_everything_says_synthetic(self) -> None:
        self.assertEqual(self.manifest["kind"], "synthetic")
        run = json.loads((self.dir / "run.json").read_text())
        self.assertIn("Not a measurement", run["statement"])
        self.assertTrue(all(t.synthetic for t in self.transcripts.values()))
        sidecars = list((self.dir / "fixtures" / "scripted" / HASH).glob("*.json"))
        self.assertEqual(len(sidecars), len(ROWS))
        for path in sidecars:
            self.assertIs(json.loads(path.read_text())["synthetic"], True, path.name)
        self.assertEqual(
            sorted(p.suffix for p in self.dir.rglob("*") if p.is_file()),
            [".json"] * (len(ROWS) + 1) + [".jsonl"] * 2,
        )

    def test_split_discipline(self) -> None:
        # Every test row is played as check.py parses it: its own text at the script's context
        # (empty unless it is a follow-up), so its re-parse is the run scripted.tsv stores.
        seen = 0
        for obj in self.lines:
            rp = obj["replay"]
            row = BY_ID[rp["utterance"]]
            if row["split"] != "test":
                continue
            seen += 1
            self.assertEqual(self.transcripts[(rp["session"], rp["trial"])].final, row["text"])
            if row["context"]:
                self.assertEqual(obj["context"].get("place_id"), row["context"], rp)
                self.assertEqual(obj["context"]["viewpoint_id"], synthetic.script_context(row))
            else:
                self.assertEqual(obj["context"], {"kind": "empty"}, rp)
        self.assertEqual(seen, 5 * 100)

    def test_t100_comes_first_after_each_launch(self) -> None:
        for obj in self.lines:
            if obj["replay"]["trial"] == 1:
                self.assertEqual(obj["replay"]["utterance"], "t100")
                self.assertEqual(obj["context"], {"kind": "empty"})
                self.assertEqual(obj["state"]["cache_state"], "after_launch")

    def test_seeded(self) -> None:
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        synthetic.generate(other, seed=3)
        for name in ("traces.jsonl", "transcripts.jsonl", "run.json"):
            self.assertEqual((other / name).read_bytes(), (self.dir / name).read_bytes(), name)

    def test_refuses_the_repository_and_the_fixtures_folder(self) -> None:
        self.assertEqual(synthetic.refuse(pr.REPO / "bench" / "x"), "inside the repository")
        self.assertIn("fixtures", synthetic.refuse(check.data_dir() / "fixtures" / "voice" / "x"))
        self.assertIsNone(synthetic.refuse(self.dir))  # an earlier synthetic run is replaced

    def test_never_overwrites_a_run_it_did_not_make(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.assertIsNone(synthetic.refuse(d))  # empty
        self.assertIsNone(synthetic.refuse(d / "new"))  # not there yet
        (d / "notes.txt").write_text("not touched by a synthetic run")
        self.assertIsNone(synthetic.refuse(d))
        (d / "traces.jsonl").write_text("{}\n")
        self.assertIn("no synthetic run.json", synthetic.refuse(d))
        (d / "run.json").write_text('{"kind": "measured"}')
        self.assertIn("not a synthetic run", synthetic.refuse(d))
        (d / "run.json").write_text("not json")
        self.assertIn("not a synthetic run", synthetic.refuse(d))
        (d / "run.json").write_text('{"kind": "synthetic"}')
        self.assertIsNone(synthetic.refuse(d))
        side = d / "fixtures" / "scripted" / HASH / "t001.1.json"
        side.parent.mkdir(parents=True)
        side.write_text('{"id": "t001", "take": 1}')  # a real sidecar says nothing of the kind
        self.assertIn("not a synthetic sidecar", synthetic.refuse(d))
        side.write_text('{"id": "t001", "take": 1, "synthetic": true}')
        self.assertIsNone(synthetic.refuse(d))
        (side.parent / "t001.1.wav").write_bytes(b"")  # never beside audio
        self.assertIn("not a synthetic sidecar", synthetic.refuse(d))
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(synthetic.main(["--out", str(d)]), 1)
        self.assertIn("not a synthetic sidecar", err.getvalue())
        with self.assertRaises(ValueError):
            synthetic.generate(d)  # the library refuses too
        self.assertEqual(side.read_text(), '{"id": "t001", "take": 1, "synthetic": true}')
        self.assertEqual(
            sorted(p.name for p in side.parent.iterdir()), ["t001.1.json", "t001.1.wav"]
        )


@unittest.skipUnless(
    BINARY.exists() and not STALE, f"snow-globe-parse not built at {BINARY}, or older than {STALE}"
)
class EndToEnd(unittest.TestCase):
    """The scorer on the made-up run, with the real parser."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = Path(tempfile.mkdtemp())
        synthetic.generate(cls.dir, seed=5)
        cls.replay_run = score.load_run(
            [cls.dir / "traces.jsonl"],
            cls.dir / "transcripts.jsonl",
            cls.dir / "fixtures",
            ROWS,
            HASH,
        )
        cls.scores = score.score_run(cls.replay_run, ROWS, score.Reparser(BINARY, CLOCKS))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.dir, True)

    def test_test_rows_score_as_check_py_scored_their_text(self) -> None:
        # Known answer: a test row's made-up transcript is its text, played at the script's
        # context, so its re-parse is check.py's own run and its verdict the one scripted.tsv
        # stores.
        test = [s for s in self.scores if s.split == "test"]
        self.assertEqual(len(test), 5 * 100)
        for s in test:
            row = BY_ID[s.utterance]
            self.assertEqual(s.agreement, row["agreement"], (s.session, s.utterance))
            self.assertEqual(s.parser_outcome, row["parser_outcome"], (s.session, s.utterance))
            self.assertEqual(s.wer_normalised.edits, 0)
            self.assertNotEqual(s.context_status, "differed")
            self.assertEqual((s.failures, s.unscorable), ([], []))
            # The app's outcome follows the same recorded parse, so it never disagrees.
            self.assertFalse([i for i in s.issues if i.startswith("the app's outcome")], s.issues)

    def test_no_stage_failed(self) -> None:
        self.assertEqual([s for s in self.scores if s.failures], [])

    def test_the_injected_dev_anomalies_are_found(self) -> None:
        reasons = sorted(
            (s.session, s.utterance, s.context_reason)
            for s in self.scores
            if s.context_status == "differed"
        )
        self.assertEqual(reasons, [(2, "d06", "row-before-misheard"), (3, "d27", "app-lost-it")])
        empty = [s for s in self.scores if s.utterance == "d16" and s.session == 1]
        self.assertEqual(empty[0].wer_raw.deletions, empty[0].wer_raw.reference_words)
        issues = sum(
            "labelled idle, but idle for less than 30 min" in s.issues for s in self.scores
        )
        self.assertEqual(issues, 5)  # one per session

    def test_the_report_is_complete_and_says_synthetic(self) -> None:
        manifest = json.loads((self.dir / "run.json").read_text())
        doc = score.build_report(
            self.replay_run,
            self.scores,
            ROWS,
            script_hash=HASH,
            resamples=40,
            seed=1,
            origin=score.provenance(manifest, self.replay_run.synthetic_inputs),
            inputs={"parser_binary": str(BINARY), "parser_sha256": score.sha256(BINARY)},
        )
        self.assertEqual(doc["provenance"]["kind"], "synthetic")
        heads = [
            g
            for g in doc["groups"]
            if (g["split"], g["state"], g["display"], g["subset"])
            == ("test", "idle", "all", "all rows")
        ]
        self.assertEqual(len(heads), 2)  # one per engine setup
        for g in heads:
            for metric in (
                "tier0",
                "tier1",
                "release_to_final",
                "first_partial_after_onset",
                "partial_lag",
                "parse",
            ):
                self.assertIn(metric, g["values_ms"])
            for ratio in (
                "exact_non_pending",
                "exact_pending",
                "wer_normalised",
                "fallback",
                "wrong_paint_per_partial_paint",
                "still_in_speech_at_key_up",
            ):
                self.assertIn(ratio, g["ratios"])
        states = {(g["state"], g["display"]) for g in doc["groups"] if g["split"] == "test"}
        for state in ("idle", "warm", "cold", "after_wake", "after_launch"):
            self.assertIn((state, "all"), states)
        self.assertIn(("idle", "asleep"), states)
        md = report.markdown(doc)
        self.assertIn("> **SYNTHETIC FIXTURE", md.splitlines()[2])
        self.assertEqual(md.count("### Headline: idle trials, test split"), 2)
        self.assertEqual(md.count("### Targets on the test set: every state, idle beside"), 2)
        self.assertEqual(md.count("### Budget hit rates: idle, test split"), 2)
        self.assertIn("Known bias", md)
        for g in heads:
            self.assertLessEqual(g["recordings"], g["utterances"])  # one take of each row
            self.assertEqual(g["ratios"]["still_in_speech_at_key_up"]["n"], g["recordings"])
        self.assertEqual(score.exit_status(doc), 0)
        self.assertEqual(doc["lines"]["transcripts_without_trace"], 0)


if __name__ == "__main__":
    unittest.main()
