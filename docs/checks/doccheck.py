"""Invariant checks for Lookout's docs (CLAUDE.md "Documents"; PRD §12 conventions, §16 milestones).

Standard library only, Python 3.9 or later, no network: `make docs-check` runs it with `python3 -I`.

Each check is a function that takes a `Repo` and returns a list of `Problem`s; an empty list means
the invariant holds. The checks test invariants, never today's wording, because PLAN.md,
DECISIONS.md, PRD_PROGRESS.md and PRD.md keep changing:

- relative links and #anchors resolve; every table row has its header's cell count;
- weekday labels match their dates; day G is defined once; G+N offsets match the dates beside them;
- the calendar's arithmetic (length = offset span, slack = the header's % of length), and each
  milestone's dates and offsets agree between PLAN.md, PRD_PROGRESS.md and PRD.md §16;
- DECISIONS.md is append-only through git history, its D-ids run 001, 002, ... without gaps, its
  headings carry a declared status and dates that never go backwards or into the future, every D-id
  after "supersedes" or "amends" points back to an earlier entry, and the "**Amended … (D-m):**"
  back-pointers inside amended entries agree with those headings;
- every D-n, ticket id and PRD § reference in the repo resolves; tickets are unique, sit under their
  milestone and carry a declared status;
- the spend ledger adds up and matches CLAUDE.md's "Spend so far";
- viewpoint ids in the docs are manifest ids, and CLAUDE.md's viewpoint table lists every manifest;
- THIRD_PARTY_NOTICES names every package the repo's lock files pin, at that version (PRD §12,
  §15.4), and PRD Appendix A's JSON example has the manifest schema's shape (D-044);
- CI (.github/workflows/ci.yml) runs every suite that `make test` runs, except the ones the
  Makefile names laptop-only (PRD §12).

`Repo` reads the working tree and git. Tests build an in-memory `Repo` instead (test_selftest.py).
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import posixpath
import re
import subprocess
import unicodedata
import urllib.parse
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

# Commits allowed to change committed lines of DECISIONS.md, with the reason. Add one only with the
# lead's sign-off (for example, scrubbing a secret or a personal detail committed by mistake).
APPEND_ONLY_EXEMPT: dict[str, str] = {}

# Historical records whose "§n" numbers refer to the PRD version current when they were written
# (DECISIONS.md is append-only; docs/review and docs/council review older drafts).
SECTION_REF_SKIP_PREFIXES = ("DECISIONS.md", "docs/review/", "docs/council/", "docs/checks/")
# A "§n" within this many characters after one of these words names another document.
OTHER_DOCUMENT_WORDS = ("Oculus", "RFC", "ISO", "v0.1", "v0.2", "v0.3")
OTHER_DOCUMENT_WINDOW = 40

TEXT_SUFFIXES = (
    ".md",
    ".py",
    ".rs",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".cjs",
    ".toml",
    ".yml",
    ".yaml",
    ".tf",
    ".hcl",
    ".sh",
    ".json",
    ".txt",
    ".cfg",
    ".ini",
)
TEXT_NAMES = ("Makefile", "Dockerfile")
MAX_TEXT_BYTES = 2_000_000
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "target",
    "dist",
    ".terraform",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".hypothesis",
    "test-results",
    "playwright-report",
}

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
FRACTIONS = {"": 0.0, "¼": 0.25, "½": 0.5, "¾": 0.75}


@dataclass(frozen=True)
class Problem:
    """One broken invariant: which check, where (path:line), and what is wrong."""

    check: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


# --------------------------------------------------------------------------------------------
# The repository view


@dataclass(frozen=True)
class FileChange:
    """One change to DECISIONS.md: a commit against one of its own parents, or HEAD against the
    working tree. `exempt` is the APPEND_ONLY_EXEMPT reason for the newer commit, or ''."""

    old_label: str
    old: str
    new_label: str
    new: str
    exempt: str = ""


class Repo:
    """Read-only view of the repo: its files, and DECISIONS.md's history.

    Built from disk and git by default. `Repo.memory(files, history)` builds one from strings, for
    tests; `history` is then the list of DECISIONS.md versions, oldest first, each one a change of
    the one before it.
    """

    def __init__(self, root: Path = REPO_ROOT) -> None:
        self.root = Path(root)
        self._files: dict[str, str] | None = None
        self._listed: list[str] | None = None
        self._index: tuple[set[str], set[str]] | None = None
        self._changes: list[FileChange] | None = None
        self.history_note = ""

    @classmethod
    def memory(cls, files: dict[str, str], history: Sequence[str] | None = None) -> Repo:
        repo = cls(Path("/nonexistent-lookout-memory-repo"))
        repo._files = dict(files)
        repo._listed = sorted(files)
        versions = [(f"v{i}", text) for i, text in enumerate(history or [])]
        if "DECISIONS.md" in files and (not versions or versions[-1][1] != files["DECISIONS.md"]):
            versions.append(("working tree", files["DECISIONS.md"]))
        repo._changes = [
            FileChange(a_label, a, b_label, b) for (a_label, a), (b_label, b) in zip(versions, versions[1:])
        ]
        return repo

    # -- files

    def _git(self, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(self.root), *args], capture_output=True, text=True, check=True, timeout=120
        )
        return out.stdout

    def paths(self) -> list[str]:
        """Repo-relative paths of tracked files plus untracked ones git does not ignore."""
        if self._listed is None:
            try:
                listed = self._git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
                names = [p for p in listed.split("\0") if p]
            except (OSError, subprocess.SubprocessError):
                names = []
                for dirpath, dirnames, filenames in os.walk(self.root):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    for name in filenames:
                        names.append(os.path.relpath(os.path.join(dirpath, name), self.root))
            self._listed = sorted({n.replace(os.sep, "/") for n in names if (self.root / n).is_file()})
        return self._listed

    def _files_and_dirs(self) -> tuple[set[str], set[str]]:
        """The repo's files (as `paths()` lists them) and every folder that holds one."""
        if self._index is None:
            files = set(self.paths())
            dirs: set[str] = set()
            for p in files:
                parts = p.split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    dirs.add("/".join(parts[:i]))
            self._index = (files, dirs)
        return self._index

    def exists(self, rel: str) -> bool:
        """True if `rel` names a file of the repo, or a folder holding one, as GitHub will see it.

        It is compared case-sensitively with `paths()` (git's tracked files plus the untracked ones
        it does not ignore), never with the disk: this laptop's APFS is case-insensitive, and a
        git-ignored file is on disk but never reaches GitHub or CI."""
        rel = rel.strip("/")
        if rel in ("", "."):
            return True
        files, dirs = self._files_and_dirs()
        return rel in files or rel in dirs

    def case_variant(self, rel: str) -> str | None:
        """The repo file or folder whose path differs from `rel` only in letter case, if any."""
        lowered = rel.strip("/").lower()
        files, dirs = self._files_and_dirs()
        return next((p for p in sorted(files | dirs) if p.lower() == lowered), None)

    def read(self, rel: str) -> str:
        if self._files is not None:
            return self._files[rel]
        # A stray non-UTF-8 byte must not stop the scan; it can't form a reference anyway.
        return (self.root / rel).read_text(encoding="utf-8", errors="replace")

    def has(self, rel: str) -> bool:
        """True if `rel` is one of `paths()`: case-sensitive, and git-ignored files don't count."""
        return rel in self._files_and_dirs()[0]

    def markdown(self) -> list[str]:
        return [p for p in self.paths() if p.endswith(".md")]

    def text_files(self) -> list[str]:
        """Text files worth scanning for references (code, configs and docs; no lock files)."""
        out = []
        for p in self.paths():
            name = posixpath.basename(p)
            if name.endswith(".lock") or name in ("pnpm-lock.yaml", "package-lock.json"):
                continue
            if not (name.endswith(TEXT_SUFFIXES) or name in TEXT_NAMES or name.startswith("Dockerfile")):
                continue
            if self._files is None:
                try:
                    if (self.root / p).stat().st_size > MAX_TEXT_BYTES:
                        continue
                except OSError:
                    continue
            out.append(p)
        return out

    # -- DECISIONS.md history

    def _decisions_at(self, rev: str, cache: dict[str, str | None]) -> str | None:
        """DECISIONS.md as commit `rev` has it, or None when that commit has no such file."""
        if rev not in cache:
            try:
                cache[rev] = self._git("show", f"{rev}:DECISIONS.md")
            except subprocess.CalledProcessError:
                cache[rev] = None
        return cache[rev]

    def decisions_changes(self) -> list[FileChange]:
        """Every change made to DECISIONS.md, oldest first: each commit that touched it against
        each of its own parents (a merge against both, so two branches that each appended are not
        compared with each other), then HEAD against the working tree when they differ.

        A parent without the file (the commit that created it) is skipped; a commit that deleted
        it compares as an empty file, so the deletion is reported. Empty, with `history_note` set,
        when git can't say. `--full-history` keeps the side branches that the default
        simplification would prune, and every merge whose file differs from one of its parents."""
        if self._changes is not None:
            return self._changes
        changes: list[FileChange] = []
        texts: dict[str, str | None] = {}
        try:
            shas = self._git("log", "--full-history", "--reverse", "--format=%H", "--", "DECISIONS.md").split()
            shallow = self._git("rev-parse", "--is-shallow-repository").strip() == "true"
            # Real parents, read without a path filter, so history simplification can't rewrite them.
            parents: dict[str, list[str]] = {}
            if shas:
                for line in self._git("show", "-s", "--format=%H %P", *shas).splitlines():
                    if line.strip():
                        sha, *ps = line.split()
                        parents[sha] = ps
            for sha in shas:
                new = self._decisions_at(sha, texts)
                label = sha[:7] + ("" if new is not None else " (deleted DECISIONS.md)")
                for parent in parents.get(sha, []):
                    old = self._decisions_at(parent, texts)
                    if old is None or old == new:
                        continue
                    changes.append(FileChange(parent[:7], old, label, new or "", exempt_reason(sha)))
            head = self._decisions_at("HEAD", texts) if shas else None
        except (OSError, subprocess.SubprocessError):
            self.history_note = "git history unavailable"
            self._changes = []
            return self._changes
        if shallow:
            self.history_note = "shallow clone: changes made at or before the oldest fetched commit unchecked"
        if head is not None and self.has("DECISIONS.md"):
            current = self.read("DECISIONS.md")
            if current != head:
                changes.append(FileChange("HEAD", head, "working tree", current))
        self._changes = changes
        return changes


