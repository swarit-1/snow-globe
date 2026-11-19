# Snow Globe — Product Requirements Document

> **Version:** v0.3 (Milestone 0: research folded in, two council rounds applied)
> **Status:** Design only. No application code exists. Awaiting owner answers to [§13](#13-open-questions-for-the-owner).
> **Date:** 18–19 Nov 2026
> **Owner:** Swarit Srivastava
> **Repo:** `snow-globe` (planned monorepo: `app/` Tauri 2 + Rust core, `renderer/` WebGL2, `workers/` forecast and capture, `backend/` FastAPI, `eval/` scoring and dashboard, `harness/` replay and latency measurement, `viewpoints/` manifest)
> **Companion docs:** [PLAN.md](PLAN.md) (milestones, dates, cut order, evidence), [DECISIONS.md](DECISIONS.md) (decisions with evidence; spend ledger), [docs/research/](docs/research/) (sourced landscape notes), [docs/council/](docs/council/) (council record)

---

## 0. TL;DR

**One line:** *Hold a key, ask "is the mountain out tomorrow at 4?", and see that real view at that hour — rendered from the real forecast and the real sky, not imagined.*

Snow Globe is a macOS push-to-talk **visual forecast**. A spoken place and time produce, within a second, an image of a real, fixed, camera-verified viewpoint whose sun and moon, cloud layers, cloud base, fog, precipitation and night lighting come from an ephemeris and NOAA's HRRR forecast for that place and hour. An image model may add photographic texture, but by construction it cannot change the sky or anything hidden by haze, and a validated check rejects it if it tries.

It is a portfolio project that has to earn three numbers, in this order:

1. **Perceived latency**, measured as a user experiences it. p95 from the OS key-up event to the first *composited* frame of each tier, after 30 minutes idle: real photo ≤ 300 ms (Tier 0), forecast-driven render ≤ 1 s (Tier 1, with a stretch target of ≤ 400 ms), refined render ≤ 3 s (Tier 2).
2. **A rendering pipeline that can be explained end to end.** Ephemeris → HRRR fields sampled where each view ray meets each cloud layer → deterministic WebGL2 shader → camera response model → optional texture refinement. The same renderer module runs in the overlay, the scrub view and headless evaluation.
3. **Accuracy measured against reality, pre-registered.** The primary claim is fixed in this document before any held-out frame is seen: on a sealed period, the 24-hour visual forecast of whether Mount Rainier's summit is visible has higher **Peirce skill score** than persistence. The skill comes from NOAA's forecast, and the README credits it. The renderer's own claim is that it **loses no skill** relative to HRRR's raw numbers (non-inferiority), and that its view-ray geometry **beats** the single-grid-cell scalar at predicting obscuration height.

**What research changed in your brief** (sources in [docs/research/](docs/research/)):

- **No licence-clean Golden Gate camera exists.** The available ones are commercial, no-derivatives, road-pointed at 320×260, or carry no stated terms. Proposed: Mount Rainier from Paradise (hero), Point Reyes (coastal fog), Grand Canyon (third, after the MSV) — all National Park Service cameras. Golden Gate only with an owner's written permission by **4 Dec 2026**.
- **Open-Meteo drops HRRR on 30 Nov 2026**, reports no run time, and has no cloud-base or ceiling field. Forecast ingest therefore reads HRRR directly from NOAA's AWS bucket.
- **Speech finalization may consume the Tier 0 budget by itself.** The best independently measured hosted p95 is ~300 ms, and on-device SpeechAnalyzer reports conflict 5×. Engines are chosen by measurement on your voice in the idle state.

**What the council changed** ([Appendix A](#appendix-a--council-review)): a Minimum Shippable Version with dates and a cut order; capture and forecast snapshots start immediately; a hindcast on archived data plus a sealed prospective window; one pre-registered primary endpoint; new baselines (climatology, forecast-scalar, forecast-class analog); clouds with vertical extent intersected along view rays; Tier 2 composited so the sky is untouchable; paint measured from screen capture with an in-frame tag; replay that swaps sample values inside the live audio callback; a data-flow inventory and licence ledger; nothing derived from camera frames published until NPS confirms in writing.

**Measured so far (this machine, 18–19 Nov 2026):**
- The Oculus ephemeris matches JPL Horizons to 0.19′ (Sun) and 0.13′ (Moon) worst case: about 150× inside the 0.5° budget ([§2.3](#23-oculus-ephemeris--reuse-audit)).
- The NPS feeds are live:
  - Point Reyes: a 3840×2160 frame updated 07:30 PM PST, with EXIF naming NPS as artist and copyright holder.
  - Rainier park camera: 1920×1080, updated off the quarter-hour.
- Open-Meteo's docs confirm "Discontinued 2026-11-30" for HRRR.

Nothing else here has been measured yet.

---

## 1. Product Vision and Positioning

### 1.1 What the product is

- **Hold** a hotkey, **say** a place and a time, **release**. Escape cancels; a new query cancels in-flight work; the overlay never takes focus from the app you are in.
- **Tiers**, each replacing the last with a 100 ms crossfade and each captioned:
  - **Tier 0:** the nearest *real* photo of that viewpoint, matched on Sun position and season.
  - **Tier 1:** the viewpoint relit in a GPU shader for the true Sun and Moon and the forecast sky.
  - **Tier 2:** the Tier 1 frame with photographic texture transferred from a few-step image model. The sky and hazed regions stay exactly Tier 1.
- **Times are local to the place.** A bare "4" is the next 4 o'clock *there*, with an AM/PM chip. "Sunset Friday", "golden hour", "tonight" and "now" resolve from the ephemeris at the place.
- **"Now"** shows the camera's latest frame, labelled LIVE with its age, beside the simulation.
- **The scrub view** drags through the forecast horizon.
- **Places outside the curated set** fall back to plain generation, labelled UNVERIFIED. This sits after the MSV.

### 1.2 What the product is NOT

- **Not an image generator.** Weather, light and geometry are computed. The image model is limited, by construction, to texture in clear regions.
- **Not a webcam viewer.** Cameras supply truth and plates.
- **Not a better weather forecast.** The skill is NOAA's. Snow Globe renders HRRR faithfully, and shows how often HRRR is wrong and why.
- **Not global.** Accuracy claims cover only curated, camera-verified viewpoints.

### 1.3 Why it is worth building

1. **Latency engineering** across speech → parse → GPU → network → GPU, measured from OS event to composited pixel, with a stale-paint correctness contract.
2. **Physically grounded rendering** that a reviewer can interrogate, down to which HRRR grid cell each cloud pixel came from.
3. **Forecast verification** in meteorology's own terms: pre-registration, persistence and climatology baselines, Peirce skill score, block bootstrap. It reports where the forecast fails (fog), and attributes the failure.

### 1.4 Comparable prior art

| Work | What it does | What Snow Globe adds |
|---|---|---|
| Wispr Flow | Push-to-talk dictation, non-activating overlay | The same interaction for a visual query, measured to the composited pixel |
| Windy / Ventusky / Apple Weather | Forecast maps and numbers | What the forecast looks like from one real viewpoint |
| Webcam sites | What a place looks like now | The future, verified against those cameras |
| Text-to-image "weather photos" | Invented weather | Computed weather; generative model confined to texture |
| Sunkavalli et al. 2007; Lalonde et al. 2009 | Relighting from a fixed camera's time-lapse | The camera's history as a relighting basis (post-MSV) |
| Koschmieder contrast methods; MIT LL VEIA | Visibility from target contrast or edge loss vs a clear-day reference | Per-landmark visibility as fog and summit truth |
| Kim & Pollefeys, PAMI 2008 | Radiometric calibration of a fixed camera from its own images | Model-free camera response, independent of the renderer |
| NPS air-quality webcams | Cameras built to document haze and visibility | The verified viewpoints |
| Delle Monache et al. 2013 (analog ensembles) | Forecast by retrieving similar past situations | A pre-registered analog baseline that the shader must not lose to silently |

---

## 2. Current State — Honest Audit

### 2.1 This repository
Empty until M0: branch `main`, no commits. M0 adds this PRD, PLAN.md, DECISIONS.md, `docs/research/` and `docs/council/`.

### 2.2 Machine inventory (inspected 18 Nov 2026)

| Item | Value | Consequence |
|---|---|---|
| Hardware | MacBook Air, Apple M4 (10-core GPU, Neural Engine, Metal 4), 16 GB, **fanless**, 60 Hz panel | Thermal state is recorded with every latency trace; one vsync = 16.7 ms. |
| OS | macOS 26.5.1 (25F80); macOS 27 is now current | Measure on 26.5.x; record OS/Xcode per measurement; no upgrade mid-milestone. |
| Xcode | 26.2, MacOSX26.2 SDK | SpeechAnalyzer and Foundation Models interfaces are in the SDK. |
| Rust | 1.98.1 with `wasm32-unknown-unknown` | oculus-core builds natively and to WASM. |
| Node / pnpm | 24.13.0 / 12.15.0 | Renderer and overlay. |
| Python / uv | 3.14.2 / 0.11.6 | Workers, backend, eval. |
| Other | Docker 29.5.3, ffmpeg 8.1.1, gh 2.91.0, Swift 6.2.3 | — |
| Missing | Tauri CLI (stable is 2.12.7) | Installed in M1. |
| Audio | Built-in MacBook Air microphone | Replay set recorded through the live capture path on it. |
| Time zone | America/Chicago | Viewpoints are in America/Los_Angeles and America/Phoenix (no DST): two real cross-zone tests. |
| Storage | Repo lives under `~/Documents` | If iCloud Desktop & Documents sync is on, voice fixtures and frames go elsewhere (checked in M1). |

### 2.3 Oculus ephemeris — reuse audit

`github.com/swarit-1/oculus`, crate `oculus-core` (MIT, same author), HEAD `06809e6`. `ephem.rs` uses JPL mean elements for the Sun (Standish 1992) and truncated ELP-2000/82 for the Moon (Meeus ch. 47), with aberration, precession–nutation and topocentric parallax. It sits on `time.rs`, `frames.rs` and `math.rs` (~1,500 lines; runtime dependency `libm` only). `sun_moon()` is exported to **Rust, WebAssembly and Python**. `natural.rs` has moonlit-sky brightness (Krisciunas & Schaefer 1991) and an empirical twilight term.

**Measured today** (`cargo test --release --test golden_astro`, 3/3 pass, against JPL Horizons and Astropy vectors):

| Quantity | Worst error | Budget |
|---|---|---|
| Sun apparent position | 0.19′ | 30′ |
| Moon apparent position | 0.13′ | 30′ |
| Moon illuminated fraction | 0.01 % | — |
| Refracted altitude, stars > 5° | 21.6″ | — |

**Caveats:**
1. `Apparent.alt` is unrefracted. Horizon refraction (~34′) exceeds the budget, so validation and rendering use refracted altitude, with −0.833° for sunrise and sunset.
2. Oculus's test sites aren't ours. M2 regenerates Horizons vectors for each viewpoint, hourly for 12 months, and checks sunrise/sunset against NOAA to ±1 min.
3. Valid to 2050.

**Proposed (D-002):** a Cargo git dependency pinned to `06809e6`; its MIT notice ships inside the WASM bundle.

### 2.4 move-predictor

This PRD mirrors move-predictor's PRD structure, and the README will mirror its README. One lesson carried over: its README reports 22–26 % top-1 against a "> 36 %" target. Here, the targets, the primary endpoint and a README skeleton with blanks are fixed up front, and every miss is reported with its measured value.

### 2.5 Current landscape (looked up 18 Nov 2026; not yet benchmarked by us)

| Area | Shortlist | Key facts |
|---|---|---|
| **On-device STT** ([notes](docs/research/stt-ondevice-macos.md)) | **SpeechAnalyzer `DictationTranscriber`** (A1); **FluidAudio Parakeet** (P) only if A1 misses its gate | Only `DictationTranscriber` honours contextual strings. Release → final reports conflict: 125–132 ms (vendor, M5 Max) vs 602–702 ms (one app). Live input broke on 26.3. |
| **Cloud STT** ([notes](docs/research/stt-cloud.md)) | **Deepgram Nova-3** (D) with `Finalize`; Soniox stt-rt-v6 in reserve | Pipecat TTFS P50/P95: Nova-3 247/298 ms. Deepgram trains by default (opt out per request with `mip_opt_out=true`). AssemblyAI excluded: free accounts can't opt out of training. |
| **LLM fallback** | **Apple Foundation Models** (in-process, $0); Claude Haiku 5.5 only if FM fails | FM is rate-limited outside a UI process and unavailable without Apple Intelligence. Haiku's retention terms must be recorded before it is enabled. |
| **Tier 2** ([notes](docs/research/refinement.md)) | **Z-Image-Turbo img2img** on fal and on Runware; FLUX.2 klein 4B on fal as a ~20-call illustration only | Only SDEdit-style img2img keeps pixels aligned. FLUX.2, Kontext, Qwen, Nano Banana, GPT-image and Seedream regenerate every pixel. New fal accounts are capped at 2 concurrent requests; cancelling a running request only signals the runner. |
| **Cameras** ([notes](docs/research/cameras.md)) | **NPS:** Rainier `webcams-mora/mountain.jpg` (1080p), Point Reyes air cam (4K, 15 min), Grand Canyon Yavapai air cam (4K, 15 min) | Windy, Skyline and EarthCam forbid storing or republishing. ShareAlike photos would force derived plates to CC BY-SA. Partner cameras on NPS pages are excluded. |
| **Forecast** ([notes](docs/research/forecast.md)) | **HRRR direct** (AWS `noaa-hrrr-bdp-pds`, byte ranges via Herbie); Open-Meteo (pinned GFS) beyond 48 h and for unverified places | HRRR runs hourly (18 h, or 48 h at 00/06/12/18Z), available 52–108 min after start. Fields: `VIS`, `HGT:cloud ceiling`, `HGT:cloud base`, `LCDC/MCDC/HCDC`, `PRATE`, `RH`, `HPBL`, snow. VIS sees fog only when cloud water is within ~100 m of the ground. Archived since 2014; HRRRv4 from Dec 2020. |
| **Independent truth** | GOES-18 (AWS `noaa-goes18`: `ABI-L2-ACMC` clear-sky mask, C02, C13 − C07) at the viewpoint pixels; on-site NPS met where present | The GOES fog/low-stratus product is not in the open bucket. No ceiling or visibility station at Paradise or Point Reyes. |
| **Sky model** | **Hosek–Wilkie** via `andrewwillmott/sun-sky` (repo Unlicense; **data tables BSD-3**, notice kept) | Fitted only for Sun ≥ 0°; twilight comes from a separate model. Fallback: three.js `Sky.js` (MIT, pinned r187). |
| **Shell** ([notes](docs/research/stt-ondevice-macos.md)) | Tauri 2.12.7; `tauri-plugin-global-shortcut` 2.4.1; `tauri-nspanel` 2.1.1 | Press and release events with no key repeat. nspanel uses the **private** `_setPreventsActivation:`. Hidden WKWebViews are suspended by default. Option-only shortcuts are rejected on macOS 15+. |

---

## 3. The Hard Problems (prioritized)

### 3.1 P0 — Speech finalization dominates Tier 0

The time is usually the last word, so Tier 0 waits for it. The best measured hosted p95 is 298 ms; one SpeechAnalyzer report has 602–702 ms from stop to final text. **Chain for Tier 0 ≤ 300 ms** (the panel is already visible from key-down, so no reveal sits on this path): STT final ≤ 240 + parse 5 + image swap from a pre-decoded bitmap ≤ 10 + composite ≤ 2 vsyncs 33 ≈ 288 ms. That requires a p95 release → final ≤ ~240 ms *in the idle state*, which no engine has yet demonstrated on this machine. The levers, measured rather than assumed:

1. **Explicit finalize on key-up.** A synthetic-silence pad is pushed as a burst, not in real time, and no audio is captured after release.
2. **Partial-based Tier 0.** At key-up, if the latest partial is stable, Tier 0 paints from it. The stability window is tuned on the development half under a ceiling: the wrong-paint rate must be ≤ 2 % (Wilson upper bound on the test set), or partial-based paint is disabled. "Wrong" means a different Tier 0 image id from the one the final selects.
3. **Engine choice in the idle state.** If A1's idle p95 exceeds 250 ms, Parakeet is benchmarked.

If Tier 0 still misses, the report states the measured p95 and the bottleneck stage. An LLM fallback rate above ~5 % fails Tier 0 by construction (~0.6–0.9 s per call), and that is stated too.

### 3.2 P0 — Tier 2 p95 over a network you don't control

For the same model, vendor figures range from 0.3 s of GPU time to ~6 s end to end, so the tails come from queueing and cold starts. New fal accounts are capped at 2 concurrent requests, and a cancelled request may keep its slot until the runner stops.

Rules:
- At most one Tier 2 request in flight per user, and cancelled-but-unfinished requests count against the cap.
- No speculative Tier 2 and no paid warm-up in v1; a TLS pre-connect on key-down only.
- The chosen proxy path may add at most 100 ms p95 over a direct call.

The headline is **"Tier 2 delivered within 3 s" over all queries**; rejections, errors and timeouts count as misses.

### 3.3 P0 — Fog and obscuration
See [§4](#4-the-fog-and-obscuration-problem).

### 3.4 P1 — Truth from webcams is noisy

The camera itself produces errors that are not forecast errors: auto-exposure, white balance, night noise, rain, ice or salt on the lens, stale frames, camera moves.

Rules:
- Staleness is detected from the NPS `imagedate` field plus HTTP `Last-Modified` plus the image hash.
- Camera moves are detected by registration against the plate.
- Exclusions apply identically to the model and to every baseline, and their counts are published.
- Results are reported with and without iced-lens exclusions as a sensitivity check.
- "Day" is defined by ephemeris Sun altitude, never by frame brightness, because brightness correlates with weather.

### 3.5 P1 — Licensing decides the viewpoints, and NPS confirmation gates publishing

- **What the frames say about ownership.** The NPS air-cam EXIF names NPS as artist and copyright holder. The capture software tag is `ARS_Digicam`, which suggests a contractor operates the camera. The Rainier park camera carries no ownership metadata.
- **Written confirmation.** It is requested from NPS Air Resources Division and Mount Rainier NP, covering all three cameras and the archive, by the end of M2.
- **Until confirmed:** capture and private evaluation only. "Private" includes the app: plates and thumbnails are served only to the owner's install, from authenticated storage.
- **If there is no answer by the deadline:** the public repo, README and demo carry code, numbers and charts only, with no frames, plates or renders. The owner signs off on that list.

### 3.6 P1 — Season, base rate and statistical power

It is November.
- **Rainier:** winter cloud makes "summit visible" frequent enough to score, but the base rate is skewed. That is why the endpoint uses the Peirce skill score: proportion correct would reward always saying "hidden".
- **Point Reyes:** fog peaks in summer, so a December window will hold few events. Point Reyes is a secondary endpoint, scored on a summer season through the hindcast if the archive reaches back that far.
- **Statistics:** the minimum detectable effect for each primary endpoint is computed from development-period variance and published before sealing. If it is implausibly large, the replacement endpoint is already named (§8.2).

### 3.7 P2 — A photograph already has lighting baked in

Relighting one photo is ill-posed. The MSV renderer uses:
- a **dehazed overcast plate** as an albedo proxy, without double-counting sky occlusion;
- a **night plate** for emissive lights;
- **metric depth** from a DEM ray-cast plus scale-aligned near-field depth;
- **DEM horizon maps** for shadows, since the occluders are off-screen;
- a **model-free camera response**.

Further physics is a post-MSV backlog, each item admitted only with a measured gain (§5.3).

### 3.8 P2 — macOS platform fragility

The risks: a private-API panel; TCC grants that reset on every ad-hoc-signed rebuild (a stable development certificate is required); hidden-webview suspension; a Swift-only speech API; Screen Recording re-consent prompts during unattended runs. A one-day spike proves them together first.

### 3.9 P2 — Forecast source churn

Open-Meteo drops HRRR on 30 Nov; RRFS replaced NAM on 3 Nov and may later replace HRRR. One forecast schema with per-source adapters; a model change is a labelled break in the series.

---

## 4. The Fog and Obscuration Problem

### 4.1 What each viewpoint needs

| Viewpoint | Phenomenon | Decisive forecast fields |
|---|---|---|
| **Rainier (Paradise, ~1,650 m; summit 4,392 m)** | Summit or upper mountain hidden by cloud base or cap cloud; camera itself inside cloud | Ceiling and cloud base **along the view ray to the summit**; low/mid cloud; snow |
| **Point Reyes** | Marine stratus and advection fog over the beach and headland; camera possibly inside the marine layer | Low cloud, cloud base, surface visibility |
| **Grand Canyon** (post-MSV) | Winter snow and cloud in the canyon | Cloud base vs rim and canyon-floor heights. The haze metric is dropped: haze is aerosol, which HRRR VIS doesn't model. |

HRRR's visibility diagnostic sees fog only when model cloud water lies within ~100 m of the ground, so a deck at 300 m reads as clear. Ceiling and cloud base are the decisive fields. **Every height field has a pinned datum** (AGL over HRRR's smoothed terrain vs MSL), recorded in DECISIONS.md, because one 3 km grid cell can span a canyon rim and floor.

### 4.2 Rendering model (MSV)

- **Per-pixel metric depth and world height** come from a DEM ray-cast with the camera pose solved from surveyed landmarks (the NPS publishes a named-landmarks image per air cam), plus scale-aligned near-field depth from a permissively licensed model (CC-BY-NC weights excluded).
- **Clouds with vertical extent.** Each layer is a slab with a base and a thickness. Each view ray is intersected with each slab, with Earth curvature, and the forecast is read **at the intersection point** (a 2° ray meets a 500 m layer ~14 km out). Noise is thresholded before filtering, so coverage doesn't bias with distance. An overlap rule is checked against HRRR total cloud.
- **The camera inside cloud** is its own state: uniform extinction, no landmarks.
- **Surface fog and ceiling slab.** Extinction is β = −ln(ε)/V, with ε pinned to the visibility diagnostic's contrast threshold, and β decays above the fog top. Thick fog uses a multiple-scattering airlight approximation rather than single scattering.
- **Precipitation** is extinction plus wet-surface darkening. There are no rain streaks, because they are invisible at webcam shutter speeds.

### 4.3 Measuring it in the real frame

- **Primary truth: binary per-landmark visibility.** Each landmark is scored visible or hidden by comparing its edge energy against an illumination-conditioned clear-day reference (VEIA-style). For Rainier the landmarks are a summit band, an upper-mountain band, the mid-slopes and a near ridge, which gives a categorical **obscuration height**: the highest visible landmark band.
- **Secondary truth:** a continuous visibility estimate from multi-landmark contrast, scored as ordinal bins bounded by the landmark distances, with the top bin censored at ~80 × the farthest distance, plus a fog-top height from world height.
- **Independent votes:** GOES-18 clear-sky mask and reflectance at the viewpoint pixels, and on-site NPS met where it exists. Disagreements are flagged, not averaged.
- **One extractor** is applied to rendered frames, baseline frames and real frames alike. It is validated against labelled real frames *and* about 50 labelled renders before any number is published.

### 4.4 What I expect, plainly

**Rainier:** HRRR ceiling and cloud base should carry real skill over persistence at 24 h, because synoptic cloud is what NWP does well. I expect the claim to be earnable on a long enough window.

**Point Reyes fog:** I expect weak skill at 24 h and frequent false alarms. Most of the error should be HRRR's rather than the renderer's. There is no published HRRR verification for this kind of fog, HRRR's marine-layer tops run ~150 m low, and SFO historically needed a purpose-built forecasting system. Post-MSV, an oracle ladder will attribute the error: forecast → HRRR f00 at valid time → GOES → camera-extracted inputs (the last rung labelled circular).

A statistical fog post-processor is out of v1; it would adjust weather, so it needs your sign-off.

---

## 5. Milestones, MSV and Cut Order

### 5.1 Minimum Shippable Version

Push-to-talk → Tier 0 and Tier 1 on **Rainier and Point Reyes**. Latency measured idle, with the paint-capture harness. P1 pre-registered and scored on the sealed period. Baselines: persistence, climatology, forecast-scalar and forecast-class analog. A validated extractor, the data-flow inventory, the licence ledger, and a README with the numbers. **Nothing outside the MSV starts until the MSV is published**, except the capture job, which starts first.

### 5.2 Milestones

Milestone dates depend on your hours per week (§13 Q2); [PLAN.md](PLAN.md) holds them. **Rule: a milestone that runs more than 25 % late loses the next item on the cut list (§5.3).**

| # | Deliverables | Done when this evidence exists |
|---|---|---|
| **0** | PRD, PLAN, DECISIONS, research, council | This document plus your answers to §13 |
| **0.5** (immediately on go-ahead) | Forecast-snapshot worker (HRRR) and polite capture for all three viewpoints; NPS archive depth check; emails to NPS (and Golden Gate owners) sent | Worker and capture logs; archive report (depth, cadence, camera epochs); design H or P chosen (§8.4) |
| **1** | Platform spike; hotkey; audio while held; A1 + D behind the STT trait; parser (+ FM fallback); overlay with Tier 0; replay and paint-capture harness; README skeleton with blanks | Replay report (idle and warm, ≥ 100 test utterances): p95 per stage; Tier 0 and first-partial numbers; WER; place accuracy; parser tests green; STT decision in DECISIONS.md |
| **1.5 — walking skeleton** (≤ 21 days after go-ahead) | Renderer v0 (sky, layered cloud with base, ceiling slab, flat fog, camera model) headless; extractor v0; P1 scored on development data vs persistence, climatology, forecast-scalar | A dev-only P1 table (no held-out data touched) |
| **2** | Tier 1 for Rainier and Point Reyes in overlay and scrub view; ephemeris validation; golden images; renderer features admitted only with measured dev gain | Ephemeris report; Tier 1 p95 (idle); golden images in CI; dev-split evidence for each admitted feature |
| **3** (after MSV core) | Tier 2 kill test (≤ $1, 20 frames) → if it passes, composited Tier 2, checks, false-accept suite, cancellation; unverified fallback | Kill-test result; Tier 2 delivery rate within 3 s; false-accept bound; spend |
| **4** | Sealed-period scoring; dashboard; extractor validation; uptime | P1 result with CI and the non-inferiority and superiority tests; secondaries with Holm correction; uptime and exclusions |
| **5** | README with demo, architecture, numbers | Every number traceable to a file and `make score`; ships on schedule whatever P1 shows |

### 5.3 Cut order (first to go)

1. Unverified-place fallback.
2. LLM fallback (alias, fuzzy and phonetic matching plus chips cover ≤ 10 places).
3. Grand Canyon viewpoint.
4. Scrub view polish (keep a bare slider).
5. Tier 2 (if the kill test fails or the schedule demands it; Tier 1 is the floor).
6. Post-MSV physics backlog items, lowest measured gain first.

**Never cut:** the capture job, the sealed evaluation, the latency harness, the data-flow inventory.

---

## 6. Detailed Specifications

### 6.1 Interaction and overlay

```
idle ──key-down──▶ listening ──partial──▶ resolving ──key-up──▶ tier0 ──▶ tier1 ──▶ tier2
  ▲                    │ Esc / key-up with no speech                 │ any state: Esc → idle
  └────────────────────┴──────────── new key-down cancels everything ┘
```

- **Panel:** transparent, non-activating, always on top (`tauri-nspanel` 2.1.1, or our own NSPanel via `objc2-app-kit` if the private API breaks).
  - It is created at launch with background throttling disabled.
  - It is **revealed on key-down**, so wake-from-suspension lands during speech rather than after key-up.
  - The spans `reveal_req → visible → first_rAF → composite` are traced on every reveal.
  - Keeping the panel ordered in at alpha 0 (click-through) is tested for latency and power.
- **Hotkey:** a normal combo via `tauri-plugin-global-shortcut` 2.4.1, not Option-only. Releasing the modifier before the key is tested. Fn stays a stretch goal.
- **Escape** is registered as a global shortcut only while the overlay is visible.
- **Destination chip:** `on-device`, or `→ <provider>` for *any* audio or text leaving the machine. It changes **before** any fallback upload. An **on-device-only** setting disables every cloud path.
- **Correctness contract:**
  - Every paint is gated in the webview, immediately before compositing, on (query_gen, scene_hash, tier) matching current state.
  - Late results are dropped.
  - A wrong paint never counts as a hit; it is reported as visible-wrong-ms and flips per query.
  - A randomized key-mash stress test (≥ 10k cycles, injected stage delays) asserts zero stale paints.

### 6.2 Speech-to-text

```rust
trait SpeechEngine { fn start(&self, ctx: SpeechContext) -> Result<Box<dyn SpeechSession>>; }
trait SpeechSession {
    fn push_audio(&mut self, pcm: &[i16]);       // capture thread, while held
    fn partials(&self) -> Receiver<Partial>;     // text, stability, t_audio_ms
    fn finish(self: Box<Self>) -> Result<Final>; // key-up: burst silence pad + explicit finalize
    fn cancel(self: Box<Self>);
}
```

- **Audio.** `cpal` 0.18.2, opened on key-down and closed on key-up. No pre-roll buffer. Audio is held in RAM for the query only. Stream teardown happens off the key-up path.
- **Swift.** An **in-process static library with a C ABI** (`@_cdecl`) hosts SpeechAnalyzer and Foundation Models. Foundation Models must run in the UI process.
- **Vocabulary.** Manifest aliases are passed as contextual strings (A1) or keyterms (D).
- **Engines.**
  - **A1:** `DictationTranscriber`. Before it is labelled `on-device`, the spike verifies with a network monitor that it makes no network calls.
  - **D:** Deepgram Nova-3. `mip_opt_out=true` is set in one function, covered by unit and integration tests. A short-lived token is minted by the backend **in parallel with opening the mic**, with audio buffered locally meanwhile, and traced as an `stt_token` span. If Deepgram has no token API, there is **no cloud primary**, never a long-lived key in the app.
  - **P:** Parakeet, only if A1's *idle* release → final p95 exceeds 250 ms.
- **Selection.** The primary is the engine with the lowest idle p95 release → final at equal or better post-parser place accuracy. The fallback is the best engine of the other kind (on-device vs cloud).

### 6.3 Parser

Output (example):

```json
{"place_id": "rainier", "viewpoint_id": "rainier.paradise-mountain",
 "valid_time": "2026-11-19T16:00:00-08:00", "valid_time_utc": "2026-11-20T00:00:00Z",
 "tz": "America/Los_Angeles",
 "time_source": {"kind": "clock", "text": "tomorrow at 4", "rule": "next-occurrence-am-or-pm"},
 "ambiguities": [{"field": "meridiem", "chosen": "pm", "alternative": "2026-11-19T04:00:00-08:00"}],
 "overrides": {}, "path": "fast", "confidence": 0.97}
```

**Fast path** (deterministic, ≤ 2 ms, Rust):
- **Places:** alias → exact → token fuzzy → phonetic (Double Metaphone), with a margin rule that triggers a disambiguation chip.
- **Times:**
  - **Clock:** a bare hour resolves to the next H:00 or H+12:00 at the place.
  - **Days:** today, tonight, tomorrow, weekdays, this/next.
  - **Solar events** from oculus-core: sunrise and sunset at −0.833°; golden hour rendered at +2°; blue hour at −5°; civil dawn and dusk at −6°.
  - **"tonight"** = 21:00 local, or 1 h after sunset if later. **"now"** triggers LIVE.
  - **DST:** a nonexistent local time shifts forward and is flagged; an ambiguous one takes the earlier offset and is flagged.
- **Horizon:** HRRR up to 48 h; GFS up to 7 days, with the caption saying so; beyond that, Tier 0 only.
- **Overrides:** a weather override switches the caption to "WHAT-IF".

**LLM fallback** (after the MSV core) runs only below a confidence threshold. It uses Foundation Models with a `@Generable` struct; the output is schema-validated and can never invent a `place_id`. Its rate and latency are reported.

**Tests:** ≥ 150 table-driven cases with an injected clock, covering:
- noon and midnight edges;
- machine zone ≠ place zone, including America/Phoenix across both DST changes;
- the DST days 1 Nov 2026 and 14 Mar 2027;
- weekday wrap;
- solar events after the morning event has passed;
- aliases, misspellings and phonetic errors;
- ambiguity.

Property tests check future-ness, horizon flags, and stability of the canonical form.

### 6.4 Tier 0

- **Library.** Captured frames plus NPS archive frames from the same camera, stored privately at ≤ 1920 px. Each entry records Sun altitude and azimuth, day of year, extractor weather class, source and licence.
- **Match.** Nearest on Sun altitude/azimuth, then day of year. The forecast-class analog adds the forecast weather class, and is a pre-registered baseline.
- **Latency path.** Thumbnails for the viewpoint's plausible next 48 h are decoded with `decode()` in the background, so key-up → parse → swap.
- **Leakage rule.** A baseline only sees frames captured before the issue time.

### 6.5 Tier 1 renderer

- **Interface.** `render(scene: SceneSpec, target) → Frame` is pure in (scene, renderer version). It is TypeScript + GLSL ES 3.0, imported by the overlay, the scrub view and the headless evaluator.
- **Inputs.** `SceneSpec` = viewpoint + `valid_time_utc` + forecast samples (per layer: cover, base, thickness; ceiling; visibility; precipitation rate and type; RH; boundary-layer height; snow; wind) + the displayed seed. Sun and Moon come from oculus-core WASM inside the module.
- **MSV passes:**
  1. Hosek–Wilkie sky for Sun ≥ 0°. A separate twilight model (oculus-core empirical term) below 0°, scored separately. Moonlit night sky.
  2. Cloud slabs intersected along view rays with curvature.
  3. Dehazed overcast plate × (sky irradiance + Sun irradiance × N·L × DEM-horizon shadow), plus night emissive above a fitted Sun-altitude threshold.
  4. Height- and depth-aware fog and ceiling slab.
  5. Precipitation as extinction and wetness.
  6. Camera model.
- **Camera model.** Model-free radiometric response (Kim & Pollefeys 2008), fitted on development frames across all weather, so the exposure slope is identified at overcast and fog luminances. Few parameters. No valid-time-frame statistic enters it. Published with it on and off.
- **Cloud Monte Carlo test.** ≥ 1,000 seeds × cover ∈ [0.1, 0.9], reporting rendered fraction by elevation band against cover.
- **Determinism.** Same `SceneSpec` and version give identical pixels on the same backend. Golden images are rendered on Chromium/SwiftShader in CI; WKWebView has a looser, documented tolerance.
- **Performance.** ≤ 50 ms of GPU time (`fenceSync`) at 1280×720. Shader variants are pre-warmed after launch and after context loss. GPU-resident plates are ≤ 1920 px wide.
- **Pre-render cost.** Tier 1 may pre-render (not paint) from a stable partial. Whether that pre-render slows STT through GPU/ANE contention is measured (A1 release → final with pre-render on vs off), not assumed.

### 6.6 Tier 2 (after the MSV core)

1. **Kill test first:** 20 Tier 1 frames, run offline through Z-Image-Turbo img2img (strength 0.15–0.35), for ≤ $1. It records the check pass rate and a preference judged by someone other than you. It continues only if refinement is visibly better and passes checks.
2. **By construction.** Tier 1's sky is composited back through the sky mask. Only high-frequency ground detail is transferred, weighted by Tier 1 transmittance T(x), so hazed or fogged regions cannot be dehazed. This runs on the GPU in the webview in ≤ 15 ms, with a per-frame phase-correlation registration check: a one-pixel crop or resize by the provider would ghost the transferred detail.
3. **Checks.** Structure (edge F1, landmark positions ±2 px), weather (sky cloud fraction, landmark contrast), light (region luminance and ΔE2000).
   - **False accepts** are measured on ≥ 200 deliberately weather-altered refinements run through the *full composited pipeline*: sky edits including sub-δ ones, snow added or removed, wet surfaces, fog texture. Zero accepts bounds the false-accept rate at ≤ 1.5 % (95 %).
   - Thresholds are calibrated on a separate calibration set.
4. **Survival.** ≥ 80 % of queries receive an accepted Tier 2 within 3 s, **and** a blind preference test (≥ 50 pairs, rated by someone other than you) prefers it in ≥ 70 % of pairs. Otherwise it ships off by default and the README says why.
5. **Providers and transport.**
   - Z-Image-Turbo on fal, using `X-Fal-Store-IO: 0` and a minimal CDN lifetime; Runware as the alternate host.
   - A 50-call cancel microbenchmark records time-to-terminal and amount billed.
   - Binary upload where the API allows it.
   - FLUX.2 klein runs on fal (not BFL, whose policy "may train on" inputs) as a ~20-call illustration of why reference editors fail, not as a rate.
6. **Disclosure.** Exported refined frames carry a burned-in label plus IPTC `trainedAlgorithmicMedia`.

### 6.7 Live camera ("now")

The latest frame is fetched in the background, polling the JSON and fetching the image only when `imagedate` changes. The caption reads `LIVE · frame time 16:00 PST (7 min old) · Courtesy National Park Service`. Installed copies follow the same polite-fetch rules.

### 6.8 Unverified places (first on the cut list)

- **Inputs.** Open-Meteo geocoding and a forecast from a pinned model.
- **Generator.** A named provider and model, with retention recorded before it ships.
- **Label.** `UNVERIFIED · generated image · scenery is invented`, burned into the pixels on export, plus IPTC metadata.
- **Scope.** No accuracy claim, and the spend guard applies.

### 6.9 Captions

```
SIMULATED · Thu 19 Nov 2026 16:00 PST (valid) · lead 22 h 35 min
cloud low 80% · mid 10% · high 0% · ceiling 2,300 m · visibility 12 km · snow 0.4 mm/h
HRRR 18Z run (NOAA), available 19:27 UTC · Sun 3.1° · Moon 41% lit, below horizon
```

- **Exports keep the caption.** Every exported or published image has its caption burned in.
- **Attribution.** NPS, NOAA and Open-Meteo (with a link where its data is shown, marked "modified") are credited per the licence ledger (§9.3).

### 6.10 Forecast worker

- **Extraction.** After each HRRR run is available, byte-range GETs (Herbie) extract the fields at every viewpoint's sample points. Sample points are placed where the view rays meet each layer height.
- **Output.** One JSON file per viewpoint per run, recording:
  - source, model, and run start (from the path and the GRIB `d=` field);
  - availability (from S3 `LastModified`, live only) and fetch time;
  - grid cells and byte ranges;
  - the values.
- **Shared file.** The app reads the latest file and the evaluator reads the same files.
- **Hindcast availability.** For archived runs, availability is modelled as run start + the p95 lag per forecast hour measured live; archived S3 timestamps record the backfill upload, not availability. A missing run falls back to the previous run and is never dropped.
- **Lead time** is valid time minus availability. 24 h leads come from the 00/06/12/18Z runs (actual 24–29 h), and the actual lead is kept.
- **Open-Meteo** (beyond 48 h; unverified places): a pinned model, bracketed by `meta.json` reads. A call is accepted only if `last_run_initialisation_time` is unchanged across it.

### 6.11 Viewpoint manifest

One entry per plate. It holds:
- id, aliases, timezone;
- camera pose and its solve residual;
- assets (plates, depth, world height, masks) with a licence per asset;
- landmarks (lat, lon, height, distance, pixel, band);
- forecast sample points;
- camera-response parameters and the frames they were fitted on;
- the webcam block: JSON/image URLs, measured interval, terms URL, U1–U4 status, attribution, polite-capture caps, ownership basis and confirmation status;
- truth sources.

The full JSON schema lands with the M1 code and is validated in CI.

### 6.12 Logs and schemas

- **Records.** `forecast_run`, `prediction` (with both lead definitions, renderer SHA, SceneSpec hash and the displayed seed), `capture` (source stamp, hash, stale flag, registration residual, exclusion reason) and `score` (metric, value, extractor version).
- **What gets predicted.** Predictions are issued hourly at leads 1, 6 and 24 h.
- **Versioning.** Inputs are frozen, so a new renderer version is scored as a new version.

### 6.13 Latency measurement

- **t0.** The OS key-up event timestamp. Replay injects key events with `CGEventPost` at the HID tap, so the real Carbon hotkey path fires. The harness process (not the app) holds a listen-only tap that records only the hotkey's timestamps, never other key codes. The tap is compiled out of release builds.
- **Paint.**
  - **Definition:** the first WindowServer-composited frame in which the tier is at ≥ 50 % opacity.
  - **Capture:** ScreenCaptureKit captures **only the overlay window** (or the tag rectangle) in measured runs. Each frame carries a pixel tag (query_gen, tier) drawn **inside the same surface** as the content: the WebGL canvas or the Tier 0 image layer.
  - **Data kept:** the tag is decoded in memory and only timestamps are kept. Nothing is written to disk without a debug flag; if written, it stays local and is deleted after 7 days.
  - **Overhead:** capture on vs off must differ by less than one vsync at p95.
  - **Diagnostics:** `requestAnimationFrame` and IPC timestamps are kept as diagnostics, each with its uncertainty.
- **Clock domains.** ScreenCaptureKit presentation timestamps and Rust `Instant` should both be host time; this is verified in the spike.
- **Spans.** OS event → handler dispatch; audio buffer delivery; `stt_token`; DNS/TCP/TLS/WebSocket/TTFB (with a connection-reused flag); proxy-internal spans with propagated trace context; provider-reported queue and inference times; readback, encode and IPC; reveal; paint.
- **Recorded per trace:** idle duration, cache, connection and model state; thermal state; power source; network interface **type only**.

### 6.14 Replay harness

- **Substitution.** Only sample *values* are swapped inside the live `cpal` callback. The real device opens, buffers arrive at their real cadence, and key-up triggers the real flush. No stage is mocked.
- **Fixtures.** ~130 distinct scripted utterances (30 dev, 100 test), recorded through the live open-on-key-down path, plus ≥ 30 opt-in real-use utterances reported separately. The report includes the share of utterances still in speech at key-up.
- **States.** Warm (< 60 s since the last query), **idle (≥ 30 min, the headline)**, post-wake and post-launch.
  - Idle trials: ≥ 100 for the shipped engine and ≥ 60 for the comparison engine, run unattended overnight under `caffeinate -i` from a frozen measurement build. Concurrent load is recorded and the calendar stated.
  - Each pass uses its own cache namespace.
- **Statistics.** Cluster bootstrap by utterance. Runs are spread over ≥ 3 days at different times of day, with engines interleaved.
- **Report.** p50/p95 per stage and tier with CIs; budget hit rates; wrong-paint rate; partial lag per engine; WER; place accuracy; fallback rate. Known bias: one speaker, one mic, one room.

---

## 7. Architecture

### 7.1 Interactive path

```
┌──────────────────────── macOS app (Tauri 2.12) ─────────────────────────┐
│ Rust core: hotkey · cpal (while held) · SpeechEngine · parser (+ oculus) │
│            cancellation · tracing · caches (forecast files, Tier 0, plates)
│   Swift static lib (C ABI): SpeechAnalyzer · Foundation Models          │
│ WKWebView panel: Tier 0 swap → Tier 1 WebGL2 (+ oculus WASM) → Tier 2   │
│                  composite (sky locked, T(x)-weighted detail)           │
└──────┬───────────────────────┬──────────────────────┬────────────────────┘
       │ forecast JSON          │ STT (if cloud)       │ Tier 2 (≤ 1 in flight)
       ▼ (authenticated store)  ▼ token minted in ‖    ▼
  private object storage    Deepgram             backend: proxy or token mint,
                                                  spend guard, per-install auth,
                                                  async log (no text, no IP kept)
```

### 7.2 Evaluation path

```
unattended:                                             batch (daily) and CI:
  hourly  forecast worker (HRRR byte ranges)              headless render (SwiftShader)
  JSON-poll capture; fetch image on imagedate change  ─▶  one extractor on renders/baselines/truth
  archive backfill (1 request at a time, ≥ 5 s apart,     score P1 (+ secondaries, Holm)
          resumable, size-capped)                         dashboard (private until NPS confirms)
  → private storage + small SQL DB                        `make score` reproduces from frozen inputs
```

### 7.3 Challenges to your default architecture (need your go-ahead)

| # | Default | Recommendation | Why |
|---|---|---|---|
| C1 | Tauri 2 | **Keep**; Swift as an in-process static library with a C ABI | `swift-rs` has no async support; Foundation Models must run in the UI process |
| C2 | Backend fetches Open-Meteo | **HRRR worker writes one JSON per viewpoint per run; the app and the eval read the same file** | Open-Meteo drops HRRR on 30 Nov, has no ceiling, no run time; one source of truth |
| C3 | Tier 2 via proxy | **Short-lived token + direct upload if supported, else a never-sleeping proxy**; ≤ 100 ms p95 gate | Removes a hop; keys stay server-side |
| C4 | One scheduled job | **Small unattended worker + daily batch** | Frozen inputs make scoring reproducible |
| C5 | Renderer takes Sun/Moon as input | **Computed inside via WASM** | One self-contained module |
| C6 | — | **Model-free camera response in Tier 1** | Otherwise brightness and colour measure the camera |
| C7 | Hosted inference only | **Optional:** a small on-device one-step model on the M4 GPU as a $0 Tier 2 candidate | Apple Silicon is a GPU here; likely lower quality |
| C8 | Golden Gate hero | **Rainier hero; Point Reyes fog; Grand Canyon third; Golden Gate only with permission by 4 Dec** | Licensing |
| C9 | Tier 2 "refines the frame" | **Tier 2 contributes ground texture only; sky and hazed regions are Tier 1 by construction** | Makes "never invents weather" structural, not just checked |

---

## 8. Metrics and Eval (pre-registration)

This section is the pre-registration. It is frozen, with a commit hash recorded in DECISIONS.md, before any held-out frame is scored, labelled or viewed. The held-out frames are excluded from the labelling page.

### 8.1 Latency targets (p95, OS key-up → composited paint, **idle**)

| Target | Chain (initial allocation) |
|---|---|
| **Tier 0 ≤ 300 ms** | STT final ≤ 240 · parse 5 · swap ≤ 10 · composite ≤ 33 |
| **First forecast-driven frame (Tier 1) ≤ 1,000 ms; stretch ≤ 400 ms** | STT final ≤ 240 (or stable partial) · parse 5 · GPU ≤ 50 · composite ≤ 33 · margin |
| **Tier 2 ≤ 3,000 ms, reported as the share of queries delivered within 3 s** | STT final ≤ 240 · parse 5 · Tier 1 render + readback + JPEG encode ≤ 80 · token or proxy ≤ 100 · upload ≤ 250 · queue + inference ≤ 1,800 · download ≤ 200 · composite + checks ≤ 30 · paint ≤ 33 ≈ 2,740 ms |

Also reported: warm, post-wake and post-launch states; first partial and place match from key-down; mic-open latency against a reference click; wrong-paint rate; LLM fallback rate. **No target moves after measuring.**

### 8.2 Accuracy endpoints

**Primary (one):**
- **P1 — Rainier summit visible, 24 h lead.**
  - **Outcome:** a daytime frame (Sun ≥ 3° by ephemeris) is "summit visible" when the summit-band landmark's edge ratio against its illumination-conditioned clear-day reference is ≥ τ. Below a lower threshold it is "hidden"; between the two it is "partial", which is excluded from P1 and reported. Camera-in-cloud frames are a separate class, scored as "hidden" in a sensitivity analysis. τ and the partial band are fixed on development data.
  - **Prediction:** the same extractor run on the rendered frame.
  - **Statistic:** **Peirce skill score** (POD − POFD). The primary test is the paired difference ΔPSS = PSS(Snow Globe) − PSS(persistence), with a 95 % CI from a block bootstrap. The block length is set from development-period autocorrelation, at least 1 day.
  - **Persistence** is the stronger of two:
    - (a) the latest frame available at issue time;
    - (b) the frame at valid − 24 h. At 24–29 h leads this frame postdates issue, so it is a conservative baseline, and the PRD says so.
  - **Claim:** made if the CI excludes 0.
  - **MDE:** computed from development data and published before sealing. If the MDE on the prospective window exceeds 0.25, the primary moves to the hindcast held-out season (design H). If H isn't available, P1 is reported as underpowered, plainly.

**Renderer claims (pre-registered with P1):**
- **R1 — non-inferiority.**
  - **Test:** PSS(Snow Globe) ≥ PSS(S) − Δ, with Δ = 0.05, against both forecast-scalar baselines.
  - **S_rule:** "visible" if HRRR ceiling at the summit-ray intersection is above the summit height (datum-corrected) and low + mid cover < 50 %.
  - **S_mos:** a logistic regression on HRRR fields, fitted on development data.
  - **What a pass shows:** against S_rule, faithfulness (the extractor recovers the rule from the render); against S_mos, that rendering keeps up with a fitted statistical forecast.
- **R2 — superiority of geometry.** For Rainier's obscuration height (ordinal landmark bands), ray-intersection sampling beats sampling HRRR at the camera's grid cell. The statistic is ordinal error, paired, with a block bootstrap.
- **R3 — analog.**
  - **Analog distance:** a weighted Sun-position plus forecast-class distance, defined on development data.
  - **Stratum:** a pre-registered "no close analog" stratum, with its n reported.
  - **If the analog is non-inferior everywhere, including that stratum,** the README says the shader buys explainability and coverage, not accuracy.

**Key secondary (not part of the primary claim):**
- **P2 — Point Reyes landmark obscured, 24 h.** The landmark, its distance, its height and the forecast fields mapped to it are named in M2 from the NPS landmarks image, before sealing. It is scored on a summer hindcast season if the archive allows.

**Secondary** (your list plus the council's; Holm-corrected; reported, not claimed):
- cloud fraction in the sky mask;
- the fog contingency table;
- brightness (L\*) and colour (ΔE2000) per region;
- embedding similarity as a diagnostic, with the model named;
- leads of 1 h and 6 h;
- climatology skill.

**The skill-score ratio** (1 − E/E_ref) is reported only over whole periods, never per day.

### 8.3 Extractor validation (before any number is published)

- **Labels.** ~150 real frames per metric, stratified by time of day and weather, plus ~50 renders. 50 frames are re-labelled a week later for intra-rater agreement; a second labeller is used if available.
- **Bars.** Summit and landmark visibility F1 ≥ 0.85. Cloud fraction MAE ≤ 0.10 by day. Fog F1 only with ≥ 30 positives.
- **Failure rule.** A metric that fails its bar is not reported, and the README says so.

### 8.4 Periods and leakage

- **Design H (archive is deep enough).**
  - Development: earlier season(s) within one camera epoch.
  - Held-out: a later season (for Rainier, winter 2025–26 if available; for P2, summer 2026).
  - The prospective window is a confirmation.
  - All fitted assets come only from development frames that precede the held-out season: plates, clear-day references, the camera response, τ, S_mos and analog weights.
  - Only HRRRv4 (Dec 2020 onward) is used.
- **Design P (archive is shallow).**
  - Development: captures from go-ahead to 14 Dec 2026.
  - Held-out: a **sealed prospective window 15 Dec 2026 – 14 Jan 2027 (≥ 28 days)**, extended by any outage.
- **Choosing and committing.** The archive check in M0.5 picks H or P. The split is committed before any held-out frame is touched.
- **Renderer features.** Development data is split into dev-fit and dev-check by alternating weeks. A feature is admitted only if it improves dev-check P1-PSS by ≥ 0.02 (or dev-check obscuration-height error), which keeps feature choice from becoming a garden of forking paths.
- **CI tests.** Leakage tests (a baseline only sees frames available at issue time; held-out frames are absent from the labelling store) and metric unit tests run on every push.

### 8.5 Dashboard

A static page with:
- P1 (with CI, n and base rate) and the R1–R3 results;
- secondaries with Holm-adjusted p-values;
- per-viewpoint strips: yesterday's forecast vs what the camera saw vs persistence;
- latency by stage;
- uptime and exclusions.

It stays private until NPS confirms. `make score` rebuilds it from frozen inputs.

---

## 9. Privacy, Licensing and Cost

### 9.1 Data-flow inventory

| Data | Where it goes | Retention | Disclosure / control | Owner |
|---|---|---|---|---|
| Live audio (on-device engine) | RAM | duration of the query | chip `on-device`; mic indicator | you |
| Live audio (cloud engine) | Deepgram (`mip_opt_out=true`) | provider: "only for the duration necessary to process the request" | chip `→ Deepgram` before the first byte; on-device-only setting | you |
| Transcript text | RAM; Foundation Models (on-device); Haiku only if enabled, after its terms are recorded | query | chip for any cloud text | you |
| Traces | local disk | 30 days rolling | IDs, lengths and parse fields only, never transcript text (replay runs excepted) | you |
| Screen-capture frames (harness) | RAM, overlay window only | discarded; timestamps kept | MEASURING indicator; Screen Recording permission disclosed; debug dumps local, 7 days | you |
| Hotkey event tap (harness) | RAM | discarded | hotkey timestamps only; compiled out of release | you |
| Replay fixtures (your voice) | local, outside iCloud and git; Deepgram only with your approval | privately until **M5 + 12 months** so README numbers stay traceable (or the README says they were deleted) | REC indicator that switches itself off; opt-in | you |
| Fixture transcripts and labels | git | permanent | committed text only | you |
| Backend log | backend DB | 90 days | install_id (pseudonymous, disclosed), viewpoint_id, valid_time, timings, check results; no text; no IP stored by our code; host access-log retention recorded once the host is chosen; first-run notice | you |
| Tier 1 frame sent for Tier 2 | fal (request history off, minimal CDN lifetime; provider application logs exist) or Runware | provider terms | "AI-refined" caption; burned-in label on export | you |
| Unverified-place text and image | Open-Meteo geocoding; named image provider | provider terms (recorded before shipping) | chip; burned-in label; IPTC metadata | you |
| Camera frames (may show people) | private storage ≤ 1920 px | **12 months after M5**, except plate sources and published examples | published only after NPS confirmation, ≤ 1280 px, people and vehicles blurred, reviewed; Rainier (busy visitor area) reviewed most carefully | you |
| Plates, depth, masks | private storage; app cache | as frames | not public until confirmed | you |
| Forecast JSON | private storage; app | life of the project | NOAA credit; Open-Meteo link where used | you |
| Provider keys | backend environment only | — | the app holds none (short-lived STT token only) | you |
| Demo recording | README | permanent | shows only content cleared for publication | you |

**Takedown:**
- **Channel:** a GitHub issue template plus a contact of your choosing (§13).
- **Response time:** within 7 days.
- **Copies deleted:** storage, CDN, dashboard and the Tier 0 index; local caches purge when the index changes.

### 9.2 Polite collection
- **Live capture:** poll the JSON; fetch the image only when `imagedate` changes; at most 4 images per hour per camera.
- **Archive backfill:** one request at a time, ≥ 5 s apart, resumable, never downloading twice, with a total size cap.
- **Identification:** the User-Agent names the project and its repo URL; backoff on errors; robots.txt honoured (NPS disallows only `/ns/`, `/search/` and `loader.cfm`).

### 9.3 Licence ledger (→ generated `THIRD_PARTY_NOTICES`)

| Item | Licence / basis | Duty |
|---|---|---|
| NPS camera frames | Generally public domain (NPS disclaimer); air-cam EXIF names NPS; written confirmation requested | "Webcam imagery courtesy National Park Service. Derived images are not NPS products." No implied endorsement. |
| HRRR, GOES-18, METAR, NBM (if used) | US government, public domain | "Forecast data: NOAA HRRR via NOAA Open Data Dissemination"; GOES-18/NOAA credited where used as truth |
| Open-Meteo | CC BY 4.0; free API non-commercial | Link next to any display; mark "modified" |
| Hosek–Wilkie data tables | **BSD-3** (Hosek & Wilkie 2012–13) via `sun-sky` (Unlicense) | Keep the notice in source and bundle |
| oculus-core | MIT | Notice inside the WASM bundle |
| Parakeet weights (if used) | CC BY 4.0 | "NVIDIA Parakeet TDT v2, CC BY 4.0" |
| Z-Image-Turbo | Apache-2.0 | Notice; disclose AI refinement |
| Depth and embedding models, DEM | Named in M2; non-commercial weights excluded | Per licence |

### 9.4 Cost (development cap $25)

| Item | Estimate | Hard cap in code |
|---|---|---|
| Deepgram STT benchmark (covered by sign-up credit) | ≈ $0.20–2 | $3 |
| Tier 2 kill test | ≤ $1 | $1 |
| Tier 2 benchmark and false-accept suite (if the kill test passes) | ≈ $5–8 | $10 |
| Tier 2 during development and eval sample | ≈ $3–5 | $6 |
| Haiku (only if FM fails) | ≈ $0.05–0.50 | $1 |
| Unverified-place generation | ≈ $0.50 | $1 |
| Hosting and storage | $0 on free tiers (§13) | $2 |
| **Total** | **≈ $10–17** | **$24** |

Ask before the first paid call to each vendor; hard caps in the proxy; ledger in DECISIONS.md.

---

## 10. Risks and Mitigation

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| STT final alone exceeds the Tier 0 chain when idle | High | High | Idle-state engine choice; partial-based paint under a 2 % wrong-paint ceiling; report the miss and the bottleneck |
| Tier 2 within 3 s for < 80 % of queries | High | Medium | Kill test first; one request in flight; Tier 1 as the floor; ships off by default if it fails |
| NPS never confirms | Medium | High | Email this week; publish numbers-only fallback; owner signs off the public list |
| P1 underpowered | Medium | High | MDE published before sealing; design H; plain "underpowered" report |
| Extractor fails validation | Medium | High | Bars set in advance; the metric is withheld if it fails |
| Golden Gate permission not granted by 4 Dec | High | Low | Golden Gate moves to v2 |
| Analog beats the shader | Medium | Medium | Pre-registered R3; the README states it |
| Screen Recording re-consent prompt stalls an overnight run | Medium | Medium | Checked in the spike; harness-only signed identity |
| `tauri-nspanel` private API breaks | Medium | Medium | Own NSPanel fallback |
| Capture or worker outage | Medium | High | Archive backfill; uptime log; period extended |
| HRRR retired mid-evaluation | Low | Medium | Source adapter; labelled break |
| Spend overrun | Low | Medium | Hard caps; ask first |
| Scope creep | High | High | MSV; cut order; 25 % lateness rule |

---

## 11. Cut List (not in v1)

Mobile, accounts, payments, Windows, Linux. Accuracy claims outside the curated set. Archive view of past times. Fn hotkey. Video. User-supplied viewpoints. Multiple languages. The fog post-processor. The factored time-lapse basis. NBM inputs (recoverable later from the AWS archive). Speculative Tier 2. Night scoring. The full oracle ladder and retrieval-rank metric (post-MSV).

---

## 12. The Portfolio Story (one page for a reviewer)

**Problem:** You can see a forecast as numbers, or a place now through a webcam. You can't see what a real place will look like at a future hour, and image generators that claim to show it invent the weather.

**What it is:** A push-to-talk macOS overlay. Ask "is Rainier out tomorrow at 4?" and within a second see that view relit for the true sun and HRRR's forecast sky, with each cloud pixel traceable to a grid cell.

**Draft headline (blanks filled at M4):**
> *On a pre-registered, sealed __-day period (base rate: summit visible __ % of daytime frames; climatology PSS __), Snow Globe's 24-hour visual forecast of Mount Rainier's summit scored a Peirce skill score of __ vs __ for persistence (Δ __, 95 % CI __ to __). The skill is NOAA HRRR's; rendering it lost none (non-inferiority margin 0.05: __), and sampling the forecast along each view ray beat the camera's own grid cell at predicting cloud height (__).*
> *Latency, after 30 min idle: first forecast-driven frame __ ms p95 from key release, measured at the composited pixel.*

**Status:** M0 complete when you approve this document.

---

## 13. Open Questions (for the owner)

Each has my recommendation; "yes to all recommendations" is a valid answer.

1. **Viewpoints.** Approve Rainier (hero), Point Reyes (fog) and Grand Canyon (third, post-MSV) in place of Golden Gate? Will you contact Lawrence Hall of Science ("The View") and ALERTCalifornia for Golden Gate, with a 4 Dec cut-off? *Rec: yes; I draft the emails.*
2. **Calendar.** Hours per week, and an MSV date? Accept the 25 %-late rule? *Rec: give me a number and I set milestone dates in PLAN.md.*
3. **Go-ahead for M0.5 now:** the forecast worker and polite capture of all three cameras, before the rest of M1, plus the NPS archive check. *Rec: yes; calendar time is the scarcest input.*
4. **Hosting.** Which accounts do you have, or will you create (free tiers)? Options:
   - GitHub Actions (free only if the repo is public; jitter is fine given archive backfill) for the worker and batch jobs;
   - private object storage for frames (Cloudflare R2 or Backblaze B2 free tier);
   - a never-sleeping proxy or token minter (Cloudflare Workers, or similar).
   Is the repo public? *Rec: public repo; R2 + Workers + Actions.*
5. **Paid vendors** (nothing is called until you say so): Deepgram (account; $200 credit; your replay recordings sent with `mip_opt_out`), fal and Runware (Tier 2 kill test ≤ $1, then ≤ $10), Haiku only if Foundation Models fails. *Rec: approve Deepgram and fal now, Runware and Haiku later.*
6. **Your time and machine.**
   - Record ~130 scripted utterances (~20 min) plus ≥ 30 real-use ones.
   - Label ~150 frames per metric plus 50 re-labels.
   - Find someone else for the Tier 2 preference test.
   - Leave the laptop running idle-latency trials overnight under `caffeinate` (several nights).
   - Grant permissions: Microphone, Speech Recognition, Screen Recording and Input Monitoring (harness only), Apple Intelligence on.
   *Rec: yes; fixtures kept privately until M5 + 12 months.*
7. **Architecture changes C1–C9** (§7.3). *Rec: approve all except C7 (on-device Tier 2), which I'd leave out unless the kill test fails.*
8. **Tier 0 on screen** when Tier 1 is ready within ~100 ms of it: show both (a brief flicker), or skip Tier 0 in that case? *Rec: skip; Tier 0 still runs and is measured.*
9. **Your requirements that sit last in the cut order:** the LLM fallback and the unverified-place fallback. Keep them (minimal, after the MSV), or drop them? *Rec: keep the LLM fallback (Foundation Models, $0); drop unverified generation, because it is exactly the "invented weather" image the project argues against.*
10. **NPS confirmation and takedown contact.** Will you send the NPS emails (I draft them), and which contact address should the README list for takedown requests? *Rec: a project-specific address, not your personal one.*
11. **macOS.** Stay on 26.5.x through M4, even though 27 is out? *Rec: yes.*

---

## Appendix A — Council review

Two rounds, four judges, each reading this document and `docs/research/` independently; I answered round 1 in [docs/council/round1-responses.md](docs/council/round1-responses.md) and each judge followed up. Summary of what was raised, what changed, and what I rejected.

### A.1 Latency engineer
**Raised:**
- t0 and "paint" were measured at the wrong points: rAF fires 1–3 vsyncs before composite, and a one-time calibration is invalid.
- The harness would measure its own cache, since passes 2–10 hit it.
- p95 over 15 utterances is just the worst one.
- "Warm" was a benchmark trick; real push-to-talk is idle.
- Speculation and cancellation had no correctness contract.
- The Tier 2 budget omitted half its stages and ignored fal's 2-concurrent-request cap and best-effort cancel.
- The 200 ms STT allocation contradicted the research.

**Round 2:**
- Inject keys through the real hotkey path.
- Swap samples inside the live audio callback.
- Draw the paint tag in the content surface.
- Add a 2 % wrong-paint ceiling.
- Push the pad as a burst.
- Run ≥ 60 idle trials.
- Mint the STT token in parallel with opening the mic.
- Cap the proxy's added p95.
- Run a cancel microbenchmark.
- Measure pre-render contention.

**Adopted:** all of it (§3.1, §3.2, §6.1, §6.2, §6.13, §6.14, §8.1).

### A.2 Graphics/ML researcher
**Raised:**
- **Clouds:** the claim that clouds "pack toward the horizon" through perspective is false for a thin layer, and mip-filtering biases coverage with distance.
- **Measurement:** the evaluation would measure HRRR, the camera model and the render-vs-photo gap, not the renderer.
- **Baselines and metrics:** climatology, forecast-scalar and analog baselines were missing; region means and embedding cosine are gameable.
- **Fog truth:** single-β Koschmieder breaks on slant paths and censoring.
- **Tier 2 checks:** circular, and calibrated on false rejects instead of false accepts.
- **Power:** one December week can't support a claim.
- **Answer to it:** a hindcast on archived data.

**Round 2:**
- Non-inferiority needs a pre-set Δ and defined scalars, plus one superiority test where geometry should matter.
- Use the Peirce skill score rather than proportion correct.
- Define the outcome classes and the persistence baseline precisely.
- Set the block length from autocorrelation.
- Close the hindcast leakage paths: fitted assets, archived availability times, HRRRv4, the labelling page.
- Use a model-free camera response (Kim & Pollefeys).
- Score the displayed seed (seed averaging retracted).
- Define day by ephemeris.
- Use dev-internal splits for feature admission.

**Adopted:** all of it (§4.2, §4.3, §6.5, §6.6, §8.2, §8.4).

### A.3 Skeptical hiring manager
**Raised:**
- Three projects stapled together.
- The headline number arrives last.
- The estimates aren't credible, and M2 is where this stalls.
- No ranked priorities or cut order.
- A null by construction.
- Point Reyes kills the demo.
- Tier 0's 300 ms is a hollow headline, because the fastest frame shows the wrong weather.

**Round 2:**
- Set dates.
- Walking skeleton ≤ 3 weeks after go-ahead.
- Golden Gate cut-off 4 Dec.
- A $1 Tier 2 kill test before building anything for it.
- Physics fixes as a gated backlog.
- Credit HRRR for the skill.
- Put the base rate in the headline.
- Check Rainier's EXIF now. Done: no ownership metadata on the park camera; NPS artist/copyright on the air cams.

**Adopted:**
- MSV, cut order and the 25 % rule (§5).
- Walking skeleton.
- Rainier hero.
- 4 Dec cut-off.
- Kill test.
- Gated backlog.
- Revised headline (§12).
- The 60-second demo.
- First-forecast-driven-frame metric.
- PLAN/PRD contradictions fixed.

### A.4 Privacy and licensing reviewer
**Raised:**
- No data inventory.
- Transcripts with no defined home.
- A cloud STT key in the app.
- A fallback that could upload after the chip said `on-device`.
- Public domain is not privacy clearance (people on the beach).
- Licence claims stronger than their sources: Hosek–Wilkie tables are BSD-3; Soniox keeps diagnostic metadata; Open-Meteo requires a link and "modified".
- Generated images of real places lose their captions once exported.

**Round 2:**
- Keep fixtures until a stated date, so numbers stay traceable.
- Disclose install_id and host logs.
- No cloud primary without a token API.
- Rules for screen capture and the event tap.
- The NPS request covers all cameras and the archive, has a deadline and a numbers-only fallback, and "private" includes the app.
- Archive backfill budget.
- FLUX.2 control on fal, not BFL.

**Adopted:** all of it (§3.5, §6.2, §6.13, §9).

### A.5 Rejected or modified, and why

| Proposal | Judge | Decision | Reason |
|---|---|---|---|
| Cut the LLM and unverified fallbacks entirely | HM | **Modified:** last in the cut order; you decide (§13 Q9) | Both are in your brief |
| Make Tier 0 only a baseline | HM | **Modified:** kept on screen, plus the forecast-driven-frame headline; you decide on skipping it (§13 Q8) | Tier 0 is in your brief |
| Defer per-query paint capture to after the MSV; ~40 idle trials | HM | **Rejected:** per-query capture kept; ≥ 100 idle trials for the shipped engine, ≥ 60 for the other | Latency is your first priority, and a one-time calibration was shown to be invalid. Two judges set the trial floor. |
| Defer Holm correction and the analog baseline | HM | **Rejected** | Holm costs nothing; dropping the strongest competitor would look like hiding it |
| Defer the oracle ladder, retrieval rank, ordinal fog bins, fog-top height | HM | **Accepted** | Not needed for P1 |
| Drop FLUX.2 klein entirely | GR | **Modified:** a ~20-call illustration on fal, clearly not a rate | Lets the README show, not assert, why reference editors fail |
| Full censored-likelihood β fit as primary fog truth | GR | **Modified:** binary per-landmark visibility is primary; continuous fit post-MSV | Same information for P1 at a fraction of the work |
| Drop the third viewpoint | HM | **Modified:** captured from day one, built after the MSV | Calendar time can't be bought back |
| Seed averaging | GR (round 1) | **Withdrawn by GR in round 2:** score the displayed seed | It was a hedge |

---

*End of document.*
