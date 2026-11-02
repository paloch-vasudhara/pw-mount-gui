// INITIAL ORBIT DETERMINATION modal helpers (T5.3b, A2b-ii, concept/IOD.png): row model, draft validation,
// pair clearing, readiness, overwrite rules, request building, output formats. Pure; the dialog owns fetching.
import { pad2 } from './datetime-fields'
import type { PassSite } from './pass-analysis'

export const SLOTS = 3
export const ERR_IOD_UNAVAILABLE = 'Analysis service unavailable'
export const PENDING_TEXT = 'Astrometry pending'
export const FAILED_TEXT = 'Astrometry failed'
export const STALE_TEXT = 'Inputs changed — run again'
export const NOT_CONVERGED_TEXT = 'Osculating elements (fit did not converge)'
export const NO_SITE_REASON = 'Station location not available'

export type IodSource = 'Manual' | 'Astrometry'
/** empty = no data; pending / failed = an astrometry job (T5.2); partial = the typed pair is set, the other pair
 *  is not (yet) computed; valid = az / el / ra / dec all known. IOD needs RA / Dec: see `rowValid`. */
export type IodRowState = 'empty' | 'pending' | 'failed' | 'partial' | 'valid'
export type IodPair = 'altaz' | 'radec'

export interface IodRow {
  state: IodRowState
  source: IodSource | null
  /** Epoch, unix seconds (whole). */
  t: number | null
  az: number | null
  el: number | null
  /** Degrees 0-360. */
  ra: number | null
  dec: number | null
  /** The pair the user / the plate solve supplied (shown bright); the other one is computed (dimmed). */
  bright: IodPair | null
  /** The astrometry job this row holds (T5.2): kept from pending through solved. */
  jobId?: string | null
  /** Why an astrometry row failed (the title of its "Astrometry failed" text). */
  reason?: string | null
}

export const emptyRow = (): IodRow => ({ state: 'empty', source: null, t: null, az: null, el: null, ra: null, dec: null, bright: null })
export const emptyRows = (): IodRow[] => Array.from({ length: SLOTS }, emptyRow)

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** T5.2 constructors: an astrometry job running / failed / solved (RA / Dec in degrees; az / el are then computed by H4). */
export const astrometryPending = (t: number | null, jobId: string | null = null): IodRow =>
  ({ ...emptyRow(), state: 'pending', source: 'Astrometry', t, jobId })
export const astrometryFailed = (t: number | null, reason: string | null = null, jobId: string | null = null): IodRow =>
  ({ ...emptyRow(), state: 'failed', source: 'Astrometry', t, reason, jobId })
export const astrometrySolved = (t: number, ra: number, dec: number, jobId: string | null = null): IodRow =>
  ({ ...emptyRow(), state: 'partial', source: 'Astrometry', t, ra, dec, bright: 'radec', jobId })

/** A done job's result as a row (the mid-exposure epoch in whole seconds, as every IOD row; RA / Dec bright, H4 fills the rest). */
export const rowFromJobResult = (jobId: string, r: { epoch_mid_unix: number, ra_deg: number, dec_deg: number }): IodRow =>
  astrometrySolved(Math.round(r.epoch_mid_unix), r.ra_deg >= 0 && r.ra_deg < 360 ? r.ra_deg : ((r.ra_deg % 360) + 360) % 360, r.dec_deg, jobId)

/** The row can feed the IOD: an epoch and RA / Dec are known (a RA / Dec row stays valid while H4 is unreachable). */
export const rowValid = (r: IodRow): boolean => (r.state === 'valid' || r.state === 'partial') && fin(r.t) && fin(r.ra) && fin(r.dec)
export const rowOccupied = (r: IodRow): boolean => r.state !== 'empty'

// ----- angle text / draft (the Add Manual inputs)

export type DraftField = 'az' | 'el' | 'ra' | 'dec'
export interface IodDraft { t: Date | null, az: string, el: string, ra: string, dec: string }

export const newDraft = (nowMs: number): IodDraft => ({ t: new Date(Math.floor(nowMs / 1000) * 1000), az: '', el: '', ra: '', dec: '' })

const MAX_DECIMALS = 6

