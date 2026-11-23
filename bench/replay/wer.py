"""Word error rate for the replay report, raw and normalised (PRD §10.6, §14; bench/README.md
"Scoring", D-057).

Both rates compare the engine's final transcript (the hypothesis) with the script line the owner
read (the reference), word by word, with the usual edit distance: WER = (S + D + I) / N, where S,
D and I are the substitutions, deletions and insertions of a minimal alignment and N is the number
of reference words. A corpus WER sums S + D + I and N over utterances before dividing.

- **Raw:** lower case, split on white space, punctuation at either end of a word dropped. Nothing
  else, so "4" against "four" is an error.
- **Normalised:** both sides first go through `normalise()`, which applies the rules of
  bench/README.md "Scoring" in this order:
  1. numbers are spelt the way `check.py`'s `spoken()` spells them for the owner's reading list:
     "4", "4:00" and "four" are all "four"; "1:30" is "one thirty" and "2:05" "two oh five"; "21st"
     is "twenty-first". Numbers `spoken()` never meets (0th, 60 and up, decimals, thousands
     separators) are spelt by the same pattern, so engine output such as "75" is still words; a
     run of more than 12 digits is read digit by digit;
  2. "a.m.", "a.m", "am" and "a m" become the one word "am" (likewise "pm");
  3. a hyphen splits a word, on both sides ("twenty-four" is "twenty four");
  4. "mt" is "mount" and "pt" is "point".

  Then **places** (step 5, "any alias counts as its place's name"): where the hypothesis says a
  manifest alias (longest first) of a place that the reference also names, those words may be
  scored as the reference's own words for that place, and the alignment takes whichever choice
  gives fewer errors (`place_readings()`). So "Mount Rainer", "Rainier" and "Tahoma" against a
  reference "Mount Rainier" are no error, and a wrong place is still an error. The reference keeps
  its own words, so N is the number of words the owner read, numbers spelt out, never one token
  per place: a single error outside a place name weighs no more than it does raw (for every
  script line N is at least the raw count), and an empty hypothesis still scores 100 %.

Standard library only; nothing here reads audio.
"""

from __future__ import annotations

import itertools
import re
import sys
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "utterances"))

import prd_reading as pr

# ---------------------------------------------------------------- numbers in words

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty"}
_TENS[9] = "ninety"
_ORDINAL_WORD = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}
_SCALES = ((1_000_000_000, "billion"), (1_000_000, "million"), (1000, "thousand"))


def cardinal(n: int) -> str:
    """A non-negative integer in words, as `check.py` spells 0-59 ("twenty-four"), and by the same
    pattern beyond ("one hundred five", "two thousand twenty-six")."""
    if n < 0:
        raise ValueError("cardinal() takes a non-negative integer")
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]} hundred" + (f" {cardinal(rest)}" if rest else "")
    for size, name in _SCALES:
        if n >= size:
            high, rest = divmod(n, size)
            return f"{cardinal(high)} {name}" + (f" {cardinal(rest)}" if rest else "")
    raise AssertionError("unreachable")


