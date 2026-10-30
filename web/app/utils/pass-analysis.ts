// Satellite Pass Analysis modal helpers (T5.1c, A2b-i): window validation, request building, time /
// duration formats (all UTC), sorting and the live [Track] eligibility. Pure; the dialog owns fetching.
import { pad2 } from './datetime-fields'

export type PassMode = 'optical' | 'los'

/** One row of POST /api/passes (analysis). `t_rise` / `t_set` are null for a GEO-like object or a pass setting after the padded window; the real times may lie outside the listed window (see `clipToWindow`). */
export interface PassRow {
  t_rise: number | null
  t_set: number | null
  rise_az: number | null
  set_az: number | null
  t_peak: number
  max_el: number
  duration_s: number
  visible_s?: number
  visible?: Array<[number, number]>
}

export interface PassResponse {
  name: string
  norad: number
  tle_epoch: string | null
  tle_age_days: number | null
  passes: PassRow[]
}

export interface PassSite { lat: number, lon: number, alt_m: number }

export const MAX_WINDOW_S = 7 * 86400
/** A row gets [Track] when it is in progress or its Lim EL crossing is at most this far ahead (Q27). */
export const TRACK_LEAD_S = 300
export const DAY_S = 86400

export const NO_SATELLITE = 'Select a satellite'
export const NO_SITE = 'Station location not available'
export const ERR_UNAVAILABLE = 'Satellite data unavailable'
export const ERR_TIMEOUT = 'Pass computation timed out'
export const NONE_FOUND_LOS = 'No passes above Lim EL in this window'
export const NONE_FOUND_OPTICAL = 'No visible passes above Lim EL in this window'

/** Now (UTC) cut to the minute: the default Start. */
export function defaultStart(nowMs: number): Date {
  return new Date(Math.floor(nowMs / 60000) * 60000)
}

export function defaultEnd(start: Date): Date {
  return new Date(start.getTime() + DAY_S * 1000)
}

export interface WindowCheck {
  startInvalid: boolean
  endInvalid: boolean
  /** Why LIST PASSES is blocked by the window (null when the window is fine). */
  reason: string | null
}

export function checkWindow(start: Date | null, end: Date | null): WindowCheck {
  const s = start?.getTime()
  const e = end?.getTime()
  if (s === undefined || !Number.isFinite(s)) return { startInvalid: true, endInvalid: false, reason: 'Enter a start time' }
  if (e === undefined || !Number.isFinite(e)) return { startInvalid: false, endInvalid: true, reason: 'Enter an end time' }
  if (e <= s) return { startInvalid: false, endInvalid: true, reason: 'End must be after Start' }
  if ((e - s) / 1000 > MAX_WINDOW_S) return { startInvalid: false, endInvalid: true, reason: 'Window longer than 7 days' }
  return { startInvalid: false, endInvalid: false, reason: null }
}

/** The reason LIST PASSES is disabled (null = ready). */
export function listBlockReason(o: { norad: number | null, site: PassSite | null, limEl: number | null, start: Date | null, end: Date | null }): string | null {
  if (o.norad === null) return NO_SATELLITE
  if (!o.site || o.limEl === null) return NO_SITE
  return checkWindow(o.start, o.end).reason
}

export function buildPassRequest(o: { norad: number, site: PassSite, limEl: number, start: Date, end: Date, mode: PassMode }) {
  return {
    norad: o.norad,
    site: { lat: o.site.lat, lon: o.site.lon, alt_m: o.site.alt_m },
    lim_el: o.limEl,
    start_unix: Math.floor(o.start.getTime() / 1000),
    end_unix: Math.floor(o.end.getTime() / 1000),
    mode: o.mode,
  }
}

/** Site + Lim EL from the gateway snapshot (`site.lat/lon/height_m`, `limits.lim_el_deg`); null while unknown. */
export function siteFromSnapshot(snap: unknown): { site: PassSite, limEl: number } | null {
  const s = snap as { site?: { lat?: unknown, lon?: unknown, height_m?: unknown } | null, limits?: { lim_el_deg?: unknown } | null } | null
  const lat = s?.site?.lat
  const lon = s?.site?.lon
  const le = s?.limits?.lim_el_deg
  if (typeof lat !== 'number' || typeof lon !== 'number' || typeof le !== 'number') return null
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || !Number.isFinite(le)) return null
  const h = s?.site?.height_m
  return { site: { lat, lon, alt_m: typeof h === 'number' && Number.isFinite(h) ? h : 0 }, limEl: le }
}

/** Error line for a failed POST /api/passes (status 0 = network / timeout). */
export function passErrorText(status: number, detail: unknown): string {
  const d = typeof detail === 'string' ? detail : ''
  if (status === 422) return d || 'Invalid request'
  if (status === 404) return 'Satellite not found'
  if (status === 503 && /timed out/i.test(d)) return ERR_TIMEOUT
  return ERR_UNAVAILABLE
}

