# PLAN.md: milestones, dates, cut order and evidence

Companion to [PRD.md](PRD.md) v0.4.2 (§16 milestones, §19 cut list). Per-ticket status and measured numbers are in [PRD_PROGRESS.md](PRD_PROGRESS.md). Decisions and spend are in [DECISIONS.md](DECISIONS.md).

Each milestone ends at a gate and stops for review. **A gate passes when its evidence exists: the measured value and, for a miss, the bottleneck.** Compiling code is not evidence. A missed target triggers the cut-order rule. It never triggers a stall or a retune.

**Update, early on Sat 21 Nov 2026 (UTC): the rename and the first push** (D-080):
- **The owner renamed the product Snow Globe.** The code and these docs carry the new names in the working tree, uncommitted. The running jobs keep the old names (`lookout-capture/0.2.1`, `Lookout/0.1 (…)`, `~/lookout-data`) until the lead's switch right after the M0.5 gate closes (M0.5, "Right after the gate").
- **First push:** with the owner's word, `m0-prd` went to `origin` `main` at 00:48:32 UTC (`23c0543`). The 20 Nov working tree listed below was committed before it (D-078, D-079). The goldens' sign-off (D-041) is still open, now for goldens that are public.
- **Names in this plan:** commands, packages and variables carry the new names. A record of something that ran keeps the name it ran under. Paths under `~/lookout-data` stay valid after the switch through its symlink.
- **The clock's earliest close moved to 23:12:07 UTC on Sun 22 Nov** (`snow-globe-eval data-report` to 06:06:26 UTC on 21 Nov: 10.796 h lost, 30.906 h covered of 41.701 h). The one loss since 20:01 UTC on 20 Nov is a capture outage at 01:23:43–01:34:51 UTC on 21 Nov (368.5 s lost), with the Mac on battery.
- **The switch is `infra/ops/migrate-to-snow-globe.sh`**, in the repo with the rename. Its gate is the data report, not a time: it refuses unless the report shows 72 h covered. Until it runs, the loops stay where they are (capture on `3cd2ca7`, the stand-in on `034c46b`) and nothing is re-snapshotted (M0.5, "Right after the gate").

