// @vitest-environment happy-dom
// T5.2d: the astrometry job store + pill (polling with fake timers, reattach, dismiss), the IOD row transitions
// (Add Astrometry -> pending -> solved -> H4; failed; overwrite), the relay error -> runStatus, and the pure helpers.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import AstrometryDialog from '../../app/components/analysis/AstrometryDialog.vue'
import AstrometryJobPill from '../../app/components/analysis/AstrometryJobPill.vue'
import IodDialog from '../../app/components/analysis/IodDialog.vue'
import { POLL_MS, useAstrometryJobStore } from '../../app/stores/astrometry-job'
import { useMountStore } from '../../app/stores/mount'
import {
  failureText, isDismissed, markDismissed, parseJob, relayErrorText, resetDismissed, stageLabel, type AstrometryJob,
} from '../../app/utils/astrometry-job'
import { rowFromJobResult, rowValid } from '../../app/utils/iod'
import { getIodMemo, resetIodMemo, setIodMemo } from '../../app/utils/iod-memo'
import type { AstrometryRunPayload } from '../../app/utils/astrometry'

const EPOCH = Date.UTC(2025, 3, 4, 13, 1, 30) / 1000
const RESULT = { ra_deg: 123.4567, dec_deg: -12.3456, epoch_mid_iso: '2025-04-04T13:01:45Z', epoch_mid_unix: EPOCH + 15.4, residual_arcsec: 1.2 }
const running = (id = 'j1', stage = 'solving on astrometry.net'): AstrometryJob => ({ job_id: id, state: 'solving', stage_text: stage })
const done = (id = 'j1'): AstrometryJob => ({ job_id: id, state: 'done', stage_text: 'done', result: RESULT })

let jobById: Record<string, AstrometryJob | number> = {}
let latest: AstrometryJob | number = 404
let fetches: string[] = []
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

const resp = (v: AstrometryJob | number) => (typeof v === 'number'
  ? { ok: false, status: v, json: async () => ({ detail: 'x' }) }
  : { ok: true, status: 200, json: async () => v })

let w: VueWrapper | null = null
const q = (s: string) => document.body.querySelector(s) as HTMLElement | null
const tick = async () => { await flushPromises(); await nextTick() }

beforeEach(() => {
  setActivePinia(createPinia())
  resetDismissed(); resetIodMemo()
  jobById = {}; latest = 404; fetches = []; posts.length = 0
  postImpl = async (path, body) => (path === '/api/transform'
    ? (body.dir === 'radec_to_altaz' ? { result: { alt: 41, az: 201 } } : { result: { ra_h: 6, dec_deg: -10 } })
    : { job_id: 'j1', state: 'uploading' })
  vi.useFakeTimers({ now: EPOCH * 1000, toFake: ['Date', 'setTimeout', 'clearTimeout'] })
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: { method?: string }) => {
    fetches.push(`${init?.method ?? 'GET'} ${url}`)
    if (url === '/api/astrometry/job') return resp(latest)
    const m = /^\/api\/astrometry\/job\/([^/]+)(\/cancel)?$/.exec(url)
    if (m) return m[2] ? { ok: true, status: 200, json: async () => ({}) } : resp(jobById[m[1]!] ?? 404)
    return { ok: true, status: 200, json: async () => ({}) }
  }))
})
afterEach(() => {
  w?.unmount(); w = null
  document.body.innerHTML = ''
  vi.useRealTimers(); vi.unstubAllGlobals()
})

