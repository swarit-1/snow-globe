# docs/checks

Consistency checks for the docs: PLAN.md, DECISIONS.md, PRD_PROGRESS.md, PRD.md and CLAUDE.md, plus every markdown file, the references that code makes to them, THIRD_PARTY_NOTICES against the lock files, and the Makefile's `make test` suites against CI. They test invariants, never today's wording, so the docs can keep changing.

```sh
make docs-check                                     # = python3 -I -B -m unittest discover -s docs/checks -t docs/checks
make test-docs                                      # docs-check, then ruff check + ruff format --check on docs/checks
python3 -I -B docs/checks/doccheck.py               # every check, with each problem as path:line
python3 -I -B docs/checks/doccheck.py links tables  # only some checks
```

The checks use the standard library only, with Python 3.9 or later, in isolated mode (`-I`). They make no network calls. They read the working tree and git, and write nothing; `-B` keeps Python from writing a `__pycache__` into the repo. Their own code is linted and format-checked by `make test-docs` (and `make lint`, `make fmt-check`) with workers/common's locked ruff, under `ruff.toml` here: the repo's rules, for Python 3.9.

## What is checked

| Check | Invariant |
|---|---|
| `links` | Every relative link in a markdown file names a file or folder of the repo as GitHub sees it: git's tracked files plus the untracked ones it does not ignore, in exact letter case (this laptop's disk is case-insensitive, and a git-ignored file never reaches GitHub). Every `#anchor` names a heading (GitHub's slug) or an `id` in its target. |
| `tables` | Every table row has as many cells as its header. GitHub splits cells on every unescaped pipe, even inside code spans, so a literal pipe is written `\|`. |
| `weekdays` | Every weekday-labelled date (`Mon 14 Dec`, `Thu 19 → Mon 23 Nov`) is a real day and falls on that weekday. A date without a year takes the year nearest the doc's `**Updated:**` date (day G when it has none), so write the year when a doc spans more than about five months. |
| `day-g` | Day G is defined the same way wherever it is defined (PLAN.md `G = Thu 19 Nov 2026`, CLAUDE.md `2026-11-19 (day G)`). Where a line has as many exact `G+N` offsets as weekday-labelled dates, each date is G + N (a date without a year is read near the day its offset names). Offsets after "about", "before", "after", `~` or `≈` are skipped. |
| `calendar` | In a table with a `Length` column, a row spanning `G+a → G+b` lasts b − a days. A slack column whose header names a percentage holds that share of the length (the 25 % rule). |
| `milestones` | Each milestone's dates agree across PLAN.md's calendar row, PLAN.md's heading and PRD_PROGRESS.md's heading. Its G offsets agree between PLAN.md's calendar and the PRD §16 table. |
| `decisions` | DECISIONS.md D-ids run D-001, D-002, … with no gap. Each heading reads `## D-NNN — title · status · …`. The status uses a word the `**Status:**` line declares. Heading dates are real, never go backwards and are not in the future. Every D-id after `supersedes`, `amends` or `closes` (`amends D-023, D-048`) points to an earlier entry. **Back-pointers:** an `**Amended <date> (D-m):**` line inside entry D-n (several ids, or a range `D-070 to D-072`, allowed) cites only later entries whose headings name D-n after one of those verbs. Once any pointer exists, the earliest entry one cites starts the rule: every entry a heading from there on amends carries a pointer back to it. A file with no pointer needs none, so dropping every pointer is as consistent as keeping them all. |
| `append-only` | No committed line of DECISIONS.md is ever changed or removed. Every commit that touched it, compared with each of its own parents (both, for a merge), only inserts lines, and so does the working tree against HEAD. Two branches that each append and merge cleanly pass. |
| `d-refs` | Every `D-NNN` cited anywhere in the repo, in docs or code, is a DECISIONS.md entry. |
| `tickets` | PRD_PROGRESS.md ticket ids are unique. Each sits under its own milestone's heading, and its status starts with a value from the `**Status values:**` list. Every ticket id cited in the repo exists. A citation whose number has another digit count than its milestone's tickets (`M4-5` while M4's are `M4-01`, …), or one digit for a milestone with no tickets, reads as a milestone range and is not checked. |
| `spend` | In DECISIONS.md's spend ledger, each running total is the previous total plus the amount, and dates never go backwards. CLAUDE.md's `Spend so far` equals the last running total. |
| `prd-sections` | Every `§n.m` in the living docs and the code names a heading of the current PRD.md, and so do both ends of a range (`§13.4-13.7`, `§10.2–10.6`, `§13.4-7`). These are skipped: DECISIONS.md, `docs/review/` and `docs/council/` (historical records numbered against older PRD versions), and a `§` within 40 characters after "Oculus", "RFC", "ISO" or "v0.1"–"v0.3". |
| `viewpoints` | Every `<park>.<view>` id in the docs that uses a manifest's park prefix is a manifest in `viewpoints/manifests/`. CLAUDE.md's viewpoint table lists exactly the manifests. |
| `notices` | `THIRD_PARTY_NOTICES` names, on one line each, every package that a lock file in the repo pins, followed by the pinned version: only spaces and `:`, `@`, `=` or `(` may sit between the name and the version, so another package's version on the same line never counts (PRD §12, the licence ledger of §15.4). Every `uv.lock`, `Cargo.lock` and `package.json` that git sees is read, so a new project is covered at once: for a uv project, its runtime closure on macOS (a marker without "or" that needs Windows or Linux leaves a package out), the closure of each extra and dependency group, and the direct members of its `dev` group; for a Cargo lock, the third-party crates each path crate depends on directly; for an npm manifest, its dependencies, devDependencies and optionalDependencies, which must pin exact versions. A bumped lock or a new dependency fails until the notices catch up. An entry for a package no lock holds any more is not caught. |
| `appendix-a` | The JSON example in PRD.md's Appendix A has the shape of `viewpoints/schema/viewpoint.schema.json`, the source of truth for the manifest (D-044). Every key is a property the schema allows (`false` properties are refused), every required key is present (`required`, and `dependentRequired` when its key is), and objects and arrays sit where the schema allows them, through `$ref`s (with the keywords beside them, which JSON Schema 2020-12 applies too), `items`, `additionalProperties` subschemas, `allOf` (every branch) and `oneOf`/`anyOf` (one branch must fit). A cycle of `$ref`s or applicators is reported, not followed. Placeholder scalars are not type-checked, and `if`/`then`/`else` and `not` are not applied. The check fails closed on what it can't read: a `$ref` other than `#` or a `#/...` pointer to a subschema of the same file (another file, a URL, an `$anchor` name, a typo); a shape keyword it does not read (`patternProperties`, `unevaluatedProperties`, `prefixItems`, `contains`, `dependencies`, `$dynamicRef`, …, or `items` as an array); and a shape constraint inside a `then`, `else` or `not` (`required`, `type`, `additionalProperties`, `items`, `$ref`, `dependentRequired`, or a `false` subschema). Scalar-only conditions, such as the real schema's `const` in a `then`, pass. If D-044 is reversed, this check goes with it. |
| `ci-suites` | CI runs every suite that `make test` runs (PRD §12). Each name in the Makefile's `TEST_SUITES` has a `test-<name>` rule, and `.github/workflows/ci.yml` runs it as `make test-<name>`, in a `run:` command (step names and comments don't count), directly or through `make test-${{ matrix.suite }}` and a matrix `suite:` entry. The exceptions are the Makefile's `LAPTOP_ONLY_SUITES` (today `speech`, which needs macOS and Xcode), which CI must then not run. CI runs no `make test-<name>` that `TEST_SUITES` lacks. |
| `data-dir-commands` | No command hard-codes the new data folder (D-080): `$HOME/snow-globe-data` (or `${HOME}`, `"$HOME"`, Make's `$(HOME)`) on any line of any text file, and `~/snow-globe-data` in a markdown command (a line of a fenced block, or an inline code span with a space in it; a bare path in backticks is a mention). Before the rename's live switch such a command would have created the folder beside `~/lookout-data`, and every data-dir resolver would then have refused to pick one (D-080). Since the switch `~/lookout-data` is a symlink to it (D-090); name `$HOME/lookout-data` or the resolved data dir, so the docs' commands keep one spelling. Kept: DECISIONS.md, `docs/evidence/`, the switch script and its test, CI's workflow (its runner has neither folder), the shell and Make resolvers' own candidate lines, and these checks. |
| `old-names` | No old name is back (D-080): `lookout` in any letter case (`lookout-*`, `lookout_*`, `LOOKOUT_*`, `LookoutSpeech`, `dev.lookout.*`) and the old C and Swift prefixes (`lk_`, `LK_`, `lkFoo`, `LKFoo`), in every text file of the repo, Swift, C and plists included (lock files are read for the product name only). [`old_names.json`](old_names.json) lists the deliberate keeps with their reasons: files kept whole (the record, recorded fixtures, hash-pinned files, dated provenance) and, for every other file, how many lines may name an old name. A new old-name line, or one fewer, fails until it is renamed or the count there changes with its reason; an entry that matches nothing fails too, so the list stays true. |

## Files

- `doccheck.py`: the checks, and a CLI that prints every problem.
- `old_names.json`: the old-names check's deliberate keeps, each with its reason.
- `ruff.toml`: lint and format settings for the three Python files here (Python 3.9, the repo's rules).
- `test_docs.py`: one test per check, run on the repo.
- `test_selftest.py`: each check passes a small consistent set of in-memory docs. A deliberate break of each invariant (one or more mutations per check) must be caught. It also covers lookalikes that must not be flagged, and the date, slug and table helpers. `Notices`, `AppendixA` and `CiSuites` cover the lock-file readers, the schema walker and the Makefile and workflow readers case by case. `GitFixtures` builds throwaway git repos in a temporary directory for what needs real git and a real disk: merges in DECISIONS.md's history, and link targets in the wrong case or ignored by git. They need the `git` CLI and are skipped without it. `RealFilesAreRead` checks that the readers find packages in every real lock file and walk the real Appendix A. Its mutations are made in memory and picked from those files, so no package, version or key is pinned and a routine bump never breaks it.

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
- **Added on 20 Nov:** `notices` and `appendix-a`, from the docs agent's proposal of the night before. Changes made when adopting them:
  - The lock files are found through git, not listed by hand, so a new project can't slip past.
  - A marker with "or" is never read as excluding a package.
  - npm ranges are reported instead of being cut down to a version.
  - Arrays are read line by line, so one-line ones parse.
  - `oneOf`/`anyOf` try every branch, and `allOf` applies them all.
  - Shape keywords the check can't read fail it.
  - The proposal's real-repo tests pinned today's versions (certifi 2026.10.23, eckitlib 2.5.0.34) and Appendix A keys, so a routine bump would have broken them. `RealFilesAreRead` picks its packages and keys from the files instead.
- **Changed on 20 Nov, after the integration review:**
  - `appendix-a` failed open in five ways, each shown with a scratch schema: a `$ref` to another file, a `$ref` to a missing target (both read as "anything"), keywords beside a `$ref` (dropped), `dependentRequired` and `$dynamicRef` (ignored). It now follows `#` and JSON pointers through arrays and escapes, applies the keywords beside a `$ref` and `dependentRequired`, reports what it can't follow or read, reports cycles instead of recursing without end, and fails on shape constraints inside `then`, `else` and `not`. The five cases are regression tests in `AppendixA`.
  - `notices` let a line pass when it held the name and, anywhere, the version, even another package's version. The version must now follow the name.
  - Added `ci-suites`, from the reviewer's scratch test: nothing checked that CI runs every suite `make test` runs.
  - Six behaviours had no test (mutants survived): `anyOf`, npm `optionalDependencies`, `prefixItems` as an unread keyword, `$defs` as a map of subschemas, markers on dev-group members, and the Cargo source in dependency resolution. Each has one now.
  - The three Python files are formatted and linted with ruff (`ruff.toml`), and `make test-docs` and CI check that.
- **Changed on 20 Nov, after the fourth workflow's docs review:**
  - DECISIONS.md gained inserted `**Amended 20 Nov (D-m):**` lines in older entries, and the review found them incomplete: four entries that new headings amended had none, and two pointers cited entries whose headings did not amend them. `decisions` now checks that pointers and headings agree in both directions, from the earliest entry a pointer cites. It read the builder's version with 6 problems, and HEAD, which has no pointers, with none.
  - A heading's list of amended entries (`amends D-030, D-049`) was checked only for its first id. Every id is now checked.
  - Five mutations and six lookalike or helper tests cover the new rule.
