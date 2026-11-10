// Aircraft display logic (T5.5), pure: frame bookkeeping + dead-reckoning for the Sky Viewer layer,
// icon rotation, labels, the A1b card formats, the TRACK AIRCRAFT verdict and the filter enable rule.
import type { AdsbInfo, AircraftRow } from './adsb'
import { fmtThousands } from './format'
import { formatAz, formatEl } from './track-format'

export const EM_DASH = '—'
const MINUS = '−'

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

// ---------------------------------------------------------------- frames + interpolation

/** Never extrapolate further than this past the frame a position anchors on (user spec: 2 s). */
export const MAX_AHEAD_S = 2
/** A rate is only derived from two distinct positions this far apart (s). */
export const RATE_MIN_DT_S = 0.2
export const RATE_MAX_DT_S = 5

export interface AzElRate { daz: number, del: number }

/** One aircraft as the store keeps it: the latest row + the position anchor the chart extrapolates from. */
export interface TrackedAircraft {
  row: AircraftRow
  /** Anchor (the last DISTINCT position) and the wall time (ms) it arrived. */
  az: number
  el: number
  t: number
  /** deg/s from the last two distinct positions; null until a second one arrives. */
  rate: AzElRate | null
}

/** Shortest signed azimuth difference b - a, in (-180, 180]. */
export function azDiff(a: number, b: number): number {
  let d = (b - a) % 360
  if (d > 180) d -= 360
  else if (d <= -180) d += 360
  return d
}

export const normAz = (a: number): number => ((a % 360) + 360) % 360

/**
 * Fold a 1 Hz frame into the tracked map. A row whose az/el equals the previous frame's (the feed
 * had no new fix) keeps the old anchor and rate, so the dead-reckoning carries on instead of
 * freezing and then jumping; a moved row re-anchors and derives the rate from the two positions.
 */
export function ingestFrame(
  prev: ReadonlyMap<string, TrackedAircraft>,
  rows: readonly AircraftRow[],
  tMs: number,
): Map<string, TrackedAircraft> {
  const out = new Map<string, TrackedAircraft>()
  for (const row of rows) {
    if (!row || typeof row.icao !== 'string' || !finite(row.az) || !finite(row.el)) continue
    const old = prev.get(row.icao)
    if (old && old.az === row.az && old.el === row.el) {
      out.set(row.icao, { ...old, row })
      continue
    }
    let rate: AzElRate | null = null
    if (old) {
      const dt = (tMs - old.t) / 1000
      if (dt >= RATE_MIN_DT_S && dt <= RATE_MAX_DT_S) {
        rate = { daz: azDiff(old.az, row.az) / dt, del: (row.el - old.el) / dt }
      }
    }
    out.set(row.icao, { row, az: row.az, el: row.el, t: tMs, rate })
  }
  return out
}

/** Az / el at wall time `nowMs`: the anchor plus rate x min(elapsed, 2 s). */
export function predictAzEl(tr: TrackedAircraft, nowMs: number): { az: number, el: number } {
  if (!tr.rate) return { az: tr.az, el: tr.el }
  const h = Math.min(MAX_AHEAD_S, Math.max(0, (nowMs - tr.t) / 1000))
  return { az: normAz(tr.az + tr.rate.daz * h), el: tr.el + tr.rate.del * h }
}

// ---------------------------------------------------------------- chart helpers

/** Faded when stale (no position for 10 s, gateway flag). */
export const AIRCRAFT_STALE_ALPHA = 0.4
/** Icon side in css px at the reference chart size (scaled like the satellite squares). Larger than
 *  the satellites (user 11-10): aircraft traffic is sparse. ~25 px at 1920 x 1080, ~16 px at 1280 x 600 @1.5. */
export const AIRCRAFT_ICON_PX = 20
export const AIRCRAFT_PICK_RADIUS_PX = 14
/** The icon outline (public/icons/aircraft.svg's path, 24 x 24, nose up), drawn as a vector so it can
 *  carry a fill + an outline in colours chosen against the sky. */
