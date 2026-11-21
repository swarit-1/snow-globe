"""Check scripted.tsv and compare snow-globe-core's parser with the PRD reading (PRD §7.6, §10.6).

Run from the repository root (stdlib only; no network, no audio):

    python3 -I -B bench/utterances/check.py               # validate, rerun the parser, compare
    python3 -I -B bench/utterances/check.py --write       # also rewrite the parser columns
    python3 -I -B bench/utterances/check.py --no-parser   # the TSV and its stored verdicts only
    python3 -I -B bench/utterances/check.py --no-parser --write  # rescore stored parser output
    python3 -I -B bench/utterances/check.py --reading-list  # the lines as the owner says them

Exit status: 0 when everything holds; 1 when the TSV is unsound, the parser's output drifted, or a
stored `agreement`/`difference` is not what compare() gives (rerun with --write); 2 when the
binary is missing or older than what it is built from.

It needs the `snow-globe-parse` binary from app/core, built with the target dir outside iCloud:

    cd app && CARGO_TARGET_DIR=$HOME/lookout-data/cache/cargo-target \
        cargo build --release --offline -p snow-globe-core --bin snow-globe-parse

(`$HOME/lookout-data` is the data dir until the rename's live switch, and a symlink to
`~/snow-globe-data` after it, so the command is right both before and after; D-080.)

`--bin`, `$SNOW_GLOBE_PARSE` (or the deprecated `$LOOKOUT_PARSE`) or
`$CARGO_TARGET_DIR/release/snow-globe-parse` (default `<data dir>/cache/cargo-target/release/
snow-globe-parse`, the data dir resolved as the workers do: `$SNOW_GLOBE_DATA_DIR`, else
`$LOOKOUT_DATA_DIR`, else `~/snow-globe-data` or, until it is moved, `~/lookout-data`; D-080)
names it. A binary older than any of its
build inputs (BUILD_INPUTS: app/core's sources, the Cargo files, the manifests it embeds) is
refused, because it would vouch for code it was not built from; `--allow-stale-binary` overrides.

Rows tagged `owner-pending` expect an outcome that no stated rule gives (bench/README.md); the
summary lists them apart.

**Columns of scripted.tsv:**

| column | meaning |
|---|---|
| `id`, `split` | `d01`-`d30` (dev), `t001`-`t100` (test); recorded in this order |
| `text` | what the owner reads, word for word |
| `tags` | category tags (TAGS below) |
| `clock`, `context` | the reference clock (clocks.tsv); for a follow-up, the last place asked |
| `expected_*` | the PRD reading (prd_reading.py): outcome, viewpoint, rule, valid time, chips |
| `tolerance_s` | 60 for solar times, else 0 |
| `prd_note` | where the PRD is silent or the reading needs a word |
| `parser_*` | what `snow-globe-parse` printed for `text` at `clock` (and `context`) |
| `agreement` | `yes`; `soft` (only a `rolled_*` flag differs, or a miss meets a fallback); `no` |
| `difference` | what differs |

The expected viewpoint is a manifest id, `no-match`, or `a\\|b` for a close call (either one).

Chips: `ampm:<chosen>><alternative>` (the AM/PM chip), `place:<alternative>` or `place:*` (any
place close call), `viewpoint:<alternative>`, and the flags `live`, `dst_gap`, `dst_fold`,
`gfs_horizon`, `rolled_to_tomorrow`, `rolled_to_next_hour`, `rolled_to_next_week`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prd_reading as pr

#: Every tag the script may use (bench/README.md explains them).
TAGS = frozenset(
    [
        "alias",
        "phonetic",
        "spelling",
        "non-alias",
        "carrier",
        "follow-up",
        "from-viewpoint",
        "close-call",
        "generic-word",
        "no-match",
        "adversarial",
        "unknown-viewpoint",
        "no-place",
        "fallback",
        "holiday",
        "extra-word",
        "bare-hour",
        "explicit-hour",
        "day-plus-bare-hour",
        "day-plus-explicit-hour",
        "ampm",
        "boundary",
        "number-word",
        "this-morning",
        "rolled",
        "now",
        "no-time",
        "relative",
        "noon",
        "midnight",
        "tomorrow",
        "today",
        "weekday",
        "weekday-wrap",
        "weekday-today",
        "next-weekday",
        "date",
        "day-only",
        "sunrise",
        "sunset",
        "dawn",
        "dusk",
        "golden-hour",
        "blue-hour",
        "morning-event",
        "solar-passed",
        "solar-based",
        "part-of-day",
        "tonight",
        "tonight-passed",
        "summer",
        "dst-fold",
        "dst-gap",
        "dst-fall",
        "dst-spring",
        "in-repeat",
        "offset-change",
        "cross-zone",
        "place-date",
        "phoenix",
        "mountain",
        "central",
        "eastern",
        "horizon-hrrr",
        "horizon-gfs",
        "out-of-range",
        "prd-example",
        "back-reference",
        "owner-pending",
    ]
)
PENDING = "owner-pending"

OUTCOMES = frozenset({"query", "miss", "fallback", "out-of-range-past", "out-of-range-beyond"})
HARD_FLAGS = frozenset({"live", "dst_gap", "dst_fold", "gfs_horizon"})
SOFT_FLAGS = frozenset({"rolled_to_tomorrow", "rolled_to_next_hour", "rolled_to_next_week"})
ERROR_OUTCOME = {
    "no_match": "miss",
    "unknown_viewpoint": "miss",
    "no_place": "miss",
    "unrecognised": "fallback",
    "invalid_date": "invalid-date",
    "no_solar_event": "no-solar-event",
}
CHIP_PREFIXES = ("ampm:", "place:", "viewpoint:")

#: What the snow-globe-parse binary is built from: (folder, glob). app/core/build.rs embeds the
#: manifests, so a manifest edit needs a rebuild too.
BUILD_INPUTS = (
    ("app/core/src", "**/*.rs"),
    ("app/core", "build.rs"),
    ("app/core", "Cargo.toml"),
    ("app", "Cargo.toml"),
    ("app", "Cargo.lock"),
    ("viewpoints/manifests", "*.json"),
)


def known_chip(chip: str) -> bool:
    """A chip compare() knows how to judge: an AM/PM, place or viewpoint chip, or a known flag."""
    return chip.startswith(CHIP_PREFIXES) or chip in HARD_FLAGS or chip in SOFT_FLAGS


def tags_of(row: dict[str, str]) -> set[str]:
    """The row's tags as a set."""
    return {t for t in row["tags"].split(",") if t}