def exempt_reason(sha: str) -> str:
    """The APPEND_ONLY_EXEMPT reason for commit `sha` (keys may be abbreviated), or ''."""
    for key, reason in APPEND_ONLY_EXEMPT.items():
        if len(key) >= 7 and sha.startswith(key):
            return reason
    return ""


# --------------------------------------------------------------------------------------------
# Markdown helpers


def prose_lines(text: str) -> list[str]:
    """The lines of `text`, with fenced code blocks blanked (line numbers are kept)."""
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        m = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            out.append("")
            continue
        if m:
            fence = m.group(1)
            out.append("")
            continue
        out.append(line)
    return out


def strip_inline_code(line: str) -> str:
    """`line` with inline code spans replaced by spaces of the same length."""
    return re.sub(r"(`+)(.+?)\1", lambda m: " " * len(m.group(0)), line)


def split_cells(line: str) -> list[str]:
    """GitHub table cells: split on unescaped pipes (also inside code spans), outer pipes dropped."""
    s = line.strip()
    parts = re.split(r"(?<!\\)\|", s)
    if s.startswith("|"):
        parts = parts[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        parts = parts[:-1]
    return [p.strip() for p in parts]


DELIMITER_ROW = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def tables(text: str) -> Iterator[tuple[int, list[tuple[int, str]]]]:
    """Yield (header line number, [(line number, line), ...]) for each pipe table, delimiter
    row included (it is the second entry)."""
    lines = prose_lines(text)
    i = 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and "|" in lines[i + 1] and DELIMITER_ROW.match(lines[i + 1]):
            rows = [(i + 1, lines[i]), (i + 2, lines[i + 1])]
            j = i + 2
            while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                rows.append((j + 1, lines[j]))
                j += 1
            yield i + 1, rows
            i = j
        else:
            i += 1


def heading_slug(text: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("*", "")
    out = []
    for ch in text.strip().lower():
        if ch == " ":
            out.append("-")
        elif ch in "-_" or unicodedata.category(ch)[0] in ("L", "N"):
            out.append(ch)
    return "".join(out)


def anchors(text: str) -> set[str]:
    """Every #anchor a markdown file offers: heading slugs (with GitHub's -1, -2 for repeats) and
    explicit id= or name= attributes."""
    seen: dict[str, int] = {}
    out: set[str] = set()
    for line in prose_lines(text):
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            slug = heading_slug(m.group(1))
            n = seen.get(slug, 0)
            out.add(slug if n == 0 else f"{slug}-{n}")
            seen[slug] = n + 1
        for a in re.findall(r"""<[^>]*\b(?:id|name)\s*=\s*["']([^"']+)["']""", line):
            out.add(a.lower())
    return out


# --------------------------------------------------------------------------------------------
# Dates

DATE_RE = re.compile(
    r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day|sday|nesday|rsday|urday)?\.?,? (\d{1,2})(?:st|nd|rd|th)?"
    r"(?![\d.,:°′%])"
    r"(?: (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?(?:,? (\d{4})\b)?)?"
)


@dataclass(frozen=True)
class LabelledDate:
    weekday: str
    date: dt.date | None  # None when the day doesn't exist (31 Nov)
    text: str
    year_written: bool = True  # False when the year was inferred from `near`


def labelled_dates(line: str, near: dt.date) -> list[LabelledDate]:
    """Weekday-labelled dates in `line`, in order ("Thu 19 Nov 2026", "Mon 23 Nov", "Thu 19").

    A date without a month takes the month (and year) of the next date in the line that has one,
    so "Fri 15 → Wed 20 Jan 2027" reads as two January 2027 dates. A month without a year takes the
    year that puts the date nearest `near` (the doc's anchor date, see `doc_anchor_date`). Dates
    with no month anywhere are left out.
    """
    found = list(DATE_RE.finditer(strip_inline_code(line)))
    out: list[LabelledDate] = []
    for k, m in enumerate(found):
        wd, day, mon, year = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if mon is None:
            later = next((f for f in found[k + 1 :] if f.group(3)), None)
            if later is None:
                continue
            mon, year = later.group(3), later.group(4)
        month = MONTHS.index(mon) + 1
        if year is not None:
            years = [int(year)]
        else:
            years = sorted(
                (near.year - 1, near.year, near.year + 1),
                key=lambda y: abs((_safe_date(y, month, min(day, 28)) - near).days),
            )
        out.append(LabelledDate(wd, _maybe_date(years[0], month, day), m.group(0), year is not None))
    return out


UPDATED_LINE = re.compile(r"^\s*[*_]*(?:last\s+)?updated[*_]*\s*:", re.I)


def doc_anchor_date(text: str, fallback: dt.date) -> dt.date:
    """The date a doc says it was updated, or `fallback` (day G) when it says none.

    The first line that starts '**Updated:**' (or 'Updated:', 'Last updated:') and carries a full
    date, weekday-labelled with a year ('Thu 19 Nov 2026') or ISO ('2026-11-19'), gives it. A
    date written without a year is read in the year nearest this date, so the docs of mid-2027
    read 'Mon 14 Jun' as 2027 once their Updated line moves on."""
    for line in prose_lines(text):
        if not UPDATED_LINE.match(line):
            continue
        dates = [d.date for d in labelled_dates(line, fallback) if d.year_written and d.date]
        iso = [
            _maybe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            for m in ISO_DATE.finditer(strip_inline_code(line))
        ]
        found = [d for d in dates + iso if d is not None]
        if found:
            return found[0]
    return fallback


def _safe_date(y: int, m: int, d: int) -> dt.date:
    return dt.date(y, m, d)


def _maybe_date(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


G_OFFSET = re.compile(r"(?<![\w+\-−])G(?:\s?\+\s?(\d+))?(?![\w+\-−])")
APPROX = re.compile(r"(?:about|approximately|around|roughly|before|after|~|≈)\s*$")


def g_offsets(line: str) -> list[int]:
    """Exact day-G offsets in `line` ("G", "G+4", "by G+21"); approximate or open-ended ones
    ("about G+24", "before G") are left out."""
    clean = strip_inline_code(line)
    out = []
    for m in G_OFFSET.finditer(clean):
        if APPROX.search(clean[: m.start()]):
            continue
        out.append(int(m.group(1) or 0))
    return out


def day_amount(text: str) -> float | None:
    """'3½ days' -> 3.5, '¾ day' -> 0.75, '~1¼ days' -> 1.25; None when there is no amount."""
    m = re.search(r"~?(\d*)([¼½¾]?)\s?days?\b", text)
    if not m or (not m.group(1) and not m.group(2)):
        return None
    return (int(m.group(1)) if m.group(1) else 0) + FRACTIONS[m.group(2)]


def find_day_g(repo: Repo) -> tuple[dt.date | None, list[Problem]]:
    """Day G as the docs define it: PLAN.md's "G = Thu 19 Nov 2026" or CLAUDE.md's
    "2026-11-19 (day G)". Every definition found must agree."""
    defs: list[tuple[str, dt.date]] = []
    pattern_plan = re.compile(
        r"\bG = \**(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w* )?(\d{1,2}) "
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* (\d{4})"
    )
    pattern_iso = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+\(day G\)")
    for name in ("PLAN.md", "CLAUDE.md", "PRD_PROGRESS.md", "PRD.md"):
        if not repo.has(name):
            continue
        for n, line in enumerate(repo.read(name).split("\n"), 1):
            for m in pattern_plan.finditer(line):
                d = _maybe_date(int(m.group(3)), MONTHS.index(m.group(2)) + 1, int(m.group(1)))
                if d:
                    defs.append((f"{name}:{n}", d))
            for m in pattern_iso.finditer(line):
                d = _maybe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if d:
                    defs.append((f"{name}:{n}", d))
    problems = []
    dates = {d for _, d in defs}
    if len(dates) > 1:
        listed = ", ".join(f"{w} says {d}" for w, d in defs)
        problems.append(Problem("day-g", defs[0][0], f"day G is defined differently: {listed}"))
    return (defs[0][1] if defs else None), problems


# --------------------------------------------------------------------------------------------
# Checks: links and tables


LINK = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")


def check_links(repo: Repo) -> list[Problem]:
    """Every relative link in every markdown file names a file or folder of the repo as GitHub sees
    it (exact case; a git-ignored file doesn't count, see Repo.exists), and every #anchor names a
    heading (or an id) of its target file."""
    problems = []
    anchor_cache: dict[str, set[str]] = {}
    for name in repo.markdown():
        base = posixpath.dirname(name)
        for n, line in enumerate(prose_lines(repo.read(name)), 1):
            for target in LINK.findall(strip_inline_code(line)):
                if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):  # http:, mailto:, ...
                    continue
                path, _, anchor = urllib.parse.unquote(target).partition("#")
                if path.startswith("/"):
                    problems.append(Problem("links", f"{name}:{n}", f"absolute link {target!r}"))
                    continue
                rel = posixpath.normpath(posixpath.join(base, path)) if path else name
                if rel.startswith("../") or rel == "..":
                    problems.append(Problem("links", f"{name}:{n}", f"{target!r} leaves the repo"))
                    continue
                if not repo.exists(rel):
                    variant = repo.case_variant(rel)
                    if variant:
                        why = f"{rel} differs in letter case from {variant} (GitHub and Linux CI are case-sensitive)"
                    else:
                        why = f"{rel} is not in the repo (missing, or ignored by git)"
                    problems.append(Problem("links", f"{name}:{n}", f"{target!r}: {why}"))
                    continue
                if anchor and rel.endswith(".md") and repo.has(rel):
                    if rel not in anchor_cache:
                        anchor_cache[rel] = anchors(repo.read(rel))
                    if anchor.lower() not in anchor_cache[rel]:
                        problems.append(
                            Problem("links", f"{name}:{n}", f"{target!r}: no heading or id #{anchor} in {rel}")
                        )
    return problems


def check_tables(repo: Repo) -> list[Problem]:
    """Every row of every markdown table has as many cells as its header (GitHub's rule: cells split
    on unescaped pipes, even inside code spans; write \\| for a literal pipe)."""
    problems = []
    for name in repo.markdown():
        for header_no, rows in tables(repo.read(name)):
            width = len(split_cells(rows[0][1]))
            for n, line in rows[1:]:
                cells = len(split_cells(line))
                if cells != width:
                    problems.append(
                        Problem("tables", f"{name}:{n}", f"{cells} cells; the header (line {header_no}) has {width}")
                    )
    return problems


# --------------------------------------------------------------------------------------------
# Checks: dates


def check_weekdays(repo: Repo) -> list[Problem]:
    """Every weekday-labelled date ("Mon 14 Dec") names a real day, and that day is that weekday.
    A date without a year is read in the year nearest its doc's Updated date (or day G)."""
    g, _ = find_day_g(repo)
    fallback = g or dt.date.today()
    problems = []
    for name in repo.markdown():
        text = repo.read(name)
        near = doc_anchor_date(text, fallback)
        for n, line in enumerate(prose_lines(text), 1):
            for d in labelled_dates(line, near):
                if d.date is None:
                    problems.append(Problem("weekdays", f"{name}:{n}", f"{d.text!r} is not a date"))
                elif WEEKDAYS[d.date.weekday()] != d.weekday:
                    hint = (
                        ""
                        if d.year_written
                        else (
                            f" (no year is written, so it is read in the year nearest {near:%d %b %Y}, this "
                            "doc's Updated date or day G; write the year if another is meant)"
                        )
                    )
                    problems.append(
                        Problem("weekdays", f"{name}:{n}", f"{d.text!r}: {d.date:%d %b %Y} is a {d.date:%a}{hint}")
                    )
    return problems


def check_day_g_offsets(repo: Repo) -> list[Problem]:
    """Day G is defined consistently, and wherever a line gives G+N offsets and the same number of
    weekday-labelled dates, each date is G plus its offset."""
    g, problems = find_day_g(repo)
    uses = [
        (name, n, line)
        for name in repo.markdown()
        for n, line in enumerate(prose_lines(repo.read(name)), 1)
        if g_offsets(line)
    ]
    if g is None:
        if uses:
            name, n, _ = uses[0]
            problems.append(
                Problem(
                    "day-g",
                    f"{name}:{n}",
                    "G+N offsets are used but day G is "
                    "never defined (PLAN.md 'G = <date>' or CLAUDE.md '<date> (day G)')",
                )
            )
        return problems
    for name, n, line in uses:
        offsets = g_offsets(line)
        dates = [d.date for d in labelled_dates(line, g)]
        if len(offsets) != len(dates) or None in dates:
            continue
        for k, off in enumerate(offsets):
            expected = g + dt.timedelta(days=off)
            # A date without a year is read in the year nearest the day its offset names.
            date = labelled_dates(line, expected)[k].date
            if date != expected:
                problems.append(
                    Problem(
                        "day-g",
                        f"{name}:{n}",
                        f"G+{off} is {expected:%a %d %b %Y}, but the line says {date:%a %d %b %Y}",
                    )
                )
    return problems


def _header_index(cells: Sequence[str], pattern: str) -> int | None:
    for i, cell in enumerate(cells):
        if re.search(pattern, cell, re.I):
            return i
    return None


def check_calendar(repo: Repo) -> list[Problem]:
    """In every table with a Length column: a row whose offsets read 'G+a → G+b' lasts b − a days,
    and a slack column whose header names a percentage holds that share of the length."""
    problems = []
    for name in repo.markdown():
        for _, rows in tables(repo.read(name)):
            header = split_cells(rows[0][1])
            i_len = _header_index(header, r"^length$")
            if i_len is None:
                continue
            i_slack = _header_index(header, r"slack")
            share = None
            if i_slack is not None:
                m = re.search(r"(\d+(?:\.\d+)?)\s?%", header[i_slack])
                share = float(m.group(1)) / 100 if m else None
            for n, line in rows[2:]:
                cells = split_cells(line)
                if len(cells) != len(header):
                    continue  # check_tables reports it
                length = day_amount(cells[i_len])
                if length is None:
                    continue
                if share is not None and i_slack is not None:
                    slack = day_amount(cells[i_slack])
                    if slack is None or abs(slack - share * length) > 1e-9:
                        problems.append(
                            Problem(
                                "calendar",
                                f"{name}:{n}",
                                f"slack {cells[i_slack]!r} is not {share:.0%} of {cells[i_len]!r}",
                            )
                        )
                spans = [c for c in cells if re.search(r"G(?:\+\d+)?\s*(?:→|->|–|—|to)\s*G\+\d+", c)]
                if len(spans) == 1:
                    offs = g_offsets(spans[0])
                    if len(offs) == 2 and offs[1] - offs[0] != length:
                        problems.append(
                            Problem(
                                "calendar",
                                f"{name}:{n}",
                                f"{spans[0]!r} spans {offs[1] - offs[0]} days, length says {cells[i_len]!r}",
                            )
                        )
    return problems


MILESTONE = r"(M\d+(?:\.\d+)?)"


def _milestone_sources(repo: Repo, g: dt.date) -> dict[str, dict[str, tuple[list[dt.date], list[int]]]]:
    """{milestone: {source: (weekday-labelled dates, G offsets)}} from PLAN.md's calendar rows and
    milestone headings, PRD_PROGRESS.md's milestone headings and PRD.md's §16 table rows."""
    out: dict[str, dict[str, tuple[list[dt.date], list[int]]]] = {}

    def add(ms: str, source: str, text: str, near: dt.date) -> None:
        dates = [d.date for d in labelled_dates(text, near) if d.date is not None]
        out.setdefault(ms, {})[source] = (dates, g_offsets(text))

    if repo.has("PLAN.md"):
        plan = repo.read("PLAN.md")
        near = doc_anchor_date(plan, g)
        for _, rows in tables(plan):
            header = split_cells(rows[0][1])
            if not header or not re.match(r"^milestone$", header[0], re.I):
                continue
            for n, line in rows[2:]:
                m = re.match(rf"^\s*\|?\s*{MILESTONE}\s", line)
                if m:
                    add(m.group(1), f"PLAN.md:{n} (calendar)", line, near)
        for n, line in enumerate(prose_lines(plan), 1):
            m = re.match(rf"^##\s+{MILESTONE}:", line)
            if m:
                add(m.group(1), f"PLAN.md:{n} (heading)", line, near)
    if repo.has("PRD_PROGRESS.md"):
        progress = repo.read("PRD_PROGRESS.md")
        near = doc_anchor_date(progress, g)
        for n, line in enumerate(prose_lines(progress), 1):
            m = re.match(rf"^#{{2,4}}\s+{MILESTONE}:", line)
            if m:
                add(m.group(1), f"PRD_PROGRESS.md:{n} (heading)", line, near)
    if repo.has("PRD.md"):
        prd = repo.read("PRD.md")
        near = doc_anchor_date(prd, g)
        for n, line in enumerate(prose_lines(prd), 1):
            m = re.match(rf"^\|\s*\*\*{MILESTONE}\s", line)
            if m:
                # Only the first cell: later cells may mention other milestones' dates.
                add(m.group(1), f"PRD.md:{n} (§16 table)", split_cells(line)[0], near)
    return out


def check_milestone_dates(repo: Repo) -> list[Problem]:
    """Each milestone's dates agree wherever they appear (PLAN.md calendar and heading,
    PRD_PROGRESS.md heading), and so do its G offsets (PLAN.md calendar, PRD.md §16 table)."""
    g, _ = find_day_g(repo)
    if g is None:
        return []
    problems = []
    for ms, sources in sorted(_milestone_sources(repo, g).items()):
        dated = [(s, d) for s, (d, _) in sources.items() if d]
        offset = [(s, o) for s, (_, o) in sources.items() if o]
        for kind, items, fmt in (
            ("dates", dated, lambda v: ", ".join(f"{x:%a %d %b %Y}" for x in v)),
            ("G offsets", offset, lambda v: ", ".join(f"G+{x}" for x in v)),
        ):
            if len({tuple(v) for _, v in items}) > 1:
                listed = "; ".join(f"{s}: {fmt(v)}" for s, v in items)
                problems.append(Problem("milestones", items[0][0], f"{ms} {kind} disagree: {listed}"))
    return problems


# --------------------------------------------------------------------------------------------
# Checks: DECISIONS.md

D_HEADING = re.compile(r"^## (D-(\d{3})) — (.+)$")
D_REF = re.compile(r"(?<![\w-])D-(\d{3})(?!\d)")
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def declared_statuses(text: str, label: str) -> list[str]:
    """Bold words declared on a line like '**Status:** **decided** (...) · **proposed** ...', or
    in a bullet list under a '**Status values:**' line ('- **done:** ...')."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if re.match(rf"^\s*\*\*{re.escape(label)}:?\*\*:?", line):
            inline = re.findall(r"\*\*([^*:]+?):?\*\*", line)[1:]
            if inline:
                return [w.strip().lower() for w in inline]
            items = []
            for follow in lines[i + 1 :]:
                m = re.match(r"^\s*[-*]\s+\*\*([^*:]+?)[:.]?\*\*", follow)
                if not m:
                    if follow.strip():
                        break
                    continue
                items.append(m.group(1).strip().lower())
            return items
    return []


def _decision_headings(text: str) -> list[tuple[int, str, int, list[str]]]:
    """(line number, D-id, number, ' · '-separated parts after the id) for each D heading."""
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        m = D_HEADING.match(line)
        if m:
            out.append((n, m.group(1), int(m.group(2)), [p.strip() for p in m.group(3).split(" · ")]))
    return out


AMEND_VERB = re.compile(r"\b(supersedes|amends|closes|reverses|replaces)\b")
# A back-pointer inside an amended entry: '- **Amended 20 Nov (D-070):** ...', citing one or more
# later entries ('D-067, D-074', 'D-070 to D-072').
AMENDED_POINTER = re.compile(r"^\s*[-*]\s+\*\*Amended\b[^()*]*\((D-\d{3}[^)]*)\):?\*\*")
D_SPAN = re.compile(r"D-(\d{3})(?:\s+to\s+D-(\d{3}))?")


def heading_targets(parts: Sequence[str]) -> list[tuple[str, int]]:
    """(verb, D number) for every entry a D heading names after 'supersedes', 'amends', 'closes',
    'reverses' or 'replaces': each D-id after the verb, up to the next verb or the end of its
    ' · ' part ('amends D-023, D-048'; 'closes D-019's open part'). Only the parts after the title
    and the status are read, so a D-id in either of those is never a target."""
    out = []
    for part in parts[2:]:
        pieces = AMEND_VERB.split(part)  # [before, verb, after, verb, after, ...]
        for i in range(1, len(pieces) - 1, 2):
            out.extend((pieces[i], int(ref)) for ref in re.findall(r"D-(\d{3})", pieces[i + 1]))
    return out


def pointer_citations(line: str) -> list[int] | None:
    """The D numbers an '**Amended … (D-a, D-b to D-c):**' line cites, ranges expanded; None when
    the line is not such a pointer."""
    m = AMENDED_POINTER.match(line)
    if not m:
        return None
    cited: list[int] = []
    for low, high in D_SPAN.findall(m.group(1)):
        cited.extend(range(int(low), int(high or low) + 1))
    return cited


def check_decision_pointers(text: str) -> list[Problem]:
    """Back-pointers in DECISIONS.md agree with the headings that amend.

    An '**Amended <date> (D-m):**' line inside entry D-n cites only later entries whose headings
    name D-n after supersedes, amends, closes, reverses or replaces. Once pointers exist, the earliest
    entry any of them cites starts the convention: from it on, every entry a heading names that way
    carries a pointer back to that heading's entry. Entries before it need none, and a file with no
    pointer at all requires none, so dropping every pointer is as consistent as keeping all of them."""
    headings = _decision_headings(text)
    if not headings:
        return []
    lines = text.split("\n")
    targets = {num: heading_targets(parts) for _, _, num, parts in headings}
    where = {num: f"DECISIONS.md:{n}" for n, _, num, _ in headings}
    bounds = [n for n, *_ in headings] + [len(lines) + 1]
    cited_by: dict[int, set[int]] = {}  # entry -> the entries its pointers cite and that amend it
    problems = []
    for i, (n, did, num, _) in enumerate(headings):
        cited_by[num] = set()
        for k in range(n + 1, bounds[i + 1]):
            line = lines[k - 1]
            if line.startswith("## "):  # the spend ledger, or any other H2 after the last entry
                break
            for ref in pointer_citations(line) or []:
                at = f"DECISIONS.md:{k}"
                if ref not in targets:
                    problems.append(
                        Problem("decisions", at, f"{did}'s pointer cites D-{ref:03d}, which is not an entry")
                    )
                elif ref <= num:
                    problems.append(
                        Problem("decisions", at, f"{did}'s pointer cites D-{ref:03d}, which is not a later entry")
                    )
                elif num not in {t for _, t in targets[ref]}:
                    problems.append(
                        Problem(
                            "decisions",
                            at,
                            f"{did}'s pointer cites D-{ref:03d}, whose heading does not name {did} after "
                            "supersedes, amends, closes, reverses or replaces",
                        )
                    )
                else:
                    cited_by[num].add(ref)
    every = [ref for refs in cited_by.values() for ref in refs]
    if not every:
        return problems
    start = min(every)
    for num in sorted(targets):
        if num < start:
            continue
        for verb, ref in targets[num]:
            if ref in cited_by and ref < num and num not in cited_by[ref]:
                problems.append(
                    Problem(
                        "decisions",
                        where[ref],
                        f"D-{num:03d} {verb} D-{ref:03d}, but D-{ref:03d} has no '**Amended … (D-{num:03d}):**' "
                        f"line pointing to it (pointers run from D-{start:03d} on)",
                    )
                )
    return problems


def check_decisions(repo: Repo, today: dt.date | None = None) -> list[Problem]:
    """DECISIONS.md: D-ids run D-001, D-002, ... in order; every heading reads
    '## D-NNN — title · status · ...' with a declared status word; heading dates are real, never
    go backwards and are not in the future; every D-id after 'supersedes/amends/closes' points back;
    and the '**Amended … (D-m):**' back-pointers agree with those headings
    (`check_decision_pointers`)."""
    if not repo.has("DECISIONS.md"):
        return [Problem("decisions", "DECISIONS.md", "missing")]
    text = repo.read("DECISIONS.md")
    today = today or dt.date.today()
    problems = []
    statuses = declared_statuses(text, "Status")
    if not statuses:
        problems.append(Problem("decisions", "DECISIONS.md", "no '**Status:**' line declares the status words"))
    for n, line in enumerate(text.split("\n"), 1):
        if line.startswith("## D-") and not D_HEADING.match(line):
            problems.append(
                Problem("decisions", f"DECISIONS.md:{n}", "a D heading must read '## D-NNN — title · status · date'")
            )
    headings = _decision_headings(text)
    numbers = [num for _, _, num, _ in headings]
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append(
            Problem(
                "decisions",
                "DECISIONS.md",
                f"D-ids are not D-001..D-{len(numbers):03d} in order: " + ", ".join(f"D-{x:03d}" for x in numbers),
            )
        )
    last_date: dt.date | None = None
    for n, did, num, parts in headings:
        where = f"DECISIONS.md:{n}"
        if len(parts) < 2:
            problems.append(Problem("decisions", where, f"{did} has no ' · status' part"))
        elif statuses and not any(re.search(rf"\b{re.escape(s)}\b", parts[1], re.I) for s in statuses):
            problems.append(
                Problem("decisions", where, f"{did} status {parts[1]!r} uses none of: {', '.join(statuses)}")
            )
        # The entry's date is the first ISO date after the status; every date must be real.
        own: dt.date | None = None
        for part in parts[2:]:
            for m in ISO_DATE.finditer(part):
                d = _maybe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if d is None:
                    problems.append(Problem("decisions", where, f"{did}: {m.group(0)} is not a date"))
                elif own is None:
                    own = d
        if own is not None:
            if own > today + dt.timedelta(days=1):
                problems.append(Problem("decisions", where, f"{did} is dated {own}, in the future"))
            if last_date is not None and own < last_date:
                problems.append(
                    Problem("decisions", where, f"{did} is dated {own}, before the entry above it ({last_date})")
                )
            last_date = own
        for verb, ref in heading_targets(parts):
            if ref >= num:
                problems.append(Problem("decisions", where, f"{did} {verb} D-{ref:03d}, which is not an earlier entry"))
    problems.extend(check_decision_pointers(text))
    return problems


def check_decisions_append_only(repo: Repo) -> list[Problem]:
    """No committed line of DECISIONS.md is ever changed or removed: every commit that touched it,
    compared with each of its own parents (both parents for a merge), and the working tree,
    compared with HEAD, only insert lines. Commits in APPEND_ONLY_EXEMPT are skipped."""
    problems = []
    for change in repo.decisions_changes():
        if change.exempt:
            continue
        a, b = change.old.split("\n"), change.new.split("\n")
        for op, i1, _i2, _j1, _j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if op in ("equal", "insert"):
                continue
            first = a[i1] if i1 < len(a) else ""
            problems.append(
                Problem(
                    "append-only",
                    f"DECISIONS.md ({change.old_label} -> {change.new_label})",
                    f"{op}s committed line {i1 + 1}: {first[:80]!r}",
                )
            )
    return problems


def check_decision_refs(repo: Repo) -> list[Problem]:
    """Every 'D-NNN' cited anywhere in the repo (docs and code) is a DECISIONS.md entry."""
    if not repo.has("DECISIONS.md"):
        return []
    known = {did for _, did, _, _ in _decision_headings(repo.read("DECISIONS.md"))}
    problems = []
    for name in repo.text_files():
        if name.startswith("docs/checks/"):
            continue
        for n, line in enumerate(repo.read(name).split("\n"), 1):
            for num in D_REF.findall(line):
                if f"D-{num}" not in known:
                    problems.append(Problem("d-refs", f"{name}:{n}", f"D-{num} is not in DECISIONS.md"))
    return problems


# --------------------------------------------------------------------------------------------
# Checks: tickets, spend, PRD sections, viewpoints

TICKET_ROW = re.compile(r"^\|\s*((?:M\d+(?:\.\d+)?|v\d+)-\d+)\b")
TICKET_REF = re.compile(r"(?<![\w.-])(M\d+(?:\.\d+)?-\d{1,2})(?![\d\w])")


def is_ticket_reference(tid: str, digits: dict[str, set[int]]) -> bool:
    """Whether 'M<a>-<n>' cited in a doc means a ticket, given the digit counts each milestone's
    ticket numbers use in PRD_PROGRESS.md (`digits`: M0 -> {1}, M1 -> {2}, ...).

    A number with another digit count than its milestone's tickets ('M4-5' while M4's tickets are
    M4-01, M4-02, ...) reads as a milestone range, M4 to M5, and so does a one-digit number for a
    milestone that has no tickets yet. Anything else is a ticket reference and must exist."""
    milestone, number = tid.rsplit("-", 1)
    if milestone in digits:
        return len(number) in digits[milestone]
    return len(number) > 1


def check_tickets(repo: Repo) -> list[Problem]:
    """PRD_PROGRESS.md: ticket ids are unique, each sits under its own milestone's heading and starts
    its status with a declared status value; every ticket id cited in the repo exists (a range such
    as 'M4-5' is not a ticket id, see `is_ticket_reference`)."""
    if not repo.has("PRD_PROGRESS.md"):
        return []
    text = repo.read("PRD_PROGRESS.md")
    statuses = declared_statuses(text, "Status values")
    problems = []
    if not statuses:
        problems.append(Problem("tickets", "PRD_PROGRESS.md", "no '**Status values:**' list declares the statuses"))
    seen: dict[str, int] = {}
    section: str | None = None
    status_col: int | None = None
    lines = prose_lines(text)
    for n, line in enumerate(lines, 1):
        h = re.match(r"^#{2,4}\s+(M\d+(?:\.\d+)?|v\d+)\b", line)
        if h:
            section = h.group(1)
            continue
        if line.startswith("#"):
            section = None
            continue
        if line.startswith("|") and n < len(lines) and DELIMITER_ROW.match(lines[n]):
            status_col = _header_index(split_cells(line), r"^status$")
            continue
        m = TICKET_ROW.match(line)
        if not m:
            continue
        tid = m.group(1)
        where = f"PRD_PROGRESS.md:{n}"
        if tid in seen:
            problems.append(Problem("tickets", where, f"{tid} also on line {seen[tid]}"))
        seen.setdefault(tid, n)
        prefix = tid.rsplit("-", 1)[0]
        if section is not None and prefix != section:
            problems.append(Problem("tickets", where, f"{tid} sits under the {section} heading"))
        if statuses and status_col is not None:
            cells = split_cells(line)
            status = cells[status_col].lower() if status_col < len(cells) else ""
            if not any(status.startswith(s) for s in statuses):
                problems.append(
                    Problem(
                        "tickets", where, f"{tid} status {status[:40]!r} starts with none of: {', '.join(statuses)}"
                    )
                )
    digits: dict[str, set[int]] = {}
    for tid in seen:
        milestone, number = tid.rsplit("-", 1)
        digits.setdefault(milestone, set()).add(len(number))
    for name in repo.text_files():
        if name.startswith("docs/checks/"):
            continue
        for n, line in enumerate(repo.read(name).split("\n"), 1):
            for tid in TICKET_REF.findall(line):
                if tid not in seen and is_ticket_reference(tid, digits):
                    problems.append(Problem("tickets", f"{name}:{n}", f"ticket {tid} is not in PRD_PROGRESS.md"))
    return problems


MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")


def _cents(text: str) -> int | None:
    m = MONEY.search(text)
    if not m:
        return None
    return round(float(m.group(1).replace(",", "")) * 100)


def check_spend_ledger(repo: Repo) -> list[Problem]:
    """DECISIONS.md's spend ledger: each running total is the previous one plus the amount, dates
    never go backwards, and CLAUDE.md's 'Spend so far' equals the last running total."""
    if not repo.has("DECISIONS.md"):
        return []
    text = repo.read("DECISIONS.md")
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if re.match(r"^##\s+Spend ledger", line, re.I)), None)
    if start is None:
        return [Problem("spend", "DECISIONS.md", "no '## Spend ledger' section")]
    body = "\n".join(lines[start:])
    problems = []
    total: int | None = None
    last_date: str | None = None
    for _header_no, rows in tables(body):
        header = split_cells(rows[0][1])
        i_amount = _header_index(header, r"^amount$")
        i_total = _header_index(header, r"running total")
        i_date = _header_index(header, r"^date$")
        if i_amount is None or i_total is None:
            continue
        running = 0
        for n, line in rows[2:]:
            where = f"DECISIONS.md:{start + n}"
            cells = split_cells(line)
            if len(cells) != len(header):
                continue
            amount, stated = _cents(cells[i_amount]), _cents(cells[i_total])
            if amount is None or stated is None:
                problems.append(Problem("spend", where, "amount or running total is not a $ figure"))
                continue
            running += amount
            if stated != running:
                problems.append(
                    Problem("spend", where, f"running total {cells[i_total]} should be ${running / 100:,.2f}")
                )
                running = stated
            if i_date is not None:
                m = ISO_DATE.search(cells[i_date])
                if m:
                    if last_date is not None and m.group(0) < last_date:
                        problems.append(Problem("spend", where, f"date {m.group(0)} is before {last_date}"))
                    last_date = m.group(0)
            total = running
        break
    if total is None:
        problems.append(Problem("spend", "DECISIONS.md", "the spend ledger has no Amount / Running total table"))
    elif repo.has("CLAUDE.md"):
        for n, line in enumerate(repo.read("CLAUDE.md").split("\n"), 1):
            m = re.search(r"Spend so far:\s*(\$\s?\d[\d,]*(?:\.\d+)?)", line)
            if m and _cents(m.group(1)) != total:
                problems.append(
                    Problem(
                        "spend",
                        f"CLAUDE.md:{n}",
                        f"'Spend so far: {m.group(1)}' but the ledger's running total is ${total / 100:,.2f}",
                    )
                )
    return problems


