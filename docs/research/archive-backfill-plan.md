# Design-H archive backfill plan (M1.5 and M2), 20 Nov 2026

**What this is.** A plan for fetching the NPS archive frames that serve as development data for design H and for K2 (PRD §13.2, §13.9). The running capture loop's archive queue does the fetching (D-029). The lead queues the work; the commands for the first tranche are in [Appendix A](#appendix-a-first-tranche-commands).

**Revised the same day after review:**
- the Sun-bin coverage now uses the extractor's own lookup;
- the time model was measured on the real queue runner, errors included;
- T0 fills its widest gaps;
- a small Point Reyes slice for M2's plate (T3pr) is scheduled after T1b;
- the HRRR estimate counts the valid hours of all six cameras.

**Nothing was fetched for this plan.** No request went to nps.gov. The plan reads only metadata already on disk:
- the `GetAvailableDays` masks of 19 Nov (`cache/nps-archive/days/`);
- the day searches cached by the archive check, for dates before 1 Jun 2026 only;
- the sidecars of the 85 pre-guard archive frames (`archive/`), the per-viewpoint state files (`state/archive/<id>.json`) and the capture logs of 19 and 20 Nov.

All of these are read-only. No image was opened, and nothing under `quarantine/` was read. The coverage and the time model ran offline too: the first through eval's `ClearDayReference.lookup`, the second through capture's `QueueRunner` against a mock nps.gov on a fake clock (Appendix B).

**State of the live queue:** `~/lookout-data/state/archive/queue.json` still does not exist (checked at 06:38 UTC on 20 Nov). The loop has been running capture 0.2.0 since 02:39:39 UTC on 20 Nov as `lookout-capture loop --interval 300 --archive-queue ~/lookout-data/state/archive/queue.json`, with the default budget (1,000 a day) and cap (5 GB), so it is doing no archive work.

## In brief

1. **Which frames.** Full-size `hires` frames (999 px wide), the frame nearest each local hour, with NPS Sun at 2° or more. Whole ISO weeks (Monday to Sunday) are the sampling unit.
   - Thumbnails (100 px) are no use: the extractor works at a 960 px analysis width.
   - Scored predictions are hourly (PRD §13.7), so hourly frames cover them; section 3 gives the measured coverage.
2. **Order.** Six tranches, in this order:
   - **T0, map** (256 requests): date the 3:2 → 16:9 switches, and run D-024's Big Bend and Grand Canyon view checks.
   - **T1a:** one week in each summer month.
   - **T1b:** one week in each shoulder month (and Point Reyes in December and January).
   - **T3pr** (about 250 requests): Point Reyes's plate inputs for M2, four nights and six clear days, on days T1 has already searched.
   - **T2:** every remaining summer day, Point Reyes first.
   - **T1c:** one week in each winter month.

   Each week runs at every camera whose span holds it before the next week starts, so an interruption leaves every camera with data.
3. **First tranche (T0 + T1a):**
   - **Size:** 447 day entries, about 4,730 requests, 4,300 frames and 0.23 GB (range 0.04–0.44 GB).
   - **Finish:** 24 Nov at 1,000 requests a day.
   - **Checked:** the commands were validated on scratch queues.
4. **Finish dates** (UTC days, from 20 Nov). These hold only if the loop runs at least 2.2 h every UTC day (section 6):

   | Tranche | Finishes |
   |---|---|
   | The stratified sample (T0–T1b) | 29 Nov |
   | T3pr, Point Reyes plate inputs for M2 | 29 Nov |
   | Summer completion (T2) | 10 Dec |
   | Winter weeks (T1c) | 15 Dec |

   - **These are the plan's own targets.** Design H's freeze date does not bind (PRD §16), and M1.5's gate is P1 at Rainier, which uses no archive frame (PLAN M1.5).
   - **The two dated needs are met.** K2's Point Reyes landmark must be named by the 14 Dec freeze (PLAN M2), and T1a supplies it on 24 Nov. M2's Point Reyes plate (10–27 Dec) gets its inputs from T3pr on 29 Nov.
5. **Budget and cap: keep 1,000 requests a day and 5 GB.**
   - The NPS email has not been sent, and the API is undocumented.
   - At 1,000 a day the stratified sample and T3pr still land on 29 Nov.
   - The whole plan downloads 1.2 GB (2.3 GB if every frame were as large as the largest measured).
   - A raise would only pull T2 forward. Make one only after NPS says yes to bulk collection (section 7).
6. **Guard.** Every planned date falls on or before 31 May 2026. Four layers refuse anything later (section 9):
   - `make_entry` when an entry is added;
   - the queue runner when an entry is worked;
   - a check on every asset that reads four local dates;
   - eval's frame store.

## 1. What the archive must supply

**Who needs it:**
- **P1 needs nothing from the archive.** P1 is Rainier's summit, and the park camera has no archive (D-018, D-019). M1.5's dev-only P1 table uses our own captures.
- **The archive serves:**
  - design H at the five ARD cameras of D-024: Point Reyes, Grand Canyon, Yosemite, Acadia and the Smokies, plus Big Bend after its check;
  - K2 at Point Reyes, scored on summer 2026;
  - possibly design P's winter climatology at Point Reyes (section 10, item 2).
- **Design-H fits come only from the archive.** Every design-H fitted asset must come from frames dated before the held-out season (PRD §13.9). That covers plates, haze, clear-day references, camera response, τ, S_mos and analog weights. Live captures start in Nov 2026, after summer 2026, so none can be used.
- **Deadlines.** Under design H the 14 Dec date does not bind (PRD §16), and M1.5's gate uses no archive frame. Two dated needs remain, both at Point Reyes:
  - **K2's landmark.** K2 is the one pre-registered key secondary that design H scores, and its Point Reyes landmark must be named before sealing (§13.4), at the 14 Dec freeze (PLAN M2).
  - **Point Reyes's plate.** M2 (10–27 Dec) builds it (PLAN M2). For K2 under design H, the plate is a fitted asset too, so its night frames and clear-day runs must be archive frames: T3pr.
  - That makes Point Reyes the first camera in every tranche.

