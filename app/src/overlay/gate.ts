/**
 * The webview paint gate (PRD §10.3): every paint is checked, immediately before compositing, on
 * (query_gen, scene_hash, tier) against what is current; anything else is dropped as stale.
 *
 * This is a mirror of `PaintGate` in app/core/src/lifecycle.rs, rule for rule and in the same order:
 * 1. a result made for another generation is dropped (`generation`);
 * 2. after Esc nothing paints until the next key press (`dismissed`);
 * 3. a result for another scene (scrubbed, flipped, or not parsed yet) is dropped (`scene`);
 * 4. a tier at or below the one already shown for this scene is dropped (`tier`).
 *
 * Two deliberate differences:
 * 1. In Rust `begin` is called with a generation the counter has just handed out, so it is always
 *    newer. Here the generation arrives in a message, so `begin` ignores one that is not newer than
 *    the current generation: a duplicate or late `query_started` can never reopen a gate that Esc closed.
 * 2. `clearScene` has no Rust counterpart yet. A query can show a scene on a stable partial, before
 *    key-up (PRD §7.2), and then end as No match or Out of range: the scene is withdrawn, so nothing
 *    of it paints, but unlike Esc the gate stays open for an answer of the same key press (taking the
 *    Out of range offer). The Rust simulation never withdraws a scene.
 * With the core's increasing generations and no withdrawn scene the two gates decide identically;
 * the key-mash mirror (harness/keymash.ts) runs the Rust simulation's streams through this gate and
 * gets the Rust counts exactly.
 */

/** The three rendering tiers (PRD §9): 0 closest real frame, 1 relit plate, 2 guarded refinement. */
export type Tier = 0 | 1 | 2;

/** Every tier, in order. */
export const TIERS: readonly Tier[] = [0, 1, 2];

/**
 * What a frame shows (PRD §10.3 `scene_hash`): the core's 64-bit FNV-1a `SceneHash`, written as 16
 * lower-case hex digits. It travels as a string because a JSON number cannot carry 64 bits exactly in
 * JavaScript (it would round to 53).
 */
export type SceneHash = string;

/** What a finished stage wants to paint. */
export interface PaintKey {
  /** Generation the result was made for. */
  readonly gen: number;
  /** Scene the result shows. */
  readonly scene: SceneHash;
  /** Its tier. */
  readonly tier: Tier;
}

/** What is current, as the gate sees it. */
export interface GateState {
  /** Current generation (0 before the first key press). */
  readonly gen: number;
  /** Current scene, once the query has been parsed. */
  readonly scene: SceneHash | null;
  /** Highest tier painted for this generation and scene. */
  readonly shown: Tier | null;
  /** True after Esc: the overlay is idle and nothing paints until the next key press. */
  readonly dismissed: boolean;
}

/** Why a paint was dropped. */
export type StaleReason = 'generation' | 'dismissed' | 'scene' | 'tier';

/** The gate's verdict. */
export type PaintDecision = { readonly accept: true } | { readonly accept: false; readonly reason: StaleReason };

/** The accepting verdict (shared, frozen). */
export const ACCEPT: PaintDecision = Object.freeze({ accept: true });

const DROPS: Readonly<Record<StaleReason, PaintDecision>> = Object.freeze({
  generation: Object.freeze({ accept: false, reason: 'generation' }),
  dismissed: Object.freeze({ accept: false, reason: 'dismissed' }),
  scene: Object.freeze({ accept: false, reason: 'scene' }),
  tier: Object.freeze({ accept: false, reason: 'tier' }),
});

/** A dropping verdict for `reason`. */
export function drop(reason: StaleReason): PaintDecision {
  return DROPS[reason];
}

/** The initial state: generation 0, nothing parsed, nothing shown. */
export const INITIAL_GATE_STATE: GateState = Object.freeze({ gen: 0, scene: null, shown: null, dismissed: false });