# "§13.4", and ranges: "§13.4-13.7", "§10.2–10.6", "§10.2–§10.6", "§13.4-7" (= §13.4 to §13.7).
SECTION_REF = re.compile(r"§\s?(\d+(?:\.\d+)*)(?:[-–]§?(\d+(?:\.\d+)*))?")


def section_range_end(start: str, end: str) -> str:
    """The full number of a range's end: '13.4', '13.7' -> '13.7'; '13.4', '7' -> '13.7'."""
    head, tail = start.split("."), end.split(".")
    if len(tail) >= len(head):
        return end
    return ".".join(head[: len(head) - len(tail)] + tail)


def prd_sections(text: str) -> set[str]:
    """Section numbers of PRD.md's headings ('## 8. Data' -> '8', '### 8.7 Storage' -> '8.7')."""
    return {m.group(1) for m in re.finditer(r"^#{1,6}\s+(\d+(?:\.\d+)*)\.?\s", text, re.M)}


def check_prd_sections(repo: Repo) -> list[Problem]:
    """Every '§n.m' in the living docs and the code names a section of the current PRD.md.

    Left out: historical records (DECISIONS.md, docs/review/, docs/council/), whose numbers are
    those of the PRD version they reviewed, and a '§' shortly after a word naming another document
    (OTHER_DOCUMENT_WORDS, e.g. "Oculus PRD §13.6", "v0.3 §4")."""
    if not repo.has("PRD.md"):
        return []
    known = prd_sections(repo.read("PRD.md"))
    problems = []
    for name in repo.text_files():
        if name.startswith(SECTION_REF_SKIP_PREFIXES):
            continue
        for n, line in enumerate(repo.read(name).split("\n"), 1):
            for m in SECTION_REF.finditer(line):
                refs = [m.group(1)]
                if m.group(2):
                    refs.append(section_range_end(m.group(1), m.group(2)))
                unknown = [ref for ref in refs if ref not in known]
                if not unknown:
                    continue
                before = line[max(0, m.start() - OTHER_DOCUMENT_WINDOW) : m.start()]
                if any(word in before for word in OTHER_DOCUMENT_WORDS):
                    continue
                for ref in unknown:
                    problems.append(Problem("prd-sections", f"{name}:{n}", f"§{ref} is not a section of PRD.md"))
    return problems