**Needs by use:**

| Use (PRD) | What it needs from the archive | Met by |
|---|---|---|
| Clear-day references for the truth extractor (§13.3) | Clear frames in every Sun bin a scored held-out frame falls in, plus a diffuse (bright-overcast) state (PLAN M2). Extractor v−1 bins are 5° of altitude × 20° of azimuth. The reference is the median over one bin: the exact bin, or the nearest non-empty bin within one step if the exact one is empty. | T1a, T1b; T2 completes them |
| τ and the partial band (§13.4); labels (§13.8) | Many frames across weather and hours. The labels are ~150 per metric, stratified by time of day and weather; fog F1 needs ≥ 30 positives. | T1a |
| S_mos (§13.6) | Many development days paired with archived HRRR. Days are the effective sample (§13.7). | T1, T2 |
| Climatology (§13.6) | Frequency by hour and month in Jun–Aug: ideally every day | T2 |
| Persistence (§13.6) | The frame at valid − 24 h and the latest frame at issue time, so consecutive days | Whole weeks: 87–89 % of T1a days also have the day before; 94–95 % after T2 |
| Block length and MDE (§13.4, §13.7) | Day-to-day autocorrelation, so runs of consecutive days | Whole weeks |
| Dev-fit and dev-check weeks (§13.9) | Whole weeks, with both halves in every season | ISO weeks; parity alternates across years (section 4) |
| Analog weights and the Tier 0 library (§13.6, R3) | Development frames across Sun positions and forecast classes, all from before the held-out season | T1, T2 |
| Plates (§8.5) and camera response (§9.4) | Several bright-overcast near-midday frames with every landmark visible; night frames; 15-min sunrise and sunset runs on clear days (pose check, part 2); frames across all weather | T1 supplies base-frame candidates and the weather spread. T3pr adds Point Reyes's nights and clear days for M2. The other cameras' plates are v1 growth (PRD §16). |
| Same view (§8.6, D-042) | Full frames spread across each span, for registration | Every T1 week; T0 |
| Epoch dates, Big Bend's check, Grand Canyon's winter 2024/25 (archive check TODO(M0.5), D-024) | Midday full frames on a grid | T0 |

## 2. Resolution: full frames only

- **The extractor needs full frames.** It resamples every frame to a 960 px analysis width (`ExtractorParams.analysis_width`, eval). The archive's `hires` is 999 px wide: 999×562 in the 16:9 epoch, 999×666 before it. `thumbmedium` is 100×56, a tenth of the detail in each direction. The v−1 primary box, 5 % of the frame tall, is 28 px high in a hires frame and 2.8 px in a thumbnail.
- **So do the plate and registration.** The plate's pose solve and D-042's registration (51 px shifts, measured at 480–960 px) need full frames too.
- **Request count is the same either way.** One GET per frame, whichever size.
  - **Measured sizes** (pre-guard sidecars):
    - `hires`: a mean of 54,141 bytes (n = 9; 10,419–101,326);
    - `thumbmedium`: 1,569 bytes (n = 76).
  - Thumbnails would save bytes, not requests, and the bytes are far below the cap (section 6).
- **No thumbnails are planned.** T0 dates the epoch switches from full frames as well, since each one also serves as a registration point and a plate candidate.
- **Resolution gap.** Design-H scoring sees archive frames at 999 px, and live frames are stored at 1920 px. D-024 already asks for live frames to be scored at the same width, or for the gap to be reported.

## 3. Frames per day: the hourly frame with Sun at 2° or more

**Options measured on real days.** These are the 66 cached pre-guard day searches (6 January and 5 July days per camera, 2021–2026), run through the queue's own `pick_assets`. Mean frames selected per day, January / July:

| Camera | `hourly`, no Sun limit | `hourly`, Sun ≥ 2° | `all`, Sun ≥ 2° | Share of a complete day (hourly, Sun ≥ 2°) |
|---|---|---|---|---|
| Point Reyes | 20.7 / 21.8 | 8.7 / 13.8 | 31.0 / 48.6 | 0.89 |
| Grand Canyon | 20.7 / 22.2 | 8.8 / 13.2 | 31.8 / 50.4 | 0.91 |
| Yosemite | 17.2 / 18.8 | 8.7 / 13.6 | 28.7 / 49.4 | 0.84 |
| Acadia | 24.0 / 22.8 | 10.0 / 15.6 | 35.0 / 55.4 | 0.99 |
| Smokies | 24.0 / 22.8 | 10.0 / 13.6 | 37.3 / 50.0 | 0.99 |
| Big Bend | 23.8 / 20.0 | 11.0 / 11.0 | 37.2 / 37.6 | 0.90 |

- **Complete days.** On a complete day, the hourly Sun ≥ 2° pick gives 9–16 frames, by month and camera (ephemeris).
- **Partial days.** The archive's own partial days bring that down to 0.84–0.99 of it (0.92 pooled). The plan's estimates use each camera's measured share.
- **Caveat on the sample.** 37 of the 66 days are in the 3:2 epoch, so the share of a complete day in the 16:9 epoch may differ. The first day of T1a measures it.

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
- **Table.** It gives the share of held-out hourly times with at least 1 or 10 development frames in the exact bin, and with at least 3 frames in the bin the extractor would use. Development frames are counted as complete days.
- **Correction.** The first version of this table summed the 3 × 3 neighbourhood of bins in its third column and reported 1.000 at every camera. The extractor never pools bins, so the real figures are lower at five cameras.

