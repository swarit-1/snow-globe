# NPS archive check (M0.5), 19 Nov 2026

**Question** (PRD §13.2, §13.9): for each NPS air-quality camera, can evaluation use **design H**? Design H pairs archived frames with archived HRRRv4 runs, inside one camera epoch. **Design P** is the sealed prospective window. This report feeds D-018 and D-019.

**Tool:** `lookout-capture archive-check` ([`workers/capture/src/lookout_capture/archive_check.py`](../../workers/capture/src/lookout_capture/archive_check.py)). It ran on 19 Nov 2026 between 08:46 and 10:41 UTC.

**Data:**
- The machine-readable result was written to `$LOOKOUT_DATA_DIR/archive/archive_check_2026-11-19.json`. It holds fields derived from frames past the held-out guard (below), so it is now in the quarantine folder (D-022). Its `request_totals_from_runs` misses one interrupted run (see "Budget used").
- Contact sheets were written to `$LOOKOUT_DATA_DIR/archive/archive_check/2026-11-19/`. **Every sheet includes tiles past the guard,** so they are now in the quarantine folder too.
- Downloaded frames are under `archive/<viewpoint_id>/`, each with a sidecar: 85 from before the guard and 22 past it.

> **This check broke D-019's held-out guard.** D-019 allows archived frames, full size or thumbnail, only from before 1 Jun 2026, and summer 2026 is the design-H held-out season. The run viewed frames from 15 Jul 2026 (inside that season) and 17 Nov 2026 at all seven cameras. The record and its consequences are in [Guard breach](#guard-breach). Every finding below is stated from frames before the guard plus metadata, which D-019 allows for any date. Observations that rest on frames past the guard appear only in the breach record.

## Answer in brief

**1. Every camera changed in 2023–2025, and design H can only use the current epoch.**
- All seven ARD cameras switched from a 3:2 frame to a 16:9 frame. The thumbnail went from 99×66 to 100×56. The archive "hires" went from 999×666 to 999×562.
- The switch happened at a different time for each camera, between Jan 2023 and Jan 2025 (table below). It is bracketed by thumbnails from before the guard.
- HRRRv4 started on 2 Dec 2020, so the HRRRv4 years before the switch belong to a different camera.
- **Design H is possible on the current 16:9 epoch at Point Reyes, Grand Canyon, Yosemite, Acadia, the Smokies and Big Bend.** At Point Reyes that epoch covers the summer fog seasons of 2024 and 2025 for development. Summer 2026 is D-019's held-out season, now affected by the breach.

**2. Grand Teton is not one view.**
- Before the guard its framing changed at least three times across the samples, and at least once inside the 16:9 epoch: 15 Jan 2025 differs from 15 Jul 2025 and 15 Jan 2026.
- So it is **not** the deep-archive P1 candidate D-019 mentioned.

**3. The archive keeps ~1000 px frames, not 4K.**
- Every `hires` asset stored is 999 px wide:
  - before the guard, 999×666 in the 3:2 epoch at all seven cameras;
  - 999×562 in the 16:9 epoch at Point Reyes on 15 Jan 2026.
- 16:9 hires at the other six cameras were seen only on 17 Nov 2026, past the guard. The 100×56 thumbnails before the guard give the same 16:9 shape.
- Live frames are 3840×2160 and stored at 1920×1080.
- A hindcast extractor would therefore see about half the live stored resolution. Its thresholds (τ, the partial band) would have to be set at archive resolution, or live frames downscaled to 999 px.

**4. Archive time stamps are right, summer included.** This comes from metadata only.
- On all 90 sampled days, NPS's own `SolarElevationAngle` matches a NOAA solar-position calculation at our parsed UTC time.
  - The mean absolute difference per day is 0.015–0.065°.
  - The best-fitting clock offset is 0 min on every day.
  - A one-hour error would show as up to ~12° at mid-morning.
- In summer, `ImageCreateDateTime` stays on local **standard** time, while `LocalTimeString` gives civil time labelled "Daylight", one hour ahead.
  - Example: `07/15/2021 01:30:00 PM` against `7/15/2021 2:30 PM Pacific Daylight Time`.
  - `lookout_common.timeutil.parse_nps_archive_time` used to reject every summer frame. It now accepts exactly this case, with a test. This closes its TODO(M0.5) on the summer convention, although the `workers/common` README still lists it as open.

