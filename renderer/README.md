# @snow-globe/renderer — Tier 1 renderer v0

Renamed from `@lookout/renderer` on 21 Nov 2026 (D-080), which changed no pixel. Renderer 0.3.0 (23 Nov) then
changed every pixel by adding the Hillaire 2020 sky. The seed domain stays `lookout-seed-v1`: changing it would
change every cloud field.

WebGL2 (GLSL ES 3.00, highp) renderer for PRD §9.2, with a float64 CPU reference of everything it draws and a
headless runner on Playwright Chromium + SwiftShader. Version **0.3.0** (the package and `RENDERER_VERSION`), stage
**v0**: renderer v0 of PLAN.md M1.5, with the Hillaire 2020 atmosphere (D-013, ticket M1.5-01), cloud slabs, a
ceiling slab, flat fog and camera v0. Still open in v0: the ceiling's overlap rule checked against HRRR TCDC
(`TODO(M1.5)`). The oculus-core WASM (Sun and Moon computed inside) arrives in M2 (`TODO(M2)`); until then the
caller supplies them and the renderer checks the Sun.

```sh
pnpm install
pnpm test               # tsc + unit tests (math, CPU reference, Monte Carlo) + headless SwiftShader tests
pnpm render --scene s.json --plate p.jpg [--sky-mask m.png] [--depth d.png] --out out.png
pnpm render:batch --jobs jobs.jsonl   # many scenes through one headless runner (eval's p1dev uses it)
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
the CLAUDE.md rule is proposed in DECISIONS.md D-041 and still needs the owner's sign-off.

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
- **`haze.beta_per_km`** (required): the viewpoint's clear-air **aerosol** extinction (per km, grey), an
  aerosol layer added to the Hillaire 2020 Earth atmosphere's molecules, ozone and background aerosol, from
  0.3.0 on. Until 0.2.0 it was the whole clear-air extinction. Eval's default of 0.03 per km is described there as
  "a ~130 km visual range"; with the molecules and the background aerosol added, the visual range at 1,650 m is about 93 km. PRD §9.3's fitted
  profiles (M2) should fit this aerosol part.
- **`renderer_version`** must equal `RENDERER_VERSION` (`0.3.0`). The CLI refuses otherwise unless
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
| 1 Atmosphere | **Hillaire 2020** (D-013): transmittance, multiple-scattering, sky-view and aerial-perspective lookup tables, rebuilt on the GPU for every frame (the first two only when the atmosphere changes), with a float64 CPU reference of each. The scene's haze is an aerosol layer of this atmosphere. Sky pixels read the sky-view table; the air in front of terrain and slabs reads the aerial-perspective volume. Details below ("Atmosphere"). | The reference Earth atmosphere with a v0 ground albedo of 0.3 (TODO(M2): per viewpoint and season). No refraction (TODO(M2): it delays sunset by about 0.57° of solar depression). The in-scatter below the highest slab shading the camera is dimmed by the expected cloud dimming (v0, like the old haze airlight). |
| 2 Sun, Moon, night sky | Anti-aliased geometric disks seen through the atmosphere's transmittance along each view ray. Direct sunlight and moonlight use the same table, times the fraction of the disk above the local horizon (the Earth's shadow). A night sky of 10^−3.75 cd/m² (airglow and starlight) is added to sky pixels and to the sky light. | Moon brightness ∝ illuminated_fraction³, no phase shape, no stars. The moonlit sky is a uniform v0 term (0.02 cd/m² per lux of horizontal moonlight). TODO(M2): Krisciunas & Schaefer via oculus-core. |
| 3 Cloud slabs | Each layer is a spherical shell [base, base + thickness]. Every view ray is intersected with Earth curvature (cancellation-free quadratic, metre-accurate in float32) and clipped to the ray end. It is then sampled at n stratified points along its in-slab path of length L: one per 1/16 of the layer's octave-0 noise cell, `n = clamp(ceil(L / step), 4, 128)`. At each point the forecast cover is read at that point's great-circle distance; the N(0,1) noise is compared with the normal quantile of that cover (**threshold first**), and only the binary masks are averaged. Opacity α = 1 − exp(−β·(m/n)·L). The colour path stops sampling a slab once its optical depth reaches 7 (α ≥ 0.99909); the coverage debug outputs always sample the whole path. **Unchanged in 0.3.0.** | v0 constants per type (`src/clouds.ts`): cell 2.4 / 3.6 / 7.2 km, β 0.03 / 0.012 / 0.0006 m⁻¹ for low / mid / high. The step was set by a convergence study, and the Monte Carlo below shows it converged. Renderer 0.1.0 used 4 samples for every path, under-drawing a cover-0.1 deck at 0–2° by 44 points. Layers are independent (random overlap); TODO(M1.5) check against HRRR TCDC. Face radiance (light pass): two-stream transmission/reflection of the slab's vertical optical depth, lit by sunlight through the atmosphere to the slab's top (its transmittance there, the Earth's shadow from there), 70 % cosine-weighted above the slab's own horizon and 30 % isotropic, plus the clear-sky light. No shadowing of lower layers by upper ones. Cloud shadows on the ground (pass 4) still take one mask sample per slab on the Sun ray: TODO(M2). |
| Ceiling | A forecast ceiling not inside a declared layer becomes a **solid** 300 m slab (no gaps), consistent with the S_rule baseline. Inside a declared layer, that layer represents it. | TODO(M1.5): replace with the TCDC-checked overlap rule (PRD §9.2 pass 3). |
| 4 Scene light | `plate × (E_direct(P) × T_cloud(P) + E_diffuse) / π`. E_direct is sunlight through the atmosphere **to the terrain point P** (the transmittance table at P's height and local Sun angle, the Earth's shadow from P), on a horizontal surface at P. T_cloud is the realised cloud transmittance from P towards the Sun (cloud shadows). E_diffuse is the camera's expected diffuse light: the **irradiance of the drawn sky-view table** (plus the night sky and moonlight), through the expected cover. | The plate is used as an albedo proxy with no dehazing, normals or DEM horizon shadows: TODO(M2). The diffuse light is the camera's, not P's: TODO(M2). |
| 6 Haze | The scene's `haze.beta_per_km` is an aerosol layer of the atmosphere (grey, single-scattering albedo 0.9, the Mie phase), uniform below 2 km above the ground. Its optical depth along every ray is exact: integrations split their cells where the ray crosses its top. The airlight is the aerial perspective's. | TODO(M2): the per-viewpoint fitted profile (PRD §9.3). The sharp top makes a thin, bright band at sunset (the top of the layer is lit through the least haze). |
| 6 Fog | Visibility V gives β = −ln(ε)/V; only the excess over the **whole clear air at the ground** (molecules, background aerosol and haze, at 550 nm) is fog. Applied only to the ray inside the near-surface layer, with a soft top (4 bands of 25 m, β·e^−(k+½)). Each segment of the ray is split where it enters or leaves the fog; inside, the atmosphere's in-scatter is dimmed by the fog's mean transmittance and the fog's airlight by the atmosphere in front of it. The fog's airlight blends from the horizon sky in the view's azimuth (thin, Koschmieder) to a multiple-scattering airlight 0.85·E/π (dense) with weight 1 − exp(−β·1 km). | **No DEM yet:** the ground under every ray is the spherical shell at the camera's ground height (`elev_m_msl − agl_m`), so the fog fills all heights below ground + 100 m: valleys below the camera are fogged and terrain rising above that height clears. A ceiling inside that layer (a surface-based deck) raises the fog top to the deck top. Cloud slabs confine their own cloud-fog between base and top in world height (pass 3), so terrain above a deck stays clear. TODO(M2): DEM ground height along the ray. |
| Ray end | Sky: the sea surface if the ray meets it, else unbounded. Terrain: the depth map; without one, the flat ground for downward rays, capped at the nominal depth. | Without a sky mask every pixel is terrain (the plate's own sky is relit, not replaced); a mask is required for meaningful sky. Sky-mask pixels below the horizon show only the in-scatter to the sea (no sea surface radiance). |
| 8 Camera | Exposure normalises so that a white diffuser under the camera's expected illuminance maps to 1 (floor 5 lux, below which frames go dark like webcams at night); linear to 0.8 then a soft exponential knee; sRGB. | Under the plate's own lighting the plate is reproduced. A clear zenith at a 22° Sun exposes at 0.086 (sRGB 0.32): physically dimmer than the v0 placeholder sky (about 2,500 cd/m² there). TODO(M2): Kim & Pollefeys model-free response, white balance, infrared monochrome mode. |
| 5, 7 | Not in v0. | TODO(M2): night lights; precipitation extinction and wet darkening (`precip_rate_mm_h` and `precip_type` are validated and hashed only). |

## Atmosphere (Hillaire 2020, D-013)

A port of the reference implementation of S. Hillaire, "A Scalable and Production Ready Sky and Atmosphere
Rendering Technique" (Computer Graphics Forum 39(4), 2020; `sebh/UnrealEngineSkyAtmosphere`, MIT), written
offline from the paper and the reference's structure (its passes, table layouts, parameterisations, the
multiple-scattering series and the energy-conserving step).

- **Licence:** the MIT notice is a legal comment in `src/atmosphere/model.ts`, which every bundle keeps
  (`bundle.test.ts`), and a comment in every atmosphere shader source. Its copyright line ("Copyright (c) 2020
  Epic Games, Inc.") was written offline; the lead checked it against the reference repository's LICENSE on
  23 Nov 2026, and it matches (D-098). The transmittance table's parameterisation is Bruneton's (2008, 2017), as the reference uses it.
- **Code:** `src/atmosphere/model.ts` (the medium, phase functions, the Earth's shadow, the tables'
  parameterisations), `src/cpu/atmosphere.ts` (the float64 reference of every pass and the light model),
  `src/webgl/atmosphere-glsl.ts` (the passes' GLSL), `src/cpu/shade.ts` (the float64 reference of the main pass's
  colour, the 'hdr' output).
- **Model:** the reference's Earth.
  - Rayleigh scattering (5.802, 13.558, 33.1) × 10⁻⁶ m⁻¹ at the ground, scale height 8 km.
  - Mie aerosol: scattering 3.996 × 10⁻⁶ m⁻¹, extinction 4.44 × 10⁻⁶ m⁻¹, scale height 1.2 km, Cornette-Shanks
    phase with g = 0.8.
  - Ozone absorption (0.650, 1.881, 0.085) × 10⁻⁶ m⁻¹ in a tent from 10 to 40 km.
  - Top at 100 km; ground albedo 0.3 for the multiple-scattering bounce.
  - Plus the scene's haze (pass 6 above).
- **Units:** the tables hold radiance per unit solar illuminance; the renderer multiplies by 128,000 lux (a white
  Sun above the atmosphere, as in v0), in linear sRGB.

| Pass | Table (RGBA32F) | Drawn | Holds |
|---|---|---|---|
| Transmittance | 256 × 64 | when the atmosphere changes | Transmittance to the top for every ray above the horizon (Bruneton's mapping). 64 cells per monotone piece, crowded at the ray's lowest point; the haze exactly. |
| Multiple scattering | 32 × 32 | when the atmosphere changes | Ψ_ms = L_2nd / (1 − f_ms) per height and Sun angle, from 8 × 8 directions of isotropic single scattering with a Lambertian ground bounce. Alpha holds the largest f_ms, an energy check (< 1). |
| Sky view | 192 × 108, twice | every frame | In-scatter from the camera in every direction: azimuth from the Sun 0–180° (squared, crowded at the Sun), zenith to horizon to nadir (squared, crowded at the horizon). The second table holds the part scattered below the shade top. |
| Irradiance | 32 × 1 | every frame | The sky-view table integrated over the upper hemisphere (32 × 32 midpoints in sin² zenith and azimuth), one partial sum per azimuth, so no fragment walks 1,024 lookups in series. |
| Light | 12 × 1 | every frame | Direct sunlight and moonlight, sky irradiance, exposure, cloud dimming, fog airlight and the slabs' face radiance (`LIGHT_TEXELS`). |
| Aerial perspective | 32 × 32 × 32, as 1024 × 32 atlases | every frame | In-scatter and transmittance from the camera to 32 logarithmic depths, 0.25 km to 500 km (the reference: 32 km), on a 32 × 32 screen grid. Two passes: each slice's own increment, then their composition along the column. |

**Integration.** Every in-scatter integral uses Hillaire's energy-conserving step: S (1 − e^(−σt·dt)) / σt per cell.
Cells are cut at the ray's physical features:

- its lowest point;
- where it crosses the haze top;
- where it crosses the Earth's shadow (the cylinder of radius R around the Earth–Sun axis, on its night side).

Each piece between features gets 24 cells (sky view) or 20 (multiple scattering). They are crowded towards both
ends of the piece (smoothstep, u²(3 − 2u)), except in the last piece of a rising ray, where they are crowded at
its start only (u²): that piece ends at the top of the atmosphere, where nothing scatters. A piece's start is
where the transmittance from the camera is highest, so in hazy air most of the light comes from there. Its far
end is the densest air (the ray's lowest point or the ground) or the haze top: at a low Sun only the top few tens
of metres of the haze layer are sunlit. The aerial perspective uses 2 uniform cells per slice, split at the same
features. Every integral also splits at the shade top.

Two earlier layouts missed light:

- **The first layout** crowded 32 cells at the camera alone. At sunset half the light comes from the thin sunlit
  top of the haze layer, and in twilight 75–90 % from sunlit air 40–97 km up and hundreds of kilometres away.
- **The pre-review 0.3.0 layout** crowded each piece's cells at its lower end only. On a ray that descends to its
  lowest point, the first cell from the camera then covered 8 % of the distance to it: 10–14 km on horizon rays
  from 800–2,200 m. In haze of 0.1–0.3 per km most of the light comes from that first cell. That layout also left
  the sunlit top of the haze layer to one coarse cell on rising rays at sunset.

**Convergence**, measured 2026-11-23 (`src/cpu/atmosphere.test.ts`). It is the largest relative error, in any
channel, of sky-view texels that carry at least 1 % of the brightest one's luminance. The texels are every 8th
azimuth column and every 2nd row, 1,296 in all. The reference is an independent integral: uniform cells between
knots at the ray's features and at 100 m × 3^k from the camera. At 64 cells per piece it agrees with 512 to
0.02 % on the worst texels. At the golden camera (1,650 m, haze 0.03 per km):

| Sun altitude | 45° | 10° | 2° | 0° | −2° | −4° | −6° | −9° | −12° |
|---|---|---|---|---|---|---|---|---|---|
| First layout (32 cells crowded at the camera; against its own 512-cell integral) | 0.14 % | 0.34 % | 1.26 % | 12.69 % | 2.53 % | 3.31 % | 4.70 % | 6.15 % | 9.28 % |
| Pre-review 0.3.0 (24 cells per piece, crowded at each piece's lower end) | 0.21 % | 0.31 % | 0.52 % | 0.44 % | 2.37 % | 1.53 % | 1.49 % | 2.46 % | 2.40 % |
| 0.3.0 (24 cells per piece, crowded at both ends) | 0.21 % | 0.19 % | 0.25 % | 0.46 % | 2.37 % | 1.53 % | 1.49 % | 0.83 % | 2.37 % |

At the viewpoints' camera heights and other hazes, the largest error with the Sun from 0° to 45° and, after the
slash, from −12° to 0°:

| Camera height | Haze 0.01 per km | 0.03 | 0.1 | 0.3 |
|---|---|---|---|---|
| 150 m, pre-review | 0.45 / 2.64 % | 0.58 / 2.29 % | 6.05 / 1.46 % | 9.23 / 1.10 % |
| 150 m, 0.3.0 | 0.45 / 3.18 % | 0.40 / 2.71 % | 0.62 / 1.62 % | 0.71 / 0.95 % |
| 808 m, pre-review | 0.40 / 2.60 % | 0.66 / 2.17 % | 2.94 / 2.39 % | 2.71 / 5.83 % |
| 808 m, 0.3.0 | 0.40 / 3.09 % | 0.37 / 2.43 % | 1.01 / 1.65 % | 0.97 / 0.77 % |
| 1,650 m, pre-review | 0.37 / 3.25 % | 0.52 / 2.46 % | 1.96 / 4.91 % | 5.96 / 10.51 % |
| 1,650 m, 0.3.0 | 0.37 / 3.23 % | 0.46 / 2.37 % | 0.73 / 2.38 % | 0.71 / 1.49 % |
| 2,150 m, pre-review | 0.49 / 3.01 % | 0.86 / 2.92 % | 4.65 / 6.26 % | 10.45 / 13.14 % |
| 2,150 m, 0.3.0 | 0.35 / 2.99 % | 0.45 / 2.56 % | 1.30 / 2.22 % | 0.65 / 1.27 % |

- **Overall:** within 1.30 % by day and 3.23 % in twilight, down from 10.45 % and 13.14 %. The test holds the golden
  camera to the original thresholds, 1 % and 3 %. It holds the sweep to 1.5 % and 3.5 %, set after this sweep.
- **Higher cameras:** at 4,000 m, outside the viewpoints' range, the largest errors are 1.27 % and 3.85 %. The
  pre-review layout gave 25.5 % and 30.3 % there, in haze of 0.3 per km.
- **What remains:** the twilight errors of up to 3.2 % are on rising rays, 2–48° up, and are what they were before
  the change. For example, 2.37 % at −2° and 48° elevation comes out the same with both layouts. Their light comes
  from sunlit air 40–97 km up, in the last piece of the ray, whose layout did not change.
- **Multiple-scattering table:** Ψ_ms is within 1.35 % of the independent reference at every height (10 m to 50
  km) and Sun from −12° to 45°, for hazes 0.03–1 per km over grounds at 140 m and 1,640 m. The pre-review layout
  missed by up to 11.6 % at 0.3 per km and 16 % at 1 per km (50 km up, Sun −4°). The first layout moved it by up to
  56 % in deep twilight and 8.4 % at the ground at sunset.
- **Darker than −12°:** the sky is below the 5-lux exposure floor's visibility, so convergence is not tested there.
- **The errors are smooth.** Along every row of six azimuth columns (0°, 14°, 29°, 60°, 98° and 180° from the Sun),
  the luminance error changes by at most 1.03 percentage points between adjacent texel rows. That covers Suns at
  2°, 0°, −3°, −6°, −9° and −12°, cameras at 1,650 and 2,150 m, and haze of 0.03 and 0.1 per km; the pre-review
  layout's largest step was 2.17. The ground's horizon, a real edge, is excluded.

**Aerial perspective accuracy**, measured 2026-11-23 (`src/cpu/atmosphere.test.ts`). The test uses the golden
camera (1,650 m, heading 30°, pitch 3°, hfov 60°), haze 0.03 per km and the Sun towards the view. It checks every
2nd froxel column and row, at points up to 12 km high and before the ray's end, against the independent reference.
It reports the in-scatter S's relative error where S is at least 10⁻³ of the largest, and the transmittance T's
absolute error.

| Sun altitude | 22° | 2° | 0° | −2° | −4° |
|---|---|---|---|---|---|
| Stored slice ends, S | 0.02 % | 0.20 % | 5.51 % | 7.31 % | 7.39 % |
| Lookups mid-slice, S: largest (99th percentile) | 6.28 % (6.25 %) | 9.00 % (5.66 %) | 36.6 % (22.8 %) | 18.2 % (9.7 %) | 16.9 % (3.6 %) |
| T, slice ends / mid-slice | 9.2e-5 / 0.022 | 9.2e-5 / 0.022 | 9.2e-5 / 0.022 | 9.2e-5 / 0.022 | 8.8e-5 / 0.022 |

- **By day:** the 6.3 % is in the first slice, 31 m from the camera, where S is tiny. There the lookup interpolates
  linearly in the logarithmic slice coordinate, but S grows linearly with distance.
- **Near sunset:** the errors sit inside the haze layer, below its top. For example, 7.6 km out at 11.7° elevation
  and 450 m below the top, S is up to 37 % too high in its worst channel. The sunlit top of the layer makes S grow
  steeply within a slice.
- **Transmittance:** the 0.022 sits where a slice is cut by the ray's end at the sea-level sphere. A slice that
  holds the haze-top crossing gives 0.019.
- **In the pixels:** a scratch harness replaced the volume's lookups with an exact integral along each pixel's own
  ray, in the CPU shade, then applied the tone curve.
  - The clear and low-deck goldens moved by at most 2 levels (mean 0.03, on 8.7 % and 7.6 % of the pixels), and fog
    by at most 1.
  - At sunset with the Sun towards the view, the clear view moved by at most 3 levels (mean 0.05) and the low deck
    by at most 6 (mean 0.04, on 9.6 % of the pixels).
  - At −2°, the low deck moved by at most 1 level.
- **Changing the volume** (CPU reference, every froxel column): 4 cells per slice brought the stored values to
  1.6–2.5 % near sunset but did not improve the lookups. 64 slices brought the lookups to 13.3 % at 0°, 8.2 % at −2°
  and 3.1 % by day, and T to 0.011. Neither change was made, and their GPU cost was not measured.
  TODO(M2): if eval needs a better sunset, add slices or place slice ends at the haze top.

**The light on the plate is the drawn sky.** E_diffuse is the irradiance of the very sky-view table the sky pixels
read. The test integrates the GPU's own table on the CPU: it matches the light pass to 9.9 × 10⁻⁸ (the clear golden).
Direct sunlight on the plate, on cloud faces and in the Sun's disk all go through the same transmittance table.

**Clouds and the in-scatter.** Light scattered by the air below the top of the highest slab that shades the
camera's ground (the shade top) is dimmed by the expected cloud dimming (expected / clear illuminance at the
camera). This is v0's rule for the haze airlight, now applied to the whole atmosphere:

- the sky-view table keeps that part separately, so the irradiance uses the clear sky;
- the aerial-perspective volume is dimmed as it is drawn.

**Plausibility (CPU tests, camera at 1,650 m, haze 0.03 per km).**

- **Zenith luminance by day:** 1,099 cd/m² at a 22° Sun and 3,092 cd/m² at 60°. The CIE clear sky (turbidity 2.5)
  gives 1,039 and 3,203.
- **Twilight:** 0.98 cd/m² at −6° and 2.4 × 10⁻³ cd/m² at −12°. The test holds them within 4× of about 1 and
  5 × 10⁻³ cd/m² (about 18–19 mag/arcsec² at −12°). Those are the removed v0 placeholder table's literature-typical
  values, not measurements made by this project, and they have not been checked against a published source (the
  port was written offline).
- **Energy:** a non-absorbing atmosphere over a black ground delivers 53–97 % of the top-of-atmosphere flux to the
  ground, never more.
- **Multiple scattering:** the largest f_ms is 0.39.
- **Sunlight:** the transmittance table matches a 4,096-cell integral to 2.1 × 10⁻³ wherever T > 10⁻³.

**Differences from the reference:**

- **The ground:** the ground sphere is mean sea level of the renderer's 6,371 km Earth (the reference uses
  6,360 km).
- **The haze:** the scene's haze is an exact extra aerosol layer.
- **The Earth's shadow:** it is a smooth fraction of the solar disk, not a 0/1 test.
- **Texel mapping:** texel centres map exactly onto the tables' parameter ranges, with exact inverses (the
  reference's lookup mapping is off by up to 1/(n + 1) texel).
- **Cell layout:** the cells are knotted at the ray's features and crowded at both ends of each piece (above).
- **Aerial-perspective range:** it reaches 500 km.
- **Precision:** the GLSL avoids the driver's cos, acos and asin. Measured on SwiftShader over 4,096 points, its
  built-ins are off by up to 1.9 × 10⁻⁴ (cos) and 6.7 × 10⁻⁵ (acos) absolute, which tilted sky-view texels near
  the zenith by 0.07°; its atan is accurate to 1.6 × 10⁻⁷. The GLSL uses a Taylor-series cos, acos and asin through
  atan, and float64 tables for its fixed quadrature angles.

**Requirements:** WebGL2 with `EXT_color_buffer_float` (RGBA32F render targets) and 2 draw buffers. No float
filtering is used: every table is read with `texelFetch` and manual bilinear or trilinear interpolation, which the
CPU reference mirrors exactly. TODO(M2): confirm both in WKWebView.

## Determinism

The same inputs give byte-identical PNGs on the same platform:

- uniforms are derived in float64 by `buildFrame()`;
- the lattice is generated from the seed, and nothing reads the clock;
- inputs are decoded in Node, and outputs are encoded with fixed pngjs settings.

This is tested across two separate browser launches and through the CLI. The atmosphere's tables are
drawn on the GPU every frame, so their determinism is tested too:

- all nine tables read back byte-identical across two browser launches;
- dropping the cached transmittance and multiple-scattering tables (by rendering another haze) and rebuilding them
  gives the same frame, byte for byte (`test/determinism.test.ts`).

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

Three scenes on the synthetic plate (`goldens/scenes/*.json`, renderer 0.3.0, run initialised
2026-11-18T12:00Z, true Sun for each valid time). Renderer 0.3.0 redrew all three: the Hillaire sky changed
every pixel, and the synthetic plate files came out byte-identical. They were redrawn once more for the review's
cell layout (both ends of each piece, above): 79, 169 and 253 of 57,600 pixels (clear, low-deck, fog) moved by one
level, and the plate files again came out byte-identical.

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

Re-measured on the 0.3.0 tree (2026-11-23), they come out the same: 0.3.0 did not touch the cloud masks.

### The atmosphere (`test/atmosphere.test.ts`)

These tolerances were set after an exploratory comparison. It found SwiftShader's cos and acos imprecise (the
GLSL now avoids them; "Atmosphere" above), and with the tolerances fixed the tests were then run.

- **Tables:** every texel and channel of the nine tables and the light pass within 1e-3 relative, plus 1e-6 of the
  table's largest value. That allows for float32 arithmetic over up to about 150 cells, and for metre-level float32
  geometry where a slice ends on the ground.
- **One exception, in the transmittance table** (added 2026-11-23, after a sweep found it):
  - **Why:** a ray whose lowest point lies just below the haze top crosses the haze along a chord of length
    2 √(2 r δ), which float32 cannot place: SwiftShader's ray sits a few centimetres of perigee height away from the
    float64 one.
  - **Rule:** such a texel may differ by more, but it must then lie within the tolerance of the CPU's values over
    the rays within 2 × 10⁻⁷ of its zenith cosine (about 0.2 m of perigee height at μ = −0.15).
  - **Limit:** at most 16 texel-channels per table may need it.
- **Frames** ('hdr', the exposed radiance before the tone curve): at least 99.5 % of pixels within 1 % in every
  channel (plus 1e-4 of the frame's mean), with a mean relative difference of at most 1e-3. A pixel may differ
  more where float32 flips a cloud-mask sample at its threshold.

The tables were measured on SwiftShader in seven conditions:

- the clear golden (Sun 22°);
- the low deck at sunset (0°) and at civil dusk (−6°);
- fog (13°);
- the clear golden's view with the camera at 150 m (haze 0.1 per km), at 808 m (0.3 per km) and at 6,000 m (0.3 per
  km).

Each cell gives the largest relative difference over the texels within the plain tolerance (counted where a value
exceeds 10⁻⁶ of the table's largest), first at the golden camera, then at the three other cameras:

| Table | Golden camera (4 conditions) | 150 m, 808 m and 6,000 m |
|---|---|---|
| Transmittance | 5.1e-4 (near the horizon, on values near 1e-3) | 6.0e-4 to 1.9e-3. Anything above 1e-3 is on values of 1.1e-3 or less, inside the 1e-6 term. 2 and 4 texel-channels were conditioned (up to 7.1e-3 and 4.8e-2). |
| Multiple scattering | 9.3e-6 | 9.1e-5 |
| Sky view (both) | 1.8e-4 | 8.4e-5 |
| Light pass | 3.2e-6 | 3.2e-6 |
| Aerial perspective, composed | 1.1e-5 (in-scatter), 8.0e-6 (transmittance) | 9.9e-6, 6.6e-6 |
| Aerial perspective, increments | 3.2e-3 (in-scatter), 9.1e-6 (transmittance) | 2.9e-4, 6.6e-5 |

The increments' 3.2e-3 sits on values 1.3–5.9 × 10⁻⁵ of the table's largest, well inside the tolerance's 1e-6 term.

**A wider sweep** (a scratch harness, the same comparison) covered 13 camera heights from 5 to 8,000 m and haze of
0, 0.03 and 0.3 per km.

- **Outside the plain tolerance:** 21 texel-channels in 8 of the 39 configurations, all in the transmittance table,
  every one on a ray whose perigee lay 0.1–5 m below the haze top. All 21 lie within the CPU's range over the
  zenith-cosine slack.
- **Everywhere else:** within tolerance, with largest relative differences of 4.1e-4 (sky view), 9.1e-5 (multiple
  scattering), 1.1e-5 (composed aerial perspective) and 3.1e-6 (light pass).

**Frames,** against the CPU shade (`src/cpu/shade.ts`):

| Scene | Pixels within 1 % | Mean relative difference |
|---|---|---|
| The three goldens (plate, mask, depth; deck, cirrus, fog) | 99.998–100 % | 3.6e-7 to 8.0e-6 |
| An all-sky view towards the Sun at sunset | 99.993 % | 3.5e-6 |
| Terrain and a deck at civil dusk | 100 % | 1.1e-5 |

**Other checks:**

- The light pass's sky irradiance equals the integral of the GPU's own sky-view table to 9.9e-8 (the clear golden).
- Mirrored pixels about the Sun's vertical plane agree to 1.0e-5, and mirrored froxel columns to 7.3e-6.

## Coverage Monte Carlo (PRD §9.5, the M1.5 gate)

`src/cpu/montecarlo.ts`, `pnpm montecarlo --case above|deck|thin|cirrus`. The tables are in
`$SNOW_GLOBE_DATA_DIR/reports/renderer/montecarlo/`, copied on 23 Nov from `cache/renderer/montecarlo/`, where the
script writes them (`cache/` is deletable; `reports/` is kept):

- from 2026-11-19 (renderer 0.2.0);
- for renderer 0.3.0 (ticket M1.5-05), `mc-*-2026-11-23-r0.3.0.json` and the final tree's
  `mc-*-2026-11-23-r0.3.0-final.json`.

Every run now records its provenance (`provenance` in the JSON):

- the renderer version;
- the git commit, and whether the renderer's files differed from it;
- the time and the load average;
- the SHA-256 of the 12 files the Monte Carlo's computation runs (`src/cpu/montecarlo.ts` and its imports).

**The gate result carries over from 0.2.0 by construction.** Those 12 files are byte-identical to 0.2.0's (commit
479d4d8), so the computation is 0.2.0's. The final tree's run reproduces the earlier 0.3.0 tables exactly, apart
from the run time.

What ties the gate to what 0.3.0 draws is `test/gpu-cpu.test.ts`, re-run on the 0.3.0 tree: κ identical on
99.986–100 % of pixels and α within 2e-3 on 99.997–100 % (above).

The worst error by elevation band, over the nine covers:

| Case | 0–2° | 2–5° | 5–10° | 10–20° | 20–45° | 45–90° |
|---|---|---|---|---|---|---|
| Thick low deck above (fraction / opacity, pts) | 0.48 / 0.35 | 0.90 / 0.65 | 1.08 / 0.76 | 1.32 / 0.93 | 1.29 / 0.89 | 0.65 / 0.45 |
| Thin mid layer | 0.43 / 0.74 | 0.23 / 0.65 | 0.80 / 0.17 | 0.13 / 0.02 | 0 / 0 | 0 / 0 |
| Cirrus | 0.52 / 0.08 | 0.60 / 0.08 | 0.63 / 0.10 | 0.98 / 0.09 | 0.87 / 0.07 | 0.18 / 0.03 |

The deck below the camera covers −1° to −2°, −2° to −5°, −5° to −10°, −10° to −20°, −20° to −45° and −45° to −90°.
Its errors by band are 1.01 / 0.76, 0.97 / 0.69, 1.29 / 0.91, 1.54 / 1.08, 1.44 / 0.98 and 0.58 / 0.36 pts.

On the thin layer the steep bands' drawn and target paths both take the 4-sample minimum at the same points, so
they agree exactly.

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

## Benchmarks

### Renderer 0.3.0 against 0.2.0 (this MacBook Air M4, 2026-11-23)

**Method.** A scratch harness ran over a frozen copy of 0.2.0 and over the final 0.3.0 tree, alternating before and
after, each run in its own browser.

- **The 0.2.0 copy:** commit 479d4d8's renderer. Its only change is that its GPU launch falls back to the headless
  shell, as 0.3.0's does.
- **Method:** it matches `pnpm bench`: page-side `render()` plus the 1-pixel barrier. Steady frames move the valid
  time by 1 min and the Sun with it, after 2 warm-ups: 30 steady frames on SwiftShader, 100 on Metal.
- **Scenes:** the three goldens at 320 × 180 (SwiftShader only), and `pnpm bench`'s clear and heavy scenes (three
  slabs, fog, Sun shadows) at 720p and 1080p.

**Raw data:** `$SNOW_GLOBE_DATA_DIR/reports/renderer/bench-sky-2026-11-23/fix/` (copied from `cache/renderer/`) holds every run's JSON, with the 1, 5
and 15 minute load averages before and after it:

- `ss-<k>-before|after.json` and `metal-<k>-before|after.json`;
- the per-pass profiles `profile-swiftshader.json`, `profile-metal.json` and `profile-metal-2.json`.

`pnpm bench` now records the git state and the load averages too. The machine is fanless, so times drift between
runs: compare within an alternation.

**SwiftShader**: 4 pairs, with a 1-minute load of 4.8–17.7 at the start of the runs, mostly these runs' own threads.
Steady p50 in ms:

| Scene | 0.2.0 | 0.3.0 | Per-pair ratio (median) |
|---|---|---|---|
| golden clear, 320 × 180 | 3.3–4.8 | 45.3–50.4 | 9.4–14.4× (13.3×) |
| golden low-deck | 11.8–14.9 | 55.9–71.8 | 3.8–6.1× (5.1×) |
| golden fog | 6.7–7.6 | 51.2–64.4 | 7.4–8.9× (8.5×) |
| clear 1280 × 720 | 51.6–63.0 | 141.7–183.2 | 2.75–3.26× (2.89×) |
| heavy 1280 × 720 | 162.6–214.8 | 272.6–345.1 | 1.59–1.84× (1.64×) |
| clear 1920 × 1080 | 124.7–168.0 | 270.6–340.7 | 2.03–2.32× (2.19×) |
| heavy 1920 × 1080 | 385.5–479.2 | 589.4–676.3 | 1.36–1.53× (1.45×) |
| First frame (compile and JIT), ms | 564–1,305 | 2,889–3,109 | +1.80–2.45 s (+2.38 s) |

The pre-review tree's 9 pairs spread wider. They are `ab-<k>-before|after.json` in the parent folder, with no load
recorded, and the review's layout keeps the same cell count:

- per-pair p50 ratios of 0.94–6.83× at 720p and 1080p (medians 1.52–3.31×);
- 2.6–43× on the goldens;
- a first frame 1.77–5.23 s longer (median 2.55 s).

**Where SwiftShader's time goes** (`profile-swiftshader.json`; 1080p, each pass to a readPixels barrier, medians,
load 9.8–10.9):

- **The atmosphere's passes:** about 36–41 ms a frame. That is the sky view at 31.5–36, the aerial perspective at
  4.4–4.8 (increments 3.7–4.0, composition 0.7–0.8), and the irradiance and light at 0.3–0.4. When the haze
  changes, add the transmittance table (13.8–13.9 ms) and the multiple-scattering table (76–77 ms).
- **The main pass:** 218 ms (clear) and 706 ms (heavy), the render with barriers minus the atmosphere's passes. It
  reads the sky-view, aerial-perspective and transmittance tables with manual bilinear and trilinear filtering
  through `texelFetch`, which SwiftShader runs on the CPU.
- **Effect on eval:** a render through the CLI is one process with one first frame. That frame cost 1.8–2.45 s more
  in these pairs, plus the per-frame cost above. Reusing one runner (`renderToFiles({ runner })`) avoids the
  repeated compiles: in the same runner, the next goldens' first frames took 134–213 ms.

**Chromium ANGLE/Metal on the Apple M4**: the headless shell with the Metal flags ("ANGLE Metal Renderer: Apple
M4"), since full Chromium is not installed here. 6 alternations of 100 steady frames, with a 1-minute load of
3.1–4.1 at the start of every run:

| Scene | 0.2.0 p50 / p95 / min | 0.3.0 p50 / p95 / min | Per-pair p50 ratio (median) |
|---|---|---|---|
| clear 1280 × 720 | 1.4–2.4 / 2.6–2.7 / 1.0–1.1 ms | 2.0–2.1 / 2.8–5.4 / 1.7–1.8 ms | 0.83–1.50× (1.17×) |
| heavy 1280 × 720 | 2.8–3.0 / 3.7–4.0 / 1.8–2.0 ms | 3.7–3.8 / 4.5–4.7 / 2.7–2.9 ms | 1.27–1.32× (1.28×) |
| clear 1920 × 1080 | 4.1–4.4 / 4.6–4.9 / 1.3–1.6 ms | 3.2–3.3 / 6.8–9.4 / 2.7–2.8 ms | 0.74–0.78× (0.75×) |
| heavy 1920 × 1080 | 5.4–5.7 / 6.8–8.0 / 3.2–3.4 ms | 6.7–7.0 / 8.3–8.7 / 4.7–5.1 ms | 1.19–1.24× (1.24×) |

- **The GPU work** shows in the fastest frames. Per pair, the atmosphere adds 0.6–1.0 ms at 720p and 1.1–1.7 ms at
  1080p.
- **Heavy 1080p:** the p50s do not overlap in any alternation (5.4–5.7 ms before, 6.7–7.0 after).
- **Clear 1080p:** the p50 is lower on 0.3.0 although its fastest frames are slower. On 0.2.0 the typical clear frame
  takes about 3× the fastest one (p50 4.1–4.4 ms against a minimum of 1.3–1.6 ms); on 0.3.0, about 1.2× (3.2–3.3
  against 2.7–2.8). The cause was not investigated.
- **First frame:** 478 ms on the first launch after the shaders changed (compile), then 54–74 ms on later launches
  (0.2.0: 22–57 ms). It includes the uploads and the first build of the transmittance and multiple-scattering
  tables.

**Earlier saved Metal runs** of the pre-review tree are `bench-gpu-{1,2,3}-{before,after}.json` in the parent folder:
`pnpm bench`, 60 frames, 03:39–03:40 UTC, no load recorded.

- **Heavy 1080p:** p50 5.8–6.1 ms before, and 7.9, 12.2 and 13.8 ms after; minimum 3.3–3.4 → 5.0–6.4 ms.
- **Clear 1080p:** p50 3.3–4.3 → 4.4–5.2 ms.
- **Cold first frame** after the shader change: 877.5 ms.

So the same scene in the same harness can run at about twice the p50 from one session to the next. The Metal tables
quoted here before (100 frames, 4 + 3 alternations) had no saved output and are dropped.

**Metal per-pass profile** (`profile-metal.json` and `profile-metal-2.json`; 1080p, each pass to a readPixels
barrier, medians, load 9–10):

- **Every frame:** sky view 0.8, irradiance 0.3–0.7, light 0.3–0.7, aerial-perspective increments 0.3–0.7 and
  composition 0.4–0.7 ms.
- **When the haze changes:** transmittance 0.4–0.5 ms and multiple scattering 3.9 ms. An earlier, unsaved profile
  gave 14–16 ms for multiple scattering, which these runs do not reproduce. Each of its 1,024 fragments integrates
  all 64 directions in series. TODO(M2): if WKWebView shows it latency-bound, split the directions across
  fragments, as the reference's compute shader does.
- **The main pass:** 3.1–3.5 ms (clear) and 7.9–8.0 ms (heavy), the render with barriers minus the atmosphere's
  passes.

**Against the PRD targets** (≤ 8 ms a frame at 1080p steady state, ≤ 50 ms of GPU time for the first frame; both
defined for WKWebView and measured in M2):

- **Steady state:** the heavy 1080p scene is at the budget on Chromium/Metal.
  - In the quiet alternations its p50 is 6.7–7.0 ms, under 8 ms, and its p95 is 8.3–8.7 ms, over it.
  - In the earlier saved runs its p50 was over 8 ms in 2 of 3 (12.2 and 13.8 ms).
- **First frame:** 54–74 ms of page-side wall time on later launches. That is not GPU time alone, which the PRD
  target counts. Right after a shader change it is 478 ms.
- **Bottleneck:** as in 0.2.0, the cloud-mask sampling: the main pass is 7.9–8.0 ms of the heavy profile's 11.2–11.5
  ms with barriers. The atmosphere adds 1.1–1.7 ms of GPU work at 1080p.

### Renderer 0.2.0 (`pnpm bench`, 2026-11-19)

Each time is page-side `render()` plus the 1-pixel barrier, as p50 / p95 over 30 or 60 frames after 2
warm-ups. Each steady frame moves the valid time by 1 min and the Sun with it. The "heavy" scene has three
slabs, fog and Sun shadows.

The machine is fanless, and repeated runs vary a lot: the clear scene measured SwiftShader 720p p50 70.3 ms and
then 49.6 ms three minutes later. Raw JSON is in `$SNOW_GLOBE_DATA_DIR/reports/renderer/bench-2026-11-19T11-*.json` (copied from `cache/renderer/`).

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
- **Bottleneck:** cloud-mask sampling on long near-horizon paths, up to 128 samples × 4 octaves × 4
  texel fetches per slab. Cirrus (β 0.0006 m⁻¹) seldom turns opaque, so its paths are never cut short.

**First frame:** SwiftShader, including shader compile and JIT: 594 ms. Metal, right after the shader changed:
240 ms; on later launches 22–25 ms, presumably the OS keeping the compiled program (not verified).

**Readback**, measured in the page:

- `readPixels` + row flip: 1.1–3.8 ms.
- base64 encoding of the pixels for the trip to Node: 50–64 ms at 720p, 111–149 ms at 1080p. The transfer
  itself is not timed.
