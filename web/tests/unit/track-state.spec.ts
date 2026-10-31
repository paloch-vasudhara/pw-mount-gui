import { describe, expect, it } from 'vitest'
import { fmtCountdown, isObjectTrackActive, trackCardState } from '../../app/utils/track-state'

const v = (o: object = {}) => ({ enabled: true, reason: null, notes: [] as string[], ...o })
const trk = (o: object = {}) => ({ norad: 25544, name: 'ISS', phase: 'waiting', t_r: 1100, t_end: 1400, notes: [], ended: null, ...o })

describe('fmtCountdown', () => {
  it('mm:ss, never negative', () => {
    expect(fmtCountdown(75)).toBe('01:15')
    expect(fmtCountdown(-5)).toBe('00:00')
    expect(fmtCountdown(0.2)).toBe('00:01')
    expect(fmtCountdown(3600)).toBe('60:00')
  })
})

describe('trackCardState', () => {
  it('idle: verdict drives button and reason', () => {
    expect(trackCardState(null, 25544, 1000, v())).toEqual({ isTrackedCard: false, buttonLabel: 'TRACK OBJECT', buttonEnabled: true, line: null })
    const r = trackCardState(null, 25544, 1000, v({ enabled: false, reason: 'Mount not homed' }))
    expect(r.buttonEnabled).toBe(false)
    expect(r.line).toBe('Mount not homed')
    expect(trackCardState(null, null, 1000, null).buttonEnabled).toBe(false)
  })
  it('enabled with a zenith note shows the note', () => {
    expect(trackCardState(null, 1, 0, v({ notes: ['Near-zenith pass: tracking lags near the peak'] })).line).toBe('Near-zenith pass: tracking lags near the peak')
  })
  it('phase lines for the tracked card', () => {
    const at = (t: object, now = 1000) => trackCardState(trk(t), 25544, now, v())
    expect(at({ phase: 'slewing' }).line).toBe('Slewing to the pass')
    expect(at({}).line).toBe('Waiting for the pass — 01:40')
    expect(at({}, 1200).line).toBe('Waiting for the pass — 00:00')
    expect(at({ phase: 'tracking' }).line).toBe('Tracking — ends in 06:40')
    expect(at({ phase: 'tracking', t_end: null }).line).toBe('Tracking')
    expect(at({ phase: 'detour' }).line).toBe('Avoiding the Sun')
    const s = at({ phase: 'tracking' })
    expect(s).toMatchObject({ isTrackedCard: true, buttonLabel: 'TRACKING', buttonEnabled: false })
  })
  it('appends the track note', () => {
    expect(trackCardState(trk({ phase: 'tracking', notes: ['Near-zenith pass'] }), 25544, 1000, v()).line).toBe('Tracking — ends in 06:40 · Near-zenith pass')
  })
  it('another object is tracked: this card stays idle', () => {
    const r = trackCardState(trk(), 99, 1000, v())
    expect(r).toMatchObject({ isTrackedCard: false, buttonLabel: 'TRACK OBJECT', buttonEnabled: true })
  })
  it('ended for this NORAD shows its reason', () => {
    const t = { norad: null, phase: null, ended: { norad: 25544, reason: 'Track ended at Lim EL', at: 5 } }
    expect(trackCardState(t, 25544, 1000, v()).line).toBe('Track ended at Lim EL')
    expect(trackCardState(t, 7, 1000, v()).line).toBeNull()
  })
})

describe('isObjectTrackActive', () => {
  it('needs a phase', () => {
    expect(isObjectTrackActive(null)).toBe(false)
    expect(isObjectTrackActive({ phase: null, ended: { reason: 'x' } })).toBe(false)
    expect(isObjectTrackActive({ phase: 'waiting' })).toBe(true)
  })
})
