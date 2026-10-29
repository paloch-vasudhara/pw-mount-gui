// Object-track state for the Track Info card (T4.2e). Pure: maps the snapshot's `track` field (gateway
// contract, times in Unix seconds) + the selected NORAD + now to the button label and the one-line status.
import type { TrackVerdict } from './track-verdict'

export type ObjectTrackPhase = 'slewing' | 'waiting' | 'tracking' | 'detour'

export interface ObjectTrackLike {
  norad?: number | null
  name?: string | null
  phase?: ObjectTrackPhase | string | null
  t_r?: number | null
  t_end?: number | null
  t_peak?: number | null
  peak_alt?: number | null
  detours?: Array<{ t_a?: number, t_b?: number, side?: string }> | null
  notes?: string[] | null
  ended?: { norad?: number | null, reason?: string | null, at?: number | null } | null
}

export interface TrackCardState {
  /** This card's object is the one being tracked (phase active). */
  isTrackedCard: boolean
  buttonLabel: 'TRACK OBJECT' | 'TRACKING'
  /** Only the verdict can enable it; never while this object is tracked. */
  buttonEnabled: boolean
  /** The single reason/status line (null = nothing to show). */
  line: string | null
}

export const OBJECT_TRACKING_JOG_REASON = 'Object tracking; press STOP first'

const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** An object track is running (any phase). */
export const isObjectTrackActive = (t: ObjectTrackLike | null | undefined): boolean => !!t && !!t.phase

/** mm:ss of a non-negative duration (rounded up so "00:00" only shows at the event). */
export function fmtCountdown(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds))
  const m = Math.floor(s / 60)
  return `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

export function phaseLine(t: ObjectTrackLike, now: number): string | null {
  switch (t.phase) {
    case 'slewing': return 'Slewing to the pass'
    case 'waiting': return num(t.t_r) ? `Waiting for the pass — ${fmtCountdown(t.t_r - now)}` : 'Waiting for the pass'
    case 'tracking': return num(t.t_end) ? `Tracking — ends in ${fmtCountdown(t.t_end - now)}` : 'Tracking'
    case 'detour': return 'Avoiding the Sun'
    default: return null
  }
}

export function trackCardState(
  track: ObjectTrackLike | null | undefined,
  norad: number | null,
  now: number,
  verdict: Pick<TrackVerdict, 'enabled' | 'reason' | 'notes'> | null = null,
): TrackCardState {
  const verdictEnabled = verdict?.enabled === true
  const idle: TrackCardState = {
    isTrackedCard: false,
    buttonLabel: 'TRACK OBJECT',
    buttonEnabled: verdictEnabled,
    line: verdict ? (verdict.reason ?? verdict.notes?.[0] ?? null) : null,
  }
  if (norad === null || !track) return idle
  if (track.phase && track.norad === norad) {
    const base = phaseLine(track, now)
    const note = track.notes?.[0]
    return {
      isTrackedCard: true,
      buttonLabel: 'TRACKING',
      buttonEnabled: false,
      line: base && note ? `${base} · ${note}` : base,
    }
  }
  if (!track.phase && track.ended && track.ended.norad === norad && track.ended.reason) {
    return { ...idle, line: track.ended.reason }
  }
  return idle
}