def manifest_ids(repo: Repo) -> list[str]:
    return sorted(
        posixpath.basename(p)[:-5]
        for p in repo.paths()
        if p.startswith("viewpoints/manifests/") and p.endswith(".json")
    )


def check_viewpoint_ids(repo: Repo) -> list[Problem]:
    """Every viewpoint id in the docs ('<park>.<view>' with a manifest's park prefix) is a manifest in
    viewpoints/manifests/, and CLAUDE.md's viewpoint table lists exactly the manifests."""
    ids = manifest_ids(repo)
    if not ids:
        return []
    prefixes = sorted({i.split(".", 1)[0] for i in ids}, key=len, reverse=True)
    token = re.compile(rf"(?<![\w./-])((?:{'|'.join(map(re.escape, prefixes))})\.[a-z0-9]+(?:-[a-z0-9]+)*)(?![\w-])")
    problems = []
    for name in repo.markdown():
        if name.startswith("docs/checks/"):
            continue
        for n, line in enumerate(prose_lines(repo.read(name)), 1):
            for vid in token.findall(line):
                if vid not in ids:
                    problems.append(
                        Problem("viewpoints", f"{name}:{n}", f"{vid} is not a manifest in viewpoints/manifests/")
                    )
    if repo.has("CLAUDE.md"):
        for header_no, rows in tables(repo.read("CLAUDE.md")):
            header = split_cells(rows[0][1])
            if not header or "viewpoint_id" not in header[0]:
                continue
            listed = sorted(re.sub(r"[`*]", "", split_cells(line)[0]) for _, line in rows[2:])
            if listed != ids:
                problems.append(
                    Problem(
                        "viewpoints",
                        f"CLAUDE.md:{header_no}",
                        f"the viewpoint table lists {listed}, the manifests are {ids}",
                    )
                )
    return problems


