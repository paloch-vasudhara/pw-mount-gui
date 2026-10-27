// GO TO COORDINATES modal rules (T3.4a, logic sheet A5c-iv / H2 / G7). Pure functions, unit-tested.
// The gateway decides everything (POST /api/mount/check is the same verdict as the real command);
// this file only holds the group switching, the command to send, the red-box rule and the status line.

export type GotoGroup = 'local' | 'celestial'
export type GotoField = 'az' | 'el' | 'ra' | 'dec'

/** Canonical units: degrees for az / el / dec, HOURS for ra (the AngleField 'ra' kind). */
export interface GotoValues { az: number, el: number, ra: number, dec: number }
export interface GotoState extends GotoValues { active: GotoGroup }

export const EL_MIN = 0
export const EL_MAX = 90

export const FIELD_GROUP: Record<GotoField, GotoGroup> = { az: 'local', el: 'local', ra: 'celestial', dec: 'celestial' }

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Local elevation is capped 0-90 deg (scrolling cannot go below 0). */
export function capElevation(el: number): number {
  if (!fin(el)) return EL_MIN
  return Math.min(EL_MAX, Math.max(EL_MIN, el))
}

export function wrapAz(az: number): number {
  const r = az % 360
  return r < 0 ? r + 360 : r
}

/** An edit of `field` makes its group the active one (the last edited group is sent). */
export function editField(state: GotoState, field: GotoField, value: number): GotoState {
  const group = FIELD_GROUP[field]
  const next: GotoState = { ...state, [field]: value, active: group }
  // A computed (inactive) local pair may hold a negative elevation: once it becomes the sent pair
  // it obeys the 0-90 cap.
  if (group === 'local') next.el = capElevation(next.el)
  return next
}

/**
 * A click anywhere in a group's area makes it active. Values are NOT touched (not even the 0-90
 * elevation cap: an out-of-range computed elevation is reported by quickCheck instead).
 */
export function activateGroup(state: GotoState, group: GotoGroup): GotoState {
  return state.active === group ? state : { ...state, active: group }
}

/** The initial pair when the modal opens: the current pointing, Local active. */
export function initialState(mount: { az?: number | null, alt?: number | null, ra_j2000_h?: number | null, dec_j2000_deg?: number | null } | null | undefined): GotoState {
  const az = fin(mount?.az) ? wrapAz(mount.az) : 0
  const el = capElevation(fin(mount?.alt) ? mount.alt : 45)
  const ra = fin(mount?.ra_j2000_h) ? mount.ra_j2000_h : 0
  const dec = fin(mount?.dec_j2000_deg) ? mount.dec_j2000_deg : 0
  return { active: 'local', az, el, ra, dec }
}

export interface GotoCommand {
  cmd: 'goto_altaz' | 'goto_radec_j2000'
  args: Record<string, number>
}

/** The command SLEW sends: the active group's pair (Local ends STOPPED, Celestial ends TRACKING). */
export function buildGotoCommand(state: GotoState): GotoCommand {
  if (state.active === 'local') return { cmd: 'goto_altaz', args: { alt: state.el, az: wrapAz(state.az) } }
  return { cmd: 'goto_radec_j2000', args: { ra_h: state.ra, dec_deg: state.dec } }
}

/** The body of POST /api/mount/check (same cmd + args as the real command). */
export function checkBody(state: GotoState): GotoCommand {
  return buildGotoCommand(state)
}

export interface TransformRequest {
  dir: 'altaz_to_radec' | 'radec_to_altaz'
  t_utc: string
  values: Record<string, number>
}

/** The H4 request for the INACTIVE group, at `nowMs` (Goto passes the current time). */
export function buildTransformRequest(state: GotoState, nowMs: number): TransformRequest {
  const t_utc = new Date(nowMs).toISOString()
  if (state.active === 'local') return { dir: 'altaz_to_radec', t_utc, values: { alt: state.el, az: wrapAz(state.az) } }
  return { dir: 'radec_to_altaz', t_utc, values: { ra_h: state.ra, dec_deg: state.dec } }
}

/** Writes a transform result into the inactive group; the active group is never touched. null = unusable result. */
export function applyTransformResult(state: GotoState, result: unknown): GotoState | null {
  const r = result as Record<string, unknown> | null | undefined
  if (!r || typeof r !== 'object') return null
  if (state.active === 'local') {
    if (!fin(r.ra_h) || !fin(r.dec_deg)) return null
    return { ...state, ra: ((r.ra_h % 24) + 24) % 24, dec: r.dec_deg }
  }
  if (!fin(r.alt) || !fin(r.az)) return null
  return { ...state, el: r.alt, az: wrapAz(r.az) }
}

