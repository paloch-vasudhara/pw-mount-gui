// Station Settings (T1.2): types, form validation and payload building (pure, unit-tested).
import type { AdsbInfo } from '@/utils/adsb'
import { ADSB_DEFAULT_RANGE_KM, ADSB_DEFAULT_URL, adsbTokenError, adsbUrlError } from '@/utils/adsb'
import type { AltUnit } from '@/utils/altitude'

export type AngleFmt = 'dd' | 'dms'

export interface StationSettingsDto {
  version: number
  location: { lat_deg: number, lon_deg: number, alt_m: number, timezone: string | null }
  hardware: { mount: string, ota: string, lim_el_deg: number, camera_pixel_um?: number | null }
  providers: { astrometry: string, satellite_db: string }
  display: { lat_format: AngleFmt, lon_format: AngleFmt, alt_unit: AltUnit }
  /** T5.7: the camera URL / username / password are write-only; only whether each is set comes back. */
  cctv?: CctvSettingsDto
  /** T5.4b: the platform read token is write-only; only whether it is set comes back. */
  adsb?: AdsbSettingsDto
}

export interface AdsbSettingsDto {
  enabled: boolean
  url: string
  range_limit_km: number
  token_set: boolean
  /** The gateway runs on adsb_interpreter's simulated feed (--simulated-adsb): the live fields are inactive. */
  simulated?: boolean
  status?: AdsbInfo
}

export interface CctvSettingsDto {
  enabled: boolean
  type: 'mjpeg' | 'hls'
  url_set: boolean
  username_set: boolean
  password_set: boolean
  configured: boolean
  /** Not configured / Connecting / Live / Unreachable: reason */
  status: string
}

export interface SiteCheckDto {
  pwi4_reachable: boolean
  ok: boolean | null
  lat_diff_deg: number | null
  lon_diff_deg: number | null
  alt_diff_m: number | null
  tolerance: { lat_lon_deg: number, alt_m: number }
}

export type SettingsResponse = StationSettingsDto & { api_key_set: boolean, site_check?: SiteCheckDto }

export interface SettingsForm {
  lat: number | null
  lon: number | null
  altM: number | null
  timezone: string
  limEl: number | null
  /** Camera pixel size (um); null = unknown (T5.2: the astrometry scale hint). */
  cameraPixelUm: number | null
  latFormat: AngleFmt
  lonFormat: AngleFmt
  altUnit: AltUnit
  mount: string
  ota: string
  astrometry: string
  satelliteDb: string
  /** What the user typed in the API key box (empty = untouched). */
  apiKey: string
  /** "clear key" was pressed. */
  clearKey: boolean
  /** T5.7 CCTV: enable, type, and the write-only url / username / password (empty = untouched). */
  cctvEnabled: boolean
  cctvType: 'mjpeg' | 'hls'
  cctvUrl: string
  cctvUser: string
  cctvPass: string
  /** Whether the gateway already holds each (placeholders + the clear links). */
  cctvUrlSet: boolean
  cctvUserSet: boolean
  cctvPassSet: boolean
  clearCctvUrl: boolean
  clearCctvUser: boolean
  clearCctvPass: boolean
  /** T5.4b ADS-B: enable, platform URL, default range limit and the write-only read token. */
  adsbEnabled: boolean
  adsbUrl: string
  adsbRangeKm: number
  /** What the user typed in the read-token box (empty = untouched). */
  adsbToken: string
  adsbTokenSet: boolean
  clearAdsbToken: boolean
}

export type FieldErrors = Partial<Record<'lat' | 'lon' | 'alt' | 'timezone' | 'limEl' | 'cameraPixelUm' | 'cctvUrl' | 'adsbUrl' | 'adsbToken', string>>

export const LIM_EL_MIN = 0
export const LIM_EL_MAX = 90
export const PIXEL_UM_MIN = 0.5
export const PIXEL_UM_MAX = 30
export const ALT_MIN_M = -500
export const ALT_MAX_M = 9000

