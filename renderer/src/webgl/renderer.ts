/**
 * WebGL2 renderer v0 (PRD §9.2): render(scene, plate, sky mask?, depth?, target canvas).
 *
 * The same module runs in the overlay (WKWebView), the scrub view and headless Chromium/SwiftShader
 * (PRD §6.1). It is pure in (scene, inputs, renderer version): uniforms are derived by buildFrame() in
 * float64 on the CPU, the cloud-noise lattice is generated from the seed, and nothing reads the clock.
 *
 * Each render first runs the Hillaire 2020 atmosphere passes (webgl/atmosphere-glsl.ts, D-013): the
 * transmittance and multiple-scattering tables when the atmosphere changed since the last render (the
 * haze), then every frame the sky-view tables, the light pass and the aerial-perspective volume, all into
 * RGBA32F textures (EXT_color_buffer_float is required). The main pass then reads them.
 *
 * For M2 (TODO(M2)): oculus-core WASM computes the Sun and Moon inside the module (until then
 * validateScene checks the caller's Sun); shader pre-warm and lost-context restore; dehazed plate,
 * normals and DEM horizon maps feed groundRadiance().
 */
import { buildFrame, type FrameParams } from '../frame.ts';
import { MAX_LAYERS, MAX_PROFILE_KNOTS } from '../math/constants.ts';
import { LATTICE_SIZE, NOISE_AMPLITUDES, NOISE_OCTAVES, makeLattice } from '../math/noise.ts';
import { MOON_RADIUS_RAD, SUN_RADIUS_RAD } from '../light.ts';
import { validateScene, type SceneSpec } from '../scene.ts';
import type { RawImage } from '../cpu/reference.ts';
import { LIGHT_TEXELS } from '../cpu/atmosphere.ts';
import {
  AP_GRID,
  AP_SLICES,
  IRRADIANCE_AZIMUTH_CELLS,
  MULTI_SCATTERING_LUT_SIZE,
  SKY_VIEW_LUT_HEIGHT,
  SKY_VIEW_LUT_WIDTH,
  TRANSMITTANCE_LUT_HEIGHT,
  TRANSMITTANCE_LUT_WIDTH,
  atmosphereParams,
  type AtmosphereParams,
} from '../atmosphere/model.ts';
import { AP_INCREMENT_FS, AP_PREFIX_FS, IRRADIANCE_FS, LIGHT_FS, MULTI_SCATTERING_FS, PASS_VERTEX_SHADER, SHADE_TOP_NONE, SKY_VIEW_FS, TRANSMITTANCE_FS } from './atmosphere-glsl.ts';
import { DEBUG_MODES, FRAGMENT_SHADER, VERTEX_SHADER, type DebugMode } from './shader.ts';

export type { DebugMode } from './shader.ts';
export type ImageInput = TexImageSource | RawImage;

export interface RenderInputs {
  plate: ImageInput;
  skyMask?: ImageInput | null;
  depth?: ImageInput | null;
  /**
   * SHA-256 of the files the images were decoded from, when the caller knows them (the Node runner
   * does). Each one given must equal its hash in scene.plate, so a render can never pair a scene with
   * another plate. Decoded pixels carry no file hash, so a caller that omits these is not checked here.
   */
  plateSha256?: string;
  skyMaskSha256?: string;
  depthSha256?: string;
}

export interface RenderOptions {
  /** 'none' draws the frame to the canvas; the debug modes draw float values to an offscreen target. */
  debug?: DebugMode;
  /** Accept a Sun that does not match the valid time and place (synthetic geometry tests only). */
  allowSyntheticSun?: boolean;
  /**
   * Time each atmosphere pass (benchmarks only): a 1-pixel readPixels barrier after each pass, so the
   * GPU work of each is measured. The barriers serialise the GPU, so the frame takes longer than without.
   */
  profile?: boolean;
}

/** Throws unless every input hash the caller attested equals the scene's (PRD §9.2: render is pure in the scene). */
export function checkInputHashes(scene: SceneSpec, inputs: Pick<RenderInputs, 'plateSha256' | 'skyMaskSha256' | 'depthSha256'>): void {
  const pairs: [string, string | undefined, string | undefined][] = [
    ['plate', inputs.plateSha256, scene.plate.sha256],
    ['sky mask', inputs.skyMaskSha256, scene.plate.sky_mask_sha256],
    ['depth', inputs.depthSha256, scene.plate.depth_sha256],
  ];
  for (const [what, given, expected] of pairs) {
    if (given !== undefined && given !== expected) {
      throw new Error(`the ${what} input (sha256 ${given.slice(0, 12)}...) is not the one scene.plate names (${expected === undefined ? 'none' : `${expected.slice(0, 12)}...`})`);
    }
  }
}

