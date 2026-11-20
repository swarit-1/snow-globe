"""Offline probe of the Lookout Lambda image (PRD §11.3; run by infra/lambda/smoke.sh, never built into the image).

smoke.sh mounts this file read-only and runs it with the image's own Python, with no network, a read-only root file
system and a non-root user. It never calls a handler, so it is the only local check of the capture handler: that
handler polls nps.gov, and the lead's capture loop is the one NPS fetcher (CLAUDE.md).

It does what the Lambda Python runtime does before the first invoke, and checks the result:

1. puts sys.path in the runtime's order (the functions of the base image's /var/runtime/bootstrap.py);
2. resolves both handler strings with the runtime's own loader (awslambdaric.bootstrap._get_handler), the way
   Lambda reads the function's handler setting;
3. checks that the packages the base image also ships (boto3, botocore, ...) load from the pinned copies in
   /var/task, and that ecCodes loads its native library from there;
4. builds, without using them, what the capture handler builds before its first request: the viewpoint
   manifests, the storage named by LOOKOUT_STORAGE, and the polite HTTP client with the project User-Agent.

It prints one JSON object (every distribution and its version included) and exits 1 if a check fails.
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

TASK_ROOT = os.environ.get("LAMBDA_TASK_ROOT", "/var/task")
RUNTIME_SITE = f"/var/lang/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
HANDLERS = ("lookout_capture.lambda_handler.handler", "lookout_forecast.lambda_handler.handler")
#: Import name -> distribution name, for modules that must come from TASK_ROOT.
PINNED = {
    "boto3": "boto3",
    "botocore": "botocore",
    "s3transfer": "s3transfer",
    "urllib3": "urllib3",
    "httpx": "httpx",
    "certifi": "certifi",
    "numpy": "numpy",
    "eccodes": "eccodes",
    "PIL": "pillow",
    "jsonschema": "jsonschema",
}
DEFAULT_UA = "Lookout/0.1 (webcam research capture; fetch-on-change)"


def runtime_sys_path() -> str:
    """Order sys.path as the base image's bootstrap does; return how it was done."""
    try:
        spec = importlib.util.spec_from_file_location("lambda_bootstrap", "/var/runtime/bootstrap.py")
        assert spec is not None and spec.loader is not None
        boot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(boot)  # defines functions only: its main() is not called
        boot.set_default_sys_path()
        boot.add_default_site_directories()
        return "runtime bootstrap"
    except (AttributeError, AssertionError, FileNotFoundError, ImportError) as exc:
        sys.path.insert(0, TASK_ROOT)  # the documented precedence: LAMBDA_TASK_ROOT first
        return f"fallback (bootstrap internals changed: {exc!r})"


def resolve(name: str) -> Any:
    """The handler object for ``name``, through the runtime's loader when it is there."""
    try:
        from awslambdaric.bootstrap import _get_handler
    except ImportError:
        module, _, attr = name.rpartition(".")
        return getattr(importlib.import_module(module), attr)
    return _get_handler(name)


def dists(path: str) -> list[str]:
    return sorted(f"{d.metadata['Name']}=={d.version}" for d in md.distributions(path=[path]))


def main() -> int:
    failures: list[str] = []
    out: dict[str, Any] = {"python": sys.version.split()[0], "sys_path": runtime_sys_path()}
    out["sys_path_head"] = sys.path[:5]

    handlers = {}
    for name in HANDLERS:
        try:
            fn = resolve(name)
        except Exception as exc:  # the runtime's FaultException: what Lambda would report at init
            handlers[name] = {"error": repr(exc)}
            failures.append(f"{name}: {exc!r}")
            continue
        where = getattr(sys.modules.get(fn.__module__), "__file__", "") or ""
        handlers[name] = {"resolved": f"{fn.__module__}.{fn.__qualname__}", "file": where}
        if not callable(fn) or f"{fn.__module__}.{fn.__qualname__}" != name:
            failures.append(f"{name} resolved to {fn!r}")
        if not where.startswith(TASK_ROOT + "/"):
            failures.append(f"{name} loaded from {where}, not {TASK_ROOT}")
    out["handlers"] = handlers

    loaded = {}
    for module, dist in PINNED.items():
        mod = importlib.import_module(module)
        loaded[module] = f"{md.version(dist)} {mod.__file__}"
        if not (mod.__file__ or "").startswith(TASK_ROOT + "/"):
            failures.append(f"{module} loaded from {mod.__file__}, not {TASK_ROOT}")
    out["loaded_from"] = loaded

    import eccodes

    out["eccodes_api_version"] = eccodes.codes_get_api_version()
    maps = Path("/proc/self/maps").read_text().splitlines()
    libeccodes = sorted({line.split()[-1] for line in maps if "libeccodes.so" in line})
    out["libeccodes"] = libeccodes
    if not libeccodes or not all(p.startswith(TASK_ROOT + "/") for p in libeccodes):
        failures.append(f"libeccodes mapped from {libeccodes}")

    from lookout_common.http import polite_client
    from lookout_common.manifest import load_manifests
    from lookout_common.storage import storage_from_env

    manifests = load_manifests()
    files = sorted(Path(os.environ["LOOKOUT_MANIFEST_DIR"]).glob("*.json"))
    out["manifests"] = sorted(manifests)
    if not manifests or len(manifests) != len(files):
        failures.append(f"{len(manifests)} manifests loaded from {len(files)} files")
    storage = storage_from_env()
    out["storage"] = type(storage).__name__
    client = polite_client()
    try:
        out["user_agent"] = client.headers.get("user-agent")
    finally:
        client.close()
    expected_ua = os.environ.get("LOOKOUT_USER_AGENT") or DEFAULT_UA
    if out["user_agent"] != expected_ua or "@" in (out["user_agent"] or ""):
        failures.append(f"user agent {out['user_agent']!r}")

    out["dists_task_root"] = dists(TASK_ROOT)
    out["dists_runtime_site_packages"] = dists(RUNTIME_SITE)
    out["failures"] = failures
    print(json.dumps(out, indent=1))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
