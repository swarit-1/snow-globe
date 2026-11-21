# Snow Globe: Product Requirements Document

> **Changelog:** v0.4.2, 21 Nov 2026 (UTC).
> - **The product is renamed Snow Globe** (your decision of 21 Nov; DECISIONS.md D-080). That changes the title, the version and name lines, §0, §1 row 9's call, §2, §8, §12's tree, §13.4's R1 and §21's headline. "The Lookout draft" still names your draft of 18 Nov, which v0.4 merged. Where a bare "Lookout" meant that draft (§0, §1, §18.2, Appendices B and D), it now says so.
> - The spec names no package, command, environment variable, path or User-Agent string under the old name, so nothing else changes here. The naming map for the code is in D-080.
> - No target, gate or cap changed.
>
> **Changelog:** v0.4.1, 19–20 Nov 2026 (UTC).
> - **Factual corrections:** the status, name and companion-docs lines; §0's "Nothing else has been measured"; the §1 calls marked accepted; the oculus-core pin (§2.4, §8.4); Herbie (§8.3, §11.3); the HRRR lag range and Rainier's measured cadence (§8.1); the National Mall (§8.2); G+25 (§16); pointers from the cost lines of §11.3 and §17 to the newer estimate (D-035); Appendix B.
> - **Factual corrections, 20 Nov (second pass):** the cost pointers of §11.3 and §17 now name the infra estimate of 20 Nov (D-060), which replaces the one D-035 quoted.
> - **Factual corrections, 20 Nov (third pass):** the same pointers add the estimate for capture 0.2.1, whose polls are spaced from the previous answer (D-065, D-066).
> - **Two spec updates that record decisions:** §8.7 storage (D-028), and Appendix A, redrawn to the shape of the committed manifest schema (D-044; to be confirmed, or reverted to v0.4's sketch).
> - No latency, accuracy or cost target changed, and the $3 hosting cap stands.
>
> **Version:** v0.4.2: v0.4.1 with the product renamed. v0.4 is a synthesis of two drafts:
> - your **Lookout** draft (18 Nov 2026);
> - **Snow Globe PRD v0.3** (Milestone 0, two council rounds, commit `d0ba83d`).
>
> **Status:** M0.5 in progress since 19 Nov 2026. You accepted every default in [§1](#1-decisions-to-confirm) that day (DECISIONS.md D-012). Progress and measured numbers are in [PRD_PROGRESS.md](PRD_PROGRESS.md).
>
> **Name:** Snow Globe, your rename of 21 Nov 2026 (D-080), which supersedes "Lookout", accepted on 19 Nov (D-016). The repo was already `snow-globe`, so its name now matches the product's.
>
> **Date:** 19 Nov 2026. **Owner:** Swarit Srivastava.
>
> **Companion docs:**
> - [PLAN.md](PLAN.md), [DECISIONS.md](DECISIONS.md) and [PRD_PROGRESS.md](PRD_PROGRESS.md) follow v0.4; they were updated on 19 Nov, after §1 was set.
> - [docs/research/](docs/research/) holds the sourced landscape notes.
> - [docs/council/](docs/council/) is the council record.
> - [docs/review/prd-crosscheck.md](docs/review/prd-crosscheck.md) lists every disagreement between the two drafts and how it was settled.

---

## 0. TL;DR

Snow Globe shows what a place will actually look like at a time you say out loud.
1. Hold a hotkey.
2. Ask, for example, "is Rainier out tomorrow at 4?". "Show me Golden Gate at 9" works once a Golden Gate camera is licensed.
3. Release.

An overlay then shows that real view at that hour, built from NOAA's forecast and the true positions of the Sun and Moon.

It is a visual forecast, not an image generator. Sun and Moon, cloud layers, cloud base, fog, precipitation and night lighting all come from data for that place and hour. An image model may add photographic texture. By construction it cannot touch the sky or anything hidden by haze, and a validated check rejects it if it tries.

**The project has to earn three claims:**
- **Fast.** Latency is the p95 from the OS key-up event to the first composited frame, after 30 minutes idle:
  - **Tier 0**, a real photo: ≤ 300 ms.
  - **Tier 1**, the relit frame: ≤ 1 s, with a stretch target of 400 ms.
  - **Tier 2**, the photographic frame: ≤ 3 s, reported as the share of queries delivered within 3 s.
- **Explainable.** Each frame comes from three stages that can be described and tested separately: retrieval, physically based relighting, and guarded refinement. The same renderer runs in the overlay, the scrub view and headless evaluation.
- **Checked against reality, pre-registered.**
  - **Primary claim:** on a sealed period, the 24-hour visual forecast of whether Mount Rainier's summit is visible beats persistence on the **Peirce skill score**.
  - **Credit:** the forecast skill is NOAA's, and the README says so.
  - **The renderer's own claims:** it loses none of that skill, and sampling the forecast along each view ray predicts cloud height better than the camera's own grid cell does.

**What the cross-check changed.** Both drafts had errors; the full record is in [docs/review/prd-crosscheck.md](docs/review/prd-crosscheck.md).
- **HRRR does not end on 30 Nov; Open-Meteo's copy of it does.**
  - Forecasts are read from NOAA's AWS bucket.
  - Its archive goes back to 2014, so evaluation can start on day one, with exact run times and the cloud-base and ceiling fields that Open-Meteo lacks.
- **Forecast visibility is not haze along the sight line.** It is a near-surface fog diagnostic, so clear-air haze is calibrated per viewpoint instead (§9.3).
- **The overlay can't both refuse focus and take plain arrow keys.** §5.4 sets the focus model.
- **The demo needs a camera.** The headline demo is Rainier unless Golden Gate permission arrives by **4 Dec 2026**.
- **Oculus's AWS stack costs ~$60 a month.** Snow Globe reuses only its serverless-compatible parts.
- **From the Lookout draft:**
  - the Hillaire 2020 atmosphere replaces Hosek–Wilkie, because twilight is when people ask;
  - the decisions table, the design section, the time-rule table, routes and jobs, graded asks, and capture of up to eight cameras.
- **From v0.3:**
  - latency measured at the composited pixel, after idle;
  - one pre-registered endpoint;
  - clouds intersected along view rays;
  - Tier 2 that cannot change the sky;
  - the data-flow inventory and licence ledger.

**Measured so far** (this machine, 18–19 Nov 2026):
- oculus-core matches JPL Horizons to 0.19′ for the Sun and 0.13′ for the Moon, worst case.
- The NPS feeds are live: Point Reyes serves 3840×2160 frames (1.86 MB) and the Rainier park camera serves 1920×1080 frames (577 KB).
- Open-Meteo's docs list HRRR as "Discontinued 2026-11-30".

Nothing else had been measured when v0.4 was written. Every number since is in PRD_PROGRESS.md.

---

## 1. Decisions to confirm

These choices were assumptions, not decisions, until you set them. Each row has a recommended default; "yes to all defaults" is a valid answer. Rows 1, 2, 3, 5, 6 and 9 come from your Lookout draft. Row 4 is new, from the cross-check, and the rest come from v0.3's open questions.

**You accepted every default on 19 Nov 2026** ("yes to all defaults", DECISIONS.md D-012).

| # | Decision | Recommended default | What changes if you pick otherwise | Your call |
|---|---|---|---|---|
| 1 | Platform | macOS 26.5.x on Apple silicon, held through M4; Windows later. | Speech and overlay code are platform-specific; the renderer, parser and backend are not. Upgrading to macOS 27 mid-milestone breaks latency comparisons. | ☑ Accepted, 19 Nov |
| 2 | Shell and renderer | Tauri 2 shell, one WebGL2 renderer. Swift as an **in-process static library** (C ABI) for SpeechAnalyzer and Foundation Models. | A native Swift + Metal app integrates better with macOS, but evaluation would need a second renderer. A separate Swift helper process loses Foundation Models, which must run in the UI process. | ☑ Accepted, 19 Nov |
| 3 | Evaluation viewpoints | National Park Service cameras.<br>• The MSV makes claims on **Mount Rainier** (Paradise park camera) and **Point Reyes**.<br>• Grand Canyon is third.<br>• Up to 8 NPS cameras are captured from M0.5.<br>• Golden Gate only with written permission by **4 Dec 2026**. | A Bay Area or Austin-only set has no licence-clean camera and no archive, so accuracy numbers wait weeks. | ☑ Accepted, 19 Nov |
| 4 | Size of v1 | The MSV (2 viewpoints, sealed P1) first, then grow toward 8 verified viewpoints. | Starting with 8 costs an estimated 1.5–2 days per viewpoint (plate, pose, landmarks, labels) before the sealed window, and that window can't move. | ☑ Accepted, 19 Nov |
| 5 | Compute and budget | Hosted, pay-per-call inference; a $25 development cap with hard caps in code totalling $24. | With your own GPU, Tier 2 can be self-hosted and the cap goes away. | ☑ Accepted, 19 Nov |
| 6 | Speech privacy | On-device by default (SpeechAnalyzer `DictationTranscriber`).<br>• A cloud engine (Deepgram Nova-3) runs in the replay bake-off.<br>• In the product it is off by default, behind a visible toggle. | Cloud-first is simpler but sends audio off the machine by default. If cloud is much faster, you decide with the numbers in hand. | ☑ Accepted, 19 Nov |
| 7 | Paid vendors | **Now:** Deepgram for the bake-off ($200 sign-up credit, training opt-out on every request) and fal for the Tier 2 kill test (≤ $1).<br>**Later:** Runware, Claude Haiku (only if Foundation Models fails), and the ungrounded-generation baseline (≤ $1). | Nothing is called until you approve that vendor. | ☑ Accepted, 19 Nov |
| 8 | Hosting | AWS serverless:<br>• Lambda, EventBridge Scheduler, S3, CloudFront and a budget alarm, built from Oculus's `bootstrap`, `storage`, `cdn` and `monitoring` Terraform.<br>• Whatever hosts the Tier 2 proxy must add ≤ 100 ms p95 when idle; Lambda and a Cloudflare Worker are both measured.<br>• GitHub Actions runs CI only.<br>• The repo is public. | • Oculus's full stack (ALB, Fargate, Redis) costs ~$60 a month, by Oculus's own estimate.<br>• GitHub Actions as the scheduler adds jitter, and scheduled workflows in a public repo are disabled after 60 days without activity, which is risky over a holiday sealed window. | ☑ Accepted, 19 Nov |
| 9 | Name | Lookout. | The repo, bundle id and overlay wordmark follow the name. | ☑ Accepted, 19 Nov; renamed **Snow Globe** on 21 Nov (D-080) |
| 10 | Fallbacks | Keep the LLM parser fallback (Foundation Models, on device, $0). Unverified places move to Later. | Unverified generation is exactly the "invented weather" image this project argues against. | ☑ Accepted, 19 Nov |
| 11 | Tier 0 when Tier 1 is close behind | If Tier 1 is ready within ~100 ms, skip painting Tier 0. It still runs and is measured. | Showing both causes a brief flicker. | ☑ Accepted, 19 Nov |
| 12 | Calendar | Tell me your hours per week; I assume ~25. A milestone more than 25 % late loses the next item on the cut list. | Dates in §16 move. | ☑ Accepted, 19 Nov |
| 13 | Start the clock | Go-ahead for M0.5 now: forecast worker, camera capture, NPS archive check and one backtest pair. | Every day of delay is a day less of archive and live data. | ☑ Accepted, 19 Nov |
| 14 | Your time | • ~20 min recording scripted utterances, plus ≥ 30 real-use ones.<br>• ~150 frames labelled per metric, plus 50 re-labels.<br>• Someone else to judge the Tier 2 preference test.<br>• Several nights of unattended idle trials.<br>• Permissions: Microphone and Speech Recognition; Screen Recording and Input Monitoring for the harness only; Apple Intelligence on. | Without the recordings and labels, the latency and accuracy numbers can't be published. | ☑ Accepted, 19 Nov |
| 15 | Emails and contact | You send the NPS confirmation emails (I draft them). The README lists a project-specific takedown address. | Without NPS confirmation, the public repo carries numbers only: no frames, plates or renders (§15). | ☑ Accepted, 19 Nov |

---

## 2. Product vision and positioning

Snow Globe answers a question no current tool answers directly: what will this exact view look like at that exact time?

| What exists | What it gives you | What is missing |
|---|---|---|
| Live webcams | The real view right now | Nothing about later. Feeds go down, and many switch to infrared at night. |
| Sun and moon planners | Where the Sun and Moon will be | No weather, and no picture of the scene. |
| Weather apps and forecast maps (Windy, Ventusky, Apple Weather) | Numbers, icons and maps for the hour | No view of a particular place. |
| Text-to-image models | A plausible picture of the place | Light, cloud and fog are invented and can't be checked. |
| Relighting from a fixed camera's time-lapse (Sunkavalli et al. 2007; Lalonde et al. 2009) | A relighting basis from the camera's history | No forecast and no product. |
| Analog ensembles (Delle Monache et al. 2013) | A forecast from similar past situations | No pictures, and no rendering of new situations. |
| **Sigg, Cavallaro, Günther and Oswald, 2022** | **A GAN that turns a webcam image into its forecast view** | **A research method trained on archives from three Swiss cameras, at 64×128 px. No product around it and no latency goal.** |

**Sigg et al. is the closest prior work** (*AIES* 2022, [arXiv 2203.15601](https://arxiv.org/abs/2203.15601)).
- Human raters did "not much better than if they guessed randomly" at telling its images from real ones.
- The images matched the COSMO-1 forecast's conditions in "at least 89 % of the examined cases".

That shows the idea is sound. Its agreement was scored against the *forecast's* conditions. Snow Globe's primary score is agreement with the frame the camera *actually recorded* at the valid time.

### 2.1 What Snow Globe adds
1. A voice interface whose first frame arrives within a third of a second, measured at the composited pixel.
2. Physics-first rendering from one plate set per viewpoint, so a new viewpoint needs no training.
3. A score for every prediction against the frame the camera actually recorded, pre-registered against persistence, climatology and analog baselines.

### 2.2 Rules the product keeps
- Data decides. The image model only adds texture, and it can't reach the sky or hazed regions.
- Every frame is labelled simulated, real or live, with its valid time and the forecast's issue time.
- Real beats simulated. "Now" shows the live camera when one exists.
- The skill is NOAA's. Snow Globe renders the forecast faithfully, and shows how often it is wrong and why.
- A measured number outranks a new feature.

### 2.3 What it is not
- Not an image generator.
- Not a webcam viewer: cameras supply truth and plates.
- Not a better weather forecast.
- Not global: accuracy claims cover only camera-verified viewpoints.

### 2.4 Starting point (inspected 18 Nov 2026)

| Item | State | Consequence |
|---|---|---|
| Machine | MacBook Air M4 (10-core GPU), 16 GB, **fanless**, 60 Hz | Thermal state is recorded with every latency trace; one vsync is 16.7 ms. |
| OS and tools | macOS 26.5.1; Xcode 26.2; Rust 1.98.1 with wasm32; Node 24; Python 3.14 with uv; Swift 6.2.3 | The SpeechAnalyzer and Foundation Models interfaces are in the SDK. The Tauri CLI is installed in M1. |
| Time zone | America/Chicago | The viewpoints are in Los Angeles and Phoenix time (Phoenix has no DST), which gives two real cross-zone tests. |
| oculus-core | `github.com/swarit-1/oculus` at `06809e6`, MIT; re-pinned on 19 Nov to the public `1b5410b`, which has the same ephemeris code (D-025). Runs in Rust, WASM and Python. | It is reused, not copied (§8.4). |
| This repo | M0 documents only, on branch `m0-prd` | No app code until §1 is set. |

---

## 3. Users, core loop and hooks

Version 1 has one user, you, plus anyone watching a 60-second demo. Wider users matter only if a startup path opens after the accuracy results are in.

| User | What they ask | Stage |
|---|---|---|
| You and a demo audience | "Is Rainier out tomorrow at 4?", then a follow-up | v1 |
| Photographers scouting light and fog | "Will the headland be in fog at blue hour Saturday?" | After v1 |
| Travelers | "Is the viewpoint worth the drive tomorrow morning?" | After v1 |

### 3.1 Core loop
1. Hold the hotkey and say a place and a time.
2. Release. A real photo appears, then a relit frame, then a photographic frame.
3. Scrub through the day, or ask a follow-up without repeating the place.

### 3.2 Hooks
- **Scrub.** The arrow keys move the valid time, and Tier 1 follows at 60 frames a second.
- **Live beside simulated.** One key shows the camera's current frame next to the forecast frame.
- **Graded asks.** Each ask about a future time is logged as a prediction. When that time arrives, the dashboard shows the real frame beside it.

### 3.3 Example utterances

| You say | It resolves to |
|---|---|
| "Is Rainier out tomorrow at 4?" | 4:00 PM tomorrow at Paradise, Pacific time, with a chip to flip AM/PM. |
| "Show me Rainier at 9" | The next 9:00 or 21:00 at the place, with an AM/PM chip. |
| "Point Reyes at sunset Friday" | Sunset on Friday, computed for the headland. |
| "Grand Canyon golden hour" | The next evening golden hour at Yavapai Point. |
| "What about midnight?" | Midnight at the last place asked about. |
| "Point Reyes now" | The live camera frame beside a simulated frame for the current hour. |
| "Golden Gate at 9" | Works only if the Golden Gate camera is licensed by 4 Dec. Otherwise: **No match**, plus the three closest known places. |

---

## 4. Scope

### 4.1 Minimum Shippable Version (MSV)
- Push-to-talk, with Tier 0 and Tier 1 on **Rainier and Point Reyes**.
- Latency measured after idle with the paint-capture harness.
- P1 pre-registered and scored on the sealed period, against persistence, climatology, forecast-scalar and analog baselines.
- A validated extractor, the data-flow inventory, the licence ledger, and a README with the numbers.

**Nothing outside the MSV starts until it is published**, except the capture and forecast jobs, which start first.

### 4.2 Version 1
- The MSV, plus:
  - Tier 2 if its kill test passes;
  - the scrub view and the live-beside-simulated view;
  - the LLM parser fallback;
  - the graded-asks dashboard.
- Then growth toward **8 verified viewpoints** with ≥ 14 days of live data. Each viewpoint passes the same gates.
- A README whose every number comes from a file and a command.

### 4.3 Later

| Item | Why it waits |
|---|---|
| Learned local correction of the forecast (a fog post-processor) | Needs the backtest pairs first, and it adjusts the weather, so it needs your sign-off. |
| Unverified places, labelled as such | The thin-wrapper path; it adds no measured claim. |
| Past times, and dates beyond 7 days | Needs reanalysis and climatology modes. |
| "What if it were clear" overrides | Must be labelled hypothetical; adds parser and caption work. |
| Fn-key trigger | Needs an event tap with Input Monitoring or Accessibility. `NSEvent` monitor crashes are reported on macOS 26. |
| Windows, Linux, mobile | Platform work with no new result. |
| Stars and skyglow from Oculus | Visible only on clear nights at dark sites. |
| Night scoring | Infrared frames need their own truth. |
| NBM and LAMP inputs (and a LAMP baseline at 1 h) | Recoverable later from archives. |
| Sun study for interiors | The likely startup wedge, and a different product. |

### 4.4 Non-goals
- A general image generator or a weather app.
- Accuracy claims for any place without a camera.
- Accounts, payments, or a hosted multi-user service.
- Training a new image model.

---

## 5. Design

The overlay is one floating panel that never steals the app you are typing in. The default look reuses the Oculus instrument direction so the two projects read as a set: near-black panel, hairline rules, mono numerals, and colour only in the image.

### 5.1 Overlay anatomy

| Part | Content |
|---|---|
| Listening pill | Waveform and the live partial transcript while the key is held. |
| Chips, top left | Place and viewpoint; valid time with zone and date; an AM/PM flip when the hour was ambiguous; the **destination chip** (`on-device`, or `→ <provider>` whenever audio or text leaves the machine). |
| Image area | The camera's aspect ratio (16:9 for every candidate), so landmarks the caption names are never cropped. Tiers cross-fade in place. |
| Tier marks, top right | Three marks that fill as tiers arrive. A hollow third mark means Tier 2 was rejected or late. |
| Caption, bottom | The label line in §5.3. |
| Scrub bar | Appears on arrow keys or hover, spanning 24 hours around the valid time (or the golden-hour interval). |

### 5.2 States

```
idle ──key-down──▶ listening ──key-up──▶ resolving ──▶ tier0 ──▶ tier1 ──▶ tier2
  ▲                    │ Esc / key-up with no speech                 │ any state: Esc → idle
  └────────────────────┴──────────── new key-down cancels everything ┘
```

| State | Trigger | What shows |
|---|---|---|
| Listening | Key down; the panel is revealed now, so wake-up happens during speech | Pill with partial transcript |
| Resolving | Key up | Chips, then Tier 0 |
| Relit | Tier 1 ready | Tier 1 frame and caption |
| Refined | Tier 2 passes its checks | Tier 2 frame |
| No match | Place not recognised | The transcript and the three closest known places, never a guess |
| Out of range | Time beyond 7 days, or in the past | The horizon and an offer of the latest valid time |
| Offline | No network | Cached tiers with a "forecast from" timestamp |

### 5.3 Caption

The caption is the honesty layer, and every frame has one.

```
SIMULATED · Fri 20 Nov, 4:00 PM PST · cloud low 80% · ceiling 2,300 m · visibility 12 km
  HRRR 18Z Thu, issued 11:27 AM PST (lead 28 h 33 min) · Sun 3.1° · Moon 41% lit, below horizon
LIVE · Mount Rainier, Paradise camera · 3:52 PM PST (8 min old) · Courtesy National Park Service
REAL PHOTO, CLOSEST MATCH · taken 14 Oct, 5:45 PM PDT · matched on Sun position and forecast class
SIMULATED + AI TEXTURE · … · sky and haze unchanged from the simulation
```

- **"Issued"** means the time the forecast run became available, not its nominal run start. Lead time is the valid time minus that availability time.
- The second line folds away and opens on hover.
- **Credits** follow the licence ledger (§15.4): NOAA, NPS, and Open-Meteo with a link where its data is shown.

### 5.4 Controls and the focus model

The panel is **non-activating**: showing it never takes keyboard focus from the app you are in. So:

| Key | Action | When it works |
|---|---|---|
| Hold hotkey | Listen; release to ask | Always (global shortcut; not Option-only, which macOS 15+ rejects) |
| Esc | Dismiss, or cancel while listening | While the overlay is visible (registered globally only then) |
| Left / Right | Scrub one hour; with Shift, ten minutes | After you click the panel |
| M | Flip AM and PM | After you click the panel |
| L | Live camera beside the simulated frame | After you click the panel |
| C | Copy the image with its label burned in | After you click the panel |

- Clicking the panel makes it the key window **without activating the app**, the Spotlight-style behaviour, and Esc hands focus back.
- Plain arrow keys and Tab are never registered globally, because that would take them from your editor.
- AM/PM uses M rather than Tab, so Tab keeps its VoiceOver and keyboard focus order.
- The M1 spike verifies that focus returns to the previous text field.

### 5.5 Copy, motion and accessibility rules
- Never call a simulated frame a photo.
- Always print the time zone and the date.
- Units follow the system locale.
- **Motion:** Tier 0 appears without a fade. Tier 1 and Tier 2 cross-fade in 120 ms with no layout shift, and the fade counts inside the measured paint time (§10.4). Reduce Motion turns the fade off.
- **Keyboard and VoiceOver:** every control works from the keyboard, and the caption is real text that VoiceOver reads.
- **Copies and exports** carry the caption burned into the pixels. Refined frames also carry IPTC `trainedAlgorithmicMedia`.

---

## 6. System architecture

Eight components and one shared library, `oculus-core`, plus outside services. The Tier 1 renderer is one module used in three places: the overlay, the scrub view and headless evaluation.

### 6.1 Components

| Component | Runs in | Owns |
|---|---|---|
| Core (Rust, in the Tauri shell) | Desktop | Hotkey press and release, audio capture while held, speech bridge, parser, ephemeris through `oculus-core`, cancellation, tracing, caches |
| Speech library (Swift static library, C ABI, in process) | Desktop | SpeechAnalyzer `DictationTranscriber` with partial results; Foundation Models for the parser fallback |
| Overlay (TypeScript) | Webview | States, chips, caption, scrub, paint gate, Tier 2 composite and checks on the GPU |
| Renderer (TypeScript + GLSL ES 3.0, WebGL2) | Webview and headless Chromium/SwiftShader | Tier 1 frames from a plate and a `SceneSpec`; `oculus-core` WASM inside |
| Workers (Python) | AWS Lambda + EventBridge Scheduler | Forecast extraction (HRRR), camera capture, prediction issuing |
| API (FastAPI on Lambda) | Cloud | Manifests with signed URLs, Tier 2 proxy and spend guard, prediction log |
| Evaluation (Python) | Daily batch | Headless render of frozen `SceneSpec`s, extractor, baselines, scoring, backtest |
| Dashboard (static site) | Web, private until NPS confirms | Accuracy and latency views |

### 6.2 Diagram

```
┌──────────────────────── macOS app (Tauri 2) ─────────────────────────────┐
│ Rust core: hotkey · cpal (while held) · SpeechEngine · parser · oculus  │
│            cancellation · tracing · caches (forecast files, Tier 0, plates)
│   Swift static lib (C ABI): SpeechAnalyzer · Foundation Models          │
│ WKWebView panel: Tier 0 swap → Tier 1 WebGL2 (+ oculus WASM) → Tier 2   │
│                  composite (sky locked, transmittance-weighted detail)  │
└──────┬─────────────────────────┬────────────────────────┬────────────────┘
       │ forecast JSON, plates,  │ STT (bake-off or        │ Tier 2 (≤ 1 in flight)
       ▼ live frame (signed)     ▼ opt-in; token in ‖ mic) ▼
  S3 + CloudFront           Deepgram                 refine proxy (spend guard)
       ▲                                                  → fal or Runware
       │ writes                                     
  Lambda workers ◀── EventBridge Scheduler          Daily batch: headless render
  (HRRR byte ranges from noaa-hrrr-bdp-pds;        (SwiftShader) → extractor →
   NPS capture; prediction issuer)                  scores → Parquet → dashboard
```

The app and the evaluator read **the same forecast file** for each viewpoint and run, so the forecast behind a scored prediction is the one the app showed.

### 6.3 Request flow

Times are p95 budgets from key release; §10 breaks them down.
1. **Key down.**
   - Reveal the panel.
   - Open the microphone and the speech session.
   - Pre-connect TLS to the refine proxy.
   - If cloud speech is on, mint its token in parallel.
2. **First partial naming a known place** (match ≥ 0.9). Load that viewpoint's plate, forecast file and Tier 0 thumbnails from the local cache, fetching anything missing.
3. **Key up.**
   - Explicit finalize.
   - Parse, resolve the time, and compute the Sun and Moon.
4. **300 ms.** Tier 0, the closest real frame from the local library.
5. **1 s.** Tier 1, the relit frame. Its 1024 px input for refinement renders offscreen at the same time.
6. **3 s.** Tier 2, shown only if it passes the checks.
7. **After.**
   - Write the trace.
   - If the valid time is in the future, log the ask as a graded prediction (§15.2).

### 6.4 Platform matrix

| Platform | Speech | Status |
|---|---|---|
| macOS 26 or later, Apple silicon | SpeechAnalyzer `DictationTranscriber`, on device | v1 |
| Older macOS | Cloud streaming or a local model | Later |
| Windows | Cloud streaming or a local model | Later |

### 6.5 Why this stack

| Choice | Reason |
|---|---|
| Tauri 2 (2.12.7) + `tauri-plugin-global-shortcut` 2.4.1 | Reports both press and release, with no key repeat, which hold-to-talk needs. The Rust core links `oculus-core` directly. |
| `tauri-nspanel` 2.1.1, with our own NSPanel as a fallback | A non-activating, always-on-top panel. It relies on a private API, hence the fallback. |
| Swift in-process static library, C ABI | `swift-rs` has no async support. Foundation Models must run in the UI process. A helper process adds IPC and permission attribution. |
| WebGL2 renderer | Runs in the macOS webview and in headless Chromium, so the app and evaluation draw the same frames. |
| Lambda + EventBridge Scheduler + S3 | Exact schedules, near-zero idle cost, and no server to keep alive. Oculus's state backend, storage, CDN and budget-alarm Terraform carry over. |
| Parquet in S3, queried with DuckDB | The prediction log is append-only and analysed in batches, with no database server. |

### 6.6 Changes to the brief's default architecture

These are proposed. Each needs your go-ahead through §1.

| # | Brief default | Proposed | Why |
|---|---|---|---|
| C1 | Tauri 2 | Keep it, with Swift as an in-process static library | See §6.5. |
| C2 | Backend fetches Open-Meteo | An HRRR worker writes one JSON per viewpoint per run; the app and evaluation read the same file | Open-Meteo's HRRR ends 30 Nov, and it has no ceiling or cloud base and no run time. |
| C3 | Tier 2 via proxy | A proxy with a spend guard, or a short-lived token for direct upload, chosen by a gate of ≤ 100 ms added p95 | Keys stay on the server. |
| C4 | One scheduled job | Small unattended workers plus a daily batch | Frozen inputs make scoring reproducible. |
| C5 | Renderer takes Sun and Moon as inputs | Computed inside the renderer via WASM | One self-contained module. |
| C6 | — | Model-free camera response in Tier 1 | Otherwise the brightness and colour metrics measure the camera. |
| C7 | Hosted inference only | **Not proposed** unless the Tier 2 kill test fails: an on-device one-step model | Likely lower quality. |
| C8 | Golden Gate as the hero | Rainier as the hero, Point Reyes for fog, Grand Canyon third; Golden Gate only with permission | Licensing. |
| C9 | Tier 2 "refines the frame" | Tier 2 adds ground texture only. The sky and hazed regions stay Tier 1 by construction. | It makes "never invents weather" structural rather than merely checked. |

---

## 7. Query understanding

Speech becomes a structured query within 5 ms of the final transcript, with no language model in the common case.

### 7.1 Speech

```rust
trait SpeechEngine { fn start(&self, ctx: SpeechContext) -> Result<Box<dyn SpeechSession>>; }
trait SpeechSession {
    fn push_audio(&mut self, pcm: &[i16]);       // capture thread, while held
    fn partials(&self) -> Receiver<Partial>;     // text, stability, t_audio_ms
    fn finish(self: Box<Self>) -> Result<Final>; // key-up: burst silence pad + explicit finalize
    fn cancel(self: Box<Self>);
}
```

**Audio:**
- `cpal` 0.18.2, opened on key-down and closed on key-up.
- No pre-roll buffer.
- Audio is held in RAM for the duration of the query only.
- Viewpoint aliases go in as contextual strings (on device) or keyterms (cloud).

**Engines:**

| Engine | Where | Partials | Role |
|---|---|---|---|
| **A1** SpeechAnalyzer `DictationTranscriber` | On device, macOS 26+ | Yes | **Default.** The only Apple transcriber that honours contextual strings. The spike confirms with a network monitor that it makes no network calls. |
| **D** Deepgram Nova-3 with `Finalize` | Vendor servers | Yes | Measured in the bake-off. In the product: off by default, behind a toggle with a persistent indicator.<br>• `mip_opt_out=true` is set in one tested function.<br>• A short-lived token is minted in parallel with opening the mic.<br>• If no token API exists, there is no cloud path: never a long-lived key in the app. |
| **P** Parakeet (FluidAudio) | On device | Yes | Benchmarked only if A1's idle p95 from release to final exceeds 250 ms. |

Soniox stt-rt-v6 is in reserve. AssemblyAI is excluded, because free accounts can't opt out of training.

**Selection.** The bake-off reports, for each engine:
- time to first partial;
- release-to-final p95 when idle;
- WER;
- place accuracy after the parser.

The default stays on device unless you switch it after seeing those numbers.

**Finalization levers** (each measured, not assumed):
1. Explicit finalize on key-up, with a silence pad pushed as a burst.
2. Tier 0 painted from a stable partial. This is allowed only while the wrong-paint rate stays ≤ 2 % (Wilson upper bound on the test set).
3. Choosing the engine by its idle-state numbers.

### 7.2 Prefetch on partials

When a partial names a known place with a match score of 0.9 or more, the core loads that viewpoint's assets. A final transcript naming a different place cancels the load.

Tier 1 may also pre-render from a stable partial. Pre-rendering is not painting. Whether it slows speech recognition through GPU or Neural Engine contention is measured (release → final with pre-render on vs off).

### 7.3 Parser
1. Normalise: lower-case, and strip filler and carrier phrases such as "show me".
2. Extract the time expression with a hand-written grammar.
3. Match what remains against the alias table.
4. Emit the query.

```json
{"place_id": "rainier", "viewpoint_id": "rainier.paradise-mountain",
 "valid_time": "2026-11-20T16:00:00-08:00", "valid_time_utc": "2026-11-21T00:00:00Z",
 "tz": "America/Los_Angeles",
 "time_source": {"kind": "clock", "text": "tomorrow at 4", "rule": "day-plus-bare-hour"},
 "ambiguities": [{"field": "meridiem", "chosen": "pm", "alternative": "2026-11-20T04:00:00-08:00"}],
 "overrides": {}, "path": "fast_path", "confidence": 0.97}
```

**The fallback** handles only what the grammar rejects:
- Foundation Models with a `@Generable` struct, on device.
- An 800 ms timeout, and a cache keyed by the normalised utterance.
- The output is schema-validated and can never invent a `place_id`.

Every fallback call misses Tier 0, so the fallback rate is reported beside the Tier 0 numbers.

### 7.4 Time rules

All times are local to the place, never to the asker's machine.

| You say | Resolves to |
|---|---|
| A bare hour, "at 9" | The next 9:00 or 21:00 at the place, with an AM/PM chip. |
| An hour with AM, PM, "tonight" or "this morning" | That time. If it has already passed, tomorrow, with the date chip highlighted. |
| A day plus a bare hour, "tomorrow at 4" | 7 to 11 means AM, 12 means noon, 1 to 6 means PM; chip shown. |
| "Now", or no time at all | The current time, with the live frame if a camera exists. |
| "Sunrise", "sunset" | The Sun's centre at −0.833° altitude, which puts the upper limb on the horizon after refraction. |
| "Golden hour" | The interval with the Sun from +6° to −4°; evening unless "morning" is said. Rendered at +2°, and the scrub bar spans the interval. |
| "Blue hour" | Sun from −4° to −6°, rendered at −5°. |
| "Morning", "afternoon", "evening", "night" | 9:00, 15:00, 19:00, 22:00 (configurable). |
| "Tonight", alone | 21:00, or 1 h after sunset if that is later. |
| "In two hours" | Now plus the offset. |
| A weekday or a date | The next such day. |
| A time with no place | The last place asked about, named on the chip. |
| A daylight-saving gap or repeat | A nonexistent time moves forward; an ambiguous one takes the earlier offset. Both are flagged on the chip. |
| **Horizon** | HRRR to 48 h; a pinned GFS run to 7 days, with the caption saying so; beyond that, the Out of range state. |

### 7.5 Place and viewpoint matching
- **Aliases.** Each viewpoint has an alias list: names, nicknames and common mishearings.
- **Matching order:** alias → exact → token-set fuzzy → phonetic key (Double Metaphone), so "point rays" still matches.
- **Close calls.** If the two best scores are within a margin, a disambiguation chip appears.
- **Viewpoints.** "From" picks a viewpoint, as in "Grand Canyon from Yavapai". Otherwise the place's default viewpoint is used.
- **A miss** returns the three closest places, never a guess.

### 7.6 Tests
- A table of at least 150 utterances with expected queries, run on every commit with an injected clock.
- Edge cases:
  - noon and midnight;
  - weekday wrap;
  - solar events after the morning event has passed;
  - aliases, misspellings and phonetic errors;
  - ambiguity.
- Daylight saving: the skipped hour in March (14 Mar 2027) and the repeated hour in November (1 Nov 2026).
- Cross-zone: asked from Chicago (this machine) or Austin about Los Angeles and Phoenix. Phoenix has no DST, so it is tested across both changes.
- Property tests:
  - a resolved time is never in the past unless the ask was "now";
  - horizon flags are correct;
  - the canonical form is stable.

---

## 8. Data

Everything Snow Globe needs is free for a non-commercial project. The evaluation archive exists for the forecast; for the cameras, it is checked in M0.5.

### 8.1 Inventory

| Source | Used for | Cadence | Status |
|---|---|---|---|
| `oculus-core` (your Rust crate) | Sun and Moon position, illuminated fraction, twilight | Computed | Reused; refraction and the camera-frame transform are added (§8.4) |
| **HRRR, direct from AWS** `noaa-hrrr-bdp-pds` | Visibility, cloud ceiling, cloud base, cloud top, low/mid/high cloud, boundary-layer cloud, RH, dew point, boundary-layer height, precipitation rate and type | Hourly runs: 18 h, or 48 h at 00/06/12/18Z. On S3 from about 51 min (f00) after the run starts. Per-hour p95 up to 101 min for f00–f18 and 117 min for f48; 0.1 % of hours took over 120 min (max 131); 23Z runs publish f06–f18 at about 111 min (a survey of 124 runs, D-027). | Public domain. Archived since 2014; HRRRv4 from Dec 2020. |
| Open-Meteo, pinned model | GFS from 48 h to 7 days | Model-dependent | Free for non-commercial use; data CC BY 4.0. Its HRRR is **discontinued 2026-11-30**, and its responses carry no run time (bracketed with `meta.json`). |
| NPS air-quality webcams | Truth frames, plates, Tier 0 library, live view | One frame per 15 min at the air cams | Generally public domain. EXIF names NPS. Written confirmation requested. |
| Mount Rainier park camera `webcams-mora/mountain.jpg` | Rainier truth and plate | Off the quarter-hour; about once a minute (measured 19 Nov, PRD_PROGRESS.md) | 1920×1080, Axis P1387. **No ownership metadata**, so confirmation is requested from the park. |
| GOES-18 (`noaa-goes18`) | Independent cloud and fog vote at the viewpoint pixels | 5 min | Public domain |
| Golden Gate cameras | Demo viewpoint | — | No licence-clean source found. Crissy Field's cameras were "temporarily unavailable" on 18 Nov. Permission asks: Lawrence Hall of Science, ALERTCalifornia; cut-off 4 Dec. |
| Hosted image model | Tier 2 | Per call | Chosen in M3 by kill test and benchmark |

### 8.2 Viewpoints

Capture starts for every camera that passes the licence check. Building (plates, pose, labels) follows the order below.

| Viewpoint | Camera | Why | Status |
|---|---|---|---|
| **Mount Rainier, Paradise** | NPS park camera, 1080p | "Is the mountain out?" Cap clouds and cloud base | **MSV hero** |
| **Point Reyes** | NPS air cam, 4K, 15 min | Coastal fog; NPS calls it the windiest place on the Pacific Coast and the second foggiest in North America | **MSV** |
| Grand Canyon, Yavapai Point | NPS air cam, 4K, 15 min | Deep scene; winter cloud in the canyon | Third, built after the MSV |
| Yosemite, Turtleback Dome | NPS air cam, 4K | Large day-to-day weather changes. (The Half Dome and El Capitan cameras belong to Yosemite Conservancy and are excluded.) | v1 candidate |
| Acadia, McFarland Hill | NPS air cam, 4K | Coastal fog on the other ocean | v1 candidate |
| Grand Teton | NPS air cam, 4K | Mountain cloud | v1 candidate |
| Great Smoky Mountains, Look Rock | NPS air cam | Classic haze site | Checked in M0.5 |
| Big Bend | NPS air cam | Very clear air, in Texas | Checked in M0.5 |
| National Mall | NPS air cam `WASH`, mounted at the Netherlands Carillon (NPS ARD webcams feature layer; monitoring since 1 Jul 2003) | A recognisable landmark | Not in the capture set, which holds 8 cameras (D-043) |
| Golden Gate Bridge | None licence-clean | The original demo ask | Only with permission by 4 Dec |

### 8.3 Forecast

- **Primary source: HRRR, read directly.**
  - Byte-range GETs via `.idx` files fetch only the needed fields at each viewpoint's sample points. The worker parses the `.idx` files and decodes with ecCodes itself; Herbie was tried and is not used (D-026). A sample point sits where a view ray meets each layer height, not at the camera's grid cell.
  - One JSON file is written per viewpoint per run. It records the source and model, the run start, the availability time (S3 `LastModified`, live runs only), the fetch time, the grid cells, the byte ranges and the values.
- **Lead time** is the valid time minus the availability time.
  - 24 h and 48 h leads come only from the 00/06/12/18Z runs, so the actual 24 h leads are 24–29 h; the actual lead is kept.
  - For archived runs, availability is modelled as run start plus the live-measured p95 lag for each forecast hour.
  - A missing run falls back to the previous one and is never dropped.
- **Datums.** Every height field has its datum pinned in DECISIONS.md (above HRRR's smoothed terrain, or above sea level). One 3 km cell can span a canyon's rim and floor.
- **Between hours,** values are interpolated linearly. The scrub view interpolates between forecast hours.
- **Source churn.** If NOAA retires HRRR for RRFS, a source adapter switches and the switch is a labelled break in every series. RRFS is on NOMADS today but not yet in an operational AWS bucket, and its off-hour cycles have no low/mid/high cloud cover.

### 8.4 Ephemeris

`oculus-core` computes the Sun from JPL mean elements (Standish 1992) and the Moon from truncated ELP-2000/82 (Meeus ch. 47), with aberration, precession, nutation and topocentric parallax. It builds for Rust, WASM and Python, so the app and evaluation run the same code.

**Measured** (18 Nov, `cargo test --release --test golden_astro`, 3/3 pass, against JPL Horizons and Astropy):

| Quantity | Worst error |
|---|---|
| Sun apparent position | 0.19′ (0.003°) |
| Moon apparent position | 0.13′ |
| Moon illuminated fraction | 0.01 % |

**Additions:**
- Apparent altitude with atmospheric refraction (`Apparent.alt` is unrefracted).
- A transform into each viewpoint's camera frame, giving a pixel position and a light direction.

**Checks:**
- 1,000 random place-and-time samples against Skyfield with a JPL ephemeris.
- Horizons vectors for each viewpoint, hourly for 12 months.
- Sunrise and sunset against NOAA to ±1 min.
- Targets: Sun ≤ 0.05°, Moon ≤ 0.3°.

The crate is a Cargo git dependency pinned to `1b5410b`, the public `main` (re-pinned from the unpushed `06809e6` on 19 Nov; the ephemeris code is identical, D-025). Its MIT notice ships inside the WASM bundle, and it is valid to 2050.

### 8.5 Viewpoint plates

A plate set is everything the renderer needs for one viewpoint; Appendix A has the manifest. The builder is an offline command:
1. **Base frame.** A bright-overcast frame near midday with **every landmark visible**. This is hard at Rainier, where overcast often hides the summit. Several such frames can be median-combined. The frame is dehazed with the per-viewpoint haze model (§9.3), so it serves as an albedo proxy.
2. **Night frame and lights mask:** the median night frame minus the median day frame, thresholded.
3. **Pose:** heading, pitch, roll and field of view, solved from three or more surveyed landmarks. NPS publishes a named-landmarks image for each air cam.
4. **Depth and world height:** a DEM ray-cast for terrain, with a permissively licensed monocular model for the near field only, scale-aligned to the DEM. Models with non-commercial (NC) weights are excluded.
5. **Normals** from depth, and **DEM horizon maps** for shadows from off-screen ridges.
6. **Masks:** sky (segmentation), lights, and landmark bands.
7. **Pose check, part 1:** landmark reprojection ≤ 1°.
8. **Pose check, part 2:** the predicted time the summit (or another landmark) loses direct sun at sunset, against archive frames, converted to degrees. Where the Sun itself is in frame (Grand Canyon, summer sunsets), its predicted pixel is checked too.
9. **Camera response** fitted on development frames (§9.4).

### 8.6 Frame quality gates

A captured frame is scored only if it passes all of these. Exclusions apply identically to the model and to every baseline, and their counts are published.

| Gate | Test |
|---|---|
| Fresh | NPS `imagedate`, HTTP `Last-Modified` and the image hash all differ from the previous capture. |
| Same view | Registration against the plate is within tolerance, which catches a moved camera. |
| Clear lens | Sharpness above a floor, which catches droplets, ice and salt. Results are reported with and without iced-lens exclusions. |
| Colour mode | Monochrome (infrared) night frames are flagged; they feed the Tier 0 library but are not scored in v1. |
| Daylight | Defined by the ephemeris Sun altitude, never by frame brightness, because brightness correlates with weather. |

### 8.7 Storage

Measured frame sizes are 577 KB for the Rainier 1080p JPEG and 1.86 MB for the Point Reyes 4K JPEG (one night sample each). Stored ARD frames are re-encoded at 1920 px.
- **What is stored** (as decided on 19 Nov, D-028):
  - `rainier.paradise-mountain` and `point-reyes.beach` (design P) keep every downloaded frame at ≤ 1920 px;
  - the other six keep the frame nearest each UTC hour at ≤ 1920 px;
  - every download also gets a 640 px thumbnail, for staleness checks and Tier 0.
- **Measured** (19 Nov, 105 frames, at the 4-an-hour cap): 262 MB a day for 8 cameras. Rainier takes 100.8 MB (its daytime frames average 1.69 MB), Point Reyes 55.4 MB and each of the other six 13.3–21.1 MB. That is about 23.6 GB over three months; keeping every frame everywhere would be 452 MB a day.
- **Kept with the frames:** plates, the frame at every prediction's valid time, the forecast JSON files and the prediction log.
- **Access:** all of it is private until NPS confirms (§15).

---

## 9. Rendering pipeline

Three tiers produce the same view at rising fidelity, and only the last one uses an image model.

### 9.1 Tier 0: closest real frame
- **Library:** captured frames plus NPS archive frames from the same camera. Each entry is indexed by:
  - Sun altitude, and Sun azimuth relative to the camera;
  - day of year;
  - the extractor's weather class (cloud fraction, low-visibility flag);
  - source and licence.
- **Query:** the same descriptor, built from the ephemeris and the forecast file. If the forecast hasn't loaded yet, the match falls back to Sun position only.
- **Latency:** thumbnails for the viewpoint's plausible next 48 h are decoded in the background, so the key-up path is parse → swap.
- **Caption:** it says this is a real photo, when it was taken, and what it was matched on.
- **Leakage:** in evaluation, Tier 0 sees only frames captured before the issue time. It is the analog baseline R3 (§13.4).

### 9.2 Tier 1: relit plate

`render(scene: SceneSpec, target) → Frame` is pure in (scene, renderer version).

`SceneSpec` holds:
- the viewpoint and the valid time;
- the forecast samples, per layer: cover, base and thickness, plus ceiling, visibility, precipitation rate and type, RH, boundary-layer height and wind;
- the seed.

Sun and Moon are computed inside the module.

| Pass | Model | Driven by |
|---|---|---|
| 1. Atmosphere | **Hillaire 2020** lookup tables (transmittance, multiple scattering, sky view, aerial perspective), valid at every Sun angle including twilight. Ported from `sebh/UnrealEngineSkyAtmosphere` (MIT); Babylon.js's GLSL port (Apache-2.0) is the alternative. | Sun and Moon direction; the per-viewpoint clear-air haze |
| 2. Sun, Moon, night sky | Geometric disks. Moon brightness from its illuminated fraction. Moonlit sky brightness from `oculus-core` (Krisciunas & Schaefer 1991). | Ephemeris, pose |
| 3. Cloud layers | **Slabs with base and thickness**, each intersected along every view ray with Earth curvature. The forecast is read at the intersection point. Noise is thresholded before filtering so coverage doesn't drift with distance, and it drifts with the forecast wind. An overlap rule is checked against HRRR total cloud. | Low, mid and high cover; cloud base and top; ceiling; wind |
| 4. Scene light | Dehazed overcast plate × (sky irradiance + Sun irradiance × N·L × DEM horizon shadow × cloud transmittance toward the Sun). | Plate, normals, horizon maps, pass 3 |
| 5. Night lights | Lights mask × an on/off curve fitted to Sun altitude on real frames | Lights mask |
| 6. Haze, fog and ceiling | §9.3 | Depth, world height, visibility, ceiling, base, RH |
| 7. Precipitation | Extinction plus wet-surface darkening. No rain streaks: they are invisible at webcam shutter speeds and distances. | Precipitation rate and type |
| 8. Camera | Model-free radiometric response (§9.4), exposure, and a monochrome mode to match infrared night frames | Scene luminance |

- **Performance:**
  - ≤ 8 ms a frame at 1080p in steady state, so scrubbing holds 60 fps.
  - ≤ 50 ms of GPU time (`fenceSync`) for the first frame, including the lookup tables and plate upload.
  - Shader variants are pre-warmed after launch and after a lost context. GPU-resident plates are ≤ 1920 px wide.
- **Known limits:** one plate is not true albedo, and terrain shadows come only from the DEM. Low-Sun and golden-hour results are reported as their own stratum.

### 9.3 Haze, fog and obscuration

| Viewpoint | Phenomenon | Decisive forecast fields |
|---|---|---|
| Rainier (Paradise ~1,650 m; summit 4,392 m) | Summit hidden by cloud base or cap cloud; the camera itself inside cloud | Ceiling and cloud base **along the view ray to the summit**; low and mid cloud |
| Point Reyes | Marine stratus and advection fog; the camera possibly inside the marine layer | Low cloud, cloud base, near-surface visibility |
| Grand Canyon | Winter cloud and snow in the canyon | Cloud base against the rim and floor heights |

The rendering model:
- **Clear-air haze is not the forecast's visibility field.**
  - HRRR VIS is a near-surface diagnostic: the minimum of a humidity term and hydrometeors in the lowest ~100 m. It sees fog only when model cloud water is that low, so a deck at 300 m reads as clear.
  - It also says nothing about aerosol haze along a 20–100 km sight line.
  - So each viewpoint gets a **clear-air extinction profile fitted on clear development frames**, from landmark contrast against distance, per season. It is a fitted asset, so the leakage rules in §13.9 apply.
  - The Grand Canyon haze metric is dropped, because HRRR doesn't model aerosol.
- **Near-surface fog** applies the visibility field only to the part of each ray below ~100 m above ground. Extinction is β = −ln(ε) / V, with ε pinned to the diagnostic's contrast threshold (Koschmieder's 3.912 / V when ε = 0.02):

  ```
  β = −ln(ε) / V        T(d) = exp(−β d)        L = L_scene · T(d) + L_sky · (1 − T(d))
  ```

- **Low cloud and ceiling slabs** confine fog between a base and a top in world height, so terrain above the deck stays clear. Above the fog top, β decays.
- **Thick fog** uses a multiple-scattering airlight approximation.
- **Camera inside cloud** is its own state: uniform extinction, and no landmarks.

### 9.4 Camera model

The radiometric response is model-free (Kim & Pollefeys, PAMI 2008), fitted on development frames across all weather, so the exposure slope is identified at overcast and fog luminance. It has few parameters, and no statistic from the valid-time frame enters it. Results are published with it on and off; otherwise the brightness and colour metrics would measure the camera, not the forecast.

### 9.5 Determinism and tests
- **Seed.** The seed is a hash of the viewpoint and the forecast run, and cloud noise drifts with the forecast wind across valid time. So scrubbing is continuous, and the same (viewpoint, run, valid time) always gives the same pixels. The displayed seed is the one that gets scored.
- **Golden images.** Twelve reference conditions per viewpoint have goldens, and a render must reach SSIM ≥ 0.98 against its golden.
  - Goldens are rendered on Chromium/SwiftShader in CI.
  - WKWebView has its own, looser tolerance, which is documented.
- **Cloud test.** ≥ 1,000 seeds × cover from 0.1 to 0.9. The rendered sky fraction must be within 5 points of the **view-ray target fraction**, reported by elevation band. The target is not the raw forecast cover: forecast cover is an area fraction over a column, and a camera sees slant rays crossing many columns.

### 9.6 Tier 2: guarded refinement (after the MSV core)
1. **Kill test first.**
   - 20 Tier 1 frames run offline through Z-Image-Turbo img2img at strength 0.15–0.35, for ≤ $1.
   - It records the check pass rate, plus a preference judged by someone other than you.
   - Work continues only if refinement is visibly better and passes the checks.
2. **Safe by construction.**
   - Tier 1's sky is composited back through the sky mask.
   - Only high-frequency ground detail is transferred, weighted by Tier 1 transmittance T(x), so hazed or fogged regions can't be dehazed.
   - This runs on the GPU in the webview in ≤ 15 ms.
   - A phase-correlation check per frame catches a provider's one-pixel crop or resize, which would otherwise ghost the transferred detail.
3. **Input:** the Tier 1 frame at 1024 px on the long edge, rendered offscreen as soon as the parameters are known.
4. **Prompt:** built from the parameters only (time-of-day phrase, sky state, fog state). The user's words never reach the model.
5. **Cache key:** viewpoint, 10-minute bucket, quantised weather, model version. Measured latency runs use their own cache namespace.
6. **Transport:**
   - At most one request in flight, and a cancelled-but-unfinished request still counts.
   - No speculative or paid warm-up; only a TLS pre-connect on key-down.
   - A 50-call microbenchmark records the time to cancel and the amount billed.
   - fal is called with `X-Fal-Store-IO: 0`; Runware is the alternate host.
7. **Illustration:** FLUX.2 klein (on fal, not BFL, whose policy "may train on" inputs) gets ~20 calls to show why reference editors fail. It is not scored as a rate.

### 9.7 Guardrails

Tier 2 is shown only if every check passes. Otherwise Tier 1 stays and the reason is logged.

| Check | Measure | Starting threshold |
|---|---|---|
| Registration | Phase-correlation shift | ≤ 1 px |
| Structure | SSIM between edge maps of Tier 1 and Tier 2, non-sky region; landmark positions | ≥ 0.80; ±2 px |
| Brightness | Mean luminance per region against Tier 1 | Within 15 % |
| Colour | ΔE2000 per region against Tier 1 | Calibrated |
| Fog | Landmark-region contrast against Tier 1 | Within 20 % |
| Sky, Sun and Moon | Identical to Tier 1 by construction | Asserted in tests |

**Thresholds:**
- They start from the values above.
- They are calibrated on ~100 human-judged pairs that are separate from the test set, and then frozen.

**False accepts** are measured before any pass rate is reported:
- ≥ 200 deliberately weather-altered refinements are run through the *full composited pipeline*.
- The alterations: sky edits (including subtle ones), snow added or removed, wet surfaces, fog texture.
- Zero accepts bounds the false-accept rate at ≤ 1.5 % (95 %).

**Survival:** Tier 2 ships on by default only if:
- ≥ 80 % of queries get an accepted Tier 2 within 3 s; **and**
- a blind preference test (≥ 50 pairs, rated by someone else) prefers it in ≥ 70 % of pairs.

Otherwise it ships off, and the README says why.

---

## 10. Latency engineering

### 10.1 Budgets (p95, OS key-up → composited paint, idle)

| Tier | Target | Chain (initial allocation, ms) |
|---|---|---|
| Tier 0 | **≤ 300** | STT final ≤ 240 · parse 5 · swap ≤ 10 · composite ≤ 33 ≈ 288 |
| Tier 1 | **≤ 1,000; stretch ≤ 400** | STT final ≤ 240 (or stable partial) · parse 5 · GPU ≤ 50 · fade to 50 % ≤ 60 · composite ≤ 33 ≈ 388 |
| Tier 2 | **≤ 3,000**, reported as the share delivered within 3 s | STT final ≤ 240 · parse 5 · render + readback + JPEG ≤ 80 · proxy ≤ 100 · upload ≤ 250 · queue + inference ≤ 1,800 · download ≤ 200 · composite + checks ≤ 30 · fade ≤ 60 · paint ≤ 33 ≈ 2,800 |

The Lookout draft's plan reached the photographic frame in 2,250 ms. It assumed ~150 ms for the final transcript and 1,400 ms for inference.

The research doesn't support those figures:
- The best independently measured hosted STT p95 is 298 ms.
- On-device reports conflict 5× (125 ms vs 602–702 ms).
- Tier 2 inference figures are vendor claims that leave out our network.

So the honest slack on Tier 2 is about 200 ms. **The final transcript is the bottleneck for all three tiers, and no engine has yet shown ≤ 240 ms idle on this machine.** These are planning numbers; each milestone replaces them with measured p50 and p95. **No target moves after measuring.**

### 10.2 Techniques

| Technique | What it saves |
|---|---|
| Reveal and pre-warm on key-down | Panel wake-up, microphone, speech session, GL context and proxy connection are ready before speech ends. |
| Prefetch on a partial | Plate, forecast file and Tier 0 thumbnails are usually local by key release. |
| Offscreen Tier 2 input | The 1024 px frame renders in parallel with the on-screen frame. |
| Local caches | Plates on disk, GPU textures in memory, forecast files by run, Tier 2 results by key. |
| Cancellation | One generation number per query. A new key press cancels everything in flight. |

### 10.3 Correctness contract
- Every paint is gated in the webview, immediately before compositing, on (query_gen, scene_hash, tier) matching the current state. Late results are dropped.
- A wrong paint never counts as a hit. It is reported as visible-wrong milliseconds and as flips per query.
- A randomised key-mash stress test (≥ 10,000 cycles with injected stage delays) must show **zero stale paints**.

### 10.4 Measurement
- **t0** is the OS key-up event.
  - Replay injects key events with `CGEventPost` at the HID tap, so the real hotkey path fires.
  - Only the harness process (not the app) holds a listen-only tap, and it records only the hotkey's timestamps.
  - The tap is compiled out of release builds.
- **Paint:**
  - **Definition:** the first WindowServer-composited frame in which the tier is at ≥ 50 % opacity.
  - **Capture:** ScreenCaptureKit captures only the overlay window.
  - **Tag:** a pixel tag carrying (query_gen, tier) is drawn inside the same surface as the content.
  - **Kept:** only timestamps.
  - **Overhead budget:** capture on vs off must differ by less than one vsync at p95.
- **Diagnostics only:** `requestAnimationFrame` and IPC timestamps. They fire 1–3 vsyncs before compositing, so they are never the paint time.
- **Clocks:** ScreenCaptureKit timestamps and Rust `Instant` should both be host time; the spike verifies this.

### 10.5 Trace

Every query writes one line of JSON. It has these timestamps:
- `key_down`, `speech_first_partial`, `place_matched`, `assets_ready`;
- `key_up`, `speech_final`, `parsed`, `forecast_ready`;
- `tier0_paint`, `tier1_paint`;
- `tier2_sent`, `tier2_first_byte`, `tier2_checked`, `tier2_paint`.

It also has these spans:
- OS event → handler;
- audio buffer delivery;
- `stt_token`;
- DNS, TCP, TLS and time to first byte, with a connection-reused flag;
- proxy-internal spans;
- provider-reported queue and inference times;
- readback and encode;
- panel reveal.

And this state:
- idle duration and cache state;
- thermal state and power source;
- network interface **type only**.

Traces stay on the machine and never contain transcript text.

### 10.6 Replay harness
- **Substitution:** only sample *values* are swapped inside the live `cpal` callback. The real device opens, buffers arrive at their real cadence, and key-up triggers the real flush. No stage is mocked.
- **Fixtures:**
  - ~130 scripted utterances (30 for development, 100 for test), recorded through the live path;
  - ≥ 30 opt-in real-use utterances, reported separately.
- **States:**
  - **idle** (≥ 30 min; the headline);
  - warm (< 60 s since the last query);
  - cold (caches cleared);
  - after wake;
  - after launch.

  Idle trials: ≥ 100 for the shipped engine and ≥ 60 for the comparison engine, run unattended overnight under `caffeinate -i` from a frozen build. Each pass uses its own cache namespace.
- **Statistics:** a cluster bootstrap by utterance. Runs are spread over ≥ 3 days at different times, with engines interleaved.
- **Report:**
  - p50 and p95 per stage and tier, with confidence intervals;
  - budget hit rates, wrong-paint rate and partial lag;
  - WER, place accuracy and fallback rate;
  - the share of utterances still in speech at key-up.

  Known bias: one speaker, one microphone, one room.
- **One command** runs it and writes a results table plus the raw traces. CI runs the parts that need no microphone.

---

## 11. Backend and infrastructure

One small API on Lambda, five scheduled jobs and an on-demand backfill. Keys for any paid service never leave the server.

### 11.1 Routes

| Route | Returns |
|---|---|
| `GET /v1/viewpoints` | Verified viewpoints and the alias-table version (a static file on CloudFront) |
| `GET /v1/viewpoints/{id}/manifest` | The plate manifest, with signed URLs for plates, forecast files and Tier 0 thumbnails |
| Forecast file (signed URL, no API hop) | The latest per-run JSON for the viewpoint: the same file evaluation reads |
| `GET /v1/live/{viewpoint}` | The latest captured camera frame and its age. The app never polls NPS directly, so there is one polite fetcher for every install. |
| `POST /v1/refine` | The raw refined frame (compositing and checks run in the webview), or "keep Tier 1" at the spend cap |
| `POST /v1/predictions` | An id for a logged graded ask |
| `GET /v1/predictions/{id}` | The prediction, plus its truth frame and scores once the valid time has passed |
| Dashboard data | Static exports from the daily batch |

### 11.2 Jobs

| Job | Schedule | Does |
|---|---|---|
| Forecast worker | Every 5 min from 50 min past each run's start, until the run is on S3 | Extracts byte ranges and writes one JSON per viewpoint per run; records the measured lag per forecast hour |
| Capture | Every 5 min | Polls the NPS JSON or HTTP headers, and fetches the image **only when it changes**, at most 4 per hour per camera. Applies the quality gates and stores frames. |
| Prediction issuer | Hourly | Freezes a `SceneSpec` for leads of 1, 3, 6, 12, 24 and 48 h, wherever a run covers them. Rendering is pure, so freezing the inputs is issuing the prediction. |
| Daily batch | Daily (a missed day re-runs from frozen inputs) | Renders frozen `SceneSpec`s headless, runs the extractor and baselines, scores, and updates the private dashboard |
| Archive backfill | On demand | One request at a time, ≥ 5 s apart, resumable, never downloading twice, size-capped |
| Cost report | Daily | Paid calls against the caps; a budget alarm on top |

### 11.3 Hosting
- **AWS, described in Terraform.**
  - **Carried over from Oculus:** `bootstrap/` (state backend), `storage.tf`, `cdn.tf` and the `aws_budgets_budget` alarm.
  - **Not carried over:** the ALB, Fargate service, ElastiCache and NAT gateway. Oculus's own PRD estimates that stack at ~$60 a month.
- **Workers and API:**
  - Lambda, with a container image for ecCodes (Herbie is not used, D-026).
  - EventBridge Scheduler (its free tier covers 14 M invocations a month).
  - S3 and CloudFront.
  - Estimated ≈ $0–1 a month in v0.4. The infra code's estimate of 20 Nov, from AWS list prices and a simulation of the committed capture code, is $2.07–4.03 in month 1 with the free tiers, rising by about $0.22 a month as storage grows, so its top is above the $3 cap from month 1 (D-060, which replaces D-035's $1.60–2.81). For capture 0.2.1, whose polls are spaced from the previous answer, it is $2.43–4.40 in month 1, and the bottom of the range passes $3 in month 4 (D-066). The Lambda free-tier figures are checked on AWS's page before deploying.
- **The Tier 2 proxy** must add ≤ 100 ms p95 over a direct call when idle. Lambda (with SnapStart or a compiled runtime) and a Cloudflare Worker are both measured, and the result goes in DECISIONS.md.
- **GitHub Actions** runs CI only. Its schedules jitter, and a public repo's schedules are disabled after 60 days without activity. The sealed window spans the holidays.
- **A server-side daily cap** on Tier 2 calls. At the cap, `/v1/refine` answers "keep Tier 1".

---

## 12. Repository layout

The repo mirrors Oculus: the PRD, a milestone runbook and a per-ticket progress file sit at the root.

```
snow-globe/                 (the product's name too, since D-080)
  PRD.md  PLAN.md  PRD_PROGRESS.md  DECISIONS.md  THIRD_PARTY_NOTICES  Makefile
  app/
    src-tauri/              Rust core: hotkey, audio, speech bridge, parser, cancellation, trace
    src/                    overlay UI (TypeScript)
    speech-macos/           Swift static library (C ABI): SpeechAnalyzer, Foundation Models
  renderer/                 WebGL2 Tier 1 renderer, goldens, headless runner
  workers/                  forecast worker, capture, prediction issuer (Python)
  server/                   FastAPI on Lambda: manifests, refine proxy, predictions
  eval/                     extractor, baselines, scoring, backtest, reports
  plates/                   plate builder; binaries live in object storage
  viewpoints/               manifests, validated against a JSON schema in CI
  bench/                    replay and paint-capture harness (voice fixtures live outside git)
  dashboard/                static accuracy and latency site
  infra/terraform/          AWS serverless resources
  docs/                     research, council, review
```

Conventions:
- small commits, with tests beside the code;
- secrets only in environment variables;
- CI on every push;
- one Make target each for `test`, `bench`, `backtest` and `score`.

---

## 13. Testing and evaluation

Evaluation starts on day one: HRRR's archive goes back to 2014, and NPS keeps an image archive whose depth is checked in M0.5.

### 13.1 Tests

| Layer | Covers | Pass rule |
|---|---|---|
| Unit | Parser table, time rules, ephemeris against references, metric code | All pass |
| Property | Resolved time never in the past; cache keys stable; canonical form stable | All pass |
| Golden image | Tier 1 under twelve conditions per viewpoint | SSIM ≥ 0.98 |
| Cloud Monte Carlo | Rendered vs view-ray target fraction | Within 5 points |
| Contract | API request and response schemas; the manifest schema | All pass |
| Leakage | Baselines see only frames available at issue time; held-out frames are absent from the labelling store | All pass |
| Stale paint | ≥ 10,000 randomised cycles | 0 stale paints |
| False accept | ≥ 200 weather-altered Tier 2 frames | Bound reported |
| Replay | End-to-end latency, idle | Measured against §14 |

### 13.2 Evaluation designs

| Mode | Data | Leads | Available |
|---|---|---|---|
| **Hindcast (design H)** | Archived NPS frames paired with archived HRRR runs (exact run times; availability modelled) | 1–48 h | Back to HRRRv4 (Dec 2020), limited by NPS archive depth (checked in M0.5) |
| **Sealed prospective (design P)** | Live captures paired with live forecast files | 1, 3, 6, 12, 24, 48 h | 15 Dec 2026 – 14 Jan 2027 (≥ 28 days, extended by any outage) |
| **Graded asks** | Your own queries, scored when they come due | Whatever you asked | From the time the API is deployed (M2), reported separately |

The M0.5 archive check chooses H or P, and the split is committed before any held-out frame is touched.

Open-Meteo's Previous Runs archive is **not** used. It offers only whole-day offsets, and its visibility came back null for HRRR, NBM and GFS.

Every pair renders Tier 1 headless. Tier 2 runs on a budgeted sample only.

### 13.3 Truth from the frame
- **Primary truth: binary visibility per landmark.** A landmark is visible when its edge energy, compared with an illumination-conditioned clear-day reference, clears a threshold (the VEIA method).
  - Rainier's bands: summit, upper mountain, mid-slopes, near ridge.
  - That gives a categorical **obscuration height**: the highest visible band.
- **Secondary truth:**
  - cloud fraction in the sky mask, against an illumination-conditioned clear-sky reference, for Sun ≥ 10°. A red-to-blue threshold would flag red twilight skies and white near-horizon haze as cloud.
  - a continuous visibility estimate from contrast across several landmarks, scored in ordinal bins bounded by landmark distances.
- **Independent votes:** GOES-18's clear-sky mask and reflectance at the viewpoint pixels, plus on-site NPS met data where it exists. Disagreements are flagged, not averaged.
- **One extractor** is applied to rendered frames, baseline frames and real frames alike.

### 13.4 Endpoints (the pre-registration)

This section is frozen, with its commit hash recorded in DECISIONS.md, before any held-out frame is scored, labelled or viewed.

**Primary: P1, Rainier summit visible at a 24 h lead.**
- **Outcome.** A daytime frame (Sun ≥ 3° by ephemeris) is "visible" when the summit band's edge ratio is ≥ τ. Below a lower threshold it is "hidden". In between it is "partial", which is excluded from P1 and reported. Frames from inside cloud are a separate class, counted as hidden in a sensitivity analysis. τ and the partial band are fixed on development data.
- **Prediction:** the same extractor, run on the rendered frame.
- **Statistic:** the Peirce skill score (PSS = POD − POFD). The test is the paired ΔPSS against persistence, with a 95 % confidence interval from a block bootstrap. The block length is set from development-period autocorrelation, and is at least 1 day.
- **Claim:** made if the confidence interval excludes 0. The minimum detectable effect is published before sealing.
  - If it exceeds 0.25 on the prospective window, the primary moves to the hindcast held-out season.
  - If no hindcast is available, P1 is reported as underpowered, plainly.

**Renderer claims (pre-registered with P1):**
- **R1, non-inferiority:** PSS(Snow Globe) ≥ PSS(S) − 0.05 against both forecast-scalar baselines, S_rule and S_mos (§13.6).
- **R2, geometry:** for Rainier's obscuration height, sampling along view rays beats sampling the camera's own grid cell. Scored as ordinal error, paired, with a block bootstrap.
- **R3, analog:** the shader against Tier 0's forecast-class analog, with a pre-registered "no close analog" stratum. If the analog is non-inferior everywhere, the README says the shader buys explainability and coverage, not accuracy.

**Key secondaries** (Holm-corrected, reported, not claimed):
- **K1, cloud-fraction error at 24 h**: skill 1 − E/E_ref against persistence, over whole periods only.
- **K2, low-visibility event at 24 h, per viewpoint**: the nearest named landmark obscured, meaning fog or cloud at camera level. Scored by PSS, with POD, FAR and CSI also reported.
  - At Point Reyes this is v0.3's P2. The landmark, its distance and height, and the forecast fields mapped to it are named in M2, before sealing.
  - Point Reyes is scored on a summer hindcast season if the archive allows, because a December window will hold few fog events.
  - At Rainier it is the near-ridge band.

**Other secondaries** (descriptive):
- brightness (mean log luminance);
- colour (ΔE2000 in the sky region);
- LPIPS and image-embedding cosine, named as diagnostics;
- Sun and Moon disk error;
- leads of 1, 3, 6, 12 and 48 h;
- climatology skill;
- ungrounded-generation scores.

### 13.5 Scores

```
POD = H / (H + M)      POFD = F / (F + C)      PSS = POD − POFD
FAR = F / (H + F)      CSI = H / (H + M + F)   SS  = 1 − E_model / E_ref
```

H, M, F and C are hits, misses, false alarms and correct negatives. CSI ignores correct negatives and moves with the base rate, which is why the claims use PSS.

### 13.6 Baselines

| Baseline | What it is | What beating it shows |
|---|---|---|
| Persistence | The stronger of (a) the latest frame available at issue time and (b) the frame at valid − 24 h | The forecast adds information |
| Climatology | The frequency for that hour and month, from development frames | Skill beyond the season |
| S_rule | "Visible" if HRRR's ceiling where the summit ray crosses it is above the summit (datum-corrected) and low + mid cover is < 50 % | Rendering is faithful (R1) |
| S_mos | Logistic regression on HRRR fields, fitted on development data | Rendering keeps up with a statistical forecast (R1) |
| Camera-cell sampling | The same render, with the forecast sampled at the camera's grid cell | View-ray geometry matters (R2) |
| Tier 0 analog | The closest real frame, matched on Sun position and forecast class | Rendering beats retrieval (R3) |
| Ungrounded generation | ~50 text-to-image frames with no forecast input (≤ $1, after vendor approval) | What grounding buys: how often a generator "shows the mountain out" regardless of weather |

### 13.7 Statistics
- Results are reported by lead time and per camera. Pooled numbers appear only beside the per-camera ones.
- Intervals are 95 %, from a block bootstrap over days, since frames within a day are correlated.
- **Effective sample size is reported beside every pair count.** On the sealed window, 28 days × 2 viewpoints gives 56 day-viewpoint blocks, whatever the number of hourly predictions.

### 13.8 Extractor validation (before any number is published)
- **Labels:**
  - ~150 real frames per metric, stratified by time of day and weather;
  - ~50 labelled renders;
  - 50 frames re-labelled a week later, plus a second labeller if available.
- **Bars:** landmark visibility F1 ≥ 0.85; cloud-fraction MAE ≤ 0.10; fog F1 only with ≥ 30 positives.
- **A metric that fails its bar is not reported,** and the README says so.

### 13.9 Periods and leakage
- **Design H:**
  - Development is the earlier season or seasons within one camera epoch. Held-out is a later season.
  - Every fitted asset comes only from development frames that precede the held-out season: plates, haze profiles, clear-day references, the camera response, τ, S_mos and analog weights.
  - Only HRRRv4 is used.
- **Design P:** development runs from go-ahead to 14 Dec 2026; held-out is the sealed window.
- **Feature admission:**
  - Development data is split into dev-fit and dev-check weeks, alternating.
  - A renderer feature is admitted only if it improves dev-check P1 PSS by ≥ 0.02, or improves obscuration-height error.

### 13.10 Dashboard
- P1 with its confidence interval, n and base rate; R1–R3; the key secondaries with Holm-adjusted p-values.
- Accuracy by lead time, each tier against each baseline, per camera.
- Latency by stage, p50 and p95, idle and warm.
- Per-viewpoint strips: yesterday's forecast, what the camera saw, and persistence.
- The ten best and ten worst predictions beside their truth frames.
- Guardrail pass rate and the reasons for failures.
- Graded asks; uptime and exclusions.
- **Access:** private until NPS confirms. `make score` rebuilds it from frozen inputs.

### 13.11 What I expect, plainly
- **Rainier:** HRRR's ceiling and cloud base should carry real skill over persistence at 24 h, because synoptic cloud is what forecast models do well.
- **Point Reyes fog:** expect weak 24 h skill and frequent false alarms, mostly HRRR's rather than the renderer's.
  - HRRR's marine-layer tops run ~150 m low.
  - SFO historically needed a purpose-built forecasting system.
  - That result is reported as measured. The latency and rendering claims stand without it.

---

## 14. Metrics and targets

Each number has one place where it is measured. A missed target is reported with its measured value and the bottleneck. Targets don't move after measuring.

| Metric | Target | Measured by |
|---|---|---|
| Tier 0 paint, p95, idle | ≤ 300 ms (warm and cold reported beside it) | Replay + paint capture |
| Tier 1 paint, p95, idle | ≤ 1 s; stretch ≤ 400 ms | Replay + paint capture |
| Tier 2 within 3 s | ≥ 80 % of queries; p95 reported | Replay + paint capture |
| First partial, p50 | ≤ 300 ms after speech onset | Replay (onset known from the fixture) |
| Release → final, p95, idle | ≤ 240 ms (allocation) | Replay |
| Exact place and time, replay test set | ≥ 95 % | Bench |
| Parser fallback rate | ≤ 10 % (Tier 0 survives only below ~5 %) | Bench |
| Wrong paints from partials | ≤ 2 % (Wilson upper bound), or partial paint disabled | Replay |
| Stale paints | 0 in ≥ 10,000 cycles | Stress test |
| Tier 1 frame time | ≤ 8 ms at 1080p steady state; first frame ≤ 50 ms GPU | Renderer bench |
| Sun and Moon position | ≤ 0.05° and ≤ 0.3°; sunrise and sunset ±1 min | Ephemeris tests |
| Pose | ≤ 1° | Plate builder |
| Rendered vs target cloud fraction | Within 5 points | Monte Carlo test |
| P1: ΔPSS vs persistence, 24 h | Claimed if the 95 % CI excludes 0 (a hypothesis, not a gate) | Sealed scoring |
| R1: non-inferiority vs S_rule and S_mos | Margin 0.05 | Sealed scoring |
| K1, K2: cloud-fraction skill; low-visibility PSS, 24 h | Reported, Holm-corrected | Sealed scoring |
| Tier 2 false accepts | ≤ 1.5 % (0 of ≥ 200) | False-accept suite |
| Tier 2 guardrail pass rate | ≥ 90 %, with thresholds frozen first | Evaluation sample |
| Tier 2 preference | ≥ 70 % of ≥ 50 pairs, rated by someone else | Blind test |
| Extractor | Landmark F1 ≥ 0.85; cloud-fraction MAE ≤ 0.10 | Labelled frames |
| Verified viewpoints | 2 for the MSV; 8 targeted for v1 | Manifest |
| Scored pairs | ≥ 5,000 hindcast pairs where the archive allows; live pairs reported with effective n | Prediction log |
| Capture and worker uptime | Reported; outages extend the window | Logs |

---

## 15. Privacy, licensing and safety

Three rules cover it:
- audio stays local by default;
- every frame is labelled;
- no camera is used against its terms, and nothing derived from camera frames is published until NPS confirms in writing.

### 15.1 Rules

| Area | Rule |
|---|---|
| Audio | Captured only while the key is held and held in RAM for the query. Transcribed on device by default. Never stored, except recordings you add to the replay set. |
| Cloud speech | Off by default. Turning it on shows a persistent indicator. The destination chip changes **before** the first byte leaves the machine, and an on-device-only setting disables every cloud path. |
| Labels | A caption on every frame. Copied and exported images carry the caption burned in, and refined frames also carry IPTC metadata. |
| Cameras | Only sources whose terms allow automated access. The manifest stores the terms URL, the date checked, the ownership basis and the confirmation status. |
| Capture rate | Never faster than the camera's own update, and at most 4 per hour. Fetch only on change. No steering of shared cameras. |
| Publication | Until NPS confirms (requested for all cameras and the archive, by the end of M2), there is capture and private evaluation only. "Private" includes the app. With no answer, the public repo, README and demo carry code, numbers and charts only, and you sign off that list. |
| People in frames | Published frames are ≤ 1280 px, with people and vehicles blurred and each one reviewed. The Rainier camera faces a busy visitor area. |
| Attribution | The app and the README credit NOAA and each camera operator. |
| Secrets | Paid-service keys live only on the server. The app holds at most a short-lived token. |
| Telemetry | Traces stay on the machine and never contain transcript text. |

### 15.2 Data-flow inventory

| Data | Where it goes | Retention | Disclosure and control |
|---|---|---|---|
| Live audio (on device) | RAM | The query | `on-device` chip; mic indicator |
| Live audio (cloud, bake-off or opt-in) | Deepgram with `mip_opt_out=true` | Provider: "only for the duration necessary" | `→ Deepgram` chip before the first byte |
| Transcript text | RAM; Foundation Models (on device) | The query | Haiku only if enabled, after its terms are recorded |
| Traces | Local disk | 30 days rolling | IDs, lengths and parse fields only |
| Screen-capture frames (harness) | RAM, overlay window only | Discarded; timestamps kept | MEASURING indicator; Screen Recording permission disclosed |
| Hotkey event tap (harness) | RAM | Discarded | Hotkey timestamps only; compiled out of release builds |
| Replay fixtures (your voice) | Local, outside iCloud and git | Until M5 + 12 months, so README numbers stay traceable | REC indicator that switches itself off; opt-in |
| Graded asks | Prediction log (Parquet in S3) | Life of the project | install_id (pseudonymous), viewpoint, valid time and timings, with no text. Disclosed on first run; off in on-device-only mode. |
| API logs | CloudWatch | 30 days (set in Terraform) | No text. Our code stores no IP; the host's access-log retention is recorded. |
| Tier 1 frame sent for Tier 2 | fal (no request history, short CDN lifetime) or Runware | Provider terms | "AI texture" caption; burned-in label on export |
| Camera frames | Private S3, ≤ 1920 px | 12 months after M5, except plate sources and published examples | Published only after NPS confirmation (§15.1) |
| Plates, depth, masks | Private S3; app cache | As frames | Not public until confirmed |
| Forecast files | S3; app | Life of the project | NOAA credit |
| Provider keys | Server environment only | — | The app holds none |

### 15.3 Polite collection
- **Live:** poll the JSON or headers, and fetch the image only on change, at most 4 per hour per camera.
- **Backfill:** one request at a time, ≥ 5 s apart, resumable, never downloading twice, with a total size cap.
- **Identification:** the User-Agent names the project and its repo URL.
- **Errors:** back off on errors.
- **robots.txt:** honoured (NPS disallows only `/ns/`, `/search/` and `loader.cfm`).

### 15.4 Licence ledger (generates `THIRD_PARTY_NOTICES`)

| Item | Licence or basis | Duty |
|---|---|---|
| NPS camera frames | Generally public domain (NPS disclaimer); air-cam EXIF names NPS; confirmation requested | "Webcam imagery courtesy National Park Service. Derived images are not NPS products." No implied endorsement. |
| HRRR, GOES-18 | US government, public domain | "Forecast data: NOAA HRRR via NOAA Open Data Dissemination" |
| Open-Meteo | CC BY 4.0; free API non-commercial | A link beside any display; marked "modified" |
| Hillaire atmosphere | `sebh/UnrealEngineSkyAtmosphere` (MIT) or Babylon.js (Apache-2.0) | Keep the notice in the source and the bundle |
| oculus-core | MIT | Notice inside the WASM bundle |
| Z-Image-Turbo | Apache-2.0 | Notice; disclose AI refinement |
| Parakeet weights (if used) | CC BY 4.0 | Credit line |
| Depth model, embedding model, DEM | Named in M2; non-commercial weights excluded | Per licence |

### 15.5 Takedown
- **Channel:** a GitHub issue template plus a project-specific contact address.
- **Response:** within 7 days.
- **Deleted from:** storage, CDN, dashboard and the Tier 0 index. Local caches purge when the index changes.

---

## 16. Milestones

Each milestone ends at a gate. **A gate passes when its evidence exists**: the measured value and, for a miss, the bottleneck. A missed target triggers the cut-order rule, not a stall and not a retune.

**Rules:**
- A milestone that runs more than 25 % late loses the next item on the cut list (§19).
- Nothing outside the MSV starts before it is published, except M0.5.
- Every model, vendor and architecture choice goes in DECISIONS.md with its measurement.
- No paid call to a vendor before your go-ahead for that vendor.

**Dates** are offsets from go-ahead day **G**, assuming ~25 hours a week (§1 row 12). Fixed dates that don't move:
- the Golden Gate cut-off, **4 Dec 2026**;
- the sealed window, **15 Dec 2026 – 14 Jan 2027** (design P), with the pre-registration frozen before it.

Under design P, the freeze needs τ, S_mos, the analog weights and the MDE from development data by 14 Dec. With go-ahead on 19 Nov that is G+25, so it moves to the front of M2. Under design H the date doesn't bind.

| Milestone | Builds | Gate evidence |
|---|---|---|
| **M0 Scout** (done 19 Nov) | PRD, research, council, cross-check | Your calls in §1 |
| **M0.5 Start the clock** (G → G+4) | • Forecast worker.<br>• Capture of every camera that passes the licence check (≤ 8).<br>• NPS archive check.<br>• Repo scaffold, CI, trace skeleton, PRD_PROGRESS.md.<br>• **One backtest pair end to end**: an archived frame plus an archived HRRR run → a crude headless render → a crude extractor score.<br>• Email drafts. | • ≥ 72 h of worker and capture logs.<br>• Archive report, with H or P chosen in DECISIONS.md.<br>• The pair rendered and scored, labelled "v−1".<br>• Emails sent (you). |
| **M1 Voice to first pixel** (G+4 → G+18) | • A one-day platform spike: hotkey with the modifier released first; panel over another app keeping focus; click-to-key focus; wake timing; cpal; Swift partials; network silence; ScreenCaptureKit tag; `CGEventPost`; clock agreement.<br>• Speech A1, plus D for the bake-off.<br>• Parser, overlay with Tier 0, replay and paint harness, README skeleton with blanks. | • Replay report (idle headline): p95 per stage, Tier 0, first partial, WER, place accuracy.<br>• ≥ 150 parser cases green.<br>• STT decision.<br>• 0 stale paints.<br>• A screen recording over another app. |
| **M1.5 Walking skeleton** (by G+21) | Renderer v0 headless (sky, cloud slabs, ceiling slab, flat fog, camera v0); extractor v0; P1 on **development data only** against persistence, climatology and S_rule | • A dev-only P1 table, with nothing held out touched.<br>• Monte Carlo test output. |
| **M2 Relit frame** (G+21 → G+38) | • Tier 1 for Rainier and Point Reyes in the overlay and scrub view.<br>• Ephemeris validation, plates, haze profiles, camera response, goldens.<br>• Features admitted on dev-check evidence.<br>• Pre-registration frozen. | • Ephemeris report.<br>• Pose report.<br>• Tier 1 p95 idle, with pre-render contention measured.<br>• Goldens in CI.<br>• Pre-registration hash. |
| **M3 Photographic frame** (G+38 → G+48) | Tier 2 kill test, then (if it passes) composite, checks, false-accept suite, cancellation and the proxy gate; then the LLM fallback | • Kill-test result.<br>• Delivery within 3 s.<br>• False-accept bound.<br>• Preference result.<br>• Spend ledger. |
| **M4 Proof** (held-out period + ~5 days) | Extractor validation; score once; dashboard; uptime | • P1 with CI, n and base rate.<br>• R1–R3; K1–K2.<br>• A plain-language fog result.<br>• Exclusions. |
| **M5 Ship** (~4 days) | README in move-predictor's shape, 60-second demo, `THIRD_PARTY_NOTICES` | Every README number traces to a file and `make score` from a fresh clone. **Ships on schedule whatever P1 shows.** |
| **v1 growth** (after M5) | Viewpoints 3–8, each with plate, pose, labels and goldens; ≥ 14 days of live data | Gate evidence per viewpoint |

**The 60-second demo:**
- 0–10 s: "Rainier tomorrow at four", asked over an editor, with a trace bar.
- 10–25 s: scrubbing the evening.
- 25–45 s: yesterday's 24 h forecast vs what the camera saw vs persistence.
- 45–60 s: skill against lead time, with confidence intervals.

### 16.1 Order of work inside a milestone
1. Write the gate's test or benchmark first.
2. Build until it produces its number.
3. Record the measured numbers in PRD_PROGRESS.md and the decisions in DECISIONS.md.
4. Stop and show the evidence.

---

## 17. Compute and cost plan

Development is capped at $25.
- Ask before the first paid call to each vendor.
- Hard caps are enforced in code.
- Every charge goes into the DECISIONS.md ledger.

| Item | What drives it | Estimate | Hard cap |
|---|---|---|---|
| Deepgram bake-off | Replay minutes (covered by sign-up credit) | ≈ $0.20–2 | $3 |
| Tier 2 kill test | 20 frames | ≤ $1 | $1 |
| Tier 2 benchmark and false-accept suite (if the kill test passes) | ~270 benchmark calls + ≥ 200 altered frames | ≈ $5–8 | $10 |
| Tier 2 during development and the evaluation sample | Interactive use; a sample in evaluation; result cache | ≈ $3–5 | $5 |
| Ungrounded-generation baseline | ~50 images | ≈ $0.25–1 | $1 |
| Haiku (only if Foundation Models fails) | < 10 % of asks, cached | ≈ $0.05–0.50 | $1 |
| Hosting and storage (AWS serverless) | Lambda, Scheduler, S3 (~12 GB in v0.4; ~24 GB under §8.7 as decided), CloudFront | ≈ $0–1 a month (v0.4); $2.07–4.03 in month 1 in the infra estimate of 20 Nov (D-060), $2.43–4.40 for capture 0.2.1 (D-066) | $3 |
| Tier 1, backtests, scoring | Headless shader renders and CPU metrics | $0 | — |
| **Total** | | **≈ $10–19** | **$24** |

---

## 18. Risks and stress test

The largest risk is that fog shows no skill at 24 h. The plan reports that result as measured, and the latency and rendering claims stand without it.

### 18.1 Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The final transcript alone breaks the Tier 0 chain when idle | High | High | Choose the engine on idle numbers; partial-based paint under a 2 % ceiling; report the miss and the bottleneck |
| Tier 2 delivered within 3 s for < 80 % of queries | High | Medium | Kill test first; one request in flight; Tier 1 is the floor; ships off if it fails |
| Fog and low cloud are too local for a 3 km model | High | Medium | P1 is at Rainier; fog is a key secondary; every lead is reported |
| NPS never confirms | Medium | High | Email in M0.5; numbers-only fallback; you sign off the public list |
| P1 underpowered | Medium | High | MDE published before sealing; design H; a plain "underpowered" report |
| The extractor fails validation | Medium | High | Bars set in advance; the metric is withheld |
| The analog beats the shader | Medium | Medium | R3 is pre-registered, and the README says so |
| Single-plate relighting looks wrong at low Sun | Medium | Medium | DEM shadows; Hillaire twilight; golden hour reported as its own stratum |
| No Golden Gate permission by 4 Dec | High | Low | Rainier carries the demo; Golden Gate moves to v2 |
| A capture or worker outage during the sealed window | Medium | High | Lambda + EventBridge, not GitHub cron; uptime alarm; archive backfill; the window is extended |
| `tauri-nspanel`'s private API breaks | Medium | Medium | Our own NSPanel fallback |
| A Screen Recording re-consent prompt stalls an overnight run | Medium | Medium | Checked in the spike; a harness-only signed identity |
| NOAA retires HRRR mid-evaluation | Low | Medium | Source adapter; a labelled break |
| Seen as a wrapper around an image API | Medium | High | Tiers 0 and 1 use no image model; Tier 2 can't touch the weather; the README leads with measured numbers |
| Spend overrun | Low | Medium | Hard caps; ask first |
| Scope creep | High | High | MSV; cut list; the 25 % rule |

### 18.2 Stress test

**v0.3 council:** four judges (latency engineer, graphics and ML researcher, skeptical hiring manager, privacy and licensing reviewer), two rounds each. Their record is in [docs/council/](docs/council/) and summarised in Appendix C.

**The Lookout draft's six objections, rechecked after the cross-check:**

| Judge | Objection | Answer now | Still open |
|---|---|---|---|
| Latency engineer | Tier 2 needs Tier 1's frame, so the budget is serial | Offscreen input at parse time; a chain of ~2,800 ms with an honest STT allocation; ≤ 1 request in flight | Final-transcript latency when idle; provider tails |
| Graphics researcher | An overcast plate is not albedo; sunsets will look flat | Dehazed plate; DEM horizon shadows; Hillaire twilight; golden hour scored separately | Specular and wet surfaces |
| Forecast verifier | Persistence is hard to beat under 6 h | P1 at 24 h, against the *stronger* persistence; all leads reported; LAMP at 1 h later | Skill at short leads |
| Hiring manager | Is this a wrapper? Three projects stapled together? | Two of three tiers use no image model; MSV; walking skeleton in ≤ 3 weeks; the headline credits NOAA | Whether M2 runs late |
| Licensing reviewer | May you store and show these frames? | NPS only; confirmation gate; people blurred; licence ledger | The Golden Gate source; NPS's reply |
| Investor | Who asks this every week? | Out of scope for v1. Decided after M4 results and ten photographer interviews. | Demand |

**Next council round:** add the forecast verifier to the four judges, and give the parts taken from the Lookout draft their first independent review.

---

## 19. Cut list

If time runs short, cut from the top.
1. Unverified places (already moved to Later).
2. Rain and snow effects beyond extinction.
3. Moon and stars (the Moon's *position* stays tested).
4. The LLM parser fallback: show "didn't catch that", with the three closest places.
5. The live-beside-simulated view.
6. Shipping the cloud speech fallback. The bake-off itself stays, because the brief requires it.
7. Grand Canyon, and any viewpoint beyond the MSV pair.
8. Scrub-view polish (keep a bare slider).
9. Tier 2: if the kill test fails or the schedule demands it. Tier 1 is the floor.
10. Post-MSV physics, lowest measured gain first.

**Never cut:** the capture and forecast jobs, the sealed evaluation, the latency and replay harness, the labels, and the data-flow inventory.

---

## 20. Open questions

Each is answered by a milestone, not by a guess.

| Question | Answer, or where it gets answered |
|---|---|
| Which fixed camera covers the Golden Gate, with terms that allow capture? | **M0: none found.** Permission asks go out, with a 4 Dec cut-off. |
| Which model replaces HRRR on Open-Meteo, and is it archived? | **M0: answered.** `ncep_rrfs_conus` (6-hourly, Single Runs from 2 Apr 2026). Not needed: HRRR is read directly, with its archive back to 2014. |
| Does NPS allow bulk archive download, and how far back does each camera go? | M0.5 archive check |
| Does the park own the Rainier `mountain.jpg` camera? | M0.5 email |
| Can SpeechAnalyzer deliver a first partial < 300 ms and a final ≤ 240 ms when idle? | M1. It runs in process, not in a helper. |
| Where does the cloud-deck height come from? | **M0: answered.** HRRR cloud base, cloud top and ceiling, with datums pinned in M2. |
| Do per-viewpoint haze profiles hold across seasons? | M2 |
| How well does single-plate relighting hold when the Sun is below 10°? | M2, as its own stratum |
| Does Tier 2 survive a $1 kill test, and which host wins? | M3 |
| Is there forecast skill for low visibility under 6 hours? | M4 |

---

## 21. Résumé lines and headline

Blanks are filled only from dashboard exports.

> *On a pre-registered, sealed __-day period (base rate: summit visible __ % of daytime frames; climatology PSS __), Snow Globe's 24-hour visual forecast of Mount Rainier's summit scored a Peirce skill score of __ vs __ for persistence (Δ __, 95 % CI __ to __). The skill is NOAA HRRR's. Rendering it lost none (non-inferiority margin 0.05: __), and sampling the forecast along each view ray beat the camera's own grid cell at predicting cloud height (__).*
> *Latency after 30 min idle: first forecast-driven frame in __ ms p95 from key release, measured at the composited pixel.*

- Built a voice-driven visual forecast app in Rust, TypeScript, WebGL2, Swift and Python: p95 of __ ms to a first real frame and __ ms to a forecast-driven frame, measured at the composited pixel after idle.
- Wrote a physically based relighting renderer (Hillaire atmosphere, view-ray cloud slabs, depth-aware fog): __ ms a frame, shared by the app and a headless evaluation job.
- Scored __ predictions (__ independent days) against national-park webcams at __ viewpoints, with skill of __ over persistence at 24 hours (95 % CI __).
- Guarded a diffusion texture step so it can't change the weather: __ % pass rate, false accepts ≤ __ %; the rest fall back to the physical render.

---

## 22. References

Checked 18–19 Nov 2026. Fuller source lists are in [docs/research/](docs/research/).

| Source | Used for |
|---|---|
| Sigg, Cavallaro, Günther and Oswald, *AIES* 2022 ([arXiv 2203.15601](https://arxiv.org/abs/2203.15601)) | Closest prior work |
| Hillaire, "A Scalable and Production Ready Sky and Atmosphere Rendering Technique", *Computer Graphics Forum* 39(4), 2020; [sebh/UnrealEngineSkyAtmosphere](https://github.com/sebh/UnrealEngineSkyAtmosphere) | Tier 1 atmosphere |
| Kim & Pollefeys, *IEEE PAMI* 2008 | Model-free camera response |
| Sunkavalli et al. 2007; Lalonde et al. 2009; Delle Monache et al. 2013 | Time-lapse relighting; analog ensembles |
| [NOAA HRRR on AWS](https://registry.opendata.aws/noaa-hrrr-pds); NOAA-EMC UPP `CALVIS_GSD.f` | Forecast fields; visibility provenance |
| [Open-Meteo GFS and HRRR docs](https://open-meteo.com/en/docs/gfs-api) | The HRRR discontinuation date |
| NWS RRFS webinar Q&A; SCN 26-48 | HRRR's continuation; RRFS replacing NAM |
| *Wind Energy Science* 10, 2755 (2025) | HRRR marine-layer tops running low |
| [NPS air-quality webcams](https://www.nps.gov/features/ard/webcams/index.html); [NPS disclaimer](https://www.nps.gov/aboutus/disclaimer.htm) | Cameras and terms |
| [Tauri global-shortcut plugin](https://v2.tauri.app/plugin/global-shortcut/) | Press and release events |
| Apple Developer Forums 811083, 818005, 819525 | Contextual strings; the 26.3 live-input break; `NSEvent` monitor crashes |
| [GitHub Actions: disabling workflows](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows) | The 60-day schedule disable |
| [Amazon EventBridge pricing](https://aws.amazon.com/eventbridge/pricing) | Scheduler free tier |
| Oculus repository (`PRD.md` §13.6; `infra/terraform`) | `oculus-core`; Terraform reuse; the cost estimate |

---

## Appendix A: Viewpoint manifest

One JSON file per viewpoint, validated in CI against `viewpoints/schema/viewpoint.schema.json`, which is the source of truth for the shape (D-044). The values below are placeholders that show it. The loader refuses v0.4's old spellings (pose fields under `camera`, `plate.landmarks`, `polite_caps.max_per_hour`).

```json
{
  "schema_version": 1,
  "id": "rainier.paradise-mountain",
  "place_id": "rainier",
  "place": "Mount Rainier",
  "aliases": ["rainier", "mount rainier", "the mountain", "paradise"],
  "default": true,
  "time_zone": "America/Los_Angeles",
  "status": "msv",
  "camera": {
    "lat": 0.0, "lon": 0.0, "elevation_m": 0,
    "view_azimuth_deg": 0.0, "view_azimuth_source": "", "position_uncertainty_m": 0,
    "sources": [{"url": "https://...", "used_for": "", "accessed": "2026-11-19"}]
  },
  "pose": {
    "heading_deg": 0.0, "pitch_deg": 0.0, "roll_deg": 0.0, "fov_deg": 0.0,
    "pose_rms_px": 0.0, "pose_check_deg": 0.0, "pose_check_method": "landmarks+sunset-shadow"
  },
  "landmarks": [{"name": "", "band": "summit", "px": [0, 0], "lat": 0.0, "lon": 0.0,
                 "height_m": 0, "distance_km": 0.0, "azimuth_deg": 0.0, "sources": []}],
  "plate": {
    "version": 1,
    "base": "base.webp", "night": "night.webp", "depth": "depth.png", "world_height": "height.png",
    "normals": "normals.webp", "horizon_map": "horizon.bin",
    "sky_mask": "sky.png", "lights_mask": "lights.png", "landmark_bands": "bands.png",
    "dem": {"source": "", "licence": "", "datum": ""},
    "assets_licence": {"base": "NPS public domain (pending confirmation)"}
  },
  "haze_profile": {"fitted_on": "dev frames", "season": "", "beta_per_km": 0.0},
  "camera_response": {"params": [], "fitted_on_frames": []},
  "forecast_samples": [{"layer": "low", "height_m": 0, "lat": 0.0, "lon": 0.0, "grid_cell": [0, 0]}],
  "truth_camera": {
    "operator": "National Park Service", "kind": "park-cam", "json_url": null, "image_url": "",
    "archive_code": null, "archive_since": null,
    "terms_url": "", "terms_checked": "2026-11-19", "ownership_basis": "", "confirmation": "to-request",
    "interval_min_nominal": null, "interval_min_measured": null,
    "fixed_view": true, "night_mode": "colour|infrared|mixed|unknown",
    "polite_caps": {"max_images_per_hour": 4, "poll_interval_s": 300, "fetch_on_change": true},
    "publish_cleared": false, "people_in_frame": true,
    "view_label": "", "image_px": [1920, 1080]
  },
  "truth_sources": ["webcam", "goes18_acmc"],
  "attribution": "Webcam imagery courtesy National Park Service. Derived images are not NPS products.",
  "notes": []
}
```

`pose`, `plate`, `haze_profile`, `camera_response` and `forecast_samples` may be null until M2, and a landmark's `px` may be null until the pose is solved.

## Appendix B: Kickoff checklist

**Yours:**
- [x] Set the calls in §1: every default accepted on 19 Nov (D-012).
- [ ] Send the NPS confirmation emails (the drafts are in [docs/outreach/](docs/outreach/)). If you want Golden Gate, contact Lawrence Hall of Science and ALERTCalifornia by 4 Dec.
- [ ] Confirm the AWS account to use. Terraform creates the budget alarm before anything else is deployed; confirm its email subscription.
- [ ] Create the Deepgram and fal accounts once approved, and put the keys in the server's environment, never in the app.
- [ ] Grant Microphone and Speech Recognition when the app first asks. Grant Screen Recording and Input Monitoring to the harness only. Turn Apple Intelligence on.
- [ ] Record the replay set in M1; label frames in M2–M4.

**Claude Code's next session:**
- [x] Bring PLAN.md and DECISIONS.md in line with your §1 calls (the sky model, hosting, the viewpoint plan), and start PRD_PROGRESS.md (19 Nov).
- [ ] Run M0.5 as in §16 (in progress since 19 Nov).
- [ ] Optionally, a council round on the parts taken from the Lookout draft (§18.2).

## Appendix C: Council review (v0.3), summarised

Full record: [docs/council/round1-responses.md](docs/council/round1-responses.md).

- **Latency engineer.**
  - Raised: paint measured at the wrong point; the harness measuring its own cache; p95 over 15 utterances; a "warm" headline; no correctness contract; an incomplete Tier 2 budget; an STT allocation that contradicted the research.
  - All adopted (§10).
- **Graphics/ML researcher.**
  - Raised: false cloud-packing geometry; an evaluation that measured HRRR and the camera rather than the renderer; missing climatology, scalar and analog baselines; single-β fog on slant paths; circular Tier 2 checks; no statistical power.
  - All adopted (§9, §13).
- **Hiring manager.**
  - Raised: "three projects stapled together"; the headline arriving last; no cut order; Tier 0's wrong-weather frame.
  - Adopted: MSV, walking skeleton, kill test, cut order, the 25 % rule, a headline that credits NOAA.
  - Rejected: deferring per-query paint capture, Holm correction and the analog baseline.
- **Privacy and licensing reviewer.**
  - Raised: no data inventory; a cloud key in the app; public domain treated as privacy clearance; overstated licences; captions lost on export.
  - All adopted (§15).

## Appendix D: Synthesis record

Every disagreement between the Lookout draft and v0.3, with its evidence and verdict, is in [docs/review/prd-crosscheck.md](docs/review/prd-crosscheck.md).

- **24 points checked in the Lookout draft:** 12 corrected outright, 7 merged, 4 where v0.3's approach is kept, and 1 confirmed (Sigg et al.).
  - Factual corrections: HRRR's end date, the Open-Meteo archive, visibility treated as haze, the Oculus hosting cost, frame sizes, and the Moon's illuminated fraction, which already exists.
  - Design corrections: overlay focus, the demo utterance, cloud-cover calibration, hourly seeds, the 16:10 frame.
  - Method corrections: gates shaped as targets, and the STT allocation.
  - Merged: the Sun-in-frame pose check, pair counts reported without effective n, the two primary metrics.
  - Kept from v0.3: idle-state headlines, the stronger persistence baseline, the in-process speech library, DEM shadows.
- **17 points checked in v0.3:** 12 where the Lookout draft's approach is adopted, 4 merged, and 1 kept (DEM depth).
  - Adopted from the Lookout draft: the atmosphere model, the decisions table, the design section, time rules, routes and jobs.
  - Merged: the eight-camera plan.
- **Kept from v0.3 throughout:** the measurement method, the pre-registration, the view-ray geometry, Tier 2 safe by construction, and the privacy inventory.

---

*End of document.*
