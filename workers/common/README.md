# lookout-common

Shared helpers for the Lookout workers (`workers/capture`, `workers/forecast`, `eval`).
Python 3.13, uv. Every function has a plain-English docstring naming the PRD section it serves.

```sh
cd workers/common && uv sync && uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

pytest puts `src/` on the import path itself (`pythonpath` in `pyproject.toml`). That matters on
this Mac: iCloud sets the macOS `hidden` flag on the venv's `.pth` files, Python 3.13 skips hidden
`.pth` files, and the editable install then vanishes (`ModuleNotFoundError: lookout_common`). For
anything else, such as the console script, put `src/` on the path by hand:

```sh
PYTHONPATH=src uv run lookout-viewpoints-check            # from workers/common
PYTHONPATH=src:../common/src uv run <command>              # from workers/capture or workers/forecast
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
| `paths` | `data_dir()` (refuses git work trees always, and `~/Documents`/`~/Desktop`/iCloud unless `LOOKOUT_ALLOW_SYNCED_DATA=1`, comparing folders by identity so case variants and firmlinked paths are caught); key builders for the CLAUDE.md data layout, plus `thumb_key()` for the PRD §8.7 thumbnails (`thumbs/<vp>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<sha256[:12]>.jpg`) |
| `storage` | `Storage` protocol, `LocalStorage`, `S3Storage`, `storage_from_env()`: atomic puts, create-only puts, JSONL appends, named locks; the same keys locally and in S3. No delete method, by design |
| `http` | `polite_client()` (project User-Agent pinned on every request and redirect, timeouts, no cookies); `MinIntervalLimiter` (cross-process spacing via `flock` everywhere except on AWS Lambda, `hold()` for one-at-a-time, `defer()` for back-off) |
| `timeutil` | `parse_nps_imagedate`, `parse_nps_archive_time`, `exif_local_to_utc`, with explicit DST gap/fold handling |
| `jsonl` | append-only logs with a UTC `t` on every record; a torn line is skipped and reported (`bad_lines`), never fatal, unless `strict=True` |
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

## NPS archive times, summer included (settled 19 Nov 2026)

`parse_nps_archive_time` reads the archive's `LocalTimeString` as an honest civil-time label (the
default "label" convention). The M0.5 archive check (`docs/research/nps-archive-check.md`, D-024)
confirmed it on all 90 sampled days, winter and summer: NPS's own `SolarElevationAngle` matches a
NOAA solar position at the parsed UTC time, with a mean absolute difference of 0.015–0.065° per
day and a best-fitting clock offset of 0 min on every day. In summer, `ImageCreateDateTime` stays
on local standard time while `LocalTimeString` gives civil time labelled "Daylight", one hour
ahead; the function accepts exactly that case and returns the instant both name. The "wall"
convention stays available. It differs from "label" only for a "Standard" label on a summer date,
which no sample showed.
