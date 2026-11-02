// IOD helpers (T5.3b): row validation / ranges, pair clearing, readiness, overwrite rules, request, formats, sorting, memo.
import { beforeEach, describe, expect, it } from 'vitest'
import {
  applyIodTransform, astrometryFailed, astrometryPending, astrometrySolved, buildIodRequest, buildIodTransform, checkDraft, draftFromRow, confirmText,
  doneText, draftToRow, editDraft, emptyRow, emptyRows, fmtAngle, fmtEpoch, fmtStateVector, fmtTle, iodErrorText, iodReadiness, isDim,
  menuState, needsConfirm, newDraft, parseAngleText, parseIodResponse, rowDisplay, rowValid, sanitizeAngleText, sortedSlots,
  type IodDraft, type IodRow,
} from '../../app/utils/iod'
import { getIodMemo, resetIodMemo, setIodMemo } from '../../app/utils/iod-memo'

const valid = (t: number, ra = 10, dec = 20): IodRow => ({ state: 'valid', source: 'Manual', t, az: 100, el: 40, ra, dec, bright: 'radec' })
const SITE = { lat: 10, lon: 20, alt_m: 30 }
const d = (o: Partial<IodDraft>): IodDraft => ({ ...newDraft(0), ...o })

describe('angle text', () => {
  it('sanitizes to a signed decimal with at most 6 decimals', () => {
    expect(sanitizeAngleText('12a.34567')).toBe('12.34567')
    expect(sanitizeAngleText('1.12345678')).toBe('1.123456')
    expect(sanitizeAngleText('-1-2.5.5')).toBe('-12.55')
    expect(sanitizeAngleText('1,5')).toBe('1.5')
  })
  it('parses; empty / partial text is null', () => {
    expect(parseAngleText('359.9999')).toBe(359.9999)
    expect(parseAngleText('-0.5')).toBe(-0.5)
    expect(parseAngleText('')).toBeNull()
    expect(parseAngleText('-')).toBeNull()
    expect(parseAngleText('.')).toBeNull()
  })
})

describe('draft: pair clearing, validation, ranges', () => {
  it('typing in one pair clears the other', () => {
    let x = editDraft(newDraft(0), 'ra', '12.5')
    x = editDraft(x, 'dec', '-3')
    expect(x).toMatchObject({ ra: '12.5', dec: '-3', az: '', el: '' })
    x = editDraft(x, 'az', '100')
    expect(x).toMatchObject({ az: '100', ra: '', dec: '' })
  })
  it('blank draft, incomplete pair, out of range', () => {
    expect(checkDraft(newDraft(0)).kind).toBe('blank')
    expect(checkDraft(d({ az: '10' }))).toMatchObject({ kind: 'invalid', bad: ['el'] })
    expect(checkDraft(d({ az: '10', el: '-1' }))).toMatchObject({ kind: 'invalid', bad: ['el'] })
    expect(checkDraft(d({ az: '361', el: '10' }))).toMatchObject({ kind: 'invalid', bad: ['az'] })
    expect(checkDraft(d({ ra: '360.1', dec: '10' }))).toMatchObject({ kind: 'invalid', bad: ['ra'] })
    expect(checkDraft(d({ ra: '10', dec: '90.5' }))).toMatchObject({ kind: 'invalid', bad: ['dec'] })
    expect(checkDraft(d({ ra: '10', dec: '-' }))).toMatchObject({ kind: 'invalid', bad: ['dec'] })
    expect(checkDraft(d({ t: null, ra: '10', dec: '0' }))).toMatchObject({ kind: 'invalid', bad: ['t'] })
  })
  it('limits are accepted', () => {
    expect(checkDraft(d({ az: '0', el: '90' })).kind).toBe('ok')
    expect(checkDraft(d({ az: '360', el: '0' })).kind).toBe('ok')
    expect(checkDraft(d({ ra: '0', dec: '-90' })).kind).toBe('ok')
  })
  it('commits a row: the typed pair is bright, 360 wraps to 0, epoch in whole seconds', () => {
    const r = draftToRow(d({ t: new Date(1_700_000_000_500), az: '360', el: '45.1234' }))!
    expect(r).toMatchObject({ state: 'partial', source: 'Manual', t: 1_700_000_000, az: 0, el: 45.1234, ra: null, bright: 'altaz' })
    expect(draftToRow(d({ az: '10' }))).toBeNull()
    expect(isDim(r, 'az')).toBe(false)
    expect(isDim(r, 'ra')).toBe(true)
  })
})