| Camera | T1a: exact ≥ 1 | T1a: exact ≥ 10 | T1a: extractor's bin ≥ 3 | T1a + T1b: exact ≥ 10 | Through T2: exact ≥ 10 | Median per held-out time (T1a / through T2) |
|---|---|---|---|---|---|---|
| Point Reyes | 0.987 | 0.931 | 1.000 | 0.967 | 0.998 | 31 / 120 |
| Grand Canyon | 0.994 | 0.767 | 0.986 | 0.818 | 0.997 | 21 / 63 |
| Yosemite | 0.980 | 0.820 | 0.977 | 0.899 | 0.998 | 20 / 76 |
| Acadia | 0.977 | 0.849 | 0.994 | 0.929 | 0.995 | 19 / 83 |
| Smokies | 0.981 | 0.766 | 0.983 | 0.842 | 0.995 | 20 / 64 |
| Big Bend | 0.916 | 0.556 | 0.963 | 0.722 | 0.970 | 12 / 46 |

- **Every hourly time has a reference after T1a.** The fallback finds a bin at all six cameras, and 0.6–8.4 % of the times use it. Through T2, the extractor's bin holds at least 3 frames for 0.999–1.000 of hourly times.

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
- Frames come from the current 16:9 epoch only (D-024).
- They start at the first date this plan treats as certainly inside the current view.
- They end on 31 May 2026, the last day before the guard.

| Viewpoint | Last 3:2 / first 16:9 sample (archive check) | Bulk starts | What T0 can add | Notes |
|---|---|---|---|---|
| `point-reyes.beach` | 2023-01-16 / 2023-07-15 | 2023-07-15 (weeks from 17 Jul) | June to mid-July 2023, if the switch came earlier: a third development summer for K2 | K2; design P; summer 2023 is a reading of D-024 (below) |
| `grand-canyon.yavapai` | 2024-01-15 / 2024-07-15 | 2024-07-15 | June to mid-July 2024 | Winter 2024/25 waits for T0's re-aim check (D-024); the 3:2 epoch is not queued (below) |
| `yosemite.turtleback` | 2023-07-15 / 2024-01-15 | 2024-01-15 | Whatever of Aug–Dec 2023 follows the switch: summer days if it came in August | |
| `acadia.mcfarland` | 2023-07-15 / 2024-01-15 | 2024-01-15 | Whatever of Aug–Dec 2023 follows the switch: summer days if it came in August | |
| `smokies.look-rock` | 2024-01-15 / 2024-07-15 | 2024-07-15 | June to mid-July 2024 | |
| `big-bend.panther-junction` | 2023-07-15 / 2024-01-15 | 2025-01-13 (the week of the 2025-01-15 sample, which matches 2026-01-15) | Some or all of 2024, once T0's check shows when the "framed lower" setup of 2024-01-15 ended (D-024) | Design H after the check |

**Not queued:**
- **Rainier:** no archive.
- **Grand Teton:** no design H (D-024).
- **Grand Canyon's 3:2 epoch.** D-024 and D-042 allow it for development and backtests only, after registration, and never for the summer-2026 season, which every fitted asset here serves.
  - One optional backtest item is in section 8.

**Point Reyes 2023, a reading of D-024.** D-024's Point Reyes row says that development uses summers 2024 and 2025. This plan also uses 15 Jul – 31 Aug 2023, which lies inside D-024's "Jul 2023 →" span.
- **What it adds:** two T1a weeks (17–23 Jul and 14–20 Aug 2023), and T2's remaining days of that summer. After T2 that is 17 of Point Reyes's 79 July days and 31 of its 93 August days.
- **Who records it:** the docs agent, in a DECISIONS entry (section 10).
- **The alternative:** to keep D-024's wording, drop the two 2023 commands at the end of Appendix A, and T2's 2023 days, until T0 has dated the switch.

**Weeks:**
- **One ISO week per month:** the week holding the 15th in odd years, and the week after it in even years.
  - Without the shift, ISO-week parity would follow the month: the week of the 15th is week 24 in June and week 33 in August in each of 2023, 2024 and 2025.
  - A parity split of dev-fit and dev-check would then put whole months on one side.
  - With the shift, every summer month in T1a has both parities. For example, June has 42 viewpoint-days in even weeks and 21 in odd weeks.
- **Eval has no dev-fit/dev-check rule yet.** Either a parity rule or alternating the sampled weeks works on this sample.
- **May 2026 gets two weeks** (18–24 and 25–31 May): the last development data before the held-out season, and the camera state closest to it.
- **Only available days are queued,** using the masks of 19 Nov. A queued day without frames would still cost one search.

## 5. Tranches and order

