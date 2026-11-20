# PRD_PROGRESS.md: tickets, owner actions and measured numbers

This file tracks progress per ticket against [PRD.md](PRD.md) v0.4.1 and [PLAN.md](PLAN.md), with every measured number (PRD §16.1). Decisions and the spend ledger are in [DECISIONS.md](DECISIONS.md).

**Updated:** Fri 20 Nov 2026, 02:15 UTC (G+1; still the evening of 19 Nov in US time). Snapshot rows say when they were read. M0 is done, and M0.5 is in progress (day 2 of 4).

**Running overrun before the 14 Dec freeze:** 0 days, of the 4 allowed across M0.5, M1 and M1.5 (PLAN.md, calendar notes).

**Status values:**
- **done:** its evidence exists.
- **in progress:** being built, but the evidence doesn't exist yet or isn't complete. Written code with no evidence counts here, not as done.
- **blocked on owner:** waiting on one of the owner actions below.
- **not started.**

**Committed so far:** `0bb42ab` (common, manifests, Makefile, CI) to `3ac98b3`. A second round of fixes from 19–20 Nov is in the working tree, uncommitted, for the lead: common, the viewpoint manifests and schema, capture 0.2.0, eval's held-out guard and pair code, `docs/checks/`, `docs/evidence/`, `spikes/`, the infra smoke test, the forecast survey log, the integration changes to the Makefile, CI and `.gitignore`, and these planning docs with `THIRD_PARTY_NOTICES`.

---

## Owner actions

Most urgent first.

1. **Keep the Mac on AC power.**
   - Capture and the forecast job run on this laptop. The capture loop runs under `caffeinate -s`, which keeps the whole Mac awake only on AC power, and the forecast job relies on it (D-030).
   - On 19 Nov it ran on battery from 06:13 to 22:48 UTC, slept 4 times and lost about 8.2 h of all-camera capture. The battery read 3 % when AC power came on at 22:48 UTC. Rainier's gaps can never be backfilled.
2. **AWS.** Until AWS is live, capture depends on this laptop, and it must be live before the sealed window opens on Tue 15 Dec.
   - Use an account that runs only Lookout (the budget counts the whole account), on the Paid plan.
   - Provide credentials through the environment, and set `TF_VAR_alarm_email`.
   - Check the Lambda concurrency quota is at least 102 (`aws lambda get-account-settings`).
   - **Before the first deploy, decide D-035:** whether PRD §17's $3 is monthly, and whether the hard stop stays on through the sealed window. The recommendation is to switch it off for 15 Dec – 14 Jan and keep the email alerts.
   - Then run `infra/deploy.sh --dry-run` and `infra/deploy.sh`, and confirm the SNS email. Nothing is applied without your word (D-014).
3. **NPS confirmation emails.**
   - The drafts are in [docs/outreach/nps-confirmation.md](docs/outreach/nps-confirmation.md), for the NPS Air Resources Division and Mount Rainier NP. The Rainier one now asks where the "mountain" camera is mounted, how often it updates and whether past frames are kept.
   - Golden Gate, if you want it: [docs/outreach/golden-gate.md](docs/outreach/golden-gate.md), by **Fri 4 Dec**.
4. **Permission to push.**
   - **First, or with it, answer D-041.** The synthetic renderer goldens are already committed (`b5bc726`). If you decline them, the lead removes them from git history before the push; deleting them in a later commit would still publish them.
   - The clone's `origin` is `swarit-1/snow-globe`, and nothing has been pushed.
   - The CI workflow (`.github/workflows/ci.yml`) runs only after the first push. The oculus-core pin is public now (D-025), so CI can fetch it.
5. **Deepgram and fal: accounts and keys, when you're ready.**
   - Keys go in the server's environment, never in the app.
   - Approved (D-012): Deepgram for the bake-off, and fal for the ≤ $1 kill test only.
   - Even with a key, you're asked before the first paid call to each vendor (PRD §17). Any other fal spend needs your approval first (PLAN.md, Rules).
   - Deepgram is needed for the M1 bake-off (from 23 Nov), and fal for the M3 kill test (from 27 Dec).
6. **Run the M1 1a spike checks** (from Mon 23 Nov), at the keyboard, following `spikes/m1-platform/README.md`.
   - Checks 1–4 need no permission. Check 5 needs Microphone; checks 8 and 10 need Screen Recording for the terminal; check 9 needs Accessibility for the injecting terminal (D-033); check 11 runs over several days.
   - Checks 6 and 7 wait for the Swift speech library, which is not written yet.
   - Report every run, failed and inconclusive ones too. On a `FAIL`, stop and send the run file.
7. **Permissions in M1:** Microphone and Speech Recognition, when the app first asks; Screen Recording and Input Monitoring for the harness only; Accessibility for the injection check (open, D-033); Apple Intelligence on.
8. **Replay recordings in M1:** ~20 minutes of scripted utterances, plus ≥ 30 real-use ones.
9. **Confirm or change, by Mon 14 Dec:**
   - which renderer P1 scores, (a) the freeze commit or (b) the M4 renderer under a no-peek rule (D-019);
   - persistence (b) beyond a 24 h lead (D-036);
   - eval's readings of the held-out split (D-038);
   - the parser's rule readings: "tonight", dates and the same day, "next friday", "tomorrow" = 12:00, and a lone generic word as a miss (D-039).

   Two calls come earlier: D-035 before the first deploy (owner action 2), and D-041 before the first push (owner action 4).
10. **iCloud.** Consider moving the repo out of `~/Documents`, which would end the venv and build-output workarounds (D-031).
11. **Disk space.** 28 GiB was free of 460 GiB at 00:50 UTC on 20 Nov (94 % used). The running loop adds about 450–470 MB a day until the new storage policy runs (D-028).

**Later** (PRD §1 rows 14–15):
- Several nights of unattended idle trials (M1).
- Labels: ~150 frames per metric plus 50 re-labels (M2–M4).
- Someone other than you to judge the Tier 2 preference test (M3).
- A project-specific contact address for the README's takedown line. It could also go in the User-Agent (D-016).