/** Keep digits, one decimal point, a leading sign; at most 6 decimals (logic sheet, changed from 4 on 11-02); the value is stored exactly as typed. */
export function sanitizeAngleText(s: string): string {
  let out = ''
  let dot = false
  let dec = 0
  for (const ch of s.replace(/,/g, '.')) {
    if (ch === '+' || ch === '-') { if (out === '') out = ch; continue }
    if (ch === '.') { if (!dot) { dot = true; out += ch } continue }
    if (ch >= '0' && ch <= '9') {
      if (dot) { if (dec >= MAX_DECIMALS) continue; dec += 1 }
      out += ch
    }
  }
  return out
}

/** The draft of a row to edit: its epoch and its BRIGHT pair at full stored precision; the dimmed pair is cleared (H4 recomputes it). */
export function draftFromRow(r: IodRow): IodDraft | null {
  if (!(r.state === 'valid' || r.state === 'partial') || !fin(r.t) || !r.bright) return null
  const txt = (v: number | null): string => (fin(v) ? (String(v).includes('e') ? v.toFixed(12).replace(/\.?0+$/, '') : String(v)) : '')
  const d: IodDraft = { t: new Date(r.t * 1000), az: '', el: '', ra: '', dec: '' }
  for (const f of PAIRS[r.bright]) d[f] = txt(r[f])
  return d
}

/** null = empty / not a number. */
export function parseAngleText(s: string): number | null {
  const t = s.trim()
  if (t === '' || !/^[+-]?(\d+\.?\d*|\.\d+)$/.test(t)) return null
  const v = Number(t)
  return Number.isFinite(v) ? v : null
}

const PAIRS: Record<IodPair, [DraftField, DraftField]> = { altaz: ['az', 'el'], radec: ['ra', 'dec'] }
const pairOf = (f: DraftField): IodPair => (f === 'az' || f === 'el' ? 'altaz' : 'radec')

/** Typing in one pair clears the other. */
export function editDraft(d: IodDraft, field: DraftField, text: string): IodDraft {
  const next: IodDraft = { ...d }
  const other = PAIRS[pairOf(field) === 'altaz' ? 'radec' : 'altaz']
  for (const f of other) next[f] = ''
  next[field] = sanitizeAngleText(text)
  return next
}

export const RANGES: Record<DraftField, [number, number]> = { az: [0, 360], el: [0, 90], ra: [0, 360], dec: [-90, 90] }
export const RANGE_TEXT: Record<DraftField, string> = {
  az: 'Az. must be between 0° and 360°',
  el: 'El. must be between 0° and 90° (above the horizon)',
  ra: 'R.A. must be between 0° and 360°',
  dec: 'Dec. must be between -90° and +90°',
}

export type DraftCheck =
  | { kind: 'blank' }
  | { kind: 'ok', pair: IodPair }
  | { kind: 'invalid', bad: Array<DraftField | 't'>, reason: string }

/** blank = nothing typed (the edit is dropped); invalid = the fields to mark + a reason. */
export function checkDraft(d: IodDraft): DraftCheck {
  const all: DraftField[] = ['az', 'el', 'ra', 'dec']
  if (all.every(f => d[f].trim() === '')) return { kind: 'blank' }
  if (!d.t || !Number.isFinite(d.t.getTime())) return { kind: 'invalid', bad: ['t'], reason: 'Epoch is not valid' }
  // the pair being filled: the one with any text (typing clears the other)
  const pair: IodPair = PAIRS.altaz.some(f => d[f].trim() !== '') ? 'altaz' : 'radec'
  const bad: Array<DraftField> = []
  let reason = ''
  for (const f of PAIRS[pair]) {
    const v = parseAngleText(d[f])
    if (v === null) {
      bad.push(f)
      reason ||= d[f].trim() === '' ? 'Enter both values of the pair' : 'Not a valid number'
    }
    else if (v < RANGES[f][0] || v > RANGES[f][1]) {
      bad.push(f)
      reason ||= RANGE_TEXT[f]
    }
  }
  return bad.length ? { kind: 'invalid', bad, reason } : { kind: 'ok', pair }
}

/** The committed row of a draft that passed `checkDraft` (null otherwise). 360° wraps to 0°. */
export function draftToRow(d: IodDraft): IodRow | null {
  const c = checkDraft(d)
  if (c.kind !== 'ok' || !d.t) return null
  const [a, b] = PAIRS[c.pair]
  const va = parseAngleText(d[a])!
  const vb = parseAngleText(d[b])!
  const row: IodRow = { ...emptyRow(), state: 'partial', source: 'Manual', t: Math.floor(d.t.getTime() / 1000), bright: c.pair }
  if (c.pair === 'altaz') { row.az = va % 360; row.el = vb }
  else { row.ra = va % 360; row.dec = vb }
  return row
}

