# lookout-common

Shared helpers for the Lookout workers (`workers/capture`, `workers/forecast`, `eval`).
Python 3.13, uv. Every function has a plain-English docstring naming the PRD section it serves.

```sh
cd workers/common && uv sync && uv run pytest -q && uv run ruff check .
```

## Using it from another uv project

```toml
[project]
dependencies = ["lookout-common"]          # add "lookout-common[s3]" for S3Storage

[tool.uv.sources]
lookout-common = { path = "../common", editable = true }   # "../workers/common" from eval/
```

An editable install lets `load_manifests()` find `viewpoints/manifests` by walking up from the
package. Anywhere else (a Lambda image, say), set `LOOKOUT_MANIFEST_DIR`.

## Modules

| Module | What it gives you |
|---|---|
| `paths` | `data_dir()` (refuses git work trees always, and `~/Documents`/`~/Desktop`/iCloud unless `LOOKOUT_ALLOW_SYNCED_DATA=1`); key builders for the CLAUDE.md data layout |
| `storage` | `Storage` protocol, `LocalStorage`, `S3Storage`, `storage_from_env()`: atomic puts, create-only puts, JSONL appends, named locks; the same keys locally and in S3 |
| `http` | `polite_client()` (project User-Agent, timeouts, no cookies); `MinIntervalLimiter` (cross-process spacing via `flock`, `hold()` for one-at-a-time, `defer()` for back-off) |
| `timeutil` | `parse_nps_imagedate`, `parse_nps_archive_time`, `exif_local_to_utc`, with explicit DST gap/fold handling |
| `jsonl` | append-only logs with a UTC `t` on every record |
| `manifest` | `load_manifests()` with JSON Schema (Draft 2020-12) and cross-file checks; typed `Viewpoint` |
| `images` | `dhash64`, `luma_stats`, `chroma_spread`, `fit_width`, `jpeg_bytes`, `exif_summary` |

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `LOOKOUT_DATA_DIR` | `~/lookout-data` | Root of all collected data (never inside the repo) |
| `LOOKOUT_ALLOW_SYNCED_DATA` | unset | `1` allows a data dir under a cloud-synced folder |
| `LOOKOUT_STORAGE` | `local` | `local` or `s3` |
| `LOOKOUT_S3_BUCKET`, `LOOKOUT_S3_PREFIX` | — | S3 location when `LOOKOUT_STORAGE=s3` |
| `LOOKOUT_USER_AGENT` | `Lookout/0.1 (webcam research capture; fetch-on-change)` | Never put a person's name or email here |
| `LOOKOUT_MANIFEST_DIR` | found by walking up | Folder of viewpoint manifests |
| `LOOKOUT_VIEWPOINT_SCHEMA` | `<manifests>/../schema/viewpoint.schema.json` | Schema path |

## Open question carried by `parse_nps_archive_time`

All archive samples so far (19 Nov 2026) are in standard time. Whether summer archive times are
local standard time (the "label" convention, the default) or DST wall time under a fixed Windows
zone name (the "wall" convention) is unverified. Check `SolarElevationAngle` against an ephemeris
for a summer day before using summer archive frames: TODO(M0.5).
