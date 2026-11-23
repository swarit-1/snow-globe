# Data-flow inventory

**Updated:** Mon 23 Nov 2026, from the code at commit `9d372bd` plus the working tree at about 21:40 UTC, and revised after review at about 22:40 UTC.

This is the data-flow inventory of [PRD §15.2](../../PRD.md), kept current as code lands ([PLAN.md](../../PLAN.md), "Ongoing, in every milestone"). It lists every place where data leaves or enters this machine. Its companion is the licence ledger: [licences.md](licences.md) and [licences.json](licences.json).

Every row was found in the code with the greps under "How the hosts were found", not from memory. Every table of flows gives each flow's component, host, what is sent, what is received, when and how often, and the limits that bind it ("none set here" where none does). The tables of flows that exist also give each one's status. Every flow in the planned table is planned, so that table has a Gate column instead, saying what must come first.

| Status | Meaning |
|---|---|
| live | It runs on this Mac today, in the background loops. |
| on demand | The code exists, and a person runs it. |
| done once | A one-off lookup by hand. Only its record is in the repository or the data dir. |
| in progress | Another package's uncommitted work, seen in the working tree. |
| planned | The PRD or PLAN.md names it, but no code sends it yet. |
| test-only | Only tests, examples or identifiers name the host. Nothing contacts it. |

## In short

- **Three flows are live, all from the two background loops.** They run from snapshot `aee6f92` (D-090): NPS live feeds and images, the NPS archive, and NOAA's HRRR bucket.
  - Each sends plain HTTPS requests with no cookie, no account, no key and no personal data.
  - The User-Agent is the default `SnowGlobe/0.1 (webcam research capture; fetch-on-change)`. It is set in [http.py](../../workers/common/src/snow_globe_common/http.py), and the live loops unset every override (D-090).
  - The far side learns this Mac's public IP address, the request times, and which public files were asked for.
- **Three evaluation fetches ran once each on Mon 23 Nov 2026,** and their code is committed: JPL Horizons' tables (38 responses) and JPL's DE421 kernel for the ephemeris validation (`f841cef`), and NOAA's archived HRRR for the hindcast (328 requests, `1d4921e`). Re-runs read their caches (rows 10–12).
  - The JPL records hold the generic User-Agent. The hindcast's records hold none; its client sends the same default unless an override is set.
- **Nothing is uploaded by the code today.** No S3, no API, no telemetry and no paid service. AWS is not applied, and no keys exist; spend so far is $0 (CLAUDE.md).
- **No audio leaves this machine today, and no code records any.** Under the PRD, audio could leave only through Deepgram, in two cases (see "Audio").
- **People and tools move data too:**
  - package installs and image pulls;
  - git pushes, only with the owner's word;
  - iCloud's sync of the repository folder;
  - the agents' own sessions.

## Audio

**Today none leaves, and no code path could send it:**
- **The speech library cannot.** The release archive of the Swift speech library (`app/speech-macos`) may not refer to any networking API: Foundation URL loading, CFNetwork, Network.framework, BSD sockets and DNS. Nor may it use SFSpeechRecognizer's server-capable tasks, AssetInventory's download calls, a recorder or a file writer.
  - The rules are in [forbidden-symbols.txt](../../app/speech-macos/scripts/forbidden-symbols.txt). `scripts/check-symbols.sh` checks the release archive against them, and `StaticChecksTests` the debug objects (D-055).
  - The library uses SpeechAnalyzer's `DictationTranscriber`, which runs on device (PRD §7.1, engine A1).
- **The transcribe tool reads files only.** `snow-globe-transcribe` reads a WAV file and never records.
- **The M1 spike opens the microphone only in its checks 5 and 6.** The owner runs those checks, and none has run yet (D-033).
  - Its samples go to the speech library in RAM and are never stored, logged or sent ([audio.rs](../../spikes/m1-platform/tauri-app/src/audio.rs)).
  - Its check 7 watches the network with `nettop` while the transcriber runs ([netwatch.rs](../../spikes/m1-platform/tauri-app/src/netwatch.rs)).
- **The M1 app shell is in progress** (`app/src-tauri/`, untracked; read at 21:46 UTC). Its `src/platform/audio.rs` opens the microphone on key-down and closes it on key-up, and pushes the samples to the speech library, which copies them into its own RAM ring. They are never stored, written or sent, and `tests/privacy_source.rs` checks that its audio and speech modules never use the file system or print. It has no network code (grep 4; row 13).
- **No Deepgram code exists, and there is no key.**

**Under the PRD, audio can leave this machine in exactly two cases, both to Deepgram (engine D)** (PRD §7.1, §15.1, §15.2; D-015). Both set `mip_opt_out=true`, in one tested function, and both use a short-lived token minted in parallel with opening the microphone. The app never holds a long-lived key; if Deepgram has no token API, there is no cloud path.
1. **The replay bake-off (M1).** The owner's own voice fixtures, kept under `$SNOW_GLOBE_DATA_DIR/fixtures/voice/`, are streamed to Deepgram. This waits for the owner's go-ahead for the vendor and a key (D-012 row 7, CLAUDE.md "Paid services"), and neither exists.
2. **In the product, only if the user turns cloud speech on.** It is off by default, behind a visible toggle with a persistent indicator.
   - Audio is captured only while the key is held, and held in RAM for that query.
   - The destination chip shows "→ Deepgram" for a full frame before the first byte leaves. The core sends nothing to a cloud destination until the overlay answers `destination_shown` for that key press ([protocol.ts](../../app/src/overlay/protocol.ts)).
   - The on-device-only setting disables every cloud path.

**No other planned path carries audio:**
- Parakeet (engine P) would run on device.
- Soniox is in reserve and would need its own approval.
- Apple's model download sends no audio.

**Speech-derived text:**
- The transcript stays in RAM and with Foundation Models, on device.
- Claude Haiku would receive text only if it is enabled, and only after its terms are recorded.
- Traces never hold transcript text: the trace format has no free-text field ([trace.rs](../../app/core/src/trace.rs)).
- The planned graded-ask log carries no text.

## Live today

