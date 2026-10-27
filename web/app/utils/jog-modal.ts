// JOG MOUNT modal logic (T3.4c, A5c-iii, G7). Pure / timer-driven, unit-tested; the component only wires it.
// Gateway contract: `jog {axis, dir, speed}` on press, re-sent every 100 ms while held (the gateway stops by
// itself after 300 ms without one), `jog_stop {}` on release; `step {axis, dir, size_arcmin}` once per press.
import type { CommandAck, ControlHolder } from './mount-socket'
import { gotoDisabledReason, type ControlSnapshotLike } from './mount-control'

export const JOG_SPEEDS = [0.5, 1.0, 2.0, 5.0, 10.0] as const
export const STEP_SIZES = [1, 5, 10, 30, 60] as const // arcmin
export const DEFAULT_SPEED = 5.0
export const DEFAULT_STEP = 10
export const KEEPALIVE_MS = 100

export type JogMode = 'jog' | 'step'
export type Pad = { axis: 0 | 1, dir: -1 | 1 }

/** Move to the next / previous entry of an ordered list; stays at the ends. */
function stepIn(list: readonly number[], v: number, d: 1 | -1): number {
  const i = list.indexOf(v)
  const from = i < 0 ? list.indexOf(list === JOG_SPEEDS ? DEFAULT_SPEED : DEFAULT_STEP) : i
  return list[Math.max(0, Math.min(list.length - 1, from + d))]!
}
export const stepSpeed = (v: number, d: 1 | -1): number => stepIn(JOG_SPEEDS, v, d)
export const stepSize = (v: number, d: 1 | -1): number => stepIn(STEP_SIZES, v, d)
export const speedAtEnd = (v: number, d: 1 | -1): boolean => stepSpeed(v, d) === v
export const sizeAtEnd = (v: number, d: 1 | -1): boolean => stepSize(v, d) === v

export const fmtSpeed = (v: number): string => v.toFixed(1)
export const fmtSize = (arcmin: number): string => (arcmin >= 60 ? `${arcmin / 60}°` : `${arcmin}′`)

/** Arrow key to pad direction (azimuth: left -1 / right +1; elevation: down -1 / up +1). */
export function keyToPad(key: string): Pad | null {
  switch (key) {
    case 'ArrowLeft': return { axis: 0, dir: -1 }
    case 'ArrowRight': return { axis: 0, dir: 1 }
    case 'ArrowDown': return { axis: 1, dir: -1 }
    case 'ArrowUp': return { axis: 1, dir: 1 }
    default: return null
  }
}

/** Why the whole pad is disabled (G7: same texts as GO TO); null = usable. */
export function jogDisabledReason(
  s: ControlSnapshotLike | null,
  opts: { linkUp: boolean, holder: ControlHolder },
): string | null {
  return gotoDisabledReason(s, opts)
}

export interface JogSnapshotLike {
  jog?: { axis?: number, dir?: number, speed?: number } | null
  step?: { axis?: number, dir?: number, size_arcmin?: number } | null
  /** T3.4e: an azimuth jog stopped at the mechanical limit; a 360° turn is offered. */
  jog_limit?: { axis?: number, dir?: number, limit_deg?: number, turn_deg?: number, est_s?: number } | null
  /** T3.4e: the 360° reposition is running. */
  turn?: { from_deg?: number, to_deg?: number } | null
}

/** The 360° turn is running (the pad is disabled, the prompt hidden). */
export const isTurning = (s: JogSnapshotLike | null): boolean => !!s?.turn

/** The wrap prompt shows while the gateway offers it, no turn runs and the user has not dismissed it. */
export function wrapPromptVisible(s: JogSnapshotLike | null, dismissed: boolean): boolean {
  return !!s?.jog_limit && !s.turn && !dismissed
}

const fmtNum = (v: number | undefined, d: number): string => (typeof v === 'number' && Number.isFinite(v) ? String(Number(v.toFixed(d))) : '?')

export const WRAP_TITLE = 'Azimuth limit reached'
export function wrapPromptText(s: JogSnapshotLike | null): string {
  const l = s?.jog_limit
  const way = (l?.dir ?? 1) < 0 ? 'left' : 'right'
  return `The azimuth axis is at its mechanical limit (±${fmtNum(l?.limit_deg, 1)}°). To keep moving ${way} the mount must turn 360° the other way round (about ${fmtNum(l?.est_s, 0)} s). Turn the mount, or stay here?`
}

