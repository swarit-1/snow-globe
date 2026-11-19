# PLAN.md: milestones, dates, cut order and evidence

Companion to [PRD.md](PRD.md) v0.4 (§16 milestones, §19 cut list). Per-ticket status and measured numbers are in [PRD_PROGRESS.md](PRD_PROGRESS.md). Decisions and spend are in [DECISIONS.md](DECISIONS.md).

Each milestone ends at a gate and stops for review. **A gate passes when its evidence exists: the measured value and, for a miss, the bottleneck.** Compiling code is not evidence. A missed target triggers the cut-order rule. It never triggers a stall or a retune.

**Where things stand on Thu 19 Nov 2026 (day G):**
- **M0 is done.** The owner accepted every PRD §1 default today (DECISIONS.md D-012).
- **M0.5 is in progress** (day 1 of 4). Being built today, with no evidence yet:
  - the repo scaffold and the CI workflow;
  - capture and the forecast worker;
  - the infra code;
  - renderer v−1 and eval scoring;
  - the Rust core.
- **A stopgap Rainier capture** has run on this laptop since 06:30 UTC, only while the laptop is awake. D-021 lists where it falls short of CLAUDE.md.
- **Evaluation design** (D-019): Rainier is design P, decided on evidence. The ARD cameras are design H, pending the archive check.
- **Waiting on the owner:**
  - AWS account, credentials and alarm email;
  - sending the NPS emails;
  - permission to push;
  - Deepgram and fal keys;
  - by 14 Dec, which renderer P1 scores (D-019).

  The full list is in PRD_PROGRESS.md.

---

## Calendar

Go-ahead day is **G = Thu 19 Nov 2026**. The owner gave no hours per week, so ~25 focused hours a week is assumed (PRD §1 row 12). The offsets come from PRD §16.

| Milestone | Offsets | Dates | Length | 25 % slack |
|---|---|---|---|---|
| M0 Scout | before G | done 19 Nov | — | — |
| M0.5 Start the clock | G → G+4 | Thu 19 Nov → Mon 23 Nov | 4 days | 1 day |
| M1 Voice to first pixel | G+4 → G+18 | Mon 23 Nov → Mon 7 Dec | 14 days | 3½ days |
| M1.5 Walking skeleton | by G+21 | by Thu 10 Dec | 3 days | ¾ day |
| M2 Relit frame | G+21 → G+38 | Thu 10 Dec → Sun 27 Dec | 17 days | 4¼ days |
| M3 Photographic frame | G+38 → G+48 | Sun 27 Dec → Wed 6 Jan 2027 | 10 days | 2½ days |
| M4 Proof | held-out period + ~5 days | about Fri 15 → Wed 20 Jan 2027 | ~5 days | ~1¼ days |
| M5 Ship | ~4 days | about Wed 20 → Sun 24 Jan 2027 | ~4 days | ~1 day |
| v1 growth | after M5 | from about 24 Jan 2027 | — | — |

- **Slack** is the 25 % rule in days. A milestone that overruns its length by more than that loses the next item on the cut list.
- **The freeze limits the slack before it.** The 14 Dec freeze doesn't move, and it needs extractor v0 and development truth from M1.5, which ends on Thu 10 Dec.
  - The 4 days from 10 to 14 Dec are planned for the freeze work itself, so any overrun in M0.5, M1 or M1.5 comes out of them.
  - Together, those three may overrun by 4 days at most, not the 5¼ their slack columns add up to (1 + 3½ + ¾).
  - **Rule:** the running overrun is tracked in PRD_PROGRESS.md.
    - From the first day of overrun, the freeze-critical work comes first: capture, the forecast worker, extractor v0, development truth, and the inputs to τ, S_mos, the analog weights and the MDE.
    - M1 work the freeze doesn't need (the overlay, speech and the harness) waits.
    - If the running overrun reaches 4 days, the next item on the cut list goes at once.
- M4 and M5 move later if outages extend the sealed window.

### Fixed dates (these don't move)