describe('6-decimal precision and Edit Entry draft', () => {
  it('stores the typed value exactly and prefills the bright pair at full precision', () => {
    const r = draftToRow(d({ az: '123.456789', el: '45.123456' }))!
    expect(r).toMatchObject({ az: 123.456789, el: 45.123456 })
    const dr = draftFromRow({ ...r, state: 'valid', ra: 90.1234567, dec: -10.5 })!
    expect(dr).toMatchObject({ az: '123.456789', el: '45.123456', ra: '', dec: '' })
    expect(draftFromRow(astrometrySolved(100, 12.3456789012, -3.2109876543))).toMatchObject({ ra: '12.3456789012', dec: '-3.2109876543', az: '' })
    expect(draftFromRow(emptyRow())).toBeNull()
    expect(draftFromRow(astrometryFailed(100))).toBeNull()
  })
  it('Edit Entry is enabled on valid / partial rows only', () => {
    expect(menuState(astrometrySolved(100, 1, 2)).edit).toBe(true)
    expect(menuState(emptyRow()).edit).toBe(false)
    expect(menuState(astrometryFailed(100)).edit).toBe(false)
    expect(menuState(astrometryPending(100)).edit).toBe(false)
  })
  it('the IOD request keeps full precision', () => {
    const rows = [1, 2, 3].map(i => astrometrySolved(100 * i, 10.123456789 * i, 5.987654321))
    expect(buildIodRequest({ lat: 1, lon: 2, alt_m: 3 }, rows)!.obs[0]).toEqual({ t_unix: 100, ra_deg: 10.123456789, dec_deg: 5.987654321 })
  })
})

describe('H4 request / result', () => {
  it('builds the request at the ROW epoch; RA in hours', () => {
    const r = draftToRow(d({ t: new Date(Date.UTC(2026, 9, 3, 15, 0, 30)), ra: '180', dec: '10' }))!
    expect(buildIodTransform(r, SITE)).toEqual({ dir: 'radec_to_altaz', t_utc: '2026-10-03T15:00:30.000Z', values: { ra_h: 12, dec_deg: 10 }, site: { lat: 10, lon: 20, height_m: 30 } })
    const a = draftToRow(d({ az: '100', el: '40' }))!
    expect(buildIodTransform(a, SITE)).toMatchObject({ dir: 'altaz_to_radec', values: { alt: 40, az: 100 } })
    expect(buildIodTransform(emptyRow(), SITE)).toBeNull()
  })
  it('fills the computed pair (RA hours to degrees) and validates the row', () => {
    const a = draftToRow(d({ az: '100', el: '40' }))!
    expect(rowValid(a)).toBe(false)
    const v = applyIodTransform(a, { ra_h: 6, dec_deg: -12.5 })!
    expect(v).toMatchObject({ state: 'valid', ra: 90, dec: -12.5 })
    expect(rowValid(v)).toBe(true)
    const b = draftToRow(d({ ra: '10', dec: '5' }))!
    expect(applyIodTransform(b, { alt: 30, az: -10 })).toMatchObject({ state: 'valid', el: 30, az: 350 })
    expect(applyIodTransform(a, { nope: 1 })).toBeNull()
    expect(applyIodTransform(a, null)).toBeNull()
  })
})

describe('menu / overwrite rules', () => {
  it('Remove is inactive on an empty row; Add Astrometry is offered (T5.2d)', () => {
    expect(menuState(emptyRow())).toEqual({ addManual: true, addAstrometry: true, edit: false, remove: false })
    expect(menuState(valid(1)).remove).toBe(true)
  })
  it('only an occupied VALID row asks', () => {
    expect(needsConfirm(valid(1))).toBe(true)
    expect(needsConfirm(emptyRow())).toBe(false)
    expect(needsConfirm(astrometryFailed(1))).toBe(false)
    expect(needsConfirm(astrometryPending(1))).toBe(false)
    expect(needsConfirm(draftToRow(d({ az: '1', el: '1' }))!)).toBe(false)
    expect(confirmText('replace', 1)).toBe('Replace row 2?')
    expect(confirmText('remove', 2)).toBe('Remove row 3?')
  })
})

describe('astrometry rows (T5.2 hook)', () => {
  it('pending / failed render their text; a solved row waits for az / el', () => {
    expect(rowDisplay(astrometryPending(5))).toEqual({ kind: 'status', text: 'Astrometry pending' })
    expect(rowDisplay(astrometryFailed(5))).toEqual({ kind: 'status', text: 'Astrometry failed' })
    expect(rowDisplay(emptyRow())).toEqual({ kind: 'empty' })
    const s = astrometrySolved(5, 10, 20)
    expect(s).toMatchObject({ source: 'Astrometry', state: 'partial', bright: 'radec' })
    expect(rowDisplay(s)).toEqual({ kind: 'values' })
    expect(rowValid(applyIodTransform(s, { alt: 1, az: 2 })!)).toBe(true)
  })
})