export function formFromDto(d: StationSettingsDto): SettingsForm {
  return {
    lat: d.location.lat_deg,
    lon: d.location.lon_deg,
    altM: d.location.alt_m,
    timezone: d.location.timezone ?? '',
    limEl: d.hardware.lim_el_deg,
    cameraPixelUm: d.hardware.camera_pixel_um ?? null,
    latFormat: d.display.lat_format,
    lonFormat: d.display.lon_format,
    altUnit: d.display.alt_unit,
    mount: d.hardware.mount,
    ota: d.hardware.ota,
    astrometry: d.providers.astrometry,
    satelliteDb: d.providers.satellite_db,
    apiKey: '',
    clearKey: false,
    cctvEnabled: d.cctv?.enabled ?? false,
    cctvType: d.cctv?.type ?? 'mjpeg',
    cctvUrl: '',
    cctvUser: '',
    cctvPass: '',
    cctvUrlSet: d.cctv?.url_set ?? false,
    cctvUserSet: d.cctv?.username_set ?? false,
    cctvPassSet: d.cctv?.password_set ?? false,
    clearCctvUrl: false,
    clearCctvUser: false,
    clearCctvPass: false,
    adsbEnabled: d.adsb?.enabled ?? false,
    adsbUrl: d.adsb?.url ?? ADSB_DEFAULT_URL,
    adsbRangeKm: d.adsb?.range_limit_km ?? ADSB_DEFAULT_RANGE_KM,
    adsbToken: '',
    adsbTokenSet: d.adsb?.token_set ?? false,
    clearAdsbToken: false,
  }
}

