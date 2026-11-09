// Astrometry from Image (T5.2c, A2b-iv, concept/Astrometry.png): the pure model behind AstrometryDialog.
// The detection table (T = target point first, then streaks S1..Sn), the preview <-> image pixel mapping, the
// click-to-specify state machine (ported from ssa-toolkit tdm-tool.js startSpecify / applySpecify), add / remove,
// the RUN readiness and the DETECT-replaces-edits rule. All coordinates are ORIGINAL image pixels; the preview
// the user sees is scaled by `preview.scale`.

export const ACCEPT_EXT = ['.fits', '.fit', '.tif', '.tiff', '.jpg', '.jpeg', '.png']
export const ACCEPT_ATTR = ACCEPT_EXT.join(',')
export const MIN_STREAKS = 10
export const DEFAULT_BRT = 1
export const TARGET_BRT = 3
export const ERR_UNSUPPORTED = 'Unsupported file type'
export const ERR_EXPIRED = 'Image expired — load it again'
export const ERR_UNAVAILABLE = 'Analysis service unavailable'
export const ERR_TOO_LARGE = 'File too large'

export type Num = number | null
export type Role = 'star' | 'target'
export type Shape = 'streak' | 'point'

/** One detection as the analysis API returns / takes it. */
export interface Detection {
  id: string
  role: Role
  shape: Shape
  x: number
  y: number
  xs: number
  ys: number
  xe: number
  ye: number
  brt: number
}

export interface ImageMeta {
  format?: string
  bit_depth?: number
  dims: [number, number]
  epoch_iso: string | null
  exposure_s: number | null
  focal_mm: number | null
  pixel_um: number | null
  warnings: string[]
}

export interface ImageInfo {
  image_id: string
  preview: { png_b64: string, width: number, height: number, scale: number }
  meta: ImageMeta
}

export interface StreakRow { uid: number, xs: Num, ys: Num, xe: Num, ye: Num, brt: number }
export interface TargetRow { x: Num, y: Num }
export interface AsTable { target: TargetRow, streaks: StreakRow[], nextUid: number, edited: boolean }

export type CellKey = 'x' | 'y' | 'xs' | 'ys' | 'xe' | 'ye' | 'brt'
export type RowRef = { kind: 'T' } | { kind: 'S', idx: number }
export type Spec = { kind: 'T' } | { kind: 'S', idx: number, part: 'start' | 'end' }

// ----- table model
export function emptyTable(): AsTable {
  return { target: { x: null, y: null }, streaks: [], nextUid: 1, edited: false }
}

const fin = (v: unknown): Num => (typeof v === 'number' && Number.isFinite(v) ? v : null)

export function clampBrt(v: unknown): number {
  const n = Math.round(Number(v))
  return Number.isFinite(n) ? Math.max(1, Math.min(5, n)) : DEFAULT_BRT
}

/** A fresh table from a /detect answer (not "edited"). */
export function fromDetections(dets: readonly Detection[]): AsTable {
  const t = dets.find(d => d.role === 'target')
  const streaks = dets.filter(d => d.role === 'star' && d.shape === 'streak')
  return {
    target: t ? { x: fin(t.x), y: fin(t.y) } : { x: null, y: null },
    streaks: streaks.map((d, i) => ({ uid: i + 1, xs: fin(d.xs), ys: fin(d.ys), xe: fin(d.xe), ye: fin(d.ye), brt: clampBrt(d.brt) })),
    nextUid: streaks.length + 1,
    edited: false,
  }
}

export const streakLabel = (idx: number): string => `S${idx + 1}`
export const streakComplete = (s: StreakRow): boolean => [s.xs, s.ys, s.xe, s.ye].every(v => v !== null)
export const targetComplete = (t: TargetRow): boolean => t.x !== null && t.y !== null
export const completeStreaks = (t: AsTable): number => t.streaks.filter(streakComplete).length

export function addStreak(t: AsTable): AsTable {
  return {
    ...t, edited: true, nextUid: t.nextUid + 1,
    streaks: [...t.streaks, { uid: t.nextUid, xs: null, ys: null, xe: null, ye: null, brt: DEFAULT_BRT }],
  }
}

