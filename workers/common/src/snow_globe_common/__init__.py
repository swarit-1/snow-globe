"""snow-globe-common: helpers shared by every Snow Globe worker (CLAUDE.md "Layout").

Modules: :mod:`.paths` (data dir and keys), :mod:`.storage` (local/S3),
:mod:`.http` (polite client, rate limiter), :mod:`.timeutil` (NPS/EXIF times),
:mod:`.jsonl` (append-only logs), :mod:`.manifest` (viewpoints), :mod:`.images`,
:mod:`.env` (``SNOW_GLOBE_*`` variables, with the deprecated ``LOOKOUT_*`` names as a fallback).
Import from the submodules; the names below are the most used ones.

The package was ``lookout-common`` (import ``lookout_common``) until 0.1.0; 0.2.0 is the same
code under the Snow Globe names (D-080).
"""

from .env import env_get
from .http import DEFAULT_USER_AGENT, MinIntervalLimiter, polite_client
from .jsonl import append_jsonl
from .manifest import Viewpoint, load_manifest, load_manifests
from .paths import (
    archive_key,
    cache_dir,
    capture_log_key,
    data_dir,
    forecast_key,
    forecast_log_key,
    frame_key,
    sidecar_key,
    state_key,
    thumb_key,
)
from .storage import KeyNotFoundError, LocalStorage, S3Storage, Storage, storage_from_env
from .timeutil import (
    exif_local_to_utc,
    isoformat_z,
    parse_nps_archive_time,
    parse_nps_imagedate,
    utcnow,
)

__version__ = "0.2.0"

__all__ = [
    "DEFAULT_USER_AGENT",
    "KeyNotFoundError",
    "LocalStorage",
    "MinIntervalLimiter",
    "S3Storage",
    "Storage",
    "Viewpoint",
    "__version__",
    "append_jsonl",
    "archive_key",
    "cache_dir",
    "capture_log_key",
    "data_dir",
    "env_get",
    "exif_local_to_utc",
    "forecast_key",
    "forecast_log_key",
    "frame_key",
    "isoformat_z",
    "load_manifest",
    "load_manifests",
    "parse_nps_archive_time",
    "parse_nps_imagedate",
    "polite_client",
    "sidecar_key",
    "state_key",
    "storage_from_env",
    "thumb_key",
    "utcnow",
]
