/**
 * The caption composer (PRD §5.3, the honesty layer): every frame gets a label line saying what it is,
 * and a second, folded line with the details.
 *
 * - Tier 0: "REAL PHOTO, CLOSEST MATCH · taken <date, time, zone> · matched on <what>" (PRD §9.1: it
 *   says this is a real photo, when it was taken, and what it was matched on).
 * - Tier 1: "SIMULATED · <valid time> · cloud <layer> <cover> · ceiling <height> · visibility <distance>",
 *   then the run, when it was issued (its availability time, not its nominal start) and the lead,
 *   the Sun and the Moon.
 * - Tier 2: "SIMULATED + AI TEXTURE · … · sky and haze unchanged from the simulation".
 * - Live (the live-beside view, PRD §3.2): "LIVE · <place>, <camera> · <time> (<age>) · <credit>".
 *
 * Copy rules (PRD §5.5): a simulated frame is never called a photo; every time carries its date and
 * zone; units follow the locale. Where the §5.3 examples drop the date ("issued 11:27 AM PST",
 * "3:52 PM PST"), the composer prints it, because §5.5 says always.
 */
import type { Credit, Display, ForecastInfo, PhotoLabel, QueryView, RefinedLabel, RunInfo, SimulatedLabel } from './protocol.ts';
import {
  formatAge,
  formatCeiling,
  formatDateTime,
  formatDegrees,
  formatDuration,
  formatList,
  formatPercent,
  formatPrecipRate,
  formatRunCycle,
  formatVisibility,
  yearIn,
  type FormatOptions,
} from './format.ts';

/** Separator between caption segments. */
export const SEP = ' · ';

/** What kind of frame a caption labels. */
export type CaptionKind = 'photo' | 'simulated' | 'refined' | 'live';

/** A composed caption. */
export interface Caption {
  readonly kind: CaptionKind;
  /** The label words that open line 1 ("REAL PHOTO, CLOSEST MATCH", "SIMULATED", ...). */
  readonly lead: string;
  /** Line 1, always visible. */
  readonly line1: string;
  /** Line 2, folded until hover or focus (PRD §5.3). */
  readonly line2: string;
  /** A visible notice above line 1 (offline), or null. */
  readonly notice: string | null;
  /** Credit lines from the licence ledger (PRD §15.4). */
  readonly credits: readonly Credit[];
  /** The whole caption as sentences, for VoiceOver (PRD §5.5: the caption is real text). */
  readonly spoken: string;
}

/** A live camera frame's label (the live-beside view; not yet sent by the core, see README). */
export interface LiveLabel {
  readonly kind: 'live';
  readonly taken_at: string;
  readonly tz: string;
  readonly camera_name: string;
  readonly credits: readonly Credit[];
}

/** What the caption needs besides the frame's own label. */
export interface CaptionContext {
  readonly display: Display;
  /** The query the frame answers (for Tier 0's "closest to" and the year rule). */
  readonly query: QueryView | null;
  readonly fmt: FormatOptions;
  /** False when the network is down: cached tiers say where their forecast is from (PRD §5.2 Offline). */
  readonly online: boolean;
  /** Wall-clock now in ms, for a live frame's age. */
  readonly nowMs?: number;
}

const MATCH_WORDS: Readonly<Record<PhotoLabel['matched_on'][number], string>> = {
  sun_position: 'Sun position',
  time_of_year: 'time of year',
  forecast_class: 'forecast class',
};

const MODEL_NAMES: Readonly<Record<RunInfo['model'], string>> = { hrrr: 'HRRR', gfs: 'GFS' };

/** Sentences for a screen reader: the lead in sentence case, then each segment. */
function spokenOf(notice: string | null, lead: string, segments: readonly string[], line2: string, credits: readonly Credit[]): string {
  const sentence = (s: string): string => {
    const t = s.trim();
    return t.endsWith('.') ? t : `${t}.`;
  };
  const leadWords = lead.charAt(0) + lead.slice(1).toLowerCase().replace('ai texture', 'AI texture');
  const parts = [...(notice ? [sentence(notice)] : []), sentence(leadWords), ...segments.map(sentence)];
  if (line2) parts.push(...line2.split(SEP).map(sentence));
  for (const c of credits) parts.push(sentence(c.text));
  return parts.join(' ');
}

/** The reference year for printing years: the query's valid year in the place's zone, else none. */
function referenceYear(ctx: CaptionContext): number | undefined {
  return ctx.query ? yearIn(ctx.query.valid_time, ctx.query.tz) : undefined;
}

/** Tier 0: a real photo, when it was taken, and what it was matched on (PRD §9.1). */
export function photoCaption(l: PhotoLabel, ctx: CaptionContext): Caption {
  const { locale } = ctx.fmt;
  const lead = 'REAL PHOTO, CLOSEST MATCH';
  const ref = referenceYear(ctx);
  const taken = `taken ${formatDateTime(l.taken_at, l.tz, locale, ref === undefined ? { weekday: false } : { weekday: false, referenceYear: ref })}`;
  const matched = `matched on ${formatList(l.matched_on.map((m) => MATCH_WORDS[m]), locale)}`;
  const segments = [taken, matched];
  const where = `${ctx.display.place_name}, ${l.camera_name}`;
  const asked = ctx.query ? [`closest to ${formatDateTime(ctx.query.valid_time, ctx.query.tz, locale)}`] : [];
  const source = l.source === 'archive' ? 'from the NPS archive' : 'from our captures';
  const line2 = [where, ...asked, source].join(SEP);
  const notice = ctx.online ? null : 'Offline: cached photos only';
  return { kind: 'photo', lead, line1: [lead, ...segments].join(SEP), line2, notice, credits: l.credits, spoken: spokenOf(notice, lead, segments, line2, l.credits) };
}