# --------------------------------------------------------------------------------------------
# THIRD_PARTY_NOTICES against the lock files (PRD §12, §15.4). The lock files are read with
# regular expressions, because tomllib needs Python 3.11 and these checks run on 3.9.

NOTICES = "THIRD_PARTY_NOTICES"
# uv dependency groups of tools that never ship: their direct members are named, their own
# dependencies are not.
NOTICES_DEV_GROUPS = ("dev",)
# uv.lock source kinds of the project itself and of its path dependencies: not third party.
UV_LOCAL_SOURCES = ("editable", "directory", "virtual", "path")
# A dependency marker without "or" that requires one of these can hold only on another OS than
# macOS, where the notices were written (spaces removed before matching).
OTHER_OS_MARKERS = (
    "sys_platform=='win32'",
    "sys_platform=='cygwin'",
    "platform_system=='Windows'",
    "os_name=='nt'",
    "sys_platform=='linux'",
    "platform_system=='Linux'",
)
# An npm version spec that pins exactly one version: '7.0.0', '=7.0.0', 'v1.2.3-rc.1'.
NPM_EXACT = re.compile(r"^=?v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$")
NPM_FIELDS = ("dependencies", "devDependencies", "optionalDependencies")

LOCK_PACKAGE = re.compile(r"(?m)^\[\[package\]\][ \t]*$")
LOCK_SUBTABLE = re.compile(r"(?m)^\[package\.([A-Za-z0-9_-]+)\][ \t]*$")
TOML_ARRAY_START = re.compile(r"^([A-Za-z0-9_-]+) = \[(.*)$")

