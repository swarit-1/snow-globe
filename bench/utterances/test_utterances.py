"""Tests for the scripted utterances and their checker (PRD §7.6, §10.6).

Run from the repository root (stdlib only, no network, no audio):

    python3 -I -B -m unittest discover -s bench/utterances -t bench/utterances -p 'test_*.py'

The parser tests run only when the `snow-globe-parse` binary exists (see check.py); they are
skipped, never faked, without it. Parser outputs used by the unit tests below were recorded from
that binary on 20 Nov 2026 (snow-globe-core at 8246a8b, then named lookout-core and lookout-parse;
D-080).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import check
import prd_reading as pr

ROWS, CLOCKS = check.load()
BINARY = check.find_binary(None)
STALE = check.newer_sources(BINARY) if BINARY.exists() else []
BY_ID = {r["id"]: r for r in ROWS}


def row(**over: str) -> dict[str, str]:
    """A synthetic resolved row for compare(): the PRD §7.3 example, with overrides."""
    base = {c: "" for c in pr.COLUMNS}
    base.update(
        expected_outcome="query",
        expected_viewpoint_id="rainier.paradise-mountain",
        expected_rule="day-plus-bare-hour",
        expected_local="2026-11-20T16:00:00-08:00",
        expected_utc="2026-11-21T00:00:00Z",
        expected_chips="ampm:pm>2026-11-20T04:00:00-08:00",
        tolerance_s="0",
        parser_outcome="query",
        parser_viewpoint_id="rainier.paradise-mountain",
        parser_rule="day-plus-bare-hour",
        parser_local="2026-11-20T16:00:00-08:00",
        parser_utc="2026-11-21T00:00:00Z",
        parser_chips="ampm:pm>2026-11-20T04:00:00-08:00",
    )
    base.update(over)
    return base


class Script(unittest.TestCase):
    """The TSV is well formed, complete, and what prd_reading.py generates."""

    def test_structure_is_sound(self) -> None:
        self.assertEqual(check.validate(ROWS, CLOCKS), [])

    def test_thirty_dev_and_a_hundred_test(self) -> None:
        self.assertEqual(sum(r["split"] == "dev" for r in ROWS), 30)
        self.assertEqual(sum(r["split"] == "test" for r in ROWS), 100)

    def test_every_alias_and_viewpoint_is_said(self) -> None:
        cov = check.coverage(ROWS)
        self.assertEqual(cov["aliases_missing"], [])
        self.assertEqual(cov["aliases"], 77)
        test_rows = [r for r in ROWS if r["split"] == "test"]
        for vp, n in check.coverage(test_rows)["rows_per_viewpoint"].items():
            self.assertGreaterEqual(n, 5, f"{vp} has only {n} test rows")

    def test_every_prd_74_rule_is_in_the_test_split(self) -> None:
        rules = check.coverage([r for r in ROWS if r["split"] == "test"])["rules"]
        for rule in (
            "bare-hour",
            "explicit-hour",
            "day-plus-bare-hour",
            "day-plus-explicit-hour",
            "now",
            "no-time",
            "relative",
            "noon",
            "midnight",
            "sunrise",
            "sunset",
            "dawn",
            "dusk",
            "golden-hour",
            "blue-hour",
            "part-of-day",
            "tonight",
            "day-only",
        ):
            self.assertIn(rule, rules)

    def test_required_cases_are_present(self) -> None:
        tags = check.coverage(ROWS)["tags"]
        for tag in (
            "dst-fold",
            "dst-gap",
            "phoenix",
            "cross-zone",
            "close-call",
            "no-match",
            "follow-up",
            "next-weekday",
            "ampm",
            "number-word",
            "phonetic",
            "horizon-gfs",
            "out-of-range",
            "in-repeat",
            "prd-example",
            "fallback",
        ):
            self.assertIn(tag, tags)

    def test_expected_columns_are_generated_not_hand_edited(self) -> None:
        fresh = pr.build_rows()
        for disk, gen in zip(ROWS, fresh, strict=True):
            for col in pr.COLUMNS[:14]:
                self.assertEqual(disk[col], gen[col], f"{disk['id']} {col}")

    def test_script_hash_covers_only_what_is_read(self) -> None:
        h = check.script_hash(ROWS)
        self.assertRegex(h, r"^[0-9a-f]{12}$")
        edited = [dict(r) for r in ROWS]
        edited[0]["expected_local"] = "changed"
        edited[0]["parser_chips"] = "changed"
        self.assertEqual(check.script_hash(edited), h)
        edited[0]["text"] += "!"
        self.assertNotEqual(check.script_hash(edited), h)

    def test_clocks_file_matches_the_code(self) -> None:
        self.assertEqual(CLOCKS, {k: v[0] for k, v in pr.CLOCKS.items()})

    def test_stored_verdicts_are_what_compare_gives(self) -> None:
        self.assertEqual(
            check.stale_verdicts(ROWS), [], "rerun check.py --write (or --no-parser --write)"
        )

    def test_a_hand_edited_verdict_is_caught(self) -> None:
        rows = [dict(r) for r in ROWS]
        tacoma = next(r for r in rows if r["text"] == "Tacoma at sunset")
        tacoma["agreement"], tacoma["difference"] = "yes", ""
        self.assertEqual(check.stale_verdicts(rows), [tacoma["id"]])

    def test_follow_ups_come_straight_after_their_context_place(self) -> None:
        self.assertEqual(check.follow_up_errors(ROWS), [])
        rows = [dict(r) for r in ROWS]
        t005 = next(r for r in rows if r["id"] == "t005")
        t005["context"] = "point-reyes"  # the builder's context: the row before is Grand Canyon
        self.assertEqual(len(check.follow_up_errors(rows)), 1)
        rows = [dict(r) for r in ROWS]
        i = next(i for i, r in enumerate(rows) if r["text"] == "And at sunrise?")
        rows[i - 1], rows[i + 1] = rows[i + 1], rows[i - 1]  # the original order: after a miss
        self.assertIn("plain query", check.follow_up_errors(rows)[0])

    def test_owner_pending_rows(self) -> None:
        pending = {r["text"] for r in ROWS if check.PENDING in check.tags_of(r)}
        self.assertEqual(
            pending,
            {
                "Rainier Valley tomorrow at 9",
                "Show me Tetonia at 8",
                "Grand at sunset",
                "Paradise Valley at 9",
                "Tacoma at sunset",
                "Yosemite Valley at sunset",
            },
        )
        for r in ROWS:
            if check.PENDING in check.tags_of(r):
                self.assertTrue(r["prd_note"].startswith("owner-pending:"), r["id"])

    def test_every_number_has_a_spoken_form(self) -> None:
        for r in ROWS:
            self.assertNotRegex(check.spoken(r["text"]), r"\d", r["id"])
        self.assertEqual(check.spoken("Pt Reyes at 10:30 am"), "Pt Reyes at ten thirty a.m.")
        self.assertEqual(
            check.spoken("Smokey Mountains on the 22nd"), "Smokey Mountains on the twenty-second"
        )
        self.assertEqual(
            check.spoken("at 4 pm on November 21st"), "at four p.m. on November twenty-first"
        )
        self.assertEqual(check.spoken("in 24 hours at 2:05"), "in twenty-four hours at two oh five")
        self.assertEqual(check.spoken("December 1st at 12"), "December first at twelve")

    def test_regeneration_keeps_parser_output_and_blanks_verdicts(self) -> None:
        fresh = pr.build_rows()
        unrun = pr.merge_parser_output(fresh, ROWS)
        self.assertEqual(unrun, [])
        for disk, gen in zip(ROWS, fresh, strict=True):
            for col in pr.PARSER_OUTPUT_COLUMNS:
                self.assertEqual(gen[col], disk[col], f"{disk['id']} {col}")
            self.assertEqual((gen["agreement"], gen["difference"]), ("", ""))
        moved = [dict(r) for r in reversed(ROWS)]  # reordering keeps the parser output
        fresh = pr.build_rows()
        self.assertEqual(pr.merge_parser_output(fresh, moved), [])
        changed = [dict(r) for r in ROWS]
        changed[0]["text"] += " please"  # another text: the old output no longer applies
        fresh = pr.build_rows()
        self.assertEqual(pr.merge_parser_output(fresh, changed), [ROWS[0]["id"]])

    def test_prd_73_example(self) -> None:
        t001 = next(r for r in ROWS if r["id"] == "t001")
        self.assertEqual(t001["text"], "Is Rainier out tomorrow at 4?")
        self.assertEqual(
            (
                t001["expected_viewpoint_id"],
                t001["expected_rule"],
                t001["expected_local"],
                t001["expected_utc"],
                t001["expected_chips"],
            ),
            (
                "rainier.paradise-mountain",
                "day-plus-bare-hour",
                "2026-11-20T16:00:00-08:00",
                "2026-11-21T00:00:00Z",
                "ampm:pm>2026-11-20T04:00:00-08:00",
            ),
        )


class Reading(unittest.TestCase):
    """The PRD reading's own pieces: the DST rule and the Sun."""

    def test_gap_moves_forward_by_the_gap(self) -> None:
        t, flag = pr.localize(ZoneInfo("America/Los_Angeles"), datetime(2027, 3, 14, 2, 30))
        self.assertEqual((t.isoformat(), flag), ("2027-03-14T03:30:00-07:00", "dst_gap"))

    def test_repeat_takes_the_earlier_offset(self) -> None:
        t, flag = pr.localize(ZoneInfo("America/Los_Angeles"), datetime(2026, 11, 1, 1, 30))
        self.assertEqual((t.isoformat(), flag), ("2026-11-01T01:30:00-07:00", "dst_fold"))

    def test_phoenix_has_neither(self) -> None:
        for naive in (datetime(2027, 3, 14, 2, 30), datetime(2026, 11, 1, 1, 30)):
            t, flag = pr.localize(ZoneInfo("America/Phoenix"), naive)
            self.assertIsNone(flag)
            self.assertEqual(t.utcoffset(), timedelta(hours=-7))

    def test_inside_the_repeat_the_second_pass_is_next(self) -> None:
        site = pr.SITES["rainier.paradise-mountain"]
        now = datetime.fromisoformat("2026-11-01T01:15:00-08:00").astimezone(UTC)
        t, flag = pr.wall(site, datetime(2026, 11, 1).date(), 1, 30, now)
        self.assertEqual(
            (t.astimezone(UTC).isoformat(), flag), ("2026-11-01T09:30:00+00:00", "dst_fold")
        )

    def test_crossings_sit_on_their_altitude(self) -> None:
        site = pr.SITES["rainier.paradise-mountain"]
        start, end = pr.local_day(site, datetime(2026, 11, 20).date())
        for event, (threshold, _, _) in pr.EVENTS.items():
            t = pr.find_crossing(site, event, start, end)
            self.assertIsNotNone(t, event)
            self.assertAlmostEqual(pr.sun_altitude_deg(t, site), threshold, delta=0.001, msg=event)

    def test_d039_the_nth_without_a_month_is_the_next_such_day(self) -> None:
        today = date(2026, 11, 19)
        self.assertEqual(pr.day_date(("date", None, 31), today), (date(2026, 12, 31), "fixed"))
        self.assertEqual(pr.day_date(("date", None, 22), today), (date(2026, 11, 22), "fixed"))
        self.assertEqual(pr.day_date(("date", None, 18), today), (date(2026, 12, 18), "fixed"))

    def test_d039_a_month_and_day_is_the_nearest_such_date(self) -> None:
        today = date(2026, 11, 19)
        self.assertEqual(pr.day_date(("date", 11, 18), today)[0], date(2026, 11, 18))  # yesterday
        self.assertEqual(pr.day_date(("date", 12, 1), today)[0], date(2026, 12, 1))
        self.assertEqual(pr.day_date(("date", 1, 5), today)[0], date(2027, 1, 5))
        site = pr.SITES["rainier.paradise-mountain"]
        now = pr.clock_instant("C1")
        self.assertEqual(
            pr.read_time(("dayonly", ("date", 11, 18)), site, now).outcome, "out-of-range-past"
        )

    def test_d039_a_day_alone_naming_today(self) -> None:
        site = pr.SITES["rainier.paradise-mountain"]
        morning = pr.clock_instant("C1")  # 10:00 PST: noon is ahead
        r = pr.read_time(("dayonly", "today"), site, morning)
        self.assertEqual(
            (r.rule, pr.fmt_local(r.t, site)), ("day-only", "2026-11-19T12:00:00-08:00")
        )
        afternoon = datetime.fromisoformat("2026-11-19T13:00:00-08:00").astimezone(UTC)
        for day in ("today", ("wd", "thu"), ("date", 11, 19)):
            r = pr.read_time(("dayonly", day), site, afternoon)
            self.assertEqual(
                (r.outcome, r.rule, r.t, r.chips),
                ("query", "day-only-now", afternoon, ("live",)),
                day,
            )
        r = pr.read_time(("dayonly", ("next", "thu")), site, afternoon)  # a week on, never today
        self.assertEqual(
            (r.rule, pr.fmt_local(r.t, site)), ("day-only", "2026-11-26T12:00:00-08:00")
        )

    def test_day_order_of_events(self) -> None:
        site = pr.SITES["point-reyes.beach"]
        d = datetime(2026, 11, 20).date()
        order = [
            "dawn",
            "blue_morning",
            "sunrise",
            "golden_morning",
            "golden_evening",
            "sunset",
            "blue_evening",
            "dusk",
        ]
        times = [pr.event_on(site, e, d) for e in order]
        self.assertEqual(times, sorted(times))


