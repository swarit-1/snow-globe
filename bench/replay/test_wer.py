"""Tests for wer.py: known-answer alignments and the normaliser's rules (bench/README.md
"Scoring"). Standard library only; every input is a made-up string, never a recording.

    python3 -I -B -m unittest discover -s bench/replay -t bench/replay -p 'test_*.py'
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "utterances"))

import check
import wer


class Alignment(unittest.TestCase):
    """Known answers for the edit counts."""

    def errors(self, ref: str, hyp: str) -> tuple[int, int, int, int]:
        e = wer.align(ref.split(), hyp.split())
        return e.substitutions, e.deletions, e.insertions, e.reference_words

    def test_known_alignments(self) -> None:
        cases = {
            ("a b c d", "a b c d"): (0, 0, 0, 4),
            ("a b c d", "a x c"): (1, 1, 0, 4),
            ("the cat sat on the mat", "the cat sit on mat"): (1, 1, 0, 6),
            ("the quick brown fox", "the quack brown fox jumps"): (1, 0, 1, 4),
            ("a b", ""): (0, 2, 0, 2),
            ("a", "a b c"): (0, 0, 2, 1),
            ("", "a"): (0, 0, 1, 0),
            ("a b c", "c b a"): (2, 0, 0, 3),
            ("x y z", "y z x"): (0, 1, 1, 3),
        }
        for (ref, hyp), want in cases.items():
            self.assertEqual(self.errors(ref, hyp), want, (ref, hyp))

    def test_wer_and_corpus_wer(self) -> None:
        e = wer.align(["a", "b", "c", "d"], ["a", "x", "c"])
        self.assertEqual((e.edits, e.wer), (2, 0.5))
        self.assertIsNone(wer.align([], []).wer)
        self.assertEqual(wer.align(["a"], ["a", "b", "c"]).wer, 2.0)  # WER can exceed 1
        pooled = [wer.align(["a", "b"], ["a"]), wer.align(["c", "d", "e", "f"], [])]
        self.assertEqual(wer.corpus_wer(pooled), (1 + 4) / (2 + 4))  # not the mean of 0.5, 1
        self.assertIsNone(wer.corpus_wer([]))

    def test_the_edit_count_is_minimal(self) -> None:
        # Against a brute-force edit distance on every pair of short sequences over {a, b}.
        import itertools

        def distance(r: tuple[str, ...], h: tuple[str, ...]) -> int:
            if not r or not h:
                return len(r) + len(h)
            return min(
                distance(r[1:], h[1:]) + (r[0] != h[0]),
                distance(r[1:], h) + 1,
                distance(r, h[1:]) + 1,
            )

        words = [()]
        for n in range(1, 5):
            words += list(itertools.product("ab", repeat=n))
        for r in words:
            for h in words:
                self.assertEqual(wer.align(r, h).edits, distance(r, h), (r, h))


class Normaliser(unittest.TestCase):
    """The rules of bench/README.md "Scoring", on made-up strings."""

    def test_numbers_are_spelt_as_check_py_spells_them(self) -> None:
        for n in range(60):
            self.assertEqual(wer.cardinal(n), check.cardinal(n), n)
        for n in range(1, 32):
            self.assertEqual(wer.ordinal(n), check.ordinal(n), n)
        self.assertEqual(wer.cardinal(75), "seventy-five")
        self.assertEqual(wer.cardinal(105), "one hundred five")
        self.assertEqual(wer.cardinal(2026), "two thousand twenty-six")
        self.assertEqual(wer.ordinal(40), "fortieth")
        self.assertEqual(wer.ordinal(101), "one hundred first")
        self.assertEqual(wer.ordinal(0), "zeroth")

    def test_any_engine_output_has_a_spelling(self) -> None:
        # One odd transcript must never stop a night's report (it once raised on "0th").
        self.assertEqual(wer.normalise("on the 0th"), ["on", "the", "zeroth"])
        self.assertEqual(wer.normalise("the 00th"), ["the", "zeroth"])
        e = wer.normalised_errors("Mount Rainier on the 1st", "Mount Rainier on the 0th")
        self.assertEqual((e.substitutions, e.reference_words), (1, 5))
        digits = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "zero"]
        self.assertEqual(
            wer.normalise("call 1234567890123"), ["call", *digits, "one", "two", "three"]
        )
        self.assertEqual(len(wer.normalise("9" * 5000 + "th")), 5000)  # past int()'s digit limit
        fullwidth_four, arabic_twelve, squared = chr(0xFF14), chr(0x0661) + chr(0x0662), chr(0xB2)
        self.assertEqual(wer.normalise(fullwidth_four), ["four"])  # NFKC folds it to "4"
        self.assertEqual(wer.normalise(arabic_twelve), ["twelve"])  # a decimal digit elsewhere
        for odd in ("4.30 pm", "1,000", "10:30a.m.", "x" + squared):
            wer.normalised_errors("Rainier at 4", odd)
        for odd in ("1/2", "\u0000", "0.0.0", "1st2nd", "12:345", "-7"):
            wer.normalised_errors("Rainier at 4", odd)

    def test_four_four_oclock_and_digits_are_one_word(self) -> None:
        for said in ("at 4", "at four", "at 4:00"):
            self.assertEqual(wer.normalise(said), ["at", "four"], said)
        self.assertEqual(wer.normalise("at 1:30"), ["at", "one", "thirty"])
        self.assertEqual(wer.normalise("at 2:05"), ["at", "two", "oh", "five"])
        self.assertEqual(wer.normalise("the 21st"), ["the", "twenty", "first"])
        self.assertEqual(wer.normalise("in 24 hours"), ["in", "twenty", "four", "hours"])
        self.assertEqual(wer.normalise("in twenty-four hours"), ["in", "twenty", "four", "hours"])

    def test_meridiem_forms_are_one_word(self) -> None:
        for said in ("10:30 am", "10:30 a.m.", "10:30 A.M.", "10:30 a m", "10:30am", "10:30 a.m"):
            self.assertEqual(wer.normalise(said), ["ten", "thirty", "am"], said)
        self.assertEqual(wer.normalise("8 p.m."), ["eight", "pm"])
        self.assertEqual(wer.normalise("4pm"), ["four", "pm"])

    def norm(self, ref: str, hyp: str) -> tuple[int, int, int, int]:
        e = wer.normalised_errors(ref, hyp)
        return e.substitutions, e.deletions, e.insertions, e.reference_words

    def test_any_alias_of_the_reference_place_is_no_error(self) -> None:
        for said in ("Mount Rainier", "Mt Rainier", "Mount Rainer", "Tahoma", "rainier"):
            self.assertEqual(self.norm("Mount Rainier", said), (0, 0, 0, 2), said)
        self.assertEqual(self.norm("Pt Reyes", "Point Reyes"), (0, 0, 0, 2))
        # Longest alias first: "grand canyon south rim" is one alias of the place.
        self.assertEqual(
            self.norm("Grand Canyon South Rim at 9", "grand canyon at nine"), (0,) * 3 + (6,)
        )
        # A word that is no alias stays as it is, and another place is still an error.
        self.assertEqual(self.norm("Point Reyes", "Point Raze"), (1, 0, 0, 2))
        self.assertEqual(self.norm("Grand Titan at noon", "Grand Canyon at noon"), (1, 0, 0, 4))
        self.assertEqual(self.norm("Mt Hood", "Mount Hood"), (0, 0, 0, 2))

    def test_n_is_the_words_the_owner_read(self) -> None:
        # One wrong word outside the place weighs what it weighs raw (t021's shape): normalising
        # never shrinks the reference to one token per place.
        ref = "Mount Rainier National Park next Thursday"
        raw = wer.raw_errors(ref, "Mount Rainier National Park next Tuesday")
        norm = wer.normalised_errors(ref, "Mount Rainier National Park next Tuesday")
        self.assertEqual((raw.edits, raw.reference_words), (1, 6))
        self.assertEqual((norm.edits, norm.reference_words), (1, 6))
        e = wer.normalised_errors(
            "Grand Canyon South Rim at 11:30", "Grand Canyon South Rim at 11:40"
        )
        self.assertEqual((e.edits, e.reference_words), (1, 7))  # raw: 1 of 6
        # Nothing heard is every word wrong, place words included.
        self.assertEqual(self.norm(ref, ""), (0, 6, 0, 6))
        # Part of a long alias: the reading as said (one word missing) beats the alias reading.
        self.assertEqual(self.norm(ref, "Rainier National Park next Thursday"), (0, 1, 0, 6))

    def test_place_readings(self) -> None:
        ref = wer.normalise("Is Mount Rainier out")
        readings = wer.place_readings(ref, wer.normalise("is tahoma out"))
        self.assertEqual(readings, [["is", "tahoma", "out"], ["is", "mount", "rainier", "out"]])
        # Only places the reference names are rewritten.
        self.assertEqual(
            wer.place_readings(ref, ["is", "acadia", "out"]), [["is", "acadia", "out"]]
        )
        # Many mentions stay bounded: as said, or every one as the reference's words.
        many = wer.normalise(" ".join(["tahoma"] * 20))
        self.assertEqual(len(wer.place_readings(ref, many)), 2)
        # As said: 4 substitutions and 16 insertions (the rewritten reading has 38 edits).
        e = wer.normalised_errors("Is Mount Rainier out", " ".join(["tahoma"] * 20))
        self.assertEqual((e.substitutions, e.insertions), (4, 16))

    def test_every_manifest_alias_is_one_span_of_its_place(self) -> None:
        import prd_reading as pr

        for site in pr.SITES.values():
            for alias in site.aliases:
                words = wer.normalise(alias)
                self.assertEqual(
                    wer.alias_spans(words), [wer.Span(0, len(words), site.place_id)], alias
                )

    def test_an_alias_of_two_places_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            wer.AliasTable.build([("grand", "grand-canyon"), ("grand", "grand-teton")])

    def test_every_script_line_normalises_like_its_spoken_form(self) -> None:
        rows, _ = check.load()
        for r in rows:
            text, said = r["text"], check.spoken(r["text"])
            self.assertEqual(wer.normalise(text), wer.normalise(said), r["id"])
            self.assertEqual(wer.normalised_errors(text, said).edits, 0, r["id"])
            # N never falls below the raw count on the script's lines.
            self.assertGreaterEqual(len(wer.normalise(text)), len(wer.raw_tokens(text)), r["id"])

    def test_raw_against_normalised(self) -> None:
        ref, hyp = "Pt Reyes at 10:30 am", "point reyes at ten thirty a.m."
        raw = wer.raw_errors(ref, hyp)
        self.assertEqual((raw.edits, raw.reference_words), (4, 5))
        norm = wer.normalised_errors(ref, hyp)
        self.assertEqual((norm.edits, norm.reference_words), (0, 6))
        self.assertEqual(wer.raw_tokens("Is Rainier out?"), ["is", "rainier", "out"])
        # Raw keeps case-folded words with their inner punctuation.
        self.assertEqual(wer.raw_tokens("at 10:30 a.m."), ["at", "10:30", "a.m"])

    def test_a_misheard_place_is_still_an_error(self) -> None:
        e = wer.normalised_errors("Rainier tomorrow at 4", "rain ear tomorrow at four")
        self.assertEqual((e.substitutions, e.insertions, e.reference_words), (1, 1, 4))


if __name__ == "__main__":
    unittest.main()