The capture and forecast loops run code from snapshot `aee6f92`. Between that snapshot and HEAD (`9d372bd`), `workers/` and `viewpoints/` differ in:
- the Rainier manifest (`0292fe8`, `1d4bf19`) and the viewpoint schema (`1d4bf19`), with the common and forecast tests that follow them;
- `workers/capture/README.md` (`53d5d4f`);
- the forecast worker's hindcast mode (`1d4921e`, row 12): a new `hindcast/` package, its tests and fixtures; hooks in `bucket.py` and `extract.py` that leave the live worker's behaviour as it was; the `hindcast` subcommand's hook in `cli.py`; a "Hindcast mode" section in `workers/forecast/README.md`; and comment-only and description-only edits to `availability.py` and `schema/forecast_run.schema.json`.

The loops run none of these.

| # | Flow and code | Host | Sent | Received | When, how often | Limits that bind it | Status |
|---|---|---|---|---|---|---|---|
| 1 | **NPS live feeds and images.** [live.py](../../workers/capture/src/snow_globe_capture/live.py) and `fetch.py`, through `polite_client`; the loop is `snow-globe-capture loop --archive-queue`, all 8 cameras | `www.nps.gov` | • **ARD air cams:** a GET of the camera's JSON feed (`featurecontent/ard/webcams/json/<code>json.txt`) with `If-None-Match` or `If-Modified-Since`.<br>• **Rainier:** a HEAD of `webcams-mora/mountain.jpg`.<br>• **On change only:** a GET of the image, with `If-None-Match`.<br>• **Headers:** Host, the User-Agent, `Accept: */*`, `Accept-Encoding: gzip, deflate`, `Connection: keep-alive`. No cookie, no authorisation, no identifier. | • The feed's `imagedate`, and the validators.<br>• JPEG frames, stored at most 1920 px wide, plus a 640 px thumbnail (D-028). | • A pass every 300 s.<br>• Each camera is polled when its slot is due.<br>• Upper bounds from the rules: 288 polls and 96 image GETs per camera per day. | • **Polls:** at most one per camera every 300 s, from the last poll's answer to the next one's send (+2 ms).<br>• **Images:** fetched only on change; at most 4 GETs per camera in any rolling hour, counted on answer times and persisted; each paced at least 0.8 × 3600/4 s after the last.<br>• **Redirects:** never followed; a 3xx is logged and backed off.<br>• **Back-off:** per camera, 5 min doubling to 1 h.<br>• **HTTP 429 or 503:** holds every `www.nps.gov` request, live and archive, honouring `Retry-After` up to 6 h.<br>• **One NPS fetcher per machine:** a host-wide lock; a second fetcher exits with status 3.<br>• **Timeouts:** 10 s to connect, 30 s to read.<br>• Sources: PRD §15.3; CLAUDE.md "Polite collection". | live |
| 2 | **NPS archive backfill.** [archive.py](../../workers/capture/src/snow_globe_capture/archive.py) and [archive_queue.py](../../workers/capture/src/snow_globe_capture/archive_queue.py), only in the loop's idle time, from `state/archive/queue.json` | `www.nps.gov` (`/airwebcams/`) | • `GET api/GetAvailableDays/<code>?<YYYYMMDDHH00>`; the stamp is a cache-buster, not an identifier.<br>• `POST api/Search/Execute`: a JSON search for one location code and one local date, 300 per page, oldest first.<br>• `GET GetAsset/<AssetID>/proxy/hires` or `thumbmedium.jpg`.<br>• The same headers as row 1. | • Availability masks.<br>• Search pages: asset ids, local times, solar elevation.<br>• JPEG frames. | While the queue has work. | • **Spacing:** one request at a time, at least 5.05 s from the end of one to the start of the next, across every process (the `nps-archive` limiter, D-077).<br>• **Budgets:** at most 1,000 requests per UTC day (persisted); a 5 GB total cap.<br>• **Resumable:** nothing is downloaded twice.<br>• **Retries:** a failing asset is tried at most 3 times a run and 6 in all.<br>• **Back-off:** after every error, a 3xx, any 4xx and a bad body included (D-083).<br>• **Shared hold:** the `www.nps.gov` hold of row 1.<br>• **Held-out guard:** no asset dated 1 Jun 2026 or later is requested (D-019, D-070). | live |
| 3 | **NOAA HRRR.** [lambda_handler.py](../../workers/forecast/src/snow_globe_forecast/lambda_handler.py) through [bucket.py](../../workers/forecast/src/snow_globe_forecast/bucket.py); the local stand-in calls the Lambda handler every 5 minutes | `noaa-hrrr-bdp-pds.s3.amazonaws.com` (anonymous HTTPS) | • S3 ListObjectsV2 GETs (`?list-type=2&prefix=hrrr.<day>/conus/...`).<br>• `.idx` GETs.<br>• Byte-range GETs (`Range: bytes=a-b`) of the GRIB2 surface files.<br>• The same polite headers. No AWS credential and no signature. | • Listings, with `LastModified`.<br>• `.idx` inventories, about 10 KB each.<br>• GRIB2 ranges: 8 fields, f00–f02, at most 40 MB a run in the stand-in's profile. | • Every 5 minutes.<br>• A run's hours once they are on S3. | • **Requests:** anonymous byte ranges, subsets only; GRIB files are deleted after extraction (CLAUDE.md "NOAA").<br>• **Range check:** a range must come back as 206 with the exact `Content-Range`; a 200 is closed unread.<br>• **Retries:** at most 4 tries, back-off 1 s doubling to 30 s, on 429, 5xx and timeouts.<br>• **Size caps:** the stand-in 40 MB a run; the CLI 150 MB by default; backfill 1,000 MB.<br>• **Parallelism:** up to 8 worker threads (`SNOW_GLOBE_FORECAST_WORKERS`). | live |

## On demand, and done once

