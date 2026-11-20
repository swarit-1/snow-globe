# PRD_PROGRESS.md: tickets, owner actions and measured numbers

This file tracks progress per ticket against [PRD.md](PRD.md) v0.4.1 and [PLAN.md](PLAN.md), with every measured number (PRD §16.1). Decisions and the spend ledger are in [DECISIONS.md](DECISIONS.md).

**Updated:** Fri 20 Nov 2026, 10:50 UTC (G+1), with corrections and re-checks at about 11:50 UTC. Snapshot rows say when they were read. M0 is done, and M0.5 is in progress (day 2 of 4).

**Running overrun before the 14 Dec freeze:** 0 days, of the 4 allowed across M0.5, M1 and M1.5 (PLAN.md, calendar notes).

**Status values:**
- **done:** its evidence exists.
- **in progress:** being built, but the evidence doesn't exist yet or isn't complete. Written code with no evidence counts here, not as done.
- **blocked on owner:** waiting on one of the owner actions below.
- **not started.**

**Committed so far:** `0bb42ab` (common, manifests, Makefile, CI) to `8246a8b`. The second workflow's packages went in one commit each at 02:37–02:42 UTC on 20 Nov (D-046).

**In the working tree, uncommitted, for the lead:**
- forecast 0.2.0 (D-053, D-054) and capture 0.2.1 (D-048);
- eval's data report and provenance fix (D-061, D-062);
- infra's grants, offline provider install and cost estimate (D-059, D-060);
- the speech library `app/speech-macos/` (D-055);
- `bench/`, `docs/m1/` and `README.md` (D-057);
- the Makefile, CI and `docs/checks/` (D-063);
- the backfill plan `docs/research/archive-backfill-plan.md` (D-052);
- these planning docs with `THIRD_PARTY_NOTICES`.

---

## Owner actions

Most urgent first. Other files cite these numbers (the tickets below, `docs/m1/owner-runbook.md`, DECISIONS.md), so a new action joins an existing item or goes at the end, and the numbers never move.

1. **Keep the Mac on AC power.**
   - Capture and the forecast job run on this laptop. The capture loop runs under `caffeinate -s`, which keeps the whole Mac awake only on AC power, and the forecast job relies on it (D-030).
   - On 19 Nov it ran on battery from 06:13 to 22:48 UTC, slept 4 times and lost 8.22 h of all-camera capture.
   - On 20 Nov it was on battery again from 03:29 to 06:27 UTC. The lid closed at 03:52, and 1.99 h of capture was lost (D-051). That moved the 72 h clock's earliest close to 22:37 UTC on Sun 22 Nov, by PLAN's rule (D-061).
   - Rainier's gaps can never be backfilled.
2. **AWS.** Until AWS is live, capture depends on this laptop, and it must be live before the sealed window opens on Tue 15 Dec.
   - Use an account that runs only Lookout (the budget counts the whole account), on the Paid plan.
   - Provide credentials through the environment, and set `TF_VAR_alarm_email`.
   - Check the Lambda concurrency quota is at least 102 (`aws lambda get-account-settings`).
   - **Before the first deploy, decide D-035,** now with D-060's estimate: $2.07–4.03 a month with the free tiers in month 1, rising by about $0.22 a month. The questions are whether PRD §17's $3 is monthly, and whether the hard stop stays on through the sealed window. At the top of the range a $3 hard stop would halt both jobs around day 27 of month 1. The recommendation is to switch it off for 15 Dec – 14 Jan and keep the email alerts.
   - Then run `infra/deploy.sh --dry-run` and `infra/deploy.sh`, and confirm the SNS email. Nothing is applied without your word (D-014).
3. **NPS confirmation emails.**
   - The drafts are in [docs/outreach/nps-confirmation.md](docs/outreach/nps-confirmation.md), for the NPS Air Resources Division and Mount Rainier NP. The Rainier one asks where the "mountain" camera is mounted, how often it updates and whether past frames are kept.
   - The archive backfill now runs (D-052). The draft's "[N] frames" now holds the plan's figures: about 4,300 frames in the first five days (about 0.25 GB), and about 22,700 frames (about 1.3 GB) by mid-December, at most 1,000 requests a day, one at a time, at least 5 seconds apart. Check them before you send it. The budget stays at 1,000 until NPS answers.
   - Golden Gate, if you want it: [docs/outreach/golden-gate.md](docs/outreach/golden-gate.md), by **Fri 4 Dec**.
4. **Permission to push.**
   - **First, or with it, answer D-041.** The synthetic renderer goldens are already committed (`b5bc726`). If you decline them, the lead removes them from git history before the push; deleting them in a later commit would still publish them.
   - The clone's `origin` is `swarit-1/snow-globe`, and nothing has been pushed.
   - The CI workflow (`.github/workflows/ci.yml`) runs only after the first push. The oculus-core pin is public now (D-025), so CI can fetch it.
   - CI runs every suite except the speech library's, which needs macOS. A macOS runner is your call, because it bills at a higher rate on a private repository (D-063).
5. **Deepgram and fal: accounts and keys, when you're ready.**
   - Keys go in the server's environment, never in the app. For the bake-off, put the Deepgram key only in a shell opened for that, never one that runs an agent, and unset it afterwards ([docs/m1/owner-runbook.md](docs/m1/owner-runbook.md), step 5).
   - Approved (D-012): Deepgram for the bake-off, and fal for the ≤ $1 kill test only.
   - Even with a key, you're asked before the first paid call to each vendor (PRD §17). Any other fal spend needs your approval first (PLAN.md, Rules).
   - Deepgram is needed for the M1 bake-off (from 23 Nov), and fal for the M3 kill test (from 27 Dec).
6. **Run the M1 1a spike checks** (from Mon 23 Nov), at the keyboard, following `spikes/m1-platform/README.md` and the runbook's step 2.
   - **First, before Mon 23 Nov, read [docs/m1/owner-runbook.md](docs/m1/owner-runbook.md).** It sequences everything M1 needs from you, these checks included.
   - Checks 1–4 need no permission. Check 5 needs Microphone; checks 8 and 10 need Screen Recording for the terminal; check 9 needs Accessibility for the injecting terminal (D-033); check 11 runs over several days.
   - Grant them to a terminal you never use for coding agents. Switch Microphone and Speech Recognition off right after checks 5–7, and the other three after the spike.
   - **Checks 6 and 7** use the speech library, now built (D-055). First run `lookout-transcribe status --locale en-US`. If it reports code 1, run `lookout-transcribe install-assets --locale en-US --yes`. Keep the "reservations" lines from before and after.
   - Report every run, failed and inconclusive ones too. On a `FAIL`, stop and send the run file.
7. **Permissions in M1:** Microphone and Speech Recognition, when the app first asks; Screen Recording and Input Monitoring for the harness only; Accessibility for the injection check (open, D-033); Apple Intelligence on. Also confirm that a speech session with Speech Recognition "not determined" shows no prompt (D-055).
8. **Replay recordings in M1,** once the recording tool exists (M1-09):
   - the 130 scripted lines (about 20 minutes; `bench/`, D-057), saying numbers as `check.py --reading-list` spells them, and the two consent questions;
   - then ≥ 30 real-use utterances.
9. **Confirm or change, by Mon 14 Dec:**
   - which renderer P1 scores, (a) the freeze commit or (b) the M4 renderer under a no-peek rule (D-019);
   - D-024's design per viewpoint, including the reading that Point Reyes's development data starts in summer 2023 (D-052);
   - whether earlier archived winters may feed design-P fits at Point Reyes (the backfill plan, section 10);
   - persistence (b) beyond a 24 h lead (D-036);
   - eval's readings of the held-out split (D-038);
   - the parser's rule readings: "tonight", dates and the same day, "next friday", "tomorrow" = 12:00, and a lone generic word as a miss (D-039);
   - the newer readings (D-058): dawn and dusk at −6°, a passed "tonight at N" going to tomorrow, "tomorrow" as the place's date, horizons from now, and the last place after a miss;
   - three rule questions from the replay script (`bench/README.md`, "Rows waiting on the owner"): a fuzzy-only near-alias, a place name plus a leftover word, and a lone generic word.

   Two calls come earlier: D-035 before the first deploy (owner action 2), and D-041 before the first push (owner action 4).
