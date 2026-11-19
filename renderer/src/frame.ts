/**
 * Derived per-frame parameters: everything the shader receives as uniforms, computed once on the CPU in
 * float64 from a validated SceneSpec. The CPU reference (cpu/reference.ts) reads the same object, so the
 * GPU and the reference share one definition of layers, noise transforms, haze, fog and light.
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
import { horizonDip, rayPointGroundDistance } from './math/geodesy.ts';
import { layerNoiseTransforms, seedToUint32, type OctaveTransform } from './math/noise.ts';
import { intervalLength, slabIntervals } from './math/shell.ts';
import type { Vec3 } from './math/vec3.ts';
import {
  CAMERA_MIN_ADAPT_LUX,
  FOG_ALBEDO,
  MOON_RADIUS_RAD,
  MOON_SKY_FACTOR,
  SKY_COLORS,
  SKY_IRRADIANCE_FACTOR,
  SUN_RADIUS_RAD,
  addRGB,
  atmosphereTransmittance,
  diskSolidAngle,
  luminanceY,
  moonNormalIlluminance,
  normalizeY,
  scaleRGB,
  skyDiffuseIlluminance,
  skyWeights,
  sunNormalIlluminance,
  sunVisibility,
  smoothstep,
  zenithLuminance,
  type RGB,
} from './light.ts';
import { LAYER_SLOT, SLAB_TYPES, cloudDirectTransmission, cloudTotalTransmission, type SlabKind } from './clouds.ts';
import { leadSeconds, sceneAdvection, type SceneSpec } from './scene.ts';

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
  /** Radiance (rgb cd/m^2) of the face of this slab that the camera sees: base, top, or in-cloud mix. */
  radiance: RGB;
}

export interface FrameLight {
  /** Direct solar illuminance on a horizontal surface under a clear sky (rgb lux). */
  eDir: RGB;
  /** Diffuse illuminance, expected over the forecast cover (rgb lux), including moonlight. */
  eDif: RGB;
  /** Expected illuminance at the camera (lux, luminance), used for exposure. */
  eCamY: number;
  /** Multiplier from radiance to exposure-normalised linear values (1 = a white diffuser under eCam). */
  exposure: number;
  sunDiskL: RGB;
  moonDiskL: RGB;
  sky: { zenithL: number; day: number; twilight: number; night: number };
  skyMoon: RGB;
  /** Circumsolar glow radiance scale (rgb). */
  glow: RGB;
  /** Scalar dimming of the clear-sky airlight by cloud (expected illuminance / clear illuminance). */
  cloudDim: number;
  /** Multiple-scattering airlight radiance of dense near-surface fog (rgb). */
  fogAir: RGB;
  /**
   * Blend weight from the haze airlight (single scattering, Koschmieder's horizon-sky assumption) to
   * fogAir for the near-surface layer: 1 - exp(-beta_fog * 1 km). ~0.05 at 50 km visibility, ~0.98 at 1 km.
   */
  fogAirWeight: number;
  sunUp: boolean;
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
  haze: { betaPerM: number; topM: number };
  fog: { betaPerM: number; topM: number; decayScaleM: number };
  nominalDepthM: number;
  light: FrameLight;
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

  // --- Light (passes 1, 2, 4, 8).
  const sunAlt = scene.sun.alt_deg;
  const sunDir = altAzToDirection(sunAlt, scene.sun.az_deg);
  const moonDir = altAzToDirection(scene.moon.alt_deg, scene.moon.az_deg);
  const eSunN = sunNormalIlluminance(sunAlt);
  const muS = Math.sin(sunAlt * DEG);
  const eDir = scaleRGB(eSunN, Math.max(muS, 0));
  const eMoonN = moonNormalIlluminance(scene.moon.alt_deg, scene.moon.illuminated_fraction);
  const eMoonH = scaleRGB(eMoonN, Math.max(Math.sin(scene.moon.alt_deg * DEG), 0));
  const skyMoon = scaleRGB(SKY_COLORS.night, MOON_SKY_FACTOR * luminanceY(eMoonH));
  const eSkyClear = addRGB(addRGB(skyDiffuseIlluminance(sunAlt), scaleRGB(skyMoon, Math.PI * SKY_IRRADIANCE_FACTOR)), eMoonH);

