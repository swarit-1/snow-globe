# Design-H archive backfill plan (M1.5 and M2), 20 Nov 2026

**What this is.** A plan for fetching the NPS archive frames that serve as development data for design H and for K2 (PRD §13.2, §13.9). The running capture loop's archive queue does the fetching (D-029). The lead queues the work. The first tranche, as queued, is in [Appendix A](#appendix-a-first-tranche-commands); the planner in [`eval/tools/backfill_plan/`](../../eval/tools/backfill_plan/) writes the drafts for the later tranches (Appendix B).

**Revised three times on 20 Nov:**
- **After review (morning):**
  - the Sun-bin coverage uses the extractor's own lookup;
  - the time model was measured on the real queue runner, errors included;
  - T0 fills its widest gaps;
  - a small Point Reyes slice for M2's plate (T3pr) is scheduled after T1b;
  - the HRRR estimate counts the valid hours of all six cameras.
- **After T0 (afternoon, from a copy of the queue taken at 12:47 UTC):**
  - the 16:9 spans start at T0's measured switch dates (section 4);
  - the dates T0 opened before D-024's spans are a separate tranche, TQ, for the owner;
  - no planned date may yield a frame dated 1 Jun 2026 in UTC (section 9);
  - the later tranches are planned from the queue's state, with inputs re-measured on the queue's first 1,000 requests;
  - T3pr is due Sun 29 Nov, and behind T1b it would miss that by a day (section 5).
- **After review of the afternoon's revision (evening; the same queue copy, unchanged at 14:46 UTC):**
  - each tranche's PLAN.md target sits beside its measured finish. With T3pr ahead of T1b, T1b and T2 end a day late, and at 3.7 % errors T1a and T1c do too; no order meets both 29 Nov targets (section 5);
  - the multi-day runs count a failed entry as unfinished, and the dates assume the lead resumes failed entries each day (section 6);
  - the request spacing is recounted exactly: 8 of 999 pairs were under 5.0009 s, one of them at 4.9944 s (section 6);
  - TQ replaces D-052's step of extending the spans (section 4), and Point Reyes's block would go before T2 (section 10, item 3);
  - the drafts refuse a mistyped queue path, an `EVENINGS` day that is not a Point Reyes T1 day, and TQ without the owner's list of cameras (section 8);
  - the local reading also checks the latest frame's wall-clock date, and a re-plan after T3pr is queued plans no T3pr twice (section 9).

**Nothing was fetched for this plan.** No request went to nps.gov. The plan reads only metadata already on disk:
- the `GetAvailableDays` masks of 19 Nov (`cache/nps-archive/days/`);
- the 244 cached day searches dated before 1 Jun 2026;
- the 907 archive sidecars (all dated before 1 Jun 2026), the per-viewpoint state files (`state/archive/<id>.json`), the day's budget file, and the capture logs of 19 and 20 Nov;
- a copy of the live queue.

All of these are read-only. No image was opened, and nothing under `quarantine/` was read. The coverage and the time model ran offline too: the first through eval's `ClearDayReference.lookup`, the second through capture's `QueueRunner` against a mock nps.gov on a fake clock (Appendix B).

**State of the live queue** (`~/lookout-data/state/archive/queue.json`, read from a copy taken at 12:47 UTC on 20 Nov, byte-identical to a second copy at 14:46 UTC; never edited):
- **441 entries:** 180 done, 260 pending and 1 active, dated 2023-01-30 to 2026-05-30.
  - **T0:** all 134 done, in 256 requests.
  - **T1a:** 46 done, 1 active and 260 pending, after 744 requests. The active entry is `point-reyes.beach/2025-07-19`, with 11 of its 15 frames stored.
- **The day's budget** of 1,000 requests was spent at 11:43:23 UTC. The queue resumes at 00:00 UTC on 21 Nov.
- **The lead's two edits** (about 12:40 UTC):
  - **The six 31 May entries were removed** (section 9). None of them had been requested.
  - **`big-bend.panther-junction/2025-06-14` was resumed.** It had failed after three search read timeouts at 11:30:05 UTC, and it retries in the next day's budget.
- **The loop** runs capture 0.2.0 from snapshot `034c46b`, whose queue code is the same as HEAD's. The planner's figures are identical under the working tree's capture 0.2.1 (section 5).

## In brief

1. **Which frames.** Full-size `hires` frames (999 px wide): the frame nearest each local hour, with NPS Sun at 2° or more. Whole ISO weeks (Monday to Sunday) are the sampling unit.
   - Thumbnails (100 px) are no use: the extractor works at a 960 px analysis width.
   - Scored predictions are hourly (PRD §13.7), so hourly frames cover them; section 3 gives the measured coverage.
2. **Spans.** T0 dated each camera's 3:2 → 16:9 switch to within 14 days (26 at Grand Canyon). Every switch came earlier than the 16:9 sample that starts D-024's span.
   - **The regular tranches keep D-024's spans.**
   - **TQ is the owner's question:** the dates between T0's first 16:9 frame and D-024's span, at five cameras. That is 232 entries and 3,253 requests (section 4).
   - **Unchanged for a design reason:** Big Bend starts in Jan 2025 for D-024's view check, and Grand Canyon's 3:2 epoch stays out (D-042).
3. **The 31 May edge.** Until the owner confirms D-038, no planned date may yield a frame dated 1 Jun 2026 in UTC.
   - At these six cameras, 31 May's evening is already 1 Jun in UTC, so 30 May 2026 is the last plannable date.
   - The planner enforces this by instant, not by a list of dates (section 9).
4. **Order.** T0 (done), T1a (queued), then T3pr ahead of T1b's pending entries, then T1b, T2 and T1c. TQ follows only once the owner answers, camera by camera; Point Reyes's block would go before T2 (section 10, item 3).
5. **Finish dates against PLAN.md's targets** (UTC days from 21 Nov, at 1,000 requests a day, in the recommended order). Error rates are 20 Nov's 1.0 %, with 19 Nov's 3.7 % in brackets. The dates hold only if the loop runs at least 2.2 h every UTC day and the lead resumes any failed entry each day (section 6).

   | Tranche | PLAN.md target | Finishes | Against the target |
   |---|---|---|---|
   | T1a, the summer weeks already queued | Tue 24 Nov | 24 Nov (25 Nov) | on time (1 day late) |
   | T3pr, Point Reyes plate inputs for M2, queued ahead of T1b | Sun 29 Nov | 25 Nov | 4 days early |
   | T1b, shoulder weeks, behind T3pr | Sun 29 Nov | 30 Nov | **1 day late:** 37 requests short (282) |
   | T2, summer completion | Thu 10 Dec | 11 Dec | **1 day late:** 122 requests short (666) |
   | T1c, winter weeks | Tue 15 Dec | 15 Dec (16 Dec) | on time (1 day late) |
   | TQ, if the owner says yes and it follows T1c | none | 19 Dec | |

   - **No order meets both 29 Nov targets.** The nine UTC days through 29 Nov hold 8,910 useful requests at 1.0 % errors (8,665 at 3.7 %), and T1a, T1b and T3pr need 8,947 in either order. Queued behind T1b, T3pr lands on 30 Nov, a day late, and T1b on 29 Nov (30 Nov).
   - **The bottleneck** is the 1,000-request daily budget, plus the 4–5 % more requests per day entry that the re-measured share of a complete day brought (section 3).
   - **So queue T3pr ahead of T1b's pending entries** (section 8). T3pr is a dated need (M2's plate); 29 Nov is T1b's own target. Its clear days can be chosen from Point Reyes's T1a frames, which land by 22 Nov for summer 2025 and May 2026.
6. **Budget and cap: keep 1,000 requests a day and 5 GB.** The open work through T1c is 24,637 requests and 1.22 GB of frames (0.49–1.91 GB). The largest viewpoint, Yosemite, reaches 0.35 GB of its 1.0 GB cap (section 7).
7. **Guard.** Every planned date passes both readings of the held-out edge: its local date, and the wall-clock date of the latest frame it can yield, are before 1 Jun 2026, and every frame it can yield is before 1 Jun 2026 00:00 UTC. The planner checks both; capture's four layers check the local reading (section 9).

## 1. What the archive must supply

**Who needs it:**
- **P1 needs nothing from the archive.** P1 is Rainier's summit, and the park camera has no archive (D-018, D-019). M1.5's dev-only P1 table uses our own captures.
- **The archive serves:**
  - design H at the five ARD cameras of D-024: Point Reyes, Grand Canyon, Yosemite, Acadia and the Smokies, plus Big Bend after its check;
  - K2 at Point Reyes, scored on summer 2026;
  - possibly design P's winter climatology at Point Reyes (section 10, item 6).
- **Design-H fits come only from the archive.** Every design-H fitted asset must come from frames dated before the held-out season (PRD §13.9). That covers plates, haze, clear-day references, camera response, τ, S_mos and analog weights. Live captures start in Nov 2026, after summer 2026, so none can be used.
- **Deadlines.** Under design H the 14 Dec date does not bind (PRD §16), and M1.5's gate uses no archive frame. Two dated needs remain, both at Point Reyes:
  - **K2's landmark.** K2 is the one pre-registered key secondary that design H scores. Its Point Reyes landmark must be named before sealing (§13.4), at the 14 Dec freeze (PLAN M2).
  - **Point Reyes's plate.** M2 (10–27 Dec) builds it (PLAN M2). For K2 under design H the plate is a fitted asset too, so its night frames and clear-day runs must be archive frames: T3pr, due 29 Nov.
  - That makes Point Reyes the first camera in every tranche.

**Needs by use:**

| Use (PRD) | What it needs from the archive | Met by |
|---|---|---|
| Clear-day references for the truth extractor (§13.3) | Clear frames in every Sun bin a scored held-out frame falls in, plus a diffuse (bright-overcast) state (PLAN M2). Extractor v−1 bins are 5° of altitude × 20° of azimuth. The reference is the median over one bin: the exact bin, or the nearest non-empty bin within one step if the exact one is empty. | T1a, T1b; T2 completes them |
| τ and the partial band (§13.4); labels (§13.8) | Many frames across weather and hours. The labels are ~150 per metric, stratified by time of day and weather; fog F1 needs ≥ 30 positives. | T1a |
| S_mos (§13.6) | Many development days paired with archived HRRR. Days are the effective sample (§13.7). | T1, T2 |
| Climatology (§13.6) | Frequency by hour and month in Jun–Aug: ideally every day | T2 |
| Persistence (§13.6) | The frame at valid − 24 h and the latest frame at issue time, so consecutive days | Whole weeks: 87–88 % of T1a days also have the day before; 94–95 % after T2 |
| Block length and MDE (§13.4, §13.7) | Day-to-day autocorrelation, so runs of consecutive days | Whole weeks |
| Dev-fit and dev-check weeks (§13.9) | Whole weeks, with both halves in every season | ISO weeks; parity alternates across years (section 4) |
| Analog weights and the Tier 0 library (§13.6, R3) | Development frames across Sun positions and forecast classes, all from before the held-out season | T1, T2 |
| Plates (§8.5) and camera response (§9.4) | Several bright-overcast near-midday frames with every landmark visible; night frames; 15-min sunrise and sunset runs on clear days (pose check, part 2); frames across all weather | T1 supplies base-frame candidates and the weather spread. T3pr adds Point Reyes's nights and clear days for M2. The other cameras' plates are v1 growth (PRD §16). |
| Same view (§8.6, D-042) | Full frames spread across each span, for registration | Every T1 week; T0 |
| Epoch dates, Big Bend's check, Grand Canyon's winter 2024/25 (archive check TODO(M0.5), D-024) | Midday full frames on a grid | T0, done on 20 Nov: the switch dates are in section 4. The two view checks wait for eval's registration (section 10, item 7). |

## 2. Resolution: full frames only

- **The extractor needs full frames.** It resamples every frame to a 960 px analysis width (`ExtractorParams.analysis_width`, eval).
  - The archive's `hires` is 999 px wide: 999×562 in the 16:9 epoch and 999×666 before it.
  - `thumbmedium` is 100×56, a tenth of the detail in each direction.
  - The v−1 primary box, 5 % of the frame tall, is 28 px high in a hires frame and 2.8 px in a thumbnail.
