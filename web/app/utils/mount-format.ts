// Mount Status display rules (T1.3 / logic sheet A4-i). Pure functions, unit-tested.

export const EM_DASH = '—'
const MINUS = '−'
export const DEFAULT_SUN_KEEPOUT_DEG = 15
/** Last status message older than this (wall clock) marks the link stale. */
export const STALE_AFTER_MS = 1_500

export type ValueTone = 'normal' | 'positive' | 'danger' | 'dim'

export interface MountSnapshotLike {
  state?: string | null
  state_reason?: string | null
  time?: { mjd?: number | null, lmst_hours?: number | null } | null
  mount?: {
    connected?: boolean | null
    az?: number | null
    alt?: number | null
    ra_j2000_h?: number | null
    dec_j2000_deg?: number | null
  } | null
  axes?: Array<{ enabled?: boolean | null, rms_arcsec?: number | null } | null> | null
  sun_distance_deg?: number | null
  hour_angle_h?: number | null
  limits?: { sun_keepout_deg?: number | null } | null
}

export interface DisplayField {
  text: string
  tone: ValueTone
  /** Status dot (axis enabled/disabled) shown before the text. */
  dot?: 'on' | 'off'
}

export interface MountDisplay {
  azimuth: DisplayField
  elevation: DisplayField
  sunDistance: DisplayField
  status: DisplayField
  statusReason: string | null
}

function finite(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** `204.63`: 2 decimals, no sign; null/non-finite gives an em dash. A value rounding to 360.00 shows 0.00. */
export function formatAzimuth(v: number | null | undefined): string {
  if (!finite(v)) return EM_DASH
  const r = Number(v.toFixed(2))
  return (r >= 360 ? r - 360 : r).toFixed(2)
}

/** `+ 58.32` / `U+2212 3.10`: sign, space, 2 decimals. A value rounding to zero is `+ 0.00`. */
export function formatElevation(v: number | null | undefined): string {
  if (!finite(v)) return EM_DASH
  const r = Number(v.toFixed(2))
  return `${r < 0 ? MINUS : '+'} ${Math.abs(r).toFixed(2)}`
}

/** `136.17` followed by the degree sign. */
export function formatSunDistance(v: number | null | undefined): string {
  if (!finite(v)) return EM_DASH
  return `${v.toFixed(2)}°`
}

/**
 * Sun Distance (A4-i): green strictly above the keep-out, red at or inside it
 * (exactly 15.00 is inside the disc, so red).
 */
export function sunDistanceTone(v: number | null | undefined, keepoutDeg = DEFAULT_SUN_KEEPOUT_DEG): ValueTone {
  if (!finite(v)) return 'dim'
  return v > keepoutDeg ? 'positive' : 'danger'
}

/** Status (A4-i): green on TRACKING, red on FAULT, plain otherwise. */
export function statusTone(state: string | null | undefined): ValueTone {
  if (state === 'TRACKING') return 'positive'
  if (state === 'FAULT') return 'danger'
  return 'normal'
}

export function isLinkStale(
  connection: string,
  lastStatusAtMs: number | null,
  nowMs: number,
  staleAfterMs = STALE_AFTER_MS,
): boolean {
  if (connection !== 'open') return true
  if (lastStatusAtMs === null) return true
  return nowMs - lastStatusAtMs > staleAfterMs
}

export function buildMountDisplay(snapshot: MountSnapshotLike | null, linkStale: boolean): MountDisplay {
  if (linkStale) {
    return {
      azimuth: { text: EM_DASH, tone: 'dim' },
      elevation: { text: EM_DASH, tone: 'dim' },
      sunDistance: { text: EM_DASH, tone: 'dim' },
      status: { text: 'DISCONNECTED', tone: 'normal' },
      statusReason: 'gateway not reachable',
    }
  }
  const az = snapshot?.mount?.az
  const alt = snapshot?.mount?.alt
  const sun = snapshot?.sun_distance_deg
  const field = (text: string, v: unknown, tone: ValueTone = 'normal'): DisplayField =>
    finite(v) ? { text, tone } : { text: EM_DASH, tone: 'dim' }
  const state = snapshot?.state ?? null
  return {
    azimuth: field(formatAzimuth(az), az),
    elevation: field(formatElevation(alt), alt),
    sunDistance: field(
      formatSunDistance(sun),
      sun,
      sunDistanceTone(sun, snapshot?.limits?.sun_keepout_deg ?? DEFAULT_SUN_KEEPOUT_DEG),
    ),
    status: state ? { text: state, tone: statusTone(state) } : { text: EM_DASH, tone: 'dim' },
    statusReason: snapshot?.state_reason ?? null,
  }
}