/**
 * The pure rule (lifecycle.rs `PaintGate::check`): nothing after Esc; otherwise only the current
 * generation and scene, and only a newer tier.
 */
export function checkPaint(current: GateState, incoming: PaintKey): PaintDecision {
  if (incoming.gen !== current.gen) return DROPS.generation;
  if (current.dismissed) return DROPS.dismissed;
  if (current.scene !== incoming.scene) return DROPS.scene;
  if (current.shown !== null && incoming.tier <= current.shown) return DROPS.tier;
  return ACCEPT;
}

/** The operations the overlay needs from a gate (the real one, or a deliberately broken one in tests). */
export interface Gate {
  /** What is current. */
  readonly state: GateState;
  /** A key press began `gen`. Returns false (and changes nothing) unless `gen` is newer. */
  begin(gen: number): boolean;
  /** Esc: nothing paints, and late parse results cannot set a scene, until the next `begin`. */
  dismiss(): void;
  /** The query `gen` resolved to `scene`, or was scrubbed or flipped to it. Returns true if it took. */
  setScene(gen: number, scene: SceneHash): boolean;
  /**
   * The query `gen` no longer has a scene (it ended as No match or Out of range): nothing paints
   * until `setScene` gives it one again. Returns true if a scene was withdrawn.
   */
  clearScene(gen: number): boolean;
  /** Check a paint without recording it. */
  check(key: PaintKey): PaintDecision;
  /** Check a paint and, if accepted, record its tier as shown. Call it immediately before compositing. */
  tryPaint(key: PaintKey): PaintDecision;
}

/** The paint gate (PRD §10.3). */
export class PaintGate implements Gate {
  #state: GateState = INITIAL_GATE_STATE;

  get state(): GateState {
    return this.#state;
  }

  begin(gen: number): boolean {
    if (!(gen > this.#state.gen)) return false;
    this.#state = { gen, scene: null, shown: null, dismissed: false };
    return true;
  }

  dismiss(): void {
    this.#state = { gen: this.#state.gen, scene: null, shown: null, dismissed: true };
  }

  setScene(gen: number, scene: SceneHash): boolean {
    const s = this.#state;
    if (gen === s.gen && !s.dismissed && s.scene !== scene) {
      this.#state = { gen: s.gen, scene, shown: null, dismissed: false };
      return true;
    }
    return false;
  }

  clearScene(gen: number): boolean {
    const s = this.#state;
    if (gen !== s.gen || s.dismissed || s.scene === null) return false;
    this.#state = { gen: s.gen, scene: null, shown: null, dismissed: false };
    return true;
  }

  check(key: PaintKey): PaintDecision {
    return checkPaint(this.#state, key);
  }

  tryPaint(key: PaintKey): PaintDecision {
    const decision = checkPaint(this.#state, key);
    if (decision.accept) this.#state = { ...this.#state, shown: key.tier };
    return decision;
  }
}

/** True if `s` is a scene hash as the protocol carries it (16 lower-case hex digits). */
export function isSceneHash(s: unknown): s is SceneHash {
  return typeof s === 'string' && /^[0-9a-f]{16}$/.test(s);
}

/** Writes a 64-bit value as a scene hash (16 lower-case hex digits). */
export function sceneHashFromBigInt(v: bigint): SceneHash {
  return BigInt.asUintN(64, v).toString(16).padStart(16, '0');
}

/**
 * FNV-1a over the parts with a 0xff separator after each, exactly as lifecycle.rs
 * `SceneHash::of_parts`, so the webview can recompute a hash the core sent (tests pin the Rust value).
 */
export function sceneHashOfParts(parts: readonly Uint8Array[]): SceneHash {
  let h = 0xcbf29ce484222325n;
  const prime = 0x100000001b3n;
  const mix = (b: number): void => {
    h ^= BigInt(b);
    h = BigInt.asUintN(64, h * prime);
  };
  for (const part of parts) {
    for (const b of part) mix(b);
    mix(0xff);
  }
  return sceneHashFromBigInt(h);
}
