import { describe, expect, it } from 'vitest'
import {
  buildControlView, homedDialogSince, isBusy, isConnected, needsHomeConfirm, refusalFromAck, REFUSAL_TTL_MS, statusLine,
  sunShortcut, trackDisabledReason,
  type ControlSnapshotLike,
} from '../../app/utils/mount-control'

const up = { linkUp: true, holder: 'none' as const, now: 1_000 }
const axes = (a: boolean, b: boolean) => [{ enabled: a }, { enabled: b }]

function snap(over: Partial<ControlSnapshotLike> = {}): ControlSnapshotLike {
  return {
    state: 'STOPPED',
    state_reason: null,
    pwi4: { reachable: true },
    mount: { connected: true },
    axes: axes(true, true),
    sequence: null,
    ...over,
  }
}

describe('connection state', () => {
  it('connected needs the mount connected and both axes enabled', () => {
    expect(isConnected(snap())).toBe(true)
    expect(isConnected(snap({ axes: axes(true, false) }))).toBe(false)
    expect(isConnected(snap({ mount: { connected: false } }))).toBe(false)
    expect(isConnected(null)).toBe(false)
  })

  it('offers CONNECT when disconnected and DISCONNECT / HOME when connected', () => {
    const d = buildControlView(snap({ state: 'DISCONNECTED', mount: { connected: false }, axes: axes(false, false) }), up)
    expect(d.connected).toBe(false)
    expect(d.connect).toEqual({ enabled: true, reason: null })
    expect(d.disconnect.enabled).toBe(false)
    expect(d.home).toEqual({ enabled: false, reason: 'Mount not connected' })
    const c = buildControlView(snap(), up)
    expect(c.connected).toBe(true)
    expect(c.disconnect.enabled).toBe(true)
    expect(c.home.enabled).toBe(true)
    expect(c.connect).toEqual({ enabled: false, reason: 'Already connected' })
  })

  it('connected but an axis disabled offers CONNECT again (re-enables)', () => {
    const v = buildControlView(snap({ state: 'FAULT', axes: axes(false, false) }), up)
    expect(v.connected).toBe(false)
    expect(v.connect.enabled).toBe(true)
    expect(v.line?.text).toBe('Axes disabled: CONNECT enables them')
  })
})

describe('busy and lock', () => {
  it('BUSY / HOMING / a running sequence disable every sequence command but not STOP', () => {
    for (const s of [
      snap({ state: 'BUSY' }),
      snap({ state: 'HOMING' }),
      snap({ sequence: { name: 'home', status: 'running' } }),
    ]) {
      expect(isBusy(s)).toBe(true)
      const v = buildControlView(s, up)
      expect(v.connect.enabled || v.disconnect.enabled || v.home.enabled).toBe(false)
      expect(v.home.reason).toBe('Busy')
      expect(v.stopEnabled).toBe(true)
    }
  })

  it('another window holding control disables commands, offers Take control, keeps STOP', () => {
    const v = buildControlView(snap(), { ...up, holder: 'other' })
    expect(v.home).toEqual({ enabled: false, reason: 'Another window has control' })
    expect(v.disconnect.enabled).toBe(false)
    expect(v.takeControl).toBe(true)
    expect(v.stopEnabled).toBe(true)
    expect(v.line).toEqual({ text: 'Controlled by another window', tone: 'normal' })
  })

  it('nobody or "you" holding control leaves commands enabled (the first command takes it)', () => {
    for (const holder of ['none', 'you'] as const) {
      const v = buildControlView(snap(), { ...up, holder })
      expect(v.home.enabled).toBe(true)
      expect(v.takeControl).toBe(false)
    }
  })

  it('link down disables everything including STOP and says why', () => {
    const v = buildControlView(snap(), { linkUp: false, holder: 'none', now: 1 })
    expect(v.stopEnabled).toBe(false)
    expect(v.connect.enabled || v.disconnect.enabled || v.home.enabled).toBe(false)
    expect(v.line?.tone).toBe('danger')
    expect(buildControlView(null, up).connect.enabled).toBe(false)
  })

  it('Pointing Mode segments follow the state', () => {
    expect(buildControlView(snap({ state: 'STOPPED' }), up)).toMatchObject({ stopLabel: true, trackingLabel: false })
    expect(buildControlView(snap({ state: 'TRACKING' }), up)).toMatchObject({ stopLabel: false, trackingLabel: true })
    expect(buildControlView(snap({ state: 'BUSY' }), up)).toMatchObject({ stopLabel: false, trackingLabel: false, stopEnabled: true })
  })
})

