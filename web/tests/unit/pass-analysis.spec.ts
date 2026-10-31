import { describe, expect, it } from 'vitest'
import {
  buildPassRequest, checkWindow, DEFAULT_PASS_SORT, defaultEnd, defaultStart, fmtDeg, fmtDuration, fmtRise, listBlockReason,
  clipToWindow, nextPassSort, passErrorText, siteFromSnapshot, sortPasses, trackEligible, type PassRow,
} from '../../app/utils/pass-analysis'

const T0 = Date.UTC(2026, 9, 12, 0, 0, 0)
const row = (o: Partial<PassRow> = {}): PassRow => ({
  t_rise: 1000, t_set: 1600, rise_az: 10, set_az: 200, t_peak: 1300, max_el: 40, duration_s: 600, visible_s: 300, ...o,
})

describe('window', () => {
  const s = new Date(T0)
  it('defaults: Start to the minute, End = Start + 24 h', () => {
    const st = defaultStart(T0 + 59_999)
    expect(st.getTime()).toBe(T0)
    expect(defaultEnd(st).getTime()).toBe(T0 + 86_400_000)
  })
  it('valid, End <= Start, > 7 days, blanks', () => {
    expect(checkWindow(s, new Date(T0 + 3600e3)).reason).toBeNull()
    expect(checkWindow(s, new Date(T0 + 7 * 86400e3)).reason).toBeNull()
    expect(checkWindow(s, s)).toMatchObject({ endInvalid: true, startInvalid: false, reason: 'End must be after Start' })
    expect(checkWindow(s, new Date(T0 - 1000)).endInvalid).toBe(true)
    expect(checkWindow(s, new Date(T0 + 7 * 86400e3 + 1000))).toMatchObject({ endInvalid: true, reason: 'Window longer than 7 days' })
    expect(checkWindow(null, s).startInvalid).toBe(true)
    expect(checkWindow(s, null).endInvalid).toBe(true)
  })
  it('LIST PASSES block reason order: satellite, site, window', () => {
    const site = { lat: 1, lon: 2, alt_m: 3 }
    const e = new Date(T0 + 1000)
    expect(listBlockReason({ norad: null, site, limEl: 10, start: s, end: e })).toBe('Select a satellite')
    expect(listBlockReason({ norad: 1, site: null, limEl: 10, start: s, end: e })).toBe('Station location not available')
    expect(listBlockReason({ norad: 1, site, limEl: 10, start: s, end: s })).toBe('End must be after Start')
    expect(listBlockReason({ norad: 1, site, limEl: 10, start: s, end: e })).toBeNull()
  })
})

describe('request / site / errors', () => {
  it('builds the body (whole seconds, no sun_alt_max)', () => {
    const b = buildPassRequest({
      norad: 25994, site: { lat: 1.5, lon: -2.5, alt_m: 30 }, limEl: 12, start: new Date(T0 + 400), end: new Date(T0 + 86_400_900), mode: 'los',
    })
    expect(b).toEqual({
      norad: 25994, site: { lat: 1.5, lon: -2.5, alt_m: 30 }, lim_el: 12, start_unix: T0 / 1000, end_unix: T0 / 1000 + 86400, mode: 'los',
    })
    expect('sun_alt_max' in b).toBe(false)
  })
  it('site from the gateway snapshot', () => {
    expect(siteFromSnapshot({ site: { lat: 1, lon: 2, height_m: 30 }, limits: { lim_el_deg: 10 } })).toEqual({ site: { lat: 1, lon: 2, alt_m: 30 }, limEl: 10 })
    expect(siteFromSnapshot({ site: { lat: 1, lon: 2 }, limits: { lim_el_deg: 10 } })?.site.alt_m).toBe(0)
    expect(siteFromSnapshot({ site: { lat: 1, lon: 2 } })).toBeNull()
    expect(siteFromSnapshot(null)).toBeNull()
  })
  it('error texts', () => {
    expect(passErrorText(422, 'Window longer than 7 days')).toBe('Window longer than 7 days')
    expect(passErrorText(503, 'Pass computation timed out')).toBe('Pass computation timed out')
    expect(passErrorText(503, 'catalogue unavailable')).toBe('Satellite data unavailable')
    expect(passErrorText(0, undefined)).toBe('Satellite data unavailable')
    expect(passErrorText(404, 'x')).toBe('Satellite not found')
  })
})

describe('formats', () => {
  it('duration: mm ss, hh mm from 1 h', () => {
    expect(fmtDuration(668)).toBe('11ᴹ 08ˢ')
    expect(fmtDuration(280)).toBe('04ᴹ 40ˢ')
    expect(fmtDuration(0)).toBe('00ᴹ 00ˢ')
    expect(fmtDuration(3599.6)).toBe('01ᴴ 00ᴹ')
    expect(fmtDuration(3600)).toBe('01ᴴ 00ᴹ')
    expect(fmtDuration(5 * 3600 + 59 * 60 + 59)).toBe('05ᴴ 59ᴹ')
    expect(fmtDuration(null)).toBe('—')
  })
  it('rises and sets: full UTC date and time', () => {
    const r = Date.UTC(2026, 9, 12, 13, 23, 14) / 1000
    expect(fmtRise(r)).toBe('2026-10-12 13:23:14')
    expect(fmtRise(null)).toBe('—')
  })
  it('degrees', () => {
    expect(fmtDeg(155.64)).toBe('155.6°')
    expect(fmtDeg(null)).toBe('—')
  })
})

