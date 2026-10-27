import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createJogSender, fmtAz, fmtEl, fmtSize, jogDisabledReason, jogStatus, keyToPad, sizeAtEnd, speedAtEnd,
  isTurning, stepBlocked, stepSize, stepSpeed, STEP_SIZES, JOG_SPEEDS, wrapPromptText, wrapPromptVisible, WRAP_TITLE,
} from '../../app/utils/jog-modal'
import type { ControlSnapshotLike } from '../../app/utils/mount-control'

const ok = { id: '1', cmd: 'jog', ok: true, reason: null }

describe('steppers', () => {
  it('speed walks the list and stops at both ends', () => {
    expect(stepSpeed(5, -1)).toBe(2)
    expect(stepSpeed(5, 1)).toBe(10)
    expect(stepSpeed(10, 1)).toBe(10)
    expect(stepSpeed(0.5, -1)).toBe(0.5)
    expect(speedAtEnd(10, 1)).toBe(true)
    expect(speedAtEnd(0.5, -1)).toBe(true)
    expect(speedAtEnd(5, 1)).toBe(false)
    expect(JOG_SPEEDS).toEqual([0.5, 1, 2, 5, 10])
  })
  it('step size walks the list and stops at both ends', () => {
    expect(stepSize(10, 1)).toBe(30)
    expect(stepSize(10, -1)).toBe(5)
    expect(stepSize(60, 1)).toBe(60)
    expect(stepSize(1, -1)).toBe(1)
    expect(sizeAtEnd(60, 1)).toBe(true)
    expect(sizeAtEnd(1, -1)).toBe(true)
    expect(STEP_SIZES).toEqual([1, 5, 10, 30, 60])
    expect(fmtSize(10)).toBe('10′')
    expect(fmtSize(60)).toBe('1°')
  })
})

describe('key mapping', () => {
  it('maps the arrows to axis / dir', () => {
    expect(keyToPad('ArrowLeft')).toEqual({ axis: 0, dir: -1 })
    expect(keyToPad('ArrowRight')).toEqual({ axis: 0, dir: 1 })
    expect(keyToPad('ArrowDown')).toEqual({ axis: 1, dir: -1 })
    expect(keyToPad('ArrowUp')).toEqual({ axis: 1, dir: 1 })
    expect(keyToPad('a')).toBeNull()
  })
})

describe('jog sender schedule', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('press sends jog, keep-alives every 100 ms, release sends jog_stop and stops', () => {
    const send = vi.fn(() => Promise.resolve(ok))
    const s = createJogSender(send, vi.fn())
    expect(s.press({ axis: 0, dir: -1 }, 5)).toBe(true)
    expect(send).toHaveBeenCalledTimes(1)
    expect(send).toHaveBeenLastCalledWith('jog', { axis: 0, dir: -1, speed: 5 })
    vi.advanceTimersByTime(99)
    expect(send).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(1)
    expect(send).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(300)
    expect(send).toHaveBeenCalledTimes(5)
    expect(s.release()).toBe(true)
    expect(send).toHaveBeenLastCalledWith('jog_stop', {})
    const n = send.mock.calls.length
    vi.advanceTimersByTime(1000)
    expect(send).toHaveBeenCalledTimes(n)
    expect(s.held).toBeNull()
  })

  it('only one direction at a time; a release of another pad does nothing', () => {
    const send = vi.fn(() => Promise.resolve(ok))
    const s = createJogSender(send, vi.fn())
    s.press({ axis: 0, dir: 1 }, 1)
    expect(s.press({ axis: 1, dir: 1 }, 1)).toBe(false)
    expect(send).toHaveBeenCalledTimes(1)
    expect(s.release({ axis: 1, dir: 1 })).toBe(false)
    expect(s.held).toEqual({ axis: 0, dir: 1 })
    expect(s.release({ axis: 0, dir: 1 })).toBe(true)
    expect(s.press({ axis: 1, dir: -1 }, 2)).toBe(true)
  })

  it('release without a hold sends nothing (blur / visibility / unmount are safe)', () => {
    const send = vi.fn(() => Promise.resolve(ok))
    const s = createJogSender(send, vi.fn())
    expect(s.release()).toBe(false)
    expect(send).not.toHaveBeenCalled()
  })

  it('a refused jog ends the hold and reports the reason', async () => {
    const send = vi.fn(() => Promise.resolve({ id: '1', cmd: 'jog', ok: false, reason: 'Mount stalled; home it first' }))
    const onRefusal = vi.fn()
    const s = createJogSender(send, onRefusal)
    s.press({ axis: 0, dir: 1 }, 5)
    await vi.advanceTimersByTimeAsync(0)
    expect(onRefusal).toHaveBeenCalledWith('Mount stalled; home it first')
    expect(s.held).toBeNull()
    await vi.advanceTimersByTimeAsync(1000)
    expect(send).toHaveBeenCalledTimes(1)
  })
})

describe('step blocking', () => {
  it('blocks while a step runs or an ack is pending', () => {
    expect(stepBlocked(null, false)).toBe(false)
    expect(stepBlocked({ step: null }, false)).toBe(false)
    expect(stepBlocked({ step: { axis: 0, dir: 1, size_arcmin: 10 } }, false)).toBe(true)
    expect(stepBlocked({ step: null }, true)).toBe(true)
  })
})

