// @vitest-environment happy-dom
// IodDialog flow with fakes: add a manual row -> H4 fills the dimmed pair -> 3 rows -> IOD -> outputs; a 422 shows its detail;
// editing marks the outputs stale; recall after close / reopen; overwrite confirms.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import IodDialog from '../../app/components/analysis/IodDialog.vue'
import { astrometryFailed, astrometryPending } from '../../app/utils/iod'
import { getIodMemo, resetIodMemo, setIodMemo } from '../../app/utils/iod-memo'
import { useMountStore } from '../../app/stores/mount'

const NOW = Date.UTC(2026, 9, 3, 15, 0, 0)
const transforms: Array<Record<string, unknown>> = []
const iodBodies: Array<Record<string, unknown>> = []
let transformFails = false
let iodStatus = 200
let iodBody: unknown
const OK = {
  epoch_unix: 1, r_km: [-1328.526, -414.964, 6921.178], v_kms: [-6.614, 3.399, -1.065], method: 'gauss+herrick-gibbs', iterations: 4,
  coe: { a_km: 7000, e: 0.001, i_deg: 97, raan_deg: 1, argp_deg: 2, nu_deg: 3, period_min: 97 },
  tle: { line1: '1 A9999U 26278.62673600 .00000143 00000-0 00000-0 0 0002', line2: '2 A9999 97.9314 333.3191 0002038 173.3232 348.1036 14.61177191 09' },
  tle_fit: { iterations: 3, pos_err_km: 0.001, vel_err_ms: 0.1, converged: true },
}
vi.mock('~/composables/useGatewayApi', () => ({
  useGatewayApi: () => ({
    post: async (_p: string, b: Record<string, unknown>) => {
      transforms.push(b)
      if (transformFails) throw new Error('down')
      return b.dir === 'altaz_to_radec' ? { result: { ra_h: 6, dec_deg: -10 } } : { result: { alt: 40, az: 200 } }
    },
  }),
}))

let w: VueWrapper | null = null
const q = (s: string) => document.body.querySelector(s) as HTMLElement | null
const qa = (s: string) => [...document.body.querySelectorAll(s)] as HTMLElement[]
const row = (slot: number) => q(`[data-testid="iod-row"][data-slot="${slot}"]`)!
const cell = (slot: number, id: string) => row(slot).querySelector(`[data-testid="${id}"]`) as HTMLElement
const run = () => q('[data-testid="iod-run"]') as HTMLButtonElement

const rowValidState = (slot: number) => ['partial', 'valid'].includes(row(slot).dataset.state ?? '') && cell(slot, 'iod-ra').textContent!.trim() !== '—'

async function openDialog() {
  setActivePinia(createPinia())
  useMountStore().snapshot = { site: { lat: 10, lon: 20, height_m: 30 }, limits: { lim_el_deg: 12 } } as never
  w = mount(IodDialog, { props: { open: false }, attachTo: document.body })
  await w.setProps({ open: true })
  await flushPromises(); await nextTick()
}
async function closeReopen() {
  await w!.setProps({ open: false }); await nextTick()
  await w!.setProps({ open: true }); await flushPromises(); await nextTick()
}
async function click(el: HTMLElement) { el.click(); await flushPromises(); await nextTick() }
async function typeIn(el: HTMLElement, v: string) {
  (el as HTMLInputElement).value = v
  el.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
}
async function enter(slot: number) {
  row(slot).dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
  await flushPromises(); await nextTick()
}
async function addManual(slot: number, a: string, b: string, pair: 'azel' | 'radec') {
  await click(cell(slot, 'iod-menu-btn'))
  await click(q('[data-testid="iod-add-manual"]')!)
  const [f1, f2] = pair === 'azel' ? ['iod-az', 'iod-el'] : ['iod-ra', 'iod-dec']
  await typeIn(cell(slot, f1!), a)
  await typeIn(cell(slot, f2!), b)
  await enter(slot)
}
async function fillThree() {
  vi.setSystemTime(NOW); await addManual(1, '10', '20', 'radec')
  vi.setSystemTime(NOW + 60_000); await addManual(2, '11', '21', 'radec')
  vi.setSystemTime(NOW + 120_000); await addManual(3, '12', '22', 'radec')
}

