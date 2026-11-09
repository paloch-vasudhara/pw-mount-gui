// The astrometry job (T5.2d): one background solve at a time (analysis keeps it; it survives a refresh). This store holds
// the current job and polls GET /api/astrometry/job/{id} every 2.5 s while it is active, so the job pill and the IOD rows
// agree. On the first mount of the pill `reattach()` asks GET /api/astrometry/job for an active or a finished, undismissed job.
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { useGatewayApi } from '~/composables/useGatewayApi'
import {
  ERR_JOB_LOST, isActiveState, isDismissed, isReviewed, markDismissed, markReviewed, parseJob, relayErrorText, type AstrometryJob,
} from '../utils/astrometry-job'
import { needsScaleReview } from '../utils/iod'

export const POLL_MS = 2500
const FETCH_TIMEOUT_MS = 5000

export const useAstrometryJobStore = defineStore('astrometry-job', () => {
  const job = ref<AstrometryJob | null>(null)
  /** The IOD row (0-based) that holds the current job id, published by the IOD dialog. */
  const row = ref<number | null>(null)
  /** The pill asked IOD to open (counter: ToolsGroup watches it). */
  const openIodTick = ref(0)
  /** The pill asked IOD to put the finished job into a free row. */
  const applyRequested = ref(false)
  /** The last Retry failure (the pill shows it as its status line until the next job starts). */
  const resumeError = ref<string | null>(null)
  const resuming = ref(false)
  /** A Retry started job `to` in place of `from`: the IOD dialog moves the row that held `from`. */
  const resumed = ref<{ from: string, to: string, n: number } | null>(null)
  /** T5.2f: the pill's Use / Discard on a job whose row is in IOD; the IOD dialog performs it on the row (n: a new request). */
  const reviewAction = ref<{ kind: 'use' | 'discard', n: number } | null>(null)
  const reviewTick = ref(0)
  const api = useGatewayApi()
  let timer: ReturnType<typeof setTimeout> | null = null
  let gen = 0

  const active = computed(() => !!job.value && isActiveState(job.value.state))
  /** A done job whose solved scale differs from the station's and has not been answered with Use yet (T5.2f). */
  const needsReview = computed(() => {
    void reviewTick.value
    const j = job.value
    return !!j && j.state === 'done' && !!j.result && needsScaleReview(j.result) && !isReviewed(j.job_id)
  })

  async function getJson(path: string): Promise<{ status: number, body: unknown }> {
    try {
      const res = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) })
      return { status: res.status, body: await res.json().catch(() => null) }
    }
    catch {
      return { status: 0, body: null }
    }
  }

  function stop(): void {
    if (timer) clearTimeout(timer)
    timer = null
  }

  function schedule(): void {
    stop()
    if (!active.value) return
    const g = gen
    timer = setTimeout(() => { void poll(g) }, POLL_MS)
  }

  async function poll(g: number): Promise<void> {
    const cur = job.value
    if (!cur || g !== gen) return
    const r = await getJson(`/api/astrometry/job/${encodeURIComponent(cur.job_id)}`)
    if (g !== gen) return
    if (r.status === 404) {
      job.value = { job_id: cur.job_id, state: 'failed', error: ERR_JOB_LOST }
    }
    else {
      const next = r.status === 200 ? parseJob(r.body) : null
      if (next && next.job_id === cur.job_id) job.value = next
      // a network blip / bad body: keep the last known job and try again
    }
    schedule()
  }

  /** A job the relay just accepted (202). */
  function start(jobId: string, state: AstrometryJob['state'] = 'uploading'): void {
    gen += 1
    resumeError.value = null
    job.value = { job_id: jobId, state, stage_text: 'uploading' }
    schedule()
  }

  /** On page load: an active job, or the latest finished one that was not dismissed. */
  async function reattach(): Promise<void> {
    if (job.value) return
    const g = ++gen
    const r = await getJson('/api/astrometry/job')
    if (g !== gen || job.value) return
    const j = r.status === 200 ? parseJob(r.body) : null
    if (!j) return
    if (!isActiveState(j.state) && isDismissed(j.job_id)) return
    job.value = j
    schedule()
  }

  async function cancel(): Promise<void> {
    const cur = job.value
    if (!cur || !isActiveState(cur.state)) return
    try {
      await fetch(`/api/astrometry/job/${encodeURIComponent(cur.job_id)}/cancel`, { method: 'POST', cache: 'no-store', signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) })
    }
    catch { /* the next poll shows the state */ }
    if (job.value?.job_id === cur.job_id) await poll(gen)
  }

  /** Retry on a resumable failed job: the gateway starts a NEW job that re-polls the same submission. */
  async function resume(): Promise<void> {
    const cur = job.value
    if (!cur || !cur.resumable || isActiveState(cur.state) || resuming.value) return
    resuming.value = true
    resumeError.value = null
    try {
      const res = await api.post<{ job_id?: string, state?: AstrometryJob['state'] }>(`/api/mount/astrometry/job/${encodeURIComponent(cur.job_id)}/resume`, {})
      if (!res || typeof res.job_id !== 'string') throw new Error('no job id')
      resumed.value = { from: cur.job_id, to: res.job_id, n: (resumed.value?.n ?? 0) + 1 }
      start(res.job_id, res.state)
    }
    catch (e) {
      const err = e as { status?: unknown, detail?: unknown }
      resumeError.value = relayErrorText(typeof err.status === 'number' ? err.status : 0, err.detail)
    }
    resuming.value = false
  }

  /** The x on a finished job. */
  function dismiss(): void {
    const cur = job.value
    if (!cur || isActiveState(cur.state)) return
    markDismissed(cur.job_id)
    resumeError.value = null
    gen += 1
    stop()
    job.value = null
    row.value = null
  }

  function requestApply(): void {
    applyRequested.value = true
    openIodTick.value += 1
  }
  /** The scale review is answered with Use: the pill returns to its normal done state. */
  function markUsed(jobId: string): void {
    markReviewed(jobId)
    reviewTick.value += 1
  }
  /** The pill's Use: with the row in IOD it uses the row; after a refresh (no row) the result goes into a free row directly. */
  function reviewUse(): void {
    const cur = job.value
    if (!cur) return
    if (row.value !== null) { reviewAction.value = { kind: 'use', n: (reviewAction.value?.n ?? 0) + 1 }; return }
    markUsed(cur.job_id)
    requestApply()
  }
  /** The pill's Discard: with the row in IOD the dialog empties it (and dismisses); without a row only the pill goes. */
  function reviewDiscard(): void {
    if (row.value !== null) reviewAction.value = { kind: 'discard', n: (reviewAction.value?.n ?? 0) + 1 }
    else dismiss()
  }
  function requestOpenIod(): void {
    openIodTick.value += 1
  }

  /** Tests / teardown. */
  function reset(): void {
    gen += 1
    stop()
    job.value = null
    row.value = null
    resumeError.value = null
    resuming.value = false
    resumed.value = null
    reviewAction.value = null
    applyRequested.value = false
    openIodTick.value = 0
  }

  return {
    job, row, openIodTick, applyRequested, active, needsReview, reviewAction, resumeError, resuming, resumed,
    start, reattach, cancel, resume, dismiss, requestApply, requestOpenIod, markUsed, reviewUse, reviewDiscard, reset,
  }
})