| Tranche | What | Day entries | Requests (expected) | Frames | Frame bytes, mean (range) | Finishes (UTC day) |
|---|---|---|---|---|---|---|
| **T0** map | `midday` hires on a 14-day grid in each switch bracket, plus an available day inside any wider gap (4 days); a 16:9 reference per camera; Big Bend fortnightly to Dec 2024; Grand Canyon weekly Nov 2024 – Mar 2025 | 134 | 256 | 134 | 7 MB (1–14) | 20 Nov |
| **T1a** summer weeks | Jun, Jul and Aug of each development summer; 18–24 and 25–31 May 2026 | 313 | 4,478 | 4,171 | 226 MB (43–423) | 24 Nov |
| **T1b** shoulder weeks | Apr, May, Sep, Oct; Apr 2026; Point Reyes Dec and Jan | 367 | 4,603 | 4,237 | 229 MB (44–429) | 29 Nov |
| **T3pr** Point Reyes plate inputs | Four nights (every hour) and six clear days (every 15-min frame), on T1 days | 10 (8 days) | 250 | 250 | 14 MB (3–25) | 29 Nov |
| **T2** summer completion | Every remaining available day of Jun–Aug: Point Reyes 2025, 2024 and 2023, then the rest, 2025 then 2024 | 738 | 10,505 | 9,773 | 529 MB (102–990) | 10 Dec |
| **T1c** winter weeks | Nov–Mar (Grand Canyon Dec 2024 – Feb 2025 held for T0) | 409 | 4,531 | 4,128 | 224 MB (43–418) | 15 Dec |
| **All** | | 1,971 | 24,623 | 22,693 | 1.23 GB (0.24–2.30) | |

**Development days per camera** (hourly entries):

| Camera | After T1a | After T1b | After T2 | After T1c | Jun / Jul / Aug days after T2 |
|---|---|---|---|---|---|
| Point Reyes | 70 | 183 | 359 | 420 | 60 / 79 / 93 |
| Grand Canyon | 47 | 96 | 199 | 248 | 30 / 48 / 58 |
| Yosemite | 56 | 119 | 261 | 346 | 60 / 62 / 62 |
| Acadia | 56 | 119 | 261 | 352 | 60 / 62 / 62 |
| Smokies | 49 | 98 | 202 | 272 | 29 / 48 / 62 |
| Big Bend | 35 | 65 | 136 | 189 | 30 / 31 / 31 |

**Why this order:**
- **T0 first.** It costs 256 requests and finishes on the first day.
  - Its frames show where the spans start. This needs no viewing: each sidecar's `original_height` reads 666 in the 3:2 epoch and 562 in the 16:9 epoch.
  - **Brackets.** Every switch is bracketed to within 14 days, except Grand Canyon's 24 Apr – 20 May 2024 (26 days): the archive has no Grand Canyon frames from 26 Apr to 19 May 2024.
  - **Gap fillers.** Four days split wider gaps where the mask has frames: 22 Aug, 5 Sep and 29 Sep 2023 at Yosemite, and 16 Apr 2024 at Grand Canyon.
  - **View grids.** Big Bend's grid has one 32-day gap, 12 Jul – 13 Aug 2024, again with no frames inside. Grand Canyon's weekly grid has none wider than 7 days.
  - The results decide what T1b, T2 and T1c may add.
- **T1a next.** It covers the held-out season's months, newest summer first, then the camera state just before the guard.
  - After T1a, every camera has 5–10 whole weeks.
  - **What it feeds.** It is the first design-H development sample: extractor v0 at the design-H cameras, a first τ, persistence, a first S_mos, the block length and a first MDE estimate. Whether it is enough is measured there, not assumed here.
  - **Not an M1.5 dependency.** M1.5's gate is P1 at Rainier, on our own captures.
- **T1b before T2.** It finishes the stratified sample across seasons before completeness. The shoulder months add clear days and the lower Sun bins of early and late summer.
- **T3pr after T1b.** Point Reyes's plate is M2 work (PRD §16; PLAN M2), and for K2 under design H its inputs must be archive frames from before the held-out season (§13.9).
  - **Nights** (§8.5 step 2, the night frame and lights mask): one T1 day per season, `--pick hourly` with no Sun limit. T1 has stored the daylight hours already, so each night costs 9–12 GETs. The days are 21 Jan 2026, 27 May 2026, 13 Aug 2025 and 15 Oct 2025: the Wednesday of the newest Point Reyes T1 week of each season.
  - **Clear days** (§8.5 step 8, the sunrise and sunset check): six clear Point Reyes days, with every 15-min frame at NPS Sun ≥ −2°, at 29–40 GETs each.
  - **Who chooses them.** Eval or the owner pick them from T1a and T1b frames as those land; development frames may be viewed.
  - **The cost model.** The plan's cost uses the Wednesdays of the six newest Point Reyes T1 weeks in Jun–Oct.
  - **Order.** The queue works its entries in file order, and `archive queue add` only appends. So queue T3pr after T1b and before T2, and hold T2 and T1c until it is in.
  - **If T2 was queued first.** Remove T2's pending entries and add them again after T3pr. Nothing is lost: `state/archive/` keeps stored frames from being fetched twice.
  - **Landing.** It finishes on 29 Nov, inside T1b's last day, and T2 and T1c keep their dates.
- **T2 before winter.** It completes the climatology cells (hour × month, now 29–93 days each) and fills nearly every Sun bin with ≥ 10 candidates. Design H holds out a summer, so T2 adds more than winter weeks do.
- **T1c last.** Winter weeks serve the Tier 0 library and year-round fits; they add the least to a summer held-out season.
- **T3, after T1c (adaptive, small).** Chosen from T1's results, for the other cameras and the secondaries:
  - full 15-min days (`--pick all`) on a few clear summer days per camera, for any thin clear-day bins;
  - nights for the other cameras' plates, which are v1 growth (PRD §16);
  - the 15-min frames at issue times for the short-lead secondaries.
- **A quirk for night picks.** `pick_assets`'s hourly rule gives a frame after 11:30 PM to hour 00 of its own date. So with no Sun limit, hour 00 can be the 11:45 PM frame when the 12:00 AM one is missing.
  - It never touches a Sun ≥ 2° entry, and a night frame is still a night frame.
  - The fix belongs to capture's owner (section 10).

## 6. Requests, bytes and time

