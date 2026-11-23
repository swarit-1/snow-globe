/**
 * Renderer version (PRD §9.2: render(scene, target) is pure in (scene, renderer version)).
 * Bump it whenever any pixel could change; frozen SceneSpecs record the version they were issued for.
 *
 * 0.3.0 (2026-11-23): the Hillaire 2020 atmosphere (D-013, PLAN M1.5) replaces the v0 placeholder sky:
 * transmittance, multiple-scattering, sky-view and aerial-perspective lookup tables built on the GPU
 * each frame (with a float64 CPU reference), the Sun and Moon disks through the atmosphere, and the
 * plate lit by the same atmosphere (direct sunlight to each terrain point, the sky-view table's
 * irradiance). The scene's haze is now an aerosol layer of that atmosphere, and the fog is the
 * visibility's excess over the whole clear air. Every pixel changed; the cloud masks did not.
 *
 * 0.2.0 (2026-11-19): clouds advect from the forecast run's initialisation (run_init_utc) instead of the
 * Unix epoch; the cloud-mask sample count follows the path length; SceneSpec gains run_init_utc, plate
 * identity, wind_series, precip_type, rh_pct and pbl_height_m_agl; the synthetic test world was re-posed.
 */
export const RENDERER_VERSION = '0.3.0';

/**
 * Milestone label: renderer v0 of PLAN M1.5 (the Hillaire 2020 sky, cloud slabs, a ceiling slab, flat
 * fog and camera v0). Open within v0: TODO(M1.5) the ceiling's overlap rule checked against HRRR TCDC.
 */
export const RENDERER_STAGE = 'v0';