// ----- formats (UTC)
const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const dateOf = (t: number): Date => new Date(Math.round(t) * 1000)
const ymdOf = (d: Date): string => `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`
const hmsOf = (d: Date): string => `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`

export const NONE = '—'

/** `YYYY-MM-DD hh:mm:ss` (UTC), for rises and sets (a set always carries its date: user 10-30). */
export function fmtRise(t: number | null): string {
  if (!isNum(t)) return NONE
  const d = dateOf(t)
  return `${ymdOf(d)} ${hmsOf(d)}`
}

/** `mmᴹ ssˢ`; from 1 h `hhᴴ mmᴹ`. */
export function fmtDuration(seconds: number | null | undefined): string {
  if (!isNum(seconds)) return NONE
  const s = Math.max(0, Math.round(seconds))
  if (s >= 3600) return `${pad2(Math.floor(s / 3600))}ᴴ ${pad2(Math.floor((s % 3600) / 60))}ᴹ`
  return `${pad2(Math.floor(s / 60))}ᴹ ${pad2(s % 60)}ˢ`
}

export const fmtDeg = (v: number | null | undefined): string => (isNum(v) ? `${v.toFixed(1)}°` : NONE)

// ----- sorting
export type PassSortKey = 'rise' | 'rise_az' | 'max_el' | 'set' | 'set_az' | 'dur'
export type SortDir = 'asc' | 'desc'
export interface PassSort { key: PassSortKey, dir: SortDir }
export const DEFAULT_PASS_SORT: PassSort = { key: 'rise', dir: 'asc' }

/** Click on a header: the same column flips direction, a new one starts ascending. */
export function nextPassSort(cur: PassSort, key: PassSortKey): PassSort {
  return cur.key === key ? { key, dir: cur.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' }
}

/** The duration a row shows: the visible part (optical) or the whole pass (LOS). */
export const rowDuration = (p: PassRow, mode: PassMode): number => (mode === 'optical' ? (p.visible_s ?? 0) : p.duration_s)

/** The listed window (Unix s) a result belongs to. */
export interface PassWindow { start: number, end: number }

/**
 * What a row shows (user 10-30): a rise before the window start or a set after its end is shown as "—"
 * (time and azimuth); `duration_s` / `visible_s` from the analysis are already clipped to the window.
 * The [Track] eligibility keeps using the real times of the row.
 */
export function clipToWindow(p: PassRow, w: PassWindow | null): PassRow {
  if (!w) return p
  const early = isNum(p.t_rise) && p.t_rise < w.start
  const late = isNum(p.t_set) && p.t_set > w.end
  if (!early && !late) return p
  return {
    ...p,
    t_rise: early ? null : p.t_rise,
    rise_az: early ? null : p.rise_az,
    t_set: late ? null : p.t_set,
    set_az: late ? null : p.set_az,
  }
}

function sortValue(p: PassRow, key: PassSortKey, mode: PassMode): number {
  switch (key) {
    case 'rise': return isNum(p.t_rise) ? p.t_rise : -Infinity // no rise = already above Lim EL before the window
    case 'set': return isNum(p.t_set) ? p.t_set : Infinity
    case 'rise_az': return isNum(p.rise_az) ? p.rise_az : -Infinity
    case 'set_az': return isNum(p.set_az) ? p.set_az : -Infinity
    case 'max_el': return p.max_el
    case 'dur': return rowDuration(p, mode)
  }
}

/** Sort into a new array by the shown values (`clipToWindow`); ties by peak time. Returns the original rows. */
export function sortPasses(rows: readonly PassRow[], sort: PassSort, mode: PassMode, win: PassWindow | null = null): PassRow[] {
  const sign = sort.dir === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    const x = sortValue(clipToWindow(a, win), sort.key, mode)
    const y = sortValue(clipToWindow(b, win), sort.key, mode)
    const c = x === y ? 0 : x < y ? -1 : 1
    return c * sign || a.t_peak - b.t_peak
  })
}

// ----- [Track]
/** The pass has ended (its Lim EL setting is behind us). */
export const passEnded = (p: PassRow, nowS: number): boolean => isNum(p.t_set) && nowS >= p.t_set

/** [Track] shows on a pass in progress (a null rise counts as in progress) or rising within TRACK_LEAD_S; never once ended. */
export function trackEligible(p: PassRow, nowS: number): boolean {
  if (passEnded(p, nowS)) return false
  if (!isNum(p.t_rise)) return true
  return p.t_rise - nowS <= TRACK_LEAD_S
}
