/**
 * Deliberately broken overlays for the browser stress run (PRD §10.3: the stress test must show it
 * catches a wrong gate, as app/core/tests/keymash.rs does with its broken gates). Each variant breaks
 * one rule of the webview's paint path; the stress run must catch every one.
 *
 * | variant                  | what is broken                                                                 | caught by          |
 * |--------------------------|--------------------------------------------------------------------------------|--------------------|
 * | generation-only          | the gate checks the generation only (no scene, no tier order, Esc ignored)     | stale paints       |
 * | esc-cancels-stages-only  | Esc drops the pending frames but leaves the gate open (Rust's EscCancelsTokenOnly) | stale paints   |
 * | no-tier-order            | the gate accepts a tier at or below the one shown                              | stale paints       |
 * | no-scene-check           | the gate accepts a frame of another scene (scrubbed away)                     | stale paints       |
 * | check-at-receipt         | the gate is checked when a frame arrives, not again before it is drawn         | stale paints       |
 * | commit-next-frame        | the gate is checked in one frame callback and the frame drawn in the next     | stale paints       |
 * | no-clear-on-reset        | a new key press or Esc leaves the old frames (and fades) on screen            | residue            |
 * | hold-without-deadline    | the Tier 0 skip rule waits for Tier 1 with no deadline                         | hold overruns      |
 * | keeps-scene-on-outcome   | No match or Out of range leaves the scene of a stable partial open and on screen | stale paints     |
 * | acks-while-hidden        | the destination is acknowledged while the panel shows no chip (idle)           | unshown acks       |
 *
 * They live here, in the harness, built from the overlay's own seams (an injected gate, scheduler
 * subclass, controller subclass or surface wrapper): the app's code has no fault switches.
 */
import { OverlayController } from '../overlay/controller.ts';
import { ACCEPT, drop, PaintGate, type Gate, type GateState, type PaintDecision, type PaintKey, type StaleReason } from '../overlay/gate.ts';
import type { MountOptions } from '../overlay/index.ts';
import { hintKey, PaintScheduler, type Entry } from '../overlay/scheduler.ts';
import type { Caption } from '../overlay/caption.ts';
import type { OverlayModel } from '../overlay/state.ts';

export const VARIANTS = [
  'real',
  'generation-only',
  'esc-cancels-stages-only',
  'no-tier-order',
  'no-scene-check',
  'check-at-receipt',
  'commit-next-frame',
  'no-clear-on-reset',
  'hold-without-deadline',
  'keeps-scene-on-outcome',
  'acks-while-hidden',
] as const;

export type Variant = (typeof VARIANTS)[number];

/** The checks a broken variant can be caught by. */
export type Catch = 'stalePaints' | 'residue' | 'holdOverruns' | 'unshownAcks';

/** Which check must catch each broken variant. */
export const CAUGHT_BY: Readonly<Record<Exclude<Variant, 'real'>, Catch>> = {
  'generation-only': 'stalePaints',
  'esc-cancels-stages-only': 'stalePaints',
  'no-tier-order': 'stalePaints',
  'no-scene-check': 'stalePaints',
  'check-at-receipt': 'stalePaints',
  'commit-next-frame': 'stalePaints',
  'no-clear-on-reset': 'residue',
  'hold-without-deadline': 'holdOverruns',
  'keeps-scene-on-outcome': 'stalePaints',
  'acks-while-hidden': 'unshownAcks',
};