**Where things stand at about 16:00 UTC on Fri 20 Nov 2026** (with the docs review's corrections at about 17:30 UTC):
- **M0 is done.** The owner accepted every PRD §1 default on 19 Nov (DECISIONS.md D-012).
- **M0.5 is in progress** (day 2 of 4).
  - **Committed:** the third workflow's packages, one commit each, at 12:35–12:36 UTC on 20 Nov, then its planning docs at 12:45 (`08a996c` to `8459e7e`, D-064). The second workflow's went in at 02:37–02:42 UTC (`4be0eca` to `8246a8b`, D-046).
  - **In the working tree, for the lead to commit:** capture 0.2.1 with the answer-time poll rule (D-065, D-066); eval's data report with the gate rule, its held-out UTC reading and the pair's stamp floor (D-069, D-070, D-073); the regenerated backfill plan (D-072); the build changes (D-075); and these docs.
  - **Two of the four gate items have evidence:** the archive report with H or P per viewpoint (D-019, D-022, D-024, D-042), and the backtest pair, scored and labelled v−1 ([docs/evidence/m05-backtest-pair.md](docs/evidence/m05-backtest-pair.md)).
  - **The 72 h log clock** started at 12:24:23 UTC on 19 Nov for all 8 cameras (06:30 UTC for Rainier). The lead chose its rule (D-069): lost time is the union of both jobs' outages plus each capture restart's excess. By that rule 10.69 h were lost, and **the clock closes at 23:05:58 UTC on Sun 22 Nov at the earliest** (`lookout-eval data-report` to 20:01:58 UTC on 20 Nov). PLAN.md's capture-only figure, kept for comparison, gives 22:44:12 UTC. The only loss since 06:29 UTC on 20 Nov is the re-snapshot at 19:44–20:01 UTC, when the Mac slept (D-079).
  - **The emails** wait for the owner.
- **Running on this laptop** until AWS is live (D-030), from a frozen snapshot of committed code with its own venvs (`~/lookout-data/cache/runtime/034c46b`, D-049):
  - capture 0.2.0 as `lookout-capture loop --archive-queue` for all 8 cameras, since 02:39:39 UTC on 20 Nov, under `caffeinate -s`, which keeps the Mac awake only on AC power;
  - the forecast Lambda handler every 5 minutes, with 8 fields since 02:39:49 UTC (D-050).
  
  Both jobs are children of the lead's session, so ending that session can stop them. The lead's supervisor would restart a job that stops, in its own session, and starting it is the owner's call (D-076). It adopts the running jobs without detaching them, so they depend on the session until they exit. The 8 h keep-awake ended at 14:27:54 UTC. The Rainier stopgap was retired at 12:13 UTC on 19 Nov (D-023).
- **Archive backfill runs** inside the loop (D-029, D-052). T0 is done: it dated the switch from 3:2 to 16:9 at the six queued ARD cameras to within 14 days, 26 at Grand Canyon; Grand Teton's stays undated, as it has no design H (D-024). On 20 Nov the day's 1,000 requests were spent by 11:43 UTC. The lead then removed the six 31 May entries (D-070) and resumed Big Bend's failed one (D-071): 441 entries, 180 done. The plan was regenerated from the live queue (D-072): T3pr goes ahead of T1b and lands 4 days early, while T1b and T2 miss PLAN's targets by a day (M1.5 below).
- **Evaluation design** (D-024): Rainier and Point Reyes are design P on the sealed window; Point Reyes adds design H for K2; five ARD cameras are design H; Grand Teton has no design H. Grand Canyon's 3:2 epoch holds two aims, so it needs a registration first (D-042). Until the owner confirms D-038, an archived frame is held out if its local or its UTC date is on or after 1 Jun 2026 (D-070).
- **M1 preparation is built, code only,** with no gate claim (D-032). None of it has run with the owner:
  - the 1a spike app (D-033), rebuilt against speech library 0.2.0 at 12:36 UTC on 20 Nov (D-074);
  - the Swift speech library 0.2.0, never run against the real recognizer (D-055); its silence pad stays 0 ms by default (D-067);
  - the owner runbook ([docs/m1/owner-runbook.md](docs/m1/owner-runbook.md)), now with the library's first real run as step 3;
  - the 130-utterance replay script (D-057), with idle trials' display rule and idle follow-ups decided (D-068);
  - the README skeleton.
- **Waiting on the owner:**
  - AC power for the Mac, and the start of the lead's supervisor (D-076);
  - AWS, with the hard-stop choice (D-035, now with D-060's and D-066's estimates) before the first deploy;
  - sending the NPS emails;
  - permission to push, with the answer on the committed goldens (D-041) first (the push came on 21 Nov; D-041 is still open, D-080);
  - Deepgram and fal keys;
  - by 14 Dec, the confirmations of D-019, D-024, D-036, D-038 (with D-070), D-039, D-044 and D-058, and the backfill plan's TQ questions (D-072).
  
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
- **Polite collection.** Only one NPS fetcher runs at a time, under the CLAUDE.md rules. That fetcher is `snow-globe-capture loop` (the snapshot's `lookout-capture loop` until the post-gate switch, D-080), behind a host-wide lock (D-023); archive backfill runs inside it, never beside it (D-029).

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

"In progress" means code exists but its evidence doesn't yet, or isn't complete. Measured numbers are in PRD_PROGRESS.md.

| Build (PRD §16) | Status, 20 Nov, about 16:00 UTC | Where |
|---|---|---|
| **Forecast worker.**<br>• HRRR byte-range GETs from `noaa-hrrr-bdp-pds` at each viewpoint's sample points (where view rays meet each layer height).<br>• One JSON per viewpoint per run: source, run start, availability (S3 `LastModified`), fetch time, grid cells, byte ranges and values.<br>• Live lag per forecast hour logged for the hindcast availability model.<br>• GRIB subsets deleted after extraction. | in progress.<br>• **Running:** 0.1.0 (`09e2ec8`) as the local stand-in, from snapshot `034c46b`, with 8 fields since 02:39:49 UTC on 20 Nov (D-049, D-050). Every hourly run from 2026111900 to 2026112014 is written at all 8 viewpoints, none missed; 443 distinct live lags in the data report's window to 15:46 UTC.<br>• **Committed, not running yet:** 0.2.0 (`08a996c`, D-064), with survey lags in the model (4,082 logged), `tick` and explicit hour lists (D-053), and the stamp floor for archived runs (D-054). 177 tests pass. | `workers/forecast/` |
| **Capture of every camera that passes the licence check: all 8 (D-020).**<br>• Poll the JSON or headers at most every 5 min, and download only on change.<br>• At most 4 per camera per rolling hour, enforced with persisted state.<br>• Quality gates (PRD §8.6); stale and move flags.<br>• Private storage at ≤ 1920 px.<br>• Measure Rainier's real update cadence.<br>• Takes Rainier over from the stopgap. | in progress.<br>• **Running:** 0.2.0 (`df1838a`) as `lookout-capture loop --archive-queue`, all 8 cameras, since 02:39:39 UTC on 20 Nov, behind the host-wide lock (D-023, D-049). It has D-028's storage, a thumbnail of every download, the shared www.nps.gov hold and the archive queue (D-029). 0.1.0 ran from 12:24:23 UTC on 19 Nov.<br>• Rainier's cadence is measured: about once a minute.<br>• Stale flags are built; the move flag (the Same view gate) waits for plate registration, TODO(M2) (D-037).<br>• Polls were up to 0.94 s early on 19 Nov (D-048). Since the restart the shortest gap is 301.05 s send to send, but 1 of 1,008 poll pairs is not provably 300 s apart at nps.gov (D-065).<br>• **In the working tree:** 0.2.1, which spaces polls and image GETs from the previous answer, so the gaps hold at nps.gov whatever the network does (D-065), and guards against failed, redirected and stalled requests (D-066). 249 tests pass, plus 4 slow schedule tests. | `workers/capture/`, `workers/common/` |
| **Stopgap Rainier capture** (D-021) | done and retired: it ran 06:30–12:13 UTC on 19 Nov and stored 24 frames (D-023). | `$SNOW_GLOBE_DATA_DIR/frames/rainier.paradise-mountain/` |
| **NPS archive check:** depth, cadence and camera epochs per camera, leading to H or P per viewpoint | done: [docs/research/nps-archive-check.md](docs/research/nps-archive-check.md), 217 requests. Every ARD camera changed from a 3:2 to a 16:9 frame in 2023–25. Design per viewpoint in D-024, with the guard breach in D-022 and Grand Canyon's re-aim in D-042 (the check's text corrected on 20 Nov). The backfill's T0 dated the switch at the six queued ARD cameras to within 14 days, 26 at Grand Canyon; Grand Teton was not queued (D-052). | `docs/research/` |
| **Repo scaffold, CI, trace skeleton, PRD_PROGRESS.md.** The scaffold covers the uv, pnpm and Cargo projects per CLAUDE.md. | • Scaffold: done (`0bb42ab`). `make test` passes all 10 suites, `speech` and `bench` included (224 s, 10:31–10:35 UTC on 20 Nov, D-063; committed in `951b704`). Since then (D-075, uncommitted): capture's 4 slow schedule tests run in it on purpose, `test-speech` also runs the speech library's symbol, signing and link checks, and `make bench-script` checks the replay script against the real parser. `make docs-check` runs 16 invariant checks (91 tests).<br>• Trace skeleton: done (`e7b87e8`).<br>• PRD_PROGRESS.md: kept current.<br>• CI: the workflow runs every `make test` suite except the laptop-only `speech` (D-063). Its first run on GitHub needs a push, which needs the owner. | repo root, `app/core/`, `.github/workflows/`, `docs/checks/` |
| **Infra code:** Terraform for the serverless stack (D-014), and the Lambda container image | in progress, code only (D-034, D-059).<br>• `infra/test.sh`: 113 checks pass, and the AWS provider now installs offline from the plugin cache.<br>• Capture's S3 grants now cover D-028's thumbnails and staging.<br>• Three local smoke runs of the forecast handler under the Runtime Interface Emulator, the last on the image of `8246a8b`.<br>• The estimate is $2.07–4.03 in month 1 with the free tiers for the committed capture 0.2.0 (D-060), and $2.43–4.40 for capture 0.2.1 (D-066).<br>• Committed in `e59247a` (D-064).<br>• Nothing is planned or applied: there are no AWS credentials, and `apply` waits for the owner. | `infra/terraform/`, `infra/lambda/`, `infra/cost/` |
| **One backtest pair end to end:** an archived frame plus an archived HRRR run, then a crude headless render (renderer v−1), then a crude extractor score, labelled "v−1" | done and committed (`d9aa416`): Grand Canyon, 15 Jan 2024, 24 h lead; scored and labelled v−1; 4 runs, byte-identical renders. Caveats: the cell is a correct negative by accident; the plate and truth are about 5 % apart in aim (D-042); no v−1 pair counts toward a metric. The records' oculus-core suffix is corrected in the evidence file (D-062). The run choice now applies the archived stamp floor, with this pair's choice unchanged (D-073, uncommitted). | [docs/evidence/m05-backtest-pair.md](docs/evidence/m05-backtest-pair.md), `renderer/`, `eval/` |
| **Email drafts.**<br>• NPS Air Resources Division and Mount Rainier NP: public domain, collection, archive and publication.<br>• Lawrence Hall of Science and ALERTCalifornia: Golden Gate. | done (`e4722a6`; the NPS draft gained the Rainier camera questions on 20 Nov). Sending them is the owner's step. On 20 Nov the NPS draft's "[N] frames" was filled with the backfill plan's figures (D-052); the regenerated plan gives about 23,700 frames (about 1.4 GB with the day searches) by mid-December, against the draft's 22,700 ([docs/research/archive-backfill-plan.md](docs/research/archive-backfill-plan.md), section 7; D-072). | [docs/outreach/](docs/outreach/) |
| **Early M1 logic:** `snow-globe-core` with the parser, time rules, an ephemeris wrapper over oculus-core `1b5410b` (D-025), solar events, trace and lifecycle | in progress (early): committed (`e7b87e8`, `fd0f206`), 108 tests, 295 parser table cases. Some rule readings wait for the owner (D-039, D-058). | `app/core/` |
| **Early M1 preparation** (D-032): the 1a spike app, the Swift speech library, the owner runbooks, the replay script | in progress (early), code only; none of it has run with the owner.<br>• The spike app is built (105 unit tests), and none of its 11 checks has run (D-033). The lead rebuilt it against speech library 0.2.0 at 12:36 UTC on 20 Nov (D-074).<br>• The speech library 0.2.0 is built (103 tests) and committed (`8443bf3`), and has never run against the recognizer (D-055).<br>• The owner runbook and the 130-utterance replay script are written and committed (`dc28c2a`, D-057); the runbook now gives the library's first real run (step 3). | `spikes/m1-platform/`, `app/speech-macos/`, `docs/m1/`, `bench/` |

**Also done on 19 Nov:**
- The data directory lives outside iCloud (D-017), and the iCloud workarounds are recorded (D-031).
- All 8 NPS feeds were scouted and found live, and the viewpoint IDs were fixed (D-020).
- The oculus-core pin moved to a public commit (D-025).
- PLAN.md, DECISIONS.md and PRD_PROGRESS.md were brought to v0.4, and PRD.md got its factual corrections (v0.4.1). A first `THIRD_PARTY_NOTICES` was written on 20 Nov.

**Done on 20 Nov (UTC):**
- 02:37–02:42: the second workflow's packages committed, one commit each (D-046). The held-out split is in git (`d9aa416`).
- 02:39: both jobs restarted on a new snapshot, `034c46b`, with their own venvs. Capture 0.2.0 went live with the archive queue, and the forecast stand-in gained `PRES:cloud base` (D-049, D-050).
- 03:11: a survey of 194 runs logged 4,082 S3 lags for the lag model (D-053).
- 09:50: the backfill plan's first tranche was queued (D-052); the day's 1,000 archive requests were spent by 11:43.
- 12:30: the lead found that both jobs are children of its session, and wrote a supervisor that is not started yet (D-076).
- 12:35–12:45: the third workflow's packages committed, one commit each, then its planning docs (`08a996c` to `8459e7e`, D-064).
- 12:36: the spike rebuilt against speech library 0.2.0 (D-074).
- About 12:40: the six 31 May queue entries removed, and Big Bend's failed entry resumed (D-070, D-071).
- 14:27:54: the 8 h keep-awake ended; only the capture loop's `caffeinate -s` remains (D-076).
- The lead's decisions: the poll rule (D-065), the silence pad (D-067), idle trials (D-068), the clock rule (D-069) and the held-out edge (D-070). This workflow's capture, eval and planner packages were built, reviewed and fixed, and are uncommitted (D-065, D-066, D-069, D-070, D-072, D-073), as are the build changes (D-075) and these docs.

**Optional, not scheduled:** a council round on the parts taken from the Lookout draft, adding a forecast-verification judge (PRD §18.2, Appendix B).

**Gate evidence:**
- [ ] ≥ 72 h of worker and capture logs: runs and frames per viewpoint, plus exclusions.
  - **The table:** `snow-globe-eval data-report` builds it from the logs, state and disk, and writes nothing (D-061). The lead runs it at the close. The renamed report reads the old names too, and must give the same figures as before the rename (D-080).
  - **Clock:** all 8 cameras from the loop's start at 12:24:23 UTC on 19 Nov; lost time extends it and is listed as exclusions. **The rule** (the lead, D-069): the union of both jobs' outages plus each capture restart's excess.
    - By that rule 10.69 h were lost, so it closes at **23:05:58 UTC on Sun 22 Nov** at the earliest (the data report to 20:01:58 UTC on 20 Nov, with 20.93 h covered of 31.63 h elapsed).
    - Read again to 06:06:26 UTC on 21 Nov: 10.796 h lost, so **23:12:07 UTC on Sun 22 Nov** at the earliest (30.906 h covered of 41.701 h). The new loss is a capture outage at 01:23:43–01:34:51 UTC on 21 Nov, 368.5 s, with the Mac on battery. PLAN.md's earlier rule then gives 22:50:21 UTC.
    - PLAN.md's earlier rule, the capture loop's outages alone with the report's 540 s threshold, stays beside it for comparison: 10.33 h lost, a close at 22:44:12 UTC.
    - A new outage moves the close later. The re-snapshot to capture 0.2.1 cost 12.7 min, not the planned 60–70 s, because the Mac slept through it (D-079).
  - **Logs before the clock:** Rainier has logs from 06:30 UTC on 19 Nov (the stopgap), and the forecast stand-in from 12:18 UTC.
  - **Outages so far:**
    - the stopgap: about 28 polls in 4 sleeps (2.4 h), before the clock started;
    - the loop on 19 Nov: 4 outages, 8.22 h;
    - the loop on 20 Nov: 4 outages, 1.99 h, while the Mac was on battery (D-051);
    - the forecast job: 5 outages, 6.04 h.
    
    The restart at 02:39 UTC on 20 Nov left each camera 483–486 s between polls. Under the gate rule its excess is 185.471 s (Acadia), which overlaps no outage (D-069); under PLAN.md's earlier rule it adds nothing. PRD_PROGRESS.md lists every outage.
  - **Exclusions:** none of these is live-worker evidence:
    - the archived `run` event of the backtest pair (run 2024011412, line 475 of `forecast_log/2026-11-19.jsonl`);
    - the 12 re-extracted runs and 160 duplicate lags of 02:39–02:50 UTC on 20 Nov (D-050);
    - the 4,082 survey lags;
    - the archive queue's work (`"worker": "archive"`).
- [x] The archive report, with H or P chosen per viewpoint in DECISIONS.md: [docs/research/nps-archive-check.md](docs/research/nps-archive-check.md); D-019, D-022 and D-024, corrected by D-042.
  - Rainier: design P. Point Reyes: design P on the sealed window, plus design H for K2.
  - Grand Canyon, Yosemite, Acadia and the Smokies: design H on their usable spans. Big Bend: design H after a check.
  - Grand Teton: no design H; it is captured for Tier 0 only.
- [x] The backtest pair rendered and scored, labelled "v−1": [docs/evidence/m05-backtest-pair.md](docs/evidence/m05-backtest-pair.md), committed in `d9aa416`.
  - The cell is a correct negative by accident: flat light in the truth, renderer haze in the render.
  - The plate and the truth are about 5 % apart in aim (D-042). Under that shift the primary box's truth ratio moves from 0.330 to 0.360, across τ_lo, so its class changes.
  - No v−1 pair counts toward a metric (`counts_toward_metrics` is false).
- [ ] Emails sent (owner).

**Right after the gate closes: the rename's live switch** (the lead; D-080). It runs once the data report shows the clock closed: 23:12:07 UTC on Sun 22 Nov at the earliest at 06:06 UTC on 21 Nov, and every new outage moves it later. Before it nothing live changes, so the rename costs the clock nothing.
1. **Close the gate.** Run the gate's data report (`make data-report`, read-only), and record the close and the table.
2. **Rehearse:** `infra/ops/migrate-to-snow-globe.sh --dry-run` from the rename commit. It runs the gate check and the real rehearsal (HEAD's workers built with their own venvs offline in a temporary folder, the new code's read-only `status` on the live data, the forecast handler imported, the three scripts' rewrite shown as diffs) and changes nothing live.
3. **Switch:** `infra/ops/migrate-to-snow-globe.sh`, as a background command of the lead's session, on AC power. It is one command under its own `caffeinate -is` that stops and starts (D-079's lessons), tested against stub loops by `infra/tests/test_migrate.sh`:
   - it refuses unless the data report shows the gate rule's 72 h covered (`--force` is the owner's override), unless `~/lookout-data` is a real folder and `~/snow-globe-data` is absent, and unless the three live scripts are the ones it was written against (SHA-256);
   - it stops the supervisor first if the owner has started it, because it would otherwise restart the old loops from `current`; it never restarts the supervisor itself (D-076);
   - capture first: the loop is stopped just after a pass ends; then `~/lookout-data` moves to `~/snow-globe-data`, with a symlink back; then the snapshot of the rename commit is built with its own venvs (D-049's procedure), the loops' scripts are rewritten for the `SNOW_GLOBE_*` names (old ones kept as `*.lookout.bak`), `current` is flipped, and the new capture loop starts about 240 s after the old loop's last pass. It must hold the NPS fetcher lock and log a `snow-globe-capture/` pass. The live User-Agent becomes `SnowGlobe/0.1 (webcam research capture; fetch-on-change)`;
   - the stand-in last: the old stand-in kept running through the move; it is stopped, its handler waited for, and the new one started and checked through its first tick. It jumps from forecast 0.1.0 (`034c46b`) to 0.3.0, which is 0.2.0's stamp floor and hour list plus the rename (D-053, D-054);
   - a failure before the new capture loop runs puts everything back (data, scripts, `current`) and restarts only the capture loop it stopped; a new stand-in that fails its first tick is stopped, and the old stand-in's script starts again from the pre-switch `current`.
4. **Check the first pass and the first stand-in run:** all 8 cameras polled, the new User-Agent and the `snow-globe-capture/` version prefix in the log, one fetcher holding the lock, the stand-in's first run file valid with `kind` `snow-globe.forecast-run`, and the data report still reading the old records.
5. **Record it** in DECISIONS.md and PRD_PROGRESS.md (M0.5-21). The restart's gap is logged like any other outage, after the gate.

`~/snow-globe-data` must not exist before step 3. If it did, renamed code would refuse to pick a data dir (D-080), and the switch refuses to run. Nothing is re-snapshotted before the switch either: the live scripts start `lookout-capture` and import `lookout_forecast`, which the renamed code no longer has, so the usual re-snapshot (flip `current`, SIGTERM the loops) would leave both jobs down. The switch is the only path onto a renamed snapshot.

**Risks to this gate:**
- **Gaps in the 72 h logs.** Until AWS is live, capture and the forecast worker run only while this laptop is awake, and `caffeinate -s` keeps it awake only on AC power. The 8 h keep-awake ended at 14:27:54 UTC on 20 Nov. On 20 Nov the Mac was on battery again for 3 h (D-051). Gaps are reported as outages and extend the 72 h clock. Rainier gaps can never be backfilled, because that camera has no archive.
- **The jobs depend on the lead's session** (D-076). Ending it can stop both loops, and the clock with them. The lead's supervisor, once the owner starts it, restarts a loop that stops, in its own session. It does not detach the running loops it adopts, which depend on the session until they exit, and it starts capture again 330 s after an adopted loop's unannounced exit, which the 72 h clock counts as lost time (D-069).
- **The frozen snapshots** (updated 21 Nov, D-080). Capture runs `3cd2ca7` (capture 0.2.1) since 19:56 UTC on 20 Nov (D-079), and the forecast stand-in still runs `034c46b` (forecast 0.1.0). Until the rename's switch there is no re-snapshot: the switch is the only path onto renamed code (M0.5, "Right after the gate"). The 20 Nov reasons for the re-snapshot that has since happened, kept as written:
  - capture 0.2.1's answer-time rule makes each answered poll pair provable from the log; under 0.2.0's 1 s send-to-send margin, 1 of 1,008 pairs since the restart is not provably 300 s apart at nps.gov (D-065);
  - the running 0.2.0 lacks the held-out guard's UTC reading, so no 31 May queue entry may be added until the loop runs 0.2.1 (D-070);
  - forecast 0.2.0's stamp floor (D-054) applies to archived runs, and the stand-in writes live runs only.
  
  The third workflow's critic suggested re-snapshotting after the 72 h clock closes; the timing is the lead's call.
- **The archive limiter's margin.** One of 999 archive request pairs on 20 Nov was 4.9944 s apart end to start on the log's stamps, 5.6 ms under CLAUDE.md's 5 s (D-077, from D-072's recount). Fixed in `3cd2ca7` (the limiter waits out the wall clock after every sleep; spacing 5.05 s) and live since 19:56 UTC on 20 Nov (D-079): on 21 Nov all 999 end-to-start pairs were at least 5.0503 s apart.
- **Disk.** 27 GiB was free of 460 GiB at 17:26 UTC on 20 Nov (19 GiB at 15:46). The live policy adds about 262 MB a day (D-028), and the backfill through T1c about 1.3 GB (D-072).

