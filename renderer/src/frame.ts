/**
 * Derived per-frame parameters: everything the shaders receive as uniforms, computed once on the CPU in
 * float64 from a validated SceneSpec. The CPU reference (cpu/reference.ts, cpu/atmosphere.ts,
 * cpu/shade.ts) reads the same object, so the GPU and the reference share one definition of layers,
 * noise transforms, the atmosphere, fog and the light model's inputs.
 *
 * Since renderer 0.3.0 the light itself (direct sunlight, sky irradiance, exposure, cloud-face radiance,
 * fog airlight) is not computed here: it comes from the Hillaire 2020 atmosphere's lookup tables, on the
 * GPU in the light pass and on the CPU in cpu/atmosphere.ts (lightFromAtmosphere), from the inputs
 * gathered in FrameParams.light.
 *
 * PRD sections: §9.2 passes 1-4, 6 and 8; §9.3 haze, fog and ceiling; §9.5 seed and advection (the
 * field drifts by the wind's time integral since the run's initialisation, math/advection.ts).
 */
import { cameraBasis, altAzToDirection, type CameraBasis } from './math/camera.ts';
import {
  CEILING_SLAB_THICKNESS_M,
  CLOUD_STEP_CELL_FRACTION,
  DEFAULT_CAMERA_AGL_M,
  DEFAULT_NOMINAL_DEPTH_M,
  DEFAULT_VISIBILITY_EPSILON,
  FOG_DECAY_SCALE_M,
  HAZE_LAYER_DEPTH_M,
  MAX_LAYERS,
  NEAR_SURFACE_FOG_DEPTH_M,
  T_INFINITE_M,
  DEG,
} from './math/constants.ts';
import { coverAt, type CoverKnot } from './math/cover.ts';
import { fogExcessBeta } from './math/extinction.ts';
import { rayPointGroundDistance } from './math/geodesy.ts';
import { layerNoiseTransforms, seedToUint32, type OctaveTransform } from './math/noise.ts';
import { intervalLength, slabIntervals } from './math/shell.ts';
import type { Vec3 } from './math/vec3.ts';
import { MOON_RADIUS_RAD, SOLAR_E0_LUX, SUN_RADIUS_RAD, diskSolidAngle, moonTopOfAtmosphereLux } from './light.ts';
import { ATMOSPHERE_MIN_HEIGHT_M, ATMOSPHERE_THICKNESS_M, atmosphereKey, atmosphereParams, clearAirExtinction, type AtmosphereParams } from './atmosphere/model.ts';
import type { LightInputs } from './cpu/atmosphere.ts';
import { LAYER_SLOT, SLAB_TYPES, cloudDirectTransmission, cloudTotalTransmission, type SlabKind } from './clouds.ts';
import { leadSeconds, sceneAdvection, type SceneSpec } from './scene.ts';

export type { LightInputs } from './cpu/atmosphere.ts';

export interface FrameLayer {
  kind: SlabKind;
  /** Stable noise slot (low 0, mid 1, high 2, ceiling 3). */
  slot: number;
  baseM: number;
  topM: number;
  betaPerM: number;
  /** Solid slabs (the ceiling slab) have coverage 1 everywhere and no noise. */
  solid: boolean;
  /** Cloud-mask sample step along view rays (m): CLOUD_STEP_CELL_FRACTION of the octave-0 noise cell. */
  stepM: number;
  profile: CoverKnot[];
  transforms: OctaveTransform[];
}

/** The frame's atmosphere (PRD §9.2 pass 1): the medium, and where the camera and the cloud shade sit in it. */
export interface FrameAtmosphere {
  params: AtmosphereParams;
  /** atmosphereKey(params): the transmittance and multiple-scattering tables are cached on it. */
  key: string;
  /** Camera height used for the sky-view table and the aerial perspective (the camera clamped into the atmosphere). */
  viewHeightM: number;
  /**
   * Top of the highest slab that can shade the camera's ground (a slab whose top is above it); light
   * scattered by the air below this height is dimmed by the expected cloud dimming. -Infinity: no such slab.
   */
  shadeTopM: number;
}

export interface FrameParams {
  width: number;
  height: number;
  basis: CameraBasis;
  /** Camera height above MSL. */
  hCam: number;
  /** v0 flat-ground height (camera height minus its height above ground). */
  hGround: number;
  seedU32: number;
  /** Seconds from the forecast run's initialisation to the valid time. */
  leadS: number;
  /** Drift of the cloud field since the run's initialisation (east, north; metres). */
  advectionM: [number, number];
  sunDir: Vec3;
  moonDir: Vec3;
  layers: FrameLayer[];
  /** The scene's clear-air haze: an aerosol layer of the atmosphere, uniform below topM (PRD §9.3). */
  haze: { betaPerM: number; topM: number };
  fog: { betaPerM: number; topM: number; decayScaleM: number };
  nominalDepthM: number;
  atmosphere: FrameAtmosphere;
  /** Inputs of the light model; the light itself comes from the atmosphere passes (cpu/atmosphere.ts lightFromAtmosphere). */
  light: LightInputs;
  /** Approximate angular size of one pixel at the image centre (rad). */
  pixelAngle: number;
}

