// Station Settings (T1.2): types, form validation and payload building (pure, unit-tested).
import type { AltUnit } from '@/utils/altitude'

export type AngleFmt = 'dd' | 'dms'

export interface StationSettingsDto {
  version: number
  location: { lat_deg: number, lon_deg: number, alt_m: number, timezone: string | null }
  hardware: { mount: string, ota: string, lim_el_deg: number }
  providers: { astrometry: string, satellite_db: string }
  display: { lat_format: AngleFmt, lon_format: AngleFmt, alt_unit: AltUnit }
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
}

export type FieldErrors = Partial<Record<'lat' | 'lon' | 'alt' | 'timezone' | 'limEl', string>>

export const LIM_EL_MIN = 0
export const LIM_EL_MAX = 90
export const ALT_MIN_M = -500
export const ALT_MAX_M = 9000

export function formFromDto(d: StationSettingsDto): SettingsForm {
  return {
    lat: d.location.lat_deg,
    lon: d.location.lon_deg,
    altM: d.location.alt_m,
    timezone: d.location.timezone ?? '',
    limEl: d.hardware.lim_el_deg,
    latFormat: d.display.lat_format,
    lonFormat: d.display.lon_format,
    altUnit: d.display.alt_unit,
    mount: d.hardware.mount,
    ota: d.hardware.ota,
    astrometry: d.providers.astrometry,
    satelliteDb: d.providers.satellite_db,
    apiKey: '',
    clearKey: false,
  }
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
  return e
}

/** PUT body. `api_key` is present only when the user typed one ("" = clear), else omitted = keep. */
export function buildPayload(f: SettingsForm): Record<string, unknown> {
  const body: Record<string, unknown> = {
    version: 1,
    location: { lat_deg: f.lat, lon_deg: f.lon, alt_m: f.altM, timezone: f.timezone },
    hardware: { mount: f.mount, ota: f.ota, lim_el_deg: f.limEl },
    providers: { astrometry: f.astrometry, satellite_db: f.satelliteDb },
    display: { lat_format: f.latFormat, lon_format: f.lonFormat, alt_unit: f.altUnit },
  }
  if (f.clearKey) body.api_key = ''
  else if (f.apiKey !== '') body.api_key = f.apiKey
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