10. **iCloud.** Consider moving the repo out of `~/Documents`, which would end the venv and build-output workarounds (D-031).
11. **Disk space.** 22 GiB was free of 460 GiB at 10:38 UTC on 20 Nov, and still at 11:47 (95 % used). The live policy adds about 262 MB a day (D-028), and the whole backfill plan about 1.33 GB (D-052). Apple Intelligence's on-device models take several GB.

**Later** (PRD §1 rows 14–15):
- Several nights of unattended idle trials (M1), after the lead's decision on display sleep (D-057).
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
| M0.5-01 Bring PLAN.md and DECISIONS.md to v0.4 (D-012 to D-021); start this file | done | [PLAN.md](PLAN.md), [DECISIONS.md](DECISIONS.md), this file (`a1fca21`); D-022 to D-045 since (`2e3b12d`, `535084b`, `034c46b`), and D-046 to D-063 on 20 Nov (uncommitted) | — |
| M0.5-02 Data directory outside iCloud | done | DECISIONS.md D-017, D-031 | see "Machine and environment" |
| M0.5-03 Scout the 8 NPS feeds; fix the viewpoint IDs | done | DECISIONS.md D-020; `~/lookout-data/samples/nps-json/` | 8 of 8 feeds live |
| M0.5-04 NPS archive API and depth | done | DECISIONS.md D-018 | start dates below |
| M0.5-05 Email drafts: NPS, Golden Gate | done | [docs/outreach/](docs/outreach/) (`e4722a6`); the Rainier questions added on 20 Nov, and the backfill's frame counts later that day (D-052) | — |
| M0.5-06 Stopgap Rainier capture | done: ran 06:30–12:13 UTC on 19 Nov, then retired | DECISIONS.md D-021, D-023 | 24 frames; 4 outages (see "Outages") |
| M0.5-07 Repo scaffold: uv, pnpm and Cargo projects, viewpoint schema and manifests, per CLAUDE.md | done | `0bb42ab`; [docs/checks/](docs/checks/) (`c9ba843`; 20 Nov changes uncommitted); DECISIONS.md D-063 | `make test`: 10 of 10 suites pass, 224 s (see "Test suites") |
| M0.5-08 Capture worker, 8 cameras.<br>• Polite: fetch on change, ≤ 4 per hour with persisted state.<br>• Quality gates (PRD §8.6), stored at ≤ 1920 px.<br>• Rainier cadence measured. | in progress: 0.2.0 runs for all 8 cameras since 02:39:39 UTC on 20 Nov, with D-028's storage and the archive queue; 0.1.0 ran from 12:24:23 UTC on 19 Nov. 0.2.1 (poll margin) is uncommitted. | `workers/capture/` (`4fae4c1`, `df1838a`); DECISIONS.md D-023, D-028, D-029, D-047, D-048, D-049; [docs/research/nps-archive-check.md](docs/research/nps-archive-check.md) | Rainier updates about once a minute; 262 MB a day under D-028; since the restart, the shortest poll gap is 301.05 s |
| M0.5-09 Forecast worker.<br>• HRRR byte ranges at the view-ray sample points.<br>• One JSON per viewpoint per run.<br>• Lag per forecast hour. | in progress: 0.1.0 runs as the local stand-in with 8 fields; 0.2.0 (survey lags, `tick`, the stamp floor) passes its tests, uncommitted | `workers/forecast/` (`09e2ec8`); DECISIONS.md D-026, D-027, D-030, D-050, D-053, D-054 | 34 of 34 hourly runs from 2026111900 to 2026112009 written; 4,082 survey lags; 576 of 576 model cells off the fallback |
| M0.5-10 Trace skeleton | done | `app/core/src/trace.rs` (`e7b87e8`) | no field can hold transcript text (a test walks the JSON) |
| M0.5-11 Infra code: Terraform (state backend, storage, CDN, budget alarm, Lambda + EventBridge Scheduler) and the Lambda image | in progress, code only: CloudFront and the API are TODO(M1); capture's S3 grants fixed for D-028; the provider installs offline; three local smoke runs of the forecast handler | `infra/terraform/`, `infra/lambda/`, `infra/cost/` (`0727ac8`, `6c385f5`; 20 Nov changes uncommitted); DECISIONS.md D-034, D-059, D-060 | `infra/test.sh` 113 checks pass; estimate $2.07–4.03 in month 1 (D-060) |
| M0.5-12 AWS deployment | blocked on owner (owner action 2) | DECISIONS.md D-014, D-034, D-035 | — |
| M0.5-13 CI (GitHub Actions, CI only) | in progress: the workflow runs every `make test` suite but the laptop-only speech suite, and `docs/checks` keeps the two lists equal. Its first GitHub run needs a push (owner action 4). | `.github/workflows/ci.yml` (`0bb42ab`, `c9ba843`; 20 Nov changes uncommitted); DECISIONS.md D-063 | actionlint 1.7.12: 0 findings (19 Nov) |
| M0.5-14 One backtest pair: an archived frame and an archived HRRR run → renderer v−1 → a crude extractor score, labelled "v−1" | done (committed in `d9aa416`) | [docs/evidence/m05-backtest-pair.md](docs/evidence/m05-backtest-pair.md); DECISIONS.md D-042, D-062 | Grand Canyon, 24 h lead: scored, a correct negative by accident; 2.6 s a pair; counts toward no metric |
| M0.5-15 NPS archive check: cadence, gaps and camera epochs per camera | done; six of the seven epoch switches dated by the backfill's T0 on 20 Nov (Grand Teton was not queued) | [docs/research/nps-archive-check.md](docs/research/nps-archive-check.md) (`4fae4c1`, `2e3b12d`; Grand Canyon corrected on 20 Nov); DECISIONS.md D-022, D-042, D-052 | 217 requests; every ARD camera went 3:2 → 16:9 in 2023–25; T0 dated six of the switches to within 14 days (26 at Grand Canyon) |
| M0.5-16 H or P per viewpoint in DECISIONS.md | done | DECISIONS.md D-019, D-022, D-024, D-042 | — |
| M0.5-17 Emails sent | blocked on owner (owner action 3) | — | — |
| M0.5-18 **Gate:** ≥ 72 h of worker and capture logs | in progress: the clock runs from 12:24:23 UTC on 19 Nov for all 8 cameras (06:30 UTC for Rainier), and outages extend it. By PLAN's rule (capture outages, with the data report's 540 s threshold) it closes at 22:36:51 UTC on Sun 22 Nov at the earliest, or about 22:40 counting the 02:39 restart as the lead does; counting both jobs (proposed), 22:50:09 UTC. The lead chooses the rule (D-061). | `~/lookout-data/capture_log/`, `forecast_log/`; `lookout-eval data-report` (D-061) | 10.21 h lost to 10:30 UTC on 20 Nov, in 8 loop outages (4 a day); 5 forecast outages (see "Outages") |
| M0.5-19 A council round on the parts taken from Lookout, adding a forecast-verification judge | not started (optional) | PRD §18.2 | — |
| M0.5-20 Local stand-ins until AWS is live: the capture loop and the forecast tick | in progress: running from the snapshot `034c46b` with its own venvs since 02:39 UTC on 20 Nov (`cfefa79` before); the capture loop runs under `caffeinate -s`, and the forecast tick relies on it | DECISIONS.md D-030, D-049, D-050, D-051 | 137 loop passes in the data report's window to 10:30 UTC (69 of 0.1.0, 68 of 0.2.0); the forecast tick from 12:18 UTC on 19 Nov |

