import { describe, expect, it } from 'vitest';
import { isCanonicalUtcSeconds, parseUtcSeconds } from './time.ts';

describe('strict UTC timestamps', () => {
  it('parses real instants, with optional seconds and fractions', () => {
    expect(parseUtcSeconds('2026-11-18T19:00:00Z')).toBe(Date.UTC(2026, 10, 18, 19) / 1000);
    expect(parseUtcSeconds('2026-11-18T19:00Z')).toBe(Date.UTC(2026, 10, 18, 19) / 1000);
    expect(parseUtcSeconds('2024-02-29T12:00:00Z')).toBe(Date.UTC(2024, 1, 29, 12) / 1000);
    expect(parseUtcSeconds('2026-11-18T19:00:00.25Z')).toBe(Date.UTC(2026, 10, 18, 19) / 1000 + 0.25);
  });

  it('rejects dates and times that do not exist instead of rolling them over (Date.parse accepts them)', () => {
    for (const s of ['2026-02-30T00:00:00Z', '2025-02-29T00:00:00Z', '2026-04-31T00:00:00Z', '2026-11-18T24:00:00Z', '2026-11-18T23:60:00Z', '2026-11-18T23:59:60Z', '2026-13-01T00:00:00Z', '0050-01-01T00:00:00Z']) {
      expect(parseUtcSeconds(s), s).toBeNull();
    }
    expect(Number.isNaN(Date.parse('2026-02-30T00:00:00Z'))).toBe(false); // why the strict parser exists
    for (const s of ['2026-11-18T19:00:00', '2026-11-18 19:00:00Z', '2026-11-18T19:00:00+00:00', '']) expect(parseUtcSeconds(s), s).toBeNull();
  });

  it('accepts only the canonical seconds form for run initialisation times', () => {
    expect(isCanonicalUtcSeconds('2026-11-18T12:00:00Z')).toBe(true);
    expect(isCanonicalUtcSeconds('2026-11-18T12:00Z')).toBe(false);
    expect(isCanonicalUtcSeconds('2026-11-18T12:00:00.000Z')).toBe(false);
    expect(isCanonicalUtcSeconds('2026-02-30T12:00:00Z')).toBe(false);
  });
});
