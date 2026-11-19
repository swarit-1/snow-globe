/**
 * Strict ISO 8601 UTC timestamps for SceneSpec (PRD §9.2 valid time, §9.5 forecast run).
 *
 * Date.parse is lenient: it accepts "2026-02-30T00:00:00Z" and silently returns 2 March, and it accepts
 * "T24:00:00Z". A valid time that shifts silently would move the clouds (advection) and the provenance,
 * so every timestamp here is parsed field by field and must round-trip through the calendar.
 */

/** Valid time: YYYY-MM-DDTHH:MM[:SS[.fffffffff]]Z. */
export const ISO_UTC_PATTERN = '^\\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\\d|3[01])T([01]\\d|2[0-3]):[0-5]\\d(:[0-5]\\d(\\.\\d{1,9})?)?Z$';

/** Forecast run initialisation time, canonical form only: YYYY-MM-DDTHH:MM:SSZ (the string deriveSeed hashes). */
export const ISO_UTC_SECONDS_PATTERN = '^\\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\\d|3[01])T([01]\\d|2[0-3]):[0-5]\\d:[0-5]\\dZ$';

const PARTS = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?Z$/;
const SECONDS_FORM = new RegExp(ISO_UTC_SECONDS_PATTERN);

/**
 * Seconds since the Unix epoch for a strict ISO 8601 UTC timestamp, or null when the string is not one
 * or names a date or time that does not exist (30 February, hour 24, second 60). Fractional seconds are
 * kept (float64).
 */
export function parseUtcSeconds(s: string): number | null {
  const m = PARTS.exec(s);
  if (!m) return null;
  const year = Number(m[1]);
  const month = Number(m[2]);
  const day = Number(m[3]);
  const hour = Number(m[4]);
  const minute = Number(m[5]);
  const second = m[6] === undefined ? 0 : Number(m[6]);
  if (month < 1 || month > 12 || day < 1 || hour > 23 || minute > 59 || second > 59) return null;
  const ms = Date.UTC(year, month - 1, day, hour, minute, second);
  const back = new Date(ms);
  // Date.UTC rolls 30 February over to 2 March and maps years 0-99 to 1900-1999: reject both.
  if (back.getUTCFullYear() !== year || back.getUTCMonth() !== month - 1 || back.getUTCDate() !== day) return null;
  const frac = m[7] === undefined ? 0 : Number(`0.${m[7]}`);
  return ms / 1000 + frac;
}

/** True when s is a real instant in the canonical YYYY-MM-DDTHH:MM:SSZ form. */
export function isCanonicalUtcSeconds(s: string): boolean {
  return SECONDS_FORM.test(s) && parseUtcSeconds(s) !== null;
}
