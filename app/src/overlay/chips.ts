/**
 * The chips (PRD §5.1, §7.4): place and viewpoint; the valid time with zone and date; an AM/PM flip
 * when the hour was ambiguous; the other reading of a close call between places; daylight-saving and
 * horizon flags; offline; and the destination chip, which says whether audio or text leaves the
 * machine (PRD §15.1: `on-device`, or `→ <provider>`).
 */
import { formatDateTime, formatTime, sameLocalDate, type FormatOptions } from './format.ts';
import type { Destination, Display, QueryView } from './protocol.ts';

/** Which chip. */
export type ChipId = 'place' | 'time' | 'meridiem' | 'place_alternative' | 'dst' | 'horizon' | 'offline' | 'destination';

/** One chip. `text` is what shows; `label` is what VoiceOver reads. */
export interface Chip {
  readonly id: ChipId;
  readonly text: string;
  readonly label: string;
  /** `highlight`: the date rolled forward (PRD §7.4); `warning`: data leaves the machine, or a setting is contradicted. */
  readonly emphasis: 'none' | 'highlight' | 'warning';
  /** The key or click that acts on it (PRD §5.4), if any. */
  readonly action: 'flip_meridiem' | null;
  /** The key that triggers the action, for its label. */
  readonly key: string | null;
}

/** Where data goes, as the overlay knows it (PRD §15.1). */
export interface DestinationState {
  readonly destinations: Destination['destinations'];
  readonly onDeviceOnly: boolean;
}

const EVENT_WORDS: Readonly<Record<string, string>> = {
  sunrise: 'Sunrise',
  sunset: 'Sunset',
  dawn: 'Dawn',
  dusk: 'Dusk',
  morning_golden_hour: 'Morning golden hour',
  evening_golden_hour: 'Golden hour',
  morning_blue_hour: 'Morning blue hour',
  evening_blue_hour: 'Blue hour',
};

const chip = (id: ChipId, text: string, label: string, emphasis: Chip['emphasis'] = 'none', action: Chip['action'] = null, key: string | null = null): Chip => ({
  id,
  text,
  label,
  emphasis,
  action,
  key,
});

/**
 * The destination chip. With no destination it reads `on-device` (`on-device only` when the setting
 * is on). Otherwise `→ Deepgram`, or `→ Deepgram, fal`. If the on-device-only setting is on and the
 * core still names a destination, the chip shows the destination anyway, as a warning: the chip
 * must say what really leaves the machine.
 */
export function destinationChip(d: DestinationState): Chip {
  if (d.destinations.length === 0) {
    return d.onDeviceOnly
      ? chip('destination', 'on-device only', 'On-device only: audio and text stay on this Mac, and every cloud path is off.')
      : chip('destination', 'on-device', 'On-device: audio and text stay on this Mac.');
  }
  const names = d.destinations.map((x) => x.provider);
  const what = [...new Set(d.destinations.flatMap((x) => x.carries))].join(' and ');
  const conflict = d.onDeviceOnly ? ' This contradicts the on-device-only setting.' : '';
  return chip('destination', `→ ${names.join(', ')}`, `Sending ${what} to ${names.join(' and ')}.${conflict}`, 'warning');
}

/** The chips for a parsed query, in display order; the destination chip is always last. */
export function chipsFor(q: QueryView, display: Display, ctx: { readonly fmt: FormatOptions; readonly destination: DestinationState; readonly online: boolean }): Chip[] {
  const { locale } = ctx.fmt;
  const out: Chip[] = [];
  const context = q.place_source === 'context';
  const placeText = `${display.place_name} · ${display.viewpoint_name}${context ? ' (last place)' : ''}`;
  out.push(chip('place', placeText, `${display.place_name}, ${display.viewpoint_name}${context ? ', the last place asked about' : ''}.`));

  const when = formatDateTime(q.valid_time, q.tz, locale);
  const event = q.time_source.event ? EVENT_WORDS[q.time_source.event] : undefined;
  const interval = q.time_source.interval;
  const span = interval ? ` (${formatTime(interval.start, q.tz, locale)} to ${formatTime(interval.end, q.tz, locale)})` : '';
  const rolled = q.flags.includes('rolled_to_tomorrow') || q.flags.includes('rolled_to_next_week') || q.flags.includes('rolled_to_next_hour');
  const timeText = event ? `${event} · ${when}` : when;
  const rolledWhy = q.flags.includes('rolled_to_next_week')
    ? ' That time had passed today, so it is next week.'
    : q.flags.includes('rolled_to_next_hour')
      ? ' Tonight had passed, so it is the next whole hour.'
      : q.flags.includes('rolled_to_tomorrow')
        ? ' That time had passed today, so it is tomorrow.'
        : '';
  out.push(chip('time', timeText, `${event ? `${event}, ` : ''}${when}${span}.${rolledWhy}`, rolled ? 'highlight' : 'none'));

  const meridiem = q.ambiguities.find((a) => a.field === 'meridiem');
  if (meridiem) {
    const chosen = meridiem.chosen.toUpperCase();
    // The other reading can fall on another day (a bare "9" read as 9 PM tonight offers 9 AM
    // tomorrow), so its date is printed unless it is the valid time's own date (PRD §5.5).
    const other = sameLocalDate(meridiem.alternative, q.valid_time, q.tz) ? formatTime(meridiem.alternative, q.tz, locale) : formatDateTime(meridiem.alternative, q.tz, locale);
    out.push(chip('meridiem', `${chosen} ⇄ ${chosen === 'AM' ? 'PM' : 'AM'}`, `Read as ${chosen}. Press M for ${other}.`, 'none', 'flip_meridiem', 'M'));
  }
  const place = q.ambiguities.find((a) => a.field === 'place' || a.field === 'viewpoint');
  if (place && display.alternative_place) {
    out.push(chip('place_alternative', `or ${display.alternative_place}?`, `A close call: this could also be ${display.alternative_place}.`));
  }
  if (q.flags.includes('dst_gap')) out.push(chip('dst', 'DST · moved forward', 'That wall time does not exist (daylight saving began), so it moved forward.', 'highlight'));
  if (q.flags.includes('dst_fold')) out.push(chip('dst', 'DST · earlier offset', 'That wall time happens twice (daylight saving ended); the earlier one is shown.', 'highlight'));
  if (q.horizon === 'gfs') out.push(chip('horizon', 'GFS · beyond 48 h', 'More than 48 hours ahead: a pinned GFS run, not HRRR.'));
  if (!ctx.online) out.push(chip('offline', 'offline', 'Offline: showing cached frames.'));
  out.push(destinationChip(ctx.destination));
  return out;
}
