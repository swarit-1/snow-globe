import { describe, expect, it } from 'vitest';
import { composeCaption, liveCaption, photoCaption, refinedCaption, simulatedCaption, type CaptionContext } from './caption.ts';
import { formatCeiling, formatDateTime, formatDuration, formatPrecipRate, formatRunCycle, formatVisibility } from './format.ts';
import { coreQuery, DISPLAY, NPS_CREDIT, photoLabel, refinedLabel, simulatedLabel } from './samples.ts';
import { Rng } from '../harness/rng.ts';

/** ICU puts a narrow no-break space before AM/PM; compare with plain spaces. */
const plain = (s: string): string => s.replace(/[  ]/g, ' ');

const rainier = coreQuery('is rainier out tomorrow at 4');
const reyes = coreQuery('point reyes at sunset friday');
const ctx = (over: Partial<CaptionContext> = {}): CaptionContext => ({
  display: DISPLAY.rainier!,
  query: rainier,
  fmt: { locale: 'en-US', units: 'metric' },
  online: true,
  ...over,
});

describe('formats (PRD §5.5: zone and date always, units by locale)', () => {
  it('prints the place zone and the date, whatever the machine zone', () => {
    expect(plain(formatDateTime('2026-11-21T00:00:00Z', 'America/Los_Angeles', 'en-US'))).toBe('Fri, Nov 20, 4:00 PM PST');
    expect(plain(formatDateTime('2026-11-21T00:00:00Z', 'America/Los_Angeles', 'en-GB'))).toBe('Fri 20 Nov, 16:00 GMT-8');
    // Phoenix keeps MST through the summer; Los Angeles switches to PDT.
    expect(plain(formatDateTime('2026-07-01T19:00:00Z', 'America/Phoenix', 'en-US'))).toBe('Wed, Jul 1, 12:00 PM MST');
    expect(plain(formatDateTime('2026-07-01T19:00:00Z', 'America/Los_Angeles', 'en-US'))).toBe('Wed, Jul 1, 12:00 PM PDT');
  });

  it('prints the year only when it is not the reference year', () => {
    expect(plain(formatDateTime('2025-11-21T16:58:00-08:00', 'America/Los_Angeles', 'en-US', { weekday: false, referenceYear: 2026 }))).toBe('Nov 21, 2025, 4:58 PM PST');
    expect(plain(formatDateTime('2026-11-21T16:58:00-08:00', 'America/Los_Angeles', 'en-US', { weekday: false, referenceYear: 2026 }))).toBe('Nov 21, 4:58 PM PST');
  });

  it('writes runs, durations and units', () => {
    expect(formatRunCycle('2026-11-19T18:00:00Z', 'en-US')).toBe('18Z Thu');
    expect(formatRunCycle('2026-11-19T06:30:00Z', 'en-US')).toBe('0630Z Thu');
    expect(formatDuration((28 * 60 + 33) * 60_000, 'en-US')).toBe('28 h 33 min');
    expect(formatDuration(45 * 60_000, 'en-US')).toBe('45 min');
    expect(formatDuration(-20 * 60_000, 'en-US')).toBe('−20 min');
    expect(formatDuration(5 * 3_600_000, 'en-US')).toBe('5 h');
    expect(formatCeiling(2300, { locale: 'en-US', units: 'metric' })).toBe('2,300 m');
    expect(formatCeiling(2300, { locale: 'en-US', units: 'us' })).toBe('7,500 ft');
    expect(formatVisibility(12_000, { locale: 'en-US', units: 'metric' })).toBe('12 km');
    expect(formatVisibility(12_000, { locale: 'en-US', units: 'us' })).toBe('7.5 mi');
    expect(formatVisibility(800, { locale: 'en-US', units: 'metric' })).toBe('800 m');
    expect(formatVisibility(3_400, { locale: 'de-DE', units: 'metric' })).toBe('3,4 km');
  });
});