## M1: Voice to first pixel · Mon 23 Nov → Mon 7 Dec · not started (core logic started early)

**Started early, code only, with no M1 gate claim (D-032).** None of it has run with the owner:
- **The 1a spike app** (`spikes/m1-platform/`, never shipped): built on 19 Nov, 105 unit tests pass, and its criteria are fixed in code before any run (D-033). None of the 11 checks has run; they need the owner at the keyboard, and the steps are in its README.
- **`snow-globe-core`** (`app/core/`): the parser, time rules, ephemeris wrapper, trace and lifecycle; numbers in PRD_PROGRESS.md (M1-02 to M1-04).
- **The Swift speech library** (`app/speech-macos/`, M1-05): 0.2.0, built on 20 Nov, 103 tests pass, committed in `8443bf3` (D-055).
  - Its C ABI keeps the spike's seven prototypes and adds a stable-text event for lever 2, plus setters for contextual strings, the silence pad and model retention.
  - It has never run against the real recognizer. Spike checks 6 and 7 use it: the spike's `--features speech` build links the real library, 0.1.0 at 03:39 UTC on 20 Nov, and 0.2.0 since the lead rebuilt it at 12:36 UTC (D-074).
  - The silence pad stays 0 ms by default; the shell and the harness set it and log it with every trial, and the bake-off measures it (D-067).