/** Client-side quick check (no flicker, no round trip): null = nothing obviously wrong. */
export function quickCheck(state: GotoState): string | null {
  const vals = state.active === 'local' ? [state.az, state.el] : [state.ra, state.dec]
  if (!vals.every(fin)) return 'Enter valid coordinates'
  if (state.active === 'local' && (state.el < EL_MIN || state.el > EL_MAX)) return 'Elevation must be between 0° and 90°'
  if (state.active === 'celestial' && Math.abs(state.dec) > 90) return 'Declination must be between -90° and +90°'
  return null
}

// ----- gateway check ---------------------------------------------------------------------------

export type CheckResult =
  | { kind: 'pending' }
  | { kind: 'ok' }
  | { kind: 'refused', reason: string, code?: string | null }
  | { kind: 'unreachable' }

export const CANNOT_VERIFY = 'Cannot verify the target'

/** Maps the check endpoint's JSON to a CheckResult; anything else is unreachable. */
export function parseCheck(body: unknown): CheckResult {
  const b = body as { ok?: unknown, reason?: unknown, code?: unknown } | null | undefined
  if (!b || typeof b !== 'object' || typeof b.ok !== 'boolean') return { kind: 'unreachable' }
  if (b.ok) return { kind: 'ok' }
  return {
    kind: 'refused',
    reason: typeof b.reason === 'string' && b.reason ? b.reason : 'Command refused',
    code: typeof b.code === 'string' ? b.code : null,
  }
}

/**
 * Red-box rule: the local Elevation box(es) turn red when the refusal is about the TARGET's
 * elevation (Lim EL / below the horizon limit / inside the Sun disc), for either active group. A
 * slew-path refusal ("Slew path passes within 15° of the Sun") leaves the boxes normal.
 */
export function elevationInvalid(check: CheckResult): boolean {
  if (check.kind !== 'refused') return false
  // The gateway's codes (T3.4a): target elevation refusals turn the box red, the path does not.
  if (check.code) return check.code === 'below_lim_el' || check.code === 'sun_target'
  const text = `${check.code ?? ''} ${check.reason}`
  if (/path/i.test(text)) return false
  return /lim[ _]?el|limit|horizon|below|sun/i.test(text)
}

// ----- slew tracking + status line -------------------------------------------------------------

export interface SlewTrack {
  kind: GotoGroup
  ackAt: number
  sawSlewing: boolean
}

/** How long after an accepted ack "Slewing…" is shown before the snapshot state is believed (ms). */
export const SLEW_GRACE_MS = 3_000

export function startSlewTrack(kind: GotoGroup, now: number): SlewTrack {
  return { kind, ackAt: now, sawSlewing: false }
}

/** Remembers that the snapshot reported SLEWING (so a later STOPPED / TRACKING means "arrived"). */
export function observeState(track: SlewTrack | null, state: string | null | undefined): SlewTrack | null {
  if (!track || track.sawSlewing || state !== 'SLEWING') return track
  return { ...track, sawSlewing: true }
}

/** The slew-follow line: Slewing… -> Arrived (STOPPED) / Tracking. null = fall through to the check line. */
export function slewLine(track: SlewTrack, state: string | null | undefined, now: number): string | null {
  if (state === 'SLEWING') return 'Slewing…'
  if (!track.sawSlewing && now - track.ackAt < SLEW_GRACE_MS) return 'Slewing…'
  if (state === 'TRACKING') return 'Tracking'
  if (state === 'STOPPED') return track.kind === 'local' ? 'Arrived (STOPPED)' : 'Stopped'
  return null
}

export interface GotoStatusInput {
  /** Disabled reason of the GO TO button (not homed / stalled / other window / busy / link). */
  gateReason: string | null
  quick: string | null
  /** Last refused ack of SLEW (cleared on any edit). */
  ackRefusal: string | null
  sending: boolean
  slew: SlewTrack | null
  state: string | null | undefined
  check: CheckResult
  now: number
}

export interface GotoStatus {
  text: string
  tone: 'normal' | 'danger'
  /** SLEW TO COORDINATES enabled. */
  canSlew: boolean
}

export function gotoStatus(i: GotoStatusInput): GotoStatus {
  const canSlew = i.gateReason === null && i.quick === null && i.check.kind === 'ok' && !i.sending
  if (i.gateReason) return { text: i.gateReason, tone: 'danger', canSlew: false }
  if (i.quick) return { text: i.quick, tone: 'danger', canSlew: false }
  if (i.ackRefusal) return { text: i.ackRefusal, tone: 'danger', canSlew }
  if (i.sending) return { text: 'Sending…', tone: 'normal', canSlew: false }
  if (i.slew) {
    const line = slewLine(i.slew, i.state, i.now)
    if (line) return { text: line, tone: 'normal', canSlew }
  }
  switch (i.check.kind) {
    case 'ok': return { text: 'Ready', tone: 'normal', canSlew }
    case 'refused': return { text: i.check.reason, tone: 'danger', canSlew: false }
    case 'unreachable': return { text: CANNOT_VERIFY, tone: 'danger', canSlew: false }
    default: return { text: 'Checking…', tone: 'normal', canSlew: false }
  }
}