class Compare(unittest.TestCase):
    """compare() classes each difference as hard or soft."""

    def test_identical_rows_agree(self) -> None:
        self.assertEqual(check.compare(row()), ("yes", ""))

    def test_a_second_off_is_a_disagreement_without_tolerance(self) -> None:
        verdict, why = check.compare(
            row(parser_utc="2026-11-21T00:00:01Z", parser_local="2026-11-20T16:00:01-08:00")
        )
        self.assertEqual(verdict, "no")
        self.assertIn("+1 s", why)

    def test_solar_tolerance(self) -> None:
        r = row(
            tolerance_s="60",
            parser_utc="2026-11-21T00:00:59Z",
            parser_local="2026-11-20T16:00:59-08:00",
        )
        self.assertEqual(check.compare(r)[0], "yes")
        r = row(
            tolerance_s="60",
            parser_utc="2026-11-21T00:01:01Z",
            parser_local="2026-11-20T16:01:01-08:00",
        )
        self.assertEqual(check.compare(r)[0], "no")

    def test_same_instant_wrong_offset(self) -> None:
        r = row(parser_local="2026-11-20T17:00:00-07:00")
        self.assertEqual(check.compare(r), ("no", "offset: expected -08:00, parser -07:00"))

    def test_ampm_alternative_must_match(self) -> None:
        r = row(parser_chips="ampm:pm>2026-11-21T04:00:00-08:00")
        self.assertEqual(check.compare(r)[0], "no")

    def test_rolled_flags_are_soft(self) -> None:
        r = row(parser_chips="ampm:pm>2026-11-20T04:00:00-08:00;rolled_to_next_week")
        self.assertEqual(check.compare(r)[0], "soft")

    def test_dst_flags_are_hard(self) -> None:
        r = row(parser_chips="ampm:pm>2026-11-20T04:00:00-08:00;dst_fold")
        self.assertEqual(check.compare(r)[0], "no")

    def test_miss_versus_fallback_is_soft(self) -> None:
        r = row(
            expected_outcome="miss",
            expected_viewpoint_id="no-match",
            parser_outcome="fallback/unrecognised",
            parser_viewpoint_id="",
        )
        self.assertEqual(check.compare(r)[0], "soft")

    def test_a_guess_is_hard(self) -> None:
        r = row(expected_outcome="miss", expected_viewpoint_id="no-match")
        self.assertEqual(check.compare(r)[0], "no")

    def test_an_unknown_parser_chip_is_hard(self) -> None:
        for extra in ("stale_forecast", "date:2026-11-20"):
            r = row(parser_chips=f"ampm:pm>2026-11-20T04:00:00-08:00;{extra}")
            verdict, why = check.compare(r)
            self.assertEqual(verdict, "no", extra)
            self.assertIn(extra, why)

    def test_close_call_needs_a_chip_and_accepts_either_place(self) -> None:
        both = "rainier.paradise-mountain|point-reyes.beach"
        r = row(
            expected_viewpoint_id=both,
            expected_chips="ampm:pm>2026-11-20T04:00:00-08:00;place:*",
            parser_viewpoint_id="point-reyes.beach",
            parser_chips="ampm:pm>2026-11-20T04:00:00-08:00;place:rainier",
        )
        self.assertEqual(check.compare(r)[0], "yes")
        r["parser_chips"] = "ampm:pm>2026-11-20T04:00:00-08:00"
        self.assertEqual(check.compare(r)[0], "no")


