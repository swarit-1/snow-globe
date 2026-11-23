# M0.5 gate 1: 72 h of worker and capture logs, closed at 23:46:10.754 UTC on Sun 22 Nov 2026

> **Names:** the records here keep the names their runs used. Until the switch, the capture loop ran `lookout-capture/0.2.1` (to 23:58:09 UTC on 22 Nov) and the stand-in forecast 0.1.0 (to 00:03:10 UTC on 23 Nov); the data dir was `~/lookout-data` until 23:58:09, and nps.gov saw `Lookout/0.1 (webcam research capture; fetch-on-change)`. Since then the data is in `~/snow-globe-data`, `~/lookout-data` is a symlink to it, and the loops run `snow-globe-capture/0.3.0` and `snow_globe_forecast` 0.3.0 from snapshot `aee6f92` (D-080, D-090).

**Gate item** (PRD §16 M0.5; PLAN.md, M0.5, gate item 1): "≥ 72 h of worker and capture logs: runs and frames per viewpoint, plus exclusions."

**Measured** by the lead's `make data-report` (eval's `snow-globe-eval data-report` at `aee6f92`, read-only) at 23:46:31.858 UTC on Sun 22 Nov 2026, on the MacBook Air M4 (macOS 26.5.1). The clock rule is the lead's of 20 Nov (D-069), with D-078's ruling as narrowed on 21 Nov (D-082). The docs agent re-read the evidence files, the logs, the lock, the queue and the power log at 00:10–00:28 UTC on 23 Nov. Each figure below names its source. The decisions are D-089 (the close) and D-090 (the switch).

## 1. Result

| Item | Value |
|---|---|
| Met at | **2026-11-22T23:46:10.754Z** (the report's `met_at_utc`) |
| Window | from 2026-11-19T12:24:23.000Z, the loop's start with all 8 cameras, to the read at 23:46:31.858Z: 83.37 h |
| Covered | **72.01 h**; in the JSON, read at 23:46:33.171Z, `covered_h` 72.006 against `required_h` 72.0 |
| Lost | 11.36 h (`lost_h` 11.363): capture outages 11.001 h, the two restarts' excess 0.264 h and forecast outages 6.039 h, an overlap counted once |
| For comparison | PLAN.md's capture-only figure: met at 23:24:24.967Z (11.00 h lost). Capture alone, with the restarts' excess: 23:32:52.611Z (11.14 h). Forecast alone: 18:26:42.991Z (6.04 h). |
| Capture | 830 passes by `cli-loop` (0.1.0 69, 0.2.0 174, 0.2.1 587), median spacing 314.03 s; 825–827 polls and 275–280 image GETs per camera; every logged frame and thumbnail on disk, none unlogged |
| Forecast | 85 live runs first written in the window at each of the 8 viewpoints, 2026111900 to 2026112222 (2 of them catch-ups), none never written; 723 distinct live lags; 0 errors |
| Read again | by the switch's gate check at 23:53:34.551Z (72.123 h covered of 83.487 h, met at the same instant), and on `~/snow-globe-data` after the switch, to 00:06:28.747Z (72.34 h of 83.70 h, met at the same instant) |

The gate's other items: the archive report (item 2) and the backtest pair (item 3) were done on 19–20 Nov. Item 4, the emails, waits for the owner, so M0.5 stays in progress for that alone.

## 2. The evidence files

They are outside the repo, in `~/lookout-data/reports/m0.5-gate/`. The folder moved with the data at the switch, so it is also `~/snow-globe-data/reports/m0.5-gate/`. The lead ran the two reports at 23:46:31–23:46:33 UTC and saved them there, with the rehearsal log, at 23:49:29 UTC on 22 Nov; the switch's log and the post-switch report followed at 00:06:28 UTC on 23 Nov (the files' birth times, `stat`). The lead hashed each file when it was saved, and the docs agent hashed them again at about 00:10 UTC on 23 Nov; the two agree.