- **So do the plate and registration.** The plate's pose solve and D-042's registration (51 px shifts, measured at 480–960 px) need full frames too.
- **Request count is the same either way:** one GET per frame, whichever size.
  - **Measured sizes:**
    - `hires`: the 822 frames the queue stored on 20 Nov had a mean of 53,767 bytes (9,819–117,584). The 689 from hourly entries, all 999×562, had a mean of 51,734 bytes (section 6).
    - `thumbmedium`: 1,569 bytes (n = 76, 19 Nov).
  - Thumbnails would save bytes, not requests, and the bytes are far below the cap (section 7).
- **No thumbnails are planned.** T0 dated the epoch switches from full frames as well, since each one also serves as a registration point and a plate candidate.
- **Resolution gap.** Design-H scoring sees archive frames at 999 px, and live frames are stored at 1920 px. D-024 already asks for live frames to be scored at the same width, or for the gap to be reported.

## 3. Frames per day: the hourly frame with Sun at 2° or more

**Options measured on real days** (19 Nov). These are the 66 cached pre-guard day searches of the archive check (6 January and 5 July days per camera, 2021–2026), run through the queue's own `pick_assets`. Mean frames selected per day, January / July:

| Camera | `hourly`, no Sun limit | `hourly`, Sun ≥ 2° | `all`, Sun ≥ 2° | Share of a complete day (hourly, Sun ≥ 2°): 19 Nov sample / 16:9 days, 20 Nov |
|---|---|---|---|---|
| Point Reyes | 20.7 / 21.8 | 8.7 / 13.8 | 31.0 / 48.6 | 0.89 / 0.948 (25 days) |
| Grand Canyon | 20.7 / 22.2 | 8.8 / 13.2 | 31.8 / 50.4 | 0.91 / 0.981 (37) |
| Yosemite | 17.2 / 18.8 | 8.7 / 13.6 | 28.7 / 49.4 | 0.84 / 0.942 (24) |
| Acadia | 24.0 / 22.8 | 10.0 / 15.6 | 35.0 / 55.4 | 0.99 / 0.996 (23) |
| Smokies | 24.0 / 22.8 | 10.0 / 13.6 | 37.3 / 50.0 | 0.99 / 0.966 (19) |
| Big Bend | 23.8 / 20.0 | 11.0 / 11.0 | 37.2 / 37.6 | 0.90 / 0.938 (37) |

- **Complete days.** On a complete day, the hourly Sun ≥ 2° pick gives 9–16 frames, by month and camera (ephemeris).
- **Partial days, re-measured.** The archive's own partial days hold a share of that. The 19 Nov sample gave 0.84–0.99 (0.92 pooled), but 37 of its 66 days were in the 3:2 epoch.
  - T0 and T1a have since searched 168 more days.
  - On the 165 cached days on or after each camera's first 16:9 date, the share is 0.938–0.996 (0.961 pooled).
  - The plan now uses each camera's 16:9 share. That makes T1b, T2 and T1c 4–5 % dearer than the morning's plan said.
- **The `hourly`, no Sun limit column** counts capture 0.2.0's quirk at midnight (section 5).

**Why hourly:**
- **Scoring is hourly.** Scored predictions are hourly (PRD §13.7: "whatever the number of hourly predictions"), at HRRR's valid hours.
- **Hourly frames sit on the UTC hour.** `pick_assets` takes the frame nearest each local hour of `ImageCreateDateTime`, which is on local standard time, and every zone here is a whole number of hours from UTC.
- **Days matter more than frames per day.** Intervals come from a block bootstrap over days (§13.7), so the effective sample of S_mos, climatology, the MDE and persistence is the number of days.
- **Hourly buys days.** At one budget, hourly frames buy 3.5 times as many days as every daylight frame (`all`, Sun ≥ 2°).

**Why 2°:**
- **NPS's angle is geometric.** NPS's `SolarElevationAngle` is the unrefracted Sun: the archive check matched it to the unrefracted NOAA angle within 0.015–0.065°.
- **The daylight rule is apparent.** It is apparent Sun ≥ 3° (§8.6, §13.4; capture's `DAYLIGHT_MIN_SUN_DEG`), which is about geometric 2.75°.
- **So 2° has margin.** A 2° filter keeps every frame P1 or K2 could score, with 0.75° to spare. The scorer applies the ephemeris rule again (§8.6).
- **A higher cut-off loses frames.** At Yosemite in January, 3° keeps 7.2 frames a day against 8.7 at 2°.

**Sun-bin coverage of the held-out season.**
- **How it was computed.** I took every hourly daylight frame time of Jun–Aug 2026, from the ephemeris alone; no frame or metadata of those dates was read. For each one I counted the planned development frames in its extractor bin.
- **What the extractor would use.** A second count asks which frames the extractor would take. `ClearDayReference.lookup` (eval) uses the exact bin when it holds any frame. Otherwise it uses the single nearest non-empty bin within one step, and the median over that one bin. It never pools bins. The planner calls that function itself, on reference entries placed at the planned frames' Sun positions.
- **Table.** It gives the share of held-out hourly times with at least 1 or 10 development frames in the exact bin, and with at least 3 frames in the bin the extractor would use. Development frames are counted as complete days. T1a is the queue's 307 entries; the six 31 May entries are gone.
- **Correction (morning).** The first version of this table summed the 3 × 3 neighbourhood of bins in its third column and reported 1.000 at every camera. The extractor never pools bins, so the real figures are lower at five cameras.

| Camera | T1a: exact ≥ 1 | T1a: exact ≥ 10 | T1a: extractor's bin ≥ 3 | T1a + T1b: exact ≥ 10 | Through T2: exact ≥ 10 | Median per held-out time (T1a / through T2) |
|---|---|---|---|---|---|---|
| Point Reyes | 0.987 | 0.931 | 1.000 | 0.967 | 0.998 | 31 / 119 |
| Grand Canyon | 0.994 | 0.738 | 0.986 | 0.789 | 0.997 | 20 / 62 |
| Yosemite | 0.980 | 0.820 | 0.977 | 0.899 | 0.998 | 19 / 76 |
| Acadia | 0.977 | 0.849 | 0.994 | 0.929 | 0.995 | 19 / 82 |
| Smokies | 0.981 | 0.766 | 0.983 | 0.842 | 0.995 | 20 / 63 |
| Big Bend | 0.916 | 0.556 | 0.963 | 0.722 | 0.970 | 11 / 46 |

- **Every hourly time has a reference after T1a.** The fallback finds a bin at all six cameras, and 0.6–8.4 % of the times use it. Through T2, the extractor's bin holds at least 3 frames for 0.999–1.000 of hourly times.
- **What moved since the morning.** Only the six 31 May days. Grand Canyon's T1a share with ≥ 10 frames in the exact bin fell from 0.767 to 0.738, and the medians fell by up to 1 frame. TQ is not counted here.

**Limits:**
- **Only clear frames count.** These counts are frames of any weather, and only clear frames enter a clear-day reference. With 10 candidates in a bin, a clear share of 30 % gives about 3 clear frames.
  - **TODO(M1.5):** the clear share per camera and bin is unknown until extractor v0 runs on T1a.
  - If it is low, T3 adds 15-min frames on clear days.
- **Every-frame metrics need the fallback.** If a metric scores every 15-min frame, not only the hourly ones, hourly development frames never reach the Sun bins between the hours. Through T2:
  - only 0.71–0.93 of held-out 15-min times have an exact-bin frame;
  - with the extractor's one-bin fallback, 0.994–1.000 have a reference;
  - 0.981–1.000 have one of at least 3 frames, and 0.88–0.98 one of at least 10.
- **Persistence (a) is up to an hour stale.** The latest frame available at issue time is the previous hourly frame, up to 59 min older than the 15-min frame, which itself appears 6.5–6.9 min after its image time (archive check, finding 5).
  - At a 24 h lead this is under 1/24 of the lead.
  - For the 1–6 h secondaries, T3 fetches the 15-min frames at the issue times. The day's search is cached by then, so each costs one GET.

## 4. Dates

**Rules:**
- **The 16:9 epoch only.** Frames come from the current 16:9 epoch (D-024), which T0 has now dated.
- **The regular tranches keep D-024's spans.** T1b, T3pr, T2 and T1c start where D-024's usable span starts, or later for a design reason. D-024 starts each span "at the first 16:9 sample before the guard", the archive check's sample.
- **The rest is the owner's question.** The dates T0 opened between its first 16:9 frame and D-024's span form a separate draft tranche, TQ (section 10, item 3).
- **The last date is 30 May 2026.** It is the last date whose frames all fall before 1 Jun 2026 in UTC as well as in local time (section 9).

**T0's answer** (D-052; from the sidecars' frame heights: 666 px in the 3:2 epoch, 562 px in the 16:9 epoch; no image opened). Local dates:

| Viewpoint | Last 3:2 / first 16:9 (T0) | D-024's span starts (first 16:9 sample) | Regular tranches start | TQ: the owner's question | Notes |
|---|---|---|---|---|---|
| `point-reyes.beach` | 27 Mar / 10 Apr 2023 | 15 Jul 2023 ("Jul 2023 →") | 15 Jul 2023 (weeks from 17 Jul) | 10 Apr – 14 Jul 2023: 58 days, 857 requests | K2; design P. D-024's note names summers 2024 and 2025, so summer 2023 is a reading (below). |
| `grand-canyon.yavapai` | 24 Apr / 20 May 2024 (no frames between) | 15 Jul 2024 | 15 Jul 2024 | 20 May – 14 Jul 2024: 51 days, 767 requests | The 3:2 epoch is never planned (D-024, D-042). Winter 2024/25 is held for the re-aim check. |
| `yosemite.turtleback` | 22 Aug / 5 Sep 2023 | 15 Jan 2024 | 15 Jan 2024 | 5 Sep 2023 – 14 Jan 2024: 25 days, 292 requests | |
| `acadia.mcfarland` | 12 Aug / 26 Aug 2023 | 15 Jan 2024 | 15 Jan 2024 | 26 Aug 2023 – 14 Jan 2024: 33 days, 411 requests | |
| `smokies.look-rock` | 11 Mar / 25 Mar 2024 | 15 Jul 2024 | 15 Jul 2024 | 25 Mar – 14 Jul 2024: 65 days, 927 requests | |
| `big-bend.panther-junction` | 2 Dec / 16 Dec 2023 | 15 Jan 2024 ("Jan 2024 (or Jul 2024) →") | 13 Jan 2025: the week of the 2025-01-15 sample, which matches 2026-01-15 | none: a design reason holds the whole span | D-024's view check of the "framed lower" 2024-01-15 frame. Eval's registration of T0's fortnightly 2024 frames decides (section 10, item 7). |

- **The sample date or the month.** D-024's table writes its spans by month ("Jul 2023 →"), but D-024 defines them by the sample. If the owner reads the whole month, 42 TQ entries (621 requests) move to T2: 1–14 Jul 2023 at Point Reyes, and 1–14 Jul 2024 at Grand Canyon and the Smokies. At Yosemite and Acadia nothing is planned on 1–14 Jan 2024.
- **TQ replaces D-052's next step.** D-052 adopted "extend the spans where T0 shows an earlier switch, and generate T1b, T3pr, T2 and T1c again". This revision does not extend the spans; it holds those dates in TQ for the owner, because D-024 starts each span at its sample. So D-052's "Next" needs amending (docs agent). The owner has three readings to choose from:
  - **D-024's sample date** (this plan's reading): TQ stays a separate question;
  - **D-024's whole named month:** 42 TQ entries (621 requests) move into T2;
  - **D-052's own:** each span starts at T0's first 16:9 date, so TQ's dates join the regular tranches (232 entries and 3,253 requests before re-planning; in the planner, `Span.d024_start` becomes `first_169`).
- **The first 16:9 date itself.** Its midday frame is 16:9, but the switch may have come that same morning. Each stored frame's sidecar records `original_height`, and eval's Same view gate applies.
- **A frame's height shows only the switch of aspect ratio, not a re-aim** (D-042). So the Big Bend and Grand Canyon view checks still need eval's registration.

**Not queued:**
- **Rainier:** no archive.
- **Grand Teton:** no design H (D-024).
- **Grand Canyon's 3:2 epoch** (to 24 Apr 2024). D-024 and D-042 allow it for development and backtests only, after registration, and never for the summer-2026 season, which every fitted asset here serves.
  - One optional backtest item is in section 8.