class ParserColumns(unittest.TestCase):
    """parser_columns() on outputs recorded from lookout-parse (8246a8b, 20 Nov 2026; D-080)."""

    def test_query(self) -> None:
        out = {
            "place_id": "rainier",
            "viewpoint_id": "rainier.paradise-mountain",
            "valid_time": "2026-11-20T16:00:00-08:00",
            "valid_time_utc": "2026-11-21T00:00:00Z",
            "tz": "America/Los_Angeles",
            "time_source": {"kind": "clock", "text": "tomorrow at 4", "rule": "day-plus-bare-hour"},
            "ambiguities": [
                {"field": "meridiem", "chosen": "pm", "alternative": "2026-11-20T04:00:00-08:00"}
            ],
            "overrides": {},
            "path": "fast_path",
            "confidence": 0.97,
            "place_source": "utterance",
            "place_match": {"stage": "alias", "score": 1.0},
            "horizon": "hrrr",
            "flags": [],
            "_exit": 0,
        }
        cols = check.parser_columns(out)
        self.assertEqual(cols["parser_chips"], "ampm:pm>2026-11-20T04:00:00-08:00")
        self.assertEqual(cols["parser_rule"], "day-plus-bare-hour")

    def test_errors(self) -> None:
        miss = {"error": "no_match", "text": "golden gate", "closest": [], "_exit": 2}
        self.assertEqual(check.parser_columns(miss)["parser_outcome"], "miss/no_match")
        self.assertEqual(check.parser_columns(miss)["parser_viewpoint_id"], "no-match")
        fb = {"error": "unrecognised", "reason": "words that are neither ...: valley", "_exit": 2}
        self.assertEqual(check.parser_columns(fb)["parser_outcome"], "fallback/unrecognised")
        oor = {
            "error": "out_of_range",
            "reason": "beyond_horizon",
            "place_id": "rainier",
            "viewpoint_id": "rainier.paradise-mountain",
            "requested": "2026-11-26T12:00:00-08:00",
            "latest_valid": "2026-11-26T10:00:00-08:00",
            "_exit": 2,
        }
        cols = check.parser_columns(oor)
        self.assertEqual(
            (cols["parser_outcome"], cols["parser_utc"]),
            ("out-of-range-beyond", "2026-11-26T20:00:00Z"),
        )