describe('astrometry-job helpers', () => {
  it('parseJob accepts the known states; a done job needs its result', () => {
    expect(parseJob(running())).toMatchObject({ job_id: 'j1' })
    expect(parseJob({ job_id: 'j', state: 'weird' })).toBeNull()
    expect(parseJob({ job_id: 'j', state: 'done' })).toBeNull()
    expect(parseJob(done())).not.toBeNull()
    expect(parseJob(null)).toBeNull()
  })
  it('texts: failure reasons, stage, relay errors', () => {
    expect(failureText({ job_id: 'a', state: 'failed', error: 'Invalid astrometry.net API key' })).toBe('Invalid astrometry.net API key')
    expect(failureText({ job_id: 'a', state: 'timeout' })).toBe('Solve timed out')
    expect(failureText({ job_id: 'a', state: 'cancelled' })).toBe('Astrometry cancelled')
    expect(failureText({ job_id: 'a', state: 'failed' })).toBe('astrometry.net could not solve this frame')
    expect(stageLabel({ job_id: 'a', state: 'queued' })).toBe('queued')
    expect(stageLabel(running())).toBe('solving on astrometry.net')
    expect(relayErrorText(409, { detail: 'Set the astrometry.net API key in Station Settings' })).toBe('Set the astrometry.net API key in Station Settings')
    expect(relayErrorText(503, { detail: 'Analysis service unavailable' })).toBe('Analysis service unavailable')
    expect(relayErrorText(0, null)).toBe('Analysis service unavailable')
    expect(relayErrorText(422, { detail: [{ msg: 'x' }] })).toBe('The request was rejected')
  })
  it('dismissed ids are remembered (module + sessionStorage)', () => {
    expect(isDismissed('a')).toBe(false)
    markDismissed('a')
    expect(isDismissed('a')).toBe(true)
    expect(JSON.parse(sessionStorage.getItem('astrometry-dismissed')!)).toEqual(['a'])
  })
  it('a solved row: RA / Dec bright, epoch in whole seconds, the job id kept', () => {
    const r = rowFromJobResult('j1', RESULT)
    expect(r).toMatchObject({ state: 'partial', source: 'Astrometry', t: EPOCH + 15, ra: 123.4567, dec: -12.3456, bright: 'radec', jobId: 'j1' })
    expect(rowValid(r)).toBe(true)
  })
})

describe('job store + pill', () => {
  async function mountPill() {
    w = mount(AstrometryJobPill, { attachTo: document.body })
    await tick()
  }
  const text = () => q('[data-testid="as-pill-text"]')?.textContent?.trim()
  const title = () => q('[data-testid="as-pill-title"]')?.textContent?.trim()

  it('shows nothing without a job; a 404 on reattach keeps it empty', async () => {
    await mountPill()
    expect(q('[data-testid="as-pill"]')).toBeNull()
    expect(fetches).toContain('GET /api/astrometry/job')
  })

  it('reattaches an active job, polls every 2.5 s, shows the stage and a Cancel', async () => {
    latest = running('j1', 'queued at astrometry.net')
    jobById.j1 = running('j1', 'queued at astrometry.net')
    await mountPill()
    expect(text()).toBe('queued at astrometry.net')
    expect(title()).toBe('Astrometry')
    expect(q('[data-testid="as-pill-cancel"]')).toBeTruthy()
    jobById.j1 = running('j1', 'solving')
    await vi.advanceTimersByTimeAsync(POLL_MS - 1)
    expect(fetches.filter(f => f === 'GET /api/astrometry/job/j1')).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(2)
    expect(fetches.filter(f => f === 'GET /api/astrometry/job/j1')).toHaveLength(1)
    expect(text()).toBe('solving')
    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(fetches.filter(f => f === 'GET /api/astrometry/job/j1')).toHaveLength(2)
    // done: polling stops
    jobById.j1 = done()
    await vi.advanceTimersByTimeAsync(POLL_MS)
    expect(q('[data-testid="as-pill"]')!.dataset.kind).toBe('done')
    const n = fetches.length
    await vi.advanceTimersByTimeAsync(POLL_MS * 3)
    expect(fetches.length).toBe(n)
  })

  it('Cancel posts the cancel and re-reads the job', async () => {
    latest = running(); jobById.j1 = running()
    await mountPill()
    jobById.j1 = { job_id: 'j1', state: 'cancelled' }
    ;(q('[data-testid="as-pill-cancel"]') as HTMLElement).click()
    await tick()
    expect(fetches).toContain('POST /api/astrometry/job/j1/cancel')
    expect(q('[data-testid="as-pill"]')!.dataset.kind).toBe('failed')
    expect(text()).toBe('Astrometry cancelled')
    expect(title()).toBe('Astrometry failed')
  })

  it('a finished job after a refresh: "apply to a row…"; the click asks IOD to open and apply; x dismisses and stays dismissed', async () => {
    latest = done()
    await mountPill()
    expect(text()).toBe('apply to a row…')
    expect(title()).toBe('Astrometry solved')
    const store = useAstrometryJobStore()
    const tick0 = store.openIodTick
    ;(q('[data-testid="as-pill-text"]') as HTMLElement).click()
    expect(store.applyRequested).toBe(true)
    expect(store.openIodTick).toBe(tick0 + 1)
    ;(q('[data-testid="as-pill-dismiss"]') as HTMLElement).click()
    await tick()
    expect(q('[data-testid="as-pill"]')).toBeNull()
    // a second page load: the same finished job is not offered again
    w.unmount()
    setActivePinia(createPinia())
    await mountPill()
    expect(q('[data-testid="as-pill"]')).toBeNull()
  })

  it('a failed job shows its reason; a lost job (404 while polling) fails readably', async () => {
    latest = { job_id: 'j2', state: 'failed', error: 'Invalid astrometry.net API key' }
    await mountPill()
    expect(text()).toBe('Invalid astrometry.net API key')
    const store = useAstrometryJobStore()
    store.reset()
    store.start('j9')
    await vi.advanceTimersByTimeAsync(POLL_MS + 1)
    expect(text()).toContain('not found')
  })
})