| File | What | SHA-256 |
|---|---|---|
| `data-report-20261122T234633Z.txt` | the gate's table (section 3): `make data-report`, read-only, to 23:46:31.858Z; 10,768 bytes | `98938ea26a8485ce891e1e9d2ef7c6eebaa684c7e528a7a0254303f72c07aa40` |
| `data-report-20261122T234633Z.json` | the same report as JSON (`make data-report DATA_REPORT_ARGS=--json`), to 23:46:33.171Z; 197,439 bytes | `93b89d23e801b1843f2595729e747da3eda053490bf0338bc3e035b41090745f` |
| `switch-dry-run-20261122T234851Z.log` | the passing rehearsal of the switch, 23:48:49–23:48:56 | `61ab02f038dc16b0001fbe8c4370e83cb630492800a50c23d24e14be2ecac6c8` |
| `switch-20261122T235334Z.log` | the switch, 23:53:34 on 22 Nov to 00:03:13 on 23 Nov | `6cbeb8b5e6ad6f9c628488858eabe4b2ec6d5c6a9d92ec3eb75123adf43015fc` |
| `data-report-20261123T000629Z-postswitch.txt` | the report read on `~/snow-globe-data` after the switch, to 00:06:28.747Z | `b69a9793e8bfca9772e0453e756f7234945b3daf0f445b8ce14cb9bda74ba663` |

## 3. The table at the close

`data-report-20261122T234633Z.txt`, in full. Its first line is the command that `make data-report` ran.

