# Snow Globe: working rules for Claude Code

**Snow Globe** is a push-to-talk macOS "visual forecast" (repo name: `snow-globe`; product name: Snow Globe since 21 Nov 2026, renamed from Lookout, D-080).

**Documents:**
- [PRD.md](PRD.md) v0.4.2 is the spec (v0.4 plus the factual corrections of 19–20 Nov and the rename of 21 Nov).
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
- The default UA is `SnowGlobe/0.1 (webcam research capture; fetch-on-change)`. It can be overridden with `SNOW_GLOBE_USER_AGENT` (the old `LOOKOUT_USER_AGENT` is still read).
  - The live capture loop has sent that default since the post-gate switch at 00:02 UTC on 23 Nov: the snapshot's code sets it, and its script unsets every override. The request log does not record the UA, so this is checked in the code, not on the wire (D-090). Before the switch, nps.gov saw the old `Lookout/0.1 (webcam research capture; fetch-on-change)` (D-080).

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
  - The lead runs `snow-globe-capture loop --archive-queue` for all 8 cameras (since the post-gate switch; before it, the snapshot's `lookout-capture loop`, D-080, D-090); the Rainier stopgap was retired (D-023). Archive backfill runs only inside that loop, from `state/archive/queue.json`.
  - The loop and a local forecast stand-in run from a frozen snapshot of committed code, with their own venvs, under `~/lookout-data/cache/runtime/<sha>/`. They never use the repo's venvs, so `uv sync` in the repo is safe. Never edit, stop or signal anything under `cache/runtime/`.
  - Since the post-gate switch (23:53 UTC on 22 Nov to 00:03 on 23 Nov, D-090), both loops run from snapshot `aee6f92` (capture 0.3.0 and forecast 0.3.0), and `current` points at it. The switch, `infra/ops/migrate-to-snow-globe.sh`, was a one-time move and refuses to run again.
  - A re-snapshot is the lead's alone, and the one exception to the rule above (D-049, D-090); no other session or agent does any of it. The lead runs it on AC power or, on battery, with the lid open and `caffeinate -i` held across it, because the supervisor's own `caffeinate -s` holds on AC only (D-079, D-089). It follows the procedure in the header of `cache/runtime/bin/supervise.sh`: flip `current`, touch `cache/runtime/graceful-stop.capture`, then SIGTERM the capture loop's python and the forecast loop's bash; the supervisor restarts each from `current`. It needs the supervisor running: without it, a stopped loop stays down. The snapshot's venvs are built offline (`uv sync --locked --no-dev --offline`), so the lead first checks that the build works in a scratch folder, because uv's cache can be emptied (D-090).
  - Commands that fetch from nps.gov (`once`, `loop`, `archive days|day|sample`, `archive-check`) take a host-wide lock and exit with status 3 if another fetcher is active. The lock is taken in the data dir and in each of `~/lookout-data` and `~/snow-globe-data` that exists, each lock file once, matched by device and inode (since the switch both paths reach one file), so it holds whichever data dir a process resolved; another `snow-globe-capture` or `lookout-capture` fetcher process also refuses it (D-080). The offline commands (`status`, `cadence`, `storage-report`, `archive queue add|list|remove|resume`) take no lock.
  - Don't start fetching commands yourself; report what you need fetched (the lead queues archive days).

**NOAA:** use anonymous HTTPS to `noaa-hrrr-bdp-pds.s3.amazonaws.com` (us-east-1) with byte-range GETs. Keep local downloads small: subsets only, and delete GRIB files after extraction.

### Data
- Data lives **outside the repo**. `~/Documents` is iCloud-synced (the `com.apple.file-provider-domain-id` xattr is set).
- Use `$SNOW_GLOBE_DATA_DIR` (the old `$LOOKOUT_DATA_DIR` is still read).
  - The data dir is `~/snow-globe-data`. The post-gate switch moved it there from `~/lookout-data` at 23:58:09 UTC on 22 Nov (D-080, D-090), and `~/lookout-data` is now a symlink to it, so paths under `~/lookout-data` keep working.
  - With neither variable set, the code uses `~/snow-globe-data` if it exists, else `~/lookout-data` if it exists, else `~/snow-globe-data`. While both exist as different folders, every resolver (Python, Rust, TypeScript, the shell scripts and the Makefile) refuses until a variable names the folder meant.
- Never replace the `~/lookout-data` symlink with a real folder, and never remove it: no `rm`, no `mkdir` or copied command that recreates it, no restore into its place. A real folder there would make every resolver refuse to pick a data dir (or, with a variable naming one folder, keep its rate-limit state, logs and forecast lock apart from the running loops'; the NPS fetcher lock still holds, since every fetcher takes it in each default folder that exists); a missing symlink breaks every command and record that names `~/lookout-data`. Commands in the docs keep naming `~/lookout-data`, which works through the symlink; docs/checks fails on a command that hard-codes the new folder (`data-dir-commands`).
- Never write frames, audio, plates, renders, traces or GRIB files inside the repo.
- Never delete anything under `~/lookout-data` or `~/snow-globe-data` except your own temp or cache files.

### Held-out data (D-019, D-022)
Metadata may be read for any date.
- **Archived frames:**
  - Never download, open, view or use an archived frame (full size or thumbnail) dated **1 Jun 2026 or later**: by the camera's local date or the UTC date, until the owner confirms D-038 (D-070).
  - Summer 2026 is design H's held-out season.
- **The sealed window:**
  - Never use any frame from **15 Dec 2026 – 14 Jan 2027**, live or archived, for development.
  - That is design P's sealed window.
- **Quarantine:** never open anything under `~/lookout-data/quarantine/` or `~/snow-globe-data/quarantine/`.

## Toolchain (machine: MacBook Air M4, macOS 26.5.1. Don't upgrade.)

**Python:** `uv` with Python **3.13** (`requires-python = ">=3.13,<3.14"`). Use `httpx`, which uses certifi.
- The python.org `python3` (3.14) has no CA certs, so always run through `uv run`.
- iCloud sets the `hidden` flag on the venvs' `.pth` files, and Python 3.13 skips hidden `.pth` files. Imports then fail outside pytest.
  - Prefix commands with `PYTHONPATH=src:../common/src`.
  - From `eval/`, use `src:../workers/common/src`.
  - The runtime snapshots under `~/lookout-data/cache/runtime/` are outside iCloud, so their venvs need no `PYTHONPATH`.
- Lint and test with `ruff` + `pytest`.
- Mark tests that touch the network `@pytest.mark.network`; they are excluded by default (`-m "not network"`).

**Node:** Node 24 with `pnpm`, TypeScript `strict`, and `vitest`. Headless rendering uses Playwright Chromium with SwiftShader.

**Rust:** stable 1.98. `oculus-core` is a git dependency (`https://github.com/swarit-1/oculus`, rev `1b5410b`, public `main`, MIT; re-pinned from the unpushed `06809e6` on 19 Nov, `ephem.rs` identical). A local copy for reading is at `~/Documents/Oculus/oculus/core`.
- Build with `CARGO_TARGET_DIR=$HOME/lookout-data/cache/cargo-target`. That keeps gigabytes of build output out of iCloud. Since the post-gate switch it is the same folder as `~/snow-globe-data/cache/cargo-target`, through the symlink.

**Other:** Terraform 1.16, Docker 29, Xcode 26.2 / Swift 6.2. There is no AWS CLI and no AWS credentials on this machine.

## Layout, and who edits what

| Path | What | Notes |
|---|---|---|
| `workers/common/` | uv project `snow-globe-common`: data paths, manifest loader and schema validation, storage (local/S3), polite HTTP client, time helpers, JSONL logs | Shared; change only with care and keep its tests green |
| `workers/capture/` | uv project `snow-globe-capture`: NPS live capture, archive backfill, archive check | |
| `workers/forecast/` | uv project `snow-globe-forecast`: HRRR extraction, availability-lag log | |
| `eval/` | uv project `snow-globe-eval`: extractor, scores, baselines, leakage tests, backtest pair | |
| `renderer/` | pnpm package `@snow-globe/renderer`: WebGL2 Tier 1 renderer and headless runner | |
| `app/core/` | Rust crate `snow-globe-core`: parser, time rules, ephemeris wrapper, solar events, trace, lifecycle | Cargo workspace at `app/Cargo.toml` |
| `app/src-tauri/`, `app/src/`, `app/speech-macos/` | Tauri shell, overlay UI, Swift static library | M1 |
| `bench/` | Replay and paint-capture harness, utterance scripts and reports | Voice recordings stay outside git, under `$SNOW_GLOBE_DATA_DIR/fixtures/voice/` |
| `spikes/` | Throwaway platform spikes, such as the M1 1a checks | Never shipped; the lead decides what moves into `app/` |
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

## Data layout under `$SNOW_GLOBE_DATA_DIR`

```
frames/<viewpoint_id>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<sha256[:12]>.jpg (+ .json sidecar)   live, UTC image time, ≤1920 px wide (D-028: every frame at Rainier and Point Reyes; the frame nearest each UTC hour at the other six)
thumbs/<viewpoint_id>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<sha256[:12]>.jpg (+ .json)           640 px thumbnail of every live download (D-028)
archive/<viewpoint_id>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<assetid[:8]>.jpg (+ .json)          NPS archive backfill
capture_log/<YYYY-MM-DD>.jsonl                                                             append-only capture events
state/                                                                                     persisted rate-limit and dedupe state; state/archive/queue.json (backfill queue), state/capture/hosts/www.nps.gov.json (shared 429/503 hold)
forecast/hrrr/<viewpoint_id>/<YYYYMMDDHH>z.json                                            one file per viewpoint per HRRR run
forecast_log/<YYYY-MM-DD>.jsonl                                                            availability lag and fetch events (written only by the forecast stand-in)
forecast_log/lag_survey/<YYYY-MM-DD>.jsonl                                                 S3 lag-survey events, "method": "survey" (written only by snow-globe-forecast lag-survey --log, D-053)
scores/pairs/, renders/pairs/                                                              backtest pair records and renders
spikes/m1/<run-id>.jsonl                                                                   M1 1a spike results (run by the owner)
cache/                                                                                     deletable caches (GRIB subsets, cache/staging/ capture staging, etc.); not cache/runtime/, which holds the live snapshot that `current` names, the loops' scripts, their logs and supervisor.pid (only the lead removes an old snapshot there)
samples/                                                                                   hand-picked sample frames and API responses
reports/                                                                                   saved evidence, kept: reports/m0.5-gate/ holds the gate's data reports and the switch's logs (D-089, D-090)
quarantine/                                                                                never opened (D-022)
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
