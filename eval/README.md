# snow-globe-eval

Renamed from `lookout-eval` on 21 Nov 2026 (D-080); 0.2.0 is the same code under the Snow Globe names. It reads every file and log written under the old ones: `lookout.*` kinds, `lookout-capture/<version>` stamps (the data report compares releases, so the rename alone is never a restart), and the held-out split's version 1 and the landmark boxes, which keep their old `kind` because their SHA-256 values are on record (the split's current version 2 is a new file under the new kind). Measurements below dated before 21 Nov were taken under the old names.

Verification for Snow Globe (PRD §13): verification scores, the ephemeris wrapper, daylight, the frame quality
gates, extractor v−1, baselines, the committed held-out split and the leakage guard, and the backtest pair.
Python 3.13, uv.

```sh
cd eval && uv sync && uv run pytest -q && uv run ruff check .
uv run snow-globe-eval ephem-check            # oculus-core vs the NPS archive's SolarElevationAngle
uv run snow-globe-eval backtest-pair --help   # one v-1 pair: SceneSpec -> headless render -> extractor
uv run snow-globe-eval heldout                # the committed held-out split, with UTC instants
uv run snow-globe-eval heldout --check FRAME  # may this frame be used for development or fits? (metadata only)
uv run snow-globe-eval data-report            # the M0.5 gate's 72 h log evidence (offline, read-only); --json
uv run snow-globe-eval extractor-v0           # extractor v0 over every Rainier capture (plate v0.1) + validation on the hand labels
```

Outside pytest, iCloud's hidden `.pth` files mean the commands need `PYTHONPATH=src:../workers/common/src`
(CLAUDE.md "Python").

The default suite also runs the renderer's own `validateScene()` (`renderer/src/scene.ts`) on eval-built
scenes under Node 24, without a browser; it skips with a reason when Node 24 is missing. The real headless
render is opt-in: `SNOW_GLOBE_EVAL_RENDER=1 uv run pytest -q tests/test_renderer_contract.py`.

## Setup and the oculus-core pin

`uv sync` builds **oculus-core** from source with maturin into this venv. That is the default `oculus`
dependency group: crate `core/`, feature `python`, through `core/bindings/python/pyproject.toml`. It is
pinned to the public commit `1b5410b01f127514fa37656eff2b7d73be7ea8da` (`origin/main`; commit 1ba95d2),
which `uv.lock` and the installed `direct_url.json` name. The build needs a Rust toolchain and takes about
20 s on an M4. The earlier pin, `06809e6`, exists only on a local branch. Between the two commits the
`sun_moon` path, `core/bindings`, `core/Cargo.toml` and `core/Cargo.lock` are identical.

Records name the build that ran: the ephemeris version is the crate version plus the commit in the
installed `oculus-core-py`'s `direct_url.json` (`0.1.0+1b5410b`), or the pin marked `.pinned` when the
build has no such commit. Until 20 Nov the suffix was a constant that still said `06809e6`, so the M0.5
pair's four records say `0.1.0+06809e6` although they ran the `1b5410b` build
([docs/evidence/m05-backtest-pair.md](../docs/evidence/m05-backtest-pair.md) §9).

Without oculus-core, use `uv sync --no-group oculus` and set `SNOW_GLOBE_EPHEMERIS=noaa` (Sun only); nothing
falls back silently. The suite passes in such a venv, with the oculus-only tests skipped.

The optional `forecast` group installs snow-globe-forecast. It is used only by
`backtest-pair --forecast auto`, which backfills one HRRR run from NOAA (network):
`uv sync --group forecast`. The M0.5 pair did not use it: it ran `python -m snow_globe_eval.pair_forecast`
inside workers/forecast's environment (which has ecCodes) on the frozen snapshot of committed code, then
passed the file with `--forecast` and the printed summary with `--forecast-summary`. Both paths choose and
extract the same way (`pair_forecast.py`). The commands are in
[docs/evidence/m05-backtest-pair.md](../docs/evidence/m05-backtest-pair.md).

