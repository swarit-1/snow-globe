#!/usr/bin/env python3
"""The licence ledger's package list, read from every lock file in the repo (PRD §15.4).

``docs/ledger/licences.json`` is the licence ledger. Its ``items`` (data sources, code ports,
algorithms, models, services, platforms and tools), ``vocabulary``, ``licence_overrides`` (the
reviewed reading of a package whose own metadata is not an SPDX expression) and reviewed
``disagreements`` are kept by hand. This script writes the rest: ``packages`` (one entry per
package that a lock file pins, own packages included), ``package_counts``, ``generated``, and the
disagreements it finds itself (``found_by: "ledger.py"``). THIRD_PARTY_NOTICES is to be generated
from the ledger at M5 (PLAN.md); until then it is kept by hand, and this script only reports
where the two differ.

Usage (standard library only, Python 3.11 or later for ``tomllib``; no network)::

    python3 -I -B docs/ledger/ledger.py check   # every package a lock file pins is in
                                                # licences.json for that lock at its version,
                                                # and no other (lock files only; exit 1 if not)
    python3 -I -B docs/ledger/ledger.py build   # re-read the locks and the installed packages'
                                                # own licence metadata, then rewrite licences.json

``build`` reads each licence where the package is installed on this machine: a wheel's
``METADATA`` (``License-Expression``, else a short ``License``, else its licence classifiers,
else its licence file) in the uv projects' ``.venv`` folders, then uv's cache; a crate's
``license`` through ``cargo metadata --offline --locked`` (the registry copies under
``~/.cargo``); an npm package's ``package.json`` under ``node_modules/.pnpm``; and the Terraform
provider's ``LICENSE.txt`` in the plugin cache (``$TF_PLUGIN_CACHE_DIR``, else
``$SNOW_GLOBE_DATA_DIR/cache/terraform-plugins``; skipped when neither is set). A package that is
not installed here (another platform's binary, a Windows-only wheel) keeps ``licence: null`` and
the status ``to-confirm``, with a generated ``confirm_with``. Every package also gets a generated
``duty`` (:func:`package_duty`), from where it ships and the hand-kept licence-to-duty table
``vocabulary.duty_by_licence``.

Both commands read the lock files git tracks (its index: committed or staged). An untracked lock
file is another package's uncommitted work: it is read only once it is added, so the ledger is
checked against what is being committed, and a clean checkout (CI) gives the same answer as this
Mac. A tracked lock file that is gone from disk (deleted, not yet removed from git) pins nothing:
``check`` reports any entry the ledger still lists for it. Both commands name either kind in a
note (:func:`lock_notes`).

The ledger never writes the owner's GitHub account: in a generated ``source`` it is ``<owner>``
(:func:`public_source`), as in the hand-kept items.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
LEDGER = HERE / "licences.json"
NOTICES = "THIRD_PARTY_NOTICES"
LOCK_NAMES = ("uv.lock", "Cargo.lock", "pnpm-lock.yaml", ".terraform.lock.hcl")

#: Where each lock file's packages end up (the ledger's ``ships`` vocabulary), by the scope the
#: lock gives them: ``root`` (a uv project itself) and ``workspace`` (a Cargo workspace member),
#: ``runtime`` (a uv project's dependencies and the extras they ask for, an npm ``dependencies``
#: entry), ``group`` (a uv dependency group other than ``dev``), ``dev``, ``normal`` and ``build``
#: (Cargo edge kinds from the workspace members), ``provider`` (Terraform). A tracked lock file
#: that is not listed fails ``build`` until someone says where its packages go.
#: ``app/src-tauri/Cargo.lock`` is the M1 app shell's (untracked while that work is in progress,
#: so neither command reads it yet): its crates are placed as app/Cargo.lock's are.
SHIPS: dict[str, dict[str, str]] = {
    "workers/common/uv.lock": {"root": "workers", "runtime": "workers", "extra": "workers", "dev": "tests-only"},
    "workers/capture/uv.lock": {"root": "workers", "runtime": "workers", "dev": "tests-only"},
    "workers/forecast/uv.lock": {"root": "workers", "runtime": "workers", "dev": "tests-only"},
    "eval/uv.lock": {"root": "eval-only", "runtime": "eval-only", "group": "eval-only", "dev": "tests-only"},
    "plates/uv.lock": {"root": "not-shipped", "runtime": "not-shipped", "dev": "tests-only"},
    "app/Cargo.lock": {"workspace": "app", "normal": "app", "build": "not-shipped", "dev": "tests-only"},
    "app/src-tauri/Cargo.lock": {"workspace": "app", "normal": "app", "build": "not-shipped", "dev": "tests-only"},
    "spikes/m1-platform/Cargo.lock": {
        "workspace": "not-shipped",
        "normal": "not-shipped",
        "build": "not-shipped",
        "dev": "not-shipped",
    },
    "renderer/pnpm-lock.yaml": {"runtime": "eval-only", "dev": "tests-only"},
    "app/src/pnpm-lock.yaml": {"runtime": "app", "dev": "not-shipped"},
    "infra/terraform/.terraform.lock.hcl": {"provider": "not-shipped"},
    "infra/terraform/bootstrap/.terraform.lock.hcl": {"provider": "not-shipped"},
}
#: Widest distribution first: the order ``ships`` lists are sorted in.
SHIPS_ORDER = ("app", "workers", "eval-only", "not-shipped", "tests-only")
#: The ``ships`` values whose packages can reach someone else: the app (from M1) and the workers
#: (if they were ever distributed; today they run on this Mac and, once applied, in the owner's
#: AWS account). Nothing else is ever redistributed, so no attribution duty applies to it.
DISTRIBUTED = ("app", "workers")

#: Marker environments: this Mac (where every venv, node_modules and Cargo build lives) and the
#: Lambda image (linux/arm64, PRD §11.3), plus Windows, only to name what is Windows-only.
ENV_MAC = {
    "sys_platform": "darwin",
    "platform_system": "Darwin",
    "platform_machine": "arm64",
    "os_name": "posix",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "python_version": "3.13",
    "python_full_version": "3.13.0",
}
ENV_LINUX = {**ENV_MAC, "sys_platform": "linux", "platform_system": "Linux", "platform_machine": "aarch64"}
ENV_WINDOWS = {
    **ENV_MAC,
    "sys_platform": "win32",
    "platform_system": "Windows",
    "platform_machine": "AMD64",
    "os_name": "nt",
}


# --- markers -------------------------------------------------------------------------------------

_MARKER_TOKEN = re.compile(r"\s*(\(|\)|==|!=|<=|>=|<|>|'[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_.]*)")


class MarkerError(ValueError):
    """A marker this reader does not evaluate."""


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text))


def marker_holds(marker: str | None, env: Mapping[str, str], extras: Iterable[str] = ()) -> bool:
    """Evaluate a PEP 508 marker as uv.lock writes them, for one platform (``env``).

    Supports ``and``, ``or``, parentheses and comparisons of a marker variable with a quoted
    string (``==`` and ``!=``, and ``<``, ``<=``, ``>``, ``>=`` on versions); ``extra`` is true for
    any of ``extras``. Raises :class:`MarkerError` on anything else, so a new kind of marker is
    reported instead of being read wrongly. No marker holds everywhere."""
    if not marker:
        return True
    tokens = _MARKER_TOKEN.findall(marker)
    if "".join(tokens).replace(" ", "") != marker.replace(" ", ""):
        raise MarkerError(f"cannot read marker {marker!r}")
    wanted = set(extras)
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise MarkerError(f"marker ends early: {marker!r}")
        pos += 1
        return tokens[pos - 1]

    def operand(tok: str) -> tuple[bool, str]:
        """(is a variable, its name or the literal's text)."""
        if tok[:1] in "'\"":
            return False, tok[1:-1]
        if tok in env or tok == "extra":
            return True, tok
        raise MarkerError(f"unknown marker variable {tok!r} in {marker!r}")

    def comparison() -> bool:
        if peek() == "(":
            take()
            result = disjunction()
            if take() != ")":
                raise MarkerError(f"unbalanced parentheses in {marker!r}")
            return result
        left, op, right = operand(take()), take(), operand(take())
        if op not in ("==", "!=", "<", "<=", ">", ">="):
            raise MarkerError(f"unknown operator {op!r} in {marker!r}")
        if left[0] == right[0]:
            raise MarkerError(f"compare one variable with one string: {marker!r}")
        var, lit = (left[1], right[1]) if left[0] else (right[1], left[1])
        if var == "extra":
            if op not in ("==", "!="):
                raise MarkerError(f"extra compares with == or != only: {marker!r}")
            return (lit in wanted) == (op == "==")
        if op in ("==", "!="):
            return (env[var] == lit) == (op == "==")
        a, b = _version_tuple(env[var]), _version_tuple(lit)
        if not left[0]:
            a, b = b, a
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]

    def conjunction() -> bool:
        result = comparison()
        while peek() == "and":
            take()
            result = comparison() and result
        return result

    def disjunction() -> bool:
        result = conjunction()
        while peek() == "or":
            take()
            result = conjunction() or result
        return result

    result = disjunction()
    if pos != len(tokens):
        raise MarkerError(f"unexpected {tokens[pos]!r} in {marker!r}")
    return result