describe('IOD wiring', () => {
  const row = (slot: number) => q(`[data-testid="iod-row"][data-slot="${slot}"]`)!
  const cell = (slot: number, id: string) => row(slot).querySelector(`[data-testid="${id}"]`) as HTMLElement
  const click = async (el: HTMLElement) => { el.click(); await tick() }
  const payload = (slot: number | null, extra: Partial<AstrometryRunPayload['meta']> = {}): AstrometryRunPayload => ({
    image_id: 'img1', detections: [], epoch_iso: '2025-04-04T13:01:30Z', exposure_s: 30, slot,
    meta: { dims: [10, 10], epoch_iso: null, exposure_s: null, focal_mm: null, pixel_um: null, warnings: [], ...extra },
  })
  const astroOpenState = () => w!.findComponent(AstrometryDialog).props('open')

  async function openIod() {
    useMountStore().snapshot = { site: { lat: 10, lon: 20, height_m: 30 }, limits: { lim_el_deg: 12 } } as never
    w = mount(IodDialog, { props: { open: false }, attachTo: document.body })
    await w.setProps({ open: true })
    await tick()
  }
  async function addAstrometry(slot: number) {
    await click(cell(slot, 'iod-menu-btn'))
    await click(q('[data-testid="iod-add-astrometry"]')!)
  }
  const emitRun = async (p: AstrometryRunPayload) => {
    w!.findComponent(AstrometryDialog).vm.$emit('run', p)
    await tick()
  }

  it('Add Astrometry is enabled and opens the Astrometry dialog for that row', async () => {
    await openIod()
    await click(cell(2, 'iod-menu-btn'))
    expect((q('[data-testid="iod-add-astrometry"]') as HTMLButtonElement).disabled).toBe(false)
    await click(q('[data-testid="iod-add-astrometry"]')!)
    expect(astroOpenState()).toBe(true)
    expect(w!.findComponent(AstrometryDialog).props('iodSlot')).toBe(1)
  })

  it('RUN: the relay gets the hints only when known; 202 -> the row is pending with the job id, the dialog closes, the store follows the job', async () => {
    await openIod()
    await addAstrometry(1)
    await emitRun(payload(0, { focal_mm: 620 }))
    const relay = posts.find(p => p.path === '/api/mount/astrometry/run')!
    expect(relay.body).toMatchObject({ image_id: 'img1', epoch_iso: '2025-04-04T13:01:30Z', exposure_s: 30, focal_mm: 620 })
    expect('pixel_um' in relay.body).toBe(false)
    expect(astroOpenState()).toBe(false)
    expect(row(1).dataset.state).toBe('pending')
    expect(cell(1, 'iod-row-status').textContent!.trim()).toBe('Astrometry pending')
    expect(cell(1, 'iod-epoch').textContent!.trim()).toBe('2025-04-04 13:01:45')
    const store = useAstrometryJobStore()
    expect(store.job?.job_id).toBe('j1')
    expect(store.row).toBe(0)
  })

  it('a relay error (409 / 503) is shown in the Astrometry dialog, which stays open; the row is untouched', async () => {
    await openIod()
    await addAstrometry(1)
    postImpl = async () => { throw Object.assign(new Error('x'), { status: 409, detail: { detail: 'Set the astrometry.net API key in Station Settings' } }) }
    await emitRun(payload(0))
    expect(astroOpenState()).toBe(true)
    expect(q('[data-testid="as-status"]')!.textContent).toContain('Set the astrometry.net API key in Station Settings')
    expect(row(1).dataset.state).toBe('empty')
    postImpl = async () => { throw Object.assign(new Error('x'), { status: 503, detail: { detail: 'Analysis service unavailable' } }) }
    await emitRun(payload(0))
    expect(q('[data-testid="as-status"]')!.textContent).toContain('Analysis service unavailable')
    expect(useAstrometryJobStore().job).toBeNull()
  })

  it('the job finishing -> RA / Dec bright, Source Astrometry, H4 fills Az / El dimmed at the mid-exposure epoch; outputs go stale', async () => {
    await openIod()
    await addAstrometry(1)
    await emitRun(payload(0))
    jobById.j1 = done()
    await vi.advanceTimersByTimeAsync(POLL_MS + 1)
    await tick()
    expect(row(1).dataset.state).toBe('valid')
    expect(cell(1, 'iod-ra').textContent!.trim()).toBe('123.457°')
    expect(cell(1, 'iod-ra').dataset.dim).toBe('0')
    expect(cell(1, 'iod-az').textContent!.trim()).toBe('201.000°')
    expect(cell(1, 'iod-az').dataset.dim).toBe('1')
    expect(cell(1, 'iod-source').textContent!.trim()).toBe('Astrometry')
    const h4 = posts.filter(p => p.path === '/api/transform').at(-1)!
    expect(h4.body).toMatchObject({ dir: 'radec_to_altaz', t_utc: new Date((EPOCH + 15) * 1000).toISOString() })
    expect(useAstrometryJobStore().row).toBe(0)
  })

  it('the job finishing while IOD is closed is applied to the memo', async () => {
    await openIod()
    await addAstrometry(2)
    await emitRun(payload(1))
    await w!.setProps({ open: false }); await tick()
    expect(getIodMemo()!.rows[1]!.state).toBe('pending')
    jobById.j1 = done()
    await vi.advanceTimersByTimeAsync(POLL_MS + 1)
    await tick()
    const r = getIodMemo()!.rows[1]!
    expect(r).toMatchObject({ source: 'Astrometry', ra: 123.4567, dec: -12.3456, jobId: 'j1' })
    expect(r.state).toBe('valid')
    expect(r.az).toBe(201)
    await w!.setProps({ open: true }); await tick()
    expect(row(2).dataset.state).toBe('valid')
  })

  it('failed / timeout / cancelled -> "Astrometry failed" with the reason in the title; replace / remove without a prompt', async () => {
    await openIod()
    await addAstrometry(1)
    await emitRun(payload(0))
    jobById.j1 = { job_id: 'j1', state: 'failed', error: 'Solved scale does not match the camera' }
    await vi.advanceTimersByTimeAsync(POLL_MS + 1)
    await tick()
    expect(row(1).dataset.state).toBe('failed')
    expect(cell(1, 'iod-row-status').textContent!.trim()).toBe('Astrometry failed')
    expect(cell(1, 'iod-row-status').title).toBe('Solved scale does not match the camera')
    await click(cell(1, 'iod-menu-btn'))
    await click(q('[data-testid="iod-add-astrometry"]')!)
    expect(q('[data-testid="iod-confirm"]')).toBeNull()
    expect(astroOpenState()).toBe(true)
  })

  it('overwrite: a valid row asks "Replace row N?" first; Yes opens the Astrometry dialog', async () => {
    await openIod()
    await w!.setProps({ open: false }); await tick()
    const valid = { ...rowFromJobResult('old', RESULT), state: 'valid' as const, az: 1, el: 2 }
    setIodMemo({ rows: [valid, ...getIodMemo()!.rows.slice(1)], result: null, stale: false, status: { kind: 'none', text: '' }, dir: 'asc' })
    await w!.setProps({ open: true }); await tick()
    await addAstrometry(1)
    expect(q('[data-testid="iod-confirm"]')!.textContent).toContain('Replace row 1?')
    expect(astroOpenState()).toBe(false)
    await click(q('[data-testid="iod-confirm-yes"]')!)
    expect(astroOpenState()).toBe(true)
  })

  it('after a refresh the pill applies a finished job to the first empty row when IOD opens', async () => {
    const store = useAstrometryJobStore()
    store.job = done('j7')
    store.requestApply()
    await openIod()
    await vi.advanceTimersByTimeAsync(10)
    await tick()
    expect(row(1).dataset.state).toBe('valid')
    expect(cell(1, 'iod-source').textContent!.trim()).toBe('Astrometry')
    expect(store.applyRequested).toBe(false)
    expect(store.row).toBe(0)
  })
})
