"""Self-tests for doccheck.py: each check passes a small consistent set of docs and catches a
deliberate break of its invariant (a mutation), so no check can pass vacuously.

Most docs here are synthetic, built in memory. `GitFixtures` builds throwaway git repos in a
temporary directory for what only real git and a real disk show: merges in DECISIONS.md's history,
letter case and git-ignored link targets. Standard library only, plus the git CLI.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import doccheck as d

TODAY = dt.date(2026, 11, 19)

PLAN = """# PLAN.md

Go-ahead day is **G = Thu 19 Nov 2026**. Decisions are in [DECISIONS.md](DECISIONS.md#d-001--first--decided--2026-11-18).

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

DECISIONS = DECISIONS_V0.replace(
    "## Spend ledger",
    "## D-002 — Second · proposed (owner) · 2026-11-19 · supersedes D-001\n- Points back to D-001.\n\n"
    "## Spend ledger",
) + "| 2026-11-19 | fal | kill test | $0.50 | $0.50 |\n"

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


def base_files() -> Dict[str, str]:
    return {
        "PLAN.md": PLAN,
        "DECISIONS.md": DECISIONS,
        "PRD_PROGRESS.md": PROGRESS,
        "PRD.md": PRD,
        "CLAUDE.md": CLAUDE,
        "docs/research/note.md": "See [the plan](../../PLAN.md) and [M1](../../PLAN.md#m1-build--thu-19--mon-23-nov--in-progress).\n"
                                 "```\n[not a link](nowhere.md) Fri 19 Nov\n```\n",
        "workers/x/src/mod.py": '"""Implements PRD §16.1 and D-002; ticket M2-01."""\n',
        "viewpoints/manifests/rainier.paradise-mountain.json": "{}\n",
        "viewpoints/manifests/point-reyes.beach.json": "{}\n",
    }


def problems(check: str, files: Dict[str, str], history: Optional[Sequence[str]] = None) -> List[d.Problem]:
    repo = d.Repo.memory(files, history if history is not None else [DECISIONS_V0, files["DECISIONS.md"]])
    if check == "decisions":
        return d.check_decisions(repo, today=TODAY)
    return d.CHECKS[check](repo)


def mutate(files: Dict[str, str], path: str, old: str, new: str) -> Dict[str, str]:
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

    CASES = [
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
        ("milestones", "PRD_PROGRESS.md", "### M2: Test (Mon 23 Nov → Mon 7 Dec)", "### M2: Test (Mon 23 Nov → Sun 6 Dec)"),
        ("milestones", "PRD.md", "**M2 Test** (G+4 → G+18)", "**M2 Test** (G+4 → G+19)"),
        ("decisions", "DECISIONS.md", "## D-002 — Second", "## D-003 — Second"),
        ("decisions", "DECISIONS.md", "· proposed (owner) ·", "· maybe ·"),
        ("decisions", "DECISIONS.md", "· proposed (owner) · 2026-11-19", "· proposed (owner) · 2026-11-17"),
        ("decisions", "DECISIONS.md", "· proposed (owner) · 2026-11-19", "· proposed (owner) · 2099-01-01"),
        ("decisions", "DECISIONS.md", "· proposed (owner) · 2026-11-19", "· proposed (owner) · 2026-11-31"),
        ("decisions", "DECISIONS.md", "supersedes D-001", "supersedes D-002"),
        ("decisions", "DECISIONS.md", "## D-002 — Second · proposed (owner) · 2026-11-19 · supersedes D-001",
         "## D-002 - Second"),
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
    ]

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
        files = mutate(base_files(), "DECISIONS.md", "## Spend ledger",
                       "## D-003 — Third · open · 2026-11-19 · amends D-002\n- More.\n\n## Spend ledger")
        self.assertEqual(problems("append-only", files), [])
        self.assertEqual(problems("decisions", files), [])

    def test_a_later_date_in_a_heading_is_not_the_entry_date(self) -> None:
        files = mutate(base_files(), "DECISIONS.md", "supersedes D-001",
                       "supersedes D-001 (of 2026-11-18)")
        self.assertEqual(problems("decisions", files), [])

    def test_decisions_with_no_date_and_several_statuses(self) -> None:
        files = mutate(base_files(), "DECISIONS.md", "## Spend ledger",
                       "## D-003 — Hosting · open (owner input needed)\n- No date.\n\n"
                       "## D-004 — Split · Rainier P decided; ARD H open · 2026-11-19\n- Mixed.\n\n## Spend ledger")
        self.assertEqual(problems("decisions", files), [])

    def test_milestone_ranges_are_not_ticket_ids(self) -> None:
        # M1's tickets have two digits (M1-01), and M4 has none yet: both read as ranges.
        self.assert_clean("tickets", "PLAN.md", "Ticket M1-01 starts it",
                          "Ticket M1-01 starts it; the owner reviews during M4-5 and M1-2")

    def test_section_ranges_whose_ends_exist(self) -> None:
        self.assert_clean("prd-sections", "PLAN.md", "PRD §16)", "PRD §1-16.1, §16–§16.1, §16.1-1)")

    def test_year_less_dates_are_read_near_the_docs_updated_date(self) -> None:
        # Mid-2027 dates without a year, in a doc updated in 2027: read as 2027, so they pass.
        later = ("# Later\n\n**Updated:** Mon 14 Jun 2027.\n\n"
                 "Freeze on Mon 14 Jun; reviews on Fri 21 May and Mon 24 May.\n")
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
        self.assertEqual([d.day_amount(s) for s in ["3½ days", "¾ day", "~1¼ days", "4 days", "—"]],
                         [3.5, 0.75, 1.25, 4.0, None])

    def test_heading_slug_matches_github(self) -> None:
        self.assertEqual(d.heading_slug("1. Decisions to confirm"), "1-decisions-to-confirm")
        self.assertEqual(d.heading_slug("D-001 — Milestone 0 · decided · 2026-11-18"),
                         "d-001--milestone-0--decided--2026-11-18")
        self.assertEqual(d.heading_slug("Hard stop at the `cap`"), "hard-stop-at-the-cap")

    def test_anchors_number_repeated_headings(self) -> None:
        self.assertEqual(d.anchors("# A\n## A\n<a id=\"Here\"></a>\n"), {"a", "a-1", "here"})

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
        self.assertEqual([d.section_range_end(a, b) for a, b in
                          [("13.4", "13.7"), ("13.4", "7"), ("1", "16.9"), ("10.2.1", "3")]],
                         ["13.7", "13.7", "16.9", "10.2.3"])

    def test_is_ticket_reference(self) -> None:
        digits = {"M0": {1}, "M1": {2}}
        self.assertEqual([d.is_ticket_reference(t, digits) for t in
                          ["M0-7", "M1-07", "M1-5", "M4-5", "M4-01", "M0.5-1"]],
                         [True, True, False, False, True, False])


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
        self.env.update(HOME=tmp.name, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="doccheck test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="doccheck test", GIT_COMMITTER_EMAIL="test@example.invalid")
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")

    def git(self, *args: str) -> str:
        out = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=self.root, env=self.env,
                             capture_output=True, text=True, check=True, timeout=60)
        return out.stdout

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "--short=7", "HEAD").strip()

    def append_only(self) -> List[d.Problem]:
        return d.check_decisions_append_only(d.Repo(self.root))

    def two_appending_branches(self) -> Dict[str, str]:
        """base; branch a inserts D-002 above the ledger; main appends a ledger row. Returns shas."""
        shas = {"base": ""}
        self.write("DECISIONS.md", DECISIONS_V0)
        shas["base"] = self.commit("base")
        self.git("checkout", "-q", "-b", "a")
        self.write("DECISIONS.md", DECISIONS_V0.replace(
            "## Spend ledger", "## D-002 — Second · proposed · 2026-11-19\n- Two.\n\n## Spend ledger"))
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
        self.write("DECISIONS.md", (self.root / "DECISIONS.md").read_text(encoding="utf-8")
                   .replace("- The first decision.", "- The first decision, reworded."))
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
