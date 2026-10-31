import { describe, expect, it } from 'vitest'
import { trackPanel } from '../../app/utils/track-panel'
import { STALE_TLE_DAYS, staleTleDays } from '../../app/utils/tle-stale'

const A = { norad: 1, name: 'ALPHA', phase: 'tracking', t_end: 200 }
const ENDED = { phase: null, ended: { norad: 1, reason: 'Stopped' } }

describe('trackPanel (A1e)', () => {
  it('nothing selected, not tracking: empty', () => {
    expect(trackPanel(null, null, 0)).toEqual({ main: null, strip: null, empty: true })
    expect(trackPanel(null, ENDED, 0).empty).toBe(true)
  })
  it('B selected, not tracking: main B', () => {
    expect(trackPanel(2, null, 0)).toEqual({ main: { norad: 2, pinned: false }, strip: null, empty: false })
  })
  it('tracking A, nothing or A selected: A pinned', () => {
    for (const s of [null, 1]) {
      expect(trackPanel(s, A, 0)).toEqual({ main: { norad: 1, pinned: true }, strip: null, empty: false })
    }
  })
  it('tracking A, B selected: main B + strip A', () => {
    const v = trackPanel(2, A, 140)
    expect(v.main).toEqual({ norad: 2, pinned: false })
    expect(v.strip).toEqual({ norad: 1, name: 'ALPHA', line: 'Tracking — ends in 01:00' })
  })
  it('B tracked replaces A: no strip', () => {
    expect(trackPanel(2, { ...A, norad: 2, name: 'B' }, 0).strip).toBeNull()
  })
  it('track ends: A stays when selected, dropped when B selected', () => {
    expect(trackPanel(1, ENDED, 0).main).toEqual({ norad: 1, pinned: false })
    expect(trackPanel(2, ENDED, 0)).toEqual({ main: { norad: 2, pinned: false }, strip: null, empty: false })
    expect(trackPanel(null, ENDED, 0).empty).toBe(true)
  })
  it('strip falls back to the NORAD without a name', () => {
    expect(trackPanel(2, { ...A, name: null }, 0).strip?.name).toBe('NORAD 1')
  })
})

describe('stale TLE', () => {
  it('threshold', () => {
    expect(STALE_TLE_DAYS).toBe(30)
    expect(staleTleDays(30)).toBeNull()
    expect(staleTleDays(30.1)).toBe(30)
    expect(staleTleDays(45.9)).toBe(45)
    expect(staleTleDays(null)).toBeNull()
    expect(staleTleDays(Number.NaN)).toBeNull()
  })
})