| # | Flow and code | Host | Sent | Received | When, how often | Limits that bind it | Status |
|---|---|---|---|---|---|---|---|
| 4 | **Capture's one-off commands.** `snow-globe-capture once`, `archive days`, `archive day`, `archive sample` and `archive-check`. The archive check made 217 requests on 19 Nov. `archive-check --rainier-page` makes one GET of `/mora/learn/photosmultimedia/webcams.htm`, through the archive client | `www.nps.gov` | As rows 1 and 2. | As rows 1 and 2. | Only the lead runs them. | • Rows 1 and 2's rules, under the same lock.<br>• Refused (status 3) while the loop runs.<br>• Agents never start them (CLAUDE.md). | on demand |
| 5 | **The forecast CLI.** `snow-globe-forecast latest`, `run`, `backfill`, `tick`, `status`, `plan`, `grid-check`, `datum-check`, `lag-survey` and `lag-check` | `noaa-hrrr-bdp-pds.s3.amazonaws.com` | As row 3. `lag-survey` and `lag-check` list only, with no GRIB. | As row 3. | On demand. | Row 3's rules. A run planned over 150 MB is refused without `--max-mb`, and `backfill` stops at 1,000 MB. | on demand |
| 6 | **The backtest pair.** [pair_forecast.py](../../eval/src/snow_globe_eval/pair_forecast.py), through the forecast worker's `HrrrBucket` | `noaa-hrrr-bdp-pds.s3.amazonaws.com` | • One ListObjectsV2 per candidate run.<br>• The two bracketing hours' `.idx` and byte ranges. | The same kinds of data, for two hours. | It ran for the M0.5 pair (Grand Canyon, 15 Jan 2024). | A 60 MB GRIB cap, checked against the `.idx` plan before any GRIB byte. | on demand |
| 7 | **The Lambda smoke test.** [smoke.sh](../../infra/lambda/smoke.sh) `IMAGE`, without `--offline`: the forecast handler under the Runtime Interface Emulator in Docker | • `noaa-hrrr-bdp-pds.s3.amazonaws.com`, from inside the container.<br>• `127.0.0.1:<port>`: the local emulator. | VIS:surface, f00, at most 10 MB a run, a 1 h look-back, invoked twice. | As row 3. | On demand, by the lead. | • Never invokes the capture handler.<br>• The offline probe runs with no network, a read-only root and a non-root user. | on demand |
| 8 | **The plate sources, fetched by hand** (not in code). `plates/` reads them offline. | • `elevation.nationalmap.gov`: the 3DEP `exportImage` DEM subsets.<br>• `carto.nationalmap.gov`: the geonames layer 5 queries. | One request each, with the User-Agent `SnowGlobe/0.1 (plate builder; public landmark lookup)`; no nps.gov request and no paid call. | Float32 GeoTIFFs and JSON feature sets. | • **Rainier:** 2 requests on Mon 23 Nov 2026, at 02:03:45 and 02:05:30 UTC.<br>• **The seven other cameras:** 16 requests the same day, from 12:30:20 to 13:18:58 UTC, for the plates in progress (their specs are untracked). One of Acadia's DEM exports answered HTTP 500 first. | • A budget of at most 3 requests per camera, recorded in five of the seven provenance files; each camera used 2 or 3.<br>• Each response is kept with its SHA-256 in `<data dir>/plates/<viewpoint_id>/sources/provenance.json`. | done once |
| 9 | **The manifests' fact lookups, by hand.** On 19 Nov, and on 23 Nov for Rainier's correction. | `epqs.nationalmap.gov`, `carto.nationalmap.gov`, `services1.arcgis.com`, `files.airnowtech.org`, `www.epa.gov`, `topoquest.com`, `en.wikipedia.org`, and `www.nps.gov` pages | Point queries and page loads. The User-Agent used is not recorded. | Coordinates and heights (facts). | Once. | Each manifest lists the URL and the access date. | done once |
| 10 | **The ephemeris validation, Horizons part.** `snow-globe-eval ephem-validate` ([horizons.py](../../eval/src/snow_globe_eval/ephem_validate/horizons.py), [plan.py](../../eval/src/snow_globe_eval/ephem_validate/plan.py)), committed in `f841cef` | `ssd.jpl.nasa.gov` (`/api/horizons.api`) | • Observer-table GETs: the body (10, the Sun, or 301, the Moon), a viewpoint's site (longitude, latitude, height), the start, stop and step, the quantities, airless or refracted.<br>• The polite client's headers, with the generic User-Agent. No cookie and no key. | • Text ephemerides inside JSON.<br>• 38 responses, all HTTP 200, cached as 27.5 MB of JSON in `<data dir>/cache/ephem/horizons/`. | • Once, on Mon 23 Nov 2026, from 10:11:26 to 10:15:37 UTC: 6 short Rainier tables of 21 Nov for the test fixtures (4 hourly, 2 at a 2 min step), then 32 year-long tables (8 sites, the Sun and the Moon, airless and refracted, hourly from 1 Jul 2026 to 30 Jun 2027).<br>• Re-runs read the cache. Only a new plan (a moved site) or an emptied cache fetches again, once. | • **Spacing:** one request at a time, at least 2 s from end to start, across processes (the `jpl-ssd` limiter).<br>• **Retries:** at most 4 tries; back-off 30 s doubling to 15 min, or a longer `Retry-After`, on 429, 5xx and transport errors. A rejected query is not retried.<br>• **Timeouts:** 15 s to connect, 240 s to read.<br>• **Cache:** every response is kept under the SHA-256 of its parameters and never fetched again; `--offline` refuses to fetch. | on demand (ran once). Six of its responses are eval's test fixtures (`eval/tests/fixtures/ephem/horizons/`), public on the next push. |
| 11 | **The ephemeris validation, Skyfield part.** [skyfield_run.py](../../eval/src/snow_globe_eval/ephem_validate/skyfield_run.py), committed in `f841cef` | • `ssd.jpl.nasa.gov`: `/ftp/eph/planets/bsp/de421.bsp`.<br>• PyPI, through uv (row 14), for the isolated environment. | • One GET of the DE421 kernel, with the polite client's headers.<br>• `uv run --no-project --python 3.13` with `skyfield==1.55`, `jplephem==2.24`, `sgp4==2.27`, `numpy==2.5.4` and `certifi==2026.10.23`: uv's own requests for any of those wheels its cache lacks. | • The kernel: 16,788,480 bytes, HTTP 200, checked (at least 10 MB, the `DAF/SPK` signature) and recorded with its SHA-256.<br>• The wheels, into uv's cache. | • The kernel once, at 10:22:06 UTC on Mon 23 Nov 2026 (`de421.bsp.json` in the data dir), and never again while it is cached.<br>• The wheels on the environment's first run; that time is not recorded.<br>• Skyfield itself downloads nothing: its output records the built-in timescale. | • **Kernel:** the `jpl-ssd` limiter (2 s); at most 3 tries, back-off 30 s doubling to 15 min; 15 s to connect, 300 s to read.<br>• **Wheels:** uv's defaults; none set here.<br>• `--offline` refuses to fetch or to build the environment. | on demand (ran once) |
| 12 | **The HRRR hindcast.** `snow-globe-forecast hindcast plan`, `fetch`, `compare`, `leads` and `status` ([hindcast/](../../workers/forecast/src/snow_globe_forecast/hindcast/)), committed in `1d4921e`; eval's `p1dev` reads its files offline | `noaa-hrrr-bdp-pds.s3.amazonaws.com` | As row 3, through a caching client. | • As row 3, for 8 runs (12Z and 18Z, 18–21 Nov) and 43 forecast hours.<br>• 444,085,987 bytes in all: 441,854,992 of GRIB ranges, the rest listings and inventories. | • Once so far, on Mon 23 Nov 2026: 328 requests (12 listings, 179 inventories, 137 GRIB ranges) and 0 retries. Its persisted ledger was last updated at 10:30:50 UTC (`<data dir>/forecast/hindcast/state/network-ledger.json`).<br>• A re-plan at 11:33:03 UTC made no request (its log).<br>• On demand; a run already written is never fetched again. | • **One request at a time** (`workers=1`).<br>• **Quiet window:** no request from :50 to :05 past each hour, retries included, so it never competes with the live stand-in.<br>• **Budget:** a persisted request ledger and a 2,000 MB cap on everything it downloads; a plan that needs more is refused before any GRIB byte.<br>• **Cache:** listings and `.idx` files are kept and never fetched twice; GRIB data is never kept (subsets are deleted after decoding).<br>• **Retries:** the bucket client's (row 3); a failing run is retried after 1, 2, 4, ... min, at most 15 min apart, and the fetch stops after its error limit. | on demand (ran once). Two of its `.idx` inventories and a listing are the forecast tests' fixtures (`workers/forecast/tests/fixtures/`). |

