// @vitest-environment happy-dom
// Resilience: the failed pill offers Retry when the job is resumable (POST the gateway resume relay, track the new job),
// the IOD row that held the old job id follows it (pending again), a relay error is the pill's status line.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import AstrometryJobPill from '../../app/components/analysis/AstrometryJobPill.vue'
import IodDialog from '../../app/components/analysis/IodDialog.vue'
import { POLL_MS, useAstrometryJobStore } from '../../app/stores/astrometry-job'
import { useMountStore } from '../../app/stores/mount'
import { parseJob, resetDismissed, type AstrometryJob } from '../../app/utils/astrometry-job'
import { astrometryFailed, emptyRow } from '../../app/utils/iod'
import { resetIodMemo, setIodMemo } from '../../app/utils/iod-memo'

const failedJob = (id = 'j1', resumable = true): AstrometryJob => ({
  job_id: id, state: 'failed', error: 'astrometry.net busy (HTTP 503) — retried 3×', resumable,
})

let jobById: Record<string, AstrometryJob | number> = {}
let postImpl: (path: string, body: Record<string, unknown>) => Promise<unknown>
const posts: Array<{ path: string, body: Record<string, unknown> }> = []

vi.mock('~/composables/useGatewayApi', () => ({
  useGatewayApi: () => ({
    post: async (path: string, body: Record<string, unknown>) => {
      posts.push({ path, body })
      return postImpl(path, body)
    },
  }),
}))

let w: VueWrapper | null = null
let pill: VueWrapper | null = null
const q = (s: string) => document.body.querySelector(s) as HTMLElement | null
const tick = async () => { await flushPromises(); await nextTick() }

beforeEach(() => {
  setActivePinia(createPinia())
  resetDismissed(); resetIodMemo()
  jobById = {}; posts.length = 0
  postImpl = async path => (path.endsWith('/resume') ? { job_id: 'j2', state: 'uploading' } : { result: { alt: 41, az: 201 } })
  vi.useFakeTimers({ now: Date.UTC(2025, 3, 4, 13, 1, 30), toFake: ['Date', 'setTimeout', 'clearTimeout'] })
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const m = /^\/api\/astrometry\/job\/([^/]+)$/.exec(url)
    const v = m ? (jobById[m[1]!] ?? 404) : 404
    return typeof v === 'number' ? { ok: false, status: v, json: async () => ({}) } : { ok: true, status: 200, json: async () => v }
  }))
})
afterEach(() => {
  w?.unmount(); w = null
  pill?.unmount(); pill = null
  document.body.innerHTML = ''
  vi.useRealTimers(); vi.unstubAllGlobals()
})

async function mountPill() {
  pill = mount(AstrometryJobPill, { attachTo: document.body })
  await tick()
}

