// @vitest-environment happy-dom
// TrackTargetDialog in mode="pick" (T5.1c): SELECT SATELLITE picker, no gateway check, picked(row) + close.
// Track mode keeps its own behaviour (TRACK SELECTED TARGET + status line).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import TrackTargetDialog from '../../app/components/track/TrackTargetDialog.vue'

const posts: string[] = []
vi.mock('~/composables/useGatewayApi', () => ({
  useGatewayApi: () => ({ post: async (path: string) => { posts.push(path); return { ok: true } } }),
}))

const ROWS = [
  { norad: 25994, name: 'TERRA', cospar: null, country: 'US', object_type: null, orbit_class: 'LEO', tle_epoch: '2026-10-01T00:00:00Z', tle_age_days: 3 },
  { norad: 7, name: 'OTHER', cospar: null, country: 'US', object_type: null, orbit_class: 'LEO', tle_epoch: null, tle_age_days: null },
]

let w: VueWrapper | null = null
const q = (sel: string) => document.body.querySelector(sel) as HTMLElement | null

async function openDialog(mode?: 'pick') {
  w = mount(TrackTargetDialog, { props: { open: false, mode }, attachTo: document.body })
  await w.setProps({ open: true })
  await flushPromises()
  await nextTick()
}

beforeEach(() => {
  setActivePinia(createPinia())
  posts.length = 0
  vi.stubGlobal('useRuntimeConfig', () => ({ app: { baseURL: '/' } }))
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ROWS })))
  vi.useFakeTimers()
})
afterEach(() => {
  w?.unmount(); w = null
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('TrackTargetDialog pick mode', () => {
  it('title, SELECT TARGET (disabled until a row is selected), no status line, no gateway check', async () => {
    await openDialog('pick')
    expect(q('[data-testid="tt-dialog"]')!.textContent).toContain('SELECT SATELLITE')
    expect(q('[data-testid="tt-track"]')).toBeNull()
    expect(q('[data-testid="tt-status"]')).toBeNull()
    const btn = q('[data-testid="tt-pick"]') as HTMLButtonElement
    expect(btn.textContent!.trim()).toBe('SELECT TARGET')
    expect(btn.disabled).toBe(true)
    q('[data-norad="25994"]')!.click()
    await nextTick()
    expect(btn.disabled).toBe(false)
    await vi.advanceTimersByTimeAsync(3000)
    expect(posts).toEqual([])
  })

  it('the button emits picked(row) and closes', async () => {
    await openDialog('pick')
    q('[data-norad="25994"]')!.click()
    await nextTick()
    ;(q('[data-testid="tt-pick"]') as HTMLButtonElement).click()
    await nextTick()
    expect(w!.emitted('picked')![0]![0]).toMatchObject({ norad: 25994, name: 'TERRA' })
    expect(w!.emitted('update:open')!.at(-1)).toEqual([false])
  })

  it('double-click picks that row', async () => {
    await openDialog('pick')
    q('[data-norad="7"]')!.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await nextTick()
    expect(w!.emitted('picked')![0]![0]).toMatchObject({ norad: 7 })
    expect(w!.emitted('update:open')!.at(-1)).toEqual([false])
  })

  it('Enter picks the selected row', async () => {
    await openDialog('pick')
    q('[data-norad="25994"]')!.click()
    await nextTick()
    q('[data-testid="tt-table"]')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await nextTick()
    expect(w!.emitted('picked')![0]![0]).toMatchObject({ norad: 25994 })
  })

  it('track mode is unchanged: TRACK SELECTED TARGET + status line + gateway check, no picked', async () => {
    await openDialog()
    expect(q('[data-testid="tt-dialog"]')!.textContent).toContain('TRACK TARGET')
    expect(q('[data-testid="tt-pick"]')).toBeNull()
    expect(q('[data-testid="tt-track"]')).not.toBeNull()
    expect(q('[data-testid="tt-status"]')!.textContent!.trim()).toBe('Select a target')
    q('[data-norad="25994"]')!.click()
    await vi.advanceTimersByTimeAsync(50)
    expect(posts).toContain('/api/mount/check')
    expect(w!.emitted('picked')).toBeUndefined()
  })
})