export function removeStreak(t: AsTable, idx: number): AsTable {
  if (idx < 0 || idx >= t.streaks.length) return t
  return { ...t, edited: true, streaks: t.streaks.filter((_, i) => i !== idx) }
}

/** Typed edit of one cell ("" / non-numeric clears a coordinate; BRT is clamped to 1-5). */
export function setCell(t: AsTable, row: RowRef, key: CellKey, text: string): AsTable {
  const raw = text.trim()
  const n = raw === '' ? null : Number(raw)
  const val: Num = n !== null && Number.isFinite(n) ? n : null
  if (row.kind === 'T') {
    if (key !== 'x' && key !== 'y') return t
    return { ...t, edited: true, target: { ...t.target, [key]: val } }
  }
  const s = t.streaks[row.idx]
  if (!s || key === 'x' || key === 'y') return t
  const next = key === 'brt' ? { ...s, brt: clampBrt(raw) } : { ...s, [key]: val }
  return { ...t, edited: true, streaks: t.streaks.map((x, i) => (i === row.idx ? next : x)) }
}

/** A cell's display text: integers as is, else one decimal; blank when unplaced. */
export function fmtCell(v: Num): string {
  if (v === null) return ''
  return Number.isInteger(v) ? String(v) : v.toFixed(1)
}

/** The edited table as the RUN / API detection list (complete rows only; ids T / S<n> by table position). */
export function toDetections(t: AsTable): Detection[] {
  const out: Detection[] = []
  if (targetComplete(t.target)) {
    const { x, y } = t.target as { x: number, y: number }
    out.push({ id: 'T', role: 'target', shape: 'point', x, y, xs: x, ys: y, xe: x, ye: y, brt: TARGET_BRT })
  }
  t.streaks.forEach((s, i) => {
    if (!streakComplete(s)) return
    const { xs, ys, xe, ye } = s as { xs: number, ys: number, xe: number, ye: number }
    out.push({ id: streakLabel(i), role: 'star', shape: 'streak', x: (xs + xe) / 2, y: (ys + ye) / 2, xs, ys, xe, ye, brt: s.brt })
  })
  return out
}

// ----- preview <-> image pixel mapping
export const previewToImage = (p: number, scale: number): number => (scale > 0 ? p / scale : p)
export const imageToPreview = (p: number, scale: number): number => p * (scale > 0 ? scale : 1)

export interface OverlayShapes {
  streaks: Array<{ idx: number, label: string, x1: number, y1: number, x2: number, y2: number, brt: number }>
  target: { x: number, y: number } | null
}

/** Overlay geometry in PREVIEW pixels (the zoom pane's content space). */
export function overlayShapes(t: AsTable, scale: number): OverlayShapes {
  const k = (v: number): number => imageToPreview(v, scale)
  const streaks: OverlayShapes['streaks'] = []
  t.streaks.forEach((s, idx) => {
    if (!streakComplete(s)) return
    const { xs, ys, xe, ye } = s as { xs: number, ys: number, xe: number, ye: number }
    streaks.push({ idx, label: streakLabel(idx), x1: k(xs), y1: k(ys), x2: k(xe), y2: k(ye), brt: s.brt })
  })
  const target = targetComplete(t.target) ? { x: k(t.target.x as number), y: k(t.target.y as number) } : null
  return { streaks, target }
}

// ----- specify (focus a coordinate, then click the image)
export const specSame = (a: Spec | null, b: Spec | null): boolean =>
  !!a && !!b && a.kind === b.kind && (a.kind === 'T' || (b.kind === 'S' && a.idx === b.idx && a.part === b.part))

export function specifyLabel(s: Spec): string {
  if (s.kind === 'T') return 'Click the image to place the target'
  return `Click the image to place the ${s.part === 'start' ? 'START' : 'END'} of ${streakLabel(s.idx)}`
}