export interface RenderInfo {
  /** CPU time to validate the scene and build uniforms (ms). */
  setupMs: number;
  /** Atmosphere passes issued for this frame, in order (the transmittance and multiple-scattering tables only when the atmosphere changed). */
  atmospherePasses: string[];
  /** With options.profile: wall time (ms) of each atmosphere pass, measured to a 1-pixel readPixels barrier. */
  passMs?: Record<string, number>;
  /** CPU time spent issuing texture uploads (ms); 0 when every input was already resident. */
  uploadMs: number;
  /** CPU time to issue the draw (ms). GPU completion is measured by the caller with finish(). */
  submitMs: number;
  uploaded: string[];
  frame: FrameParams;
}

export interface GlInfo {
  vendor: string;
  renderer: string;
  unmaskedVendor: string | null;
  unmaskedRenderer: string | null;
  version: string;
  shadingLanguageVersion: string;
  maxFragmentUniformVectors: number;
  maxDrawBuffers: number;
  colorBufferFloat: boolean;
}

/** A float texture read back from the GPU: RGBA, row-major, row 0 = the bottom texel row (GL order). */
export interface FloatImage {
  width: number;
  height: number;
  data: Float32Array;
}

/** The atmosphere tables of the last render, read back for tests (renderer.readAtmosphere()). */
export interface AtmosphereReadback {
  transmittance: FloatImage;
  multiScattering: FloatImage;
  skyView: FloatImage;
  skyViewBelow: FloatImage;
  light: FloatImage;
  apIncScatter: FloatImage;
  apIncTransmittance: FloatImage;
  apScatter: FloatImage;
  apTransmittance: FloatImage;
}

/** A compiled program and its uniform locations. */
class Program {
  readonly program: WebGLProgram;
  private readonly gl: WebGL2RenderingContext;
  private readonly locs = new Map<string, WebGLUniformLocation | null>();
  constructor(gl: WebGL2RenderingContext, program: WebGLProgram) {
    this.gl = gl;
    this.program = program;
  }
  loc(name: string): WebGLUniformLocation | null {
    if (!this.locs.has(name)) this.locs.set(name, this.gl.getUniformLocation(this.program, name));
    return this.locs.get(name)!;
  }
}

type LutName = 'trans' | 'ms' | 'sky0' | 'sky1' | 'irr' | 'light' | 'apInc0' | 'apInc1' | 'apS' | 'apT';
type PassName = 'trans' | 'ms' | 'sky' | 'irr' | 'light' | 'apInc' | 'apPrefix';

const LUT_SIZES: Record<LutName, [number, number]> = {
  trans: [TRANSMITTANCE_LUT_WIDTH, TRANSMITTANCE_LUT_HEIGHT],
  ms: [MULTI_SCATTERING_LUT_SIZE, MULTI_SCATTERING_LUT_SIZE],
  sky0: [SKY_VIEW_LUT_WIDTH, SKY_VIEW_LUT_HEIGHT],
  sky1: [SKY_VIEW_LUT_WIDTH, SKY_VIEW_LUT_HEIGHT],
  irr: [IRRADIANCE_AZIMUTH_CELLS, 1],
  light: [LIGHT_TEXELS.count, 1],
  apInc0: [AP_GRID * AP_SLICES, AP_GRID],
  apInc1: [AP_GRID * AP_SLICES, AP_GRID],
  apS: [AP_GRID * AP_SLICES, AP_GRID],
  apT: [AP_GRID * AP_SLICES, AP_GRID],
};

/** Which tables each pass writes (MRT attachment order). */
const PASS_TARGETS: Record<PassName, LutName[]> = {
  trans: ['trans'],
  ms: ['ms'],
  sky: ['sky0', 'sky1'],
  irr: ['irr'],
  light: ['light'],
  apInc: ['apInc0', 'apInc1'],
  apPrefix: ['apS', 'apT'],
};

const PASS_SOURCES: Record<PassName, string> = {
  trans: TRANSMITTANCE_FS,
  ms: MULTI_SCATTERING_FS,
  sky: SKY_VIEW_FS,
  irr: IRRADIANCE_FS,
  light: LIGHT_FS,
  apInc: AP_INCREMENT_FS,
  apPrefix: AP_PREFIX_FS,
};

/** Texture units: 0-3 the main pass's inputs, 4-9 the atmosphere tables. */
const UNIT = { trans: 4, ms: 5, sky0: 5, sky1: 6, apS: 7, apT: 8, light: 9, apInc0: 4, apInc1: 5, irr: 6 } as const;

/**
 * The GLSL bakes in the reference Earth atmosphere (atmosphere/model.ts); only the haze is a uniform.
 * Throws if a frame asks for anything else, so the GPU never silently renders another atmosphere.
 */
