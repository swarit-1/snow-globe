# PRD_PROGRESS.md: tickets, owner actions and measured numbers

This file tracks progress per ticket against [PRD.md](PRD.md) v0.4 and [PLAN.md](PLAN.md), with every measured number (PRD §16.1). Decisions and the spend ledger are in [DECISIONS.md](DECISIONS.md).

**Updated:** Thu 19 Nov 2026, 08:30 UTC (day G). M0 is done, and M0.5 is in progress (day 1 of 4).

**Running overrun before the 14 Dec freeze:** 0 days, of the 4 allowed across M0.5, M1 and M1.5 (PLAN.md, calendar notes).

**Status values:**
- **done:** its evidence exists.
- **in progress:** being built, but the evidence doesn't exist yet. Written code with no evidence counts here, not as done.
- **blocked on owner:** waiting on one of the owner actions below.
- **not started.**

---

## Owner actions

Most urgent first.

1. **AWS.**
   - Confirm the account to use.
   - Provide credentials through the environment.
   - Confirm the budget-alarm email subscription. Terraform creates the alarm before anything else, and nothing is applied without your word (D-014).
   - **Why it's urgent:**
     - Until AWS is live, capture runs only while this laptop is awake.
     - A Rainier gap is lost for good, because that camera has no archive. The laptop slept for 50 of the stopgap's first 120 minutes.
     - AWS must be live before the sealed window opens on Tue 15 Dec.
2. **NPS confirmation emails.**
   - The drafts are ready in [docs/outreach/nps-confirmation.md](docs/outreach/nps-confirmation.md), for the NPS Air Resources Division and Mount Rainier NP.
   - Golden Gate, if you want it: [docs/outreach/golden-gate.md](docs/outreach/golden-gate.md), by **Fri 4 Dec**.
3. **Permission to push.**
   - The clone's `origin` is `swarit-1/snow-globe`, and nothing has been pushed.
   - The CI workflow is written (`.github/workflows/ci.yml`). It runs only after the first push.
4. **Deepgram and fal: accounts and keys, when you're ready.**
   - Keys go in the server's environment, never in the app.
   - Approved (D-012): Deepgram for the bake-off, and fal for the ≤ $1 kill test only.
   - Even with a key, you're asked before the first paid call to each vendor (PRD §17). Any other fal spend needs your approval first (PLAN.md, Rules).
   - Deepgram is needed for the M1 bake-off (from 23 Nov), and fal for the M3 kill test (from 27 Dec).
5. **Permissions in M1** (from Mon 23 Nov):
   - Microphone and Speech Recognition, when the app first asks;
   - Screen Recording and Input Monitoring, for the harness only;
   - Apple Intelligence on.
6. **Replay recordings in M1:** ~20 minutes of scripted utterances, plus ≥ 30 real-use ones.
7. **Which renderer P1 scores** (D-019), by **Mon 14 Dec**. The options:
   - (a) the renderer at the freeze commit;
   - (b) the M4 renderer, improved on development data only, with frames after 14 Dec kept out of Tier 0, the live view and labelling until M4 scores.

   The work up to 14 Dec is the same either way.

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
| M0-4 PRD v0.4 | done | [PRD.md](PRD.md) (`ab96af0`) | — |
| M0-5 oculus-core ephemeris against JPL Horizons | done | DECISIONS.md D-002 | Sun 0.19′, Moon 0.13′, worst case |
| M0-6 **Gate:** the owner's PRD §1 calls | done | DECISIONS.md D-012 | 15 of 15 defaults accepted |