describe('Retry on the failed pill', () => {
  it('parseJob keeps the resumable flag', () => {
    expect(parseJob(failedJob())!.resumable).toBe(true)
  })

  it('is shown only for a resumable failed job', async () => {
    const store = useAstrometryJobStore()
    store.job = failedJob('j1', false)
    await mountPill()
    expect(q('[data-testid="as-pill-retry"]')).toBeNull()
    store.job = failedJob('j1', true)
    await tick()
    expect(q('[data-testid="as-pill-retry"]')).toBeTruthy()
    store.job = { job_id: 'j1', state: 'solving', resumable: true }
    await tick()
    expect(q('[data-testid="as-pill-retry"]')).toBeNull()
    expect(q('[data-testid="as-pill-title"]')!.textContent).toBe('Astrometry')
  })

  it('posts the gateway resume relay and follows the new job', async () => {
    const store = useAstrometryJobStore()
    store.job = failedJob('j1')
    await mountPill()
    q('[data-testid="as-pill-retry"]')!.click()
    await tick()
    expect(posts).toEqual([{ path: '/api/mount/astrometry/job/j1/resume', body: {} }])
    expect(store.job!.job_id).toBe('j2')
    expect(q('[data-testid="as-pill"]')!.dataset.kind).toBe('running')
    jobById.j2 = { job_id: 'j2', state: 'solving', stage_text: 'Re-attached to astrometry.net submission 7' }
    await vi.advanceTimersByTimeAsync(POLL_MS + 1)
    expect(q('[data-testid="as-pill-text"]')!.textContent).toContain('Re-attached')
  })

  it('a relay error is the pill status line (the job stays failed)', async () => {
    const store = useAstrometryJobStore()
    store.job = failedJob('j1')
    postImpl = async () => { throw Object.assign(new Error('x'), { status: 409, detail: { detail: 'An astrometry solve is already running' } }) }
    await mountPill()
    q('[data-testid="as-pill-retry"]')!.click()
    await tick()
    expect(q('[data-testid="as-pill-text"]')!.textContent).toBe('An astrometry solve is already running')
    expect(store.job!.job_id).toBe('j1')
    expect(q('[data-testid="as-pill-retry"]')).toBeTruthy()
    postImpl = async () => { throw Object.assign(new Error('x'), { status: 503, detail: { detail: 'Analysis service unavailable' } }) }
    q('[data-testid="as-pill-retry"]')!.click()
    await tick()
    expect(q('[data-testid="as-pill-text"]')!.textContent).toBe('Analysis service unavailable')
  })
})

describe('Retry re-points the IOD row', () => {
  it('the row that held the old job id holds the new one and stays "Astrometry pending"', async () => {
    useMountStore().snapshot = { site: { lat: 10, lon: 20, height_m: 30 }, limits: { lim_el_deg: 12 } } as never
    const rows = [astrometryFailed(1743771705, 'astrometry.net busy (HTTP 503)', 'j1'), emptyRow(), emptyRow()]
    setIodMemo({ rows, result: null, stale: false, status: { kind: 'none', text: '' }, dir: 'asc' } as never)
    w = mount(IodDialog, { props: { open: false }, attachTo: document.body })
    await w.setProps({ open: true })
    await tick()
    const row = (n: number) => q(`[data-testid="iod-row"][data-slot="${n}"]`)!
    expect(row(1).dataset.state).toBe('failed')
    const store = useAstrometryJobStore()
    store.job = failedJob('j1')
    await mountPill()
    q('[data-testid="as-pill-retry"]')!.click()
    await tick()
    expect(row(1).dataset.state).toBe('pending')
    expect(row(1).querySelector('[data-testid="iod-row-status"]')!.textContent!.trim()).toBe('Astrometry pending')
    expect(store.row).toBe(0)
    expect(store.job!.job_id).toBe('j2')
    // the new job finishing resolves that same row
    jobById.j2 = { job_id: 'j2', state: 'done', stage_text: 'done', result: { ra_deg: 10, dec_deg: 5, epoch_mid_iso: 'x', epoch_mid_unix: 1743771705 } }
    await vi.advanceTimersByTimeAsync(POLL_MS + 1)
    await tick()
    expect(row(1).dataset.state).toBe('valid')
  })

  it('a failed Retry leaves the row failed', async () => {
    useMountStore().snapshot = { site: { lat: 10, lon: 20, height_m: 30 }, limits: { lim_el_deg: 12 } } as never
    setIodMemo({ rows: [astrometryFailed(1743771705, 'x', 'j1'), emptyRow(), emptyRow()], result: null, stale: false, status: { kind: 'none', text: '' }, dir: 'asc' } as never)
    w = mount(IodDialog, { props: { open: false }, attachTo: document.body })
    await w.setProps({ open: true })
    await tick()
    const store = useAstrometryJobStore()
    store.job = failedJob('j1')
    postImpl = async () => { throw Object.assign(new Error('x'), { status: 503, detail: null }) }
    await store.resume()
    await tick()
    expect(q('[data-testid="iod-row"][data-slot="1"]')!.dataset.state).toBe('failed')
    expect(store.resumeError).toBe('Analysis service unavailable')
  })
})
