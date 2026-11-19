import { createHash } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { sha256HexBytes, sha256Hex } from './sha256.ts';

describe('sha256', () => {
  it('matches FIPS test vectors', () => {
    expect(sha256Hex('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
    expect(sha256Hex('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    expect(sha256Hex('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq')).toBe(
      '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1',
    );
  });
  it('matches node:crypto on lengths around block boundaries', () => {
    for (const n of [1, 55, 56, 57, 63, 64, 65, 119, 120, 1000, 100_003]) {
      const bytes = new Uint8Array(n).map((_, i) => (i * 31 + n) & 255);
      expect(sha256HexBytes(bytes)).toBe(createHash('sha256').update(bytes).digest('hex'));
    }
  });
});
