// @vitest-environment happy-dom
// PassAnalysisDialog flow with fakes: LIST PASSES gating, request body, table, [Track] on eligible rows.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import PassAnalysisDialog from '../../app/components/analysis/PassAnalysisDialog.vue'
import { resetPassModalMemo } from '../../app/utils/pass-analysis-memo'
import { useMountStore } from '../../app/stores/mount'
import { useSelectionStore } from '../../app/stores/selection'

const NOW = Date.UTC(2026, 9, 12, 12, 0, 0)
const bodies: Array<Record<string, unknown>> = []
let passes: unknown[] = []
vi.mock('~/composables/useGatewayApi', () => ({
  useGatewayApi: () => ({ post: async () => ({ ok: true, notes: [] }) }),
}))

let w: VueWrapper | null = null
const q = (sel: string) => document.body.querySelector(sel) as HTMLElement | null
const qa = (sel: string) => [...document.body.querySelectorAll(sel)] as HTMLElement[]

async function openDialog(norad?: number) {
  setActivePinia(createPinia())
  const store = useMountStore()
  store.snapshot = { site: { lat: 10, lon: 20, height_m: 30 }, limits: { lim_el_deg: 12 } } as never
  store.sendCommand = vi.fn(async () => ({ id: 1, cmd: 'track_object', ok: true })) as never
  if (norad) useSelectionStore().select(norad)
  w = mount(PassAnalysisDialog, { props: { open: false }, attachTo: document.body })
  await w.setProps({ open: true })
  await flushPromises()
  await nextTick()
}

beforeEach(() => {
  resetPassModalMemo()
  vi.useFakeTimers({ now: NOW })
  bodies.length = 0
  vi.stubGlobal('useRuntimeConfig', () => ({ app: { baseURL: '/' } }))
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: { body?: string }) => {
    if (url === '/api/passes') {
      bodies.push(JSON.parse(init!.body!))
      return { ok: true, status: 200, json: async () => ({ name: 'TERRA', norad: 25994, tle_epoch: null, tle_age_days: 2, passes }) }
    }
    return { ok: true, json: async () => [{ norad: 25994, name: 'TERRA', tle_age_days: 2, orbit_class: 'LEO' }] }
  }))
})
afterEach(() => {
  w?.unmount(); w = null
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('PassAnalysisDialog', () => {
  it('empty: LIST PASSES disabled with the reason, placeholder shown', async () => {
    await openDialog()
    const list = q('[data-testid="pa-list"]') as HTMLButtonElement
    expect(list.disabled).toBe(true)
    expect(list.title).toBe('Select a satellite')
    expect(q('[data-testid="pa-sat"]')!.textContent).toContain('no satellite selected')
    expect(q('[data-testid="pa-mode"] button.active')!.textContent!.trim()).toBe('OPTICAL')
  })

  it('prefills the selected object; lists passes; [Track] only on eligible rows; sends track_object', async () => {
    const t = NOW / 1000
    passes = [
      { t_rise: t - 3600, t_set: t - 3000, rise_az: 1, set_az: 2, t_peak: t - 3300, max_el: 30, duration_s: 600, visible_s: 100 },
      { t_rise: t - 100, t_set: t + 500, rise_az: 1, set_az: 2, t_peak: t + 100, max_el: 50, duration_s: 600, visible_s: 400 },
      { t_rise: t + 3000, t_set: t + 3600, rise_az: 1, set_az: 2, t_peak: t + 3300, max_el: 20, duration_s: 600, visible_s: 200 },
    ]
    await openDialog(25994)
    expect(q('[data-testid="pa-sat"]')!.textContent).toContain('TERRA (25994)')
    const list = q('[data-testid="pa-list"]') as HTMLButtonElement
    expect(list.disabled).toBe(false)
    list.click()
    await flushPromises()
    expect(bodies[0]).toMatchObject({ norad: 25994, site: { lat: 10, lon: 20, alt_m: 30 }, lim_el: 12, mode: 'optical' })
    expect(bodies[0]!.end_unix as number - (bodies[0]!.start_unix as number)).toBe(86400)
    expect(qa('[data-testid="pa-row"]')).toHaveLength(3)
    await vi.advanceTimersByTimeAsync(1100)
    const rows = qa('[data-testid="pa-row"]')
    expect(rows.map(r => !!r.querySelector('[data-testid="pa-track"]'))).toEqual([false, true, false])
    const btn = rows[1]!.querySelector('[data-testid="pa-track"]') as HTMLButtonElement
    expect(btn.disabled).toBe(false)
    btn.click()
    await flushPromises()
    expect(useMountStore().sendCommand).toHaveBeenCalledWith('track_object', { norad: 25994 })
  })

  it('an invalid window disables LIST PASSES with the reason', async () => {
    await openDialog(25994)
    ;(w!.vm as unknown as { $: { setupState: { end: Date } } }).$.setupState.end = new Date(NOW - 1000)
    await nextTick()
    const list = q('[data-testid="pa-list"]') as HTMLButtonElement
    expect(list.disabled).toBe(true)
    expect(list.title).toBe('End must be after Start')
    expect(q('[data-testid="pa-end"]')!.classList.contains('is-invalid')).toBe(true)
  })
  it('reopening recalls the last state; rises / sets outside the window show a dash', async () => {
    const t = NOW / 1000
    passes = [
      { t_rise: t - 600, t_set: t + 300, rise_az: 11, set_az: 22, t_peak: t, max_el: 40, duration_s: 300, visible_s: 300 },
      { t_rise: t + 86000, t_set: t + 86800, rise_az: 33, set_az: 44, t_peak: t + 86400, max_el: 25, duration_s: 400, visible_s: 400 },
    ]
    await openDialog(25994)
    ;(q('[data-testid="pa-list"]') as HTMLButtonElement).click()
    await flushPromises()
    const cells = () => qa('[data-testid="pa-row"]').map(r => [...r.querySelectorAll('td')].slice(0, 5).map(c => c.textContent!.trim()))
    const before = cells()
    expect(before[0]![0]).toBe('—') // rise before Start
    expect(before[0]![1]).toBe('—')
    expect(before[1]![3]).toBe('—') // set after End
    expect(before[1]![4]).toBe('—')
    useSelectionStore().select(39500) // a different dashboard selection does not override the recalled state
    await w!.setProps({ open: false })
    await nextTick()
    await w!.setProps({ open: true })
    await flushPromises()
    expect(q('[data-testid="pa-sat"]')!.textContent).toContain('TERRA (25994)')
    expect(cells()).toEqual(before)
    expect(bodies).toHaveLength(1) // recalled, not recomputed
  })
})