Since D-054 the choice lists each candidate run (one S3 listing request each, counted in the summary's
`choice_transfer`) and never dates an archived hour before its own trusted S3 stamps, as the extractor
does when it writes the file. That needs forecast 0.2.0 or later (then `lookout-forecast`, in the repo since 08a996c); the
running jobs' snapshot (034c46b) holds 0.1.0, which `pair_forecast` refuses. With the lag model of its
day the M0.5 pair's choice is unchanged by the floor (a test replays the two runs' recorded listings).
Today's lag model, with the survey lags of D-053, would choose another run for it (2024011418, its
f25/f26 modelled at 19:31:36Z, before the 19:45Z issue time), floor or no floor; the recorded pair keeps
the run it was made with.

## Modules (`src/snow_globe_eval/`)

| Module | PRD | What it does |
|---|---|---|
| `scores.py` | §13.4, §13.5, §13.7 | Contingency with excluded/partial masks; POD, POFD, PSS, FAR, CSI, bias (`None` when undefined); skill score from whole-period totals; Wilson interval; paired ΔPSS and single PSS with a day-block bootstrap; Holm–Bonferroni |
| `ephemeris.py` | §8.4 | oculus-core Sun/Moon (geometric) plus refraction (Sæmundsson, or NOAA's for comparison); explicit backend choice |
| `noaa_solar.py` | §8.4 | Independent NOAA GML solar algorithm (Meeus ch. 25/28): fallback and cross-check |
| `ephem_check.py` | §8.4 | NPS `SolarElevationAngle` cross-check (`snow-globe-eval ephem-check`) |
| `daylight.py` | §8.6 | Day from the ephemeris Sun altitude only; thresholds are parameters (P1 3°, cloud fraction 10°) |
| `gates.py` | §8.6 | The frame quality gates: Fresh, Daylight and Usable frame applied; Same view applied when the caller passes a registration shift against the plate and a tolerance (extractor v0 does), else "not checked"; Clear lens and the Colour-mode cut-off not yet (reported as "not checked"); exclusion mask and counts |
| `extractor.py` | §13.3 | v−1 landmark visibility from normalised Sobel energy against a Sun-binned clear-day reference; camera-in-cloud flag; camera-fault class (dark, saturated, blank) |
| `extractor_v0.py` | §13.3, §13.4 | v0: the same energy per **plate landmark band** (summit, upper, mid, near), against the median of the 3 clear-day frames nearest in Sun position (within 3° altitude and 15° azimuth, never from the scored frame's own local day); classes visible ≥ 0.60 > partial ≥ 0.35 > hidden (v−1's placeholder τ, fixed before any v0 ratio); night, camera fault, excluded (a §8.6 gate failed), in cloud, no reference; obscuration height. The plate's band, overlay and base PNGs are checked against `plate.json`, bands may not share a pixel, and each band's validity is carried |
| `extractor_v0_run.py` | §13.3, §13.8, §8.6 | `snow-globe-eval extractor-v0`: v0 over every development capture of a viewpoint against plate v0.1 by default, writing `results.jsonl`, `ratio_series.csv`, `summary.json` and `validation.json` under `<data dir>/scores/extractor-v0/<viewpoint>/<UTC stamp>/` (never over an earlier run) and a line in `runs.jsonl` beside them. The reference pool holds only clear-day frames a fit may use (the split's fit bound) that pass the gates; every frame gets Fresh, Daylight, Usable frame and Same view (registration against the plate's base within 4 px, v0 values), with exclusion and not-checked counts. Validation leaves the reference pool out of the judged sample (pool frames are scored against one another), reports the pool alone as a sanity check, judges F1 with label-partial frames left out and the extractor's partial counted as not visible, never meets the bar with thresholds given on the command line, and lists every §13.8 condition still missing. `summary.json` records the SHA-256 of the extractor's modules and the git state |
| `labels.py`, `labels_rainier_v0_1.json`, `labels_rainier_v0.json` | §13.8, §13.1 | Hand labels by viewing: Rainier's 105 daytime captures of 19–22 Nov 2026, per band visible/partial/hidden, camera in cloud, sky, light, clear-day reference; the protocol is in the file. v0 is the first pass (every band, before any v0 ratio); v0.1, the default, keeps its summit labels and labels upper, mid and near again inside the plate v0.1 band outlines (after a v0 run existed; each frame keeps its first-pass labels). A file labelling a held-out or quarantined frame is refused, by its time field and by the time in its key |
| `frames.py` | data layout | Frame records (with NPS's own archive times) and the `frames/` and `archive/` catalogue; never reads under `quarantine/` |
| `heldout.py`, `heldout_periods_v2.json`, `heldout_periods.json` | §13.2, §13.9 | The committed held-out split (D-019, D-022, D-024), version 2 current and version 1 kept byte for byte: periods per viewpoint and design, the archive guard, the quarantine, scoring exclusions; strict, versioned loader; `cited_split` resolves a record's citation of either version |
| `leakage.py` | §13.1, §13.9 | `FrameStore` with issue-time and fit views that raise `LeakageError`, under the committed split; no labels on held-out frames outside the sealed scoring; `frame_file_verdict` for one file |
| `baselines.py` | §13.6, §9.1 | Persistence (a), (b) and the dev-only choice; climatology; S_rule; analog (Tier 0) |
| `forecast_io.py` | §8.3 | Reads snow-globe-forecast run files: interpolation, availability, datums, winds, the summit ray |
| `scene.py` | §9.2, §9.5 | SceneSpec for renderer 0.2.0 (`run_init_utc`, plate id and hashes); schema plus cross-field checks; the renderer's own `validateScene()` under Node (`renderer_validate.mjs`) |
| `backtest_pair.py`, `cli.py` | §16 M0.5 | The v−1 pair and the `snow-globe-eval` console script. Record and scene under `scores/pairs/<viewpoint>/`, render under `renders/pairs/<viewpoint>/`; every input's hash, image time and availability; wall time per stage; `--tag` for repeat runs |
| `pair_forecast.py` | §8.3, §13.9, §16 M0.5 | The pair's HRRR run: snow-globe-forecast's own `select_run_for_issue`, `LagModel` and `listed_availability` (0.2.0 or later, D-054), so an archived hour is never chosen before its own S3 stamps; then its `Extractor` for only the scene's fields and the two bracketing hours, under a 60 MB GRIB cap. A reused file must record the availability the choice gives; a refusal says which side differs and suggests re-extracting only when that would help. Runs inside snow-globe-forecast's environment (`python -m snow_globe_eval.pair_forecast`) |
| `landmark_boxes.py`, `landmark_boxes_v1.json` | §13.3, §16 M0.5 | By-eye landmark boxes in normalised image coordinates, drawn on the plate; they fit any resolution of the drawn frame's aspect ratio only; not in the viewpoint manifest; replaced by the M2 pose solve |
| `pair_score.py` | §13.4, §13.5 | One pair's P1 cell (event "visible"; partial masked; in-cloud excluded, hidden in the sensitivity) as a one-pair `Contingency`, plus the ratio error |
| `registration.py` | §8.6 | How far a frame's aim differs from the plate's (phase correlation of gradient images): a diagnostic in the pair record; the Same view gate itself stays "not checked" |
| `data_report/` | §16 M0.5, §15.3 | `snow-globe-eval data-report`: the M0.5 gate's log evidence from `$SNOW_GLOBE_DATA_DIR`, read-only. Per viewpoint: polls, image GETs, frames and thumbnails by storage policy (checked against the disk), rate caps, errors, poll gaps send to send, the most GETs in a rolling hour, the bounds at nps.gov (the least poll gap and 5-GET span nps.gov can have seen, and the pairs and windows not provably 300 s or 3600 s apart), and the distinct live HRRR runs with their hours and fields; outages, restarts and their excess, exclusions and the 72 h clock by the gate rule. Never opens an image, `archive/` or `quarantine/` |

## Choices worth knowing

- **The held-out split** is committed in `src/snow_globe_eval/heldout_periods_v2.json` (version 2, SHA-256
  `82574ceb38ebed8ad6c5f61d02868244bf0673d8c900e0bbcc477143a95ae3a3`), which every new record cites by its
  version and SHA-256. Version 2 changes text only: its `archive_guard.date_rule` states the rule the
  guards apply (any local or UTC date on or after 1 Jun 2026, the standard-time reading included; D-047,
  D-070), its `kind` is the renamed one (D-080), and a `revision` note says what changed. Its periods,
  viewpoints, scoring exclusions, guard date and quarantine folder are version 1's, which a test checks.
  Version 1, `heldout_periods.json` (SHA-256 `c437544eecdd7a0d33b67c2374e1d388ebe45c36aeca4b2923b39c0d1d31de83`,
  D-038), stays byte for byte, old `kind` and all: the M0.5 backtest pair's records cite it.
  `committed_split(1)` loads it and `cited_split(citation)` resolves a record's `split` to either
  version (by version and SHA-256), for a reader of records: no code reads a record's citation back yet,
  so only its tests call it. `snow-globe-eval heldout` prints version 2 and version 1's identity.
  Local days are in each viewpoint's manifest zone; a test checks the zones match.

  | Viewpoint | Held out | Notes |
  |---|---|---|
  | Rainier | design P, the sealed window 15 Dec 2026 – 14 Jan 2027 | Extended by any outage; no extension yet. |
  | Point Reyes | the same sealed window (P), plus summer 2026, 1 Jun – 31 Aug (H, for K2) | |
  | Grand Canyon, Yosemite, Acadia, the Smokies, Big Bend | summer 2026 (H) | |
  | Grand Teton | nothing | No design (D-024): excluded from scored metrics; development use only. |

  - **Scoring exclusion:** 15 Jul 2026 at the 7 ARD cameras (D-022), as a mask (`scoring_excluded_mask`),
    and in each pair record (`leakage_checks.scoring_exclusion`, `extractor.eligible_for_metrics`). A pair
    record's `counts_toward_metrics` is also False while the extractor is unvalidated (PRD §13.8), so it is
    False for every v−1 pair; `not_counted_because` lists the reasons.
  - **Archive guard (D-019):** an archived frame dated 1 Jun 2026 or later is refused for development at
    every viewpoint. A frame counts as archived when its key is under `archive/`, its source is
    `nps-archive`, or its sidecar carries NPS's archive times. Live sidecars never do. The date is the
    latest of:
    - `ImageCreateDateTime`, `LocalTimeString`, and the civil date of the parsed UTC time (capture's rule);
    - two dates eval adds: `ImageCreateDateTime` read as local standard time, and the record's own UTC time.

    The standard-time reading follows NPS's summer convention. It decides the summer edge when
    `LocalTimeString` is missing: "05/31/2026 11:30:00 PM" is 00:30 PDT on 1 Jun. NPS strings that are
    present but unreadable are refused. This is snow-globe-capture's `heldout.py` rule, reproduced (eval does
    not import capture). A test loads capture's own file and fuzzes the two rules: eval finds every date
    capture finds, so it is never looser.
  - **Quarantine (D-022):** anything under `$SNOW_GLOBE_DATA_DIR/quarantine/` is refused on every path, sealed
    or not. That covers any folder of that name in any letter case, symlinks resolved, and the folder found
    by identity (device and inode), since this Mac's disk is case-insensitive. The rule applies to the store,
    the catalogue (`scan_frames` checks where each listed frame and sidecar resolves), sidecar reads, the
    pair's every input (the Fresh gate's previous capture included) and `heldout --check`. For frames on
    disk, give a `FrameStore` the reader `frames.storage_reader(storage)`: it checks again at every read, so
    a link made after the scan is still refused.
  - **Changing the split** bumps `version`, cites a new DECISIONS.md entry, and is committed before any frame
    it releases or newly holds out is viewed. `test_the_committed_periods_are_those_of_d019_d022_d024` pins
    the values, so a change shows up in the same commit.
- **Leakage.** A baseline issued at *t* sees only frames imaged *and* available (fetched) by *t*, on every
  access path. There is no exception. Held-out frames (a period, an extra period, or past the archive
  guard) are refused by issue-time views, fits and the labelling store. Only a store built with
  `SealedScoring(<40-hex pre-registration commit>)` may label or read them (M4). Fits never see them, sealed
  or not, nor any frame at or after the **fit bound**. That bound is the viewpoint's earliest held-out start,
  or a design's own period with `for_fit(design=...)`: design P at Point Reyes may fit on live frames from
  after summer 2026; the default may not. A store refuses quarantined records and viewpoints the split does
  not name (fail closed). `held_out=` extras can only add periods. A store and `frame_file_verdict` always
  use the committed split: there is no `split=` parameter, and `FrameStore.split` is read-only.
  `heldout --check` refuses a path that does not exist.
- **Persistence (b)** is "the frame at valid − 24 h" from frames available at the issue time: the search
  window stops at the issue time. When the lead exceeds 24 h, valid − 24 h is after the issue time, so (b)
  uses valid − 24·k h with k = ceil(lead / 24 h) and flags it (`valid-minus-48h`). This reading of §13.6
  is a decision for the owner; the alternative is no (b) forecast beyond 24 h.
- **Bootstrap.** Moving blocks of whole local days, wrapped (circular), by default. Reasons:
  - A fixed block length can be frozen in the pre-registration.
  - At equal mean block length its variance estimate beats the stationary bootstrap's (Lahiri 1999).
  - Wrapping keeps the end days from being under-sampled in a 31-day window.

  `method="stationary"` is there for sensitivity only. The interval comes from order statistics (with
  B resamples and k = ceil(B·α/2), the k-th smallest and k-th largest ΔPSS), and the p-value is
  2·min(P\*(Δ ≤ 0), P\*(Δ ≥ 0)) over the same resamples. So "p < α" holds exactly when the interval
  excludes 0; a property test checks it. At p = α exactly the interval does not exclude 0, while Holm
  rejects at adjusted p ≤ α. Day labels are local days: datetime64 labels must have the unit `D`.
- **Daylight** uses the geometric altitude unless asked otherwise. The pre-registration fixes which one P1 uses.
- **Climatology** is fitted per issue time on development frames available by that time. Hours are local
  standard time. The yes/no cut defaults to the fitted base rate.
- **Backtest pair.** Every frame rule is checked from metadata before any byte of a frame is read; a test
  spies on hashing and decoding to prove it. The rules:
  - No input may be quarantined: the frames, plate, masks and forecast file, and also the
    `--forecast-summary`, the `--boxes` file and the `--references-file` listing, each refused before it is
    opened.
  - The truth frame must not be held out under the committed split. Only `PairInputs.sealed` lifts that,
    for M4; the CLI has no such flag.
  - Clear-day references and the plate must be taken before the issue time and available by then, not on
    the valid time's local day, from the same viewpoint, not held out (sealed or not), and before the fit
    bound.

  Two consistency rules refuse a pair (`PairError`) before anything is written:
  - **One aspect ratio.** The plate and every reference must have the truth frame's aspect ratio (within
    1 %), and so must the frame the boxes were drawn on (`drawn_on.size`). A normalised box on a frame of
    another aspect covers other terrain: the ARD cameras went from 3:2 to 16:9 (D-024).
  - **The forecast summary describes this pair.** It must name the same forecast file and run, viewpoint,
    valid and issue times, bracketing hours, and the availability the file records for them.

  `--held-out FIRST:LAST` adds days to the split and never removes any. `--design P|H` picks the design
  whose period sets the fit bound (default: the strictest).

