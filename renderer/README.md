# @snow-globe/renderer — Tier 1 renderer v0

Renamed from `@lookout/renderer` on 21 Nov 2026 (D-080); package 0.2.0. The rename changed no pixel, so
`RENDERER_VERSION` (below) and the goldens are unchanged, and the seed domain stays `lookout-seed-v1`:
changing it would change every render.

WebGL2 (GLSL ES 3.00, highp) renderer for PRD §9.2, with a float64 CPU reference of the cloud-mask
logic and a headless runner on Playwright Chromium + SwiftShader. Version **0.2.0**, stage **v0**: it serves
the M0.5 backtest pair (labelled "v−1"). It is **not yet the full M1.5 renderer v0**: PLAN.md M1.5 and
D-013 put the Hillaire 2020 sky in renderer v0, and that pass is still a placeholder (`TODO(M1.5)`). The
oculus-core WASM (Sun and Moon computed inside) arrives in M2 (`TODO(M2)`); until then the caller supplies
them and the renderer checks the Sun.

```sh
pnpm install
pnpm test               # tsc + unit tests (math, CPU reference, Monte Carlo) + headless SwiftShader tests
pnpm render --scene s.json --plate p.jpg [--sky-mask m.png] [--depth d.png] --out out.png
pnpm bench              # per-frame times, SwiftShader and (if available) Chromium's GPU path
pnpm montecarlo [--case above|deck|thin|cirrus]   # PRD §9.5 coverage Monte Carlo table (M1.5 gate evidence)
pnpm goldens:update     # regenerate the synthetic plate and the three goldens (review before committing)
pnpm build              # dist/snow-globe-renderer.js (ESM) and .iife.js
```

`pnpm render` writes `out.png` and `out.json` (renderer version, GL renderer string, scene hash, input
and output SHA-256, render, readback and encode ms). Relative paths resolve against the directory pnpm was
run from. It refuses:

- an `--out` or `--meta` path inside the repository (symlinks resolved): renders belong under
  the data dir (`$SNOW_GLOBE_DATA_DIR`, else `$LOOKOUT_DATA_DIR`, else `~/snow-globe-data` or, until it
  is moved, `~/lookout-data`), per CLAUDE.md;
- a plate, sky mask or depth file whose SHA-256 is not the one `scene.plate` names.

The only images in the repo are the **synthetic** plate and goldens in `goldens/` (procedural, no camera
pixels), and `pnpm goldens:update` refuses any other viewpoint or plate. This synthetic-only exception to
the CLAUDE.md rule is not yet recorded in CLAUDE.md or DECISIONS.md; it needs the owner's sign-off.

## Contract

`scene.schema.json` (draft 2020-12) and `validateScene()` in `src/scene.ts` define `SceneSpec`. Unknown
fields are rejected. A test checks that both accept the goldens and reject the same 36 malformed
documents. It also pins down the rules only `validateScene()` can check (each one passes the schema),
and the schema's description lists them:

- the seed is derived from the viewpoint and the run;
- timestamps are real instants (no 30 February);
- the valid time is 0–384 h after the run;
- cover-profile distances and wind-series leads strictly increase;
- the wind series agrees with the valid-time wind;
- the Sun matches the valid time and place.

The schema's patterns already reject month 13, day 32, hour 24 and second 60, and `contains`/`maxContains`
reject duplicate layer names. Eval validates with Python `jsonschema` (Draft 2020-12), which agrees on these.

- **`run_init_utc`** (required, `YYYY-MM-DDTHH:MM:SSZ`): the forecast run's initialisation time.
  - `seed` must equal `deriveSeed(viewpoint_id, run_init_utc)`.
  - A run's cloud field is fixed at its initialisation time and drifts by the **time integral of the
    wind** up to the valid time (`src/math/advection.ts`), so scrubbing is continuous and the same
    (viewpoint, run, valid time) always gives the same pixels.
  - Renderer 0.1.0 used wind × seconds since 1970 instead, so a 1e-6 m/s change in the wind moved the
    field by about 1.8 km.
- **`forecast.wind_series`** (optional): `[{lead_h, u_ms, v_ms}]`, for example HRRR's hourly winds,
  integrated exactly (linear between knots, constant beyond).
  - Read at the valid time, it must be within 0.05 m/s of `wind_u_ms` / `wind_v_ms`.
  - Without a series, the valid-time wind is held constant since the run started (a v0 approximation). A
    producer that changes `wind_u_ms` / `wind_v_ms` from frame to frame without a series still gets
    continuous drift, but mis-paced by (dw/dt) × lead time. **Scrubbing producers should send the series.**
  - One wind moves every layer. TODO(M2): per-layer winds at cloud height; HRRR's 10 m wind, all that
    producers have today, is a weak proxy for how clouds aloft move.
