# docs/checks

Consistency checks for the docs: PLAN.md, DECISIONS.md, PRD_PROGRESS.md, PRD.md and CLAUDE.md, plus every markdown file, and the references that code makes to them. They test invariants, never today's wording, so the docs can keep changing.

```sh
make docs-check                                     # = python3 -I -B -m unittest discover -s docs/checks -t docs/checks
python3 -I -B docs/checks/doccheck.py               # every check, with each problem as path:line
python3 -I -B docs/checks/doccheck.py links tables  # only some checks
```

The checks use the standard library only, with Python 3.9 or later, in isolated mode (`-I`). They make no network calls. They read the working tree and git, and write nothing; `-B` keeps Python from writing a `__pycache__` into the repo.

## What is checked

| Check | Invariant |
|---|---|
| `links` | Every relative link in a markdown file names a file or folder of the repo as GitHub sees it: git's tracked files plus the untracked ones it does not ignore, in exact letter case (this laptop's disk is case-insensitive, and a git-ignored file never reaches GitHub). Every `#anchor` names a heading (GitHub's slug) or an `id` in its target. |
| `tables` | Every table row has as many cells as its header. GitHub splits cells on every unescaped pipe, even inside code spans, so a literal pipe is written `\|`. |
| `weekdays` | Every weekday-labelled date (`Mon 14 Dec`, `Thu 19 → Mon 23 Nov`) is a real day and falls on that weekday. A date without a year takes the year nearest the doc's `**Updated:**` date (day G when it has none), so write the year when a doc spans more than about five months. |
| `day-g` | Day G is defined the same way wherever it is defined (PLAN.md `G = Thu 19 Nov 2026`, CLAUDE.md `2026-11-19 (day G)`). Where a line has as many exact `G+N` offsets as weekday-labelled dates, each date is G + N (a date without a year is read near the day its offset names). Offsets after "about", "before", "after", `~` or `≈` are skipped. |
| `calendar` | In a table with a `Length` column, a row spanning `G+a → G+b` lasts b − a days. A slack column whose header names a percentage holds that share of the length (the 25 % rule). |
| `milestones` | Each milestone's dates agree across PLAN.md's calendar row, PLAN.md's heading and PRD_PROGRESS.md's heading. Its G offsets agree between PLAN.md's calendar and the PRD §16 table. |
| `decisions` | DECISIONS.md D-ids run D-001, D-002, … with no gap. Each heading reads `## D-NNN — title · status · …`. The status uses a word the `**Status:**` line declares. Heading dates are real, never go backwards and are not in the future. A `supersedes`, `amends` or `closes D-n` points to an earlier entry. |
| `append-only` | No committed line of DECISIONS.md is ever changed or removed. Every commit that touched it, compared with each of its own parents (both, for a merge), only inserts lines, and so does the working tree against HEAD. Two branches that each append and merge cleanly pass. |
| `d-refs` | Every `D-NNN` cited anywhere in the repo, in docs or code, is a DECISIONS.md entry. |
| `tickets` | PRD_PROGRESS.md ticket ids are unique. Each sits under its own milestone's heading, and its status starts with a value from the `**Status values:**` list. Every ticket id cited in the repo exists. A citation whose number has another digit count than its milestone's tickets (`M4-5` while M4's are `M4-01`, …), or one digit for a milestone with no tickets, reads as a milestone range and is not checked. |
| `spend` | In DECISIONS.md's spend ledger, each running total is the previous total plus the amount, and dates never go backwards. CLAUDE.md's `Spend so far` equals the last running total. |
| `prd-sections` | Every `§n.m` in the living docs and the code names a heading of the current PRD.md, and so do both ends of a range (`§13.4-13.7`, `§10.2–10.6`, `§13.4-7`). These are skipped: DECISIONS.md, `docs/review/` and `docs/council/` (historical records numbered against older PRD versions), and a `§` within 40 characters after "Oculus", "RFC", "ISO" or "v0.1"–"v0.3". |
| `viewpoints` | Every `<park>.<view>` id in the docs that uses a manifest's park prefix is a manifest in `viewpoints/manifests/`. CLAUDE.md's viewpoint table lists exactly the manifests. |

## Files

- `doccheck.py`: the checks, and a CLI that prints every problem.
- `test_docs.py`: one test per check, run on the repo.
- `test_selftest.py`: each check passes a small consistent set of in-memory docs. A deliberate break of each invariant (one or more mutations per check) must be caught. It also covers lookalikes that must not be flagged, and the date, slug and table helpers. `GitFixtures` builds throwaway git repos in a temporary directory for what needs real git and a real disk: merges in DECISIONS.md's history, and link targets in the wrong case or ignored by git. They need the `git` CLI and are skipped without it.

## When a check fails

Fix the doc, not the check. Two deliberate escape hatches exist:

- **`APPEND_ONLY_EXEMPT`** in `doccheck.py`: a commit allowed to rewrite committed DECISIONS.md lines, with the reason. Use it only with the lead's sign-off, for example to scrub a secret or a personal detail. A reversed decision gets a new entry instead.
- **The `append-only` check needs history.** On a shallow clone it checks only the history it can see. CI checks out the full history (`fetch-depth: 0`).

## History

These checks replace the docs agent's scratch tests of 19 Nov (`test_docs.py`, `mutate_check.py`, `mdcheck.py`):

- **Kept and generalised:** links, table widths, weekday labels, G offsets, the slack rule, D-id order, D-references and append-only. Append-only was checked against HEAD only; it now covers the whole history. The scratch checks pinned exact counts (21 D-ids, 7 calendar rows, more than 30 dates); these don't.
- **Changed:** `mdcheck.py` treated a pipe inside a code span as text. GitHub splits cells on it, so the table check now follows GitHub.
- **Dropped:** the review-finding probes (`test_f1`–`test_f12`) and their mutation runner. They matched sentences of the 19 Nov text, so any rewording would break them.
- **Dropped:** the measured-number checks (archive start dates, sample sizes, stopgap counts and outages, Last-Modified against EXIF). They read a dated snapshot of `~/lookout-data` that CI doesn't have. They pinned "as of 08:30 UTC" moments, and the stopgap they read is retired (D-023). The numbers stay in the agents' reports and PRD_PROGRESS.md.
- **Added:** milestone dates across documents, D-heading statuses and dates, ticket ids and statuses, the spend ledger, PRD § references and viewpoint ids.
