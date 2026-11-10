// Track Target > AIRCRAFT tab helpers (T5.5), pure: range preset filter, search (callsign / ICAO,
// exact match first as the satellite tab), sortable columns (default: range, nearest first).
import type { AircraftRow } from './adsb'

export const TARGET_RANGE_PRESETS_KM = [20, 50, 100, 200, 350] as const
export const TARGET_DEFAULT_RANGE_KM = 100

export type AircraftSortKey = 'callsign' | 'icao' | 'alt' | 'range' | 'el'
export interface AircraftSortState { key: AircraftSortKey, dir: 'asc' | 'desc' }
export const DEFAULT_AIRCRAFT_SORT: AircraftSortState = { key: 'range', dir: 'asc' }

/** Click on a header: the same column flips the direction; a new column starts ascending
 *  (altitude and elevation start descending: highest first). */
export function nextAircraftSort(cur: AircraftSortState, key: AircraftSortKey): AircraftSortState {
  if (cur.key === key) return { key, dir: cur.dir === 'asc' ? 'desc' : 'asc' }
  return { key, dir: key === 'alt' || key === 'el' ? 'desc' : 'asc' }
}

/** Rows within the range preset (km) and matching the search text (substring of callsign or ICAO, case-insensitive). */
export function filterAircraft(rows: readonly AircraftRow[], query: string, rangeKm: number): AircraftRow[] {
  const q = query.trim().toUpperCase()
  return rows.filter((r) => {
    if (!(r.range_m / 1000 <= rangeKm)) return false
    if (!q) return true
    return (r.callsign ?? '').toUpperCase().includes(q) || r.icao.toUpperCase().includes(q)
  })
}

function sortValue(r: AircraftRow, key: AircraftSortKey): number | string | null {
  switch (key) {
    case 'callsign': return r.callsign ? r.callsign.trim().toLowerCase() || null : null
    case 'icao': return r.icao.toLowerCase()
    case 'alt': return typeof r.alt_ft === 'number' ? r.alt_ft : null
    case 'range': return r.range_m
    case 'el': return r.el
  }
}

/** Stable copy sorted by one column; empty values always go last. */
export function sortAircraft(rows: readonly AircraftRow[], s: AircraftSortState): AircraftRow[] {
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

/** True when the search text equals the callsign or the ICAO exactly. */
export function isExactAircraft(r: AircraftRow, query: string): boolean {
  const q = query.trim().toUpperCase()
  if (!q) return false
  return (r.callsign ?? '').trim().toUpperCase() === q || r.icao.toUpperCase() === q
}

/** Exact matches on top (stable; the column sort applies within each group). */
export function exactFirstAircraft(rows: readonly AircraftRow[], query: string): AircraftRow[] {
  if (!query.trim()) return [...rows]
  return [...rows.filter(r => isExactAircraft(r, query)), ...rows.filter(r => !isExactAircraft(r, query))]
}

/** The list the tab shows: range + search filter, column sort, exact matches first. */
export function aircraftTabRows(rows: readonly AircraftRow[], query: string, rangeKm: number, sort: AircraftSortState): AircraftRow[] {
  return exactFirstAircraft(sortAircraft(filterAircraft(rows, query, rangeKm), sort), query)
}
