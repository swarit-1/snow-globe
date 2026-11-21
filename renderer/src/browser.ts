/**
 * Browser bundle entry (esbuild): the renderer, the scene contract and the CPU reference, with no Node
 * imports. The headless runner loads the IIFE build, which sets globalThis.SnowGlobeRenderer.
 */
export {
  SnowGlobeRenderer,
  render,
  flipRows,
  checkInputHashes,
  type GlInfo,
  type ImageInput,
  type RenderInfo,
  type RenderInputs,
  type RenderOptions,
} from './webgl/renderer.ts';
export { DEBUG_MODES, type DebugMode } from './webgl/shader.ts';
export {
  validateScene,
  sceneHash,
  canonicalJson,
  deriveSeed,
  leadSeconds,
  sceneAdvection,
  validTimeSeconds,
  SceneValidationError,
  PRECIP_TYPES,
  type PlateSpec,
  type PrecipType,
  type SceneSpec,
  type ValidateOptions,
  type WindKnot,
} from './scene.ts';
export { buildFrame, type FrameParams } from './frame.ts';
export { solarPosition, angularSeparationDeg, SUN_CHECK_TOLERANCE_DEG } from './ephemeris/sun.ts';
export { advectionDisplacement, windAt } from './math/advection.ts';
export { parseUtcSeconds } from './time.ts';
export { RENDERER_VERSION, RENDERER_STAGE } from './version.ts';
