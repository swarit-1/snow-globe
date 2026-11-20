# PRD_PROGRESS.md: tickets, owner actions and measured numbers

This file tracks progress per ticket against [PRD.md](PRD.md) v0.4.1 and [PLAN.md](PLAN.md), with every measured number (PRD §16.1). Decisions and the spend ledger are in [DECISIONS.md](DECISIONS.md).

**Updated:** Fri 20 Nov 2026, about 16:00 UTC (G+1), with the docs review's corrections at about 17:30. Snapshot rows say when they were read. M0 is done, and M0.5 is in progress (day 2 of 4).

**Running overrun before the 14 Dec freeze:** 0 days, of the 4 allowed across M0.5, M1 and M1.5 (PLAN.md, calendar notes).

**Status values:**
- **done:** its evidence exists.
- **in progress:** being built, but the evidence doesn't exist yet or isn't complete. Written code with no evidence counts here, not as done.
- **blocked on owner:** waiting on one of the owner actions below.
- **not started.**

**Committed so far:** `0bb42ab` (common, manifests, Makefile, CI) to `8459e7e`. The second workflow's packages went in one commit each at 02:37–02:42 UTC on 20 Nov (D-046), and the third workflow's at 12:35–12:36 UTC, with its planning docs at 12:45 (`08a996c` to `8459e7e`, D-064).

**In the working tree, uncommitted, for the lead:**
- capture 0.2.1: polls and image GETs spaced from the previous answer, and guards for failed, redirected and stalled requests (D-065, D-066);
- eval: the data report's gate rule and nps.gov bounds (D-069), the held-out guard's UTC reading (D-070), and the pair's stamp floor (D-073);
- the regenerated backfill plan, `eval/tools/backfill_plan/` and `docs/research/archive-backfill-plan.md` (D-072);
- the Makefile, CI comments and CLAUDE.md's layout line (D-075);
- these planning docs, the owner runbook, the spike, bench and forecast READMEs, the archive check, PRD.md's cost pointers and `THIRD_PARTY_NOTICES`.

---

## Owner actions

Most urgent first. Other files cite these numbers (the tickets below, `docs/m1/owner-runbook.md`, DECISIONS.md), so a new action joins an existing item or goes at the end, and the numbers never move.

1. **Keep the Mac on AC power.**
   - Capture and the forecast job run on this laptop. The capture loop runs under `caffeinate -s`, which keeps the whole Mac awake only on AC power, and the forecast job relies on it (D-030).
   - On 19 Nov it ran on battery from 06:13 to 22:48 UTC, slept 4 times and lost 8.22 h of all-camera capture.
   - On 20 Nov it was on battery again from 03:29 to 06:27 UTC. The lid closed at 03:52, and 1.99 h of capture was lost (D-051). Under the lead's gate rule (D-069) the 72 h clock now closes at 23:06 UTC on Sun 22 Nov at the earliest, after the re-snapshot's 12.7 min (D-079).
   - The 8 h keep-awake you approved ended at 14:27:54 UTC on 20 Nov. Since then only the capture loop's `caffeinate -s` keeps the Mac awake, and only on AC power with the lid open.
   - Rainier's gaps can never be backfilled.
   - **Start the lead's supervisor** (D-076, D-078). Both jobs are children of the lead's session, so ending that session can stop them and the clock. The supervisor finds the running loops itself (capture's pid from the NPS fetcher lock, the forecast loop by its script path), and when one exits it starts a replacement politely, in the supervisor's own session; the permission system refused the lead's own launch. Paste this into your own Terminal (it names no pid, so it stays right after the lead's restarts):
     ```sh
     mkdir -p ~/lookout-data/cache/runtime/logs && cd ~/lookout-data/cache/runtime/bin
     ( nohup /usr/bin/perl -MPOSIX -e 'POSIX::setsid() != -1 or die "setsid: $!\n"; exec @ARGV or die "exec: $!\n"' \
         /bin/bash supervise.sh >> ../logs/supervisor.out 2>&1 < /dev/null & )
     sleep 2; ps -o pid,ppid,tty,stat,command -p "$(cat ~/lookout-data/cache/runtime/supervisor.pid)"
     tail -3 ~/lookout-data/cache/runtime/logs/supervisor.log
     ```
     - The `ps` line should show PPID `1`, TTY `??` and an `s` in STAT: the supervisor then runs in its own session. The log's first line names the two pids it adopted; neither should read `none`. If `ps` shows nothing, read `../logs/supervisor.out`.
     - The parentheses matter. Typed bare at a Terminal prompt, `setsid` fails, Perl returns −1 and `or die` misses it, so the supervisor would stay in your Terminal's session (D-076).
     - **What it does not do:** the adopted loops stay children of the lead's session until they exit. The supervisor restarts them after that, capture 330 s after an exit nobody announced.
     - **To stop everything later:** `kill "$(cat ~/lookout-data/cache/runtime/supervisor.pid)"` stops the supervisor and any loop it started, but never a loop it adopted; `tail ~/lookout-data/cache/runtime/logs/supervisor.log` says which. An adopted loop is stopped with `kill <its pid>`; the capture loop then finishes its current camera and saves its state.
2. **AWS.** Until AWS is live, capture depends on this laptop, and it must be live before the sealed window opens on Tue 15 Dec.
   - Use an account that runs only Lookout (the budget counts the whole account), on the Paid plan.
   - Provide credentials through the environment, and set `TF_VAR_alarm_email`.
   - Check the Lambda concurrency quota is at least 102 (`aws lambda get-account-settings`).
   - **Before the first deploy, decide D-035,** now with D-060's estimate: $2.07–4.03 a month with the free tiers in month 1, rising by about $0.22 a month. With capture 0.2.1, which the lead's poll rule needs, it is $2.43–4.40 in month 1, and the bottom of the range passes $3 in month 4 (D-066). The questions are whether PRD §17's $3 is monthly, and whether the hard stop stays on through the sealed window. At the top of the range a $3 hard stop would halt both jobs around day 26 of month 1. The recommendation is to switch it off for 15 Dec – 14 Jan and keep the email alerts.
   - Then run `infra/deploy.sh --dry-run` and `infra/deploy.sh`, and confirm the SNS email. Nothing is applied without your word (D-014).