### M0.5: Start the clock (Thu 19 → Mon 23 Nov) · in progress

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M0.5-01 Bring PLAN.md and DECISIONS.md to v0.4 (D-012 to D-021); start this file | done; awaiting the lead's commit | [PLAN.md](PLAN.md), [DECISIONS.md](DECISIONS.md), this file | — |
| M0.5-02 Data directory outside iCloud | done | DECISIONS.md D-017 | see "Measured so far" |
| M0.5-03 Scout the 8 NPS feeds; fix the viewpoint IDs | done | DECISIONS.md D-020; `~/lookout-data/samples/nps-json/` | 8 of 8 feeds live |
| M0.5-04 NPS archive API and depth | done | DECISIONS.md D-018 | start dates below |
| M0.5-05 Email drafts: NPS, Golden Gate | done | [docs/outreach/](docs/outreach/) (`e4722a6`) | — |
| M0.5-06 Stopgap Rainier capture | in progress: running while the laptop is awake. Its cap isn't persisted yet (D-021). | DECISIONS.md D-021; `~/lookout-data/capture_log/2026-11-19.jsonl` | 8 frames stored as of 08:30 UTC; 2 outages totalling 50 min, no frame lost |
| M0.5-07 Repo scaffold: uv, pnpm and Cargo projects, viewpoint schema and manifests, per CLAUDE.md | in progress | `workers/common/` and the other CLAUDE.md paths (evidence pending) | — |
| M0.5-08 Capture worker, 8 cameras.<br>• Polite: fetch on change, ≤ 4 per hour with persisted state.<br>• Quality gates (PRD §8.6), stored at ≤ 1920 px.<br>• Rainier cadence measured. | in progress | `workers/capture/` (evidence pending) | — |
| M0.5-09 Forecast worker.<br>• HRRR byte ranges at the view-ray sample points.<br>• One JSON per viewpoint per run.<br>• Lag per forecast hour. | in progress | `workers/forecast/` (evidence pending) | — |
| M0.5-10 Trace skeleton | in progress | `app/core/` (evidence pending) | — |
| M0.5-11 Infra code: Terraform (state backend, storage, CDN, budget alarm, Lambda + EventBridge Scheduler) and the Lambda image | in progress, code only | `infra/terraform/`, `infra/lambda/` (evidence pending) | — |
| M0.5-12 AWS deployment | blocked on owner (owner action 1) | DECISIONS.md D-014 | — |
| M0.5-13 CI (GitHub Actions, CI only) | in progress: the workflow is written. Its first GitHub run needs a push (owner action 3). | `.github/workflows/ci.yml` (evidence pending) | — |
| M0.5-14 One backtest pair: an archived frame and an archived HRRR run → renderer v−1 → a crude extractor score, labelled "v−1" | in progress | `renderer/`, `eval/` (evidence pending) | — |
| M0.5-15 NPS archive check: cadence, gaps and camera epochs per camera | in progress. It reads metadata for any date, but views archived frames only from before 1 Jun 2026 (D-019). | `docs/research/nps-archive-check.md` (being written) | — |
| M0.5-16 H or P per viewpoint in DECISIONS.md | in progress: Rainier P is decided; the H parts wait on M0.5-15 | DECISIONS.md D-019 | — |
| M0.5-17 Emails sent | blocked on owner (owner action 2) | — | — |
| M0.5-18 **Gate:** ≥ 72 h of worker and capture logs | in progress | `~/lookout-data/capture_log/`, `forecast_log/` | Rainier stopgap only, from 06:30 UTC 19 Nov; 2 outages so far (50 min) |
| M0.5-19 A council round on the parts taken from Lookout, adding a forecast-verification judge | not started (optional) | PRD §18.2 | — |

### M1: Voice to first pixel (Mon 23 Nov → Mon 7 Dec) · not started, with some core logic started early

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M1-01 Platform spike: one day, 11 checks (PLAN.md 1a) | not started | — | — |
| M1-02 `lookout-core`: query lifecycle, `query_gen` cancellation, trace | in progress (early) | `app/core/` (evidence pending) | — |
| M1-03 Parser fast path and time rules.<br>• ≥ 150 table-driven cases and property tests.<br>• DST and cross-zone cases. | in progress (early) | `app/core/` (evidence pending) | — |
| M1-04 Ephemeris wrapper and solar events over oculus-core `06809e6` | in progress (early) | `app/core/` (evidence pending) | — |
| M1-05 Speech A1: `DictationTranscriber` through the Swift static library | not started | — | — |
| M1-06 Speech D: Deepgram Nova-3, bake-off only | blocked on owner (owner action 4) | DECISIONS.md D-015 | — |
| M1-07 Overlay with Tier 0: states, caption, chips, destination chip, focus model, paint gate | not started | — | — |
| M1-08 Tier 0: library index, Sun-position match, background decode | not started | — | — |
| M1-09 Replay and paint-capture harness | not started | — | — |
| M1-10 Replay recordings: ~130 scripted, plus ≥ 30 real-use | not started (owner, in M1) | — | — |
| M1-11 macOS permissions | not started (owner, in M1) | — | — |
| M1-12 Overnight idle trials: ≥ 100 for the shipped engine, ≥ 60 for the comparison engine | not started | — | — |
| M1-13 Stale-paint stress test, ≥ 10,000 cycles | not started | — | — |
| M1-14 README skeleton with blanks | not started | — | — |
| M1-15 **Gate:**<br>• replay report;<br>• ≥ 150 parser cases;<br>• STT decision;<br>• 0 stale paints;<br>• a screen recording over another app. | not started | — | — |