### M1: Voice to first pixel (Mon 23 Nov → Mon 7 Dec) · not started, with some core logic started early

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M1-01 Platform spike: one day, 11 checks (PLAN.md 1a) | in progress (early): the spike app is built; none of the 11 checks has run (owner action 6). Checks 6 and 7 can use the speech library once the spike is rebuilt with `--features speech` against it; that build links the real library: 0.1.0 at 03:39 UTC on 20 Nov, and 0.2.0 since the lead rebuilt it at 12:36 UTC. | `spikes/m1-platform/` (`b73c53b`); [docs/m1/owner-runbook.md](docs/m1/owner-runbook.md); DECISIONS.md D-032, D-033, D-055 | 105 unit tests; see "M1 preparation" |
| M1-02 `lookout-core`: query lifecycle, `query_gen` cancellation, trace | in progress (early) | `app/core/` (`e7b87e8`) | key-mash: 40,000 cycles, 0 stale paints |
| M1-03 Parser fast path and time rules.<br>• ≥ 150 table-driven cases and property tests.<br>• DST and cross-zone cases. | in progress (early): some rule readings wait for the owner (D-039, D-058); back-references ("same place") are to be built on dev row d06, never on a test row (D-057) | `app/core/tests/parser_table.rs`, `properties.rs` | 295 table cases; parse p95 124–126 µs |
| M1-04 Ephemeris wrapper and solar events over oculus-core `1b5410b` | in progress (early) | `app/core/src/solar.rs`; DECISIONS.md D-025 | sunrise and sunset within 3.9 s of NOAA; Moon within 0.355° |
| M1-05 Speech A1: `DictationTranscriber` through the Swift static library | in progress (early): library 0.2.0 built and unit-tested on a mock engine, uncommitted; never run against the real recognizer, which needs the owner (owner action 6) | `app/speech-macos/`; DECISIONS.md D-055, D-056 | 103 tests; push p99 ≤ 5.12 µs; see "M1 preparation" |
| M1-06 Speech D: Deepgram Nova-3, bake-off only | blocked on owner (owner action 5) | DECISIONS.md D-015 | — |
| M1-07 Overlay with Tier 0: states, caption, chips, destination chip, focus model, paint gate | not started | — | — |
| M1-08 Tier 0: library index, Sun-position match, background decode | not started | — | — |
| M1-09 Replay and paint-capture harness | not started: the recording tool, the scorer and two trace fields are TODO(M1); the scoring rule is proposed (D-057) | `bench/README.md` | — |
| M1-10 Replay recordings: ~130 scripted, plus ≥ 30 real-use | in progress (early): the script is ready, nothing is recorded; recording waits for M1-09 and the owner | `bench/utterances/scripted.tsv` (uncommitted); DECISIONS.md D-057 | 130 rows (30 dev, 100 test), script hash `3b4256f1cf51`; the parser alone gets 94 of 100 test rows exact on the text |
| M1-11 macOS permissions | not started (owner, in M1): the runbook names all five grants, and when to switch each off | [docs/m1/owner-runbook.md](docs/m1/owner-runbook.md), step 1 | — |
| M1-12 Overnight idle trials: ≥ 100 for the shipped engine, ≥ 60 for the comparison engine | not started: before the first night the lead decides whether idle passes keep the display awake (D-057) | [docs/m1/owner-runbook.md](docs/m1/owner-runbook.md), step 6 | — |
| M1-13 Stale-paint stress test, ≥ 10,000 cycles | in progress (early): the Rust model of the paint gate passes; the webview gate is M1 work | `app/core/tests/keymash.rs` | 40,000 cycles, 0 stale paints; 3 broken gates caught |
| M1-14 README skeleton with blanks | done (early), awaiting the lead's commit: the headline sentence and the metrics table, every number a blank | [README.md](README.md) | — |
| M1-15 **Gate:**<br>• replay report;<br>• ≥ 150 parser cases;<br>• STT decision;<br>• 0 stale paints;<br>• a screen recording over another app. | not started | — | — |

### M1.5: Walking skeleton (by Thu 10 Dec) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M1.5-01 Renderer v0, headless: Hillaire sky, cloud slabs, ceiling slab, flat fog, camera v0 | in progress (early): renderer 0.2.0 has cloud slabs, a ceiling slab, fog and camera v0; the Hillaire sky is TODO(M1.5) | `renderer/` (`b5bc726`) | see "Renderer" |
| M1.5-02 First Rainier plate: base frame, pose, summit band. Needs a captured midday frame with the summit visible. | not started | — | — |
| M1.5-03 Extractor v0 | not started (extractor v−1 exists in eval) | `eval/src/lookout_eval/extractor.py` | — |
| M1.5-04 **Gate:** dev-only P1 table against persistence, climatology and S_rule | not started | — | — |
| M1.5-05 **Gate:** cloud Monte Carlo test | in progress (early): passes on renderer 0.2.0; re-run on the M1.5 renderer | `renderer/src/cpu/montecarlo*.ts`; `pnpm montecarlo` | within 1.54 points of the view-ray target at 1,000 seeds |
| M1.5-06 Design-H archive backfill: T0, T1a, T1b, T3pr, T2, T1c (not a gate item) | in progress: the plan is written and reviewed; the lead queued T0 and T1a (447 entries) at 09:50 UTC on 20 Nov; T0 is done, T1a is running | [docs/research/archive-backfill-plan.md](docs/research/archive-backfill-plan.md) (uncommitted); DECISIONS.md D-052 | at 10:39 UTC: 146 of 447 entries done, 454 requests, 318 frames, 16.0 MB of frames. At 11:43 UTC the day's 1,000 requests were spent: 180 done, 1 failed |

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
| M5-01 README with the numbers, 60-second demo, `THIRD_PARTY_NOTICES`, and `make score` from a fresh clone | in progress (early): a first `THIRD_PARTY_NOTICES`, written by hand from the lock files and revised on 20 Nov. `docs/checks` keeps it in step with every `uv.lock`, `Cargo.lock` and `package.json`; the Terraform lock files and Lookout's own package versions are checked by hand. The README skeleton (M1-14) | [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES), [README.md](README.md) | — |

### v1 growth (after M5) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| V1-01 Viewpoints 3–8, each with plate, pose, labels, goldens and ≥ 14 days of live data | not started. Capture for all 8 runs since 19 Nov. | — | — |
| V1-02 Live-beside-simulated view | not started | — | — |

---

## Measured so far

Every entry below was measured or checked on this machine (MacBook Air M4, macOS 26.5.1), or by NOAA's or NPS's own servers as stated. Times are UTC. The agents' reports and the evidence files hold the full tables.

### Outages

Outages extend the 72 h gate, as they extend the sealed window, and each one is listed as an exclusion (PLAN.md, M0.5 gate). Every outage so far was the laptop sleeping on battery (`pmset -g log`). `caffeinate -s` holds the Mac awake only on AC power. The Mac was on battery from 06:13 to 22:48 on 19 Nov, and from 03:29 to 06:27 on 20 Nov (D-051).

| Job | Outage | Length | Lost (extends the clock) |
|---|---|---|---|
| Rainier stopgap | 19 Nov 06:50:14 → 07:10:34 | 20.3 min between polls | ~3 polls (before the clock) |
| Rainier stopgap | 19 Nov 07:50:37 → 08:25:22 | 34.8 min | ~6 polls (before the clock) |
| Rainier stopgap | 19 Nov 08:50:24 → 09:41:57 | 51.5 min | ~9 polls (before the clock) |
| Rainier stopgap | 19 Nov 10:01:59 → 10:57:19 | 55.3 min | ~10 polls (before the clock) |
| Handover, by design | 19 Nov 12:12:23 → 12:24:40 (last stopgap record → first loop pass) | 12.3 min | the 11-minute guard window (D-023) |
| Capture loop, 8 cameras | 19 Nov 12:56:00 → 15:09:08 | 133.1 min between passes | 128.1 min |
| Capture loop | 19 Nov 15:09:08 → 18:02:52 | 173.7 min | 168.7 min |
| Capture loop | 19 Nov 18:02:52 → 19:35:08 | 92.3 min | 87.3 min |
| Capture loop | 19 Nov 19:58:15 → 21:52:09 | 113.9 min | 108.9 min |
| Capture loop, restart on 0.2.0 (D-049) | 20 Nov 02:36:58–02:37:11 → 02:45:04–02:45:15 (each camera's last 0.1.0 poll → its first 0.2.0 poll) | 483–486 s between polls at each camera | none under the report's 540 s rule; 183–186 s if counted as the lead counts it, a short outage (D-061) |
| Capture loop | 20 Nov 04:10:03 → 05:02:43 | 52.7 min | 47.7 min |
| Capture loop | 20 Nov 05:02:43 → 05:42:35 | 39.9 min | 34.9 min |
| Capture loop | 20 Nov 05:42:35 → 06:18:37 | 36.0 min | 31.0 min |
| Capture loop | 20 Nov 06:18:37 → 06:29:29 | 10.9 min | 5.9 min |
| Forecast stand-in | 19 Nov 13:55:36 → 15:09:31 (run 13Z) | 73.9 min | 68.9 min |
| Forecast stand-in | 19 Nov 16:08:09 → 18:32:10 (runs 15Z–17Z) | 144.0 min | 139.0 min |
| Forecast stand-in | 19 Nov 18:54:08 → 19:35:26 (run 18Z) | 41.3 min | 36.3 min |
| Forecast stand-in | 19 Nov 19:55:38 → 21:40:34 (runs 19Z–20Z) | 104.9 min | 99.9 min |
| Forecast stand-in, restart (D-049) | 20 Nov: the old loop's stop (not logged) → 02:39:48 (the new loop's start, `ps`); first fetch 02:39:49.5 | not measured | none: no run fell due in it (2026112001 was written at 01:57:09; 2026112002 was due at 02:55:14 and written at 02:55:30) |
| Forecast stand-in | 20 Nov 04:55:13 → 05:18:20 (run 04Z, written 1,387 s late) | 23.1 min | 18.1 min |

