"""Tests of docs/ledger/ledger.py, the licence ledger's lock readers (PRD §15.4).

Run with the standard library only (Python 3.11 or later, for ``tomllib``)::

    python3 -I -B -m unittest discover -s docs/ledger -t docs/ledger

The readers and the duty rule are tested on small in-memory inputs. ``Check`` runs ``check()``
on a throwaway git repository, so a ledger that misses a package or lists one a lock no longer
pins is caught, an untracked lock file is left alone, and a tracked one deleted from disk pins
nothing instead of stopping the check. ``RealLedger`` checks that licences.json
lists every package of every tracked lock file at its version (this package's acceptance), that
every entry carries its duty, and that the owner's GitHub account is written nowhere. Both are
skipped where git is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any, ClassVar

import ledger

UV_LOCK = textwrap.dedent(
    """
    version = 1
    revision = 3
    requires-python = "==3.13.*"

    [[package]]
    name = "app"
    version = "0.1.0"
    source = { editable = "." }
    dependencies = [
        { name = "lib", extra = ["fast"] },
        { name = "winonly", marker = "sys_platform == 'win32'" },
    ]

    [package.dev-dependencies]
    dev = [
        { name = "tester" },
    ]
    docs = [
        { name = "docgen" },
    ]

    [[package]]
    name = "lib"
    version = "1.0.0"
    source = { registry = "https://pypi.org/simple" }
    dependencies = [
        { name = "core" },
    ]

    [package.optional-dependencies]
    fast = [
        { name = "speedups", marker = "sys_platform != 'win32'" },
    ]

    [[package]]
    name = "core"
    version = "2.0.0"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "speedups"
    version = "3.0.0"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "winonly"
    version = "4.0.0"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "tester"
    version = "5.0.0"
    source = { registry = "https://pypi.org/simple" }
    dependencies = [
        { name = "core" },
    ]

    [[package]]
    name = "docgen"
    version = "6.0.0"
    source = { git = "https://example.invalid/docgen?rev=abc#abc" }
    """
)

PNPM_LOCK = textwrap.dedent(
    """\
    lockfileVersion: '9.0'

    importers:

      .:
        dependencies:
          bundler:
            specifier: 1.0.0
            version: 1.0.0
        devDependencies:
          '@types/thing':
            specifier: 2.0.0
            version: 2.0.0
          runner:
            specifier: 3.0.0
            version: 3.0.0(@types/thing@2.0.0)

    packages:

      '@bundler/linux-x64@1.0.0':
        resolution: {integrity: sha512-x}
        cpu: [x64]
        os: [linux]

      '@types/thing@2.0.0':
        resolution: {integrity: sha512-y}

      bundler@1.0.0:
        resolution: {integrity: sha512-z}

      helper@4.0.0:
        resolution: {integrity: sha512-w}

      runner@3.0.0:
        resolution: {integrity: sha512-v}

    snapshots:

      '@bundler/linux-x64@1.0.0':
        optional: true

      '@types/thing@2.0.0': {}

      bundler@1.0.0:
        optionalDependencies:
          '@bundler/linux-x64': 1.0.0

      helper@4.0.0: {}

      runner@3.0.0(@types/thing@2.0.0):
        dependencies:
          helper: 4.0.0
    """
)


def by_name(packages: list[dict]) -> dict[str, dict]:
    return {p["name"]: p for p in packages}


class Markers(unittest.TestCase):
    def test_platforms(self) -> None:
        self.assertTrue(ledger.marker_holds("sys_platform != 'win32'", ledger.ENV_MAC))
        self.assertFalse(ledger.marker_holds("sys_platform != 'win32'", ledger.ENV_WINDOWS))
        self.assertTrue(ledger.marker_holds("sys_platform == 'linux'", ledger.ENV_LINUX))
        self.assertTrue(ledger.marker_holds(None, ledger.ENV_MAC))

    def test_and_or_parentheses(self) -> None:
        mac = ledger.ENV_MAC
        self.assertTrue(ledger.marker_holds("sys_platform == 'darwin' and implementation_name != 'pypy'", mac))
        self.assertFalse(ledger.marker_holds("sys_platform == 'linux' and implementation_name == 'cpython'", mac))
        self.assertTrue(ledger.marker_holds("(sys_platform == 'linux' or sys_platform == 'darwin')", mac))
        self.assertFalse(ledger.marker_holds("sys_platform == 'linux' or (os_name == 'nt')", mac))

    def test_versions_and_extras(self) -> None:
        mac = ledger.ENV_MAC
        self.assertTrue(ledger.marker_holds("python_full_version >= '3.10'", mac))
        self.assertFalse(ledger.marker_holds("python_version < '3.11'", mac))
        self.assertTrue(ledger.marker_holds("'3.14' > python_version", mac))
        self.assertTrue(ledger.marker_holds("extra == 's3'", mac, extras=["s3"]))
        self.assertFalse(ledger.marker_holds("extra == 's3'", mac))

    def test_unknown_markers_are_refused(self) -> None:
        for marker in ("platform_release == '1'", "sys_platform ~= 'x'", "sys_platform == 'darwin' and", "'a' == 'b'"):
            with self.subTest(marker=marker), self.assertRaises(ledger.MarkerError):
                ledger.marker_holds(marker, ledger.ENV_MAC)


class UvLock(unittest.TestCase):
    def setUp(self) -> None:
        self.pkgs = by_name(ledger.read_uv_lock(UV_LOCK))

    def test_every_package_is_listed(self) -> None:
        self.assertEqual(set(self.pkgs), {"app", "lib", "core", "speedups", "winonly", "tester", "docgen"})

    def test_scopes(self) -> None:
        p = self.pkgs
        self.assertEqual(p["app"]["scopes"], {"root": True})
        self.assertTrue(p["app"]["own"])
        self.assertEqual(p["lib"]["scopes"], {"runtime": True, "linux-arm64": True})
        self.assertEqual(p["core"]["scopes"], {"runtime": False, "linux-arm64": False, "dev": False})
        self.assertEqual(p["speedups"]["scopes"], {"runtime": False, "linux-arm64": False}, "an extra's dependency")
        self.assertEqual(p["winonly"]["scopes"], {"windows-only": True})
        self.assertEqual(p["tester"]["scopes"], {"dev": True})
        self.assertEqual(p["docgen"]["scopes"], {"group:docs": True})

    def test_sources(self) -> None:
        self.assertEqual(self.pkgs["lib"]["source"], "pypi.org")
        self.assertTrue(self.pkgs["docgen"]["source"].startswith("git https://example.invalid/"))
        self.assertFalse(self.pkgs["docgen"]["own"])

    def test_two_roots_are_refused(self) -> None:
        twice = UV_LOCK + '\n[[package]]\nname = "other"\nversion = "1"\nsource = { virtual = "." }\n'
        with self.assertRaises(ValueError):
            ledger.read_uv_lock(twice)


class PnpmLock(unittest.TestCase):
    def setUp(self) -> None:
        self.pkgs = by_name(ledger.read_pnpm_lock(PNPM_LOCK))

    def test_every_package_is_listed(self) -> None:
        self.assertEqual(set(self.pkgs), {"@bundler/linux-x64", "@types/thing", "bundler", "helper", "runner"})
        self.assertEqual(self.pkgs["runner"]["version"], "3.0.0")

    def test_scopes(self) -> None:
        p = self.pkgs
        self.assertEqual(p["bundler"]["scopes"], {"runtime": True})
        self.assertEqual(p["@bundler/linux-x64"]["scopes"], {"runtime": False, "platform": False})
        self.assertEqual(p["@bundler/linux-x64"]["platform"], {"cpu": ["x64"], "os": ["linux"]})
        self.assertEqual(p["@types/thing"]["scopes"], {"dev": True})
        self.assertEqual(p["runner"]["scopes"], {"dev": True})
        self.assertEqual(p["helper"]["scopes"], {"dev": False}, "reached through a peer-suffixed snapshot")

    def test_keys(self) -> None:
        self.assertEqual(ledger.pnpm_key("'@types/node@24.19.2'"), ("@types/node", "24.19.2"))
        self.assertEqual(ledger.pnpm_key("vite@8.3.6(@types/node@24.19.2)(esbuild@0.28.2)"), ("vite", "8.3.6"))
        with self.assertRaises(ValueError):
            ledger.pnpm_key("noversion")


class OtherLocks(unittest.TestCase):
    def test_cargo(self) -> None:
        text = textwrap.dedent(
            """
            version = 4

            [[package]]
            name = "mine"
            version = "0.1.0"
            dependencies = ["dep"]

            [[package]]
            name = "dep"
            version = "1.2.3"
            source = "registry+https://github.com/rust-lang/crates.io-index"
            checksum = "00"

            [[package]]
            name = "gitdep"
            version = "0.1.0"
            source = "git+https://example.invalid/repo?rev=abc#abc"
            """
        )
        pkgs = by_name(ledger.read_cargo_lock(text))
        self.assertTrue(pkgs["mine"]["own"])
        self.assertEqual(pkgs["dep"]["source"], "crates.io")
        self.assertEqual(pkgs["gitdep"]["source"], "git+https://example.invalid/repo?rev=abc#abc")

    def test_terraform(self) -> None:
        text = 'provider "registry.terraform.io/hashicorp/aws" {\n  version     = "6.73.0"\n'
        text += '  hashes = [\n    "h1:x",\n  ]\n}\n'
        (p,) = ledger.read_tf_lock(text)
        self.assertEqual((p["name"], p["version"]), ("registry.terraform.io/hashicorp/aws", "6.73.0"))


class Notices(unittest.TestCase):
    LINES: ClassVar[list[str]] = [
        "    chrono         0.4.45    normal   MIT OR Apache-2.0",
        "    Development only: pytest 9.2.0 (MIT), ruff 0.17.5 (MIT),",
        "    numpy              2.5.5       BSD-3-Clause AND 0BSD AND MIT AND Zlib AND",
        "                                   CC0-1.0",
        "    @types/node            24.19.2   MIT           development",
        "    eccodes 2.50.1 and eccodeslib 2.50.0.34 (Apache-2.0, ECMWF)",
        "    foo 1.2.30   MIT",
    ]

    def claims(self, name: str, version: str) -> list[tuple[int, str | None]]:
        return ledger.notices_claims(self.LINES, name, version)

    def test_licence_after_the_version(self) -> None:
        self.assertEqual(self.claims("chrono", "0.4.45"), [(1, "MIT OR Apache-2.0")])
        self.assertEqual(self.claims("pytest", "9.2.0"), [(2, "MIT")])
        self.assertEqual(self.claims("ruff", "0.17.5"), [(2, "MIT")])
        self.assertEqual(self.claims("numpy", "2.5.5"), [(3, "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0")])
        self.assertEqual(self.claims("eccodeslib", "2.50.0.34"), [(6, "Apache-2.0")])
        self.assertEqual(self.claims("eccodes", "2.50.1"), [(6, None)])

    def test_no_match_inside_longer_names_or_versions(self) -> None:
        self.assertEqual(self.claims("node", "24.19.2"), [])
        self.assertEqual(self.claims("@types/node", "24.19.2"), [(5, "MIT")])
        self.assertEqual(self.claims("foo", "1.2.3"), [])

    def test_spdx_key(self) -> None:
        self.assertEqual(ledger.spdx_key("MIT OR Apache-2.0"), ledger.spdx_key("Apache-2.0 OR MIT"))
        self.assertEqual(ledger.spdx_key("MIT/Apache-2.0"), ledger.spdx_key("Apache-2.0 OR MIT"))
        self.assertNotEqual(ledger.spdx_key("MIT AND Apache-2.0"), ledger.spdx_key("MIT OR Apache-2.0"))
        self.assertIsNone(ledger.spdx_key(None))


class Metadata(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def dist(self, head: str, files: dict[str, str] | None = None) -> Path:
        d = self.tmp / f"pkg{len(list(self.tmp.iterdir()))}-1.0.dist-info"
        d.mkdir()
        (d / "METADATA").write_text(head + "\n\nThe long description.\n")
        for name, text in (files or {}).items():
            (d / name).parent.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(text)
        return d

    def test_expression_first(self) -> None:
        d = self.dist("Metadata-Version: 2.4\nName: a\nLicense-Expression: MIT\nLicense: BSD")
        self.assertEqual(ledger.metadata_licence(d), ("MIT", "METADATA License-Expression"))

    def test_names_and_free_text(self) -> None:
        apache = "Classifier: License :: OSI Approved :: Apache Software License"
        d = self.dist(f"Name: b\nLicense: Apache License Version 2.0\n{apache}")
        self.assertEqual(ledger.metadata_licence(d)[0], "Apache-2.0")
        d = self.dist("Name: c\nLicense: Dual License\nClassifier: License :: OSI Approved :: BSD License")
        lic, why = ledger.metadata_licence(d)
        self.assertIsNone(lic)
        self.assertIn("Dual License", why)
        self.assertIn("BSD License", why)

    def test_licence_file_when_metadata_names_none(self) -> None:
        apache = "Apache License\nVersion 2.0, January 2004\n"
        d = self.dist("Name: d\nVersion: 1.0", {"licenses/LICENSE": apache})
        self.assertEqual(ledger.metadata_licence(d)[0], "Apache-2.0")
        self.assertIsNone(ledger.metadata_licence(self.dist("Name: e"))[0])


class Output(unittest.TestCase):
    def test_dump_round_trips_with_one_line_per_package(self) -> None:
        doc = {"kind": "k", "items": [{"id": "x"}], "packages": [{"name": "a", "in": [{"lock": "l"}]}, {"name": "b"}]}
        text = ledger.dump(doc)
        self.assertEqual(json.loads(text), doc)
        self.assertIn('\n  {"name": "a", "in": [{"lock": "l"}]},\n  {"name": "b"}\n ]\n}\n', text)

    def test_ships(self) -> None:
        self.assertEqual(
            ledger.ships_of("eval/uv.lock", {"group:oculus": True, "dev": False}), ["eval-only", "tests-only"]
        )
        self.assertEqual(ledger.ships_of("app/Cargo.lock", {"other-platform": False}), [])
        self.assertEqual(ledger.ships_of("app/Cargo.lock", {"normal": True}), ["app"])

    def test_owner_account_is_written_as_owner(self) -> None:
        src = "git+https://github.com/Someone/oculus?rev=abc#abc"
        self.assertEqual(ledger.public_source(src, "someone"), "git+https://github.com/<owner>/oculus?rev=abc#abc")
        self.assertEqual(ledger.public_source(src, "other"), src, "another account is left as it is")
        self.assertEqual(ledger.public_source(src, None), src)
        self.assertEqual(ledger.public_source("crates.io", "someone"), "crates.io")


RULES = {"MIT": "notice", "Apache-2.0": "licence copy", "Unicode-3.0": "unicode notice"}


def package(**fields: Any) -> dict[str, Any]:
    """A packages[] entry with the fields the duty rule reads, overridden by ``fields``."""
    entry: dict[str, Any] = {
        "own": False,
        "licence": "MIT",
        "licence_source": "METADATA License",
        "ships": ["tests-only"],
        "status": "confirmed",
        "notices": {"lines": [], "licence_text": []},
    }
    entry.update(fields)
    return entry


class Duties(unittest.TestCase):
    def test_parse(self) -> None:
        p = ledger.parse_licence
        self.assertEqual(p("MIT"), ("id", "MIT"))
        self.assertEqual(p("MIT OR Apache-2.0"), ("or", [("id", "MIT"), ("id", "Apache-2.0")]))
        self.assertEqual(p("MIT/Apache-2.0"), p("MIT OR Apache-2.0"))
        self.assertEqual(
            p("(MIT OR Apache-2.0) AND Unicode-3.0"),
            ("and", [("or", [("id", "MIT"), ("id", "Apache-2.0")]), ("id", "Unicode-3.0")]),
        )
        self.assertEqual(
            p("MIT OR Apache-2.0 AND Zlib"), ("or", [("id", "MIT"), ("and", [("id", "Apache-2.0"), ("id", "Zlib")])])
        )
        self.assertEqual(p("Apache-2.0 WITH LLVM-exception"), ("with", "Apache-2.0", "LLVM-exception"))
        for bad in ("MIT OR", "(MIT", "MIT)", "Dual License", ""):
            with self.subTest(expr=bad), self.assertRaises(ledger.LedgerError):
                p(bad)

    def test_duty_text(self) -> None:
        text = ledger.duty_text(ledger.parse_licence("(MIT OR Apache-2.0) AND Unicode-3.0"), RULES)
        self.assertEqual(
            text, "all of [one of MIT (notice) or Apache-2.0 (licence copy)] and Unicode-3.0 (unicode notice)"
        )
        with self.assertRaises(ledger.LedgerError):
            ledger.duty_text(ledger.parse_licence("Zlib"), RULES)

    def test_package_duty(self) -> None:
        duty = ledger.package_duty
        self.assertEqual(duty(package(), RULES), "None: not redistributed")
        self.assertEqual(duty(package(licence=None, ships=["eval-only"]), RULES), "None: not redistributed")
        self.assertEqual(duty(package(licence="Zlib", ships=[]), {}), "None: not redistributed", "no rule needed")
        self.assertEqual(duty(package(ships=["app", "not-shipped"]), RULES), "With the app, from M1: MIT (notice)")
        workers = duty(package(licence="Apache-2.0", ships=["workers", "eval-only"]), RULES)
        self.assertTrue(workers.startswith("None while the workers are not distributed"), workers)
        self.assertTrue(workers.endswith("if they ever are: Apache-2.0 (licence copy)"), workers)
        self.assertIn("own.snow-globe", duty(package(own=True, licence=None, ships=["app"]), RULES))
        self.assertTrue(duty(package(licence=None, ships=["app"]), RULES).startswith("To be read with its terms"))
        with self.assertRaises(ledger.LedgerError):
            duty(package(licence="Zlib", ships=["app"]), RULES)

    def test_confirm_with(self) -> None:
        cw = ledger.package_confirm_with
        self.assertIsNone(cw(package()))
        absent = cw(package(status="to-confirm", licence=None, licence_source="not installed on this Mac (x)"))
        self.assertTrue(absent and absent.startswith("The lead, only if it is ever installed"), absent)
        differs = package(status="to-confirm", notices={"lines": [7], "licence_text": ["BSD-3-Clause"]})
        self.assertIn("BSD-3-Clause", cw(differs) or "")
        self.assertIn("licence_overrides", cw(package(status="to-confirm", licence=None)) or "")


CARGO_LOCK = """\
version = 4