describe('status line', () => {
  const o = { linkUp: true, holder: 'none' as const, refusal: null, now: 5_000 }

  it('shows the current step while a sequence runs', () => {
    const l = statusLine(snap({ state: 'BUSY', state_reason: 'Connecting: enable axes…', sequence: { name: 'connect', status: 'running' } }), o)
    expect(l).toEqual({ text: 'Connecting: enable axes…', tone: 'normal' })
  })

  it('shows the failed step in red and the skipped-home warning in amber', () => {
    expect(statusLine(snap({ sequence: { name: 'connect', status: 'failed', error: 'Enable axes failed: PWI4 refused the request (HTTP 500)' } }), o))
      .toEqual({ text: 'Enable axes failed: PWI4 refused the request (HTTP 500)', tone: 'danger' })
    expect(statusLine(snap({ sequence: { name: 'connect', status: 'done', warning: 'Home skipped: path passes within 15° of the Sun' } }), o))
      .toEqual({ text: 'Home skipped: path passes within 15° of the Sun', tone: 'warn' })
    expect(statusLine(snap({ sequence: { name: 'home', status: 'cancelled', error: 'Cancelled by STOP' } }), o))
      .toEqual({ text: 'Cancelled by STOP', tone: 'warn' })
    expect(statusLine(snap({ sequence: { name: 'home', status: 'done' } }), o)).toBeNull()
  })

  it('a refusal wins and expires', () => {
    const refusal = { text: 'Another window has control', at: 5_000 }
    const s = snap({ sequence: { name: 'connect', status: 'failed', error: 'x failed: y' } })
    expect(statusLine(s, { ...o, refusal })).toEqual({ text: 'Another window has control', tone: 'danger' })
    expect(statusLine(s, { ...o, refusal, now: 5_000 + REFUSAL_TTL_MS + 1 })?.text).toBe('x failed: y')
  })

  it('shows no hint while BUSY (a just-ended sequence, stale axes)', () => {
    expect(statusLine(snap({ state: 'BUSY', axes: axes(false, false) }), o)).toBeNull()
  })

  it('hints when PWI4 is not running', () => {
    expect(statusLine(snap({ pwi4: { reachable: false }, state: 'DISCONNECTED' }), o)?.text).toBe('PWI4 not running: CONNECT starts it')
  })
})

describe('acks', () => {
  it('turns refusals into status-line text, ok acks into nothing', () => {
    expect(refusalFromAck({ id: '1', cmd: 'home', ok: true, reason: null }, 7)).toBeNull()
    expect(refusalFromAck({ id: '1', cmd: 'home', ok: false, reason: 'Slew path passes within 15° of the Sun' }, 7))
      .toEqual({ text: 'Slew path passes within 15° of the Sun', at: 7 })
    expect(refusalFromAck({ id: '1', cmd: 'home', ok: false, reason: null }, 7)?.text).toBe('Command refused')
  })

  it('the Sun-blocked disconnect is a question, not an error', () => {
    const ack = { id: '1', cmd: 'disconnect', ok: false, reason: 'Home is blocked by the Sun; disconnect without homing?', code: 'home_blocked_confirm' }
    expect(needsHomeConfirm(ack)).toBe(true)
    expect(refusalFromAck(ack, 1)).toBeNull()
    expect(needsHomeConfirm({ id: '1', cmd: 'disconnect', ok: false, reason: 'x' })).toBe(false)
  })
})

