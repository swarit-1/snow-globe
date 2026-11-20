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
  headings carry a declared status and dates that never go backwards or into the future, and a
  "supersedes D-n" points back to an earlier entry;
- every D-n, ticket id and PRD § reference in the repo resolves; tickets are unique, sit under their
  milestone and carry a declared status;
- the spend ledger adds up and matches CLAUDE.md's "Spend so far";
- viewpoint ids in the docs are manifest ids, and CLAUDE.md's viewpoint table lists every manifest.

`Repo` reads the working tree and git. Tests build an in-memory `Repo` instead (test_selftest.py).
"""

from __future__ import annotations

import datetime as dt
import difflib
import os
import posixpath
import re
import subprocess
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]

# Commits allowed to change committed lines of DECISIONS.md, with the reason. Add one only with the
# lead's sign-off (for example, scrubbing a secret or a personal detail committed by mistake).
APPEND_ONLY_EXEMPT: Dict[str, str] = {}

# Historical records whose "§n" numbers refer to the PRD version current when they were written
# (DECISIONS.md is append-only; docs/review and docs/council review older drafts).
SECTION_REF_SKIP_PREFIXES = ("DECISIONS.md", "docs/review/", "docs/council/", "docs/checks/")
# A "§n" within this many characters after one of these words names another document.
OTHER_DOCUMENT_WORDS = ("Oculus", "RFC", "ISO", "v0.1", "v0.2", "v0.3")
OTHER_DOCUMENT_WINDOW = 40

TEXT_SUFFIXES = (".md", ".py", ".rs", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".toml", ".yml", ".yaml",
                 ".tf", ".hcl", ".sh", ".json", ".txt", ".cfg", ".ini")
TEXT_NAMES = ("Makefile", "Dockerfile")
MAX_TEXT_BYTES = 2_000_000
SKIP_DIRS = {".git", "node_modules", ".venv", "target", "dist", ".terraform", "__pycache__",
             ".pytest_cache", ".ruff_cache", ".hypothesis", "test-results", "playwright-report"}

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
        self._files: Optional[Dict[str, str]] = None
        self._listed: Optional[List[str]] = None
        self._index: Optional[Tuple[Set[str], Set[str]]] = None
        self._changes: Optional[List[FileChange]] = None
        self.history_note = ""

    @classmethod
    def memory(cls, files: Dict[str, str], history: Optional[Sequence[str]] = None) -> "Repo":
        repo = cls(Path("/nonexistent-lookout-memory-repo"))
        repo._files = dict(files)
        repo._listed = sorted(files)
        versions = [(f"v{i}", text) for i, text in enumerate(history or [])]
        if "DECISIONS.md" in files and (not versions or versions[-1][1] != files["DECISIONS.md"]):
            versions.append(("working tree", files["DECISIONS.md"]))
        repo._changes = [FileChange(a_label, a, b_label, b)
                         for (a_label, a), (b_label, b) in zip(versions, versions[1:])]
        return repo

    # -- files

    def _git(self, *args: str) -> str:
        out = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True,
                             check=True, timeout=120)
        return out.stdout

    def paths(self) -> List[str]:
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
            self._listed = sorted({n.replace(os.sep, "/") for n in names
                                   if (self.root / n).is_file()})
        return self._listed

    def _files_and_dirs(self) -> Tuple[Set[str], Set[str]]:
        """The repo's files (as `paths()` lists them) and every folder that holds one."""
        if self._index is None:
            files = set(self.paths())
            dirs: Set[str] = set()
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

    def case_variant(self, rel: str) -> Optional[str]:
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

    def markdown(self) -> List[str]:
        return [p for p in self.paths() if p.endswith(".md")]

    def text_files(self) -> List[str]:
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

    def _decisions_at(self, rev: str, cache: Dict[str, Optional[str]]) -> Optional[str]:
        """DECISIONS.md as commit `rev` has it, or None when that commit has no such file."""
        if rev not in cache:
            try:
                cache[rev] = self._git("show", f"{rev}:DECISIONS.md")
            except subprocess.CalledProcessError:
                cache[rev] = None
        return cache[rev]

    def decisions_changes(self) -> List[FileChange]:
        """Every change made to DECISIONS.md, oldest first: each commit that touched it against
        each of its own parents (a merge against both, so two branches that each appended are not
        compared with each other), then HEAD against the working tree when they differ.

        A parent without the file (the commit that created it) is skipped; a commit that deleted
        it compares as an empty file, so the deletion is reported. Empty, with `history_note` set,
        when git can't say. `--full-history` keeps the side branches that the default
        simplification would prune, and every merge whose file differs from one of its parents."""
        if self._changes is not None:
            return self._changes
        changes: List[FileChange] = []
        texts: Dict[str, Optional[str]] = {}
        try:
            shas = self._git("log", "--full-history", "--reverse", "--format=%H", "--",
                             "DECISIONS.md").split()
            shallow = self._git("rev-parse", "--is-shallow-repository").strip() == "true"
            # Real parents, read without a path filter, so history simplification can't rewrite them.
            parents: Dict[str, List[str]] = {}
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


