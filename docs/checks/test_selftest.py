"""Self-tests for doccheck.py: each check passes a small consistent set of docs and catches a
deliberate break of its invariant (a mutation), so no check can pass vacuously.

Most docs here are synthetic, built in memory. `GitFixtures` builds throwaway git repos in a
temporary directory for what only real git and a real disk show: merges in DECISIONS.md's history,
letter case and git-ignored link targets. `RealFilesAreRead` checks that the lock-file and Appendix A
readers find something in the repo's real files, with mutations picked from those files (in
memory), so a format they misread can't let `notices` or `appendix-a` pass vacuously. Standard
library only, plus the git CLI.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import posixpath
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import doccheck as d

TODAY = dt.date(2026, 11, 19)

PLAN = """# PLAN.md

Go-ahead day is **G = Thu 19 Nov 2026**.
Decisions are in [DECISIONS.md](DECISIONS.md#d-001--first--decided--2026-11-18).

| Milestone | Offsets | Dates | Length | 25 % slack |
|---|---|---|---|---|
| M0 Scout | before G | done 19 Nov | — | — |
| M1 Build | G → G+4 | Thu 19 Nov → Mon 23 Nov | 4 days | 1 day |
| M2 Test | G+4 → G+18 | Mon 23 Nov → Mon 7 Dec | 14 days | 3½ days |
| M3 Ship | G+18 → G+48 | Mon 7 Dec → Wed 6 Jan 2027 | 30 days | 7½ days |

## M1: Build · Thu 19 → Mon 23 Nov · in progress

Ticket M1-01 starts it (D-002, PRD §16). With go-ahead a day later it would be about G+24.

## M2: Test · Mon 23 Nov → Mon 7 Dec · not started

| Date | What |
|---|---|
| Mon 14 Dec 2026 (G+25) | Freeze; the pipe in `a \\| b` is escaped |
"""

DECISIONS_V0 = """# DECISIONS.md

Entries are append-only.

**Status:** **decided** (evidence exists) · **proposed** (awaiting the owner) · **open** (to measure).

## D-001 — First · decided · 2026-11-18
- The first decision.

## Spend ledger

| Date | Vendor | What | Amount | Running total |
|---|---|---|---|---|
| 2026-11-18 | — | nothing | $0.00 | $0.00 |
"""

DECISIONS = (
    DECISIONS_V0.replace(
        "## Spend ledger",
        "## D-002 — Second · proposed (owner) · 2026-11-19 · supersedes D-001\n- Points back to D-001.\n\n"
        "## Spend ledger",
    )
    + "| 2026-11-19 | fal | kill test | $0.50 | $0.50 |\n"
)

PROGRESS = """# PRD_PROGRESS.md

**Updated:** Thu 19 Nov 2026 (day G).

**Status values:**
- **done:** its evidence exists.
- **in progress:** being built.
- **not started.**

### M1: Build (Thu 19 → Mon 23 Nov) · in progress

| Ticket | Status | Evidence |
|---|---|---|
| M1-01 Do it | in progress: half done | `rainier.paradise-mountain` |

### M2: Test (Mon 23 Nov → Mon 7 Dec) · not started

| Ticket | Status | Evidence |
|---|---|---|
| M2-01 Test it | not started | [plan](PLAN.md#m2-test--mon-23-nov--mon-7-dec--not-started) |
"""

PRD = """# PRD

## 1. Decisions to confirm

## 16. Milestones

| Milestone | Builds |
|---|---|
| **M1 Build** (G → G+4) | things |
| **M2 Test** (G+4 → G+18) | more, then M3 (G+18 → G+48) |

### 16.1 Order of work
"""

CLAUDE = """# CLAUDE.md

The owner accepted on 2026-11-19 (day G). Spend so far: $0.50.

| `viewpoint_id` | Camera |
|---|---|
| `rainier.paradise-mountain` | park camera |
| `point-reyes.beach` | air camera |
"""

# The `notices` fixtures: one uv project ("demo") with a path dependency's extra, a Windows-only
# marker, an "or" marker, a root extra, a one-line dependency group and a development group with a
# Windows-only member; one Cargo path crate; one npm manifest with an optional dependency; and the
# notices that cover exactly what they require.
UV_LOCK = """version = 1
revision = 3
requires-python = "==3.13.*"

[[package]]
name = "alpha"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "beta" },
    { name = "winonly", marker = "sys_platform == 'win32'" },
]

[[package]]
name = "beta"
version = "0.4.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "boto"
version = "9.9.9"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "demo"
version = "0.1.0"
source = { editable = "." }
dependencies = [
    { name = "alpha" },
    { name = "eitheros", marker = "sys_platform == 'win32' or sys_platform == 'darwin'" },
    { name = "localdep", extra = ["s3"] },
]

[package.optional-dependencies]
plot = [
    { name = "plotlib" },
]

[package.dev-dependencies]
dev = [
    { name = "tester" },
    { name = "wintool", marker = "sys_platform == 'win32'" },
]
forecast = [{ name = "grouped" }]

[package.metadata]
requires-dist = [{ name = "alpha", specifier = ">=1" }]

[[package]]
name = "eitheros"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "grouped"
version = "3.1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "groupeddep" },
]

[[package]]
name = "groupeddep"
version = "0.0.1"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "localdep"
version = "0.1.0"
source = { editable = "../localdep" }

[package.optional-dependencies]
s3 = [
    { name = "boto" },
]

[[package]]
name = "plotlib"
version = "2.2.2"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "tester"
version = "7.0.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "testerdep" },
]

[[package]]
name = "testerdep"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "winonly"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "wintool"
version = "0.9.0"
source = { registry = "https://pypi.org/simple" }
"""

CARGO_LOCK = """version = 4

[[package]]
name = "fastrand"
version = "2.3.0"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "mycrate"
version = "0.1.0"
dependencies = [
 "myutil",
 "serde",
 "tempfile 3.27.0",
]

[[package]]
name = "myutil"
version = "0.1.0"
dependencies = [
 "serde",
]

[[package]]
name = "serde"
version = "1.0.229"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "tempfile"
version = "3.27.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
dependencies = [
 "fastrand",
]
"""

PACKAGE_JSON = json.dumps(
    {
        "name": "demo",
        "dependencies": {"pngjs": "7.0.0"},
        "devDependencies": {"@types/pngjs": "6.0.5", "vitest": "5.1.0"},
        "optionalDependencies": {"fsevents": "2.3.3"},
    },
    indent=2,
)

NOTICES = """Third-party notices (fixture)

Python (workers/common/uv.lock)
    alpha        1.2.3    MIT
    beta         0.4.0    MIT
    boto         9.9.9    Apache-2.0 (the s3 extra)
    eitheros     1.0.0    BSD-3-Clause
    plotlib      2.2.2    MIT (the plot extra)
    grouped      3.1.0    MIT
    groupeddep   0.0.1    MIT
    Development only: tester 7.0.0 (MIT).
Rust (app/Cargo.lock)
    serde        1.0.229  MIT OR Apache-2.0
    tempfile     3.27.0   MIT OR Apache-2.0
Node (renderer/package.json)
    pngjs        7.0.0    MIT
    @types/pngjs 6.0.5    MIT
    vitest       5.1.0    MIT
    fsevents     2.3.3    MIT (optional)