3. **NPS confirmation emails.**
   - The drafts are in [docs/outreach/nps-confirmation.md](docs/outreach/nps-confirmation.md), for the NPS Air Resources Division and Mount Rainier NP. The Rainier one asks where the "mountain" camera is mounted, how often it updates and whether past frames are kept.
   - The archive backfill now runs (D-052). The draft's "[N] frames" now holds the plan's figures: about 4,300 frames in the first five days (about 0.25 GB), and about 22,700 frames (about 1.3 GB) by mid-December, at most 1,000 requests a day, one at a time, at least 5 seconds apart. The regenerated plan gives about 23,700 frames (about 1.4 GB with the day searches) through mid-December, and about 3,000 more if you approve TQ (D-072; the plan's section 7). Use the newer figure, and check it before you send. The budget stays at 1,000 until NPS answers.
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
   - **Checks 6 and 7** use the speech library 0.2.0 (D-055). The README's Build block rebuilds `m1-spike` without speech, so the runbook's step 2.1 rebuilds it with `--features speech` right after, as the lead did at 12:36 UTC on 20 Nov (D-074). Then follow the runbook's step 3:
     - first run `~/lookout-data/cache/speech-macos/release/lookout-transcribe status --locale en-US`;
     - only if it reports code 1, run `~/lookout-data/cache/speech-macos/release/lookout-transcribe install-assets --locale en-US --yes`;
     - keep the "reservations" lines from before and after;
     - check that `m1-spike` is a 0.2.0 speech build: `strings -a ~/lookout-data/cache/cargo-target/release/m1-spike | grep 'lookout-speech 0.2.0'` prints one line.
   - Report every run, failed and inconclusive ones too. On a `FAIL`, stop and send the run file.
7. **Permissions in M1:** Microphone, when the app first asks; Speech Recognition probably not at all, since the library never asks for it (D-055); Screen Recording and Input Monitoring for the harness only; Accessibility for the injection check (open, D-033); Apple Intelligence on. Also confirm that a speech session with Speech Recognition "not determined" shows no prompt (D-055; runbook step 3).
8. **Replay recordings in M1,** once the recording tool exists (M1-09):
   - the 130 scripted lines (about 20 minutes; `bench/`, D-057), saying numbers as `check.py --reading-list` spells them, and the two consent questions;
   - then ≥ 30 real-use utterances.
9. **Confirm or change, by Mon 14 Dec:**
   - which renderer P1 scores, (a) the freeze commit or (b) the M4 renderer under a no-peek rule (D-019);
   - D-024's design per viewpoint, including the reading that Point Reyes's development data starts in summer 2023 (D-052);
   - **the backfill plan's TQ questions** (D-072): may design-H development use the dates between T0's first 16:9 frame and D-024's spans (Point Reyes 10 Apr – 14 Jul 2023, 58 entries; Grand Canyon 20 May – 14 Jul 2024, 51; Yosemite 5 Sep 2023 – 14 Jan 2024, 25; Acadia 26 Aug 2023 – 14 Jan 2024, 33; the Smokies 25 Mar – 14 Jul 2024, 65), and how D-024's spans read: from the archive check's sample date (the plan's reading), the whole named month (42 entries into T2), or T0's first 16:9 date (D-052's). Point Reyes's block, if approved, goes before T2 so it lands before the freeze, on 30 Nov (1 Dec at 3.7 % errors). T2 then lands on 11 Dec (12 Dec) and T1c on 16 Dec (17 Dec), past its 15 Dec target at both error rates;
   - whether earlier archived winters may feed design-P fits at Point Reyes (the backfill plan, section 10);
   - persistence (b) beyond a 24 h lead (D-036);
   - eval's readings of the held-out split (D-038), including the held-out edge: the local day only, which re-allows the six 31 May queue entries (about 92 requests), or the local or the UTC date, the lead's reading until you answer (D-070);
   - PRD Appendix A redrawn to the manifest schema's shape in v0.4.1, or v0.4's sketch with a note (D-044);
   - the parser's rule readings: "tonight", dates and the same day, "next friday", "tomorrow" = 12:00, and a lone generic word as a miss (D-039);
   - the newer readings (D-058): dawn and dusk at −6°, a passed "tonight at N" going to tomorrow, "tomorrow" as the place's date, horizons from now, and the last place after a miss;
   - three rule questions from the replay script (`bench/README.md`, "Rows waiting on the owner"): a fuzzy-only near-alias, a place name plus a leftover word, and a lone generic word.

   Two calls come earlier: D-035 before the first deploy (owner action 2), and D-041 before the first push (owner action 4).
10. **iCloud.** Consider moving the repo out of `~/Documents`, which would end the venv and build-output workarounds (D-031).
11. **Disk space.** 27 GiB was free of 460 GiB at 17:26 UTC on 20 Nov (94 % used; 19 GiB at 15:46 and 22 GiB at 10:38). The live policy adds about 262 MB a day (D-028), and the backfill through T1c about 1.3 GB (D-072). Apple Intelligence's on-device models take several GB. The lead can free some: the third workflow's critic listed deletable caches (the session scratchpad, `cache/cargo-target` and an old Docker image).

**Later** (PRD §1 rows 14–15):
- Several nights of unattended idle trials (M1), under `caffeinate -i` with the display allowed to sleep, as the lead decided (D-068).
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
| M0.5-01 Bring PLAN.md and DECISIONS.md to v0.4 (D-012 to D-021); start this file | done | [PLAN.md](PLAN.md), [DECISIONS.md](DECISIONS.md), this file (`a1fca21`); D-022 to D-045 since (`2e3b12d`, `535084b`, `034c46b`), D-046 to D-063 on 20 Nov (`8459e7e`), and D-064 to D-076 later that day (uncommitted) | — |
| M0.5-02 Data directory outside iCloud | done | DECISIONS.md D-017, D-031 | see "Machine and environment" |
| M0.5-03 Scout the 8 NPS feeds; fix the viewpoint IDs | done | DECISIONS.md D-020; `~/lookout-data/samples/nps-json/` | 8 of 8 feeds live |
| M0.5-04 NPS archive API and depth | done | DECISIONS.md D-018 | start dates below |
| M0.5-05 Email drafts: NPS, Golden Gate | done | [docs/outreach/](docs/outreach/) (`e4722a6`); the Rainier questions added on 20 Nov, and the backfill's frame counts later that day (D-052) | — |
| M0.5-06 Stopgap Rainier capture | done: ran 06:30–12:13 UTC on 19 Nov, then retired | DECISIONS.md D-021, D-023 | 24 frames; 4 outages (see "Outages") |
| M0.5-07 Repo scaffold: uv, pnpm and Cargo projects, viewpoint schema and manifests, per CLAUDE.md | done | `0bb42ab`; [docs/checks/](docs/checks/) (`c9ba843`, `951b704`; the later changes of 20 Nov uncommitted); DECISIONS.md D-063, D-075 | `make test`: 10 of 10 suites pass, 224 s (see "Test suites"); `make bench-script` and the new `test-speech` steps pass in scratch folders |
| M0.5-08 Capture worker, 8 cameras.<br>• Polite: fetch on change, ≤ 4 per hour with persisted state.<br>• Quality gates (PRD §8.6), stored at ≤ 1920 px.<br>• Rainier cadence measured. | in progress: 0.2.0 runs for all 8 cameras since 02:39:39 UTC on 20 Nov, with D-028's storage and the archive queue; 0.1.0 ran from 12:24:23 UTC on 19 Nov. 0.2.1, with polls and GETs spaced from the previous answer (D-065, D-066), is uncommitted and not running. | `workers/capture/` (`4fae4c1`, `df1838a`); DECISIONS.md D-023, D-028, D-029, D-047, D-048, D-049, D-065, D-066; [docs/research/nps-archive-check.md](docs/research/nps-archive-check.md) | Rainier updates about once a minute; 262 MB a day under D-028; since the restart, the shortest poll gap is 301.05 s send to send, and 1 of 1,008 pairs is not provably 300 s apart at nps.gov |
| M0.5-09 Forecast worker.<br>• HRRR byte ranges at the view-ray sample points.<br>• One JSON per viewpoint per run.<br>• Lag per forecast hour. | in progress: 0.1.0 runs as the local stand-in with 8 fields; 0.2.0 (survey lags, `tick`, the stamp floor) is committed (`08a996c`) and not running yet | `workers/forecast/` (`09e2ec8`, `08a996c`); DECISIONS.md D-026, D-027, D-030, D-050, D-053, D-054 | 39 of 39 hourly runs from 2026111900 to 2026112014 written at all 8 viewpoints; 4,082 survey lags; 576 of 576 model cells off the fallback |
| M0.5-10 Trace skeleton | done | `app/core/src/trace.rs` (`e7b87e8`) | no field can hold transcript text (a test walks the JSON) |
| M0.5-11 Infra code: Terraform (state backend, storage, CDN, budget alarm, Lambda + EventBridge Scheduler) and the Lambda image | in progress, code only: CloudFront and the API are TODO(M1); capture's S3 grants fixed for D-028; the provider installs offline; three local smoke runs of the forecast handler | `infra/terraform/`, `infra/lambda/`, `infra/cost/` (`0727ac8`, `6c385f5`, `e59247a`); DECISIONS.md D-034, D-059, D-060, D-066 | `infra/test.sh` 113 checks pass; estimate $2.07–4.03 in month 1 for capture 0.2.0 (D-060), $2.43–4.40 for 0.2.1 (D-066) |
| M0.5-12 AWS deployment | blocked on owner (owner action 2) | DECISIONS.md D-014, D-034, D-035 | — |
| M0.5-13 CI (GitHub Actions, CI only) | in progress: the workflow runs every `make test` suite but the laptop-only speech suite, and `docs/checks` keeps the two lists equal. Its first GitHub run needs a push (owner action 4). | `.github/workflows/ci.yml` (`0bb42ab`, `c9ba843`, `951b704`; comment changes of D-075 uncommitted); DECISIONS.md D-063, D-075 | actionlint 1.7.12: 0 findings (19 Nov) |
| M0.5-14 One backtest pair: an archived frame and an archived HRRR run → renderer v−1 → a crude extractor score, labelled "v−1" | done (committed in `d9aa416`); the run choice now applies the archived stamp floor, uncommitted, with this pair's choice unchanged (D-073) | [docs/evidence/m05-backtest-pair.md](docs/evidence/m05-backtest-pair.md); DECISIONS.md D-042, D-062, D-073 | Grand Canyon, 24 h lead: scored, a correct negative by accident; 2.6 s a pair; counts toward no metric |
| M0.5-15 NPS archive check: cadence, gaps and camera epochs per camera | done; six of the seven epoch switches dated by the backfill's T0 on 20 Nov (Grand Teton was not queued, and needs no date without design H); the check's to-do closed on that basis | [docs/research/nps-archive-check.md](docs/research/nps-archive-check.md) (`4fae4c1`, `2e3b12d`; Grand Canyon corrected in `6b94942`); DECISIONS.md D-022, D-042, D-052 | 217 requests; every ARD camera went 3:2 → 16:9 in 2023–25; T0 dated six of the switches to within 14 days (26 at Grand Canyon) |
| M0.5-16 H or P per viewpoint in DECISIONS.md | done | DECISIONS.md D-019, D-022, D-024, D-042 | — |
| M0.5-17 Emails sent | blocked on owner (owner action 3) | — | — |
| M0.5-18 **Gate:** ≥ 72 h of worker and capture logs | in progress: the clock runs from 12:24:23 UTC on 19 Nov for all 8 cameras (06:30 UTC for Rainier). Under the lead's rule (D-069), both jobs' outages and each capture restart's excess extend it, and it closes at 23:05:58 UTC on Sun 22 Nov at the earliest. PLAN.md's capture-only figure, kept for comparison: 22:44:12 UTC. | `~/lookout-data/capture_log/`, `forecast_log/`; `lookout-eval data-report` (D-061, D-069) | 10.69 h lost to 20:01:58 UTC on 20 Nov: 9 loop outages, two restarts' excess (185.5 s at 02:39 and 763.6 s at 19:44, D-079) and 5 forecast outages, the overlaps counted once (see "Outages"); 20.93 h covered |
| M0.5-19 A council round on the parts taken from Lookout, adding a forecast-verification judge | not started (optional) | PRD §18.2 | — |
| M0.5-20 Local stand-ins until AWS is live: the capture loop and the forecast tick | in progress: capture 0.2.1 from the snapshot `3cd2ca7` since 19:56 UTC on 20 Nov (`034c46b` from 02:39 UTC, `cfefa79` before); the forecast tick still from `034c46b`, each with its own venvs; the capture loop runs under `caffeinate -s`, and the forecast tick relies on it. Both are children of the lead's session; the lead's supervisor is written but not started (owner action 1, D-076) | DECISIONS.md D-030, D-049, D-050, D-051, D-076, D-079 | 245 loop passes in the data report's window to 20:01 UTC (69 of 0.1.0, 174 of 0.2.0, 2 of 0.2.1); the forecast tick from 12:18 UTC on 19 Nov |