- **The owner runbook** ([docs/m1/owner-runbook.md](docs/m1/owner-runbook.md)): every M1 step for the owner, in order. It names all five permissions, and when to switch Microphone and Speech Recognition off again. Its step 3 is the speech library's first real run, then checks 6 and 7.
- **The replay script** (`bench/`, M1-10): 130 utterances, 30 dev and 100 test, with the expected query of each (D-057). Nothing is recorded; the recording tool is M1-09.
- **The README skeleton** (`README.md`, 1d): the headline sentence and the metrics table, with blanks.
- **The real app shell waits** for the spike's results.

**1a. Platform spike** (one day of checks; the code lives in `spikes/m1-platform/` and is never shipped, D-032). Stop and report if any check fails:
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
- **`snow-globe-core`:** query lifecycle with `query_gen` and cancellation, and tracing with no transcript text. Started 19 Nov.
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
  - **The script exists** (D-057): `bench/utterances/scripted.tsv`, 130 rows, script hash `3b4256f1cf51`.
  - Six rows are `owner-pending` and are reported apart until the owner answers three rule questions.
  - Follow-ups come straight after the row that sets their place, and t100 is played first after a launch.
- Sample substitution inside the live `cpal` callback, and a separate cache namespace per pass.
- Five states: idle (the headline), warm, cold, after wake and after launch.
- Overnight idle runs under `caffeinate -i` from a frozen build:
  - ≥ 100 idle trials for the shipped engine and ≥ 60 for the comparison engine;
  - spread over ≥ 3 days, with engines interleaved.
  - **Decided by the lead** (D-068): idle trials keep the system awake (`caffeinate -i`, never `-d`) and let the display sleep as the Mac's settings do (off after 10 min on AC). Each trial logs the display state, and display-asleep trials are reported apart as well as in the totals. Follow-up rows may be idle trials, reported as idle follow-ups.
