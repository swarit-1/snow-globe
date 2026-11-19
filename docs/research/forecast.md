# Forecast data, fog guidance, ground truth and sky models — 18 Nov 2026

Desk research by a research agent plus a handful of live keyless calls on 2026-11-19 03:00–03:31 UTC (evening of 18 Nov Pacific). "Verified" = the agent called the endpoint. No signups, no keys. Feeds DECISIONS.md D-008 and the Tier 1 sky choice.

## Three findings that change the plan

1. **Open-Meteo marks both HRRR models "Discontinued 2026-11-30"** and points users to RRFS ([gfs-api docs](https://open-meteo.com/en/docs/gfs-api)). NOAA itself keeps HRRR until a later RRFS upgrade ([NWS RRFS webinar Q&A](https://www.weather.gov/media/wrn/calendar/RRFS_REF-Webinar%26RegistrationFormQANotes.pdf)); HRRR was still publishing to AWS (verified). So `ncep_hrrr_conus` on Open-Meteo stops in ~12 days; HRRR data does not.
2. **RRFS replaced NAM, operational since 2026-11-03** ([SCN 26-48](https://www.weather.gov/media/notification/pdf_2026/scn26-048_Updated_RRFS_and_REFS_Implementation.aaf.pdf)). Its off-hour cycles publish only `2dfld.subh` files, which have VIS and cloud base/ceiling but **no low/mid/high cloud cover** (verified, 23Z index). Open-Meteo's hourly `ncep_rrfs_conus` updates every 6 h, and its 18Z run appeared ~6 h after start.
3. **Open-Meteo forecast responses contain no model-run time** (verified).

## 1. Open-Meteo

- Hourly fields verified at the Golden Gate: `cloud_cover`, `cloud_cover_low/mid/high`, `visibility` (m), `precipitation`, `precipitation_probability`, `weather_code`, `dew_point_2m`, `relative_humidity_2m`, `boundary_layer_height`, `freezing_level_height`. **No cloud-base or ceiling variable.**
- Model ids: `ncep_hrrr_conus`, `ncep_hrrr_conus_15min`, `ncep_rrfs_conus`, `ncep_rrfs_conus_15min`, `ncep_rrfs_north_america`, `ncep_nbm_conus`, `ncep_gfs_seamless`, `ncep_gfs_global`. Pinning works; several models return suffixed keys (`visibility_ncep_hrrr_conus`); legacy `gfs_hrrr` still works. `best_match` and `ncep_gfs_seamless` returned HRRR's values for the next hours. NBM: visibility yes, cloud layers and BL height "undefined". RRFS-15min: visibility yes, cloud cover null.
- **Visibility provenance:** HRRR VIS is a post-processing diagnostic (NOAA-EMC/UPP `CALVIS_GSD.f`): the minimum of a humidity-based clear-air visibility and a hydrometeor visibility from cloud water/precipitation in the lowest ~100 m, raised when low-level shear is 4–6 m/s. **Fog appears only if the model puts cloud water within ~100 m of the ground; stratus at bridge-tower height reads as good visibility.** Hourly values are top-of-hour snapshots.
- **Grid snapping:** the Golden Gate point snapped to a sea cell (37.8268, −122.5061, elevation 0). Store the returned cell.
- **Run time:** `https://api.open-meteo.com/data/{model}/static/meta.json` gives `last_run_initialisation_time`, `last_run_availability_time`, `update_interval_seconds` (verified). Observed: HRRR 01Z → available 02:27Z; NBM 02Z → 02:48Z; RRFS-15min 00Z → 02:32Z; RRFS CONUS 18Z → 23:59Z.
- **Previous Runs API:** whole-day offsets only (`_previous_day1…7`), most models from 2024-01; `visibility_previous_day1` was null for HRRR, NBM, GFS in a live test. **Cannot give 1 h or 6 h leads.**
- **Historical Forecast API:** stitches the first hours of each run (HRRR from 2018-01-01, GFS from 2021-03-23, NBM from 2024-10-08), includes visibility and cloud layers. **Near-analysis, not forecast-as-issued.** Useful for the oracle-input render.
- **Single Runs API** (`single-runs-api.open-meteo.com`, `run=` start time; archive from 2026-04-02): returned the HRRR 2026-11-18T12:00 run with visibility and layers (verified). At 03:31Z the 21Z and 00Z runs existed but not 23Z/01Z → lags by hours and may not keep every hourly cycle (unverified).
- **Free tier** ([terms](https://open-meteo.com/en/terms), [pricing](https://open-meteo.com/en/pricing)): non-commercial (no subscriptions, no ads); 600/min, 5,000/h, 10,000/day, 300k/month; > 10 variables counts as a fraction more than one call. Previous Runs, Historical Forecast and Single Runs included. **Data CC-BY 4.0** (attribution required). Shared hosts may hit limits sooner. "Portfolio" isn't named, but an ad-free one matches the non-commercial examples; our volume is hundreds of calls/day.

## 2. HRRR direct (NOAA open data)

- AWS bucket `noaa-hrrr-bdp-pds` (archive since 2014) ([registry](https://registry.opendata.aws/noaa-hrrr-pds)).
- `wrfsfc` fields (verified, 11-18 12Z f06): `VIS:surface`, `HGT:cloud ceiling`, `HGT:cloud base`, `HGT:cloud top`, `LCDC/MCDC/HCDC`, `TCDC:boundary layer cloud layer`, `DPT/RH:2 m`, `HPBL`, `HGT:0C isotherm`, `PRATE`, `APCP`, `CRAIN/CSNOW`.
- Hourly runs; 00/06/12/18Z to 48 h, others to 18 h (verified 12Z → 48 h, 13Z → 18 h). On S3: f00 ~52 min after run start, f18 ~85 min, f48 ~108 min.
- Fetch a few grid points cheaply with byte-range GETs using `.idx` files, via **Herbie** (`herbie-data` 2026.10.0, MIT).
- RRFS direct: NOMADS `rrfs/prod/rrfs.YYYYMMDD/CC/` (verified; 18Z f000 at 19:12Z, f084 at 20:53Z). `noaa-rrfs-pds` on AWS has prototype/retro only; `noaa-rrfs-ops` → NoSuchBucket.
- **Verdict:** primary source. It is the only source with ceiling and cloud base, it outlives Open-Meteo's HRRR cutoff, and its provenance is exact. Cost: one small Python worker.

## 3. Fog skill and guidance

- **No published HRRR visibility verification for SF coastal advection fog was found.** Closest: a 2025 *Wind Energy Science* study off Morro Bay/Humboldt found HRRR warm-season stratocumulus tops ~150 m too low with a weak diurnal cycle ([WES 10, 2755](https://wes.copernicus.org/articles/10/2755/2025/)). SFO historically needed a purpose-built Marine Stratus Forecast System (MIT Lincoln Lab ATC-319, 2006). **Expect raw model visibility to be weak; ceiling, cloud base and low cloud are the main fog signals.**
- **NBM v5.0** (effective 2026-04-30, SCN 26-24): AWS `noaa-nbm-grib2-pds` `core` files (verified, 12Z f006) carry visibility + P(vis < 1, 2, 3, 5 mi) and ceiling + P(ceiling < 500, 1000, 2000, 3000, 6600 ft) and cloud base. Station text bulletin `text/blend_nbhtx.tHHz` (28.5 MB, ~73 min after run) covers hours 1–25 for KSFO, KOAK, KHAF, KSQL, KMRY with CIG, LCB, VIS and category probabilities. Element key from MDL v4.x text cards ([textcard v4.2](https://vlab.noaa.gov/web/mdl/nbm-textcard-v4.2)); no v5 key found. Open-Meteo doesn't expose these probabilities.
- **LAMP v2.7:** NOMADS `lmp/prod/lmp.tHHMMz.lavtxt.ascii` (verified, 0230Z) gives KSFO hourly to 25 h: CIG, VIS categories, conditional CCG/CVS, and obstruction OBV (BR/FG/HZ). **Best free baseline for 1 h leads.** A 2.5 km gridded GLMP exists.
- **GOES Fog/Low Stratus (ABI-L2-GFLS)** is distributed via AWIPS and NCEI/CLASS but **not** in the `noaa-goes18` AWS bucket; no keyless real-time access confirmed.

## 4. Ground truth besides the webcam

- **METAR:** `https://aviationweather.gov/api/data/metar?ids=KSFO,KOAK,KHAF&format=json&hours=1` (verified). Fields: `obsTime`, `visib` (string, e.g. "10+"), `clouds[{cover, base}]` (ft), `vertVis`, `wxString`, `fltCat`, `rawOb`. 100 req/min, custom User-Agent, 30 days of history; bulk cache refreshed every minute ([API](https://aviationweather.gov/data/api/)). KHAF's automated 0315Z report lacked visibility and sky. Older history: Iowa Environmental Mesonet `asos.py` (verified for KSFO, Aug 2026).
- **NOAA CO-OPS visibility sensors** (6-min): Pier 17 (9414296) and Oakland Berth 38 (9414797) via `api.tidesandcurrents.noaa.gov` `datagetter?product=visibility` (verified; both returned "10.0", unit not confirmed). No sensor found at the Golden Gate itself.
- **GOES-18 on AWS (`noaa-goes18`):** `ABI-L2-CMIPC` 5-min CONUS imagery (C02 0.5 km, ~66 MB/file; C13 − C07 for night fog); `ABI-L2-ACMC` clear-sky mask (~4 MB, ~4 min after scan); `ABI-L2-CCLC` cloud cover layers; `ABI-L2-ACHAC` cloud-top height. CIMSS reports a "barcode" striping artifact in GOES-18 Band 7.
- The airports are 15–20 km from the bridge, so the satellite pixel over the strait is the key spatial truth.

## 5. Daytime analytic sky models for WebGL2

| Model | Implementation | Licence | Status | Notes |
|---|---|---|---|---|
| **Hosek–Wilkie** | `andrewwillmott/sun-sky` (C++ + bgfx shader) | Unlicense (reference C code BSD-3) | last push 2026-04-01 | Includes Preetham, Hosek, CIE; 64×2 table for GPU evaluation; night/twilight transitions; an "overcast" blend factor. Needs a GLSL port. `benanders/Hosek-Wilkie` has no licence. |
| Preetham | three.js `examples/jsm/objects/Sky.js` | MIT | r187 2026-10-30 | r188 (dev) deprecates `turbidity` for `mieCoefficient` → pin r187 |
| Bruneton | reference repo (C++/OpenGL); `@takram/three-atmosphere` from `takram-design-engineering/three-geospatial` | BSD-3 / MIT | takram pushed 2026-05-27 | Runtime LUTs and an aerial-perspective effect in WebGL |
| Hillaire 2020 | `sebh/UnrealEngineSkyAtmosphere` (DirectX); Babylon.js atmosphere add-on (GLSL/WGSL); `webgpu-sky-atmosphere` | MIT / Apache-2.0 / MIT | 2022 / tied to Babylon / WebGPU-only | — |

## Recommendations (agent's)

- **Forecast primary:** HRRR `wrfsfc` from AWS via Herbie: `VIS:surface`, `HGT:cloud ceiling`, `HGT:cloud base`, `LCDC:low cloud layer`, `MCDC:middle cloud layer`, `HCDC:high cloud layer`, `TCDC:boundary layer cloud layer`, `APCP`, `PRATE`, `DPT/RH:2 m`, `HPBL`.
- **Convenience/fallback:** Open-Meteo `models=ncep_hrrr_conus` until 2026-11-30; after that `ncep_rrfs_conus` (6-hourly, has layers) + `ncep_rrfs_conus_15min` (visibility) + `ncep_nbm_conus`, with `hourly=cloud_cover_low,cloud_cover_mid,cloud_cover_high,cloud_cover,visibility,precipitation,weather_code,dew_point_2m,relative_humidity_2m,boundary_layer_height,freezing_level_height`.
- **Fog supplement:** NBM `core` visibility/ceiling probabilities and the NBH bulletin for KSFO/KOAK/KHAF; LAMP `lavtxt` (OBV = FG/BR) for 1 h leads; RRFS (VIS, CEIL) from NOMADS as a second model.
- **Issue time, per prediction:** source, model id, run start, availability time, fetch time, valid time, actual lead (valid − run start), nominal lead bucket, grid cell lat/lon/elevation, URL or byte range. Direct GRIB: run start from the path and GRIB `d=`; availability from S3 LastModified. Open-Meteo: pin one model (never `best_match`/seamless); read `meta.json` before and after the data call and accept only if `last_run_initialisation_time` is unchanged; re-check with Single Runs. **24 h leads with HRRR come from 00/06/12/18Z runs, so actual leads are 24–29 h.** Backfill by choosing the run whose *availability* time precedes the simulated issue time (HRRR lag 52–108 min).
- **Fog truth:** combine votes from METAR (KSFO + KOAK/KHAF where reported: visibility, ceiling, FG/BR, `fltCat`), CO-OPS Pier 17 visibility, and GOES-18 strait/headland pixels (ACMC mask, C02 by day, C13 − C07 at night); the webcam is a fourth vote; disagreements are flagged.
- **Tier 1 sky:** Hosek–Wilkie ported from `andrewwillmott/sun-sky` (Unlicense): turbidity and Sun position map directly, the 64×2 table fits a WebGL2 texture, it handles twilight, and its overcast factor can take high/mid cloud. Fallback: three.js `Sky.js` (Preetham, MIT) pinned at r187. `@takram/three-atmosphere` (Bruneton, MIT) is the option for physically based aerial perspective tied to visibility.