```text
cd eval && PYTHONPATH=src:../workers/common/src:../workers/forecast/src uv run --locked snow-globe-eval data-report 
Snow Globe data report (PRD §16 M0.5, PLAN.md gate 1): 2026-11-19T12:24:23.000Z to 2026-11-22T23:46:31.858Z (83.37 h)
data dir: ~/lookout-data

72 h clock, gate rule (the lead's of 20 Nov, settling D-061: the union of both jobs' outages and each capture restart's excess): 72.01 h covered of 83.37 h elapsed; 11.36 h lost (capture outages 11.001 h, restart excess 0.264 h, forecast outages 6.039 h, an overlap counted once); met at 2026-11-22T23:46:10.754Z
  PLAN.md's capture-only figure (capture outages alone; a restart that is not an outage loses nothing): 72.37 h covered; 11.00 h lost; met at 2026-11-22T23:24:24.967Z
  capture alone (outages and restart excess): 72.23 h covered; 11.14 h lost; met at 2026-11-22T23:32:52.611Z
  forecast alone: 77.33 h covered; 6.04 h lost; met at 2026-11-22T18:26:42.991Z

Capture: 830 passes by cli-loop (lookout-capture/0.1.0 69, lookout-capture/0.2.0 174, lookout-capture/0.2.1 587); median spacing 314.03 s
viewpoint                    polls  GETs  full thumb  downloads by policy      capped errors  longest gap  min send gap GETs/h
acadia.mcfarland               827   278    92   244  legacy 25, hourly 244       260      4    164.7 min     300.065 s      4
big-bend.panther-junction      827   277    91   249  legacy 23, hourly 249       267      3    171.9 min     300.072 s      4
grand-canyon.yavapai           827   276    91   245  legacy 24, hourly 245       280      5    171.9 min     300.061 s      4
grand-teton.science-center     827   277    92   250  legacy 25, hourly 250       275      2    171.9 min     299.243 s      4
point-reyes.beach              827   278   277   252  legacy 25, every 252        269      0    171.9 min     299.134 s      4
rainier.paradise-mountain      827   280   279   256  legacy 23, every 256        547      1    174.5 min     299.171 s      4
smokies.look-rock              825   275    92   249  legacy 24, hourly 249       272      3    174.5 min     299.064 s      4
yosemite.turtleback            827   279    90   238  legacy 23, hourly 238       285     15    174.5 min     299.156 s      4
  bases: longest gap and min send gap are send to send (sent_at, else t - elapsed_ms), as capture's scheduler spaced the polls; GETs/h is the most image GETs sent in any rolling hour (send times)
  disk: every logged frame and thumbnail is on disk, none unlogged

Polite collection at nps.gov (PRD §15.3): the least gap or span nps.gov can have seen, whatever the network did, and how many pairs or windows are unproven
viewpoint                    poll pairs    least gap  unproven   5-GET windows    least span  unproven
acadia.mcfarland                    826    299.588 s         2             274    3698.474 s         0
big-bend.panther-junction           826    300.002 s         0             273    3702.388 s         0
grand-canyon.yavapai                826    300.002 s         0             272    3699.725 s         0
grand-teton.science-center          826    299.087 s         1             273    3699.579 s         0
point-reyes.beach                   826    299.060 s         1             274    3699.520 s         0
rainier.paradise-mountain           826    299.107 s         2             276    3612.816 s         0
smokies.look-rock                   824    299.001 s         2             271    3701.952 s         0
yosemite.turtleback                 826    299.093 s         2             275    3698.391 s         0
  unproven: not provably 300 s (3600 s for 5 GETs) apart at nps.gov, because the least gap (span) is under it or a send cannot be placed; that is not the same as shown closer
  bases: least gap = the next poll's earliest receipt - the previous poll's latest; least span = the 5th GET's earliest receipt - the 1st's latest, over every 5 consecutive GETs. earliest = sent_at, else t - elapsed_ms - 0.05 ms; latest = answer_bound_at (a request that failed after it may have left: 300 s after the failure), else answered_at, else t + 1 ms (t is written after the answer, to the ms)

Forecast: live HRRR runs first written in the window, per viewpoint (a later write of a run is a re-extraction, never a run; runs include the catch-ups, due before the window)
viewpoint                    runs catch-up  first      last       hours   fields on disk (runs)    re-extr rewritten never
acadia.mcfarland               85        2  2026111900 2026112222 0,1,2   8 (81); 7 (4)                 12        12     0
big-bend.panther-junction      85        2  2026111900 2026112222 0,1,2   8 (81); 7 (4)                 12        12     0
grand-canyon.yavapai           85        2  2026111900 2026112222 0,1,2   8 (81); 7 (4)                 12        12     0
grand-teton.science-center     85        2  2026111900 2026112222 0,1,2   8 (81); 7 (4)                 12        12     0
point-reyes.beach              85        2  2026111900 2026112222 0,1,2   8 (81); 7 (4)                 12        12     0
rainier.paradise-mountain      85        2  2026111900 2026112222 0,1,2   8 (81); 7 (4)                 12        12     0
smokies.look-rock              85        2  2026111900 2026112222 0,1,2   8 (81); 7 (4)                 12        12     0
yosemite.turtleback            85        2  2026111900 2026112222 0,1,2   8 (81); 7 (4)                 12        12     0
  723 distinct live lags; 0 errors; median fetchable lag 3322.0 s; 78 quiet gaps over 900 s between stand-in records (normal between hourly runs, not outages)

Outages and restart excess (lost = the part that extends the gate rule's clock)
job       start           end                      gap        lost  note
capture   11-19 12:56:00  11-19 15:09:08     133.1 min   128.1 min  gap between passes
capture   11-19 15:09:08  11-19 18:02:52     173.7 min   168.7 min  gap between passes
capture   11-19 18:02:52  11-19 19:35:08      92.3 min    87.3 min  gap between passes
capture   11-19 19:58:15  11-19 21:52:09     113.9 min   108.9 min  gap between passes
capture   11-20 04:10:03  11-20 05:02:43      52.7 min    47.7 min  gap between passes
capture   11-20 05:02:43  11-20 05:42:35      39.9 min    34.9 min  gap between passes
capture   11-20 05:42:35  11-20 06:18:37      36.0 min    31.0 min  gap between passes
capture   11-20 06:18:37  11-20 06:29:29      10.9 min     5.9 min  gap between passes
capture   11-20 19:43:52  11-20 19:56:14      12.4 min     7.4 min  gap between passes
capture   11-21 01:23:43  11-21 01:34:51      11.1 min     6.1 min  gap between passes
capture   11-22 05:39:19  11-22 05:59:17      20.0 min    15.0 min  gap between passes
capture   11-22 06:14:54  11-22 06:24:29       9.6 min     4.6 min  gap between passes
capture   11-22 06:31:34  11-22 06:51:04      19.5 min    14.5 min  gap between passes
capture   11-20 02:36:58  11-20 02:45:03       8.1 min     3.1 min  restart excess (acadia.mcfarland's polls)
capture   11-20 19:43:50  11-20 20:01:34      17.7 min    12.7 min  restart excess (smokies.look-rock's polls)
forecast  11-19 13:55:36  11-19 15:09:31      73.9 min    68.9 min  no live run written while runs were due: runs 2026111913
forecast  11-19 16:08:09  11-19 18:32:10     144.0 min   139.0 min  no live run written while runs were due: runs 2026111915, 2026111916, 2026111917
forecast  11-19 18:54:08  11-19 19:35:26      41.3 min    36.3 min  no live run written while runs were due: runs 2026111918
forecast  11-19 19:55:38  11-19 21:40:34     104.9 min    99.9 min  no live run written while runs were due: runs 2026111919, 2026111920
forecast  11-20 04:55:13  11-20 05:18:20      23.1 min    18.1 min  no live run written while runs were due: runs 2026112004

Starts, stops and settings changes
  capture restart: stopped after 11-20 02:37:12 (the old loop's last record), started 11-20 02:39:39 (147.5 s; passes 147.5 s apart; excess 185.471 s (polls 483.080-485.471 s apart across it, longest acadia.mcfarland)): capture_version lookout-capture/0.1.0 -> lookout-capture/0.2.0; pass starts 161.1 s apart, under the 300 s interval of one loop
  capture restart: stopped after 11-20 19:43:52 (the old loop's last record), started 11-20 19:56:14 (741.4 s; passes 741.5 s apart, an outage; excess 763.623 s (polls 1058.858-1063.623 s apart across it, longest smokies.look-rock)): capture_version lookout-capture/0.2.0 -> lookout-capture/0.2.1
  forecast settings changed from 11-20 02:39:49: fields added ['PRES:cloud base'], removed -, hours [0, 1, 2] -> [0, 1, 2]
  NPS fetcher lock now: started_at 2026-11-20T19:56:14Z

Exclusions (they do not count)
  capture_log: before the window: 377 11-19 06:30:13 to 11-19 12:12:23 [archive_request 217, error 3, pass 1, rate_capped 17, stored 139] e.g. capture_log/2026-11-19.jsonl:1
  capture_log: archive event (worker: archive), not live capture: 6013 11-20 09:52:25 to 11-22 01:45:45 [archive_request 3000, error 7, queue_done 311, queue_failed 1, stored 2694] e.g. capture_log/2026-11-20.jsonl:1824
  forecast_log: before the window: 224 11-19 09:54:28 to 11-19 12:21:24 [lag 214, run 10] e.g. forecast_log/2026-11-19.jsonl:1
  forecast_log: archived forecast run (live is not true): not live-worker evidence: 1 11-19 23:13:13 to 11-19 23:13:13 [run 1] e.g. forecast_log/2026-11-19.jsonl:475
  forecast_log: duplicate lag (its run and hour were logged): 160 11-20 02:39:51 to 11-20 02:50:26 [lag 160] e.g. forecast_log/2026-11-20.jsonl:15
  forecast_log: re-extraction of a run already written (the run counts once): 12 11-20 02:39:51 to 11-20 02:50:26 [run 12] e.g. forecast_log/2026-11-20.jsonl:34; forecast_log/2026-11-20.jsonl:84; forecast_log/2026-11-20.jsonl:104
  forecast_log/lag_survey: survey lag (an S3 survey): not live-worker evidence: 4082 11-20 03:11:55 to 11-20 03:12:40 [lag 4082] e.g. forecast_log/lag_survey/2026-11-20.jsonl:1
  forecast/hrrr: live forecast file whose run was not first written inside the window: 80 e.g. forecast/hrrr/acadia.mcfarland/2026111902z.json; forecast/hrrr/acadia.mcfarland/2026111903z.json; forecast/hrrr/acadia.mcfarland/2026111904z.json
  forecast/hrrr: archived forecast file (live is not true): not live-worker evidence: 1 e.g. forecast/hrrr/grand-canyon.yavapai/2024011412z.json
  outages: capture outage: its lost time extends the 72 h clock (the gate rule and PLAN.md's figure): 13 e.g. 2026-11-19T12:56:00.665Z; 2026-11-19T15:09:08.819Z; 2026-11-19T18:02:52.550Z
  outages: capture restart excess: its lost time extends the 72 h clock (the gate rule): 2 e.g. 2026-11-20T02:36:58.471Z; 2026-11-20T19:43:50.592Z
  outages: forecast outage: its lost time extends the 72 h clock (the gate rule): 5 e.g. 2026-11-19T13:55:36.000Z; 2026-11-19T16:08:09.000Z; 2026-11-19T18:54:08.000Z
```