/** Fraction of the camera's Sun ray that is blocked by a layer's forecast cover (0 if the ray misses it). */
function coverOnSunRay(hCam: number, sunDir: Vec3, layer: { baseM: number; topM: number; solid: boolean; profile: CoverKnot[] }): number {
  const [near, far] = slabIntervals(hCam, sunDir[2], layer.baseM, layer.topM, 0, T_INFINITE_M);
  const iv = intervalLength(near) > 0 ? near : far;
  const len = intervalLength(iv);
  if (len <= 0) return 0;
  if (layer.solid) return 1;
  const t = iv.lo + 0.5 * len;
  return coverAt(layer.profile, rayPointGroundDistance(hCam, sunDir, t) / 1000);
}

/**
 * Builds the frame parameters for a validated scene. Pure: the same scene always gives the same object.
 */
export function buildFrame(scene: SceneSpec): FrameParams {
  const cam = scene.camera;
  const basis = cameraBasis(cam);
  const hCam = cam.elev_m_msl;
  const hGround = hCam - (cam.agl_m ?? DEFAULT_CAMERA_AGL_M);
  const seedU32 = seedToUint32(scene.seed);
  const leadS = leadSeconds(scene);
  const advectionM = sceneAdvection(scene);
  const f = scene.forecast;

  // --- Slabs (pass 3). Declared layers in slot order, then the ceiling slab if the ceiling is not inside one.
  // v0 rule: the ceiling becomes a solid 300 m slab (consistent with the S_rule baseline).
  // TODO(M1.5): replace with the overlap rule checked against HRRR total cloud (TCDC), PRD §9.2 pass 3.
  const declared = [...f.layers].sort((a, b) => LAYER_SLOT[a.name] - LAYER_SLOT[b.name]);
  const raw: { kind: SlabKind; baseM: number; topM: number; solid: boolean; profile: CoverKnot[] }[] = declared.map((l) => ({
    kind: l.name,
    baseM: l.base_m_msl,
    topM: l.base_m_msl + l.thickness_m,
    solid: false,
    profile: l.cover_profile.map((k) => ({ distance_km: k.distance_km, cover_0_1: k.cover_0_1 })),
  }));
  let ceilingDeckTop: number | null = null;
  if (f.ceiling_m_msl !== null) {
    const ceil = f.ceiling_m_msl;
    const host = raw.find((l) => l.baseM <= ceil && ceil <= l.topM);
    if (host) {
      ceilingDeckTop = host.topM;
    } else {
      raw.push({ kind: 'ceiling', baseM: ceil, topM: ceil + CEILING_SLAB_THICKNESS_M, solid: true, profile: [{ distance_km: 0, cover_0_1: 1 }] });
      ceilingDeckTop = ceil + CEILING_SLAB_THICKNESS_M;
    }
  }
  if (raw.length > MAX_LAYERS) throw new Error(`buildFrame: ${raw.length} slabs exceed MAX_LAYERS`);

  // --- Atmosphere (pass 1): the reference Earth atmosphere plus the scene's haze as an aerosol layer,
  // uniform below 2 km above the v0 flat ground (TODO(M2): the per-viewpoint fitted profile, PRD §9.3).
  const hazeBeta = scene.haze.beta_per_km / 1000;
  const hazeTop = hGround + HAZE_LAYER_DEPTH_M;
  const params = atmosphereParams(hazeBeta, hazeTop);

  // --- Light inputs (passes 2, 4, 8): v0 cloud factors from the forecast cover, the Moon's phase, the slabs.
  const sunAlt = scene.sun.alt_deg;
  const sunDir = altAzToDirection(sunAlt, scene.sun.az_deg);
  const moonDir = altAzToDirection(scene.moon.alt_deg, scene.moon.az_deg);
  const muS = Math.sin(sunAlt * DEG);
  let tAmb = 1;
  let tDirExp = 1;
  let tBeamTot = 1;
  let shadeTopM = -Infinity;
  for (const l of raw) {
    if (l.topM <= hGround) continue; // a deck below the camera's ground does not shade the camera
    const tau = SLAB_TYPES[l.kind].betaPerM * (l.topM - l.baseM);
    const tTot = cloudTotalTransmission(tau);
    const tDir = cloudDirectTransmission(tau, muS);
    const cZen = l.solid ? 1 : coverAt(l.profile, 0);
    const cSun = coverOnSunRay(hCam, sunDir, l);
    tAmb *= 1 - cZen * (1 - tTot);
    tDirExp *= 1 - cSun * (1 - tDir);
    tBeamTot *= 1 - cSun * (1 - tTot);
    shadeTopM = Math.max(shadeTopM, l.topM);
  }
  const moonLux = moonTopOfAtmosphereLux(scene.moon.illuminated_fraction);

  const layers: FrameLayer[] = raw.map((l) => {
    const type = SLAB_TYPES[l.kind];
    const slot = LAYER_SLOT[l.kind];
    return {
      kind: l.kind,
      slot,
      baseM: l.baseM,
      topM: l.topM,
      betaPerM: type.betaPerM,
      solid: l.solid,
      stepM: type.cellSizeM * CLOUD_STEP_CELL_FRACTION,
      profile: l.profile,
      transforms: layerNoiseTransforms(seedU32, slot, type.cellSizeM, advectionM[0], advectionM[1]),
    };
  });
  // Cloud faces (v0): the camera sees a slab's base from below, its top from above, and a mix from inside.
  const lightLayers = raw.map((l) => {
    const tTot = cloudTotalTransmission(SLAB_TYPES[l.kind].betaPerM * (l.topM - l.baseM));
    let wAbove: number;
    if (hCam <= l.baseM) wAbove = 0;
    else if (hCam >= l.topM) wAbove = 1;
    else wAbove = (hCam - l.baseM) / (l.topM - l.baseM);
    return { topM: l.topM, tTot, wBelow: 1 - wAbove, wAbove };
  });

  // --- Fog (pass 6, PRD §9.3). Visibility includes the clear air, so only the excess over the atmosphere's
  // extinction at the ground (green channel, 550 nm) is fog. v0 flat ground (no DEM): the ground under
  // every ray is the shell at hGround, so near-surface fog fills every height below hGround + 100 m.
  // TODO(M2): DEM ground height along the ray.
  const eps = f.visibility_epsilon ?? DEFAULT_VISIBILITY_EPSILON;
  const clearAirGreen = clearAirExtinction(params, Math.max(hGround, 0))[1];
  const fogBeta = fogExcessBeta(f.visibility_m, clearAirGreen, eps);
  let fogTop = hGround + NEAR_SURFACE_FOG_DEPTH_M;
  // A ceiling inside the near-surface layer is a surface-based deck: the fog fills it up to its top.
  if (f.ceiling_m_msl !== null && f.ceiling_m_msl <= fogTop && ceilingDeckTop !== null) fogTop = Math.max(fogTop, ceilingDeckTop);

  const light: LightInputs = {
    solarLux: SOLAR_E0_LUX,
    tAmb,
    tDirExp,
    tBeamTot,
    moonLux,
    layers: lightLayers,
    // Blend weight from the haze-like airlight (Koschmieder's horizon sky) to the dense-fog airlight:
    // 1 - exp(-beta_fog * 1 km). ~0.035 at 50 km visibility (haze 0.03 per km, ground at 1,640 m), ~0.98 at 1 km.
    fogAirWeight: 1 - Math.exp(-fogBeta * 1000),
    sunDiskL: [SOLAR_E0_LUX / diskSolidAngle(SUN_RADIUS_RAD), SOLAR_E0_LUX / diskSolidAngle(SUN_RADIUS_RAD), SOLAR_E0_LUX / diskSolidAngle(SUN_RADIUS_RAD)],
    moonDiskL: [moonLux / diskSolidAngle(MOON_RADIUS_RAD), moonLux / diskSolidAngle(MOON_RADIUS_RAD), moonLux / diskSolidAngle(MOON_RADIUS_RAD)],
  };

  return {
    width: cam.width,
    height: cam.height,
    basis,
    hCam,
    hGround,
    seedU32,
    leadS,
    advectionM,
    sunDir,
    moonDir,
    layers,
    haze: { betaPerM: hazeBeta, topM: hazeTop },
    fog: { betaPerM: fogBeta, topM: fogTop, decayScaleM: FOG_DECAY_SCALE_M },
    nominalDepthM: scene.plate.nominal_depth_m ?? DEFAULT_NOMINAL_DEPTH_M,
    atmosphere: {
      params,
      key: atmosphereKey(params),
      viewHeightM: Math.min(Math.max(hCam, ATMOSPHERE_MIN_HEIGHT_M), ATMOSPHERE_THICKNESS_M - ATMOSPHERE_MIN_HEIGHT_M),
      shadeTopM,
    },
    light,
    pixelAngle: (2 * basis.tanHalfH) / cam.width,
  };
}