[[package]]
name = "mine"
version = "0.1.0"

[[package]]
name = "dep"
version = "1.2.3"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "other"
version = "2.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
"""


def listed(name: str, version: str, lock: str) -> dict[str, Any]:
    return {"ecosystem": "crates", "name": name, "version": version, "in": [{"lock": lock}]}


@unittest.skipUnless(shutil.which("git"), "needs git")
class Check(unittest.TestCase):
    """check() itself, on a throwaway repository: it must report, not only pass."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.git("init", "-q")
        (self.root / "Cargo.lock").write_text(CARGO_LOCK)
        self.git("add", "Cargo.lock")
        (self.root / "spike").mkdir()
        (self.root / "spike" / "Cargo.lock").write_text(CARGO_LOCK.replace('"other"', '"untracked-only"'))
        self.ledger_path = self.root / "licences.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def check(self, packages: list[dict[str, Any]]) -> list[str]:
        self.ledger_path.write_text(json.dumps({"packages": packages}))
        return ledger.check(self.root, self.ledger_path)

    def test_a_whole_ledger_passes(self) -> None:
        whole = [listed("mine", "0.1.0", "Cargo.lock"), listed("dep", "1.2.3", "Cargo.lock")]
        self.assertEqual(self.check([*whole, listed("other", "2.0.0", "Cargo.lock")]), [])

    def test_a_removed_package_and_an_extra_lock_entry_are_reported(self) -> None:
        packages = [
            listed("mine", "0.1.0", "Cargo.lock"),
            listed("dep", "1.2.3", "Cargo.lock"),
            listed("gone", "9.9.9", "Cargo.lock"),
        ]
        self.assertEqual(
            self.check(packages),
            [
                "Cargo.lock: crates other 2.0.0 is not in licences.json",
                "licences.json: crates gone 9.9.9 is listed for Cargo.lock, which does not pin it",
            ],
        )

    def test_a_changed_version_and_a_wrong_lock_are_reported(self) -> None:
        packages = [
            listed("mine", "0.1.0", "Cargo.lock"),
            listed("dep", "1.2.4", "Cargo.lock"),
            listed("other", "2.0.0", "spike/Cargo.lock"),
        ]
        self.assertEqual(len(self.check(packages)), 4)

    def test_an_untracked_lock_file_is_noted_not_checked(self) -> None:
        self.assertEqual(ledger.lock_files(self.root), ["Cargo.lock"])
        self.assertEqual(ledger.untracked_lock_files(self.root), ["spike/Cargo.lock"])
        whole = [listed(n, v, "Cargo.lock") for n, v in (("mine", "0.1.0"), ("dep", "1.2.3"), ("other", "2.0.0"))]
        self.assertEqual(self.check(whole), [])
        self.git("add", "spike/Cargo.lock")
        self.assertEqual(ledger.untracked_lock_files(self.root), [])
        self.assertIn("spike/Cargo.lock: crates untracked-only 2.0.0 is not in licences.json", self.check(whole))

    def test_a_tracked_lock_file_missing_on_disk_pins_nothing(self) -> None:
        whole = [listed(n, v, "Cargo.lock") for n, v in (("mine", "0.1.0"), ("dep", "1.2.3"), ("other", "2.0.0"))]
        (self.root / "Cargo.lock").unlink()
        self.assertEqual(ledger.lock_files(self.root), ["Cargo.lock"], "git still tracks it")
        self.assertEqual(ledger.missing_lock_files(self.root), ["Cargo.lock"])
        self.assertEqual(ledger.read_locks(self.root), {})
        self.assertEqual(
            self.check(whole),
            [
                f"licences.json: crates {name} {version} is listed for Cargo.lock, which does not pin it"
                for name, version in (("dep", "1.2.3"), ("mine", "0.1.0"), ("other", "2.0.0"))
            ],
        )

    def test_both_commands_note_untracked_and_missing_lock_files(self) -> None:
        (note,) = ledger.lock_notes(self.root)
        self.assertIn("spike/Cargo.lock is not tracked by git", note)
        self.assertIn("say in SHIPS where its packages go", note, "SHIPS does not place spike/Cargo.lock")
        (self.root / "Cargo.lock").unlink()
        notes = ledger.lock_notes(self.root)
        self.assertEqual(len(notes), 2)
        self.assertIn("Cargo.lock is tracked by git but missing on disk, so it pins nothing", notes[1])