/** Checks the generation only. */
class GenerationOnlyGate implements Gate {
  #gen = 0;
  get state(): GateState {
    return { gen: this.#gen, scene: null, shown: null, dismissed: false };
  }
  begin(gen: number): boolean {
    if (!(gen > this.#gen)) return false;
    this.#gen = gen;
    return true;
  }
  dismiss(): void {}
  setScene(): boolean {
    return false;
  }
  clearScene(): boolean {
    return false;
  }
  check(key: PaintKey): PaintDecision {
    return key.gen === this.#gen ? ACCEPT : drop('generation');
  }
  tryPaint(key: PaintKey): PaintDecision {
    return this.check(key);
  }
}

/** The real gate with one rule turned into an accept. */
class RuleBrokenGate implements Gate {
  readonly #inner = new PaintGate();
  readonly #ignore: StaleReason;
  constructor(ignore: StaleReason) {
    this.#ignore = ignore;
  }
  get state(): GateState {
    return this.#inner.state;
  }
  begin(gen: number): boolean {
    return this.#inner.begin(gen);
  }
  dismiss(): void {
    this.#inner.dismiss();
  }
  setScene(gen: number, scene: string): boolean {
    return this.#inner.setScene(gen, scene);
  }
  clearScene(gen: number): boolean {
    return this.#inner.clearScene(gen);
  }
  check(key: PaintKey): PaintDecision {
    const d = this.#inner.check(key);
    return !d.accept && d.reason === this.#ignore ? ACCEPT : d;
  }
  tryPaint(key: PaintKey): PaintDecision {
    const d = this.#inner.tryPaint(key);
    return !d.accept && d.reason === this.#ignore ? ACCEPT : d;
  }
}

/** Esc stops the pending work but does not close the gate. */
class EscStagesOnlyController extends OverlayController {
  protected override onCancelled(): void {
    this.scheduler.reset();
  }
}

/** No match and Out of range change the panel's words but leave the scene open and its frame up. */
class KeepsSceneController extends OverlayController {
  protected override onOutcome(): void {}
}

/** Acknowledges the destination whatever the panel shows, idle included. */
class AcksWhileHiddenController extends OverlayController {
  protected override chipShown(_m: OverlayModel): boolean {
    return true;
  }
}

/** Checked at receipt only: never re-checked while waiting, and drawn even if the gate now refuses. */
class CheckAtReceiptScheduler extends PaintScheduler {
  protected override checkAtFrame(): PaintDecision {
    return ACCEPT;
  }
  protected override acceptAtPaint(key: PaintKey): PaintDecision {
    this.o.gate.tryPaint(key);
    return ACCEPT;
  }
}

/** Checked in this frame callback, drawn in the next one. */
class CommitNextFrameScheduler extends PaintScheduler {
  protected override draw(best: Entry, ready: readonly Entry[], _frameMs: number, caption: Caption, checkedMs: number): void {
    this.o.clock.requestFrame((next) => super.draw(best, ready, next, caption, checkedMs));
  }
}

/** The skip rule holds Tier 0 for as long as Tier 1 is expected, with no deadline. */
class HoldForeverScheduler extends PaintScheduler {
  protected override holdDeadline(best: Entry, now: number): number | null {
    if (best.key.tier !== 0 || this.o.gate.state.shown !== null) return null;
    const { gen, scene } = best.key;
    const waiting = this.entries.some((e) => e.key.tier === 1 && e.key.gen === gen && e.key.scene === scene) || this.hints.has(hintKey(gen, scene, 1));
    return waiting ? now + 3_600_000 : null;
  }
}

/** The mount options that build `variant`. */
export function variantOptions(variant: Variant): Pick<MountOptions, 'gate' | 'scheduler' | 'controller' | 'surface'> {
  switch (variant) {
    case 'real':
      return {};
    case 'generation-only':
      return { gate: new GenerationOnlyGate() };
    case 'esc-cancels-stages-only':
      return { controller: (o) => new EscStagesOnlyController(o) };
    case 'no-tier-order':
      return { gate: new RuleBrokenGate('tier') };
    case 'no-scene-check':
      return { gate: new RuleBrokenGate('scene') };
    case 'check-at-receipt':
      return { scheduler: (o) => new CheckAtReceiptScheduler(o) };
    case 'commit-next-frame':
      return { scheduler: (o) => new CommitNextFrameScheduler(o) };
    case 'no-clear-on-reset':
      return {
        surface: (view) => ({
          commit: (r) => view.commit(r),
          clear: () => {},
          configure: (s) => view.configure(s),
          burnIn: (c) => view.burnIn(c),
        }),
      };
    case 'hold-without-deadline':
      return { scheduler: (o) => new HoldForeverScheduler(o) };
    case 'keeps-scene-on-outcome':
      return { controller: (o) => new KeepsSceneController(o) };
    case 'acks-while-hidden':
      return { controller: (o) => new AcksWhileHiddenController(o) };
  }
}