| Date | What | Effect on the plan |
|---|---|---|
| Mon 30 Nov 2026 (G+11) | Open-Meteo stops serving HRRR | None for the worker, which reads HRRR from NOAA's bucket (D-008). Open-Meteo stays only for pinned GFS beyond 48 h. |
| Fri 4 Dec 2026 (G+15) | Golden Gate permission cut-off | With written permission by then, Golden Gate can carry the demo. Without it, Rainier does and Golden Gate moves to v2. |
| Mon 14 Dec 2026 (G+25) | Pre-registration freeze (design P) | τ, S_mos, the analog weights and the MDE come from development data and are committed, with the hash in DECISIONS.md. This lands at the front of M2, and it caps the slack before it (see the calendar notes). |
| Tue 15 Dec 2026 → Thu 14 Jan 2027 (G+26 → G+56) | Sealed window (design P) | 31 days (≥ 28), extended by any outage. Capture must run unattended through the holidays, so AWS (D-014) has to be live before it opens. Until then, capture runs only while this laptop is awake. |

---

## Rules

- **The 25 % rule.** A milestone more than 25 % late loses the next item on the cut list (below).
- **MSV first.** Nothing outside the MSV starts before the MSV is published, except M0.5, whose capture and forecast jobs start first.
  - Note: PRD §16 itself schedules work outside the MSV before M5: the scrub view in M2, and Tier 2 and the LLM fallback in M3 (§9.6 calls Tier 2 "after the MSV core").
  - This plan follows the §16 table. If the owner wants the rule applied literally, those items move after M5.
- **Record everything.**
  - Every model, vendor and architecture choice goes into DECISIONS.md with its measurement.
  - Measured numbers go into PRD_PROGRESS.md.
- **Paid calls.** A paid call needs all three of these:
  - the owner's approval of that vendor for that use;
  - a key in the environment;
  - asking the owner before the first paid call to each vendor (PRD §17).

  **Approved now** (D-012, row 7): Deepgram for the bake-off, and fal for the ≤ $1 Tier 2 kill test only. Neither has an account or key yet.

  **Each needs its own approval first:** every other paid line in PRD §17 and §9.6:
  - the Tier 2 benchmark and false-accept suite;
  - Tier 2 in development and the evaluation sample;
  - the FLUX.2 klein illustration on fal;
  - the ungrounded-generation baseline;
  - Runware and Haiku;
  - AWS hosting, whose deployment waits for the owner anyway (D-014).
- **Measurement.**
  - Targets don't move after measuring.
  - Never tune a benchmark, special-case a test input, or mock a stage in a measured run.
- **Order of work inside a milestone** (PRD §16.1):
  1. Write the gate's test or benchmark first.
  2. Build until it produces its number.
  3. Record the number in PRD_PROGRESS.md and the decision in DECISIONS.md.
  4. Stop and show the evidence.
- **Code hygiene.** Small commits, tests beside the code, CI on every push, and secrets in environment variables only.
- **Owner-only actions.** These wait for the owner's word: `git push`, GitHub changes, `terraform apply`, any AWS call that creates something, and emails.
- **Polite collection.** Only one NPS fetcher runs at a time, under the CLAUDE.md rules. While the stopgap runs, it is that fetcher (D-021).

## Cut order (PRD §19)

If time runs short, cut from the top. Item 1 is already gone: unverified places moved to Later with D-012 (row 10). So the first slip costs item 2.

1. Unverified places (already moved to Later).
2. Rain and snow effects beyond extinction.
3. Moon and stars. The Moon's *position* stays tested.
4. The LLM parser fallback. Instead, show "didn't catch that" with the three closest places.
5. The live-beside-simulated view.
6. Shipping the cloud speech fallback. The bake-off itself stays.
7. Grand Canyon, and any viewpoint beyond the MSV pair.
8. Scrub-view polish (keep a bare slider).
9. Tier 2, if the kill test fails or the schedule demands it. Tier 1 is the floor.
10. Post-MSV physics, lowest measured gain first.

**Never cut:**
- the capture and forecast jobs;
- the sealed evaluation;
- the latency and replay harness;
- the labels;
- the data-flow inventory.

## Ongoing, in every milestone

- The capture and forecast jobs keep running. Their uptime and exclusions are logged.
- The data-flow inventory (PRD §15.2) and the licence ledger (§15.4) are kept current as code lands.
- Every charge goes into the DECISIONS.md spend ledger.
- PRD_PROGRESS.md is updated at every gate.

---

## M0: Scout · done (19 Nov)