beforeEach(() => {
  resetIodMemo()
  transforms.length = 0; iodBodies.length = 0
  transformFails = false; iodStatus = 200; iodBody = OK
  vi.useFakeTimers({ now: NOW, toFake: ['Date', 'setTimeout', 'clearTimeout'] })
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: { body?: string }) => {
    if (url === '/api/iod') {
      iodBodies.push(JSON.parse(init!.body!))
      return { ok: iodStatus === 200, status: iodStatus, json: async () => iodBody }
    }
    return { ok: true, status: 200, json: async () => ({}) }
  }))
})
afterEach(() => {
  w?.unmount(); w = null
  document.body.innerHTML = ''
  vi.useRealTimers(); vi.unstubAllGlobals()
})

describe('IodDialog', () => {
  it('opens empty: three empty rows, IOD disabled with the reason, Add Astrometry enabled, Remove inactive', async () => {
    await openDialog()
    expect(qa('[data-testid="iod-row"]')).toHaveLength(3)
    expect(run().disabled).toBe(true)
    expect(run().title).toBe('Three observations are needed (0 of 3)')
    await click(cell(1, 'iod-menu-btn'))
    expect((q('[data-testid="iod-add-astrometry"]') as HTMLButtonElement).disabled).toBe(false)
    expect((q('[data-testid="iod-remove"]') as HTMLButtonElement).disabled).toBe(true)
  })

  it('Add Manual: epoch = now, typing one pair clears the other, Enter computes the dimmed pair at the row epoch', async () => {
    await openDialog()
    await click(cell(1, 'iod-menu-btn'))
    await click(q('[data-testid="iod-add-manual"]')!)
    expect(cell(1, 'iod-epoch')).toBeTruthy()
    await typeIn(cell(1, 'iod-az'), '100.1234')
    await typeIn(cell(1, 'iod-ra'), '10')
    expect((cell(1, 'iod-az') as HTMLInputElement).value).toBe('')
    await typeIn(cell(1, 'iod-az'), '100.1234')
    await typeIn(cell(1, 'iod-el'), '40.5')
    await enter(1)
    expect(transforms[0]).toMatchObject({ dir: 'altaz_to_radec', t_utc: '2026-10-03T15:00:00.000Z', values: { alt: 40.5, az: 100.1234 } })
    expect(cell(1, 'iod-az').textContent!.trim()).toBe('100.123°')
    expect(cell(1, 'iod-az').dataset.dim).toBe('0')
    expect(cell(1, 'iod-ra').textContent!.trim()).toBe('90.000°')
    expect(cell(1, 'iod-ra').dataset.dim).toBe('1')
    expect(cell(1, 'iod-dec').textContent!.trim()).toBe('-10.000°')
    expect(cell(1, 'iod-source').textContent!.trim()).toBe('Manual')
  })

  it('an out-of-range value keeps the row in edit with the field marked; Esc cancels', async () => {
    await openDialog()
    await click(cell(2, 'iod-menu-btn'))
    await click(q('[data-testid="iod-add-manual"]')!)
    await typeIn(cell(2, 'iod-az'), '10')
    await typeIn(cell(2, 'iod-el'), '-5')
    await enter(2)
    expect(cell(2, 'iod-el').classList.contains('iod-bad')).toBe(true)
    expect(transforms).toHaveLength(0)
    q('[data-slot="dialog-content"]')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(row(2).dataset.state).toBe('empty')
  })

  it('3 rows -> IOD button ready -> request -> Done + outputs; copy buttons', async () => {
    const writeText = vi.fn(async () => {})
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    await openDialog()
    await fillThree()
    expect(run().disabled).toBe(false)
    await click(run())
    expect(iodBodies[0]).toMatchObject({ site: { lat: 10, lon: 20, alt_m: 30 } })
    expect((iodBodies[0]!.obs as unknown[])).toHaveLength(3)
    expect((iodBodies[0]!.obs as Array<{ ra_deg: number }>)[0]).toMatchObject({ ra_deg: 10, dec_deg: 20 })
    expect(q('[data-testid="iod-status"]')!.textContent!.trim()).toBe('Done · Gauss + Herrick-Gibbs')
    expect(q('[data-testid="iod-state"]')!.textContent).toBe('R_2 : [ -1328.526, -414.964, 6921.178 ]   V_2 : [ -6.614, 3.399, -1.065 ]')
    expect(q('[data-testid="iod-tle"]')!.textContent).toBe(`${OK.tle.line1}\n${OK.tle.line2}`)
    await click(q('[data-testid="iod-copy-state"]')!)
    expect(writeText).toHaveBeenCalledWith(q('[data-testid="iod-state"]')!.textContent)
    expect(q('[data-testid="iod-copy-state"]')!.textContent!.trim()).toBe('Copied')
    await click(q('[data-testid="iod-copy-tle"]')!)
    expect(writeText).toHaveBeenLastCalledWith(`${OK.tle.line1}\n${OK.tle.line2}`)
  })

  it('a 422 shows its detail as-is; a 503 shows "Analysis service unavailable"', async () => {
    await openDialog()
    await fillThree()
    iodStatus = 422; iodBody = { detail: 'No orbit fits these observations (check the epochs and angles)' }
    await click(run())
    expect(q('[data-testid="iod-status"]')!.textContent!.trim()).toBe('No orbit fits these observations (check the epochs and angles)')
    expect(q('[data-testid="iod-state"]')!.textContent).toBe('')
    iodStatus = 503; iodBody = { detail: 'x' }
    await click(run())
    expect(q('[data-testid="iod-status"]')!.textContent!.trim()).toBe('Analysis service unavailable')
  })

  it('editing a row after a result marks the outputs stale until the next run', async () => {
    await openDialog()
    await fillThree()
    await click(run())
    vi.setSystemTime(NOW + 90_000) // a distinct epoch for the replacement row
    await click(cell(2, 'iod-menu-btn'))
    await click(q('[data-testid="iod-add-manual"]')!)
    expect(q('[data-testid="iod-confirm"]')!.textContent).toContain('Replace row 2?')
    await click(q('[data-testid="iod-confirm-yes"]')!)
    await typeIn(cell(2, 'iod-ra'), '50')
    await typeIn(cell(2, 'iod-dec'), '5')
    await enter(2)
    expect(q('[data-testid="iod-status"]')!.textContent!.trim()).toBe('Inputs changed — run again')
    expect(q('.iod-box')!.classList.contains('is-stale')).toBe(true)
    await click(run())
    expect(q('[data-testid="iod-status"]')!.textContent!.trim()).toBe('Done · Gauss + Herrick-Gibbs')
    expect(q('.iod-box')!.classList.contains('is-stale')).toBe(false)
  })

  for (const slot of [1, 2, 3]) {
    it(`Replace row ${slot}: Yes opens THAT row's inputs focused, a focus loss to nothing keeps them, Enter updates the row`, async () => {
      await openDialog()
      await fillThree()
      vi.setSystemTime(NOW + 600_000)
      await click(cell(slot, 'iod-menu-btn'))
      await click(q('[data-testid="iod-add-manual"]')!)
      await click(q('[data-testid="iod-confirm-yes"]')!)
      const az = cell(slot, 'iod-az') as HTMLInputElement
      expect(az).toBeTruthy()
      expect(document.activeElement).toBe(az)
      // the removed Yes button: a focusout with no new focused element must not commit / cancel the edit
      row(slot).dispatchEvent(new FocusEvent('focusout', { bubbles: true, relatedTarget: null }))
      await vi.advanceTimersByTimeAsync(5)
      expect(cell(slot, 'iod-az')).toBeTruthy()
      await typeIn(cell(slot, 'iod-az'), '77.123456')
      await typeIn(cell(slot, 'iod-el'), '33')
      await enter(slot)
      expect(cell(slot, 'iod-az').textContent!.trim()).toBe('77.123°')
      expect(transforms.at(-1)).toMatchObject({ dir: 'altaz_to_radec', values: { alt: 33, az: 77.123456 } })
      for (const o of [1, 2, 3].filter(x => x !== slot)) expect(cell(o, 'iod-ra').textContent!.trim()).not.toBe('—')
    })
  }

  it('a pointer press on the dialog outside the edited row commits it', async () => {
    await openDialog()
    await click(cell(1, 'iod-menu-btn'))
    await click(q('[data-testid="iod-add-manual"]')!)
    await typeIn(cell(1, 'iod-ra'), '10')
    await typeIn(cell(1, 'iod-dec'), '20')
    q('[data-testid="iod-status"]')!.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    await flushPromises(); await nextTick()
    expect(row(1).dataset.state).toBe('valid')
  })

  it('every H4 request carries the Settings site; without a site H4 is not called', async () => {
    await openDialog()
    await addManual(1, '10', '20', 'radec')
    expect(transforms[0]!.site).toEqual({ lat: 10, lon: 20, height_m: 30 })
    useMountStore().snapshot = { limits: { lim_el_deg: 12 } } as never
    await nextTick()
    transforms.length = 0
    await addManual(2, '11', '21', 'radec')
    expect(transforms).toHaveLength(0)
    expect(row(2).dataset.state).toBe('partial')
  })

  const editMemo = () => {
    setIodMemo({
      rows: [
        { state: 'valid', source: 'Astrometry', t: 1_000_000, az: 5, el: 6, ra: 12.3456789, dec: -4.5678912, bright: 'radec', jobId: 'j1' },
        astrometryFailed(1_000_100), { state: 'empty', source: null, t: null, az: null, el: null, ra: null, dec: null, bright: null },
      ],
      result: OK as never, stale: false, status: { kind: 'done', text: 'Done · x' }, dir: 'asc',
    })
  }

  it('Edit Entry: prefilled at full precision, Esc restores, Enter makes an Astrometry row Manual and marks outputs stale', async () => {
    editMemo()
    await openDialog()
    await click(cell(2, 'iod-menu-btn'))
    expect((q('[data-testid="iod-edit"]') as HTMLButtonElement).disabled).toBe(true)
    await click(cell(2, 'iod-menu-btn'))
    await click(cell(3, 'iod-menu-btn'))
    expect((q('[data-testid="iod-edit"]') as HTMLButtonElement).disabled).toBe(true)
    await click(cell(3, 'iod-menu-btn'))
    await click(cell(1, 'iod-menu-btn'))
    expect(qa('.iod-mi').map(b => b.textContent!.trim())).toEqual(['Add Manual', 'Add Astrometry', 'Edit Entry', 'Remove Entry'])
    await click(q('[data-testid="iod-edit"]')!)
    expect((cell(1, 'iod-ra') as HTMLInputElement).value).toBe('12.3456789')
    expect((cell(1, 'iod-dec') as HTMLInputElement).value).toBe('-4.5678912')
    expect((cell(1, 'iod-az') as HTMLInputElement).value).toBe('')
    expect(document.activeElement).toBe(cell(1, 'iod-ra'))
    await typeIn(cell(1, 'iod-ra'), '99.9')
    q('[data-slot="dialog-content"]')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(cell(1, 'iod-source').textContent!.trim()).toBe('Astrometry')
    expect(cell(1, 'iod-ra').textContent!.trim()).toBe('12.346°')
    expect(q('.iod-box')!.classList.contains('is-stale')).toBe(false)
    w!.unmount(); w = null; document.body.innerHTML = '' // the synthetic Esc may also close the dialog: a fresh one for the commit path
    editMemo()
    await openDialog()
    await click(cell(1, 'iod-menu-btn'))
    await click(q('[data-testid="iod-edit"]')!)
    await typeIn(cell(1, 'iod-ra'), '99.9')
    await enter(1)
    expect(cell(1, 'iod-source').textContent!.trim()).toBe('Manual')
    expect(cell(1, 'iod-ra').textContent!.trim()).toBe('99.900°')
    expect(transforms.at(-1)).toMatchObject({ dir: 'radec_to_altaz', t_utc: new Date(1_000_000_000).toISOString() })
    expect(q('.iod-box')!.classList.contains('is-stale')).toBe(true)
  })

  it('sends the typed 6-decimal values to /api/iod untouched', async () => {
    await openDialog()
    vi.setSystemTime(NOW); await addManual(1, '10.123456', '20.654321', 'radec')
    vi.setSystemTime(NOW + 60_000); await addManual(2, '11', '21', 'radec')
    vi.setSystemTime(NOW + 120_000); await addManual(3, '12', '22', 'radec')
    await click(run())
    expect((iodBodies[0] as { obs: Array<Record<string, number>> }).obs[0]).toMatchObject({ ra_deg: 10.123456, dec_deg: 20.654321 })
  })

  it('Remove Entry on a valid row asks first; No keeps it; Yes empties it', async () => {
    await openDialog()
    await addManual(1, '10', '20', 'radec')
    await click(cell(1, 'iod-menu-btn'))
    await click(q('[data-testid="iod-remove"]')!)
    expect(q('[data-testid="iod-confirm"]')!.textContent).toContain('Remove row 1?')
    await click(q('[data-testid="iod-confirm-no"]')!)
    expect(row(1).dataset.state).toBe('valid')
    await click(cell(1, 'iod-menu-btn'))
    await click(q('[data-testid="iod-remove"]')!)
    await click(q('[data-testid="iod-confirm-yes"]')!)
    expect(row(1).dataset.state).toBe('empty')
  })

  it('when the gateway is down an Az / El row is not valid; a RA / Dec row stays valid', async () => {
    await openDialog()
    transformFails = true
    await addManual(1, '10', '20', 'azel')
    expect(row(1).dataset.state).toBe('partial')
    expect(cell(1, 'iod-ra').textContent!.trim()).toBe('—')
    expect(run().title).toBe('Row 1 has no R.A. / Dec. yet')
    await addManual(2, '10', '20', 'radec')
    expect(rowValidState(2)).toBe(true) // RA / Dec known: usable without H4
    expect(cell(2, 'iod-ra').textContent!.trim()).toBe('10.000°')
  })

  it('recall after close / reopen; the memo is saved on close only; a pending / failed astrometry row renders its text', async () => {
    await openDialog()
    expect(getIodMemo()).toBeNull() // the immediate watch at mount saved nothing
    await fillThree()
    await click(run())
    await closeReopen()
    expect(qa('[data-testid="iod-row"]').every(r => r.dataset.state === 'valid')).toBe(true)
    expect(q('[data-testid="iod-status"]')!.textContent!.trim()).toBe('Done · Gauss + Herrick-Gibbs')
    expect(q('[data-testid="iod-state"]')!.textContent).toContain('R_2 : [')
    // constructed astrometry rows
    const memo = getIodMemo()!
    setIodMemo({ ...memo, rows: [astrometryPending(NOW / 1000), astrometryFailed(NOW / 1000), memo.rows[2]!] })
    w!.unmount(); w = null; document.body.innerHTML = ''
    await openDialog()
    expect(cell(1, 'iod-row-status').textContent!.trim()).toBe('Astrometry pending')
    expect(cell(2, 'iod-row-status').textContent!.trim()).toBe('Astrometry failed')
    expect(cell(1, 'iod-source').textContent!.trim()).toBe('Astrometry')
    // a failed row is replaced without a prompt
    await click(cell(2, 'iod-menu-btn'))
    await click(q('[data-testid="iod-add-manual"]')!)
    expect(q('[data-testid="iod-confirm"]')).toBeNull()
  })

  it('the Epoch header sorts; slot numbers stay with their rows', async () => {
    await openDialog()
    vi.setSystemTime(NOW + 120_000); await addManual(1, '10', '20', 'radec')
    vi.setSystemTime(NOW); await addManual(2, '11', '21', 'radec')
    expect(qa('[data-testid="iod-row"]').map(r => r.dataset.slot)).toEqual(['2', '1', '3'])
    await click(q('[data-testid="iod-sort-epoch"]')!)
    expect(qa('[data-testid="iod-row"]').map(r => r.dataset.slot)).toEqual(['1', '2', '3'])
    expect(q('[data-testid="iod-sort-epoch"]')!.textContent).toContain('▼')
  })
})