## The data report (PLAN.md M0.5 gate 1)

`snow-globe-eval data-report [--since ISO] [--until ISO] [--json [PATH]]` reads `capture_log/`,
`forecast_log/` (and `forecast_log/lag_survey/`), the sidecars under `frames/` and `thumbs/`,
`forecast/hrrr/` and `state/`, and writes nothing (only `--json PATH`, where you say). The window starts by
default at 12:24:23 UTC on 19 Nov 2026, when the loop took over all 8 cameras, and ends now. The rules,
in full in `data_report/__init__.py`:

- **Counted:** capture records of the scheduled runners (`cli-loop`, `lambda`); live forecast `run`
  records that were the first write of at least one viewpoint, `lag` records (each run and hour once) and
  `error` records. Per viewpoint, a run counts when that viewpoint's first write of it falls in the
  window; a later write is a re-extraction, never a run, even when the first write was before the window.
  Runs due before the window and first written in it are counted and listed apart as catch-ups.
- **Excluded, each with a reason and file:line examples:** records outside the window, archive events,
  other runners (`cli-once`, the retired stopgap), archived forecast runs (`"live": false`, such as the
  pair's run 2024011412 at line 475 of `forecast_log/2026-11-19.jsonl`), re-extractions, duplicate lags,
  survey lags, damaged lines, and forecast files on disk whose run was not first written in the window.
- **Capture outage:** more than 540 s between consecutive passes; it loses the gap minus one 300 s
  interval, as PLAN.md's 8.2 h for 19 Nov does. No scheduled pass in the logs loses the whole window.
- **Forecast outage:** the stand-in writes records only when it writes a run, about hourly, so a quiet
  gap between its records is not an outage (such gaps are listed for information only). Instead, each
  HRRR cycle is due when its extracted hours were all on S3 (`s3_lags_s`), and a run is late when its
  first write finished (`fetch_finished_utc`, D-027) more than 900 s after that. The stand-in was down
  from due + 300 s until it next finished writing a live run, any run (a fetch's start or an `error`
  record does not end that), when that is more than 900 s after the due time. A cycle never written gets
  an estimated due time from the median measured lag: of the window's complete runs, else of every
  complete run read, else D-027's 120 min. Cycles due up to 6 h before the window lose the part inside
  it, and no live run in the logs loses the whole window. With no per-tick record, these are lower bounds.
- **Restarts:** a `capture_version` or runner change, a `loop_stopped` or `loop_started` record between two
  passes, or two pass starts under 295 s apart (the 300 s interval less a 5 s tolerance); for the
  stand-in, a change of fields or hours. A pass starts at its `started_at` where capture logs it (0.3.0 on),
  else at t − `duration_s` (every record so far; `started_from` says which). A capture restart's
  **excess** is the longest any camera went from its last poll before the restart to its first poll after
  (send to send), less one 300 s interval, when positive. It is lost from that camera's last poll + 300 s
  to its next poll. A poll is before the restart when the old loop wrote it: at or before, in the logs,
  the last record before the new loop's `loop_started` record (exact while the wall clock runs forward: a
  loop writes it first, inside the NPS fetcher lock; records are ordered by `t`, then file and line, so a
  clock stepped back across the restart can misplace it), else the old loop's `loop_stopped` record, else
  its last record of the old version
  and runner before the new loop's first pass start (`split_by`). So a new loop that polls at once is
  split right, and so is one that sleeps in its first pass, always with 0.3.0's records and, before them,
  when a `loop_stopped` record or a version or runner change tells the loops apart. The 02:39 restart of
  20 Nov: polls 483.080–485.471 s apart, excess 185.471 s (Acadia).