**5. The archive and the live feed both update every 15 minutes.**
- **Archive** (metadata): 15-minute cadence, day and night, on every camera and year sampled.
  - The median gap is 15 min on every sampled day.
  - Gaps of 30 min or more occur on most days.
  - Some days are partial: 22 frames at Big Bend on 12 Jul 2024 and at Yosemite on 12 Jan 2021, and 35 at Point Reyes on 16 Jan 2023.
  - Days available per year in 2024–2025: 320–366, except Big Bend at 332–335.
- **Live feed** (one pass, 10:56 UTC):
  - The JSON `imagedate` read `…05:45 AM EST` and `02:45 AM PST` at 10:56 UTC, so it also moves every 15 minutes.
  - EXIF time equals `imagedate` to the minute on all 7 ARD cameras.
  - The image's `Last-Modified` follows by 6.50–6.88 min.
  - The JSON's own `Last-Modified` comes 8–25 s after the image's.

**6. The Rainier park camera has no archive.** The park's webcam page has no archive link, no time-lapse and no mention of past images (details below).

## Guard breach

**What D-019 says** (committed before the check, "fixed before any archived frame is viewed"):
- Held out at every ARD camera: summer 2026, 1 Jun – 31 Aug 2026, local dates.
- Guard for the epoch check: metadata (`GetAvailableDays`, `Search/Execute`) may be read for any date. Archived frames, full size or thumbnail, are viewed only from before 1 Jun 2026.
- Changing the season takes a new entry, made before any frame from the new season is viewed.

**What happened** on 19 Nov 2026, 09:44:34 to 10:40:52 UTC:
- The check sampled 15 Jul 2026 and 17 Nov 2026 at every camera. It had no date guard; one was added afterwards (below).
- It downloaded **22 frames past the guard**:
  - 2026-07-15, inside the held-out season: the midday `thumbmedium` at all 7 cameras, plus the Point Reyes `hires` (`20260715T201500Z_c7dc841d.jpg`, 999×562, the fog camera of K2);
  - 2026-11-17: the midday `thumbmedium` and `hires` at all 7 cameras.
- From them it built the 7 contact sheets and computed dHash distances. **All dHash figures in the first version of this report were distances to the 17 Nov 2026 thumbnail.**
- They were looked at by eye, and the first version of this report drew framing conclusions from them.
- Separately, 2025-07-15 midday thumbnails were viewed at every camera, as development data. That was allowed. It does mean summer 2025 can no longer become a held-out season under D-019's rule. The first version of this report suggested exactly that move ("held-out on summer 2025"); it is withdrawn.

**Observations that rest on frames past the guard.** They are listed here only so that the lead can judge what leaked. They must not inform any design-H choice, exclusion rule or pre-registration.
- Grand Teton: "no conifer" on 2026-07-15 and 2026-11-17, read as a re-aim between 15 Jan and 15 Jul 2026.
- Big Bend: 2026-07-15 "looks tighter on the mesa".
- Point Reyes, Grand Canyon, Yosemite, Acadia, the Smokies: "same framing" on 2026-07-15 and 2026-11-17.
- dHash change-point candidates at Point Reyes and Big Bend, splitting 2026-01-15 from 2026-07-15 (margins 8.2 and 8.3 bits).
- 999×562 hires sizes on 2026-07-15 (Point Reyes) and on 2026-11-17 (all seven).

**Quarantined** by the lead on 19 Nov (D-022). These were moved, not deleted, to `$LOOKOUT_DATA_DIR/quarantine/held-out-breach-2026-11-19/`, whose README gives the undo steps:
- the 22 frames and their 22 sidecars, from `archive/<viewpoint_id>/2026-07-15/` and `archive/<viewpoint_id>/2026-11-17/`;
- the 7 contact sheets;
- `archive_check_2026-11-19.json`, which holds the derived fields.

The search caches for those days are metadata and are allowed.

**Fixed in code** (`workers/capture`, with tests):
- `heldout.HELD_OUT_FROM = 2026-06-01`. Backfill, the archive check and the CLI refuse any archived frame on or after it.
- The guard reads the latest of three local dates, so a summer frame stamped 31 May on standard time is refused too:
  - `ImageCreateDateTime`;
  - `LocalTimeString`;
  - the civil date of the UTC time.
- A frame whose date cannot be read is refused.
- `sample_dates` lists July 2026 as "held out" and has no 17 Nov extra.
- The check's `_download` will not read a post-guard sidecar even when the file exists.
- Only `--held-out-override "<reason>"` releases such frames. The reason is logged with every frame it releases and written into the sidecar. It is meant for scoring only.

