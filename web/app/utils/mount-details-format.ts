// Detailed Mount Status modal (T1.4 / A4-ii): parameter formatters + display builder.
// Pure functions, unit-tested. Shared rules (tones, link staleness) live in mount-format.ts.
import {
  DEFAULT_SUN_KEEPOUT_DEG, EM_DASH, statusTone, sunDistanceTone,
  type DisplayField, type MountSnapshotLike, type ValueTone,
} from './mount-format'

const MINUS = '−'
const PRIME = '′'
const DPRIME = '″'
/** Superscript h / m / s (U+02B0, U+1D50, U+02E2); the modal renders them as <sup>. */
export const SUP_H = 'ʰ'
export const SUP_M = 'ᵐ'
export const SUP_S = 'ˢ'

function finite(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

const pad2 = (n: number) => String(n).padStart(2, '0')

/** Splits a non-negative amount (hours or degrees) into whole units / minutes / 2-decimal seconds, carrying correctly. */
function sexagesimal(value: number): { u: number, m: number, s: string } {
  const totalCs = Math.round(value * 3600 * 100)
  const cs = totalCs % 100
  const secs = Math.floor(totalCs / 100)
  return {
    u: Math.floor(secs / 3600),
    m: Math.floor(secs / 60) % 60,
    s: `${pad2(secs % 60)}.${pad2(cs)}`,
  }
}

/** `61 333.19885`: 5 decimals, single-space thousands separator. */
export function formatMjd(v: number | null | undefined): string {
  if (!finite(v)) return EM_DASH
  const [int = '0', frac = ''] = Math.abs(v).toFixed(5).split('.')
  const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, ' ')
  return `${v < 0 ? MINUS : ''}${grouped}.${frac}`
}

/** `HH:MM:SS` from sidereal hours (wrapped into 0-24). */
export function formatLst(hours: number | null | undefined): string {
  if (!finite(hours)) return EM_DASH
  const total = Math.round((((hours % 24) + 24) % 24) * 3600) % 86400
  return `${pad2(Math.floor(total / 3600))}:${pad2(Math.floor((total % 3600) / 60))}:${pad2(total % 60)}`
}

/** `320.500°`: 3 decimals, no sign; rounding to 360.000 shows 0.000. */
export function formatAzimuth3(v: number | null | undefined): string {
  if (!finite(v)) return EM_DASH
  const r = Number(v.toFixed(3))
  return `${(r >= 360 ? r - 360 : r).toFixed(3)}°`
}

/** `39.302°`: 3 decimals; U+2212 minus when negative (no plus sign), as in the render. */
export function formatElevation3(v: number | null | undefined): string {
  if (!finite(v)) return EM_DASH
  const r = Number(v.toFixed(3))
  return `${r < 0 ? MINUS : ''}${Math.abs(r).toFixed(3)}°`
}

/** `16ʰ 25ᵐ 43.32ˢ`: right ascension from hours, wrapped into 0-24. */
export function formatRa(hours: number | null | undefined): string {
  if (!finite(hours)) return EM_DASH
  const { u, m, s } = sexagesimal(((hours % 24) + 24) % 24)
  return `${pad2(u % 24)}${SUP_H} ${pad2(m)}${SUP_M} ${s}${SUP_S}`
}

/** `+50° 08′ 39.30″`: declination; the sign is always shown (zero is +). */
export function formatDec(deg: number | null | undefined): string {
  if (!finite(deg)) return EM_DASH
  const { u, m, s } = sexagesimal(Math.abs(deg))
  const zero = u === 0 && m === 0 && Number(s) === 0
  return `${deg < 0 && !zero ? MINUS : '+'}${pad2(u)}° ${pad2(m)}${PRIME} ${s}${DPRIME}`
}

/** `03ʰ 12ᵐ 20.11ˢ`: hour angle (the gateway wraps it to +-12 h); U+2212 minus when negative, no plus sign. */
export function formatHourAngle(hours: number | null | undefined): string {
  if (!finite(hours)) return EM_DASH
  const { u, m, s } = sexagesimal(Math.abs(hours))
  const zero = u === 0 && m === 0 && Number(s) === 0
  return `${hours < 0 && !zero ? MINUS : ''}${pad2(u)}${SUP_H} ${pad2(m)}${SUP_M} ${s}${SUP_S}`
}

