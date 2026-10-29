// TRACK OBJECT verdict (T4.1b): maps the answer of POST /api/mount/check {cmd:'track_object'} to the
// button state. The gateway decides everything (pass, limits, TLE age, mount guards); this only parses.

export type TrackPhase = 'track' | 'pre_position'

export interface TrackVerdict {
  enabled: boolean
  /** Why the button is disabled (null when enabled). */
  reason: string | null
  /** What the gateway would do on click (null when refused). */
  phase: TrackPhase | null
  /** Advisory notes from the check (e.g. near-zenith pass); [] when none. */
  notes: string[]
  /** Sun detour the track would need (null when none). */
  detour: { seconds: number, count: number } | null
}

export const GATEWAY_NOT_RESPONDING = 'Gateway not responding'

export const UNREACHABLE_VERDICT: TrackVerdict = { enabled: false, reason: GATEWAY_NOT_RESPONDING, phase: null, notes: [], detour: null }

/** Defensive parse: a malformed body is treated like an unreachable gateway. */
export function parseTrackVerdict(body: unknown): TrackVerdict {
  const b = body as { ok?: unknown, reason?: unknown, pass?: unknown, notes?: unknown, detour?: unknown } | null | undefined
  if (!b || typeof b !== 'object' || typeof b.ok !== 'boolean') return UNREACHABLE_VERDICT
  if (!b.ok) {
    return { enabled: false, reason: typeof b.reason === 'string' && b.reason ? b.reason : 'Command refused', phase: null, notes: [], detour: null }
  }
  const p = b.pass as { phase?: unknown } | null | undefined
  const phase: TrackPhase = p && typeof p === 'object' && p.phase === 'pre_position' ? 'pre_position' : 'track'
  const notes = Array.isArray(b.notes) ? b.notes.filter((n): n is string => typeof n === 'string' && n !== '') : []
  const d = b.detour as { seconds?: unknown, count?: unknown } | null | undefined
  const detour = d && typeof d === 'object' && typeof d.seconds === 'number' && Number.isFinite(d.seconds)
    ? { seconds: d.seconds, count: typeof d.count === 'number' ? d.count : 1 }
    : null
  return { enabled: true, reason: null, phase, notes, detour }
}
