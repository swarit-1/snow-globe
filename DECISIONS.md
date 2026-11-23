# DECISIONS.md

Every model, vendor, data-source and architecture decision, with the evidence behind it. Entries are append-only; a reversed decision gets a new entry that points back.

**Status:** **decided** (evidence exists) · **proposed** (awaiting owner go-ahead) · **open** (to be measured).

---

## D-001 — Milestone 0 is documents only · decided · 2026-11-18
- PRD.md, PLAN.md, this file, `docs/research/` and `docs/council/`. No application code until the owner's go-ahead.

## D-002 — Ephemeris: reuse oculus-core · proposed · 2026-11-18
- **Decision:** Cargo git dependency on `oculus-core` (github.com/swarit-1/oculus, `core/`, MIT), pinned to `06809e6`.
  - `ephem::sun_moon` is used natively (solar events in the parser), via the `wasm` feature inside the renderer, and via the `python` feature in eval.
  - The MIT notice ships in the WASM bundle.
- **Evidence** (2026-11-18, `cargo test --release --test golden_astro`, 3/3 pass): worst error vs JPL Horizons was Sun 0.19′ and Moon 0.13′ (294 rows, 3 sites, 1950–2050), against a 30′ budget.
- **Caveats carried into M2:**
  - `Apparent.alt` is unrefracted, so we validate and render refracted altitude.
  - Horizons vectors are regenerated for each viewpoint.
  - Valid to 2050.
- **Rejected:** copying the ~1,500 lines, because fixes would fork.

## D-003 — Tier 1 renderer API: WebGL2 · decided (owner default) · 2026-11-18
- WebGL2 / GLSL ES 3.0. It runs in WKWebView and headless in Chromium/SwiftShader, which gives deterministic CPU renders for golden images and eval.

## D-004 — Speech-to-text · open (shortlist set) · 2026-11-19
- **Benchmark first:** A1 = SpeechAnalyzer `DictationTranscriber`; D = Deepgram Nova-3 with `Finalize`.
- **P = FluidAudio Parakeet** only if A1's *idle* release → final p95 exceeds 250 ms.
- **Soniox stt-rt-v6 in reserve** (no audio stored, no training) if Deepgram's opt-out or token flow fails.
- **Excluded:**
  - AssemblyAI, because free accounts can't opt out of training;
  - SpeechTranscriber, because it ignores contextual strings;
  - Deepgram Flux and the re-decode race, unless measurement calls for them.
- **Selection rule:** lowest idle p95 release → final at equal or better post-parser place accuracy. Fallback = the best engine of the other kind.
- **Sources:** `docs/research/stt-ondevice-macos.md`, `docs/research/stt-cloud.md`.

## D-005 — LLM fallback parser · open · 2026-11-19
- Apple Foundation Models in-process (`@Generable`), $0, no text leaves the machine.
- Claude Haiku 5.5 with thinking disabled, only if FM is unavailable or fails accuracy, and only after Anthropic's retention and training terms are recorded here.
- Last-but-one in the cut order.
- **Sources:** `docs/research/stt-ondevice-macos.md` §6, `docs/research/stt-cloud.md`.

## D-006 — Tier 2 refinement · open · 2026-11-19
- **Kill test first** (≤ $1): Z-Image-Turbo img2img on fal.
- **If it passes:** benchmark against Runware's host.
- **FLUX.2 klein 4B** on fal as a ~20-call illustration only (not BFL, whose privacy policy says it "may train on" inputs).
- **Composition:** sky locked to Tier 1; only transmittance-weighted ground detail transferred.
- **Source:** `docs/research/refinement.md`.

## D-007 — Viewpoints and cameras · proposed (owner input needed) · 2026-11-19
- **Viewpoints:**
  - Rainier park cam `nps.gov/webcams-mora/mountain.jpg` (1920×1080, Axis P1387; ownership metadata absent; cadence to be measured);
  - Point Reyes NPS air cam (3840×2160, 15 min);
  - Grand Canyon Yavapai air cam (3840×2160, 15 min; built after the MSV).
- **Golden Gate:** only with written permission by 4 Dec 2026.
- **Spot check 2026-11-19 04:20 UTC:** air-cam EXIF has `Artist: National Park Service`, `Copyright: National Park Service`, `Software: ARS_Digicam`. The Rainier air cam is only 512×288 and looks WSW (not at the summit), so it was rejected in favour of the park cam.
- **Publishing gated on NPS written confirmation** (PRD §3.5).
- **Source:** `docs/research/cameras.md`.

## D-008 — Forecast source and issue time · proposed · 2026-11-19
- **Primary:** HRRR direct from AWS `noaa-hrrr-bdp-pds` via Herbie byte ranges.
- **Open-Meteo:** pinned GFS beyond 48 h and for unverified places, bracketed by `meta.json`.
- **Why:**
  - Open-Meteo discontinues HRRR on 2026-11-30 (confirmed on its docs page, 2026-11-19).
  - It has no ceiling or cloud-base field.
  - Its responses carry no run time.
- **Issue time:**
  - Live runs: S3 `LastModified`.
  - Hindcast runs: run start + measured p95 lag per forecast hour.
  - Lead time = valid − availability.
- **Source:** `docs/research/forecast.md`.

## D-009 — Hosting · open (owner input needed)
- **Proposed:**
  - public repo;
  - GitHub Actions for the worker and batch jobs;
  - Cloudflare R2 for private frames;
  - Cloudflare Workers for the token minter or proxy (no scale-to-zero cold start), gated at ≤ 100 ms added p95.

## D-010 — Sky model · proposed · 2026-11-19
- Hosek–Wilkie ported from `andrewwillmott/sun-sky` (repo Unlicense; **data tables BSD-3**, notice kept), used for Sun ≥ 0°.
- Twilight from a separate model.
- Fallback: three.js `Sky.js` pinned at r187.

## D-011 — Evaluation design · proposed (frozen at the pre-registration commit) · 2026-11-19
- **Primary endpoint:** P1 = Rainier summit visible at 24 h, scored by ΔPSS vs the stronger persistence baseline, with a block bootstrap.
- **Renderer claims** R1–R3 as in PRD §8.2.
- **Design** H or P, chosen by the NPS archive check in M0.5.
- **Pre-registration hash:** to be recorded here before the held-out period starts.

## D-012 — The owner accepted every PRD §1 default · decided (owner) · 2026-11-19
- **The owner's words** (19 Nov 2026): "yes to all defaults. works for me. go ahead and implement this prd now."
  - This is go-ahead day **G**. M0 closed and M0.5 started the same day.
- **The 15 calls,** as written in PRD v0.4 §1:

| # | Decision | Call |
|---|---|---|
| 1 | Platform | macOS 26.5.x on Apple silicon, held through M4. Windows later. |
| 2 | Shell and renderer | A Tauri 2 shell and one WebGL2 renderer. Swift is an in-process static library (C ABI) for SpeechAnalyzer and Foundation Models. |
| 3 | Evaluation viewpoints | NPS cameras.<br>• The MSV makes claims on Mount Rainier (the Paradise park camera) and Point Reyes.<br>• Grand Canyon is third.<br>• Up to 8 NPS cameras are captured from M0.5.<br>• Golden Gate only with written permission by 4 Dec 2026. |
| 4 | Size of v1 | The MSV (2 viewpoints, sealed P1) first, then growth toward 8 verified viewpoints. |
| 5 | Compute and budget | Hosted, pay-per-call inference. A $25 development cap, with hard caps in code totalling $24 (PRD §17). |
| 6 | Speech privacy | On device by default (SpeechAnalyzer `DictationTranscriber`).<br>• Deepgram Nova-3 runs in the replay bake-off.<br>• In the product it is off by default, behind a visible toggle. |
| 7 | Paid vendors | **Approved now:** Deepgram for the bake-off, and fal for the Tier 2 kill test (≤ $1).<br>**Later, each with its own approval:** Runware, Claude Haiku (only if Foundation Models fails) and the ungrounded-generation baseline (≤ $1). |
| 8 | Hosting | AWS serverless: Lambda, EventBridge Scheduler, S3, CloudFront and a budget alarm, built from Oculus's `bootstrap`, `storage`, `cdn` and `monitoring` Terraform.<br>• The Tier 2 proxy host must add ≤ 100 ms p95 when idle; Lambda and a Cloudflare Worker are both measured.<br>• GitHub Actions runs CI only.<br>• The repo is public. |
| 9 | Name | Lookout. |
| 10 | Fallbacks | Keep the LLM parser fallback (Foundation Models, on device, $0). Unverified places move to Later. |
| 11 | Tier 0 when Tier 1 is close behind | If Tier 1 is ready within ~100 ms, skip painting Tier 0. It still runs and is measured. |
| 12 | Calendar | No hours were given, so ~25 h a week is assumed. A milestone more than 25 % late loses the next item on the cut list. The dates are in PLAN.md. |
| 13 | Start the clock | M0.5 starts now: the forecast worker, camera capture, the NPS archive check and one backtest pair. |
| 14 | Owner's time | • ~20 min of scripted recordings, plus ≥ 30 real-use ones.<br>• ~150 frames labelled per metric, plus 50 re-labels.<br>• Someone else to judge the Tier 2 preference test.<br>• Several nights of unattended idle trials.<br>• Permissions: Microphone and Speech Recognition; Screen Recording and Input Monitoring for the harness only; Apple Intelligence on. |
| 15 | Emails and contact | The owner sends the NPS confirmation emails, which Claude drafts. The README lists a project-specific takedown address. |

- **Scope.** Read together with "go ahead and implement this prd now", the go-ahead covers PRD v0.4 as written, including the §6.6 changes C1–C9. C7 stays "not proposed".
- **What this does to earlier entries.** Each one still reads as written; this list records where it stands now.
  - **D-001** (M0 is documents only): complete. Code starts in M0.5.
  - **D-002** (oculus-core) and **D-008** (HRRR direct from AWS): accepted as part of the PRD (§8.4 and §8.3). The M0.5 build uses both.
  - **D-003** (WebGL2): confirmed by row 2.
  - **D-004** (speech-to-text): amended by D-015 (row 6). Deepgram is approved for the bake-off (row 7).
  - **D-005** (LLM fallback): row 10 keeps Foundation Models. Haiku is a "later" vendor (row 7).
  - **D-006** (Tier 2): fal is approved for the ≤ $1 kill test only (row 7). Runware needs its own approval.
  - **D-007** (viewpoints): settled by row 3. The IDs and the capture set are in D-020.
  - **D-009** (hosting): superseded by D-014 (row 8).
  - **D-010** (sky model): superseded by D-013.
  - **D-011** (evaluation design): still proposed. The per-viewpoint design is D-019.
  - Section numbers inside D-001 to D-011 refer to PRD v0.3. In v0.4, publication is §15.1 and the endpoints are §13.4.
- **No paid call has been made.** Deepgram and fal have no accounts or keys yet.
  - Even with a key, the owner is asked before the first paid call to each vendor (PRD §17).
  - fal spend beyond the kill test needs its own approval.
- **Amended 21 Nov (D-080):** the owner renamed the product **Snow Globe** on 21 Nov, so row 9's call is now Snow Globe. The other 14 rows stand.

## D-013 — Tier 1 atmosphere: Hillaire 2020 · decided (owner go-ahead on PRD v0.4) · 2026-11-19 · supersedes D-010
- **Decision:** the Tier 1 atmosphere pass uses the Hillaire 2020 lookup tables: transmittance, multiple scattering, sky view and aerial perspective (PRD §9.2).
  - It is one model, valid at every Sun angle, twilight included.
  - Ported from `sebh/UnrealEngineSkyAtmosphere` (MIT). The alternative is Babylon.js's GLSL port (Apache-2.0).
  - The licence notice stays in the source and in the bundle (PRD §15.4).
- **Why:** D-010 stitched Hosek–Wilkie (Sun ≥ 0°) to a separate twilight model, which put the seam at sunset. Sunset is the moment people ask about most (cross-check S1, [docs/review/prd-crosscheck.md](docs/review/prd-crosscheck.md)).
- **Dropped from D-010:** the Hosek–Wilkie port and its BSD-3 tables, the separate twilight model, and the three.js `Sky.js` fallback.
- **Not yet measured** (M1.5–M2):
  - frame time: ≤ 8 ms at 1080p, and a first frame ≤ 50 ms GPU, lookup tables included;
  - the golden images;
  - the low-Sun stratum.
- **Amended 23 Nov (D-098):** built as renderer 0.3.0 (`5e50a9a`), with its deviations from the reference listed there. Measured on Chromium ANGLE/Metal on the M4, not yet in WKWebView: heavy 1080p p50 6.7–7.0 ms, under the 8 ms, and p95 8.3–8.7 ms, over it; clear 1080p p95 6.8–9.4 ms; the first frame 54–74 ms of page time, 478 ms right after a shader change, against ≤ 50 ms of GPU time, which is not measured (page time is not GPU time). The goldens are regenerated for it. The low-Sun stratum is still open.

## D-014 — Hosting: AWS serverless · decided (owner default, §1 row 8); deployment waits for the owner · 2026-11-19 · supersedes D-009
- **Decision** (PRD §11.3):
  - **Terraform carried over from Oculus:** `bootstrap/` (state backend), `storage.tf`, `cdn.tf` and the `aws_budgets_budget` alarm.
  - **Not carried over:** the ALB, Fargate service, ElastiCache and NAT gateway.
  - **Workers and API:** on Lambda, with a container image for `eccodes` and Herbie. Also EventBridge Scheduler, private S3 and CloudFront.
  - **The budget alarm is created before anything else.**
  - **GitHub Actions** runs CI only, and the repo is public.
  - **The Tier 2 proxy host** must add ≤ 100 ms p95 over a direct call when idle. Lambda and a Cloudflare Worker are both measured in M3, and the result is recorded here.
- **Why:**
  - Oculus's full stack costs ~$60 a month by its own estimate (Oculus `PRD.md` §13.6). That would use up the $25 cap in under two weeks.
  - GitHub Actions schedules jitter, and a public repo's scheduled workflows are disabled after 60 days without activity. The sealed window spans the holidays.
  - EventBridge Scheduler's free tier covers 14 M invocations a month (PRD §11.3). The AWS Price List for us-east-1 (published 18 Nov 2026) agrees: "$0 for the first 14 million scheduled invocations".
  - The Lambda free-tier figures come from a secondary source and are checked on AWS's page before deploying.
  - The estimate is ≈ $0–1 a month, under a $3 hard cap (PRD §17).
- **What changes from D-009:**
  - EventBridge Scheduler replaces GitHub Actions as the scheduler.
  - Private S3 replaces Cloudflare R2 for frames.
  - A Cloudflare Worker is no longer the default proxy, but it stays a measured candidate.
  - The public repo carries over.
- **State on 19 Nov:**
  - The Terraform and the Lambda image are being written in `infra/terraform/` and `infra/lambda/`. Their checks run offline (`infra/test.sh`), with no plan or apply.
  - This machine has no AWS CLI and no AWS credentials, so nothing has been planned against an account or applied.
  - Deployment waits for the owner: the account, credentials and the alarm email (PRD Appendix B).
  - Until then, capture runs only on this laptop (D-021).
  - The clone's `origin` is `github.com/swarit-1/snow-globe`. It has no remote refs, and nothing has been pushed. Pushing needs the owner's word.
- **Amended 21 Nov (D-080):** with the owner's word, `m0-prd` was pushed to `origin` `main` at 00:48:32 UTC on 21 Nov (`23c0543`), the repo's first push. The product is now Snow Globe, so the repo's name already matches it.

## D-015 — Speech: on device by default, cloud only in the bake-off · decided (owner default, §1 row 6) · 2026-11-19 · amends D-004
- **Product default:** A1, SpeechAnalyzer `DictationTranscriber`. It runs on device, in process, through the Swift static library (C ABI).
- **Cloud engine D** (Deepgram Nova-3 with `Finalize`):
  - **In the replay bake-off:** it runs alongside A1.
  - **In the product:** off by default, behind a visible toggle with a persistent indicator.
    - The destination chip changes before the first byte leaves the machine.
    - An on-device-only setting disables every cloud path (PRD §15.1).
  - **Safeguards:**
    - `mip_opt_out=true` is set in one tested function.
    - A short-lived token is minted in parallel with opening the mic. If no token API exists, there is no cloud path.
    - There is never a long-lived key in the app.
  - **Status:** approved for the bake-off (D-012 row 7). It hasn't run, because there is no account or key yet.
- **What changes in D-004:**
  - The bake-off still reports every engine on D-004's measures: first partial, idle release → final p95, WER, and place accuracy after the parser.
  - D-004's selection rule no longer picks the shipped default by itself. The default stays on device unless the owner switches after seeing those numbers (PRD §7.1).
  - Shipping a cloud fallback is item 6 on the cut list. The bake-off itself stays.