def prose_lines(text: str) -> List[str]:
    """The lines of `text`, with fenced code blocks blanked (line numbers are kept)."""
    out: List[str] = []
    fence: Optional[str] = None
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


def split_cells(line: str) -> List[str]:
    """GitHub table cells: split on unescaped pipes (also inside code spans), outer pipes dropped."""
    s = line.strip()
    parts = re.split(r"(?<!\\)\|", s)
    if s.startswith("|"):
        parts = parts[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        parts = parts[:-1]
    return [p.strip() for p in parts]


DELIMITER_ROW = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def tables(text: str) -> Iterator[Tuple[int, List[Tuple[int, str]]]]:
    """Yield (header line number, [(line number, line), ...]) for each pipe table, delimiter
    row included (it is the second entry)."""
    lines = prose_lines(text)
    i = 0
    while i < len(lines):
        if ("|" in lines[i] and i + 1 < len(lines) and "|" in lines[i + 1]
                and DELIMITER_ROW.match(lines[i + 1])):
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


def anchors(text: str) -> Set[str]:
    """Every #anchor a markdown file offers: heading slugs (with GitHub's -1, -2 for repeats) and
    explicit id= or name= attributes."""
    seen: Dict[str, int] = {}
    out: Set[str] = set()
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
    date: Optional[dt.date]  # None when the day doesn't exist (31 Nov)
    text: str
    year_written: bool = True  # False when the year was inferred from `near`


def labelled_dates(line: str, near: dt.date) -> List[LabelledDate]:
    """Weekday-labelled dates in `line`, in order ("Thu 19 Nov 2026", "Mon 23 Nov", "Thu 19").

    A date without a month takes the month (and year) of the next date in the line that has one,
    so "Fri 15 → Wed 20 Jan 2027" reads as two January 2027 dates. A month without a year takes the
    year that puts the date nearest `near` (the doc's anchor date, see `doc_anchor_date`). Dates
    with no month anywhere are left out.
    """
    found = list(DATE_RE.finditer(strip_inline_code(line)))
    out: List[LabelledDate] = []
    for k, m in enumerate(found):
        wd, day, mon, year = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if mon is None:
            later = next((f for f in found[k + 1:] if f.group(3)), None)
            if later is None:
                continue
            mon, year = later.group(3), later.group(4)
        month = MONTHS.index(mon) + 1
        if year is not None:
            years = [int(year)]
        else:
            years = sorted((near.year - 1, near.year, near.year + 1),
                           key=lambda y: abs((_safe_date(y, month, min(day, 28)) - near).days))
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
        iso = [_maybe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
               for m in ISO_DATE.finditer(strip_inline_code(line))]
        found = [d for d in dates + iso if d is not None]
        if found:
            return found[0]
    return fallback


def _safe_date(y: int, m: int, d: int) -> dt.date:
    return dt.date(y, m, d)


def _maybe_date(y: int, m: int, d: int) -> Optional[dt.date]:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


G_OFFSET = re.compile(r"(?<![\w+\-−])G(?:\s?\+\s?(\d+))?(?![\w+\-−])")
APPROX = re.compile(r"(?:about|approximately|around|roughly|before|after|~|≈)\s*$")


def g_offsets(line: str) -> List[int]:
    """Exact day-G offsets in `line` ("G", "G+4", "by G+21"); approximate or open-ended ones
    ("about G+24", "before G") are left out."""
    clean = strip_inline_code(line)
    out = []
    for m in G_OFFSET.finditer(clean):
        if APPROX.search(clean[:m.start()]):
            continue
        out.append(int(m.group(1) or 0))
    return out


def day_amount(text: str) -> Optional[float]:
    """'3½ days' -> 3.5, '¾ day' -> 0.75, '~1¼ days' -> 1.25; None when there is no amount."""
    m = re.search(r"~?(\d*)([¼½¾]?)\s?days?\b", text)
    if not m or (not m.group(1) and not m.group(2)):
        return None
    return (int(m.group(1)) if m.group(1) else 0) + FRACTIONS[m.group(2)]


def find_day_g(repo: Repo) -> Tuple[Optional[dt.date], List[Problem]]:
    """Day G as the docs define it: PLAN.md's "G = Thu 19 Nov 2026" or CLAUDE.md's
    "2026-11-19 (day G)". Every definition found must agree."""
    defs: List[Tuple[str, dt.date]] = []
    pattern_plan = re.compile(r"\bG = \**(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w* )?(\d{1,2}) "
                              r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* (\d{4})")
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


def check_links(repo: Repo) -> List[Problem]:
    """Every relative link in every markdown file names a file or folder of the repo as GitHub sees
    it (exact case; a git-ignored file doesn't count, see Repo.exists), and every #anchor names a
    heading (or an id) of its target file."""
    problems = []
    anchor_cache: Dict[str, Set[str]] = {}
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
                        problems.append(Problem("links", f"{name}:{n}",
                                                f"{target!r}: no heading or id #{anchor} in {rel}"))
    return problems


def check_tables(repo: Repo) -> List[Problem]:
    """Every row of every markdown table has as many cells as its header (GitHub's rule: cells split
    on unescaped pipes, even inside code spans; write \\| for a literal pipe)."""
    problems = []
    for name in repo.markdown():
        for header_no, rows in tables(repo.read(name)):
            width = len(split_cells(rows[0][1]))
            for n, line in rows[1:]:
                cells = len(split_cells(line))
                if cells != width:
                    problems.append(Problem("tables", f"{name}:{n}",
                                            f"{cells} cells; the header (line {header_no}) has {width}"))
    return problems


# --------------------------------------------------------------------------------------------
# Checks: dates


def check_weekdays(repo: Repo) -> List[Problem]:
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
                    hint = "" if d.year_written else (
                        f" (no year is written, so it is read in the year nearest {near:%d %b %Y}, this "
                        "doc's Updated date or day G; write the year if another is meant)")
                    problems.append(Problem("weekdays", f"{name}:{n}",
                                            f"{d.text!r}: {d.date:%d %b %Y} is a {d.date:%a}{hint}"))
    return problems


def check_day_g_offsets(repo: Repo) -> List[Problem]:
    """Day G is defined consistently, and wherever a line gives G+N offsets and the same number of
    weekday-labelled dates, each date is G plus its offset."""
    g, problems = find_day_g(repo)
    uses = [(name, n, line) for name in repo.markdown()
            for n, line in enumerate(prose_lines(repo.read(name)), 1) if g_offsets(line)]
    if g is None:
        if uses:
            name, n, _ = uses[0]
            problems.append(Problem("day-g", f"{name}:{n}", "G+N offsets are used but day G is "
                                    "never defined (PLAN.md 'G = <date>' or CLAUDE.md '<date> (day G)')"))
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
                problems.append(Problem("day-g", f"{name}:{n}",
                                        f"G+{off} is {expected:%a %d %b %Y}, "
                                        f"but the line says {date:%a %d %b %Y}"))
    return problems


def _header_index(cells: Sequence[str], pattern: str) -> Optional[int]:
    for i, cell in enumerate(cells):
        if re.search(pattern, cell, re.I):
            return i
    return None


def check_calendar(repo: Repo) -> List[Problem]:
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
                        problems.append(Problem("calendar", f"{name}:{n}",
                                                f"slack {cells[i_slack]!r} is not {share:.0%} of {cells[i_len]!r}"))
                spans = [c for c in cells if re.search(r"G(?:\+\d+)?\s*(?:→|->|–|—|to)\s*G\+\d+", c)]
                if len(spans) == 1:
                    offs = g_offsets(spans[0])
                    if len(offs) == 2 and offs[1] - offs[0] != length:
                        problems.append(Problem("calendar", f"{name}:{n}",
                                                f"{spans[0]!r} spans {offs[1] - offs[0]} days, length says {cells[i_len]!r}"))
    return problems


MILESTONE = r"(M\d+(?:\.\d+)?)"


def _milestone_sources(repo: Repo, g: dt.date) -> Dict[str, Dict[str, Tuple[List[dt.date], List[int]]]]:
    """{milestone: {source: (weekday-labelled dates, G offsets)}} from PLAN.md's calendar rows and
    milestone headings, PRD_PROGRESS.md's milestone headings and PRD.md's §16 table rows."""
    out: Dict[str, Dict[str, Tuple[List[dt.date], List[int]]]] = {}

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


def check_milestone_dates(repo: Repo) -> List[Problem]:
    """Each milestone's dates agree wherever they appear (PLAN.md calendar and heading,
    PRD_PROGRESS.md heading), and so do its G offsets (PLAN.md calendar, PRD.md §16 table)."""
    g, _ = find_day_g(repo)
    if g is None:
        return []
    problems = []
    for ms, sources in sorted(_milestone_sources(repo, g).items()):
        dated = [(s, d) for s, (d, _) in sources.items() if d]
        offset = [(s, o) for s, (_, o) in sources.items() if o]
        for kind, items, fmt in (("dates", dated, lambda v: ", ".join(f"{x:%a %d %b %Y}" for x in v)),
                                 ("G offsets", offset, lambda v: ", ".join(f"G+{x}" for x in v))):
            if len({tuple(v) for _, v in items}) > 1:
                listed = "; ".join(f"{s}: {fmt(v)}" for s, v in items)
                problems.append(Problem("milestones", items[0][0], f"{ms} {kind} disagree: {listed}"))
    return problems


# --------------------------------------------------------------------------------------------
# Checks: DECISIONS.md

D_HEADING = re.compile(r"^## (D-(\d{3})) — (.+)$")
D_REF = re.compile(r"(?<![\w-])D-(\d{3})(?!\d)")
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def declared_statuses(text: str, label: str) -> List[str]:
    """Bold words declared on a line like '**Status:** **decided** (...) · **proposed** ...', or
    in a bullet list under a '**Status values:**' line ('- **done:** ...')."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if re.match(rf"^\s*\*\*{re.escape(label)}:?\*\*:?", line):
            inline = re.findall(r"\*\*([^*:]+?):?\*\*", line)[1:]
            if inline:
                return [w.strip().lower() for w in inline]
            items = []
            for follow in lines[i + 1:]:
                m = re.match(r"^\s*[-*]\s+\*\*([^*:]+?)[:.]?\*\*", follow)
                if not m:
                    if follow.strip():
                        break
                    continue
                items.append(m.group(1).strip().lower())
            return items
    return []


def _decision_headings(text: str) -> List[Tuple[int, str, int, List[str]]]:
    """(line number, D-id, number, ' · '-separated parts after the id) for each D heading."""
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        m = D_HEADING.match(line)
        if m:
            out.append((n, m.group(1), int(m.group(2)), [p.strip() for p in m.group(3).split(" · ")]))
    return out


def check_decisions(repo: Repo, today: Optional[dt.date] = None) -> List[Problem]:
    """DECISIONS.md: D-ids run D-001, D-002, ... in order; every heading reads
    '## D-NNN — title · status · ...' with a declared status word; heading dates are real, never
    go backwards and are not in the future; 'supersedes/amends/closes D-n' points back."""
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
            problems.append(Problem("decisions", f"DECISIONS.md:{n}",
                                    "a D heading must read '## D-NNN — title · status · date'"))
    headings = _decision_headings(text)
    numbers = [num for _, _, num, _ in headings]
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append(Problem("decisions", "DECISIONS.md",
                                f"D-ids are not D-001..D-{len(numbers):03d} in order: "
                                + ", ".join(f"D-{x:03d}" for x in numbers)))
    last_date: Optional[dt.date] = None
    for n, did, num, parts in headings:
        where = f"DECISIONS.md:{n}"
        if len(parts) < 2:
            problems.append(Problem("decisions", where, f"{did} has no ' · status' part"))
        elif statuses and not any(re.search(rf"\b{re.escape(s)}\b", parts[1], re.I) for s in statuses):
            problems.append(Problem("decisions", where,
                                    f"{did} status {parts[1]!r} uses none of: {', '.join(statuses)}"))
        # The entry's date is the first ISO date after the status; every date must be real.
        own: Optional[dt.date] = None
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
                problems.append(Problem("decisions", where,
                                        f"{did} is dated {own}, before the entry above it ({last_date})"))
            last_date = own
        for part in parts[2:]:
            for verb, ref in re.findall(r"\b(supersedes|amends|closes|reverses|replaces)\s+D-(\d{3})", part):
                if int(ref) >= num:
                    problems.append(Problem("decisions", where,
                                            f"{did} {verb} D-{ref}, which is not an earlier entry"))
    return problems


def check_decisions_append_only(repo: Repo) -> List[Problem]:
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
            problems.append(Problem("append-only", f"DECISIONS.md ({change.old_label} -> {change.new_label})",
                                    f"{op}s committed line {i1 + 1}: {first[:80]!r}"))
    return problems


def check_decision_refs(repo: Repo) -> List[Problem]:
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


def is_ticket_reference(tid: str, digits: Dict[str, Set[int]]) -> bool:
    """Whether 'M<a>-<n>' cited in a doc means a ticket, given the digit counts each milestone's
    ticket numbers use in PRD_PROGRESS.md (`digits`: M0 -> {1}, M1 -> {2}, ...).

    A number with another digit count than its milestone's tickets ('M4-5' while M4's tickets are
    M4-01, M4-02, ...) reads as a milestone range, M4 to M5, and so does a one-digit number for a
    milestone that has no tickets yet. Anything else is a ticket reference and must exist."""
    milestone, number = tid.rsplit("-", 1)
    if milestone in digits:
        return len(number) in digits[milestone]
    return len(number) > 1


def check_tickets(repo: Repo) -> List[Problem]:
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
    seen: Dict[str, int] = {}
    section: Optional[str] = None
    status_col: Optional[int] = None
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
                problems.append(Problem("tickets", where,
                                        f"{tid} status {status[:40]!r} starts with none of: {', '.join(statuses)}"))
    digits: Dict[str, Set[int]] = {}
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


def _cents(text: str) -> Optional[int]:
    m = MONEY.search(text)
    if not m:
        return None
    return int(round(float(m.group(1).replace(",", "")) * 100))


def check_spend_ledger(repo: Repo) -> List[Problem]:
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
    total: Optional[int] = None
    last_date: Optional[str] = None
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
                problems.append(Problem("spend", where,
                                        f"running total {cells[i_total]} should be ${running / 100:,.2f}"))
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
                problems.append(Problem("spend", f"CLAUDE.md:{n}",
                                        f"'Spend so far: {m.group(1)}' but the ledger's running total is ${total / 100:,.2f}"))
    return problems


# "§13.4", and ranges: "§13.4-13.7", "§10.2–10.6", "§10.2–§10.6", "§13.4-7" (= §13.4 to §13.7).
SECTION_REF = re.compile(r"§\s?(\d+(?:\.\d+)*)(?:[-–]§?(\d+(?:\.\d+)*))?")


def section_range_end(start: str, end: str) -> str:
    """The full number of a range's end: '13.4', '13.7' -> '13.7'; '13.4', '7' -> '13.7'."""
    head, tail = start.split("."), end.split(".")
    if len(tail) >= len(head):
        return end
    return ".".join(head[:len(head) - len(tail)] + tail)


def prd_sections(text: str) -> Set[str]:
    """Section numbers of PRD.md's headings ('## 8. Data' -> '8', '### 8.7 Storage' -> '8.7')."""
    return {m.group(1) for m in re.finditer(r"^#{1,6}\s+(\d+(?:\.\d+)*)\.?\s", text, re.M)}


def check_prd_sections(repo: Repo) -> List[Problem]:
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
                before = line[max(0, m.start() - OTHER_DOCUMENT_WINDOW):m.start()]
                if any(word in before for word in OTHER_DOCUMENT_WORDS):
                    continue
                for ref in unknown:
                    problems.append(Problem("prd-sections", f"{name}:{n}", f"§{ref} is not a section of PRD.md"))
    return problems


def manifest_ids(repo: Repo) -> List[str]:
    return sorted(posixpath.basename(p)[:-5] for p in repo.paths()
                  if p.startswith("viewpoints/manifests/") and p.endswith(".json"))


def check_viewpoint_ids(repo: Repo) -> List[Problem]:
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
                    problems.append(Problem("viewpoints", f"{name}:{n}",
                                            f"{vid} is not a manifest in viewpoints/manifests/"))
    if repo.has("CLAUDE.md"):
        for header_no, rows in tables(repo.read("CLAUDE.md")):
            header = split_cells(rows[0][1])
            if not header or "viewpoint_id" not in header[0]:
                continue
            listed = sorted(re.sub(r"[`*]", "", split_cells(line)[0]) for _, line in rows[2:])
            if listed != ids:
                problems.append(Problem("viewpoints", f"CLAUDE.md:{header_no}",
                                        f"the viewpoint table lists {listed}, the manifests are {ids}"))
    return problems


CHECKS: Dict[str, Callable[[Repo], List[Problem]]] = {
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
}


def run_all(repo: Repo, only: Iterable[str] = ()) -> Dict[str, List[Problem]]:
    """Run every check (or the named ones) and return {check name: problems}."""
    names = list(only) or list(CHECKS)
    return {name: CHECKS[name](repo) for name in names}


def main(argv: Optional[Sequence[str]] = None) -> int:
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