# --- lock readers --------------------------------------------------------------------------------


def norm_py(name: str) -> str:
    """A Python distribution name normalised as PEP 503 does (``Typing_Extensions`` -> ``typing-extensions``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _uv_source(src: Mapping[str, Any]) -> tuple[str, bool]:
    """(source text, own) for a uv.lock ``source`` table."""
    if src.get("registry") == "https://pypi.org/simple":
        return "pypi.org", False
    if "registry" in src:
        return f"registry {src['registry']}", False
    if "git" in src:
        return f"git {src['git']}", False
    for key in ("editable", "directory", "path", "virtual"):
        if key in src:
            return f"{key} {src[key]} (this repository)", True
    return json.dumps(src, sort_keys=True), False


def read_uv_lock(text: str) -> list[dict[str, Any]]:
    """Every package of a uv.lock, with the scopes the root project gives it on this Mac.

    Scopes: ``runtime`` (the root's dependencies and the extras they ask for), ``group:<name>``
    (each dependency group other than ``dev``), ``dev`` (the dev group), ``extra:<name>`` (the
    root's own extras), each followed through every package's dependencies with markers
    evaluated for macOS arm64 (:data:`ENV_MAC`). A package reached on no scope there is
    ``linux-only`` or ``windows-only`` when that platform's markers reach it, else
    ``unreached``. ``linux-arm64`` is added to the runtime packages that linux/arm64 installs
    too (the Lambda image's platform, which installs capture's and forecast's runtime closures,
    PRD §11.3). Each scope's value says whether the root names the package itself (a direct
    dependency)."""
    lock = tomllib.loads(text)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for p in lock.get("package", []):
        by_name.setdefault(p["name"], []).append(p)

    def find(dep: Mapping[str, Any]) -> dict[str, Any]:
        candidates = by_name.get(dep["name"], [])
        if "version" in dep:
            candidates = [c for c in candidates if c.get("version") == dep["version"]]
        if len(candidates) != 1:
            raise ValueError(f"uv.lock: {len(candidates)} packages match the dependency {dict(dep)!r}")
        return candidates[0]

    roots = [
        p
        for ps in by_name.values()
        for p in ps
        if p.get("source", {}).get("editable") == "." or p.get("source", {}).get("virtual") == "."
    ]
    if len(roots) != 1:
        raise ValueError(f"expected one root project in a uv.lock, found {len(roots)}")
    root = roots[0]

    def closure(starts: Iterable[Mapping[str, Any]], env: Mapping[str, str]) -> dict[int, bool]:
        """{id(package): named by a start} for every package reached from ``starts``."""
        extras_seen: dict[int, set[str]] = {}
        direct: dict[int, bool] = {}
        stack: list[tuple[Mapping[str, Any], bool]] = [(d, True) for d in starts]
        while stack:
            dep, is_direct = stack.pop()
            if not marker_holds(dep.get("marker"), env):
                continue
            pkg = find(dep)
            key = id(pkg)
            extras = set(dep.get("extra", []))
            direct[key] = direct.get(key, False) or is_direct
            if key in extras_seen and extras <= extras_seen[key]:
                continue
            extras_seen.setdefault(key, set()).update(extras)
            nxt = list(pkg.get("dependencies", []))
            for extra in sorted(extras_seen[key]):
                nxt += pkg.get("optional-dependencies", {}).get(extra, [])
            stack += [(d, False) for d in nxt]
        return direct

    scopes: dict[int, dict[str, bool]] = {}

    def add(scope: str, reached: Mapping[int, bool]) -> None:
        for key, is_direct in reached.items():
            if key == id(root):
                continue
            entry = scopes.setdefault(key, {})
            entry[scope] = entry.get(scope, False) or is_direct

    runtime = list(root.get("dependencies", []))
    groups = root.get("dev-dependencies", {})
    extras = root.get("optional-dependencies", {})
    add("runtime", closure(runtime, ENV_MAC))
    for group, members in sorted(groups.items()):
        add("dev" if group == "dev" else f"group:{group}", closure(members, ENV_MAC))
    for extra, members in sorted(extras.items()):
        add(f"extra:{extra}", closure(members, ENV_MAC))
    on_lambda = closure(runtime, ENV_LINUX)
    everything = runtime + [m for ms in groups.values() for m in ms] + [m for ms in extras.values() for m in ms]
    linux_any = closure(everything, ENV_LINUX)
    windows_any = closure(everything, ENV_WINDOWS)
    out = []
    for name in sorted(by_name):
        for p in by_name[name]:
            key = id(p)
            source, own = _uv_source(p.get("source", {}))
            if p is root:
                found = {"root": True}
            elif key in scopes:
                found = dict(scopes[key])
                if "runtime" in found and key in on_lambda:
                    found["linux-arm64"] = found["runtime"]
            elif key in linux_any:
                found = {"linux-only": linux_any[key]}
            elif key in windows_any:
                found = {"windows-only": windows_any[key]}
            else:
                found = {"unreached": False}
            out.append({"name": name, "version": p.get("version", ""), "source": source, "own": own, "scopes": found})
    return out


def read_cargo_lock(text: str) -> list[dict[str, Any]]:
    """Every package of a Cargo.lock: name, version, source, and whether it is the workspace's
    own (no source). Scopes come from ``cargo metadata`` (:func:`cargo_licences`), not the lock."""
    lock = tomllib.loads(text)
    out = []
    for p in lock.get("package", []):
        src = p.get("source")
        shown = "crates.io" if src == "registry+https://github.com/rust-lang/crates.io-index" else src
        out.append(
            {
                "name": p["name"],
                "version": p["version"],
                "source": shown or "path (this repository)",
                "own": src is None,
                "scopes": {},
            }
        )
    return out


def pnpm_key(key: str) -> tuple[str, str]:
    """``'@types/node@24.19.2'`` or ``vite@8.3.6(@types/node@24.19.2)`` -> (name, version)."""
    key = key.strip().strip("'\"")
    base = key.split("(", 1)[0]
    at = base.rfind("@")
    if at <= 0:
        raise ValueError(f"cannot read pnpm package key {key!r}")
    return base[:at], base[at + 1 :]


def _scalar(text: str) -> str:
    return text.strip().strip("'\"")


def read_pnpm_lock(text: str) -> list[dict[str, Any]]:
    """Every package of a pnpm-lock.yaml (lockfile v9), with its scope from the root importer.

    Reads only what the v9 layout needs: ``importers`` > ``.`` > ``dependencies``,
    ``devDependencies`` and ``optionalDependencies`` (name > ``version``); ``packages`` (one key
    per name@version, with ``os`` and ``cpu`` for platform binaries); and ``snapshots`` (each one's
    ``dependencies`` and ``optionalDependencies``). Scopes: ``runtime`` (reached from
    ``dependencies`` or ``optionalDependencies``) and ``dev`` (from ``devDependencies``), each
    saying whether the importer names the package itself; ``platform`` marks a platform binary."""
    section = importer = importer_kind = importer_dep = None
    starts: dict[str, list[tuple[str, str]]] = {"runtime": [], "dev": []}
    platforms: dict[tuple[str, str], dict[str, list[str]]] = {}
    snapshots: dict[tuple[str, str], list[tuple[str, str]]] = {}
    current_pkg: tuple[str, str] | None = None
    current_snap: tuple[str, str] | None = None
    snap_list: str | None = None
    for raw in text.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            section = line.rstrip(":")
        elif section == "importers":
            if indent == 2:
                importer = _scalar(line.rstrip(":"))
            elif indent == 4 and importer == ".":
                importer_kind = line.rstrip(":")
            elif indent == 6 and importer == ".":
                importer_dep = _scalar(line.rstrip(":"))
            elif indent == 8 and importer == "." and importer_dep and line.startswith("version:"):
                version = _scalar(line.split(":", 1)[1]).split("(", 1)[0]
                starts["dev" if importer_kind == "devDependencies" else "runtime"].append((importer_dep, version))
        elif section == "packages":
            if indent == 2:
                current_pkg = pnpm_key(line.rstrip(":"))
                platforms[current_pkg] = {}
            elif indent == 4 and current_pkg and line.startswith(("os:", "cpu:")):
                field, value = line.split(":", 1)
                platforms[current_pkg][field] = [_scalar(v) for v in value.strip().strip("[]").split(",") if v.strip()]
        elif section == "snapshots":
            if indent == 2:
                current_snap = pnpm_key(line.rstrip(":"))
                snapshots.setdefault(current_snap, [])
                snap_list = None
            elif indent == 4 and current_snap:
                snap_list = line.rstrip(":") if line.endswith(":") else None
            elif indent == 6 and current_snap and snap_list in ("dependencies", "optionalDependencies"):
                name, value = line[1:].split("':", 1) if line.startswith("'") else line.split(":", 1)
                snapshots[current_snap].append((_scalar(name), _scalar(value).split("(", 1)[0]))
    scopes: dict[tuple[str, str], dict[str, bool]] = {}
    for scope, roots in starts.items():
        stack = [(r, True) for r in roots]
        seen: set[tuple[str, str]] = set()
        while stack:
            key, is_direct = stack.pop()
            entry = scopes.setdefault(key, {})
            entry[scope] = entry.get(scope, False) or is_direct
            if key not in seen:
                seen.add(key)
                stack += [(k, False) for k in snapshots.get(key, [])]
    out = []
    for key in sorted(platforms):
        name, version = key
        found = dict(scopes.get(key, {"unreached": False}))
        if platforms[key]:
            found["platform"] = False
        out.append(
            {
                "name": name,
                "version": version,
                "source": "registry.npmjs.org",
                "own": False,
                "scopes": found,
                "platform": platforms[key],
            }
        )
    return out


def read_tf_lock(text: str) -> list[dict[str, Any]]:
    """Every provider of a .terraform.lock.hcl: its address and version."""
    out = []
    for m in re.finditer(r'provider\s+"([^"]+)"\s*\{(.*?)\n\}', text, re.S):
        version = re.search(r'^\s*version\s*=\s*"([^"]+)"', m.group(2), re.M)
        out.append(
            {
                "name": m.group(1),
                "version": version.group(1) if version else "",
                "source": f"Terraform registry ({m.group(1).split('/')[0]})",
                "own": False,
                "scopes": {"provider": True},
            }
        )
    return out


READERS = {
    "uv.lock": read_uv_lock,
    "Cargo.lock": read_cargo_lock,
    "pnpm-lock.yaml": read_pnpm_lock,
    ".terraform.lock.hcl": read_tf_lock,
}
ECOSYSTEM = {"uv.lock": "pypi", "Cargo.lock": "crates", "pnpm-lock.yaml": "npm", ".terraform.lock.hcl": "terraform"}


def repo_paths(root: Path, *, untracked: bool = True) -> list[str]:
    """The repo's files: those git tracks (its index, so staged new files count), plus, with
    ``untracked``, the untracked ones git does not ignore."""
    others = ["--others", "--exclude-standard"] if untracked else []
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", *others], check=True, capture_output=True
    )
    return sorted({p for p in out.stdout.decode().split("\0") if p})


def _is_lock(path: str) -> bool:
    return path.rsplit("/", 1)[-1] in LOCK_NAMES


def lock_files(root: Path) -> list[str]:
    """Every lock file git tracks, by its basename (:data:`LOCK_NAMES`). ``check`` and ``build``
    read those that are on disk (:func:`read_locks`). An untracked one is another package's
    uncommitted work (:func:`untracked_lock_files`)."""
    return [p for p in repo_paths(root, untracked=False) if _is_lock(p)]


def untracked_lock_files(root: Path) -> list[str]:
    """Lock files on disk that git does not track yet (and does not ignore). Both commands name
    them in a note and read them once they are added."""
    tracked = set(lock_files(root))
    return [p for p in repo_paths(root) if _is_lock(p) and p not in tracked]


def missing_lock_files(root: Path) -> list[str]:
    """Lock files git tracks that are gone from disk (deleted, not yet removed from git). They pin
    nothing: ``check`` reports any entry the ledger still lists for one."""
    return [p for p in lock_files(root) if not (root / p).is_file()]


def lock_notes(root: Path) -> list[str]:
    """The notes both commands print: each untracked lock file, which neither reads until it is
    added, and each tracked one missing on disk, which pins nothing."""
    notes = []
    for path in untracked_lock_files(root):
        todo = "run build" if path in SHIPS else "say in SHIPS where its packages go, then run build"
        notes.append(f"note: {path} is not tracked by git, so it is not read; once it is added, {todo}")
    for path in missing_lock_files(root):
        notes.append(f"note: {path} is tracked by git but missing on disk, so it pins nothing")
    return notes


def read_locks(root: Path) -> dict[str, list[dict[str, Any]]]:
    """{lock path: its packages} for every lock file git tracks that is on disk (:func:`lock_files`;
    a tracked one missing on disk pins nothing, :func:`missing_lock_files`)."""
    return {
        path: READERS[path.rsplit("/", 1)[-1]]((root / path).read_text())
        for path in lock_files(root)
        if (root / path).is_file()
    }


def origin_account(root: Path) -> str | None:
    """The GitHub account of this repository's ``origin`` remote (the owner's), or None. Read
    only so that :func:`public_source` can keep it out of the ledger."""
    out = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True, check=False
    )
    m = re.search(r"github\.com[:/]+([^/]+)/", out.stdout) if out.returncode == 0 else None
    return m.group(1) if m else None


def public_source(source: str, account: str | None) -> str:
    """``source`` with the GitHub account ``account`` (the owner's) written ``<owner>``, so the
    ledger never carries the owner's handle; any other account is left as it is."""
    if not account:
        return source
    return re.sub(rf"(github\.com[:/]+){re.escape(account)}(?=/)", r"\1<owner>", source, flags=re.I)


# --- licences ------------------------------------------------------------------------------------

_LICENCE_TEXTS = (
    ("Apache-2.0", ("Apache License", "Version 2.0")),
    ("MPL-2.0", ("Mozilla Public License Version 2.0",)),
    ("MIT", ("Permission is hereby granted, free of charge",)),
    ("BSD-3-Clause", ("Redistribution and use in source and binary forms", "Neither the name")),
    ("BSD-2-Clause", ("Redistribution and use in source and binary forms",)),
)


def licence_from_text(text: str) -> str | None:
    """Which licence a licence file's text is, for the few texts the ledger meets; else None."""
    for spdx, needles in _LICENCE_TEXTS:
        if all(n in text for n in needles):
            return spdx
    return None


#: Licence names that wheels write in ``License`` instead of an SPDX id, read as that id.
LICENCE_NAMES = {
    "apache license version 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "mit license": "MIT",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-3-clause license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
}


def spdx_like(text: str) -> bool:
    """True if ``text`` is only licence ids (:data:`_LICENCE_ID`), operators and parentheses."""
    tokens = text.replace("(", " ").replace(")", " ").split()
    return bool(tokens) and all(t in ("AND", "OR", "WITH") or _LICENCE_ID.fullmatch(t) for t in tokens)


def metadata_licence(dist_info: Path) -> tuple[str | None, str]:
    """(licence, how it was read) from a wheel's dist-info folder: ``License-Expression``, else a
    ``License`` that is an SPDX expression or a name in :data:`LICENCE_NAMES`, else, when
    ``License`` is missing or ``UNKNOWN``, a licence file's text (:func:`licence_from_text`).
    Anything else (a free-text ``License`` such as "Dual License", or a copyright line) gives no
    licence: the reason names the field and the classifiers, and ``licence_overrides`` in
    licences.json carries the reviewed reading."""
    head = (dist_info / "METADATA").read_text(errors="replace").split("\n\n", 1)[0]
    fields: dict[str, list[str]] = {}
    for line in head.split("\n"):
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            fields.setdefault(k.strip(), []).append(v.strip())
    if fields.get("License-Expression"):
        return fields["License-Expression"][0], "METADATA License-Expression"
    lic = (fields.get("License") or [""])[0]
    if lic and spdx_like(lic):
        return lic, "METADATA License"
    if lic.lower() in LICENCE_NAMES:
        return LICENCE_NAMES[lic.lower()], f"METADATA License {lic!r}"
    classifiers = [c.split("::")[-1].strip() for c in fields.get("Classifier", []) if c.startswith("License ::")]
    if not lic or lic.upper() == "UNKNOWN":
        for f in sorted(dist_info.rglob("*")):
            if f.is_file() and re.match(r"(?i)(licen[cs]e|copying)", f.name):
                found = licence_from_text(f.read_text(errors="replace"))
                if found:
                    return found, f"its licence file {f.relative_to(dist_info).as_posix()} (METADATA names none)"
    said = f"License {lic[:60]!r}" if lic else "no License"
    return None, f"METADATA has {said} and classifiers {classifiers or 'none'}: reviewed reading needed"


def _dist_infos(root: Path) -> dict[tuple[str, str], Path]:
    """{(normalised name, version): dist-info folder} from every uv project's venv, then uv's cache."""
    index: dict[tuple[str, str], Path] = {}
    folders = [(root / p).parent / ".venv" for p in sorted(SHIPS) if p.endswith("uv.lock")]
    sites = [s for v in folders for s in sorted(v.glob("lib/python3*/site-packages"))]
    sites.append(Path(os.environ.get("UV_CACHE_DIR") or Path.home() / ".cache" / "uv") / "archive-v0")
    for site in sites:
        for d in sorted(site.glob("*.dist-info")) + sorted(site.glob("*/*.dist-info")):
            m = re.match(r"^(.+?)-([^-]+)\.dist-info$", d.name)
            if m and (d / "METADATA").is_file():
                index.setdefault((norm_py(m.group(1)), m.group(2)), d)
    return index


def python_licences(root: Path, packages: Sequence[dict[str, Any]], index: Mapping[tuple[str, str], Path]) -> None:
    """Fill ``licence`` and ``licence_source`` of a uv.lock's packages, in place, from installed wheels."""
    for pkg in packages:
        where = index.get((norm_py(pkg["name"]), pkg["version"]))
        if where is None:
            pkg["licence"], pkg["licence_source"] = None, "not installed on this Mac (no wheel in a venv or uv's cache)"
            continue
        lic, how = metadata_licence(where)
        shown = where.relative_to(root).as_posix() if where.is_relative_to(root) else f"uv cache, {where.name}"
        pkg["licence"], pkg["licence_source"] = lic, f"{how} ({shown})"


def cargo_licences(root: Path, lock_path: str, packages: Sequence[dict[str, Any]]) -> None:
    """Fill licences and Cargo scopes, in place, from ``cargo metadata --offline --locked``.

    Scopes, for macOS arm64 (``--filter-platform aarch64-apple-darwin``): ``workspace`` for the
    members; ``normal`` for crates a member reaches through normal edges only; ``build`` for the
    crates the build edges of those reach (and their dependencies), when no normal path does;
    ``dev`` for the rest that a member's dev edges reach; ``other-platform`` for crates this Mac
    never builds. Each scope's value says whether a member names the crate itself."""
    workdir = (root / lock_path).parent
    base = ["cargo", "metadata", "--offline", "--locked", "--format-version", "1"]
    full = json.loads(subprocess.run(base, cwd=workdir, check=True, capture_output=True).stdout)
    mac = json.loads(
        subprocess.run(
            [*base, "--filter-platform", "aarch64-apple-darwin"], cwd=workdir, check=True, capture_output=True
        ).stdout
    )
    by_nv = {(p["name"], p["version"]): p for p in full["packages"]}
    nodes = {n["id"]: n for n in mac["resolve"]["nodes"]}
    names = {p["id"]: (p["name"], p["version"]) for p in mac["packages"]}
    members = set(mac["workspace_members"])

    def kinds(dep: Mapping[str, Any]) -> set[str]:
        return {k["kind"] or "normal" for k in dep["dep_kinds"]}

    def walk(starts: Iterable[str], follow: set[str]) -> set[str]:
        seen: set[str] = set()
        stack = list(starts)
        while stack:
            nid = stack.pop()
            if nid not in seen:
                seen.add(nid)
                stack += [d["pkg"] for d in nodes[nid]["deps"] if kinds(d) & follow]
        return seen

    def targets(starts: Iterable[str], kind: str) -> set[str]:
        return {d["pkg"] for s in starts for d in nodes[s]["deps"] if kind in kinds(d)}

    normal = walk(members, {"normal"})
    build = walk(targets(normal, "build"), {"normal", "build"}) - normal
    dev = walk(targets(members, "dev"), {"normal", "build"}) - normal - build
    named: dict[str, set[str]] = {}
    for m in members:
        for d in nodes[m]["deps"]:
            named.setdefault(d["pkg"], set()).update(kinds(d))
    scope_of: dict[tuple[str, str], dict[str, bool]] = {}
    for nid, nv in names.items():
        if nid in members:
            scope_of[nv] = {"workspace": True}
        elif nid in normal:
            scope_of[nv] = {"normal": "normal" in named.get(nid, set())}
        elif nid in build:
            scope_of[nv] = {"build": "build" in named.get(nid, set())}
        elif nid in dev:
            scope_of[nv] = {"dev": "dev" in named.get(nid, set())}
    for pkg in packages:
        nv = (pkg["name"], pkg["version"])
        pkg["scopes"] = scope_of.get(nv, {"other-platform": False})
        meta = by_nv.get(nv)
        if meta is None:
            pkg["licence"], pkg["licence_source"] = None, "not in cargo metadata"
        elif meta.get("license"):
            pkg["licence"], pkg["licence_source"] = meta["license"], "Cargo.toml (cargo metadata --offline)"
        elif meta.get("license_file"):
            pkg["licence"], pkg["licence_source"] = None, "Cargo.toml license-file (not read)"
        else:
            pkg["licence"], pkg["licence_source"] = None, "Cargo.toml declares none"


def npm_licences(root: Path, lock_path: str, packages: Sequence[dict[str, Any]]) -> None:
    """Fill licences, in place, from each installed package's package.json under node_modules/.pnpm
    (``<name, with / as +>@<version>``, with a ``_<peers>`` suffix when it has peers)."""
    store = (root / lock_path).parent / "node_modules" / ".pnpm"
    for pkg in packages:
        folder = pkg["name"].replace("/", "+") + "@" + pkg["version"]
        hits = [
            d / "node_modules" / pkg["name"] / "package.json"
            for d in sorted(store.glob(folder + "*"))
            if d.name == folder or d.name.startswith(folder + "_")
        ]
        hits = [h for h in hits if h.is_file()]
        pkg["installed"] = bool(hits)
        if not hits:
            pkg["licence"], pkg["licence_source"] = (
                None,
                "not installed on this Mac (another platform's optional package)",
            )
            continue
        data = json.loads(hits[0].read_text())
        lic = data.get("license")
        if isinstance(lic, dict):
            lic = lic.get("type")
        if not lic and isinstance(data.get("licenses"), list):
            lic = " OR ".join(str(x.get("type")) for x in data["licenses"] if isinstance(x, dict))
        shown = hits[0].relative_to(root).as_posix()
        pkg["licence"], pkg["licence_source"] = lic or None, f"package.json license ({shown})"


def tf_licences(packages: Sequence[dict[str, Any]]) -> None:
    """Fill licences, in place, from the provider's LICENSE.txt in the Terraform plugin cache."""
    cache = os.environ.get("TF_PLUGIN_CACHE_DIR") or ""
    if not cache and os.environ.get("SNOW_GLOBE_DATA_DIR"):
        cache = os.path.join(os.environ["SNOW_GLOBE_DATA_DIR"], "cache", "terraform-plugins")
    for pkg in packages:
        pkg["licence"], pkg["licence_source"] = (
            None,
            "plugin cache not given ($TF_PLUGIN_CACHE_DIR or $SNOW_GLOBE_DATA_DIR)",
        )
        for lic in sorted(Path(cache).glob(f"{pkg['name']}/{pkg['version']}/*/LICENSE*")) if cache else []:
            found = licence_from_text(lic.read_text(errors="replace"))
            if found:
                pkg["licence"] = found
                pkg["licence_source"] = f"its LICENSE.txt in the plugin cache ({lic.parent.name} package)"
                break


# --- THIRD_PARTY_NOTICES -------------------------------------------------------------------------

_KIND_WORDS = {"normal", "dev", "build", "runtime", "development"}
_LICENCE_ID = re.compile(
    r"(MIT(-[0-9A-Za-z]+)?|Apache-2\.0|BSD-[0-9]-Clause|0BSD|ISC|Zlib|MPL-2\.0|CC0-1\.0|CC-BY-[0-9.]+|Unlicense|"
    r"Unicode-[0-9.]+|PSF-2\.0|BSL-1\.0|L?GPL-[0-9.]+(-or-later|-only)?|BUSL-1\.1|X11|NCSA)"
)


def notices_claims(lines: Sequence[str], name: str, version: str) -> list[tuple[int, str | None]]:
    """(line number, licence text or None) for every THIRD_PARTY_NOTICES line that names ``name``
    with ``version`` as docs/checks' ``notices`` check matches them (the name, then only spaces
    and ``:``, ``@``, ``=`` or ``(``, then the version). The licence text is what follows the
    version on that line: a kind word (``normal``, ``dev``, ...) is skipped, a parenthesis is read
    inside, and an expression that ends in AND, OR or WITH goes on into the next line."""
    pair = re.compile(rf"(?<![\w@/.-]){re.escape(name)}[\s:@=(]+{re.escape(version)}(?!\w|[.-]\w)", re.I)
    out: list[tuple[int, str | None]] = []
    for i, line in enumerate(lines):
        m = pair.search(line)
        if not m:
            continue
        text = line[m.end() :]
        if text.lstrip().startswith("("):
            text = text.lstrip()[1:].split(")", 1)[0]
        words: list[str] = []
        j = i
        while True:
            for tok in text.replace("(", " ( ").replace(",", " , ").split():
                if not words and tok.lower() in _KIND_WORDS:
                    continue
                if tok in ("AND", "OR", "WITH") or _LICENCE_ID.fullmatch(tok):
                    words.append(tok)
                    continue
                break
            if words and words[-1] in ("AND", "OR", "WITH") and j + 1 < len(lines):
                j += 1
                text = lines[j]
                continue
            break
        out.append((i + 1, " ".join(words) or None))
    return out


def spdx_key(expr: str | None) -> Any:
    """A comparison key for a licence expression: a set of ids when it is only ORs (``/`` read as
    OR), else its normalised text, so ``MIT OR Apache-2.0`` equals ``Apache-2.0 OR MIT``."""
    if not expr:
        return None
    text = re.sub(r"\s+", " ", expr.replace("/", " OR ").replace("(", " ").replace(")", " ")).strip().upper()
    parts = text.split(" OR ")
    if all(" AND " not in p and " WITH " not in p for p in parts):
        return frozenset(p.strip() for p in parts)
    return text


# --- duties --------------------------------------------------------------------------------------


class LedgerError(ValueError):
    """A licence expression this reader cannot parse, or a duty the hand-kept table lacks."""


def parse_licence(expr: str) -> Any:
    """A licence expression as a tree: ``("id", "MIT")``, ``("with", "Apache-2.0", "LLVM-exception")``,
    ``("and", [...])`` or ``("or", [...])``. WITH binds tightest, then AND, then OR, as in SPDX;
    ``/`` is read as OR (some crates write ``MIT/Apache-2.0``). Raises :class:`LedgerError` on
    anything else, so an unreadable expression is reported instead of guessed."""
    tokens = expr.replace("/", " OR ").replace("(", " ( ").replace(")", " ) ").split()
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise LedgerError(f"licence expression ends early: {expr!r}")
        pos += 1
        return tokens[pos - 1]

    def atom() -> Any:
        tok = take()
        if tok == "(":
            inner = either()
            if take() != ")":
                raise LedgerError(f"unbalanced parentheses in {expr!r}")
            return inner
        if not _LICENCE_ID.fullmatch(tok):
            raise LedgerError(f"not a licence id: {tok!r} in {expr!r}")
        if peek() == "WITH":
            take()
            return ("with", tok, take())
        return ("id", tok)

    def both() -> Any:
        parts = [atom()]
        while peek() == "AND":
            take()
            parts.append(atom())
        return parts[0] if len(parts) == 1 else ("and", parts)

    def either() -> Any:
        parts = [both()]
        while peek() == "OR":
            take()
            parts.append(both())
        return parts[0] if len(parts) == 1 else ("or", parts)

    tree = either()
    if pos != len(tokens):
        raise LedgerError(f"unexpected {tokens[pos]!r} in {expr!r}")
    return tree


def duty_text(tree: Any, rules: Mapping[str, str]) -> str:
    """What a licence expression asks of anyone who distributes the package, from ``rules``
    (``vocabulary.duty_by_licence``): an id as ``MIT (its duty)``; OR as "one of ..." (one
    licence's duty is enough); AND as "all of ..."; an exception (WITH) only adds permissions."""
    if tree[0] in ("id", "with"):
        lid = tree[1]
        if lid not in rules:
            raise LedgerError(f"vocabulary.duty_by_licence in licences.json has no duty for {lid}")
        if tree[0] == "with":
            return f"{lid} WITH {tree[2]} ({rules[lid]}; the exception only adds permissions)"
        return f"{lid} ({rules[lid]})"
    parts = [duty_text(t, rules) for t in tree[1]]
    parts = [f"[{text}]" if t[0] in ("and", "or") else text for text, t in zip(parts, tree[1], strict=True)]
    if tree[0] == "or":
        return "one of " + " or ".join(parts)
    return "all of " + " and ".join(parts)


def package_duty(entry: Mapping[str, Any], rules: Mapping[str, str]) -> str:
    """One package's attribution duty (``vocabulary.duty_rule``): none unless it ships in the app
    or the workers (:data:`DISTRIBUTED`); for the workers, none while they are not distributed,
    with what would apply if they were; for the app, from M1, its licence expression's duties
    (:func:`duty_text`). Snow Globe's own packages point to ``own.snow-globe``."""
    if entry["own"]:
        return "Snow Globe's own: its licence is the owner's to choose (items: own.snow-globe)"
    if not set(entry["ships"]) & set(DISTRIBUTED):
        return "None: not redistributed"
    if not entry["licence"]:
        return "To be read with its terms: its licence was not read here"
    duty = duty_text(parse_licence(entry["licence"]), rules)
    if "app" in entry["ships"]:
        return f"With the app, from M1: {duty}"
    return (
        "None while the workers are not distributed (they run on this Mac and, once AWS is applied, in the "
        f"owner's account); if they ever are: {duty}"
    )


def package_confirm_with(entry: Mapping[str, Any]) -> str | None:
    """Who settles a ``to-confirm`` package, and how; None for any other status."""
    if entry["status"] != "to-confirm":
        return None
    reach = "it ships in the app or the workers" if set(entry["ships"]) & set(DISTRIBUTED) else "never redistributed"
    if entry["licence"]:
        texts = " | ".join(entry["notices"]["licence_text"])
        return (
            f"The lead: {NOTICES} reads {texts}, the package's own metadata {entry['licence']}; "
            f"decide which is right ({reach})"
        )
    if (entry["licence_source"] or "").startswith("not installed"):
        return (
            "The lead, only if it is ever installed here or redistributed: it installs only on another "
            f"platform, so its licence was not read on this Mac, with no network ({reach})"
        )
    return f"The lead: read its licence in its own files and record the reading in licence_overrides ({reach})"


# --- build and check -----------------------------------------------------------------------------


def ships_of(lock_path: str, scopes: Mapping[str, bool]) -> list[str]:
    """The ``ships`` values a lock file's scopes give a package (:data:`SHIPS`), widest first."""
    table = SHIPS[lock_path]
    found = {table[s.split(":", 1)[0]] for s in scopes if s.split(":", 1)[0] in table}
    return sorted(found, key=SHIPS_ORDER.index)


def build(root: Path = ROOT) -> dict[str, Any]:
    """Re-read every tracked lock file and licence, and return the new licences.json (hand-kept
    parts kept). Each package's ``duty`` and, when to-confirm, ``confirm_with`` are generated
    (:func:`package_duty`, :func:`package_confirm_with`); a duty the hand-kept
    ``vocabulary.duty_by_licence`` lacks stops the build, as an unplaced lock file does."""
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    rules: Mapping[str, str] = ledger.get("vocabulary", {}).get("duty_by_licence", {})
    account = origin_account(root)
    locks = read_locks(root)
    unplaced = sorted(set(locks) - set(SHIPS))
    if unplaced:
        raise SystemExit(f"ledger.py: say in SHIPS where these lock files' packages go: {', '.join(unplaced)}")
    index = _dist_infos(root)
    for path, pkgs in locks.items():
        kind = path.rsplit("/", 1)[-1]
        if kind == "uv.lock":
            python_licences(root, pkgs, index)
        elif kind == "Cargo.lock":
            cargo_licences(root, path, pkgs)
        elif kind == "pnpm-lock.yaml":
            npm_licences(root, path, pkgs)
        else:
            tf_licences(pkgs)
    notices = (root / NOTICES).read_text().split("\n")
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path, pkgs in sorted(locks.items()):
        eco = ECOSYSTEM[path.rsplit("/", 1)[-1]]
        for p in pkgs:
            entry = merged.setdefault(
                (eco, p["name"], p["version"]),
                {
                    "ecosystem": eco,
                    "name": p["name"],
                    "version": p["version"],
                    "source": public_source(p["source"], account),
                    "own": p["own"],
                    "licence": None,
                    "licence_source": None,
                    "in": [],
                    "ships": [],
                },
            )
            if p.get("licence") and not entry["licence"]:
                entry["licence"], entry["licence_source"] = p["licence"], p["licence_source"]
            elif not entry["licence"] and not entry["licence_source"]:
                entry["licence_source"] = p.get("licence_source")
            ships = ships_of(path, p["scopes"])
            where: dict[str, Any] = {"lock": path, "scopes": sorted(p["scopes"]), "direct": any(p["scopes"].values())}
            where["ships"] = ships
            if p.get("platform"):
                where["platform"] = p["platform"]
            if "installed" in p:
                where["installed_here"] = p["installed"]
            entry["in"].append(where)
            entry["ships"] = sorted(set(entry["ships"]) | set(ships), key=SHIPS_ORDER.index)
    overrides: dict[str, dict[str, str]] = ledger.get("licence_overrides", {})
    packages = []
    found: list[dict[str, Any]] = []
    for name in sorted(set(overrides) - {" ".join(k) for k in merged}):
        found.append(
            {
                "found_by": "ledger.py",
                "kind": "override-unused",
                "what": name,
                "ledger": "licence_overrides names a package no lock file pins at that version",
                "for_the_lead": "drop or update the override",
            }
        )
    for key in sorted(merged):
        entry = merged[key]
        override = overrides.get(" ".join(key))
        if override:
            entry["read_here"] = {"licence": entry["licence"], "licence_source": entry["licence_source"]}
            entry["licence"] = override["licence"]
            entry["licence_source"] = f"reviewed reading (licence_overrides): {override['why']}"
        claims = [] if entry["own"] else notices_claims(notices, entry["name"], entry["version"])
        texts = sorted({c for _, c in claims if c})
        entry["notices"] = {"lines": [n for n, _ in claims], "licence_text": texts}
        agrees = not texts or any(spdx_key(t) == spdx_key(entry["licence"]) for t in texts)
        if entry["own"]:
            entry["status"] = "own"
        elif entry["licence"] and agrees:
            entry["status"] = "confirmed"
        else:
            entry["status"] = "to-confirm"
        try:
            entry["duty"] = package_duty(entry, rules)
        except LedgerError as exc:
            raise SystemExit(f"ledger.py: {entry['ecosystem']} {entry['name']} {entry['version']}: {exc}") from exc
        confirm_with = package_confirm_with(entry)
        if confirm_with:
            entry["confirm_with"] = confirm_with
        if entry["licence"] and not agrees:
            lines = ", ".join(map(str, entry["notices"]["lines"]))
            found.append(
                {
                    "found_by": "ledger.py",
                    "kind": "licence-differs",
                    "what": f"{entry['ecosystem']} {entry['name']} {entry['version']}",
                    "notices": f"{NOTICES} line(s) {lines}: {' | '.join(texts)}",
                    "ledger": f"{entry['licence']} ({entry['licence_source']})",
                    "for_the_lead": "decide which is right; THIRD_PARTY_NOTICES is not edited here",
                }
            )
        packages.append(entry)
    counts: dict[str, Any] = {}
    for path, pkgs in sorted(locks.items()):
        third = [p for p in pkgs if not p["own"]]
        by_ship: dict[str, int] = {}
        by_scope: dict[str, int] = {}
        for p in third:
            widest = (ships_of(path, p["scopes"]) or ["none on this Mac"])[0]
            by_ship[widest] = by_ship.get(widest, 0) + 1
            for s in p["scopes"]:
                by_scope[s] = by_scope.get(s, 0) + 1
        counts[path] = {
            "packages": len(pkgs),
            "own": len(pkgs) - len(third),
            "third_party": len(third),
            "third_party_by_widest_ships": dict(sorted(by_ship.items())),
            "third_party_by_scope": dict(sorted(by_scope.items())),
        }
    git = ["git", "-C", str(root)]
    commit = subprocess.run([*git, "rev-parse", "--short", "HEAD"], check=True, capture_output=True, text=True)
    changed = subprocess.run([*git, "status", "--porcelain", "--", *locks], check=True, capture_output=True, text=True)
    ledger["generated"] = {
        "by": "docs/ledger/ledger.py build",
        "at_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": commit.stdout.strip(),
        "lock_files": sorted(locks),
        "lock_files_changed_since_commit": bool(changed.stdout.strip()),
        "network": "none",
    }
    ledger["package_counts"] = counts
    ledger["packages"] = packages
    reviewed = [d for d in ledger.get("disagreements", []) if d.get("found_by") != "ledger.py"]
    ledger["disagreements"] = reviewed + found
    return ledger


def check(root: Path = ROOT, ledger_path: Path = LEDGER) -> list[str]:
    """Problems, over the lock files git tracks (:func:`lock_files`): a package a lock file pins
    that the ledger lacks for that lock at that version, or one the ledger lists for a lock that
    does not pin it (an untracked lock file, or a tracked one missing on disk, pins nothing)."""
    ledger = json.loads(ledger_path.read_text())
    have = {(p["ecosystem"], p["name"], p["version"], w["lock"]) for p in ledger.get("packages", []) for w in p["in"]}
    want = set()
    for path, pkgs in read_locks(root).items():
        eco = ECOSYSTEM[path.rsplit("/", 1)[-1]]
        want |= {(eco, p["name"], p["version"], path) for p in pkgs}
    problems = [
        f"{lock}: {eco} {name} {ver} is not in {ledger_path.name}" for eco, name, ver, lock in sorted(want - have)
    ]
    problems += [
        f"{ledger_path.name}: {eco} {name} {ver} is listed for {lock}, which does not pin it"
        for eco, name, ver, lock in sorted(have - want)
    ]
    return problems


def dump(ledger: Mapping[str, Any]) -> str:
    """licences.json's text: indented JSON with ``packages`` last and one line per package, so a
    lock bump changes only the lines of the packages it touches."""
    head = json.dumps({k: v for k, v in ledger.items() if k != "packages"}, indent=1, ensure_ascii=False)
    rows = ",\n".join("  " + json.dumps(p, ensure_ascii=False) for p in ledger.get("packages", []))
    return head[: -len("\n}")] + ',\n "packages": [\n' + rows + "\n ]\n}\n"


def main(argv: Sequence[str] | None = None) -> int:
    """``build`` or ``check`` (the module docstring says what each does)."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("command", choices=("build", "check"))
    args = parser.parse_args(argv)
    if args.command == "build":
        ledger = build()
        LEDGER.write_text(dump(ledger))
        made = sum(1 for d in ledger["disagreements"] if d.get("found_by") == "ledger.py")
        print(f"wrote {LEDGER.relative_to(ROOT)}: {len(ledger['packages'])} packages, {made} generated disagreement(s)")
        for note in lock_notes(ROOT):
            print(note)
        return 0
    problems = check()
    for p in problems:
        print(p)
    for note in lock_notes(ROOT):
        print(note)
    print(f"ledger check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