export const AIRCRAFT_PATH = 'M21 15.5v-1.8l-7.6-4.8V3.6c0-.8-.6-1.6-1.4-1.6s-1.4.8-1.4 1.6v5.3L3 13.7v1.8l7.6-2.4v5.2l-2 1.5V21l3.4-1 3.4 1v-1.2l-2-1.5v-5.2z'
export const AIRCRAFT_PATH_SIZE = 24
/** Outline width in css px (each side of the edge). */
export const AIRCRAFT_OUTLINE_PX = 1.5

/** Fill + outline against the sky (user 11-10: the outline must stand out from the sky colour). The
 *  sky runs from dark night to light orange day (`skyBrightness` 0..1): a light sky gets a light icon
 *  with a dark outline, a dark sky a dark icon with a light outline. */
export function aircraftIconColors(skyBright: number): { fill: string, outline: string } {
  return skyBright >= 0.5
    ? { fill: '#f4f6f8', outline: '#161c24' }
    : { fill: '#262c35', outline: '#eef1f4' }
}

/**
 * Canvas rotation (radians, clockwise on screen) for the aircraft icon, whose nose points UP (north
 * on the chart). The chart is a view of the sky from below: N up and E to the LEFT (x = cx - r sin az),
 * so a ground track of theta (compass, clockwise from north) points to the screen vector
 * (-sin theta, -cos theta) = the up vector rotated by -theta. A missing track leaves the icon upright.
 */
export function iconRotationRad(trackDeg: number | null | undefined): number {
  if (!finite(trackDeg)) return 0
  return (-trackDeg * Math.PI) / 180
}

/** The chart label: callsign when known, else the ICAO. */
export function aircraftLabel(row: Pick<AircraftRow, 'callsign' | 'icao'>): string {
  const c = typeof row.callsign === 'string' ? row.callsign.trim() : ''
  return c !== '' ? c : row.icao
}

/** `CALLSIGN (ICAO)`, or just the ICAO without a callsign. */
export function aircraftTitle(row: Pick<AircraftRow, 'callsign' | 'icao'>): string {
  const c = typeof row.callsign === 'string' ? row.callsign.trim() : ''
  return c !== '' ? `${c} (${row.icao})` : row.icao
}

// ---------------------------------------------------------------- A1b card formats

/** `13.69510° N` (.5f with the cardinal). */
export function formatLat(v: number | null | undefined): string {
  return finite(v) ? `${Math.abs(v).toFixed(5)}° ${v < 0 ? 'S' : 'N'}` : EM_DASH
}

/** `100.74820° E`. */
export function formatLon(v: number | null | undefined): string {
  return finite(v) ? `${Math.abs(v).toFixed(5)}° ${v < 0 ? 'W' : 'E'}` : EM_DASH
}

/** `35 000 ft` (.0f). */
export function formatAltFt(v: number | null | undefined): string {
  return finite(v) ? `${fmtThousands(Math.round(v).toString())} ft` : EM_DASH
}

/** `23.456 km` (.3f) from metres. */
export function formatRangeKm(rangeM: number | null | undefined): string {
  return finite(rangeM) ? `${fmtThousands((rangeM / 1000).toFixed(3))} km` : EM_DASH
}

/** `451 kt` (.0f). */
export function formatKt(v: number | null | undefined): string {
  return finite(v) ? `${Math.round(v)} kt` : EM_DASH
}

/** Compass track, 0-359 degrees. */
export function formatTrack(v: number | null | undefined): string {
  if (!finite(v)) return EM_DASH
  const r = Math.round(normAz(v)) % 360
  return `${r}°`
}

/** Signed `+1 500 fpm` / `−640 fpm`; level flight is `0 fpm`. */
export function formatVRate(v: number | null | undefined): string {
  if (!finite(v)) return EM_DASH
  const r = Math.round(v)
  if (r === 0) return '0 fpm'
  return `${r < 0 ? MINUS : '+'}${fmtThousands(Math.abs(r).toString())} fpm`
}

/** `1.2 s`. */
export function formatAgeS(v: number | null | undefined): string {
  return finite(v) ? `${v.toFixed(1)} s` : EM_DASH
}

