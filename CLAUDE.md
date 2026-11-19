# Lookout: working rules for Claude Code

**Lookout** is a push-to-talk macOS "visual forecast" (repo name: `snow-globe`; product name: Lookout).

**Documents:**
- [PRD.md](PRD.md) v0.4 is the spec.
- [PLAN.md](PLAN.md) holds the milestones.
- [DECISIONS.md](DECISIONS.md) holds decisions with evidence, plus the spend ledger.
- [PRD_PROGRESS.md](PRD_PROGRESS.md) tracks progress and measured numbers.

**Go-ahead:** the owner accepted every PRD §1 default on 2026-11-19 (day G), and M0.5 started that day.

## Hard rules

These come from the owner. Do not relax them.

**Numbers:**
- Report real measured numbers. Never tune a benchmark, special-case test inputs, or mock a stage in a measured run.
- Unit tests may use recorded fixtures; a *measured run* may not.
- If a target is missed, report the measured value and the bottleneck.

**Paid services:**
- No paid API calls of any kind (Deepgram, fal, Runware, Anthropic, any image model) unless the owner's go-ahead *and* a key are both present.
- No keys exist yet. Spend so far: $0.

**Secrets:** environment variables only. Never commit keys, `.env` files or credentials.

**Outward-facing actions (forbidden):**
- no `git push`;
- no GitHub repo changes;
- no `terraform apply`;
- no AWS calls that create anything;
- no emails.

Only the lead session commits. Subagents never commit.

**Privacy:**
- Never put the owner's name, email or GitHub handle in requests to external services, including the User-Agent.
- The default UA is `Lookout/0.1 (webcam research capture; fetch-on-change)`. It can be overridden with `LOOKOUT_USER_AGENT`.

**Audio:** never record audio. Voice fixtures come only from the owner.

### Polite collection (PRD §15.3)

**NPS live cameras:**
- Poll the camera JSON or HTTP headers at most every 5 minutes.
- Download an image only when it has changed, and at most 4 images per camera per rolling hour, enforced with persisted state.

**NPS archive (`/airwebcams/api/...`, `/airwebcams/GetAsset/...`):**
- one request at a time, at least 5 s apart;
- resumable, never downloading anything twice;
- with a total size cap.

**General:**
- Back off on errors.
- Never run two NPS fetchers at once.
  - The lead runs `lookout-capture loop` for all 8 cameras; the Rainier stopgap was retired (D-023).
  - Every `lookout-capture` command except `status` takes a host-wide lock and exits with status 3 if another fetcher is active.
  - Don't start capture or archive commands yourself; report what you need fetched.

**NOAA:** use anonymous HTTPS to `noaa-hrrr-bdp-pds.s3.amazonaws.com` (us-east-1) with byte-range GETs. Keep local downloads small: subsets only, and delete GRIB files after extraction.

### Data
- Data lives **outside the repo**. `~/Documents` is iCloud-synced (the `com.apple.file-provider-domain-id` xattr is set).
- Use `$LOOKOUT_DATA_DIR`, defaulting to `~/lookout-data`.
- Never write frames, audio, plates, renders, traces or GRIB files inside the repo.
- Never delete anything under `~/lookout-data` except your own temp or cache files.

### Held-out data (D-019, D-022)
Metadata may be read for any date.
- **Archived frames:**
  - Never download, open, view or use an archived frame (full size or thumbnail) dated **1 Jun 2026 or later**.
  - Summer 2026 is design H's held-out season.
- **The sealed window:**
  - Never use any frame from **15 Dec 2026 – 14 Jan 2027**, live or archived, for development.
  - That is design P's sealed window.
- **Quarantine:** never open anything under `~/lookout-data/quarantine/`.

## Toolchain (machine: MacBook Air M4, macOS 26.5.1. Don't upgrade.)

**Python:** `uv` with Python **3.13** (`requires-python = ">=3.13,<3.14"`). Use `httpx`, which uses certifi.
- The python.org `python3` (3.14) has no CA certs, so always run through `uv run`.
- iCloud sets the `hidden` flag on the venvs' `.pth` files, and Python 3.13 skips hidden `.pth` files. Imports then fail outside pytest.
  - Prefix commands with `PYTHONPATH=src:../common/src`.
  - From `eval/`, use `src:../workers/common/src`.
- Lint and test with `ruff` + `pytest`.
- Mark tests that touch the network `@pytest.mark.network`; they are excluded by default (`-m "not network"`).

**Node:** Node 24 with `pnpm`, TypeScript `strict`, and `vitest`. Headless rendering uses Playwright Chromium with SwiftShader.

**Rust:** stable 1.98. `oculus-core` is a git dependency (`https://github.com/swarit-1/oculus`, rev `06809e6`, MIT, public). A local copy for reading is at `~/Documents/Oculus/oculus/core`.

**Other:** Terraform 1.16, Docker 29, Xcode 26.2 / Swift 6.2. There is no AWS CLI and no AWS credentials on this machine.

## Layout, and who edits what

