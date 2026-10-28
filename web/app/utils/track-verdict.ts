// TRACK OBJECT verdict (T4.1b): maps the answer of POST /api/mount/check {cmd:'track_object'} to the
// button state. The gateway decides everything (pass, limits, TLE age, mount guards); this only parses.

export type TrackPhase = 'track' | 'pre_position'

export interface TrackVerdict {
  enabled: boolean
  /** Why the button is disabled (null when enabled). */
  reason: string | null
  /** What the gateway would do on click (null when refused). */
  phase: TrackPhase | null
}

export const GATEWAY_NOT_RESPONDING = 'Gateway not responding'

export const UNREACHABLE_VERDICT: TrackVerdict = { enabled: false, reason: GATEWAY_NOT_RESPONDING, phase: null }

/** Defensive parse: a malformed body is treated like an unreachable gateway. */
export function parseTrackVerdict(body: unknown): TrackVerdict {
  const b = body as { ok?: unknown, reason?: unknown, pass?: unknown } | null | undefined
  if (!b || typeof b !== 'object' || typeof b.ok !== 'boolean') return UNREACHABLE_VERDICT
  if (!b.ok) {
    return { enabled: false, reason: typeof b.reason === 'string' && b.reason ? b.reason : 'Command refused', phase: null }
  }
  const p = b.pass as { phase?: unknown } | null | undefined
  const phase: TrackPhase = p && typeof p === 'object' && p.phase === 'pre_position' ? 'pre_position' : 'track'
  return { enabled: true, reason: null, phase }
}