### M1: Voice to first pixel (Mon 23 Nov → Mon 7 Dec) · not started, with some core logic started early

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M1-01 Platform spike: one day, 11 checks (PLAN.md 1a) | in progress (early): the spike app is built; none of the 11 checks has run (owner action 6). Checks 6 and 7 use the speech library through the spike's `--features speech` build, which links the real library: 0.1.0 at 03:39 UTC on 20 Nov, and 0.2.0 since the lead rebuilt it at 12:36 UTC (D-074). | `spikes/m1-platform/` (`b73c53b`); [docs/m1/owner-runbook.md](docs/m1/owner-runbook.md), step 3; DECISIONS.md D-032, D-033, D-055, D-074 | 105 unit tests; the speech build 12,991,256 bytes with 1,013 `LookoutSpeech` symbols; see "M1 preparation" |
| M1-02 `lookout-core`: query lifecycle, `query_gen` cancellation, trace | in progress (early) | `app/core/` (`e7b87e8`) | key-mash: 40,000 cycles, 0 stale paints |
| M1-03 Parser fast path and time rules.<br>• ≥ 150 table-driven cases and property tests.<br>• DST and cross-zone cases. | in progress (early): some rule readings wait for the owner (D-039, D-058); back-references ("same place") are to be built on dev row d06, never on a test row (D-057) | `app/core/tests/parser_table.rs`, `properties.rs` | 295 table cases; parse p95 124–126 µs |
| M1-04 Ephemeris wrapper and solar events over oculus-core `1b5410b` | in progress (early) | `app/core/src/solar.rs`; DECISIONS.md D-025 | sunrise and sunset within 3.9 s of NOAA; Moon within 0.355° |
| M1-05 Speech A1: `DictationTranscriber` through the Swift static library | in progress (early): library 0.2.0 built, unit-tested on a mock engine and committed (`8443bf3`); never run against the real recognizer, which needs the owner (owner action 6). Its silence pad stays 0 ms by default, set and logged by the shell and harness (D-067). | `app/speech-macos/`; DECISIONS.md D-055, D-056, D-067 | 103 tests; push p99 ≤ 5.12 µs; see "M1 preparation" |
| M1-06 Speech D: Deepgram Nova-3, bake-off only | blocked on owner (owner action 5) | DECISIONS.md D-015 | — |
| M1-07 Overlay with Tier 0: states, caption, chips, destination chip, focus model, paint gate | not started | — | — |
| M1-08 Tier 0: library index, Sun-position match, background decode | not started | — | — |
| M1-09 Replay and paint-capture harness | not started: the recording tool, the scorer and two trace fields are TODO(M1); the scoring rule is proposed (D-057) | `bench/README.md` | — |
| M1-10 Replay recordings: ~130 scripted, plus ≥ 30 real-use | in progress (early): the script is ready and committed, nothing is recorded; recording waits for M1-09 and the owner. `make bench-script` checks it against the real parser (D-075). | `bench/utterances/scripted.tsv` (`dc28c2a`); DECISIONS.md D-057, D-075 | 130 rows (30 dev, 100 test), script hash `3b4256f1cf51`; the parser alone gets 94 of 100 test rows exact on the text (re-run by `make bench-script` at 15:56 UTC: the same) |
| M1-11 macOS permissions | not started (owner, in M1): the runbook names all five grants, and when to switch each off | [docs/m1/owner-runbook.md](docs/m1/owner-runbook.md), step 1 | — |
| M1-12 Overnight idle trials: ≥ 100 for the shipped engine, ≥ 60 for the comparison engine | not started: the lead decided that idle trials keep the system awake and let the display sleep, with display-asleep trials reported apart, and that follow-ups may be idle (D-068) | [docs/m1/owner-runbook.md](docs/m1/owner-runbook.md), step 6 | — |
| M1-13 Stale-paint stress test, ≥ 10,000 cycles | in progress (early): the Rust model of the paint gate passes; the webview gate is M1 work | `app/core/tests/keymash.rs` | 40,000 cycles, 0 stale paints; 3 broken gates caught |
| M1-14 README skeleton with blanks | done (early), committed in `dc28c2a`: the headline sentence and the metrics table, every number a blank | [README.md](README.md) | — |
| M1-15 **Gate:**<br>• replay report;<br>• ≥ 150 parser cases;<br>• STT decision;<br>• 0 stale paints;<br>• a screen recording over another app. | not started | — | — |