- **Unchanged from D-004:**
  - the Parakeet gate (only if A1's idle release → final p95 exceeds 250 ms);
  - Soniox in reserve;
  - the exclusions.
- **Evidence:** none yet. In M1, the spike checks with a network monitor that `DictationTranscriber` makes no network calls.

## D-016 — Product name: Lookout · decided (owner default, §1 row 9) · 2026-11-19
- **Name.** The product is **Lookout**.
- **Repo.** It stays `snow-globe`, here and on GitHub, until the owner renames it. Renaming is a GitHub change, so it waits for the owner's word.
- **Identifiers.**
  - The bundle id and the overlay wordmark follow the name; they are set in M1.
  - Packages are named `lookout-*` (Python and Rust) and `@lookout/*` (pnpm), as in CLAUDE.md.
- **User-Agent:** `Lookout/0.1 (webcam research capture; fetch-on-change)`. It can be overridden with `LOOKOUT_USER_AGENT`.
- **Open point:** PRD §15.3 wants the User-Agent to name the repo URL.
  - That URL contains the owner's GitHub handle, which CLAUDE.md keeps out of every request to an outside service. So for now the User-Agent carries no URL.
  - A project-specific contact address (PRD §15.5) would fit there instead. That is the owner's call.
- **Amended 21 Nov (D-080):** the owner renamed the product **Snow Globe**, which supersedes the name, the package names and the User-Agent above. Packages are now `snow-globe-*` (imports `snow_globe_*`) and `@snow-globe/*`. The User-Agent is `SnowGlobe/0.1 (webcam research capture; fetch-on-change)`, overridden with `SNOW_GLOBE_USER_AGENT`; `LOOKOUT_USER_AGENT` is still read. The running loop sends the old User-Agent until the lead's switch after the M0.5 gate. The repo point stands. So does the open point: the User-Agent still carries no URL.

## D-017 — Data lives outside iCloud and outside the repo · decided · 2026-11-19
- **Evidence** (checked 19 Nov):
  - `xattr -l ~/Documents` shows `com.apple.file-provider-domain-id` set to the iCloud Drive file provider. So `~/Documents` is synced, and so is the repo inside it.
  - `~/lookout-data` carries no file-provider attribute.
- **Decision:** all data lives in `$LOOKOUT_DATA_DIR`, which defaults to `~/lookout-data`. The layout is in CLAUDE.md.
  - That covers frames, archive backfill, capture and forecast logs, rate-limit state, forecast files, caches and samples.
  - None of it is written inside the repo.
- **Why:**
  - Frames stay private until NPS confirms.
  - The owner's voice fixtures must stay "outside iCloud and git" (PRD §15.2).
  - An iCloud-synced folder would copy both to Apple's servers.
- **Later data:** replay fixtures, plates, renders and traces follow the same rule when they arrive (M1–M2).
- **Amended 21 Nov (D-080):** the code takes the data dir from `$SNOW_GLOBE_DATA_DIR`, then the deprecated `$LOOKOUT_DATA_DIR`, then `~/snow-globe-data` if it exists, then `~/lookout-data` if it exists, and otherwise uses `~/snow-globe-data`. `~/lookout-data` stays the data dir until the lead's switch after the M0.5 gate, which moves it to `~/snow-globe-data` and leaves `~/lookout-data` as a symlink. The rule itself is unchanged: data lives outside iCloud and outside the repo.

## D-018 — NPS image archive: API and depth · decided · 2026-11-19
- **Found** on 19 Nov in the page script of `https://www.nps.gov/AirWebCams/pore`. It is not a documented public API.
  - `GET /airwebcams/api/GetAvailableDays/<code>` returns `MinDay`, `MaxDay` and availability per year.
  - `POST /airwebcams/api/Search/Execute` returns up to 300 assets a page. Each asset has `AssetID`, `ImageCreateDateTime` (local time), `LocalTimeString` (with the zone name), `SolarElevationAngle` and `TimeOfDay`.
  - `GET /airwebcams/GetAsset/<AssetID>/proxy/hires` returns full resolution, and `.../thumbmedium.jpg` returns a thumbnail.
  - Example requests and responses are in `~/lookout-data/samples/nps-json/`.
- **Depth** (`MinDay` and `MaxDay` from `GetAvailableDays`, fetched 19 Nov; codes as in CLAUDE.md):

| Code | Camera | First day | Latest day |
|---|---|---|---|
| `mora` | Rainier ARD air cam: looks WSW, not at the summit; not a viewpoint | 2003-07-18 | 2026-11-18 |
| `pore` | Point Reyes | 2004-01-06 | 2026-11-18 |
| `grca` | Grand Canyon, Yavapai Point | 2001-11-08 | 2026-11-18 |
| `grte` | Grand Teton, Teton Science Center | 2011-08-30 | 2026-11-18 |
| `yose` | Yosemite, Turtleback Dome | 2005-10-18 | 2026-11-18 |
| `ACAD` | Acadia, McFarland Hill | 2006-08-08 | 2026-11-19 |
| `grsm` | Great Smoky Mountains, Look Rock | 1998-04-01 | 2026-11-19 |
| `BIBE` | Big Bend | 2004-10-05 | 2026-11-19 |

- **Sampled day** (`pore`, 17 Nov 2026):
  - 88 frames, from 00:00 to 23:45 PST, day and night.
  - Mostly 15 minutes apart: 83 gaps of 15 min, 3 of 30 min and 1 of 90 min.
  - All 88 carry `SolarElevationAngle` and `TimeOfDay`.
- **The gap:** the Rainier park camera (`webcams-mora/mountain.jpg`, the summit view P1 uses) is not part of the ARD network and has **no NPS archive**. The `mora` archive belongs to the ARD air cam (512×288, looking WSW), which D-007 rejected as a viewpoint.
- **Use:** archive backfill, for the Tier 0 library and hindcast pairs, under the polite rules:
  - one request at a time, ≥ 5 s apart;
  - resumable, never downloading anything twice;
  - with a total size cap.
- **Still to measure:** per camera, the cadence over the years, the gaps, and camera epochs such as moves or resolution changes. These go in `docs/research/nps-archive-check.md` (being written), which feeds D-019.
  - The check follows D-019's guard: metadata for any date, but archived frames only from before 1 Jun 2026.
- **PRD §20's archive question:** this answers the depth half. Bulk-download permission is asked in the NPS email ([docs/outreach/nps-confirmation.md](docs/outreach/nps-confirmation.md)).

## D-019 — Evaluation design per viewpoint · Rainier P decided; ARD H open (epoch check); renderer question proposed · 2026-11-19
- **Rainier: design P, decided on evidence.**
  - The summit camera has no archive (D-018), so design P is the only design open to it.
  - P1 stays at Rainier, as PRD v0.4 sets it (§1 row 3, §13.4) and the owner accepted (D-012).
  - **Development:** go-ahead (19 Nov) to 14 Dec 2026.
  - **Held out:** the sealed window, 15 Dec 2026 – 14 Jan 2027: 31 days (≥ 28), extended by any outage.
  - **The pre-registration is fitted on development data and frozen by 14 Dec,** at the front of M2 (M2 starts 10 Dec). It covers:
    - τ and the partial band;
    - S_mos;
    - the analog weights and the "no close analog" stratum;
    - the block length and the MDE;
    - K2's landmark mapping.
  - **If the MDE exceeds 0.25,** PRD §13.4 moves the primary to a hindcast season. Rainier has none, so P1 would be reported as underpowered, plainly.
  - **Consequences:**
    - Rainier's development data is only what we capture from 19 Nov: about 25 days, fewer if the laptop sleeps before AWS is live.
    - Tier 0's Rainier library is the same frames, which bears on R3.
- **Point Reyes: design P on the sealed window, plus design H for K2.**
  - As the MSV's second viewpoint, it shares the sealed window with Rainier, which gives at least 56 day-viewpoint blocks (28 days × 2, PRD §13.7).
  - K2 (fog) is scored on the summer hindcast season below, if the archive allows (PRD §13.4). That part is open until the epoch check reports.
- **The other six ARD cameras** (Grand Canyon, Grand Teton, Yosemite, Acadia, the Smokies, Big Bend): **design H, open until the epoch check reports.**
  - Every archive starts between 1998 and 2011 (D-018), before HRRRv4 (Dec 2020). Design H uses HRRRv4 only.
  - **Condition:** it works only where no camera move or change splits the HRRRv4 years. That check is M0.5-15 (`docs/research/nps-archive-check.md`, being written).
- **The design H split, fixed before any archived frame is viewed.** PRD §13.2 commits the split before any held-out frame is touched.
  - **Held out, at every ARD camera:** summer 2026, 1 Jun – 31 Aug 2026 (local dates). It is the latest complete season on 19 Nov, and the summer that PRD §13.4 asks for at Point Reyes.
  - **Development:** HRRRv4 seasons before 1 Jun 2026, inside the camera epoch that holds the held-out season. Every fitted asset comes from development frames (PRD §13.9).
  - **Guard for the epoch check:**
    - It may read metadata (`GetAvailableDays`, `Search/Execute`) for any date.
    - It views archived frames, full size or thumbnail, only from before 1 Jun 2026.
    - So a camera change inside the held-out season can only show up at scoring, and the pre-registration needs an exclusion rule for it.
  - **Viewed so far:** no archived frame. Scouting fetched `GetAvailableDays` for the 8 codes and one metadata search (`pore`, 17 Nov 2026).
  - **Commit order:** this entry is committed before any archived frame is viewed. Changing the season takes a new entry, made before any frame from the new season is viewed.
- **Proposed, for the owner by 14 Dec: which renderer P1 scores.** PRD §13.4 and §16 freeze τ, S_mos, the analog weights and the MDE, but don't say which renderer version is scored.
  - **(a) The renderer at the freeze commit.** Later versions are reported as exploratory. In practice this is renderer v0 plus whatever lands by 14 Dec, so M2's haze profiles, camera response and DEM shadows would not count toward P1 or R1.
  - **(b) The renderer at M4,** improved through M2 and M3 on development data only, with features admitted on dev-check P1 PSS (PRD §13.9). A no-peek rule keeps frames dated after 14 Dec out of Tier 0, the live view and labelling until M4 scores.
  - **The trade:** the PRD's feature-admission rule fits (b). (a) gives up M2's features for P1, in exchange for a renderer no one could have tuned on held-out frames. That matters because design P's window overlaps M2 and M3.
  - **Either way,** the work up to 14 Dec is the same, because the freeze commit records the renderer.
- **Not proposed: moving P1 off Rainier.** A deep-archive camera such as Grand Teton would allow design H. But it would reopen §1 row 3, which the owner accepted, and §13.4 already covers an underpowered P1.
- **Relation to D-011:** this fills its "Design H or P" line. D-011 itself stays proposed until the pre-registration commit.
- **Amended 20 Nov (D-070):** until the owner confirms D-038's local-day reading, the held-out season's "local dates" are read as the camera's local date or the UTC date. An archived frame is held out if either is on or after 1 Jun 2026, so 31 May's local evening is held out at every ARD camera.

## D-020 — Viewpoint IDs and the 8-camera capture set · decided · 2026-11-19
- **IDs** take the form `<place>.<site>`, lower case with hyphens:

| `viewpoint_id` | Camera | Feed | NPS archive |
|---|---|---|---|
| `rainier.paradise-mountain` | Park camera, 1920×1080, Axis P1387 | `webcams-mora/mountain.jpg` (HEAD for `Last-Modified`) | none (MSV hero) |
| `point-reyes.beach` | Air cam, looks NNE | `porejson.txt`, `porelarge.jpg` | `pore`, from 2004 |
| `grand-canyon.yavapai` | Air cam, looks NW | `grcajson.txt`, `grcalarge.jpg` | `grca`, from 2001 |
| `grand-teton.science-center` | Air cam, looks W | `grtejson.txt` | `grte`, from 2011 |
| `yosemite.turtleback` | Air cam, looks SW | `yosejson.txt` | `yose`, from 2005 |
| `acadia.mcfarland` | Air cam, looks NE | `acadjson.txt` | `ACAD`, from 2006 |
| `smokies.look-rock` | Air cam, looks E | `grsmjson.txt` | `grsm`, from 1998 |
| `big-bend.panther-junction` | Air cam, looks N | `bibejson.txt` | `BIBE`, from 2004 |

- **Capture set:** all 8, from M0.5 (D-012 row 3).
  - On 19 Nov all 8 feeds were live.
  - The ARD JSON feeds sampled all carry `imagedate`, `imagesite`, `viewdetail` and `imagefilelarge`. That covers the seven air cams above, plus `mora`. Samples are in `~/lookout-data/samples/nps-json/`.
- **Build order** (PRD §8.2):
  1. Rainier and Point Reyes (the MSV);
  2. Grand Canyon;
  3. Yosemite, Acadia and Grand Teton;
  4. the Smokies and Big Bend.
- **Not in the set:**
  - the Rainier ARD air cam (D-007);
  - Yosemite Conservancy's Half Dome and El Capitan cameras;
  - the National Mall, which has no NPS air cam;
  - Golden Gate, unless written permission arrives by 4 Dec 2026.
- **Licence:** NPS imagery is generally public domain, and written confirmation is being requested (the owner sends the emails). Frames stay private until NPS confirms (PRD §15.1).
- **Manifests:** `viewpoints/manifests/<viewpoint_id>.json`, validated against `viewpoints/schema/viewpoint.schema.json` (PRD Appendix A).

## D-021 — Stopgap Rainier capture on this laptop · decided (running, with the shortfalls below) · 2026-11-19
- **What:** a local poller for `https://www.nps.gov/webcams-mora/mountain.jpg`, running since 06:30 UTC on 19 Nov (one process, PID 79758).
  - It sends one HEAD every 5 minutes, and a GET only when `Last-Modified` or the ETag has changed, for at most 4 frames per rolling hour.
  - The first frame was stored at 06:30:13 UTC.
  - Frames and JSON sidecars go to `$LOOKOUT_DATA_DIR/frames/rainier.paradise-mountain/`, and events go to `capture_log/`. The sidecars say `"capture_version": "stopgap-0"`.
- **Why:**
  - The summit camera has no archive (D-018), so every hour not captured is development data lost for P1 under design P (D-019).
  - AWS can't be deployed yet (D-014).
- **Where it falls short of CLAUDE.md** (read from its source and its output on 19 Nov):
  - **The cap isn't persisted.** The 4-per-hour count is a list in memory, so a restart resets it. CLAUDE.md requires persisted state, and `$LOOKOUT_DATA_DIR/state/` doesn't exist yet.
  - **The image time is `Last-Modified`, not the camera's time.** `Last-Modified` runs 12–15 s after the EXIF time (8 of 8 frames). The filenames and `image_time_utc` use it. Each sidecar says `"image_time_source": "http_last_modified"`, and the JPEGs keep their EXIF, so the camera time can be recovered.
  - **It is untested scratch code.** It runs from the session scratchpad under `/private/tmp`, which a reboot or cleanup can clear, and nothing restarts it.
- **Limits:**
  - **Awake only.** It runs only while the laptop is awake. Gaps are lost for good, and they count as outages in the uptime report.
    - Outages by 08:30 UTC, both idle sleep (`pmset -g log`): 06:52:41 → 07:09:41 UTC and 07:51:52 → 08:24:52 UTC, with 9 polls missed in all. PRD_PROGRESS.md keeps the running list.
    - No frame was lost either time. Each came after the hour's 4 frames were stored, so those polls would have been capped.
  - **Burst sampling.** The rolling cap takes the first 4 changes in each hour. So far that gives 4 frames 5 minutes apart (:30–:45 past the hour), then none for 45 minutes.
  - **One NPS fetcher at a time** (CLAUDE.md). While it runs, the archive check (M0.5-15) and the capture worker must not fetch from nps.gov in a separate process. The capture worker takes Rainier over and never runs beside it.
  - It ends when the AWS capture job is live.
- **Numbers:** in PRD_PROGRESS.md.

## D-022 — Held-out breach in the archive check; design H keeps summer 2026 except 15 Jul · decided (lead; the owner may change it before the 14 Dec freeze) · 2026-11-19 · amends D-019
- **What happened.** The full record is in `docs/research/nps-archive-check.md`, under "Guard breach".
  - D-019's guard allows archived frames only from before 1 Jun 2026. The M0.5 archive check had no date guard in code.
  - Between 09:44:34 and 10:40:52 UTC on 19 Nov, the check downloaded 22 frames past the guard, built contact sheets from them and looked at them:
    - **8 dated 15 Jul 2026,** inside the held-out season: the midday thumbnail at all 7 ARD cameras, plus the Point Reyes hires;
    - **14 dated 17 Nov 2026:** the midday thumbnail and hires at all 7 cameras.
  - **D-019's commit order was not met.** D-019 says "viewed so far: no archived frame" and was meant to be committed before any frame was viewed. It reached the repo at a1fca21, after the check had run.
  - **Cause:** the lead ran the docs and capture work packages in parallel, so the check ran before D-019's guard was in code.
- **What leaked:**
  - **15 Jul 2026:** one midday frame per camera, so that day's weather at each camera is now known.
  - **Framing:** the breach record lists what was observed at Grand Teton, Big Bend and the others, only so that it can be set aside.
  - **17 Nov 2026:** these show the current framing, which live capture shows anyway, and they are outside the held-out season. They add nothing.
- **Decision:**
  - The held-out season stays **summer 2026** (1 Jun – 31 Aug 2026, local dates).
  - **15 Jul 2026 is excluded from scoring at all 7 ARD cameras.**
  - **Why not another season:** no summer is untouched. A mid-July midday thumbnail of every summer from 2021 to 2025 was viewed as development data, so moving the season would not remove the problem.
  - **What may inform design H:** the observations under "Observations that rest on frames past the guard" must not shape any design-H choice, exclusion rule or pre-registration. Which cameras enter design H is decided from:
    - frames dated before 1 Jun 2026;
    - metadata;
    - a camera-change rule, written into the pre-registration and applied mechanically at scoring.
- **Enforcement:**
  - **Capture code:** `workers/capture/.../heldout.py` refuses archived frames dated 1 Jun 2026 or later in backfill, the archive check and the CLI. Only `--held-out-override "<reason>"`, meant for scoring, releases them, and every release is logged.
  - **Eval code:** eval's leakage guard defaults to design P's window and does not yet apply this archive guard. Adding it is integration work (W2).
  - **Data:** the 22 frames, their sidecars, the 7 contact sheets and the check's summary JSON were moved, not deleted, to `$LOOKOUT_DATA_DIR/quarantine/held-out-breach-2026-11-19/`. Its README gives the undo steps. `archive/` keeps 85 frames, the latest dated 15 Jan 2026.
- **Amended 20 Nov (D-070):** until the owner confirms D-038's local-day reading, both guards also read the UTC date, so an archived frame is held out if its local or its UTC date is on or after 1 Jun 2026; capture's guard has also read `ImageCreateDateTime` as local standard time since D-047. The lead removed the six queue entries dated 31 May.

## D-023 — Two NPS fetchers ran at once, and Rainier got 5 downloads in an hour · decided (fixed in code; stopgap retired) · 2026-11-19 · closes D-021
- **What happened:**
  - **During the archive check** (08:46–10:41 UTC), the Rainier stopgap kept polling: 6 stopgap events. CLAUDE.md says never to run two NPS fetchers at once.
  - **At the capture worker's first live pass** (10:56:15–10:56:31 UTC), the stopgap was still running.
    - Rainier got **5 image GETs in the rolling hour** from 10:56:28. The cap is 4.
    - It got two HEADs 53 s apart. The limit is one every 5 minutes.
  - **Load on nps.gov stayed small:** archive requests were at least 5 s apart, plus one extra Rainier HEAD and one extra GET. Both are still breaches of the polite-collection rules.
  - **Cause:** the lead started the capture work package without stopping the stopgap first.
- **Fix:**
  - **A host-wide lock.** Every `lookout-capture` command that touches nps.gov takes `state/locks/nps-fetcher.lock`. It exits with status 3, before sending anything, if:
    - another fetcher holds the lock;
    - the stopgap is running;
    - stopgap records appear in the last 11 minutes.
  - **The stopgap was stopped at 12:13:12 UTC.** It ran 06:30–12:13 and stored 24 Rainier frames; its last record was at 12:12:23.
  - **The production loop took over** after the 11-minute window: `lookout-capture loop --interval 300`, all 8 cameras.
    - It runs on this laptop under `caffeinate -s`, which keeps the Mac awake only on AC power.
    - It uses `PYTHONPATH=src:../common/src`, because iCloud hides the venv's `.pth` files.
  - **Rainier is now paced:** image GETs are at least 720 s apart, so frames spread through the hour instead of coming in bursts of 4.
- **Still true:**
  - Until AWS is live (D-014), capture runs only while the laptop is awake and this session's background task lives.
  - Archive backfill can't run while the loop holds the lock. It needs the loop paused, or one process that does both, to be built in W2.
- **Amended 20 Nov (D-065):** the lead's rule counts the 5-minute limit and the cap of 4 images an hour from the previous answer, not from the previous send as D-048 did after polls came up to 0.94 s early on 19 Nov. So both hold at nps.gov whatever the network does (capture 0.2.1, uncommitted and not running).

## D-024 — Evaluation design per viewpoint, from the archive check · decided (lead; the owner may change it before the 14 Dec freeze) · 2026-11-19 · closes D-019's open part
- **Evidence:** `docs/research/nps-archive-check.md`, which uses only frames from before the guard plus metadata (D-022).
  - All seven ARD cameras changed from a 3:2 frame to a 16:9 frame, at a different time each, between 2023 and 2025.
  - The archive keeps 999 px frames.
  - Timestamps match NPS's own solar elevation on all 90 sampled days.
- **Per viewpoint.** "Usable span" starts at the first 16:9 sample before the guard. The held-out season is summer 2026 everywhere, excluding 15 Jul 2026 (D-022).

| Viewpoint | Design | Usable span for design H | Notes |
|---|---|---|---|
| `rainier.paradise-mountain` | **P** (P1), sealed window 15 Dec 2026 – 14 Jan 2027 | none | The park camera has no archive. |
| `point-reyes.beach` | **P** on the sealed window, plus **H for K2** | Jul 2023 → | Development uses summers 2024 and 2025. The 3:2 epoch (2021–22) is a different view. |
| `grand-canyon.yavapai` | **H** | Jul 2024 → | The 3:2 epoch (Dec 2020 – Jan 2024, one framing) may serve development and backtests, but not the summer-2026 season. Check 2025-01-15 on full frames before using winter 2024/25. |
| `yosemite.turtleback` | **H** | Jan 2024 → | It has the most stable 16:9 framing by dHash. |
| `acadia.mcfarland` | **H** | Jan 2024 → | |
| `smokies.look-rock` | **H** | Jul 2024 → | It has the deepest coverage, 358–366 days a year. |
| `big-bend.panther-junction` | **H**, after a check | Jan 2024 (or Jul 2024) → | Check 2024-01 for a framing change, on frames before the guard only. |
| `grand-teton.science-center` | **no design H** | none | Its framing changed inside the 16:9 epoch: 2025-01-15 differs from 2025-07 and 2026-01. Excluded from scored metrics; live capture continues for Tier 0. |

- **For every design-H use:**
  - Fit τ, the clear-day references and the haze profiles on 999 px archive frames dated before 1 Jun 2026.
  - Either score live frames at the same width or report the resolution gap.
  - A camera-change exclusion rule goes into the pre-registration and is applied mechanically at scoring.
- **Not used:** the observations made from frames past the guard (D-022).

## D-025 — oculus-core pinned to the public commit 1b5410b · decided (lead) · 2026-11-19 · amends D-002
- **Decision:** eval (`1ba95d2`) and `app/core` (`fd0f206`) pin `oculus-core` to `1b5410b`, the public `main` of github.com/swarit-1/oculus, instead of `06809e6`.
- **Why:** `06809e6` is on no remote. It sits only on the local branch `overnight/2026-09-22`, 33 commits ahead of `1b5410b`, so a fresh clone or CI could not fetch it (Cargo: "revspec not found"; GitHub's API: 422, from the builders' reports).
- **Evidence** (19 Nov):
  - `ephem.rs`, `core/bindings`, `core/Cargo.toml` and `core/Cargo.lock` are identical in both commits. The diff only adds a `fly` feature that nothing here uses.
  - On `1b5410b`: eval 179 passed, 1 skipped; core 108 passed, with its NOAA and Moon statistics identical to 4 decimals.
  - A clean `CARGO_HOME` fetched the lock file with `--locked`.
  - The core builder's injected ref in `~/.cargo/git/db` was removed.
- **Unchanged from D-002:** the Horizons check (Sun 0.19′, Moon 0.13′) and its caveats. D-012 accepted D-002 as part of the PRD.
- **Left over, for the owners:** eval's `OCULUS_PINNED_REV` constant and a doc comment in `app/core/src/solar.rs` still name `06809e6`. The eval venv holds a `1b5410b` build (its `direct_url.json`), but eval's backend version string, and with it the M0.5 pair record, read `0.1.0+06809e6`.

## D-026 — HRRR extraction without Herbie; height datums; PRES:cloud base kept · decided (evidence; PRES:cloud base by the lead) · 2026-11-19 · amends D-008
- **Decision:** lookout-forecast parses the `.idx` files, makes the byte-range GETs (through `lookout_common.polite_client`) and decodes with ecCodes itself. Herbie is not used, so the Lambda image needs the ecCodes wheels (`eccodes` 2.50.1, `eccodeslib` 2.50.0.34), not "eccodes and Herbie" (PRD §11.3, D-014).
- **Why not Herbie** (a logged trial of Herbie 2026.10.0, 19 Nov):
  - its requests carried `python-requests/2.34.2` as the User-Agent, not the project's;
  - two HEADs and the `.idx` GET had no timeout;
  - it made 4 requests for one field-hour and exposes no S3 `LastModified`, so a listing is needed anyway;
  - it wrote config files to `~/.config/herbie` and left the GRIB subset on disk;
  - it adds pandas, pyproj, xarray, cfgrib and requests (about 84 MB).
  - Its VIS f00 byte range equals ours; a test keeps it so.
- **Datums** (NOAA-EMC UPP code, plus a whole-grid check of run 2026111907 f00):

| Field | Datum | Data check |
|---|---|---|
| `HGT:cloud ceiling` | m above MSL | 0 of 502,664 cloudy cells below the model terrain |
| `HGT:cloud top` | m above MSL | 0 of 457,194 cells below the terrain |
| `HGT:cloud base` | m above MSL where valid; often a phantom | 110,670 of 891,599 cells (12.4 %) sit 7.4–8.3 km below the height `PRES:cloud base` implies, in columns with high cloud only; just 2.47 % are below the terrain, so a terrain check misses most |
| `HPBL` | m above ground | below the terrain height in 100 % of the 86,217 cells where terrain is above 2,000 m |
| `HGT:surface` | m above MSL: model terrain, not the DEM | identical bit for bit at f00, f01, f18 and f48, so it is fetched once per run |
| every other field | n/a (not a height); `UGRD`/`VGRD` are grid-relative | |

- **`PRES:cloud base` stays in the default field set** (the lead, 19 Nov), which is then 22 fields. It costs 1.33 MB an hour and no extra request. The phantom rate was 12.4 % on 07Z f00 and 19.8 % on 09Z f03 (the review), so it varies.
- **Still to do:** mask phantom bases in the renderer and extractor inputs (TODO(M1.5)). The local stand-in's 7-field profile leaves `PRES:cloud base` out (D-030).
- **Field codes corrected on live data:** HPBL is WMO 0-3-18, and CRAIN, CFRZR, CICEP and CSNOW are 0-1-33 to 0-1-36. The decoder's identity check caught all five before anything was written.

## D-027 — Issue time and the availability-lag model · decided (lead) · 2026-11-19 · amends D-008
- **Issue time, as built** (lookout-forecast):
  - **Live runs:** an hour counts as available at the later of its S3 `LastModified` (GRIB2 and `.idx`) and the time Lookout finished fetching it (`fetch_finished_utc`). Selection reads stored files only, so a run that was never fetched is never modelled into existence.
  - **Archived runs:** run start plus a `LagModel` lag, recomputed at selection from the current log, never the value an old file stored.
  - Lead time = valid − availability, as in D-008.
- **The lag model** (PRD §8.3):
  - It is keyed by (cycle hour, forecast hour): the p95 of the cell's logged lags once it has 20. Before that, the later of the pooled p95 for that forecast hour and the cell's largest logged lag. With too little data, a flat 120 min (`conservative-fallback`).
  - Lags may come from S3 `LastModified` surveys of past runs (`lag-survey --log`) as well as from live runs. Survey lags are stored apart (`forecast_log/lag_survey/`) and labelled `"method": "survey"`; a live lag wins when both exist.
  - PRD §8.3's "live-measured p95" is read as "measured from S3 `LastModified`, never assumed".
- **Evidence** (19 Nov):
  - A survey of 124 runs (14 Nov 06Z – 19 Nov 09Z): 2,986 hour-lags, 37.3 % of them later than the forecast.md curve the first build used. 3 lags (0.10 %) exceeded 120 min, all in run 2026111715 (max 131.2 min).
  - All five 23Z runs published f06–f18 at about 111 min, against medians of 62.6–85.1 min for the other cycles. A p95 pooled over cycles would model those hours 10–37 min early.
  - The first build's default was earlier than all 19 lags logged for run 2026111908, by 1.1–12.3 min.
- **State at 00:50 UTC on 20 Nov:** the keyed model and the 120 min fallback are committed (`09e2ec8`). The survey log (`laglog.py`) is written, but `lag-survey --log` is not wired into the CLI yet, so no survey lag is logged. The live log holds 454 lags from the 24 runs of 19 Nov.

## D-028 — Frame storage: every frame at the design-P cameras, the hourly frame elsewhere, a thumbnail for every download · decided (lead) · 2026-11-19
- **Amends PRD §8.7,** which kept the frame nearest each hour plus 15-minute thumbnails at every camera.
- **Decision:**
  - `rainier.paradise-mountain` and `point-reyes.beach` (design P, D-024; Rainier has no archive) keep every downloaded frame at ≤ 1920 px.
  - The other six keep the frame nearest each UTC hour at ≤ 1920 px. Each download waits in `cache/staging/`; hour H is decided at H + 60 min, and only the staged copies are deleted.
  - Every download also gets a 640 px JPEG thumbnail (q85) at `thumbs/<viewpoint_id>/<UTC date>/`, named like its frame (`lookout_common.thumb_key`).
  - The capture job never deletes anything under `frames/`, `archive/` or `thumbs/`. Retention after M5 (PRD §15.2) and takedowns (PRD §15.5) are separate deletions, each approved by the owner.
- **Measured** (`lookout-capture storage --day 2026-11-19` at 22:38 UTC, on the 105 frames of 06:30–22:23 UTC, at 96 downloads a camera a day, the 4-an-hour cap):

| Camera | Policy | MB a day |
|---|---|---|
| `rainier.paradise-mountain` | every frame | 100.8 |
| `point-reyes.beach` | every frame | 55.4 |
| the other six | the frame nearest each hour | 13.3–21.1 each |
| **All 8** | | **262.0**, against 451.9 for every frame everywhere |

- **Over 90 days:** 23.6 GB, against 40.7 GB for every frame and PRD §8.7's ~12 GB. A re-run at 23:58 UTC over every frame of 19 Nov gave 269.9 MB a day (24.3 GB per 90 days).
- **Rainier is the big one:** its frames average 1,687 KB with the Sun up (n=6), against 825 KB over all 32.
- **Why:** design P scores live frames only, and Rainier's can never be backfilled. The six design-H cameras have an archive.
- **State:** the policy is in capture 0.2.0, in the working tree. The running loop is the frozen 0.1.0 snapshot (D-030): until the lead re-snapshots, it keeps every frame at all 8 cameras and makes no thumbnails. The disk had 28 GiB free of 460 GiB at 00:50 UTC on 20 Nov.

## D-029 — One NPS fetcher: archive backfill runs inside the capture loop · decided (lead) · 2026-11-19 · amends D-023
- **Decision:** archive backfill runs only in the capture loop's idle time between live passes (`lookout-capture loop --archive-queue PATH`), in the same process and under the same host-wide lock. It never runs as a second process.
- **Rules in the code** (capture 0.2.0):
  - one request at a time, at least 5 s apart (the `nps-archive` limiter);
  - a request starts only if it fits before the next live pass is due (the limiter's wait + 30 s + 5 s);
  - **one back-off for `www.nps.gov`:** an HTTP 429 or 503 from a live poll or an archive request holds both, and a hold is never shortened;
  - the held-out guard (D-019, D-022), a request budget per UTC day (default 1,000), a cap per viewpoint and a 5 GB total cap (PRD §15.3); resumable.
- **Evidence** (a fake clock, no network): after a live 503 asking for 30 min, the first build let 89 archive requests out during the hold; this code lets 0. After an archive 503, the next live pass is held with 0 HEADs.
- **State:** not running. The frozen loop (D-030) predates it, and no queue file exists.

## D-030 — Local stand-ins for the capture and forecast jobs until AWS is live · decided (lead; running) · 2026-11-19 · amends D-014
- **What runs on this laptop** until the owner deploys (D-014):
  - **Capture:** `lookout-capture loop --interval 300`, all 8 cameras, from 12:24:23 UTC on 19 Nov.
  - **Forecast:** the forecast Lambda handler every 5 minutes, with a light profile: 7 fields (VIS, cloud ceiling, cloud base, LCDC, MCDC, HCDC and terrain), f00–f02, at most 40 MB a run. First tick 12:18 UTC; 20.8–22.3 MB of GRIB for each run it fetched on 19 Nov (the forecast builder's 08Z smoke run fetched 25.4 MB in 12 GRIB requests).
  - Both run from a frozen snapshot of committed code (`~/lookout-data/cache/runtime/cfefa79`), with third-party packages from the repo's venvs.
  - Only the capture loop runs under `caffeinate -s`, which keeps the whole Mac awake while it is on AC power. The forecast tick has no sleep assertion of its own and stays awake through the loop's (`pmset -g assertions` and `ps`, 01:48 UTC on 20 Nov).
- **Limits:**
  - `caffeinate -s` keeps the Mac awake only on AC power. On 19 Nov the Mac ran on battery from 06:13 to 22:48 UTC. From its start to 00:41 UTC on 20 Nov, the loop lost about 92 of its 139 expected passes (8.2 of 12.3 h) in 4 sleeps. PRD_PROGRESS.md lists them.
  - The forecast tick slept too. Every hourly run of 19 Nov (00Z–23Z) was still written by 23:56 UTC: 00Z–11Z as catch-up at its first ticks (12:18–12:26, except 08Z and 10Z, written earlier by the builder's smoke tests), and four later runs 1.5–2.4 h after publication (13Z, 15Z, 16Z and 19Z). A late fetch makes that run's design-P availability late (D-027).
  - Repo edits reach the jobs only when the lead re-snapshots, so state, log and file formats stay backward compatible.
  - The light profile leaves out `PRES:cloud base` (D-026).
- **Ends** when the AWS jobs are live.
- **Amended 20 Nov (D-076):** both jobs are children of the lead's session, and the lead's supervisor is written but not started. Since 02:39 UTC on 20 Nov they have run from snapshot `034c46b` with their own venvs (D-049), the forecast with 8 fields (D-050); the Mac was on battery again from 03:29 to 06:27 UTC (D-051).

## D-031 — Working around iCloud on this Mac · decided (lead) · 2026-11-19
- **Problem:** the repo is in iCloud-synced `~/Documents` (D-017). iCloud sets the macOS `hidden` flag on files in the venvs, and Python 3.13 skips hidden `.pth` files, so `import lookout_common` fails outside pytest.
  - Measured on 19 Nov: 8,393 hidden entries in `workers/common/.venv`, and 6,173 in `workers/forecast/.venv` (173 at build time). Common's `.pth` was hidden again after being cleared.
  - `UV_NO_EDITABLE=1` is no fix: it ran a stale cached wheel (`cli.py` from 04:02 while the source had changed at 05:59).
- **Decision:**
  - Python commands run with `PYTHONPATH=src:../common/src` (`src:../workers/common/src` from eval), and each project's pytest settings put its sources on the path.
  - Rust builds use `CARGO_TARGET_DIR=$HOME/lookout-data/cache/cargo-target`. Terraform's 735 MB provider cache is `$LOOKOUT_DATA_DIR/cache/terraform-plugins`.
  - CLAUDE.md carries both.
- **Recommendation to the owner:** move the repo out of `~/Documents` (or keep the venvs outside it with `UV_PROJECT_ENVIRONMENT`). That ends the workarounds.

## D-032 — M1 preparation started early, code only · decided (lead) · 2026-11-19
- **Decision:** three M1 inputs start during M0.5, as code only and with no M1 gate claim:
  - the M1 1a platform spike app, `spikes/m1-platform/`;
  - the Swift speech library, `app/speech-macos/` (C ABI);
  - the owner runbooks for the M1 checks.
- **The real app shell waits** for the spike's results (PLAN M1 1a: stop and report if any check fails).
- **State at 00:50 UTC on 20 Nov:**
  - The spike is built: 105 unit tests pass, and its four release binaries build. None of the 11 checks has run. They need the owner at the keyboard, and some need a permission.
  - The speech library and the runbooks are not in the repo; their work packages returned no report.
  - Checks 6 (Swift partials) and 7 (network silence) need the speech library first.

## D-033 — M1 1a spike: pass criteria fixed before any run · decided (spike code; the owner runs the checks) · 2026-11-19
- **Rule:** every check's criterion is fixed in code before the check runs, and every result line prints it. Results are reported as measured, failures included (PLAN M1 1a).
- **Criteria** (after the spike's adversarial review, 19 Nov):
  - **Check 1, hotkey release:** a release is key-first or modifier-first by whether the modifiers are still held when the handler runs, with a 20 ms simultaneity band. Without `--key-state`, a modifier-up trigger inferred from the instructed order is counted apart, with its basis. The guided run asks until both orders are observed, at most 30 trials.
  - **Check 6, Swift partials:** key-up → finish and finish → FINAL are reported beside release → FINAL (the D-004 measure). A FINAL before the finish call fails the trial.
  - **Check 7, network silence:** a byte from the spike or a speech-recognition process fails. A byte from a speech-adjacent or general daemon makes the run inconclusive, never a pass, and a Wi-Fi-off run decides it. Processes are named by their executable, through their pid.
  - **Check 8, window-only capture:** the evidence is the desktop-independent window filter and the tag decoded at the frame's corner. `contentRect` is logged but not judged until a run shows its unit (TODO(M1)).
  - **Check 9, injection:** latency counts from the clock read just before each `CGEventPost`, an upper bound. A handler call outside every trial's window fails the join.
  - **Check 10, clock agreement:** it fails if `Instant` is not `CLOCK_UPTIME_RAW`, if drift exceeds the bracket + 1 µs, if a frame arrives more than 1 ms early, if the median lag is outside 0–250 ms, or if the median-of-pairs slope is outside 1 ± 0.01. Late frames are reported.
  - **Check 11, re-consent:** the run file keeps the signing identifier, CDHash and signature kind, never the certificate's subject (the owner's name) or the team id.
- **The thresholds are the spike's own;** PLAN M1 1a gives none.
- **Open:** PRD §1 row 14 and PLAN M1 list Input Monitoring for the harness, but check 9's `CGEventPost` also needs Accessibility.
- **Measured without any permission:** Rust's `Instant` is `CLOCK_UPTIME_RAW` on this Mac (3 of 3 runs).

## D-034 — AWS infrastructure choices in the Terraform (not applied) · decided (infra work package) · 2026-11-19 · amends D-014
- **Budget and hard stop:** a $3 monthly budget (PRD §17's hosting cap), with emails at 33, 67 and 100 % of actual spend and when the month's forecast passes 100 %. At 100 % of actual spend, an AWS Budgets action attaches a deny-invoke policy to the scheduler role (`budget_hard_stop`, on by default). AWS resets it each month. D-035 asks the owner about the sealed window.
- **Forecast every 5 minutes** (`rate(5 minutes)`, PRD §11.2), not the first build's 10.
- **Keys per job:** each job writes only its own prefixes, `state/<job>/` and `state/locks/<job>*`, and deletes only its own locks (plus `cache/` for forecast). A test asserts that the two jobs' writable keys never overlap.
- **Versioned data bucket:** old versions are kept 30 days, and 1 day for `capture_log/`, `forecast_log/` and `cache/`, because every logged record rewrites the day's log (about 0.8–4.8 GB of old versions a day).
- **Region rule:** `deploy.sh` takes the region only from `TF_VAR_region` (default us-east-1), and refuses before any AWS call when the AWS CLI points elsewhere.
- **Test pinning:** every test file pins every variable to its default, and a static check fails if a pin and its default disagree, so a `*.auto.tfvars` file or an exported `TF_VAR_*` cannot change the results.
- **Kept from the first build:** hashicorp/aws `~> 6.73` and Terraform `>= 1.11, < 2.0.0`; S3-native state locking; reserved concurrency 1 and 0 retries per job; a two-phase deploy that creates the budget, the alarm email and ECR first; immutable image tags.
- **Evidence:** `make test-infra` 82 passed, 0 failed (static 5, root 47, bootstrap 4, scripts 26), and 23 of 23 targeted mutations caught. Nothing is planned against an account or applied.
- **Amended 21 Nov (D-087):** the budget now counts costs before credits and refunds (`include_credit` and `include_refund` set to false), and still counts tax, so the hard stop fires on usage even while credits pay the bill. Not applied.

## D-035 — Hosting: the $3 cap read as monthly; the hard stop in the sealed window · proposed (for the owner) · 2026-11-19
- **Reading:** PRD §17's $3 hosting cap is a monthly cap, as the infra README and D-014 read it.
- **Estimate** (the infra package, 19 Nov; computed from AWS list prices and from request counts read in the workers' code; nothing has run on AWS): about **$1.60–2.81 a month** with the free tiers, and $3.13–8.35 without them. PRD §17 and D-014 say "≈ $0–1".
  - The largest line is S3 requests, $1.52–1.89: every logged record is a whole-log GET and PUT. Batching each capture pass's records into one append would save $0.37–0.75 a month.
  - D-028's storage is above the estimate's 2–12 GB: 23.6 GB after 90 days, about $0.54 a month at $0.023 per GB-month (arithmetic, not a measurement).
- **Recommendation:** turn the hard stop off for the sealed window, 15 Dec 2026 – 14 Jan 2027, and keep the email alerts. A halt at $3 would stop capture and forecast for the rest of that month, and sealed-window data can't be recovered (Rainier has no archive). The deploy flag is `-var budget_hard_stop=false`.
- **Needed from the owner, before the first deploy:** confirm the monthly reading and choose the hard-stop setting.
- **Amended 21 Nov (D-087, D-088):** the budget counts costs before credits, with tax (D-087), so on an account with credits the $3 hard stop still fires on usage: about day 25 of month 1 at the top of D-086's estimate. Whether the $3 is before or after tax joins the questions above. AWS's spend limits cannot stand in for the hard stop: their minimum is $20, and the account that gets Activate credits cannot have one (D-088).

## D-036 — Persistence (b) beyond a 24 h lead · proposed (for the owner) · 2026-11-19
- **Conflict:** PRD §13.6 defines persistence (b) as the frame at valid − 24 h, and PRD §13.1 says baselines see only frames available at the issue time. Beyond a 24 h lead the first breaks the second. Even at exactly 24 h, the first build read a frame from 5 min after the issue time.
- **Implemented reading** (eval): (b) is the frame at valid − 24·k h, with k = ceil(lead / 24 h), chosen only from frames available at the issue time. k = 1 up to 24 h. Predictions with k > 1 carry the flag `valid-minus-<24k>h`.
- **The alternative:** no (b) forecast beyond 24 h.
- **Evidence:** the leakage audit over random issue times, with leads up to 48 h, finds no read after the issue time.
- **Needed from the owner:** confirm it, or choose the alternative, before the 14 Dec freeze.

## D-037 — Frame gates: a camera-fault class and a usable-frame gate · decided (eval and capture; v−1 thresholds) · 2026-11-19
- **Additions to PRD §8.6:**
  - **Camera fault:** a frame is `camera_fault` when at least 95 % of its analysis pixels are ≤ 4/255 or ≥ 251/255, or its whole luma range is ≤ 1/255. It is not `in_cloud`, it has no state, and no baseline uses it.
  - **Usable frame:** a new gate that fails on a camera fault or on capture's `decode_failed` or `tiny_file` flags.
- **Gates not built yet are reported, not passed:** Same view, Clear lens and the Colour-mode cut-off are "not checked" (TODO(M2)), and their counts are published with the exclusions.
- **Capture records the inputs:** the Sun position with a `sun_below_3deg` flag (Daylight); a `monochrome` flag below a chroma spread of 1.0; a Laplacian-variance sharpness and the dHash distance to the previous frame, both without flags.
- **Evidence:** before the class existed, black and white frames scored as "hidden, in cloud". Two foggy midday frames read 2.4 and 2.7 for sharpness, against 31–2,074 for other midday frames, so a sharpness floor would exclude the fog K2 measures; none is set.
- **Night mode in the manifests** (a factual correction, 19 Nov): `infrared` at Grand Canyon, Grand Teton, Yosemite, the Smokies and Big Bend, from night frames with a chroma spread of 0–0.27. Acadia stays `unknown`: its only dark frame (the Sun at −8.9°) was in colour.
- **Amended 23 Nov (D-100):** Same view is now checked in eval's extractor v0, with v0 values (TODO(M2)): the plate's base registered onto the frame within 4 px, and not checked below an NCC of 0.3. None of Rainier's 105 daytime frames failed it. Clear lens and the Colour-mode cut-off are still "not checked", and Tier 0 does not apply Same view yet (D-097).

## D-038 — The held-out split file (fixed, not yet in git), and how eval reads it · decided (eval, in code); readings proposed for the owner · 2026-11-19
- **The split:** `eval/src/lookout_eval/heldout_periods.json`, version 1, SHA-256 `c437544eecdd7a0d33b67c2374e1d388ebe45c36aeca4b2923b39c0d1d31de83`.
  - It encodes D-019, D-022 and D-024: design P's sealed window; design H's summer 2026 without 15 Jul 2026; Grand Teton out of scored metrics; the 1 Jun 2026 archive guard.
  - It supersedes the builder's `e8194fff7793…`; only two descriptive texts changed. It has never been committed, so the version stays 1.
  - **Not in git yet** (untracked on 20 Nov). PRD §13.2 needs the split committed before any held-out frame is touched, so it is the first file for the lead to commit.
- **Enforced in eval:**
  - A `FrameStore` takes only the packaged split, `committed_split()`: the file above, beside the module. Held-out frames are read only through `SealedScoring(<40-hex pre-registration commit>)`.
  - The quarantine folder is refused in any letter case, after resolving links, and by folder identity.
  - The archive guard also reads `ImageCreateDateTime` as local standard time. It can only hold out more, and only in the hour before local midnight from 1 Jun 2026 on.
  - A record counts as archived on any one sign: its key, its source, or NPS archive times in its sidecar.
- **Readings for the owner to confirm by 14 Dec:**
  - an outage extension applies to both Rainier and Point Reyes;
  - design-P fits at Point Reyes may use frames from after summer 2026;
  - 15 Jul 2026 is excluded by the valid or truth local day;
  - the three rules added in review: the standard-time reading, the three archive signs, and the quarantine refused in any case and by identity.
- **Evidence:** at the held-out fix, eval 262 passed, 1 skipped (318 passed, 2 skipped on the working tree at 01:46 UTC on 20 Nov); 47 of 48 mutants caught (the survivor is equivalent). On the real catalogue (metadata only), nothing on disk is held out: 85 archive records and 105 live frames. Capture's guard does not yet use the standard-time reading; a 12,000-case fuzz shows eval is never looser.
- **Still to do:** fits inside one camera epoch (D-024's usable spans) need split version 2 (TODO(M2)).
- **Amended 20 Nov (D-070):** capture's guard has read `ImageCreateDateTime` as local standard time since D-047 (`df1838a`), so the line above that it doesn't no longer holds. Both guards now also read the UTC date (capture 0.2.1 and eval, uncommitted): until the owner confirms the local-day reading, a frame is held out if its local or its UTC date is on or after 1 Jun 2026. The lead removed the six queue entries dated 31 May, whose evening frames carry 1 Jun UTC stamps. The split file's own text still describes the older rule (D-070).
- **Amended 21 Nov (D-085):** held-out split version 2 (`heldout_periods_v2.json`) corrects the text only; version 1 stays byte for byte (SHA-256 `c437544e…`) for the records that cite it. Fits inside one camera epoch now need version 3 (TODO(M2)).

## D-039 — Parser rule readings where PRD §7.4 is silent · proposed (for the owner) · 2026-11-19
- **In the code now** (lookout-core; each pinned by table cases):
  - **"tonight"** with a bare hour: 12 is midnight, 1–3 is after midnight and 4–11 is PM. 4 and 5 carry an AM/PM chip whose alternative is the next morning. Asked before 04:00, "tonight" means the night in progress.
  - **Dates:** "the Nth" with no month is the next such day, so "the 31st" asked on 19 Nov is 31 Dec. A month and day without a year is the nearest such date, so a recent one reads as past.
  - **The same day:** "today", the weekday or the date said alone, naming today, gives 12:00 while that is ahead, else now.
  - **"next friday"** is the first Friday at least one day ahead (on a Thursday, tomorrow).
  - **"tomorrow"** alone is 12:00.
  - **A lone generic word** ("grand", "mount", "point", "big", "south") is a miss that lists the three closest places, not a close call or a match.
- **Also in the code** (from the review, within PRD §7.4): during the repeated DST hour, an ambiguous time takes its second occurrence once the first has passed; an AM/PM alternative in the past or more than 7 days ahead is not offered; words left over beside a place name go to the fallback (`Unrecognised`).
- **Evidence:** the parser table holds 295 cases in 16 categories, all passing, and 19 of 19 mutants of the new rules are caught.
- **Needed from the owner:** confirm or change them. What the rules refuse goes to the M3 fallback.

## D-040 — Query JSON: four fields after the PRD's ten · decided (core; additive to PRD §7.3) · 2026-11-19
- The first ten fields are exactly the PRD §7.3 example; a field-by-field test checks it.
- Then come `place_source`, `place_match` {`stage`, `score`}, `horizon`, and `flags` (`live`, `rolled_to_tomorrow`, `rolled_to_next_hour`, `rolled_to_next_week`, `dst_gap`, `dst_fold`, `gfs_horizon`).
- Solar rules add `event`, `sun_altitude_deg` and `interval` to `time_source`.
- `place_match.score` can be 1.0 on a miss for a generic word, so it is not a confidence. Confidence itself stays a fixed heuristic until the replay set calibrates it (TODO(M3)).

## D-041 — Synthetic renderer goldens in the repo · proposed (for the owner; already committed in b5bc726) · 2026-11-19
- **Proposal:** keep `renderer/goldens/` in the public repo as an exception to CLAUDE.md's rule against renders and plates in the repo. It holds a procedural plate, sky mask and depth image and three golden PNGs: 148 KB of PNGs (156 KB with the scenes and the manifest), no camera pixels.
- **Already in git, before any sign-off:** all 10 files under `renderer/goldens/` (the 6 PNGs, 3 scene files and a manifest) were committed in `b5bc726` (renderer 0.2.0, 12:11 UTC on 19 Nov). That set aside CLAUDE.md's data rule ("never write … plates, renders … inside the repo") without the owner's word. Nothing has been pushed.
- **Why:** the golden-image tests (PRD §9.5; M2's goldens in CI) need committed references, and these contain no NPS pixels.
- **Guards in code:** `pnpm render` and the library refuse outputs inside the repo (links resolved), and `goldens:update` refuses any scene that is not the synthetic viewpoint and plate.
- **Needed from the owner, before or with the permission to push** (PRD_PROGRESS.md, owner action 4): sign it off or decline.
  - If declined, the goldens move under `$LOOKOUT_DATA_DIR` and CI makes its own.
  - The lead must then also remove them from git history before the first push. A later commit that only deletes them would still publish them, inside `b5bc726`.
- **Amended 21 Nov (D-080):** the first push (`23c0543`, 00:48:32 UTC on 21 Nov) published `b5bc726`, so the goldens are public and the history step above has lapsed. The sign-off is still the owner's. If the owner declines, a later commit moves them under `$SNOW_GLOBE_DATA_DIR`, and they stay in the public history.
- **Amended 23 Nov (D-098):** renderer 0.3.0 regenerated the three goldens for its new sky; the synthetic plate files came out byte-identical. The 6 PNGs are now 149,624 bytes, still with no camera pixels. Committed in `5e50a9a` and pushed at 11:13:14 UTC on 23 Nov (`git reflog`), so these are public too. The sign-off is still the owner's.

## D-042 — Grand Canyon's 3:2 epoch holds two aims · decided (evidence) · 2026-11-19 · amends D-024
- **Correction:** D-024 lets Grand Canyon's 3:2 epoch (Dec 2020 – Jan 2024, "one framing") serve development and backtests. A full-frame registration of the 15 Jan 2021 and 15 Jan 2024 hires (both 999×666) shows the camera re-aimed between them: the content moved 51 px right and 30 px up, 5.1 % of the width and 4.5 % of the height, at the same scale.
- **Consequence:** inside one epoch, references, τ and landmark boxes are fitted across dates only after a full-frame registration against the plate (the Same view gate, M2). The contact sheets and dHash behind D-024 can't see a shift this small.
- **Evidence:** `docs/evidence/m05-backtest-pair.md`, section 2. Gradient NCC is 0.45 aligned against −0.01 unaligned, and the shift holds over 5 windows × 3 analysis widths (+48.9 to +52.0 px across, −29.1 to −30.5 px up). Moving the pair's primary box by the shift moves its ratio from 0.330 to 0.360, across τ_lo.
- **D-024 otherwise stands.**

## D-043 — The National Mall has an NPS air cam; it stays out of the capture set · decided (correction) · 2026-11-19 · amends D-020
- **Correction:** D-020 and PRD §8.2 say the National Mall has no NPS air cam. The NPS ARD webcams feature layer lists one: site `WASH`, at the Netherlands Carillon (38.89, −77.07), monitoring since 1 Jul 2003.
- **Decision** (the lead): it stays out of the capture set, which already holds 8 cameras (PRD §1 row 3: up to 8).
- **Source:** the scaffold builder's read of the NPS Air Quality Monitoring Map feature layer (ArcGIS FeatureServer, layer 9) on 19 Nov. Not re-checked since, because this run makes no nps.gov requests.

## D-044 — The viewpoint manifest's shape is the schema's · decided (in code since 0bb42ab; PRD Appendix A redrawn to match, to be confirmed) · 2026-11-19 · amends D-020
- **Correction:** D-020 points to PRD Appendix A for the manifests. The schema committed in `0bb42ab`, `viewpoints/schema/viewpoint.schema.json`, has another shape, and it is the one the loader and every worker read:
  - the pose fields sit in a top-level `pose` object (null until M2), not under `camera`;
  - landmarks form a top-level `landmarks` array, and `plate.landmarks` is refused;
  - the hourly cap is `truth_camera.polite_caps.max_images_per_hour`, beside `poll_interval_s`;
  - it adds `schema_version`, `place_id`, `status`, `notes`, `camera.view_azimuth_source`, `truth_camera.interval_min_nominal`, `view_label` and `image_px`.
- **Enforcement** (common's second-round fix, in the working tree and not yet committed): the loader refuses Appendix A's old spellings (camera pose fields, `plate.landmarks`, `polite_caps.max_per_hour`), naming the new place. It also checks landmark bearings (within 0.5°), `archive_since` against `archive_code`, and alias collisions between places.
- **PRD Appendix A** is redrawn to the schema's shape in v0.4.1. That changes the spec's text, not only a fact, so it is listed as a spec update and waits for the lead's or the owner's confirmation. The alternative is v0.4's sketch (`ab96af0`) with a note that the schema differs.

## D-045 — A third overlap with the stopgap: the scaffold build's nps.gov requests · decided (record; D-023's fix covers it) · 2026-11-19 · amends D-023
- **What happened:** at about 07:25 UTC on 19 Nov, while the Rainier stopgap was polling, the scaffold work package made 9 nps.gov requests:
  - 6 `Range: bytes=0-65535` GETs of ARD images (grca, grte, yose, acad, grsm, bibe), through `polite_client`, at least 6 s apart. Yosemite ignored the range and sent 2,520,399 bytes, held in memory only.
  - 3 page loads (the Rainier webcams page, the Grand Canyon air-webcam page and the Yavapai museum page). They did not carry the Lookout User-Agent.
- **Rule broken:** CLAUDE.md at `e4722a6`: never run two NPS fetchers at once. The stopgap's log shows `rate_capped` HEADs at 07:20:35 and 07:25:35 and frames stored at 07:30:36 and 07:35:36. Rainier's 4-an-hour cap was not touched.
- **Record:** no log of these 9 requests exists under `~/lookout-data`; the builder's report is the only record. D-023 lists the other two overlaps.
- **Since then:** the host-wide lock (D-023) refuses any second `lookout-capture` fetcher. Page loads by other tools are outside that lock, so in this run no agent makes nps.gov requests at all (the lead's instruction).

## D-046 — The second workflow's work committed per package; forecast's survey-lag change held back · decided (lead) · 2026-11-20
- **Verified by the lead and committed,** 02:37:06–02:42:36 UTC on 20 Nov, one commit per package:
  - `4be0eca` common: review fixes for the shared scaffold;
  - `df1838a` capture 0.2.0 (D-028, D-029), with the lead's two fixes (D-047, D-048);
  - `d9aa416` eval: the held-out guard (D-038) and the M0.5 backtest pair v−1;
  - `c9ba843` build: Makefile, CI and docs checks;
  - `b73c53b` the M1 1a spike (D-032, D-033);
  - `6c385f5` infra: the Lambda image smoke test;
  - `034c46b` docs: D-025 to D-045, PLAN.md, PRD_PROGRESS.md, PRD v0.4.1 and the notices;
  - `8246a8b` CLAUDE.md: runtime snapshots with their own venvs, offline capture commands, data layout.
- **Held back:** forecast's unfinished survey-lag change. It failed `make test` (one test and a ruff E501) and had no review (the second workflow's critic). This workflow finished it (D-053, D-054).
- **What the commits settle:**
  - The held-out split is in git: `eval/src/lookout_eval/heldout_periods.json`, version 1, SHA-256 `c437544e…` unchanged, first committed in `d9aa416` (02:37:23 UTC on 20 Nov). D-038's "not in git yet" no longer holds.
  - PRD §13.2's order (split committed before any held-out frame is touched) is met only for split v1's held-out set, as D-022 redrew it. Split v1 leaves 15 Jul 2026 out of scoring at the seven ARD cameras, because frames of that day were downloaded and viewed on 19 Nov, before any split existed (D-022). For summer 2026 as D-019 first drew it, the order was not met.
  - The M0.5 pair's evidence file is committed in `d9aa416`.
- **Still uncommitted** (10:45 UTC on 20 Nov): this workflow's packages, which D-047 to D-063 describe. Only the lead commits.
- **Amended 20 Nov (D-064):** the third workflow's packages were committed the same way, one commit per package, at 12:35–12:36 UTC, then its planning docs at 12:45 (`08a996c` to `8459e7e`). The "still uncommitted" line above is out of date from there.

## D-047 — Capture's held-out guard also reads ImageCreateDateTime as local standard time · decided (lead, in df1838a) · 2026-11-20 · amends D-022
- **Change:** `heldout.asset_local_dates` adds a fourth reading, the civil date of `ImageCreateDateTime` read as local standard time. An archived asset's date is the latest of the four; an asset with no readable date is refused. Eval's guard already read it so (D-038).
- **Why:** the second workflow's critic found capture's guard looser than eval's at the summer edge. A stamp of "05/31/2026 11:30 PM" with no readable `LocalTimeString` was dated 31 May and allowed, though it is 00:30 PDT on 1 Jun. The archive queue refuses only days on or after 1 Jun, so a 31 May entry reaches that edge.
- **No frame was affected.** No archive request was sent between the archive check's last one (10:40:52 UTC on 19 Nov) and this fix. The next was the queue's first, at 09:52:25 UTC on 20 Nov, from the fixed code (`capture_log/`).
- **Evidence:**
  - Capture and eval agree on all 2.36 M random cases (`workers/capture/README.md`, the 20 Nov review).
  - The backfill plan's end-to-end run of 31 May with no Sun limit: Point Reyes and Acadia refused their 23:00–23:45 standard-time frames without a request, and Grand Canyon (Arizona keeps standard time) stored all 96 ([docs/research/archive-backfill-plan.md](docs/research/archive-backfill-plan.md), section 9).
- D-038's line "capture's guard does not yet use the standard-time reading" no longer holds.
- **Amended 20 Nov (D-070):** both guards now also read the UTC date (capture 0.2.1 and eval, uncommitted). Until the owner confirms D-038's local-day reading, an archived frame is held out if its local or its UTC date is on or after 1 Jun 2026.

## D-048 — Polls came up to 0.94 s early on 19 Nov; polls are now spaced send to send · decided (lead in df1838a; capture 0.2.1 in the working tree) · 2026-11-20 · amends D-023
- **Breach:** CLAUDE.md allows a poll at most every 5 minutes. On 19 Nov, 8 of 504 per-camera poll gaps were 299.06–299.7 s, measured on send times (log time − `elapsed_ms`): Grand Teton, Point Reyes, Rainier ×2, the Smokies ×2 and Yosemite ×2, at 12:29 and 23:05–23:16 UTC (the second workflow's critic). The 4-an-hour cap held: at most 4 image GETs in any rolling hour at every camera.
- **Cause:** `last_poll_at` was stamped before the request waited about 1.1 s on the host's request spacing, so the 300 s check ran on stamps, not on sends.
- **Fix 1, running** (the lead, `df1838a`; live since 02:39:39 UTC on 20 Nov): a poll is stamped at the send, after the spacing wait, and the next is due 300 s plus a 1 s margin later. A new test reproduces the 293 s gap on the old code.
  - **Live check:** from 02:39:39 UTC on 20 Nov to the pass that ended at 10:45:05, 70 polls per camera; the shortest gap between sends was 301.053–301.064 s, none under 300 s (`request` records in `capture_log/2026-11-20.jsonl`).
- **Fix 2, in the working tree** (capture 0.2.1, this workflow's capture review; not committed, not running):
  - **Problem:** on a schedule that fires exactly every 300 s (the Lambda's `rate(5 minutes)`, or `loop --jitter 0`), the 1 s margin makes each poll about 1.05 s later than the last, until the camera skips an invocation. Fake clock, Lambda schedule, 8 cameras, 24 h: 48 skipped invocations (6 per camera), longest gap 555.9 s, passes up to 63.1 s long (mean 37.9 s).
  - **Change:** a 2 ms margin (`POLL_MARGIN_S`, for the millisecond stamp). A pass waits for a camera whose slot is near, inside its lock and right before its poll (Lambda 45 s, `loop` 60 s). Each image GET's record moves to its send time for the rolling-hour cap (`reserved_at` keeps the reservation).
  - **After:** 24 h: 0 skips, gaps 300.001–300.539 s, passes 18.5 s on average (max 20.0 s). 10 days with a 20 ms storage delay: 0 skips, longest gap 300.726 s. `loop` for 72 h with 0 s and 30 s jitter: no gap under 300 s, no skip.
  - **Cost:** capture's billed waiting on Lambda falls from 35.7 s to 14.6 s a pass (D-060).
- **Open, for the lead before committing 0.2.1:** the server sees a request after connection set-up and transit. The first request of a pass took a median 102.6 ms against 65.9 ms for the others (`workers/capture/README.md`). So a gap seen at nps.gov can be tens of ms shorter than ours, which a 1 s margin absorbs and a 2 ms margin does not.
- **Amended 20 Nov (D-065):** the open question is settled by the lead's answer-time rule. A poll is due only at the previous poll's answer + 300 s + 2 ms, and the image cap counts GETs answered in the last 3,600 s, so the gaps hold at nps.gov whatever the network does. Fix 2's send-to-send margin is replaced; capture 0.2.1 implements the new rule (uncommitted, not running). Fix 1, 0.2.0's 1 s margin, still runs.

## D-049 — Re-snapshot at 034c46b with the runtime's own venvs; capture 0.2.0 is live · decided (lead; running) · 2026-11-20 · amends D-030
- **The snapshot:** committed code at `~/lookout-data/cache/runtime/034c46b`, with its own venvs. It is outside iCloud, so its jobs need no `PYTHONPATH`. The running jobs no longer use the repo's venvs, so `make test` and `uv sync` in the repo are safe. That closes the critic's risk that a sync would swap packages under the running loop.
- **Capture loop:**
  - stopped by SIGTERM at 02:39:33 UTC on 20 Nov, after its last pass at 02:37:12;
  - restarted at 02:39:39 UTC on capture 0.2.0 (`df1838a`) as `lookout-capture loop --interval 300 --archive-queue ~/lookout-data/state/archive/queue.json`, under `caffeinate -s`;
  - the queue file was empty; the defaults hold: a budget of 1,000 archive requests per UTC day and a 5 GB total cap.
- **The restart's gap:** the new loop's first pass, at 02:39:39, found all 8 cameras not yet due (persisted state). The next polls came at 02:45:04–02:45:15. Each camera's gap between polls was 483–486 s, about 3 min beyond one interval.
  - The lead counts this as a short outage for the 72 h clock: 183–186 s beyond one 300 s interval.
  - The data report (D-061) counts an outage only for a gap over 540 s between passes, and the passes were 147.5 s apart (02:37:12 → 02:39:39), so its clock figures include none for the restart. D-061 asks the lead to settle which counts.
- **Forecast stand-in:** restarted on the snapshot, still lookout-forecast 0.1.0, with the profile of D-050. Its loop started at 02:39:48 UTC (`ps`), and its first fetch at 02:39:49.5 (`forecast_log/`). No log records when the old loop stopped. No run fell due in the gap: 2026112001 had been written at 01:57:09, and 2026112002 was due at 02:55:14 and written at 02:55:30.
- **What is live now** (capture 0.2.0):
  - D-028's storage: every frame at Rainier and Point Reyes, the frame nearest each UTC hour at the other six, and a 640 px thumbnail of every download;
  - the shared www.nps.gov hold and the store-failure back-off;
  - the archive queue inside the loop (D-029; first tranche in D-052).
  
  D-028's and D-029's "State" lines are out of date from here.
- **The switch kept one far frame for one hour** (`workers/capture/README.md`). For hour 03:00 UTC, Acadia, Grand Canyon, Grand Teton and Yosemite kept 0.1.0's 02:30:00 frame, 1,800 s from the hour, as the hour's only full-size frame. Their 02:45, 03:00 and 03:15 frames survive only as thumbnails. A frame whose sidecar has no `hourly_selection` comes from the every-frame code, and eval should not read it as the frame nearest its hour.
- **Amended 20 Nov (D-076):** the jobs this entry restarted are children of the lead's Claude Code session (parent pid 72719, through zsh), so ending that session can stop them. The lead's supervisor, written but not started, would restart them in their own session once they exit.
- **Amended 20 Nov (D-079):** the lead re-snapshotted capture to `3cd2ca7` (capture 0.2.1) at 19:44–19:56 UTC; the forecast stand-in still runs from `034c46b`. A restart now holds its own sleep assertion across the stop and the start.
- **Amended 23 Nov (D-090):** both jobs now run from `aee6f92`, built by the rename's switch. Every snapshot build runs `uv sync --locked --no-dev --offline`, which needs every locked wheel in uv's cache. On 22 Nov that cache had been emptied, and the switch's first rehearsal failed offline before it stopped anything. So the offline build is checked in a scratch folder before a loop is stopped.

## D-050 — The forecast stand-in keeps PRES:cloud base: 8 fields, f00–f02 · decided (lead; running) · 2026-11-20 · amends D-030
- **Profile from 02:39:49 UTC on 20 Nov:** VIS, cloud ceiling, cloud base, `PRES:cloud base`, LCDC, MCDC, HCDC and terrain, at f00–f02, at most 40 MB a run. D-026 asked for `PRES:cloud base`; the default field set already keeps it.
- **One re-extraction:** because the field set changed, the 12 h lookback was extracted again from NOAA's open bucket at 02:39:51–02:50:26 UTC.
  - 12 runs, 2026111914 to 2026112001 (the lead's note said about 13), at 23.9–25.0 MB of GRIB each, 293.5 MB in all.
  - Their `run` and `lag` events were logged a second time. The lag model keeps the first event per (run, fh). The data report sets aside the 12 re-extractions and 160 duplicate lags.
- **Since then:** 8 new runs, 2026112002 to 2026112009, each with 8 fields and 23.8–25.1 MB of GRIB, all under the 40 MB cap (`forecast_log/2026-11-20.jsonl`, read at 10:39 UTC).
- D-026's and D-030's notes that the stand-in leaves `PRES:cloud base` out no longer hold.
- **Amended 23 Nov (D-101):** this profile serves only design P's 1 h lead. Its newest hour, f02, is valid 61–66 min after S3 publishes it, so its files give a lead of about 1 h at most, and none of 3, 6, 12, 24 or 48 h. The AWS deployment is set to extract every hour, which would serve the leads to 24 h (no run gives 48 h, D-102); if it is not live by 15 Dec, the stand-in needs a design-P profile.

## D-051 — The Mac was on battery again from 03:29 to 06:27 UTC on 20 Nov; on AC since · decided (record; the owner keeps it on AC) · 2026-11-20 · amends D-030
- **The lead's note** says the Mac has been on AC power since 22:48 UTC on 19 Nov. The power log (`pmset -g log`, read at 10:40 UTC) shows otherwise:
  - on AC from 22:48:22 UTC on 19 Nov;
  - **on battery from 03:29:09 to 06:27:36 UTC on 20 Nov** (100 % falling to 85 %). The lid closed at 03:52:37 UTC (clamshell sleep, 95 %), and the Mac idle-slept from 04:08:22 UTC, with dark wakes;
  - on AC from 06:27:36 UTC; at 10:38 UTC on AC, 100 %, charged.
- **Cost:** 4 capture outages, 04:10:03–06:29:29 UTC, 1.99 h lost (D-061). The forecast stand-in wrote run 2026112004 1,387 s late.
- `caffeinate -s` holds the Mac awake only on AC power (D-023, D-030). Since 06:27:54 UTC a `caffeinate -dims -t 28800` also holds the display and the system awake, until 14:27:54 UTC (`pmset -g assertions`, 10:45 UTC).

## D-052 — Design-H archive backfill: the plan, its budget, and the first tranche queued · decided (lead; running) · 2026-11-20 · amends D-029
- **The plan:** [docs/research/archive-backfill-plan.md](docs/research/archive-backfill-plan.md), 20 Nov, revised the same day after its review. It reads metadata on disk only; nothing was fetched for it.
  - **Frames:** full-size `hires` (999 px wide; the extractor works at 960 px), the frame nearest each local hour with NPS Sun ≥ 2°, in whole ISO weeks.
  - **Order:** T0 (date the 3:2 → 16:9 switches, and D-024's Big Bend and Grand Canyon view checks), T1a (a week in each summer month), T1b (shoulder months), T3pr (Point Reyes's plate inputs for M2), T2 (every remaining summer day, Point Reyes first), T1c (winter weeks).
  - **Size:** 1,971 day entries, about 24,600 requests, 22,700 frames and 1.23 GB (0.24–2.30 GB).
  - **Not queued:** Rainier (no archive), Grand Teton (no design H, D-024), Grand Canyon's 3:2 epoch (D-024, D-042), and anything on or after 1 Jun 2026. Four layers refuse that: `make_entry`, the queue runner, a four-date check on every asset (D-047), and eval's frame store.
- **Budget and caps:** keep 1,000 requests per UTC day, 5 GB in total and the default 1.0 GB per viewpoint.
  - The NPS email has not been sent and the API is undocumented, so raise the budget only once NPS says yes to bulk collection.
  - At 1,000 a day: the stratified sample (T0–T1b) and T3pr finish on 29 Nov, T2 on 10 Dec and T1c on 15 Dec, provided the loop runs at least 2.2 h every UTC day.
  - That time model ran on the real queue runner with a fake clock: 1.76 h of loop time per 1,000 requests without errors, 2.10–2.11 h at 19 Nov's error rate (3.7 %).
- **Queued by the lead** at 09:50:30–09:50:37 UTC on 20 Nov: the first tranche as written, T0 and T1a.
  - 447 entries: 134 midday entries (T0) and 313 hourly ones (T1a), dated 2023-01-30 to 2026-05-31.
  - The first archive request went out at 09:52:25 UTC.
- **Progress at 10:39 UTC** (`lookout-capture archive queue list --json`, offline):
  - **T0 done:** 134 of 134 entries, 256 requests, 134 frames, 8.6 MB of frames. The plan expected 256 requests and 7 MB.
  - **T1a:** 12 entries done and 1 active; 198 requests, 184 frames.
  - **In all:** 454 of the day's 1,000 requests, with 16.0 MB of frames and 6.7 MB of search answers. One archive error, a TLS connect timeout at 10:37:10 UTC.
- **The day's budget, spent at 11:43:23 UTC** (`capture_log/2026-11-20.jsonl`; the queue listed offline at 11:47):
  - 1,000 requests in 1.85 h from the first, with no loop outage between: 990 answered 200, and 10 failed in transit (6 `hires` connect timeouts and 4 searches), each followed by a wait of 30 s or more. 52.7 MB in all, 44.2 MB of them frames.
  - The plan's time model gives 1.76 h without errors and 2.10–2.11 h at 3.7 % errors; 1.0 % of these requests failed.
  - After three failed searches, `big-bend.panther-junction/2025-06-14` was marked failed at 11:30:05 UTC. Whether to resume it is the lead's call.
  - The queue then held 180 entries done, 1 active, 1 failed and 265 pending, waiting for the next UTC day's budget.
- **T0's answer** (sidecar frame heights only; no image opened). The last 3:2 frame (666 px tall) and the first 16:9 frame (562 px), by local date:

| Camera | Last 3:2 | First 16:9 |
|---|---|---|
| `point-reyes.beach` | 27 Mar 2023 | 10 Apr 2023 |
| `acadia.mcfarland` | 12 Aug 2023 | 26 Aug 2023 |
| `yosemite.turtleback` | 22 Aug 2023 | 5 Sep 2023 |
| `big-bend.panther-junction` | 2 Dec 2023 | 16 Dec 2023 |
| `smokies.look-rock` | 11 Mar 2024 | 25 Mar 2024 |
| `grand-canyon.yavapai` | 24 Apr 2024 | 20 May 2024 (the archive has no frames between) |

  - Every switch falls inside the archive check's bracket, and each is earlier than the start of the plan's bulk span at that camera. Big Bend's span starts in Jan 2025 for another reason: D-024's check of its "framed lower" 2024-01-15 frame.
  - A frame's height shows only the switch of aspect ratio, not a re-aim (D-042). Big Bend's and Grand Canyon's view checks still need eval's registration.
- **Point Reyes 2023, a reading of D-024.** The tranche includes Point Reyes's weeks of 17–23 Jul and 14–20 Aug 2023. They lie inside D-024's "Jul 2023 →" span, though D-024's note names summers 2024 and 2025 for development. The lead queued them as written. T0 now dates Point Reyes's 16:9 epoch from between 27 Mar and 10 Apr 2023. The owner may change this before the 14 Dec freeze.
- **Next, from the plan:**
  - extend the spans where T0 shows an earlier switch, and generate T1b, T3pr, T2 and T1c again;
  - ask eval to register the Big Bend and Grand Canyon check frames;
  - queue T1b while T1a runs, then T3pr's six clear days once T1a has landed.
- **Archived HRRR for these dates is the forecast package's job:** 361 development dates through T2 (424 with T1c), about 96 GB of transfer at the 24 h lead through T2 (113 GB with T1c), deleted after extraction.
- **Amended 20 Nov (D-070 to D-072):** the lead removed the six 31 May entries and resumed Big Bend's failed entry, so the queue holds 441 entries. The plan was regenerated from the live queue: T3pr goes ahead of T1b, and the dates before D-024's spans form TQ for the owner instead of extending the spans. Against this entry's finish days, at 1.0 % errors, T3pr moves 4 days earlier (25 Nov), T1b (30 Nov) and T2 (11 Dec) a day later, and T1c stays on 15 Dec (16 Dec at 3.7 %), assuming the lead resumes failed entries every day.

## D-053 — Forecast 0.2.0: survey lags in the model, `tick`, explicit hours · decided (forecast package; uncommitted, not running) · 2026-11-20 · amends D-027
- **Survey lags** (`lookout-forecast lag-survey --log`) are written only by that command, as `"method": "survey"` events in `forecast_log/lag_survey/<day>.jsonl`, under the lock `state/locks/forecast-lag-survey.lock`. A survey never logs an hour already logged, live or survey. Where both exist the live lag wins; both enter the p95.
- **The real survey,** 03:11:48–03:12:41 UTC on 20 Nov:
  - 194 runs (2026111200 to 2026112001), 0 incomplete, 4,676 listed hour-lags;
  - 4,082 survey events logged; the 594 hours already logged live all had equal lags (0.0 s apart);
  - 194 listings, 3,160,172 bytes, by the survey's own ledger, no GRIB. Its saved pages (`--save-listings`) number 197, 3,181,611 bytes: it first listed 2026112003, 2026112002 and 2026112001 to find the newest complete run, then listed 2026112001 again at the end, and the ledger leaves those three out. 0.2.0 re-uses the end search's listing, and reports `transfer_with_end_search` beside `transfer`.
- **Lags** (median / p95 / max): f00 51.36 / 60.52 / 63.28 min; f06 62.77 / 74.45 / 115.92; f12 73.91 / 86.22 / 131.17; f18 85.24 / 98.15 / 115.68; f48 107.57 / 116.65 / 117.48 (n = 33). 3 lags above 120 min; 1,847 later than the forecast.md curve.
- **The model:** 576 of 576 cells are now off the fallback, all by rule 2 (436 on live lags alone). No cell has the 20 lags rule 1 needs (8–9 each). TODO(M1): survey again now and then, from the last surveyed run or earlier.
- **Lag records are checked strictly:** the run is a `YYYYMMDDHH` string, the hour an integer of 0 or more, and the lag a number from 0 to 24 h. Anything else is counted as invalid and not used. The real log has 0 invalid records.
- **`tick`:** one call of the Lambda handler, the code path the scheduled Lambda runs. It exits 0, or 1 if a run failed in the call, or 4 if every failure was a cap refusal; the JSON is unchanged.
- **Also:**
  - an explicit hour list (`ExtractOptions.hours`, `run --hours`);
  - one listing per extracted run;
  - a field-set change that is not a superset now leaves stored runs `kept` at no cost. On 0.1.0 it re-fetches 3 runs every tick and writes nothing: 18 range requests and 138,159 bytes a tick, reproduced on HEAD;
  - `lag-survey` lists the end run once.
- **Versions:** the package is 0.2.0, and `schema_version` stays 1, because eval's reader accepts only 1. Live files written by 0.2.0 validate under the 0.1.0 schema; archived files do not (2 errors, 4 when the stamps win, D-054).
- **Evidence:** 177 passed, 1 network test deselected; eval's pair contract 13 of 13; an offline replay of the survey with the final code gave identical events.
- **Not running:** the stand-in runs 0.1.0 from `034c46b` until the lead re-snapshots.

## D-054 — An archived HRRR hour is never available before its own S3 stamps · decided in code (forecast package; uncommitted); the PRD §8.3 change is proposed for the lead · 2026-11-20 · amends D-027
- **Finding** (this workflow's forecast review): with survey lags, every fallback cell moved to rule 2, which rests on November 2026 lags. For archived runs of other seasons the model could then be earlier than the run's own S3 stamps, so a run could be chosen for an issue time before it was published (PRD §13.9 leakage).
  - **Reproduced:** run 2025071500 f46 was modelled available at 01:49:36Z, but its stamps read 01:54:30Z (GRIB2) and 01:54:34Z (`.idx`). `select_stored_run` chose it at an issue time of 01:52Z.
  - **Scale:** 18 archived runs were listed (2015-07 to 2026-03; 442,353 bytes of listings, no GRIB). In the 14 runs that keep both stamps, 49 of 686 hours were published after the model's time, by up to 6.37 min (2021031512 f32).
- **Decision:** an archived hour's availability is the later of the model and the floor its trusted S3 stamps set.
  - The floor is the later of the hour's GRIB2 and `.idx` `LastModified`, as for a live run.
  - A stamp counts only within 24 h of the run start (`STAMP_TRUST_H`). With only the GRIB2 stamp trusted, the floor is that stamp + 300 s; with neither, the model alone.
  - Where the stamps win, the source is `archived-s3-lastmodified`, and `availability_model` records `s3_floor_utc` and `s3_stamps_ignored`. `validate_document` refuses an archived time before its floor.
  - `listed_availability(bucket)` applies the floor to a run not stored yet.
- **Evidence:**
  - With the floor, 0 of the 686 hours are early, and 91 hours take their stamp.
  - Rewritten stamps exist: every `.idx` of 2020120312 and 2021011500 is stamped 17 Feb 2021, and both objects of the 2015 and 2018 runs carry 2020–2021 stamps. Published stamps fall 46.6–131.2 min after the run start (4,688 hours of 195 November runs).
  - The `.idx` came 3–188 s after its GRIB2 (median 4 s) in 5,374 hours, and never before it.
  - The M0.5 pair's run choice is unchanged by the floor.
  - **Re-checked** from the listings the forecast package saved (scratch folders of this workflow; 197 November pages and 18 archived runs, 442,353 bytes): 4,688 hours of 195 November runs at 46.6–131.2 min, 686 hours of 14 archived runs at 49.9–116.5 min, and the `.idx` 3–188 s after its GRIB2 over all 5,374. The fixer's reproduction script, run on the final code at 11:46 UTC, gives 2025071500 f46 its `.idx` stamp, 01:54:34Z, and `select_stored_run` at 01:52Z chooses nothing. `select_run_for_issue` with no listing still gives the model's 01:49:36Z, which is why eval should pass `listed_availability` (below).
- **The same 24 h rule for the lag log:** the survey never logs an hour with a stamp more than 24 h after the run start, and readers count such a lag as invalid. One rewritten stamp would otherwise push its cell to 33–75 days.
- **For the lead:**
  - eval's `pair_forecast.choose_run` should pass `listed_availability`, or choose among stored files with `select_stored_run`;
  - PRD §8.3's "modelled as run start plus the live-measured p95 lag" may want the floor added. That is a spec change, not a fact, so v0.4.1 leaves it.
- **Risks:** a real publication delay of over 24 h would be read as a rewrite (none seen; the largest is 131.2 min). The 300 s margin is empirical (largest gap seen 188 s).
- **Amended 20 Nov (D-073):** eval's `choose_run` now passes `listed_availability`, so the backtest pair's run choice applies the floor; the M0.5 pair's choice is unchanged.

## D-055 — Speech library 0.2.0: the C ABI, a stable-text event, and symbol checks · decided (speech package; never run on the recognizer) · 2026-11-20 · amends D-015
- **What:** `app/speech-macos/`, a Swift static library with a C ABI (`include/lookout_speech.h`). It runs engine A1, SpeechAnalyzer with `DictationTranscriber`, on device only. `lookout-transcribe` replays a WAV through the ABI; its timings are diagnostics, never the D-004 or PRD §10.6 measure.
- **ABI beyond the spike's seven prototypes** (all additive):
  - `lk_speech_set_contextual_strings`: the viewpoint aliases of PRD §7.1, passed to `AnalysisContext.contextualStrings`;
  - `lk_speech_set_silence_pad_ms`: lever 1, default 0 ms (D-056);
  - `lk_speech_set_model_retention`;
  - named `LK_SPEECH_*` codes;
  - **`LK_EV_STABLE = 4`:** the finalized part of the hypothesis, with its audio range. It comes right after the PARTIAL of the same result, or alone when a tentative result is finalized unchanged, and its text is always a UTF-8 byte prefix of the current PARTIAL.
  
  Consumers ignore kinds they don't know. The spike maps 4 to `Unknown` and is unaffected.
- **Why a separate event:** PRD §7.1's `Partial` carries `stability`, and lever 2 and §10.1 act on a stable partial. A byte count in `error_code` would collide with the status codes and show non-zero codes on partials in check 6's logs.
- **Bounds in code:**
  - `lk_speech_cancel` waits for at most one chunk of 4,096 samples, during the silence pad too;
  - the converter's input copy is zeroed once consumed;
  - a refused push sets an overflow flag that is never cleared;
  - `lk_speech_free` must not overlap any other call on the session.
- **Static checks:**
  - One deny list (`scripts/forbidden-symbols.txt`) covers undefined symbols and Objective-C selectors.
  - An exact, reviewed allow list (`scripts/archive-imports.txt`: 400 imports and 39 selectors) fails on any change until it is reviewed.
  - A self-test must reject 20 crafted archives.
  - These catch accidents, not deliberate evasion; spike check 7's network monitor stays the run-time evidence.
- **Tests:** a C initializer puts a trap engine behind the recognizer for the whole test process, so no test reaches the Speech framework or a prompt. `lookout-transcribe` is signed ad hoc as `dev.lookout.transcribe`, with its Info.plist bound.
- **Evidence** (`scripts/build.sh`, 20 Nov):
  - 103 tests pass, also in `make test` at 10:31 UTC;
  - push p99 2.71–5.12 µs over six runs, 0 of 30,000 pushes over 1 ms; start p50 0.74–0.96 ms;
  - 0 callbacks after cancel in 1,800 sessions, and 0 audio fed after cancel in 1,200;
  - cancel waits 1.26–1.73 ms during a backlog, and 5.2–13.5 ms during a pad at 10 ms per buffer;
  - `liblookout_speech.a` is 907,616 bytes, and its 10 exported `_lk_speech_*` symbols are identical to 0.1.0's.
- **Not known until the owner runs it:**
  - whether `progressiveShortDictation` finalizes while the key is held; if not, STABLE arrives just before FINAL and lever 2 gains nothing;
  - which identity AssetInventory reservations and TCC key on (TODO(M1));
  - whether a session runs with Speech Recognition "not determined" and no prompt. Apple's documentation says SpeechAnalyzer's transcribers don't need that permission (TODO(M1)).
- **Not in it:** the Foundation Models parser fallback (TODO(M2)).
- **Amended 20 Nov (D-074):** the lead rebuilt the spike against this library at 12:36 UTC, so checks 6 and 7 run on 0.2.0. The silence pad's default is decided in D-067: 0 ms, set and logged by the shell and the harness.
- **Amended 21 Nov (D-080):** the rename changes the ABI's names, not its shape or its numbers. The header is `include/snow_globe_speech.h`, the functions are `sg_speech_*`, the codes and event kinds `SG_*` (`SG_EV_STABLE = 4`), the modules `SnowGlobeSpeech`, `SnowGlobeSpeechC` and `SnowGlobeTranscribeCore`, and the archive `libsnow_globe_speech.a`. The tool is `snow-globe-transcribe`, signed as `dev.snowglobe.transcribe`; the spike is `dev.snowglobe.spike-m1`. The figures above are 0.2.0's, under the old names. The spike, the only consumer, now links the new names; its speech build is redone after the post-gate switch. Permissions and asset reservations would key on the new identifiers. As far as the record shows, none was granted to the old ones: the library has never run against the recognizer.

## D-056 — The silence pad defaults to 0 ms until the bake-off measures it · proposed (for the lead) · 2026-11-20
- **Why it needs a decision:** PRD §7.1's finalization lever 1 (a burst silence pad at key-up) is to be "measured, not assumed". The library's default is 0 ms, so a caller that never sets it runs with lever 1 off. The spike never calls `lk_speech_set_silence_pad_ms`, so its check-6 trials run without a pad.
- **Proposal** (the speech package): keep 0 ms as the library's default. The M1 shell and harness set the pad explicitly and log it with each trial. The bake-off measures the pad, model retention and a launch-time warm-up.
- **Needed from the lead:** confirm, or choose another default before the bake-off.
- **Amended 20 Nov (D-067):** decided by the lead as proposed. The library's default stays 0 ms; the M1 shell and the harness set the pad explicitly and log it with every trial; the bake-off measures it. The spike's check-6 trials run with the pad off, which is fine for a platform check.

## D-057 — The M1 replay script: 130 utterances, with owner-pending rows reported apart · decided (bench package; nothing recorded) · 2026-11-20
- **The script:** `bench/utterances/scripted.tsv`, 130 rows (30 dev, 100 test). Every one of the 77 aliases is said, and the script hash, over id, split and text, is `3b4256f1cf51`. There are 6 reference clocks.
  - The expected results come from PRD §7.4–§7.5 and D-039, generated by `prd_reading.py`.
  - `check.py` reruns `lookout-parse`. It exits 1 on drift or a stale stored verdict, and 2 on a missing or stale binary.
- **Split discipline:** test rows are looked at only in measured runs. The three already seen are named (t036, t074, t075). The text-level number before any parser change is recorded: 94 of 100 exact.
- **Owner-pending rows** (d01, d02, d29, t035, t036, t075) keep the reading's guess and are reported apart until the owner rules on three rule questions:
  - a name close to an alias that only fuzzy matching finds;
  - a place name plus a leftover word;
  - a lone generic word.
  
  The builder's row-level parser asks ("tacoma" as an alias or a refusal; a "yosemite valley" alias) are withdrawn, because they would tune the test set.
- **Follow-ups:**
  - each comes straight after a row that resolves to its context place, which `check.py` enforces;
  - the scorer re-parses at the context the app actually used (an id in the trace), never at the context column;
  - t100 is played first after a launch.
- **Proposed for the lead's scorer:**
  - "exact place and time" without the owner-pending rows, with the denominator named;
  - normalised WER applies the same spoken number forms (`check.py --reading-list`) to both sides.
- **Consent:** the scripted set is opted in once per version of the script, and real-use recordings one at a time.
- **Format:** a recording keeps the device's own channel count and rate, recorded in its sidecar. The harness refuses a fixture that differs from the open stream (TODO(M1)).
- **Evidence:**
  - 40 bench tests;
  - against the reading, the parser gets 94 of 100 test rows exact, 97 counting soft differences (94 of 97 and 96 of 97 without the three pending test rows);
  - solar times within 1 s of an independent Meeus Sun on 28 rows;
  - clock-based times identical on 86 of 86 rows.
- **Open, for the lead before the first overnight run:**
  - whether idle passes keep the display awake (`caffeinate -di`, or the Lock Screen setting) or let it sleep and report those trials apart. This Mac turns its display off after 10 min on AC, and every idle trial follows 30 min without a query;
  - whether follow-up trials may be idle. PRD §7.4 sets no expiry for the last place asked about.
- **Amended 20 Nov (D-068):** both open items are decided by the lead. Idle trials keep the system awake (`caffeinate -i`, never `-d`) and let the display sleep as the Mac's settings do; each trial logs the display state, and display-asleep trials are reported apart as well as inside the totals. Follow-up rows may be idle trials, reported as idle follow-ups.
- **Amended 23 Nov (D-096):** the scorer is built (`bench/replay/score.py`, `5fdfb3e`), so far on synthetic trials only. The lead decided the two proposals above as built: exact place and time leaves out the owner-pending rows and names its denominator, and normalised WER follows D-096's rule. The re-parse runs at the context the trace records (trace schema 2, D-095).

## D-058 — More parser readings for the owner · proposed (for the owner) · 2026-11-20 · amends D-039
- **From the replay script** (D-057). The parser and the reading agree on each; the owner confirms or changes them with D-039, by 14 Dec:
  - dawn and dusk are civil twilight, Sun at −6° (PRD §7.4 has no rule for them);
  - "tonight" asked after it has passed means the next whole hour;
  - an hour said with "tonight" that has passed goes to tomorrow, as §7.4 row 2 says literally: "Rainier tonight at 9" asked at 21:30 is tomorrow at 21:00 (t030);
  - a part of the day, or noon, that has passed today means tomorrow's, though §7.4 states that roll only for an hour said with AM, PM, "tonight" or "this morning";
  - "live" means now;
  - "tomorrow" is the place's tomorrow (§7.4, "local to the place"). Asked at 23:30 in Chicago, "Acadia tomorrow at 9" is Saturday (d20);
  - the 48 h and 7-day horizons count from now. Counted from the newest usable model run (PRD §8.3), t012 would get the GFS caption, and t022 and t083 would be out of range or at the limit.
- **Undefined:** what "the last place asked about" is after a miss. The script no longer depends on it.
- The three rule questions of D-057 go to the owner as well.

## D-059 — AWS: capture's S3 grants for D-028, a 35-day cache expiry and an offline provider install · decided (infra package; not applied) · 2026-11-20 · amends D-034
- **Grants:**
  - capture reads and writes `frames/`, `thumbs/`, `cache/staging/`, `capture_log/`, `state/capture/` and its locks;
  - capture deletes only `cache/staging/` and its own locks;
  - forecast loses its `cache/` grant, because its GRIB subsets stay on the function's `/tmp`.
  
  Without the change, capture 0.2.0 on Lambda would have lost the six hourly cameras' frames: a medium finding of this workflow's capture review.
- **Pinned by tests:** `lambda.tftest.hcl` checks the grants against the keys the worker code builds. It pins each role's whole policy, exactly four statements, so an added statement cannot widen a grant.
- **`cache/` expires after 35 days, not 7.** It is a backstop for staged copies capture never deleted. It outlasts the longest hard stop (one budget month), because a stopped capture job decides its last hours only when it runs again.
- **Offline provider install:**
  - `infra/tf_providers.sh` installs hashicorp/aws 6.73.0 from the plugin cache (`init -plugin-dir`), with no network, when the cached package's `h1:` hash is in the lock file;
  - otherwise the default mode falls back to the registry; `--offline` never contacts it;
  - Terraform runs with `CHECKPOINT_DISABLE=1`;
  - CI on linux_amd64 always falls back, because the lock files hold the darwin_arm64 hash only.
- **Evidence:**
  - `infra/test.sh` passes every check: static 6, root `terraform test` 50, bootstrap 4, the offline provider path 22, scripts 26, cost 5, and the build-context check (22.5 s);
  - it also passed under a sandbox that denied outbound connections;
  - 41 targeted mutations were caught: 24 in the policies, and 17 in the cost estimate, the no-email check and the tflint step.
  
  Nothing is planned against an account or applied.
- **Lambda smoke, run 3** (06:59 UTC on 20 Nov, image `lookout-lambda:8246a8bd570b` built from `8246a8b`, 293.8 MB compressed):
  - init 232 ms;
  - first call 2,696 ms, extracting 2 runs (7,254,750 bytes from NOAA);
  - second call 38 ms;
  - peak memory 144.8 MiB.
  
  These are laptop numbers under the Runtime Interface Emulator, not AWS.

## D-060 — Hosting estimate redone on 8246a8b: $2.07–4.03 in month 1 with the free tiers · proposed (for the owner, with D-035) · 2026-11-20 · amends D-035
- **New estimate** (`infra/README.md`, "Monthly cost estimate"). `cost/estimate.py` runs on a simulation of the committed capture code: 24 h against a mock S3 bucket. Nothing is deployed, so nothing is observed on AWS.
  - **With the free tiers:** $2.07–4.03 in month 1, $2.32–4.29 in month 2, $2.53–4.52 in month 3, then about $0.22 more each month as storage grows. The bottom of the range passes $3 in month 6; the top is above it from month 1.
  - **Without them:** $5.63–9.83 in month 1.
  - D-035's $1.60–2.81 is superseded.
- **The largest lines:**
  - S3 requests, $1.87–1.92;
  - Lambda compute up to $1.81 at the top of the range. Two inputs drive it: capture's billed waiting, 35.7 s a pass on average under 0.2.0's 1 s poll margin, and forecast run times, assumed to be 30–150 s.
- **With capture 0.2.1** (D-048): the waiting falls to 14.6 s a pass, and the total with the free tiers to $2.07–2.81 in month 1 and $2.31–3.07 in month 2.
- **What it means for D-035:** at the top of the range the free 400,000 GB-s run out on day 22, and the $3 hard stop would halt both schedules around day 27 of month 1. D-035's recommendation stands: keep the alerts and turn the hard stop off for the sealed window. PRD §17's "≈ $0–1" is out of date; PRD v0.4.1's pointers now name this estimate.
- **Levers** (the workers' owners):
  - capture takes a lock and writes its state at every poll: 5,360 PUTs a day, $0.80 a month;
  - forecast re-checks its 12 h lookback with a HEAD per viewpoint: $0.33 a month;
  - old state versions are kept 30 days: about $0.04 a month.
- **Amended 20 Nov (D-066):** with capture 0.2.1's answer-time rule (D-065), the estimate with the free tiers is $2.43–4.40 in month 1 ($2.43–3.35 with a 20 s Lambda window), and the bottom of the range passes $3 in month 4 instead of month 6. The 0.2.1 line above (14.6 s of waiting, $2.07–2.81) came from a zero-latency simulation of the earlier 0.2.1 and no longer holds.
- **Amended 21 Nov (D-086):** re-priced for capture 0.3.0 at `8fa6b6a`, with measured request times, a steady-state window and six seeds: $2.57–4.51 in month 1 with the free tiers, before tax.

## D-061 — The M0.5 data report, and how the 72 h clock is counted · decided (eval's counting rules; the clock rule is proposed for the lead) · 2026-11-20
- **Tool:** `lookout-eval data-report [--since] [--until] [--json]` (`eval/src/lookout_eval/data_report/`). It reads logs, state and the disk, and writes nothing. It builds the gate's table: runs and frames per viewpoint, outages and exclusions (PRD §16, M0.5). The second workflow's critic found no tool that did.
- **Counting rules:**
  - **Capture:** a gap of more than 540 s between passes is an outage. The lost time is the gap less one 300 s interval.
  - **Forecast runs:** a run counts at a viewpoint where that viewpoint's first live write of it falls inside the window. A later write is a re-extraction. Catch-ups (runs due before the window) are counted and listed apart.
  - **Forecast lateness** is judged on `fetch_finished_utc` (D-027). A run is due when its last extracted hour is on S3. It is late, and the span is an outage, when no live run finished within 900 s of the due time (`forecast_gap_s`). The outage runs from the due time to the finish of the next live run written; time is lost from due + 300 s, so lost = length − 300 s.
  - **Cycles never written** are dated by the window's median lag, else the median over every complete run in the logs, else 120 min. With no live run in the logs, the whole window is lost.
  - **Read at run time:** state files are read as they are when the report runs. Archive work, the backtest pair's archived run, survey lags and duplicate lags are listed as exclusions. Every path is written with `~` for the home directory.
- **The clock:**
  - The headline is PLAN.md's rule: the capture loop's outages extend the clock.
  - The union of both jobs' outages is shown beside it, labelled "proposed, not a recorded rule".
  - At 10:30 UTC on 20 Nov: capture 10.21 h lost, so the earliest close is 22:36:51 UTC on Sun 22 Nov. Both jobs: 10.43 h, 22:50:09 UTC. Both close later if anything more is lost. A re-run to 11:42 UTC gave the same figures.
  - **The 540 s threshold is the report's,** not PLAN.md's. Under it the 02:39 restart (D-049) loses nothing: its passes were 147.5 s apart, though each camera went 483–486 s between polls. Counted as the lead counts it, a short outage of 183–186 s beyond one interval, the capture close moves to about 22:40 UTC (22:39:54–22:39:57), and the both-jobs close to about 22:53.
- **Needed from the lead:** choose the rule and record it here, in two parts:
  - which outages count: the capture loop's (PLAN.md) or both jobs';
  - whether a restart counts: the report counts only gaps over 540 s between passes, so the 02:39 restart adds nothing; counted from each camera's last poll to its next, less one 300 s interval, it adds 183–186 s.
- **Evidence:**
  - 40 data-report tests;
  - 14 of 14 mutants killed (the eval fixer's `mutate.py`; re-run on a scratch copy of the working tree at 11:43 UTC on 20 Nov: baseline 53 passed, 14 of 14 killed);
  - an independent standard-library script matched all three clocks to the millisecond (06:33:54 UTC cut).
- **Amended 20 Nov (D-069):** the lead chose the rule: the union of both jobs' outages plus each capture restart's excess. The report's headline is now that rule (JSON `schema_version` 2), with PLAN.md's capture-only figure beside it. To 15:46:36 UTC on 20 Nov: 10.48 h lost, so the earliest close is 22:53:14.871 UTC on Sun 22 Nov (PLAN.md's figure: 22:36:51.258).

## D-062 — oculus-core provenance: records now name the build that ran · decided (eval; uncommitted) · 2026-11-20 · amends D-025
- **Change:** eval's `OculusBackend.version` reads the commit from the installed `oculus-core-py`'s PEP 610 `direct_url.json`, giving `0.1.0+1b5410b`. When only the pin is known it adds `.pinned`.
  - `OCULUS_PINNED_REV` is `1b5410b…`, and a test keeps it equal to `pyproject.toml` and `uv.lock`.
  - `app/core/src/solar.rs`'s comment names `1b5410b`.
  - That resolves D-025's "Left over".
- **The four M0.5 pair records** keep `oculus-core 0.1.0+06809e6`, as written, because they are measured outputs. The evidence file's §9 says the build that ran was `1b5410b`:
  - its `direct_url.json` names that commit;
  - it was built at 12:31:59 UTC on 19 Nov and has been in `eval/.venv` since 22:08 UTC at the latest, both before run 1 (23:25 UTC).
  
  No number changes, because `ephem.rs` is identical in both commits.
- **Also in eval:** its dev group adds `eccodes` 2.50.1 and `eccodeslib` 2.50.0.34, the forecast worker's locked versions. So the pair's real-extractor test now runs in `make test` instead of skipping, and an import failure fails it.

## D-063 — `make test` runs ten suites; CI runs all but the laptop-only speech suite · decided (integration package; uncommitted) · 2026-11-20
- **Suites:** common, capture, forecast, eval, renderer, rust, speech, infra, docs and bench (`TEST_SUITES`).
  - Every Python suite runs `ruff check`, `ruff format --check` and pytest behind a dead proxy.
  - `speech` (app/speech-macos, `swift test`) is in `LAPTOP_ONLY_SUITES`: it needs macOS 26 and Xcode 26.2, and CI has no macOS job.
  - Adding a macOS runner is the owner's call, because it bills at a higher rate on a private repository.
- **The docs checks gain three:**
  - `notices`: THIRD_PARTY_NOTICES names every third-party package that a `uv.lock`, `Cargo.lock` or `package.json` pins, at its version. It reads neither the two Terraform lock files nor Lookout's own package versions, which are kept by hand;
  - `appendix-a`: PRD Appendix A has the manifest schema's shape (D-044);
  - `ci-suites`: CI runs exactly `make test`'s suites, less the laptop-only ones.
  
  That makes 16 checks and 85 tests.
- **Evidence:**
  - `make test` passed all 10 suites, 10:30:57–10:34:41 UTC on 20 Nov (224 s): common 258, capture 191, forecast 177, eval 367 (1 skipped), renderer 183, rust 108, speech 103, infra 113, docs 85 and bench 40;
  - with dependencies cached, all 10 suites also passed under a sandbox that denied every outbound connection and DNS (223 s; the Makefile's header).
- **Amended 20 Nov (D-075):** `make test` runs capture's slow tests on purpose (4 of them since capture 0.2.1); `make test-speech` also runs `scripts/build.sh --no-test`; a separate `make bench-script` checks the script against the real parser. CI still runs every suite but `speech`.
- **Amended 23 Nov (D-092):** `make test` runs 12 suites since `22930cc`: plates and overlay joined, and bench also runs bench/replay's 93 tests. All 12 passed in 652 s on wave 1's committed tree (09:43–09:54 UTC on 23 Nov). CI gains a plates job and an overlay job, so it runs 11 jobs, every suite but `speech`. They first run on GitHub on wave 1's push at 11:13:14 UTC (`git reflog`), whose result was not read for these docs.

## D-064 — The third workflow's packages committed one per package, then the planning docs · decided (lead) · 2026-11-20 · amends D-046
- **Committed by the lead** on branch `m0-prd`, in the third workflow's critic's order (git's times, UTC on 20 Nov):
  - `08a996c` forecast 0.2.0: survey lags, `tick` and the archived stamp floor (D-053, D-054), 12:35:35;
  - `1a39820` eval: the 72 h data report, oculus-core provenance and the backfill planner, with the lead's `sent_at` reader (D-061, D-062), 12:35:45;
  - `e59247a` infra: capture's IAM grants, the offline provider install and the cost estimate (D-059, D-060), 12:35:51;
  - `8443bf3` speech-macos 0.2.0 and `lookout-transcribe` (D-055), 12:35:57;
  - `dc28c2a` bench, the M1 owner runbook and the README skeleton (D-057), 12:36:06;
  - `6b94942` docs: the archive backfill plan, the archive check's correction and the NPS email's figures (D-042, D-052), 12:36:06;
  - `951b704` build: `make test` and CI for speech, bench, infra cost and the docs checks (D-063), 12:36:19;
  - `8459e7e` docs: D-046 to D-063, PLAN.md, PRD_PROGRESS.md, the PRD and the notices, 12:45:03. Before committing, the lead corrected the "linked only against a stub" wording in PLAN.md (M1 1a), PRD_PROGRESS.md (M1-01) and the spike README (check 6): the spike's 03:39 UTC build linked the real library 0.1.0 (D-074). `make test-docs` then passed its 85 tests.
- **Held back:** capture 0.2.1, until D-048's margin question was settled. D-065 settles it.
- **Uncommitted at about 16:00 UTC:** capture 0.2.1 (D-065, D-066); eval's data report, held-out guard and pair choice (D-069, D-070, D-073); the backfill planner (D-072); the Makefile, the CI comments and CLAUDE.md's layout line (D-075); these docs. Nothing has been pushed.
- **What this changes in earlier entries:** D-053 to D-055, D-057 and D-059 to D-063 now describe committed code. The jobs still run snapshot `034c46b` (D-049), so forecast 0.2.0 doesn't run yet.
- **Amended 20 Nov (D-078):** the fourth workflow's packages were committed the same way: `b9520e2`, `7f76411`, `3f48e75` and `670138b`, then its planning docs.

## D-065 — Polls and image GETs are spaced from the previous answer, not the previous send · decided (lead; capture 0.2.1 implements it, uncommitted and not running) · 2026-11-20 · amends D-023, D-048
- **Rule** (the lead, settling D-048's open question):
  - A camera's next poll is due only when the clock has reached the previous poll's answer time + 300 s + 2 ms (`POLL_MARGIN_S`, for the millisecond stamp).
  - The answer time is one clock reading, taken when the response has arrived or the request failed.
  - The image cap counts the same way: a GET may be sent only while fewer than 4 of the camera's GETs were answered in the last 3,600 s.
- **Why:** nps.gov receives a request no later than it answers it, and receives the next no earlier than we send it. So answer + 300 s guarantees at least 300 s at nps.gov whatever the network does. A send-to-send margin can't, because a request reaches the server after connection set-up and transit (D-048).
- **The running 0.2.0, measured by the lead** (1 s send-to-send margin; `request` records of runner `cli-loop` in `capture_log/2026-11-20.jsonl`, 02:39:39–12:36 UTC):
  - 712 consecutive poll pairs over 8 cameras, all at least 301.053 s apart send to send;
  - on the provable basis (the next send minus the previous record's `t`, which is written after the answer), 1 pair of 712 is not provably 300 s apart at nps.gov: Acadia, 299.590 s. Send to send it was 301.066 s, so nps.gov saw between 299.590 and 301.275 s (eval's review). The other seven cameras' bounds were 300.869–300.979 s;
  - image GETs: in every window of 5 consecutive GETs, the 5th's send minus the 1st's answer was at least 3,720.2 s;
  - under the 2 ms send-to-send margin of D-048's fix 2, nearly every pair would be unprovable: 300.002 s less the previous request's elapsed time.
  - **Re-measured** by the docs agent with eval's data report (D-069), whose bounds widen each stamp for its millisecond rounding, to 15:46:36 UTC: since 02:39:39, 1 of 1,008 poll pairs is not provably 300 s apart (Acadia, least possible gap 299.588 s), and none of 316 five-GET windows (least span 3,720.229 s). Since 12:24:23 on 19 Nov: 10 of 1,560 pairs, the other 9 from 0.1.0 before the restart (D-048).
- **As built in capture 0.2.1** (`workers/capture/README.md`):
  - answer stamps (`answered_at`, `last_answer_at`) are rounded up to the millisecond and send stamps truncated, so each pair proves itself from the log: a poll's `sent_at` minus the previous `answered_at` is at least 300.002 s;
  - each poll is reserved and saved before its send, as each GET already was;
  - a live request not on its way 30 s after its reservation is abandoned unsent, so a request with no saved answer counts as answered 60 s after its reservation (the deadline plus the 30 s write timeout). httpx's timeouts each cover one operation, and DNS none, so they give no such bound;
  - a 0.2.0 state counts as answered at the later of its send + 60 s and the moment 0.2.1 first reads it, and its GET records from their `t` + 62 s. This is tested on a real 0.2.0 state copied from the running loop;
  - within a process, answers are also read on a monotonic clock, so a forward wall-clock step can't bring a poll early. Across a restart the exposure equals the step, which a test measures;
  - a pass waits for slots only inside its own window, counted from its start (Lambda 45 s, `loop` 60 s). Counted from each camera's visit instead, passes reached 78.1 s (p99 74.0 s) over 10 days;
  - `request` records gain `sent_at` and `answered_at`, and `archive_request` records gain `runner`. `STATE_SCHEMA` stays 1, because every new field is optional.
- **Measured on a fake clock** (the real capture code against a fake nps.gov that logs each receipt, with request times drawn from 1,024 real 0.2.0 answers; the capture package's scratch scripts):
  - **the 2 ms send-to-send rule** on the exact Lambda schedule: 1,027 of 2,296 poll pairs under 300 s at the server in 24 h, down to 296.659 s, and 11,050 of 23,032 in 10 days;
  - **this code:** 0 pairs under 300 s at the server in every run (24 h and 10 days; with a storage delay, 0–2 s of invocation jitter, or 1 % request spikes of 2–8 s). The smallest gap provable from the log is 300.002 s, and every 5 consecutive GETs span at least 3,601.0 s;
  - **`loop` for 72 h** with its default 0–30 s jitter: 0 of 6,592 pairs under 300 s (207 with the old code), and no skip.
- **The cost on Lambda's exact `rate(5 minutes)`:** each poll lands its elapsed time later than the last, so a camera sometimes falls outside a pass's window and skips that invocation, which leaves one gap of about 555–622 s.
  - 1.12 skips per camera per day over 24 h, and 1.43 over 10 days, with the measured request times; 94–95 of 96 images per camera per day.
  - The review's other models gave 1.95–2.98 (6.53 with 0.45 s median requests), 3.9 with 0–30 s of invocation jitter and 36.6 with 0–60 s. EventBridge's real jitter is measured after deployment.
  - Passes: p99 about 46–52 s. The longest reached 83.5 s when the latency pool included the log's 10 s TLS and 30 s read timeouts, inside Lambda's 95 s reserve.
  - Billed waiting: about 36 s a pass (D-066).
  - The window is one constant, `lambda_handler._WAIT_FOR_DUE_S`, 45 s until the lead chooses. 20 s gives 3.33 skips per camera per day and 17.7 s of waiting over 10 days, still with 0 pairs under 300 s.
  - **Open, for the lead before the sealed window (15 Dec):** whether design P counts an outage per pass or per camera. A skip is one camera's gap, skips cluster (in one review run, 25 of 109 passes with a skip skipped 2–6 cameras), and the data report's 540 s threshold today applies only between passes.
- **Tests:** 19 new server-side, schedule and crash tests fail on the pre-change code and pass on this code. D-066 has the final suite counts.
- **Not running:** the loop runs 0.2.0 from snapshot `034c46b`. Restarted within seconds of a pass's end, the switch costs each camera about 60–70 s more than a normal gap (the review's upgrade simulation: 359.9–370.1 s at the server), and up to about 300 s if restarted just before the next pass. D-069 counts it as restart excess.
- This supersedes D-048's fix 2, the 2 ms send-to-send margin.

## D-066 — Capture 0.2.1's guards for requests that fail, redirect or stall, and what 0.2.1 costs on AWS · decided (capture package; uncommitted); the estimate is proposed for the owner with D-035 · 2026-11-20 · amends D-060
- **Found by this workflow's two capture reviews,** each reproduced with the real capture code on a fake clock or over loopback (127.0.0.1); nothing reached nps.gov. Fixed in capture 0.2.1 (the version is unchanged):
  - **A failure that may still reach nps.gov:** a request that failed after it may have left the machine, with no response head back (a read timeout, a reset, an interrupt), counts as answered 300 s after the failure (`FAILED_AFTER_SEND_BOUND_S`), because a queue on the server side can hand it on after we gave up. A failure that proves nothing was sent (connect, TLS, pool, the send deadline), or one after a response head, is not bounded.
    - The reviewers' loopback runs put the next allowed poll 296.0–299.5 s after the server read the failed one; now 597.0–599.5 s.
    - `capture_log` holds 3 such failures in 2,065 live requests (12:24 UTC on 19 Nov to 15:17 on 20 Nov).
    - 300 s is an assumption about queues on the server side, not a proof.
  - **No redirect is followed** by the live, archive or queue clients. A 3xx is an error, logged with its `Location`, and the archive does not retry it. Before, a same-host 301 made nps.gov see 48 polls a camera against 24 logged in 2 h. Nothing suggests that nps.gov redirects these URLs today.
  - **Total deadlines:** a poll's body must arrive within 30 s of the request's start and an image's within 60 s. So a body that trickles in can no longer hold a visit past the S3 lock's 300 s lifetime (reproduced with moto: a second runner took the lock over, and the first runner's save then lost the second's poll).
  - **A rollback to 0.2.0 stays polite:** while a poll is reserved, `last_poll_at` holds the reservation + 60 s. Restarted right after a crash between send and save, the real 0.2.0 code (`034c46b`) waits 360.7 s, not 1 s.
  - **The send deadline counts system sleep:** it is checked on the wall clock and on a monotonic clock that counts sleep (`CLOCK_MONOTONIC_RAW` on macOS). On this Mac, `time.monotonic()` was 2,159,174 s behind that clock. Before, an hour's sleep between the reservation and the send let the poll go out an hour late.
  - **Also:**
    - on Lambda each request record names its execution environment (`lambda_log_stream`), because the 2 ms margin assumes that Lambda hosts' clocks agree to within 2 ms, which is unmeasured;
    - `answer_bound` records are dated when they are written;
    - `cadence` measures poll spacing send to send, and `status` says what the next poll counts from (`next_poll_basis`);
    - inside the loop, the archive queue starts at least 1 s after the last live request (the 20 Nov log has a live-to-archive gap of 0.005 s).
- **Tests:** 16 new cases. 14 fail on the pre-fix code, and mutants catch the other 2. Capture: 249 passed (4 slow deselected), and the slow set 4 passed; ruff, format and `uv lock --check` clean. eval: 403 passed, 1 skipped.
- **Left open:**
  - a conditional (`If-Match`) state write on S3, in `lookout_common.storage` (its owner), so that a holder whose lock was taken over can't overwrite the next holder's state;
  - resetting a failed socket so the kernel drops an unsent head (`SO_LINGER`), which needs httpcore's internals;
  - the Lambda hosts' clock offset, measurable only after deployment.
- **Cost against D-060** (`infra/cost/sim_capture_s3.py` on scratch exports of the code, 24 h against a mock bucket, then `infra/cost/estimate.py`; computed, and nothing ran on AWS):
  - S3 calls a day: 11,048 PUT and 10,176 GET, against 8,944 and 7,902 for the committed 0.2.0 that D-060 priced (+2,104 PUT, +2,274 GET). State and lock PUTs: 7,624, against 5,360.
  - Billed waiting with real request times is about 36 s a pass. D-048's 14.6 s came from a zero-latency simulation; with real request times its 2 ms code also crept, to 37.2 s.
  - **With the free tiers: $2.43–4.40 in month 1** at 36 s of waiting ($2.43–3.35 with a 20 s Lambda window), against D-060's $2.07–4.03. The bottom of the range passes the $3 cap in month 4 instead of month 6. At the top, the hard stop would come on about day 25.7 of month 1, not 26.7.
  - D-035's recommendation stands: keep the email alerts, and switch the hard stop off for the sealed window.
  - **A lever for the owners:** on S3, treat the takeover of an expired camera lock as the crash signal and drop the poll-reservation PUT, about $0.34 a month. It needs `lookout_common`'s S3 lock to report a takeover.
- **Amended 21 Nov (D-083):** capture 0.3.0 (uncommitted) also backs the archive limiter off after every redirect and 4xx, as after a 5xx: 30 s, doubling to 30 min. A redirect is still not followed.
- **Amended 21 Nov (D-086):** the estimate is now $2.57–4.51 in month 1 with the free tiers, before tax (each seed alone gives a top of $4.46–4.58). D-066's own inputs reproduce its $2.43–4.40 exactly; steady-state storage and request counts and the forecast recount make the difference.

## D-067 — The silence pad: 0 ms in the library, set and logged by the shell and the harness · decided (lead) · 2026-11-20 · closes D-056
- **Decision** (the lead, as D-056 proposed):
  - the library's default stays 0 ms (`lk_speech_set_silence_pad_ms`, D-055);
  - the M1 shell and the replay harness set the pad explicitly and log it with every trial;
  - the bake-off measures it, with model retention and a launch-time warm-up (PRD §7.1's finalization levers, "measured, not assumed").
- **The spike's check-6 trials run with the pad off.** That is fine for a platform check, which asks whether the library returns partials and exactly one FINAL. Its release → FINAL numbers are reported as lever 1 off.
- `app/speech-macos/README.md` still asks the lead to record this default; its owner can close that item.

## D-068 — Idle trials keep the system awake and let the display sleep; follow-ups may be idle · decided (lead) · 2026-11-20 · amends D-057
- **(a) Display sleep:** idle trials keep the system awake with `caffeinate -i`, never `-d`, and let the display sleep as the Mac's own settings do, as in real use (on AC this Mac turns its display off after 10 minutes, D-057).
  - Each trial logs the display state; the harness trace gets that field (TODO(M1), M1-09).
  - Display-asleep trials are reported apart, and also inside the totals.
- **(b) Follow-ups:** follow-up rows may be idle trials, because PRD §7.4 sets no expiry for the last place asked about. They are reported as idle follow-ups.
- **What it changes:** the first night's dry-run question, whether paint capture still works once the display sleeps, now applies to every night ([docs/m1/owner-runbook.md](docs/m1/owner-runbook.md), step 6).
- This closes D-057's two open items. The rest of D-057 stands.

## D-069 — The 72 h clock counts both jobs' outages and each capture restart's excess · decided (lead; eval's data report implements it, uncommitted) · 2026-11-20 · amends D-061
- **Rule** (the lead; the stricter reading of PRD §16's "worker and capture logs"): lost time is the union of both jobs' outages, plus each capture restart's excess. A restart's excess is the longest any camera went from its last poll before the restart to its first poll after it, less one 300 s interval, if positive. PLAN.md's capture-only figure stays beside it for comparison.
- **As built** (`lookout-eval data-report`, whose JSON is now `schema_version` 2):
  - `clock.lost_h`, `covered_h` and `earliest_close_utc` are the gate rule. PLAN.md's figure moved to `clock.plan_md_capture_only`, and `clock.by_job` gives each job alone;
  - the excess is the interval from that camera's last poll + 300 s to its next poll, unioned with the outages, so a restart that is also an outage counts once;
  - a restart splits each camera's polls at the old loop's last record (its `loop_stopped` record, else its last record under the old version and runner). A restart just before `--since` counts the part of its excess inside the window.
- **Measured** (read-only, on the live data):
  - **to 13:15:00 UTC on 20 Nov,** by the eval package, and by an independent standard-library script to the millisecond: the gate rule's 10.48 h lost, so the **earliest close is 2026-11-22T22:53:14.871Z (Sun 22 Nov)**. PLAN.md's figure: 10.21 h, 22:36:51.258Z. Capture alone: 22:39:56.728Z. Forecast alone: 18:26:42.991Z;
  - **the 02:39 restart** (D-049): 185.471 s of excess, set by Acadia (last poll sent at 02:36:58.471Z, the next at 02:45:03.942Z). Across the eight cameras it was 183.080–185.471 s, inside D-061's 183–186 s. It overlaps no other outage, so the gate close is D-061's both-jobs close (22:50:09.401Z) plus the excess, 185.4708 s unrounded: 22:53:14.8718Z, which the report prints cut to the millisecond as 22:53:14.871Z (the eval review's independent script, in microseconds);
  - **re-run by the docs agent to 15:46:36 UTC:** the same four closes; 16.89 h covered of 27.37 h elapsed; no outage since 06:29:29.
- **Polite collection at nps.gov is in the report now** (PRD §15.3). Per viewpoint it gives:
  - the least gap nps.gov can have seen between consecutive polls: the next poll's earliest receipt minus the previous one's latest;
  - the least span of every 5 consecutive image GETs;
  - how many pairs or windows are unproven: not provably 300 s (3,600 s) apart, which is not the same as shown closer.
  
  An answer read from `t` is widened by 1 ms, and a send from `t − elapsed_ms` by 0.05 ms, for the stamps' rounding. A request with no usable send time proves nothing. The figures are in D-065.
- **Evidence:** eval 403 passed, 1 skipped; 58 data-report tests; 59 of 60 mutants killed. The survivor is a `loop_stopped` record at exactly a pass boundary, which capture never writes.
- **Open, for the lead:** a camera not polled again after a restart is measured to the window's end, so a camera dropped at a restart would keep the clock from ever closing (the review's test: 9.5 h lost of a 10 h window). Today's restart is unaffected: all 8 cameras were polled again. The review suggests measuring only the cameras the new loop polls, and listing the others apart.
- **The re-snapshot to capture 0.2.1 is a restart:** about 60–70 s of excess if it is made within seconds of a pass's end (D-065).
- TODO(M1) in eval: split at a pass's wall-clock start once capture logs one; neither 0.2.0 nor 0.2.1 does.
- **Amended 20 Nov (D-078):** a camera with no poll after a restart is flagged in the per-viewpoint table and left out of that restart's excess, so one dropped camera cannot keep the clock from closing (`TODO(M1)` in eval; no figure changes).
- **Amended 20 Nov (D-079):** the 19:44 restart's excess is 763.6 s; to 20:01:58 UTC, 10.69 h are lost and the earliest close is 23:05:58 UTC on Sun 22 Nov.
- **Amended 21 Nov (D-082):** the data report splits a restart at capture 0.3.0's `loop_started` record or a pass's `started_at` when the logs hold them (D-084), which does this entry's TODO(M1), and a request's latest receipt is now the later of `answer_bound_at` and `answered_at`. No gate figure moved.
- **Amended 22 Nov (D-089):** the clock closed at 23:46:10.754 UTC on 22 Nov: 72.01 h covered of 83.37 h elapsed, 11.36 h lost (`make data-report` at 23:46:31 UTC).

## D-070 — The held-out archive edge read on the local or the UTC date until the owner confirms D-038; six 31 May queue entries removed · decided (lead, pending the owner) · 2026-11-20 · amends D-019, D-022, D-038, D-047, D-052
- **Reading** (the lead): until the owner confirms D-038's local-day reading, CLAUDE.md's "dated 1 Jun 2026 or later" means either the camera's local date or the UTC date. An archived frame is held out if any reading of its date, local or UTC, is on or after 1 Jun 2026.
  - That holds out 31 May's local evening at every ARD camera, from 00:00 UTC on 1 Jun: 16:00 in local standard time at Point Reyes and Yosemite, 17:00 at Grand Canyon and Grand Teton, 18:00 at Big Bend and 19:00 at Acadia and the Smokies. NPS stamps summer frames in local standard time (D-047).
- **The queue:** at about 12:40 UTC the lead removed the six entries dated 2026-05-31 (Point Reyes, Grand Canyon, Yosemite, Acadia, the Smokies and Big Bend; `hires`, hourly, Sun ≥ 2°), whose evening frames carry 1 Jun UTC stamps. Nothing of theirs had been requested. A copy of the queue before the edits is in the lead's scratchpad.
- **In code** (capture 0.2.1 and eval, uncommitted): capture's `heldout.asset_utc_dates` and eval's `nps_utc_dates` read
  - the instant's UTC date, under both conventions;
  - each local clock in the camera's zone: both sides of a DST fold, and D-047's standard-time reading;
  - for a date with no readable clock, every UTC date its local day touches (fail-closed).
  
  Day-level refusals stay at 1 Jun. Evidence:
  - both suites hold the same per-zone edge table, and the queue test refuses the 31 May asset stamped 10:45 PM (11:45 PM PDT);
  - eval's parity fuzz loads capture's file and checks that the two guards agree: 6,000 cases (the builder), 0 disagreements;
  - the capture review's own fuzz (`fuzz_heldout.py`, 130,000 cases over two seeds, each built from a true instant and then mutated) found 0 disagreements and allowed no asset whose true UTC or civil date is on or after 1 Jun. It refused 10,384 of the 58,238 truly allowed cases (8,004 of 44,744 in its 100,000-case run), every one with a mutated stamp: fail-closed. None of the 13,139 truly allowed cases with both stamps as NPS writes them was refused (re-run by the docs fixer on the working tree);
  - the backfill planner applies the same edge by instant: 30 May 2026 is the last plannable date at all six cameras (D-072);
  - two eval tests now encode this reading. They pass with the new `heldout.py` and fail with HEAD's, so they are committed with it.
- **Capture's guard has read the standard-time reading since D-047** (`df1838a`), so D-038's "capture's guard does not yet use the standard-time reading" no longer held from 20 Nov.
- **Open:**
  - **The running loop lacks the UTC reading:** 0.2.0 allows 31 May evening assets (reproduced in the review). Nothing is exposed, because the queue ends on 30 May. Until the loop runs 0.2.1 and the owner has confirmed D-038, no 31 May entry is added.
  - **The split's text:** `eval/src/lookout_eval/heldout_periods.json`'s `archive_guard.date_rule` still calls the standard-time reading "eval only" and doesn't mention UTC dates. The code is right. Editing the text changes the split's SHA-256 (`c437544e…`), which every record carries, so a split version bump is the lead's call.
  - **The owner** may re-allow the six entries, about 92 requests at today's shares (D-072), by confirming D-038's local-day reading (PRD_PROGRESS.md, owner action 9).
- **Amended 21 Nov (D-085):** the split's text is corrected in a new file, version 2; version 1 and its SHA-256 `c437544e…` are unchanged.

## D-071 — Big Bend's failed backfill entry resumed; the queue after the lead's edits · decided (lead; operation) · 2026-11-20 · amends D-052
- `big-bend.panther-junction/2025-06-14` failed after three search ReadTimeouts at 11:30:05 UTC. At about 12:40 UTC the lead resumed it (`archive queue resume`, offline). It retries in the next UTC day's budget.
- **The queue after both edits** (this one and D-070): 441 entries; 180 done, 260 pending and 1 active (`point-reyes.beach/2025-07-19`, part done); the last date is 2026-05-30.
  - Listed again offline at 15:47 UTC (`lookout-capture archive queue list --json`, the working tree's code): the same counts, and 1,000 of the day's 1,000 requests spent, so the next request waits for 00:00 UTC on 21 Nov.
- **It will happen again:** in the planner's runs on the real queue runner, a day search failed three times running about once every 9–13 simulated days at 20 Nov's error pattern. The plan's finish days assume a daily resume (D-072).
- **Amended 21 Nov (D-081):** the resumed entry succeeded in 21 Nov's budget. At about 03:10 UTC the queue held 808 entries: 246 done, 561 pending, 1 active and 0 failed.

## D-072 — The backfill plan regenerated from the live queue: T3pr ahead of T1b, TQ for the owner, a daily resume · decided (backfill planner; uncommitted; queuing is the lead's) · 2026-11-20 · amends D-052
- **Inputs:** a read-only copy of the queue at 12:47:40 UTC (441 entries; 180 done, 260 pending, 1 active), with each entry costed from its progress, and inputs re-measured on the queue's first 1,000 requests ([docs/research/archive-backfill-plan.md](docs/research/archive-backfill-plan.md), revised; `eval/tools/backfill_plan/`).
- **Requests still to send:** T1a 3,864, as queued; T1b 367 entries, 4,812; T3pr 10 entries, 271 (318 with T3pr first); T2 738 entries, 10,975; T1c 409 entries, 4,715.
  - Through T1c: 24,637 requests over 1,785 entries and about 1.22 GB of frames (0.49–1.91 GB), so about 1.26 GB in all against the 5 GB cap.
  - Frames through T1c: the 822 stored on 20 Nov plus 22,881 to come, about 23,700 (about 1.4 GB with the day searches). That is the NPS email's figure (the plan, section 7); TQ would add about 3,000.
- **Spans and TQ:**
  - The regular tranches keep D-024's starts, read as the archive check's first 16:9 sample: 15 Jul 2023 at Point Reyes; 15 Jul 2024 at Grand Canyon and the Smokies; 15 Jan 2024 at Yosemite and Acadia. So T1b, T2 and T1c hold exactly the morning plan's dates.
  - The dates between T0's first 16:9 frame (D-052) and those starts form **TQ**: 232 entries and 3,253 requests at five cameras, queued only if the owner agrees. TQ replaces D-052's next step, "extend the spans".
  - Big Bend keeps its Jan 2025 start for its view check. Grand Canyon's 3:2 epoch stays out, and its winter 2024/25 stays held.
- **The UTC edge** (D-070): a date is plannable only if its local date is before 1 Jun 2026, the latest instant any of its frames can carry is before 2026-06-01T00:00Z, and that frame's wall-clock date is before 1 Jun. At all six cameras the last plannable date is 30 May 2026. The planner refuses to plan behind an open queue entry that fails either reading.
- **Finish days** at 1,000 requests a UTC day from 21 Nov, at 1.0 % (3.7 %) errors, with T3pr ahead of T1b as recommended: T1a 24 (25) Nov, T3pr 25 Nov, T1b 30 Nov, T2 11 Dec and T1c 15 (16) Dec. TQ, after T1c, would land on 19 Dec.
  - **Against PLAN.md's targets** (T1a by Tue 24 Nov; T0–T1b and T3pr by Sun 29 Nov; T2 by Thu 10 Dec; T1c by Tue 15 Dec): at 1.0 % errors T1b (30 Nov) and T2 (11 Dec) miss by a day, while T1a (24 Nov), T3pr (25 Nov, 4 days early) and T1c (15 Dec) meet theirs. At 3.7 % errors T1a (25 Nov) and T1c (16 Dec) also miss by a day. The nine UTC days through 29 Nov hold 8,910 useful requests at 1.0 % errors (8,665 at 3.7 %), and T1a, T1b and T3pr need 8,947 in either order, so no order meets both 29 Nov targets. With T3pr first, T1b is 37 requests short on 29 Nov and T2 122 short on 10 Dec.
  - **The bottleneck:** the budget of 1,000 requests a day, plus 4–5 % more requests from the re-measured share of a complete day.
  - **Why T3pr first:** behind T1b it lands on 30 Nov at both error rates, after its 29 Nov due date (Point Reyes's M2 plate inputs). Ahead of T1b it lands on 25 Nov, and T1b moves from 29 to 30 Nov.
- **A daily resume:** without one, 7 of 16 multi-day runs on the real queue runner ended with 1–2 failed entries (10 in all), and their tranches never finished. With a daily `archive queue resume`, every run of all 4 seeds matched the schedule to the day. So the finish days assume the lead resumes failed entries every day (D-071).
- **Time model:** replaying 20 Nov's own outcomes gives 1.835 h of loop time per 1,000 requests (1.816–1.852 h), against 1.849 h for the real loop. That is a consistency check on its own day; 21 Nov's log gives the first check out of sample. The loop needs at least 2.2 h a UTC day (2.3 h covers every seed at 3.7 % errors).
- **Inputs re-measured:** 689 hourly `hires` frames, all 999×562, at 51,734 B on average (31,316 B at Point Reyes to 79,973 B at Yosemite); a complete day's share 0.938–0.996 by camera, 0.961 pooled; 1.0 % of the queue's first 1,000 requests failed.
- **Spacing, recounted exactly:** of the queue's 999 end-to-start pairs on 20 Nov, 991 were at least 5.0009 s, 7 were 5.0004–5.0008 s, and 1 was 4.9944 s (09:56:47.412 → 09:56:52.672 UTC); the median was 5.0109 s. On 19 Nov the least of 216 was 5.0005 s.
  - On the log's stamps, that one pair missed CLAUDE.md's "at least 5 s apart" by 5.6 ms. It is recorded on its own, with its proposed fix, in D-077.
  - Spacing archive requests at 5.05 s would cost nothing measurable in the time model (51 requests a window either way).
- **Draft scripts** (never run; they are regenerated from a fresh copy of the queue before use): each refuses a `Q` that is not an existing queue file given by an absolute path. `draft-T3pr.sh` takes as `EVENINGS` only the plan's 182 Point Reyes T1 days, and nothing after 30 May. `draft-TQ.sh` queues only the cameras named in `TQ_CAMERAS`.
- **Evidence:** 44 of 44 planner tests (the original 14 unchanged) and 33 of 33 mutants caught; ruff clean. The review reproduced every id, cost and finish day with an independent standard-library script.
- **For the owner:** which reading of the spans:
  - D-024's sample date (the plan's);
  - D-024's whole named month, which moves 42 entries and 621 requests into T2;
  - or D-052's own, spans from T0's first 16:9 date, which puts TQ into the regular tranches.
  
  If Point Reyes's TQ block (857 requests) is approved, it goes before T2 and lands on 30 Nov (1 Dec at 3.7 %), before the 14 Dec freeze. T2 then lands on 11 Dec (12 Dec at 3.7 %) and T1c on 16 Dec (17 Dec), so T1c misses its 15 Dec target at both error rates, by one day at 1.0 % and two at 3.7 % (the plan's `schedule_t3pr_first_tq_point_reyes_before_t2`).
- **For the lead:**
  - queue T3pr, then T1b (`EVENINGS=… sh draft-T3pr.sh && sh draft-T1b.sh`), once T3pr's clear days are chosen from Point Reyes's T1a frames, by 23 Nov;
  - resume failed entries every day;
  - ask eval to register T0's Big Bend 2024 and Grand Canyon winter 2024/25 frames (D-042).
- **Archived HRRR needed:** T1a–T2, 360 dates and 5,812 valid hours, 95.9 GB at the 24 h lead; with T1c, 423 dates and 112.5 GB; with TQ too, 495 dates and 130.9 GB.
- **Amended 20 Nov (D-078):** the lead queued T1b behind T1a at 18:12 UTC (way (b) of section 8), from drafts regenerated on a fresh copy of the live queue; T3pr follows when its six evenings are chosen, and T1b then moves behind it.
- **Amended 21 Nov (D-083):** capture 0.3.0's loop resumes a failed day search itself at its first idle on a later UTC day, at most once a day and 3 times in all, so the daily resume these finish days assume becomes automatic after the post-gate switch; until then the lead resumes by hand. `loop_sim` keeps 0.2.x's model unless it is given the new setting.
- **Amended 23 Nov (D-091):** T3pr is queued ahead of T1b, at 01:40 UTC on 23 Nov, with four clear days, not six: only four passed eval's three viewers. The last two come from T1b's Point Reyes September and October weeks once those land.

## D-073 — The backtest pair's run choice applies the archived stamp floor · decided (eval; uncommitted) · 2026-11-20 · amends D-054
- **Change:** `pair_forecast.choose_run` passes `listed_availability(bucket)` to `select_run_for_issue`, on the client the extraction uses. So a candidate run is available at the later of the lag model and its trusted S3 stamps, as in the extractor, and a run whose stamps are later than the issue time is skipped for an older one.
  - It refuses a lookout-forecast without `listed_availability` (0.1.0), and records the choice's own listing requests as `choice_transfer`.
  - When the file and the choice disagree, the refusal names each side's source, lag and stamp floor. It suggests re-extracting only for a reused archived file, the one case where that helps.
- **Evidence:**
  - The M0.5 pair's choice is unchanged under its day's lag model: 2024011412 f31/f32 (weight 0.75) at 14:00:00Z, with 2024011418 skipped. This used the recorded listings and the real lookout-forecast 0.2.0; the review rebuilt that day's model from the 474 log lines before the pair's run.
  - Under today's model (653 live and 4,082 survey lags) the choice would be 2024011418 f25/f26 at 19:31:36Z, because of the survey lags, not the floor. The recorded pair stays the measured output of its day; a re-run would be a different pair.
  - 25 pair-forecast tests (13 before).
- **It needs** lookout-forecast 0.2.0 (`08a996c`), which the running snapshot `034c46b` lacks. Each choice lists the candidate runs on NOAA's bucket: for the M0.5 pair, 2 more anonymous listings of 24,393 bytes each.

## D-074 — The spike rebuilt against speech 0.2.0 for checks 6 and 7 · decided (lead; operation) · 2026-11-20 · amends D-055
- At 12:36 UTC the lead ran `cargo build --release --offline --locked -p m1-spike-app --features speech`, with `CARGO_TARGET_DIR=~/lookout-data/cache/cargo-target`, in 13.6 s.
- The binary, `~/lookout-data/cache/cargo-target/release/m1-spike`, is 12,991,256 bytes. It embeds "lookout-speech 0.2.0 (SpeechAnalyzer + DictationTranscriber, on device only; macOS SDK 260200)", holds 1,013 `LookoutSpeech` symbols and links Speech.framework. The docs agent read it again at 15:41 UTC with `strings`, `nm` and `otool -L`.
- It is still signed ad hoc with the linker's identifier, `m1_spike-e8e55116026759cb`, and no Info.plist is bound (TODO(M1), as before: which identity AssetInventory reservations and TCC key on).
- The earlier build, at 03:39:58 UTC, linked the real library 0.1.0, not a stub (the third workflow's critic, re-checked by the lead: 989 symbols, Speech.framework, "lookout-speech 0.1.0").
- `lookout-transcribe` 0.2.0, for the owner's `status` and `install-assets` steps, is `~/lookout-data/cache/speech-macos/release/lookout-transcribe`: built at 12:11 UTC, and signed as `dev.lookout.transcribe` with its Info.plist bound (`codesign -dv`). The next `make test` runs `scripts/build.sh` again (D-075), which may relink the library and re-sign the tool at the same paths: the docs fixer's scratch run of it at 17:22 UTC made a 916,200-byte archive and a 559,952-byte tool, against the live 907,616 and 558,304 bytes.
- **A caution for the owner:** the spike README's Build block (`cargo build --release --workspace`) rebuilds `m1-spike` without the speech feature, and the runbook has the owner run that block first. Checks 6 and 7 then record `skipped` until it is rebuilt with `--features speech`. So the runbook's step 2.1 rebuilds it with the lead's command right after the Build block, and step 3.4 checks the binary before check 6. Checks 6 and 7 run on that 0.2.0 speech build, not on the lead's binary of 12:36.
- `app/speech-macos/README.md` still says that 0.2.0 was not relinked into the spike; its owner updates it.

## D-075 — make test: capture's slow tests on purpose, the speech build checks, and a bench-script target · decided (lead; the integration agent; uncommitted) · 2026-11-20 · amends D-063
- **Capture's slow tests run in `make test` on purpose** (the lead): they guard the schedule behaviour behind the polite rule (D-065).
  - The Makefile's `PYTEST_ARGS` (`-q -m "not network"`) replaces capture's own `addopts` (`-m 'not network and not slow'`), so `make test-capture` and CI run them. The marker stays as it is, and the Makefile now says why.
  - Since capture 0.2.1 there are 4 such tests, 3 days each of the four schedules: 81 s on a quiet machine and 241 s under other agents' load, against D-063's one test.
- **`make test-speech` also runs `scripts/build.sh --no-test`,** after `swift test`. That is the release build, the signing check of `lookout-transcribe`, the symbol deny list and reviewed baseline (400 imports, 39 selectors), the checker's mutation self-test (20 crafted archives) and the C-host link check.
  - Both steps run, and either failing fails the suite.
  - It writes the release build under `${LOOKOUT_DATA_DIR:-~/lookout-data}/cache/speech-macos`, build.sh's own path, and deletes its temporary folders under `$TMPDIR`. The third workflow's critic measured 9.4 s for it on an existing build.
- **`make bench-script`,** new: it builds `lookout-parse` (`cargo build --release --offline --locked -p lookout-core --bin lookout-parse` in `app/`, with `CARGO_TARGET_DIR` outside iCloud), then runs `bench/utterances/check.py --bin` on that binary, on workers/common's Python 3.13. check.py needs Python 3.11 or later, and `/usr/bin/python3` is 3.9.
  - It exits 1 on drift or a stale stored verdict, and 2 on a missing or stale binary.
  - It is not in `make test` or CI: CI's bench job sets up no Rust toolchain (the rust job is separate), and `--offline` needs the crates cached.
- **CI is unchanged:** it runs every `make test` suite except `speech` (the docs checks' `ci-suites`). The workflow's comments now describe the speech suite's two steps and capture's slow tests.
- **Measured by the docs agent,** with scratch target and data folders, because nothing may be written under `~/lookout-data` in this run:
  - `make bench-script` with a fresh scratch `CARGO_TARGET_DIR`: 40 crates built in 9.81 s; check.py exits 0 with 121 rows agreeing, 4 soft and 5 not (test split 94, 3 and 3). The binary is bit for bit the existing one (SHA-256 `ed5c1815…`); 10.6 s in all;
  - `make test-speech` with a scratch `LOOKOUT_DATA_DIR`: `swift test` 103 passed, 0 failed (36.0 s of tests, plus a cold debug build), then `build.sh --no-test`: release build 6.1 s, signed `dev.lookout.transcribe` with its Info.plist bound, check-symbols passed (400 imports, 39 selectors), the self-test passed (20 mutants, plus the no-ABI and harmless-import cases) and the link check passed. About 14 s for build.sh from a cold release build, and 63.7 s for the suite;
  - the docs checks: 16 checks, 85 tests, all passing; `make test-bench`: 40 tests passed.
- **Re-run by the docs fixer** at 17:20–17:23 UTC, after the docs review's corrections, with the same scratch folders: the docs checks 16 of 16 and 91 tests (6 new for the back-pointer rule); `make test-bench` 40 passed; `make bench-script` exit 0 in 10.8 s, 40 crates, the same 121, 4 and 5 rows and the same binary (`ed5c1815…`); `make test-speech` exit 0 in 62.0 s, `swift test` 103 passed, then build.sh's checks all passed. actionlint finds nothing in the workflow.

## D-076 — The running jobs depend on the lead's session; a supervisor is written but not started; the 8 h keep-awake has ended · decided (lead; record; starting the supervisor is the owner's call) · 2026-11-20 · amends D-030, D-049
- **Found at 12:30 UTC:** the capture loop (pid 36437) and the forecast stand-in (pid 36474) are children of the lead's Claude Code session (parent pid 72719, through zsh). Ending that session can stop them, and the 72 h clock with them.
- **The supervisor,** `~/lookout-data/cache/runtime/bin/supervise.sh`, written by the lead:
  - it adopts the running loops by pid, and starts replacements from `current` only after they exit, so two fetchers never run;
  - it restarts a loop that exits: 10 s after a graceful exit 0, 60 s after exit 3 (the lock is busy) and 330 s after anything else, so a poll sent but not recorded by a killed process can never be followed within 5 minutes. The delay doubles for quick deaths, up to 3,600 s;
  - it never starts a forecast stand-in while an earlier handler still runs;
  - on SIGTERM it stops the loops it started itself. Loops it adopted keep running;
  - keep-awake is opt-in (`LOOKOUT_AWAKE_UNTIL_UTC`);
  - tested against fake loops in the lead's scratchpad: adoption, exits 0, 3 and 1, the quick-death back-off and the shutdown, all as designed.
  
  It meets the capture package's advice for a supervisor: at least 1 s before a restart, a growing delay on repeated exits, and no immediate restart after exit 3 (its builder's report and `workers/capture/README.md`).
- **What adoption does not do** (this workflow's docs review, reproduced by the docs fixer against a copy of the lead's tested script and two fake loops):
  - **It doesn't detach the running loops.** They stay children of the lead's session until they exit. If the session ends and takes the capture loop with it, the supervisor starts a new one 330 s later (exit unknown), and the 72 h clock counts the gap as lost time (D-069).
  - **It doesn't stop them.** SIGTERM to the supervisor stops only the loops it started; its log reads "SIGTERM to capture none and forecast none", and both adopted fakes kept running. To stop everything, the owner also sends SIGTERM to the adopted pids. The capture loop then finishes its current camera and saves its state, as 0.2.0's loop does on SIGTERM.
- **The header's start command doesn't detach either, when typed at a Terminal prompt.** An interactive zsh makes the first process of a background job, here perl, the leader of its own process group, so `POSIX::setsid()` fails with EPERM. Perl returns −1 for that, which is true, so `or die` never fires, and the supervisor stays in the Terminal's session on its tty. The review showed it; the docs fixer reproduced it with zsh on a pty: TTY `ttys000` and STAT `SN`, where a session leader has `s`. Run in a subshell, the same command detaches: PPID 1, TTY `??` and STAT `SNs`, because perl is then not a group leader.
  - Owner action 1 now gives the subshell form, with `!= -1`, `mkdir -p ../logs` and a `ps` check.
  - The header in `supervise.sh` needs the same change; that file is the lead's.
- **Not started:** the permission system refused the lead's detached launch, so starting it is the owner's decision (PRD_PROGRESS.md, owner action 1). Until it runs, the session-dependence risk stands, and after it runs the adopted loops still depend on the session until they exit.
- **Keep-awake:** the owner-approved `caffeinate -dims -t 28800` (from 06:27:54 UTC) ended at 14:27:54 UTC, and the lead did not extend it. Only the capture loop's `caffeinate -s` now holds the Mac awake, and only on AC power.
  - At 15:46 UTC (`ps`, `pmset -g batt`, `pmset -g assertions`): both jobs running, capture since 02:39:39 and the forecast stand-in since 02:39:48 (13 h 07 min); on AC power, 100 %, charged; the only sleep assertion is that `caffeinate -s`.
- **The machine:** 19 GiB free of 460 GiB at 15:46 UTC (96 % used, `df -g`). Eight orphaned `terraform-provider-aws` processes (parent pid 1), left by scratch mutation runs of 19 and 20 Nov and outside `cache/runtime/`, held 949,040 KiB of resident memory at 15:46 UTC, about 0.9 GiB. That varies: readings between 15:46 and 17:08 UTC ran from 0.5 to 1.2 GiB (`ps -o rss`). The lead may stop them.
- **Amended 20 Nov (D-078):** the supervisor now finds the running loops itself (the NPS fetcher lock and the forecast script's path), so the owner's start command names no pid; its header gives the verified start form (a subshell, `setsid() != -1`). Still not started.
- **Amended 20 Nov (D-079):** the supervisor now holds `caffeinate -s` while it runs, so a restart cannot sleep through; on 20 Nov the Mac slept one second after a loop exited.
- **Amended 21 Nov (D-081):** a supervisor has run since 08:31:55 UTC on 21 Nov in its own session (pid 20119, PPID 1, TTY `??`, STAT `SNs`), holding its own `caffeinate -s`. The running loops are still children of the lead's session. The switch's preflight would not find it, so the lead stops it by hand before the switch (D-081).
- **Amended 22 Nov (D-089):** the supervisor's opt-in keep-awake was not set (its log: "awake until never (opt-in)"). On battery on 22 Nov its `caffeinate -s` and the loop's did not hold, and the Mac idle-slept twice. The lead's `caffeinate -i -w 20119` kept it awake from 06:53 UTC.
- **Amended 23 Nov (D-090):** the switch stopped the supervisor at 23:58:07 UTC on 22 Nov, signalling no adopted loop, and did not restart it. The owner started it again at 00:36:33 UTC on 23 Nov (pid 26187, in its own session), with its keep-awake set to 05:00:00 UTC. It adopted the new loops, which stay children of the lead's session through the switch's process.

## D-077 — One archive request pair on 20 Nov was 4.9944 s apart on the log's stamps · open (a breach record; the fix is proposed for workers/common) · 2026-11-20
- **What the log shows** (the backfill planner's recount, D-072; recounted by the docs fixer from a copy of `capture_log/2026-11-20.jsonl` taken at 17:03 UTC). A request's end is its record's `t`, and its start is `t − elapsed_ms`.
  - The queue's 1,000 `archive_request` records on 20 Nov make 999 end-to-start pairs: 991 at least 5.0009 s apart, 7 at 5.0004–5.0008 s, and 1 at 4.9944 s. The median is 5.0109 s.
  - That one pair: a request ended at 09:56:47.412 UTC, and the next, a `hires` GET, started at 09:56:52.406 (`t` 09:56:52.672, `elapsed_ms` 265.6, `waited_s` 4.987).
  - On 19 Nov the least of 216 pairs was 5.0005 s.
- **Against the rule:** CLAUDE.md asks for archive requests "one request at a time, at least 5 s apart". On the log's stamps that pair missed it by 5.6 ms. The load on nps.gov didn't change: one request at a time, 1,000 a day. But the rule is the owner's, so the miss is recorded like the earlier ones (D-023, D-048).
- **The stamps don't explain it.** `t` is cut to the millisecond and `elapsed_ms` is rounded to 0.1 ms, which moves a gap by about 1 ms, not 5.6. Capture writes a request's record before the limiter stamps that request's end, and it reads the next start back from the next record. So with a steady wall clock, the limiter's own end-to-start spacing was no longer than the log's 4.9944 s.
- **Cause: unknown.** The log can't tell an early start from a wall-clock adjustment between the two stamps.
  - The limiter is `MinIntervalLimiter.hold()` in `workers/common/src/lookout_common/http.py`. It sleeps for the rest of the spacing. Then, if no other process changed its state meanwhile, it lets the request start whatever the clock now reads, which is how it caps the wait at one interval when the clock has stepped back. So a sleep that ends early on the wall clock is not caught.
- **Proposed fix, for `workers/common`'s owner, with a test:** read the clock again after the sleep and wait out any remainder, or space archive requests at 5.05 s (D-072: no measurable cost).
- **Status:** the running loop (capture 0.2.0, snapshot `034c46b`) is unchanged, and capture 0.2.1 doesn't touch the limiter. The same margin applies from 00:00 UTC on 21 Nov, when the next day's budget opens, until a fixed `workers/common` is committed and the runtime is re-snapshotted.
- **Amended 20 Nov (D-079):** fixed in `3cd2ca7` (the limiter waits out the wall clock after every sleep; archive spacing 5.05 s), live in the capture loop since 19:56 UTC.
- **Amended 21 Nov (D-081):** closed. On the fix's first full UTC day, 21 Nov, the 999 end-to-start pairs of the day's 1,000 archive requests were all at least 5.0503 s apart (median 5.0557 s), none under 5.05 s.


## D-078 — The fourth workflow committed per package; the supervisor finds the running loops itself; T1b queued; a camera not polled after a restart is flagged, not counted · decided (lead) · 2026-11-20 · amends D-064, D-069, D-072, D-076
- **Commits** (UTC, 20 Nov), in the fourth critic's order: `b9520e2` capture 0.2.1 with eval's held-out parity files (D-065, D-066, D-070); `7f76411` eval's gate-rule clock, nps.gov bounds and pair choice (D-069, D-073); `3f48e75` the backfill planner and plan (D-072); `670138b` build integration (D-075); then this planning-docs commit. D-065 to D-075 were written while their code was uncommitted; these commits hold it. Nothing has been pushed.
- **The supervisor finds the loops itself** (amends D-076). Rewritten by the lead at about 18:10 UTC, still not started:
  - by default it adopts the capture loop named in `state/locks/nps-fetcher.lock` (if that pid is alive and is a capture loop) and the oldest running `forecast_loop.sh` of the runtime folder; `--adopt-capture` and `--adopt-forecast` take `PID`, `auto` or `none`;
  - so the owner's start command (PRD_PROGRESS owner action 1) names no pid and stays right after the lead's restarts;
  - its header now gives the start form the docs fixer verified: a subshell, and `setsid() != -1`;
  - stopping it never signals a loop it adopted.
  - **Tested** against fake loops in the session scratchpad: it adopted the capture loop through a fake lock and the forecast loop by its script path, started no second stand-in, started each replacement only after the adopted loop exited (forecast after 10 s; capture after the crash delay, its exit being unknown), and stopped its own loops on SIGTERM.
- **T1b queued now, behind T1a** (amends D-072; way (b) of the plan's section 8).
  - **Why now:** T3pr's six clear evenings are chosen from Point Reyes's T1a frames, which land on 21–22 Nov, so T3pr cannot be queued tonight. T1a runs out on about 24 Nov, and queuing T1b now means the loop keeps working after that even if no session is open. When T3pr is ready, it is queued and T1b moved behind it; the planner measured that move as costing nothing.
  - **What ran:** the lead regenerated the drafts with `plan.py` from a fresh copy of the live queue (SHA-256 `c8664e37…`, unchanged since the lead's 12:37 UTC edits) and ran `draft-T1b.sh` at 18:12:44–18:12:50 UTC (exit 0). All 54 `add` commands added their entries and none was already queued.
  - **The queue after it:** 808 entries (180 done, 627 pending, 1 active), dates 2023-01-30 to 2026-05-30, no duplicate id, and all 367 T1b ids present. The regenerated plan gives T1a 261 open entries and 3,864 requests (done on 24 Nov), and T1b 4,812 requests (done on 29 Nov at 20 Nov's error rate).
- **A camera not polled after a restart** (eval's finding 5; amends D-069). Under D-069's rule as written, a camera with no poll after a restart is measured to the window's end, so one camera dropped from the loop would keep the gate's clock from ever closing. The lead's ruling:
  - such a camera is flagged in the per-viewpoint table as not polled since the restart, and its own gap is still reported;
  - it is left out of that restart's excess.
  - `TODO(M1)` in eval's data report. Every restart so far polled all 8 cameras afterwards, so no figure changes.
- **The daily resume** (D-072): while a session is open, the lead runs `archive queue resume` for any failed entry each UTC day; otherwise it is an owner step. `TODO(M1)` for capture: resume an entry whose day search failed, automatically, once a UTC day.
- **Also:**
  - CLAUDE.md's archived-frames rule carries D-070's interim reading (local or UTC date);
  - the NPS email draft's frame figure is the regenerated plan's: about 23,700 frames (about 1.4 GB) by mid-December;
  - the plan's line on the supervisor says what adoption does not remove.
- **Amended 20 Nov (D-079):** the capture loop's pid changed at the 19:56 restart (now 70525); the supervisor's start command needs no change.
- **Amended 21 Nov (D-082):** built in eval's data report, and narrowed: a camera not polled since a restart is flagged, and it counts up to the window's end while the latest loop lists it; only a camera the latest loop does not list is left out. The narrowing waits for the lead's word.
- **Amended 21 Nov (D-083):** capture's `TODO(M1)` above is built in 0.3.0 (uncommitted): a failed day search is resumed automatically, once a UTC day and 3 times in all. The manual daily resume stays until the 0.3.0 loop runs, after the switch.
- **Amended 23 Nov (D-090):** the 0.3.0 loop runs since 00:02:07 UTC on 23 Nov, and its queue heartbeat records `auto_resume_max` 3, so the lead's manual daily resume has ended.
- **Amended 23 Nov (D-091):** T3pr is queued, and T1b has moved behind it as planned here. The lead removed T1b's 367 pending entries and added the same 367 again after T3pr's 8, at 01:40 UTC on 23 Nov.

## D-079 — The archive limiter fix committed; capture 0.2.1 live since 19:56 UTC; the Mac slept through the restart · decided (lead; record and operation) · 2026-11-20 · amends D-049, D-069, D-076, D-077, D-078
- **The limiter fix** (D-077's fix): commit `3cd2ca7`, in `workers/common` and capture's spacing constant.
  - `MinIntervalLimiter.hold()` and `wait()` start a request only when three things hold: the wall clock shows the interval since the recorded last request (and any back-off is over); this process's own last request is an interval ago on a sleep-inclusive monotonic clock (`timeutil.steady_clock`); and the real wait asked for at the first read has passed.
  - After every sleep, the limiter reads both clocks and the state again and waits out any remainder. HEAD's shortcut is gone: it let a request start once the state was unchanged after a sleep, which is how 20 Nov's pair logged 4.9944 s (reproduced in a test: 4.99399996 s on HEAD's code).
  - A wall-clock step back costs at most one more interval of real waiting. Back-offs, and the guarantee between two processes, remain wall-clock only (documented).
  - `ARCHIVE_MIN_INTERVAL_S` is now 5.05 s, so the log's own stamps (`t` cut to the millisecond, `elapsed_ms` to 0.1 ms) also show at least 5 s. In loop_sim that costs about 1 % more loop time per 1,000 requests, and every simulated day still spends the 1,000-request budget.
  - **Checks:** common 293 tests (35 new; 19 of the builder's fail on HEAD); capture 249 + 4 slow; the planner's 44. Two adversarial reviews (clock semantics, regressions) found nothing above low; the fixer fixed 8 code defects with tests and documented the rest.
- **`make test` on `3cd2ca7`:** all 10 suites passed, run 19:41–20:00 UTC (1,078 s of wall time). Capture's pytest reported 178.6 s against 886 s of wall time, because the Mac slept through most of that span (below).
- **The re-snapshot** (D-049's procedure, capture only):
  - the lead built `~/lookout-data/cache/runtime/3cd2ca7` at 19:41 UTC with its own venvs (capture 0.2.1, forecast 0.2.0; `uv sync --locked --no-dev --offline`). Its read-only `status` showed every camera's next poll on 0.2.1's `legacy_state` bound;
  - `current` → `3cd2ca7` at 19:43:59; SIGTERM to the 0.2.0 loop (pid 36437) at 19:44:05, 13 s after its pass ended at 19:43:52; it exited at 19:44:06 with status 0;
  - the new loop (pid 70525, `lookout-capture/0.2.1`) started at 19:56:13 and took the NPS fetcher lock at 19:56:14.
  - The forecast stand-in (pid 36474) was not restarted. It still runs forecast 0.1.0 from `034c46b`, and moves to 0.2.0 at its next start.
- **Why the restart took 12 minutes: the Mac slept.** It went to sleep at 19:44:07 UTC, one second after the old loop exited (`pmset -g log`). It woke for 9 s at 19:44:48, slept again at 19:44:57, and stayed asleep until 19:56:05; the lead's start command ran 8 s after that.
  - The old loop's `caffeinate -s` was the last assertion holding the Mac (the 8 h keep-awake ended at 14:27:54 UTC, D-076).
  - This Mac's system sleep is set to 1 minute (`pmset -g`), so it slept the moment that assertion dropped.
- **What it cost** (`lookout-eval data-report` to 20:01:58 UTC):
  - each camera went 1,058.9–1,063.6 s between its last 0.2.0 poll (19:43:41–19:43:52) and its first 0.2.1 poll (20:01:20–20:01:36);
  - the gate rule counts that restart's excess as 763.6 s (Smokies); the loop's own 12.4 min gap between passes is an outage under PLAN.md's figure;
  - 10.69 h are now lost, and the clock's earliest close moves from 22:53:14 to **23:05:58 UTC on Sun 22 Nov**. PLAN.md's capture-only figure: 22:44:12 UTC. The plan had expected 60–70 s.
  - Part of that cost is by design: 0.2.0's state records no answers, so 0.2.1 counted each camera's last poll as answered when it first read the state (19:56:14), then waited 300 s (D-065's upgrade bound).
- **The first 0.2.1 pass** (20:01:20–20:01:37): all 8 cameras polled (HTTP 200), each request record carries `sent_at` and `answered_at`, and every camera stored a new image. No new unproven pair: the window's 10 unproven pairs are all from 19 Nov's 0.1.0 code except Acadia's 299.588 s (0.2.0).
- **Lessons, now in the procedure:**
  - a restart holds its own sleep assertion (`caffeinate -s`, on AC) across the stop and the start, and stops and starts in one command;
  - the supervisor (still not started) now holds `caffeinate -s` while it runs, the same AC-only assertion the loop holds, so its own restarts cannot sleep through (tested in the scratchpad).
- **Still session-attached:** the new loop is a child of the lead's session, like the old one. The supervisor finds it through the fetcher lock (D-078), so owner action 1's command is unchanged.
- **Amended 21 Nov (D-080):** this entry's commit, `23c0543`, was pushed to `origin` `main` at 00:48:32 UTC on 21 Nov, the repo's first push. The next restart is the rename's live switch after the M0.5 gate, which uses this entry's lessons. Until then the running loop keeps the old names: `lookout-capture/0.2.1`, `Lookout/0.1 (…)` and `~/lookout-data`.
- **Amended 22 Nov (D-089):** `caffeinate -s` holds only on AC power. On battery on 22 Nov the Mac idle-slept while the loop's and the supervisor's were both held, and 2,043.8 s were lost. On battery a keep-awake needs an idle-sleep assertion: `caffeinate -i`, or the supervisor's opt-in `-ims`.

## D-080 — The product is renamed Snow Globe; old names stay readable; the live switch waits for the gate; the first push · decided (owner; the rename is uncommitted, and the switch is the lead's, after the gate) · 2026-11-21 · supersedes D-016's names; amends D-012, D-014, D-017, D-041, D-055, D-079
- **The owner's decision** (21 Nov 2026, UTC): every name changes to Snow Globe. The owner likes the name better, and it suits the product as a brand, including as a physical product one day. It supersedes "Lookout" (D-016, and row 9 of D-012). The repo was already `snow-globe`, so nothing on GitHub needs renaming.
- **The naming map,** applied everywhere the code and the living docs name the product:

| What | Before | After |
|---|---|---|
| Prose and UI | Lookout | Snow Globe |
| Kebab case: the Python distributions and their CLIs, the Rust crates and binaries, the Swift CLI, the spike's names | `lookout-` (`lookout-capture`, `lookout-core`, `lookout-parse`, `lookout-transcribe`) | `snow-globe-` |
| Snake case: the Python import packages, the Rust crate's import name | `lookout_` (`lookout_common`, `lookout_core`) | `snow_globe_` |
| PascalCase: the Swift modules, type names | `Lookout` (`LookoutSpeech`, `LookoutSpeechC`, `LookoutTranscribeCore`) | `SnowGlobe` |
| npm | `@lookout/renderer` | `@snow-globe/renderer` |
| C ABI | `lk_speech_*`, other `lk_` and `LK_` symbols, `lookout_speech.h` | `sg_speech_*`, `sg_` and `SG_`, `snow_globe_speech.h` |
| Signing identifiers | `dev.lookout.*` | `dev.snowglobe.*` |
| Environment variables | `LOOKOUT_*` | `SNOW_GLOBE_*` |
| User-Agent | `Lookout/0.1 (webcam research capture; fetch-on-change)`, overridden with `LOOKOUT_USER_AGENT` | `SnowGlobe/0.1 (webcam research capture; fetch-on-change)`, overridden with `SNOW_GLOBE_USER_AGENT` |
| Default data dir | `~/lookout-data` | `~/snow-globe-data`, found as below |
| Terraform names, tags and bucket prefixes | `lookout` | `snow-globe`; never applied, so nothing deployed changes |

- **Compatibility,** which is why this is not a search-and-replace:
  - **Environment:** one helper in `workers/common` (`snow_globe_common.env`) reads `SNOW_GLOBE_<X>` first, then the deprecated `LOOKOUT_<X>`, and every Python reader uses it. The Rust core's data-dir lookup (`app/core/src/datadir.rs`) follows the same order. The running loops' scripts set `LOOKOUT_MANIFEST_DIR` and `LOOKOUT_FORECAST_*`, and older shells may set `LOOKOUT_DATA_DIR`.
  - **The data dir:** `SNOW_GLOBE_DATA_DIR`; else `LOOKOUT_DATA_DIR`; else `~/snow-globe-data` if it exists; else `~/lookout-data` if it exists; else `~/snow-globe-data`. The `UnsafeDataDirError` guard is unchanged. While no variable names it and both folders exist as different folders (by device and inode, so the switch's symlink is one folder), every resolver refuses instead of picking one: Python (`AmbiguousDataDirError`), the Rust core and the spike, the renderer, the shell scripts and the Makefile.
  - **One NPS fetcher, whichever data dir:** a renamed fetcher takes `state/locks/nps-fetcher.lock` in its data dir and in each of `~/lookout-data` and `~/snow-globe-data` that exists (one file once, by device and inode), so it always respects the running 0.2.1 loop's lock at `~/lookout-data` and old code always respects its; it also refuses while another `snow-globe-capture` or `lookout-capture` process runs a fetching command (`fetcher_guard`, tested with the 0.2.1 lock protocol in another process).
  - **Old data stays readable for good.** Readers accept both the old and the new value of every `kind`, schema id, version prefix (`lookout-capture/0.2.1` and `snow-globe-capture/<version>`), worker or runner name and state field that carries the old name. Writers write the new names. Recorded old-format fixtures test this.
  - **The gate's figures don't move.** Eval's data report must give the same figures on the real logs before and after the rename. A change of version prefix counts as a restart only where a real restart happened.
  - **Versions:** every renamed package goes up one minor version (capture 0.2.1 → 0.3.0), so the logs tell the renamed code from the running 0.2.1. The working tree at 01:25 UTC on 21 Nov had snow-globe-common 0.2.0, snow-globe-capture 0.3.0, snow-globe-forecast 0.3.0, snow-globe-eval 0.2.0, snow-globe-core 0.2.0, `@snow-globe/renderer` 0.2.0 and the speech library 0.3.0. The renderer's `RENDERER_VERSION` (0.2.0) is its pixel version, not a package version, and stays: renders and goldens record it (PRD §9.2).
- **What keeps the old names, and why:**
  - **The record:** D-001 to D-079 (DECISIONS.md is append-only, and docs/checks enforces it), and git history, which is never rewritten.
  - **Evidence and measured records:** `docs/evidence/`, the archive check's report, PRD_PROGRESS.md's measured rows and THIRD_PARTY_NOTICES' revision notes keep the names their runs used, with a one-line note on the rename. A `lookout-*` command, `LOOKOUT_*` variable, `lk_speech_*` symbol or `lookout-speech` version string there is what actually ran.
  - **Files whose hash is on record:** eval's held-out split (`heldout_periods.json`, SHA-256 `c437544e…`, D-038) and the backtest pair's landmark boxes (`landmark_boxes_v1.json`, `697bb3c2…`) move to `eval/src/snow_globe_eval/` byte for byte, old `kind` and all. Both hashes still held in the working tree at 01:10 UTC on 21 Nov. Editing either file would change a hash on record.
  - **"The Lookout draft":** PRD.md and the cross-check keep that name for the owner's draft of 18 Nov, which v0.4 merged with "Snow Globe PRD v0.3".
  - **The data and the running jobs, until the switch:** `~/lookout-data` and everything in it; capture 0.2.1 from `3cd2ca7`, which logs `lookout-capture/0.2.1` and sends `Lookout/0.1 (…)`; the forecast stand-in, forecast 0.1.0 from `034c46b`; and the `LOOKOUT_*` variables in their scripts. Nothing under `cache/runtime/` changes before the switch.
- **The live switch, after the M0.5 gate closes** (the lead's; at the close the data report gives, which was 23:12:07 UTC on Sun 22 Nov at the earliest at 06:06 UTC on 21 Nov, after a 368.5 s capture outage at 01:23–01:34 UTC; 23:05:58 per D-079 before it):
  - Nothing live changes before then, so no restart can cost the 72 h clock.
  - **Until the switch the loops stay where they are:** the capture loop on `3cd2ca7`, the forecast stand-in on `034c46b`. The switch is the only re-snapshot path until then. The documented re-snapshot (flip `current`, SIGTERM the loops) cannot run a renamed commit: the live scripts start `lookout-capture` and import `lookout_forecast`, which the renamed code no longer has, so both jobs would fail to start. A fix needed before then waits for the switch.
  - Then the lead runs `infra/ops/migrate-to-snow-globe.sh`, committed with the rename and tested against stub loops by `infra/tests/test_migrate.sh` (`--dry-run` first):
    - its gate is the data report, not a time: before stopping anything it runs `snow-globe-eval data-report` read-only and refuses unless the gate rule shows 72 h covered (`clock.met`); `--force` is the owner's override;
    - it rehearses before stopping anything: the rename commit's workers built with their own venvs offline in a temporary folder, the new code's read-only `status` on the live data, and the rewrite of the three live scripts, which it checks by SHA-256;
    - capture first: stopped just after a pass ends; `~/lookout-data` moves to `~/snow-globe-data`, and a symlink to it stays behind at `~/lookout-data`; the snapshot, the rewritten scripts (`SNOW_GLOBE_*` names, old ones kept as `*.lookout.bak`) and `current`; then the new capture loop, about 240 s after the old loop's last pass. The live User-Agent becomes `SnowGlobe/0.1 (webcam research capture; fetch-on-change)`;
    - the stand-in last, once the new capture loop holds the lock and has logged a pass: stopped, its handler waited for, the new one started and checked through its first tick. It jumps from forecast 0.1.0 to 0.3.0;
    - its rollback restarts only what it stopped, and only while no instance of it is alive: a failure before the new capture loop runs puts the data, the scripts and `current` back and restarts the old capture loop; a capture loop that ignores its SIGTERM is left alone and nothing starts beside it; a failed first stand-in tick brings the old stand-in's script back from the pre-switch `current`.
  - It follows D-079's lessons: one command that stops and starts, under its own `caffeinate -is`. If the owner has started the supervisor by then, it is stopped first, because it would otherwise restart the old loops from `current`; it is not restarted by the script (D-076).
- **Found while writing this entry** (the docs builder; for the lead and the code builder):
  - The NPS fetcher lock (`state/locks/nps-fetcher.lock`) and the persisted rate-limit state live in the data dir. If `~/snow-globe-data` existed before the switch, renamed code run without `SNOW_GLOBE_DATA_DIR` would read it instead of `~/lookout-data`. It would then take a second lock and keep a second rate-limit state beside the running loop's, and the gate's data report would read empty logs. The switch's move would also land inside the existing folder.
  - `~/snow-globe-data` did not exist at 01:08 UTC on 21 Nov, nor at 06:10 UTC.
  - **Done** (the fixer, 21 Nov): nothing creates `~/snow-globe-data` before the switch, build-output defaults included; every copy-paste command in CLAUDE.md, the READMEs, the runbook and infra's docs names `~/lookout-data` (valid through the symlink afterwards) or the resolved data dir; CLAUDE.md says never to create the folder before the switch; and docs/checks fails on a hard-coded `$HOME/snow-globe-data` in a command (`data-dir-commands`) and on an old name outside the list of deliberate keeps (`old-names`). The resolvers' refusal and the host-wide lock above make an early folder fail loudly instead of splitting the lock and the logs.
- **The first push** (the owner's word, 21 Nov):
  - The owner authorized pushing `m0-prd` to `origin` `main`. `git reflog` shows `refs/remotes/origin/main` set to `23c0543` "by push" at 00:48:32 UTC on 21 Nov (18:48:32 −0600). The lead reports that it is public at github.com/swarit-1/snow-globe. It is the repo's first push (D-014). The rename came after it, so it is not in that push.
  - **CI** runs on every push (`on: push`), so this push was its first chance to run on GitHub. The lead reports that its first run failed only in the infra job's shellcheck, on two "info" notes that the laptop's newer shellcheck does not raise: SC2317 in `infra/lambda/smoke.sh`'s trap-only cleanup and SC2015 in `infra/tests/test_scripts.sh`. Both are fixed in the code (a narrow disable with a comment, and a real `if`), with no repo-wide change of severity.
  - **D-041:** the push published `b5bc726`, so the synthetic renderer goldens are public. Keeping them still needs the owner's sign-off.
  - **The licence:** README.md and THIRD_PARTY_NOTICES said the owner would choose one before the repository was made public. None is chosen yet, so the repository grants none.
  - CLAUDE.md's rule is unchanged: a push needs the owner's word.
- **Spend:** $0. No paid call was made.
- **Amended 21 Nov (D-081):** the rename was committed as `8fa6b6a` and pushed at 06:53:57 UTC on 21 Nov. CI's run on it passed all 9 jobs, and `make test` on its tree passed all 10 suites in 347 s. The live switch still waits for the gate.
- **Amended 23 Nov (D-090):** the switch ran from 23:53:34 UTC on 22 Nov to 00:03:13 UTC on 23 Nov, after the gate closed (D-089). The data is in `~/snow-globe-data` from 23:58:09, and `~/lookout-data` is a symlink to it. Capture 0.3.0 and the forecast stand-in 0.3.0 run from `aee6f92`, and the live User-Agent is `SnowGlobe/0.1 (…)`. So this entry's "until the switch" items are past; the records made before it keep the old names. The switch refuses to run again.

## D-081 — The lead's records of 21 Nov: the rename pushed with CI green, a battery outage, the fixed limiter's first full day, the Activate application, the supervisor running · decided (lead; record and operation) · 2026-11-21 · amends D-071, D-076, D-080; closes D-077
- **The rename is committed and pushed** (amends D-080). `8fa6b6a` was committed at 06:53:47 UTC and pushed to `origin` `main` at 06:53:57 UTC (`git reflog`: "update by push"). The owner had authorized pushing (D-080).
  - Before the push, the lead ran `make test` on its tree (06:47–06:53 UTC): all 10 suites passed in 347 s, and `~/snow-globe-data` was never created.
  - **CI on GitHub** (the lead's reading of the runs): the first push's run (44846267746, on `23c0543`) failed only in the infra job, on two shellcheck "info" notes that GitHub's newer shellcheck raises: SC2317 in `infra/lambda/smoke.sh`'s trap-only cleanup and SC2015 in `infra/tests/test_scripts.sh:309`. Every other job passed. The rename's run (44877704643) passed all 9 jobs, infra included.
  - So D-080's "the rename is uncommitted" no longer holds. The live switch still waits for the gate, and the running jobs keep the old names until then.
- **A capture outage on battery.** At 01:24:21 UTC the Mac was on battery (`pmset -g log`: "Using Batt(Charge: 28)"), where the loop's `caffeinate -s` does not hold. Capture's passes were 668.5 s apart, 01:23:43.273 → 01:34:51.761 UTC. Under the gate rule (D-069) that lost 368.5 s, and the earliest close moved from 23:05:58.495 to **23:12:06.983 UTC on Sun 22 Nov** (the data report to 01:50 UTC, measured by the rename's code builder).
  - **The power log, read by the docs agent:** the Mac slept from 01:25:20 UTC and was on AC again at 01:34:44 UTC (27 %). It was on battery again from 03:02:33 to 05:46:20 UTC, falling from 81 % to 41 %; it did not sleep, so nothing was lost. The lead's reading of AC power at 81 %, charging, at 03:02 UTC came just before that unplug. It has been on AC since, apart from 9 s at 08:22:33 UTC, and was at 100 %, charged, at 09:54 UTC.
- **The clock since** (the docs agent: `snow-globe-eval data-report`, the working tree's code, on a read-only copy of the live data taken at 10:01:16 UTC, with images replaced by empty files): to 10:01 UTC, 10.796 h lost and 34.815 h covered of 45.61 h, so the earliest close is still 2026-11-22T23:12:06.983Z.
  - The parts: capture outages 10.433 h, the two restarts' excess 0.264 h (185.471 s and 763.623 s) and forecast outages 6.039 h, the overlaps counted once. PLAN.md's capture-only figure: 22:50:21.196Z; capture alone 22:58:48.840Z; forecast alone 18:26:42.991Z.
  - 404 passes, 161 of them on 0.2.1; 47 forecast runs written at every viewpoint, the last 2026112108, with 0 errors. The window's 10 unproven poll pairs are the same 10 as on 20 Nov, none from 0.2.1.
- **The fixed archive limiter's first full UTC day** (closes D-077). 21 Nov's 1,000 archive requests, all from `lookout-capture/0.2.1`, ran from 00:00:46.940 to 01:52:22.829 UTC. Their 999 end-to-start pairs were all at least 5.0503 s apart (median 5.0557 s): 0 under 5.05 s, and so 0 under 5 s. That is the lead's check of the logs; the docs agent recounted it from `capture_log/2026-11-21.jsonl` and got the same figures. 998 were answered 200 and 2 failed in transit; 62 were day searches and 938 `hires` GETs.
- **Live polls under 0.2.1** (the lead's check, from 19:56 UTC on 20 Nov to 03:00 UTC on 21 Nov): 630 consecutive poll pairs, the least next send minus the previous answer 300.005 s, 0 under 300 s; 184 windows of 5 image GETs, the least 3,703.157 s, 0 under 3,600 s.
- **The queue** (amends D-071), at about 03:10 UTC: 808 entries, 246 done, 561 pending, 1 active and 0 failed. Big Bend's 2025-06-14, resumed on 20 Nov, succeeded. Copies read at 07:33 and about 08:45 UTC (the capture builder and fixer) gave the same counts: the file had not changed since 01:52:22 UTC, when the day's budget ran out.
- **AWS Activate** (the owner, 21 Nov). The owner applied for $5,000 of AWS Activate credits (the Portfolio tier, through an Activate Provider), as the startup Snow Globe, with the website https://github.com/swarit-1/snow-globe, for a new AWS account on the Paid plan, which bills only usage beyond credits.
  - The lead filled in steps 3 and 4, and corrected step 2 from Founders ($1,000) to Portfolio; the owner created and linked the account, and submitted.
  - AWS's review takes 5–10 business days, and the credits last 2 years.
  - Nothing is deployed and nothing runs on the account. D-087 and D-088 record what credits mean for the budget, and why a spend limit cannot replace its hard stop.
- **A process lesson from the rename workflow** (not a code defect). The lead sent a message to a running workflow agent, the code builder, and that resumed a second copy of it from its transcript. Both copies edited the tree for about 3 minutes, then split the work. The copy stopped at about 01:12 UTC and handed back, and the lead unblocked the original by writing a file it had been told to wait for. The final tree was reviewed by two reviewers and a fixer, and passes `make test`.
  - **The rule from now on:** never message a running workflow agent. Relay through a file the agent reads, or wait for its stage to finish.
- **The supervisor runs** (amends D-076; the docs agent's `ps`, read-only, at 09:54 UTC). `/bin/bash supervise.sh`, pid 20119, has run since 08:31:55 UTC on 21 Nov with PPID 1, TTY `??` and STAT `SNs`, so in its own session, as owner action 1 intended. Its working folder is `~/lookout-data/cache/runtime/bin`, and it holds `caffeinate -s -w 20119` (D-079).
  - Its log, which names the loops it adopted, was not read: it lives under `cache/runtime/`.
  - The capture loop (pid 70525) and the stand-in (pid 36474) are still children of the lead's session, as adoption leaves them (D-076).
- **Found by the docs agent, for the lead before the switch.** `infra/ops/migrate-to-snow-globe.sh` reads the supervisor's pid from `supervisor.pid`, but treats it as running only if its command line contains `$HOME/lookout-data/cache/runtime/bin/supervise.sh`. Owner action 1 starts it as `/bin/bash supervise.sh` from that folder, so the match fails: `ps -p 20119 -o command= | grep -q -- "$HOME/lookout-data/cache/runtime/bin/supervise.sh"` returns 1 (matching on `supervise.sh` alone returns 0).
  - The switch would then print "supervisor: not running" and leave it running. By D-076 and D-078, the supervisor starts a replacement from `current` once an adopted loop exits: capture 330 s after its exit, where the NPS fetcher lock makes a second loop exit with status 3 and the supervisor retries; and the stand-in 10 s after its loop exits, possibly beside the switch's new one.
  - `infra/tests/test_migrate.sh` starts no supervisor process, so no test covered this.
  - Either stop the supervisor by hand right before the switch, with owner action 1's stop command (it never signals an adopted loop), or make the script accept the pid file's process by its working folder, with a test. PLAN.md's switch steps now include the first.
  - **Fixed by the lead before commit:** the script now accepts the `supervisor.pid` process when its command is `supervise.sh` and its working folder is this runtime's `bin/` (or its command holds the full path), and it refuses, naming the pid, a running `supervise.sh` it does not recognise. A new case starts the supervisor the owner's way and checks the switch finds it, stops it before the loops, and leaves one capture loop and one stand-in (`infra/tests/test_migrate.sh`: 8 passed; `make test-infra`: all checks passed). The manual stop in PLAN.md's steps stays as a second guard.
- **A leftover process** (the lead's record; `ps` at 09:54 UTC): pid 65008, a W5 reviewer's frozen-clock hazard script (`mixed_clocks.py`, in the lead's session scratchpad, run by workers/common's venv), PPID 1, 0.1 % CPU and 5,344 KiB resident, since 18:59:46 UTC on 20 Nov. It is not ours to stop without the owner; the owner can `kill 65008`.
- **The machine at 09:54–10:01 UTC:** swap 12.3 of 13.3 GB used (971 MB free); Docker Desktop's 12 processes held about 0.5 GiB resident; 35 GiB free of 460 GiB on the data volume (92 % used). Nothing running needs Docker. It is needed for the Lambda image build, the smoke runs and the deploy (`infra/deploy.sh` checks for it), and `make test-infra`'s build-context check skips without it. So the owner may quit Docker Desktop until then.
- **Spend:** $0. No paid call was made; the AWS account exists, but nothing runs on it.
- **Amended 22 Nov (D-089):** three more outages on battery on 22 Nov (2,043.8 s lost) moved the earliest close from 23:12:06.983 to 23:46:10.754 UTC, and the gate closed then.
- **Amended 23 Nov (D-090):** the switch found the owner-started supervisor itself (the fix above, in `aee6f92`) and stopped it before the loops, signalling none of them, so the second guard, PLAN.md's hand stop, was skipped (the lead's call).

## D-082 — The data report builds D-078's restart ruling, narrowed; restarts split exactly on capture 0.3.0's records; the latest receipt reads answer_bound_at · decided (eval package; the narrowing confirmed by the lead) · 2026-11-21 · amends D-069, D-078
- **D-078's flag, as built** in `snow-globe-eval data-report` (its JSON is now `schema_version` 3; no reader checks the version, and the switch reads only `kind` and `clock`):
  - a camera with no poll after a restart, in any log the report reads, is flagged on the restart's row (`not_polled_since`: its last poll, its gap to the window's end, `in_latest_loop` and `left_out`) and on its viewpoint's row (`not_polled_since_restart`, the oldest such restart), and marked in the text table;
  - a restart before `--since` stays listed while a camera is still unpolled since it.
- **Narrowed** (the eval review's first finding, fixed by the eval fixer). It changes D-078's text, so it needs the lead's word.
  - Whether such a camera counts is decided by the latest capture loop in the logs: the last record that lists the loop's cameras, a `pass` record's `outcomes` or a `loop_started` record's `viewpoints` (D-084).
  - A camera that loop lists is waiting for its next poll, so its gap so far counts up to the window's end, as D-069 counted it before D-078.
  - A camera it does not list is left out, as D-078 ruled, and so is every camera when no record gives a list (`in_latest_loop` null). So one dropped camera still cannot keep the clock from closing, and the lost time only grows while a camera waits.
  - **Why:** under D-078 as written, every camera of an ordinary restart was left out until the new loop's first polls were logged, so for about 5 minutes after any restart the report showed 0 s of excess and too early a close. Reproduced on the 19:44 restart of 20 Nov, with copies of the live logs cut at 19:58 and 20:01 UTC: 0.0 s of excess and a close at 23:00:36.321Z, 322.174 s earlier than the 23:05:58.495Z the report gives once the polls are logged (cut at 20:02). With the narrowed rule the three cuts give 558.074, 738.074 and 763.623 s, HEAD's figures (0 of 97 figures differ).
  - **Not each restart's own new loop:** the review's prototype took the camera list from each restart's first pass. A camera that one loop listed and the next dropped would then count through the first restart up to the window's end, and the clock would never close. A test pins the latest loop's list: 185.1 s lost with a 2 h window and with a 4 h one.
- **Restarts split exactly when capture 0.3.0's records are there** (D-084; this does D-069's TODO(M1)):
  - first choice: the last record before the first `loop_started` between the two passes; next, the last `loop_stopped`; last, as before, the old loop's last record of its version and runner before the new loop's first pass start, which is that pass's `started_at` when logged, else `t − duration_s`;
  - a `loop_started` record is restart evidence on its own, so a restart within one release is seen (a test: 100.11 s of excess, and no restart without the record);
  - records are ordered by their place in the logs, `(t, file, line)`, so equal millisecond stamps go by line. The split is exact while the wall clock runs forward; a step back across a restart can misplace it (documented);
  - each restart row says how it was split (`split_by`, `started_from`, `loop_started_utc`).
  - Both real restarts (02:39 and 19:44 on 20 Nov) predate the new records, so they are split as before, with the same excess: 185.471 s and 763.623 s.
- **The latest receipt** of a request (D-069's bounds at nps.gov) is now the later of `answer_bound_at` and `answered_at`. A record that names an `answer_bound` but has no `answer_bound_at` gets no bound, so the pairs it starts count as unproven (fail closed).
  - The one request record with the field is the Smokies poll sent at 20:53:49.592Z on 20 Nov: a 30 s ReadTimeout, bounded at 20:59:19.597Z by capture 0.2.1's 300 s rule (D-066). Its least gap to the next poll is now 300.011 s, not 600.011 s. The Smokies' least gap for the window stays 299.001 s (19 Nov, 0.1.0), and no unproven count changes.
  - 73 records carry `answer_bound_at`: that request and 72 `answer_bound` events (64 `legacy_record`, 8 `legacy_state`). The builder's report said 1 of 64; the review's 73 reproduced.
  - Requests before 0.2.1 that failed after sending are still bounded at their failure time. Widening those 3 by 300 s would change no figure (the builder's what-if: a Big Bend pair 5,693.166 → 5,393.166 s, a Grand Canyon pair 659.388 → 359.388 s, a Rainier 5-GET window 5,006.720 → 4,706.720 s).
- **No gate figure moved.** On the eval fixer's copy to 08:19 UTC and its three cuts of 20 Nov, HEAD's code and the final tree agree on all 97 figures compared. The builder's copies to 07:08 and 07:43 UTC gave the same clock under both, and so did the docs agent's copy to 10:01 UTC under the final tree: 10.796 h lost, earliest close 2026-11-22T23:12:06.983Z. The reports differ in new fields and rule texts only.
- **Evidence:** eval 444 passed, 1 skipped (the opt-in render) and 2 deselected (`test_extractor.py::test_sample_frames`, which opens real sample frames; `make test` runs them), against 417 at HEAD; ruff check and format clean. 48 mutants of the changed code, 41 killed. The 7 survivors (the review's R04, R07–R10, R15 and R16) survived the builder's tree too and were not raised as findings. The new test with three records in one millisecond fails on HEAD (Acadia's gap 301.0 s instead of 9.1 s).
- **The lead's rulings (21 Nov):**
  - the narrowing is confirmed: it fixes a real flaw in D-078 as written (the early close after every restart), and a dropped camera still cannot keep the clock from closing;
  - D-069's conservative counting stays in both edge cases below (a back-off set before a restart, and a dropped camera's time once a later loop lists it again): counting more lost time keeps the gate from reading as generous. Revisit only if it ever blocks the gate for a reason outside capture.
- **The questions as the eval package put them:**
  - confirm the narrowing, or go back to D-078 as written with one line (`_restarts(..., None)` in `capture_section`); the early close after each restart then returns;
  - D-069 as written counts a camera's whole gap once it is polled again. That includes a back-off set before a restart (a test pins 7,011.5 s of excess for a 2 h back-off) and the time a dropped camera was out, from the moment a later loop lists it again (a simulation: 0.0 h while it was dropped, then 2,202.1 s once it was re-added). Whether such time should count is the lead's call;
  - the report relies on `pass` records listing every camera in `outcomes` (all 386 real ones, from 0.1.0 to 0.2.1, name 8) and on `loop_started`'s `viewpoints`. If either is renamed, it falls back to D-078 as written;
  - `TODO(M1)` in `eval/src/snow_globe_eval/data_report/capture.py`: `answer_bound` events of kind `unanswered_reservation` (a process died before saving an answer) are not yet turned into requests for the bounds. None is in the logs.

## D-083 — Capture 0.3.0 resumes a failed day search by itself, once a UTC day and 3 times in all; every archive redirect or 4xx backs off · decided (capture package; uncommitted, not running); the manual daily resume stays until the switch · 2026-11-21 · amends D-066, D-072, D-078
- **The resume** (D-078's `TODO(M1)` for capture). `QueueRunner.run_idle` starts each idle window with `auto_resume()`, which sets a failed entry back to pending when:
  - its day search failed 3 times, each with an error worth another try: a timeout, a connection error, HTTP 429 or 5xx, a body too slow or too large, or a body that is not JSON (`archive.is_retryable`, the rule the backfill already used for assets);
  - it is the loop's first idle on a later UTC day than the failure, never the same day;
  - it has not been resumed that UTC day, nor 3 times in all (`AUTO_RESUME_MAX`). The resume history is checked too, so once a day holds across a wall-clock step back over midnight, and `archive queue resume` keeps that history, so the cap is never reset.
- **Never resumed by itself:** a search with a redirect or another 4xx among its errors (`failure_kind` `search_not_retryable`; a 403 or 404 can mean nps.gov is refusing us); a search whose errors 0.2.x counted without judging them (`search_unclassified`); a search cut short; an internal error; an unknown viewpoint or a bad date; and `refused` (held-out) or `stopped` (size cap) entries.
  - Entries that 0.2.0 or 0.2.1 failed carry no `failure_kind`. They are read from their reason with a fail-closed whitelist and dated by the failing step's `last_step_at`, so they are judged by their last error only; up to 2 earlier errors are unknown. That is still no more than D-078's manual resume did.
- **It sends nothing.** It edits the queue under its lock, with the same code as `archive queue resume`. Each entry is set back on a copy, so one that cannot be set back stays as it was and never blocks the others. Each resume is logged as `queue_auto_resumed`; a failure as `queue_auto_resume_error`, at most once a UTC day, with the entry's id.
- **Opt-in** (the capture review's first finding): `QueueRunner(auto_resume_max=0)` is the default, as in 0.2.x, and `snow-globe-capture loop` passes 3.
  - The backfill planner's `loop_sim` builds a `QueueRunner` with the defaults, so it keeps 0.2.x's model, and D-072's figures stay reproducible: the review's no-resume scenario gives HEAD's result again (its last two output lines byte for byte), and the 44 planner tests pass. With the resume on by default, the planner had counted a tranche as settled on a day after which 0.3.0 would reopen its failed entry.
  - To model 0.3.0, the planner needs a setting such as `--auto-resume-max N` that also counts a resumable failed entry as open (for the planner's owner).
- **The listing says who resumes.** The queue file's runner heartbeat now records `capture_version` and `auto_resume_max`, and `archive queue list` promises an automatic resume only when a cap is recorded. While the 0.2.1 loop works the queue, its heartbeat has neither, so the listing says the entry needs `archive queue resume`.
  - Listed on a read-only copy of the live queue (about 08:45 UTC): 808 entries, 246 done, 561 pending, 1 active. No entry qualifies, and no open entry has a search error counted, so none would be `search_unclassified` today.
- **Every archive redirect or 4xx backs off** (the capture review's last finding; there since 0.2.0, against CLAUDE.md's "Back off on errors"). `ArchiveClient._send` now defers the archive limiter after a redirect and after every 4xx, as it already did after a 5xx: 30 s, doubling to 30 min, reset by a success, honouring `Retry-After` for 429 and 503. Failure counts and kinds, the retries, the shared www.nps.gov hold and live polling are unchanged, and a redirect is still not followed (D-066).
  - Against a 403 on every search (40 entries, a day of the loop's idles on a fake clock): before, 120 searches in 715 s, 5 s apart; after, 53 in 24 h, with gaps of 30, 60, 120, 240, 480, 960 and then 1,800 s.
  - In the live logs of 19–21 Nov, none of 2,217 archive requests got a 3xx or 4xx: 2,195 were answered 200, 2 answered 500 (searches) and 20 failed in transit.
  - It applies to every archive caller: the queue, `archive day` and `archive-check`. A kind that kept answering 404 would slow that work to one request every 30 min until one succeeds.
- **Evidence:** capture 362 passed, 4 deselected (337 on the builder's code, 269 at `8fa6b6a`); `-m slow` 4 passed; the make-equivalent run 366 passed; ruff, format and `uv lock --check --offline` clean; the planner's 44 tests pass. Of the fix round's 25 new tests, 21 fail on the builder's code; the other 4 pin correct behaviour the review found untested. The UTC-day rule is also tested in America/Chicago and Asia/Tokyo. A mutation harness run under TZ=UTC, as CI runs: 49 of 52 mutants killed; the 3 survivors are equivalent or harmless.
- **Until the switch** the live loop is 0.2.1, which never resumes anything by itself. So the lead's daily manual `archive queue resume` (D-072, D-078) stays until the 0.3.0 loop's first queue step has written its heartbeat; the listing says which applies.
- **Open:**
  - a redirect or 4xx on a day search is still retried twice more before the entry fails, now with the back-off between; failing at the first such answer would be politer;
  - as at HEAD, an open entry whose progress is not a record (only a hand edit makes one) stalls the queue at every idle;
  - **the version** stays 0.3.0 (the review's fifth finding, the lead's call), so `snow-globe-capture/0.3.0` names both the pushed `8fa6b6a`, which has none of D-083 and D-084, and this change. Either commit it before the switch and check that the switch's snapshot holds it, or bump capture to 0.3.1.
- **Amended 23 Nov (D-090):** live from 00:02:07 UTC on 23 Nov. It was committed in `c9f68eb` before the switch, and the switch's snapshot `aee6f92` holds it (`auto_resume_max=AUTO_RESUME_MAX` in its `cli.py`), so capture stayed 0.3.0. The 0.3.0 loop's queue heartbeat records `auto_resume_max` 3, so `archive queue list` promises the automatic resume, and the lead's manual daily resume has ended.

## D-084 — Capture 0.3.0 logs each pass's start, and each loop's start and clean stop · decided (capture package; uncommitted, not running) · 2026-11-21
- **New fields and records** (additive; nothing existing changes format):
  - `pass` records gain `started_at`, the pass's start in UTC to the millisecond (truncated); `t` is still its end. The summary `run_pass` returns keeps its seconds-precision `started_at`.
  - `loop_started` is written by `loop()` before anything is sent. The CLI takes the NPS fetcher lock first, in `main()`, so a refused start writes none. Fields: `worker` capture, `viewpoint_id` null, `pid`, `started_at`, `interval_s`, `jitter_s`, `viewpoints`, `runner` and `capture_version`.
  - `loop_stopped` is now written at every stop that is not an interrupt or a crash; before, only the foreign-fetcher check wrote one. Fields: `reason` (`signal`, `requested`, `max_passes`, `before_pass` or `error`), `passes`, `pid` and `loop_started_at` (not `started_at`, so that a reader of `started_at` is not confused); for a signal, `signal` (for example SIGTERM) and `signal_at`; for the last two reasons, `error`. Its `t` is the stop.
  - A second signal (a KeyboardInterrupt), a BaseException or a kill writes no `loop_stopped`. An unexpected exception from a pass writes reason `error`, and is raised again.
  - The queue runner's records now take `t` from the runner's clock when they are made, as live records do; in production that is the same moment.
- **What it costs:** 40 bytes per pass record (10,040 bytes on 20 Nov's 251 passes, the review's count), about 11.5 KB a day; loop records only at a start or a stop.
- **What uses it:** eval's data report splits a restart at the `loop_started`, and reads the latest loop's cameras from `viewpoints` (D-082).
- **The switch's own restart:** the 0.2.1 loop that the post-gate switch stops writes no `loop_stopped` on SIGTERM; that is 0.2.1's code. So the report splits that restart at the old loop's last record before the new loop's `loop_started` (tested on the recorded 0.2.1 log followed by a synthetic 0.3.0 start).
- **Evidence:** 8 tests in `workers/capture/tests/test_loop_records.py` pin every field and reason. The SIGTERM test sends the signal only when the loop's handler is installed: with the handler removed, the builder's version killed the pytest run (status 143), and the fixed one fails cleanly (1 failed, 7 passed). No 0.3.0 loop has run, so eval's tests use synthetic records built to this shape.
- **Amended 23 Nov (D-090):** the first 0.3.0 loop started at 00:02:07 UTC on 23 Nov. Its `loop_started` record and every pass's `started_at` are in the live log, and the data report split the switch's restart at the old loop's last record before that `loop_started`, as this entry says: 4.797 s of excess.

## D-085 — Held-out split version 2: the same periods with corrected text; version 1 stays byte for byte · decided (eval, at the lead's request; uncommitted); the file must cite this entry before it is committed · 2026-11-21 · amends D-038, D-070
- **The file:** `eval/src/snow_globe_eval/heldout_periods_v2.json`, version 2, `kind` `snow-globe.heldout-periods`, decided 2026-11-21; untracked. It is generated from version 1's text, so every unchanged byte is identical. It changes only:
  - `kind` (the renamed one, D-080), `version`, `decided`, and `decisions`, which adds D-038, D-047, D-070 and D-080;
  - a new `revision` note;
  - `archive_guard.date_rule`, which now states the rule both guards apply: any local or UTC date, the standard-time reading in capture too since D-047, fail closed (D-070);
  - `archive_guard.source` (the renamed capture path) and `quarantine.rule` (no folder named by its old name).
  
  The periods, viewpoints, scoring exclusions, the guard's `from_local_day` and the quarantine folder are version 1's. The guard code reads only `from_local_day`, so no verdict changes.
- **Hashes (SHA-256):**
  - version 1, `eval/src/snow_globe_eval/heldout_periods.json`: `c437544eecdd7a0d33b67c2374e1d388ebe45c36aeca4b2923b39c0d1d31de83`, byte-identical to HEAD and to D-038's;
  - version 2 as built: `e64ae4443998d1b15f46966b39be3ff35d13677925c096f4d0997796acffa6d8` (6,207 bytes).
- **Its change rule needs this entry.** Version 2's `change_rule` says any change "bumps `version`, cites a new DECISIONS.md entry, and is committed before any frame it releases or newly holds out is viewed". As built, its `decisions` cite no entry that records it (the eval review's second finding). This is that entry. The lead adds `"D-085"` to `decisions` before committing. Appended as the last item, with nothing else changed, the file's SHA-256 becomes `82574ceb38ebed8ad6c5f61d02868244bf0673d8c900e0bbcc477143a95ae3a3` (6,216 bytes; computed by the docs agent on a scratch copy). Then:
  - re-pin that hash in `eval/tests/test_heldout.py` (`SPLIT_V2_SHA256`, and the expected `decisions` list), the `sha256 e64ae4443998` prefix in `eval/tests/test_backtest_pair.py`, and `eval/README.md`;
  - commit `heldout_periods_v2.json` and `eval/tests/fixtures/capture_log_failed_after_send_0.2.1_20261120T2048.jsonl`, both untracked, in the same commit as eval's code: `committed_split()` fails without the file.
- **Readers:** `committed_split()` returns version 2, `committed_split(1)` version 1 and `committed_splits()` both. `cited_split(citation)` resolves a record's citation of either version by version and SHA-256, but nothing calls it yet. New records cite version 2; the M0.5 pair records keep version 1, whose citation resolves exactly. The `heldout` CLI prints version 2 and version 1's identity.
- **What it does to earlier entries:** D-070's open item on the split's text is done; version 1 keeps the older wording, so the records that cite its hash stay true. D-038's still-to-do, fits inside one camera epoch, now needs split version 3 (TODO(M2)), as eval's `leakage.FitView` and README now say.
- **Evidence:** `test_split_version_2_changes_text_only` checks the keys that differ; identical periods, viewpoints, exclusions, guard date and quarantine folder; identical held-out intervals and fit bounds at all 8 viewpoints; and the same verdicts on a 31 May evening archive frame and a 30 May one. The tests pin both hashes; eval's suite is in D-082.
- **Amended 23 Nov (D-090):** committed in `60e05ba` at 11:00:30 UTC on 21 Nov, before the switch, as SHA-256 `82574ceb38ebed8ad6c5f61d02868244bf0673d8c900e0bbcc477143a95ae3a3` (6,216 bytes); its `decisions` list cites D-085, as this entry's heading requires.

## D-086 — Hosting re-priced at 8fa6b6a: $2.57–4.51 in month 1 with the free tiers, before tax · proposed (for the owner, with D-035 and D-087) · 2026-11-21 · amends D-060, D-066
- **New estimate** (`infra/README.md`, "Monthly cost estimate"; `infra/cost/estimate.py` on `infra/cost/sim_capture_8fa6b6a.json`, at the prices read on 19 Nov; computed, and nothing ran on AWS). It prices capture 0.3.0 as committed at `8fa6b6a`; the working tree's capture changes (D-083, D-084) are not in it.
  - **With the free tiers, before tax:** $2.57–4.51 in month 1, $2.90–4.86 in month 2 and $3.12–5.09 in month 3, then about $0.22 more each month. Priced from each of the six runs alone, month 1 is $2.57–2.58 at the bottom and $4.46–4.58 at the top.
  - **Without them:** $6.14–10.31 in month 1.
  - The bottom passes $3 in month 3 (D-066: month 4), or month 2 with an 8 % sales tax (D-087). At the top, the free GB-s run out on day 22.4 and spend passes $3 on day 25.4 (25.2–25.5 across the runs), so the hard stop would come about day 25 of month 1.
  - The largest lines: S3 requests $2.28–2.30; Lambda compute $0–1.82 with the free tier ($3.14–7.15 without).
- **Against D-066's $2.43–4.40:** D-066's own inputs, priced at the new waiting, reproduce $2.43–4.40 to the cent. Steady-state request counts add $0.02 at both ends, steady-state old state versions $0.07, and the forecast recount $0.05 at the bottom and $0.02 at the top.
- **What changed in the simulation** (`infra/cost/sim_capture_s3.py`, run on a git-archive export of `8fa6b6a` with the capture venv's moto):
  - each nps.gov request now takes a measured time, drawn by stage and by cold or warm connection from 1,340 answered live requests of capture 0.2.1 (20:01 UTC on 20 Nov to 07:00 UTC on 21 Nov; the new `nps_request_times.py`, which counts a request after any failure as cold). Before, every request was instant, which gave 14.62 s of waiting, the zero-latency figure D-066 had set aside;
  - it warms up for 7 simulated days and counts 3, because capture's state documents reach their steady size only once their 240-hash lists fill, after about 2.5 days;
  - six seeds, pooled by the new `sim_pool.py`, each kept whole under `replicates` (the review found that one seed's 34.27 s was the lowest of four runs);
  - memory is bounded: a first 10-day run grew past 21 GB under moto and was killed; the final runs peaked at 563–586 MB.
- **Measured in the simulation:**
  - billed waiting 35.88 s a pass, pooled (34.90–37.15 s by seed, SD 0.82 s), plus 2.23 s of nps.gov request time; 3.85 skips per camera per day (3.21–4.25 by run); 748 stores a day of the 768 allowed;
  - capture's S3 calls a day: 11,195 PUT, 174 LIST, 9,929 GET, 705 HEAD and 3,426 DELETE, 88.3 a pass; capture's request bill $1.83 a month;
  - old state versions 302.3 MB a day at steady state (56.8 KB each), against 97 MB on day 1: about $0.21 a month once steady, about $0.14 more than the day-1 sizes every earlier estimate used (D-066 included).
- **Forecast, counted by running its code** (the new `infra/cost/count_forecast_s3.py` against the forecast tests' fake bucket): at `8fa6b6a` an idle call makes 117, 119 or 121 GET-tier requests for 13, 12 or 11 written runs, and an extraction 19 GET-tier, 11 PUT-tier and 1 DELETE, because `Extractor.extract` runs `write_decisions` again under its lock, which both the builder and the review had missed. That adds 90,000 GET-tier requests a month (+$0.036), now in the totals. The same tool on `8246a8b` gives D-060's count.
- **Sensitivities** (not in the totals): at 20 ms per S3 request the waiting falls to 33.47 s while a whole pass stays about as long (37.51 s against 37.87 s), so the estimate's added 5–20 s errs high. Quarter-length nps.gov requests raise the waiting to 38.01 s and month 1 to $2.56–4.62.
- **Not modelled:** Lambda's own times to nps.gov and S3, EventBridge's start jitter and capture's CPU time; the request times are this laptop's, over a home connection. This simulation gives 3.85 skips per camera a day, against the capture package's 1.1–1.4 (D-065); their feed timings differ, which was not tested.
- **Evidence:** `make test-infra` exit 0 in 66 s: 130 passed (static 6, root Terraform 52, bootstrap 4, providers 24, scripts 26, build context 1, cost 10, migrate 7), fmt and shellcheck clean, tflint skipped (not installed). 21 of 21 mutants of the fixes' tests caught. The new simulation check will run in CI's infra job for the first time, about 10 s.
- **Levers, for the owner:** a 7-day old-version rule for `state/` saves about $0.16 a month but shortens the 30-day undo window the README promises; dropping the per-poll reservation write saves $0.34 a month (D-066's lever, capture's); forecast's repeated reads cost about $0.04 a month (forecast's).

## D-087 — The AWS budget counts costs before credits and refunds, with tax · decided (infra package; not applied); whether the $3 cap includes tax is for the owner · 2026-11-21 · amends D-034, D-035
- **Change** (`infra/terraform/monitoring.tf`): the budget's `cost_types` set `include_credit = false` and `include_refund = false`. Every other cost type keeps AWS's default (counted, at unblended rates), tax included. Nothing is applied.
- **Why:** the account will carry credits: $100 at sign-up (the infra brief's figure) and, if approved, the $5,000 Activate package (D-081). Netted against usage, credits would keep the budget near $0 for months, so no email and no hard stop would ever fire. Counting costs before credits keeps PRD §17's $3 cap on usage.
- **What it does to the hard stop:** at 100 % of actual cost the budget action attaches the deny policy (D-034), so the stop now fires on gross usage even while credits pay the bill: about day 25 of month 1 at the top of D-086's range. D-035's question, the hard stop through the sealed window, stays the owner's.
- **Tax:** the budget counts tax, and the tests pin it, while D-086's estimate is before tax. With a sales tax of t, the stop fires at $3 ÷ (1 + t) of pre-tax spend: $2.78 at 8 %, when month 2's bottom of $2.90 becomes $3.13. Whether PRD §17's $3 is before or after tax is the owner's call; if before, `include_tax = false`, pinned in the test. When AWS adds tax within the month was not checked.
- **CostTypes is deprecated** in the Budgets API (the infra builder's reading of AWS's reference; not re-checked offline). If AWS stopped honouring it, the budget would fall back to counting credits, and the stop would go blind. CreateBudget takes `FilterExpression` with `Metrics`, or `CostFilters` with `CostTypes`, not both, and the pinned provider refuses a mix at plan time ("cost_types conflicts with metrics"). Moving to the newer pair needs an AWS-side check of the dimension values before any apply, so it waits for the owner.
- **Evidence:** two new `terraform test` runs (root runs 50 → 52, all passing; plan-only, against a mocked provider), which both fail with HEAD's `monitoring.tf`. Six deliberate breakages, each caught: `include_refund = true` (2 runs fail), `include_refund` removed (2), `include_credit = true` (3), `include_tax = false` added (1), `cost_types` removed (fails), `metrics` added (the provider refuses the plan).

## D-088 — AWS spend limits cannot hold the $3 cap; the budget action stays the hard stop · decided (infra package; from AWS's docs, not re-checked offline; nothing built) · 2026-11-21 · amends D-035
- **What AWS's documentation says** (read by the infra builder on 21 Nov; the review had no network to check it):
  - a spend limit caps a project's pre-tax monthly cost, with a minimum of $20 or AWS's own estimate, if higher;
  - at the limit AWS pauses the project and stops all its resources, and deletes the data after 90 days without action;
  - limits "exclude credits", read here as gross usage;
  - they are set in the console only: no API, CLI, CloudFormation type or Terraform resource. The review confirmed offline that the pinned provider (6.73.0) has none; its only `monthly_spend_limit` belongs to `aws_sns_sms_preferences`;
  - AWS offers them only on the new sign-up, and Activate credits only on the advanced one, and activating advanced features removes spend limits for good.
- **So:** a spend limit cannot hold $3, and the account that gets the Activate credits cannot have one. D-035's budget action (D-034, now counting costs before credits, D-087) stays the only automatic stop. A spend limit could only be an outer $20 ceiling, with its early controls off, on a new-sign-up account without Activate credits. `infra/README.md` has the sourced note and that proposal; nothing is built.
- **Unconfirmed:** the infra brief dated spend limits to August 2026, but the builder found the announcement dated 16 Sep 2026; the documentation pages carry no date, and their wording of the pause length differs from the announcement's.
- **For the owner:** by these documents, the account that gets the $5,000 of credits will have no spend limit.

## D-089 — M0.5 gate 1 closed: 72 h of both jobs' logs covered at 23:46:10.754 UTC on Sun 22 Nov, after three outages on battery that day · decided (lead; record) · 2026-11-22 · amends D-069, D-076, D-079, D-081
- **The close** (the lead's `make data-report`, read-only, at 23:46:31.858 UTC; the gate rule, D-069): met at **2026-11-22T23:46:10.754Z**. From the loop's start at 12:24:23.000 UTC on 19 Nov, 72.01 h are covered of 83.37 h elapsed. 11.36 h are lost: capture outages 11.001 h, the two restarts' excess 0.264 h and forecast outages 6.039 h, an overlap counted once.
  - The same report as JSON, read at 23:46:33.171 UTC: `required_h` 72.0, `covered_h` 72.006, `elapsed_h` 83.369, `lost_h` 11.363, `met` true, `met_at_utc` 2026-11-22T23:46:10.754Z.
  - Beside it in the report: PLAN.md's capture-only figure was met at 23:24:24.967Z (11.00 h lost), capture alone at 23:32:52.611Z (11.14 h) and forecast alone at 18:26:42.991Z (6.04 h).
  - The switch's own gate check read it again at 23:53:34.551 UTC: 72.123 h covered of 83.487 h, met at the same instant (D-090).
- **What the table shows at the close:**
  - capture: 830 passes by `cli-loop` (`lookout-capture/0.1.0` 69, `0.2.0` 174, `0.2.1` 587), median spacing 314.03 s; 825–827 polls and 275–280 image GETs per camera; every logged frame and thumbnail on disk, and none unlogged;
  - forecast: 85 live runs first written in the window at each of the 8 viewpoints, 2026111900 to 2026112222 (2 of them catch-ups), and none never written; 723 distinct live lags; 0 errors;
  - exclusions, the same kinds as since 20 Nov: records before the window, the archive queue's 6,013 records, the backtest pair's archived run, the 12 re-extractions and 160 duplicate lags of 20 Nov, and the 4,082 survey lags.
- **The evidence,** outside the repo in `~/lookout-data/reports/m0.5-gate/` (it moved with the data, D-090), summarised in [docs/evidence/m05-gate.md](docs/evidence/m05-gate.md). SHA-256, taken by the lead and checked again by the docs agent at about 00:10 UTC on 23 Nov:
  - `data-report-20261122T234633Z.txt`, the table: `98938ea26a8485ce891e1e9d2ef7c6eebaa684c7e528a7a0254303f72c07aa40`;
  - `data-report-20261122T234633Z.json`: `93b89d23e801b1843f2595729e747da3eda053490bf0338bc3e035b41090745f`;
  - `switch-dry-run-20261122T234851Z.log`, the passing rehearsal: `61ab02f038dc16b0001fbe8c4370e83cb630492800a50c23d24e14be2ecac6c8`;
  - `switch-20261122T235334Z.log`, the switch: `6cbeb8b5e6ad6f9c628488858eabe4b2ec6d5c6a9d92ec3eb75123adf43015fc`;
  - `data-report-20261123T000629Z-postswitch.txt`, read on `~/snow-globe-data` after the switch: `b69a9793e8bfca9772e0453e756f7234945b3daf0f445b8ce14cb9bda74ba663`.
- **22 Nov's three capture outages,** the only losses since 21 Nov (the data report, checked against `capture_log/2026-11-22.jsonl`):
  - 05:39:19.364 → 05:59:17.751 UTC: 1,198.4 s between passes, 898.4 s lost;
  - 06:14:54.668 → 06:24:29.365: 574.7 s, 274.7 s lost;
  - 06:31:34.302 → 06:51:04.989: 1,170.7 s, 870.7 s lost.
  
  That is 2,043.8 s in all. It moved the earliest close from 23:12:06.983 (D-081) to 23:46:10.754 UTC.
- **The cause** (`pmset -g log`): from 18:55:11 UTC on 21 Nov the Mac ran with its lid closed on AC, in dark wake ("Clamshell Sleep"), and the loops kept running (107 passes to 04:15:11 in `capture_log/`, none more than 333.3 s apart). It went on battery at 04:16:45 UTC on 22 Nov, at 100 %. The supervisor's `caffeinate -s` (pid 20135) and the capture loop's (pid 70531) both held PreventSystemSleep, which counts only on AC power, so on battery neither counted.
  - At 04:16:45 the summary read "No Assertions", and the Mac slept at once for 14 s ("Notification Wake Back to Sleep"), until the lid was opened at 04:16:59. No outage came of it.
  - After that it stayed awake only while powerd held "Prevent sleep while display is on" (04:17:00–05:40:23 and 06:05:36–06:16:03); the summary then mostly read "PrevIdle DeclUser kDisp". Each time that assertion ended, the two `caffeinate -s` lines were tagged "[System: No Assertions]", and the Mac idle-slept:
    - "Idle Sleep" at 05:40:29 (83 %), then dark wakes;
    - a full wake at 06:05:35 (user activity);
    - "Idle Sleep" again at 06:16:09 (81 %), then dark wakes;
    - a keyboard wake at 06:50:25 (80 %).
  
  The passes that ended the first two gaps (05:58:59.9 → 05:59:17.751 and 06:24:21.5 → 06:24:29.365) ran in dark wakes. The third (06:46:53.8 → 06:51:04.989, 251.23 s) began in a 2 s dark wake (06:46:53–06:46:55), in which a JSON GET's connection was reset at 06:46:53. It spanned the 210 s sleep that followed and ended after the keyboard wake at 06:50:25. Its Acadia image GET, sent at 06:46:58 as the Mac went back to sleep, was abandoned at its 60 s deadline at 06:50:53 (235.5 s after it was sent), 28 s into that full wake.
- **The stopgap:** the lead's background `caffeinate -i -w 20119` (the supervisor's pid). Its PreventUserIdleSystemSleep assertion (pid 11415) held from 06:53:27 UTC until the switch stopped the supervisor at 23:58:07: the power log marks it "ClientDied" then, after 17:04:40. That kind of assertion holds on battery. The Mac stayed on battery until 09:46:47 UTC (48 % at 09:30:58) and did not sleep, and no outage followed 06:51:04.
  - The supervisor's own keep-awake (`LOOKOUT_AWAKE_UNTIL_UTC`, D-076) was not set when the owner started it on 21 Nov. Its log's first line reads "awake until never (opt-in)".
  - Power after that (`pmset -g log`): on AC from 09:46:47 (47 %) to 23:32:45 (100 %); on battery from 23:32:45 to 23:51:06, apart from 2 s at 23:32:53; on AC from 23:51:06, before the switch.
- **The lesson:** on this Mac, `caffeinate -s` (PreventSystemSleep) holds only on AC power. On battery only an idle-sleep assertion holds it, such as `caffeinate -i` (PreventUserIdleSystemSleep) or the supervisor's opt-in `caffeinate -ims`, and only with the lid open. The loops hold `-s` alone. So unattended running on battery needs one of those set before the Mac leaves AC power.
- **Live errors on 22 Nov** (`capture_log/2026-11-22.jsonl`): 10 of 2,856 live requests, each followed by a 300 s back-off. 5 image GETs were answered 404; 3 requests were answered 504 at 05:07:56–05:08:46 (one image GET, two JSON GETs); and there were the reset and the deadline above. No 429 or 503 came, so no www.nps.gov hold was set (`state/capture/hosts/` does not exist).
- **Polite collection on 22 Nov** (the docs agent's count on `capture_log/`, from 00:00 UTC on 22 Nov to 00:13:12 UTC on 23 Nov: capture 0.2.1, then 0.3.0's first three passes; with the data report's bases):
  - every camera's least poll gap at nps.gov (the next send minus the previous answer's bound) was 300.002–300.003 s, over 269–271 pairs a camera, so none was under 300 s;
  - every 5 consecutive image GETs spanned at least 3,706.3 s (84–88 windows a camera), so none was under 3,600 s;
  - the day's 1,000 archive requests, all answered 200 (65 day searches and 935 `hires` GETs), were logged from 00:04:01.537 to 01:45:45.336 UTC. Their least end-to-start gap was 5.0507 s, and none was under 5.05 s.
  
  Over the whole window the report's unproven poll pairs are still the same 10, all from 19–20 Nov, and no 5-GET window is unproven.
- **Held-out data:** no queue entry is dated on or after 1 Jun 2026; the latest is 2026-05-30 (the queue listing, D-090).
- **The gate's items:** item 1, this one, is done. Items 2 (the archive report) and 3 (the backtest pair) were done on 19–20 Nov. Item 4, the emails, waits for the owner, so M0.5 stays in progress for that alone.
- **Spend:** $0. No paid call was made.

## D-090 — The rename's live switch ran: the data in ~/snow-globe-data, both loops on snapshot aee6f92, SnowGlobe/0.1 live · decided (lead; record and operation) · 2026-11-23 · amends D-049, D-076, D-078, D-080, D-081, D-083, D-084, D-085
- **What ran:** `infra/ops/migrate-to-snow-globe.sh` from HEAD `aee6f92`, as one background command of the lead's session (pid 20907), on AC power, from 23:53:34 UTC on 22 Nov to 00:03:13 UTC on 23 Nov, right after the gate closed (D-089). Its log is `switch-20261122T235334Z.log` in the gate's evidence folder (SHA-256 `6cbeb8b5…`, D-089). The times below are that log's, in UTC.
- **The rehearsals** (`--dry-run`, which changes nothing live):
  - **The first,** at 23:46:47, stopped at its step 2, before anything was stopped: `uv sync --locked --no-dev --offline` could not find botocore 1.44.6 in uv's cache. That is the lead's record; this try's log was not kept.
  - **Why:** uv's cache (`~/.cache/uv`) had been emptied, some time between the lead's `make data-report` at 04:17:33 UTC on 22 Nov, which ran `uv`, and 06:51:47 UTC. The folder's birth time is 06:51:47 (`stat`): the lead's next `uv run`, its `make data-report` at 06:51:47, made it again. The lead read it as 16 KiB (`du -sh`: 16K) at 23:47:15 UTC, after the failed rehearsal. The lead's storage cleanup of 22 Nov removed its scratch files, the cargo target folder and an old Lambda image, not this cache. What emptied it is not known: the lead's free-space readings rose from 48 GiB at 04:21:24, after that cleanup, to 92 GiB at 06:50:46 UTC with no further deletion of the lead's (its session log).
  - **The fix:** the lead refilled the cache from PyPI, a free public index, with an online `uv sync --locked --no-dev` of `workers/capture` and `workers/forecast` in a scratch export of HEAD (`git archive HEAD workers viewpoints`), touching no repo venv: 143 MiB of cache (`du -sh`; about 150 MB). The cache's `archive-v0`, `builds-v0` and `wheels-v6` folders were born at 23:48:39 UTC.
  - **The second** passed at 23:48:49–23:48:56 (`switch-dry-run-20261122T234851Z.log`, `61ab02f0…`): the gate check read met; HEAD's workers built offline with their own venvs; the new code's `status` read 8 cameras on the live data; the forecast handler imported; and the three scripts' rewrite showed as diffs. The Mac was on battery then, so a real run would have been refused without `--allow-battery`.
- **The switch, step by step:**
  - 23:53:34, the preflight: power AC; the three live scripts unchanged since 01:40 UTC on 21 Nov (SHA-256); source `aee6f92` (snow-globe-capture 0.3.0); the capture loop pid 70525, the stand-in pid 36474 (working folder in `034c46b`) and the supervisor pid 20119; `current` → `3cd2ca7`.
  - 23:53:35: the gate met (D-089). 23:53:40: the rehearsal passed again, and `caffeinate -is -w 20907` (pid 21036) held from then to 00:03:13.
  - 23:58:07: the 0.2.1 loop's pass ended at 23:58:06.815 (it had started at 23:57:56). The switch stopped the supervisor first. The supervisor's log at 23:58:07 reads "SIGTERM to capture none and forecast none", then "stopped", so it signalled no adopted loop; the switch logged it gone at 23:58:08.
  - 23:58:08: SIGTERM to the capture loop 70525, which exited at 23:58:09.
  - 23:58:09: `~/lookout-data` moved to `~/snow-globe-data`, and a symlink `~/lookout-data` → `~/snow-globe-data` took its place.
  - 23:58:09–23:58:14: the snapshot `~/snow-globe-data/cache/runtime/aee6f92` was built with its own venvs (`uv sync --locked --no-dev --offline`: 23 packages for capture, 30 for forecast). Its `status` read 8 cameras with storage at `~/snow-globe-data`, and the forecast handler imported. The three scripts were rewritten, the old ones kept as `*.lookout.bak`, and `current` → `aee6f92`.
  - 23:58:14 → 00:02:06: a 232 s wait, so that every camera fell due in the new loop's first 60 s window. 00:02:06: the new capture loop started (pid 22115). 00:02:07: it held the NPS fetcher lock and wrote `loop_started` (00:02:07.088). 00:03:10.250: its first pass ended.
  - 00:03:10: SIGTERM to the old stand-in's bash (36474), and the new stand-in started (pid 22288). 00:03:13: its first tick extracted run 2026112223 (3 hours, 8 files, 25,764,369 bytes, 1.8 s); 2026112300 was not on S3 yet.
  - 00:03:13: done. The switch released its `caffeinate` and waits on both loops, which are its children.
- **PLAN.md's step 6 checks** (the docs agent: the logs, the lock, `ps` and the files, read-only, 00:10–00:28 UTC on 23 Nov):
  - **All 8 cameras polled:** the first 0.3.0 pass (`started_at` 00:02:07.110, ended 00:03:10.250) sent 7 JSON GETs, Rainier's HEAD and 4 image GETs (each right after its camera's poll), all answered 200. Outcomes: 4 stored, 3 unchanged and 1 rate capped (Rainier).
  - **The version prefix:** every record of the new loop carries `snow-globe-capture/0.3.0`.
  - **`loop_started`:** pid 22115, `interval_s` 300, `jitter_s` 30 and the 8 viewpoints.
  - **`started_at` on each pass:** on all 4 passes to 00:18:31.
  - **One fetcher holds the lock:** `state/locks/nps-fetcher.lock` names pid 22115, started at 00:02:07Z, running `snow-globe-capture loop --interval 300 --archive-queue …/state/archive/queue.json`. `ps` shows one capture loop (22115) and its `caffeinate -s` (22127).
  - **The User-Agent:** request records do not log it. The snapshot's default is `SnowGlobe/0.1 (webcam research capture; fetch-on-change)` (`workers/common/src/snow_globe_common/http.py`, line 60). The rewritten scripts unset every `LOOKOUT_*` and `SNOW_GLOBE_*` variable before they set the data dir and the manifests, so nothing overrides it. This is checked in the code and the scripts, not on the wire.
  - **The stand-in's first run:** `forecast/hrrr/<viewpoint>/2026112223z.json` at all 8 viewpoints: `kind` `snow-globe.forecast-run`, `live` true, hours 0–2, 8 fields, `snow_globe_forecast` 0.3.0. All 8 pass `validate_document` of `workers/forecast` at `aee6f92` (the docs agent, offline). The run fell due at 00:02:47, when its f01 reached S3 (3,767 s after the run start), and was written at 00:03:13, so no forecast run was late.
  - **The report still reads the old records:** on `~/snow-globe-data` at 00:06:29 UTC (`data-report-20261123T000629Z-postswitch.txt`, `b69a9793…`), the close and the outage totals are unchanged (met at 23:46:10.754Z; capture outages 11.001 h, forecast outages 6.039 h), while the switch's 4.797 s of excess, after the close, raised the restart excess to 0.265 h and the lost total to 11.37 h; the old versions' passes (69, 174 and 590) are counted beside 0.3.0's first.
- **The restart's cost,** from that report. The 0.2.1 loop wrote no `loop_stopped` (D-084), so the report split the restart at its last record before `loop_started`, 23:58:06, 240.3 s earlier. The passes were 303.4 s apart, and their starts 250.6 s. Each camera went 300.070–304.797 s from its last 0.2.1 poll to its first 0.3.0 poll, so the excess is 4.797 s (Yosemite), 0.1 min lost, after the gate. The re-snapshot of 20 Nov cost 763.6 s (D-079).
- **Polite collection across the switch** (the docs agent, on `capture_log/`, with the data report's bases): the least poll gap at nps.gov across the restart was 300.006 s (Big Bend; 300.070 s send to send). The 5-GET windows that span it, to 00:13 UTC, were at least 3,720.2 s (Acadia). The new loop's 90 archive requests to 00:13:12 UTC were at least 5.0523 s apart end to start, none under 5.05 s.
- **The queue on 0.3.0** (`snow-globe-capture archive queue list`, offline, the docs agent at 00:15:49 UTC): 808 entries, 318 done, 489 pending, 1 active and 0 failed, dated 2023-01-30 to 2026-05-30. The runner's heartbeat: pid 22115, `snow-globe-capture/0.3.0`, `auto_resume_max` 3, budget `archive-queue-2026-11-23` at 120 of 1,000. The lead's listing at 00:04:35 read 312 done, 495 pending and 17 of 1,000. So the listing promises the automatic resume, and the lead's manual daily resume (D-072, D-078, D-083) has ended.
- **D-083's version question is settled:** D-083 and D-084 were committed in `c9f68eb` at 11:00:30 UTC on 21 Nov, before the switch, and the snapshot holds them: line 115 of its `workers/capture/src/snow_globe_capture/cli.py` passes `auto_resume_max=AUTO_RESUME_MAX`. Capture stayed 0.3.0. The snapshot's source is also `origin` `main`: `git reflog` shows `refs/remotes/origin/main` set to `aee6f92` "by push" at 11:24:01 UTC on 21 Nov, with the owner's word, given at 11:23:49 UTC in the lead's session. CI's run on it was not read for this entry.
- **D-085's condition is met:** `60e05ba` holds eval's report (D-082) and held-out split version 2, whose `decisions` list cites D-085 (SHA-256 `82574ceb…`, 6,216 bytes), so the split was committed citing its entry, before the switch.
- **The supervisor:** the switch found it through `supervisor.pid` and its working folder (the fix in `aee6f92`, D-081): its preflight named pid 20119, and it stopped the supervisor at 23:58:07–23:58:08, before the loops, signalling none. So PLAN.md's hand stop (step 3), which D-081 kept as a second guard, was skipped (the lead's call). The switch never restarts it (D-076). At 00:10:34 and again at 00:27:52 UTC on 23 Nov, `supervisor.pid` did not exist and the log's last line was the 23:58:07 stop, so it was not running.
  - **The owner started it again at 00:36:33 UTC on 23 Nov** (owner action 1). The docs agent's read at 00:40 UTC: `supervisor.pid` holds 26187, and `ps` shows `/bin/bash …/cache/runtime/bin/supervise.sh` with PPID 1, TTY `??` and STAT `SNs`, so it runs in its own session. Its log: "supervisor 26187 started; current -> …/aee6f92; adopt capture=22115 forecast=22288; awake until 2026-11-23T05:00:00Z", then its `caffeinate -s` (pid 26250) and "caffeinate -ims for 15807 s (pid 26257)".
  - `pmset -g assertions` shows pid 26257 holding PreventUserIdleSystemSleep, PreventSystemSleep and PreventDiskIdle for 15,807 s, so the Mac is kept awake until 05:00:00 UTC on battery too, with the lid open; on battery a closed lid sleeps it whatever the assertions (D-089).
- **What runs now** (`ps`, 00:10 and 00:40 UTC): the capture loop (pid 22115) with its `caffeinate -s` (pid 22127), and the stand-in's bash (pid 22288). Both are children of the switch's process (20907), whose shell's parent is the lead's Claude Code session (pid 72719, the session of D-076). The supervisor adopted them without detaching them (D-076), so they still depend on that session until they exit; after an exit it starts a replacement in its own session. After 05:00:00 UTC only the AC-only `caffeinate -s` assertions remain (D-089).
- **The lesson for every snapshot build** (D-049's procedure): a snapshot's venvs are built with `uv sync --locked --no-dev --offline`, so they need every locked wheel in uv's cache, and a cache cleaner can empty that cache at any time. The rehearsal caught it before anything stopped. So check that the offline build works in a scratch folder (the rehearsal does) before stopping a loop. If the cache must be refilled, do it from a scratch export, never in the repo's venvs or under `cache/runtime/`.
- **What changes for the docs:**
  - the data dir is `~/snow-globe-data` from 23:58:09 UTC on 22 Nov. `~/lookout-data` is a symlink to it, so commands keep naming `~/lookout-data`; a real folder in its place would make every resolver refuse (D-080);
  - the switch refuses to run again, because it needs `~/lookout-data` to be a real folder;
  - a re-snapshot is again the procedure in the supervisor's header: flip `current`, touch `cache/runtime/graceful-stop.capture`, then SIGTERM the capture loop's python and the stand-in's bash, and each restarts from `current`. It needs the supervisor running, and it is the lead's, on AC power or, on battery, with the lid open and `caffeinate -i` held across it (D-079, D-089).
- **Spend:** $0. No paid call was made.
- **Amended 23 Nov (D-104):** the supervisor's keep-awake ended at 05:00:00 UTC on 23 Nov. The Mac went on battery at 05:25:29 UTC and, with the lid closed, slept at 07:12:46–07:49:03 and 08:49:12–09:36:07 UTC: capture lost its 5-minute cadence for 41.0 and 49.1 min, and run 2026112308 was written 37.9 min after it fell due. It is on AC from 09:36:50 UTC.

## D-091 — T3pr queued ahead of T1b with four clear Point Reyes days, not six; the last two come from T1b's September and October weeks · decided (lead; queued from eval's choice) · 2026-11-23 · amends D-072, D-078
- **Who chose:** eval, as D-072 assigns ("eval or the owner, from Point Reyes's T1 frames"), in a read-only workflow of the lead's session (`wf_99ff1e93-e1f`, 00:49–01:30 UTC on 23 Nov). It opened only frames dated before 1 Jun 2026 by local date, UTC stamp and folder date, wrote only to the session scratchpad, and never touched the live queue.
- **Candidates:** the sidecars of the 517 Point Reyes hires T1a frames give 34 local days: the weeks of 9 Jun, 14 Jul and 11 Aug 2025, and 18–30 May 2026. All 34 are complete in their last three daylight hours and at midday, and all are in the fresh `draft-T3pr.sh`'s 182 `T3PR_DAYS`. A metadata score ranked them: the Laplacian variance, chroma and luma of the last three evening frames, the frame nearest sunset counted double. That score is a weak proxy, so the top 14 were viewed.
- **Votes:** three viewers, each weighing one lens (the sunlit landmarks; the sky toward the Sun; the whole day), independently opened each day's last three evening frames and its midday frame. Each judged whether the evening was clear: direct sun on the scene until near sunset, with no fog or low cloud on the landmarks or the Sun (PRD §8.5 step 8, the sunset check).
  - 2026-05-28: 3 of 3 clear, all with high confidence.
  - 2026-05-18, 2026-05-26 and 2025-06-14: 2 of 3, with a positive confidence-weighted margin (low 1, medium 2, high 3).
  - 2026-05-19, 2025-06-11, 2025-08-14, 2025-07-20 and 2026-05-30: 2 of 3, but at or below zero once weighted. 2026-05-19's T3pr run would also end 15–18 min before sunset: the archive has no 20:15 PDT frame, and 20:30 is under −2°.
  - 2025-06-15: 1 of 3. 2026-05-20, 2026-05-24, 2026-05-25 and 2026-05-27: 0 of 3.
- **The lead's call: four now, not six padded.** No day the viewers rejected or split on is used. The four sunsets lie at azimuth 295.4–300.3°, so they cover only the late spring and early summer Sun.
- **The last two** are chosen the same way from T1b's Point Reyes September and October weeks once those frames land. The cost model's stand-ins for them, 2025-09-17 and 2025-10-15, set at 272.3° and 258.7°, which widens the sunset check's spread. They are then queued ahead of T1b's remaining entries, the same way as below.
  - **Estimated:** take the planner's figures at 01:06 UTC: T1a had about 1,258 requests open, and T1b's September 2025 week sits about 1,885 requests into T1b. Add T3pr's 8 entries: 172 new GETs for the four clear days, by their cached searches, plus the four night days' hourly frames. At 1,000 requests a UTC day, that week then lands on about 26 Nov. That leaves time to queue the two and meet T3pr's 29 Nov due date. This is an estimate, not a measurement.
- **What ran** (way (b) of the plan's section 8): one guarded script of the lead's, at 01:40:31–01:40:59 UTC on 23 Nov, against the live queue (`Q=$HOME/lookout-data/state/archive/queue.json`). The running 0.3.0 loop kept working: the queue is edited under its `flock` and replaced atomically.
  1. The drafts were regenerated with `plan.py` from a fresh copy (808 entries, SHA-256 `8ed48eb8…`): 367 T1b entries pending, and no T3pr entry yet.
  2. `draft-T3pr.sh` with `EVENINGS='2025-06-14 2026-05-18 2026-05-26 2026-05-28'` added 8 entries. Four are night days, every hour with any Sun: 2026-01-21, 2026-05-27, 2025-08-13 and 2025-10-15. Four are the clear days, every frame at NPS Sun ≥ −2°.
  3. `archive queue remove` took out T1b's 367 pending entries.
  4. A re-plan from a fresh copy gave the same 367 T1b ids. Its `draft-T1b.sh` (54 `add` lines) put them back.
  5. **The result:** 816 entries, no duplicate id. Positions 1–441 are unchanged, T3pr is at 442–449 and T1b at 450–816.
  
  At 01:41:15 UTC the 0.3.0 runner had used 960 of the day's 1,000 requests, so T3pr starts after T1a's remainder, from 24 Nov.
- **Held-out guard:** every frame T3pr can fetch on those days is before 2026-06-01T00:00Z, by local date, UTC instant and wall-clock date. This was checked with capture's `held_out_reason` and the planner's `plannable_in`; the latest clear-day frame is 2026-05-29T03:30Z. None falls in the sealed window.
- **Evidence** (outside the repo, in `~/lookout-data/reports/t3pr/`):
  - `choice-20261123.json`: the inventory, the votes and the choice (SHA-256 `f0eb277b…`);
  - `queue-before-20261123T014031Z.json` (`8ed48eb8…`) and `queue-after-20261123T014118Z.json` (`57ddfede…`);
  - the three command logs.
- **Spend:** $0. No paid call was made.

## D-092 — Wave 1: five M1 and M1.5 work packages built, reviewed, fixed, verified and committed one per package; `make test` runs 12 suites · decided (lead; record) · 2026-11-23 · amends D-063
- **What ran:** a workflow of the lead's session, at about 02:00–06:10 UTC on 23 Nov (the lead's times; the packages' saved runs fall between 02:03 and 05:58 UTC). Five work packages: Tier 0 (`tier0`), the replay scorer (`harness`), the overlay (`overlay`), the sky (`sky`), and the Rainier plate with extractor v0 (`plate`). Each was built, reviewed through two independent lenses (correctness and the spec), fixed and verified: 25 agents. All five verifiers passed (`"pass": true`); the lead counts 0 errors.
  - Each agent's result is a JSON file (`build_<pkg>.json`, `review_<pkg>_correctness.json`, `review_<pkg>_spec.json`, `fix_<pkg>.json`, `verify_<pkg>.json`; `plate-digest.txt` sums up the plate's). They were written to the lead's session scratchpad, which a reboot or cleanup can clear, so at 11:37:59 UTC on 23 Nov the lead copied them, with `make-test-0943.log` and `overlay-pnpm-test.log`, to the data dir's `reports/wave1/`, which is kept.
  - **Saved in the data dir:** Tier 0's bench, ask and decoder runs (`reports/tier0/`), the plate with its sources (`plates/rainier.paradise-mountain/`), the extractor runs (`scores/extractor-v0/`), and the capture and forecast logs behind D-101, D-102 and D-104. The renderer's raw benchmark and Monte Carlo runs were written to `cache/renderer/`, which CLAUDE.md lists among the deletable caches; at 11:37:59 UTC the lead copied the ones renderer/README.md cites to `reports/renderer/`.
  - **Kept only as the agents' own records:** the verifiers' re-runs that D-093 to D-100 quote (the overlay's three stress runs and its 45 of 806 fades, Tier 0's runs of 04:36–04:41 UTC, the scorer's synthetic run, the sky's `pnpm test`) survive as the numbers in their `verify_<pkg>.json` files in `reports/wave1/`, with no raw output beside them. `app/src/test-results/`, git-ignored, keeps only the latest `pnpm test`'s stress output.
- **Committed** at 09:56:36–09:57:33 UTC, one commit per package:
  - `3029929` core 0.3.0: Tier 0 (M1-08; D-097);
  - `5fdfb3e` bench: the replay scorer and trace schema 2 (M1-09; D-095, D-096);
  - `cacd513` overlay 0.1.0: the web layer, its protocol and the paint gate (M1-07, M1-13; D-093, D-094);
  - `5e50a9a` renderer 0.3.0: the Hillaire 2020 atmosphere (M1.5-01; D-098);
  - `0292fe8` plates 0.1.0 and eval's extractor v0 (M1.5-02, M1.5-03; D-099, D-100);
  - `22930cc` build: `make test` and CI run the overlay, plates and replay suites.
  
  They were pushed to `origin` `main` at 11:13:14 UTC (`git reflog`: "update by push"). CI's run on that push was not read for these docs.
- **The lead's changes before committing:**
  - `app/src/test/stress.browser.test.ts`: the broken overlay `esc-cancels-stages-only` had to be caught at least 10 times in 2,400 cycles. It was caught 17, 15, 12 and 11 times in four full runs, and 9–23 times (median 12) in 16 more samples, one of them below 10, so `pnpm test` could fail about one run in 16–20 although the mutant was caught every time (verify_overlay.json). Its own floor is now 3 (`AT_LEAST_VARIANT`); the real overlay must still show 0 of every violation. The full overlay suite then passed: 151 tests.
  - `app/src/preview/preview.ts`: the preview's Tier 0 sample was taken after its own question. Its `taken_at` is now 2026-11-18T15:45:00-08:00 (PRD §9.1: only frames taken before it).
  - `THIRD_PARTY_NOTICES`: zune-jpeg 0.5.15 and zune-core 0.5.3 (3a); the Hillaire port moved from "planned" to section 2, with its MIT line checked by the lead (D-098); the USGS 3DEP raster and the Bright Star Catalogue as data sources (D-099, D-103); plates' closure (4e–4g); the overlay's Node packages (5b).
  - The Makefile and `ci.yml`: `TEST_SUITES` gains `plates` and `overlay`, and `BENCH_CHECKS` runs bench/replay's 93 tests. CI gains a plates job, with no Rust (eval's default `oculus` group does not carry over, so `plates/uv.lock` holds no oculus-core-py), and an overlay job.
  - The data dir's stale `cache/tier0/library-v1.json` is deleted. Tier 0's cache is `library-v2.json` (`app/core/src/tier0/library.rs`); build_tier0.json's "v1" predates the fix.
- **`make test` on the committed tree** (the lead, 09:43–09:54 UTC; `reports/wave1/make-test-0943.log`): all 12 suites passed in 652 s. common 315 tests (11 s), capture 366 (137 s), forecast 184 and 1 deselected (22 s), eval 468 and 1 skipped (24 s), plates 66 (9 s), renderer 220 (187 s), overlay 151 (113 s), rust 192 (10 s), speech 103 (54 s), infra all checks (80 s), docs 102 (4 s), bench 42 (2 skipped) and 93 (4 skipped) (1 s). bench's 6 parser-dependent tests skipped, as designed, with no fresh release `snow-globe-parse`: 4 end-to-end tests in bench/replay and 2 in bench/utterances.
- **Network:** only free, public, keyless sources: the npm registry for the overlay's lock file; the USGS National Map for the plate (GNIS at 02:03:45 and 3DEP at 02:05:30 UTC, 16 requests by the builder's count; `sources/provenance.json` records the User-Agent `SnowGlobe/0.1 (plate builder; public landmark lookup)`, but the fetch was a one-off command that is not committed and logs no requests, so the User-Agent is checked neither in code nor on the wire, verify_plate.json); and the lead's check of the Hillaire licence on GitHub. The packages report no NPS or NOAA request.
- **Spend:** $0. No paid call was made.

## D-093 — The overlay's core-to-webview protocol v1 is the IPC contract the M1 shell implements · decided (lead) · 2026-11-23
- **The contract:** `app/src/overlay/protocol.ts` defines it once. The TypeScript types, the runtime validator and `app/src/protocol.schema.json` (JSON Schema 2020-12; a test fails when the committed file is stale) all come from that one definition. Every message carries `v: 1`; there are 17 inbound types (core to webview) and 10 outbound.
  - Delivery is in order on one channel. `query_gen` rises by one per key press from 1, and stays below 2^53.
  - `scene_hash` travels as 16 lower-case hex digits, because a 64-bit JSON number loses precision in JavaScript.
  - `parsed` carries the core's Query JSON verbatim, D-040's fields included, under a strict schema.
  - A tier's label kind is bound to its tier (0 photo, 1 simulated, 2 refined), so a simulated frame cannot be labelled a photo.
  - Errors and refusals carry codes and paths, never text.
- **The readings accepted with it** (build_overlay.json, "decisions"):
  - **the gate** mirrors `lifecycle.rs`'s `PaintGate` rule for rule (generation, dismissed, scene, tier). `begin` ignores a generation that is not newer, because the generation arrives in a message: a late `query_started` cannot reopen a gate that Esc closed. The fix added a second difference, `clearScene`: a scene shown on a stable partial is withdrawn when the query ends as No match or Out of range (`gate.ts`);
  - **when:** the gate is checked at receipt (no decode when refused) and again inside the frame callback, in the same synchronous block as the draw and the caption;
  - **D-012 row 11, the skip rule:** a ready Tier 0 is held only while nothing of its scene is shown and Tier 1 of the same scene is decoding, or announced as due within 100 ms of Tier 0 being ready. It is skipped if Tier 1 comes in time, released at once if Tier 1 is rejected, and otherwise painted in the last frame before ready + 100 ms. With no announcement, Tier 0 is never delayed;
  - **captions:** where PRD §5.3's examples and §5.5 differ, §5.5 wins (the date and the zone always);
  - **the destination chip** shows what really leaves the machine. If on-device only is set and the core still names a provider, the chip shows it as a warning and the overlay sends `protocol_error`. `destination_shown` goes out per key press, two frame callbacks after the chip is on screen, so the core can hold the first cloud byte until then (PRD §15.1).
- **What the shell does to meet it** (the hand-offs in fix_overlay.json): write `SceneHash` as 16 hex digits (`lifecycle.rs` writes a bare number today) and map the tiers both ways (`"tier0"` to 0, …); send `tier_rejected` for every tier that will not come; answer `use_offer` with `scene_changed`; serve frames under `snowglobe://localhost/`. It also maps Tier 0's names onto the protocol's: the core's `sun_altitude`, `sun_azimuth`, `day_of_year`, `live` and `nps_archive` against `sun_position`, `time_of_year`, `capture` and `archive`, as the Tier 0 fixer recommended (fix_tier0.json). Whether a night match on Sun altitude alone gets its own protocol value is open. A `clear_scene` in `PaintGate` would keep the Rust model in step.
- **Evidence** (verify_overlay.json; app/src/README.md): `pnpm test` 17 files, 151 tests (134 unit, 17 browser). Three runs of the real overlay in headless Chromium, 64 lanes × 160 = 10,240 cycles each, gave 0 on all ten checks, stale paints included; gate check to end of draw p99 0.10–0.155 ms; Tier 0 held at most 98.6–101.4 ms. All 10 broken overlays were caught. The TypeScript mirror of `keymash.rs` reproduces every Rust count on its four seeds (40,000 cycles, 0 stale). The validator and Ajv in strict mode agree on 4,120 near-miss messages.
- **Not shown yet:** this is headless Chromium with a simulated core. WebKit's frame callbacks, Web Animations timing and Reduce Motion in the real panel are untested until `app/src-tauri` exists (TODO(M1)).
- **Open, for the lead** (verify_overlay.json): a copy burns in caption line 1 and the credits but not line 2, although PRD §5.5 and §15.1 have copies carry the caption; and with on-device only set, a Tier 2 frame that arrives still paints and is marked shown while the chip reads "on-device only", so the overlay relies on the core never sending one.

## D-094 — A 120 ms linear fade reaches 50 % at 66.7–83.3 ms, so PRD §10.1's 60 ms allocation cannot be met as written · proposed (for the owner); fades that cross 50 % after a scrub are open, for the lead · 2026-11-23
- **Measured** (verify_overlay.json, three stress runs; app/src/README.md): PRD §5.5's 120 ms cross-fade for Tier 1 and Tier 2, linear, reaches 50 % opacity 83.33, 66.67 and 83.33 ms after its first frame at 60 Hz (p50 per run). PRD §10.1's Tier 1 chain allocates "fade to 50 % ≤ 60".
- **Why:** the Web Animations clock starts at the commit frame or one frame later, and at 60 Hz the first frame at or past 60 ms of fade comes 66.7 ms after its start. The fade was kept linear; it was not tuned to the number.
- **What it costs:** at 83.3 ms in place of 60, the chain's sum (≈ 388 ms) becomes about 411 ms: past the 400 ms stretch target, inside the 1,000 ms target. These are page timestamps; the PRD's paint is the composited frame (§10.4), which the replay harness will measure.
- **Options the package names:** an ease-out curve that reaches 50 % sooner, or a shorter fade. Keeping the fade as it is leaves the allocation missed by 7–23 ms.
- **Needed from the owner:** an ease-out curve, a shorter fade, or the fade as it is with the miss recorded. Tier 2's chain allows "fade ≤ 60" too (PRD §10.1), so the choice covers both tiers.
- **Open, for the lead (a design question, not ruled on yet; PLAN.md, 1c):** after a scrub or an AM/PM flip, `PaintScheduler.sceneChanged` lets the old scene's running Tier 1 or 2 cross-fade go on, and it can reach 50 %, PRD §10.4's paint point, while another scene is selected. One instrumented run found 45 of 806 cross-fades doing so (verify_overlay.json). The stress harness judges staleness at the draw, so its checks stay 0.
  - Those fades' `paint` reports carry `half_ms` with the old scene's hash, and §10.4's pixel tag carries only (query_gen, tier). So a paint-capture harness would read them as current paints, against §10.3's "a wrong paint never counts as a hit". D-096's open tag question is the same gap.
  - The options (verify_overlay.json): freeze or cut a running fade on a scene change, or count a 50 % crossing after the change as visible-wrong time (`previous_ms` already covers the interval). Either way the core ignores `half_ms` for a scene that is no longer current.

## D-095 — Trace schema 2 and its one pinned example are the Rust–Python contract · decided (lead) · 2026-11-23
- **Schema 2** (`app/core/src/trace.rs`, `TRACE_SCHEMA` 1 → 2, in `5fdfb3e`) adds the fields bench/README.md marked TODO(M1), and what the scorer needs to work from traces alone:
  - `context`: the last place the parser was given, as manifest ids the place table knows, or `empty`, `not_recorded` or `unlisted`;
  - `state.display` (D-068); `speech`, with the engine, `silence_pad_ms`, `model_retention` and `warm_up` (D-067); `levers` (`pre_render`, `partial_paint`, `paint_capture`);
  - `paints`, each with `matches_final`; the mark `audio_first_sample`; the spans `partial_lag` and `stable_lag`;
  - `replay`: `script_hash`, `utterance`, `take`, `session` and `trial`, each of a fixed form, and a rejected id is never echoed.
  
  It is additive, except that `Mark::ALL` grows from 14 to 15 entries and `TraceState` gains a field.
- **Still no transcript text:** every string is an enum name, a timestamp or a validated id, and a JSON-walk test in Rust and one in Python prove it.
- **The contract:** one pinned example, `SCHEMA_TWO_EXAMPLE`, a raw string in `app/core/tests/trace_fields.rs`, is compared with the writer's own line. `bench/replay/trace_example.py` reads it from that file for the Python tests, and `test_traces.py` checks the Rust enums and struct fields against the reader. So the writer and the reader cannot drift apart unseen.
- **Not a trace in the repo:** CLAUDE.md keeps traces out of the repo. The example is a schema example built on a fake clock, not a captured trace; it holds no transcript text and lives inside the Rust test, not in a trace file. build_harness.json still names `bench/replay/testdata/trace-schema-2.example.json`; the fixer deleted that file and moved the example into the test (fix_harness.json, F25).
- **Evidence:** `trace_fields` (10 tests) and `trace::` (6) pass (verify_harness.json), inside `make test`'s 192 Rust tests (D-092).

## D-096 — The replay scorer's readings: the wrong-paint gate is the one-sided 95 % Wilson upper bound per trial that painted from a partial · decided (lead; the owner may overrule) · 2026-11-23 · amends D-057
- **What exists:** `bench/replay/score.py` and its report, standard library only (`5fdfb3e`).
  - It re-parses each final transcript with the real `snow-globe-parse`, at the row's reference clock and at the context the trace records (D-095).
  - It measures every stage from the trace's marks. A tier's paint time is its earliest recorded paint, never a mark alone.
  - It reports per setup × split × state × display, plus idle follow-ups, as Markdown and JSON with no transcript text. Intervals come from a seeded cluster bootstrap of at least 10,000 resamples, by utterance, and by session too when a group spans sessions (Owen's pigeonhole bootstrap).
  - It refuses a parser binary older than its sources, or, for a measured run, one whose SHA-256 differs from the run's.
- **The lead's readings** (build_harness.json, "decisions", as the fixer left them):
  - **Wrong paints** (PRD §14: "≤ 2 % (Wilson upper bound), or partial paint disabled"): the gate figure is the one-sided 95 % Wilson upper bound per trial that painted from a partial, the trials at risk. The figure per judged trial is reported beside it, with the two-sided interval and a cluster-bootstrap interval, which is never the target's bound. With no wrong paint at all, the one-sided bound first falls under 2 % at 133 such trials (the two-sided at 189).
  - **The headline** is idle trials on the test split (D-068), on which PRD §10.1 defines the latency budgets. The accuracy targets (exact place and time, the fallback rate, wrong paints) are shown on the whole test split, every state, as PRD §14 sets them, with idle beside. The dev split is reported apart and never used for claims.
  - **Exact place and time** is D-057's figure: every trial of a row not tagged `owner-pending`, with its denominator named. It carries the ≥ 95 % target. The settled rows alone (less d06 and t074, stored as `no`) and the owner-pending rows are reported apart.
  - **Partial lag** is each PARTIAL event's arrival minus the end of the audio it covers, pooled over events; stable lag the same way. **A fallback row is placed right** when the re-parse reaches the fallback or its viewpoint.
  - **Normalised WER** spells numbers, a.m./p.m., hyphens and Mt/Pt the same way on both sides. A place alias the hypothesis says for a place the reference names may be read as the reference's own words, and the reading with the fewest edits counts. N is the reference's word count after the spelling steps (fix_harness.json, F19).
- **Nothing is measured yet:** the scorer has run only on synthetic trials, and every report says so at its top. 650 synthetic trials took 43.8 s at `--jobs 9` on 10 cores, and 25 of 25 headline figures matched the verifier's own recomputation (verify_harness.json).
- **Still open:**
  - TODO(M1): the recording tool, the harness, and the writers of `transcripts.jsonl` and `run.json`; real-use trials, which are read and rejected with a reason; a trace mark for a Tier 0 that was ready but skipped; `KEY_UP_TOLERANCE_MS`, 50 ms as proposed.
  - For the lead: PRD §10.4's pixel tag carries only (query_gen, tier). When Tier 0 paints the partial's scene and then the final's in one query, the capture cannot tell which composite showed which. Either the tag also carries a scene index (a PRD §10.4 change) or the webview's gate logs each composite's scene (bench/replay/README.md).
  - For the lead: a trial labelled idle but idle for less than 30 min is flagged and counted, yet stays in the idle headline, because the groups follow the trace's label (verify_harness.json).
  - For the lead: the parser staleness check goes by file time. So a measured run scored with its own frozen parser (SHA-256 matching `run.json`) after any later core edit needs `--allow-stale-binary`, and the report then calls the parser "older than its sources" (verify_harness.json).

## D-097 — Tier 0: a provisional distance, the sealed window by local or UTC date, and only frames taken before the ask, in the product too · decided (lead) · 2026-11-23
- **What exists** (core 0.3.0, `3029929`): the `tier0` module and two commands, `snow-globe-ask` (an utterance in, a real photo out) and `snow-globe-tier0` (`index`, `bench`). The library reads capture's sidecars only (`frames/`, `thumbs/`, `archive/`), one entry per capture, and caches itself at `cache/tier0/library-v2.json` in the data dir. The caption is PRD §5.3's: "REAL PHOTO, CLOSEST MATCH · taken … · matched on …".
- **The distance is provisional: documented, not fitted** (`matcher.rs`). d = √(A² + Z² + Y²), where A is the Sun's altitude on an asinh scale (6°); Z is the relative Sun azimuth over 15°, weighted by the cosine of the mean altitude and faded out as the asked Sun goes from 0° to −12°; and Y is the gap in time of year over 45 days. A frame qualifies within d ≤ 4.0; ties go to the lower key.
  - Fitted weights, the "no close analog" limit and one distance shared with eval's R3 analog (`baselines.py`: 1.0 × Δaltitude, 0.25 × Δazimuth, 0 × Δday of year, a 5° limit) wait for the pre-registration (TODO(M2)). Until then Tier 0 and R3 can choose different analogs.
  - Because Y stays, PRD §9.1's fallback ("Sun position only" until the forecast has loaded) is read as: the weather class is what falls back, and Sun position and time of year stay. The caption says "matched on Sun position and time of year" and "weather not matched". The weather class itself is TODO(M1.5).
- **The sealed window is excluded by local OR UTC date,** from every stamp a live sidecar carries: image time, file name, folder, fetch time and candidate image times. That is stricter than the split's local-day basis. It drops 14 Dec from 00:00 UTC on 15 Dec to local midnight: 16:00–23:59 PST at the Pacific cameras, and from 17:00 MST, 18:00 CST or 19:00 EST at the others, so it costs development frames only. The archive's 1 Jun 2026 edge uses capture's own readings and fails closed (D-070). Tier 0 compiles in eval's committed splits, applies every period of the current one, and refuses to run while a newer split file is committed beside it.
- **Only frames taken before the time of asking, and fetched by it, qualify,** in the product as in evaluation: PRD §9.1's leakage rule, applied everywhere. An ask replayed with `--now` never sees a later frame. An archived frame counts as available from its image time, as in eval.
- **Decoder:** zune-jpeg 0.5.15 (MIT OR Apache-2.0 OR Zlib), about twice as fast as jpeg-decoder 0.3.2 at every size: 640 px thumbnails p50 1.15 against 2.25 ms (`reports/tier0/decoder-choice-20261123T042308Z.txt` in the data dir); the builder's earlier, unsaved run gave 0.57 against 1.28 ms (build_tier0.json).
- **Measured** (verify_tier0.json, 04:36–04:41 UTC; release build, M4, AC, a load of 4.3–11.6 from other agents, so noisy): 7,047 sidecars gave 5,952 entries; match p95 52.8–56.6 µs over 2,000 queries; the key-up core path (parse, match, decoded-set lookup) p95 249–331 µs; 7,997 of 8,000 times asked in the next 48 h already decoded, on a 1-minute grid; 0 decode failures in 5,952; `snow-globe-ask` 57–115 ms end to end, the index load included. The saved run of 04:23:58 UTC (`reports/tier0/bench-20261123T042357Z.json`): 5,941 entries, match p95 68.0 µs, key-up p95 281.8 µs.
  - PRD §10.1's Tier 0 budget, ≤ 300 ms from key-up to paint, cannot be measured until speech and the shell exist. The core's share is under 0.4 ms at p95.
- **Stubs:** the weather class and D-037's camera-fault class (TODO(M1.5)); the Same view gate, the fitted weights and D-019's option (b) (TODO(M2)); prefetch on partial transcripts (TODO(M1)).
- **Open, for the lead** (verify_tier0.json): a capture enters the library if any of its copies passes the rules, so a held-out `frames/` sidecar beside a passing `thumbs/` one would still index it from the thumbnail (none on disk: all 977 frame and thumbnail pairs agree); and the decoded prefetch set has no cap or eviction, 81–96 MiB per viewpoint for 48 h, so the shell must drop other viewpoints' sets.

## D-098 — Renderer 0.3.0, the Hillaire 2020 atmosphere: its deviations from the reference and its cost · decided (sky package; the licence line checked by the lead) · 2026-11-23 · amends D-013, D-041
- **Built** (`5e50a9a`; the package and `RENDERER_VERSION` 0.3.0): Hillaire's lookup tables, ported from `sebh/UnrealEngineSkyAtmosphere` (MIT). Transmittance and multiple scattering are cached per atmosphere; the sky view (two tables), irradiance, light and aerial perspective are rebuilt every frame on the GPU. A float64 CPU twin mirrors every pass and is tested against it. The light on the plate is the drawn sky: the diffuse light is the irradiance of the same sky-view table, and direct light goes through the same transmittance table.
- **Deviations from the reference,** each for this renderer's cameras (build_sky.json, renderer/README.md):
  - the ground is the mean sea level of a 6,371 km Earth at every viewpoint;
  - the scene's haze is an exact aerosol layer (grey, albedo 0.9, Mie phase) on top of the reference's atmosphere;
  - the Earth's shadow is a smooth fraction of the disk;
  - the aerial perspective spans 0.25–500 km on a log scale (the reference: 32 km), because sight lines and slabs reach hundreds of kilometres;
  - the ground albedo is 0.3 (v0);
  - each ray's cells are cut at its lowest point and where it crosses the haze top and the Earth's shadow, 24 or 20 to a piece, crowded at both ends. The first layout was off by up to 12.7 % at sunset;
  - the shaders use their own cos, acos and asin, because SwiftShader's are off by up to 1.9e-4.
- **A meaning changed:** `scene.haze.beta_per_km` is now the aerosol on top of the clear air, and fog is the visibility's excess over all of it at the ground. So eval's default of 0.03 per km gives a visual range of about 93 km at 1,650 m, not the ~130 km its comment says.
- **Cost on the M4's GPU** (Chromium ANGLE/Metal, the closest stack to the product's; 6 alternations of 100 frames at a load of 3.1–4.1; renderer/README.md):
  - the atmosphere adds 0.6–1.0 ms a frame at 720p and 1.1–1.7 ms at 1080p;
  - heavy 1080p: p50 5.4–5.7 → 6.7–7.0 ms, p95 6.8–8.0 → 8.3–8.7 ms; clear 1080p p95 4.6–4.9 → 6.8–9.4 ms;
  - first frame: 478 ms right after a shader change, then 54–74 ms of page time, against PRD §9.2's ≤ 50 ms of GPU time (`fenceSync`). Page time is not GPU time, and the GPU time is not measured, so that target is not shown met (renderer/README.md);
  - against PRD §14's ≤ 8 ms at 1080p (defined for WKWebView and measured in M2), heavy 1080p's p50 is under and its p95 over, and clear 1080p's p95 is over in 3 of the 6 alternations (`reports/renderer/bench-sky-2026-11-23/fix/metal-*-after.json`, copied from `cache/renderer/`). The sky verifier's two later runs, at 06:06 UTC (verify_sky.json), fall inside these ranges, except a heavy 1080p p95 of 8.1 ms, still over 8 ms. In the earlier saved runs heavy 1080p's p50 was over 8 ms in 2 of 3 (12.2 and 13.8 ms). The bottleneck is still the clouds: the main pass takes 7.9–8.0 ms of the heavy profile's 11.2–11.5.
- **Cost on SwiftShader,** the test environment: the golden scenes are 3.8–14.4× slower and heavy 1080p 1.36–1.53×, and each process's first frame is 1.8–2.45 s longer. `pnpm test` is 26 files and 220 tests in 192 s (185 tests in 34.8 s before).
- **Accuracy:** the sky view is within 1.30 % of an independent integral with the Sun at 0–45°, and within 3.23 % at −12° to 0°, for cameras at 150–2,150 m and hazes of 0.01–0.3 per km. Aerial-perspective lookups between slices are off by up to 36.6 % at a 0° Sun inside the haze: at most 2 levels on the goldens and 6 at sunset (TODO(M2)).
- **The Monte Carlo gate** gives 0.2.0's maxima again (1.32, 1.54, 0.80 and 0.98 points, against PRD §9.5's 5). The 12 files it runs are byte-identical to 0.2.0's, so this is a carry-over, tied to 0.3.0 by the GPU-against-CPU opacity check run again on it. A gap from 0.2.0 remains: the Monte Carlo's drawn path (`montecarlo.ts`) is a separate implementation from `cpu/reference.ts`'s α, which the GPU test checks, and no test ties the two per ray (verify_sky.json).
- **Licence:** the line "Copyright (c) 2020 Epic Games, Inc." was written from memory, offline, by the sky agent. The lead checked it against the reference repository's LICENSE on 23 Nov 2026, and THIRD_PARTY_NOTICES now lists the port in section 2. The notice is a legal comment that the bundle keeps, and a comment in the shaders.
- **What it does to earlier entries:** D-013's frame time is now measured, on Chromium/Metal (WKWebView in M2), and its goldens exist; its low-Sun stratum is still open. D-041's goldens are regenerated for 0.3.0.

## D-099 — Rainier plate v0.1: the camera 457 m from the manifest's position, Columbia Crest hidden, so P1's summit band is the visible dome above 4,000 m; the near band not valid · decided (plates package and the lead); the manifest proposal not applied · 2026-11-23
- **What exists** (`0292fe8`): `plates/`, the uv project `snow-globe-plates` 0.1.0, an offline builder (`build`, `match-stars`, `candidates`; no network code). It writes a plate set to `plates/<viewpoint_id>/<version>/` in the data dir, never the repo. Every input must pass eval's committed split as a frame a fit may use (`frame_file_verdict`).
  - Plate v0.1 of `rainier.paradise-mountain` was built at 04:35:27 UTC on 23 Nov. Its `plate.json` names the 13 input frames with their SHA-256 (3 base, 1 geometry, 9 night), and a verifier's rebuild reproduced every PNG byte for byte.
- **Base frame:** the per-pixel median of the bright-overcast frames of 21 Nov at 17:50:04, 18:21:01 and 18:37:01 UTC, which register with zero shift. All 105 daytime captures were viewed, and only 21 Nov 17:19–20:28 UTC had overcast with the mountain out near midday. So PLAN.md's risk of no suitable frame did not happen. Not dehazed (TODO(M2)).
- **Pose,** solved jointly from 3 GNIS landmarks marked by viewing (Gibraltar Rock, Anvil Rock, Little Tahoma Peak) and 37 stars in 9 infrared night frames of 20 Nov: heading 2.137°, pitch 9.994°, roll 1.643°, hfov 68.56°.
  - **The camera** is at 46.787833, −121.7370365, its lens at 1,697.95 m with an assumed 10 m above the 3DEP ground. That is 457 m from the manifest's position (275.1 m east and 365.1 m north), which assumes the Jackson Visitor Center to ±500 m. A lens 3–40 m above the ground puts it 325–439 m north instead.
  - The landmark angles (0.12–0.28°) are fit residuals, not a check. The stars sit at a median of 1.62 px; the landmark-only and star-only poses differ by a median of 0.49° (at most 0.84°) across the frame.
  - **The independent check,** the DEM skyline against the observed one per band, at p90: summit 0.41°, upper 0.37° and mid 0.16°, against a 1° bar at p90, which the plate's spec takes from PRD §8.5's landmark reprojection limit (`band_registration_bar`). The near band misses at 1.22°.
- **Columbia Crest is hidden from this camera:** a ray cast through the USGS 3DEP DEM shows the dome's nearer slope above the line of sight by up to 0.47° 225 m in front of the crest (0.43° from the manifest's position). The manifest gained a source note saying so (committed in `0292fe8`; the crest's pixel stays null). So P1's summit band is the visible dome above 4,000 m (4,247 px).
- **The near band is not valid,** so Rainier's K2 band, the near ridge (PRD §13.4), cannot be used until the camera position or model is fixed (TODO(M2)). K2's landmark mapping is part of the 14 Dec freeze (D-019), four days into M2, so this fix is needed by then. If it is not ready, what the freeze records for Rainier's K2 is for the lead. A near-field refit was tried and not adopted: no single pinhole camera fits the stars, the far landmarks and the near ridge together (fix_plate.json).
- **The manifest proposal,** `plates/specs/rainier.paradise-mountain.manifest-proposal.json`, is the lead's call and is not applied: the solved camera position, a pose block and three more landmarks. Applying it needs an `upper` value in the schema's landmark band enum and changes to the forecast tests (`test_rainier_points`), because the forecast worker turns every manifest landmark into a sample point. It is wave 2's work.
- **Sources:** USGS 3DEP and GNIS, public domain, with their requests in the data dir's `plates/rainier.paradise-mountain/sources/provenance.json`; the star catalogue in D-103.

## D-100 — Extractor v0 misses PRD §13.8's bar on the summit, so it cannot score P1; extractor v0.2 is next · decided (lead; record) · 2026-11-23 · amends D-037
- **What exists** (`0292fe8`, eval): `extractor_v0.py` measures v−1's normalised Sobel energy in each plate band (summit, upper, mid, near) against the median of the 3 clear-day frames nearest in Sun position: within 3° of altitude and 15° of azimuth, and never from the scored frame's own local day. Its classes are visible ≥ 0.60 > partial ≥ 0.35 > hidden, v−1's placeholder τ, fixed before any v0 ratio.
  - `snow-globe-eval extractor-v0` writes each run to `scores/extractor-v0/<viewpoint>/<UTC stamp>/` in the data dir, with a line in `runs.jsonl`.
  - The labels cover Rainier's 105 daytime frames of 19–22 Nov, by viewing: `labels_rainier_v0.json`, the first pass, and `labels_rainier_v0_1.json`, which labels upper, mid and near again.
- **The run** (20261123T044317Z; plate v0.1, labels v0.1): 323 captures, 218 at night and 105 by day. 44 were scored, and 61 of the 105 (58 %) had no usable reference.
- **Validation** (that run's `validation.json`), with the 18 reference-pool frames left out: **summit F1 0.50 on 28 judged frames** (2 hits, 4 misses, 0 false alarms, 22 correct negatives; POD 0.33, PSS 0.33), against PRD §13.8's 0.85. Upper and mid score F1 1.0 (n 32 each), and near 1.0 (n 30, on a band the plate marks not valid), all on unambiguous frames. `publishable` is false, with 10 reasons.
- **Why it fails** (verify_plate.json):
  - it reads bright-overcast summits as partial or hidden, because its clear-day references show a sunlit skyline against a blue sky. It even calls the summit partial in the plate's own three base frames (ratios 0.36–0.46), which were chosen because the summit was visible;
  - 54 of 87 judged frames had no reference. The captures hold only the clear mornings of 20 and 21 Nov and the alpenglow of 19 Nov, so no afternoon frame was scored, and 29 of the 33 scored come from 21–22 Nov;
  - one labeller, the agent that built it; no re-label a week later, no second labeller, no labelled renders;
  - the camera-in-cloud flag caught 0 of the 2 in-cloud frames it could score.
- **So v0 cannot score P1** (PRD §13.8: a metric that fails its bar is not reported), and a meaningful development P1 table, M1.5's gate (by Thu 10 Dec), waits for an extractor that passes. The 14 Dec freeze, which doesn't move, fixes τ and the partial band (D-019), which come from the extractor, so extractor v0.2 is freeze-critical work (PLAN.md, Calendar).
- **Next, the lead's plan:** extractor v0.2, with references conditioned on the sky state (overcast frames with the summit visible among them), more labels from two independent labellers, and a leave-one-day-out check. Which bar M1.5's table needs, validation.json's `meets_f1_bar` (F1 ≥ 0.85 on the labels at hand) or its `publishable` (the full §13.8 sample), is the lead's call, still open.
- **D-037's Same view gate is now applied,** in extractor v0, with v0 values (TODO(M2)): the plate's base is registered onto the frame, within 4 px, and the gate is not checked below an NCC of 0.3. 99 of the 105 daytime frames register at 0 px and 6 at 2 px; none failed.

## D-101 — The forecast stand-in serves only design P's 1 h lead; the AWS deployment would serve the others to 24 h · proposed (for the owner, with owner action 2) · 2026-11-23 · amends D-050
- **The need:** design P pairs live captures with live forecast files at leads of 1, 3, 6, 12, 24 and 48 h (PRD §13.2; the hourly issuer, §11.2), through the sealed window of 15 Dec 2026 – 14 Jan 2027. P1 is at 24 h.
- **The gap:** the local stand-in keeps f00–f02 (D-050). Its newest hour, f02, is valid 61–66 min after S3 publishes it (f02's lags on 23 Nov were 3,245–3,537 s, `forecast_log/2026-11-23.jsonl`). So its files serve a lead of about 1 h at most, and none of 3, 6, 12, 24 or 48 h.
- **AWS would cover the leads to 24 h:** the deployed forecast job extracts all 22 fields at every hour, because Terraform sets no `SNOW_GLOBE_FORECAST_*` variable (infra/README.md, "Forecast files at the deployed profile"). No file of that profile has been written yet (the same section), so this rests on its configuration, not on a run. Its f48 gives about 46 h of lead at p95, so no run covers the 48 h lead, whose reading is D-102's. Applying it before 15 Dec (owner action 2) closes the rest of this gap, as it closes capture's.
- **Otherwise** the stand-in needs a design-P profile before 15 Dec: the hours each lead needs, out to f48 at the synoptic cycles. Its size is not measured. For scale, a whole run of all 22 fields is 453 MB of GRIB to f18 and 1,202 MB to f48 (workers/forecast/README.md).
- **What the docs already said:** PLAN.md's fixed dates and D-014 need AWS live before the window, for unattended capture; D-030 ends the stand-in when AWS is live, and notes that a late fetch makes a run's design-P availability late (D-027); D-035 asks whether the hard stop stays on through the window. None said that the stand-in's hours cannot serve the window's leads.

## D-102 — The 48 h lead: with lead = valid − availability, f48 reaches about 46.1 h at p95 · proposed (for the owner) · 2026-11-23
- **The conflict:** PRD §13.2 and §11.2 list a 48 h lead, "wherever a run covers them". Lead time is valid − availability (D-008, D-027). HRRR's longest hour, f48 of the 00, 06, 12 and 18Z runs, is valid 48 h after its run's start but reaches S3 1.8–2.0 h after it, so no run covers a 48 h lead. PRD §8.3 also says "24 h and 48 h leads come only from the 00/06/12/18Z runs, so the actual 24 h leads are 24–29 h; the actual lead is kept". At 48 h no run's actual lead reaches the nominal one.
- **The figures:** f48's 29 lags in the logged survey (`forecast_log/lag_survey/2026-11-20.jsonl`, D-053): median 107.57 min, p95 111.80 min by the nearest rank, max 116.65 min. With the 4 live f48 lags of 19–20 Nov, which the lag model pools with them (D-027), n = 33: p95 116.65 min and max 117.48 min, D-053's figures. Either way the longest lead at p95 is 46.1 h (46.14 h and 46.06 h).
- **Two readings:**
  - (a) keep the definition, so the issuer freezes no 48 h prediction, and HRRR's longest lead is reported as it is, about 46 h;
  - (b) keep the definition, and let the "48 h" row hold f48 at the lead it has when it is issued, about 46 h at p95, named as such.
- **Not affected:** P1, at 24 h, which the synoptic runs cover.
- **Needed from the owner,** before the 14 Dec freeze: (a) or (b).

## D-103 — The Bright Star Catalogue's terms are the owner's to confirm · proposed (for the owner) · 2026-11-23
- **The use:** `snow-globe-plates match-stars` (`starmatch.py`) reads the Bright Star Catalogue, 5th revised edition (Hoffleit & Warren 1991; CDS V/50), from the owner's own local copy under `~/Documents/Oculus`, to match stars in Rainier's night frames for the camera's pose (D-099).
- **Nothing of it is in the repo:** the build refuses star positions inside a spec. The matched positions live only in the data dir's match file, `plates/rainier.paradise-mountain/sources/stars_bsc5_v0.1.json`, which the spec cites by SHA-256. The first spec, with 37 Hipparcos-2 positions (CDS I/311, "free for scientific use"), was moved out of the repo to `sources/superseded/` and was never committed.
- **The terms are unconfirmed:** the catalogue's ReadMe states no licence, and the owner's Oculus ledger lists it as public (fix_plate.json); neither was checked further offline. THIRD_PARTY_NOTICES lists it with its terms "still to be confirmed by the owner".
- **A privacy note:** `plate.json` and the match file record the catalogue's absolute path, which names this Mac's user account. That is harmless in the private data dir; strip it before any plate goes to object storage or otherwise leaves this Mac (verify_plate.json).
- **Needed from the owner:** confirm that the terms allow this use, with the positions kept in the data dir, or name another catalogue.

## D-104 — 23 Nov on battery after the keep-awake ended: two lid-closed sleeps cost 41 and 49 min of 5-minute capture, and run 2026112308 came 38 min late · decided (lead; record) · 2026-11-23 · amends D-090
- **Power** (`pmset -g log`, its local times read in UTC):
  - the supervisor's keep-awake ended at 05:00:00 UTC (D-090), and the Mac went on battery at 05:25:29, at 100 % ("Using Batt");
  - **clamshell sleep** at 07:12:46 (64 %), with dark wakes at 07:30:03 (3 s) and 07:38:53 (9 s), until the lid opened at 07:49:03 (63 %);
  - **clamshell sleep** at 08:49:12 (49 %), with dark wakes at 09:04:15 (2 s), 09:19:19 (15 s), 09:21:01 (11 s) and 09:21:19 (6 s), until the lid opened at 09:36:07;
  - the charger was attached at 09:36:50 UTC, at 49 % (the log's attach chime and "Using AC"). The lead read 54 %, charging, at 09:42:59.
- **Capture** (`capture_log/2026-11-23.jsonl`; between pass ends, the data report's basis):
  - 07:08:00.841 → 07:49:03.491: 2,462.7 s between passes (41.0 min), 2,162.7 s lost by the gate rule's counting. The pass between them began in the 07:30:03 dark wake and ended only after the wake (1,139.81 s);
  - 08:47:22.893 → 09:19:29.766: 1,926.9 s (1,626.9 s lost), then 09:19:34.318 → 09:36:27.231: 1,012.9 s (712.9 s lost). Together that is 49.1 min without the 5-minute cadence; the 09:19 passes ran in dark wakes.
  
  The gate closed on 22 Nov (D-089), so no clock moves. The frames are lost, Rainier's for good. A pass has run every 5 min since 09:36, and the 09:36:08 pass stored all 8 cameras.
- **Errors on 23 Nov to 10:00 UTC:** 12, each followed by a 300 s back-off. Around the sleeps, three connection resets (Big Bend at 07:38:53, Grand Canyon at 07:49:04, Grand Teton at 09:19:20) and two requests never sent because their send deadline had passed (Acadia's image at 07:49:03, the Smokies' JSON at 09:19:19). Earlier, on AC: three JSON GETs answered 504 and three read timeouts at 03:43–03:45, in one pass of 174.9 s, and a Yosemite image GET answered 404 at 00:29. No 429 or 503 came.
- **Forecast:** run 2026112308 fell due at 08:58:57 UTC, when its f02 reached S3, and was written at 09:36:50.814, 44 s after the lid opened: complete (hours 0–2, all 8 viewpoints) and 37.9 min late. By the data report's rule, which ends it at the fetch's finish (09:36:50.756), that is a forecast outage of 2,273.8 s, 1,973.8 s lost. Every other run of the day finished within 274 s of falling due.
  - The late fetch does not bias the lag model: each `lag` record's `lag_s` is the later of the GRIB2 and `.idx` S3 stamps minus the run start (`lag_events` in `workers/forecast/src/snow_globe_forecast/laglog.py`). Because it came late, the fetch also listed and logged hours f03–f17.
  - Selection counts the run as available only from its fetch's finish, 09:36:50.756 (`live-fetch-finished`, D-027), as design P would.
- **Battery while awake:** 100 → 64 % from 05:25:29 to 07:12:42 UTC (about 20 % an hour, while wave 1's last agents ran, to about 06:10), and 63 → 50 % from 07:49:03 to 08:49:08 (13 % an hour). The lead's reading of the top consumers at 09:37 UTC, not saved to a file: iCloud's `bird` (70 % CPU) and `fileproviderd` (24 %), Docker Desktop's VM (18 %) and VS Code (13 %). Moving the repo out of iCloud is owner action 10, and Docker Desktop can be quit (owner action 12).
- **The lesson, D-089's again:** the supervisor's and the loop's `caffeinate -s` hold on AC only. On AC a closed lid dark-wakes and the loops run (from 18:55:11 UTC on 21 Nov, "Entering DarkWake state due to 'Clamshell Sleep' … Using AC"); on battery each closed lid slept the Mac at once. A closed lid on battery sleeps it whatever is held (D-089, D-090), so even the keep-awake, which ended at 05:00 UTC, would not have kept it running; only AC power does.
- **Spend:** $0. No paid call was made.

---

## Spend ledger

| Date | Vendor | What | Amount | Running total |
|---|---|---|---|---|
| 2026-11-18/19 | — | M0 research and spot checks used only free, keyless public endpoints | $0.00 | $0.00 |
| 2026-11-19 | — | M0.5 day 1: feed scouting, archive API discovery, the stopgap Rainier capture and local builds, using only free, keyless public endpoints. Deepgram and fal are approved (D-012) but have no accounts or keys, so no calls were made. | $0.00 | $0.00 |
| 2026-11-20 | — | M0.5 day 2: the capture loop, the archive backfill and the forecast stand-in, on nps.gov's and NOAA's free, keyless endpoints (the forecast package's S3 listings too), and local builds, tests and simulations. Nothing deployed to AWS; Deepgram and fal still have no accounts or keys, so no paid call was made. | $0.00 | $0.00 |
| 2026-11-21 | — | M0.5 day 3: the capture loop, the archive backfill and the forecast stand-in on nps.gov's and NOAA's free, keyless endpoints; the rename's commit and push, with CI's runs on GitHub; local builds, tests and simulations. The owner created an AWS account and applied for Activate credits (D-081), but nothing is deployed or runs on it. Deepgram and fal still have no accounts or keys, so no paid call was made. | $0.00 | $0.00 |
| 2026-11-22/23 | — | M0.5 day 4: the capture loop, the archive backfill and the forecast stand-in on nps.gov's and NOAA's free, keyless endpoints; the gate's close and the rename's live switch, both on this laptop (D-089, D-090); uv's package cache refilled from PyPI, a free public index. Nothing is deployed or runs on the AWS account. Deepgram and fal still have no accounts or keys, so no paid call was made. | $0.00 | $0.00 |
| 2026-11-23 | — | M1 wave 1 (D-092): five work packages built, reviewed, fixed and verified on this laptop, with only free, public, keyless sources: the npm registry for the overlay's lock file, the USGS National Map for the Rainier plate's DEM and landmarks, and GitHub for the lead's check of the Hillaire licence; then wave 1's push to GitHub, which starts a CI run there. The capture loop, the archive backfill and the forecast stand-in ran on nps.gov's and NOAA's free, keyless endpoints. Nothing is deployed or runs on the AWS account. Deepgram and fal still have no accounts or keys, so no paid call was made. | $0.00 | $0.00 |