| Path | What | Notes |
|---|---|---|
| `workers/common/` | uv project `lookout-common`: data paths, manifest loader and schema validation, storage (local/S3), polite HTTP client, time helpers, JSONL logs | Shared; change only with care and keep its tests green |
| `workers/capture/` | uv project `lookout-capture`: NPS live capture, archive backfill, archive check | |
| `workers/forecast/` | uv project `lookout-forecast`: HRRR extraction, availability-lag log | |
| `eval/` | uv project `lookout-eval`: extractor, scores, baselines, leakage tests, backtest pair | |
| `renderer/` | pnpm package `@lookout/renderer`: WebGL2 Tier 1 renderer and headless runner | |
| `app/core/` | Rust crate `lookout-core`: parser, time rules, ephemeris wrapper, solar events, trace, lifecycle | Cargo workspace at `app/Cargo.toml` |
| `app/src-tauri/`, `app/src/`, `app/speech-macos/` | Tauri shell, overlay UI, Swift static library | M1 |
| `viewpoints/` | `schema/viewpoint.schema.json` and `manifests/<viewpoint_id>.json` | Factual corrections allowed; say so in your report |
| `infra/terraform/`, `infra/lambda/` | AWS serverless Terraform, and the Lambda container image | Never applied without the owner |
| `docs/` | Research, council, review notes | |
| Root files (`Makefile`, `.github/`, `.gitignore`, `CLAUDE.md`) | | Lead session or the integration agent only. Report the targets you need instead of editing these. |

Planning docs (`PLAN.md`, `DECISIONS.md`, `PRD_PROGRESS.md`, `PRD.md`) are edited only by the docs agent or the lead session. Put decision notes and measured numbers in your final report.

## Viewpoint IDs (8 NPS cameras)

| `viewpoint_id` | Camera | Feed | NPS archive |
|---|---|---|---|
| `rainier.paradise-mountain` | Park camera, 1920×1080, Axis P1387 | `https://www.nps.gov/webcams-mora/mountain.jpg` (HEAD for `Last-Modified`) | **none**; MSV hero |
| `point-reyes.beach` | Air cam, looks NNE | JSON `featurecontent/ard/webcams/json/porejson.txt`; image `.../images/porelarge.jpg` | `pore`, from 2004 |
| `grand-canyon.yavapai` | Air cam, looks NW | `grcajson.txt` / `grcalarge.jpg` | `grca`, from 2001 |
| `grand-teton.science-center` | Air cam, looks W | `grtejson.txt` | `grte`, from 2011 |
| `yosemite.turtleback` | Air cam, looks SW | `yosejson.txt` | `yose`, from 2005 |
| `acadia.mcfarland` | Air cam, looks NE | `acadjson.txt` | `ACAD`, from 2006 |
| `smokies.look-rock` | Air cam, looks E | `grsmjson.txt` | `grsm`, from 1998 |
| `big-bend.panther-junction` | Air cam, looks N | `bibejson.txt` | `BIBE`, from 2004 |

JSON base: `https://www.nps.gov/featurecontent/ard/webcams/json/`. Images: `https://www.nps.gov/featurecontent/ard/webcams/images/<file>`.

### NPS archive API (found 2026-11-19, via the page script at `https://www.nps.gov/AirWebCams/pore`; codes are case-insensitive, and lower case worked for all 8)
- `GET https://www.nps.gov/airwebcams/api/GetAvailableDays/<code>?<cachebuster>` returns `MinDay`, `MaxDay` and per-year availability.
- `POST https://www.nps.gov/airwebcams/api/Search/Execute` with a JSON body returns up to 300 results per page. An example body and response are in `~/lookout-data/samples/nps-json/`. Each asset has:
  - `AssetID`;
  - `ImageCreateDateTime` (local time);
  - `LocalTimeString`, with the zone name;
  - `SolarElevationAngle` and `TimeOfDay`.
- `GET https://www.nps.gov/airwebcams/GetAsset/<AssetID>/proxy/hires` returns full resolution; `.../thumbmedium.jpg` returns a thumbnail.
- Cadence is 15 minutes, day and night.

## Data layout under `$LOOKOUT_DATA_DIR`

```
frames/<viewpoint_id>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<sha256[:12]>.jpg (+ .json sidecar)   live, UTC image time, ≤1920 px wide
archive/<viewpoint_id>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<assetid[:8]>.jpg (+ .json)          NPS archive backfill
capture_log/<YYYY-MM-DD>.jsonl                                                             append-only capture events
state/                                                                                     persisted rate-limit and dedupe state
forecast/hrrr/<viewpoint_id>/<YYYYMMDDHH>z.json                                            one file per viewpoint per HRRR run
forecast_log/<YYYY-MM-DD>.jsonl                                                            availability lag and fetch events
cache/                                                                                     deletable caches (GRIB subsets, etc.)
samples/                                                                                   hand-picked sample frames and API responses
```

## Commands
- Python, per project: `cd <project> && uv sync && uv run pytest -q && uv run ruff check .`
- Rust: `cd app && cargo test --workspace`
- Renderer: `cd renderer && pnpm install && pnpm test`
- Terraform: `terraform -chdir=infra/terraform init -backend=false && terraform -chdir=infra/terraform validate`
- `make test` runs everything that needs no network, microphone or credentials.

## Style
- Small modules, with tests beside the code. Type hints in Python; `strict` in TypeScript.
- No placeholder logic presented as finished work. Mark stubs `TODO(M<n>)` and list them in your report.
- Plain-English docstrings: say what a function does and which PRD section it implements.
