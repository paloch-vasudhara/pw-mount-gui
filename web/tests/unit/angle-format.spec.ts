// Ported from ssa-toolkit web/tests/unit/angle-format.spec.ts @84f2ded
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import {
  FORMATS_FOR, FORMAT_LABEL,
  decToSplit, raDisplayHd, raDisplayDd, ddDisplay,
  splitToDecimal, convert,
} from '../../app/utils/angle-format'

describe('rounding, verbatim', () => {
  it('raDisplayHd', () => {
    expect(raDisplayHd(6.5)).toBe(6.5)
    expect(raDisplayHd(0)).toBe('')
    expect(raDisplayHd(NaN)).toBe('')
  })
  it('raDisplayDd', () => {
    expect(raDisplayDd(6.5)).toBe(97.5)
    expect(raDisplayDd(1 / 3)).toBe(5)
  })
  it('ddDisplay', () => {
    expect(ddDisplay(0)).toBe('')
    expect(ddDisplay(13.756789)).toBe(13.7568)
  })
})

describe('decToSplit', () => {
  it('positive', () => {
    expect(decToSplit(13.75)).toEqual({ d: 13, m: 45, s: 0 })
  })
  it('negative — sign on d only, m/s positive', () => {
    expect(decToSplit(-13.75)).toEqual({ d: -13, m: 45, s: 0 })
  })
  it('zero — no -0', () => {
    const sp = decToSplit(0)
    expect(sp).toEqual({ d: 0, m: 0, s: 0 })
    expect(Object.is(sp.d, -0)).toBe(false)
  })
  it('60.00-seconds case is NOT re-normalised (legacy behaviour, not an accident)', () => {
    // Chosen so (abs - d)*60 lands just under a minute boundary and the raw
    // seconds value rounds UP to 60.00 at 2dp without carrying into minutes.
    const dec = 1 + 44 / 60 + 59.996 / 3600
    const sp = decToSplit(dec)
    expect(sp.s).toBe(60)
    expect(sp.m).toBe(44)
  })
})

describe('round trips', () => {
  const GRAIN = 1 / 360000 // 2dp-seconds grain in degrees

  it.each([13.75, -118.3, 0.0001, 89.9999])('dd -> dms -> dd identity for %f', (v) => {
    const back = splitToDecimal(decToSplit(v))
    expect(Math.abs(back - v)).toBeLessThan(GRAIN)
    if (v < 0) expect(back).toBeLessThan(0)
  })

  it.each([6.5, 23.9999])('hd -> hms -> dd -> hd stays within grain and never wraps past 24 for %f', (v) => {
    const sp = decToSplit(v)
    const hoursFromHms = splitToDecimal(sp)
    const deg = hoursFromHms * 15
    const hoursBack = deg / 15
    expect(Math.abs(hoursBack - v)).toBeLessThan(GRAIN)
    expect(hoursBack).toBeLessThanOrEqual(24)
  })
})

describe('cardinal / sign preservation', () => {
  it('latlon dms -> dd stays negative', () => {
    const toDms = convert(-45.5, 'latlon', 'dms')
    const back = convert(toDms, 'latlon', 'dd')
    expect(back).toBeLessThan(0)
  })
  it('ra never returns negative for a positive input', () => {
    for (const fmt of FORMATS_FOR.ra) {
      expect(convert(6.5, 'ra', fmt)).toBeGreaterThanOrEqual(0)
    }
  })
})

describe('empty / zero', () => {
  it('convert(0, kind, fmt) === 0 for every kind x format pair', () => {
    for (const kind of Object.keys(FORMATS_FOR) as (keyof typeof FORMATS_FOR)[]) {
      for (const fmt of FORMATS_FOR[kind]) {
        expect(convert(0, kind, fmt)).toBe(0)
      }
    }
  })
  it('NaN / Infinity -> 0', () => {
    expect(convert(NaN, 'latlon', 'dd')).toBe(0)
    expect(convert(Infinity, 'ra', 'hd')).toBe(0)
    expect(convert(-Infinity, 'angle', 'dms')).toBe(0)
  })
})

describe('tables', () => {
  it('FORMATS_FOR', () => {
    expect(FORMATS_FOR.latlon).toEqual(['dd', 'dms'])
    expect(FORMATS_FOR.angle).toEqual(['dd', 'dms'])
    expect(FORMATS_FOR.ra).toEqual(['hd', 'hms', 'dd'])
  })
  it('FORMAT_LABEL matches legacy option text', () => {
    expect(FORMAT_LABEL).toEqual({ dd: 'DD', dms: 'DMS', hd: 'HD', hms: 'HMS' })
  })
})