"""

# The `appendix-a` fixtures: a small manifest schema, and an example of its shape.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "camera", "landmarks"],
    "properties": {
        "id": {"type": "string"},
        "camera": {"$ref": "#/$defs/camera"},
        "plate": {"type": ["object", "null"], "properties": {"base": {"type": "string"}, "landmarks": False}},
        "landmarks": {"type": "array", "items": {"$ref": "#/$defs/landmark"}},
        "pose": {
            "oneOf": [
                {"type": "null"},
                {"type": "object", "additionalProperties": False, "properties": {"fov_deg": {"type": "number"}}},
            ]
        },
    },
    "$defs": {
        "camera": {
            "type": "object",
            "additionalProperties": False,
            "required": ["lat"],
            "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
        },
        "landmark": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {"name": {"type": "string"}, "px": {"type": ["array", "null"]}},
        },
    },
}

EXAMPLE: dict[str, Any] = {
    "id": "x.y",
    "camera": {"lat": 0.0, "lon": 0.0},
    "plate": {"base": "b.jpg"},
    "landmarks": [{"name": "", "px": [0, 0]}],
    "pose": {"fov_deg": 0.0},
}

APPENDIX_A = "\n## Appendix A: Viewpoint manifest\n\n```json\n" + json.dumps(EXAMPLE, indent=2) + "\n```\n"

# The `ci-suites` fixtures: a Makefile whose TEST_SUITES holds a matrix suite, a suite with a job of
# its own and a laptop-only suite, and a workflow that runs exactly the first two. Step names and
# shell comments that mention other suites must not count.
MAKEFILE = """# Fixture
TEST_SUITES ?= common docs \\
\tspeech
LAPTOP_ONLY_SUITES := speech  # macOS only

test-common: ## common
\t@echo common
test-docs: docs-check ## docs
docs-check:
\t@echo docs
test-speech:
\t@echo speech
"""

CI_YML = """name: ci
jobs:
  python:
    strategy:
      matrix:
        include:
          - suite: common  # the matrix
            lock: workers/common/uv.lock
    steps:
      - name: make test-${{ matrix.suite }} and make test-speech (a name, not a command)
        run: make test-${{ matrix.suite }}
  docs:
    steps:
      - run: |
          python3 --version
          # make test-speech (a comment)
          make --no-print-directory -C . PYTHON=python3 test-docs
      - name: after
        run: echo done