**Point Reyes 2023, a reading of D-024.** D-024's Point Reyes row says that development uses summers 2024 and 2025. This plan also uses 15 Jul – 31 Aug 2023, which lies inside D-024's span.
- **What it adds:**
  - two T1a weeks, queued as written: 17–23 Jul and 14–20 Aug 2023;
  - T2's remaining days of that summer.
  - After T2 that is 17 of Point Reyes's 79 July days and 31 of its 93 August days.
- **TQ goes further.** It reaches back to 10 Apr 2023 and adds June 2023 (30 days), which is the owner's question either way.
- **Who records it:** the docs agent, in a DECISIONS entry (section 10).
- **The alternative:** to keep D-024's wording, remove the two 2023 T1a weeks from the queue before they are worked (they come last in T1a), and drop T2's 2023 days from `draft-T2.sh`.

**Weeks:**
- **One ISO week per month:** the week holding the 15th in odd years, and the week after it in even years.
  - Without the shift, ISO-week parity would follow the month: the week of the 15th is week 24 in June and week 33 in August in each of 2023, 2024 and 2025.
  - A parity split of dev-fit and dev-check would then put whole months on one side.
  - With the shift, every summer month in T1a has both parities. For example, June has 42 viewpoint-days in even weeks and 21 in odd weeks.
- **A window's first month.** A week that starts before the window gives way to the first full week on or after its start. Acadia's TQ August week, for example, is 28 Aug – 3 Sep 2023, after its 26 Aug switch.
- **Eval has no dev-fit/dev-check rule yet.** Either a parity rule or alternating the sampled weeks works on this sample.
- **May 2026 has two weeks, 18–24 and 25–30 May.** They are the last development data before the held-out season, and the camera state closest to it. 31 May is past the UTC edge.
- **Only available days are queued,** using the masks of 19 Nov. A queued day without frames would still cost one search.

## 5. Tranches and order

Planned from the queue's state (the copy of 12:47 UTC on 20 Nov), with the share of a complete day measured on 16:9 days (section 3) and the frame sizes measured on 20 Nov (section 6). Requests are those still to send; finish days are UTC days at 1,000 requests a day from 21 Nov, at 1.0 % errors (3.7 % in brackets).

| Tranche | What | Day entries | Requests still to send (searches + GETs) | Frames | Frame bytes, mean (range) | Finishes (UTC day) |
|---|---|---|---|---|---|---|
| **T0** map | `midday` hires on a 14-day grid in each switch bracket, plus gap fillers; a 16:9 reference per camera; Big Bend fortnightly to Dec 2024; Grand Canyon weekly Nov 2024 – Mar 2025 | 134, all done | 0 (256 sent) | 134 stored | 8.6 MB stored | 20 Nov |
| **T1a** summer weeks | Jun, Jul and Aug of each development summer; 18–24 and 25–30 May 2026 | 307: 46 done, 1 active, 260 pending | 3,864 (255 + 3,609; 744 sent) | 3,609 | 193 MB (78–302) | 24 Nov (25 Nov) |
| **T1b** shoulder weeks | Apr, May, Sep, Oct; Apr 2026; Point Reyes Dec and Jan | 367 | 4,812 (366 + 4,446) | 4,446 | 230 MB (94–367) | 29 Nov (30 Nov) right behind T1a; 30 Nov behind T3pr |
| **T3pr** Point Reyes plate inputs | Four nights (every hour) and six clear days (every 15-min frame), on T1 days | 10 (8 days) | 271 (0 + 271) | 271 | 9 MB (4–18) | 30 Nov behind T1b; 25 Nov ahead of it |
| **T2** summer completion | Every remaining available day of Jun–Aug: Point Reyes 2025, 2024 and 2023, then the rest, 2025 then 2024 | 738 | 10,975 (732 + 10,243) | 10,243 | 544 MB (220–858) | 11 Dec |
| **T1c** winter weeks | Nov–Mar (Grand Canyon Dec 2024 – Feb 2025 held for the re-aim check) | 409 | 4,715 (403 + 4,312) | 4,312 | 240 MB (97–369) | 15 Dec (16 Dec) |
| **Open work through T1c** | | 1,785 | 24,637 (1,756 + 22,881) | 22,881 | 1.22 GB (0.49–1.91) | |
| **TQ** the owner's question | The dates T0 opened before D-024's spans, sampled the same way: a core week a month, then every remaining summer day (section 4) | 232 | 3,253 (213 + 3,040) | 3,040 | 145 MB (58–234) | 19 Dec, if queued after T1c |

- **What changed since the morning's plan.** The regular tranches hold the same dates (T1b 367, T3pr 10, T2 738 and T1c 409 entries), so widening the spans moved nothing into them. T1a lost its six 31 May entries (about 92 expected requests at today's shares). T1b, T2 and T1c each cost 4–5 % more, from the re-measured share of a complete day.
- **Byte ranges.** They multiply each camera's frames by its smallest and largest measured frame (section 6).

**Against PLAN.md's targets.** PLAN.md (M1.5) sets "T0–T1b and T3pr by Sun 29 Nov, T2 by Thu 10 Dec and T1c by Tue 15 Dec", and T1a for Tue 24 Nov. The planner reports each miss (`analysis.json`: `days_after_plan_target`, and `requests_short_by_target`, the useful requests the days through the target lack). At 1.0 % errors, with 3.7 % in brackets:

| Tranche | Target | Plan order (T1b, then T3pr) | T3pr ahead of T1b (recommended) |
|---|---|---|---|
| T1a | 24 Nov | on time (1 day late, 13 requests short) | the same |
| T1b | 29 Nov | on time (1 day late, 11 short) | 1 day late, 37 short (282) |
| T3pr | 29 Nov | 1 day late, 37 short (282) | 4 days early |
| T2 | 10 Dec | 1 day late, 122 short (666) | the same |
| T1c | 15 Dec | on time (1 day late, 567 short) | the same |

- **Each miss is one day,** a few dozen to a few hundred requests. The bottleneck is the daily budget (In brief, item 5).
- **For the docs agent:** PLAN.md's and PRD_PROGRESS.md's backfill dates need these measured finishes beside them.

**Development days per camera** (hourly entries):

| Camera | After T1a | After T1b | After T2 | After T1c | With TQ | Jun / Jul / Aug days after T2 |
|---|---|---|---|---|---|---|
| Point Reyes | 69 | 182 | 358 | 419 | 477 | 60 / 79 / 93 |
| Grand Canyon | 46 | 95 | 198 | 247 | 298 | 30 / 48 / 58 |
| Yosemite | 55 | 118 | 260 | 345 | 370 | 60 / 62 / 62 |
| Acadia | 55 | 118 | 260 | 351 | 384 | 60 / 62 / 62 |
| Smokies | 48 | 97 | 201 | 271 | 336 | 29 / 48 / 62 |
| Big Bend | 34 | 64 | 135 | 188 | 188 | 30 / 31 / 31 |

TQ would add June days at three cameras: 30 each at Point Reyes (2023), Grand Canyon and the Smokies (2024).

**Why this order:**
- **T0 first.** It cost 256 requests and finished on the first day, 20 Nov. Its sidecars dated every switch (section 4).
- **T1a next.** It covers the held-out season's months, newest summer first, then the camera state just before the guard.
  - **What it feeds.** It is the first design-H development sample: extractor v0 at the design-H cameras, a first τ, persistence, a first S_mos, the block length and a first MDE estimate. Whether it is enough is measured there, not assumed here.
  - **Not an M1.5 dependency.** M1.5's gate is P1 at Rainier, on our own captures.
  - **Landing.** Point Reyes's weeks of summer 2025 land on 21 Nov and its May 2026 weeks on 22 Nov, at the planned rate.
- **T1b before T2.** It finishes the stratified sample across seasons before completeness. The shoulder months add clear days and the lower Sun bins of early and late summer.
- **T3pr ahead of T1b's remaining entries.** Point Reyes's plate is M2 work (PRD §16; PLAN M2). For K2 under design H its inputs must be archive frames from before the held-out season (§13.9). Its inputs are due on Sun 29 Nov.
  - **Nights** (§8.5 step 2, the night frame and lights mask): one T1 day per season, `--pick hourly` with no Sun limit. T1 stores the daylight hours, so each night costs 9.5–13.3 GETs. The days are 21 Jan 2026, 27 May 2026, 13 Aug 2025 and 15 Oct 2025, the Wednesday of the newest Point Reyes T1 week of each season.
  - **Clear days** (§8.5 step 8, the sunrise and sunset check): six clear Point Reyes days, with every 15-min frame at NPS Sun ≥ −2°, at 31–45 GETs each.
  - **Who chooses them.** Eval or the owner, from Point Reyes's T1 frames; development frames may be viewed.
  - **The cost model** uses the Wednesdays of the six newest Point Reyes T1 weeks in Jun–Oct.
  - **Why it moves.** The queue works its entries in file order, and `archive queue add` only appends.
    - Behind all of T1b, T3pr's ~300 requests finish on 30 Nov at either error rate: 9.04 and 9.29 budget-days from 21 Nov.
    - Ahead of T1b, they finish on 25 Nov, and T1b on 30 Nov instead of 29 Nov: a day after PLAN's T0–T1b target, which no order meets together with T3pr's (above).
    - Section 8 gives the two ways to queue it.
  - **T2 and T1c come after T3pr.** If T2 was queued first, remove T2's pending entries and add them again after T3pr. Nothing is lost: `state/archive/` keeps stored frames from being fetched twice.
- **T2 before winter.** It completes the climatology cells (hour × month, now 29–93 days each) and fills nearly every Sun bin with ≥ 10 candidates. Design H holds out a summer, so T2 adds more than winter weeks do.
- **T1c last of the regular tranches.** Winter weeks serve the Tier 0 library and year-round fits; they add the least to a summer held-out season.
- **TQ only on the owner's word**, camera by camera: Point Reyes's block before T2, the others after T1c (section 10, item 3).
- **T3, after T1c (adaptive, small).** Chosen from T1's results, for the other cameras and the secondaries:
  - full 15-min days (`--pick all`) on a few clear summer days per camera, for any thin clear-day bins;
  - nights for the other cameras' plates, which are v1 growth (PRD §16);
  - the 15-min frames at issue times for the short-lead secondaries.
- **A quirk for night picks, fixed in capture 0.2.1.** Capture 0.2.0's hourly rule, which the loop runs, gives a frame after 11:30 PM to hour 00 of its own date. So with no Sun limit, hour 00 can be the 11:45 PM frame whenever no 12:00 or 12:15 AM frame exists. It is 15 min from midnight, and a search lists frames in time order (all 234 cached searches), so a 12:15 AM frame wins the tie.
  - **How often.** On the cached real days the 0.2.0 rule does this on 14 of 35 days at Point Reyes, 11 of 52 at Grand Canyon, 9 of 33 at Yosemite, 19 of 31 at Acadia, 11 of 30 at the Smokies and 8 of 53 at Big Bend. On 71 of those 72 days no frame from 12:00 to 12:30 AM existed. On Big Bend's 1 Jul 2024 the 11:45 PM frame took the place of the 12:30 AM frame. (Both capture versions' `pick_assets` on the cached searches.)
  - **What it touches.** It never touches a Sun ≥ 2° entry, and a night frame is still a night frame.
  - **The fix.** Capture 0.2.1 (working tree, uncommitted) leaves such a frame out: hour 00 then has no frame on those 71 days, and the 12:30 AM frame on Big Bend's. Every planned entry and every cost is identical under both versions.

## 6. Requests, bytes and time

**Requests per day entry:**
- **One `Search`.** A day holds at most 96 frames, under the 300-a-page size, so one page. It is cached once the day is two days old, so a later entry for the same day costs no search.
- **One GET per selected frame,** minus any frame already stored.
- **The searches already done.** T0 searched its 122 uncached days on 20 Nov. T1a's entries on T0 days cost no search.
- **T3pr's entries** are on T1 days, so they cost GETs only, and only for frames not yet stored.
  - **Ahead of T1b, some pay more.** Its entries on T1b days pay their day's search and daylight frames instead: two nights (21 Jan 2026 and 15 Oct 2025) and, in the cost model, two clear days (23 Oct 2024 and 17 Sep 2025). The clear day of 15 Oct 2025 shares its night's search.
  - **The total barely moves.** T3pr then costs 318 requests (4 searches) instead of 271, and T1b 4,765 instead of 4,812.

