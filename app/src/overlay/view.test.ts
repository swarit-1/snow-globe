import { describe, expect, it } from 'vitest';
import { simulatedCaption } from './caption.ts';
import { coreQuery, DISPLAY, simulatedLabel } from './samples.ts';
import { INITIAL_MODEL, type OverlayModel } from './state.ts';
import { announcement, clipText, errorText, glue, keyCommand, noticeText, repeats, TRANSCRIPT_SHOWN } from './view.ts';

const none = { shift: false, meta: false, ctrl: false, alt: false };

describe('view helpers (pure parts of view.ts)', () => {
  it('maps the PRD §5.4 keys, and leaves Command, Control and Option keys to the system', () => {
    expect(keyCommand('Escape', none)).toEqual({ type: 'dismiss' });
    expect(keyCommand('ArrowLeft', none)).toEqual({ type: 'scrub', minutes: -60 });
    expect(keyCommand('ArrowRight', { ...none, shift: true })).toEqual({ type: 'scrub', minutes: 10 });
    expect(keyCommand('M', { ...none, shift: true })).toEqual({ type: 'flip_meridiem' });
    expect(keyCommand('c', none)).toEqual({ type: 'copy' });
    expect(keyCommand('c', { ...none, meta: true })).toBeNull();
    expect(keyCommand('ArrowLeft', { ...none, alt: true })).toBeNull();
    expect(keyCommand('Tab', none)).toBeNull(); // Tab keeps its VoiceOver and focus order
    expect(keyCommand('l', none)).toBeNull(); // the live-beside view is not built (PLAN v1 growth)
  });

  it('keeps numbers with units and times with zones on one line, changing no words', () => {
    const s = 'SIMULATED · Fri, Nov 20, 4:00 PM PST · ceiling 2,300 m · lead 28 h 33 min · 16:00 GMT-8 · cloud low 80%';
    const g = glue(s);
    expect(g.replace(/ /g, ' ')).toBe(s);
    for (const kept of ['2,300 m', '28 h', '33 min', 'PM PST', '16:00 GMT-8']) expect(g).toContain(kept);
    expect(g).toContain('· cloud low');
  });

  it('acts once on a held Esc, M or C, and lets the scrub arrows repeat', () => {
    expect(repeats({ type: 'scrub', minutes: 60 })).toBe(true);
    for (const c of [{ type: 'dismiss' }, { type: 'flip_meridiem' }, { type: 'copy' }] as const) expect(repeats(c)).toBe(false);
  });

  it('clips a long transcript for display, keeping its start and end', () => {
    expect(clipText('rainier at 4')).toBe('rainier at 4');
    const long = `${'is rainier out '.repeat(200)}tomorrow at 4`;
    const shown = clipText(long);
    expect([...shown].length).toBeLessThanOrEqual(TRANSCRIPT_SHOWN + 1);
    expect(shown.startsWith('is rainier out')).toBe(true);
    expect(shown.endsWith('tomorrow at 4')).toBe(true);
    expect(shown).toContain(' … ');
  });

  it('says only what is true after an error: the fatal text never claims a frame stays, speech text follows the destination', () => {
    const base = { ...INITIAL_MODEL, phase: 'error' as const, gen: 1 };
    expect(errorText('render_failed', base).join(' ')).not.toMatch(/stays on screen/);
    const cloud = { ...base, destinations: [{ provider: 'Deepgram', carries: ['audio' as const] }] };
    expect(errorText('speech_unavailable', base)[1]).toBe('On-device speech could not start. Nothing was sent anywhere.');
    expect(errorText('speech_unavailable', cloud)[1]).toBe('Speech could not start. Cloud speech is on, so audio may have reached Deepgram.');
    // A non-fatal render failure names what stays only when it does.
    const showing = { ...INITIAL_MODEL, phase: 'showing' as const, gen: 1 };
    expect(noticeText('render_failed', { ...showing, shownTier: 0 })).toBe('The simulated frame could not be drawn. The real photo stays on screen.');
    expect(noticeText('render_failed', { ...showing, shownTier: null })).toBe('The simulated frame could not be drawn.');
    expect(noticeText('render_failed', { ...showing, shownTier: 1 })).toBe('A refined frame could not be drawn. The simulation stays on screen.');
  });

  it('keeps "< 0.01" with its number', () => {
    expect(glue('rain < 0.01 in/h')).toBe('rain < 0.01 in/h');
  });

  it('announces each state for VoiceOver (PRD §5.5)', () => {
    const caption = simulatedCaption(simulatedLabel(), { display: DISPLAY.rainier!, query: coreQuery('is rainier out tomorrow at 4'), fmt: { locale: 'en-US', units: 'metric' }, online: true });
    const at = (m: Partial<OverlayModel>): string => announcement({ ...INITIAL_MODEL, ...m });
    expect(at({ phase: 'idle' })).toBe('');
    expect(at({ phase: 'listening' })).toBe('Listening.');
    expect(at({ phase: 'resolving' })).toBe('Looking it up.');
    expect(at({ phase: 'showing', caption }).replace(/ /g, ' ')).toBe('Simulated. Fri, Nov 20, 4:00 PM PST. cloud low 80%. ceiling 2,300 m. visibility 12 km.');
    expect(at({ phase: 'no_match', noMatch: { transcript: 'golden gate', closest: [{ place_id: 'yosemite', place: 'Yosemite', score: 0.4 }] } })).toBe('No match for golden gate. Closest known places: Yosemite.');
    expect(at({ phase: 'error', error: { code: 'microphone_denied', fatal: true } })).toBe('Microphone access is off.');
  });
});
