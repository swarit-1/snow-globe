# lookout-eval

Verification for Lookout (PRD §13): verification scores, the ephemeris wrapper, daylight, the frame quality
gates, extractor v−1, baselines, the committed held-out split and the leakage guard, and the backtest pair.
Python 3.13, uv.

```sh
cd eval && uv sync && uv run pytest -q && uv run ruff check .
uv run lookout-eval ephem-check            # oculus-core vs the NPS archive's SolarElevationAngle
uv run lookout-eval backtest-pair --help   # one v-1 pair: SceneSpec -> headless render -> extractor
uv run lookout-eval heldout                # the committed held-out split, with UTC instants
uv run lookout-eval heldout --check FRAME  # may this frame be used for development or fits? (metadata only)
```

The default suite also runs the renderer's own `validateScene()` (`renderer/src/scene.ts`) on eval-built
scenes under Node 24, without a browser; it skips with a reason when Node 24 is missing. The real headless
render is opt-in: `LOOKOUT_EVAL_RENDER=1 uv run pytest -q tests/test_renderer_contract.py`.

## Setup and the oculus-core pin

`uv sync` builds **oculus-core** from source with maturin into this venv. That is the default `oculus`
dependency group: crate `core/`, feature `python`, through `core/bindings/python/pyproject.toml`. It is
pinned to the public commit `1b5410b01f127514fa37656eff2b7d73be7ea8da` (`origin/main`; commit 1ba95d2),
which `uv.lock` and the installed `direct_url.json` name. The build needs a Rust toolchain and takes about
20 s on an M4. The earlier pin, `06809e6`, exists only on a local branch. Between the two commits the
`sun_moon` path, `core/bindings`, `core/Cargo.toml` and `core/Cargo.lock` are identical.

Without oculus-core, use `uv sync --no-group oculus` and set `LOOKOUT_EPHEMERIS=noaa` (Sun only); nothing
falls back silently. The suite passes in such a venv, with the oculus-only tests skipped.

The optional `forecast` group installs lookout-forecast. It is used only by
`backtest-pair --forecast auto`, which backfills one HRRR run from NOAA (network):
`uv sync --group forecast`. The M0.5 pair did not use it: it ran `python -m lookout_eval.pair_forecast`
inside workers/forecast's environment (which has ecCodes) on the frozen snapshot of committed code, then
passed the file with `--forecast` and the printed summary with `--forecast-summary`. Both paths choose and
extract the same way (`pair_forecast.py`). The commands are in
[docs/evidence/m05-backtest-pair.md](../docs/evidence/m05-backtest-pair.md).

## Modules (`src/lookout_eval/`)