**For the lead and the owner:**
1. Record the breach in D-019.
2. Re-decide the design-H held-out season under D-019's rule.

   One possibility is to keep summer 2026 and exclude 15 Jul 2026 from scoring at every camera. Moving to another summer is not available: every summer from 2021 to 2025 had a mid-July midday thumbnail viewed as development data.

3. With the owner's OK, move the 22 frames, their sidecars and the 7 contact sheets to a quarantine folder outside `archive/`, without deleting them.

**Done on 19 Nov (D-022 and D-023):**
- The breach is recorded.
- The season stays summer 2026, with 15 Jul 2026 excluded at every camera.
- The files are quarantined; the lead made the move, which can be undone.

## Method

**Politeness:**
- Requests went one at a time, at least 5 s from the end of one to the start of the next, through `MinIntervalLimiter("nps-archive", 5 s).hold()` from `lookout_common`, shared by every process. The minimum gap between request starts in the log is 5.1 s.
- Errors deferred the next request: 30 s, doubling.
- A request budget of **220** was persisted in `state/archive/budget-archive-check-2026-11-19.json`. Every request was counted before it was sent.
- The User-Agent was `Lookout/0.1 (webcam research capture; fetch-on-change)`, with no cookies.
- **Not polite enough, on one count:** the Rainier stopgap (D-021) polled nps.gov throughout the run. It logged 6 events between 08:50 and 10:02 UTC. That breaks CLAUDE.md's "never run two NPS fetchers at once". The capture CLI now refuses to start while the stopgap runs (`fetcher_guard.py`).

**API:** the routes are in CLAUDE.md.
- Codes were sent as the archive spells them: `ACAD` and `BIBE` in upper case, the rest in lower case.
- Each search is for one local day, `PageSize` 300, oldest first.

**Steps, per viewpoint:**
1. `GetAvailableDays` once. Each month's value is a 31-bit mask, where bit *d*−1 set means day *d* has frames. That gives `MinDay`, `MaxDay` and the days per year.
2. Sampled dates: the 15th of January and July for 2021–2026, or the nearest available day of that month.
   - The 19 Nov run also took 2026-11-17: 13 dates per camera, two of them past the guard.
   - The code now takes at most 11 dates, all before 1 Jun 2026, and lists July 2026 as held out.
3. For each date, one `Search`. Searches of days at least two days old are cached under `cache/nps-archive/`. From each search:
   - frame count;
   - gaps between frames;
   - `TimeOfDay` counts;
   - the `SolarElevationAngle` range;
   - a time-stamp test. For frames with Sun ≥ 5°, the NOAA solar elevation (`lookout_capture.sun`) at the parsed UTC time is compared with NPS's angle, and the constant clock offset (−120..+120 min) that fits best is found.
4. **Midday frame** (highest NPS Sun angle) of every sampled date: download its `thumbmedium`.
5. **Hires:** the newest sampled midday, plus one per older epoch (the last sample before each thumbnail-size change). On 19 Nov "the newest" was 17 Nov 2026, past the guard.
6. **dHash64** distance of every midday thumbnail to the newest one, and a change-point search over the series. The figures below were recomputed with the newest thumbnail before the guard, 15 Jan 2026, as the reference.
7. A **contact sheet** per camera, inspected by eye.

**Budget used:**
- **217 requests were sent**, counted from the `archive_request` events in `capture_log/2026-11-19.jsonl`:
  - 7 `GetAvailableDays`;
  - 99 `Search`;
  - 92 `thumbmedium`;
  - 18 `hires`, of which 17 were stored and one timed out;
  - 1 page (the Rainier webcam page).
- **5,910,413 bytes** in total, of which the searches were 4.6 MB.
- The budget counter reads 218: one request was counted and then interrupted by a deliberate stop before it was sent.
- The summary JSON's `request_totals_from_runs` reads 203 requests, 5,315,257 bytes and 9 errors. It sums only runs that finished. The 14 missing requests are the Grand Canyon first attempt between 08:49:28 and 08:51 UTC: 1 `GetAvailableDays` and 13 searches, 595,156 bytes and 1 error. It stopped before merging (see "First attempt").

**Errors (10 requests):**
- 7 TLS connect timeouts.
- 1 server disconnect.
- 2 HTTP 500s on the paging probe (below).