- **A camera not polled since a restart** (the lead's ruling, D-078): no poll after the restart in the
  logs read. It is flagged on its row (`not_polled_since_restart`, and in the table) and listed in the
  restart's `not_polled_since` with its own gap to the window's end. What that gap counts depends on the
  latest capture loop in the logs read, the one that would poll it next (`capture.latest_loop`: its latest
  pass record's `outcomes`, which every release logs for each camera of the pass, or a newer
  `loop_started` record's `viewpoints`), which `in_latest_loop` gives:
  - **listed** (`left_out` false): the camera is waiting, not yet due (after an ordinary restart, every
    camera until the new loop's first polls) or in a back-off. Its gap so far counts in the excess, to
    the window's end (`ongoing`), so the excess only grows until the next poll is logged and the clock
    never shows met early. Leaving these out too would show 0 s of excess until the first polls: on the
    live logs cut at 19:58 and at 20:01 UTC on 20 Nov, between the 19:44 restart's first pass (19:56:14,
    every camera not yet due) and its first polls (20:01:20–20:01:36), the close would have been
    322.174 s early.
  - **not listed** (dropped), or no record lists the latest loop's cameras (`left_out` true): left out of
    the excess, so a dropped camera can never keep the clock from closing, through its own restart or an
    earlier one.

  Once the logs hold its next poll it counts like any camera, its whole gap included: a long back-off set
  before a restart is then in that restart's excess (D-069's rule as written), and so is the time a
  dropped camera was out, from the moment a later loop lists it again.
- **Polite collection at nps.gov** (PRD §15.3; the lead's rule of 20 Nov): nps.gov received a request no
  earlier than its send and no later than its answer. From the records' millisecond stamps, the earliest
  receipt is `sent_at` (capture 0.2.1), else t − `elapsed_ms` − 0.05 ms (`elapsed_ms` is rounded to
  0.1 ms; this assumes the Mac did not sleep mid-request), and none for a record with neither; the
  latest is `answer_bound_at` (capture 0.2.1's bound for a request that failed after it may have left,
  `answer_bound: failed_after_send`: 300 s after the failure), else `answered_at` (rounded up), else
  t + 1 ms (t is written after the answer, truncated), and none for a record that names an `answer_bound`
  with no `answer_bound_at`. So
  nps.gov saw two consecutive polls at least earliest(N+1) − latest(N) apart, and 5 consecutive image
  GETs over at least earliest(5th) − latest(1st). The report gives the shortest of each per camera and
  counts the **unproven** pairs and windows: not provably 300 s (3600 s) apart, because the bound is
  under the limit (`under_limit`) or a send or an answer cannot be placed (`unbounded`). Unproven is not shown
  closer. The send-to-send columns stay beside them, and the tables and `rules.bases` name each
  figure's basis. Bounds are shown rounded down to the millisecond.
- **The 72 h clock:** covered = elapsed − lost. The headline is the **gate rule**, the lead's decision of
  20 Nov that settles D-061: lost = the union of both jobs' outages and each capture restart's excess.
  Beside it, labelled: PLAN.md's capture-only figure (the capture outages alone) and each job's own
  (capture: outages and restart excess; forecast: outages). Report `schema_version` 3 (from 21 Nov: the
  D-078 flag with `in_latest_loop`, `left_out` and the section's `latest_loop`, the restart split fields
  and `answer_bound_at`; the figures of every record so far are those of version 2).
- **State files** (`state/capture/<id>.json`, the fetcher lock) are read as they are when the report runs
  (`state_now`), not as of `--until`. The home directory is printed as `~`.

## Measured (this machine, MacBook Air M4, 19 Nov 2026)

**Ephemeris vs NPS.** `snow-globe-eval ephem-check`, 88 `pore` assets of 17 Nov, Point Reyes camera position:
- With the Sun up (39 assets), NPS differs from our refracted altitude by at most 0.0026° (RMS 0.0015°).
  It differs from the geometric altitude by up to 0.4768° (RMS 0.1174°). **So NPS reports a refracted
  altitude.**
- Below the horizon, NPS keeps applying the Sæmundsson formula without any cutoff. Geometric altitude plus
  that formula reproduces all 88 values to 0.0035°; a fitted timestamp offset of −0.73 s leaves 0.0016°.
- oculus-core and the NOAA algorithm agree to 0.0080° on these assets, and to 0.0126–0.0130° in altitude
  (0.0112–0.0117° in azimuth × cos alt) over 2,000 random times and places per seed, three seeds
  (2020–2030, latitude −60° to 70°, Sun above −5°).

**Extractor.** 21 ms per 1920×1080 frame and 38 ms per 3840×2160 frame, from the JPEG file (decode
included), at the 960 px analysis width; 16 ms and 20 ms from a decoded image. The ratio of a JPEG to the
same image as PNG differs by at most 1.19 % (quality 50), 0.91 % (75), 0.55 % (90) and 0.52 % (95), over
15 procedural images.

**Bootstrap.**
- Coverage of nominal 95 % intervals on iid days: 196 of 200 trials.
- 10,000 resamples for 31 days × 8 pairs: 2.3 ms median (15 warm runs).
- The old interpolated interval disagreed with "p < 0.05" in 276 of 6,000 small-B cases; the
  order-statistic interval in none.

**Held-out split.**
- Applied to the real catalogue at 22:45 UTC on 19 Nov. Keys and sidecars only: nothing under
  `quarantine/` was listed, nothing was decoded.
  - 85 archive records, all dated on or before 15 Jan 2026; none held out, and every sidecar carries the
    NPS strings.
  - 105 live frames, none held out. Fits under the strictest bound may use the 32 at Rainier and the 10 at
    Grand Teton; design P adds the 11 at Point Reyes. The other five cameras' live frames are after their
    summer-2026 fit bound.
  - No listed frame or sidecar links into the quarantine. 0.08 s in all, with the per-file checks.

  The live count grows as capture runs: there were 81 live frames earlier the same day.
- Building a `FrameStore` of 100,000 archive records: 2.7 s (27 µs a record, median of 3), at a load
  average of about 5 from other jobs; the adversarial review measured 1.2 s at a lower load.
  - Most of it is the NPS time parsing (`strptime` and `parse_nps_archive_time`), done once per record
    and then cached.
  - The standard-time reading and the case-insensitive key check add 1.8 µs a record (9 %), measured
    A/B in one process.
  - Listing the 100,000 records: 24 ms (issue-time view) and 92 ms (fit view).
- `scan_frames`' per-file quarantine checks (file and sidecar, where they resolve) add 20 µs a frame
  (+31 %) on a synthetic catalogue of 20,000 frames: 1.74 s against 1.33 s, medians of 5.
- Against snow-globe-capture's own rule, 12,000 fuzzed NPS string pairs (3 seeds): capture never refuses a
  frame that eval allows. Eval alone refuses 138–159 per 4,000, all at the summer edge, through the
  standard-time reading.
- Mutation check: 47 of 48 deliberate breaks of the rules were caught by the suite. Each break was made in
  a scratch copy, one at a time.
  - The 48 are the build's 15, the adversarial review's 21, and 12 for the rules this fix added.
  - Those 12 cover case-insensitive names, folder identity, the resolved-path layer, links in the
    catalogue, the store reader, the standard-time reading, missing files, the previous capture's
    sidecar, sidecar roots and NPS times as an archive sign.
  - The survivor is equivalent: a store that skipped its unknown-viewpoint check still raises on the next
    line.

**Renderer contract.** The renderer's `validateScene()` under Node takes 56 ms per call (median, warm).
The real `pnpm render` of an eval-built scene on the synthetic plate: 573–669 ms render, 0.75–1.19 s in
all (renderer timing, three runs, SwiftShader).

## Not done (TODO)

- **M4:**
  - the sealed scoring itself (`SealedScoring`), and extractor validation against labels (§13.8);
  - `SealedScoring` should check its commit against the pre-registration hash DECISIONS.md records (today
    it checks the form only: 40 lower-case hex characters);
  - the camera-change exclusion rule of the pre-registration (D-024) is not in the split.
- **M2:**
  - fit τ, the partial band, the in-cloud and camera-fault thresholds, the analog weights and the clear-air
    haze on development data;
  - the camera pose;
  - choose the block length from development autocorrelation, and the minimum detectable effect;
  - quality gates: Same view (registration against the plate), Clear lens (a sharpness floor, reported with
    and without iced-lens exclusions) and the Colour-mode cut-off (calibrated on development day and night
    frames; the two local night samples measure `chroma_spread` 0.00 and 0.01);
  - provenance checks for the sky mask and depth image, from the plate builder;
  - fits within one camera epoch (PRD §13.9). D-024's usable span per design-H camera is only a note in
    the split, so `FitView` does not filter by it, and a design-H fit can mix epochs. For example, Point
    Reyes's 2021–22 3:2 frames are a different view. This is not leakage. The fix is a per-viewpoint
    `development_from` in a new version of the split (version 3, since version 2 is the text-only
    correction; with a DECISIONS.md entry), applied in `FitView` and the pair's fitted-asset check
    (`TODO(M2)` in `leakage.FitView`).
- **M1.5:**
  - per-layer cloud bases from model levels instead of the pressure-band mapping in `scene.py`, and the
    `PRES:cloud base` cross-check before `HGT:cloud base` is used (the M0.5 pair's camera cell had a
    phantom 1.6–2.0 km base under a ~10 km one; harmless there only because low cover was 0 %);
  - `pair_forecast.py` should use the forecast worker's explicit hour list (since 0.2.0) (`ExtractOptions.hours`,
    `plan_run(hours=)`, D-053) and stop substituting `snow_globe_forecast.extract.run_hours` for one call;
  - `pair_forecast.py` should let the extractor write to a scratch storage and copy only the forecast
    file into `forecast/hrrr/`: today an archived extraction also adds a `run` event to the live
    `forecast_log/` and a state marker (the M0.5 pair's are line 475 of `forecast_log/2026-11-19.jsonl`
    and `state/forecast/hrrr/2024011412z.json`).
- **M2:** the pose solve replaces `landmark_boxes_v1.json`; the Same view tolerance turns the registration
  diagnostic into a gate; the clear-day reference needs a diffuse-light (bright-overcast) state as well as
  the Sun bin. The M0.5 pair read a visible but flat-lit far rim as "hidden" (ratio 0.33) because its only
  reference was a sunny frame.
- **Ran for real:** the M0.5 pair (Grand Canyon, 15 Jan 2024 19:45Z, 24 h lead) on 19 Nov 2026, three
  times, and a fourth time on 20 Nov (00:16 UTC) after the review fixes, with the same scene, render and
  scores; see [docs/evidence/m05-backtest-pair.md](../docs/evidence/m05-backtest-pair.md).
  `backtest-pair --forecast auto` itself has still not reached NOAA: it needs `uv sync --group forecast`
  (the dev group now brings ecCodes, at the forecast worker's locked versions, so that
  `tests/test_pair_forecast.py` runs the real extractor offline in `make test`; snow-globe-forecast itself
  comes only with the `forecast` group). `pair_forecast.py` did reach NOAA, from snow-globe-forecast's
  environment.