/** The arm for a focused coordinate input: the target's X / Y, a streak's S (start) or E (end) pair. */
export function specFor(row: RowRef, key: CellKey): Spec | null {
  if (row.kind === 'T') return key === 'x' || key === 'y' ? { kind: 'T' } : null
  if (key === 'xs' || key === 'ys') return { kind: 'S', idx: row.idx, part: 'start' }
  if (key === 'xe' || key === 'ye') return { kind: 'S', idx: row.idx, part: 'end' }
  return null
}

/** Max-t point along fixed -> other that stays inside [0,w] x [0,h] (ssa-toolkit _snapSeg). */
function snapSeg(fx: number, fy: number, ox: number, oy: number, w: number, h: number): [number, number] {
  const dx = ox - fx
  const dy = oy - fy
  const cap = (f: number, d: number, max: number): number => (d > 0 ? (max - f) / d : d < 0 ? (0 - f) / d : Infinity)
  const t = Math.max(0, Math.min(1, cap(fx, dx, w), cap(fy, dy, h)))
  return [fx + dx * t, fy + dy * t]
}

/**
 * Commit a clicked IMAGE point to the armed row. Specifying one end of a streak that has no other end yet, with a
 * complete reference streak present, auto-fills the other end (same length + orientation, kept inside the frame).
 */
export function applySpecify(t: AsTable, spec: Spec, ix: number, iy: number, dims: readonly [number, number]): AsTable {
  const x = Math.round(ix)
  const y = Math.round(iy)
  if (spec.kind === 'T') return { ...t, edited: true, target: { x, y } }
  const s = t.streaks[spec.idx]
  if (!s) return t
  let n: StreakRow = spec.part === 'start' ? { ...s, xs: x, ys: y } : { ...s, xe: x, ye: y }
  const ref = t.streaks.find((o, i) => i !== spec.idx && streakComplete(o))
  if (ref) {
    const vx = (ref.xe as number) - (ref.xs as number)
    const vy = (ref.ye as number) - (ref.ys as number)
    if (spec.part === 'start' && s.xe === null && s.ye === null) {
      const p = snapSeg(x, y, x + vx, y + vy, dims[0], dims[1])
      n = { ...n, xe: Math.round(p[0]), ye: Math.round(p[1]) }
    }
    else if (spec.part === 'end' && s.xs === null && s.ys === null) {
      const p = snapSeg(x, y, x - vx, y - vy, dims[0], dims[1])
      n = { ...n, xs: Math.round(p[0]), ys: Math.round(p[1]) }
    }
  }
  return { ...t, edited: true, streaks: t.streaks.map((o, i) => (i === spec.idx ? n : o)) }
}

/** After a removal the armed row may be gone or shifted. */
export function specAfterRemove(spec: Spec | null, removedIdx: number): Spec | null {
  if (!spec || spec.kind === 'T') return spec
  if (spec.idx === removedIdx) return null
  return spec.idx > removedIdx ? { ...spec, idx: spec.idx - 1 } : spec
}

/** The same shift for the selected row. */
export function rowAfterRemove(sel: RowRef | null, removedIdx: number): RowRef | null {
  if (!sel || sel.kind === 'T') return sel
  if (sel.idx === removedIdx) return null
  return sel.idx > removedIdx ? { kind: 'S', idx: sel.idx - 1 } : sel
}

// ----- RUN readiness + DETECT confirm
export interface Readiness { ready: boolean, reason: string | null }

export function runReadiness(t: AsTable, hasImage: boolean, epoch: Date | null): Readiness {
  const n = completeStreaks(t)
  let reason: string | null = null
  if (!hasImage) reason = 'Load an image'
  else if (t.streaks.length === 0) reason = 'Detect or add streaks'
  else if (n < MIN_STREAKS) reason = `At least ${MIN_STREAKS} streaks are needed (${n} of ${MIN_STREAKS})`
  else if (!targetComplete(t.target)) reason = 'Mark the target'
  else if (!epoch || Number.isNaN(epoch.getTime())) reason = 'Enter the exposure start time'
  return { ready: reason === null, reason }
}

/** DETECT replaces the table: ask first when the user has edited it. */
export const needsDetectConfirm = (t: AsTable): boolean => t.edited

export const DETECT_CONFIRM_TEXT = 'Replace your edits with a new detection?'

