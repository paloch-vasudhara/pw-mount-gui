import { describe, expect, it } from 'vitest'
import { GATEWAY_NOT_RESPONDING, parseTrackVerdict } from '../../app/utils/track-verdict'

describe('parseTrackVerdict', () => {
  it('ok + track phase enables', () => {
    expect(parseTrackVerdict({ ok: true, reason: null, pass: { phase: 'track' } })).toEqual({ enabled: true, reason: null, phase: 'track' })
  })
  it('ok + pre_position keeps the phase', () => {
    expect(parseTrackVerdict({ ok: true, pass: { phase: 'pre_position' } })).toEqual({ enabled: true, reason: null, phase: 'pre_position' })
  })
  it('ok without a pass defaults to track', () => {
    expect(parseTrackVerdict({ ok: true, pass: null }).phase).toBe('track')
  })
  it.each(['Mount is not homed', 'Peak below the elevation limit', 'Satellite data unavailable'])('passes the refusal reason through: %s', (reason) => {
    expect(parseTrackVerdict({ ok: false, reason, code: 'x' })).toEqual({ enabled: false, reason, phase: null })
  })
  it('a refusal without a reason gets a generic one', () => {
    expect(parseTrackVerdict({ ok: false }).reason).toBe('Command refused')
  })
  it.each([null, undefined, 'x', 42, {}, { ok: 'yes' }])('malformed body %j is unreachable', (body) => {
    expect(parseTrackVerdict(body)).toEqual({ enabled: false, reason: GATEWAY_NOT_RESPONDING, phase: null })
  })
})