"""


def base_files() -> dict[str, str]:
    return {
        "PLAN.md": PLAN,
        "DECISIONS.md": DECISIONS,
        "PRD_PROGRESS.md": PROGRESS,
        "PRD.md": PRD + APPENDIX_A,
        "CLAUDE.md": CLAUDE,
        "docs/research/note.md": "See [the plan](../../PLAN.md) and "
        "[M1](../../PLAN.md#m1-build--thu-19--mon-23-nov--in-progress).\n"
        "```\n[not a link](nowhere.md) Fri 19 Nov\n```\n",
        "workers/x/src/mod.py": '"""Implements PRD §16.1 and D-002; ticket M2-01."""\n',
        "viewpoints/manifests/rainier.paradise-mountain.json": "{}\n",
        "viewpoints/manifests/point-reyes.beach.json": "{}\n",
        "viewpoints/schema/viewpoint.schema.json": json.dumps(SCHEMA),
        "workers/common/uv.lock": UV_LOCK,
        "app/Cargo.lock": CARGO_LOCK,
        "renderer/package.json": PACKAGE_JSON,
        "THIRD_PARTY_NOTICES": NOTICES,
        "Makefile": MAKEFILE,
        ".github/workflows/ci.yml": CI_YML,
    }


def problems(check: str, files: dict[str, str], history: Sequence[str] | None = None) -> list[d.Problem]:
    repo = d.Repo.memory(files, history if history is not None else [DECISIONS_V0, files["DECISIONS.md"]])
    if check == "decisions":
        return d.check_decisions(repo, today=TODAY)
    return d.CHECKS[check](repo)


def mutate(files: dict[str, str], path: str, old: str, new: str) -> dict[str, str]:
    """A copy of `files` with the first `old` in `path` replaced by `new`; `old` must be there."""
    if old not in files[path]:
        raise AssertionError(f"mutation anchor {old!r} not in {path}")
    out = dict(files)
    out[path] = files[path].replace(old, new, 1)
    return out


class BaselinePasses(unittest.TestCase):
    def test_every_check_passes_the_consistent_docs(self) -> None:
        files = base_files()
        for check in d.CHECKS:
            with self.subTest(check=check):
                self.assertEqual(problems(check, files), [], check)


class MutationsAreCaught(unittest.TestCase):
    """Each case breaks one invariant; the named check must report it."""

    CASES = (
        # (check, path, old, new)
        ("links", "PLAN.md", "[DECISIONS.md](DECISIONS.md#", "[DECISIONS.md](DECISION.md#"),
        ("links", "PLAN.md", "(DECISIONS.md#d-001--first--decided--2026-11-18)", "(DECISIONS.md#d-001-first)"),
        ("links", "docs/research/note.md", "(../../PLAN.md)", "(../../../PLAN.md)"),
        ("links", "PRD_PROGRESS.md", "(PLAN.md#m2-test", "(PLAN.md#m2-tests"),
        ("tables", "PLAN.md", "| M1 Build | G → G+4 |", "| M1 Build | G → G+4 | extra |"),
        ("tables", "PLAN.md", "`a \\| b`", "`a | b`"),
        ("weekdays", "PLAN.md", "Mon 23 Nov → Mon 7 Dec | 14", "Mon 23 Nov → Tue 7 Dec | 14"),
        ("weekdays", "PRD_PROGRESS.md", "**Updated:** Thu 19 Nov 2026", "**Updated:** Thu 31 Nov 2026"),
        ("weekdays", "PLAN.md", "Mon 7 Dec → Wed 6 Jan 2027", "Mon 7 Dec → Wed 6 Jan 2028"),
        ("day-g", "CLAUDE.md", "2026-11-19 (day G)", "2026-11-20 (day G)"),
        ("day-g", "PLAN.md", "Mon 14 Dec 2026 (G+25)", "Mon 14 Dec 2026 (G+24)"),
        ("day-g", "PRD_PROGRESS.md", "Thu 19 Nov 2026 (day G)", "Fri 20 Nov 2026 (day G)"),
        ("calendar", "PLAN.md", "| 4 days | 1 day |", "| 4 days | 2 days |"),
        ("calendar", "PLAN.md", "| 14 days | 3½ days |", "| 15 days | 3¾ days |"),
        (
            "milestones",
            "PRD_PROGRESS.md",
            "### M2: Test (Mon 23 Nov → Mon 7 Dec)",
            "### M2: Test (Mon 23 Nov → Sun 6 Dec)",
        ),
        ("milestones", "PRD.md", "**M2 Test** (G+4 → G+18)", "**M2 Test** (G+4 → G+19)"),
        ("decisions", "DECISIONS.md", "## D-002 — Second", "## D-003 — Second"),
        ("decisions", "DECISIONS.md", "· proposed (owner) ·", "· maybe ·"),
        ("decisions", "DECISIONS.md", "· proposed (owner) · 2026-11-19", "· proposed (owner) · 2026-11-17"),
        ("decisions", "DECISIONS.md", "· proposed (owner) · 2026-11-19", "· proposed (owner) · 2099-01-01"),
        ("decisions", "DECISIONS.md", "· proposed (owner) · 2026-11-19", "· proposed (owner) · 2026-11-31"),
        ("decisions", "DECISIONS.md", "supersedes D-001", "supersedes D-002"),
        (
            "decisions",
            "DECISIONS.md",
            "## D-002 — Second · proposed (owner) · 2026-11-19 · supersedes D-001",
            "## D-002 - Second",
        ),
        ("decisions", "DECISIONS.md", "**Status:** **decided**", "**State:** **decided**"),
        ("append-only", "DECISIONS.md", "- The first decision.", "- The first decision, reworded."),
        ("append-only", "DECISIONS.md", "Entries are append-only.\n", ""),
        ("d-refs", "PLAN.md", "(D-002, PRD §16)", "(D-012, PRD §16)"),
        ("d-refs", "workers/x/src/mod.py", "D-002", "D-003"),
        ("tickets", "PRD_PROGRESS.md", "| M2-01 Test it |", "| M1-01 Test it |"),
        ("tickets", "PRD_PROGRESS.md", "| M2-01 Test it |", "| M1-02 Test it |"),
        ("tickets", "PRD_PROGRESS.md", "| in progress: half done |", "| waiting |"),
        ("tickets", "PLAN.md", "Ticket M1-01 starts it", "Ticket M1-07 starts it"),
        ("tickets", "PLAN.md", "Ticket M1-01 starts it", "Ticket M4-01 starts it"),
        ("tickets", "PRD_PROGRESS.md", "**Status values:**", "**Statuses:**"),
        ("spend", "DECISIONS.md", "| $0.50 | $0.50 |", "| $0.50 | $0.75 |"),
        ("spend", "CLAUDE.md", "Spend so far: $0.50.", "Spend so far: $0."),
        ("spend", "DECISIONS.md", "| 2026-11-19 | fal |", "| 2026-11-17 | fal |"),
        ("spend", "DECISIONS.md", "## Spend ledger", "## Spending"),
        ("prd-sections", "PLAN.md", "PRD §16)", "PRD §17)"),
        ("prd-sections", "workers/x/src/mod.py", "PRD §16.1", "PRD §16.2"),
        ("prd-sections", "PLAN.md", "PRD §16)", "PRD §1-16.9)"),
        ("prd-sections", "PLAN.md", "PRD §16)", "PRD §16.1–§16.9)"),
        ("prd-sections", "workers/x/src/mod.py", "PRD §16.1", "PRD §16.1-9"),
        ("viewpoints", "PRD_PROGRESS.md", "`rainier.paradise-mountain`", "`rainier.paradise`"),
        ("viewpoints", "CLAUDE.md", "| `point-reyes.beach` | air camera |\n", ""),
        ("notices", "THIRD_PARTY_NOTICES", "    beta         0.4.0    MIT\n", ""),
        ("notices", "THIRD_PARTY_NOTICES", "    groupeddep   0.0.1    MIT\n", ""),
        ("notices", "THIRD_PARTY_NOTICES", "    plotlib      2.2.2", "    plotlib      2.2.20"),
        ("notices", "THIRD_PARTY_NOTICES", "    pngjs        7.0.0    MIT\n", ""),
        ("notices", "workers/common/uv.lock", 'name = "beta"\nversion = "0.4.0"', 'name = "beta"\nversion = "0.4.1"'),
        (
            "notices",
            "workers/common/uv.lock",
            '    { name = "tester" },\n',
            '    { name = "tester" },\n    { name = "newtool" },\n',
        ),
        ("notices", "app/Cargo.lock", 'name = "serde"\nversion = "1.0.229"', 'name = "serde"\nversion = "1.0.230"'),
        ("notices", "renderer/package.json", '"vitest": "5.1.0"', '"vitest": "^5.1.0"'),
        ("appendix-a", "PRD.md", '"lat": 0.0', '"latitude": 0.0'),
        ("appendix-a", "PRD.md", '"plate": {', '"plate": {"landmarks": [], '),
        ("appendix-a", "PRD.md", '"landmarks": [', '"landmark": ['),
        (
            "appendix-a",
            "viewpoints/schema/viewpoint.schema.json",
            '"required": ["id", "camera", "landmarks"]',
            '"required": ["id", "camera", "landmarks", "status"]',
        ),
        (
            "appendix-a",
            "viewpoints/schema/viewpoint.schema.json",
            '"camera": {"$ref": "#/$defs/camera"}',
            '"camera": {"$ref": "camera.schema.json"}',
        ),
        ("ci-suites", "Makefile", "TEST_SUITES ?= common docs", "TEST_SUITES ?= common docs bench"),
        ("ci-suites", "Makefile", "LAPTOP_ONLY_SUITES := speech", "LAPTOP_ONLY_SUITES :="),
        ("ci-suites", "Makefile", "test-docs: docs-check", "docs: docs-check"),
        ("ci-suites", ".github/workflows/ci.yml", "PYTHON=python3 test-docs", "PYTHON=python3 docs-check"),
        ("ci-suites", ".github/workflows/ci.yml", "- suite: common", "- suite: commons"),
        ("ci-suites", ".github/workflows/ci.yml", "run: echo done", "run: make test-speech"),
    )

    def test_each_mutation_is_caught(self) -> None:
        for check, path, old, new in self.CASES:
            with self.subTest(check=check, path=path, change=f"{old!r} -> {new!r}"):
                files = mutate(base_files(), path, old, new)
                history = [DECISIONS_V0, base_files()["DECISIONS.md"]]
                found = problems(check, files, history if check == "append-only" else None)
                self.assertTrue(found, f"{check} missed {old!r} -> {new!r}")

    def test_every_check_has_a_mutation(self) -> None:
        self.assertEqual({c for c, *_ in self.CASES}, set(d.CHECKS))


class NotFlagged(unittest.TestCase):
    """Things that look like violations but are not."""

    def assert_clean(self, check: str, path: str, old: str, new: str) -> None:
        files = mutate(base_files(), path, old, new)
        self.assertEqual(problems(check, files), [])

    def test_code_blocks_and_inline_code_are_ignored(self) -> None:
        self.assert_clean("links", "PLAN.md", "## M1: Build", "`[x](missing.md)` and\n\n## M1: Build")
        self.assert_clean("weekdays", "PLAN.md", "## M1: Build", "`Fri 19 Nov`\n\n## M1: Build")

    def test_section_refs_in_history_and_other_documents(self) -> None:
        self.assert_clean("prd-sections", "DECISIONS.md", "- The first decision.", "- PRD v0.3 §3.5 said so.")
        self.assert_clean("prd-sections", "PLAN.md", "PRD §16)", "PRD §16; Oculus `PRD.md` §13.6)")

    def test_approximate_offsets_are_not_paired(self) -> None:
        self.assert_clean("day-g", "PLAN.md", "about G+24.", "about G+24 (Mon 14 Dec).")

    def test_appending_a_decision_is_allowed(self) -> None:
        files = mutate(
            base_files(),
            "DECISIONS.md",
            "## Spend ledger",
            "## D-003 — Third · open · 2026-11-19 · amends D-002\n- More.\n\n## Spend ledger",
        )
        self.assertEqual(problems("append-only", files), [])
        self.assertEqual(problems("decisions", files), [])

    def test_a_later_date_in_a_heading_is_not_the_entry_date(self) -> None:
        files = mutate(base_files(), "DECISIONS.md", "supersedes D-001", "supersedes D-001 (of 2026-11-18)")
        self.assertEqual(problems("decisions", files), [])

    def test_decisions_with_no_date_and_several_statuses(self) -> None:
        files = mutate(
            base_files(),
            "DECISIONS.md",
            "## Spend ledger",
            "## D-003 — Hosting · open (owner input needed)\n- No date.\n\n"
            "## D-004 — Split · Rainier P decided; ARD H open · 2026-11-19\n- Mixed.\n\n## Spend ledger",
        )
        self.assertEqual(problems("decisions", files), [])

    def test_milestone_ranges_are_not_ticket_ids(self) -> None:
        # M1's tickets have two digits (M1-01), and M4 has none yet: both read as ranges.
        self.assert_clean(
            "tickets",
            "PLAN.md",
            "Ticket M1-01 starts it",
            "Ticket M1-01 starts it; the owner reviews during M4-5 and M1-2",
        )

    def test_section_ranges_whose_ends_exist(self) -> None:
        self.assert_clean("prd-sections", "PLAN.md", "PRD §16)", "PRD §1-16.1, §16–§16.1, §16.1-1)")

    def test_year_less_dates_are_read_near_the_docs_updated_date(self) -> None:
        # Mid-2027 dates without a year, in a doc updated in 2027: read as 2027, so they pass.
        later = (
            "# Later\n\n**Updated:** Mon 14 Jun 2027.\n\nFreeze on Mon 14 Jun; reviews on Fri 21 May and Mon 24 May.\n"
        )
        files = dict(base_files(), **{"docs/later.md": later})
        self.assertEqual(problems("weekdays", files), [])
        iso = dict(base_files(), **{"docs/later.md": later.replace("Mon 14 Jun 2027", "2027-06-14")})
        self.assertEqual(problems("weekdays", iso), [])
        # A wrong label is still caught, with a hint about the inferred year.
        wrong = dict(base_files(), **{"docs/later.md": later.replace("on Mon 14 Jun;", "on Tue 14 Jun;")})
        found = problems("weekdays", wrong)
        self.assertEqual(len(found), 1, found)
        self.assertIn("14 Jun 2027 is a Mon", found[0].message)
        self.assertIn("no year is written", found[0].message)

    def test_a_far_g_offset_reads_its_date_near_that_day(self) -> None:
        # G+207 is Mon 14 Jun 2027; without a year the date is read near the day the offset names.
        self.assert_clean("day-g", "PLAN.md", "about G+24.", "about G+24. Retention review Mon 14 Jun (G+207).")


class Helpers(unittest.TestCase):
    def test_labelled_dates_borrow_month_and_year_from_the_next_date(self) -> None:
        got = [(x.weekday, x.date) for x in d.labelled_dates("about Fri 15 → Wed 20 Jan 2027", TODAY)]
        self.assertEqual(got, [("Fri", dt.date(2027, 1, 15)), ("Wed", dt.date(2027, 1, 20))])

    def test_labelled_dates_pick_the_year_nearest_day_g(self) -> None:
        got = [x.date for x in d.labelled_dates("Sun 27 Dec → Wed 6 Jan and Wed 15 Jul", TODAY)]
        self.assertEqual(got, [dt.date(2026, 12, 27), dt.date(2027, 1, 6), dt.date(2026, 7, 15)])

    def test_labelled_dates_skip_numbers_that_are_not_days(self) -> None:
        self.assertEqual(d.labelled_dates("Sun 0.19′ and Sun 3.1° at Sat 12:00 Nov", TODAY), [])

    def test_g_offsets(self) -> None:
        self.assertEqual(d.g_offsets("G → G+4, by G+21, about G+24, before G, `G+9`, GFS, G-offsets"), [0, 4, 21])

    def test_day_amount(self) -> None:
        self.assertEqual(
            [d.day_amount(s) for s in ["3½ days", "¾ day", "~1¼ days", "4 days", "—"]], [3.5, 0.75, 1.25, 4.0, None]
        )

    def test_heading_slug_matches_github(self) -> None:
        self.assertEqual(d.heading_slug("1. Decisions to confirm"), "1-decisions-to-confirm")
        self.assertEqual(
            d.heading_slug("D-001 — Milestone 0 · decided · 2026-11-18"), "d-001--milestone-0--decided--2026-11-18"
        )
        self.assertEqual(d.heading_slug("Hard stop at the `cap`"), "hard-stop-at-the-cap")

    def test_anchors_number_repeated_headings(self) -> None:
        self.assertEqual(d.anchors('# A\n## A\n<a id="Here"></a>\n'), {"a", "a-1", "here"})

    def test_split_cells_respects_escaped_pipes_only(self) -> None:
        self.assertEqual(d.split_cells("| a | `b \\| c` | d |"), ["a", "`b \\| c`", "d"])
        self.assertEqual(len(d.split_cells("| a | `b | c` | d |")), 4)

    def test_declared_statuses(self) -> None:
        self.assertEqual(d.declared_statuses(DECISIONS, "Status"), ["decided", "proposed", "open"])
        self.assertEqual(d.declared_statuses(PROGRESS, "Status values"), ["done", "in progress", "not started"])

    def test_doc_anchor_date(self) -> None:
        cases = [
            ("**Updated:** Thu 19 Nov 2026, 08:30 UTC (day G).", dt.date(2026, 11, 19)),
            ("Updated: 2027-06-14, after the review", dt.date(2027, 6, 14)),
            ("_Last updated:_ Mon 14 Jun 2027", dt.date(2027, 6, 14)),
            ("**Updated:** Mon 14 Jun (no year)", TODAY),
            ("Nothing here was Updated: 2027-06-14", TODAY),
        ]
        for line, want in cases:
            with self.subTest(line=line):
                self.assertEqual(d.doc_anchor_date(f"# Doc\n\n{line}\n", TODAY), want)

    def test_section_range_end(self) -> None:
        self.assertEqual(
            [d.section_range_end(a, b) for a, b in [("13.4", "13.7"), ("13.4", "7"), ("1", "16.9"), ("10.2.1", "3")]],
            ["13.7", "13.7", "16.9", "10.2.3"],
        )

    def test_is_ticket_reference(self) -> None:
        digits = {"M0": {1}, "M1": {2}}
        self.assertEqual(
            [d.is_ticket_reference(t, digits) for t in ["M0-7", "M1-07", "M1-5", "M4-5", "M4-01", "M0.5-1"]],
            [True, True, False, False, True, False],
        )


class Notices(unittest.TestCase):
    """THIRD_PARTY_NOTICES against uv.lock, Cargo.lock and package.json (the `notices` check)."""

    def test_what_a_uv_lock_requires(self) -> None:
        required, errors = d.uv_required(UV_LOCK)
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(required),
            sorted(
                [
                    ("alpha", "1.2.3", "runtime"),
                    ("beta", "0.4.0", "runtime"),
                    ("boto", "9.9.9", "runtime"),  # through localdep[s3], a path dependency
                    ("eitheros", "1.0.0", "runtime"),  # an "or" marker that can hold on macOS
                    ("plotlib", "2.2.2", "extra plot"),
                    ("grouped", "3.1.0", "group forecast"),  # a one-line group array, with its closure
                    ("groupeddep", "0.0.1", "group forecast"),
                    ("tester", "7.0.0", "group dev, direct"),  # its own dependency, testerdep, never ships
                ]
            ),
        )  # never winonly or wintool (Windows only), demo or localdep (the project and a path dependency)

    def test_what_a_cargo_lock_requires(self) -> None:
        required, errors = d.cargo_required(CARGO_LOCK)
        self.assertEqual(errors, [])
        # The direct third-party dependencies of each path crate: never myutil (a path crate
        # itself), and never fastrand, which comes through tempfile.
        self.assertEqual(
            required,
            [
                ("serde", "1.0.229", "crate mycrate"),
                ("serde", "1.0.229", "crate myutil"),
                ("tempfile", "3.27.0", "crate mycrate"),
            ],
        )
        _, errors = d.cargo_required(CARGO_LOCK.replace(' "tempfile 3.27.0",', ' "tempfile 3.28.0",'))
        self.assertEqual(errors, ["mycrate: cannot resolve dependency 'tempfile 3.28.0'"])

    def test_what_a_package_json_requires(self) -> None:
        required, errors = d.npm_required(PACKAGE_JSON)
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(required),
            [
                ("@types/pngjs", "6.0.5", "npm devDependencies"),
                ("fsevents", "2.3.3", "npm optionalDependencies"),
                ("pngjs", "7.0.0", "npm dependencies"),
                ("vitest", "5.1.0", "npm devDependencies"),
            ],
        )
        exact = json.dumps({"dependencies": {"x": "=1.2.3", "y": "v2.0.0-rc.1"}})
        self.assertEqual(
            d.npm_required(exact), ([("x", "1.2.3", "npm dependencies"), ("y", "2.0.0-rc.1", "npm dependencies")], [])
        )
        for spec in ("^5.1.0", "~5.1.0", ">=5", "5.x", "5", "latest", "workspace:*"):
            with self.subTest(spec=spec):
                required, errors = d.npm_required(json.dumps({"devDependencies": {"vitest": spec}}))
                self.assertEqual(required, [])
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("is not an exact version", errors[0])
        self.assertEqual(d.npm_required("[]"), ([], ["not a JSON object"]))
        self.assertIn("not JSON", d.npm_required("{")[1][0])

    def test_marker_applies(self) -> None:
        cases = {
            "": True,
            "sys_platform == 'win32'": False,
            "platform_system == 'Windows'": False,
            "sys_platform == 'linux' and python_full_version >= '3.13'": False,
            "sys_platform != 'win32'": True,
            "sys_platform == 'darwin'": True,
            "implementation_name != 'PyPy'": True,
            "sys_platform == 'win32' or sys_platform == 'darwin'": True,  # misread can only add a notice
        }
        for marker, want in cases.items():
            with self.subTest(marker=marker):
                self.assertEqual(d.marker_applies(marker), want)

    def test_toml_arrays(self) -> None:
        table = (
            'dependencies = [\n    { name = "a", extra = ["s3"] },\n    { name = "b" },\n]\n'
            'one = [{ name = "c" }]\nempty = []\nname = "x"\n'
        )
        arrays = d.toml_arrays(table)
        self.assertEqual(sorted(arrays), ["dependencies", "empty", "one"])
        self.assertEqual(d._uv_entries(arrays["dependencies"]), [("a", None, ("s3",), ""), ("b", None, (), "")])
        self.assertEqual(d._uv_entries(arrays["one"]), [("c", None, (), "")])
        self.assertEqual(arrays["empty"], "")

    def test_named_with_version(self) -> None:
        lines = ["    alpha        1.2.3    MIT", "    @types/pngjs 6.0.5    MIT", "Development only: Tester 7.0.0."]
        self.assertTrue(d.named_with_version(lines, "alpha", "1.2.3"))
        self.assertTrue(d.named_with_version(lines, "tester", "7.0.0"))  # any letter case
        self.assertFalse(d.named_with_version(lines, "alpha", "1.2"))  # a longer version
        self.assertFalse(d.named_with_version(lines, "alph", "1.2.3"))  # a longer name
        self.assertFalse(d.named_with_version(lines, "pngjs", "6.0.5"))  # only @types/pngjs
        self.assertFalse(d.named_with_version(["alpha", "1.2.3"], "alpha", "1.2.3"))  # two lines
        # Only spaces and ':', '@', '=' or '(' may sit between them, so another package's version on
        # the same line never counts (review finding, 20 Nov).
        self.assertFalse(d.named_with_version(["    foo          1.16.0   MIT (pulls in six)"], "six", "1.16.0"))
        self.assertFalse(d.named_with_version(["six, see foo 1.16.0"], "six", "1.16.0"))
        self.assertFalse(d.named_with_version(["alpha 11.2.3"], "alpha", "1.2.3"))
        for line in ("six==1.16.0", "six@1.16.0", "six (1.16.0)", "six: 1.16.0", "Also six 1.16.0."):
            with self.subTest(line=line):
                self.assertTrue(d.named_with_version([line], "six", "1.16.0"))

    def test_cargo_dependencies_are_resolved_by_source_too(self) -> None:
        # Two crates with one name and version, from crates.io and from git: Cargo.lock then names
        # the source in the dependency, and only that crate may match.
        lock = (
            'version = 4\n\n[[package]]\nname = "app"\nversion = "0.1.0"\ndependencies = [\n'
            ' "dup 1.0.0 (git+https://example.invalid/dup?rev=abc#abc)",\n]\n\n'
            '[[package]]\nname = "dup"\nversion = "1.0.0"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n\n'
            '[[package]]\nname = "dup"\nversion = "1.0.0"\nsource = "git+https://example.invalid/dup?rev=abc#abc"\n'
        )
        self.assertEqual(d.cargo_required(lock), ([("dup", "1.0.0", "crate app")], []))
        ambiguous = lock.replace(" (git+https://example.invalid/dup?rev=abc#abc)", "")
        self.assertEqual(d.cargo_required(ambiguous)[1], ["app: cannot resolve dependency 'dup 1.0.0'"])

    def test_a_new_lock_file_is_covered_at_once(self) -> None:
        bench_lock = (
            'version = 1\n\n[[package]]\nname = "bench"\nversion = "0.1.0"\nsource = { virtual = "." }\n'
            'dependencies = [\n    { name = "gamma" },\n]\n\n'
            '[[package]]\nname = "gamma"\nversion = "3.0.0"\nsource = { registry = "https://pypi.org/simple" }\n'
        )
        found = problems("notices", dict(base_files(), **{"bench/uv.lock": bench_lock}))
        self.assertEqual(
            [(p.where, p.message) for p in found],
            [("THIRD_PARTY_NOTICES", "gamma 3.0.0 (bench/uv.lock, runtime) is not named with its version on one line")],
        )

    def test_lock_files_need_the_notices_and_no_lock_file_needs_none(self) -> None:
        found = d.check_notices(d.Repo.memory({"workers/common/uv.lock": UV_LOCK}))
        self.assertEqual(
            [(p.where, p.message) for p in found],
            [("THIRD_PARTY_NOTICES", "missing, and 1 lock file(s) need it: workers/common/uv.lock")],
        )
        self.assertEqual(d.check_notices(d.Repo.memory({"README.md": "# x\n"})), [])

    def test_lock_resolution_errors_are_reported(self) -> None:
        no_root = UV_LOCK.replace('source = { editable = "." }', 'source = { editable = "./demo" }')
        self.assertEqual(d.uv_required(no_root), ([], ['expected one project package (source "."), found 0']))
        ghost = UV_LOCK.replace('    { name = "beta" },\n', '    { name = "beta" },\n    { name = "ghost" },\n', 1)
        self.assertEqual(d.uv_required(ghost)[1], ["cannot resolve ghost"])


def appendix_repo(example: Any = None, schema: Any = None, raw: str | None = None) -> d.Repo:
    text = raw if raw is not None else json.dumps(EXAMPLE if example is None else example, indent=2)
    prd = f"# PRD\n\n## Appendix A: Viewpoint manifest\n\n```json\n{text}\n```\n\n## Appendix B\n"
    return d.Repo.memory({"PRD.md": prd, d.MANIFEST_SCHEMA: json.dumps(SCHEMA if schema is None else schema)})


def appendix_messages(example: Any = None, schema: Any = None) -> list[str]:
    return sorted(p.message for p in d.check_appendix_a(appendix_repo(example, schema)))


def reading_messages(schema: Any) -> list[str]:
    """The problems check_appendix_a reports against the schema itself, not the example."""
    return [p.message for p in d.check_appendix_a(appendix_repo(schema=schema)) if p.where == d.MANIFEST_SCHEMA]


class AppendixA(unittest.TestCase):
    """PRD Appendix A's example against the manifest schema (the `appendix-a` check)."""

    def example(self) -> dict[str, Any]:
        return copy.deepcopy(EXAMPLE)

    def test_the_consistent_example_passes(self) -> None:
        self.assertEqual(appendix_messages(), [])

    def test_an_unknown_key(self) -> None:
        ex = self.example()
        ex["camera"]["heading_deg"] = 0.0
        self.assertEqual(appendix_messages(ex), ["camera.heading_deg: not a property in the schema"])

    def test_a_missing_required_key(self) -> None:
        ex = self.example()
        del ex["camera"]["lat"]
        self.assertEqual(appendix_messages(ex), ["camera.lat: required by the schema, missing"])

    def test_a_forbidden_key_in_an_open_object(self) -> None:
        ex = self.example()
        ex["plate"]["landmarks"] = []
        ex["plate"]["sky_mask"] = "sky.png"  # plate stays open to other keys
        self.assertEqual(appendix_messages(ex), ["plate.landmarks: the schema forbids this key"])

    def test_array_items_and_one_of_are_followed(self) -> None:
        ex = self.example()
        ex["landmarks"][0]["band"] = "summit"
        ex["pose"]["roll_deg"] = 0.0
        self.assertEqual(
            appendix_messages(ex),
            ["landmarks[0].band: not a property in the schema", "pose.roll_deg: not a property in the schema"],
        )

    def test_an_object_or_array_where_the_schema_allows_none(self) -> None:
        ex = self.example()
        ex["id"] = {"nested": True}
        ex["landmarks"][0]["name"] = ["a"]
        self.assertEqual(
            appendix_messages(ex),
            ["id: an object where the schema allows none", "landmarks[0].name: an array where the schema allows none"],
        )

    def test_one_of_with_two_object_branches_takes_the_one_that_fits(self) -> None:
        schema = copy.deepcopy(SCHEMA)
        schema["properties"]["pose"] = {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["fov_deg"],
                    "properties": {"fov_deg": {"type": "number"}},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["matrix"],
                    "properties": {"matrix": {"type": "array"}},
                },
            ]
        }
        ex = self.example()
        ex["pose"] = {"matrix": [1, 0, 0]}
        self.assertEqual(appendix_messages(ex, schema), [])  # the second branch fits
        ex["pose"] = {"matrix": [1], "fov_deg": 1.0}  # neither fits: the closest is reported
        self.assertEqual(appendix_messages(ex, schema), ["pose.matrix: not a property in the schema"])

    def test_all_of_applies_every_branch_and_additional_properties_schemas_are_followed(self) -> None:
        schema = copy.deepcopy(SCHEMA)
        schema["properties"]["plate"] = {
            "allOf": [
                {"type": "object", "required": ["base"]},
                {"type": "object", "additionalProperties": {"type": "string"}},
            ]
        }
        self.assertEqual(appendix_messages(self.example(), schema), [])
        ex = self.example()
        ex["plate"] = {"night": {"file": "n.webp"}}
        self.assertEqual(
            appendix_messages(ex, schema),
            ["plate.base: required by the schema, missing", "plate.night: an object where the schema allows none"],
        )

    def test_a_schema_keyword_the_check_does_not_read_fails_it(self) -> None:
        schema = copy.deepcopy(SCHEMA)
        schema["$defs"]["camera"]["patternProperties"] = {"^x_": {"type": "string"}}
        schema["$defs"]["landmark"]["properties"]["px"]["prefixItems"] = [{"type": "number"}]
        schema["$defs"]["default"] = {"propertyNames": {"pattern": "^[a-z]+$"}}  # a $defs entry, not the keyword
        schema["$defs"]["camera"]["properties"]["prefixItems"] = {"type": "string"}  # a property's name
        schema["$defs"]["landmark"]["properties"]["name"]["enum"] = [{"unevaluatedProperties": 1}]  # a value
        found = d.check_appendix_a(appendix_repo(schema=schema))
        self.assertEqual(
            [(p.where, p.message) for p in found],
            [
                (
                    d.MANIFEST_SCHEMA,
                    f"uses {kw}, which check_appendix_a does not read yet; teach it before trusting this check",
                )
                for kw in ("patternProperties", "prefixItems", "propertyNames")
            ],
        )

    def test_more_keywords_the_check_does_not_read(self) -> None:
        # Review finding (20 Nov): $dynamicRef and dependencies passed unread.
        cases = {
            "$dynamicRef": {"$dynamicRef": "#camera"},
            "$recursiveRef": {"$recursiveRef": "#"},
            "contains": {"type": "array", "contains": {"required": ["name"]}},
            "dependencies": {"dependencies": {"lat": ["lon"]}},
            "items as an array (the tuple form before JSON Schema 2020-12)": {"items": [{"type": "object"}]},
        }
        for what, sub in cases.items():
            with self.subTest(what=what):
                schema = copy.deepcopy(SCHEMA)
                schema["properties"]["camera"] = sub
                self.assertEqual(
                    reading_messages(schema),
                    [f"uses {what}, which check_appendix_a does not read yet; teach it before trusting this check"],
                )

    def test_a_ref_the_check_cannot_follow_fails_it(self) -> None:
        # Review finding (20 Nov): each of these was read as "anything", so the wrong camera passed.
        ex = self.example()
        ex["camera"] = {"anything": {"goes": [1]}}
        for ref in (
            "camera.schema.json",
            "https://example.invalid/v.json#/$defs/camera",
            "#/$defs/typo",
            "#camera",
            "#/$defs/camera/required/7",
            7,
        ):
            with self.subTest(ref=ref):
                schema = copy.deepcopy(SCHEMA)
                schema["properties"]["camera"] = {"$ref": ref}
                self.assertEqual(
                    appendix_messages(ex, schema),
                    [
                        f"/properties/camera: cannot follow $ref {ref!r}; only '#' and '#/...' pointers to a subschema "
                        "of this file are read"
                    ],
                )

    def test_refs_follow_json_pointers(self) -> None:
        schema = copy.deepcopy(SCHEMA)
        schema["$defs"]["a/b c"] = {"oneOf": [{"type": "null"}, {"$ref": "#/$defs/camera"}]}
        schema["properties"]["camera"] = {"$ref": "#/$defs/a~1b%20c/oneOf/1"}  # ~1, %20 and an array index
        self.assertEqual(appendix_messages(schema=schema), [])
        ex = self.example()
        del ex["camera"]["lat"]
        self.assertEqual(appendix_messages(ex, schema), ["camera.lat: required by the schema, missing"])
        tree = {"type": "object", "additionalProperties": False, "properties": {"child": {"$ref": "#"}}}
        self.assertEqual(
            d.schema_shape_problems({"child": {"child": {"x": 1}}}, tree, tree),
            ["child.child.x: not a property in the schema"],
        )

    def test_keywords_beside_a_ref_apply_too(self) -> None:
        # Review finding (20 Nov): JSON Schema 2020-12 applies them; they were dropped.
        schema = copy.deepcopy(SCHEMA)
        schema["properties"]["camera"] = {"$ref": "#/$defs/camera", "required": ["lon"]}
        ex = self.example()
        del ex["camera"]["lon"]
        self.assertEqual(appendix_messages(ex, schema), ["camera.lon: required by the schema, missing"])
        schema["properties"]["camera"] = {"$ref": "#/$defs/camera", "type": "array"}
        self.assertEqual(appendix_messages(schema=schema), ["camera: an object where the schema allows none"])

    def test_dependent_required(self) -> None:
        # Review finding (20 Nov): it was ignored.
        schema = copy.deepcopy(SCHEMA)
        schema["$defs"]["camera"]["dependentRequired"] = {"lat": ["lon"], "alt": ["lat"]}
        self.assertEqual(appendix_messages(schema=schema), [])
        ex = self.example()
        del ex["camera"]["lon"]
        self.assertEqual(
            appendix_messages(ex, schema), ["camera.lon: required by the schema when lat is present, missing"]
        )

    def test_any_of_takes_a_branch_that_fits(self) -> None:
        schema = copy.deepcopy(SCHEMA)
        schema["properties"]["pose"] = {
            "anyOf": [
                {"type": "object", "additionalProperties": False, "properties": {"fov_deg": {"type": "number"}}},
                {"type": "object", "additionalProperties": False, "properties": {"matrix": {"type": "array"}}},
            ]
        }
        ex = self.example()
        ex["pose"] = {"matrix": [1, 0, 0]}
        self.assertEqual(appendix_messages(ex, schema), [])  # the second branch fits
        ex["pose"] = {"matrix": [1], "fov_deg": 1.0}  # neither fits: the closest is reported
        self.assertEqual(appendix_messages(ex, schema), ["pose.matrix: not a property in the schema"])

    def test_a_cycle_is_reported_not_followed_forever(self) -> None:
        cycle = "camera: $ref, allOf, oneOf or anyOf lead back to a schema already applied here (a cycle)"
        loops = [
            {"$ref": "#/$defs/loop"},
            {"allOf": [{"$ref": "#/$defs/loop"}]},
            {"anyOf": [{"$ref": "#/$defs/loop"}, {"$ref": "#/$defs/loop"}]},
        ]  # two branches: no blow-up
        for loop in loops:
            with self.subTest(loop=loop):
                schema = copy.deepcopy(SCHEMA)
                schema["$defs"]["loop"] = loop
                schema["properties"]["camera"] = {"$ref": "#/$defs/loop"}
                self.assertEqual(sorted(set(appendix_messages(schema=schema))), [cycle])
        schema = copy.deepcopy(SCHEMA)  # the same schema twice in one place is no cycle
        schema["properties"]["camera"] = {"allOf": [{"$ref": "#/$defs/camera"}, {"$ref": "#/$defs/camera"}]}
        self.assertEqual(appendix_messages(schema=schema), [])

    def test_shape_constraints_inside_then_else_and_not_fail_it(self) -> None:
        cases = {
            "uses required inside then": {"if": {"required": ["lat"]}, "then": {"required": ["lon"]}},
            "uses additionalProperties inside else": {
                "if": {},
                "else": {"properties": {"x": {"additionalProperties": False}}},
            },
            "uses $ref inside not": {"not": {"$ref": "#/$defs/landmark"}},
            "uses type inside then": {"if": {}, "then": {"properties": {"lat": {"type": "array"}}}},
            "uses a false subschema inside then": {"if": {}, "then": {"properties": {"lat": False}}},
            "uses else: false": {"if": {}, "else": False},
        }
        for what, sub in cases.items():
            with self.subTest(what=what):
                schema = copy.deepcopy(SCHEMA)
                schema["$defs"]["camera"].update(sub)
                self.assertEqual(
                    reading_messages(schema),
                    [
                        f"{what}, which check_appendix_a does not apply (scalars are placeholders, so it can't tell "
                        "whether `if` holds); teach it before trusting this check"
                    ],
                )
        # Scalar-only constraints in then (like the real schema's truth_camera) and shapes inside if pass.
        schema = copy.deepcopy(SCHEMA)
        schema["$defs"]["camera"].update(
            {
                "if": {"required": ["lat"], "properties": {"lat": {"const": 0}}},
                "then": {"properties": {"lon": {"const": 0, "minimum": 0}}},
            }
        )
        self.assertEqual(appendix_messages(schema=schema), [])

    def test_the_example_is_found_and_located(self) -> None:
        ex = self.example()
        ex["zz"] = 1
        self.assertEqual(
            [str(p) for p in d.check_appendix_a(appendix_repo(ex))], ["PRD.md:6: zz: not a property in the schema"]
        )  # line 6 opens the JSON
        self.assertIn("the example is not JSON", d.check_appendix_a(appendix_repo(raw='{"id": '))[0].message)
        later_block = d.Repo.memory(
            {
                "PRD.md": "# PRD\n\n## Appendix A: x\n\nno block\n\n## Appendix B\n\n```json\n{}\n```\n",
                d.MANIFEST_SCHEMA: "{}",
            }
        )
        self.assertEqual(
            [p.message for p in d.check_appendix_a(later_block)], ["no ```json block under '## Appendix A'"]
        )
        self.assertEqual(
            [p.where for p in d.check_appendix_a(d.Repo.memory({"PRD.md": "# PRD\n"}))], [d.MANIFEST_SCHEMA]
        )
        self.assertEqual(d.check_appendix_a(d.Repo.memory({"README.md": "# x\n"})), [])