# (name, version, why the notices must name it)
Requirement = tuple[str, str, str]
UvEntry = tuple[str, Optional[str], tuple[str, ...], str]


def _toml_string(table: str, key: str) -> str | None:
    m = re.search(rf'(?m)^{re.escape(key)} = "([^"]*)"', table)
    return m.group(1) if m else None


def toml_arrays(table: str) -> dict[str, str]:
    """The `key = [ ... ]` arrays of one lock-file table, by key, as raw text: a one-line array, or
    a multi-line one closed by a line that is just `]` (the way uv and Cargo write them)."""
    out: dict[str, str] = {}
    lines = table.split("\n")
    i = 0
    while i < len(lines):
        m = TOML_ARRAY_START.match(lines[i])
        i += 1
        if not m:
            continue
        rest = m.group(2).rstrip()
        if rest.endswith("]"):
            out[m.group(1)] = rest[:-1]
            continue
        body = [rest]
        while i < len(lines) and lines[i].strip() != "]":
            body.append(lines[i])
            i += 1
        i += 1  # the closing `]`
        out[m.group(1)] = "\n".join(body)
    return out


def _uv_entries(body: str) -> list[UvEntry]:
    """(name, version or None, extras, marker) of each `{ name = ... }` entry of a uv.lock array."""
    out: list[UvEntry] = []
    for m in re.finditer(r"\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", body):
        entry = m.group(1)
        name = re.search(r'(?<![\w-])name = "([^"]+)"', entry)
        if not name:
            continue
        version = re.search(r'(?<![\w-])version = "([^"]+)"', entry)
        extras = re.search(r"(?<![\w-])extra = \[([^\]]*)\]", entry)
        marker = re.search(r'(?<![\w-])marker = "([^"]*)"', entry)
        out.append(
            (
                name.group(1),
                version.group(1) if version else None,
                tuple(re.findall(r'"([^"]+)"', extras.group(1))) if extras else (),
                marker.group(1) if marker else "",
            )
        )
    return out


def parse_uv_lock(text: str) -> list[dict[str, Any]]:
    """Each [[package]] of a uv.lock: name, version, source kind, whether it is the project itself
    (source "."), its dependencies, and its optional-dependencies and dev-dependencies by group."""
    packages = []
    for block in LOCK_PACKAGE.split(text)[1:]:
        parts = LOCK_SUBTABLE.split(block)
        main, subs = parts[0], dict(zip(parts[1::2], parts[2::2]))
        source = re.search(r'(?m)^source = \{ ?([A-Za-z]+) = "([^"]*)"', main)
        packages.append(
            {
                "name": _toml_string(main, "name"),
                "version": _toml_string(main, "version"),
                "source": source.group(1) if source else "",
                "root": bool(source) and source.group(2) == ".",
                "deps": _uv_entries(toml_arrays(main).get("dependencies", "")),
                "optional": {g: _uv_entries(b) for g, b in toml_arrays(subs.get("optional-dependencies", "")).items()},
                "dev": {g: _uv_entries(b) for g, b in toml_arrays(subs.get("dev-dependencies", "")).items()},
            }
        )
    return packages


def marker_applies(marker: str) -> bool:
    """Whether a uv.lock dependency marker can hold on macOS. Only a marker without "or" that
    requires another OS (OTHER_OS_MARKERS) is ruled out. Anything else counts as applying, so a
    marker this reading gets wrong can only ask for one notice too many, never drop one."""
    if re.search(r"\bor\b", marker):
        return True
    compact = marker.replace(" ", "").replace('"', "'")
    return not any(token in compact for token in OTHER_OS_MARKERS)


def uv_required(text: str) -> tuple[list[Requirement], list[str]]:
    """What THIRD_PARTY_NOTICES must name for one uv.lock, and the lock's resolution errors.

    The project's runtime closure on macOS ('runtime'); the closure of each of its extras ('extra
    <name>') and dependency groups ('group <name>'); and only the direct members of the groups in
    NOTICES_DEV_GROUPS ('group dev, direct'). The project and its path dependencies are not third
    party, but their dependencies are followed."""
    packages = parse_uv_lock(text)
    roots = [p for p in packages if p["root"]]
    if len(roots) != 1:
        return [], [f'expected one project package (source "."), found {len(roots)}']
    root = roots[0]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for p in packages:
        by_name.setdefault(p["name"], []).append(p)
    third_party = {(p["name"], p["version"]) for p in packages if p["source"] not in UV_LOCAL_SOURCES}
    errors: list[str] = []

    def resolve(name: str, version: str | None) -> dict[str, Any] | None:
        found = [p for p in by_name.get(name, []) if version is None or p["version"] == version]
        if len(found) != 1:
            errors.append(f"cannot resolve {name} {version or ''}".rstrip())
            return None
        return found[0]

    def closure(entries: Sequence[UvEntry]) -> list[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        expanded: set[tuple[str, str, str]] = set()
        stack = list(entries)
        while stack:
            name, version, extras, marker = stack.pop()
            if not marker_applies(marker):
                continue
            pkg = resolve(name, version)
            if pkg is None:
                continue
            key = (pkg["name"], pkg["version"])
            if key not in seen:
                seen.add(key)
                stack.extend(pkg["deps"])
            for extra in extras:
                if (key[0], key[1], extra) not in expanded:
                    expanded.add((key[0], key[1], extra))
                    stack.extend(pkg["optional"].get(extra, []))
        return sorted(k for k in seen if k in third_party)

    required: list[Requirement] = [(n, v, "runtime") for n, v in closure(root["deps"])]
    for group, entries in sorted(root["optional"].items()):
        required += [(n, v, f"extra {group}") for n, v in closure(entries)]
    for group, entries in sorted(root["dev"].items()):
        if group not in NOTICES_DEV_GROUPS:
            required += [(n, v, f"group {group}") for n, v in closure(entries)]
            continue
        for name, version, _extras, marker in entries:
            pkg = resolve(name, version) if marker_applies(marker) else None
            if pkg is not None and (pkg["name"], pkg["version"]) in third_party:
                required.append((pkg["name"], pkg["version"], f"group {group}, direct"))
    return required, list(dict.fromkeys(errors))


def parse_cargo_lock(text: str) -> list[dict[str, Any]]:
    """Each [[package]] of a Cargo.lock: name, version, source ('' for a path crate), dependencies."""
    out = []
    for block in LOCK_PACKAGE.split(text)[1:]:
        deps = re.findall(r'"([^"]+)"', toml_arrays(block).get("dependencies", ""))
        out.append(
            {
                "name": _toml_string(block, "name"),
                "version": _toml_string(block, "version"),
                "source": _toml_string(block, "source") or "",
                "deps": deps,
            }
        )
    return out


def cargo_required(text: str) -> tuple[list[Requirement], list[str]]:
    """What THIRD_PARTY_NOTICES must name for one Cargo.lock: every third-party crate (one with a
    source) that a path crate depends on directly ('crate <path crate>'). Cargo.lock lists normal,
    build and dev dependencies alike; a dependency is written 'name', 'name version' or
    'name version (source)'."""
    packages = parse_cargo_lock(text)
    errors: list[str] = []
    required: set[Requirement] = set()
    for p in packages:
        if p["source"]:
            continue
        for dep in p["deps"]:
            name, version, source = ([*dep.split(" ", 2), None, None])[:3]
            found = [
                q
                for q in packages
                if q["name"] == name
                and (version is None or q["version"] == version)
                and (source is None or q["source"] == source.strip("()"))
            ]
            if len(found) != 1:
                errors.append(f"{p['name']}: cannot resolve dependency {dep!r}")
            elif found[0]["source"]:
                required.add((found[0]["name"], found[0]["version"], f"crate {p['name']}"))
    return sorted(required), errors


def npm_required(text: str) -> tuple[list[Requirement], list[str]]:
    """What THIRD_PARTY_NOTICES must name for one package.json: every entry of its NPM_FIELDS at
    the one version it pins ('npm <field>'). A range such as '^1.2.0' is reported instead, because
    the manifest alone can't say which version is locked."""
    try:
        manifest = json.loads(text)
    except ValueError as exc:
        return [], [f"not JSON: {exc}"]
    if not isinstance(manifest, dict):
        return [], ["not a JSON object"]
    required: list[Requirement] = []
    errors: list[str] = []
    for field in NPM_FIELDS:
        for name, spec in sorted((manifest.get(field) or {}).items()):
            m = NPM_EXACT.match(str(spec).strip())
            if m:
                required.append((name, m.group(1), f"npm {field}"))
            else:
                errors.append(
                    f"{name} {spec!r} ({field}) is not an exact version, so the locked "
                    "version can't be read from the manifest; pin it"
                )
    return required, errors


NOTICES_READERS: dict[str, Callable[[str], tuple[list[Requirement], list[str]]]] = {
    "uv.lock": uv_required,
    "Cargo.lock": cargo_required,
    "package.json": npm_required,
}


def notices_sources(repo: Repo) -> list[str]:
    """Every uv.lock, Cargo.lock and package.json of the repo as git sees it (`paths()`), so a new
    project is covered at once and nothing under an ignored node_modules/ ever is."""
    return [p for p in repo.paths() if posixpath.basename(p) in NOTICES_READERS]


def named_with_version(lines: Sequence[str], name: str, version: str) -> bool:
    """True if one of `lines` names `name` (in any letter case, not inside a longer name such as
    '@types/name' or 'name-extra') followed by `version` (not inside a longer version), with only
    spaces and the separators ':', '@', '=' and '(' between them. So another package's version on
    the same line ('foo 1.16.0 (pulls in six)') never counts for this one."""
    # The version is not the start of '1.2.30', '1.2.3.4' or '1.2.3-rc1'; a full stop may follow.
    pair = re.compile(rf"(?<![\w@/.-]){re.escape(name)}[\s:@=(]+{re.escape(version)}(?!\w|[.-]\w)", re.I)
    return any(pair.search(line) for line in lines)


def check_notices(repo: Repo) -> list[Problem]:
    """THIRD_PARTY_NOTICES names, on one line each, every package the repo's lock files pin, at the
    pinned version (PRD §12 and the licence ledger of §15.4); uv_required, cargo_required and
    npm_required say which. A bumped lock or a new dependency fails until the notices catch up. An
    entry for a package that no lock holds any more is not caught."""
    sources = notices_sources(repo)
    if not sources:
        return []
    if not repo.has(NOTICES):
        return [Problem("notices", NOTICES, f"missing, and {len(sources)} lock file(s) need it: " + ", ".join(sources))]
    lines = repo.read(NOTICES).split("\n")
    problems: list[Problem] = []
    for path in sources:
        required, errors = NOTICES_READERS[posixpath.basename(path)](repo.read(path))
        problems += [Problem("notices", path, e) for e in errors]
        for name, version, why in required:
            if not named_with_version(lines, name, version):
                problems.append(
                    Problem(
                        "notices",
                        NOTICES,
                        f"{name} {version} ({path}, {why}) is not named with its version on one line",
                    )
                )
    return problems


# --------------------------------------------------------------------------------------------
# PRD Appendix A against the viewpoint manifest schema (D-044)

MANIFEST_SCHEMA = "viewpoints/schema/viewpoint.schema.json"
# Keywords that admit, forbid or reshape keys or items, which schema_shape_problems does not read.
# A schema that uses one fails the check until the check learns it, instead of passing unread
# (schema_reading_problems). `items` written as an array (the tuple form before 2020-12) counts too.
UNREAD_SHAPE_KEYWORDS = (
    "patternProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "prefixItems",
    "additionalItems",
    "contains",
    "dependentSchemas",
    "dependencies",
    "propertyNames",
    "$dynamicRef",
    "$recursiveRef",
)
# Subschemas that schema_shape_problems does not apply: whether `if` holds depends on scalar values,
# which are placeholders in Appendix A, and `not` is not inverted. A shape constraint inside one of
# them (one of SHAPE_KEYWORDS, or a `false` subschema) fails the check; scalar-only ones, such as
# a `const` in a `then`, do not.
UNAPPLIED_KEYWORDS = ("then", "else", "not")
SHAPE_KEYWORDS = ("$ref", "type", "required", "dependentRequired", "additionalProperties", "items")
# Keywords whose values are JSON instances (or, for dependentRequired, lists of property names),
# never subschemas: the scans do not look inside them.
INSTANCE_KEYWORDS = ("const", "enum", "default", "examples", "dependentRequired")
# Keywords whose values map names to subschemas.
SCHEMA_MAP_KEYWORDS = ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas")
# What _ref_target returns for a $ref it can't follow.
UNRESOLVED: Any = object()


def appendix_a_example(prd: str) -> tuple[int, str]:
    """(line number of its first line, text) of the first ```json block under '## Appendix A',
    before the next '## ' heading; (0, '') if there is none."""
    lines = prd.split("\n")
    start = next((i for i, text in enumerate(lines) if re.match(r"^## Appendix A\b", text)), None)
    if start is None:
        return 0, ""
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            break
        if lines[i].strip() == "```json":
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == "```":
                    return i + 2, "\n".join(lines[i + 1 : j])
            break
    return 0, ""


ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


def _ref_target(ref: Any, schema: Any) -> Any:
    """The subschema that a `$ref` of `schema` names, or UNRESOLVED if this check can't follow it.

    Only a reference into the same file is read: '#' (the whole schema) or a '#/...' JSON pointer
    (percent-decoded, with ~1 and ~0 unescaped, through objects and array indexes) that leads to a
    value. Another file or URL, a plain-name fragment such as '#camera' ($anchor) and a pointer to
    nothing are UNRESOLVED."""
    if not isinstance(ref, str) or not ref.startswith("#"):
        return UNRESOLVED
    pointer = urllib.parse.unquote(ref[1:])
    if not pointer:
        return schema
    if not pointer.startswith("/"):
        return UNRESOLVED
    target: Any = schema
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict) and part in target:
            target = target[part]
        elif isinstance(target, list) and ARRAY_INDEX.fullmatch(part) and int(part) < len(target):
            target = target[int(part)]
        else:
            return UNRESOLVED
    return target