function assertBakedAtmosphere(p: AtmosphereParams): void {
  const ref = atmosphereParams(0, 0);
  const skip = new Set(['hazeScattering', 'hazeExtinction', 'hazeTopM']);
  for (const [k, v] of Object.entries(ref)) {
    if (skip.has(k)) continue;
    if (JSON.stringify((p as unknown as Record<string, unknown>)[k]) !== JSON.stringify(v)) throw new Error(`the GPU atmosphere bakes in ${k}; this frame asks for another value`);
  }
}

function isRaw(x: ImageInput): x is RawImage {
  const d = (x as Partial<RawImage>).data;
  return typeof (x as Partial<RawImage>).width === 'number' && (d instanceof Uint8Array || d instanceof Uint8ClampedArray);
}

function imageSize(x: ImageInput): { width: number; height: number } {
  if (isRaw(x)) return { width: x.width, height: x.height };
  const s = x as { width?: number; height?: number; videoWidth?: number; videoHeight?: number; displayWidth?: number; displayHeight?: number };
  const width = s.videoWidth ?? s.displayWidth ?? s.width ?? 0;
  const height = s.videoHeight ?? s.displayHeight ?? s.height ?? 0;
  return { width, height };
}

const now = (): number => (typeof performance !== 'undefined' ? performance.now() : Date.now());

type TexSlot = 'plate' | 'skyMask' | 'depth' | 'lattice';

export class SnowGlobeRenderer {
  readonly gl: WebGL2RenderingContext;
  readonly canvas: HTMLCanvasElement | OffscreenCanvas;
  private program: WebGLProgram | null = null;
  private main: Program | null = null;
  private passes: Record<PassName, Program> | null = null;
  private luts: Record<LutName, WebGLTexture> | null = null;
  private lutFbos: Record<PassName, WebGLFramebuffer> | null = null;
  private baseKey: string | null = null;
  private vao: WebGLVertexArrayObject | null = null;
  private tex = new Map<TexSlot, WebGLTexture>();
  private texSource = new Map<TexSlot, unknown>();
  private latticeSeed: number | null = null;
  private dummy: WebGLTexture | null = null;
  private floatTarget: { fbo: WebGLFramebuffer; tex: WebGLTexture; width: number; height: number } | null = null;
  private lastTarget: 'canvas' | 'float' = 'canvas';
  private contextLost = false;

  constructor(canvas: HTMLCanvasElement | OffscreenCanvas) {
    this.canvas = canvas;
    const gl = canvas.getContext('webgl2', {
      antialias: false,
      alpha: false,
      depth: false,
      stencil: false,
      premultipliedAlpha: false,
      preserveDrawingBuffer: false,
      powerPreference: 'high-performance',
    }) as WebGL2RenderingContext | null;
    if (!gl) throw new Error('SnowGlobeRenderer: WebGL2 is not available');
    this.gl = gl;
    // TODO(M2): restore and pre-warm shader variants after a lost context (PRD §9.2 performance).
    canvas.addEventListener('webglcontextlost', () => {
      this.contextLost = true;
    });
  }