### M1.5: Walking skeleton (by Thu 10 Dec) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M1.5-01 Renderer v0, headless: Hillaire sky, cloud slabs, ceiling slab, flat fog, camera v0 | not started (v−1 is M0.5-14) | — | — |
| M1.5-02 First Rainier plate: base frame, pose, summit band. Needs a captured midday frame with the summit visible. | not started | — | — |
| M1.5-03 Extractor v0 | not started | — | — |
| M1.5-04 **Gate:** dev-only P1 table against persistence, climatology and S_rule | not started | — | — |
| M1.5-05 **Gate:** cloud Monte Carlo test | not started | — | — |

### M2: Relit frame (Thu 10 → Sun 27 Dec) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M2-01 **Pre-registration freeze, by Mon 14 Dec under design P.**<br>• τ, S_mos, analog weights, block length, MDE, K2 mapping.<br>• Commit hash.<br>• Which renderer P1 scores (owner action 7). | not started | DECISIONS.md D-011, D-019 | — |
| M2-02 Ephemeris validation: refraction, camera transform, Horizons per viewpoint, Skyfield ×1,000, NOAA ±1 min | not started | — | apparent position so far: Sun 0.19′, Moon 0.13′ (D-002) |
| M2-03 Plates for Rainier and Point Reyes; pose report | not started | — | — |
| M2-04 Haze profiles and camera response | not started | — | — |
| M2-05 Tier 1 in the overlay and scrub view; Tier 1 p95 idle, with pre-render contention | not started | — | — |
| M2-06 Goldens in CI on SwiftShader; WKWebView tolerance | not started | — | — |
| M2-07 Datums pinned | not started | — | — |
| M2-08 Feature admission on dev-check evidence | not started | — | — |
| M2-09 API on Lambda; graded asks begin | not started; needs AWS (owner action 1) | — | — |

### M3: Photographic frame (Sun 27 Dec → Wed 6 Jan) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M3-01 Tier 2 kill test, ≤ $1 on fal | not started. It needs a fal key, and you're asked before the first call (owner action 4). | — | — |
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
| M5-01 README with the numbers, 60-second demo, `THIRD_PARTY_NOTICES`, and `make score` from a fresh clone | not started | — | — |

### v1 growth (after M5) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| V1-01 Viewpoints 3–8, each with plate, pose, labels, goldens and ≥ 14 days of live data | not started. Capture for all 8 begins in M0.5. | — | — |
| V1-02 Live-beside-simulated view | not started | — | — |

---

## Measured so far

Every entry below was measured or checked on this machine. Nothing else has been measured yet.

