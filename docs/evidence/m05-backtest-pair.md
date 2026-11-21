# M0.5 backtest pair, v−1: Grand Canyon, 15 Jan 2024, 24 h lead

> **Rename note, 21 Nov 2026:** the product is now Snow Globe (D-080). This file records runs of 19–20 Nov, so its commands, modules, paths and variables keep the names those runs used. Their `lookout-*`, `lookout_*` and `LOOKOUT_*` are today's `snow-globe-*`, `snow_globe_*` and `SNOW_GLOBE_*`, and the two hashed `eval/src/lookout_eval/` files now sit, byte for byte, in `eval/src/snow_globe_eval/`.

**Gate item** (PRD §16 M0.5, PLAN.md M0.5): "One backtest pair end to end: an archived frame plus an archived HRRR run, then a crude headless render, then a crude extractor score, labelled v−1."

**Measured** on 19 Nov 2026 (day G), 23:13–23:35 UTC, on the MacBook Air M4 (macOS 26.5.1), and run a fourth time at 00:16 UTC on 20 Nov on the code after the adversarial review's fixes (section 14). The pair is labelled **v−1** in its record. It is a test of the pipeline, not a forecast result: one pair carries no skill information, and both extractor calls are biased by known v−1 artefacts (below). No v−1 pair counts toward a metric (the record says so since run 4).

## 1. Result

| Item | Value |
|---|---|
| Status | `scored`, label `v-1`, pair id `20240115T194500Z_L24h_7838502574` |
| Valid time / issue time | 2024-01-15T19:45Z (12:45 MST) / 2024-01-14T19:45Z (valid − 24 h) |
| Forecast | HRRR run 2024011412, f31 and f32 (weight 0.75 on f32), modelled available 2024-01-14T14:00Z |
| Primary box `north-rim-cliffs` | truth ratio **0.330 → hidden**; render ratio **0.504 → partial** |
| P1 cell (event "visible") | **correct negative**: the render does not forecast "visible", and the truth is not "visible" |
| Second box `mid-canyon` | truth 0.407 → partial (masked out of P1); render 0.756 → visible |
| Determinism | runs 2, 3 and 4 gave a byte-identical scene and render PNG; every record field agrees except times, tag-bearing paths and, in run 4, the fields the review fixes added |
| Wall time | 2.76 s, 2.54 s and 2.61 s per pair, runs 1, 2 and 4 (render stage 2.31 s, 2.07 s and 2.15 s, of which SwiftShader drew for 1.08 s, 1.07 s and 1.05 s); the forecast extraction, run once, took 3.55 s in the extractor for 35.3 MB of GRIB |

**Read the cell with care.** By eye the far (North) rim is plainly visible in the truth frame: under thin high overcast and flat light, with lower contrast than on the sunny reference day. The v−1 extractor reads that loss of shadow edges as "hidden". The render loses edge energy for a different reason: the renderer adds haze over its nominal 15 km ray length on top of the plate's own haze. Both errors push towards "not visible", so the cell is a correct negative by accident. A human label would make it a miss. This is the bias PRD §13.8's extractor validation exists to catch.

## 2. What was paired, and why

**Frames on disk.** Before the guard date (1 Jun 2026, D-019) and outside `quarantine/`, the NPS archive copies hold 9 hires frames across 7 cameras. Only Grand Canyon has two hires frames in one camera epoch:

| Viewpoint | Hires frames on disk | Usable as one pair? |
|---|---|---|
| `grand-canyon.yavapai` | 2021-01-15 19:45Z and 2024-01-15 19:45Z, both 999×666 (3:2 epoch) | **yes** |
| `point-reyes.beach` | 2021-01-15 (999×666, 3:2) and 2026-01-15 (999×562, 16:9) | no: two epochs (D-024) |
| the other five ARD cameras | one hires each | no plate in the same epoch |
| `rainier.paradise-mountain` | none: the park camera has no archive (D-019) | no |

**The pair.**
- **Truth (target):** `archive/grand-canyon.yavapai/2024-01-15/20240115T194500Z_e224e6bb.jpg`, 999×666, NPS image time 2024-01-15T19:45Z.
- **Plate and the only clear-day reference:** `archive/grand-canyon.yavapai/2021-01-15/20210115T194500Z_57fb3725.jpg`, 999×666, NPS image time 2021-01-15T19:45Z.
  - It is before the issue time and not on the valid time's local day.
  - The date and clock time match the truth's, so the Sun is in the same extractor bin: altitude 32.94° vs 32.80°, azimuth 181.93° vs 181.99° (bin 30–35°, 180–200°).