Every sample lost to a timeout was retried within the budget, so no sampled date is missing for those reasons. Grand Teton's January 2023 sample is missing because no day was available that month.

**First attempt:** the run started at 08:46 and was stopped at 08:51, when summer time stamps failed to parse (finding 4). Its cached searches and stored files were reused, so no request was repeated except one search.

**Paging is not working:**
- Page 2 of a search answered HTTP 500 twice:
  - once with `POST /api/Search/Execute/<SearchID>`;
  - once with `Page=2` and the `SearchID` in the body.
- A day holds at most 96 frames, below the page size of 300, so backfill does not need paging.
- The client stops at the failed page and never caches an incomplete search.

## Per camera

**Days per year** come from `GetAvailableDays` (fetched 19 Nov 2026). "Since HRRRv4" counts available days from 2 Dec 2020 to `MaxDay`, out of 2,179 calendar days.

| Viewpoint | Code | Archive from | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 (to 19 Nov) | Since HRRRv4 |
|---|---|---|---|---|---|---|---|---|---|---|
| `point-reyes.beach` | `pore` | 2004-01-06 | 290 | 322 | 303 | 316 | 360 | 357 | 310 | 1,983 |
| `grand-canyon.yavapai` | `grca` | 2001-11-08 | 313 | 339 | 327 | 301 | 320 | 359 | 323 | 1,997 |
| `grand-teton.science-center` | `grte` | 2011-08-30 | 309 | 364 | 327 | 316 | 358 | 359 | 323 | 2,075 |
| `yosemite.turtleback` | `yose` | 2005-10-18 | 359 | 232 | 260 | 337 | 366 | 350 | 323 | 1,898 |
| `acadia.mcfarland` | `ACAD` | 2006-08-08 | 355 | 336 | 340 | 353 | 366 | 359 | 323 | 2,106 |
| `smokies.look-rock` | `grsm` | 1998-04-01 | 362 | 365 | 362 | 362 | 366 | 358 | 323 | 2,165 |
| `big-bend.panther-junction` | `BIBE` | 2004-10-05 | 358 | 359 | 310 | 356 | 335 | 332 | 323 | 2,045 |

**Cadence, epochs and resolution:**
- Frames per sampled day (metadata) are listed for the 13 dates in the order Jan 2021, Jul 2021, …, Jul 2026, 17 Nov 2026. A dash means no day was available that month.
- The epoch switch is bracketed by the last 3:2 sample and the first 16:9 sample, all before the guard.
- dHash is the mean distance in bits (min–max, number of thumbnails) to the 15 Jan 2026 midday thumbnail, out of 64. It is computed from the `dhash64` in the sidecars of thumbnails before the guard only.

| Viewpoint | Frames per sampled day | Median gap | 3:2 → 16:9 switch between | Hires, 3:2 epoch | dHash, 3:2 epoch | dHash, 16:9 epoch |
|---|---|---|---|---|---|---|
| `point-reyes.beach` | 92, 82, 56, 92, 35, 74, 92, 58, 90, 96, 94, 87, 88 | 15 min | 2023-01-16 and 2023-07-15 | 999×666 (16:9: 999×562 on 2026-01-15) | 31.4 (25–38, n=5) | 24.2 (17–30, n=5) |
| `grand-canyon.yavapai` | 84, 85, 37, 85, 88, 90, 63, 62, 96, 96, 96, 96, 88 | 15 min | 2024-01-15 and 2024-07-15 | 999×666 | 28.4 (19–36, n=7) | 27.0 (21–37, n=3) |
| `grand-teton.science-center` | 92, 84, 92, 89, –, 43, 92, 62, 96, 95, 96, 96, 90 | 15 min | 2024-07-15 and 2025-01-15 | 999×666 | 31.1 (23–42, n=7) | 27.0 (24–30, n=2) |
| `yosemite.turtleback` | 22, 84, 37, 52, 51, 64, 96, 58, 96, 94, 96, 96, 87 | 15 min | 2023-07-15 and 2024-01-15 | 999×666 | 26.2 (16–43, n=6) | 15.2 (9–25, n=4) |
| `acadia.mcfarland` | 92, 84, 92, 92, 92, 92, 92, 70, 96, 96, 96, 96, 95 | 15 min | 2023-07-15 and 2024-01-15 | 999×666 | 24.7 (15–34, n=6) | 23.0 (14–31, n=4) |
| `smokies.look-rock` | 86, 82, 92, 89, 89, 80, 90, 68, 94, 93, 92, 93, 95 | 15 min | 2024-01-15 and 2024-07-15 | 999×666 | 29.9 (23–38, n=7) | 30.7 (30–32, n=3) |
| `big-bend.panther-junction` | 93, 87, 88, 60, 92, 90, 67, 22, 96, 96, 96, 96, 92 | 15 min | 2023-07-15 and 2024-01-15 | 999×666 | 17.7 (9–25, n=6) | 14.5 (4–21, n=4) |

