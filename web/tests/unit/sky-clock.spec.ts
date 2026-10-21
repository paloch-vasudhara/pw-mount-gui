import { describe, expect, it } from 'vitest'
import { formatClock } from '../../app/utils/sky-clock'

const MS = Date.UTC(2026, 9, 8, 13, 42, 52)

describe('formatClock', () => {
  it('UTC', () => {
    expect(formatClock(MS, 'utc', 'Asia/Bangkok')).toEqual({ date: '2026-10-08', hh: '13', mm: '42', ss: '52' })
  })
  it('local uses the site timezone', () => {
    expect(formatClock(MS, 'local', 'Asia/Bangkok')).toEqual({ date: '2026-10-08', hh: '20', mm: '42', ss: '52' })
  })
  it('date rolls over with the zone, midnight is 00', () => {
    const t = Date.UTC(2026, 9, 8, 17, 0, 0)
    expect(formatClock(t, 'local', 'Asia/Bangkok')).toEqual({ date: '2026-10-09', hh: '00', mm: '00', ss: '00' })
  })
  it('null or unknown timezone falls back to the PC zone', () => {
    expect(formatClock(MS, 'local', null)).toEqual(formatClock(MS, 'local', undefined))
    expect(() => formatClock(MS, 'local', 'Mars/Olympus')).not.toThrow()
  })
})