---

## Tickets

### M0: Scout · done

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M0-1 Sourced research notes: speech-to-text, refinement, cameras, forecast | done | [docs/research/](docs/research/) (`da60f0a`) | — |
| M0-2 Council review: four judges, two rounds | done | [docs/council/](docs/council/) (`96e3627`) | — |
| M0-3 Cross-check of the Lookout draft against v0.3 | done | [docs/review/prd-crosscheck.md](docs/review/prd-crosscheck.md) (`66a837f`) | 24 points checked in Lookout, 17 in v0.3 |
| M0-4 PRD v0.4 | done | [PRD.md](PRD.md) (`ab96af0`); factual corrections in v0.4.1 | — |
| M0-5 oculus-core ephemeris against JPL Horizons | done | DECISIONS.md D-002, D-025 | Sun 0.19′, Moon 0.13′, worst case |
| M0-6 **Gate:** the owner's PRD §1 calls | done | DECISIONS.md D-012 | 15 of 15 defaults accepted |

### M0.5: Start the clock (Thu 19 → Mon 23 Nov) · in progress

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M0.5-01 Bring PLAN.md and DECISIONS.md to v0.4 (D-012 to D-021); start this file | done | [PLAN.md](PLAN.md), [DECISIONS.md](DECISIONS.md), this file (`a1fca21`); D-022 to D-045 since | — |
| M0.5-02 Data directory outside iCloud | done | DECISIONS.md D-017, D-031 | see "Machine and environment" |
| M0.5-03 Scout the 8 NPS feeds; fix the viewpoint IDs | done | DECISIONS.md D-020; `~/lookout-data/samples/nps-json/` | 8 of 8 feeds live |
| M0.5-04 NPS archive API and depth | done | DECISIONS.md D-018 | start dates below |
| M0.5-05 Email drafts: NPS, Golden Gate | done | [docs/outreach/](docs/outreach/) (`e4722a6`); the Rainier questions added on 20 Nov | — |
| M0.5-06 Stopgap Rainier capture | done: ran 06:30–12:13 UTC on 19 Nov, then retired | DECISIONS.md D-021, D-023 | 24 frames; 4 outages (see "Outages") |
| M0.5-07 Repo scaffold: uv, pnpm and Cargo projects, viewpoint schema and manifests, per CLAUDE.md | done | `0bb42ab`; [docs/checks/](docs/checks/) (uncommitted) | `make test`: 7 of 8 suites pass, forecast fails (see "Test suites") |
| M0.5-08 Capture worker, 8 cameras.<br>• Polite: fetch on change, ≤ 4 per hour with persisted state.<br>• Quality gates (PRD §8.6), stored at ≤ 1920 px.<br>• Rainier cadence measured. | in progress: 0.1.0 runs for all 8 cameras since 12:24:23 UTC; 0.2.0 (storage policy, backfill inside the loop, cadence tool) awaits commit and re-snapshot | `workers/capture/` (`4fae4c1`); DECISIONS.md D-023, D-028, D-029; [docs/research/nps-archive-check.md](docs/research/nps-archive-check.md) | Rainier updates about once a minute; 262 MB a day under the new policy |
| M0.5-09 Forecast worker.<br>• HRRR byte ranges at the view-ray sample points.<br>• One JSON per viewpoint per run.<br>• Lag per forecast hour. | in progress: built and running as the local stand-in; survey logging not wired up; 1 test fails | `workers/forecast/` (`09e2ec8`); DECISIONS.md D-026, D-027, D-030 | 24 of 24 hourly runs of 19 Nov written; 454 live lags |
| M0.5-10 Trace skeleton | done | `app/core/src/trace.rs` (`e7b87e8`) | no field can hold transcript text (a test walks the JSON) |
| M0.5-11 Infra code: Terraform (state backend, storage, CDN, budget alarm, Lambda + EventBridge Scheduler) and the Lambda image | in progress, code only: CloudFront and the API are TODO(M1); a first local image build and offline handler run on 20 Nov, report pending | `infra/terraform/`, `infra/lambda/` (`0727ac8`); DECISIONS.md D-034 | `make test-infra` 82 passed; estimate in D-035 |
| M0.5-12 AWS deployment | blocked on owner (owner action 2) | DECISIONS.md D-014, D-034, D-035 | — |
| M0.5-13 CI (GitHub Actions, CI only) | in progress: the workflow is written and lints clean. Its first GitHub run needs a push (owner action 4). | `.github/workflows/ci.yml` (`0bb42ab`) | actionlint 1.7.12: 0 findings |
| M0.5-14 One backtest pair: an archived frame and an archived HRRR run → renderer v−1 → a crude extractor score, labelled "v−1" | done; awaiting the lead's commit | [docs/evidence/m05-backtest-pair.md](docs/evidence/m05-backtest-pair.md) | Grand Canyon, 24 h lead: scored, correct negative; 2.6 s a pair |
| M0.5-15 NPS archive check: cadence, gaps and camera epochs per camera | done | [docs/research/nps-archive-check.md](docs/research/nps-archive-check.md) (`4fae4c1`, `2e3b12d`); DECISIONS.md D-022 | 217 requests; every ARD camera went 3:2 → 16:9 in 2023–25 |
| M0.5-16 H or P per viewpoint in DECISIONS.md | done | DECISIONS.md D-019, D-022, D-024, D-042 | — |
| M0.5-17 Emails sent | blocked on owner (owner action 3) | — | — |
| M0.5-18 **Gate:** ≥ 72 h of worker and capture logs | in progress: the clock runs from 12:24:23 UTC on 19 Nov for all 8 cameras (06:30 UTC for Rainier). Outages extend it, so after the 8.2 h lost on 19 Nov it closes at about 20:36 UTC on Sun 22 Nov at the earliest | `~/lookout-data/capture_log/`, `forecast_log/` | 8 outages so far: 4 of the stopgap, before the clock started, and 4 of the loop (see "Outages") |
| M0.5-19 A council round on the parts taken from Lookout, adding a forecast-verification judge | not started (optional) | PRD §18.2 | — |
| M0.5-20 Local stand-ins until AWS is live: the capture loop and the forecast tick | in progress: running from a frozen snapshot (`cfefa79`); the capture loop runs under `caffeinate -s`, and the forecast tick relies on it | DECISIONS.md D-030 | 47 loop passes to 00:41 UTC on 20 Nov; the forecast tick from 12:18 UTC |

