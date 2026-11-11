// Aircraft-track state for the A1b card and the Track Target AIRCRAFT tab (T5.6b). Pure: maps the snapshot's
// `airtrack` field (gateway contract; times in Unix seconds) + the selected ICAO to the one-line status.
// The gateway decides everything (rules, rendezvous, ends); this only words it.
import { parseTrackVerdict, type TrackVerdict } from './track-verdict'

export type AirTrackPhase = 'slewing' | 'waiting' | 'tracking'

export interface AirTrackLike {
  icao?: string | null
  callsign?: string | null
  phase?: AirTrackPhase | string | null
  t_r?: number | null
  notes?: string[] | null
  /** Set for 30 s after a track ended (the other fields are null then). */
  ended?: { icao?: string | null, reason?: string | null, at?: number | null } | null
}

export interface AirCardState {
  /** This card's aircraft is the one being tracked (any phase): the button stays disabled. */
  isTracked: boolean
  /** The phase text while tracked, the ended reason for 30 s after (null = nothing to show). */
  line: string | null
}

export const AIR_PHASE_LINES: Record<string, string> = {
  slewing: 'Slewing to rendezvous',
  waiting: 'Waiting for the aircraft',
  tracking: 'Tracking',
}

/** An aircraft track is running (any phase). */
export const isAirTrackActive = (t: AirTrackLike | null | undefined): boolean => !!t && !!t.phase

/** The gateway check of `track_aircraft` is parsed like the satellite one (ok / reason / notes). */
export const parseAircraftVerdict = (body: unknown): TrackVerdict => parseTrackVerdict(body)

export function airTrackCardState(air: AirTrackLike | null | undefined, icao: string | null): AirCardState {
  const none: AirCardState = { isTracked: false, line: null }
  if (!air || icao === null) return none
  if (air.phase && air.icao === icao) {
    const base = AIR_PHASE_LINES[air.phase] ?? null
    const note = air.notes?.[0]
    return { isTracked: true, line: base && note ? `${base} · ${note}` : base }
  }
  if (!air.phase && air.ended && air.ended.icao === icao && air.ended.reason) {
    return { isTracked: false, line: air.ended.reason }
  }
  return none
}
