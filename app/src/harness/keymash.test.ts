import { describe, expect, it } from 'vitest';
import { CYCLES_PER_SEED, FAULTS, faultyGate, generationOnlyGate, realGate, SEEDS, simulate, type Fault, type Report } from './keymash.ts';

/**
 * The Rust key-mash's own output (app/core/tests/keymash.rs at commit 479d4d8, run on 22 Nov 2026
 * with `cargo test -p snow-globe-core --test keymash -- --nocapture`). The TypeScript mirror replays
 * the same streams through the webview's PaintGate, so every count must match. If keymash.rs changes
 * its simulation, re-run it and update these numbers; if only one side's gate changes, this fails.
 */
const RUST_REAL: Record<string, Omit<Report, 'stalePaints' | 'wrongDrops' | 'simSeconds'> & { simSecondsRounded: number }> = {
  '0xc0ffee': {
    presses: 10000, escapes: 534, events: 69957, composites: 14182, accepted: 9414,
    dropped: { dismissed: 107, generation: 3263, scene: 726, tier: 672 },
    cancelledStages: 9247, lateResults: 3214, scrubs: 1386, generationsPainted: 3424, simSecondsRounded: 13688,
  },
  '0x5eed0002': {
    presses: 10000, escapes: 479, events: 70992, composites: 14577, accepted: 9735,
    dropped: { dismissed: 96, generation: 3273, scene: 742, tier: 731 },
    cancelledStages: 9226, lateResults: 3202, scrubs: 1499, generationsPainted: 3518, simSecondsRounded: 13925,
  },
  '0xbadc0de5': {
    presses: 10000, escapes: 530, events: 70187, composites: 14282, accepted: 9656,
    dropped: { dismissed: 77, generation: 3181, scene: 737, tier: 631 },
    cancelledStages: 9252, lateResults: 3101, scrubs: 1458, generationsPainted: 3458, simSecondsRounded: 13921,
  },
  '0xddba115': {
    presses: 10000, escapes: 490, events: 70510, composites: 14311, accepted: 9609,
    dropped: { dismissed: 92, generation: 3269, scene: 708, tier: 633 },
    cancelledStages: 9303, lateResults: 3204, scrubs: 1420, generationsPainted: 3483, simSecondsRounded: 13907,
  },
};

/** The Rust negative controls' (stale paints, paints) per seed, in SEEDS order. */
const RUST_MUTANTS: Record<'generation-only' | Fault, [number, number][]> = {
  'generation-only': [[1505, 10919], [1569, 11304], [1445, 11101], [1433, 11042]],
  'esc-cancels-token-only': [[100, 9514], [89, 9824], [73, 9729], [89, 9698]],
  'no-tier-order': [[672, 10086], [731, 10466], [631, 10287], [633, 10242]],
  'no-scene-check': [[726, 10140], [742, 10477], [737, 10393], [708, 10317]],
};

const hex = (s: bigint): string => `0x${s.toString(16)}`;

describe('key-mash mirror of app/core/tests/keymash.rs (PRD §10.3)', () => {
  it('makes zero stale paints in 40,000 cycles and reproduces every Rust count exactly', () => {
    const t0 = performance.now();
    let total = 0;
    for (const seed of SEEDS) {
      const r = simulate(seed, CYCLES_PER_SEED, realGate());
      const want = RUST_REAL[hex(seed)]!;
      console.log(
        `TS mirror seed ${hex(seed)}: ${r.presses} presses, ${r.escapes} Esc, ${r.events} events, ${r.composites} composites, ` +
          `${r.accepted} painted, ${r.stalePaints} stale, ${r.wrongDrops} fresh dropped, dropped ${JSON.stringify(r.dropped)}, ` +
          `${r.cancelledStages} cancelled stages, ${r.lateResults} late results, ${r.scrubs} scrubs, ${r.generationsPainted} generations painted, ${r.simSeconds.toFixed(0)} s simulated`,
      );
      expect(r.stalePaints).toBe(0);
      expect(r.wrongDrops).toBe(0);
      const { stalePaints: _s, wrongDrops: _w, simSeconds, ...rest } = r;
      const { simSecondsRounded, ...wantRest } = want;
      expect(rest).toEqual(wantRest);
      expect(Math.round(simSeconds)).toBe(simSecondsRounded);
      // The Rust test's exercise thresholds, which this mirror must meet too.
      expect(r.dropped.generation).toBeGreaterThan(100);
      expect(r.dropped.scene).toBeGreaterThan(100);
      expect(r.dropped.tier).toBeGreaterThan(100);
      expect(r.dropped.dismissed).toBeGreaterThan(50);
      expect(r.lateResults).toBeGreaterThan(1000);
      expect(r.accepted).toBeGreaterThan(r.presses / 2);
      total += r.presses;
    }
    console.log(`TS mirror: ${total} cycles in ${(performance.now() - t0).toFixed(0)} ms`);
    expect(total).toBe(40_000);
  });

  it('catches the generation-only gate as the Rust test does, with the same counts', () => {
    SEEDS.forEach((seed, i) => {
      const r = simulate(seed, CYCLES_PER_SEED, generationOnlyGate());
      expect([r.stalePaints, r.accepted]).toEqual(RUST_MUTANTS['generation-only'][i]);
      expect(r.stalePaints).toBeGreaterThan(100);
    });
  });

  for (const fault of FAULTS) {
    it(`catches the broken rule ${fault}, with the Rust counts`, () => {
      SEEDS.forEach((seed, i) => {
        const r = simulate(seed, CYCLES_PER_SEED, faultyGate(fault));
        expect([r.stalePaints, r.accepted]).toEqual(RUST_MUTANTS[fault][i]);
        expect(r.stalePaints).toBeGreaterThan(10);
      });
    });
  }

  it('is deterministic', () => {
    const a = simulate(SEEDS[0]!, 500, realGate());
    const b = simulate(SEEDS[0]!, 500, realGate());
    expect([a.events, a.accepted, a.dropped]).toEqual([b.events, b.accepted, b.dropped]);
  });
});
