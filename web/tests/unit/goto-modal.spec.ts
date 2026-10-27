import { describe, expect, it } from 'vitest'
import {
  activateGroup, applyTransformResult, buildGotoCommand, buildTransformRequest, capElevation, CANNOT_VERIFY, editField, elevationInvalid,
  gotoStatus, initialState, observeState, parseCheck, quickCheck, SLEW_GRACE_MS, slewLine, startSlewTrack,
  type CheckResult, type GotoState, type GotoStatusInput,
} from '../../app/utils/goto-modal'
import { buildControlView, gotoDisabledReason, type ControlSnapshotLike } from '../../app/utils/mount-control'

const base: GotoState = { active: 'local', az: 157.2349, el: 51.41, ra: 22.5434, dec: -28.253 }

describe('active group switching', () => {
  it('editing a group makes it active; the other keeps its values', () => {
    const a = editField(base, 'ra', 5.5)
    expect(a.active).toBe('celestial')
    expect(a.ra).toBe(5.5)
    expect(a.az).toBe(base.az)
    expect(editField(a, 'az', 10).active).toBe('local')
  })
  it('a computed negative elevation is capped when Local becomes active', () => {
    const s = editField({ ...base, active: 'celestial', el: -28.7 }, 'az', 23)
    expect(s.active).toBe('local')
    expect(s.el).toBe(0)
  })
})

describe('elevation cap', () => {
  it('stays within 0-90', () => {
    expect(capElevation(-3)).toBe(0)
    expect(capElevation(95)).toBe(90)
    expect(capElevation(45.5)).toBe(45.5)
    expect(capElevation(Number.NaN)).toBe(0)
    expect(editField(base, 'el', -5).el).toBe(0)
  })
  it('quick check flags bad input only for the active group', () => {
    expect(quickCheck(base)).toBeNull()
    expect(quickCheck({ ...base, el: 120 })).toMatch(/0° and 90°/)
    expect(quickCheck({ ...base, az: Number.NaN })).toMatch(/valid/)
    expect(quickCheck({ ...base, active: 'celestial', el: -40 })).toBeNull()
    expect(quickCheck({ ...base, active: 'celestial', dec: 91 })).toMatch(/Declination/)
  })
})

describe('command sent', () => {
  it('local active sends goto_altaz with alt/az', () => {
    expect(buildGotoCommand(base)).toEqual({ cmd: 'goto_altaz', args: { alt: 51.41, az: 157.2349 } })
    expect(buildGotoCommand({ ...base, az: 370 }).args.az).toBeCloseTo(10)
  })
  it('celestial active sends goto_radec_j2000 with ra_h/dec_deg', () => {
    expect(buildGotoCommand({ ...base, active: 'celestial' })).toEqual({ cmd: 'goto_radec_j2000', args: { ra_h: 22.5434, dec_deg: -28.253 } })
  })
})

describe('transform of the inactive group', () => {
  const now = Date.UTC(2026, 9, 27, 12, 0, 0)
  it('local active -> altaz_to_radec at now', () => {
    expect(buildTransformRequest(base, now)).toEqual({ dir: 'altaz_to_radec', t_utc: '2026-10-27T12:00:00.000Z', values: { alt: 51.41, az: 157.2349 } })
  })
  it('celestial active -> radec_to_altaz', () => {
    expect(buildTransformRequest({ ...base, active: 'celestial' }, now).dir).toBe('radec_to_altaz')
    expect(buildTransformRequest({ ...base, active: 'celestial' }, now).values).toEqual({ ra_h: 22.5434, dec_deg: -28.253 })
  })
  it('a result only rewrites the inactive group', () => {
    const l = applyTransformResult(base, { ra_h: 1.5, dec_deg: 2.5 })!
    expect(l).toMatchObject({ active: 'local', az: base.az, el: base.el, ra: 1.5, dec: 2.5 })
    const c = applyTransformResult({ ...base, active: 'celestial' }, { alt: -28.7, az: 365 })!
    expect(c).toMatchObject({ ra: base.ra, dec: base.dec, el: -28.7 })
    expect(c.az).toBeCloseTo(5)
    expect(applyTransformResult(base, { alt: 1, az: 2 })).toBeNull()
    expect(applyTransformResult(base, null)).toBeNull()
  })
  it('initial state is the current pointing, local active, el capped', () => {
    expect(initialState({ az: 10, alt: -2, ra_j2000_h: 3, dec_j2000_deg: 4 })).toEqual({ active: 'local', az: 10, el: 0, ra: 3, dec: 4 })
    expect(initialState(null).active).toBe('local')
  })
})

describe('check result and red boxes', () => {
  it('parses the endpoint body', () => {
    expect(parseCheck({ ok: true, reason: null, target: { alt: 1, az: 2 } })).toEqual({ kind: 'ok' })
    expect(parseCheck({ ok: false, reason: 'Within 15° of the Sun', code: 'sun' })).toEqual({ kind: 'refused', reason: 'Within 15° of the Sun', code: 'sun' })
    expect(parseCheck({ ok: false })).toMatchObject({ kind: 'refused', reason: 'Command refused' })
    expect(parseCheck({})).toEqual({ kind: 'unreachable' })
    expect(parseCheck(null)).toEqual({ kind: 'unreachable' })
  })
  it('elevation boxes are red for Lim EL / Sun target reasons, not for path or other refusals', () => {
    const r = (reason: string): CheckResult => ({ kind: 'refused', reason })
    expect(elevationInvalid(r('Below station limit elevation (Lim EL 20°)'))).toBe(true)
    expect(elevationInvalid(r('Within 15° of the Sun'))).toBe(true)
    expect(elevationInvalid(r('Slew path passes within 15° of the Sun'))).toBe(false)
    expect(elevationInvalid(r('Mount not homed'))).toBe(false)
    expect(elevationInvalid({ kind: 'ok' })).toBe(false)
    expect(elevationInvalid({ kind: 'unreachable' })).toBe(false)
  })
})

