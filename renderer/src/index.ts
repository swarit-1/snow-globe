/**
 * Library entry for Node and bundlers: the scene contract, the math, the CPU reference and the WebGL
 * renderer. Node-only helpers (headless runner, PNG I/O) live in src/node/ and are imported directly.
 */
export * from './browser.ts';
export { cameraBasis, pixelRay, projectDirection, altAzToDirection, directionToAltAz } from './math/camera.ts';
export { sphereInside, slabIntervals, slabPathLength, pointHeight } from './math/shell.ts';
export { rayPointGroundDistance, horizonDip, haversineDistance } from './math/geodesy.ts';
export { coverAt, type CoverKnot } from './math/cover.ts';
export { betaFromVisibility, transmittance, koschmieder, KOSCHMIEDER_CONSTANT } from './math/extinction.ts';
export { coverThreshold, cloudMask, thresholdThenFilter, randomOverlapCover } from './math/coverage.ts';
export { cpuCoverage, rayLayerCoverage, encodeDepth, decodeDepth, type RawImage } from './cpu/reference.ts';
export { atmosphereParams, atmosphereKey, type AtmosphereParams } from './atmosphere/model.ts';
export {
  computeBaseLuts,
  computeFrameAtmosphere,
  lightFromAtmosphere,
  type AtmosphereLight,
  type FrameAtmosphereLuts,
  type LightInputs,
} from './cpu/atmosphere.ts';
export { cpuShade } from './cpu/shade.ts';
export { runCoverageMonteCarlo, formatMonteCarloReport, DEFAULT_MC_CONFIG, DECK_BELOW_MC_CONFIG } from './cpu/montecarlo.ts';
export { CLOUD_STEP_CELL_FRACTION, CLOUD_MIN_SAMPLES, CLOUD_MAX_SAMPLES, CLOUD_TAU_OPAQUE, cloudSampleCount } from './math/constants.ts';
