// MOUNT CONTROL panel rules (T3.2 / logic sheet A5a, A5b, A5c, G7). Pure functions, unit-tested.
// The panel only reflects what the gateway reports; every refusal reason comes from the gateway.

import type { CommandAck, ControlHolder } from './mount-socket'

/** The gateway's snapshot.sequence (connect / disconnect / home progress, kept after it ends). */
export interface SequenceLike {
  name: 'connect' | 'disconnect' | 'home' | string
  step?: string | null
  step_index?: number | null
  steps?: string[] | null
  status: 'running' | 'done' | 'failed' | 'cancelled' | string
  error?: string | null
  warning?: string | null
}

export interface ControlSnapshotLike {
  state?: string | null
  state_reason?: string | null
  warning?: string | null
  pwi4?: { reachable?: boolean | null } | null
  mount?: { connected?: boolean | null, alt?: number | null } | null
  axes?: Array<{ enabled?: boolean | null } | null> | null
  sequence?: SequenceLike | null
  sun?: { alt?: number | null } | null
  sun_distance_deg?: number | null
  limits?: { lim_el_deg?: number | null, sun_keepout_deg?: number | null } | null
  /** Not-homed guard (T3.3, Q3); null when the mount is not connected. */
  homed?: HomedInfo | null
  /** T3.5: a Sun-avoiding route (state SLEWING, state_reason "Avoiding the Sun"). */
  route?: { active?: boolean, sides?: string[], t_end?: number } | null
}

export interface HomedInfo {
  state: 'homed' | 'not_homed' | 'overridden' | string
  since?: string | null
  /** 'stalled': the gateway stopped the mount on a stalled axis; only a completed home clears it. */
  reason?: 'stalled' | string | null
}

export const STALLED_REASON = 'Mount stalled; home it first'
export const STALLED_TEXT = 'Mount stalled (axis not moving); it may be at a physical limit. Home the mount before further use.'

export type LineTone = 'normal' | 'warn' | 'danger'

export interface Refusal {
  text: string
  at: number
}

/** How long a refusal stays on the status line (ms). */
export const REFUSAL_TTL_MS = 12_000

export const CODE_HOME_BLOCKED = 'home_blocked_confirm'

export const TIP_TRACK_TARGET = 'Arrives in T4'
export const TIP_OTHER_WINDOW = 'Another window has control'

export interface ControlButton {
  enabled: boolean
  /** Why it is disabled (tooltip, G7); null when enabled. */
  reason: string | null
}

export interface ControlView {
  linkUp: boolean
  /** Mount connected AND both axes enabled (anything less offers CONNECT again). */
  connected: boolean
  /** A connect / disconnect / home sequence is running (state BUSY or HOMING). */
  busy: boolean
  state: string
  holder: ControlHolder
  connect: ControlButton
  disconnect: ControlButton
  home: ControlButton
  /** TRACK (A5b: tracks the current RA/Dec) with the G7 reason it is disabled. */
  track: ControlButton
  /** GO TO (A5c-iv): opens the Go To modal. */
  goto: ControlButton
  /** JOG (A5c-iii): opens the Jog modal; same gate as GO TO. */
  jog: ControlButton
  /** homed.state === 'not_homed' while connected: shows the NOT HOMED chip + inline actions. */
  notHomed: boolean
  /** notHomed because an axis stalled: STALLED chip, HOME only (no Proceed anyway). */
  stalled: boolean
  /** STOP is never disabled while the gateway link is up (accepted from any window). */
  stopEnabled: boolean
  /** Left Pointing Mode segment: the active "STOPPED" label, or the STOP button. */
  stopLabel: boolean
  trackingLabel: boolean
  line: { text: string, tone: LineTone } | null
  /** "Controlled by another window" is showing (offers TAKE CONTROL). */
  takeControl: boolean
}

export function isConnected(s: ControlSnapshotLike | null): boolean {
  if (!s || !s.mount?.connected) return false
  const axes = s.axes ?? []
  return axes.length >= 2 && !!axes[0]?.enabled && !!axes[1]?.enabled
}