describe('caption composer (PRD §5.3)', () => {
  it('reproduces the PRD Tier 1 example, with the date the §5.5 rule adds', () => {
    const c = simulatedCaption(simulatedLabel(), ctx());
    expect(plain(c.line1)).toBe('SIMULATED · Fri, Nov 20, 4:00 PM PST · cloud low 80% · ceiling 2,300 m · visibility 12 km');
    expect(plain(c.line2)).toBe('HRRR 18Z Thu, issued Thu, Nov 19, 11:27 AM PST (lead 28 h 33 min) · Sun 3.1° · Moon 41% lit, below horizon');
    expect(c.notice).toBeNull();
    expect(c.credits.map((x) => x.text)).toEqual(['Forecast data: NOAA HRRR via NOAA Open Data Dissemination', NPS_CREDIT.text]);
  });

  it('follows the locale and its units', () => {
    const gb = simulatedCaption(simulatedLabel(), ctx({ fmt: { locale: 'en-GB', units: 'metric' } }));
    expect(plain(gb.line1)).toBe('SIMULATED · Fri 20 Nov, 16:00 GMT-8 · cloud low 80% · ceiling 2,300 m · visibility 12 km');
    const us = simulatedCaption(simulatedLabel(), ctx({ fmt: { locale: 'en-US', units: 'us' } }));
    expect(plain(us.line1)).toBe('SIMULATED · Fri, Nov 20, 4:00 PM PST · cloud low 80% · ceiling 7,500 ft · visibility 7.5 mi');
  });

  it('says a Tier 0 frame is a real photo, when it was taken (with the year when it differs) and what it matched on (PRD §9.1)', () => {
    const c = photoCaption(photoLabel(), ctx({ display: DISPLAY['point-reyes']!, query: reyes }));
    expect(plain(c.line1)).toBe('REAL PHOTO, CLOSEST MATCH · taken Nov 21, 2025, 4:58 PM PST · matched on Sun position and forecast class');
    expect(plain(c.line2)).toBe('Point Reyes, Point Reyes Beach camera · closest to Fri, Nov 20, 4:56 PM PST · from the NPS archive');
    const sunOnly = photoCaption(photoLabel({ matched_on: ['sun_position'], source: 'capture' }), ctx({ display: DISPLAY['point-reyes']!, query: reyes }));
    expect(plain(sunOnly.line1)).toContain('matched on Sun position');
    expect(sunOnly.line2).toContain('from our captures');
  });

  it('labels Tier 2 as simulated with AI texture and keeps the sky claim (PRD §6.6 C9)', () => {
    const c = refinedCaption(refinedLabel(), ctx());
    expect(plain(c.line1)).toBe('SIMULATED + AI TEXTURE · Fri, Nov 20, 4:00 PM PST · cloud low 80% · ceiling 2,300 m · visibility 12 km · sky and haze unchanged from the simulation');
    expect(c.line2.endsWith('texture Z-Image-Turbo via fal')).toBe(true);
  });

  it('writes the live caption of the PRD example, with its date', () => {
    const c = liveCaption(
      { kind: 'live', taken_at: '2026-11-22T15:52:00-08:00', tz: 'America/Los_Angeles', camera_name: 'Paradise camera', credits: [{ text: 'Courtesy National Park Service' }] },
      ctx({ nowMs: Date.parse('2026-11-22T16:00:00-08:00') }),
    );
    expect(plain(c.line1)).toBe('LIVE · Mount Rainier, Paradise camera · Sun, Nov 22, 3:52 PM PST (8 min old) · Courtesy National Park Service');
  });

  it('says where an offline forecast is from (PRD §5.2 Offline)', () => {
    const c = simulatedCaption(simulatedLabel(), ctx({ online: false }));
    expect(plain(c.notice!)).toBe('Offline: forecast from HRRR 18Z Thu, issued Thu, Nov 19, 11:27 AM PST');
    expect(photoCaption(photoLabel(), ctx({ online: false })).notice).toBe('Offline: cached photos only');
  });

  it('names the GFS range and a pinned run, no ceiling, and precipitation', () => {
    const c = simulatedCaption(
      simulatedLabel({
        run: { model: 'gfs', cycle_utc: '2026-11-19T12:00:00Z', available_at: '2026-11-19T16:10:00Z', pinned: true },
        forecast: { cloud_low: 0.1, cloud_mid: 0.6, cloud_high: 0.6, ceiling_m: null, visibility_m: null, precip_rate_mm_h: 1.24, precip_type: 'snow' },
      }),
      ctx(),
    );
    expect(plain(c.line1)).toBe('SIMULATED · Fri, Nov 20, 4:00 PM PST · GFS forecast, beyond the 48 h HRRR range · cloud mid 60% · no ceiling · snow 1.2 mm/h');
    expect(plain(c.line2)).toContain('GFS 12Z Thu (pinned run), issued Thu, Nov 19, 8:10 AM PST');
  });

  it('never prints a rate above zero as zero (a trace of rain is "< 0.01 in/h", not "0 in/h")', () => {
    expect(formatPrecipRate(0.1, { locale: 'en-US', units: 'us' })).toBe('< 0.01 in/h');
    expect(formatPrecipRate(0.04, { locale: 'en-US', units: 'metric' })).toBe('< 0.1 mm/h');
    expect(formatPrecipRate(0.04, { locale: 'de-DE', units: 'metric' })).toBe('< 0,1 mm/h');
    expect(formatPrecipRate(0.127, { locale: 'en-US', units: 'us' })).toBe('0.01 in/h');
    expect(formatPrecipRate(0.05, { locale: 'en-US', units: 'metric' })).toBe('0.1 mm/h');
    expect(formatPrecipRate(1.24, { locale: 'en-US', units: 'metric' })).toBe('1.2 mm/h');
    const c = simulatedCaption(simulatedLabel({ forecast: { cloud_low: 0.9, cloud_mid: 0, cloud_high: 0, ceiling_m: 400, visibility_m: 5000, precip_rate_mm_h: 0.1, precip_type: 'rain' } }), ctx({ fmt: { locale: 'en-US', units: 'us' } }));
    expect(plain(c.line1).endsWith('· rain < 0.01 in/h')).toBe(true);
    // Every rate above zero, in both unit systems, prints with a digit other than a lone zero.
    for (let mm = 0.001; mm < 3; mm *= 1.37) {
      for (const units of ['us', 'metric'] as const) expect(formatPrecipRate(mm, { locale: 'en-US', units }), `${mm} ${units}`).not.toMatch(/^0 /);
    }
  });

  it('never calls a simulated frame a photo, and always prints a date and a zone (2,000 random labels)', () => {
    const rng = new Rng(0x5eedn);
    const zones = ['America/Los_Angeles', 'America/Phoenix', 'America/New_York', 'America/Chicago', 'America/Denver'];
    for (let i = 0; i < 2000; i++) {
      const tz = zones[rng.range(0, zones.length)]!;
      const valid = new Date(Date.UTC(2026, rng.range(0, 12), rng.range(1, 29), rng.range(0, 24), rng.range(0, 60))).toISOString().replace('.000Z', 'Z');
      const offsetValid = valid.replace('Z', '+00:00');
      const base = simulatedLabel({
        valid_time: offsetValid,
        tz,
        forecast: { cloud_low: rng.float(), cloud_mid: rng.float(), cloud_high: rng.float(), ceiling_m: rng.chance(0.3) ? null : rng.range(0, 9000), visibility_m: rng.chance(0.2) ? null : rng.range(50, 80_000), precip_rate_mm_h: rng.chance(0.7) ? null : rng.uniform(0, 20), precip_type: 'rain' },
        sun_altitude_deg: rng.uniform(-90, 90),
        moon: { illuminated: rng.float(), altitude_deg: rng.uniform(-90, 90) },
      });
      const locale = rng.chance(0.5) ? 'en-US' : 'en-GB';
      const units = rng.chance(0.5) ? 'us' : 'metric';
      const c1 = composeCaption(base, ctx({ fmt: { locale, units }, online: rng.chance(0.8) }));
      const { kind: _k, ...rest } = base;
      const c2 = composeCaption({ kind: 'refined', ...rest, refine: { model: 'm', provider: 'p' } }, ctx({ fmt: { locale, units } }));
      for (const c of [c1, c2]) {
        const all = [c.lead, c.line1, c.line2, c.notice ?? '', c.spoken].join(' ');
        expect(all).not.toMatch(/photo/i);
        expect(c.line1.startsWith('SIMULATED')).toBe(true);
        const when = formatDateTime(offsetValid, tz, locale);
        expect(c.line1).toContain(when);
        expect(when).toMatch(/(P|M|C|E)[SD]T|GMT[-+]\d/);
      }
      const p = composeCaption(photoLabel({ taken_at: offsetValid, tz }), ctx({ fmt: { locale, units } }));
      expect(p.line1.startsWith('REAL PHOTO, CLOSEST MATCH · taken ')).toBe(true);
    }
  });

  it('gives VoiceOver whole sentences without the visual separators', () => {
    const c = simulatedCaption(simulatedLabel(), ctx());
    expect(c.spoken).not.toContain('·');
    expect(plain(c.spoken).startsWith('Simulated. Fri, Nov 20, 4:00 PM PST. cloud low 80%.')).toBe(true);
    expect(refinedCaption(refinedLabel(), ctx()).spoken.startsWith('Simulated + AI texture.')).toBe(true);
  });
});
