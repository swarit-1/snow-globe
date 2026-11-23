/**
 * Formatting for chips and captions (PRD §5.3, §5.5): every time is printed in the place's zone with
 * its date and zone name, and units follow the system locale (metric, or US customary).
 *
 * Everything goes through Intl with an explicit locale and zone, never the machine's zone: PRD §7.4
 * says all times are local to the place. Formatters are cached, since building one costs far more
 * than using it.
 */

/** Unit system for distances (the core reads it from the macOS locale and sends it in `settings`). */
export type Units = 'metric' | 'us';

/** Locale and units for one rendering. */
export interface FormatOptions {
  readonly locale: string;
  readonly units: Units;
}

const dtfCache = new Map<string, Intl.DateTimeFormat>();
function dtf(locale: string, o: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const k = `${locale}|${JSON.stringify(o)}`;
  let f = dtfCache.get(k);
  if (f === undefined) {
    f = new Intl.DateTimeFormat(locale, o);
    dtfCache.set(k, f);
  }
  return f;
}

const nfCache = new Map<string, Intl.NumberFormat>();
function nf(locale: string, o: Intl.NumberFormatOptions): Intl.NumberFormat {
  const k = `${locale}|${JSON.stringify(o)}`;
  let f = nfCache.get(k);
  if (f === undefined) {
    f = new Intl.NumberFormat(locale, o);
    nfCache.set(k, f);
  }
  return f;
}

const ms = (t: string | number): number => (typeof t === 'number' ? t : Date.parse(t));

/** The calendar year of instant `t` in zone `tz`. */
export function yearIn(t: string | number, tz: string): number {
  return Number(dtf('en-US', { timeZone: tz, year: 'numeric' }).format(ms(t)));
}

/** True if instants `a` and `b` fall on the same calendar date in zone `tz`. */
export function sameLocalDate(a: string | number, b: string | number, tz: string): boolean {
  const f = dtf('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' });
  return f.format(ms(a)) === f.format(ms(b));
}

/**
 * Date and time with weekday, zone name and (when it differs from `referenceYear`) the year:
 * "Fri, Nov 20, 4:00 PM PST" in en-US, "Fri 20 Nov, 16:00 GMT-8" in en-GB (PRD §5.5).
 */