describe('TRACK disabled reasons (T3.3, G7)', () => {
  const o = { linkUp: true, holder: 'none' as const }
  const ok = (over: Partial<ControlSnapshotLike> = {}) => snap({
    mount: { connected: true, alt: 40 },
    sun: { alt: -30 },
    sun_distance_deg: 90,
    limits: { lim_el_deg: 20, sun_keepout_deg: 15 },
    homed: { state: 'homed', since: 't0' },
    ...over,
  })

  it('is enabled when everything is fine', () => {
    expect(trackDisabledReason(ok(), o)).toBeNull()
    expect(buildControlView(ok(), up).track).toEqual({ enabled: true, reason: null })
    expect(trackDisabledReason(ok({ homed: { state: 'overridden', since: 't' } }), o)).toBeNull()
  })

  it('covers every branch', () => {
    expect(trackDisabledReason(ok(), { ...o, linkUp: false })).toBe('Gateway not reachable')
    expect(trackDisabledReason(ok({ mount: { connected: false, alt: 40 } }), o)).toBe('Mount not connected')
    expect(trackDisabledReason(ok(), { ...o, holder: 'other' })).toBe('Another window has control')
    expect(trackDisabledReason(ok({ state: 'BUSY' }), o)).toBe('Busy')
    expect(trackDisabledReason(ok({ state: 'HOMING' }), o)).toBe('Busy')
    expect(trackDisabledReason(ok({ axes: axes(true, false) }), o)).toBe('Axes disabled')
    expect(trackDisabledReason(ok({ state: 'SLEWING' }), o)).toBe('Slewing')
    expect(trackDisabledReason(ok({ state: 'SLEWING', state_reason: 'Avoiding the Sun' }), o)).toBe('Avoiding the Sun') // T3.5 Q12
    expect(trackDisabledReason(ok({ homed: { state: 'not_homed', since: 't' } }), o)).toBe('Mount not homed')
    expect(trackDisabledReason(ok({ mount: { connected: true, alt: 10 } }), o)).toBe('Below Lim EL (20°)')
    expect(trackDisabledReason(ok({ sun_distance_deg: 7.25, sun: { alt: 40 } }), o))
      .toBe('Inside the Sun disc (7.3° from the Sun)')
  })

  it('applies the Sun shortcut like the gateway (sun_alt + 15 < min(Lim EL, mount alt))', () => {
    const near = { sun_distance_deg: 5 }
    expect(sunShortcut(0, 20, 40)).toBe(true)
    expect(sunShortcut(5, 20, 40)).toBe(false) // 20 < 20 is false
    expect(trackDisabledReason(ok({ ...near, sun: { alt: 0 } }), o)).toBeNull()
    expect(trackDisabledReason(ok({ ...near, sun: { alt: 6 } }), o)).toMatch(/^Inside the Sun disc/)
    // a mount below Lim EL: the Lim EL reason has priority over the Sun
    expect(trackDisabledReason(ok({ ...near, mount: { connected: true, alt: 10 } }), o)).toBe('Below Lim EL (20°)')
  })

  it('priority: connection > other window > busy > axes > slewing > not homed > Lim EL > Sun', () => {
    const all = ok({ state: 'SLEWING', homed: { state: 'not_homed', since: 't' }, mount: { connected: true, alt: 10 }, sun_distance_deg: 1, sun: { alt: 50 } })
    expect(trackDisabledReason(all, o)).toBe('Slewing')
    expect(trackDisabledReason({ ...all, state: 'STOPPED' }, o)).toBe('Mount not homed')
    expect(trackDisabledReason({ ...all, state: 'STOPPED', homed: { state: 'homed' } }, o)).toBe('Below Lim EL (20°)')
    expect(trackDisabledReason({ ...all, state: 'BUSY' }, { ...o, holder: 'other' })).toBe('Another window has control')
    expect(trackDisabledReason({ ...all, mount: { connected: false } }, { ...o, holder: 'other' })).toBe('Mount not connected')
    expect(trackDisabledReason({ ...all, state: 'BUSY', axes: axes(false, false) }, o)).toBe('Busy')
  })

  it('shows the reason on the status line; not-homed gets the amber line and the chip', () => {
    const v = buildControlView(ok({ state: 'SLEWING' }), up)
    expect(v.track).toEqual({ enabled: false, reason: 'Slewing' })
    expect(v.line).toEqual({ text: 'TRACK: Slewing', tone: 'normal' })
    const nh = buildControlView(ok({ homed: { state: 'not_homed', since: 't' } }), up)
    expect(nh.notHomed).toBe(true)
    expect(nh.line).toEqual({ text: 'Mount not homed: positions may be wrong', tone: 'warn' })
    expect(nh.track.reason).toBe('Mount not homed')
    expect(buildControlView(ok(), up).notHomed).toBe(false)
    expect(buildControlView(ok({ state: 'TRACKING' }), up).line).toBeNull()
  })
})

describe('Pointing Mode segments (T3.3)', () => {
  it('maps STOPPED / TRACKING / SLEWING', () => {
    const slew = buildControlView(snap({ state: 'SLEWING' }), up)
    expect(slew).toMatchObject({ stopLabel: false, trackingLabel: false, stopEnabled: true })
    expect(slew.track.enabled).toBe(false)
    expect(buildControlView(snap({ state: 'STOPPED' }), up)).toMatchObject({ stopLabel: true, trackingLabel: false })
    expect(buildControlView(snap({ state: 'TRACKING' }), up)).toMatchObject({ stopLabel: false, trackingLabel: true })
  })

  it('STOP needs only a reachable PWI4', () => {
    expect(buildControlView(snap({ pwi4: { reachable: false }, state: 'DISCONNECTED' }), up).stopEnabled).toBe(false)
    expect(buildControlView(snap({ state: 'DISCONNECTED', mount: { connected: false } }), up).stopEnabled).toBe(true)
  })
})