describe('status line', () => {
  const ok: CheckResult = { kind: 'ok' }
  const i = (over: Partial<GotoStatusInput> = {}): GotoStatusInput => ({
    gateReason: null, quick: null, ackRefusal: null, sending: false, slew: null, state: 'STOPPED', check: ok, now: 10_000, ...over,
  })
  it('Ready and SLEW enabled when the check is ok', () => {
    expect(gotoStatus(i())).toEqual({ text: 'Ready', tone: 'normal', canSlew: true })
  })
  it('the gateway reason disables SLEW in red; unreachable gives Cannot verify; pending waits', () => {
    expect(gotoStatus(i({ check: { kind: 'refused', reason: 'Within 15° of the Sun' } }))).toEqual({ text: 'Within 15° of the Sun', tone: 'danger', canSlew: false })
    expect(gotoStatus(i({ check: { kind: 'unreachable' } }))).toEqual({ text: CANNOT_VERIFY, tone: 'danger', canSlew: false })
    expect(gotoStatus(i({ check: { kind: 'pending' } })).canSlew).toBe(false)
  })
  it('the guard reason and quick check win over an ok check', () => {
    expect(gotoStatus(i({ gateReason: 'Mount not homed' }))).toEqual({ text: 'Mount not homed', tone: 'danger', canSlew: false })
    expect(gotoStatus(i({ quick: 'Enter valid coordinates' })).canSlew).toBe(false)
  })
  it('an ack refusal shows in red; sending shows Sending', () => {
    expect(gotoStatus(i({ ackRefusal: 'Slew path passes within 15° of the Sun' }))).toMatchObject({ tone: 'danger', text: 'Slew path passes within 15° of the Sun' })
    expect(gotoStatus(i({ sending: true }))).toMatchObject({ text: 'Sending…', canSlew: false })
  })
  it('follows the slew: Slewing then Arrived (STOPPED) for local', () => {
    let t = startSlewTrack('local', 10_000)
    expect(slewLine(t, 'STOPPED', 10_500)).toBe('Slewing…') // state not updated yet
    t = observeState(t, 'SLEWING')!
    expect(t.sawSlewing).toBe(true)
    expect(gotoStatus(i({ slew: t, state: 'SLEWING' })).text).toBe('Slewing…')
    expect(gotoStatus(i({ slew: t, state: 'STOPPED', now: 20_000 })).text).toBe('Arrived (STOPPED)')
  })
  it('celestial ends Tracking; a missed short slew falls back after the grace; STOP shows Stopped', () => {
    const t = startSlewTrack('celestial', 0)
    expect(slewLine(t, 'TRACKING', SLEW_GRACE_MS - 1)).toBe('Slewing…')
    expect(slewLine(t, 'TRACKING', SLEW_GRACE_MS + 1)).toBe('Tracking')
    expect(slewLine(observeState(t, 'SLEWING')!, 'STOPPED', 9_000)).toBe('Stopped')
    expect(slewLine(observeState(t, 'SLEWING')!, 'BUSY', 9_000)).toBeNull()
  })
})

describe('GO TO button guard', () => {
  const snap = (o: Partial<ControlSnapshotLike> = {}): ControlSnapshotLike => ({
    state: 'STOPPED', mount: { connected: true }, axes: [{ enabled: true }, { enabled: true }], sequence: null, ...o,
  })
  const up = { linkUp: true, holder: 'none' as const }
  it('enabled when connected and homed', () => {
    expect(gotoDisabledReason(snap(), up)).toBeNull()
    expect(buildControlView(snap(), up).goto).toEqual({ enabled: true, reason: null })
    expect(gotoDisabledReason(snap({ state: 'SLEWING' }), up)).toBeNull()
  })
  it('reasons: link, not connected, other window, busy, not homed, stalled', () => {
    expect(gotoDisabledReason(null, up)).toBe('Gateway not reachable')
    expect(gotoDisabledReason(snap({ mount: { connected: false } }), up)).toBe('Mount not connected')
    expect(gotoDisabledReason(snap(), { ...up, holder: 'other' })).toBe('Another window has control')
    expect(gotoDisabledReason(snap({ state: 'BUSY' }), up)).toBe('Busy')
    expect(gotoDisabledReason(snap({ homed: { state: 'not_homed' } }), up)).toBe('Mount not homed')
    expect(gotoDisabledReason(snap({ homed: { state: 'not_homed', reason: 'stalled' } }), up)).toBe('Mount stalled; home it first')
    expect(gotoDisabledReason(snap({ homed: { state: 'overridden' } }), up)).toBeNull()
  })
})

describe('activateGroup', () => {
  it('switches the active group and never touches a value (even a negative computed elevation)', () => {
    const s: GotoState = { ...base, active: 'celestial', el: -28.75 }
    const n = activateGroup(s, 'local')
    expect(n).toEqual({ ...s, active: 'local' })
    expect(activateGroup(n, 'celestial')).toEqual(s)
    expect(activateGroup(base, 'local')).toBe(base)
  })
})