# ---------------------------------------------------------------- numbers as the owner says them

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
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}
_ORDINALS = {
    1: "first",
    2: "second",
    3: "third",
    5: "fifth",
    8: "eighth",
    9: "ninth",
    12: "twelfth",
    20: "twentieth",
    30: "thirtieth",
}


def cardinal(n: int) -> str:
    """0-59 in words ("twenty-four")."""
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")


def ordinal(n: int) -> str:
    """1-31 as an ordinal in words ("twenty-second")."""
    if n in _ORDINALS:
        return _ORDINALS[n]
    if n < 20:
        return _ONES[n] + "th"
    tens, ones = divmod(n, 10)
    return f"{_TENS[tens]}-{ordinal(ones)}"


def spoken(text: str) -> str:
    """`text` with its numbers written as the owner reads them aloud: "4" as "four", "1:30" as
    "one thirty", "2:05" as "two oh five", "21st" as "twenty-first", "am" as "a.m." (bench/README.md
    "Recording"). The WER normaliser is to use the same forms (TODO(M1), the scorer)."""

    def clock(m: re.Match[str]) -> str:
        h, mm = int(m[1]), int(m[2])
        if mm == 0:
            return cardinal(h)
        return f"{cardinal(h)} {'oh ' if mm < 10 else ''}{cardinal(mm)}"

    out = re.sub(r"\b(\d{1,2}):(\d{2})\b", clock, text)
    out = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", lambda m: ordinal(int(m[1])), out)
    out = re.sub(r"\b(\d{1,2})\b", lambda m: cardinal(int(m[1])), out)
    return re.sub(r"\b([ap])m\b", r"\1.m.", out)


# ---------------------------------------------------------------- validation