**Requests per day entry:**
- **One `Search`.** A day holds at most 96 frames, under the 300-a-page size, so one page. It is cached once the day is two days old, so a later entry for the same day costs no search.
- **One GET per selected frame,** minus any frame already stored.
- **T0's searches.** Its 122 searches count only the days not already cached: the archive check already searched 12 of T0's 134 days (the 11 references and Big Bend's 2024-07-12).
- **T3pr's entries** are on days T1 searched, so they cost GETs only, and only for frames not yet stored.

**Measured inputs** (re-measured on 20 Nov with `measure_inputs.py`, Appendix B):

| Quantity | Value | Source |
|---|---|---|
| `hires` asset | 54,141 bytes mean (n = 9; median 59,631; 10,419–101,326). 8 are 999×666, 1 is 999×562; all midday frames | pre-guard sidecars, `content_length` |
| Day search response | 49,372 bytes mean (n = 92) | 19 Nov capture log, `archive_request` |
| Cached search file | 24,462 bytes mean (n = 76) | `cache/nps-archive/search/` |
| Sidecar | 1,619 bytes mean (n = 85) | `archive/` |
| Request time | Search: median 215 ms, mean 534 ms, p90 1.64 s, max 4.55 s (n = 92). GET: median 216 ms, mean 366 ms, max 837 ms (`hires`, n = 9); median 210 ms, mean 420 ms (thumbnails, n = 76) | 19 Nov capture log; p90 by inclusive interpolation (1.18–2.12 s by other methods) |
| Spacing, back to back | End to start: 5.01 s median over all 216 pairs, never under 5.00 s. Start to start: median 5.22 s and mean 5.62 s over pairs under 25 s apart (n = 201); mean 5.27 s under 6.5 s (n = 184) | 19 Nov capture log; start = log time − elapsed |
| Errors | 8 of 215 (3.7 %): 7 TLS connect timeouts (about 10 s each with that run's client) and 1 disconnect (30.1 s). 7 of the 8 fell within 10.4 min. The 2 deliberate paging probes are left out. | 19 Nov capture log |
| Live pass | First 73 passes: 9.3 s median, 16.9 s p90 (inclusive), 19.5 s max; they start 315.4 s apart (median of gaps under 400 s; 318.9 s over all gaps). Capture 0.2.0: 11.9 s median, 47.7 s max (n = 24, 02:39–06:40 UTC on 20 Nov) | 19–20 Nov capture logs |
| Live traffic | 370 requests and 189 MB in 3.03 h (00:04–03:06 UTC, 20 Nov): about 2,935 requests and 1.5 GB per 24 h | 20 Nov capture log |
| Free disk | 24 GiB of 460 GiB | `df`, 06:38 UTC, 20 Nov |

**Byte estimates rest on midday frames.** The 9 sizes are all midday frames, and only one is 16:9. Morning, evening and fog frames may differ. The ranges in the tables multiply by the smallest and largest measured frame. After the first day of T1a, `archive queue list --json` gives the real bytes per entry.

**Time model, measured on the real queue runner.**
- **How.** `loop_sim.py` drives capture's `QueueRunner.run_idle` the way the loop calls it (`live.py`): the next pass is due 300 s plus 0–30 s of jitter after the last one started, and the idle hook gets the time left.
  - **Real parts:** the 5 s file limiter, the persisted daily budget, a copy of the first-tranche queue and real storage, all on a fake clock in a scratch data dir.
  - **Simulated parts:** nps.gov is a mock transport whose answers take the measured request times. Pass durations are drawn from the 22 passes of capture 0.2.0 logged when the runs began, and, as a check, from the first 73.
  - **Runs:** 20 seeds per scenario, each spending one UTC day's 1,000 requests.
- **Results**, in hours of loop uptime to spend one day's 1,000 requests:

| Errors | Hours, mean (range) | Requests per full window | Failed requests per day |
|---|---|---|---|
| None (passes of capture 0.2.0) | 1.76 (1.74–1.78) | 49.5 | 0 |
| None (the first 73 passes) | 1.75 (1.73–1.77) | 49.9 | 0 |
| Independent connect timeouts at 3.7 % | 2.11 (2.00–2.21) | 41.4 | 38 (26–48) |
| The 19 Nov sequence of outcomes, replayed | 2.10 (2.07–2.14) | 41.5 | 37 (35–40) |

- **Why errors cost time.** A failed request costs its own timeout: 5 s to connect, 10 s to read, in the queue's client. Then the limiter defers the next request by 30 s, doubling on consecutive failures (`archive.py`, `_send`). A request starts only if the wait plus 35 s fits before the next pass, so near the end of a window a deferral ends it.
- **Checks in the same runs.** No two requests ran closer than 5.0 s end to start, and none started with less than 35.0 s before the next pass. Every run spent exactly the 1,000-request budget, and no entry failed.
- **The analytic check agrees with the no-error runs.** It gives 49 requests a window, 560 an hour, so 1.79 h a day. The first request of a window needs 35 s left, each later one 40 s (its 5 s wait plus 35 s), and each takes 5.62 s with its wait.
- **Correction.** The first version of this plan said 48 requests a window and 1.82 h a day. It left out the window's first request and the time errors cost.
- **The budget binds while the Mac is awake.** On 19 Nov (UTC) the loop ran 40 passes, about 3.5 h, even on battery.
- **But the Mac does sleep.** On 20 Nov the loop ran without a gap from 00:04 to 04:10 UTC. From 04:10 to 06:29 UTC its passes were 53, 40, 36 and 11 min apart, which looks like the Mac sleeping.
- **Clustering costs no more.** The 19 Nov pattern, with 7 of its 8 errors in 10.4 min, costs no more than independent errors at the same rate, because a success between two failures resets the deferral. A run of consecutive failures would cost more, since the deferral doubles each time (30, 60, 120 s and so on).
- **Errors.** At 3.7 %, about 963 useful requests a day remain. A failed GET is retried in the entry's next sweep, up to 3 sweeps.

**Finish dates.** The tranche table assumes the first tranche is queued on 20 Nov (UTC) and the budget is used every day. Each UTC day on which the loop runs for less than about 2.2 h moves every later date back by up to a day. If the first tranche is queued too late on 20 Nov to leave 2.2 h of loop time before 00:00 UTC, every date moves back by a day too.

**Disk.** The whole plan needs about 1.33 GB, against 24 GiB free:
- frames: 1.23 GB;
- sidecars: 37 MB;
- cached searches: 47 MB;
- capture-log lines: about 16 MB.

The live capture's growth is in D-028.

## 7. Budget and cap

- **Daily budget: keep 1,000.**
  - **Politeness first.** The confirmation email ([docs/outreach/nps-confirmation.md](../outreach/nps-confirmation.md)) has not been sent. It is the message that asks whether automated collection is acceptable and whether there is a preferred way to get archive images in bulk. The archive API is undocumented (D-018).
  - **The load is small.** 1,000 requests and about 54 MB a day add 34 % to our requests to www.nps.gov and about 3.6 % to our bytes (live capture: about 2,935 requests and 1.5 GB a day, measured above).
  - **The schedule doesn't need more.** At 1,000 a day, T0–T1b and T3pr land on 29 Nov.
  - **Why not more.** A raise would only pull T2 (10 Dec) forward, and design H's freeze does not bind on 14 Dec. Raise it only if NPS answers yes to bulk collection. At 2,000 a day (about 4.2 h of loop time), T2 would finish on 30 Nov.
  - **Why not less.** Halving it would push T1b and T3pr to 9 Dec, the eve of M2, and T2 to 31 Dec.
- **Total cap: keep 5 GB.**
  - The whole plan downloads 1.23 GB, or 2.30 GB if every frame were the largest measured.
  - That leaves room for T3 and for the summer-2026 frames that scoring fetches later under `--held-out-override`. Those are about 7,700 frames and 0.4 GB at the same hourly rule (92 days × 6 cameras), and they count toward the same total, which the queue reads from `state/archive/`.
- **Per-viewpoint `--max-gb`: keep the default of 1.0.**
  - The largest is Point Reyes: 0.28 GB, or 0.53 GB at the largest frame.
  - Note: the cap compares the viewpoint's lifetime `bytes_downloaded`, not one entry's.
- **The email's "[N] frames".** For the owner to fill in: "about 4,300 frames in the first five days (about 0.25 GB), and about 22,700 frames (about 1.3 GB) by mid-December, at most 1,000 requests a day, one at a time, at least 5 seconds apart". The gigabytes include the day searches.

## 8. The first tranche

**How the lead runs it.**
- **Where the script is.** The script is in [Appendix A](#appendix-a-first-tranche-commands). A copy is `out/first-tranche.sh` in the session scratch folder (`w3/backfill/`, Appendix B); if a reboot has cleared that folder, save Appendix A as a file.
- **Two safeguards.** It refuses to run unless `Q` names the queue file, so a test run can never touch the live queue. It finds the repo with git, so run it from inside the repo (or set `REPO` to the repo's root).

```sh
Q=$HOME/lookout-data/state/archive/queue.json sh first-tranche.sh
```

- It runs 62 `archive queue add` commands and a final `archive queue list`.
- It edits the queue file under its lock and never contacts nps.gov, so it is safe while the loop runs (D-029). It took 7.6 s on a scratch queue.
- The running loop picks the entries up in its next idle window.

**Validated on scratch queues** (20 Nov, about 06:30 UTC):
- **The queue.** `queue.json` was built with the real `make_entry` and `ArchiveQueue` from `workers/capture`: 447 entries, every one `pending`, dated 2023-01-30 to 2026-05-31.
  - Of these, 134 are `hires/midday/any-sun` and 313 are `hires/hourly/sun>=2`.
- **The script.** Run with `Q` set to a second scratch file, it built 447 entries identical to `queue.json`, in the same order, field by field except `added_at`.
- **The later tranches.** The full plan (T0 to T1c, 1,971 entries) was rebuilt the same way. The scripts were the first tranche and the four drafts, with `draft-T3pr.sh` given the six modelled clear days. The result was also identical.
- **The listing and the status.**
  - `lookout-capture archive queue list --queue <scratch>/queue.json` exits 0 and lists them.
  - `lookout-capture status --archive-queue <scratch>/queue.json` exits 0 and reports `{"pending": 447}` (`{"pending": 1971}` for the full plan).
- **A spelling fix.** The task's spelling, `archive queue list --archive-queue <path>`, is refused by argparse (exit 2). In `archive queue`, the option is `--queue`; `--archive-queue` belongs to `loop` and `status`.
- **Refusals.** Without `Q`, or for `draft-T3pr.sh` without `EVENINGS`, a script exits 1 before writing anything. Run outside the repo with no `REPO`, it exits 128 (git) and writes nothing.
- **The live queue was never touched.** It still does not exist.
- **The code is the loop's.** The snapshot the loop runs (`cache/runtime/034c46b`) has byte-identical `archive_queue.py`, `archive.py`, `heldout.py`, `paths.py` and `timeutil.py`, and the same `loop()` in `live.py`. Its `cli.py` differs from the working tree only by a `storage-report` alias that another package is adding, outside `archive queue`.

**What to watch:**
- `lookout-capture status` (no network) shows the queue's `by_status` and the runner's heartbeat.
- `lookout-capture archive queue list --json` shows each entry's requests and bytes.
- The budget file is `state/archive/budget-archive-queue-<UTC date>.json`.

**After T0 finishes** (on the first day). Read the frame size from the sidecars; no image needs to be viewed:

```sh
python3 -I -c 'import json,glob; [print(d["viewpoint_id"], d["image_time_utc"][:10], d["original_width"], d["original_height"]) for d in sorted((json.load(open(f)) for f in glob.glob("'$HOME'/lookout-data/archive/*/*/*.json")), key=lambda d: (d["viewpoint_id"], d["image_time_utc"])) if d["kind"] == "hires"]'
```

- **666 then 562** dates each switch to within 14 days, except at Grand Canyon. Its bracket of 24 Apr – 20 May 2024 is 26 days, because the archive has no frames inside it.
- **Then:**
  - where T0 shows a switch came earlier, extend the spans in `plan.py` (June 2023 at Point Reyes; June–July 2024 at Grand Canyon and the Smokies; Aug–Dec 2023 at Yosemite and Acadia), and generate T1b, T3pr, T2 and T1c again;
  - ask eval for the registration of the Big Bend and Grand Canyon check frames against their 16:9 references (`lookout_eval.registration`, D-042);
  - queue T1b while T1a runs.

**After T1a lands (24 Nov).** Choose T3pr's six clear days from Point Reyes's T1a and T1b frames, and queue the T3pr draft before T1b finishes (29 Nov), then T2, then T1c:

```sh
Q=$HOME/lookout-data/state/archive/queue.json EVENINGS='<six YYYY-MM-DD days>' sh draft-T3pr.sh
```

- `make_entry` refuses any day on or after 1 Jun 2026, so a wrong day cannot reach the held-out season.

**Optional, for eval's next backtest pair.** Section 12 of [the backtest pair's evidence](../evidence/m05-backtest-pair.md) asks for same-aim plate candidates. These are 12 days at Grand Canyon in the 3:2 epoch, which D-024 and D-042 allow for backtests.
- **Why not `midday`.** On those days the highest-Sun frame is 12:30 MST, at azimuth 178–179°. That is the bin next to the pair's (30–35°, 180–200°).
- **The 13:00 frame matches.** It sits at azimuth 186–188° and altitude 30.6–32.2°, so it is in the pair's bin.
- **The command.** About 41 requests on complete days: 12 searches and 29 frames, mostly 12:00 and 13:00 MST. 2024-01-06 has no frames.

```sh
cd "$(git rev-parse --show-toplevel)/workers/capture"
PYTHONPATH=src:../common/src uv run --no-sync lookout-capture archive queue add grand-canyon.yavapai 2024-01-01 2024-01-02 2024-01-03 2024-01-04 2024-01-05 2024-01-07 2024-01-08 2024-01-09 2024-01-10 2024-01-11 2024-01-12 2024-01-13 --hires --pick hourly --min-sun-deg 30 --queue "$Q"
```

## 9. The held-out guard

**Every planned date falls on or before 31 May 2026.** The planner asserts it, and the listings of both scratch queues show it: 2023-01-30 to 2026-05-31. Four layers refuse anything later:

1. **When an entry is added.** `make_entry` raises for any day on or after `HELD_OUT_FROM` (2026-06-01), so `archive queue add` exits 1 and writes nothing. Checked on a scratch queue:
   - `add point-reyes.beach 2026-06-01`, `... 2026-05-31 --through 2026-06-02`, `... 2026-05-30 2026-05-31 2026-06-01` and `... 2026-07-15` each exit 1 with "refused: … the D-019 held-out guard", and no queue file is written;
   - so do the other ISO spellings of 1 Jun, `20260601` and `2026-W23-1`;
   - one bad day refuses the whole command;
   - `add point-reyes.beach 2026-05-31` is accepted.
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
     - Every stored frame is on 31 May in its camera's civil time.
   - So a 31 May entry can never pull a frame from 1 Jun. Every planned 31 May entry is `sun>=2`, so its late-evening frames are not selected anyway.
4. **When eval reads.** Eval's `FrameStore` takes only the committed split (`heldout_periods.json`, committed in `d9aa416`, SHA-256 `c437544e…`). It reads archived frames dated on or after 1 Jun 2026 only through `SealedScoring(<pre-registration commit>)` (D-038).

**Scoring is the only exception.** The queue never takes `--held-out-override`. Only `archive day|sample --held-out-override "<reason>"` releases held-out frames, it is meant for scoring, and it is logged with every frame it releases.

**Tests.**
- **Capture's guard tests pass:** 7 passed, 44 deselected (`test_archive_queue.py` and `test_archive.py`, `-k "held_out or guard or refus"`). They cover:
  - `make_entry` at 31 May and 1 Jun;
  - a hand-edited entry refused with zero requests;
  - a CLI range across the guard refused whole;
  - the four-date reading;
  - the override.
- **Capture's whole offline suite passes:** 190 passed (20 Nov, working tree).
- **Two proposed tests pass** against the same code: the end-to-end 31 May run above, and the ISO spellings (6 cases, `capture-tests/test_heldout_boundary.py`, Appendix B). They are proposed for capture's own suite (section 10).

## 10. For the lead and the owner

1. **Queue the first tranche.** Only the lead edits the live queue, with the command in section 8.
2. **Point Reyes in winter, for design P: the owner's reading.** T1b fetches Point Reyes's December and January weeks of 2023/24 to 2025/26: 38 available days. They could give design P's sealed window a climatology and winter Sun-bin references.
   - **The conflict.** PRD §13.9 says design P's development runs from go-ahead to 14 Dec 2026.
   - **What the reading decides.** Whether earlier archived winters may feed design-P fits at Point Reyes, at 999 px against live 1920 px.
   - **Either way the frames are useful.** They serve K2 in winter and Tier 0.
3. **Send the NPS email, with [N] filled in** (section 7). Until NPS replies, the budget stays at 1,000.
4. **Choose T3pr's clear days.** Six clear Point Reyes days from T1a and T1b frames, queued with `draft-T3pr.sh` before T1b finishes and before T2 (section 8). Eval, the plate's builder or the owner can choose.
5. **Archived HRRR for 361 development dates.** One archived HRRR run serves every viewpoint of a date, because the GRIB messages are CONUS-wide. But the cameras span four time zones, so a date needs the union of their daylight valid hours.
   - **Count.** T1a to T2 hold 361 distinct dates, with 16.2 valid hours per date on average (9–19) and 5,830 distinct valid hours in all. With T1c: 424 dates and 6,835 hours.
   - **Size.** At the backtest pair's measured 16.5 MB a forecast hour (12 fields), the 24 h lead alone is about 96 GB of transfer through T2, and about 113 GB with T1c. The files are deleted after extraction.
   - **The list.** The UTC valid hours of every date are in `hrrr-valid-hours.json` (Appendix B). T0 and T3pr need no HRRR.
   - **Correction.** The first version said 360 dates and about 14 hours a date (83 GB). It took one camera's daylight, not the union over the cameras.
   - **Owner.** That is the forecast work package's job, not this queue's.
6. **Point Reyes 2023 (docs agent).** Record the reading of D-024 in section 4, or drop the 2023 weeks.
7. **The dev-fit/dev-check rule (eval).** Either ISO-week parity or the order of the sampled weeks works on T1 (section 4).
8. **Capture's owner.**
   - **The hourly quirk.** `pick_assets` gives a frame after 11:30 PM to hour 00 of its own date (section 5). Roll the date forward when the slot wraps to 0, or drop such frames from the day's slots.
   - **Two proposed tests.** Add the two tests of `capture-tests/test_heldout_boundary.py` to `tests/test_archive_queue.py`.
9. **Not checked here:**
   - the clear-sky share per camera (TODO(M1.5), from extractor v0 on T1a);
   - re-aims inside each span smaller than the thumbnails show (TODO(M2), Same view registration, D-042), which may exclude some weeks later.
10. **Keep the planner, if wanted.** The planner, its 14 tests, the simulator and the measurement script sit in the session scratch folder, which a reboot can clear.
    - **State:** lint-clean under capture's ruff settings. Under eval's, only one import block's grouping differs, and `ruff --fix` re-sorts it.
    - **Home:** the planner needs both capture's queue code and eval's extractor, so it belongs where both import, for example `eval/` with capture on the path. Moving it is the lead's call.

## Appendix A: first-tranche commands

T0 (134 entries), then T1a (313 entries), in queue order: 447 entries in all. Generated by `plan.py` and run on a scratch queue as described in section 8. Run it from inside the repo.

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

**Planner.** `plan.py`, run from `eval/` with `PYTHONPATH=src:../workers/common/src:../workers/capture/src`.
- It disables sockets before importing anything else, so it cannot reach the network.
- It uses capture's own `AvailableDays`, `pick_assets`, `make_entry`, `ArchiveQueue`, `HELD_OUT_FROM` and `solar_elevation_deg`, eval's `noaa_solar` for azimuths, and eval's `ClearDayReference.lookup` for the coverage.
- **Complete-day model.** For any day without a cached search, a complete day is modelled as frames at :00, :15, :30 and :45 local standard time, carrying the geometric Sun that NPS reports. That model goes through `pick_assets` and is scaled by the camera's measured share of a complete day.
- **Entries on a day already worked** (T3pr, and a T1c week that meets a T0 day) cost no search and no GET for frames an earlier entry stored.

**Simulator.** `loop_sim.py`, run the same way. It drives the real `QueueRunner` on a fake clock against a mock nps.gov whose search answers carry real-format `AssetID` and `LocalTimeString` values, and it deletes each run's scratch data dir afterwards. Its request times come from pre-guard assets only: their ids are read from the sidecars on disk.

**Files.** All are in the session scratch folder (`w3/backfill/`), which a reboot can clear:

| File | What it holds |
|---|---|
| `scripts/plan.py`, `scripts/test_plan.py` | The planner and its 14 tests |
| `scripts/loop_sim.py` | The time model on the real queue runner |
| `scripts/measure_inputs.py` | The measured inputs of section 6 |
| `queue.json` | The first tranche: 447 entries |
| `queue-plan-all.json` | Every tranche: 1,971 entries (T3pr with its six modelled clear days) |
| `out/first-tranche.sh` | Appendix A |
| `out/draft-T1b.sh`, `out/draft-T3pr.sh`, `out/draft-T2.sh`, `out/draft-T1c.sh` | Drafts, to generate again after T0 |
| `out/plan.json` | Every entry, with its tranche, ISO week, reason and expected cost |
| `out/analysis.json` | Every planner measurement above |
| `out/hrrr-valid-hours.json` | The UTC valid hours per development date (T1a–T1c) |
| `out/loop-sim.json`, `out/measure-inputs.json` | The time model's runs and the re-measured inputs |
| `cli-check/` | The CLI validation queues and output |
| `capture-tests/` | The two proposed capture tests |

**Held-out note.** One log summary run for the first version of this plan printed a grouped mean of the byte sizes of the 8 archive hires requests from 19 Nov that are not on disk: those of 15 Jul and 17 Nov 2026, now in quarantine. It is request metadata, which CLAUDE.md allows for any date, but no number in this plan uses it. The hires sizes above come from the 9 pre-guard sidecars only.