## In progress in other packages

This is another package's uncommitted work in the working tree, read at 21:46 UTC. It is still changing, and the lead reconciles it when it is committed.

| # | Flow and code | Host | Sent | Received | When, how often | Limits that bind it | Status |
|---|---|---|---|---|---|---|---|
| 13 | **The M1 app shell.** `app/src-tauri/` (the Tauri 2 process: hotkey, panel, microphone, speech bridge) and `app/src/shell/` (the bridge, the IPC and the Tier 1 loader in TypeScript), untracked | None: app-local only | • Nothing leaves the machine. The Tier 1 loader (`app/src/shell/tier1.ts`) fetches its job and the plate images from `snowglobe://localhost/...`, and `isFrameUrl` refuses any other URL.<br>• The shell tells online from offline by reading its interfaces' addresses (`getifaddrs`, in `src/platform/system.rs`), sending no traffic. | The core's job and the plate PNGs, served from memory, each checked against its SHA-256. | Each Tier 1 render, and each `network` message to the page, from M1. | • The webview's content security policy lets it connect only to itself, Tauri's IPC (`ipc:`, `http://ipc.localhost`) and `snowglobe:` (`tauri.conf.json`).<br>• No HTTP client or socket in its code (grep 4).<br>• Its lock file, like the spike's, holds reqwest 0.13.5, hyper and tokio, which tauri 2.12.7 pulls in; neither the shell's code nor the spike's names them. | in progress |

## Install, build and CI time

The tools below send their own default headers. This repository neither sets nor checks them, and none was looked at on the wire.