describe('readiness', () => {
  it('needs 3 valid rows with distinct epochs; the reason says what is missing', () => {
    const rows = emptyRows()
    expect(iodReadiness(rows)).toEqual({ ready: false, reason: 'Three observations are needed (0 of 3)' })
    rows[0] = valid(100); rows[1] = valid(200)
    expect(iodReadiness(rows).reason).toBe('Three observations are needed (2 of 3)')
    rows[2] = valid(200)
    expect(iodReadiness(rows)).toEqual({ ready: false, reason: 'The observations need different epochs' })
    rows[2] = valid(300)
    expect(iodReadiness(rows)).toEqual({ ready: true, reason: null })
    expect(iodReadiness(rows, false).reason).toBe('Station location not available')
  })
  it('pending / failed / unconverted rows block with their row number', () => {
    const rows = [valid(1), astrometryPending(2), valid(3)]
    expect(iodReadiness(rows).reason).toBe('Row 2: Astrometry is still running')
    rows[1] = astrometryFailed(2)
    expect(iodReadiness(rows).reason).toContain('Row 2: Astrometry failed')
    rows[1] = draftToRow(d({ az: '1', el: '1' }))!
    expect(iodReadiness(rows).reason).toBe('Row 2 has no R.A. / Dec. yet')
  })
})

describe('request', () => {
  it('sends the site and the three observations in epoch order', () => {
    const rows = [valid(300, 3, 3), valid(100, 1, 1), valid(200, 2, 2)]
    expect(buildIodRequest({ lat: 1, lon: 2, alt_m: 3 }, rows)).toEqual({
      site: { lat: 1, lon: 2, alt_m: 3 },
      obs: [{ t_unix: 100, ra_deg: 1, dec_deg: 1 }, { t_unix: 200, ra_deg: 2, dec_deg: 2 }, { t_unix: 300, ra_deg: 3, dec_deg: 3 }],
    })
    expect(buildIodRequest({ lat: 1, lon: 2, alt_m: 3 }, [valid(1), valid(2), emptyRow()])).toBeNull()
  })
  it('error text: a 422 shows its detail as-is, anything else is "unavailable"', () => {
    expect(iodErrorText(422, 'No orbit fits these observations (check the epochs and angles)')).toBe('No orbit fits these observations (check the epochs and angles)')
    expect(iodErrorText(422, null)).toBe('The observations were rejected')
    expect(iodErrorText(503, 'x')).toBe('Analysis service unavailable')
    expect(iodErrorText(0, null)).toBe('Analysis service unavailable')
  })
  it('parses a response only with the fields the display needs', () => {
    const ok = { r_km: [1, 2, 3], v_kms: [1, 2, 3], tle: { line1: 'a', line2: 'b' }, method: 'gauss+gibbs' }
    expect(parseIodResponse(ok)).not.toBeNull()
    expect(parseIodResponse({ ...ok, r_km: [1] })).toBeNull()
    expect(parseIodResponse(null)).toBeNull()
  })
})

describe('formats', () => {
  it('state vector, 3 decimals; no negative zero', () => {
    expect(fmtStateVector([-1328.5262, -414.964, 6921.178], [-6.614, 3.399, -1.065])).toBe(
      'R_2 : [ -1328.526, -414.964, 6921.178 ]   V_2 : [ -6.614, 3.399, -1.065 ]')
    expect(fmtStateVector([-0.0001, 0, 1], [0, 0, 0])).toContain('[ 0.000, 0.000, 1.000 ]')
  })
  it('TLE, angles, epoch, method', () => {
    expect(fmtTle({ line1: 'L1', line2: 'L2' })).toBe('L1\nL2')
    expect(fmtAngle(155.6314)).toBe('155.631°')
    expect(fmtAngle(16.3744, true)).toBe('+16.374°')
    expect(fmtAngle(-29.4524, true)).toBe('-29.452°')
    expect(fmtAngle(null)).toBe('—')
    expect(fmtEpoch(Date.UTC(2026, 9, 3, 15, 0, 30) / 1000)).toBe('2026-10-03 15:00:30')
    expect(doneText({ method: 'gauss+herrick-gibbs' } as never)).toBe('Done · Gauss + Herrick-Gibbs')
    expect(doneText({ method: 'gauss+gibbs' } as never)).toBe('Done · Gauss + Gibbs')
  })
})

describe('epoch sorting', () => {
  it('slots keep their numbers; empty rows go last in both directions', () => {
    const rows = [valid(300), emptyRow(), valid(100)]
    expect(sortedSlots(rows, 'asc')).toEqual([2, 0, 1])
    expect(sortedSlots(rows, 'desc')).toEqual([0, 2, 1])
  })
})

describe('memo', () => {
  beforeEach(resetIodMemo)
  it('stores and resets', () => {
    expect(getIodMemo()).toBeNull()
    setIodMemo({ rows: emptyRows(), result: null, stale: false, status: { kind: 'none', text: '' }, dir: 'asc' })
    expect(getIodMemo()?.dir).toBe('asc')
    resetIodMemo()
    expect(getIodMemo()).toBeNull()
  })
})