- [x] Inspected the machine, this repo, move-predictor and Oculus. Measured oculus-core's ephemeris against JPL Horizons: worst case Sun 0.19′, Moon 0.13′ (D-002).
- [x] Five sourced research notes in [docs/research/](docs/research/). NPS feeds, EXIF and Open-Meteo's HRRR cut-off spot-checked.
- [x] Council: four judges, two rounds ([docs/council/](docs/council/)). PRD v0.3 (`d0ba83d`).
- [x] Cross-check of the Lookout draft against v0.3 ([docs/review/prd-crosscheck.md](docs/review/prd-crosscheck.md)). PRD v0.4 (`ab96af0`).
- [x] **Gate:** the owner's calls in PRD §1. All 15 defaults were accepted on 19 Nov (D-012).

## M0.5: Start the clock · Thu 19 → Mon 23 Nov · in progress

"In progress" means code is being written and its evidence does not exist yet.

| Build (PRD §16) | Status, 19 Nov | Where |
|---|---|---|
| **Forecast worker.**<br>• HRRR byte-range GETs from `noaa-hrrr-bdp-pds` at each viewpoint's sample points (where view rays meet each layer height).<br>• One JSON per viewpoint per run: source, run start, availability (S3 `LastModified`), fetch time, grid cells, byte ranges and values.<br>• Live lag per forecast hour logged for the hindcast availability model.<br>• GRIB subsets deleted after extraction. | in progress | `workers/forecast/` |
| **Capture of every camera that passes the licence check: all 8 (D-020).**<br>• Poll the JSON or headers at most every 5 min, and download only on change.<br>• At most 4 per camera per rolling hour, enforced with persisted state.<br>• Quality gates (PRD §8.6); stale and move flags.<br>• Private storage at ≤ 1920 px.<br>• Measure Rainier's real update cadence.<br>• Takes Rainier over from the stopgap. | in progress | `workers/capture/`, `workers/common/` |
| **Stopgap Rainier capture** (D-021) | running since 06:30 UTC, only while the laptop is awake. Its cap isn't persisted, and it runs from a scratch folder (D-021). | `$LOOKOUT_DATA_DIR/frames/rainier.paradise-mountain/` |
| **NPS archive check:** depth, cadence and camera epochs per camera, leading to H or P per viewpoint | in progress.<br>• The API and depth are found (D-018).<br>• Rainier is design P, decided: its camera has no archive (D-019).<br>• The ARD cameras are design H if this check finds no camera change across the HRRRv4 years.<br>• Guard: metadata for any date, but archived frames only from before 1 Jun 2026, because summer 2026 is held out (D-019). | `docs/research/nps-archive-check.md` (being written) |
| **Repo scaffold, CI, trace skeleton, PRD_PROGRESS.md.** The scaffold covers the uv, pnpm and Cargo projects per CLAUDE.md. | • Scaffold and trace skeleton: in progress.<br>• PRD_PROGRESS.md: started.<br>• CI: workflow written (`.github/workflows/ci.yml`). Its first run on GitHub needs a push, which needs the owner. | repo root, `app/core/`, `.github/workflows/` |
| **Infra code:** Terraform for the serverless stack (D-014), and the Lambda container image | in progress, code only. Its checks run offline (`infra/test.sh`). Nothing is planned or applied: there are no AWS credentials, and `apply` waits for the owner. | `infra/terraform/`, `infra/lambda/` |
| **One backtest pair end to end:** an archived frame plus an archived HRRR run, then a crude headless render (renderer v−1), then a crude extractor score, labelled "v−1" | in progress | `renderer/`, `eval/` |
| **Email drafts.**<br>• NPS Air Resources Division and Mount Rainier NP: public domain, collection, archive and publication.<br>• Lawrence Hall of Science and ALERTCalifornia: Golden Gate. | done (drafts committed in `e4722a6`). Sending them is the owner's step. | [docs/outreach/](docs/outreach/) |
| **Early M1 logic:** `lookout-core` with the parser, time rules, an ephemeris wrapper over oculus-core `06809e6`, solar events, trace and lifecycle | in progress | `app/core/` |

**Also done today:**
- The data directory now lives outside iCloud (D-017).
- All 8 NPS feeds were scouted and found live, and the viewpoint IDs were fixed (D-020).
- PLAN.md, DECISIONS.md and PRD_PROGRESS.md were brought to v0.4.

**Optional, not scheduled:** a council round on the parts taken from the Lookout draft, adding a forecast-verification judge (PRD §18.2, Appendix B).

