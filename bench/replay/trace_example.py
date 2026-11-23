"""The schema-2 trace example that app/core/tests/trace_fields.rs pins, for the scorer's tests
(PRD §10.5; bench/replay/README.md "Inputs").

`trace_fields.rs` builds one fully populated trace record by hand on a fake clock and checks that
the Rust writer's JSON equals the raw string `SCHEMA_TWO_EXAMPLE` in the same file. The Python
tests read that string from there, so the writer and this reader share one copy and cannot drift
apart unnoticed. It is a schema example, not a captured trace and not a measurement, and it is
kept inside the Rust test rather than as a trace file in the repository (CLAUDE.md "Data").

Standard library only.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "utterances"))

import prd_reading as pr

TRACE_FIELDS_RS = pr.REPO / "app" / "core" / "tests" / "trace_fields.rs"
_CONST = re.compile(r'^const SCHEMA_TWO_EXAMPLE: &str = r#"(.*?)"#;$', re.S | re.M)


def text() -> str:
    """The example's JSON text, as written in trace_fields.rs."""
    found = _CONST.findall(TRACE_FIELDS_RS.read_text(encoding="utf-8"))
    if len(found) != 1:
        raise LookupError(f"{TRACE_FIELDS_RS}: expected one SCHEMA_TWO_EXAMPLE raw string")
    return found[0]


def load() -> dict[str, Any]:
    """The example, decoded (a fresh copy each call)."""
    obj = json.loads(text())
    if not isinstance(obj, dict):
        raise LookupError(f"{TRACE_FIELDS_RS}: SCHEMA_TWO_EXAMPLE is not a JSON object")
    return obj