/** `0.560″`: axis RMS error in arcseconds. */
export function formatRms(v: number | null | undefined): string {
  if (!finite(v)) return EM_DASH
  return `${v.toFixed(3)}${DPRIME}`
}

/** `82.270°`: the modal shows 3 decimals (the dashboard panel shows 2). */
export function formatSunDistance3(v: number | null | undefined): string {
  if (!finite(v)) return EM_DASH
  return `${v.toFixed(3)}°`
}

export interface DetailsDisplay {
  mjd: DisplayField
  lst: DisplayField
  azimuth: DisplayField
  elevation: DisplayField
  ra: DisplayField
  dec: DisplayField
  hourAngle: DisplayField
  status: DisplayField
  statusReason: string | null
  axis0Status: DisplayField
  axis1Status: DisplayField
  axis0Rms: DisplayField
  axis1Rms: DisplayField
  sunDistance: DisplayField
  /** For the (disabled) control groups: which segment is highlighted. */
  mountConnected: boolean
  tracking: boolean
}

const DIM: DisplayField = { text: EM_DASH, tone: 'dim' }

function axisStatusField(enabled: boolean | null | undefined, mountConnected: boolean): DisplayField {
  if (!mountConnected || typeof enabled !== 'boolean') return DIM
  return enabled
    ? { text: 'Enabled', tone: 'normal', dot: 'on' }
    : { text: 'Disabled', tone: 'normal', dot: 'off' }
}

export function buildDetailsDisplay(snapshot: MountSnapshotLike | null, linkStale: boolean): DetailsDisplay {
  if (linkStale || !snapshot) {
    return {
      mjd: DIM, lst: DIM, azimuth: DIM, elevation: DIM, ra: DIM, dec: DIM, hourAngle: DIM,
      status: linkStale ? { text: 'DISCONNECTED', tone: 'normal' } : DIM,
      statusReason: linkStale ? 'gateway not reachable' : null,
      axis0Status: DIM, axis1Status: DIM, axis0Rms: DIM, axis1Rms: DIM, sunDistance: DIM,
      mountConnected: false, tracking: false,
    }
  }
  const field = (text: string, v: unknown, tone: ValueTone = 'normal'): DisplayField =>
    finite(v) ? { text, tone } : DIM
  const m = snapshot.mount
  const connected = m?.connected === true
  const state = snapshot.state ?? null
  const sun = snapshot.sun_distance_deg
  const ax0 = snapshot.axes?.[0]
  const ax1 = snapshot.axes?.[1]
  return {
    mjd: field(formatMjd(snapshot.time?.mjd), snapshot.time?.mjd),
    lst: field(formatLst(snapshot.time?.lmst_hours), snapshot.time?.lmst_hours),
    azimuth: field(formatAzimuth3(m?.az), m?.az),
    elevation: field(formatElevation3(m?.alt), m?.alt),
    ra: field(formatRa(m?.ra_j2000_h), m?.ra_j2000_h),
    dec: field(formatDec(m?.dec_j2000_deg), m?.dec_j2000_deg),
    hourAngle: field(formatHourAngle(snapshot.hour_angle_h), snapshot.hour_angle_h),
    status: state ? { text: state, tone: statusTone(state) } : DIM,
    statusReason: snapshot.state_reason ?? null,
    axis0Status: axisStatusField(ax0?.enabled, connected),
    axis1Status: axisStatusField(ax1?.enabled, connected),
    axis0Rms: connected ? field(formatRms(ax0?.rms_arcsec), ax0?.rms_arcsec) : DIM,
    axis1Rms: connected ? field(formatRms(ax1?.rms_arcsec), ax1?.rms_arcsec) : DIM,
    sunDistance: field(
      formatSunDistance3(sun),
      sun,
      sunDistanceTone(sun, snapshot.limits?.sun_keepout_deg ?? DEFAULT_SUN_KEEPOUT_DEG),
    ),
    mountConnected: connected,
    tracking: state === 'TRACKING',
  }
}