**Measured inputs** (re-measured at 12:53 UTC on 20 Nov with `measure_inputs.py`, Appendix B; the queue's first day is 20 Nov, the archive check's 19 Nov):

| Quantity | Value | Source |
|---|---|---|
| `hires` asset | Hourly entries: 51,734 bytes mean (n = 689, all 999×562; median 48,814; p5 16,754, p95 105,142; 14,603–117,584). Per camera: Point Reyes 31,316 (n = 191), Smokies 44,880 (91), Acadia 51,985 (112), Grand Canyon 54,350 (106), Big Bend 66,649 (84), Yosemite 79,973 (105). All 822 queue frames: 53,767 mean. The morning's figure was 54,141 (n = 9, midday only). | capture 0.2.0 sidecars, `content_length` |
| Day search response | 50,671 bytes mean (n = 168; 49,372 on 19 Nov, n = 92) | 20 Nov capture log, `archive_request` |
| Cached search file | 25,186 bytes mean (n = 244) | `cache/nps-archive/search/`, dates before 1 Jun 2026 |
| Sidecar | 1,835 bytes mean (n = 822) | capture 0.2.0 sidecars in `archive/` |
| Capture-log lines | 304 bytes per request, 361 per stored frame, 276 per finished entry | 20 Nov capture log |
| Request time | Search: median 217 ms, mean 280 ms, p90 423 ms, max 3.43 s (n = 168). GET: median 235 ms, mean 314 ms, p90 428 ms, max 9.36 s (n = 822). The archive check's searches had a longer tail (median 215 ms, mean 534 ms, p90 1.64 s, n = 92). | 20 Nov capture log; p90 by inclusive interpolation |
| Spacing, back to back | Start to start: median 5.244 s; mean 5.284 s over pairs under 6.5 s (n = 959). End to start, counted exactly: 991 of 999 pairs at 5.0009 s or more, 7 at 5.0004–5.0008 s, and one at 4.9944 s (09:56:47.412 → 09:56:52.672 UTC; section 10, item 12). Median 5.0109 s, 5th percentile 5.0023 s. On 19 Nov the 216 pairs had a minimum of 5.0005 s (6 under 5.0009 s) | 20 Nov and 19 Nov capture logs; start = log time − `elapsed_ms`, in decimal arithmetic (`measure_inputs.py`, `end_to_start_exact`). The stamps have 1 ms resolution, so a gap within about 1 ms of 5.000 s cannot be told from it |
| Errors | 20 Nov: 10 of 1,000 (1.0 %): 7 TLS connect timeouts (5.01–5.11 s each) and 3 read timeouts (10.04–10.07 s, one day's search three times running). 19 Nov: 8 of 215 (3.7 %) | capture logs |
| The day's spend | 1,000 requests in 1.849 h, from the first start (09:52:25) to the last answer (11:43:24 UTC), with no loop outage between. 52.7 MB, 44.2 MB of them frames | 20 Nov capture log |
| Live pass | Capture 0.2.0, 95 passes (02:39–12:52 UTC, 20 Nov): median 11.32 s, p90 16.79 s, max 47.71 s. They start 315.7 s apart (median of gaps under 400 s). The first 73 passes on 19 Nov: median 9.3 s | 19–20 Nov capture logs |
| Loop uptime per UTC day | 19 Nov: 2.92 h; the loop started at 12:24 UTC and was out for most of 12:56–21:52 UTC. 20 Nov: 10.54 h by 12:52 UTC, with a gap from 04:10 to 06:29 UTC | `pass` records; a gap of 400 s or more between pass starts is an outage |
| Live traffic | 370 requests and 189 MB in 3.03 h (00:04–03:06 UTC, 20 Nov): about 2,935 requests and 1.5 GB per 24 h | 20 Nov capture log |
| Free disk | 22 GiB of 460 GiB | `df`, 13:31 UTC, 20 Nov |

**Byte estimates rest on T1a's first frames.** The 689 hourly frames come mostly from June and July 2025. Shoulder, winter and fog frames may differ. Point Reyes's frames are the smallest and Yosemite's the largest. After each tranche, `archive queue list --json` gives the real bytes per entry.

**Time model, measured on the real queue runner.**
- **How.** `loop_sim.py` drives capture's `QueueRunner.run_idle` the way the loop calls it (`live.py`): the next pass is due 300 s plus 0–30 s of jitter after the last one started, and the idle hook gets the time left.
  - **Real parts:** the 5 s file limiter, the persisted daily budget, a copy of the planned queue (the open T1a entries, then T1b and T3pr), and real storage, all on a fake clock in a scratch data dir. The code is HEAD's, the same as the running snapshot's.
  - **Simulated parts:** nps.gov is a mock transport.
    - Its answers take the measured request times of the queue's first day.
    - Its days keep each hourly slot with the camera's measured share of a complete day.
    - Pass durations are drawn from the 95 passes of capture 0.2.0 and, as a check, from the first 73.
- **Runs, mode `hours`:** 20 seeds per scenario, each spending one UTC day's 1,000 requests.
- **Results**, in hours of loop uptime to spend one day's 1,000 requests:

| Errors | Hours, mean (range) | Requests per full window | Failed requests per day (range) |
|---|---|---|---|
| None (passes of capture 0.2.0) | 1.71 (1.70–1.75) | 51.0 | 0 |
| None (the first 73 passes) | 1.71 (1.70–1.72) | 51.0 | 0 |
| 20 Nov's sequence of outcomes, replayed (1.0 %) | 1.84 (1.82–1.85) | 47.6 | 10 |
| Independent connect timeouts at 3.7 % | 2.07 (1.94–2.19) | 42.1 | 38.5 (27–54) |
| 19 Nov's sequence of outcomes, replayed (3.7 %) | 2.05 (2.01–2.10) | 42.6 | 37.4 (35–40) |

- **What moved since the morning.** The morning's model gave 1.76 h without errors and 2.10–2.11 h at 3.7 %. The new inputs are 20 Nov's request times: searches with a shorter tail, and passes of capture 0.2.0. They shorten each figure.
- **A consistency check, not a validation.** Replaying 20 Nov's own sequence of outcomes gives 1.835 h on average (1.816–1.852 h over 20 seeds), and the loop itself took 1.849 h for its first 1,000 requests. But the model's request times, passes and outcome sequence all come from that same day, so the match is in-sample.
  - **The out-of-sample check is 21 Nov.** Once 21 Nov's budget is spent, `measure_inputs.py --day 2026-11-21.jsonl` gives the real time from the day's first archive request to its 1,000th (`hours_to_1000th`), to compare with the model's 1.835 h (lead action, section 10).
  - **The model depends on the capture version.** It runs HEAD's capture 0.2.0, as the loop does. Under the working tree's 0.2.1 the same days-mode seed (T3pr first, 20 Nov's sequence, seed 0) sends 15 fewer requests over its run, 9,059 against 9,074, and a different entry fails, because 0.2.1's hourly rule drops the 11:45 PM frame and that shifts the replayed sequence (found in review, and reproduced). T1a and T3pr finished on the same days, and T1b's last entry settled on the same day, 30 Nov. Run the model again once the loop runs 0.2.1.
- **Why errors cost time.** A failed request costs its own timeout: 5 s to connect, 10 s to read, in the queue's client. Then the limiter defers the next request by 30 s, doubling on consecutive failures (`archive.py`, `_send`). A request starts only if the wait plus 35 s fits before the next pass, so near the end of a window a deferral ends it.
- **Checks in the same runs.** No two requests ran closer than 5.0 s end to start on the fake clock, and none started with less than 35.0 s before the next pass. Every run spent exactly the 1,000-request budget, and no entry failed.
- **The analytic check agrees with the no-error runs.** With 20 Nov's inputs (11.32 s passes, 5.28 s from start to start) it gives 51 requests a window, 583 an hour, so 1.72 h a day. With the morning's inputs it gave 49 a window and 1.79 h.
- **Runs, mode `days`:** the queue is worked UTC day after UTC day, with 2.2 h of loop time each day from 00:00 UTC. Each order was run with 4 seeds, at 20 Nov's sequence of outcomes and at independent connect timeouts of 3.7 %, with and without the lead's daily resume of failed entries (`--resume-failed`: `archive queue resume --all` at the start of each UTC day). A tranche counts as finished only when none of its entries is pending, active, failed or stopped. With the daily resume:

| Queue order | Errors | T1a | T1b | T3pr | Entries resumed (4 runs) | Most requests in a UTC day | Days short of the budget |
|---|---|---|---|---|---|---|---|
| T1a, T1b, T3pr (the plan's) | 20 Nov's sequence | 24 Nov | 29 Nov | 30 Nov | 3 | 1,000 | 0 of 36 |
| T1a, T1b, T3pr | 3.7 % independent | 25 Nov | 30 Nov | 30 Nov | 0 | 1,000 | 2 of 36 (993 and 999 requests) |
| T1a, T3pr, T1b | 20 Nov's sequence | 24 Nov | 30 Nov | 25 Nov | 3 | 1,000 | 0 of 36 |
| T1a, T3pr, T1b | 3.7 % independent | 25 Nov | 30 Nov | 25 Nov | 1 | 1,000 | 2 of 36 (970 and 999 requests) |

- **With the daily resume, every cell held for all 4 seeds.** The runs agree with the planner's schedule (section 5) to the day, and every resumed entry was done by the end of its run.
- **Without it, the tranches do not finish.** 7 of the 16 runs ended with 1–2 entries failed: 6 of the 8 at 20 Nov's sequence (9 entries: 8 of T1b and 1 of T3pr) and 1 of the 8 at independent errors (1 of T1b).
  - **The cause.** Each was a day search that failed three times running, as `big-bend.panther-junction/2025-06-14` did on 20 Nov: 20 Nov's sequence holds three read timeouts in a row.
  - **The effect.** In those runs the tranche's last entry still settled on the dates above, but the failed entries' frames were never fetched. So T1b finished in only 2 of the 8 runs at 20 Nov's sequence, and T3pr missed its date in 1 of the 4 T3pr-first runs there.
  - **A correction.** The first version of this table counted those runs as finished, and reported finish days for tranches the runs did not hold (found in review, and fixed). The runs without the resume repeat the first version's request for request (all 16 request counts match), so only the reading changed.
- **So the dates assume the lead's daily resume** (section 8, "What to watch"). At 20 Nov's sequence an entry failed about once every 9–13 simulated UTC days (9 in 80 run-days without the resume, 6 with it), and at independent errors once in 80.
- **No UTC day went over the budget.** None of the runs sent more than 1,000 requests in a UTC day.
- **2.2 h is the floor at 3.7 % errors.** It fell 1–30 requests short on 2 of 36 days, which moved no date. The slowest of the 20 hours-mode seeds needed 2.19 h from the day's first request, and that request can start up to 5 min after 00:00 UTC. So 2.3 h a day covers every seed.
- **The budget binds only while the Mac is awake.** On 20 Nov the loop ran 10.54 h by 12:52 UTC, but it had a gap from 04:10 to 06:29 UTC (Mac asleep; D-051).
- **Two risks to the 2.2 h.**
  - **The session.** The loop is a child of the lead's Claude Code session, so ending that session can stop it. The lead's supervisor script is written but not started; starting it is the owner's call.
  - **Keep-awake.** The owner's `caffeinate -dims` ends at 14:27:54 UTC on 20 Nov. After that only the loop's `caffeinate -s` (on AC power only) keeps the Mac awake.
- **Clustering costs no more.** The 19 Nov pattern, with 7 of its 8 errors in 10.4 min, costs no more than independent errors at the same rate, because a success between two failures resets the deferral. A run of consecutive failures costs more, since the deferral doubles each time (30, 60, 120 s and so on): 20 Nov's three read timeouts in a row deferred 30 s and then 60 s.

**Finish dates.**
- **What the tranche table assumes.** The work resumes at 00:00 UTC on 21 Nov, and each UTC day's budget is used.
- **How a short day costs.** Each UTC day on which the loop runs for less than about 2.2 h moves every later date back by up to a day.
- **Throughput by error rate.** At 1.0 % errors, 990 useful requests a day remain; at 3.7 %, 963. A failed GET is retried in the entry's next sweep, up to 3 sweeps.

**Disk.** The open work through T1c needs about 1.32 GB, against 22 GiB free:
- frames: 1.22 GB;
- sidecars: 42 MB;
- cached searches: 44 MB;
- capture-log lines: about 16 MB.

The live capture's growth is in D-028.

## 7. Budget and cap

- **Daily budget: keep 1,000.**
  - **Politeness first.** The confirmation email ([docs/outreach/nps-confirmation.md](../outreach/nps-confirmation.md)) has not been sent. It is the message that asks whether automated collection is acceptable and whether there is a preferred way to get archive images in bulk. The archive API is undocumented (D-018).
  - **The load is small.** 1,000 requests and about 53 MB a day (52.7 MB on 20 Nov) add 34 % to our requests to www.nps.gov and about 3.5 % to our bytes. Live capture makes about 2,935 requests and 1.5 GB a day (section 6).
  - **The schedule doesn't need more.** At 1,000 a day, T1a lands on 24 Nov, T3pr on 25 Nov if queued ahead of T1b, and T1b on 30 Nov, a day after PLAN's target (section 5).
  - **Why not more.** A raise would only pull T2 forward, and design H's freeze does not bind on 14 Dec. Raise it only if NPS answers yes to bulk collection. At 2,000 a day (about 3.4–4.2 h of loop time), T2 would finish on 1 Dec and T1c on 3 Dec.
  - **Why not less.** At 500 a day, T3pr would still land on 29 Nov ahead of T1b, but T1b would end on 9 Dec, T2 on 31 Dec – 1 Jan and T1c on 9–11 Jan.
- **Total cap: keep 5 GB.**
  - The queue's caps compare each viewpoint's lifetime `bytes_downloaded`. That is 45 MB in all so far, the archive check's frames included.
  - After T1c that becomes 1.26 GB, or 1.96 GB at each camera's largest measured frame. With TQ it is 1.41 GB (2.19 GB).
  - That leaves room for T3 and for the summer-2026 frames that scoring fetches later under `--held-out-override`. Those are about 7,700 frames and 0.4 GB at the same hourly rule (92 days × 6 cameras), and they count toward the same total, which the queue reads from `state/archive/`.
- **Per-viewpoint `--max-gb`: keep the default of 1.0.**
  - The largest viewpoint is now Yosemite, whose frames are the largest: 0.35 GB after T1c (0.51 GB at its largest frame), 0.37 GB with TQ.
  - Point Reyes, which has the most days, reaches 0.20 GB (0.41 GB at its largest frame), TQ included.
  - Note: the cap compares the viewpoint's lifetime `bytes_downloaded`, not one entry's.
- **The email's "[N] frames".** For the owner to fill in: "about 23,700 frames (about 1.4 GB with the day searches) by mid-December, at most 1,000 requests a day, one at a time, at least 5 seconds apart". The frames are the 822 stored on 20 Nov plus 22,881 to come through T1c. TQ, if the owner says yes, adds about 3,000.

## 8. The queue and the next tranches

**The first tranche, as it ran.**
- **Queued** by the lead at 09:50:30–09:50:37 UTC on 20 Nov: T0 and T1a as in Appendix A, 447 entries.
- **The first request** went out at 09:52:25 UTC.
- **T0 finished** that morning; T1a was 47 entries in when the day's 1,000 requests were spent at 11:43:23 UTC.
- **The lead's edits at about 12:40 UTC:** the six 31 May entries were removed (section 9), and Big Bend's 2025-06-14 was resumed.
- **Checked against a copy of the queue** (12:47 UTC): its 441 ids are Appendix A's 447, in the same order, less exactly those six. The lead's pre-edit copy holds all 447. The queue's T0 entries are exactly the T0 rule's (`tranche0`, checked by the planner).

**Validated on scratch queues** (20 Nov, about 06:30 UTC), before the first tranche was queued:
- The scripts were run with `Q` set to scratch files. They built entries identical to the planner's, field by field except `added_at`.
- `lookout-capture archive queue list --queue <scratch>` and `lookout-capture status --archive-queue <scratch>` exit 0. In `archive queue`, the option is `--queue`; `--archive-queue` belongs to `loop` and `status`.
- Without `Q`, a script exits 1 before writing anything. Run outside the repo with no `REPO`, it exits 128 (git) and writes nothing.
- The drafts of this revision were not run, on any queue. Their `add` lines were parsed back into entry ids instead. They match the planner's entries one for one, in order; every date in them is on or before 30 May 2026; and none is already in the queue (0 of 367 for T1b). Capture's real `make_entry` accepts all 1,756 entries still to queue, TQ included.

**What to watch:**
- **Every day: resume failed entries.** An entry whose day search fails three times is marked `failed` and waits (on 20 Nov, `big-bend.panther-junction/2025-06-14`). In the multi-day runs that happened about once every 9–13 simulated UTC days at 20 Nov's error pattern, and on 20 Nov itself once in the day's 1,000 requests (section 6). Run `lookout-capture status` or `archive queue list` (no network), then `lookout-capture archive queue resume <id> --queue "$Q"` for each failed entry. The finish dates assume this.
- `lookout-capture status` (no network) shows the queue's `by_status` and the runner's heartbeat.
- `lookout-capture archive queue list --json` shows each entry's requests and bytes.
- The budget file is `state/archive/budget-archive-queue-<UTC date>.json`.

**Next: T1b and T3pr.** The drafts are generated by the planner (Appendix B). Run each from inside the repo, with the live queue in `Q`. Two ways to get T3pr ahead of T1b:
- **(a) No reordering.** Choose the six clear days from Point Reyes's T1a frames by 23 Nov; the summer-2025 weeks land on 21 Nov and the May 2026 weeks on 22 Nov. Then queue T3pr, then T1b, before T1a runs out on 24 Nov. The `&&` keeps T1b out if T3pr's draft refuses or fails, so T3pr can be fixed and queued first:

  ```sh
  export Q=$HOME/lookout-data/state/archive/queue.json
  EVENINGS='<six YYYY-MM-DD days>' sh draft-T3pr.sh && sh draft-T1b.sh
  ```

  T3pr then lands on 25 Nov, and T1b on 30 Nov.
- **(b) T1b now.** Queue `draft-T1b.sh` now. When T3pr is ready, queue `draft-T3pr.sh`, then move all of T1b behind it:
  - remove every T1b entry by id, from inside `workers/capture`: `PYTHONPATH=src:../common/src uv run --no-sync lookout-capture archive queue remove $(cat <folder>/ids-T1b.txt) --queue "$Q"`;
  - run `draft-T1b.sh` again, which adds them all back, behind T3pr.

  Removing a done or active T1b entry along with the pending ones is harmless. Measured on capture's real `QueueRunner` against a mock archive (HEAD's code, scratch queue): a done entry removed and added back cost no request (its search was cached and its 13 frames already stored), and an active one cost only its 10 frames not yet stored. Capture also keeps an entry removed during a step removed, and `state/archive/` keeps stored frames from being fetched twice. T3pr then lands within a UTC day of being queued, so by 29 Nov if it is queued by 28 Nov.
- **Guards in the drafts.**
  - **Every draft** refuses to run unless `Q` names an existing, non-empty queue file by an absolute path. `archive queue add` would otherwise start a new queue at a mistyped path, which the loop never works.
  - **`draft-T3pr.sh`** refuses to run without `EVENINGS`. Before it queues anything, it refuses a list that names no day, a word not spelled `YYYY-MM-DD`, a day after 30 May 2026, or a day that is not one of the plan's Point Reyes T1 days (`T3PR_DAYS` in the script: T1a as queued, T1b as planned). So a typo, a date that does not exist, or a day of the 3:2 epoch or of TQ is refused. `set -f` keeps the list from being expanded as file names. `make_entry` alone refuses only 1 Jun and later, so it would accept 31 May (section 9).
- **Then T2 and T1c,** in that order (`draft-T2.sh`, `draft-T1c.sh`).
- **TQ only on the owner's word.** `draft-TQ.sh` is one block per camera. It refuses to run unless `TQ_CAMERAS` names the cameras the owner approved (for example `TQ_CAMERAS=point-reyes.beach`), refuses a name that has no block, and queues only the named blocks (section 10, item 3).

**Optional, for eval's next backtest pair.** Section 12 of [the backtest pair's evidence](../evidence/m05-backtest-pair.md) asks for same-aim plate candidates. These are 12 days at Grand Canyon in the 3:2 epoch, which D-024 and D-042 allow for backtests.
- **Why not `midday`.** On those days the highest-Sun frame is 12:30 MST, at azimuth 178–179°. That is the bin next to the pair's (30–35°, 180–200°).
- **The 13:00 frame matches.** It sits at azimuth 186–188° and altitude 30.6–32.2°, so it is in the pair's bin.
- **The command.** About 41 requests on complete days: 12 searches and 29 frames, mostly 12:00 and 13:00 MST. 2024-01-06 has no frames.

```sh
cd "$(git rev-parse --show-toplevel)/workers/capture"
PYTHONPATH=src:../common/src uv run --no-sync lookout-capture archive queue add grand-canyon.yavapai 2024-01-01 2024-01-02 2024-01-03 2024-01-04 2024-01-05 2024-01-07 2024-01-08 2024-01-09 2024-01-10 2024-01-11 2024-01-12 2024-01-13 --hires --pick hourly --min-sun-deg 30 --queue "$Q"
```

## 9. The held-out guard

**Two readings of "dated 1 Jun 2026 or later".** Until the owner confirms D-038's local-day reading, the lead reads it as either the camera's local date or the UTC date (lead's decision of 20 Nov; amends the D-019/D-022 reading, pending the owner).

**The 31 May edge.**
- **Why 31 May fails.** At these six cameras (UTC−4 to UTC−7 in summer) the evening of 31 May local is 1 Jun in UTC.
- **What the removed entries would have fetched** (ephemeris, complete day, hourly Sun ≥ 2°):

  | Camera | 31 May frames on 1 Jun UTC (of all selected) | Latest |
  |---|---|---|
  | Point Reyes | 4 (of 15) | 03:00 UTC |
  | Yosemite | 4 (of 15) | 02:45 UTC |
  | Grand Canyon | 3 (of 15) | 02:00 UTC |
  | Big Bend | 2 (of 14) | 01:00 UTC |
  | Smokies | 1 (of 14) | 00:00 UTC |
  | Acadia | 0 (of 16) | 23:45 UTC on 31 May (19:45 EDT) |

- **The rule refuses the whole date at all six cameras, Acadia included.** A date's search can return any frame stamped up to 23:59:59 local standard time, which is 04:59:59–07:59:59 UTC on 1 Jun.
- **The lead removed the six entries** at about 12:40 UTC. Nothing of theirs had been requested. The queue's latest date is now 30 May 2026.

**The planner's rule, by instant** (`plannable_in`, `eval/tools/backfill_plan/plan.py`). A date is plannable only if all three hold:
- **Its local date is before 1 Jun 2026.**
- **Its latest possible frame's wall-clock date, in the camera's zone, is before 1 Jun 2026.** This is capture's reading of each frame (D-047): in daylight time a standard-time stamp late on 31 May is already 1 Jun by the wall clock.
- **Its latest possible frame is before 1 Jun 2026 00:00 UTC.** The latest instant any of its frames can carry is the later of two:
  - 23:59:59 local standard time on that date. A day's search returns the frames stamped on that date (20,351 of 20,351 cached assets), and NPS stamps standard time all year, to the second. A few stamps are off the 15-minute cadence, such as 23:56, so the bound is the end of the day, not 23:45.
  - Every instant of the date's cached search, read both by `LocalTimeString` and as standard time.
- **So 30 May 2026 is the last plannable date at all six cameras.** Its last possible stamp is 04:59:59–07:59:59 UTC on 31 May.
- **A computed rule, not a list.** In UTC and Tokyo, 31 May passes: its last stamp is before 1 Jun 00:00 UTC and on 31 May by the wall clock. In Paris it fails the wall-clock reading, since 23:00 CET is 00:00 CEST on 1 Jun; the first version of the rule let it through (found in review, and fixed). A test checks UTC, Tokyo, Paris and UTC−12. At the six US cameras the UTC bound comes first, so nothing planned changed.

**Where it applies:**
1. **Every tranche builder.** The week, summer-day, T0-grid and gap-filling functions plan only plannable dates.
2. **`build()`.** It checks every planned entry again. It refuses to plan at all while an open queue entry fails either reading, and it lists such entries.
3. **T3pr's night days** come only from plannable T1 days. A queued entry settled before this rule, such as a done 31 May entry, can't lead a night onto its date (found by a test, and fixed).
4. **`draft-T3pr.sh`.** It accepts only the plan's Point Reyes T1 days, and it refuses any `EVENINGS` day after 30 May on its own as well. That bound is the rule's own output for Point Reyes.
5. **The queue.** Its latest date is 30 May, and the planner checks that no open entry fails either reading (none does).
6. **Capture and eval.** Their guards get a UTC reading in the fourth workflow's capture package (not part of this plan). Until then they enforce the local reading below, which already refuses frames past local midnight on 31 May.

**Effect on the plan:**
- T1a lost 6 entries (about 92 expected requests at today's shares), one development day per camera.
- The HRRR dates fell from 361 to 360 (section 10, item 8).

**Capture's layers, the local reading.** Four layers refuse anything dated 1 Jun 2026 or later in local time:
1. **When an entry is added.** `make_entry` raises for any day on or after `HELD_OUT_FROM` (2026-06-01), so `archive queue add` exits 1 and writes nothing. Checked on a scratch queue:
   - `add point-reyes.beach 2026-06-01`, `... 2026-05-31 --through 2026-06-02`, `... 2026-05-30 2026-05-31 2026-06-01` and `... 2026-07-15` each exit 1 with "refused: … the D-019 held-out guard", and no queue file is written;
   - so do the other ISO spellings of 1 Jun, `20260601` and `2026-W23-1`;
   - one bad day refuses the whole command;
   - `add point-reyes.beach 2026-05-31` is accepted. That is why the T3pr draft checks its own days.
2. **When an entry is worked.** The runner checks the date again (`QueueRunner._advance`). An entry dated on or after the guard, say one edited by hand, is marked `refused` before any request.
3. **For every asset.** `backfill_assets` asks `held_out_reason` for every asset before any request or read.
   - It reads four local dates and refuses when the latest is on or after 1 Jun 2026:
     - `ImageCreateDateTime`;
     - `LocalTimeString`;
     - the civil date of the UTC time;
     - `ImageCreateDateTime` read as local standard time.
   - It fails closed on an unreadable stamp. Checked with the real function:
     - "05/31/2026 11:30:00 PM" is refused at Point Reyes and at Acadia (00:30 daylight time on 1 Jun);
     - the same stamp is allowed at Grand Canyon (Arizona keeps standard time);
     - "05/31/2026 10:30:00 PM" is allowed at Point Reyes;
     - a garbled stamp is refused.
   - **End to end.** 31 May was queued with `--pick all` and no Sun limit, and worked by the real `QueueRunner` against a mock archive with real-format stamps.
     - Point Reyes and Acadia stored 92 frames each. They refused 23:00–23:45 standard time (00:00–00:45 civil on 1 Jun) without a request.
     - Grand Canyon stored all 96.
     - Every stored frame is on 31 May in its camera's civil time, but its evening frames are on 1 Jun in UTC. The UTC reading above is what keeps those out.
4. **When eval reads.** Eval's `FrameStore` takes only the committed split (`heldout_periods.json`, committed in `d9aa416`, SHA-256 `c437544e…`). It reads archived frames dated on or after 1 Jun 2026 only through `SealedScoring(<pre-registration commit>)` (D-038).

**Scoring is the only exception.** The queue never takes `--held-out-override`. Only `archive day|sample --held-out-override "<reason>"` releases held-out frames, it is meant for scoring, and it is logged with every frame it releases.

**Tests.**
- **The planner's own tests: 44 pass** (`test_plan.py`, run as its README says): the original 14, the afternoon's 15 and 15 from the review. The afternoon's cover:
  - the spans and owner windows;
  - the regular tranches staying in their windows and Grand Canyon's winter hold;
  - the UTC rule in all six zones, and its instant reading east and west of Greenwich;
  - a cached asset that reads past the edge;
  - what 31 May would have fetched;
  - no planned or scripted date past 30 May, and `make_entry` accepting every planned entry;
  - an open queued entry past the edge, or one for a viewpoint outside the plan, stopping the plan;
  - the `EVENINGS` check, run by `/bin/sh`;
  - entries the queue already holds;
  - the work left in a queued entry;
  - the finish-day arithmetic;
  - a season without a Point Reyes week.
- **The review's 15** cover:
  - the exact spacing count (`measure_inputs.py`);
  - a days-mode run on the real `QueueRunner` whose entry fails its search three times: unfinished without a resume, finished a day later with the daily resume; and only the queue's own tranches reported;
  - the drafts run by `/bin/sh` with a stub `uv` that only records its arguments: `EVENINGS` lists refused before anything is queued (out of range, the 3:2 epoch, TQ, a date that does not exist, blank, a glob), a `Q` that is missing, relative, a folder or empty, and `TQ_CAMERAS` unset, unknown or blank;
  - the T3pr list holding exactly Point Reyes's T1 days;
  - PLAN.md's targets and the misses; TQ's Point Reyes block moved ahead of T2;
  - a re-plan after T3pr is queued, with two, six and seven real clear days;
  - the newest warm weeks for the clear days, and none when every clear day is queued;
  - each camera's 16:9 share of a complete day once it rests on ten days; no retry on an entry's last sweep;
  - name lookups and datagrams blocked; the data-dir guards under `python -O`.
- **33 mutants, all caught.** Each weakens or undoes one rule in a scratch copy of the tools, and the copied tests must fail:
  - the afternoon's eight: no UTC rule; a day bound in UTC rather than local time; cached assets not read; the owner window kept in the regular tranches; no design hold; queued entries planned again; T3pr reading unplannable queue days; the `EVENINGS` bound a day late;
  - the reviewer's four, three of which survived the afternoon's tests: the 16:9 share ignored; no retry rule on the last sweep dropped; clear days from the oldest warm weeks; TQ inside the regular windows;
  - 21 that each undo one fix of the review (the spacing bins, the four days-mode rules, the three `EVENINGS` rules, the target arithmetic, the block move, the three T3pr re-plan rules, the two `Q` rules, the two `TQ_CAMERAS` rules, the wall-clock date, name lookups, and the data-dir guard as an `assert` or by string prefix).
- **Capture's guard tests** (morning): 7 passed, 44 deselected (`test_archive_queue.py` and `test_archive.py`, `-k "held_out or guard or refus"`). They cover:
  - `make_entry` at 31 May and 1 Jun;
  - a hand-edited entry refused with zero requests;
  - a CLI range across the guard refused whole;
  - the four-date reading;
  - the override.
- **Capture's working tree** (capture 0.2.1, uncommitted) now holds the two tests this plan proposed: the end-to-end 31 May run and the ISO spellings.

## 10. For the lead and the owner

1. **Queue T1b** with `draft-T1b.sh`. None of its 367 entries is in the queue. It fits the daily budget beside T1a's 261 remaining entries.
   - The queue spends at most 1,000 requests a UTC day whatever it holds, and the multi-day runs show it (section 6).
   - T1a's 3,864 remaining requests plus T1b's 4,812 take 8.76 budget-days at 1.0 % errors (9.01 at 3.7 %). So T1b on its own would end on 29 Nov (30 Nov). With T3pr ahead of it, as item 2 asks, T1b ends on 30 Nov at both rates, a day after PLAN's target (section 5).
2. **Queue T3pr ahead of T1b's pending entries,** by one of the two ways in section 8, so that it lands by Sun 29 Nov. Behind all of T1b it lands on 30 Nov. No order meets both 29 Nov targets at 1,000 requests a day (In brief, item 5).
   - Its clear days are chosen from Point Reyes's frames by eval, the plate's builder or the owner.
3. **The owner's questions: TQ.** D-024 starts each span at the archive check's first 16:9 sample. T0 dated every switch earlier. May design-H development use the dates between?

   | Camera | Dates | Entries | Requests | What they add |
   |---|---|---|---|---|
   | Point Reyes | 10 Apr – 14 Jul 2023 | 58 | 857 | A third June (2023) for K2; Apr–May 2023 weeks |
   | Grand Canyon | 20 May – 14 Jul 2024 | 51 | 767 | June 2024, and early July 2024 |
   | Yosemite | 5 Sep 2023 – 14 Jan 2024 | 25 | 292 | Autumn and early winter 2023 weeks |
   | Acadia | 26 Aug 2023 – 14 Jan 2024 | 33 | 411 | Late August 2023, then autumn and early winter weeks |
   | Smokies | 25 Mar – 14 Jul 2024 | 65 | 927 | Spring 2024 weeks and June 2024 |

   - **The month reading.** If the owner reads D-024's spans as whole months ("Jul 2023 →"), 42 of these entries (621 requests) move into T2 instead: 1–14 July at Point Reyes, Grand Canyon and the Smokies.
   - **TQ replaces D-052's step of extending the spans** (section 4), so D-052's "Next" needs amending (docs agent). D-052's own reading, spans from T0's first 16:9 date, would put all of TQ into the regular tranches.
   - **Big Bend is not a question here.** Its span is held for D-024's view check (item 7).
   - **Point Reyes's block goes before T2, if approved.** At 857 requests it is under one budget-day. Queued right after T1b it lands on 30 Nov (1 Dec at 3.7 %), in time to inform K2's landmark before the 14 Dec freeze. T2 then ends on 11 Dec (12 Dec) and T1c on 16 Dec (17 Dec), up to a day later than without it. Queued after T1c it would land on 19 Dec, after the freeze (`analysis.json`, `schedule_t3pr_first_tq_point_reyes_before_t2`).
   - **Each camera's block of `draft-TQ.sh` is queued only when `TQ_CAMERAS` names it** (section 8): Point Reyes's before T2, the others after T1c.
4. **D-038 (the owner).** Confirming the local-day reading would make 31 May 2026 plannable again: the six removed entries, about 92 requests at today's shares. Until then, the planner's UTC rule stays.
5. **Point Reyes 2023 (docs agent).** Record the reading of D-024 in section 4, or drop the 2023 weeks.
6. **Point Reyes in winter, for design P: the owner's reading.** T1b fetches Point Reyes's December and January weeks of 2023/24 to 2025/26: 38 available days. They could give design P's sealed window a climatology and winter Sun-bin references.
   - **The conflict.** PRD §13.9 says design P's development runs from go-ahead to 14 Dec 2026.
   - **What the reading decides.** Whether earlier archived winters may feed design-P fits at Point Reyes, at 999 px against live 1920 px.
   - **Either way the frames are useful.** They serve K2 in winter and Tier 0.
7. **Eval: register the Big Bend and Grand Canyon check frames** against their 16:9 references (`lookout_eval.registration`, D-042). T0 fetched them on 20 Nov.
   - The answer sets Big Bend's start (`SPANS[BIBE].design_start`, now 13 Jan 2025) and Grand Canyon's winter hold (`HOLDS`, now Dec 2024 – Feb 2025).
   - Then run the planner again.
8. **Archived HRRR for 360 development dates** (forecast work package, not this queue).
   - **One run serves all six cameras.** One archived HRRR run serves every viewpoint of a date, because the GRIB messages are CONUS-wide. But the cameras span four time zones, so a date needs the union of their daylight valid hours.
   - **Count.** T1a to T2 hold 360 distinct dates, with 16.1 valid hours per date on average (9–19) and 5,812 distinct valid hours in all. With T1c: 423 dates and 6,817 hours. With TQ as well: 495 dates and 7,931 hours.
   - **Size.** At the backtest pair's measured 16.5 MB a forecast hour (12 fields), the 24 h lead alone is about 96 GB of transfer through T2, 113 GB with T1c, and 131 GB with TQ. The files are deleted after extraction.
   - **The edge.** The latest valid hour is 03:00 UTC on 31 May 2026, from 30 May's evening.
   - **The list.** The UTC valid hours of every date through T1c are in `hrrr-valid-hours.json` (Appendix B). T0 and T3pr need no HRRR.
9. **Send the NPS email, with [N] filled in** (section 7). Until NPS replies, the budget stays at 1,000.
10. **The dev-fit/dev-check rule (eval).** Either ISO-week parity or the order of the sampled weeks works on T1 (section 4).
11. **Capture's owner.**
    - **The hourly quirk.** It is fixed in capture 0.2.1's working tree, and the loop still runs 0.2.0 (section 5).
    - **The two proposed tests** are in capture's working tree too, uncommitted.
12. **`workers/common`'s owner: one archive request may have started 6 ms early, and the margin is thin.** Counted exactly from the log's stamps (section 6), one of the queue's 999 back-to-back pairs on 20 Nov measured 4.9944 s end to start (09:56:47.412 → 09:56:52.672 UTC), and 7 more measured 5.0004–5.0008 s. The median was 5.0109 s and the 5th percentile 5.0023 s. On 19 Nov the minimum of 216 pairs was 5.0005 s.
    - **The real gap can only be shorter than the stamps show,** unless the wall clock moved between them. A request's log record is written inside the limiter's hold, before the limiter records the finish.
    - **A possible cause.** `MinIntervalLimiter.hold()` goes ahead after its sleep without reading the clock again when the stored state is unchanged (`(last, not_before) == slept_on`). So a wall clock that runs slow during the sleep, or steps back, can let a request start a few ms before the 5 s are up. The log cannot tell whether that happened here.
    - **So a few milliseconds of clock slew can break the 5 s rule.** With 1 ms stamps the log cannot show that a gap within about 1 ms of 5.000 s kept the rule, and 8 of 999 pairs were that close or closer.
    - **The fix belongs to `workers/common`:** re-read the clock after the sleep, or space archive requests by a margin. A 50 ms margin (5.05 s) would have covered every pair measured. It adds about 1 % to each request's 5.28 s cycle, and in the analytic model at 20 Nov's inputs a window still fits 51 requests, so the daily hours do not change (`analysis.json`, `loop_throughput_no_errors_20nov_inputs_5_05_s`). This plan doesn't change the limiter.
13. **The 2.2 h a day, and a daily resume.** The finish dates need the loop to run at least 2.2 h every UTC day, and any failed entry to be resumed each day (section 8, "What to watch"; section 6). At 19 Nov's error rate 2.2 h is the floor, and 2.3 h covers every simulated seed (section 6).
    - Starting the lead's supervisor (an owner action) limits that dependence: a loop it adopted still stops if the session ends, but the supervisor then starts a replacement (after 330 s for capture, since that exit is unknown). Loops the supervisor starts itself do not depend on the session.
    - Keeping the Mac on AC keeps `caffeinate -s` in force.
14. **Not checked here:**
    - the clear-sky share per camera (TODO(M1.5), from extractor v0 on T1a);
    - re-aims inside each span smaller than the thumbnails show (TODO(M2), Same view registration, D-042), which may exclude some weeks later.
15. **The planner** is in [`eval/tools/backfill_plan/`](../../eval/tools/backfill_plan/), committed in `1a39820`. This revision of it is in the working tree, uncommitted.
    - **What changed:** it plans from the queue, with the spans, TQ, the UTC rule, the multi-day time model and, after review, PLAN's targets, failed entries counted as unfinished with an optional daily resume, the draft guards, the wall-clock reading, the T3pr re-plan, and the network and data-dir guards. 44 tests.
    - **State:** lint-clean under eval's `ruff check .` and `ruff format --check .`.
    - **How it runs:** as its README says. `make test` does not collect it; a `make` target for it is the lead's call (root files).
16. **The time model's out-of-sample check (lead).** Once 21 Nov's budget is spent, copy that day's capture log and run `measure_inputs.py --logs <copy> --day 2026-11-21.jsonl`. Compare its `hours_to_1000th` with the model's 1.835 h, on a day with no loop outage between the two requests (the same output's `loop_uptime`), and report both.

## Appendix A: first-tranche commands

T0 (134 entries), then T1a (313 entries), in queue order: 447 entries in all. Generated by `plan.py` (the morning's version) and run on a scratch queue as described in section 8. The lead queued it at 09:50:30–09:50:37 UTC on 20 Nov.

**The six 31 May entries were removed.** At about 12:40 UTC the lead removed the six 2026-05-31 entries: the last day of each line in the block "T1a core week of 2026-05-25", one per camera (section 9). So the queue holds 441 of these 447 entries, in this order. Don't run this script again as it stands: it would add the six back. Re-running it would add only those six, since every other entry is already queued.

```sh
#!/bin/sh
# First tranche (T0 map + T1a summer weeks) of the design-H archive backfill.
# docs/research/archive-backfill-plan.md. Edits the queue file only: no request to
# nps.gov. The running 'lookout-capture loop --archive-queue' works it in its idle
# time (D-029). Every date is before 2026-06-01; make_entry refuses any later one.
set -eu
# Q must be set: Q=$HOME/lookout-data/state/archive/queue.json for the live queue.
: "${Q:?set Q to the queue file, e.g. Q=$HOME/lookout-data/state/archive/queue.json}"
# Run it from inside the repo, or set REPO to the repo's root.
REPO=${REPO:-$(git rev-parse --show-toplevel)}
cd "$REPO/workers/capture"
run() { PYTHONPATH=src:../common/src uv run --no-sync lookout-capture "$@" --queue "$Q"; }
# T0 epoch: date the 3:2 -> 16:9 switch from the frame size in the sidecar
run archive queue add point-reyes.beach 2023-01-30 2023-02-13 2023-02-27 2023-03-13 2023-03-27 2023-04-10 2023-04-24 2023-05-08 2023-05-22 2023-06-05 2023-06-19 2023-07-03 --hires --pick midday
# T0 epoch: 16:9 hires reference for registration (search cached)
run archive queue add point-reyes.beach 2023-07-15 --hires --pick midday
# T0 epoch: date the 3:2 -> 16:9 switch from the frame size in the sidecar
run archive queue add grand-canyon.yavapai 2024-01-29 2024-02-12 2024-02-26 2024-03-11 2024-03-25 2024-04-08 2024-04-16 2024-04-24 2024-05-20 2024-06-03 2024-06-17 2024-07-01 --hires --pick midday
# T0 epoch: 16:9 hires reference for registration (search cached)
run archive queue add grand-canyon.yavapai 2024-07-15 --hires --pick midday
# T0 epoch: date the 3:2 -> 16:9 switch from the frame size in the sidecar
run archive queue add yosemite.turtleback 2023-07-29 2023-08-12 2023-08-22 2023-09-05 2023-09-09 2023-09-21 2023-09-29 2023-10-07 2023-10-21 2023-11-04 2023-11-18 2023-12-02 2023-12-16 2023-12-30 2024-01-13 --hires --pick midday
# T0 epoch: 16:9 hires reference for registration (search cached)
run archive queue add yosemite.turtleback 2024-01-15 --hires --pick midday
# T0 epoch: date the 3:2 -> 16:9 switch from the frame size in the sidecar
run archive queue add acadia.mcfarland 2023-07-29 2023-08-12 2023-08-26 2023-09-09 2023-09-23 2023-10-07 2023-10-21 2023-11-04 2023-11-18 2023-12-02 2023-12-16 2023-12-30 2024-01-13 --hires --pick midday
# T0 epoch: 16:9 hires reference for registration (search cached)
run archive queue add acadia.mcfarland 2024-01-15 --hires --pick midday
# T0 epoch: date the 3:2 -> 16:9 switch from the frame size in the sidecar
run archive queue add smokies.look-rock 2024-01-29 2024-02-12 2024-02-26 2024-03-11 2024-03-25 2024-04-08 2024-04-22 2024-05-06 2024-05-20 2024-06-03 2024-06-17 2024-07-01 --hires --pick midday
# T0 epoch: 16:9 hires reference for registration (search cached)
run archive queue add smokies.look-rock 2024-07-15 --hires --pick midday
# T0 epoch: date the 3:2 -> 16:9 switch from the frame size in the sidecar
run archive queue add big-bend.panther-junction 2023-07-29 2023-08-12 2023-08-26 2023-09-09 2023-09-23 2023-10-07 2023-10-21 2023-11-04 2023-11-18 2023-12-02 2023-12-16 2023-12-30 2024-01-13 --hires --pick midday
# T0 epoch: 16:9 hires reference for registration (search cached)
run archive queue add big-bend.panther-junction 2024-01-15 --hires --pick midday
# T0 view: Big Bend framing from 2024-01 until it matches 2025-01/2026-01 (D-024)
run archive queue add big-bend.panther-junction 2024-01-29 2024-02-12 2024-02-26 2024-03-11 2024-03-25 2024-04-08 2024-04-22 2024-05-06 2024-05-20 2024-06-03 2024-06-17 2024-07-01 2024-07-12 2024-08-13 2024-08-26 2024-09-09 2024-09-23 2024-10-07 2024-10-21 2024-11-04 2024-11-18 2024-12-02 2024-12-16 2024-12-30 --hires --pick midday
# T0 view: 16:9 reference (cached)
run archive queue add big-bend.panther-junction 2025-01-15 2026-01-15 --hires --pick midday
# T0 view: Grand Canyon winter 2024/25 possible temporary re-aim (D-024)
run archive queue add grand-canyon.yavapai 2024-11-04 2024-11-11 2024-11-18 2024-11-25 2024-12-02 2024-12-09 2024-12-16 2024-12-23 2024-12-30 2025-01-06 2025-01-13 2025-01-20 2025-01-27 2025-02-03 2025-02-10 2025-02-17 2025-02-24 2025-03-03 2025-03-10 2025-03-17 2025-03-24 2025-03-31 --hires --pick midday
# T0 view: the odd 2025-01-15 frame (cached)
run archive queue add grand-canyon.yavapai 2025-01-15 --hires --pick midday
# T0 view: 16:9 reference (cached)
run archive queue add grand-canyon.yavapai 2025-07-15 2026-01-15 --hires --pick midday
# T1a core week of 2025-06-09
run archive queue add point-reyes.beach 2025-06-09 2025-06-10 2025-06-11 2025-06-12 2025-06-13 2025-06-14 2025-06-15 --hires --pick hourly --min-sun-deg 2
run archive queue add grand-canyon.yavapai 2025-06-09 2025-06-10 2025-06-11 2025-06-12 2025-06-13 2025-06-14 2025-06-15 --hires --pick hourly --min-sun-deg 2
run archive queue add yosemite.turtleback 2025-06-09 2025-06-10 2025-06-11 2025-06-12 2025-06-13 2025-06-14 2025-06-15 --hires --pick hourly --min-sun-deg 2
run archive queue add acadia.mcfarland 2025-06-09 2025-06-10 2025-06-11 2025-06-12 2025-06-13 2025-06-14 2025-06-15 --hires --pick hourly --min-sun-deg 2
run archive queue add smokies.look-rock 2025-06-09 2025-06-10 2025-06-11 2025-06-12 2025-06-13 2025-06-14 2025-06-15 --hires --pick hourly --min-sun-deg 2
run archive queue add big-bend.panther-junction 2025-06-09 2025-06-10 2025-06-11 2025-06-12 2025-06-13 2025-06-14 2025-06-15 --hires --pick hourly --min-sun-deg 2
# T1a core week of 2025-07-14
run archive queue add point-reyes.beach 2025-07-14 2025-07-15 2025-07-16 2025-07-17 2025-07-18 2025-07-19 2025-07-20 --hires --pick hourly --min-sun-deg 2
run archive queue add grand-canyon.yavapai 2025-07-14 2025-07-15 2025-07-16 2025-07-17 2025-07-18 2025-07-19 2025-07-20 --hires --pick hourly --min-sun-deg 2
run archive queue add yosemite.turtleback 2025-07-14 2025-07-15 2025-07-16 2025-07-17 2025-07-18 2025-07-19 2025-07-20 --hires --pick hourly --min-sun-deg 2
run archive queue add acadia.mcfarland 2025-07-14 2025-07-15 2025-07-16 2025-07-17 2025-07-18 2025-07-19 2025-07-20 --hires --pick hourly --min-sun-deg 2
run archive queue add smokies.look-rock 2025-07-14 2025-07-15 2025-07-16 2025-07-17 2025-07-18 2025-07-19 2025-07-20 --hires --pick hourly --min-sun-deg 2
run archive queue add big-bend.panther-junction 2025-07-14 2025-07-15 2025-07-16 2025-07-17 2025-07-18 2025-07-19 2025-07-20 --hires --pick hourly --min-sun-deg 2
# T1a core week of 2025-08-11
run archive queue add point-reyes.beach 2025-08-11 2025-08-12 2025-08-13 2025-08-14 2025-08-15 2025-08-16 2025-08-17 --hires --pick hourly --min-sun-deg 2
run archive queue add grand-canyon.yavapai 2025-08-11 2025-08-12 2025-08-13 2025-08-14 2025-08-15 2025-08-16 2025-08-17 --hires --pick hourly --min-sun-deg 2
run archive queue add yosemite.turtleback 2025-08-11 2025-08-12 2025-08-13 2025-08-14 2025-08-15 2025-08-16 2025-08-17 --hires --pick hourly --min-sun-deg 2
run archive queue add acadia.mcfarland 2025-08-11 2025-08-12 2025-08-13 2025-08-14 2025-08-15 2025-08-16 2025-08-17 --hires --pick hourly --min-sun-deg 2
run archive queue add smokies.look-rock 2025-08-11 2025-08-12 2025-08-13 2025-08-14 2025-08-15 2025-08-16 2025-08-17 --hires --pick hourly --min-sun-deg 2
run archive queue add big-bend.panther-junction 2025-08-11 2025-08-12 2025-08-13 2025-08-14 2025-08-15 2025-08-16 2025-08-17 --hires --pick hourly --min-sun-deg 2
# T1a core week of 2026-05-25
run archive queue add point-reyes.beach 2026-05-25 2026-05-26 2026-05-27 2026-05-28 2026-05-29 2026-05-30 2026-05-31 --hires --pick hourly --min-sun-deg 2
run archive queue add grand-canyon.yavapai 2026-05-25 2026-05-26 2026-05-27 2026-05-28 2026-05-29 2026-05-30 2026-05-31 --hires --pick hourly --min-sun-deg 2
run archive queue add yosemite.turtleback 2026-05-25 2026-05-26 2026-05-27 2026-05-28 2026-05-29 2026-05-30 2026-05-31 --hires --pick hourly --min-sun-deg 2
run archive queue add acadia.mcfarland 2026-05-25 2026-05-26 2026-05-27 2026-05-28 2026-05-29 2026-05-30 2026-05-31 --hires --pick hourly --min-sun-deg 2
run archive queue add smokies.look-rock 2026-05-25 2026-05-26 2026-05-27 2026-05-28 2026-05-29 2026-05-30 2026-05-31 --hires --pick hourly --min-sun-deg 2
run archive queue add big-bend.panther-junction 2026-05-25 2026-05-26 2026-05-27 2026-05-28 2026-05-29 2026-05-30 2026-05-31 --hires --pick hourly --min-sun-deg 2
# T1a core week of 2026-05-18
run archive queue add point-reyes.beach 2026-05-18 2026-05-19 2026-05-20 2026-05-21 2026-05-22 2026-05-23 2026-05-24 --hires --pick hourly --min-sun-deg 2
run archive queue add grand-canyon.yavapai 2026-05-18 2026-05-19 2026-05-20 2026-05-21 2026-05-22 2026-05-23 2026-05-24 --hires --pick hourly --min-sun-deg 2
run archive queue add yosemite.turtleback 2026-05-18 2026-05-19 2026-05-20 2026-05-21 2026-05-22 2026-05-23 2026-05-24 --hires --pick hourly --min-sun-deg 2
run archive queue add acadia.mcfarland 2026-05-18 2026-05-19 2026-05-20 2026-05-21 2026-05-22 2026-05-23 2026-05-24 --hires --pick hourly --min-sun-deg 2
run archive queue add smokies.look-rock 2026-05-18 2026-05-19 2026-05-20 2026-05-21 2026-05-22 2026-05-23 2026-05-24 --hires --pick hourly --min-sun-deg 2
run archive queue add big-bend.panther-junction 2026-05-18 2026-05-19 2026-05-20 2026-05-21 2026-05-22 2026-05-23 2026-05-24 --hires --pick hourly --min-sun-deg 2
# T1a core week of 2024-06-17
run archive queue add point-reyes.beach 2024-06-17 2024-06-18 2024-06-19 2024-06-20 2024-06-21 2024-06-22 2024-06-23 --hires --pick hourly --min-sun-deg 2
run archive queue add yosemite.turtleback 2024-06-17 2024-06-18 2024-06-19 2024-06-20 2024-06-21 2024-06-22 2024-06-23 --hires --pick hourly --min-sun-deg 2
run archive queue add acadia.mcfarland 2024-06-17 2024-06-18 2024-06-19 2024-06-20 2024-06-21 2024-06-22 2024-06-23 --hires --pick hourly --min-sun-deg 2
# T1a core week of 2024-07-22
run archive queue add point-reyes.beach 2024-07-22 2024-07-23 2024-07-24 2024-07-25 2024-07-26 2024-07-27 2024-07-28 --hires --pick hourly --min-sun-deg 2
run archive queue add grand-canyon.yavapai 2024-07-22 2024-07-23 2024-07-24 2024-07-25 2024-07-26 2024-07-27 2024-07-28 --hires --pick hourly --min-sun-deg 2
run archive queue add yosemite.turtleback 2024-07-22 2024-07-23 2024-07-24 2024-07-25 2024-07-26 2024-07-27 2024-07-28 --hires --pick hourly --min-sun-deg 2
run archive queue add acadia.mcfarland 2024-07-22 2024-07-23 2024-07-24 2024-07-25 2024-07-26 2024-07-27 2024-07-28 --hires --pick hourly --min-sun-deg 2
run archive queue add smokies.look-rock 2024-07-22 2024-07-23 2024-07-24 2024-07-25 2024-07-26 2024-07-27 2024-07-28 --hires --pick hourly --min-sun-deg 2
# T1a core week of 2024-08-19
run archive queue add point-reyes.beach 2024-08-19 2024-08-20 2024-08-21 2024-08-22 2024-08-23 2024-08-24 2024-08-25 --hires --pick hourly --min-sun-deg 2
run archive queue add grand-canyon.yavapai 2024-08-21 2024-08-22 2024-08-23 2024-08-24 2024-08-25 --hires --pick hourly --min-sun-deg 2
run archive queue add yosemite.turtleback 2024-08-19 2024-08-20 2024-08-21 2024-08-22 2024-08-23 2024-08-24 2024-08-25 --hires --pick hourly --min-sun-deg 2
run archive queue add acadia.mcfarland 2024-08-19 2024-08-20 2024-08-21 2024-08-22 2024-08-23 2024-08-24 2024-08-25 --hires --pick hourly --min-sun-deg 2
run archive queue add smokies.look-rock 2024-08-19 2024-08-20 2024-08-21 2024-08-22 2024-08-23 2024-08-24 2024-08-25 --hires --pick hourly --min-sun-deg 2
# T1a core week of 2023-07-17
run archive queue add point-reyes.beach 2023-07-17 2023-07-18 2023-07-19 2023-07-20 2023-07-21 2023-07-22 2023-07-23 --hires --pick hourly --min-sun-deg 2
# T1a core week of 2023-08-14
run archive queue add point-reyes.beach 2023-08-14 2023-08-15 2023-08-16 2023-08-17 2023-08-18 2023-08-19 2023-08-20 --hires --pick hourly --min-sun-deg 2
run archive queue list
```

## Appendix B: method and files

**The planner** is [`eval/tools/backfill_plan/`](../../eval/tools/backfill_plan/), committed in `1a39820`. This revision is in the working tree, uncommitted. Its [README](../../eval/tools/backfill_plan/README.md) gives the commands. Run it from `eval/` with `PYTHONPATH=src:../workers/common/src:../workers/capture/src`.
- **No network, and only reading.** `plan.py` and `loop_sim.py` disable sockets before importing anything else, so they cannot reach the network. The queue is read from a copy (`--queue`) and never written.
- **Capture's and eval's own code.**
  - Capture: `AvailableDays`, `pick_assets`, `make_entry`, `entry_id`, `ArchiveQueue`, `HELD_OUT_FROM` and `solar_elevation_deg`.
  - Eval: `noaa_solar` for azimuths, and `ClearDayReference.lookup` for the coverage.
- **From the queue's state.** The queue's entries come first, in their order, with the work each has left:
  - nothing once settled;
  - the selected frames not yet stored once searched;
  - otherwise the plan's own cost.
  - Each later tranche is appended only where its entry id is not already queued. A queued entry is labelled with the tranche whose rule makes it, else T0 (midday) or T1a (hourly).
- **Complete-day model.** For any day without a cached search, a complete day is modelled as frames at :00, :15, :30 and :45 local standard time, carrying the geometric Sun that NPS reports. That model goes through `pick_assets` and is scaled by the camera's measured share of a complete day on 16:9 days.
- **Entries on a day already worked** (T3pr, and a T1c week that meets a T0 day) cost no search and no GET for frames an earlier entry stored.
- **Same results under either capture version.** Run with HEAD's capture code (exported with `git archive`; the same as the running snapshot's) and with the working tree's capture 0.2.1, it plans the same entries at the same costs. Only the "hourly, no Sun limit" measurement differs (section 5).

**The simulator** is `loop_sim.py`, run the same way.
- It drives the real `QueueRunner` on a fake clock against a mock nps.gov. The mock's search answers carry real-format `AssetID` and `LocalTimeString` values, and each run's scratch data dir is deleted afterwards.
- **Request times** come from the queue's first day, for pre-guard assets only: their ids are read from the sidecars on disk.
- **The two modes.** `--mode hours` measures one day's budget. `--mode days` works the queue day after day and reports, for each tranche the queue holds, the day it finished: when none of its entries is pending, active, failed or stopped. It also reports the day each settled (none open, failures left) and the entries still failed at the end. `--resume-failed` starts each UTC day with `archive queue resume --all`, the lead's daily step.
- **`--tranches`** keeps only the open entries of the tranches named. Settled entries never change what the runner does.

**`measure_inputs.py`** re-measures every input in section 6 from sidecars, cached searches, the capture logs and the queue.

**Files.** The tool is in the repo:

| File | What it holds |
|---|---|
| `eval/tools/backfill_plan/plan.py` | The planner |
| `eval/tools/backfill_plan/loop_sim.py` | The time model on the real queue runner |
| `eval/tools/backfill_plan/measure_inputs.py` | The measured inputs of section 6 |
| `eval/tools/backfill_plan/test_plan.py` | Its 29 tests |
| `eval/tools/backfill_plan/README.md` | How to run them |

This revision's outputs are in the session scratch folder, which a reboot can clear: the afternoon's in `w4/backfill/out/` and `w4/backfill/sim/`, and the review's in `w4/backfill-fix/out/plan-head/` (the planner's), `w4/backfill-fix/out/measure-inputs.json` and `w4/backfill-fix/sim/` (the multi-day runs). `plan.py` writes them again from a copy of the queue:

| File | What it holds |
|---|---|
| `plan.json` | Every entry, queued or planned, with its tranche, status, ISO week, reason and expected cost |
| `analysis.json` | Every planner measurement above, with the queue checks, spans, UTC edge, schedules and caps |
| `hrrr-valid-hours.json` | The UTC valid hours per development date (T1a–T1c) |
| `draft-T1b.sh`, `draft-T3pr.sh`, `draft-T2.sh`, `draft-T1c.sh` | The drafts for the regular tranches, holding only entries the queue does not hold |
| `draft-TQ.sh` | The owner's question, one block per camera; it runs only with `TQ_CAMERAS` set |
| `ids-T1b.txt` | T1b's entry ids, for moving T1b behind T3pr (section 8, way b) |
| `queue-plan.json`, `queue-plan-t3pr-first.json` | Scratch queues: the queue read, then T1b, T3pr, T2 and T1c, in the plan's order and with T3pr first |
| `measure-inputs.json` | The re-measured inputs |
| `loop-sim-hours.json`, `loop-sim-days-plan-order.json`, `loop-sim-days-t3pr-first.json` | The time model's runs (afternoon). The review's: `loop-sim-hours.json` again (all 100 runs identical) and `loop-sim-days-{plan-order,t3pr-first}-resume-{no,yes}.json` |

The morning's outputs, including the first tranche's validation queues, are in the session scratch folder `w3/backfill/`.

**Held-out note.** One log summary, run for the first version of this plan, printed a grouped mean of the byte sizes of the 8 archive hires requests from 19 Nov that are not on disk: those of 15 Jul and 17 Nov 2026, now in quarantine. It is request metadata, which CLAUDE.md allows for any date, but no number in this plan uses it. The hires sizes above come from sidecars dated before 1 Jun 2026 only.
