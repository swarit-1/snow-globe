import { describe, expect, it } from 'vitest';
import { chipsFor, destinationChip, type Chip } from './chips.ts';
import { coreQuery, DISPLAY } from './samples.ts';

const plain = (s: string): string => s.replace(/[  ]/g, ' ');
const fmt = { locale: 'en-US', units: 'metric' as const };
const local = { destinations: [], onDeviceOnly: false };
const chips = (utterance: string, over: Partial<Parameters<typeof chipsFor>[2]> = {}): Chip[] => {
  const q = coreQuery(utterance);
  return chipsFor(q, DISPLAY[q.place_id]!, { fmt, destination: local, online: true, ...over });
};
const byId = (cs: Chip[], id: Chip['id']): Chip | undefined => cs.find((c) => c.id === id);

describe('chips (PRD §5.1, §7.4)', () => {
  it('shows place and viewpoint, the valid time with date and zone, and an AM/PM flip on M', () => {
    const cs = chips('is rainier out tomorrow at 4');
    expect(byId(cs, 'place')!.text).toBe('Mount Rainier · Paradise camera');
    expect(plain(byId(cs, 'time')!.text)).toBe('Fri, Nov 20, 4:00 PM PST');
    const m = byId(cs, 'meridiem')!;
    expect(m.text).toBe('PM ⇄ AM');
    expect(m.action).toBe('flip_meridiem');
    expect(m.key).toBe('M');
    expect(plain(m.label)).toBe('Read as PM. Press M for 4:00 AM PST.');
    expect(cs[cs.length - 1]!.id).toBe('destination');
  });

  it('prints the other reading’s date when it falls on another day (PRD §5.5: always the date)', () => {
    // "show me rainier at 9" late in the evening: 9 PM tonight, or 9 AM tomorrow (resolve.rs bare hour).
    const q = coreQuery('show me rainier at 9');
    const late = { ...q, valid_time: '2026-11-19T21:00:00-08:00', valid_time_utc: '2026-11-20T05:00:00Z', ambiguities: [{ field: 'meridiem' as const, chosen: 'pm' as const, alternative: '2026-11-20T09:00:00-08:00' }] };
    const cs = chipsFor(late, DISPLAY.rainier!, { fmt, destination: local, online: true });
    expect(plain(byId(cs, 'time')!.text)).toBe('Thu, Nov 19, 9:00 PM PST');
    expect(plain(byId(cs, 'meridiem')!.label)).toBe('Read as PM. Press M for Fri, Nov 20, 9:00 AM PST.');
    // The same day keeps the short form.
    expect(plain(byId(chips('show me rainier at 9'), 'meridiem')!.label)).toBe('Read as AM. Press M for 9:00 PM PST.');
  });

  it('names the last place on the chip when the place came from context', () => {
    expect(byId(chips('what about midnight'), 'place')!.text).toBe('Mount Rainier · Paradise camera (last place)');
  });

  it('highlights the date chip when the time rolled to tomorrow', () => {
    const t = byId(chips('rainier at 7 am'), 'time')!;
    expect(t.emphasis).toBe('highlight');
    expect(t.label).toContain('so it is tomorrow');
    expect(byId(chips('rainier at 9 pm'), 'time')!.emphasis).toBe('none');
  });

  it('flags daylight-saving folds and the GFS horizon', () => {
    expect(byId(chips('rainier sunday at 1:30 am'), 'dst')!.text).toBe('DST · earlier offset');
    expect(byId(chips('rainier next wednesday at noon'), 'horizon')!.text).toBe('GFS · beyond 48 h');
    expect(byId(chips('is rainier out tomorrow at 4'), 'horizon')).toBeUndefined();
  });

  it('names solar events, and the interval of golden hour', () => {
    const t = byId(chips('grand canyon golden hour'), 'time')!;
    expect(plain(t.text)).toBe('Golden hour · Thu, Nov 19, 5:02 PM MST');
    expect(plain(t.label)).toContain('(4:40 PM MST to 5:35 PM MST)');
    expect(plain(byId(chips('point reyes at sunset friday'), 'time')!.text)).toBe('Sunset · Fri, Nov 20, 4:56 PM PST');
  });

  it('shows offline when the network is down', () => {
    expect(byId(chips('is rainier out tomorrow at 4', { online: false }), 'offline')).toBeDefined();
  });
});

describe('destination chip (PRD §15.1)', () => {
  it('reads on-device, or on-device only when the setting is on', () => {
    expect(destinationChip({ destinations: [], onDeviceOnly: false }).text).toBe('on-device');
    const only = destinationChip({ destinations: [], onDeviceOnly: true });
    expect(only.text).toBe('on-device only');
    expect(only.label).toContain('every cloud path is off');
  });

  it('names every provider that receives audio, text or images, as a warning', () => {
    const c = destinationChip({ destinations: [{ provider: 'Deepgram', carries: ['audio'] }, { provider: 'fal', carries: ['image', 'text'] }], onDeviceOnly: false });
    expect(c.text).toBe('→ Deepgram, fal');
    expect(c.emphasis).toBe('warning');
    expect(c.label).toBe('Sending audio and image and text to Deepgram and fal.');
  });

  it('still shows a destination that contradicts on-device only, and says so', () => {
    const c = destinationChip({ destinations: [{ provider: 'Deepgram', carries: ['audio'] }], onDeviceOnly: true });
    expect(c.text).toBe('→ Deepgram');
    expect(c.label).toContain('contradicts the on-device-only setting');
  });
});