### M1: Voice to first pixel (Mon 23 Nov → Mon 7 Dec) · not started, with some core logic started early

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M1-01 Platform spike: one day, 11 checks (PLAN.md 1a) | in progress (early): the spike app is built; none of the 11 checks has run (owner action 6) | `spikes/m1-platform/` (uncommitted); DECISIONS.md D-032, D-033 | 105 unit tests; see "M1 preparation" |
| M1-02 `lookout-core`: query lifecycle, `query_gen` cancellation, trace | in progress (early) | `app/core/` (`e7b87e8`) | key-mash: 40,000 cycles, 0 stale paints |
| M1-03 Parser fast path and time rules.<br>• ≥ 150 table-driven cases and property tests.<br>• DST and cross-zone cases. | in progress (early): some rule readings wait for the owner (D-039) | `app/core/tests/parser_table.rs`, `properties.rs` | 295 table cases; parse p95 124–126 µs |
| M1-04 Ephemeris wrapper and solar events over oculus-core `1b5410b` | in progress (early) | `app/core/src/solar.rs`; DECISIONS.md D-025 | sunrise and sunset within 3.9 s of NOAA; Moon within 0.355° |
| M1-05 Speech A1: `DictationTranscriber` through the Swift static library | not started: assigned early (D-032), but no code is in the repo | — | — |
| M1-06 Speech D: Deepgram Nova-3, bake-off only | blocked on owner (owner action 5) | DECISIONS.md D-015 | — |
| M1-07 Overlay with Tier 0: states, caption, chips, destination chip, focus model, paint gate | not started | — | — |
| M1-08 Tier 0: library index, Sun-position match, background decode | not started | — | — |
| M1-09 Replay and paint-capture harness | not started | — | — |
| M1-10 Replay recordings: ~130 scripted, plus ≥ 30 real-use | not started (owner, in M1) | — | — |
| M1-11 macOS permissions | not started (owner, in M1) | — | — |
| M1-12 Overnight idle trials: ≥ 100 for the shipped engine, ≥ 60 for the comparison engine | not started | — | — |
| M1-13 Stale-paint stress test, ≥ 10,000 cycles | in progress (early): the Rust model of the paint gate passes; the webview gate is M1 work | `app/core/tests/keymash.rs` | 40,000 cycles, 0 stale paints; 3 broken gates caught |
| M1-14 README skeleton with blanks | not started | — | — |
| M1-15 **Gate:**<br>• replay report;<br>• ≥ 150 parser cases;<br>• STT decision;<br>• 0 stale paints;<br>• a screen recording over another app. | not started | — | — |

### M1.5: Walking skeleton (by Thu 10 Dec) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M1.5-01 Renderer v0, headless: Hillaire sky, cloud slabs, ceiling slab, flat fog, camera v0 | in progress (early): renderer 0.2.0 has cloud slabs, a ceiling slab, fog and camera v0; the Hillaire sky is TODO(M1.5) | `renderer/` (`b5bc726`) | see "Renderer" |
| M1.5-02 First Rainier plate: base frame, pose, summit band. Needs a captured midday frame with the summit visible. | not started | — | — |
| M1.5-03 Extractor v0 | not started (extractor v−1 exists in eval) | `eval/src/lookout_eval/extractor.py` | — |
| M1.5-04 **Gate:** dev-only P1 table against persistence, climatology and S_rule | not started | — | — |
| M1.5-05 **Gate:** cloud Monte Carlo test | in progress (early): passes on renderer 0.2.0; re-run on the M1.5 renderer | `renderer/src/cpu/montecarlo*.ts`; `pnpm montecarlo` | within 1.54 points of the view-ray target at 1,000 seeds |

### M2: Relit frame (Thu 10 → Sun 27 Dec) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M2-01 **Pre-registration freeze, by Mon 14 Dec under design P.**<br>• τ, S_mos, analog weights, block length, MDE, K2 mapping.<br>• Commit hash.<br>• Which renderer P1 scores (owner action 9). | not started | DECISIONS.md D-011, D-019, D-038 | — |
| M2-02 Ephemeris validation: refraction, camera transform, Horizons per viewpoint, Skyfield ×1,000, NOAA ±1 min | not started | — | apparent position so far: Sun 0.19′, Moon 0.13′ (D-002); NPS's own Sun angles match our refracted altitude to 0.0026° |
| M2-03 Plates for Rainier and Point Reyes; pose report | not started | — | — |
| M2-04 Haze profiles and camera response | not started | — | — |
| M2-05 Tier 1 in the overlay and scrub view; Tier 1 p95 idle, with pre-render contention | not started | — | — |
| M2-06 Goldens in CI on SwiftShader; WKWebView tolerance | not started (three synthetic goldens match exactly on this Mac; D-041) | `renderer/goldens/` | — |
| M2-07 Datums pinned | in progress (early): the HRRR height fields are pinned (D-026); the DEM datum comes with the plates | DECISIONS.md D-026 | — |
| M2-08 Feature admission on dev-check evidence | not started | — | — |
| M2-09 API on Lambda; graded asks begin | not started; needs AWS (owner action 2) | — | — |

### M3: Photographic frame (Sun 27 Dec → Wed 6 Jan) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M3-01 Tier 2 kill test, ≤ $1 on fal | not started. It needs a fal key, and you're asked before the first call (owner action 5). | — | — |
| M3-02 Composite, checks, false-accept suite, cancel benchmark, proxy gate, spend guard | not started. fal spend beyond the kill test needs your approval first, within the PRD §17 caps. | — | — |
| M3-03 Blind preference test, judged by someone else | not started | — | — |
| M3-04 LLM parser fallback (Foundation Models) | not started | — | — |