class CiSuites(unittest.TestCase):
    """`make test` against CI (the `ci-suites` check), and its Makefile and workflow readers."""

    def test_make_variable(self) -> None:
        text = "A = x\nTEST_SUITES ?= a b \\\n\tc  # three\nTEST_SUITES += d\nB :=\nC::=y\ntest-a := 1\n"
        self.assertEqual(d.make_variable(text, "TEST_SUITES"), ["a", "b", "c"])  # the first assignment
        self.assertEqual(d.make_variable(text, "B"), [])
        self.assertEqual(d.make_variable(text, "C"), ["y"])
        self.assertIsNone(d.make_variable(text, "MISSING"))
        self.assertIsNone(d.make_variable("XTEST_SUITES = a\n", "TEST_SUITES"))

    def test_ci_make_targets(self) -> None:
        targets, suites = d.ci_make_targets(CI_YML)
        self.assertEqual(suites, {"common"})
        self.assertEqual(targets, {"test-common", "test-docs"})  # not the step name, the comment or echo
        one_line = "steps:\n  - run: cd x && make -C app -j 4 test-rust lint\n  - run: make test  # all\n"
        self.assertEqual(d.ci_make_targets(one_line)[0], {"test-rust", "lint", "test"})

    def test_the_problems_name_the_suite(self) -> None:
        files = mutate(base_files(), "Makefile", "TEST_SUITES ?= common docs", "TEST_SUITES ?= common docs bench")
        self.assertEqual(
            [(p.where, p.message) for p in problems("ci-suites", files)],
            [
                ("Makefile", "TEST_SUITES names bench, but there is no test-bench rule"),
                (
                    ".github/workflows/ci.yml",
                    "never runs make test-bench (TEST_SUITES); run it, or list it in LAPTOP_ONLY_SUITES",
                ),
            ],
        )
        files = mutate(base_files(), ".github/workflows/ci.yml", "run: echo done", "run: make test-speech test-bench")
        self.assertEqual(
            [(p.where, p.message) for p in problems("ci-suites", files)],
            [
                (".github/workflows/ci.yml", "runs make test-speech, which the Makefile lists in LAPTOP_ONLY_SUITES"),
                (".github/workflows/ci.yml", "runs make test-bench, which TEST_SUITES lacks"),
            ],
        )
        files = mutate(base_files(), "Makefile", "LAPTOP_ONLY_SUITES := speech", "LAPTOP_ONLY_SUITES := speech rust")
        self.assertEqual(
            [p.message for p in problems("ci-suites", files)],
            ["LAPTOP_ONLY_SUITES names rust, which TEST_SUITES lacks"],
        )
        files = mutate(base_files(), "Makefile", "test-common: ## common", "test-common := x")  # a variable
        self.assertEqual(
            [p.message for p in problems("ci-suites", files)],
            ["TEST_SUITES names common, but there is no test-common rule"],
        )

    def test_make_test_in_ci_covers_every_suite_but_the_laptop_ones(self) -> None:
        files = mutate(
            base_files(), ".github/workflows/ci.yml", "run: make test-${{ matrix.suite }}\n", "run: make test\n"
        )
        files = mutate(
            files, ".github/workflows/ci.yml", "make --no-print-directory -C . PYTHON=python3 test-docs", "true"
        )
        self.assertEqual(problems("ci-suites", files), [])

    def test_nothing_to_compare_without_both_files(self) -> None:
        for missing in ("Makefile", ".github/workflows/ci.yml"):
            with self.subTest(missing=missing):
                files = {k: v for k, v in base_files().items() if k != missing}
                self.assertEqual(problems("ci-suites", files), [])
        files = mutate(base_files(), "Makefile", "TEST_SUITES ?= common docs \\\n\tspeech", "SUITES = common")
        self.assertEqual([p.message for p in problems("ci-suites", files)], ["TEST_SUITES is not assigned, or empty"])