class BinaryFreshness(unittest.TestCase):
    """newer_sources() sees a build input written after the binary."""

    def test_an_old_binary_is_stale(self) -> None:
        with tempfile.NamedTemporaryFile() as f:
            old = pr.REPO / "viewpoints" / "manifests"
            newest = max(p.stat().st_mtime for p in old.glob("*.json"))
            os.utime(f.name, (newest - 60, newest - 60))
            self.assertTrue(
                any(
                    p.startswith("viewpoints/manifests/") for p in check.newer_sources(Path(f.name))
                )
            )
            os.utime(f.name, (newest + 3600 * 24 * 365, newest + 3600 * 24 * 365))
            self.assertEqual(check.newer_sources(Path(f.name)), [])


@unittest.skipUnless(
    BINARY.exists() and not STALE, f"snow-globe-parse not built at {BINARY}, or older than {STALE}"
)
class LiveParser(unittest.TestCase):
    """Against the real binary: the stored parser columns are current, and the Sun agrees."""

    def test_parser_columns_are_current(self) -> None:
        stale = []
        for r in ROWS:
            fresh = check.parser_columns(check.run_parser(BINARY, r, CLOCKS))
            if any(r[k] != v for k, v in fresh.items()):
                stale.append(r["id"])
        self.assertEqual(stale, [], "rerun check.py --write after a parser or manifest change")

    def test_solar_times_agree_within_two_seconds(self) -> None:
        for r in ROWS:
            if r["tolerance_s"] == "60" and r["parser_utc"] and r["agreement"] != "no":
                d = abs(
                    (
                        check._parse_utc(r["parser_utc"]) - check._parse_utc(r["expected_utc"])
                    ).total_seconds()
                )
                self.assertLessEqual(d, 2, r["id"])