describe('disabled reasons and status', () => {
  const base: ControlSnapshotLike = {
    state: 'STOPPED', mount: { connected: true }, axes: [{ enabled: true }, { enabled: true }], homed: { state: 'homed' },
  }
  const o = { linkUp: true, holder: 'you' as const }
  it('uses the existing texts', () => {
    expect(jogDisabledReason(base, o)).toBeNull()
    expect(jogDisabledReason(null, o)).toBe('Gateway not reachable')
    expect(jogDisabledReason({ ...base, mount: { connected: false } }, o)).toBe('Mount not connected')
    expect(jogDisabledReason(base, { ...o, holder: 'other' })).toBe('Another window has control')
    expect(jogDisabledReason({ ...base, state: 'BUSY' }, o)).toBe('Busy')
    expect(jogDisabledReason({ ...base, axes: [{ enabled: false }, { enabled: true }] }, o)).toBe('Axes disabled')
    expect(jogDisabledReason({ ...base, homed: { state: 'not_homed' } }, o)).toBe('Mount not homed')
    expect(jogDisabledReason({ ...base, homed: { state: 'not_homed', reason: 'stalled' } }, o)).toBe('Mount stalled; home it first')
  })
  it('status line precedence: reason, refusal, jog, step, ready', () => {
    const b = { reason: null, refusal: null, mode: 'jog' as const, snapshot: null, held: null, pending: false }
    expect(jogStatus(b)).toEqual({ text: 'Ready', tone: 'normal' })
    expect(jogStatus({ ...b, reason: 'Busy', refusal: 'x' })).toEqual({ text: 'Busy', tone: 'danger' })
    expect(jogStatus({ ...b, refusal: 'Step in progress' })).toEqual({ text: 'Step in progress', tone: 'danger' })
    expect(jogStatus({ ...b, snapshot: { jog: { axis: 0, dir: -1, speed: 5 } } }).text).toBe('Jogging left at 5.0°/s')
    expect(jogStatus({ ...b, held: { axis: 1, dir: 1 } }).text).toBe('Jogging up…')
    expect(jogStatus({ ...b, snapshot: { step: { axis: 1, dir: -1, size_arcmin: 60 } } }).text).toBe('Step down 1°…')
  })
  it('formats az / el like the render', () => {
    expect(fmtAz(315.0371)).toBe('315.04')
    expect(fmtEl(71.2)).toBe('+ 71.20')
    expect(fmtEl(-3.456)).toBe('− 3.46')
    expect(fmtEl(null)).toBe('—')
  })
})

describe('JogDialog disabled look', () => {
  it('the disabled arrows use solid colours, never opacity (the guide line must not show through)', () => {
    const src = readFileSync('app/components/mount/JogDialog.vue', 'utf8')
    const rule = src.split('}').filter(l => l.includes('.jg-arrow:disabled'))
    expect(rule.length).toBeGreaterThan(0)
    for (const l of rule) expect(l).not.toMatch(/opacity: /)
    expect(rule.join(' ')).toMatch(/background:/)
  })
})

describe('wrap-turn prompt (T3.4e)', () => {
  const lim = { axis: 0, dir: 1, limit_deg: 270, turn_deg: 360, est_s: 72 }
  const b = { reason: null, refusal: null, mode: 'jog' as const, snapshot: null, held: null, pending: false }
  it('is visible only with jog_limit, no turn and not dismissed', () => {
    expect(wrapPromptVisible(null, false)).toBe(false)
    expect(wrapPromptVisible({ jog_limit: null }, false)).toBe(false)
    expect(wrapPromptVisible({}, false)).toBe(false)
    expect(wrapPromptVisible({ jog_limit: lim }, false)).toBe(true)
    expect(wrapPromptVisible({ jog_limit: lim }, true)).toBe(false)
    expect(wrapPromptVisible({ jog_limit: lim, turn: { from_deg: 270, to_deg: -90 } }, false)).toBe(false)
  })
  it('texts follow dir', () => {
    expect(WRAP_TITLE).toBe('Azimuth limit reached')
    expect(wrapPromptText({ jog_limit: lim })).toBe('The azimuth axis is at its mechanical limit (±270°). To keep moving right the mount must turn 360° the other way round (about 72 s). Turn the mount, or stay here?')
    expect(wrapPromptText({ jog_limit: { ...lim, dir: -1 } })).toContain('keep moving left')
  })
  it('turning disables the pad flag and sets the status', () => {
    expect(isTurning({ turn: { from_deg: 1, to_deg: 2 } })).toBe(true)
    expect(isTurning({ turn: null })).toBe(false)
    expect(isTurning(null)).toBe(false)
    expect(jogStatus({ ...b, snapshot: { turn: { from_deg: 270, to_deg: -90 } } })).toEqual({ text: 'Turning 360° to -90°…', tone: 'normal' })
    expect(jogStatus({ ...b, reason: 'Busy', refusal: 'x', snapshot: { turn: { to_deg: 10.5 } } }).text).toBe('Turning 360° to 10.5°…')
  })
  it('component wiring: commands, refusal, solid pad', () => {
    const src = readFileSync('app/components/mount/JogDialog.vue', 'utf8')
    expect(src).toContain("sendCommand('wrap_turn', {})")
    expect(src).toContain("sendCommand('wrap_turn_cancel', {})")
    expect(src).toContain('data-testid="jg-wrap-turn"')
    expect(src).toContain('data-testid="jg-wrap-stay"')
    expect(src).toMatch(/disabled\.value \|\| turning\.value/)
    expect(src).toMatch(/if \(!v\) stayHere\(\)/)
  })
})