// ----- H4 (POST /api/transform on the gateway) at the ROW EPOCH

export interface IodTransformRequest {
  dir: 'altaz_to_radec' | 'radec_to_altaz'
  t_utc: string
  values: Record<string, number>
  /** The Settings location, always sent so the conversion never depends on PWI4. */
  site: { lat: number, lon: number, height_m: number }
}

/** Null while the row has no epoch / typed pair. RA goes to the API in hours. */
export function buildIodTransform(r: IodRow, site: PassSite): IodTransformRequest | null {
  if (!fin(r.t)) return null
  const t_utc = new Date(r.t * 1000).toISOString()
  const st = { lat: site.lat, lon: site.lon, height_m: site.alt_m }
  if (r.bright === 'altaz' && fin(r.az) && fin(r.el)) return { dir: 'altaz_to_radec', t_utc, values: { alt: r.el, az: r.az }, site: st }
  if (r.bright === 'radec' && fin(r.ra) && fin(r.dec)) return { dir: 'radec_to_altaz', t_utc, values: { ra_h: r.ra / 15, dec_deg: r.dec }, site: st }
  return null
}

const wrap360 = (v: number): number => ((v % 360) + 360) % 360

/** The row with the computed pair filled (RA hours -> degrees 0-360); null = unusable result. */
export function applyIodTransform(r: IodRow, result: unknown): IodRow | null {
  const o = result as Record<string, unknown> | null | undefined
  if (!o || typeof o !== 'object') return null
  if (r.bright === 'altaz') {
    if (!fin(o.ra_h) || !fin(o.dec_deg)) return null
    return { ...r, state: 'valid', ra: wrap360(o.ra_h * 15), dec: o.dec_deg }
  }
  if (r.bright === 'radec') {
    if (!fin(o.alt) || !fin(o.az)) return null
    return { ...r, state: 'valid', el: o.alt, az: wrap360(o.az) }
  }
  return null
}

// ----- (...) menu, overwrite rules

export interface MenuState { addManual: boolean, addAstrometry: boolean, edit: boolean, remove: boolean }
/** Edit Entry: only a row holding values (valid / partial); a failed astrometry row is replaced, not edited. */
export const menuState = (r: IodRow): MenuState => ({
  addManual: true, addAstrometry: true, edit: (r.state === 'valid' || r.state === 'partial') && r.bright !== null, remove: rowOccupied(r),
})

/** Adding to / removing an occupied VALID row asks first; an empty, pending, failed or unconverted row does not. */
export const needsConfirm = (r: IodRow): boolean => rowValid(r)
export const confirmText = (kind: 'replace' | 'remove', slot: number): string => `${kind === 'replace' ? 'Replace' : 'Remove'} row ${slot + 1}?`

// ----- sorting (the slot numbers stay with their rows)

export type EpochDir = 'asc' | 'desc'
/** Slot indices in display order; rows without an epoch go last whatever the direction. */
export function sortedSlots(rows: IodRow[], dir: EpochDir): number[] {
  const idx = rows.map((_, i) => i)
  return idx.sort((a, b) => {
    const ta = rows[a]!.t
    const tb = rows[b]!.t
    if (ta === null && tb === null) return a - b
    if (ta === null) return 1
    if (tb === null) return -1
    return (dir === 'asc' ? ta - tb : tb - ta) || a - b
  })
}
export const nextEpochDir = (d: EpochDir): EpochDir => (d === 'asc' ? 'desc' : 'asc')
export const epochArrow = (d: EpochDir): string => (d === 'desc' ? ' ▼' : ' ▲')

// ----- readiness

export interface Readiness { ready: boolean, reason: string | null }

/** 3 valid rows with distinct epochs; the reason names what is missing (the button's `title`). */
export function iodReadiness(rows: IodRow[], hasSite = true): Readiness {
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i]!
    if (r.state === 'pending') return { ready: false, reason: `Row ${i + 1}: Astrometry is still running` }
    if (r.state === 'failed') return { ready: false, reason: `Row ${i + 1}: Astrometry failed (remove or replace the row)` }
    if (r.state === 'partial' && !rowValid(r)) return { ready: false, reason: `Row ${i + 1} has no R.A. / Dec. yet` }
  }
  const valid = rows.filter(rowValid)
  if (valid.length < SLOTS) return { ready: false, reason: `Three observations are needed (${valid.length} of ${SLOTS})` }
  if (new Set(valid.map(r => r.t)).size < SLOTS) return { ready: false, reason: 'The observations need different epochs' }
  if (!hasSite) return { ready: false, reason: NO_SITE_REASON }
  return { ready: true, reason: null }
}

