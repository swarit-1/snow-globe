# lookout-eval

Verification for Lookout (PRD §13): verification scores, the ephemeris wrapper, daylight, the frame quality
gates, extractor v−1, baselines, the leakage guard and the backtest pair. Python 3.13, uv.

```sh
cd eval && uv sync && uv run pytest -q && uv run ruff check .
uv run lookout-eval ephem-check            # oculus-core vs the NPS archive's SolarElevationAngle
uv run lookout-eval backtest-pair --help   # one v-1 pair: SceneSpec -> headless render -> extractor
```

The default suite also runs the renderer's own `validateScene()` (`renderer/src/scene.ts`) on eval-built
scenes under Node 24, without a browser; it skips with a reason when Node 24 is missing. The real headless
render is opt-in: `LOOKOUT_EVAL_RENDER=1 uv run pytest -q tests/test_renderer_contract.py`.

## Setup and the oculus-core pin

`uv sync` builds **oculus-core** from source with maturin into this venv. That is the default `oculus`
dependency group: crate `core/`, feature `python`, through `core/bindings/python/pyproject.toml`. It is
pinned to commit `06809e6141432119d399cd22f29f6f517bf0226d`. The build needs a Rust toolchain and takes
about 20 s on an M4.

**That commit is not on GitHub** (checked 19 Nov 2026):
- It sits only on the local branch `overnight/2026-09-22` of `~/Documents/Oculus/oculus`, 33 commits ahead
  of `origin/main` (`1b5410b`, `git rev-list --count 1b5410b..06809e6`).
- The `sun_moon` code path is identical in both commits: `git diff 1b5410b 06809e6` is empty over
  `core/src/{ephem,time,frames,math}.rs`, `core/Cargo.toml` and `core/bindings/python`; `python.rs` differs
  only in functions eval does not call.
- On this machine the exact pinned SHA was built from the local copy, by pointing git at it for one command:

```sh
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url./Users/swart/Documents/Oculus/oculus.insteadOf \
  GIT_CONFIG_VALUE_0=https://github.com/swarit-1/oculus uv sync
```

`uv.lock` still names the GitHub URL. Elsewhere, including CI (`uv sync --locked`), `uv sync` fails until
that commit is pushed or the pin moves to a public commit (an owner decision; `app/core` uses the same pin).
Without oculus-core, use `uv sync --no-group oculus` and set `LOOKOUT_EPHEMERIS=noaa` (Sun only); nothing
falls back silently. The suite passes in such a venv, with the oculus-only tests skipped.

The optional `forecast` group installs lookout-forecast. It is used only by
`backtest-pair --forecast auto`, which backfills one HRRR run from NOAA (network):
`uv sync --group forecast`.

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
| `frames.py` | data layout | Frame records and the `frames/` and `archive/` catalogue |
| `leakage.py` | §13.1, §13.9 | `FrameStore` with issue-time and fit views that raise `LeakageError`; no labels on held-out frames outside the sealed scoring |
| `baselines.py` | §13.6, §9.1 | Persistence (a), (b) and the dev-only choice; climatology; S_rule; analog (Tier 0) |
| `forecast_io.py` | §8.3 | Reads lookout-forecast run files: interpolation, availability, datums, winds, the summit ray |
| `scene.py` | §9.2, §9.5 | SceneSpec for renderer 0.2.0 (`run_init_utc`, plate id and hashes); schema plus cross-field checks; the renderer's own `validateScene()` under Node (`renderer_validate.mjs`) |
| `backtest_pair.py`, `cli.py` | §16 M0.5 | The v−1 pair and the `lookout-eval` console script |

## Choices worth knowing

- **Leakage.** A baseline issued at *t* sees only frames imaged *and* available (fetched) by *t*, on every
  access path. There is no exception. Fits see only development frames from before the earliest held-out
  period. A `FrameStore` refuses labels on held-out frames unless it is built with `SealedScoring` (the
  commit of the frozen pre-registration), which only the M4 sealed scoring may do.
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
- **Backtest pair.** Clear-day references and the plate must be taken before the issue time and available
  by then, not on the valid time's local day, from the same viewpoint, not held out and before the first
  held-out period. The held-out periods default to design P's sealed window (15 Dec 2026 – 14 Jan 2027,
  the viewpoint's local days) until the committed split replaces it; `--held-out FIRST:LAST` overrides.

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

**Renderer contract.** The renderer's `validateScene()` under Node takes 56 ms per call (median, warm).
The real `pnpm render` of an eval-built scene on the synthetic plate: 573–669 ms render, 0.75–1.19 s in
all (renderer timing, three runs, SwiftShader).

## Not done (TODO)

- **M0.5:** the committed design H / P split replaces the default held-out periods (DECISIONS.md).
- **M2:**
  - fit τ, the partial band, the in-cloud and camera-fault thresholds, the analog weights and the clear-air
    haze on development data;
  - the camera pose;
  - choose the block length from development autocorrelation, and the minimum detectable effect;
  - quality gates: Same view (registration against the plate), Clear lens (a sharpness floor, reported with
    and without iced-lens exclusions) and the Colour-mode cut-off (calibrated on development day and night
    frames; the two local night samples measure `chroma_spread` 0.00 and 0.01);
  - provenance checks for the sky mask and depth image, from the plate builder.
- **M1.5:** per-layer cloud bases from model levels instead of the pressure-band mapping in `scene.py`.
- **M4:** extractor validation against labels (§13.8); the sealed scoring (`SealedScoring`).
- **Untried:** `backtest-pair --forecast auto` has never reached NOAA, and no pair has run on real frames
  (no landmark box is defined yet); both are the integration step's.
