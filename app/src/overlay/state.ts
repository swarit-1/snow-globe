/**
 * The overlay state machine (PRD §5.2):
 *
 * ```
 * idle ──key-down──▶ listening ──key-up──▶ resolving ──▶ tier0 ──▶ tier1 ──▶ tier2
 *   ▲                    │ Esc / key-up with no speech                 │ any state: Esc → idle
 *   └────────────────────┴──────────── new key-down cancels everything ┘
 * ```
 *
 * plus the outcomes No match, Out of range and a fatal error, and two things that hold across
 * queries: the settings (on-device only, units, motion, measuring) and the network state (Offline).
 *
 * `reduce` is pure. Messages for any generation but the current one are ignored, and after Esc
 * (`idle`) nothing of the dismissed query changes the model. The paint gate (gate.ts) makes the same
 * cut for paints; this model only decides what the panel says. Painted frames enter as a `painted`
 * event from the scheduler, so the tier shown, its marks and its caption change together with the
 * pixels.
 *
 * Two predicates are shared with the controller, so the gate and the model can never disagree about
 * a message: `acceptsScene` (a `parsed` or `scene_changed` takes effect: the gate opens to that scene
 * and the panel resolves it) and `acceptsOutcome` (a No match or Out of range takes effect: the gate
 * withdraws the scene and the surface is cleared). No match and Out of range can follow a scene shown
 * on a stable partial (PRD §7.2), and an answer can follow them (the Out of range offer, PRD §5.2).
 */
import type { Caption } from './caption.ts';
import type { SceneHash, Tier } from './gate.ts';
import type { Destination, Display, ErrorMsg, Inbound, NoMatch, OutOfRange, QueryView } from './protocol.ts';
import type { Units } from './format.ts';

/** Where the overlay is in PRD §5.2. `showing` is tier0, tier1 or tier2, by `shownTier`. */
export type Phase = 'idle' | 'listening' | 'resolving' | 'showing' | 'no_match' | 'out_of_range' | 'error';

/** A tier mark (PRD §5.1): pending, shown, skipped (it ran; a higher tier is shown), rejected (rejected, late or failed to load: hollow), or off. */
export type MarkState = 'pending' | 'shown' | 'skipped' | 'rejected' | 'off';

/** The settings the overlay shows. */
export interface SettingsState {
  readonly onDeviceOnly: boolean;
  readonly cloudSpeech: boolean;
  readonly tier2: boolean;
  readonly locale: string;
  readonly units: Units;
  /** null follows the system's Reduce Motion. */
  readonly reduceMotion: boolean | null;
  readonly measuring: boolean;
}

/** Defaults before the core's first `settings` message: on device, metric, follow the system. */
export const DEFAULT_SETTINGS: SettingsState = Object.freeze({
  onDeviceOnly: false,
  cloudSpeech: false,
  tier2: false,
  locale: 'en-US',
  units: 'metric',
  reduceMotion: null,
  measuring: false,
});

/** The whole model the view renders. */
export interface OverlayModel {
  readonly phase: Phase;
  /** Current generation (0 before the first key press). */
  readonly gen: number;
  /** The live or final transcript (listening, resolving, No match). */
  readonly transcript: string;
  readonly stable: boolean;
  /** Microphone level for the waveform, 0 to 1. */
  readonly level: number;
  /** The key has come up (listening has ended). */
  readonly released: boolean;
  /** The query as last parsed, scrubbed or flipped (what the chips show). */
  readonly query: QueryView | null;
  readonly display: Display | null;
  /** The selected scene. */
  readonly scene: SceneHash | null;
  /** The scene of the frame on screen; after a scrub it lags `scene` until the new scene paints. */
  readonly shownScene: SceneHash | null;
  /** The highest tier painted for `scene` (null before the first paint of the scene). */
  readonly shownTier: Tier | null;
  readonly marks: readonly [MarkState, MarkState, MarkState];
  /** The caption of the frame on screen. */
  readonly caption: Caption | null;
  readonly noMatch: Pick<NoMatch, 'transcript' | 'closest'> | null;
  readonly outOfRange: Omit<OutOfRange, 'v' | 'type' | 'query_gen' | 'core_ns'> | null;
  readonly error: Pick<ErrorMsg, 'code' | 'fatal'> | null;
  readonly settings: SettingsState;
  readonly destinations: Destination['destinations'];
  readonly destinationSeq: number;
  readonly online: boolean;
}