**Same epoch, slightly different aim.**
- **Frame size:** both 999×666, the 3:2 epoch that D-024 lets Grand Canyon use for development and backtests.
- **Contact sheets** ([docs/research/nps-archive-check.md](../research/nps-archive-check.md)): "Same framing 2021-01 → 2024-01".
- **dHash from the sidecars** (the task's check): the two thumbnails are 23 of 64 bits apart (`814b02024a0d81e0` vs `f9f9020d2687d170`). Within the 3:2 epoch, the other thumbnails are 10–31 bits from 2021-01 and 12–28 bits from 2024-01. The four 16:9 thumbnails (2024-07, 2025-01, 2025-07 and 2026-01, 100×56 in their sidecars) are 31–35 bits from 2024-01 and 19–36 bits from 2021-01. A whole-frame dHash moves with sky and light, so 23 bits neither confirms nor rules out one framing, as the archive check found.
- **Full-frame registration** (`lookout_eval.registration`, recorded in the pair's `diagnostics`):
  - same scale (best 0.995 over 0.98–1.02; a separate fine search put 1.000 within 0.004 NCC of it);
  - the truth's content sits **51 px right and 30 px up** of the plate's: 5.1 % of the width and 4.5 % of the height;
  - gradient-image NCC is 0.45 aligned against −0.01 unaligned, over the boxes' window (x 0.06–0.90, y 0.37–0.56).
  - Over 5 windows × 3 analysis widths (480, 720 and 960 px; the windows are listed in section 7) the shift is +48.9 to +52.0 px and −29.1 to −30.5 px. The lowest x shift, +48.9 px, comes from the window (0.10, 0.45, 0.90, 0.90) at 960 px with scale 0.980, the edge of the scale search.

  So it is one camera, re-aimed slightly between the two dates: one epoch, not quite one framing. The pair goes ahead as v−1, with boxes that stay on the same terrain under that shift (section 4). The record measures how much the shift matters. A same-aim plate is the first thing to fix for the next pair (section 12).

  This contradicts a factual premise of D-024 ("one framing" in Grand Canyon's 3:2 epoch, Dec 2020 – Jan 2024) and of the archive check ("Same framing 2021-01 → 2024-01"), which came from contact sheets and dHash. Inside that epoch, a full-frame registration against the plate is needed before references, τ or boxes are fitted across dates. The correction to D-024 and the archive check is for the docs agent (section 12); this document does not change them.

**What the frames show** (looked at by eye on 19 Nov 2026):
- **Plate, 15 Jan 2021, 12:45 MST.**
  - Deep blue sky with a few thin cirrus streaks and no cloud over the canyon.
  - Strong direct sun from the south: crisp, dark shadows on the buttes.
  - Very clear air: the North Rim skyline is sharp, and a distant peak shows on the far-left horizon. No snow in view.
- **Truth, 15 Jan 2024, 12:45 MST.**
  - The sky is almost covered by a thin, whitish high cloud layer (cirrostratus, with streaks), with blue showing only near the right-hand horizon.
  - The sunlight is diffuse: shadows are weak, and the whole scene has a milky, low-contrast veil.
  - Visibility is still good: the North Rim and the far-left peak are visible, only softer. Snow lies on the near slopes at lower left.
  - No low cloud and no fog anywhere in the canyon.

## 3. The forecast run

**Choice.** lookout-forecast's own `select_run_for_issue` and `LagModel.from_storage`, from the frozen snapshot of committed code (`~/lookout-data/cache/runtime/cfefa79`). Every hour of an archived run is modelled; the lag model read the live lag log, which held 451 distinct (run, hour) lags, all logged on 19 Nov.
- **2024011418 skipped:** f25/f26 modelled available at 20:00Z, after the 19:45Z issue time.
- **2024011412 chosen:** f31 and f32, modelled available at **2024-01-14T14:00Z**.
- **Rule applied:** `conservative-fallback`. No (12Z, f31/f32) lag is logged yet, and only 3 are logged for those hours over all cycles (20 are needed), so availability is run start + 120 min. On S3, f31 and f32 were complete (the later of each GRIB file and its `.idx`) at 13:28:11Z and 13:29:37Z: the model is 30–32 minutes later than the real publication, never earlier.
- **Lead:** 24 h nominal; 29.75 h from availability; 31.75 h from run start (24 h leads come only from the 00/06/12/18Z runs).

**Extraction.** `python -m lookout_eval.pair_forecast` ran inside workers/forecast's environment (ecCodes 2.50.0) with the snapshot's `lookout_forecast` and `lookout_common` first on `PYTHONPATH`.
- **Fields:** `VIS:surface`, `HGT:cloud ceiling`, `HGT:cloud base`, `PRES:cloud base`, `HGT:cloud top`, `LCDC`, `MCDC`, `HCDC`, `PRATE:surface`, and `UGRD`/`VGRD` at 10 m. The model terrain is fetched once, with f31. This is everything the v−1 scene and S_rule read, plus the cloud-base cross-check.
- **Hours:** only f31 and f32. The worker's CLI (`lookout-forecast run --max-fh 32`) always extracts f00–f32. At the 16.5 MB an hour these fields cost on this run, that is about 545 MB, far past the task's 60 MB. So `pair_forecast` runs the committed `Extractor` with the hour list substituted for one call, and checks that the hours extracted are the hours asked for. If that substitution broke, the planner and the extractor would see every hour of the run, f00–f48 for a 12Z run (`run_hours(run, None)`; about 808 MB at 16.5 MB an hour). The planned-hours check would then refuse the run, and the 60 MB cap would too, before any GRIB byte was fetched; a test shows the first and the planned size. TODO(M1.5): an explicit hour list in lookout-forecast.
- **Transfer:**
  - GRIB: 35,258,352 bytes in 13 ranged GETs (f31: 18,811,836, including 2,153,695 of terrain; f32: 16,446,516);
  - other: 1 listing and 2 `.idx` files (45,515 bytes) for the extractor, and the same again for the plan that checked the 60 MB cap first; 0 retries;
  - wall time: the extractor took 3.55 s (GRIB download 5.29 s summed over its 2 threads; decoding 0.55 s). The saved summary records 0.002 s to choose the run and 4.005 s to plan and extract. The process's own wall time was not saved, so none is claimed.
- **Clean-up:** the GRIB subsets were deleted (`cache/hrrr/` is empty).
- **Written:**
  - `forecast/hrrr/grand-canyon.yavapai/2024011412z.json`;
  - the extractor's usual side effects: `state/forecast/hrrr/2024011412z.json` and one `run` event (archived, `"live": false`, no `lag` events) in `forecast_log/2026-11-19.jsonl`;
  - the printed summary, saved as `scores/pairs/grand-canyon.yavapai/20240115T194500Z_L24h.pair-forecast.json`.
- **Availability, re-checked.** The file records f31 and f32 as available at 14:00Z, the same as the summary's choice. Since the review, `pair_forecast` refuses to reuse a file whose recorded availability differs from the choice's, and the pair refuses a summary that disagrees with the file (section 14). On 20 Nov, just before run 4, the choice was made again offline, with the snapshot's `select_run_for_issue` and the live lag log, which had grown since. It gave the same run, hours and 14:00Z (still `conservative-fallback`, 3 pooled lags).

**What HRRR forecast, 31.75 h ahead**, at the valid time (linear between f31 and f32):
- **At the camera's grid cell:**
  - visibility 67.8 km;
  - ceiling 10,495 m MSL;
  - cloud cover: low 0 %, mid 17 %, high 87 %;
  - no precipitation; 10 m wind components +6.95 m/s eastward and −3.55 m/s northward (a north-westerly).
- **Along the view ray to 45 km:**
  - cloud cover: low 0 % everywhere, mid 5–99 %, high 76–100 %;
  - visibility 41–76 km.
- **In short:** high overcast, some mid cloud, no low cloud. That matches what the truth frame shows.
- **Two crude points of the v−1 scene:**
  - **Phantom cloud base.** `HGT:cloud base` at the camera cell is 1,680 m MSL, while `PRES:cloud base` says 262–279 hPa, about 10 km. This is the known phantom low base under high-only cloud (lookout-forecast's datum evidence). The v−1 scene builder uses `HGT:cloud base` without the `PRES` check, so its "low" layer starts at 1,680 m. That is harmless here only because low cover is 0 % everywhere (TODO(M1.5)).
  - **Camera cell.** The model terrain of the camera's 3 km cell is 1,230 m: the cell lies over the canyon, while the camera stands on the rim at 2,150 m.

## 4. The v−1 landmark boxes

`eval/src/lookout_eval/landmark_boxes_v1.json` (SHA-256 `697bb3c2f44230571ead4fade966edc60a07af7a3d06d2e0a949e7f36b820330`):
- drawn by eye on the plate frame, in normalised image coordinates (0–1, origin top left);
- not in the viewpoint manifest, which stays free of landmarks until M2;
- the file says the M2 pose solve (PRD §8.5) replaces them.

| Box | x0, y0, x1, y1 | What it covers |
|---|---|---|
| `north-rim-cliffs` (primary) | 0.06, 0.37, 0.44, 0.42 | the far rim's upper cliff band, left half, just below its skyline; not surveyed; the canyon is roughly 15–20 km rim to rim here |
| `mid-canyon` | 0.30, 0.47, 0.90, 0.56 | buttes and temples inside the canyon, nearer than the far rim; not surveyed |

- **Robust to the measured shift.** Each box lies inside one band of terrain with no sky, so a 5 % shift leaves it on the same kind of terrain. The far-rim skyline (sky above rock) is deliberately not boxed: a 4.5 % vertical shift changes how much sky such a box holds, and with it the edge energy (on one band at y 0.30–0.35 the unshifted truth-to-plate ratio is 7.3).
- **Disclosure.** I looked at the truth frame (the task asks for a description) before fixing the boxes. The boxes were placed for robustness to the shift, which is geometry, not the truth's weather, and the truth frame is a development frame, not held out. In M2 the boxes come from the pose solve, without anyone looking at scored frames.
- **What was measured on the truth before the boxes were fixed** (added after the review, which asked):
  - The by-eye look above, and the full-frame registration of the truth against the plate (section 2): geometry.
  - The skyline argument above cites a truth-to-plate edge ratio, 7.3 on the band y 0.30–0.35. That number reads the truth frame. The files on disk do not show whether it was computed before the boxes file was last written (19 Nov, 23:14:43 UTC). The band table in section 7, both its first version and the recomputed one, came after the pair.
  - The boxes' own truth ratios (0.330 and 0.407) first appear on disk in the run-1 record (23:25 UTC).
  - No other box set was scored in any record: all four records carry the same boxes file (SHA-256 `697bb3c2…`), and there is no other pair record on disk.
  - The primary box's truth ratio, 0.330, sits 0.02 under τ_lo, and it decides between a cell (hidden, a correct negative) and the partial mask (the moved box reads 0.360). So the cell must not be read as evidence of anything (section 1).
- **Rule for the next pairs:** fix the boxes from the plate and the registration only, before computing anything on the truth frame (section 12).

## 5. Commands

Run from the repo root on 19 Nov 2026. `RT=~/lookout-data/cache/runtime/current` (→ `cfefa79`); `D=~/lookout-data`.

```sh
# Step 2: choose and extract the run (NOAA's open bucket only). --plan-only first read the two .idx files.
cd workers/forecast
PYTHONPATH=$RT/workers/forecast/src:$RT/workers/common/src:$PWD/../../eval/src \
LOOKOUT_MANIFEST_DIR=$RT/viewpoints/manifests \
uv run --no-sync python -m lookout_eval.pair_forecast \
  --viewpoint grand-canyon.yavapai --valid 2024-01-15T19:45:00Z --lead 24 \
  > $D/scores/pairs/grand-canyon.yavapai/20240115T194500Z_L24h.pair-forecast.json

# Steps 4-5: the pair (tags run1 to run4). No network.
cd ../../eval
PYTHONPATH=src:../workers/common/src uv run --no-sync lookout-eval backtest-pair \
  --viewpoint grand-canyon.yavapai --valid 2024-01-15T19:45:00Z --lead 24 \
  --truth $D/archive/grand-canyon.yavapai/2024-01-15/20240115T194500Z_e224e6bb.jpg \
  --forecast $D/forecast/hrrr/grand-canyon.yavapai/2024011412z.json \
  --forecast-summary $D/scores/pairs/grand-canyon.yavapai/20240115T194500Z_L24h.pair-forecast.json \
  --reference $D/archive/grand-canyon.yavapai/2021-01-15/20210115T194500Z_57fb3725.jpg \
  --plate $D/archive/grand-canyon.yavapai/2021-01-15/20210115T194500Z_57fb3725.jpg \
  --boxes v-1 --design H --tag run1          # then the same with --tag run2, run3 and run4
```

Run 4 (20 Nov, 00:16 UTC) was the same command with `--tag run4`, on the code after the review fixes (section 14), timed with `/usr/bin/time -p`.

- The summary was first printed to a scratch file and then copied byte for byte; its SHA-256 is in the record.
- `PYTHONPATH` works around iCloud hiding the venvs' `.pth` files (CLAUDE.md).
- `--design H`: Grand Canyon is design H (D-024), so the fit bound is the start of its summer-2026 held-out season.
- Everything else is the eval default: extractor v−1 thresholds (τ_hi 0.6, τ_lo 0.35), the nominal pose (heading 315° from the manifest, pitch 0°, horizontal FOV 60°), haze 0.03 km⁻¹, no sky mask, no depth image, and SwiftShader.

The renderer step inside the pair is `pnpm --dir renderer render --scene <scores/.../scene.json> --plate <plate> --out <renders/.../render.png> --meta <renders/.../render.json> --backend swiftshader`. It uses renderer 0.2.0 on Playwright's headless Chromium 158.0.8127.18, with GL "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (LLVM 10.0.0)))".

## 6. Wall time per stage

Seconds, from the record's `timings_s` (`time.perf_counter`). The CLI processes took 3.24 s, 2.86 s and 2.97 s in all, runs 1, 2 and 4 (`/usr/bin/time`).

| Stage | Run 1 | Run 2 | Run 4 (after the fixes) |
|---|---|---|---|
| Leakage checks (metadata only: split, quarantine, truth, references, plate) | 0.0019 | 0.0015 | 0.0019 |
| Load and check the forecast file (and, run 4, its summary against the pair) | 0.0005 | 0.0005 | 0.0005 |
| Hash the inputs (and, run 4, read the frame sizes for the aspect check) | 0.0016 | 0.0016 | 0.0018 |
| Build and write the SceneSpec | 0.0005 | 0.0006 | 0.0005 |
| Schema check (`renderer/scene.schema.json`) | 0.0013 | 0.0013 | 0.0013 |
| Renderer's own `validateScene()` under Node | 0.0672 | 0.0670 | 0.0679 |
| Render: `pnpm render` process | 2.3057 | 2.0743 | 2.1512 |
| … of which: browser launch / draw / encode (renderer `timing_ms`) | 0.654 / 1.076 / 0.037 | 0.459 / 1.072 / 0.037 | 0.508 / 1.055 / 0.037 |
| Clear-day reference, both boxes | 0.0470 | 0.0520 | 0.0496 |
| Extractor on the truth, both boxes | 0.0397 | 0.0419 | 0.0398 |
| Extractor on the render, both boxes | 0.0504 | 0.0577 | 0.0498 |
| Quality gates | 0.0003 | 0.0004 | 0.0004 |
| Score | 0.0001 | 0.0001 | 0.0001 |
| Registration diagnostic | 0.1569 | 0.1580 | 0.1567 |
| Shift-aligned truth diagnostic | 0.0383 | 0.0383 | 0.0379 |
| **Total** | **2.7599** | **2.5421** | **2.6130** |

The forecast step ran once, before the pair. Its summary records 0.002 s to choose the run and 4.005 s to plan and extract; the process's own wall time was not saved.

## 7. Scores

**Extractor v−1 per box.** Ratio = edge energy in the box ÷ the clear-day reference's (the plate). Classes: visible ≥ 0.6, hidden < 0.35, partial in between.

| Box | Truth ratio → class | Render ratio → class | P1 cell | Truth with the box moved by the measured shift |
|---|---|---|---|---|
| `north-rim-cliffs` (primary) | 0.3303 → hidden | 0.5035 → partial | correct negative | 0.3604 → partial |
| `mid-canyon` | 0.4070 → partial | 0.7559 → visible | none (partial truth is masked) | 0.3625 → partial |

- **Primary score:** render ratio − truth ratio = +0.173; log ratio = +0.422.
- **The one-pair table:** H 0, M 0, F 0, C 1. PSS, POD, FAR, CSI and bias are undefined, with the reasons in the record: one pair never has both events and non-events.
- **Sensitivity table** (in-cloud truth counted as hidden): also a correct negative. Neither frame is in cloud (whole-frame energy ratios 0.486 for the truth and 0.856 for the render).
- **Quality gates on the truth:** Daylight passed (Sun 32.80°) and Usable frame passed. Fresh was not checked (no previous archive capture on disk). Same view, Clear lens and Colour mode are not implemented yet, so they are "not checked".
- **The shift matters at the threshold.** Moving the primary box by the measured shift puts it on the same terrain as on the plate. Its ratio goes from 0.330 (hidden) to 0.360 (partial), across τ_lo = 0.35. The v−1 class of this box is not robust to the camera's re-aim.

**Where the edge energy goes** (a diagnostic, not in the record). Edge-energy ratio against the plate, in horizontal bands across x 0.30–0.95, on the truth, the run-1 render and the plate. The first table (computed after the pair on 19 Nov) could be reproduced only to within ±0.016 by the review, and its band edges were not recorded. So the table below was recomputed on 20 Nov with this exact method, using eval's own functions:
- each band is `Region.box(0.30, y, 0.95, y + 0.05, frame_size=(1, 1))` for y = 0.30, 0.35, …, 0.90, scored with `edge_stats` and the default `ExtractorParams` (960 px analysis width);
- ratio = the band's `region_energy` in the frame ÷ the same band's in the plate;
- "moved" moves the band on the truth by the record's shift (`dx_norm` 0.051, `dy_norm` −0.0453) with `backtest_pair._translated`, as the record's aligned-truth diagnostic does, and divides by the plate's unmoved band.

| Rows (y) | Render ÷ plate | Truth ÷ plate (unmoved) | Truth ÷ plate (moved by the shift) |
|---|---|---|---|
| 0.30–0.35 | 0.570 | 7.284 (truth skyline vs plate sky) | 0.640 |
| 0.35–0.40 | 0.603 | 0.580 | 0.619 |
| 0.40–0.45 | 0.522 | 0.449 | 0.420 |
| 0.45–0.50 | 0.401 | 0.350 | 0.361 |
| 0.50–0.55 | 0.947 | 0.405 | 0.351 |
| 0.55–0.60 | 0.984 | 0.401 | 0.383 |
| 0.60–0.65 | 0.989 | 0.338 | 0.344 |
| 0.65–0.70 | 0.996 | 0.328 | 0.378 |
| 0.70–0.75 | 0.999 | 0.497 | 0.405 |
| 0.75–0.80 | 0.998 | 0.401 | 0.397 |
| 0.80–0.85 | 0.999 | 0.452 | 0.413 |
| 0.85–0.90 | 1.000 | 0.576 | 0.425 |
| 0.90–0.95 | 0.996 | 0.347 | 0.373 |

The same script measured the registration spread quoted in section 2, with `estimate_shift(plate, truth, window=w, analysis_width=n)` for n = 480, 720 and 960 and five windows w: (0.06, 0.37, 0.90, 0.56), the boxes' window as in the record; (0.05, 0.35, 0.95, 0.60); (0.30, 0.40, 0.95, 0.95); (0.33, 0.36, 0.98, 0.95); and (0.10, 0.45, 0.90, 0.90).

- **The truth** keeps only 0.33–0.58 of the plate's edge energy in every terrain band (0.34–0.64 with the bands moved by the shift), from the far rim down to the nearest walls about 1 km away. A loss that does not grow with distance is the light (diffuse sun, no cast shadows), not air between the camera and the landmark. The v−1 reference is conditioned on Sun position only, so it cannot tell flat light from obscuration.
- **The render** keeps the plate's texture below the image's middle row (0.95–1.00) but loses 40–60 % above it. That step is geometry:
  - With pitch 0° and no depth image, rays above the centre row end 15 km away (the renderer's nominal depth), so they collect 15 km of added haze (T ≈ 0.64).
  - Rays below it hit the renderer's flat ground, 10 m below the camera: 5.7 km away at 0.1° down, 570 m at 1° down, closer still further down. They collect almost no haze.
  - In the real frame, the far rim's skyline is near y ≈ 0.35, not 0.5.

## 8. Determinism

| File | Run 1 SHA-256 | Run 2 |
|---|---|---|
| `scene.json` | `89d6701bafd8344c624e27e5deba0907917d728189f7808d1d92bb42aa511b21` | identical |
| `render.png` (999×666, 703,457 bytes) | `41849cee0e0047b1b88bb07739524341ddbc633ea18c36bf607701361530ff83` | identical |
| renderer `scene_hash` | `dd95c01c632461d408d257ff9f017785a50721014956148caa0f107e612ec03f` | identical |
| pair record (`.run1.json` / `.run2.json`) | `304b1266f93261bb0218752450a0147286d453e6bb0930e2659acc99b68a9bff` | `62f2a7628683745ace290f590163ebf17c1ea1eba503b498db651283a314695a` |
| `render.json` | `4262e2b8ee7a98070e92aa7c143c920ef171df4296731d6977368dc89d8f7287` | `a9e51a90368ca5252e3879e7d36618f23ad01d6589d57b989bdff05167ff45f8` |
| `renderer.log` | `90bed3bc319d79f6975495709d75861d68dfe9fe46904f9ff4e6e2c9c674a26c` | `ca569f8ef2379f4b74984cb7f1f2a4bcc0bd914520b26f1e467ce3862074da7d` |

**Every difference, listed.**
- **Record:** of 1,096 leaf fields, 35 differ. They are:
  - `created_utc`, `tag`, `output_name`;
  - the output paths that carry the tag (record, scene and render paths, the render command, `stderr_tail`);
  - the renderer's `created_utc` and `timing_ms`, `render.wall_s`, and `timings_s`.
- **Identical between the runs:** every extractor output (energies, ratios, classes, reference lookups), the score, the registration, the gates, the leakage checks and the scene document.
- **`render.json`:** only `created_utc`, the output path and `timing_ms` differ.
- **`renderer.log`:** only the paths and the run time differ.

The pair id does not depend on the tag, so both runs share it.

**A third run** (`--tag run3`, 23:34 UTC) used the builder's final code, after edits that changed only docstrings and the README. It gave the same `scene.json` and `render.png` hashes again. Of the record's 1,096 leaf fields, 37 differ from run 1, all of them times or tag-bearing paths. Its record is `scores/pairs/grand-canyon.yavapai/20240115T194500Z_L24h_7838502574.run3.json` (SHA-256 `c4c3b896d77b04dd989765267f1685bc77f70dd3b7b756e1838aa0dc45b0acdb`), and it took 2.67 s.

**A fourth run** (`--tag run4`, 20 Nov, 00:16 UTC) used the code after the review fixes (section 14). The only later change is a guard that refuses a malformed `drawn_on.size` in a hand-written boxes file; the committed file's `[999, 666]` does not reach it. Run 4 passed every new check: the summary describes this pair, and all three frames and the boxes' frame are 999×666.
- **Same outputs:** the same `scene.json` (`89d6701b…`) and `render.png` (`41849cee…`), and the same pair id, ratios, classes, cell, registration and aligned ratios as run 1.
- **Record:** `scores/pairs/grand-canyon.yavapai/20240115T194500Z_L24h_7838502574.run4.json`, SHA-256 `84c034aaa8baf4022a3cc2564739cfd996d17a59de50fa4517887a0144a6ea8f`, 1,104 leaf fields. Of run 1's 1,096, 45 differ:
  - 35 are the same kinds as run 2's: times, timings and tag-bearing paths;
  - 10 are what the fixes add or change: `extractor` and `score` `eligible_for_metrics` (new, true), `counts_toward_metrics` (now false), `not_counted_because` (new, two reasons), and the plate's and the reference's `size` (new, 999×666).
- **Wall time:** 2.61 s per pair, 2.97 s for the CLI process.

## 9. SHA-256 of every input and output

| Role | File | SHA-256 | Image time / available |
|---|---|---|---|
| Truth frame | `archive/grand-canyon.yavapai/2024-01-15/20240115T194500Z_e224e6bb.jpg` | `c2c8104b3d559cb7ece2031a4de072713381cca51acf55e8f874aa44b934dd98` | 2024-01-15T19:45Z / 2024-01-15T19:45Z |
| Plate and reference | `archive/grand-canyon.yavapai/2021-01-15/20210115T194500Z_57fb3725.jpg` | `c331d6bba51c28507eb38ddfa0f48bdb5177d43dcae9aed3a092c244a0d39c9c` | 2021-01-15T19:45Z / 2021-01-15T19:45Z |
| Forecast file | `forecast/hrrr/grand-canyon.yavapai/2024011412z.json` (33,692 bytes) | `a0cf26916f6f07db0c40575085b7a72afa7fbea64173c861f045abc81b768c76` | run 2024-01-14T12Z / f31, f32 modelled 2024-01-14T14:00Z |
| Forecast summary | `scores/pairs/grand-canyon.yavapai/20240115T194500Z_L24h.pair-forecast.json` | `dcba031e8f89fc6aa1105fabd2c587ab695979b08ad6d322e7fca87da3c8c877` | written 2026-11-19 |
| Landmark boxes | `eval/src/lookout_eval/landmark_boxes_v1.json` | `697bb3c2f44230571ead4fade966edc60a07af7a3d06d2e0a949e7f36b820330` | drawn 2026-11-19 |
| Held-out split | `eval/src/lookout_eval/heldout_periods.json` v1 | `c437544eecdd7a0d33b67c2374e1d388ebe45c36aeca4b2923b39c0d1d31de83` | committed split (D-019, D-022, D-024) |
| Scene | `scores/pairs/grand-canyon.yavapai/20240115T194500Z_L24h_7838502574.run1/scene.json` | `89d6701bafd8344c624e27e5deba0907917d728189f7808d1d92bb42aa511b21` | |
| Render | `renders/pairs/grand-canyon.yavapai/20240115T194500Z_L24h_7838502574.run1/render.png` | `41849cee0e0047b1b88bb07739524341ddbc633ea18c36bf607701361530ff83` | |
| Record, run 1 | `scores/pairs/grand-canyon.yavapai/20240115T194500Z_L24h_7838502574.run1.json` | `304b1266f93261bb0218752450a0147286d453e6bb0930e2659acc99b68a9bff` | |
| Record, run 2 | `scores/pairs/grand-canyon.yavapai/20240115T194500Z_L24h_7838502574.run2.json` | `62f2a7628683745ace290f590163ebf17c1ea1eba503b498db651283a314695a` | |
| Record, run 4 | `scores/pairs/grand-canyon.yavapai/20240115T194500Z_L24h_7838502574.run4.json` | `84c034aaa8baf4022a3cc2564739cfd996d17a59de50fa4517887a0144a6ea8f` | |
| `render.json`, run 4 | `renders/pairs/grand-canyon.yavapai/20240115T194500Z_L24h_7838502574.run4/render.json` | `e77a948c79979bc296960a4d2bea8184ddb6be24dd842f1b9de020d888e27608` | |

- **Where these live:** paths are under `~/lookout-data`, except the two `eval/` files.
- **Our copies of the archive frames** were fetched on 2026-11-19, at 10:30:58Z (truth) and 09:48:16Z (plate), before this task, by the archive check. Neither was fetched here.
- **Software:**
  - eval 0.1.0, run from the working tree on top of commit 3ac98b3 (uncommitted changes, recorded as `dirty`);
  - lookout-forecast 0.1.0 from the snapshot `cfefa79`;
  - renderer 0.2.0; oculus-core 0.1.0 built from the public pin `1b5410b` (D-025). All four records' `versions.ephemeris` read `oculus-core 0.1.0+06809e6`, and they are left as written. That suffix was stale:
    - it was eval's `OCULUS_PINNED_REV` constant, which the pin move in `1ba95d2` did not update; that commit changed only `pyproject.toml` and `uv.lock`;
    - the build that ran was `1b5410b`: in `eval/.venv`, the `direct_url.json` of `oculus-core-py` names commit `1b5410b01f127514fa37656eff2b7d73be7ea8da`. That build was made at 12:31:59 UTC on 19 Nov (the uv cache entry, whose `oculus_core.abi3.so` has the same SHA-256 and whose times the venv's copies keep), and it has been in `eval/.venv` since 22:08 UTC at the latest (the copies' inode change time). Both are before run 1 (23:25 UTC);
    - no number changes: between the two commits, `core/src/ephem.rs`, `core/bindings`, `core/Cargo.toml` and `core/Cargo.lock` are identical, and `core/src/python.rs` differs only by a `fly_m: 0.0` field in three functions other than `sun_moon` (checked on 20 Nov with `git diff` in the local oculus clone);
    - eval now takes the suffix from the installed build's `direct_url.json` (`0.1.0+1b5410b`), so later records name the build that ran;
  - Python 3.13.13, NumPy 2.5.5, SciPy 1.18.1, Pillow 13.0.0, Node 24.13.0.

## 10. Leakage audit

- **Issue time:** 2024-01-14T19:45Z (valid − 24 h).
- **Every input a forecast issued then would use was available at or before it:**
  - the HRRR hours f31/f32: modelled at 2024-01-14T14:00Z (on S3 they were complete at 13:28:11Z and 13:29:37Z);
  - the plate and clear-day reference: NPS image time 2021-01-15T19:45Z (an archive frame was on the live feed at its image time);
  - the renderer, the scene builder and the boxes are code and fixed assets, with no weather input after the issue time.
- **Held out:** nothing. Grand Canyon's held-out season is summer 2026 under design H (committed split v1, D-024). All three frames are dated years before the 1 Jun 2026 archive guard (D-019), and nothing came from `quarantine/` (D-022). Runs 1–3 did not check the forecast summary and the boxes file against the quarantine; neither is under it (both paths are in section 9), and run 4 checked them. The pair's own checks passed and are in the record's `leakage_checks`:
  - the truth is not held out;
  - the plate and reference were taken before the issue, available by then, not on the valid day and before the fit bound 2026-06-01T07:00Z;
  - the forecast was available by the issue time.
- **No NPS request** was made in this task. The frames were already on disk. The only network was NOAA's open bucket: 3 listings, 6 `.idx` files and 13 GRIB ranges, 35,394,897 bytes in all (the `--plan-only` run, then the extraction run's own plan, then the extractor).
- **The truth frame was looked at** before the boxes were fixed (section 4). It is a development frame, so this is not held-out leakage. It is disclosed because a by-eye box chosen after seeing the outcome could favour it. The boxes sit on terrain bands for robustness to the camera shift.
- **Scoring exclusion:** none for this date. Runs 1–3 recorded `counts_toward_metrics: true`, which only said that the committed split and the gates allow the pair. A table builder keying on that field would have counted an unvalidated extractor's pair, so the field was split (section 14). Run 4 records:
  - `eligible_for_metrics: true`: the split and the gates allow it;
  - `counts_toward_metrics: false`, with `not_counted_because`: "extractor v-1 is not validated against labels (PRD §13.8)" and "a v-1 pair is a pipeline test (PRD §16 M0.5), not a scored pair".

  No metric uses v−1 pairs.

## 11. What is crude about v−1

1. **One pair.** There is no skill information, and PSS is undefined.
2. **The extractor's reference is one sunny frame.**
   - Conditioning on Sun position alone reads a visible, flat-lit rim as "hidden" (section 7).
   - τ_hi 0.6 and τ_lo 0.35 are placeholders.
   - The extractor has not been validated against labels (PRD §13.8).
3. **Boxes by eye.** They are not surveyed, and the landmark distances are guesses. The truth's camera aim differs from the plate's by 5 %, and moving the box across that shift flips the primary class (section 7).
4. **No Same view gate.** Registration is measured but not gated; the tolerance is M2's.
5. **The render's geometry is nominal.**
   - Pitch 0° and FOV 60° are not solved, and there is no depth image or sky mask.
   - Every pixel is treated as terrain at most 15 km away: the upper image gets 15 km of added haze on top of the plate's real haze, and the lower image hits a flat ground plane.
   - The mid layer (3,686–8,117 m by the pressure-band mapping) is drawn as an opaque grey band over the upper third of the frame, which hides the sky. The 87–100 % high cloud (8,117 m) and the 10.5 km ceiling are out of reach of the 15 km rays, so neither is drawn.
6. **The plate is not dehazed or relit for overcast.** The render is the sunny plate under a cloudy forecast. It keeps the plate's shadows, even though the forecast says high overcast.
7. **The scene builder's layer mapping.** Cloud bases come from pressure bands, and `HGT:cloud base` is used without the `PRES:cloud base` check. That was harmless here only because low cover was 0 %.
8. **Availability is modelled conservatively.** The fallback is +120 min, because the lag log has too few lags for f31/f32 yet. It is never early; it is 30–32 min late against S3 for this run.
9. **The forecast hour list is substituted for one call** (`pair_forecast.only_hours`), because lookout-forecast takes only a maximum hour.
10. **No S_rule.** Grand Canyon's manifest has no landmarks, so S_rule is not computed.

## 12. What M1.5 and M2 replace, and what the next pair needs

- **M1.5:**
  - renderer v0 with the Hillaire sky, cloud slabs, a ceiling slab and flat fog;
  - extractor v0;
  - P1 on development data only, against persistence, climatology and S_rule;
  - per-layer cloud bases from model levels, with the `PRES:cloud base` check;
  - an explicit hour list in lookout-forecast, so `pair_forecast` stops substituting `run_hours`.
- **M2:**
  - the plate builder (PRD §8.5): a bright-overcast base frame with every landmark visible, dehazed; the pose solve from surveyed landmarks, which replaces `landmark_boxes_v1.json`; a DEM depth image; the sky and landmark-band masks;
  - the Same view gate with a tolerance (registration against the plate);
  - clear-day references fitted per viewpoint, with a diffuse-light state as well as the Sun bin;
  - τ and the partial band fixed on development data, and extractor validation against labels (PRD §13.8);
  - per-viewpoint haze profiles.
- **For the next pair:**
  - **A same-aim plate.** Grand Canyon hires frames from clear days in the two weeks before the issue time. For example `grca` at 19:45Z (12:45 MST) on 1–13 Jan 2024: 13 thumbnails first (about 2 KB each), then the hires of the clear ones (about 90 KB each).
  - **A bright-overcast frame** from the same weeks, as a second reference state.
  - **Boxes fixed before the truth is measured:** from the plate and the registration only (section 4).
  - All of these are before the guard, so the lead can fetch them through the capture loop's archive queue. Until then this pair stands as the v−1 evidence.
- **For the docs agent:** D-024's Grand Canyon row and the archive check say the 3:2 epoch has one framing. The 2021-01-15 and 2024-01-15 hires differ by a measured re-aim of about 5 % (section 2), so a full-frame Same view registration is needed inside the epoch before references, τ or boxes are fitted across dates.

## 13. Files

- **Code** (eval, this task):
  - `eval/src/lookout_eval/pair_forecast.py`, `landmark_boxes.py`, `landmark_boxes_v1.json`, `pair_score.py` and `registration.py`;
  - changes to `backtest_pair.py` and `cli.py`, with tests beside them, and a TODO(M1.5) note on the cloud-base check in `scene.py`'s docstring;
  - `eval/README.md` updated.
- **Code** (eval, the review fixes on 20 Nov, section 14): `backtest_pair.py`, `cli.py`, `landmark_boxes.py` and `pair_forecast.py`, with tests in `test_backtest_pair.py`, `test_cli.py`, `test_landmark_boxes.py` and `test_pair_forecast.py`, and the README.
- **Data** (under `~/lookout-data`, outside the repo):
  - `scores/pairs/grand-canyon.yavapai/`: the forecast summary, the four pair records (`.run1` to `.run4`) and their `scene.json` folders;
  - `renders/pairs/grand-canyon.yavapai/<pair id>.run1/` to `.run4/`: `render.png`, `render.json` and `renderer.log`;
  - `forecast/hrrr/grand-canyon.yavapai/2024011412z.json`;
  - the extractor's side effects, outside the task's output folders: the archived `run` event at line 475 of `forecast_log/2026-11-19.jsonl` and the marker `state/forecast/hrrr/2024011412z.json`. The lag readers ignore `run` events, and the forecast Lambda looks back only 12 h, but the event sits in the live log that backs the M0.5 "≥ 72 h of worker logs" evidence, so that evidence should leave it out.

## 14. After the adversarial review (20 Nov 2026)

The review reproduced the pair, then found three medium problems and several small ones. The code findings are fixed in eval, with tests, and run 4 shows that none of the fixes changes the pair's outputs. Two findings concern other documents and are passed on: D-024's "one framing" (section 12) and the archived `run` event in the live worker log (section 13).

| Review finding | Fix | On this pair |
|---|---|---|
| The forecast summary and a `--boxes` file were read without the quarantine check (D-022) | Both are refused before they are read, as are a `--references-file` listing and an auto forecast's file | Neither is under `quarantine/` |
| The summary was checked only for the file's SHA-256 and the run | It must also match the pair's viewpoint, valid and issue times (its own and its choice's), bracketing hours, and the file's recorded availability | All match |
| Frames of another aspect ratio were scored, with only a diagnostic note | The plate, every reference, the boxes' `drawn_on` frame and any pixel region must have the truth's aspect ratio within 1 %; otherwise the pair is refused | All 999×666 |
| `--boxes v-1` at an uncovered viewpoint crashed with a `KeyError` traceback | Exit 2 with a message | Not hit |
| A v−1 record said `counts_toward_metrics: true` | `eligible_for_metrics` (split and gates) and `counts_toward_metrics` (also a validated extractor, never in v−1), with reasons | Eligible; does not count |
| A reused forecast file keeps the availability of the lag model of its day | `pair_forecast` refuses a file whose recorded availability differs from the choice's; the summary carries the file's value | 14:00Z both, re-checked |
| No test of the direction in which the shift moves a box, or of a lost hour substitution | Both tested | The moved box's ratio, 0.360, is unchanged |
| Doc inaccuracies | Corrected: four 16:9 thumbnails; f00–f48; the band table's method; no process time claimed; the `--plan-only` transfer, 45,515 bytes | |
| The disclosure did not say what was measured on the truth before the boxes were fixed | Section 4 says so | |

The new tests were run against scratch copies of the code with each fix undone, one at a time (13 such copies); every one failed there.
