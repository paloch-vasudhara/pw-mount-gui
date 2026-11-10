// ADS-B (T5.4b): the gateway's `adsb` status block, the 1 Hz `aircraft` message types and the
// Settings status line. Pure; the T5.5 display logic (interpolation, card, filters, tab) is in aircraft.ts.

/** States the gateway reports (live: adsb_core's client states; simulated: see below). */
export type AdsbState =
  | 'connecting' | 'connected' | 'stale' | 'unreachable' | 'auth_failed' | 'tls_error' | 'error'
  | 'not_configured' | 'disabled' | 'module_missing' | 'simulated' | string

/**
 * `snapshot.adsb` (10 Hz status, no aircraft list) and `aircraft.data.adsb` (1 Hz).
 * `green` is the Q62 enable rule: the aircraft features are usable only when it is true;
 * otherwise `detail` says why.
 */
export interface AdsbInfo {
  state: AdsbState
  green: boolean
  /** The gateway was launched with --simulated-adsb: data comes from adsb_interpreter's feed. */
  simulated: boolean
  detail: string
  delay_p50_s?: number | null
  delay_p95_s?: number | null
  counts?: Record<string, number>
  last_event_age_s?: number | null
  range_limit_km?: number | null
}

/** One visible aircraft of the 1 Hz `aircraft` message (above 0 deg, within the range limit). */
export interface AircraftRow {
  icao: string
  callsign: string | null
  /** Position (5 dp) for the A1b card; absent on a gateway older than T5.5. */
  lat?: number | null
  lon?: number | null
  az: number
  el: number
  range_m: number
  track_deg: number | null
  alt_ft: number | null
  gs_kt: number | null
  vrate_fpm: number | null
  pos_age_s: number
  stale: boolean
  height_source: string | null
  /** 'velocity' | 'kalman' | null (no basis to predict yet) */
  predictor: string | null
}

export interface AircraftMessageData {
  adsb: AdsbInfo
  aircraft: AircraftRow[]
}

export const ADSB_DEFAULT_URL = 'wss://10.22.123.68/api/v1/ws'
export const ADSB_RANGE_LIMITS_KM = [20, 50, 100, 200, 350] as const
export const ADSB_DEFAULT_RANGE_KM = 100
export const SIMULATED_SOURCE = 'adsb_interpreter feed 8500'

/** Why a typed platform URL is refused (mirrors the gateway), else null. */
export function adsbUrlError(url: string): string | null {
  const v = url.trim()
  if (v === '') return null
  if (/\s/.test(v)) return 'The URL must not contain spaces'
  let u: URL
  try {
    u = new URL(v)
  }
  catch {
    return `Enter a full URL, e.g. ${ADSB_DEFAULT_URL}`
  }
  if (u.protocol !== 'wss:') return 'The URL must start with wss://'
  if (u.username || u.password || /^[a-z]+:\/\/[^/?#]*@/i.test(v)) return 'The read token has its own field; no login in the URL'
  if (u.search || u.hash || v.includes('?') || v.includes('#')) return 'The URL must not have a query or a fragment'
  return null
}

/** Why a typed read token is refused (mirrors the gateway), else null. Empty = untouched. */
export function adsbTokenError(token: string): string | null {
  if (token === '') return null
  if (token.length > 4096) return 'The token is too long'
  if (/\s/.test(token)) return 'The token must not contain spaces'
  return null
}

function secs(v: number, d = 1): string {
  return `${v.toFixed(d)} s`
}

/** The Settings status line: state + reason, last message age, delay p50. */
export function adsbStatusLine(s: AdsbInfo | null | undefined): string {
  if (!s) return 'Not available'
  const parts = [s.detail || s.state]
  if (s.green && s.last_event_age_s != null) parts.push(`last message ${secs(s.last_event_age_s)} ago`)
  if (s.green && s.delay_p50_s != null) parts.push(`delay p50 ${secs(s.delay_p50_s, 2)}`)
  return parts.join(' · ')
}