// ----- metadata / errors
/** Metadata DATE-OBS -> a Date (UTC assumed when no zone is given). */
export function parseEpoch(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const s = /([zZ]|[+-]\d\d:?\d\d)$/.test(iso) ? iso : `${iso}Z`
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? null : d
}

export function parseExposure(text: string): number | null {
  const n = Number(text.trim())
  return text.trim() !== '' && Number.isFinite(n) && n > 0 ? n : null
}

export function fileAllowed(name: string): boolean {
  const i = name.lastIndexOf('.')
  return i >= 0 && ACCEPT_EXT.includes(name.slice(i).toLowerCase())
}

/** A readable line for a failed analysis call (never the raw body). */
export function astrometryErrorText(status: number, detail: unknown): string {
  const d = typeof detail === 'string' ? detail : ''
  if (status === 404) return ERR_EXPIRED
  if (status === 413) return d || ERR_TOO_LARGE
  if (status === 422) return d || ERR_UNSUPPORTED
  if (status === 0 || status >= 502) return ERR_UNAVAILABLE
  return d || 'Request failed'
}

/** One-line detect summary (never wraps: the status line keeps one height); the full warnings go in the `title`. */
export function detectSummary(n: number, hasTarget: boolean, warnings: readonly string[]): string {
  const base = `${n} streak${n === 1 ? '' : 's'} detected, ${hasTarget ? 'target found' : 'no target found: click the image to mark it'}`
  const edge = warnings.map(w => /excluded (\d+) frame-clipped \(edge\)/.exec(w)).find(m => m)
  return edge ? `${base} · ${edge[1]} edge streak${edge[1] === '1' ? '' : 's'} excluded` : base
}

/** The full detect warnings, for the status line's `title`. */
export const detectTitle = (warnings: readonly string[]): string => warnings.join(' ')

// ----- image source (T5.2f): Station | Other setup

export type ImageSource = 'station' | 'other'
export const SOURCE_OPTIONS = [{ value: 'station', label: 'Station' }, { value: 'other', label: 'Other' }]
/** The station's OTA (fixed, RASA11); the pixel size comes from Settings. */
export const STATION_FOCAL_MM = 620
const SOURCE_TOL = 0.25

const num = (v: number): string => String(Number(v.toFixed(2)))

/** The file's own optics (FITS FOCALLEN / XPIXSZ) vs the station's: a plate scale more than 25 % off (a missing half of the
 *  pair is taken from the station; with the station pixel unknown only the focal length is compared) suggests "Other setup".
 *  Returns the status-line note, or null when the file matches / has nothing to compare. */
export function suggestOther(meta: Pick<ImageMeta, 'focal_mm' | 'pixel_um'>, stationPixelUm: number | null): string | null {
  const f = typeof meta.focal_mm === 'number' && meta.focal_mm > 0 ? meta.focal_mm : null
  const p = typeof meta.pixel_um === 'number' && meta.pixel_um > 0 ? meta.pixel_um : null
  const sp = typeof stationPixelUm === 'number' && stationPixelUm > 0 ? stationPixelUm : null
  let ratio: number | null = null // file plate scale / station plate scale
  if (f !== null) ratio = (STATION_FOCAL_MM / f) * (p !== null && sp !== null ? p / sp : 1)
  else if (p !== null && sp !== null) ratio = p / sp
  if (ratio === null || (ratio >= 1 - SOURCE_TOL && ratio <= 1 + SOURCE_TOL)) return null
  const parts = [f !== null ? `${num(f)} mm` : null, p !== null ? `${num(p)} µm` : null].filter(Boolean).join(', ')
  return `File optics (${parts}) differ from the station: treated as Other setup`
}

/** What the dialog emits on RUN (T5.2d sends it to the gateway relay). `slot` = the IOD row the dialog was opened for. */
export interface AstrometryRunPayload {
  image_id: string
  detections: Detection[]
  epoch_iso: string
  exposure_s: number | null
  meta: ImageMeta
  slot: number | null
  /** Station: the relay fills the missing hints from the station; Other: only the file's own focal_mm / pixel_um are sent. */
  source: ImageSource
}