- **PRD §9.2 fields, validated and hashed but not rendered yet (`TODO(M2)`):**
  - `forecast.precip_type` (`none | rain | snow | freezing_rain | ice_pellets | mixed`);
  - `forecast.rh_pct` (0–100);
  - `forecast.pbl_height_m_agl` (boundary-layer height above ground; HRRR HPBL is above the surface).
- **Plate identity:** `plate.id` and `plate.sha256` (SHA-256 of the plate file) are required.
  `plate.sky_mask_sha256` / `plate.depth_sha256` are present exactly when `has_sky_mask` / `has_depth`
  are true.
  - The plate is therefore in `scene_hash`, which feeds PRD §10.3 paint gating and the cache keys.
  - `pnpm render` checks the hashes against the files.
  - `SnowGlobeRenderer.render()` checks the hashes a caller attests (`inputs.plateSha256` etc.). Decoded
    pixels carry no file hash, so a browser caller that attests nothing is not checked.
- **Sun:** still supplied by the caller (geometric altitude, azimuth clockwise from north).
  - `validateScene()` rejects a Sun more than **1°** from the NOAA/Meeus solar position for the valid time
    at the camera (`src/ephemeris/sun.ts`). That formula matches Meeus Example 25.a to 1e-5°.
  - Synthetic geometry tests opt out with `allowSyntheticSun`, a render option that is never part of the
    scene. The CLI and eval cannot set it.
- **Moon:** supplied by the caller and **not checked** yet. TODO(M2): Sun and Moon computed inside via the
  oculus-core WASM.
- **Other optional fields, with defaults:** `camera.agl_m` (10 m), `forecast.visibility_epsilon` (0.02),
  `plate.nominal_depth_m` (15,000 m), `meta` (free-form provenance).
- **`renderer_version`** must equal `RENDERER_VERSION` (`0.2.0`). The CLI refuses otherwise unless
  `--allow-version-mismatch` is given; the metadata records both.

### Conventions

Local East-North-Up frame at the camera: x east, y north, z up; Earth is a sphere of R = 6,371,000 m and
every height is metres above MSL. Heading is clockwise from true north; pitch up is positive; positive roll
turns the camera clockwise as seen from behind. Pixel (i, j) has its centre at (i + 0.5, j + 0.5) from the
top-left; pixels are square, so the vertical half-angle is `atan(tan(hfov/2) · h / w)`.

### Inputs

| Input | Format |
|---|---|
| Plate | JPEG or PNG, sRGB. Decoded in Node (jpeg-js, pngjs) and uploaded as raw RGBA into an `SRGB8_ALPHA8` texture (mipmapped), so no browser decoder or colour management touches it. Stretched to the frame. |
| Sky mask | PNG, R channel: 255 = sky, 0 = terrain; ≥ 0.5 counts as sky for geometry, edges blend the backgrounds. |
| Depth | PNG, RGB24 distance along the view ray in metres = R·65536 + G·256 + B; 0 = unknown, 0xFFFFFF = sky. Read with `texelFetch` (no filtering). |

## Passes (v0) and the approximations they make

