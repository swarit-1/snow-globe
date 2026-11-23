# Snow Globe

**Updated:** 2026-11-23 (UTC).

[![ci](https://github.com/swarit-1/snow-globe/actions/workflows/ci.yml/badge.svg)](https://github.com/swarit-1/snow-globe/actions/workflows/ci.yml)

**A push-to-talk visual forecast for macOS, in development.** As designed, you hold a hotkey, ask what a place will look like at a time ("is Rainier out tomorrow at 4?"), and let go. An overlay then shows that real camera view at that hour, built from NOAA's forecast and the true positions of the Sun and Moon, and every frame says whether it is a real photo, a live frame or a simulation. The parts are built and tested; the app that joins them is not. The overlay's protocol already refuses a frame whose label does not match its tier (D-093).

The project was called Lookout until 21 Nov 2026 (D-080).

**Reading this page.** `D-080` is an entry in [DECISIONS.md](DECISIONS.md), the append-only log of decisions and their evidence. `PRD §21` is a section of the specification, [PRD.md](PRD.md). `M1-09` is a ticket in [PRD_PROGRESS.md](PRD_PROGRESS.md), and M1 is a milestone of [PLAN.md](PLAN.md). `TODO(M2)` marks a stub in the code that milestone M2 is to replace. "The owner" is the person whose project this is, who makes the calls that PRD §1 lists. Every measured number names its source.

The headline below was written into the PRD before any of its numbers was measured ([PRD §21](PRD.md#21-résumé-lines-and-headline)), and every blank is unfilled. The blanks are filled only from the evaluation dashboard's exports, and a blank stays blank until its number is measured:

> *On a pre-registered, sealed `___`-day period (base rate: summit visible `___` % of daytime frames; climatology PSS `___`), Snow Globe's 24-hour visual forecast of Mount Rainier's summit scored a Peirce skill score of `___` vs `___` for persistence (Δ `___`, 95 % CI `___` to `___`). The skill is NOAA HRRR's. Rendering it lost none (non-inferiority margin 0.05: `___`), and sampling the forecast along each view ray beat the camera's own grid cell at predicting cloud height (`___`).*
>
> *Latency after 30 min idle: first forecast-driven frame in `___` ms p95 from key release, measured at the composited pixel.*

No row of PRD §14 is measured yet. The targets are in [Headline numbers](#headline-numbers), beside the partial measurements made so far.

## Snow Globe in brief

1. It is designed to turn a spoken question about a place and a time into that camera's view at that hour. It answers in three tiers: the closest real photo; the camera's calibrated base image, relit from NOAA's forecast and the true Sun and Moon; and, only if it passes its checks, photographic texture that cannot change the sky.
2. Its primary claim, P1, is written in PRD §13.4 and is to be frozen with its hash by Mon 14 Dec 2026. P1 tests whether, over a sealed window from Tue 15 Dec 2026 to Thu 14 Jan 2027 (extended by any outage), its 24-hour forecast of whether Mount Rainier's summit is visible beats persistence on the Peirce skill score (PSS). Persistence is the "no change" forecast: the summit will look as it did in an earlier frame. PSS is the hit rate minus the false-alarm rate. The skill in Snow Globe's forecast is NOAA's.
3. Today it is a set of tested parts and two running data collectors, not an app. The shell that joins the parts is not built, and no headline number is measured yet.

## Contents

- [Why this exists](#why-this-exists) · [What a query looks like](#what-a-query-looks-like) · [Status at a glance](#status-at-a-glance), with the [timeline](#timeline)
- [System architecture](#system-architecture) · [How a query becomes a picture](#how-a-query-becomes-a-picture) · [How a prediction becomes a score](#how-a-prediction-becomes-a-score)
- [Query understanding](#query-understanding) · [Data pipeline](#data-pipeline) · [Plates: calibrating a webcam](#plates-calibrating-a-webcam)
- [Rendering](#rendering) · [Latency engineering](#latency-engineering) · [Interfaces and file formats](#interfaces-and-file-formats) · [Evaluation design](#evaluation-design)
- [Headline numbers](#headline-numbers) · [Measured results so far](#measured-results-so-far)
- [Quick start](#quick-start) · [Running the collectors](#running-the-collectors) · [Command reference](#command-reference) · [Configuration reference](#configuration-reference)
- [Project structure](#project-structure) · [Testing and CI](#testing-and-ci) · [Infrastructure and cost](#infrastructure-and-cost)
- [Privacy, licensing and safety](#privacy-licensing-and-safety) · [Technical decisions and trade-offs](#technical-decisions-and-trade-offs) · [How it compares](#how-it-compares)
- [Roadmap](#roadmap) · [Documents](#documents) · [Credits and licences](#credits-and-licences)

## Why this exists

A weather app says "80 % low cloud". It does not say whether you will see the mountain from the visitor centre at 4 PM. A webcam shows the view now, not tomorrow, and an image generator's weather is invented. Snow Globe answers one narrow question, **what will this real camera see at this hour?**, in three stages that can each be tested on their own (PRD §0):
1. **Retrieval (Tier 0):** the camera's closest real photo, matched on the Sun's position and the time of year.
2. **Physically based relighting (Tier 1):** the camera's calibrated base image, relit from NOAA's HRRR forecast (the High-Resolution Rapid Refresh, an hourly model on a 3 km grid) and the true Sun and Moon.
3. **Guarded refinement (Tier 2):** photographic texture from an image model, which by construction cannot touch the sky or anything hidden by haze.

Every prediction is to be scored against the frame the camera recorded, against baselines fixed in advance (PRD §2.1). The closest prior work, a GAN (generative adversarial network) that turns a webcam image into its forecast view (AIES 2022, arXiv 2203.15601), was scored against the forecast's own conditions (PRD §2). The rules (PRD §2.2): data decides, and a model adds texture only; every frame is labelled real, live or simulated; the skill is NOAA's; a measured number outranks a feature.

## What a query looks like

As designed (PRD §3, §5): you hold the hotkey, and a floating panel appears without taking focus from the app you are typing in. You say a place and a time and let go. A real photo appears, then the relit frame, then, if it passes its checks, a photographic one. After you click the panel, the arrow keys scrub the valid time an hour at a time, or ten minutes with Shift. "Rainier tomorrow at 4" means 4:00 PM Pacific, with a chip offering 4:00 AM. "Golden Gate at 9", with no licensed Golden Gate camera, gets No match and the three closest places, never a guess (PRD §3.3).

| Tier | What it shows | Today |
|---|---|---|
| 0 | the camera's closest real frame, matched on the Sun's position and the time of year | built in the Rust core; the paint needs the app shell |
| 1 | the camera's plate (its calibrated base frame and masks), relit by a WebGL2 renderer (WebGL2 is the browser's GPU drawing interface) from the forecast and the Sun and Moon | renderer 0.3.0 runs headless; in the overlay from M2 |
| 2 | photographic texture, with the sky restored and checks that reject any changed weather | not built; a kill test costing at most $1 comes first |

Each tier's latency target is in [How a query becomes a picture](#how-a-query-becomes-a-picture). Every frame carries a caption, "the honesty layer" (PRD §5.3). Two of the PRD's examples:

```text
SIMULATED · Fri 20 Nov, 4:00 PM PST · cloud low 80% · ceiling 2,300 m · visibility 12 km
  HRRR 18Z Thu, issued 11:27 AM PST (lead 28 h 33 min) · Sun 3.1° · Moon 41% lit, below horizon
REAL PHOTO, CLOSEST MATCH · taken 14 Oct, 5:45 PM PDT · matched on Sun position and forecast class
```

"Issued" is when the forecast run became available; the lead is the valid time minus that. Tier 0 has no weather class yet (TODO(M1.5)), so the caption it prints today says what it really matched on, as in this example from its tests:

```text
REAL PHOTO, CLOSEST MATCH · taken Sun 22 Nov, 3:30 PM PST · matched on Sun position and time of year · Courtesy National Park Service
```

What runs end to end today is `snow-globe-ask`: a sentence in, the closest real photo and its caption out (D-097).

### The overlay, built without its window

The overlay's web layer, `@snow-globe/overlay` 0.1.0 ([app/src](app/src/README.md)), is built and tested in headless Chromium; the native panel and the hotkey wait for the shell.
- **States** (PRD §5.2): idle, listening (key down), resolving (key up), then showing Tier 0, 1 or 2. Esc, or a key-up with no speech, returns to idle, and a new key press cancels everything. A query can also end as No match (the transcript and the three closest places), Out of range (the past, or more than 7 days ahead) or an error. Offline, a notice says what the cached tiers can show.
- **What it shows:** a listening pill with a waveform and the partial transcript; chips for the place and viewpoint, the time (highlighted when it rolled forward), the AM/PM flip, a close call (`or <other place>?`), a daylight-saving change, a time beyond HRRR's 48 h and offline; the destination chip last, `on-device` or, as a warning, `→ <provider>`; three tier marks (filled, hollow when a tier was rejected, dashed when off); the 16:9 image, letterboxed and never cropped; and the caption.
- **Keys,** once the panel is clicked (PRD §5.4): Esc dismisses; Left and Right scrub an hour, or 10 minutes with Shift; M flips AM and PM; C copies the frame with the caption's first line and the credits burned in. With Cmd, Ctrl or Option held, a key passes to the system.
- **Motion and access** (PRD §5.5): Tier 0 appears at once; Tiers 1 and 2 cross-fade in 120 ms unless Reduce Motion is on. Text contrast is at least 4.5:1 against anything it can sit on, computed in tests, with a stronger palette under Increase Contrast. The caption's height is fixed, so no tier shifts the layout, and a live region announces each state.

## Status at a glance

A snapshot of Mon 23 Nov 2026 (UTC). [PLAN.md](PLAN.md) holds the plan, [PRD_PROGRESS.md](PRD_PROGRESS.md) every ticket and measured number, and [DECISIONS.md](DECISIONS.md) every decision, D-001 to D-118, with its evidence. Dates are offsets from day G, Thu 19 Nov 2026, when the owner accepted every default of PRD §1 (D-012). The planning docs record the work through wave 1, the five work packages committed on 23 Nov (D-092), and wave 2, committed the same day (D-105 to D-118).

| Milestone | Offsets | Dates | State |
|---|---|---|---|
| M0 Scout | before G | done Thu 19 Nov 2026 | done: the 15 calls of PRD §1, accepted on day G |
| M0.5 Start the clock | G → G+4 | Thu 19 → Mon 23 Nov | in progress: ≥ 72 h of logs, the archive report and one backtest pair (v−1) are done; one gate item is open |
| M1 Voice to first pixel | G+4 → G+18 | Mon 23 Nov → Mon 7 Dec | in progress |
| M1.5 Walking skeleton | by G+21 | by Thu 10 Dec | in progress (early) |
| M2 Relit frame | G+21 → G+38 | Thu 10 → Sun 27 Dec | in progress (early): the ephemeris validation, the Rainier plate and the HRRR datums begun ahead of it |
| M3 Photographic frame | G+38 → G+48 | Sun 27 Dec 2026 → Wed 6 Jan 2027 | not started |
| M4 Proof | sealed window + ~5 days | about Fri 15 → Wed 20 Jan 2027 | not started |
| M5 Ship | ~4 days | about Wed 20 → Sun 24 Jan 2027 | not started |

Each milestone's gate evidence (PRD §16) is in the [roadmap](#roadmap).

- **Built and tested,** as the [project structure](#project-structure) lists: the Rust core with Tier 0; the overlay's web layer, tested in headless Chromium with a simulated core; the speech library, tested on a mock engine only; the renderer; both workers; the evaluation package; the plate builder; the replay scorer; and the AWS Terraform for the two jobs, plan-tested on a mocked provider (CloudFront and the API are not in it yet). `make test` passed all 12 suites on wave 1's tree (D-092).
- **Built, never run:** the speech library on Apple's real recognizer, and the M1 platform spike's 11 checks (D-033, D-055).
- **Running:** two collectors on the development laptop, standing in for the AWS jobs: the capture loop over all 8 cameras since 12:24:23 UTC on 19 Nov, with the archive backfill inside it since 20 Nov, and the forecast stand-in. Both run from a frozen snapshot of commit `aee6f92` since the switch of 22–23 Nov (D-030, D-049, D-090). That snapshot predates the Rainier manifest's corrected camera position, so the stand-in still samples Rainier from the old one until it is re-snapshotted.
- **Committed after wave 1 (wave 2), 23 Nov (D-105 to D-111):** the Rainier manifest's camera moved to plate v0.1's solved position; a hindcast mode (forecasts for past frames, from archived HRRR runs) for P1's 24 h lead ([workers/forecast](workers/forecast/README.md)); a batch entry point for the renderer ([renderer](renderer/README.md)); extractor v0.2, checked leave-one-day-out on two blind labellers' labels; an ephemeris validation against JPL Horizons, Skyfield and NOAA; and a first development-only P1 table, still provisional ([eval](eval/README.md)). Their figures are in [Measured results so far](#measured-results-so-far).
- **Committed with wave 2's records:** [docs/ledger/](docs/ledger/), the inventory of every network flow the code makes or could make and the ledger of every licence (PRD §15.2, §15.4; D-105). The seven other cameras' plates are in progress: the plate builder held Rainier-only assumptions that blocked all seven (D-118).
- **Not built:** the Tauri shell (Tauri is a Rust desktop framework with a system web view), so no hotkey, panel or app window; Tier 1 in the overlay; Tier 2; the LLM (large language model) parser fallback; the API and the prediction issuer; the dashboard; the AWS deployment; `make score`, which says so and exits 2.
- **Must land before the sealed window opens on Tue 15 Dec:**
  - an extractor that passes PRD §13.8's bar on a full validation sample, because the freeze's visibility threshold τ comes from it (D-100); v0 failed, and v0.2's first check is not yet publishable ([eval](eval/README.md));
  - live forecast files that cover P1's 24 h lead. The stand-in keeps only each run's first three forecast hours, f00–f02, so it serves a lead of about 1 h; the leads to 24 h need the AWS job or a longer profile on the laptop (D-101, open);
  - a valid near band for Rainier's K2, the low-visibility secondary claim (D-099);
  - unattended uptime: every capture outage so far was the laptop asleep (D-089, D-104, D-117), and from 15 Dec each outage extends the window.
- **Recorded misses**, with values and bottlenecks in [Measured results so far](#measured-results-so-far): extractor v0's summit F1 (D-100); plate v0.1's near band (D-099); the cross-fade's time to 50 % (D-094); the renderer's 1080p p95 in headless Chromium, for the heavy scene and, in 3 of 6 runs, the clear one (D-098); and one refracted Sun epoch in the ephemeris validation ([eval](eval/README.md)).
- **Breaches of the project's rules,** all on 19–20 Nov, each fixed in code:
  - 22 archived frames past the held-out date were viewed during the archive check; they were quarantined, and 15 Jul 2026 left scoring (D-022);
  - two NPS fetchers ran at once, so Rainier got 5 image downloads in an hour against the cap of 4, and two HEADs 53 s apart (D-023);
  - the scaffold build made 9 nps.gov requests while the stopgap ran (D-045);
  - polls came up to 0.94 s early: 8 of 504 gaps were under 300 s (D-048);
  - one pair of archive requests was 4.9944 s apart (D-077).

  From capture 0.2.1's start at 19:56 UTC on 20 Nov to the last counts on 23 Nov, every poll pair is provably at least 300 s apart at nps.gov and every archive gap is at least 5.05 s (D-089, D-090, [PRD_PROGRESS.md](PRD_PROGRESS.md)).

### Timeline

Times are UTC; commit and push times are from git.

- **Thu 19 Nov 2026, day G.** M0's documents are committed at 04:29 (D-001): research notes, PRD v0.3, a council of four judges over two rounds, and a check against JPL Horizons of oculus-core, the Rust Sun and Moon library the project reuses (D-002). PRD v0.4, a synthesis of two drafts, follows at 06:14, and the owner accepts all 15 PRD §1 defaults (D-012). A stopgap poller captures Rainier from 06:30, because that camera has no archive (D-021). During the NPS archive check, 22 archived frames past the held-out date are viewed, so they go to quarantine and 15 Jul 2026 leaves scoring (D-022). Two NPS fetchers also run at once, so Rainier gets 5 image downloads in an hour against the cap of 4; every fetcher now takes a host-wide lock (D-023). **The capture loop takes over all 8 cameras at 12:24:23, starting the 72 h clock** (D-030). The first backtest pair runs at 23:13, labelled v−1: a crude pipeline test that counts toward no metric.
- **Fri 20 Nov.** The storage policy and archive queue go live (D-028, D-049); the first backfill tranche (D-052); a survey of 194 HRRR runs' lags (D-053). After polls came up to 0.94 s early on 19 Nov, polls are spaced from the previous answer (D-048, D-065), and after one archive pair came 4.9944 s apart, the archive limiter waits out the wall clock (D-077); capture 0.2.1 runs both from 19:56 (D-079).
- **Sat 21 Nov.** The first push to GitHub at 00:48 (D-080); the rename to Snow Globe, pushed at 06:53 with CI passing 9 of 9 jobs (D-081); capture 0.3.0 is committed (D-083), and it goes live at the switch.
- **Sun 22 Nov.** **M0.5's 72 h log item is met at 23:46:10.754** (D-089); the rename's live switch then moves the data and restarts both collectors (D-090).
- **Mon 23 Nov.** On battery with its lid closed, the laptop sleeps twice in the morning, stopping capture for 41 and 49 min (D-104). Wave 1, five work packages each reviewed for correctness and against the spec, then fixed and verified, is committed at 09:56–09:57 and pushed at 11:13 (D-092): Tier 0, the replay scorer with trace schema 2, the overlay's web layer, the Hillaire sky, and plates with extractor v0. Two Linux-only test failures are fixed at 11:28, and the docs record wave 1 at 11:40. Wave 2's parts are committed from 14:05: the manifest correction, the hindcast mode and the batch renderer (14:05–14:09), extractor v0.2 (19:37), the ephemeris validation (20:26) and the dev-only P1 table (20:37). In the afternoon the laptop sleeps again on battery with its lid closed, and no camera stores a frame from 15:48 to 19:50 (D-117). The planning docs record wave 2 and the ledger is committed at 22:46 (D-105 to D-118).

## System architecture

The app and the evaluator read the same forecast file, so the forecast behind a scored prediction is the one the app showed (PRD §6.2), and one renderer serves both. The app path, as designed:

```text
            hold the hotkey, speak a place and a time, release
                                  |
                                  v
  +--------------------------------------------------------------+
  | Tauri 2 shell (app/src-tauri)                  [not built]   |
  | global hotkey, non-activating panel, cpal audio while held   |
  +-------------------------------+------------------------------+
                                  | audio samples, in RAM only
                                  v
  +--------------------------------------------------------------+
  | Swift speech library (app/speech-macos)                      |
  | on-device DictationTranscriber: partial, stable, final text  |
  | [built; never run on the real recognizer]                    |
  +-------------------------------+------------------------------+
                                  | final transcript, through a C ABI
                                  v
  +--------------------------------------------------------------+
  | Rust core (app/core)                                         |
  | parser and time rules -> query; Sun and Moon (oculus-core);  |
  | query_gen and the paint gate; trace; Tier 0 match + decode   |
  | [built and tested]                                           |
  +-------------------------------+------------------------------+
                                  | protocol v1: JSON messages over the
                                  | shell's IPC [the IPC is not built]
                                  v
  +--------------------------------------------------------------+
  | Overlay web layer (app/src), in the shell's webview          |
  | states, chips, caption, frame cache, webview paint gate;     |
  | Tier 2 composite and checks [planned]                        |
  | [built; tested in headless Chromium only]                    |
  +---------------+----------------------------+-----------------+
           Tier 1 |                            | Tier 2
                  v                            v
  +------------------------------+   +---------------------------+
  | Renderer (renderer/), WebGL2 |   | Tier 2 proxy and          |
  | plate + SceneSpec -> frame   |   | image model               |
  | [built; headless only]       |   | [not built]               |
  +------------------------------+   +---------------------------+
```

The data path, which runs today:

```text
  nps.gov: 8 park webcams + archive      NOAA HRRR on S3 (anonymous HTTPS)
                  |                                      |
                  v                                      v
  +---------------------------------+    +---------------------------------+
  | snow-globe-capture loop         |    | forecast stand-in, every 5 min  |
  | 1 fetcher; <= 1 poll / 5 min;   |    | .idx byte ranges -> ecCodes ->  |
  | <= 4 images / camera / hour;    |    | samples along each view ray;    |
  | archive 5.05 s apart, 1,000/day |    | 8 fields, f00-f02, <= 40 MB/run |
  | [running on the laptop]         |    | [running on the laptop]         |
  +---------------+-----------------+    +---------------+-----------------+
                  |                                      |
                  v                                      v
  frames/ thumbs/ archive/               forecast/hrrr/<vp>/<run>z.json
  + a JSON sidecar per image             + forecast_log/ (availability lags)
                  |                                      |
      +-----------+-------------+                        |
      v           v             v                        v
   Tier 0      plates/       eval/               SceneSpec for the
   library     pose,         extractor,          renderer (Tier 1);
   (app/core)  masks         baselines,          eval/ S_rule
                             scores
```

The evaluation path, as designed ([How a prediction becomes a score](#how-a-prediction-becomes-a-score)):

```text
  issue time t                                    valid time v = t + lead
      |                                                    |
      v                                                    |
  the newest HRRR run whose hours bracketing v             |
  were available by t: forecast/hrrr/<vp>/<run>z.json      |
      |   + the plate + the Sun and Moon at v              |
      v                                                    v
  SceneSpec --> renderer, headless, on the CPU      the camera's frame at v
                      |                                    |
                      v                                    v
             extractor: prediction                 extractor: truth
                      |                                    |
                      +-----------------+------------------+
                                        v
          hits, misses, false alarms and correct negatives, by local day
                                        |
                                        v
      PSS; ΔPSS against persistence, day-block bootstrap, 95 % interval
```

1. The shell, on Tauri 2, will own the hotkey and the panel; `cpal`, a Rust audio library, will open the microphone only while the key is held. The Swift library wraps Apple's on-device `DictationTranscriber` behind a 10-function C ABI (a plain C calling interface).
2. The Rust core turns the transcript into a query and computes the Sun and Moon with oculus-core. It will drive the overlay through protocol v1, the IPC (inter-process communication) contract the shell implements (D-093), and the overlay gates every frame just before it draws.
3. The collectors fill the data dir; the evaluation reads it offline and renders headless with the same renderer.
4. In the shipped app, the core will take manifests and forecast files from the project's API by signed URL, and live frames from `GET /v1/live/{viewpoint}`. The app itself never polls nps.gov, so the polite caps hold however many people ask (PRD §11.1; the API is M2's and not built). Today `snow-globe-ask` reads the local data dir.

## How a query becomes a picture

The request flow as designed (PRD §6.3). Today the core runs steps 3 and 4 inside `snow-globe-ask`:
1. **Key down.** The panel appears, the microphone and a speech session open, and a new generation number, `query_gen`, cancels every older piece of work.
2. **Key held.** When a partial transcript names a known place with a match score of at least 0.9, and no other place comes close, the core is to load that viewpoint's plate, forecast file and Tier 0 thumbnails; a final transcript naming another place cancels the load (PRD §7.2). The parser's match for this exists; the prefetch itself is TODO(M1).
3. **Key up.** The final transcript is parsed, the time resolved in the place's zone, the Sun and Moon computed.
4. **Tier 0 by 300 ms.** The closest frames for every minute of the next 48 h are already decoded, so this is a lookup and a swap.
5. **Tier 1 by 1 s.** The renderer draws the plate under the SceneSpec (the frozen inputs of one render, [defined below](#tier-1-the-relit-plate)), and it cross-fades in over 120 ms.
6. **Tier 2 by 3 s,** only if it passes its checks.
7. **After.** A trace line is written, holding no transcript text. A question about the future is to be logged as a graded ask, scored when it falls due and reported apart from P1 (PRD §13.2). P1's cases come from an hourly issuer, not from what anyone asks. Neither the log nor the issuer is built.

The budgets (PRD §10.1), in ms, p95 from the OS key-up to the composited paint, idle:

| Tier | Target | How the initial allocation adds up |
|---|---|---|
| 0 | ≤ 300 | final transcript 240 + parse 5 + swap 10 + composite 33 = 288 |
| 1 | ≤ 1,000 (stretch 400) | 240 + 5 + GPU 50 + fade to 50 % 60 + composite 33 = 388 |
| 2 | ≤ 3,000 (the share within 3 s) | 240 + 5 + render 80 + proxy 100 + upload 250 + inference 1,800 + download 200 + checks 30 + fade 60 + paint 33 = 2,798 |

The final transcript dominates every tier. The best independently measured hosted speech-to-text p95 is 298 ms, and on-device reports disagree by five times (PRD §10.1). So the bake-off measures every engine at idle, and the shipped default stays on device unless the owner switches after seeing those numbers (D-004, D-015). Only the core's share is measured so far: under 0.4 ms at p95 (D-097). The cross-fade, timed as page timestamps in headless Chromium, misses its 60 ms allocation: its median time to 50 % was 83.3 ms in two of the three runs and 66.7 ms in the third. With 83.3 ms in place of 60 ms, the Tier 1 allocation sums to about 411 ms, past the 400 ms stretch target but inside the 1,000 ms one; with 66.7 ms it sums to about 395 ms, inside both. No Tier 1 paint has been measured (D-094).

## How a prediction becomes a score

The evaluation path as designed (PRD §6.2, §11.2, §13):
1. **Issue.** For a valid time v and a lead, the issue time is t = v − lead. The forecast is the newest HRRR run whose hours bracketing v were available by t, and the lead is v minus that availability ([lead time](#hrrr-forecast-extraction)). An hourly issuer will do this for leads of 1, 3, 6, 12, 24 and 48 h (PRD §11.2; not built).
2. **Freeze.** A SceneSpec is built from that forecast file, the viewpoint's plate, and the Sun and Moon at v. Rendering is pure, so freezing the inputs is issuing the prediction.
3. **Render.** The renderer the app uses draws it headless on SwiftShader, a CPU implementation of a GPU.
4. **Extract, twice.** One extractor reads the render (the prediction) and the frame the camera took at v (the truth). Each comes out visible, partial or hidden, or is set apart as night, camera fault, excluded by a gate, no reference or camera in cloud ([the extractor](#the-extractor)).
5. **Baselines.** Persistence, climatology and S_rule (a rule on HRRR's ceiling and cloud cover) see only the frames and forecasts available by t, with the same exclusions.
6. **Score.** Hits, misses, false alarms and correct negatives give PSS, the Peirce skill score: the hit rate minus the false-alarm rate ([below](#scores-and-baselines)). ΔPSS against persistence is resampled by whole days, the same days for both, and P1 is claimed only if its 95 % interval excludes 0 ([evaluation design](#evaluation-design)).

M0.5's backtest pair (v−1) ran steps 1–4 once on archived data, and counts toward no metric. Since 23 Nov, `snow-globe-eval p1dev` runs all six steps on Rainier's development frames, with 24 h hindcasts and extractor v0. Because v0 fails its bar, that table is provisional, and none of its scores is reported here (PRD §13.8; [eval](eval/README.md)).

## Query understanding

Speech becomes a structured query within milliseconds of the final transcript, with no language model in the common case (PRD §7). The parser is a hand-written grammar in Rust ([app/core](app/core/)) with an alias table compiled in from the manifests. It normalises the text ("4pm" → "4 pm"), protects known names, strips carrier phrases ("show me"), reads the time with eight matchers where the longest match wins, drops fillers, matches the place, and resolves the time in the place's zone. Every word is accounted for: a stray word beside a name ("rainier beach") or one that looks like a time ("thanksgiving") makes the query `unrecognised`, the input for the planned LLM fallback (TODO(M3)).

**Time rules** (PRD §7.4), all in the place's zone:

| You say | Rule | Resolves to |
|---|---|---|
| "at 9" | `bare-hour` | the next 9:00 or 21:00; the other is the AM/PM chip |
| "tomorrow at 4" | `day-plus-bare-hour` | 7–11 AM, 12 noon, 1–6 PM; the other half of the day is the chip |
| sunrise, sunset; dawn, dusk | the event | the Sun's centre at −0.833°; at −6° (a proposed reading, D-058) |
| golden hour, blue hour | `golden-hour`, `blue-hour` | +6° to −4°, rendered at +2°; −4° to −6°, rendered at −5° |
| morning, afternoon, evening, night | `part-of-day` | 9:00, 15:00, 19:00, 22:00 |

A wall time that a daylight-saving change skips moves forward (02:30 on 14 Mar 2027 in Los Angeles becomes 03:30 PDT). A repeated hour takes the earlier offset, or the later one when asked during that hour after the first has passed; both cases are flagged (D-039). The past, or more than 7 days ahead, is Out of range. Beyond HRRR's 48 h the query is flagged for the GFS (NOAA's Global Forecast System), which has no code yet.

**Place matching** is a cascade, each stage tried only when the one before found nothing (PRD §7.5): the manifests' 77 aliases ("point rays"); exact names; a normalised Levenshtein (edit-distance) score of at least 0.80, accepted only when anchored by a word that is not generic like "beach" ("grnd canyn" matches, "pebble beach" misses); then Double Metaphone phonetic keys ("rayneer"). Two close places give a chip; a miss returns the three closest.

**The query** has PRD §7.3's ten fields, then four more (D-040). The core's test `prd_example_end_to_end` pins the first ten for "Is Rainier out tomorrow at 4?", asked at 10:00 PST on 19 Nov:

```text
{"place_id":"rainier","viewpoint_id":"rainier.paradise-mountain",
 "valid_time":"2026-11-20T16:00:00-08:00","valid_time_utc":"2026-11-21T00:00:00Z",
 "tz":"America/Los_Angeles",
 "time_source":{"kind":"clock","text":"tomorrow at 4","rule":"day-plus-bare-hour"},
 "ambiguities":[{"field":"meridiem","chosen":"pm","alternative":"2026-11-20T04:00:00-08:00"}],
 "overrides":{},"path":"fast_path","confidence":0.97,
 "place_source":"utterance","place_match":{"stage":"alias","score":1.0},"horizon":"hrrr","flags":[]}
```

Confidence is a fixed heuristic until the replay set calibrates it (TODO(M3)): 1.0 for an alias or an exact name, times 0.97 when an AM/PM chip is offered, so 0.97 here.

**Ephemeris.** oculus-core (MIT), pinned to its public commit `1b5410b` (D-025), computes the Sun from JPL's mean orbital elements and the Moon from a truncated ELP-2000/82 series, with aberration, precession, nutation and topocentric parallax (PRD §8.4). Solar events come from a 1-minute grid that skips ahead by how far the Sun is from the threshold (its altitude changes at most 0.26° a minute), then bisection to 1 ms: about 40 Sun evaluations a day instead of 1,440.

**Implementation:** `app/core/src/parser.rs`, `resolve.rs`, `timeparse.rs` and `places.rs`, with the cases in `app/core/tests/parser_table.rs`; solar events in `app/core/src/solar.rs`.

## Data pipeline

### The eight cameras

Design P scores live frames on the sealed window; design H scores archived frames against archived forecasts on a held-out season ([details](#designs-h-and-p-and-the-held-out-data)). Seven of the cameras belong to the NPS Air Resources Division (ARD), whose cameras publish a JSON feed, a frame every 15 minutes and an archive; all seven switched from a 3:2 to a 16:9 frame in 2023–25, so design H uses the 16:9 epoch only (D-024).

| Viewpoint | Camera | Design | Frames kept | NPS archive |
|---|---|---|---|---|
| `rainier.paradise-mountain` | park camera, 1920×1080, about one image a minute | P | every frame | none |
| `point-reyes.beach` | ARD camera, looks NNE | P, and H for K2 | every frame | from 2004 |
| `grand-canyon.yavapai` | ARD camera, looks NW | H | nearest each UTC hour | from 2001 |
| `grand-teton.science-center` | ARD camera, looks W | none: its framing changed inside its 16:9 epoch | nearest each hour | from 2011 |
| `yosemite.turtleback` | ARD camera, looks SW | H | nearest each hour | from 2005 |
| `acadia.mcfarland` | ARD camera, looks NE | H | nearest each hour | from 2006 |
| `smokies.look-rock` | ARD camera, looks E | H | nearest each hour | from 1998 |
| `big-bend.panther-junction` | ARD camera, looks N | H, after a framing check | nearest each hour | from 2004 |

Rainier's park camera has no archive, so a Rainier hour not captured is lost. Frames are kept at most 1920 px wide, with a 640 px thumbnail of every download (D-028), and each viewpoint is a schema-checked manifest in [viewpoints/manifests/](viewpoints/manifests/).

### Live capture and the archive backfill

Each pass of [workers/capture](workers/capture/README.md) polls a camera no sooner than its last answer + 300 s + 2 ms. nps.gov receives each request no later than it answers it, and the next one no earlier than it is sent. So nps.gov's gap between two polls is at least the next send minus the previous answer, which the scheduler keeps at 300.002 s or more whatever the network does (D-065). Each poll is reserved and saved before it is sent, and a request not on its way 30 s after its reservation is abandoned, so a crash or a sleep cannot squeeze two polls together. A conditional GET of the feed (a HEAD at Rainier) decides whether the image changed. An image download goes out only while fewer than 4 were answered in the last hour, and at least 720 s after the previous one's answer. Each frame gets a JSON sidecar with its image time, digests, sharpness, the Sun's position and flags, and every request is logged to append-only JSONL.

NPS's AirWebCams archive is an undocumented API, found in a page script, with a day list, a search that returns each frame's local time and Sun angle, and 999 px downloads (D-018, D-024). The backfill runs only inside the capture loop, between passes (D-029): one request at a time, at least 5.05 s apart so the logged stamps prove the 5 s gap (D-077), at most 1,000 a UTC day and 5 GB in all (D-052). It is resumable and never downloads anything twice (D-018, D-029), and since capture 0.3.0 it resumes failed day searches by itself (D-083). It refuses any archived frame whose date, read locally or in UTC, falls on or after 1 Jun 2026 or cannot be read (D-070).

### Polite collection, enforced in code

| Rule (PRD §15.3) | Enforced by |
|---|---|
| Poll each camera at most every 5 minutes | the poll slot, counted from the previous answer and persisted; the manifest schema requires `poll_interval_s` ≥ 300 |
| Download only on change, at most 4 images per camera per rolling hour | the change check, the rolling-hour cap counted on answer times, and 720 s pacing, persisted per camera |
| Archive: one request at a time, ≥ 5 s apart, resumable, size-capped | the file-locked archive limiter, the queue, the daily budget, the 5 GB cap |
| Back off on errors | per-stage back-off, and the shared nps.gov hold on a 429 or 503 |
| Never two NPS fetchers at once | a host-wide lock: `once`, `loop`, `archive days\|day\|sample` and `archive-check` exit with status 3, before sending anything, while another fetcher is active |
| Identify the project | the default User-Agent `SnowGlobe/0.1 (webcam research capture; fetch-on-change)`; `SNOW_GLOBE_USER_AGENT` overrides it, and by the project's rule an override never names a person (the code does not check it) |

PRD §15.3 also asks for the repository URL in the User-Agent; the default leaves it out for now (D-016).

### HRRR forecast extraction

HRRR runs start every hour; the 00, 06, 12 and 18Z (synoptic) runs reach 48 h ahead (f48) and the rest f18, and NOAA publishes each hour to the public S3 bucket `noaa-hrrr-bdp-pds`, f00 about 51 minutes after the run starts (PRD §8.1). The worker ([workers/forecast](workers/forecast/README.md)) reads each hour's `.idx` inventory, fetches only the byte ranges it needs, and decodes them with ecCodes (the standard library for GRIB2, the WMO's gridded binary format), checking each message's identity and grid. It samples each viewpoint at the camera's cell, 30 points along the view azimuth out to 45 km, each landmark and the 5 × 5 block around the camera, writes one validated JSON file per viewpoint per run, and deletes the GRIB. Picking, per layer, the cell where the view ray meets that layer's height needs the camera pose (TODO(M2)).

The stand-in keeps 8 of 22 defined fields: visibility, ceiling, cloud-base height and pressure, low, mid and high cloud cover, and the model terrain (D-050). The pressure is kept because HRRR's cloud-base height is often invalid: on one run, 12.4 % of cloudy cells had a phantom base 7.4–8.3 km too low (D-026; masking it is TODO(M1.5)). Herbie, a common HRRR library, is not used: it sent another User-Agent and left requests without timeouts (D-026).

**Lead time is measured from availability:** the valid time minus the moment the forecast could be used. For a live run that is the later of its S3 stamps (GRIB2 and `.idx`) and the moment our fetch of it finished (D-008, D-027). An archived hour gets the run start plus a lag modelled by cycle and forecast hour, never earlier than its own S3 stamps (D-054). Only the 00, 06, 12 and 18Z runs reach past f18, so a "24 h" forecast has an actual lead of 24–29 h (PRD §8.3). No run covers a true 48 h lead, and how that row is reported is open (D-102). The stand-in keeps f00–f02, so its files serve design P's 1 h lead at most (D-101).

**Hindcast mode** (`snow-globe-forecast hindcast`, committed on 23 Nov) fetches the archived runs that P1's 24 h lead needs for the development frames, in the live files' format, writing only under the data dir's `forecast/hindcast/`. It waits out a quiet window around the stand-in's fetches and refuses past a 2,000 MB total. It dates each hour by its S3 stamps alone, which is not D-054's rule; which rule P1's development table uses is open ([workers/forecast](workers/forecast/README.md)).

**Implementation:** `workers/capture/src/snow_globe_capture/` (`live.py`, `state.py`, `archive.py`, `archive_queue.py`, `fetcher_guard.py`, `heldout.py`) with `workers/common/src/snow_globe_common/http.py` and `paths.py`; `workers/forecast/src/snow_globe_forecast/` (`extract.py`, `idx.py`, `decode.py`, `points.py`, `availability.py`, `lambda_handler.py` and `hindcast/`).

### Data layout

Data lives outside the repository and outside iCloud, under `$SNOW_GLOBE_DATA_DIR` (D-017), and the Python and Rust resolvers refuse a data dir inside a git work tree or, unless told otherwise, an iCloud-synced folder.
With neither variable set, the code takes `~/snow-globe-data` if it exists, else the old `~/lookout-data` if it exists, else `~/snow-globe-data`; while both exist as different folders, every resolver refuses until a variable names one. The old `$LOOKOUT_DATA_DIR` is still read, and since the night of 22 Nov the old path is a symlink to the new folder (D-080, D-090).

```text
$SNOW_GLOBE_DATA_DIR/
  frames/<viewpoint_id>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>_<sha256[:12]>.jpg (+ .json)   live frames
  thumbs/, archive/                                   thumbnails; the NPS archive backfill
  capture_log/, forecast_log/, state/                 append-only events and lags; limits, queue, locks
  forecast/hrrr/<viewpoint_id>/<YYYYMMDDHH>z.json     one file per viewpoint per HRRR run
  forecast/hindcast/                                  the P1 hindcast's files, log and state
  scores/, renders/, labels/                          evaluation runs and renders; labelling samples
  plates/, cache/, reports/                           plate sets; deletable caches; saved evidence
  quarantine/                                         never opened (D-022)
```

## Plates: calibrating a webcam

A plate set is what the renderer and the extractor know about one fixed camera: a base frame in soft light; the camera's pose (heading, pitch, roll, horizontal field of view) and position; and masks for the sky, the caption strip, near trees and landmark bands by world height, each named in a `plate.json` with its SHA-256. `snow-globe-plates` ([plates](plates/README.md)) builds one offline from our own captures, a USGS elevation model and star positions (PRD §8.5):

1. **The spec** ([plates/specs/rainier.paradise-mountain.v0.1.json](plates/specs/rainier.paradise-mountain.v0.1.json)) holds everything decided by viewing frames: base-frame candidates, landmark pixels and their uncertainty, night frames, band edges and a tree occluder.
2. **The input guard:** every frame must be one that eval's committed held-out split lets a fit use, with bytes that match its SHA-256.
3. **Stars:** `match-stars` finds Bright Star Catalogue stars in the camera's infrared night frames, at their apparent places, in five passes of shrinking search radius.
4. **The joint pose solve:** six parameters by least squares; the stars carry the orientation, and three landmarks from USGS's GNIS place names carry the camera's east and north position, with the lens assumed to sit 10 m above the ground. The landmark angles, 0.12–0.28°, are fit residuals, not a check, because the position is fitted to the same three landmarks; a leave-one-out check needs a fourth. The orientation from the stars is good to about 0.1–0.2°, limited by the pinhole camera model: 40 of the last pass's 77 star matches fall outside its 4 px cut (TODO(M2)). So PRD §14's pose row waits for M2's pose report.
5. **The ray cast and masks:** for every pixel, a ray through a USGS 3DEP bare-earth elevation model (DEM) finds terrain or sky; each band is a world-height range (at Rainier: summit ≥ 4,000 m, upper, mid, near), less the sky, the occluder and the caption.
6. **The independent check:** each band's skyline in the DEM's view against the one a clear frame shows; a band passes at p90 ≤ 1.0°, PRD §8.5's reprojection limit, and one that fails is marked not valid.
7. **The base frame:** the per-pixel median of soft-light frames that register with zero shift; not dehazed yet (TODO(M2)).

One current plate exists, Rainier v0.1 (D-099), which supersedes a v0 built the same night. Its solved camera stood 457 m north-east of the manifest's assumed position, and since 23 Nov the manifest carries the solved position, with a 200 m uncertainty ([viewpoints/manifests/](viewpoints/manifests/)). The summit, Columbia Crest, is hidden behind the dome's nearer slope, so **P1's summit band is the visible dome above 4,000 m**. Its near band fails the check, so Rainier's K2 band waits on a fix ([results](#plates-and-the-extractor)). Dehazing, night lights, depth, a sunset-shadow pose check and Point Reyes's plate are TODO(M2). The star catalogue's terms are open, and none of it is in the repository (D-103).

**Implementation:** `plates/src/snow_globe_plates/` (`build.py`, `joint.py`, `starmatch.py`, `dem.py`, `masks.py`, `skyline.py`).

## Rendering

### Tier 0: the closest real frame

Tier 0 lives in the Rust core (`app/core/src/tier0/`). Its library indexes capture's sidecars, never the images, and excludes with a reason anything held out, too small, of another framing (the archive's 3:2 epoch) or unusable. It compiles in eval's committed split, and only frames taken and fetched before the time of asking qualify, as in evaluation. Frames from the sealed window never enter its library, so during the window Tier 0 shows only older frames (D-097). The match uses the Sun's altitude, its azimuth relative to the view and the time of year; the forecast's weather class is TODO(M1.5):

```text
d = sqrt(A² + Z² + Y²)
A = |g(h_frame) − g(h_query)|,  where g(h) = 6° · asinh(max(h, −18°) / 6°)
Z = w(h_query) · |Δazimuth| · cos(mean altitude) / 15°,  w = 1 at ≥ 0°, falling to 0 at −12°
Y = |Δt mod 365.2422 days| / 45 days
```

The asinh scale keeps a degree a degree near the horizon, where light changes fastest. A frame qualifies at d ≤ 4.0; otherwise the answer is "no close analog", never a guess. The scales are documented, not fitted, until the pre-registration fits them (D-097). **Implementation:** `app/core/src/tier0/` (`matcher.rs`, `library.rs`, `caption.rs`, `ask.rs`).

### Tier 1: the relit plate

`render(scene: SceneSpec, target) → Frame` is a pure function of the scene and the renderer's version (PRD §9.2). A **SceneSpec** holds the camera, the valid time and run start, the Sun and Moon, the forecast's cloud layers, ceiling, visibility and wind, the haze and the plate's hashes, under a JSON Schema. The renderer's `scene_hash`, the SHA-256 of its canonical JSON, names the exact inputs of one render; it is not the paint gate's scene hash ([Latency engineering](#latency-engineering)). What version 0.3.0 has ([renderer](renderer/README.md)):

| PRD §9.2 pass | In 0.3.0 |
|---|---|
| 1 Atmosphere: Hillaire 2020 lookup tables | built |
| 2 Sun and Moon disks | built; the caller supplies their positions until oculus-core's WASM build (TODO(M2)). The scene carries the geometric altitude, so near the horizon a disk sits up to 0.64° below where it appears ([eval](eval/README.md)) |
| 3 Clouds: slabs along each view ray, with Earth's curvature | built; the ceiling's overlap rule is TODO(M1.5) |
| 4 Light: the plate × (sky irradiance + direct sun × cloud transmittance) | built, without DEM shadows or normals (TODO(M2)) |
| 5 Night lights; 7 Precipitation | not built |
| 6 Haze and fog | built, as flat fog |
| 8 Camera: response, exposure, monochrome mode | exposure and a tone curve |

**The Hillaire 2020 atmosphere** draws small lookup tables on the GPU: transmittance and multiple scattering when the atmosphere changes, and the sky view, irradiance and aerial perspective (the haze between the camera and each point) every frame. One model is valid at every Sun angle, twilight included. The earlier plan stitched Hosek–Wilkie, used only with the Sun up, to a separate twilight model, which put a seam at sunset, the moment people ask about most (D-010, D-013). Renderer 0.3.0 ports the MIT-licensed reference implementation, adds an exact aerosol haze layer, and extends aerial perspective to 500 km because sight lines reach hundreds of kilometres. It carries its own trigonometry, because SwiftShader's is off by up to 1.9e-4 (D-098). The plate is lit by the drawn sky: diffuse light from the same sky-view table, direct light through the same transmittance table.

**Clouds** are up to four slabs (low, mid, high and a ceiling deck), each with a base, a thickness and a cover profile along the view. Along each view ray the shader samples a noise field normalised to exactly standard normal and marks a sample cloudy when the noise is below Φ⁻¹(cover), the standard normal quantile, so a share `cover` of the samples is cloudy. The threshold comes before the average: averaging first would pull the field toward its mean, and the Monte Carlo's control that filters first misses by 93.6 points. So the drawn cloud fraction follows the forecast's cover. The field drifts with the wind, and its seed hashes the viewpoint and run, so frames are reproducible.

**Fog:** HRRR's visibility is a near-surface diagnostic, not haze along the sight line (PRD §9.3), so fog applies only to the part of each ray within about 100 m of the ground. Visibility V gives β = −ln(ε)/V (Koschmieder's 3.912/V at the default ε = 0.02), and only its excess over the clear air's own extinction at the ground is drawn as fog.

**Determinism.** Every pass has a float64 CPU twin in TypeScript that the GPU is tested against. A headless runner drives Chromium through Playwright (a browser-automation library) on SwiftShader, so the evaluation gets frames without a GPU, byte-identical from run to run on the same platform. Three synthetic goldens must match pixel for pixel on darwin/arm64, the platform they were recorded on, and reach SSIM (structural similarity) ≥ 0.98 elsewhere, as on CI's Linux runner. Removing a golden's named feature must change at least 10 % of its pixels, so a golden cannot pass by drawing nothing; removing the low deck changes 19.0 %. Keeping the goldens in git awaits sign-off (D-041).

**Implementation:** `renderer/src/webgl/` (`renderer.ts`, `shader.ts`, `atmosphere-glsl.ts`), `renderer/src/atmosphere/model.ts` and `renderer/src/scene.ts`.

### Tier 2: guarded refinement, planned

Nothing of Tier 2 is built, and no paid call has been made. The plan (PRD §9.6, §9.7) starts with a kill test: 20 Tier 1 frames through an image-to-image model (Z-Image-Turbo) for at most $1. Only if it passes: the sky comes back through its mask, only high-frequency ground detail is transferred, the prompt comes from the scene's parameters and never the user's words, and frozen guardrails check registration, edges, landmarks, brightness, colour and fog. Before any pass rate is reported, at least 200 deliberately weather-altered frames must give 0 accepts, which bounds false accepts at 1.5 %. Tier 2 then ships on by default only if at least 80 % of queries get an accepted Tier 2 within 3 s and a blind test of at least 50 pairs, judged by someone else, prefers it in at least 70 %. Otherwise it ships off, and this README will say why.

## Latency engineering

**Where it is measured** (PRD §10.4): from the OS key-up event. The replay harness injects it with `CGEventPost` at the HID (human interface device) tap, where hardware input enters WindowServer, macOS's window server and compositor, so the real hotkey path fires. Only the harness holds a listen-only tap, and the tap is compiled out of release builds. The paint is the first frame that WindowServer shows with the tier at 50 % opacity or more, read by ScreenCaptureKit from a pixel tag inside the frame. Capture on and off must differ by under one vsync, 16.7 ms at 60 Hz. The headline state is idle, the first query after 30 untouched minutes, on a fanless MacBook Air M4 (PRD §2.4).

**The paint gate.** Every key press gets a generation number, and a new press cancels everything older (PRD §10.2). Every paint is gated on (generation, scene hash, tier), in order (PRD §10.3):
1. a frame of another generation is dropped;
2. after Esc, nothing paints;
3. a frame of another scene is dropped;
4. a tier at or below the one shown is dropped.

A **stale paint** is any frame drawn that is not for the latest key press, comes after an Esc, is not of the scene selected now, or is not above every tier already shown for that scene. The gate's scene hash is the core's 64-bit FNV-1a hash of the viewpoint, the valid instant and the overrides, sent as 16 hex digits because a 64-bit JSON number would round in JavaScript (D-093).
- **Twice, rule for rule:** the gate exists in [app/core](app/core/) and in [app/src](app/src/README.md). The overlay checks a frame when it arrives (a refused frame is never decoded) and again in the frame callback that draws it, in the same synchronous block as the draw. A broken variant that drew one frame late made 171 stale paints.
- **Two stress tests:** a seeded Rust model, and the real overlay in headless Chromium, 64 lanes each with a simulated core injecting key mashing, Esc, late and lost results and failing decodes. Ten counts must be 0, and ten deliberately broken overlays must each be caught. The browser harness judges staleness at the draw. In one instrumented run, 45 of 806 cross-fades reached 50 % after a scrub had selected another scene, which PRD §10.4's paint capture would count as current paints; that is open (D-094).
- **The skip rule:** a ready Tier 0 waits only while a Tier 1 of the same scene is decoding or announced as due within 100 ms (PRD §1, row 11; D-093).
- **Privacy on the same channel:** the overlay sends `destination_shown` two frame callbacks after the destination chip is on screen, and protocol v1 forbids the core to send any byte to a cloud destination before that (D-093).

**The trace** (PRD §10.5): one JSON line per query, trace schema 2, with 15 timestamps from key-down to the Tier 2 paint, every paint and whether its scene matched the final parse, and the machine's idle, thermal, power and display state. It never holds transcript text, and tests in Rust and Python walk the JSON to prove it. One pinned example inside a Rust test is the Python scorer's fixture, so a test fails if writer and reader drift apart (D-095).

**Replay.** The harness (PRD §10.6) will replay recordings through the live audio path, swapping only sample values inside the live `cpal` callback so that no stage is mocked, over a fixed script of 130 utterances (30 development, 100 test). The expected answers are worked out from PRD §7.4 and §7.5 by code that shares nothing with the parser; that code was written after the parser's source had been read, and [bench](bench/README.md) lists the caveats. Built so far (M1-09) is the scorer ([bench/replay](bench/replay/README.md)), which re-parses each transcript with the real parser and bootstraps every figure. Its wrong-paint gate, as the lead reads PRD §14 (the owner may overrule it), is the one-sided 95 % Wilson upper bound, a confidence bound for a proportion, over the trials that painted from a partial; with no wrong paint it falls under 2 % at 133 such trials (D-096). The pixel tag carries no scene index, so two Tier 0 composites in one query cannot be told apart, an open question (D-096). The harness itself is TODO(M1), and the M1 platform spike ([spikes/m1-platform](spikes/m1-platform/README.md)), with its 11 checks fixed in code before any run (D-033), has not run.

**Implementation:** `app/core/src/lifecycle.rs` and `trace.rs`; `app/src/overlay/gate.ts`, `scheduler.ts` and `controller.ts`; the stress tests in `app/core/tests/keymash.rs` and `app/src/harness/`; the scorer in `bench/replay/score.py`.

## Interfaces and file formats

The contracts between the parts. Each built contract is defined once, in the file named, and checked by tests.

| Contract | Defined in | Shape | State |
|---|---|---|---|
| Query JSON | `app/core/src/query.rs` | PRD §7.3's ten fields, then `place_source`, `place_match` {`stage`, `score`}, `horizon` and `flags` (D-040); a parse error prints `{"error": …}` and exits 2 | built |
| Protocol v1, core to overlay | `app/src/overlay/protocol.ts`, which generates `app/src/protocol.schema.json` | every message carries `v: 1`. 17 types inbound: `query_started`, `partial`, `audio_level`, `released`, `parsed`, `scene_changed`, `tier_started`, `tier_frame_ready`, `tier_rejected`, `no_match`, `out_of_range`, `cancelled`, `error`, `settings`, `destination`, `network`, `prefetch`. 10 outbound: `ready`, `paint`, `destination_shown`, `dismiss`, `scrub`, `flip_meridiem`, `copy`, `use_offer`, `open_url`, `protocol_error`. A tier's label is bound to its tier, and frame URLs must be app-local (`snowglobe://localhost/` or `asset://localhost/`) | built; the shell that speaks it is not (D-093) |
| Speech C ABI | `app/speech-macos/include/snow_globe_speech.h` | ten `sg_speech_*` functions; events READY, PARTIAL, STABLE (always a prefix of the current PARTIAL), FINAL and ERROR; at most one terminal event, and an ERROR if none arrives within 15 s of `sg_speech_finish` | built; never run on the recognizer (D-055) |
| SceneSpec | `renderer/scene.schema.json` and `renderer/src/scene.ts` | the camera, valid time and run start, Sun and Moon, cloud layers, ceiling, visibility, wind, haze and the plate's hashes; `scene_hash` is the SHA-256 of its canonical JSON | built |
| Forecast run file | `workers/forecast/src/snow_globe_forecast/schema/forecast_run.schema.json` | `kind` `snow-globe.forecast-run`, `schema_version` 1; per hour the valid time, both S3 stamps, the availability, the lag and the byte ranges; the sample points (camera cell, ray points, landmarks, the 5 × 5 block) and their values per field | built |
| Frame sidecar | `workers/capture/src/snow_globe_capture/live.py` | the image time and its candidates, validators, sizes, SHA-256 and dHash (a perceptual hash), luma, chroma and sharpness, the Sun, flags, the frame policy, the thumbnail, the User-Agent and capture's version | built |
| Trace | `app/core/src/trace.rs`; the pinned example in `app/core/tests/trace_fields.rs` | trace schema 2: one JSON line per query, 15 marks, every paint with `matches_final`, the machine's state, no transcript text (D-095) | built; nothing writes it until the shell exists |
| `snow-globe-ask` output | `app/core/src/tier0/ask.rs` | one JSON object: the query, the frame's path, the caption, the distance and what it matched on, the alternatives and the stage timings; exit 0 a frame, 2 a parse error, 3 no frame qualifies, 4 the library is unavailable, 5 none of the closest 5 reads | built |
| HTTP API | PRD §11.1 | `GET /v1/viewpoints`; manifests and forecast files by signed URL; `GET /v1/live/{viewpoint}`; `POST /v1/refine`; `POST /v1/predictions` | not built (M2) |

A change to `protocol.ts` fails its test until `node scripts/schema.ts` regenerates the schema, and `pnpm build` stops on a stale schema ([app/src](app/src/README.md)).

## Evaluation design

### P1, the primary claim

PRD §13.4 is the pre-registration, to be frozen with its commit hash by Mon 14 Dec 2026. A daytime frame (the Sun at 3° or more) is "visible" when the summit band's edge ratio ([below](#the-extractor)) is at least τ and "hidden" below a lower threshold; "partial", in between, is excluded and reported. The prediction is the same extractor run on the rendered frame. The statistic is ΔPSS, the paired difference in PSS against persistence, with a 95 % interval from a block bootstrap, which resamples whole days to keep the weather's day-to-day correlation; the claim is made only if the interval excludes 0. The minimum detectable effect (MDE) is published before sealing, and above 0.25 P1 is reported as underpowered (D-019). Everything fitted is committed with its hash by 14 Dec, before any held-out frame is scored, labelled or viewed: τ, the S_mos baseline (a logistic regression on HRRR fields, [below](#scores-and-baselines)), the weights of Tier 0's analog match, the block length and the MDE.

### Scores and baselines

From hits H, misses M, false alarms F and correct negatives C (PRD §13.5):

```text
POD  = H / (H + M)        the hit rate (probability of detection)
POFD = F / (F + C)        the false-alarm rate (probability of false detection)
PSS  = POD − POFD         the Peirce skill score: 0 for no skill, 1 for perfect
FAR  = F / (H + F)        the false-alarm ratio
CSI  = H / (H + M + F)    the critical success index
skill score = 1 − E_model / E_ref
```

PSS is used because CSI ignores correct negatives and moves with the base rate. Exclusions apply identically to the model and every baseline. The paired ΔPSS ([eval](eval/README.md)) is a circular moving-block bootstrap over local calendar days (an outage day is an empty day), with the same indices for model and reference, 10,000 resamples and a required seed; its p-value is defined so that p < α exactly when the interval excludes 0.

| Baseline (PRD §13.6) | What it predicts | Built |
|---|---|---|
| Persistence | the stronger, on development cases, of (a) the latest daytime frame at the issue time and (b) the frame at valid − 24·k h, k = ceil(lead / 24 h) (D-036) | yes |
| Climatology | the visible frequency for that month and hour | yes |
| S_rule | "visible" if HRRR's ceiling where the summit ray crosses it is above the summit, and low plus mid cover is under 50 % | yes |
| S_mos; camera-cell sampling (R2) | a logistic regression on HRRR fields; the forecast at the camera's own cell | no |
| Tier 0's analog (R3) | the nearest earlier frame of the same class. Today eval picks it by a placeholder distance, √((1.0 Δaltitude)² + (0.25 Δazimuth)²) within 5°, else by the Sun alone and flagged, which is not Tier 0's distance; the two become one at the pre-registration (D-097, TODO(M2)) | placeholder weights |

### Designs H and P, and the held-out data

- **Design H, hindcast:** archived frames with archived HRRR runs at 1–48 h leads, inside one camera epoch, and only from HRRR version 4 (HRRRv4), which began in December 2020. Summer 2026, 1 Jun to 31 Aug, is held out at Point Reyes and the five H cameras.
- **Design P, sealed prospective:** live frames with live forecast files at 1, 3, 6, 12, 24 and 48 h over the sealed window, at least 28 days, extended by any outage; Rainier can only be design P (D-019). A case is one daytime valid time at one viewpoint and one lead. The effective n is reported beside every count: about 28 days × 2 viewpoints = 56 day-blocks over the two viewpoints of the MSV, the Minimum Shippable Version (PRD §13.7, §4.1). P1 is Rainier alone, so its blocks are the window's 31 local days, more if an outage extends the window (D-019). The [camera table](#the-eight-cameras) gives each viewpoint's design (D-024).
- **Held out from development:** the sealed window, live or archived, and every archived frame dated 1 Jun 2026 or later by its local or UTC date (D-070); frames under `quarantine/` are never opened (D-022). The split is a committed, versioned file (D-085).
- **Leakage guards:** in `FrameStore` a view at issue time t sees only frames taken and available by t, a fit never sees a held-out frame, and only `SealedScoring(<commit>)`, in M4, opens one. Tests flip every label after the issue time to show that no score moves.

### The extractor

P1's truth comes from the frame, and one extractor serves real, rendered and baseline frames (PRD §13.3). It follows the FAA and MIT Lincoln Laboratory's VEIA method (Visibility Estimation through Image Analytics): edges a clear day shows fade when cloud, fog or haze lie between the camera and a landmark. It takes the Sobel gradient magnitude of the frame's luma over the local mean luma, so an exposure change cancels. The **edge ratio** is a band's mean energy over the same band's in a clear-day reference: visible at 0.60 or more, hidden below 0.35, partial between. These are v−1's placeholder thresholds, fixed before any v0 ratio existed; P1's τ and partial band are to be fitted on development data and frozen on 14 Dec (PRD §13.4), and thresholds given on the command line mark a run so that it can never meet the bar. Before any band is scored, a frame may be set apart as night (the Sun below 3°), camera fault (dark, saturated or blank), excluded by a frame gate, no reference, or camera in cloud, and each count is reported. Baselines read the same classes: visible is yes, hidden or in cloud is no, and partial or camera fault gives no forecast.

Extractor v0's reference is the three labelled clear-day frames nearest in Sun position, never from the scored frame's own day. Extractor v0.2 keeps v0's energy and conditions the reference on the light: it divides the summit's energy by the contrast that clear air would give lit snow against this frame's sky, takes the three frames nearest in that light and in Sun altitude on other days, and fits τ rather than fixing it ([eval](eval/README.md)).

**Validation bars** (PRD §13.8): landmark F1 (2H/(2H + M + F)) at least 0.85 and cloud-fraction mean absolute error at most 0.10, on about 150 labelled real frames per metric, about 50 labelled renders and 50 frames re-labelled a week later. **A metric whose extractor fails its bar is not reported, and this README says so.** Extractor v0 failed on the summit band; v0.2 meets the F1 bar on its first check but not yet the full validation ([results](#plates-and-the-extractor)).

**Implementation:** `eval/src/snow_globe_eval/` (`extractor_v0.py`, `extractor_v0_2.py`, `scores.py`, `baselines.py`, `leakage.py`, `heldout.py`, `p1dev.py`).

### Renderer claims, secondaries and Monte Carlo checks

- **R1:** rendering loses no skill: PSS(Snow Globe) ≥ PSS(S) − 0.05 against S_rule and S_mos. **R2:** sampling along each view ray beats the camera's own cell at predicting the obscuration height, the highest visible band. **R3:** the shader against Tier 0's analog; if the analog is non-inferior everywhere, this README will say the shader buys explainability and coverage, not accuracy. R1–R3 are pre-registered with P1 (PRD §13.4).
- **K1 and K2:** cloud-fraction skill, and a 24 h low-visibility event at the nearest named landmark, reported with Holm's correction for multiple tests, not claimed. At Point Reyes the PRD expects weak 24 h fog skill and frequent false alarms, mostly HRRR's rather than the renderer's; for one, HRRR's marine-layer tops run about 150 m low (PRD §13.11).
- **Monte Carlo checks:** for the statistic, 200 simulated trials of 30 independent days with a known ΔPSS must give 95 % intervals that cover it in 89–99 % of trials; correlated days and blocks longer than a day are not in this check. For the clouds (PRD §9.5), at least 1,000 seeds × cover 0.1–0.9 must land the drawn cloud fraction within 5 points of the view-ray target, with negative controls that must miss.

## Headline numbers

The targets are PRD §14's, and they don't move after measuring; a missed target is reported with its measured value and the bottleneck. **No row is measured yet under PRD §14's definition**, so every Measured cell is blank; partial and development measurements, the recorded misses among them, follow in [Measured results so far](#measured-results-so-far). PSS, the Peirce skill score, is the hit rate minus the probability of false detection (POD − POFD, [above](#scores-and-baselines)); persistence is the "no change" forecast; the MSV centres on Tiers 0 and 1 on Rainier and Point Reyes and a scored P1 (PRD §4.1).

| Metric (PRD §14) | Target | Measured | Measured by | Due |
|---|---|---|---|---|
| Tier 0 paint, p95, idle | ≤ 300 ms (warm and cold reported beside it) | `___` ms | Replay + paint capture | M1 |
| Tier 1 paint, p95, idle | ≤ 1 s; stretch ≤ 400 ms | `___` ms | Replay + paint capture | M2 |
| Tier 2 within 3 s | ≥ 80 % of queries; p95 reported | `___` % | Replay + paint capture | M3 |
| First partial transcript, p50 | ≤ 300 ms after speech onset | `___` ms | Replay (onset known from the fixture) | M1 |
| Key release → final transcript, p95, idle | ≤ 240 ms (an allocation) | `___` ms | Replay | M1 |
| Exact place and time, replay test set | ≥ 95 % | `___` % | Bench | M1 |
| Parser fallback rate | ≤ 10 % (Tier 0 survives only below ~5 %) | `___` % | Bench | M1 |
| Wrong paints from partial transcripts | ≤ 2 % (Wilson upper bound), or partial paint disabled | `___` % | Replay | M1 |
| Stale paints | 0 in ≥ 10,000 cycles | `___` | Stress test | M1 |
| Tier 1 frame time | ≤ 8 ms at 1080p steady state; first frame ≤ 50 ms of GPU time | `___` ms | Renderer bench | M2 |
| Sun and Moon position | ≤ 0.05° and ≤ 0.3°; sunrise and sunset ±1 min | `___` | Ephemeris tests | M2 |
| Pose | ≤ 1° | `___`° | Plate builder | M2 |
| Rendered vs target cloud fraction | within 5 points | `___` points | Monte Carlo test | M1.5 |
| P1: ΔPSS vs persistence, 24 h | claimed if the 95 % CI excludes 0 (a hypothesis, not a gate) | `___` (CI `___` to `___`) | Sealed scoring | M4 |
| R1: non-inferiority vs S_rule and S_mos | margin 0.05 | `___` | Sealed scoring | M4 |
| K1, K2: cloud-fraction skill; low-visibility PSS, 24 h | reported, Holm-corrected | `___` | Sealed scoring | M4 |
| Tier 2 false accepts | ≤ 1.5 % (0 of ≥ 200) | `___` | False-accept suite | M3 |
| Tier 2 guardrail pass rate | ≥ 90 %, with thresholds frozen first | `___` % | Evaluation sample | M3 |
| Tier 2 preference | ≥ 70 % of ≥ 50 pairs, rated by someone else | `___` % | Blind test | M3 |
| Extractor | landmark F1 ≥ 0.85; cloud-fraction MAE ≤ 0.10 | `___` | Labelled frames | M4 |
| Verified viewpoints | 2 for the MSV; 8 targeted for v1 | `___` | Manifest | M4, v1 |
| Scored pairs | ≥ 5,000 hindcast pairs where the archive allows; live pairs with their effective n | `___` | Prediction log | M4 |
| Capture and worker uptime | reported; outages extend the window | `___` | Logs | M4 |

## Measured results so far

On the MacBook Air M4 (macOS 26.5.1). None of this fills a headline blank.

### Data collection and forecasts

| What | Value | Conditions, date | Source |
|---|---|---|---|
| M0.5 gate 1: ≥ 72 h of logs | **met at 23:46:10.754 UTC on Sun 22 Nov**: 72.01 h covered of 83.37 h elapsed, 11.36 h lost; 85 forecast runs per viewpoint, none missed | from 12:24:23 UTC on 19 Nov, by the gate rule below; every capture outage was the Mac asleep | [docs/evidence/m05-gate.md](docs/evidence/m05-gate.md), D-089 |
| Politeness at nps.gov | least poll gap 299.001–300.002 s by camera, with 10 of 6,606 pairs not provably 300 s apart, all before capture 0.2.1; at most 4 image downloads in any rolling hour | the gate window, 19 Nov 12:24 to 22 Nov 23:46, from the log's stamps | [docs/evidence/m05-gate.md](docs/evidence/m05-gate.md), D-089 |
| Archive spacing | 1,000 requests, least gap 5.0506 s; one pair of 20 Nov was 4.9944 s, before the limiter fix | 23 Nov's daily budget, capture 0.3.0, counted at 10:20 UTC | [PRD_PROGRESS.md](PRD_PROGRESS.md#the-nps-archive), D-077 |
| Storage under D-028 | 262.0 MB a day projected for 8 cameras, against 451.9 MB for every frame | from 105 frames' sizes at 96 downloads a camera a day, 19 Nov | [workers/capture/README.md](workers/capture/README.md) |
| HRRR publication lag, median / p95 | f00 51.36 / 60.52 min; f18 85.24 / 98.15 min; f48 107.57 / 116.65 min (n = 33, 4 of them logged live), so f48's longest lead is about 46.1 h | the survey of 194 runs from S3 listings, no GRIB, 20 Nov | [PRD_PROGRESS.md](PRD_PROGRESS.md#forecast), D-053, D-102 |
| Hindcast for P1's 24 h lead | 105 daytime Rainier frames of 19–22 Nov paired with 8 synoptic runs (43 forecast hours); actual leads 24.01–29.94 h; 441.9 MB of GRIB in 137 range requests, 0 retries, 0 errors | 23 Nov, 10:28 UTC, with the corrected manifest; availability from the S3 stamps alone | [workers/forecast/README.md](workers/forecast/README.md) |

The gate rule (D-069), the stricter of two readings of PRD §16: covered time is the elapsed time less the lost time, and lost time is the union of both jobs' outages plus each capture restart's excess. A capture gap of more than 540 s between passes loses the gap less 300 s; a forecast run written more than 900 s after it fell due counts as an outage from due + 300 s to its write. `make data-report` recomputes it, read-only.

### Query understanding and the ephemeris

| What | Value | Conditions, date | Source |
|---|---|---|---|
| Parser table | 295 cases in 16 categories pass (PRD §7.6 asks for 150); 19 of 19 mutants of the new rules caught | an injected clock; passing in `make test` on wave 1's tree, 23 Nov (D-092) | [PRD_PROGRESS.md](PRD_PROGRESS.md#snow-globe-core), D-039 |
| Parse latency | p95 123.7–126.3 µs, against a 5 ms budget | 3 × 10,000 parses, release build, recorded 20 Nov | [PRD_PROGRESS.md](PRD_PROGRESS.md#snow-globe-core) |
| The parser on the replay script's text | 94 of 100 test rows exact | text only, 05:40 UTC on 20 Nov, the same on re-runs; PRD §14's metric needs replay | [PRD_PROGRESS.md](PRD_PROGRESS.md#m1-preparation), D-057 |
| Solar events against an independent NOAA implementation | sunrise within 3.886 s, sunset within 2.832 s; altitude within 0.0128° | 25 dates × 8 viewpoints; passing in `make test`, 23 Nov | [PRD_PROGRESS.md](PRD_PROGRESS.md#snow-globe-core) |
| The Moon against a low-precision almanac | max 0.355° | the reference is itself good to about 0.3°, so this checks the wrapper, not PRD §14's 0.3°; passing in `make test`, 23 Nov | [PRD_PROGRESS.md](PRD_PROGRESS.md#snow-globe-core) |
| oculus-core against JPL Horizons and Skyfield (`ephem-validate`) | Sun: geometric worst 12.7″ against Horizons over 70,080 epochs, 30.7″ against Skyfield over 1,000 samples; refracted p99 12.6″, with **one epoch beyond the 0.05° target**: Yosemite, 13 May 2027 03:00Z, 0.647°, where a 1.6″ difference puts the two geometric altitudes either side of the −1° refraction cutoff, so the command exits 1. Moon: worst 9.6″ (Horizons) and 19.1″ (Skyfield). Sunrise and sunset within 3.21 s of NOAA over 5,840 events | 23 Nov, offline from the cache; Horizons hourly at the 8 viewpoints, 1 Jul 2026 to 30 Jun 2027; Skyfield samples from 1998 to 2030; the cutoff convention is open. It supersedes D-002's worst cases of 18 Nov (0.19′ Sun, 0.13′ Moon; 294 rows, 3 sites), now 0.212′ and 0.161′ | [eval/README.md](eval/README.md), D-002; Horizons is cited under [Credits](#credits-and-licences) |

### Tier 0 and the overlay

| What | Value | Conditions, date | Source |
|---|---|---|---|
| Tier 0 library and match | 7,047 sidecars → 5,952 entries (0 held out, since no held-out frame is on disk yet); match p95 52.8–56.6 µs; in 200 random asks, 199 chose an independently written oracle's frame, and the 200th was rightly "no close analog" | 04:36–04:41 UTC on 23 Nov, release build, on AC, under a load of 4.3–11.6 from other work, so noisy | [PRD_PROGRESS.md](PRD_PROGRESS.md#snow-globe-core), D-097 |
| Key-up core path: parse, match, lookup | p95 249–331 µs | the same runs; the 300 ms paint needs the shell | [PRD_PROGRESS.md](PRD_PROGRESS.md#snow-globe-core), D-097 |
| `snow-globe-ask`, end to end | 57–115 ms, the index load included | the same runs | [PRD_PROGRESS.md](PRD_PROGRESS.md#snow-globe-core), D-097 |
| Stale paints, the web layer alone | 0 in each of 3 runs of 10,240 cycles, all 10 broken overlays caught; the Rust model, 0 in 40,000 | 3 verifier runs in headless Chromium at 04:42 UTC on 23 Nov, with a simulated core and no app shell; staleness judged at the draw, and in one instrumented run 45 of 806 cross-fades reached 50 % after a scrub had selected another scene, which PRD §10.4's paint capture would count (open, D-094); PRD §14's row needs the real panel (M1-13) | [PRD_PROGRESS.md](PRD_PROGRESS.md#overlay-appsrc), D-093 |
| Cross-fade to 50 % | **66.7–83.3 ms against the 60 ms allocation: missed** | page timestamps in the same 3 runs, not the composited paint PRD §10.4 measures; a 120 ms linear fade at 60 Hz. Bottleneck: the Web Animations clock starts at the commit frame or one later. Kept linear, not tuned; the curve is open | [app/src/README.md](app/src/README.md), D-094 |

### Renderer 0.3.0

| What | Value | Conditions, date | Source |
|---|---|---|---|
| Frame time at 1080p | heavy scene: p50 6.7–7.0 ms, **p95 8.3–8.7 ms, over 8 ms**; clear scene: p50 3.2–3.3 ms, **p95 6.8–9.4 ms, over 8 ms in 3 of 6 alternations**; earlier saved runs: heavy **p50 12.2 and 13.8 ms in 2 of 3, over 8 ms** | Playwright's headless Chromium shell on the M4's GPU through ANGLE (Chromium's layer that maps WebGL onto Metal), 6 alternations of 100 frames at a load of 3.1–4.1, 23 Nov. The heavy scene has three cloud slabs, fog, haze and Sun shadows on the synthetic plate. The p50 can double between sessions, and the 8 ms target is defined for WKWebView, the shell's web view, and measured in M2. Bottleneck, heavy scene: cloud-mask sampling in the main pass, 7.9–8.0 ms of its 11.2–11.5 ms in a per-pass profile. The clear scene has no cloud slabs, and no committed record names a cause for its p95 overruns | [renderer/README.md](renderer/README.md), D-098 |
| The Hillaire atmosphere's cost | +0.6–1.0 ms a frame at 720p, +1.1–1.7 ms at 1080p | the same runs | [renderer/README.md](renderer/README.md), D-098 |
| First frame | 478 ms on the first launch after a shader change (compile), then 54–74 ms of page time on later launches; GPU time not measured, so the ≤ 50 ms target is not shown met | the same runs | [renderer/README.md](renderer/README.md), D-098 |
| Sky accuracy | sky view within 1.30 % of an independent integral with the Sun at 0–45°, and 3.23 % at −12° to 0°; aerial-perspective lookups between slices **off by up to 36.6 %** at a 0° Sun inside the haze, at most 2 levels on the goldens and 6 at sunset (TODO(M2)) | the CPU twin, cameras at 150–2,150 m, hazes of 0.01–0.3 per km, 23 Nov | [renderer/README.md](renderer/README.md), D-098 |
| Cloud Monte Carlo | within 1.54 points of the view-ray target (target 5); negative controls miss by 7.8–93.6 points | 1,000 seeds, 4 geometries, 23 Nov; carried over from 0.2.0's byte-identical test code, and in progress because its drawn path is not tied ray by ray to the GPU-checked opacity (M1.5-05) | [renderer/README.md](renderer/README.md), D-098 |

### Plates and the extractor

| What | Value | Conditions, date | Source |
|---|---|---|---|
| Rainier plate v0.1 pose | heading 2.137°, pitch 9.994°, roll 1.643°, field of view 68.56°, from 37 stars and 3 landmarks; the solved camera 457 m north-east of the manifest's earlier, assumed position, which the manifest now replaces with it | built at 04:35:27 UTC on 23 Nov; a verifier's rebuild matched every PNG byte for byte | [plates/README.md](plates/README.md), D-099 |
| Band skyline check, p90, bar 1.0° | summit 0.41°, upper 0.37°, mid 0.16°; **near 1.22°: missed, band not valid** | plate v0.1, 23 Nov. Bottleneck: no single pinhole camera fits the stars, the far landmarks and the near ridge | [plates/README.md](plates/README.md), D-099 |
| Extractor v0, summit band | **F1 0.50 on 28 judged frames, against 0.85: missed**; 2 hits, 4 misses, 0 false alarms, 22 correct negatives | 23 Nov, plate v0.1, the 18 reference-pool frames left out; one labeller, the AI agent that built the extractor, with no second labeller, re-label pass or labelled renders. Bottleneck: its clear-day references show a sunlit skyline against blue sky, so bright-overcast summits read partial or hidden; 54 of the 87 judged frames had no reference | [PRD_PROGRESS.md](PRD_PROGRESS.md#plates-and-extractor-v0), D-100 |
| Extractor v0.2, summit band | pre-registered leave-one-day-out check: **F1 1.000** (30 hits, 0 misses, 0 false alarms, 68 correct negatives) on 98 judged frames over 4 days, meeting the 0.85 bar on these labels; upper and mid also 1.000; v0 as built scores 0.571 on the same labels and folds | 23 Nov, plate v0.1; labels from two blind AI labellers of one model family (summit agreement 104 of 105, Cohen's kappa 0.983). Not publishable under PRD §13.8: 98 judged frames, not about 150, with 29 of the 30 visible summits on 20–21 Nov; no re-label pass; no labelled renders. The decisive folds rest on one reference frame per band, the method was designed after viewing v0's energies on these frames, and it flags 1 of 7 in-cloud frames. A prospective check on frames from 23 Nov on is TODO(M1.5) | [eval/README.md](eval/README.md) |
| Backtest pair v−1 | Grand Canyon, a 24 h lead: truth 0.330 (hidden), render 0.504 (partial), a correct negative by accident. With the camera's measured 5 % re-aim applied, the truth box reads 0.360, across the 0.35 threshold, so the cell shows nothing; 2.61 s a pair on SwiftShader | renderer 0.2.0, 19–20 Nov; counts toward no metric | [docs/evidence/m05-backtest-pair.md](docs/evidence/m05-backtest-pair.md) |
| Development P1 table (`p1dev`) | runs end to end on the 105 daytime frames of 19–22 Nov, 103 of them with a 24 h hindcast and a render; **no score is reported**, because it uses extractor v0, which fails its bar | 23 Nov, renderer 0.3.0 on SwiftShader, plate v0.1; provisional until an extractor that passes is wired in (PRD §13.8) | [eval/README.md](eval/README.md) |
| ΔPSS interval coverage | 196 of 200 trials | nominal 95 %, 30 independent days, 19 Nov | [eval/README.md](eval/README.md) |

Because extractor v0 failed its bar, it cannot score P1, and nothing it would score is reported (PRD §13.8). Extractor v0.2 meets the F1 bar on its first, pre-registered check, but not yet PRD §13.8's full validation, so a meaningful development P1 table and the τ the freeze needs still wait for an extractor that passes; which bar M1.5's table needs is open (D-100).

## Quick start

**The app** targets macOS 26.5 on Apple silicon, and the platform stays macOS 26.5.x through milestone M4 (D-012). **The tests** also run on Linux: CI runs every suite but `speech` on Ubuntu 24.04 (D-063, D-092). You need uv with Python 3.13, Node 24 with pnpm, Rust 1.98 and Terraform 1.16 (CI pins uv 0.11.6, pnpm 12.15.0, Rust 1.98.1 and Terraform 1.16.1); eval's first sync also builds oculus-core from git with Rust. The `speech` suite needs Xcode 26.2 with Swift 6.2, and skips without them. Clone the repository, then:

```sh
make help                                          # every target, one line each
make test                                          # every suite, then a summary
make test-rust                                     # one suite (test-common, test-eval, ...)
make docs-check                                    # invariants of the planning docs
```

- **`make test`** needs no microphone, credentials or paid service, and never contacts NPS, NOAA or AWS. Its first run downloads dependencies (PyPI, crates.io, the GitHub pin of oculus-core, npm, Playwright's Chromium, the Terraform registry); then every suite runs offline. On the M4 it took 652 s on wave 1's tree (D-092). `make test TEST_SUITES='<names>'` runs a subset of `common capture forecast eval plates renderer overlay rust speech infra docs bench`.
- **Data goes outside the repository,** under `$SNOW_GLOBE_DATA_DIR`, by default `~/snow-globe-data` ([Data layout](#data-layout)).
- **In an iCloud-synced folder** iCloud hides the virtual environments' `.pth` files, which Python 3.13 skips, so imports fail outside pytest (D-031). The Makefile works around it. By hand, prefix commands with `PYTHONPATH=src:../common/src` in `workers/capture` and `workers/forecast`, `PYTHONPATH=src:../workers/common/src` in `eval` and `PYTHONPATH=src:../workers/common/src:../eval/src` in `plates`. Keep Rust's build output out of the repository too: the Makefile exports `CARGO_TARGET_DIR` as the resolved data dir's `cache/cargo-target`, and for cargo by hand, set `SNOW_GLOBE_DATA_DIR` first and build with `CARGO_TARGET_DIR="$SNOW_GLOBE_DATA_DIR/cache/cargo-target"`.

Each package on its own ([CLAUDE.md](CLAUDE.md), "Commands"):

```sh
cd <project> && uv sync && uv run pytest -q && uv run ruff check .      # Python, per project
cd app && cargo test --workspace                                         # Rust
cd renderer && pnpm install && pnpm test                                 # the renderer
cd app/src && pnpm install && pnpm test                                  # the overlay
terraform -chdir=infra/terraform init -backend=false && terraform -chdir=infra/terraform validate
```

**A first look with no camera or data:** the renderer's `pnpm test` renders the three synthetic goldens headless and checks them pixel for pixel on darwin/arm64 (SSIM ≥ 0.98 elsewhere). The overlay's preview writes every overlay state into one page that opens from disk, 24 panels with sample captions and no camera frame ([app/src](app/src/README.md)):

```sh
cd app/src
pnpm install --frozen-lockfile --prefer-offline
pnpm preview            # writes app/dist/preview.html: every state with sample captions
```

The replay scorer runs on made-up data exactly as [bench/replay](bench/replay/README.md) gives it. It needs a release build of `snow-globe-parse`, which `make bench-script` makes offline once `make test` (or `make test-rust`) has filled cargo's cache. It also needs Python 3.11 or later, because both scripts import `datetime.UTC`: macOS's `/usr/bin/python3` is 3.9, so use a 3.13 interpreter, such as the one uv manages for workers/common, as `make test-bench` does (D-075).

```sh
python3 -I -B bench/replay/synthetic.py --out /tmp/replay-synthetic
python3 -I -B bench/replay/score.py --traces /tmp/replay-synthetic/traces.jsonl \
    --transcripts /tmp/replay-synthetic/transcripts.jsonl \
    --fixtures /tmp/replay-synthetic/fixtures --run /tmp/replay-synthetic/run.json \
    --out /tmp/replay-synthetic/report
```

The report opens with a SYNTHETIC FIXTURE banner, and a synthetic report is never evidence. `snow-globe-parse` prints the query JSON for any sentence, offline. `snow-globe-ask` adds the closest real photo, but it needs captured frames in the data dir: without them it exits 3 (no frame qualifies) or 4 (no library), never a guess ([Command reference](#command-reference)).

## Running the collectors

These targets run the collectors or read what they wrote. Those marked NETWORK contact nps.gov or NOAA's S3 bucket, and none is part of `make test`. The code enforces the [polite-collection rules](#polite-collection-enforced-in-code) whatever the caller asks, and every NPS command refuses with exit 3 while another fetcher runs. That lock sees only its own machine, and the project already runs the one NPS fetcher the rules allow, so please don't run the NPS commands from a clone; a copy that does must set `SNOW_GLOBE_USER_AGENT`, so that nps.gov can tell it apart from this project. Until the AWS jobs are live, the local forecast stand-in is the only writer of forecast files, so the forecast targets refuse with error 5 while it runs (D-030). Capture stores live frames for scoring, the sealed window's included, and refuses archived frames dated 1 Jun 2026 or later; eval, Tier 0 and the plate builder refuse, for development, every sealed-window frame and every archived frame from that date.

| Make target | Network | What it does |
|---|---|---|
| `make capture-status`, `make forecast-status` | none | persisted capture state; forecast files, the lag model, the cache |
| `make capture-once`, `make capture-loop` | NETWORK (nps.gov) | one polite pass; a pass every 300 s plus jitter until Ctrl-C |
| `make archive-check` | NETWORK (nps.gov archive) | the M0.5 archive check, one request at a time at least 5 s apart |
| `make forecast-latest`, `make forecast-tick` | NETWORK (NOAA S3) | the newest complete run in the light profile; one call of the Lambda handler |
| `make backtest BACKTEST_ARGS=…` | NETWORK (NOAA) with `--forecast auto` | one v−1 backtest pair, rendered to the data dir |
| `make data-report`, `make ephem-check` | none, read-only | the 72 h gate's evidence; oculus-core against NPS's Sun angles |

## Command reference

Python console scripts run through uv from their package folder, with the `PYTHONPATH` above on an iCloud-synced checkout; the Rust ones are binaries of `snow-globe-core`. The package READMEs, or a binary's usage text, list every flag.

| Command | Network | What it does |
|---|---|---|
| `snow-globe-capture once`, `loop [--interval 300] [--archive-queue PATH]` | NETWORK (nps.gov) | one pass; the loop with the archive backfill inside it |
| `snow-globe-capture archive days\|day\|sample …`, `archive-check` | NETWORK (nps.gov archive) | archive metadata and frames; the M0.5 check |
| `snow-globe-capture status`, `cadence <viewpoint_id>`, `storage`, `archive queue add\|list\|remove\|resume` | none | state, cadence, bytes per day, the backfill queue |
| `snow-globe-forecast latest`, `run YYYYMMDDHH`, `backfill`, `tick`, `lag-survey` | NETWORK (NOAA S3) | extract runs; one handler call; survey publication lags |
| `snow-globe-forecast hindcast plan\|fetch\|compare\|leads\|status` | NETWORK (NOAA S3), except `status` | P1's 24 h-lead hindcast, written only under `forecast/hindcast/` |
| `snow-globe-eval extractor-v0`, `extractor-v0.2 --run-id ID`, `p1dev [--run-id ID]`, `heldout [--check FRAME]`, `data-report` | none | the extractors and their checks; the dev-only P1 table; the held-out split; the gate report |
| `snow-globe-eval ephem-validate` | NETWORK the first time (JPL Horizons, the DE421 file, PyPI for Skyfield), then cached; `--offline` re-runs from the cache | the ephemeris validation |
| `snow-globe-eval backtest-pair --help` | NETWORK (NOAA) only with `--forecast auto` | one v−1 pair: SceneSpec → headless render → extractor |
| `snow-globe-plates match-stars`, `build --spec specs/rainier.paradise-mountain.v0.1.json [--dry-run]`, `candidates` | none | stars, a plate set, candidate base frames |
| `snow-globe-parse "<utterance>" [--now <RFC3339>] [--machine-tz <IANA>] [--context <place_id\|viewpoint_id>] [--manifests <dir>] [--pretty]` | none | the query JSON; exit 2 on a parse error |
| `snow-globe-ask "<utterance>"` with the same flags and `[--data-dir <dir>] [--no-cache] [--alternatives <n>] [--open]`; `snow-globe-tier0 index\|bench` | none | the closest real photo as JSON; the Tier 0 library report and benchmark |
| `pnpm render --scene s.json --plate p.jpg [--sky-mask m.png] [--depth d.png] --out out.png`, `pnpm render:batch --jobs jobs.jsonl` (renderer) | none | one frame and its metadata, refusing an output inside the repository; many scenes through one headless runner |
| `pnpm bench`, `pnpm montecarlo`, `pnpm goldens:update` (renderer); `pnpm stress`, `pnpm preview` (app/src) | none | frame times; the coverage table; the synthetic plate and goldens, for review before committing; the key-mash run; every overlay state |
| `node scripts/schema.ts` (app/src) | none | rewrites `protocol.schema.json` after a change to `overlay/protocol.ts` |
| `infra/deploy.sh` | AWS: creates resources and costs money | the owner's deploy; it has never run |

## Configuration reference

Every `SNOW_GLOBE_*` variable also reads its name from before the rename.

| Variable | Default | What it sets |
|---|---|---|
| `SNOW_GLOBE_DATA_DIR` | resolved as in [Data layout](#data-layout) | the data root |
| `SNOW_GLOBE_ALLOW_SYNCED_DATA` | unset | `1` allows a data dir in a synced folder |
| `SNOW_GLOBE_STORAGE`, `SNOW_GLOBE_S3_BUCKET`, `SNOW_GLOBE_S3_PREFIX` | `local`; none; none | the local or S3 storage backend, and the key prefix in the bucket |
| `SNOW_GLOBE_MANIFEST_DIR` | the first `viewpoints/manifests` found walking up | the manifests folder |
| `SNOW_GLOBE_USER_AGENT` | `SnowGlobe/0.1 (webcam research capture; fetch-on-change)` | the User-Agent; by the project's rule it never names a person |
| `SNOW_GLOBE_FORECAST_FIELDS`, `_MAX_FH`, `_MAX_MB` | all 22 fields, every hour, unset (the CLI then refuses runs above 150 MB) | the forecast profile; the Makefile's light profile is 8 fields to f02, 40 MB |
| `SNOW_GLOBE_EPHEMERIS` | `oculus` | `noaa` for the independent NOAA solar algorithm |
| `SNOW_GLOBE_PARSE` | the release build in the cargo target dir | the parser binary that `check.py` and the replay scorer run |
| `SNOW_GLOBE_EVAL_RENDER`; `SNOW_GLOBE_RENDERER_DIR`, `SNOW_GLOBE_NODE` | unset; found | `1` runs eval's real-render tests; where eval finds the renderer and Node |
| `OVERLAY_BROWSER_ARGS` | unset | `swiftshader` restores the renderer's GL flags in the overlay's browser tests |
| `TEST_SUITES`, `TF_INIT`, `CARGO_TARGET_DIR` (Make) | all 12; `auto`; the data dir's `cache/cargo-target` | the suites `make test` runs; how Terraform installs its provider; where Rust builds go |

Settings in data and code: each manifest's `polite_caps` (4 images an hour, polls ≥ 300 s, fetch on change; the schema allows nothing looser), Tier 0's `MAX_DISTANCE` of 4.0, and the Terraform's $3 monthly hosting budget with its hard stop (`monthly_budget_usd`, `budget_hard_stop`), which has never been applied. PRD §17's other caps are to be enforced in code when paid calls are built (the spend guard, M3-02, not started).

## Project structure

```text
snow-globe/
├── PRD.md, PLAN.md, DECISIONS.md, PRD_PROGRESS.md   spec, plan, decisions, progress and numbers
├── THIRD_PARTY_NOTICES                              data sources, algorithms and packages, with licences
├── CLAUDE.md                                        working rules for coding agents on this repository
├── Makefile, .github/workflows/ci.yml               one entry point; CI on every push
├── app/
│   ├── core/            Rust: parser, time rules, solar, lifecycle, trace, tier0/
│   ├── src/             the overlay: protocol, gate, scheduler, caption; stress harness
│   └── speech-macos/    the Swift speech library and its symbol checks
├── renderer/            WebGL2 passes, the Hillaire sky, the CPU twin, the headless runner and batch entry, goldens/
├── workers/
│   ├── common/          data paths, manifests, storage, polite HTTP, time helpers, JSONL logs
│   ├── capture/         live capture, archive backfill and queue, fetcher lock, held-out guard
│   └── forecast/        .idx planning, ecCodes decode, sample points, availability, the run schema, hindcast/
├── eval/                extractors, scores, baselines, held-out split, leakage guard, data report,
│                        ephemeris validation, the dev-only P1 table
├── plates/              the plate builder and specs/
├── viewpoints/          schema/ and the 8 manifests
├── bench/               utterances/ (the replay script) and replay/ (the scorer)
├── spikes/m1-platform/  the M1 platform spike, never shipped
├── infra/               terraform/, lambda/ (the image), cost/ (the estimate), ops/ (the rename's switch)
└── docs/                research/, council/, review/, evidence/, checks/, m1/
```

## Testing and CI

`make test` runs 12 suites in turn and prints a summary (D-063, D-092). Every Python suite runs `uv sync --locked`, `ruff check`, `ruff format --check` and pytest with network tests deselected and every proxy pointed at a closed port, so a test that tries a real HTTP call fails at once. Counts are from the run on wave 1's tree:

| Suite | What it covers | Tests |
|---|---|---|
| `common`, `capture` | data-dir refusals, storage, the limiter, manifests; the polite schedule against a fake nps.gov that records arrival times | 315, 366 |
| `forecast` | the byte-range plan, decode checks, sample points, the lag model, on a synthetic bucket | 184 |
| `eval`, `plates` | scores, the bootstrap, baselines, split and leakage audits, extractors; pose, stars and masks on synthetic scenes | 468, 66 |
| `renderer`, `overlay` | tsc, unit tests and headless browser tests: goldens, determinism, GPU against CPU; the key-mash stress run | 220, 151 |
| `rust` | `cargo test`, clippy with warnings as errors, rustfmt | 192 |
| `speech` | `swift test` behind a trap engine, then the release build's symbol checks (macOS only) | 103 |
| `infra`, `docs`, `bench` | plan-only Terraform tests on a mocked provider; the 18 [docs invariants](docs/checks/README.md); the replay script and scorer | all checks, 102, 135 (6 skipped) |

**CI** ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs on every push and pull request: 11 jobs on ubuntu-24.04, every suite but the laptop-only `speech`, each action pinned to a full SHA, with read-only permissions and no secrets; a docs check fails if CI and `make test` ever run different suites. The rename's run passed 9 of 9 jobs (D-081). The wave 1 push's run failed two floating-point tests on Linux, fixed in `89018a6` and `58190f6`; the next run passed every job but one, lost to a stalled apt update, and `8ea7bee` bounds and retries that step.

## Infrastructure and cost

**None of it has been applied, and no AWS resource exists from it** ([infra/README.md](infra/README.md), D-014). The Terraform describes two Lambda functions from one arm64 image, capture and forecast, each with reserved concurrency 1, on EventBridge Scheduler every 5 minutes with no retries, so NPS polling can never exceed the schedule. They share one private, versioned S3 bucket, and each job's role may write only its own prefixes and delete only its own locks and staged copies, so no job can delete a frame, a forecast or a log. A $3 monthly budget emails at 33, 67 and 100 % and then hard-stops the schedules; it counts costs before credits, so credits cannot hide spend from it (D-087). `infra/test.sh` checks it all with no AWS.

**The estimate** is computed, not observed: a simulation runs the committed capture code on a fake clock against a mock S3, with nps.gov request times drawn from the live log (D-086). With the free tiers and before tax it is $2.57–4.51 in month 1, $2.90–4.86 in month 2 and $3.12–5.09 in month 3, then about $0.22 more each month as storage grows, so it straddles the $3 cap from month 1; S3 requests are the largest line. Whether the hard stop stays on through the sealed window is open (D-035).

**Development spend** is capped at $25. PRD §17 sets per-vendor hard caps totalling $24, to be enforced in code once paid-call code exists; none is written yet, and the only cap in code is the $3 hosting budget, in Terraform that has never been applied. A paid call needs the owner's go-ahead for that vendor and a key in the environment. **Spend so far: $0.00**; no paid API has been called ([DECISIONS.md](DECISIONS.md), spend ledger).

## Privacy, licensing and safety

- **Audio is to stay on the Mac by default** (PRD §15.1): the shell, not built yet, will capture it only while the hotkey is held, keep it in RAM for that query and transcribe it on device. The speech library already never opens the microphone, zeroes its buffers and writes nothing to disk, and its release build is checked against a deny list of network, download, recording and file-writing symbols.
- **Cloud speech** (PRD §15.1, D-015) is to be measured in the replay bake-off, which has not run, since no key exists yet. In the product it would be an opt-in switch, off by default. The rules: the destination chip changes before any byte leaves the machine, and an on-device-only setting turns every cloud path off. The overlay's chip and its acknowledgement are built; the cloud path and the setting are not, and the overlay still paints a Tier 2 frame that arrives under on-device only, an open item (D-093).
- **Traces** keep timings and ids, never what was said (PRD §10.5). Replay recordings will be the owner's own voice, kept outside git and iCloud; none exists yet ([bench/README.md](bench/README.md)).
- **Camera frames stay private** until the National Park Service confirms in writing (PRD §15.1). Until then the repository, this README and any demo carry code, numbers and charts only. Frames published later will be at most 1280 px, with people and vehicles blurred.
- **Collection is polite,** and held-out periods stay out of development (D-019, D-022); the code enforces both. Secrets come from the environment only. Paid-service keys are to live only on the server, and the app will hold at most a short-lived token (PRD §15.1); no key exists yet.
- **Takedown:** an issue template and a project-specific contact address are planned; the address is still to be set (`TODO(M5)`, PRD §15.5).
- **Licence:** Snow Globe's own licence is not chosen, and the repository has no LICENSE file, so **no licence is granted** until one is chosen. The Python packages' `pyproject.toml` files say "MIT", the JavaScript packages say "UNLICENSED", and the Rust crates declare none ([THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES)).

## Technical decisions and trade-offs

Each choice is in [DECISIONS.md](DECISIONS.md) with its evidence; a reversed choice gets a new entry, never an edit.

- **HRRR straight from NOAA** (D-008, D-026). Open-Meteo stops serving HRRR on Mon 30 Nov 2026 and gives no run time, cloud base or ceiling. NOAA's bucket gives exact availability times, an archive back to 2014 for hindcasts, and only the bytes needed.
- **Lead from availability** (D-027, D-054, D-102). A forecast cannot be used before it is on S3. Measuring lead from availability costs nominal hours, but no score uses a forecast before it existed.
- **One WebGL2 renderer** (D-003). A native Metal renderer would suit macOS, but the evaluation would need a second renderer, and a scored frame would not be the frame the app shows.
- **Hillaire 2020 over Hosek–Wilkie** (D-013, D-098). One model valid at every Sun angle, with no seam at sunset, for about 1.1–1.7 ms a frame at 1080p, measured in headless Chromium on the M4's GPU through ANGLE (23 Nov); WKWebView's cost is measured in M2.
- **Tauri with an in-process Swift library** (PRD §6.5). Apple's speech and Foundation Models (Apple's on-device language model) need Swift, and Foundation Models must run in the UI process, so a helper process is out.
- **A grammar first, a model only as a fallback** (PRD §7, D-005). A hand-written grammar parses in microseconds, deterministically, and never invents a place; a query that needs the fallback gives up Tier 0's budget.
- **PSS against persistence, pre-registered, in day blocks** (D-011). CSI moves with the base rate; day blocks respect the weather's autocorrelation; freezing every fitted value before the window leaves no room to tune.
- **Rainier on a sealed window** (D-019, D-024). With no archive, a prospective window is Rainier's only honest test; the archived cameras get a held-out summer instead.
- **Guards in code, quarantine not deletion** (D-022, D-070). After held-out frames were viewed by mistake on day G, every reader of frames got a guard that fails closed, and the frames moved to a folder no code opens.
- **Polls spaced from the server's answer** (D-065). Send-to-send spacing cannot prove the gap at the server, where network delays can bring two requests closer; spacing from the previous answer can.
- **Laptop stand-ins from frozen snapshots** (D-030, D-049). Repository work can never change a running job; the cost is the laptop's sleep, which caused every capture outage so far (D-089, D-104).
- **Serverless AWS with a hard stop** (D-014, D-086, D-087). A container stack with a load balancer and a cache would cost about $60 a month. Lambda, EventBridge Scheduler and S3 were chosen to fit PRD §17's $3 monthly hosting cap, but the computed estimate, $2.57–4.51 in month 1 before tax, straddles it, so the budget's hard stop, which counts costs before credits, is what holds it; whether it stays on through the sealed window is open (D-035). GitHub Actions stays CI only: its schedules jitter and lapse after 60 idle days.
- **A compatible rename** (D-080). Old variables, paths and records stay readable, and the live jobs moved only after the 72 h gate closed.

## How it compares

| | Weather app | Live webcam | Image generator | Webcam-to-forecast GAN (AIES 2022) | Snow Globe (as designed) |
|---|---|---|---|---|---|
| What you see | numbers and icons for an area | this camera, now | any picture you describe | a forecast view at 64×128 px | this camera, at the hour you ask |
| Weather from | a forecast model | the sky itself | the model's training | a forecast, through a learned mapping | NOAA's HRRR, along the camera's view rays |
| Says what is real | n/a | all real, but only now | no | n/a: a research method, with no product | every frame: real, live or simulated |
| Checked against | not your view | n/a | nothing | the forecast's own conditions | the frame the camera recorded, against persistence, pre-registered |

The skill in any Snow Globe forecast is NOAA's. Snow Globe is designed to add the view (one plate per viewpoint, so a new viewpoint needs no training), a speed target (a first real frame within 300 ms of key release, not yet measured), and a check against what the camera saw, frozen before the sealed window (PRD §2.1).

**Why not ask an image model for tomorrow's view?** Its weather would be invented and unscored. Here the sky, cloud and haze come only from HRRR and physics. Tier 2's model may add ground texture only: the sky is restored through its mask, the prompt never holds the user's words, and frozen checks reject a frame whose weather changed (PRD §2.2, §9.6). Nothing here trains an image model (PRD §4.4); what is fitted (thresholds, a logistic regression, haze profiles, analog weights) comes from development data and is frozen before the window (PRD §13.9).

## Roadmap

| When | What | Gate evidence (PRD §16) |
|---|---|---|
| M1, to Mon 7 Dec | the Tauri shell; speech's first real run; the overlay and Tier 0 in a real panel; the replay harness | the idle replay report; ≥ 150 parser cases; the speech decision; 0 stale paints; a screen recording |
| M1.5, by Thu 10 Dec | an extractor that passes its bar (v0.2's first check is in, not yet publishable); a development-only P1 table on it, against persistence, climatology and S_rule | that table; the cloud Monte Carlo |
| Freeze, Mon 14 Dec | τ, S_mos, the analog weights, the block length, the MDE, K2's landmarks | the pre-registration hash |
| Tue 15 Dec 2026 → Thu 14 Jan 2027 | the sealed window, extended by any outage | uptime and exclusions |
| M2, Thu 10 → Sun 27 Dec | ephemeris validation (a first run is committed); both plates; Tier 1 in the overlay; goldens in CI; the API | ephemeris and pose reports; Tier 1 p95 idle |
| M3, Sun 27 Dec 2026 → Wed 6 Jan 2027 | Tier 2's kill test, then, only if it passes, its checks; the LLM fallback | the kill test; the 3 s share; false accepts; preference |
| M4, about Fri 15 → Wed 20 Jan 2027 | extractor validation; score once; a private dashboard | P1 with its CI, n and base rate; R1–R3; K1–K2 |
| M5, about Wed 20 → Sun 24 Jan 2027 | this README with its numbers; a 60-second demo | every number traced; `make score` from a fresh clone |
| v1, after M5 | viewpoints 3–8, each with ≥ 14 days of live data | the MSV's gates, per viewpoint |

A milestone more than 25 % late loses the next item on the cut list (PRD §19): rain and snow beyond extinction, the Moon and stars, the LLM fallback, live beside simulated, a shipped cloud-speech option, viewpoints beyond the first two, scrub polish, Tier 2, then post-MSV physics. Never cut: the collectors, the sealed evaluation, the latency harness, the labels and the data-flow inventory. Golden Gate joins only with written permission by Fri 4 Dec 2026; otherwise it moves to v2.

**Open decisions:** the fade's curve (D-094); the 48 h lead (D-102); which renderer P1 scores (D-019); the bar M1.5's table needs (D-100); persistence (b) (D-036); the held-out date reading (D-038, D-070); parser readings (D-039, D-058); the scorer's readings (D-096); the design per viewpoint (D-024); the manifest's shape (D-044); the goldens in git (D-041); the star catalogue (D-103); the hard stop (D-035); and the licence.

## Documents

| Document | What it holds |
|---|---|
| [PRD.md](PRD.md), [PLAN.md](PLAN.md) | the specification, v0.4.2; milestones, dates, the cut order and gate evidence |
| [DECISIONS.md](DECISIONS.md), [PRD_PROGRESS.md](PRD_PROGRESS.md) | every decision with its evidence and the spend ledger; tickets and every measured number |
| [docs/evidence/](docs/evidence/), [docs/research/](docs/research/), [docs/review/prd-crosscheck.md](docs/review/prd-crosscheck.md) | the 72 h gate and the first backtest pair; the research behind the main choices; how PRD v0.4's two drafts were reconciled |
| [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) | every source and package, with its licence |
| Package READMEs | [workers/common](workers/common/README.md), [capture](workers/capture/README.md), [forecast](workers/forecast/README.md), [eval](eval/README.md), [plates](plates/README.md), [renderer](renderer/README.md), [app/src](app/src/README.md), [app/speech-macos](app/speech-macos/README.md), [bench](bench/README.md), [bench/replay](bench/replay/README.md), [infra](infra/README.md), [spikes/m1-platform](spikes/m1-platform/README.md) |

## Credits and licences

- **Forecast data: NOAA HRRR via NOAA Open Data Dissemination** (public domain). The forecast skill in any result here is NOAA's.
- **Webcam imagery courtesy National Park Service.** Derived images are not NPS products, and no endorsement is implied.
- **Elevations and place names:** USGS 3DEP and GNIS (public domain).
- **The sky:** a port of the MIT-licensed reference implementation of Hillaire 2020, "Copyright (c) 2020 Epic Games, Inc."
- **The Sun and Moon:** oculus-core (MIT).
- **Ephemeris references,** used only to validate oculus-core: Giorgini, J.D. and the JPL Solar System Dynamics Group, "NASA/JPL Horizons On-Line Ephemeris System", https://ssd.jpl.nasa.gov/api/horizons.api, data retrieved 23 Nov 2026; and JPL's DE421 planetary ephemeris, read through Skyfield (MIT). Neither JPL source states a licence.
- Every other source, algorithm and package, with its licence, is in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES), the ledger of PRD §15.4.
- **Snow Globe's own licence is not chosen yet, so none is granted.**