def ordinal(n: int) -> str:
    """A non-negative integer as an ordinal in words ("zeroth", "twenty-second", "thirtieth");
    1-31 exactly as `check.py`'s `ordinal()`."""
    if n < 0:
        raise ValueError("ordinal() takes a non-negative integer")
    words = cardinal(n)
    head, sep, last = words.rpartition(" ")
    if not sep:
        head, sep, last = words.rpartition("-")
    if last in _ORDINAL_WORD:
        last = _ORDINAL_WORD[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return f"{head}{sep}{last}"


def _clock(m: re.Match[str]) -> str:
    """ "1:30" as "one thirty", "2:05" as "two oh five", "4:00" as "four" (check.py `spoken()`)."""
    h, mm = int(m[1]), int(m[2])
    if mm == 0:
        return cardinal(h)
    return f"{cardinal(h)} {'oh ' if mm < 10 else ''}{cardinal(mm)}"


#: A run of more digits than this is read digit by digit (no transcript says such a number, and
#: it keeps every input within int()'s and cardinal()'s reach).
_LONG_DIGITS = 12


def _digits(digits: str) -> str:
    """Each digit of `digits` as a word ("four two")."""
    return " ".join(_ONES[int(d)] for d in digits)


def _number_words(digits: str) -> str:
    """A run of digits as a cardinal, or digit by digit when longer than `_LONG_DIGITS`."""
    return _digits(digits) if len(digits) > _LONG_DIGITS else cardinal(int(digits))


def _ordinal_words(digits: str) -> str:
    """A run of digits before "st", "nd", "rd" or "th" as an ordinal (digit by digit if long)."""
    return _digits(digits) if len(digits) > _LONG_DIGITS else ordinal(int(digits))


def spell_numbers(text: str) -> str:
    """`text` (lower case) with every number written out in words (step 1 of `normalise()`).
    It never raises: every run of digits has a spelling."""
    out = re.sub(r"(\d)(?=[ap]\.?\s?m\b)", r"\1 ", text)  # "4pm" -> "4 pm", "10:30a.m." too
    out = re.sub(r"\b(\d{1,3}(?:,\d{3})+)\b", lambda m: m[1].replace(",", ""), out)
    out = re.sub(r"\b(\d{1,2}):(\d{2})\b", _clock, out)
    out = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", lambda m: _ordinal_words(m[1]), out)
    out = re.sub(r"\b(\d+)\.(\d+)\b", lambda m: f"{_number_words(m[1])} point {_digits(m[2])}", out)
    return re.sub(r"\b(\d+)\b", lambda m: _number_words(m[1]), out)


# ---------------------------------------------------------------- tokens

_ABBREVIATIONS = {"mt": "mount", "pt": "point"}
_MERIDIEM = re.compile(r"\b([ap])\s?\.\s?m\b\.?")


def _strip_edges(token: str) -> str:
    """`token` without punctuation or symbols at either end (an apostrophe inside stays)."""
    start, end = 0, len(token)
    while start < end and not token[start].isalnum():
        start += 1
    while end > start and not token[end - 1].isalnum():
        end -= 1
    return token[start:end]


def _fold(text: str) -> str:
    """NFKC, lower case, and straight apostrophes."""
    text = unicodedata.normalize("NFKC", text).lower()
    return text.replace(chr(0x2019), "'").replace(chr(0x2018), "'")


def raw_tokens(text: str) -> list[str]:
    """The raw WER's words: lower case, split on white space, edge punctuation dropped."""
    return [t for t in (_strip_edges(w) for w in _fold(text).split()) if t]


def normalise(text: str) -> list[str]:
    """Steps 1-4 of the module doc: numbers spelt, meridiem joined, hyphens split, abbreviations
    expanded. Step 5 (places) needs both sides, so it is in `place_readings()`."""
    out = spell_numbers(_fold(text))
    out = _MERIDIEM.sub(r"\1m", out)
    out = re.sub("[-/" + chr(0x2010) + "-" + chr(0x2015) + "]", " ", out)
    tokens = [t for t in (_strip_edges(w) for w in out.split()) if t]
    joined: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in ("a", "p") and i + 1 < len(tokens) and tokens[i + 1] == "m":
            joined.append(tokens[i] + "m")
            i += 2
            continue
        joined.append(_ABBREVIATIONS.get(tokens[i], tokens[i]))
        i += 1
    return joined


@dataclass(frozen=True)
class AliasTable:
    """Alias word sequences (already through steps 1-4) mapped to their place's id."""

    by_words: Mapping[tuple[str, ...], str]
    longest: int

    @staticmethod
    def build(aliases: Iterable[tuple[str, str]]) -> AliasTable:
        """From (alias, place_id) pairs. An alias claimed by two places is refused: the
        normaliser would otherwise hide a wrong place."""
        by_words: dict[tuple[str, ...], str] = {}
        for alias, place_id in aliases:
            words = tuple(normalise(alias))
            if not words:
                continue
            if by_words.get(words, place_id) != place_id:
                raise ValueError(f"alias {alias!r} names two places")
            by_words[words] = place_id
        return AliasTable(by_words, max((len(w) for w in by_words), default=0))


def manifest_aliases() -> AliasTable:
    """Every alias of every manifest in viewpoints/manifests (through prd_reading's reader)."""
    return AliasTable.build((a, s.place_id) for s in pr.SITES.values() for a in s.aliases)


_DEFAULT_ALIASES: AliasTable | None = None


def _default_aliases() -> AliasTable:
    global _DEFAULT_ALIASES
    if _DEFAULT_ALIASES is None:
        _DEFAULT_ALIASES = manifest_aliases()
    return _DEFAULT_ALIASES


def alias_spans(tokens: Sequence[str], aliases: AliasTable | None = None) -> list[Span]:
    """Every manifest alias in `tokens` (words already through steps 1-4), scanning left to right
    and taking the longest alias at each word."""
    table = aliases or _default_aliases()
    spans: list[Span] = []
    i = 0
    while i < len(tokens):
        for n in range(min(table.longest, len(tokens) - i), 0, -1):
            place = table.by_words.get(tuple(tokens[i : i + n]))
            if place:
                spans.append(Span(i, i + n, place))
                i += n
                break
        else:
            i += 1
    return spans


@dataclass(frozen=True)
class Span:
    """Words `start` to `end` (exclusive) are an alias of `place_id`."""

    start: int
    end: int
    place_id: str


#: The most readings `place_readings()` tries; past it, only "keep every alias as said" and
#: "every alias as the reference's first words for its place" (a transcript naming its place a
#: dozen times is not one the script asks for, and this keeps the work bounded).
MAX_READINGS = 256


def place_readings(
    reference: Sequence[str], hypothesis: Sequence[str], aliases: AliasTable | None = None
) -> list[list[str]]:
    """Step 5 of the module doc: the readings of `hypothesis` (words through steps 1-4) to align
    with `reference`. Each alias the hypothesis says for a place the reference names is either
    kept as said or replaced by one of the reference's own alias words for that place. The first
    reading keeps every word as said."""
    table = aliases or _default_aliases()
    forms: dict[str, list[tuple[str, ...]]] = {}
    for span in alias_spans(reference, table):
        words = tuple(reference[span.start : span.end])
        if words not in forms.setdefault(span.place_id, []):
            forms[span.place_id].append(words)
    spans = [s for s in alias_spans(hypothesis, table) if s.place_id in forms]
    choices: list[list[tuple[str, ...] | None]] = [[None, *forms[s.place_id]] for s in spans]
    count = 1
    for c in choices:
        count *= len(c)
    picks: list[tuple[tuple[str, ...] | None, ...]]
    if count > MAX_READINGS:
        picks = [tuple(None for _ in spans), tuple(forms[s.place_id][0] for s in spans)]
    else:
        picks = list(itertools.product(*choices))
    readings = []
    for pick in picks:
        out: list[str] = []
        i = 0
        for span, words in zip(spans, pick, strict=True):
            out.extend(hypothesis[i : span.start])
            out.extend(hypothesis[span.start : span.end] if words is None else words)
            i = span.end
        out.extend(hypothesis[i:])
        readings.append(out)
    return readings


# ---------------------------------------------------------------- alignment


@dataclass(frozen=True)
class Errors:
    """The edit counts of one alignment."""

    substitutions: int
    deletions: int
    insertions: int
    reference_words: int

    @property
    def edits(self) -> int:
        """S + D + I."""
        return self.substitutions + self.deletions + self.insertions

    @property
    def wer(self) -> float | None:
        """(S + D + I) / N, or None when the reference has no words."""
        return self.edits / self.reference_words if self.reference_words else None


def align(reference: Sequence[str], hypothesis: Sequence[str]) -> Errors:
    """A minimal edit-distance alignment of two word lists (unit costs), with its S, D and I.
    Among minimal alignments, a substitution is preferred to a deletion, and a deletion to an
    insertion, so the split is deterministic."""
    n, m = len(reference), len(hypothesis)
    # cost[i][j]: edits to turn reference[:i] into hypothesis[:j]
    cost = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = i
    for j in range(1, m + 1):
        cost[0][j] = j
    for i in range(1, n + 1):
        ri, row, prev = reference[i - 1], cost[i], cost[i - 1]
        for j in range(1, m + 1):
            sub = prev[j - 1] + (ri != hypothesis[j - 1])
            row[j] = min(sub, prev[j] + 1, row[j - 1] + 1)
    s = d = ins = 0
    i, j = n, m
    while i or j:
        if i and j and cost[i][j] == cost[i - 1][j - 1] + (reference[i - 1] != hypothesis[j - 1]):
            s += reference[i - 1] != hypothesis[j - 1]
            i, j = i - 1, j - 1
        elif i and cost[i][j] == cost[i - 1][j] + 1:
            d += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    return Errors(s, d, ins, n)


def raw_errors(reference: str, hypothesis: str) -> Errors:
    """Raw WER counts (module doc)."""
    return align(raw_tokens(reference), raw_tokens(hypothesis))


def normalised_errors(reference: str, hypothesis: str, aliases: AliasTable | None = None) -> Errors:
    """Normalised WER counts (module doc): steps 1-4 on both sides, then the place reading of
    the hypothesis with the fewest edits (the first such, so the result is deterministic)."""
    ref, hyp = normalise(reference), normalise(hypothesis)
    best: Errors | None = None
    for reading in place_readings(ref, hyp, aliases):
        e = align(ref, reading)
        if best is None or e.edits < best.edits:
            best = e
    assert best is not None  # place_readings() always gives the reading as said
    return best


def corpus_wer(errors: Iterable[Errors]) -> float | None:
    """Σ(S + D + I) / ΣN, or None when there are no reference words."""
    edits = words = 0
    for e in errors:
        edits += e.edits
        words += e.reference_words
    return edits / words if words else None