### M4: Proof (about Fri 15 → Wed 20 Jan 2027) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M4-01 Labels: ~150 frames per metric, ~50 renders, 50 re-labels (owner) | not started | — | — |
| M4-02 Extractor validation against the PRD §13.8 bars | not started | — | — |
| M4-03 Score once: P1, R1–R3, K1–K2 | not started | — | — |
| M4-04 Private dashboard, uptime and exclusions | not started | — | — |

### M5: Ship (about Wed 20 → Sun 24 Jan 2027) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M5-01 README with the numbers, 60-second demo, `THIRD_PARTY_NOTICES`, and `make score` from a fresh clone | in progress (early): only a first `THIRD_PARTY_NOTICES`, written by hand from the lock files on 20 Nov | [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) | — |

### v1 growth (after M5) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| V1-01 Viewpoints 3–8, each with plate, pose, labels, goldens and ≥ 14 days of live data | not started. Capture for all 8 runs since 19 Nov. | — | — |
| V1-02 Live-beside-simulated view | not started | — | — |

---

## Measured so far

Every entry below was measured or checked on this machine (MacBook Air M4, macOS 26.5.1), or by NOAA's or NPS's own servers as stated. Times are UTC. The agents' reports and the evidence files hold the full tables.

### Outages

Outages extend the 72 h gate, as they extend the sealed window, and each one is listed as an exclusion (PLAN.md, M0.5 gate). All of them were laptop sleep on battery (`pmset -g log`); `caffeinate -s` holds the Mac awake only on AC power, and it was on battery from 06:13 to 22:48 on 19 Nov.

| Job | Outage (19 Nov) | Length | Missed |
|---|---|---|---|
| Rainier stopgap | 06:50:14 → 07:10:34 | 20.3 min between polls | ~3 polls |
| Rainier stopgap | 07:50:37 → 08:25:22 | 34.8 min | ~6 polls |
| Rainier stopgap | 08:50:24 → 09:41:57 | 51.5 min | ~9 polls |
| Rainier stopgap | 10:01:59 → 10:57:19 | 55.3 min | ~10 polls |
| Handover, by design | 12:12:23 → 12:24:40 (last stopgap record → first loop pass) | 12.3 min | the 11-minute guard window (D-023) |
| Capture loop, 8 cameras | 12:56:00 → 15:09:08 | 133.1 min between passes | ~24 passes |
| Capture loop | 15:09:08 → 18:02:52 | 173.7 min | ~32 passes |
| Capture loop | 18:02:52 → 19:35:08 | 92.3 min | ~16 passes |
| Capture loop | 19:58:15 → 21:52:09 | 113.9 min | ~20 passes |

- **How:** gaps between consecutive records in `capture_log/` (stopgap polls; loop `pass` records), matched to the sleep blocks in `pmset -g log`. Missed = round(gap ÷ the normal interval) − 1, with 5 min for the stopgap and the loop's median 320 s. The passes at 15:09 and 18:03 ran in brief dark wakes. A 7.2 min gap at 19:51–19:58 was one late pass, not an outage.
- **Totals:** stopgap ~28 polls (2.4 h); loop ~92 of ~139 expected passes (8.2 of 12.3 h). From 12:24:40 on 19 Nov to 00:41:15 on 20 Nov the loop made 47 passes, and each camera had a stored frame in 7 or 8 of the 13 UTC hours. Since the switch to AC power at 22:48 there has been no outage (checked to 01:44 on 20 Nov, 12 more passes).
- **The 72 h clock:** the loop's 8.2 h move its earliest close from 12:24 to about 20:36 UTC on Sun 22 Nov. A new outage moves it again.
- **Forecast tick:** it slept at the same times. Every hourly run of 19 Nov (00Z–23Z) was still written by 23:56: 00Z–11Z as catch-up at its first ticks (12:18–12:26), except 08Z and 10Z, which the forecast builder's smoke tests wrote at 09:54 and 12:01; runs 13Z, 15Z, 16Z and 19Z 1.5–2.4 h after their f00–f02 were on S3, and the rest within 0.8 h. Since 22:48, on AC power, each run has been written within 2 min of publication.
- **Not live-worker evidence:** the backtest pair's archived `run` event (run 2024011412) at line 475 of `forecast_log/2026-11-19.jsonl`.

### Capture

