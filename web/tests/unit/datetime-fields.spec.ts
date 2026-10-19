// Ported from ssa-toolkit web/tests/unit/datetime-fields.spec.ts @84f2ded
import { describe, it, expect } from 'vitest'
import {
  MIN_YEAR, maxYear, daysInMonth, stepField, padSec, secDecimals, pad2,
  clampNum, ymd, calLo, calHi, dateFieldsValid, readFields, utcDateToFields,
  fieldsToUtcDate, clampField, clampDayToMonth,
} from '../../app/utils/datetime-fields'

describe('daysInMonth — the century non-leap + ordinary leap/non-leap pairs', () => {
  it('Feb 1900 = 28 (century non-leap — the classic Gregorian trap)', () => {
    expect(daysInMonth(1900, 1)).toBe(28)
  })
  it('Feb 2000 = 29 (century leap — divisible by 400)', () => {
    expect(daysInMonth(2000, 1)).toBe(29)
  })
  it('Feb 2024 = 29 (ordinary leap)', () => {
    expect(daysInMonth(2024, 1)).toBe(29)
  })
  it('Feb 2025 = 28 (ordinary non-leap)', () => {
    expect(daysInMonth(2025, 1)).toBe(28)
  })
})

describe('stepField — day-clamp-to-month rule', () => {
  it('year-step from 2024-02-29 clamps to 2025-02-28 (never rolls to March)', () => {
    const f = { y: 2024, mo: 2, d: 29, h: 0, mi: 0, s: 0 }
    const next = stepField(f, 'y', 1)!
    expect(next).toMatchObject({ y: 2025, mo: 2, d: 28 })
  })
  it('month-step from 2024-01-31 lands on 2024-02-29 (leap Feb, not March)', () => {
    const f = { y: 2024, mo: 1, d: 31, h: 0, mi: 0, s: 0 }
    const next = stepField(f, 'mo', 1)!
    expect(next).toMatchObject({ y: 2024, mo: 2, d: 29 })
  })
  it('month-step wraps December -> next January', () => {
    const f = { y: 2024, mo: 12, d: 15, h: 0, mi: 0, s: 0 }
    const next = stepField(f, 'mo', 1)!
    expect(next).toMatchObject({ y: 2025, mo: 1, d: 15 })
  })
  it('year-step below MIN_YEAR is refused (null, no-op)', () => {
    const f = { y: MIN_YEAR, mo: 1, d: 1, h: 0, mi: 0, s: 0 }
    expect(stepField(f, 'y', -1)).toBeNull()
  })
  it('year-step above maxYear() is refused (null, no-op)', () => {
    const f = { y: maxYear(), mo: 1, d: 1, h: 0, mi: 0, s: 0 }
    expect(stepField(f, 'y', 1)).toBeNull()
  })
  it('day carry rolls Jan 31 + 1d -> Feb 1 (native UTC Date carry)', () => {
    const f = { y: 2024, mo: 1, d: 31, h: 0, mi: 0, s: 0 }
    const next = stepField(f, 'd', 1)!
    expect(next).toMatchObject({ y: 2024, mo: 2, d: 1 })
  })
  it('hour carry rolls 23h + 1h -> 00h, day + 1', () => {
    const f = { y: 2024, mo: 6, d: 15, h: 23, mi: 0, s: 0 }
    const next = stepField(f, 'h', 1)!
    expect(next).toMatchObject({ d: 16, h: 0 })
  })
  it('seconds step uses secStep (decoupled increment, e.g. 0.001)', () => {
    const f = { y: 2024, mo: 6, d: 15, h: 0, mi: 0, s: 0 }
    const next = stepField(f, 's', 1, 0.001)!
    expect(next.s).toBeCloseTo(0.001, 6)
  })
})

describe('maxYear', () => {
  it('=== new Date().getUTCFullYear() + 100, recomputed live', () => {
    expect(maxYear()).toBe(new Date().getUTCFullYear() + 100)
  })
})

describe('padSec / secDecimals / pad2', () => {
  it("padSec(3.5, 3) === '03.500'", () => {
    expect(padSec(3.5, 3)).toBe('03.500')
  })
  it("padSec(9, 0) === '09' (whole seconds)", () => {
    expect(padSec(9, 0)).toBe('09')
  })
  it('secDecimals(1) === 0', () => {
    expect(secDecimals(1)).toBe(0)
  })
  it('secDecimals(0.001) === 3', () => {
    expect(secDecimals(0.001)).toBe(3)
  })
  it("pad2(7) === '07'", () => {
    expect(pad2(7)).toBe('07')
  })
})

describe('ymd / calLo / calHi — ordering across a year boundary', () => {
  it('Dec 31 of year N sorts before Jan 1 of year N+1', () => {
    expect(ymd(2024, 12, 31)).toBeLessThan(ymd(2025, 1, 1))
  })
  it('calLo() === MIN_YEAR-01-01, calHi() === maxYear()-12-31', () => {
    expect(calLo()).toBe(ymd(MIN_YEAR, 1, 1))
    expect(calHi()).toBe(ymd(maxYear(), 12, 31))
  })
})

describe('clampNum / dateFieldsValid / readFields', () => {
  it('clampNum clamps both directions', () => {
    expect(clampNum(5, 0, 10)).toBe(5)
    expect(clampNum(-5, 0, 10)).toBe(0)
    expect(clampNum(15, 0, 10)).toBe(10)
  })
  it('dateFieldsValid is false when any of y/mo/d is NaN', () => {
    expect(dateFieldsValid({ y: 2024, mo: 6, d: 15 })).toBe(true)
    expect(dateFieldsValid({ y: NaN, mo: 6, d: 15 })).toBe(false)
  })
  it('readFields parses the six box strings, defaulting h/mi/s to 0', () => {
    const f = readFields('2024', '06', '15', '', '', '')
    expect(f).toMatchObject({ y: 2024, mo: 6, d: 15, h: 0, mi: 0, s: 0 })
  })
})

describe('utcDateToFields / fieldsToUtcDate — round trip', () => {
  it('round-trips a UTC instant exactly', () => {
    const dt = new Date(Date.UTC(2026, 8, 10, 7, 30, 12, 500))
    const f = utcDateToFields(dt)
    expect(f).toMatchObject({ y: 2026, mo: 9, d: 10, h: 7, mi: 30 })
    expect(f.s).toBeCloseTo(12.5, 6)
    expect(fieldsToUtcDate(f).getTime()).toBe(dt.getTime())
  })
})

describe('clampField / clampDayToMonth — blur clamp, no carry', () => {
  it('clamps seconds to [0, 59.999]', () => {
    const f = { y: 2024, mo: 6, d: 15, h: 0, mi: 0, s: 0 }
    expect(clampField('s', 70, f)).toBe(59.999)
  })
  it('clamps day to the CURRENT month length (leap-aware), no carry into month', () => {
    const f = { y: 2025, mo: 2, d: 15, h: 0, mi: 0, s: 0 }
    expect(clampField('d', 40, f)).toBe(28)
  })
  it('clampDayToMonth re-clamps Feb 30 -> 28/29 after a month/year edit', () => {
    expect(clampDayToMonth({ y: 2025, mo: 2, d: 30, h: 0, mi: 0, s: 0 })).toBe(28)
    expect(clampDayToMonth({ y: 2024, mo: 2, d: 30, h: 0, mi: 0, s: 0 })).toBe(29)
  })
  it('clampDayToMonth re-clamps Apr 31 -> 30', () => {
    expect(clampDayToMonth({ y: 2025, mo: 4, d: 31, h: 0, mi: 0, s: 0 })).toBe(30)
  })
})