export function isBusy(s: ControlSnapshotLike | null): boolean {
  if (!s) return false
  return s.sequence?.status === 'running' || s.state === 'BUSY' || s.state === 'HOMING'
}

/** The status line (G7): refusal > running step > failed step > cancelled > warning > hints. */
export function statusLine(
  s: ControlSnapshotLike | null,
  opts: { linkUp: boolean, holder: ControlHolder, refusal: Refusal | null, now: number },
): { text: string, tone: LineTone } | null {
  if (!opts.linkUp) return { text: 'Gateway not reachable: controls disabled', tone: 'danger' }
  if (!s) return null
  const { refusal } = opts
  if (refusal && opts.now - refusal.at < REFUSAL_TTL_MS) return { text: refusal.text, tone: 'danger' }
  const seq = s.sequence
  if (seq?.status === 'running') {
    const verb = seq.name === 'connect' ? 'Connecting' : seq.name === 'disconnect' ? 'Disconnecting' : 'Homing'
    return { text: s.state_reason ?? `${verb}…`, tone: 'normal' }
  }
  if (seq?.status === 'failed' && seq.error) return { text: seq.error, tone: 'danger' }
  if (seq?.status === 'cancelled') return { text: seq.error ?? 'Cancelled', tone: 'warn' }
  if (seq?.status === 'done' && seq.warning) return { text: seq.warning, tone: 'warn' }
  if (isBusy(s)) return null // the sequence just ended: the next status has not arrived yet
  if (opts.holder === 'other') return { text: 'Controlled by another window', tone: 'normal' }
  if (s.pwi4?.reachable === false) return { text: 'PWI4 not running: CONNECT starts it', tone: 'normal' }
  if (s.mount?.connected && !isConnected(s)) return { text: 'Axes disabled: CONNECT enables them', tone: 'normal' }
  if (isStalled(s)) {
    const w = s.state_reason ?? s.warning // the gateway sends the stall text as state_reason (T3.3b)
    return { text: w && /stalled/i.test(w) ? w : STALLED_TEXT, tone: 'danger' }
  }
  if (isNotHomed(s)) return { text: 'Mount not homed: positions may be wrong', tone: 'warn' }
  if (isConnected(s) && s.state !== 'TRACKING') {
    const why = trackDisabledReason(s, { linkUp: true, holder: opts.holder })
    if (why) return { text: `TRACK: ${why}`, tone: 'normal' }
  }
  return null
}

const KEEPOUT_DEG = 15

const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

export function isNotHomed(s: ControlSnapshotLike | null): boolean {
  return isConnected(s) && s?.homed?.state === 'not_homed'
}

export function isStalled(s: ControlSnapshotLike | null): boolean {
  return isNotHomed(s) && s?.homed?.reason === 'stalled'
}

/** Gateway Sun shortcut (safety.sun_shortcut): the whole Sun disc is below Lim EL and below the mount. */
export function sunShortcut(sunAlt: number, limEl: number, mountAlt: number): boolean {
  return sunAlt + KEEPOUT_DEG < Math.min(limEl, mountAlt)
}

/**
 * Why TRACK is disabled (G7), first match wins; null = enabled. The gateway re-checks and its
 * refusal stays authoritative.
 */
export function trackDisabledReason(
  s: ControlSnapshotLike | null,
  opts: { linkUp: boolean, holder: ControlHolder },
): string | null {
  if (!opts.linkUp || !s) return 'Gateway not reachable'
  if (!s.mount?.connected) return 'Mount not connected'
  if (opts.holder === 'other') return TIP_OTHER_WINDOW
  if (isBusy(s)) return 'Busy'
  if (!isConnected(s)) return 'Axes disabled'
  if (s.state === 'SLEWING') return s.state_reason === 'Avoiding the Sun' ? 'Avoiding the Sun' : 'Slewing' // T3.5, Q12
  if (s.homed?.state === 'not_homed') return s.homed.reason === 'stalled' ? STALLED_REASON : 'Mount not homed'
  const lim = s.limits?.lim_el_deg
  const alt = s.mount?.alt
  const sunAlt = s.sun?.alt
  if (num(lim) && num(alt) && alt < lim) return `Below Lim EL (${lim}°)`
  const sunSep = s.sun_distance_deg
  const keep = s.limits?.sun_keepout_deg ?? KEEPOUT_DEG
  if (num(sunSep) && sunSep <= keep) { // <= like the gateway (safety.py KEEPOUT_DEG)
    const skip = num(sunAlt) && num(lim) && num(alt) && sunShortcut(sunAlt, lim, alt)
    if (!skip) return `Inside the Sun disc (${sunSep.toFixed(1)}° from the Sun)`
  }
  return null
}