| PRD pass | v0 | Approximation / TODO |
|---|---|---|
| 1 Sky | Analytic placeholder: zenith luminance from a log table over Sun altitude (CIE-like by day, measured twilight sky brightness below), day / twilight / night colour weights, horizon brightening, a circumsolar glow, moonlit sky. | Continuous in Sun altitude and finite at every altitude (tested on CPU and GPU). **Placeholder**: TODO(M1.5) Hillaire 2020 LUTs (D-013, PLAN M1.5). The bundle keeps legal comments (esbuild `legalComments: 'eof'`, tested), so the port's MIT notice will survive bundling. |
| 2 Sun, Moon | Anti-aliased geometric disks; colour from a per-channel air-mass extinction. | Moon brightness ∝ illuminated_fraction³, no phase shape, no stars: TODO(M2). |
| 3 Cloud slabs | Each layer is a spherical shell [base, base + thickness]. Every view ray is intersected with Earth curvature (cancellation-free quadratic, metre-accurate in float32) and clipped to the ray end. It is then sampled at n stratified points along its in-slab path of length L: one per 1/16 of the layer's octave-0 noise cell, `n = clamp(ceil(L / step), 4, 128)`. At each point the forecast cover is read at that point's great-circle distance; the N(0,1) noise is compared with the normal quantile of that cover (**threshold first**), and only the binary masks are averaged. Opacity α = 1 − exp(−β·(m/n)·L). The colour path stops sampling a slab once its optical depth reaches 7 (α ≥ 0.99909); the coverage debug outputs always sample the whole path. | v0 constants per type (`src/clouds.ts`): cell 2.4 / 3.6 / 7.2 km, β 0.03 / 0.012 / 0.0006 m⁻¹ for low / mid / high. The step was set by a convergence study, and the Monte Carlo below shows it converged. Renderer 0.1.0 used 4 samples for every path, under-drawing a cover-0.1 deck at 0–2° by 44 points. Layers are independent (random overlap); TODO(M1.5) check against HRRR TCDC. Face radiance: two-stream transmission/reflection of the slab's vertical optical depth, lit by the Sun above the slab's own horizon (altitude + dip). No shadowing of lower layers by upper ones. Cloud shadows on the ground (pass 4) still take one mask sample per slab on the Sun ray: TODO(M2). |
| Ceiling | A forecast ceiling not inside a declared layer becomes a **solid** 300 m slab (no gaps), consistent with the S_rule baseline. Inside a declared layer, that layer represents it. | TODO(M1.5): replace with the TCDC-checked overlap rule (PRD §9.2 pass 3). |
| 4 Scene light | `plate × (E_direct × T_sun(P) + E_diffuse) / π`, where T_sun is the realised cloud transmittance from the terrain point P towards the Sun (cloud shadows) and E_diffuse uses the expected cover. | The plate is used as an albedo proxy with no dehazing, normals or DEM horizon shadows: TODO(M2). |
| 6 Haze | Clear-air extinction `haze.beta_per_km`, uniform below 2 km above the ground; airlight = horizon sky radiance × cloud dimming. | TODO(M2): per-viewpoint fitted profile. |
| 6 Fog | Visibility V gives β = −ln(ε)/V; only the excess over the haze is fog. Applied only to the ray inside the near-surface layer, with a soft top (4 bands of 25 m, β·e^−(k+½)). Airlight blends from the haze airlight (thin) to a multiple-scattering airlight 0.85·E/π (dense) with weight 1 − exp(−β·1 km). | **No DEM yet:** the ground under every ray is the spherical shell at the camera's ground height (`elev_m_msl − agl_m`), so the fog fills all heights below ground + 100 m: valleys below the camera are fogged and terrain rising above that height clears. A ceiling inside that layer (a surface-based deck) raises the fog top to the deck top. Cloud slabs confine their own cloud-fog between base and top in world height (pass 3), so terrain above a deck stays clear. TODO(M2): DEM ground height along the ray. |
| Ray end | Sky: the sea surface if the ray meets it, else unbounded. Terrain: the depth map; without one, the flat ground for downward rays, capped at the nominal depth. | Without a sky mask every pixel is terrain (the plate's own sky is relit, not replaced); a mask is required for meaningful sky. |
| 8 Camera | Exposure normalises so that a white diffuser under the camera's expected illuminance maps to 1 (floor 5 lux, below which frames go dark like webcams at night); linear to 0.8 then a soft exponential knee; sRGB. | Under the plate's own lighting the plate is reproduced. TODO(M2): Kim & Pollefeys model-free response, white balance, infrared monochrome mode. |
| 5, 7 | Not in v0. | TODO(M2): night lights; precipitation extinction and wet darkening (`precip_rate_mm_h` and `precip_type` are validated and hashed only). |

## Determinism

The same inputs give byte-identical PNGs on the same platform:

- uniforms are derived in float64 by `buildFrame()`;
- the lattice is generated from the seed, and nothing reads the clock;
- inputs are decoded in Node, and outputs are encoded with fixed pngjs settings.

This is tested across two separate browser launches and through the CLI.

Measured on 2026-11-19: Chromium's `gl.finish()` returned in ~0.1 ms while SwiftShader was still drawing.
So `finish()` here ends with a 1-pixel `readPixels` barrier, and every render time is measured to that
barrier.

Advection is anchored at the forecast run (above). Measured on SwiftShader, low-deck golden, 2026-11-19:

| Check | Renderer 0.1.0 (review) | Now |
|---|---|---|
| Wind 6.3 → `Math.fround(6.3)` (a float32 decode) at the same valid time | 4,083 of 57,600 pixels changed | 0 pixels |
| Wind +0.01 m/s | mean \|diff\| 34.0 levels, about a 3 h jump | 1.23 levels; a 60 s scrub step is 1.87 |
| 60 s step across a forecast hour, with hourly winds 6 → 9 → 3 m/s | decorrelated | 2.28 levels; 1.27× the same step under a constant 9 m/s |

Noise-field correlation at 4,000 points, 7 h after the run's initialisation:

- 1.000000 under float32 rounding of the wind;
- 0.999994 for a 1e-4 m/s difference;
- the epoch-anchored control drops to 0.005.

## Goldens

Three scenes on the synthetic plate (`goldens/scenes/*.json`, renderer 0.2.0, run initialised
2026-11-18T12:00Z, true Sun for each valid time):

- `clear`;
- `low-deck`: a 1,000–1,400 m deck in the valley below a camera at 1,650 m, with thin cirrus above;
- `fog`: visibility 1.5 km at the camera.

The synthetic world was re-posed (`synthetic.test-ridge/plate-v2`). The camera's ledge now ends 15 m in
front of it instead of 400 m, so the camera looks down into the valley:

- the deck is drawn at opacity ≥ 0.5 on 19.0 % of the frame;
- removing the deck changes 10,971 of 57,600 pixels (19.0 %). Under renderer 0.1.0 it changed 48.

The golden test also requires each golden's named feature (the deck, the fog) to change at least 10 % of
the pixels when removed. Removing the fog changes 100 % of the pixels.

Matching rules:

- On the platform and GL renderer recorded in `goldens/manifest.json`, renders must match pixel for pixel.
  SSIM is always reported.
- On any other platform the PRD rule applies: SSIM ≥ 0.98. SwiftShader on Linux x86 will probably not be
  bit-identical to macOS arm64, so CI on another architecture should either regenerate goldens there or
  rely on the SSIM rule.
- The synthetic plate's regeneration check compares **decoded pixels**. The PNG bytes depend on the zlib
  build, so their hash is reported for information only.

SSIM follows Wang et al. 2004 (11×11 Gaussian, σ 1.5, luma). It matches scikit-image 0.25.2 to within
4e-14.

## GPU vs CPU agreement threshold

The threshold was declared before measuring, and `test/gpu-cpu.test.ts` enforces it. The CPU reference is
float64; the GPU is float32.

- **Ray directions:** within 1e-5.
- **Ray ends:** within 1e-5 relative.
- **Mean cover at the sample points:** within 1e-3.
- **Coverage κ:** at least 99.5 % of pixels identical per layer, with mean κ within 0.002. A pixel may
  differ only when a sample's noise lies within float32 rounding of the threshold, or when float32 puts
  the path length on the other side of a sample-count step.
- **Drawn opacity α** (what is composited, and what the Monte Carlo's drawn path computes): at least
  99.5 % of pixels within 2e-3 per layer, with mean α within 0.002.

Measured on SwiftShader, in four cases:

- an all-sky view with three layers plus a ceiling;
- the synthetic plate with mask and depth, with the deck drawn on 19.0 % of pixels;
- a deck below the camera over flat ground, every ray below the horizon, with the deck drawn on 34.0 %;
- no mask and no depth.

Results:

- κ identical on 99.986–100.000 % of pixels;
- α within 2e-3 on 99.997–100.000 %;
- mean differences 0.0000;
- target differences ≤ 6.6e-7;
- ray directions within 1.9e-7, and ray ends exact.

## Coverage Monte Carlo (PRD §9.5, the M1.5 gate)

`src/cpu/montecarlo.ts`, `pnpm montecarlo --case above|deck|thin|cirrus`. Tables from 2026-11-19 are in
`$SNOW_GLOBE_DATA_DIR/cache/renderer/montecarlo/`.

Each case uses 1,000 seeds × cover 0.1–0.9 × 6 elevation bands × 72 rays per band. Every ray crosses one
slab twice:

- **Drawn:** the renderer's own sampling and opacity, per pixel the same as the GPU's α (above).
- **View-ray target:** the same cloud field along the same ray at a converged step (1/64 of the noise
  cell, no cap). A slant ray crosses many columns, so near the horizon it meets cloud far more often than
  the area cover says. With cover 0.1 at 0–2°, the mean column cover along the rays is 0.136 but 88 % of
  rays meet cloud.

The gate compares two statistics in every row:

- the **rendered sky fraction** (rays drawn at opacity ≥ 0.5) against the view-ray target fraction;
- the mean opacity against the target's, which carries semi-transparent cirrus.

Renderer 0.1.0 gated **mask coverage κ** instead. By linearity, κ equals the mean cover in expectation,
so that gate could not see what was drawn. κ is still checked against the mean cover, as a test of the
noise marginal.

| Case | max \|fraction − target\| | max \|opacity − target\| | max \|κ − mean cover\| |
|---|---|---|---|
| Thick low deck 1 km above, cover rising 0.3 over 120 km | 1.32 pts | 0.93 pts | 2.86 pts |
| Deck below the camera, rays −90° to −1° ending at the ground | 1.54 pts | 1.08 pts | 2.54 pts |
| 50 m mid layer, constant cover | 0.80 pts | 0.74 pts | 2.57 pts |
| 1.5 km cirrus, constant cover | 0.98 pts | 0.10 pts | 1.40 pts |

The κ column moves by up to about 3 points: the 72 rays of an overhead band share one cloud cell, so their
seed-to-seed spread is large.

**Convergence.** Halving the step and doubling the cap moves no row by more than 0.95 points (threshold
2 points; 300 seeds, paired).

**Negative controls** (300 seeds; the drawn path broken, the target correct) show the gate has power:

| Control | Miss |
|---|---|
| Filtering before thresholding | 93.6 pts |
| Dropping the variance normalisation | 24.0 pts |
| Renderer 0.1.0's constant 4 samples | 44.5 pts |
| Flat-Earth geometry, thin layer | 19.2 pts |
| Flat-Earth geometry, deck below the camera | 7.8 pts |
| Flat-Earth geometry, thick deck above (reported, not gated) | 5.8 pts |

The exact-geometry unit tests also guard curvature.

What the gate does **not** show: whether this cloud-field model's sky cover matches real cameras for a
given HRRR cover. That is a modelling question for the development frames (M2).

## Benchmarks (`pnpm bench`, this MacBook Air M4, 2026-11-19)

Each time is page-side `render()` plus the 1-pixel barrier, as p50 / p95 over 30 or 60 frames after 2
warm-ups. Each steady frame moves the valid time by 1 min and the Sun with it. The "heavy" scene has three
slabs, fog and Sun shadows.

The machine is fanless, and repeated runs vary a lot: the clear scene, which this change does not touch,
measured SwiftShader 720p p50 70.3 ms and then 49.6 ms three minutes later. Raw JSON is in
`$SNOW_GLOBE_DATA_DIR/cache/renderer/bench-2026-11-19T11-*.json`.

**SwiftShader** is a CPU rasteriser and **not representative of WKWebView**:

| Size | Clear | Heavy |
|---|---|---|
| 1280×720 | 49.6 / 56.9 ms | 174 / 217 ms |
| 1920×1080 | 117 / 134 ms | 414 / 547 ms |

**Chromium ANGLE/Metal on the Apple M4**, three runs:

| Size | Clear | Heavy |
|---|---|---|
| 1280×720 | 2.2–2.4 / 2.6–4.3 ms | 3.3–4.5 / 3.8–13.2 ms |
| 1920×1080 | 2.9–4.5 / 4.9–8.2 ms | 6.0–9.2 / 7.0–18.5 ms |

**Cost of convergence.** With renderer 0.1.0's constant 4 samples, heavy 1080p measured 4.4 / 4.5 ms
(builder, one run).

- Converged sampling alone measured 15.5 / 24.3 ms.
- Sampling slabs front to back, and skipping the ones hidden behind opaque nearer slabs (colour path only;
  under 0.1 % of a pixel), brings it to the range above.
- That range is at or above the PRD's ≤ 8 ms at 1080p (a WKWebView target, measured in M2).
- **Bottleneck:** cloud-mask sampling on long near-horizon paths, up to 128 samples × 4 octaves × 4
  texel fetches per slab. Cirrus (β 0.0006 m⁻¹) seldom turns opaque, so its paths are never cut short.

**First frame:**

- SwiftShader, including shader compile and JIT: 594 ms.
- Metal, right after the shader changed: 240 ms.
- Metal on later launches: 22–25 ms. This is presumably the OS keeping the compiled program (not
  verified).

**Readback**, measured in the page:

- `readPixels` + row flip: 1.1–3.8 ms.
- base64 encoding of the pixels for the trip to Node: 50–64 ms at 720p, 111–149 ms at 1080p. The transfer
  itself is not timed.
- Renderer 0.1.0 reported the sum as "readPixels + transfer". Almost all of it was the encode.

The PRD targets (≤ 8 ms per frame at 1080p steady state, ≤ 50 ms GPU time for the first frame with
pre-warmed shaders) are defined for WKWebView. They are measured in M2.