def _pointer(where: str, key: Any) -> str:
    """JSON pointer `where` extended by one object key or array index."""
    return f"{where}/{str(key).replace('~', '~0').replace('/', '~1')}"


def _schema_types(node: dict[str, Any]) -> set[str]:
    t = node.get("type")
    return set(t) if isinstance(t, list) else ({t} if isinstance(t, str) else set())


def _admits_kind(node: Any, schema: Any, kind: str, seen: tuple[int, ...] = ()) -> bool:
    """Whether schema `node` lets a JSON `kind` ('object' or 'array') through its own `type`, its
    $ref target's and its allOf branches'. A $ref it can't follow admits anything (it is reported
    by schema_reading_problems), and so does a cycle (`seen`: the schemas on the way here)."""
    if node is False:
        return False
    if not isinstance(node, dict) or id(node) in seen:
        return True
    types = _schema_types(node)
    if types and kind not in types:
        return False
    chain = (*seen, id(node))
    target = _ref_target(node["$ref"], schema) if "$ref" in node else UNRESOLVED
    if target is not UNRESOLVED and not _admits_kind(target, schema, kind, chain):
        return False
    return all(_admits_kind(sub, schema, kind, chain) for sub in node.get("allOf") or [])


def schema_shape_problems(value: Any, node: Any, schema: Any, path: str = "", seen: tuple[int, ...] = ()) -> list[str]:
    """Where an object or array `value` leaves the shape that schema `node` allows: a key the schema
    forbids, or does not list under `additionalProperties: false`; a required key that is missing;
    an object or an array where the schema allows only other types.

    It follows properties, additionalProperties (false, or a subschema), required,
    dependentRequired, items, local $refs (with any keywords beside a $ref, which JSON Schema
    2020-12 applies as well), allOf (every branch must fit) and oneOf/anyOf (some branch must fit;
    otherwise the problems of the closest branch are reported: the fewest, among branches whose
    type allows the value's kind). Scalars are placeholders and are not checked. Three things are
    left to schema_reading_problems, which fails the check on them: a $ref this walker can't
    follow (it constrains nothing here), the UNREAD_SHAPE_KEYWORDS, and shape constraints inside
    if/then/else and not, which are not applied. `seen` holds the schemas that $ref, allOf, oneOf
    and anyOf steps already applied at this place of `value`: meeting one again is a cycle, which
    is reported instead of followed forever."""
    if not isinstance(value, (dict, list)):
        return []
    where = path or "(top)"
    if node is False:
        return [f"{where}: the schema allows no value here"]
    if not isinstance(node, dict):
        return []
    if id(node) in seen:
        return [f"{where}: $ref, allOf, oneOf or anyOf lead back to a schema already applied here (a cycle)"]
    chain = (*seen, id(node))
    out: list[str] = []
    if "$ref" in node:
        target = _ref_target(node["$ref"], schema)
        if target is not UNRESOLVED:
            out += schema_shape_problems(value, target, schema, path, chain)
    kind = "object" if isinstance(value, dict) else "array"
    types = _schema_types(node)
    if types and kind not in types:
        return [*out, f"{where}: an {kind} where the schema allows none"]
    if isinstance(value, dict):
        props = node.get("properties", {})
        extra = node.get("additionalProperties", True)
        prefix = f"{path}." if path else ""
        for key, sub in value.items():
            here = f"{prefix}{key}"
            if key in props:
                if props[key] is False:
                    out.append(f"{here}: the schema forbids this key")
                else:
                    out += schema_shape_problems(sub, props[key], schema, here)
            elif extra is False:
                out.append(f"{here}: not a property in the schema")
            else:
                out += schema_shape_problems(sub, extra, schema, here)
        for key in node.get("required", []):
            if key not in value:
                out.append(f"{prefix}{key}: required by the schema, missing")
        dependent = node.get("dependentRequired")
        for key, needs in dependent.items() if isinstance(dependent, dict) else []:
            if key in value:
                out += [
                    f"{prefix}{need}: required by the schema when {key} is present, missing"
                    for need in needs
                    if need not in value
                ]
    elif "items" in node and not isinstance(node["items"], list):
        for i, item in enumerate(value):
            out += schema_shape_problems(item, node["items"], schema, f"{path}[{i}]")
    for sub in node.get("allOf", []):
        out += schema_shape_problems(value, sub, schema, path, chain)
    for key in ("oneOf", "anyOf"):
        branches = node.get(key) or []
        results = [schema_shape_problems(value, sub, schema, path, chain) for sub in branches]
        if results and all(results):
            same_kind = [r for sub, r in zip(branches, results) if _admits_kind(sub, schema, kind)]
            out += min(same_kind or results, key=len)
    return out