| What | Value | How and when |
|---|---|---|
| Rainier update cadence | **About once a minute.** At every poll its newest image was at most 125.9 s old: median age 20.6 s, p90 69.0 s (n=48; 40 of 48 at most 60 s). To 23:57: n=63, median 20.7 s, p90 70.3 s, max 125.9 s. The camera's own EXIF times sit within 5.1 s of a whole minute (median 2.1 s), and each upload's `Last-Modified` follows 10.3–26.3 s later (median 22.8 s; n=20 loop frames, sidecars read at 01:50 on 20 Nov). | `lookout-capture cadence` (capture 0.2.0): server answer time minus HEAD `Last-Modified` at each poll, 19 Nov to 22:37:29. Every consecutive pair of polls changed, so the 300 s intervals measure our poll spacing, not the camera. |
| ARD camera cadence | `imagedate` every 900 s (median, n=6–7 each; one 1,800 s gap at Yosemite); the image is 4.5–25 min old when polled. The JSON file is rewritten twice every 10 minutes (minutes 1–2 and 5 past each 10-minute mark, 209 of 209 values). | Same tool, capture log of 19 Nov |
| Image times | EXIF − JSON `imagedate` = 0.0 min at 7 of 7 ARD cameras. Image `Last-Modified` − EXIF = +6.50 to +6.88 min (ARD) and +0.171 min at Rainier. JSON `Last-Modified` − image `Last-Modified` = 8–25 s (Yosemite 8, Point Reyes 13, Grand Teton and the Smokies 18, Grand Canyon 22, Acadia and Big Bend 25). | The 8 sidecars of the smoke pass, 10:56 |
| Smoke pass, all 8 | 8 of 8 stored in 17.88 s: 16 nps.gov requests (7 JSON GETs, 1 HEAD, 8 image GETs), 15,742,510 image bytes downloaded, 5,175,244 stored | `lookout-capture once` at 10:56:15 |
| Frame sizes | ARD: 490,890–3,493,083 B at 3840×2160, stored as 203,877–868,375 B at 1920×1080 (q90), over all 131 ARD frames to 00:50 on 20 Nov; the 7 of the smoke pass at 10:56 alone gave 860,197–2,741,881 B, stored as 569,728–736,571 B. Rainier 1920×1080, kept as downloaded: 825 KB mean (n=32), 1,687 KB with the Sun up (n=6). | ARD: `original_bytes` and `stored_bytes` in the frame sidecars, read at 01:50 on 20 Nov. Rainier: the storage run at 22:38 (D-028). |
| Storage per day | 262.0 MB for 8 cameras under D-028 (Rainier 100.8, Point Reyes 55.4, the six hourly cameras 13.3–21.1 each), against 451.9 MB keeping every frame. 90 days: 23.6 GB against 40.7 GB. Re-run over all of 19 Nov: 269.9 against 468.9 MB a day. | `lookout-capture storage --day 2026-11-19` at 22:38 (105 frames) and 23:58; thumbnails made in memory; 96 downloads a camera a day |
| On disk now | 170 frames, 102 MB: Rainier 40 (24 from the stopgap), the others 17–19 each. 85 archive frames (1.1 MB). | `find` and `du`, 00:50 on 20 Nov |
| Rainier image GETs, stopgap plus capture (corrects "combined maximum 4") | **5** in the rolling hour from 10:56:28, against the cap of 4; two HEADs 53 s apart (D-023) | `fetched_at_utc` of the Rainier sidecars |
| Loop requests and errors, 19 Nov | 433 requests logged one by one, 12:24–23:59: 272 JSON GETs, 118 image GETs and 39 HEADs answered 200; 3 image GETs answered 404 (Yosemite twice, Grand Canyon once) and 1 JSON GET was reset (Big Bend). Each of the 4 errors was followed by a back-off. | `request`, `error` and `backoff` records in `capture_log/2026-11-19.jsonl` |
| Host back-off (capture 0.2.0) | After a live 503 asking for 30 min, archive requests sent during the hold: 89 before the fix, 0 after. After an archive 503: a hold is set and the next live pass sends 0 HEADs. | Scratch probe with a fake clock, no network; kept as tests |
| A store failure (capture 0.2.0) | One unchanged image over 2 h with storage failing: fetched 8 times before the fix, once after | Fake feed and refused writes; kept as a test |
| Night frames (colour mode) | Chroma spread on night frames: 0.0002–0.27 at the ARD cameras (solar altitudes of −11 to −49°), 0.011 at Rainier. The greyest daylight frames read 3.2–5.2 (Yosemite, Grand Teton). Acadia's only dark frame, at a solar altitude of −8.9°, read 12.9 (colour). | Sidecar `chroma_spread` (metadata only), with the NOAA solar equations |
| Sharpness (Clear lens input) | Two foggy midday archive frames 2.4 and 2.7; seven other midday hires 31.4–2,073.5; night frames 1,031–1,638 | Laplacian variance at ≤ 640 px on stored frames |
| Test suite | 180 passed (capture 0.2.0); ruff check and format clean | `PYTHONPATH=src:../common/src .venv/bin/python -m pytest -q`, 00:06 on 20 Nov |

### The NPS archive

| What | Value | How and when |
|---|---|---|
| Archive check requests | **217** sent: 7 `GetAvailableDays`, 99 searches (2 of them paging probes, both HTTP 500), 92 thumbnails, 18 hires and 1 page load (the Rainier webcams page, 66,378 B); 5,910,413 bytes; 10 errors (7 TLS timeouts, 1 disconnect and the 2 HTTP 500s); minimum gap 5.106 s start to start; budget 218 of 220. The summary JSON's 203 misses a 14-request first Grand Canyon attempt. | `archive_request` events in `capture_log/2026-11-19.jsonl`, 08:46–10:41 |
| Camera epochs | All 7 ARD cameras went from a 3:2 frame (thumbnail 99×66, hires 999×666) to 16:9 (100×56, 999×562). Switch windows: pore 2023-01-16 → 2023-07-15; grca 2024-01-15 → 07-15; grte 2024-07-15 → 2025-01-15; yose, acad and bibe 2023-07-15 → 2024-01-15; grsm 2024-01-15 → 07-15. | Thumbnail sizes on 13 sampled midday frames per camera, checked on contact sheets |
| Grand Canyon re-aim inside the 3:2 epoch | The 2024-01-15 hires is shifted 51 px right and 30 px up from the 2021-01-15 one (5.1 % and 4.5 % of the frame), at the same scale (D-042) | Full-frame registration in the backtest pair |
| Archive resolution | 999 px wide only, not 4K; no EXIF time in any of the 17 stored hires | Downloaded hires sidecars |
| Archive time stamps | On 90 sampled days, NPS's `SolarElevationAngle` matches a NOAA ephemeris at our parsed UTC: mean 0.015–0.065° a day, best clock offset 0 min. In summer, `ImageCreateDateTime` is local standard time. | `convention_check` with the Sun ≥ 5°, offsets −120 to +120 min |
| NPS's Sun angle is refracted | Sun up (39 assets): NPS − our refracted altitude max 0.0026°, RMS 0.0015°; against the geometric altitude max 0.4768°. Over all 88, NPS applies Sæmundsson refraction at every altitude, even below the horizon (max residual 0.0035°, a −0.73 s time offset). | `lookout-eval ephem-check` on `search_pore_20261117.json` |
| Archive depth | Days available in 2024 and 2025, and days since HRRRv4 (of 2,179): pore 360/357 (1,983); grca 320/359 (1,997); grte 358/359 (2,075); yose 366/350 (1,898); acad 366/359 (2,106); grsm 366/358 (2,165); bibe 335/332 (2,045) | `GetAvailableDays` bitmasks |
| Cadence | Median gap 15 min on every sampled day; partial days down to 22 frames | Search results of 13 dates per camera |
| dHash as an epoch signal | No separation: weather moves midday dHash by up to 43 bits between days | Hamming distance on thumbnails before the guard |