- **How:** `lookout-eval data-report --until 2026-11-20T10:30:00Z` (read-only, D-061), checked against `capture_log/` and `pmset -g log`.
  - A capture outage is a gap of over 540 s between passes (the data report's threshold); the lost time is the gap less one 300 s interval. The 02:39 restart's 147.5 s pass gap is not one, though each camera then went 483–486 s between polls.
  - A forecast run is due when its last extracted hour is on S3 (its newest `LastModified`). A forecast outage is a due run with no live run finished within 900 s of that time; it runs from the due time, the table's start, to the finish of the next live run written, and the time is lost from due + 300 s, so lost = length − 300 s.
  - The stopgap rows are the earlier hand count: missed = round(gap ÷ 5 min) − 1.
  - The passes at 15:09 and 18:03 on 19 Nov, and 05:02, 05:42 and 06:18 on 20 Nov, ran in brief dark wakes.
- **Totals to 10:30 UTC on 20 Nov:** capture lost 10.21 h of 22.09 h (8.22 h on 19 Nov, 1.99 h on 20 Nov), so 11.89 h are covered. The forecast job lost 6.04 h, and the union of both is 10.43 h. No outage since 06:29:29 UTC (the data report re-run to 11:42 UTC gives the same clocks).
- **The 72 h clock:** by PLAN's rule (the capture loop's outages, with the data report's 540 s threshold) its earliest close is 22:36:51 UTC on Sun 22 Nov. Counting the restart's 183–186 s as well, as the lead does, it is about 22:40 UTC (22:39:54–22:39:57). Counting both jobs, which is proposed and not a recorded rule, it is 22:50:09 UTC, or about 22:53 with the restart. The lead chooses both parts of the rule, and a new outage moves the close again (D-061).
- **Forecast catch-up on 19 Nov:** every hourly run of 19 Nov (00Z–23Z) was written by 23:56.
  - 00Z–11Z came as catch-up at the stand-in's first ticks (12:18–12:26). The exceptions are 08Z and 10Z, which the forecast builder's smoke tests wrote at 09:54 and 12:01.
  - The data report counts 2026111900 and 2026111901 as catch-ups in its window.
- **Not live-worker evidence:**
  - the backtest pair's archived `run` event (run 2024011412) at line 475 of `forecast_log/2026-11-19.jsonl`;
  - the 12 re-extractions and 160 duplicate lags of 02:39–02:50 UTC on 20 Nov (D-050);
  - the 4,082 survey lags in `forecast_log/lag_survey/`;
  - the archive queue's records (`"worker": "archive"`).

### Capture

| What | Value | How and when |
|---|---|---|
| Rainier update cadence | **About once a minute.** At every poll its newest image was at most 125.9 s old: median age 20.6 s, p90 69.0 s (n=48; 40 of 48 at most 60 s). To 23:57: n=63, median 20.7 s, p90 70.3 s, max 125.9 s. The camera's own EXIF times sit within 5.1 s of a whole minute (median 2.1 s), and each upload's `Last-Modified` follows 10.3–26.3 s later (median 22.8 s; n=20 loop frames, sidecars read at 01:50 on 20 Nov). | `lookout-capture cadence` (capture 0.2.0): server answer time minus HEAD `Last-Modified` at each poll, 19 Nov to 22:37:29. Every consecutive pair of polls changed, so the 300 s intervals measure our poll spacing, not the camera. |
| ARD camera cadence | `imagedate` every 900 s (median, n=6–7 each; one 1,800 s gap at Yosemite); the image is 4.5–25 min old when polled. The JSON file is rewritten twice every 10 minutes (minutes 1–2 and 5 past each 10-minute mark, 209 of 209 values). | Same tool, capture log of 19 Nov |
| Image times | EXIF − JSON `imagedate` = 0.0 min at 7 of 7 ARD cameras. Image `Last-Modified` − EXIF = +6.50 to +6.88 min (ARD) and +0.171 min at Rainier. JSON `Last-Modified` − image `Last-Modified` = 8–25 s (Yosemite 8, Point Reyes 13, Grand Teton and the Smokies 18, Grand Canyon 22, Acadia and Big Bend 25). | The 8 sidecars of the smoke pass, 10:56 |
| Smoke pass, all 8 | 8 of 8 stored in 17.88 s: 16 nps.gov requests (7 JSON GETs, 1 HEAD, 8 image GETs), 15,742,510 image bytes downloaded, 5,175,244 stored | `lookout-capture once` at 10:56:15 |
| Frame sizes | ARD: 490,890–3,493,083 B at 3840×2160, stored as 203,877–868,375 B at 1920×1080 (q90), over all 131 ARD frames to 00:50 on 20 Nov; the 7 of the smoke pass at 10:56 alone gave 860,197–2,741,881 B, stored as 569,728–736,571 B. Rainier 1920×1080, kept as downloaded: 825 KB mean (n=32), 1,687 KB with the Sun up (n=6). | ARD: `original_bytes` and `stored_bytes` in the frame sidecars, read at 01:50 on 20 Nov. Rainier: the storage run at 22:38 (D-028). |
| Storage per day | 262.0 MB for 8 cameras under D-028 (Rainier 100.8, Point Reyes 55.4, the six hourly cameras 13.3–21.1 each), against 451.9 MB keeping every frame. 90 days: 23.6 GB against 40.7 GB. Re-run over all of 19 Nov: 269.9 against 468.9 MB a day. | `lookout-capture storage --day 2026-11-19` at 22:38 (105 frames) and 23:58; thumbnails made in memory; 96 downloads a camera a day |
| On disk now | 325 frames (209.3 MiB of JPEG; Rainier 76, 24 of them from the stopgap), 223 thumbnails (13.3 MiB), 24 staged downloads (15.0 MiB) and 907 archive frames (42.7 MiB). At 11:01: 321 frames, 207 thumbnails, 12 staged and 578 archive frames. At 00:50 on 20 Nov: 170 frames (102 MB) and 85 archive frames. | `find` (JPEG files and their bytes), 11:47 on 20 Nov; the 11:01 counts are the docs builder's `find` and `du` |
| Downloads by policy, capture 0.2.0 (D-028), 02:39–10:30 on 20 Nov | Rainier and Point Reyes kept every download full size (24 and 25) and made a thumbnail of each. At the six hourly cameras, 23–25 downloads each; 6–7 were promoted to `frames/` as the frame nearest their hour, and every download got a thumbnail. For hour 03:00, Acadia, Grand Canyon, Grand Teton and Yosemite each dropped their 3 staged copies (02:45, 03:00 and 03:15; 12 in all) and kept 0.1.0's 02:30 frame as the hour's only full-size frame (D-049; the report's `staging_dropped: 1` counts events, not copies). Before the switch, 0.1.0 kept every download at all 8 (23–25 each). Every logged frame and thumbnail is on disk, and none is unlogged. | `lookout-eval data-report` (D-061), the capture block |
| Poll spacing, 19 Nov (corrects "at most every 5 minutes held") | 8 of 504 per-camera gaps were 299.06–299.7 s, on send times: Grand Teton, Point Reyes, Rainier ×2, the Smokies ×2 and Yosemite ×2, at 12:29 and 23:05–23:16. The cap of 4 GETs in any rolling hour held at every camera (D-048). | The second workflow's critic: send time = log time − `elapsed_ms` |
| Poll spacing since the restart (capture 0.2.0, the stamp at the send and a 1 s margin) | 70 polls per camera from 02:39:39 to the pass that ended at 10:45:05 on 20 Nov; the shortest gap between sends is 301.053–301.064 s, with a median of 312.8–314.9 s; none is under 300 s | `request` records (JSON GETs and Rainier HEADs) in `capture_log/2026-11-20.jsonl`, send = log time − `elapsed_ms` |
| Capture 0.2.1 on an exact 300 s schedule (fake clock, no network) | 0.2.0 on the Lambda schedule over 24 h: 48 skipped invocations (6 per camera), longest gap 555.9 s, passes 37.9 s on average and up to 63.1 s. 0.2.1: 0 skips, gaps 300.001–300.539 s, passes 18.5 s (max 20.0 s). Over 10 days with a 20 ms storage delay, 0 skips and a longest gap of 300.726 s. Under `loop` for 72 h with 0 and 30 s jitter: no gap under 300 s (D-048). | The capture fixer's schedule simulation on a fake clock, 8 cameras, fake feeds |
| Rainier image GETs, stopgap plus capture (corrects "combined maximum 4") | **5** in the rolling hour from 10:56:28, against the cap of 4; two HEADs 53 s apart (D-023) | `fetched_at_utc` of the Rainier sidecars |
| Loop requests and errors, 19 Nov | 433 requests logged one by one, 12:24–23:59: 272 JSON GETs, 118 image GETs and 39 HEADs answered 200; 3 image GETs answered 404 (Yosemite twice, Grand Canyon once) and 1 JSON GET was reset (Big Bend). Each of the 4 errors was followed by a back-off. | `request`, `error` and `backoff` records in `capture_log/2026-11-19.jsonl` |
| Loop requests and errors, 20 Nov to 10:30 | 1,048 live requests: 677 JSON GETs, 97 HEADs and 267 image GETs answered 200; 4 image GETs answered 404 (Yosemite twice, Big Bend, Acadia); 3 failed in transit (an Acadia connect timeout, a Rainier read timeout and a Grand Canyon reset). Each of the 7 errors was followed by a back-off. The archive queue's own error is under "The NPS archive". | `request`, `error` and `backoff` records in `capture_log/2026-11-20.jsonl` |
| Host back-off (capture 0.2.0) | After a live 503 asking for 30 min, archive requests sent during the hold: 89 before the fix, 0 after. After an archive 503: a hold is set and the next live pass sends 0 HEADs. | Scratch probe with a fake clock, no network; kept as tests |
| A store failure (capture 0.2.0) | One unchanged image over 2 h with storage failing: fetched 8 times before the fix, once after | Fake feed and refused writes; kept as a test |
| Night frames (colour mode) | Chroma spread on night frames: 0.0002–0.27 at the ARD cameras (solar altitudes of −11 to −49°), 0.011 at Rainier. The greyest daylight frames read 3.2–5.2 (Yosemite, Grand Teton). Acadia's only dark frame, at a solar altitude of −8.9°, read 12.9 (colour). | Sidecar `chroma_spread` (metadata only), with the NOAA solar equations |
| Sharpness (Clear lens input) | Two foggy midday archive frames 2.4 and 2.7; seven other midday hires 31.4–2,073.5; night frames 1,031–1,638 | Laplacian variance at ≤ 640 px on stored frames |
| Test suite | 191 passed (capture 0.2.1, the working tree), plus a 3-day schedule test that runs only with `-m slow` (1 passed, 53 s); ruff check and format clean. At the commit of 0.2.0 (`df1838a`): 182 passed. | `make test`, 10:31 on 20 Nov; the capture fixer's run for the slow test |

### The NPS archive

| What | Value | How and when |
|---|---|---|
| Archive check requests | **217** sent: 7 `GetAvailableDays`, 99 searches (2 of them paging probes, both HTTP 500), 92 thumbnails, 18 hires and 1 page load (the Rainier webcams page, 66,378 B); 5,910,413 bytes; 10 errors (7 TLS timeouts, 1 disconnect and the 2 HTTP 500s); minimum gap 5.106 s start to start; budget 218 of 220. The summary JSON's 203 misses a 14-request first Grand Canyon attempt. | `archive_request` events in `capture_log/2026-11-19.jsonl`, 08:46–10:41 |
| Camera epochs | All 7 ARD cameras went from a 3:2 frame (thumbnail 99×66, hires 999×666) to 16:9 (100×56, 999×562). Switch windows: pore 2023-01-16 → 2023-07-15; grca 2024-01-15 → 07-15; grte 2024-07-15 → 2025-01-15; yose, acad and bibe 2023-07-15 → 2024-01-15; grsm 2024-01-15 → 07-15. | Thumbnail sizes on 13 sampled midday frames per camera, checked on contact sheets |
| Grand Canyon re-aim inside the 3:2 epoch | The 2024-01-15 hires is shifted 51 px right and 30 px up from the 2021-01-15 one (5.1 % and 4.5 % of the frame), at the same scale: +48.9 to +52.0 px and −29.1 to −30.5 px over 5 windows × 3 analysis widths (D-042). The archive check's text now says so. | Full-frame registration in the backtest pair |
| Archive resolution | 999 px wide only, not 4K; no EXIF time in any of the 17 stored hires | Downloaded hires sidecars |
| Archive time stamps | On 90 sampled days, NPS's `SolarElevationAngle` matches a NOAA ephemeris at our parsed UTC: mean 0.015–0.065° a day, best clock offset 0 min. In summer, `ImageCreateDateTime` is local standard time. | `convention_check` with the Sun ≥ 5°, offsets −120 to +120 min |
| NPS's Sun angle is refracted | Sun up (39 assets): NPS − our refracted altitude max 0.0026°, RMS 0.0015°; against the geometric altitude max 0.4768°. Over all 88, NPS applies Sæmundsson refraction at every altitude, even below the horizon (max residual 0.0035°, a −0.73 s time offset). | `lookout-eval ephem-check` on `search_pore_20261117.json` |
| Archive depth | Days available in 2024 and 2025, and days since HRRRv4 (of 2,179): pore 360/357 (1,983); grca 320/359 (1,997); grte 358/359 (2,075); yose 366/350 (1,898); acad 366/359 (2,106); grsm 366/358 (2,165); bibe 335/332 (2,045) | `GetAvailableDays` bitmasks |
| Cadence | Median gap 15 min on every sampled day; partial days down to 22 frames | Search results of 13 dates per camera |
| dHash as an epoch signal | No separation: weather moves midday dHash by up to 43 bits between days | Hamming distance on thumbnails before the guard |
| The 3:2 → 16:9 switch, dated by T0 (D-052) | Last 3:2 frame (666 px tall) → first 16:9 frame (562 px), by local date: Point Reyes 27 Mar → 10 Apr 2023; Acadia 12 → 26 Aug 2023; Yosemite 22 Aug → 5 Sep 2023; Big Bend 2 → 16 Dec 2023; the Smokies 11 → 25 Mar 2024; Grand Canyon 24 Apr → 20 May 2024, with no frames between. Each camera switched once, with no return to 3:2. Grand Teton was not queued. | `original_height` in the 390 `hires` sidecars under `archive/` (the newest dated 15 Jan 2026), read at 10:46 on 20 Nov (the 390th was fetched at 10:46:24); no image opened. Re-read at 11:51 over all 831 `hires` sidecars then on disk: the same dates, one change per camera. |
| Archive queue, first tranche (T0 + T1a, 447 entries) | Queued 09:50:30–09:50:37; first request 09:52:25. At 10:39: 146 done, 1 active, 300 pending. T0: 134 of 134 entries, 256 requests, 134 frames, 8.6 MB of frames (the plan: 256 requests and 7 MB). T1a: 13 entries started, 198 requests, 184 frames. 454 of the day's 1,000 requests: 15,994,126 bytes of frames and 6,705,492 of search answers, none refused. One error: a TLS connect timeout at 10:37:10. To 10:30, 129 searches and 238 `hires` GETs, all answered 200. **At 11:43:23 the day's 1,000 requests were spent,** 1.85 h after the first: 990 answered 200, and 10 failed in transit (6 `hires` connect timeouts, 4 searches), each followed by a wait of 30 s or more; 52,709,139 bytes, 44,196,432 of them frames. After three failed searches, Big Bend's 2025-06-14 entry was marked failed at 11:30:05. At 11:47 the queue held 180 done, 1 active, 1 failed and 265 pending, waiting for the next UTC day's budget. | `lookout-capture archive queue list --json` (offline; 10:39 and 11:47) and `archive_request`, `error` and `queue_failed` records in `capture_log/2026-11-20.jsonl` |
| Backfill time model | 1.76 h of loop time to spend 1,000 requests without errors (49.5 requests per idle window), 2.10–2.11 h at 19 Nov's 3.7 % error rate; no two requests closer than 5.0 s end to start | `loop_sim.py`: capture's real `QueueRunner` on a fake clock against a mock nps.gov, 20 seeds per scenario (the backfill plan, section 6) |
| Archive request sizes | `hires` 54,141 bytes on average (n = 9, 10,419–101,326) before the queue; a day's search answer 49,372 bytes (n = 92); request time median 215 ms | Pre-guard sidecars and the 19 Nov capture log (the backfill plan, section 6) |

### Forecast

| What | Value | How and when |
|---|---|---|
| Publication lag, live | Run 2026111908: f00 53.33 min … f18 86.37 min. Run 2026111910: f00 51.33, f04 70.57 (after f05's 62.68), f18 85.63. Hours are sometimes published out of order (07Z had f09 before f08). | S3 `LastModified` (the later of GRIB2 and `.idx`) minus run start |
| Publication lag survey | 124 runs, 14 Nov 06Z – 19 Nov 09Z: 2,986 hour-lags; 37.3 % later than the forecast.md curve; per-hour p95 up to 25 min above it (f11 97.1 against 72.2 min); highest p95 100.85 min (f00–f18) and 116.65 min (f48); 3 lags above 120 min (0.10 %, max 131.17). All five 23Z runs published f06–f18 at about 111 min. | `lookout-forecast lag-survey`: one listing per run, 2,018,062 bytes, no GRIB |
| Publication lag survey, logged (forecast 0.2.0, D-053) | 194 runs, 2026111200 to 2026112001, 0 incomplete; 4,676 listed hour-lags; 4,082 survey events logged; the 594 hours already logged live all matched (0.0 s). Median / p95 / max: f00 51.36 / 60.52 / 63.28 min; f06 62.77 / 74.45 / 115.92; f12 73.91 / 86.22 / 131.17; f18 85.24 / 98.15 / 115.68; f48 (n = 33) 107.57 / 116.65 / 117.48. 3 lags above 120 min; 1,847 later than the forecast.md curve. 194 listings, 3,160,172 bytes, by the survey's own ledger, no GRIB, 53 s. The pages it saved number 197 (3,181,611 bytes): first it listed 2026112003, 2026112002 and 2026112001 to find the newest complete run, which it listed again at the end, and its ledger leaves those three out. | `lookout-forecast lag-survey --log --save-listings`, 03:11:48–03:12:41 on 20 Nov; replayed offline with the final code, identical events; the saved pages counted at 11:40 |
| The lag model after the survey | 4,700 lags of 198 runs: 618 live, 4,082 survey, 160 live duplicates set aside, 0 invalid. 576 of 576 cells are off the fallback, all by rule 2 (436 on live lags alone); no cell has the 20 lags rule 1 needs (8–9 each). | `lookout-forecast status --offline --json`, 06:11 on 20 Nov |
| Archived runs against their own S3 stamps (D-054) | 18 runs, 2015-07 to 2026-03. 14 keep both stamps (686 hours), 2 keep only GRIB2 stamps (98 hours) and 2 keep neither (53 hours). Without the floor, 49 of the 686 hours were published after the model's time, by up to 6.37 min (2021031512 f32); the live-only model gives 3. With the floor, 0 are, and 91 hours take their stamp. | `lag-check` listings (442,353 bytes, no GRIB) replayed through the final code against the live + survey log |
| S3 stamps as publication times | Published stamps fall 46.6–131.2 min after the run start (4,688 hours of 195 November runs) and 49.9–116.5 min in 14 archived runs. Rewritten stamps are 33.6 days to 5.6 years late. The `.idx` came 3–188 s after its GRIB2 (median 4 s, 99th percentile 9 s) in 5,374 hours, never before it. | Stamps parsed from every saved listing |
| Live-log censoring | In the 13 first live extractions made within 80 min of their run start, 48 of 268 f03+ hours were listed and logged; the stand-in logs later hours only when they were published early | First `run` event per live run in `forecast_log/`, read-only |
| Local stand-in | All 34 hourly runs from 2026111900 to 2026112009 written at all 8 viewpoints. 19 Nov: the 7-field f00–f02 profile, 20.8–22.3 MB of GRIB for each of the 22 runs the stand-in fetched and for the builder's 10Z smoke run; the builder's 08Z smoke run fetched 25.4 MB (12 GRIB requests); first tick 12:18. 20 Nov from 02:39:49: 8 fields (D-050). A one-time re-extraction of 12 runs (2026111914 to 2026112001) took 293.5 MB, then 8 new runs 23.8–25.1 MB each. 419 distinct live lags in the data report's window to 10:30. | `forecast_log/` `run` and `lag` events; `lookout-eval data-report` |
| Forecast files on disk | 241 of 241 validate against 0.2.0's schema and floor checks (240 live, 1 archived; all written by 0.1.0); at 11:45, 281 of 281 (280 live). A live file written by 0.2.0 has 0 errors under the 0.1.0 schema; an archived one has 2, or 4 when the stamps win. | The forecast fixer's read-only checks, 06:11 on 20 Nov; re-run at 11:45 (`validate_document` over every file, and the fixer's fake-bucket schema check) |
| Live smoke, final code | Run 2026111910, f00–f02, 7 fields, 8 viewpoints: 21,440,626 bytes (10 range requests, 3 `.idx`, 1 listing), 3.46 s wall; 8 valid files; no GRIB left behind | `tests/test_live.py`, 12:01 |
| Whole-run sizes, all 22 fields | 453.0 MB for an 18 h run, 1,202.1 MB for a 48 h run, with terrain fetched once | `lookout-forecast plan` (inventories only) |
| Grid mapping | Max error 1.30e-11 km against ecCodes' own coordinates over 1,905,141 cells; every cell maps back to itself | `lookout-forecast grid-check 2026111907` |
| Datums and the phantom cloud base | See D-026: ceiling and top never below the model terrain; HPBL above ground; 12.4 % phantom `HGT:cloud base` on 07Z f00 (19.8 % on 09Z f03, in review) | `lookout-forecast datum-check 2026111907 --fh 0` |
| Model terrain at the camera cells | Against the manifest elevations: Rainier +13.9 m, Grand Teton +107 m, Acadia −35 m, Big Bend −90 m, Smokies −174 m, Yosemite −182 m, Grand Canyon −920 m (the cell is over the canyon), Point Reyes −150 m (the cell is sea) | `datum-check` camera cells |
| Herbie trial | 4 requests for one field-hour, all as `python-requests/2.34.2`, two with no timeout; byte range identical to ours | Logged trial, 19 Nov (D-026) |
| NOAA download, builder | About 132.5 MB in all on 19 Nov | The forecast builder's transfer ledgers, in its first-workflow report |
| NOAA download, 20 Nov to 10:39 | Stand-in: 530.4 MB of GRIB in 22 run events (41.5 MB for 2 runs on 7 fields, 293.5 MB for the re-extraction, 195.4 MB for 8 new runs). Forecast package: the survey's 197 listing pages (3,181,611 bytes; its ledger counts 194, 3,160,172 bytes, D-053) and 10 more listings of archived runs (221,729 bytes), no GRIB. Lambda smoke run 3: 7.25 MB (D-059). | `forecast_log/2026-11-20.jsonl` `run` events; the forecast and infra reports |
| Test suite | 177 passed, 1 network test deselected (forecast 0.2.0); ruff check, ruff format --check and `uv lock --check` clean; eval's pair contract 13 of 13 in forecast's environment. Before (00:47): 135 passed, 1 failed. | `make test`, 10:31 on 20 Nov, and the forecast fixer's run |

### Evaluation and the backtest pair

| What | Value | How and when |
|---|---|---|
| Backtest pair, v−1 | Grand Canyon, valid 2024-01-15 19:45, 24 h lead, HRRR run 2024011412 f31–f32. Primary box: truth 0.330 (hidden), render 0.504 (partial): a correct negative, by accident (both v−1 errors push towards "not visible"). With the measured 5 % re-aim applied, the truth box reads 0.360, across τ_lo (D-042). 2.61 s a pair, 2.15 s of it the render; 4 runs, byte-identical scene and render. Since run 4 the record says `counts_toward_metrics` false, as for every v−1 pair. Committed in `d9aa416`. | [docs/evidence/m05-backtest-pair.md](docs/evidence/m05-backtest-pair.md), 23:13–23:35 on 19 Nov and 00:16 on 20 Nov |
| Held-out split | Version 1, SHA-256 `c437544e…`; on the real catalogue 0 of 85 archive records and 0 of 105 live frames are held out; 47 of 48 mutants caught | `measure_real.py` (metadata only), 22:45 (D-038) |
| Guard costs | +1.8 µs a record to build a `FrameStore` (2.24 against 2.06 s for 100,000 records); +20.4 µs a frame for the catalogue's quarantine checks (1.74 against 1.33 s for 20,000) | A/B runs in one process, alternating |
| Real render on an eval scene | 573, 586 and 669 ms on SwiftShader (3 of 3 exit 0) | `LOOKOUT_EVAL_RENDER=1 pytest tests/test_renderer_contract.py` |
| Renderer `validateScene()` under Node | 56 ms a call (median of 10, warm) | `renderer_validate()` |
| Extractor v−1 | 21.1 ms for 1920×1080 and 37.7 ms for 3840×2160 from the JPEG; 8 px blur gives ratios 0.45 and 0.12; no camera-fault false positive on 3 night samples | `edge_stats`, median of 10 |
| PNG against JPEG (corrects "within 0.5 %") | The ratio moves by up to 1.19 % at q50, 0.91 % at q75, 0.55 % at q90 and 0.52 % at q95 | 15 procedural images against a 5-frame reference |
| Bootstrap (corrects "8 ms") | 10,000 resamples for 31 days × 8 pairs in **2.3 ms**; coverage of nominal 95 % intervals 196 of 200 | `paired_delta_pss`, 15 warm runs |
| Interval against p-value | The old interpolated interval disagreed with "p < 0.05" in 276 of 6,000 cases; the order-statistic interval in 0 | 3,000 seeds × 2 values of B |
| oculus-core against NOAA | 0.0080° on the 88 NPS times; max 0.0128° in altitude over 2,000 random times and places | `noaa_solar.py` |
| The M0.5 data report (D-061), window 12:24:23 on 19 Nov → 10:30 on 20 Nov (22.09 h) | **Clock** (PLAN's rule, with the report's 540 s threshold): 10.21 h lost, 11.89 h covered, earliest close 22:36:51.258 UTC on Sun 22 Nov. Both jobs (proposed): 10.43 h lost, close 22:50:09.401 UTC. Forecast alone: 6.04 h lost. **Capture:** 137 passes (69 on 0.1.0, 68 on 0.2.0), median spacing 315.3 s; 136 polls and 48–50 image GETs per camera, at most 4 GETs in any rolling hour. **Forecast:** 24 runs first written in the window per viewpoint, 2 of them catch-ups, plus 12 re-extractions each; 0 errors. | `lookout-eval data-report --until 2026-11-20T10:30:00Z` (read-only, 0.2–0.6 s) |
| The data report, checked | At the 06:33:54 cut, an independent standard-library script gave all three clocks to the millisecond. 14 of 14 mutants of the counting rules were killed, again in a re-run at 11:43 on a scratch copy of the working tree (baseline 53 tests passed). On the eval builder's window, to 03:25:40, PLAN's clock read 8.217 h lost and a close at 20:37:25 UTC on Sun 22 Nov, as PLAN and the critic had it before the 20 Nov outages. | The eval fixer's `xcheck2.py` (`python3 -I`, no lookout code) and `mutate.py` |
| oculus-core provenance (D-062) | The four pair records say `0.1.0+06809e6`, but the build that ran was `1b5410b`. Its `direct_url.json` names that commit; it was built at 12:31:59 on 19 Nov and was in `eval/.venv` by 22:08 at the latest, both before run 1 (23:25). Later records name the installed build (`0.1.0+1b5410b`). | `stat`, `shasum` and `json.load` on the venv, the uv cache and the records (read-only) |
| Test suite | 367 passed, 1 skipped (the opt-in render), 18.6 s; the pair's real-extractor test now runs, with ecCodes in eval's dev group. Data-report tests: 40. Before: 318 passed, 2 skipped (01:46); at the held-out fix (D-038), 262 passed, 1 skipped. | `make test`, 10:31 on 20 Nov (dead proxies, `PYTHONPATH=src:../workers/common/src`) |

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
| Infra checks | All pass: static 6, root `terraform test` 50, bootstrap 4, the offline provider path 22, scripts 26, cost 5 (113 in all), plus fmt, shellcheck and the build-context check; 22.5 s. The whole `infra/test.sh` also passed under a sandbox that denied outbound connections. 41 targeted mutations caught: 24 in the IAM policies, 17 in the cost, no-email and tflint checks. Before (19 Nov): 82 passed, 23 of 23 mutations caught. The 19 Nov stall of `test_scripts.sh` (10–13 min in 2 of 5 runs) was not seen in the runs of 20 Nov; its cause is still not recorded. | `infra/test.sh` with a mocked provider, 20 Nov (the infra fixer's final run; `make test` at 10:31 too) |
| Hosting estimate, 20 Nov (computed, not measured; D-060) | For the committed capture 0.2.0 (`8246a8b`), with the free tiers: $2.07–4.03 in month 1, $2.32–4.29 in month 2, $2.53–4.52 in month 3, then about $0.22 more each month. Without them: $5.63–9.83 in month 1. S3 requests $1.87–1.92; Lambda compute up to $1.81. Capture 0.2.1: $2.07–2.81 in month 1. D-035's $1.60–2.81 is superseded. | `infra/cost/estimate.py` on `cost/sim_capture_8246a8b.json`: 24 h of the committed capture code against a mock S3 bucket (moto) and fake feeds; AWS list prices of 12–18 Nov |
| Capture's simulated S3 traffic and waiting | One pass makes 50–216 S3 requests, 73 on average; per day 8,944 PUT, 174 LIST, 7,902 GET, 702 HEAD and 3,420 DELETE. A pass waits 35.7 s on average before its own work (32.8 s for slots, 2.9 s spacing), from 0.2.0's 1 s poll margin; 0.2.1 waits 14.6 s. | The same simulation (`infra/README.md`, "Monthly cost estimate") |
| Lambda image builds | Cold build 20.0 s; rebuild after AWS republished the base 15.8 s; rebuild with nothing changed 1.1 s; rebuild of `8246a8b` 12.2 s (tag `8246a8bd570b`). 293.8 MB compressed (the pinned AWS base 200.3 MB); 1.25 GB unpacked. Both base images are pinned by digest. | `infra/lambda/build.sh`, Docker Desktop 29.5.3, BuildKit 0.30.0, native linux/arm64, 20 Nov |
| Lambda smoke runs (forecast handler under the Runtime Interface Emulator, 2048 MB, 1.16 vCPU, tiny profile) | Run 1 (00:17): init 267 ms, first call 1,278 ms (3,588,572 bytes from NOAA, 11 files), second call 187 ms, peak 137.8 MiB. Run 2 (00:26): 299 ms, 1,222 ms, 209 ms, 138.4 MiB. Run 3 (06:59, image of `8246a8b`): 232 ms, 2,696 ms for 2 runs (7,254,750 bytes, 20 files), 38 ms, 144.8 MiB. No GRIB left behind. Laptop numbers, not AWS. | `infra/lambda/smoke.sh` (`INIT REPORT`, `INVOKE RTDONE`, the cgroup's `memory.peak`); `infra/README.md`, "The Lambda image" |
| EventBridge Scheduler free tier (us-east-1) | "$0 for the first 14 million scheduled invocations" | AWS Price List file published 18 Nov 2026 (D-014) |

### lookout-common and the docs checks

| What | Value | How and when |
|---|---|---|
| Limiter across processes with `LOOKOUT_STORAGE=s3` | Minimum gap between request starts: 23 µs before the fix (in-process mode), 1.014 s at a 1.0 s interval after (file mode) | 2 spawned processes × 2 `hold()` each |
| Torn JSONL line | Before: the next append glued onto the fragment and every later read failed. After: both backends read every record, and the fragment is reported in `bad_lines`. | Scratch scripts on a temp file and on a moto S3 bucket |
| Test suite | 258 passed; ruff check and format clean; 8 of 8 manifests valid | `uv run pytest -q` in `workers/common`; again in `make test` at 10:31 on 20 Nov |
| Docs checks | 16 invariant checks, 85 tests, all passing; `notices`, `appendix-a` and `ci-suites` are the three added on 20 Nov (D-063). Before: 13 checks, 45 tests. | `make test` (`test-docs`), 10:31 on 20 Nov; `python3 -I -B docs/checks/doccheck.py` after this update |

### M1 preparation

| What | Value | How and when |
|---|---|---|
| Spike unit tests | 105 passed; clippy `-D warnings` clean with and without `--features speech`; four release binaries build (`m1-spike` 12,591,144 B) | `cargo test --workspace` in `spikes/m1-platform` |
| Rust `Instant` is `CLOCK_UPTIME_RAW` | 3 of 3 runs: offset −12 to −84 µs, drift +1 to +146 ns over ~150 ms | `instant_is_the_host_clock_on_this_machine` |
| `nettop` output when piped | Held back: all 4 headers arrived together at 3.0 s; under `script` they came each second | Timestamped header lines only |
| Torn pixel-tag strips | 0 of 29,930 consecutive-tag cuts decode as a third tag | Exhaustive unit test |
| The 11 platform checks | **none run yet** (they need the owner at the keyboard) | — |
| Speech library 0.2.0: tests and build (D-055) | 103 passed (76 before the review), 0 failed; Swift warnings are errors in every target; ShellCheck clean. `liblookout_speech.a` 907,616 bytes, 10 exported `_lk_speech_*` symbols, identical to 0.1.0's; the same 58 autolink entries, and 400 imports, 3 fewer than 0.1.0. A C host links it with the minimal flags and runs. | `scripts/build.sh` (swift test, release build, symbol check and its self-test, link check), 20 Nov; again in `make test` at 10:31 |
| Speech library: timing on a mock engine | `lk_speech_push_f32` (480 frames): p50 0.21–0.88 µs, p99 2.71–5.12 µs, 0 of 30,000 calls over 1 ms across six runs. `lk_speech_start` at 48 kHz: p50 0.737–0.963 ms. `lk_speech_cancel` waits 1.26–1.73 ms during a 19 s backlog, and 5.2–13.5 ms during a silence pad at 10 ms per buffer, against about 200 ms without the pad's cancel check. | `PushLatencyTests`, `StartLatencyTests`, `CancelEdgeTests`, host clock (CLOCK_UPTIME_RAW), load average 2–5 |
| Speech library: lifetime and privacy checks | 0 callbacks after cancel returned in 1,800 random-timing sessions, and 0 audio fed after it in 1,200. Memory after 150 full sessions and after 500 start-and-free cycles: −4.2 to +4.0 MB, against 4 MB for one leaked ring. Every converter input copy zeroed (94 of 94 chunks), with output bit-identical to plain AVAudioConverter. The symbol check rejects all 20 crafted archives. | `CancelTests`, `CancelEdgeTests`, `LeakTests`, `UnitTests`, `scripts/check-symbols-selftest.sh` |
| Speech library: the real recognizer | **never run** (it needs the owner: the model install, and a possible permission prompt) | — |
| Replay script (D-057) | 130 rows (30 dev, 100 test), 77 of 77 aliases said, script hash `3b4256f1cf51`. Test outcomes: 88 query, 6 miss, 2 fallback, 2 past the horizon, 2 beyond it. Six rows are owner-pending. 40 bench tests pass. | `python3 -I -B bench/utterances/check.py --no-parser` and the bench tests, 20 Nov; again in `make test` at 10:31 |
| The parser against the script's reading (text only, not the PRD §14 replay metric) | Test split: 94 of 100 exact, 97 counting soft differences (94 of 97 and 96 of 97 without the 3 owner-pending test rows); all rows: 121 yes, 4 soft, 5 no. Solar times within 1 s of an independent Meeus Sun (28 rows, mean 0.25 s); clock-based times identical on 86 of 86 rows. | `check.py --write` on `lookout-parse` from `8246a8b`'s lookout-core, 05:40 on 20 Nov |

### Test suites, all packages

| Suite | Result | How and when |
|---|---|---|
| Full `make test` | **All 10 suites passed, 224 s:** common 9 s, capture 65 s, forecast 21 s, eval 19 s, renderer 39 s, rust 7 s, speech 37 s, infra 23 s, docs 3 s, bench 1 s. On 19 Nov (8 suites, 230 s), forecast failed. | The integration agent's run on the working tree, 10:30:57–10:34:41 on 20 Nov, exit 0 |
| `make test` with no network | All 10 suites passed (223 s) under a macOS sandbox that denied every outbound IP connection except loopback, and DNS | The integration agent, 20 Nov (the Makefile's header) |
| Per package, latest | common 258; capture 191; forecast 177 (1 network test deselected); eval 367 and 1 skip; renderer 183; core 108; speech 103; infra 113; docs 85; bench 40; spike 105 (not in `make test`) | `make test`, 10:31 on 20 Nov; the spike's own run on 19 Nov |
| CI workflow lint | 0 findings | actionlint 1.7.12, 19 Nov |

### Machine and environment

| What | Value | How and when |
|---|---|---|
| iCloud | `~/Documents` has the iCloud Drive file-provider xattr; `~/lookout-data` has none | `xattr -l`, 19 Nov (D-017) |
| Hidden venv files | 19 Nov: 8,393 in `workers/common/.venv`; 6,173 in `workers/forecast/.venv` (173 at build time). 01:48 on 20 Nov: common 8,393, capture 8,449, forecast 7,549, eval 5,197. | `find <venv> -flags +hidden \| wc -l` (D-031); the 6,173 is the eval fixer's count |
| Battery and power | 19 Nov: on battery from 06:13 to 22:48; the last reading on battery was 11 % at 22:25, and it read 3 % at 22:48, when AC power came on. 20 Nov: on AC until 03:29:09, then on battery until 06:27:36 (100 % down to 85 %). The lid closed at 03:52:37 (clamshell sleep, a full wake at 03:56:06), and the Mac idle-slept from 04:08:22, with dark wakes. On AC since 06:27:36; 100 %, charged, at 10:38 (D-051). | `pmset -g log` and `pmset -g batt`, read at 10:40 on 20 Nov |
| Sleep assertions | The capture loop's `caffeinate -s` (on behalf of the snapshot's `lookout-capture`), which holds only on AC. Also a `caffeinate -dims -t 28800` since 06:27:54 on 20 Nov, which holds the display and the system awake until 14:27:54. | `pmset -g assertions` and `ps`, 10:45 on 20 Nov |
| Power settings | Sleep 1 min and display sleep 10 min, on AC and on battery; Low Power Mode off. The Battery pane calls the setting "Energy Mode" (Automatic or Low Power). | `pmset -g custom`, read by the M1 docs package at about 04:00 on 20 Nov |
| Disk | 22 GiB free of 460 GiB at 10:38 on 20 Nov (95 % used). Earlier: 28 GiB at 00:50, 21 GiB at 04:03 and 23 GiB at 06:11 on 20 Nov; 44 GiB at about 12:00 on 19 Nov (the capture fixer's reading). | `df -g` |

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
