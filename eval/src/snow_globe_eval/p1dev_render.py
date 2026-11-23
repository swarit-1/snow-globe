"""Batch renders for the dev P1 table (PRD §13.2 "Every pair renders Tier 1 headless", §9.5).

The renderer's batch entry point, ``renderer/scripts/render-batch.ts``, renders a JSON Lines job
list with one headless runner (Chromium with SwiftShader, as every eval render), through the
renderer's own ``renderToFiles``: so each render gets the checks of ``pnpm render`` (the scene
validated by the renderer's ``validateScene``, its renderer version, the plate and sky-mask
files matched to the scene's SHA-256s, outputs outside the repo), and Chromium's launch and the
first frame's compile are paid once per batch.

- :func:`renderer_identity` asks the script for the identity of the rendering code (renderer
  version, SHA-256 of the browser bundle and of the Node-side files, git state); a render is
  reused on resume only when its scene and that identity are unchanged (:mod:`.p1dev`).
- :func:`render_batch` writes the job list, runs the script and returns one result per job,
  keeping the script's whole output in a log beside the jobs. A job with no result line (the
  script died) comes back as failed, with the log's tail.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scene import NODE_MIN_MAJOR, RendererValidatorUnavailableError, node_executable

__all__ = [
    "BATCH_SCRIPT",
    "RenderJob",
    "batch_command",
    "identity_digest",
    "render_batch",
    "renderer_identity",
]

#: The renderer's batch entry point, relative to ``renderer/``.
BATCH_SCRIPT = Path("scripts") / "render-batch.ts"
#: Seconds a batch may take: a fixed allowance for the launch, plus this much per job.
_BATCH_BASE_S = 120.0
_BATCH_PER_JOB_S = 120.0

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class RenderJob:
    """One render: the scene, the plate files it names, and where the PNG and metadata go."""

    id: str
    scene: Path
    plate: Path
    sky_mask: Path | None
    depth: Path | None
    out: Path
    meta: Path

    def to_json(self) -> dict[str, Any]:
        """The job line the batch script reads (absolute paths)."""
        return {
            "id": self.id,
            "scene": str(self.scene.resolve()),
            "plate": str(self.plate.resolve()),
            "sky_mask": None if self.sky_mask is None else str(self.sky_mask.resolve()),
            "depth": None if self.depth is None else str(self.depth.resolve()),
            "out": str(self.out.resolve()),
            "meta": str(self.meta.resolve()),
        }


def _node_major(node: str, runner: Runner) -> int:
    try:
        done = runner([node, "--version"], capture_output=True, text=True, timeout=30, check=True)
        return int(done.stdout.strip().lstrip("v").split(".")[0])
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RendererValidatorUnavailableError(f"cannot read {node} --version: {exc}") from exc


def batch_command(
    renderer_dir: Path, *, node: str | None = None, runner: Runner = subprocess.run
) -> list[str]:
    """``[node, renderer/scripts/render-batch.ts]``, after checking Node 24+ and the script."""
    executable = node or node_executable()
    if executable is None:
        raise RendererValidatorUnavailableError("node not found (set SNOW_GLOBE_NODE)")
    major = _node_major(executable, runner)
    if major < NODE_MIN_MAJOR:
        raise RendererValidatorUnavailableError(f"node {major} found; need {NODE_MIN_MAJOR}+")
    script = renderer_dir / BATCH_SCRIPT
    if not script.is_file():
        raise RendererValidatorUnavailableError(f"{script} not found")
    return [executable, str(script)]


def _lines(stdout: str) -> list[dict[str, Any]]:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and str(item.get("kind", "")).startswith("render-batch-"):
            out.append(item)
    return out


def renderer_identity(
    renderer_dir: Path, *, node: str | None = None, runner: Runner = subprocess.run
) -> dict[str, Any]:
    """The rendering code's identity, from ``render-batch.ts --identity`` (renders nothing)."""
    command = [*batch_command(renderer_dir, node=node, runner=runner), "--identity"]
    try:
        done = runner(command, capture_output=True, text=True, timeout=120, cwd=str(renderer_dir))
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"{shlex.join(command)}: {exc}") from exc
    starts = [i for i in _lines(done.stdout or "") if i.get("kind") == "render-batch-start"]
    if done.returncode != 0 or not starts:
        raise RuntimeError(
            f"{shlex.join(command)} exited {done.returncode}: {(done.stderr or '')[-1000:]}"
        )
    identity = dict(starts[0])
    identity.pop("kind", None)
    identity.pop("jobs", None)
    identity.pop("backend", None)
    return identity


def render_batch(
    jobs: Sequence[RenderJob],
    *,
    renderer_dir: Path,
    log_path: Path,
    backend: str = "swiftshader",
    node: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Render ``jobs`` with one runner; returns ``results`` by job id, the identity and the log."""
    if not jobs:
        return {"results": {}, "start": None, "summary": None, "returncode": 0, "log": None}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path = log_path.with_suffix(".jobs.jsonl")
    jobs_path.write_text(
        "".join(json.dumps(job.to_json()) + "\n" for job in jobs), encoding="utf-8"
    )
    command = [
        *batch_command(renderer_dir, node=node, runner=runner),
        "--jobs",
        str(jobs_path),
        "--backend",
        backend,
    ]
    timeout = _BATCH_BASE_S + _BATCH_PER_JOB_S * len(jobs)
    started = time.monotonic()
    try:
        done = runner(
            command, capture_output=True, text=True, timeout=timeout, cwd=str(renderer_dir)
        )
        returncode, stdout, stderr = done.returncode, done.stdout or "", done.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        returncode, stdout, stderr = -1, "", f"{type(exc).__name__}: {exc}"
    wall = time.monotonic() - started
    log_path.write_text(
        f"$ {shlex.join(command)}\n# exit {returncode} after {wall:.2f} s\n"
        f"--- stdout\n{stdout}\n--- stderr\n{stderr}\n",
        encoding="utf-8",
    )
    items = _lines(stdout)
    start = next((i for i in items if i.get("kind") == "render-batch-start"), None)
    summary = next((i for i in items if i.get("kind") == "render-batch-summary"), None)
    results: dict[str, dict[str, Any]] = {
        str(i["id"]): i for i in items if i.get("kind") == "render-batch-result"
    }
    for job in jobs:
        if job.id not in results:
            results[job.id] = {
                "kind": "render-batch-result",
                "id": job.id,
                "ok": False,
                "out": str(job.out),
                "meta": str(job.meta),
                "sha256": None,
                "error": f"no result line (exit {returncode}): {stderr[-1000:]}",
            }
    return {
        "results": results,
        "start": start,
        "summary": summary,
        "returncode": returncode,
        "wall_s": round(wall, 3),
        "log": str(log_path),
    }


def identity_digest(identity: Mapping[str, Any] | None) -> str | None:
    """The identity's one-line digest (None when unknown)."""
    if not identity:
        return None
    value = identity.get("digest")
    return None if value is None else str(value)
