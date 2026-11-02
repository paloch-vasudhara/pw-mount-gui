// Astrometry job model (T5.2d): the analysis job record, its states, the readable texts, and the dismissed-job memory.
// Pure helpers; the store (stores/astrometry-job.ts) owns polling.

export type JobState = 'uploading' | 'queued' | 'solving' | 'mapping' | 'done' | 'failed' | 'cancelled' | 'timeout'

export interface JobResult {
  ra_deg: number
  dec_deg: number
  epoch_mid_iso: string
  epoch_mid_unix: number
  residual_arcsec?: number
  scale_arcsec_px?: number
  n_stars_used?: number
  warnings?: string[]
}

export interface AstrometryJob {
  job_id: string
  state: JobState
  stage_text?: string
  started?: number
  result?: JobResult | null
  error?: string | null
  /** Failed / timed out / cancelled after the upload: POST .../resume re-polls the same astrometry.net submission. */
  resumable?: boolean
}

const STATES: readonly string[] = ['uploading', 'queued', 'solving', 'mapping', 'done', 'failed', 'cancelled', 'timeout']

export const isActiveState = (s: JobState): boolean => s === 'uploading' || s === 'queued' || s === 'solving' || s === 'mapping'
/** failed / cancelled / timeout: no result. */
export const isFailState = (s: JobState): boolean => s === 'failed' || s === 'cancelled' || s === 'timeout'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** The job when the body has the fields the UI needs; null otherwise. */
export function parseJob(b: unknown): AstrometryJob | null {
  const o = b as Partial<AstrometryJob> | null
  if (!o || typeof o !== 'object' || typeof o.job_id !== 'string' || typeof o.state !== 'string' || !STATES.includes(o.state)) return null
  if (o.state === 'done') {
    const r = o.result
    if (!r || !fin(r.ra_deg) || !fin(r.dec_deg) || !fin(r.epoch_mid_unix)) return null
  }
  return o as AstrometryJob
}

export const ERR_JOB_LOST = 'Astrometry job not found (the analysis service restarted)'

/** Why a job ended without a result, for the pill and the row title. */
export function failureText(j: AstrometryJob): string {
  const e = typeof j.error === 'string' ? j.error.trim() : ''
  if (e) return e
  if (j.state === 'cancelled') return 'Astrometry cancelled'
  if (j.state === 'timeout') return 'Solve timed out'
  return 'astrometry.net could not solve this frame'
}

/** The stage shown while the job runs ("Astrometry · <stage>"). */
export function stageLabel(j: AstrometryJob): string {
  const t = typeof j.stage_text === 'string' ? j.stage_text.trim() : ''
  if (t) return t
  return j.state === 'uploading' ? 'uploading' : j.state === 'queued' ? 'queued' : j.state === 'mapping' ? 'mapping' : 'solving'
}

/** Status line text of a failed POST /api/mount/astrometry/run (409 / 422 / 404 / 503 show their `detail`; status 0 = unreachable). */
export function relayErrorText(status: number, detail: unknown): string {
  const d0 = (detail as { detail?: unknown } | null)?.detail ?? detail
  const d = typeof d0 === 'string' ? d0.trim() : ''
  if (status === 0 || status >= 500) return d && status === 503 ? d : 'Analysis service unavailable'
  return d || 'The request was rejected'
}

// ----- dismissed jobs (client-side; per viewer: module scope + sessionStorage)

const KEY = 'astrometry-dismissed'
const dismissed = new Set<string>()
let loaded = false

function load(): void {
  if (loaded) return
  loaded = true
  try {
    const raw = sessionStorage.getItem(KEY)
    const a = raw ? JSON.parse(raw) : []
    if (Array.isArray(a)) for (const id of a) if (typeof id === 'string') dismissed.add(id)
  }
  catch { /* storage unavailable: module scope only */ }
}

export function isDismissed(id: string): boolean {
  load()
  return dismissed.has(id)
}

export function markDismissed(id: string): void {
  load()
  dismissed.add(id)
  try { sessionStorage.setItem(KEY, JSON.stringify([...dismissed].slice(-20))) }
  catch { /* ignore */ }
}

/** Forget the dismissed jobs (tests). */
export function resetDismissed(): void {
  dismissed.clear()
  loaded = false
  try { sessionStorage.removeItem(KEY) }
  catch { /* ignore */ }
}