/** The model before anything happened. */
export const INITIAL_MODEL: OverlayModel = Object.freeze({
  phase: 'idle',
  gen: 0,
  transcript: '',
  stable: false,
  level: 0,
  released: false,
  query: null,
  display: null,
  scene: null,
  shownScene: null,
  shownTier: null,
  marks: ['pending', 'pending', 'pending'] as const,
  caption: null,
  noMatch: null,
  outOfRange: null,
  error: null,
  settings: DEFAULT_SETTINGS,
  destinations: [],
  destinationSeq: 0,
  online: true,
});

/** Events from inside the webview. */
export type LocalEvent =
  | { readonly type: 'painted'; readonly gen: number; readonly scene: SceneHash; readonly tier: Tier; readonly caption: Caption }
  /** A frame's pixels could not be loaded: that tier will not paint unless the core sends it again. */
  | { readonly type: 'failed'; readonly gen: number; readonly scene: SceneHash; readonly tier: Tier }
  /** Nothing can come for the selected scene, so the previous scene's frame was taken off screen. */
  | { readonly type: 'cleared'; readonly gen: number }
  | { readonly type: 'local_dismiss' };

/** Marks for a new scene: Tier 2 is off when on-device only or refinement is off (PRD §15.1). */
export function freshMarks(s: SettingsState): readonly [MarkState, MarkState, MarkState] {
  return ['pending', 'pending', s.onDeviceOnly || !s.tier2 ? 'off' : 'pending'];
}

const ACTIVE: ReadonlySet<Phase> = new Set(['listening', 'resolving', 'showing']);
/** Where a scene message takes effect: the live phases, and the two outcomes it can answer. */
const SCENE_PHASES: ReadonlySet<Phase> = new Set(['listening', 'resolving', 'showing', 'no_match', 'out_of_range']);

/** True if a `parsed` or `scene_changed` for `gen` takes effect now (the gate opens to its scene). */
export function acceptsScene(m: OverlayModel, gen: number): boolean {
  return gen === m.gen && SCENE_PHASES.has(m.phase);
}

/** True if a `no_match` or `out_of_range` for `gen` takes effect now (the gate withdraws the scene). */
export function acceptsOutcome(m: OverlayModel, gen: number): boolean {
  return gen === m.gen && ACTIVE.has(m.phase);
}

/**
 * True when nothing more can come for the selected scene: every tier of it was rejected, failed or
 * is off, and none of it is on screen. The panel then says so instead of waiting (or showing the
 * previous scene's frame) for ever.
 */
export function exhausted(m: OverlayModel): boolean {
  return m.scene !== null && m.shownScene !== m.scene && m.marks.every((s) => s === 'rejected' || s === 'off');
}

/** True while the frame on screen belongs to the query's previous scene (after a scrub, flip or offer). */
export function showingPrevious(m: OverlayModel): boolean {
  return m.shownScene !== null && m.scene !== null && m.shownScene !== m.scene;
}