  /** Driver and capability strings, including the unmasked renderer (used to confirm SwiftShader). */
  glInfo(): GlInfo {
    const gl = this.gl;
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    return {
      vendor: String(gl.getParameter(gl.VENDOR)),
      renderer: String(gl.getParameter(gl.RENDERER)),
      unmaskedVendor: dbg ? String(gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL)) : null,
      unmaskedRenderer: dbg ? String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)) : null,
      version: String(gl.getParameter(gl.VERSION)),
      shadingLanguageVersion: String(gl.getParameter(gl.SHADING_LANGUAGE_VERSION)),
      maxFragmentUniformVectors: Number(gl.getParameter(gl.MAX_FRAGMENT_UNIFORM_VECTORS)),
      maxDrawBuffers: Number(gl.getParameter(gl.MAX_DRAW_BUFFERS)),
      colorBufferFloat: gl.getExtension('EXT_color_buffer_float') !== null,
    };
  }

  /**
   * Compiles and links the main program and the atmosphere passes, and allocates the atmosphere's RGBA32F
   * tables (called lazily by render; call early to pre-warm).
   */
  compile(): void {
    if (this.program) return;
    const gl = this.gl;
    if (!gl.getExtension('EXT_color_buffer_float')) throw new Error('SnowGlobeRenderer: the atmosphere needs EXT_color_buffer_float (RGBA32F render targets)');
    const sh = (type: number, src: string, what: string): WebGLShader => {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
        const log = gl.getShaderInfoLog(s);
        gl.deleteShader(s);
        throw new Error(`${what} shader compile failed: ${log}`);
      }
      return s;
    };
    const link = (fsSrc: string, vsSrc: string, what: string): Program => {
      const vs = sh(gl.VERTEX_SHADER, vsSrc, what);
      const fs = sh(gl.FRAGMENT_SHADER, fsSrc, what);
      const p = gl.createProgram()!;
      gl.attachShader(p, vs);
      gl.attachShader(p, fs);
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(`${what} program link failed: ${gl.getProgramInfoLog(p)}`);
      gl.deleteShader(vs);
      gl.deleteShader(fs);
      return new Program(gl, p);
    };
    this.main = link(FRAGMENT_SHADER, VERTEX_SHADER, 'main');
    const passes = {} as Record<PassName, Program>;
    for (const name of Object.keys(PASS_SOURCES) as PassName[]) passes[name] = link(PASS_SOURCES[name], PASS_VERTEX_SHADER, `atmosphere ${name}`);
    this.passes = passes;
    const luts = {} as Record<LutName, WebGLTexture>;
    for (const name of Object.keys(LUT_SIZES) as LutName[]) {
      const t = gl.createTexture()!;
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texStorage2D(gl.TEXTURE_2D, 1, gl.RGBA32F, LUT_SIZES[name][0], LUT_SIZES[name][1]);
      this.setSampling(gl.NEAREST, gl.NEAREST);
      luts[name] = t;
    }
    this.luts = luts;
    const fbos = {} as Record<PassName, WebGLFramebuffer>;
    for (const name of Object.keys(PASS_TARGETS) as PassName[]) {
      const fbo = gl.createFramebuffer()!;
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      const targets = PASS_TARGETS[name];
      targets.forEach((lut, i) => gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0 + i, gl.TEXTURE_2D, luts[lut], 0));
      gl.drawBuffers(targets.map((_, i) => gl.COLOR_ATTACHMENT0 + i));
      if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error(`atmosphere ${name} framebuffer incomplete`);
      fbos[name] = fbo;
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    this.lutFbos = fbos;
    this.program = this.main.program;
    this.vao = gl.createVertexArray();
    this.dummy = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.dummy);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([0, 0, 0, 255]));
    this.setSampling(gl.NEAREST, gl.NEAREST);
  }

  private u(name: string): WebGLUniformLocation | null {
    return this.main!.loc(name);
  }

  private setSampling(min: number, mag: number): void {
    const gl = this.gl;
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, min);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, mag);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  }

  /** Uploads an image to a slot unless the same source object is already resident. */
  private upload(slot: Exclude<TexSlot, 'lattice'>, img: ImageInput, uploaded: string[]): void {
    if (this.texSource.get(slot) === img && this.tex.has(slot)) return;
    const gl = this.gl;
    let t = this.tex.get(slot);
    if (!t) {
      t = gl.createTexture()!;
      this.tex.set(slot, t);
    }
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
    // The plate is sRGB-encoded colour: an sRGB texture makes filtering happen in linear light.
    const internal = slot === 'plate' ? gl.SRGB8_ALPHA8 : gl.RGBA8;
    if (isRaw(img)) {
      const data = img.data instanceof Uint8Array ? img.data : new Uint8Array(img.data.buffer, img.data.byteOffset, img.data.byteLength);
      gl.texImage2D(gl.TEXTURE_2D, 0, internal, img.width, img.height, 0, gl.RGBA, gl.UNSIGNED_BYTE, data);
    } else {
      gl.texImage2D(gl.TEXTURE_2D, 0, internal, gl.RGBA, gl.UNSIGNED_BYTE, img);
    }
    if (slot === 'plate') {
      gl.generateMipmap(gl.TEXTURE_2D);
      this.setSampling(gl.LINEAR_MIPMAP_LINEAR, gl.LINEAR);
    } else if (slot === 'skyMask') {
      this.setSampling(gl.LINEAR, gl.LINEAR);
    } else {
      this.setSampling(gl.NEAREST, gl.NEAREST);
    }
    this.texSource.set(slot, img);
    uploaded.push(slot);
  }

  private uploadLattice(seedU32: number, uploaded: string[]): void {
    if (this.latticeSeed === seedU32 && this.tex.has('lattice')) return;
    const gl = this.gl;
    let t = this.tex.get('lattice');
    if (!t) {
      t = gl.createTexture()!;
      this.tex.set('lattice', t);
    }
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, LATTICE_SIZE, LATTICE_SIZE, 0, gl.RED, gl.FLOAT, makeLattice(seedU32));
    this.setSampling(gl.NEAREST, gl.NEAREST);
    this.latticeSeed = seedU32;
    uploaded.push('lattice');
  }

  private ensureFloatTarget(width: number, height: number): void {
    const gl = this.gl;
    if (this.floatTarget && this.floatTarget.width === width && this.floatTarget.height === height) return;
    if (!gl.getExtension('EXT_color_buffer_float')) throw new Error('debug modes need EXT_color_buffer_float');
    if (this.floatTarget) {
      gl.deleteFramebuffer(this.floatTarget.fbo);
      gl.deleteTexture(this.floatTarget.tex);
    }
    const tex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texStorage2D(gl.TEXTURE_2D, 1, gl.RGBA32F, width, height);
    this.setSampling(gl.NEAREST, gl.NEAREST);
    const fbo = gl.createFramebuffer()!;
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error('float framebuffer incomplete');
    this.floatTarget = { fbo, tex, width, height };
  }

  /**
   * Renders one frame. Throws if the scene is invalid or the inputs disagree with scene.plate flags.
   * The draw is only issued; call finish() (or read pixels) to wait for the GPU.
   */
  render(sceneInput: SceneSpec, inputs: RenderInputs, opts: RenderOptions = {}): RenderInfo {
    if (this.contextLost) throw new Error('SnowGlobeRenderer: WebGL context lost');
    const t0 = now();
    const scene = validateScene(sceneInput, { allowSyntheticSun: opts.allowSyntheticSun === true });
    if (scene.plate.has_sky_mask !== Boolean(inputs.skyMask)) throw new Error('scene.plate.has_sky_mask does not match the inputs');
    if (scene.plate.has_depth !== Boolean(inputs.depth)) throw new Error('scene.plate.has_depth does not match the inputs');
    checkInputHashes(scene, inputs);
    const plateSize = imageSize(inputs.plate);
    if (!(plateSize.width > 0 && plateSize.height > 0)) throw new Error('plate image has no size');
    const frame = buildFrame(scene);
    this.compile();
    const gl = this.gl;
    const { width, height } = frame;
    const t1 = now();

    const uploaded: string[] = [];
    gl.activeTexture(gl.TEXTURE0);
    this.upload('plate', inputs.plate, uploaded);
    gl.activeTexture(gl.TEXTURE1);
    if (inputs.skyMask) this.upload('skyMask', inputs.skyMask, uploaded);
    gl.activeTexture(gl.TEXTURE2);
    if (inputs.depth) this.upload('depth', inputs.depth, uploaded);
    gl.activeTexture(gl.TEXTURE3);
    this.uploadLattice(frame.seedU32, uploaded);
    const t2 = now();

    gl.disable(gl.BLEND);
    gl.disable(gl.DEPTH_TEST);
    gl.bindVertexArray(this.vao);
    const passMs: Record<string, number> | undefined = opts.profile ? {} : undefined;
    const atmospherePasses = this.renderAtmosphere(frame, passMs);

    const debug = opts.debug ?? 'none';
    if (debug === 'none') {
      if (this.canvas.width !== width) this.canvas.width = width;
      if (this.canvas.height !== height) this.canvas.height = height;
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      this.lastTarget = 'canvas';
    } else {
      this.ensureFloatTarget(width, height);
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.floatTarget!.fbo);
      this.lastTarget = 'float';
    }
    gl.viewport(0, 0, width, height);
    gl.useProgram(this.program);

    // Bind textures (dummy for absent inputs; samplers must always be complete).
    const bind = (unit: number, slot: TexSlot, present: boolean, name: string) => {
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, present ? this.tex.get(slot)! : this.dummy);
      gl.uniform1i(this.u(name), unit);
    };
    bind(0, 'plate', true, 'u_plate');
    bind(1, 'skyMask', Boolean(inputs.skyMask), 'u_skyMask');
    bind(2, 'depth', Boolean(inputs.depth), 'u_depth');
    bind(3, 'lattice', true, 'u_lattice');
    this.bindLuts(this.main!, { u_transLut: 'trans', u_skyLut0: 'sky0', u_skyLut1: 'sky1', u_apS: 'apS', u_apT: 'apT', u_lightLut: 'light' });

    this.setUniforms(frame, scene, DEBUG_MODES[debug]);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    const t3 = now();
    return { setupMs: t1 - t0, atmospherePasses, ...(passMs ? { passMs } : {}), uploadMs: t2 - t1, submitMs: t3 - t2, uploaded, frame };
  }

  /** Binds atmosphere tables to their texture units and points the program's samplers at them. */
  private bindLuts(p: Program, samplers: Record<string, LutName>): void {
    const gl = this.gl;
    for (const [uniform, lut] of Object.entries(samplers)) {
      const unit = UNIT[lut];
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, this.luts![lut]);
      gl.uniform1i(p.loc(uniform), unit);
    }
  }

  /** The haze uniforms every atmosphere program shares. */
  private setHaze(p: Program, fr: FrameParams): void {
    const gl = this.gl;
    const a = fr.atmosphere.params;
    gl.uniform3f(p.loc('u_hazeScat'), a.hazeScattering[0], a.hazeScattering[1], a.hazeScattering[2]);
    gl.uniform3f(p.loc('u_hazeExt'), a.hazeExtinction[0], a.hazeExtinction[1], a.hazeExtinction[2]);
    gl.uniform1f(p.loc('u_hazeTop'), a.hazeTopM);
  }

  /**
   * Pass 1 (PRD §9.2, D-013): the Hillaire 2020 tables for this frame. The transmittance and
   * multiple-scattering tables are redrawn only when the atmosphere changed since the last render;
   * the sky-view tables, the light pass and the aerial perspective are redrawn every frame.
   */
  private renderAtmosphere(fr: FrameParams, passMs?: Record<string, number>): string[] {
    const gl = this.gl;
    assertBakedAtmosphere(fr.atmosphere.params);
    const passes: string[] = [];
    const px = new Float32Array(4);
    if (passMs) this.finish();
    const run = (name: PassName, setup: (p: Program) => void): void => {
      const p = this.passes![name];
      const [w, h] = LUT_SIZES[PASS_TARGETS[name][0]!];
      const t = now();
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.lutFbos![name]);
      gl.viewport(0, 0, w, h);
      gl.useProgram(p.program);
      setup(p);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      if (passMs) {
        gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.FLOAT, px);
        passMs[name] = now() - t;
      }
      passes.push(name);
    };
    const shadeTop = Number.isFinite(fr.atmosphere.shadeTopM) ? fr.atmosphere.shadeTopM : SHADE_TOP_NONE;
    const hView = fr.atmosphere.viewHeightM;
    if (this.baseKey !== fr.atmosphere.key) {
      run('trans', (p) => this.setHaze(p, fr));
      run('ms', (p) => {
        this.setHaze(p, fr);
        this.bindLuts(p, { u_transLut: 'trans' });
      });
      this.baseKey = fr.atmosphere.key;
    }
    run('sky', (p) => {
      this.setHaze(p, fr);
      this.bindLuts(p, { u_transLut: 'trans', u_msLut: 'ms' });
      gl.uniform1f(p.loc('u_viewH'), hView);
      gl.uniform1f(p.loc('u_muS'), fr.sunDir[2]);
      gl.uniform1f(p.loc('u_shadeTop'), shadeTop);
    });
    run('irr', (p) => {
      this.setHaze(p, fr);
      this.bindLuts(p, { u_transLut: 'trans', u_skyLut0: 'sky0' });
      gl.uniform1f(p.loc('u_viewH'), hView);
    });
    run('light', (p) => {
      this.setHaze(p, fr);
      this.bindLuts(p, { u_transLut: 'trans', u_irrLut: 'irr' });
      const L = fr.light;
      gl.uniform1f(p.loc('u_viewH'), hView);
      gl.uniform3f(p.loc('u_sunDir'), fr.sunDir[0], fr.sunDir[1], fr.sunDir[2]);
      gl.uniform3f(p.loc('u_moonDir'), fr.moonDir[0], fr.moonDir[1], fr.moonDir[2]);
      gl.uniform1f(p.loc('u_solarLux'), L.solarLux);
      gl.uniform1f(p.loc('u_tAmb'), L.tAmb);
      gl.uniform1f(p.loc('u_tDirExp'), L.tDirExp);
      gl.uniform1f(p.loc('u_tBeamTot'), L.tBeamTot);
      gl.uniform1f(p.loc('u_moonLux'), L.moonLux);
      gl.uniform1i(p.loc('u_layerCount'), L.layers.length);
      const ll = new Float32Array(MAX_LAYERS * 4);
      L.layers.forEach((l, i) => ll.set([l.topM, l.tTot, l.wBelow, l.wAbove], i * 4));
      gl.uniform4fv(p.loc('u_layerLight'), ll);
    });
    run('apInc', (p) => {
      this.setHaze(p, fr);
      this.bindLuts(p, { u_transLut: 'trans', u_msLut: 'ms', u_lightLut: 'light' });
      const b = fr.basis;
      gl.uniform3f(p.loc('u_camF'), b.forward[0], b.forward[1], b.forward[2]);
      gl.uniform3f(p.loc('u_camR'), b.right[0], b.right[1], b.right[2]);
      gl.uniform3f(p.loc('u_camU'), b.up[0], b.up[1], b.up[2]);
      gl.uniform2f(p.loc('u_tanHalf'), b.tanHalfH, b.tanHalfV);
      gl.uniform1f(p.loc('u_viewH'), hView);
      gl.uniform3f(p.loc('u_sunDir'), fr.sunDir[0], fr.sunDir[1], fr.sunDir[2]);
      gl.uniform1f(p.loc('u_shadeTop'), shadeTop);
    });
    run('apPrefix', (p) => this.bindLuts(p, { u_apInc0: 'apInc0', u_apInc1: 'apInc1' }));
    return passes;
  }

  /**
   * Reads back every atmosphere table of the last render as float RGBA (GL row order: row 0 is the bottom
   * texel row). For tests: the GPU-versus-CPU comparison and the determinism checks.
   */
  readAtmosphere(): AtmosphereReadback {
    const gl = this.gl;
    if (!this.lutFbos) throw new Error('readAtmosphere: nothing rendered yet');
    const read = (pass: PassName, attachment: number): FloatImage => {
      const lut = PASS_TARGETS[pass][attachment]!;
      const [w, h] = LUT_SIZES[lut];
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.lutFbos![pass]);
      gl.readBuffer(gl.COLOR_ATTACHMENT0 + attachment);
      const data = new Float32Array(w * h * 4);
      gl.readPixels(0, 0, w, h, gl.RGBA, gl.FLOAT, data);
      gl.readBuffer(gl.COLOR_ATTACHMENT0);
      return { width: w, height: h, data };
    };
    const out: AtmosphereReadback = {
      transmittance: read('trans', 0),
      multiScattering: read('ms', 0),
      skyView: read('sky', 0),
      skyViewBelow: read('sky', 1),
      light: read('light', 0),
      apIncScatter: read('apInc', 0),
      apIncTransmittance: read('apInc', 1),
      apScatter: read('apPrefix', 0),
      apTransmittance: read('apPrefix', 1),
    };
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    return out;
  }

  private setUniforms(fr: FrameParams, scene: SceneSpec, debugMode: number): void {
    const gl = this.gl;
    const b = fr.basis;
    const L = fr.light;
    this.setHaze(this.main!, fr);
    gl.uniform1f(this.u('u_viewH'), fr.atmosphere.viewHeightM);
    gl.uniform1f(this.u('u_solarLux'), L.solarLux);
    gl.uniform2f(this.u('u_resolution'), fr.width, fr.height);
    gl.uniform3f(this.u('u_camF'), b.forward[0], b.forward[1], b.forward[2]);
    gl.uniform3f(this.u('u_camR'), b.right[0], b.right[1], b.right[2]);
    gl.uniform3f(this.u('u_camU'), b.up[0], b.up[1], b.up[2]);
    gl.uniform2f(this.u('u_tanHalf'), b.tanHalfH, b.tanHalfV);
    gl.uniform1f(this.u('u_hCam'), fr.hCam);
    gl.uniform1f(this.u('u_hGround'), fr.hGround);
    gl.uniform3f(this.u('u_sunDir'), fr.sunDir[0], fr.sunDir[1], fr.sunDir[2]);
    gl.uniform3f(this.u('u_moonDir'), fr.moonDir[0], fr.moonDir[1], fr.moonDir[2]);
    gl.uniform1i(this.u('u_hasSkyMask'), scene.plate.has_sky_mask ? 1 : 0);
    gl.uniform1i(this.u('u_hasDepth'), scene.plate.has_depth ? 1 : 0);
    gl.uniform1f(this.u('u_nominalDepth'), fr.nominalDepthM);

    const geom = new Float32Array(MAX_LAYERS * 4);
    const step = new Float32Array(MAX_LAYERS).fill(1);
    const counts = new Int32Array(MAX_LAYERS);
    const profile = new Float32Array((MAX_LAYERS * MAX_PROFILE_KNOTS) * 2);
    const noiseM = new Float32Array(MAX_LAYERS * NOISE_OCTAVES * 4);
    const noiseC = new Float32Array(MAX_LAYERS * NOISE_OCTAVES * 2);
    fr.layers.forEach((l, li) => {
      geom.set([l.baseM, l.topM, l.betaPerM, l.solid ? 1 : 0], li * 4);
      step[li] = l.stepM;
      counts[li] = l.profile.length;
      l.profile.forEach((k, j) => profile.set([k.distance_km, k.cover_0_1], (li * MAX_PROFILE_KNOTS + j) * 2));
      l.transforms.forEach((t, o) => {
        noiseM.set([t.m00, t.m01, t.m10, t.m11], (li * NOISE_OCTAVES + o) * 4);
        noiseC.set([t.c0, t.c1], (li * NOISE_OCTAVES + o) * 2);
      });
    });
    gl.uniform1i(this.u('u_layerCount'), fr.layers.length);
    gl.uniform4fv(this.u('u_layerGeom'), geom);
    gl.uniform1fv(this.u('u_layerStep'), step);
    gl.uniform1iv(this.u('u_profileCount'), counts);
    gl.uniform4fv(this.u('u_profile'), profile);
    gl.uniform4fv(this.u('u_noiseM'), noiseM);
    gl.uniform4fv(this.u('u_noiseC'), noiseC);
    gl.uniform4f(this.u('u_noiseAmp'), NOISE_AMPLITUDES[0]!, NOISE_AMPLITUDES[1]!, NOISE_AMPLITUDES[2]!, NOISE_AMPLITUDES[3]!);

    gl.uniform1f(this.u('u_fogBeta'), fr.fog.betaPerM);
    gl.uniform1f(this.u('u_fogTop'), fr.fog.topM);
    gl.uniform1f(this.u('u_fogDecayH'), fr.fog.decayScaleM);
    gl.uniform1f(this.u('u_fogAirW'), L.fogAirWeight);

    gl.uniform3fv(this.u('u_sunDiskL'), new Float32Array(L.sunDiskL));
    gl.uniform3fv(this.u('u_moonDiskL'), new Float32Array(L.moonDiskL));
    gl.uniform1f(this.u('u_sunRadius'), SUN_RADIUS_RAD);
    gl.uniform1f(this.u('u_moonRadius'), MOON_RADIUS_RAD);
    gl.uniform1f(this.u('u_pixelAngle'), fr.pixelAngle);
    gl.uniform1i(this.u('u_debugMode'), debugMode);
  }

  /**
   * Blocks until the GPU has finished the last render. Chromium's gl.finish() returned in ~0.1 ms while
   * SwiftShader was still drawing (measured 2026-11-19), so the barrier is a 1-pixel readPixels from the
   * render target, which cannot complete before the draw has.
   */
  finish(): void {
    const gl = this.gl;
    gl.finish();
    if (this.lastTarget === 'float' && this.floatTarget) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.floatTarget.fbo);
      gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.FLOAT, new Float32Array(4));
    } else {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4));
    }
  }

  /** Reads the last 'none' render as RGBA8, top row first (call in the same task as render()). */
  readRGBA(): Uint8Array {
    const gl = this.gl;
    if (this.lastTarget !== 'canvas') throw new Error('readRGBA: last render was a debug render; use readFloat');
    const w = this.canvas.width;
    const h = this.canvas.height;
    const buf = new Uint8Array(w * h * 4);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.pixelStorei(gl.PACK_ALIGNMENT, 1);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf);
    return flipRows(buf, w, h, 4);
  }

  /** Reads the last debug render as float RGBA, top row first. */
  readFloat(): { width: number; height: number; data: Float32Array } {
    const gl = this.gl;
    const t = this.floatTarget;
    if (!t || this.lastTarget !== 'float') throw new Error('readFloat: no debug render to read');
    const buf = new Float32Array(t.width * t.height * 4);
    gl.bindFramebuffer(gl.FRAMEBUFFER, t.fbo);
    gl.readPixels(0, 0, t.width, t.height, gl.RGBA, gl.FLOAT, buf);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    return { width: t.width, height: t.height, data: flipRows(buf, t.width, t.height, 4) };
  }

  dispose(): void {
    const gl = this.gl;
    for (const t of this.tex.values()) gl.deleteTexture(t);
    this.tex.clear();
    this.texSource.clear();
    if (this.floatTarget) {
      gl.deleteFramebuffer(this.floatTarget.fbo);
      gl.deleteTexture(this.floatTarget.tex);
      this.floatTarget = null;
    }
    if (this.dummy) gl.deleteTexture(this.dummy);
    if (this.luts) for (const t of Object.values(this.luts)) gl.deleteTexture(t);
    if (this.lutFbos) for (const f of Object.values(this.lutFbos)) gl.deleteFramebuffer(f);
    if (this.passes) for (const p of Object.values(this.passes)) gl.deleteProgram(p.program);
    if (this.program) gl.deleteProgram(this.program);
    if (this.vao) gl.deleteVertexArray(this.vao);
    this.luts = null;
    this.lutFbos = null;
    this.passes = null;
    this.main = null;
    this.baseKey = null;
    this.program = null;
  }
}

/** WebGL rows are bottom-up; images are top-down. */
export function flipRows<T extends Uint8Array | Float32Array>(buf: T, w: number, h: number, channels: number): T {
  const out = new (buf.constructor as new (n: number) => T)(buf.length);
  const row = w * channels;
  for (let y = 0; y < h; y++) out.set(buf.subarray((h - 1 - y) * row, (h - y) * row), y * row);
  return out;
}

const renderers = new WeakMap<object, SnowGlobeRenderer>();

/**
 * Functional entry point: render(scene, plate, sky mask?, depth?, target canvas). Keeps one renderer
 * (program and resident textures) per canvas.
 */
export function render(
  scene: SceneSpec,
  plate: ImageInput,
  skyMask: ImageInput | null | undefined,
  depth: ImageInput | null | undefined,
  target: HTMLCanvasElement | OffscreenCanvas,
  options: RenderOptions = {},
): RenderInfo {
  let r = renderers.get(target);
  if (!r) {
    r = new SnowGlobeRenderer(target);
    renderers.set(target, r);
  }
  return r.render(scene, { plate, skyMask: skyMask ?? null, depth: depth ?? null }, options);
}