**Gate evidence:**
- [ ] ≥ 72 h of worker and capture logs: runs and frames per viewpoint, plus exclusions. So far there is only the Rainier stopgap log, from 06:30 UTC on 19 Nov, with one outage (06:52–07:09 UTC, laptop asleep).
- [ ] The archive report, with H or P chosen per viewpoint in DECISIONS.md (D-019):
  - Rainier: design P, decided.
  - Point Reyes: design P on the sealed window, plus design H for K2.
  - The other six ARD cameras: design H.
  - Both H parts stay open until the epoch check reports.
- [ ] The backtest pair rendered and scored, labelled "v−1".
- [ ] Emails sent (owner).

**Risks to this gate:**
- **Gaps in the 72 h logs.** Until AWS is live, capture and the forecast worker run only while this laptop is awake. Gaps are reported as outages. Rainier gaps can never be backfilled, because that camera has no archive.
- **The epoch check.** A camera it can't clear by 23 Nov has no design H yet. D-019 records that camera as open, and the gate reports the miss with its bottleneck.
- **One NPS fetcher at a time.** While the stopgap runs, the archive check can't fetch from nps.gov in a separate process (CLAUDE.md). The lead settles how they share.

## M1: Voice to first pixel · Mon 23 Nov → Mon 7 Dec · not started (core logic started early)

**1a. Platform spike** (one day, throwaway branch). Stop and report if any check fails:
- hotkey press and release, including when the modifier is released first;
- a non-activating panel over another app that keeps focus;
- click-to-key focus, with focus returning to the previous text field;
- the panel revealed on key-down, with wake-from-suspension timed;
- `cpal` capture opened on key-down;
- the Swift static library (C ABI) returning `DictationTranscriber` partials;
- network silence during `DictationTranscriber`, checked with a network monitor;
- a ScreenCaptureKit window-only capture with an in-surface pixel tag;
- `CGEventPost` hotkey injection;
- host-clock agreement between ScreenCaptureKit timestamps and `Instant`;
- whether Screen Recording re-consent prompts appear.

**1b. Core.**
- **`lookout-core`:** query lifecycle with `query_gen` and cancellation, and tracing with no transcript text. Started 19 Nov.
- **`SpeechEngine` trait:**
  - A1 `DictationTranscriber`, the default (D-015).
  - D, Deepgram Nova-3, for the bake-off only, once the owner's account and key exist. `mip_opt_out` is set in one tested function, and the token is minted in parallel with the mic, or there is no cloud path.
- **Parser fast path (§7.3) and time rules (§7.4).** Started 19 Nov.
  - ≥ 150 table-driven cases with an injected clock, plus property tests.
  - The DST hours: 1 Nov 2026 repeated, 14 Mar 2027 skipped.
  - Cross-zone cases: Chicago to Los Angeles and Phoenix.
- **Overlay (§5):**
  - states, caption, chips and the destination chip;
  - the on-device-only setting;
  - Esc while visible, and the click-to-key focus model;
  - the webview paint gate.
- **Tier 0:**
  - library index, Sun-position match, and background `decode()`.
  - Skip painting Tier 0 when Tier 1 is ready within ~100 ms, but still run and measure it (D-012 row 11).
  - Rainier's library is our own captures only (D-019).
- **Stale-paint stress test:** ≥ 10,000 randomised cycles with injected delays.

**1c. Harness** (§10.4, §10.6).
- A recording tool through the live path, with a REC indicator that switches itself off.
- ~130 scripted utterances (30 dev, 100 test) plus ≥ 30 real-use ones, recorded by the owner.
- Sample substitution inside the live `cpal` callback, and a separate cache namespace per pass.
- Five states: idle (the headline), warm, cold, after wake and after launch.
- Overnight idle runs under `caffeinate -i` from a frozen build:
  - ≥ 100 idle trials for the shipped engine and ≥ 60 for the comparison engine;
  - spread over ≥ 3 days, with engines interleaved.
- A cluster-bootstrap report.

**1d. README skeleton,** with the headline sentence and blanks.

**From the owner in M1:**
- Permissions: Microphone and Speech Recognition; Screen Recording and Input Monitoring for the harness only; Apple Intelligence on.
- The replay recordings, and several nights of idle trials.
- A Deepgram account and key, for arm D. The owner is asked again before its first paid call.

**Gate evidence:**
- [ ] Spike notes and numbers in DECISIONS.md.
- [ ] **Replay report**, idle as the headline:
  - p50 and p95 per stage;
  - Tier 0 and the first partial;
  - WER and place accuracy;
  - the wrong-paint rate (Wilson bound) and partial lag;
  - the share of utterances still in speech at key-up.