/** One step of the state machine. */
export function reduce(m: OverlayModel, e: Inbound | LocalEvent): OverlayModel {
  switch (e.type) {
    case 'query_started':
      if (!(e.query_gen > m.gen)) return m;
      return {
        ...INITIAL_MODEL,
        phase: 'listening',
        gen: e.query_gen,
        marks: freshMarks(m.settings),
        settings: m.settings,
        destinations: m.destinations,
        destinationSeq: m.destinationSeq,
        online: m.online,
      };
    case 'settings': {
      const settings: SettingsState = {
        onDeviceOnly: e.on_device_only,
        cloudSpeech: e.cloud_speech,
        tier2: e.tier2,
        locale: e.locale,
        units: e.units,
        reduceMotion: e.reduce_motion,
        measuring: e.measuring,
      };
      const off = settings.onDeviceOnly || !settings.tier2;
      const m2 = m.marks[2] === 'pending' && off ? 'off' : m.marks[2] === 'off' && !off ? 'pending' : m.marks[2];
      return { ...m, settings, marks: [m.marks[0], m.marks[1], m2] };
    }
    case 'destination':
      return e.seq > m.destinationSeq ? { ...m, destinations: e.destinations, destinationSeq: e.seq } : m;
    case 'network':
      return { ...m, online: e.online };
    case 'prefetch':
      return m; // pixels only: nothing the panel shows changes
    case 'local_dismiss':
      return m.phase === 'idle' ? m : { ...m, phase: 'idle' };
    case 'error':
      if (e.query_gen === null) return e.fatal ? { ...m, phase: 'error', error: { code: e.code, fatal: true } } : { ...m, error: { code: e.code, fatal: false } };
      if (e.query_gen !== m.gen || m.phase === 'idle') return m;
      return e.fatal ? { ...m, phase: 'error', error: { code: e.code, fatal: true } } : { ...m, error: { code: e.code, fatal: false } };
    default:
      break;
  }
  // Everything below belongs to one generation.
  if (e.type === 'painted' || e.type === 'failed' || e.type === 'cleared' ? e.gen !== m.gen : e.query_gen !== m.gen) return m;
  if (m.phase === 'idle') return m; // dismissed: nothing of this query changes the panel
  switch (e.type) {
    case 'partial':
      return m.phase === 'listening' || m.phase === 'resolving' ? { ...m, transcript: e.text, stable: e.stable } : m;
    case 'audio_level':
      return m.phase === 'listening' ? { ...m, level: e.level } : m;
    case 'released':
      return m.phase === 'listening' ? { ...m, phase: m.shownScene === null ? 'resolving' : 'showing', released: true, level: 0 } : m;
    case 'parsed':
    case 'scene_changed': {
      if (!acceptsScene(m, e.query_gen)) return m;
      const sceneChanged = m.scene !== e.scene_hash;
      const answered = m.phase === 'no_match' || m.phase === 'out_of_range'; // the offer taken, or a place chosen
      return {
        ...m,
        phase: answered ? 'resolving' : m.phase === 'listening' && !m.released ? 'listening' : m.shownScene === null ? 'resolving' : m.phase,
        query: e.query,
        display: e.display,
        scene: e.scene_hash,
        shownTier: sceneChanged ? null : m.shownTier,
        marks: sceneChanged ? freshMarks(m.settings) : m.marks,
        noMatch: null,
        outOfRange: null,
        // A non-fatal error was about the scene before; it does not carry over to a new one.
        error: sceneChanged ? null : m.error,
      };
    }
    case 'tier_rejected':
      return markUnpainted(m, e.scene_hash, e.tier);
    case 'failed':
      return markUnpainted(m, e.scene, e.tier);
    case 'no_match':
    case 'out_of_range': {
      if (!acceptsOutcome(m, e.query_gen)) return m;
      // The scene is withdrawn (the controller has cleared the surface): nothing of it stays. Out of
      // range keeps the query the core last resolved, so Left or Right can scrub back from it.
      const withdrawn = { ...m, scene: null, shownScene: null, shownTier: null, caption: null, marks: freshMarks(m.settings) };
      if (e.type === 'no_match') return { ...withdrawn, phase: 'no_match', query: null, display: null, transcript: e.transcript, noMatch: { transcript: e.transcript, closest: e.closest } };
      const { v: _v, type: _t, query_gen: _g, core_ns: _c, ...rest } = e;
      return { ...withdrawn, phase: 'out_of_range', outOfRange: rest };
    }
    case 'cancelled':
      return { ...m, phase: 'idle' };
    case 'painted': {
      if (!ACTIVE.has(m.phase) || e.scene !== m.scene) return m;
      const marks = m.marks.map((s, i) => (i === e.tier ? 'shown' : i < e.tier && s === 'pending' ? 'skipped' : s)) as unknown as [MarkState, MarkState, MarkState];
      return {
        ...m,
        phase: m.phase === 'listening' && !m.released ? 'listening' : 'showing',
        shownTier: e.tier,
        shownScene: e.scene,
        caption: e.caption,
        marks,
      };
    }
    case 'cleared':
      return m.shownScene === null ? m : { ...m, shownScene: null, shownTier: null, caption: null };
    default:
      return m;
  }
}

/**
 * A tier of `scene` that will not paint (rejected by the core, or its pixels failed): a pending mark
 * turns hollow. A mark already shown, skipped or off keeps its state (off is the on-device-only
 * setting's visible state, PRD §15.1).
 */
function markUnpainted(m: OverlayModel, scene: SceneHash, tier: Tier): OverlayModel {
  if (scene !== m.scene || !ACTIVE.has(m.phase) || m.marks[tier] !== 'pending') return m;
  const marks = m.marks.map((s, i) => (i === tier ? 'rejected' : s)) as unknown as [MarkState, MarkState, MarkState];
  return { ...m, marks };
}

/** "tier0", "tier1" or "tier2" while showing, else the phase (the names of PRD §5.2). */
export function stateName(m: OverlayModel): string {
  return m.phase === 'showing' && m.shownTier !== null ? `tier${m.shownTier}` : m.phase;
}
