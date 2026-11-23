"""Tests for traces.py: the schema-2 reader against the writer in app/core/src/trace.rs.

The example is one record built by hand on a fake clock in app/core/tests/trace_fields.rs, which
checks the writer's JSON against the `SCHEMA_TWO_EXAMPLE` string in that file; these tests read
the same string (trace_example.py), so the writer and this reader cannot drift apart unnoticed.
It is a schema example, not a captured trace and not a measurement.

    python3 -I -B -m unittest discover -s bench/replay -t bench/replay -p 'test_*.py'
"""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "utterances"))

import prd_reading as pr
import trace_example
import traces as tr

TRACE_RS = pr.REPO / "app" / "core" / "src" / "trace.rs"
LIFECYCLE_RS = pr.REPO / "app" / "core" / "src" / "lifecycle.rs"
QUERY_RS = pr.REPO / "app" / "core" / "src" / "query.rs"
PLACES = {vp: s.place_id for vp, s in pr.SITES.items()}
SHA = "ab" * 32


def example() -> dict:
    """The pinned example, decoded."""
    return trace_example.load()


def rust_enum(source: str, name: str) -> list[str]:
    """The serialised names of a Rust enum's variants (snake_case, or `#[serde(rename)]`)."""
    m = re.search(rf"pub enum {name} \{{(.*?)\n\}}", source, re.S)
    assert m, name
    names, rename = [], None
    for line in m[1].splitlines():
        line = line.strip()
        if r := re.match(r'#\[serde\(rename = "([^"]+)"\)\]', line):
            rename = r[1]
        elif v := re.match(r"([A-Z][A-Za-z0-9]*)\s*[,(]", line):
            names.append(rename or re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", v[1]).lower())
            rename = None
    return names


def rust_fields(source: str, name: str) -> list[str]:
    """The field names of a Rust struct with named fields, in order."""
    m = re.search(rf"pub struct {name} \{{(.*?)\n\}}", source, re.S)
    assert m, name
    return re.findall(r"^\s*(?:pub )?([a-z_0-9]+):", m[1], re.M)


class ThePinnedExample(unittest.TestCase):
    """The writer's own output reads back with every schema-2 field."""

    def test_the_example_lives_in_the_rust_test(self) -> None:
        self.assertEqual(example()["schema"], tr.SCHEMA)
        rust = trace_example.TRACE_FIELDS_RS.read_text(encoding="utf-8")
        # The Rust test that compares the writer's JSON with this very string.
        self.assertIn("fn schema_two_example_matches_the_scorers_copy()", rust)
        self.assertIn("SCHEMA_TWO_EXAMPLE", rust.split("fn schema_two_example_matches")[1])
        self.assertIsNot(example(), example())  # a fresh copy each call

    def test_reads_back(self) -> None:
        line = tr.parse_trace(example(), "example", PLACES)
        self.assertEqual(
            (line.context_kind, line.context_viewpoint_id), ("place", "rainier.paradise-mountain")
        )
        self.assertEqual(line.context_place_id, "rainier")
        self.assertEqual(line.display, "asleep")
        self.assertEqual(line.cache_state, "idle")
        self.assertEqual(
            line.setup,
            tr.Setup("dictation_transcriber", 0, "lingering", True, False, True, True),
        )
        self.assertEqual(line.replay, tr.Replay("3b4256f1cf51", "t032", 1, 2, 31))
        self.assertEqual(line.marks["key_up"] - line.marks["key_down"], 1_100_000_000)
        self.assertEqual(line.marks["audio_first_sample"], 1_040_000_000)
        lags = [(s.name, s.seq, s.dur_ns) for s in line.spans if s.name.endswith("_lag")]
        self.assertEqual(
            lags,
            [
                ("partial_lag", 0, 80_000_000),
                ("partial_lag", 1, 120_000_000),
                ("stable_lag", 0, 150_000_000),
            ],
        )
        self.assertEqual(
            [(p.tier, p.source, p.scene, p.matches_final) for p in line.paints],
            [
                ("tier0", "partial", 0, False),
                ("tier0", "final", 1, True),
                ("tier1", "final", 1, True),
            ],
        )
        self.assertEqual((line.transcript_words, line.partials), (3, 3))

    def test_no_string_but_enum_names_times_and_ids(self) -> None:
        enums = set().union(
            tr.OUTCOMES,
            tr.PATHS,
            tr.CACHE_STATES,
            tr.THERMAL,
            tr.POWER,
            tr.NETWORK,
            tr.DISPLAY,
            tr.ENGINES,
            tr.RETENTION,
            tr.TIERS,
            tr.SOURCES,
            tr.CONTEXT_KINDS,
            tr.SPANS,
        )

        def walk(v: object, key: str) -> None:
            if isinstance(v, str):
                ok = (
                    v in enums
                    or tr.WALL.fullmatch(v)
                    or (key == "viewpoint_id" and v in PLACES)
                    or (key == "place_id" and v in PLACES.values())
                    or (key == "script_hash" and tr.SCRIPT_HASH.fullmatch(v))
                    or (key == "utterance" and tr.SCRIPTED_ID.fullmatch(v))
                )
                self.assertTrue(ok, f"{key}: {v!r}")
            elif isinstance(v, list):
                for x in v:
                    walk(x, key)
            elif isinstance(v, dict):
                for k, x in v.items():
                    walk(x, k)

        walk(example(), "")


class TheReaderMatchesTheWriter(unittest.TestCase):
    """The reader's names are trace.rs's (and query.rs's and lifecycle.rs's), one by one."""

    def test_enums(self) -> None:
        source = TRACE_RS.read_text(encoding="utf-8")
        for rust, python in (
            ("Mark", tr.MARKS),
            ("SpanName", tr.SPANS),
            ("CacheState", tr.CACHE_STATES),
            ("ThermalState", tr.THERMAL),
            ("PowerSource", tr.POWER),
            ("NetworkInterface", tr.NETWORK),
            ("DisplayState", tr.DISPLAY),
            ("Outcome", tr.OUTCOMES),
            ("EngineKind", tr.ENGINES),
            ("ModelRetention", tr.RETENTION),
            ("PaintSource", tr.SOURCES),
            ("ContextPlace", tr.CONTEXT_KINDS),
        ):
            self.assertEqual(tuple(rust_enum(source, rust)), tuple(python), rust)
        self.assertEqual(tuple(rust_enum(LIFECYCLE_RS.read_text("utf-8"), "Tier")), tr.TIERS)
        self.assertEqual(tuple(rust_enum(QUERY_RS.read_text("utf-8"), "ParsePath")), tr.PATHS)
        self.assertIn(f"pub const TRACE_SCHEMA: u32 = {tr.SCHEMA};", source)

    def test_struct_fields(self) -> None:
        # The keys the reader requires are the writer's fields, so a new field fails here.
        source = TRACE_RS.read_text(encoding="utf-8")
        obj = example()
        for rust, keys in (
            ("Levers", list(tr.LEVERS)),
            ("SpeechSetup", list(obj["speech"])),
            ("TraceState", list(obj["state"])),
            ("Lengths", list(obj["lengths"])),
            ("Paint", list(obj["paints"][0])),
            ("ReplayTrial", list(obj["replay"])),
        ):
            self.assertEqual(rust_fields(source, rust), keys, rust)
        record = rust_fields(source, "TraceRecord")
        self.assertEqual(sorted(record), sorted(obj), "TraceRecord")
        for name in record:  # every field, even an optional one, is read or refused by name
            example_without = copy.deepcopy(obj)
            example_without.pop(name)
            if name in ("path", "replay"):
                tr.parse_trace(example_without, "x", PLACES)
            else:
                with self.assertRaises(tr.InputError, msg=name):
                    tr.parse_trace(example_without, "x", PLACES)


class Refusals(unittest.TestCase):
    """A malformed line is an error naming where, never a guess."""

    def refused(self, mutate, needle: str) -> None:
        obj = example()
        mutate(obj)
        with self.assertRaises(tr.InputError) as caught:
            tr.parse_trace(obj, "line 7", PLACES)
        self.assertIn("line 7", str(caught.exception))
        self.assertIn(needle, str(caught.exception))

    def test_schema_and_keys(self) -> None:
        self.refused(lambda o: o.update(schema=1), "schema 1")
        self.refused(lambda o: o.update(transcript="Is Rainier out?"), "unknown transcript")
        self.refused(lambda o: o.pop("context"), "missing context")
        self.refused(lambda o: o["state"].pop("display"), "missing display")
        self.refused(lambda o: o["marks"].update(key_wiggle=o["marks"]["key_up"]), "key_wiggle")

    def test_enums_and_types(self) -> None:
        self.refused(lambda o: o["state"].update(display="dim"), "display")
        self.refused(lambda o: o["speech"].update(engine="whisper"), "engine")
        self.refused(lambda o: o["speech"].update(warm_up="yes"), "warm_up")
        self.refused(lambda o: o["speech"].pop("warm_up"), "missing warm_up")
        self.refused(lambda o: o["levers"].update(partial_paint=1), "partial_paint")
        self.refused(lambda o: o["levers"].update(overlay=True), "unknown overlay")
        self.refused(lambda o: o.pop("levers"), "missing levers")
        self.refused(lambda o: o["paints"][0].update(tier="tier3"), "tier")
        self.refused(lambda o: o["paints"][0].update(matches_final="yes"), "matches_final")
        self.refused(lambda o: o.update(query_gen=-1), "query_gen")
        self.refused(lambda o: o.update(query_gen=True), "query_gen")
        self.refused(lambda o: o["marks"]["key_up"].update(wall="yesterday"), "wall")

    def test_levers_may_be_null(self) -> None:
        obj = example()
        obj["levers"] = dict.fromkeys(tr.LEVERS)
        obj["speech"]["warm_up"] = None
        line = tr.parse_trace(obj, "x", PLACES)
        self.assertEqual(
            line.setup, tr.Setup("dictation_transcriber", 0, "lingering", None, None, None, None)
        )

    def test_not_finite_numbers_are_refused_with_the_line(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        good = json.dumps(example())
        for token in ("NaN", "Infinity", "-Infinity"):
            path = d / "traces.jsonl"
            path.write_text(good + "\n" + good.replace('"query_gen": 31', f'"query_gen": {token}'))
            with self.assertRaises(tr.InputError) as caught:
                list(tr.read_jsonl(path))
            self.assertIn("traces.jsonl:2", str(caught.exception))
            self.assertIn(f"{token} is not a finite number", str(caught.exception))

    def test_context_and_replay_ids(self) -> None:
        self.refused(
            lambda o: o["context"].update(viewpoint_id="acadia.mcfarland"), "not a manifest"
        )
        self.refused(lambda o: o["context"].update(kind="maybe"), "context.kind")
        self.refused(lambda o: o["context"].update(kind="empty"), "unknown place_id")
        self.refused(lambda o: o["replay"].update(script_hash="3B4256F1CF51"), "script_hash")
        self.refused(lambda o: o["replay"].update(utterance="rainier at nine"), "utterance")
        self.refused(lambda o: o["replay"].update(take=0), "take")

    def test_span_arithmetic(self) -> None:
        self.refused(lambda o: o["spans"][0].update(dur_ns=1), "dur_ns")

    def test_optional_blocks(self) -> None:
        obj = example()
        del obj["replay"], obj["path"]
        line = tr.parse_trace(obj, "x", PLACES)
        self.assertIsNone(line.replay)
        self.assertIsNone(line.path)
        obj = copy.deepcopy(example())
        obj["context"] = {"kind": "empty"}
        self.assertEqual(tr.parse_trace(obj, "x", PLACES).context_kind, "empty")


class SideFiles(unittest.TestCase):
    """Transcripts and sidecars."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, True))

    def test_transcripts(self) -> None:
        path = self.dir / "transcripts.jsonl"
        path.write_text(
            '{"session": 1, "trial": 1, "final": "made up words"}\n\n'
            '{"session": 1, "trial": 2, "final": "", "utterance": "t001", "synthetic": true}\n'
        )
        got = tr.read_transcripts(path)
        self.assertEqual(got[(1, 1)].final, "made up words")
        self.assertEqual((got[(1, 2)].utterance, got[(1, 2)].synthetic), ("t001", True))
        path.write_text(path.read_text() + '{"session": 1, "trial": 1, "final": "again"}\n')
        with self.assertRaises(tr.InputError):
            tr.read_transcripts(path)
        path.write_text("not json\n")
        with self.assertRaises(tr.InputError):
            tr.read_transcripts(path)

    def test_sidecars(self) -> None:
        path = tr.sidecar_path(self.dir, "3b4256f1cf51", "t001", 1)
        self.assertEqual(path, self.dir / "scripted" / "3b4256f1cf51" / "t001.1.json")
        path.parent.mkdir(parents=True)
        side = {
            "id": "t001",
            "take": 1,
            "script_hash": "3b4256f1cf51",
            "key_up_ms": 1900.0,
            "speech_onset_ms": 210,
            "speech_end_ms": 1950.5,
        }
        path.write_text(json.dumps(side))
        got = tr.read_sidecar(path, "3b4256f1cf51", "t001", 1)
        self.assertTrue(got.still_in_speech_at_key_up)
        self.assertFalse(got.synthetic)
        with self.assertRaises(tr.InputError):
            tr.read_sidecar(path, "3b4256f1cf51", "t001", 2)  # another take
        side["speech_end_ms"] = "late"
        path.write_text(json.dumps(side))
        with self.assertRaises(tr.InputError):
            tr.read_sidecar(path, "3b4256f1cf51", "t001", 1)

    def test_a_sidecar_number_must_be_finite(self) -> None:
        # json.loads takes NaN and Infinity by default; one would corrupt every percentile.
        path = tr.sidecar_path(self.dir, "3b4256f1cf51", "t001", 1)
        path.parent.mkdir(parents=True)
        base = '{"id": "t001", "take": 1, "script_hash": "3b4256f1cf51", "key_up_ms": 1900.0, '
        for onset in ("NaN", "Infinity", "-Infinity", "1e999"):
            path.write_text(base + f'"speech_onset_ms": {onset}, "speech_end_ms": 1950.5}}')
            with self.assertRaises(tr.InputError, msg=onset) as caught:
                tr.read_sidecar(path, "3b4256f1cf51", "t001", 1)
            self.assertIn("finite", str(caught.exception))

    def test_run_manifest(self) -> None:
        path = self.dir / "run.json"
        path.write_text('{"kind": "synthetic"}')
        self.assertEqual(tr.read_run_manifest(path)["kind"], "synthetic")
        measured = {"kind": "measured", "build_commit": "8fa6b6a", "parser_sha256": SHA}
        path.write_text(json.dumps({**measured, "tier2_enabled": False}))
        self.assertFalse(tr.read_run_manifest(path)["tier2_enabled"])
        for bad in (
            {"kind": "probably real"},
            {"kind": "measured"},  # a measured run names its build and its parser
            {"kind": "measured", "build_commit": "8fa6b6a"},
            {**measured, "build_commit": "HEAD"},
            {**measured, "parser_sha256": SHA[:-1]},
            {**measured, "tier2_enabled": "yes"},
            {"kind": "synthetic", "seed": float("nan")},
        ):
            path.write_text(json.dumps(bad))
            with self.assertRaises(tr.InputError, msg=bad):
                tr.read_run_manifest(path)


if __name__ == "__main__":
    unittest.main()