- [ ] ≥ 150 parser cases green, counted by category.
- [ ] The STT decision in DECISIONS.md, with the Parakeet gate evaluated idle.
- [ ] 0 stale paints in ≥ 10,000 cycles.
- [ ] A screen recording of the overlay over another app that keeps focus.

## M1.5: Walking skeleton · by Thu 10 Dec · not started

- **Renderer v0, headless:**
  - the sky (Hillaire 2020, D-013);
  - cloud slabs intersected along view rays;
  - a ceiling slab and flat fog;
  - camera model v0.
- **Extractor v0:** per-landmark edge ratio against an illumination-conditioned clear-day reference.
- **P1 on development data only,** against persistence, climatology and S_rule.
- **Dependency:** a first Rainier plate (base frame, pose, summit band), pulled forward from M2.
  - Its base frame has to come from our own captures, because the summit camera has no archive.
  - PRD §8.5 asks for a bright-overcast midday frame with every landmark visible, which is hard at Rainier. If none is captured in time, M1.5 slips.

**Gate evidence:**
- [ ] A dev-only P1 table, with nothing held out touched.
- [ ] Cloud Monte Carlo test output: ≥ 1,000 seeds × cover from 0.1 to 0.9, within 5 points of the view-ray target fraction, by elevation band.

## M2: Relit frame · Thu 10 → Sun 27 Dec · not started

**First, by Mon 14 Dec (design P): freeze the pre-registration.**
- **Fitted on development data:**
  - τ and the partial band;
  - S_mos;
  - the analog distance, weights and "no close analog" stratum;
  - the block length, from development autocorrelation;
  - the MDE, computed and published.
- **Named:** K2's Point Reyes landmark, with its distance and height and the forecast fields mapped to it.
- **Commit it** and record the hash in DECISIONS.md (D-011). The commit also fixes the renderer code as of 14 Dec.
- **Which renderer P1 scores is the owner's call, needed by 14 Dec** (D-019). The PRD freezes the fitted values but doesn't say which renderer is scored.
  - **(a)** The renderer at the freeze commit, with later versions reported as exploratory. In practice that is renderer v0 from M1.5 plus whatever lands by 14 Dec, so the rest of M2 would not count toward P1 or R1.
  - **(b)** The renderer at M4, improved through M2 and M3 on development data only (feature admission, PRD §13.9). Frames dated after 14 Dec stay out of Tier 0, the live view and labelling until M4 scores.
  - The work up to 14 Dec is the same under both.
- **This date binds:** P1 is at Rainier, which can only use design P (D-019).

**Then:**
- **Ephemeris validation:**
  - refracted apparent altitude, and the transform into each camera frame;
  - Horizons vectors per viewpoint, hourly for 12 months;
  - 1,000 random samples against Skyfield;
  - sunrise and sunset against NOAA to ±1 min;
  - targets: Sun ≤ 0.05°, Moon ≤ 0.3°.
- **Plates for Rainier and Point Reyes** (§8.5):
  - dehazed overcast base frame and night frame;
  - pose from ≥ 3 landmarks (reprojection ≤ 1°), plus the sunset-shadow check;
  - DEM ray-cast depth plus a scale-aligned near-field model (no NC weights);
  - world height and normals;
  - DEM horizon maps;
  - sky, lights and landmark-band masks.
- **Haze and camera response:**
  - haze profiles: clear-air extinction per viewpoint and season, fitted on clear development frames;
  - the camera response (Kim & Pollefeys), fitted on development frames across all weather.
- **Tier 1 in the overlay and the scrub view** (a bare slider).
- **Golden images in CI** on Chromium/SwiftShader: twelve conditions per viewpoint, SSIM ≥ 0.98. The WKWebView tolerance is documented.
- **Datums:** the datum of every height field is pinned in DECISIONS.md.
- **Feature admission:** one at a time, on dev-check evidence only. A feature needs a ≥ 0.02 dev-check P1 PSS gain or an obscuration-height gain.
- **API on Lambda:** manifests, forecast files by signed URL, the live frame and predictions.
  - Graded asks start once it is deployed.
  - This needs the AWS deployment (D-014).
- **NPS confirmation.** PRD §15.1 looks for it by the end of M2. Without it, the public repo, README and demo carry code, numbers and charts only, and the owner signs off that list.

