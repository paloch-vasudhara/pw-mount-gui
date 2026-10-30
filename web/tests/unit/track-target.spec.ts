import { describe, expect, it } from 'vitest'
import {
  exactFirst, isExactMatch,
  buildSearchQuery, DEFAULT_SORT, formatEpoch, isCapped, moveSelection, nextSort, sortRows, targetStatus,
  type SatRow,
} from '../../app/utils/track-target'
import type { TrackVerdict } from '../../app/utils/track-verdict'

const row = (norad: number, o: Partial<SatRow> = {}): SatRow => ({
  norad, name: `S${norad}`, cospar: null, country: 'US', object_type: 'PAYLOAD', orbit_class: 'LEO', tle_epoch: '2026-10-08T05:59:55Z', tle_age_days: 1, ...o,
})
const ok: TrackVerdict = { enabled: true, reason: null, phase: 'track', notes: [], detour: null }

describe('buildSearchQuery', () => {
  it('default is just the limit', () => expect(buildSearchQuery({ q: '', orbit: 'any', country: 'any' })).toBe('limit=500'))
  it('adds trimmed q, orbit_group and country', () => {
    const p = new URLSearchParams(buildSearchQuery({ q: ' thai com ', orbit: 'geo', country: 'TH' }))
    expect(p.get('q')).toBe('thai com')
    expect(p.get('orbit_group')).toBe('geo')
    expect(p.get('country')).toBe('TH')
    expect(p.get('limit')).toBe('500')
  })
  it('ignores an unknown orbit', () => expect(new URLSearchParams(buildSearchQuery({ q: '', orbit: 'x', country: 'any' })).has('orbit_group')).toBe(false))
  it('cap detection', () => {
    expect(isCapped(500)).toBe(true)
    expect(isCapped(499)).toBe(false)
  })
})

describe('sortRows', () => {
  const rows = [row(3, { name: 'b', tle_epoch: '2026-01-02T00:00:00Z' }), row(10, { name: 'A', orbit_class: 'GEO', country: 'TH' }), row(7, { name: 'c', tle_epoch: null, orbit_class: null })]
  it('norad desc default', () => expect(sortRows(rows, DEFAULT_SORT).map(r => r.norad)).toEqual([10, 7, 3]))
  it('norad asc', () => expect(sortRows(rows, { key: 'norad', dir: 'asc' }).map(r => r.norad)).toEqual([3, 7, 10]))
  it('name case-insensitive', () => expect(sortRows(rows, { key: 'name', dir: 'asc' }).map(r => r.norad)).toEqual([10, 3, 7]))
  it('empty orbit last in both directions', () => {
    expect(sortRows(rows, { key: 'orbit', dir: 'asc' }).map(r => r.norad)).toEqual([10, 3, 7])
    expect(sortRows(rows, { key: 'orbit', dir: 'desc' }).map(r => r.norad)).toEqual([3, 10, 7])
  })
  it('epoch as time, missing last', () => {
    expect(sortRows(rows, { key: 'epoch', dir: 'asc' }).map(r => r.norad)).toEqual([3, 10, 7])
    expect(sortRows(rows, { key: 'epoch', dir: 'desc' }).map(r => r.norad)).toEqual([10, 3, 7])
  })
  it('origin', () => expect(sortRows(rows, { key: 'origin', dir: 'asc' }).map(r => r.norad)).toEqual([10, 3, 7]))
  it('stable on ties and does not mutate', () => {
    const t = [row(1), row(2), row(3)]
    expect(sortRows(t, { key: 'orbit', dir: 'desc' }).map(r => r.norad)).toEqual([1, 2, 3])
    expect(t.map(r => r.norad)).toEqual([1, 2, 3])
  })
  it('nextSort', () => {
    expect(nextSort(DEFAULT_SORT, 'norad')).toEqual({ key: 'norad', dir: 'asc' })
    expect(nextSort(DEFAULT_SORT, 'name')).toEqual({ key: 'name', dir: 'asc' })
    expect(nextSort(DEFAULT_SORT, 'epoch')).toEqual({ key: 'epoch', dir: 'desc' })
  })
})

describe('formatEpoch', () => {
  it('UTC', () => expect(formatEpoch('2026-10-08T05:59:55Z', 'utc')).toBe('2026-10-08 05:59:55'))
  it('site zone', () => expect(formatEpoch('2026-10-08T05:59:55Z', 'local', 'Asia/Bangkok')).toBe('2026-10-08 12:59:55'))
  it('missing / bad', () => {
    expect(formatEpoch(null, 'utc')).toBe('—')
    expect(formatEpoch('nope', 'utc')).toBe('—')
  })
})

describe('targetStatus', () => {
  const base = { selected: 5, verdict: ok, sending: false, ackRefusal: null }
  it('nothing selected', () => expect(targetStatus({ ...base, selected: null })).toMatchObject({ text: 'Select a target', canTrack: false }))
  it('ready', () => expect(targetStatus(base)).toMatchObject({ text: 'Ready', canTrack: true }))
  it('note', () => expect(targetStatus({ ...base, verdict: { ...ok, notes: ['Near zenith'] } })).toMatchObject({ text: 'Near zenith', canTrack: true }))
  it('refused', () => expect(targetStatus({ ...base, verdict: { ...ok, enabled: false, reason: 'Below horizon' } })).toMatchObject({ text: 'Below horizon', canTrack: false }))
  it('pending', () => expect(targetStatus({ ...base, verdict: null })).toMatchObject({ canTrack: false }))
  it('sending', () => expect(targetStatus({ ...base, sending: true }).canTrack).toBe(false))
  it('ack refusal wins, danger', () => expect(targetStatus({ ...base, ackRefusal: 'No' })).toMatchObject({ text: 'No', tone: 'danger' }))
})

describe('moveSelection', () => {
  const n = [9, 8, 7]
  it('from none', () => {
    expect(moveSelection(n, null, 1)).toBe(9)
    expect(moveSelection(n, null, -1)).toBe(7)
  })
  it('moves and clamps', () => {
    expect(moveSelection(n, 9, 1)).toBe(8)
    expect(moveSelection(n, 7, 1)).toBe(7)
    expect(moveSelection(n, 9, -1)).toBe(9)
  })
  it('unknown current / empty', () => {
    expect(moveSelection(n, 1, 1)).toBe(9)
    expect(moveSelection([], null, 1)).toBeNull()
  })
})

describe('exactFirst', () => {
  const row = (norad: number, name: string, cospar: string | null = null): SatRow =>
    ({ norad, name, cospar, country: null, object_type: null, orbit_class: null, tle_epoch: null, tle_age_days: null })
  const rows = [row(100909, 'TERRA NOVA'), row(25994, 'TERRA'), row(5, 'X', '1999-068A')]
  it('puts exact name / NORAD / COSPAR matches on top, keeping the order otherwise', () => {
    expect(exactFirst(rows, ' terra ').map(r => r.norad)).toEqual([25994, 100909, 5])
    expect(exactFirst(rows, '5').map(r => r.norad)).toEqual([5, 100909, 25994])
    expect(exactFirst(rows, '1999-068a').map(r => r.norad)).toEqual([5, 100909, 25994])
    expect(exactFirst(rows, '').map(r => r.norad)).toEqual([100909, 25994, 5])
    expect(isExactMatch(rows[0]!, 'TERRA')).toBe(false)
  })
})