Where the 15th had no frames, the nearest available day of the month was sampled:
- Point Reyes 2022-01-29 and 2023-01-16;
- Yosemite 2021-01-12 and 2022-01-18;
- Grand Teton 2023-07-13;
- Big Bend 2022-07-06 and 2024-07-12.

Every sample's date is in the JSON.

**Time stamps:**
- Zone names seen: "… Standard Time" in January and "… Daylight Time" in July, except Grand Canyon, which reads "US Mountain Standard Time" all year.
- Mean |NPS − NOAA| per day: 0.015–0.065°. The best offset is 0 min on all 90 sampled days.

**What dHash can and cannot tell here:**
- On 100 px thumbnails, midday frames on different days differ by up to 43 bits (typically 15–35) whenever the weather or the season differs: fog, snow, cloud, foliage.
- So the mean distance does not separate the two epochs clearly at any camera. Yosemite and Point Reyes come closest.
- On the thumbnails before the guard, the change-point search raises **no candidate at any camera**. The best splits have margins of 1.4–7.0 bits, against a threshold of 8.
- **The epochs above come from the thumbnail size change and from looking at the contact sheets, not from dHash.**
- Small pans or zooms of a few per cent are below what a 100×56 thumbnail can show. The PRD §8.6 "Same view" registration on full frames decides those.

## View changes by eye (contact sheets)

These are judgements on upscaled 100×56 thumbnails, made on 19 Nov. Only the parts about dates before the guard are kept here. The sheets themselves include tiles past the guard and should not be opened again before quarantine.

| Viewpoint | 3:2 epoch | 16:9 epoch, before 1 Jun 2026 |
|---|---|---|
| Point Reyes | 2022-01-29 and 2023-01-16 are framed differently: the beach sweeps from mid-left in 2023, from the centre in 2022. Three of the five samples are fog-white, so the 3:2 view can't be confirmed. | Same framing in 2024-01, 2024-07, 2025-01, 2025-07 and 2026-01. 2023-07-15 is fog. |
| Grand Canyon | Same framing 2021-01 → 2024-01 by eye: white rock wedge bottom left, horizon at ~35 %. **Corrected 20 Nov (D-042):** the full 999×666 hires of 2021-01-15 and 2024-01-15 differ by a re-aim of +48.9 to +52.0 px across and −29.1 to −30.5 px up (about 5 %), measured by the backtest pair's registration, too small for the thumbnails to show. | Same framing in 2024-07, 2025-07 and 2026-01. **2025-01-15 lacks the bottom-left wedge**: possibly a temporary re-aim. Check it on the full frame. |
| Grand Teton | **2021–2022:** a large conifer on the right edge. **2023-07-13 to 2024-07-15:** no conifer; the range sits right of centre. | **2025-01-15:** conifer centre-right. **2025-07 and 2026-01:** conifer at the right edge. That makes at least two views in the 16:9 epoch. |
| Yosemite | Similar ridge framing 2021–2023 (snow and haze vary). | Same framing 2024-01 → 2026-01. The tightest dHash cluster: 9–25 bits. |
| Acadia | Similar 2021-01 → 2023-07 (a white building at left when leafless). | Same framing 2024-01 → 2026-01. |
| Smokies | Similar ridge framing 2021-01 → 2024-01. | Same framing 2024-07 → 2026-01. |
| Big Bend | Very stable 2021-01 → 2023-07: mesa at ~75 % across, horizon mid-frame. | 2025-01 and 2026-01 match. **2024-01-15 is framed lower**: a transitional setup is possible. Check it on the full frame. |

## Recommendation per viewpoint

"Usable HRRRv4 span" runs from the first 16:9 sample onward. The exact switch date is still to be found (TODO below), so the start may move up to six months earlier. Held-out at every camera is summer 2026 (D-019), pending the lead's decision on the breach.