describe('not-homed dialog show-once (Q3)', () => {
  const nh = (since: string, over: Partial<ControlSnapshotLike> = {}) =>
    snap({ homed: { state: 'not_homed', since }, ...over })
  const d = (s: ControlSnapshotLike | null, dismissedSince: string | null = null, linkUp = true) =>
    homedDialogSince(s, { linkUp, dismissedSince })

  it('shows for a not_homed session and returns its since', () => {
    expect(d(nh('A'))).toBe('A')
  })

  it('stays hidden once dismissed for that since, returns for a new since', () => {
    expect(d(nh('A'), 'A')).toBeNull()
    expect(d(nh('B'), 'A')).toBe('B')
  })

  it('does not show when homed / overridden / unknown, disconnected, busy or link down', () => {
    expect(d(snap({ homed: { state: 'homed', since: 'A' } }))).toBeNull()
    expect(d(snap({ homed: { state: 'overridden', since: 'A' } }))).toBeNull()
    expect(d(snap({ homed: null }))).toBeNull()
    expect(d(nh('A', { mount: { connected: false } }))).toBeNull()
    expect(d(nh('A', { state: 'BUSY' }))).toBeNull()
    expect(d(nh('A', { sequence: { name: 'connect', status: 'running' } }))).toBeNull()
    expect(d(nh('A'), null, false)).toBeNull()
    expect(d(null)).toBeNull()
  })

  it('appears after a running sequence ends if not dismissed meanwhile', () => {
    expect(d(nh('A', { state: 'BUSY' }))).toBeNull()
    expect(d(nh('A', { state: 'STOPPED' }))).toBe('A')
  })
})

describe('stalled variant of the not-homed guard (T3.3b)', () => {
  const st = (over: Partial<ControlSnapshotLike> = {}, reason: string | null | 'absent' = 'stalled') =>
    snap({
      mount: { connected: true, alt: 40 },
      homed: { state: 'not_homed', since: 'S', ...(reason === 'absent' ? {} : { reason }) },
      ...over,
    })

  it('flags stalled, with the red status line and the stalled TRACK reason', () => {
    const v = buildControlView(st(), up)
    expect(v).toMatchObject({ notHomed: true, stalled: true })
    expect(v.track.reason).toBe('Mount stalled; home it first')
    expect(v.line?.tone).toBe('danger')
    expect(v.line?.text).toMatch(/Home the mount before further use/)
  })

  it('uses the gateway stall text (state_reason) when it names the stall', () => {
    const w = 'Mount stalled (axis 1 not moving); it may be at a physical limit. Home the mount before further use.'
    expect(buildControlView(st({ state_reason: w }), up).line).toEqual({ text: w, tone: 'danger' })
    expect(buildControlView(st({ warning: w }), up).line).toEqual({ text: w, tone: 'danger' })
    expect(buildControlView(st({ warning: 'other' }), up).line?.text).not.toBe('other')
  })

  it('keeps the stalled reason in the same priority slot', () => {
    const o = { linkUp: true, holder: 'none' as const }
    expect(trackDisabledReason(st({ state: 'SLEWING' }), o)).toBe('Slewing')
    expect(trackDisabledReason(st({ state: 'STOPPED', mount: { connected: true, alt: 5 } }), o)).toBe('Mount stalled; home it first')
  })

  it('absent or null reason is a plain not-homed', () => {
    for (const r of ['absent', null] as const) {
      const v = buildControlView(st({}, r), up)
      expect(v).toMatchObject({ notHomed: true, stalled: false })
      expect(v.track.reason).toBe('Mount not homed')
      expect(v.line).toEqual({ text: 'Mount not homed: positions may be wrong', tone: 'warn' })
    }
  })

  it('the dialog shows once per since for a stall too', () => {
    expect(homedDialogSince(st(), { linkUp: true, dismissedSince: null })).toBe('S')
    expect(homedDialogSince(st(), { linkUp: true, dismissedSince: 'S' })).toBeNull()
  })
})