/** The "baro" flag: the height is barometric only (no GNSS height or difference). Other sources: no flag. */
export function heightSourceFlag(src: string | null | undefined): { label: string, title: string } | null {
  if (src === 'baro') return { label: 'BARO', title: 'Barometric height only (no GNSS height or GNSS-baro difference)' }
  return null
}

export interface AircraftCardValues {
  title: string
  lat: string
  lon: string
  alt: string
  range: string
  az: string
  el: string
  gs: string
  track: string
  vrate: string
  age: string
  flag: { label: string, title: string } | null
}

/** All card strings for one row (a null row = every value is an em dash, the title falls back to the ICAO). */
export function aircraftCardValues(row: AircraftRow | null | undefined, icao: string): AircraftCardValues {
  if (!row) {
    return {
      title: icao, lat: EM_DASH, lon: EM_DASH, alt: EM_DASH, range: EM_DASH, az: EM_DASH, el: EM_DASH,
      gs: EM_DASH, track: EM_DASH, vrate: EM_DASH, age: EM_DASH, flag: null,
    }
  }
  return {
    title: aircraftTitle(row),
    lat: formatLat(row.lat),
    lon: formatLon(row.lon),
    alt: formatAltFt(row.alt_ft),
    range: formatRangeKm(row.range_m),
    az: formatAz(row.az),
    el: formatEl(row.el),
    gs: formatKt(row.gs_kt),
    track: formatTrack(row.track_deg),
    vrate: formatVRate(row.vrate_fpm),
    age: formatAgeS(row.pos_age_s),
    flag: heightSourceFlag(row.height_source),
  }
}

// ---------------------------------------------------------------- TRACK AIRCRAFT verdict

export const AIRCRAFT_TRACK_PENDING = 'Aircraft tracking: coming in T5.6b'
export const AIRCRAFT_NOT_IN_VIEW = 'Aircraft not in view'
export const ADSB_STATUS_UNAVAILABLE = 'ADS-B status unavailable'

export interface AircraftVerdict { enabled: boolean, reason: string | null }

/**
 * The one place that decides TRACK AIRCRAFT (card and Track Target tab). T5.6b replaces the final
 * branch with the gateway check (`/api/mount/check` track_aircraft); until then the button is present
 * but disabled with a fixed reason.
 */
export function aircraftTrackVerdict(row: AircraftRow | null | undefined, adsb: AdsbInfo | null | undefined): AircraftVerdict {
  if (!adsb) return { enabled: false, reason: ADSB_STATUS_UNAVAILABLE }
  if (!adsb.green) return { enabled: false, reason: adsb.detail || 'ADS-B feed down' }
  if (!row) return { enabled: false, reason: AIRCRAFT_NOT_IN_VIEW }
  return { enabled: false, reason: AIRCRAFT_TRACK_PENDING }
}

// ---------------------------------------------------------------- Filters / chips

/** Filters > Aircrafts is usable only on a green ADS-B connection; otherwise `reason` is its tooltip. */
export function aircraftFilterState(adsb: AdsbInfo | null | undefined): { enabled: boolean, reason: string | null } {
  if (!adsb) return { enabled: false, reason: ADSB_STATUS_UNAVAILABLE }
  if (adsb.green) return { enabled: true, reason: null }
  return { enabled: false, reason: adsb.detail || 'ADS-B feed down' }
}

/** "ADS-B feed down" chip: ADS-B is in use (enabled / simulated) but not green. Off, not configured or module missing: no chip. */
export function feedDownChip(adsb: AdsbInfo | null | undefined): { show: boolean, title: string } {
  if (!adsb || adsb.green) return { show: false, title: '' }
  const quiet = adsb.state === 'disabled' || adsb.state === 'not_configured' || adsb.state === 'module_missing'
  return { show: !quiet, title: adsb.detail || 'ADS-B feed down' }
}

/** "SIMULATED DATA" badge. */
export function showSimulatedBadge(adsb: AdsbInfo | null | undefined): boolean {
  return !!adsb && adsb.simulated === true && adsb.green === true
}
