"""The docs invariants, run on this repo's working tree and git history (`make docs-check`).

One test per check in doccheck.CHECKS; doccheck.py says what each invariant is. A failure lists
every problem as path:line. Standard library only; no network.
"""

from __future__ import annotations

import unittest

import doccheck

REPO = doccheck.Repo()


class DocsInvariants(unittest.TestCase):
    """Each check must report no problems on the repo as it stands."""

    def assert_clean(self, name: str) -> None:
        problems = doccheck.CHECKS[name](REPO)
        if problems:
            self.fail(f"{name}: {len(problems)} problem(s)\n" + "\n".join(f"  {p}" for p in problems))

    def test_links_resolve(self) -> None:
        self.assert_clean("links")

    def test_tables_have_their_header_width(self) -> None:
        self.assert_clean("tables")

    def test_weekday_labels_match_dates(self) -> None:
        self.assert_clean("weekdays")

    def test_day_g_and_offsets(self) -> None:
        self.assert_clean("day-g")

    def test_calendar_arithmetic(self) -> None:
        self.assert_clean("calendar")

    def test_milestone_dates_agree(self) -> None:
        self.assert_clean("milestones")

    def test_decisions_headings_ids_dates(self) -> None:
        self.assert_clean("decisions")

    def test_decisions_append_only(self) -> None:
        if not REPO.decisions_changes():
            self.skipTest(REPO.history_note or "no change to DECISIONS.md to compare")
        self.assert_clean("append-only")

    def test_decision_references_resolve(self) -> None:
        self.assert_clean("d-refs")

    def test_tickets(self) -> None:
        self.assert_clean("tickets")

    def test_spend_ledger(self) -> None:
        self.assert_clean("spend")

    def test_prd_section_references_resolve(self) -> None:
        self.assert_clean("prd-sections")

    def test_viewpoint_ids_are_manifests(self) -> None:
        self.assert_clean("viewpoints")

    def test_third_party_notices_name_every_locked_package(self) -> None:
        self.assert_clean("notices")

    def test_prd_appendix_a_has_the_manifest_schemas_shape(self) -> None:
        self.assert_clean("appendix-a")

    def test_ci_runs_every_make_test_suite(self) -> None:
        self.assert_clean("ci-suites")

    def test_every_check_has_a_test(self) -> None:
        names = {
            "links",
            "tables",
            "weekdays",
            "day-g",
            "calendar",
            "milestones",
            "decisions",
            "append-only",
            "d-refs",
            "tickets",
            "spend",
            "prd-sections",
            "viewpoints",
            "notices",
            "appendix-a",
            "ci-suites",
        }
        self.assertEqual(set(doccheck.CHECKS), names)


if __name__ == "__main__":
    unittest.main()
