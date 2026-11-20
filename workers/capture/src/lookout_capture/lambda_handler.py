"""AWS Lambda entry point for the capture job (PRD §11.2 "Capture", §11.3).

EventBridge Scheduler invokes ``lookout_capture.lambda_handler.handler`` every 5 minutes
(``infra/terraform/scheduler.tf``). One invocation runs one pass with
``LOOKOUT_STORAGE=s3``. It is safe under concurrent invocation: each camera is handled
under an S3 conditional-put lock and its persisted poll slot, so a second invocation skips
cameras that are locked or not yet due instead of polling them twice. The pass writes its
``capture_log`` records in one ``append_jsonl_many`` (each S3 append rewrites the day's log),
thumbnails to ``thumbs/``, and stages the six hourly cameras' frames under
``cache/staging/`` (:mod:`.retention`); the role needs those prefixes (README, "Storage").
"""

from __future__ import annotations

from typing import Any

from lookout_common.manifest import load_manifests
from lookout_common.storage import storage_from_env

from .live import CaptureRunner

__all__ = ["handler"]

#: Lambda's timeout is 120 s (infra/terraform/lambda.tf); keep headroom for the last camera.
_MIN_TIME_LEFT_S = 25.0
_WAIT_FOR_DUE_S = 45.0


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """Run one capture pass and return its summary (counts per event, outcome per camera).

    ``event`` may carry ``{"only": ["point-reyes.beach", ...]}`` to limit the cameras.
    """
    payload = event or {}
    storage = storage_from_env()
    viewpoints = load_manifests()
    only = payload.get("only")
    selected = [viewpoints[v] for v in only] if only else list(viewpoints.values())

    def time_left() -> float:
        getter = getattr(context, "get_remaining_time_in_millis", None)
        return getter() / 1000.0 if callable(getter) else float("inf")

    with CaptureRunner.default(storage, runner="lambda") as runner:
        return runner.run_pass(
            selected,
            wait_for_due_s=_WAIT_FOR_DUE_S,
            time_left=time_left,
            min_time_left_s=_MIN_TIME_LEFT_S,
        )