| # | Flow | Host | Sent | Received | When, how often | Limits that bind it | Status |
|---|---|---|---|---|---|---|---|
| 14 | `uv sync` and `uv run` in each Python project; `uv pip install` inside the Lambda build | • `pypi.org` (the simple index).<br>• `files.pythonhosted.org` (files).<br>• `github.com` (the oculus repository, for `oculus-core-py`, built from a pinned commit). | Package names and versions. | Wheels and source archives; the lock files pin each version and hash. | • When a lock file or a venv changes.<br>• The snapshot venvs build with `--offline` (D-090). | • CI runs `uv sync --locked`.<br>• No rate or size limit is set here: uv's defaults apply. | on demand |
| 15 | `cargo build` and `cargo test` (app, spike) | • `index.crates.io` and `static.crates.io` (cargo's sparse registry; the lock files name `crates.io-index`).<br>• `github.com` (the oculus repository, the `oculus-core` git dependency). | Crate names and versions. | Crate sources. | • When `Cargo.lock` changes.<br>• `--offline` works from the copies under `~/.cargo`. | None set here: cargo's defaults apply. | on demand |
| 16 | `pnpm install` (renderer, overlay), through corepack | `registry.npmjs.org`, pnpm's default registry. The lock records integrity hashes, not hosts. | Package names and versions. | • Tarballs.<br>• Corepack also downloads pnpm itself once per version. | On demand, and in CI. | • CI installs with `--frozen-lockfile`.<br>• esbuild's install script is not allowed to run (`allowBuilds: esbuild: false`). | on demand |
| 17 | `pnpm exec playwright-core install --with-deps --only-shell chromium` | • Playwright's download host: it is set inside playwright-core and not named in this repository.<br>• Ubuntu's package mirrors, for `--with-deps`. | The shell's name and revision; the system packages' names. | The Chromium headless shell and its system libraries. | CI only. This Mac's copy (revision 1254) is already installed. | In CI since `8ea7bee`: each attempt at most 300 s (killed 15 s later), at most 3 attempts 15 s apart, and the step at most 16 min. | on demand |
| 18 | `infra/lambda/build.sh` (`docker buildx build`) | • `public.ecr.aws`: the base image, pinned by digest.<br>• `ghcr.io`: `astral-sh/uv:0.11.6`, pinned by digest.<br>• PyPI, from inside the build.<br>• With `--push`, the owner's ECR repository (planned). | Image references and package names; with `--push`, the image. | Image layers and wheels. | On demand, by the lead. | • Both base images are pinned by digest.<br>• `--push` only by the owner; no AWS credentials exist on this machine. | on demand |
| 19 | `infra/test.sh` and [tf_providers.sh](../../infra/tf_providers.sh) | • `registry.terraform.io` and `releases.hashicorp.com`, only when the plugin cache does not match the lock file (`SNOW_GLOBE_TF_INIT=auto`).<br>• `checkpoint-api.hashicorp.com`: never (`CHECKPOINT_DISABLE=1`).<br>• `github.com`, for `tflint --init`, only when tflint is installed. It is not installed here. | The provider's name and version. | The AWS provider (about 160 MB). | • On demand.<br>• On 20 Nov all of `infra/test.sh` passed under a macOS sandbox that denied every outbound connection except loopback and Unix sockets (infra/README.md). | • The registry only on a cache miss; no checkpoint calls.<br>• tflint's download only when tflint is installed. | on demand |
| 20 | `snow-globe-transcribe install-assets --locale L --yes` | Apple's asset servers, through macOS's AssetInventory. The host is not in the code. | macOS's request for the locale's model; no audio. | The on-device dictation model. | Once per locale, by the owner (docs/m1/owner-runbook.md). | • Only the owner runs it, with `--yes`.<br>• The speech library itself cannot download (its symbol checks). | on demand |
| 21 | `git push` and `git fetch` | `github.com`, over HTTPS (`origin`) | • The committed tree and its history.<br>• The commit identity is a generic workstation name and a non-routable local address, not the owner's. | Refs and objects (`git fetch`). | Six pushes so far, by `git reflog`: three on 21 Nov (`23c0543` at 00:48:32, `8fa6b6a` at 06:53:57 and `aee6f92` at 11:24:01 UTC; D-080, D-081, D-090), and three on 23 Nov (`22930cc` at 11:13:14, `6bfd520` at 11:43:34 and `8ea7bee` at 12:20:06 UTC; D-092 records the first). | Only with the owner's word; agents never push (CLAUDE.md). | on demand |
| 22 | GitHub Actions CI ([ci.yml](../../.github/workflows/ci.yml)) | GitHub's `ubuntu-24.04` runners, not this Mac. They fetch the pinned actions, uv 0.11.6, Rust 1.98.1, Node 24, pnpm 12.15.0, Terraform 1.16.1, every locked package and the Chromium shell. | Nothing from this Mac beyond the push (row 21). | Test results, on GitHub. | On every push and pull request, and by hand (`workflow_dispatch`). | • Each job at most 10 to 30 min; a newer run cancels an older one on the same ref.<br>• A read-only token (`contents: read`), no secrets and no AWS credentials.<br>• Tests marked `network` are deselected, and pytest runs behind a dead proxy. | on demand (each push) |

## Outside the code

Data also leaves this machine in two ways the code does not control:

- **iCloud Drive.** The repository folder is under `~/Documents`, which iCloud syncs (CLAUDE.md).
  - So every file in it is copied to the owner's iCloud account at Apple: code, docs, lock files, the uv venvs, `node_modules` and test results.
  - The data dir and the Cargo target folder are outside iCloud (D-017, D-031). So frames, voice fixtures, logs, plates, renders, traces and GRIB files are not synced.
- **Agent sessions.** Claude Code sessions (the lead's and its agents') send what they read to Anthropic's API: code, docs, command output, data-dir metadata and logs.
  - They never open held-out frames, the sealed window or quarantine (CLAUDE.md).
  - Earlier sessions fetched web pages for the research notes in `docs/research/` (M0).
  - The scaffold build sent 9 requests to nps.gov on 19 Nov (D-045).
  - On 23 Nov sessions read JPL's Horizons manual and DE export page for the notices (`9d372bd`), Open-Meteo's docs page, and the web sources of the any-place research, whose memo and briefs are in `<data dir>/reports/anyplace/`. They also made the plate-source requests of row 8.

## Planned: the PRD and PLAN.md name these, and no code sends them yet

| Flow | Host | What would be sent | What would come back | When, how often | Limits planned | Gate |
|---|---|---|---|---|---|---|
| **Cloud speech (engine D)** | Deepgram | • Audio, only while the key is held, as "Audio" above says.<br>• Viewpoint aliases as keyterms.<br>• `mip_opt_out=true`, and a short-lived token. | Partial and final transcripts, after an explicit finalize on key-up (PRD §6.3). | • The bake-off: the owner's replay fixtures, in M1.<br>• In the product: each key hold, only with cloud speech on. | • The "→ Deepgram" chip first; the on-device-only setting disables it.<br>• A short-lived token, never a long-lived key.<br>• The bake-off's hard cap is $3 (PRD §17). | The owner's go-ahead and a key (PRD §1 row 7). |
| **Tier 2 refinement (M3)** | • fal, called with `X-Fal-Store-IO: 0`.<br>• Runware as the alternate host. | • The Tier 1 frame, 1024 px on the long edge.<br>• A prompt built from the parameters only (time of day, sky state, fog state). The user's words never reach the model.<br>• It goes through the server's refine proxy, which holds the key. | The refined frame, or "keep Tier 1" at the daily cap (`POST /v1/refine`, PRD §11.1). | • The kill test: 20 frames, offline, for at most $1; the FLUX.2 klein illustration, about 20 calls on fal.<br>• Then, only if it passes, each query's Tier 2. | • At most one request in flight, and a cancelled one still counts.<br>• No speculative or paid warm-up; only a TLS pre-connect on key-down.<br>• A spend guard and a server-side daily cap (PRD §11.3). | The owner's approval of each vendor, and a key (PRD §9.6). |
| **The LLM parser fallback** | Anthropic (Claude Haiku) | The transcript text that the grammar rejects: the only planned path for text. | A parse of it. | Only when the grammar rejects an utterance and Foundation Models fails. | Not set yet. | Only if Foundation Models fails, after its terms are recorded (PRD §15.2). |
| **Forecasts beyond 48 h (M2)** | Open-Meteo | Coordinates and the pinned model (GFS). | Forecasts from 48 h to 7 days. Its responses carry no run time, so they are bracketed with `meta.json` (PRD §8.1). | Model-dependent (PRD §8.1). | Not set yet. | Open-Meteo's free API is for non-commercial use; see [licences.md](licences.md), R9. The lead proposed reading NOAA's GFS from NODD instead (ruling j, D-115), which waits for the owner. |
| **An independent cloud and fog vote** | `noaa-goes18` on S3, anonymous | Byte ranges, as row 3. | GOES-18's clear-sky mask and reflectance at the viewpoint pixels. | Not set yet; GOES-18 itself updates every 5 min (PRD §8.1). | CLAUDE.md's NOAA rule: anonymous byte ranges, subsets only. | PRD §8.1, §13.3. |
| **On-site NPS met data** | NPS, where a park has it | Station queries. | Met observations at the camera. | Not set yet. | PRD §15.3's rules, where the host is nps.gov. | PRD §13.3. |
| **AWS, once the owner applies the Terraform** (D-014, D-034) | AWS, `us-east-1` | • **Capture Lambda:** polls `www.nps.gov` exactly as row 1 and writes to the private S3 data bucket.<br>• **Forecast Lambda:** reads NOAA's bucket as row 3.<br>• **Logs:** CloudWatch Logs.<br>• **Alarms:** by SNS e-mail to `TF_VAR_alarm_email`, an address the owner gives to AWS at deploy.<br>• **Deploy:** `deploy.sh` calls STS, Lambda, S3, ECR and Terraform with the owner's credentials. | • The services' answers to the deploy (identity, Lambda and S3 results, Terraform state).<br>• Alarm e-mails, to the owner. | • Capture every 5 min; the forecast worker every 5 min from 50 min past each run's start, until the run is on S3 (PRD §11.2).<br>• The deploy, by the owner. | • Capture's reserved concurrency is 1, and it must never run beside the local loop.<br>• 30-day log retention (`log_retention_days = 30`).<br>• Budgets with a hard-stop action. | The owner (`infra/deploy.sh`). No AWS credentials exist on this machine. |
| **The app's backend** (PRD §11.1; not written) | CloudFront and an API on Lambda | • `GET /v1/viewpoints` and each viewpoint's manifest.<br>• `GET /v1/live/{viewpoint}`: the app never polls NPS itself.<br>• `POST /v1/refine`.<br>• `POST /v1/predictions`, a graded ask: `install_id` (pseudonymous), viewpoint, valid time and timings, with no text. | Verified viewpoints; manifests with signed URLs for plates, forecast files and Tier 0 thumbnails; the latest live frame and its age; the refined frame; a prediction's id. | At launch and per query: the first partial that names a place loads its assets from the cache, fetching anything missing (PRD §6.3). | • One polite fetcher for every install: the server's capture.<br>• Graded asks are disclosed on first run and off in on-device-only mode.<br>• Our code stores no IP address. | M1 onwards. |
| **The Tier 2 proxy's host** | Lambda or a Cloudflare Worker | Tier 2's requests, above. | Tier 2's answers. | As Tier 2. | It must add at most 100 ms p95 over a direct call when idle; both hosts are measured (PRD §11.3). | PRD §11.3. |
| **Credit links** | The camera operator's or NOAA's site, in the user's browser | The browser's own request for the page. | The page, in the browser. | Only when the user opens a credit. | The overlay's `open_url` asks the shell to open an `https://` credit link; the panel never navigates. | M1. |
| **The takedown channel** | GitHub (an issue template) and a project contact address | Nothing from the app. | Requests from rights holders. | When one comes; answered within 7 days (PRD §15.5). | None set here. | PRD §15.5. |
| **The M2 depth and embedding models; the M4 ungrounded baseline** | Not named yet | Not set yet. | Not set yet. | Not set yet. | Not set yet. | Each needs its own approval. |

## Hosts that the code names but never contacts

| Host or URL | Where | What it is |
|---|---|---|
| `127.0.0.1:9` | the Makefile's `DEAD_PROXY`; `infra/cost/*.py`; `infra/tests/test_tf_providers.sh` | A dead proxy, so tests and simulations cannot reach a real host. |
| `127.0.0.1`, `127.0.0.1:$port` | `infra/lambda/smoke.sh` | The local Lambda emulator (row 7). |
| `localhost`, `snowglobe://localhost`, `asset://localhost`, `http://ipc.localhost`, `::1` | `app/src/overlay/`; `app/src/shell/` and `app/src-tauri/` (in progress); the Makefile's `NO_PROXY`; the spike's `netwatch.rs` | App-local URLs: the overlay and the shell's Tier 1 loader refuse any other URL, so the webview never fetches a remote camera, and `http://ipc.localhost` is Tauri's IPC origin in the shell's content security policy. `::1`, the IPv6 loopback, is in the Makefile's `NO_PROXY`, and the spike's `netwatch.rs` and the shell's `system.rs` leave it out of the addresses that reach beyond the machine. |
| `snow-globe.invalid` (http and https) | [headless.ts](../../renderer/src/node/headless.ts); `app/src/test/browser.ts`; schema `$id`s | The renderer's and the overlay tests' page origin. Every request to it is answered in process, and every other request is aborted. Playwright's default Chromium switches include `--disable-background-networking`. |
| `json-schema.org` | schema files | `$schema` identifiers. |
| `s3.amazonaws.com/doc/2006-03-01/` | `bucket.py`, forecast test fixtures | The S3 XML namespace. |
| `www.apple.com/DTDs/...` | `Info.plist` files | A plist DOCTYPE. |
| `budgets.amazonaws.com`, `cloudwatch.amazonaws.com`, `lambda.amazonaws.com`, `scheduler.amazonaws.com` | `infra/terraform/` | IAM service principals, not endpoints. |
| `123456789012.dkr.ecr.us-east-1.amazonaws.com` | infra tests | A placeholder account. |
| `aws.amazon.com`, `docs.aws.amazon.com` | `infra/README.md` | Pricing and documentation links. |
| `example.com`, `example.org`, `example.invalid`, `x.example`, `other.example`, `http://x`, `https://x`, `http://localhost`, `snowglobe://evil.example`; and, in the shell's route tests (in progress), `snowglobe://example.com`, `snowglobe://LOCALHOST`, `snowglobe://user@localhost` and `localhost:8080` | tests | Test inputs, mostly for redirects and refused URLs. |
| `file://` | `app/src/scripts/*.ts`, tests | Local file URLs. |
| `^https://noaa-hrrr-bdp-pds\.s3\.amazonaws\.com/...` | `forecast_run.schema.json` | A URL pattern for row 3's host. |
| `open-meteo.com` | overlay tests | Credit text for a planned source. |
| `www.arb.ca.gov` | `workers/capture/tests/fixtures/porejson.txt` | A link inside a recorded NPS feed. |
| `ssd-api.jpl.nasa.gov` | `eval/tests/fixtures/ephem/horizons/*.json` (committed in `f841cef`) | Text inside recorded Horizons responses (row 10). |
| `crates.io`, `golang.org`, `github.com` (bare) | comments in CI, the Makefile, the spike's README and `infra/tf_providers.sh`; `.tflint.hcl` | Mentions (rows 15 and 19). |
| `s3://$data_bucket`, `s3://$...` | `infra/deploy.sh`, `infra/README.md` | The owner's private data bucket (planned). |
| `tauri://`, `tauri://localhost` | the spike's `Cargo.toml` comment; the shell's `src/scheme.rs` and its tests (in progress) | The Tauri webview's local scheme. `tauri://localhost` is the shell page's origin on macOS, the only origin that the shell's `snowglobe:` handler lets read its answers. |

**What grep 2 matched that is not a host.** These are code fields, not hosts: `data.aws` (Terraform `data.aws_*`), `self.app`, `s.app`, `l.app`, `self.net`, `t.net`, `x.dev`, `y.dev`, `a.dev`, `b.dev`, `c.dev`, `opened.dev`, `checked.dev`, `self.invalid`, `log.invalid`, `laglog.invalid`. `dev.snowglobe.app` is the app shell's bundle identifier. `nexample.com` is `\nexample.com` inside a test string. Grep 3 also matched version numbers such as `2.50.0.34`, `2.5.0.34` and `6.2.3.3`.

## How the hosts were found

These ran from the repository root at 21:48 UTC on Mon 23 Nov 2026, with identical output in bash and zsh. They read the files git sees, tracked and untracked but not ignored, in `workers/ eval/ renderer/ app/ plates/ bench/ infra/ spikes/ viewpoints/ .github/` and the Makefile. That includes the lock files and the uncommitted files of other packages, so the counts move as those packages work.

```sh
# The paths are written out in each command, so they split the same way in bash and zsh.
# 1. every scheme URL, counted by scheme and host (46 distinct)
git ls-files -co --exclude-standard -z -- workers eval renderer app plates bench infra spikes viewpoints .github Makefile \
  | xargs -0 grep -I -h -o -E "[A-Za-z][A-Za-z0-9+.-]*://[^][ \"'<>(){}\`,;|\\]+" \
  | sed -E 's#^([A-Za-z][A-Za-z0-9+.-]*://)([^/:?#]*).*#\1\2#' | sort | uniq -c | sort -rn
# 2. bare host names without a scheme (40 distinct, false positives included)
git ls-files -co --exclude-standard -z -- workers eval renderer app plates bench infra spikes viewpoints .github Makefile \
  | xargs -0 grep -I -h -o -E "(^|[^A-Za-z0-9@./_-])([A-Za-z0-9-]+\.)+(com|gov|org|net|io|dev|ai|app|edu|aws|invalid|example)([^A-Za-z0-9-]|$)" \
  | sed -E 's/^[^A-Za-z0-9]//; s/[^A-Za-z0-9]$//' | tr 'A-Z' 'a-z' | sort | uniq -c | sort -rn
# 3. IP addresses, localhost and the IPv6 loopback (81 matches, 8 distinct strings)
git ls-files -co --exclude-standard -z -- workers eval renderer app plates bench infra spikes viewpoints .github Makefile \
  | xargs -0 grep -I -n -o -E "(localhost|::1\b|\b([0-9]{1,3}\.){3}[0-9]{1,3})(:[0-9a-z\$]+)?"
# 4. HTTP clients, sockets and network tools (89 files: 9 lock files and 80 others)
git ls-files -co --exclude-standard -z -- workers eval renderer app plates bench infra spikes viewpoints .github Makefile \
  | xargs -0 grep -I -l -E "\b(httpx|requests\.(get|post|Session)|urllib\.request|http\.client|import socket|socket\.|boto3|botocore|aiohttp|websocket|WebSocket|XMLHttpRequest|fetch\(|reqwest|ureq|hyper::|TcpStream|TcpListener|UdpSocket|std::net|tokio::net|URLSession|NWConnection|URLRequest|AssetInventory|curl |wget |docker (pull|push|build|buildx|run)|aws [a-z0-9]+ |terraform (init|apply|plan)|tflint --init|playwright-core install|page\.goto|\.route\(|nettop)"
```

**What the greps found:**
- **Grep 1:** each host it found is in a table above, or in "Hosts that the code names but never contacts". The lock files' `files.pythonhosted.org`, `pypi.org` and `github.com` are covered by rows 14 and 15.
- **Grep 3** now matches `::1` too (`::1\b`, so the placeholder account in IAM ARNs, `iam::123456789012`, is not taken for it). It is in the loopback row above.
- **Grep 4:** every file it found falls into one of four groups:
  - the code of a flow above, the app shell's Tier 1 loader (row 13) included;
  - a test of one, whose client is `httpx.MockTransport`, moto or a fake bucket;
  - a check that keeps one away: the speech library's symbol lists and scripts, the spike's `nettop` check, the Makefile's dead proxy;
  - a note about one: READMEs, comments, `pyproject.toml` dependency lines, the cost tool's measured request times, and the lock files.
- **The counts, corrected.** This file first said that grep 4 finds "79 files outside the lock files". The verifier's re-run at 10:51:59 UTC found 85 files, 77 of them outside the lock files: the 79 had counted the two `.terraform.lock.hcl` files as other files. Since then 4 more files match: the app shell's lock file and Tier 1 loader, and a Skyfield and a hindcast test.
- **A re-run at 22:29 UTC,** in `/bin/bash` with the system grep, while the app shell and the renderer's in-cloud work were still uncommitted:
  - greps 2 and 4 gave the same output as at 21:48;
  - grep 1 found 50 distinct strings, 4 more, all in the shell: its page's origin `tauri://localhost` and three refused test URLs (the `tauri://` and test-input rows above);
  - grep 3 found 137 matches of 9 strings: the 56 new matches, and the one new string `localhost:8080` (a refused test URL), are all in `app/src-tauri/`;
  - nothing new is contacted. The counts in the commands above are refreshed when those packages are committed.
- **Other networking:**
  - No Python, Rust, TypeScript or Swift code outside these files opens a socket or an HTTP client.
  - `app/core` has no network code.
  - The overlay and the shell's Tier 1 loader fetch only app-local URLs.
  - `bench/` and `plates/` make no request.
  - The backfill planner disables sockets before it runs (`eval/tools/backfill_plan/plan.py`).

**Hosts in the documents only.** The research notes, the PRD and the decisions cite 70 more hosts that no code contacts, from the same grep 1 over `docs/` and the root documents, at 21:48 UTC (`docs/ledger/` left out, since this file lists them):

account.windy.com, aistudio.google.com, alertcalifornia.org, api.open-meteo.com, api.windy.com, artificialanalysis.ai, arxiv.org, assemblyai.com, aviationweather.gov, bugs.webkit.org, cameras.alertcalifornia.org, cloud.google.com, commons.wikimedia.org, community.openai.com, computingforgeeks.com, console.groq.com, creativecommons.org, cwwp2.dot.ca.gov, deepgram.com, dev.to, developer.apple.com, developers.deepgram.com, developers.openai.com, docs.fluidinference.com, docs.github.com, docs.rs, docs.soniox.com, docs.speechmatics.com, dot.ca.gov, elevenlabs.io, events.ecmwf.int, fogcam.org, form.jotform.com, forum.babylonjs.com, hpwren.ucsd.edu, huggingface.co, lawrencehallofscience.org, machinelearning.apple.com, mthamilton.ucolick.org, platform.claude.com, registry.opendata.aws, soniox.com, support.apple.com, toolradar.com, v2.tauri.app, vlab.noaa.gov, weathercams.faa.gov, webkit.org, wes.copernicus.org, wiz.io, www.assemblyai.com, www.cantoni.org, www.earthcam.com, www.exploratorium.edu, www.faa.gov, www.flickr.com, www.gladia.io, www.law.cornell.edu, www.mapillary.com, www.mediawiki.org, www.ndbc.noaa.gov, www.parksconservancy.org, www.pipecat.ai, www.skylinewebcams.com, www.speechmatics.com, www.ucolick.org, www.usgs.gov, www.weather.gov, plus `noaa-goes18` (an S3 bucket name) and `...` (a placeholder in the PRD).

## Local only: data that never leaves this machine

- **The data dir.** It sits outside iCloud and git (D-017). It holds:
  - `frames/`, `thumbs/` and `archive/`;
  - `capture_log/` and `forecast_log/`;
  - `state/`, including the limiters' `state/ratelimit/`;
  - plates, renders and reports;
  - the evaluation caches: `cache/ephem/` (the Horizons tables, the DE421 kernel and the Skyfield output) and `forecast/hindcast/`.
- **Traces.** On local disk, with ids, lengths and parse fields only (PRD §15.2). The 30-day rolling retention of PRD §15.2 is not in `app/core/src/trace.rs` (checked again at `9d372bd`). The shell that writes traces arrives in M1.
- **The spike's screen capture and event tap.** In RAM only, and timestamps only (PRD §15.2).
- **Replay fixtures** (the owner's voice). Kept locally, outside iCloud and git, until M5 + 12 months.

## Disagreements for the lead

1. **The User-Agent's repo URL: resolved by D-113.**
   - PRD §15.3 said: "the User-Agent names the project and its repo URL."
   - The code's default names the project only ([http.py](../../workers/common/src/snow_globe_common/http.py)), and CLAUDE.md forbids the owner's GitHub handle in any request. The repository's URL contains the handle.
   - The lead dropped the URL on 23 Nov, to follow the owner's privacy rule (ruling h); a project URL without the handle may be added later. PRD §15.3 now says that the User-Agent carries no repo URL (corrected 23 Nov, D-113).
2. **PRD §15.2's table lacks flows that exist.** Its rows cover the product's flows, and none of these:
   - the NPS archive backfill, with its own limits;
   - the HRRR fetches as a flow (only "Forecast files: S3; app" is there), and the hindcast (`1d4921e`);
   - the ephemeris validation's JPL Horizons and DE421 fetches (`f841cef`);
   - install- and build-time registries;
   - git and CI on GitHub;
   - iCloud's sync of the repository;
   - the plate builder's one-off USGS fetches, now for all 8 cameras;
   - Apple's model download;
   - the alarm e-mail that the AWS deploy gives to SNS.

   Suggestion: one line in §15.2 that points to this file.
3. **The 30-day trace retention (PRD §15.2) is not implemented anywhere yet.** The trace writer that M1's shell brings needs it. The shell in progress writes no trace file yet: its diagnostics are JSON lines on stderr, with no free text (`app/src-tauri/src/diag.rs`).
4. **For the record, not a breach:**
   - The forecast worker sends up to 8 requests at once to NOAA's bucket. CLAUDE.md's NOAA rule asks for anonymous byte ranges and small local downloads; it sets no concurrency limit. "One request at a time" (PRD §15.3) is the NPS archive's rule. The hindcast sends one at a time.
   - The live loop's polls are requests too: up to 12 per camera per hour, against the "at most 4 per hour" of PRD §15.1. That limit binds image downloads, which the code enforces; the polls are bound by CLAUDE.md's "at most every 5 minutes".