### M1.5: Walking skeleton (by Thu 10 Dec) · not started

| Ticket | Status | Evidence | Measured |
|---|---|---|---|
| M1.5-01 Renderer v0, headless: Hillaire sky, cloud slabs, ceiling slab, flat fog, camera v0 | in progress (early): renderer 0.2.0 has cloud slabs, a ceiling slab, fog and camera v0; the Hillaire sky is TODO(M1.5) | `renderer/` (`b5bc726`) | see "Renderer" |
| M1.5-02 First Rainier plate: base frame, pose, summit band. Needs a captured midday frame with the summit visible. | not started | — | — |
| M1.5-03 Extractor v0 | not started (extractor v−1 exists in eval) | `eval/src/lookout_eval/extractor.py` | — |
| M1.5-04 **Gate:** dev-only P1 table against persistence, climatology and S_rule | not started | — | — |
| M1.5-05 **Gate:** cloud Monte Carlo test | in progress (early): passes on renderer 0.2.0; re-run on the M1.5 renderer | `renderer/src/cpu/montecarlo*.ts`; `pnpm montecarlo` | within 1.54 points of the view-ray target at 1,000 seeds |
| M1.5-06 Design-H archive backfill: T0, T1a, T1b, T3pr, T2, T1c (not a gate item) | in progress: the lead queued T0 and T1a (447 entries) at 09:50 UTC on 20 Nov; T0 is done, T1a is running. The lead removed the six 31 May entries and resumed Big Bend's failed one (D-070, D-071). The plan, regenerated from the live queue, puts T3pr ahead of T1b: T3pr lands 4 days early, T1a and T1c on their targets, and T1b and T2 a day late at 1.0 % errors (D-072). | [docs/research/archive-backfill-plan.md](docs/research/archive-backfill-plan.md) (`6b94942`; regenerated, uncommitted); DECISIONS.md D-052, D-070 to D-072 | At 11:43 UTC the day's 1,000 requests were spent. Queue at 15:47 UTC: 441 entries, 180 done, 1 active, 260 pending. Finish days at 1.0 % errors: T1a 24 Nov, T3pr 25 Nov, T1b 30 Nov, T2 11 Dec, T1c 15 Dec |

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
| Capture loop, restart on 0.2.0 (D-049) | 20 Nov 02:36:58–02:37:11 → 02:45:04–02:45:15 (each camera's last 0.1.0 poll → its first 0.2.0 poll) | 483.080–485.471 s between polls at each camera | 185.471 s under the gate rule (Acadia's excess, 02:41:58.471 → 02:45:03.942; D-069); none under PLAN.md's earlier 540 s rule |
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