/** Why a typed camera URL is refused (mirrors the gateway), else null. */
export function cctvUrlError(url: string): string | null {
  const v = url.trim()
  if (v === '') return null
  if (/\s/.test(v)) return 'The URL must not contain spaces'
  let u: URL
  try {
    u = new URL(v)
  }
  catch {
    return 'Enter a full URL, e.g. http://192.168.1.20/mjpeg'
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return 'Only http:// or https:// (RTSP goes through the relay)'
  if (u.username || u.password || /^[a-z]+:\/\/[^/?#]*@/i.test(v)) return 'Put the login in Username / Password, not in the URL'
  return null
}

function inRange(v: number | null, lo: number, hi: number): boolean {
  return v != null && Number.isFinite(v) && v >= lo && v <= hi
}

export function validateForm(f: SettingsForm, zones: readonly string[]): FieldErrors {
  const e: FieldErrors = {}
  if (!inRange(f.lat, -90, 90)) e.lat = 'Latitude must be between -90 and 90'
  if (!inRange(f.lon, -180, 180)) e.lon = 'Longitude must be between -180 and 180'
  if (!inRange(f.altM, ALT_MIN_M, ALT_MAX_M)) e.alt = 'Altitude must be between -500 and 9000 m'
  if (!f.timezone || !zones.includes(f.timezone)) e.timezone = 'Choose a time zone from the list'
  if (!inRange(f.limEl, LIM_EL_MIN, LIM_EL_MAX)) e.limEl = 'Lim EL must be between 0 and 90'
  if (f.cameraPixelUm != null && !inRange(f.cameraPixelUm, PIXEL_UM_MIN, PIXEL_UM_MAX)) e.cameraPixelUm = 'Camera pixel size must be between 0.5 and 30 µm'
  const urlErr = cctvUrlError(f.cctvUrl)
  if (urlErr) e.cctvUrl = urlErr
  else if (f.cctvEnabled && f.cctvUrl.trim() === '' && (!f.cctvUrlSet || f.clearCctvUrl)) e.cctvUrl = 'Enter the camera stream URL'
  const adsbUrlErr = adsbUrlError(f.adsbUrl)
  if (adsbUrlErr) e.adsbUrl = adsbUrlErr
  else if (f.adsbEnabled && f.adsbUrl.trim() === '') e.adsbUrl = 'Enter the ADS-B platform URL'
  const adsbTokenErr = adsbTokenError(f.adsbToken)
  if (adsbTokenErr) e.adsbToken = adsbTokenErr
  return e
}

/** PUT body. `api_key` is present only when the user typed one ("" = clear), else omitted = keep. */
export function buildPayload(f: SettingsForm): Record<string, unknown> {
  const body: Record<string, unknown> = {
    version: 1,
    location: { lat_deg: f.lat, lon_deg: f.lon, alt_m: f.altM, timezone: f.timezone },
    hardware: { mount: f.mount, ota: f.ota, lim_el_deg: f.limEl, camera_pixel_um: f.cameraPixelUm },
    providers: { astrometry: f.astrometry, satellite_db: f.satelliteDb },
    display: { lat_format: f.latFormat, lon_format: f.lonFormat, alt_unit: f.altUnit },
  }
  if (f.clearKey) body.api_key = ''
  else if (f.apiKey !== '') body.api_key = f.apiKey
  // CCTV url / username / password are sent only when typed or cleared; an omitted field is kept.
  const cctv: Record<string, unknown> = { enabled: f.cctvEnabled, type: f.cctvType }
  if (f.clearCctvUrl) cctv.url = ''
  else if (f.cctvUrl.trim() !== '') cctv.url = f.cctvUrl.trim()
  if (f.clearCctvUser) cctv.username = ''
  else if (f.cctvUser !== '') cctv.username = f.cctvUser
  body.cctv = cctv
  // ADS-B: the read token is sent only when typed ("" = clear), else omitted = keep.
  body.adsb = { enabled: f.adsbEnabled, url: f.adsbUrl.trim(), range_limit_km: f.adsbRangeKm }
  if (f.clearAdsbToken) body.adsb_token = ''
  else if (f.adsbToken !== '') body.adsb_token = f.adsbToken
  if (f.clearCctvPass) body.cctv_password = ''
  else if (f.cctvPass !== '') body.cctv_password = f.cctvPass
  return body
}

function fmt(n: number, d: number): string {
  return `${n >= 0 ? '+' : '-'}${Math.abs(n).toFixed(d)}`
}

export function siteWarning(c: SiteCheckDto | undefined | null): string | null {
  if (!c || c.ok !== false) return null
  return `Location differs from PWI4's site: Δlat ${fmt(c.lat_diff_deg ?? 0, 4)}°, `
    + `Δlon ${fmt(c.lon_diff_deg ?? 0, 4)}°, Δalt ${fmt(c.alt_diff_m ?? 0, 1)} m`
}

export function pcTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  }
  catch {
    return 'UTC'
  }
}

export function timeZoneList(): string[] {
  try {
    const list = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.('timeZone')
    if (list && list.length) return list.includes('UTC') ? list : ['UTC', ...list]
  }
  catch { /* fall through */ }
  return ['UTC']
}

const EPS = 1e-7

/**
 * True when the form differs from the loaded settings (`baseline` = formFromDto of the DTO as
 * loaded, before any first-run timezone substitution). A typed API key or "clear key" counts, and
 * so does a format-only change (those are persisted in settings.display).
 */
export function isDirty(f: SettingsForm, baseline: SettingsForm): boolean {
  if (f.apiKey !== '' || f.clearKey) return true
  if (f.cctvUrl !== '' || f.cctvUser !== '' || f.cctvPass !== '') return true
  if (f.clearCctvUrl || f.clearCctvUser || f.clearCctvPass) return true
  if (f.adsbToken !== '' || f.clearAdsbToken) return true
  const num = (a: number | null, b: number | null) =>
    a === b || (a != null && b != null && Math.abs(a - b) <= EPS)
  return !(
    num(f.lat, baseline.lat) && num(f.lon, baseline.lon) && num(f.altM, baseline.altM)
    && num(f.limEl, baseline.limEl) && num(f.cameraPixelUm, baseline.cameraPixelUm)
    && f.timezone === baseline.timezone
    && f.latFormat === baseline.latFormat && f.lonFormat === baseline.lonFormat
    && f.altUnit === baseline.altUnit
    && f.mount === baseline.mount && f.ota === baseline.ota
    && f.astrometry === baseline.astrometry && f.satelliteDb === baseline.satelliteDb
    && f.cctvEnabled === baseline.cctvEnabled && f.cctvType === baseline.cctvType
    && f.adsbEnabled === baseline.adsbEnabled && f.adsbUrl.trim() === baseline.adsbUrl.trim()
    && f.adsbRangeKm === baseline.adsbRangeKm
  )
}

/** Body for POST /api/mount/site-check (the proposed location, nothing saved). */
export function buildSiteCheckBody(f: SettingsForm): Record<string, unknown> {
  return { lat_deg: f.lat, lon_deg: f.lon, alt_m: f.altM }
}
