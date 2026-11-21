# Snow Globe

**Updated:** 2026-11-21 (UTC). A skeleton (PLAN.md M1 1d): the blanks are filled only from the
evaluation dashboard's exports (PRD §21), and a blank stays blank until its number is measured.

> *On a pre-registered, sealed ___-day period (base rate: summit visible ___ % of daytime frames;
> climatology PSS ___), Snow Globe's 24-hour visual forecast of Mount Rainier's summit scored a
> Peirce skill score of ___ vs ___ for persistence (Δ ___, 95 % CI ___ to ___). The skill is
> NOAA HRRR's. Rendering it lost none (non-inferiority margin 0.05: ___), and sampling the
> forecast along each view ray beat the camera's own grid cell at predicting cloud height (___).*
>
> *Latency after 30 min idle: first forecast-driven frame in ___ ms p95 from key release,
> measured at the composited pixel.*

Snow Globe is a push-to-talk visual forecast for macOS, in development. It was called Lookout
until 21 Nov 2026 (D-080). The aim:
1. You hold a hotkey and ask, for example, "is Rainier out tomorrow at 4?".
2. You let go.
3. An overlay shows that real view at that hour, built from NOAA's forecast and the true
   positions of the Sun and Moon.

It is meant as a visual forecast, not an image generator. Cloud, fog and light come from data
for that place and hour, and every frame says whether it is real, live or simulated. The
specification is [PRD.md](PRD.md).