const padName = (axis: number, dir: number): string => (axis === 0 ? (dir < 0 ? 'left' : 'right') : (dir < 0 ? 'down' : 'up'))

/** Pad buttons are blocked while a step runs or its ack is pending (STEP mode only). */
export function stepBlocked(s: JogSnapshotLike | null, pending: boolean): boolean {
  return pending || !!s?.step
}

export function jogStatus(o: {
  reason: string | null
  refusal: string | null
  mode: JogMode
  snapshot: JogSnapshotLike | null
  held: Pad | null
  pending: boolean
}): { text: string, tone: 'normal' | 'danger' } {
  const t = o.snapshot?.turn
  if (t) return { text: `Turning 360°${typeof t.to_deg === 'number' ? ` to ${fmtNum(t.to_deg, 1)}°` : ''}…`, tone: 'normal' }
  if (o.reason) return { text: o.reason, tone: 'danger' }
  if (o.refusal) return { text: o.refusal, tone: 'danger' }
  const j = o.snapshot?.jog
  if (j && typeof j.axis === 'number' && typeof j.dir === 'number') {
    return { text: `Jogging ${padName(j.axis, j.dir)}${typeof j.speed === 'number' ? ` at ${fmtSpeed(j.speed)}°/s` : ''}`, tone: 'normal' }
  }
  if (o.held) return { text: `Jogging ${padName(o.held.axis, o.held.dir)}…`, tone: 'normal' }
  const st = o.snapshot?.step
  if (st && typeof st.axis === 'number' && typeof st.dir === 'number') {
    return { text: `Step ${padName(st.axis, st.dir)}${typeof st.size_arcmin === 'number' ? ` ${fmtSize(st.size_arcmin)}` : ''}…`, tone: 'normal' }
  }
  if (o.pending) return { text: 'Step…', tone: 'normal' }
  return { text: 'Ready', tone: 'normal' }
}

export const fmtAz = (v: number | null | undefined): string => (typeof v === 'number' && Number.isFinite(v) ? v.toFixed(2) : '—')
export function fmtEl(v: number | null | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  const t = Math.abs(v).toFixed(2)
  return `${v < 0 && Number(t) !== 0 ? '−' : '+'} ${t}`
}

type Send = (cmd: 'jog' | 'jog_stop', args: Record<string, unknown>) => Promise<CommandAck> | CommandAck

/**
 * Press-and-hold sender. press() sends `jog` at once and every KEEPALIVE_MS after; release() sends
 * `jog_stop` and stops the interval. Only one direction at a time (a second press is ignored). A refused
 * ack ends the hold (no jog_stop: nothing is running) and is reported through onRefusal.
 */
export function createJogSender(send: Send, onRefusal: (reason: string) => void, onChange?: (held: Pad | null) => void) {
  let held: Pad | null = null
  let timer: ReturnType<typeof setInterval> | null = null

  function fire(args: Record<string, unknown>, mine: Pad) {
    void Promise.resolve(send('jog', args)).then((ack) => {
      if (ack.ok || held !== mine) return
      clear()
      onRefusal(ack.reason ?? 'Command refused')
    })
  }
  function clear() {
    if (timer) clearInterval(timer)
    timer = null
    held = null
    onChange?.(null)
  }
  return {
    get held() { return held },
    press(pad: Pad, speed: number): boolean {
      if (held) return false
      const mine: Pad = { axis: pad.axis, dir: pad.dir }
      held = mine
      onChange?.(mine)
      const args = { axis: mine.axis, dir: mine.dir, speed }
      fire(args, mine)
      timer = setInterval(() => fire(args, mine), KEEPALIVE_MS)
      return true
    },
    /** Release; with a pad, only when that pad is the held one (a key-up of another key does nothing). */
    release(pad?: Pad): boolean {
      if (!held) return false
      if (pad && (pad.axis !== held.axis || pad.dir !== held.dir)) return false
      clear()
      void Promise.resolve(send('jog_stop', {}))
      return true
    },
  }
}