export function formatDateTime(t: string | number, tz: string, locale: string, o: { weekday?: boolean; referenceYear?: number } = {}): string {
  const showYear = o.referenceYear !== undefined && yearIn(t, tz) !== o.referenceYear;
  return dtf(locale, {
    timeZone: tz,
    ...(o.weekday === false ? {} : { weekday: 'short' }),
    month: 'short',
    day: 'numeric',
    ...(showYear ? { year: 'numeric' } : {}),
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(ms(t));
}

/** Time of day with the zone name only ("11:27 AM PST"), for a time whose date is printed beside it. */
export function formatTime(t: string | number, tz: string, locale: string): string {
  return dtf(locale, { timeZone: tz, hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }).format(ms(t));
}

/**
 * A forecast run's cycle, the way forecasters write it: the UTC hour and the UTC weekday ("18Z Thu").
 * The weekday follows the locale; the hour is always two digits and Z.
 */
export function formatRunCycle(cycleUtc: string, locale: string): string {
  const d = new Date(ms(cycleUtc));
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = d.getUTCMinutes();
  const wd = dtf(locale, { timeZone: 'UTC', weekday: 'short' }).format(d);
  return `${hh}${mm === 0 ? '' : String(mm).padStart(2, '0')}Z ${wd}`;
}

/** A duration in hours and minutes ("28 h 33 min", "45 min", "−20 min"), rounded to the minute. */
export function formatDuration(durationMs: number, locale: string): string {
  const negative = durationMs < 0;
  const total = Math.round(Math.abs(durationMs) / 60_000);
  const h = Math.floor(total / 60);
  const m = total % 60;
  const n = nf(locale, { maximumFractionDigits: 0 });
  const parts = h > 0 ? [`${n.format(h)} h`, ...(m > 0 ? [`${n.format(m)} min`] : [])] : [`${n.format(m)} min`];
  return `${negative && total > 0 ? '−' : ''}${parts.join(' ')}`;
}

/** How old a frame is ("8 min old", "2 h 5 min old", "just now"). */
export function formatAge(ageMs: number, locale: string): string {
  if (ageMs < 60_000) return 'just now';
  return `${formatDuration(ageMs, locale)} old`;
}

/** A fraction as a whole percentage ("80%"). */
export function formatPercent(fraction: number, locale: string): string {
  return nf(locale, { style: 'percent', maximumFractionDigits: 0 }).format(fraction);
}

/** An angle in degrees with one decimal ("3.1°"). */
export function formatDegrees(deg: number, locale: string): string {
  return nf(locale, { style: 'unit', unit: 'degree', unitDisplay: 'narrow', minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(deg);
}

/** A cloud ceiling, rounded to 100 m or 100 ft ("2,300 m", "7,500 ft"). */
export function formatCeiling(meters: number, o: FormatOptions): string {
  if (o.units === 'us') {
    const ft = Math.round(meters / 0.3048 / 100) * 100;
    return nf(o.locale, { style: 'unit', unit: 'foot', unitDisplay: 'short', maximumFractionDigits: 0 }).format(ft);
  }
  const m = Math.round(meters / 100) * 100;
  return nf(o.locale, { style: 'unit', unit: 'meter', unitDisplay: 'short', maximumFractionDigits: 0 }).format(m);
}

/** A visibility: one decimal below 10 km (or 10 mi), whole numbers above ("12 km", "7.5 mi", "800 m"). */
export function formatVisibility(meters: number, o: FormatOptions): string {
  if (o.units === 'us') {
    const mi = meters / 1609.344;
    if (mi < 0.25) {
      const ft = Math.round(meters / 0.3048 / 100) * 100;
      return nf(o.locale, { style: 'unit', unit: 'foot', unitDisplay: 'short', maximumFractionDigits: 0 }).format(ft);
    }
    return nf(o.locale, { style: 'unit', unit: 'mile', unitDisplay: 'short', maximumFractionDigits: mi < 10 ? 1 : 0 }).format(mi);
  }
  if (meters < 1000) {
    const m = Math.round(meters / 100) * 100;
    return nf(o.locale, { style: 'unit', unit: 'meter', unitDisplay: 'short', maximumFractionDigits: 0 }).format(m);
  }
  const km = meters / 1000;
  return nf(o.locale, { style: 'unit', unit: 'kilometer', unitDisplay: 'short', maximumFractionDigits: km < 10 ? 1 : 0 }).format(km);
}

/**
 * A precipitation rate ("1.2 mm/h", "0.05 in/h"). A rate above zero never prints as zero: below the
 * last digit shown it reads "< 0.01 in/h" or "< 0.1 mm/h", so the caption never says rain at a rate of 0.
 */
export function formatPrecipRate(mmPerHour: number, o: FormatOptions): string {
  if (o.units === 'us') {
    const inch = mmPerHour / 25.4;
    const f = nf(o.locale, { maximumFractionDigits: 2, minimumFractionDigits: 0 });
    return inch > 0 && inch < 0.005 ? `< ${f.format(0.01)} in/h` : `${f.format(inch)} in/h`;
  }
  const f = nf(o.locale, { style: 'unit', unit: 'millimeter-per-hour', unitDisplay: 'short', maximumFractionDigits: 1 });
  return mmPerHour > 0 && mmPerHour < 0.05 ? `< ${f.format(0.1)}` : f.format(mmPerHour);
}

/** Joins words as a list in the locale ("Sun position and forecast class"). */
export function formatList(items: readonly string[], locale: string): string {
  return new Intl.ListFormat(locale, { style: 'long', type: 'conjunction' }).format(items);
}
