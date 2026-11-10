// Track Info panel states (A1e). Pure: selection + snapshot `track` + now (Unix s) -> what to render.
import { isObjectTrackActive, phaseLine, type ObjectTrackLike } from './track-state'

export interface TrackPanelView {
  /** The full card: the tracked object when pinned, else the selection. */
  main: { norad: number, pinned: boolean } | null
  /** One-line strip for the tracked object while another one is selected. */
  strip: { norad: number, name: string, line: string } | null
  empty: boolean
  /** An aircraft is selected (T5.5): its card is the main card; a tracked satellite shows as the strip. */
  aircraft?: boolean
}

export function trackPanel(
  selectedNorad: number | null,
  track: ObjectTrackLike | null | undefined,
  now: number,
  aircraftSelected = false,
): TrackPanelView {
  const a = track && isObjectTrackActive(track) && typeof track.norad === 'number' ? track : null
  if (aircraftSelected) {
    const strip = a
      ? { norad: a.norad as number, name: a.name || `NORAD ${a.norad}`, line: phaseLine(a, now) ?? '' }
      : null
    return { main: null, strip, empty: false, aircraft: true }
  }
  if (a) {
    const an = a.norad as number
    if (selectedNorad === null || selectedNorad === an) {
      return { main: { norad: an, pinned: true }, strip: null, empty: false }
    }
    return {
      main: { norad: selectedNorad, pinned: false },
      strip: { norad: an, name: a.name || `NORAD ${an}`, line: phaseLine(a, now) ?? '' },
      empty: false,
    }
  }
  if (selectedNorad !== null) return { main: { norad: selectedNorad, pinned: false }, strip: null, empty: false }
  return { main: null, strip: null, empty: true }
}