| Module | PRD | What it does |
|---|---|---|
| `scores.py` | §13.4, §13.5, §13.7 | Contingency with excluded/partial masks; POD, POFD, PSS, FAR, CSI, bias (`None` when undefined); skill score from whole-period totals; Wilson interval; paired ΔPSS and single PSS with a day-block bootstrap; Holm–Bonferroni |
| `ephemeris.py` | §8.4 | oculus-core Sun/Moon (geometric) plus refraction (Sæmundsson, or NOAA's for comparison); explicit backend choice |
| `noaa_solar.py` | §8.4 | Independent NOAA GML solar algorithm (Meeus ch. 25/28): fallback and cross-check |
| `ephem_check.py` | §8.4 | NPS `SolarElevationAngle` cross-check (`lookout-eval ephem-check`) |
| `daylight.py` | §8.6 | Day from the ephemeris Sun altitude only; thresholds are parameters (P1 3°, cloud fraction 10°) |
| `gates.py` | §8.6 | The frame quality gates: Fresh, Daylight and Usable frame applied; Same view, Clear lens and the Colour-mode cut-off not yet (reported as "not checked"); exclusion mask and counts |
| `extractor.py` | §13.3 | v−1 landmark visibility from normalised Sobel energy against a Sun-binned clear-day reference; camera-in-cloud flag; camera-fault class (dark, saturated, blank) |
| `frames.py` | data layout | Frame records (with NPS's own archive times) and the `frames/` and `archive/` catalogue; never reads under `quarantine/` |
| `heldout.py`, `heldout_periods.json` | §13.2, §13.9 | The committed held-out split (D-019, D-022, D-024): periods per viewpoint and design, the archive guard, the quarantine, scoring exclusions; strict, versioned loader |
| `leakage.py` | §13.1, §13.9 | `FrameStore` with issue-time and fit views that raise `LeakageError`, under the committed split; no labels on held-out frames outside the sealed scoring; `frame_file_verdict` for one file |
| `baselines.py` | §13.6, §9.1 | Persistence (a), (b) and the dev-only choice; climatology; S_rule; analog (Tier 0) |
| `forecast_io.py` | §8.3 | Reads lookout-forecast run files: interpolation, availability, datums, winds, the summit ray |
| `scene.py` | §9.2, §9.5 | SceneSpec for renderer 0.2.0 (`run_init_utc`, plate id and hashes); schema plus cross-field checks; the renderer's own `validateScene()` under Node (`renderer_validate.mjs`) |
| `backtest_pair.py`, `cli.py` | §16 M0.5 | The v−1 pair and the `lookout-eval` console script. Record and scene under `scores/pairs/<viewpoint>/`, render under `renders/pairs/<viewpoint>/`; every input's hash, image time and availability; wall time per stage; `--tag` for repeat runs |
| `pair_forecast.py` | §8.3, §16 M0.5 | The pair's HRRR run: lookout-forecast's own `select_run_for_issue` and `LagModel`, then its `Extractor` for only the scene's fields and the two bracketing hours, under a 60 MB GRIB cap. A reused file must record the availability the choice gives. Runs inside lookout-forecast's environment (`python -m lookout_eval.pair_forecast`) |
| `landmark_boxes.py`, `landmark_boxes_v1.json` | §13.3, §16 M0.5 | By-eye landmark boxes in normalised image coordinates, drawn on the plate; they fit any resolution of the drawn frame's aspect ratio only; not in the viewpoint manifest; replaced by the M2 pose solve |
| `pair_score.py` | §13.4, §13.5 | One pair's P1 cell (event "visible"; partial masked; in-cloud excluded, hidden in the sensitivity) as a one-pair `Contingency`, plus the ratio error |
| `registration.py` | §8.6 | How far a frame's aim differs from the plate's (phase correlation of gradient images): a diagnostic in the pair record; the Same view gate itself stays "not checked" |

## Choices worth knowing

- **The held-out split** is committed in `src/lookout_eval/heldout_periods.json` (version 1). Every pair
  record names its version and SHA-256. Local days are in each viewpoint's manifest zone; a test checks the
  zones match.

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
    present but unreadable are refused. This is lookout-capture's `heldout.py` rule, reproduced (eval does
    not import capture). A test loads capture's own file and fuzzes the two rules: eval finds every date
    capture finds, so it is never looser.
  - **Quarantine (D-022):** anything under `$LOOKOUT_DATA_DIR/quarantine/` is refused on every path, sealed
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

## Measured (this machine, MacBook Air M4, 19 Nov 2026)

**Ephemeris vs NPS.** `lookout-eval ephem-check`, 88 `pore` assets of 17 Nov, Point Reyes camera position:
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
- Against lookout-capture's own rule, 12,000 fuzzed NPS string pairs (3 seeds): capture never refuses a
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
    `development_from` in the split (version 2, with a DECISIONS.md entry), applied in `FitView` and the
    pair's fitted-asset check (`TODO(M2)` in `leakage.FitView`).
- **M1.5:**
  - per-layer cloud bases from model levels instead of the pressure-band mapping in `scene.py`, and the
    `PRES:cloud base` cross-check before `HGT:cloud base` is used (the M0.5 pair's camera cell had a
    phantom 1.6–2.0 km base under a ~10 km one; harmless there only because low cover was 0 %);
  - an explicit hour list in lookout-forecast (`ExtractOptions.hours`), so `pair_forecast.py` can stop
    substituting `lookout_forecast.extract.run_hours` for one call;
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
  `backtest-pair --forecast auto` itself has still not reached NOAA (the eval venv has no ecCodes);
  `pair_forecast.py` did, from lookout-forecast's environment.