class Names(unittest.TestCase):
    """The checker reads the renamed variables and data dir, with the old names as a fallback
    (D-080)."""

    def setUp(self) -> None:
        self.saved = dict(os.environ)
        for name in list(os.environ):
            if name.startswith(("SNOW_GLOBE_", "LOOKOUT_")) or name in ("HOME", "CARGO_TARGET_DIR"):
                del os.environ[name]
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        os.environ["HOME"] = str(self.home)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.saved)

    def test_new_name_first_then_the_old_one(self) -> None:
        self.assertIsNone(check.env("SNOW_GLOBE_PARSE"))
        os.environ["LOOKOUT_PARSE"] = "/old/parse"
        self.assertEqual(check.find_binary(None), Path("/old/parse"))
        os.environ["SNOW_GLOBE_PARSE"] = " "  # blank counts as unset
        self.assertEqual(check.find_binary(None), Path("/old/parse"))
        os.environ["SNOW_GLOBE_PARSE"] = "/new/parse"
        self.assertEqual(check.find_binary(None), Path("/new/parse"))
        self.assertEqual(check.find_binary("/arg"), Path("/arg"))

    def test_data_dir_order(self) -> None:
        new, old = self.home / "snow-globe-data", self.home / "lookout-data"
        self.assertEqual(check.data_dir(), new)  # neither exists
        old.mkdir()
        self.assertEqual(check.data_dir(), old)  # until it is moved
        new.mkdir()
        with self.assertRaises(SystemExit) as caught:  # both, apart: ambiguous (D-080)
            check.data_dir()
        self.assertIn("different folders", str(caught.exception.code))
        new.rmdir()
        old.rename(new)
        old.symlink_to(new)  # what the switch leaves
        self.assertEqual(check.data_dir(), new)
        os.environ["LOOKOUT_DATA_DIR"] = "/legacy"
        self.assertEqual(check.data_dir(), Path("/legacy"))
        os.environ["SNOW_GLOBE_DATA_DIR"] = "/current"
        self.assertEqual(check.data_dir(), Path("/current"))
        target = Path("/current/cache/cargo-target/release/snow-globe-parse")
        self.assertEqual(check.find_binary(None), target)
        os.environ["CARGO_TARGET_DIR"] = "/t"
        self.assertEqual(check.find_binary(None), Path("/t/release/snow-globe-parse"))


if __name__ == "__main__":
    unittest.main()
