"""``snow-globe-eval ephem-validate``: the M2 ephemeris validation (PRD §8.4; :mod:`.run`).

Registered on eval's command line by :func:`register`; :func:`handle` runs it. The first run
fetches the Horizons tables and the DE421 kernel (politely, once) and builds Skyfield's
isolated environment with uv; every later run, or any run with ``--offline``, reads the cache
under ``<data dir>/cache/ephem/`` and needs no network.

Exit status: 0 when every target is met, 1 when one is missed (the report is written either
way and names the cases), 2 on an error (eval's ``main``).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

__all__ = ["handle", "register"]


def register(sub: Any) -> None:
    """Add the ``ephem-validate`` subcommand to eval's subparsers."""
    parser = sub.add_parser(
        "ephem-validate",
        help="oculus-core vs JPL Horizons (12 months, 8 viewpoints), Skyfield and NOAA",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="fetch nothing and run nothing that needs the network: every input must be cached",
    )
    parser.add_argument(
        "--replan",
        action="store_true",
        help="read the camera positions from the manifests again (fetches tables for moved sites)",
    )
    parser.add_argument("--no-skyfield", action="store_true", help="skip the Skyfield stage")
    parser.add_argument("--no-camera", action="store_true", help="skip the camera-frame stage")
    parser.add_argument("--cache", type=Path, help="cache folder (default: <data dir>/cache/ephem)")
    parser.add_argument(
        "--out", type=Path, help="report folder (default: <data dir>/reports/ephem/<UTC stamp>)"
    )
    parser.set_defaults(handler=handle)


def handle(args: argparse.Namespace) -> int:
    """Run the validation; print the verdict and the report folder.

    Returns 0 when every target is met and 1 when one is missed (see the module docstring).
    """
    from snow_globe_common.paths import data_dir

    from .run import RunOptions, run_validation, verdict_met

    root = data_dir()
    options = RunOptions(
        cache_root=(args.cache or root / "cache" / "ephem").expanduser(),
        reports_root=root / "reports" / "ephem",
        offline=args.offline,
        replan=args.replan,
        skyfield=not args.no_skyfield,
        camera=not args.no_camera,
        out_dir=args.out.expanduser() if args.out else None,
    )
    report = run_validation(options)
    met = verdict_met(report["verdict"])
    print(
        json.dumps(
            {"out_dir": report["out_dir"], "all_met": met, "verdict": report["verdict"]}, indent=2
        )
    )
    return 0 if met else 1