  let tAmb = 1;
  let tDirExp = 1;
  let tBeamTot = 1;
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
  }
  const eDif = addRGB(scaleRGB(eSkyClear, tAmb), scaleRGB(eDir, Math.max(tBeamTot - tDirExp, 0)));
  const eCam = addRGB(scaleRGB(eDir, tDirExp), eDif);
  const eCamY = luminanceY(eCam);
  const eClearY = luminanceY(addRGB(eDir, eSkyClear));
  const cloudDim = eClearY > 0 ? Math.min(1, eCamY / eClearY) : 1;

  const layers: FrameLayer[] = raw.map((l) => {
    const type = SLAB_TYPES[l.kind];
    const tau = type.betaPerM * (l.topM - l.baseM);
    const tTot = cloudTotalTransmission(tau);
    // v0 cloud lighting: the slab sees the Sun above its own horizon (altitude + dip), with a 30 %
    // isotropic "volume" term so low Sun still lights cloud sides; sky light from above. Heuristic.
    const altL = sunAlt + horizonDip(Math.max(l.topM - hGround, 0)) / DEG;
    const eSunL = scaleRGB(sunNormalIlluminance(altL), 0.7 * Math.max(Math.sin(altL * DEG), 0) + 0.3 * sunVisibility(altL));
    const eAbove = addRGB(eSunL, eSkyClear);
    const below = scaleRGB(eAbove, tTot / Math.PI);
    const above = scaleRGB(eAbove, (1 - tTot) / Math.PI);
    let radiance: RGB;
    if (hCam <= l.baseM) radiance = below;
    else if (hCam >= l.topM) radiance = above;
    else {
      const a = (hCam - l.baseM) / (l.topM - l.baseM);
      radiance = addRGB(scaleRGB(below, 1 - a), scaleRGB(above, a));
    }
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
      radiance,
    };
  });

  // --- Haze and fog (pass 6, PRD §9.3).
  const hazeBeta = scene.haze.beta_per_km / 1000;
  const eps = f.visibility_epsilon ?? DEFAULT_VISIBILITY_EPSILON;
  // v0 flat ground (no DEM): the ground under every ray is the shell at hGround, so near-surface fog fills
  // every height below hGround + 100 m. TODO(M2): DEM ground height along the ray.
  let fogTop = hGround + NEAR_SURFACE_FOG_DEPTH_M;
  // A ceiling inside the near-surface layer is a surface-based deck: the fog fills it up to its top.
  if (f.ceiling_m_msl !== null && f.ceiling_m_msl <= fogTop && ceilingDeckTop !== null) fogTop = Math.max(fogTop, ceilingDeckTop);

  const sunColorNorm = normalizeY(atmosphereTransmittance(sunAlt));
  const zenithL = zenithLuminance(sunAlt);
  const w = skyWeights(sunAlt);
  const light: FrameLight = {
    eDir,
    eDif,
    eCamY,
    exposure: Math.PI / Math.max(eCamY, CAMERA_MIN_ADAPT_LUX),
    sunDiskL: scaleRGB(eSunN, 1 / diskSolidAngle(SUN_RADIUS_RAD)),
    moonDiskL: scaleRGB(eMoonN, 1 / diskSolidAngle(MOON_RADIUS_RAD)),
    sky: { zenithL, day: w.day, twilight: w.twilight, night: w.night },
    skyMoon,
    glow: scaleRGB(sunColorNorm, 2.0 * zenithL * smoothstep(-6, 2, sunAlt)),
    cloudDim,
    fogAir: scaleRGB(eCam, FOG_ALBEDO / Math.PI),
    fogAirWeight: 1 - Math.exp(-fogExcessBeta(f.visibility_m, hazeBeta, eps) * 1000),
    sunUp: luminanceY(eDir) > 0,
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
    haze: { betaPerM: hazeBeta, topM: hGround + HAZE_LAYER_DEPTH_M },
    fog: { betaPerM: fogExcessBeta(f.visibility_m, hazeBeta, eps), topM: fogTop, decayScaleM: FOG_DECAY_SCALE_M },
    nominalDepthM: scene.plate.nominal_depth_m ?? DEFAULT_NOMINAL_DEPTH_M,
    light,
    pixelAngle: (2 * basis.tanHalfH) / cam.width,
  };
}