describe('sorting', () => {
  const a = row({ t_rise: 300, t_set: 400, max_el: 10, rise_az: 90, set_az: 5, duration_s: 100, visible_s: 900, t_peak: 350 })
  const b = row({ t_rise: 100, t_set: 200, max_el: 70, rise_az: 10, set_az: 300, duration_s: 500, visible_s: 20, t_peak: 150 })
  const geo = row({ t_rise: null, t_set: null, rise_az: null, set_az: null, max_el: 40, duration_s: 7200, visible_s: 0, t_peak: 50 })
  const ids = (rs: PassRow[]) => rs.map(r => r.t_peak)
  it('default: rises ascending (null rise first)', () => {
    expect(ids(sortPasses([a, b, geo], DEFAULT_PASS_SORT, 'los'))).toEqual([50, 150, 350])
  })
  it('descending flips; sets put a null last when ascending', () => {
    expect(ids(sortPasses([a, b, geo], { key: 'rise', dir: 'desc' }, 'los'))).toEqual([350, 150, 50])
    expect(ids(sortPasses([a, b, geo], { key: 'set', dir: 'asc' }, 'los'))).toEqual([150, 350, 50])
  })
  it('every other column', () => {
    expect(ids(sortPasses([a, b], { key: 'max_el', dir: 'desc' }, 'los'))).toEqual([150, 350])
    expect(ids(sortPasses([a, b], { key: 'rise_az', dir: 'asc' }, 'los'))).toEqual([150, 350])
    expect(ids(sortPasses([a, b], { key: 'set_az', dir: 'asc' }, 'los'))).toEqual([350, 150])
  })
  it('duration follows the mode (visible_s optical, duration_s LOS)', () => {
    expect(ids(sortPasses([a, b], { key: 'dur', dir: 'asc' }, 'optical'))).toEqual([150, 350])
    expect(ids(sortPasses([a, b], { key: 'dur', dir: 'asc' }, 'los'))).toEqual([350, 150])
  })
  it('header click: same column flips, a new one starts ascending; input untouched', () => {
    expect(nextPassSort(DEFAULT_PASS_SORT, 'rise')).toEqual({ key: 'rise', dir: 'desc' })
    expect(nextPassSort({ key: 'rise', dir: 'desc' }, 'max_el')).toEqual({ key: 'max_el', dir: 'asc' })
    const input = [a, b]
    sortPasses(input, DEFAULT_PASS_SORT, 'los')
    expect(input[0]).toBe(a)
  })
})

describe('[Track] eligibility', () => {
  const now = 1000
  it('in progress', () => expect(trackEligible(row({ t_rise: 900, t_set: 1500 }), now)).toBe(true))
  it('rising within 5 min (edge inclusive), not beyond', () => {
    expect(trackEligible(row({ t_rise: now + 300, t_set: now + 900 }), now)).toBe(true)
    expect(trackEligible(row({ t_rise: now + 301, t_set: now + 900 }), now)).toBe(false)
  })
  it('ended: none (also at the set instant)', () => {
    expect(trackEligible(row({ t_rise: 100, t_set: 999 }), now)).toBe(false)
    expect(trackEligible(row({ t_rise: 100, t_set: now }), now)).toBe(false)
  })
  it('null rise: in progress unless ended; null set never ends', () => {
    expect(trackEligible(row({ t_rise: null, t_set: null }), now)).toBe(true)
    expect(trackEligible(row({ t_rise: null, t_set: 500 }), now)).toBe(false)
    expect(trackEligible(row({ t_rise: 900, t_set: null }), now)).toBe(true)
  })
})

describe('clipToWindow', () => {
  const p: PassRow = { t_rise: 90, t_set: 210, rise_az: 1, set_az: 2, t_peak: 150, max_el: 30, duration_s: 100 }
  it('dashes a rise before the start and a set after the end (time and azimuth)', () => {
    expect(clipToWindow(p, { start: 100, end: 200 })).toMatchObject({ t_rise: null, rise_az: null, t_set: null, set_az: null })
    expect(clipToWindow(p, { start: 50, end: 300 })).toBe(p)
    expect(clipToWindow(p, { start: 50, end: 200 })).toMatchObject({ t_rise: 90, rise_az: 1, t_set: null, set_az: null })
    expect(clipToWindow(p, null)).toBe(p)
  })
  it('sorting uses the shown (dashed) values and returns the original rows', () => {
    const q: PassRow = { ...p, t_rise: 120, t_peak: 160 }
    const out = sortPasses([q, p], { key: 'rise', dir: 'asc' }, 'los', { start: 100, end: 300 })
    expect(out).toEqual([p, q]) // the dashed rise sorts first
    expect(out[0]!.t_rise).toBe(90)
  })
})