**None of it exists yet as an app.** There is no overlay and no speech path. The renderer has
made one crude headless frame, labelled v−1, for the M0.5 backtest pair. See [Status](#status).

## Contents

- [Headline numbers](#headline-numbers)
- [Status](#status)
- [How it is meant to work](#how-it-is-meant-to-work)
- [Repository layout](#repository-layout)
- [Running the tests](#running-the-tests)
- [Data](#data)
- [Credits and licences](#credits-and-licences)
- [Privacy](#privacy)
- [Documents](#documents)

## Headline numbers

The targets are PRD §14's and don't move after measuring. A missed target will be reported with
its measured value and the bottleneck. Every measured cell will name the file and command it
came from.

| Metric | Target (PRD §14) | Measured | Measured by |
|---|---|---|---|
| Tier 0 (closest real photo) paint, p95, after 30 min idle | ≤ 300 ms | ___ ms | replay + paint capture (M1) |
| Tier 1 (relit frame) paint, p95, after 30 min idle | ≤ 1 s; stretch ≤ 400 ms | ___ ms | replay + paint capture (M2) |
| Tier 2 (photographic frame) delivered within 3 s | ≥ 80 % of queries | ___ % | replay + paint capture (M3) |
| Exact place and time, replay test set | ≥ 95 % | ___ % | bench (M1) |
| P1: Rainier summit visible, 24 h lead, ΔPSS vs persistence | claimed if the 95 % CI excludes 0 | ___ (CI ___ to ___) | sealed scoring (M4) |
| R1: non-inferiority vs the forecast-scalar baselines | margin 0.05 | ___ | sealed scoring (M4) |
| Stale paints | 0 in ≥ 10,000 cycles | ___ | stress test (M1) |

## Status

Day by day in [PLAN.md](PLAN.md); every measured number so far, with how it was measured, is in
[PRD_PROGRESS.md](PRD_PROGRESS.md).

- **M0, the scout, is done.** The owner accepted every PRD §1 default on 19 Nov 2026 (D-012).
- **M0.5, "start the clock", is in progress** (19 → 23 Nov):
  - the capture of all 8 NPS cameras;
  - a local stand-in for the forecast job, reading NOAA HRRR;
  - the archive check and one end-to-end backtest pair, labelled v−1.

  Both jobs run on the development laptop until the AWS deployment, which waits for the owner
  (D-014, D-030).
- **The 72 h log clock is still running.** It started at 12:24:23 UTC on 19 Nov for all eight
  cameras, and outages extend it. With 10.80 h lost to sleep and restarts by 06:06 UTC on 21 Nov,
  it closes at 23:12:07 UTC on Sun 22 Nov at the earliest (the data report; every new outage moves
  it later). The jobs keep their old Lookout names until the lead's switch after that
  (`infra/ops/migrate-to-snow-globe.sh`, D-080).
- **M1, voice to first pixel**, runs from Mon 23 Nov to Mon 7 Dec 2026:
  - a platform spike, built but not yet run;
  - on-device speech, the parser, the overlay with Tier 0, and the replay harness;
  - this README's first numbers.

  The owner's steps are in [docs/m1/owner-runbook.md](docs/m1/owner-runbook.md).
- **First pushed on 21 Nov 2026** (`23c0543`, with the owner's word; D-080). CI runs on every
  push; the first run's result is not recorded yet.

## How it is meant to work

Three tiers paint the same view, each more detailed than the last (PRD §9):

| Tier | What | Target |
|---|---|---|
| 0 | The closest real frame from the camera's own library, matched on the Sun's position and the forecast class | ≤ 300 ms |
| 1 | That viewpoint's plate, relit by a WebGL2 renderer from the forecast, the Sun and the Moon | ≤ 1 s |
| 2 | Photographic texture from an image model; it cannot change the sky or anything in haze, and a check rejects it if it tries | ≤ 3 s, or not shown |

Speech is to be transcribed on the Mac by default (PRD §7.1). The app and the evaluation are to
share one renderer and one forecast file, so that a scored prediction is the one the app showed
(PRD §6).

## Repository layout

| Path | What | State |
|---|---|---|
| `workers/common/` | Shared Python: data paths, manifests, storage, polite HTTP, logs | built, tested |
| `workers/capture/` | NPS live capture and archive backfill | built; running from a snapshot |
| `workers/forecast/` | HRRR extraction and the availability-lag log | built; running from a snapshot |
| `eval/` | Extractor, scores, baselines, leakage tests, the backtest pair | built, tested (v−1) |
| `renderer/` | The WebGL2 Tier 1 renderer and its headless runner | built, tested (v−1) |
| `app/core/` | Rust core: parser, time rules, ephemeris, solar events, trace, lifecycle | built, tested; no app around it yet |
| `spikes/m1-platform/` | The M1 platform spike (throwaway) | built, not run |
| `bench/` | The scripted utterances and recording instructions; no audio | written; nothing recorded |
| `viewpoints/` | The schema and the 8 viewpoint manifests | built |
| `infra/` | AWS serverless Terraform and the Lambda image | code only; never applied |
| `docs/` | Research, council, reviews, evidence, outreach drafts, M1 runbook | — |

## Running the tests

On macOS (Apple silicon), with uv and Python 3.13, Node 24 with pnpm, Rust 1.98 and Terraform
1.16:

```sh
make help                                          # every target, one line each
make test                                          # every suite, then a summary
make test TEST_SUITES='common capture forecast eval renderer rust docs'   # offline: all but infra
make test-rust                                     # one suite (test-common, test-eval, ...)
make docs-check                                    # invariants of the planning docs
python3 -I -B -m unittest discover -s bench/utterances -t bench/utterances -p 'test_*.py'
```

- **`make test`** needs no microphone, credentials or paid service.
  - It never contacts NPS or AWS.
  - The first run downloads dependencies; after that every suite runs offline except
    `test-infra`, which asks registry.terraform.io for provider versions.
  - The Makefile header has the details.
- **If the repository sits in an iCloud-synced folder,** iCloud hides the Python venvs' `.pth`
  files and fills the Rust build folder. The Makefile works around both: `PYTHONPATH` per
  package, and `CARGO_TARGET_DIR` outside the repo (D-031). Outside the Makefile, see CLAUDE.md
  "Toolchain".
- **Targets marked NETWORK** in `make help` contact NPS or NOAA. They are never part of
  `make test`, and the NPS ones refuse to run while another NPS fetcher is active.

## Data

- **Nothing captured lives in this repository.** Frames, forecasts, logs and voice fixtures go
  under `$SNOW_GLOBE_DATA_DIR` (default `~/snow-globe-data`), outside git and outside iCloud
  (D-017). The older `$LOOKOUT_DATA_DIR` and `~/lookout-data` are still found (D-080).
- **Camera frames stay private** until the National Park Service confirms in writing (PRD
  §15.1). Until then, the repository, README and demo carry code, numbers and charts only.
- **The repository holds no camera pixels.** `renderer/goldens/` holds synthetic test images
  only, and keeping them in git waits for the owner's sign-off (D-041).
- **Collection is polite** (PRD §15.3): one fetcher, change-only downloads, at most four frames
  per camera per hour, and an archive backfill one request at a time, ≥ 5 s apart.
- **Held-out periods stay out of development.** Summer 2026 is held out for design H, and
  15 Dec 2026 – 14 Jan 2027 is the sealed window for design P (D-019, D-022). The evaluation code
  carries a held-out guard (D-038).

## Credits and licences

- **Forecast data: NOAA HRRR via NOAA Open Data Dissemination** (public domain). The forecast
  skill in any result here is NOAA's.
- **Webcam imagery courtesy National Park Service.** Derived images are not NPS products, and no
  endorsement is implied.
- **Open-Meteo** (CC BY 4.0) will be credited, with a link, wherever its data is shown.
- **oculus-core** (MIT) computes the Sun and the Moon.
- Every third-party package and data source, with its licence, is in
  [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES). The ledger behind it is PRD §15.4.
- **Snow Globe's own licence is not chosen yet,** so none is granted. The owner picks one; the
  repository was first pushed on 21 Nov 2026 without it (D-080).
- **Takedown:** a project-specific contact address is still to be set (`TODO(M5)`, PRD §15.5).

## Privacy

The rules the app is being built to (PRD §15.1, §15.2):
- **Audio stays on the Mac by default.** It is captured only while the hotkey is held, kept in
  RAM for that query, and transcribed on device.
- **Cloud speech** is a measured comparison and an opt-in switch, never the default. The chip on
  screen changes before any byte leaves the machine.
- **Traces** keep timings and IDs, never what was said (PRD §10.5).
- **Replay recordings** are the owner's own voice, kept outside git and iCloud
  ([bench/README.md](bench/README.md)). The scripted set is opted in as a set, once per version
  of the script; real-use recordings are opted in one at a time.
- **Paid-service keys** live in a server's environment, never in the app.

## Documents

| Document | What |
|---|---|
| [PRD.md](PRD.md) | The specification, v0.4.2 |
| [PLAN.md](PLAN.md) | Milestones, dates, the cut order and gate evidence |
| [DECISIONS.md](DECISIONS.md) | Every decision with its evidence, and the spend ledger |
| [PRD_PROGRESS.md](PRD_PROGRESS.md) | Tickets, owner actions and every measured number |
| [docs/m1/owner-runbook.md](docs/m1/owner-runbook.md) | What the owner does in M1, in order |
| [bench/README.md](bench/README.md) | The scripted utterances and how they are recorded |
| [spikes/m1-platform/README.md](spikes/m1-platform/README.md) | The M1 platform checks |