| What | Value | How and when |
|---|---|---|
| oculus-core ephemeris, worst error | • Sun 0.19′ (0.003°).<br>• Moon 0.13′.<br>• Moon illuminated fraction 0.01 %. | `cargo test --release --test golden_astro` in oculus-core, against JPL Horizons and Astropy, 18 Nov (D-002).<br>• 3/3 tests pass, over 294 rows, 3 sites and 1950–2050.<br>• Both are inside the PRD §14 position targets (Sun ≤ 0.05°, Moon ≤ 0.3°). The refracted, per-viewpoint validation comes in M2. |
| Rainier park camera frame | 1920×1080, 577 KB (576,746 bytes) | `~/lookout-data/samples/mountain.jpg`, night of 18 Nov; checked with `sips` and `ls` |
| Point Reyes air-cam frame | 3840×2160, 1.86 MB (1,861,951 bytes) | `~/lookout-data/samples/porelarge.jpg`, night of 18 Nov |
| Rainier ARD air-cam frame (`mora`) | 512×288, 50 KB (50,099 bytes) | `~/lookout-data/samples/moralarge.jpg` |
| Stopgap Rainier capture | • 8 frames stored, 06:30:13 → 07:45:37 UTC on 19 Nov, 665,637–763,915 bytes each (night).<br>• They come in bursts: 4 frames 5 minutes apart (06:30–06:45 and 07:30–07:45), then `rate_capped` for the rest of the rolling hour. The capped events were at 06:50:14, 07:10:34, 07:15:35, 07:20:35, 07:25:35, 07:50:37 and 08:25:22.<br>• No `unchanged`, error or exception events. | `capture_log/2026-11-19.jsonl` and the frame sidecars, read at 08:30 UTC |
| Stopgap outages | • 06:52:41 → 07:09:41 UTC (17 min): 3 polls missed.<br>• 07:51:52 → 08:24:52 UTC (33 min): 6 polls missed.<br>• Both were idle sleep. No frame was lost: each came after the hour's 4 frames were stored, when every poll was capped. | `pmset -g log`, and the gaps between polls in the capture log |
| Rainier timestamps | • `Last-Modified` is 12–15 s after the EXIF time in 8 of 8 frames, so it marks each camera upload.<br>• Frames 1–4 were fetched in the same second as the upload, because the poll's phase happened to match. After the first wake, fetches ran 20–21 s after `Last-Modified`.<br>• EXIF times, 18 Nov PST: 22:30:00, 22:35:01, 22:40:01, 22:45:00, 23:30:01, 23:35:03, 23:40:03 and 23:45:01.<br>• 8 distinct SHA-256 values. | Frame sidecars, and `sips -g creation` on the stored frames, 08:13 UTC |
| Rainier update cadence | **Not measured yet.**<br>• Each of the 6 polls that came 5 minutes after a download found a new image. All 8 EXIF times sit 0–3 s after a 5-minute mark.<br>• A 5-minute poll at a fixed phase can't tell a 5-minute cadence from a faster one.<br>• An 18 Nov frame was stamped 20:21:03 PST, off the 5-minute grid ([docs/research/cameras.md](docs/research/cameras.md)).<br>• `Last-Modified` works as the HEAD change signal (row above). | Capture worker, M0.5-08 |
| Sampled archive day (`pore`, 17 Nov 2026) | • 88 frames, 00:00 → 23:45 PST.<br>• Gaps: 83 of 15 min, 3 of 30 min, 1 of 90 min.<br>• `TimeOfDay`: night 44, day 39, dusk 3, dawn 2.<br>• All 88 carry `SolarElevationAngle`. | `~/lookout-data/samples/nps-json/search_pore_20261117.json` |
| NPS feeds | • 8 of 8 live.<br>• All 8 ARD JSON feeds sampled carry `imagedate`, `imagesite`, `viewdetail` and `imagefilelarge`. | Scouting, 19 Nov; `samples/nps-json/*json.txt` |
| iCloud | • `~/Documents` has the iCloud Drive file-provider xattr.<br>• `~/lookout-data` has none. | `xattr -l`, 19 Nov (D-017) |
| Open-Meteo HRRR | Listed as "Discontinued 2026-11-30" | Open-Meteo docs page, saved 19 Nov 03:16 UTC and re-read locally (D-008) |
| EventBridge Scheduler free tier (us-east-1) | "$0 for the first 14 million scheduled invocations" | AWS Price List file for EventBridge, published 18 Nov 2026 21:44 UTC, saved 19 Nov and read locally (D-014) |
| Spend | $0.00 | DECISIONS.md ledger |

### NPS archive start dates

From `GetAvailableDays` (`MinDay`, `MaxDay`), fetched 19 Nov. The files are `~/lookout-data/samples/nps-json/days_<code>.json`. Per-camera cadence, gaps and epochs go in `docs/research/nps-archive-check.md` (being written).

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
