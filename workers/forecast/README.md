# lookout-forecast

The Lookout forecast worker (PRD §8.3, §11.2). It reads HRRR directly from NOAA's open-data
bucket `noaa-hrrr-bdp-pds` over anonymous HTTPS, fetches only the byte ranges of the fields it
needs (from each hour's `.idx`), decodes them with ecCodes, samples every viewpoint's points and
writes one JSON file per viewpoint per run. It also logs the publication lag of every forecast
hour, which is the availability model for archived runs.

Python 3.13, uv, path dependency on `../common` (`lookout-common[s3]`).

```sh
cd workers/forecast && uv sync && uv run pytest -q && uv run ruff check .
uv run pytest -m network -s tests/test_live.py        # live smoke, about 21 MB
```

## Commands

| Command | Does |
|---|---|
| `lookout-forecast latest [--max-fh N] [--fields F ...] [--only VP]` | Extract the newest complete run (skips it if already written) |
| `lookout-forecast run YYYYMMDDHH [...] [--allow-incomplete]` | Extract one run (only the hours it needs must exist) |
| `lookout-forecast backfill --start YYYYMMDDHH --end YYYYMMDDHH [--every-h N] [--synoptic-only] [--max-total-mb N]` | Archived runs; availability is modelled |
| `lookout-forecast status [--offline] [--hours N]` | Files written, lag model, cache size, and (online) which runs are on S3 |
| `lookout-forecast plan YYYYMMDDHH [--fields ...]` | Bytes per field per hour, from the inventories only (no GRIB download) |
| `lookout-forecast grid-check YYYYMMDDHH [--write-fixture PATH]` | Compare the grid mapping with ecCodes' latitude/longitude arrays |
| `lookout-forecast datum-check YYYYMMDDHH` | Compare the height fields with the model terrain and with `PRES:cloud base` |
| `lookout-forecast lag-survey --start YYYYMMDDHH --end YYYYMMDDHH` | Publication lags of past runs from S3 listings (one per run, no GRIB, writes nothing) |

Shared options: `--max-mb N` refuses a run whose planned download is larger (planned from the
inventories, before any GRIB byte is fetched); `--force` rewrites existing files; `--json`.
Fields can be named as in the inventory (`"HGT:cloud base"`), by alias (`CLOUDBASE`, `TERRAIN`,
`TCDC`, `BLCDC`) or by a unique variable (`VIS`, `LCDC`).

Without flags, `LOOKOUT_FORECAST_FIELDS`, `LOOKOUT_FORECAST_MAX_FH` and `LOOKOUT_FORECAST_MAX_MB`
apply, so `make forecast-latest` can be limited from the environment.

**Downloads stay small by default.** With neither `--max-mb` nor `LOOKOUT_FORECAST_MAX_MB`, the CLI
refuses a run planned above **150 MB** (exit code 4, before any GRIB byte). All 22 fields cost
about 24 MB per forecast hour: 453.0 MB for an 18 h run (2026111910), 1,202.1 MB for a 48 h run
(2026111906). So a whole run needs an explicit `--max-mb`, or fewer fields or hours (the smoke's
7 fields for f00-f02 are 21.4 MB). `backfill` also stops at **1,000 MB** of GRIB in all unless
`--max-total-mb` says otherwise, and each run's cap is what the total has left, so one run can
never overshoot it. `HGT:surface`, the static model terrain, is fetched once per run, with the
first hour (it was 8 % of a whole-run download when fetched every hour).

Lambda: `lookout_forecast.lambda_handler.handler`. Each call processes, newest first, every run of
the last `LOOKOUT_FORECAST_LOOKBACK_H` (12) hours that is not written yet: complete runs, plus runs
still incomplete after `LOOKOUT_FORECAST_STALE_H` (4) hours, with their missing hours recorded. A
run already covered costs one small storage read. Other settings: `LOOKOUT_FORECAST_MAX_RUNS` (4),
`LOOKOUT_FORECAST_WORKERS` (8), `LOOKOUT_FORECAST_MAX_MB` (no default in the Lambda),
`LOOKOUT_FORECAST_RESERVE_S` (180).

One run's failure never stops the others: any exception (ecCodes, a full `/tmp`, a bug) becomes an
`error` event and a back-off record, and the call moves on to older runs. A cap refusal is not
retried until the fields, hours, cap or viewpoints change; other errors are retried after 10, 20,
40, ... minutes, at most 4 h apart (`backoff.py`). A busy extraction lock (for example the S3 lock
of a call that timed out, until its 900 s expiry) or a failed bucket listing defers the rest of
the call to the next tick. Each row of the summary reports the bytes that run cost in this call.

## What it writes (under `$LOOKOUT_DATA_DIR`, through `lookout_common.storage`)

| Key | Content |
|---|---|
| `forecast/hrrr/<viewpoint_id>/<YYYYMMDDHH>z.json` | One run for one viewpoint; schema `src/lookout_forecast/schema/forecast_run.schema.json` |
| `forecast_log/<YYYY-MM-DD>.jsonl` | `lag` events (`run`, `fh`, `last_modified`, `lag_s`, ...) for every listed hour of a live run; one `run` event per extraction (that extraction's own bytes, seconds, requests); `error` events from the Lambda (`kind`, `attempts`, `next_try_utc`, `bytes`) |
| `state/forecast/hrrr/<YYYYMMDDHH>z.json` | Which viewpoints, fields and hours are written (idempotency) |
| `state/forecast/hrrr/<YYYYMMDDHH>z.failure.json` | The Lambda's back-off record for a failed run; marked `resolved_utc` once the run is written |
| `cache/hrrr/<run>/*.subset.grib2` | Each hour's byte ranges, deleted as soon as they are decoded |

A forecast file holds: `schema_version`, `source` (`noaa-hrrr-bdp-pds`), `model` (`hrrr-conus`),
`model_version` (null: not encoded in GRIB2), `run_start_utc`, `live`, `fetch_started_utc` and
`fetch_finished_utc`, the `grid`, and per forecast hour `valid_time_utc`, the object `url`,
`availability_utc` and `availability_source` (`live-s3-lastmodified` or `modeled`, with the model
used), `lag_s`, every message's byte range and the requests actually made; `missing_hours` lists
the hours asked for that were not on S3 (as the state marker does). `points` lists each
sample point (`id`, `kind` camera/ray/landmark/neighborhood, `lat`, `lon`, `i`, `j`, `cell_lat`,
`cell_lon`, `model_terrain_m`, `distance_km`, `azimuth_deg`, `name`). `fields` gives each field's
GRIB `shortName`, `typeOfLevel`, `level`, `units`, `datum` and the datum's evidence. `values` is
`[field][forecast hour][point]` with `null` where the GRIB bitmap marks a cell missing; an
`HGT:surface` row repeats the first hour's terrain for every hour (it is static: bit-for-bit
identical at f00, f01, f18 and f48 of 2026111906, all 1,905,141 cells), and later hours list no
byte range for it. `lookout_forecast.output.validate_document` checks the schema and the shapes.

Sample points per viewpoint: the camera cell; the view azimuth every 1.5 km from 1.5 to 45 km
(M2 picks where the ray meets each layer); every manifest landmark (Rainier's summit); and the
5 × 5 block around the camera cell. That is 56 points, plus one per landmark.

**Lead** = `valid_time_utc - availability_utc`. A run is live if it is processed within 24 h of its
start: availability is then the later of the GRIB2 and `.idx` `LastModified` stamps.

**Archived runs** get a modelled availability that must never be early (an early time leaks a newer
run into a prediction, PRD §13.9). `availability.LagModel` keys the lag by the run's cycle hour
*and* forecast hour, because cycles differ (evidence below), and for hour `fh` of cycle `c` uses:
1. the p95 of the live lags logged for (`c`, `fh`) once there are 20 (`p95-cycle-hour`);
2. else, once `fh` has 20 lags over all cycles and (`c`, `fh`) has one: the later of that pooled
   p95 (the PRD §8.3 rule) and the largest lag logged for (`c`, `fh`)
   (`p95-forecast-hour-or-cycle-max`);
3. else **120 min**, or the largest lag logged for (`c`, `fh`) if later (`conservative-fallback`).

The typical lags of `docs/research/forecast.md` (f00 52 min, f18 85 min, f48 108 min) are not used:
37.3 % of the surveyed lags were later than them, and they were earlier than all 19 lags logged for
2026111908.

**Choosing a run** for an issued prediction: `availability.select_stored_run(storage, viewpoint,
issue, valid)` returns the newest *stored* run whose needed hours were available by the issue
time, with the actual lead. A run or hour that is not stored is skipped (the search falls back to
an older run; it is never modelled into existence). A live hour counts as available at the later
of its S3 time and the file's `fetch_finished_utc`, since Lookout could not use it earlier (design P).
An archived hour is re-modelled from the current log: the `availability_utc` stored in an archived
file is only what the model said when the file was written. `select_run_for_issue` is the same rule
without storage. 24 h leads only come from 00/06/12/18Z runs.

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
| Earlier smoke (first build, 09:54 UTC): run 2026111908, same request | 25.4 MB; its "3 listings" included the `latest` search, and HGT:surface was fetched every hour. Its 8 files in `~/lookout-data/forecast/hrrr/*/2026111908z.json` come from that build |
| Publication lag, 2026111908 / 2026111910 (logged) | f00 53.3 / 51.3 min, f02 57.2 / 55.3, f06 64.6 / 63.8, f18 86.4 / 85.6; out of order: 08Z f05 73.5, 10Z f04 70.6 |
| Publication lag survey: 124 runs, 2026-11-14 06Z to 2026-11-19 09Z (`lag-survey`, 2.0 MB of listings) | 2,986 hour-lags; 37.3 % later than the forecast.md curve; per-hour p95 up to 25 min above it (f11 97.1 vs 72.2 min); 3 lags above 120 min (0.10 %, all 2026111715, max 131.2 min); every 23Z run published f06-f18 at ~111 min while other cycles' medians were 62.6-85.1 min |
| HGT:surface across hours, run 2026111906 | identical at f00, f01, f18, f48 (max difference 0.0 m over 1,905,141 cells; 2,153,695-byte messages); 8.6 MB downloaded to check |
| All 22 fields, whole run (terrain once) | 453.0 MB (18 h run 2026111910, 14.95 % of the GRIB2 files); 1,202.1 MB (48 h run 2026111906, 15.04 %); the plan itself costs one listing and one `.idx` per hour (546,827 bytes for 49 hours) |
| Costliest fields (MB per hour) | HPBL 2.99, HGT:cloud base 2.83, UGRD 2.38, VGRD 2.38, HGT:surface 2.15 (now once per run), ceiling 1.86, top 1.72, RH 1.65, VIS 1.50 |

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

## Troubleshooting: `ModuleNotFoundError: No module named 'lookout_common'`

The repo sits in iCloud-synced `~/Documents`. On this machine the `.pth` files of editable installs
in `.venv` gain the macOS `hidden` flag (seen at 04:01 and again before 06:00 on 2026-11-19), and
Python 3.13 skips hidden `.pth` files. The tests do not depend on them (`pythonpath` in
`pyproject.toml`). For the CLI, run `PYTHONPATH=src:../common/src uv run lookout-forecast ...`,
which always runs the current source, or `chflags nohidden .venv/lib/python3.13/site-packages/*.pth`.
Do **not** rely on `UV_NO_EDITABLE=1`: it installed a cached wheel of the package (its `cli.py`
from 04:02, while the source had changed at 05:59), because uv only rebuilds a local package when
its `pyproject.toml` changes.

## Notes and TODOs

- `TODO(M2)`: choose, per layer, the ray cell where the view ray meets the layer height (needs
  the camera pose); M0.5 stores the whole ray.
- `TODO(M1.5)`: mask phantom cloud bases (HGT against `PRES:cloud base`) in the renderer and the
  extractor inputs.
- `TODO(M0.5)`: the laptop profile (which fields and hours `make forecast-latest` asks for) is the
  lead's call; until then the 150 MB default cap refuses a whole-run download.
- `TODO(M1)`: with the worker logging every run, the lag model leaves the 120 min fallback after
  about 1 day for f00-f18 and 5 days for f19-f48 (20 synoptic runs), and reaches per-cycle p95s
  after about 20 days. Seeding it from `lag-survey` (which can only err late) would make that
  immediate; that is the lead's decision (PRD §8.3 says "live-measured").
