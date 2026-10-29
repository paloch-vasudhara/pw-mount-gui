// Track Target modal helpers (T4.3c, A5c-ii): query building, client-side sorting, epoch format,
// status-line text and keyboard selection. Pure; the dialog owns fetching and polling.
import { formatClock, type ClockMode } from './sky-clock'
import { ownerName } from './owner-names'
import type { TrackVerdict } from './track-verdict'

export interface SatRow {
  norad: number
  name: string
  cospar: string | null
  country: string | null
  object_type: string | null
  orbit_class: string | null
  tle_epoch: string | null
  tle_age_days: number | null
}

export type SortKey = 'norad' | 'name' | 'orbit' | 'origin' | 'epoch'
export type SortDir = 'asc' | 'desc'
export interface SortState { key: SortKey, dir: SortDir }

export const SEARCH_LIMIT = 500
export const DEFAULT_SORT: SortState = { key: 'norad', dir: 'desc' }
export const ORBIT_GROUPS = ['leo', 'meo', 'geo'] as const
export const ANY = 'any'
export const SELECT_TARGET = 'Select a target'
export const FOOTER_CAPPED = `Showing the first ${SEARCH_LIMIT} matches — refine the search`
export const DATA_UNAVAILABLE = 'Satellite data unavailable'

export interface SearchFilters { q: string, orbit: string, country: string }
export const EMPTY_FILTERS: SearchFilters = { q: '', orbit: ANY, country: ANY }

/** Query string for GET /api/sats/search (no leading "?"). Empty / "any" filters are left out. */
export function buildSearchQuery(f: SearchFilters, limit = SEARCH_LIMIT): string {
  const p = new URLSearchParams()
  const q = f.q.trim()
  if (q) p.set('q', q)
  if ((ORBIT_GROUPS as readonly string[]).includes(f.orbit)) p.set('orbit_group', f.orbit)
  if (f.country && f.country !== ANY) p.set('country', f.country)
  p.set('limit', String(limit))
  return p.toString()
}

export const isCapped = (n: number): boolean => n >= SEARCH_LIMIT

/** Click on a header: the same column flips the direction; a new column starts ascending
 *  (NORAD ID and EPOCH start descending: newest first). */
export function nextSort(cur: SortState, key: SortKey): SortState {
  if (cur.key === key) return { key, dir: cur.dir === 'asc' ? 'desc' : 'asc' }
  return { key, dir: key === 'norad' || key === 'epoch' ? 'desc' : 'asc' }
}

function epochMs(r: SatRow): number | null {
  if (!r.tle_epoch) return null
  const t = Date.parse(r.tle_epoch)
  return Number.isFinite(t) ? t : null
}

function sortValue(r: SatRow, key: SortKey): number | string | null {
  switch (key) {
    case 'norad': return r.norad
    case 'name': return (r.name ?? '').toLowerCase()
    case 'orbit': return r.orbit_class ? r.orbit_class.toLowerCase() : null
    case 'origin': return r.country ? r.country.toLowerCase() : null
    case 'epoch': return epochMs(r)
  }
}

/** Stable copy sorted by one column; empty values always go last. */
export function sortRows(rows: readonly SatRow[], s: SortState): SatRow[] {
  const sign = s.dir === 'asc' ? 1 : -1
  return rows
    .map((r, i) => ({ r, i, v: sortValue(r, s.key) }))
    .sort((a, b) => {
      if (a.v === null || b.v === null) {
        if (a.v === b.v) return a.i - b.i
        return a.v === null ? 1 : -1
      }
      const c = a.v < b.v ? -1 : a.v > b.v ? 1 : 0
      return c !== 0 ? c * sign : a.i - b.i
    })
    .map(x => x.r)
}

/** `YYYY-MM-DD hh:mm:ss` in the dashboard clock's zone; a dash when missing / unparsable. */
export function formatEpoch(iso: string | null | undefined, mode: ClockMode, tz?: string | null): string {
  if (!iso) return '—'
  const ms = Date.parse(iso)
  if (!Number.isFinite(ms)) return '—'
  const p = formatClock(ms, mode, tz)
  return `${p.date} ${p.hh}:${p.mm}:${p.ss}`
}

export interface TargetStatus { text: string, tone: 'normal' | 'danger', canTrack: boolean }

/** Status line + whether TRACK SELECTED TARGET is enabled. */
export function targetStatus(o: {
  selected: number | null
  verdict: TrackVerdict | null
  sending: boolean
  ackRefusal: string | null
}): TargetStatus {
  if (o.selected === null) return { text: SELECT_TARGET, tone: 'normal', canTrack: false }
  if (o.sending) return { text: 'Starting track…', tone: 'normal', canTrack: false }
  const v = o.verdict
  if (o.ackRefusal) return { text: o.ackRefusal, tone: 'danger', canTrack: !!v?.enabled }
  if (!v) return { text: 'Checking…', tone: 'normal', canTrack: false }
  if (!v.enabled) return { text: v.reason ?? 'Command refused', tone: 'normal', canTrack: false }
  return { text: v.notes.length ? v.notes.join(' · ') : 'Ready', tone: 'normal', canTrack: true }
}

/** Up / down: next selected NORAD (clamped at the ends; nothing selected: first on down, last on up). */
export function moveSelection(norads: readonly number[], cur: number | null, delta: 1 | -1): number | null {
  if (!norads.length) return null
  const i = cur === null ? -1 : norads.indexOf(cur)
  if (i < 0) return delta === 1 ? norads[0]! : norads[norads.length - 1]!
  return norads[Math.max(0, Math.min(norads.length - 1, i + delta))]!
}

/** Origin combobox filter: owner codes whose code OR full name contains the query (case-insensitive substring). */
export function filterOwners(codes: readonly string[], query: string): string[] {
  const q = query.trim().toLowerCase()
  if (!q) return [...codes]
  return codes.filter(c => c.toLowerCase().includes(q) || ownerName(c).toLowerCase().includes(q))
}