**Gate evidence:**
- [ ] The pre-registration hash: by 14 Dec under design P.
- [ ] The ephemeris report.
- [ ] The pose report.
- [ ] Tier 1 p95 idle from the harness, with pre-render contention measured.
- [ ] Goldens green in CI.
- [ ] Dev-split evidence for each admitted feature.

## M3: Photographic frame · Sun 27 Dec → Wed 6 Jan · not started

M3 runs inside the sealed window. Its work, Tier 2 and the LLM fallback, doesn't change the Tier 1 renders that P1 scores (PRD §13.2).

1. **Kill test** (≤ $1 on fal). It needs the owner's fal account and key, and the owner is asked before the first call.
   - Run 20 Tier 1 frames through Z-Image-Turbo img2img at strength 0.15–0.35.
   - Record the check pass rate, plus a blind preference judged by someone other than the owner.
   - Stop here if it fails.
2. **If it passes.** fal spend beyond the kill test needs the owner's approval first, within the PRD §17 caps. Then:
   - a GPU composite (sky locked, T(x)-weighted detail, ≤ 15 ms) with phase-correlation registration;
   - checks with thresholds calibrated on ~100 human-judged pairs, then frozen;
   - a false-accept suite: ≥ 200 weather-altered refinements through the full pipeline;
   - at most one request in flight, and a 50-call cancel microbenchmark;
   - the proxy gate: ≤ 100 ms added p95 when idle, with Lambda and a Cloudflare Worker both measured;
   - a spend guard and a server-side daily cap;
   - Runware as the alternate host (needs its own approval);
   - the FLUX.2 klein illustration (~20 calls on fal).
3. **LLM parser fallback:** Foundation Models, on device (D-005, D-012 row 10).

**Gate evidence:**
- [ ] The kill-test result.
- [ ] The share of queries with Tier 2 delivered within 3 s, plus its p95.
- [ ] The false-accept bound.
- [ ] The preference result.
- [ ] The spend ledger.
- [ ] The Tier 2 decision in DECISIONS.md.

## M4: Proof · about Fri 15 → Wed 20 Jan 2027 · not started

- **Extractor validation** (bars from PRD §13.8):
  - labels: ~150 real frames per metric and ~50 renders, with 50 re-labelled a week apart (a second labeller if available);
  - bars: landmark F1 ≥ 0.85; cloud-fraction MAE ≤ 0.10; fog F1 only with ≥ 30 positives;
  - held-out frames stay out of the labelling store;
  - the owner labels during M2–M4.
- **Score once:** P1, R1–R3, K1–K2 (Holm-corrected) and the descriptive secondaries.
  - One secondary, the ungrounded-generation baseline (~50 images, ≤ $1), needs its own vendor approval first (D-012 row 7).
- **Uptime and exclusions,** plus an iced-lens sensitivity check.
- **Dashboard:** private until NPS confirms. `make score` rebuilds it from frozen inputs.

**Gate evidence:**
- [ ] P1 with its CI, n and base rate, whatever it shows.
- [ ] R1–R3 and K1–K2.
- [ ] A plain-language fog result.
- [ ] Uptime and exclusions.

## M5: Ship · about Wed 20 → Sun 24 Jan 2027 · not started

M5 ships on schedule whatever P1 shows.

- **README** in move-predictor's shape. The headline is filled only from dashboard exports.
- **60-second demo:**
  - 0–10 s: "Rainier tomorrow at four", asked over an editor, with a trace bar;
  - 10–25 s: scrubbing the evening;
  - 25–45 s: yesterday's 24 h forecast vs what the camera saw vs persistence;
  - 45–60 s: skill against lead time, with confidence intervals.
- **`THIRD_PARTY_NOTICES`,** generated from the licence ledger.
- **Only content cleared for publication:** NPS confirmation received, and people and vehicles blurred.

**Gate evidence:**
- [ ] Every README number traces to a file and a command.
- [ ] `make score` works from a fresh clone.

## v1 growth · after M5 · not started

Capture for all 8 cameras already runs from M0.5.

- **Viewpoints 3–8,** each with plate, pose, labels and goldens, and ≥ 14 days of live data:
  - Grand Canyon first;
  - then the v1 candidates Yosemite, Acadia and Grand Teton;
  - then the Smokies and Big Bend.
- **The rest of v1** (PRD §4.2): the live-beside-simulated view. PRD §16 gives it no milestone, so it lands here unless the owner moves it.

**Gate evidence:**
- [ ] Gate evidence for each viewpoint: the same gates as the MSV pair.