/** The dominant cloud layer and its cover ("cloud low 80%"); ties go to the lower layer. */
export function cloudSegment(f: ForecastInfo, locale: string): string {
  const layers: [string, number][] = [
    ['low', f.cloud_low],
    ['mid', f.cloud_mid],
    ['high', f.cloud_high],
  ];
  let best = layers[0]!;
  for (const layer of layers) if (layer[1] > best[1]) best = layer;
  return `cloud ${best[0]} ${formatPercent(best[1], locale)}`;
}

function weatherSegments(f: ForecastInfo, fmt: FormatOptions): string[] {
  const out = [cloudSegment(f, fmt.locale)];
  out.push(f.ceiling_m === null ? 'no ceiling' : `ceiling ${formatCeiling(f.ceiling_m, fmt)}`);
  if (f.visibility_m !== null) out.push(`visibility ${formatVisibility(f.visibility_m, fmt)}`);
  if (f.precip_rate_mm_h !== null && f.precip_rate_mm_h > 0) out.push(`${f.precip_type ?? 'precipitation'} ${formatPrecipRate(f.precip_rate_mm_h, fmt)}`);
  return out;
}

/** "HRRR 18Z Thu, issued Thu, Nov 19, 11:27 AM PST (lead 28 h 33 min)". Lead is valid time minus availability (PRD §5.3). */
export function runSegment(l: SimulatedLabel | RefinedLabel, locale: string): string {
  const r = l.run;
  const lead = Date.parse(l.valid_time) - Date.parse(r.available_at);
  const pinned = r.pinned ? ' (pinned run)' : '';
  return `${MODEL_NAMES[r.model]} ${formatRunCycle(r.cycle_utc, locale)}${pinned}, issued ${formatDateTime(r.available_at, l.tz, locale)} (lead ${formatDuration(lead, locale)})`;
}

function skySegments(l: SimulatedLabel | RefinedLabel, locale: string): string[] {
  const moonWhere = l.moon.altitude_deg >= 0 ? 'above horizon' : 'below horizon';
  return [`Sun ${formatDegrees(l.sun_altitude_deg, locale)}`, `Moon ${formatPercent(l.moon.illuminated, locale)} lit, ${moonWhere}`];
}

function simulatedParts(l: SimulatedLabel | RefinedLabel, ctx: CaptionContext): { segments: string[]; line2: string[]; notice: string | null } {
  const { locale } = ctx.fmt;
  const when = formatDateTime(l.valid_time, l.tz, locale);
  const model = l.run.model === 'gfs' ? ['GFS forecast, beyond the 48 h HRRR range'] : [];
  const segments = [when, ...model, ...weatherSegments(l.forecast, ctx.fmt)];
  const line2 = [runSegment(l, locale), ...skySegments(l, locale)];
  const notice = ctx.online ? null : `Offline: forecast from ${MODEL_NAMES[l.run.model]} ${formatRunCycle(l.run.cycle_utc, locale)}, issued ${formatDateTime(l.run.available_at, l.tz, locale)}`;
  return { segments, line2, notice };
}

/** Tier 1: the relit plate. */
export function simulatedCaption(l: SimulatedLabel, ctx: CaptionContext): Caption {
  const lead = 'SIMULATED';
  const p = simulatedParts(l, ctx);
  const line2 = p.line2.join(SEP);
  return { kind: 'simulated', lead, line1: [lead, ...p.segments].join(SEP), line2, notice: p.notice, credits: l.credits, spoken: spokenOf(p.notice, lead, p.segments, line2, l.credits) };
}

/** Tier 2: Tier 1 with ground texture from an image model; the sky and haze are Tier 1's by construction (PRD §6.6 C9). */
export function refinedCaption(l: RefinedLabel, ctx: CaptionContext): Caption {
  const lead = 'SIMULATED + AI TEXTURE';
  const p = simulatedParts(l, ctx);
  const segments = [...p.segments, 'sky and haze unchanged from the simulation'];
  const line2 = [...p.line2, `texture ${l.refine.model} via ${l.refine.provider}`].join(SEP);
  return { kind: 'refined', lead, line1: [lead, ...segments].join(SEP), line2, notice: p.notice, credits: l.credits, spoken: spokenOf(p.notice, lead, segments, line2, l.credits) };
}

/** A live camera frame: where, when, how old, and the credit on line 1 (the PRD §5.3 example). */
export function liveCaption(l: LiveLabel, ctx: CaptionContext): Caption {
  const { locale } = ctx.fmt;
  const lead = 'LIVE';
  const now = ctx.nowMs ?? Date.now();
  const age = formatAge(now - Date.parse(l.taken_at), locale);
  const segments = [`${ctx.display.place_name}, ${l.camera_name}`, `${formatDateTime(l.taken_at, l.tz, locale)} (${age})`, ...l.credits.slice(0, 1).map((c) => c.text)];
  const rest = l.credits.slice(1);
  return { kind: 'live', lead, line1: [lead, ...segments].join(SEP), line2: '', notice: null, credits: rest, spoken: spokenOf(null, lead, segments, '', rest) };
}

/** The caption for a tier frame's label. */
export function composeCaption(l: PhotoLabel | SimulatedLabel | RefinedLabel, ctx: CaptionContext): Caption {
  switch (l.kind) {
    case 'photo':
      return photoCaption(l, ctx);
    case 'simulated':
      return simulatedCaption(l, ctx);
    case 'refined':
      return refinedCaption(l, ctx);
  }
}