def validate(rows: list[dict[str, str]], clocks: dict[str, str]) -> list[str]:
    """Structural problems in the script (an empty list when it is sound)."""
    errors: list[str] = []
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        errors.append("ids are not unique")
    dev = [r for r in rows if r["split"] == "dev"]
    test = [r for r in rows if r["split"] == "test"]
    if (len(dev), len(test)) != (30, 100):
        errors.append(f"splits are {len(dev)} dev and {len(test)} test, not 30 and 100 (PRD §10.6)")
    expected_ids = [f"d{i:02d}" for i in range(1, 31)] + [f"t{i:03d}" for i in range(1, 101)]
    if ids != expected_ids:
        errors.append("ids are not d01-d30 then t001-t100 in order")
    texts = [r["text"].strip().lower() for r in rows]
    dupes = sorted({t for t in texts if texts.count(t) > 1})
    if dupes:
        errors.append(f"duplicate texts: {dupes}")
    for r in rows:
        rid = r["id"]
        if r["split"] not in ("dev", "test"):
            errors.append(f"{rid}: split {r['split']!r}")
        if not r["text"].strip():
            errors.append(f"{rid}: empty text")
        bad = [t for t in r["tags"].split(",") if t not in TAGS]
        if bad:
            errors.append(f"{rid}: unknown tags {bad}")
        if r["clock"] not in clocks:
            errors.append(f"{rid}: unknown clock {r['clock']!r}")
        if r["context"] and r["context"] not in pr.PLACE_TO_VIEWPOINT:
            errors.append(f"{rid}: unknown context place {r['context']!r}")
        if r["expected_outcome"] not in OUTCOMES:
            errors.append(f"{rid}: expected_outcome {r['expected_outcome']!r}")
        vps = r["expected_viewpoint_id"].split("|")
        if r["expected_outcome"] == "miss":
            if r["expected_viewpoint_id"] != "no-match":
                errors.append(f"{rid}: a miss must expect no-match")
        elif any(v not in pr.SITES for v in vps):
            errors.append(f"{rid}: unknown viewpoint {r['expected_viewpoint_id']!r}")
        if r["expected_outcome"] in ("query", "out-of-range-past", "out-of-range-beyond"):
            if not (r["expected_rule"] and r["expected_local"] and r["expected_utc"]):
                errors.append(f"{rid}: a resolved row needs a rule and both times")
            else:
                local = datetime.fromisoformat(r["expected_local"])
                utc = datetime.strptime(r["expected_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                if local != utc:
                    errors.append(f"{rid}: expected_local and expected_utc are different instants")
        if r["tolerance_s"] not in ("0", "60"):
            errors.append(f"{rid}: tolerance_s {r['tolerance_s']!r}")
        unknown = sorted(c for c in r["expected_chips"].split(";") if c and not known_chip(c))
        if unknown:
            errors.append(f"{rid}: unknown expected chips {unknown}")
        if re.search(r"\d", spoken(r["text"])):
            errors.append(f"{rid}: a number spoken() cannot spell: {r['text']!r}")
        if ("follow-up" in tags_of(r)) != bool(r["context"]):
            errors.append(f"{rid}: a follow-up tag needs a context, and a context a follow-up tag")
    errors += follow_up_errors(rows)
    return errors


def follow_up_errors(rows: list[dict[str, str]]) -> list[str]:
    """A follow-up (a row with a context) must come straight after a row that resolves to its
    context place, so the app's own "last place asked about" (PRD §7.4) is the one the row expects
    when the script is replayed in order (bench/README.md "Scoring"). That row must be a plain
    query: not a miss, a fallback, out of range, a close call or owner-pending."""
    errors = []
    for prev, r in zip([None, *rows[:-1]], rows, strict=True):
        if not r["context"]:
            continue
        rid = r["id"]
        if prev is None:
            errors.append(f"{rid}: a follow-up cannot be the first row")
            continue
        vp = prev["expected_viewpoint_id"]
        if (
            prev["expected_outcome"] != "query"
            or "|" in vp
            or PENDING in tags_of(prev)
            or vp not in pr.SITES
        ):
            errors.append(
                f"{rid}: the row before ({prev['id']}) must be a plain query naming {r['context']}"
            )
        elif pr.SITES[vp].place_id != r["context"]:
            errors.append(
                f"{rid}: context {r['context']}, but the row before ({prev['id']}) resolves to "
                f"{pr.SITES[vp].place_id}"
            )
    return errors


def coverage(rows: list[dict[str, str]]) -> dict[str, object]:
    """What the script covers: viewpoints, manifest aliases, rules and tags (counted)."""

    def norm(s: str) -> str:
        return " " + " ".join("".join(c if c.isalnum() else " " for c in s.lower()).split()) + " "

    texts = [norm(r["text"]) for r in rows]
    aliases = {a for s in pr.SITES.values() for a in s.aliases}
    missing_aliases = sorted(a for a in aliases if not any(norm(a) in t for t in texts))
    vp_rows: dict[str, int] = {v: 0 for v in pr.SITES}
    for r in rows:
        for v in r["expected_viewpoint_id"].split("|"):
            if v in vp_rows:
                vp_rows[v] += 1
    rules: dict[str, int] = {}
    tags: dict[str, int] = {}
    for r in rows:
        if r["expected_rule"]:
            rules[r["expected_rule"]] = rules.get(r["expected_rule"], 0) + 1
        for t in r["tags"].split(","):
            tags[t] = tags.get(t, 0) + 1
    return {
        "aliases": len(aliases),
        "aliases_missing": missing_aliases,
        "rows_per_viewpoint": vp_rows,
        "rules": dict(sorted(rules.items())),
        "tags": dict(sorted(tags.items())),
    }


# ---------------------------------------------------------------- the parser


def env(name: str) -> str | None:
    """``SNOW_GLOBE_<X>``, else its deprecated ``LOOKOUT_<X>`` form, else None (D-080).

    The workers' rule (snow_globe_common.env), which this standard-library script cannot import:
    a blank value counts as unset.
    """
    for candidate in (name, "LOOKOUT_" + name.removeprefix("SNOW_GLOBE_")):
        value = os.environ.get(candidate)
        if value is not None and value.strip():
            return value
    return None


def data_dir() -> Path:
    """The data dir in the workers' order (D-080): ``$SNOW_GLOBE_DATA_DIR``, else
    ``$LOOKOUT_DATA_DIR``, else ``~/snow-globe-data`` if it exists, else ``~/lookout-data`` if it
    exists, else ``~/snow-globe-data``. Like the workers, it refuses (``SystemExit``) while both
    folders exist and are different folders: something created ``~/snow-globe-data`` before
    the switch moved the data."""
    named = env("SNOW_GLOBE_DATA_DIR")
    if named:
        return Path(named.strip()).expanduser()
    home = Path.home()
    new, old = home / "snow-globe-data", home / "lookout-data"
    if new.exists():
        if old.exists() and not os.path.samefile(new, old):
            raise SystemExit(
                f"check.py: both {new} and {old} exist and are different folders, so the data "
                "dir is ambiguous (D-080); set SNOW_GLOBE_DATA_DIR, CARGO_TARGET_DIR or --bin"
            )
        return new
    return old if old.exists() else new


def find_binary(arg: str | None) -> Path:
    """The snow-globe-parse binary: --bin, $SNOW_GLOBE_PARSE, or the release build in the target
    dir."""
    if arg:
        return Path(arg)
    named = env("SNOW_GLOBE_PARSE")
    if named:
        return Path(named)
    target = os.environ.get("CARGO_TARGET_DIR") or str(data_dir() / "cache" / "cargo-target")
    return Path(target) / "release" / "snow-globe-parse"


def run_parser(binary: Path, row: dict[str, str], clocks: dict[str, str]) -> dict:
    """Run snow-globe-parse on a row's text at its clock; returns the JSON plus the exit code."""
    cmd = [str(binary), row["text"], "--now", clocks[row["clock"]], "--machine-tz", pr.MACHINE_TZ]
    if row["context"]:
        cmd += ["--context", row["context"]]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode not in (0, 2):
        raise RuntimeError(
            f"{row['id']}: snow-globe-parse exited {proc.returncode}: {proc.stderr.strip()}"
        )
    out = json.loads(proc.stdout)
    out["_exit"] = proc.returncode
    return out


def utc_of(local: str) -> str:
    """RFC 3339 with an offset -> UTC with Z."""
    return datetime.fromisoformat(local).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parser_columns(out: dict) -> dict[str, str]:
    """The parser_* columns for one snow-globe-parse result."""
    cols = {
        "parser_outcome": "",
        "parser_viewpoint_id": "",
        "parser_rule": "",
        "parser_local": "",
        "parser_utc": "",
        "parser_chips": "",
    }
    if out["_exit"] == 0:
        chips = []
        for a in out.get("ambiguities", []):
            if a["field"] == "meridiem":
                chips.append(f"ampm:{a['chosen']}>{a['alternative']}")
            else:
                chips.append(f"{a['field']}:{a['alternative']}")
        chips += out.get("flags", [])
        cols.update(
            parser_outcome="query",
            parser_viewpoint_id=out["viewpoint_id"],
            parser_rule=out["time_source"]["rule"],
            parser_local=out["valid_time"],
            parser_utc=out["valid_time_utc"],
            parser_chips=";".join(chips),
        )
        return cols
    kind = out["error"]
    if kind == "out_of_range":
        cols.update(
            parser_outcome=f"out-of-range-{'past' if out['reason'] == 'past' else 'beyond'}",
            parser_viewpoint_id=out["viewpoint_id"],
            parser_local=out["requested"],
            parser_utc=utc_of(out["requested"]),
        )
        return cols
    mapped = ERROR_OUTCOME.get(kind, kind)
    cols["parser_outcome"] = f"{mapped}/{kind}" if mapped != kind else kind
    cols["parser_viewpoint_id"] = "no-match" if mapped == "miss" else out.get("viewpoint_id", "")
    return cols


# ---------------------------------------------------------------- comparison


def _chips(text: str) -> set[str]:
    return {c for c in text.split(";") if c}


def _parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def compare(row: dict[str, str]) -> tuple[str, str]:
    """(agreement, difference) between the expected and parser columns of a row."""
    hard: list[str] = []
    soft: list[str] = []
    exp_out = row["expected_outcome"]
    got_out = row["parser_outcome"].split("/")[0]
    if exp_out != got_out:
        if {exp_out, got_out} == {"miss", "fallback"}:
            soft.append(
                f"outcome: expected {exp_out}, parser {row['parser_outcome']} (both refuse; the "
                f"fallback rate differs)"
            )
        else:
            hard.append(f"outcome: expected {exp_out}, parser {row['parser_outcome']}")
        return _verdict(hard, soft)
    if exp_out in ("miss", "fallback"):
        return _verdict(hard, soft)
    allowed = set(row["expected_viewpoint_id"].split("|"))
    if row["parser_viewpoint_id"] not in allowed:
        hard.append(
            f"viewpoint: expected {row['expected_viewpoint_id']}, parser "
            f"{row['parser_viewpoint_id']}"
        )
    if exp_out == "query" and row["expected_rule"] != row["parser_rule"]:
        hard.append(f"rule: expected {row['expected_rule']}, parser {row['parser_rule']}")
    if row["parser_utc"]:
        delta = (_parse_utc(row["parser_utc"]) - _parse_utc(row["expected_utc"])).total_seconds()
        tol = int(row["tolerance_s"])
        if abs(delta) > tol:
            hard.append(
                f"time: expected {row['expected_local']}, parser {row['parser_local']} "
                f"({delta:+.0f} s)"
            )
        elif row["expected_local"][-6:] != row["parser_local"][-6:]:
            hard.append(
                f"offset: expected {row['expected_local'][-6:]}, parser {row['parser_local'][-6:]}"
            )
    if exp_out == "query":
        exp, got = _chips(row["expected_chips"]), _chips(row["parser_chips"])
        unknown = sorted(c for c in got if not known_chip(c))
        if unknown:  # a chip or flag this checker cannot judge is never a silent "yes"
            hard.append(f"unknown parser chips: {', '.join(unknown)}")
        exp_place = any(c.startswith("place:") for c in exp)
        got_place = any(c.startswith(("place:", "viewpoint:")) for c in got)
        if exp_place != got_place:
            hard.append(
                f"close-call chip: expected {'one' if exp_place else 'none'}, parser "
                f"{'one' if got_place else 'none'}"
            )
        exp_ampm = {c for c in exp if c.startswith("ampm:")}
        got_ampm = {c for c in got if c.startswith("ampm:")}
        if exp_ampm != got_ampm:
            hard.append(
                f"ampm chip: expected {sorted(exp_ampm) or 'none'}, parser "
                f"{sorted(got_ampm) or 'none'}"
            )
        for flags, out in ((HARD_FLAGS, hard), (SOFT_FLAGS, soft)):
            for flag in sorted(flags):
                if (flag in exp) != (flag in got):
                    out.append(
                        f"flag {flag}: expected {_set(flag in exp)}, parser {_set(flag in got)}"
                    )
    return _verdict(hard, soft)


def _set(on: bool) -> str:
    return "set" if on else "unset"


def _verdict(hard: list[str], soft: list[str]) -> tuple[str, str]:
    if hard:
        return "no", "; ".join(hard + soft)
    if soft:
        return "soft", "; ".join(soft)
    return "yes", ""


def stale_verdicts(rows: list[dict[str, str]]) -> list[str]:
    """Ids of rows whose stored `agreement` and `difference` are not what compare() gives for the
    columns beside them (a reading changed, a verdict was edited by hand, or none was written)."""
    return [r["id"] for r in rows if (r["agreement"], r["difference"]) != compare(r)]


def newer_sources(binary: Path) -> list[str]:
    """Build inputs (BUILD_INPUTS) modified after `binary` was written, as repo paths; empty when
    the binary is at least as new as everything it is built from."""
    built = binary.stat().st_mtime
    newer = []
    for folder, pattern in BUILD_INPUTS:
        for path in sorted((pr.REPO / folder).glob(pattern)):
            if path.is_file() and path.stat().st_mtime > built:
                newer.append(str(path.relative_to(pr.REPO)))
    return newer


def summarise(rows: list[dict[str, str]]) -> dict[str, object]:
    """Agreement counts overall and by split, with the owner-pending rows also counted apart."""
    counts = {"yes": 0, "soft": 0, "no": 0}
    by_split = {"dev": dict(counts), "test": dict(counts)}
    settled = {"dev": dict(counts), "test": dict(counts)}
    for r in rows:
        counts[r["agreement"]] += 1
        by_split[r["split"]][r["agreement"]] += 1
        if PENDING not in tags_of(r):
            settled[r["split"]][r["agreement"]] += 1
    return {
        "agreement": counts,
        "by_split": by_split,
        "by_split_without_pending": settled,
        "pending": [
            {"id": r["id"], "agreement": r["agreement"], "text": r["text"]}
            for r in rows
            if PENDING in tags_of(r)
        ],
    }


# ---------------------------------------------------------------- CLI


def load() -> tuple[list[dict[str, str]], dict[str, str]]:
    """scripted.tsv rows and clocks.tsv as {clock: now}."""
    rows = pr.read_tsv(pr.SCRIPT_TSV)
    clocks = {c["clock"]: c["now"] for c in pr.read_tsv(pr.CLOCKS_TSV)}
    return rows, clocks


def script_hash(rows: list[dict[str, str]]) -> str:
    """The first 12 hex digits of SHA-256 over "id<TAB>split<TAB>text<LF>" for every row, in order.

    It names the folder recordings of this script go in (bench/README.md), so a recording is never
    scored against a line it was not read from. Expected and parser columns do not enter it: fixing
    a reading or the parser keeps the recordings valid; changing what is read does not."""
    data = "".join(f"{r['id']}\t{r['split']}\t{r['text']}\n" for r in rows).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:12]


def reading_list(rows: list[dict[str, str]]) -> str:
    """The script as the owner reads it: id, the line, and how to say its numbers if it has any."""
    lines = []
    for r in rows:
        say = spoken(r["text"])
        lines.append(f"{r['id']}\t{r['text']}" + (f"\t(say: {say})" if say != r["text"] else ""))
    return "\n".join(lines)


def _print_summary(summary: dict[str, object], rows: list[dict[str, str]]) -> None:
    by_split, settled = summary["by_split"], summary["by_split_without_pending"]
    counts = summary["agreement"]
    print(
        f"agreement with the PRD reading: {counts['yes']} yes, {counts['soft']} soft, "
        f"{counts['no']} no "
        f"(dev {by_split['dev']}, test {by_split['test']})"
    )
    print(f"without the owner-pending rows: dev {settled['dev']}, test {settled['test']}")
    for r in rows:
        if r["agreement"] != "yes":
            flag = " owner-pending" if PENDING in tags_of(r) else ""
            print(f"  {r['id']} [{r['agreement']}{flag}] {r['text']}: {r['difference']}")
    pending_yes = [p["id"] for p in summary["pending"] if p["agreement"] == "yes"]
    if pending_yes:
        print(f"  owner-pending rows that agree: {', '.join(pending_yes)}")


def main(argv: list[str]) -> int:
    """Validate the script, rerun the parser, and compare (see the module doc)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--write",
        action="store_true",
        help="rewrite the parser and verdict columns of scripted.tsv",
    )
    ap.add_argument(
        "--no-parser", action="store_true", help="validate the TSV and its stored verdicts only"
    )
    ap.add_argument("--bin", help="path to snow-globe-parse")
    ap.add_argument(
        "--allow-stale-binary", action="store_true", help="use a binary older than its sources"
    )
    ap.add_argument(
        "--reading-list", action="store_true", help="print the lines as the owner reads them"
    )
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)

    rows, clocks = load()
    if list(rows[0]) != pr.COLUMNS:
        print("scripted.tsv: columns differ from prd_reading.COLUMNS", file=sys.stderr)
        return 1
    errors = validate(rows, clocks)
    cov = coverage(rows)
    if cov["aliases_missing"]:
        errors.append(f"aliases never said: {cov['aliases_missing']}")
    if errors:
        for e in errors:
            print(f"scripted.tsv: {e}", file=sys.stderr)
        return 1
    if args.reading_list:
        print(reading_list(rows))
        return 0
    summary: dict[str, object] = {
        "rows": len(rows),
        "script_hash": script_hash(rows),
        "coverage": cov,
    }

    if args.no_parser:
        stale = stale_verdicts(rows)
        if args.write:
            unrun = [r["id"] for r in rows if not r["parser_outcome"]]
            if unrun:
                print(
                    f"no parser output to rescore for {', '.join(unrun)}: run check.py --write",
                    file=sys.stderr,
                )
                return 1
            for r in rows:
                r["agreement"], r["difference"] = compare(r)
            pr.write_tsv(pr.SCRIPT_TSV, rows, pr.COLUMNS)
            stale = []
        summary.update(stale_verdicts=stale)
        if not stale:
            summary.update(summarise(rows))
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print(
                f"scripted.tsv: {len(rows)} rows valid; every one of {cov['aliases']} aliases "
                f"said; "
                f"script hash {summary['script_hash']}"
            )
        if stale:
            print(
                f"stored agreement/difference out of date for {', '.join(stale)} (rerun with "
                f"--write)",
                file=sys.stderr,
            )
            return 1
        return 0

    binary = find_binary(args.bin)
    if not binary.exists():
        print(
            f"snow-globe-parse not found at {binary}; build it (see this file's doc) or pass --bin",
            file=sys.stderr,
        )
        return 2
    newer = newer_sources(binary)
    if newer and not args.allow_stale_binary:
        print(
            f"snow-globe-parse at {binary} is older than {', '.join(newer)}; rebuild it",
            file=sys.stderr,
        )
        return 2
    stale = stale_verdicts(rows)
    drift = []
    for r in rows:
        fresh = parser_columns(run_parser(binary, r, clocks))
        if any(r[k] != v for k, v in fresh.items()):
            drift.append(r["id"])
        r.update(fresh)
        r["agreement"], r["difference"] = compare(r)
    summary.update(binary=str(binary), drift=drift, stale_verdicts=stale, **summarise(rows))
    summary["disagreements"] = [
        {
            "id": r["id"],
            "agreement": r["agreement"],
            "text": r["text"],
            "difference": r["difference"],
        }
        for r in rows
        if r["agreement"] != "yes"
    ]
    if args.write:
        pr.write_tsv(pr.SCRIPT_TSV, rows, pr.COLUMNS)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"snow-globe-parse: {binary}" + (" (older than its sources)" if newer else ""))
        _print_summary(summary, rows)
        if drift and not args.write:
            print(
                f"parser output changed since scripted.tsv was written: {', '.join(drift)} (rerun "
                f"with --write)"
            )
        if stale and not args.write:
            print(
                f"stored agreement/difference were out of date for {', '.join(stale)} (rerun with "
                f"--write)"
            )
    return 1 if ((drift or stale) and not args.write) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