| Viewpoint | Design H? | Usable HRRRv4-era span (archive at 999×562) | Notes |
|---|---|---|---|
| **Point Reyes** (MSV) | **Yes, for K2 (fog)** | Jul 2023 → now. Development: summers 2024 and 2025, winters 2023/24 to 2025/26. Held out: summer 2026. | Fits PRD §13.4, "K2 on a summer hindcast season". The breach touched its held-out season: one thumbnail and the hires of 15 Jul 2026. Exclude the 2021–2022 3:2 epoch, which is a different camera. |
| Rainier (MSV, P1) | **No: design P only** | None | No archive for the park camera (below). D-019 stands. |
| **Grand Canyon** | **Yes** | Jul 2024 → now (≈ 2.3 years). The 3:2 epoch, Dec 2020 → Jan 2024 (3+ years at 999×666), is a second usable span, but not for the summer-2026 held-out season, and (corrected 20 Nov, D-042) only after a full-frame registration across dates: it holds at least two aims. | Check 2025-01-15 on full frames before relying on winter 2024/25. |
| **Grand Teton** | **Not without finer epoch mapping** | At least two views in the 16:9 epoch: 2025-01-15 differs from 2025-07 and 2026-01. | **Drop it as the deep-archive P1 candidate in D-019.** Grand Canyon or the Smokies would serve that role better. |
| **Yosemite** | **Yes** | Jan 2024 → now | The most stable 16:9 epoch by dHash. 2021–2022 had few days (232 and 260). |
| **Acadia** | **Yes** | Jan 2024 → now | Coastal fog on the Atlantic side. 2024–2025: 366 and 359 days. |
| **Smokies** | **Yes** | Jul 2024 → now | The deepest coverage: 358–366 days a year. |
| **Big Bend** | **Yes, with a check** | Jan 2024 (or Jul 2024) → now | Check 2024-01 for a framing change. 2024–2025 have fewer days (335 and 332). |

**For every design-H use:**
- Fit τ, the clear-day references and the haze profiles on 999 px archive frames from before 1 Jun 2026 only.
- Score live frames downscaled to the same width, or report the resolution gap as a limitation.
- Pair archive times through `parse_nps_archive_time` (UTC verified above).
- Camera changes inside the held-out season show up only at scoring. The pre-registration needs an exclusion rule for them (D-019).

## Rainier park camera

**What was checked:** one GET of `https://www.nps.gov/mora/learn/photosmultimedia/webcams.htm` (09:43:56 UTC, HTTP 200, 66,378 bytes, 108 links).

**What the page offers:**
- No link or sentence about an archive, past images, a time-lapse, a loop or history.
- No `AirWebCams` link.
- It embeds the current images only:
  - `webcams-mora/mountain.jpg` (our camera);
  - `longmire.jpg`, `east.jpg`, `west.jpg`, `gh.jpg`, `tatoosh.jpg`;
  - the ARD `moralarge.jpg`;
  - two `inactive_webcam.png` placeholders.

**Conclusion:**
- D-018's finding holds: the summit camera has no NPS archive.
- The `mora` archive code belongs to the 512×288 ARD camera looking WSW.
- Rainier's frames exist only from our own capture, which began at 06:30 UTC on 19 Nov with the stopgap.
- Asking the park whether it keeps past `mountain.jpg` frames is worth a line in the NPS email: a hindcast at Rainier would need them.

## Limits and to-dos

- **TODO(M0.5):** date each 3:2 → 16:9 switch exactly. A binary search on daily midday thumbnails takes about 2 requests a step and about 8 steps for a six-month window. Then date the Grand Teton re-aims the same way. Every switch window lies before 1 Jun 2026, so the guard allows this. The search must stay before the guard, which the code now enforces.
- **TODO(M2):** confirm "same view" inside each chosen development span by registering full frames. This covers Grand Canyon 2025-01-15 and Big Bend 2024-01-15.
- **The sample is thin:** 2 dates a year cannot see a temporary re-aim of weeks. The registration gate catches those at scoring time.
- **Sun-angle check, residual and scope:**
  - The residual (≤ 0.065° mean) is consistent with sub-minute agreement.
  - It shows that NPS's time stamps and NPS's own Sun angles agree with each other and with the ephemeris.
  - It does not prove the camera clock itself; that check would need the archive frames' EXIF.
  - None of the 17 archive hires stored (9 from before the guard) carries an EXIF time, so that check is not possible from the archive.
