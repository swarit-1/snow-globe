/**
 * Renderer version (PRD §9.2: render(scene, target) is pure in (scene, renderer version)).
 * Bump it whenever any pixel could change; frozen SceneSpecs record the version they were issued for.
 *
 * 0.2.0 (2026-11-19): clouds advect from the forecast run's initialisation (run_init_utc) instead of the
 * Unix epoch; the cloud-mask sample count follows the path length; SceneSpec gains run_init_utc, plate
 * identity, wind_series, precip_type, rh_pct and pbl_height_m_agl; the synthetic test world was re-posed.
 */
export const RENDERER_VERSION = '0.2.0';

/**
 * Milestone label. v0 serves the M0.5 backtest pair ("v-1"). It is not yet the full M1.5 renderer v0:
 * the Hillaire 2020 sky (D-013) is still TODO(M1.5).
 */
export const RENDERER_STAGE = 'v0';