- **How:** `lookout-eval data-report --until 2026-11-20T10:30:00Z` (read-only, D-061), checked against `capture_log/` and `pmset -g log`; re-run with the gate rule to 13:15 UTC (the eval package) and to 15:46:36 UTC (the docs agent, D-069), with the same outages.
  - A capture outage is a gap of over 540 s between passes (the data report's threshold); the lost time is the gap less one 300 s interval. The 02:39 restart's 147.5 s pass gap is not one, though each camera then went 483–486 s between polls.
  - A restart's excess (the gate rule, D-069) is the longest any camera went from its last poll before the restart to its first poll after it, less one 300 s interval, counted as the interval from that camera's last poll + 300 s to its next poll.
  - A forecast run is due when its last extracted hour is on S3 (its newest `LastModified`). A forecast outage is a due run with no live run finished within 900 s of that time; it runs from the due time, the table's start, to the finish of the next live run written, and the time is lost from due + 300 s, so lost = length − 300 s.
  - The stopgap rows are the earlier hand count: missed = round(gap ÷ 5 min) − 1.
  - The passes at 15:09 and 18:03 on 19 Nov, and 05:02, 05:42 and 06:18 on 20 Nov, ran in brief dark wakes.
- **Totals to 15:46:36 UTC on 20 Nov** (27.37 h elapsed): the gate rule's union is 10.48 h lost (capture outages 10.208 h, the restart's excess 0.052 h, forecast outages 6.039 h, the overlaps counted once), so 16.89 h are covered. Capture's outages alone: 10.21 h (8.22 h on 19 Nov, 1.99 h on 20 Nov). No outage since 06:29:29 UTC.
- **The 72 h clock** (the lead's rule, D-069): its earliest close is **23:05:58.495 UTC on Sun 22 Nov**, if nothing more is lost (the data report to 20:01:58 UTC on 20 Nov; it was 22:53:14.871 before the 19:44 re-snapshot, D-079).
  - PLAN.md's earlier rule (the capture loop's outages alone), kept for comparison: 22:44:12.708 UTC. Capture alone, with the restarts' excess: 22:52:40.352 UTC. Forecast alone: 18:26:42.991 UTC.
  - A new outage moves the close later. The re-snapshot's restart cost 763.6 s of excess, not the planned 60–70 s, because the Mac slept through it (D-079).
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
| Poll spacing since the restart (capture 0.2.0, the stamp at the send and a 1 s margin) | 70 polls per camera from 02:39:39 to the pass that ended at 10:45:05 on 20 Nov; the shortest gap between sends is 301.053–301.064 s, with a median of 312.8–314.9 s; none is under 300 s. To 15:46:36: 127 polls per camera, the shortest send gap 301.053–301.058 s. | `request` records (JSON GETs and Rainier HEADs) in `capture_log/2026-11-20.jsonl`, send = log time − `elapsed_ms`; `lookout-eval data-report --since 2026-11-20T02:39:39Z` to 15:46:36 (read-only) |
| Polite collection at nps.gov, provable (D-065, D-069) | **Polls:** since 02:39:39 on 20 Nov, 1 of 1,008 consecutive pairs is not provably 300 s apart at nps.gov: Acadia, least possible gap 299.588 s (send to send 301.066 s). The other cameras' least gaps are 300.690–300.950 s. Since 12:24:23 on 19 Nov: 10 of 1,560 pairs, the other 9 from 0.1.0. **Image GETs:** none of 316 five-GET windows since 02:39:39 is unproven (least span 3,720.229 s), nor of 515 since 19 Nov (least 3,612.816 s, Rainier). "Unproven" means not provably 300 s (3,600 s) apart, not shown closer. | `lookout-eval data-report` (eval's working tree) to 15:46:36 UTC, read-only: the next poll's earliest receipt (`t − elapsed_ms` − 0.05 ms) minus the previous one's latest (`t` + 1 ms) |
| Capture 0.2.1 with the answer-time rule (fake clock, no network; D-065) | **The 2 ms send-to-send rule it replaces,** on the exact Lambda schedule: 1,027 of 2,296 poll pairs under 300 s at the server in 24 h (down to 296.659 s), 11,050 of 23,032 in 10 days. **This code:** 0 under 300 s in every run (24 h and 10 days; storage delay, 0–2 s of jitter, 1 % request spikes); provable minimum 300.002 s; every 5 GETs span ≥ 3,601.0 s. Skips: 1.12 per camera per day over 24 h and 1.43 over 10 days with the measured request times, each a gap of about 555–622 s; 94–95 images of 96 per camera per day; passes p99 about 46–52 s, at most 83.5 s with the log's timeouts in the pool (the review). `loop` 72 h with 0–30 s jitter: 0 of 6,592 pairs under 300 s, no skip. | The capture package's simulator: the real capture code against a fake nps.gov that logs receipts, request times drawn from 1,024 real 0.2.0 answers; the review's other latency models gave 1.95–2.98 skips (3.9 with 0–30 s of invocation jitter) |
| Capture 0.2.1's guards (D-066) | Failed after it may have been sent: the next allowed poll 597.0–599.5 s after the server read the failed one (296.0–299.5 s before). A same-host redirect: 5 polls at nps.gov and 5 logged per camera in 2 h (48 against 24 before). A rollback to 0.2.0 right after a crash: 0.2.0 waits 360.7 s (1 s before). An hour's sleep between reservation and send: nothing sent (the poll went out an hour late before). Live failures in the log: 4 of 2,065 requests (12:24 on 19 Nov to 15:17 on 20 Nov), 3 of which the 300 s bound would now cover. | The reviews' reproducers, run before and after the fix (loopback 127.0.0.1, fake clocks, moto S3); `capture_log` copies |
| Rainier image GETs, stopgap plus capture (corrects "combined maximum 4") | **5** in the rolling hour from 10:56:28, against the cap of 4; two HEADs 53 s apart (D-023) | `fetched_at_utc` of the Rainier sidecars |
| Loop requests and errors, 19 Nov | 433 requests logged one by one, 12:24–23:59: 272 JSON GETs, 118 image GETs and 39 HEADs answered 200; 3 image GETs answered 404 (Yosemite twice, Grand Canyon once) and 1 JSON GET was reset (Big Bend). Each of the 4 errors was followed by a back-off. | `request`, `error` and `backoff` records in `capture_log/2026-11-19.jsonl` |
| Loop requests and errors, 20 Nov to 10:30 | 1,048 live requests: 677 JSON GETs, 97 HEADs and 267 image GETs answered 200; 4 image GETs answered 404 (Yosemite twice, Big Bend, Acadia); 3 failed in transit (an Acadia connect timeout, a Rainier read timeout and a Grand Canyon reset). Each of the 7 errors was followed by a back-off. The archive queue's own error is under "The NPS archive". | `request`, `error` and `backoff` records in `capture_log/2026-11-20.jsonl` |
| Host back-off (capture 0.2.0) | After a live 503 asking for 30 min, archive requests sent during the hold: 89 before the fix, 0 after. After an archive 503: a hold is set and the next live pass sends 0 HEADs. | Scratch probe with a fake clock, no network; kept as tests |
| A store failure (capture 0.2.0) | One unchanged image over 2 h with storage failing: fetched 8 times before the fix, once after | Fake feed and refused writes; kept as a test |
| Night frames (colour mode) | Chroma spread on night frames: 0.0002–0.27 at the ARD cameras (solar altitudes of −11 to −49°), 0.011 at Rainier. The greyest daylight frames read 3.2–5.2 (Yosemite, Grand Teton). Acadia's only dark frame, at a solar altitude of −8.9°, read 12.9 (colour). | Sidecar `chroma_spread` (metadata only), with the NOAA solar equations |
| Sharpness (Clear lens input) | Two foggy midday archive frames 2.4 and 2.7; seven other midday hires 31.4–2,073.5; night frames 1,031–1,638 | Laplacian variance at ≤ 640 px on stored frames |
| Test suite | 249 passed (capture 0.2.1, the working tree), plus 4 slow schedule tests that the default run deselects and `make test` runs on purpose (4 passed; 81 s on a quiet machine, 241 s under load; D-075); ruff check, format and `uv lock --check` clean. 19 of the new server-side, schedule and crash tests fail on the code before the answer rule, and 14 of the 16 fix-round cases on the code before the fixes. Earlier: 191 at 10:31 on 20 Nov; 182 at the commit of 0.2.0 (`df1838a`). | The capture fixer's runs, 15:20–15:27 on 20 Nov |

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
| Archive queue, first tranche (T0 + T1a, 447 entries) | Queued 09:50:30–09:50:37; first request 09:52:25. At 10:39: 146 done, 1 active, 300 pending. T0: 134 of 134 entries, 256 requests, 134 frames, 8.6 MB of frames (the plan: 256 requests and 7 MB). T1a: 13 entries started, 198 requests, 184 frames. 454 of the day's 1,000 requests: 15,994,126 bytes of frames and 6,705,492 of search answers, none refused. One error: a TLS connect timeout at 10:37:10. To 10:30, 129 searches and 238 `hires` GETs, all answered 200. **At 11:43:23 the day's 1,000 requests were spent,** 1.85 h after the first: 990 answered 200, and 10 failed in transit (6 `hires` connect timeouts, 4 searches), each followed by a wait of 30 s or more; 52,709,139 bytes, 44,196,432 of them frames. After three failed searches, Big Bend's 2025-06-14 entry was marked failed at 11:30:05. At 11:47 the queue held 180 done, 1 active, 1 failed and 265 pending, waiting for the next UTC day's budget. **At about 12:40 the lead removed the six 31 May entries and resumed Big Bend's** (D-070, D-071). At 15:47: 441 entries, 180 done, 1 active (`point-reyes.beach/2025-07-19`), 260 pending; the last date 2026-05-30; 1,000 of the day's 1,000 requests spent. | `lookout-capture archive queue list --json` (offline; 10:39, 11:47 and 15:47, the last with the working tree's code) and `archive_request`, `error` and `queue_failed` records in `capture_log/2026-11-20.jsonl` |
| Archive request spacing, end to start (D-072) | 20 Nov, 999 pairs: 991 at 5.0009 s or more, 7 at 5.0004–5.0008 s, and **1 at 4.9944 s** (09:56:47.412 → 09:56:52.672), 5.6 ms under the 5 s rule on the log's stamps; median 5.0109 s, 5th percentile 5.0023 s. 19 Nov, 216 pairs: least 5.0005 s, median 5.0091 s. | The planner fixer's exact decimal count on copies of `capture_log/` (start = `t` − `elapsed_ms`); whether the one pair started early or the wall clock was adjusted, the log can't tell |
| Backfill time model | 1.76 h of loop time to spend 1,000 requests without errors (49.5 requests per idle window), 2.10–2.11 h at 19 Nov's 3.7 % error rate. Re-measured on 20 Nov's own inputs: 1.71 h without errors, 1.835 h (1.816–1.852 h) replaying 20 Nov's outcomes against the real loop's 1.849 h (a check on its own day), 2.07 h at 3.7 % independent errors. Without a daily resume, 7 of 16 multi-day runs ended with failed entries; with it, every run of 4 seeds met the schedule's days. | `loop_sim.py`: capture's real `QueueRunner` on a fake clock against a mock nps.gov, 20 seeds per scenario (the backfill plan, section 6; D-072) |
| The regenerated plan (D-072) | Requests still to send: T1a 3,864, T1b 4,812, T3pr 271, T2 10,975, T1c 4,715; TQ, for the owner, 3,253. Finish days at 1,000 a day from 21 Nov, T3pr first, 1.0 % (3.7 %) errors: T1a 24 (25) Nov, T3pr 25 Nov, T1b 30 Nov, T2 11 Dec, T1c 15 (16) Dec. Against PLAN's targets, at 1.0 % errors T1b (29 Nov) and T2 (10 Dec) are missed by a day, and T1a, T3pr and T1c are met; at 3.7 % T1a and T1c are also a day late. Frames through T1c about 1.22 GB (0.49–1.91 GB). | `eval/tools/backfill_plan/plan.py` on a read-only queue copy of 12:47:40 UTC; 44 tests, 33 of 33 mutants caught; an independent stdlib reproduction (the review) |
| Archive request sizes | `hires` 54,141 bytes on average (n = 9, 10,419–101,326) before the queue; a day's search answer 49,372 bytes (n = 92); request time median 215 ms. From the queue's 20 Nov frames: 689 hourly `hires` frames, all 999×562, 51,734 B on average (Point Reyes 31,316 B to Yosemite 79,973 B); search median 217 ms, GET median 235 ms. | Pre-guard sidecars and the 19 Nov capture log (the backfill plan, section 6); the queue's sidecars and 20 Nov log, read by `measure_inputs.py` (metadata only) |

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
| Local stand-in | All 39 hourly runs from 2026111900 to 2026112014 written at all 8 viewpoints (files listed at about 16:00 on 20 Nov; the data report to 15:46 finds none never written, 0 errors and 443 distinct live lags in its window). 19 Nov: the 7-field f00–f02 profile, 20.8–22.3 MB of GRIB for each of the 22 runs the stand-in fetched and for the builder's 10Z smoke run; the builder's 08Z smoke run fetched 25.4 MB (12 GRIB requests); first tick 12:18. 20 Nov from 02:39:49: 8 fields (D-050). A one-time re-extraction of 12 runs (2026111914 to 2026112001) took 293.5 MB, then 8 new runs 23.8–25.1 MB each. 419 distinct live lags in the data report's window to 10:30. | `forecast_log/` `run` and `lag` events; `lookout-eval data-report` |
| Forecast files on disk | 241 of 241 validate against 0.2.0's schema and floor checks (240 live, 1 archived; all written by 0.1.0); at 11:45, 281 of 281 (280 live). A live file written by 0.2.0 has 0 errors under the 0.1.0 schema; an archived one has 2, or 4 when the stamps win. | The forecast fixer's read-only checks, 06:11 on 20 Nov; re-run at 11:45 (`validate_document` over every file, and the fixer's fake-bucket schema check) |
| Live smoke, final code | Run 2026111910, f00–f02, 7 fields, 8 viewpoints: 21,440,626 bytes (10 range requests, 3 `.idx`, 1 listing), 3.46 s wall; 8 valid files; no GRIB left behind | `tests/test_live.py`, 12:01 |
| Whole-run sizes, all 22 fields | 453.0 MB for an 18 h run, 1,202.1 MB for a 48 h run, with terrain fetched once | `lookout-forecast plan` (inventories only) |
| Grid mapping | Max error 1.30e-11 km against ecCodes' own coordinates over 1,905,141 cells; every cell maps back to itself | `lookout-forecast grid-check 2026111907` |
| Datums and the phantom cloud base | See D-026: ceiling and top never below the model terrain; HPBL above ground; 12.4 % phantom `HGT:cloud base` on 07Z f00 (19.8 % on 09Z f03, in review) | `lookout-forecast datum-check 2026111907 --fh 0` |
| Model terrain at the camera cells | Against the manifest elevations: Rainier +13.9 m, Grand Teton +107 m, Acadia −35 m, Big Bend −90 m, Smokies −174 m, Yosemite −182 m, Grand Canyon −920 m (the cell is over the canyon), Point Reyes −150 m (the cell is sea) | `datum-check` camera cells |
| Herbie trial | 4 requests for one field-hour, all as `python-requests/2.34.2`, two with no timeout; byte range identical to ours | Logged trial, 19 Nov (D-026) |
| NOAA download, builder | About 132.5 MB in all on 19 Nov | The forecast builder's transfer ledgers, in its first-workflow report |
| NOAA download, 20 Nov to 10:39 | Stand-in: 530.4 MB of GRIB in 22 run events (41.5 MB for 2 runs on 7 fields, 293.5 MB for the re-extraction, 195.4 MB for 8 new runs). Forecast package: the survey's 197 listing pages (3,181,611 bytes; its ledger counts 194, 3,160,172 bytes, D-053) and 10 more listings of archived runs (221,729 bytes), no GRIB. Lambda smoke run 3: 7.25 MB (D-059). | `forecast_log/2026-11-20.jsonl` `run` events; the forecast and infra reports |
| Test suite | 177 passed, 1 network test deselected (forecast 0.2.0, committed in `08a996c`); ruff check, ruff format --check and `uv lock --check` clean; eval's pair contract 13 of 13 in forecast's environment. Before (00:47): 135 passed, 1 failed. | `make test`, 10:31 on 20 Nov, and the forecast fixer's run |

### Evaluation and the backtest pair

| What | Value | How and when |
|---|---|---|
| Backtest pair, v−1 | Grand Canyon, valid 2024-01-15 19:45, 24 h lead, HRRR run 2024011412 f31–f32. Primary box: truth 0.330 (hidden), render 0.504 (partial): a correct negative, by accident (both v−1 errors push towards "not visible"). With the measured 5 % re-aim applied, the truth box reads 0.360, across τ_lo (D-042). 2.61 s a pair, 2.15 s of it the render; 4 runs, byte-identical scene and render. Since run 4 the record says `counts_toward_metrics` false, as for every v−1 pair. Committed in `d9aa416`. | [docs/evidence/m05-backtest-pair.md](docs/evidence/m05-backtest-pair.md), 23:13–23:35 on 19 Nov and 00:16 on 20 Nov |
| Held-out split | Version 1, SHA-256 `c437544e…`; on the real catalogue 0 of 85 archive records and 0 of 105 live frames are held out; 47 of 48 mutants caught | `measure_real.py` (metadata only), 22:45 (D-038) |
| The held-out edge's UTC reading (D-070) | Capture's and eval's guards agree on 6,000 generated cases (eval's parity fuzz, the builder) and on 130,000 more (the capture review's own fuzz, built from true instants): 0 disagreements in decisions or date sets. In the review's fuzz no asset whose true UTC or civil date is on or after 1 Jun 2026 was allowed. It refused 10,384 of the 58,238 truly allowed cases (8,004 of 44,744 in its 100,000-case run), every one with a mutated stamp (fail-closed), and none of the 13,139 whose stamps were both as NPS writes them. The guard turns at 00:00 UTC on 1 Jun: 16:00 local standard time in the Pacific zone, 17:00 in the Mountain zone and Arizona, 18:00 in the Central and 19:00 in the Eastern zone. The running 0.2.0 still allows those 31 May evening assets; the queue holds none. | eval's parity fuzz loading capture's `heldout.py`; the capture review's `fuzz_heldout.py`, seeds 1 (30,000 cases) and 7 (100,000), re-run by the docs fixer on the working tree with the refusals split by mutation |
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
| The data report with the gate rule (D-069), window 12:24:23 on 19 Nov → 15:46:36 on 20 Nov (27.37 h) | **Clock** (the lead's rule: both jobs' outages and each restart's excess): 10.48 h lost, 16.89 h covered, earliest close **22:53:14.871 UTC on Sun 22 Nov**. PLAN.md's capture-only figure: 10.21 h, 22:36:51.258. Capture alone: 22:39:56.728. Forecast alone: 6.04 h, 18:26:42.991. The 02:39 restart's excess: 185.471 s (Acadia), inside D-061's 183–186 s, overlapping no outage. **Capture:** 197 passes (69 on 0.1.0, 128 on 0.2.0); 196 polls and 68–70 image GETs per camera, at most 4 GETs in any rolling hour. **Forecast:** 29 runs first written in the window per viewpoint, 2 of them catch-ups; 0 errors. **nps.gov bounds:** see Capture. | `lookout-eval data-report --until 2026-11-20T15:46:36Z` (eval's working tree, read-only, 0.6 s); the same closes to 13:15 in the eval package's run, checked to the millisecond by an independent stdlib script |
| oculus-core provenance (D-062) | The four pair records say `0.1.0+06809e6`, but the build that ran was `1b5410b`. Its `direct_url.json` names that commit; it was built at 12:31:59 on 19 Nov and was in `eval/.venv` by 22:08 at the latest, both before run 1 (23:25). Later records name the installed build (`0.1.0+1b5410b`). | `stat`, `shasum` and `json.load` on the venv, the uv cache and the records (read-only) |
| Test suite | 403 passed, 1 skipped (the opt-in render), on the working tree at 14:45 on 20 Nov: data-report tests 58, pair-forecast tests 25, held-out tests 62; 59 of 60 mutants killed (D-069). The backfill planner's own 44 tests are separate (`eval/tools/backfill_plan/`, not collected by `make test`). Earlier: 367 passed, 1 skipped at 10:31 (data-report tests 40); 318 passed, 2 skipped (01:46); at the held-out fix (D-038), 262 passed, 1 skipped. | The eval fixer's run (dead proxies, `PYTHONPATH=src:../workers/common/src`); `make test`, 10:31 |

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
| Hosting estimate, 20 Nov (computed, not measured; D-060) | For the committed capture 0.2.0 (`8246a8b`), with the free tiers: $2.07–4.03 in month 1, $2.32–4.29 in month 2, $2.53–4.52 in month 3, then about $0.22 more each month. Without them: $5.63–9.83 in month 1. S3 requests $1.87–1.92; Lambda compute up to $1.81. D-035's $1.60–2.81 is superseded. **Capture 0.2.1 with the answer-time rule** (D-066): $2.43–4.40 in month 1 at 36 s of waiting a pass ($2.43–3.35 with a 20 s Lambda window); the bottom of the range passes $3 in month 4 instead of month 6, and the top's hard stop comes about day 25.7 of month 1 instead of 26.7. The earlier 0.2.1 figure ($2.07–2.81) came from a zero-latency simulation. | `infra/cost/estimate.py` on `cost/sim_capture_8246a8b.json`: 24 h of the committed capture code against a mock S3 bucket (moto) and fake feeds; AWS list prices of 12–18 Nov. For 0.2.1: the capture package's run of the same tools on scratch exports, with the waiting from its latency simulation |
| Capture's simulated S3 traffic and waiting | One pass makes 50–216 S3 requests, 73 on average; per day 8,944 PUT, 174 LIST, 7,902 GET, 702 HEAD and 3,420 DELETE. A pass waits 35.7 s on average before its own work (32.8 s for slots, 2.9 s spacing), from 0.2.0's 1 s poll margin. **Capture 0.2.1:** 11,048 PUT and 10,176 GET a day (+2,104 and +2,274; state and lock PUTs 7,624 against 5,360), and about 36 s of waiting a pass with real request times; the 14.6 s quoted before came from a zero-latency simulation. | The same simulation (`infra/README.md`, "Monthly cost estimate"); the capture package's (D-066) |
| Lambda image builds | Cold build 20.0 s; rebuild after AWS republished the base 15.8 s; rebuild with nothing changed 1.1 s; rebuild of `8246a8b` 12.2 s (tag `8246a8bd570b`). 293.8 MB compressed (the pinned AWS base 200.3 MB); 1.25 GB unpacked. Both base images are pinned by digest. | `infra/lambda/build.sh`, Docker Desktop 29.5.3, BuildKit 0.30.0, native linux/arm64, 20 Nov |
| Lambda smoke runs (forecast handler under the Runtime Interface Emulator, 2048 MB, 1.16 vCPU, tiny profile) | Run 1 (00:17): init 267 ms, first call 1,278 ms (3,588,572 bytes from NOAA, 11 files), second call 187 ms, peak 137.8 MiB. Run 2 (00:26): 299 ms, 1,222 ms, 209 ms, 138.4 MiB. Run 3 (06:59, image of `8246a8b`): 232 ms, 2,696 ms for 2 runs (7,254,750 bytes, 20 files), 38 ms, 144.8 MiB. No GRIB left behind. Laptop numbers, not AWS. | `infra/lambda/smoke.sh` (`INIT REPORT`, `INVOKE RTDONE`, the cgroup's `memory.peak`); `infra/README.md`, "The Lambda image" |
| EventBridge Scheduler free tier (us-east-1) | "$0 for the first 14 million scheduled invocations" | AWS Price List file published 18 Nov 2026 (D-014) |

### lookout-common and the docs checks

| What | Value | How and when |
|---|---|---|
| Limiter across processes with `LOOKOUT_STORAGE=s3` | Minimum gap between request starts: 23 µs before the fix (in-process mode), 1.014 s at a 1.0 s interval after (file mode) | 2 spawned processes × 2 `hold()` each |
| Torn JSONL line | Before: the next append glued onto the fragment and every later read failed. After: both backends read every record, and the fragment is reported in `bad_lines`. | Scratch scripts on a temp file and on a moto S3 bucket |
| Test suite | 258 passed; ruff check and format clean; 8 of 8 manifests valid | `uv run pytest -q` in `workers/common`; again in `make test` at 10:31 on 20 Nov |
| Docs checks | 16 invariant checks, 91 tests, all passing, 2.4 s; ruff check and format clean on `docs/checks` (4 files); `notices`, `appendix-a` and `ci-suites` are the three added on 20 Nov (D-063). The 6 newest cover the `decisions` check's back-pointer rule, added after the docs review: it found 6 problems in the builder's DECISIONS.md and none in the fixed one or in HEAD. Before: 85 tests at 16:10, and 13 checks with 45 tests on 19 Nov. | `make test-docs` with `UV_OFFLINE=1`, 17:20 on 20 Nov, after this update; `make test`, 10:31 |

### M1 preparation

| What | Value | How and when |
|---|---|---|
| Spike unit tests | 105 passed; clippy `-D warnings` clean with and without `--features speech`; four release binaries build (`m1-spike` 12,591,144 B) | `cargo test --workspace` in `spikes/m1-platform` |
| The spike's speech build (D-074) | Rebuilt by the lead at 12:36 on 20 Nov in 13.6 s: `m1-spike` 12,991,256 B, embedding "lookout-speech 0.2.0 (SpeechAnalyzer + DictationTranscriber, on device only; macOS SDK 260200)", 1,013 `LookoutSpeech` symbols, Speech.framework linked; signed ad hoc as `m1_spike-e8e55116026759cb`, Info.plist not bound. The 03:39 build linked the real 0.1.0 (989 symbols). | `cargo build --release --offline --locked -p m1-spike-app --features speech`; re-read by the docs agent at 15:41 with `strings`, `nm`, `otool -L` and `codesign -dv` |
| Rust `Instant` is `CLOCK_UPTIME_RAW` | 3 of 3 runs: offset −12 to −84 µs, drift +1 to +146 ns over ~150 ms | `instant_is_the_host_clock_on_this_machine` |
| `nettop` output when piped | Held back: all 4 headers arrived together at 3.0 s; under `script` they came each second | Timestamped header lines only |
| Torn pixel-tag strips | 0 of 29,930 consecutive-tag cuts decode as a third tag | Exhaustive unit test |
| The 11 platform checks | **none run yet** (they need the owner at the keyboard) | — |
| Speech library 0.2.0: tests and build (D-055) | 103 passed (76 before the review), 0 failed; Swift warnings are errors in every target; ShellCheck clean. `liblookout_speech.a` 907,616 bytes, 10 exported `_lk_speech_*` symbols, identical to 0.1.0's; the same 58 autolink entries, and 400 imports, 3 fewer than 0.1.0. A C host links it with the minimal flags and runs. `make test-speech` now runs `build.sh --no-test` after `swift test` (D-075): in a scratch data folder at 15:57, 103 passed, then a cold release build of 6.1 s, the signing check, check-symbols (400 imports, 39 selectors), the self-test (20 mutants) and the link check all passed; 63.7 s in all. | `scripts/build.sh` (swift test, release build, symbol check and its self-test, link check), 20 Nov; again in `make test` at 10:31; `LOOKOUT_DATA_DIR=<scratch> make test-speech` at 15:57 |
| Speech library: timing on a mock engine | `lk_speech_push_f32` (480 frames): p50 0.21–0.88 µs, p99 2.71–5.12 µs, 0 of 30,000 calls over 1 ms across six runs. `lk_speech_start` at 48 kHz: p50 0.737–0.963 ms. `lk_speech_cancel` waits 1.26–1.73 ms during a 19 s backlog, and 5.2–13.5 ms during a silence pad at 10 ms per buffer, against about 200 ms without the pad's cancel check. | `PushLatencyTests`, `StartLatencyTests`, `CancelEdgeTests`, host clock (CLOCK_UPTIME_RAW), load average 2–5 |
| Speech library: lifetime and privacy checks | 0 callbacks after cancel returned in 1,800 random-timing sessions, and 0 audio fed after it in 1,200. Memory after 150 full sessions and after 500 start-and-free cycles: −4.2 to +4.0 MB, against 4 MB for one leaked ring. Every converter input copy zeroed (94 of 94 chunks), with output bit-identical to plain AVAudioConverter. The symbol check rejects all 20 crafted archives. | `CancelTests`, `CancelEdgeTests`, `LeakTests`, `UnitTests`, `scripts/check-symbols-selftest.sh` |
| Speech library: the real recognizer | **never run** (it needs the owner: the model install, and a possible permission prompt) | — |
| Replay script (D-057) | 130 rows (30 dev, 100 test), 77 of 77 aliases said, script hash `3b4256f1cf51`. Test outcomes: 88 query, 6 miss, 2 fallback, 2 past the horizon, 2 beyond it. Six rows are owner-pending. 40 bench tests pass. Against the real parser (`make bench-script`, D-075): no drift and no stale verdict, 121 rows agree, 4 soft, 5 not; the freshly built `lookout-parse` is bit for bit the one the TSV was written with (SHA-256 `ed5c1815…`). The expected columns regenerate unchanged from the committed `prd_reading.py`, which changed after the second freeze (`bench/README.md`). | `make test-bench` with `UV_OFFLINE=1` at 16:10 on 20 Nov; `make bench-script` with a scratch `CARGO_TARGET_DIR` at 15:56 (10.6 s, 40 crates built in 9.8 s); `prd_reading.py` run in a scratch copy of the repo |
| The parser against the script's reading (text only, not the PRD §14 replay metric) | Test split: 94 of 100 exact, 97 counting soft differences (94 of 97 and 96 of 97 without the 3 owner-pending test rows); all rows: 121 yes, 4 soft, 5 no. Solar times within 1 s of an independent Meeus Sun (28 rows, mean 0.25 s); clock-based times identical on 86 of 86 rows. | `check.py --write` on `lookout-parse` from `8246a8b`'s lookout-core, 05:40 on 20 Nov |

### Test suites, all packages

| Suite | Result | How and when |
|---|---|---|
| Full `make test` | **All 10 suites passed, 224 s:** common 9 s, capture 65 s, forecast 21 s, eval 19 s, renderer 39 s, rust 7 s, speech 37 s, infra 23 s, docs 3 s, bench 1 s. On 19 Nov (8 suites, 230 s), forecast failed. Not re-run since this workflow's changes, which add capture's 4 slow tests (81–241 s) and about 10 s of speech checks (D-075): a full run writes into `~/lookout-data`'s build caches, which this workflow's agents may not touch. | The integration agent's run on the working tree, 10:30:57–10:34:41 on 20 Nov, exit 0 |
| `make test` with no network | All 10 suites passed (223 s) under a macOS sandbox that denied every outbound IP connection except loopback, and DNS | The integration agent, 20 Nov (the Makefile's header) |
| Per package, latest | common 258; capture 249 plus 4 slow (the working tree; 191 at 10:31); forecast 177 (1 network test deselected); eval 403 and 1 skip (the working tree; 367 at 10:31); renderer 183; core 108; speech 103; infra 113; docs 85; bench 40; spike 105 and the backfill planner 44 (neither in `make test`) | `make test`, 10:31 on 20 Nov; the capture, eval and planner fixers' runs, 14:45–15:27; `make test-docs` and `make test-bench` at 16:10; the spike's own run on 19 Nov |
| CI workflow lint | 0 findings | actionlint 1.7.12, 19 Nov |

### Machine and environment

| What | Value | How and when |
|---|---|---|
| iCloud | `~/Documents` has the iCloud Drive file-provider xattr; `~/lookout-data` has none | `xattr -l`, 19 Nov (D-017) |
| Hidden venv files | 19 Nov: 8,393 in `workers/common/.venv`; 6,173 in `workers/forecast/.venv` (173 at build time). 01:48 on 20 Nov: common 8,393, capture 8,449, forecast 7,549, eval 5,197. | `find <venv> -flags +hidden \| wc -l` (D-031); the 6,173 is the eval fixer's count |
| Battery and power | 19 Nov: on battery from 06:13 to 22:48; the last reading on battery was 11 % at 22:25, and it read 3 % at 22:48, when AC power came on. 20 Nov: on AC until 03:29:09, then on battery until 06:27:36 (100 % down to 85 %). The lid closed at 03:52:37 (clamshell sleep, a full wake at 03:56:06), and the Mac idle-slept from 04:08:22, with dark wakes. On AC since 06:27:36; 100 %, charged, at 10:38 (D-051), and still at 15:46. | `pmset -g log` and `pmset -g batt`, read at 10:40 and 15:46 on 20 Nov |
| Sleep assertions | The capture loop's `caffeinate -s` (pid 36443, on behalf of the snapshot's `lookout-capture`), which holds only on AC. The `caffeinate -dims -t 28800` of 06:27:54 on 20 Nov ended at 14:27:54; at 15:46 the loop's is the only one (D-076). | `pmset -g assertions` and `ps`, 10:45 and 15:46 on 20 Nov |
| The running jobs | Capture loop pid 36437, up since 02:39:39 on 20 Nov, and forecast stand-in pid 36474, up since 02:39:48 (13 h 07 min at 15:46), both children of the lead's session (D-076). Eight orphaned `terraform-provider-aws` processes (parent pid 1) from scratch mutation runs: 949,040 KiB of resident memory at 15:46, about 0.9 GiB, which varies (0.5–1.2 GiB in readings to 17:08). That is memory, not disk; the lead may stop them. | `ps` (`lstart`; `rss`, in KiB), 15:46–17:08 on 20 Nov |
| Power settings | Sleep 1 min and display sleep 10 min, on AC and on battery; Low Power Mode off. The Battery pane calls the setting "Energy Mode" (Automatic or Low Power). | `pmset -g custom`, read by the M1 docs package at about 04:00 on 20 Nov |
| Disk | 27 GiB free of 460 GiB at 17:26 on 20 Nov (94 % used on the data volume). Earlier: 19 GiB at 15:46, 22 GiB at 10:38, 28 GiB at 00:50, 21 GiB at 04:03 and 23 GiB at 06:11 on 20 Nov; 44 GiB at about 12:00 on 19 Nov (the capture fixer's reading). | `df -g` |

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