### Forecast

| What | Value | How and when |
|---|---|---|
| Publication lag, live | Run 2026111908: f00 53.33 min … f18 86.37 min. Run 2026111910: f00 51.33, f04 70.57 (after f05's 62.68), f18 85.63. Hours are sometimes published out of order (07Z had f09 before f08). | S3 `LastModified` (the later of GRIB2 and `.idx`) minus run start |
| Publication lag survey | 124 runs, 14 Nov 06Z – 19 Nov 09Z: 2,986 hour-lags; 37.3 % later than the forecast.md curve; per-hour p95 up to 25 min above it (f11 97.1 against 72.2 min); highest p95 100.85 min (f00–f18) and 116.65 min (f48); 3 lags above 120 min (0.10 %, max 131.17). All five 23Z runs published f06–f18 at about 111 min. | `lookout-forecast lag-survey`: one listing per run, 2,018,062 bytes, no GRIB |
| Local stand-in | 24 of 24 hourly runs of 19 Nov written (00Z–23Z) with the 7-field f00–f02 profile. 20.8–22.3 MB of GRIB for each of the 22 runs the stand-in fetched, and for the builder's 10Z smoke run; the builder's 08Z smoke run fetched 25.4 MB (12 GRIB requests). 454 live lags logged; first tick 12:18 | `forecast_log/2026-11-19.jsonl` `run` and `lag` events |
| Live smoke, final code | Run 2026111910, f00–f02, 7 fields, 8 viewpoints: 21,440,626 bytes (10 range requests, 3 `.idx`, 1 listing), 3.46 s wall; 8 valid files; no GRIB left behind | `tests/test_live.py`, 12:01 |
| Whole-run sizes, all 22 fields | 453.0 MB for an 18 h run, 1,202.1 MB for a 48 h run, with terrain fetched once | `lookout-forecast plan` (inventories only) |
| Grid mapping | Max error 1.30e-11 km against ecCodes' own coordinates over 1,905,141 cells; every cell maps back to itself | `lookout-forecast grid-check 2026111907` |
| Datums and the phantom cloud base | See D-026: ceiling and top never below the model terrain; HPBL above ground; 12.4 % phantom `HGT:cloud base` on 07Z f00 (19.8 % on 09Z f03, in review) | `lookout-forecast datum-check 2026111907 --fh 0` |
| Model terrain at the camera cells | Against the manifest elevations: Rainier +13.9 m, Grand Teton +107 m, Acadia −35 m, Big Bend −90 m, Smokies −174 m, Yosemite −182 m, Grand Canyon −920 m (the cell is over the canyon), Point Reyes −150 m (the cell is sea) | `datum-check` camera cells |
| Herbie trial | 4 requests for one field-hour, all as `python-requests/2.34.2`, two with no timeout; byte range identical to ours | Logged trial, 19 Nov (D-026) |
| NOAA download, builder | About 132.5 MB in all on 19 Nov | The forecast builder's transfer ledgers, in its first-workflow report |
| Test suite | 135 passed, 1 failed (`test_cycle_hour_p95_once_a_cell_has_enough_lags`), 1 network test deselected | `.venv/bin/python -m pytest -q -m 'not network'` with a temporary HOME, 00:47 on 20 Nov |

### Evaluation and the backtest pair

| What | Value | How and when |
|---|---|---|
| Backtest pair, v−1 | Grand Canyon, valid 2024-01-15 19:45, 24 h lead, HRRR run 2024011412 f31–f32. Primary box: truth 0.330 (hidden), render 0.504 (partial): a correct negative, by accident (both v−1 errors push towards "not visible"). 2.61 s a pair, 2.15 s of it the render; 4 runs, byte-identical scene and render. | [docs/evidence/m05-backtest-pair.md](docs/evidence/m05-backtest-pair.md), 23:13–23:35 on 19 Nov and 00:16 on 20 Nov |
| Held-out split | Version 1, SHA-256 `c437544e…`; on the real catalogue 0 of 85 archive records and 0 of 105 live frames are held out; 47 of 48 mutants caught | `measure_real.py` (metadata only), 22:45 (D-038) |
| Guard costs | +1.8 µs a record to build a `FrameStore` (2.24 against 2.06 s for 100,000 records); +20.4 µs a frame for the catalogue's quarantine checks (1.74 against 1.33 s for 20,000) | A/B runs in one process, alternating |
| Real render on an eval scene | 573, 586 and 669 ms on SwiftShader (3 of 3 exit 0) | `LOOKOUT_EVAL_RENDER=1 pytest tests/test_renderer_contract.py` |
| Renderer `validateScene()` under Node | 56 ms a call (median of 10, warm) | `renderer_validate()` |
| Extractor v−1 | 21.1 ms for 1920×1080 and 37.7 ms for 3840×2160 from the JPEG; 8 px blur gives ratios 0.45 and 0.12; no camera-fault false positive on 3 night samples | `edge_stats`, median of 10 |
| PNG against JPEG (corrects "within 0.5 %") | The ratio moves by up to 1.19 % at q50, 0.91 % at q75, 0.55 % at q90 and 0.52 % at q95 | 15 procedural images against a 5-frame reference |
| Bootstrap (corrects "8 ms") | 10,000 resamples for 31 days × 8 pairs in **2.3 ms**; coverage of nominal 95 % intervals 196 of 200 | `paired_delta_pss`, 15 warm runs |
| Interval against p-value | The old interpolated interval disagreed with "p < 0.05" in 276 of 6,000 cases; the order-statistic interval in 0 | 3,000 seeds × 2 values of B |
| oculus-core against NOAA | 0.0080° on the 88 NPS times; max 0.0128° in altitude over 2,000 random times and places | `noaa_solar.py` |
| Test suite | 318 passed, 2 skipped (ecCodes is not in eval's environment; the opt-in render), 17.2 s, at 01:46 on 20 Nov. At the held-out fix (D-038): 262 passed, 1 skipped; 253 passed, 9 skipped without oculus-core. | `.venv/bin/python -B -m pytest -q -p no:cacheprovider -m 'not network'` with `PYTHONPATH=src:../workers/common/src`, a temporary HOME holding copies of the 3 samples the tests read, and the proxies pointed at a closed port |

### Renderer

| What | Value | How and when |
|---|---|---|
| Monte Carlo gate (PRD §9.5) | Drawn sky fraction within 1.54 points of the view-ray target at 1,000 seeds × cover 0.1–0.9 × 6 bands, on 4 geometries; negative controls miss by 7.8–93.6 points | `pnpm montecarlo --case above,deck,thin,cirrus` |
| Goldens | 3 of 3 match exactly (SSIM 1.000000) on darwin/arm64 SwiftShader; removing the low deck changes 19.0 % of pixels | `test/goldens.test.ts` |
| Determinism | Two browser launches and the CLI give byte-identical PNGs | `test/determinism.test.ts` |
| Frame time, Chromium on the M4 GPU (Metal) | Heavy 1080p p50 6.0–9.2 ms, p95 7.0–18.5 ms over three runs (the PRD's 8 ms budget is for WKWebView, measured in M2); clear 1080p p50 2.9–4.5 ms | `pnpm bench`, 30–60 frames |
| Frame time, SwiftShader (CPU) | 1080p clear p50 117 ms, heavy 414 ms; first frame 594 ms | `pnpm bench` |
| Readback | `readPixels` plus the row flip 1.1–3.8 ms; base64 encode 50–149 ms (the old "readback" figure was mostly the encode) | `PageTiming` |
| Real NPS plate | A 1920×1080 Rainier sample renders in 740.1 ms (SwiftShader) | `pnpm render` on `samples/mountain.jpg` |
| Test suite | 183 passed (154 unit, 29 headless) in 36.9 s | `pnpm test` |

### lookout-core

| What | Value | How and when |
|---|---|---|
| Parser table | 295 cases in 16 categories, all passing; 19 of 19 mutants of the new rules caught | `tests/parser_table.rs` |
| Parse latency, in process | p50 4.0–4.2 µs, p95 123.7–126.3 µs, max 145.8–151.8 µs (budget 5 ms) | `cargo run --release --example parse_bench 500`, 3 × 10,000 parses |
| Solar events against NOAA | Sunrise max 3.886 s, sunset max 2.832 s (n=200 each); other thresholds max 3.959 s (n=2,000); geometric altitude max 0.0128° (n=4,807) | `tests/solar_vs_noaa.rs`, 25 dates × 8 viewpoints |
| Moon | Position within 0.355° max, 0.100° mean, of an independent almanac Moon; phase within 0.0017 (n=3,200) | `tests/moon_vs_almanac.rs` |
| Key-mash stale-paint test | 40,000 cycles, 0 stale paints; 3 broken gates caught | `tests/keymash.rs` |
| Test suite | 108 passed; clippy `-D warnings`, rustfmt and rustdoc clean | `cargo test --workspace` |

### Infra and cost

| What | Value | How and when |
|---|---|---|
| Infra checks | 82 passed, 0 failed (static 5, root `terraform test` 47, bootstrap 4, scripts 26); 23 of 23 targeted mutations caught; `test_scripts.sh` stalled 10–13 min in 2 of 5 runs (cause unknown) | `make test-infra`, mocked provider |
| Hosting estimate (computed, not measured) | $1.60–2.81 a month with the free tiers, $3.13–8.35 without (PRD §17: ≈ $0–1). S3 requests are the largest line, $1.52–1.89. | AWS list prices × 17,280 runs a month, request counts read from the workers' code (D-035) |
| Lambda image, first local build | Built in 20.0 s. A local invocation of the forecast handler extracted one hour of run 2026111923 from NOAA (3.59 MB) in 1,222 ms after a 299 ms init, with a peak of 138.4 MiB. The infra report on this is not in yet. | `infra/lambda/build.sh` and the Runtime Interface Emulator, 00:12–00:33 on 20 Nov (scratch logs) |
| EventBridge Scheduler free tier (us-east-1) | "$0 for the first 14 million scheduled invocations" | AWS Price List file published 18 Nov 2026 (D-014) |

### lookout-common and the docs checks

| What | Value | How and when |
|---|---|---|
| Limiter across processes with `LOOKOUT_STORAGE=s3` | Minimum gap between request starts: 23 µs before the fix (in-process mode), 1.014 s at a 1.0 s interval after (file mode) | 2 spawned processes × 2 `hold()` each |
| Torn JSONL line | Before: the next append glued onto the fragment and every later read failed. After: both backends read every record, and the fragment is reported in `bad_lines`. | Scratch scripts on a temp file and on a moto S3 bucket |
| Test suite | 258 passed; ruff check and format clean; 8 of 8 manifests valid | `uv run pytest -q` in `workers/common` |
| Docs checks | 13 invariant checks, 45 tests, all passing | `make docs-check` |
| Two more docs checks, proposed (not in `docs/checks/` yet) | `notices`: THIRD_PARTY_NOTICES names every locked runtime package (Python closures with their groups, Rust and npm direct dependencies) at its version. `appendix-a`: PRD Appendix A has the manifest schema's shape. Both clean on the working tree; 22 self-tests pass; on a copy without the 20 Nov additions, `notices` reports 20 missing packages, eckitlib, findlibs and certifi among them. | The docs fixer's scratch modules, `python3 -I -B -m unittest`, 02:00 on 20 Nov |

### M1 preparation

| What | Value | How and when |
|---|---|---|
| Spike unit tests | 105 passed; clippy `-D warnings` clean with and without `--features speech`; four release binaries build (`m1-spike` 12,591,144 B) | `cargo test --workspace` in `spikes/m1-platform` |
| Rust `Instant` is `CLOCK_UPTIME_RAW` | 3 of 3 runs: offset −12 to −84 µs, drift +1 to +146 ns over ~150 ms | `instant_is_the_host_clock_on_this_machine` |
| `nettop` output when piped | Held back: all 4 headers arrived together at 3.0 s; under `script` they came each second | Timestamped header lines only |
| Torn pixel-tag strips | 0 of 29,930 consecutive-tag cuts decode as a third tag | Exhaustive unit test |
| The 11 platform checks | **none run yet** (they need the owner at the keyboard) | — |

### Test suites, all packages

| Suite | Result | How and when |
|---|---|---|
| Full `make test` | common, capture, eval, renderer, rust, infra and docs passed; forecast failed (1 test); 230 s in all | A scratch copy of the working tree, 22:42–22:46 on 19 Nov, on battery |
| Per package, latest | common 258; capture 180; forecast 135 and 1 failure; eval 318 and 2 skips (01:46 on 20 Nov); renderer 183; core 108; infra 82; docs 45; spike 105 | Each package's own report or run, 19–20 Nov |
| CI workflow lint | 0 findings | actionlint 1.7.12 |

### Machine and environment

| What | Value | How and when |
|---|---|---|
| iCloud | `~/Documents` has the iCloud Drive file-provider xattr; `~/lookout-data` has none | `xattr -l`, 19 Nov (D-017) |
| Hidden venv files | 19 Nov: 8,393 in `workers/common/.venv`; 6,173 in `workers/forecast/.venv` (173 at build time). 01:48 on 20 Nov: common 8,393, capture 8,449, forecast 7,549, eval 5,197. | `find <venv> -flags +hidden \| wc -l` (D-031); the 6,173 is the eval fixer's count |
| Battery and power | On battery from 06:13 to 22:48 on 19 Nov. The last reading on battery was 11 % at 22:25; it read 3 % at 22:48, when AC power came on. On AC since. | `pmset -g log` |
| Disk | 28 GiB free of 460 GiB at 00:50 on 20 Nov, 27 GiB at 01:48 (44 GiB at about 12:00 on 19 Nov, the capture fixer's reading) | `df -h ~` |

### Earlier measurements (18–19 Nov)

| What | Value | How and when |
|---|---|---|
| oculus-core ephemeris, worst error | • Sun 0.19′ (0.003°).<br>• Moon 0.13′.<br>• Moon illuminated fraction 0.01 %. | `cargo test --release --test golden_astro` in oculus-core, against JPL Horizons and Astropy, 18 Nov (D-002). 3/3 tests pass, over 294 rows, 3 sites and 1950–2050. The refracted, per-viewpoint validation comes in M2. |
| Rainier park camera frame | 1920×1080, 577 KB (576,746 bytes) | `~/lookout-data/samples/mountain.jpg`, night of 18 Nov |
| Point Reyes air-cam frame | 3840×2160, 1.86 MB (1,861,951 bytes) | `~/lookout-data/samples/porelarge.jpg`, night of 18 Nov |
| Rainier ARD air-cam frame (`mora`) | 512×288, 50 KB (50,099 bytes) | `~/lookout-data/samples/moralarge.jpg` |
| Stopgap Rainier capture | 24 frames stored, 06:30:13 → 12:12 on 19 Nov, in bursts of up to 4 a rolling hour; its last record at 12:12:23 | `capture_log/2026-11-19.jsonl` (D-021, D-023) |
| Rainier timestamps, stopgap | `Last-Modified` is 12–15 s after the EXIF time in 8 of 8 frames, so it marks each upload and works as the HEAD change signal | Frame sidecars and `sips -g creation` |
| Sampled archive day (`pore`, 17 Nov 2026) | • 88 frames, 00:00 → 23:45 PST.<br>• Gaps: 83 of 15 min, 3 of 30 min, 1 of 90 min.<br>• `TimeOfDay`: night 44, day 39, dusk 3, dawn 2.<br>• All 88 carry `SolarElevationAngle`. | `~/lookout-data/samples/nps-json/search_pore_20261117.json` |
| NPS feeds | • 8 of 8 live.<br>• All 8 ARD JSON feeds sampled carry `imagedate`, `imagesite`, `viewdetail` and `imagefilelarge`. | Scouting, 19 Nov; `samples/nps-json/*json.txt` |
| Open-Meteo HRRR | Listed as "Discontinued 2026-11-30" | Open-Meteo docs page, saved 19 Nov 03:16 and re-read locally (D-008) |
| Spend | $0.00 | DECISIONS.md ledger |

### NPS archive start dates

From `GetAvailableDays` (`MinDay`, `MaxDay`), fetched 19 Nov. The files are `~/lookout-data/samples/nps-json/days_<code>.json`. Per-camera cadence, gaps and epochs are in [docs/research/nps-archive-check.md](docs/research/nps-archive-check.md).

The scouting requests used lower-case codes, and `GetAvailableDays` answered all 8. CLAUDE.md writes Acadia and Big Bend in upper case (`ACAD`, `BIBE`), and this table follows it.

| Viewpoint | Code | First day | Latest day |
|---|---|---|---|
| `rainier.paradise-mountain` | none: the park camera has no archive | — | — |
| (Rainier ARD air cam; not a viewpoint) | `mora` | 2003-07-18 | 2026-11-18 |
| `point-reyes.beach` | `pore` | 2004-01-06 | 2026-11-18 |
| `grand-canyon.yavapai` | `grca` | 2001-11-08 | 2026-11-18 |
| `grand-teton.science-center` | `grte` | 2011-08-30 | 2026-11-18 |
| `yosemite.turtleback` | `yose` | 2005-10-18 | 2026-11-18 |
| `acadia.mcfarland` | `ACAD` | 2006-08-08 | 2026-11-19 |
| `smokies.look-rock` | `grsm` | 1998-04-01 | 2026-11-19 |
| `big-bend.panther-junction` | `BIBE` | 2004-10-05 | 2026-11-19 |