class RealFilesAreRead(unittest.TestCase):
    """The lock-file and Appendix A readers find something in the repo's real files, so `notices`
    and `appendix-a` can't pass vacuously on a format they misread. Each mutation is made in memory
    and picked from the real files, so no package, version or key is pinned here."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = d.Repo()
        cls.sources = d.notices_sources(cls.repo)

    def real_files(self) -> dict[str, str]:
        names = [*self.sources, d.NOTICES, "PRD.md", d.MANIFEST_SCHEMA]
        return {n: self.repo.read(n) for n in names if self.repo.has(n)}

    def first_uv_lock(self) -> str:
        found = [p for p in self.sources if posixpath.basename(p) == "uv.lock"]
        if not found:
            self.skipTest("no uv.lock in the repo")
        return found[0]

    def test_every_lock_file_yields_packages_and_no_errors(self) -> None:
        self.assertTrue(self.sources, "no uv.lock, Cargo.lock or package.json in the repo")
        for path in self.sources:
            with self.subTest(path=path):
                required, errors = d.NOTICES_READERS[posixpath.basename(path)](self.repo.read(path))
                self.assertEqual(errors, [])
                self.assertTrue(required, f"{path}: the reader found nothing to name")

    def test_a_package_dropped_from_the_notices_is_caught(self) -> None:
        files, path = self.real_files(), self.first_uv_lock()
        name, version, _ = d.uv_required(files[path])[0][0]
        files[d.NOTICES] = "\n".join(
            line for line in files[d.NOTICES].split("\n") if not d.named_with_version([line], name, version)
        )
        messages = [p.message for p in d.check_notices(d.Repo.memory(files))]
        self.assertTrue(any(m.startswith(f"{name} {version} ({path},") for m in messages), messages)

    def test_a_version_bumped_in_a_real_lock_is_caught(self) -> None:
        files, path = self.real_files(), self.first_uv_lock()
        name, version, _ = d.uv_required(files[path])[0][-1]
        old = f'name = "{name}"\nversion = "{version}"'
        self.assertIn(old, files[path])
        files[path] = files[path].replace(old, f'name = "{name}"\nversion = "{version}.999"')
        messages = [p.message for p in d.check_notices(d.Repo.memory(files))]
        self.assertTrue(
            any(f"{name} {version}.999 (" in m or f"cannot resolve {name}" in m for m in messages), messages
        )

    def test_the_real_appendix_a_example_is_walked(self) -> None:
        files = self.real_files()
        if "PRD.md" not in files or d.MANIFEST_SCHEMA not in files:
            self.skipTest("PRD.md or the manifest schema is missing")
        schema = json.loads(files[d.MANIFEST_SCHEMA])
        line, text = d.appendix_a_example(files["PRD.md"])
        self.assertTrue(line, "no ```json block under '## Appendix A' in PRD.md")
        example = json.loads(text)
        required = [key for key in schema.get("required", []) if key in example]
        self.assertTrue(required, "the example has none of the schema's required top-level keys")
        del example[required[0]]
        example["zz_not_in_the_schema"] = 1
        messages = d.schema_shape_problems(example, schema, schema)
        self.assertIn(f"{required[0]}: required by the schema, missing", messages)
        if schema.get("additionalProperties") is False:
            self.assertIn("zz_not_in_the_schema: not a property in the schema", messages)


@unittest.skipIf(shutil.which("git") is None, "the git CLI is not installed")
class GitFixtures(unittest.TestCase):
    """Throwaway git repos in a temporary directory, removed afterwards: merges in DECISIONS.md's
    history (review finding: neighbouring log entries were compared, so two branches that each
    appended looked like a deletion), and link targets in the wrong case or ignored by git."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="doccheck-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        self.root.mkdir()
        # Isolated from the user's and the system's git config, with a fixed generic identity.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            HOME=tmp.name,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME="doccheck test",
            GIT_AUTHOR_EMAIL="test@example.invalid",
            GIT_COMMITTER_NAME="doccheck test",
            GIT_COMMITTER_EMAIL="test@example.invalid",
        )
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")

    def git(self, *args: str) -> str:
        out = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return out.stdout

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "--short=7", "HEAD").strip()

    def append_only(self) -> list[d.Problem]:
        return d.check_decisions_append_only(d.Repo(self.root))

    def two_appending_branches(self) -> dict[str, str]:
        """base; branch a inserts D-002 above the ledger; main appends a ledger row. Returns shas."""
        shas = {"base": ""}
        self.write("DECISIONS.md", DECISIONS_V0)
        shas["base"] = self.commit("base")
        self.git("checkout", "-q", "-b", "a")
        self.write(
            "DECISIONS.md",
            DECISIONS_V0.replace(
                "## Spend ledger", "## D-002 — Second · proposed · 2026-11-19\n- Two.\n\n## Spend ledger"
            ),
        )
        shas["a"] = self.commit("a: D-002")
        self.git("checkout", "-q", "main")
        self.write("DECISIONS.md", DECISIONS_V0 + "| 2026-11-19 | — | nothing | $0.00 | $0.00 |\n")
        shas["main"] = self.commit("main: a ledger row")
        return shas

    def test_two_branches_that_each_append_merge_cleanly(self) -> None:
        self.two_appending_branches()
        self.git("merge", "-q", "--no-ff", "--no-edit", "a")  # automatic: no manual edits
        self.assertEqual(self.append_only(), [])
        # base -> a, base -> main, and the merge against each of its two parents.
        self.assertEqual(len(d.Repo(self.root).decisions_changes()), 4)

    def test_a_merge_that_drops_a_parents_line_is_reported(self) -> None:
        shas = self.two_appending_branches()
        self.git("merge", "-q", "--no-ff", "--no-commit", "a")
        merged = (self.root / "DECISIONS.md").read_text(encoding="utf-8")
        self.write("DECISIONS.md", merged.replace("| 2026-11-19 | — | nothing | $0.00 | $0.00 |\n", ""))
        merge = self.commit("merge a, dropping main's ledger row")
        found = self.append_only()
        self.assertEqual([p.where for p in found], [f"DECISIONS.md ({shas['main']} -> {merge})"])
        self.assertIn("2026-11-19", found[0].message)

    def test_a_rewrite_on_a_merged_branch_is_reported(self) -> None:
        self.two_appending_branches()
        self.git("merge", "-q", "--no-ff", "--no-edit", "a")
        self.git("checkout", "-q", "-b", "b")
        self.write(
            "DECISIONS.md",
            (self.root / "DECISIONS.md")
            .read_text(encoding="utf-8")
            .replace("- The first decision.", "- The first decision, reworded."),
        )
        rewrite = self.commit("b: reword D-001")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "--no-edit", "b")
        found = self.append_only()
        self.assertIn(rewrite, " ".join(p.where for p in found))
        self.assertTrue(all("The first decision." in p.message for p in found), found)

    def test_the_working_tree_is_compared_with_head(self) -> None:
        self.write("DECISIONS.md", DECISIONS_V0)
        self.commit("base")
        self.write("DECISIONS.md", DECISIONS_V0.replace("Entries are append-only.\n", ""))
        found = self.append_only()
        self.assertEqual([p.where for p in found], ["DECISIONS.md (HEAD -> working tree)"])

    def test_deleting_decisions_is_reported(self) -> None:
        self.write("DECISIONS.md", DECISIONS_V0)
        self.commit("base")
        self.git("rm", "-q", "DECISIONS.md")
        gone = self.commit("delete it")
        found = self.append_only()
        self.assertTrue(found)
        self.assertTrue(all(f"{gone} (deleted DECISIONS.md)" in p.where for p in found), found)

    def test_links_follow_git_and_exact_letter_case(self) -> None:
        self.write("PLAN.md", "# Plan\n\n## Step one\n")
        self.write(".gitignore", "local/\n")
        self.write("README.md", "[a](PLAN.md#step-one) [b](docs/) [c](docs/new.md) [d](.)\n")
        self.commit("init")
        self.write("docs/new.md", "Untracked, not ignored: it will be committed with the link.\n")
        self.write("local/notes.md", "On disk, but git ignores it.\n")
        self.assertEqual(d.check_links(d.Repo(self.root)), [])
        self.write("README.md", "[p](plan.md) [n](local/notes.md) [ok](PLAN.md)\n")
        found = d.check_links(d.Repo(self.root))
        self.assertEqual(len(found), 2, found)
        self.assertIn("plan.md differs in letter case from PLAN.md", found[0].message)
        self.assertIn("local/notes.md is not in the repo (missing, or ignored by git)", found[1].message)


if __name__ == "__main__":
    unittest.main()