- A cluster-bootstrap report.
- **Scoring, proposed** (D-057): "exact place and time" without the owner-pending rows, with the denominator named. The scorer re-parses at the place the app actually used as context. Normalised WER spells numbers the same way on both sides.

**1d. README skeleton,** with the headline sentence and blanks. Written on 20 Nov (`README.md`); its blanks fill only from measured exports.

**From the owner in M1** (in order in [docs/m1/owner-runbook.md](docs/m1/owner-runbook.md)):
- Permissions: Microphone; Speech Recognition only if `snow-globe-transcribe status` reports code 2, since the library never asks for it (D-055); Screen Recording and Input Monitoring for the harness only; Apple Intelligence on. The spike's `CGEventPost` check also needs Accessibility, which PRD §1 row 14 doesn't list (open, D-033). The runbook grants them to a terminal never used for coding agents.
- The speech library's first real run: `snow-globe-transcribe status --locale en-US`, then `install-assets --locale en-US --yes` only if it reports code 1, keeping the `reservations` lines, then spike checks 6 and 7 on a speech build of the renamed library (the runbook's step 2.1 rebuilds the spike with `--features speech` right after the README's Build block), noting whether Speech Recognition "not determined" shows a prompt (runbook step 3; D-055, D-074, D-080).
- The replay recordings, and several nights of idle trials.
- Answers to the three rule questions and the new parser readings (D-057, D-058).
- A Deepgram account and key, for arm D, kept in a shell opened only for that. The owner is asked again before its first paid call.

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
- **Design-H archive backfill** (D-052; the plan is [docs/research/archive-backfill-plan.md](docs/research/archive-backfill-plan.md)). It runs inside the capture loop from 20 Nov, at most 1,000 requests a UTC day, and the lead queues each tranche.
  - **Not an M1.5 gate dependency:** M1.5's gate is P1 at Rainier, on our own captures, and Rainier has no archive.
  - **What it feeds:** extractor v0 at the design-H cameras, a first τ, persistence, a first S_mos, the block length and a first MDE, all from T1a's summer weeks (due Tue 24 Nov).
  - **Its two dated needs:**
    - K2's Point Reyes landmark, named by the 14 Dec freeze, from T1a;
    - the inputs of Point Reyes's M2 plate, from T3pr, due Sun 29 Nov.
  - **Its own targets,** if the loop runs at least 2.2 h every UTC day: T0–T1b and T3pr by Sun 29 Nov, T2 by Thu 10 Dec and T1c by Tue 15 Dec. T0 finished on 20 Nov.
  - **Measured against them** (the plan regenerated from the live queue, D-072; 1,000 requests a UTC day from 21 Nov, 1.0 % errors, with T3pr queued ahead of T1b as it recommends): T1a lands on Tue 24 Nov, T3pr on Wed 25 Nov, T1b on Mon 30 Nov, T2 on Fri 11 Dec and T1c on Tue 15 Dec.
    - **Missed by a day:** T1b (37 requests short on 29 Nov) and T2 (122 short on 10 Dec). The nine UTC days through 29 Nov hold 8,910 useful requests, and T1a, T1b and T3pr need 8,947, so no order meets both 29 Nov targets. At 19 Nov's 3.7 % error rate, T1a (25 Nov) and T1c (Wed 16 Dec) are also a day late.
    - **Bottleneck:** the 1,000-a-day budget, which stays until NPS answers the email, plus 4–5 % more requests from the re-measured share of a complete day.
    - **The finish days also assume** at least 2.2 h of loop time every UTC day and a daily resume of failed entries (D-071, D-072).
  - **For the owner:** TQ, 232 entries and 3,253 requests between T0's first 16:9 frames and D-024's spans, queued only with the owner's yes. Point Reyes's TQ block (857 requests), if approved, goes before T2 and lands on Mon 30 Nov (Tue 1 Dec at 3.7 % errors), before the freeze. T2 then lands on Fri 11 Dec (Sat 12 Dec) and T1c on Wed 16 Dec (Thu 17 Dec), so T1c misses its 15 Dec target at both error rates (D-072).
  - **Still open:** the clear-sky share per camera and Sun bin, from extractor v0 on T1a (the plan's TODO(M1.5)), and re-aims too small for the thumbnails, from the Same view registration in M2 (D-042).
  - **Archived HRRR** for these dates is the forecast worker's job: 360 development dates through T2 (423 with T1c; D-072).

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
- **Decided by the lead before the sealed window opens:** whether design P counts an outage per pass or per camera. On the Lambda's schedule a camera sometimes skips one invocation, a gap of about 555–622 s at that camera only (D-065).
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
- **`THIRD_PARTY_NOTICES`,** generated from the licence ledger. A first version, written by hand from the lock files, exists since 20 Nov, and `docs/checks` fails when a `uv.lock`, `Cargo.lock` or `package.json` pins a third-party package it doesn't name at that version (D-063). The Terraform lock files and Snow Globe's own package versions are checked by hand.
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