How to read it:
- **72 h clock:** the gate rule's line is the gate. The three indented lines are for comparison only (D-069).
- **Capture:** per camera, the polls and image GETs, the full-size frames and thumbnails kept, the downloads by storage policy (D-028: `every` at Rainier and Point Reyes, `hourly` at the six others, `legacy` before capture 0.2.0), the polls held back by the 4-a-rolling-hour cap, the errors, the longest gap between polls, the least gap between sends, and the most image GETs sent in any rolling hour.
- **Polite collection:** the least gap or span nps.gov can have seen, whatever the network did (D-069, D-082). "Unproven" means not provably 300 s (3,600 s) apart; it does not mean shown closer.
- **Forecast:** the live runs first written in the window, per viewpoint, and the lag log.
- **Outages and restart excess,** and **Exclusions:** what extends the clock, and what does not count.

## 4. Outages and exclusions

**Capture outages by day** (the table's rows; the causes from `pmset -g log`):

| Day (UTC) | Outages | Lost | Cause |
|---|---|---|---|
| 19 Nov | 4, from 12:56:00 to 21:52:09 | 493.0 min (8.22 h) | the Mac asleep on battery, which it ran on from 06:13 to 22:48 |
| 20 Nov | 4, from 04:10:03 to 06:29:29 | 119.4 min (1.99 h) | the Mac asleep on battery, 03:29–06:27 (D-051) |
| 20 Nov | 1, 19:43:52 → 19:56:14, the re-snapshot to capture 0.2.1 | 7.4 min | the Mac slept on AC once the old loop's assertion dropped (D-079) |
| 21 Nov | 1, 01:23:43 → 01:34:51 | 6.1 min (368.5 s) | the Mac asleep on battery (D-081) |
| 22 Nov | 3, from 05:39:19 to 06:51:04 | 34.1 min (2,043.8 s) | the Mac idle-slept on battery (section 5) |

They add up to 660.0 min (39,602.0 s), the report's 11.001 h.

**Restart excess:** 185.471 s at the restart to capture 0.2.0 at 02:39 on 20 Nov (Acadia's polls), and 763.623 s at the 19:44 re-snapshot on 20 Nov (the Smokies' polls), which holds that day's 7.4 min outage. That is the report's 0.264 h. The switch's restart, after the gate, added 4.797 s (section 7).

**Forecast outages:** 5, all on 19–20 Nov, 6.039 h; the runs due in them were 2026111913, 15, 16, 17, 18, 19, 20 and 2026112004. None since.

**Exclusions,** none of which counts as live-worker evidence (the table's last block):
- `capture_log`: 377 records before the window (06:30:13–12:12:23 on 19 Nov: the stopgap and the smoke pass), and the archive queue's 6,013 records (`"worker": "archive"`, from 09:52:25 on 20 Nov to 01:45:45 on 22 Nov);
- `forecast_log`: 224 records before the window; the backtest pair's archived run (line 475 of `forecast_log/2026-11-19.jsonl`); 160 duplicate lags and 12 re-extractions of 02:39–02:50 on 20 Nov (D-050);
- `forecast_log/lag_survey`: the 4,082 survey lags (D-053);
- `forecast/hrrr`: 80 live files whose run was first written before the window, and the pair's archived file.

## 5. 22 Nov: three outages on battery

| Outage (UTC) | Between passes | Lost |
|---|---|---|
| 05:39:19.364 → 05:59:17.751 | 1,198.4 s | 898.4 s |
| 06:14:54.668 → 06:24:29.365 | 574.7 s | 274.7 s |
| 06:31:34.302 → 06:51:04.989 | 1,170.7 s | 870.7 s |

They moved the earliest close from 23:12:06.983 (D-081) to 23:46:10.754 UTC, by their 2,043.8 s.

**The power log** (`pmset -g log`; its local times are UTC − 6 h):

| UTC | Event |
|---|---|
| 21 Nov 18:55:11 → 22 Nov 04:16:45 | on AC with the lid closed, in dark wake ("Clamshell Sleep"); the loops keep running (107 passes in `capture_log/`, none more than 333.3 s apart) |
| 04:16:45 | on battery, at 100 %; the assertion summary reads "No Assertions", and the Mac sleeps at once for 14 s ("Notification Wake Back to Sleep") |
| 04:16:59 | lid wake; no outage came of the 14 s |
| 04:17:00 → 05:40:23 | awake while powerd holds "Prevent sleep while display is on" (the summary mostly reads "PrevIdle DeclUser kDisp") |
| 05:40:23–05:40:29 | that assertion released, the two `caffeinate -s` lines tagged "[System: No Assertions]"; "Idle Sleep" at 83 %, then dark wakes |
| 06:05:35 | full wake (user activity), at 82 %; the display assertion holds from 06:05:36 |
| 06:16:03–06:16:09 | the display assertion released; "Idle Sleep" at 81 %, then dark wakes |
| 06:46:53 → 06:46:55 | a 2 s dark wake, then 210 s of sleep ("Sleep Service Back to Sleep") |
| 06:50:25 | keyboard wake, at 80 % |
| 06:53:27 | the lead's `caffeinate -i -w 20119` starts holding PreventUserIdleSystemSleep (pid 11415; its age when it ended, 17:04:40, dates it) |
| 09:30:58 | 48 %, still on battery, awake |
| 09:46:47 | on AC, at 47 % |
| 23:32:45 → 23:51:06 | on battery at 100 %, apart from 2 s at 23:32:53; awake |
| 23:51:06 | on AC, before the switch |
| 23:58:07 | the lead's `caffeinate -i` ends ("ClientDied") as the switch stops the supervisor |

**Why the Mac slept:** both of our sleep assertions held then were `caffeinate -s` (PreventSystemSleep): the supervisor's (pid 20135) and the capture loop's (pid 70531). That assertion counts only on AC power, so on battery only powerd's display assertion kept the Mac awake, and it idle-slept each time that ended. The supervisor's opt-in keep-awake, which holds on battery, was not set: its log's first line reads "awake until never (opt-in)". After the lead's `caffeinate -i` the Mac stayed awake on battery until it went back on AC at 09:46:47, and no outage followed 06:51:04.

**The passes that ended the gaps** (`capture_log/2026-11-22.jsonl`; a pass's start is its `t` less its `duration_s`): the first two (05:58:59.9 → 05:59:17.751 and 06:24:21.5 → 06:24:29.365) ran in dark wakes. The third (06:46:53.8 → 06:51:04.989, 251.23 s) began in the 2 s dark wake of 06:46:53, spanned the 210 s sleep after it and ended after the keyboard wake at 06:50:25.

**The errors in the gaps:** a JSON GET (Smokies) reset at 06:46:53, in the 2 s dark wake (06:46:53–06:46:55). An image GET (Acadia) was sent at 06:46:58, as the Mac went back to sleep for 210 s, and was abandoned at its 60 s deadline at 06:50:53 (235.5 s after it was sent), 28 s after the keyboard (full) wake at 06:50:25. Each was followed by a 300 s back-off.

## 6. Polite collection (PRD §15.3)

| What | Span (UTC) | Result | Source |
|---|---|---|---|
| Polls: least gap at nps.gov | the window | 299.001–300.002 s by camera; 10 of 6,606 pairs unproven, all from 19–20 Nov, before capture 0.2.1 (D-065, D-079) | the table |
| Polls: least gap at nps.gov | 22 Nov 00:00 → 23 Nov 00:13:12 (capture 0.2.1, then 0.3.0's first three passes) | 300.002–300.003 s by camera, over 269–271 pairs a camera; none under 300 s | the docs agent, on `capture_log/` |
| 5 consecutive image GETs: least span | the window | 3,612.816 s (Rainier, 02:21–03:21 UTC on 20 Nov, across the 02:39 restart); 0 of 2,188 windows unproven | the table; the JSON's `image_gets_at_nps.shortest` |
| 5 consecutive image GETs: least span | 22 Nov 00:00 → 23 Nov 00:13:12 | 3,706.3 s, over 84–88 windows a camera; none under 3,600 s | the docs agent |
| Image GETs in any rolling hour | the window | at most 4 at every camera | the table |
| Archive requests: least end-to-start gap | 22 Nov | 1,000 requests, logged from 00:04:01.537 to 01:45:45.336, all answered 200 (65 day searches, 935 `hires` GETs): least 5.0507 s, none under 5.05 s | the docs agent |
| Across the switch | 22 Nov 23:50 → 23 Nov 00:13 | least poll gap 300.006 s (Big Bend; 300.070 s send to send); the 5-GET windows that span the restart at least 3,720.2 s (Acadia); the new loop's 90 archive requests at least 5.0523 s apart | the docs agent |
| Back-off | 22 Nov | 10 errors in 2,856 live requests, each followed by a 300 s back-off: 5 image 404s, 3 answers of 504 at 05:07:56–05:08:46, the reset and the deadline (section 5). No 429 or 503, so no www.nps.gov hold. | `capture_log/`; `state/capture/hosts/` does not exist |
| Held-out dates | 23 Nov 00:15:49 | no queue entry dated on or after 1 Jun 2026; the latest is 2026-05-30 | `snow-globe-capture archive queue list`, offline |

**Bases,** the data report's: a poll pair's gap is the next poll's earliest receipt minus the previous poll's latest. The earliest receipt is `sent_at`, else `t − elapsed_ms − 0.05 ms`. The latest is the later of `answer_bound_at` and `answered_at`, else `answered_at`, else `t + 1 ms`. A 5-GET span is the 5th GET's earliest receipt minus the 1st's latest. An archive request ends at its record's `t` and starts at `t − elapsed_ms`.

## 7. The rename's live switch

**The timeline** (`switch-20261122T235334Z.log`, unless named):

| UTC | Step |
|---|---|
| 22 Nov 23:46:47 | The first rehearsal (`--dry-run`) is refused at its step 2, with nothing stopped: `uv sync --locked --no-dev --offline` cannot find botocore 1.44.6 in uv's cache. This is the lead's record; this try's log was not kept. |
| 23:48:39 | The lead's refill of uv's cache: its `archive-v0`, `builds-v0` and `wheels-v6` folders are born then (`stat`). |
| 23:48:49–23:48:56 | The rehearsal passes, on battery (`switch-dry-run-20261122T234851Z.log`). |
| 23:53:34 | The switch starts, on AC. Preflight: the three live scripts unchanged since 01:40 UTC on 21 Nov (SHA-256); source `aee6f92`; capture loop 70525, stand-in 36474, supervisor 20119; `current` → `3cd2ca7`. |
| 23:53:35 | Gate check: met (72.123 h covered of 83.487 h at 23:53:34.551Z). |
| 23:53:40 | The rehearsal passes again; `caffeinate -is -w 20907` (pid 21036) holds from here to 00:03:13. |
| 23:58:06.815 | The 0.2.1 loop's last pass ends (it started at 23:57:56). |
| 23:58:07–23:58:08 | The supervisor 20119 is stopped first. Its own log, at 23:58:07: "SIGTERM to capture none and forecast none", then "stopped". |
| 23:58:08–23:58:09 | SIGTERM to the capture loop 70525; it exits. |
| 23:58:09 | `~/lookout-data` moves to `~/snow-globe-data`, and the symlink `~/lookout-data` → `~/snow-globe-data` is made. |
| 23:58:09–23:58:14 | Snapshot `aee6f92` built with its own venvs, offline; its `status` reads 8 cameras with storage at `~/snow-globe-data`; the forecast handler imports; the three scripts are rewritten (old ones kept as `*.lookout.bak`); `current` → `aee6f92`. |
| 23 Nov 00:02:06 | After a 232 s wait, the new capture loop starts (pid 22115). |
| 00:02:07 | It holds the NPS fetcher lock and writes `loop_started` (00:02:07.088, `capture_log/2026-11-23.jsonl`). |
| 00:03:10.250 | Its first pass ends. |
| 00:03:10 | SIGTERM to the old stand-in's bash (36474); the new stand-in starts (pid 22288). |
| 00:03:13 | The stand-in's first tick extracts run 2026112223 (3 hours, 8 files, 25,764,369 bytes, 1.8 s; 2026112300 not yet on S3). Done; the switch releases its `caffeinate` and waits on the two loops, its children. |

**PLAN.md's step 6 checks** (the docs agent, read-only, 00:10–00:28 UTC on 23 Nov):

| Check | Result |
|---|---|
| All 8 cameras polled | Yes. The first 0.3.0 pass (`started_at` 00:02:07.110, ended 00:03:10.250) sent 7 JSON GETs, Rainier's HEAD and 4 image GETs (each right after its camera's poll), all answered 200. Outcomes: 4 stored, 3 unchanged, 1 rate capped (Rainier). |
| The `snow-globe-capture/` version prefix in the log | Yes: `snow-globe-capture/0.3.0` on every record of the new loop. |
| A `loop_started` record listing the 8 cameras | Yes: pid 22115, `interval_s` 300, `jitter_s` 30, the 8 viewpoints. |
| A `started_at` on each pass | Yes, on all 4 passes to 00:18:31. |
| One fetcher holding the lock | Yes: `state/locks/nps-fetcher.lock` names pid 22115, `started_at` 2026-11-23T00:02:07Z, running `snow-globe-capture loop --interval 300 --archive-queue …/state/archive/queue.json`; `ps` shows one capture loop and its `caffeinate -s` (pid 22127). |
| The new User-Agent | In the code, not on the wire: request records do not log it. The snapshot's default is `SnowGlobe/0.1 (webcam research capture; fetch-on-change)` (`workers/common/src/snow_globe_common/http.py`, line 60), and the rewritten scripts unset every `LOOKOUT_*` and `SNOW_GLOBE_*` variable before they set the data dir and the manifests. |
| The stand-in's first run file, `kind` `snow-globe.forecast-run` | Yes, at all 8 viewpoints: `live` true, hours 0–2, 8 fields, `snow_globe_forecast` 0.3.0. All 8 pass `validate_document` of `workers/forecast` at `aee6f92`. The run fell due at 00:02:47, when its f01 reached S3, and was written at 00:03:13. |
| The data report still reads the old records | Yes: on `~/snow-globe-data` (section 2) the close and the outage totals are unchanged (met at 23:46:10.754Z; capture outages 11.001 h, forecast outages 6.039 h), while the switch's 4.797 s of excess, after the close, raised the restart excess to 0.265 h and the lost total to 11.37 h; the old versions' passes (69, 174, 590) are counted beside 0.3.0's. |
| D-083 in the snapshot (step 2) | Yes: line 115 of its `workers/capture/src/snow_globe_capture/cli.py` passes `auto_resume_max=AUTO_RESUME_MAX`, and the queue heartbeat from pid 22115 records `auto_resume_max` 3. |
| D-085's citation (step 2) | Yes: `60e05ba`, an ancestor of the snapshot's `aee6f92` (the snapshot itself holds only `workers/` and `viewpoints/`), holds eval's report (D-082) and held-out split version 2, whose `decisions` list cites D-085 (`git show 60e05ba:eval/src/snow_globe_eval/heldout_periods_v2.json`; SHA-256 `82574ceb…`, 6,216 bytes). |

**The restart's cost,** from `data-report-20261123T000629Z-postswitch.txt`, verbatim:

```text
  capture restart: stopped after 11-22 23:58:06 (the last record before loop_started), started 11-23 00:02:07 (240.3 s; passes 303.4 s apart; excess 4.797 s (polls 300.070-304.797 s apart across it, longest yosemite.turtleback)): capture_version lookout-capture/0.2.1 -> snow-globe-capture/0.3.0; pass starts 250.6 s apart, under the 300 s interval of one loop; loop_started at 2026-11-23T00:02:07.088Z (pid 22115)
```

So the switch cost 4.797 s of excess (0.1 min), after the gate had closed. The re-snapshot of 20 Nov had cost 763.6 s (D-079). The same report's clock line, verbatim:

```text
72 h clock, gate rule (the lead's of 20 Nov, settling D-061: the union of both jobs' outages and each capture restart's excess): 72.34 h covered of 83.70 h elapsed; 11.37 h lost (capture outages 11.001 h, restart excess 0.265 h, forecast outages 6.039 h, an overlap counted once); met at 2026-11-22T23:46:10.754Z
```

**After the switch** (`ps`, the supervisor's files and the queue listing):
- The supervisor was not restarted by the switch, which never does (D-076). At 00:10:34 and 00:27:52 UTC, `supervisor.pid` did not exist and its log ended with the 23:58:07 stop.
- The owner started it again at 00:36:33 UTC (PRD_PROGRESS.md, owner action 1). At 00:40 UTC: pid 26187, PPID 1, TTY `??`, STAT `SNs`; its log reads "adopt capture=22115 forecast=22288; awake until 2026-11-23T05:00:00Z", then "caffeinate -ims for 15807 s (pid 26257)", which `pmset -g assertions` shows holding PreventUserIdleSystemSleep until 05:00:00 UTC. That holds on battery too, but only with the lid open: on battery a closed lid sleeps the Mac whatever the assertions (D-089).
- The new loops are children of the switch's process (pid 20907), and through its shell of the lead's Claude Code session (pid 72719); the supervisor adopted them without detaching them. Until 00:36:33 the only keep-awake of ours was the capture loop's `caffeinate -s`, on AC only.
- The queue at 00:15:49: 808 entries, 318 done, 489 pending, 1 active, 0 failed; budget `archive-queue-2026-11-23` at 120 of 1,000.

## 8. What this file does not show

- **The User-Agent on the wire.** Request records do not log it; the check is in the code and the scripts (section 7).
- **The first rehearsal's own log.** It was not kept. Its failure is the lead's record, and the birth times of uv's cache folders agree with it.
- **What emptied uv's cache,** some time between the lead's `make data-report` at 04:17:33 UTC on 22 Nov, which ran `uv`, and 06:51:47 UTC, when the lead's next `uv run` made the folder again (its birth time is on disk). The lead's free-space readings (48 GiB at 04:21:24, 92 GiB at 06:50:46) and its reading of the cache as 16K (`du -sh`, at 23:47:15, after the failed rehearsal) are in its session log only.
- **CI on `aee6f92`.** `git reflog` shows it pushed to `origin` `main` at 11:24:01 UTC on 21 Nov; its CI run was not read.