def schema_reading_problems(schema: Any) -> list[str]:
    """What keeps schema_shape_problems from reading `schema` in full, so that check_appendix_a
    fails instead of passing unread: each of the UNREAD_SHAPE_KEYWORDS it uses (and `items` as an
    array); each shape constraint inside a then, else or not (SHAPE_KEYWORDS, or a `false`
    subschema); and each $ref that _ref_target can't follow, with its JSON pointer. Instance values
    (INSTANCE_KEYWORDS) are not searched, and the members of SCHEMA_MAP_KEYWORDS maps are
    subschemas whatever their names."""
    unread: set[str] = set()
    unapplied: set[str] = set()
    refs: list[str] = []

    def visit(node: Any, where: str, inside: str) -> None:
        if isinstance(node, list):
            for i, item in enumerate(node):
                visit(item, _pointer(where, i), inside)
            return
        if not isinstance(node, dict):
            return
        for key, sub in node.items():
            here = _pointer(where, key)
            if key in UNREAD_SHAPE_KEYWORDS:
                unread.add(f"uses {key}")
            elif key == "items" and isinstance(sub, list):
                unread.add("uses items as an array (the tuple form before JSON Schema 2020-12)")
            if inside and key in SHAPE_KEYWORDS:
                unapplied.add(f"uses {key} inside {inside}")
            if key == "$ref" and _ref_target(sub, schema) is UNRESOLVED:
                refs.append(
                    f"{where or '/'}: cannot follow $ref {sub!r}; only '#' and '#/...' pointers to a "
                    "subschema of this file are read"
                )
            if key in INSTANCE_KEYWORDS:
                continue
            within = inside or (key if key in UNAPPLIED_KEYWORDS else "")
            if key in ("then", "else") and sub is False:
                unapplied.add(f"uses {key}: false")
            if key in SCHEMA_MAP_KEYWORDS and isinstance(sub, dict):
                for name, child in sub.items():
                    if within and child is False:
                        unapplied.add(f"uses a false subschema inside {within}")
                    visit(child, _pointer(here, name), within)
            else:
                visit(sub, here, within)

    visit(schema, "", "")
    return (
        [f"{u}, which check_appendix_a does not read yet; teach it before trusting this check" for u in sorted(unread)]
        + [
            f"{u}, which check_appendix_a does not apply (scalars are placeholders, so it can't tell "
            "whether `if` holds); teach it before trusting this check"
            for u in sorted(unapplied)
        ]
        + refs
    )


def check_appendix_a(repo: Repo) -> list[Problem]:
    """PRD.md Appendix A's JSON example has the shape of viewpoints/schema/viewpoint.schema.json,
    the source of truth for the manifest (D-044): see schema_shape_problems. If D-044 is reversed
    and Appendix A goes back to a sketch that differs from the schema, this check goes too."""
    if not repo.has("PRD.md"):
        return []
    if not repo.has(MANIFEST_SCHEMA):
        return [Problem("appendix-a", MANIFEST_SCHEMA, "missing, so PRD Appendix A can't be checked")]
    line, text = appendix_a_example(repo.read("PRD.md"))
    if not line:
        return [Problem("appendix-a", "PRD.md", "no ```json block under '## Appendix A'")]
    try:
        example = json.loads(text)
    except ValueError as exc:
        return [Problem("appendix-a", f"PRD.md:{line}", f"the example is not JSON: {exc}")]
    try:
        schema = json.loads(repo.read(MANIFEST_SCHEMA))
    except ValueError as exc:
        return [Problem("appendix-a", MANIFEST_SCHEMA, f"not JSON: {exc}")]
    problems = [Problem("appendix-a", MANIFEST_SCHEMA, msg) for msg in schema_reading_problems(schema)]
    problems += [Problem("appendix-a", f"PRD.md:{line}", msg) for msg in schema_shape_problems(example, schema, schema)]
    return problems


# --------------------------------------------------------------------------------------------
# make test against CI (PRD §12: CI on every push; ci.yml runs the `make test-<suite>` targets)

MAKEFILE = "Makefile"
CI_WORKFLOW = ".github/workflows/ci.yml"
MATRIX_SUITE = "${{ matrix.suite }}"
CI_RUN = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*?)\s*$")
CI_SUITE = re.compile(r"(?m)^\s*(?:-\s+)?suite:\s*([A-Za-z0-9_.-]+)\s*(?:#.*)?$")
# make options whose value is the next word: never a target.
MAKE_OPTIONS_WITH_VALUE = ("-C", "-f", "-I", "-o", "-W", "--directory", "--file", "--makefile", "--include-dir")


def make_variable(text: str, name: str) -> list[str] | None:
    """The words of Makefile variable `name` at its first assignment (`=`, `:=`, `::=` or `?=`,
    with `\\` continuation lines; a `#` starts a comment), or None if it is never assigned."""
    lines = text.split("\n")
    assign = re.compile(rf"^{re.escape(name)}[ \t]*(?::{{1,2}}|\?)?=(.*)$")
    for i, line in enumerate(lines):
        m = assign.match(line)
        if not m:
            continue
        value, j = m.group(1), i
        while value.endswith("\\") and j + 1 < len(lines):
            j += 1
            value = value[:-1] + " " + lines[j]
        return value.split("#")[0].split()
    return None


def ci_make_targets(text: str) -> tuple[set[str], set[str]]:
    """(the targets that the workflow's `run:` commands pass to make, its matrix `suite:` values).

    A `run:` is one line or a `|`/`>` block (the lines indented deeper than its key). Words after
    `make` that are options (with the value of MAKE_OPTIONS_WITH_VALUE), numbers or VAR=value
    assignments are not targets, and `${{ matrix.suite }}` stands for every matrix `suite:` value.
    Step names and comments never count."""
    lines = text.split("\n")
    suites = set(CI_SUITE.findall(text))
    commands: list[str] = []
    i = 0
    while i < len(lines):
        m = CI_RUN.match(lines[i])
        i += 1
        if not m:
            continue
        if m.group(2)[:1] in ("|", ">"):
            indent = len(m.group(1))
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
                commands.append(lines[i])
                i += 1
        else:
            commands.append(m.group(2))
    targets: set[str] = set()
    for command in commands:
        command = command.split(" #")[0] if not command.lstrip().startswith("#") else ""
        for m in re.finditer(r"(?<![\w-])make((?:[ \t]+[^\s;&|()]+)*)", command.replace(MATRIX_SUITE, "\0")):
            words = m.group(1).split()
            for k, word in enumerate(words):
                if word.startswith("-") or "=" in word or word.isdigit():
                    continue
                if k and words[k - 1] in MAKE_OPTIONS_WITH_VALUE:
                    continue
                if "\0" in word:
                    targets |= {word.replace("\0", s) for s in suites}
                else:
                    targets.add(word)
    return targets, suites


def check_ci_suites(repo: Repo) -> list[Problem]:
    """CI runs every suite that `make test` runs (PRD §12): each name in the Makefile's TEST_SUITES
    has a `test-<name>` rule, and .github/workflows/ci.yml runs it as `make test-<name>` (directly,
    or through `make test-${{ matrix.suite }}` and a matrix `suite:` entry; `make test` covers
    all but the laptop-only ones), unless the Makefile names it in LAPTOP_ONLY_SUITES, which CI
    must then not run as `make test-<name>`. CI runs no `make test-<name>` that TEST_SUITES lacks.
    With no Makefile or no ci.yml there is nothing to compare."""
    if not (repo.has(MAKEFILE) and repo.has(CI_WORKFLOW)):
        return []
    makefile = repo.read(MAKEFILE)
    suites = make_variable(makefile, "TEST_SUITES")
    if not suites:
        return [Problem("ci-suites", MAKEFILE, "TEST_SUITES is not assigned, or empty")]
    laptop = make_variable(makefile, "LAPTOP_ONLY_SUITES") or []
    targets, _ = ci_make_targets(repo.read(CI_WORKFLOW))
    in_ci = set(suites) - set(laptop) if "test" in targets else set()
    in_ci |= {t[len("test-") :] for t in targets if t.startswith("test-")}
    problems: list[Problem] = []
    for suite in suites:
        if not re.search(rf"(?m)^test-{re.escape(suite)}[ \t]*:(?!=)", makefile):
            problems.append(
                Problem("ci-suites", MAKEFILE, f"TEST_SUITES names {suite}, but there is no test-{suite} rule")
            )
        if suite in laptop and suite in in_ci:
            problems.append(
                Problem(
                    "ci-suites", CI_WORKFLOW, f"runs make test-{suite}, which the Makefile lists in LAPTOP_ONLY_SUITES"
                )
            )
        elif suite not in laptop and suite not in in_ci:
            problems.append(
                Problem(
                    "ci-suites",
                    CI_WORKFLOW,
                    f"never runs make test-{suite} (TEST_SUITES); run it, or list it in LAPTOP_ONLY_SUITES",
                )
            )
    for suite in sorted(in_ci - set(suites)):
        problems.append(Problem("ci-suites", CI_WORKFLOW, f"runs make test-{suite}, which TEST_SUITES lacks"))
    for suite in sorted(set(laptop) - set(suites)):
        problems.append(Problem("ci-suites", MAKEFILE, f"LAPTOP_ONLY_SUITES names {suite}, which TEST_SUITES lacks"))
    return problems


CHECKS: dict[str, Callable[[Repo], list[Problem]]] = {
    "links": check_links,
    "tables": check_tables,
    "weekdays": check_weekdays,
    "day-g": check_day_g_offsets,
    "calendar": check_calendar,
    "milestones": check_milestone_dates,
    "decisions": check_decisions,
    "append-only": check_decisions_append_only,
    "d-refs": check_decision_refs,
    "tickets": check_tickets,
    "spend": check_spend_ledger,
    "prd-sections": check_prd_sections,
    "viewpoints": check_viewpoint_ids,
    "notices": check_notices,
    "appendix-a": check_appendix_a,
    "ci-suites": check_ci_suites,
}


def run_all(repo: Repo, only: Iterable[str] = ()) -> dict[str, list[Problem]]:
    """Run every check (or the named ones) and return {check name: problems}."""
    names = list(only) or list(CHECKS)
    return {name: CHECKS[name](repo) for name in names}


def main(argv: Sequence[str] | None = None) -> int:
    """`python3 -I docs/checks/doccheck.py [check ...]`: print every problem; exit 1 if any."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    unknown = [a for a in args if a not in CHECKS]
    if unknown:
        print(f"unknown check(s): {', '.join(unknown)}; known: {', '.join(CHECKS)}")
        return 2
    repo = Repo()
    results = run_all(repo, args)
    bad = 0
    for name, problems in results.items():
        print(f"{name:13s} {'ok' if not problems else f'{len(problems)} problem(s)'}")
        for p in problems:
            print(f"  {p}")
        bad += len(problems)
    if repo.history_note:
        print(f"note: {repo.history_note}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