// ----- request / response

export interface IodResponse {
  epoch_unix: number
  r_km: number[]
  v_kms: number[]
  method: 'gauss+gibbs' | 'gauss+herrick-gibbs'
  iterations: number
  coe: { a_km: number, e: number, i_deg: number, raan_deg: number, argp_deg: number, nu_deg: number, period_min: number }
  tle: { line1: string, line2: string }
  tle_fit: { iterations: number, pos_err_km: number, vel_err_ms: number, converged: boolean }
}

export interface IodRequest {
  site: PassSite
  obs: Array<{ t_unix: number, ra_deg: number, dec_deg: number }>
}

/** The three valid rows in epoch order (null when the rows are not ready). */
export function buildIodRequest(site: PassSite, rows: IodRow[]): IodRequest | null {
  const valid = rows.filter(rowValid)
  if (valid.length !== SLOTS) return null
  const obs = valid
    .map(r => ({ t_unix: r.t!, ra_deg: r.ra!, dec_deg: r.dec! }))
    .sort((a, b) => a.t_unix - b.t_unix)
  return { site: { lat: site.lat, lon: site.lon, alt_m: site.alt_m }, obs }
}

/** The response when it has the fields the display needs; null otherwise. */
export function parseIodResponse(b: unknown): IodResponse | null {
  const o = b as Partial<IodResponse> | null
  if (!o || typeof o !== 'object') return null
  const vec = (v: unknown): v is number[] => Array.isArray(v) && v.length === 3 && v.every(fin)
  if (!vec(o.r_km) || !vec(o.v_kms) || typeof o.tle?.line1 !== 'string' || typeof o.tle.line2 !== 'string') return null
  return o as IodResponse
}

/** Status line text of a failed POST /api/iod (status 0 = network / timeout); a 422 shows its detail as-is. */
export function iodErrorText(status: number, detail: unknown): string {
  const d = typeof detail === 'string' ? detail.trim() : ''
  if (status === 422) return d || 'The observations were rejected'
  return ERR_IOD_UNAVAILABLE
}

export const methodLabel = (m: string): string => (m === 'gauss+herrick-gibbs' ? 'Gauss + Herrick-Gibbs' : m === 'gauss+gibbs' ? 'Gauss + Gibbs' : m)
export const doneText = (r: IodResponse): string => `Done · ${methodLabel(r.method)}`

// ----- formats

const num3 = (v: number): string => {
  const s = v.toFixed(3)
  return s === '-0.000' ? '0.000' : s
}
const vec3 = (v: number[]): string => `[ ${v.map(num3).join(', ')} ]`

/** `R_2 : [ x, y, z ]   V_2 : [ vx, vy, vz ]` (km, km/s, 3 decimals). */
export const fmtStateVector = (r: number[], v: number[]): string => `R_2 : ${vec3(r)}   V_2 : ${vec3(v)}`
export const fmtTle = (t: { line1: string, line2: string }): string => `${t.line1}\n${t.line2}`

/** Angle cell: 3 decimals + degree sign; `signed` shows a leading + (Dec.); null = "—". */
export function fmtAngle(v: number | null, signed = false): string {
  if (!fin(v)) return '—'
  const s = v.toFixed(3)
  return `${signed && v >= 0 ? '+' : ''}${s === '-0.000' ? '0.000' : s}°`
}

/** Epoch cell: `YYYY-MM-DD HH:MM:SS` (UTC). */
export function fmtEpoch(t: number | null): string {
  if (!fin(t)) return ''
  const d = new Date(Math.round(t) * 1000)
  return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`
}

export type RowDisplay =
  | { kind: 'empty' }
  | { kind: 'status', text: string }
  | { kind: 'values' }

/** What the angle cells of a committed row show: nothing, the astrometry job state, or the four values. */
export function rowDisplay(r: IodRow): RowDisplay {
  if (r.state === 'empty') return { kind: 'empty' }
  if (r.state === 'pending') return { kind: 'status', text: PENDING_TEXT }
  if (r.state === 'failed') return { kind: 'status', text: FAILED_TEXT }
  return { kind: 'values' }
}

/** True when a cell of the committed row is the computed (dimmed) pair. */
export const isDim = (r: IodRow, f: DraftField): boolean => r.bright !== null && r.bright !== pairOf(f)