@unittest.skipUnless(shutil.which("git"), "needs git")
class RealLedger(unittest.TestCase):
    def test_every_locked_package_is_in_the_ledger(self) -> None:
        self.assertEqual(ledger.check(), [])

    def test_every_lock_file_has_a_place(self) -> None:
        self.assertEqual(sorted(set(ledger.lock_files(ledger.ROOT)) - set(ledger.SHIPS)), [])

    def test_hand_kept_parts_are_whole(self) -> None:
        doc = json.loads(ledger.LEDGER.read_text())
        statuses = set(doc["vocabulary"]["status"])
        ships = set(doc["vocabulary"]["ships"])
        kinds = set(doc["vocabulary"]["kind"])
        for entry in doc["items"]:
            with self.subTest(item=entry["id"]):
                self.assertIn(entry["status"], statuses)
                self.assertIn(entry["kind"], kinds)
                self.assertLessEqual(set(entry["ships"]), ships)
                self.assertTrue(entry["licence"] and entry["use"])
                self.assertTrue(entry.get("duty"), "every item gives its attribution duty")
                if entry["status"] == "to-confirm":
                    self.assertTrue(entry.get("confirm_with"), "a to-confirm item names who confirms it")
        self.assertEqual(len({e["id"] for e in doc["items"]}), len(doc["items"]), "item ids are unique")

    def test_every_package_carries_its_generated_duty(self) -> None:
        doc = json.loads(ledger.LEDGER.read_text())
        statuses = set(doc["vocabulary"]["status"])
        ships = set(doc["vocabulary"]["ships"])
        rules = doc["vocabulary"]["duty_by_licence"]
        for p in doc["packages"]:
            with self.subTest(package=f"{p['ecosystem']} {p['name']} {p['version']}"):
                self.assertIn(p["status"], statuses)
                self.assertLessEqual(set(p["ships"]), ships)
                self.assertEqual(p.get("duty"), ledger.package_duty(p, rules), "duty is current with the rule")
                self.assertEqual(p.get("confirm_with"), ledger.package_confirm_with(p))
                if p["status"] == "to-confirm":
                    self.assertTrue(p.get("confirm_with"), "a to-confirm package names who confirms it")

    def test_the_owner_account_is_written_nowhere(self) -> None:
        account = ledger.origin_account(ledger.ROOT)
        if not account:
            self.skipTest("no GitHub origin remote")
        for name in ("licences.json", "licences.md", "data-flows.md"):
            with self.subTest(file=name):
                self.assertNotIn(account.lower(), (ledger.HERE / name).read_text().lower())


if __name__ == "__main__":
    unittest.main()
