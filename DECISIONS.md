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

---

## Spend ledger

| Date | Vendor | What | Amount | Running total |
|---|---|---|---|---|
| 2026-11-18/19 | — | M0 research and spot checks used only free, keyless public endpoints | $0.00 | $0.00 |
| 2026-11-19 | — | M0.5 day 1: feed scouting, archive API discovery, the stopgap Rainier capture and local builds, using only free, keyless public endpoints. Deepgram and fal are approved (D-012) but have no accounts or keys, so no calls were made. | $0.00 | $0.00 |