/** Why GO TO is disabled (G7), first match wins; null = enabled. A slew in progress does not block it (re-target). */
export function gotoDisabledReason(
  s: ControlSnapshotLike | null,
  opts: { linkUp: boolean, holder: ControlHolder },
): string | null {
  if (!opts.linkUp || !s) return 'Gateway not reachable'
  if (!s.mount?.connected) return 'Mount not connected'
  if (opts.holder === 'other') return TIP_OTHER_WINDOW
  if (isBusy(s)) return 'Busy'
  if (!isConnected(s)) return 'Axes disabled'
  if (s.homed?.state === 'not_homed') return s.homed.reason === 'stalled' ? STALLED_REASON : 'Mount not homed'
  return null
}

/**
 * The "Mount not homed" dialog (Q3): returns the `since` value to show it for, or null. It shows
 * once per `since` per window (`dismissedSince`), only while connected and no sequence runs.
 */
export function homedDialogSince(
  s: ControlSnapshotLike | null,
  opts: { linkUp: boolean, dismissedSince: string | null },
): string | null {
  if (!opts.linkUp || !s || !isNotHomed(s) || isBusy(s)) return null
  const since = s.homed?.since ?? ''
  return since === opts.dismissedSince ? null : since
}

export function buildControlView(
  s: ControlSnapshotLike | null,
  opts: { linkUp: boolean, holder: ControlHolder, refusal?: Refusal | null, now?: number },
): ControlView {
  const linkUp = opts.linkUp && s !== null
  const connected = linkUp && isConnected(s)
  const busy = linkUp && isBusy(s)
  const state = s?.state ?? ''
  const other = opts.holder === 'other'

  const gate = (extra: string | null): ControlButton => {
    const reason = !linkUp ? 'Gateway not reachable' : busy ? 'Busy' : other ? TIP_OTHER_WINDOW : extra
    return { enabled: reason === null, reason }
  }
  const line = statusLine(s, {
    linkUp: opts.linkUp,
    holder: opts.holder,
    refusal: opts.refusal ?? null,
    now: opts.now ?? Date.now(),
  })
  return {
    linkUp,
    connected,
    busy,
    state,
    holder: opts.holder,
    connect: gate(connected ? 'Already connected' : null),
    disconnect: gate(connected ? null : 'Mount not connected'),
    home: gate(connected ? null : 'Mount not connected'),
    track: (() => {
      const reason = trackDisabledReason(s, { linkUp, holder: opts.holder })
      return { enabled: reason === null, reason }
    })(),
    goto: (() => {
      const reason = gotoDisabledReason(s, { linkUp, holder: opts.holder })
      return { enabled: reason === null, reason }
    })(),
    jog: (() => {
      const reason = gotoDisabledReason(s, { linkUp, holder: opts.holder })
      return { enabled: reason === null, reason }
    })(),
    notHomed: linkUp && isNotHomed(s),
    stalled: linkUp && isStalled(s),
    stopEnabled: linkUp && s?.pwi4?.reachable !== false,
    stopLabel: state === 'STOPPED',
    trackingLabel: state === 'TRACKING',
    line,
    takeControl: linkUp && other,
  }
}

/** The ack of a refused / failed command as a status-line refusal; null when the ack was ok. */
export function refusalFromAck(ack: CommandAck, at: number): Refusal | null {
  if (ack.ok) return null
  if (ack.code === CODE_HOME_BLOCKED) return null // handled by the second confirmation
  return { text: ack.reason ?? 'Command refused', at }
}

export function needsHomeConfirm(ack: CommandAck): boolean {
  return !ack.ok && ack.code === CODE_HOME_BLOCKED
}
