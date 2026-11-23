/**
 * The key-mash simulations' random numbers: SplitMix64, bit for bit as the `Rng` of
 * app/core/tests/keymash.rs, so the TypeScript mirror (keymash.ts) replays the Rust streams exactly
 * (PRD §10.3 stress test). BigInt keeps the 64-bit arithmetic exact.
 */

const M64 = (1n << 64n) - 1n;
const GOLDEN = 0x9e3779b97f4a7c15n;
const MIX1 = 0xbf58476d1ce4e5b9n;
const MIX2 = 0x94d049bb133111ebn;
const TWO_53 = 2 ** 53;

/** SplitMix64 (the Rust test's `Rng`). */
export class Rng {
  #s: bigint;

  constructor(seed: bigint | number) {
    this.#s = BigInt.asUintN(64, BigInt(seed));
  }

  /** The next 64-bit value. */
  next(): bigint {
    this.#s = (this.#s + GOLDEN) & M64;
    let z = this.#s;
    z = ((z ^ (z >> 30n)) * MIX1) & M64;
    z = ((z ^ (z >> 27n)) * MIX2) & M64;
    return z ^ (z >> 31n);
  }

  /** `lo + next % (hi - lo)`, as the Rust `range` (lo < hi, both below 2^53). */
  range(lo: number, hi: number): number {
    return lo + Number(this.next() % BigInt(hi - lo));
  }

  /** True with probability `p`, as the Rust `chance` (53 high bits over 2^53, compared with <=). */
  chance(p: number): boolean {
    return Number(this.next() >> 11n) / TWO_53 <= p;
  }

  /** Mostly `lo..hi`, with a 5 % tail up to `tail` (the Rust `heavy`). */
  heavy(lo: number, hi: number, tail: number): number {
    return this.chance(0.05) ? this.range(hi, tail) : this.range(lo, hi);
  }

  /** A float in [0, 1) from the 53 high bits (for the browser run's delays). */
  float(): number {
    return Number(this.next() >> 11n) / TWO_53;
  }

  /** A float in [lo, hi) (for the browser run's delays, in milliseconds). */
  uniform(lo: number, hi: number): number {
    return lo + (hi - lo) * this.float();
  }

  /** Mostly [lo, hi), with a 5 % tail up to `tail` (float version of `heavy`). */
  heavyFloat(lo: number, hi: number, tail: number): number {
    return this.chance(0.05) ? this.uniform(hi, tail) : this.uniform(lo, hi);
  }
}
