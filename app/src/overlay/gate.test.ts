import { describe, expect, it } from 'vitest';
import { checkPaint, INITIAL_GATE_STATE, isSceneHash, PaintGate, sceneHashFromBigInt, sceneHashOfParts, type PaintKey, type Tier } from './gate.ts';

const A = sceneHashFromBigInt(1n);
const B = sceneHashFromBigInt(2n);
const key = (gen: number, scene: string, tier: Tier): PaintKey => ({ gen, scene, tier });
const enc = (s: string): Uint8Array => new TextEncoder().encode(s);

describe('PaintGate mirrors lifecycle.rs', () => {
  it('follows gate_rules from lifecycle.rs', () => {
    const g = new PaintGate();
    expect(g.begin(1)).toBe(true);
    expect(g.tryPaint(key(1, A, 0))).toEqual({ accept: false, reason: 'scene' }); // not parsed yet
    g.setScene(1, A);
    expect(g.tryPaint(key(1, A, 1))).toEqual({ accept: true });
    expect(g.tryPaint(key(1, A, 0))).toEqual({ accept: false, reason: 'tier' });
    expect(g.tryPaint(key(1, A, 1))).toEqual({ accept: false, reason: 'tier' });
    expect(g.tryPaint(key(1, B, 2))).toEqual({ accept: false, reason: 'scene' });
    g.setScene(1, B);
    expect(g.tryPaint(key(1, A, 2))).toEqual({ accept: false, reason: 'scene' });
    expect(g.tryPaint(key(1, B, 1))).toEqual({ accept: true });
    g.begin(2);
    expect(g.tryPaint(key(1, B, 2))).toEqual({ accept: false, reason: 'generation' });
    g.setScene(1, A);
    expect(g.state.scene).toBeNull(); // an old generation cannot set the scene
  });

  it('drops a late paint after Esc, and a late parse cannot reopen it (a_late_paint_after_esc_is_dropped)', () => {
    const g = new PaintGate();
    g.begin(1);
    g.setScene(1, A);
    expect(g.tryPaint(key(1, A, 0)).accept).toBe(true);
    g.dismiss();
    expect(g.tryPaint(key(1, A, 2))).toEqual({ accept: false, reason: 'dismissed' });
    g.setScene(1, B);
    expect(g.state.scene).toBeNull();
    expect(g.tryPaint(key(1, B, 1))).toEqual({ accept: false, reason: 'dismissed' });
    g.begin(2);
    g.setScene(2, A);
    expect(g.tryPaint(key(2, A, 0)).accept).toBe(true);
  });

  it('checks the generation before Esc, as Rust does (an old generation reads generation, not dismissed)', () => {
    const g = new PaintGate();
    g.begin(3);
    g.setScene(3, A);
    g.dismiss();
    expect(g.check(key(2, A, 0))).toEqual({ accept: false, reason: 'generation' });
    expect(g.check(key(3, A, 0))).toEqual({ accept: false, reason: 'dismissed' });
  });

  it('accepts only strictly rising tiers for a scene, and restarts them when the scene changes', () => {
    const g = new PaintGate();
    g.begin(1);
    g.setScene(1, A);
    expect(g.tryPaint(key(1, A, 0)).accept).toBe(true);
    expect(g.tryPaint(key(1, A, 2)).accept).toBe(true);
    expect(g.tryPaint(key(1, A, 1))).toEqual({ accept: false, reason: 'tier' });
    g.setScene(1, B);
    expect(g.state.shown).toBeNull();
    expect(g.tryPaint(key(1, B, 0)).accept).toBe(true);
    // Setting the same scene again changes nothing (shown is kept), as in Rust.
    g.setScene(1, B);
    expect(g.state.shown).toBe(0);
  });

  it('check() never records; tryPaint() records only what it accepts', () => {
    const g = new PaintGate();
    g.begin(1);
    g.setScene(1, A);
    expect(g.check(key(1, A, 1)).accept).toBe(true);
    expect(g.state.shown).toBeNull();
    expect(g.tryPaint(key(1, B, 1)).accept).toBe(false);
    expect(g.state.shown).toBeNull();
  });

  it('withdraws a scene without dismissing (No match or Out of range after a stable partial): nothing paints until a new scene', () => {
    const g = new PaintGate();
    g.begin(1);
    g.setScene(1, A);
    expect(g.tryPaint(key(1, A, 0))).toEqual({ accept: true });
    expect(g.clearScene(2)).toBe(false); // not the current generation
    expect(g.clearScene(1)).toBe(true);
    expect(g.state).toEqual({ gen: 1, scene: null, shown: null, dismissed: false });
    expect(g.check(key(1, A, 1))).toEqual({ accept: false, reason: 'scene' });
    expect(g.clearScene(1)).toBe(false); // nothing left to withdraw
    // The same key press can still be answered (the Out of range offer), unlike after Esc.
    expect(g.setScene(1, B)).toBe(true);
    expect(g.tryPaint(key(1, B, 0))).toEqual({ accept: true });
    g.dismiss();
    expect(g.clearScene(1)).toBe(false);
    expect(g.setScene(1, A)).toBe(false);
  });

  it('ignores a begin that is not newer (a deliberate difference from Rust)', () => {
    const g = new PaintGate();
    g.begin(5);
    g.setScene(5, A);
    g.dismiss();
    expect(g.begin(5)).toBe(false); // a duplicate query_started cannot reopen a dismissed gate
    expect(g.state.dismissed).toBe(true);
    expect(g.begin(4)).toBe(false);
    expect(g.state.gen).toBe(5);
    expect(g.begin(6)).toBe(true);
    expect(g.state).toEqual({ gen: 6, scene: null, shown: null, dismissed: false });
  });

  it('starts at generation 0 with nothing current', () => {
    expect(new PaintGate().state).toEqual(INITIAL_GATE_STATE);
    expect(checkPaint(INITIAL_GATE_STATE, key(0, A, 0))).toEqual({ accept: false, reason: 'scene' });
  });
});

describe('scene hashes', () => {
  it('matches the pinned FNV-1a value of lifecycle.rs (of_parts(&[b""]) = 0xaf64_724c_8602_eb6e)', () => {
    expect(sceneHashOfParts([enc('')])).toBe('af64724c8602eb6e');
  });

  it('is stable and separates parts', () => {
    expect(sceneHashOfParts([enc('rainier.paradise-mountain'), enc('x')])).toBe(sceneHashOfParts([enc('rainier.paradise-mountain'), enc('x')]));
    expect(sceneHashOfParts([enc('ab'), enc('c')])).not.toBe(sceneHashOfParts([enc('a'), enc('bc')]));
  });

  it('writes 64-bit values as 16 lower-case hex digits, wrapping like u64', () => {
    expect(sceneHashFromBigInt(0n)).toBe('0000000000000000');
    expect(sceneHashFromBigInt(2n ** 64n - 1n)).toBe('ffffffffffffffff');
    expect(sceneHashFromBigInt(2n ** 64n + 5n)).toBe('0000000000000005');
    expect(isSceneHash('af64724c8602eb6e')).toBe(true);
    expect(isSceneHash('AF64724C8602EB6E')).toBe(false);
    expect(isSceneHash('af64724c8602eb6')).toBe(false);
    expect(isSceneHash(12345)).toBe(false);
  });
});
