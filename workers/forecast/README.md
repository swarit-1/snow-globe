# snow-globe-forecast

Renamed from `lookout-forecast` on 21 Nov 2026 (D-080). Forecast 0.3.0 is 0.2.0 under the Snow Globe names: it writes `kind: snow-globe.forecast-run` and `software.snow_globe_forecast`, reads `SNOW_GLOBE_FORECAST_*` (else the old `LOOKOUT_FORECAST_*`), and every file and log written before stays valid and readable (`kind: lookout.forecast-run`, `software.lookout_forecast`). Measurements below dated before 21 Nov were taken under the old names.

The Snow Globe forecast worker (PRD §8.3, §11.2). It reads HRRR directly from NOAA's open-data
bucket `noaa-hrrr-bdp-pds` over anonymous HTTPS, fetches only the byte ranges of the fields it
needs (from each hour's `.idx`), decodes them with ecCodes, samples every viewpoint's points and
writes one JSON file per viewpoint per run. It also logs the publication lag of every forecast
hour, live and from S3 listings of past runs (D-027), which is the availability model for
archived runs.

Version 0.2.0 (20 Nov 2026): survey lag log, `tick`, `lag-check`, explicit hour lists, one
listing per extracted run, no download for a run that would write nothing, and archived hours
that are never available before their own S3 stamps. It reads every file, state marker and log
written by 0.1.0. Live files it writes are valid under the 0.1.0 schema; archived files are not
(see "Compatibility" below).

Python 3.13, uv, path dependency on `../common` (`snow-globe-common[s3]`).

```sh
cd workers/forecast && uv sync && uv run pytest -q && uv run ruff check .
uv run pytest -m network -s tests/test_live.py        # live smoke, about 21 MB
```

## Commands

| Command | Does |
|---|---|
| `snow-globe-forecast latest [--max-fh N] [--fields F ...] [--only VP]` | Extract the newest complete run (skips it if already written) |
| `snow-globe-forecast run YYYYMMDDHH [...] [--allow-incomplete] [--hours FH ...]` | Extract one run (only the hours it needs must exist); `--hours 24 25` extracts exactly those hours instead of 0..`--max-fh` |
| `snow-globe-forecast backfill --start YYYYMMDDHH --end YYYYMMDDHH [--every-h N] [--synoptic-only] [--max-total-mb N]` | Archived runs; availability is modelled |
| `snow-globe-forecast tick [--json] [--uncapped]` | One call of the Lambda handler, `handler({}, None)`: the code path the scheduled Lambda runs, with the `SNOW_GLOBE_FORECAST_*` environment; prints its summary (`--json`: the handler's return value, unchanged). Exits 0, or 1 if a run failed in the call (4 if every failure was a cap refusal), so a loop can tell without parsing the JSON. Refuses to start without `SNOW_GLOBE_FORECAST_MAX_MB` unless `--uncapped` |
| `snow-globe-forecast status [--offline] [--hours N]` | Files written, the lag log by method (live, survey, duplicates dropped), the lag model and how many of its 576 cells have left the fallback, cache size, and (online) which runs are on S3 |
| `snow-globe-forecast plan YYYYMMDDHH [--fields ...] [--hours FH ...]` | Bytes per field per hour, from the inventories only (no GRIB download) |
| `snow-globe-forecast grid-check YYYYMMDDHH [--write-fixture PATH]` | Compare the grid mapping with ecCodes' latitude/longitude arrays |
| `snow-globe-forecast datum-check YYYYMMDDHH` | Compare the height fields with the model terrain and with `PRES:cloud base` |
| `snow-globe-forecast lag-survey --start YYYYMMDDHH [--end YYYYMMDDHH] [--log] [--save-listings DIR]` | Publication lags of past runs from S3 listings (one per run, no GRIB; the run the newest-complete search found is not listed again). `--end` defaults to the newest complete run. Without `--log` it writes nothing; with it, every listed hour not logged yet (live or survey) becomes a survey lag event in `forecast_log/lag_survey/<today>.jsonl`. An hour with a stamp more than 24 h after the run start (a rewritten object) is left out and never logged (`stamps_not_trusted`) |
| `snow-globe-forecast lag-check YYYYMMDDHH ... [--save-listings DIR]` | Each run's S3-stamp lags next to the lag model (live + survey, and live only), and the availability an archived run of it gets (`availability_lag_min`, `availability_source`, `hours_set_by_stamps`, `hours_with_ignored_stamps`); one listing per run, writes nothing |
| `snow-globe-forecast hindcast plan\|fetch\|compare\|leads\|status` | P1's 24 h-lead hindcast, written only under `forecast/hindcast/` (see "Hindcast mode" below) |

Shared options: `--max-mb N` refuses a run whose planned download is larger (planned from the
inventories, before any GRIB byte is fetched); `--force` rewrites existing files; `--json`.
Fields can be named as in the inventory (`"HGT:cloud base"`), by alias (`CLOUDBASE`, `TERRAIN`,
`TCDC`, `BLCDC`) or by a unique variable (`VIS`, `LCDC`).

Without flags, `SNOW_GLOBE_FORECAST_FIELDS`, `SNOW_GLOBE_FORECAST_MAX_FH` and `SNOW_GLOBE_FORECAST_MAX_MB`
apply, so `make forecast-latest` can be limited from the environment.

**Downloads stay small by default.** With neither `--max-mb` nor `SNOW_GLOBE_FORECAST_MAX_MB`, the CLI
refuses a run planned above **150 MB** (exit code 4, before any GRIB byte). All 22 fields cost
about 24 MB per forecast hour: 453.0 MB for an 18 h run (2026111910), 1,202.1 MB for a 48 h run
(2026111906). So a whole run needs an explicit `--max-mb`, or fewer fields or hours (the smoke's
7 fields for f00-f02 are 21.4 MB). `backfill` also stops at **1,000 MB** of GRIB in all unless
`--max-total-mb` says otherwise, and each run's cap is what the total has left, so one run can
never overshoot it. `HGT:surface`, the static model terrain, is fetched once per run, with the
first hour (it was 8 % of a whole-run download when fetched every hour).

Lambda: `snow_globe_forecast.lambda_handler.handler` (`snow-globe-forecast tick` makes the same call
locally). Each call processes, newest first, every run of the last `SNOW_GLOBE_FORECAST_LOOKBACK_H`
(12) hours that is not written yet: complete runs, plus runs still incomplete after
`SNOW_GLOBE_FORECAST_STALE_H` (4) hours, with their missing hours recorded. A run already covered
costs one small storage read. A run that is extracted is listed once: the handler hands the
listing that showed it ready to the extractor (0.1.0 listed it twice, about 3.6-13 KB more per
run). Other settings: `SNOW_GLOBE_FORECAST_MAX_RUNS` (4), `SNOW_GLOBE_FORECAST_WORKERS` (8),
`SNOW_GLOBE_FORECAST_MAX_MB` (no default in the Lambda), `SNOW_GLOBE_FORECAST_RESERVE_S` (180),
`SNOW_GLOBE_FORECAST_TIMEOUT_S` (600, used without a Lambda context).

**Field-set changes.** Files are never narrowed: a file is rewritten only by an extraction that
holds all its fields and hours (or with `--force`).
- A wider field set (20 Nov, 02:39 UTC: `PRES:cloud base` added to the stand-in's profile) makes
  every run of the look-back uncovered, so each is extracted again (about 24 MB each), its files
  are rewritten and its `run` and `lag` events are logged a second time. Readers cope: the lag log
  keeps one lag per (run, hour) and counts the dropped duplicates (`status`: 160 on 20 Nov), `status`
  counts files, not run events, and a survey logs nothing for those hours. A rewritten live file's
  `fetch_finished_utc` is the new fetch, so its design-P availability can only move later.
- A field set that is not a superset (one field swapped for another) would write nothing. 0.1.0,
  which the running snapshot still has, fetches such runs again on every tick and writes nothing:
  reproduced on the committed code with the fake bucket (3 runs re-fetched on each of 3 ticks,
  0 files written). With the stand-in's profile that would be up to 4 runs of about 24 MB per
  5-minute tick until they leave the 12 h look-back. 0.2.0 reports them `kept` and makes no
  bucket request for them; `run`, `latest` and `backfill` likewise download nothing when no file
  would be written.

One run's failure never stops the others: any exception (ecCodes, a full `/tmp`, a bug) becomes an
`error` event and a back-off record, and the call moves on to older runs. A cap refusal is not
retried until the fields, hours, cap or viewpoints change; other errors are retried after 10, 20,
40, ... minutes, at most 4 h apart (`backoff.py`). A busy extraction lock (for example the S3 lock
of a call that timed out, until its 900 s expiry) or a failed bucket listing defers the rest of
the call to the next tick. Each row of the summary reports the bytes that run cost in this call.

## What it writes (under `$SNOW_GLOBE_DATA_DIR`, through `snow_globe_common.storage`)

| Key | Content |
|---|---|
| `forecast/hrrr/<viewpoint_id>/<YYYYMMDDHH>z.json` | One run for one viewpoint; schema `src/snow_globe_forecast/schema/forecast_run.schema.json` |
| `forecast_log/<YYYY-MM-DD>.jsonl` | `lag` events (`run`, `fh`, `last_modified`, `lag_s`, ..., `"method": "live"`; 0.1.0 wrote no `method`, which reads as live) for every listed hour of a live run; one `run` event per extraction (that extraction's own bytes, seconds, requests); `error` events from the Lambda (`kind`, `attempts`, `next_try_utc`, `bytes`). Every extraction appends to it: the Lambda (or the local stand-in), `tick`, `run`, `latest`, `backfill`, and anything else that calls `Extractor.extract`, such as eval's backtest pair (its archived run wrote a `run` event there on 19 Nov, 23:13 UTC). Survey events never go here |
| `forecast_log/lag_survey/<YYYY-MM-DD>.jsonl` | Survey `lag` events (`"method": "survey"`, plus `listed_utc`), written only by `lag-survey --log`, dated by the UTC day of the write; one per (run, hour) not already logged |
| `state/locks/forecast-lag-survey.lock` | Held by `lag-survey --log`, so two surveys never log the same hour twice |
| `state/forecast/hrrr/<YYYYMMDDHH>z.json` | Which viewpoints, fields and hours are written (idempotency) |
| `state/forecast/hrrr/<YYYYMMDDHH>z.failure.json` | The Lambda's back-off record for a failed run; marked `resolved_utc` once the run is written |
| `cache/hrrr/<run>/*.subset.grib2` | Each hour's byte ranges, deleted as soon as they are decoded |

A forecast file holds: `schema_version`, `source` (`noaa-hrrr-bdp-pds`), `model` (`hrrr-conus`),
`model_version` (null: not encoded in GRIB2), `run_start_utc`, `live`, `fetch_started_utc` and
`fetch_finished_utc`, the `grid`, and per forecast hour `valid_time_utc`, the object `url`,
`availability_utc` and `availability_source` (`live-s3-lastmodified`; for archived runs `modeled`
or `archived-s3-lastmodified`, with the model used and the stamp floor in `availability_model`),
`lag_s`, every message's byte range and the requests actually made; `missing_hours` lists
the hours asked for that were not on S3 (as the state marker does). `points` lists each
sample point (`id`, `kind` camera/ray/landmark/neighborhood, `lat`, `lon`, `i`, `j`, `cell_lat`,
`cell_lon`, `model_terrain_m`, `distance_km`, `azimuth_deg`, `name`). `fields` gives each field's
GRIB `shortName`, `typeOfLevel`, `level`, `units`, `datum` and the datum's evidence. `values` is
`[field][forecast hour][point]` with `null` where the GRIB bitmap marks a cell missing; an
`HGT:surface` row repeats the first hour's terrain for every hour (it is static: bit-for-bit
identical at f00, f01, f18 and f48 of 2026111906, all 1,905,141 cells), and later hours list no
byte range for it. `snow_globe_forecast.output.validate_document` checks the schema and the shapes.

Sample points per viewpoint: the camera cell; the view azimuth every 1.5 km from 1.5 to 45 km
(M2 picks where the ray meets each layer); every manifest landmark (Rainier's summit, Gibraltar
Rock, Anvil Rock and Little Tahoma Peak since the manifest correction of 23 Nov 2026); and the
5 × 5 block around the camera cell. That is 56 points, plus one per landmark.

**Lead** = `valid_time_utc - availability_utc`. A run is live if it is processed within 24 h of its
start: availability is then the later of the GRIB2 and `.idx` `LastModified` stamps.

**Archived runs** get a modelled availability that must never be early (an early time leaks a newer
run into a prediction, PRD §13.9). `availability.LagModel` keys the lag by the run's cycle hour
*and* forecast hour, because cycles differ (evidence below), and for hour `fh` of cycle `c` uses:
1. the p95 of the lags logged for (`c`, `fh`) once there are 20 (`p95-cycle-hour`);
2. else, once `fh` has 20 lags over all cycles and (`c`, `fh`) has one: the later of that pooled
   p95 (the PRD §8.3 rule) and the largest lag logged for (`c`, `fh`)
   (`p95-forecast-hour-or-cycle-max`);
3. else **120 min**, or the largest lag logged for (`c`, `fh`) if later (`conservative-fallback`).

"Logged" means live and survey lags (D-027). They are kept apart (`method`; a live lag wins where
both exist), counted apart (`samples_by_method`, `pooled_samples_by_method` in `availability_model`
and `status`), and `LagModel.from_storage(storage, methods={"live"})` gives the live-only model.
**Survey lags enter the p95** because:
- they are the same measurement: on 20 Nov the survey re-listed 594 hours already logged live, and
  all 594 lags were equal (largest difference 0.0 s);
- a later listing can only show the same stamps or, after a re-upload, later ones, so a survey can
  only err late; and the January 2024 runs still carry their publication stamps (below);
- the live log alone is censored: the stand-in's f00-f02 profile processes a run as soon as f02 is
  up, so a later hour is logged live only if it was published before that moment, which drops
  exactly the late hours. 2026111911 (processed at 78.0 min) lacks f09, published at 79.1 min after
  f10-f13; 2026111912 (56.5 min) has 5 of its 49 hours. The survey filled both (6 and 44 hours).

**An archived hour is never available before its own S3 stamps say it was published** (PRD
§13.9; review finding, 20 Nov). Its availability is the later of the model and the stamp floor
(`availability.archived_availability`, `discovery.publication_floor`): the later of its GRIB2
and `.idx` `LastModified`, as for a live run. A stamp more than **24 h** after the run start is a
rewritten object, not a publication, and is ignored (`s3_stamps_ignored`); if only the GRIB2
stamp counts, the floor is that stamp + 300 s (the `.idx` came 3-188 s after its GRIB2 in 5,374
listed hours). Where the stamps win, `availability_source` is `archived-s3-lastmodified`. Why:
the model is fitted on November 2026 lags, and other seasons published some hours later. On 20
Nov, 18 archived runs were listed (2015-07 to 2026-03, 442,353 bytes, no GRIB). Of the 686 hours
whose two stamps count (14 runs), 49 were published after the time the live + survey model
gives, by up to 6.37 min (2021031512 f32): 18 in 2021031512, 17 in 2023071500, 10 in 2023011518
(the live-only model: 3). With the floor none is, and 91 hours take their stamp (42 of them in
the two runs whose `.idx` was rewritten, from the GRIB2 stamp + 300 s). Rewritten stamps exist:
every `.idx` of 2020120312 and 2021011500 is stamped 17 Feb 2021 (75.7 and 33.6 days late) while
their GRIB2 files keep publication stamps; both objects of 2015071512 and 2018071512 carry
2020-2021 stamps. A survey never logs an hour with such a stamp, and readers count a lag above
24 h as invalid (one would set its cell's lag to weeks).

The typical lags of `docs/research/forecast.md` (f00 52 min, f18 85 min, f48 108 min) are not used:
37.3 % of the surveyed lags were later than them, and they were earlier than all 19 lags logged for
2026111908.

**Choosing a run** for an issued prediction: `availability.select_stored_run(storage, viewpoint,
issue, valid)` returns the newest *stored* run whose needed hours were available by the issue
time, with the actual lead. A run or hour that is not stored is skipped (the search falls back to
an older run; it is never modelled into existence). A live hour counts as available at the later
of its S3 time and the file's `fetch_finished_utc`, since Snow Globe could not use it earlier (design P).
An archived hour is re-modelled from the current log (the `availability_utc` stored in an archived
file is only what the model said when the file was written) and never placed before the stamps the
file records. `select_run_for_issue` is the same rule without storage: with no `availability`
callback it can only model, which can be earlier than the run's stamps; pass
`availability.listed_availability(bucket)` (one listing per candidate run) to apply the floor
before a run is stored, as a backtest pair should. 24 h leads only come from 00/06/12/18Z runs.

**Compatibility.** `schema_version` is still 1, because eval's `forecast_io` accepts only 1. Files
written by 0.2.0 are read by 0.1.0's code paths unchanged, and live files validate under the 0.1.0
schema. Archived files do not: `availability_model` has `samples_by_method`,
`pooled_samples_by_method`, `s3_floor_utc` and `s3_stamps_ignored`, and `availability_source` may be
`archived-s3-lastmodified` (checked on the fake bucket: 0 errors for a live file, 2 for an archived
one, 4 when the stamps won). Validate archived files with the 0.2.0 schema; a bump to 2 needs eval
to accept both.

## Fields and datums

Names were verified against live inventories (2026-11-19 06Z f00, f01, f06, f48). Each appears
exactly once per hour (`anl` at f00). Every decoded message is checked against the GRIB2 identity
in `fields.py` (discipline, category, number, first fixed surface, level), its run and hour, its
byte length and its grid. That check caught three catalogue errors on live data before anything
was written: `HPBL` is WMO 0-3-18 (not NCEP 0-3-196), and the precipitation types are WMO 0-1-33
to 0-1-36 (not 192-195).

| Field | ecCodes | Datum | Evidence (HRRR 2026111907 f00, 1,905,141 cells) |
|---|---|---|---|
| `HGT:cloud ceiling` | `gh`, cloudCeiling, gpm | m above MSL | 0 of 502,664 cells below the model terrain; the lowest ceiling is 34.6 m above it |
| `HGT:cloud base` | `gh`, cloudBase, gpm | m above MSL, **often invalid** | Agrees with `PRES:cloud base` where valid (median gap 9 m), but in 12.4 % of cloudy cells it is a phantom low base 7.4-8.3 km too low (see below) |
| `HGT:cloud top` | `gh`, cloudTop, gpm | m above MSL | 0 of 457,194 cells below the terrain; the lowest top is 42.0 m above it |
| `HPBL:surface` | `blh`, surface, m | m above ground | Below the terrain height in 100 % of the 86,217 cells above 2,000 m (median 181 m against 2,275 m of terrain); minimum 9.9 m |
| `HGT:surface` | `orog`, surface, m | m above MSL | The model's smoothed terrain (-81.1 to 3,995.1 m) |
| all others | | n/a | Values are not heights |

GRIB2 metadata gives `HGT` as WMO 0-3-5, geopotential height (gpm), which is relative to MSL. The
NOAA-EMC UPP source (`develop` at `41077f7`, and tag `v4.1.0.beta03`) builds the cloud heights from
`ZINT = (PH + PHB) / g` with `ZINT(LM+1) = FIS / g` (`INITPOST.F` lines 831-850 and 981; `CLDRAD.f`
IGET 178, 179 and 408). PBLH is read unchanged from WRF. Line-level citations are in `fields.py`.

**The cloud-base problem.** In 2.45 % of cloudy cells, `HGT:cloud base` is a phantom base that also
falls below the terrain (0.06 % where the terrain is under 500 m, 37.5 % where it is above 2,500 m). Comparing it with the
height `PRES:cloud base` implies (standard-lapse-rate hypsometric equation from the surface
pressure) finds 110,670 of 891,599 cells (12.4 %) where HGT is 7.4-8.3 km too low; only 21,868 of
them are below the terrain. Those columns hold high cloud only (mean HCDC 63 %, LCDC 3.7 %, against
LCDC 77.7 % where low bases agree). So the pressure is right and HGT is wrong there. `PRES:cloud
base` is therefore in the default field set (22 fields; it is the next message in every file, so it
adds 1.3 MB per hour and no request). Use `HGT:cloud base` only where it agrees with the pressure.

Other pitfalls seen on live data:
- `UGRD`/`VGRD` are grid-relative (`uvRelativeToGrid = 1`); rotate with `grid.earth_relative_wind`.
- Missing cells read back as `9999` unless the bitmap is used; `decode.values_and_mask` uses it.
- Hours are published out of order (07Z had f09 before f08; 05Z had f01 seven minutes before
  f00), so completeness means every hour, not the last one.
- S3 gives a multipart upload the time it *started*; the later `.idx` stamp (about 4 s after the
  GRIB2) is used as availability.
- At f00 the precipitation types are near-empty constant fields (188 bytes each).
- The 3 km terrain is not the DEM: Rainier's summit cell is 3,467 m against 4,392 m, Grand Canyon's
  camera cell 1,230 m against a 2,150 m rim camera, and Point Reyes' camera cell is sea (0.01 m).

## Grid

HRRR CONUS is Lambert conformal on a 6,371,229 m sphere: 1799 × 1059 cells of 3 km, tangent at
38.5° N, `LoV` 262.5°, scanning mode 64, so cell `(i, j)` is value `j * 1799 + i`. `grid.py` uses
Snyder's spherical formulas, and every message is checked against `HRRR_CONUS`. Measured against
ecCodes' own latitude/longitude arrays (2026111907 f00): the maximum position error is 1.3e-11 km
over all 1,905,141 cells, and the inverse maps every cell back to itself.
`tests/fixtures/hrrr_conus_grid.json` holds the grid keys and 20 true `(i, j, lat, lon)` tuples (no
GRIB data).

## Measured (2026-11-19)

| What | Value |
|---|---|
| Live smoke (final code, 12:01 UTC): run 2026111910, f00-f02, 7 fields, 8 viewpoints | 21,440,626 bytes this run: 21,398,192 GRIB in 10 ranges, 1 listing, 3 inventories, 0 retries; 3.46 s wall, 3.08 s extraction, decode 0.50 s; 8 files of 29.3-30.9 KB; no GRIB left |
| Earlier smoke (first build, 09:54 UTC): run 2026111908, same request | 25.4 MB; its "3 listings" included the `latest` search, and HGT:surface was fetched every hour. Its 8 files in `~/snow-globe-data/forecast/hrrr/*/2026111908z.json` come from that build |
| Publication lag, 2026111908 / 2026111910 (logged) | f00 53.3 / 51.3 min, f02 57.2 / 55.3, f06 64.6 / 63.8, f18 86.4 / 85.6; out of order: 08Z f05 73.5, 10Z f04 70.6 |
| Publication lag survey: 124 runs, 2026-11-14 06Z to 2026-11-19 09Z (`lag-survey`, 2.0 MB of listings) | 2,986 hour-lags; 37.3 % later than the forecast.md curve; per-hour p95 up to 25 min above it (f11 97.1 vs 72.2 min); 3 lags above 120 min (0.10 %, all 2026111715, max 131.2 min); every 23Z run published f06-f18 at ~111 min while other cycles' medians were 62.6-85.1 min |
| `lag-survey --log`, 2026-11-12 00Z to 2026-11-20 01Z (2026112001, the newest complete run) (20 Nov, 03:11-03:12 UTC) | 194 runs, 0 incomplete; 4,676 listed hour-lags; **4,082 survey events logged** (594 hours were already logged live, all 594 with equal lags); 194 listings, 3,160,172 bytes, 0 retries, 53 s wall; 3 lags above 120 min (the same 2026111715 hours, max 131.17 min). The log then held 4,679 lags of 195 runs (597 live, 4,082 survey) |
| Lag-model cells (24 cycles x f00-f18 + 4 synoptic cycles x f19-f48 = 576) | Off the 120 min fallback: 436 before the survey (live lags alone; 140 on it), **576 after** (all by rule 2; no cell has 20 lags yet, 8-9 each) |
| Archived runs 2024011412 and 2024011418 (`lag-check`, one listing each, 48,786 bytes, no GRIB) | Stamps from 2024-01-14 12:49:49-13:47:13Z and 18:50:46-19:48:57Z: lags 49.9-107.2 and 50.8-109.0 min, 49/49 hours each, the same shape as live runs 1,040 days later. So the archive keeps publication times. Every hour is 1.27-32.0 min earlier than the model (tightest: 18Z f38, 99.60 vs 100.87 min); none is later |
| 18 archived runs, 2015071512 to 2026031518 (`lag-check`, one listing each, 442,353 bytes, no GRIB; replayed with the final code against the live + survey log, 20 Nov 06 UTC) | 14 runs keep both stamps (686 hours, lags 49.9-116.5 min); 2020120312 and 2021011500 keep GRIB2 stamps only (their `.idx` are stamped 17 Feb 2021); 2015071512 and 2018071512 keep neither. 49 of the 686 hours were published after the model's time (up to 6.37 min, 2021031512 f32; live-only model: 3); with the stamp floor 0 are, and 91 hours take their stamp |
| HGT:surface across hours, run 2026111906 | identical at f00, f01, f18, f48 (max difference 0.0 m over 1,905,141 cells; 2,153,695-byte messages); 8.6 MB downloaded to check |
| All 22 fields, whole run (terrain once) | 453.0 MB (18 h run 2026111910, 14.95 % of the GRIB2 files); 1,202.1 MB (48 h run 2026111906, 15.04 %); the plan itself costs one listing and one `.idx` per hour (546,827 bytes for 49 hours) |
| Costliest fields (MB per hour) | HPBL 2.99, HGT:cloud base 2.83, UGRD 2.38, VGRD 2.38, HGT:surface 2.15 (now once per run), ceiling 1.86, top 1.72, RH 1.65, VIS 1.50 |

## Hindcast mode (P1 at a 24 h lead; PRD §13.4)

`snow-globe-forecast hindcast ...` (`snow_globe_forecast.hindcast`) fetches archived runs for P1's
development pairs. It runs the live extractor through `HindcastStorage`, which maps the extractor's
keys into `<data dir>/forecast/hindcast/` and refuses any other key, so it never writes
`forecast/hrrr/` or `forecast_log/`; the live data dir (frames, thumbs, live files, `forecast_log`) is
read through a read-only view.

| Command | Does |
|---|---|
| `hindcast plan [--lead-h 24] [--max-total-mb 2000]` | Every captured daytime frame (Sun >= 3 deg, geometric, NOAA equations at the manifest camera; sidecars of `frames/` and `thumbs/`, never the sealed window) is paired with the newest run whose hours bracketing its valid time were available 24 h before it (`select_run_for_issue`; only 00/06/12/18Z runs reach that far), and each run's hours are priced from the `.idx` byte ranges of the live worker's 8 fields. Listings and inventories only. Writes `plan/p1-lead<lead>h.json` (`p1-lead24h.json` for P1), so a plan at another lead never replaces P1's. Exit 4 if what the hindcast has already downloaded plus the runs not written yet exceed the cap (`totals.within_cap`, the rule `fetch` applies) |
| `hindcast fetch [--lead-h 24] [--max-total-mb 2000] [--max-errors 4]` | Fetches the plan at that lead, Rainier only: one request at a time, retries with back-off, resumable (a written run costs no request), and every byte the hindcast downloads (plan included) counted against the cap. A run whose file holds other hours (another plan) is fetched for the union, so the file is widened, never narrowed; a run the extractor still writes nothing for is listed in `runs_not_written`, never as written, and the command exits 1 (`not-written`) |
| `hindcast compare` | Each hindcast hour's S3 availability against the live lag model and the live + survey model, and against the lag the live log recorded; each row gives the lags each model pools for its hour (the p95 rules need 20) |
| `hindcast leads [--leads 1 3 6 12 24 48] [--sample-runs ...] [--max-total-mb 2000]` | Design P's forecast hours per run and MB per day (see below), priced at three field profiles from a few `.idx` files |
| `hindcast status` | Files, plans and their totals, progress and the network ledger (offline) |

Every command but `status` holds one lock (`forecast/hindcast/state/locks/hindcast-command.lock`),
so no two run at once and the network ledger has one writer (each ledger update also adds only its
own client's new counts, under a lock of its own). Before every network attempt, retries included,
a command waits out the quiet window (`--quiet-start-minute 50 --quiet-end-minute 5` by default,
the task's window), and refuses the request once the hindcast's whole download has reached
`--max-total-mb`. Of the stand-in's 75 live fetches from 20 Nov 06:00Z to 23 Nov 09:40Z, 61 started
between :50 and :05 and 13 between :05 and :15 (runs published late); `--quiet-end-minute 15` would
cover those too.

What it writes, under `forecast/hindcast/`: `hrrr/<viewpoint_id>/<YYYYMMDDHH>z.json` (the live files'
format, `"live": false`), `log/<day>.jsonl` (the extractor's `run` events and `hindcast-*` events:
plan, run, error, back-off, quiet-window pause, cap), `state/` (markers, locks, `hindcast-fetch.json`
progress, `network-ledger.json`), `plan/` (the current plan and its history), `reports/`, `cache/s3/`
(every listing and `.idx` answer, so none is fetched twice) and `cache/grib/` (subsets, deleted as
soon as they are decoded).

**Availability in a hindcast file** is each hour's S3 `LastModified`, the later of the GRIB2 and
`.idx` stamps, exactly as a live file records it, so it is never earlier than S3 says
(`availability_source` `archived-s3-lastmodified`; a stamp more than 24 h after the run start is not
trusted, and an hour with no trusted stamp is refused). `availability_model` holds the **live lag
model's** prediction for the hour (`LagModel` over `forecast_log/*.jsonl`'s live lags only, when the
file was written), for comparison only, with the floor in `s3_floor_utc`. So in a hindcast file
`archived-s3-lastmodified` means "the stamps, by the hindcast's rule", not "the stamps were later
than the model" as in the worker's own archived files; such an hour has `availability_model.lag_s`
later than `lag_s`.

**This is the task's rule, not D-008's.** D-008, D-027 and PRD §8.3 model an archived run as its run
start plus the p95 lag, never before the stamps (D-054). `availability.select_stored_run` applies that rule to
any `"live": false` file and ignores its `availability_utc`; given no `lag_model`, it reads the lag
log of the storage it is handed, and a `HindcastStorage`'s log holds no lags, so every hour would sit
on the 120 min fallback. Pass the live data dir's `LagModel` explicitly, and read `availability_utc`
to pair under the hindcast's rule (eval's P1 dev table does). Re-selecting the plan's 105 pairs from
the 8 files (23 Nov, 11:34 UTC): the S3 rule gives the plan's run for all 105; the D-054 rule with
the live + survey model for 104 (1 moves to another stored run); with the live-only model, or
`select_stored_run` on the hindcast storage with no model, for 97 (6 move, 2 find no stored run).
Which rule P1 uses is the lead's call.

Measured on 23 Nov 2026 (Rainier, manifest as corrected that day):

| What | Value |
|---|---|
| Frames (`plan`, 10:28 UTC) | 342 unique (342 frame and 294 thumbnail sidecars, 294 in both), 19 Nov 06:30Z to 23 Nov 10:23Z; 105 daytime, all paired; Sun check against the 317 sidecars that record it: max 0.0046 deg, no frame changes side of 3 deg |
| Plan | 8 runs (2026111812 f30-f31; 2026111818, 2026111918, 2026112018, 2026112118 f25-f30; 2026111912, 2026112012 f27-f32; 2026112112 f28-f32): 43 forecast hours, 441,854,992 bytes planned; actual leads 24.01-29.94 h (median 27.22); the plan cost 716,863 bytes in 51 requests (8 listings, 43 `.idx`) |
| Fetch (10:28:43-10:29:26 UTC) | 8 files written; 441,854,992 bytes of GRIB in 137 range requests, the planned bytes exactly; 0 retries, 0 errors; 42.9 s wall, 37.9 s in transfers, 0 s paused; no subset left |
| S3 against the lag models (43 hours) | S3 lags 81.7-94.8 min. Live lag model: every hour on the 120 min fallback, 25.2-38.3 min later than S3, never early; the live log holds 4 live lags for each of f25-f32 (runs 2026111900, 06, 18 and 2026112000), below the 20 its p95 rules need. Live + survey model: 1 hour early, 2026112018 f26 by 342 s (S3 92.6 min, model 86.9 min; a run the survey never listed), the others later by up to 641 s. The 20 hours the live or survey log also holds (6 live, 14 survey) agree with S3 to 0.0 s |
| Design P, daytime at Rainier and Point Reyes in the sealed window (`leads`) | 21 runs a day, 130 forecast hours for leads 1-24 h; 48 h is never served. At the stand-in's 8 fields (D-050): 1,280 MB a day (1,722 MB with the stand-in's f00-f02 for every run). The same hours at the scene builder's 12 fields: 2,137 MB (2,903 MB); at all 22 fields, the deployed job's profile (D-101): 3,284 MB (4,521 MB) |
| Design P, every valid time | 24 runs, 320 forecast hours (20 for each synoptic run; 11-14, 240 in all, for the 20 others). 8 fields: 3,093 MB a day (3,298 MB with f00-f02); 12 fields: 5,202 MB (5,560 MB); 22 fields: 8,025 MB (8,610 MB) |
| Bytes per forecast hour (`.idx` of 2026112200, 06, 07, 08; terrain 2.15 MB once per run) | 8 fields 7.8-10.5 MB; 12 fields 13.6-17.4 MB; 22 fields 22.1-26.3 MB |

## Why not Herbie (D-008 said Herbie)

`herbie-data` 2026.10.0 installs cleanly with uv on Python 3.13, and its inventory gave the same
range as this package (`VIS:surface` f00, bytes 1364280-2852420). A logged trial on 2026-11-19
showed why it isn't used here:
- It calls module-level `requests`, so every request carries `python-requests/2.34.2` instead of
  the project User-Agent. Its HEAD and `.idx` requests had `timeout=None`: one stalled socket would
  hold a Lambda run until its 600 s timeout.
- It made four requests for one field-hour (two HEAD probes, the `.idx`, the range). It does not
  expose `LastModified` or `Size`, which availability needs, so the ListObjectsV2 listing is needed
  anyway, and that listing already proves the files exist.
- On import it wrote `config.toml`, `__init__.py` and `custom_template.py` to `~/.config/herbie`.
  Its `xarray(remove_grib=True)` left the subset and the `.idx` on disk.
- It adds pandas (53 MB), pyproj (20 MB), xarray (11 MB), cfgrib and requests to the Lambda image,
  for features this worker doesn't use.

What is kept: the same ecCodes wheels Herbie installs (`eccodes` 2.50.1 with `eccodeslib`
2.50.0.34), its grouping of adjacent messages into one range, and its check that a range answer
is `206`.

## Troubleshooting: `ModuleNotFoundError: No module named 'snow_globe_common'`

The repo sits in iCloud-synced `~/Documents`. On this machine the `.pth` files of editable installs
in `.venv` gain the macOS `hidden` flag (seen at 04:01 and again before 06:00 on 2026-11-19), and
Python 3.13 skips hidden `.pth` files. The tests do not depend on them (`pythonpath` in
`pyproject.toml`). For the CLI, run `PYTHONPATH=src:../common/src uv run snow-globe-forecast ...`,
which always runs the current source, or `chflags nohidden .venv/lib/python3.13/site-packages/*.pth`.
Do **not** rely on `UV_NO_EDITABLE=1`: it installed a cached wheel of the package (its `cli.py`
from 04:02, while the source had changed at 05:59), because uv only rebuilds a local package when
its `pyproject.toml` changes.

## Notes and TODOs

- `TODO(M2)`: choose, per layer, the ray cell where the view ray meets the layer height (needs
  the camera pose); M0.5 stores the whole ray.
- `TODO(M1.5)`: mask phantom cloud bases (HGT against `PRES:cloud base`) in the renderer and the
  extractor inputs.
- The laptop profile is set (closed 20 Nov): the Makefile's light profile (`SNOW_GLOBE_FORECAST_FIELDS`,
  `SNOW_GLOBE_FORECAST_MAX_FH`, `SNOW_GLOBE_FORECAST_MAX_MB`: 8 fields, f00-f02, at most 40 MB a run) is
  the local stand-in's since 02:39 UTC on 20 Nov (D-050), so `make forecast-latest` and
  `make forecast-tick` ask for exactly what the stand-in fetches. Run without those variables, the
  CLI's 150 MB default cap still refuses a whole-run download (exit 4).
- `TODO(M1)`: no cell has the 20 lags rule 1 needs yet (8-9 each after the 20 Nov survey); a
  survey of about 12 more days gets there. Run `lag-survey --log --start <the last surveyed run, or
  earlier>` now and then: overlap costs only listings (about 8-33 KB a run) and logs nothing twice,
  while starting after the last surveyed run would skip runs that were incomplete or not yet
  published when it ran. The live log alone cannot get there for f03 and later: the stand-in's
  f00-f02 profile logs a later hour only if it was already published (in the 13 first extractions
  made 55.5-78.7 min after their run start, 48 of 268 f03+ hours were listed), so those cells
  would get a p95 of their early hours only.
- `TODO(M1)`: the model is fitted on November 2026 lags. Stored archived runs are now floored by
  their own stamps, so an early model can no longer leak; it still decides which run is chosen
  when the model is later than the stamps, and it is all there is for a run with no trusted
  stamp. Surveying the design-H seasons (listings only, about 24-33 KB per synoptic run; the
  survey skips rewritten stamps) is the lead's decision.
